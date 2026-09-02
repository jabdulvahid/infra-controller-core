"""Fabric context — ContainerLab containers and BGP summary for nico-dev.

Switches in the dev topology: super-spine, spine-1, leaf-cp, leaf-mat.
dpu-1 is a separate container handled by collectors/dpu.py.
Bridge nodes (br-<dc>-*) are host bridges, not Docker containers.
"""

import json
import re
import subprocess


def _docker_exec(container, cmd, timeout=8):
    r = subprocess.run(
        ['docker', 'exec', container] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout, r.returncode


def _container_name(clab_name, node):
    return f'clab-{clab_name}-{node}'


def _get_containers(clab_name):
    """Return list of dicts for all clab containers for this site."""
    r = subprocess.run(
        ['docker', 'ps', '-a',
         '--filter', f'name=clab-{clab_name}-',
         '--format', '{{.Names}}\t{{.Status}}\t{{.Image}}'],
        capture_output=True, text=True,
    )
    containers = []
    for line in r.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            name   = parts[0]
            status = parts[1]
            image  = parts[2] if len(parts) > 2 else ''
            node   = name.removeprefix(f'clab-{clab_name}-')
            containers.append({
                'name':    name,
                'node':    node,
                'status':  status,
                'image':   image.split('@')[0].split('/')[-1],
                'running': status.startswith('Up'),
            })
    return sorted(containers, key=lambda c: c['node'])


def _bgp_summary(clab_name, node):
    """Quick BGP check: return (total_peers, established_peers)."""
    cname = _container_name(clab_name, node)
    out, rc = _docker_exec(cname, ['vtysh', '-c', 'show bgp summary'], timeout=5)
    if rc != 0:
        return 0, 0
    total = 0
    established = 0
    for line in out.splitlines():
        m = re.match(r'\S+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\S+\s+(\S+)', line.strip())
        if m:
            total += 1
            if m.group(1).isdigit():
                established += 1
    return total, established


def _get_bridges(dc_name):
    """Return site-specific bridge names and their state."""
    # 'ip' is Linux-only: on macOS this collector runs off-host and must
    # degrade, not traceback (same class as 20260823-#1; found 2026-08-26
    # running ndev from the Mac against a clone site).
    try:
        r = subprocess.run(
            ['ip', '-j', 'link', 'show', 'type', 'bridge'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    bridges = []
    try:
        links = json.loads(r.stdout)
        for link in links:
            name = link.get('ifname', '')
            if name.startswith(f'br-{dc_name}-'):
                bridges.append({
                    'name':  name,
                    'role':  name.removeprefix(f'br-{dc_name}-'),
                    'state': link.get('operstate', 'UNKNOWN'),
                    'flags': link.get('flags', []),
                })
    except Exception:
        pass
    return sorted(bridges, key=lambda b: b['name'])


def collect(site):
    """Collect fabric context data."""
    clab_name = site['clab_name']
    dc_name   = site['dc_name']

    # Off-host: the fabric is the VM's docker. Never consult the host's —
    # on a Linux host with same-named leftovers (nico-sim's clab-dc1-*) it
    # reported a stale fabric as ✓ 11/11 (20260902-#8).
    if not site.get('_on_vm', True):
        return {
            'containers': [], 'switches': [], 'bridges': [],
            'total_switches': 0, 'running_switches': 0, 'leafs': [],
            'spines': [], 'ss_bgp_total': 0, 'ss_bgp_estab': 0,
            'offhost': True,
            'error': 'fabric is VM-side — run ndev on the VM for switch/BGP detail',
        }

    all_containers = _get_containers(clab_name)
    bridges        = _get_bridges(dc_name)

    # 20260823-#1: no containers at all → error, not an empty-but-healthy view
    if not all_containers:
        return {
            'containers': [], 'switches': [], 'bridges': bridges,
            'total_switches': 0, 'running_switches': 0, 'leafs': [],
            'spines': [], 'ss_bgp_total': 0, 'ss_bgp_estab': 0,
            'error': 'no fabric containers found (clab-' + clab_name + '-*) — the fabric lives on the VM; run ndev there, or deploy it (deploy-dev-fabric.py)',
        }

    # Switches are fabric containers — exclude dpu-* (handled by dpu collector)
    switches = [c for c in all_containers if not c['node'].startswith('dpu-')]

    # Quick BGP summary on super-spine as overall health indicator
    ss_total, ss_estab = 0, 0
    ss = next((c for c in switches if c['node'] == 'super-spine'), None)
    if ss and ss['running']:
        ss_total, ss_estab = _bgp_summary(clab_name, 'super-spine')

    running_switches = sum(1 for c in switches if c['running'])

    # Categorise leafs for display
    leafs    = sorted([c['node'] for c in switches if c['node'].startswith('leaf-')])
    spines   = sorted([c['node'] for c in switches if c['node'].startswith('spine-')])

    return {
        'containers':        all_containers,
        'switches':          switches,
        'bridges':           bridges,

        'total_switches':    len(switches),
        'running_switches':  running_switches,

        'leafs':             leafs,
        'spines':            spines,

        # Super-spine BGP as health indicator
        'ss_bgp_total':      ss_total,
        'ss_bgp_estab':      ss_estab,
    }


def collect_detail(site):
    """Collect detailed fabric data including per-node BGP."""
    base      = collect(site)
    clab_name = site['clab_name']

    bgp_detail = {}
    for sw in base['switches']:
        if not sw['running']:
            bgp_detail[sw['node']] = {'error': 'not running'}
            continue
        out, rc = _docker_exec(
            _container_name(clab_name, sw['node']),
            ['vtysh', '-c', 'show bgp summary'],
            timeout=8,
        )
        bgp_detail[sw['node']] = {
            'raw': out,
            'rc':  rc,
        }

    base['bgp_detail'] = bgp_detail
    return base
