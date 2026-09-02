#!/usr/bin/env python3
"""
nico-dev — One-command bring-up, LINUX HOST edition (libvirt/KVM).

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

Interactive moments:
  prep  — maybe the VM password once (usually not: key pre-authorized)
  route — host sudo password (ip route)
(No GUI step on Linux — the share path is set by virt-install.)
"""

import argparse
import datetime
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

NICO_DEV = Path(__file__).resolve().parent
# When invoked via the platform dispatcher, show ITS name in hints.
ENTRY = os.environ.get('NICO_DEV_ENTRY', sys.argv[0])

# Output styling — colour only on a terminal; NO_COLOR / FORCE_COLOR honoured.
_TTY = ((sys.stdout.isatty() or os.environ.get('FORCE_COLOR'))
        and not os.environ.get('NO_COLOR'))


def paint(code, s):
    return f'\033[{code}m{s}\033[0m' if _TTY else s


def green(s): return paint('32', s)
def red(s): return paint('31', s)
def yellow(s): return paint('33', s)
def bold(s): return paint('1', s)

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
    # Pin the identity: bare ssh offers EVERY agent key; sshd disconnects
    # after MaxAuthTries rejections before the right key gets a turn
    # ("Too many authentication failures", exit 255) — the prepare-vm.sh
    # lesson, relearned on the dev-up maiden run.
    ident = []
    if args.ssh_key:
        priv = str(Path(args.ssh_key).expanduser()).removesuffix('.pub')
        ident = ['-i', priv, '-o', 'IdentitiesOnly=yes']
    return ['ssh', '-o', 'StrictHostKeyChecking=accept-new'] + ident + [
            f'{args.user}@{args.ip}', remote_cmd]


def preflight(args):
    """Cheap host-side validation of resolved options — runs before ANY
    step (and in --dry-run), so bad values fail here, not mid-run.
    Returns [(ok, text)] — EVERY check, pass or fail, so the dry-run
    shows what was verified, not just what broke."""
    checks = []

    def check(ok, good, bad):
        checks.append((bool(ok), good if ok else bad))

    if args.ssh_key:
        check(Path(args.ssh_key).expanduser().exists(),
              f'ssh key {args.ssh_key}',
              f'ssh_key file not found: {args.ssh_key}')
    check(Path(args.share).expanduser().is_dir(),
          f'share folder {args.share}',
          f'share folder does not exist: {args.share}')
    if args.ngc_tag:
        check(os.environ.get(args.token_env),
              f'NGC API key present in env var {args.token_env}',
              f'env var {args.token_env} is empty or unset (the NGC API key)')
        img = args.ngc_image or os.environ.get('NICO_NGC_IMAGE')
        check(img, f'NGC image {img}',
              'no NGC image: set ngc.nico_image in the config '
              'or export NICO_NGC_IMAGE')
        check(subprocess.run(['docker', 'info'],
                             capture_output=True).returncode == 0,
              'docker daemon reachable (the ngc step retags via the host registry)',
              'docker daemon not reachable (the ngc step needs '
              'the host registry): sudo systemctl start docker; '
              'sudo usermod -aG docker $USER')
    check(subprocess.run(['virsh', '-c', 'qemu:///system', 'list'],
                         capture_output=True).returncode == 0,
          'libvirt reachable without sudo (qemu:///system)',
          'libvirt not reachable without sudo (qemu:///system): '
          'sudo usermod -aG libvirt $USER, then relogin')
    missing = [t for t in ('virt-install', 'cloud-localds', 'qemu-img')
               if not shutil.which(t)]
    check(not missing, 'virt-install, cloud-localds, qemu-img present',
          f'not found: {", ".join(missing)} — check-prereqs.sh names the apt package')
    return checks


def octet_warnings(args):
    """Non-fatal: does this host already route the chosen octets somewhere?
    A corp VPN or LAN using <underlay>.x/<overlay>.x means the site VIPs
    become unreachable (or worse, you talk to something real). Probes one
    representative address per prefix; a specific (non-default) route that
    is NOT our own VM route is a clash."""
    warns = []
    dflt = subprocess.run(['ip', 'route', 'show', 'default'],
                          capture_output=True, text=True).stdout
    default_gw = dflt.split('via')[1].split()[0] if 'via' in dflt else ''
    for kind, probe in (('underlay', f'{args.underlay}.133.1.17'),
                        ('overlay', f'{args.overlay}.150.0.1')):
        out = subprocess.run(['ip', 'route', 'get', probe],
                             capture_output=True, text=True).stdout
        gw = out.split('via')[1].split()[0] if 'via' in out else ''
        # our own VM route or the plain default gateway = no clash;
        # anything else (another gateway, or directly connected) = clash
        if gw and gw in (args.ip, default_gw):
            continue
        if out.strip():
            warns.append(
                f'{kind} octet {getattr(args, kind)}: this host already '
                f'routes {probe} ({" ".join(out.split()[:5])}) — likely a '
                f'VPN/LAN clash; consider a different --{kind}')
    return warns


def stale_known_hosts(ip):
    """Hosts (the IP + any /etc/hosts names for it) with known_hosts
    entries. When the run CREATES the VM, any such entry is guaranteed
    stale (new VM = new host key) — the classic silent time-sink."""
    hosts = {ip}
    try:
        for line in Path('/etc/hosts').read_text().splitlines():
            parts = line.split('#')[0].split()
            if parts and parts[0] == ip:
                hosts.update(parts[1:])
    except OSError:
        pass
    return sorted(h for h in hosts if subprocess.run(
        ['ssh-keygen', '-F', h], capture_output=True).returncode == 0)


def build_steps(args):
    """Returns [(key, where, description, [commands], recovery)]."""
    share = str(Path(args.share).expanduser())
    site_vm = f'/home/{args.user}/mac/sites/{args.dc}/{args.site}'   # bindfs view
    site_mac = f'{share}/sites/{args.dc}/{args.site}'
    ndev_vm = f'/home/{args.user}/mac/{args.nico_dev_rel}'
    vip_net = f'{args.underlay}.133.1.0/27'

    steps = [
        ('vm', 'Host', 'Build the base VM (cloud image + cloud-init)',
         [[sys.executable, NICO_DEV / 'build-nico-dev-vm.py',
           '--name', args.name, '--share', share, '--user', args.user,
           '--password', args.password]
          + (['--ssh-key', args.ssh_key] if args.ssh_key else [])
          + (['--uid', str(args.uid)] if args.uid is not None else [])
          + (['--host-num', str(args.host_num)] if args.host_num else [])
          + (['--cpus', str(args.vm_cpus)] if args.vm_cpus else [])
          + (['--mem-mb', str(args.vm_mem_mb)] if args.vm_mem_mb else [])
          + (['--disk-gb', str(args.vm_disk_gb)] if args.vm_disk_gb else [])
          + (['--ip', args.ip_explicit] if args.ip_explicit else [])
          + ['--dc', args.dc, '--site', args.site]],
         'The vm step is rerun-safe (existing volumes/domain reused).\n'
         'ssh timeout → console: virsh console ' + args.name + ' (Ctrl-] exits).'),

        ('prep', 'Host', 'Prepare the VM (mounts, tools, ssh keys)',
         [['bash', NICO_DEV / 'prepare-vm.sh', 'init',
           '--vm-ip', args.ip, '--vm-user', args.user,
           '--share', args.share_name]
          + (['--ssh-key', args.ssh_key] if args.ssh_key else [])],
         'Check plain `ssh {u}@{ip}` works and the virtiofs share is\n'
         'attached (virsh dumpxml ' + args.name + ' | grep filesystem). how-to §2.'
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

        ('build', 'Host', 'Build nico images → local registry',
         [[sys.executable, NICO_DEV / 'build-dev-nico-mac.py',
           site_mac, '--tag', args.tag]],
         'Needs the docker daemon and DISK: the builder cache grows fast —\n'
         '`docker builder prune -af`. First build 20-40 min, later 2-5 min.'),

        ('registry', 'VM', 'Verify registry reachable from the VM',
         [vm_ssh(args, f'python3 {ndev_vm}/ndev.py {site_vm} registry verify')],
         'If containerd shows ✗: the config_path fix in how-to §7.'),

        ('nico', 'VM', 'Deploy the nico stack',
         [vm_ssh(args, f'sudo python3 {ndev_vm}/deploy-dev-nico.py'
                       f' {site_vm} --tag {args.tag}')],
         'helm --wait timeouts: rerun (idempotent). Pods Running but VIP\n'
         'refused: kubectl rollout restart deployment/nico-api -n nico-system\n'
         '(20260826-#7). how-to §8.'),

        ('route', 'Host', 'Route the service VIPs + final status',
         [['sudo', 'ip', 'route', 'replace', vip_net, 'via', args.ip],
          [sys.executable, NICO_DEV / 'ndev.py', site_mac]],
         f'Route not persistent across host reboots — re-add with\n'
         f'sudo ip route replace {vip_net} via {args.ip}. ndev shows status.'),
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
        'ngc', 'Host', 'Deploy pre-built image from NGC (no source build)',
        [ngc_cmd],
        'Checks: the key in $' + args.token_env + ' needs registry-read on '
        'the image\'s org/team; the tag must exist for the host arch\n'
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
                        'host UID)')
    p.add_argument('--vm-cpus', type=int, default=None,
                   help='VM CPU cores (vm step passthrough; default 6)')
    p.add_argument('--vm-mem-mb', type=int, default=None,
                   help='VM memory MB (vm step passthrough; default 12288 — '
                        'a ceiling; measured working set ~5G, 8192 runs it)')
    p.add_argument('--vm-disk-gb', type=int, default=None,
                   help='VM disk GB, sparse (vm step passthrough; default 120)')
    p.add_argument('--host-num', type=int, default=None,
                   help='last octet of the VM IP (vm step passthrough; '
                        'default 126; distinct per VM on this host)')
    p.add_argument('--share', default=DEF_SHARE,
                   help='host folder shared into the VM (default: derived '
                        f'from this script\'s location: {DEF_SHARE})')
    p.add_argument('--share-name', default='share',
                   help='virtiofs mount tag (the builder tags it "share")')
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
    p.add_argument('--step-delay', type=int, default=10, metavar='SECS',
                   help='settle pause between steps (default 10; 0 disables) '
                        '— nico-sim lesson: give bridges/DHCP/pods a beat')
    p.add_argument('--retries', type=int, default=1, metavar='N',
                   help='retry a failed step command N times, 15s apart, '
                        'before giving up (default 1; steps are idempotent)')
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
        # Config principle (Jasmeer, 2026-09-01): related some_xxx options
        # live under a `some:` group in the yaml. Groups expand to the
        # flat option names here; add new families to this table.
        groups = {
            'ngc': {'nico_tag': 'ngc_tag', 'nico_image': 'ngc_image',
                    'token_env': 'token_env'},
            'vm': {'cpus': 'vm_cpus', 'mem_mb': 'vm_mem_mb',
                   'disk_gb': 'vm_disk_gb'},
        }
        for gname, sub in groups.items():
            g = cfg.pop(gname, None)
            if g is None:
                continue
            if not isinstance(g, dict) or set(g) - set(sub):
                raise SystemExit(f'Error: {gname}: in {cfg_path} must be a '
                                 f'map with keys: {", ".join(sub)}')
            cfg.update({sub[k]: v for k, v in g.items()})
        if 'ngc_tag' in cfg and 'tag' in cfg:
            raise SystemExit(f'Error: {cfg_path} sets BOTH deploy modes '
                             f'— `tag:` (source build) and `ngc:` '
                             f'(pre-built). Choose one.')
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

    mode = (f'NGC pre-built, tag {args.ngc_tag}' if args.ngc_tag
            else f'source build, tag {args.tag}')
    print(bold(f'nico-dev — bring-up: {args.name} @ {args.ip}'))
    print(f'  site {args.dc}/{args.site} (underlay {args.underlay}, '
          f'overlay {args.overlay}) — {mode}')

    # ── Preflight: every check listed, ✓ or ✗; warnings ⚠ ──────────────
    print(f'\n{bold("Preflight")}')
    checks = preflight(args)
    for ok, text in checks:
        print(f'  {green("✓") if ok else red("✗")} {text}')
    warns = octet_warnings(args)
    for w in warns:
        print(f'  {yellow("⚠")} {w}')
    if not warns:
        print(f'  {green("✓")} octets {args.underlay}/{args.overlay} not '
              f'already routed on this host')
    # Fresh-VM runs: pre-existing known_hosts entries for the VM's IP (or
    # its /etc/hosts names) are guaranteed stale — scrub instead of letting
    # step ssh die on a host-key mismatch mid-run.
    failed = sum(1 for ok, _ in checks if not ok)
    if keys[start] == 'vm':
        stale = stale_known_hosts(args.ip)
        if stale:
            scrub = not args.dry_run and not failed     # only if we'll proceed
            verb = 'removing' if scrub else 'would remove'
            print(f'  {yellow("⚠")} stale known_hosts entries for '
                  f'{", ".join(stale)} — {verb} (a NEW VM gets a new host key)')
            if scrub:
                for h in stale:
                    subprocess.run(['ssh-keygen', '-R', h], capture_output=True)
        else:
            print(f'  {green("✓")} no stale known_hosts entries for {args.ip}')
    if failed and not args.dry_run:
        raise SystemExit(red(f'\n✗ {failed} preflight problem(s) — fix the '
                             f'✗ lines above and rerun.'))

    # ── Plan ───────────────────────────────────────────────────────────
    plan = steps[start:stop + 1]
    print(f'\n{bold("Plan")} — {len(plan)} steps: '
          f'{" → ".join(keys[start:stop + 1])}')

    if args.dry_run:
        for key, where, desc, cmds, _ in plan:
            n = keys.index(key) + 1
            print(f'\n{bold(f"{n}/{len(keys)} {key}")} [{where}] — {desc}')
            for cmd in cmds:
                print(f'    $ {" ".join(shlex.quote(str(c)) for c in cmd)}')
        print('\n' + '━' * 64)
        if failed:
            print(red(f'✗ NOT READY — {failed} preflight problem(s); fix the '
                      f'✗ lines above, then rerun --dry-run.'))
            raise SystemExit(1)
        note = f' ({len(warns)} warning(s) above — read them)' if warns else ''
        go = ' '.join(shlex.quote(a) for a in
                      [ENTRY] + [a for a in sys.argv[1:] if a != '--dry-run'])
        print(green(f'✓ READY — preflight passed, {len(plan)} steps planned'
                    f'{note}. Nothing was executed. Go:'))
        print(f'    {go}')
        return

    first = True
    for key, where, desc, cmds, recovery in steps[start:stop + 1]:
        if not first and args.step_delay > 0:
            print(f'  (settling {args.step_delay}s before the next step...)')
            time.sleep(args.step_delay)
        first = False
        n = keys.index(key) + 1
        print(f'\n{bold(f"━━ Step {n}/{len(keys)}: {key}")} [{where}] — {desc} ━━')
        for cmd in cmds:
            rc = sh(cmd)
            attempt = 0
            while rc != 0 and attempt < args.retries:
                attempt += 1
                print(f'  ✗ exit {rc} — retrying in 15s '
                      f'({attempt}/{args.retries}; steps are idempotent)...')
                time.sleep(15)
                rc = sh(cmd)
            if rc != 0:
                if args.config:
                    resume = f'{ENTRY} --config {args.config} --from {key}'
                else:
                    resume = (f'{ENTRY} --name {args.name} --from {key}'
                              + (f' --tag {args.tag}'
                                 if key in ('build', 'nico') else ''))
                hdr = red(f'✗ Step "{key}" failed (exit {rc}).')
                print(f'''
{hdr}

Recovery:
{recovery}

Then resume with:
  {resume}''', file=sys.stderr)
                raise SystemExit(1)
        print()

    print('=' * 60)
    print(green(f'  ✓ Done. GUI: https://{args.underlay}.133.1.17/admin'))
    print(f'  KUBECONFIG=%s/sites/{args.dc}/{args.site}/{args.dc}-{args.site}.kubeconfig.yaml'
          % str(Path(args.share).expanduser()))
    print('=' * 60)


if __name__ == '__main__':
    main()
