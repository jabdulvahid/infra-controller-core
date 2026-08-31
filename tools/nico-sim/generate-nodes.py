#!/usr/bin/env python3
"""
DC Simulation — Node generator (replaces generate-vms.py)

Generates both DPU stand-in VMs and CP host VMs together, since they are
always paired. Each CP node gets one DPU stand-in VM that:
  - Connects to the leaf via the fabric bridge (dpu_fabric prefix)
  - Connects to the CP host VM via an internal host bridge (control_plane_prefix)
  - Runs FRR for BGP peering with the leaf

Reads nico-sim.yaml, verifies the fabric topology and BGP state, then generates:
  - OOB libvirt network XML
  - Cloud-init configs per VM (user-data, meta-data, network-config)
  - deploy-nodes.sh  (creates host-side bridges + DPU VMs + host VMs)
  - destroy-nodes.sh (removes everything created by deploy-nodes.sh)
  - node-reference.txt (IP/MAC/ASN assignments summary)

Prerequisites:
  1. generate-fabric.py nico-sim.yaml has been run → fabric/topo.clab.yml exists
  2. sudo ./fabric/deploy.sh has been run → ContainerLab fabric is running
  3. BGP peers Established across the fabric

Usage:
  python3 generate-nodes.py nico-sim.yaml [options]

Output layout (default ./vm/):
  oob-network.xml
  deploy-nodes.sh
  destroy-nodes.sh
  node-reference.txt
  cloud-init/
    cp-dpu-1/  cp-1/  cp-dpu-2/  cp-2/  cp-dpu-3/  cp-3/
      user-data  meta-data  network-config
"""

import argparse
import ipaddress
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sim_validate import validate_sim

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: pip3 install pyyaml', file=sys.stderr)
    sys.exit(1)


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


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate DPU stand-in + CP host VMs for DC simulation',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site',
                   help='Site folder or nico-sim yaml file')
    p.add_argument('--topo', default=None,
                   help='topo.clab.yml (default: {site}/fabric/topo.clab.yml)')
    p.add_argument('--output-dir', default=None,
                   help='Output dir (default: {site}/vm)')
    p.add_argument('--skip-bgp-check', action='store_true',
                   help='Skip BGP state verification (use when fabric is being rebuilt)')
    p.add_argument('--ssh-key', default=None,
                   help='Path to SSH public key to inject into VMs '
                        '(default: ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)')
    return p.parse_args()


# ── Config loading and validation ─────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def validate_provisioning(sim):
    """
    Validate that os_image_* fields are in the right section per provisioning mode.

    golden_image mode: os_image_* must be in golden_image.control_plane, NOT in control_plane.
    cloud_init mode:   os_image_* must be in control_plane, golden_image section is ignored.
    Error if the config is contradictory (both present).
    """
    cp           = sim.get('control_plane', {})
    provisioning = cp.get('provisioning', 'cloud_init')
    has_img_in_cp  = 'os_image_url' in cp
    has_img_in_gi  = 'os_image_url' in sim.get('golden_image', {}).get('control_plane', {})

    if provisioning == 'golden_image':
        if has_img_in_cp:
            print('Error: control_plane.os_image_url must not be set when '
                  'provisioning: golden_image.', file=sys.stderr)
            print('  Move os_image_url/name/dir to golden_image.control_plane.',
                  file=sys.stderr)
            sys.exit(1)
        if not has_img_in_gi:
            print('Error: golden_image.control_plane.os_image_url is required '
                  'when provisioning: golden_image.', file=sys.stderr)
            print('  Set os_image_url/name/dir under golden_image.control_plane.',
                  file=sys.stderr)
            sys.exit(1)
        if not sim.get('golden_image', {}).get('control_plane', {}).get('path'):
            print('Error: golden_image.control_plane.path is required when '
                  'provisioning: golden_image.', file=sys.stderr)
            sys.exit(1)
    else:  # cloud_init
        if not has_img_in_cp:
            print('Error: control_plane.os_image_url is required when '
                  'provisioning: cloud_init.', file=sys.stderr)
            sys.exit(1)
        if has_img_in_gi:
            print('Warning: golden_image.control_plane.os_image_url is set but '
                  'provisioning: cloud_init — golden_image section will be ignored.')


def resolve_image_config(sim):
    """
    Return (img_url, img_name, img_dir, golden_path) based on provisioning mode.
    DPU stand-in VMs always use the base Ubuntu image (img_url/name/dir).
    Host VMs use golden_path when provisioning: golden_image, else the base image.
    """
    cp           = sim['control_plane']
    provisioning = cp.get('provisioning', 'cloud_init')
    if provisioning == 'golden_image':
        gi_cp = sim['golden_image']['control_plane']
        return (gi_cp['os_image_url'], gi_cp['os_image_name'],
                gi_cp['os_image_dir'], gi_cp['path'])
    else:
        return (cp['os_image_url'], cp['os_image_name'],
                cp['os_image_dir'], '')


def load_topo(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── Fabric verification ───────────────────────────────────────────────────────

def verify_topology_structure(topo, sim):
    nodes      = topo.get('topology', {}).get('nodes', {})
    spines     = [n for n in nodes if n.startswith('spine-')]
    leafs      = [n for n in nodes if n.startswith('leaf-')]
    # Bridge names are site-specific: br-{dc_name}-cp{i}
    dc_name    = sim['fabric'].get('dc_name', 'nico-sim')
    cp_bridges = sorted([n for n in nodes if n.startswith(f'br-{dc_name}-cp')])

    errors = []
    if 'super-spine' not in nodes:
        errors.append('No super-spine node found in topology')
    if not spines:
        errors.append('No spine nodes found in topology')
    if not leafs:
        errors.append('No leaf nodes found in topology')
    if errors:
        raise RuntimeError('Topology structure errors:\n' + '\n'.join(f'  - {e}' for e in errors))

    print(f'  topology tiers: 1 super-spine, {len(spines)} spines, {len(leafs)} leafs ✓')

    num_vms = sim['control_plane']['num_vms']
    if len(cp_bridges) < num_vms:
        raise RuntimeError(
            f'Not enough CP bridge nodes in topology: need {num_vms}, '
            f'found {len(cp_bridges)} (br-{dc_name}-cp*).\n'
            f'  → Regenerate fabric with fabric.num_control_plane_leafs >= {num_vms}'
        )
    print(f'  CP bridge nodes: {len(cp_bridges)} available, {num_vms} requested ✓')
    return cp_bridges


def verify_fabric_running(sim):
    lab_name = sim['fabric'].get('dc_name', 'nico-sim')
    result = subprocess.run(
        ['docker', 'ps', '--filter', f'name=clab-{lab_name}', '--format', '{{.Names}}'],
        capture_output=True, text=True
    )
    running = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    if not running:
        raise RuntimeError(
            f"No ContainerLab containers running for lab '{lab_name}'.\n"
            f"  → Deploy the fabric first: cd output && sudo ./deploy.sh"
        )
    switches = [c for c in running if any(n in c for n in ['super-spine', 'spine-', 'leaf-'])]
    print(f'  running containers: {len(switches)} switch nodes ✓')
    return running


def verify_bgp_state(sim):
    lab_name   = sim['fabric'].get('dc_name', 'nico-sim')
    super_name = f'clab-{lab_name}-super-spine'

    result = subprocess.run(
        ['docker', 'exec', super_name, 'vtysh', '-c', 'show bgp summary'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f'Failed to query BGP on super-spine: {result.stderr}')

    not_established = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 10 and '.' in parts[0]:
            state_pfx = parts[9]
            if not state_pfx.isdigit():
                not_established.append(f'{parts[0]} state={state_pfx}')

    if not_established:
        raise RuntimeError(
            'BGP peers not Established on super-spine:\n'
            + '\n'.join(f'  - {p}' for p in not_established)
            + '\n  → Wait for BGP to converge and retry'
        )
    print('  super-spine BGP: all peers Established ✓')


# ── IP / MAC helpers ──────────────────────────────────────────────────────────

def slash31_pairs(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for subnet in net.subnets(new_prefix=31):
        hosts = list(subnet.hosts())
        yield str(hosts[0]), str(hosts[1])


def slash32_hosts(prefix_str):
    net = ipaddress.IPv4Network(prefix_str, strict=False)
    for host in net.hosts():
        yield str(host)


def _site_octet(sim):
    """Extract underlay first octet from switch_underlay prefix — unique per site.
    Used as 3rd MAC byte so VMs from different sites never conflict.
    dc-sim (underlay=7) → 07, ytl (underlay=9) → 09.
    """
    pfx = sim.get('fabric', {}).get('prefixes', {}).get('switch_underlay', '7.0.0.0/8')
    return int(pfx.split('.')[0])


def vm_mac(index, iface_num, site_octet=0):
    """Host VM MAC: 52:54:SS:00:NN:MM  (SS = site underlay octet)"""
    return f'52:54:{site_octet:02x}:00:{index:02x}:{iface_num:02x}'


def dpu_mac(index, iface_num, site_octet=0):
    """DPU stand-in VM MAC: 52:54:SS:01:NN:MM  (SS = site underlay octet)"""
    return f'52:54:{site_octet:02x}:01:{index:02x}:{iface_num:02x}'


def mh_mac(index, iface_num, site_octet=0):
    """Managed-host VM MAC: 52:54:SS:04:NN:MM  (SS = site underlay octet)"""
    return f'52:54:{site_octet:02x}:04:{index:02x}:{iface_num:02x}'


# ── IP allocation ─────────────────────────────────────────────────────────────

def allocate_cp_nodes(sim, cp_bridges):
    """
    Allocate IPs and MACs for each CP node pair (DPU stand-in + host VM).

    dpu_fabric pool (7.131.0.0/24):
      /31 pair i: leaf_ip (.0) ↔ dpu_fabric_ip (.1)
      — same order as generate-fabric.py allocates them

    control_plane_prefix (7.132.0.0/29 → four /31s):
      /31 pair i: dpu_internal_ip (.even) ↔ host_internal_ip (.odd)
    """
    pfx      = sim['fabric']['prefixes']
    cp       = sim['control_plane']
    dpu_pool = slash31_pairs(pfx['dpu_fabric'])
    cp_pool  = slash31_pairs(pfx.get('control_plane_prefix', cp.get('control_plane_prefix', '7.132.1.0/24')))
    lb_pool  = slash32_hosts(pfx.get('dpu_loopbacks', '7.131.0.0/16'))
    fab          = sim['fabric']
    sw_base      = fab.get('switch_asn_base', fab.get('asn_base', 65000))
    dpu_base     = fab.get('dpu_asn_base', fab.get('asn_base', 65000) + 20)
    metallb_asn  = int(sim.get('nico-system', {}).get('helm-values', {})
                       .get('net-plan', {}).get('metallb_asn', 0))
    num_vms  = sim['control_plane']['num_vms']
    vp          = sim['control_plane']['vm_prefix']
    dp          = sim['control_plane'].get('dpu_prefix', f'{vp}-dpu')
    dc_name     = sim['fabric'].get('dc_name', 'nico-sim')
    sitename    = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx    = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    site_octet  = _site_octet(sim)

    nodes = []
    for i in range(1, num_vms + 1):
        leaf_ip, dpu_fabric_ip   = next(dpu_pool)
        dpu_internal_ip, host_ip = next(cp_pool)
        dpu_loopback             = next(lb_pool)   # belongs to DPU stand-in, not host VM

        nodes.append({
            'index': i,
            # Host VM — no FRR, no BGP loopback
            'host_name':        f'{name_pfx}{vp}-{i}',
            'host_internal_ip': host_ip,
            'host_oob_mac':     vm_mac(i, 1, site_octet),   # 52:54:00:00:0i:01
            'host_dpu_mac':     vm_mac(i, 2, site_octet),   # 52:54:00:00:0i:02 → br-cp{i}-host
            # DPU stand-in VM
            'dpu_name':         f'{name_pfx}{dp}-{i}',
            'dpu_loopback':     dpu_loopback,    # BGP router-ID + future VTEP source IP
            'dpu_fabric_ip':    dpu_fabric_ip,
            'dpu_internal_ip':  dpu_internal_ip,
            'dpu_oob_mac':      dpu_mac(i, 1, site_octet),  # 52:54:00:01:0i:01
            'dpu_fabric_mac':   dpu_mac(i, 2, site_octet),  # 52:54:00:01:0i:02 → br-cp{i} (leaf)
            'dpu_host_mac':     dpu_mac(i, 3, site_octet),  # 52:54:00:01:0i:03 → br-cp{i}-host (host VM)
            # Common
            'leaf_ip':          leaf_ip,
            'leaf_asn':         sw_base + 10 + i,
            'dpu_asn':          dpu_base + i,
            'metallb_asn':      metallb_asn,
            'leaf_name':        f'leaf-{i}',
            'fabric_bridge':    cp_bridges[i - 1],   # br-cp1, br-cp2, br-cp3
            'host_bridge':      f'br-cp{i}-host',    # DPU↔host internal bridge
        })

    return nodes


def allocate_mh_nodes(sim):
    """
    Allocate MACs for each MH (managed-host) VM.

    MH VMs are simpler than CP nodes — no DPU stand-in, no FRR, no k8s.
    They connect to:
      eth0 (OOB):    {dc_name}-oob-mh libvirt network (192.168.210.0/24) — DHCP
      eth1 (fabric): br-{dc_name}-mhN bridge (shared with relay container)
                     NO IP assigned — Nico configures this at ingestion time.

    Leaf mapping comes from nico-system.underlay (explicit):
      rack-leaf-4 → leaf-4 → br-{dc_name}-mh1
      rack-leaf-5 → leaf-5 → br-{dc_name}-mh2

    managed_hosts.num_vms is a dict: {underlay_name: vm_count}.
    VMs are numbered sequentially across racks (mh-1..N).
    """
    mh           = sim.get('managed_hosts', {})
    num_vms_cfg  = mh.get('num_vms', {})
    vm_prefix    = mh.get('vm_prefix', 'mh')
    underlay_map = sim.get('nico-system', {}).get('underlay', {})
    dc_name     = sim['fabric'].get('dc_name', 'nico-sim')
    sitename    = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx    = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    site_octet  = _site_octet(sim)

    mh_dpu_count = sim.get('mh_dpu', {}).get('count', 0)
    if mh_dpu_count > 0:
        print(f'  Note: mh_dpu.count={mh_dpu_count} — standalone MH DPU VMs not yet implemented',
              file=sys.stderr)

    if not isinstance(num_vms_cfg, dict):
        return []   # validation should have caught this already

    nodes = []
    global_index = 0   # sequential VM number across all racks
    for rack_index, (underlay_name, count) in enumerate(num_vms_cfg.items(), start=1):
        leaf_name   = underlay_map.get(underlay_name, '')
        # Derive leaf number from leaf name (e.g. "leaf-4" → 4)
        try:
            leaf_num = int(leaf_name.split('-')[1]) if leaf_name else 0
        except (IndexError, ValueError):
            leaf_num = 0

        for vm_in_rack in range(count):
            global_index += 1
            rack_local_n = vm_in_rack + 1   # 1-based within the rack
            nodes.append({
                'index':         global_index,
                'name':          f'{name_pfx}{vm_prefix}-{global_index}',
                'dpu_name':      f'{name_pfx}{vm_prefix}-dpu-{global_index}',
                'oob_mac':       mh_mac(global_index, 1, site_octet),
                'fabric_mac':    mh_mac(global_index, 2, site_octet),
                'fabric_bridge': f"br-{sim['fabric'].get('dc_name', 'nico-sim')}-mh{rack_index}",
                'rack_index':    rack_index,
                'rack_local_n':  rack_local_n,
                'leaf_index':    leaf_num,
                'underlay':      underlay_name,
                'leaf_name':     leaf_name,
            })
    return nodes


# ── FRR helpers ───────────────────────────────────────────────────────────────

def gen_dpu_frr_conf(node):
    """FRR config for DPU stand-in: BGP with leaf, advertise loopback + DPU↔host /31.

    Loopback (from 7.130.2.0/24) is the BGP router-ID and will become the VTEP
    source IP for EVPN. Advertised as /32 so the full fabric can reach it.
    The point-to-point fabric IP (7.131.0.x) is NOT advertised — the loopback
    is the stable identifier, matching production DPU behaviour.
    """
    dpu_net = ipaddress.IPv4Network(f'{node["dpu_internal_ip"]}/31', strict=False)

    lines = []
    lines.append('frr version 9.1')
    lines.append('frr defaults datacenter')
    lines.append(f'hostname {node["dpu_name"]}')
    lines.append('log syslog informational')
    lines.append('!')
    lines.append('! Loopback — BGP router-ID and future VTEP source IP')
    lines.append('interface lo')
    lines.append(f' ip address {node["dpu_loopback"]}/32')
    lines.append('!')
    lines.append(f'! Fabric uplink to {node["leaf_name"]}')
    lines.append('interface eth1')
    lines.append(f' ip address {node["dpu_fabric_ip"]}/31')
    lines.append('!')
    lines.append(f'! Host-side link to {node["host_name"]}')
    lines.append('interface eth2')
    lines.append(f' ip address {node["dpu_internal_ip"]}/31')
    lines.append('!')
    lines.append(f'router bgp {node["dpu_asn"]}')
    lines.append(f' bgp router-id {node["dpu_loopback"]}')
    lines.append(' bgp bestpath as-path multipath-relax')
    lines.append(' !')
    lines.append(f' neighbor {node["leaf_ip"]} remote-as {node["leaf_asn"]}')
    lines.append(f' neighbor {node["leaf_ip"]} description {node["leaf_name"]}')
    lines.append(f' neighbor {node["host_internal_ip"]} remote-as {node["metallb_asn"]}')
    lines.append(f' neighbor {node["host_internal_ip"]} description metallb-{node["host_name"]}')
    lines.append(' !')
    lines.append(' address-family ipv4 unicast')
    lines.append(f'  network {node["dpu_loopback"]}/32')
    lines.append(f'  network {dpu_net.network_address}/31')
    lines.append(f'  neighbor {node["leaf_ip"]} activate')
    lines.append(f'  neighbor {node["host_internal_ip"]} activate')
    lines.append(f'  neighbor {node["host_internal_ip"]} soft-reconfiguration inbound')
    lines.append(' exit-address-family')
    lines.append('!')
    return '\n'.join(lines) + '\n'


def _frr_daemons():
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


# ── DNS helper ────────────────────────────────────────────────────────────────

def _dns_config(sim):
    """Return (primary_dns_or_None, all_dns_list, resolv_conf_ns_lines_escaped)."""
    inet_cfg = sim['fabric'].get('internet_uplink', {})
    if inet_cfg.get('enabled', False):
        inet_hosts = list(ipaddress.IPv4Network(
            inet_cfg.get('prefix', '7.130.3.0/30'), strict=False).hosts())
        primary = str(inet_hosts[0])
    else:
        primary = None
    extra   = sim['control_plane'].get('dns_servers', ['8.8.8.8', '8.8.4.4'])
    all_dns = ([primary] if primary else []) + [str(s) for s in extra if str(s) != primary]
    ns_lines = '\\n'.join(f'nameserver {ns}' for ns in all_dns)
    return primary, all_dns, ns_lines


# ── Cloud-init: DPU stand-in ──────────────────────────────────────────────────

def gen_dpu_user_data(node, sim, ssh_pub_key):
    """Cloud-init user-data for DPU stand-in VM. Installs FRR, enables IP forwarding."""
    name = node['dpu_name']
    _, _, ns_lines = _dns_config(sim)
    frr_conf = gen_dpu_frr_conf(node)
    daemons  = _frr_daemons()

    lines = []
    lines.append('#cloud-config')
    lines.append(f'hostname: {name}')
    lines.append(f'fqdn: {name}.sim.local')
    lines.append('manage_etc_hosts: true')
    lines.append('')

    pw = sim['control_plane'].get('console_password', '')
    if pw:
        lines.append('chpasswd:')
        lines.append('  expire: false')
        lines.append('  users:')
        lines.append(f'    - name: ubuntu')
        lines.append(f'      password: "{pw}"')
        lines.append(f'      type: text')
        lines.append('')

    if ssh_pub_key:
        lines.append('ssh_authorized_keys:')
        lines.append(f'  - {ssh_pub_key.strip()}')
        lines.append('')

    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('  # Regenerate machine-id so each VM gets a unique DHCP Client ID.')
    lines.append('  # All VMs share the same backing qcow2 image which has a baked-in')
    lines.append('  # machine-id; without this, dnsmasq treats all DPU VMs as the same')
    lines.append('  # DHCP client and only one appears in the lease table at a time.')
    lines.append('  - [cloud-init-per, once, machine-id-regen, bash, -c,')
    lines.append('    "truncate -s0 /etc/machine-id && systemd-machine-id-setup"]')
    lines.append('  # Enable IP forwarding immediately — before packages stage — so CP host VMs')
    lines.append('  # can route through this DPU stand-in during their own cloud-init.')
    lines.append('  # runcmd runs after packages which is too late (race condition with cp host VMs).')
    lines.append('  - sysctl -w net.ipv4.ip_forward=1')
    lines.append('  - sysctl -w net.ipv4.conf.all.forwarding=1')
    lines.append('')

    lines.append('packages:')
    lines.append('  - frr')
    lines.append('  - frr-pythontools')
    lines.append('  - python3')
    lines.append('  - net-tools')
    lines.append('  - iputils-ping')
    lines.append('  - traceroute')
    lines.append('package_update: true')
    lines.append('')

    lines.append('write_files:')
    lines.append('  - path: /etc/frr/frr.conf')
    lines.append('    owner: root:root')
    lines.append('    permissions: "0644"')
    lines.append('    content: |')
    for l in frr_conf.splitlines():
        lines.append(f'      {l}')
    lines.append('')
    lines.append('  - path: /etc/frr/daemons')
    lines.append('    owner: root:root')
    lines.append('    permissions: "0644"')
    lines.append('    content: |')
    for l in daemons.splitlines():
        lines.append(f'      {l}')
    lines.append('')
    lines.append('  - path: /etc/sysctl.d/99-dpu-forward.conf')
    lines.append('    owner: root:root')
    lines.append('    permissions: "0644"')
    lines.append('    content: |')
    lines.append('      net.ipv4.ip_forward = 1')
    lines.append('      net.ipv4.conf.all.forwarding = 1')
    lines.append('')

    lines.append('runcmd:')
    lines.append('  - sysctl -p /etc/sysctl.d/99-dpu-forward.conf')
    lines.append('  - chown -R frr:frr /etc/frr')
    lines.append('  - systemctl enable frr')
    lines.append('  - systemctl start frr')
    lines.append('')

    lines.append('final_message: |')
    lines.append(f'  {name} cloud-init complete.')
    lines.append(f'  Loopback   : {node["dpu_loopback"]}/32 (BGP router-ID)')
    lines.append(f'  Fabric IP  : {node["dpu_fabric_ip"]}/31 (leaf peer: {node["leaf_ip"]})')
    lines.append(f'  Internal IP: {node["dpu_internal_ip"]}/31 '
                 f'({node["host_name"]}: {node["host_internal_ip"]})')
    lines.append(f'  BGP ASN    : {node["dpu_asn"]} (leaf: {node["leaf_asn"]})')

    return '\n'.join(lines) + '\n'


def gen_dpu_network_config(node, sim):
    """Cloud-init network-config (netplan v2) for DPU stand-in VM.
    eth0 = OOB (DHCP, no default route)
    eth1 = fabric (static IP from dpu_fabric, default route via leaf when BGP comes up)
    eth2 = host-side (static IP from control_plane_prefix)
    Default route comes from BGP (FRR learns 0.0.0.0/0 from leaf), not netplan.
    """
    _, all_dns, _ = _dns_config(sim)
    dns_str = ', '.join(all_dns)

    lines = []
    lines.append('version: 2')
    lines.append('ethernets:')
    lines.append('  oob:')
    lines.append(f'    # OOB management — MAC {node["dpu_oob_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["dpu_oob_mac"]}"')
    lines.append('    set-name: eth0')
    lines.append('    dhcp4: true')
    lines.append('    dhcp4-overrides:')
    lines.append('      use-routes: false')
    lines.append('      use-dns: false')
    lines.append('')
    lines.append('  fabric:')
    lines.append(f'    # Fabric uplink to {node["leaf_name"]} — MAC {node["dpu_fabric_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["dpu_fabric_mac"]}"')
    lines.append('    set-name: eth1')
    lines.append(f'    addresses: [{node["dpu_fabric_ip"]}/31]')
    lines.append('    routes:')
    lines.append(f'      - to: 0.0.0.0/0')
    lines.append(f'        via: {node["leaf_ip"]}   # static default via leaf — required for internet')
    lines.append(f'        # before FRR/BGP starts (same reason CP VMs need static route via DPU)')
    lines.append('    nameservers:')
    lines.append(f'      addresses: [{dns_str}]')
    lines.append('')
    lines.append('  host_link:')
    lines.append(f'    # Host-side link to {node["host_name"]} — MAC {node["dpu_host_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["dpu_host_mac"]}"')
    lines.append('    set-name: eth2')
    lines.append(f'    addresses: [{node["dpu_internal_ip"]}/31]')
    lines.append('')
    lines.append('  lo:')
    lines.append(f'    # BGP router-ID + future VTEP source IP — {node["dpu_loopback"]}/32')
    lines.append(f'    addresses: [{node["dpu_loopback"]}/32]')

    return '\n'.join(lines) + '\n'


# ── Cloud-init: host CP VM ────────────────────────────────────────────────────

def gen_host_user_data(node, sim, ssh_pub_key):
    """Cloud-init user-data for CP host VM. No FRR — DPU stand-in handles BGP."""
    name = node['host_name']
    _, _, ns_lines = _dns_config(sim)

    lines = []
    lines.append('#cloud-config')
    lines.append(f'hostname: {name}')
    lines.append(f'fqdn: {name}.sim.local')
    lines.append('manage_etc_hosts: true')
    lines.append('')

    pw = sim['control_plane'].get('console_password', '')
    if pw:
        lines.append('chpasswd:')
        lines.append('  expire: false')
        lines.append('  users:')
        lines.append(f'    - name: ubuntu')
        lines.append(f'      password: "{pw}"')
        lines.append(f'      type: text')
        lines.append('')

    if ssh_pub_key:
        lines.append('ssh_authorized_keys:')
        lines.append(f'  - {ssh_pub_key.strip()}')
        lines.append('')

    _, _, _, golden_path = resolve_image_config(sim)
    if not golden_path:
        # cloud_init provisioning — install packages on first boot (~3-5 min).
        # Switch to golden_image provisioning for ~30s boot:
        #   1. Run: sudo python3 create-golden-image.py nico-sim.yaml
        #   2. Set control_plane.provisioning: golden_image in nico-sim.yaml
        lines.append('packages:')
        lines.append('  - python3')
        lines.append('  - python3-pip')
        lines.append('  - curl')
        lines.append('  - wget')
        lines.append('  - apt-transport-https')
        lines.append('  - ca-certificates')
        lines.append('  - gnupg')
        lines.append('  - lsb-release')
        lines.append('  - net-tools')
        lines.append('  - iputils-ping')
        lines.append('  - traceroute')
        lines.append('package_update: true')
        lines.append('')

    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('')

    lines.append('final_message: |')
    lines.append(f'  {name} cloud-init complete.')
    lines.append(f'  Internal IP  : {node["host_internal_ip"]}/31 '
                 f'(gateway: {node["dpu_internal_ip"]})')
    lines.append(f'  DPU stand-in : {node["dpu_name"]} '
                 f'(loopback: {node["dpu_loopback"]}, fabric: {node["dpu_fabric_ip"]})')
    lines.append('  Internet     : via DPU stand-in → fabric → host NAT')

    return '\n'.join(lines) + '\n'


def gen_host_network_config(node, sim):
    """Cloud-init network-config (netplan v2) for CP host VM.
    eth0 = OOB (DHCP, no default route)
    eth1 = host-side link to DPU stand-in (IP from control_plane_prefix)
    Default route via DPU stand-in's internal IP.
    """
    _, all_dns, _ = _dns_config(sim)
    dns_str = ', '.join(all_dns)

    lines = []
    lines.append('version: 2')
    lines.append('ethernets:')
    lines.append('  oob:')
    lines.append(f'    # OOB management — MAC {node["host_oob_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["host_oob_mac"]}"')
    lines.append('    set-name: eth0')
    lines.append('    dhcp4: true')
    lines.append('    dhcp4-overrides:')
    lines.append('      use-routes: false')
    lines.append('      use-dns: false')
    lines.append('')
    lines.append('  dpu_link:')
    lines.append(f'    # Link to DPU stand-in ({node["dpu_name"]}) — MAC {node["host_dpu_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["host_dpu_mac"]}"')
    lines.append('    set-name: eth1')
    lines.append(f'    addresses: [{node["host_internal_ip"]}/31]')
    lines.append('    routes:')
    lines.append(f'      - to: 0.0.0.0/0')
    lines.append(f'        via: {node["dpu_internal_ip"]}   # DPU stand-in internal IP')
    lines.append('    nameservers:')
    lines.append(f'      addresses: [{dns_str}]')

    return '\n'.join(lines) + '\n'


def gen_meta_data(name):
    return f'instance-id: {name}\nlocal-hostname: {name}\n'


# ── Utility VM helpers ────────────────────────────────────────────────────────

def get_utility_info(sim):
    """Return registry VM config: fabric IP (from registry_link prefix .2), MAC, OOB network, sizing."""
    util     = sim.get('nico_container_registry', {})
    util_pfx = sim['fabric'].get('registry_link', sim['fabric'].get('utility_link', {})).get('prefix', '7.132.0.4/30')
    util_net = ipaddress.IPv4Network(util_pfx, strict=False)
    util_hosts = list(util_net.hosts())
    ss_ip    = str(util_hosts[0])   # super-spine gets .1
    util_ip  = str(util_hosts[1])   # registry VM gets .2
    dc_name    = sim['fabric'].get('dc_name', 'nico-sim')
    sitename   = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx   = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    so         = _site_octet(sim)
    return {
        'name':          f'{name_pfx}{util.get("vm_prefix", "registry")}',
        'fabric_ip':     util_ip,
        'gateway_ip':    ss_ip,
        'fabric_prefix': util_pfx,
        'oob_mac':       f'52:54:{so:02x}:03:01:01',
        'fabric_mac':    f'52:54:{so:02x}:03:01:02',
        'fabric_bridge': f'br-{dc_name}-registry',
        'port':          util.get('port', 5000),
        'sizing':        util.get('sizing', {'ram_mb': 4096, 'vcpus': 2, 'disk_gb': 100}),
        'oob_network':   util.get('oob_network', {'name': f'{dc_name}-reg',
                                                    'prefix': '192.168.202.0/24'}),
    }


def gen_utility_user_data(sim, ssh_pub_key):
    """Cloud-init for utility VM: install Docker, run registry:2."""
    util_info = get_utility_info(sim)
    _, _, ns_lines = _dns_config(sim)
    reg_port = util_info['port']

    lines = []
    lines.append('#cloud-config')
    lines.append(f'hostname: {util_info["name"]}')
    lines.append(f'fqdn: {util_info["name"]}.sim.local')
    lines.append('manage_etc_hosts: true')
    lines.append('')
    pw = sim['control_plane'].get('console_password', '')
    if pw:
        lines.append('chpasswd:')
        lines.append('  expire: false')
        lines.append('  users:')
        lines.append(f'    - name: ubuntu')
        lines.append(f'      password: "{pw}"')
        lines.append(f'      type: text')
        lines.append('')
    if ssh_pub_key:
        lines.append('ssh_authorized_keys:')
        lines.append(f'  - {ssh_pub_key.strip()}')
        lines.append('')
    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('')
    lines.append('packages:')
    lines.append('  - docker.io')
    lines.append('  - python3')
    lines.append('  - net-tools')
    lines.append('  - iputils-ping')
    lines.append('package_update: true')
    lines.append('')
    lines.append('runcmd:')
    lines.append('  - systemctl enable docker')
    lines.append('  - systemctl start docker')
    lines.append(f'  - docker run -d -p {reg_port}:{reg_port} --restart=always --name registry registry:2')
    lines.append('')
    lines.append('final_message: |')
    lines.append(f'  {util_info["name"]} cloud-init complete.')
    lines.append(f'  Docker registry: http://{util_info["fabric_ip"]}:{reg_port}')
    return '\n'.join(lines) + '\n'


def gen_utility_network_config(sim):
    """Network config for registry VM: OOB DHCP + static fabric IP from registry_link prefix (.2)."""
    util     = get_utility_info(sim)
    _, all_dns, _ = _dns_config(sim)
    dns_str  = ', '.join(all_dns)
    lines = []
    lines.append('version: 2')
    lines.append('ethernets:')
    lines.append('  oob:')
    lines.append(f'    # OOB management — MAC {util["oob_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{util["oob_mac"]}"')
    lines.append('    set-name: eth0')
    lines.append('    dhcp4: true')
    lines.append('    dhcp4-overrides:')
    lines.append('      use-routes: false')
    lines.append('      use-dns: false')
    lines.append('')
    lines.append('  fabric:')
    lines.append(f'    # Direct link to super-spine via registry_link — MAC {util["fabric_mac"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{util["fabric_mac"]}"')
    lines.append('    set-name: eth1')
    lines.append(f'    addresses: [{util["fabric_ip"]}/30]')
    lines.append('    routes:')
    lines.append(f'      - to: 0.0.0.0/0')
    lines.append(f'        via: {util["gateway_ip"]}   # super-spine registry_link IP')
    lines.append('    nameservers:')
    lines.append(f'      addresses: [{dns_str}]')
    return '\n'.join(lines) + '\n'


def oob_bridge_name(oob_net_name):
    """Derive a short (≤15 char) libvirt bridge name from an OOB network name.

    libvirt always derives the bridge interface from the network name even when
    <bridge/> has no 'name' attribute — using 'virbr-{network-name}' which can
    exceed the Linux 15-char interface name limit.

    Pattern: vbr-{dc_name}-{role_abbrev}
      ytl-oob-cp       → vbr-ytl-cp   (10 chars)
      ytl-oob-mh       → vbr-ytl-mh   (10 chars)
      ytl-oob-registry → vbr-ytl-reg  (11 chars)
    """
    parts = oob_net_name.split('-oob-')
    if len(parts) == 2:
        dc, role = parts
        return f'vbr-{dc}-{role[:3]}'
    # Fallback: truncate to fit
    return f'vbr{oob_net_name}'[:15]


def gen_utility_oob_network_xml(sim):
    """OOB libvirt network for the registry VM (separate from oob-cp)."""
    dc_name = sim['fabric'].get('dc_name', 'nico-sim')
    oob_cfg = sim.get('nico_container_registry', {}).get('oob_network',
                      {'name': f'{dc_name}-reg', 'prefix': '192.168.202.0/24'})
    name    = oob_cfg['name']
    bridge  = oob_bridge_name(name)
    prefix  = oob_cfg['prefix']
    net     = ipaddress.IPv4Network(prefix, strict=False)
    gw      = str(list(net.hosts())[0])
    mask    = str(net.netmask)
    hosts   = list(net.hosts())
    dhcp_start = str(hosts[9])
    dhcp_end   = str(hosts[49])
    dns_servers = sim['control_plane'].get('dns_servers', ['8.8.8.8', '8.8.4.4'])
    forwarders  = '\n'.join(f'    <forwarder addr="{ns}"/>' for ns in dns_servers)
    return f'''\
<network>
  <name>{name}</name>
  <bridge name="{bridge}"/>
  <dns>
{forwarders}
  </dns>
  <ip address="{gw}" netmask="{mask}">
    <dhcp>
      <range start="{dhcp_start}" end="{dhcp_end}"/>
      <lease expiry="7" unit="days"/>
    </dhcp>
  </ip>
</network>
'''


# ── Cloud-init: MH host VM ────────────────────────────────────────────────────

def gen_mh_user_data(node, sim, ssh_pub_key):
    """
    Cloud-init user-data for a managed-host VM.

    Deliberately minimal: python3 only, no FRR, no k8s, no Docker.
    Nico handles all fabric configuration at ingestion time (DHCP → Nico agent
    flow).  The OOB interface (eth0) gets an IP from Nico DHCP via the relay;
    the fabric interface (eth1) is left unconfigured until Nico assigns it.
    """
    name = node['name']
    mh   = sim.get('managed_hosts', {})
    _, _, ns_lines = _dns_config(sim)
    pw   = mh.get('console_password', sim.get('control_plane', {}).get('console_password', ''))

    lines = []
    lines.append('#cloud-config')
    lines.append(f'hostname: {name}')
    lines.append(f'fqdn: {name}.sim.local')
    lines.append('manage_etc_hosts: true')
    lines.append('')

    if pw:
        lines.append('chpasswd:')
        lines.append('  expire: false')
        lines.append('  users:')
        lines.append(f'    - name: ubuntu')
        lines.append(f'      password: "{pw}"')
        lines.append(f'      type: text')
        lines.append('')

    if ssh_pub_key:
        lines.append('ssh_authorized_keys:')
        lines.append(f'  - {ssh_pub_key.strip()}')
        lines.append('')

    lines.append('bootcmd:')
    lines.append(f'  - [cloud-init-per, once, dns-setup, bash, -c, '
                 f'"rm -f /etc/resolv.conf && printf \'{ns_lines}\\n\' > /etc/resolv.conf"]')
    lines.append('')

    # Minimal package set: python3 for Nico agent tooling; no FRR, no k8s prereqs.
    lines.append('packages:')
    lines.append('  - python3')
    lines.append('  - net-tools')
    lines.append('  - iputils-ping')
    lines.append('package_update: true')
    lines.append('')

    mh_oob_name = sim.get('managed_hosts', {}).get('oob_network', {}).get(
        'name', f"{sim['fabric'].get('dc_name', 'nico-sim')}-oob-mh")
    lines.append('final_message: |')
    lines.append(f'  {name} cloud-init complete.')
    lines.append(f'  OOB MAC    : {node["oob_mac"]} — DHCP via {mh_oob_name} network')
    lines.append(f'  Fabric MAC : {node["fabric_mac"]} — NO IP; Nico configures at ingestion')
    lines.append(f'  Rack       : {node["fabric_bridge"]} (leaf-{node["leaf_index"]})')

    return '\n'.join(lines) + '\n'


def gen_mh_network_config(node, sim):
    """
    Cloud-init network-config (netplan v2) for a managed-host VM.

    eth0 (OOB MAC):    dhcp4: true — will eventually get IP from Nico DHCP via relay.
                       No default route from DHCP; fabric is the only egress path
                       but it has no IP yet, so the VM is isolated until ingested.
    eth1 (fabric MAC): NO IP, NO dhcp4 — Nico configures this at ingestion time.
                       Declared here only to set the name and prevent cloud-init
                       from assigning an IP via DHCP.
    """
    lines = []
    lines.append('version: 2')
    lines.append('ethernets:')
    lines.append('  oob:')
    lines.append(f'    # OOB management — MAC {node["oob_mac"]}')
    lines.append(f'    # Will get IP from Nico DHCP via relay-rack-{node["leaf_index"]}')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["oob_mac"]}"')
    lines.append('    set-name: eth0')
    lines.append('    dhcp4: true')
    lines.append('    dhcp4-overrides:')
    lines.append('      use-routes: false')
    lines.append('      use-dns: false')
    lines.append('')
    lines.append('  fabric:')
    lines.append(f'    # Fabric uplink to {node["fabric_bridge"]} — MAC {node["fabric_mac"]}')
    lines.append('    # NO IP — Nico configures this interface at managed-host ingestion time.')
    lines.append('    match:')
    lines.append(f'      macaddress: "{node["fabric_mac"]}"')
    lines.append('    set-name: eth1')
    lines.append('    dhcp4: false')
    lines.append('    # addresses: []  — intentionally empty; Nico assigns overlay/admin IP')

    return '\n'.join(lines) + '\n'


def gen_mh_oob_network_xml(sim):
    """OOB libvirt network for MH VMs (separate from oob-cp)."""
    mh      = sim.get('managed_hosts', {})
    dc_name = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    vm_prefix = mh.get('vm_prefix', 'mh')
    oob_cfg = mh.get('oob_network', {'name': f'{dc_name}-mh', 'prefix': '192.168.210.0/24'})
    name    = oob_cfg['name']
    bridge  = oob_bridge_name(name)
    prefix  = oob_cfg['prefix']
    net     = ipaddress.IPv4Network(prefix, strict=False)
    gw      = str(list(net.hosts())[0])
    mask    = str(net.netmask)
    hosts   = list(net.hosts())
    dhcp_start = str(hosts[9])
    dhcp_end   = str(hosts[49])
    dns_servers = sim['control_plane'].get('dns_servers', ['8.8.8.8', '8.8.4.4'])
    forwarders  = '\n'.join(f'    <forwarder addr="{ns}"/>' for ns in dns_servers)
    pfx      = sim['fabric'].get('prefixes', {})
    so       = int(pfx.get('switch_underlay', '7.0.0.0/8').split('.')[0])

    vms_cfg = {v['index']: v for v in mh.get('vms', [])}
    static_hosts = []
    num_vms_cfg = mh.get('num_vms', {})
    total = sum(num_vms_cfg.values()) if isinstance(num_vms_cfg, dict) else int(num_vms_cfg)
    for i in range(1, total + 1):
        mac    = mh_mac(i, 1, so)
        ip     = vms_cfg[i]['oob_ip'] if i in vms_cfg else str(hosts[9 + i])
        vname  = f'{name_pfx}{vm_prefix}-{i}'
        static_hosts.append(f'      <host mac="{mac}" name="{vname}" ip="{ip}"/>')

    static_xml = '\n'.join(static_hosts)
    return f'''\
<network>
  <name>{name}</name>
  <bridge name="{bridge}"/>
  <dns>
{forwarders}
  </dns>
  <ip address="{gw}" netmask="{mask}">
    <dhcp>
      <range start="{dhcp_start}" end="{dhcp_end}"/>
      <lease expiry="7" unit="days"/>
{static_xml}
    </dhcp>
  </ip>
</network>
'''


# ── OOB network XML ───────────────────────────────────────────────────────────

def gen_oob_network_xml(sim):
    """Shared OOB libvirt network for both DPU stand-in VMs and host VMs.

    Static DHCP host entries are generated for each VM so they always get
    the same IP regardless of boot order or cloud-init reboot timing.
    Without static entries, multiple VMs rebooting during cloud-init can
    cause dnsmasq to reassign IPs, breaking check-vms MAC→IP lookup.
    """
    oob      = sim['control_plane']['oob_network']
    name     = oob['name']
    bridge   = oob_bridge_name(name)
    prefix   = oob['prefix']
    internet = oob.get('internet', False)

    net        = ipaddress.IPv4Network(prefix, strict=False)
    gw         = str(list(net.hosts())[0])
    mask       = str(net.netmask)
    host_list  = list(net.hosts())
    dhcp_start = str(host_list[9])
    dhcp_end   = str(host_list[98])

    forward     = '<forward mode="nat"/>' if internet else ''
    dns_servers = sim['control_plane'].get('dns_servers', ['8.8.8.8', '8.8.4.4'])
    forwarders  = '\n'.join(f'    <forwarder addr="{ns}"/>' for ns in dns_servers)

    # Static host entries: DPUs at .10+, CP hosts at .20+
    cp    = sim['control_plane']
    n_cp  = cp['num_vms']
    vp    = cp.get('vm_prefix', 'cp')
    dp    = cp.get('dpu_prefix', 'cp-dpu')
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    name_pfx = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    pfx      = sim['fabric'].get('prefixes', {})
    so       = int(pfx.get('switch_underlay', '7.0.0.0/8').split('.')[0])

    # Build index → explicit OOB IPs from control_plane.vms (if defined)
    vms_cfg  = {v['index']: v for v in cp.get('vms', [])}

    static_hosts = []
    for i in range(1, n_cp + 1):
        dpu_mac  = f'52:54:{so:02x}:01:{i:02x}:01'
        dpu_ip   = vms_cfg[i]['dpu_oob_ip'] if i in vms_cfg else str(host_list[9 + i])
        dpu_name = f'{name_pfx}{dp}-{i}'
        static_hosts.append(f'      <host mac="{dpu_mac}" name="{dpu_name}" ip="{dpu_ip}"/>')
    for i in range(1, n_cp + 1):
        cp_mac   = f'52:54:{so:02x}:00:{i:02x}:01'
        cp_ip    = vms_cfg[i]['host_oob_ip'] if i in vms_cfg else str(host_list[19 + i])
        cp_name  = f'{name_pfx}{vp}-{i}'
        static_hosts.append(f'      <host mac="{cp_mac}" name="{cp_name}" ip="{cp_ip}"/>')

    static_xml = '\n'.join(static_hosts)

    return f'''\
<network>
  <name>{name}</name>
  <bridge name="{bridge}"/>
  {forward}
  <dns>
{forwarders}
  </dns>
  <ip address="{gw}" netmask="{mask}">
    <dhcp>
      <range start="{dhcp_start}" end="{dhcp_end}"/>
      <lease expiry="7" unit="days"/>
{static_xml}
    </dhcp>
  </ip>
</network>
'''


# ── deploy-nodes.sh ───────────────────────────────────────────────────────────

def gen_deploy_nodes_sh(nodes, sim, output_dir, mh_nodes=None, ssh_priv_path=None):
    cp       = sim['control_plane']
    oob      = cp['oob_network']
    img_url, img_name, img_dir, golden_path = resolve_image_config(sim)
    cp_ram   = cp['sizing']['ram_mb']
    cp_vcpus = cp['sizing']['vcpus']
    cp_disk  = cp['sizing']['disk_gb']
    ds       = cp.get('dpu_sizing', {'ram_mb': 2048, 'vcpus': 2, 'disk_gb': 20})
    dpu_ram  = ds['ram_mb']
    dpu_vcpu = ds['vcpus']
    dpu_disk = ds['disk_gb']

    n_pairs  = len(nodes)
    oob_name = oob['name']

    lines = []
    lines.append('#!/usr/bin/env bash')
    lines.append('# Generated by generate-nodes.py — do not edit manually')
    lines.append('#')
    lines.append('# Deploys CP node pairs sequentially: DPU stand-in first, verified via SSH,')
    lines.append('# then the CP host VM.  On ANY failure: all created resources are cleaned up')
    lines.append('# automatically before exit.  No partial state is left behind.')
    lines.append('#')
    lines.append('# Usage:  sudo ./deploy-nodes.sh')
    lines.append('# Retry:  sudo ./deploy-nodes.sh    (after a failure the script self-cleans)')
    lines.append('set -euo pipefail')
    lines.append('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"')
    lines.append('')
    lines.append('FORCE=0')
    lines.append('for arg in "$@"; do [ "$arg" = "--force" ] && FORCE=1; done')
    lines.append('')

    # Resource tracking globals and cleanup function
    lines.append('# ── Resource tracking ────────────────────────────────────────────────────────')
    lines.append('CREATED_VMS=()')
    lines.append('CREATED_DISKS=()')
    lines.append('CREATED_SEED_ISOS=()')
    lines.append('CREATED_BRIDGES=()')
    lines.append('OOB_NET_CREATED=false')
    lines.append(f'OOB_NET_NAME="{oob_name}"')
    lines.append('')
    lines.append('# ── abort: cleanup everything created so far and exit ────────────────────────')
    lines.append('# Called on any error (trap ERR/INT/TERM) or explicit failure.')
    lines.append('# Removes VMs, disks, seed ISOs, bridges, OOB network then exits 1.')
    lines.append('abort() {')
    lines.append('  local msg="$1"')
    lines.append('  set +e   # disable exit-on-error so cleanup runs fully')
    lines.append('  echo ""')
    lines.append('  echo "ERROR: $msg"')
    lines.append('  echo ""')
    lines.append('  echo "=== Auto-cleanup: removing all created resources ==="')
    lines.append('  if [ ${#CREATED_VMS[@]} -gt 0 ]; then')
    lines.append('    for vm in "${CREATED_VMS[@]}"; do')
    lines.append('      virsh dominfo "$vm" &>/dev/null || continue')
    lines.append('      virsh destroy "$vm" 2>/dev/null || true')
    lines.append('      virsh undefine "$vm" --remove-all-storage --nvram 2>/dev/null ||')
    lines.append('        virsh undefine "$vm" --remove-all-storage 2>/dev/null || true')
    lines.append('      echo "  removed VM: $vm"')
    lines.append('    done')
    lines.append('  fi')
    lines.append('  if [ ${#CREATED_DISKS[@]} -gt 0 ]; then')
    lines.append('    for f in "${CREATED_DISKS[@]}"; do rm -f "$f"; done')
    lines.append('  fi')
    lines.append('  if [ ${#CREATED_SEED_ISOS[@]} -gt 0 ]; then')
    lines.append('    for f in "${CREATED_SEED_ISOS[@]}"; do rm -f "$f"; done')
    lines.append('  fi')
    lines.append('  if [ ${#CREATED_BRIDGES[@]} -gt 0 ]; then')
    lines.append('    for br in "${CREATED_BRIDGES[@]}"; do')
    lines.append('      ip link show "$br" &>/dev/null || continue')
    lines.append('      ip link set "$br" down 2>/dev/null || true')
    lines.append('      ip link delete "$br" type bridge 2>/dev/null || true')
    lines.append('      echo "  removed bridge: $br"')
    lines.append('    done')
    lines.append('  fi')
    lines.append('  if $OOB_NET_CREATED && virsh net-info "$OOB_NET_NAME" &>/dev/null; then')
    lines.append('    virsh net-destroy "$OOB_NET_NAME" 2>/dev/null || true')
    lines.append('    virsh net-undefine "$OOB_NET_NAME" 2>/dev/null || true')
    lines.append('    echo "  removed OOB network: $OOB_NET_NAME"')
    lines.append('  fi')
    lines.append('  echo ""')
    lines.append('  echo "Cleanup complete. All created resources removed."')
    lines.append('  echo ""')
    lines.append('  echo "  Retry:  sudo ./deploy-nodes.sh"')
    lines.append('  echo "  If the problem persists, open a defect with the error above."')
    lines.append('  exit 1')
    lines.append('}')
    lines.append('')
    lines.append('# Like abort, but KEEPS all created resources — used for verification')
    lines.append('# failures where the VM itself is the evidence (auto-cleanup used to')
    lines.append('# destroy the only thing worth inspecting).')
    lines.append('abort_keep() {')
    lines.append('  local msg="$1"')
    lines.append('  set +e')
    lines.append('  trap - ERR INT TERM')
    lines.append('  echo ""')
    lines.append('  echo "ERROR: $msg"')
    lines.append('  echo ""')
    lines.append('  echo "=== Resources KEPT for debugging (no auto-cleanup) ==="')
    lines.append('  echo "  Inspect : sudo virsh console <vm-name>"')
    lines.append('  echo "  Clean up: sudo ./destroy-nodes.sh   (then re-run deploy-nodes.sh)"')
    lines.append('  exit 1')
    lines.append('}')
    lines.append('')
    lines.append("trap 'abort \"Unexpected error at line $LINENO\"' ERR")
    lines.append("trap 'abort \"Interrupted by user\"' INT TERM")
    lines.append('')

    # Helper functions
    lines.append('# ── get_oob_ip: look up DHCP lease IP by MAC, then by hostname ──────────────')
    lines.append('get_oob_ip() {')
    lines.append('  # Use "if" not "&&" throughout — with set -e, "[ -n $x ] && cmd" returns 1')
    lines.append('  # when $x is empty, triggering set -e and killing the subshell before')
    lines.append('  # the caller\'s "|| true" can catch it. "if" is exempt from set -e.')
    lines.append('  local mac="${1,,}" net="$2" hostname="${3:-}"')
    lines.append('  local leases; leases=$(virsh net-dhcp-leases "$net" 2>/dev/null || true)')
    lines.append('  local ip')
    lines.append('  # Try MAC match first')
    lines.append('  ip=$(echo "$leases" \\')
    lines.append("    | awk -v m=\"$mac\" 'tolower($3)==m && $4==\"ipv4\"{split($5,a,\"/\"); print a[1]; exit}' || true)")
    lines.append('  if [ -n "$ip" ]; then echo "$ip"; return 0; fi')
    lines.append('  # Fallback: hostname match')
    lines.append('  if [ -n "$hostname" ]; then')
    lines.append('    ip=$(echo "$leases" \\')
    lines.append("      | awk -v h=\"$hostname\" 'tolower($6)==tolower(h) && $4==\"ipv4\"{split($5,a,\"/\"); print a[1]; exit}' || true)")
    lines.append('    if [ -n "$ip" ]; then echo "$ip"; return 0; fi')
    lines.append('  fi')
    lines.append('  return 0  # always succeed — caller checks if output is empty')
    lines.append('}')
    lines.append('')
    lines.append('# ── wait_for_dhcp: poll until DHCP lease, set OOB_IP, abort on timeout ───────')
    lines.append('OOB_IP=""')
    lines.append('wait_for_dhcp() {')
    lines.append('  local vm="$1" mac="$2" net="$3" max=180 i=0')
    lines.append('  OOB_IP=""')
    lines.append('  printf "  [%s] waiting for DHCP lease..." "$vm"')
    lines.append('  while [ $i -lt $max ]; do')
    lines.append('    local ip; ip=$(get_oob_ip "$mac" "$net" "$vm")')
    lines.append('    if [ -n "$ip" ]; then')
    lines.append('      OOB_IP="$ip"')
    lines.append('      echo " got $ip (${i}s)"')
    lines.append('      return 0')
    lines.append('    fi')
    lines.append('    sleep 5; i=$((i+5)); printf "."')
    lines.append('  done')
    lines.append('  echo ""')
    lines.append('  abort_keep "$vm did not get a DHCP lease after ${max}s — VM failed to boot. Watch it boot: sudo virsh console $vm"')
    lines.append('}')
    lines.append('')
    lines.append('# ── SSH key (pinned at generation time) ──────────────────────────────────────')
    lines.append('# The public key baked into the VMs was derived from THIS private key by')
    lines.append('# generate-nodes.py — using the same key here guarantees auth matches.')
    lines.append(f'_SSH_KEY="{ssh_priv_path or ""}"')
    lines.append('[ -f "$_SSH_KEY" ] || abort "SSH key $_SSH_KEY not found — re-run generate-nodes.py on this machine"')
    lines.append('# Reject passphrase-protected keys upfront: verification uses BatchMode SSH,')
    lines.append('# where an encrypted key fails every probe silently.')
    lines.append('ssh-keygen -y -P "" -f "$_SSH_KEY" >/dev/null 2>&1 || \\')
    lines.append('  abort "SSH key $_SSH_KEY is passphrase-protected — BatchMode SSH cannot use it. Re-run generate-nodes.py with --ssh-key <unencrypted-key>"')
    lines.append('echo "  SSH key: $_SSH_KEY"')
    lines.append('')
    lines.append('# ── verify_ip_forward: SSH into DPU OOB IP and confirm ip_forward=1 ──────────')
    lines.append('# This is the definitive check — no timing assumptions.')
    lines.append('# SSH failures ("x") are tracked separately from ip_forward!=1 ("."): if SSH')
    lines.append('# never succeeds within the grace window, the problem is host-side auth or')
    lines.append('# key injection — NOT a bootcmd failure — and waiting longer is pointless.')
    lines.append('SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null')
    lines.append('         -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes')
    lines.append('         -i "$_SSH_KEY")')
    lines.append('verify_ip_forward() {')
    lines.append('  local vm="$1" oob_ip="$2" max=120 i=0 ssh_ok=0 ssh_grace=60')
    lines.append('  printf "  [%s] SSH verify ip_forward=1 at %s..." "$vm" "$oob_ip"')
    lines.append('  while [ $i -lt $max ]; do')
    lines.append('    local result rc=0')
    lines.append('    result=$(ssh "${SSH_OPTS[@]}" ubuntu@"$oob_ip" \\')
    lines.append('      "sysctl -n net.ipv4.ip_forward" 2>/dev/null) || rc=$?')
    lines.append('    if [ "$rc" -eq 0 ]; then')
    lines.append('      ssh_ok=1')
    lines.append('      if [ "$result" = "1" ]; then')
    lines.append('        echo " confirmed (${i}s)"')
    lines.append('        return 0')
    lines.append('      fi')
    lines.append('    fi')
    lines.append('    if [ "$ssh_ok" -eq 0 ] && [ $i -ge $ssh_grace ]; then')
    lines.append('      echo ""')
    lines.append('      abort_keep "$vm: no successful SSH to $oob_ip in ${ssh_grace}s — host-side SSH/key problem, not a bootcmd failure. Reproduce: ssh -o BatchMode=yes -i $_SSH_KEY ubuntu@$oob_ip true"')
    lines.append('    fi')
    lines.append('    sleep 5; i=$((i+5))')
    lines.append('    if [ "$ssh_ok" -eq 1 ]; then printf "."; else printf "x"; fi')
    lines.append('  done')
    lines.append('  echo ""')
    lines.append('  abort_keep "$vm: ip_forward not 1 after ${max}s — bootcmd may have failed. Check: virsh console $vm"')
    lines.append('}')
    lines.append('')

    # Prerequisites
    lines.append('# ── Prerequisites ────────────────────────────────────────────────────────────')
    lines.append(f'IMG="{img_dir}/{img_name}"')
    lines.append('if [ ! -f "$IMG" ]; then')
    lines.append('  echo "Downloading Ubuntu cloud image..."')
    lines.append(f'  wget -O "$IMG" "{img_url}"')
    lines.append('fi')
    lines.append('')
    # Determine host VM backing image (DPU stand-in VMs always use vanilla $IMG)
    if golden_path:
        lines.append('# golden_image provisioning: packages + kubeadm pre-installed (~30s boot)')
        lines.append(f'HOST_BACKING="{golden_path}"')
        lines.append('if [ ! -f "$HOST_BACKING" ]; then')
        lines.append(f'  abort "Golden image not found: $HOST_BACKING -- build it first: sudo python3 create-golden-image.py nico-sim.yaml"')
        lines.append('fi')
        lines.append('echo "  provisioning: golden_image ($HOST_BACKING)"')
    else:
        lines.append('# cloud_init provisioning: packages installed on first boot (~3-5 min)')
        lines.append('HOST_BACKING="$IMG"')
        lines.append('echo "  provisioning: cloud_init (vanilla Ubuntu)"')
    # Helper: define + start a libvirt network; handles already-defined-but-inactive case
    def _net_lines(net_name, xml_file, tracked=False):
        out = []
        out.append(f'if virsh net-info "{net_name}" &>/dev/null; then')
        out.append(f'  if [ "$FORCE" = "0" ]; then')
        out.append(f'    echo "ERROR: libvirt network \\"{net_name}\\" already exists."')
        out.append(f'    echo "  VMs may be running. Run: sudo python3 destroy-site.py <site>"')
        out.append(f'    echo "  Or re-run with --force to destroy and recreate."')
        out.append(f'    exit 1')
        out.append(f'  fi')
        out.append(f'  virsh net-destroy  "{net_name}" 2>/dev/null || true')
        out.append(f'  virsh net-undefine "{net_name}" 2>/dev/null || true')
        out.append(f'fi')
        out.append(f'virsh net-define "$SCRIPT_DIR/{xml_file}"')
        out.append(f'virsh net-autostart "{net_name}"')
        out.append(f'virsh net-start "{net_name}"')
        if tracked:
            out.append('OOB_NET_CREATED=true')
        out.append(f'echo "  created {net_name}"')
        return out

    lines += _net_lines(oob_name, 'oob-network.xml', tracked=True)

    # Utility VM OOB network
    util     = get_utility_info(sim)
    util_oob = util['oob_network']['name']
    lines += _net_lines(util_oob, 'oob-registry-network.xml')
    lines.append('if ! command -v cloud-localds &>/dev/null; then')
    lines.append('  apt-get install -y cloud-image-utils')
    lines.append('fi')
    lines.append('')

    # Create host-side bridges
    lines.append('# Host-side bridges (DPU stand-in <-> CP host VM)')
    for node in nodes:
        br = node['host_bridge']
        lines.append(f'if ! ip link show "{br}" &>/dev/null; then')
        lines.append(f'  ip link add "{br}" type bridge && ip link set "{br}" up')
        lines.append(f'  CREATED_BRIDGES+=("{br}")')
        lines.append(f'  echo "  created bridge {br}"')
        lines.append('fi')
    lines.append('')

    # Sequential pair creation
    lines.append('# ── Sequential pair deployment ────────────────────────────────────────────────')
    lines.append(f'echo "Deploying {n_pairs} CP node pair(s) sequentially..."')
    lines.append('echo ""')
    for idx, node in enumerate(nodes, 1):
        dpu = node['dpu_name']
        host = node['host_name']
        fbr = node['fabric_bridge']
        hbr = node['host_bridge']

        lines.append(f'echo "===== Pair {idx}/{n_pairs}: {dpu} + {host} ====="')
        lines.append('')

        # Step 1: DPU VM
        lines.append(f'echo "  Step 1/{idx}: start {dpu}"')
        lines.append(f'if virsh dominfo "{dpu}" &>/dev/null; then')
        lines.append(f'  abort "{dpu} already exists — clean up first: ~/claude-notes/nico-sim/destroy-vms.py <site-folder> --force"')
        lines.append('fi')
        lines.append(f'DPU_DISK="{img_dir}/{dpu}.qcow2"')
        lines.append(f'DPU_SEED="{img_dir}/{dpu}-seed.iso"')
        lines.append('CREATED_DISKS+=("$DPU_DISK")')
        lines.append('CREATED_SEED_ISOS+=("$DPU_SEED")')
        lines.append(f'qemu-img create -f qcow2 -F qcow2 -b "$IMG" "$DPU_DISK" {dpu_disk}G')
        lines.append(f'cloud-localds \\')
        lines.append(f'  --network-config "$SCRIPT_DIR/cloud-init/{dpu}/network-config" \\')
        lines.append(f'  "$DPU_SEED" "$SCRIPT_DIR/cloud-init/{dpu}/user-data" \\')
        lines.append(f'  "$SCRIPT_DIR/cloud-init/{dpu}/meta-data"')
        lines.append(f'virt-install \\')
        lines.append(f'  --name "{dpu}" --memory {dpu_ram} --vcpus {dpu_vcpu} \\')
        lines.append(f'  --disk "$DPU_DISK",format=qcow2,bus=virtio \\')
        lines.append(f'  --disk "$DPU_SEED",device=cdrom \\')
        lines.append(f'  --network network={oob_name},model=virtio,mac={node["dpu_oob_mac"]} \\')
        lines.append(f'  --network bridge={fbr},model=virtio,mac={node["dpu_fabric_mac"]} \\')
        lines.append(f'  --network bridge={hbr},model=virtio,mac={node["dpu_host_mac"]} \\')
        lines.append(f'  --os-variant ubuntu24.04 --noautoconsole --import')
        lines.append(f'CREATED_VMS+=("{dpu}")')
        lines.append(f'echo "  {dpu} started"')
        lines.append('')

        # Step 2: Wait for DHCP + SSH verify ip_forward
        lines.append(f'echo "  Step 2/{idx}: verify {dpu} is ready to forward traffic"')
        lines.append(f'wait_for_dhcp "{dpu}" "{node["dpu_oob_mac"]}" "{oob_name}"')
        lines.append(f'verify_ip_forward "{dpu}" "$OOB_IP"')
        lines.append(f'echo "  {dpu}: DHCP OK, ip_forward=1 confirmed via SSH — safe to start {host}"')
        lines.append('')

        # Step 3: Host VM
        lines.append(f'echo "  Step 3/{idx}: start {host}"')
        lines.append(f'if virsh dominfo "{host}" &>/dev/null; then')
        lines.append(f'  abort "{host} already exists — clean up first: ~/claude-notes/nico-sim/destroy-vms.py <site-folder> --force"')
        lines.append('fi')
        lines.append(f'HOST_DISK="{img_dir}/{host}.qcow2"')
        lines.append(f'HOST_SEED="{img_dir}/{host}-seed.iso"')
        lines.append('CREATED_DISKS+=("$HOST_DISK")')
        lines.append('CREATED_SEED_ISOS+=("$HOST_SEED")')
        lines.append(f'qemu-img create -f qcow2 -F qcow2 -b "$HOST_BACKING" "$HOST_DISK" {cp_disk}G')
        lines.append(f'cloud-localds \\')
        lines.append(f'  --network-config "$SCRIPT_DIR/cloud-init/{host}/network-config" \\')
        lines.append(f'  "$HOST_SEED" "$SCRIPT_DIR/cloud-init/{host}/user-data" \\')
        lines.append(f'  "$SCRIPT_DIR/cloud-init/{host}/meta-data"')
        lines.append(f'virt-install \\')
        lines.append(f'  --name "{host}" --memory {cp_ram} --vcpus {cp_vcpus} \\')
        lines.append(f'  --disk "$HOST_DISK",format=qcow2,bus=virtio \\')
        lines.append(f'  --disk "$HOST_SEED",device=cdrom \\')
        lines.append(f'  --network network={oob_name},model=virtio,mac={node["host_oob_mac"]} \\')
        lines.append(f'  --network bridge={hbr},model=virtio,mac={node["host_dpu_mac"]} \\')
        lines.append(f'  --os-variant ubuntu24.04 --noautoconsole --import')
        lines.append(f'CREATED_VMS+=("{host}")')
        lines.append(f'echo "  {host} started — internet via {dpu} (ip_forward=1 verified)"')
        lines.append(f'echo "===== Pair {idx}/{n_pairs} done ====="')
        lines.append('echo ""')
        lines.append('')

    lines.append(f'echo "All {n_pairs} pairs deployed."')
    lines.append('echo ""')

    # ── MH VMs ────────────────────────────────────────────────────────────────
    # Deployed AFTER CP pairs.  br-{dc_name}-mhN bridges must exist (created by
    # fabric/deploy.sh via ContainerLab) before these VMs start.
    # Guard: only emitted when mh_nodes is non-empty (managed_hosts.num_vms > 0).
    if mh_nodes:
        mh               = sim['managed_hosts']
        mh_oob           = mh['oob_network']
        mh_oob_name      = mh_oob['name']
        mh_provisioning  = mh.get('provisioning', 'bare_metal')
        # os_image_* only needed for golden_image/cloud_init — not for bare_metal
        if mh_provisioning == 'golden_image':
            gi_mh       = sim.get('golden_image', {}).get('managed_hosts', {})
            mh_img_url  = gi_mh.get('os_image_url',  mh.get('os_image_url', ''))
            mh_img_name = gi_mh.get('os_image_name', mh.get('os_image_name', ''))
            mh_img_dir  = gi_mh.get('os_image_dir',  mh.get('os_image_dir', '/var/lib/libvirt/images'))
        elif mh_provisioning == 'bare_metal':
            mh_img_url  = ''
            mh_img_name = ''
            mh_img_dir  = mh.get('os_image_dir', '/var/lib/libvirt/images')
        else:
            mh_img_url  = mh.get('os_image_url', '')
            mh_img_name = mh.get('os_image_name', '')
            mh_img_dir  = mh.get('os_image_dir', '/var/lib/libvirt/images')
        mh_sz       = mh['sizing']
        mh_ram      = mh_sz['ram_mb']
        mh_vcpu     = mh_sz['vcpus']
        mh_disk     = mh_sz['disk_gb']

        lines.append('# ═══ Managed-host VMs ══════════════════════════════════════════════')
        lines.append(f'echo "===== Managed-host VMs ({len(mh_nodes)} total, provisioning={mh_provisioning}) ====="')

        if mh_provisioning == 'bare_metal':
            # Empty disk — no OS. VM boots → disk empty → PXE/network boot →
            # DHCP on OOB → Nico discovers it. Correct production model.
            lines.append(f'MH_DISK_DIR="{mh_img_dir}"')
        elif mh_provisioning == 'golden_image':
            gi_mh_path = sim.get('golden_image', {}).get('managed_hosts', {}).get('path', '')
            lines.append(f'MH_BACKING="{gi_mh_path}"')
            lines.append(f'if [ ! -f "$MH_BACKING" ]; then')
            lines.append(f'  echo "Error: MH golden image not found: $MH_BACKING"')
            lines.append(f'  echo "  Run: sudo python3 create-golden-image.py nico-sim.yaml --target mh"')
            lines.append(f'  exit 1')
            lines.append(f'fi')
        else:
            lines.append(f'MH_BACKING="{mh_img_dir}/{mh_img_name}"')
            lines.append('if [ ! -f "$MH_BACKING" ]; then')
            lines.append(f'  wget -O "$MH_BACKING" "{mh_img_url}"')
            lines.append('fi')
        lines.append('')

        lines += _net_lines(mh_oob_name, 'oob-mh-network.xml')

        lines.append('')
        lines.append(f'# Verify fabric bridges exist (created by fabric/deploy.sh)')
        for mhn in mh_nodes:
            br = mhn['fabric_bridge']
            lines.append(f'if ! ip link show "{br}" &>/dev/null; then')
            lines.append(f'  abort "{br} bridge not found — run fabric/deploy.sh before deploy-nodes.sh"')
            lines.append(f'fi')
            lines.append(f'echo "  {br}: exists"')
        lines.append('')

        for mhn in mh_nodes:
            vm_name = mhn['name']
            lines.append(f'echo "  Starting {vm_name}..."')
            lines.append(f'if virsh dominfo "{vm_name}" &>/dev/null; then')
            lines.append(f'  abort "{vm_name} already exists — clean up first: ./destroy-nodes.sh"')
            lines.append('fi')
            lines.append(f'MH_DISK="{mh_img_dir}/{vm_name}.qcow2"')
            lines.append('CREATED_DISKS+=("$MH_DISK")')

            if mh_provisioning == 'bare_metal':
                # Create empty disk — no OS, no cloud-init seed.
                # Boot order: network first (PXE/DHCP), then disk.
                lines.append(f'qemu-img create -f qcow2 "$MH_DISK" {mh_disk}G')
                lines.append(f'virt-install \\')
                lines.append(f'  --name "{vm_name}" --memory {mh_ram} --vcpus {mh_vcpu} \\')
                lines.append(f'  --disk "$MH_DISK",format=qcow2,bus=virtio \\')
                lines.append(f'  --network network={mh_oob_name},model=virtio,mac={mhn["oob_mac"]} \\')
                lines.append(f'  --network bridge={mhn["fabric_bridge"]},model=virtio,mac={mhn["fabric_mac"]} \\')
                lines.append(f'  --os-variant ubuntu24.04 --pxe --noautoconsole')
            else:
                # golden_image / cloud_init: full OS boot with cloud-init seed
                lines.append(f'MH_SEED="{mh_img_dir}/{vm_name}-seed.iso"')
                lines.append('CREATED_SEED_ISOS+=("$MH_SEED")')
                if mh_provisioning == 'golden_image':
                    lines.append(f'qemu-img create -f qcow2 -F qcow2 -b "$MH_BACKING" "$MH_DISK" {mh_disk}G')
                lines.append(f'cloud-localds \\')
                lines.append(f'  --network-config "$SCRIPT_DIR/cloud-init/{vm_name}/network-config" \\')
                lines.append(f'  "$MH_SEED" "$SCRIPT_DIR/cloud-init/{vm_name}/user-data" \\')
                lines.append(f'  "$SCRIPT_DIR/cloud-init/{vm_name}/meta-data"')
                lines.append(f'virt-install \\')
                lines.append(f'  --name "{vm_name}" --memory {mh_ram} --vcpus {mh_vcpu} \\')
                lines.append(f'  --disk "$MH_DISK",format=qcow2,bus=virtio \\')
                lines.append(f'  --disk "$MH_SEED",device=cdrom \\')
                lines.append(f'  --network network={mh_oob_name},model=virtio,mac={mhn["oob_mac"]} \\')
                lines.append(f'  --network bridge={mhn["fabric_bridge"]},model=virtio,mac={mhn["fabric_mac"]} \\')
                lines.append(f'  --os-variant ubuntu24.04 --noautoconsole --import')

            lines.append(f'CREATED_VMS+=("{vm_name}")')
            lines.append(f'echo "  {vm_name} started (OOB MAC: {mhn["oob_mac"]})"')
            lines.append('')

        lines.append(f'echo "All MH VMs started."')
        lines.append('echo "  eth0 (OOB):    DHCP — Nico DHCP relay will assign IP and discover host"')
        lines.append('echo "  eth1 (fabric): NO IP — Nico assigns at managed-host ingestion"')
        lines.append('echo ""')

    # Utility VM — deploy after CP pairs (no ordering dependency, but logical last)
    util      = get_utility_info(sim)
    util_name = util['name']
    util_oob  = util['oob_network']['name']
    util_sz   = util['sizing']
    lines.append('# ═══ Utility VM ════════════════════════════════════════════════')
    lines.append(f'echo "===== Utility VM: {util_name} ====="')
    lines.append(f'if virsh dominfo "{util_name}" &>/dev/null; then')
    lines.append(f'  abort "{util_name} already exists — clean up first: ~/claude-notes/nico-sim/destroy-vms.py <site-folder> --force"')
    lines.append('fi')
    lines.append(f'UTIL_DISK="{img_dir}/{util_name}.qcow2"')
    lines.append(f'UTIL_SEED="{img_dir}/{util_name}-seed.iso"')
    lines.append('CREATED_DISKS+=("$UTIL_DISK")')
    lines.append('CREATED_SEED_ISOS+=("$UTIL_SEED")')
    lines.append(f'qemu-img create -f qcow2 -F qcow2 -b "$IMG" "$UTIL_DISK" {util_sz["disk_gb"]}G')
    lines.append(f'cloud-localds \\')
    lines.append(f'  --network-config "$SCRIPT_DIR/cloud-init/{util_name}/network-config" \\')
    lines.append(f'  "$UTIL_SEED" "$SCRIPT_DIR/cloud-init/{util_name}/user-data" \\')
    lines.append(f'  "$SCRIPT_DIR/cloud-init/{util_name}/meta-data"')
    lines.append(f'virt-install \\')
    lines.append(f'  --name "{util_name}" --memory {util_sz["ram_mb"]} --vcpus {util_sz["vcpus"]} \\')
    lines.append(f'  --disk "$UTIL_DISK",format=qcow2,bus=virtio \\')
    lines.append(f'  --disk "$UTIL_SEED",device=cdrom \\')
    lines.append(f'  --network network={util_oob},model=virtio,mac={util["oob_mac"]} \\')
    lines.append(f'  --network bridge={util["fabric_bridge"]},model=virtio,mac={util["fabric_mac"]} \\')
    lines.append(f'  --os-variant ubuntu24.04 --noautoconsole --import \\')
    lines.append(f'  --check mac_in_use=off')
    lines.append(f'CREATED_VMS+=("{util_name}")')
    lines.append(f'echo "  {util_name} started — Docker + registry:2 installing (~3 min)"')
    lines.append('echo ""')
    lines.append('echo "All nodes started."')
    lines.append('echo "DPU stand-in VMs: installing FRR + BGP (~3-5 min)"')
    lines.append('echo "CP host VMs:      installing packages (~2 min)"')
    lines.append(f'echo "Utility VM:       installing Docker + starting registry:2 (~3 min)"')
    lines.append('echo ""')
    lines.append('echo "Monitor (inside VM):  sudo cat /var/log/cloud-init-output.log"')
    lines.append('echo "SSH via OOB:          python3 ssh-vm.py nico-sim.yaml <name> --via oob"')
    lines.append(f'echo "Registry endpoint:    http://{util["fabric_ip"]}:{util["port"]}"')

    return '\n'.join(lines) + '\n'


# ── destroy-nodes.sh ──────────────────────────────────────────────────────────

def gen_destroy_nodes_sh(nodes, sim, mh_nodes=None):
    oob     = sim['control_plane']['oob_network']
    _, _, img_dir, _ = resolve_image_config(sim)
    util    = get_utility_info(sim)

    all_vms = [util['name']]   # utility VM first
    for node in nodes:
        all_vms.append(node['dpu_name'])
        all_vms.append(node['host_name'])
    if mh_nodes:
        for mhn in mh_nodes:
            all_vms.append(mhn['name'])

    lines = []
    lines.append('#!/usr/bin/env bash')
    lines.append('# Generated by generate-nodes.py — removes all DPU + host VMs and bridges')
    lines.append('set -euo pipefail')
    lines.append('')

    lines.append('echo "=== Stopping and removing VMs ==="')
    for name in all_vms:
        lines.append(f'if virsh dominfo {name} &>/dev/null; then')
        lines.append(f'  virsh destroy {name} 2>/dev/null || true')
        lines.append(f'  virsh undefine {name} --remove-all-storage --nvram 2>/dev/null || \\')
        lines.append(f'    virsh undefine {name} --remove-all-storage 2>/dev/null || true')
        lines.append(f'  echo "  removed {name}"')
        lines.append(f'fi')
        lines.append(f'rm -f {img_dir}/{name}-seed.iso')
    lines.append('')

    lines.append('echo "=== Removing host-side bridges ==="')
    for node in nodes:
        br = node['host_bridge']
        lines.append(f'if ip link show {br} &>/dev/null; then')
        lines.append(f'  ip link set {br} down 2>/dev/null || true')
        lines.append(f'  ip link delete {br} type bridge 2>/dev/null || true')
        lines.append(f'  echo "  removed {br}"')
        lines.append(f'fi')
    lines.append('')

    lines.append('echo "=== Removing OOB networks ==="')
    oob_nets = [oob['name'], util['oob_network']['name']]
    if mh_nodes:
        mh_oob = sim.get('managed_hosts', {}).get('oob_network', {}).get(
            'name', f"{sim['fabric'].get('dc_name', 'nico-sim')}-oob-mh")
        if mh_oob not in oob_nets:
            oob_nets.append(mh_oob)
    for net_name in oob_nets:
        lines.append(f'if virsh net-info {net_name} &>/dev/null; then')
        lines.append(f'  virsh net-destroy {net_name} 2>/dev/null || true')
        lines.append(f'  virsh net-undefine {net_name} 2>/dev/null || true')
        lines.append(f'  echo "  removed {net_name}"')
        lines.append('fi')
    if mh_nodes:
        lines.append('')
        lines.append('# Note: br-{dc_name}-mhN bridges are ContainerLab-managed; removed by:')
        lines.append('#   cd output && sudo clab destroy -t topo.clab.yml --cleanup')
    lines.append('')
    lines.append('echo "Done. Re-run deploy-nodes.sh to recreate."')

    return '\n'.join(lines) + '\n'


# ── node-reference.txt ────────────────────────────────────────────────────────

def gen_node_reference(nodes, sim):
    pfx = sim['fabric']['prefixes']
    cp  = sim['control_plane']
    lines = []
    lines.append('# Node Reference — generated by generate-nodes.py')
    lines.append(f'# dpu_fabric          : {pfx["dpu_fabric"]}')
    cp_pfx = pfx.get('control_plane_prefix', cp.get('control_plane_prefix', ''))
    lines.append(f'# control_plane_prefix: {cp_pfx}')
    lines.append('')

    for node in nodes:
        lines.append(f'## {node["host_name"]} + {node["dpu_name"]}')
        lines.append(f'#')
        lines.append(f'# DPU stand-in ({node["dpu_name"]}):')
        lines.append(f'#   fabric bridge : {node["fabric_bridge"]}')
        lines.append(f'#   loopback      : {node["dpu_loopback"]}/32  (BGP router-ID, future VTEP)')
        lines.append(f'#   fabric IP     : {node["dpu_fabric_ip"]}/31  '
                     f'(leaf: {node["leaf_ip"]}, ASN {node["leaf_asn"]} ↔ DPU ASN {node["dpu_asn"]})')
        lines.append(f'#   internal IP   : {node["dpu_internal_ip"]}/31  (eth2 → {node["host_bridge"]})')
        lines.append(f'#   OOB MAC       : {node["dpu_oob_mac"]}')
        lines.append(f'#   fabric MAC    : {node["dpu_fabric_mac"]}')
        lines.append(f'#   host-side MAC : {node["dpu_host_mac"]}')
        lines.append(f'#')
        lines.append(f'# CP host VM ({node["host_name"]}):')
        lines.append(f'#   host bridge   : {node["host_bridge"]}')
        lines.append(f'#   internal IP   : {node["host_internal_ip"]}/31  (gateway: {node["dpu_internal_ip"]})')
        lines.append(f'#   OOB MAC       : {node["host_oob_mac"]}')
        lines.append(f'#   DPU-link MAC  : {node["host_dpu_mac"]}')
        lines.append('')

    return '\n'.join(lines) + '\n'


# ── SSH key ───────────────────────────────────────────────────────────────────

def find_ssh_key(override):
    """Resolve the SSH key pair used for VM access.

    Returns (pub_key_text, priv_key_path). The public key injected into the
    VMs is ALWAYS derived from the private key (ssh-keygen -y), so the pair
    can never mismatch. Passphrase-protected keys are rejected: the generated
    deploy-nodes.sh verifies VMs over BatchMode SSH, where an encrypted key
    fails every probe silently (surfacing as a bogus "ip_forward not 1").

    --ssh-key accepts either half of the pair. Auto-discovery skips unusable
    (passphrase-protected) keys and tries the next candidate.
    """
    if override:
        p = Path(override).expanduser()
        if not p.exists():
            raise FileNotFoundError(f'SSH key not found: {override}')
        priv = p.with_suffix('') if p.suffix == '.pub' else p
        if not priv.exists():
            raise FileNotFoundError(
                f'{p} is a public key but its private half {priv} does not exist.\n'
                f'  Pass the private key with --ssh-key instead.')
        candidates = [priv]
    else:
        # When running as root via sudo, prefer the original user's keys
        sudo_user = os.environ.get('SUDO_USER')
        search_homes = []
        if sudo_user:
            search_homes.append(Path(f'/home/{sudo_user}'))
        search_homes.append(Path('~').expanduser())
        candidates = []
        for home in search_homes:
            for name in ['id_nico_sim', 'id_ed25519', 'id_rsa', 'id_ecdsa']:
                pv = home / '.ssh' / name
                if pv.exists() and pv not in candidates:
                    candidates.append(pv)

    unusable = []
    for priv in candidates:
        r = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(priv)],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), str(priv)
        unusable.append(str(priv))

    if candidates:
        raise RuntimeError(
            'No usable SSH private key — these are passphrase-protected (or unreadable),\n'
            'and BatchMode SSH in deploy-nodes.sh cannot prompt for passphrases:\n'
            + ''.join(f'  {p}\n' for p in unusable)
            + 'Create a dedicated unencrypted key and pass it explicitly:\n'
            '  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_nico_sim\n'
            '  ... --ssh-key ~/.ssh/id_nico_sim')

    # No keys at all — generate a dedicated pair
    priv_path = Path('~/.ssh/id_nico_sim').expanduser()
    priv_path.parent.mkdir(mode=0o700, exist_ok=True)
    r = subprocess.run(
        ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(priv_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f'ssh-keygen failed: {r.stderr.strip()}')
    print(f'  Generated new SSH key pair: {priv_path}')
    pub = Path(str(priv_path) + '.pub').read_text().strip()
    return pub, str(priv_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args   = parse_args()
    sim_yaml, fabric_dir, vm_dir = resolve_site(args.site)
    sim    = load_sim(sim_yaml)
    output = Path(args.output_dir or vm_dir)

    # Validate provisioning and cross-references before doing anything else
    validate_provisioning(sim)
    validate_sim(sim)

    topo_path = Path(args.topo) if args.topo else Path(fabric_dir) / 'topo.clab.yml'

    if not topo_path.exists():
        print(f'Error: topology file not found: {topo_path}', file=sys.stderr)
        print(f'  → Run generate-fabric.py first', file=sys.stderr)
        sys.exit(1)

    topo = load_topo(topo_path)

    img_url, img_name, img_dir, golden_path = resolve_image_config(sim)
    provisioning = sim['control_plane'].get('provisioning', 'cloud_init')

    print('DC Simulation — Node generator (DPU stand-ins + CP host VMs)')
    print(f'  sim config   : {sim_yaml}')
    print(f'  topology     : {topo_path}')
    print(f'  output       : {output}')
    print(f'  provisioning : {provisioning}'
          + (f'  ({golden_path})' if golden_path else ''))
    pfx = sim['fabric']['prefixes']
    cp  = sim['control_plane']
    fab = sim['fabric']
    print(f'  dpu_fabric           : {pfx["dpu_fabric"]}')
    print(f'  dpu_loopbacks        : {pfx.get("dpu_loopbacks", "")}')
    print(f'  control_plane_prefix : {pfx.get("control_plane_prefix", cp.get("control_plane_prefix", ""))}')
    print(f'  switch_asn_base      : {fab.get("switch_asn_base", fab.get("asn_base"))}')
    print(f'  dpu_asn_base         : {fab.get("dpu_asn_base")}')
    print()

    # ── Verify fabric ─────────────────────────────────────────────────────────
    print('Verifying fabric...')
    cp_bridges = verify_topology_structure(topo, sim)
    verify_fabric_running(sim)
    if not args.skip_bgp_check:
        verify_bgp_state(sim)
    else:
        print('  BGP check skipped (--skip-bgp-check)')
    print()

    # ── Allocate IPs and MACs ─────────────────────────────────────────────────
    nodes = allocate_cp_nodes(sim, cp_bridges)

    print('Node assignments:')
    for n in nodes:
        print(f'  {n["dpu_name"]:12s}  loopback={n["dpu_loopback"]}/32  '
              f'fabric={n["dpu_fabric_ip"]}/31  internal={n["dpu_internal_ip"]}/31  ASN={n["dpu_asn"]}')
        print(f'  {n["host_name"]:12s}  internal={n["host_internal_ip"]}/31  '
              f'via DPU={n["dpu_name"]}')
    print()

    # ── Managed-host VMs ─────────────────────────────────────────────────────
    mh_cfg    = sim.get('managed_hosts', {})
    _mh_vms   = mh_cfg.get('num_vms', {})
    mh_total  = sum(_mh_vms.values()) if isinstance(_mh_vms, dict) else int(_mh_vms)
    mh_nodes  = []
    if mh_total > 0:
        mh_nodes = allocate_mh_nodes(sim)
        print(f'Managed-host VM assignments ({mh_total} VMs):')
        for mhn in mh_nodes:
            print(f'  {mhn["name"]:8s}  oob_mac={mhn["oob_mac"]}  '
                  f'fabric_mac={mhn["fabric_mac"]}  bridge={mhn["fabric_bridge"]}')
        print()
    else:
        print('  managed_hosts.num_vms=0 — skipping MH VM generation')

    # ── SSH key ───────────────────────────────────────────────────────────────
    ssh_pub_key, ssh_priv_path = find_ssh_key(args.ssh_key)
    print(f'  SSH key: {ssh_priv_path} (public key derived from it)')
    print()

    # ── Generate files ────────────────────────────────────────────────────────
    output.mkdir(parents=True, exist_ok=True)

    (output / 'oob-network.xml').write_text(gen_oob_network_xml(sim))
    print('  [ok] oob-network.xml')

    (output / 'oob-registry-network.xml').write_text(gen_utility_oob_network_xml(sim))
    print('  [ok] oob-registry-network.xml')

    if mh_nodes:
        (output / 'oob-mh-network.xml').write_text(gen_mh_oob_network_xml(sim))
        print('  [ok] oob-mh-network.xml')

    # Utility VM cloud-init
    util_info = get_utility_info(sim)
    util_dir  = output / 'cloud-init' / util_info['name']
    util_dir.mkdir(parents=True, exist_ok=True)
    (util_dir / 'user-data').write_text(gen_utility_user_data(sim, ssh_pub_key))
    (util_dir / 'meta-data').write_text(gen_meta_data(util_info['name']))
    (util_dir / 'network-config').write_text(gen_utility_network_config(sim))
    print(f'  [ok] cloud-init/{util_info["name"]}/')

    ci_dir = output / 'cloud-init'
    for node in nodes:
        # DPU stand-in cloud-init
        dpu_dir = ci_dir / node['dpu_name']
        dpu_dir.mkdir(parents=True, exist_ok=True)
        (dpu_dir / 'user-data').write_text(gen_dpu_user_data(node, sim, ssh_pub_key))
        (dpu_dir / 'meta-data').write_text(gen_meta_data(node['dpu_name']))
        (dpu_dir / 'network-config').write_text(gen_dpu_network_config(node, sim))
        print(f'  [ok] cloud-init/{node["dpu_name"]}/')

        # CP host VM cloud-init
        host_dir = ci_dir / node['host_name']
        host_dir.mkdir(parents=True, exist_ok=True)
        (host_dir / 'user-data').write_text(gen_host_user_data(node, sim, ssh_pub_key))
        (host_dir / 'meta-data').write_text(gen_meta_data(node['host_name']))
        (host_dir / 'network-config').write_text(gen_host_network_config(node, sim))
        print(f'  [ok] cloud-init/{node["host_name"]}/')

    # MH VM cloud-init
    for mhn in mh_nodes:
        mh_dir = ci_dir / mhn['name']
        mh_dir.mkdir(parents=True, exist_ok=True)
        (mh_dir / 'user-data').write_text(gen_mh_user_data(mhn, sim, ssh_pub_key))
        (mh_dir / 'meta-data').write_text(gen_meta_data(mhn['name']))
        (mh_dir / 'network-config').write_text(gen_mh_network_config(mhn, sim))
        print(f'  [ok] cloud-init/{mhn["name"]}/')

    deploy_path = output / 'deploy-nodes.sh'
    deploy_path.write_text(gen_deploy_nodes_sh(nodes, sim, output, mh_nodes=mh_nodes,
                                               ssh_priv_path=ssh_priv_path))
    deploy_path.chmod(0o755)
    print('  [ok] deploy-nodes.sh')

    destroy_path = output / 'destroy-nodes.sh'
    destroy_path.write_text(gen_destroy_nodes_sh(nodes, sim, mh_nodes=mh_nodes))
    destroy_path.chmod(0o755)
    print('  [ok] destroy-nodes.sh')

    (output / 'node-reference.txt').write_text(gen_node_reference(nodes, sim))
    print('  [ok] node-reference.txt')

    print()
    print('Deploy nodes:')
    print(f'  cd {output} && sudo ./deploy-nodes.sh')
    print()
    print('Destroy nodes:')
    print(f'  cd {output} && sudo ./destroy-nodes.sh')
    print()
    print('DPU stand-in VMs boot order:')
    print('  1. DPU stand-in boots first → installs FRR → starts BGP with leaf')
    print('  2. CP host VM boots → gets route via DPU stand-in → internet works')
    print('  3. BGP on leaf shows DPU stand-in as Established neighbor')


if __name__ == '__main__':
    main()
