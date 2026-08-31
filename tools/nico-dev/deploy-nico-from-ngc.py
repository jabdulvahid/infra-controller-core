#!/usr/bin/env python3
"""
nico-dev — Deploy a pre-built nico image straight from NGC (no source build)

One command for the NGC-first user (user design 2026-08-26): ensures the
local registry exists (users who never ran a build have none), logs docker
into nvcr.io, pulls the arm64 image, retags it into the local registry, and
invokes the deploy underneath.

  ./deploy-nico-from-ngc.py <site> v2.2.0-pr-441-gc594e35f3
  ./deploy-nico-from-ngc.py <site> <tag> --token-env NGC_API_TOKEN_DSX_CARBIDE_DEV
  ./deploy-nico-from-ngc.py <site> <tag> --initial     # first deploy on a fresh site

Notes:
  - The NGC key must be minted for the org/team that hosts your image, with
    the registry-read role. Set the image via --ngc-image or NICO_NGC_IMAGE.
  - The repo checkout is STILL required (helm charts come from it, per the
    site yaml) — only the image build is skipped.
  - No-downgrade rule applies: pick a tag at or past your DB ledger
    (sha-match trick: git rev-parse --short=9 HEAD in the repo checkout).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_NGC_IMAGE = os.environ.get('NICO_NGC_IMAGE', '')
REGISTRY_PORT = 5000


def run(cmd, label, input_text=None, check=True):
    print(f'  $ {" ".join(cmd)}')
    r = subprocess.run(cmd, input=input_text, text=True)
    if check and r.returncode != 0:
        print(f'Error: {label} failed (exit {r.returncode})', file=sys.stderr)
        sys.exit(1)
    return r


def ensure_registry(port):
    """Start the local registry container if not already running (an NGC-first
    user has never run build-dev-nico-mac.py, which normally creates it)."""
    r = subprocess.run(['docker', 'inspect', 'registry', '--format',
                        '{{.State.Running}}'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() == 'true':
        print(f'  Registry already running on port {port} ✓')
        return
    if subprocess.run(['docker', 'start', 'registry'],
                      capture_output=True).returncode == 0:
        print('  Registry started ✓')
        return
    print(f'  Creating registry container on port {port}...')
    r3 = subprocess.run(['docker', 'run', '-d', '-p', f'{port}:5000',
                         '--restart=always', '--name', 'registry',
                         'registry:2'], capture_output=True)
    if r3.returncode != 0:
        print('Error: could not start registry (is colima/docker running?)',
              file=sys.stderr)
        print(r3.stderr.decode(), file=sys.stderr)
        sys.exit(1)
    print(f'  Registry created on port {port} ✓')


def main():
    p = argparse.ArgumentParser(
        description='Deploy a pre-built nico image from NGC',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('site', help='site folder (e.g. <share>/sites/dc1/dev1)')
    p.add_argument('ngc_tag', help='NGC image tag (e.g. v2.2.0-pr-441-g<sha>)')
    p.add_argument('--token-env', default='NGC_API_KEY', metavar='VAR',
                   help='NAME of the env var holding the NGC API key '
                        '(default: NGC_API_KEY). The value is never printed.')
    p.add_argument('--ngc-image', default=DEFAULT_NGC_IMAGE,
                   help=f'NGC image repository (default: {DEFAULT_NGC_IMAGE})')
    p.add_argument('--initial', action='store_true',
                   help='first deploy on a fresh site: run deploy-dev-nico.py '
                        'instead of redeploy-dev-nico.py')
    args = p.parse_args()

    if not args.ngc_image:
        print('Error: no NGC image set. Pass --ngc-image '
              'nvcr.io/<org>/<team>/<image> or export NICO_NGC_IMAGE.',
              file=sys.stderr)
        sys.exit(1)

    token = os.environ.get(args.token_env, '')
    if not token:
        print(f'Error: env var {args.token_env} is empty or unset. Export your '
              f'NGC API key there, or pass --token-env <var>.', file=sys.stderr)
        sys.exit(1)

    site = str(Path(args.site).expanduser().resolve())
    ngc_ref = f'{args.ngc_image}:{args.ngc_tag}'
    local_tag = f'ngc-{args.ngc_tag}'
    local_ref = f'localhost:{REGISTRY_PORT}/nico:{local_tag}'
    here = Path(__file__).resolve().parent

    print('nico-dev — Deploy nico from NGC')
    print(f'  site       : {site}')
    print(f'  NGC image  : {ngc_ref}')
    print(f'  local tag  : {local_tag}')
    print(f'  token from : ${args.token_env}')
    print()

    print('Step 1: Local registry...')
    ensure_registry(REGISTRY_PORT)

    print('Step 2: docker login nvcr.io...')
    run(['docker', 'login', 'nvcr.io', '-u', '$oauthtoken', '--password-stdin'],
        'docker login (check the key: needs registry-read on the image org/team '
        '— see how-to.md)', input_text=token)

    print('Step 3: Pull (linux/arm64)...')
    r = run(['docker', 'pull', '--platform', 'linux/arm64', ngc_ref],
            'docker pull', check=False)
    if r.returncode != 0:
        print('Error: pull failed. If the error says "no matching manifest",\n'
              'this tag has no arm64 build; if "manifest unknown", the tag\n'
              'does not exist (tags/list two-step in how-to.md lists them);\n'
            'if "unauthorized", the key lacks the org/role.', file=sys.stderr)
        sys.exit(1)

    print('Step 4: Retag + push into the local registry...')
    run(['docker', 'tag', ngc_ref, local_ref], 'docker tag')
    run(['docker', 'push', local_ref], 'docker push')

    # The REST images (Go) are NOT on NGC — they are always built from the
    # repo checkout, tagged to match the core image. Fast: buildx Go builds,
    # minutes. Found on the dev-up maiden run: --initial deploys the full
    # REST stack, which then pulls nico-rest-*:<local_tag> from the local
    # registry — nothing had ever pushed them (the earlier validation was a
    # redeploy over a golden clone with the REST stack already placed).
    print(f'Step 5: REST images from the checkout (tag {local_tag})...')
    run([sys.executable, str(here / 'build-dev-nico-mac.py'), site,
         '--tag', local_tag, '--rest-only'],
        'REST image build (rest-api/ buildx)')

    deploy = 'deploy-dev-nico.py' if args.initial else 'redeploy-dev-nico.py'
    print(f'Step 6: {deploy} --tag {local_tag}...')
    run([sys.executable, str(here / deploy), site, '--tag', local_tag],
        deploy)

    print()
    print('=' * 55)
    print(f'  nico deployed from NGC: {args.ngc_tag} ✓')
    print(f'  (local registry tag: nico:{local_tag})')
    print('=' * 55)


if __name__ == '__main__':
    main()
