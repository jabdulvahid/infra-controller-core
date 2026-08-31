#!/usr/bin/env python3
"""
nico-dev — Install Kubernetes (kubeadm) on the VM host and configure MetalLB BGP.

The VM host IS the CP node. kubeadm runs directly here — no nested VMs.

Steps:
  1. Install kubeadm/kubelet/kubectl (containerd already present via Docker)
  2. Configure containerd (SystemdCgroup, insecure registry mirror)
  3. System prerequisites (swap off, kernel modules, sysctl)
  4. kubeadm init
  5. Install flannel CNI
  6. Remove control-plane taint (single-node)
  7. Wait for node Ready
  8. Install MetalLB
  9. Configure MetalLB BGP peer → DPU stand-in container

Must run as root (sudo).

Usage:
  sudo python3 deploy-dev-cp.py <site>
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml')
                 if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def run(cmd, check=True, capture=False, env=None):
    r = subprocess.run(cmd, capture_output=capture, text=True,
                       env=env or os.environ.copy())
    if check and r.returncode != 0:
        err = (r.stderr or '').strip()
        print(f'  ! {" ".join(str(c) for c in cmd)} failed: {err}', file=sys.stderr)
        sys.exit(1)
    return r


def kubectl(args, kubeconfig, capture=True, stdin=None):
    env = {**os.environ, 'KUBECONFIG': kubeconfig}
    r = subprocess.run(['kubectl'] + args, capture_output=capture, text=True,
                       env=env, input=stdin)
    return r


def helm(args, kubeconfig):
    env = {**os.environ, 'KUBECONFIG': kubeconfig}
    return run(['helm'] + args, env=env)


def wait_for_node(kubeconfig, timeout=180, interval=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl(['get', 'nodes', '--no-headers'], kubeconfig)
        if r.returncode == 0 and r.stdout.strip():
            return True
        time.sleep(interval)
        print('.', end='', flush=True)
    print()
    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if os.geteuid() != 0:
        print('Error: must run as root (sudo)', file=sys.stderr)
        sys.exit(1)

    site_yaml, site_folder = resolve_site(sys.argv[1])
    cfg = yaml.safe_load(open(site_yaml))
    dc  = cfg['fabric']['dc_name']
    pfx = cfg['fabric']['prefixes']

    k8s_cfg  = cfg.get('kubernetes', cfg.get('k3s', {}))
    # Accept "1.32" or "v1.32.0+k3s1" — extract major.minor for apt repo
    raw_ver  = str(k8s_cfg.get('version', '1.32')).lstrip('v').split('+')[0]
    k8s_ver  = '.'.join(raw_ver.split('.')[:2])   # "1.32"
    pod_cidr = k8s_cfg.get('pod_cidr', '10.244.0.0/16')

    fabric_dir = Path(site_folder) / 'fabric'
    ips = json.loads((fabric_dir / 'fabric-ips.json').read_text())
    vm_cp_ip  = ips['vm_cp_ip']   # 7.132.1.1 — kubelet node IP
    dpu_cp_ip = ips['dpu_cp_ip']  # 7.132.1.0 — MetalLB BGP peer

    svc_vips = pfx.get('service_vips', '7.133.1.0/27')
    mb_asn   = cfg['fabric']['metallb_asn']
    dpu_asn  = cfg['fabric']['dpu_asn']

    registry = cfg.get('registry', {})
    reg_host = registry.get('host', '192.168.64.1')
    reg_port = registry.get('port', 5000)

    sitename      = cfg.get('nico-system', {}).get('helm-values', {}).get('sitename', 'dev')
    kube_filename = cfg.get('kubeconfig', f'{dc}-{sitename}.kubeconfig.yaml')
    kubeconfig    = str(Path(site_folder) / kube_filename)

    chart_versions = cfg.get('chart-versions', {})
    mb_ver = chart_versions.get('metallb', '0.14.5')

    admin_conf = '/etc/kubernetes/admin.conf'

    print('nico-dev — Deploy CP (kubeadm + MetalLB)')
    print(f'  site     : {site_yaml}')
    print(f'  node IP  : {vm_cp_ip}  (VM host = CP-1)')
    print(f'  DPU peer : {dpu_cp_ip}  (MetalLB BGP)')
    print(f'  k8s ver  : {k8s_ver}')
    print(f'  registry : {reg_host}:{reg_port}')

    # ── Step 1: Install kubeadm/kubelet/kubectl ───────────────────────────────
    # containerd is already present via Docker (containerd.io package)
    print('\nStep 1: Installing kubeadm/kubelet/kubectl...')
    keyring = Path('/etc/apt/keyrings/kubernetes-apt-keyring.gpg')
    if not keyring.exists():
        keyring.parent.mkdir(parents=True, exist_ok=True)
        key_url = f'https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/Release.key'
        r = subprocess.run(['curl', '-fsSL', key_url], capture_output=True)
        if r.returncode != 0:
            print('  Error: failed to fetch k8s apt key', file=sys.stderr); sys.exit(1)
        gpg = subprocess.run(['gpg', '--dearmor', '-o', str(keyring)],
                             input=r.stdout, capture_output=True)
        if gpg.returncode != 0:
            print('  Error: gpg dearmor failed', file=sys.stderr); sys.exit(1)
        print('  added apt keyring')

    sources = Path('/etc/apt/sources.list.d/kubernetes.list')
    repo_line = (f'deb [signed-by={keyring}] '
                 f'https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/ /\n')
    if not sources.exists() or sources.read_text() != repo_line:
        sources.write_text(repo_line)
        run(['apt-get', 'update', '-q'])
        print('  apt sources updated')

    if subprocess.run(['which', 'kubeadm'], capture_output=True).returncode != 0:
        run(['apt-get', 'install', '-y', '-q', 'kubeadm', 'kubelet', 'kubectl'])
        run(['apt-mark', 'hold', 'kubelet', 'kubeadm', 'kubectl'])
        print('  kubeadm/kubelet/kubectl installed ✓')
    else:
        print('  kubeadm already installed — skipping')

    # ── Step 2: Configure containerd ──────────────────────────────────────────
    print('\nStep 2: Configuring containerd...')
    config_toml = Path('/etc/containerd/config.toml')
    config_toml.parent.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(['containerd', 'config', 'default'],
                       capture_output=True, text=True)
    cfg_text = r.stdout

    # Enable SystemdCgroup (required for k8s with systemd cgroup driver)
    cfg_text = cfg_text.replace('SystemdCgroup = false', 'SystemdCgroup = true')

    # Point registry config_path at certs.d so per-registry hosts.toml is honored.
    # Handles both containerd v1 ([plugins."io.containerd.grpc.v1.cri".registry])
    # and v2 ([plugins.'io.containerd.cri.v1.images'.registry]) section names and
    # quoting; v2 emits an explicit `config_path = ''` line, v1 may omit it.
    # A silent non-match here is exactly the bug that caused the
    # HTTP-response-to-HTTPS-client pull failures — so count matches and fail
    # loudly if the config shape is unrecognized.
    certs_d = '/etc/containerd/certs.d'
    reg_section = re.compile(
        r"(\[plugins\.['\"](?:io\.containerd\.cri\.v1\.images|io\.containerd\.grpc\.v1\.cri)['\"]\.registry\]"
        r"[ \t]*\n(?:[ \t]*\n)*[ \t]*config_path[ \t]*=[ \t]*)(''|\"\")")
    cfg_text, n = reg_section.subn(rf"\g<1>'{certs_d}'", cfg_text)
    if n == 0:
        # v1-style config without an explicit config_path line: insert one
        header = re.compile(
            r"(\[plugins\.['\"](?:io\.containerd\.cri\.v1\.images|io\.containerd\.grpc\.v1\.cri)['\"]\.registry\][ \t]*\n)")
        cfg_text, n = header.subn(rf"\g<1>      config_path = '{certs_d}'\n", cfg_text)
    if n == 0:
        print('  Error: could not find the CRI registry section in '
              '`containerd config default` output.\n'
              '  containerd version may have changed its config schema — inspect:\n'
              '    containerd config default | grep -n -A2 registry', file=sys.stderr)
        sys.exit(1)
    config_toml.write_text(cfg_text)

    # Verify what actually landed on disk — a wrong config here surfaces later
    # as a confusing image-pull failure, so make it fail HERE instead.
    written = config_toml.read_text()
    if not re.search(rf"(?<!\w)config_path\s*=\s*['\"]{re.escape(certs_d)}['\"]", written):
        print(f'  Error: config_path not set in {config_toml} after write',
              file=sys.stderr)
        sys.exit(1)
    print(f'  config_path → {certs_d} (verified) ✓')

    # Insecure registry mirror (Mac registry at reg_host:reg_port)
    reg_dir = Path(f'/etc/containerd/certs.d/{reg_host}:{reg_port}')
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / 'hosts.toml').write_text(
        f'server = "http://{reg_host}:{reg_port}"\n\n'
        f'[host."http://{reg_host}:{reg_port}"]\n'
        f'  capabilities = ["pull", "resolve"]\n'
        f'  skip_verify = true\n'
    )
    run(['systemctl', 'restart', 'containerd'])
    print('  containerd configured ✓')

    # ── Step 3: System prerequisites ──────────────────────────────────────────
    print('\nStep 3: System prerequisites...')
    run(['swapoff', '-a'])
    for mod in ['overlay', 'br_netfilter']:
        run(['modprobe', mod])
    Path('/etc/modules-load.d/k8s.conf').write_text('overlay\nbr_netfilter\n')
    Path('/etc/sysctl.d/k8s.conf').write_text(
        'net.bridge.bridge-nf-call-iptables  = 1\n'
        'net.bridge.bridge-nf-call-ip6tables = 1\n'
        'net.ipv4.ip_forward                 = 1\n'
        # kubelet follows container logs via inotify; Ubuntu's default of 128
        # instances exhausts once the full stack runs ("failed to create
        # fsnotify watcher: too many open files" on kubectl logs -f).
        # Generous ceilings: instances are ~1 KB of kernel memory when idle
        # and users keep adding charts — the limit is a DoS guard, not a
        # reservation, so headroom is free on a single-user dev VM.
        'fs.inotify.max_user_instances       = 65536\n'
        'fs.inotify.max_user_watches         = 1048576\n'
    )
    run(['sysctl', '--system', '-q'])

    # Set node IP so MetalLB BGP-peers from the CP link address (not UTM NAT)
    Path('/etc/default/kubelet').write_text(
        f'KUBELET_EXTRA_ARGS="--node-ip={vm_cp_ip}"\n'
    )
    run(['systemctl', 'daemon-reload'])
    # Disable swap permanently (survives reboot) — kubelet refuses to start with swap on
    run(['swapoff', '-a'])
    fstab = Path('/etc/fstab').read_text()
    if 'swap' in fstab:
        Path('/etc/fstab').write_text(
            '\n'.join(
                f'#{l}' if 'swap' in l and not l.startswith('#') else l
                for l in fstab.splitlines()
            ) + '\n'
        )
        print('  swap disabled in /etc/fstab ✓')
    print('  prerequisites ✓')

    # ── Step 4: kubeadm init ──────────────────────────────────────────────────
    print('\nStep 4: kubeadm init...')
    if Path(admin_conf).exists():
        print('  already initialized — skipping (run kubeadm reset to start over)')
    else:
        subprocess.run([
            'kubeadm', 'init',
            f'--pod-network-cidr={pod_cidr}',
        ], check=True)
        print('  kubeadm init ✓')

    # ── Step 5: Copy kubeconfig ───────────────────────────────────────────────
    print('\nStep 5: Kubeconfig...')
    raw_kube = Path(admin_conf).read_text()
    Path(kubeconfig).write_text(raw_kube)
    Path(kubeconfig).chmod(0o600)
    print(f'  kubeconfig → {kubeconfig}')

    # ── Step 6: Install flannel CNI ───────────────────────────────────────────
    print('\nStep 6: Installing flannel CNI...')
    flannel_url = ('https://github.com/flannel-io/flannel/releases/'
                   'latest/download/kube-flannel.yml')
    r = subprocess.run(['curl', '-fsSL', flannel_url], capture_output=True, text=True)
    if r.returncode != 0:
        print('  Error: failed to download flannel manifest', file=sys.stderr)
        sys.exit(1)
    flannel_manifest = r.stdout
    if pod_cidr != '10.244.0.0/16':
        flannel_manifest = flannel_manifest.replace('10.244.0.0/16', pod_cidr)
    p = subprocess.run(
        ['kubectl', '--kubeconfig', admin_conf, 'apply', '-f', '-'],
        input=flannel_manifest, capture_output=True, text=True
    )
    if p.returncode != 0:
        print(f'  Error: flannel apply failed: {p.stderr}', file=sys.stderr)
        sys.exit(1)
    print('  flannel CNI applied ✓')

    # ── Step 7: Remove control-plane taint (single-node) ─────────────────────
    print('\nStep 7: Removing control-plane taint...')
    r = subprocess.run(
        ['kubectl', '--kubeconfig', admin_conf,
         'taint', 'nodes', '--all',
         'node-role.kubernetes.io/control-plane-'],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print('  taint removed ✓')
    else:
        print('  taint already absent')

    # kubeadm >=1.24 labels CP nodes exclude-from-external-load-balancers,
    # which silently mutes MetalLB announcements from this node — the exact
    # bug that cost a day in nico-sim (2026-08-05). Strip it.
    r = subprocess.run(
        ['kubectl', '--kubeconfig', admin_conf,
         'label', 'nodes', '--all',
         'node.kubernetes.io/exclude-from-external-load-balancers-'],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print('  exclude-from-external-load-balancers label removed ✓')
    else:
        print('  exclude-from-external-load-balancers label already absent')

    # ── Step 8: Wait for node Ready ───────────────────────────────────────────
    print('\nStep 8: Waiting for node Ready', end='')
    if not wait_for_node(admin_conf, timeout=180):
        print('\nError: node not ready after 180s', file=sys.stderr)
        sys.exit(1)
    print('\n  node Ready ✓')

    # ── Step 9: Install MetalLB ───────────────────────────────────────────────
    print('\nStep 9: Installing MetalLB...')
    helm(['repo', 'add', 'metallb', 'https://metallb.github.io/metallb'], kubeconfig)
    helm(['repo', 'update'], kubeconfig)
    helm(['upgrade', '--install', 'metallb', 'metallb/metallb',
          '--version', mb_ver,
          '-n', 'metallb-system', '--create-namespace',
          '--wait', '--timeout', '10m'], kubeconfig)
    print('  MetalLB installed ✓')

    r = kubectl(['wait', '--for=condition=Established',
                 'crd/ipaddresspools.metallb.io', '--timeout=60s'], kubeconfig)
    if r.returncode != 0:
        print('  Error: MetalLB CRDs not ready', file=sys.stderr); sys.exit(1)

    # ── Step 10: Configure MetalLB BGP ───────────────────────────────────────
    print('\nStep 10: Configuring MetalLB BGP...')
    metallb_cfg = f'''\
---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: nico-vips
  namespace: metallb-system
spec:
  addresses:
    - {svc_vips}
  autoAssign: false
---
apiVersion: metallb.io/v1beta2
kind: BGPPeer
metadata:
  name: dpu-1
  namespace: metallb-system
spec:
  myASN: {mb_asn}
  peerASN: {dpu_asn}
  peerAddress: {dpu_cp_ip}
---
apiVersion: metallb.io/v1beta1
kind: BGPAdvertisement
metadata:
  name: nico-vips
  namespace: metallb-system
spec:
  ipAddressPools:
    - nico-vips
'''
    p = subprocess.run(
        ['kubectl', '--kubeconfig', kubeconfig, 'apply', '-f', '-'],
        input=metallb_cfg, capture_output=True, text=True
    )
    if p.returncode != 0:
        print(f'  MetalLB config failed: {p.stderr}', file=sys.stderr)
        sys.exit(1)
    print('  MetalLB BGP configured ✓')
    print(f'  VIP pool : {svc_vips}')
    print(f'  BGP peer : {dpu_cp_ip} AS={dpu_asn}')

    # ── Done ──────────────────────────────────────────────────────────────────
    # ── Set KUBECONFIG in user's .bashrc ─────────────────────────────────────
    real_user = os.environ.get('SUDO_USER', os.environ.get('USER', ''))
    if real_user:
        bashrc = Path(f'/home/{real_user}/.bashrc')
        export_line = f'export KUBECONFIG={kubeconfig}\n'
        if bashrc.exists() and kubeconfig not in bashrc.read_text():
            with bashrc.open('a') as f:
                f.write(f'\n# nico-dev\n{export_line}')
            print(f'  KUBECONFIG added to {bashrc} ✓')
        else:
            print(f'  KUBECONFIG already in {bashrc}')

    print(f'\n{"="*55}')
    print(f'  CP deployed ✓')
    print(f'  kubeconfig : {kubeconfig}')
    print(f'\n  Verify (VM):')
    print(f'    source ~/.bashrc')
    print(f'    kubectl get nodes')
    dev_folder = cfg.get('nico_dev_folder', 'claude-notes/nico-dev')
    mac_root   = cfg.get('nico_mac_folder', '<mac-share>')
    mac_dev    = f'{mac_root}/{dev_folder}'
    # Re-root the ACTUAL site folder from the VM share root onto the Mac share
    # root — never assume sites/<name> (the user chooses the folder layout).
    try:
        rel = Path(site_folder).resolve().relative_to(
            Path(cfg.get('nico_vm_folder', '')).expanduser().resolve())
        mac_site = f'{mac_root}/{rel}'
    except (ValueError, OSError):
        mac_site = f'{mac_root}/sites/{sitename}'
    print(f'\n  Next (Mac) — build images, verify registry, deploy Nico:')
    print(f'    python3 {mac_dev}/build-dev-nico-mac.py {mac_site} --tag <tag>')
    print(f'    python3 {mac_dev}/ndev.py {mac_site} registry verify')
    print(f'    python3 {mac_dev}/deploy-dev-nico.py {mac_site} --tag <tag>')
    print(f'{"="*55}')


if __name__ == '__main__':
    main()
