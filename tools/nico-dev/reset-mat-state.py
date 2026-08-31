#!/usr/bin/env python3
"""
nico-dev — Reset all MAT fleet state to t0 (20260823-#3).

MAT's cleanup_on_quit deletes machines only; nico retains the fleet's
FOOTPRINT — explored endpoints (incl. login-lockout guards), expected-*
registrations, and Vault per-MAC BMC credentials that nico ROTATED during
ingestion. Deterministic MACs mean the next run reuses the same MACs, the
site-explorer tries the rotated creds against fresh factory-cred mocks,
fails, and locks the endpoints out (NICO-SITEEXPLORER-144).

Run this on the Mac AFTER stopping MAT and BEFORE the next MAT run.

Default scope (fleet state only — day-1 operator config survives):
  - machines (force-delete via admin-cli)
  - expected machines (erase)
  - explored endpoints (site-explorer delete, from the live report)
  - stale machine interfaces (machine-interfaces delete --mac-address, fleet
    MACs 02:/06: only) — these DHCP-created rows are what re-seeds the
    site-explorer and resurrects endpoints after a reset (20260825-#1)
  - Vault per-MAC BMC creds (secrets/machines/bmc/<mac>/root)

--full additionally recycles day-1 credential config (true t0):
  - wipes Vault machines/bmc/site (site-wide root) and
    machines/all_{hosts,dpus}/site_default UEFI defaults
  - then RE-CREATES all three via admin-cli using the mandatory --password,
    so one command leaves the site fully ready for the next MAT run

Usage:
  ./reset-mat-state.py <site-folder> [--yes]
  ./reset-mat-state.py <site-folder> --full --password '<site-password>' [--yes]
"""

import argparse
import base64
import subprocess
import sys
from pathlib import Path


def sh(cmd, check=True, capture=True):
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        print(f'  ! failed: {" ".join(str(c) for c in cmd)}\n'
              f'    {(r.stderr or r.stdout or "").strip()[:300]}', file=sys.stderr)
        sys.exit(1)
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument('site', help='site folder (e.g. <share>/sites/dc1/dev1)')
    p.add_argument('--full', action='store_true',
                   help='ALSO recycle day-1 credential config (site-wide BMC '
                        'root, UEFI defaults): wipe + re-create with --password')
    p.add_argument('--password', default=None, metavar='PWD',
                   help='password for the three re-created site-wide '
                        'credentials (REQUIRED with --full)')
    p.add_argument('--yes', action='store_true', help='skip confirmation')
    args = p.parse_args()
    if args.full and not args.password:
        p.error('--full requires --password (the three site-wide credentials '
                'are re-created in the same run)')
    if args.password and not args.full:
        p.error('--password is only meaningful with --full')

    site = Path(args.site).expanduser().resolve()
    admin = site / 'run-admin-cli.sh'
    kubeconfigs = list(site.glob('*.kubeconfig.yaml'))
    if not admin.exists() or not kubeconfigs:
        print(f'Error: {site} lacks run-admin-cli.sh / kubeconfig — '
              f'run configure-clis.py / deploy-dev-cp.py first', file=sys.stderr)
        sys.exit(1)
    kubeconfig = str(kubeconfigs[0])

    print('nico-dev — Reset MAT fleet state to t0')
    print(f'  site : {site}')
    print(f'  scope: fleet state' + (' + day-1 credentials (--full)' if args.full else
                                     ' (day-1 credentials preserved)'))
    print('  NOTE : stop MAT first (Ctrl-C on the VM) — this does not check.')
    if not args.yes:
        if input('Proceed? [y/N] ').strip().lower() != 'y':
            sys.exit(0)

    def admin_cli(*a, check=False):
        return sh([str(admin), *a], check=check)

    # ── 1. machines ───────────────────────────────────────────────────────────
    print('\nStep 1: force-delete machines')
    out = admin_cli('machine', 'show').stdout or ''
    ids = sorted({w for line in out.splitlines() for w in line.split()
                  if w.startswith('fm100') and len(w) > 50})
    for mid in ids:
        r = admin_cli('machine', 'force-delete', '--machine', mid)
        print(f'  {mid[:20]}… {"✓" if "succeeded" in (r.stdout or "") else r.stdout.strip()[:60]}')
    if not ids:
        print('  none present ✓')

    # ── 2. expected machines ──────────────────────────────────────────────────
    print('Step 2: erase expected machines')
    r = admin_cli('expected-machine', 'erase')
    print(f'  {"✓" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:80]}')

    # ── 3. explored endpoints ─────────────────────────────────────────────────
    print('Step 3: delete explored endpoints')
    import re
    out = admin_cli('site-explorer', 'get-report', 'endpoint').stdout or ''
    eps = sorted(set(re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', out)))
    for ip in eps:
        admin_cli('site-explorer', 'delete', '--address', ip)
        print(f'  {ip} ✓')
    if not eps:
        print('  none present ✓')

    # ── 4. stale machine interfaces (the endpoint-resurrection seed) ──────────
    # BMC DHCP creates machine_interfaces rows; machine force-delete leaves the
    # machine_id=NULL ones behind, and the site-explorer enumerates exactly
    # those to re-create "explored endpoints" minutes after step 3 (20260825-#1).
    # Fleet MACs are locally administered (02:/06: test pools) — never touch
    # anything else.
    print('Step 4: delete stale machine interfaces (fleet MACs)')
    out = admin_cli('machine-interfaces', 'show').stdout or ''
    fleet_macs = sorted({m for m in re.findall(
        r'\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b', out)
        if m.lower().startswith(('02:', '06:'))})
    for mac in fleet_macs:
        r = admin_cli('machine-interfaces', 'delete', '--mac-address', mac)
        print(f'  {mac} {"✓" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:60]}')
    if not fleet_macs:
        print('  none present ✓')

    # ── 5. Vault: per-MAC rotated BMC creds ───────────────────────────────────
    print('Step 5: clear Vault per-MAC BMC credentials')
    r = sh(['kubectl', '--kubeconfig', kubeconfig, 'get', 'secret',
            'vault-init-keys', '-n', 'vault',
            '-o', 'jsonpath={.data.root_token}'])
    token = base64.b64decode(r.stdout.strip()).decode()

    def vault(cmd):
        return sh(['kubectl', '--kubeconfig', kubeconfig, 'exec', '-n', 'vault',
                   'vault-0', '-c', 'vault', '--', 'sh', '-c',
                   f'VAULT_TOKEN={token} vault {cmd}'], check=False)

    r = vault('kv list -mount=secrets machines/bmc/')
    macs = [l.strip().rstrip('/') for l in (r.stdout or '').splitlines()
            if ':' in l]
    for mac in macs:
        vault(f'kv metadata delete -mount=secrets "machines/bmc/{mac}/root"')
        print(f'  {mac} ✓')
    if not macs:
        print('  none present ✓')

    # ── 6. optional: day-1 credential config ──────────────────────────────────
    if args.full:
        print('Step 6: recycle day-1 credential config (--full)')
        for path in ['machines/bmc/site/root',
                     'machines/all_hosts/site_default/uefi-metadata-items/auth',
                     'machines/all_dpus/site_default/uefi-metadata-items/auth']:
            vault(f'kv metadata delete -mount=secrets "{path}"')
            print(f'  wiped {path} ✓')
        for cmd_args, label in [
            (['credential', 'add-bmc', '--kind=site-wide-root',
              f'--password={args.password}'], 'site-wide BMC root'),
            (['credential', 'add-uefi', '--kind=dpu',
              f'--password={args.password}'], 'UEFI dpu default'),
            (['credential', 'add-uefi', '--kind=host',
              f'--password={args.password}'], 'UEFI host default'),
        ]:
            r = admin_cli(*cmd_args, check=True)
            print(f'  created {label} ✓')

    print('\n' + '=' * 55)
    print('  Fleet state reset ✓ — next MAT run starts from t0.')
    if args.full:
        print('  Day-1 credentials re-created with the provided password ✓')
    print('=' * 55)


if __name__ == '__main__':
    main()
