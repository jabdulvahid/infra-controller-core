#!/usr/bin/env python3
"""
nico-dev — Deploy a pre-built nico image straight from NGC (no source build)

One command for the NGC-first user (user design 2026-08-26): ensures the
local registry exists (users who never ran a build have none), logs docker
into nvcr.io, pulls the host-arch image, retags it into the local registry, and
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
import platform
import subprocess
import sys
from pathlib import Path

DEFAULT_NGC_IMAGE = os.environ.get('NICO_NGC_IMAGE', '')
REGISTRY_PORT = 5000
# VM arch == host arch: Apple Silicon pulls arm64, Intel pulls amd64.
DOCKER_ARCH = 'arm64' if platform.machine() == 'arm64' else 'amd64'


def run(cmd, label, input_text=None, check=True):
    print(f'  $ {" ".join(cmd)}')
    r = subprocess.run(cmd, input=input_text, text=True)
    if check and r.returncode != 0:
        print(f'Error: {label} failed (exit {r.returncode})', file=sys.stderr)
        sys.exit(1)
    return r


def ensure_registry(port):
    """Start the local registry container if not already running (an NGC-first
    user has never run build-dev-nico.py, which normally creates it)."""
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
    p.add_argument('--images', default=None, metavar='JSON',
                   help='NGC image names {"core": ..., "rest": {local: ngc}}; '
                        'default: images.source.names in the site yaml, else the fixed names')
    p.add_argument('--tags', default=None, metavar='GROUP=TAG,...',
                   help='per-group NGC tag overrides (core, rest); groups not named '
                        'use the positional tag (bringup.yaml ngc.tags)')
    p.add_argument('--no-import', action='store_true',
                   help='skip the image delivery through the share (the VM then pulls '
                        'through the registry tunnel — the stall-prone path)')
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
    here = Path(__file__).resolve().parent
    import importlib.util
    _spec = importlib.util.spec_from_file_location('site_images', here / 'site_images.py')
    site_images = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(site_images)
    # one default tag, optional per-group override; each group is one Helm release
    tags = site_images.resolve_group_tags(args.ngc_tag, args.tags)
    local_tags = {g: f'ngc-{t}' for g, t in tags.items()}
    # the site yaml (read once, used for the recorded NGC names, the delivery
    # and the provenance record below)
    site_yamls = [f for f in Path(site).glob('*.yaml') if '.kubeconfig' not in f.name]
    site_cfg = {}
    if len(site_yamls) == 1:
        import yaml as _yaml
        site_cfg = _yaml.safe_load(site_yamls[0].read_text()) or {}
    # NGC names: --images, else the site yaml's recorded map, else the defaults
    recorded = site_images.read(site_cfg)['source']['names'] if site_cfg else {}
    names = site_images.resolve_ngc_names(site_images.parse_names_arg(args.images) or recorded)
    ngc_ref = f'{args.ngc_image}:{tags["core"]}'
    local_tag = local_tags['core']
    local_ref = f'localhost:{REGISTRY_PORT}/nico:{local_tag}'

    print('nico-dev — Deploy nico from NGC')
    print(f'  site       : {site}')
    print(f'  NGC image  : {ngc_ref}')
    print(f'  local tag  : {local_tag}')
    print(f'  REST tag   : {tags["rest"]}' + ('' if tags['rest'] == tags['core'] else '   (override)'))
    print(f'  token from : ${args.token_env}')
    print()

    print('Step 1: Local registry...')
    ensure_registry(REGISTRY_PORT)

    print('Step 2: docker login nvcr.io...')
    run(['docker', 'login', 'nvcr.io', '-u', '$oauthtoken', '--password-stdin'],
        'docker login (check the key: needs registry-read on the image org/team '
        '— see how-to.md)', input_text=token)

    print(f'Step 3: Pull (linux/{DOCKER_ARCH})...')
    r = run(['docker', 'pull', '--platform', f'linux/{DOCKER_ARCH}', ngc_ref],
            'docker pull', check=False)
    if r.returncode != 0:
        print('Error: pull failed. If the error says "no matching manifest",\n'
              f'this tag has no {DOCKER_ARCH} build; if "manifest unknown", the tag\n'
              'does not exist (tags/list two-step in how-to.md lists them);\n'
            'if "unauthorized", the key lacks the org/role.', file=sys.stderr)
        sys.exit(1)

    print('Step 4: Retag + push into the local registry...')
    run(['docker', 'tag', ngc_ref, local_ref], 'docker tag')
    run(['docker', 'push', local_ref], 'docker push')

    # REST images: CI publishes them alongside the core image, SAME tag,
    # same org/team (verified 2026-08-31: nico-rest-db's tag list includes
    # v2.2.0-pr-441-gc594e35f3). Pull + retag + push the six the base
    # deploy references — zero build, version-matched with the core by
    # construction. Fallback if a tag is missing: build them from the
    # checkout (--rest-only; version skew vs the core is then possible).
    rest_images = site_images.IMAGE_NAMES['rest']      # the fixed local (chart) names, one place
    ngc_base, ngc_core_name = site_images.split_ngc_image(args.ngc_image)
    rest_tag, rest_local = tags['rest'], local_tags['rest']
    print(f'Step 5: REST images from NGC ({ngc_base}/*:{rest_tag})...')
    fell_back = False
    for i, image in enumerate(rest_images, 1):
        ngc_name = names['rest'].get(image, image)
        src = f'{ngc_base}/{ngc_name}:{rest_tag}'
        dst = f'localhost:{REGISTRY_PORT}/{image}:{rest_local}'
        print(f'  [{i}/{len(rest_images)}] {image}' + (f'  (NGC: {ngc_name})' if ngc_name != image else ''))
        r = run(['docker', 'pull', '--platform', f'linux/{DOCKER_ARCH}', src],
                f'pull {image}', check=False)
        if r.returncode != 0:
            print(f'\n  WARNING: {src} not pullable — falling back to '
                  f'building the REST images from the checkout\n'
                  f'  (they may not exactly match the core image\'s '
                  f'commit).')
            run([sys.executable, str(here / 'build-dev-nico.py'), site,
                 '--tag', rest_local, '--rest-only'],
                'REST image build (rest-api/ buildx)')
            fell_back = True
            break
        run(['docker', 'tag', src, dst], f'tag {image}')
        run(['docker', 'push', dst], f'push {image}')
    if not fell_back:
        print(f'  REST images pulled + pushed ✓ ({len(rest_images)}, '
              f'tag {rest_tag} → {rest_local})')

    # Deliver the images into the VM's containerd through the share, so kubelet
    # never pulls them through the registry tunnel (stalls; and the 9 GB layer
    # unpack that outlived containerd's watchdog). imagePullPolicy is
    # IfNotPresent everywhere, so a present image is simply used.
    if not args.no_import and site_cfg:
        _spec = importlib.util.spec_from_file_location('image_delivery', here / 'image_delivery.py')
        _idl = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_idl)
        vm_registry = site_images.read(site_cfg)['registry']          # as the VM names it
        refs = [f'{vm_registry}/nico:{local_tag}'] + [f'{vm_registry}/{img}:{rest_local}' for img in rest_images]
        print('Step 5b: Deliver images to the VM through the share...')
        _idl.deliver(site_cfg, site, refs, label=local_tag, push_reg=f'localhost:{REGISTRY_PORT}')

    # Record the source in the site yaml (source of truth) BEFORE deploying,
    # so a failed deploy still leaves the provenance behind; the deploy
    # script records images.tag itself on success.
    if len(site_yamls) == 1:
        site_images.record(site_yamls[0], source={
            'kind': 'ngc', 'registry': ngc_base, 'tag': args.ngc_tag, 'tags': tags,
            'core_image': ngc_core_name, 'token_env': args.token_env, 'names': names})
        print(f'  site yaml images.source updated ({site_yamls[0].name})')

    deploy = 'deploy-dev-nico.py' if args.initial else 'redeploy-dev-nico.py'
    # the core release takes the core tag; the REST releases take theirs
    # (redeploy rolls the core release only, so it needs no REST tag)
    deploy_cmd = [sys.executable, str(here / deploy), site, '--tag', local_tag]
    if args.initial:
        deploy_cmd += ['--rest-tag', rest_local]
    print(f'Step 6: {deploy} --tag {local_tag}' + (f' --rest-tag {rest_local}' if args.initial else '') + '...')
    run(deploy_cmd, deploy)

    print()
    print('=' * 55)
    print(f'  nico deployed from NGC: core {tags["core"]}, REST {rest_tag} ✓')
    print(f'  (local registry tags: nico:{local_tag}, REST {rest_local})')
    print('=' * 55)


if __name__ == '__main__':
    main()
