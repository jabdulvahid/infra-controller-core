"""BGP context — sessions across all fabric BGP speakers (nico-dev standalone).

Includes dpu-1: in nico-dev the DPU stand-in is an FRR container and holds
the MetalLB peering session (the host/VIP path) — the one session a developer
most often needs to check.
"""

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed


def _docker_bgp(container, timeout=8):
    r = subprocess.run(
        ['docker', 'exec', container, 'vtysh', '-c', 'show bgp summary'],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout, r.returncode


# FRR names the address-family in the SECTION header ("IPv4 Unicast Summary:",
# "L2VPN EVPN Summary:"), NOT in the Neighbor column-header line — the AF must
# be inferred from the lines seen since the previous table ended.
_AF_HINTS = [
    ('l2vpn evpn',    ['l2vpn', 'evpn']),
    ('ipv6 unicast',  ['ipv6']),
    ('vpnv4 unicast', ['vpnv4']),
    ('ipv4 unicast',  ['ipv4']),
]
_AF_ORDER = ['ipv4 unicast', 'l2vpn evpn', 'ipv6 unicast', 'vpnv4 unicast']


def _guess_af(lines, table_idx):
    text = ' '.join(lines).lower()
    for af, keywords in _AF_HINTS:
        if all(k in text for k in keywords):
            return af
    return _AF_ORDER[min(table_idx, len(_AF_ORDER) - 1)]


def _parse_bgp_summary(output):
    """Parse vtysh 'show bgp summary' — multi-AF aware."""
    peers = {}
    table_idx  = 0
    in_table   = False
    current_af = _AF_ORDER[0]
    context    = []

    for line in output.splitlines():
        if 'Neighbor' in line and 'State' in line:
            current_af = _guess_af(context, table_idx)
            table_idx += 1
            in_table   = True
            context    = []
            continue

        if not in_table:
            context.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            in_table = False
            continue

        m = re.match(
            r'(\S+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+'
            r'(\S+)\s+(\S+)\s+\S+\s*(.*)',
            stripped,
        )
        if not m:
            continue

        peer_raw, _ver, asn, updown, state_pfx, desc = m.groups()
        ip_m    = re.search(r'\(([0-9.]+)\)', peer_raw)
        peer_ip = ip_m.group(1) if ip_m else peer_raw
        established = state_pfx.isdigit()
        # NoNeg = FRR BGP collision resolution; session is working
        no_neg = (state_pfx == 'NoNeg' and updown != 'never')

        if peer_ip not in peers:
            peers[peer_ip] = {
                'ip':   peer_ip,
                'asn':  asn,
                'desc': desc.strip(),
                'afs':  {},
            }

        peers[peer_ip]['afs'][current_af] = {
            'established': established,
            'no_neg':      no_neg,
            'state':       f'Estab({state_pfx} pfx)' if established
                           else ('NoNeg(collision)' if no_neg else state_pfx),
            'updown':      updown,
        }

    return list(peers.values())


def collect(site):
    """Collect BGP state from all fabric BGP speakers (switches + dpu-*)."""
    clab_name = site['clab_name']

    # Off-host: never consult the host's docker — a Linux host with
    # same-named leftovers (nico-sim's clab-dc1-*) reported THEIR sessions
    # as this site's "✓ 62/62" (20260902-#8).
    if not site.get('_on_vm', True):
        return {
            'nodes': {}, 'total_sessions': 0, 'established': 0,
            'not_established': 0, 'switch_count': 0, 'offhost': True,
            'error': 'BGP is VM-side — run ndev on the VM',
        }

    r = subprocess.run(
        ['docker', 'ps', '--filter', f'name=clab-{clab_name}-',
         '--format', '{{.Names}}'],
        capture_output=True, text=True,
    )
    # All fabric containers run FRR in nico-dev — dpu-1 included (it holds
    # the MetalLB session toward the VM host)
    switch_containers = r.stdout.splitlines()

    # 20260823-#1: an empty container list must be an ERROR, not "0 sessions
    # all healthy" — on the Mac, docker is colima and the fabric is invisible.
    if not switch_containers:
        return {
            'nodes': {}, 'total_sessions': 0, 'established': 0,
            'not_established': 0, 'switch_count': 0,
            'error': 'no fabric containers found (clab-' + clab_name + '-*) — the fabric lives on the VM; run ndev there, or deploy it (deploy-dev-fabric.py)',
        }

    nodes = {}

    def fetch(cname):
        node = cname.removeprefix(f'clab-{clab_name}-')
        out, rc = _docker_bgp(cname)
        if rc != 0:
            return node, {'error': True, 'peers': []}
        peers = _parse_bgp_summary(out)
        return node, {'error': False, 'peers': peers}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch, c): c for c in switch_containers}
        for future in as_completed(futures):
            node, result = future.result()
            nodes[node] = result

    # Aggregate totals
    total_sessions = 0
    established    = 0
    for node_data in nodes.values():
        for peer in node_data.get('peers', []):
            for af_data in peer.get('afs', {}).values():
                total_sessions += 1
                if af_data.get('established') or af_data.get('no_neg'):
                    established += 1

    return {
        'nodes':           nodes,
        'total_sessions':  total_sessions,
        'established':     established,
        'not_established': total_sessions - established,
        'switch_count':    len(switch_containers),
    }
