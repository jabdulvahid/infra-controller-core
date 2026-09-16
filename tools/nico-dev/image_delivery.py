#!/usr/bin/env python3
"""
nico-dev — deliver container images to the VM's containerd through the SHARE,
so the VM never pulls them through the registry tunnel.

Why (2026-09-16 fresh bring-up, root cause of the recurring "pull stuck,
restart containerd" ritual): the VM reaches the Mac's registry at
192.168.64.1:5000 through lima's ssh port forwarder (colima). That tunnel
stalls — the registry sees no request while containerd reports Pulling for
ten minutes — and containerd sits on the half-open connection until its
watchdog fires. Every chart nico-dev deploys uses imagePullPolicy
IfNotPresent, so once an image is IN containerd, kubelet never contacts the
registry for it. This module puts it there without the tunnel: the host
writes `docker save` output into the site folder (the share), the VM runs
`ctr -n k8s.io images import` on it, and the tarball is removed. Same
channel MAT's binary already uses; deterministic; works for both lanes.

The registry stays where it is: docker's own store on the host, and what the
scripts query for tag lists. The VM just stops pulling from it.

  image_delivery.py <site> <ref>...        # manual delivery of VM-facing refs,
                                           #   e.g. 192.168.64.1:5000/nico:ngc-v2.3.0-…
  image_delivery.py <site> --check <ref>   # is it in the VM's containerd?

The VM is reached over ssh from the host using the `vm:` block of the site
yaml (ip, user, ssh_key — recorded by create-dev-site.py from bring-up.py);
older site yamls fall back to nico-dev's conventions (192.168.64.126, the
user in nico_vm_folder, the default ssh identity).
"""

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed', file=sys.stderr)
    sys.exit(1)

CTR_NS = 'k8s.io'


# ── the VM, from the site yaml ───────────────────────────────────────────────
def vm_info(cfg):
    """(ip, user, private_key_path_or_None). Fallbacks for site yamls that
    predate the vm: block."""
    vm = cfg.get('vm') or {}
    user = vm.get('user')
    if not user:
        parts = Path(cfg.get('nico_vm_folder', '/home/nico/mac')).parts
        user = parts[2] if len(parts) > 2 and parts[1] == 'home' else 'nico'
    ip = vm.get('ip')
    if not ip:
        host = (cfg.get('registry') or {}).get('host', '192.168.64.1')   # the Mac as the VM sees it
        ip = '.'.join(host.split('.')[:3] + ['126'])
    key = vm.get('ssh_key') or None
    if key:
        key = str(Path(key).expanduser())
        if key.endswith('.pub'):
            key = key[:-4]
    return ip, user, key


def on_vm(site_folder, cfg):
    """Is this process on the VM? The site folder lies under exactly one of the
    two share views recorded in the site yaml (same rule as collectors/site.py)."""
    sf = os.path.realpath(str(site_folder)) + '/'
    for key, answer in (('nico_vm_folder', True), ('nico_mac_folder', False)):
        root = cfg.get(key)
        if root and sf.startswith(os.path.realpath(os.path.expanduser(root)).rstrip('/') + '/'):
            return answer
    return sys.platform != 'darwin'


def vm_path(host_path, cfg):
    """Map a host-side path under nico_mac_folder to the VM-side path under nico_vm_folder."""
    mac_root = os.path.realpath(os.path.expanduser(cfg['nico_mac_folder'])).rstrip('/')
    hp = os.path.realpath(str(host_path))
    if not hp.startswith(mac_root + '/'):
        sys.exit(f'Error: {host_path} is not under the share ({mac_root}); the VM cannot see it')
    return cfg['nico_vm_folder'].rstrip('/') + hp[len(mac_root):]


def ssh_argv(cfg, remote_cmd):
    ip, user, key = vm_info(cfg)
    argv = ['ssh', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=15']
    if key:
        argv += ['-i', key, '-o', 'IdentitiesOnly=yes']   # bare ssh offers every agent key (MaxAuthTries)
    return argv + [f'{user}@{ip}', remote_cmd]


def run_on_vm(cfg, site_folder, remote_cmd, check=True, capture=True):
    """Run a command on the VM — directly when we are the VM, over ssh from the host."""
    if on_vm(site_folder, cfg):
        r = subprocess.run(['bash', '-c', remote_cmd], capture_output=capture, text=True)
    else:
        r = subprocess.run(ssh_argv(cfg, remote_cmd), capture_output=capture, text=True)
    if check and r.returncode != 0:
        print(f'  ! on the VM: {remote_cmd[:120]}\n    {(r.stderr or r.stdout or "").strip()[:500]}', file=sys.stderr)
        sys.exit(1)
    return r


# ── delivery ─────────────────────────────────────────────────────────────────
def _docker(args, check=True, capture=True):
    r = subprocess.run(['docker'] + args, capture_output=capture, text=True)
    if check and r.returncode != 0:
        print(f'  ! docker {" ".join(args)[:100]}: {(r.stderr or r.stdout or "").strip()[:400]}', file=sys.stderr)
        sys.exit(1)
    return r


def ensure_local_refs(refs, push_reg):
    """Make sure docker holds every VM-facing ref (<registry-as-VM-sees-it>/name:tag).
    buildx --push leaves nothing in the local store, and the NGC lane tags the
    localhost name: pull from the local registry and/or retag as needed."""
    for ref in refs:
        if _docker(['image', 'inspect', ref], check=False).returncode == 0:
            continue
        name_tag = ref.split('/', 1)[1]                    # name:tag
        local = f'{push_reg}/{name_tag}'
        if _docker(['image', 'inspect', local], check=False).returncode != 0:
            print(f'  pulling {local} from the local registry (buildx --push keeps no local copy)')
            _docker(['pull', local], capture=False)
        _docker(['tag', local, ref])


def vm_has(cfg, site_folder, refs):
    """Which of refs are present in the VM's containerd (k8s.io namespace)."""
    r = run_on_vm(cfg, site_folder, f'sudo ctr -n {CTR_NS} images ls -q', check=False)
    have = set(r.stdout.split()) if r.returncode == 0 else set()
    return [ref for ref in refs if ref in have]


def deliver(cfg, site_folder, refs, label, push_reg=None, dry_run=False, keep_tar=False):
    """Export refs from docker into <site>/images/<label>.tar on the share and
    import them into the VM's containerd. Skips refs already present.
    Returns the list of refs imported."""
    site_folder = Path(site_folder).expanduser()
    if on_vm(site_folder, cfg):
        sys.exit('Error: image delivery must run on the host (docker lives there)')
    push_reg = push_reg or f'localhost:{(cfg.get("registry") or {}).get("port", 5000)}'
    present = vm_has(cfg, site_folder, refs) if not dry_run else []
    todo = [r for r in refs if r not in present]
    if present:
        print(f'  already in the VM\'s containerd: {len(present)} of {len(refs)} image(s) — skipping those')
    if not todo:
        return []
    tar_host = site_folder / 'images' / f'{label}.tar'
    tar_vm = vm_path(tar_host, cfg)
    ip, user, _ = vm_info(cfg)
    print(f'\nDelivering {len(todo)} image(s) to the VM through the share (not the registry tunnel)')
    for r in todo:
        print(f'  {r}')
    if dry_run:
        print(f'  (dry run) docker save -o {tar_host} …; ssh {user}@{ip} sudo ctr -n {CTR_NS} images import {tar_vm}; rm {tar_host}')
        return todo
    ensure_local_refs(todo, push_reg)
    tar_host.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    _docker(['save', '-o', str(tar_host)] + todo, capture=False)
    size_gb = tar_host.stat().st_size / 1024**3
    print(f'  exported {size_gb:.1f} GB to {tar_host.name} in {time.time() - t0:.0f}s')
    t1 = time.time()
    # ctr import: no progress watchdog, no tunnel — the share is the transport
    run_on_vm(cfg, site_folder, f'sudo ctr -n {CTR_NS} images import {shlex.quote(tar_vm)}', capture=False)
    print(f'  imported into containerd ({CTR_NS}) in {time.time() - t1:.0f}s')
    missing = [r for r in todo if r not in vm_has(cfg, site_folder, todo)]
    if missing:
        sys.exit(f'Error: not in the VM\'s containerd after import: {", ".join(missing)}')
    if not keep_tar:
        tar_host.unlink(missing_ok=True)
    print(f'  {len(todo)} image(s) present in the VM ✓')
    return todo


# ── CLI ──────────────────────────────────────────────────────────────────────
def _load_site(arg):
    p = Path(arg).expanduser()
    folder = p if p.is_dir() else p.parent
    yamls = [f for f in folder.glob('*.yaml') if '.kubeconfig' not in f.name]
    if len(yamls) != 1:
        sys.exit(f'Error: expected exactly one site yaml in {folder}, found {len(yamls)}')
    return yaml.safe_load(yamls[0].read_text()) or {}, folder


def main():
    p = argparse.ArgumentParser(description='Deliver images to the VM through the share',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('site', help='site folder or site yaml')
    p.add_argument('refs', nargs='+', help='VM-facing image refs, e.g. 192.168.64.1:5000/nico:ngc-…')
    p.add_argument('--check', action='store_true', help='only report which refs the VM already has')
    p.add_argument('--label', default=None, help='tarball name (default: derived from the first ref)')
    p.add_argument('--keep-tar', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    cfg, folder = _load_site(a.site)
    if a.check:
        have = set(vm_has(cfg, folder, a.refs))
        for r in a.refs:
            print(f'  {"✓" if r in have else "✗"} {r}')
        sys.exit(0 if have == set(a.refs) else 1)
    label = a.label or a.refs[0].rsplit(':', 1)[-1]
    deliver(cfg, folder, a.refs, label, dry_run=a.dry_run, keep_tar=a.keep_tar)


if __name__ == '__main__':
    main()
