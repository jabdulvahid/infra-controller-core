#!/usr/bin/env python3
"""
nico-dev — checkout parity: does this repo still look like what the nico-dev
scripts assume?

nico-dev re-implements helm-prereqs/setup.sh in Python rather than calling
it, and its recovery script embeds the same topology. That is a hidden
dependency: when upstream renames a chart path, a resource, a namespace, a
setup.sh phase, or flips a values default, nico-dev either fails mid-deploy
or, worse, keeps working while quietly diverging from what setup.sh
produces (found 2026-09-16 with #5694, which made Temporal/Keycloak's
database an opt-in move onto nico-pg-cluster).

This script lists every such assumption once, each tagged with the nico-dev
file that depends on it, and checks them against a checkout. It changes
nothing. check-prereqs.sh runs it when the tools sit inside a checkout, and
bring-up.py runs it in its preflight against the site's repo.

  check-parity.py                 # the checkout this script is grafted into
  check-parity.py <repo-path>     # another checkout
  check-parity.py --quiet         # print only the failures

Exit 0 when every assumption holds, 1 otherwise. A ✗ names the upstream
change and the nico-dev file to update; it is not something to work around
in the checkout.
"""

import argparse
import subprocess
import sys
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


# ── helpers ──────────────────────────────────────────────────────────────────
def load_yaml(path):
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


class Checks:
    def __init__(self, repo, quiet):
        self.repo, self.quiet = repo, quiet
        self.fail = 0
        self.group = None

    def section(self, title):
        self.group = title
        if not self.quiet:
            print(f'── {title} ──')

    def ok(self, text):
        if not self.quiet:
            print(f'  ✓ {text}')

    def bad(self, text, consumer, hint):
        self.fail += 1
        print(f'  ✗ {text}')
        print(f'      depends: {consumer}')
        print(f'      → {hint}')

    def path(self, rel, consumer, what=None):
        p = self.repo / rel
        if p.exists():
            self.ok(f'{rel}')
        else:
            self.bad(f'{rel} missing', consumer,
                     f'upstream moved or removed it{" (" + what + ")" if what else ""}; '
                     f'update the path in {consumer}')
        return p.exists()

    def contains(self, rel, needle, consumer, meaning):
        p = self.repo / rel
        text = p.read_text() if p.exists() else ''
        if needle in text:
            self.ok(f'{rel} defines {meaning}')
        else:
            self.bad(f'{rel} no longer contains {needle!r} ({meaning})', consumer,
                     f'upstream renamed or removed it; update {consumer}')

    def value(self, rel, keys, expected, consumer, meaning):
        got = dig(load_yaml(self.repo / rel), *keys)
        label = '.'.join(keys)
        if got == expected:
            self.ok(f'{rel}: {label} = {expected!r} ({meaning})')
        else:
            self.bad(f'{rel}: {label} is {got!r}, nico-dev assumes {expected!r} ({meaning})', consumer,
                     f'upstream changed the default; decide whether nico-dev follows it, then update {consumer}')

    def key(self, rel, keys, consumer, meaning):
        got = dig(load_yaml(self.repo / rel), *keys)
        label = '.'.join(keys)
        if got is not None:
            self.ok(f'{rel}: {label} present ({meaning})')
        else:
            self.bad(f'{rel}: {label} missing ({meaning})', consumer,
                     f'upstream removed or renamed the key; update {consumer}')


# ── the assumptions ──────────────────────────────────────────────────────────
def run(repo, quiet):
    c = Checks(repo, quiet)

    c.section('core deploy (deploy-dev-nico.py, redeploy-dev-nico.py, generate_dev_values.py)')
    c.path('helm/Chart.yaml', 'deploy-dev-nico.py / redeploy-dev-nico.py', 'the nico umbrella chart')
    c.path('helm-prereqs/Chart.yaml', 'deploy-dev-nico.py', 'the nico-prereqs chart')
    c.path('helm-prereqs/setup.sh', 'rest_deploy.py mirrors its phases')
    for k in ('vault', 'rest', 'flow', 'postgresql'):
        c.key('helm-prereqs/values.yaml', (k,), 'generate_dev_values.py gen_nico_prereqs', f'nico-dev renders {k}:')
    c.contains('helm-prereqs/templates/postgresql.yaml', 'name: nico-pg-cluster',
               'rest_deploy.py, restart-ordered.sh', 'the shared PostgreSQL cluster nico-pg-cluster')
    c.contains('helm-prereqs/templates/postgresql.yaml', 'nico_rest: nico-rest.nico',
               'rest_deploy.py deploy_nico_rest', 'the nico_rest database owned by nico-rest.nico')
    c.contains('helm-prereqs/templates/eso-external-secrets.yaml', 'nico-rest-pg-creds',
               'rest_deploy.py deploy_nico_rest', 'the ESO-synced REST DB credential secret')
    c.path('helm/charts/nico-api/templates/dpf-rbac.yaml', 'generate_dev_values.py (nico-api.dpf.rbacCreate)')
    c.key('helm/charts/nico-api/values.yaml', ('dpf', 'rbacCreate'), 'generate_dev_values.py', 'DPF Role toggle')
    c.value('helm/charts/nico-api/values.yaml', ('dpf', 'operatorNamespace'), 'dpf-operator-system',
            'deploy-dpf-sim.py, nico-dev.yaml template', 'the DPF namespace nico-dev uses')

    c.section('REST stack (rest_deploy.py — a transcription of setup.sh phases 7a–7i)')
    for phase, what in (('[7b/7]', 'REST CA issuer'), ('[7c/7] NICo REST postgres', 'standalone REST postgres'),
                        ('[7d/7] Keycloak', 'Keycloak'), ('[7e/7] Temporal TLS bootstrap', 'Temporal TLS'),
                        ('[7f/7] Temporal', 'Temporal install'), ('[7g/7] NICo REST helm chart', 'REST umbrella'),
                        ('[7i/7] NICo REST site-agent', 'site-agent')):
        c.contains('helm-prereqs/setup.sh', f'_SETUP_PHASE="{phase}', 'rest_deploy.py', f'setup.sh phase {what}')
    c.path('rest-api/scripts/gen-site-ca.sh', 'rest_deploy.py deploy_rest_postgres')
    c.path('rest-api/deploy/kustomize/base/cert-manager-io', 'rest_deploy.py deploy_rest_postgres')
    c.path('rest-api/deploy/kustomize/base/postgres', 'rest_deploy.py deploy_rest_postgres', 'standalone REST postgres')
    for f in ('namespace.yaml', 'db-creds.yaml', 'certificates.yaml'):
        c.path(f'rest-api/deploy/kustomize/base/temporal-helm/{f}', 'rest_deploy.py deploy_temporal')
    c.path('rest-api/temporal-helm/temporal/Chart.yaml', 'rest_deploy.py deploy_temporal', 'vendored Temporal chart')
    c.path('rest-api/temporal-helm/temporal/values-kind.yaml', 'rest_deploy.py deploy_temporal')
    c.path('helm-prereqs/keycloak/setup.sh', 'rest_deploy.py deploy_keycloak')
    c.path('helm-prereqs/keycloak/get-token.sh', 'how-to (nicocli token)')
    c.path('helm-prereqs/values/nico-rest.yaml', 'rest_deploy.py deploy_nico_rest')
    c.contains('helm-prereqs/values/nico-rest.yaml', 'keycloak.nico-rest',
               'rest_deploy.py (Keycloak in namespace nico-rest)', 'the Keycloak in-cluster URL host')
    c.path('helm/rest/nico-rest/Chart.yaml', 'rest_deploy.py deploy_nico_rest', 'the REST umbrella chart')
    c.path('helm/rest/nico-rest-site-agent/Chart.yaml', 'rest_deploy.py deploy_site_agent')
    c.contains('helm/rest/nico-rest-site-agent/values.yaml', 'FLOW_GRPC_ENABLED',
               'deploy-flow.py (site-agent --set envConfig.FLOW_GRPC_ENABLED)', 'the site-agent Flow switch')
    # #5694 (2026-09-04): Temporal/Keycloak DB consolidation onto nico-pg-cluster is
    # opt-in upstream; nico-dev still deploys the standalone postgres (legacy path).
    # TODO (nico-dev): adopt the consolidation for fresh sites — decision pending.
    for k in ('temporal', 'keycloak'):
        c.value('helm-prereqs/values.yaml', (k, 'useHaPostgres'), False,
                'rest_deploy.py (legacy standalone postgres path; TODO #5694 consolidation)',
                f'{k} DB stays on the standalone postgres by default')

    c.section('images (site_images.py — the fixed local names; build-dev-nico.py, deploy-flow.py --build)')
    for name in site_images.IMAGE_NAMES['rest'] + site_images.IMAGE_NAMES['flow']:
        c.path(f'rest-api/docker/production/Dockerfile.{name}', 'site_images.IMAGE_NAMES / build-dev-nico.py',
               f'the {name} image')
    c.path('helm/charts/nico-flow/Chart.yaml', 'deploy-flow.py')
    c.path('helm/charts/nico-flow/templates/namespace.yaml', 'deploy-flow.py (pre-applies it)')
    flow_imgs = set((dig(load_yaml(repo / 'helm/charts/nico-flow/values.yaml'), 'images') or {}).keys())
    if flow_imgs == {'flow'}:
        c.ok('helm/charts/nico-flow/values.yaml: one flow container (post-#5325 chart)')
    else:
        c.bad(f'helm/charts/nico-flow/values.yaml images = {sorted(flow_imgs)}; nico-dev implements the '
              f'single-container chart', 'deploy-flow.py, site_images.IMAGE_NAMES[flow]',
              'the Flow chart changed shape; update deploy-flow.py and the flow image list')
    for arch in ('aarch64', 'x86_64'):
        c.path(f'dev/docker/Dockerfile.build-container-{arch}', 'build-dev-nico.py (source-build lane)')

    c.section('DPF (deploy-dpf-sim.py, deploy-dev-nico.py CRD step)')
    crds = sorted((repo / 'crates/dpf/crds').glob('*.yaml')) if (repo / 'crates/dpf/crds').is_dir() else []
    if crds:
        c.ok(f'crates/dpf/crds: {len(crds)} CRDs')
    else:
        c.bad('crates/dpf/crds has no CRDs', 'deploy-dpf-sim.py ensure_dpf_prereqs',
              'upstream moved the DPF CRDs; update CRD_DIR in deploy-dpf-sim.py')
    for kind in ('dpus', 'dpudevices', 'dpunodes'):
        if any(f'_{kind}.provisioning.dpu.nvidia.com' in p.name for p in crds):
            c.ok(f'crates/dpf/crds: {kind} CRD present')
        else:
            c.bad(f'crates/dpf/crds: no {kind} CRD', 'deploy-dpf-sim.py preflight (crds_established)',
                  'the CRD set changed; update the kinds checked in deploy-dpf-sim.py')
    for rel in ('dev/k8s/dpf-sim-controller/Dockerfile', 'dev/k8s/dpf-sim-controller/config/rbac/role.yaml',
                'dev/k8s/dpf-sim-controller/config/manager/manager.yaml'):
        c.path(rel, 'deploy-dpf-sim.py')
    mgr = load_yaml(repo / 'dev/k8s/dpf-sim-controller/config/manager/manager.yaml')
    args = (dig(mgr, 'spec', 'template', 'spec', 'containers') or [{}])[0].get('args', [])
    if any(a.startswith('--phase-dwell=') for a in args) and any(a.startswith('--dpf-namespace=') for a in args):
        c.ok('dpf-sim-controller manager.yaml: --dpf-namespace / --phase-dwell args (nico-dev rewrites them)')
    else:
        c.bad(f'dpf-sim-controller manager.yaml args = {args}', 'deploy-dpf-sim.py render_manifests',
              'the simulator flags changed; update render_manifests in deploy-dpf-sim.py')

    return c.fail


def main():
    p = argparse.ArgumentParser(description='nico-dev ↔ checkout parity check',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('repo', nargs='?', default=None,
                   help='repo checkout to check (default: the one this script is grafted into)')
    p.add_argument('--quiet', action='store_true', help='print only failures')
    a = p.parse_args()
    if a.repo:
        repo = Path(a.repo).expanduser().resolve()
    else:
        r = subprocess.run(['git', '-C', str(HERE), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('Error: not inside a git checkout — pass the repo path')
        repo = Path(r.stdout.strip())
    if not (repo / 'helm-prereqs').is_dir():
        sys.exit(f'Error: {repo} does not look like the NICo repo (no helm-prereqs/)')
    if not a.quiet:
        print(f'nico-dev ↔ checkout parity: {repo}')
    fail = run(repo, a.quiet)
    if not a.quiet:
        print()
        print('All assumptions hold.' if not fail
              else f'{fail} assumption(s) no longer hold — the checkout changed under nico-dev; '
                   f'fix the named nico-dev file(s), not the repo.')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
