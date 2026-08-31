#!/usr/bin/env python3
"""
nico-dev — One-command bring-up: empty Mac → running nico cluster.

A RUNNER only: it calls the existing unit scripts in order and adds nothing
else. Each unit stays independently runnable (how-to §1-§9 unchanged);
this encodes the order, the argument plumbing between steps, and the
recovery story.

  ./dev-up.py --name nico-dev-c5                    # the whole chain
  ./dev-up.py --name x --from build                 # resume after a failure
  ./dev-up.py --name x --until fabric               # stop early
  ./dev-up.py --list                                # show the steps

Steps: vm → prep → site → fabric → cp → build → registry → nico → route.
On failure: prints that step's known failure modes + the exact resume
command, and exits. Reruns are safe — every unit script is idempotent or
self-healing (see issues.md 20260828-#2..#4 for the vm step).

Interactive moments (by design, not accident):
  vm    — UTM share Path is not scriptable: one GUI step + Enter
  prep  — the VM password, once (before key auth is installed)
  route — Mac sudo password
"""

import argparse
import datetime
import shlex
import subprocess
import sys
from pathlib import Path

NICO_DEV = Path(__file__).resolve().parent

# Self-locating defaults: when this script lives inside the share tree,
# every layout flag is derivable from its own path.
#   <share>/<repo>/tools/nico-dev  → fork-branch layout (repo = source too)
#   <share>/<anything>/nico-dev    → side-by-side layout (repo separate)
if NICO_DEV.parent.name == 'tools':
    DEF_SHARE = str(NICO_DEV.parents[2])
    DEF_REPO = NICO_DEV.parents[1].name
else:
    DEF_SHARE = str(NICO_DEV.parents[1])
    DEF_REPO = 'infra-controller-core'
DEF_REL = str(NICO_DEV.relative_to(DEF_SHARE))


def sh(cmd, interactive=True):
    """Run, streaming output; return exit code. cmd is a list."""
    print(f'  $ {" ".join(shlex.quote(str(c)) for c in cmd)}')
    return subprocess.run([str(c) for c in cmd]).returncode


def vm_ssh(args, remote_cmd):
    return ['ssh', '-o', 'StrictHostKeyChecking=accept-new',
            f'{args.user}@{args.ip}', remote_cmd]


def build_steps(args):
    """Returns [(key, where, description, [commands], recovery)]."""
    share = str(Path(args.share).expanduser())
    site_vm = f'/home/{args.user}/mac/sites/{args.site}'      # bindfs view
    site_mac = f'{share}/sites/{args.site}'
    ndev_vm = f'/home/{args.user}/mac/{args.nico_dev_rel}'
    vip_net = f'{args.underlay}.133.1.0/27'

    steps = [
        ('vm', 'Mac', 'Build the base VM (cloud image + cloud-init)',
         [[sys.executable, NICO_DEV / 'build-nico-dev-vm.py',
           '--name', args.name, '--share', share, '--user', args.user,
           '--password', args.password]
          + (['--ssh-key', args.ssh_key] if args.ssh_key else [])
          + (['--uid', str(args.uid)] if args.uid is not None else [])
          + (['--host-num', str(args.host_num)] if args.host_num else [])
          + (['--ip', args.ip_explicit] if args.ip_explicit else [])],
         'The vm step is rerun-safe and self-healing (issues 20260828-#2..#4).\n'
         'Common: share Path not set in UTM (GUI step), ssh timeout →\n'
         'utmctl attach ' + args.name + ' for the serial console.'),

        ('prep', 'Mac', 'Prepare the VM (mounts, tools, ssh keys)',
         [['bash', NICO_DEV / 'prepare-vm.sh', 'init',
           '--vm-ip', args.ip, '--vm-user', args.user,
           '--share', args.share_name]],
         'Check plain `ssh {u}@{ip}` works (password auth) and the share\n'
         'is attached in UTM (Settings → Sharing: VirtFS + Path). how-to §2.'
         .format(u=args.user, ip=args.ip)),

        ('site', 'VM', 'Create the site config',
         [vm_ssh(args,
                 f'python3 {ndev_vm}/create-dev-site.py'
                 f' --dc-name {args.dc} --site-name {args.site}'
                 f' --underlay {args.underlay} --overlay {args.overlay}'
                 f' --folder {site_vm}'
                 f' --nico-vm-folder /home/{args.user}/mac'
                 f' --nico-mac-folder {share}'
                 f' --nico-repo-folder {args.repo}'
                 f' --nico-dev-folder {args.nico_dev_rel.removesuffix("/nico-dev")}/nico-dev')],
         f'Inspect {site_mac}/{args.site}.yaml — all later steps read it. how-to §3.'),

        ('fabric', 'VM', 'Deploy the ContainerLab fabric + verify',
         [vm_ssh(args, f'sudo python3 {ndev_vm}/deploy-dev-fabric.py {site_vm}'),
          vm_ssh(args, f'python3 {ndev_vm}/ndev.py {site_vm} fabric verify')],
         'ndev fabric verify names the broken piece (BGP peers, bridges,\n'
         'pings). how-to §4 + troubleshooting. Rerunning redeploys cleanly.'),

        ('cp', 'VM', 'Deploy the Kubernetes control plane',
         [vm_ssh(args, f'sudo python3 {ndev_vm}/deploy-dev-cp.py {site_vm}')],
         'kubeadm/containerd issues: how-to §5. Rerun is idempotent.'),

        ('build', 'Mac', 'Build nico images → local registry',
         [[sys.executable, NICO_DEV / 'build-dev-nico-mac.py',
           site_mac, '--tag', args.tag]],
         'Needs colima running (`colima start --cpu 4 --memory 8`) and DISK:\n'
         'the builder cache grows ~100GB/week — `docker builder prune -af`\n'
         '(20260827-#1). First build 20-40 min, later 2-5 min. how-to §6.'),

        ('registry', 'VM', 'Verify registry reachable from the VM',
         [vm_ssh(args, f'python3 {ndev_vm}/ndev.py {site_vm} registry verify')],
         'If containerd shows ✗: the config_path fix in how-to §7.'),

        ('nico', 'VM', 'Deploy the nico stack',
         [vm_ssh(args, f'sudo python3 {ndev_vm}/deploy-dev-nico.py'
                       f' {site_vm} --tag {args.tag}')],
         'helm --wait timeouts: rerun (idempotent). Pods Running but VIP\n'
         'refused: kubectl rollout restart deployment/nico-api -n nico-system\n'
         '(20260826-#7). how-to §8.'),

        ('route', 'Mac', 'Route the service VIPs + final status',
         [['sudo', 'route', '-n', 'add', '-net', vip_net, args.ip],
          [sys.executable, NICO_DEV / 'ndev.py', site_mac]],
         f'Re-add the route after reboot/VPN/all-VMs-stopped (bridge100\n'
         f'lifecycle): sudo route -n add -net {vip_net} {args.ip}. how-to §9.\n'
         f'(route "File exists" = already there — harmless; ndev shows status.)'),
    ]
    if args.ngc_tag:
        steps = apply_ngc_mode(steps, args, site_mac)
    return steps


def apply_ngc_mode(steps, args, site_mac):
    """--ngc-tag: deploy a pre-built image instead of building from source.
    Replaces build+nico with one step calling deploy-nico-from-ngc.py
    (registry-ensure → login → pull → retag → push → deploy --initial)."""
    ngc_cmd = [sys.executable, NICO_DEV / 'deploy-nico-from-ngc.py',
               site_mac, args.ngc_tag, '--token-env', args.token_env,
               '--initial']
    if args.ngc_image:
        ngc_cmd += ['--ngc-image', args.ngc_image]
    ngc_step = (
        'ngc', 'Mac', 'Deploy pre-built image from NGC (no source build)',
        [ngc_cmd],
        'Checks: the key in $' + args.token_env + ' needs registry-read on '
        'the image\'s org/team; the tag must exist as linux/arm64\n'
        '(docker manifest inspect <image>:<tag>); a 10GB image needs ~25GB '
        'free across colima+registry+VM. how-to: "Deploying pre-built\n'
        'NGC images".')
    steps = [s for s in steps if s[0] not in ('build', 'nico')]
    at = next(i for i, s in enumerate(steps) if s[0] == 'registry') + 1
    steps.insert(at, ngc_step)
    return steps


def main():
    p = argparse.ArgumentParser(
        description='One-command nico-dev bring-up (runner over the unit scripts)',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--name', help='VM name (required unless --list)')
    p.add_argument('--ip', default=None,
                   help='VM static IP (default 192.168.64.<host-num>)')
    p.add_argument('--ip-explicit', default=None, metavar='IP',
                   help='pass --ip through to build-nico-dev-vm.py '
                        '(foreign vmnet subnet); also sets --ip')
    p.add_argument('--user', default='nico')
    p.add_argument('--password', default='Welcome123!',
                   help='VM user password (vm step passthrough)')
    p.add_argument('--ssh-key', default=None,
                   help='public key file to authorize (vm step passthrough; '
                        'default: first ~/.ssh/id_*.pub)')
    p.add_argument('--uid', type=int, default=None,
                   help='VM user UID (vm step passthrough; default: your '
                        'Mac UID)')
    p.add_argument('--host-num', type=int, default=None,
                   help='last octet of the VM IP (vm step passthrough; '
                        'default 126). On a non-192.168.64 subnet use '
                        '--ip-explicit instead')
    p.add_argument('--share', default=DEF_SHARE,
                   help='Mac folder shared into the VM (default: derived '
                        f'from this script\'s location: {DEF_SHARE})')
    p.add_argument('--share-name', default='share',
                   help='VirtFS mount tag (UTM-created VMs tag it "share")')
    p.add_argument('--dc', default='dc1')
    p.add_argument('--site', default='dev')
    p.add_argument('--underlay', type=int, default=11)
    p.add_argument('--overlay', type=int, default=12)
    p.add_argument('--repo', default=DEF_REPO,
                   help='nico repo folder name inside the share '
                        f'(default: {DEF_REPO})')
    p.add_argument('--nico-dev-rel', default=DEF_REL,
                   help='nico-dev folder path relative to the share '
                        f'(default: {DEF_REL})')
    p.add_argument('--tag', default=None,
                   help='source-build image tag (default main-YYYYMMDD; '
                        'mutually exclusive with --ngc-tag)')
    p.add_argument('--ngc-tag', default=None, metavar='TAG',
                   help='deploy this pre-built NGC image tag instead of '
                        'building from source (replaces the build+nico '
                        'steps — the quickest onboarding path)')
    p.add_argument('--ngc-image', default=None, metavar='REPO',
                   help='NGC image repository nvcr.io/<org>/<team>/<image> '
                        '(default: $NICO_NGC_IMAGE)')
    p.add_argument('--token-env', default='NGC_API_KEY', metavar='VAR',
                   help='NAME of the env var holding your NGC API key '
                        '(default: NGC_API_KEY; the value is never printed)')
    p.add_argument('--config', default=None, metavar='FILE',
                   help='yaml file supplying any of these options as '
                        'defaults (keys = option names with underscores); '
                        'explicit command-line flags override the file')
    p.add_argument('--from', dest='from_step', default=None, metavar='STEP',
                   help='resume from this step')
    p.add_argument('--until', dest='until_step', default=None, metavar='STEP',
                   help='stop after this step')
    p.add_argument('--list', action='store_true', help='list steps and exit')
    p.add_argument('--dry-run', action='store_true',
                   help='print the resolved plan (steps + exact commands) '
                        'without running anything')
    # --config pre-pass: the yaml supplies DEFAULTS; explicit flags win.
    conf = argparse.ArgumentParser(add_help=False)
    conf.add_argument('--config', default=None)
    conf_args, _ = conf.parse_known_args()
    if conf_args.config:
        import yaml
        cfg_path = Path(conf_args.config).expanduser()
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        # NGC settings live in an `ngc:` group; expand to the flat options.
        ngc = cfg.pop('ngc', None)
        if ngc is not None:
            sub = {'tag': 'ngc_tag', 'image': 'ngc_image',
                   'token_env': 'token_env'}
            if not isinstance(ngc, dict) or set(ngc) - set(sub):
                raise SystemExit(f'Error: ngc: in {cfg_path} must be a map '
                                 f'with keys: {", ".join(sub)}')
            if 'tag' in cfg:
                raise SystemExit(f'Error: {cfg_path} sets BOTH deploy modes '
                                 f'— `tag:` (source build) and `ngc:` '
                                 f'(pre-built). Choose one.')
            cfg.update({sub[k]: v for k, v in ngc.items()})
        valid = {a.dest for a in p._actions}
        unknown = sorted(set(cfg) - valid)
        if unknown:
            raise SystemExit(
                f'Error: unknown key(s) in {cfg_path}: {", ".join(unknown)}\n'
                f'Valid keys: {", ".join(sorted(valid - {"help"}))}')
        p.set_defaults(**cfg)

    args = p.parse_args()
    if args.tag and args.ngc_tag:
        raise SystemExit('Error: both deploy modes set — --tag (source '
                         'build) and --ngc-tag (pre-built). Choose one.')
    if not args.tag:
        args.tag = 'main-' + datetime.date.today().strftime('%Y%m%d')
    if args.ip_explicit:
        args.ip = args.ip_explicit
    elif args.ip is None:
        args.ip = f'192.168.64.{args.host_num or 126}'

    if args.list or not args.name:
        if not args.list and not args.name:
            p.error('--name is required (or use --list)')
        dummy = args
        dummy.name = dummy.name or '<name>'
        for i, (key, where, desc, _, _r) in enumerate(build_steps(dummy), 1):
            print(f'  {i}. {key:9s} [{where:3s}] {desc}')
        return

    steps = build_steps(args)
    keys = [s[0] for s in steps]
    for flag, val in (('--from', args.from_step), ('--until', args.until_step)):
        if val and val not in keys:
            raise SystemExit(f'Error: {flag} {val} — steps are: {", ".join(keys)}')
    start = keys.index(args.from_step) if args.from_step else 0
    stop = keys.index(args.until_step) if args.until_step else len(keys) - 1

    print(f'nico-dev — bring-up: {args.name} @ {args.ip}')
    mode = (f'NGC pre-built, tag {args.ngc_tag}' if args.ngc_tag
            else f'source build, tag {args.tag}')
    print(f'  site {args.dc}/{args.site} (underlay {args.underlay}, '
          f'overlay {args.overlay}) — {mode}')
    print(f'  steps: {" → ".join(keys[start:stop + 1])}\n')

    if args.dry_run:
        for key, where, desc, cmds, _ in steps[start:stop + 1]:
            print(f'━━ {key} [{where}] — {desc}')
            for cmd in cmds:
                print(f'  $ {" ".join(shlex.quote(str(c)) for c in cmd)}')
        print('\n(dry run — nothing executed)')
        return

    for key, where, desc, cmds, recovery in steps[start:stop + 1]:
        n = keys.index(key) + 1
        print(f'━━ Step {n}/{len(keys)}: {key} [{where}] — {desc} ━━')
        for cmd in cmds:
            rc = sh(cmd)
            if rc != 0:
                resume = (f'{sys.argv[0]} --name {args.name} --from {key}'
                          + (f' --tag {args.tag}' if key in ('build', 'nico')
                             else ''))
                print(f'''
✗ Step "{key}" failed (exit {rc}).

Recovery:
{recovery}

Then resume with:
  {resume}''', file=sys.stderr)
                raise SystemExit(1)
        print()

    print('=' * 60)
    print(f'  Done. GUI: https://{args.underlay}.133.1.17/admin')
    print(f'  KUBECONFIG=%s/sites/{args.site}/{args.dc}-{args.site}.kubeconfig.yaml'
          % str(Path(args.share).expanduser()))
    print('=' * 60)


if __name__ == '__main__':
    main()
