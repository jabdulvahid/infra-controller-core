#!/usr/bin/env python3
"""
nico-dev — Build a nico-dev base VM from scratch (no manual Ubuntu install)

Creates a UTM VM from the Ubuntu cloud image (~900MB download, no
installer) personalized by cloud-init: nico user, base packages, static IP
derived from THIS Mac's vmnet subnet, qemu-guest-agent. The result is the
"vanilla Ubuntu + prerequisites" starting point of the advanced-user path
(how-to §1) — ready for deploy-dev-fabric.py onward.

Spikes validated 2026-08-28: cloud image exists (900MB arm64), UTM.sdef
supports make/update-configuration/Configuration Suite. See POR backlog.

  ./build-nico-dev-vm.py --name nico-dev-c5            # full build
  ./build-nico-dev-vm.py --name x --dry-run            # show plan only
  ./build-nico-dev-vm.py --name x --stage seed         # stop after a stage

Stages: image (download+resize) → seed (cloud-init ISO) → vm (AppleScript
create) → boot (start + wait for ssh).

Prereqs on the Mac: UTM, automation permission for your terminal over UTM
(System Settings → Privacy → Automation), an SSH public key in ~/.ssh.
No qemu install needed: the disk resize is done by UTM itself via the
scriptable `guest size` drive property (its bundled qemu-img is an
in-process dylib, not shell-executable — verified 2026-08-28).
"""

import argparse
import os
import platform
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UBUNTU_RELEASE = '26.04'
# Host arch drives everything: Apple Silicon → arm64 guest, Intel → amd64.
HOST_ARCH = platform.machine()                    # 'arm64' | 'x86_64'
IMG_ARCH = 'arm64' if HOST_ARCH == 'arm64' else 'amd64'   # Ubuntu image name
QEMU_ARCH = 'aarch64' if HOST_ARCH == 'arm64' else 'x86_64'
CLOUD_IMG_URL = (f'https://cloud-images.ubuntu.com/releases/{UBUNTU_RELEASE}/'
                 f'release/ubuntu-{UBUNTU_RELEASE}-server-cloudimg-{IMG_ARCH}.img')
CACHE_DIR = Path.home() / '.cache' / 'nico-dev'
DEF_DISK_GB = 120
DEF_CPUS = 6
DEF_MEM_MB = 12288
VMNET_PLIST = '/Library/Preferences/SystemConfiguration/com.apple.vmnet.plist'
DEFAULT_HOST_NUM = 126        # nico-dev convention: <subnet>.126


def run(cmd, label='', check=True, capture=False, input_text=None):
    print(f'  $ {" ".join(str(c) for c in cmd)}')
    r = subprocess.run(cmd, text=True, input=input_text,
                       capture_output=capture)
    if check and r.returncode != 0:
        if capture and r.stderr:
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f'Error: {label or cmd[0]} failed (exit {r.returncode})')
    return r


def vmnet_subnet():
    """The Mac's UTM shared-network subnet base (e.g. '192.168.64'), read
    from the vmnet plist. Deriving the static IP from it kills the
    foreign-Mac off-subnet risk at creation time (POR C3b known risk).
    The plist is root-only (640 root:wheel) — try a passwordless sudo
    before falling back to the common default."""
    try:
        with open(VMNET_PLIST, 'rb') as f:
            addr = plistlib.load(f).get('Shared_Net_Address', '192.168.64.1')
    except (FileNotFoundError, PermissionError):
        r = subprocess.run(['sudo', '-n', 'cat', VMNET_PLIST],
                           capture_output=True)
        if r.returncode == 0:
            addr = plistlib.loads(r.stdout).get('Shared_Net_Address',
                                                '192.168.64.1')
        else:
            addr = '192.168.64.1'
            print(f'  WARNING: vmnet plist unreadable (root-only) — '
                  f'assuming {addr}.\n  If your UTM VMs get addresses on '
                  f'a DIFFERENT subnet, rerun with an explicit --ip.')
    return addr.rsplit('.', 1)[0]


def qcow2_grow(disk, new_size):
    """Grow a qcow2's virtual size by header patch — no qemu-img on macOS,
    and UTM's scripting can't resize existing drives (20260828-#3).

    A GROW (no snapshots, no backing file) only changes two header fields:
    size (offset 24) and l1_size (offset 36) — provided the larger L1 entry
    count still fits inside the L1 table's already-allocated cluster(s) and
    the tail entries are zero (= unallocated, valid). Every precondition is
    checked; any surprise aborts before writing a byte."""
    with open(disk, 'r+b') as f:
        hdr = f.read(104)
        magic, version = struct.unpack('>4sI', hdr[0:8])
        if magic != b'QFI\xfb' or version not in (2, 3):
            raise SystemExit(f'Error: {disk} is not a qcow2 v2/v3 image')
        backing_off, = struct.unpack('>Q', hdr[8:16])
        cluster_bits, = struct.unpack('>I', hdr[20:24])
        cur_size, = struct.unpack('>Q', hdr[24:32])
        crypt, l1_size = struct.unpack('>II', hdr[32:40])
        l1_off, = struct.unpack('>Q', hdr[40:48])
        nb_snapshots, = struct.unpack('>I', hdr[60:64])
        if cur_size >= new_size:
            print(f'  qcow2 virtual size already {cur_size >> 30}G ✓')
            return
        if backing_off or crypt or nb_snapshots:
            raise SystemExit('Error: image has backing file/encryption/'
                             'snapshots — cannot header-grow')
        cluster = 1 << cluster_bits
        per_l1 = (cluster // 8) * cluster          # bytes one L1 entry maps
        need_l1 = -(-new_size // per_l1)
        l1_alloc = -(-(l1_size * 8) // cluster) * cluster
        if need_l1 * 8 > l1_alloc:
            raise SystemExit(f'Error: need {need_l1} L1 entries but only '
                             f'{l1_alloc // 8} fit the allocated L1 table')
        f.seek(l1_off + l1_size * 8)
        if f.read((need_l1 - l1_size) * 8).strip(b'\x00'):
            raise SystemExit('Error: L1 table tail is not zero — refusing')
        f.seek(24)
        f.write(struct.pack('>Q', new_size))
        f.seek(36)
        f.write(struct.pack('>I', need_l1))
    print(f'  qcow2 virtual size: {cur_size >> 30}G → {new_size >> 30}G '
          f'(header grow, L1 {l1_size} → {need_l1} entries) ✓')


def stage_image(work, args):
    print('\n── Stage: image (download + copy) ──')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / Path(CLOUD_IMG_URL).name
    if cached.exists():
        print(f'  Cached: {cached} ✓')
    else:
        run(['curl', '-fL', '-o', str(cached) + '.part', CLOUD_IMG_URL],
            'cloud image download')
        os.rename(str(cached) + '.part', cached)
        print(f'  Downloaded: {cached} ✓')

    disk = work / f'{args.name}.qcow2'
    if disk.exists():
        # Rerun safety: never clobber a disk a created VM may already own
        # (and may have booted). Delete the work dir to force a fresh copy.
        print(f'  Disk exists — reusing: {disk} ✓')
    else:
        shutil.copy2(cached, disk)
        print(f'  Disk ready: {disk} ✓')
    qcow2_grow(disk, (args.disk_gb * 1024) << 20)
    return disk


def stage_seed(work, args):
    print('\n── Stage: seed (cloud-init ISO) ──')
    if args.ip:
        static_ip = args.ip
        gateway = static_ip.rsplit('.', 1)[0] + '.1'
        print(f'  static IP (explicit): {static_ip}, gateway {gateway}')
    else:
        subnet = vmnet_subnet()
        static_ip = f'{subnet}.{args.host_num}'
        gateway = f'{subnet}.1'
        print(f'  vmnet subnet: {subnet}.0/24 → static IP {static_ip}')

    if args.ssh_key:
        key_file = Path(args.ssh_key).expanduser()
        if not key_file.exists():
            raise SystemExit(f'Error: --ssh-key {key_file} not found')
    else:
        pubkeys = sorted(Path.home().glob('.ssh/id_*.pub'))
        if not pubkeys:
            raise SystemExit(
                'Error: no SSH public key in ~/.ssh (ssh-keygen first, '
                'or pass --ssh-key)')
        key_file = pubkeys[0]
    ssh_key = key_file.read_text().strip()
    print(f'  SSH key: {key_file.name} ✓')
    print(f'  VM user: {args.user} uid={args.uid}'
          + (' (= your Mac UID — share files owned by you in the VM)'
             if args.uid == os.getuid() else ''))

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
write_files:
  - path: /etc/netplan/60-nico-dev-static.yaml
    permissions: '0600'
    content: |
      network:
        version: 2
        ethernets:
          nic0:
            match:
              driver: virtio_net
            dhcp4: false
            addresses: [{static_ip}/24]
            routes:
              - to: default
                via: {gateway}
            nameservers:
              addresses: [{gateway}]
runcmd:
  - netplan apply
  - systemctl enable --now qemu-guest-agent
  - systemctl enable --now docker
final_message: "nico-dev base VM ready: ssh {args.user}@{static_ip}"
''')

    iso = work / f'{args.name}-seed.iso'
    iso.unlink(missing_ok=True)      # hdiutil refuses to overwrite
    run(['hdiutil', 'makehybrid', '-iso', '-joliet',
         '-iso-volume-name', 'CIDATA', '-joliet-volume-name', 'CIDATA',
         '-o', str(iso), str(seed_dir)], 'seed ISO')
    print(f'  Seed ISO: {iso} ✓')
    return iso, static_ip


def vm_exists(name):
    utmctl = '/Applications/UTM.app/Contents/MacOS/utmctl'
    r = subprocess.run([utmctl, 'list'], capture_output=True, text=True)
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].strip() == name:
            return True
    return False


def stage_vm(disk, iso, args):
    print('\n── Stage: vm (UTM AppleScript create) ──')
    # `qemu configuration` has NO share-path property (sdef: only
    # `directory share mode`; a `directory shares` list exists only on
    # apple configuration — passing it here is the -1700 coercion error,
    # found 2026-08-28). VirtFS mode is set here; the PATH is the one
    # remaining GUI step, printed below.
    if vm_exists(args.name):
        print(f'  VM "{args.name}" already exists — skipping create ✓')
        # UTM IMPORTED (copied) the staging images into the VM bundle at
        # creation (20260828-#4) — growing the staging disk after the fact
        # does not reach the VM. Heal: patch the bundle copy (stopped only).
        bundle_disk = (Path.home() / 'Library/Containers/com.utmapp.UTM'
                       / 'Data/Documents' / f'{args.name}.utm' / 'Data'
                       / f'{args.name}.qcow2')
        if bundle_disk.exists():
            utmctl = '/Applications/UTM.app/Contents/MacOS/utmctl'
            st = subprocess.run([utmctl, 'status', args.name],
                                capture_output=True,
                                text=True).stdout.strip().lower()
            if st == 'stopped':
                print(f'  Bundle disk: {bundle_disk}')
                qcow2_grow(bundle_disk, (args.disk_gb * 1024) << 20)
            else:
                print(f'  VM is "{st}" — NOT touching the bundle disk '
                      f'(stop the VM and rerun to grow it)')
    else:
        script = f'''
tell application "UTM"
    set vm to make new virtual machine with properties ¬
        {{backend:qemu, configuration:{{name:"{args.name}", architecture:"{QEMU_ARCH}", ¬
          memory:{args.mem_mb}, cpu cores:{args.cpus}, ¬
          directory share mode:VirtFS, ¬
          displays:{{{{hardware:"virtio-gpu-pci"}}}}, ¬
          serial ports:{{{{interface:ptty}}}}, ¬
          drives:{{{{removable:false, source:POSIX file "{disk}"}}, ¬
                   {{removable:false, source:POSIX file "{iso}"}}}}}}}}
    get name of vm
end tell'''
        r = run(['osascript', '-e', script], 'UTM make virtual machine',
                capture=True)
        print(f'  VM created: {r.stdout.strip()} ✓')
        print(f'  (UTM imported COPIES of disk+seed into the VM bundle — '
              f'{Path(disk).parent} is now only a rerun cache)')
    # No drive resize here: UTM scripting can't resize an existing drive
    # (guest size ignored at make-time; absent from fetched drive records;
    # -10006 on update — 20260828-#3). The disk is grown at stage image.
    print(f'''
  ONE MANUAL STEP (AppleScript cannot set the share path):
    UTM → {args.name} → Settings → Sharing → Path → Browse →
    select  {args.share}
  (Share mode is already VirtFS. Do it now — before boot — so §2's
  prepare-vm.sh finds the share. The VM works without it until then.)''')


def stage_boot(static_ip, args):
    print('\n── Stage: boot (start + wait for ssh) ──')
    utmctl = '/Applications/UTM.app/Contents/MacOS/utmctl'
    run([utmctl, 'start', args.name], 'utmctl start')
    print(f'  Waiting for cloud-init + ssh at {static_ip} '
          f'(first boot: ~3-5 min incl. package installs)...')
    deadline = time.time() + 900
    while time.time() < deadline:
        if subprocess.run(['nc', '-z', '-w', '2', static_ip, '22'],
                          capture_output=True).returncode == 0:
            print(f'  ssh is up ✓')
            break
        time.sleep(10)
    else:
        raise SystemExit(f'Error: ssh not reachable at {static_ip} after 15 min '
                         f'— check the UTM console (utmctl attach {args.name})')
    print(f'''
{'=' * 60}
  Base VM ready:  ssh {args.user}@{static_ip}   (key auth, or the password)
  Next (advanced-user path, how-to §2 onward):
    create-dev-site.py → deploy-dev-fabric.py → deploy-dev-cp.py → ...
{'=' * 60}''')


def main():
    p = argparse.ArgumentParser(
        description='Build a nico-dev base VM from the Ubuntu cloud image',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--name', required=True, help='VM name (e.g. nico-dev-c5)')
    p.add_argument('--share', default=str(Path.home()),
                   help='Mac folder for the VirtFS share (default: your home)')
    p.add_argument('--host-num', type=int, default=DEFAULT_HOST_NUM,
                   help=f'last octet of the static IP (default {DEFAULT_HOST_NUM})')
    p.add_argument('--cpus', type=int, default=DEF_CPUS,
                   help=f'VM CPU cores (default {DEF_CPUS})')
    p.add_argument('--mem-mb', type=int, default=DEF_MEM_MB,
                   help=f'VM memory in MB (default {DEF_MEM_MB}; measured working '
                        'set ~5G — 8192 is enough to RUN the sim)')
    p.add_argument('--disk-gb', type=int, default=DEF_DISK_GB,
                   help=f'VM disk virtual size in GB, sparse '
                        f'(default {DEF_DISK_GB})')
    p.add_argument('--ip', default=None,
                   help='full static IP, overriding vmnet detection + '
                        '--host-num. MUST be on this Mac\'s UTM shared-'
                        'network subnet (usually 192.168.64.x — check what '
                        'DHCP gives a UTM VM); gateway/DNS assumed at .1 '
                        'of its /24')
    p.add_argument('--user', default='nico',
                   help='initial VM user (default: nico)')
    p.add_argument('--uid', type=int, default=os.getuid(),
                   help='UID for the VM user (default: YOUR Mac UID, '
                        f'{os.getuid()} — so VirtFS share files are owned '
                        'by you on both sides; pass 1000 for the Ubuntu '
                        'classic)')
    p.add_argument('--password', default='Welcome123!',
                   help='initial VM user password (default: Welcome123!)')
    p.add_argument('--ssh-key', default=None,
                   help='public key file to authorize (default: first '
                        '~/.ssh/id_*.pub found)')
    p.add_argument('--work-dir', default=None,
                   help='where the VM disk lives (default: ~/UTM-disks/<name>)')
    p.add_argument('--stage', choices=['image', 'seed', 'vm', 'boot'],
                   default='boot', help='stop after this stage')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    work = Path(args.work_dir or Path.home() / 'UTM-disks' / args.name)
    print('nico-dev — Build base VM')
    print(f'  name     : {args.name}')
    print(f'  ubuntu   : {UBUNTU_RELEASE} cloud image ({IMG_ARCH})')
    print(f'  disk     : {args.disk_gb}G sparse, {args.cpus} cpus, {args.mem_mb}MB')
    print(f'  share    : {args.share}')
    print(f'  user     : {args.user}')
    print(f'  work dir : {work}')
    if args.dry_run:
        print('\n(dry run — nothing executed)')
        return
    work.mkdir(parents=True, exist_ok=True)

    disk = stage_image(work, args)
    if args.stage == 'image':
        return
    iso, static_ip = stage_seed(work, args)
    if args.stage == 'seed':
        return
    stage_vm(disk, iso, args)
    if args.stage == 'vm':
        return
    if sys.stdin.isatty():
        input('\n  Set the share Path now (VM is stopped), then press '
              'Enter to boot... ')
    stage_boot(static_ip, args)


if __name__ == '__main__':
    main()
