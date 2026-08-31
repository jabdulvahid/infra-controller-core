#!/usr/bin/env python3
"""
nico-sim — Generate VM configs and deploy them.

Wrapper that runs generate-nodes.py then sudo ./deploy-nodes.sh.
Keep generate-nodes.py and deploy-nodes.sh separate for debugging.

Usage:
  ./deploy-vms.py ~/sites/ytl
"""

import subprocess
import sys
from pathlib import Path


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac')]
        if not yamls:
            print(f'Error: no site yaml found in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}: {[f.name for f in yamls]}', file=sys.stderr); sys.exit(1)
        return str(yamls[0].resolve()), str((p / "fabric").resolve()), str((p / "vm").resolve())
    return str(p.resolve()), str((p.parent / "fabric").resolve()), str((p.parent / "vm").resolve())


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0 if sys.argv[1:] == ['--help'] else 1)

    site = sys.argv[1]
    _, fabric_dir, vm_dir = resolve_site(site)
    fabric_dir = str(Path(fabric_dir).resolve())
    vm_dir     = str(Path(vm_dir).resolve())
    script_dir = Path(__file__).parent

    # Step 1: generate VM configs
    print('=== Generating VM configs ===')
    r = subprocess.run([sys.executable, str(script_dir / 'generate-nodes.py'), site])
    if r.returncode != 0:
        print('generate-nodes.py failed — aborting.', file=sys.stderr)
        sys.exit(r.returncode)

    # Step 2: print deploy command — run directly in terminal for full output.
    # deploy-nodes.sh has long wait loops (DHCP, BGP) that don't work reliably
    # when wrapped in a Python subprocess. Run it directly so you see all output.
    deploy_sh = Path(vm_dir) / 'deploy-nodes.sh'
    if not deploy_sh.exists():
        print(f'Error: {deploy_sh} not found.', file=sys.stderr)
        sys.exit(1)

    print()
    print('=== VM configs generated. Deploy with: ===')
    print(f'  sudo {deploy_sh}')
    print()
    print('Monitor progress in another terminal:')
    print(f'  {Path(__file__).parent}/check-vms.py {site} --watch')


if __name__ == '__main__':
    main()
