#!/usr/bin/env bash
# nico-dev — boot-path smoke test (~6 min, unattended).
#
#   smoke-test.sh                  # build + boot a throwaway VM, assert, delete
#   smoke-test.sh --keep           # keep the VM on failure for autopsy
#   smoke-test.sh --ssh-key ~/.ssh/id_foo.pub
#
# RULE (20260901, after a day of boot-path regressions): no push that
# touches the cloud-init seed, the VM creation record, or prepare-vm's
# guest section without a green smoke run.
#
# What it asserts: VM creates, boots, static IP up EARLY (seed
# network-config), ssh answers, cloud-init reaches 'done', arch matches
# the host. No share, no site, no k8s — this is the boot path only.

set -euo pipefail

NAME="nico-smoke"
HOST_NUM=124                       # off the .126 convention — never collides
MEM_MB=4096; CPUS=2; DISK_GB=20    # bare Ubuntu + cloud-init only
HERE="$(cd "$(dirname "$0")" && pwd)"
UTMCTL="/Applications/UTM.app/Contents/MacOS/utmctl"
KEEP=0; SSH_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep)    KEEP=1; shift ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$SSH_KEY" ]]; then
    SSH_KEY="$(ls "$HOME"/.ssh/id_*.pub 2>/dev/null | head -1)"
fi
[[ -n "$SSH_KEY" && -f "${SSH_KEY/#\~/$HOME}" ]] || {
    echo "Error: no SSH public key (pass --ssh-key)" >&2; exit 1; }
PRIV="${SSH_KEY%.pub}"

PASS=0
START=$(date +%s)
teardown() {
    if [[ "$PASS" -eq 1 || "$KEEP" -eq 0 ]]; then
        "$UTMCTL" stop "$NAME" >/dev/null 2>&1 || true
        "$UTMCTL" delete "$NAME" >/dev/null 2>&1 || true
        rm -rf "$HOME/UTM-disks/$NAME"
        [[ -n "${IP:-}" ]] && ssh-keygen -R "$IP" >/dev/null 2>&1 || true
    elif [[ "$PASS" -eq 0 ]]; then
        echo "FAILED — VM kept for autopsy (--keep): $NAME @ ${IP:-?}" >&2
    fi
}
trap teardown EXIT

echo "━━ smoke: build + boot throwaway VM ($NAME, ${MEM_MB}MB/${CPUS}cpu/${DISK_GB}G) ━━"
ssh-keygen -R "192.168.64.$HOST_NUM" >/dev/null 2>&1 || true
OUT=$(mktemp)
# stdin </dev/null skips the share-Path pause — a smoke VM needs no share
if ! python3 "$HERE/build-nico-dev-vm.py" --name "$NAME" \
        --host-num "$HOST_NUM" --mem-mb "$MEM_MB" --cpus "$CPUS" \
        --disk-gb "$DISK_GB" --ssh-key "$SSH_KEY" \
        </dev/null 2>&1 | tee "$OUT"; then
    echo "✗ SMOKE FAIL: build/boot step failed" >&2; exit 1
fi
IP=$(grep -oE 'static IP[^0-9]*([0-9.]+)' "$OUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
[[ -n "$IP" ]] || { echo "✗ SMOKE FAIL: could not parse static IP" >&2; exit 1; }
rm -f "$OUT"

SSH=(ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes
     -i "$PRIV" -o ConnectTimeout=10 "nico@$IP")

echo "━━ smoke: cloud-init must reach done ━━"
"${SSH[@]}" 'timeout 900 cloud-init status --wait >/dev/null; cloud-init status' \
    | grep -q 'done' || { echo "✗ SMOKE FAIL: cloud-init not done" >&2; exit 1; }
echo "  cloud-init done ✓"

echo "━━ smoke: static IP + arch ━━"
"${SSH[@]}" "ip -4 -br addr" | grep -q "$IP" \
    || { echo "✗ SMOKE FAIL: static IP not on the NIC" >&2; exit 1; }
GUEST_ARCH=$("${SSH[@]}" uname -m)
HOST_ARCH=$(uname -m)
WANT=$([[ "$HOST_ARCH" == "arm64" ]] && echo aarch64 || echo x86_64)
[[ "$GUEST_ARCH" == "$WANT" ]] \
    || { echo "✗ SMOKE FAIL: guest arch $GUEST_ARCH != $WANT" >&2; exit 1; }
echo "  $IP on NIC ✓   guest arch $GUEST_ARCH ✓"

PASS=1
echo ""
echo "✓ SMOKE PASS in $(( $(date +%s) - START ))s — boot path is good (VM deleted)"
