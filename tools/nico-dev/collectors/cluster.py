"""cluster context — single-node kubeadm cluster running on the VM host.

Uses the kubeconfig discovered by the site collector.
kubectl must be available on PATH.
"""

import re
import subprocess


def _kubectl(kubeconfig, args, timeout=15):
    cmd = ['kubectl', '--kubeconfig', kubeconfig] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return '', f'kubectl timed out after {timeout}s', 1
    return r.stdout, r.stderr, r.returncode


def _parse_nodes(output):
    """
    Parse 'kubectl get nodes -o wide --no-headers' output.
    Columns: NAME  STATUS  ROLES  AGE  VERSION  INTERNAL-IP  ...
    """
    nodes = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            nodes.append({
                'name':    parts[0],
                'status':  parts[1],   # Ready / NotReady
                'roles':   parts[2],
                'version': parts[4],
                'ip':      parts[5],
            })
    return nodes


def collect(site):
    """Collect kubernetes node status."""
    kubeconfig = site.get('_kubeconfig')

    if not kubeconfig:
        return {
            'node_name':  '',
            'node_ip':    '',
            'ready':      False,
            'version':    '',
            'pod_count':  0,
            'kubeconfig': None,
            'error':      'kubeconfig not found',
        }

    # Get nodes
    out, err, rc = _kubectl(kubeconfig, ['get', 'nodes', '-o', 'wide', '--no-headers'])
    nodes = _parse_nodes(out) if rc == 0 else []

    # Single-node kubeadm cluster — expect exactly one node
    node = nodes[0] if nodes else {}

    # Count running pods
    pod_out, _pod_err, pod_rc = _kubectl(
        kubeconfig,
        ['get', 'pods', '-A', '--no-headers'],
    )
    pod_lines = [l for l in pod_out.splitlines() if l.strip()] if pod_rc == 0 else []
    pod_count = len(pod_lines)

    return {
        'node_name':  node.get('name', ''),
        'node_ip':    node.get('ip', ''),
        'ready':      node.get('status', '') == 'Ready',
        'version':    node.get('version', ''),
        'pod_count':  pod_count,
        'kubeconfig': kubeconfig,
        'error':      '' if rc == 0 else (err.strip() or out.strip() or 'kubectl error'),
    }
