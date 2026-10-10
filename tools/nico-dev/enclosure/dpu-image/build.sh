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
time vm image build -t "$TAG" "$HERE"

# Locate the built image's disk in the store and flatten it.
digest=$(vm image list 2>/dev/null | awk -v t="$TAG" '$0 ~ t {print $NF; exit}')
disk=""
for candidate in "$HOME/.config/vm/images/${digest#sha256:}/disk.qcow2" "$HOME/.config/vm/images/$digest/disk.qcow2"; do
    [[ -f "$candidate" ]] && { disk="$candidate"; break; }
done
if [[ -z "$disk" ]]; then
    echo "could not locate the store disk for $TAG (digest '$digest'); run: vm image list" >&2
    echo "then: qemu-img convert -O qcow2 ~/.config/vm/images/<sha>/disk.qcow2 $OUT" >&2
    exit 1
fi
echo "== flatten $disk -> $OUT"
qemu-img convert -O qcow2 "$disk" "$OUT"
qemu-img info "$OUT" | grep -E 'virtual size|backing' || true
echo "done: $OUT"
