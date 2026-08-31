#!/usr/bin/env python3
"""
DC Simulation — ContainerLab fabric topology generator

Reads nico-sim.yaml and generates a complete ContainerLab topology:
  - 1 super-spine, 2 spines, N leafs — FRR Linux containers
  - Bridge nodes: br-cp1..M (CP VM uplinks), br-helper, br-spare-N
  - Per-node /etc/frr/frr.conf and /etc/frr/daemons configs
  - eBGP underlay with separate switch and server underlay prefixes

Usage:
  python3 generate-fabric.py nico-sim.yaml [--output-dir ./output]

Output layout:
  <output-dir>/
    topo.clab.yml
    deploy.sh            (creates bridges + deploys topology)
    vm-reference.txt     (VM IP/ASN assignments for generate-vms.py)
    nodes/
      super-spine/  spine-1/  spine-2/  leaf-1/ ...
        frr.conf
        daemons
"""

import argparse
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sim_validate import validate_sim

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: pip3 install pyyaml', file=sys.stderr)
    sys.exit(1)

NUM_SPINES = 2  # fixed at 2


# ── Site resolver ─────────────────────────────────────────────────────────────

def resolve_site(arg):
    """If arg is a directory, auto-discover site yaml and fabric paths within it.
    Returns (sim_yaml_path, fabric_dir, vm_dir) as strings.
    If arg is a file, returns (arg, parent/fabric, parent/vm).
    """
    p = Path(arg).expanduser()
    if p.is_dir():
        # Find site yaml: *.yaml but not *-mac.yaml
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml found in {p}', file=sys.stderr)
            sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple site yamls in {p}: {[f.name for f in yamls]}', file=sys.stderr)
            sys.exit(1)
        return str(yamls[0]), str(p / 'fabric'), str(p / 'vm')
    else:
        parent = p.parent
        return str(p), str(parent / 'fabric'), str(parent / 'vm')


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(yaml_path):
    """Load and validate nico-sim.yaml, return a Config object."""
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    fab = data.get('fabric', {})
    pfx = fab.get('prefixes', {})

    class Config:
        pass

    c = Config()
    c.dc_name           = fab.get('dc_name', fab.get('lab_name', 'nico-sim'))
    c.num_leafs         = fab.get('num_leafs', 4)
    c.num_cp_leafs      = fab.get('num_control_plane_leafs', 3)
    c.image             = fab.get('image', 'frrouting/frr:latest')
    c.switch_underlay   = pfx.get('switch_underlay', '7.128.0.0/16')
    c.dpu_fabric        = pfx.get('dpu_fabric',      '7.130.0.0/16')
    cp = data.get('control_plane', {})
    c.control_plane_prefix = cp.get('control_plane_prefix', pfx.get('control_plane_prefix', '7.132.1.0/24'))
    c.loopback_prefix      = pfx.get('switch_loopbacks', '7.129.0.0/16')
    c.dpu_loopback_prefix  = pfx.get('dpu_loopbacks', '7.131.0.0/16')
    c.mgmt_prefix          = pfx.get('mgmt', '172.20.20.0/24')
    c.switch_asn_base      = fab.get('switch_asn_base', fab.get('asn_base', 65000))
    c.dpu_asn_base         = fab.get('dpu_asn_base', fab.get('asn_base', 65000) + 20)
    c.datacenter_asn       = fab.get('datacenter_asn', c.switch_asn_base - 1)
    c.internet_uplink   = fab.get('internet_uplink', {'enabled': False})
    c.registry_link     = fab.get('registry_link', fab.get('utility_link', {'enabled': False}))
    c.evpn              = fab.get('evpn', {'enabled': False})
    c.evpn_vnis         = fab.get('evpn_vnis', {})
    c.underlay_leafs    = fab.get('underlay_leafs', {})
    c.service_vnis      = fab.get('service_vnis', [])
    # service_vips lives in nico-system.helm-values.net-plan; also check fabric.prefixes
    _np = data.get('nico-system', {}).get('helm-values', {}).get('net-plan', {})
    c.service_vips      = _np.get('service_vips') or pfx.get('service_vips', None)
    c.underlay_pool     = pfx.get('underlay_pool', None)
    # dns_servers is the single source of truth for all DNS in the simulation:
    # dnsmasq upstream on br-internet, VM resolv.conf fallback, OOB forwarders
    c.dns_servers       = data.get('control_plane', {}).get(
                              'dns_servers', ['8.8.8.8', '8.8.4.4'])
    # ── Managed-host configuration ─────────────────────────────────────────────
    # Consumed by build_topology to generate DHCP relay containers for MH racks.
    nico_sys             = data.get('nico-system', {})
    helm_values          = nico_sys.get('helm-values', {})
    c.managed_hosts        = data.get('managed_hosts', {})
    c.mh_underlay_networks = helm_values.get('networks', {})
    c.dhcp_vip             = helm_values.get('net-plan', {}).get('dhcp_vip', '')
    # underlay: explicit map of underlay_name → leaf_name from nico-system.underlay
    c.underlay_leaf_map    = nico_sys.get('underlay', {})
    c._raw                 = data   # kept for validate_sim()

    # ── Derive mat leaf name and underlay prefix from underlay_leafs ──────────
    # The mat leaf is the one with a host bridge (relay: false, bridge: set).
    c.mat_leaf_name    = None
    c.mat_leaf_bridge  = None
    c.mat_underlay_net = None
    c.mat_gateway_ip   = None
    for uname, ucfg in c.underlay_leafs.items():
        if isinstance(ucfg, dict) and ucfg.get('bridge') and not ucfg.get('relay', True):
            c.mat_leaf_name   = ucfg.get('leaf', 'leaf-mat')
            c.mat_leaf_bridge = ucfg.get('bridge', 'br-internet')
            net_cfg = c.mh_underlay_networks.get(uname, {})
            c.mat_underlay_net = net_cfg.get('prefix')
            c.mat_gateway_ip   = net_cfg.get('gateway')
            break

    # oob_routing_mode: how managed host OOB/BMC prefixes reach Nico-api.
    #   underlay_local — leaf-mat gets the gateway IP on eth3; advertises prefix
    #                    into BGP so the fabric routes directly to it. Simple.
    #   overlay_evpn   — prefix exported into EVPN VNI 900 (RT {dc_asn}:900);
    #                    DPU stand-ins import via vpc_{ControlPlaneVNI} VRF and
    #                    leak to default VRF. Matches production SMN architecture.
    #                    NOT YET IMPLEMENTED — see nico-sim context doc dated 2026-08-13.
    c.oob_routing_mode = data.get('managed_hosts', {}).get('oob_routing_mode', 'underlay_local')

    if c.num_leafs < 3:
        print(f'Error: fabric.num_leafs must be at least 3, got {c.num_leafs}', file=sys.stderr)
        sys.exit(1)
    if c.num_cp_leafs > c.num_leafs:
        print(f'Error: fabric.num_control_plane_leafs ({c.num_cp_leafs}) '
              f'> fabric.num_leafs ({c.num_leafs})', file=sys.stderr)
        sys.exit(1)

    return c


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate ContainerLab fabric topology from nico-sim.yaml',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder or nico-sim yaml file')
    p.add_argument('--output-dir', default=None,
                   help='Output dir (default: {site_folder}/fabric)')
    return p.parse_args()


# ── IP helpers ────────────────────────────────────────────────────────────────

def slash31_pairs(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for subnet in net.subnets(new_prefix=31):
        hosts = list(subnet.hosts())
        yield str(hosts[0]), str(hosts[1])


def slash32_hosts(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for host in net.hosts():
        yield str(host)


def mgmt_host(prefix_str, offset):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    return str(list(net.hosts())[offset])


# ── Topology builder ──────────────────────────────────────────────────────────

def build_topology(cfg):
    N            = cfg.num_leafs
    num_cp_leafs = cfg.num_cp_leafs

    sw_pool  = slash31_pairs(cfg.switch_underlay)
    srv_pool = slash31_pairs(cfg.dpu_fabric)
    lb_pool  = slash32_hosts(cfg.loopback_prefix)

    sw_base  = cfg.switch_asn_base
    dpu_base = cfg.dpu_asn_base

    # Build leaf names: numbered CP/MH leafs + mat leaf substituted for the last slot.
    leaf_names = []
    for i in range(1, N + 1):
        if cfg.mat_leaf_name and i == N:
            leaf_names.append(cfg.mat_leaf_name)   # e.g. 'leaf-mat' replaces leaf-N
        else:
            leaf_names.append(f'leaf-{i}')
    switch_names = ['super-spine', 'spine-1', 'spine-2'] + leaf_names

    asns = {'super-spine': sw_base, 'spine-1': sw_base+1, 'spine-2': sw_base+2}
    for i, lname in enumerate(leaf_names, start=1):
        asns[lname] = sw_base + 10 + i
    for i in range(1, num_cp_leafs+1):
        asns[f'cp-{i}'] = dpu_base + i          # DPU stand-in ASN
    asns['helper'] = dpu_base + num_cp_leafs + 1  # helper VM ASN

    loopbacks = {}
    mgmt_ips  = {}
    for idx, name in enumerate(switch_names):
        loopbacks[name] = next(lb_pool)
        mgmt_ips[name]  = mgmt_host(cfg.mgmt_prefix, 9 + idx)

    raw_links = []

    # super-spine ↔ spines
    for s in range(1, NUM_SPINES+1):
        ip_a, ip_b = next(sw_pool)
        raw_links.append((
            'super-spine', f'eth{s}',
            f'spine-{s}',  'eth1',
            ip_a, ip_b,
            f'super-spine:eth{s} ↔ spine-{s}:eth1  [{ip_a}/31]',
        ))

    # spines ↔ leafs
    for s in range(1, NUM_SPINES+1):
        for idx, lname in enumerate(leaf_names, start=1):
            ip_a, ip_b = next(sw_pool)
            spine_eth = idx + 1
            raw_links.append((
                f'spine-{s}', f'eth{spine_eth}',
                lname,         f'eth{s}',
                ip_a, ip_b,
                f'spine-{s}:eth{spine_eth} ↔ {lname}:eth{s}  [{ip_a}/31]',
            ))

    # leafs ↔ VM bridge nodes (server underlay)
    bridge_nodes = []

    # CP leafs → br-{dc_name}-cp1..M
    for i in range(1, num_cp_leafs+1):
        ip_a, ip_b = next(srv_pool)
        lname        = leaf_names[i-1]
        bridge       = f'br-{cfg.dc_name}-cp{i}'
        bridge_iface = f'eth-cp{i}'
        bridge_nodes.append(bridge)
        raw_links.append((
            lname,  'eth3',
            bridge,  bridge_iface,
            ip_a, ip_b,
            f'{lname}:eth3 ↔ {bridge}:{bridge_iface} (cp-{i} VM)  [{ip_a}/31]',
        ))

    # Leafs after CP leafs: MH rack relay containers (if configured) or br-helper/br-spare.
    #
    # When managed_hosts.num_vms > 0 and underlay networks are defined:
    #   - Each MH leaf gets a DHCP relay container (alpine:latest) and a bridge for MH VMs.
    #   - relay-rack-{i}: linux container; eth1 → br-mh-rack-{r} (underlay), eth2 → leaf
    #   - br-mh-rack-{r}: bridge node; MH VMs also attach here via libvirt
    #   - The relay runs dhcrelay -i eth1 -U eth2 {dhcp_vip}
    #   - The leaf gets an eth-relay interface (/31 from dpu_fabric pool) but NO BGP neighbor
    #     for the relay — the relay does not run FRR.
    #
    # When not configured: falls back to br-helper and br-spare-N (original behavior).
    relay_nodes  = {}   # relay container metadata; returned in topo dict and used by gen_topo
    mh_cfg       = getattr(cfg, 'managed_hosts', {}) or {}
    mh_networks  = getattr(cfg, 'mh_underlay_networks', {}) or {}
    dhcp_vip     = getattr(cfg, 'dhcp_vip', '') or ''
    _num_vms = mh_cfg.get('num_vms', {})
    _total_mh = (sum(_num_vms.values()) if isinstance(_num_vms, dict) else int(_num_vms))
    use_mh_relay = bool(_total_mh > 0 and mh_networks)

    helper_leaf = num_cp_leafs + 1
    for idx in range(helper_leaf, N + 1):
        lname        = leaf_names[idx - 1]     # actual leaf name (e.g. 'leaf-4', 'leaf-mat')
        rack_index   = idx - num_cp_leafs      # 1-indexed rack
        rack_net_key = f'rack-leaf-{idx}'
        rack_net     = mh_networks.get(rack_net_key, {})
        ip_a, ip_b   = next(srv_pool)

        # Is this the mat leaf? (named leaf with host bridge, no relay)
        is_mat_leaf = (lname == cfg.mat_leaf_name and cfg.mat_leaf_bridge)

        if is_mat_leaf:
            # leaf-mat: connects to the internet bridge node as an additional endpoint.
            # 'host:bridge' in ContainerLab means "create an interface NAMED bridge in
            # host namespace" — NOT "attach to existing bridge". Since the bridge already
            # exists (created by deploy.sh), we use the bridge as a ContainerLab bridge
            # NODE instead, adding eth-mat as a second endpoint alongside eth-uplink.
            # MAT IP aliases live on this bridge; leaf-mat gets eth3 on the bridge side.
            mat_bridge = cfg.mat_leaf_bridge
            if mat_bridge not in bridge_nodes:
                bridge_nodes.append(mat_bridge)
            raw_links.append((
                lname,      'eth3',
                mat_bridge,  'eth-mat',
                ip_a, ip_b,
                f'{lname}:eth3 ↔ {mat_bridge}:eth-mat (MAT BMC mocks, API-mode)  [{ip_a}/31]',
            ))
            # mat leaf info stored for FRR config (underlay route advertisement)
            relay_nodes[f'mat-{lname}'] = {
                'name':            lname,
                'is_mat':          True,
                'bridge':          mat_bridge,
                'rack_index':      rack_index,
                'leaf_index':      idx,
                'underlay_prefix': cfg.mat_underlay_net or '',
                'leaf_relay_ip':   ip_a,
            }

        elif use_mh_relay and rack_net:
            # MH rack: relay container + br-mh-rack bridge
            relay_name      = f'relay-rack-{idx}'
            bridge_name     = f'br-{cfg.dc_name}-mh{rack_index}'
            underlay_prefix = rack_net.get('prefix', '')
            underlay_gw     = rack_net.get('gateway', '')
            pfx_len         = (ipaddress.IPv4Network(underlay_prefix, strict=False).prefixlen
                               if underlay_prefix else 24)

            bridge_nodes.append(bridge_name)

            raw_links.append((
                lname,      'eth-relay',
                relay_name,  'eth2',
                ip_a, ip_b,
                f'{lname}:eth-relay ↔ {relay_name}:eth2  [{ip_a}/31]',
            ))
            raw_links.append((
                relay_name,   'eth1',
                bridge_name,  f'eth-mh-{rack_index}',
                '', '',
                f'{relay_name}:eth1 ↔ {bridge_name} (MH rack {rack_index} underlay bridge)',
            ))

            relay_nodes[relay_name] = {
                'name':             relay_name,
                'bridge':           bridge_name,
                'rack_index':       rack_index,
                'leaf_index':       idx,
                'underlay_prefix':  underlay_prefix,
                'underlay_gw':      underlay_gw,
                'underlay_pfx_len': pfx_len,
                'relay_ip':         ip_b,
                'leaf_relay_ip':    ip_a,
                'dhcp_vip':         dhcp_vip,
            }

        elif idx == helper_leaf:
            helper_bridge = f'br-{cfg.dc_name}-helper'
            bridge_nodes.append(helper_bridge)
            raw_links.append((
                lname,          'eth3',
                helper_bridge,  'eth-helper',
                ip_a, ip_b,
                f'{lname}:eth3 ↔ {helper_bridge}:eth-helper (helper VM)  [{ip_a}/31]',
            ))

        else:
            bridge       = f'br-{cfg.dc_name}-spare-{idx}'
            bridge_iface = f'eth-spare-{idx}'
            bridge_nodes.append(bridge)
            raw_links.append((
                lname,  'eth3',
                bridge,  bridge_iface,
                ip_a, ip_b,
                f'{lname}:eth3 ↔ {bridge}:{bridge_iface} (spare)  [{ip_a}/31]',
            ))

    # Per-node link index — built from switch links only (not internet uplink).
    # relay_node_names: relay containers don't run FRR, so the leaf must NOT create
    # a BGP neighbor for the relay IP.  The 'no_bgp' flag in the link dict tells
    # gen_frr to emit only the interface stanza, not the neighbor/activate lines.
    # mat entries in relay_nodes are keyed as 'mat-{lname}', not relay containers
    relay_container_names = {k for k, v in relay_nodes.items() if not v.get('is_mat')}
    node_links = {n: [] for n in switch_names}
    for (na, ia, nb, ib, ip_a, ip_b, comment) in raw_links:
        if ip_a == '' and ip_b == '':
            continue  # relay↔bridge links have no IP; skip for FRR config
        peer_b = nb if nb not in bridge_nodes else _bridge_vm_name(nb)
        peer_a = na
        if na in node_links:
            node_links[na].append({
                'local_iface': ia, 'peer': peer_b,
                'local_ip': ip_a, 'peer_ip': ip_b,
                'peer_asn': asns.get(_bridge_vm_name(nb), asns.get(nb, 0)),
                'comment': comment,
                'no_bgp': nb in relay_container_names,
            })
        if nb in node_links:
            node_links[nb].append({
                'local_iface': ib, 'peer': peer_a,
                'local_ip': ip_b, 'peer_ip': ip_a,
                'peer_asn': asns.get(na, 0),
                'comment': comment,
                'no_bgp': na in relay_container_names,
            })

    # ── Internet uplink (super-spine → br-internet → host → internet) ─────────
    # Kept separate so it doesn't create a spurious BGP neighbor entry.
    internet = None
    extra_links = []
    inet_cfg = cfg.internet_uplink
    if inet_cfg.get('enabled', False):
        inet_prefix = inet_cfg.get('prefix', '7.130.3.0/30')
        inet_hosts  = list(ipaddress.IPv4Network(inet_prefix, strict=False).hosts())
        host_ip     = str(inet_hosts[0])   # host side (.1)
        ss_ip       = str(inet_hosts[1])   # super-spine side (.2)
        ss_eth      = f'eth{NUM_SPINES + 1}'  # eth3 when NUM_SPINES=2

        inet_bridge = f'br-{cfg.dc_name}-internet'
        if inet_bridge not in bridge_nodes:
            bridge_nodes.append(inet_bridge)
        extra_links.append((
            'super-spine', ss_eth,
            inet_bridge,   'eth-uplink',
            host_ip, ss_ip,
            f'super-spine:{ss_eth} ↔ {inet_bridge} (internet uplink)  [{inet_prefix}]',
        ))
        internet = {
            'enabled':     True,
            'prefix':      inet_prefix,
            'host_ip':     host_ip,
            'ss_ip':       ss_ip,
            'ss_eth':      ss_eth,
            'inet_bridge': inet_bridge,
        }

    # ── Registry VM link (super-spine → br-registry → registry VM) ───────────
    # Kept separate so it doesn't create a spurious BGP neighbor entry.
    # super-spine gets .5, registry VM gets .6.
    registry = None
    reg_cfg = cfg.registry_link
    if reg_cfg.get('enabled', False):
        reg_prefix  = reg_cfg.get('prefix', '7.132.0.4/30')
        reg_hosts   = list(ipaddress.IPv4Network(reg_prefix, strict=False).hosts())
        ss_reg_ip   = str(reg_hosts[0])   # super-spine side
        reg_vm_ip   = str(reg_hosts[1])   # registry VM side
        ss_reg_eth  = f'eth{NUM_SPINES + 2}'  # eth4 when NUM_SPINES=2

        reg_bridge = f'br-{cfg.dc_name}-registry'
        if reg_bridge not in bridge_nodes:
            bridge_nodes.append(reg_bridge)
        extra_links.append((
            'super-spine', ss_reg_eth,
            reg_bridge,    'eth-registry',
            ss_reg_ip, reg_vm_ip,
            f'super-spine:{ss_reg_eth} ↔ {reg_bridge} (registry VM link)  [{reg_prefix}]',
        ))
        registry = {
            'enabled':  True,
            'prefix':   reg_prefix,
            'ss_ip':    ss_reg_ip,
            'vm_ip':    reg_vm_ip,
            'ss_eth':   ss_reg_eth,
        }

    nodes = {}
    for name in switch_names:
        nodes[name] = {
            'asn':      asns[name],
            'loopback': loopbacks[name],
            'mgmt_ip':  mgmt_ips[name],
            'links':    node_links[name],
        }

    return {
        'nodes':           nodes,
        'bridge_nodes':    bridge_nodes,
        'raw_links':       raw_links + extra_links,
        'switch_names':    switch_names,
        'leaf_names':      leaf_names,
        'num_cp_leafs':    num_cp_leafs,
        'internet':        internet,
        'registry':        registry,
        'evpn_cfg':        cfg.evpn,
        'switch_asn_base': sw_base,
        'dpu_asn_base':    dpu_base,
        'vm_asns':         {k: v for k, v in asns.items() if k not in switch_names},
        'relay_nodes':     relay_nodes,
        'mat_leaf_name':   cfg.mat_leaf_name,
    }


def _bridge_vm_name(bridge):
    if bridge.endswith('-internet'):
        return 'host'
    if bridge.endswith('-registry'):
        return 'registry'
    if bridge.endswith('-helper'):
        return 'helper'
    # br-{dc_name}-cp{N}  — find last '-cp' followed by digits
    idx = bridge.rfind('-cp')
    if idx != -1 and bridge[idx + 3:].isdigit():
        return f'cp-{bridge[idx + 3:]}'
    # br-{dc_name}-mh{N}  — find last '-mh' followed by digits
    idx = bridge.rfind('-mh')
    if idx != -1 and bridge[idx + 3:].isdigit():
        return f'mh-{bridge[idx + 3:]}'
    return bridge


# ── File generators ───────────────────────────────────────────────────────────

def gen_topo(cfg, topo):
    lines = []
    lines.append(f'name: {cfg.dc_name}')
    lines.append('')
    lines.append('mgmt:')
    lines.append(f'  network: {cfg.dc_name}-mgmt')
    lines.append(f'  ipv4-subnet: {cfg.mgmt_prefix}')
    lines.append('')
    lines.append('topology:')
    lines.append('  defaults:')
    lines.append(f'    kind: linux')
    lines.append(f'    image: {cfg.image}')
    lines.append('')
    lines.append('  nodes:')
    lines.append('    # ── Network nodes (FRRouting) ──────────────────────────────────────────')
    evpn = topo.get('evpn_cfg')
    for name, info in topo['nodes'].items():
        is_leaf = name.startswith('leaf-')
        lines.append(f'')
        lines.append(f'    {name}:')
        lines.append(f'      mgmt-ipv4: {info["mgmt_ip"]}')
        lines.append(f'      binds:')
        lines.append(f'        - nodes/{name}/frr.conf:/etc/frr/frr.conf')
        lines.append(f'        - nodes/{name}/daemons:/etc/frr/daemons')
        exec_cmds = []
        if evpn and evpn.get('enabled') and is_leaf:
            lines.append(f'        - nodes/{name}/vxlan-setup.sh:/etc/frr/vxlan-setup.sh')
            exec_cmds.append('nohup sh /etc/frr/vxlan-setup.sh &')
        # underlay_local: add gateway IP to leaf-mat eth3 so FRR has a connected
        # route for the MAT underlay prefix and can advertise it via BGP.
        if (name == cfg.mat_leaf_name
                and cfg.oob_routing_mode == 'underlay_local'
                and cfg.mat_gateway_ip
                and cfg.mat_underlay_net):
            pfx_len = ipaddress.IPv4Network(cfg.mat_underlay_net, strict=False).prefixlen
            exec_cmds.append(f'ip addr add {cfg.mat_gateway_ip}/{pfx_len} dev eth3')
        if exec_cmds:
            lines.append(f'      exec:')
            for cmd in exec_cmds:
                lines.append(f'        - {cmd}')
    lines.append('')
    lines.append('    # ── Bridge nodes — expose leaf ports to KVM VMs ──────────────────────')
    for bridge in topo['bridge_nodes']:
        vm = _bridge_vm_name(bridge)
        lines.append(f'')
        lines.append(f'    {bridge}:')
        if bridge.endswith('-internet'):
            lines.append(f'      kind: bridge  # host internet uplink — host gets IP on this bridge')
        elif bridge.endswith('-registry'):
            lines.append(f'      kind: bridge  # registry VM attaches a TAP interface here')
        elif '-mh' in bridge:
            lines.append(f'      kind: bridge  # MH rack underlay — relay eth1 + KVM VM "{vm}" attach here')
        else:
            lines.append(f'      kind: bridge  # KVM VM "{vm}" attaches a TAP interface here')

    # ── DHCP relay containers (one per MH rack leaf) ──────────────────────────
    # relay-rack-{i}: alpine:latest linux container that:
    #   eth0 → br-mh-rack-{r}: holds the underlay gateway IP (giaddr injected into DHCP relay)
    #   eth1 → leaf-{i}:eth-relay: path to Nico DHCP VIP via the fabric
    # The leaf does NOT BGP-peer with the relay — 'no_bgp: true' in the leaf's link entry.
    relay_nodes = topo.get('relay_nodes', {})
    if relay_nodes:
        lines.append('')
        lines.append('    # ── DHCP relay containers (MH racks) ──────────────────────────────────')
        for rname, rinfo in relay_nodes.items():
            if rinfo.get('is_mat'):
                continue   # mat leaf is not a relay container — skip
            gw      = rinfo['underlay_gw']
            plen    = rinfo['underlay_pfx_len']
            rip     = rinfo['relay_ip']
            lvip    = rinfo['leaf_relay_ip']
            dvip    = rinfo['dhcp_vip']
            rack_r  = rinfo['rack_index']
            leaf_i  = rinfo['leaf_index']
            lines.append(f'')
            lines.append(f'    {rname}:')
            lines.append(f'      image: dc-sim-relay:latest')
            lines.append(f'      # Built by deploy.sh: alpine:3.18 + dhcp (provides dhcrelay).')
            lines.append(f'      # eth0 = ContainerLab management (reserved)')
            lines.append(f'      # eth1 = underlay bridge br-mh-rack-{rack_r}; giaddr = {gw}')
            lines.append(f'      # eth2 = leaf-{leaf_i} fabric uplink; relay path → Nico DHCP {dvip}')
            lines.append(f'      exec:')
            # dhcrelay is pre-installed in dc-sim-relay:latest — no apk install needed.
            # Interface config needs no shell. dhcrelay needs sh -c for & backgrounding.
            lines.append(f'        - "ip link set eth1 up"')
            lines.append(f'        - "ip addr add {gw}/{plen} dev eth1"')
            lines.append(f'        - "ip link set eth2 up"')
            lines.append(f'        - "ip addr add {rip}/31 dev eth2"')
            lines.append(f'        - "ip route add {dvip}/32 via {lvip} dev eth2"')
            relay_cmd = f"sh -c 'dhcrelay -i eth1 -U eth2 {dvip} &'"
            lines.append(f'        - "{relay_cmd}"')

    lines.append('')
    lines.append('  links:')
    for (na, ia, nb, ib, ip_a, ip_b, comment) in topo['raw_links']:
        if ib == '':
            # relay↔bridge link with no remote interface — skip (no IP, no clab endpoint)
            continue
        lines.append(f'    # {comment}')
        lines.append(f'    - endpoints: ["{na}:{ia}", "{nb}:{ib}"]')
    return '\n'.join(lines) + '\n'


def gen_frr(name, info, internet=None, registry=None, evpn=None,
            mat_leaf_info=None, datacenter_asn=None, evpn_vnis=None):
    lines = []
    lines.append('frr version 9.1')
    lines.append('frr defaults datacenter')
    lines.append(f'hostname {name}')
    lines.append('log syslog informational')
    lines.append('!')
    lines.append('interface lo')
    lines.append(f' ip address {info["loopback"]}/32')
    lines.append('!')
    for link in info['links']:
        lines.append(f'! {link["comment"]}')
        lines.append(f'interface {link["local_iface"]}')
        lines.append(f' ip address {link["local_ip"]}/31')
        lines.append('!')
    # Internet uplink interface (super-spine only)
    if name == 'super-spine' and internet:
        lines.append(f'! Internet uplink to host')
        lines.append(f'interface {internet["ss_eth"]}')
        lines.append(f' ip address {internet["ss_ip"]}/30')
        lines.append('!')
        lines.append(f'! Default route via host — redistributed into BGP as 0.0.0.0/0')
        lines.append(f'ip route 0.0.0.0/0 {internet["host_ip"]}')
        lines.append('!')
    # Registry VM link interface (super-spine only)
    if name == 'super-spine' and registry:
        lines.append(f'! Registry VM link (Docker registry:2 at {registry["vm_ip"]}:5000)')
        lines.append(f'interface {registry["ss_eth"]}')
        lines.append(f' ip address {registry["ss_ip"]}/30')
        lines.append('!')
    lines.append(f'router bgp {info["asn"]}')
    lines.append(f' bgp router-id {info["loopback"]}')
    lines.append(' bgp bestpath as-path multipath-relax')
    lines.append(' !')
    for link in info['links']:
        # Relay containers (no_bgp=True) get an interface stanza but no BGP neighbor:
        # the relay runs dhcrelay, not FRR, so there is no BGP session to establish.
        if link.get('no_bgp', False):
            continue
        lines.append(f' neighbor {link["peer_ip"]} remote-as {link["peer_asn"]}')
        lines.append(f' neighbor {link["peer_ip"]} description {link["peer"]}')
    lines.append(' !')
    lines.append(' address-family ipv4 unicast')
    lines.append(f'  network {info["loopback"]}/32')
    # Advertise default route to all BGP peers (super-spine only, when internet enabled)
    if name == 'super-spine' and internet:
        lines.append('  network 0.0.0.0/0')
    # Advertise registry subnet so fabric can route to registry VM (super-spine only)
    if name == 'super-spine' and registry:
        reg_net = ipaddress.IPv4Network(registry['prefix'], strict=False)
        lines.append(f'  network {reg_net.network_address}/30')
    # Advertise mat underlay prefix into BGP (leaf-mat only)
    if mat_leaf_info and name == mat_leaf_info.get('name') and mat_leaf_info.get('underlay_prefix'):
        u_net = ipaddress.IPv4Network(mat_leaf_info['underlay_prefix'], strict=False)
        lines.append(f'  network {u_net.network_address}/{u_net.prefixlen}  ! MAT underlay')
    # Leafs: also advertise the server-facing /31 so super-spine can route
    # return traffic back to VMs (e.g. NAT reply from host → fabric → VM)
    for link in info['links']:
        peer = link.get('peer', '')
        if peer.startswith('cp-') or peer == 'helper':
            lines.append(f'  network {link["local_ip"]}/31')
    for link in info['links']:
        if link.get('no_bgp', False):
            continue
        lines.append(f'  neighbor {link["peer_ip"]} activate')
    lines.append(' exit-address-family')
    lines.append('!')
    # ── EVPN address family ───────────────────────────────────────────────────
    if evpn and evpn.get('enabled', False):
        is_leaf = name.startswith('leaf-') or (mat_leaf_info and name == mat_leaf_info.get('name'))
        dc_asn  = datacenter_asn or 0
        vni_map = evpn_vnis or {}

        lines.append(' address-family l2vpn evpn')
        for link in info['links']:
            if not link.get('internet_uplink') and not link.get('no_bgp', False):
                lines.append(f'  neighbor {link["peer_ip"]} activate')
        if is_leaf:
            lines.append('  advertise-all-vni')
            # Export route-target for mat leaf (BMC routes)
            if mat_leaf_info and name == mat_leaf_info.get('name'):
                bmc_vni = vni_map.get('managed_node_bmc', 900)
                lines.append(f'  route-target export {dc_asn}:{bmc_vni}  ! managed_node_bmc')
                lines.append(f'  route-target import {dc_asn}:{vni_map.get("site_controller", 50100)}  ! site_controller')
        lines.append(' exit-address-family')
        lines.append('!')
    return '\n'.join(lines) + '\n'


def gen_vxlan_setup(loopback_ip, vnis):
    """
    Generate a startup script that creates VXLAN interfaces on a leaf node.
    Waits for the loopback IP to be assigned by FRR/zebra before creating
    the interface (zebra assigns IPs from frr.conf asynchronously at boot).
    """
    lines = []
    lines.append('#!/bin/sh')
    lines.append('# Generated by generate-fabric.py — VXLAN interface setup for EVPN')
    lines.append(f'# Waits for loopback {loopback_ip} then creates VXLAN interfaces.')
    lines.append(f'VTEP_IP="{loopback_ip}"')
    lines.append('i=0')
    lines.append('while [ $i -lt 60 ]; do')
    lines.append('  ip addr show lo | grep -q "$VTEP_IP" && break')
    lines.append('  sleep 1; i=$((i+1))')
    lines.append('done')
    for vni in vnis:
        lines.append(f'ip link add vxlan{vni} type vxlan id {vni} local "$VTEP_IP" '
                     f'dstport 4789 nolearning 2>/dev/null || true')
        lines.append(f'ip link set vxlan{vni} up 2>/dev/null || true')
    lines.append('# Signal FRR to re-scan interfaces')
    lines.append('# FRR detects new VXLAN interfaces automatically via netlink — no manual clear needed.')
    return '\n'.join(lines) + '\n'


def gen_daemons():
    return """\
zebra=yes
bgpd=yes
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
pathd=no
"""


def gen_vm_reference(topo, cfg):
    lines = []
    lines.append('# VM Network Reference')
    lines.append('# Generated by generate-fabric.py')
    lines.append('# Used by generate-nodes.py to reference VM/DPU assignments.')
    lines.append('')
    vm_asns = topo['vm_asns']
    for (vm, bridge, vm_ip, leaf_link) in _vm_links(topo):
        lines.append(f'## {vm}')
        lines.append(f'#   bridge:     {bridge}')
        lines.append(f'#   fabric_ip:  {vm_ip}/31')
        lines.append(f'#   peer_ip:    {_peer_ip(vm_ip)}')
        lines.append(f'#   peer_asn:   {_vm_peer_asn(bridge, topo)}')
        lines.append(f'#   dpu_asn:    {vm_asns.get(vm, 0)}')
        lines.append('')
    return '\n'.join(lines) + '\n'


def _vm_links(topo):
    for (na, ia, nb, ib, ip_a, ip_b, comment) in topo['raw_links']:
        if nb.startswith('br-') and not nb.endswith('-internet'):
            if not ip_b:
                # relay↔bridge links have no IP (relay assigns eth1 IP via startup exec)
                continue
            vm = _bridge_vm_name(nb)
            yield (vm, nb, ip_b, comment)


def _peer_ip(ip):
    addr = ipaddress.IPv4Address(ip)
    return str(addr - 1) if int(addr) % 2 == 1 else str(addr + 1)


def _vm_peer_asn(bridge, topo):
    for (na, ia, nb, ib, ip_a, ip_b, comment) in topo['raw_links']:
        if nb == bridge:
            return topo['nodes'][na]['asn']
    return 0


def gen_deploy_sh(topo, cfg):
    bridges     = topo['bridge_nodes']
    internet    = topo.get('internet')
    relay_nodes = topo.get('relay_nodes', {})
    inet_bridge = internet['inet_bridge'] if internet else f'br-{cfg.dc_name}-internet'
    lines = []
    lines.append('#!/usr/bin/env bash')
    lines.append('# Generated by generate-fabric.py — do not edit manually')
    lines.append('set -euo pipefail')
    lines.append('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"')
    lines.append('')
    lines.append('FORCE=0')
    lines.append('for arg in "$@"; do [ "$arg" = "--force" ] && FORCE=1; done')
    lines.append('')
    lines.append('echo "=== Creating Linux bridges ==="')
    for br in bridges:
        if br == inet_bridge:
            continue  # handled separately below with IP assignment
        lines.append(f'if ! ip link show {br} &>/dev/null; then')
        lines.append(f'  ip link add {br} type bridge')
        lines.append(f'  ip link set {br} up')
        lines.append(f'  echo "  created {br}"')
        lines.append(f'else')
        lines.append(f'  echo "  {br} already exists"')
        lines.append(f'fi')

    if internet:
        host_ip = internet['host_ip']
        prefix  = internet['prefix']
        lines.append('')
        lines.append(f'echo "=== Setting up internet uplink ({inet_bridge}) ==="')
        lines.append(f'if ! ip link show {inet_bridge} &>/dev/null; then')
        lines.append(f'  ip link add {inet_bridge} type bridge')
        lines.append(f'  ip link set {inet_bridge} up')
        lines.append(f'  echo "  created {inet_bridge}"')
        lines.append(f'else')
        lines.append(f'  echo "  {inet_bridge} already exists"')
        lines.append(f'fi')
        lines.append(f'if ! ip addr show {inet_bridge} | grep -q "{host_ip}"; then')
        lines.append(f'  ip addr add {host_ip}/30 dev {inet_bridge}')
        lines.append(f'  echo "  assigned {host_ip}/30 to {inet_bridge} (host internet uplink)"')
        lines.append(f'fi')
        lines.append('')
        lines.append('echo "=== Enabling IP forwarding and NAT ==="')
        lines.append('sysctl -w net.ipv4.ip_forward=1 > /dev/null')
        lines.append('echo "  ip_forward=1"')
        lines.append('# Detect host internet interface from default route')
        lines.append('HOST_INET=$(ip route show default | awk \'NR==1{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}\')')
        lines.append('echo "  detected internet interface: $HOST_INET"')
        lines.append('if [ -n "$HOST_INET" ]; then')
        lines.append('  iptables -t nat -C POSTROUTING -o "$HOST_INET" -j MASQUERADE 2>/dev/null || \\')
        lines.append('    iptables -t nat -A POSTROUTING -o "$HOST_INET" -j MASQUERADE')
        lines.append('  echo "  NAT/MASQUERADE rule added on $HOST_INET"')
        lines.append('  iptables -t nat -L POSTROUTING -n | grep MASQUERADE || echo "  WARNING: rule may not have been applied"')
        lines.append('else')
        lines.append('  echo "  WARNING: could not detect internet interface — NAT not configured"')
        lines.append('  echo "  Run manually: sudo iptables -t nat -A POSTROUTING -o <iface> -j MASQUERADE"')
        lines.append('fi')
        lines.append('')
        lines.append('echo "=== Adding host routes to fabric ranges ==="')
        lines.append('# Host needs routes to fabric IP ranges so NAT reply traffic')
        lines.append('# (de-NAT\'d by conntrack back to VM fabric IPs) can be forwarded')
        lines.append('# through br-internet → super-spine → leaf → VM.')
        ss_ip = internet['ss_ip']
        for prefix in [cfg.switch_underlay, cfg.dpu_fabric,
                       cfg.loopback_prefix, cfg.mgmt_prefix[:-3] + '0/24']:
            # Skip mgmt prefix — it's already directly connected via ContainerLab
            if prefix == cfg.mgmt_prefix:
                continue
        # Add a broad summary covering all fabric ranges
        reg_prefix_str = cfg.registry_link.get('prefix', '') if cfg.registry_link.get('enabled') else None
        fabric_prefixes = [cfg.switch_underlay, cfg.dpu_fabric, cfg.loopback_prefix,
                           cfg.dpu_loopback_prefix, cfg.control_plane_prefix]
        if reg_prefix_str:
            fabric_prefixes.append(reg_prefix_str)
        if cfg.underlay_pool:
            fabric_prefixes.append(cfg.underlay_pool)
        # Service VIPs — MetalLB announces these into the fabric via BGP.
        # Add route so sim-host can curl nico-api and other LoadBalancer services.
        if cfg.service_vips:
            fabric_prefixes.append(cfg.service_vips)
        for fabric_pfx in fabric_prefixes:
            lines.append(f'if ! ip route show {fabric_pfx} | grep -q "{fabric_pfx}"; then')
            lines.append(f'  ip route add {fabric_pfx} via {ss_ip} dev {inet_bridge} 2>/dev/null || true')
            lines.append(f'  echo "  added route: {fabric_pfx} via {ss_ip}"')
            lines.append(f'fi')
        lines.append('')
        lines.append(f'echo "=== Starting fabric DNS server on {inet_bridge} ==="')
        lines.append(f'# Run dnsmasq on {inet_bridge} so VMs can use the host as DNS.')
        lines.append(f'# {inet_bridge} is site-specific — kill any existing dnsmasq')
        lines.append('# on this interface before starting (handles redeploy).')
        lines.append(f'PID_FILE=/var/run/{cfg.dc_name}-dns.pid')
        lines.append('# Kill this site\'s previous instance if running')
        lines.append('if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then')
        lines.append('  kill "$(cat $PID_FILE)" 2>/dev/null && rm -f "$PID_FILE"')
        lines.append('fi')
        lines.append(f'# Kill any other dnsmasq bound to {inet_bridge} (not tracked by PID file)')
        lines.append(f'if pgrep -f "interface={inet_bridge}" > /dev/null 2>&1; then')
        lines.append(f'  if [ "$FORCE" = "0" ]; then')
        lines.append(f'    echo "ERROR: dnsmasq already running for {inet_bridge} (no site PID file)."')
        lines.append(f'    echo "  Run: sudo python3 destroy-site.py <site>  or re-run with --force"')
        lines.append(f'    exit 1')
        lines.append(f'  fi')
        lines.append(f'  pkill -f "interface={inet_bridge}" 2>/dev/null || true')
        lines.append(f'fi')
        lines.append('sleep 0.5')
        # Upstream DNS comes from nico-sim.yaml dns_servers — single source of truth
        upstream_args = ' '.join(f'--server={ns}' for ns in cfg.dns_servers)
        lines.append(f'dnsmasq \\')
        lines.append(f'  --interface={inet_bridge} \\')
        lines.append(f'  --bind-dynamic \\')          # handles multiple IPs on shared bridge
        lines.append(f'  --no-dhcp-interface={inet_bridge} \\')
        lines.append(f'  --pid-file=$PID_FILE \\')
        lines.append(f'  --log-facility=/var/log/{cfg.dc_name}-dns.log \\')
        lines.append(f'  {upstream_args}')
        lines.append(f'echo "  DNS server started on {inet_bridge} '
                     f'(upstream: {", ".join(str(s) for s in cfg.dns_servers)})"')
        lines.append('')
        lines.append('# Allow DNS queries from fabric to reach dnsmasq on this host.')
        lines.append(f'# iptables INPUT may block traffic from {inet_bridge} by default.')
        lines.append(f'# Allow DNS (dnsmasq) and HTTPS/443 (MAT BMC mocks) from the fabric side.')
        lines.append(f'iptables -C INPUT -i {inet_bridge} -p udp --dport 53 -j ACCEPT 2>/dev/null || \\')
        lines.append(f'  iptables -I INPUT -i {inet_bridge} -p udp --dport 53 -j ACCEPT')
        lines.append(f'iptables -C INPUT -i {inet_bridge} -p tcp --dport 53 -j ACCEPT 2>/dev/null || \\')
        lines.append(f'  iptables -I INPUT -i {inet_bridge} -p tcp --dport 53 -j ACCEPT')
        lines.append(f'echo "  iptables: allowed DNS (UDP+TCP 53) from {inet_bridge}"')
        lines.append(f'iptables -C INPUT -i {inet_bridge} -p tcp --dport 443 -j ACCEPT 2>/dev/null || \\')
        lines.append(f'  iptables -I INPUT -i {inet_bridge} -p tcp --dport 443 -j ACCEPT')
        lines.append(f'echo "  iptables: allowed HTTPS (TCP 443) from {inet_bridge} (MAT BMC mocks)"')

    # ── MH rack bridges — must exist before clab deploy and before MH VMs start ──
    # These are ContainerLab bridge nodes (kind: bridge) AND libvirt bridges.
    # Creating them here explicitly ensures they are available for both:
    #   1. ContainerLab: relay-rack-{i}:eth1 attaches here at clab deploy time
    #   2. libvirt:      deploy-nodes.sh uses --network bridge=br-mh-rack-N for MH VMs
    # Cleanup: clab destroy --cleanup removes these bridges automatically.
    if relay_nodes:
        lines.append('')
        lines.append('echo "=== Creating MH rack underlay bridges ==="')
        mh_bridges_seen = set()
        for rname, rinfo in relay_nodes.items():
            if rinfo.get('is_mat'):
                continue   # mat leaf uses br-internet — already created above
            bridge_name = rinfo['bridge']
            if bridge_name in mh_bridges_seen:
                continue
            mh_bridges_seen.add(bridge_name)
            lines.append(f'if ! ip link show {bridge_name} &>/dev/null; then')
            lines.append(f'  ip link add {bridge_name} type bridge')
            lines.append(f'  ip link set {bridge_name} up')
            lines.append(f'  echo "  created {bridge_name} (MH rack {rinfo["rack_index"]} underlay)"')
            lines.append(f'else')
            lines.append(f'  echo "  {bridge_name} already exists"')
            lines.append(f'fi')

    lines.append('')
    lines.append('echo')
    lines.append('echo "=== Building relay container image ==="')
    lines.append('# alpine:latest (3.24+) removed ISC DHCP. Use alpine:3.18 which still')
    lines.append('# has the dhcp package (provides /usr/sbin/dhcrelay).')
    lines.append('if docker image inspect dc-sim-relay:latest > /dev/null 2>&1; then')
    lines.append('  echo "  dc-sim-relay:latest already exists — skipping build"')
    lines.append('else')
    lines.append('  echo "  Building dc-sim-relay:latest (alpine:3.18 + dhcp)..."')
    lines.append('  docker build -t dc-sim-relay:latest - << \'RELAY_EOF\'')
    lines.append('FROM alpine:3.18')
    lines.append('RUN apk add --no-cache dhcp')
    lines.append('RELAY_EOF')
    lines.append('  echo "  dc-sim-relay:latest built ✓"')
    lines.append('fi')
    lines.append('')
    # Collect known ContainerLab bridge-side endpoint names
    clab_ifaces = set()
    for i in range(1, cfg.num_cp_leafs + 1):
        clab_ifaces.add(f'eth-cp{i}')
    mh_count = sum(1 for v in cfg.underlay_leafs.values()
                   if isinstance(v, dict) and v.get('relay', False))
    for r in range(1, mh_count + 1):
        clab_ifaces.add(f'eth-mh-{r}')
    clab_ifaces.update(['eth-uplink', 'eth-registry', 'eth-helper', 'eth-mat'])
    all_bridges = [f'br-{cfg.dc_name}-cp{i}' for i in range(1, cfg.num_cp_leafs + 1)]
    all_bridges += [f'br-{cfg.dc_name}-mh{r}' for r in range(1, mh_count + 1)]
    all_bridges += [f'br-{cfg.dc_name}-internet', f'br-{cfg.dc_name}-registry']

    lines.append('echo')
    lines.append('echo "=== Checking for existing deployment ==="')
    lines.append('_CONFLICT=""')
    for iface in sorted(clab_ifaces):
        lines.append(f'ip link show {iface} &>/dev/null && _CONFLICT="$_CONFLICT {iface}"')
    for br in all_bridges:
        lines.append(f'for _s in $(ls /sys/class/net/{br}/brif/ 2>/dev/null); do')
        lines.append(f'  _CONFLICT="$_CONFLICT {br}/$_s"')
        lines.append(f'done')
    lines.append('if [ -n "$_CONFLICT" ]; then')
    lines.append('  echo "ERROR: Existing ContainerLab interfaces/bridge-slaves found:"')
    lines.append('  echo "  $_CONFLICT"')
    lines.append('  echo ""')
    lines.append('  echo "  The fabric (or its VMs) is already deployed."')
    lines.append(f'  echo "  To tear everything down: sudo python3 destroy-site.py <site>"')
    lines.append('  echo "  To force overwrite (WARNING: may disrupt running VMs): --force"')
    lines.append('  if [ "$FORCE" = "0" ]; then exit 1; fi')
    lines.append('  echo "  --force: flushing existing interfaces..."')
    for iface in sorted(clab_ifaces):
        lines.append(f'  ip link delete {iface} 2>/dev/null || true')
    for br in all_bridges:
        lines.append(f'  for _s in $(ls /sys/class/net/{br}/brif/ 2>/dev/null); do')
        lines.append(f'    ip link delete "$_s" 2>/dev/null || true')
        lines.append(f'  done')
    lines.append('fi')
    lines.append('echo "  ok"')
    lines.append('')
    lines.append('echo')
    lines.append('echo "=== Deploying ContainerLab topology ==="')
    lines.append('cd "$SCRIPT_DIR"')
    lines.append(f'if clab inspect -n {cfg.dc_name} &>/dev/null 2>&1; then')
    lines.append('  if [ "$FORCE" = "0" ]; then')
    lines.append(f'    echo "ERROR: ContainerLab lab \'{cfg.dc_name}\' is already running."')
    lines.append(f'    echo "  Run: sudo python3 destroy-site.py <site>"')
    lines.append('    echo "  Or re-run with --force to destroy and redeploy."')
    lines.append('    exit 1')
    lines.append('  fi')
    lines.append('  echo "  --force: destroying existing ContainerLab lab..."')
    lines.append('fi')
    lines.append('clab destroy -t topo.clab.yml --cleanup 2>/dev/null || true')
    lines.append('clab deploy -t topo.clab.yml')
    lines.append('')
    lines.append('# Fix SSH config permissions — ContainerLab writes this as root,')
    lines.append('# which blocks SSH (and git) for non-root users.')
    lines.append(f'SSH_CONF="/etc/ssh/ssh_config.d/clab-{cfg.dc_name}.conf"')
    lines.append('if [ -f "$SSH_CONF" ]; then')
    lines.append('  chmod 644 "$SSH_CONF"')
    lines.append('  echo "Fixed permissions on $SSH_CONF"')
    lines.append('fi')
    lines.append('# Make fabric output dir readable by non-root (verify-fabric, nsim)')
    lines.append('chmod -R a+rX "$SCRIPT_DIR"')
    lines.append('echo "Fixed permissions on $SCRIPT_DIR"')
    return '\n'.join(lines) + '\n'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    sim_yaml, fabric_dir, _ = resolve_site(args.site)
    output_dir = args.output_dir or fabric_dir
    cfg  = load_config(sim_yaml)
    validate_sim(cfg._raw)

    output = Path(output_dir)
    print(f'Generating fabric topology from {sim_yaml}')
    print(f'  {cfg.num_leafs} leafs ({cfg.num_cp_leafs} CP leafs), 2 spines, 1 super-spine')
    print(f'  switch underlay : {cfg.switch_underlay}')
    print(f'  dpu fabric      : {cfg.dpu_fabric}')
    print(f'  switch loopbacks: {cfg.loopback_prefix}')
    print(f'  dpu loopbacks   : {cfg.dpu_loopback_prefix}')
    print(f'  mgmt            : {cfg.mgmt_prefix}')
    print(f'  switch ASN base : {cfg.switch_asn_base}')
    print(f'  dpu ASN base    : {cfg.dpu_asn_base}')
    print(f'  image           : {cfg.image}')
    print(f'  output          : {output}')
    inet = cfg.internet_uplink
    if inet.get('enabled', False):
        hosts = list(ipaddress.IPv4Network(inet.get('prefix', '7.130.3.0/30'), strict=False).hosts())
        print(f'  internet uplink : {inet.get("prefix")}  host={hosts[0]}  super-spine={hosts[1]}')
    reg = cfg.registry_link
    if reg.get('enabled', False):
        rhosts = list(ipaddress.IPv4Network(reg.get('prefix', '7.132.0.4/30'), strict=False).hosts())
        print(f'  registry link   : {reg.get("prefix")}  super-spine={rhosts[0]}  registry-vm={rhosts[1]}')
    if cfg.mat_leaf_name:
        print(f'  mat leaf        : {cfg.mat_leaf_name}  bridge={cfg.mat_leaf_bridge}  underlay={cfg.mat_underlay_net}')
    print()

    topo = build_topology(cfg)
    output.mkdir(parents=True, exist_ok=True)

    (output / 'topo.clab.yml').write_text(gen_topo(cfg, topo))
    print(f'  [ok] topo.clab.yml')

    nodes_dir = output / 'nodes'
    daemons   = gen_daemons()
    internet  = topo.get('internet')
    registry  = topo.get('registry')
    evpn      = topo.get('evpn_cfg')
    evpn_enabled = evpn and evpn.get('enabled', False)
    vnis = []
    if evpn_enabled:
        vnis = [evpn.get('control_plane_vni', 60000),
                evpn.get('managed_host_vni',  60100)]

    for name, info in topo['nodes'].items():
        node_dir = nodes_dir / name
        node_dir.mkdir(parents=True, exist_ok=True)
        # Find mat leaf info for this node if applicable
        mat_info = None
        for rk, rv in topo.get('relay_nodes', {}).items():
            if rv.get('is_mat') and rv.get('name') == name:
                mat_info = rv
                break
        (node_dir / 'frr.conf').write_text(
            gen_frr(name, info, internet=internet, registry=registry, evpn=evpn,
                    mat_leaf_info=mat_info, datacenter_asn=cfg.datacenter_asn,
                    evpn_vnis=cfg.evpn_vnis))
        (node_dir / 'daemons').write_text(daemons)
        if evpn_enabled and name.startswith('leaf-'):
            (node_dir / 'vxlan-setup.sh').write_text(
                gen_vxlan_setup(info['loopback'], vnis))
        print(f'  [ok] nodes/{name}/')
    if evpn_enabled:
        print(f'  EVPN enabled: VNIs {vnis}')

    (output / 'vm-reference.txt').write_text(gen_vm_reference(topo, cfg))
    print(f'  [ok] vm-reference.txt')

    deploy_path = output / 'deploy.sh'
    deploy_path.write_text(gen_deploy_sh(topo, cfg))
    deploy_path.chmod(0o755)
    print(f'  [ok] deploy.sh')

    print()
    print('Deploy:')
    print(f'  cd {output} && sudo ./deploy.sh')
    print()
    print('Destroy:')
    print(f'  cd {output} && sudo clab destroy -t topo.clab.yml --cleanup')
    mh_bridges = [topo['relay_nodes'][r]['bridge']
                  for r in topo.get('relay_nodes', {})
                  if not topo['relay_nodes'][r].get('is_mat')]
    if mh_bridges:
        print(f'  # br-mh-rack bridges {mh_bridges} are cleaned up by clab destroy --cleanup')
        print(f'  # If bridges persist: sudo ip link delete <br-mh-rack-N> type bridge')


if __name__ == '__main__':
    main()
