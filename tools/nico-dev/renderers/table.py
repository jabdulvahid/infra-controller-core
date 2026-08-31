"""Human-readable table renderer for ndev contexts — standalone, no nsim imports."""

GREEN  = '\033[32m'
RED    = '\033[31m'
YELLOW = '\033[33m'
CYAN   = '\033[36m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

def ok(s):   return f'{GREEN}✓ {s}{RESET}'
def fail(s): return f'{RED}✗ {s}{RESET}'
def warn(s): return f'{YELLOW}⚠ {s}{RESET}'
def bold(s): return f'{BOLD}{s}{RESET}'
def dim(s):  return f'{DIM}{s}{RESET}'
def hdr(s):  return f'{CYAN}{BOLD}{s}{RESET}'


def _health(ok_count, total, label=''):
    if total == 0:
        return dim('n/a')
    if ok_count == total:
        return ok(f'{ok_count}/{total}{" " + label if label else ""}')
    elif ok_count == 0:
        return fail(f'{ok_count}/{total}{" " + label if label else ""}')
    else:
        return warn(f'{ok_count}/{total}{" " + label if label else ""}')


def render_info(site, fabric=None, dpu=None, cluster=None, bgp=None):
    """Render the top-level ndev <site> summary."""
    W = 47
    lines = []
    lines.append(bold(f'\n{"─"*W}'))
    lines.append(bold(f'  ndev — {site["site_id"]}'))
    lines.append(f'{"─"*W}')
    lines.append(f'  {"dc_name":<18} {site["dc_name"]}')
    lines.append(f'  {"sitename":<18} {site["sitename"]}')
    lines.append(f'  {"api_vip":<18} {site["api_vip"] or dim("not set")}')
    lines.append(f'  {"registry":<18} {site["registry"] or dim("not set")}')
    lines.append(f'  {"kubeconfig":<18} {site["_kubeconfig"] or dim("not found")}')
    lines.append('')

    if fabric:
        sw_ok  = fabric['running_switches']
        sw_tot = fabric['total_switches']
        bgp_h  = _health(fabric['ss_bgp_estab'], fabric['ss_bgp_total'], 'BGP peers')
        leafs  = fabric.get('leafs', [])
        spines = fabric.get('spines', [])

        lines.append(f'  {"fabric":<18} {_health(sw_ok, sw_tot, "switches")}  super-spine: {bgp_h}')
        lines.append(f'  {"":18} {"├── super-spine":25} 1')
        lines.append(f'  {"":18} {"├── spine":25} {len(spines)}')
        leaf_names = ', '.join(leafs) if leafs else ''
        lines.append(f'  {"":18} {"└── leafs":25} {len(leafs)}  ({leaf_names})')

    if dpu and dpu['running'] is None:
        # Off-host (Mac): the DPU container is only visible from the VM.
        lines.append(f'  {"DPU stand-in":<18} '
                     + dim('n/a (VM-side — run ndev on the VM)'))
    elif dpu:
        run_icon  = ok('') if dpu['running'] else fail('')
        ip_fwd    = dpu.get('ip_forward')
        fwd_str   = f'ip_forward: {ip_fwd}' if ip_fwd is not None else dim('ip_forward: ?')
        bgp_str   = f'BGP: {dpu["bgp_estab"]}/{dpu["bgp_total"]} established'
        stat_str  = 'running' if dpu['running'] else 'stopped'
        lines.append(
            f'  {"DPU stand-in":<18} {run_icon}{stat_str}  {bgp_str}  {fwd_str}'
        )

    if cluster:
        if cluster.get('error') and not cluster.get('node_name'):
            lines.append(f'  {"k8s node":<18} {warn(cluster["error"])}')
        else:
            run_icon = ok('') if cluster['ready'] else fail('')
            status   = 'Ready' if cluster['ready'] else 'NotReady'
            ver      = cluster.get('version', '')
            ip       = cluster.get('node_ip', '')
            pods     = cluster.get('pod_count', 0)
            lines.append(
                f'  {"k8s node":<18} {run_icon}{status}  {ver}  IP: {ip}  pods: {pods}'
            )

    if bgp:
        lines.append(
            f'  {"BGP sessions":<18} {_health(bgp["established"], bgp["total_sessions"])}'
        )

    lines.append(f'{"─"*W}\n')
    return '\n'.join(lines)


def render_fabric(data, detail=False):
    lines = [bold('\n══ Fabric ══════════════════════════════════════')]
    if data.get('error'):
        lines.append(f'\n  {warn(data["error"])}\n')
        return '\n'.join(lines)

    lines.append(hdr('\nContainerLab containers:'))
    lines.append(f'  {"Node":<22} {"Status":<12} {"Image"}')
    lines.append(f'  {"─"*22} {"─"*12} {"─"*20}')
    for c in data['containers']:
        status = ok('running') if c['running'] else fail('stopped')
        lines.append(f'  {c["node"]:<22} {status:<20} {dim(c["image"])}')

    if data['bridges']:
        lines.append(hdr('\nLinux bridges:'))
        for b in data['bridges']:
            state = ok(b['state']) if b['state'] == 'UP' else warn(b['state'])
            lines.append(f'  {b["name"]:<30} {state}')

    ss_e = data['ss_bgp_estab']
    ss_t = data['ss_bgp_total']
    lines.append(hdr('\nBGP (super-spine):'))
    lines.append(f'  {_health(ss_e, ss_t, "peers established")}')

    if detail and data.get('bgp_detail'):
        lines.append(hdr('\nBGP detail per switch:'))
        for node, d in sorted(data['bgp_detail'].items()):
            lines.append(f'\n  {bold(node)}')
            if d.get('error'):
                lines.append(f'    {fail("not running")}')
            else:
                for line in d.get('raw', '').splitlines():
                    if line.strip():
                        lines.append(f'    {line}')

    lines.append('')
    return '\n'.join(lines)


def render_dpu(data, detail=False):
    lines = [bold('\n══ DPU Stand-in ═════════════════════════════════')]

    run_icon = ok('') if data['running'] else fail('')
    lines.append(f'\n  {run_icon}{data["name"]}')
    lines.append(f'  {"running":<18} {data["running"]}')
    if data.get('fabric_ip'):
        lines.append(f'  {"fabric_ip":<18} {data["fabric_ip"]}')
    if data.get('cp_link_ip'):
        lines.append(f'  {"cp_link_ip":<18} {data["cp_link_ip"]}')

    bgp_h = _health(data['bgp_estab'], data['bgp_total'], 'established')
    lines.append(f'  {"BGP":<18} {bgp_h}')

    ip_fwd = data.get('ip_forward')
    if ip_fwd is not None:
        fwd_str = ok(str(ip_fwd)) if ip_fwd == 1 else fail(str(ip_fwd))
        lines.append(f'  {"ip_forward":<18} {fwd_str}')

    lines.append('')
    return '\n'.join(lines)


def render_cluster(data, detail=False):
    lines = [bold('\n══ k8s Node ═════════════════════════════════════')]

    if data.get('error') and not data.get('node_name'):
        lines.append(f'\n  {warn(data["error"])}')
        lines.append('')
        return '\n'.join(lines)

    run_icon = ok('') if data['ready'] else fail('')
    lines.append(f'\n  {run_icon}{data["node_name"]}')
    lines.append(f'  {"status":<18} {"Ready" if data["ready"] else "NotReady"}')
    lines.append(f'  {"version":<18} {data["version"]}')
    lines.append(f'  {"node_ip":<18} {data["node_ip"]}')
    lines.append(f'  {"pods":<18} {data["pod_count"]} total')
    lines.append(f'  {"kubeconfig":<18} {dim(data["kubeconfig"] or "not found")}')

    lines.append('')
    return '\n'.join(lines)


def render_registry(data):
    lines = [bold('\n══ Registry ═════════════════════════════════════')]
    reg = data['registry']
    if data['reachable']:
        lines.append(f'\n  {ok(reg)}  reachable')
    else:
        lines.append(f'\n  {fail(reg)}  {data["reach_error"]}')

    if data['on_vm'] and data['containerd_ok'] is not None:
        if data['containerd_ok']:
            lines.append(f'  {ok("containerd")}  insecure registry configured')
        else:
            lines.append(f'  {fail("containerd")}  {data["containerd_err"]}')

    images = data.get('images', [])
    if images:
        lines.append('')
        lines.append(hdr('  Images:'))
        for img in images:
            tags = ', '.join(img['tags']) if img['tags'] else dim('(no tags)')
            lines.append(f'    {img["repo"]:<30} {tags}')
    elif data['reachable']:
        lines.append(f'\n  {warn("registry is empty — no images pushed yet")}')

    lines.append('')
    return '\n'.join(lines)


def render_bgp(data, detail=False):
    lines = [bold('\n══ BGP ══════════════════════════════════════════')]
    if data.get('error'):
        lines.append(f'\n  {warn(data["error"])}\n')
        return '\n'.join(lines)
    lines.append(f'\n  Total sessions : {data["total_sessions"]}')
    lines.append(f'  Established    : {ok(str(data["established"]))}')
    if data['not_established'] > 0:
        lines.append(f'  Not established: {fail(str(data["not_established"]))}')

    if detail:
        lines.append(hdr('\nPer-node detail:'))
        for node, nd in sorted(data['nodes'].items()):
            peers = nd.get('peers', [])
            e = sum(1 for p in peers
                    for af in p.get('afs', {}).values()
                    if af.get('established'))
            t = sum(len(p.get('afs', {})) for p in peers)
            lines.append(f'\n  {bold(node)}  {_health(e, t)}')
            for peer in peers:
                for af, af_data in peer.get('afs', {}).items():
                    af_short = {'ipv4 unicast': 'ipv4', 'l2vpn evpn': 'evpn'}.get(af, af)
                    if af_data.get('established'):
                        icon = ok('')
                    elif af_data.get('no_neg'):
                        icon = warn('')
                    else:
                        icon = fail('')
                    lines.append(
                        f'    {icon}{peer["ip"]:<18} {af_short:<6} '
                        f'{af_data["state"]:<20} {dim("up=" + af_data["updown"])} '
                        f'{dim(peer.get("desc",""))}')

    lines.append('')
    return '\n'.join(lines)
