"""Fabric verification — health checks for nico-dev fabric verify.

Standalone copy; no imports from nsim.
Uses the same ContainerLab docker exec approach as nsim verify_fabric.py.
"""

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────

GREEN  = '\033[32m'
RED    = '\033[31m'
YELLOW = '\033[33m'
BOLD   = '\033[1m'
RESET  = '\033[0m'

def ok(s):   return f'{GREEN}✓ {s}{RESET}'
def fail(s): return f'{RED}✗ {s}{RESET}'
def warn(s): return f'{YELLOW}⚠ {s}{RESET}'
def bold(s): return f'{BOLD}{s}{RESET}'

def _green(s): return f'{GREEN}{s}{RESET}'
def _red(s):   return f'{RED}{s}{RESET}'


# ── Topology helpers ──────────────────────────────────────────────────────────

def _load_topo(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def _get_switch_nodes(topo):
    """Return fabric switch nodes (excludes bridges) plus dpu-* nodes separately."""
    nodes = topo.get('topology', {}).get('nodes', {})
    bridge_nodes = {k for k, v in nodes.items() if v.get('kind', '') == 'bridge'}
    dpu_nodes    = sorted(k for k in nodes if k.startswith('dpu-'))
    switch_nodes = [n for n in nodes if n not in bridge_nodes and n not in dpu_nodes]
    super_spines = [n for n in switch_nodes if n == 'super-spine']
    spines       = sorted([n for n in switch_nodes if n.startswith('spine-')])
    leafs        = sorted([n for n in switch_nodes if n.startswith('leaf-')])
    return super_spines + spines + leafs, super_spines, spines, leafs, dpu_nodes


def _cname(lab_name, node):
    return f'clab-{lab_name}-{node}'


# ── Docker helpers ────────────────────────────────────────────────────────────

def _docker_exec(container, cmd, timeout=10):
    result = subprocess.run(
        ['docker', 'exec', container] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


def _is_running(container):
    r = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Running}}', container],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == 'true'


# ── Check 1: Container status ─────────────────────────────────────────────────

def check_containers(lab_name, all_switches):
    print(bold('\n═══ Check 1: Container Status ═══'))
    issues = []
    for node in all_switches:
        cname   = _cname(lab_name, node)
        running = _is_running(cname)
        if running:
            print(f'  {ok(f"{node:20s}")}  container={cname}')
        else:
            print(f'  {fail(f"{node:20s}")}  container={cname} NOT RUNNING')
            issues.append(f'container {cname} not running')
    return issues


# ── Check 2: BGP state ───────────────────────────────────────────────────────

_AF_HINTS = [
    ('l2vpn evpn',    ['l2vpn', 'evpn']),
    ('ipv6 unicast',  ['ipv6']),
    ('vpnv4 unicast', ['vpnv4']),
    ('ipv4 unicast',  ['ipv4']),
]
_AF_ORDER = ['ipv4 unicast', 'l2vpn evpn', 'ipv6 unicast', 'vpnv4 unicast']
_AF_SHORT = {
    'ipv4 unicast':  'ipv4',
    'l2vpn evpn':    'evpn',
    'ipv6 unicast':  'ipv6',
    'vpnv4 unicast': 'vpnv4',
}


def _guess_af(lines, table_idx):
    text = ' '.join(lines).lower()
    for af, keywords in _AF_HINTS:
        if all(k in text for k in keywords):
            return af
    return _AF_ORDER[min(table_idx, len(_AF_ORDER) - 1)]


def _parse_bgp_summary(output):
    peers_by_ip = {}
    table_idx   = 0
    in_table    = False
    current_af  = _AF_ORDER[0]
    context     = []

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
        m = re.match(r'(\S+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+'
                     r'(\S+)\s+(\S+)\s+\S+\s*(.*)', stripped)
        if not m:
            continue
        peer_raw, _ver, asn, updown, state_pfx, desc = m.groups()
        ip_m    = re.search(r'\(([0-9.]+)\)', peer_raw)
        peer_ip = ip_m.group(1) if ip_m else peer_raw
        established = state_pfx.isdigit()
        no_neg = (state_pfx == 'NoNeg' and updown != 'never')
        if peer_ip not in peers_by_ip:
            peers_by_ip[peer_ip] = {
                'peer': peer_ip, 'peer_name': peer_raw.split('(')[0],
                'asn': asn, 'desc': desc.strip(), 'address_families': {},
            }
        peers_by_ip[peer_ip]['address_families'][current_af] = {
            'established': established or no_neg,
            'no_neg':      no_neg,
            'state': f'Established ({state_pfx} pfx)' if established
                     else ('NoNeg(collision)' if no_neg else state_pfx),
            'updown': updown,
        }

    result = []
    for peer_ip, p in peers_by_ip.items():
        afs     = p['address_families']
        primary = afs.get('ipv4 unicast') or afs.get(next(iter(afs)), {})
        result.append({
            'peer': peer_ip, 'peer_name': p['peer_name'], 'asn': p['asn'],
            'desc': p['desc'], 'established': primary.get('established', False),
            'updown': primary.get('updown', 'never'),
            'state': primary.get('state', 'Unknown'), 'address_families': afs,
        })
    return result


def check_bgp(lab_name, all_switches, switch_only=False):
    mode_note = '  [switch-only: non-switch peers not counted as failures]' \
                if switch_only else ''
    print(bold(f'\n═══ Check 2: BGP Peer State{mode_note} ═══'))
    issues     = []
    switch_set = set(all_switches)

    for node in all_switches:
        cname = _cname(lab_name, node)
        if not _is_running(cname):
            print(f'  {warn(f"{node:20s}")}  skipped (not running)')
            continue
        stdout, _, _ = _docker_exec(cname, ['vtysh', '-c', 'show bgp summary'])
        peers  = _parse_bgp_summary(stdout)
        rid_m  = re.search(r'BGP router identifier ([0-9.]+), local AS number (\d+)', stdout)
        if rid_m:
            rid, asn = rid_m.groups()
            print(f'\n  {bold(node)}  router-id={rid}  AS={asn}  peers={len(peers)}')
        else:
            print(f'\n  {bold(node)}  (could not parse router info)')

        for p in peers:
            peer_ip   = p['peer']
            peer_asn  = p['asn']
            peer_desc = p['desc']
            updown    = p['updown']
            afs       = p.get('address_families', {})
            is_non_sw = peer_desc not in switch_set and peer_desc.strip().lower() not in switch_set
            multi_af  = len(afs) > 1
            state_inline = '' if multi_af else f'  {p["state"]}'
            ns_tag = ('  [non-switch peer]' if is_non_sw else '')

            if p['established']:
                print(f'    {ok("peer " + f"{peer_ip:16s}")}'
                      f'  AS={peer_asn:6s}{state_inline}  up={updown}  desc={peer_desc}')
            else:
                if is_non_sw and switch_only:
                    print(f'    {warn("peer " + f"{peer_ip:16s}")}'
                          f'  AS={peer_asn:6s}{state_inline}  up={updown}  desc={peer_desc}{ns_tag}')
                else:
                    print(f'    {fail("peer " + f"{peer_ip:16s}")}'
                          f'  AS={peer_asn:6s}{state_inline}  up={updown}  desc={peer_desc}{ns_tag}')
                    if not (is_non_sw and switch_only):
                        issues.append(f'{node}: peer {peer_ip} ({peer_desc}) not Established '
                                      f'(state={p["state"]})')

            if multi_af:
                for af_name in _AF_ORDER:
                    if af_name not in afs:
                        continue
                    af  = afs[af_name]
                    lbl = _AF_SHORT.get(af_name, af_name)
                    if af['established']:
                        print(f'        {_green("✓")} {lbl:<6s}: {_green(af["state"])}')
                    else:
                        marker = warn('⚠') if (is_non_sw and switch_only) else fail('✗')
                        print(f'        {marker} {lbl:<6s}: {af["state"]}')

    return issues


# ── Check 3: Loopback reachability ───────────────────────────────────────────

def _get_loopback(lab_name, node):
    stdout, _, _ = _docker_exec(_cname(lab_name, node), ['ip', 'addr', 'show', 'lo'])
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('inet ') and '127.' not in line:
            return line.split()[1].split('/')[0]
    return None


def _ping_loopback(lab_name, src, src_lo, dst, dst_lo):
    stdout, _, rc = _docker_exec(
        _cname(lab_name, src),
        ['ping', '-c1', '-W2', f'-I{src_lo}', dst_lo],
        timeout=5,
    )
    m = re.search(r'time=([0-9.]+)', stdout)
    return rc == 0, float(m.group(1)) if m else None


def check_loopback_reachability(lab_name, all_switches):
    print(bold('\n═══ Check 3: Loopback Reachability ═══'))
    issues    = []
    loopbacks = {}
    for node in all_switches:
        if not _is_running(_cname(lab_name, node)):
            continue
        lb = _get_loopback(lab_name, node)
        if lb:
            loopbacks[node] = lb
            print(f'  {node:20s}  loopback={lb}')
        else:
            print(f'  {warn(f"{node:20s}")}  could not determine loopback IP')

    if len(loopbacks) < 2:
        print(f'  {warn("not enough nodes with loopbacks")}')
        return issues
    print()

    pairs   = [(s, d) for s in loopbacks for d in loopbacks if s != d]
    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_ping_loopback, lab_name, s, loopbacks[s], d, loopbacks[d]): (s, d)
                for s, d in pairs}
        for fut in as_completed(futs):
            s, d = futs[fut]
            try:
                results[(s, d)] = fut.result()
            except Exception:
                results[(s, d)] = (False, None)

    for src in sorted(loopbacks):
        src_lo = loopbacks[src]
        print(f'  {bold("from " + src + " (" + src_lo + ")")}')
        for dst in sorted(loopbacks):
            if src == dst:
                continue
            dst_lo    = loopbacks[dst]
            ok_f, rtt = results.get((src, dst), (False, None))
            rtt_str   = f'{rtt:.3f}ms' if rtt is not None else 'timeout'
            dst_label = dst + ' (' + dst_lo + ')'
            if ok_f:
                print(f'    {ok("→ " + f"{dst_label:30s}")}  rtt={rtt_str}')
            else:
                print(f'    {fail("→ " + f"{dst_label:30s}")}  {rtt_str}')
                issues.append(f'loopback ping failed: {src}({src_lo}) → {dst}({dst_lo})')

    return issues


# ── Check 4: Internet uplink ─────────────────────────────────────────────────

def _inet_bridge(topo):
    for name in topo.get('topology', {}).get('nodes', {}):
        if name.endswith('-internet') or 'internet' in name:
            return name
    return None


def check_internet_uplink(lab_name, topo):
    print(bold('\n═══ Check 4: Internet Uplink ═══'))
    issues      = []
    super_cname = _cname(lab_name, 'super-spine')
    inet_bridge = _inet_bridge(topo)

    if not inet_bridge:
        print(f'  {warn("no internet bridge found in topology")}')
        return issues

    r = subprocess.run(['ip', 'addr', 'show', inet_bridge], capture_output=True, text=True)
    if r.returncode != 0:
        print(f'  {fail(inet_bridge + " bridge")}  not found on host')
        issues.append(f'{inet_bridge} bridge missing')
        return issues
    ip_m = re.search(r'inet ([0-9.]+)/(\d+)', r.stdout)
    if ip_m:
        host_ip = ip_m.group(1)
        print(f'  {ok(inet_bridge + " bridge")}  host IP={host_ip}/{ip_m.group(2)}')
    else:
        print(f'  {warn(inet_bridge + " bridge")}  exists but no IP assigned')
        issues.append(f'{inet_bridge} has no IP')
        host_ip = None

    if _is_running(super_cname) and host_ip:
        stdout, _, rc = _docker_exec(super_cname, ['ping', '-c1', '-W2', host_ip], timeout=5)
        rtt_m = re.search(r'time=([0-9.]+)', stdout)
        rtt   = f'{float(rtt_m.group(1)):.3f}ms' if rtt_m else 'timeout'
        label = f'super-spine → host ({host_ip})'
        if rc == 0:
            print(f'  {ok(label)}  rtt={rtt}')
        else:
            print(f'  {warn(label)}  {rtt}  (non-critical)')

    k_out, _, k_rc = _docker_exec(super_cname, ['ip', 'route', 'show', 'default'])
    v_out, _, v_rc = _docker_exec(super_cname, ['vtysh', '-c', 'show ip route 0.0.0.0/0'])
    has_default = (k_rc == 0 and k_out.strip()) or \
                  (v_rc == 0 and ('0.0.0.0/0' in v_out or 'default' in v_out))
    route_line  = k_out.strip().splitlines()[0] if k_out.strip() else ''
    if has_default:
        print(f'  {ok("default route in super-spine")}  {route_line}')
    else:
        print(f'  {fail("default route in super-spine")}  no default route')
        issues.append('super-spine has no default route')

    # Use TCP (port 80) instead of ICMP — ICMP is often blocked on corporate networks
    _, _, rc = _docker_exec(
        super_cname,
        ['python3', '-c',
         "import socket; s=socket.create_connection(('1.1.1.1',80),timeout=5); print('ok')"],
        timeout=8,
    )
    if rc == 0:
        print(f'  {ok("super-spine → internet (1.1.1.1:80 TCP)")}')
    else:
        print(f'  {fail("super-spine → internet (1.1.1.1:80 TCP)")}  timeout')
        issues.append('super-spine cannot reach internet')

    return issues


# ── Check 5: DNS ─────────────────────────────────────────────────────────────

def check_dns(lab_name, dns_servers, hostname='archive.ubuntu.com'):
    print(bold('\n═══ Check 5: DNS Resolution ═══'))
    issues      = []
    super_cname = _cname(lab_name, 'super-spine')

    if not _is_running(super_cname):
        print(f'  {warn("super-spine not running — skipping")}')
        return issues

    # Use python3 socket (available in FRR container) — avoids dependency on
    # nslookup/dig and skips explicit nameserver queries that NVIDIA blocks on port 53.
    stdout, stderr, rc = _docker_exec(
        super_cname,
        ['python3', '-c',
         f"import socket; print(socket.gethostbyname('{hostname}'))"],
        timeout=8,
    )
    ip = stdout.strip()
    if rc == 0 and ip:
        print(f'  {ok(hostname):40s}  resolved → {ip}')
    else:
        err = stderr.strip().splitlines()[-1] if stderr.strip() else 'no response'
        print(f'  {fail(hostname):40s}  {err}')
        issues.append(f'DNS: {hostname} unresolvable')

    return issues


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(container_issues, bgp_issues, ping_issues, inet_issues,
                  dns_issues, no_ping, switch_only):
    all_issues = container_issues + bgp_issues + ping_issues + inet_issues + dns_issues
    total      = len(all_issues)

    print(bold('\n═══ Summary ═══'))
    if all_issues:
        for issue in all_issues:
            print(f'  {fail(issue)}')
        print()

    c_ok = '✓' if not container_issues else '✗'
    b_ok = '✓' if not bgp_issues       else '✗'
    p_ok = ('✓' if not ping_issues else '✗') if not no_ping else 'skipped'
    i_ok = '✓' if not inet_issues else '✗'
    d_ok = '✓' if not dns_issues  else '✗'

    b_note = '  (non-switch peers ignored)' if switch_only else ''
    print(f'  containers     : {c_ok}  ({len(container_issues)} issues)')
    print(f'  BGP peers      : {b_ok}  ({len(bgp_issues)} issues){b_note}')
    print(f'  loopbacks      : {p_ok}  ({len(ping_issues)} issues)')
    print(f'  internet uplink: {i_ok}  ({len(inet_issues)} issues)')
    print(f'  DNS resolution : {d_ok}  ({len(dns_issues)} issues)')
    print()

    healthy = total == 0
    if healthy:
        parts = []
        if not no_ping:
            parts.append('all loopbacks reachable')
        if not inet_issues:
            parts.append('internet reachable')
        if not dns_issues:
            parts.append('DNS resolving')
        detail = ' — all containers running, all BGP Established' + \
                 (', ' + ', '.join(parts) if parts else '')
        if switch_only:
            detail += ' (switch-only)'
        print(bold(_green(f'FABRIC HEALTH: ✓ HEALTHY{detail}')))
    else:
        print(bold(_red(f'FABRIC HEALTH: ✗ UNHEALTHY — {total} issue(s) found')))

    return healthy


# ── Entry point ───────────────────────────────────────────────────────────────

def run_checks(site_data, no_ping=False, switch_only=False,
               dns_host='archive.ubuntu.com', json_out=False):
    """Run all fabric verification checks for a site. Returns True if healthy.

    json_out=True: human-readable progress goes to stderr; a structured JSON
    summary is printed to stdout (for the web UI / scripting consumers).
    """
    if json_out:
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            healthy, issues = _run_checks_impl(site_data, no_ping, switch_only, dns_host)
        out = {
            'healthy': healthy,
            'checks': {
                'containers':      {'ok': not issues['containers'], 'issues': issues['containers']},
                'bgp':             {'ok': not issues['bgp'], 'issues': issues['bgp'],
                                    'switch_only': switch_only},
                'loopbacks':       ({'skipped': True} if no_ping else
                                    {'ok': not issues['loopbacks'], 'issues': issues['loopbacks']}),
                'internet_uplink': {'ok': not issues['internet'], 'issues': issues['internet']},
                'dns':             {'ok': not issues['dns'], 'issues': issues['dns'],
                                    'hostname': dns_host},
            },
        }
        print(json.dumps(out, indent=2))
        return healthy
    healthy, _ = _run_checks_impl(site_data, no_ping, switch_only, dns_host)
    return healthy


def _run_checks_impl(site_data, no_ping, switch_only, dns_host):
    """Returns (healthy, {check_name: [issue strings]})."""
    empty = {'containers': [], 'bgp': [], 'loopbacks': [], 'internet': [], 'dns': []}
    site_folder = site_data.get('_site_folder', '')
    topo_path   = str(Path(site_folder) / 'fabric' / 'topo.clab.yml')

    if not Path(topo_path).exists():
        print(f'Error: topology not found: {topo_path}', flush=True)
        print('  Run generate-dev-fabric.py + deploy-dev-fabric.py first.')
        empty['containers'] = [f'topology not found: {topo_path}']
        return False, empty

    topo = _load_topo(topo_path)
    lab_name = topo.get('name', site_data.get('clab_name', 'dev'))
    all_switches, _, spines, leafs, dpu_nodes = _get_switch_nodes(topo)

    sim         = site_data.get('_sim', {})
    dns_servers = sim.get('fabric', {}).get('dns_servers', [])

    print(bold(f'DC Fabric Health Check — lab: {lab_name}'))
    print(f'  topology : {topo_path}')
    print(f'  nodes    : 1 super-spine, {len(spines)} spines, {len(leafs)} leafs, '
          f'{len(dpu_nodes)} dpu ({len(all_switches) + len(dpu_nodes)} total)')
    print(f'  DNS      : container resolver (fabric nodes inherit Docker DNS '
          f'→ VM resolver)')
    has_uplink = _inet_bridge(topo) is not None
    if has_uplink:
        print(f'  internet : uplink detected ({_inet_bridge(topo)})')
    if switch_only:
        print('  mode     : switch-only (non-switch peers not counted as failures)')

    container_issues = check_containers(lab_name, all_switches + dpu_nodes)
    bgp_issues       = check_bgp(lab_name, all_switches, switch_only=switch_only)
    ping_issues      = [] if no_ping else check_loopback_reachability(lab_name, all_switches)
    inet_issues      = check_internet_uplink(lab_name, topo) if has_uplink else []
    dns_issues       = check_dns(lab_name, dns_servers, hostname=dns_host)

    healthy = print_summary(container_issues, bgp_issues, ping_issues,
                            inet_issues, dns_issues, no_ping, switch_only)
    return healthy, {'containers': container_issues, 'bgp': bgp_issues,
                     'loopbacks': ping_issues, 'internet': inet_issues,
                     'dns': dns_issues}
