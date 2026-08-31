"""
nico-dev — NICo REST stack deployment (ported from helm-prereqs/setup.sh phase 7).

Deploys, in order (each is a DEPLOY_ORDER release in deploy-dev-nico.py):
  rest-postgres          7a-7c: nico-rest ns + CA signing secret + ClusterIssuer
                         + dedicated postgres StatefulSet (temporal/keycloak DBs;
                         the REST app DB itself lives on nico-pg-cluster via the
                         helm-prereqs rest.enabled ESO sync)
  keycloak               7d: dev IdP (reference configuration)
  temporal               7e-7f: TLS bootstrap + vendored chart (values-kind) +
                         cloud/site namespaces
  nico-rest              7g: the umbrella (api/workflow/site-manager/db/certsmgr),
                         DB creds injected from the ESO-synced secret, workflow
                         workers pointed at nico-pg-cluster (#3081)
  nico-rest-site-agent   7i: pre-applied vault-issued gRPC client cert, per-site
                         temporal namespace, FLOW_GRPC_ENABLED=false (Flow is off
                         in nico-dev by default)

Exposure: NodePort 30388 (production parity) — from the Mac:
http://<vm-ip>:30388.
"""

import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def _run(cmd, kubeconfig, check=True, capture=True, cwd=None, stdin=None, timeout=900):
    import os
    env = {**os.environ, 'KUBECONFIG': kubeconfig}
    r = subprocess.run(cmd, env=env, capture_output=capture, text=True,
                       cwd=cwd, input=stdin, timeout=timeout)
    if check and r.returncode != 0:
        print(f'  ! failed: {" ".join(str(c) for c in cmd)}', file=sys.stderr)
        print(f'    {(r.stderr or r.stdout or "").strip()[:800]}', file=sys.stderr)
        sys.exit(1)
    return r


def _kubectl(args, kubeconfig, **kw):
    return _run(['kubectl'] + args, kubeconfig, **kw)


def site_uuid(site_folder):
    """Stable per-site UUID, persisted in the site folder (survives redeploys)."""
    f = Path(site_folder) / 'rest-site-uuid'
    if f.exists():
        return f.read_text().strip()
    u = str(uuid.uuid4())
    f.write_text(u + '\n')
    print(f'  generated site UUID {u} → {f}')
    return u


def deploy_rest_postgres(rest_dir, kubeconfig):
    """7a-7c: namespace, CA signing secret, ClusterIssuer, dedicated postgres."""
    _kubectl(['create', 'namespace', 'nico-rest'], kubeconfig, check=False)

    r = _kubectl(['get', 'secret', 'ca-signing-secret', '-n', 'nico-rest'],
                 kubeconfig, check=False)
    if r.returncode != 0:
        print('  generating NICo REST CA signing secret...')
        _run(['bash', './scripts/gen-site-ca.sh'], kubeconfig, cwd=rest_dir)
    else:
        print('  ca-signing-secret present ✓')

    print('  applying nico-rest-ca-issuer ClusterIssuer...')
    _kubectl(['apply', '-k', 'deploy/kustomize/base/cert-manager-io'],
             kubeconfig, cwd=rest_dir)
    _kubectl(['wait', '--for=condition=Ready',
              'clusterissuer/nico-rest-ca-issuer', '--timeout=60s'], kubeconfig)

    print('  deploying dedicated REST postgres (temporal/keycloak DBs)...')
    _kubectl(['apply', '-k', 'deploy/kustomize/base/postgres'],
             kubeconfig, cwd=rest_dir)
    _kubectl(['rollout', 'status', 'statefulset/postgres', '-n', 'postgres',
              '--timeout=300s'], kubeconfig)
    print('  rest-postgres ✓')


def deploy_keycloak(prereqs_dir, kubeconfig):
    """7d: dev IdP via the reference helm-prereqs/keycloak/setup.sh."""
    print('  deploying Keycloak (reference dev IdP)...')
    _run(['bash', str(Path(prereqs_dir) / 'keycloak' / 'setup.sh')],
         kubeconfig, capture=False)
    print('  keycloak ✓')


_TEMPORAL_ADDR = 'temporal-frontend.temporal:7233'
_TEMPORAL_TLS = ('--tls-cert-path /var/secrets/temporal/certs/server-interservice/tls.crt '
                 '--tls-key-path /var/secrets/temporal/certs/server-interservice/tls.key '
                 '--tls-ca-path /var/secrets/temporal/certs/server-interservice/ca.crt '
                 '--tls-server-name interservice.server.temporal.local')


def _temporal_exec(kubeconfig, tcmd, check=True):
    return _kubectl(['exec', '-n', 'temporal', 'deploy/temporal-admintools', '--',
                     'sh', '-c', f'{tcmd} --address {_TEMPORAL_ADDR} {_TEMPORAL_TLS}'],
                    kubeconfig, check=check)


def ensure_temporal_namespace(name, kubeconfig):
    r = _temporal_exec(kubeconfig, f'temporal operator namespace describe -n "{name}"',
                       check=False)
    if r.returncode == 0:
        print(f'  temporal namespace {name} ✓ (exists)')
        return
    r = _temporal_exec(kubeconfig,
                       f'temporal operator namespace create -n "{name}" --retention 72h',
                       check=False)
    if r.returncode != 0 and 'already exists' not in (r.stdout + r.stderr).lower():
        print(f'  ! failed to create temporal namespace {name}:', file=sys.stderr)
        print(f'    {(r.stderr or r.stdout).strip()[:400]}', file=sys.stderr)
        sys.exit(1)
    print(f'  temporal namespace {name} ✓')


def deploy_temporal(rest_dir, kubeconfig):
    """7e-7f: TLS bootstrap, vendored chart (kind values), cloud+site namespaces."""
    print('  temporal TLS bootstrap...')
    for f in ['namespace.yaml', 'db-creds.yaml', 'certificates.yaml']:
        _kubectl(['apply', '-f', f'deploy/kustomize/base/temporal-helm/{f}'],
                 kubeconfig, cwd=rest_dir)
    _kubectl(['wait', '--for=condition=Ready', 'certificate', '--all',
              '-n', 'temporal', '--timeout=180s'], kubeconfig)

    print('  installing temporal (vendored chart, kind-sized values)...')
    _run(['helm', 'upgrade', '--install', 'temporal',
          str(Path(rest_dir) / 'temporal-helm' / 'temporal'),
          '--namespace', 'temporal',
          '-f', str(Path(rest_dir) / 'temporal-helm' / 'temporal' / 'values-kind.yaml'),
          '--timeout', '600s', '--wait'], kubeconfig, capture=False)

    _kubectl(['rollout', 'status', 'deploy/temporal-frontend', '-n', 'temporal',
              '--timeout=180s'], kubeconfig)
    _kubectl(['rollout', 'status', 'deploy/temporal-admintools', '-n', 'temporal',
              '--timeout=180s'], kubeconfig)

    print('  waiting for temporal API...')
    for i in range(24):
        r = _temporal_exec(kubeconfig, 'temporal operator namespace list', check=False)
        if r.returncode == 0:
            break
        time.sleep(5)
    else:
        print('  ! temporal frontend not ready for namespace operations', file=sys.stderr)
        sys.exit(1)

    ensure_temporal_namespace('cloud', kubeconfig)
    ensure_temporal_namespace('site', kubeconfig)
    print('  temporal ✓')


def _ensure_pg_trgm(kubeconfig):
    """The nico-rest-db GIN index migration needs the pg_trgm extension in
    nico_rest. Zalando's preparedDatabases conflicts with the databases
    section (upstream setup.sh's note), so create it directly on the Patroni
    primary — idempotent. Ported from helm-prereqs/setup.sh; waits up to
    120s for the operator to have created the nico_rest database."""
    print('  ensuring pg_trgm extension in nico_rest...')
    for _ in range(24):
        primary = _kubectl(
            ['get', 'pods', '-n', 'postgres',
             '-l', 'application=spilo,spilo-role=master',
             '-o', 'jsonpath={.items[0].metadata.name}'],
            kubeconfig, check=False).stdout.strip()
        if primary:
            r = _kubectl(
                ['exec', '-n', 'postgres', primary, '--', 'su', 'postgres',
                 '-c', "psql -d nico_rest -c "
                       "'CREATE EXTENSION IF NOT EXISTS pg_trgm;'"],
                kubeconfig, check=False)
            if r.returncode == 0:
                print('  pg_trgm ready ✓')
                return
        time.sleep(5)
    print('  ! pg_trgm not installable in nico_rest after 120s — the '
          'nico-rest-db GIN index migration would fail.', file=sys.stderr)
    print('    Is rest.enabled true in nico-prereqs (creates the nico_rest '
          'DB) and nico-pg-cluster healthy?', file=sys.stderr)
    sys.exit(1)


def deploy_nico_rest(repo, rest_dir, kubeconfig, registry, tag, dev_values):
    """7g: the REST umbrella. DB creds come from the ESO-synced secret
    (helm-prereqs rest.enabled=true); workflow workers pointed at
    nico-pg-cluster/nico_rest (#3081 consolidation)."""
    _ensure_pg_trgm(kubeconfig)
    print('  waiting for ESO-synced REST DB creds (nico-rest-pg-creds)...')
    for i in range(24):
        r = _kubectl(['get', 'secret', 'nico-rest-pg-creds', '-n', 'nico-rest'],
                     kubeconfig, check=False)
        if r.returncode == 0:
            break
        time.sleep(5)
    else:
        print('  ! nico-rest-pg-creds not synced after 120s — is rest.enabled true '
              'in nico-prereqs and the nico-rest namespace present?', file=sys.stderr)
        sys.exit(1)

    user = _kubectl(['get', 'secret', 'nico-rest-pg-creds', '-n', 'nico-rest',
                     '-o', 'jsonpath={.data.username}'], kubeconfig).stdout
    pw = _kubectl(['get', 'secret', 'nico-rest-pg-creds', '-n', 'nico-rest',
                   '-o', 'jsonpath={.data.password}'], kubeconfig).stdout
    import base64
    user = base64.b64decode(user).decode()
    pw = base64.b64decode(pw).decode()

    creds = tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False)
    creds.write(
        'nico-rest-common:\n  secrets:\n    dbCreds:\n'
        f'      username: "{user}"\n      password: "{pw}"\n'
        'nico-rest-workflow:\n  secrets:\n    dbCreds: "db-creds"\n'
        '  config:\n    db:\n'
        '      host: "nico-pg-cluster.postgres.svc.cluster.local"\n'
        '      name: "nico_rest"\n      user: "nico-rest.nico"\n')
    creds.close()

    try:
        print('  installing nico-rest umbrella...')
        _run(['helm', 'upgrade', '--install', 'nico-rest',
              str(Path(repo) / 'helm' / 'rest' / 'nico-rest'),
              '--namespace', 'nico-rest',
              '-f', str(Path(repo) / 'helm-prereqs' / 'values' / 'nico-rest.yaml'),
              '-f', str(dev_values),
              '-f', creds.name,
              '--set', f'global.image.repository={registry}',
              '--set', f'global.image.tag={tag}',
              '--timeout', '600s', '--wait'], kubeconfig, capture=False)
    finally:
        Path(creds.name).unlink(missing_ok=True)
    print('  nico-rest ✓')


def deploy_site_agent(repo, kubeconfig, registry, tag, s_uuid):
    """7i: pre-applied vault-issued client cert, per-site temporal namespace,
    then the site-agent chart. Flow gRPC disabled (Flow is off in nico-dev)."""
    chart = str(Path(repo) / 'helm' / 'rest' / 'nico-rest-site-agent')
    values = str(Path(repo) / 'helm-prereqs' / 'values' / 'nico-site-agent.yaml')
    base_args = ['--namespace', 'nico-rest', '-f', values,
                 '--set', f'global.image.repository={registry}',
                 '--set', f'global.image.tag={tag}']

    # Stale-registration guard (ported): a site-registration secret bound to a
    # different UUID must be recreated by the agent.
    r = _kubectl(['get', 'secret', 'site-registration', '-n', 'nico-rest',
                  '-o', 'jsonpath={.data.cluster_id}'], kubeconfig, check=False)
    if r.returncode == 0 and r.stdout:
        import base64
        prior = base64.b64decode(r.stdout).decode().strip()
        if prior and prior != s_uuid:
            print(f'  site-registration bound to stale UUID {prior} — deleting')
            _kubectl(['delete', 'secret', 'site-registration', '-n', 'nico-rest'],
                     kubeconfig, check=False)

    print('  pre-applying site-agent gRPC client certificate...')
    tpl = _run(['helm', 'template', 'nico-rest-site-agent', chart] + base_args +
               ['--show-only', 'templates/certificate.yaml'], kubeconfig)
    _kubectl(['apply', '-f', '-'], kubeconfig, stdin=tpl.stdout)
    _kubectl(['annotate', 'certificate/core-grpc-client-site-agent-certs',
              '-n', 'nico-rest',
              'meta.helm.sh/release-name=nico-rest-site-agent',
              'meta.helm.sh/release-namespace=nico-rest', '--overwrite'],
             kubeconfig, check=False)
    _kubectl(['label', 'certificate/core-grpc-client-site-agent-certs',
              '-n', 'nico-rest',
              'app.kubernetes.io/managed-by=Helm', '--overwrite'],
             kubeconfig, check=False)
    _kubectl(['wait', '--for=condition=Ready',
              'certificate/core-grpc-client-site-agent-certs',
              '-n', 'nico-rest', '--timeout=120s'], kubeconfig)

    ensure_temporal_namespace(s_uuid, kubeconfig)

    print('  installing nico-rest-site-agent...')
    _run(['helm', 'upgrade', '--install', 'nico-rest-site-agent', chart] + base_args +
         ['--set', f'envConfig.CLUSTER_ID={s_uuid}',
          '--set', f'envConfig.TEMPORAL_SUBSCRIBE_NAMESPACE={s_uuid}',
          '--set', 'envConfig.TEMPORAL_SUBSCRIBE_QUEUE=site',
          '--set', 'envConfig.FLOW_GRPC_ENABLED=false',
          '--timeout', '300s', '--wait'], kubeconfig, capture=False)

    # The site-agent attempts its NICo-core gRPC connection exactly ONCE at
    # startup (5s deadline); a transient failure leaves NicoClient nil
    # forever and all inventory activities panic (upstream setup.sh note).
    # Verify the connection landed; restart for a fresh attempt if not.
    print('  verifying site-agent → nico-core gRPC connection...')
    connected = False
    for _ in range(24):
        pods = _kubectl(['get', 'pods', '-n', 'nico-rest',
                         '-l', 'app.kubernetes.io/name=nico-rest-site-agent',
                         '-o', 'name'], kubeconfig,
                        check=False).stdout.strip().splitlines()
        if pods:
            r = _kubectl(['logs', '-n', 'nico-rest', pods[0], '--since=5m'],
                         kubeconfig, check=False)
            if 'NicoClient: successfully connected to server' in (r.stdout or ''):
                connected = True
                break
        time.sleep(5)
    if connected:
        print('  site-agent connected to nico-core gRPC ✓')
    else:
        print('  site-agent did not confirm the gRPC connection — '
              'restarting it for a fresh attempt...')
        _kubectl(['rollout', 'restart', 'statefulset/nico-rest-site-agent',
                  '-n', 'nico-rest'], kubeconfig)
        _kubectl(['rollout', 'status', 'statefulset/nico-rest-site-agent',
                  '-n', 'nico-rest', '--timeout=120s'], kubeconfig)
    print('  nico-rest-site-agent ✓')
