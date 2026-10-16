#!/usr/bin/env bash
# Build the Enclosure DPU image's PARENT, vmctl's library/dpu:latest, on an
# x86 Linux host. One-time per host (hours under TCG); the Enclosure child
# layer (build.sh) is rebuilt on top of it per NICo version.
#
#   bash build-base.sh [--vmctl DIR]
#
#   --vmctl DIR   a vmctl checkout on branch main (default ~/vmctl). Clone it
#                 first: git clone git@github.com:NVIDIA-dev/vmctl.git ~/vmctl
#
# Host prerequisites, once (vmctl README, "Release installation"): Go at the
# version in vmctl's go.mod, the `vm` client (release archive or
# `go install ./cmd/vm` from the checkout), then as root
# `vm host deps --install --build-tools` and `vm host setup --apply`, and as
# the user `vm setup && vm doctor`. The script checks and names what is
# missing; it does not install host packages itself.
set -euo pipefail

VMCTL="$HOME/vmctl"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vmctl) VMCTL="$2"; shift 2 ;;
        *) echo "usage: $0 [--vmctl DIR]" >&2; exit 2 ;;
    esac
done
[[ -d "$VMCTL/library/dpu/build" ]] || { echo "$VMCTL is not a vmctl checkout (library/dpu/build missing); git clone git@github.com:NVIDIA-dev/vmctl.git $VMCTL" >&2; exit 1; }
for c in vm go qemu-system-aarch64 aarch64-linux-gnu-gcc packer; do
    command -v "$c" >/dev/null || { echo "$c missing: see the host prerequisites in the header of $0" >&2; exit 1; }
done
[[ -f /usr/share/AAVMF/AAVMF_CODE.fd ]] || { echo "AAVMF firmware missing: sudo apt-get install -y qemu-system-arm qemu-efi-aarch64" >&2; exit 1; }
vm doctor >/dev/null || { echo "vm doctor reports problems; fix them first" >&2; exit 1; }

# The vm client hands paths to the user daemon, whose cwd is $HOME: absolute paths only.
if vm image list 2>/dev/null | grep -q 'library/ubuntu-24.04-arm64'; then
    echo "== library/ubuntu-24.04-arm64:latest present"
else
    echo "== arm64 root image (about 10 minutes)"
    time vm image build -t library/ubuntu-24.04-arm64:latest "$VMCTL/library/roots/ubuntu-24.04-arm64/build"
fi

if vm image list 2>/dev/null | grep -q 'library/dpu'; then
    echo "== library/dpu:latest present; nothing to do (vm image list)"
    exit 0
fi
# vm image build does not run the recipe's build-inputs.sh hook (only vmctl's
# library builder does); without it Packer fails on output/shims. The shim
# build resolves its Go module from the current directory.
echo "== DPU layer inputs (shims, mlx5 backend; Go + aarch64 cross-compiler on the host)"
( cd "$VMCTL" && bash library/dpu/build-inputs.sh && ls -l library/dpu/output/shims )
echo "== library/dpu:latest (hours under TCG: DKMS builds inside the arm64 guest)"
time vm image build -t library/dpu:latest "$VMCTL/library/dpu/build"
vm image list | grep 'library/dpu'
echo "done: next  bash build-nico-images.sh --repo ~/build/nico  and  bash build.sh --nico-images ..."
