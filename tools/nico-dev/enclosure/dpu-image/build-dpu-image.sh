#!/usr/bin/env bash
# Build the Enclosure DPU image in one command: NICo's arm64 agent and
# DHCP-server images from a NICo checkout, then the golden QCOW2 from Ubuntu's
# arm64 cloud image with real HBN and those images baked in. Prints the plan,
# including every asset it will reuse from an earlier run, and proceeds only
# after confirmation (--no-prompt to skip).
#
#   bash build-dpu-image.sh --nico-repo DIR [--nico-ref REF] [--ubuntu-image URL|PATH]
#                           [--hbn-image REF] [--out PATH] [--rebuild-nico-images] [--no-prompt]
#
#   --nico-repo DIR       NICo checkout to build the agent and DHCP server from (required).
#                         Cloned from upstream at --nico-ref if DIR does not exist.
#   --nico-ref REF        ref to check out when cloning (default upstream/main)
#   --ubuntu-image X      Ubuntu arm64 cloud image, URL or local file
#                         (default noble-server-cloudimg-arm64.img, current)
#   --hbn-image REF       DOCA HBN container (default 3.4.0-doca3.4.0, pinned by digest)
#   --out PATH            golden image (default /var/lib/libvirt/images/enclosure-dpu.qcow2)
#   --rebuild-nico-images build the NICo images even if a tarball from the same commit exists
#   --no-prompt           do not ask for confirmation
#
# Runs build-dpu-nico-images.sh and build-dpu.sh (same directory); see the
# README "Build" for host prerequisites.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NICO_REPO=""; NICO_REF="upstream/main"
UBUNTU_IMAGE="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
HBN_IMAGE="${HBN_IMAGE:-nvcr.io/nvidia/doca/doca_hbn@sha256:9c2008c9ad1ad776fd1263072a78bd3dedaee2b9f315b99071d3e1495f7b1b4f}"
OUT=/var/lib/libvirt/images/enclosure-dpu.qcow2
WORK="$HOME/.cache/enclosure-dpu-build"
REBUILD_NICO=0; NO_PROMPT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nico-repo) NICO_REPO="$2"; shift 2 ;;
        --nico-ref) NICO_REF="$2"; shift 2 ;;
        --ubuntu-image) UBUNTU_IMAGE="$2"; shift 2 ;;
        --hbn-image) HBN_IMAGE="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --rebuild-nico-images) REBUILD_NICO=1; shift ;;
        --no-prompt) NO_PROMPT=1; shift ;;
        -h|--help) sed -n 2,22p "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; sed -n 8,10p "$0" >&2; exit 2 ;;
    esac
done
[[ -n "$NICO_REPO" ]] || { echo "--nico-repo DIR is required" >&2; exit 2; }
NICO_REPO="${NICO_REPO/#\~/$HOME}"

# ── what exists already ─────────────────────────────────────────────────────
TARBALL="$HERE/files/images/nico-arm64-images.tgz"
repo_state="will be cloned from upstream at $NICO_REF"
repo_head=""
if [[ -d "$NICO_REPO/.git" ]]; then
    repo_head=$(git -C "$NICO_REPO" rev-parse --short HEAD)
    repo_state="exists, HEAD $repo_head ($(git -C "$NICO_REPO" log -1 --format=%cd --date=short)), reused as is"
fi
nico_action="build"
nico_reason="no tarball yet"
if [[ -f "$TARBALL" ]]; then
    if (( REBUILD_NICO )); then
        nico_reason="tarball exists, --rebuild-nico-images given"
    elif [[ -f "$TARBALL.provenance" ]]; then
        tb_commit=$(sed -n 's/^commit=//p' "$TARBALL.provenance")
        tb_built=$(sed -n 's/^built=//p' "$TARBALL.provenance")
        if [[ -n "$repo_head" && "$tb_commit" == "$(git -C "$NICO_REPO" rev-parse HEAD)" ]]; then
            nico_action="reuse"; nico_reason="tarball built $tb_built from this checkout's HEAD ${tb_commit:0:9}"
        elif [[ -z "$repo_head" ]]; then
            nico_reason="tarball exists (built $tb_built from ${tb_commit:0:9}) but the checkout will be cloned fresh"
        else
            nico_reason="tarball built from ${tb_commit:0:9}, checkout is at $repo_head"
        fi
    else
        nico_reason="tarball exists without provenance (older build); rebuilding to record it"
    fi
fi
cloud_name=$(basename "$UBUNTU_IMAGE")
if [[ -f "$UBUNTU_IMAGE" ]]; then cloud_state="local file, used as is"
elif [[ -f "$WORK/$cloud_name" ]]; then cloud_state="cached in $WORK ($(date -r "$WORK/$cloud_name" +%Y-%m-%d)), checksum re-verified against the publisher"
else cloud_state="will be downloaded to $WORK"; fi
shopt -s nullglob; mft=("$HERE"/files/mft/mft_*_arm64.deb); shopt -u nullglob
mft_state=$([[ ${#mft[@]} -gt 0 ]] && echo "reused: $(basename "${mft[0]}")" || echo "will be downloaded")
cross_state=$(docker image inspect build-artifacts-container-cross-aarch64:latest >/dev/null 2>&1 && echo "reused (docker image present)" || echo "will be built")
out_state=$([[ -e "$OUT" ]] && echo "EXISTS, will be overwritten ($(date -r "$OUT" +%Y-%m-%d))" || echo "new")

cat <<EOF

Enclosure DPU image build — plan
  NICo checkout        $NICO_REPO
                       $repo_state
  NICo images tarball  $TARBALL
                       $nico_action: $nico_reason
  cross build image    $cross_state
  Ubuntu cloud image   $UBUNTU_IMAGE
                       $cloud_state
  MFT (flint)          $mft_state
  DOCA HBN image       $HBN_IMAGE
                       pulled inside the build VM (not cached between builds)
  output               $OUT
                       $out_state
  logs                 $WORK/build-<timestamp>/ (serial.log, provision.log)

Steps: $([[ $nico_action == build ]] && echo "1) build-dpu-nico-images.sh  2) build-dpu.sh" || echo "build-dpu.sh only (NICo images reused)")
EOF
if (( ! NO_PROMPT )); then
    read -r -p "Proceed? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

start=$(date +%s)
if [[ $nico_action == build ]]; then
    bash "$HERE/build-dpu-nico-images.sh" --repo "$NICO_REPO" --ref "$NICO_REF" --out "$WORK/nico-arm64-images.tgz"
    nico_args=(--nico-images "$WORK/nico-arm64-images.tgz")
else
    nico_args=()
fi
bash "$HERE/build-dpu.sh" "${nico_args[@]}" --out "$OUT" --work "$WORK" --cloud-image "$UBUNTU_IMAGE" --hbn-image "$HBN_IMAGE"
echo "total: $(( ($(date +%s) - start) / 60 )) min"
