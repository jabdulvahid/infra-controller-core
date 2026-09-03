#!/usr/bin/env python3
"""
nico-dev add-on — deploy NICo Flow (flow + psm + nsm) onto a running nico-dev site.

A POST-bring-up option: nothing in dev-up / deploy-dev-nico changes. Run it
after the site is up, from the host (helm/kubectl reach the cluster through
the site kubeconfig). One script, one chart; deploy-<chart>.py is the pattern
for further add-ons.

  deploy-flow.py <site> --ngc                    # images from NGC at the REST tag
  deploy-flow.py <site> --build                  # images built from the checkout
  deploy-flow.py <site>                          # images already in the registry
  deploy-flow.py <site> --tag <tag> …            # explicit tag (default: the
                                                 #   deployed nico-rest tag)
  deploy-flow.py <site> --uninstall              # remove the flow release
  deploy-flow.py <site> --dry-run

What Flow needs that a base nico-dev site does not have (all created here):
  1. images     nico-flow (plus nico-psm/nico-nsm on checkouts older than
                #5325, 2026-08-31 — read from the chart's values.yaml) at the
                SAME tag as NICo REST (they ship on the REST release line)
  2. prereqs    the flow database + user on nico-pg-cluster and its DB
                credentials synced by ESO into the flow namespace (psm/nsm
                DBs + Vault tokens too on the older chart) — all rendered by
                the nico-prereqs chart when flow.enabled=true (a helm upgrade
                with --reuse-values)
  3. temporal   a `flow` Temporal namespace
  4. certs      flow-certificate (vault-nico-issuer) and temporal-client-certs
                (nico-rest-ca-issuer), pre-applied so the pod never races them
  5. chart      helm/charts/nico-flow in namespace `flow`
  6. site-agent FLOW_GRPC_ENABLED=true (nico-dev installs it with false)

Mirrors helm-prereqs/setup.sh phase 7h; the differences are only the
image source (local registry) and the dev-mode flowEnv.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed', file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location('site_images', HERE / 'site_images.py')
site_images = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(site_images)
RELEASE, NS = 'flow', 'flow'


def flow_images(chart):
    """The containers the CHECKOUT's chart deploys → image names.

    Upstream removed PSM/NSM from the flow pod on 2026-08-31 (#5325): newer
    checkouts have only `images.flow`, older ones flow+psm+nsm. Read the
    chart's values.yaml instead of assuming either shape."""
    vals = yaml.safe_load((chart / 'values.yaml').read_text()) or {}
    keys = list((vals.get('images') or {}).keys()) or ['flow']
    return [f'nico-{k}' for k in keys]


def needs_vault_tokens(prereqs_dir):
    """Older prereqs charts write psm/nsm Vault tokens via a hook job; the
    template is gone in checkouts past #5325."""
    return (prereqs_dir / 'templates' / 'flow-vault-tokens-job.yaml').exists()
DOCKER_ARCH = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'amd64'


# ── helpers ──────────────────────────────────────────────────────────────────
def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if '.kubeconfig' not in f.name]
        if len(yamls) != 1:
            sys.exit(f'Error: expected exactly one site yaml in {p}, found {len(yamls)}')
        return str(yamls[0]), str(p)
    return str(p), str(p.parent)


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


def pull_images_from_ngc(push_reg, ngc_base, ngc_tag, local_tag, token_env, images):
    token = os.environ.get(token_env)
    if not token:
        sys.exit(f'Error: env var {token_env} is empty or unset (the NGC API key)')
    print(f'\nPulling {", ".join(images)}:{ngc_tag} from {ngc_base} → {push_reg}:{local_tag}')
    run(['docker', 'login', 'nvcr.io', '-u', '$oauthtoken', '--password-stdin'],
        stdin=token, capture=True)
    for i, image in enumerate(images, 1):
        src = f'{ngc_base}/{image}:{ngc_tag}'
        dst = f'{push_reg}/{image}:{local_tag}'
        print(f'  [{i}/{len(images)}] {src}')
        run(['docker', 'pull', '--platform', f'linux/{DOCKER_ARCH}', src], capture=False)
        run(['docker', 'tag', src, dst])
        run(['docker', 'push', dst], capture=False)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description='Deploy NICo Flow onto a nico-dev site (add-on)',
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument('site', help='site folder or site yaml')
    src = p.add_mutually_exclusive_group()
    src.add_argument('--ngc', action='store_true',
                     help='pull nico-flow/psm/nsm from NGC (needs --ngc-image or $NICO_NGC_IMAGE base)')
    src.add_argument('--build', action='store_true',
                     help='build nico-flow/psm/nsm from the checkout (docker buildx)')
    p.add_argument('--tag', default=None,
                   help='image tag in the local registry (default: the deployed nico-rest tag)')
    p.add_argument('--ngc-tag', default=None,
                   help='NGC tag to pull (default: --tag without its ngc- prefix)')
    p.add_argument('--ngc-image', default=None,
                   help='NGC repository BASE, e.g. nvcr.io/<org>/<team> (default: dirname of $NICO_NGC_IMAGE)')
    p.add_argument('--token-env', default='NGC_API_KEY', help='env var NAME holding the NGC key')
    p.add_argument('--flow-env', default='development', choices=['development', 'staging', 'production'],
                   help="flow's FLOW_ENV (default development)")
    p.add_argument('--uninstall', action='store_true', help='remove the flow release, disable site-agent Flow gRPC')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    site_yaml, site_folder = resolve_site(args.site)
    cfg = yaml.safe_load(open(site_yaml))
    dc = cfg['fabric']['dc_name']
    sitename = cfg.get('nico-system', {}).get('helm-values', {}).get('sitename', 'dev')
    repo_folder = cfg.get('nico_repo_folder', 'infra-controller-core')
    repo = None
    for root in (cfg.get('nico_mac_folder', ''), cfg.get('nico_vm_folder', '')):
        if root and (Path(root).expanduser() / repo_folder).is_dir():
            repo = Path(root).expanduser() / repo_folder
            break
    if repo is None:
        sys.exit('Error: nico repo not reachable from here (nico_mac_folder / nico_vm_folder)')
    kubeconfig = str(Path(site_folder) / cfg.get('kubeconfig', f'{dc}-{sitename}.kubeconfig.yaml'))
    if not Path(kubeconfig).exists():
        sys.exit(f'Error: kubeconfig not found: {kubeconfig}')
    env = {'KUBECONFIG': kubeconfig}
    reg = cfg.get('registry', {})
    registry = f'{reg.get("host", "192.168.64.1")}:{reg.get("port", 5000)}'   # as the VM sees it
    push_reg = f'localhost:{reg.get("port", 5000)}'                          # as this host pushes
    chart = repo / 'helm' / 'charts' / 'nico-flow'
    prereqs_dir = repo / 'helm-prereqs'
    sa_chart = repo / 'helm' / 'rest' / 'nico-rest-site-agent'
    for path in (chart, prereqs_dir, sa_chart):
        if not path.is_dir():
            sys.exit(f'Error: {path} not in the checkout')
    images = flow_images(chart)                 # what THIS checkout's chart deploys
    vault_tokens = needs_vault_tokens(prereqs_dir)
    components = [i.removeprefix('nico-') for i in images]

    # tag: default = what nico-rest runs (flow ships on the REST release line)
    rest_tag = helm_values('nico-rest', 'nico-rest', env).get('global', {}).get('image', {}).get('tag', '')
    tag = args.tag or rest_tag
    if not tag:
        sys.exit('Error: cannot determine the tag (nico-rest not deployed?) — pass --tag')

    print('nico-dev add-on — NICo Flow')
    print(f'  site       : {site_yaml}')
    print(f'  kubeconfig : {kubeconfig}')
    print(f'  chart      : {chart}')
    print(f'  registry   : {registry}  (push via {push_reg})')
    print(f'  tag        : {tag}' + ('' if tag == rest_tag else f'   (nico-rest runs {rest_tag or "?"})'))
    print(f'  images     : {", ".join(images)} — '
          f'{"build from checkout" if args.build else "pull from NGC" if args.ngc else "must already be in the registry"}')
    print(f'  chart shape: {len(images)} container(s); vault-token hook: '
          f'{"yes (pre-#5325 chart)" if vault_tokens else "no"}')
    print(f'  flowEnv    : {args.flow_env}')

    # ── uninstall ─────────────────────────────────────────────────────────
    if args.uninstall:
        print('\nUninstalling flow…')
        if args.dry_run:
            print('  (dry run) helm uninstall flow -n flow; site-agent FLOW_GRPC_ENABLED=false')
            return
        run(['helm', 'uninstall', RELEASE, '-n', NS], env=env, check=False, capture=False)
        run(['helm', 'upgrade', 'nico-rest-site-agent', str(sa_chart), '-n', 'nico-rest',
             '--reuse-values', '--set', 'envConfig.FLOW_GRPC_ENABLED=false',
             '--timeout', '300s', '--wait'], env=env, capture=False)
        print('  flow removed; site-agent Flow gRPC disabled ✓')
        print('  (kept: flow/psm/nsm databases and secrets from nico-prereqs — harmless;\n'
              '   the flow namespace stays for the ESO-synced secrets)')
        return

    # ── preflight ─────────────────────────────────────────────────────────
    print('\nPreflight')
    probs = []
    if subprocess.run(['docker', 'info'], capture_output=True).returncode != 0 and (args.build or args.ngc):
        probs.append('docker daemon not reachable (needed to build or pull images)')
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
                         'deploy the site first (dev-up / deploy-dev-nico.py)')
        elif kubectl(['get', 'deploy', 'nico-api', '-n', 'nico-system'], env,
                     check=False).returncode != 0:
            probs.append('nico-api deployment missing — core deploy incomplete')
        if kubectl(['get', 'deploy', 'temporal-admintools', '-n', 'temporal'], env,
                   check=False).returncode != 0:
            probs.append('temporal-admintools not found (REST stack incomplete)')
    for pr in probs:
        print(f'  ✗ {pr}')
    if not probs:
        print('  ✓ core release, issuers, nico-prereqs, temporal present')
    if probs and not args.dry_run:
        sys.exit(1)

    if args.dry_run:
        print('\nPlan (dry run — nothing executed):')
        print(f'  1. images   {"buildx " + ", ".join(images) if args.build else "pull " + ", ".join(images) + " from NGC" if args.ngc else "verify " + ", ".join(images) + " in registry"} @ {tag}')
        print(f'  2. prereqs  helm upgrade nico-prereqs --reuse-values --set flow.enabled=true '
              f'(DB + ESO sync for {", ".join(components)}'
              f'{"; psm/nsm vault tokens" if vault_tokens else ""})')
        print(f'  3. temporal namespace flow')
        print(f'  4. certs    pre-apply flow-certificate + temporal-client-certs, wait Ready')
        print(f'  5. chart    helm upgrade --install flow {chart} -n flow --set global.image.repository={registry} --set global.image.tag={tag} --set flowEnv={args.flow_env}')
        print(f'  6. site-agent helm upgrade nico-rest-site-agent --reuse-values --set envConfig.FLOW_GRPC_ENABLED=true')
        return

    # ── 1. images ─────────────────────────────────────────────────────────
    if args.build:
        build_images(repo, push_reg, tag, images)
    elif args.ngc:
        # defaults come from the site yaml's images.source (written by the
        # NGC deploy), so no flags are needed on a site deployed from NGC
        src_cfg = site_images.read(cfg)['source']
        ngc_tag = args.ngc_tag or src_cfg.get('tag') or tag.removeprefix('ngc-')
        base = (args.ngc_image or src_cfg.get('registry')
                or os.environ.get('NICO_NGC_IMAGE', '').rsplit('/', 1)[0])
        token_env = args.token_env if args.token_env != 'NGC_API_KEY' else (src_cfg.get('token_env') or 'NGC_API_KEY')
        if not base:
            sys.exit('Error: NGC registry base unknown — pass --ngc-image nvcr.io/<org>/<team> '
                     '(or deploy the site from NGC first so images.source records it)')
        pull_images_from_ngc(push_reg, base, ngc_tag, tag, token_env, images)
    missing = [i for i in images if not registry_has(push_reg, i, tag)]
    if missing:
        sys.exit(f'Error: not in {push_reg} at tag {tag}: {", ".join(missing)} — use --build or --ngc')
    print(f'  images present in registry at {tag} ✓')

    # ── 2. prereqs: flow.enabled=true on the existing nico-prereqs release ─
    print('\nEnabling flow prerequisites (nico-prereqs --set flow.enabled=true)…')
    # captured: helm prints the prereqs chart's generic NOTES ("Next step —
    # deploy NICo Core") after every upgrade, which reads as if this script
    # were about to deploy core. It is not. Output is shown only on failure.
    r = run(['helm', 'upgrade', 'nico-prereqs', str(prereqs_dir), '-n', 'nico-system',
             '--reuse-values', '--set', 'flow.enabled=true', f'--set', f'flow.namespace={NS}',
             '--timeout', '10m', '--wait'], env=env, capture=True)
    rev = next((l.split(':', 1)[1].strip() for l in r.stdout.splitlines()
                if l.startswith('REVISION:')), '?')
    print(f'  nico-prereqs upgraded (revision {rev}) with flow.enabled=true ✓')
    # The ESO ClusterExternalSecrets target the flow namespace by name, so it
    # must exist before the credential secrets can land. Pre-#5325 charts had
    # the vault-token hook job create it; now the flow chart's own
    # namespace.yaml does — apply that first (helm adopts it later).
    r = run(['helm', 'template', RELEASE, str(chart), '--namespace', NS,
             '--show-only', 'templates/namespace.yaml'], env=env)
    kubectl(['apply', '-f', '-'], env, stdin=r.stdout)
    if vault_tokens:
        for s in ('psm-vault-token', 'nsm-vault-token'):
            if not wait_for(f'secret {s}', lambda s=s: kubectl(['get', 'secret', s, '-n', NS], env,
                                                               check=False).returncode == 0, 300):
                print('  diagnose: kubectl logs -n nico-system job/flow-vault-tokens', file=sys.stderr)
                sys.exit(1)
    for svc in components:
        s = f'{svc}.nico.nico-pg-cluster.credentials'
        if not wait_for(f'secret {s}', lambda s=s: kubectl(['get', 'secret', s, '-n', NS], env,
                                                           check=False).returncode == 0, 300):
            print(f'  diagnose: kubectl describe clusterexternalsecret {svc}-db-eso', file=sys.stderr)
            sys.exit(1)

    # ── 3. temporal namespace `flow` (reuse nico-dev's helper, unmodified) ─
    import importlib.util
    spec = importlib.util.spec_from_file_location('rest_deploy', HERE / 'rest_deploy.py')
    rest_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rest_deploy)
    rest_deploy.ensure_temporal_namespace('flow', kubeconfig)

    # ── 4. certs pre-applied (setup.sh 7h dance) ───────────────────────────
    helm_args = ['--namespace', NS, '--create-namespace',
                 '--set', f'global.image.repository={registry}',
                 '--set', f'global.image.tag={tag}',
                 '--set', f'flowEnv={args.flow_env}']
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
    print(f'  gRPC: flow.{NS}.svc.cluster.local:50051'
          + ('  psm:50052  nsm:50053' if 'nico-psm' in images else ''))
    print(f'  Remove: {sys.argv[0]} {args.site} --uninstall')
    print('=' * 60)


if __name__ == '__main__':
    main()
