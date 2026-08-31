#!/usr/bin/env python3
"""
nico-sim — Create a new simulation site

All parameters are named (no positional args) to avoid ordering errors.

Usage:
  ./create-new-site.py --dc-name <dc> --site-name <site> --folder <dir> \\
                       --underlay <octet> --overlay <octet> \\
                       [--dc-nick <4chr>] [--site-nick <4chr>]

Required:
  --dc-name     Datacenter name (any length, human-readable). e.g. "datacenter-west"
  --site-name   Nico site name within the DC (any length). e.g. "ytl-production"
  --folder      Output folder for generated site yaml. e.g. ~/sites/ytl
  --underlay    First octet for fabric/infrastructure IPs (7 or higher, unique per site)
  --overlay     First octet for overlay/tenant IPs (must differ from --underlay)

Optional:
  --dc-nick     ≤4 char nick for dc_name used in bridge names (default: dc_name[:4])
  --site-nick   ≤4 char nick for site_name used in VM/OOB names (default: site_name[:4])
  --template-dir  Directory with nico-sim.yaml templates (default: script directory)
  --dry-run     Show what would be created without writing files

Nick names (≤4 chars each) are used where Linux interface name limits apply:
  ContainerLab bridges: br-{dc_nick}-internet (≤15 chars when dc_nick ≤4)
  libvirt OOB networks: {site_nick}-cp, {site_nick}-mh, {site_nick}-reg
  VM names:             {dc_nick}-{site_nick}-cp-1

Examples:
  ./create-new-site.py --dc-name dc1 --site-name ytl --folder ~/sites/ytl \\
                       --underlay 9 --overlay 10

  ./create-new-site.py --dc-name datacenter-west --site-name ytl-production \\
                       --folder ~/sites/ytl --underlay 9 --overlay 10 \\
                       --dc-nick dcw1 --site-nick ytl

Address scheme derived from --underlay (u) and --overlay (v):
  switch_underlay:      {u}.128.0.0/16
  dpu_fabric:           {u}.130.0.0/16
  internet_uplink:      {u}.132.0.0/30
  service_vips:         {u}.133.1.0/27
  underlay_pool:        {u}.140.0.0/14
  overlay:              {v}.150.0.0/16

Collision detection: checks virsh networks, VM names, Docker networks,
and host routes before writing any files.
"""

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Create a new nico-sim site configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('--dc-name', required=True,
                   help='Datacenter name (any length, human-readable). e.g. "datacenter-west"')
    p.add_argument('--site-name', required=True,
                   help='Nico site name within the DC. e.g. "ytl-production"')
    p.add_argument('--folder', required=True,
                   help='Output folder for generated site yaml (will be created)')
    p.add_argument('--underlay', type=int, required=True,
                   help='First octet for fabric/infrastructure IPs (7 or higher, unique per site)')
    p.add_argument('--overlay', type=int, required=True,
                   help='First octet for overlay/tenant IPs (must differ from --underlay)')
    p.add_argument('--dc-nick', default=None,
                   help='≤4 char nick for dc_name used in bridge names (default: dc_name[:4])')
    p.add_argument('--site-nick', default=None,
                   help='≤4 char nick for site_name used in VM/OOB names (default: site_name[:4])')
    p.add_argument('--template-dir', default=None,
                   help='Directory with nico-sim.yaml templates (default: script directory)')
    p.add_argument('--config-size', required=True, choices=['large', 'small'],
                   help='large = full sim (nico-sim.yaml); small = Mac/low-resource (nico-sim-mac.yaml)')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be created without writing files')
    return p.parse_args()


def validate_nick(name, value, label):
    """Validate and return a ≤4-char nick, erroring early if invalid."""
    # Strip trailing hyphens from auto-truncated values (e.g. "ytl-" → "ytl")
    value = value.rstrip('-')
    if len(value) == 0:
        print(f'Error: {label} is empty after stripping trailing hyphens.')
        print(f'  Use --{label.replace("_", "-")} to specify a short nick explicitly.')
        sys.exit(1)
    if len(value) > 4:
        print(f'Error: {label} "{value}" exceeds 4 characters ({len(value)} chars).')
        print(f'  Linux bridge names must be ≤15 chars; nicks ≤4 chars ensures this.')
        print(f'  Use --{label.replace("_", "-")} to specify a short nick.')
        sys.exit(1)
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', value):
        print(f'Error: {label} "{value}" must be lowercase alphanumeric (hyphens allowed mid-name).')
        sys.exit(1)
    return value


# ── Prefix derivation ─────────────────────────────────────────────────────────

def derive_prefixes(underlay_octet, overlay_octet, site_name, dc_nick, site_nick):
    """Derive all site-specific prefixes from underlay and overlay octets."""
    o  = underlay_octet
    ov = overlay_octet
    oob_base = 200 + ((o - 7) // 2) * 20  # OOB base: block of 20 per odd-octet pair
                                            # o=7→200, o=9→220, o=11→240
    mgmt_third = 20 + ((o - 7) // 2)   # ContainerLab mgmt: o=7→20, o=9→21, o=11→22

    return {
        'net_octet':    o,
        'overlay_octet': ov,

        # ── Fabric /16s ──────────────────────────────────────────────────────
        'switch_underlay':       f'{o}.128.0.0/16',
        'switch_loopbacks':      f'{o}.129.0.0/16',
        'dpu_fabric':            f'{o}.130.0.0/16',
        'dpu_loopbacks':         f'{o}.131.0.0/16',

        # ── Infrastructure /30s and /24 ───────────────────────────────────────
        'internet_uplink':       f'{o}.132.0.0/30',
        'registry_link':         f'{o}.132.0.4/30',
        'control_plane_prefix':  f'{o}.132.1.0/24',

        # ── Service VIPs ─────────────────────────────────────────────────────
        'service_vips':          f'{o}.133.1.0/27',
        'service_vips_reserved': f'{o}.133.1.32/27',

        # ── Per-rack underlay pool ────────────────────────────────────────────
        'underlay_pool':         f'{o}.140.0.0/14',
        'rack_leaf_4_prefix':    f'{o}.140.0.0/24',
        'rack_leaf_5_prefix':    f'{o}.140.1.0/24',
        'rack_mat_hosts_prefix': f'{o}.140.2.0/24',
        'service_prefix':        f'{o}.138.0.0/16',

        # ── Overlay ──────────────────────────────────────────────────────────
        'overlay':               f'{ov}.150.0.0/16',
        'admin_prefix':          f'{ov}.135.0.0/16',

        # ── ContainerLab management ───────────────────────────────────────────
        'mgmt':                  f'172.20.{mgmt_third}.0/24',

        # ── OOB subnets ───────────────────────────────────────────────────────
        'oob_cp_prefix':         f'192.168.{oob_base}.0/24',
        'oob_registry_prefix':   f'192.168.{oob_base + 2}.0/24',
        'oob_mh_prefix':         f'192.168.{oob_base + 3}.0/24',

        # ── OOB network names — use site_nick (≤4 chars) so libvirt bridge
        # virbr-{nick}-{role} stays within the 15-char Linux interface limit.
        'oob_cp_name':           f'{site_nick}-cp',
        'oob_registry_name':     f'{site_nick}-reg',
        'oob_mh_name':           f'{site_nick}-mh',

        # ── Nick names for bridge/VM name generation ──────────────────────────
        'dc_nick':               dc_nick,
        'site_nick':             site_nick,
    }


# ── Collision detection ───────────────────────────────────────────────────────

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip()


def get_virsh_networks():
    networks = []
    try:
        out = run(['virsh', 'net-list', '--all', '--name'])
        for name in out.splitlines():
            name = name.strip()
            if not name:
                continue
            xml = run(['virsh', 'net-dumpxml', name])
            for m in re.finditer(r"<ip\s+address='([^']+)'\s+(?:netmask|prefix)='([^']+)'", xml):
                ip, mask = m.group(1), m.group(2)
                try:
                    net = ipaddress.IPv4Network(f'{ip}/{mask}', strict=False)
                    networks.append((name, net))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return networks


def get_virsh_vms():
    try:
        out = run(['virsh', 'list', '--all', '--name'])
        return {n.strip() for n in out.splitlines() if n.strip()}
    except FileNotFoundError:
        return set()


def get_docker_networks():
    networks = []
    try:
        out = run(['docker', 'network', 'ls', '--format', '{{.Name}}'])
        for name in out.splitlines():
            name = name.strip()
            if not name:
                continue
            info = run(['docker', 'network', 'inspect', name, '--format',
                        '{{range .IPAM.Config}}{{.Subnet}} {{end}}'])
            for subnet_str in info.split():
                try:
                    net = ipaddress.IPv4Network(subnet_str, strict=False)
                    networks.append((name, net))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return networks


def get_host_routes():
    routes = []
    try:
        out = run(['ip', 'route', 'show'])
        for line in out.splitlines():
            m = re.match(r'^(\d+\.\d+\.\d+\.\d+/\d+)', line)
            if m:
                try:
                    routes.append(ipaddress.IPv4Network(m.group(1), strict=False))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return routes


def check_collisions(pfx, site_name):
    issues = []
    o = pfx['net_octet']

    virsh_nets  = get_virsh_networks()
    virsh_vms   = get_virsh_vms()
    docker_nets = get_docker_networks()
    host_routes = get_host_routes()

    proposed_oob = [
        (pfx['oob_cp_name'],       ipaddress.IPv4Network(pfx['oob_cp_prefix'])),
        (pfx['oob_registry_name'], ipaddress.IPv4Network(pfx['oob_registry_prefix'])),
        (pfx['oob_mh_name'],       ipaddress.IPv4Network(pfx['oob_mh_prefix'])),
    ]
    proposed_mgmt     = ipaddress.IPv4Network(pfx['mgmt'])
    fabric_supernet   = ipaddress.IPv4Network(f'{o}.0.0.0/8')
    existing_net_names = {name for name, _ in virsh_nets}

    for prop_name, prop_net in proposed_oob:
        for existing_name, existing_net in virsh_nets:
            if prop_net.overlaps(existing_net):
                issues.append(('error',
                    f'OOB subnet {prop_net} ({prop_name}) overlaps with '
                    f'existing virsh network "{existing_name}" ({existing_net})'))

    for docker_name, docker_net in docker_nets:
        if proposed_mgmt.overlaps(docker_net):
            issues.append(('error',
                f'ContainerLab management {proposed_mgmt} overlaps with '
                f'existing Docker network "{docker_name}" ({docker_net})'))

    for vm_name in virsh_vms:
        # VM names follow {site_name}-{site_name}-{role}-N or {site_name}-{role}-N
        if vm_name.startswith(f'{site_name}-'):
            issues.append(('error',
                f'VM "{vm_name}" already exists in virsh '
                f'(would conflict with site "{site_name}")'))

    for route in host_routes:
        if route.subnet_of(fabric_supernet) or fabric_supernet.subnet_of(route):
            issues.append(('warn',
                f'Host route {route} overlaps with proposed fabric space '
                f'{fabric_supernet}. net_octet={o} may already be in use.'))

    for prop_name, _ in proposed_oob:
        if prop_name in existing_net_names:
            issues.append(('error',
                f'Virsh network "{prop_name}" already exists. '
                f'Did you already create site "{site_name}"?'))

    return issues


# ── Config rewriting ──────────────────────────────────────────────────────────

def rewrite_config(template_path, pfx, site_name, dc_name, dc_nick, site_nick):
    """Load template nico-sim.yaml and return rewritten content for the new site."""
    with open(template_path) as f:
        content = f.read()

    o        = pfx['net_octet']
    ov       = pfx['overlay_octet']
    oob_base = 200 + ((o - 7) // 2) * 20  # mirrors derive_prefixes logic

    # Order matters — longer/more specific patterns first to avoid partial matches.
    replacements = [
        # ── Fabric /16s ──────────────────────────────────────────────────────
        ('7.128.0.0/16',      f'{o}.128.0.0/16'),
        ('7.129.0.0/16',      f'{o}.129.0.0/16'),
        ('7.130.0.0/16',      f'{o}.130.0.0/16'),
        ('7.131.0.0/16',      f'{o}.131.0.0/16'),

        # ── Infrastructure links + CP prefix ─────────────────────────────────
        ('7.132.0.4/30',      f'{o}.132.0.4/30'),
        ('7.132.0.0/30',      f'{o}.132.0.0/30'),
        ('7.132.1.0/24',      f'{o}.132.1.0/24'),

        # ── Service VIPs ─────────────────────────────────────────────────────
        ('7.133.1.32/27',     f'{o}.133.1.32/27'),
        ('7.133.1.0/27',      f'{o}.133.1.0/27'),

        # ── Per-rack underlay pool ────────────────────────────────────────────
        ('7.140.0.0/14',      f'{o}.140.0.0/14'),
        ('7.140.0.0/24',      f'{o}.140.0.0/24'),
        ('7.140.1.0/24',      f'{o}.140.1.0/24'),
        ('7.140.2.0/24',      f'{o}.140.2.0/24'),
        # Gateway IPs must be replaced too (relay containers use these as giaddr)
        ('"7.140.0.1"',       f'"{o}.140.0.1"'),
        ('"7.140.1.1"',       f'"{o}.140.1.1"'),
        ('"7.140.2.1"',       f'"{o}.140.2.1"'),

        # ── Service / fabric services prefix ─────────────────────────────────
        ('7.138.0.0/16',      f'{o}.138.0.0/16'),
        ('7.138.0.0/24',      f'{o}.138.0.0/24'),
        ('7.138.1.0/24',      f'{o}.138.1.0/24'),
        ('7.138.2.0/24',      f'{o}.138.2.0/24'),
        ('7.138.3.0/24',      f'{o}.138.3.0/24'),

        # ── Individual infrastructure IPs (in comments/examples) ─────────────
        ('7.132.0.6',         f'{o}.132.0.6'),
        ('7.132.0.5',         f'{o}.132.0.5'),
        ('7.132.0.2',         f'{o}.132.0.2'),
        ('7.132.0.1',         f'{o}.132.0.1'),
        ('7.132.1.1',         f'{o}.132.1.1'),
        ('7.132.1.3',         f'{o}.132.1.3'),
        ('7.132.1.5',         f'{o}.132.1.5'),
        ('7.130.0.1',         f'{o}.130.0.1'),
        ('7.130.0.3',         f'{o}.130.0.3'),
        ('7.130.0.5',         f'{o}.130.0.5'),
        ('7.131.0.1',         f'{o}.131.0.1'),
        ('7.131.0.2',         f'{o}.131.0.2'),
        ('7.131.0.3',         f'{o}.131.0.3'),

        # ── VIP assignments (quoted strings in yaml) ─────────────────────────
        ('"7.133.1.',         f'"{o}.133.1.'),

        # ── Overlay ──────────────────────────────────────────────────────────
        ('8.150.0.0/16',      f'{ov}.150.0.0/16'),
        ('8.135.0.0/16',      f'{ov}.135.0.0/16'),
        ('"8.135.0.1"',       f'"{ov}.135.0.1"'),

        # ── ContainerLab management ───────────────────────────────────────────
        ('172.20.20.0/24',    pfx['mgmt']),

        # ── OOB subnets ───────────────────────────────────────────────────────
        ('192.168.200.0/24',  pfx['oob_cp_prefix']),
        ('192.168.202.0/24',  pfx['oob_registry_prefix']),
        ('192.168.210.0/24',  pfx['oob_mh_prefix']),

        # ── Static OOB IPs (control_plane.vms) ────────────────────────────────
        ('192.168.200.11',    f'192.168.{oob_base}.11'),
        ('192.168.200.12',    f'192.168.{oob_base}.12'),
        ('192.168.200.13',    f'192.168.{oob_base}.13'),
        ('192.168.200.21',    f'192.168.{oob_base}.21'),
        ('192.168.200.22',    f'192.168.{oob_base}.22'),
        ('192.168.200.23',    f'192.168.{oob_base}.23'),

        # ── Static OOB IPs (managed_hosts.vms) ────────────────────────────────
        ('192.168.210.11',    f'192.168.{oob_base + 3}.11'),
        ('192.168.210.12',    f'192.168.{oob_base + 3}.12'),
        ('192.168.210.13',    f'192.168.{oob_base + 3}.13'),
        ('192.168.210.14',    f'192.168.{oob_base + 3}.14'),

        # ── OOB network names (use site_nick — virbr-{nick}-{role} ≤15 chars) ──
        ('name: dc1-cp',        f'name: {pfx["oob_cp_name"]}'),
        ('name: dc1-reg',       f'name: {pfx["oob_registry_name"]}'),
        ('name: dc1-mh',        f'name: {pfx["oob_mh_name"]}'),

        # ── Site / DC identity ────────────────────────────────────────────────
        ('kubeconfig: dc1-site1.kubeconfig.yaml', f'kubeconfig: {site_nick}.kubeconfig.yaml'),
        ('dc_name: dc1',                        f'dc_name: {dc_name}'),
        ('dc_nick_name: dc1',                   f'dc_nick_name: {dc_nick}'),
        ('sitename: site1',                     f'sitename: {site_name}'),
        ('site_nick_name: sit1',                f'site_nick_name: {site_nick}'),
        ('domain: dc1-site1.example.com',       f'domain: {site_nick}.example.com'),
        ('kubectl config: ~/.kube/config-dc1-site1',
                                                f'kubectl config: ~/.kube/config-{site_nick}'),

        # ── ContainerLab bridge names use dc_nick ────────────────────────────
        ('br-dc1-internet',                     f'br-{dc_nick}-internet'),
    ]

    for old, new in replacements:
        content = content.replace(old, new)

    return content


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # ── Validate inputs upfront ───────────────────────────────────────────────
    if args.underlay < 7:
        print(f'Error: --underlay must be >= 7. Got {args.underlay}')
        sys.exit(1)
    if args.overlay == args.underlay:
        print(f'Error: --overlay must differ from --underlay ({args.underlay})')
        sys.exit(1)

    dc_name   = args.dc_name
    site_name = args.site_name

    # Derive nicks (default: first 4 chars), then validate
    dc_nick   = validate_nick(dc_name,   args.dc_nick   or dc_name[:4],   'dc_nick')
    site_nick = validate_nick(site_name, args.site_nick or site_name[:4], 'site_nick')

    # Warn if nick was auto-truncated
    if args.dc_nick is None and dc_nick != dc_name:
        print(f'  Note: dc_nick auto-set to "{dc_nick}" (first 4 chars of dc_name "{dc_name}")')
        print(f'  Use --dc-nick to specify a different nick.')
    if args.site_nick is None and site_nick != site_name:
        print(f'  Note: site_nick auto-set to "{site_nick}" (first 4 chars of site_name "{site_name}")')
        print(f'  Use --site-nick to specify a different nick.')

    pfx        = derive_prefixes(args.underlay, args.overlay, site_name, dc_nick, site_nick)
    folder     = Path(args.folder).expanduser()
    script_dir = Path(args.template_dir).expanduser() if args.template_dir \
                 else Path(__file__).parent

    template_file = 'nico-sim.yaml' if args.config_size == 'large' else 'nico-sim-mac.yaml'
    template_path = (script_dir / template_file).resolve()
    if not template_path.exists():
        print(f'Error: template not found: {template_path}', file=sys.stderr)
        sys.exit(1)

    print('nico-sim — Create New Site')
    print(f'  dc_name    : {dc_name}  (nick: {dc_nick})')
    print(f'  site_name  : {site_name}  (nick: {site_nick})')
    print(f'  folder     : {folder}')
    print(f'  config-size: {args.config_size}  (template: {template_file})')
    print(f'  underlay   : {args.underlay}  → {args.underlay}.x.x.x (fabric/infrastructure)')
    print(f'  overlay    : {args.overlay}  → {args.overlay}.x.x.x (tenant VPCs)')
    print()
    print('Derived prefixes:')
    print(f'  switch_underlay       : {pfx["switch_underlay"]}')
    print(f'  switch_loopbacks      : {pfx["switch_loopbacks"]}')
    print(f'  dpu_fabric            : {pfx["dpu_fabric"]}')
    print(f'  dpu_loopbacks         : {pfx["dpu_loopbacks"]}')
    print(f'  internet_uplink       : {pfx["internet_uplink"]}')
    print(f'  registry_link         : {pfx["registry_link"]}')
    print(f'  control_plane_prefix  : {pfx["control_plane_prefix"]}')
    print(f'  service_vips          : {pfx["service_vips"]}')
    print(f'  underlay_pool         : {pfx["underlay_pool"]}')
    print(f'  overlay               : {pfx["overlay"]}')
    print(f'  oob-cp                : {pfx["oob_cp_prefix"]}  (virsh: {pfx["oob_cp_name"]})')
    print(f'  oob-registry          : {pfx["oob_registry_prefix"]}  (virsh: {pfx["oob_registry_name"]})')
    print(f'  oob-mh                : {pfx["oob_mh_prefix"]}  (virsh: {pfx["oob_mh_name"]})')
    print(f'  clab management       : {pfx["mgmt"]}')
    print()

    print('Checking for conflicts...')
    issues = check_collisions(pfx, site_nick)
    errors = [i for i in issues if i[0] == 'error']
    warns  = [i for i in issues if i[0] == 'warn']

    for _, msg in warns:
        print(f'  ⚠  {msg}')
    for _, msg in errors:
        print(f'  ✗  {msg}')

    if errors:
        print(f'\n  {len(errors)} conflict(s) — aborting.')
        print(f'  Choose a different net_octet or clean up existing resources.')
        sys.exit(1)

    if not issues:
        print('  No conflicts found ✓')
    print()

    if args.dry_run:
        print('Dry run — no files written.')
        return

    folder.mkdir(parents=True, exist_ok=True)

    dest = folder / f'{site_nick}.yaml'
    dest.write_text(rewrite_config(script_dir / template_file, pfx, site_name, dc_name, dc_nick, site_nick))
    print(f'Created: {dest}')

    print()
    print('Next steps:')
    print(f'  cd {folder}')
    print(f'  # Review {site_nick}.yaml — set infra_controller_repo path')
    print(f'  ~/claude-notes/nico-sim/generate-fabric.py {site_nick}.yaml')
    print(f'  cd fabric && sudo ./deploy.sh && cd ..')
    print(f'  ~/claude-notes/nico-sim/generate-nodes.py {site_nick}.yaml \\')
    print(f'    --topo ./fabric/topo.clab.yml')
    print(f'  cd vm && sudo ./deploy-nodes.sh')


if __name__ == '__main__':
    main()
