#!/usr/bin/env python3
"""
nico-sim — Generate VM configs and deploy them.

Generates node configs via generate-nodes.py then hands off directly to
deploy-nodes.sh via os.execv — no subprocess wrapping, so DHCP/BGP wait
loops and signal handling work exactly as if you ran the script manually.

Monitor boot progress in a second terminal:
  python3 check-vms.py <site> --watch

Usage:
  sudo python3 deploy-nodes.py <site>
"""

import os
import sys
import subprocess
from pathlib import Path


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml found in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0].resolve()), str(p.resolve())
    return str(p.resolve()), str(p.parent.resolve())


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0 if sys.argv[1:] == ['--help'] else 1)

    site              = sys.argv[1]
    site_yaml, _      = resolve_site(site)
    script_dir        = Path(__file__).parent

    # Step 1: generate VM configs
    print('=== Generating VM configs ===')
    r = subprocess.run([sys.executable, str(script_dir / 'generate-nodes.py'), site])
    if r.returncode != 0:
        print('generate-nodes.py failed — aborting.', file=sys.stderr)
        sys.exit(r.returncode)

    # Step 2: hand off to deploy-nodes.sh via execv — replaces this process
    # entirely so DHCP/BGP wait loops and signals work as if run directly.
    site_folder = Path(site_yaml).parent
    deploy_sh   = site_folder / 'vm' / 'deploy-nodes.sh'
    if not deploy_sh.exists():
        print(f'Error: {deploy_sh} not found after generate-nodes.py', file=sys.stderr)
        sys.exit(1)

    # Make vm/ dir readable by non-root (check-vms.py, nsim)
    subprocess.run(['chmod', '-R', 'a+rX', str(site_folder / 'vm')], capture_output=True)

    print(f'\n=== Deploying nodes (handing off to {deploy_sh.name}) ===')
    print(f'Monitor in another terminal: python3 check-vms.py {site} --watch\n')
    os.execv('/bin/bash', ['bash', str(deploy_sh)])


if __name__ == '__main__':
    main()
