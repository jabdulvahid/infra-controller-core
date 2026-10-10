#!/usr/bin/env bash
# Boot one Enclosure DPU VM from the flattened image for verification.
# Stands in for the Enclosure tool until it exists: writes the cloud-init
# seed (dpu.env, login key), an overlay disk over the golden image and a
# libvirt domain with the BlueField SMBIOS identity and three NICs.
#
#   bash boot-test.sh [--name dpu-test1] [--image /var/lib/libvirt/images/enclosure-dpu.qcow2]
#                     [--network default] [--serial MT020000010003] [--password P]
#   bash boot-test.sh --name dpu-test1 --destroy
#
# All three NICs sit on one libvirt network for this smoke test; the Enclosure
# tool will cable them separately. The agent and DHCP-server units will fail
# until arm64 images exist; HBN and the interface identity can be verified
# regardless. Credentials are generated when --password is omitted and kept
# in the domain's work directory, never printed.
set -euo pipefail

NAME=dpu-test1
IMAGE=/var/lib/libvirt/images/enclosure-dpu.qcow2
NET=default
SERIAL=MT020000010003
PASSWORD=""
DESTROY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --network) NET="$2"; shift 2 ;;
        --serial) SERIAL="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --destroy) DESTROY=1; shift ;;
        *) echo "usage: see header of $0" >&2; exit 2 ;;
    esac
done

WORK=/var/lib/libvirt/images/enclosure/$NAME
CONNECT=qemu:///system

if (( DESTROY )); then
    sudo virsh --connect "$CONNECT" destroy "$NAME" 2>/dev/null || true
    sudo virsh --connect "$CONNECT" undefine --nvram "$NAME" 2>/dev/null || true
    sudo rm -rf "$WORK"
    echo "destroyed $NAME"
    exit 0
fi

for c in virt-install qemu-img; do command -v "$c" >/dev/null || { echo "$c missing" >&2; exit 1; }; done
[[ -f "$IMAGE" ]] || { echo "golden image not found: $IMAGE (build.sh --out ...)" >&2; exit 1; }
if sudo virsh --connect "$CONNECT" dominfo "$NAME" >/dev/null 2>&1; then
    echo "domain $NAME exists; run with --destroy first" >&2; exit 1
fi

# MACs: the example's scheme; the serial is "MT" + OOB MAC without colons.
OOB_MAC=02:00:00:01:00:03
UPLINK_MAC=02:00:00:01:00:04
HOSTLINK_MAC=02:00:00:01:00:05
FACTORY_MAC=02:00:00:01:00:02

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
[[ -n "$PASSWORD" ]] || PASSWORD=$(openssl rand -hex 8)

# dpu.env: systemd's EnvironmentFile= keeps trailing "# ..." as part of the
# value, so no inline comments here.
cat > "$tmp/dpu.env" <<EOF
DPU_OOB_MAC=$OOB_MAC
DPU_UPLINK_MAC=$UPLINK_MAC
DPU_HOSTLINK_MAC=$HOSTLINK_MAC
DPU_FACTORY_MAC=$FACTORY_MAC
DPU_SERIAL=$SERIAL
NVUE_USERNAME=carbide
NVUE_PASSWORD=$PASSWORD
NICO_DPU_AGENT_IMAGE=localhost/nico/forge-dpu-agent:enclosure
NICO_DHCP_SERVER_IMAGE=localhost/nico/forge-dhcp-server:enclosure
EOF

keys=""
for k in "$HOME"/.ssh/id_*.pub; do
    [[ -f "$k" ]] && keys+="      - $(cat "$k")"$'\n'
done

cat > "$tmp/meta-data" <<EOF
instance-id: enclosure-$NAME
local-hostname: $NAME
EOF
# A fresh instance-id makes cloud-init treat the flattened image as a new
# instance and run write_files/users again.
{
    cat <<EOF
#cloud-config
users:
  - name: vm
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
EOF
    if [[ -n "$keys" ]]; then
        echo "    ssh_authorized_keys:"
        printf '%s' "$keys"
    fi
    cat <<EOF
chpasswd:
  expire: false
  users:
    - name: vm
      password: $PASSWORD
      type: text
ssh_pwauth: false
write_files:
  - path: /etc/enclosure/dpu.env
    permissions: "0600"
    encoding: b64
    content: $(base64 -w0 < "$tmp/dpu.env")
EOF
} > "$tmp/user-data"

if command -v cloud-localds >/dev/null; then
    cloud-localds "$tmp/seed.iso" "$tmp/user-data" "$tmp/meta-data"
elif command -v genisoimage >/dev/null; then
    genisoimage -quiet -output "$tmp/seed.iso" -volid cidata -joliet -rock "$tmp/user-data" "$tmp/meta-data"
elif command -v xorriso >/dev/null; then
    xorriso -as mkisofs -quiet -output "$tmp/seed.iso" -volid cidata -joliet -rock "$tmp/user-data" "$tmp/meta-data"
else
    echo "need cloud-localds (cloud-image-utils), genisoimage or xorriso" >&2; exit 1
fi

sudo install -d -m 0750 -o libvirt-qemu -g kvm "$WORK"
sudo install -m 0640 -o libvirt-qemu -g kvm "$tmp/seed.iso" "$WORK/seed.iso"
sudo install -m 0600 "$tmp/dpu.env" "$WORK/dpu.env"
# Overlay keeps the golden image pristine; one per DPU, as the tool will do.
sudo qemu-img create -q -f qcow2 -b "$IMAGE" -F qcow2 "$WORK/disk.qcow2"
sudo chown libvirt-qemu:kvm "$WORK/disk.qcow2"

# NIC order = dpu.env order: OOB, uplink (p0), host link (pf0hpf).
# --sysinfo makes virt-install set <smbios mode="sysinfo"/>, so DMI inside the
# guest shows the BlueField identity the agent's enumeration derives from.
virt-install --connect "$CONNECT" --name "$NAME" \
    --arch aarch64 --machine virt --virt-type qemu --cpu cortex-a72 \
    --vcpus 4 --memory 4096 --boot uefi --import \
    --sysinfo system.manufacturer=Nvidia,system.product="BlueField SoC",system.serial="$SERIAL" \
    --disk path="$WORK/disk.qcow2",bus=virtio \
    --disk path="$WORK/seed.iso",device=cdrom,readonly=on \
    --network network="$NET",model=virtio,mac="$OOB_MAC" \
    --network network="$NET",model=virtio,mac="$UPLINK_MAC" \
    --network network="$NET",model=virtio,mac="$HOSTLINK_MAC" \
    --graphics none --console pty,target.type=serial \
    --osinfo detect=on,require=off --noautoconsole

cat <<EOF

$NAME started. First boot writes the .link files and reboots itself; the
second boot starts HBN (pull is local; NVUE start takes a few minutes under TCG).

  sudo virsh console $NAME            # login: vm, password in $WORK/dpu.env (NVUE_PASSWORD)
  sudo virsh domifaddr $NAME          # OOB address once DHCP answers; then: ssh vm@<addr>

Inside, in order:
  systemctl status enclosure-first-boot enclosure-hbn --no-pager
  cat /sys/class/dmi/id/product_serial /sys/class/dmi/id/product_name
  ip -br link                          # expect oob_net0 p0_if pf0hpf_if pf0dpu1 pf0dpu1_if
  journalctl -u enclosure-hbn -b --no-pager
  sudo podman exec enclosure-hbn supervisorctl status
  sudo podman exec enclosure-hbn nv show interface
  curl -ksS -u carbide:\$(sudo awk -F= '/^NVUE_PASSWORD/{print \$2}' /etc/enclosure/dpu.env) https://127.0.0.1:8765/nvue_v1/system
EOF
