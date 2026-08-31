#!/usr/bin/env python3
"""
DC Simulation — Nico component builder

Builds Nico Docker images from infra-controller-core source and pushes
to the utility VM registry (7.130.4.2:5000 by default).

Key insight: all core Nico services share a SINGLE Docker image ('nico').
  nico-api, nico-dhcp, nico-dns, nico-hardware-health, nico-ssh-console-rs,
  nico-bmc-proxy, nico-machine-a-tron → all use image: {registry}/nico:{tag}

Components NOT built (disabled or third-party in simulation):
  nico-pxe, nico-dsx-exchange-consumer  — disabled in nico-sim.yaml
  nico-ntp                               — uses dockurr/chrony (third-party)
  REST services (nico-rest, nico-mcp)    — disabled in nico-prereqs

Build sequence (x86_64 only — simulation host is amd64):
  1. build-container   Dockerfile.build-container-x86_64   (Rust compiler)
  2. runtime-container Dockerfile.runtime-container-x86_64 (Debian runtime)
  3. nico:{tag}        Dockerfile.release-container-sa-x86_64 (multi-stage)
  4. push all three to utility VM registry

Prerequisites:
  - Docker with buildx plugin installed on sim-host
  - Utility VM deployed and reachable at its fabric IP (7.130.4.2)
  - sim-host has route to 7.130.4.0/30 via fabric (set by deploy.sh)

Usage:
  python3 build-nico-components.py nico-sim.yaml
  python3 build-nico-components.py nico-sim.yaml --tag v1.2.3
  python3 build-nico-components.py nico-sim.yaml --skip-registry-setup
"""

import argparse
import ipaddress
import shutil
import subprocess
import sys
import json
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Build Nico Docker images and push to simulation registry',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--tag', default=None,
                   help='Image tag (default: from nico-system.image.tag in nico-sim.yaml)')
    p.add_argument('--skip-registry-setup', action='store_true',
                   help='Skip starting registry:2 container on utility VM '
                        '(use if already running or not yet deployed)')
    p.add_argument('--push-only', action='store_true',
                   help='Push already-built local images to registry (skip build)')
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────



def resolve_site(arg):
    from pathlib import Path as _P
    p = _P(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=__import__('sys').stderr); __import__('sys').exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=__import__('sys').stderr); __import__('sys').exit(1)
        return str(yamls[0])
    return str(p)

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def utility_fabric_ip(sim):
    """Utility VM fabric IP: always the .2 host of fabric.utility_link.prefix."""
    util_prefix = sim['fabric'].get('registry_link', sim['fabric'].get('utility_link', {})).get('prefix', '7.132.0.4/30')
    net         = ipaddress.IPv4Network(util_prefix, strict=False)
    return str(list(net.hosts())[1])   # .2 = utility VM


def find_priv_key():
    import sim_ssh
    return sim_ssh.find_priv_key()


SSH_OPTS = ['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes']


# ── Registry VM setup ─────────────────────────────────────────────────────────

def ensure_registry_running(reg_ip, reg_port, priv_key):
    """Check if utility VM registry:2 container is running and reachable."""
    # Utility VM fabric IP is deterministic (7.130.4.2) — no OOB lookup needed
    print(f'  Registry: {reg_ip}:{reg_port}')
    print('  Note: utility VM OOB must be accessible to start registry:2.')
    print('  If utility VM is not yet deployed, use --skip-registry-setup')
    print('  and start registry:2 manually: docker run -d -p 5000:5000 registry:2')

    # Check if registry is reachable
    r = subprocess.run(['curl', '-sf', f'http://{reg_ip}:{reg_port}/v2/'],
                       capture_output=True, timeout=5)
    if r.returncode == 0:
        print(f'  Registry already reachable at {reg_ip}:{reg_port} ✓')
        return

    print(f'  Registry not reachable — attempting to start on utility VM...')
    # Would need OOB IP to SSH — for now inform the user
    print(f'  Please start registry on the utility VM:')
    print(f'    docker run -d -p {reg_port}:{reg_port} --restart=always --name registry registry:2')


def configure_insecure_registry(reg_addr):
    """Ensure Docker daemon allows pushing to the insecure local registry."""
    daemon_cfg = Path('/etc/docker/daemon.json')
    try:
        cfg = json.loads(daemon_cfg.read_text()) if daemon_cfg.exists() else {}
    except json.JSONDecodeError:
        cfg = {}

    insecure = cfg.get('insecure-registries', [])
    if reg_addr not in insecure:
        insecure.append(reg_addr)
        cfg['insecure-registries'] = insecure
        cfg_json = json.dumps(cfg, indent=2)
        print(f'  Adding {reg_addr} to /etc/docker/daemon.json insecure-registries')
        # Write via sudo — daemon.json is root-owned
        r = subprocess.run(['sudo', 'tee', str(daemon_cfg)],
                           input=cfg_json, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'  Note: could not write daemon.json: {r.stderr.strip()}')
            print(f'  Run manually: echo \'{cfg_json}\' | sudo tee {daemon_cfg}')
            print(f'  Then: sudo systemctl reload docker')
            return
        subprocess.run(['sudo', 'systemctl', 'reload', 'docker'], check=True)
        print('  Docker daemon reloaded ✓')
    else:
        print(f'  {reg_addr} already in insecure-registries ✓')


# ── Docker build helpers ──────────────────────────────────────────────────────

def docker_build(tag, dockerfile, context, build_args=None, label=''):
    print(f'  Building {label or tag}...')
    cmd = ['docker', 'build',
           '--progress=plain',   # show full RUN output (cargo errors visible)
           '-t', tag, '-f', str(dockerfile)]
    if build_args:
        for k, v in build_args.items():
            cmd += ['--build-arg', f'{k}={v}']
    cmd.append(str(context))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f'docker build failed for {tag}')
    print(f'  Built {tag} ✓')


def docker_push(tag):
    print(f'  Pushing {tag}...')
    r = subprocess.run(['docker', 'push', tag])
    if r.returncode != 0:
        raise RuntimeError(f'docker push failed for {tag}')
    print(f'  Pushed {tag} ✓')


# ── Build sequence ────────────────────────────────────────────────────────────

def build_and_push(sim, registry, tag, push_only):
    """
    Build order:
      1. build-container   — Rust compiler + build tools (x86_64)
      2. runtime-container — minimal Debian runtime (x86_64)
      3. nico              — multi-stage: compile all Rust then copy into runtime
    """
    repo       = sim['infra_controller_repo']
    repo_path  = Path(repo)
    docker_dir = repo_path / 'dev' / 'docker'

    # nico-simulation/ files live in claude-notes — copy into nico repo before build,
    # remove after.  The nico repo (infra-controller-core) is never permanently modified.
    this_dir       = Path(__file__).parent
    local_sim_dir  = this_dir / 'nico-simulation'
    repo_sim_dir   = repo_path / 'nico-simulation'

    build_ctr   = f'{registry}/carbide-build:x86_64'
    runtime_ctr = f'{registry}/carbide-runtime:x86_64'
    nico_img    = f'{registry}/nico:{tag}'

    if not push_only:
        # Copy simulation files into nico repo for the duration of the build
        repo_sim_dir.mkdir(exist_ok=True)
        runtime_src = local_sim_dir / 'Dockerfile.runtime-sim'
        runtime_dst = repo_sim_dir / 'Dockerfile.runtime-sim'
        shutil.copy2(runtime_src, runtime_dst)
        print(f'  Copied {runtime_src.name} → {runtime_dst}')

        # Also copy nico sim build Dockerfile
        nico_src = local_sim_dir / 'Dockerfile.nico-sim'
        nico_dst = repo_sim_dir / 'Dockerfile.nico-sim'
        shutil.copy2(nico_src, nico_dst)
        print(f'  Copied {nico_src.name} → {nico_dst}')

        try:
            # Step 1: build-container (Rust compiler)
            docker_build(
                tag=build_ctr,
                dockerfile=docker_dir / 'Dockerfile.build-container-x86_64',
                context=repo_path,
                label='build-container (Rust compiler)',
            )

            # Step 2: runtime-container (sim variant — no fluent-bit)
            # Production Dockerfile pins a fluent-bit GPG key that drifts from
            # what packages.fluentbit.io serves; fluent-bit is not needed in simulation.
            docker_build(
                tag=runtime_ctr,
                dockerfile=runtime_dst,
                context=repo_path,
                label='runtime-container (sim — no fluent-bit)',
            )

            # Step 3: nico image — sim variant: cargo build only, no CI gates,
            # excludes bmc-mock (broken Cargo.toml in current branch, test mock only)
            docker_build(
                tag=nico_img,
                dockerfile=nico_dst,
                context=repo_path,
                build_args={
                    'CONTAINER_BUILD_X86_64':   build_ctr,
                    'CONTAINER_RUNTIME_X86_64': runtime_ctr,
                },
                label=f'nico:{tag} (sim build — no CI gates, no bmc-mock)',
            )

        finally:
            # Always clean up — leave nico repo unchanged
            runtime_dst.unlink(missing_ok=True)
            nico_dst.unlink(missing_ok=True)
            if repo_sim_dir.exists() and not any(repo_sim_dir.iterdir()):
                repo_sim_dir.rmdir()
            print(f'  Cleaned up sim Dockerfiles from nico repo')

    # Push all three
    print()
    print('Pushing images to registry...')
    docker_push(build_ctr)
    docker_push(runtime_ctr)
    docker_push(nico_img)

    return nico_img


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    sim  = load_sim(resolve_site(args.site))

    ns        = sim.get('nico-system', {})
    img_cfg   = ns.get('image', {})
    tag       = args.tag or img_cfg.get('local_nico_tag', 'latest')
    reg_port  = sim.get('nico_container_registry', sim.get('utility', {})).get('port', 5000)
    reg_ip    = utility_fabric_ip(sim)
    registry  = f'{reg_ip}:{reg_port}'
    repo      = sim.get('infra_controller_repo', '')

    print('DC Simulation — Nico Component Builder')
    print(f'  repo             : {repo}')
    print(f'  registry         : {registry}')
    print(f'  image tag        : {tag}')
    print(f'  image source     : {img_cfg.get("source", "local")}')
    print()

    if not repo:
        print('Error: infra_controller_repo not set in nico-sim.yaml', file=sys.stderr)
        sys.exit(1)
    if not Path(repo).exists():
        print(f'Error: infra_controller_repo not found: {repo}', file=sys.stderr)
        sys.exit(1)

    if img_cfg.get('source', 'local') != 'local':
        print('image.source is not "local" — nothing to build.')
        print('For nvcr source, images are pulled directly during deployment.')
        sys.exit(0)

    # ── Configure insecure registry on Docker daemon ──────────────────────────
    print('Step 1: Configure Docker for insecure local registry')
    try:
        configure_insecure_registry(registry)
    except PermissionError:
        print(f'  Note: cannot write /etc/docker/daemon.json (not root).')
        print(f'  Add manually: {{"insecure-registries": ["{registry}"]}}')
        print(f'  Then: sudo systemctl reload docker')
    print()

    # ── Ensure registry:2 is running on utility VM ────────────────────────────
    if not args.skip_registry_setup:
        print('Step 2: Ensure registry:2 is running on utility VM')
        ensure_registry_running(reg_ip, reg_port, find_priv_key())
        print()
    else:
        print('Step 2: Skipped (--skip-registry-setup)')
        print()

    # ── Build and push ────────────────────────────────────────────────────────
    action = 'Pushing' if args.push_only else 'Building and pushing'
    print(f'Step 3: {action} Nico images')
    nico_img = build_and_push(sim, registry, tag, push_only=args.push_only)
    print()

    print('=' * 60)
    print('Build complete.')
    print()
    print('Images available in simulation registry:')
    print(f'  {nico_img}')
    print()
    print('Next: update nico-sim.yaml and run generate-sim-values.py')
    print(f'  nico-system:')
    print(f'    image:')
    print(f'      source: local')
    print(f'      tag: {tag}')
    print()
    print('Then deploy:')
    print('  python3 generate-sim-values.py nico-sim.yaml')
    print('  python3 deploy-nico-system.py nico-sim.yaml')
    print('=' * 60)


if __name__ == '__main__':
    main()
