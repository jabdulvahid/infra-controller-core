#!/usr/bin/env python3
"""
nico-dev — Tear down a nico-dev VM on a LINUX host (the one-command delete).

  dev-down.py --name nico-dc1-dev1          # by VM name
  dev-down.py --config devup-mysite.yaml    # by the same config dev-up used
  dev-down.py --name X --remove-infra       # also drop nico-nat + pool
                                            #   (refused while other nico VMs exist)

Removes exactly what build-nico-dev-vm created for that VM: the libvirt
domain, its volumes in the nico-dev pool, its host route, its ledger entry.
The site folder and worktree are YOURS — never deleted (printed as hints).
Shared infrastructure (network nico-nat, pool nico-dev) is left alone
unless --remove-infra, and then only when no nico-dev VMs remain.
"""

import argparse
import subprocess
import sys
from pathlib import Path

CONN = 'qemu:///system'
NET_NAME = 'nico-nat'
POOL = 'nico-dev'
LEDGER_DIR = Path.home() / '.nico-dev' / 'vms'


def virsh(*a, check=False):
    r = subprocess.run(['virsh', '-c', CONN, *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f'Error: virsh {a[0]} failed: {r.stderr.strip()}')
    return r


def read_ledger(name):
    p = LEDGER_DIR / f'{name}.yaml'
    if not p.exists():
        return {}
    return {k.strip(): v.strip() for k, _, v in
            (l.partition(':') for l in p.read_text().splitlines()
             if l and not l.startswith('#') and ':' in l)}


def main():
    p = argparse.ArgumentParser(description='Tear down a nico-dev VM (Linux host)',
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument('--name', help='VM name')
    p.add_argument('--config', help='devup yaml (name: or dc/site → nico-<dc>-<site>)')
    p.add_argument('--remove-infra', action='store_true',
                   help=f'also remove {NET_NAME} and pool {POOL} if no nico VMs remain')
    p.add_argument('--yes', action='store_true', help='no confirmation prompt')
    args = p.parse_args()

    name = args.name
    if args.config:
        import yaml
        cfg = yaml.safe_load(Path(args.config).expanduser().read_text()) or {}
        name = name or cfg.get('name') or f"nico-{cfg.get('dc', 'dc1')}-{cfg.get('site', 'dev')}"
    if not name:
        p.error('--name or --config required')

    ledger = read_ledger(name)
    exists = virsh('dominfo', name).returncode == 0
    print(f'nico-dev — tear down {name}')
    print(f'  domain : {"present" if exists else "not found"}')
    print(f'  ledger : {LEDGER_DIR / (name + ".yaml")} '
          f'{"✓" if ledger else "(none — falling back to libvirt state)"}')
    vols = [v.strip() for v in ledger.get('volumes', '').strip('[]').split(',') if v.strip()] \
        or [f'{name}-root.qcow2', f'{name}-seed.iso']
    print(f'  volumes: {", ".join(vols)}')
    if ledger.get('ip'):
        print(f'  ip     : {ledger["ip"]} (its VIP route will be removed if present)')

    if not args.yes:
        ans = input('Proceed? [y/N]: ').strip().lower()
        if ans != 'y':
            raise SystemExit('aborted')

    if exists:
        virsh('destroy', name)
        virsh('undefine', name, '--remove-all-storage', check=True)
        print(f'  domain {name} destroyed + undefined, storage removed ✓')
    for v in vols:                                   # belt and braces
        if virsh('vol-info', '--pool', POOL, v).returncode == 0:
            virsh('vol-delete', '--pool', POOL, v)
            print(f'  volume {v} deleted ✓')

    # host route(s) that point at this VM's IP
    if ledger.get('ip'):
        routes = subprocess.run(['ip', 'route', 'show'], capture_output=True,
                                text=True).stdout.splitlines()
        for line in routes:
            if f'via {ledger["ip"]}' in line:
                net = line.split()[0]
                subprocess.run(['sudo', 'ip', 'route', 'del', net])
                print(f'  route {net} via {ledger["ip"]} removed ✓')
        subprocess.run(['ssh-keygen', '-R', ledger['ip']], capture_output=True)

    (LEDGER_DIR / f'{name}.yaml').unlink(missing_ok=True)
    print('  ledger entry removed ✓')

    if args.remove_infra:
        others = [l for l in virsh('list', '--all', '--name').stdout.split()
                  if l.startswith('nico-')]
        if others:
            print(f'  --remove-infra: refused, nico VMs still exist: {", ".join(others)}')
        else:
            for a in (('net-destroy', NET_NAME), ('net-undefine', NET_NAME),
                      ('pool-destroy', POOL), ('pool-undefine', POOL)):
                virsh(*a)
            print(f'  shared infra removed: network {NET_NAME}, pool {POOL} ✓')

    print(f'''
Done. Not touched (yours to keep or remove):
  share/site folder{"  " + ledger["share"] if ledger.get("share") else ""}
  git worktree (git worktree remove <path>)''')


if __name__ == '__main__':
    main()
