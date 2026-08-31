#!/usr/bin/env python3
"""
DC Simulation — Configure insecure registry trust

Configures both:
  1. containerd on all CP host VMs  — so k8s can pull from the local registry
  2. Docker daemon on sim-host      — so build-nico-components.py can push

Run after deploy-nodes.sh once the utility VM is up and cloud-init is done.
Safe to re-run: checks if already configured before applying.

Usage:
  python3 setup-insecure-registry.py nico-sim.yaml
  python3 setup-insecure-registry.py nico-sim.yaml --cp-only      # skip local Docker
  python3 setup-insecure-registry.py nico-sim.yaml --docker-only  # skip CP VMs
"""

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=/dev/null',
    '-o', 'LogLevel=ERROR',
    '-o', 'ConnectTimeout=10',
    '-o', 'BatchMode=yes',
]


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Configure insecure registry trust on CP VMs and sim-host',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--cp-only',     action='store_true',
                   help='Only configure CP VMs containerd (skip local Docker)')
    p.add_argument('--docker-only', action='store_true',
                   help='Only configure local Docker daemon (skip CP VMs)')
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0])
    return str(p)


def registry_address(sim):
    """Return registry VM address (fabric-ip:port) from nico-sim.yaml."""
    reg_prefix = sim['fabric'].get('registry_link',
                    sim['fabric'].get('utility_link', {})).get('prefix', '7.132.0.4/30')
    net    = ipaddress.IPv4Network(reg_prefix, strict=False)
    reg_ip = str(list(net.hosts())[1])   # .2/.6 = registry VM
    port   = sim.get('nico_container_registry',
                sim.get('utility', {})).get('port', 5000)
    return f'{reg_ip}:{port}'


def find_priv_key():
    import sim_ssh
    return sim_ssh.find_priv_key()


def get_cp_oob_ips(sim):
    """Get OOB IPs for CP host VMs from virsh DHCP leases (qemu:///system)."""
    cp       = sim['control_plane']
    n        = cp['num_vms']
    oob_net  = cp['oob_network']['name']
    pfx      = sim['fabric'].get('prefixes', {})
    so       = int(pfx.get('switch_underlay', '7.0.0.0/8').split('.')[0])
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    vp       = cp.get('vm_prefix', 'cp')

    target_macs = {f'52:54:{so:02x}:00:{i:02x}:01': f'{name_pfx}{vp}-{i}'
                   for i in range(1, n + 1)}

    r = subprocess.run(['virsh', '-c', 'qemu:///system', 'net-dhcp-leases', oob_net],
                       capture_output=True, text=True)
    results = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            mac = parts[2].lower()
            if mac in target_macs:
                ip = parts[4].split('/')[0]
                results[target_macs[mac]] = ip

    # Fallback: static OOB reservations from the site yaml. dnsmasq does not
    # always write static (dhcp-host) assignments to the lease file, so a VM
    # can be up and reachable while absent from net-dhcp-leases.
    vms_cfg = cp.get('vms', [])
    for i in range(1, n + 1):
        name = f'{name_pfx}{vp}-{i}'
        if name not in results and len(vms_cfg) >= i:
            static_ip = vms_cfg[i - 1].get('host_oob_ip')
            if static_ip:
                results[name] = static_ip
                print(f'  [{name}] not in DHCP leases — using static host_oob_ip {static_ip}')

    return results   # {'dc1-ytl-cp-1': '192.168.220.x', ...}


# ── CP VMs: configure containerd ──────────────────────────────────────────────

def configure_containerd(vm_name, oob_ip, registry, priv_key):
    """
    Configure containerd to pull from the local HTTP registry.

    Uses the containerd 2.x certs.d format: creates
    /etc/containerd/certs.d/<registry>/hosts.toml with an HTTP endpoint.
    The old registry.mirrors format in config.toml is silently ignored in
    containerd 2.x (confirmed on containerd 2.2.1).

    Idempotent: skips if hosts.toml already exists for this registry.
    """
    hosts_dir = f'/etc/containerd/certs.d/{registry}'
    hosts_toml = f'{hosts_dir}/hosts.toml'

    # Fix containerd config.toml:
    # 1. Remove old registry.mirrors format (silently ignored in 2.x but causes
    #    duplicate TOML table errors that make containerd ignore config_path).
    # 2. Fix config_path: containerd 2.x treats the colon-separated value
    #    '/etc/containerd/certs.d:/etc/docker/certs.d' as a single literal path
    #    (not a list), so it never finds certs.d. Replace with single path.
    cleanup_cmd = (
        'sudo sed -i '
        '"/\\[plugins\\.\\\"io\\.containerd\\.grpc\\.v1\\.cri\\\"\\.registry\\.mirrors\\./d" '
        '/etc/containerd/config.toml && '
        'sudo sed -i "/endpoint = /d" '
        '/etc/containerd/config.toml && '
        'sudo sed -i "s|:/etc/docker/certs.d||" /etc/containerd/config.toml'
    )
    subprocess.run(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', cleanup_cmd],
        capture_output=True,
    )

    # Ensure config_path is actually SET. The colon-strip above only fixes a
    # dual-path value; `containerd config default` on some versions (v2.3+)
    # emits config_path = '' — with an empty value containerd ignores certs.d
    # entirely and the hosts.toml below is dead config (pulls fail with
    # "server gave HTTP response to HTTPS client"). Handle both the v2
    # (single-quoted, io.containerd.cri.v1.images) and v1 (double-quoted,
    # io.containerd.grpc.v1.cri) config schemas, then VERIFY — a silent no-op
    # here is exactly how this bug class survives.
    ensure_cmd = (
        'sudo sed -i "/\\[plugins\\.\'io\\.containerd\\.cri\\.v1\\.images\'\\.registry\\]/'
        '{n;s|config_path = \'\'|config_path = \'/etc/containerd/certs.d\'|}" '
        '/etc/containerd/config.toml && '
        'sudo sed -i "/\\[plugins\\.\\\"io\\.containerd\\.grpc\\.v1\\.cri\\\"\\.registry\\]/'
        '{n;s|config_path = \\\"\\\"|config_path = \\\"/etc/containerd/certs.d\\\"|}" '
        '/etc/containerd/config.toml && '
        'grep -Eq "^[[:space:]]*config_path[[:space:]]*=[[:space:]]*'
        '[\'\\\"]/etc/containerd/certs.d[\'\\\"]" /etc/containerd/config.toml'
    )
    r = subprocess.run(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', ensure_cmd],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f'  [{vm_name}] ✗ config_path not set in containerd config after fix attempts')
        print(f'    Inspect: ssh ubuntu@{oob_ip} "grep -n -A2 registry /etc/containerd/config.toml"')
        return False

    check_cmd = f'test -f {hosts_toml}'
    r = subprocess.run(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', check_cmd],
        capture_output=True,
    )
    if r.returncode == 0:
        print(f'  [{vm_name}] containerd already trusts {registry} (certs.d) — skipping')
        # Still restart to pick up any config.toml cleanup done above
        subprocess.run(
            ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}',
             'sudo systemctl restart containerd'],
            capture_output=True,
        )
        return True

    # Write hosts.toml using the containerd 2.x registry configuration format.
    # server = the base URL used for auth/API calls (HTTP for insecure local registry).
    # host entry grants pull + resolve capabilities without TLS.
    setup_cmd = (
        f'sudo mkdir -p {hosts_dir} && '
        f'printf "server = \\"http://{registry}\\"\\n\\n'
        f'[host.\\"http://{registry}\\"]\\n'
        f'  capabilities = [\\"pull\\", \\"resolve\\"]\\n" '
        f'| sudo tee {hosts_toml} > /dev/null && '
        f'sudo systemctl restart containerd && '
        f'echo "containerd configured and restarted"'
    )
    r = subprocess.run(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', setup_cmd],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f'  [{vm_name}] ✓ containerd configured for {registry} (certs.d)')
        return True
    print(f'  [{vm_name}] ✗ failed: {r.stderr.strip() or r.stdout.strip()}')
    return False


# ── sim-host: configure Docker daemon ─────────────────────────────────────────

def configure_docker_daemon(registry):
    """
    Add registry to /etc/docker/daemon.json insecure-registries.
    Requires root — will attempt sudo if not already root.
    Idempotent: skips if already configured.
    """
    daemon_cfg = Path('/etc/docker/daemon.json')

    # Read existing config
    try:
        cfg = json.loads(daemon_cfg.read_text()) if daemon_cfg.exists() else {}
    except (json.JSONDecodeError, PermissionError):
        cfg = {}

    insecure = cfg.get('insecure-registries', [])
    if registry in insecure:
        print(f'  [sim-host] Docker already trusts {registry} — skipping')
        return

    insecure.append(registry)
    cfg['insecure-registries'] = insecure
    new_content = json.dumps(cfg, indent=2) + '\n'

    # Write requires root
    if os.geteuid() == 0:
        daemon_cfg.write_text(new_content)
        subprocess.run(['systemctl', 'reload', 'docker'], check=True)
        print(f'  [sim-host] ✓ Docker configured for {registry}')
    else:
        # Write via sudo
        r = subprocess.run(
            ['sudo', 'bash', '-c',
             f'cat > /etc/docker/daemon.json << \'ENDJSON\'\n{new_content}ENDJSON\n'
             f'systemctl reload docker'],
            capture_output=False,
        )
        if r.returncode == 0:
            print(f'  [sim-host] ✓ Docker configured for {registry}')
        else:
            print(f'  [sim-host] ✗ failed — run manually:')
            print(f'    sudo bash -c \'echo "{json.dumps(cfg)}" '
                  f'> /etc/docker/daemon.json && systemctl reload docker\'')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args     = parse_args()
    sim      = load_sim(resolve_site(args.site))
    registry = registry_address(sim)

    print('DC Simulation — Configure Insecure Registry Trust')
    print(f'  registry : {registry}')
    print()

    if not args.cp_only:
        print('Step 1: Configure Docker daemon on sim-host')
        configure_docker_daemon(registry)
        print()

    cp_ips   = {}
    priv_key = None

    if not args.docker_only:
        print('Step 2: Configure containerd on CP host VMs')
        try:
            priv_key = find_priv_key()
        except RuntimeError as e:
            print(f'  Error: {e}')
            sys.exit(1)

        print('  Looking up CP VM OOB IPs from virsh DHCP leases...')
        cp_ips = get_cp_oob_ips(sim)
        if not cp_ips:
            print('  No CP VMs found in DHCP leases — are the VMs running?')
            sys.exit(1)

        failed = []
        for vm_name in sorted(cp_ips):
            oob_ip = cp_ips[vm_name]
            print(f'  [{vm_name}] OOB IP: {oob_ip}')
            if not configure_containerd(vm_name, oob_ip, registry, priv_key):
                failed.append(vm_name)
        print()
        if failed:
            print(f'Error: containerd configuration failed on: {", ".join(failed)}',
                  file=sys.stderr)
            sys.exit(1)

    print('Step 3: Verify registry reachability')
    verify_registry(registry, cp_ips, priv_key)


def verify_registry(registry, cp_ips, priv_key):
    """Verify registry is reachable from sim-host and each CP VM."""
    import urllib.request, urllib.error

    # sim-host
    url = f'http://{registry}/v2/_catalog'
    hostname = socket.gethostname()
    print(f'  [{hostname}] curl {url}')
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(f'  [{hostname}] ✓ {resp.read().decode().strip()}')
    except Exception as e:
        print(f'  [{hostname}] ✗ {e}')

    # Each CP VM: HTTP reachability AND the actual containerd pull path.
    # curl proves the network; only crictl proves containerd honors certs.d
    # (ctr does NOT — it bypasses the CRI plugin). Pulling a nonexistent tag
    # is deliberate: a registry "not found" means the HTTP plumbing works;
    # an "HTTPS client" error means config_path/hosts.toml is broken.
    for vm_name in sorted(cp_ips):
        oob_ip = cp_ips[vm_name]
        r = subprocess.run(
            ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}',
             f'curl -sf http://{registry}/v2/_catalog'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print(f'  [{vm_name}]  ✓ HTTP {r.stdout.strip()}')
        else:
            print(f'  [{vm_name}]  ✗ registry unreachable — check registry VM and fabric')
            continue

        probe = (f'command -v crictl >/dev/null 2>&1 && '
                 f'sudo crictl pull {registry}/nico-sim-plumbing-probe:none 2>&1 || true')
        r = subprocess.run(
            ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', probe],
            capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout + r.stderr).lower()
        if 'https client' in out or 'http: server gave' in out:
            print(f'  [{vm_name}]  ✗ containerd pull path BROKEN (HTTPS fallback) — '
                  f'config_path/hosts.toml not honored')
        elif 'not found' in out or 'unknown' in out or 'notfound' in out:
            print(f'  [{vm_name}]  ✓ containerd pull path OK (registry answered over HTTP)')
        elif not out.strip():
            print(f'  [{vm_name}]  - crictl not available — pull path untested')
        else:
            print(f'  [{vm_name}]  ? unexpected crictl output: {out.strip()[:100]}')

    print()


if __name__ == '__main__':
    main()
