#!/usr/bin/env python3
"""
nico-dev — Destroy the dev environment (kubeadm cluster + fabric + routes).

Runs on the VM. Must run as root (sudo).

Usage:
  sudo python3 destroy-dev.py <site>
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed.'); sys.exit(1)


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


def run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True)


def ok(msg):  print(f'  ✓ {msg}')
def skip(msg): print(f'  - {msg} (not found)')


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    if os.geteuid() != 0:
        print('Error: must run as root (sudo)', file=sys.stderr); sys.exit(1)

    site_yaml, site_folder = resolve_site(sys.argv[1])
    cfg = yaml.safe_load(open(site_yaml))
    dc  = cfg['fabric']['dc_name']
    fabric_dir = Path(site_folder) / 'fabric'
    topo = fabric_dir / 'topo.clab.yml'

    print(f'nico-dev — Destroy {dc}')

    # Kubernetes (kubeadm)
    print('\n── Kubernetes ────────────────────────────────')
    if shutil.which('kubeadm') and Path('/etc/kubernetes/manifests').exists():
        subprocess.run(['kubeadm', 'reset', '-f'], capture_output=True)
        ok('kubeadm reset')
        run(['systemctl', 'stop', 'kubelet'])
        for d in ['/etc/cni/net.d', '/var/lib/etcd']:
            if Path(d).exists():
                shutil.rmtree(d, ignore_errors=True)
                ok(f'removed {d}')
        # CNI leaves its interfaces behind after reset
        for iface in ['cni0', 'flannel.1']:
            if run(['ip', 'link', 'show', iface]).returncode == 0:
                run(['ip', 'link', 'delete', iface])
                ok(f'interface {iface}')
    else:
        skip('kubeadm cluster')

    # Fabric boot service (deploy-dev-fabric installs it; without this the
    # fabric would redeploy itself on the next reboot)
    print('\n── Boot service ──────────────────────────────')
    svc = Path('/etc/systemd/system/nico-dev-fabric.service')
    if svc.exists():
        run(['systemctl', 'disable', '--now', 'nico-dev-fabric.service'])
        svc.unlink()
        run(['systemctl', 'daemon-reload'])
        ok('nico-dev-fabric.service disabled + removed')
    else:
        skip('nico-dev-fabric.service')
    env_file = Path('/etc/nico-dev/env')
    if env_file.exists():
        env_file.unlink()
        ok('/etc/nico-dev/env')

    # ContainerLab
    print('\n── ContainerLab ──────────────────────────────')
    if topo.exists():
        r = subprocess.run(['clab', 'destroy', '-t', str(topo), '--cleanup'],
                           capture_output=True)
        ok(f'clab destroy {topo.name}') if r.returncode == 0 else skip('clab destroy')
    r = subprocess.run(['clab', 'destroy', '-n', dc, '--cleanup'], capture_output=True)
    if r.returncode == 0:
        ok(f'clab destroy -n {dc}')

    # Bridges
    print('\n── Bridges ───────────────────────────────────')
    for br in [f'br-{dc}-cp', f'br-{dc}-internet']:
        if run(['ip', 'link', 'show', br]).returncode == 0:
            run(['ip', 'link', 'set', br, 'down'])
            run(['ip', 'link', 'delete', br, 'type', 'bridge'])
            ok(f'bridge {br}')
        else:
            skip(f'bridge {br}')

    # iptables rules
    print('\n── iptables ──────────────────────────────────')
    inet_br = f'br-{dc}-internet'
    for proto, port in [('udp', '53'), ('tcp', '53'), ('tcp', '443')]:
        rule = ['-i', inet_br, '-p', proto, '--dport', port, '-j', 'ACCEPT']
        if run(['iptables', '-C', 'INPUT'] + rule).returncode == 0:
            run(['iptables', '-D', 'INPUT'] + rule)
            ok(f'removed INPUT {proto}/{port}')

    # Site files (generated artifacts only — the site yaml stays)
    print('\n── Site files ────────────────────────────────')
    for path in ['dev-values', 'fabric']:
        p = Path(site_folder) / path
        if p.exists():
            shutil.rmtree(p)
            ok(f'removed {path}/')
    for kc in Path(site_folder).glob('*.kubeconfig.yaml'):
        kc.unlink()
        ok(f'removed {kc.name}')

    print(f'\n{"="*50}')
    print(f'  {dc} destroyed. Run deploy-dev-fabric.py to redeploy.')
    print(f'{"="*50}')


if __name__ == '__main__':
    main()
