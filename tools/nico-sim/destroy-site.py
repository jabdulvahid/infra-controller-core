#!/usr/bin/env python3
"""
nico-sim — Destroy a site and all its resources.

Removes everything added to the sim-host by deploying a nico-sim site:
  - Nico k8s stack (helm releases + namespaces)
  - KVM VMs (CP, DPU, MH, registry)
  - Libvirt OOB networks
  - ContainerLab fabric (containers, bridges, veths, SSH config)
  - dnsmasq for this site
  - iptables rules (masquerade, DNS)
  - IP routes to fabric ranges
  - Linux bridges (fabric + host-side)
  - /etc/hosts entries added by configure-clis.py
  - Site certs and run scripts

After this script completes the sim-host is clean and you can redeploy
from scratch following test-run.md: deploy-fabric.py → deploy-nodes.py →
form-k8s-cluster.py → setup-insecure-registry.py → build-nico-components.py →
deploy-nico-system.py.

To remove ONLY the VMs (keep the fabric): sudo {site}/vm/destroy-nodes.sh

Usage:
  sudo python3 destroy-site.py <site>            # destroy everything
  sudo python3 destroy-site.py <site> --dry-run  # show what would be destroyed
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


# ── helpers ───────────────────────────────────────────────────────────────────

def run(cmd, dry=False, check=False, capture=False):
    if dry:
        print(f'  [dry] {" ".join(str(c) for c in cmd)}')
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode != 0:
        stderr = (r.stderr or '').strip()
        if stderr:
            print(f'  ! {" ".join(str(c) for c in cmd)}: {stderr}')
        if check:
            raise RuntimeError(f'Command failed (exit {r.returncode}): {" ".join(str(c) for c in cmd)}')
    return r


def exists(cmd):
    """Return True if command exits 0."""
    return subprocess.run(cmd, capture_output=True).returncode == 0


def step(msg):
    print(f'\n── {msg} {"─" * (50 - len(msg))}')


def done(msg):
    print(f'  ✓ {msg}')


def skip(msg):
    print(f'  - {msg} (not found)')


# ── site loading ──────────────────────────────────────────────────────────────

def resolve_site(arg):
    p = Path(arg).expanduser().resolve()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(p.parent)


def load_site(site_yaml):
    with open(site_yaml) as f:
        return yaml.safe_load(f)


def derive(sim, site_folder=None):
    """Derive all resource names from the site yaml."""
    fab      = sim.get('fabric', {})
    pfx      = fab.get('prefixes', {})
    cp       = sim.get('control_plane', {})
    mh       = sim.get('managed_hosts', {})
    nico_sys = sim.get('nico-system', {})
    hv       = nico_sys.get('helm-values', {})

    dc_name  = fab.get('dc_name', 'nico-sim')
    sitename = hv.get('sitename', '')
    name_pfx = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    site_octet = int(pfx.get('switch_underlay', '7.0.0.0/8').split('.')[0])

    n_cp = cp.get('num_vms', 0)
    vp   = cp.get('vm_prefix', 'cp')
    dp   = cp.get('dpu_prefix', 'cp-dpu')

    n_cp_leafs = fab.get('num_control_plane_leafs', n_cp)

    # CP + DPU VMs
    cp_vms  = [f'{name_pfx}{vp}-{i}'    for i in range(1, n_cp + 1)]
    dpu_vms = [f'{name_pfx}{dp}-{i}'    for i in range(1, n_cp + 1)]

    # MH VMs
    mh_vm_prefix = mh.get('vm_prefix', 'mh')
    num_vms_cfg  = mh.get('num_vms', {})
    total_mh     = sum(num_vms_cfg.values()) if isinstance(num_vms_cfg, dict) else int(num_vms_cfg or 0)
    mh_vms = [f'{name_pfx}{mh_vm_prefix}-{i}' for i in range(1, total_mh + 1)]

    # Registry VM
    util = sim.get('nico_container_registry', {})
    reg_prefix = util.get('vm_prefix', 'registry')
    registry_vm = f'{name_pfx}{reg_prefix}'

    # OOB libvirt networks
    oob_cp  = cp.get('oob_network', {}).get('name', f'{dc_name}-cp')
    oob_reg = util.get('oob_network', {}).get('name', f'{dc_name}-reg')
    oob_mh  = mh.get('oob_network', {}).get('name', f'{dc_name}-mh')
    oob_nets = list(dict.fromkeys([oob_cp, oob_reg, oob_mh]))

    # Host-side bridges (DPU↔CP internal)
    host_bridges = [f'br-cp{i}-host' for i in range(1, n_cp + 1)]

    # Fabric bridges (ContainerLab bridge nodes)
    fabric_bridges  = [f'br-{dc_name}-cp{i}' for i in range(1, n_cp_leafs + 1)]
    mh_count = sum(1 for v in fab.get('underlay_leafs', {}).values()
                   if isinstance(v, dict) and v.get('relay', False))
    fabric_bridges += [f'br-{dc_name}-mh{r}' for r in range(1, mh_count + 1)]
    fabric_bridges += [f'br-{dc_name}-internet', f'br-{dc_name}-registry']

    # Internet uplink
    inet = sim.get('fabric', {}).get('internet_uplink', {})
    inet_bridge = f'br-{dc_name}-internet'
    inet_prefix = inet.get('prefix', '')
    inet_host_ip = str(list(ipaddress.IPv4Network(inet_prefix, strict=False).hosts())[0]) if inet_prefix else ''
    ss_ip = str(list(ipaddress.IPv4Network(inet_prefix, strict=False).hosts())[1]) if inet_prefix else ''

    # Fabric prefixes to remove routes for
    fabric_routes = []
    for key in ['switch_underlay', 'dpu_fabric', 'dpu_loopbacks', 'loopbacks']:
        v = pfx.get(key)
        if v:
            fabric_routes.append(v)
    cp_pfx = cp.get('control_plane_prefix', '')
    if cp_pfx:
        fabric_routes.append(cp_pfx)
    reg_link = fab.get('registry_link', {})
    if reg_link.get('enabled') and reg_link.get('prefix'):
        fabric_routes.append(reg_link['prefix'])
    underlay_pool = fab.get('underlay_pool', '')
    if underlay_pool:
        fabric_routes.append(underlay_pool)
    np = hv.get('net-plan', {})
    svc_vips = np.get('service_vips', '')
    if svc_vips:
        fabric_routes.append(svc_vips)

    # /etc/hosts entry
    kube_id  = f'{dc_name}-{sitename}' if sitename else dc_name
    api_host = f'nico-api.{dc_name}-{sitename}' if sitename else f'nico-api.{dc_name}'

    # Kubeconfig — check site folder first (works for root), then ~/.kube
    kube_filename = sim.get('kubeconfig', f'{kube_id}.kubeconfig.yaml')
    kube_in_site  = Path(site_folder) / kube_filename if site_folder else None
    kube_in_home  = Path.home() / '.kube' / f'config-{kube_id}'
    kubeconfig    = kube_in_site if (kube_in_site and kube_in_site.exists()) else kube_in_home

    # k8s namespaces (helm uninstall + kubectl delete)
    k8s_namespaces = [
        'nico-system', 'vault', 'external-secrets',
        'postgres', 'cert-manager', 'local-path-storage', 'metallb-system',
    ]

    return {
        'dc_name':         dc_name,
        'sitename':        sitename,
        'kube_id':         kube_id,
        'api_host':        api_host,
        'cp_vms':          cp_vms,
        'dpu_vms':         dpu_vms,
        'mh_vms':          mh_vms,
        'registry_vm':     registry_vm,
        'oob_nets':        oob_nets,
        'host_bridges':    host_bridges,
        'fabric_bridges':  fabric_bridges,
        'inet_bridge':     inet_bridge,
        'inet_host_ip':    inet_host_ip,
        'ss_ip':           ss_ip,
        'fabric_routes':   fabric_routes,
        'k8s_namespaces':  k8s_namespaces,
        'kubeconfig':      str(kubeconfig),
        'pid_file':        f'/var/run/{dc_name}-dns.pid',
        'log_file':        f'/var/log/{dc_name}-dns.log',
        'ssh_conf':        f'/etc/ssh/ssh_config.d/clab-{dc_name}.conf',
    }


# ── destroy phases ────────────────────────────────────────────────────────────

def destroy_nico(d, site_folder, dry):
    step('Nico k8s stack')
    kubeconfig = d['kubeconfig']
    if not Path(kubeconfig).exists():
        skip(f'kubeconfig {kubeconfig} not found — skipping k8s cleanup')
        return

    env = {**os.environ, 'KUBECONFIG': kubeconfig}

    helm_releases = [
        ('nico',                   'nico-system'),
        ('nico-prereqs',           'nico-system'),
        ('postgres-operator',      'postgres'),
        ('external-secrets',       'external-secrets'),
        ('vault',                  'vault'),
        ('cert-manager',           'cert-manager'),
        ('metallb',                'metallb-system'),
        ('local-path-provisioner', 'local-path-storage'),
    ]
    for release, ns in helm_releases:
        r = subprocess.run(['helm', 'status', release, '-n', ns],
                           capture_output=True, env=env)
        if r.returncode == 0:
            run(['helm', 'uninstall', release, '-n', ns, '--wait', '--timeout', '2m'],
                dry=dry)
            done(f'helm uninstall {release} -n {ns}')
        else:
            skip(f'helm release {release}')

    # Delete namespaces (removes all remaining resources including CRDs)
    for ns in d['k8s_namespaces']:
        if exists(['kubectl', 'get', 'namespace', ns, '--kubeconfig', kubeconfig]):
            run(['kubectl', 'delete', 'namespace', ns, '--ignore-not-found=true',
                 '--kubeconfig', kubeconfig, '--timeout=60s'], dry=dry)
            done(f'namespace {ns}')
        else:
            skip(f'namespace {ns}')

    if not dry:
        kf = d['kubeconfig']
        if Path(kf).exists():
            Path(kf).unlink()
            done(f'removed kubeconfig {kf}')


def destroy_vms(d, img_dir, dry):
    step('KVM VMs')
    all_vms = d['cp_vms'] + d['dpu_vms'] + d['mh_vms'] + [d['registry_vm']]
    for vm in all_vms:
        found = exists(['virsh', '-c', 'qemu:///system', 'dominfo', vm])
        if found:
            # destroy first (ignore error if already shut off)
            run(['virsh', '-c', 'qemu:///system', 'destroy', vm],
                dry=dry, capture=True)
            run(['virsh', '-c', 'qemu:///system', 'undefine', vm,
                 '--remove-all-storage', '--nvram'], dry=dry, capture=True)
            done(f'VM {vm}')
        else:
            # Still attempt undefine — VM may be defined but in a broken state
            # (failed creation, no domain info) while still holding a MAC address
            r = run(['virsh', '-c', 'qemu:///system', 'undefine', vm,
                     '--remove-all-storage', '--nvram'], dry=dry, capture=True)
            if not dry and r.returncode == 0:
                done(f'VM {vm} (was undefined, not running)')
            else:
                skip(f'VM {vm}')
        iso = Path(img_dir) / f'{vm}-seed.iso'
        if iso.exists() and not dry:
            iso.unlink()
            done(f'removed {iso}')


def destroy_oob_networks(d, dry):
    step('Libvirt OOB networks')
    for net in d['oob_nets']:
        if exists(['virsh', '-c', 'qemu:///system', 'net-info', net]):
            run(['virsh', '-c', 'qemu:///system', 'net-destroy',  net], dry=dry)
            run(['virsh', '-c', 'qemu:///system', 'net-undefine', net], dry=dry)
            done(f'network {net}')
        else:
            skip(f'network {net}')


def destroy_host_bridges(d, dry):
    step('Host-side bridges (DPU↔CP)')
    for br in d['host_bridges']:
        if exists(['ip', 'link', 'show', br]):
            run(['ip', 'link', 'set', br, 'down'], dry=dry)
            run(['ip', 'link', 'delete', br, 'type', 'bridge'], dry=dry)
            done(f'bridge {br}')
        else:
            skip(f'bridge {br}')


def _find_clab():
    """Find clab binary, checking common locations."""
    import shutil
    clab = shutil.which('clab')
    if clab:
        return clab
    for p in ['/usr/local/bin/clab', '/usr/bin/clab',
              '/home/jabdulvahid/.local/bin/clab',
              str(Path.home() / '.local/bin/clab')]:
        if Path(p).exists():
            return p
    return 'clab'  # let it fail with a clear error


def destroy_fabric(d, fabric_dir, dry):
    step('ContainerLab fabric')
    topo    = Path(fabric_dir) / 'topo.clab.yml'
    clab    = _find_clab()

    # Derive lab name from topo file (may differ from dc_name)
    lab_name = d['dc_name']
    if topo.exists():
        try:
            with open(topo) as f:
                topo_data = yaml.safe_load(f)
            lab_name = topo_data.get('name', lab_name)
        except Exception:
            pass

    # Also infer lab name from running containers: clab-{lab}-*
    if not dry:
        r = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                           capture_output=True, text=True)
        dc = d['dc_name']
        sn = d['sitename']
        for cname in r.stdout.splitlines():
            if cname.startswith(f'clab-{sn}-') or cname.startswith(f'clab-{dc}-'):
                # extract lab segment: clab-{lab}-{node}
                parts = cname[len('clab-'):].split('-')
                candidate = parts[0]
                if cname.startswith(f'clab-{sn}-'):
                    candidate = sn
                elif cname.startswith(f'clab-{dc}-'):
                    candidate = dc
                lab_name = candidate
                break

    print(f'  lab name: {lab_name}  clab: {clab}')

    # Try topo-file destroy first; fall back to lab-name destroy
    destroyed = False
    if topo.exists():
        r = run([clab, 'destroy', '-t', str(topo), '--cleanup'], dry=dry, capture=True)
        if dry or r.returncode == 0:
            done(f'clab destroy -t {topo}')
            destroyed = True
        else:
            print(f'  ! clab destroy -t failed (exit {r.returncode}): {r.stderr.strip()}')
            print(f'    Falling back to lab-name destroy...')

    if not destroyed:
        r = run([clab, 'destroy', '-n', lab_name, '--cleanup'], dry=dry, capture=True)
        if dry or r.returncode == 0:
            done(f'clab destroy -n {lab_name}')
            destroyed = True
        else:
            print(f'  ! clab destroy -n {lab_name} failed: {r.stderr.strip()}')
            print(f'    Falling back to docker stop/rm...')

    # Always verify containers are gone — clab destroy may exit 0 without acting
    # (e.g. when stored topo path differs from the path we provided)
    if not dry:
        dc, sn = d['dc_name'], d['sitename']
        r = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                           capture_output=True, text=True)
        victims = [c for c in r.stdout.splitlines()
                   if c.startswith(f'clab-{sn}-') or c.startswith(f'clab-{dc}-')]
        if victims:
            print(f'  ! {len(victims)} containers still running after clab destroy — force removing...')
            subprocess.run(['docker', 'stop'] + victims, capture_output=True)
            subprocess.run(['docker', 'rm']   + victims, capture_output=True)
            done(f'docker stop/rm {len(victims)} containers')

    ssh_conf     = Path(d['ssh_conf'])
    ssh_conf_alt = Path(f'/etc/ssh/ssh_config.d/clab-{lab_name}.conf')
    for conf in set([ssh_conf, ssh_conf_alt]):
        if conf.exists():
            if not dry:
                conf.unlink()
            done(f'removed {conf}')


def destroy_dnsmasq(d, dry):
    step('dnsmasq')
    pid_file = d['pid_file']
    log_file = d['log_file']
    killed = False
    if Path(pid_file).exists():
        pid = Path(pid_file).read_text().strip()
        if pid and exists(['kill', '-0', pid]):
            run(['kill', pid], dry=dry)
            killed = True
        if not dry:
            Path(pid_file).unlink(missing_ok=True)
        done(f'killed dnsmasq (PID {pid})')
    # Also catch dnsmasq bound to the bridge without a PID file
    inet = d['inet_bridge']
    r = subprocess.run(['pgrep', '-f', f'interface={inet}'], capture_output=True)
    if r.returncode == 0:
        run(['pkill', '-f', f'interface={inet}'], dry=dry)
        if not killed:
            done(f'killed dnsmasq bound to {inet}')
    if not killed and r.returncode != 0:
        skip('dnsmasq')
    if not dry and Path(log_file).exists():
        Path(log_file).unlink()


def destroy_iptables(d, dry):
    step('iptables rules')
    inet_bridge = d['inet_bridge']

    # DNS and HTTPS INPUT rules
    for proto, port in [('udp', '53'), ('tcp', '53'), ('tcp', '443')]:
        rule = ['-i', inet_bridge, '-p', proto, '--dport', port, '-j', 'ACCEPT']
        if exists(['iptables', '-C', 'INPUT'] + rule):
            run(['iptables', '-D', 'INPUT'] + rule, dry=dry)
            done(f'removed INPUT {proto}/{port} rule for {inet_bridge}')
        else:
            skip(f'INPUT {proto}/{port} for {inet_bridge}')

    # Detect host internet interface
    r = subprocess.run(
        ['ip', 'route', 'show', 'default'],
        capture_output=True, text=True,
    )
    host_iface = None
    for token in r.stdout.split():
        if token == 'dev':
            host_iface = r.stdout.split()[r.stdout.split().index('dev') + 1]
            break
    if host_iface:
        rule = ['-o', host_iface, '-j', 'MASQUERADE']
        if exists(['iptables', '-t', 'nat', '-C', 'POSTROUTING'] + rule):
            run(['iptables', '-t', 'nat', '-D', 'POSTROUTING'] + rule, dry=dry)
            done(f'removed MASQUERADE rule on {host_iface}')
        else:
            skip(f'MASQUERADE on {host_iface}')
    else:
        skip('MASQUERADE (could not detect host internet interface)')


def destroy_routes(d, dry):
    step('IP routes to fabric ranges')
    for pfx in d['fabric_routes']:
        r = subprocess.run(['ip', 'route', 'show', pfx], capture_output=True, text=True)
        if r.stdout.strip():
            run(['ip', 'route', 'del', pfx], dry=dry)
            done(f'route del {pfx}')
        else:
            skip(f'route {pfx}')


def destroy_inet_bridge(d, dry):
    step('Internet bridge and fabric bridges')
    inet = d['inet_bridge']
    ip   = d['inet_host_ip']

    if ip and exists(['ip', 'addr', 'show', inet]):
        r = subprocess.run(['ip', 'addr', 'show', inet], capture_output=True, text=True)
        if ip in r.stdout:
            run(['ip', 'addr', 'del', f'{ip}/30', 'dev', inet], dry=dry)
            done(f'removed {ip}/30 from {inet}')

    for br in d['fabric_bridges']:
        if exists(['ip', 'link', 'show', br]):
            run(['ip', 'link', 'set', br, 'down'], dry=dry)
            run(['ip', 'link', 'delete', br, 'type', 'bridge'], dry=dry)
            done(f'bridge {br}')
        else:
            skip(f'bridge {br}')


def destroy_hosts_entry(d, dry):
    step('/etc/hosts')
    api_host  = d['api_host']
    hosts_path = Path('/etc/hosts')
    content    = hosts_path.read_text()
    # Match lines added by configure-clis.py: ip hostname  # nico-sim <hostname>
    pattern = re.compile(rf'^.*{re.escape(api_host)}.*$', re.MULTILINE)
    if pattern.search(content):
        new_content = pattern.sub('', content)
        new_content = re.sub(r'\n{3,}', '\n\n', new_content)
        if not dry:
            hosts_path.write_text(new_content)
        done(f'removed /etc/hosts entry for {api_host}')
    else:
        skip(f'/etc/hosts entry for {api_host}')


def destroy_site_files(site_folder, dry):
    step('Site certs and run scripts')
    for path in ['certs', 'mat', 'sim-values', 'run-mat.sh', 'run-admin-cli.sh']:
        p = Path(site_folder) / path
        if p.exists():
            if not dry:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                else:
                    p.unlink()
            done(f'removed {p}')
        else:
            skip(str(p))


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Destroy a nico-sim site and all its resources',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('site', help='Site folder (e.g. ./sites/ytl) or yaml file')
    p.add_argument('--dry-run', '-n', action='store_true',
                   help='Print what would be destroyed without doing it')
    return p.parse_args()


def main():
    args = parse_args()
    dry  = args.dry_run

    if os.geteuid() != 0 and not dry:
        print('Error: destroy-site.py must be run as root (sudo)', file=sys.stderr)
        sys.exit(1)

    site_yaml, site_folder = resolve_site(args.site)
    sim = load_site(site_yaml)
    d   = derive(sim, site_folder)

    fabric_dir = str(Path(site_folder) / 'fabric')
    vm_dir     = str(Path(site_folder) / 'vm')

    cp    = sim.get('control_plane', {})
    img_dir = cp.get('os_image_dir', '/var/lib/libvirt/images')

    print(f'\n{"═"*55}')
    print(f'  destroy-site — {d["dc_name"]}' + (f'-{d["sitename"]}' if d['sitename'] else ''))
    if dry:
        print('  DRY RUN — no changes will be made')
    print(f'{"═"*55}')

    destroy_nico(d, site_folder, dry)
    destroy_vms(d, img_dir, dry)
    destroy_oob_networks(d, dry)
    destroy_host_bridges(d, dry)
    destroy_fabric(d, fabric_dir, dry)
    destroy_dnsmasq(d, dry)
    destroy_iptables(d, dry)
    destroy_routes(d, dry)
    destroy_inet_bridge(d, dry)
    destroy_hosts_entry(d, dry)
    destroy_site_files(site_folder, dry)

    print(f'\n{"═"*55}')
    if dry:
        print('  Dry run complete. Re-run without --dry-run to destroy.')
    else:
        print('  Site destroyed. Ready for a clean redeploy:')
        print(f'  1. sudo python3 deploy-fabric.py {args.site}')
        print(f'  2. sudo python3 deploy-vms.py    {args.site}')
        print(f'  3. python3 deploy-nico-system.py {args.site}')
    print(f'{"═"*55}\n')


if __name__ == '__main__':
    main()
