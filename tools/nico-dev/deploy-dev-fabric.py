#!/usr/bin/env python3
"""
nico-dev — Generate and deploy the dev fabric.

Steps:
  1. Generate ContainerLab topology + FRR configs
  2. Create Linux bridges (br-<dc>-cp, br-<dc>-internet)
  3. Enable IP forwarding + NAT + fabric routes on host
  4. Deploy ContainerLab
  5. Add host CP interface to br-<dc>-cp (connects VM to DPU stand-in)
  6. Add static route for MAT underlay

Must run as root (sudo).

Usage:
  sudo python3 deploy-dev-fabric.py <site>
"""

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


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


def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f'  ! {" ".join(cmd)}: {r.stderr.strip()}', file=sys.stderr)
    return r


def _install_boot_service(site_yaml):
    """Copy this script + generator to a local path and register as a systemd service.

    The local copy at /usr/local/lib/nico-dev/ is SSHFS-independent, so the
    service works at boot before the Mac mount is available. On every fabric
    deploy the local copy is refreshed from the current (possibly SSHFS-mounted)
    source — one script, one source of truth.
    """
    lib_dir = Path('/usr/local/lib/nico-dev')
    lib_dir.mkdir(parents=True, exist_ok=True)

    this_dir = Path(__file__).parent
    for script in ['deploy-dev-fabric.py', 'generate-dev-fabric.py']:
        src = this_dir / script
        dst = lib_dir / script
        if src.exists() and src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
            dst.chmod(0o755)

    site_abs = str(Path(site_yaml).resolve())

    # Site path goes in an EnvironmentFile — keeps the service unit generic
    # (no username, no path) so a golden VM image works for any user/site.
    env_dir = Path('/etc/nico-dev')
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / 'env').write_text(f'NICO_DEV_SITE={site_abs}\n')

    # RequiresMountsFor: at boot the site yaml and fabric dir live on the 9p
    # share (+ bindfs) — without this ordering the service can fire before the
    # mounts and fail to read the site config. (Golden images that point
    # NICO_DEV_SITE at VM-local /etc/nico-dev/dev.yaml are unaffected.)
    site_dir = str(Path(site_abs).parent)
    service = f'''\
[Unit]
Description=nico-dev ContainerLab fabric
After=docker.service containerd.service network-online.target
Wants=network-online.target
Requires=docker.service containerd.service
RequiresMountsFor={site_dir}

[Service]
Type=oneshot
EnvironmentFile=/etc/nico-dev/env
ExecStart=/usr/bin/python3 {lib_dir}/deploy-dev-fabric.py ${{NICO_DEV_SITE}}
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''
    svc = Path('/etc/systemd/system/nico-dev-fabric.service')
    svc.write_text(service)
    subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
    subprocess.run(['systemctl', 'enable', 'nico-dev-fabric.service'], capture_output=True)
    print(f'  scripts    → {lib_dir}')
    print(f'  site       → {site_abs}  (/etc/nico-dev/env)')
    print(f'  nico-dev-fabric.service enabled ✓')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if os.geteuid() != 0:
        print('Error: must run as root (sudo)', file=sys.stderr)
        sys.exit(1)

    # Tool preflight — a missing binary must be a one-line diagnosis, not a
    # traceback (a silent installer failure once shipped a clab-less VM).
    for tool in ['clab', 'docker', 'ip', 'iptables']:
        if shutil.which(tool) is None:
            print(f'Error: required tool "{tool}" not found on PATH.', file=sys.stderr)
            if tool == 'clab':
                print('  Install: curl -fsSL https://get.containerlab.dev -o /tmp/c.sh '
                      '&& sudo bash /tmp/c.sh', file=sys.stderr)
            print('  (prepare-vm.sh normally installs this — re-run it to repair)',
                  file=sys.stderr)
            sys.exit(1)

    site_yaml, site_folder = resolve_site(sys.argv[1])
    cfg = yaml.safe_load(open(site_yaml))
    dc  = cfg['fabric']['dc_name']
    pfx = cfg['fabric']['prefixes']
    fabric_dir = Path(site_folder) / 'fabric'

    print('nico-dev — Deploy fabric')
    print(f'  site   : {site_yaml}')
    print(f'  dc_name: {dc}')

    # ── Step 1: Generate topology ─────────────────────────────────────────────
    print('\nStep 1: Generating fabric topology...')
    gen = Path(__file__).parent / 'generate-dev-fabric.py'
    r = subprocess.run([sys.executable, str(gen), sys.argv[1]])
    if r.returncode != 0:
        print('generate-dev-fabric.py failed', file=sys.stderr); sys.exit(1)

    ips = json.loads((fabric_dir / 'fabric-ips.json').read_text())

    # ── Step 2: Linux bridges ─────────────────────────────────────────────────
    # Disable swap — kubelet refuses to start with swap on. Run here so the
    # boot service guarantees it before restarting kubelet, regardless of
    # whether /etc/fstab was fixed in the base image.
    subprocess.run(['swapoff', '-a'], capture_output=True)
    print('\nStep 2: Creating Linux bridges...')
    for br in [f'br-{dc}-cp', f'br-{dc}-internet']:
        if run(['ip', 'link', 'show', br], check=False).returncode != 0:
            run(['ip', 'link', 'add', br, 'type', 'bridge'])
            run(['ip', 'link', 'set', br, 'up'])
            print(f'  created {br}')
        else:
            print(f'  {br} already exists')

    # ── Step 3: IP forwarding + NAT + routes ──────────────────────────────────
    print('\nStep 3: Enabling IP forwarding + NAT...')
    run(['sysctl', '-w', 'net.ipv4.ip_forward=1'])

    # Assign host internet uplink IP on the internet bridge
    inet_br = f'br-{dc}-internet'
    host_inet = ips['host_inet_ip']
    r = run(['ip', 'addr', 'show', inet_br], check=False)
    if host_inet not in r.stdout:
        run(['ip', 'addr', 'add', f'{host_inet}/30', 'dev', inet_br])
        print(f'  assigned {host_inet}/30 to {inet_br}')

    # NAT for outbound internet
    host_iface = subprocess.run(
        ['ip', 'route', 'show', 'default'],
        capture_output=True, text=True
    ).stdout.split()
    host_iface = host_iface[host_iface.index('dev') + 1] if 'dev' in host_iface else None
    if host_iface:
        # MASQUERADE for fabric bridge traffic going out to internet
        r = run(['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-s',
                 cfg['fabric']['prefixes']['internet_uplink'],
                 '-o', host_iface, '-j', 'MASQUERADE'], check=False)
        if r.returncode != 0:
            run(['iptables', '-t', 'nat', '-A', 'POSTROUTING', '-s',
                 cfg['fabric']['prefixes']['internet_uplink'],
                 '-o', host_iface, '-j', 'MASQUERADE'])

        # FORWARD: Docker sets policy DROP — explicitly allow fabric bridge traffic
        for fwd_rule in [
            ['-I', 'FORWARD', '-i', inet_br, '-o', host_iface, '-j', 'ACCEPT'],
            ['-I', 'FORWARD', '-i', host_iface, '-o', inet_br,
             '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
        ]:
            if run(['iptables', '-C'] + fwd_rule[1:], check=False).returncode != 0:
                run(['iptables'] + fwd_rule)
        print(f'  NAT/MASQUERADE + FORWARD rules on {host_iface}')

    # Allow port 443 (MAT BMC mocks) and 53 (DNS) from fabric bridge
    for proto, port in [('tcp', '53'), ('udp', '53'), ('tcp', '443')]:
        rule = ['-i', inet_br, '-p', proto, '--dport', port, '-j', 'ACCEPT']
        if run(['iptables', '-C', 'INPUT'] + rule, check=False).returncode != 0:
            run(['iptables', '-I', 'INPUT'] + rule)
    print(f'  iptables: allowed DNS + HTTPS from {inet_br}')

    # ── Step 3b: Ensure ARM64-compatible FRR image ────────────────────────────
    # frrouting/frr:latest has no ARM64 variant — build from apt instead
    # (native arch either way; apt resolves per-arch). Name is arch-neutral.
    # The local image is built once and reused on subsequent runs.
    image = 'frr-local:local'
    print(f'\nStep 3b: Ensuring local FRR image ({image})...')
    # Always rebuild to pick up Dockerfile changes
    r = subprocess.run(['docker', 'image', 'inspect', image],
                       capture_output=True)
    has_docker_start = False
    if r.returncode == 0:
        # Check the image actually has docker-start (might be stale)
        check = subprocess.run(
            ['docker', 'run', '--rm', image, 'test', '-f', '/usr/lib/frr/docker-start'],
            capture_output=True)
        has_docker_start = (check.returncode == 0)

    if not has_docker_start:
        print(f'  Building {image} from Ubuntu apt (FRR has no ARM64 Docker image)...')
        dockerfile = r'''FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && \
    apt-get install -y -q frr frr-pythontools iproute2 iputils-ping tini && \
    rm -rf /var/lib/apt/lists/*

# Create docker-start script (not included in Ubuntu apt package)
RUN printf '#!/bin/bash\n\
set -e\n\
mkdir -p /run/frr /var/log/frr\n\
chown frr:frr /run/frr /var/log/frr\n\
[ -f /etc/frr/vtysh.conf ] || printf "service integrated-vtysh-config\\n" > /etc/frr/vtysh.conf\n\
[ -f /etc/frr/daemons ] && . /etc/frr/daemons\n\
/usr/lib/frr/zebra -d -s 90000000 --vty_socket /run/frr 2>/dev/null || true\n\
[ "$bgpd" = "yes" ]    && /usr/lib/frr/bgpd    -d --vty_socket /run/frr 2>/dev/null || true\n\
[ "$staticd" = "yes" ] && /usr/lib/frr/staticd  -d --vty_socket /run/frr 2>/dev/null || true\n\
sleep 2\n\
vtysh -f /etc/frr/frr.conf 2>/dev/null || true\n\
exec /usr/lib/frr/watchfrr zebra bgpd staticd\n\
' > /usr/lib/frr/docker-start && chmod +x /usr/lib/frr/docker-start

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/lib/frr/docker-start"]
'''
        env = {**os.environ, 'DOCKER_BUILDKIT': '0'}
        r2 = subprocess.run(
            ['docker', 'build', '-t', image, '-'],
            input=dockerfile, capture_output=False, text=True, env=env
        )
        if r2.returncode != 0:
            print(f'  Error: failed to build {image}', file=sys.stderr)
            sys.exit(1)
        print(f'  {image} built ✓')
    else:
        print(f'  {image} already exists and is valid ✓')

    # Import into containerd namespace used by ContainerLab (separate from Docker)
    print(f'  Importing into containerd (clab namespace)...')
    import tempfile
    tmp_tar = tempfile.mktemp(suffix='.tar')
    try:
        subprocess.run(['docker', 'save', '-o', tmp_tar, image], check=True)
        imp = subprocess.run(['ctr', '-n', 'clab', 'images', 'import', tmp_tar],
                             capture_output=True)
        if imp.returncode == 0:
            print(f'  containerd import ✓')
        else:
            print(f'  Warning: containerd import failed: {imp.stderr.decode().strip()}')
    finally:
        if os.path.exists(tmp_tar):
            os.unlink(tmp_tar)

    # ── Step 3c: Clean up any leftover containers from previous runs ───────────
    print('\nStep 3c: Cleaning up any leftover containers...')
    topo = fabric_dir / 'topo.clab.yml'
    subprocess.run(['clab', 'destroy', '-n', dc, '--cleanup'],
                   capture_output=True)
    r = subprocess.run(['docker', 'ps', '-aq', '--filter', f'name=clab-{dc}-'],
                       capture_output=True, text=True)
    leftover = r.stdout.split()
    if leftover:
        subprocess.run(['docker', 'rm', '-f'] + leftover, capture_output=True)
        print(f'  removed {len(leftover)} leftover container(s)')
    else:
        print('  no leftover containers')

    # ── Step 4: Deploy ContainerLab ───────────────────────────────────────────
    print('\nStep 4: Deploying ContainerLab topology...')
    r = subprocess.run(['clab', 'deploy', '-t', str(topo)], cwd=str(fabric_dir))
    if r.returncode != 0:
        print('clab deploy failed', file=sys.stderr); sys.exit(1)

    # Fix permissions so non-root can read fabric dir
    subprocess.run(['chmod', '-R', 'a+rX', str(fabric_dir)], capture_output=True)
    print('  ContainerLab deployed ✓')

    # ── Step 5: Attach VM host to the CP bridge (CP link) ─────────────────────
    cp_br = f'br-{dc}-cp'
    print(f'\nStep 5: Attaching VM host to {cp_br}...')
    vm_cp_ip = ips['vm_cp_ip']
    cp_link  = pfx['control_plane_link']
    plen     = ipaddress.IPv4Network(cp_link, strict=False).prefixlen

    r = run(['ip', 'addr', 'show', cp_br], check=False)
    if vm_cp_ip not in r.stdout:
        run(['ip', 'addr', 'add', f'{vm_cp_ip}/{plen}', 'dev', cp_br])
        print(f'  assigned {vm_cp_ip}/{plen} to {cp_br} (MetalLB BGP peer)')
    else:
        print(f'  {vm_cp_ip}/{plen} already on {cp_br}')

    # ── Step 6: Add fabric routes on host ─────────────────────────────────────
    print('\nStep 6: Adding fabric routes...')
    ss_inet = ips['ss_inet_ip']
    sw_underlay = pfx['switch_underlay']
    dpu_fabric  = pfx['dpu_fabric']
    dpu_lo_pfx  = pfx['dpu_loopbacks']
    sw_lo_pfx   = pfx['switch_loopbacks']
    svc_vips    = pfx.get('service_vips', '')

    for route_pfx in [sw_underlay, dpu_fabric, dpu_lo_pfx, sw_lo_pfx]:
        if run(['ip', 'route', 'show', route_pfx], check=False).stdout.strip():
            continue
        run(['ip', 'route', 'add', route_pfx, 'via', ss_inet, 'dev', inet_br])
        print(f'  added route: {route_pfx} via {ss_inet}')

    if svc_vips:
        if not run(['ip', 'route', 'show', svc_vips], check=False).stdout.strip():
            run(['ip', 'route', 'add', svc_vips, 'via', ss_inet, 'dev', inet_br])
            print(f'  added route: {svc_vips} via {ss_inet} (MetalLB VIPs)')

    # MAT underlay via leaf-mat (through the internet bridge → fabric)
    mat_pfx = pfx['mat_underlay']
    if not run(['ip', 'route', 'show', mat_pfx], check=False).stdout.strip():
        run(['ip', 'route', 'add', mat_pfx, 'via', ss_inet, 'dev', inet_br])
        print(f'  added route: {mat_pfx} via {ss_inet} (MAT underlay)')

    # ── Step 7: Restart kubelet (now that the CP bridge and its node IP exist) ───
    if Path('/usr/bin/kubelet').exists():
        print('\nStep 7: Restarting kubelet...')
        run(['systemctl', 'restart', 'kubelet'], check=False)
        print('  kubelet restarted ✓')

    # ── Step 8: Install self as boot service ───────────────────────────────────
    print('\nStep 8: Installing nico-dev-fabric boot service...')
    _install_boot_service(site_yaml)

    print(f'\n{"="*55}')
    print(f'  Fabric deployed ✓')
    print(f'  VM CP IP : {vm_cp_ip}')
    print(f'  DPU CP IP: {ips["dpu_cp_ip"]}  (MetalLB BGP peer)')
    print(f'\n  Boot service: nico-dev-fabric.service (auto-starts on reboot)')
    here = Path(__file__).resolve().parent
    print(f'\n  Verify: python3 {here}/ndev.py {sys.argv[1]} fabric verify')
    print(f'  Next  : sudo python3 {here}/deploy-dev-cp.py {sys.argv[1]}')
    print(f'{"="*55}')


if __name__ == '__main__':
    main()
