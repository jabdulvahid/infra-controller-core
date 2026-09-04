#!/usr/bin/env python3
"""
nico-dev — Redeploy Nico to a new image tag.

Does a helm upgrade of the nico release only — no values regeneration,
no prereqs touched. Use after build-dev-nico.py pushes a new tag.

Reads site yaml for kubeconfig and helm dir. Runs on Mac.

Usage:
  python3 redeploy-dev-nico.py <site> --tag <tag>
  python3 redeploy-dev-nico.py ~/Mac/sites/dev --tag test1
  python3 redeploy-dev-nico.py ~/Mac/sites/dev --tag latest
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: pip install pyyaml')
    sys.exit(1)


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}: {[f.name for f in yamls]}',
                  file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def _kubectl_json(args, env):
    """kubectl ... -o json → dict, or None when kubectl fails (unreachable
    API, bad kubeconfig). Callers must treat None as 'unknown', never as
    'nothing there'."""
    import json
    r = subprocess.run(['kubectl', '--request-timeout=15s'] + args + ['-o', 'json'],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        print(f'  ! kubectl {" ".join(args)} failed: {(r.stderr or "").strip()[:200]}',
              file=sys.stderr)
        return None
    return json.loads(r.stdout)


def _millicores(q):
    """'500m' → 500, '2' → 2000, '0.5' → 500, None → 0."""
    if not q:
        return 0
    q = str(q)
    return int(q[:-1]) if q.endswith('m') else int(float(q) * 1000)


_cpu_advised = False


def cpu_sizing_advice(env, pending_pod):
    """The workaround is not the fix: a node where a surge pod cannot place is
    under-sized for rolling updates. Say so once, with numbers and the exact
    resize steps (Jasmeer, 2026-09-04: "it is imperative on us to tell them to
    increase it")."""
    global _cpu_advised
    if _cpu_advised:
        return
    nodes = (_kubectl_json(['get', 'nodes'], env) or {}).get('items', [])
    alloc = nodes[0]['status']['allocatable'].get('cpu', '0') if nodes else '0'
    alloc_m = _millicores(alloc)
    have = -(-alloc_m // 1000)
    pods = (_kubectl_json(['get', 'pods', '-A'], env) or {}).get('items', [])

    def cpu_of(p):
        return sum(_millicores(c.get('resources', {}).get('requests', {}).get('cpu'))
                   for c in p['spec'].get('containers', []))
    # The scheduler charges a Terminating pod's requests until it is gone
    # (nico-api's grace period is 240s), so right after a restart the node
    # looks fuller than its steady state. Only steady state + surge >
    # allocatable means "resize"; otherwise the shortfall is transient.
    running = [p for p in pods if p['status'].get('phase') == 'Running']
    terminating = [p for p in running if p['metadata'].get('deletionTimestamp')]
    term = sum(cpu_of(p) for p in terminating)
    steady = max(0, sum(cpu_of(p) for p in running) - term)
    pend = cpu_of(pending_pod)
    if steady + pend <= alloc_m:
        print(f'    (transient: {term}m still held by {len(terminating)} Terminating pod(s); '
              f'steady state {steady}m + surge {pend}m fits in {alloc} allocatable — no resize needed)')
        return
    _cpu_advised = True
    want = max(8, (steady + pend) // 1000 + 2)   # surge + headroom; 8 = "code development" tier
    print(f'''  ┌─ VM is under-sized for rolling updates ──────────────────────────────────
  │ Steady-state CPU requests: {steady}m of {alloc} allocatable (plus {term}m in {len(terminating)} Terminating pod(s) right now);
  │ this surge pod asks {pend}m more.
  │ The workaround gets this rollout through, but every future redeploy and
  │ restart will hit the same wall. Fix it properly:
  │   1. Stop the VM.
  │   2. Increase CPUs from {have} to at least {want} (and memory to 16 GB if it is below):
  │        macOS/UTM : UTM → select VM → ⌘E → System → CPU Cores
  │        Linux     : virsh setvcpus <vm-name> {want} --config --maximum && virsh setvcpus <vm-name> {want} --config
  │   3. Set  vm: {{ cpus: {want} }}  in your devup yaml so dev-up/records agree.
  │   4. Start the VM; on the VM run restart-ordered.sh to settle the site.
  └──────────────────────────────────────────────────────────────────────────''')


def watch_rollout(ns, env, policy, timeout=600, poll=10):
    """Wait for every Deployment in ns to finish rolling; diagnose a surge
    pod stuck on 'Insufficient cpu' and, with policy scale-down-first, free room by
    deleting ONE old pod of that deployment (20260903-#2).

    Acts on the scheduler's verdict only — on a cluster with room the
    condition never appears and nothing is touched, so multi-node setups
    behave exactly as before.
    """
    import time
    start = time.time()
    warned = set()
    # scale-down-first: deployments whose strategy we patched, with the
    # original strategy to restore. Deleting old pods (the first approach)
    # was a coin toss — the OLD ReplicaSet recreates each deleted pod and its
    # replacement kept winning the scheduler race against the surge pod.
    # Switching the strategy to maxSurge 0 / maxUnavailable 1 makes the
    # controller remove an old pod FIRST, so the new one always fits.
    patched = {}

    def restore_strategies():
        import json
        for dep, orig in patched.items():
            subprocess.run(['kubectl', 'patch', 'deploy', dep, '-n', ns, '--type', 'merge',
                            '-p', json.dumps({'spec': {'strategy': orig}})],
                           env=env, capture_output=True)
            print(f'  restored {dep} rollout strategy to the chart\'s ({orig})')

    while True:
        got = _kubectl_json(['get', 'deploy', '-n', ns], env)
        if got is None:
            if time.time() - start > timeout:
                print(f'Error: could not read deployments in {ns} for {timeout}s',
                      file=sys.stderr)
                return False
            time.sleep(poll)
            continue
        deps = got.get('items', [])
        pending = []
        for d in deps:
            spec, st = d.get('spec', {}), d.get('status', {})
            want = spec.get('replicas', 1)
            done = (st.get('observedGeneration', 0) >= d['metadata'].get('generation', 0)
                    and st.get('updatedReplicas', 0) == want
                    and st.get('availableReplicas', 0) == want
                    and st.get('replicas', 0) == want)
            if not done:
                pending.append(d['metadata']['name'])
        if not pending:
            print(f'  all deployments in {ns} rolled out ✓')
            restore_strategies()
            return True
        if time.time() - start > timeout:
            print(f'Error: rollout still incomplete after {timeout}s: '
                  f'{", ".join(pending)}', file=sys.stderr)
            restore_strategies()
            return False

        # Surge pods the scheduler refuses for lack of CPU
        pods = (_kubectl_json(['get', 'pods', '-n', ns], env) or {}).get('items', [])
        rs_by_name = {r['metadata']['name']: r for r in
                      (_kubectl_json(['get', 'rs', '-n', ns], env) or {}).get('items', [])}

        def deploy_of(pod):
            for o in pod['metadata'].get('ownerReferences', []):
                if o['kind'] == 'ReplicaSet':
                    for o2 in rs_by_name.get(o['name'], {}).get('metadata', {}).get('ownerReferences', []):
                        if o2['kind'] == 'Deployment':
                            return o2['name'], o['name']
            return None, None

        def revision(rs_name):
            ann = rs_by_name.get(rs_name, {}).get('metadata', {}).get('annotations', {})
            try:
                return int(ann.get('deployment.kubernetes.io/revision', '0'))
            except ValueError:
                return 0

        # the deployment's CURRENT ReplicaSet = highest revision among its RSs
        newest_rs = {}
        for rs in rs_by_name.values():
            for o in rs['metadata'].get('ownerReferences', []):
                if o['kind'] == 'Deployment':
                    cur = newest_rs.get(o['name'])
                    if cur is None or revision(rs['metadata']['name']) > revision(cur):
                        newest_rs[o['name']] = rs['metadata']['name']

        strategy_of = {d['metadata']['name']: d.get('spec', {}).get('strategy', {}) for d in deps}

        for pod in pods:
            if pod['status'].get('phase') != 'Pending':
                continue
            conds = pod['status'].get('conditions', [])
            msg = next((c.get('message', '') for c in conds
                        if c.get('type') == 'PodScheduled' and c.get('status') == 'False'), '')
            if 'Insufficient cpu' not in msg:
                continue
            dep, pod_rs = deploy_of(pod)
            if not dep:
                continue
            # Only the NEW ReplicaSet's pod is the stuck surge pod. A Pending
            # pod of an OLD ReplicaSet is that RS replacing something we (or
            # the rollout) removed — never act on it.
            if pod_rs != newest_rs.get(dep):
                continue
            name = pod['metadata']['name']
            cpu_sizing_advice(env, pod)
            if policy == 'scale-down-first':
                if dep in patched:
                    continue          # already switched; the controller is on it
                import json
                patched[dep] = strategy_of.get(dep, {})
                print(f'  ⚠ {name} Pending: Insufficient cpu — scale-down-first: '
                      f'switching {dep} to maxSurge 0 / maxUnavailable 1 for this '
                      f'rollout (old pod goes first, new one fits; chart strategy '
                      f'restored afterwards)')
                subprocess.run(['kubectl', 'patch', 'deploy', dep, '-n', ns, '--type', 'merge',
                                '-p', json.dumps({'spec': {'strategy': {
                                    'type': 'RollingUpdate',
                                    'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1}}}})],
                               env=env, capture_output=True)
            elif name not in warned:
                warned.add(name)
                print(f'  ⚠ {name} Pending: Insufficient cpu — the node has no room '
                      f'for {dep}\'s surge pod (single node, requests ~fully '
                      f'committed).\n    Unstick by hand:  kubectl -n {ns} delete pod '
                      f'<an old {dep} pod>\n    Or set redeploy.on_insufficient_cpu: '
                      f'scale-down-first in the site yaml (or --on-insufficient-cpu '
                      f'scale-down-first). Waiting…')
        time.sleep(poll)


def main():
    p = argparse.ArgumentParser(
        description='Redeploy Nico to a new image tag (helm upgrade only)'
    )
    p.add_argument('site', help='Site folder or site yaml path')
    p.add_argument('--tag', required=True,
                   help='Image tag to deploy (must already be in the registry)')
    p.add_argument('--force', action='store_true',
                   help='Proceed even if this tag is already the deployed one '
                        '(only useful with imagePullPolicy Always)')
    p.add_argument('--on-insufficient-cpu', choices=['wait', 'scale-down-first'], default=None,
                   help='override the site yaml nico-system.redeploy.on_insufficient_cpu '
                        '(default from the site yaml, else wait)')
    args = p.parse_args()

    site_yaml, site_folder = resolve_site(args.site)
    cfg = yaml.safe_load(open(site_yaml))
    dc  = cfg['fabric']['dc_name']

    # Resolve nico repo path (nico_repo_folder inside whichever share root is accessible)
    repo_folder = cfg.get('nico_repo_folder', 'infra-controller-core')
    nico_mac = cfg.get('nico_mac_folder', '')
    nico_vm  = cfg.get('nico_vm_folder', '')
    if nico_mac and Path(nico_mac).expanduser().exists():
        repo = Path(nico_mac).expanduser() / repo_folder
    elif nico_vm and Path(nico_vm).expanduser().exists():
        repo = Path(nico_vm).expanduser() / repo_folder
    else:
        print('Error: neither nico_mac_folder nor nico_vm_folder is accessible.',
              file=sys.stderr)
        print('  Check nico_mac_folder / nico_vm_folder in site yaml.', file=sys.stderr)
        sys.exit(1)

    helm_dir      = str(repo / 'helm')
    sitename      = cfg.get('nico-system', {}).get('helm-values', {}).get('sitename', 'dev')
    kube_filename = cfg.get('kubeconfig', f'{dc}-{sitename}.kubeconfig.yaml')
    kubeconfig    = str(Path(site_folder) / kube_filename)

    if not Path(helm_dir).exists():
        print(f'Error: helm dir not found: {helm_dir}', file=sys.stderr)
        sys.exit(1)
    if not Path(kubeconfig).exists():
        print(f'Error: kubeconfig not found: {kubeconfig}', file=sys.stderr)
        sys.exit(1)

    env = {**os.environ, 'KUBECONFIG': kubeconfig}

    print('nico-dev — Redeploy Nico')
    print(f'  tag        : {args.tag}')
    print(f'  kubeconfig : {kubeconfig}')
    print(f'  helm dir   : {helm_dir}')

    # Same tag as deployed = no pod-template change = NO rollout, and the
    # node keeps its cached image (imagePullPolicy IfNotPresent) — a rebuild
    # under the same tag silently changes nothing on the cluster
    # (20260903-#1). Refuse unless told otherwise.
    r = subprocess.run(['helm', 'get', 'values', 'nico', '-n', 'nico-system',
                        '-o', 'json'], env=env, capture_output=True, text=True)
    deployed = ''
    if r.returncode == 0:
        try:
            import json
            deployed = json.loads(r.stdout).get('global', {}).get('image', {}).get('tag', '')
        except ValueError:
            pass
    print(f'  deployed   : {deployed or "(unknown)"}')
    if deployed == args.tag and not args.force:
        print(f'\nError: tag {args.tag} is already the deployed tag — helm sees no '
              f'change, nothing rolls out, and the node would reuse its cached\n'
              f'  image even if you rebuilt it. Rebuild under a NEW tag '
              f'(e.g. --tag {args.tag}-2) and redeploy that, or pass --force.',
              file=sys.stderr)
        sys.exit(1)

    # Policy for a rollout that cannot schedule its surge pod (20260903-#2):
    # site yaml nico-system.redeploy.on_insufficient_cpu, CLI overrides.
    policy = (args.on_insufficient_cpu
              or cfg.get('nico-system', {}).get('redeploy', {}).get('on_insufficient_cpu')
              or 'wait')
    if policy not in ('wait', 'scale-down-first'):
        print(f'Error: redeploy.on_insufficient_cpu must be wait or scale-down-first, got {policy!r}',
              file=sys.stderr)
        sys.exit(1)
    print(f'  on Insufficient cpu: {policy}')

    # Apply without --wait; we watch the rollout ourselves so a stuck surge
    # pod gets diagnosed (and, with scale-down-first, unstuck) instead of a silent
    # ten-minute helm timeout.
    r = subprocess.run([
        'helm', 'upgrade', 'nico', helm_dir,
        '-n', 'nico-system',
        '--reuse-values',
        '--set', f'global.image.tag={args.tag}',
    ], env=env)
    if r.returncode != 0:
        print('Error: helm upgrade failed', file=sys.stderr)
        sys.exit(1)

    if not watch_rollout('nico-system', env, policy, timeout=600):
        sys.exit(1)

    print(f'\n  Nico redeployed with tag {args.tag} ✓')
    # images.tag in the site yaml = what the cluster runs now
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location('site_images', Path(__file__).parent / 'site_images.py')
    _si = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_si)
    _si.record(site_yaml, deployed_tag=args.tag)
    print(f'  site yaml images.tag = {args.tag} ✓')

    # helm re-renders nico-api-config-files, silently dropping the
    # allow_insecure_discovery patch deploy-dev-nico.py applied — without
    # it, MAT machine discovery fails PermissionDenied ('source IP and
    # selected interface do not identify one host'): 20260825-#4.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'deploy_dev_nico', Path(__file__).parent / 'deploy-dev-nico.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.patch_allow_insecure_discovery('nico-system', kubeconfig)

    print()
    subprocess.run(
        ['kubectl', 'get', 'pods', '-n', 'nico-system', '--no-headers', '-o', 'wide'],
        env=env,
    )


if __name__ == '__main__':
    main()
