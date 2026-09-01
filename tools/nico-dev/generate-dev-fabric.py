#!/usr/bin/env python3
"""
nico-dev — Generate ContainerLab fabric topology for the dev sim.

Topology: super-spine → spine-1 → leaf-cp + leaf-mat
  - leaf-cp:  CP leaf, wired to DPU stand-in container and the CP bridge (br-<dc>-cp)
  - leaf-mat: MAT leaf, wired to the internet bridge (br-<dc>-internet)

Output: {site}/fabric/topo.clab.yml + per-node FRR configs

Usage:
  python3 generate-dev-fabric.py <site>
"""

import ipaddress
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml')
                 if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def hosts(prefix):
    net = ipaddress.IPv4Network(prefix, strict=False)
    return [str(h) for h in net.hosts()]


def slash31(prefix):
    net = ipaddress.IPv4Network(prefix, strict=False)
    h = list(net.hosts())
    return str(h[0]), str(h[1])


def prefix_first(prefix):
    return str(list(ipaddress.IPv4Network(prefix, strict=False).hosts())[0])


# ── FRR config generators ─────────────────────────────────────────────────────

def frr_daemons():
    return '\n'.join([
        'bgpd=yes',
        'zebra=yes',
        'staticd=yes',
        'vtysh_enable=yes',
        'zebra_options=" -s 90000000 --daemon -A 127.0.0.1"',
        'bgpd_options="   --daemon -A 127.0.0.1"',
        'staticd_options=" --daemon -A 127.0.0.1"',
    ]) + '\n'


def frr_super_spine(cfg, ss_lo, ss_spine_ip, spine_lo, spine_ss_ip, host_inet_ip):
    pfx      = cfg['fabric']['prefixes']
    asn_base = cfg['fabric']['switch_asn_base']
    ss_asn   = asn_base
    sp_asn   = asn_base + 1
    # host_inet_ip is the host side of the internet uplink (e.g. 7.132.0.1)
    # ss_inet_ip is super-spine's side (host_inet_ip + 1)
    inet_net  = ipaddress.IPv4Network(pfx['internet_uplink'], strict=False)
    inet_h    = list(inet_net.hosts())
    ss_inet_ip = str(inet_h[1])   # super-spine side (.2)

    return f'''\
frr version 9.1
frr defaults datacenter
hostname super-spine
log syslog informational
!
interface lo
 ip address {ss_lo}/32
!
interface eth1
 ip address {ss_spine_ip}/31
!
! eth2: internet uplink to host (internet bridge)
interface eth2
 ip address {ss_inet_ip}/30
!
! Default route via host for internet access
ip route 0.0.0.0/0 {host_inet_ip}
!
router bgp {ss_asn}
 bgp router-id {ss_lo}
 bgp bestpath as-path multipath-relax
 !
 neighbor {spine_ss_ip} remote-as {sp_asn}
 neighbor {spine_ss_ip} description spine-1
 !
 address-family ipv4 unicast
  network {ss_lo}/32
  network 0.0.0.0/0
  neighbor {spine_ss_ip} activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor {spine_ss_ip} activate
 exit-address-family
!
'''


def frr_spine(cfg, sp_lo, sp_ss_ip, ss_lo, ss_sp_ip,
              sp_cp_ip, lcp_sp_ip, sp_mat_ip, lmat_sp_ip):
    asn_base = cfg['fabric']['switch_asn_base']
    ss_asn   = asn_base
    sp_asn   = asn_base + 1
    lcp_asn  = asn_base + 11
    lmat_asn = asn_base + 16

    return f'''\
frr version 9.1
frr defaults datacenter
hostname spine-1
log syslog informational
!
interface lo
 ip address {sp_lo}/32
!
interface eth1
 ip address {sp_ss_ip}/31
!
interface eth2
 ip address {sp_cp_ip}/31
!
interface eth3
 ip address {sp_mat_ip}/31
!
router bgp {sp_asn}
 bgp router-id {sp_lo}
 bgp bestpath as-path multipath-relax
 !
 neighbor {ss_sp_ip} remote-as {ss_asn}
 neighbor {ss_sp_ip} description super-spine
 neighbor {lcp_sp_ip} remote-as {lcp_asn}
 neighbor {lcp_sp_ip} description leaf-cp
 neighbor {lmat_sp_ip} remote-as {lmat_asn}
 neighbor {lmat_sp_ip} description leaf-mat
 !
 address-family ipv4 unicast
  network {sp_lo}/32
  neighbor {ss_sp_ip} activate
  neighbor {lcp_sp_ip} activate
  neighbor {lmat_sp_ip} activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor {ss_sp_ip} activate
  neighbor {lcp_sp_ip} activate
  neighbor {lmat_sp_ip} activate
 exit-address-family
!
'''


def frr_leaf_cp(cfg, lcp_lo, lcp_sp_ip, sp_lcp_ip, lcp_dpu_ip, dpu_lcp_ip):
    pfx    = cfg['fabric']['prefixes']
    asn_base = cfg['fabric']['switch_asn_base']
    sp_asn   = asn_base + 1
    lcp_asn  = asn_base + 11
    dpu_asn  = cfg['fabric']['dpu_asn']
    cp_link  = pfx['control_plane_link']

    # Advertise the CP link subnet so VM host (CP node) is reachable via fabric
    cp_link_net = str(ipaddress.IPv4Network(cp_link, strict=False))

    return f'''\
frr version 9.1
frr defaults datacenter
hostname leaf-cp
log syslog informational
!
interface lo
 ip address {lcp_lo}/32
!
interface eth1
 ip address {lcp_sp_ip}/31
!
! eth2 connects to the CP bridge (br-<dc>-cp) (DPU stand-in + VM host CP interface)
interface eth2
 ip address {lcp_dpu_ip}/31
!
router bgp {lcp_asn}
 bgp router-id {lcp_lo}
 bgp bestpath as-path multipath-relax
 !
 neighbor {sp_lcp_ip} remote-as {sp_asn}
 neighbor {sp_lcp_ip} description spine-1
 neighbor {dpu_lcp_ip} remote-as {dpu_asn}
 neighbor {dpu_lcp_ip} description dpu-1
 !
 address-family ipv4 unicast
  network {lcp_lo}/32
  network {cp_link_net}
  neighbor {sp_lcp_ip} activate
  neighbor {dpu_lcp_ip} activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor {sp_lcp_ip} activate
  neighbor {dpu_lcp_ip} activate
 exit-address-family
!
'''


def frr_leaf_mat(cfg, lmat_lo, lmat_sp_ip, sp_lmat_ip, mat_gw):
    pfx      = cfg['fabric']['prefixes']
    asn_base = cfg['fabric']['switch_asn_base']
    sp_asn   = asn_base + 1
    lmat_asn = asn_base + 16
    mat_pfx  = pfx['mat_underlay']
    mat_net  = str(ipaddress.IPv4Network(mat_pfx, strict=False))
    mat_plen = ipaddress.IPv4Network(mat_pfx, strict=False).prefixlen

    return f'''\
frr version 9.1
frr defaults datacenter
hostname leaf-mat
log syslog informational
!
interface lo
 ip address {lmat_lo}/32
!
interface eth1
 ip address {lmat_sp_ip}/31
!
! eth2 connects to the internet bridge (MAT BMC mocks)
interface eth2
 ip address {mat_gw}/{mat_plen}
!
router bgp {lmat_asn}
 bgp router-id {lmat_lo}
 bgp bestpath as-path multipath-relax
 !
 neighbor {sp_lmat_ip} remote-as {sp_asn}
 neighbor {sp_lmat_ip} description spine-1
 !
 address-family ipv4 unicast
  network {lmat_lo}/32
  network {mat_net}
  neighbor {sp_lmat_ip} activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor {sp_lmat_ip} activate
 exit-address-family
!
'''


def frr_dpu(cfg, dpu_lo, dpu_lcp_ip, lcp_dpu_ip, dpu_cp_ip, cp_dpu_ip):
    pfx     = cfg['fabric']['prefixes']
    dpu_asn = cfg['fabric']['dpu_asn']
    mb_asn  = cfg['fabric']['metallb_asn']
    lcp_asn = cfg['fabric']['switch_asn_base'] + 11
    cp_link = pfx['control_plane_link']
    cp_net  = str(ipaddress.IPv4Network(cp_link, strict=False))

    return f'''\
frr version 9.1
frr defaults datacenter
hostname dpu-1
log syslog informational
!
interface lo
 ip address {dpu_lo}/32
!
! eth1: fabric uplink to leaf-cp (eth0 reserved for ContainerLab mgmt)
interface eth1
 ip address {dpu_lcp_ip}/31
 ip forwarding
!
! eth2: internal link to VM host (CP node)
interface eth2
 ip address {dpu_cp_ip}/31
 ip forwarding
!
ip forwarding
!
router bgp {dpu_asn}
 bgp router-id {dpu_lo}
 bgp bestpath as-path multipath-relax
 !
 neighbor {lcp_dpu_ip} remote-as {lcp_asn}
 neighbor {lcp_dpu_ip} description leaf-cp
 neighbor {cp_dpu_ip} remote-as {mb_asn}
 neighbor {cp_dpu_ip} description metallb
 !
 address-family ipv4 unicast
  network {dpu_lo}/32
  network {cp_net}
  neighbor {lcp_dpu_ip} activate
  neighbor {cp_dpu_ip} activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor {lcp_dpu_ip} activate
  advertise ipv4 unicast
 exit-address-family
!
'''


# ── ContainerLab topology ─────────────────────────────────────────────────────

def gen_topo(cfg, ips, dc):
    image    = 'frr-local:local'   # local apt-built FRR (native arch)
    mgmt_pfx = '172.20.30.0/24'   # separate from nico-sim to allow coexistence

    lines = [
        f'name: {dc}',
        '',
        'mgmt:',
        f'  network: {dc}-mgmt',
        f'  ipv4-subnet: {mgmt_pfx}',
        '',
        'topology:',
        '  defaults:',
        f'    kind: linux',
        f'    image: {image}',
        f'    image-pull-policy: never',
        '',
        '  nodes:',
        '',
        '    super-spine:',
        f'      mgmt-ipv4: 172.20.30.10',
        '      binds:',
        f'        - nodes/super-spine/frr.conf:/etc/frr/frr.conf',
        f'        - nodes/super-spine/daemons:/etc/frr/daemons',
        '      exec:',
        '        - sysctl -w net.ipv4.ip_forward=1',
        '        - sysctl -w net.ipv4.conf.all.forwarding=1',
        '',
        '    spine-1:',
        f'      mgmt-ipv4: 172.20.30.11',
        '      binds:',
        f'        - nodes/spine-1/frr.conf:/etc/frr/frr.conf',
        f'        - nodes/spine-1/daemons:/etc/frr/daemons',
        '      exec:',
        '        - sysctl -w net.ipv4.ip_forward=1',
        '        - sysctl -w net.ipv4.conf.all.forwarding=1',
        '',
        '    leaf-cp:',
        f'      mgmt-ipv4: 172.20.30.12',
        '      binds:',
        f'        - nodes/leaf-cp/frr.conf:/etc/frr/frr.conf',
        f'        - nodes/leaf-cp/daemons:/etc/frr/daemons',
        '      exec:',
        '        - sysctl -w net.ipv4.ip_forward=1',
        '        - sysctl -w net.ipv4.conf.all.forwarding=1',
        '',
        '    leaf-mat:',
        f'      mgmt-ipv4: 172.20.30.13',
        '      binds:',
        f'        - nodes/leaf-mat/frr.conf:/etc/frr/frr.conf',
        f'        - nodes/leaf-mat/daemons:/etc/frr/daemons',
        '      exec:',
        '        - sysctl -w net.ipv4.ip_forward=1',
        '        - sysctl -w net.ipv4.conf.all.forwarding=1',
        '',
        '    dpu-1:',
        f'      mgmt-ipv4: 172.20.30.20',
        '      binds:',
        f'        - nodes/dpu-1/frr.conf:/etc/frr/frr.conf',
        f'        - nodes/dpu-1/daemons:/etc/frr/daemons',
        '      exec:',
        '        - sysctl -w net.ipv4.ip_forward=1',
        '        - sysctl -w net.ipv4.conf.all.forwarding=1',
        '        - sysctl -w net.ipv4.conf.eth1.forwarding=1',
        '        - sysctl -w net.ipv4.conf.eth2.forwarding=1',
        '',
        '    # Bridge nodes — connect fabric to host interfaces',
        '    # (names MUST match the br-<dc>-* bridges deploy-dev-fabric.py creates)',
        '',
        f'    br-{dc}-cp:',
        '      kind: bridge',
        '      # DPU eth1 + host CP interface attach here',
        '',
        f'    br-{dc}-internet:',
        '      kind: bridge',
        '      # leaf-mat eth2 + host internet uplink',
        '',
        '  links:',
        '    # super-spine ↔ spine-1',
        f'    - endpoints: ["super-spine:eth1", "spine-1:eth1"]',
        '    # spine-1 ↔ leaf-cp',
        f'    - endpoints: ["spine-1:eth2", "leaf-cp:eth1"]',
        '    # spine-1 ↔ leaf-mat',
        f'    - endpoints: ["spine-1:eth3", "leaf-mat:eth1"]',
        '    # leaf-cp ↔ dpu-1 (fabric uplink)',
        f'    - endpoints: ["leaf-cp:eth2", "dpu-1:eth1"]',
        '    # dpu-1 ↔ CP bridge (internal link to VM host CP interface)',
        f'    - endpoints: ["dpu-1:eth2", "br-{dc}-cp:eth-dpu"]',
        '    # leaf-mat ↔ internet bridge (MAT BMC side)',
        f'    - endpoints: ["leaf-mat:eth2", "br-{dc}-internet:eth-mat"]',
        '    # super-spine ↔ internet bridge (internet uplink to host NAT)',
        f'    - endpoints: ["super-spine:eth2", "br-{dc}-internet:eth-uplink"]',
    ]
    return '\n'.join(lines) + '\n'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    site_yaml, site_folder = resolve_site(sys.argv[1])
    cfg = load_yaml(site_yaml)

    pfx    = cfg['fabric']['prefixes']
    dc     = cfg['fabric']['dc_name']
    outdir = Path(site_folder) / 'fabric'
    outdir.mkdir(parents=True, exist_ok=True)

    print(f'nico-dev — Generate fabric topology')
    print(f'  site   : {site_yaml}')
    print(f'  dc_name: {dc}')
    print(f'  output : {outdir}')

    # ── Assign IPs ───────────────────────────────────────────────────────────
    # switch_underlay /31 pairs
    sw_net = ipaddress.IPv4Network(pfx['switch_underlay'], strict=False)
    sw_pairs = list(sw_net.subnets(new_prefix=31))

    # super-spine ↔ spine-1: pair 0
    ss_sp_ip,  sp_ss_ip  = hosts(str(sw_pairs[0]))[:2]
    # spine-1 ↔ leaf-cp:   pair 1
    sp_lcp_ip, lcp_sp_ip = hosts(str(sw_pairs[1]))[:2]
    # spine-1 ↔ leaf-mat:  pair 2
    sp_lmat_ip, lmat_sp_ip = hosts(str(sw_pairs[2]))[:2]

    # loopbacks
    lo_net = ipaddress.IPv4Network(pfx['switch_loopbacks'], strict=False)
    lo_hosts = list(lo_net.hosts())
    ss_lo   = str(lo_hosts[0])   # 7.129.0.1
    sp_lo   = str(lo_hosts[1])   # 7.129.0.2
    lcp_lo  = str(lo_hosts[2])   # 7.129.0.3
    lmat_lo = str(lo_hosts[3])   # 7.129.0.4

    # DPU fabric: pair 0 of dpu_fabric /31
    dpu_fab_net  = ipaddress.IPv4Network(pfx['dpu_fabric'], strict=False)
    dpu_fab_pair = list(dpu_fab_net.subnets(new_prefix=31))[0]
    lcp_dpu_ip, dpu_lcp_ip = hosts(str(dpu_fab_pair))[:2]

    # DPU loopback
    dpu_lo_net = ipaddress.IPv4Network(pfx['dpu_loopbacks'], strict=False)
    dpu_lo     = str(list(dpu_lo_net.hosts())[0])   # 7.131.0.1

    # Control plane link (VM host ↔ DPU)
    cp_link_net  = ipaddress.IPv4Network(pfx['control_plane_link'], strict=False)
    cp_link_h    = list(cp_link_net.hosts())
    dpu_cp_ip    = str(cp_link_h[0])   # 7.132.1.0  (DPU side)
    vm_cp_ip     = str(cp_link_h[1])   # 7.132.1.1  (VM host / CP side)

    # Internet uplink (host ↔ super-spine for NAT)
    inet_net   = ipaddress.IPv4Network(pfx['internet_uplink'], strict=False)
    inet_hosts = list(inet_net.hosts())
    host_inet_ip = str(inet_hosts[0])  # 7.132.0.1  (host)
    ss_inet_ip   = str(inet_hosts[1])  # 7.132.0.2  (super-spine)

    # MAT underlay gateway (on leaf-mat:eth2)
    mat_net = ipaddress.IPv4Network(pfx['mat_underlay'], strict=False)
    mat_gw  = str(list(mat_net.hosts())[0])

    ips = {
        'ss_lo': ss_lo,   'sp_lo': sp_lo,
        'lcp_lo': lcp_lo, 'lmat_lo': lmat_lo,
        'dpu_lo': dpu_lo,
        'ss_sp_ip': ss_sp_ip,   'sp_ss_ip': sp_ss_ip,
        'sp_lcp_ip': sp_lcp_ip, 'lcp_sp_ip': lcp_sp_ip,
        'sp_lmat_ip': sp_lmat_ip, 'lmat_sp_ip': lmat_sp_ip,
        'lcp_dpu_ip': lcp_dpu_ip, 'dpu_lcp_ip': dpu_lcp_ip,
        'dpu_cp_ip': dpu_cp_ip,   'vm_cp_ip': vm_cp_ip,
        'host_inet_ip': host_inet_ip, 'ss_inet_ip': ss_inet_ip,
        'mat_gw': mat_gw,
    }

    print()
    print(f'  IP assignments:')
    print(f'    super-spine loopback : {ss_lo}')
    print(f'    spine-1 loopback     : {sp_lo}')
    print(f'    leaf-cp loopback     : {lcp_lo}')
    print(f'    leaf-mat loopback    : {lmat_lo}')
    print(f'    dpu-1 loopback       : {dpu_lo}')
    print(f'    dpu-1 fabric IP      : {dpu_lcp_ip} (peer: leaf-cp {lcp_dpu_ip})')
    print(f'    dpu-1 CP link        : {dpu_cp_ip}/31')
    print(f'    VM host CP link      : {vm_cp_ip}/31  ← CP node IP')
    print(f'    host internet        : {host_inet_ip}/30')
    print(f'    mat gateway          : {mat_gw}')

    # ── Write topology ────────────────────────────────────────────────────────
    def write_file(path, content):
        """write_text, healing the docker bind-mount directory trap: if a
        container ever started while a bind-source file was missing, Docker
        created it as a DIRECTORY — and write_text fails with
        IsADirectoryError forever after. Remove such directories first."""
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
            print(f'  [fixed] {path.name} was a directory (docker bind-mount leftover) — removed')
        path.write_text(content)

    write_file(outdir / 'topo.clab.yml', gen_topo(cfg, ips, dc))
    print(f'\n  [ok] topo.clab.yml')

    # ── Write per-node FRR configs ────────────────────────────────────────────
    nodes = {
        'super-spine': frr_super_spine(cfg, ss_lo, ss_sp_ip, sp_lo, sp_ss_ip,
                                       ips['host_inet_ip']),
        'spine-1':     frr_spine(cfg, sp_lo, sp_ss_ip, ss_lo, ss_sp_ip,
                                 sp_lcp_ip, lcp_sp_ip, sp_lmat_ip, lmat_sp_ip),
        'leaf-cp':     frr_leaf_cp(cfg, lcp_lo, lcp_sp_ip, sp_lcp_ip,
                                   lcp_dpu_ip, dpu_lcp_ip),
        'leaf-mat':    frr_leaf_mat(cfg, lmat_lo, lmat_sp_ip, sp_lmat_ip, mat_gw),
        'dpu-1':       frr_dpu(cfg, dpu_lo, dpu_lcp_ip, lcp_dpu_ip,
                               dpu_cp_ip, vm_cp_ip),
    }
    for node, frr_conf in nodes.items():
        node_dir = outdir / 'nodes' / node
        node_dir.mkdir(parents=True, exist_ok=True)
        write_file(node_dir / 'frr.conf', frr_conf)
        write_file(node_dir / 'daemons', frr_daemons())
        print(f'  [ok] nodes/{node}/')

    # ── Write IP reference for deploy script ──────────────────────────────────
    import json
    write_file(outdir / 'fabric-ips.json', json.dumps(ips, indent=2))
    print(f'  [ok] fabric-ips.json')

    print()
    print(f'Deploy:')
    print(f'  sudo python3 deploy-dev-fabric.py {sys.argv[1]}')


if __name__ == '__main__':
    main()
