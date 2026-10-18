#!/usr/bin/env bash
# Build the Enclosure DPU image in one command: NICo's arm64 agent and
# DHCP-server images from a NICo checkout, the x86 bmc-mock from the same
# commit (for the enclosure's two BMCs), then the golden QCOW2 from Ubuntu's
# arm64 cloud image with real HBN and those images baked in. Prints the plan,
# including every asset it will reuse from an earlier run, and proceeds only
# after confirmation (--no-prompt to skip).
#
#   bash build-dpu-image.sh --nico-repo DIR [--nico-ref REF] [--ubuntu-image URL|PATH]
#                           [--hbn-image REF] [--out PATH] [--rebuild-nico-images] [--no-prompt]
#
#   --nico-repo DIR       NICo checkout to build the agent and DHCP server from (required).
#                         Cloned from upstream if DIR does not exist; a build checkout the
#                         tool moves to --nico-ref on every run (it refuses a dirty one).
#   --nico-ref REF        what to build: a branch (default upstream/main, i.e. main's head),
#                         a release tag such as v2.4.0, a commit, or `latest-release` for the
#                         highest v* tag upstream carries
#   --ubuntu-image X      Ubuntu arm64 cloud image, URL or local file
#                         (default noble-server-cloudimg-arm64.img, current)
#   --hbn-version TAG     DOCA HBN release to pull from nvcr.io/nvidia/doca/doca_hbn, as its
#                         tag "<hbn>-doca<doca>", e.g. 3.4.0-doca3.4.0 or 2.4.1-doca2.9.3
#                         (default: 3.4.0-doca3.4.0 pinned by digest, what NICo's DPF service pins)
#   --hbn-image REF       any HBN container reference instead (repo:tag or repo@sha256:...)
#   --out PATH            golden image (default /var/lib/libvirt/images/enclosure-dpu.qcow2)
#   --rebuild-nico-images build the NICo images and bmc-mock even if ones from the same commit exist
#   --rebuild-all         ignore every cached asset: NICo images, cloud image, MFT package
#   --no-prompt           do not ask for confirmation
#
# Runs build-dpu-nico-images.sh and build-dpu.sh (same directory); see the
# README "Build" for host prerequisites.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NICO_REPO=""; NICO_REF="upstream/main"
UBUNTU_IMAGE="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
HBN_REPO=nvcr.io/nvidia/doca/doca_hbn
HBN_DEFAULT_TAG=3.4.0-doca3.4.0            # NICo's DOCA_HBN_SERVICE_IMAGE_TAG (crates/api-core/src/dpf_services.rs)
HBN_DEFAULT_DIGEST=sha256:9c2008c9ad1ad776fd1263072a78bd3dedaee2b9f315b99071d3e1495f7b1b4f
HBN_VERSION="$HBN_DEFAULT_TAG"; HBN_IMAGE="${HBN_IMAGE:-}"
OUT=/var/lib/libvirt/images/enclosure-dpu.qcow2
WORK="$HOME/.cache/enclosure-dpu-build"
REBUILD_NICO=0; REBUILD_ALL=0; NO_PROMPT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nico-repo) NICO_REPO="$2"; shift 2 ;;
        --nico-ref) NICO_REF="$2"; shift 2 ;;
        --ubuntu-image) UBUNTU_IMAGE="$2"; shift 2 ;;
        --hbn-version) HBN_VERSION="$2"; shift 2 ;;
        --hbn-image) HBN_IMAGE="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --rebuild-nico-images) REBUILD_NICO=1; shift ;;
        --rebuild-all) REBUILD_ALL=1; REBUILD_NICO=1; shift ;;
        --no-prompt) NO_PROMPT=1; shift ;;
        -h|--help) sed -n 2,22p "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; sed -n 8,10p "$0" >&2; exit 2 ;;
    esac
done
[[ -n "$NICO_REPO" ]] || { echo "--nico-repo DIR is required" >&2; exit 2; }
NICO_REPO="${NICO_REPO/#\~/$HOME}"
# The HBN reference: an explicit --hbn-image wins; the default release is
# pulled by digest so the image is reproducible; another release by its tag.
if [[ -n "$HBN_IMAGE" ]]; then hbn_label="$HBN_IMAGE (given as --hbn-image)"
elif [[ "$HBN_VERSION" == "$HBN_DEFAULT_TAG" ]]; then HBN_IMAGE="$HBN_REPO@$HBN_DEFAULT_DIGEST"; hbn_label="DOCA HBN $HBN_VERSION (${HBN_DEFAULT_DIGEST:0:19}…, NICo's pinned release)"
else HBN_IMAGE="$HBN_REPO:$HBN_VERSION"; hbn_label="DOCA HBN $HBN_VERSION ($HBN_IMAGE, by tag: not pinned by digest)"; fi

# ── the NICo ref to build ───────────────────────────────────────────────────
repo_head=""; target_sha=""; target_desc=""
if [[ -d "$NICO_REPO/.git" ]]; then
    if [[ -n "$(git -C "$NICO_REPO" status --porcelain --untracked-files=no)" ]]; then
        echo "$NICO_REPO has local changes; this tool checks out --nico-ref there, so use a clean build checkout" >&2; exit 1
    fi
    remote=$(git -C "$NICO_REPO" remote | grep -qx upstream && echo upstream || echo origin)
    git -C "$NICO_REPO" fetch -q --tags "$remote"
    ref="$NICO_REF"
    [[ "$ref" == upstream/* ]] && ref="$remote/${ref#upstream/}"
    if [[ "$ref" == latest-release ]]; then
        ref=$(git -C "$NICO_REPO" tag -l 'v[0-9]*' --sort=-v:refname | head -1)
        [[ -n "$ref" ]] || { echo "no v<number> release tag in $NICO_REPO ($remote)" >&2; exit 1; }
    fi
    target_sha=$(git -C "$NICO_REPO" rev-parse --verify -q "$ref^{commit}") || { echo "--nico-ref $NICO_REF: not found in $NICO_REPO ($remote)" >&2; exit 1; }
    repo_head=$(git -C "$NICO_REPO" rev-parse HEAD)
    tagnote=$(git -C "$NICO_REPO" describe --tags --exact-match "$target_sha" 2>/dev/null || true)
    target_desc="$NICO_REF → ${target_sha:0:9} ($(git -C "$NICO_REPO" log -1 --format=%cd --date=short "$target_sha"))${tagnote:+, release tag $tagnote}"
    [[ "$repo_head" == "$target_sha" ]] && target_desc+=", already checked out" || target_desc+=", will be checked out (detached)"
fi

# ── what exists already ─────────────────────────────────────────────────────
TARBALL="$HERE/files/images/nico-arm64-images.tgz"
cloud_name=$(basename "$UBUNTU_IMAGE")
if (( REBUILD_ALL )); then
    rm -f "$TARBALL" "$TARBALL.provenance" "$HERE"/files/mft/mft_*_arm64.deb "$WORK/bmc-mock" "$WORK/bmc-mock.provenance"
    [[ -f "$UBUNTU_IMAGE" ]] || rm -f "$WORK/$cloud_name"
    echo "--rebuild-all: cached NICo tarball, MFT package and downloaded cloud image removed"
fi
repo_state="will be cloned from upstream and checked out at $NICO_REF"
[[ -n "$target_sha" ]] && repo_state="$target_desc"
nico_action="build"
nico_reason="no tarball yet"
if [[ -f "$TARBALL" ]]; then
    if (( REBUILD_NICO )); then
        nico_reason="tarball exists, --rebuild-nico-images given"
    elif [[ -f "$TARBALL.provenance" ]]; then
        tb_commit=$(sed -n 's/^commit=//p' "$TARBALL.provenance")
        tb_built=$(sed -n 's/^built=//p' "$TARBALL.provenance")
        if [[ -n "$target_sha" && "$tb_commit" == "$target_sha" ]]; then
            nico_action="reuse"; nico_reason="tarball built $tb_built from ${tb_commit:0:9}, the commit --nico-ref resolves to"
        elif [[ -z "$target_sha" ]]; then
            nico_reason="tarball exists (built $tb_built from ${tb_commit:0:9}) but the checkout will be cloned fresh"
        else
            nico_reason="tarball built from ${tb_commit:0:9}, --nico-ref resolves to ${target_sha:0:9}"
        fi
    else
        nico_reason="tarball exists without provenance (older build); rebuilding to record it"
    fi
fi
BMC_MOCK="$WORK/bmc-mock"
mock_action="build"; mock_reason="not built yet"
if [[ -f "$BMC_MOCK" && -f "$BMC_MOCK.provenance" ]]; then
    mock_commit=$(sed -n 's/^commit=//p' "$BMC_MOCK.provenance")
    if (( REBUILD_NICO )); then mock_reason="exists, rebuild requested"
    elif [[ -n "$target_sha" && "$mock_commit" == "$target_sha" ]]; then mock_action="reuse"; mock_reason="built from ${mock_commit:0:9}, the commit --nico-ref resolves to"
    else mock_reason="built from ${mock_commit:0:9}, --nico-ref resolves to ${target_sha:0:9}"; fi
fi
if [[ $mock_action == build ]]; then
    if command -v cargo >/dev/null && command -v protoc >/dev/null; then mock_how="native cargo build (cargo and protoc present)"
    else mock_how="in the repository's minimal build container (no host protoc)"; fi
    mock_reason="$mock_reason; $mock_how"
fi
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
  bmc-mock (x86)       $BMC_MOCK
                       $mock_action: $mock_reason
  Ubuntu cloud image   $UBUNTU_IMAGE
                       $cloud_state
  MFT (flint)          $mft_state
  DOCA HBN             $hbn_label
                       pulled inside the build VM (not cached between builds)
  output               $OUT
                       $out_state
  logs                 $WORK/build-<timestamp>/ (serial.log, provision.log)

Steps: $([[ $nico_action == build ]] && echo "build-dpu-nico-images.sh; " || echo "")$([[ $mock_action == build ]] && echo "cargo build -p bmc-mock; " || echo "")build-dpu.sh
EOF
if (( ! NO_PROMPT )); then
    read -r -p "Proceed? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

start=$(date +%s)
if [[ -n "$target_sha" && "$repo_head" != "$target_sha" ]]; then
    git -C "$NICO_REPO" checkout -q --detach "$target_sha"
    echo "checked out ${target_sha:0:9} in $NICO_REPO"
fi
if [[ $nico_action == build ]]; then
    bash "$HERE/build-dpu-nico-images.sh" --repo "$NICO_REPO" --ref "$NICO_REF" --out "$WORK/nico-arm64-images.tgz"
    nico_args=(--nico-images "$WORK/nico-arm64-images.tgz")
else
    nico_args=()
fi
if [[ $mock_action == build ]]; then
    echo; echo "== bmc-mock (x86) from $NICO_REPO"
    if command -v cargo >/dev/null && command -v protoc >/dev/null; then
        (cd "$NICO_REPO" && cargo build --release -p bmc-mock)
    else
        # The repository's minimal cargo container (Rust + protoc); CARGO_HOME is mounted as /cargo.
        export CARGO_HOME="${CARGO_HOME:-$HOME/.cache/nico-cargo-home}"; mkdir -p "$CARGO_HOME"
        (cd "$NICO_REPO" && docker image inspect carbide-build-minimal >/dev/null 2>&1 || cargo make build-cargo-docker-image-minimal)
        (cd "$NICO_REPO" && cargo make cargo-docker-minimal -- build --release -p bmc-mock)
    fi
    install -m 0755 "$NICO_REPO/target/release/bmc-mock" "$BMC_MOCK"
    printf 'commit=%s\nref=%s\nbuilt=%s\n' "$(git -C "$NICO_REPO" rev-parse HEAD)" "$NICO_REF" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$BMC_MOCK.provenance"
fi
HBN_VERSION="$HBN_VERSION" bash "$HERE/build-dpu.sh" "${nico_args[@]}" --out "$OUT" --work "$WORK" --cloud-image "$UBUNTU_IMAGE" --hbn-image "$HBN_IMAGE"
echo "bmc-mock: $BMC_MOCK  (enclosure file bmc.bmc_mock)"
echo "total: $(( ($(date +%s) - start) / 60 )) min"
