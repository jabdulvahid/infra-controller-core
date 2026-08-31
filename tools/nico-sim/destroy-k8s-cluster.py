#!/usr/bin/env python3
"""
DC Simulation — Kubernetes Cluster Teardown

Undoes form-k8s-cluster.py: resets kubeadm on all CP nodes in parallel.

Actions on each node:
  - kubeadm reset -f
  - Remove CNI config (/etc/cni/net.d)
  - Remove etcd data (/var/lib/etcd)
  - Delete CNI network interfaces (flannel.1, cni0)
  - Remove local kubeconfig (optional)

Usage:
  python3 destroy-k8s-cluster.py nico-sim.yaml
  python3 destroy-k8s-cluster.py nico-sim.yaml --keep-kubeconfig
"""

import argparse
import subprocess
import sys
import threading
import time
from ipaddress import IPv4Network
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=/dev/null',
    '-o', 'LogLevel=ERROR',
    '-o', 'ConnectTimeout=10',
    '-o', 'BatchMode=yes',
]

_print_lock = threading.Lock()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Tear down the kubeadm k8s cluster on DC simulation CP VMs',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('sim_yaml', help='nico-sim.yaml')
    p.add_argument('--keep-kubeconfig', action='store_true',
                   help='Do not remove the local kubeconfig file')
    p.add_argument('--kubeconfig',
                   default='~/.kube/config-dc-sim',
                   help='Local kubeconfig path to remove')
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def find_priv_key():
    import sim_ssh
    return sim_ssh.find_priv_key()


def get_cp_nodes(sim):
    pfx    = sim['fabric']['prefixes']
    cp     = sim['control_plane']
    prefix = cp['vm_prefix']
    n      = cp['num_vms']
    cp_net = IPv4Network(cp.get('control_plane_prefix', pfx.get('control_plane_prefix', '7.132.0.0/29')), strict=False)
    pairs  = list(cp_net.subnets(new_prefix=31))
    nodes  = []
    for i in range(n):
        nodes.append({
            'name':    f'{prefix}-{i+1}',
            'oob_mac': f'52:54:00:00:{(i+1):02x}:01',
        })
    return nodes


# ── OOB IP ────────────────────────────────────────────────────────────────────

def get_oob_ip(mac, oob_net):
    r = subprocess.run(['virsh', 'net-dhcp-leases', oob_net],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2].lower() == mac.lower():
            return parts[4].split('/')[0]
    return None


# ── SSH ───────────────────────────────────────────────────────────────────────

def ssh_stream(oob_ip, priv_key, cmd, prefix='', timeout=120):
    proc = subprocess.Popen(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output = []
    label = f'[{prefix}] ' if prefix else ''
    try:
        for line in proc.stdout:
            with _print_lock:
                print(f'    {label}{line}', end='', flush=True)
            output.append(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    return proc.returncode, ''.join(output)


# ── Reset ─────────────────────────────────────────────────────────────────────

def reset_node(node, oob_net, priv_key, errors):
    name = node['name']
    oob_ip = get_oob_ip(node['oob_mac'], oob_net)
    if not oob_ip:
        with _print_lock:
            print(f'  [{name}] no OOB IP — VM not running, skipping')
        return

    with _print_lock:
        print(f'  [{name}] resetting at {oob_ip}...')

    cmd = (
        'sudo kubeadm reset -f 2>&1; '
        'sudo rm -rf /etc/cni/net.d /var/lib/etcd; '
        'sudo ip link delete flannel.1 2>/dev/null || true; '
        'sudo ip link delete cni0 2>/dev/null || true; '
        'echo "[done]"'
    )
    rc, out = ssh_stream(oob_ip, priv_key, cmd, prefix=name)
    if '[done]' not in out:
        errors[name] = f'reset may have failed (no [done] marker)\n{out[-500:]}'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args     = parse_args()
    sim      = load_sim(args.sim_yaml)
    oob_net  = sim['control_plane']['oob_network']['name']
    nodes    = get_cp_nodes(sim)
    priv_key = find_priv_key()

    print('DC Simulation — Kubernetes Cluster Teardown')
    print(f'  nodes    : {[n["name"] for n in nodes]}')
    print(f'  SSH key  : {priv_key}')
    print()

    # Reset all nodes in parallel
    print(f'Step 1: kubeadm reset on all nodes (parallel)')
    errors = {}
    threads = [
        threading.Thread(target=reset_node,
                         args=(node, oob_net, priv_key, errors),
                         daemon=True)
        for node in nodes
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print()

    if errors:
        print('Warnings (non-fatal):')
        for name, msg in errors.items():
            print(f'  {name}: {msg}')
        print()

    # Remove local kubeconfig
    if not args.keep_kubeconfig:
        kc = Path(args.kubeconfig).expanduser()
        if kc.exists():
            kc.unlink()
            print(f'Step 2: Removed local kubeconfig: {kc}')
        else:
            print(f'Step 2: Local kubeconfig not found (already removed): {kc}')
    else:
        print('Step 2: Kept local kubeconfig (--keep-kubeconfig)')
    print()

    print('Done. Re-form with:')
    print('  python3 form-k8s-cluster.py nico-sim.yaml')


if __name__ == '__main__':
    main()
