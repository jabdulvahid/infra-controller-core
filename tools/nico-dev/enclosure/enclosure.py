#!/usr/bin/env python3
"""Enclosure: one DPU stand-in with its two BMCs, as plain libvirt on a hypervisor.

An enclosure is what you plug into a NICo site to get a managed DPU (and, later,
its server) without hardware: an arm64 DPU VM running real HBN and the real
NICo agent, a DPU BMC and a host BMC (two bmc-mock processes), on bridges the
hypervisor already has. The tool knows nothing about the site beyond two
inputs, the site's root CA and (optionally) its API URL; everything else the
DPU needs arrives by DHCP from the site, and registering the enclosure with the
site (the expected machine) is done outside the tool from the identity table
it prints.

  enclosure.py create  <file> [--reuse-bridge]   build and start; idempotent
  enclosure.py status  <file>                    identities, leases, units, reachability
  enclosure.py destroy <file>                    undo create; removes only what it created
  enclosure.py dpu-config <file> --set SECTION.KEY=VALUE ...   edit the agent config on the DPU

The enclosure file (every key with a default may be omitted):

  name: encl1
  bmc:
    index: 1                        # bmc-mock instance index; the pool is 02:00:00:<index>:xx:xx
    host_profile: lenovo_gb300_nvl  # a Redfish-reset profile with exactly one BlueField-3 DPU
    bmc_mock: /path/to/bmc-mock
    cert_dir: ""                    # default: the tool generates a self-signed pair per enclosure
    dpu_firmware: {bmc: BF-25.10-24, uefi: 4.13.2-12-g943a91640d, nic: 32.47.3576, cec: 00.02.0195.0000_n02}
  dpu:
    image: /var/lib/libvirt/images/enclosure-dpu.qcow2
    vcpus: 4
    memory_mb: 4096
    ssh_authorized_keys: []         # extra public keys for the vm login (the tool adds its own)
    agent_image: localhost/nico/forge-dpu-agent:enclosure
    dhcp_image: localhost/nico/forge-dhcp-server:enclosure
    nvue_password: ""               # default generated; env ENCLOSURE_NVUE_PASSWORD wins
    login_password: ""              # default generated; env ENCLOSURE_LOGIN_PASSWORD wins
  networks:
    oob: patch-encl1                # existing bridge: DPU OOB NIC and both BMC namespaces
    uplink: patch-enclup1           # existing bridge: DPU p0 (MTU 9000)
    hostlink: ""                    # default: the tool creates <name>-hl
  site:
    root_ca: /path/to/site-ca.pem   # the site's bootstrap CA; the only site file the DPU needs
    api_url: https://carbide-api.forge
  hypervisor:
    libvirt: qemu:///system
    state_dir: /var/lib/enclosure

Run as root on the hypervisor (libvirt system instance, bridges, namespaces).
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('PyYAML is required (apt install python3-yaml)')

HERE = Path(__file__).resolve().parent
BMC_SH = HERE / 'bmc.sh'
DEFAULT_API_URL = 'https://carbide-api.forge'
DEFAULT_DPU_FIRMWARE = {
    'bmc': 'BF-25.10-24',
    'uefi': '4.13.2-12-g943a91640d',
    'nic': '32.47.3576',
    'cec': '00.02.0195.0000_n02',
}
# Profiles NICo resets over Redfish that carry exactly one BlueField-3 DPU.
# nvidia_dgx_h100 is excluded: NICo force-restarts that vendor/model over IPMI,
# which a standalone bmc-mock does not serve.
HOST_PROFILES = ('lenovo_gb300_nvl', 'supermicro_gb300_nvl')


# ── helpers ──────────────────────────────────────────────────────────────────
class Fail(SystemExit):
    def __init__(self, msg):
        super().__init__(f'enclosure: {msg}')


def run(cmd, check=True, capture=True, env=None, input=None):
    r = subprocess.run(cmd, text=True, capture_output=capture, env=env, input=input)
    if check and r.returncode != 0:
        raise Fail(f'{shlex.join(cmd)} failed ({r.returncode}):\n{(r.stderr or r.stdout or "").strip()}')
    return r


def link_exists(name):
    return subprocess.run(['ip', 'link', 'show', name], capture_output=True).returncode == 0


def mac(index, last):
    return f'02:00:00:{index:02x}:00:{last:02x}'


def mac_digits(m):
    return m.replace(':', '')


def eui64_link_local(m):
    b = [int(x, 16) for x in m.split(':')]
    b[0] ^= 0x02
    return str(ipaddress.IPv6Address(f'fe80::{b[0]:02x}{b[1]:02x}:{b[2]:02x}ff:fe{b[3]:02x}:{b[4]:02x}{b[5]:02x}'))


# ── the file ─────────────────────────────────────────────────────────────────
class Enclosure:
    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = yaml.safe_load(self.path.read_text()) or {}
        self.raw = raw
        self.name = str(raw.get('name') or '').strip()
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,11}', self.name):
            raise Fail('name: 1-12 chars, [a-z0-9-], starting with a letter (it prefixes bridges, namespaces and units)')
        bmc = raw.get('bmc') or {}
        dpu = raw.get('dpu') or {}
        nets = raw.get('networks') or {}
        site = raw.get('site') or {}
        hv = raw.get('hypervisor') or {}

        self.index = int(bmc.get('index', 0))
        if not 1 <= self.index <= 255:
            raise Fail('bmc.index: 1..255 (0 is MAT\'s pool)')
        self.host_profile = str(bmc.get('host_profile') or 'lenovo_gb300_nvl')
        if self.host_profile not in HOST_PROFILES:
            raise Fail(f'bmc.host_profile: one of {", ".join(HOST_PROFILES)} (Redfish reset, one BF3 DPU)')
        self.bmc_mock = Path(str(bmc.get('bmc_mock') or '')).expanduser()
        self.cert_dir = Path(str(bmc.get('cert_dir'))).expanduser() if bmc.get('cert_dir') else None
        fw = dict(DEFAULT_DPU_FIRMWARE)
        fw.update({k: str(v) for k, v in (bmc.get('dpu_firmware') or {}).items()})
        self.dpu_firmware = fw

        self.image = Path(str(dpu.get('image') or '/var/lib/libvirt/images/enclosure-dpu.qcow2')).expanduser()
        self.vcpus = int(dpu.get('vcpus', 4))
        self.memory_mb = int(dpu.get('memory_mb', 4096))
        self.extra_keys = [str(k) for k in (dpu.get('ssh_authorized_keys') or [])]
        self.agent_image = str(dpu.get('agent_image') or 'localhost/nico/forge-dpu-agent:enclosure')
        self.dhcp_image = str(dpu.get('dhcp_image') or 'localhost/nico/forge-dhcp-server:enclosure')
        self.nvue_password_in = os.environ.get('ENCLOSURE_NVUE_PASSWORD') or str(dpu.get('nvue_password') or '')
        self.login_password_in = os.environ.get('ENCLOSURE_LOGIN_PASSWORD') or str(dpu.get('login_password') or '')

        self.oob = str(nets.get('oob') or '')
        self.uplink = str(nets.get('uplink') or '')
        self.hostlink = str(nets.get('hostlink') or '') or f'{self.name}-hl'
        self.hostlink_given = bool(nets.get('hostlink'))
        if not self.oob or not self.uplink:
            raise Fail('networks.oob and networks.uplink are required (existing hypervisor bridges)')

        self.root_ca = Path(str(site.get('root_ca') or '')).expanduser()
        self.api_url = str(site.get('api_url') or DEFAULT_API_URL)

        self.connect = str(hv.get('libvirt') or 'qemu:///system')
        self.state_root = Path(str(hv.get('state_dir') or '/var/lib/enclosure')).expanduser()
        self.state = self.state_root / self.name
        self.dpu_domain = f'{self.name}-dpu'

    # identities derived from the index: what bmc-mock will report for this
    # instance (DPU k = bmc, factory, oob; then host = bmc), plus the DPU VM's
    # own uplink and host-link NICs above anything the mock allocates.
    @property
    def ids(self):
        i = self.index
        oob = mac(i, 3)
        return {
            'index': i,
            'dpu_bmc_mac': mac(i, 1),
            'dpu_factory_mac': mac(i, 2),
            'dpu_oob_mac': oob,
            'dpu_serial': 'MT' + mac_digits(oob),
            'dpu_uplink_mac': mac(i, 0x10),
            'dpu_hostlink_mac': mac(i, 0x11),
            'host_bmc_mac': mac(i, 4),
            'host_chassis_serial': mac_digits(mac(i, 4)),
            'host_profile': self.host_profile,
        }

    def print_identity(self):
        d = self.ids
        print(f'\n{self.name}: identities (bmc-mock instance index {d["index"]})')
        rows = [
            ('DPU BMC MAC', d['dpu_bmc_mac'], 'DPU BMC mock, on ' + self.oob),
            ('DPU factory MAC', d['dpu_factory_mac'], 'host-facing PF; NICo mints the host admin address for it'),
            ('DPU OOB MAC', d['dpu_oob_mac'], 'DPU VM oob_net0, on ' + self.oob),
            ('DPU serial', d['dpu_serial'], 'SMBIOS product serial; expected-machine -d'),
            ('DPU uplink MAC', d['dpu_uplink_mac'], 'DPU VM p0_if, on ' + self.uplink),
            ('DPU host-link MAC', d['dpu_hostlink_mac'], 'DPU VM pf0hpf_if, on ' + self.hostlink),
            ('host BMC MAC', d['host_bmc_mac'], 'host BMC mock; expected-machine --bmc-mac-address'),
            ('host chassis serial', d['host_chassis_serial'], 'expected-machine --chassis-serial-number'),
            ('host profile', d['host_profile'], 'bmc-mock --hardware-profile'),
        ]
        w = max(len(r[0]) for r in rows)
        for k, v, note in rows:
            print(f'  {k:<{w}}  {v:<22} {note}')
        print('\nRegister on the site (from any host with a configured nico-admin-cli), credentials = the mock\'s factory defaults:')
        print(f'  nico-admin-cli expected-machine add --bmc-mac-address {d["host_bmc_mac"]} '
              f'--bmc-username <mock-user> --bmc-password <mock-password> '
              f'--chassis-serial-number {d["host_chassis_serial"]} -d {d["dpu_serial"]}')


# ── state dir ────────────────────────────────────────────────────────────────
def state_read(enc, name, default=None):
    p = enc.state / name
    return json.loads(p.read_text()) if p.exists() else default


def state_write(enc, name, data, mode=0o644):
    enc.state.mkdir(parents=True, exist_ok=True)
    p = enc.state / name
    p.write_text(json.dumps(data, indent=2) + '\n')
    p.chmod(mode)


def other_enclosure_with_index(enc):
    if not enc.state_root.exists():
        return None
    for d in enc.state_root.iterdir():
        if d.name == enc.name or not (d / 'identity.json').exists():
            continue
        try:
            if json.loads((d / 'identity.json').read_text()).get('index') == enc.index:
                return d.name
        except Exception:
            continue
    return None


# ── checks ───────────────────────────────────────────────────────────────────
def check_host(enc, for_create=True):
    if os.geteuid() != 0:
        raise Fail('run as root (sudo): libvirt system instance, bridges and namespaces')
    missing = [c for c in ('virsh', 'virt-install', 'qemu-img', 'ip', 'ssh', 'ssh-keygen') if not shutil.which(c)]
    if missing:
        raise Fail('missing on the hypervisor: ' + ', '.join(missing))
    if not any(shutil.which(c) for c in ('cloud-localds', 'genisoimage', 'xorriso')):
        raise Fail('need cloud-localds (cloud-image-utils), genisoimage or xorriso for the seed ISO')
    if not for_create:
        return
    for label, p in (('dpu.image', enc.image), ('bmc.bmc_mock', enc.bmc_mock), ('site.root_ca', enc.root_ca)):
        if not p.is_file():
            raise Fail(f'{label}: {p} is not a file')
    if enc.cert_dir is not None and (not (enc.cert_dir / 'tls.crt').is_file() or not (enc.cert_dir / 'tls.key').is_file()):
        raise Fail(f'bmc.cert_dir: tls.crt and tls.key not in {enc.cert_dir}')
    if enc.cert_dir is None and not shutil.which('openssl'):
        raise Fail('openssl missing (needed to generate the BMC mocks\' TLS pair; or set bmc.cert_dir)')
    if not os.access(enc.bmc_mock, os.X_OK):
        raise Fail(f'bmc.bmc_mock: {enc.bmc_mock} is not executable')
    if not BMC_SH.is_file():
        raise Fail(f'{BMC_SH} missing next to this tool')
    for label, br in (('networks.oob', enc.oob), ('networks.uplink', enc.uplink)):
        if not link_exists(br):
            raise Fail(f'{label}: bridge {br} does not exist on this hypervisor (nico-dev: enclosure/cable.sh create)')
    if enc.hostlink_given and not link_exists(enc.hostlink):
        raise Fail(f'networks.hostlink: bridge {enc.hostlink} does not exist')
    if not enc.root_ca.read_text().lstrip().startswith('-----BEGIN CERTIFICATE-----'):
        raise Fail(f'site.root_ca: {enc.root_ca} is not a PEM certificate')
    other = other_enclosure_with_index(enc)
    if other:
        raise Fail(f'bmc.index {enc.index} is already used by enclosure {other!r} in {enc.state_root}')


# ── create ───────────────────────────────────────────────────────────────────
def ensure_secrets(enc):
    s = state_read(enc, 'secrets.json', {})
    changed = False
    for key, given in (('nvue_password', enc.nvue_password_in), ('login_password', enc.login_password_in)):
        if given and s.get(key) != given:
            s[key] = given
            changed = True
        elif not s.get(key):
            s[key] = secrets.token_hex(8)
            changed = True
    if changed:
        state_write(enc, 'secrets.json', s, 0o600)
    return s


def ensure_ssh_key(enc):
    d = enc.state / 'ssh'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = d / 'id_ed25519'
    if not key.exists():
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'enclosure-{enc.name}', '-f', str(key)])
    return key


def ensure_mock_certs(enc):
    """The mocks' Redfish listener needs a TLS pair; NICo's explorer does not
    verify BMC certificates, so a self-signed pair per enclosure is enough."""
    if enc.cert_dir is not None:
        return enc.cert_dir
    d = enc.state / 'certs'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (d / 'tls.crt').exists() or not (d / 'tls.key').exists():
        run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
             '-nodes', '-days', '3650', '-subj', f'/CN=enclosure-{enc.name}-bmc',
             '-keyout', str(d / 'tls.key'), '-out', str(d / 'tls.crt')])
        (d / 'tls.key').chmod(0o600)
        print(f'  BMC TLS pair: generated in {d}')
    return d


def ensure_hostlink_bridge(enc, reuse):
    created = state_read(enc, 'bridges.json', [])
    if link_exists(enc.hostlink):
        if enc.hostlink in created or enc.hostlink_given or reuse:
            if reuse and enc.hostlink not in created and not enc.hostlink_given:
                created.append(enc.hostlink)
                state_write(enc, 'bridges.json', created)
            print(f'  host-link bridge {enc.hostlink}: present')
            return
        raise Fail(f'bridge {enc.hostlink} exists but was not created by this enclosure; '
                   f'name it in networks.hostlink to use it, or pass --reuse-bridge to take it over')
    run(['ip', 'link', 'add', enc.hostlink, 'type', 'bridge'])
    run(['ip', 'link', 'set', enc.hostlink, 'mtu', '9000'])
    run(['ip', 'link', 'set', enc.hostlink, 'up'])
    created.append(enc.hostlink)
    state_write(enc, 'bridges.json', created)
    print(f'  host-link bridge {enc.hostlink}: created (private, MTU 9000)')


def write_seed(enc, secrets_, key):
    d = enc.ids
    seed = enc.state / 'seed'
    seed.mkdir(parents=True, exist_ok=True)
    # systemd EnvironmentFile keeps trailing "# ..." as part of the value: no inline comments.
    dpu_env = (
        f'DPU_OOB_MAC={d["dpu_oob_mac"]}\n'
        f'DPU_UPLINK_MAC={d["dpu_uplink_mac"]}\n'
        f'DPU_HOSTLINK_MAC={d["dpu_hostlink_mac"]}\n'
        f'DPU_FACTORY_MAC={d["dpu_factory_mac"]}\n'
        f'DPU_SERIAL={d["dpu_serial"]}\n'
        f'NVUE_USERNAME=carbide\n'
        f'NVUE_PASSWORD={secrets_["nvue_password"]}\n'
        f'NICO_DPU_AGENT_IMAGE={enc.agent_image}\n'
        f'NICO_DHCP_SERVER_IMAGE={enc.dhcp_image}\n'
    )
    (seed / 'dpu.env').write_text(dpu_env)
    (seed / 'dpu.env').chmod(0o600)
    keys = [key.with_suffix('.pub').read_text().strip()] + enc.extra_keys
    files = [
        ('/etc/enclosure/dpu.env', '0600', dpu_env),
        ('/opt/forge/forge_root.pem', '0644', enc.root_ca.read_text()),
    ]
    if enc.api_url != DEFAULT_API_URL:
        files.append(('/opt/forge/config.toml', '0644', f'[forge-system]\napi-server = "{enc.api_url}"\n'))
    user_data = ['#cloud-config', 'users:', '  - name: vm', '    groups: [adm, sudo]', '    shell: /bin/bash',
                 '    sudo: ALL=(ALL) NOPASSWD:ALL', '    lock_passwd: false', '    ssh_authorized_keys:']
    user_data += [f'      - {k}' for k in keys]
    user_data += ['chpasswd:', '  expire: false', '  users:', '    - name: vm',
                  f'      password: {secrets_["login_password"]}', '      type: text', 'ssh_pwauth: false',
                  'write_files:']
    for path, mode, content in files:
        b64 = base64.b64encode(content.encode()).decode()
        user_data += [f'  - path: {path}', f'    permissions: "{mode}"', '    encoding: b64', f'    content: {b64}']
    (seed / 'user-data').write_text('\n'.join(user_data) + '\n')
    (seed / 'meta-data').write_text(f'instance-id: enclosure-{enc.name}-dpu\nlocal-hostname: {enc.dpu_domain}\n')
    iso = seed / 'seed.iso'
    if shutil.which('cloud-localds'):
        run(['cloud-localds', str(iso), str(seed / 'user-data'), str(seed / 'meta-data')])
    elif shutil.which('genisoimage'):
        run(['genisoimage', '-quiet', '-output', str(iso), '-volid', 'cidata', '-joliet', '-rock',
             str(seed / 'user-data'), str(seed / 'meta-data')])
    else:
        run(['xorriso', '-as', 'mkisofs', '-quiet', '-output', str(iso), '-volid', 'cidata', '-joliet', '-rock',
             str(seed / 'user-data'), str(seed / 'meta-data')])
    if subprocess.run(['id', 'libvirt-qemu'], capture_output=True).returncode == 0:
        shutil.chown(iso, 'libvirt-qemu', 'kvm')
    os.chmod(iso, 0o640)
    return iso


def domain_state(enc):
    r = run(['virsh', '--connect', enc.connect, 'domstate', enc.dpu_domain], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def define_dpu(enc, iso):
    d = enc.ids
    if domain_state(enc) is not None:
        print(f'  DPU VM {enc.dpu_domain}: present ({domain_state(enc)})')
        return
    disk = enc.state / 'disk.qcow2'
    if not disk.exists():
        run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-b', str(enc.image), '-F', 'qcow2', str(disk)])
        if subprocess.run(['id', 'libvirt-qemu'], capture_output=True).returncode == 0:
            shutil.chown(disk, 'libvirt-qemu', 'kvm')
    # NIC order = dpu.env order: OOB, uplink (p0), host link (pf0hpf). The SMBIOS
    # identity must say BlueField in both the product and the board name: Core
    # classifies a report as a DPU from the board name, the agent from the product.
    run(['virt-install', '--connect', enc.connect, '--name', enc.dpu_domain,
         '--arch', 'aarch64', '--machine', 'virt', '--virt-type', 'qemu', '--cpu', 'cortex-a72',
         '--vcpus', str(enc.vcpus), '--memory', str(enc.memory_mb), '--boot', 'uefi', '--import',
         '--sysinfo', f'system.manufacturer=Nvidia,system.product="BlueField SoC",system.serial={d["dpu_serial"]},'
                      f'baseBoard.manufacturer=Nvidia,baseBoard.product="BlueField SoC"',
         '--disk', f'path={disk},bus=virtio',
         '--disk', f'path={iso},device=cdrom,readonly=on',
         '--network', f'bridge={enc.oob},model=virtio,mac={d["dpu_oob_mac"]}',
         '--network', f'bridge={enc.uplink},model=virtio,mac={d["dpu_uplink_mac"]}',
         '--network', f'bridge={enc.hostlink},model=virtio,mac={d["dpu_hostlink_mac"]}',
         '--graphics', 'none', '--console', 'pty,target.type=serial',
         '--osinfo', 'detect=on,require=off', '--noautoconsole'])
    print(f'  DPU VM {enc.dpu_domain}: defined and started')


def set_tap_mtus(enc):
    # virsh attach has no --mtu on older libvirt; the uplink and host-link taps
    # are set on the hypervisor side, the guest side by HBN.
    r = run(['virsh', '--connect', enc.connect, 'domiflist', enc.dpu_domain], check=False)
    if r.returncode != 0:
        return
    d = enc.ids
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[4].lower() in (d['dpu_uplink_mac'], d['dpu_hostlink_mac']) and parts[0] != '-':
            subprocess.run(['ip', 'link', 'set', parts[0], 'mtu', '9000'], capture_output=True)


def start_mocks(enc):
    d = enc.ids
    fw = enc.dpu_firmware
    common = ['--bridge', enc.oob, '--profile', enc.host_profile, '--instance', str(enc.index),
              '--bin', str(enc.bmc_mock), '--certs', str(ensure_mock_certs(enc))]
    run(['bash', str(BMC_SH), 'create', enc.name, 'dpu', d['dpu_bmc_mac'], *common, '--dpu-index', '0', '--',
         '--dpu-bmc-firmware', fw['bmc'], '--dpu-uefi-firmware', fw['uefi'],
         '--dpu-nic-firmware', fw['nic'], '--dpu-cec-firmware', fw['cec']], capture=False)
    run(['bash', str(BMC_SH), 'create', enc.name, 'host', d['host_bmc_mac'], *common], capture=False)


def mock_status(enc):
    r = run(['bash', str(BMC_SH), 'status', enc.name], check=False)
    out = {}
    for line in r.stdout.splitlines():
        m = re.match(r'(host|dpu)\s+.*addr=(\S+)\s+unit=(\S+)(?:\s+redfish=(\d+))?', line)
        if m:
            out[m.group(1)] = {'addr': m.group(2), 'unit': m.group(3), 'redfish': m.group(4) or '-'}
    return out


def dpu_ssh_target(enc):
    return f'vm@{eui64_link_local(enc.ids["dpu_oob_mac"])}%{enc.oob}'


def dpu_ssh(enc, cmd, timeout=15):
    key = enc.state / 'ssh' / 'id_ed25519'
    r = subprocess.run(['ssh', '-i', str(key), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
                        '-o', f'ConnectTimeout={timeout}', dpu_ssh_target(enc), cmd],
                       text=True, capture_output=True)
    return r


def dpu_probe(enc):
    """What the DPU says about itself over the OOB bridge's link-local address."""
    r = dpu_ssh(enc, "ip -4 -o addr show oob_net0 | awk '{print $4}'; "
                     "systemctl is-active enclosure-hbn enclosure-nico-dhcp-server enclosure-nico-dpu-agent | tr '\\n' ' '; echo; "
                     "cat /usr/local/lib/enclosure/provenance 2>/dev/null | tr '\\n' ' '")
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines() + ['', '', '']
    return {'oob_addr': lines[0].strip() or '-', 'units': lines[1].strip(), 'provenance': lines[2].strip()}


def wait_for(enc, seconds):
    deadline = time.time() + seconds
    leases, dpu = {}, None
    while time.time() < deadline:
        leases = mock_status(enc)
        ok = all(k in leases and leases[k]['addr'] != 'none' and leases[k]['redfish'] == '200' for k in ('dpu', 'host'))
        dpu = dpu_probe(enc)
        if ok and dpu and dpu['oob_addr'] != '-':
            break
        time.sleep(10)
    return leases, dpu


def cmd_create(enc, args):
    check_host(enc)
    print(f'{enc.name}: create')
    enc.state.mkdir(parents=True, exist_ok=True)
    state_write(enc, 'identity.json', enc.ids)
    secrets_ = ensure_secrets(enc)
    key = ensure_ssh_key(enc)
    ensure_hostlink_bridge(enc, args.reuse_bridge)
    iso = write_seed(enc, secrets_, key)
    define_dpu(enc, iso)
    set_tap_mtus(enc)
    print('  BMC mocks:')
    start_mocks(enc)
    print(f'  waiting up to {args.wait}s for the three leases (mocks via bmc.sh, DPU via ssh on the OOB bridge)...')
    leases, dpu = wait_for(enc, args.wait)
    report(enc, leases, dpu)
    if not dpu or dpu['oob_addr'] == '-':
        print('\n  The DPU has not taken its OOB lease yet. First boot renames the NICs and reboots once; '
              'under TCG allow several minutes, then rerun `status`. If the mocks have leases and the DPU never '
              'does, check the OOB cable and the site\'s relay on that leaf port.')
    enc.print_identity()


def report(enc, leases, dpu):
    print(f'\n{enc.name}: status')
    created = state_read(enc, 'bridges.json', [])
    for label, br in (('oob', enc.oob), ('uplink', enc.uplink), ('hostlink', enc.hostlink)):
        kind = 'created by this enclosure' if br in created else 'external'
        print(f'  bridge {label:<8} {br:<16} {"up" if link_exists(br) else "MISSING"}  ({kind})')
    print(f'  DPU VM   {enc.dpu_domain:<16} {domain_state(enc) or "not defined"}')
    for role in ('dpu', 'host'):
        l = leases.get(role)
        print(f'  BMC {role:<5} ' + (f'{l["addr"]:<18} unit={l["unit"]} redfish={l["redfish"]}' if l else 'not running'))
    if dpu:
        print(f'  DPU OOB  {dpu["oob_addr"]:<18} units: {dpu["units"]}')
        if dpu['provenance']:
            print(f'  DPU image {dpu["provenance"]}')
    else:
        print(f'  DPU      unreachable over {dpu_ssh_target(enc)} (not booted yet, or no link-local on the bridge)')


def cmd_status(enc, args):
    check_host(enc, for_create=False)
    report(enc, mock_status(enc), dpu_probe(enc))
    enc.print_identity()


def cmd_destroy(enc, args):
    check_host(enc, for_create=False)
    print(f'{enc.name}: destroy')
    if domain_state(enc) is not None:
        run(['virsh', '--connect', enc.connect, 'destroy', enc.dpu_domain], check=False)
        run(['virsh', '--connect', enc.connect, 'undefine', '--nvram', enc.dpu_domain])
        print(f'  DPU VM {enc.dpu_domain}: removed')
    run(['bash', str(BMC_SH), 'destroy', enc.name], capture=False, check=False)
    for br in state_read(enc, 'bridges.json', []):
        if link_exists(br):
            run(['ip', 'link', 'del', br])
            print(f'  bridge {br}: removed')
    if enc.state.exists():
        shutil.rmtree(enc.state)
        print(f'  state {enc.state}: removed')


def cmd_dpu_config(enc, args):
    check_host(enc, for_create=False)
    sets = {}
    for item in args.set:
        m = re.fullmatch(r'([a-z-]+)\.([a-z-]+)=(.*)', item)
        if not m:
            raise Fail(f'--set wants SECTION.KEY=VALUE, got {item!r}')
        sets.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    r = dpu_ssh(enc, 'sudo cat /opt/forge/config.toml 2>/dev/null || true')
    if r.returncode != 0:
        raise Fail(f'cannot reach the DPU over {dpu_ssh_target(enc)}: {r.stderr.strip()}')
    text = r.stdout
    for section, kv in sets.items():
        header = f'[{section}]'
        if header not in text:
            text = text.rstrip('\n') + ('\n\n' if text.strip() else '') + header + '\n'
        for k, v in kv.items():
            val = v if re.fullmatch(r'(true|false|-?\d+)', v) else f'"{v}"'
            pat = re.compile(rf'(?ms)(^\[{re.escape(section)}\]\n(?:(?!^\[).*\n)*?)^{re.escape(k)}\s*=.*\n')
            if pat.search(text):
                text = pat.sub(rf'\g<1>{k} = {val}\n', text, count=1)
            else:
                text = text.replace(header + '\n', f'{header}\n{k} = {val}\n', 1)
    r = subprocess.run(['ssh', '-i', str(enc.state / 'ssh' / 'id_ed25519'), '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
                        dpu_ssh_target(enc),
                        'sudo tee /opt/forge/config.toml >/dev/null' + (' && sudo systemctl restart enclosure-nico-dpu-agent' if args.restart_agent else '')],
                       text=True, input=text, capture_output=True)
    if r.returncode != 0:
        raise Fail(f'writing the agent config failed: {r.stderr.strip()}')
    print(f'{enc.name}: /opt/forge/config.toml updated' + (' and the agent restarted' if args.restart_agent else ''))
    print(text)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split('\n', 2)[2])
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('create', help='build and start the enclosure (idempotent)')
    c.add_argument('file')
    c.add_argument('--reuse-bridge', action='store_true', help='take over an existing, unrecorded host-link bridge of the derived name')
    c.add_argument('--wait', type=int, default=600, help='seconds to wait for the leases (default 600)')
    c.set_defaults(fn=cmd_create)
    s = sub.add_parser('status', help='identities, bridges, VM, mocks, DPU leases and units')
    s.add_argument('file')
    s.set_defaults(fn=cmd_status)
    d = sub.add_parser('destroy', help='remove the VM, the mocks, the bridges this enclosure created, and its state')
    d.add_argument('file')
    d.set_defaults(fn=cmd_destroy)
    k = sub.add_parser('dpu-config', help='edit the agent config.toml on the DPU over ssh')
    k.add_argument('file')
    k.add_argument('--set', action='append', default=[], metavar='SECTION.KEY=VALUE', required=True)
    k.add_argument('--no-restart-agent', dest='restart_agent', action='store_false')
    k.set_defaults(fn=cmd_dpu_config)
    args = p.parse_args()
    enc = Enclosure(args.file)
    args.fn(enc, args)


if __name__ == '__main__':
    main()
