#!/usr/bin/env python3
"""
nico-sim — Generate fabric topology and deploy it.

Wrapper that runs generate-fabric.py then sudo ./deploy.sh.
Keep generate-fabric.py and deploy.sh separate for debugging.

Usage:
  ./deploy-fabric.py <site>           # deploy fabric for site
  ./deploy-fabric.py <site> --force   # overwrite existing deployment (WARNING)
"""

import subprocess
import sys
from pathlib import Path


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml found in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}: {[f.name for f in yamls]}', file=sys.stderr); sys.exit(1)
        return str(yamls[0].resolve()), str((p / 'fabric').resolve()), str((p / 'vm').resolve())
    return str(p.resolve()), str((p.parent / 'fabric').resolve()), str((p.parent / 'vm').resolve())


def check_not_running(dc_name):
    """Abort if ContainerLab lab is already deployed. Skipped under --force."""
    r = subprocess.run(
        ['sudo', 'clab', 'inspect', '-n', dc_name],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f'ERROR: ContainerLab lab "{dc_name}" is already running.')
        print('  The fabric is deployed. To tear everything down:')
        print('    sudo python3 destroy-site.py <site>')
        print('  To force overwrite (WARNING: may disrupt running VMs):')
        print('    ./deploy-fabric.py <site> --force')
        sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0)

    force = '--force' in args
    site  = next(a for a in args if not a.startswith('-'))

    site_yaml, fabric_dir, _ = resolve_site(site)
    fabric_dir = str(Path(fabric_dir).resolve())
    script_dir = Path(__file__).parent

    if not force:
        try:
            import yaml
            with open(site_yaml) as f:
                sim = yaml.safe_load(f)
            dc_name = sim.get('fabric', {}).get('dc_name', 'nico-sim')
            check_not_running(dc_name)
        except ImportError:
            pass  # pyyaml not available — deploy.sh will do its own check

    # Step 1: generate fabric
    print('=== Generating fabric topology ===')
    r = subprocess.run([sys.executable, str(script_dir / 'generate-fabric.py'), site])
    if r.returncode != 0:
        print('generate-fabric.py failed — aborting.', file=sys.stderr)
        sys.exit(r.returncode)

    # Step 2: deploy (requires root)
    deploy_sh = Path(fabric_dir) / 'deploy.sh'
    if not deploy_sh.exists():
        print(f'Error: {deploy_sh} not found.', file=sys.stderr)
        sys.exit(1)

    deploy_args = ['sudo', str(deploy_sh)]
    if force:
        deploy_args.append('--force')

    print('\n=== Deploying fabric ===')
    r = subprocess.run(deploy_args, cwd=fabric_dir)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
