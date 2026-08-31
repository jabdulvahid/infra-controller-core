#!/usr/bin/env python3
"""
DC Simulation — Nico System Deployer

Installs the complete Nico stack on the simulation k8s cluster in dependency order:

  1. cert-manager      (jetstack)             — TLS certificate management
  2. Vault             (hashicorp, production) — PKI + secrets; init/unseal handled
  3. ESO               (external-secrets)     — distributes Vault secrets to k8s
  4. postgres-operator (zalando)              — CRD for HA PostgreSQL
  5. nico-prereqs      (helm-prereqs/)        — namespace, DB, Vault config, ESO resources
  6. nico              (helm/)                — all Nico core services

Vault is initialised and unsealed automatically on first run.
Unseal keys + root token stored in k8s Secret 'vault-unseal-keys' (vault namespace).
On cluster restart, run with --unseal to re-unseal without full redeploy.

Usage:
  python3 deploy-nico-system.py nico-sim.yaml
  python3 deploy-nico-system.py nico-sim.yaml --unseal       # re-unseal Vault only
  python3 deploy-nico-system.py nico-sim.yaml --destroy      # uninstall everything
  python3 deploy-nico-system.py nico-sim.yaml --regen-values # regenerate sim-values first
"""

import argparse
import base64
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: sudo apt install python3-yaml')
    sys.exit(1)


# ── Helm chart configuration ──────────────────────────────────────────────────

HELM_REPOS = {
    'jetstack':        'https://charts.jetstack.io',
    'hashicorp':       'https://helm.releases.hashicorp.com',
    'external-secrets':'https://charts.external-secrets.io',
    'sais':            'https://opensource.zalando.com/postgres-operator/charts/postgres-operator',
    'metallb':         'https://metallb.github.io/metallb',
}

# Deployment order matters — each step depends on the previous
DEPLOY_ORDER = [
    'metallb',
    'local-path-provisioner',  # StorageClass for PVCs (not in kubeadm by default)
    'cert-manager',
    'vault',
    'external-secrets',
    'postgres-operator',
    'nico-prereqs',
    'nico',
]

def local_path_provisioner_url(version):
    return (
        f'https://raw.githubusercontent.com/rancher/local-path-provisioner/'
        f'{version}/deploy/local-path-storage.yaml'
)

DESTROY_ORDER = list(reversed(DEPLOY_ORDER))


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Deploy Nico system on DC simulation k8s cluster',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl) or yaml file')
    p.add_argument('--unseal', action='store_true',
                   help='Re-unseal Vault only (use after cluster restart)')
    p.add_argument('--destroy', action='store_true',
                   help='Uninstall all Nico components in reverse order')
    p.add_argument('--skip-to', default=None,
                   metavar='RELEASE',
                   help=f'Skip to a specific release (one of: {", ".join(DEPLOY_ORDER)})')
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_chart_versions(sim):
    """Load pinned chart versions from nico-sim.yaml, failing loudly if any are missing."""
    versions = sim.get('chart-versions', {})
    required = [
        'metallb', 'local-path-provisioner', 'cert-manager',
        'vault', 'external-secrets', 'postgres-operator',
    ]
    missing = [k for k in required if not versions.get(k)]
    if missing:
        raise RuntimeError(
            f'Missing pinned chart versions in nico-sim.yaml chart-versions: '
            + ', '.join(missing)
        )
    return versions


def resolve_paths(sim, sim_yaml):
    """Return paths needed for deployment."""
    repo        = sim.get('infra_controller_repo', '')
    sim_dir     = Path(sim_yaml).parent
    site_name   = sim.get('fabric', {}).get('dc_name', 'nico-sim')
    values_dir  = Path(sim_yaml).parent / 'sim-values'
    helm_dir    = Path(repo) / 'helm'
    prereqs_dir = Path(repo) / 'helm-prereqs'
    return {
        'repo':        repo,
        'sim_dir':     sim_dir,
        'values_dir':  values_dir,
        'helm':        helm_dir,
        'prereqs':     prereqs_dir,
    }


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0]), str(p)
    return str(p), str(Path(p).parent)


def find_kubeconfig(sim, site_folder=None):
    """Find the simulation kubeconfig — site folder first, then ~/.kube fallback."""
    dc_name  = sim.get('fabric', {}).get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    kube_id  = f'{dc_name}-{sitename}' if sitename else dc_name
    candidates = []
    kube_filename = sim.get('kubeconfig')
    if kube_filename and site_folder:
        candidates.append(Path(site_folder) / kube_filename)
    candidates.append(Path(f'~/.kube/config-{kube_id}').expanduser())
    candidates.append(Path(os.environ.get('KUBECONFIG', '~/.kube/config')).expanduser())
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def kubectl_env(kubeconfig):
    env = os.environ.copy()
    if kubeconfig:
        env['KUBECONFIG'] = kubeconfig
    return env


# ── Shell helpers ─────────────────────────────────────────────────────────────

def run(cmd, env=None, capture=False, check=True, timeout=300, input_data=None):
    """Run a shell command, return CompletedProcess."""
    r = subprocess.run(
        cmd, env=env, capture_output=capture, text=True,
        timeout=timeout, check=False,
        input=input_data,
    )
    if check and r.returncode != 0:
        err = r.stderr.strip() if capture else ''
        raise RuntimeError(
            f'Command failed (exit {r.returncode}): {" ".join(str(c) for c in cmd)}'
            + (f'\n{err}' if err else '')
        )
    return r


def kubectl(args, env, capture=False, check=True, timeout=120, input_data=None):
    return run(['kubectl'] + args, env=env, capture=capture, check=check,
               timeout=timeout, input_data=input_data)


def helm(args, env, capture=False, check=True, timeout=600):
    return run(['helm'] + args, env=env, capture=capture, check=check, timeout=timeout)


# ── Preflight ─────────────────────────────────────────────────────────────────

INSTALL_HINTS = {
    'kubectl': 'See https://kubernetes.io/docs/tasks/tools/ or use snap install kubectl --classic',
    'helm':    'curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash',
}

def check_tools():
    for tool in ['kubectl', 'helm']:
        if run(['which', tool], capture=True, check=False).returncode != 0:
            hint = INSTALL_HINTS.get(tool, '')
            raise RuntimeError(
                f'{tool} not found in PATH.\n  Install: {hint}'
            )


def check_cluster(env):
    r = kubectl(['cluster-info'], env, capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            'Cannot reach k8s cluster. Set KUBECONFIG or run form-k8s-cluster.py first.')
    print('  Cluster reachable ✓')


def check_registry_images(sim):
    """Verify Nico images are in the local registry before deploying."""
    import ipaddress as _ip
    reg_prefix = sim['fabric'].get('registry_link',
                    sim['fabric'].get('utility_link', {})).get('prefix', '7.132.0.4/30')
    net    = _ip.IPv4Network(reg_prefix, strict=False)
    reg_ip = str(list(net.hosts())[1])
    port   = sim.get('nico_container_registry',
                sim.get('utility', {})).get('port', 5000)
    registry = f'{reg_ip}:{port}'
    tag = sim.get('nico-system', {}).get('image', {}).get('local_nico_tag', 'latest')

    import urllib.request, urllib.error
    try:
        url = f'http://{registry}/v2/nico/tags/list'
        resp = urllib.request.urlopen(url, timeout=5)
        data = json.loads(resp.read())
        tags = data.get('tags') or []
        if tag in tags:
            print(f'  nico:{tag} found in registry {registry} ✓')
        else:
            print(f'  ✗ nico:{tag} NOT in registry {registry} (found tags: {tags})')
            print(f'    Run: ~/claude-notes/nico-sim/build-nico-components.py <site>')
    except Exception as e:
        print(f'  ✗ Cannot reach registry {registry}: {e}')
        print(f'    Ensure registry VM is up and images are pushed.')


def ensure_sim_values(paths, site_yaml):
    """Always regenerate sim-values from the site yaml before deploying."""
    gen_script = Path(__file__).parent / 'generate-sim-values.py'
    print('  Generating sim-values...')
    run([sys.executable, str(gen_script), site_yaml])
    print('  sim-values generated ✓')


# ── Helm repos ────────────────────────────────────────────────────────────────

def ensure_helm_repos(env):
    r = helm(['repo', 'list', '-o', 'json'], env, capture=True, check=False)
    existing = {repo['name'] for repo in json.loads(r.stdout or '[]')}
    for name, url in HELM_REPOS.items():
        if name not in existing:
            helm(['repo', 'add', name, url], env)
            print(f'  added repo {name}')
    helm(['repo', 'update'], env, capture=True)
    print('  Helm repos up to date ✓')


# ── Namespace ─────────────────────────────────────────────────────────────────

def ensure_namespace(ns, env):
    r = kubectl(['get', 'namespace', ns], env, capture=True, check=False)
    if r.returncode != 0:
        kubectl(['create', 'namespace', ns], env)
        print(f'  created namespace {ns}')


# ── Wait helpers ──────────────────────────────────────────────────────────────

def wait_for_pods(namespace, selector, count, timeout=300, env=None):
    """Poll until `count` pods matching selector are Running."""
    print(f'  Waiting for {count} pod(s) in {namespace} ({selector})', end='', flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl(['get', 'pods', '-n', namespace, '-l', selector,
                     '--field-selector=status.phase=Running',
                     '--no-headers'], env, capture=True, check=False)
        running = len([l for l in r.stdout.splitlines() if l.strip()])
        if running >= count:
            print(f' ✓ ({running} running)')
            return
        time.sleep(5)
        print('.', end='', flush=True)
    print()
    raise RuntimeError(f'Timeout waiting for pods in {namespace} ({selector})')


def wait_for_job(namespace, job_name, timeout=300, env=None):
    """Wait for a k8s Job to complete."""
    print(f'  Waiting for job {job_name} in {namespace}', end='', flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl(['get', 'job', job_name, '-n', namespace,
                     '-o', 'jsonpath={.status.succeeded}'],
                    env, capture=True, check=False)
        if r.stdout.strip() == '1':
            print(' ✓ completed')
            return
        r2 = kubectl(['get', 'job', job_name, '-n', namespace,
                      '-o', 'jsonpath={.status.failed}'],
                     env, capture=True, check=False)
        if r2.stdout.strip() and int(r2.stdout.strip() or 0) > 3:
            raise RuntimeError(f'Job {job_name} failed')
        time.sleep(5)
        print('.', end='', flush=True)
    print()
    raise RuntimeError(f'Timeout waiting for job {job_name}')


# ── Vault init/unseal ─────────────────────────────────────────────────────────

VAULT_SECRET = 'vault-unseal-keys'
VAULT_NS     = 'vault'


def vault_exec(cmd, env, capture=True):
    """Run a command inside vault-0 pod."""
    return kubectl(
        ['exec', 'vault-0', '-n', VAULT_NS, '--'] + cmd,
        env, capture=capture, check=False,
    )


def vault_is_initialized(env):
    r = vault_exec(['vault', 'status', '-format=json'], env)
    if r.returncode in (0, 2):   # 0=unsealed, 2=sealed — both mean initialized
        try:
            return json.loads(r.stdout).get('initialized', False)
        except json.JSONDecodeError:
            pass
    return False


def vault_is_sealed(env):
    r = vault_exec(['vault', 'status', '-format=json'], env)
    try:
        return json.loads(r.stdout).get('sealed', True)
    except (json.JSONDecodeError, AttributeError):
        return True


def vault_init(env):
    """Initialize Vault, return (root_token, unseal_keys)."""
    print('  Initializing Vault...')
    r = vault_exec(
        ['vault', 'operator', 'init',
         '-key-shares=5', '-key-threshold=3', '-format=json'], env)
    if r.returncode != 0:
        raise RuntimeError(f'vault operator init failed: {r.stderr}')
    data        = json.loads(r.stdout)
    root_token  = data['root_token']
    unseal_keys = data['unseal_keys_b64']

    # Store in k8s Secret for re-use after restarts
    secret_data = {
        'root_token':  base64.b64encode(root_token.encode()).decode(),
    }
    for i, key in enumerate(unseal_keys):
        secret_data[f'unseal_key_{i}'] = base64.b64encode(key.encode()).decode()

    secret_json = json.dumps({
        'apiVersion': 'v1',
        'kind':       'Secret',
        'metadata':   {'name': VAULT_SECRET, 'namespace': VAULT_NS},
        'data':       secret_data,
    })
    r2 = run(
        ['kubectl', 'apply', '-f', '-'],
        env=env, capture=True,
        input_data=secret_json,
    )
    print(f'  Vault initialized, keys stored in Secret {VAULT_NS}/{VAULT_SECRET} ✓')
    return root_token, unseal_keys


def _run_with_stdin(cmd, input_data, env):
    """Run command with stdin data."""
    r = subprocess.run(cmd, input=input_data, env=env,
                       capture_output=True, text=True)
    return r


def vault_read_keys(env):
    """Read root token and unseal keys from k8s Secret."""
    r = kubectl(
        ['get', 'secret', VAULT_SECRET, '-n', VAULT_NS, '-o', 'json'],
        env, capture=True,
    )
    data = json.loads(r.stdout)['data']
    root_token  = base64.b64decode(data['root_token']).decode()
    unseal_keys = []
    i = 0
    while f'unseal_key_{i}' in data:
        unseal_keys.append(base64.b64decode(data[f'unseal_key_{i}']).decode())
        i += 1
    return root_token, unseal_keys


def vault_unseal_with_keys(unseal_keys, env):
    """Unseal Vault using stored keys (need 3 of 5)."""
    print('  Unsealing Vault', end='', flush=True)
    for key in unseal_keys[:3]:
        r = vault_exec(['vault', 'operator', 'unseal', key], env)
        if r.returncode != 0:
            raise RuntimeError(f'vault operator unseal failed: {r.stderr}')
        print('.', end='', flush=True)
    print(' ✓')


def wait_for_vault_pod(env, timeout=300):
    """Wait for vault-0 to be in Running state (may still be sealed)."""
    print('  Waiting for vault-0 pod', end='', flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = kubectl(
            ['get', 'pod', 'vault-0', '-n', VAULT_NS,
             '-o', 'jsonpath={.status.phase}'],
            env, capture=True, check=False,
        )
        phase = r.stdout.strip()
        if phase == 'Running':
            print(' ✓ Running')
            return
        time.sleep(5)
        print('.', end='', flush=True)
    print()
    # Show diagnostic info before failing
    r = kubectl(['get', 'pod', 'vault-0', '-n', VAULT_NS, '-o', 'wide'],
                env, capture=True, check=False)
    print(r.stdout.strip())
    r2 = kubectl(['describe', 'pod', 'vault-0', '-n', VAULT_NS],
                 env, capture=True, check=False)
    # Show just Events section
    for line in r2.stdout.splitlines():
        if 'Events:' in line or 'Warning' in line or 'FailedScheduling' in line \
                or 'FailedMount' in line or 'Pending' in line:
            print(f'  {line}')
    raise RuntimeError(
        'Timeout waiting for vault-0 pod to be Running.\n'
        '  Common cause: local-path StorageClass missing.\n'
        '  Fix: python3 deploy-nico-system.py nico-sim.yaml --skip-to vault\n'
        '  after installing local-path-provisioner (see Step 0 below).'
    )


def deploy_and_configure_vault(paths, sim, env, version):
    """Install Vault and return root token.

    Dev mode:  auto-initialized and unsealed — just install and wait for Ready.
               Root token is the fixed devRootToken from nico-sim.yaml.
    Prod mode: install without --wait (starts sealed), then init/unseal manually,
               storing keys in k8s Secret for re-use after restarts.
    """
    vault_cfg   = sim['nico-system']['vault']
    mode        = vault_cfg.get('mode', 'dev')
    values_file = str(paths['values_dir'] / 'vault.yaml')
    ns          = VAULT_NS

    ensure_namespace(ns, env)
    print(f'  helm upgrade --install vault hashicorp/vault --version {version} -n {ns} (mode: {mode})')

    if mode == 'dev':
        # Dev mode: helm --wait works because Vault auto-unseals
        helm([
            'upgrade', '--install', 'vault', 'hashicorp/vault',
            '--version', version,
            '-n', ns, '-f', values_file,
            '--wait', '--timeout', '3m',
        ], env)
        root_token = vault_cfg.get('dev_root_token', 'root')
        print(f'  Vault dev mode Ready ✓ (root token: {root_token})')
        return root_token

    # Production mode: starts sealed, must init and unseal manually
    helm([
        'upgrade', '--install', 'vault', 'hashicorp/vault',
        '--version', version,
        '-n', ns, '-f', values_file,
        '--timeout', '5m',
        # No --wait: pod starts Running but sealed (not Ready)
    ], env)

    wait_for_vault_pod(env)
    time.sleep(5)  # brief settle before querying status

    if vault_is_initialized(env):
        print('  Vault already initialized — reading stored keys')
        root_token, unseal_keys = vault_read_keys(env)
    else:
        root_token, unseal_keys = vault_init(env)

    if vault_is_sealed(env):
        vault_unseal_with_keys(unseal_keys, env)
    else:
        print('  Vault already unsealed ✓')

    kubectl(['wait', 'pod/vault-0', '-n', VAULT_NS,
             '--for=condition=Ready', '--timeout=60s'], env)
    print('  Vault production mode Ready ✓')
    return root_token


# ── Individual deployers ──────────────────────────────────────────────────────

def deploy_metallb(paths, sim, env, version):
    fab = sim['fabric']
    cp  = sim['control_plane']
    np  = sim['nico-system']['helm-values'].get('net-plan', {})

    metallb_asn  = int(np.get('metallb_asn', 0))   # myASN / bgpAsnStart
    dpu_asn_base = fab['dpu_asn_base']
    num_vms      = cp['num_vms']
    vp           = cp.get('vm_prefix', 'cp')
    dp           = cp.get('dpu_prefix', f'{vp}-dpu')
    dc_name      = fab.get('dc_name', 'nico-sim')
    sitename     = sim['nico-system']['helm-values'].get('sitename', '')
    name_pfx     = f'{dc_name}-{sitename}-' if sitename else f'{dc_name}-'
    service_vips = np.get('service_vips', '')

    cp_prefix = cp.get('control_plane_prefix', '7.132.0.0/29')
    cp_net    = ipaddress.IPv4Network(cp_prefix, strict=False)
    cp_pairs  = list(cp_net.subnets(new_prefix=31))

    ensure_namespace('metallb-system', env)
    print(f'  helm upgrade --install metallb metallb/metallb --version {version} -n metallb-system')
    helm(['upgrade', '--install', 'metallb', 'metallb/metallb',
          '--version', version,
          '-n', 'metallb-system', '--wait', '--timeout', '5m'], env)
    kubectl(['wait', '--for=condition=Established',
             'crd/ipaddresspools.metallb.io', '--timeout=60s'], env)

    parts = []
    parts.append('---')
    parts.append('apiVersion: metallb.io/v1beta1')
    parts.append('kind: IPAddressPool')
    parts.append('metadata:')
    parts.append('  name: nico-vips')
    parts.append('  namespace: metallb-system')
    parts.append('spec:')
    parts.append('  addresses:')
    parts.append(f'    - {service_vips}')
    parts.append('  autoAssign: false')

    for i in range(1, num_vms + 1):
        pair_hosts   = list(cp_pairs[i - 1].hosts())
        dpu_internal = str(pair_hosts[0])
        parts.append('---')
        parts.append('apiVersion: metallb.io/v1beta2')
        parts.append('kind: BGPPeer')
        parts.append('metadata:')
        parts.append(f'  name: {dp}-{i}')
        parts.append('  namespace: metallb-system')
        parts.append('spec:')
        parts.append(f'  myASN: {metallb_asn}')
        parts.append(f'  peerASN: {dpu_asn_base + i}')
        parts.append(f'  peerAddress: {dpu_internal}')
        parts.append('  nodeSelectors:')
        parts.append('    - matchLabels:')
        parts.append(f'        kubernetes.io/hostname: {name_pfx}{vp}-{i}')

    parts.append('---')
    parts.append('apiVersion: metallb.io/v1beta1')
    parts.append('kind: BGPAdvertisement')
    parts.append('metadata:')
    parts.append('  name: nico-vips')
    parts.append('  namespace: metallb-system')
    parts.append('spec:')
    parts.append('  ipAddressPools:')
    parts.append('    - nico-vips')

    kubectl(['apply', '-f', '-'], env, input_data='\n'.join(parts) + '\n')
    print('  MetalLB Ready ✓')


def deploy_local_path_provisioner(env, version):
    """Install Rancher local-path-provisioner to provide local-path StorageClass.
    kubeadm has no default StorageClass — Vault, PostgreSQL PVCs need this."""
    r = kubectl(['get', 'storageclass', 'local-path'],
                env, capture=True, check=False)
    if r.returncode == 0:
        print('  local-path StorageClass already exists ✓')
        return
    url = (
        f'https://raw.githubusercontent.com/rancher/local-path-provisioner/'
        f'{version}/deploy/local-path-storage.yaml'
    )
    print(f'  Installing local-path-provisioner {version} (provides local-path StorageClass)...')
    kubectl(['apply', '-f', url], env)
    # Wait for provisioner pod
    wait_for_pods('local-path-storage', 'app=local-path-provisioner', 1,
                  timeout=120, env=env)
    # Make local-path the default StorageClass
    kubectl([
        'patch', 'storageclass', 'local-path',
        '-p', '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}',
    ], env)
    print('  local-path-provisioner installed and set as default StorageClass ✓')


def deploy_cert_manager(paths, env, version):
    values = str(paths['values_dir'] / 'cert-manager.yaml')
    ensure_namespace('cert-manager', env)
    print(f'  helm upgrade --install cert-manager jetstack/cert-manager --version {version}')
    helm([
        'upgrade', '--install', 'cert-manager', 'jetstack/cert-manager',
        '--version', version,
        '-n', 'cert-manager',
        '-f', values,
        '--wait', '--timeout', '5m',
    ], env)
    print('  cert-manager Ready ✓')


def deploy_eso(paths, env, version):
    values = str(paths['values_dir'] / 'eso.yaml')
    ensure_namespace('external-secrets', env)
    print(f'  helm upgrade --install external-secrets external-secrets/external-secrets --version {version}')
    helm([
        'upgrade', '--install', 'external-secrets',
        'external-secrets/external-secrets',
        '--version', version,
        '-n', 'external-secrets',
        '-f', values,
        '--wait', '--timeout', '5m',
    ], env)

    # Wait for all ESO pods to be Running. This is the meaningful readiness
    # signal — if the controller, cert-controller, and webhook pods are all up,
    # ESO is ready to serve. Previous attempts to use CRD discovery checks and
    # dry-run apply were defeated by kubectl's REST mapper cache issues and are
    # not reliable gates.
    print('  Waiting for ESO pods', end='', flush=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        r = kubectl(
            ['get', 'pods', '-n', 'external-secrets',
             '--field-selector=status.phase=Running',
             '--no-headers'],
            env, capture=True, check=False,
        )
        running = [l for l in r.stdout.splitlines() if l.strip()]
        if len(running) >= 3:
            print(f' ✓ ({len(running)} running)')
            break
        time.sleep(3)
        print('.', end='', flush=True)
    else:
        print()
        raise RuntimeError('ESO pods not all running after 120s')
    print('  ESO Ready ✓')


def deploy_postgres_op(paths, env, version):
    values = str(paths['values_dir'] / 'zalando-postgres-op.yaml')
    ensure_namespace('postgres', env)
    print(f'  helm upgrade --install postgres-operator sais/postgres-operator --version {version}')
    helm([
        'upgrade', '--install', 'postgres-operator',
        'sais/postgres-operator',
        '--version', version,
        '-n', 'postgres',
        '-f', values,
        '--wait', '--timeout', '5m',
    ], env)
    print('  postgres-operator Ready ✓')


def deploy_nico_prereqs(paths, root_token, env):
    values    = str(paths['values_dir'] / 'nico-prereqs.yaml')
    chart_dir = str(paths['prereqs'])
    ns        = 'nico-system'
    # Pre-create nico-system with helm ownership labels so helm can adopt it.
    # The chart has templates/namespace.yaml, so helm must own the namespace.
    # --create-namespace creates it without labels → helm can't adopt its own
    # template resource. Pre-creating with the exact ownership metadata lets
    # helm adopt the namespace and then apply the template (which adds the
    # nico.nvidia.com/managed label and keep annotations) without conflict.
    ns_yaml = (
        'apiVersion: v1\nkind: Namespace\nmetadata:\n'
        f'  name: {ns}\n'
        '  labels:\n'
        '    app.kubernetes.io/managed-by: "Helm"\n'
        '  annotations:\n'
        '    meta.helm.sh/release-name: "nico-prereqs"\n'
        f'    meta.helm.sh/release-namespace: "{ns}"\n'
    )
    kubectl(['apply', '-f', '-'], env, input_data=ns_yaml, capture=True, check=False)

    # Pre-create ssh-host-key in OpenSSH format before nico-prereqs installs.
    # The chart uses genPrivateKey "ed25519" which produces PKCS8 PEM format, but
    # nico-ssh-console-rs requires OPENSSH PRIVATE KEY format. The chart's _helpers.tpl
    # lookup reuses an existing secret, so pre-creating it with an ssh-keygen key
    # gives the server what it needs without changing the chart.
    r_key = kubectl(['get', 'secret', 'ssh-host-key', '-n', ns],
                    env, capture=True, check=False)
    if r_key.returncode != 0:
        import subprocess as _sp, tempfile as _tf, os as _os
        with _tf.TemporaryDirectory() as tmpdir:
            keyfile = f'{tmpdir}/ssh_host_ed25519_key'
            _sp.run(
                ['ssh-keygen', '-t', 'ed25519', '-f', keyfile, '-N', '', '-C', ''],
                capture_output=True, env=_os.environ,
            )
            private_key = open(keyfile).read()
        secret_yaml = (
            'apiVersion: v1\nkind: Secret\n'
            'metadata:\n'
            f'  name: ssh-host-key\n'
            f'  namespace: {ns}\n'
            '  labels:\n'
            '    app.kubernetes.io/managed-by: "Helm"\n'
            '  annotations:\n'
            '    meta.helm.sh/release-name: "nico-prereqs"\n'
            f'    meta.helm.sh/release-namespace: "{ns}"\n'
            'type: Opaque\n'
            'stringData:\n'
            '  ssh_host_ed25519_key: |\n'
            + ''.join(f'    {line}\n' for line in private_key.splitlines())
            + '  ssh_host_ed25519_key_pub: ""\n'
        )
        kubectl(['apply', '-f', '-'], env, input_data=secret_yaml, capture=True, check=False)
        print('  Pre-created ssh-host-key secret in OpenSSH format ✓')

    # Diagnostic: show what ESO CRDs and versions are actually registered
    # before helm runs, so failures can be diagnosed without re-running.
    r = kubectl(['get', 'crd', '-o',
                 'custom-columns=NAME:.metadata.name,VERSIONS:.spec.versions[*].name',
                 '--no-headers'], env, capture=True, check=False)
    eso_crds = [l for l in r.stdout.splitlines() if 'external-secrets' in l]
    if eso_crds:
        print('  ESO CRDs registered:')
        for l in eso_crds:
            print(f'    {l}')
    else:
        print('  WARNING: no external-secrets CRDs found — ESO may not have installed CRDs')

    r2 = kubectl(['api-resources', '--api-group=external-secrets.io',
                  '--verbs=list', '--no-headers'], env, capture=True, check=False)
    api_lines = [l for l in r2.stdout.splitlines() if l.strip()]
    print(f'  ESO api-resources ({len(api_lines)} types): {[l.split()[0] for l in api_lines]}')

    print(f'  helm upgrade --install nico-prereqs {chart_dir}')
    helm([
        'upgrade', '--install', 'nico-prereqs', chart_dir,
        '-n', ns,
        '-f', values,
        '--set', f'vault.token={root_token}',
        '--wait', '--timeout', '10m',
    ], env)
    # Wait for vault-config-job to complete
    try:
        wait_for_job(ns, 'nico-prereqs-vault-config', timeout=300, env=env)
    except RuntimeError:
        print('  Note: vault-config-job may be named differently — check manually')
    print('  nico-prereqs Ready ✓')


def deploy_nico(paths, sim, env):
    values    = str(paths['values_dir'] / 'nico.yaml')
    chart_dir = str(paths['helm'])
    ns        = 'nico-system'
    print(f'  helm upgrade --install nico {chart_dir}')
    helm([
        'upgrade', '--install', 'nico', chart_dir,
        '-n', ns,
        '-f', values,
        '--wait', '--timeout', '15m',
    ], env)
    _patch_nico_api_config(ns, env)
    print('  nico Ready ✓')


def _patch_nico_api_config(ns, env):
    """Append simulation-only settings to all nico-api config keys in the ConfigMap.

    configFiles.nicoApiConfig in helm REPLACES the full config, so we can't
    use it. Instead we patch the rendered ConfigMap after deploy to append
    only our additions to ALL config keys, since the binary may load either
    nico-api-config.toml (nico image) or carbide-api-config.toml (legacy image).

    Settings added:
      [machines]
      allow_insecure_discovery = true   — MAT DPU source IPs don't match Nico's
                                          interface records (simulated MACs/IPs).
                                          Skips the IP-ownership check so DPUs
                                          can progress past DPUInitializing/Init.
    """
    import json as _json
    import tempfile, os

    cm_name = 'nico-api-config-files'
    # allow_insecure_discovery is a TOP-LEVEL field in CarbideConfig (not under [machines]).
    addition = '\nallow_insecure_discovery = true\n'
    config_keys = ['nico-api-config.toml', 'carbide-api-config.toml']

    # Fetch the full ConfigMap as JSON to preserve all keys
    r = kubectl(['get', 'configmap', cm_name, '-n', ns, '-o', 'json'],
                env, capture=True, check=False)
    if r.returncode != 0:
        print(f'  Warning: could not read {cm_name} — skipping patch')
        return

    cm = _json.loads(r.stdout)
    data = cm.get('data', {})
    patched_any = False

    for key in config_keys:
        if key not in data:
            continue
        if 'allow_insecure_discovery' in data[key]:
            print(f'  {cm_name}/{key}: allow_insecure_discovery already set ✓')
            continue
        # Insert BEFORE the first [section] header so it stays at TOML root scope.
        # In TOML, values after a [section] header belong to that section — appending
        # at the end would put allow_insecure_discovery inside [rms] or similar.
        lines = data[key].splitlines(keepends=True)
        insert_pos = next(
            (i for i, l in enumerate(lines) if l.strip().startswith('[')), len(lines)
        )
        lines.insert(insert_pos, addition.lstrip('\n') + '\n')
        data[key] = ''.join(lines)
        patched_any = True
        print(f'  {cm_name}/{key}: inserting allow_insecure_discovery=true at root level')

    if not patched_any:
        return

    # Use kubectl patch --type=merge to update only the data keys we changed.
    # Safer than kubectl apply (which requires managedFields/resourceVersion
    # to be correct) and preserves all other ConfigMap keys unchanged.
    patch_data = _json.dumps({'data': {k: data[k] for k in config_keys if k in data}})
    patch = kubectl(['patch', 'configmap', cm_name, '-n', ns,
                     '--type=merge', '-p', patch_data],
                    env, capture=True, check=False)
    if patch.returncode == 0:
        kubectl(['rollout', 'restart', 'deployment/nico-api', '-n', ns], env)
        kubectl(['rollout', 'status', 'deployment/nico-api', '-n', ns,
                 '--timeout=120s'], env)
        print(f'  {cm_name}: patched and nico-api restarted ✓')
    else:
        print(f'  Warning: failed to patch {cm_name}: {patch.stderr}')



# ── Destroy ───────────────────────────────────────────────────────────────────

RELEASE_NAMESPACES = {
    'nico':                     'nico-system',
    'nico-prereqs':              'nico-system',
    'postgres-operator':         'postgres',
    'external-secrets':          'external-secrets',
    'vault':                     'vault',
    'cert-manager':              'cert-manager',
    'local-path-provisioner':    'local-path-storage',
    'metallb':                   'metallb-system',
}


def destroy_all(env, versions):
    """Uninstall all Nico releases and clean up namespaces for a clean re-deploy."""
    print('Step 1: Uninstalling Nico releases (reverse order)...')
    for release in DESTROY_ORDER:
        ns = RELEASE_NAMESPACES.get(release, 'default')
        if release == 'local-path-provisioner':
            r = kubectl(['delete', '-f', local_path_provisioner_url(versions['local-path-provisioner'])],
                        env, capture=True, check=False)
            print(f'  removed {release}' if r.returncode == 0
                  else f'  {release} not found or already removed')
            continue
        r = helm(['uninstall', release, '-n', ns], env, capture=True, check=False)
        print(f'  uninstalled {release}' if r.returncode == 0
              else f'  {release} not found or already removed')

    # Delete ESO CRDs explicitly — helm uninstall never removes CRDs (by design).
    # If CRDs from a different ESO version remain, the next install skips them
    # (helm won't upgrade CRDs either) and we end up with the wrong CRD versions.
    # Deleting here ensures the pinned ESO version installs its own CRDs fresh.
    print()
    print('Step 2: Removing ESO CRDs...')
    r = kubectl(['get', 'crd', '-o', 'name'], env, capture=True, check=False)
    eso_crds = [l.split('/')[-1] for l in r.stdout.splitlines()
                if 'external-secrets' in l]
    if eso_crds:
        kubectl(['delete', 'crd'] + eso_crds + ['--ignore-not-found=true'],
                env, capture=True, check=False)
        print(f'  deleted {len(eso_crds)} ESO CRDs')
    else:
        print('  no ESO CRDs found')

    # Delete namespaces — required because nico-system has helm.sh/resource-policy: keep
    # which prevents helm uninstall from removing it. Force-delete all managed namespaces.
    print()
    print('Step 3: Removing namespaces...')
    managed_ns = ['nico-system', 'vault', 'external-secrets', 'postgres',
                  'cert-manager', 'local-path-storage', 'metallb-system']
    for ns in managed_ns:
        r = kubectl(['delete', 'namespace', ns, '--ignore-not-found=true',
                     '--timeout=60s'], env, capture=True, check=False)
        if r.returncode == 0:
            print(f'  removed namespace {ns}')
        else:
            print(f'  namespace {ns}: {r.stderr.strip()[:80]}')

    print()
    print('Destroy complete. Re-deploy with:')
    print('  python3 deploy-nico-system.py nico-sim.yaml')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args     = parse_args()
    site_yaml, site_folder = resolve_site(args.site)
    sim      = load_sim(site_yaml)

    kubeconfig = find_kubeconfig(sim, site_folder)
    env        = kubectl_env(kubeconfig)
    paths      = resolve_paths(sim, site_yaml)

    print('DC Simulation — Nico System Deployer')
    print(f'  sim config  : {site_yaml}')
    print(f'  repo        : {paths["repo"]}')
    print(f'  kubeconfig  : {kubeconfig or "default"}')
    print(f'  sim-values  : {paths["values_dir"]}')
    print()

    # Preflight
    check_tools()
    check_cluster(env)
    check_registry_images(sim)
    print()

    versions = load_chart_versions(sim)

    if args.destroy:
        destroy_all(env, versions)
        return

    if args.unseal:
        print('Re-unsealing Vault...')
        wait_for_vault_pod(env)
        _, unseal_keys = vault_read_keys(env)
        if vault_is_sealed(env):
            vault_unseal_with_keys(unseal_keys, env)
        else:
            print('  Vault is already unsealed ✓')
        return

    # Ensure sim-values are generated
    print('Checking sim-values...')
    ensure_sim_values(paths, site_yaml)
    print()

    # Add Helm repos
    print('Checking Helm repos...')
    ensure_helm_repos(env)
    print()

    # Determine start point
    skip_set = set()
    if args.skip_to:
        if args.skip_to not in DEPLOY_ORDER:
            print(f'Error: unknown release "{args.skip_to}". '
                  f'Choose from: {", ".join(DEPLOY_ORDER)}', file=sys.stderr)
            sys.exit(1)
        skip_set = set(DEPLOY_ORDER[:DEPLOY_ORDER.index(args.skip_to)])
        print(f'Skipping to {args.skip_to} (skipping: {", ".join(skip_set)})')
        print()

    root_token = None

    INTER_STEP_WAIT = 20   # seconds between components — avoids API server race conditions

    first = True
    for release in DEPLOY_ORDER:
        if release in skip_set:
            continue

        if not first:
            print(f'  Waiting {INTER_STEP_WAIT}s before next component...', flush=True)
            time.sleep(INTER_STEP_WAIT)
        first = False

        print(f'── {release} {"─" * (50 - len(release))}')

        if release == 'metallb':
            deploy_metallb(paths, sim, env, versions['metallb'])

        elif release == 'local-path-provisioner':
            deploy_local_path_provisioner(env, versions['local-path-provisioner'])

        elif release == 'cert-manager':
            deploy_cert_manager(paths, env, versions['cert-manager'])

        elif release == 'vault':
            root_token = deploy_and_configure_vault(paths, sim, env, versions['vault'])

        elif release == 'external-secrets':
            deploy_eso(paths, env, versions['external-secrets'])

        elif release == 'postgres-operator':
            deploy_postgres_op(paths, env, versions['postgres-operator'])

        elif release == 'nico-prereqs':
            if root_token is None:
                vault_cfg = sim['nico-system']['vault']
                if vault_cfg.get('mode', 'dev') == 'dev':
                    root_token = vault_cfg.get('dev_root_token', 'root')
                    print(f'  Using dev mode root token: {root_token}')
                else:
                    root_token, _ = vault_read_keys(env)
            deploy_nico_prereqs(paths, root_token, env)

        elif release == 'nico':
            deploy_nico(paths, sim, env)

        print()

    print('=' * 60)
    print('Nico system deployed successfully.')
    print()
    print('Verify:')
    print(f'  kubectl get pods -n nico-system')
    print(f'  kubectl get pods -n vault')
    print(f'  kubectl get pods -n postgres')
    print()
    print('On cluster restart, unseal Vault:')
    print(f'  python3 {Path(__file__).name} {site_yaml} --unseal')
    print('=' * 60)


if __name__ == '__main__':
    main()
