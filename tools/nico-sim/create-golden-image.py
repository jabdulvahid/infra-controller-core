#!/usr/bin/env python3
"""
DC Simulation — Golden Image Builder

Creates a pre-baked Ubuntu qcow2 image for CP host VMs with all packages
and Kubernetes prerequisites pre-installed:
  - Common packages (python3, curl, net-tools, etc.)
  - containerd + runc (container runtime, SystemdCgroup configured)
  - kubelet + kubeadm + kubectl (pinned version from nico-sim.yaml)
  - Kernel modules: overlay, br_netfilter
  - sysctl: bridge-nf-call-iptables, ip_forward
  - swap disabled

Run once in the dev environment. No fabric dependency — uses a simple
temporary NAT network for internet access during package installation.

On deployment, CP host VMs boot from this image and cloud-init only runs
the fast steps (hostname, network-config, SSH key, bootcmd) — skipping
package installation entirely. Boot time: ~30s vs ~3-5 min.

Usage:
  sudo python3 create-golden-image.py nico-sim.yaml [--output /path/to/golden.qcow2]

After creation, set golden_image in nico-sim.yaml:
  control_plane:
    golden_image: /var/lib/libvirt/images/cp-golden.qcow2

Then re-run: python3 generate-nodes.py ... && sudo ./node-fabric/deploy-nodes.sh
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)

# Builder VM constants — temporary, destroyed after successful image export.
# Disk/seed paths are defaults; --work-dir relocates them (set in main()).
BUILDER_NET  = 'golden-builder'
BUILDER_VM   = 'golden-builder-vm'
BUILDER_MAC  = '52:54:00:ff:ff:01'
BUILDER_DISK = '/tmp/golden-builder.qcow2'
BUILDER_SEED = '/tmp/golden-builder-seed.iso'
BUILDER_NET_XML = '/tmp/golden-builder-net.xml'


# ── Arg parsing / config ──────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Build pre-baked golden image for CP or MH VMs',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('sim_yaml', help='nico-sim.yaml')
    p.add_argument('--target', default='cp', choices=['cp', 'mh'],
                   help='Which golden image to build: '
                        'cp = control-plane (kubeadm prereqs), '
                        'mh = managed-host (minimal, no k8s)')
    p.add_argument('--output', default=None,
                   help='Output path (default: golden_image.{target}.path from nico-sim.yaml)')
    p.add_argument('--ssh-key', default=None,
                   help='SSH key for builder VM access — private key path, or a '
                        '.pub with its private half next to it (public key is '
                        'derived from the private key; must have no passphrase)')
    p.add_argument('--work-dir', default='/tmp',
                   help='Directory for the temporary builder VM disk and seed ISO. '
                        'The disk can grow to 40G during package installation — '
                        'point this at a filesystem with enough free space '
                        '(beware: /tmp is a small tmpfs on some distros)')
    return p.parse_args()


def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── SSH key helpers ───────────────────────────────────────────────────────────

def assert_key_usable(priv_path):
    """
    Reject passphrase-protected keys upfront. The script SSHes with
    BatchMode=yes (no prompts), so an encrypted key fails every probe
    silently and the build burns the full cloud-init timeout for nothing.
    """
    r = run(['ssh-keygen', '-y', '-P', '', '-f', str(priv_path)])
    if r.returncode != 0:
        raise RuntimeError(
            f'SSH private key {priv_path} is passphrase-protected (or unreadable).\n'
            f'  BatchMode SSH cannot use it. Either:\n'
            f'    - create a dedicated key:  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_nico_sim\n'
            f'      and pass it:             --ssh-key ~/.ssh/id_nico_sim\n'
            f'    - or remove the passphrase: ssh-keygen -p -N "" -f {priv_path}')
    return r.stdout.strip()   # the derived public key


def find_ssh_priv_key():
    # Script runs as root (sudo); find calling user's key via SUDO_USER
    caller = os.environ.get('SUDO_USER', '')
    candidates = []
    if caller:
        for k in ['id_nico_sim', 'id_ed25519', 'id_rsa', 'id_ecdsa']:
            candidates.append(Path(f'/home/{caller}/.ssh/{k}'))
    for k in ['~/.ssh/id_nico_sim', '~/.ssh/id_ed25519', '~/.ssh/id_rsa']:
        candidates.append(Path(k).expanduser())
    for p in candidates:
        if p.exists():
            return p
    raise RuntimeError('No SSH private key found. Run ssh-keygen first.')


def resolve_ssh_keys(override=None):
    """
    Return (pub_key_text, priv_key_path).

    --ssh-key accepts either half of the pair: a private key, or a .pub file
    with its private half next to it. The injected public key is ALWAYS
    derived from the private key (ssh-keygen -y), so the pair can never
    mismatch — a stale/regenerated .pub file cannot lock the script out.
    """
    if override:
        p = Path(override).expanduser()
        if not p.exists():
            raise FileNotFoundError(f'SSH key not found: {override}')
        priv = p.with_suffix('') if p.suffix == '.pub' else p
        if not priv.exists():
            raise FileNotFoundError(
                f'{p} is a public key but its private half {priv} does not exist.\n'
                f'  Pass the private key with --ssh-key instead.')
    else:
        priv = find_ssh_priv_key()
    pub = assert_key_usable(priv)
    return pub, str(priv)


# ── cloud-init for builder VM ─────────────────────────────────────────────────

def gen_mh_builder_user_data(sim, ssh_pub_key):
    """
    cloud-init user-data for building the MH golden image.
    Installs a minimal package set — no kubernetes, no containerd.
    MH VMs are tenant workload hosts; they only need basic Linux tools
    so cloud-init can complete without network access at boot time.
    """
    cp      = sim['control_plane']
    dns     = cp.get('dns_servers', ['10.126.136.6', '10.126.136.22'])
    ns_lines = '\\n'.join(f'nameserver {ns}' for ns in dns)

    lines = []
    lines.append('#cloud-config')
    lines.append('hostname: mh-golden-builder')
    lines.append('manage_etc_hosts: true')
    lines.append('')
    lines.append('ssh_authorized_keys:')
    lines.append(f'  - {ssh_pub_key.strip()}')
    lines.append('')
    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('')
    lines.append('packages:')
    lines.append('  - python3')
    lines.append('  - curl')
    lines.append('  - wget')
    lines.append('  - net-tools')
    lines.append('  - iputils-ping')
    lines.append('  - iproute2')
    lines.append('  - openssh-server')
    lines.append('  - dnsmasq')      # for sim-dpu-agent on mh-dpu VMs
    lines.append('  - ncat')
    lines.append('')
    lines.append('package_update: true')
    lines.append('package_upgrade: true')
    lines.append('')
    lines.append('runcmd:')
    lines.append('  - systemctl enable ssh')
    lines.append('  - echo "MH golden image build complete"')
    lines.append('')
    lines.append('final_message: "MH golden image ready"')
    return '\n'.join(lines)


def gen_builder_user_data(sim, ssh_pub_key):
    """
    cloud-init user-data for the temporary builder VM.
    Installs all packages + kubeadm prereqs; runs runcmd to configure them.
    """
    # golden_image.control_plane.kubernetes is builder config (not CP runtime config)
    cp      = sim['control_plane']
    k8s_ver = sim.get('golden_image', {}).get('control_plane', {}).get(
              'kubernetes', {}).get('version', '1.32')
    dns     = cp.get('dns_servers', ['10.126.136.6', '10.126.136.22'])
    ns_lines = '\\n'.join(f'nameserver {ns}' for ns in dns)

    lines = []
    lines.append('#cloud-config')
    lines.append('hostname: golden-builder')
    lines.append('manage_etc_hosts: true')
    lines.append('')
    lines.append('ssh_authorized_keys:')
    lines.append(f'  - {ssh_pub_key.strip()}')
    lines.append('')
    # Write resolv.conf early — NVIDIA blocks public DNS port 53
    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('')
    lines.append('packages:')
    lines.append('  - apt-transport-https')
    lines.append('  - ca-certificates')
    lines.append('  - curl')
    lines.append('  - gnupg')
    lines.append('  - gpg')
    lines.append('  - wget')
    lines.append('  - python3')
    lines.append('  - python3-pip')
    lines.append('  - net-tools')
    lines.append('  - iputils-ping')
    lines.append('  - traceroute')
    lines.append('  - lsb-release')
    lines.append('  - containerd')
    lines.append('package_update: true')
    lines.append('')
    lines.append('write_files:')
    lines.append('  - path: /etc/modules-load.d/k8s.conf')
    lines.append('    owner: root:root')
    lines.append('    permissions: "0644"')
    lines.append('    content: |')
    lines.append('      overlay')
    lines.append('      br_netfilter')
    lines.append('')
    lines.append('  - path: /etc/sysctl.d/k8s.conf')
    lines.append('    owner: root:root')
    lines.append('    permissions: "0644"')
    lines.append('    content: |')
    lines.append('      net.bridge.bridge-nf-call-iptables  = 1')
    lines.append('      net.bridge.bridge-nf-call-ip6tables = 1')
    lines.append('      net.ipv4.ip_forward                 = 1')
    lines.append('')
    lines.append('runcmd:')
    lines.append('  - modprobe overlay')
    lines.append('  - modprobe br_netfilter')
    lines.append('  - sysctl --system')
    lines.append('  - swapoff -a')
    lines.append("  - sed -i '/ swap / s/^\\(.*\\)$/#\\1/g' /etc/fstab")
    lines.append('  - mkdir -p /etc/containerd')
    lines.append("  - sh -c 'containerd config default > /etc/containerd/config.toml'")
    lines.append("  - sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml")
    lines.append('  - systemctl restart containerd')
    lines.append('  - systemctl enable containerd')
    lines.append('  - mkdir -p /etc/apt/keyrings')
    lines.append(f'  - sh -c \'curl -fsSL https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/Release.key'
                 f' | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg\'')
    lines.append(f'  - sh -c \'echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg]'
                 f' https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/ /"'
                 f' > /etc/apt/sources.list.d/kubernetes.list\'')
    lines.append('  - apt-get update')
    lines.append('  - apt-get install -y kubelet kubeadm kubectl')
    lines.append('  - apt-mark hold kubelet kubeadm kubectl')
    lines.append('  - systemctl enable kubelet')
    lines.append('')
    lines.append('final_message: |')
    lines.append('  Golden image builder: cloud-init complete.')
    lines.append(f'  Installed: containerd, kubelet, kubeadm, kubectl v{k8s_ver}')
    lines.append('  Ready for image cleanup and export.')

    return '\n'.join(lines) + '\n'


def gen_builder_network_xml():
    """Temporary libvirt NAT network for internet access during build."""
    return """\
<network>
  <name>golden-builder</name>
  <bridge name="virbr-golden"/>
  <forward mode="nat"/>
  <ip address="192.168.199.1" netmask="255.255.255.0">
    <dhcp>
      <range start="192.168.199.10" end="192.168.199.50"/>
    </dhcp>
  </ip>
</network>
"""


# ── libvirt / process helpers ─────────────────────────────────────────────────

def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check(cmd, **kw):
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise RuntimeError(f'Command failed: {" ".join(str(c) for c in cmd)}')
    return r


def virsh(*args):
    return run(['virsh'] + list(args))


SSH_OPTS = ['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes']


def ssh(oob_ip, priv_key, cmd, timeout=30, capture=True):
    full = ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', cmd]
    return subprocess.run(full, capture_output=capture, text=True, timeout=timeout)


# ── Builder VM lifecycle ──────────────────────────────────────────────────────

def create_builder_network():
    if virsh('net-info', BUILDER_NET).returncode == 0:
        print(f'  Builder network {BUILDER_NET} already exists')
        return
    Path(BUILDER_NET_XML).write_text(gen_builder_network_xml())
    check(['virsh', 'net-define', BUILDER_NET_XML])
    check(['virsh', 'net-autostart', BUILDER_NET])
    check(['virsh', 'net-start', BUILDER_NET])
    Path(BUILDER_NET_XML).unlink(missing_ok=True)
    print(f'  Created builder network {BUILDER_NET} (NAT, 192.168.199.0/24)')


def get_builder_oob_ip():
    """Parse virsh DHCP lease: $1=date $2=time $3=mac $4=proto $5=ip/prefix."""
    r = virsh('net-dhcp-leases', BUILDER_NET)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2].lower() == BUILDER_MAC.lower():
            return parts[4].split('/')[0]
    return None


def wait_for_dhcp(max_wait=180):
    print('  Waiting for builder VM DHCP lease', end='', flush=True)
    for i in range(0, max_wait, 5):
        ip = get_builder_oob_ip()
        if ip:
            print(f' → {ip} ({i}s)')
            return ip
        time.sleep(5)
        print('.', end='', flush=True)
    print()
    raise RuntimeError('Builder VM did not get a DHCP lease after 3 min')


def wait_for_cloud_init(oob_ip, priv_key, timeout=3600, target='cp', ssh_grace=180):
    """Poll cloud-init status until done or error.

    ssh_grace: if not a single SSH probe succeeds within this window, abort —
    the problem is host↔VM SSH (key/firewall), not cloud-init, and waiting
    out the full timeout would only burn an hour to report nothing.
    """
    if target == 'mh':
        print('  Installing packages — minimal MH set, no kubeadm (~3-5 min)')
    else:
        print('  Installing packages + kubeadm prereqs (~10-20 min on slow networks)')
    print(f'  Polling cloud-init via SSH every 15s (ubuntu@{oob_ip}); '
          f'progress line every 60s')
    start = time.time()
    last_status_print = start
    ssh_ok = False
    while time.time() - start < timeout:
        try:
            r = ssh(oob_ip, priv_key, 'cloud-init status 2>/dev/null', timeout=10)
            if r.returncode == 0:
                ssh_ok = True
                status = r.stdout.strip()
                if 'done' in status:
                    elapsed = int(time.time() - start)
                    print(f'  cloud-init done ({elapsed}s)')
                    return
                if 'error' in status:
                    # Fetch last lines of cloud-init log for diagnosis
                    log_r = ssh(oob_ip, priv_key,
                                'sudo tail -20 /var/log/cloud-init-output.log 2>/dev/null',
                                timeout=10)
                    log_tail = log_r.stdout.strip() if log_r.returncode == 0 else '(unavailable)'
                    raise RuntimeError(
                        f'cloud-init error: {status}\n'
                        f'  Last log lines:\n{log_tail}\n'
                        f'  Full log: virsh console {BUILDER_VM}')
        except subprocess.TimeoutExpired:
            pass

        elapsed = int(time.time() - start)
        if not ssh_ok and elapsed >= ssh_grace:
            raise RuntimeError(
                f'no successful SSH to builder VM {oob_ip} in {elapsed}s — aborting early.\n'
                f'  cloud-init may be running fine; the HOST cannot authenticate to the VM.\n'
                f'  Likely causes: passphrase-protected key, host firewall blocking '
                f'192.168.199.0/24, or SSH key not injected.\n'
                f'  Try manually:  ssh -o BatchMode=yes -i {priv_key} ubuntu@{oob_ip} true\n'
                f'  VM console:    sudo virsh console {BUILDER_VM}')

        # Print a status line every 60s showing elapsed time and last apt activity
        if time.time() - last_status_print >= 60:
            last_status_print = time.time()
            apt_r = ssh(oob_ip, priv_key,
                        'sudo tail -1 /var/log/cloud-init-output.log 2>/dev/null',
                        timeout=10)
            last_line = apt_r.stdout.strip() if apt_r and apt_r.returncode == 0 \
                        else '(ssh unreachable)'
            print(f'  [{elapsed}s] {last_line[:80]}')
        time.sleep(15)

    # On timeout, fetch log tail to help diagnose
    log_r = ssh(oob_ip, priv_key,
                'sudo tail -20 /var/log/cloud-init-output.log 2>/dev/null', timeout=15)
    log_tail = log_r.stdout.strip() if log_r and log_r.returncode == 0 else '(unavailable)'
    raise RuntimeError(
        f'cloud-init timed out after {timeout}s\n'
        f'  Last log lines:\n{log_tail}'
    )


# Binaries that must exist in the builder VM before the image is exported.
# `cloud-init status` reports 'done' even when package modules failed, so a
# fast box and a fast failure look identical without this check.
CP_REQUIRED_BINARIES = ['kubeadm', 'kubelet', 'kubectl', 'containerd']
MH_REQUIRED_BINARIES = ['python3', 'curl', 'wget', 'ping', 'ip', 'dnsmasq', 'ncat']


def verify_image_contents(oob_ip, priv_key, target):
    """Fail loudly if expected binaries are missing — before export, while the
    builder VM is still alive and its logs are inspectable."""
    required = CP_REQUIRED_BINARIES if target == 'cp' else MH_REQUIRED_BINARIES
    print(f'  Checking binaries: {", ".join(required)}')
    probe = '; '.join(f'command -v {b} >/dev/null 2>&1 || echo MISSING:{b}'
                      for b in required)
    r = ssh(oob_ip, priv_key, probe, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f'verification SSH to builder VM failed: {r.stderr.strip()}')
    missing = [l.split(':', 1)[1] for l in r.stdout.splitlines() if l.startswith('MISSING:')]
    if missing:
        log_r = ssh(oob_ip, priv_key,
                    'sudo tail -30 /var/log/cloud-init-output.log 2>/dev/null', timeout=15)
        log_tail = log_r.stdout.strip() if log_r.returncode == 0 else '(unavailable)'
        raise RuntimeError(
            f'image verification FAILED — missing binaries: {", ".join(missing)}\n'
            f'  cloud-init reported done, but package installation did not complete\n'
            f'  (apt/DNS failures do not fail cloud-init). Builder VM kept for inspection.\n'
            f'  Last cloud-init log lines:\n{log_tail}')
    if target == 'cp':
        v = ssh(oob_ip, priv_key,
                'kubeadm version -o short 2>/dev/null; containerd --version 2>/dev/null',
                timeout=15)
        for line in v.stdout.strip().splitlines():
            print(f'  {line}')
    print('  All required binaries present ✓')


def cleanup_image_state(oob_ip, priv_key):
    """
    Clean cloud-init state so it re-runs on deployment (for hostname/network/SSH key).
    Also remove apt cache, SSH host keys, machine-id, hostname, netplan configs.
    """
    script = (
        'set -e; '
        'sudo cloud-init clean --logs --machine-id; '
        'sudo apt-get clean; '
        'sudo rm -rf /var/lib/apt/lists/*; '
        'sudo rm -f /etc/ssh/ssh_host_*; '
        'sudo truncate -s 0 /etc/machine-id; '
        'sudo rm -f /etc/hostname; '
        'sudo rm -f /etc/netplan/*.yaml /etc/netplan/*.yml 2>/dev/null || true; '
        'sudo shutdown -h now'
    )
    print('  Cleaning cloud-init state, apt cache, SSH host keys...')
    # SSH may disconnect when shutdown runs — that's expected
    try:
        ssh(oob_ip, priv_key, script, timeout=60, capture=False)
    except Exception:
        pass   # disconnect on shutdown is normal

    # Wait for VM to shut off
    print('  Waiting for builder VM shutdown', end='', flush=True)
    for _ in range(60):
        r = virsh('domstate', BUILDER_VM)
        if 'shut off' in r.stdout:
            print(' done')
            return
        time.sleep(3)
        print('.', end='', flush=True)
    print(' (forcing off)')
    virsh('destroy', BUILDER_VM)
    time.sleep(2)


def export_golden_image(src_disk, output_path):
    """Convert qcow2 overlay to standalone compressed golden image."""
    print(f'  Converting to standalone golden image: {output_path}')
    print('  (compressing — takes 1-3 min...)')
    check(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', '-c',
           src_disk, output_path])
    size_mb = Path(output_path).stat().st_size // (1024 * 1024)
    print(f'  Done. Size: {size_mb} MB')


def destroy_builder():
    """Remove builder VM, disk, seed ISO."""
    r = virsh('dominfo', BUILDER_VM)
    if r.returncode == 0:
        virsh('destroy', BUILDER_VM)
        time.sleep(2)
        virsh('undefine', BUILDER_VM, '--remove-all-storage', '--nvram')
        print(f'  removed builder VM {BUILDER_VM}')
    for f in [BUILDER_DISK, BUILDER_SEED, BUILDER_NET_XML]:
        if Path(f).exists():
            Path(f).unlink()


def destroy_builder_network():
    if virsh('net-info', BUILDER_NET).returncode == 0:
        virsh('net-destroy', BUILDER_NET)
        virsh('net-undefine', BUILDER_NET)
        print(f'  removed builder network {BUILDER_NET}')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global BUILDER_DISK, BUILDER_SEED, BUILDER_NET_XML
    args   = parse_args()
    sim    = load_sim(args.sim_yaml)
    cp     = sim['control_plane']
    target = args.target   # 'cp' or 'mh'

    # Relocate builder scratch files if requested (default /tmp)
    work_dir = Path(args.work_dir).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    BUILDER_DISK    = str(work_dir / 'golden-builder.qcow2')
    BUILDER_SEED    = str(work_dir / 'golden-builder-seed.iso')
    BUILDER_NET_XML = str(work_dir / 'golden-builder-net.xml')

    gi     = sim.get('golden_image', {})
    gi_tgt = gi.get('control_plane' if target == 'cp' else 'managed_hosts', {})

    if not gi_tgt:
        section = 'golden_image.control_plane' if target == 'cp' else 'golden_image.managed_hosts'
        print(f'Error: {section} section missing from nico-sim.yaml', file=sys.stderr)
        sys.exit(1)

    img_url  = gi_tgt.get('os_image_url')
    img_name = gi_tgt.get('os_image_name')
    img_dir  = gi_tgt.get('os_image_dir')

    if not all([img_url, img_name, img_dir]):
        section = 'golden_image.control_plane' if target == 'cp' else 'golden_image.managed_hosts'
        print(f'Error: os_image_url/name/dir must be set in {section}', file=sys.stderr)
        sys.exit(1)

    default_name = 'cp-golden.qcow2' if target == 'cp' else 'mh-golden.qcow2'
    output = args.output or gi_tgt.get('path') or f'{img_dir}/{default_name}'

    print('DC Simulation — Golden Image Builder')
    print(f'  target       : {target} ({"control-plane k8s nodes" if target == "cp" else "managed-host VMs — minimal, no k8s"})')
    print(f'  base image   : {img_name}')
    if target == 'cp':
        gi_cp = gi_tgt
        k8s_ver = gi_cp.get('kubernetes', {}).get('version', '1.32')
        print(f'  k8s version  : v{k8s_ver}')
    print(f'  output       : {output}')

    import shutil as _shutil
    free_gb = _shutil.disk_usage(work_dir).free // 2**30
    print(f'  work dir     : {work_dir} ({free_gb} GB free)')
    if free_gb < 45:
        print(f'  ⚠ builder disk can grow to 40G — {work_dir} may run out of space.')
        print(f'    Consider: --work-dir /var/lib/libvirt/images (or another large filesystem)')

    ssh_pub, priv_key = resolve_ssh_keys(args.ssh_key)
    print(f'  SSH key      : {priv_key} (public key derived from it)')
    print()

    # Clean any leftover builder resources from a previous failed run
    destroy_builder()

    success = False
    try:
        # ── Step 1: base image ────────────────────────────────────────────────
        print('Step 1: Base image')
        base_img = f'{img_dir}/{img_name}'
        if not Path(base_img).exists():
            print(f'  Downloading {img_url}')
            print(f'  → {base_img}')
            check(['wget', '-O', base_img, img_url])
        size_mb = Path(base_img).stat().st_size // (1024 * 1024)
        print(f'  Present: {base_img} ({size_mb} MB)')

        # ── Step 2: builder network ───────────────────────────────────────────
        print('Step 2: Builder NAT network')
        create_builder_network()

        # ── Step 3: builder VM disk + seed ────────────────────────────────────
        print('Step 3: Builder VM disk and cloud-init seed')
        check(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
               '-b', base_img, BUILDER_DISK, '40G'])

        if target == 'mh':
            user_data = gen_mh_builder_user_data(sim, ssh_pub)
            meta_data = 'instance-id: mh-golden-builder\nlocal-hostname: mh-golden-builder\n'
        else:
            user_data = gen_builder_user_data(sim, ssh_pub)
            meta_data = 'instance-id: golden-builder\nlocal-hostname: golden-builder\n'

        ud_tmp = Path(tempfile.mktemp(suffix='-user-data'))
        md_tmp = Path(tempfile.mktemp(suffix='-meta-data'))
        ud_tmp.write_text(user_data)
        md_tmp.write_text(meta_data)
        check(['cloud-localds', BUILDER_SEED, str(ud_tmp), str(md_tmp)])
        ud_tmp.unlink(missing_ok=True)
        md_tmp.unlink(missing_ok=True)

        if not Path(BUILDER_SEED).exists():
            raise RuntimeError(f'cloud-localds failed to create {BUILDER_SEED}')
        print(f'  Builder disk : {BUILDER_DISK} (40G max, backed by base image)')
        print(f'  Seed ISO     : {BUILDER_SEED} (cloud-init user-data + SSH key)')

        # ── Step 4: start builder VM ──────────────────────────────────────────
        print('Step 4: Starting builder VM')
        check(['virt-install',
               '--name',        BUILDER_VM,
               '--memory',      '4096',
               '--vcpus',       '2',
               '--disk',        f'{BUILDER_DISK},format=qcow2,bus=virtio',
               '--disk',        f'{BUILDER_SEED},device=cdrom',
               '--network',     f'network={BUILDER_NET},model=virtio,mac={BUILDER_MAC}',
               '--os-variant',  'ubuntu24.04',
               '--noautoconsole',
               '--import'])
        print(f'  {BUILDER_VM} started (4 GB RAM, 2 vCPUs, network {BUILDER_NET})')
        print(f'  Watch the boot live: sudo virsh console {BUILDER_VM}  (Ctrl+] to exit)')

        # ── Step 5: wait for DHCP ─────────────────────────────────────────────
        print('Step 5: Network')
        oob_ip = wait_for_dhcp()

        # ── Step 6: wait for cloud-init ───────────────────────────────────────
        print('Step 6: Package installation (cloud-init)')
        wait_for_cloud_init(oob_ip, priv_key, target=target)

        # ── Step 7: verify installed binaries ─────────────────────────────────
        print('Step 7: Verify image contents')
        verify_image_contents(oob_ip, priv_key, target)

        # ── Step 8: clean image state ─────────────────────────────────────────
        print('Step 8: Image cleanup')
        cleanup_image_state(oob_ip, priv_key)

        # ── Step 9: export ────────────────────────────────────────────────────
        print('Step 9: Export golden image')
        export_golden_image(BUILDER_DISK, output)
        success = True

    finally:
        print()
        if success:
            print('Cleaning up builder resources...')
            destroy_builder()
            destroy_builder_network()
        else:
            # Keep the builder VM so the failure can be inspected — auto-cleanup
            # here used to destroy the only evidence. The next run cleans it up.
            print(f'Build FAILED — builder VM kept for debugging:')
            print(f'  console : sudo virsh console {BUILDER_VM}')
            print(f'  ssh     : ssh -i {priv_key} ubuntu@<builder-ip>   '
                  f'(IP: virsh net-dhcp-leases {BUILDER_NET})')
            print(f'  cleanup : automatic on next run, or manually:')
            print(f'            sudo virsh destroy {BUILDER_VM} && '
                  f'sudo virsh undefine {BUILDER_VM} --remove-all-storage --nvram')

    print()
    print('=' * 60)
    print('Golden image ready.')
    print(f'  Path: {output}')
    print()
    print('Next steps:')
    if target == 'mh':
        print(f'  Path already set in nico-sim.yaml: golden_image.managed_hosts.path')
        print(f'  Deploy the fabric first (generate-fabric.py + deploy.sh), then:')
        print(f'  1. python3 generate-nodes.py nico-sim.yaml \\')
        print(f'       --topo ./fabric/topo.clab.yml --output-dir ./vm')
        print(f'  2. sudo ./node-fabric/deploy-nodes.sh')
        print()
        print('MH VMs (mh-rack-leaf-4-1/2, mh-rack-leaf-5-1/2) will boot in ~30s.')
        print('Packages pre-installed: python3, ssh, iproute2, iputils-ping, dnsmasq.')
        print('No k8s — these are tenant workload hosts, ingested by Nico.')
    else:
        print(f'  1. Uncomment in nico-sim.yaml:')
        print(f'       golden_image: {output}')
        print(f'  2. python3 generate-nodes.py nico-sim.yaml \\')
        print(f'       --topo ./fabric/topo.clab.yml --output-dir ./vm')
        print(f'  3. sudo ./node-fabric/deploy-nodes.sh')
        print()
        print('CP host VMs will boot in ~30s (packages already installed).')
        print('kubeadm is pre-installed — cluster formation is just:')
        print('  kubeadm init / kubeadm join')
    print('=' * 60)


if __name__ == '__main__':
    main()
