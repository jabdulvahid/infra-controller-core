#!/usr/bin/env bash
# nico-dev — one-command onboarding from a golden image ZIP (macOS, Apple Silicon)
#
#   onboard-golden.sh --zip ~/Downloads/nico-dev-golden-20260903.utm.zip \
#                     --dest ~/nico-tests/vm-20260903 [--ssh-key ~/.ssh/id_ed25519.pub] \
#                     [--vm-ip 192.168.64.126] [--skip-clone] [--skip-ui-share] [--dump-ui]
#
# From "I have the zip" to "the admin UI is open", hands off except for the
# things macOS itself insists on:
#   * the first run pops two one-time permission dialogs — Accessibility
#     (for the UI-scripted share step) and Automation (UTM / System Events)
#   * one sudo prompt for the VIP route
#
# Steps (each idempotent — rerun after a failure, it skips what is done):
#   1  UTM present (brew install --cask utm if not), launched once
#   2  unzip the golden bundle into --dest (the ZIP is KEPT for re-tests)
#   3  import into UTM (open <bundle>) and wait for utmctl to list it
#   4  shared folder: <dest>/shared, clone NVIDIA/infra-controller + graft tools
#   5  set the UTM shared directory by UI scripting (the one thing UTM does
#      not expose to scripts for QEMU VMs — 20260903-#5); verified from
#      inside the VM, manual fallback printed if it did not stick
#   6  start the VM, wait for ssh
#   7  install your key (expect + the image's default password), then run
#      first-boot.sh NON-INTERACTIVELY — the current one, copied in over ssh,
#      so an image baked with the older interactive script works too
#   8  wait for the cluster, add the VIP route (sudo), open the admin UI
#
# Explicit platform gate: macOS only (UTM). Linux hosts use dev-up.py.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "Error: macOS only (UTM). On Linux use dev-up.py." >&2; exit 1; }
[[ "$(uname -m)" == "arm64" ]] || { echo "Error: Apple Silicon only (the golden image is arm64)." >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
ZIP=""; DEST=""; SSH_KEY=""; VM_IP="192.168.64.126"; VM_USER="nico"; VM_PASS='Welcome123!'
SKIP_CLONE=0; SKIP_UI_SHARE=0; DUMP_UI=0; REPO_URL="https://github.com/NVIDIA/infra-controller.git"
GRAFT_URL="https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh"

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip) ZIP="$2"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --vm-ip) VM_IP="$2"; shift 2 ;;
        --password) VM_PASS="$2"; shift 2 ;;
        --repo-url) REPO_URL="$2"; shift 2 ;;
        --skip-clone) SKIP_CLONE=1; shift ;;
        --skip-ui-share) SKIP_UI_SHARE=1; shift ;;
        --dump-ui) DUMP_UI=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Error: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m━━ %s\033[0m\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }
warn() { printf '  ⚠ %s\n' "$1"; }
die()  { printf '  ✗ %s\n' "$1" >&2; exit 1; }

# ── --dump-ui: print UTM's front-window accessibility tree and exit ─────────
# For adapting the UI-scripting step to a UTM version whose layout differs.
if [[ "$DUMP_UI" -eq 1 ]]; then
    open -a UTM; sleep 2
    osascript -e 'tell application "System Events" to tell process "UTM" to get entire contents of window 1'
    exit 0
fi

[[ -n "$ZIP" && -f "$ZIP" ]] || die "--zip <golden image zip> is required and must exist"
[[ -n "$DEST" ]] || die "--dest <folder> is required (e.g. ~/nico-tests/vm-20260903)"
DEST="${DEST/#\~/$HOME}"; ZIP="${ZIP/#\~/$HOME}"
SHARE_DIR="$DEST/shared"
if [[ -z "$SSH_KEY" ]]; then
    SSH_KEY="$(ls "$HOME"/.ssh/id_ed25519.pub "$HOME"/.ssh/id_*.pub 2>/dev/null | head -1 || true)"
fi
[[ -n "$SSH_KEY" && -f "${SSH_KEY/#\~/$HOME}" ]] || die "no SSH public key (pass --ssh-key, or: ssh-keygen -t ed25519)"
SSH_KEY="${SSH_KEY/#\~/$HOME}"; PRIV="${SSH_KEY%.pub}"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$PRIV" -o ConnectTimeout=8 "$VM_USER@$VM_IP")

echo "nico-dev — onboard from golden image"
echo "  zip        : $ZIP  (kept)"
echo "  dest       : $DEST"
echo "  share      : $SHARE_DIR"
echo "  ssh key    : $SSH_KEY"
echo "  vm         : $VM_USER@$VM_IP"

# ── 1. UTM ─────────────────────────────────────────────────────────────────
say "1/8 UTM"
if [[ ! -x /Applications/UTM.app/Contents/MacOS/utmctl ]]; then
    command -v brew >/dev/null || die "UTM not installed and Homebrew missing — install UTM from https://mac.getutm.app then rerun"
    echo "  installing UTM (brew install --cask utm)…"
    brew install --cask utm
fi
UTMCTL=/Applications/UTM.app/Contents/MacOS/utmctl
open -a UTM; sleep 2
"$UTMCTL" list >/dev/null 2>&1 || warn "utmctl needs the Automation permission — if macOS asked, click Allow and rerun"
ok "UTM present, running"

# ── 2. unzip (zip retained) ────────────────────────────────────────────────
say "2/8 unpack the golden bundle into $DEST"
mkdir -p "$DEST"
BUNDLE="$(find "$DEST" -maxdepth 1 -name '*.utm' -type d | head -1 || true)"
if [[ -z "$BUNDLE" ]]; then
    echo "  unzipping (this is ~11 GB compressed → ~26 GB)…"
    ditto -x -k "$ZIP" "$DEST"
    BUNDLE="$(find "$DEST" -maxdepth 1 -name '*.utm' -type d | head -1 || true)"
    [[ -n "$BUNDLE" ]] || die "no .utm bundle found in $DEST after unzip"
fi
VM_NAME="$(plutil -extract Information.Name raw -o - "$BUNDLE/config.plist" 2>/dev/null || basename "$BUNDLE" .utm)"
ok "bundle $(basename "$BUNDLE")  (VM name: $VM_NAME)"

# ── 3. import into UTM ─────────────────────────────────────────────────────
say "3/8 import into UTM"
if "$UTMCTL" list 2>/dev/null | grep -q " $VM_NAME\$"; then
    ok "already imported"
else
    open "$BUNDLE"
    for _ in $(seq 1 30); do
        "$UTMCTL" list 2>/dev/null | grep -q " $VM_NAME\$" && break
        sleep 2
    done
    "$UTMCTL" list 2>/dev/null | grep -q " $VM_NAME\$" || die "UTM did not list '$VM_NAME' after import — open UTM and check"
    ok "imported"
fi
"$UTMCTL" status "$VM_NAME" 2>/dev/null | grep -qi started && die "'$VM_NAME' is already running — stop it first (the share must be set while stopped)"

# ── 4. shared folder + repo + tools ────────────────────────────────────────
say "4/8 shared folder: repo + nico-dev tools"
mkdir -p "$SHARE_DIR"
if [[ "$SKIP_CLONE" -eq 1 ]]; then
    ok "clone skipped (--skip-clone)"
elif [[ -d "$SHARE_DIR/infra-controller/.git" || -f "$SHARE_DIR/infra-controller/.git" ]]; then
    ok "infra-controller already present"
else
    git clone "$REPO_URL" "$SHARE_DIR/infra-controller"
    ok "cloned $REPO_URL"
fi
if [[ -d "$SHARE_DIR/infra-controller" && ! -f "$SHARE_DIR/infra-controller/tools/nico-dev/dev-up.py" ]]; then
    (cd "$SHARE_DIR/infra-controller" && curl -fsSL "$GRAFT_URL" | bash)
fi
[[ -f "$SHARE_DIR/infra-controller/tools/nico-dev/dev-up.py" ]] && ok "nico-dev tools grafted (stable channel)"

# ── 5. the shared directory in UTM (UI scripting) ──────────────────────────
# UTM exposes no share-path property for QEMU VMs (20260828-#2, 20260903-#5);
# the only legitimate way to grant the sandbox bookmark is UTM's own file
# dialog. Drive it: select the VM, Edit, Sharing, Browse…, ⌘⇧G, path, Open,
# Save. Verified afterwards from INSIDE the VM (step 6), never assumed.
set_share_via_ui() {
    osascript - "$VM_NAME" "$SHARE_DIR" <<'APPLESCRIPT'
on run argv
    set vmName to item 1 of argv
    set sharePath to item 2 of argv
    tell application "UTM" to activate
    delay 1
    tell application "System Events"
        tell process "UTM"
            set frontmost to true
            -- 1. select the VM in the sidebar (rows whose text contains the name)
            set sidebarRows to rows of outline 1 of scroll area 1 of splitter group 1 of window 1
            repeat with r in sidebarRows
                if (value of static text 1 of UI element 1 of r) contains vmName then
                    select r
                    exit repeat
                end if
            end repeat
            delay 0.5
            -- 2. Edit (⌘E opens the settings sheet for the selected VM)
            keystroke "e" using command down
            delay 1.5
            -- 3. Sharing pane in the settings sidebar
            set paneRows to rows of outline 1 of scroll area 1 of sheet 1 of window 1
            repeat with r in paneRows
                if (value of static text 1 of UI element 1 of r) is "Sharing" then
                    select r
                    exit repeat
                end if
            end repeat
            delay 0.8
            -- 4. Browse… → Go to folder (⌘⇧G) → path → Open
            click button "Browse…" of sheet 1 of window 1
            delay 1
            keystroke "g" using {command down, shift down}
            delay 0.6
            keystroke sharePath
            delay 0.4
            keystroke return
            delay 0.8
            keystroke return  -- "Open" is the default button
            delay 1
            -- 5. Save the settings sheet
            click button "Save" of sheet 1 of window 1
            delay 0.8
        end tell
    end tell
end run
APPLESCRIPT
}
say "5/8 UTM shared directory → $SHARE_DIR"
if [[ "$SKIP_UI_SHARE" -eq 1 ]]; then
    warn "UI step skipped (--skip-ui-share): set it by hand — UTM → $VM_NAME → Settings → Sharing → Path"
else
    if set_share_via_ui; then
        ok "UI scripting ran (verified in step 6)"
    else
        warn "UI scripting failed (Accessibility permission? UTM layout changed? try --dump-ui)."
        warn "Set it by hand: UTM → $VM_NAME → Settings → Sharing → Directory Share Mode VirtFS, Path $SHARE_DIR"
        read -rp "  Press Enter when done (or Ctrl-C)… "
    fi
fi

# ── 6. start + wait for ssh ────────────────────────────────────────────────
say "6/8 start $VM_NAME, wait for ssh"
ssh-keygen -R "$VM_IP" >/dev/null 2>&1 || true
"$UTMCTL" start "$VM_NAME"
for i in $(seq 1 90); do nc -z -w 2 "$VM_IP" 22 2>/dev/null && break; sleep 2; done
nc -z -w 2 "$VM_IP" 22 2>/dev/null || die "ssh not reachable at $VM_IP after 3 min (only ONE nico-dev VM may run at a time)"
ok "ssh port open"

# ── 7. key auth (expect + default password), then first-boot non-interactively
say "7/8 personalize (first-boot.sh, non-interactive)"
if ! "${SSH[@]}" -o BatchMode=yes true 2>/dev/null; then
    command -v expect >/dev/null || die "expect not found (it ships with macOS) — run: ssh-copy-id $VM_USER@$VM_IP  then rerun"
    expect -c "
        set timeout 60
        spawn ssh-copy-id -o StrictHostKeyChecking=accept-new -i \"$SSH_KEY\" $VM_USER@$VM_IP
        expect {
            -re {[Pp]assword:} { send \"$VM_PASS\r\"; exp_continue }
            eof
        }" >/dev/null
    "${SSH[@]}" -o BatchMode=yes true || die "key auth still failing — password changed? use --password"
    ok "ssh key installed"
else
    ok "key auth already works"
fi
# share really offered by UTM? (this is the verification of step 5)
if ! "${SSH[@]}" 'cat /sys/bus/virtio/devices/*/mount_tag 2>/dev/null | tr "\0" "\n" | grep -qx share'; then
    die "UTM is not offering the 'share' directory to the VM. Stop the VM, set UTM → $VM_NAME → Settings → Sharing (VirtFS, Path $SHARE_DIR), rerun."
fi
ok "share device present in the VM"
if "${SSH[@]}" 'test -s /etc/nico-dev/env' 2>/dev/null; then
    ok "first-boot already done ($("${SSH[@]}" cat /etc/nico-dev/env))"
else
    # the CURRENT first-boot.sh (flags, no hostname prompt), regardless of
    # which one the image was baked with
    scp -q -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$PRIV" \
        "$HERE/first-boot.sh" "$VM_USER@$VM_IP:/tmp/first-boot.sh"
    KEY_LINE="$(head -1 "$SSH_KEY")"
    "${SSH[@]}" -t "sudo bash /tmp/first-boot.sh --ssh-key '$KEY_LINE' --mac-folder '$SHARE_DIR' --yes"
    ssh-keygen -R "$VM_IP" >/dev/null 2>&1 || true      # first-boot regenerates host keys
    ok "first-boot complete"
fi

# ── 8. cluster, route, UI ──────────────────────────────────────────────────
say "8/8 cluster, route, admin UI"
echo "  waiting for nico-system pods…"
for i in $(seq 1 60); do
    NOT_READY="$("${SSH[@]}" 'kubectl get pods -n nico-system --no-headers 2>/dev/null | grep -v -E "Running|Completed" | wc -l' 2>/dev/null | tr -d ' ' || echo 99)"
    [[ "$NOT_READY" == "0" ]] && break
    sleep 10
done
[[ "$NOT_READY" == "0" ]] && ok "all nico-system pods Running/Completed" || warn "some pods still settling — kubectl get pods -n nico-system on the VM"
SITE_YAML="$(find "$SHARE_DIR/sites" -maxdepth 3 -name '*.yaml' ! -name '*kubeconfig*' 2>/dev/null | head -1 || true)"
API_VIP="$(grep -E '^\s*api_vip:' "$SITE_YAML" 2>/dev/null | sed -E 's/.*"([0-9.]+)".*/\1/' || true)"
if [[ -n "$API_VIP" ]]; then
    VIP_NET="$(echo "$API_VIP" | awk -F. '{print $1"."$2"."$3".0/27"}')"
    echo "  route ${VIP_NET} via $VM_IP (sudo)…"
    sudo route -n add -net "$VIP_NET" "$VM_IP" >/dev/null 2>&1 || true
    ok "route present"
    echo
    echo "  Admin UI : https://$API_VIP/admin"
    echo "  KUBECONFIG=$(find "$SHARE_DIR/sites" -name '*.kubeconfig.yaml' | head -1)"
    open "https://$API_VIP/admin" || true
else
    warn "could not find the site yaml under $SHARE_DIR/sites — first-boot may not have written it"
fi
echo
echo "Done. Re-test any time: stop + delete '$VM_NAME' in UTM, remove $DEST/*.utm, rerun with the same --zip."
