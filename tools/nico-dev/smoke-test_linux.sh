#!/usr/bin/env bash
# nico-dev — boot-path smoke test, LINUX HOST edition (libvirt/KVM).
#
#   smoke-test.sh            # via the dispatcher: build + boot + assert + delete
#   smoke-test.sh --keep     # keep the VM on failure for autopsy
#
# Asserts: VM creates (nico-nat/pool ensured), boots, static IP up EARLY,
# ssh answers, cloud-init reaches done, guest arch matches the host.
# Throwaway VM 'nico-smoke' on .124 — never collides with real VMs.

set -euo pipefail

NAME="nico-smoke"
HOST_NUM=124
MEM_MB=4096; CPUS=2; DISK_GB=20
HERE="$(cd "$(dirname "$0")" && pwd)"
CONN="qemu:///system"
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
        virsh -c "$CONN" destroy "$NAME" >/dev/null 2>&1 || true
        virsh -c "$CONN" undefine "$NAME" --remove-all-storage >/dev/null 2>&1 || true
        rm -rf "$HOME/.cache/nico-dev/work/$NAME" "$HOME/.nico-dev/vms/$NAME.yaml"
        [[ -n "${IP:-}" ]] && ssh-keygen -R "$IP" >/dev/null 2>&1 || true
    elif [[ "$PASS" -eq 0 ]]; then
        echo "FAILED — VM kept for autopsy (--keep): virsh console $NAME | ssh nico@${IP:-?}" >&2
    fi
}
trap teardown EXIT

echo "━━ smoke: build + boot throwaway VM ($NAME, ${MEM_MB}MB/${CPUS}cpu/${DISK_GB}G) ━━"
ssh-keygen -R "192.168.64.$HOST_NUM" >/dev/null 2>&1 || true
OUT=$(mktemp)
if ! python3 "$HERE/build-nico-dev-vm.py" --name "$NAME" \
        --host-num "$HOST_NUM" --mem-mb "$MEM_MB" --cpus "$CPUS" \
        --disk-gb "$DISK_GB" --ssh-key "$SSH_KEY" --dc smk --site smoke \
        2>&1 | tee "$OUT"; then
    echo "✗ SMOKE FAIL: build/boot step failed" >&2; exit 1
fi
IP=$(grep -oE 'static IP ([0-9.]+)' "$OUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
[[ -n "$IP" ]] || { echo "✗ SMOKE FAIL: could not parse static IP" >&2; exit 1; }
rm -f "$OUT"

SSH=(ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes
     -i "$PRIV" -o ConnectTimeout=10 "nico@$IP")

echo "━━ smoke: cloud-init must reach done ━━"
CI_OUT=$("${SSH[@]}" 'timeout 900 cloud-init status --wait >/dev/null 2>&1; cloud-init status --long' || true)
if ! echo "$CI_OUT" | grep -q 'done'; then
    echo "✗ SMOKE FAIL: cloud-init not done. Status:" >&2
    echo "$CI_OUT" | sed 's/^/    /' >&2
    "${SSH[@]}" 'sudo grep -iE "error|warn|fail" /var/log/cloud-init.log | tail -10' \
        2>/dev/null | sed 's/^/    /' >&2 || true
    exit 1
fi
echo "  cloud-init done ✓"

echo "━━ smoke: static IP + arch + share ━━"
"${SSH[@]}" "ip -4 -br addr" | grep -q "$IP" \
    || { echo "✗ SMOKE FAIL: static IP not on the NIC" >&2; exit 1; }
GUEST_ARCH=$("${SSH[@]}" uname -m)
[[ "$GUEST_ARCH" == "$(uname -m)" ]] \
    || { echo "✗ SMOKE FAIL: guest arch $GUEST_ARCH != host $(uname -m)" >&2; exit 1; }
# the virtiofs device must be visible (mount itself is prepare-vm's job)
"${SSH[@]}" 'grep -qs share /sys/bus/virtio/devices/*/mount_tag' \
    || { echo "✗ SMOKE FAIL: virtiofs share tag not visible in the guest" >&2; exit 1; }
echo "  $IP on NIC ✓   guest arch $GUEST_ARCH ✓   share tag visible ✓"

PASS=1
echo ""
echo "✓ SMOKE PASS in $(( $(date +%s) - START ))s — Linux boot path is good (VM deleted)"
