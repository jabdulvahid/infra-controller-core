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
SKIP_CLONE=0; SKIP_UI_SHARE=0; DUMP_UI=0; TEST_SHARE=0; VM_NAME_OPT=""
REPO_URL="https://github.com/NVIDIA/infra-controller.git"
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
        --test-share) TEST_SHARE=1; shift ;;      # run ONLY the UI share step against an imported VM
        --vm-name) VM_NAME_OPT="$2"; shift 2 ;;   # with --test-share: which VM (default: bundle in --dest)
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

[[ "$TEST_SHARE" -eq 1 || ( -n "$ZIP" && -f "$ZIP" ) ]] || die "--zip <golden image zip> is required and must exist"
[[ -n "$DEST" ]] || die "--dest <folder> is required (e.g. ~/nico-tests/vm-20260903)"
DEST="${DEST/#\~/$HOME}"; ZIP="${ZIP/#\~/$HOME}"
SHARE_DIR="$DEST/shared"
if [[ -z "$SSH_KEY" ]]; then
    # deterministic preference (NOT `ls | head`: locale collation sorts
    # id_ed25519_git_signing.pub before id_ed25519.pub — picked a signing key
    # on the first live run)
    for cand in id_ed25519.pub id_ecdsa.pub id_rsa.pub; do
        [[ -f "$HOME/.ssh/$cand" ]] && { SSH_KEY="$HOME/.ssh/$cand"; break; }
    done
    [[ -n "$SSH_KEY" ]] || SSH_KEY="$(find "$HOME/.ssh" -maxdepth 1 -name 'id_*.pub' | sort | head -1 || true)"
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

# ── 0. can we drive UTM's GUI at all? (Accessibility) ──────────────────────
# Probe BEFORE the 26 GB unzip: without the Accessibility permission the UI
# step (5) cannot work, and macOS grants it only from System Settings.
# -1743 = "not allowed assistive access". Any other answer means we may look.
if [[ "$SKIP_UI_SHARE" -eq 0 ]]; then
    say "0/8 Accessibility (needed to drive UTM's Sharing dialog)"
    open -a UTM; sleep 2
    if PROBE="$(osascript -e 'tell application "System Events" to tell process "UTM" to get name of window 1' 2>&1)"; then
        ok "System Events can see UTM (window: ${PROBE:-?})"
    elif grep -q "1743\|assistive" <<< "$PROBE"; then
        echo "  ✗ your terminal app is not allowed to control other apps." >&2
        echo "    System Settings → Privacy & Security → Accessibility → enable your terminal" >&2
        echo "    (Terminal, iTerm2, Warp, …), then rerun. Or pass --skip-ui-share to set the" >&2
        echo "    UTM shared directory by hand." >&2
        exit 1
    else
        warn "System Events probe returned: ${PROBE} — continuing (UTM may have no window yet)"
    fi
fi

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

# ── --test-share: exercise ONLY the UI share step, then exit ───────────────
if [[ "$TEST_SHARE" -eq 1 ]]; then
    VM_NAME="$VM_NAME_OPT"
    if [[ -z "$VM_NAME" ]]; then
        B="$(find "$DEST" -maxdepth 1 -name '*.utm' -type d | head -1 || true)"
        [[ -n "$B" ]] || die "--test-share needs --vm-name (no .utm bundle in $DEST to read the name from)"
        VM_NAME="$(plutil -extract Information.Name raw -o - "$B/config.plist" 2>/dev/null || basename "$B" .utm)"
    fi
    "$UTMCTL" status "$VM_NAME" 2>/dev/null | grep -qi started && die "'$VM_NAME' is running — the share is set while stopped"
    say "test: UTM shared directory of '$VM_NAME' → $SHARE_DIR"
    if TITLE="$(set_share_via_ui)"; then
        echo "  picker now shows: '$TITLE'  (want: '$(basename "$SHARE_DIR")')"
        [[ "$TITLE" == "$(basename "$SHARE_DIR")" ]] && ok "share set" || warn "title differs — check UTM"
    else
        die "UI scripting failed — rerun --dump-ui and share the tree"
    fi
    exit 0
fi

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
# Layout from a live --dump-ui (UTM 4.x, 2026-09-04):
#   window 1 → group 1 → splitter group 1 →
#     group 1 → scroll area 1 → outline 1 → rows (sidebar; UI element 1 →
#               static text 1 = VM name)
#     group 2 → scroll area 1 (detail pane) → … static text "Shared Directory",
#               then ONE `menu button` titled with the current share folder
#               (or the placeholder when none) — UTM's quick share picker,
#               whose menu has "Browse…". No Settings sheet needed.
# Prints the menu button's title afterwards (= the chosen folder's name).
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
            set win to window 1
            set sidebar to outline 1 of scroll area 1 of group 1 of splitter group 1 of group 1 of win
            -- 1. select the VM row by name
            set found to false
            repeat with r in rows of sidebar
                try
                    if (value of static text 1 of UI element 1 of r) is vmName then
                        set selected of r to true
                        set found to true
                        exit repeat
                    end if
                end try
            end repeat
            if not found then error "VM row '" & vmName & "' not found in the UTM sidebar"
            delay 0.8
            -- 2. the Shared Directory quick picker in the detail pane
            set detail to scroll area 1 of group 2 of splitter group 1 of group 1 of win
            set mb to menu button 1 of detail
            click mb
            delay 0.7
            set picked to false
            repeat with mi in menu items of menu 1 of mb
                try
                    if (name of mi) starts with "Browse" then
                        click mi
                        set picked to true
                        exit repeat
                    end if
                end try
            end repeat
            if not picked then error "no Browse… item in the Shared Directory menu"
            delay 1.2
            -- 3. the Open panel: Go to folder (⌘⇧G), type the path, confirm twice
            keystroke "g" using {command down, shift down}
            delay 0.7
            keystroke sharePath
            delay 0.5
            keystroke return
            delay 0.9
            keystroke return
            delay 1.2
            return title of menu button 1 of detail
        end tell
    end tell
end run
APPLESCRIPT
}
say "5/8 UTM shared directory → $SHARE_DIR"
if [[ "$SKIP_UI_SHARE" -eq 1 ]]; then
    warn "UI step skipped (--skip-ui-share): set it by hand — UTM → $VM_NAME → Settings → Sharing → Path"
else
    if TITLE="$(set_share_via_ui)"; then
        if [[ "$TITLE" == "$(basename "$SHARE_DIR")" ]]; then
            ok "UTM picker shows '$TITLE' (verified again from inside the VM in step 7)"
        else
            warn "UTM picker shows '$TITLE', expected '$(basename "$SHARE_DIR")' — will verify from inside the VM"
        fi
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
