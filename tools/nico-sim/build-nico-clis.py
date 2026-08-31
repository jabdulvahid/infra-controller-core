#!/usr/bin/env python3
"""
nico-sim — Build nico-admin-cli and machine-a-tron (MAT) from source

Builds the operator CLI tools from infra-controller-core using cargo.
Optionally installs them to a directory on $PATH.

Crates built:
  nico-admin-cli    — operator CLI for day-1 site setup, credential management
  machine-a-tron    — managed host BMC/DHCP simulator (MAT)

After building, machine-a-tron needs CAP_NET_BIND_SERVICE to bind port 443
(Nico's default BMC port). This script can apply setcap automatically.

Usage:
  ./build-nico-clis.py ~/sites/ytl
  ./build-nico-clis.py ~/sites/ytl --install-to ~/.local/bin
  ./build-nico-clis.py ~/sites/ytl --admin-cli-only
  ./build-nico-clis.py ~/sites/ytl --mat-only
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0])
    return str(p)


def parse_args():
    p = argparse.ArgumentParser(
        description='Build nico-admin-cli and machine-a-tron from source',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--install-to', default=None, metavar='DIR',
                   help='Copy built binaries to this directory (e.g. ~/.local/bin)')
    p.add_argument('--admin-cli-only', action='store_true',
                   help='Build only nico-admin-cli')
    p.add_argument('--mat-only', action='store_true',
                   help='Build only machine-a-tron')
    p.add_argument('--no-setcap', action='store_true',
                   help='Skip setcap cap_net_bind_service on machine-a-tron')
    p.add_argument('--release', action='store_true', default=True,
                   help='Build in release mode (default)')
    return p.parse_args()


def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def run(cmd, cwd=None, label=''):
    print(f'  $ {" ".join(str(c) for c in cmd)}')
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f'{label or cmd[0]} failed (exit {r.returncode})')


def build_crate(repo_path, crate_name, label):
    print(f'\n── Building {label} ──────────────────────────────────')
    print(f'  First build: ~5-10 min  |  incremental: ~1 min')
    print(f'  Cargo output follows — wait for the ✓ line below.')
    print()
    run(
        ['cargo', 'build', '--release', '-p', crate_name],
        cwd=repo_path,
        label=f'cargo build {crate_name}',
    )
    binary = Path(repo_path) / 'target' / 'release' / label
    if not binary.exists():
        # Try the crate name directly as binary name
        binary = Path(repo_path) / 'target' / 'release' / crate_name
    if not binary.exists():
        raise RuntimeError(f'Binary not found after build. Expected: {binary}')
    size_mb = binary.stat().st_size / 1024 / 1024
    print(f'  ✓ {binary}  ({size_mb:.0f} MB)')
    return binary


def apply_setcap(binary):
    """Grant CAP_NET_BIND_SERVICE so MAT can bind port 443 without root."""
    print(f'  Applying setcap cap_net_bind_service to {binary.name}...')
    r = subprocess.run(
        ['sudo', 'setcap', 'cap_net_bind_service=+ep', str(binary)],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f'  ✓ setcap applied — {binary.name} can bind port 443 without root')
    else:
        print(f'  ⚠ setcap failed: {r.stderr.strip()}')
        print(f'  Run manually: sudo setcap cap_net_bind_service=+ep {binary}')


def install_binary(binary, install_dir):
    """Copy binary to install_dir."""
    dest = Path(install_dir).expanduser() / binary.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(str(binary), str(dest))
    dest.chmod(0o755)
    print(f'  Installed: {dest}')
    return dest


def main():
    args = parse_args()
    site_yaml  = resolve_site(args.site)
    sim        = load_sim(site_yaml)
    repo       = sim.get('infra_controller_repo', '')

    if not repo:
        print('Error: infra_controller_repo not set in site yaml', file=sys.stderr)
        sys.exit(1)
    repo_path = Path(repo)
    if not repo_path.exists():
        print(f'Error: infra_controller_repo not found: {repo}', file=sys.stderr)
        sys.exit(1)

    build_admin = not args.mat_only
    build_mat   = not args.admin_cli_only

    print('nico-sim — Build CLI Tools')
    print(f'  repo      : {repo}')
    print(f'  building  : '
          + ', '.join(filter(None, [
              'nico-admin-cli' if build_admin else '',
              'machine-a-tron' if build_mat else '',
          ])))
    if args.install_to:
        print(f'  install-to: {args.install_to}')
    print()

    built = {}

    # ── Build nico-admin-cli ──────────────────────────────────────────────────
    if build_admin:
        binary = build_crate(repo_path, 'nico-admin-cli', 'nico-admin-cli')
        built['nico-admin-cli'] = binary

        if args.install_to:
            installed = install_binary(binary, args.install_to)
            built['nico-admin-cli'] = installed

    # ── Build machine-a-tron ──────────────────────────────────────────────────
    if build_mat:
        # The crate is named carbide-machine-a-tron, binary is machine-a-tron
        binary = build_crate(repo_path, 'carbide-machine-a-tron', 'machine-a-tron')
        built['machine-a-tron'] = binary

        if args.install_to:
            installed = install_binary(binary, args.install_to)
            built['machine-a-tron'] = installed

        if not args.no_setcap:
            print()
            apply_setcap(built['machine-a-tron'])

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print('=' * 55)
    print('Build complete.')
    print()
    for name, path in built.items():
        print(f'  {name}: {path}')
    print()

    if args.install_to:
        install_dir = Path(args.install_to).expanduser()
        print(f'Binaries installed to {install_dir}.')
        print(f'Ensure {install_dir} is in $PATH:')
        print(f'  export PATH="{install_dir}:$PATH"')
    else:
        print('Binaries are in target/release/. Add to PATH or use --install-to.')
        target = repo_path / 'target' / 'release'
        print(f'  export PATH="{target}:$PATH"')

    print()
    print('Next: configure certs and run scripts:')
    print(f'  ~/claude-notes/nico-sim/configure-clis.py {Path(args.site).expanduser()}')


if __name__ == '__main__':
    main()
