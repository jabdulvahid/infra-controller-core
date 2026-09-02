#!/usr/bin/env python3
"""
nico-dev — Build a nico-dev base VM on a LINUX host (libvirt/KVM edition).

Same shape as the Mac edition: Ubuntu cloud image + cloud-init, static IP
on 192.168.64.<host-num>, share mounted in the guest at /mnt/mac. Linux
specifics (all CLI — there is no GUI step on this platform):

  network  nico-nat        libvirt NAT net, 192.168.64.0/24, bridge virbr-nico
                           (SHARED infra — one per host, created loudly if
                           missing, never touches existing nets)
  pool     nico-dev        libvirt dir pool for VM disks (system-mode QEMU
                           cannot read $HOME, so disks live in a pool; all
                           managed via virsh vol-* — no sudo)
  disk     <vm>-root.qcow2 cloud image, qemu-img resized, virsh vol-uploaded
  seed     <vm>-seed.iso   cloud-localds (user-data, meta-data, network-config)
  share    virtiofs        host folder → guest tag 'share' (9p is wrong here:
                           system QEMU runs as libvirt-qemu, so 9p-written
                           files would be unreadable on the host)
  console  serial          virsh console <vm> (getty enabled by the seed)

Ledger: ~/.nico-dev/vms/<vm>.yaml records what was created (dev-down reads it).

  ./build-nico-dev-vm.py --name nico-dc1-dev1 --share ~/nico-tests/vm1/shared
  ./build-nico-dev-vm.py --name x --dry-run
  ./build-nico-dev-vm.py --name x --stage seed

Stages: image → seed → vm → boot.  Rerun-safe: existing volumes/domain are
reused, the seed is regenerated. Teardown: dev-down.py (or virsh destroy +
virsh undefine --remove-all-storage).
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UBUNTU_RELEASE = '26.04'
HOST_ARCH = platform.machine()                        # 'x86_64' | 'aarch64'
IMG_ARCH = 'arm64' if HOST_ARCH == 'aarch64' else 'amd64'
SERIAL_TTY = 'ttyAMA0' if HOST_ARCH == 'aarch64' else 'ttyS0'
CLOUD_IMG_URL = (f'https://cloud-images.ubuntu.com/releases/{UBUNTU_RELEASE}/'
                 f'release/ubuntu-{UBUNTU_RELEASE}-server-cloudimg-{IMG_ARCH}.img')
CACHE_DIR = Path.home() / '.cache' / 'nico-dev'
LEDGER_DIR = Path.home() / '.nico-dev' / 'vms'
CONN = 'qemu:///system'
NET_NAME = 'nico-nat'
BRIDGE = 'virbr-nico'                  # ≤15 chars (IFNAMSIZ); fixed, shared
POOL = 'nico-dev'
POOL_DIR = '/var/lib/libvirt/images/nico-dev'
DEF_SUBNET = '192.168.64'              # identical addressing to the Mac
DEF_DISK_GB = 120
DEF_CPUS = 6
DEF_MEM_MB = 12288
DEFAULT_HOST_NUM = 126


def run(cmd, label='', check=True, capture=False, input_text=None):
    print(f'  $ {" ".join(str(c) for c in cmd)}')
    r = subprocess.run([str(c) for c in cmd], text=True, input=input_text,
                       capture_output=capture)
    if check and r.returncode != 0:
        if capture and r.stderr:
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f'Error: {label or cmd[0]} failed (exit {r.returncode})')
    return r


def virsh(*a, check=True, capture=True):
    return run(['virsh', '-c', CONN, *a], f'virsh {a[0]}', check=check,
               capture=capture)


def virsh_ok(*a):
    return subprocess.run(['virsh', '-c', CONN, *a],
                          capture_output=True).returncode == 0


# ── shared infrastructure (created loudly, never modified if present) ──────

def ensure_net(subnet):
    if virsh_ok('net-info', NET_NAME):
        xml = virsh('net-dumpxml', NET_NAME).stdout
        if f"address='{subnet}.1'" not in xml:
            raise SystemExit(f'Error: libvirt network {NET_NAME} exists but is '
                             f'not on {subnet}.0/24 — remove it or pass the '
                             f'matching --subnet')
        print(f'  network {NET_NAME} ({subnet}.0/24) exists ✓')
        return
    print(f'  Creating libvirt network {NET_NAME}: NAT {subnet}.0/24, '
          f'bridge {BRIDGE}, DHCP {subnet}.2-.99 (static VMs live above)')
    xml = f'''<network>
  <name>{NET_NAME}</name>
  <forward mode='nat'/>
  <bridge name='{BRIDGE}' stp='on' delay='0'/>
  <ip address='{subnet}.1' netmask='255.255.255.0'>
    <dhcp><range start='{subnet}.2' end='{subnet}.99'/></dhcp>
  </ip>
</network>
'''
    with tempfile.NamedTemporaryFile('w', suffix='.xml', delete=False) as f:
        f.write(xml)
        path = f.name
    try:
        virsh('net-define', path)
        virsh('net-start', NET_NAME)
        virsh('net-autostart', NET_NAME)
    finally:
        os.unlink(path)
    print(f'  network {NET_NAME} created + autostart ✓')


def ensure_pool():
    if virsh_ok('pool-info', POOL):
        print(f'  storage pool {POOL} exists ✓')
        return
    print(f'  Creating libvirt storage pool {POOL} at {POOL_DIR}')
    virsh('pool-define-as', POOL, 'dir', '--target', POOL_DIR)
    virsh('pool-build', POOL)
    virsh('pool-start', POOL)
    virsh('pool-autostart', POOL)
    print(f'  pool {POOL} created + autostart ✓')


def vol_exists(name):
    return virsh_ok('vol-info', '--pool', POOL, name)


def vol_upload(name, local, fmt, size):
    if vol_exists(name):
        virsh('vol-delete', '--pool', POOL, name)
    virsh('vol-create-as', POOL, name, str(size), '--format', fmt)
    virsh('vol-upload', '--pool', POOL, name, str(local))
    virsh('pool-refresh', POOL)


# ── stages ────────────────────────────────────────────────────────────────

def stage_image(work, args):
    print('\n── Stage: image (download + resize + upload to pool) ──')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / Path(CLOUD_IMG_URL).name
    if cached.exists():
        print(f'  Cached: {cached} ✓')
    else:
        run(['curl', '-fL', '--retry', '5', '--retry-all-errors',
             '-o', str(cached) + '.part', CLOUD_IMG_URL], 'cloud image download')
        os.rename(str(cached) + '.part', cached)
        print(f'  Downloaded: {cached} ✓')

    ensure_pool()
    root_vol = f'{args.name}-root.qcow2'
    if vol_exists(root_vol):
        # Rerun safety: never clobber a disk an existing VM may own.
        print(f'  volume {POOL}/{root_vol} exists — reusing ✓')
        return root_vol
    disk = work / root_vol
    shutil.copy2(cached, disk)
    run(['qemu-img', 'resize', str(disk), f'{args.disk_gb}G'], 'qemu-img resize')
    print(f'  local disk prepared ({args.disk_gb}G sparse) ✓')
    vol_upload(root_vol, disk, 'qcow2', f'{args.disk_gb}G')
    disk.unlink(missing_ok=True)        # the pool copy is the real one
    print(f'  volume {POOL}/{root_vol} uploaded ✓')
    return root_vol


def stage_seed(work, args):
    print('\n── Stage: seed (cloud-init ISO) ──')
    static_ip = args.ip or f'{args.subnet}.{args.host_num}'
    gateway = static_ip.rsplit('.', 1)[0] + '.1'
    print(f'  static IP {static_ip}, gateway {gateway} ({NET_NAME})')

    if args.ssh_key:
        key_file = Path(args.ssh_key).expanduser()
        if not key_file.exists():
            raise SystemExit(f'Error: --ssh-key {key_file} not found')
    else:
        pubkeys = sorted(Path.home().glob('.ssh/id_*.pub'))
        if not pubkeys:
            raise SystemExit('Error: no SSH public key in ~/.ssh (ssh-keygen '
                             'first, or pass --ssh-key)')
        key_file = pubkeys[0]
    ssh_key = key_file.read_text().strip()
    print(f'  SSH key: {key_file.name} ✓')
    print(f'  VM user: {args.user} uid={args.uid}'
          + (' (= your host UID — share files owned by you in the VM)'
             if args.uid == os.getuid() else ''))

    # SYNC NOTE: user-data/network-config content mirrors
    # build-nico-dev-vm_mac.py by design (separate host codebases, user
    # ruling 2026-09-02). Keep the two in step when changing cloud-init.
    seed_dir = work / 'seed'
    seed_dir.mkdir(exist_ok=True)
    (seed_dir / 'meta-data').write_text(
        f'instance-id: {args.name}\nlocal-hostname: {args.name}\n')
    (seed_dir / 'user-data').write_text(f'''\
#cloud-config
hostname: {args.name}
users:
  - name: {args.user}
    uid: {args.uid}
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo, docker]
    shell: /bin/bash
    lock_passwd: false
    ssh_authorized_keys:
      - {ssh_key}
chpasswd:
  expire: false
  users:
    - {{name: {args.user}, password: {args.password}, type: text}}
ssh_pwauth: true
package_update: true
packages:
  - docker.io
  - python3-yaml
  - rsync
  - bindfs
  - qemu-guest-agent
  - curl
growpart:
  mode: auto
  devices: ['/']
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable --now docker
  - systemctl enable --now serial-getty@{SERIAL_TTY}.service || true
final_message: "nico-dev base VM ready: ssh {args.user}@{static_ip}"
''')
    # Static IP via the seed's network-config (replaces cloud-init's fallback
    # DHCP; applies at EARLY boot). Name glob en* matches every NIC type
    # (virtio here; e1000 on UTM x86) — 20260901-#4/#9.
    (seed_dir / 'network-config').write_text(f'''\
version: 2
ethernets:
  nic0:
    match:
      name: en*
    dhcp4: false
    addresses: [{static_ip}/24]
    routes:
      - to: default
        via: {gateway}
    nameservers:
      addresses: [{gateway}]
''')
    iso = work / f'{args.name}-seed.iso'
    iso.unlink(missing_ok=True)
    run(['cloud-localds', '-N', str(seed_dir / 'network-config'), str(iso),
         str(seed_dir / 'user-data'), str(seed_dir / 'meta-data')], 'cloud-localds')
    seed_vol = f'{args.name}-seed.iso'
    vol_upload(seed_vol, iso, 'raw', iso.stat().st_size)
    print(f'  seed volume {POOL}/{seed_vol} ✓')
    return seed_vol, static_ip


def stage_vm(root_vol, seed_vol, args):
    print('\n── Stage: vm (virt-install) ──')
    ensure_net(args.subnet)
    share = str(Path(args.share).expanduser().resolve())
    if virsh_ok('dominfo', args.name):
        print(f'  VM "{args.name}" already exists — skipping create ✓')
        state = virsh('domstate', args.name).stdout.strip()
        if state != 'running':
            virsh('start', args.name)
            print('  started ✓')
        return
    desc = (f'nico-dev VM · site {args.dc}/{args.site} · created '
            f'{time.strftime("%Y-%m-%d")} · share {share}')
    cmd = ['virt-install', '--connect', CONN, '--name', args.name,
           '--memory', str(args.mem_mb), '--vcpus', str(args.cpus),
           '--import', '--osinfo', 'detect=on,require=off',
           '--disk', f'vol={POOL}/{root_vol},bus=virtio,format=qcow2',
           '--disk', f'vol={POOL}/{seed_vol},device=cdrom,format=raw',
           '--network', f'network={NET_NAME},model=virtio',
           # virtiofs needs shared memory backing
           '--memorybacking', 'source.type=memfd,access.mode=shared',
           '--filesystem', f'source={share},target=share,driver.type=virtiofs',
           '--graphics', 'none', '--console', 'pty,target.type=serial',
           '--metadata', f'description={desc}',
           '--noautoconsole']
    run(cmd, 'virt-install')
    print(f'  VM created + started: {args.name} ✓  (console: virsh console {args.name})')


def write_ledger(args, static_ip):
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (LEDGER_DIR / f'{args.name}.yaml').write_text(f'''\
# nico-dev ledger — what build-nico-dev-vm (linux) created; dev-down reads it
platform: linux
vm: {args.name}
connection: {CONN}
network: {NET_NAME}
pool: {POOL}
volumes: [{args.name}-root.qcow2, {args.name}-seed.iso]
ip: {static_ip}
dc: {args.dc}
site: {args.site}
share: {Path(args.share).expanduser().resolve()}
user: {args.user}
created: {time.strftime('%Y-%m-%dT%H:%M:%S')}
''')
    print(f'  ledger: {LEDGER_DIR / (args.name + ".yaml")} ✓')


def stage_boot(static_ip, args):
    print('\n── Stage: boot (wait for ssh) ──')
    print(f'  Waiting for cloud-init + ssh at {static_ip} '
          f'(static IP applies early; package installs continue behind)...')
    deadline = time.time() + 900
    while time.time() < deadline:
        if subprocess.run(['nc', '-z', '-w', '2', static_ip, '22'],
                          capture_output=True).returncode == 0:
            print('  ssh is up ✓')
            break
        time.sleep(10)
    else:
        raise SystemExit(f'Error: ssh not reachable at {static_ip} after 15 min '
                         f'— console: virsh console {args.name}')
    write_ledger(args, static_ip)
    print(f'''
{'=' * 60}
  Base VM ready:  ssh {args.user}@{static_ip}   (key auth, or the password)
  Console:        virsh console {args.name}   (Ctrl-] to leave)
  Teardown:       dev-down.py --name {args.name}
{'=' * 60}''')


def main():
    p = argparse.ArgumentParser(
        description='Build a nico-dev base VM on a Linux host (libvirt/KVM)',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--name', required=True, help='VM name (e.g. nico-dc1-dev1)')
    p.add_argument('--share', default=str(Path.home()),
                   help='host folder shared into the VM (virtiofs, tag "share")')
    p.add_argument('--host-num', type=int, default=DEFAULT_HOST_NUM,
                   help=f'last octet of the static IP (default {DEFAULT_HOST_NUM})')
    p.add_argument('--subnet', default=DEF_SUBNET,
                   help=f'first three octets of {NET_NAME} (default {DEF_SUBNET} '
                        '— identical to the Mac edition)')
    p.add_argument('--ip', default=None, help='full static IP (overrides subnet/host-num)')
    p.add_argument('--user', default='nico')
    p.add_argument('--uid', type=int, default=os.getuid(),
                   help=f'VM user UID (default: your UID {os.getuid()} — share '
                        'ownership matches both ways)')
    p.add_argument('--password', default='Welcome123!')
    p.add_argument('--ssh-key', default=None)
    p.add_argument('--cpus', type=int, default=DEF_CPUS)
    p.add_argument('--mem-mb', type=int, default=DEF_MEM_MB)
    p.add_argument('--disk-gb', type=int, default=DEF_DISK_GB)
    p.add_argument('--dc', default='', help='(metadata) datacenter name')
    p.add_argument('--site', default='', help='(metadata) site name')
    p.add_argument('--work-dir', default=None,
                   help='scratch dir (default: ~/.cache/nico-dev/work/<name>)')
    p.add_argument('--stage', choices=['image', 'seed', 'vm', 'boot'], default='boot')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    work = Path(args.work_dir or CACHE_DIR / 'work' / args.name)
    print('nico-dev — Build base VM (Linux/libvirt)')
    print(f'  name     : {args.name}')
    print(f'  ubuntu   : {UBUNTU_RELEASE} cloud image ({IMG_ARCH})')
    print(f'  disk     : {args.disk_gb}G sparse, {args.cpus} cpus, {args.mem_mb}MB')
    print(f'  network  : {NET_NAME} {args.subnet}.0/24 → '
          f'{args.ip or f"{args.subnet}.{args.host_num}"}')
    print(f'  share    : {args.share} (virtiofs → /mnt/mac)')
    print(f'  pool     : {POOL}')
    if args.dry_run:
        print('\n(dry run — nothing executed)')
        return
    work.mkdir(parents=True, exist_ok=True)

    root_vol = stage_image(work, args)
    if args.stage == 'image':
        return
    seed_vol, static_ip = stage_seed(work, args)
    if args.stage == 'seed':
        return
    stage_vm(root_vol, seed_vol, args)
    if args.stage == 'vm':
        return
    stage_boot(static_ip, args)


if __name__ == '__main__':
    main()
