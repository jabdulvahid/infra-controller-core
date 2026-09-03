#!/usr/bin/env bash
# =============================================================================
# first-boot.sh — Personalise a cloned nico-dev golden image
#
# Run ONCE on a freshly cloned nico-dev VM to set up your environment,
# mount the Mac shared folder, and start the Nico cluster.
#
# Run via SSH from your Mac (paste works, no serial console needed):
#   ssh nico@192.168.64.126   # password: Welcome123!
#   sudo bash /usr/local/lib/nico-dev/first-boot.sh
#
# What it does:
#   1. Keeps the VM hostname (it is the kubeadm node name — not renamable)
#   2. Installs your SSH public key (optional)
#   3. Mounts the Mac shared folder (9p) at /mnt/mac and ~/mac
#   4. Copies the template site yaml to your mac share, updating folder paths
#   5. Writes the kubeconfig to the share
#   6. Writes /etc/nico-dev/env with your site path
#   7. Copies the nico-dev script bundle to the Mac share
#   8. Restarts nico-dev-fabric → Nico cluster comes up using cached images
# =============================================================================
set -euo pipefail

# Everything below runs inside main(): bash parses the ENTIRE file before
# executing, so overwriting this script mid-run (20260826-#4: step 8 copies
# the bundle over the running file when executed from the share) cannot
# corrupt execution.
main() {

if [[ $EUID -ne 0 ]]; then
    echo "Error: run as root (sudo bash $0)" >&2
    exit 1
fi

TEMPLATE_SITE="/etc/nico-dev/dev.yaml"
if [[ ! -f "$TEMPLATE_SITE" ]]; then
    echo "Error: template site yaml not found at $TEMPLATE_SITE" >&2
    echo "  Was bake-golden-image.sh run before exporting this image?" >&2
    exit 1
fi

USERNAME=nico

# ── Arguments (non-interactive mode) ─────────────────────────────────────────
# Every prompt has a flag; with all of them (or --yes for the confirmations)
# the script runs without a terminal. Prompts remain only as the fallback
# for a value that was not given.
#
#   first-boot.sh [--ssh-key <file-or-key>] [--mac-folder <path>] [--share <tag>] [--yes]
#
SSH_PUBKEY=""; MAC_REPO_PATH=""; SHARE_NAME=""; ASSUME_YES=0
usage() {
    cat <<'EOF'
Usage: sudo bash first-boot.sh [options]
  --ssh-key <file|key>   public key to authorize for nico (a file path, or the
                         one-line key itself); omit to skip / be prompted
  --mac-folder <path>    the Mac folder shared into the VM (the share root,
                         NOT the repo inside it), e.g. /Users/you/nico-tests/vm1/shared
  --share <tag>          UTM share name (default: the one UTM offers, normally "share")
  --yes                  no confirmation prompts
  -h, --help             this text
With --mac-folder and --yes the script needs no terminal.
EOF
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ssh-key)    SSH_PUBKEY="$2"; shift 2 ;;
        --mac-folder) MAC_REPO_PATH="$2"; shift 2 ;;
        --share)      SHARE_NAME="$2"; shift 2 ;;
        --yes|-y)     ASSUME_YES=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Error: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done
# --ssh-key may be a file; validate either form up front (a bad key would
# otherwise surface much later as "passwordless SSH does not work").
if [[ -n "$SSH_PUBKEY" ]]; then
    [[ -f "$SSH_PUBKEY" ]] && SSH_PUBKEY="$(head -1 "$SSH_PUBKEY")"
    if ! ssh-keygen -l -f /dev/stdin <<< "$SSH_PUBKEY" >/dev/null 2>&1; then
        echo "Error: --ssh-key does not parse as an SSH public key" >&2; exit 2
    fi
fi
if [[ -n "$MAC_REPO_PATH" && "$MAC_REPO_PATH" != /* ]]; then
    echo "Error: --mac-folder must be an absolute Mac path (e.g. /Users/you/nico-tests/vm1/shared)" >&2; exit 2
fi
if [[ ! -t 0 && -z "$MAC_REPO_PATH" ]]; then
    echo "Error: no terminal and no --mac-folder — nothing to prompt with. Pass --mac-folder (and --yes)." >&2; exit 2
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

# printf -v, never eval: pasted values (SSH keys especially) can contain
# quotes/$()/backticks — eval would execute them as root on a stray paste.
ask() {
    local prompt="$1" default="$2" var="$3"
    # preset by a command-line flag → no prompt
    if [[ -n "${!var:-}" ]]; then
        echo "  $prompt: ${!var}  (from command line)"
        return
    fi
    if [[ -n "$default" ]]; then
        read -rp "  $prompt [$default]: " val
        printf -v "$var" '%s' "${val:-$default}"
    else
        while true; do
            read -rp "  $prompt: " val
            if [[ -n "$val" ]]; then
                printf -v "$var" '%s' "$val"
                break
            fi
            echo "  (required)"
        done
    fi
}

ask_ssh_key() {
    # Paste-friendly SSH pubkey prompt with validation: a truncated or
    # line-wrapped paste otherwise lands silently in authorized_keys and
    # only fails much later as 'passwordless SSH does not work'.
    local var="$1" val
    if [[ -n "${!var:-}" ]]; then
        echo "  SSH public key: $(ssh-keygen -l -f /dev/stdin <<< "${!var}" 2>/dev/null)  (from command line)"
        return
    fi
    if [[ ! -t 0 ]]; then
        printf -v "$var" '%s' ''   # non-interactive, no key given → skip
        return
    fi
    while true; do
        echo "  Your SSH public key (paste one line from: cat ~/.ssh/id_ed25519.pub)"
        read -rp "  (leave blank to skip — you can run 'ssh-copy-id nico@<vm-ip>' from the Mac later): " val
        if [[ -z "$val" ]]; then
            printf -v "$var" '%s' ''
            return
        fi
        if ssh-keygen -l -f /dev/stdin <<< "$val" >/dev/null 2>&1; then
            printf -v "$var" '%s' "$val"
            return
        fi
        echo "  ✗ That does not parse as an SSH public key (truncated paste?) — try again."
    done
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  nico-dev — First Boot Setup"
echo "============================================================"
echo
echo "This script personalises your nico-dev VM."
echo "It runs once and takes ~2 minutes."
echo
echo "You will need:"
echo "  - Your SSH public key (optional — paste from: cat ~/.ssh/id_ed25519.pub)"
echo "  - The path to your repos folder on Mac (where infra-controller-core lives)"
echo "  (or pass them: first-boot.sh --ssh-key <file> --mac-folder <path> --yes)"
echo
[[ "$ASSUME_YES" -eq 1 ]] || read -rp "Press Enter to continue..."
echo

# ── Step 1: Gather inputs ─────────────────────────────────────────────────────
echo "── Configuration ────────────────────────────────────────────"
echo
ask_ssh_key SSH_PUBKEY
# The hostname is NOT a choice on a golden image: the baked single-node
# kubeadm cluster carries it in the Node object, the kubelet client cert
# (system:node:<name>) and the etcd member. Renaming the VM leaves kubelet
# introducing itself as a node the API server refuses (NotReady, etcd
# crashloop, exec "pod does not exist") — 20260903-#4, found on the first
# clone of nico-dev-golden-20260903 when the old example answer "nico-dev"
# no longer matched the builder's name.
HOSTNAME="$(hostname)"
echo "  VM hostname     : $HOSTNAME  (fixed — bound to the kubeadm node name)"
# Auto-detect the UTM share from the virtio 9p device(s): each offered
# share exposes its name in /sys/bus/virtio/devices/*/mount_tag. Empty
# Path / wrong share mode in UTM = NO device = precise early diagnosis
# (20260828-#1 follow-up; also removes the share-name prompt + the
# name-mismatch failure class).
DETECTED_TAGS=$(cat /sys/bus/virtio/devices/*/mount_tag 2>/dev/null | tr '\0' '\n' | sort -u | grep . || true)
TAG_COUNT=$(echo "$DETECTED_TAGS" | grep -c . || true)
if [[ "$TAG_COUNT" -eq 0 ]]; then
    echo
    echo "ERROR: UTM is not offering ANY shared folder to this VM." >&2
    echo "  In UTM: VM Settings → Sharing →" >&2
    echo "    - Directory Share Mode: VirtFS  (WebDAV/none will not work)" >&2
    echo "    - Path: click Browse and SELECT a folder (must not be empty)" >&2
    echo "  Then REBOOT the VM and run this script again." >&2
    exit 1
elif [[ "$TAG_COUNT" -eq 1 ]]; then
    SHARE_NAME="$DETECTED_TAGS"
    echo "  UTM share detected automatically: '$SHARE_NAME' ✓"
else
    echo "  Multiple UTM shares detected:"
    echo "$DETECTED_TAGS" | sed 's/^/    - /'
    ask "UTM share name (from the list above)" "share" SHARE_NAME
fi
ask "Mac repos PARENT folder — the folder CONTAINING your repo clones, not a repo itself (e.g. /Users/you/projects)" "" MAC_REPO_PATH

# Derive paths — dc/site names come from the baked template yaml, never
# hardcoded (the kubeconfig name must match what configure-clis.py expects:
# {dc_name}-{sitename}.kubeconfig.yaml)
DC_NAME=$(grep -E '^[[:space:]]*dc_name:' "$TEMPLATE_SITE" | head -1 | awk '{print $2}')
SITENAME=$(grep -E '^[[:space:]]*sitename:' "$TEMPLATE_SITE" | head -1 | awk '{print $2}')
DC_NAME="${DC_NAME:-dev}"
SITENAME="${SITENAME:-dev}"

VM_SHARE_MOUNT="/mnt/mac"
USER_MAC_LINK="/home/${USERNAME}/mac"
# sites/<dc>/<site> — the nico-dev convention everywhere (create-dev-site,
# docs, the maintainer tree). A flat sites/<site> here split fabric
# artifacts across two trees and orphaned the kubeconfig (20260826-#3).
SITE_DIR="${VM_SHARE_MOUNT}/sites/${DC_NAME}/${SITENAME}"
SITE_YAML="${SITE_DIR}/${SITENAME}.yaml"
KUBECONFIG_PATH="${SITE_DIR}/${DC_NAME}-${SITENAME}.kubeconfig.yaml"

echo
echo "── Summary ──────────────────────────────────────────────────"
echo "  username        : $USERNAME"
echo "  hostname        : $HOSTNAME"
echo "  UTM share name  : $SHARE_NAME"
echo "  VM mount        : $VM_SHARE_MOUNT → $USER_MAC_LINK"
echo "  Mac repos path  : $MAC_REPO_PATH"
echo "  site yaml       : $SITE_YAML"
echo "  kubeconfig      : $KUBECONFIG_PATH"
echo
if [[ "$ASSUME_YES" -eq 1 ]]; then
    echo "  (--yes: proceeding)"
else
    read -rp "Proceed? [Y/n]: " confirm
    if [[ "${confirm,,}" == "n" ]]; then
        echo "Aborted."
        exit 0
    fi
fi
echo

# ── Step 2: Hostname — kept ───────────────────────────────────────────────────
# Deliberately no rename (see the note at the prompt): the name is the
# kubeadm node name. Verify the binding instead so a mismatch is loud.
echo "Step 2: Hostname '$HOSTNAME' kept (kubeadm node name)..."
NODE_IN_CLUSTER=$(kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "$NODE_IN_CLUSTER" && "$NODE_IN_CLUSTER" != "$HOSTNAME" ]]; then
    echo "ERROR: this VM is named '$HOSTNAME' but the baked cluster's node is '$NODE_IN_CLUSTER'." >&2
    echo "  Fix: sudo hostnamectl set-hostname $NODE_IN_CLUSTER && sudo systemctl restart kubelet, then rerun." >&2
    exit 1
fi
echo "  Hostname: $HOSTNAME ✓"
echo

# ── Step 3: Install SSH key ───────────────────────────────────────────────────
echo "Step 3: Configuring SSH access for $USERNAME..."
SSH_DIR="/home/${USERNAME}/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
chown -R "${USERNAME}:${USERNAME}" "$SSH_DIR"
if [[ -n "$SSH_PUBKEY" ]]; then
    echo "$SSH_PUBKEY" >> "${SSH_DIR}/authorized_keys"
    sort -u "${SSH_DIR}/authorized_keys" -o "${SSH_DIR}/authorized_keys"
    chmod 600 "${SSH_DIR}/authorized_keys"
    chown "${USERNAME}:${USERNAME}" "${SSH_DIR}/authorized_keys"
    echo "  SSH key installed ✓"
else
    echo "  SSH key skipped — add later: ssh-copy-id ${USERNAME}@$(hostname -I | awk '{print $1}')"
fi
echo

# ── Step 3b: Regenerate SSH host keys ─────────────────────────────────────────
# The golden image ships the maintainer VM's host keys; every clone would
# otherwise share identical host PRIVATE keys (user finding 2026-08-26).
# Regenerate per-clone. The restart does not kill the current SSH session,
# but the NEXT connection will warn about a changed host key — expected;
# clear it with: ssh-keygen -R <vm-ip>
echo "Step 3b: Regenerating SSH host keys for this clone..."
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A >/dev/null
systemctl restart ssh
echo "  Host keys regenerated ✓ (next ssh will warn: run 'ssh-keygen -R <vm-ip>' on your Mac)"
echo

# ── Step 4: Mount Mac shared folder ──────────────────────────────────────────
echo "Step 4: Mounting Mac shared folder ($SHARE_NAME → $VM_SHARE_MOUNT)..."
mkdir -p "$VM_SHARE_MOUNT"

FSTAB_LINE="${SHARE_NAME}  ${VM_SHARE_MOUNT}  9p  trans=virtio,version=9p2000.L,rw,_netdev,nofail  0  0"
if grep -q "^${SHARE_NAME}\b" /etc/fstab; then
    echo "  /etc/fstab entry already present — skipping"
else
    echo "$FSTAB_LINE" >> /etc/fstab
    echo "  Added to /etc/fstab ✓"
fi

if mountpoint -q "$VM_SHARE_MOUNT"; then
    echo "  $VM_SHARE_MOUNT already mounted ✓"
else
    mount "$VM_SHARE_MOUNT" 2>/dev/null || true
fi
# HARD GATE (20260828-#1): a missing share must STOP the run — continuing
# writes the personalization onto the clone's local disk, and the real
# mount later lands on top and hides it. Found live on the first
# foreign-Mac certification run.
if ! mountpoint -q "$VM_SHARE_MOUNT"; then
    echo
    echo "ERROR: the Mac shared folder is NOT mounted at $VM_SHARE_MOUNT." >&2
    echo "  In UTM: VM Settings → Sharing →" >&2
    echo "    - Directory Share Mode: VirtFS  (WebDAV/none will not work)" >&2
    echo "    - Shared directory: your Mac folder" >&2
    echo "  Then REBOOT the VM and run this script again." >&2
    exit 1
fi

if [[ ! -e "$USER_MAC_LINK" ]]; then
    ln -s "$VM_SHARE_MOUNT" "$USER_MAC_LINK"
    chown -h "${USERNAME}:${USERNAME}" "$USER_MAC_LINK"
    echo "  Created ~/mac → $VM_SHARE_MOUNT ✓"
fi
echo

# ── Step 5: Copy site yaml with updated folder paths ─────────────────────────
echo "Step 5: Setting up site yaml..."
mkdir -p "$SITE_DIR"

# nico_dev_folder → 'nico-dev': step 8 copies the script bundle to
# <share>/nico-dev, so the user's yaml must point there (their share may not
# contain the maintainer's own checkouts).
sed \
    -e "s|nico_vm_folder:.*|nico_vm_folder: ${VM_SHARE_MOUNT}|" \
    -e "s|nico_mac_folder:.*|nico_mac_folder: ${MAC_REPO_PATH}|" \
    -e "s|nico_dev_folder:.*|nico_dev_folder: nico-dev|" \
    "$TEMPLATE_SITE" > "$SITE_YAML"

chown "${USERNAME}:${USERNAME}" "$SITE_YAML"
echo "  Written: $SITE_YAML"
echo "    nico_vm_folder  → $VM_SHARE_MOUNT"
echo "    nico_mac_folder → $MAC_REPO_PATH"
echo

# ── Step 6: Write kubeconfig to the share ────────────────────────────────────
echo "Step 6: Writing kubeconfig..."
VM_IP=$(ip route get 192.168.64.1 2>/dev/null | grep -oP 'src \K\S+' \
        || ip -4 addr show scope global | grep -oP '(?<=inet )[\d.]+' | head -1)
echo "  VM IP: $VM_IP"

# Defensive: patch k8s manifests if IP changed (no-op when IP is static)
OLD_IP=$(grep -oP 'https://\K[\d.]+(?=:6443)' /etc/kubernetes/admin.conf | head -1 || true)
if [[ -n "$OLD_IP" && "$OLD_IP" != "$VM_IP" ]]; then
    echo "  IP changed: $OLD_IP → $VM_IP — patching k8s manifests"
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/admin.conf
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/manifests/kube-apiserver.yaml
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/manifests/etcd.yaml
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/kubelet.conf
    echo "  Patched admin.conf, kube-apiserver.yaml, etcd.yaml, kubelet.conf ✓"
fi

cp /etc/kubernetes/admin.conf "$KUBECONFIG_PATH"
chown "${USERNAME}:${USERNAME}" "$KUBECONFIG_PATH"
echo "  Kubeconfig written → https://${VM_IP}:6443 ✓"
echo

# ── Step 7: Write /etc/nico-dev/env ──────────────────────────────────────────
echo "Step 7: Writing /etc/nico-dev/env..."
mkdir -p /etc/nico-dev
cat > /etc/nico-dev/env <<EOF
NICO_DEV_SITE=${SITE_YAML}
NICO_DEV_USER=${USERNAME}
EOF
echo "  NICO_DEV_SITE=${SITE_YAML} ✓"
echo

# ── Step 8: Copy script bundle to Mac shared folder ──────────────────────────
echo "Step 8: Copying nico-dev scripts to Mac share..."
NICO_DEV_SHARE="${VM_SHARE_MOUNT}/nico-dev"
mkdir -p "$NICO_DEV_SHARE"
# rsync writes temp+rename (fresh inodes) — never truncates a file some
# process (like THIS script) is still reading (20260826-#4)
rsync -a /usr/local/lib/nico-dev/ "$NICO_DEV_SHARE/"
chown -R "${USERNAME}:${USERNAME}" "$NICO_DEV_SHARE"
chown -R "${USERNAME}:${USERNAME}" "$SITE_DIR"
echo "  Scripts → $NICO_DEV_SHARE ✓"
echo "  Site    → $SITE_DIR ✓"
echo

# ── Step 9: Add KUBECONFIG to nico's shell profile ───────────────────────────
echo "Step 9: Configuring shell environment..."
BASHRC="/home/${USERNAME}/.bashrc"
# REPLACE, never skip: the baked image carries the maintainer's KUBECONFIG
# line, and append-if-absent left that stale path in every clone
# (20260826-#3).
sed -i '/# nico-dev cluster/d; /export KUBECONFIG=/d' "$BASHRC" 2>/dev/null || true
cat >> "$BASHRC" <<EOF

# nico-dev cluster
export KUBECONFIG=${KUBECONFIG_PATH}
EOF
echo "  KUBECONFIG set in ~/.bashrc → ${KUBECONFIG_PATH} ✓"
echo

# ── Step 10: Restart services → Nico comes up ────────────────────────────────
echo "Step 10: Restarting nico-dev-fabric..."
systemctl restart nico-dev-fabric
echo "  Services restarted ✓"
echo

# ── Step 11: Wait for Nico pods ──────────────────────────────────────────────
echo "Step 11: Waiting for Nico pods (up to 3 min)..."
deadline=$((SECONDS + 180))
while [[ $SECONDS -lt $deadline ]]; do
    # grep -c exits 1 on zero matches; under set -e -o pipefail that killed
    # the script on the HAPPY path (all pods already Running — the clone
    # boots into a working cluster), silently skipping the summary and
    # POST-SETUP.txt (20260826-#2). '|| true' keeps the count usable.
    not_running=$(kubectl --kubeconfig /etc/kubernetes/admin.conf get pods -n nico-system \
        --no-headers 2>/dev/null | grep -cvE "Running|Completed" || true)
    total=$(kubectl --kubeconfig /etc/kubernetes/admin.conf get pods -n nico-system \
        --no-headers 2>/dev/null | wc -l || true)
    if [[ $total -gt 0 && $not_running -eq 0 ]]; then
        echo "  All $total pods Running/Completed ✓"
        break
    fi
    echo "  Waiting... ($not_running/$total not ready)"
    sleep 10
done
echo

# ── Done ──────────────────────────────────────────────────────────────────────
# Service VIP prefix comes from the site yaml (octets vary per site)
SVC_VIPS=$(grep -E '^\s*service_vips:' "$SITE_YAML" | head -1 | awk '{print $2}')
SVC_VIPS="${SVC_VIPS:-<service-vips-prefix>}"

SUMMARY="============================================================
  Setup complete! Welcome to nico-dev.
============================================================

SSH access:
  ssh ${USERNAME}@${VM_IP}
  (first reconnect will warn about a changed host key — this clone
   regenerated its own; clear with: ssh-keygen -R ${VM_IP})

Check cluster (on VM):
  kubectl get pods -n nico-system
  ndev                      # overall site status
  ndev fabric verify        # full fabric health check
  ndev fabric shell         # explore the switches (vtysh) — see how-to.md

On your Mac:

  # 1. Add route to Nico service VIPs (re-run after each network change)
  sudo route -n add -net ${SVC_VIPS} ${VM_IP}

  # 2. Set KUBECONFIG
  export KUBECONFIG=${MAC_REPO_PATH}/sites/${DC_NAME}/${SITENAME}/${DC_NAME}-${SITENAME}.kubeconfig.yaml

  # 3. Build and configure CLIs
  python3 ${MAC_REPO_PATH}/nico-dev/build-nico-clis.py ${MAC_REPO_PATH}/sites/${DC_NAME}/${SITENAME} --install-to ~/.local/bin
  python3 ${MAC_REPO_PATH}/nico-dev/configure-clis.py ${MAC_REPO_PATH}/sites/${DC_NAME}/${SITENAME}"

echo "$SUMMARY"

# Persist — the terminal scrolls away, this file does not
echo "$SUMMARY" > "/home/${USERNAME}/POST-SETUP.txt"
chown "${USERNAME}:${USERNAME}" "/home/${USERNAME}/POST-SETUP.txt"
echo
echo "(These instructions are saved in ~/POST-SETUP.txt)"

}
main "$@"
