#!/usr/bin/env python3
"""
nico-dev — Upgrade Nico helm release to a new image tag.

Run from inside infra-controller-core. Requires KUBECONFIG env var.

Usage:
  KUBECONFIG=~/.kube/nico-dev.yaml python3 upgrade-dev-nico.py --tag abc1234
  python3 upgrade-dev-nico.py --tag latest          # revert to golden image
  python3 upgrade-dev-nico.py                        # default: git SHA of cwd
"""

import argparse
import subprocess
import sys
from pathlib import Path


def git_sha():
    r = subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print('Error: not inside a git repo — pass --tag explicitly or run from infra-controller-core.',
              file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def main():
    p = argparse.ArgumentParser(
        description='Upgrade Nico on the dev cluster to a new image tag'
    )
    p.add_argument('--tag', default=None,
                   help='Image tag (default: git SHA of cwd). '
                        'Use --tag latest to revert to the golden image.')
    args = p.parse_args()

    import os
    kubeconfig = os.environ.get('KUBECONFIG')
    if not kubeconfig:
        print('Error: KUBECONFIG env var not set.', file=sys.stderr)
        print('  export KUBECONFIG=~/.kube/nico-dev.yaml', file=sys.stderr)
        sys.exit(1)

    helm_dir = str(Path.cwd() / 'helm')
    if not Path(helm_dir).exists():
        print(f'Error: helm dir not found: {helm_dir}', file=sys.stderr)
        print('  Run from inside infra-controller-core.', file=sys.stderr)
        sys.exit(1)

    tag = args.tag or git_sha()

    print('nico-dev — Upgrade Nico')
    print(f'  tag        : {tag}')
    print(f'  helm dir   : {helm_dir}')
    print(f'  kubeconfig : {kubeconfig}')

    r = subprocess.run([
        'helm', 'upgrade', 'nico', helm_dir,
        '-n', 'nico-system',
        '--reuse-values',
        '--set', f'global.image.tag={tag}',
        '--wait', '--timeout', '10m',
    ])

    if r.returncode != 0:
        print('Error: helm upgrade failed', file=sys.stderr)
        sys.exit(1)

    print(f'\n  Nico upgraded to {tag} ✓')
    print()
    subprocess.run(['kubectl', 'get', 'pods', '-n', 'nico-system', '--no-headers', '-o', 'wide'])

    if tag == 'latest':
        print('\n  (golden image — no registry needed)')


if __name__ == '__main__':
    main()
