#!/usr/bin/env python3
"""
DC Simulation — SSH into a VM by name

Looks up the VM's IP address (OOB from virsh DHCP leases, or fabric from
nico-sim.yaml allocation) and opens an SSH session.

Usage:
  python3 ssh-vm.py nico-sim.yaml --list                    # list all VMs and IPs
  python3 ssh-vm.py nico-sim.yaml cp-1 --via oob            # SSH via OOB interface
  python3 ssh-vm.py nico-sim.yaml cp-1 --via fabric         # SSH via fabric interface
  python3 ssh-vm.py nico-sim.yaml cp-1 --via oob --user root
"""

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── Config loading ────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def slash31_pairs(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for subnet in net.subnets(new_prefix=31):
        hosts = list(subnet.hosts())
        yield str(hosts[0]), str(hosts[1])


def slash32_hosts(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for host in net.hosts():
        yield str(host)


def build_vm_table(sim):
    """
    Build a list of VM info dicts from nico-sim.yaml.
    Includes DPU stand-in VMs (if dpu_per_vm > 0) and CP host VMs.

    With DPU architecture:
      - DPU stand-in VMs hold the fabric IPs (from dpu_fabric prefix)
      - CP host VMs hold internal IPs (from control_plane_prefix)
    """
    cp     = sim['control_plane']
    prefix = cp['vm_prefix']
    dp     = cp.get('dpu_prefix', f'{prefix}-dpu')
    n      = cp['num_vms']
    fab    = sim['fabric']
    pfx    = fab['prefixes']
    sw_base  = fab.get('switch_asn_base', fab.get('asn_base', 65000))
    dpu_base = fab.get('dpu_asn_base', fab.get('asn_base', 65000) + 20)

    dpu_per_vm = cp.get('dpu_per_vm', 0)

    dpu_pool = slash31_pairs(pfx.get('dpu_fabric', pfx.get('server_underlay', '7.131.0.0/24')))
    lb_pool  = slash32_hosts(cp.get('dpu_loopbacks', pfx.get('dpu_loopbacks', '7.130.2.0/24')))

    # control_plane_prefix is only needed when DPU stand-ins are configured
    cp_prefix = cp.get('control_plane_prefix', pfx.get('control_plane_prefix'))
    if dpu_per_vm > 0 and cp_prefix:
        cp_pool = slash31_pairs(cp_prefix)
    else:
        cp_pool = None

    vms = []
    for i in range(1, n + 1):
        leaf_ip, dpu_ip = next(dpu_pool)
        loopback        = next(lb_pool)

        if dpu_per_vm > 0 and cp_pool is not None:
            # DPU stand-in VM — loopback belongs here (BGP router-ID)
            dpu_internal_ip, host_internal_ip = next(cp_pool)
            vms.append({
                'name':        f'{dp}-{i}',
                'fabric_ip':   dpu_ip,
                'fabric_peer': leaf_ip,
                'loopback':    loopback,            # DPU's loopback (BGP router-ID)
                'oob_network': cp['oob_network']['name'],
                'vm_asn':      dpu_base + i,
                'type':        'dpu-stand-in',
            })
            # CP host VM — no FRR, no loopback
            vms.append({
                'name':        f'{prefix}-{i}',
                'fabric_ip':   host_internal_ip,   # accessible via DPU routing
                'fabric_peer': dpu_internal_ip,    # DPU's internal IP (gateway)
                'loopback':    '–',                # host VM has no BGP loopback
                'oob_network': cp['oob_network']['name'],
                'vm_asn':      0,                  # host VM has no BGP ASN
                'type':        'control-plane',
            })
        else:
            # Legacy: direct fabric connection (no DPU)
            vms.append({
                'name':        f'{prefix}-{i}',
                'fabric_ip':   dpu_ip,
                'fabric_peer': leaf_ip,
                'loopback':    loopback,
                'oob_network': cp['oob_network']['name'],
                'vm_asn':      base + 20 + i,
                'type':        'control-plane',
            })

    # Helper VM (always direct fabric, no DPU)
    if 'helper' in sim:
        helper = sim['helper']
        h_pfx  = helper.get('vm_prefix', 'helper')
        leaf_ip, vm_ip = next(dpu_pool)
        next(lb_pool)  # consume loopback slot
        vms.append({
            'name':        h_pfx,
            'fabric_ip':   vm_ip,
            'fabric_peer': leaf_ip,
            'loopback':    '–',
            'oob_network': helper['oob_network']['name'],
            'vm_asn':      dpu_base + n + 1,   # helper: dpu_base + num_cp_vms + 1
            'type':        'helper',
        })

    return vms


def find_vm(vms, name):
    for vm in vms:
        if vm['name'] == name:
            return vm
    return None


# ── OOB IP lookup ─────────────────────────────────────────────────────────────

def get_oob_ip(vm_name, oob_network, mac=None):
    """
    Look up the OOB (DHCP) IP of a VM. Tries multiple methods because
    libvirt DHCP leases expire (1h default, now 7 days after fix).
    """
    r = subprocess.run(['virsh', 'net-dhcp-leases', oob_network],
                       capture_output=True, text=True)
    leases = r.stdout if r.returncode == 0 else ''

    # Method 1: hostname match in DHCP leases
    for line in leases.splitlines():
        if vm_name.lower() in line.lower():
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)/\d+', line)
            if m:
                return m.group(1)

    # Method 2: MAC match in DHCP leases
    if mac:
        for line in leases.splitlines():
            if mac.lower() in line.lower():
                m = re.search(r'(\d+\.\d+\.\d+\.\d+)/\d+', line)
                if m:
                    return m.group(1)

    # Method 3: virsh domifaddr --source lease
    r = subprocess.run(['virsh', 'domifaddr', vm_name, '--source', 'lease'],
                       capture_output=True, text=True)
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)/\d+', line)
            if m:
                ip = m.group(1)
                if not ip.startswith('127.'):
                    return ip

    # Method 4: virsh domifaddr without source (ARP table)
    r = subprocess.run(['virsh', 'domifaddr', vm_name],
                       capture_output=True, text=True)
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)/\d+', line)
            if m:
                ip = m.group(1)
                if not ip.startswith('127.') and not ip.startswith('169.'):
                    return ip

    return None


# ── VM state ──────────────────────────────────────────────────────────────────

def vm_state(name):
    r = subprocess.run(['virsh', 'domstate', name], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.strip()
    return 'not found'


# ── List VMs ──────────────────────────────────────────────────────────────────

def bold(s):  return f'\033[1m{s}\033[0m'
def green(s): return f'\033[32m{s}\033[0m'
def red(s):   return f'\033[31m{s}\033[0m'
def yellow(s):return f'\033[33m{s}\033[0m'
def dim(s):   return f'\033[2m{s}\033[0m'


def list_vms(vms):
    print(bold(f'\n{"NAME":<12} {"TYPE":<16} {"STATE":<12} {"OOB IP":<18} '
               f'{"FABRIC IP":<16} {"LOOPBACK":<14} {"ASN"}'))
    print('─' * 100)

    for vm in vms:
        state    = vm_state(vm['name'])
        state_c  = green(state) if state == 'running' else \
                   yellow(state) if state == 'shut off' else red(state)
        oob_ip   = get_oob_ip(vm['name'], vm['oob_network']) or dim('DHCP/unknown')
        fabric   = vm['fabric_ip'] + '/31'
        loopback = vm['loopback'] + '/32'

        print(f'{bold(vm["name"]):<21} {vm["type"]:<16} {state_c:<21} '
              f'{str(oob_ip):<18} {fabric:<16} {loopback:<14} {vm["vm_asn"]}')

    print()
    print('OOB network: connect via serial console or from host only (isolated)')
    print('Fabric:      reachable via the ContainerLab fabric (BGP peer)')
    print('Loopback:    BGP router-ID, advertised into fabric')
    print()
    print(bold('SSH examples:'))
    if vms:
        name = vms[0]['name']
        print(f'  python3 ssh-vm.py nico-sim.yaml {name} --via oob')
        print(f'  python3 ssh-vm.py nico-sim.yaml {name} --via fabric')


# ── SSH ───────────────────────────────────────────────────────────────────────

def ssh_to(ip, user, extra_args=None):
    """Replace current process with SSH session."""
    cmd = [
        'ssh',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'LogLevel=ERROR',
        f'{user}@{ip}',
    ]
    if extra_args:
        cmd += extra_args
    print(f'  Connecting: {" ".join(cmd)}')
    os.execvp('ssh', cmd)  # replaces this process — no return


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='SSH into a DC simulation VM by name',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('sim_yaml',
                   help='Simulation definition file (nico-sim.yaml)')
    p.add_argument('vm_name', nargs='?',
                   help='VM name to SSH into (e.g. cp-1, cp-2, helper)')
    p.add_argument('--via', choices=['oob', 'fabric'],
                   help='Interface to SSH through: oob (DHCP) or fabric (static)')
    p.add_argument('--user', default='ubuntu',
                   help='SSH username (Ubuntu cloud images default to ubuntu)')
    p.add_argument('--list', '-l', action='store_true',
                   help='List all VMs with their names, state, and IPs')
    return p.parse_args()


def main():
    args = parse_args()

    if not Path(args.sim_yaml).exists():
        print(f'Error: {args.sim_yaml} not found', file=sys.stderr)
        sys.exit(1)

    sim = load_sim(args.sim_yaml)
    vms = build_vm_table(sim)

    # ── List mode ────────────────────────────────────────────────────────────
    if args.list:
        print(bold(f'DC Simulation VMs  [{args.sim_yaml}]'))
        list_vms(vms)
        sys.exit(0)

    # ── SSH mode ─────────────────────────────────────────────────────────────
    if not args.vm_name:
        print('Error: specify a VM name or use --list', file=sys.stderr)
        sys.exit(1)

    if not args.via:
        print('Error: --via is required (oob or fabric)', file=sys.stderr)
        sys.exit(1)

    vm = find_vm(vms, args.vm_name)
    if not vm:
        names = ', '.join(v['name'] for v in vms)
        print(f'Error: VM "{args.vm_name}" not found in nico-sim.yaml', file=sys.stderr)
        print(f'  Known VMs: {names}', file=sys.stderr)
        sys.exit(1)

    state = vm_state(vm['name'])
    if state != 'running':
        print(f'Warning: VM {vm["name"]} state is "{state}" — SSH may fail')

    if args.via == 'oob':
        ip = get_oob_ip(vm['name'], vm['oob_network'], mac=vm.get('oob_mac'))
        if not ip:
            print(f'Error: could not determine OOB IP for {vm["name"]}', file=sys.stderr)
            print(f'  The VM may not be running or cloud-init may not have finished.',
                  file=sys.stderr)
            print(f'  Try: virsh net-dhcp-leases {vm["oob_network"]}', file=sys.stderr)
            sys.exit(1)
        print(f'  VM:      {vm["name"]}')
        print(f'  Via:     OOB network ({vm["oob_network"]})')
        print(f'  OOB IP:  {ip}')
        print(f'  User:    {args.user}')
        ssh_to(ip, args.user)

    elif args.via == 'fabric':
        ip = vm['fabric_ip']
        pfx = sim['fabric']['prefixes']
        net = pfx.get('dpu_fabric', pfx.get('server_underlay', '7.131.0.0/24'))
        print(f'  VM:         {vm["name"]}  ({vm["type"]})')
        print(f'  Via:        fabric ({net})')
        print(f'  Fabric IP:  {ip}  (peer/gateway: {vm["fabric_peer"]})')
        if vm['loopback'] != '–':
            print(f'  Loopback:   {vm["loopback"]}/32')
        print(f'  User:       {args.user}')
        ssh_to(ip, args.user)


if __name__ == '__main__':
    main()
