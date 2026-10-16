#!/usr/bin/env bash
# Build NICo's arm64 forge-dpu-agent and forge-dhcp-server container images on
# an x86 Linux host and pack them into the tarball build.sh bakes into the
# Enclosure DPU image. Everything runs in containers from the NICo repository's
# own build definitions (the cross container CI uses; the two Dockerfiles only
# COPY onto a distroless base), so no arm64 emulation is needed.
#
#   bash build-dpu-nico-images.sh --repo DIR [--ref REF] [--out PATH]
#
#   --repo DIR   a NICo checkout to build from (cloned if DIR does not exist;
#                see --ref). Build from the commit the site's Core runs, or
#                upstream main at the time the site was brought up.
#   --ref REF    git ref to check out when cloning (default upstream/main)
#   --out PATH   tarball to write (default ./nico-arm64-images.tgz)
#
# Host prerequisites: git, docker (the user in the docker group), cargo-make
# (`cargo install cargo-make`, needs a Rust toolchain — the compile itself runs
# inside the cross container, the toolchain only runs cargo-make), curl, tar.
set -euo pipefail

REPO=""; REF="upstream/main"; OUT="$PWD/nico-arm64-images.tgz"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "usage: $0 --repo DIR [--ref REF] [--out PATH]" >&2; exit 2 ;;
    esac
done
[[ -n "$REPO" ]] || { echo "--repo DIR is required" >&2; exit 2; }
for c in git docker cargo curl tar; do command -v "$c" >/dev/null || { echo "$c missing" >&2; exit 1; }; done
cargo make --version >/dev/null 2>&1 || { echo "cargo-make missing: cargo install cargo-make" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker is not usable by this user" >&2; exit 1; }

# Tags dpu.env expects: ${CARBIDE_IMAGE_REGISTRY}/forge-<name>:${DPU_AGENT_PKG_VERSION},
# and bluefield/Makefile.toml derives DPU_AGENT_PKG_VERSION from $VERSION.
export CARBIDE_IMAGE_REGISTRY=localhost/nico VERSION=enclosure
AGENT=localhost/nico/forge-dpu-agent:enclosure
DHCP=localhost/nico/forge-dhcp-server:enclosure
MFT=mft-4.35.0-159-aarch64-deb          # flint for the agent image; what CI fetches (ci.yaml)

if [[ ! -d "$REPO/.git" ]]; then
    echo "== clone upstream NICo at $REF into $REPO"
    git clone -q https://github.com/dsx-ai-factory/infra-controller.git "$REPO"
    git -C "$REPO" remote rename origin upstream
    git -C "$REPO" fetch -q upstream
    git -C "$REPO" checkout -q "$REF"
fi
cd "$REPO"
echo "== building from $(git rev-parse --short HEAD) ($(git log -1 --format=%cd --date=short))"

if [[ ! -d "$MFT/DEBS" ]]; then
    echo "== MFT deb (flint) for the agent image"
    curl -fLO "https://www.mellanox.com/downloads/MFT/$MFT.tgz"
    tar -zxf "$MFT.tgz" && rm -f "$MFT.tgz"
fi
ls "$MFT"/DEBS/mft_*_arm64.deb >/dev/null

# CARGO_HOME is mounted into the cross container as /cargo; keep it a
# Linux-only directory so no host rustup proxies end up on the container's PATH.
export CARGO_HOME="${CARGO_HOME_LINUX:-$HOME/.cache/nico-cross-cargo-home}"
mkdir -p "$CARGO_HOME"

echo "== cross container + arm64 binaries"
cargo make build-cross-docker-image
cargo make --cwd bluefield build-dpu-agent-and-dhcp-server
for b in forge-dpu-agent forge-dhcp-server; do
    bash scripts/check-aarch64-pagesize.sh --allow-empty "target/aarch64-unknown-linux-gnu/release/$b"
done

echo "== runtime images"
cargo make --cwd bluefield docker-build-dpu-agent
cargo make --cwd bluefield docker-build-dhcp-server
docker image inspect "$AGENT" "$DHCP" --format '{{index .RepoTags 0}} {{.Architecture}} {{.Id}}'

echo "== $OUT"
docker save "$AGENT" "$DHCP" | gzip > "$OUT"
ls -l "$OUT"
echo "done: bake with  bash build.sh --nico-images $OUT --out /var/lib/libvirt/images/enclosure-dpu.qcow2"
