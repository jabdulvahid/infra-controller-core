#!/usr/bin/env python3
"""
nico-sim — Destroy a site's ContainerLab fabric

Conservatively tears down the fabric for a given site folder, cleaning up:
  1. ContainerLab topology (clab destroy --cleanup)
  2. Stale veth interfaces left by partial deploys
  3. Site-specific Linux bridges
  4. Host routes added by deploy.sh
  5. DNS server (dnsmasq) started by deploy.sh
  6. ContainerLab mgmt Docker network

Does NOT touch:
  - KVM virtual machines (use cleanup-vms.py for that)
  - Nico k8s deployment (use deploy-nico-system.py --destroy)
  - The site YAML files or generated fabric/ directory
  - The dc-sim or any other site's resources
  - iptables MASQUERADE rules (shared with other sites/tools)

Usage:
  ./destroy-fabric.py ~/sites/ytl
  ./destroy-fabric.py ~/sites/ytl --dry-run
"""

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


def run(cmd, dry_run=False, capture=True):
    if dry_run:
        print(f'  [dry-run] {" ".join(str(c) for c in cmd)}')
        return ''
    r = subprocess.run(cmd, capture_output=capture, text=True)
    return r.stdout.strip() if capture else ''


def run_shell(cmd, dry_run=False):
    if dry_run:
        print(f'  [dry-run] {cmd}')
        return
    subprocess.run(cmd, shell=True)


def parse_args():
    p = argparse.ArgumentParser(
        description='Destroy a nico-sim site fabric — conservative cleanup',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('site_folder', help='Site folder (e.g. ~/sites/ytl)')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be done without making changes')
    return p.parse_args()


def find_site_yaml(site_folder):
    """Find the site yaml in the folder (e.g. ytl.yaml)."""
    folder = Path(site_folder).expanduser()
    yamls = [f for f in folder.glob('*.yaml')
             if f.stem not in ('nico-sim', 'nico-sim-mac') and not f.stem.endswith('-mac')]
    if not yamls:
        print(f'Error: no site yaml found in {folder}', file=sys.stderr)
        sys.exit(1)
    if len(yamls) > 1:
        print(f'Error: multiple yamls in {folder}: {yamls}', file=sys.stderr)
        sys.exit(1)
    return yamls[0]


def load_dc_name(yaml_path):
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    return data.get('fabric', {}).get('dc_name',
           data.get('fabric', {}).get('lab_name', yaml_path.stem))


def load_site_nick(yaml_path):
    """Return site_nick_name (used in OOB virsh network names like ytl-cp)."""
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    sitename = (data.get('nico-system', {})
                    .get('helm-values', {})
                    .get('sitename', yaml_path.stem))
    # site_nick_name is stored in nico-system.helm-values
    nick = (data.get('nico-system', {})
                .get('helm-values', {})
                .get('site_nick_name'))
    return nick or sitename[:4]


def main():
    args = parse_args()
    dry = args.dry_run

    site_folder = Path(args.site_folder).expanduser()
    if not site_folder.is_dir():
        print(f'Error: folder not found: {site_folder}', file=sys.stderr)
        sys.exit(1)

    site_yaml  = find_site_yaml(site_folder)
    dc_name    = load_dc_name(site_yaml)
    site_nick  = load_site_nick(site_yaml)
    topo_file  = site_folder / 'fabric' / 'topo.clab.yml'

    print(f'nico-sim — Destroy Fabric')
    print(f'  site     : {dc_name}')
    print(f'  folder   : {site_folder}')
    print(f'  topology : {topo_file}')
    if dry:
        print(f'  DRY RUN  : no changes will be made')
    print()

    # ── Step 1: ContainerLab destroy ─────────────────────────────────────────
    print('Step 1: ContainerLab destroy')
    if topo_file.exists():
        run(['sudo', 'clab', 'destroy', '-t', str(topo_file), '--cleanup'],
            dry_run=dry, capture=False)
    else:
        print(f'  topology file not found ({topo_file}) — skipping clab destroy')
    print()

    # ── Step 2: Stale veth interfaces ────────────────────────────────────────
    print('Step 2: Removing stale veth interfaces')
    # Known ContainerLab endpoint names it creates on bridges
    stale_ifaces = [
        f'eth-cp{i}' for i in range(1, 10)
    ] + [
        f'eth-mh-{r}' for r in range(1, 10)
    ] + ['eth-uplink', 'eth-registry', 'eth-helper', 'eth-mat']
    for iface in stale_ifaces:
        result = run(['ip', 'link', 'show', iface], dry_run=False)
        if result:  # interface exists
            print(f'  deleting orphaned veth: {iface}')
            run(['sudo', 'ip', 'link', 'delete', iface], dry_run=dry)
    # Also scan bridge brif directories
    site_bridges = (
        [f'br-{dc_name}-cp{i}' for i in range(1, 10)] +
        [f'br-{dc_name}-mh{r}' for r in range(1, 10)] +
        [f'br-{dc_name}-internet', f'br-{dc_name}-registry',
         f'br-{dc_name}-helper']
    )
    for br in site_bridges:
        brif_dir = Path(f'/sys/class/net/{br}/brif')
        if brif_dir.is_dir():
            for iface_path in brif_dir.iterdir():
                iface = iface_path.name
                print(f'  deleting bridge slave: {iface} (from {br})')
                run(['sudo', 'ip', 'link', 'delete', iface], dry_run=dry)
    print()

    # ── Step 3: Site-specific Linux bridges ──────────────────────────────────
    print('Step 3: Removing site Linux bridges')
    existing_bridges = run(['ip', 'link', 'show', 'type', 'bridge'], dry_run=False)
    for br in site_bridges:
        if br in existing_bridges:
            print(f'  deleting bridge: {br}')
            run(['sudo', 'ip', 'link', 'set', br, 'down'], dry_run=dry)
            run(['sudo', 'ip', 'link', 'delete', br, 'type', 'bridge'], dry_run=dry)
        else:
            print(f'  {br}: not found — skipping')
    print()

    # ── Step 4: Host routes ───────────────────────────────────────────────────
    print('Step 4: Removing host routes added by deploy.sh')
    route_table = run(['ip', 'route', 'show'], dry_run=False)
    removed = 0
    for line in route_table.splitlines():
        # Match routes via br-{dc_name}-internet or routes in 9.x.x.x space
        # We conservatively only remove routes whose gateway references our bridge
        if f'br-{dc_name}-internet' in line or f'br-{dc_name}' in line:
            prefix = line.split()[0]
            print(f'  removing route: {prefix}')
            run(['sudo', 'ip', 'route', 'del', prefix], dry_run=dry)
            removed += 1
    if removed == 0:
        print('  no site-specific routes found')
    print()

    # ── Step 5: DNS server ────────────────────────────────────────────────────
    print('Step 5: Stopping dnsmasq DNS server')
    pid_file = f'/var/run/{dc_name}-dns.pid'
    pid_result = run(['cat', pid_file], dry_run=False)
    if pid_result:
        print(f'  killing dnsmasq pid {pid_result}')
        run(['sudo', 'kill', pid_result], dry_run=dry)
        run(['sudo', 'rm', '-f', pid_file], dry_run=dry)
    else:
        print(f'  pid file {pid_file} not found — skipping')
    print()

    # ── Step 6: Docker mgmt network ──────────────────────────────────────────
    # ContainerLab creates a Docker network {dc_name}-mgmt at deploy time.
    # Always attempt removal — don't rely on listing (format may vary).
    # Disconnect any remaining containers first so rm can't fail on active endpoints.
    print('Step 6: Removing ContainerLab mgmt Docker network')
    mgmt_net = f'{dc_name}-mgmt'
    if not dry:
        import subprocess as _sp
        # Disconnect ALL clab-{dc_name}-* containers still attached.
        # clab destroy in step 1 may have failed if topo file was missing,
        # leaving containers running and blocking network removal.
        containers_out = _sp.run(
            ['docker', 'network', 'inspect', mgmt_net,
             '--format', '{{range .Containers}}{{.Name}} {{end}}'],
            capture_output=True, text=True).stdout.strip()
        for cname in containers_out.split():
            _sp.run(['docker', 'network', 'disconnect', '--force', mgmt_net, cname],
                    capture_output=True)
            print(f'  disconnected: {cname}')
        r = _sp.run(['docker', 'network', 'rm', mgmt_net],
                    capture_output=True, text=True)
        if r.returncode == 0:
            print(f'  removed Docker network: {mgmt_net}')
        elif 'No such network' in r.stderr or 'not found' in r.stderr.lower():
            print(f'  {mgmt_net}: not found — skipping')
        else:
            print(f'  {mgmt_net}: {r.stderr.strip() or "unknown error"}')
    else:
        print(f'  [dry-run] docker network rm {mgmt_net}')
    print()

    # ── Step 7: Virsh OOB networks left by failed VM deploys ─────────────────
    # generate-nodes.py creates two families of virsh networks:
    #   {dc_name}-*   e.g. dc1-mgmt  (ContainerLab mgmt, though Docker handles this)
    #   {site_nick}-* e.g. ytl-cp, ytl-mh, ytl-reg  (OOB networks for VMs)
    # Both must be removed to allow a clean redeploy.
    print('Step 7: Removing leftover virsh OOB networks')
    try:
        virsh_nets = run(['virsh', 'net-list', '--all', '--name'], dry_run=False)
        removed = 0
        for net in virsh_nets.splitlines():
            net = net.strip()
            if net and (net.startswith(f'{dc_name}-') or net.startswith(f'{site_nick}-')):
                print(f'  removing virsh network: {net}')
                run(['virsh', 'net-destroy',  net], dry_run=dry)
                run(['virsh', 'net-undefine', net], dry_run=dry)
                removed += 1
        if removed == 0:
            print(f'  no {dc_name}-* or {site_nick}-* virsh networks found — skipping')
    except FileNotFoundError:
        print('  virsh not found — skipping')
    print()

    print(f'Fabric destroy complete for site: {dc_name}')
    if dry:
        print('(dry run — no changes were made)')


if __name__ == '__main__':
    main()
