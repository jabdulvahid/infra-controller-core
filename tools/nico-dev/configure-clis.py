#!/usr/bin/env python3
"""
nico-dev — Configure admin-cli and MAT for a site

Generates certificates, config files, and run scripts for:
  - nico-admin-cli: operator CLI for day-1 Nico site setup
  - MAT (Machine-a-tron): simulates managed host BMC/DHCP discovery (runs on VM)

Output layout under <site-folder>/certs/:
  admin/
    ca.pem              — site root CA (from nico-roots secret)
    client.pem          — admin-cli client cert
    client-key.pem      — admin-cli client key
  mat/
    mat-ca.pem          — site root CA
    mat-client.pem      — MAT client cert (SPIFFE: machine-a-tron)
    mat-client-key.pem  — MAT client key

Scripts generated in <site-folder>/:
  run-admin-cli.sh      — launch nico-admin-cli with certs pre-configured
  run-mat.sh            — launch MAT (run from the share, on the VM)

/etc/hosts entry added on Mac (safe for multi-site):
  {api_vip} nico-api.{dc_name}-{sitename}

Note on connectivity: admin-cli connects to the API VIP ({api_vip}).
This IP is inside the ContainerLab fabric. If it is not reachable from Mac,
add a route via the VM's ACTUAL address (do not assume 192.168.64.2 — UTM's
DHCP pool varies; the script derives it from the kubeconfig server URL):
  sudo route -n add -net <service_vips_prefix> <vm-ip>

Usage:
  ./configure-clis.py ~/sites/dev
  ./configure-clis.py ~/sites/dev --dry-run
  ./configure-clis.py ~/sites/dev --admin-cli-only
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print('Error: pyyaml not installed. Run: pip3 install pyyaml')
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
        description='Configure admin-cli and MAT certs/scripts for a nico-dev site',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('site', help='Site folder (e.g. ~/sites/dev) or yaml file')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be done without making changes')
    p.add_argument('--skip-hosts', action='store_true',
                   help='Skip /etc/hosts update')
    p.add_argument('--admin-cli-only', action='store_true',
                   help='Skip MAT cert/config generation')
    return p.parse_args()


def load_cfg(path):
    with open(path) as f:
        return yaml.safe_load(f)


def find_kubeconfig(cfg, site_folder):
    dc_name  = cfg['fabric'].get('dc_name', 'dev')
    sitename = cfg.get('nico-system', {}).get('helm-values', {}).get('sitename', '')
    # nico-dev kubeconfig naming: {dc_name}-{sitename}.kubeconfig.yaml in the site folder
    kube_filename = cfg.get('kubeconfig', f'{dc_name}-{sitename or "dev"}.kubeconfig.yaml')
    candidates = [
        Path(site_folder) / kube_filename,
        Path(f'~/.kube/config-{dc_name}-{sitename}').expanduser() if sitename else None,
        Path(os.environ.get('KUBECONFIG', '~/.kube/config')).expanduser(),
    ]
    for c in candidates:
        if c and c.exists():
            return str(c)
    raise RuntimeError(
        f'No kubeconfig found. Run deploy-dev-cp.py first. '
        f'Expected: {site_folder}/{kube_filename}')


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


def resolve_vault_token(cfg, kubeconfig):
    """The Vault root token: file-mode Vault generates one at operator init
    (persisted in the vault-init-keys secret by deploy-dev-nico.py); only
    legacy dev-mode uses the static dev token."""
    r = kubectl(['get', 'secret', 'vault-init-keys', '-n', 'vault',
                 '-o', 'jsonpath={.data.root_token}'], kubeconfig)
    if r.returncode == 0 and r.stdout.strip():
        import base64
        return base64.b64decode(r.stdout.strip()).decode()
    return cfg.get('nico-system', {}).get('vault', {}).get('dev_root_token', 'root')


def issue_cert_via_vault(kubeconfig, common_name, uri_sans=None, ttl='720h',
                         token='root'):
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

        # -s only (no -f): on HTTP errors Vault returns a JSON error body we
        # want to SHOW, not swallow ('-f' turned a 403 into an empty message)
        cmd = [
            'curl', '-s',
            '-H', f'X-Vault-Token: {token}',
            '-H', 'Content-Type: application/json',
            '-d', json.dumps(payload),
            'http://127.0.0.1:8200/v1/nicoca/issue/nico-cluster'
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f'Vault unreachable via port-forward: {r.stderr}')

        body = json.loads(r.stdout or '{}')
        if body.get('errors'):
            raise RuntimeError(f'Vault refused cert issue: {body["errors"]}')
        data = body.get('data', {})
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
        input=f'\n{entry}  # nico-dev {hostname}\n',
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f'  Warning: could not update /etc/hosts: {r.stderr.strip()}')
        print(f'  Add manually: echo "{entry}" | sudo tee -a /etc/hosts')


def gen_mat_toml(cfg, site_folder):
    """Generate MAT config.toml from site yaml. MAT runs on the VM."""
    hv        = cfg.get('nico-system', {}).get('helm-values', {})
    np        = hv.get('net-plan', {})
    dc_name   = cfg['fabric'].get('dc_name', 'dev')
    sitename  = hv.get('sitename', dc_name)
    networks  = hv.get('networks', {})
    mat_net   = networks.get('rack-mat-hosts', {})
    admin_net = networks.get('admin', {})
    mat_gw    = mat_net.get('gateway', '')
    admin_gw  = admin_net.get('gateway', '')

    return f'''\
# MAT (Machine-a-tron) configuration for site {sitename}
# Generated by configure-clis.py
# NOTE: MAT runs on the VM, not Mac — br-{dc_name}-internet is a VM bridge.
# Nothing to copy: the VM sees this site folder via the share; run run-mat.sh there.
carbide_api_url = "https://nico-api.{dc_name}-{sitename}:443"
tui_enabled = false
# /var/log, NOT /tmp: MAT runs as root and fs.protected_regular=2 blocks
# O_CREAT on another user's files in sticky dirs (even for root) — a fixed
# name in /tmp dies with a bare EACCES before the logger exists.
log_file = "/var/log/machine-a-tron-{dc_name}.log"
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
# Canonical relay names (MAT #5229). The old admin_dhcp_relay_address was a
# MISNOMER for the underlay relay (DPU OOB boot + switch NVOS); pointing it
# at the admin gateway wedged DPU OOB DHCP once nico #5084 started
# validating segment types (20260826-#1). Both relays are the underlay
# segment's gateway — BMC and DPU-OOB share the management underlay.
bmc_dhcp_relay_address = "{mat_gw}"       # rack-mat-hosts gateway (underlay-typed)
underlay_dhcp_relay_address = "{mat_gw}"  # DPU OOB boot relay — MUST map to an underlay-typed segment
host_reboot_delay = 10
dpu_reboot_delay = 5
'''


def gen_run_mat_sh(cfg, site_folder):
    hv       = cfg.get('nico-system', {}).get('helm-values', {})
    dc_name  = cfg['fabric'].get('dc_name', 'dev')
    sitename = hv.get('sitename', dc_name)
    api_host = f'nico-api.{dc_name}-{sitename}'
    api_vip  = hv.get('net-plan', {}).get('api_vip', '')
    # BMC alias prefix, e.g. '11.140.2.' — used to clean stale aliases
    mat_underlay = cfg['fabric'].get('prefixes', {}).get('mat_underlay', '')
    alias_prefix = mat_underlay.rsplit('.', 1)[0] + '.' if mat_underlay else ''
    # local-route CIDRs covering the BMC prefix EXCLUDING the fabric
    # gateway (first usable address, nico-dev convention) — a local route
    # over the gateway hijacks it (20260825-#3). Computed for ANY prefix
    # length, not just /24.
    import ipaddress
    bmc_net = ipaddress.ip_network(mat_underlay) if mat_underlay else None
    bmc_local_cidrs = ' '.join(
        str(c) for c in sorted(
            bmc_net.address_exclude(
                ipaddress.ip_network(f'{bmc_net.network_address + 1}/32')))
    ) if bmc_net else ''
    return f'''\
#!/usr/bin/env bash
# Run MAT for this site — ON THE VM (it binds br-{dc_name}-internet, which only
# exists there), e.g.:
#   ~/mac/sites/{dc_name}/{sitename}/run-mat.sh
#
# Only two paths matter: the MAT binary and the fleet toml, both set just
# below. To run a custom build: copy this script, point those two lines at
# your binary/config (they must live under the site folder — the VM can't
# see other Mac paths), run the copy. Everything below the marker is
# plumbing you never edit — its main job is putting the client certs where
# MAT expects them so they don't have to be spelled on every command line.

set -euo pipefail
# Self-locating: resolves to the site folder on whichever side runs it.
SITE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

# ── the only two paths you should ever edit ─────────────────────────────
MAT_BIN="$SITE/mat/machine-a-tron"        # Linux binary (build-nico-clis.py)
MAT_CONFIG="$SITE/mat/mat-config.toml"    # fleet definition
# ── no edits below this line: cert + staging plumbing ───────────────────

# MAT must not read the 9p share at runtime (the 0600 key is unreadable to
# the VM user over 9p → MAT silently proceeds certless → anonymous → 403;
# other runtime share reads are equally untrustworthy), so everything is
# staged VM-local first. Staged names carry a variant tag (MAT_BIN's parent
# folder) so script copies pointing at different builds never overwrite
# each other.
VARIANT="$(basename "$(dirname "$MAT_BIN")")"
BIN="/usr/local/bin/machine-a-tron.$VARIANT"
CERT_DIR="/etc/machine-a-tron/{dc_name}"
CONF="$CERT_DIR/config.$VARIANT.toml"

if [[ ! -f "$MAT_BIN" ]]; then
    echo "ERROR: $MAT_BIN not found — run build-nico-clis.py on the Mac first." >&2
    exit 1
fi
if ! file "$MAT_BIN" | grep -q ELF; then
    echo "ERROR: $MAT_BIN is not a Linux binary — rebuild with the current" >&2
    echo "       build-nico-clis.py (it builds in a Linux container)." >&2
    exit 1
fi
if ! cmp -s "$MAT_BIN" "$BIN" 2>/dev/null; then
    echo "Installing machine-a-tron → $BIN (sudo)..."
    sudo install -m 755 "$MAT_BIN" "$BIN"
fi

echo "Syncing MAT certs + config → $CERT_DIR (sudo)..."
sudo install -d -m 700 "$CERT_DIR"
for f in mat-ca.pem mat-client.pem mat-client-key.pem; do
    sudo cp "$SITE/certs/mat/$f" "$CERT_DIR/$f"
done
sudo chmod 600 "$CERT_DIR"/mat-client-key.pem
sudo cp "$MAT_CONFIG" "$CONF"
# The log path derives from THIS script's name (run-mat-foo.sh → suffix
# "-foo"), so parallel dev variants never overwrite each other's logs.
# Rewritten in the STAGED copy only; the source toml is untouched.
TAG="$(basename "${{BASH_SOURCE[0]}}" .sh)"; TAG="${{TAG#run-mat}}"
MAT_LOG="/var/log/machine-a-tron-{dc_name}$TAG.log"
sudo sed -i '/^log_file *=/d' "$CONF"
sudo sed -i '1i log_file = "'"$MAT_LOG"'"' "$CONF"
echo "MAT log: $MAT_LOG"
# bmc-mock's Redfish SERVER certs (repo dev certs, staged by configure-clis;
# build-independent, so always sourced from $SITE/mat/ even for custom
# builds); bmc-mock resolves them via $REPO_ROOT/crates/bmc-mock/tls.{{crt,key}}
sudo install -d -m 700 "$CERT_DIR/repo-root/crates/bmc-mock"
sudo cp "$SITE/mat/tls.crt" "$SITE/mat/tls.key" "$CERT_DIR/repo-root/crates/bmc-mock/"

# MAT resolves the API by hostname; make sure the VM knows it too.
if ! grep -q "{api_host}" /etc/hosts; then
    echo "Adding {api_vip} {api_host} to /etc/hosts (sudo)..."
    echo "{api_vip} {api_host}" | sudo tee -a /etc/hosts >/dev/null
fi

# Clean stale BMC IP aliases from a previous MAT that died without its
# cleanup (Ctrl-C/kill): reset-mat-state.py runs on the Mac and cannot
# touch VM network state, so every launch starts from a clean interface.
# MAT re-creates exactly the aliases it needs.
for a in $(ip -o -4 addr show dev br-{dc_name}-internet 2>/dev/null \\
           | awk '{{print $4}}' | grep "^{alias_prefix}" || true); do
    echo "Removing stale BMC alias $a (sudo)..."
    sudo ip addr del "$a" dev br-{dc_name}-internet
done

# The VM must own the BMC addresses: upstream MAT (post-Aug-2026
# refactor) no longer adds per-machine IP aliases — it serves ALL mock
# BMCs from one 0.0.0.0:443 listener routed by Host header — so packets
# to those IPs must be accepted locally. 'local' routes do that
# (invisible in 'ip addr'!) but MUST NOT cover the fabric gateway .1:
# a whole-/24 local route hijacked it and broke discovery-phase DHCP
# relay (20260825-#3). This CIDR set covers .2-.255, .1 excluded.
# Local routes do not survive reboot, so ensure them on every launch.
sudo ip route del local {mat_underlay} dev br-{dc_name}-internet 2>/dev/null || true  # the old too-broad route
echo "Ensuring local routes for the BMC prefix (gateway excluded, sudo)..."
for CIDR in {bmc_local_cidrs}; do
    # 'replace' is add-or-update: idempotent, unlike 'add' (File exists)
    sudo ip route replace local "$CIDR" dev br-{dc_name}-internet
done

# MAT runs as root (nico-sim-validated): it binds port 443 for the BMC mocks
# AND creates IP aliases on br-{dc_name}-internet (CAP_NET_ADMIN). Vars go
# through 'env' so sudoers setenv policy cannot silently drop them.
sudo env FORGE_ROOT_CA_PATH="$CERT_DIR/mat-ca.pem" \\
         CLIENT_CERT_PATH="$CERT_DIR/mat-client.pem" \\
         CLIENT_KEY_PATH="$CERT_DIR/mat-client-key.pem" \\
         REPO_ROOT="$CERT_DIR/repo-root" \\
         "$BIN" "$CONF" "$@"
'''


def gen_run_admin_cli_sh(cfg, site_folder):
    hv       = cfg.get('nico-system', {}).get('helm-values', {})
    dc_name  = cfg['fabric'].get('dc_name', 'dev')
    sitename = hv.get('sitename', dc_name)
    api_host = f'nico-api.{dc_name}-{sitename}'
    abs_site = Path(site_folder).expanduser().resolve()
    return f'''\
#!/usr/bin/env bash
# Run nico-admin-cli for this site. Requires nico-admin-cli in $PATH.
#
# Note: the 'version' subcommand shows 'IGNORING SERVER CERT' — this is expected.
# All other subcommands perform real TLS verification with the site CA.
#
# If {api_host} is not reachable from Mac, add a route via the VM's IP
# (find it: grep server <site>/​*.kubeconfig.yaml):
#   sudo route -n add -net <service_vips_prefix> <vm-ip>

set -euo pipefail
SITE="{abs_site}"

API_URL="https://{api_host}:443" \\
ROOT_CA_PATH="$SITE/certs/admin/ca.pem" \\
CLIENT_CERT_PATH="$SITE/certs/admin/client.pem" \\
CLIENT_KEY_PATH="$SITE/certs/admin/client-key.pem" \\
nico-admin-cli "$@"
'''


def main():
    args        = parse_args()
    dry         = args.dry_run
    site_yaml   = resolve_site(args.site)
    site_folder = Path(site_yaml).parent
    cfg         = load_cfg(site_yaml)

    dc_name  = cfg['fabric'].get('dc_name', 'dev')
    hv       = cfg.get('nico-system', {}).get('helm-values', {})
    sitename = hv.get('sitename', dc_name)
    api_vip  = hv.get('net-plan', {}).get('api_vip', '')

    print('nico-dev — Configure CLIs')
    print(f'  site      : {site_folder}')
    print(f'  dc_name   : {dc_name}  sitename: {sitename}')
    print(f'  api_vip   : {api_vip}')
    if dry:
        print(f'  DRY RUN   : no files written')
    print()

    kubeconfig = find_kubeconfig(cfg, str(site_folder))
    print(f'  kubeconfig: {kubeconfig}')
    print()

    # ── Create directories ────────────────────────────────────────────────────
    admin_dir   = site_folder / 'certs' / 'admin'
    mat_dir     = site_folder / 'certs' / 'mat'
    mat_cfg_dir = site_folder / 'mat'
    if not dry:
        admin_dir.mkdir(parents=True, exist_ok=True)
        if not args.admin_cli_only:
            mat_dir.mkdir(parents=True, exist_ok=True)
            mat_cfg_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Fetch CA cert ─────────────────────────────────────────────────
    print('Step 1: Fetch site CA from nico-roots secret')
    ca_cert = get_ca_cert(kubeconfig)
    print(f'  CA cert fetched ({len(ca_cert)} bytes) ✓')

    if not dry:
        (admin_dir / 'ca.pem').write_text(ca_cert)
        print(f'  Written: {admin_dir}/ca.pem')
        if not args.admin_cli_only:
            (mat_dir / 'mat-ca.pem').write_text(ca_cert)
            print(f'  Written: {mat_dir}/mat-ca.pem')
    print()

    # ── Step 2: Issue admin-cli cert ──────────────────────────────────────────
    print('Step 2: Issue admin-cli client cert')
    vault_token = resolve_vault_token(cfg, kubeconfig) if not dry else ''
    if not dry:
        admin_cert, admin_key = issue_cert_via_vault(
            kubeconfig,
            common_name=f'nico-admin-cli-{sitename or dc_name}',
            ttl='720h',
            token=vault_token,
        )
        (admin_dir / 'client.pem').write_text(admin_cert)
        (admin_dir / 'client-key.pem').write_text(admin_key)
        (admin_dir / 'client-key.pem').chmod(0o600)
        print(f'  Written: {admin_dir}/client.pem')
        print(f'  Written: {admin_dir}/client-key.pem')
    else:
        print('  [dry-run] would issue admin-cli cert from Vault')
    print()

    # ── Step 3: Issue MAT cert ────────────────────────────────────────────────
    if not args.admin_cli_only:
        print('Step 3: Issue MAT client cert (SPIFFE: machine-a-tron)')
        if not dry:
            mat_cert, mat_key = issue_cert_via_vault(
                kubeconfig,
                common_name='machine-a-tron',
                uri_sans='spiffe://nico.local/nico-system/sa/machine-a-tron',
                ttl='720h',
                token=vault_token,
            )
            (mat_dir / 'mat-client.pem').write_text(mat_cert)
            (mat_dir / 'mat-client-key.pem').write_text(mat_key)
            (mat_dir / 'mat-client-key.pem').chmod(0o600)
            print(f'  Written: {mat_dir}/mat-client.pem')
            print(f'  Written: {mat_dir}/mat-client-key.pem')
        else:
            print('  [dry-run] would issue MAT cert from Vault')
        print()

        # ── Step 4: Generate MAT config.toml ─────────────────────────────────
        print('Step 4: Generate MAT config.toml')
        toml_path = mat_cfg_dir / 'mat-config.toml'
        if not dry:
            toml_path.write_text(gen_mat_toml(cfg, site_folder))
            print(f'  Written: {toml_path}')
        else:
            print(f'  [dry-run] would write {toml_path}')

        # Stage the repo's shipped bmc-mock dev SERVER certs: the mock BMCs
        # serve Redfish over TLS, and bmc-mock resolves its tls.crt via
        # CARGO_MANIFEST_DIR (a container-build path that exists nowhere at
        # runtime) → /opt/carbide → $REPO_ROOT. run-mat.sh installs these
        # VM-locally and points REPO_ROOT at them.
        repo = Path(cfg.get('nico_mac_folder', '')).expanduser() / \
               cfg.get('nico_repo_folder', 'infra-controller-core')
        bmc_src = repo / 'crates' / 'bmc-mock'
        if not dry:
            for f in ['tls.crt', 'tls.key']:
                src = bmc_src / f
                if not src.exists():
                    print(f'  Error: {src} not found — bmc-mock dev certs '
                          f'missing from the checkout', file=sys.stderr)
                    sys.exit(1)
                (mat_cfg_dir / f).write_bytes(src.read_bytes())
            print(f'  Staged bmc-mock server certs → {mat_cfg_dir}/tls.crt,.key')
        print()

    # ── Step 5: Generate run scripts ─────────────────────────────────────────
    step = 3 if args.admin_cli_only else 5
    print(f'Step {step}: Generate run scripts')
    admin_sh_path = site_folder / 'run-admin-cli.sh'
    if not dry:
        admin_sh_path.write_text(gen_run_admin_cli_sh(cfg, site_folder))
        admin_sh_path.chmod(0o755)
        print(f'  Written: {admin_sh_path}')
    else:
        print(f'  [dry-run] would write {admin_sh_path}')

    if not args.admin_cli_only:
        mat_sh_path = site_folder / 'run-mat.sh'
        if not dry:
            mat_sh_path.write_text(gen_run_mat_sh(cfg, site_folder))
            mat_sh_path.chmod(0o755)
            print(f'  Written: {mat_sh_path}')
        else:
            print(f'  [dry-run] would write {mat_sh_path}')
    print()

    # ── Step 6: /etc/hosts ───────────────────────────────────────────────────
    if not args.skip_hosts:
        hosts_step = 4 if args.admin_cli_only else 6
        print(f'Step {hosts_step}: Update /etc/hosts')
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
    print('Run admin-cli:')
    print(f'  {admin_sh_path} version')
    print(f'  {admin_sh_path} site-explorer --help')
    print()
    # Derive the VM's actual IP from the kubeconfig server URL — never assume
    # 192.168.64.2 (UTM's DHCP pool varies; documented trap in conversations.md)
    vm_ip = '<vm-ip>'
    try:
        kube_cfg = yaml.safe_load(Path(kubeconfig).read_text())
        server   = kube_cfg['clusters'][0]['cluster']['server']
        vm_ip    = server.split('//')[1].split(':')[0]
    except Exception:
        pass

    if not args.admin_cli_only:
        mat_sh_path = site_folder / 'run-mat.sh'
        print('MAT runs on the VM — its certs/config/script are already visible')
        print('there via the shared folder (no scp needed). On the VM:')
        print(f'  ssh nico@{vm_ip}')
        # Re-root the site folder onto the VM's share mount (self-computed —
        # never guess path segments)
        mac_root = cfg.get('nico_mac_folder', '')
        vm_root  = cfg.get('nico_vm_folder', '~/mac')
        try:
            rel = Path(site_folder).resolve().relative_to(
                Path(mac_root).expanduser().resolve())
            print(f'  {vm_root}/{rel}/run-mat.sh')
        except ValueError:
            print(f'  {vm_root}/sites/<dc>/<site>/run-mat.sh')
        print()
    print('If API VIP is not reachable from Mac, add a route:')
    svc_prefix = cfg.get('nico-system', {}).get('helm-values', {}).get('net-plan', {}).get('service_vips', '')
    if svc_prefix:
        print(f'  sudo route -n add -net {svc_prefix} {vm_ip}')
    else:
        print(f'  sudo route -n add -net <service_vips_prefix> {vm_ip}')
    print()
    print('Fix UEFI credentials (if needed):')
    print(f'  {admin_sh_path} credential add-uefi --kind=dpu --password=<pwd>')
    print(f'  {admin_sh_path} credential add-uefi --kind=host --password=<pwd>')
    print(f'  {admin_sh_path} credential add-bmc --kind=site-wide-root --password=<pwd>')


if __name__ == '__main__':
    main()
