#!/usr/bin/env bash
# =============================================================================
# bake-golden-image.sh — Prepare the nico-dev VM for golden image export
#
# Run this ONCE on the VM before taking a UTM snapshot/export.
# It does NOT modify the running Nico stack — only prepares the image so
# new users can boot from it and run first-boot.sh to personalise.
#
# Prerequisites (must be done manually before running this script):
#   - VM has a static IP configured via netplan (e.g. 192.168.64.126)
#   - Nico stack is fully deployed and all pods are Running
#   - Username on the VM is 'nico' with passwordless sudo
#
# What it does:
#   1. Saves a canonical copy of the site yaml to /etc/nico-dev/dev.yaml
#      (VM-local, not in the Mac share) for first-boot.sh to copy
#   2. Copies the entire nico-dev script bundle to /usr/local/lib/nico-dev/
#      so new users can run it immediately after booting
#   3. Clears /etc/nico-dev/env (to be written by first-boot.sh)
#   4. Resets nico user credentials for distribution:
#      - Password → Welcome123!
#      - Clears SSH authorized_keys
#      - Enables SSH password authentication
#   5. Verifies Nico pods are running before snapshot
#
# Usage (on VM, as root):
#   sudo bash bake-golden-image.sh <site-yaml>
#
# Example:
#   sudo bash /mnt/mac/<nico-dev-folder>/bake-golden-image.sh \
#     /mnt/mac/sites/dev/dev.yaml
# =============================================================================
set -euo pipefail

SITE_YAML="${1:-}"
if [[ -z "$SITE_YAML" ]]; then
    echo "Usage: sudo bash $0 <site-yaml>" >&2
    echo "  e.g. sudo bash $0 /mnt/mac/sites/dev/dev.yaml" >&2
    exit 1
fi
if [[ ! -f "$SITE_YAML" ]]; then
    echo "Error: site yaml not found: $SITE_YAML" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Error: run as root (sudo)" >&2
    exit 1
fi

LIB_DIR="/usr/local/lib/nico-dev"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATIC_IP=$(ip route get 192.168.64.1 2>/dev/null | grep -oP 'src \K\S+' || hostname -I | awk '{print $1}')

echo "nico-dev — Bake Golden Image"
echo "  site yaml  : $SITE_YAML"
echo "  script dir : $SCRIPT_DIR"
echo "  lib dir    : $LIB_DIR"
echo "  static IP  : $STATIC_IP"
echo

# ── Step 1: Save canonical site yaml to /etc/nico-dev/ ────────────────────────
echo "Step 1: Saving canonical site yaml..."
mkdir -p /etc/nico-dev
cp "$SITE_YAML" /etc/nico-dev/dev.yaml
echo "  Saved: /etc/nico-dev/dev.yaml ✓"
echo

# ── Step 2: Copy nico-dev script bundle to /usr/local/lib/nico-dev/ ──────────
echo "Step 2: Copying nico-dev scripts to $LIB_DIR..."
mkdir -p "$LIB_DIR"
rsync -a --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='_backup/' \
    --exclude='*.utm' \
    --exclude='*.qcow2' \
    "${SCRIPT_DIR}/" "$LIB_DIR/"
chmod +x "$LIB_DIR"/*.sh "$LIB_DIR"/*.py 2>/dev/null || true
echo "  Copied $(find "$LIB_DIR" -maxdepth 1 -type f | wc -l) files → $LIB_DIR ✓"

# Add to PATH system-wide so 'ndev.py', 'deploy-dev-fabric.py' etc. work without full path
cat > /etc/profile.d/nico-dev.sh <<'EOF'
export PATH="/usr/local/lib/nico-dev:$PATH"
EOF
echo "  Added $LIB_DIR to system PATH via /etc/profile.d/nico-dev.sh ✓"

# ndev as a first-class command — with /etc/nico-dev/env providing the default
# site, non-advanced users can just type: ndev  /  ndev fabric verify
ln -sf "$LIB_DIR/ndev.py" /usr/local/bin/ndev
echo "  Installed /usr/local/bin/ndev ✓"
echo

# ── Step 3: Clear user-specific env ───────────────────────────────────────────
echo "Step 3: Clearing /etc/nico-dev/env (first-boot.sh will write it)..."
echo "# Written by first-boot.sh at provisioning time" > /etc/nico-dev/env
echo "  Cleared /etc/nico-dev/env ✓"
echo

# ── Step 4: Reset nico user credentials for distribution ──────────────────────
echo "Step 4: Resetting nico user credentials..."

# Reset password
echo "nico:Welcome123!" | chpasswd
echo "  Password reset to Welcome123! ✓"

# Clear SSH authorized_keys
NICO_SSH_DIR="/home/nico/.ssh"
mkdir -p "$NICO_SSH_DIR"
> "${NICO_SSH_DIR}/authorized_keys"
chmod 600 "${NICO_SSH_DIR}/authorized_keys"
chown -R nico:nico "$NICO_SSH_DIR"
echo "  SSH authorized_keys cleared ✓"

# Enable SSH password authentication
SSHD_CONFIG="/etc/ssh/sshd_config"
if grep -q "^PasswordAuthentication" "$SSHD_CONFIG"; then
    sed -i "s/^PasswordAuthentication.*/PasswordAuthentication yes/" "$SSHD_CONFIG"
else
    echo "PasswordAuthentication yes" >> "$SSHD_CONFIG"
fi
systemctl restart ssh
echo "  SSH password authentication enabled ✓"
echo

# ── Step 4b: Wipe MAT runtime residue ─────────────────────────────────────────
# run-mat.sh stages the maintainer's MAT client certs/keys and binaries
# VM-local; a distributed image must not carry them (each user's run-mat.sh
# re-stages their own at first launch).
echo "Step 4b: Wiping MAT runtime residue..."
rm -f /usr/local/bin/machine-a-tron*
rm -rf /etc/machine-a-tron
rm -f /var/log/machine-a-tron-*.log
echo "  Staged MAT binaries, certs, configs, and logs removed ✓"
echo

# ── Step 4c: Delete stray debug/one-off pods ──────────────────────────────────
# kubectl debug node/... leaves Completed node-debugger-* pods in default;
# baked residue confuses every image user (found on vm1, 2026-08-26).
echo "Step 4c: Deleting stray debug pods..."
kubectl --kubeconfig /etc/kubernetes/admin.conf get pods -n default --no-headers 2>/dev/null \
    | awk '/node-debugger/{print $1}' \
    | xargs -r kubectl --kubeconfig /etc/kubernetes/admin.conf delete pod -n default 2>/dev/null || true
echo "  Stray debug pods removed ✓"
echo

# ── Step 5: Clean up disk space before export ────────────────────────────────
echo "Step 5: Cleaning up disk space..."

# Docker build cache — can be GBs from any previous image builds on this VM
docker builder prune -af --filter until=0s 2>/dev/null || true
echo "  Docker build cache cleared ✓"

# Dangling Docker images (untagged layers from builds or pulls)
docker image prune -f 2>/dev/null || true
echo "  Dangling Docker images cleared ✓"

# Apt cache — not needed in the golden image
apt-get clean
rm -rf /var/lib/apt/lists/*
echo "  Apt cache cleared ✓"

# Journal logs — keep only last 10MB
journalctl --vacuum-size=10M 2>/dev/null || true
echo "  Journal logs trimmed ✓"

# Bash history
> /root/.bash_history
> /home/nico/.bash_history 2>/dev/null || true
echo "  Shell history cleared ✓"

DISK_USED=$(df -BG / | awk 'NR==2 {print $3}')
echo "  Disk used after cleanup: ${DISK_USED}"
echo

# ── Step 6: Verify Nico pods are running (HARD GATE) ─────────────────────────
echo "Step 6: Verifying Nico pods..."
PODS=$(kubectl --kubeconfig /etc/kubernetes/admin.conf get pods -n nico-system \
    --no-headers 2>/dev/null || true)
echo "$PODS" | awk '{print "  " $1 "\t" $3}'
TOTAL=$(echo "$PODS" | grep -c . || true)
UNHEALTHY=$(echo "$PODS" | grep -v -E "Running|Completed" | grep -c . || true)
if [[ "$TOTAL" -eq 0 || "$UNHEALTHY" -gt 0 ]]; then
    echo
    echo "ERROR: refusing to bake — $UNHEALTHY of $TOTAL nico-system pods are not" >&2
    echo "Running/Completed (or no pods found). A golden image snapshots this state" >&2
    echo "for every future user. Fix the stack first, or set FORCE_BAKE=1 to override." >&2
    [[ "${FORCE_BAKE:-0}" != "1" ]] && exit 1
    echo "  FORCE_BAKE=1 set — continuing anyway"
fi
echo

# ── Step 6b: Verify vault is in file mode (HARD GATE) ────────────────────────
# Dev-mode vault is in-memory: every clone loses ALL Vault state on any pod
# restart and nico-api crashes with 403s. A golden image must use file mode.
# Parse, don't grep: comment lines between 'vault:' and 'mode:' broke the
# old -A3 grep (the comments DOCUMENTING this rule pushed the key out of range).
VAULT_MODE=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('/etc/nico-dev/dev.yaml'))
print((cfg.get('nico-system', {}).get('vault', {}) or {}).get('mode', ''))
" 2>/dev/null || echo "parse-error")
if [[ "$VAULT_MODE" != "file" ]]; then
    echo "ERROR: refusing to bake — vault.mode is not 'file' in the site yaml." >&2
    echo "  Dev-mode vault loses all state on pod restart in every clone." >&2
    echo "  Fix: set nico-system.vault.mode: file in the site yaml, then" >&2
    echo "  redeploy vault:  deploy-dev-nico.py <site> --only vault" >&2
    echo "  (then re-run nico-prereqs + nico). Or FORCE_BAKE=1 to override." >&2
    [[ "${FORCE_BAKE:-0}" != "1" ]] && exit 1
    echo "  FORCE_BAKE=1 set — continuing anyway"
fi
echo "  vault mode: file ✓"
echo

# ── Step 6c: Verify the MAT fleet is at t0 (HARD GATE) ───────────────────────
# A golden image with a half-ingested fleet confuses every future user.
# Fix: stop MAT, run reset-mat-state.py <site> --yes on the Mac, re-bake.
MACHINES=$(kubectl --kubeconfig /etc/kubernetes/admin.conf exec -n postgres \
    nico-pg-cluster-0 -c postgres -- psql -U postgres -d nico_system_nico \
    -tA -c "SELECT count(*) FROM machines" 2>/dev/null || echo "?")
if [[ "$MACHINES" != "0" ]]; then
    echo "ERROR: refusing to bake — $MACHINES machine(s) in the database." >&2
    echo "  Stop MAT, run reset-mat-state.py <site> --yes, then re-bake." >&2
    echo "  (Or FORCE_BAKE=1 to override.)" >&2
    [[ "${FORCE_BAKE:-0}" != "1" ]] && exit 1
    echo "  FORCE_BAKE=1 set — continuing anyway"
fi
echo "  MAT fleet at t0 (0 machines) ✓"
echo

# ── Step 6d: Verify allow_insecure_discovery is present (HARD GATE) ───────────
# MAT machine discovery needs it (20260825-#4); helm redeploys silently drop
# it. A golden image without it breaks the first MAT run for every user.
# Fix: deploy-dev-nico.py's patch_allow_insecure_discovery, or redeploy-dev-nico.py
# (both re-apply it).
if ! kubectl --kubeconfig /etc/kubernetes/admin.conf get configmap \
        nico-api-config-files -n nico-system -o yaml 2>/dev/null \
        | grep -q "allow_insecure_discovery"; then
    echo "ERROR: refusing to bake — allow_insecure_discovery missing from" >&2
    echo "  nico-api-config-files (20260825-#4: a helm redeploy dropped it)." >&2
    echo "  Re-apply via redeploy-dev-nico.py or the patch function, then re-bake." >&2
    echo "  (Or FORCE_BAKE=1 to override.)" >&2
    [[ "${FORCE_BAKE:-0}" != "1" ]] && exit 1
    echo "  FORCE_BAKE=1 set — continuing anyway"
fi
echo "  allow_insecure_discovery present ✓"
echo

# ── Step 7: Verify Nico images are cached in containerd ───────────────────────
echo "Step 7: Checking cached images in containerd..."
crictl images 2>/dev/null | grep -v "^IMAGE" | awk '{print "  " $1 ":" $2}' | grep -v "<none>" | head -20
echo

# ── Done ──────────────────────────────────────────────────────────────────────
echo "============================================================"
echo "VM is ready for golden image export."
echo
echo "NOTE: /etc/nico-dev/env is now CLEARED — if you boot THIS VM again"
echo "      before exporting, the fabric will NOT come up (20260826-#6)."
echo "      Restore it with:"
echo "        echo 'NICO_DEV_SITE=$SITE_YAML' | sudo tee /etc/nico-dev/env"
echo "        sudo systemctl restart nico-dev-fabric"
echo
echo "Static IP baked in: $STATIC_IP"
echo
echo "Next steps:"
echo "  1. In UTM settings: REMOVE the shared directory first (it is baked"
echo "     into config.plist otherwise — importers must set their own; re-add"
echo "     yours after the export)"
echo "  2. In UTM: right-click the VM → Share... (UTM's export)"
echo "     Save as: nico-dev-golden-$(date +%Y%m%d).utm  (date-stamped: images are versioned)"
echo "  3. Distribute the exported image to developers"
echo "  4. Developers: import into UTM, then BEFORE booting:"
echo "       - Sharing: set shared folder to their Mac repos folder (share name: 'share')"
echo "       - (No MAC regeneration needed — static IP is set inside the VM)"
echo "  5. Boot the VM, then SSH from Mac terminal:"
echo "       ssh nico@${STATIC_IP}"
echo "       Password: Welcome123!"
echo "  6. Run first-boot.sh:"
echo "       sudo bash /usr/local/lib/nico-dev/first-boot.sh"
echo "============================================================"
