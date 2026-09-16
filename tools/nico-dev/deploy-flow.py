#!/usr/bin/env python3
"""
nico-dev add-on — NICo Flow, from its own standalone config (flow.yaml).

A POST-bring-up option: nothing in bring-up / deploy-dev-nico changes, and
nothing is read from or written to the site yaml. Everything this script
needs is in flow.yaml (see flow-example.yaml and addon_config.py), plus the
site FOLDER for the cluster's kubeconfig. Run it from the host after the
site is up:

  deploy-flow.py <site-folder> --config flow.yaml             # install or refresh
  deploy-flow.py <site-folder> --config flow.yaml --dry-run   # preflight + plan only
  deploy-flow.py <site-folder> --status                       # what is installed (from Helm)
  deploy-flow.py <site-folder> --uninstall                    # remove the flow release

What it does (helm-prereqs/setup.sh phase 7h, implemented against the CURRENT
chart; an older checkout is refused with "refresh the worktree"):
  1. images: pull nico-flow from NGC (source: ngc) or build it from the
     checkout (source: build) into the local registry
  2. nico-prereqs: --reuse-values --set flow.enabled=true (flow database on
     nico-pg-cluster + ESO credential sync) — a base release the Flow chart
     design makes us flip; no base script is involved
  3. Temporal namespace `flow`
  4. the chart's Certificates pre-applied and adopted (avoids the FailedMount race)
  5. helm upgrade --install flow
  6. nico-rest-site-agent: --reuse-values --set envConfig.FLOW_GRPC_ENABLED=true
--uninstall reverses 5 and 6; the databases and secrets from 2 stay (harmless).
"""

import argparse
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
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location('addon_config', HERE / 'addon_config.py')
addon_config = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(addon_config)

RELEASE, NS = 'flow', 'flow'
# The chart's images by their LOCAL names — fixed by the chart. One container
# since upstream #5325 (2026-08-31) folded PSM and NSM into nico-flow.
FLOW_IMAGES = ['nico-flow']
DOCKER_ARCH = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'amd64'


def check_chart_generation(chart, prereqs_dir):
    """This script implements the CURRENT chart: a single-container flow pod
    (upstream #5325, 2026-08-31, removed PSM/NSM and the vault-token hook).
    A checkout from before that is not supported here — the fix is to
    refresh the worktree, not to teach the script an older chart shape.
    Returns a problem string or None."""
    vals = yaml.safe_load((chart / 'values.yaml').read_text()) or {}
    keys = set((vals.get('images') or {}).keys())
    old_hook = (prereqs_dir / 'templates' / 'flow-vault-tokens-job.yaml').exists()
    if keys - {'flow'} or old_hook:
        return ('this checkout predates upstream #5325 (2026-08-31): the flow chart '
                f'still has containers {sorted(keys)}'
                + (' and helm-prereqs still has flow-vault-tokens-job.yaml' if old_hook else '')
                + ' — refresh the worktree (git pull / rebase onto origin/main) and rerun')
    if keys != {'flow'}:
        return f'unexpected flow chart images {sorted(keys)} — newer than this script knows; check ADDONS.md'
    return None


# ── helpers ──────────────────────────────────────────────────────────────────
def run(cmd, env=None, check=True, capture=True, cwd=None, stdin=None, timeout=900):
    r = subprocess.run(cmd, env={**os.environ, **(env or {})}, capture_output=capture,
                       text=True, cwd=cwd, input=stdin, timeout=timeout)
    if check and r.returncode != 0:
        print(f'  ! failed: {" ".join(map(str, cmd))[:160]}\n    '
              f'{(r.stderr or r.stdout or "").strip()[:600]}', file=sys.stderr)
        sys.exit(1)
    return r


def kubectl(args, env, **kw):
    # bounded API calls: an unreachable cluster fails in seconds, not minutes
    return run(['kubectl', '--request-timeout=20s'] + args, env=env, **kw)


def helm_values(release, ns, env):
    try:
        r = run(['helm', 'get', 'values', release, '-n', ns, '-o', 'json'], env=env,
                check=False, timeout=40)
    except subprocess.TimeoutExpired:
        return {}
    try:
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except ValueError:
        return {}


def wait_for(desc, probe, timeout=180, poll=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe():
            print(f'  {desc} ✓')
            return True
        time.sleep(poll)
    print(f'Error: {desc} not ready after {timeout}s', file=sys.stderr)
    return False


# ── images ───────────────────────────────────────────────────────────────────
def registry_has(reg, image, tag):
    r = subprocess.run(['curl', '-sf', '-m', '5', f'http://{reg}/v2/{image}/tags/list'],
                       capture_output=True, text=True)
    try:
        return r.returncode == 0 and tag in (json.loads(r.stdout).get('tags') or [])
    except ValueError:
        return False


def build_images(repo, push_reg, tag, images):
    rest_dir = repo / 'rest-api'
    if not rest_dir.is_dir():
        sys.exit(f'Error: {rest_dir} not in the checkout')
    print(f'\nBuilding {", ".join(images)} (Go, linux/{DOCKER_ARCH}) → {push_reg}:{tag}')
    for i, image in enumerate(images, 1):
        print(f'  [{i}/{len(images)}] {image}:{tag}')
        run(['docker', 'buildx', 'build', '--platform', f'linux/{DOCKER_ARCH}', '--push',
             '--build-arg', 'TARGETOS=linux', '--build-arg', f'TARGETARCH={DOCKER_ARCH}',
             '-t', f'{push_reg}/{image}:{tag}',
             '-f', f'docker/production/Dockerfile.{image}', '.'],
            cwd=rest_dir, capture=False)


def pull_images_from_ngc(push_reg, ngc_base, ngc_tag, local_tag, token_env, images, ngc_names):
    """images = the local (chart) names; ngc_names maps local → NGC name."""
    token = os.environ.get(token_env)
    if not token:
        sys.exit(f'Error: env var {token_env} is empty or unset (the NGC API key)')
    print(f'\nPulling {", ".join(images)}:{ngc_tag} from {ngc_base} → {push_reg}:{local_tag}')
    run(['docker', 'login', 'nvcr.io', '-u', '$oauthtoken', '--password-stdin'],
        stdin=token, capture=True)
    for i, image in enumerate(images, 1):
        src = f'{ngc_base}/{ngc_names.get(image, image)}:{ngc_tag}'
        dst = f'{push_reg}/{image}:{local_tag}'
        print(f'  [{i}/{len(images)}] {src}')
        run(['docker', 'pull', '--platform', f'linux/{DOCKER_ARCH}', src], capture=False)
        run(['docker', 'tag', src, dst])
        run(['docker', 'push', dst], capture=False)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description='Deploy NICo Flow onto a nico-dev site (add-on, standalone config)',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('site', help='site folder (holds the kubeconfig); the site yaml is not read')
    p.add_argument('--config', default=None, metavar='flow.yaml',
                   help='the add-on config (required to install; see flow-example.yaml)')
    p.add_argument('--uninstall', action='store_true', help='remove the flow release, disable site-agent Flow gRPC')
    p.add_argument('--status', action='store_true', help='show what is installed, from Helm')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    kubeconfig = addon_config.find_kubeconfig(args.site)
    env = {'KUBECONFIG': str(kubeconfig)}

    # ── status ────────────────────────────────────────────────────────────
    if args.status:
        vals = helm_values(RELEASE, NS, env)
        r = run(['helm', 'list', '-n', NS, '-o', 'json'], env=env, check=False, timeout=40)
        rels = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
        if not rels:
            print(f'flow: not installed (no helm release {RELEASE!r} in namespace {NS})')
            return
        rel = rels[0]
        img = (vals.get('global') or {}).get('image') or {}
        print(f'flow: {rel.get("status")}, chart {rel.get("chart")}, revision {rel.get("revision")}, '
              f'updated {rel.get("updated", "")[:19]}')
        print(f'  image      : {img.get("repository", "?")}/nico-flow:{img.get("tag", "?")}')
        print(f'  flowEnv    : {vals.get("flowEnv", "?")}')
        sa = helm_values('nico-rest-site-agent', 'nico-rest', env)
        print(f'  site-agent : FLOW_GRPC_ENABLED={(sa.get("envConfig") or {}).get("FLOW_GRPC_ENABLED", "?")}')
        kubectl(['get', 'pods', '-n', NS, '-o', 'wide'], env, capture=False, check=False)
        return

    # the checkout: the chart (and the build) come from it
    cfg = addon_config.load(args.config, FLOW_IMAGES, 'flow') if not args.uninstall else None
    repo = cfg.repo if cfg else addon_config.tools_checkout()
    if repo is None:
        sys.exit('Error: no checkout found — these tools are not inside a git checkout; set build.repo in flow.yaml')
    chart = repo / 'helm' / 'charts' / 'nico-flow'
    prereqs_dir = repo / 'helm-prereqs'
    sa_chart = repo / 'helm' / 'rest' / 'nico-rest-site-agent'
    for path in (chart, prereqs_dir, sa_chart):
        if not path.is_dir():
            sys.exit(f'Error: {path} not in the checkout {repo}')

    print('nico-dev add-on — NICo Flow')
    print(f'  site       : {Path(args.site).expanduser()}')
    print(f'  kubeconfig : {kubeconfig}')
    print(f'  chart      : {chart}')

    # ── uninstall ─────────────────────────────────────────────────────────
    if args.uninstall:
        print('\nUninstalling flow…')
        if args.dry_run:
            print(f'  (dry run) helm uninstall {RELEASE} -n {NS}; site-agent FLOW_GRPC_ENABLED=false')
            return
        run(['helm', 'uninstall', RELEASE, '-n', NS], env=env, check=False, capture=False)
        run(['helm', 'upgrade', 'nico-rest-site-agent', str(sa_chart), '-n', 'nico-rest',
             '--reuse-values', '--set', 'envConfig.FLOW_GRPC_ENABLED=false',
             '--timeout', '300s', '--wait'], env=env, capture=False)
        print('  flow removed; site-agent Flow gRPC disabled ✓')
        print('  (kept: the flow database and secrets from nico-prereqs — harmless;\n'
              '   the flow namespace stays for the ESO-synced secrets)')
        return

    print(cfg.describe())
    registry, push_reg, tag = cfg.registry, cfg.push_reg, cfg.local_tag
    flow_env = str(cfg.chart.get('flowEnv', 'development'))
    images = FLOW_IMAGES

    # ── preflight ─────────────────────────────────────────────────────────
    print('\nPreflight')
    probs = []
    chart_problem = check_chart_generation(chart, prereqs_dir)
    if chart_problem:
        probs.append(chart_problem)
    if cfg.source == 'build':
        for image in images:
            if not (repo / 'rest-api' / 'docker' / 'production' / f'Dockerfile.{image}').is_file():
                probs.append(f'rest-api/docker/production/Dockerfile.{image} not in {repo} — the image set changed; update FLOW_IMAGES')
    if not (sa_chart / 'values.yaml').is_file() or 'FLOW_GRPC_ENABLED' not in (sa_chart / 'values.yaml').read_text():
        probs.append('nico-rest-site-agent chart has no FLOW_GRPC_ENABLED value — the site-agent switch moved; update this script')
    if flow_env not in ('development', 'staging', 'production'):
        probs.append(f'chart.flowEnv must be development, staging or production (got {flow_env!r})')
    if subprocess.run(['docker', 'info'], capture_output=True).returncode != 0:
        probs.append('docker daemon not reachable (needed to build or pull the image)')
    if cfg.source == 'ngc' and not os.environ.get(cfg.ngc['token_env']):
        probs.append(f'env var {cfg.ngc["token_env"]} is empty or unset (the NGC API key)')
    # one bounded connectivity probe first — an unreachable API otherwise
    # costs a discovery-retry minute per check below
    if kubectl(['get', '--raw', '/version'], env, check=False, timeout=30).returncode != 0:
        probs.append(f'cluster API not reachable via {kubeconfig} (VM down? route missing?)')
    else:
        if kubectl(['get', 'clusterissuer', 'vault-nico-issuer'], env, check=False).returncode != 0:
            probs.append('ClusterIssuer vault-nico-issuer missing (nico-prereqs not deployed?)')
        if kubectl(['get', 'clusterissuer', 'nico-rest-ca-issuer'], env, check=False).returncode != 0:
            probs.append('ClusterIssuer nico-rest-ca-issuer missing (NICo REST not deployed? '
                         'flow needs its temporal client cert)')
        if not helm_values('nico-prereqs', 'nico-system', env):
            probs.append('helm release nico-prereqs not found in nico-system')
        # Flow is an add-on to a DEPLOYED core: refuse on a half-built site
        if not helm_values('nico', 'nico-system', env):
            probs.append('helm release nico (NICo core) not found in nico-system — '
                         'deploy the site first (bring-up / deploy-dev-nico.py)')
        elif kubectl(['get', 'deploy', 'nico-api', '-n', 'nico-system'], env,
                     check=False).returncode != 0:
            probs.append('nico-api deployment missing — core deploy incomplete')
        if kubectl(['get', 'deploy', 'temporal-admintools', '-n', 'temporal'], env,
                   check=False).returncode != 0:
            probs.append('temporal-admintools not found (REST stack incomplete)')
    for pr in probs:
        print(f'  ✗ {pr}')
    if not probs:
        print('  ✓ current chart generation; core release, issuers, nico-prereqs, temporal present')
    if probs and not args.dry_run:
        sys.exit(1)

    if args.dry_run:
        print('\nPlan (dry run — nothing executed):')
        how = (f'buildx {", ".join(images)} from {repo}' if cfg.source == 'build'
               else f'pull {", ".join(cfg.ngc_names[i] for i in images)}:{cfg.ngc["tag"]} from {cfg.ngc["registry"]}')
        print(f'  1. images   {how} → {push_reg} @ {tag}')
        print(f'  2. prereqs  helm upgrade nico-prereqs --reuse-values --set flow.enabled=true '
              f'(flow database + ESO credential sync)')
        print(f'  3. temporal namespace flow')
        print(f'  4. certs    pre-apply flow-certificate + temporal-client-certs, wait Ready')
        print(f'  5. chart    helm upgrade --install flow {chart} -n flow --set global.image.repository={registry} --set global.image.tag={tag} --set flowEnv={flow_env}')
        print(f'  6. site-agent helm upgrade nico-rest-site-agent --reuse-values --set envConfig.FLOW_GRPC_ENABLED=true')
        return

    # ── 1. images ─────────────────────────────────────────────────────────
    if cfg.source == 'build':
        build_images(repo, push_reg, tag, images)
    else:
        pull_images_from_ngc(push_reg, cfg.ngc['registry'], cfg.ngc['tag'], tag,
                             cfg.ngc['token_env'], images, cfg.ngc_names)
    missing = [i for i in images if not registry_has(push_reg, i, tag)]
    if missing:
        sys.exit(f'Error: not in {push_reg} at tag {tag}: {", ".join(missing)}')
    print(f'  images present in registry at {tag} ✓')

    # ── 2. prereqs: flow.enabled=true on the existing nico-prereqs release ─
    print('\nEnabling flow prerequisites (nico-prereqs --set flow.enabled=true)…')
    # captured: helm prints the prereqs chart's generic NOTES ("Next step —
    # deploy NICo Core") after every upgrade, which reads as if this script
    # were about to deploy core. It is not. Output is shown only on failure.
    r = run(['helm', 'upgrade', 'nico-prereqs', str(prereqs_dir), '-n', 'nico-system',
             '--reuse-values', '--set', 'flow.enabled=true', '--set', f'flow.namespace={NS}',
             '--timeout', '10m', '--wait'], env=env, capture=True)
    rev = next((l.split(':', 1)[1].strip() for l in r.stdout.splitlines()
                if l.startswith('REVISION:')), '?')
    print(f'  nico-prereqs upgraded (revision {rev}) with flow.enabled=true ✓')
    # The ESO ClusterExternalSecret targets the flow namespace by name, so it
    # must exist before the credential secret can land: apply the chart's own
    # namespace.yaml first (helm adopts it at install).
    r = run(['helm', 'template', RELEASE, str(chart), '--namespace', NS,
             '--show-only', 'templates/namespace.yaml'], env=env)
    kubectl(['apply', '-f', '-'], env, stdin=r.stdout)
    s = 'flow.nico.nico-pg-cluster.credentials'
    if not wait_for(f'secret {s}', lambda: kubectl(['get', 'secret', s, '-n', NS], env,
                                                    check=False).returncode == 0, 300):
        print('  diagnose: kubectl describe clusterexternalsecret flow-db-eso', file=sys.stderr)
        sys.exit(1)

    # ── 3. temporal namespace `flow` (reuse nico-dev's helper, unmodified) ─
    spec = _ilu.spec_from_file_location('rest_deploy', HERE / 'rest_deploy.py')
    rest_deploy = _ilu.module_from_spec(spec)
    spec.loader.exec_module(rest_deploy)
    rest_deploy.ensure_temporal_namespace('flow', kubeconfig)

    # ── 4. certs pre-applied (setup.sh 7h dance) ───────────────────────────
    helm_args = ['--namespace', NS, '--create-namespace',
                 '--set', f'global.image.repository={registry}',
                 '--set', f'global.image.tag={tag}',
                 '--set', f'flowEnv={flow_env}']
    print('\nPre-applying flow Certificates…')
    for tpl in ('templates/namespace.yaml', 'templates/certificate.yaml'):
        r = run(['helm', 'template', RELEASE, str(chart)] + helm_args + ['--show-only', tpl], env=env)
        kubectl(['apply', '-f', '-'], env, stdin=r.stdout)
    for kind_name in ('certificate/flow-certificate', 'certificate/temporal-client-certs'):
        kubectl(['annotate', kind_name, '-n', NS, f'meta.helm.sh/release-name={RELEASE}',
                 f'meta.helm.sh/release-namespace={NS}', '--overwrite'], env)
        kubectl(['label', kind_name, '-n', NS, 'app.kubernetes.io/managed-by=Helm', '--overwrite'], env)
    kubectl(['annotate', 'namespace', NS, f'meta.helm.sh/release-name={RELEASE}',
             f'meta.helm.sh/release-namespace={NS}', '--overwrite'], env)
    kubectl(['label', 'namespace', NS, 'app.kubernetes.io/managed-by=Helm', '--overwrite'], env)
    for cert in ('flow-certificate', 'temporal-client-certs'):
        kubectl(['wait', '--for=condition=Ready', f'certificate/{cert}', '-n', NS, '--timeout=180s'],
                env, capture=False)

    # ── 5. the chart ──────────────────────────────────────────────────────
    print('\nInstalling flow…')
    run(['helm', 'upgrade', '--install', RELEASE, str(chart)] + helm_args +
        ['--timeout', '300s', '--wait'], env=env, capture=False)

    # ── 6. site-agent: turn Flow gRPC on ──────────────────────────────────
    print('\nEnabling Flow gRPC on the site-agent…')
    run(['helm', 'upgrade', 'nico-rest-site-agent', str(sa_chart), '-n', 'nico-rest',
         '--reuse-values', '--set', 'envConfig.FLOW_GRPC_ENABLED=true',
         '--timeout', '300s', '--wait'], env=env, capture=False)

    print('\n' + '=' * 60)
    print(f'  ✓ NICo Flow deployed (tag {tag}) — namespace {NS}')
    kubectl(['get', 'pods', '-n', NS, '-o', 'wide'], env, capture=False)
    print(f'  gRPC: flow.{NS}.svc.cluster.local:50051')
    print(f'  Status: {sys.argv[0]} {args.site} --status    Remove: {sys.argv[0]} {args.site} --uninstall')
    print('=' * 60)


if __name__ == '__main__':
    main()
