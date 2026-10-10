#!/usr/bin/env bash
# Build the Enclosure DPU image: vmctl `vm image build` of this child recipe on
# top of library/dpu:latest, then flatten to a standalone QCOW2 for libvirt.
#
#   bash build.sh [--out PATH] [--tag NAME:TAG]
#   HBN_IMAGE=nvcr.io/nvidia/doca/doca_hbn@sha256:... bash build.sh
#
# The vm daemon resolves relative paths from $HOME, so every path is absolute.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="enclosure/dpu:latest"
OUT="$HOME/enclosure-dpu.qcow2"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        *) echo "usage: $0 [--out PATH] [--tag NAME:TAG]" >&2; exit 2 ;;
    esac
done

command -v vm >/dev/null || { echo "vm (vmctl client) not on PATH" >&2; exit 1; }
command -v qemu-img >/dev/null || { echo "qemu-img missing" >&2; exit 1; }
vm image list 2>/dev/null | grep -q 'library/dpu' || {
    echo "parent library/dpu:latest is not in the vm image store; build it first" >&2
    echo "  (claude-notes/nico-dev/spike-vmctl-dpu-tcg-main.md, steps 1-2)" >&2
    exit 1
}

echo "== vm image build -t $TAG $HERE"
# vm image build takes the recipe directory; packer variables pass through PKR_VAR_*.
if [[ -n "${HBN_IMAGE:-}" ]]; then export PKR_VAR_hbn_image="$HBN_IMAGE"; fi
log=$(mktemp)
# The build prints the resulting image digest as its last line ("sha256:...").
time vm image build -t "$TAG" "$HERE" | tee "$log"
digest=$(grep -oE '^sha256:[0-9a-f]{64}' "$log" | tail -1 | cut -d: -f2)
rm -f "$log"
[[ -n "$digest" ]] || { echo "could not read the image digest from the build output; run: vm image list" >&2; exit 1; }
disk="$HOME/.config/vm/images/$digest/disk.qcow2"
[[ -f "$disk" ]] || { echo "store disk not found: $disk" >&2; exit 1; }

echo "== flatten $disk -> $OUT"
sudo_if_needed=""
if ! { [[ -w "$OUT" ]] || { [[ ! -e "$OUT" ]] && [[ -w "$(dirname "$OUT")" ]]; }; }; then sudo_if_needed=sudo; fi
$sudo_if_needed qemu-img convert -O qcow2 "$disk" "$OUT"
if [[ -n "$sudo_if_needed" ]] && id libvirt-qemu >/dev/null 2>&1; then
    sudo chown libvirt-qemu:kvm "$OUT"
fi
qemu-img info "$OUT" | grep -E 'virtual size|backing' || true
echo "done: $OUT"
