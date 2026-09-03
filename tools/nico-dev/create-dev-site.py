#!/usr/bin/env python3
"""
nico-dev — Create a new dev simulation site.

Generates a site yaml from the nico-dev.yaml template with site-specific
IP prefixes, names, and shared folder paths.

Usage:
  python3 create-dev-site.py \\
    --dc-name dc1 --site-name dev \\
    --underlay 11 --overlay 12 \\
    --folder /mnt/mac/sites/dev \\
    --nico-vm-folder /mnt/mac \\
    --nico-mac-folder ~/Mac
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


def parse_args():
    p = argparse.ArgumentParser(
        description='Create a new nico-dev site',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('--dc-name',        required=True, help='Datacenter name (e.g. dc1)')
    p.add_argument('--site-name',      required=True, help='Site name (e.g. dev)')
    p.add_argument('--folder',         required=True,
                   help='Output folder for the site yaml (must be inside the shared folder)')
    p.add_argument('--underlay',       type=int, required=True,
                   help='Underlay octet (e.g. 7 → 7.x.x.x fabric)')
    p.add_argument('--overlay',        type=int, required=True,
                   help='Overlay octet (e.g. 8 → 8.x.x.x tenant VPCs)')
    p.add_argument('--nico-vm-folder', required=True,
                   help='Shared folder root as seen from the VM (e.g. /mnt/mac)')
    p.add_argument('--nico-mac-folder', required=True,
                   help='Shared folder root as seen from the Mac (e.g. ~/Mac)')
    p.add_argument('--nico-repo-folder', required=True,
                   help='Nico repo dir INSIDE the shared folder, e.g. '
                        'infra-controller-core or infra-controller (validated to exist)')
    p.add_argument('--nico-dev-folder', required=True,
                   help='nico-dev scripts dir INSIDE the shared folder, e.g. '
                        'claude-notes/nico-dev or nico-dev (validated to exist)')
    p.add_argument('--registry-host',  default='192.168.64.1',
                   help='Mac registry host as seen from VM (default: 192.168.64.1)')
    p.add_argument('--registry-port',  type=int, default=5000,
                   help='Mac registry port (default: 5000)')
    p.add_argument('--dry-run',        action='store_true',
                   help='Show what would be created without writing files')
    return p.parse_args()


def validate_share_folder(share_root, rel, what, hint_names):
    """Validate a MANDATORY share-relative folder at site creation time.

    Explicit, never guessed: the values are required flags, validated here
    once, recorded in the yaml — so no later script fails on a wrong repo
    name and no user has to hand-edit the site yaml. On error, list what
    actually exists in the share to make the fix obvious.
    """
    root = Path(share_root).expanduser()
    rel = rel.strip('/')
    if (root / rel).is_dir():
        return rel
    print(f'Error: {what} not found: {root / rel}', file=sys.stderr)
    print(f'  (typical names: {", ".join(hint_names)})', file=sys.stderr)
    entries = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if entries:
        print(f'  The share ({root}) contains: {", ".join(entries[:12])}', file=sys.stderr)
    else:
        print(f'  The share ({root}) is empty or missing — check the mount.', file=sys.stderr)
    sys.exit(1)


def derive_prefixes(o, ov):
    return {
        'switch_underlay':    f'{o}.128.0.0/16',
        'switch_loopbacks':   f'{o}.129.0.0/16',
        'dpu_fabric':         f'{o}.130.0.0/16',
        'dpu_loopbacks':      f'{o}.131.0.0/16',
        'internet_uplink':    f'{o}.132.0.0/30',
        'control_plane_link': f'{o}.132.1.0/31',
        'mat_underlay':       f'{o}.140.2.0/24',
        'service_vips':       f'{o}.133.1.0/27',
        'overlay':            f'{ov}.150.0.0/16',
        'admin_network':      f'{ov}.135.0.0/16',
    }


def rewrite(content, pfx, dc_name, site_name, args):
    o  = pfx['_underlay_octet']
    ov = pfx['_overlay_octet']

    replacements = [
        # Shared folder paths
        ('NICO_VM_FOLDER',    args.nico_vm_folder),
        ('NICO_MAC_FOLDER',   args.nico_mac_folder),
        ('NICO_REPO_FOLDER',  args._repo_folder),
        ('NICO_DEV_FOLDER',   args._dev_folder),
        # Registry
        ('192.168.64.1',      args.registry_host),
        ('port: 5000',        f'port: {args.registry_port}'),
        # Names
        ('dc_name: dev',      f'dc_name: {dc_name}'),
        ('sitename: dev',     f'sitename: {site_name}'),
        ('domain: dev.example.com', f'domain: {site_name}.example.com'),
        # IP prefixes
        ('7.128.0.0/16',      pfx['switch_underlay']),
        ('7.129.0.0/16',      pfx['switch_loopbacks']),
        ('7.130.0.0/16',      pfx['dpu_fabric']),
        ('7.131.0.0/16',      pfx['dpu_loopbacks']),
        ('7.132.0.0/30',      pfx['internet_uplink']),
        ('7.132.1.0/31',      pfx['control_plane_link']),
        ('7.140.2.0/24',      pfx['mat_underlay']),
        ('7.140.2.',          f'{o}.140.2.'),   # mat gateway etc.
        ('7.133.1.0/27',      pfx['service_vips']),
        ('7.133.1.',          f'{o}.133.1.'),
        ('7.132.0.',          f'{o}.132.0.'),
        ('7.132.1.',          f'{o}.132.1.'),
        ('8.135.0.0/16',      pfx['admin_network']),
        ('"8.135.0.1"',       f'"{ov}.135.0.1"'),
        ('8.150.0.0/16',      pfx['overlay']),
    ]

    for old, new in replacements:
        content = content.replace(old, new)
    return content


def main():
    args    = parse_args()
    script_dir = Path(__file__).parent
    template   = script_dir / 'nico-dev.yaml'

    if not template.exists():
        print(f'Error: template not found: {template}', file=sys.stderr)
        sys.exit(1)

    o  = args.underlay
    ov = args.overlay

    if o == ov:
        print('Error: --underlay and --overlay must be different octets', file=sys.stderr)
        sys.exit(1)

    # Name rules — fail fast with a clear message, never at deploy time.
    # dc-name lands in Linux bridge names (15-char interface limit: br-<dc>-internet
    # = 3 + len + 9 → max 3 chars). Both names land in hostnames
    # (nico-api.<dc>-<site>), filenames, and helm values → lowercase alphanumeric,
    # starting with a letter. site-name capped at 8 for readable derived names.
    import re as _re
    if not _re.fullmatch(r'[a-z][a-z0-9]{0,2}', args.dc_name):
        print(f'Error: --dc-name "{args.dc_name}" is invalid.', file=sys.stderr)
        print('  Rules: 1-3 chars, lowercase letters/digits, starts with a letter '
              '(e.g. dc1).', file=sys.stderr)
        print('  Why 3: it lands in bridge names — "br-<dc>-internet" must fit the '
              '15-char Linux interface limit.', file=sys.stderr)
        sys.exit(1)
    if not _re.fullmatch(r'[a-z][a-z0-9]{0,7}', args.site_name):
        print(f'Error: --site-name "{args.site_name}" is invalid.', file=sys.stderr)
        print('  Rules: 1-8 chars, lowercase letters/digits, starts with a letter '
              '(e.g. dev1, lab2).', file=sys.stderr)
        print('  It lands in hostnames (nico-api.<dc>-<site>), the kubeconfig '
              'filename, and helm values.', file=sys.stderr)
        sys.exit(1)

    pfx = derive_prefixes(o, ov)
    pfx['_underlay_octet'] = o
    pfx['_overlay_octet']  = ov

    # Validate the mandatory repo + nico-dev locations (VM-side view) —
    # recorded in the yaml, hard error here rather than four scripts later.
    args._repo_folder = validate_share_folder(
        args.nico_vm_folder, args.nico_repo_folder,
        'nico repo', ['infra-controller-core', 'infra-controller'])
    args._dev_folder = validate_share_folder(
        args.nico_vm_folder, args.nico_dev_folder,
        'nico-dev scripts folder', ['infra-controller-core/tools/nico-dev', 'claude-notes/nico-dev', 'nico-dev'])

    folder   = Path(args.folder).expanduser()
    out_file = folder / f'{args.site_name}.yaml'

    print('nico-dev — Create Site')
    print(f'  dc_name        : {args.dc_name}')
    print(f'  site_name      : {args.site_name}')
    print(f'  folder         : {folder}')
    print(f'  underlay       : {o}  → {o}.x.x.x')
    print(f'  overlay        : {ov}  → {ov}.x.x.x')
    print(f'  nico_vm_folder : {args.nico_vm_folder}')
    print(f'  nico_mac_folder: {args.nico_mac_folder}')
    print(f'  nico repo      : <share>/{args._repo_folder}')
    print(f'  nico-dev       : <share>/{args._dev_folder}')
    print(f'  registry       : {args.registry_host}:{args.registry_port}')
    print()
    print('Derived prefixes:')
    for k, v in pfx.items():
        if not k.startswith('_'):
            print(f'  {k:<22}: {v}')

    if args.dry_run:
        print(f'\n[dry-run] would write: {out_file}')
        return

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        subprocess.run(['sudo', 'mkdir', '-p', str(folder)], check=True)
        real_user = os.environ.get('SUDO_USER') or os.environ.get('USER', '')
        if real_user:
            subprocess.run(['sudo', 'chown', '-R', real_user, str(folder)], check=True)
    content = template.read_text()
    content = rewrite(content, pfx, args.dc_name, args.site_name, args)
    out_file.write_text(content)
    print(f'\nCreated: {out_file}')
    print()
    # All hint paths are derived from the recorded folders — never bare script
    # names (which only work from a particular cwd) or hardcoded locations.
    vm_dev  = Path(args.nico_vm_folder).expanduser() / args._dev_folder
    mac_dev = Path(args.nico_mac_folder) / args._dev_folder
    # Mac-side site path: re-root --folder from the VM share root onto the Mac
    # share root (falls back to <mac>/sites/<site> if outside the share).
    mac_folder = Path(args.nico_mac_folder).expanduser()
    try:
        rel      = folder.resolve().relative_to(Path(args.nico_vm_folder).expanduser().resolve())
        mac_site = mac_folder / rel
    except ValueError:
        mac_site = mac_folder / 'sites' / args.site_name

    print('Next steps (on VM):')
    print(f'  sudo python3 {vm_dev}/deploy-dev-fabric.py {folder}')
    print(f'  sudo python3 {vm_dev}/deploy-dev-cp.py {folder}')
    print()
    print('Then on Mac (pick a meaningful tag, e.g. v2.0.0):')
    print(f'  python3 {mac_dev}/build-dev-nico.py {mac_site} --tag <tag>')
    print(f'  python3 {mac_dev}/deploy-dev-nico.py {mac_site} --tag <tag>')


if __name__ == '__main__':
    main()
