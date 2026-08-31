#!/usr/bin/env python3
"""
nico-sim — Destroy all KVM VMs for a site.

Wrapper around cleanup-vms.py that accepts a site folder.
Destroys CP VMs, DPU stand-ins, MH VMs, registry VM, and their
OOB libvirt networks and disk images.

Does NOT touch the ContainerLab fabric (use destroy-fabric.py for that).

Usage:
  ./destroy-vms.py ~/sites/ytl              # interactive confirmation
  ./destroy-vms.py ~/sites/ytl --force      # no prompts
  ./destroy-vms.py ~/sites/ytl --dry-run    # show what would be removed
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
        return str(yamls[0]), str(p / 'fabric'), str(p / 'vm')
    return str(p), str(p.parent / 'fabric'), str(p.parent / 'vm')


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0 if sys.argv[1:] == ['--help'] else 1)

    site      = sys.argv[1]
    extra     = sys.argv[2:]   # pass through --force, --dry-run, etc.
    sim_yaml, _, _ = resolve_site(site)
    script_dir = Path(__file__).parent

    print(f'=== Destroying VMs for site: {Path(sim_yaml).stem} ===')
    print(f'    yaml: {sim_yaml}')
    if '--dry-run' in extra:
        print('    mode: dry-run')
    elif '--force' in extra:
        print('    mode: force (no prompts)')
    print()

    r = subprocess.run(
        [sys.executable, str(script_dir / 'cleanup-vms.py'), sim_yaml] + extra
    )
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
