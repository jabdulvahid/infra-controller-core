#!/usr/bin/env python3
"""
nico-dev — Build Nico Docker images for the host arch and push to the local registry.
(Formerly build-dev-nico-mac.py; runs on a Mac or a Linux host — the images
follow the host arch: arm64 on Apple Silicon, amd64 on x86_64.)

Reads the site yaml for registry host/port and infra-controller-core path.
Starts the Docker registry automatically if it is not already running.

Usage:
  python3 build-dev-nico.py <site> --tag <tag>
  python3 build-dev-nico.py <site> --tag test1
  python3 build-dev-nico.py <site> --tag latest --push-only

Build sequence (native for the host arch):
  1. build-container-<arch>    dev/docker/Dockerfile.build-container-{aarch64|x86_64}
  2. runtime-dev               nico-dev-docker/Dockerfile.runtime-dev  (no fluent-bit)
  3. nico:<tag>                nico-dev-docker/Dockerfile.nico-dev     (no CI gates)
  4. Push all three to localhost:<port>
"""

import argparse
import platform
import shutil
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


def ensure_registry(port):
    """Start registry container if not already running."""
    r = subprocess.run(
        ['docker', 'inspect', 'registry', '--format', '{{.State.Running}}'],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip() == 'true':
        print(f'  Registry already running on port {port} ✓')
        return

    # Container exists but stopped — start it
    r2 = subprocess.run(['docker', 'start', 'registry'], capture_output=True)
    if r2.returncode == 0:
        print(f'  Registry started ✓')
        return

    # Container doesn't exist — create it
    print(f'  Creating registry container on port {port}...')
    r3 = subprocess.run([
        'docker', 'run', '-d',
        '-p', f'{port}:5000',
        '--restart=always',
        '--name', 'registry',
        'registry:2',
    ], capture_output=True)
    if r3.returncode != 0:
        print(f'  Error: could not start registry', file=sys.stderr)
        print(r3.stderr.decode(), file=sys.stderr)
        sys.exit(1)
    print(f'  Registry created and started on port {port} ✓')


def docker_build(tag, dockerfile, context, build_args=None, label=''):
    print(f'  Building {label or tag}...')
    cmd = ['docker', 'build', '--progress=plain', '-t', tag, '-f', str(dockerfile)]
    if build_args:
        for k, v in build_args.items():
            cmd += ['--build-arg', f'{k}={v}']
    cmd.append(str(context))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f'  Error: docker build failed for {tag}', file=sys.stderr)
        sys.exit(1)
    print(f'  {tag} built ✓')


def docker_push(tag):
    print(f'  Pushing {tag}...')
    r = subprocess.run(['docker', 'push', tag])
    if r.returncode != 0:
        print(f'  Error: docker push failed for {tag}', file=sys.stderr)
        sys.exit(1)
    print(f'  {tag} pushed ✓')


# Host arch drives the whole build: Apple Silicon → aarch64/arm64,
# Intel/AMD → x86_64/amd64. The repo carries build containers for both.
# (macOS reports 'arm64', Linux 'aarch64' — both are the same arch.)
_ARM = platform.machine() in ('arm64', 'aarch64')
MACHINE = 'aarch64' if _ARM else 'x86_64'
DOCKER_ARCH = 'arm64' if _ARM else 'amd64'
HOST_OS = platform.system()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('site', help='Site folder or site yaml path')
    p.add_argument('--tag', required=True,
                   help='Image tag (e.g. latest, test1, v1.2.3, any string)')
    p.add_argument('--push-only', action='store_true',
                   help='Push already-built images, skip build')
    p.add_argument('--skip-rest', action='store_true',
                   help='Skip building the NICo REST images (rest-api/ Makefile)')
    p.add_argument('--rest-only', action='store_true',
                   help='Build/push ONLY the REST images — for the NGC path, '
                        'where the core image comes pre-built but the REST '
                        'images are always built from this checkout (Go, '
                        'minutes not tens of minutes)')
    p.add_argument('--overwrite-tag', action='store_true',
                   help='Allow rebuilding a tag that already exists in the registry '
                        '(the cluster will NOT pick it up without a new tag or '
                        'imagePullPolicy Always — see the warning)')
    args = p.parse_args()
    if args.rest_only and args.skip_rest:
        p.error('--rest-only and --skip-rest are contradictory')

    site_yaml, site_folder = resolve_site(args.site)
    cfg = yaml.safe_load(open(site_yaml))

    registry = cfg.get('registry', {})
    reg_host = registry.get('host', '192.168.64.1')
    reg_port = registry.get('port', 5000)

    nico_mac = cfg.get('nico_mac_folder', '')
    if not nico_mac:
        print('Error: nico_mac_folder not set in site yaml', file=sys.stderr)
        print('  Re-run create-dev-site.py with --nico-mac-folder', file=sys.stderr)
        sys.exit(1)

    repo_folder = cfg.get('nico_repo_folder', 'infra-controller-core')
    repo_path = Path(nico_mac).expanduser() / repo_folder
    if not repo_path.exists():
        print(f'Error: nico repo not found at {repo_path}', file=sys.stderr)
        print('  Check nico_repo_folder in the site yaml (set by create-dev-site.py).',
              file=sys.stderr)
        sys.exit(1)

    push_reg    = f'localhost:{reg_port}'
    build_ctr   = f'{push_reg}/carbide-build:{MACHINE}'
    runtime_ctr = f'{push_reg}/carbide-runtime:{MACHINE}'
    nico_img    = f'{push_reg}/nico:{args.tag}'

    # Docker reachable? (colima not started is the usual cause on a Mac)
    if subprocess.run(['docker', 'info'], capture_output=True).returncode != 0:
        print('Error: docker daemon not reachable.', file=sys.stderr)
        print('  Mac: colima start   |   Linux: sudo systemctl start docker '
              '(and usermod -aG docker $USER, relogin)', file=sys.stderr)
        sys.exit(1)

    # Show exactly what is being built — the image tag is just a label; the
    # CODE is whatever this checkout points at.
    def git(*a):
        return subprocess.run(['git', '-C', str(repo_path)] + list(a),
                              capture_output=True, text=True).stdout.strip()
    branch = git('branch', '--show-current') or '(detached HEAD)'
    sha    = git('rev-parse', '--short', 'HEAD')
    dirty  = ' +uncommitted-changes' if git('status', '--porcelain') else ''

    print(f'nico-dev — Build Nico (linux/{DOCKER_ARCH}, on this {HOST_OS} host)')
    print(f'  site     : {site_yaml}')
    print(f'  repo     : {repo_path}')
    print(f'  checkout : {branch} @ {sha}{dirty}')
    print(f'  registry : {push_reg}  (VM pulls from {reg_host}:{reg_port})')
    print(f'  tag      : {args.tag}')
    if dirty:
        print('  ⚠ repo has uncommitted changes — they WILL be baked into the image')

    print('\nStep 1: Ensuring registry is running...')
    ensure_registry(reg_port)

    # Same-tag rebuild trap (20260903-#1): the cluster's pod template still
    # says nico:<tag>, so a redeploy changes nothing and the node reuses its
    # cached image (imagePullPolicy IfNotPresent). Refuse to overwrite an
    # existing tag unless asked; the dev loop should bump the tag every time.
    if not args.push_only:
        r = subprocess.run(['curl', '-sf', '-m', '5',
                            f'http://{push_reg}/v2/nico/tags/list'],
                           capture_output=True, text=True)
        existing = []
        if r.returncode == 0:
            try:
                import json
                existing = json.loads(r.stdout).get('tags') or []
            except ValueError:
                pass
        if args.tag in existing:
            if not args.overwrite_tag:
                print(f'\nError: nico:{args.tag} already exists in {push_reg}.\n'
                      f'  A rebuild under the same tag is invisible to the cluster: '
                      f'the pod template does not change, so nothing rolls out,\n'
                      f'  and the node keeps the cached image. Use a new tag '
                      f'(e.g. --tag {args.tag}-2), or --overwrite-tag if you '
                      f'really mean it.', file=sys.stderr)
                sys.exit(1)
            print(f'  ⚠ overwriting existing tag {args.tag} — the cluster will not '
                  f'pull it unless the pod template changes or pull policy is Always')

    this_dir     = Path(__file__).parent / 'nico-dev-docker'
    docker_dir   = repo_path / 'dev' / 'docker'
    repo_dev_dir = repo_path / 'nico-dev-docker'

    if args.rest_only:
        print('\nSteps 2-4: Skipped (--rest-only: core image comes from NGC)')
    elif not args.push_only:
        repo_dev_dir.mkdir(exist_ok=True)
        copies = ['Dockerfile.runtime-dev', 'Dockerfile.nico-dev']
        for name in copies:
            shutil.copy2(this_dir / name, repo_dev_dir / name)

        try:
            print(f'\nStep 2: Build build-container-{MACHINE}...')
            docker_build(
                tag=build_ctr,
                dockerfile=docker_dir / f'Dockerfile.build-container-{MACHINE}',
                context=repo_path,
                label=f'build-container-{MACHINE} (Rust compiler)',
            )

            print('\nStep 3: Build runtime-dev...')
            docker_build(
                tag=runtime_ctr,
                dockerfile=repo_dev_dir / 'Dockerfile.runtime-dev',
                context=repo_path,
                label=f'runtime-dev ({DOCKER_ARCH}, no fluent-bit)',
            )

            print(f'\nStep 4: Build nico:{args.tag}...')
            docker_build(
                tag=nico_img,
                dockerfile=repo_dev_dir / 'Dockerfile.nico-dev',
                context=repo_path,
                build_args={
                    # ARG names are historical; values are for the HOST arch
                    'CONTAINER_BUILD_AARCH64':   build_ctr,
                    'CONTAINER_RUNTIME_AARCH64': runtime_ctr,
                    # what `carbide-api version` reports as build_version —
                    # the production Makefile passes the same git describe;
                    # without it the dev image says build_version= (blank)
                    'VERSION': git('describe', '--tags', '--always', '--dirty')
                               or f'dev-{args.tag}',
                    # kea hook install path: /usr/lib/<triplet>/kea/hooks
                    'GNU_TRIPLET':               f'{MACHINE}-linux-gnu',
                },
                label=f'nico:{args.tag} ({DOCKER_ARCH} dev)',
            )
        finally:
            for name in copies:
                (repo_dev_dir / name).unlink(missing_ok=True)
            if repo_dev_dir.exists() and not any(repo_dev_dir.iterdir()):
                repo_dev_dir.rmdir()
    else:
        print('\nSteps 2-4: Skipped (--push-only)')

    # ── NICo REST images (Go, rest-api/ Makefile — base feature) ──────────────
    rest_enabled = cfg.get('nico-system', {}).get('rest', {}).get('enabled', True)
    rest_dir = repo_path / 'rest-api'
    if args.skip_rest or not rest_enabled:
        print('\nStep 4b: NICo REST images — skipped '
              f'({"--skip-rest" if args.skip_rest else "rest disabled in site yaml"})')
    elif not rest_dir.is_dir():
        print(f'\nError: REST enabled but {rest_dir} not found in the checkout.',
              file=sys.stderr)
        sys.exit(1)
    else:
        # buildx is required (the production Dockerfiles use TARGETARCH).
        r = subprocess.run(['docker', 'buildx', 'version'], capture_output=True)
        if r.returncode != 0:
            print('\nError: docker buildx not available — required for the REST '
                  'image build.\n  Mac (colima): brew install docker-buildx && '
                  'mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/bin/'
                  'docker-buildx" ~/.docker/cli-plugins/docker-buildx\n'
                  '  Linux: sudo apt install docker-buildx',
                  file=sys.stderr)
            sys.exit(1)
        # NOT `make docker-build`: the Makefile honors DOCKER_ARCHES in its
        # build loop but the manifest step hardcodes both -amd64 and -arm64
        # tags, so an arm64-only build dies at 'imagetools create'. Run the
        # equivalent buildx loop directly — arm64-only, tagged {tag} straight
        # (single-arch needs no manifest), and only the images the base
        # deploy references (the Makefile's full list adds nico-flow/psm/
        # nsm/mcp, which base nico-dev never pulls).
        rest_images = [
            'nico-rest-api', 'nico-rest-workflow', 'nico-rest-site-manager',
            'nico-rest-site-agent', 'nico-rest-db', 'nico-rest-cert-manager',
        ]
        print(f'\nStep 4b: Building NICo REST images (Go, {DOCKER_ARCH}) → {push_reg}...')
        for i, image in enumerate(rest_images, 1):
            print(f'\n  [{i}/{len(rest_images)}] {image}:{args.tag}')
            r = subprocess.run(
                ['docker', 'buildx', 'build', '--platform', f'linux/{DOCKER_ARCH}',
                 '--push',
                 '--build-arg', 'TARGETOS=linux',
                 '--build-arg', f'TARGETARCH={DOCKER_ARCH}',
                 '-t', f'{push_reg}/{image}:{args.tag}',
                 '-f', f'docker/production/Dockerfile.{image}', '.'],
                cwd=rest_dir)
            if r.returncode != 0:
                print(f'  Error: REST image build failed ({image})', file=sys.stderr)
                sys.exit(1)
        print(f'\n  REST images built and pushed ✓ '
              f'({len(rest_images)} images, tag {args.tag})')

    if args.rest_only:
        print(f'\n{"="*55}')
        print(f'  REST-only build + push complete ✓  (tag {args.tag})')
        print(f'{"="*55}')
        return

    # Size gate: with CARGO_PROFILE_RELEASE_DEBUG=false the nico image is
    # ~1-2 GB uncompressed (265 MB compressed). Debug symbols once bloated it
    # to 10.7 GB — too big to pull reliably over the UTM NAT link. Catch that
    # regression HERE, not as a mysterious pull timeout on the VM.
    r = subprocess.run(['docker', 'image', 'inspect', nico_img,
                        '--format', '{{.Size}}'], capture_output=True, text=True)
    if r.returncode == 0:
        size_gb = int(r.stdout.strip()) / 1024**3
        print(f'\n  nico image size: {size_gb:.1f} GB uncompressed')
        if size_gb > 4:
            print(f'  ⚠ WARNING: image is unusually large — debug symbols may have crept back in.')
            print(f'    Check CARGO_PROFILE_RELEASE_DEBUG=false in nico-dev-docker/Dockerfile.nico-dev')
            print(f'    (a bloated image fails or times out pulling over the UTM NAT link)')

    print('\nStep 5: Pushing to registry...')
    docker_push(build_ctr)
    docker_push(runtime_ctr)
    docker_push(nico_img)

    print(f'\n{"="*55}')
    print('  Build + push complete ✓')
    print(f'  tag : {args.tag}')
    here = Path(__file__).resolve().parent
    print(f'\n  Verify registry (host or VM):')
    print(f'    {here}/ndev.py {args.site} registry verify')
    print(f'\n  First deploy (on the host):')
    print(f'    {here}/deploy-dev-nico.py {args.site} --tag {args.tag}')
    print(f'\n  Redeploy after code change (on the host):')
    print(f'    {here}/redeploy-dev-nico.py {args.site} --tag {args.tag}')
    print(f'{"="*55}')


if __name__ == '__main__':
    main()
