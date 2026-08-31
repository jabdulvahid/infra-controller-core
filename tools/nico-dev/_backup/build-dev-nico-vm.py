#!/usr/bin/env python3
"""
nico-dev — Build Nico Docker images (ARM64) on VM and import into containerd.

Run from inside infra-controller-core on the VM:
  sudo python3 /path/to/build-dev-nico-vm.py
  sudo python3 /path/to/build-dev-nico-vm.py --tag v1.2.3

Images are imported directly into containerd's k8s.io namespace and tagged
as 192.168.64.1:5000/nico:<tag> — matching what the helm chart expects.
imagePullPolicy: IfNotPresent means k8s finds them without hitting any registry.

Build sequence (ARM64, native on VM):
  1. build-container-aarch64   dev/docker/Dockerfile.build-container-aarch64
  2. runtime-dev               nico-dev-docker/Dockerfile.runtime-dev  (no fluent-bit)
  3. nico:<tag>                nico-dev-docker/Dockerfile.nico-dev     (no CI gates)
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Image name prefix that the helm chart expects (matches deploy-dev-nico.py defaults)
REGISTRY_PREFIX = '192.168.64.1:5000'


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


def containerd_import(docker_tag, ctr_tag, label=''):
    """Save docker image and import into containerd k8s.io namespace."""
    print(f'  Importing {label or docker_tag} → containerd...')

    save = subprocess.Popen(
        ['docker', 'save', docker_tag],
        stdout=subprocess.PIPE,
    )
    imp = subprocess.run(
        ['ctr', '-n', 'k8s.io', 'images', 'import', '-'],
        stdin=save.stdout,
    )
    save.stdout.close()
    save.wait()

    if save.returncode != 0 or imp.returncode != 0:
        print(f'  Error: containerd import failed for {docker_tag}', file=sys.stderr)
        sys.exit(1)

    # After import, docker.io/library/<name>:<tag> exists in containerd.
    # Tag it to the registry-prefixed name so k8s finds it with IfNotPresent.
    docker_ctr_name = f'docker.io/library/{docker_tag}'
    r = subprocess.run(
        ['ctr', '-n', 'k8s.io', 'images', 'tag', '--force',
         docker_ctr_name, ctr_tag],
    )
    if r.returncode != 0:
        print(f'  Warning: ctr tag failed ({docker_ctr_name} → {ctr_tag})', file=sys.stderr)

    print(f'  {ctr_tag} in containerd ✓')


def git_sha():
    r = subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print('Error: not inside a git repo. Run from infra-controller-core.', file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--tag', default=None,
                   help='Image tag (default: git short SHA of cwd)')
    args = p.parse_args()

    repo_path = Path.cwd()
    tag = args.tag or git_sha()

    # Local docker tags (short names for build)
    build_ctr_local   = 'carbide-build:aarch64'
    runtime_ctr_local = 'carbide-runtime:aarch64'
    nico_local        = f'nico:{tag}'

    # Containerd names (must match helm chart image.repository + image.tag)
    build_ctr_ctr   = f'{REGISTRY_PREFIX}/carbide-build:aarch64'
    runtime_ctr_ctr = f'{REGISTRY_PREFIX}/carbide-runtime:aarch64'
    nico_ctr        = f'{REGISTRY_PREFIX}/nico:{tag}'

    print('nico-dev — Build Nico (ARM64, VM → containerd)')
    print(f'  repo       : {repo_path}')
    print(f'  tag        : {tag}')
    print(f'  target     : containerd k8s.io namespace')

    this_dir     = Path(__file__).parent / 'nico-dev-docker'
    docker_dir   = repo_path / 'dev' / 'docker'
    repo_dev_dir = repo_path / 'nico-dev-docker'

    repo_dev_dir.mkdir(exist_ok=True)
    copies = ['Dockerfile.runtime-dev', 'Dockerfile.nico-dev']
    for name in copies:
        shutil.copy2(this_dir / name, repo_dev_dir / name)

    try:
        print('\nStep 1: Build build-container-aarch64...')
        docker_build(
            tag=build_ctr_local,
            dockerfile=docker_dir / 'Dockerfile.build-container-aarch64',
            context=repo_path,
            label='build-container-aarch64 (Rust compiler)',
        )

        print('\nStep 2: Build runtime-dev...')
        docker_build(
            tag=runtime_ctr_local,
            dockerfile=repo_dev_dir / 'Dockerfile.runtime-dev',
            context=repo_path,
            label='runtime-dev (ARM64, no fluent-bit)',
        )

        print(f'\nStep 3: Build nico:{tag}...')
        docker_build(
            tag=nico_local,
            dockerfile=repo_dev_dir / 'Dockerfile.nico-dev',
            context=repo_path,
            build_args={
                'CONTAINER_BUILD_AARCH64':   build_ctr_local,
                'CONTAINER_RUNTIME_AARCH64': runtime_ctr_local,
            },
            label=f'nico:{tag} (ARM64 dev)',
        )
    finally:
        for name in copies:
            (repo_dev_dir / name).unlink(missing_ok=True)
        if repo_dev_dir.exists() and not any(repo_dev_dir.iterdir()):
            repo_dev_dir.rmdir()

    print('\nStep 4: Import into containerd k8s.io...')
    containerd_import(build_ctr_local,   build_ctr_ctr,   'carbide-build')
    containerd_import(runtime_ctr_local, runtime_ctr_ctr, 'carbide-runtime')
    containerd_import(nico_local,        nico_ctr,        f'nico:{tag}')

    print(f'\n{"="*55}')
    print('  Build + import complete ✓')
    print(f'  tag : {tag}')
    print(f'\n  Deploy (rolling update):')
    print(f'    python3 upgrade-dev-nico.py --tag {tag}')
    print(f'{"="*55}')


if __name__ == '__main__':
    main()
