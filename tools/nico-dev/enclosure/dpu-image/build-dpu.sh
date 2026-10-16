#!/usr/bin/env bash
# Build the Enclosure DPU image from Ubuntu's arm64 cloud image with plain
# qemu and cloud-init: no vmctl, no packer. Boots the cloud image once under
# qemu-system-aarch64, provisions it over ssh (real HBN, NICo's arm64 agent and
# DHCP-server images, MFT for flint, the enclosure wiring), seals it, and
# flattens the disk into the golden QCOW2 that enclosure.py overlays per DPU.
#
#   bash build-dpu.sh --nico-images /path/nico-arm64-images.tgz [--out PATH] [options]
#
#   --nico-images PATH  docker archive with localhost/nico/forge-{dpu-agent,dhcp-server}:enclosure
#                       (build-dpu-nico-images.sh); copied into files/images/, baked by provision.sh.
#                       Omit only to reuse the tarball already in files/images/.
#   --out PATH          golden image to write (default /var/lib/libvirt/images/enclosure-dpu.qcow2)
#   --work DIR          cache and scratch (default ~/.cache/enclosure-dpu-build): cloud image, build disk, logs
#   --cloud-image URL   Ubuntu arm64 cloud image (default noble-server-cloudimg-arm64.img, current)
#   --disk-size SIZE    build disk size (default 16G)   --cpus N (4)   --memory MB (4096)
#   --accel kvm|hvf|tcg force the accelerator (default: kvm on arm64 Linux, hvf on macOS, else tcg)
#   HBN_IMAGE=...       override the HBN container (default pinned in provision.sh's caller below)
#
# Host prerequisites: qemu-system-aarch64 + qemu-img, UEFI firmware for the
# virt machine (Linux: qemu-efi-aarch64 → /usr/share/AAVMF; macOS: brew qemu's
# edk2-aarch64-code.fd), one of cloud-localds / genisoimage / xorriso, ssh,
# curl. On an x86 host the guest runs under TCG: expect 30–60 minutes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=/var/lib/libvirt/images/enclosure-dpu.qcow2
WORK="$HOME/.cache/enclosure-dpu-build"
CLOUD_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
DISK_SIZE=16G; CPUS=4; MEMORY=4096; ACCEL=""; NICO_IMAGES=""
# HBN pinned by digest: 3.4.0-doca3.4.0, what NICo's DPF service definitions
# pin and what the enclosure has run since Phase A.
HBN_IMAGE="${HBN_IMAGE:-nvcr.io/nvidia/doca/doca_hbn@sha256:9c2008c9ad1ad776fd1263072a78bd3dedaee2b9f315b99071d3e1495f7b1b4f}"
MFT=mft-4.35.0-159-aarch64-deb      # flint on the DPU OS; the same package CI fetches for the agent image

while [[ $# -gt 0 ]]; do
    case "$1" in
        --nico-images) NICO_IMAGES="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        --cloud-image) CLOUD_IMAGE_URL="$2"; shift 2 ;;
        --disk-size) DISK_SIZE="$2"; shift 2 ;;
        --cpus) CPUS="$2"; shift 2 ;;
        --memory) MEMORY="$2"; shift 2 ;;
        --accel) ACCEL="$2"; shift 2 ;;
        *) sed -n 2,24p "$0" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "build-dpu: $*" >&2; exit 1; }

# ── prerequisites ───────────────────────────────────────────────────────────
for c in qemu-system-aarch64 qemu-img ssh ssh-keygen curl tar; do command -v "$c" >/dev/null || die "$c missing"; done
MKISO=""
for c in cloud-localds genisoimage xorriso; do command -v "$c" >/dev/null && { MKISO=$c; break; }; done
[[ -n "$MKISO" ]] || die "need cloud-localds (cloud-image-utils), genisoimage or xorriso"

os=$(uname -s); arch=$(uname -m)
if [[ -z "$ACCEL" ]]; then
    if [[ "$os" == Linux && "$arch" == aarch64 && -w /dev/kvm ]]; then ACCEL=kvm
    elif [[ "$os" == Darwin && "$arch" == arm64 ]]; then ACCEL=hvf
    else ACCEL=tcg; fi
fi
case "$ACCEL" in
    tcg) ACCEL_ARGS=(-accel tcg,thread=multi -cpu cortex-a72) ;;
    kvm) ACCEL_ARGS=(-accel kvm -cpu host) ;;
    hvf) ACCEL_ARGS=(-accel hvf -cpu host) ;;
    *) die "--accel: kvm, hvf or tcg" ;;
esac
FW_CODE=""; FW_VARS=""
for pair in "/usr/share/AAVMF/AAVMF_CODE.fd:/usr/share/AAVMF/AAVMF_VARS.fd" \
            "/usr/share/qemu/edk2-aarch64-code.fd:/usr/share/qemu/edk2-arm-vars.fd" \
            "/opt/homebrew/share/qemu/edk2-aarch64-code.fd:/opt/homebrew/share/qemu/edk2-arm-vars.fd" \
            "/usr/local/share/qemu/edk2-aarch64-code.fd:/usr/local/share/qemu/edk2-arm-vars.fd"; do
    if [[ -f "${pair%%:*}" && -f "${pair##*:}" ]]; then FW_CODE=${pair%%:*}; FW_VARS=${pair##*:}; break; fi
done
[[ -n "$FW_CODE" ]] || die "UEFI firmware for aarch64 not found (Linux: apt install qemu-efi-aarch64; macOS: brew install qemu)"

# ── inputs ──────────────────────────────────────────────────────────────────
mkdir -p "$WORK" "$HERE/files/images" "$HERE/files/mft"
if [[ -n "$NICO_IMAGES" ]]; then
    [[ -f "$NICO_IMAGES" ]] || die "--nico-images: $NICO_IMAGES not found"
    rm -f "$HERE"/files/images/*.tgz "$HERE"/files/images/*.tar "$HERE"/files/images/*.tar.gz
    cp "$NICO_IMAGES" "$HERE/files/images/nico-arm64-images.tgz"
fi
shopt -s nullglob
nico_tarballs=("$HERE"/files/images/*.tgz "$HERE"/files/images/*.tar "$HERE"/files/images/*.tar.gz)
mft_debs=("$HERE"/files/mft/mft_*_arm64.deb)
shopt -u nullglob
(( ${#nico_tarballs[@]} )) || die "no NICo image tarball in files/images/; pass --nico-images (README \"Build\")"
if (( ${#mft_debs[@]} == 0 )); then
    say "MFT package (flint) for the DPU OS"
    curl -fL -o "$WORK/$MFT.tgz" "https://www.mellanox.com/downloads/MFT/$MFT.tgz"
    tar -xzf "$WORK/$MFT.tgz" -C "$WORK"
    cp "$WORK/$MFT"/DEBS/mft_*_arm64.deb "$HERE/files/mft/"
    rm -rf "$WORK/$MFT" "$WORK/$MFT.tgz"
fi

say "Ubuntu arm64 cloud image"
CLOUD_IMG="$WORK/$(basename "$CLOUD_IMAGE_URL")"
if [[ ! -f "$CLOUD_IMG" ]]; then
    curl -fL -o "$CLOUD_IMG.part" "$CLOUD_IMAGE_URL" && mv "$CLOUD_IMG.part" "$CLOUD_IMG"
fi
# Verify against the publisher's SHA256SUMS when the image came from the default location.
if [[ "$CLOUD_IMAGE_URL" == https://cloud-images.ubuntu.com/* ]]; then
    sums="$(dirname "$CLOUD_IMAGE_URL")/SHA256SUMS"
    if curl -fsL -o "$WORK/SHA256SUMS" "$sums"; then
        expected=$(awk -v f="*$(basename "$CLOUD_IMG")" '$2==f{print $1}' "$WORK/SHA256SUMS")
        actual=$(sha256sum "$CLOUD_IMG" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$CLOUD_IMG" | cut -d' ' -f1)
        if [[ -n "$expected" && "$expected" != "$actual" ]]; then
            echo "cloud image checksum mismatch (cached copy is stale?); re-downloading" >&2
            curl -fL -o "$CLOUD_IMG.part" "$CLOUD_IMAGE_URL" && mv "$CLOUD_IMG.part" "$CLOUD_IMG"
            actual=$(sha256sum "$CLOUD_IMG" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$CLOUD_IMG" | cut -d' ' -f1)
            [[ "$expected" == "$actual" ]] || die "cloud image checksum still wrong"
        fi
        echo "  $(basename "$CLOUD_IMG") sha256 ok"
    fi
fi

# ── build VM ────────────────────────────────────────────────────────────────
BUILD="$WORK/build-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$BUILD"
say "build VM in $BUILD (accel $ACCEL)"
qemu-img create -q -f qcow2 -b "$CLOUD_IMG" -F qcow2 "$BUILD/disk.qcow2" "$DISK_SIZE"
cp "$FW_VARS" "$BUILD/vars.fd"
ssh-keygen -q -t ed25519 -N '' -C enclosure-dpu-build -f "$BUILD/id_build"
cat > "$BUILD/user-data" <<EOF
#cloud-config
users:
  - name: vm
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$BUILD/id_build.pub")
ssh_pwauth: false
package_update: false
EOF
printf 'instance-id: enclosure-dpu-build\nlocal-hostname: dpu-build\n' > "$BUILD/meta-data"
case "$MKISO" in
    cloud-localds) cloud-localds "$BUILD/seed.iso" "$BUILD/user-data" "$BUILD/meta-data" ;;
    genisoimage) genisoimage -quiet -output "$BUILD/seed.iso" -volid cidata -joliet -rock "$BUILD/user-data" "$BUILD/meta-data" ;;
    xorriso) xorriso -as mkisofs -quiet -output "$BUILD/seed.iso" -volid cidata -joliet -rock "$BUILD/user-data" "$BUILD/meta-data" ;;
esac

PORT=$(( 20000 + RANDOM % 20000 ))
qemu-system-aarch64 -machine virt "${ACCEL_ARGS[@]}" -smp "$CPUS" -m "$MEMORY" \
    -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
    -drive if=pflash,format=raw,file="$BUILD/vars.fd" \
    -drive file="$BUILD/disk.qcow2",if=virtio,format=qcow2 \
    -drive file="$BUILD/seed.iso",if=virtio,format=raw,media=cdrom,readonly=on \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22 -device virtio-net-pci,netdev=n0 \
    -display none -serial "file:$BUILD/serial.log" -monitor none \
    -pidfile "$BUILD/qemu.pid" -daemonize
trap 'if [[ -f "$BUILD/qemu.pid" ]] && kill -0 "$(cat "$BUILD/qemu.pid")" 2>/dev/null; then kill "$(cat "$BUILD/qemu.pid")"; fi' EXIT

SSH=(ssh -i "$BUILD/id_build" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 vm@127.0.0.1)
say "waiting for ssh (serial: $BUILD/serial.log)"
for i in $(seq 240); do
    "${SSH[@]}" true 2>/dev/null && break
    sleep 5
    (( i % 12 == 0 )) && echo "  still booting ($((i*5))s)"
    (( i == 240 )) && die "guest did not come up within 20 minutes; see $BUILD/serial.log"
done
echo "  up after about $((i*5))s"

say "upload files/ and provision.sh"
tar -C "$HERE/files" -cf - . | "${SSH[@]}" 'mkdir -p /tmp/enclosure-files && tar -C /tmp/enclosure-files -xf -'
"${SSH[@]}" 'cat > /tmp/provision.sh' < "$HERE/provision.sh"

say "provision (HBN $HBN_IMAGE)"
"${SSH[@]}" "sudo env HBN_IMAGE='$HBN_IMAGE' bash /tmp/provision.sh" 2>&1 | tee "$BUILD/provision.log"

say "seal: cloud-init clean, build key and machine-id removed, shutdown"
# A fresh instance-id per enclosure then re-runs cloud-init (users, write_files);
# an empty machine-id makes each DPU VM mint its own, so networkd's DHCP
# identity and anything keyed on the machine id differ between enclosures.
"${SSH[@]}" 'sudo cloud-init clean --logs; sudo rm -f /home/vm/.ssh/authorized_keys; sudo truncate -s0 /etc/machine-id; sudo rm -f /var/lib/dbus/machine-id; sudo sync; sudo systemctl poweroff' || true
# qemu removes its pidfile when it exits.
for i in $(seq 60); do [[ -f "$BUILD/qemu.pid" ]] || break; sleep 2; done
[[ -f "$BUILD/qemu.pid" ]] && kill -0 "$(cat "$BUILD/qemu.pid")" 2>/dev/null && die "guest did not power off"
trap - EXIT

say "flatten → $OUT"
sudo_if_needed=""
if ! { [[ -w "$OUT" ]] || { [[ ! -e "$OUT" ]] && [[ -w "$(dirname "$OUT")" ]]; }; }; then sudo_if_needed=sudo; fi
$sudo_if_needed qemu-img convert -O qcow2 "$BUILD/disk.qcow2" "$OUT"
if [[ -n "$sudo_if_needed" ]] && id libvirt-qemu >/dev/null 2>&1; then sudo chown libvirt-qemu:kvm "$OUT"; fi
qemu-img info "$OUT" | grep -E '^(virtual size|disk size)'
grep -A3 '^built=' "$BUILD/provision.log" | sed 's/^/  /' || true
echo "done: $OUT  (build dir kept: $BUILD)"
