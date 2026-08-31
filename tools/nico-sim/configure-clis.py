#!/usr/bin/env python3
"""
nico-sim — Configure MAT and admin-cli for a site

Generates certificates, config files, and run scripts for:
  - MAT (Machine-a-tron): simulates managed host BMC/DHCP discovery
  - nico-admin-cli: operator CLI for day-1 Nico site setup

Output layout under <site-folder>/certs/:
  mat/
    mat-ca.pem          — site root CA (from nico-roots secret)
    mat-client.pem      — MAT client cert (SPIFFE: machine-a-tron)
    mat-client-key.pem  — MAT client key
    mat/mat-config.toml — MAT configuration (separate from certs)
  admin/
    ca.pem              — site root CA
    client.pem          — admin-cli client cert
    client-key.pem      — admin-cli client key

Scripts generated in <site-folder>/:
  run-mat.sh            — launch MAT with certs pre-configured
  run-admin-cli.sh      — launch nico-admin-cli with certs pre-configured

/etc/hosts entry added (site-specific, safe for multi-site):
  {api_vip} nico-api.{dc_name}-{sitename}

Usage:
  ./configure-clis.py ~/sites/ytl
  ./configure-clis.py ~/sites/ytl --dry-run
"""

import argparse
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


def resolve_site(arg):
    p = Path(arg).expanduser()
    if p.is_dir():
        yamls = [f for f in p.glob('*.yaml') if not f.stem.endswith('-mac') and '.kubeconfig' not in f.name]
        if not yamls:
            print(f'Error: no site yaml in {p}', file=sys.stderr); sys.exit(1)
        if len(yamls) > 1:
            print(f'Error: multiple yamls in {p}', file=sys.stderr); sys.exit(1)
        return str(yamls[0])
    return str(p)


def parse_args():
    p = argparse.ArgumentParser(
        description='Configure MAT and admin-cli certs/scripts for a nico-sim site',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/ytl)')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be done without making changes')
    p.add_argument('--skip-hosts', action='store_true',
                   help='Skip /etc/hosts update')
    return p.parse_args()


def load_sim(path):
    with open(path) as f:
        return yaml.safe_load(f)


def find_kubeconfig(sim, site_folder=None):
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
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
    raise RuntimeError(
        f'No kubeconfig found. Run form-k8s-cluster.py first. '
        f'Expected: {site_folder}/{kube_filename or kube_id + ".kubeconfig.yaml"}')


def kubectl(args, kubeconfig, capture=True):
    env = os.environ.copy()
    env['KUBECONFIG'] = kubeconfig
    r = subprocess.run(['kubectl'] + args, capture_output=capture, text=True, env=env)
    return r


def get_ca_cert(kubeconfig):
    """Fetch the site root CA from the nico-roots secret."""
    r = kubectl([
        'get', 'secret', 'nico-roots', '-n', 'nico-system',
        '-o', "jsonpath={.data.ca\\.crt}"
    ], kubeconfig)
    if r.returncode != 0:
        raise RuntimeError(f'Failed to get nico-roots secret: {r.stderr.strip()}')
    import base64
    return base64.b64decode(r.stdout.strip()).decode()


def issue_cert_via_vault(kubeconfig, common_name, uri_sans=None, ttl='720h'):
    """Issue a cert from Vault PKI via port-forward."""
    print(f'  Port-forwarding Vault...')
    pf = subprocess.Popen(
        ['kubectl', '-n', 'vault', 'port-forward', 'vault-0', '8200:8200'],
        env={**os.environ, 'KUBECONFIG': kubeconfig},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    try:
        payload = {'common_name': common_name, 'ttl': ttl}
        if uri_sans:
            payload['uri_sans'] = uri_sans

        cmd = [
            'curl', '-sf',
            '-H', 'X-Vault-Token: root',
            '-H', 'Content-Type: application/json',
            '-d', json.dumps(payload),
            'http://127.0.0.1:8200/v1/nicoca/issue/nico-cluster'
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f'Vault cert issue failed: {r.stderr}')

        data = json.loads(r.stdout).get('data', {})
        if not data.get('certificate'):
            raise RuntimeError(f'No certificate in Vault response: {r.stdout[:500]}')
        return data['certificate'], data['private_key']
    finally:
        pf.terminate()
        pf.wait()


def update_hosts(hostname, ip, dry_run):
    """Add hostname→IP to /etc/hosts if not already present."""
    hosts_path = Path('/etc/hosts')
    content = hosts_path.read_text()
    entry = f'{ip} {hostname}'

    if hostname in content:
        # Check if the IP matches
        for line in content.splitlines():
            if hostname in line and line.strip().startswith(ip):
                print(f'  /etc/hosts: {entry} already present ✓')
                return
        print(f'  /etc/hosts: {hostname} exists with different IP — update manually')
        return

    if dry_run:
        print(f'  [dry-run] would add to /etc/hosts: {entry}')
        return

    print(f'  Adding to /etc/hosts: {entry}')
    r = subprocess.run(
        ['sudo', 'tee', '-a', str(hosts_path)],
        input=f'\n{entry}  # nico-sim {hostname}\n',
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f'  Warning: could not update /etc/hosts: {r.stderr.strip()}')
        print(f'  Add manually: echo "{entry}" | sudo tee -a /etc/hosts')


def gen_mat_toml(sim, site_folder):
    """Generate MAT config.toml from site yaml."""
    hv       = sim.get('nico-system', {}).get('helm-values', {})
    np       = hv.get('net-plan', {})
    api_vip  = np.get('api_vip', '')
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = hv.get('sitename', dc_name)
    networks = hv.get('networks', {})
    mat_net  = networks.get('rack-mat-hosts', {})
    admin_net = networks.get('admin', {})
    mat_gw   = mat_net.get('gateway', '')
    admin_gw = admin_net.get('gateway', '')
    certs_dir = Path(site_folder) / 'certs' / 'mat'

    return f'''\
# MAT (Machine-a-tron) configuration for site
# Generated by configure-clis.py
carbide_api_url = "https://nico-api.{dc_name}-{sitename}:443"
interface = "br-{dc_name}-internet"
tui_enabled = false
log_file = "/tmp/mat-{dc_name}.log"
cleanup_on_quit = true
register_expected_machines = true
bmc_mock_port = 443  # must be 443 (Nico hardcodes DEFAULT_BMC_HTTPS_PORT=443)

[dhcp]
type = "api"   # API mode — no physical DHCP relay needed

[machines.rack-mat-hosts]
hw_type = "wiwynn_gb200_nvl"
host_count = 2
dpu_per_host_count = 1
vpc_count = 0
subnets_per_vpc = 0
oob_dhcp_relay_address = "{mat_gw}"    # rack-mat-hosts gateway
admin_dhcp_relay_address = "{admin_gw}"  # admin network gateway
host_reboot_delay = 10
dpu_reboot_delay = 5
'''


def gen_run_mat_sh(site_folder, binary_hint='machine-a-tron'):
    abs_site = Path(site_folder).expanduser().resolve()
    return f'''\
#!/usr/bin/env bash
# Run MAT for this site. Requires {binary_hint} in $PATH.
#
# bmc_mock_port=443 requires CAP_NET_BIND_SERVICE. Grant once after each build:
#   sudo setcap cap_net_bind_service=+ep $(which {binary_hint})

set -euo pipefail
SITE="{abs_site}"

FORGE_ROOT_CA_PATH="$SITE/certs/mat/mat-ca.pem" \\
CLIENT_CERT_PATH="$SITE/certs/mat/mat-client.pem" \\
CLIENT_KEY_PATH="$SITE/certs/mat/mat-client-key.pem" \\
{binary_hint} "$SITE/mat/mat-config.toml" "$@"
'''


def gen_run_admin_cli_sh(sim, site_folder, binary_hint='nico-admin-cli'):
    hv       = sim.get('nico-system', {}).get('helm-values', {})
    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = hv.get('sitename', dc_name)
    api_host = f'nico-api.{dc_name}-{sitename}'
    abs_site = Path(site_folder).expanduser().resolve()
    return f'''\
#!/usr/bin/env bash
# Run nico-admin-cli for this site. Requires {binary_hint} in $PATH.
#
# Note: the 'version' subcommand shows 'IGNORING SERVER CERT' — this is expected.
# All other subcommands perform real TLS verification with the site CA.

set -euo pipefail
SITE="{abs_site}"

API_URL="https://{api_host}:443" \\
ROOT_CA_PATH="$SITE/certs/admin/ca.pem" \\
CLIENT_CERT_PATH="$SITE/certs/admin/client.pem" \\
CLIENT_KEY_PATH="$SITE/certs/admin/client-key.pem" \\
{binary_hint} "$@"
'''


def main():
    args     = parse_args()
    dry      = args.dry_run
    site_yaml = resolve_site(args.site)
    site_folder = Path(site_yaml).parent
    sim      = load_sim(site_yaml)

    dc_name  = sim['fabric'].get('dc_name', 'nico-sim')
    sitename = sim.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    api_vip  = sim.get('nico-system', {}).get('helm-values', {}).get(
                   'net-plan', {}).get('api_vip', '')

    print('nico-sim — Configure CLIs')
    print(f'  site      : {site_folder}')
    print(f'  dc_name   : {dc_name}  sitename: {sitename}')
    print(f'  api_vip   : {api_vip}')
    if dry:
        print(f'  DRY RUN   : no files written')
    print()

    kubeconfig = find_kubeconfig(sim, str(site_folder))
    print(f'  kubeconfig: {kubeconfig}')
    print()

    # ── Create directories ────────────────────────────────────────────────────
    mat_dir   = site_folder / 'certs' / 'mat'   # MAT TLS certs
    admin_dir = site_folder / 'certs' / 'admin'  # admin-cli TLS certs
    mat_cfg_dir = site_folder / 'mat'             # MAT config (separate from certs)
    if not dry:
        mat_dir.mkdir(parents=True, exist_ok=True)
        admin_dir.mkdir(parents=True, exist_ok=True)
        mat_cfg_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Fetch CA cert ─────────────────────────────────────────────────
    print('Step 1: Fetch site CA from nico-roots secret')
    ca_cert = get_ca_cert(kubeconfig)
    print(f'  CA cert fetched ({len(ca_cert)} bytes) ✓')

    if not dry:
        (mat_dir / 'mat-ca.pem').write_text(ca_cert)
        (admin_dir / 'ca.pem').write_text(ca_cert)
        print(f'  Written: {mat_dir}/mat-ca.pem')
        print(f'  Written: {admin_dir}/ca.pem')
    print()

    # ── Step 2: Issue MAT cert ────────────────────────────────────────────────
    print('Step 2: Issue MAT client cert (SPIFFE: machine-a-tron)')
    if not dry:
        mat_cert, mat_key = issue_cert_via_vault(
            kubeconfig,
            common_name='machine-a-tron',
            uri_sans='spiffe://nico.local/nico-system/sa/machine-a-tron',
            ttl='720h',
        )
        (mat_dir / 'mat-client.pem').write_text(mat_cert)
        (mat_dir / 'mat-client-key.pem').write_text(mat_key)
        (mat_dir / 'mat-client-key.pem').chmod(0o600)
        print(f'  Written: {mat_dir}/mat-client.pem')
        print(f'  Written: {mat_dir}/mat-client-key.pem')
    else:
        print('  [dry-run] would issue MAT cert from Vault')
    print()

    # ── Step 3: Issue admin-cli cert ──────────────────────────────────────────
    print('Step 3: Issue admin-cli client cert')
    if not dry:
        admin_cert, admin_key = issue_cert_via_vault(
            kubeconfig,
            common_name=f'nico-admin-cli-{sitename or dc_name}',
            ttl='720h',
        )
        (admin_dir / 'client.pem').write_text(admin_cert)
        (admin_dir / 'client-key.pem').write_text(admin_key)
        (admin_dir / 'client-key.pem').chmod(0o600)
        print(f'  Written: {admin_dir}/client.pem')
        print(f'  Written: {admin_dir}/client-key.pem')
    else:
        print('  [dry-run] would issue admin-cli cert from Vault')
    print()

    # ── Step 4: Generate MAT config.toml ─────────────────────────────────────
    print('Step 4: Generate MAT config.toml')
    toml_content = gen_mat_toml(sim, site_folder)
    toml_path = mat_cfg_dir / 'mat-config.toml'
    if not dry:
        toml_path.write_text(toml_content)
        print(f'  Written: {toml_path}')
    else:
        print(f'  [dry-run] would write {toml_path}')
    print()

    # ── Step 5: Generate run scripts ─────────────────────────────────────────
    print('Step 5: Generate run scripts')
    mat_sh_path   = site_folder / 'run-mat.sh'
    admin_sh_path = site_folder / 'run-admin-cli.sh'
    if not dry:
        mat_sh_path.write_text(gen_run_mat_sh(site_folder))
        mat_sh_path.chmod(0o755)
        admin_sh_path.write_text(gen_run_admin_cli_sh(sim, site_folder))
        admin_sh_path.chmod(0o755)
        print(f'  Written: {mat_sh_path}')
        print(f'  Written: {admin_sh_path}')
    else:
        print(f'  [dry-run] would write {mat_sh_path}')
        print(f'  [dry-run] would write {admin_sh_path}')
    print()

    # ── Step 6: /etc/hosts ───────────────────────────────────────────────────
    if not args.skip_hosts:
        print('Step 6: Update /etc/hosts')
        if api_vip:
            api_host = f'nico-api.{dc_name}-{sitename}'
            update_hosts(api_host, api_vip, dry)
        else:
            print('  Warning: api_vip not set in site yaml — skipping hosts update')
        print()

    # ── Done ─────────────────────────────────────────────────────────────────
    print('=' * 55)
    print('CLIs configured.')
    print()
    print('Grant MAT capability to bind port 443 (once per binary build):')
    print('  sudo setcap cap_net_bind_service=+ep $(which machine-a-tron)')
    print()
    print('Run MAT:')
    print(f'  {mat_sh_path}')
    print()
    print('Run admin-cli:')
    print(f'  {admin_sh_path} version')
    print(f'  {admin_sh_path} site-explorer --help')
    print()
    print('Fix UEFI credentials (if needed):')
    print(f'  {admin_sh_path} credential add-uefi --kind=dpu --password=<pwd>')
    print(f'  {admin_sh_path} credential add-uefi --kind=host --password=<pwd>')
    print(f'  {admin_sh_path} credential add-bmc --kind=site-wide-root --password=<pwd>')


if __name__ == '__main__':
    main()
