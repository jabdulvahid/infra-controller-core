#!/usr/bin/env python3
"""
nico-dev — Redeploy Nico to a new image tag.

Does a helm upgrade of the nico release only — no values regeneration,
no prereqs touched. Use after build-dev-nico.py pushes a new tag.

Reads site yaml for kubeconfig and helm dir. Runs on Mac.

Usage:
  python3 redeploy-dev-nico.py <site> --tag <tag>
  python3 redeploy-dev-nico.py ~/Mac/sites/dev --tag test1
  python3 redeploy-dev-nico.py ~/Mac/sites/dev --tag latest
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: pip install pyyaml')
    sys.exit(1)


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}: {[f.name for f in yamls]}',
                  file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def main():
    p = argparse.ArgumentParser(
        description='Redeploy Nico to a new image tag (helm upgrade only)'
    )
    p.add_argument('site', help='Site folder or site yaml path')
    p.add_argument('--tag', required=True,
                   help='Image tag to deploy (must already be in the registry)')
    p.add_argument('--force', action='store_true',
                   help='Proceed even if this tag is already the deployed one '
                        '(only useful with imagePullPolicy Always)')
    args = p.parse_args()

    site_yaml, site_folder = resolve_site(args.site)
    cfg = yaml.safe_load(open(site_yaml))
    dc  = cfg['fabric']['dc_name']

    # Resolve nico repo path (nico_repo_folder inside whichever share root is accessible)
    repo_folder = cfg.get('nico_repo_folder', 'infra-controller-core')
    nico_mac = cfg.get('nico_mac_folder', '')
    nico_vm  = cfg.get('nico_vm_folder', '')
    if nico_mac and Path(nico_mac).expanduser().exists():
        repo = Path(nico_mac).expanduser() / repo_folder
    elif nico_vm and Path(nico_vm).expanduser().exists():
        repo = Path(nico_vm).expanduser() / repo_folder
    else:
        print('Error: neither nico_mac_folder nor nico_vm_folder is accessible.',
              file=sys.stderr)
        print('  Check nico_mac_folder / nico_vm_folder in site yaml.', file=sys.stderr)
        sys.exit(1)

    helm_dir      = str(repo / 'helm')
    sitename      = cfg.get('nico-system', {}).get('helm-values', {}).get('sitename', 'dev')
    kube_filename = cfg.get('kubeconfig', f'{dc}-{sitename}.kubeconfig.yaml')
    kubeconfig    = str(Path(site_folder) / kube_filename)

    if not Path(helm_dir).exists():
        print(f'Error: helm dir not found: {helm_dir}', file=sys.stderr)
        sys.exit(1)
    if not Path(kubeconfig).exists():
        print(f'Error: kubeconfig not found: {kubeconfig}', file=sys.stderr)
        sys.exit(1)

    env = {**os.environ, 'KUBECONFIG': kubeconfig}

    print('nico-dev — Redeploy Nico')
    print(f'  tag        : {args.tag}')
    print(f'  kubeconfig : {kubeconfig}')
    print(f'  helm dir   : {helm_dir}')

    # Same tag as deployed = no pod-template change = NO rollout, and the
    # node keeps its cached image (imagePullPolicy IfNotPresent) — a rebuild
    # under the same tag silently changes nothing on the cluster
    # (20260903-#1). Refuse unless told otherwise.
    r = subprocess.run(['helm', 'get', 'values', 'nico', '-n', 'nico-system',
                        '-o', 'json'], env=env, capture_output=True, text=True)
    deployed = ''
    if r.returncode == 0:
        try:
            import json
            deployed = json.loads(r.stdout).get('global', {}).get('image', {}).get('tag', '')
        except ValueError:
            pass
    print(f'  deployed   : {deployed or "(unknown)"}')
    if deployed == args.tag and not args.force:
        print(f'\nError: tag {args.tag} is already the deployed tag — helm sees no '
              f'change, nothing rolls out, and the node would reuse its cached\n'
              f'  image even if you rebuilt it. Rebuild under a NEW tag '
              f'(e.g. --tag {args.tag}-2) and redeploy that, or pass --force.',
              file=sys.stderr)
        sys.exit(1)

    r = subprocess.run([
        'helm', 'upgrade', 'nico', helm_dir,
        '-n', 'nico-system',
        '--reuse-values',
        '--set', f'global.image.tag={args.tag}',
        '--wait', '--timeout', '10m',
    ], env=env)

    if r.returncode != 0:
        print('Error: helm upgrade failed', file=sys.stderr)
        sys.exit(1)

    print(f'\n  Nico redeployed with tag {args.tag} ✓')

    # helm re-renders nico-api-config-files, silently dropping the
    # allow_insecure_discovery patch deploy-dev-nico.py applied — without
    # it, MAT machine discovery fails PermissionDenied ('source IP and
    # selected interface do not identify one host'): 20260825-#4.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'deploy_dev_nico', Path(__file__).parent / 'deploy-dev-nico.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.patch_allow_insecure_discovery('nico-system', kubeconfig)

    print()
    subprocess.run(
        ['kubectl', 'get', 'pods', '-n', 'nico-system', '--no-headers', '-o', 'wide'],
        env=env,
    )


if __name__ == '__main__':
    main()
