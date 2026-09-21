#!/usr/bin/env python3
"""
nico-dev — DPF on a nico-dev site: the DPF CRDs and the dpf-sim-controller.

DPF (DOCA Platform Framework) is NICo's default DPU-provisioning path. With
`dpf: true` in bringup.yaml (the default) a nico-dev site gets three things:

  1. `[dpf] enabled = true` in the API config and the nico-api DPF RBAC —
     rendered by generate_dev_values.py from the site yaml, chart-managed.
  2. The DPF CRDs and the operator namespace, applied BEFORE nico installs —
     deploy-dev-nico.py imports ensure_dpf_prereqs() from this file. nico-api
     creates DPF resources at startup and crash-loops without the CRDs.
  3. The simulator — this script, the `dpf` step at the end of bring-up. It
     plays the DPF operator's half: watches the DPUDevice/DPUNode resources
     NICo creates and walks a DPU resource through the real phase sequence,
     including the Redfish reboot round-trip against MAT's BMC mocks. Without
     it every MAT host parks in `dpuinit` forever.

Runs on the HOST (docker for the image build, kubectl/helm via the site's
kubeconfig), like deploy-flow.py. The simulator image is not published to
NGC; it is always built here from the checkout named in the site yaml
(dev/k8s/dpf-sim-controller, Go, host arch) and pushed to the local registry.

  deploy-dpf-sim.py <site>                     # build if missing, deploy/refresh the simulator
  deploy-dpf-sim.py <site> --phase-dwell 10s   # slower phase walk (default from the site yaml, 3s)
  deploy-dpf-sim.py <site> --os-install-dwell 5m  # hold each DPU in OS Installing that long (default: phase dwell)
  deploy-dpf-sim.py <site> --rebuild           # rebuild the image even if the registry has the tag
  deploy-dpf-sim.py <site> --repo ~/w/nico-x --rebuild   # build the simulator from another checkout (feature branch)
  deploy-dpf-sim.py <site> --crds-only         # namespace + CRDs only (what deploy-dev-nico does)
  deploy-dpf-sim.py <site> --uninstall         # remove the simulator (CRDs stay)
  deploy-dpf-sim.py <site> --dry-run

Site yaml (advanced users), block nico-system.dpf:
  enabled: true|false          from bringup.yaml `dpf:`; false = legacy iPXE path, nothing below applies
  namespace: dpf-operator-system
  sim:
    image: dpf-sim-controller  name in the local registry; tag = images.tag
    phase_dwell: 3s            time per DPU phase
    os_install_dwell: 0s       time in OS Installing; 0s = same as phase_dwell (simulator --os-install-dwell)
    resources: {requests: {cpu: 100m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}

Never install the real DPF operator on a site running this simulator: both
would drive DPU.status.phase. The preflight refuses if one is found.

MAT and the two modes: MAT registers every host with DPF on, so on a DPF
site the whole fleet takes the DPF path. For an iPXE run of chosen hosts,
`run-admin-cli.sh dpf disable <host>` after discovery and before ingestion
(refused once a host was ingested via DPF). For a whole-fleet iPXE site,
bring the site up with `dpf: false`.
"""

import argparse
import copy
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed', file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
DOCKER_ARCH = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'amd64'
SIM_DIR = Path('dev/k8s/dpf-sim-controller')          # relative to the repo
CRD_DIR = Path('crates/dpf/crds')                      # relative to the repo
DEPLOYMENT = 'dpf-sim-controller'

DEFAULTS = {
    'enabled': True,
    'namespace': 'dpf-operator-system',
    'sim': {
        'image': 'dpf-sim-controller',
        'phase_dwell': '3s',
        'os_install_dwell': '0s',
        'resources': {
            'requests': {'cpu': '100m', 'memory': '128Mi'},
            'limits':   {'cpu': '500m', 'memory': '512Mi'},
        },
    },
}


# ── site yaml ────────────────────────────────────────────────────────────────
def dpf_cfg(cfg):
    """nico-system.dpf with defaults filled in. Absent block = DPF off (a site
    yaml from before the feature)."""
    block = (cfg.get('nico-system') or {}).get('dpf')
    if block is None:
        out = copy.deepcopy(DEFAULTS)
        out['enabled'] = False
        return out
    out = copy.deepcopy(DEFAULTS)
    out['enabled'] = bool(block.get('enabled', True))
    out['namespace'] = block.get('namespace') or out['namespace']
    sim = block.get('sim') or {}
    out['sim']['image'] = sim.get('image') or out['sim']['image']
    out['sim']['phase_dwell'] = str(sim.get('phase_dwell') or out['sim']['phase_dwell'])
    out['sim']['os_install_dwell'] = str(sim.get('os_install_dwell') or out['sim']['os_install_dwell'])
    res = sim.get('resources') or {}
    for k in ('requests', 'limits'):
        if res.get(k):
            out['sim']['resources'][k] = dict(res[k])
    return out


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if '.kubeconfig' not in f.name]
        if len(yamls) != 1:
            sys.exit(f'Error: expected exactly one site yaml in {p}, found {len(yamls)}')
        return str(yamls[0]), str(p)
    return str(p), str(p.parent)


def resolve_repo(cfg):
    repo_folder = cfg.get('nico_repo_folder', 'infra-controller-core')
    for root in (cfg.get('nico_mac_folder', ''), cfg.get('nico_vm_folder', '')):
        if root and (Path(root).expanduser() / repo_folder).is_dir():
            return Path(root).expanduser() / repo_folder
    return None


# ── process helpers ──────────────────────────────────────────────────────────
def run(cmd, env=None, check=True, capture=True, cwd=None, stdin=None, timeout=900):
    r = subprocess.run([str(c) for c in cmd], env={**os.environ, **(env or {})},
                       capture_output=capture, text=True, cwd=cwd, input=stdin, timeout=timeout)
    if check and r.returncode != 0:
        print(f'  ! failed: {" ".join(map(str, cmd))[:160]}\n    '
              f'{(r.stderr or r.stdout or "").strip()[:600]}', file=sys.stderr)
        sys.exit(1)
    return r


def kubectl(args, env, **kw):
    return run(['kubectl', '--request-timeout=20s'] + list(args), env=env, **kw)


# ── prerequisites: namespace + CRDs (imported by deploy-dev-nico.py) ─────────
def ensure_dpf_prereqs(repo, namespace, kubeconfig, dry_run=False):
    """Create the DPF operator namespace and apply the DPF CRDs shipped in the
    repo, then wait for them to be established. Idempotent. Must run before
    the nico release installs when [dpf] is enabled: nico-api writes DPF
    resources at startup, and the nico-api chart's DPF Role needs the
    namespace to exist. Safe to call on a site without DPF (harmless CRDs).
    Runs wherever kubectl and the repo checkout are reachable (VM or host)."""
    repo = Path(repo)
    crds = repo / CRD_DIR
    if not crds.is_dir():
        sys.exit(f'Error: DPF CRDs not found at {crds} — is the checkout complete?')
    env = {'KUBECONFIG': str(kubeconfig)}
    n = len(list(crds.glob('*.yaml')))
    print(f'  DPF prerequisites: namespace {namespace}, {n} CRDs from {CRD_DIR}')
    if dry_run:
        print(f'  (dry run) kubectl create namespace {namespace}; kubectl apply -f {crds}; '
              f'kubectl wait --for condition=established -f {crds}')
        return
    if kubectl(['get', 'namespace', namespace], env, check=False).returncode != 0:
        kubectl(['create', 'namespace', namespace], env)
    kubectl(['apply', '--server-side', '--force-conflicts', '-f', str(crds)], env, timeout=300)
    kubectl(['wait', '--for', 'condition=established', '--timeout=300s', '-f', str(crds)],
            env, timeout=330)
    print('  DPF CRDs established ✓')


# ── cluster probes ───────────────────────────────────────────────────────────
def crds_established(env):
    r = kubectl(['get', 'crd', 'dpus.provisioning.dpu.nvidia.com',
                 'dpudevices.provisioning.dpu.nvidia.com', 'dpunodes.provisioning.dpu.nvidia.com',
                 '-o', 'name'], env, check=False)
    return r.returncode == 0 and len(r.stdout.split()) == 3


def real_operator_present(env, namespace):
    """The real DPF operator and the simulator must never both run. Detect the
    operator by its deployments (name contains dpf-operator) or helm release."""
    found = []
    r = kubectl(['get', 'deployment', '-n', namespace, '-o', 'name'], env, check=False)
    found += [d for d in r.stdout.split() if 'dpf-operator' in d]
    r = run(['helm', 'list', '-n', namespace, '-q'], env=env, check=False, timeout=40)
    found += [f'helm release {x}' for x in r.stdout.split() if 'dpf-operator' in x]
    return found


def api_has_dpf(env, namespace):
    """True when nico-api runs with [dpf] enabled: at startup it writes BFB
    resources into the operator namespace and logs the SDK initialisation."""
    r = kubectl(['get', 'bfb', '-n', namespace, '-o', 'name'], env, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return True
    r = kubectl(['logs', 'deployment/nico-api', '-n', 'nico-system', '--tail=3000'], env,
                check=False, timeout=60)
    return 'Initializing DPF SDK' in (r.stdout or '')


# ── image ────────────────────────────────────────────────────────────────────
def registry_has(reg, image, tag):
    r = subprocess.run(['curl', '-sf', '-m', '5', f'http://{reg}/v2/{image}/tags/list'],
                       capture_output=True, text=True)
    try:
        return r.returncode == 0 and tag in (json.loads(r.stdout).get('tags') or [])
    except ValueError:
        return False


def sim_source_id(repo):
    """Short id of the simulator source in this checkout: the last commit that
    touched dev/k8s/dpf-sim-controller, plus '-dirty' when the tree has local
    edits there. The image tag carries it, so an image the registry already has
    can only be reused when it was built from the same source as the RBAC and
    CRDs rendered from this checkout (20260918-#3: a tag built from a newer
    tree was reused with an older checkout's Role → the simulator could not
    list DPUDeployments and every host sat in DPUInitializing/WaitingForReady)."""
    try:
        sha = subprocess.run(['git', '-C', str(repo), 'log', '-1', '--format=%h', '--', SIM_DIR],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain', '--', SIM_DIR],
                               capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        sha, dirty = '', ''
    if not sha:
        return 'nogit'
    return sha + ('-dirty' if dirty else '')


def build_image(repo, push_reg, image, tag, dry_run=False):
    sim_dir = repo / SIM_DIR
    if not (sim_dir / 'Dockerfile').is_file():
        sys.exit(f'Error: {sim_dir} not in the checkout (needs a tree with dev/k8s/dpf-sim-controller)')
    ref = f'{push_reg}/{image}:{tag}'
    print(f'\nBuilding {image}:{tag} (Go, linux/{DOCKER_ARCH}) → {push_reg}')
    if dry_run:
        print(f'  (dry run) docker buildx build --platform linux/{DOCKER_ARCH} --push -t {ref} {sim_dir}')
        return
    # Native arch build: the upstream Makefile defaults to amd64 and warns
    # about QEMU on Apple Silicon; nico-dev builds for the host arch, which is
    # what the VM runs, so no emulation is involved.
    run(['docker', 'buildx', 'build', '--platform', f'linux/{DOCKER_ARCH}', '--push',
         '-t', ref, '.'], cwd=sim_dir, capture=False, timeout=1800)


# ── manifests ────────────────────────────────────────────────────────────────
def render_manifests(repo, namespace, image_ref, phase_dwell, os_install_dwell, resources):
    """The upstream config/ rendered the way `make deploy` does (namespace,
    image, dwells), plus nico-dev's resource requests. The OS-install dwell is
    passed only when set, so a simulator built before the flag existed still
    starts (it would reject an unknown flag)."""
    sim_dir = repo / SIM_DIR
    rbac = (sim_dir / 'config' / 'rbac' / 'role.yaml').read_text().replace('dpf-operator-system', namespace)
    mgr = yaml.safe_load((sim_dir / 'config' / 'manager' / 'manager.yaml').read_text())
    mgr['metadata']['namespace'] = namespace
    c = mgr['spec']['template']['spec']['containers'][0]
    c['image'] = image_ref
    c['args'] = [a for a in c.get('args', [])
                 if not a.startswith(('--dpf-namespace=', '--phase-dwell=', '--os-install-dwell='))]
    c['args'] += [f'--dpf-namespace={namespace}', f'--phase-dwell={phase_dwell}']
    if os_install_dwell and os_install_dwell not in ('0', '0s'):
        c['args'].append(f'--os-install-dwell={os_install_dwell}')
    c['resources'] = resources
    # The image is delivered into containerd through the share (image_delivery.py);
    # with IfNotPresent kubelet never contacts the registry tunnel for it. Upstream's
    # Always + rollout-restart idiom is replaced by delivering the new content and
    # restarting (deliver() re-imports when --rebuild produced a new image).
    c['imagePullPolicy'] = 'IfNotPresent'
    docs = [d for d in yaml.safe_load_all(rbac) if d] + [mgr]
    return docs


def apply_manifests(docs, env, dry_run=False, delete=False):
    text = yaml.safe_dump_all(docs, sort_keys=False)
    verb = 'delete' if delete else 'apply'
    if dry_run:
        kinds = ', '.join(f"{d['kind']}/{d['metadata']['name']}" for d in docs)
        print(f'  (dry run) kubectl {verb} -f - <<< {kinds}')
        return
    extra = ['--ignore-not-found'] if delete else []
    kubectl([verb] + extra + ['-f', '-'], env, stdin=text)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description='DPF CRDs + dpf-sim-controller for a nico-dev site',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('site', help='site folder or site yaml')
    p.add_argument('--tag', default=None, help='image tag in the local registry (default: images.tag of the site)')
    p.add_argument('--phase-dwell', default=None, help='time per DPU phase, e.g. 3s, 30s (default: site yaml)')
    p.add_argument('--os-install-dwell', default=None,
                   help='time a DPU stays in OS Installing, e.g. 2m; 0s = same as the phase dwell (default: site yaml)')
    p.add_argument('--rebuild', action='store_true', help='rebuild the image even if the registry has the tag')
    p.add_argument('--repo', default=None, metavar='DIR',
                   help='build the simulator (and take the CRDs) from this nico checkout instead of the '
                        'site yaml one, e.g. a feature-branch worktree')
    p.add_argument('--crds-only', action='store_true', help='namespace + CRDs only, no simulator')
    p.add_argument('--uninstall', action='store_true', help='remove the simulator Deployment and RBAC (CRDs stay)')
    p.add_argument('--force', action='store_true', help='deploy even if the site yaml has dpf disabled')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    site_yaml, site_folder = resolve_site(args.site)
    cfg = yaml.safe_load(open(site_yaml))
    dpf = dpf_cfg(cfg)
    dc = cfg['fabric']['dc_name']
    sitename = (cfg.get('nico-system') or {}).get('helm-values', {}).get('sitename', 'dev')
    repo = Path(args.repo).expanduser().resolve() if args.repo else resolve_repo(cfg)
    if repo is None:
        sys.exit('Error: nico repo not reachable from here (nico_mac_folder / nico_vm_folder)')
    kubeconfig = Path(site_folder) / cfg.get('kubeconfig', f'{dc}-{sitename}.kubeconfig.yaml')
    if not kubeconfig.exists():
        sys.exit(f'Error: kubeconfig not found: {kubeconfig}')
    env = {'KUBECONFIG': str(kubeconfig)}

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location('site_images', HERE / 'site_images.py')
    site_images = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(site_images)
    img = site_images.read(cfg)
    registry = img['registry']                                   # as the VM pulls
    push_reg = f'localhost:{registry.rsplit(":", 1)[-1]}'        # as this host pushes
    tag = args.tag or (img.get('tag') if img.get('tag') not in ('', 'none', None) else None) or 'dev'
    # <site tag>-sim<source id>: see sim_source_id()
    tag = f'{tag}-sim{sim_source_id(repo)}'
    ns = dpf['namespace']
    image = dpf['sim']['image']
    dwell = args.phase_dwell or dpf['sim']['phase_dwell']
    install_dwell = args.os_install_dwell or dpf['sim']['os_install_dwell']
    image_ref = f'{registry}/{image}:{tag}'

    print('nico-dev — DPF: CRDs + dpf-sim-controller')
    print(f'  site        : {site_yaml}')
    print(f'  kubeconfig  : {kubeconfig}')
    print(f'  dpf.enabled : {dpf["enabled"]}')
    print(f'  namespace   : {ns}')
    print(f'  image       : {image_ref}  (push via {push_reg})')
    print(f'  phase dwell : {dwell}')
    print(f'  OS install  : {install_dwell}  (0s = phase dwell)')
    print(f'  resources   : {json.dumps(dpf["sim"]["resources"])}')

    if not dpf['enabled'] and not (args.uninstall or args.force):
        print('\nThis site has dpf.enabled: false (legacy iPXE path) — nothing to do.\n'
              '  To run DPF, bring the site up with `dpf: true` in bringup.yaml (the default).\n'
              '  --force deploys the simulator anyway (it idles until nico-api runs with [dpf] enabled).')
        return

    # ── uninstall ─────────────────────────────────────────────────────────
    if args.uninstall:
        print('\nRemoving the simulator (the DPF CRDs and namespace stay)…')
        docs = render_manifests(repo, ns, image_ref, dwell, install_dwell, dpf['sim']['resources'])
        apply_manifests(docs, env, dry_run=args.dry_run, delete=True)
        print('  removed ✓  Hosts reaching dpuinit will now wait forever; disable DPF in the API '
              'config too, or redeploy the simulator.')
        return

    # ── prerequisites (also the whole job under --crds-only) ──────────────
    print('\nPrerequisites')
    ensure_dpf_prereqs(repo, ns, kubeconfig, dry_run=args.dry_run)
    if args.crds_only:
        return

    # ── preflight ─────────────────────────────────────────────────────────
    print('\nPreflight')
    probs, warns = [], []
    if not args.dry_run and not crds_established(env):
        probs.append('DPF CRDs not established (dpus/dpudevices/dpunodes)')
    ops = real_operator_present(env, ns)
    if ops:
        probs.append(f'the REAL DPF operator is on this cluster ({", ".join(ops)}) — it and the '
                     f'simulator would both drive DPU.status.phase; remove one')
    if run(['docker', 'info'], check=False, timeout=30).returncode != 0:
        probs.append('docker daemon not reachable (needed to build the simulator image)')
    if not args.dry_run and not api_has_dpf(env, ns):
        warns.append('nico-api does not (yet) show DPF enabled — no BFB resources, no "Initializing DPF SDK" '
                     'in its log. If the site was just deployed, give it a minute; if it was deployed with '
                     'dpf: false, the simulator will idle and hosts take the iPXE path.')
    for w in warns:
        print(f'  ⚠ {w}')
    for pr in probs:
        print(f'  ✗ {pr}')
    if probs:
        sys.exit(1)
    print('  ✓ preflight ok')

    # ── image ─────────────────────────────────────────────────────────────
    built = False
    if args.dry_run or args.rebuild or not registry_has(push_reg, image, tag):
        build_image(repo, push_reg, image, tag, dry_run=args.dry_run)
        built = True
    else:
        print(f'\n  image {image}:{tag} already in the registry ✓  (--rebuild to rebuild)')
    # into the VM's containerd through the share, not the registry tunnel
    _spec = _ilu.spec_from_file_location('image_delivery', HERE / 'image_delivery.py')
    _idl = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_idl)
    if built and not args.dry_run and _idl.vm_has(cfg, site_folder, [image_ref]):
        # a rebuild under the same tag: containerd still holds the old content under
        # this name; ctr import replaces it, so force the delivery
        _idl.run_on_vm(cfg, site_folder, f'sudo ctr -n k8s.io images rm {image_ref}', check=False)
    _idl.deliver(cfg, site_folder, [image_ref], label=f'{image}-{tag}', push_reg=push_reg, dry_run=args.dry_run)

    # ── deploy ────────────────────────────────────────────────────────────
    print('\nDeploying the simulator')
    docs = render_manifests(repo, ns, image_ref, dwell, install_dwell, dpf['sim']['resources'])
    apply_manifests(docs, env, dry_run=args.dry_run)
    if args.dry_run:
        print('\n(dry run) done — nothing was changed')
        return
    # imagePullPolicy is Always upstream and the tag is mutable: a restart
    # guarantees a rebuilt image actually lands (same reasoning as `make deploy`).
    kubectl(['rollout', 'restart', f'deployment/{DEPLOYMENT}', '-n', ns], env)
    r = kubectl(['rollout', 'status', f'deployment/{DEPLOYMENT}', '-n', ns, '--timeout=180s'],
                env, check=False, timeout=200)
    if r.returncode != 0:
        print('Error: simulator rollout did not complete:', file=sys.stderr)
        print((r.stderr or r.stdout).strip()[:800], file=sys.stderr)
        print(f'  diagnose: kubectl -n {ns} describe pod -l app.kubernetes.io/name={DEPLOYMENT}; '
              f'kubectl -n {ns} logs deployment/{DEPLOYMENT}', file=sys.stderr)
        sys.exit(1)
    print('  dpf-sim-controller Running ✓')

    print(f'''
Done. On a MAT run, hosts pass through dpuinit as the simulator walks each DPU to Ready:
  kubectl -n {ns} get dpudevice,dpunode,dpu -w          # the resources NICo and the simulator exchange
  kubectl -n {ns} logs deployment/{DEPLOYMENT} -f       # phase walk, reboot round-trips
  <site>/run-admin-cli.sh machine show                       # hosts leaving dpuinit
iPXE for chosen hosts: <site>/run-admin-cli.sh dpf disable <host>  (before ingestion). Details: mat-in-nico-dev.md §13.''')


if __name__ == '__main__':
    main()
