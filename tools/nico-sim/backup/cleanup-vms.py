#!/usr/bin/env python3
"""
DC Simulation — Control-plane VM cleanup

Lists and destroys VMs, disk images, and OOB libvirt network created by
deploy-vms.sh. Prompts before each destructive action unless --force is given.

Usage:
  python3 cleanup-vms.py nico-sim.yaml              # interactive prompts
  python3 cleanup-vms.py nico-sim.yaml --dry-run    # show what would be removed
  python3 cleanup-vms.py nico-sim.yaml --force      # no prompts, destroy everything
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def virsh(*args):
    # Always connect to qemu:///system so we operate on the same VM pool
    # as sudo ./deploy-nodes.sh (which uses the system libvirt, not the
    # user session). Without this, virsh without sudo talks to a different
    # pool and VMs appear to not exist even though they do.
    return run(['virsh', '-c', 'qemu:///system'] + list(args))


def vm_exists(name):
    r = virsh('dominfo', name)
    return r.returncode == 0


def vm_is_running(name):
    r = virsh('domstate', name)
    return r.returncode == 0 and r.stdout.strip() == 'running'


def net_exists(name):
    r = virsh('net-info', name)
    return r.returncode == 0


def confirm(prompt, force):
    """Ask for confirmation. Returns True if force or user says yes."""
    if force:
        print(f'  {prompt} [forced]')
        return True
    ans = input(f'  {prompt} [y/N] ').strip().lower()
    return ans == 'y'


def green(s): return f'\033[32m{s}\033[0m'
def red(s):   return f'\033[31m{s}\033[0m'
def bold(s):  return f'\033[1m{s}\033[0m'
def warn(s):  return f'\033[33m⚠ {s}\033[0m'


# ── Config loading ────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _name_prefix(sim):
    """Return {dc_name}-{sitename}- prefix used in all VM names."""
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    return f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'


def get_vm_names(sim):
    cp      = sim['control_plane']
    vp      = cp['vm_prefix']
    dp      = cp.get('dpu_prefix', f'{vp}-dpu')
    n       = cp['num_vms']
    pfx     = _name_prefix(sim)
    dc_name = sim['fabric'].get('dc_name', 'nico-sim')

    # Registry VM
    reg_vp   = sim.get('nico_container_registry', sim.get('utility', {})).get('vm_prefix', 'registry')
    vms = [f'{pfx}{reg_vp}']

    # CP DPU stand-ins and host VMs
    for i in range(1, n + 1):
        vms.append(f'{pfx}{dp}-{i}')   # DPU stand-in
        vms.append(f'{pfx}{vp}-{i}')   # CP host VM

    # MH VMs
    mh = sim.get('managed_hosts', {})
    mh_vp = mh.get('vm_prefix', 'mh')
    num_vms_cfg = mh.get('num_vms', {})
    if isinstance(num_vms_cfg, dict):
        total_mh = sum(num_vms_cfg.values())
        for i in range(1, total_mh + 1):
            vms.append(f'{pfx}{mh_vp}-{i}')

    return vms


def get_host_bridges(sim):
    """Return host-side bridges — none in new schema (DPU uses fabric bridge directly)."""
    return []


def get_disk_paths(sim, vm_names):
    cp = sim['control_plane']
    # os_image_dir lives in control_plane (cloud_init) or golden_image.control_plane (golden_image)
    img_dir = (cp.get('os_image_dir')
               or sim.get('golden_image', {}).get('control_plane', {}).get('os_image_dir')
               or '/var/lib/libvirt/images')
    paths   = []
    for name in vm_names:
        paths.append(Path(img_dir) / f'{name}.qcow2')
        paths.append(Path(img_dir) / f'{name}-seed.iso')
    return paths


def get_oob_networks(sim):
    networks = []
    # CP OOB network
    networks.append(sim['control_plane']['oob_network']['name'])
    # Registry VM OOB network
    reg = sim.get('nico_container_registry', sim.get('utility', {}))
    reg_oob = reg.get('oob_network', {}).get('name')
    if reg_oob:
        networks.append(reg_oob)
    # MH OOB network
    mh_oob = sim.get('managed_hosts', {}).get('oob_network', {}).get('name')
    if mh_oob:
        networks.append(mh_oob)
    return list(dict.fromkeys(networks))  # deduplicate, preserve order


# ── Inventory ─────────────────────────────────────────────────────────────────

def print_inventory(vm_names, disk_paths, oob_networks):
    print(bold('\n── What will be removed ──────────────────────────────────────'))

    print('\nVMs:')
    for name in vm_names:
        exists  = vm_exists(name)
        running = vm_is_running(name) if exists else False
        state   = red('RUNNING') if running else ('exists' if exists else 'not found')
        marker  = '✓' if exists else '·'
        print(f'  {marker} {name:20s}  {state}')

    print('\nDisk images:')
    for p in disk_paths:
        exists = p.exists()
        size   = f'  ({p.stat().st_size // 1024 // 1024} MB)' if exists else ''
        marker = '✓' if exists else '·'
        print(f'  {marker} {str(p):60s}{size}')

    print('\nOOB libvirt networks:')
    for net in oob_networks:
        exists = net_exists(net)
        marker = '✓' if exists else '·'
        print(f'  {marker} {net}')

    print()


# ── Cleanup actions ───────────────────────────────────────────────────────────

def destroy_vms(vm_names, dry_run, force):
    print(bold('\n── Step 1: Stop and undefine VMs ─────────────────────────────'))
    for name in vm_names:
        if not vm_exists(name):
            print(f'  · {name}: not found, skipping')
            continue

        running = vm_is_running(name)
        if running:
            print(f'\n  VM {bold(name)} is {red("RUNNING")}.')
            if not confirm(f'Force-stop and destroy {name}?', force):
                print(f'    Skipped {name}.')
                continue
        else:
            print(f'\n  VM {bold(name)} is shut off.')
            if not confirm(f'Undefine and remove {name}?', force):
                print(f'    Skipped {name}.')
                continue

        if dry_run:
            print(f'    [dry-run] would: virsh destroy {name}; virsh undefine {name} --remove-all-storage')
            continue

        if running:
            r = virsh('destroy', name)
            if r.returncode == 0:
                print(f'    Stopped {name}')
            else:
                print(f'    {warn("Failed to stop " + name + ": " + r.stderr.strip())}')

        r = virsh('undefine', name, '--remove-all-storage', '--nvram')
        if r.returncode != 0:
            # retry without --nvram if it fails (NVRAM may not exist)
            r = virsh('undefine', name, '--remove-all-storage')
        if r.returncode == 0:
            print(f'    Undefined and removed {name}')
        else:
            print(f'    {warn("Failed to undefine " + name + ": " + r.stderr.strip())}')


def remove_disks(disk_paths, dry_run, force):
    print(bold('\n── Step 2: Remove leftover disk images ───────────────────────'))
    remaining = [p for p in disk_paths if p.exists()]
    if not remaining:
        print('  No leftover disk images found.')
        return

    for p in remaining:
        size = f'{p.stat().st_size // 1024 // 1024} MB'
        print(f'\n  Found: {p}  ({size})')
        if not confirm(f'Delete {p.name}?', force):
            print(f'    Skipped.')
            continue
        if dry_run:
            print(f'    [dry-run] would: rm {p}')
            continue
        p.unlink()
        print(f'    Deleted {p.name}')


def remove_host_bridges(bridges, dry_run, force):
    if not bridges:
        return
    print(bold('\n── Step 2b: Remove host-side bridges (br-cp{N}-host) ────────'))
    for br in bridges:
        r = subprocess.run(['ip', 'link', 'show', br], capture_output=True)
        if r.returncode != 0:
            print(f'  · {br}: not found, skipping')
            continue
        print(f'\n  Bridge {bold(br)} exists.')
        if not confirm(f'Remove bridge {br}?', force):
            print(f'    Skipped.')
            continue
        if dry_run:
            print(f'    [dry-run] would: ip link delete {br} type bridge')
            continue
        subprocess.run(['ip', 'link', 'set', br, 'down'], capture_output=True)
        r = subprocess.run(['ip', 'link', 'delete', br, 'type', 'bridge'], capture_output=True)
        if r.returncode == 0:
            print(f'    Removed {br}')
        else:
            print(f'    {warn("Failed: " + r.stderr.decode().strip())}')


def remove_networks(oob_networks, dry_run, force):
    print(bold('\n── Step 3: Remove OOB libvirt networks ───────────────────────'))
    for net in oob_networks:
        if not net_exists(net):
            print(f'  · {net}: not found, skipping')
            continue
        print(f'\n  Network {bold(net)} exists.')
        if not confirm(f'Destroy and undefine network {net}?', force):
            print(f'    Skipped.')
            continue
        if dry_run:
            print(f'    [dry-run] would: virsh net-destroy {net}; virsh net-undefine {net}')
            continue
        virsh('net-destroy', net)
        r = virsh('net-undefine', net)
        if r.returncode == 0:
            print(f'    Removed network {net}')
        else:
            print(f'    {warn("Failed: " + r.stderr.strip())}')


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Clean up DC simulation VMs and associated resources',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--force',   action='store_true',
                   help='Skip all confirmation prompts — destroy everything immediately')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be removed without actually removing anything')
    return p.parse_args()


def resolve_yaml(arg):
    """Accept site folder or yaml file path."""
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac')]
        if not yamls:
            print(f'Error: no site yaml found in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0])
    return str(p)


def main():
    args = parse_args()
    sim_yaml = resolve_yaml(args.site)

    if not Path(sim_yaml).exists():
        print(f'Error: {sim_yaml} not found', file=sys.stderr)
        sys.exit(1)

    sim          = load_sim(sim_yaml)
    vm_names     = get_vm_names(sim)
    disk_paths   = get_disk_paths(sim, vm_names)
    oob_networks = get_oob_networks(sim)
    host_bridges = get_host_bridges(sim)

    mode = red('DRY RUN — no changes will be made') if args.dry_run \
           else (red('FORCE MODE — no confirmation prompts') if args.force else 'interactive')

    print(bold(f'DC Simulation — VM Cleanup  [{mode}]'))
    print(f'  config: {sim_yaml}')

    print_inventory(vm_names, disk_paths, oob_networks)

    if not args.dry_run and not args.force:
        print(bold('This will permanently delete VMs and data. Proceed carefully.\n'))

    destroy_vms(vm_names, args.dry_run, args.force)
    remove_disks(disk_paths, args.dry_run, args.force)
    remove_host_bridges(host_bridges, args.dry_run, args.force)
    remove_networks(oob_networks, args.dry_run, args.force)

    print(bold('\n── Done ──────────────────────────────────────────────────────'))
    if args.dry_run:
        print('  Dry run complete — nothing was changed.')
    else:
        print('  Cleanup complete. Re-run generate-nodes.py + deploy-nodes.sh to recreate.')


if __name__ == '__main__':
    main()
