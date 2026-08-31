#!/usr/bin/env python3
"""
DC Simulation — Kubernetes Cluster Formation

Forms a kubeadm HA control-plane cluster across all CP host VMs.
No manual SSH steps required.

All 3 CP VMs join as control-plane nodes (matches production Nico architecture):
  cp-1 : kubeadm init (primary control plane)
  cp-2 : kubeadm join --control-plane
  cp-3 : kubeadm join --control-plane

Each node uses its fabric IP (7.132.0.x) as the node-ip and apiserver
advertise address, matching the DPU stand-in BGP advertisement.

Usage:
  python3 form-k8s-cluster.py nico-sim.yaml
  python3 form-k8s-cluster.py nico-sim.yaml --reset      # reset + re-form
  python3 form-k8s-cluster.py nico-sim.yaml --skip-cni   # skip Flannel install
"""

import argparse
import os
import re
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

FLANNEL_URL = ('https://github.com/flannel-io/flannel/releases/latest/'
               'download/kube-flannel.yml')


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Form a kubeadm k8s HA cluster across DC simulation CP VMs',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--reset', action='store_true',
                   help='Run kubeadm reset on all nodes before forming cluster '
                        '(use when re-forming after a previous kubeadm init)')
    p.add_argument('--skip-cni', action='store_true',
                   help='Skip Flannel CNI installation')
    p.add_argument('--kubeconfig',
                   default=None,
                   help='Where to save the kubeconfig (default: <site_folder>/<kube_id>.kubeconfig.yaml)')
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def find_priv_key():
    import sim_ssh
    return sim_ssh.find_priv_key()


def get_cp_nodes(sim):
    """
    Return list of node dicts derived from nico-sim.yaml IP allocation.
    node_ip = host VM internal IP (from control_plane_prefix /31 pairs, odd address)
    oob_mac = deterministic OOB MAC matching generate-nodes.py vm_mac(i, 1)
    """
    pfx      = sim['fabric']['prefixes']
    cp       = sim['control_plane']
    vp       = cp['vm_prefix']
    n        = cp['num_vms']
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    so       = int(pfx.get('switch_underlay', '7.0.0.0/8').split('.')[0])

    cp_pfx  = cp.get('control_plane_prefix', pfx.get('control_plane_prefix', '7.132.1.0/24'))
    cp_net  = IPv4Network(cp_pfx, strict=False)
    subnets = list(cp_net.subnets(new_prefix=31))

    nodes = []
    for i in range(n):
        hosts   = list(subnets[i].hosts())
        node_ip = str(hosts[1])                              # .1 = host VM
        oob_mac = f'52:54:{so:02x}:00:{(i+1):02x}:01'     # vm_mac(i+1, 1, so)
        nodes.append({
            'name':    f'{name_pfx}{vp}-{i+1}',
            'node_ip': node_ip,
            'oob_mac': oob_mac,
            'index':   i + 1,
        })
    return nodes


# ── OOB IP resolution ─────────────────────────────────────────────────────────

def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def get_oob_ip(mac, oob_net):
    """Parse virsh DHCP lease output: $1=date $2=time $3=mac $4=proto $5=ip/prefix."""
    r = subprocess.run(['virsh', '-c', 'qemu:///system', 'net-dhcp-leases', oob_net],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2].lower() == mac.lower():
            return parts[4].split('/')[0]
    return None


def wait_for_oob_ip(name, mac, oob_net, timeout=120):
    print(f'  [{name}] waiting for OOB IP', end='', flush=True)
    for i in range(0, timeout, 5):
        ip = get_oob_ip(mac, oob_net)
        if ip:
            print(f' → {ip} ({i}s)')
            return ip
        time.sleep(5)
        print('.', end='', flush=True)
    print()
    raise RuntimeError(f'{name}: no OOB IP after {timeout}s — is the VM running?')


# ── SSH helpers ───────────────────────────────────────────────────────────────

def ssh_run(oob_ip, priv_key, cmd, timeout=60):
    """Run SSH command, return (rc, combined stdout+stderr)."""
    r = subprocess.run(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout + r.stderr


_print_lock = threading.Lock()


def ssh_stream(oob_ip, priv_key, cmd, timeout=600, prefix=''):
    """Run SSH command, stream output to console, return (rc, full_output)."""
    proc = subprocess.Popen(
        ['ssh'] + SSH_OPTS + ['-i', priv_key, f'ubuntu@{oob_ip}', cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output = []
    label = f'[{prefix}] ' if prefix else '    '
    try:
        for line in proc.stdout:
            with _print_lock:
                print(f'    {label}{line}', end='', flush=True)
            output.append(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f'SSH command timed out after {timeout}s')
    return proc.returncode, ''.join(output)


# ── Cluster operations ────────────────────────────────────────────────────────

def reset_node(name, oob_ip, priv_key):
    print(f'  [{name}] kubeadm reset...')
    cmd = ('sudo kubeadm reset -f 2>&1; '
           'sudo rm -rf /etc/cni/net.d /var/lib/etcd; '
           'sudo ip link delete flannel.1 2>/dev/null || true; '
           'sudo ip link delete cni0 2>/dev/null || true')
    rc, _ = ssh_stream(oob_ip, priv_key, cmd, timeout=120)
    if rc != 0:
        print(f'  [{name}] reset returned {rc} (may be ok if not previously initialized)')


def configure_node_ip(name, oob_ip, node_ip, priv_key):
    """Set KUBELET_EXTRA_ARGS so kubelet registers with the fabric IP, not OOB."""
    print(f'  [{name}] kubelet --node-ip={node_ip}')
    cmd = (f'echo \'KUBELET_EXTRA_ARGS="--node-ip={node_ip}"\' '
           f'| sudo tee /etc/default/kubelet > /dev/null')
    rc, out = ssh_run(oob_ip, priv_key, cmd)
    if rc != 0:
        raise RuntimeError(f'{name}: failed to set kubelet node-ip: {out}')


def kubeadm_init(name, oob_ip, node_ip, pod_cidr, priv_key):
    """Run kubeadm init on the primary control-plane node."""
    print(f'  [{name}] kubeadm init (~2 min)...')
    cmd = (
        f'sudo kubeadm init '
        f'--apiserver-advertise-address={node_ip} '
        f'--control-plane-endpoint={node_ip}:6443 '
        f'--pod-network-cidr={pod_cidr} '
        f'--upload-certs '     # enables HA: other nodes can join as control-plane
        f'2>&1'
    )
    rc, output = ssh_stream(oob_ip, priv_key, cmd, timeout=300)
    if rc != 0:
        raise RuntimeError(f'{name}: kubeadm init failed (exit {rc})')

    # Set up kubeconfig for ubuntu user on the primary node
    setup_cmd = ('mkdir -p $HOME/.kube && '
                 'sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config && '
                 'sudo chown $(id -u):$(id -g) $HOME/.kube/config')
    ssh_run(oob_ip, priv_key, setup_cmd)
    return output


def extract_join_cmds(output):
    """
    Extract control-plane and worker join commands from kubeadm init output.
    Returns (cp_join_cmd, worker_join_cmd) — cp_join_cmd has --control-plane flag.
    """
    lines = output.splitlines()
    joins = []
    current = []
    in_cmd = False

    for line in lines:
        s = line.strip()
        if s.startswith('kubeadm join'):
            in_cmd = True
            current = [s.rstrip('\\')]
            if not s.endswith('\\'):
                joins.append(' '.join(p.strip() for p in current if p.strip()))
                current = []
                in_cmd = False
        elif in_cmd:
            if s and not s.startswith('#') and not s.startswith('Please') \
                    and not s.startswith('As a safeguard'):
                current.append(s.rstrip('\\'))
                if not s.endswith('\\'):
                    joins.append(' '.join(p.strip() for p in current if p.strip()))
                    current = []
                    in_cmd = False
            else:
                if current:
                    joins.append(' '.join(p.strip() for p in current if p.strip()))
                current = []
                in_cmd = False

    if current:
        joins.append(' '.join(p.strip() for p in current if p.strip()))

    valid = [j for j in joins if '--token' in j and '--discovery-token-ca-cert-hash' in j]
    if not valid:
        raise RuntimeError(
            f'No kubeadm join command found in kubeadm init output.\n'
            f'Last 2000 chars:\n{output[-2000:]}')

    cp_join = next((j for j in valid if '--control-plane' in j), None)
    wk_join = next((j for j in valid if '--control-plane' not in j), None)

    if not cp_join:
        raise RuntimeError(
            'No control-plane join command found. '
            'Was --upload-certs passed to kubeadm init?')

    return cp_join, wk_join


def kubeadm_join_cp(name, oob_ip, node_ip, join_cmd, priv_key):
    """Join a node as a control-plane member (call from thread or directly)."""
    with _print_lock:
        print(f'  [{name}] kubeadm join --control-plane...')
    full_cmd = (f'sudo {join_cmd} '
                f'--apiserver-advertise-address={node_ip} '
                f'2>&1')
    rc, output = ssh_stream(oob_ip, priv_key, full_cmd, timeout=300, prefix=name)
    if rc != 0:
        raise RuntimeError(f'{name}: kubeadm join failed (exit {rc}):\n{output[-1000:]}')


def kubeadm_join_cp_parallel(nodes, cp_join, priv_key):
    """Run kubeadm join --control-plane on multiple nodes simultaneously."""
    errors = {}

    def _join(node):
        try:
            kubeadm_join_cp(node['name'], node['oob_ip'], node['node_ip'],
                            cp_join, priv_key)
        except Exception as e:
            errors[node['name']] = str(e)

    threads = [threading.Thread(target=_join, args=(n,), daemon=True) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        msgs = '\n'.join(f'{n}: {e}' for n, e in errors.items())
        raise RuntimeError(f'One or more nodes failed to join:\n{msgs}')


def remove_control_plane_taint(primary_oob_ip, priv_key):
    """Remove NoSchedule taint so pods can run on all control-plane nodes."""
    print('  Removing control-plane NoSchedule taint (all nodes will schedule pods)...')
    cmd = "kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>&1 || true"
    rc, out = ssh_run(primary_oob_ip, priv_key, cmd)
    print(f'    {out.strip()}')


def install_flannel(primary_oob_ip, priv_key):
    print(f'  Installing Flannel CNI from {FLANNEL_URL}...')
    cmd = f'kubectl apply -f {FLANNEL_URL} 2>&1'
    rc, output = ssh_stream(primary_oob_ip, priv_key, cmd, timeout=60)
    if rc != 0:
        raise RuntimeError(f'Flannel install failed:\n{output}')


def wait_nodes_ready(primary_oob_ip, priv_key, expected, timeout=300):
    print(f'  Waiting for {expected} nodes to be Ready', end='', flush=True)
    start = time.time()
    while time.time() - start < timeout:
        rc, out = ssh_run(primary_oob_ip, priv_key,
                          "kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}'",
                          timeout=15)
        if rc == 0:
            statuses = [s.strip() for s in out.strip().splitlines() if s.strip()]
            ready = sum(1 for s in statuses if s == 'Ready')
            not_ready = [s for s in statuses if s != 'Ready']
            if ready == expected:
                elapsed = int(time.time() - start)
                print(f' all {ready}/{expected} Ready ({elapsed}s)')
                return
        time.sleep(10)
        print('.', end='', flush=True)
    print()
    raise RuntimeError(f'Nodes not Ready after {timeout}s. Check: kubectl get nodes')


def get_kubeconfig(primary_oob_ip, priv_key):
    rc, out = ssh_run(primary_oob_ip, priv_key,
                      'cat $HOME/.kube/config', timeout=15)
    if rc != 0 or not out.strip():
        raise RuntimeError('Could not read kubeconfig from primary node')
    return out


def save_kubeconfig(content, path_str):
    path = Path(path_str).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args          = parse_args()
    site_yaml, site_folder = resolve_site(args.site)
    sim           = load_sim(site_yaml)
    cp            = sim['control_plane']
    oob_net       = cp['oob_network']['name']
    gi_cp         = sim.get('golden_image', {}).get('control_plane', {})
    pod_cidr      = gi_cp.get('kubernetes', {}).get('pod_network_cidr', '10.244.0.0/16')

    nodes    = get_cp_nodes(sim)
    priv_key = find_priv_key()
    primary  = nodes[0]
    workers  = nodes[1:]

    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    kube_id  = f'{dc_name}-{sitename}' if sitename else dc_name

    # Default: write kubeconfig to site folder (works for any user, incl. root)
    # The site yaml's kubeconfig field defines the filename; fall back to kube_id.
    kube_filename = sim.get('kubeconfig', f'{kube_id}.kubeconfig.yaml')
    default_path  = str(Path(site_folder) / kube_filename)
    kubeconfig_path = args.kubeconfig or default_path

    print('DC Simulation — Kubernetes Cluster Formation')
    print(f'  sim config   : {site_yaml}')
    print(f'  pod CIDR     : {pod_cidr}')
    print(f'  nodes        : {[n["name"] + " (" + n["node_ip"] + ")" for n in nodes]}')
    print(f'  primary      : {primary["name"]} ({primary["node_ip"]})')
    print(f'  API endpoint : {primary["node_ip"]}:6443')
    print(f'  SSH key      : {priv_key}')
    print(f"  kubeconfig   : {kubeconfig_path}")
    print(f'  mode         : all 3 nodes as control-plane (HA)')
    print()

    # ── Step 1: Resolve OOB IPs ───────────────────────────────────────────────
    print('Step 1: Resolve OOB IPs')
    for node in nodes:
        node['oob_ip'] = wait_for_oob_ip(node['name'], node['oob_mac'], oob_net)
    print()

    # ── Step 2: Reset (optional) ──────────────────────────────────────────────
    if args.reset:
        print('Step 2: Reset all nodes')
        for node in nodes:
            reset_node(node['name'], node['oob_ip'], priv_key)
        print()
    else:
        print('Step 2: Skipped (use --reset to reset before forming)')
        print()

    # ── Step 3: Configure kubelet node-ip ─────────────────────────────────────
    print('Step 3: Configure kubelet --node-ip on all nodes')
    for node in nodes:
        configure_node_ip(node['name'], node['oob_ip'], node['node_ip'], priv_key)
    print()

    # ── Step 4: kubeadm init on primary ───────────────────────────────────────
    print(f'Step 4: kubeadm init on {primary["name"]}')
    init_output = kubeadm_init(
        primary['name'], primary['oob_ip'],
        primary['node_ip'], pod_cidr, priv_key,
    )
    cp_join, wk_join = extract_join_cmds(init_output)
    print(f'  Extracted control-plane join command ✓')
    print()

    # ── Step 5: Join remaining nodes as control-plane (in parallel) ───────────
    print(f'Step 5: Join {[n["name"] for n in workers]} as control-plane nodes (parallel)')
    kubeadm_join_cp_parallel(workers, cp_join, priv_key)
    print()

    # ── Step 6: Remove control-plane taint ────────────────────────────────────
    print('Step 6: Remove control-plane NoSchedule taint')
    remove_control_plane_taint(primary['oob_ip'], priv_key)
    print()

    # ── Step 7: Install Flannel CNI ───────────────────────────────────────────
    if not args.skip_cni:
        print('Step 7: Install Flannel CNI')
        install_flannel(primary['oob_ip'], priv_key)
        print()
    else:
        print('Step 7: Skipped (--skip-cni)')
        print()

    # ── Step 8: Wait for all nodes Ready ──────────────────────────────────────
    print('Step 8: Wait for all nodes Ready')
    wait_nodes_ready(primary['oob_ip'], priv_key, len(nodes))
    print()

    # ── Step 9: Save kubeconfig ───────────────────────────────────────────────
    print('Step 9: Save kubeconfig')
    kubeconfig_content = get_kubeconfig(primary['oob_ip'], priv_key)
    saved = save_kubeconfig(kubeconfig_content, kubeconfig_path)
    print(f'  Saved: {saved}')
    print()

    # ── Step 10: Remove LoadBalancer exclusion label ──────────────────────────
    # Kubernetes 1.24+ automatically adds node.kubernetes.io/exclude-from-external-load-balancers
    # to control-plane nodes. MetalLB respects this label and skips all LoadBalancer
    # service announcements. Remove it so MetalLB can advertise VIPs via BGP.
    print('Step 10: Remove LoadBalancer exclusion label from CP nodes')
    env = os.environ.copy()
    env['KUBECONFIG'] = saved
    node_names = [n['name'] for n in nodes]
    result = subprocess.run(
        ['kubectl', 'label', 'nodes'] + node_names +
        ['node.kubernetes.io/exclude-from-external-load-balancers-'],
        env=env, capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f'  Removed exclude-from-external-load-balancers from: {", ".join(node_names)} ✓')
    else:
        print(f'  Warning: label removal failed — run manually if MetalLB VIPs not reachable:')
        print(f'    kubectl label node {" ".join(node_names)} node.kubernetes.io/exclude-from-external-load-balancers-')
    print()

    # ── Done ──────────────────────────────────────────────────────────────────
    print('=' * 60)
    print('Cluster ready.')
    print()
    print('From this host (sim-host):')
    print(f'  export KUBECONFIG={saved}')
    print(f'  kubectl get nodes -o wide')
    print()
    print('From inside a CP VM:')
    print(f'  python3 ssh-vm.py nico-sim.yaml cp-1 --via oob')
    print(f'  kubectl get nodes -o wide')
    print('=' * 60)


if __name__ == '__main__':
    main()
