#!/usr/bin/env bash
# Enclosure cable, hypervisor side: one Linux bridge that reaches one leaf port
# inside the site VM. The bridge is plugged into the site VM as an extra virtio
# NIC with a fixed MAC; inside the VM, deploy-dev-fabric.py enslaves that NIC
# (found by MAC = fabric.enclosure.cable_mac in the site yaml) to br-<dc>-encl,
# which the topology links to leaf-mat:eth3. Enclosure VMs then attach their
# OOB NIC to the same bridge.
#
#   cable.sh create  <bridge> <site-vm> <cable-mac> [mtu]  # bridge + site VM NIC (persistent + live)
#   cable.sh attach  <bridge> <vm> <mac> [mtu]             # move a VM NIC (by MAC) onto the bridge
#   cable.sh status  <bridge>
#
# Example (jaslinux):
#   sudo bash cable.sh create patch-encl1 vm-feature1 52:54:00:ee:01:01
#   sudo bash cable.sh attach patch-encl1 dpu-test1 02:00:00:01:00:03
set -euo pipefail
CONNECT=${CONNECT:-qemu:///system}
# virsh attach-interface has no --mtu on older libvirt; set it on the tap
# instead (the guest side is set by whoever configures the NIC in the guest).
set_tap_mtu() {
  local vm=$1 mac=$2 mtu=$3 tap
  [[ -n "$mtu" ]] || return 0
  tap=$(virsh --connect "$CONNECT" domiflist "$vm" | awk -v m="$mac" 'tolower($5)==tolower(m){print $1}')
  [[ -n "$tap" && "$tap" != "-" ]] && ip link set "$tap" mtu "$mtu" || true
}
cmd=${1:-}; shift || true
case "$cmd" in
  create)
    br=$1; vm=$2; mac=$3; mtu=${4:-}
    if ! ip link show "$br" >/dev/null 2>&1; then
      ip link add "$br" type bridge
      [[ -n "$mtu" ]] && ip link set "$br" mtu "$mtu"
      ip link set "$br" up
      echo "bridge $br created${mtu:+ (mtu $mtu)}"
    fi
    if virsh --connect "$CONNECT" domiflist "$vm" | grep -qi "$mac"; then
      echo "$vm already has a NIC $mac"
    else
      virsh --connect "$CONNECT" attach-interface "$vm" --type bridge --source "$br" \
        --model virtio --mac "$mac" --config --live
      set_tap_mtu "$vm" "$mac" "$mtu"
      echo "$vm: NIC $mac attached to $br${mtu:+ (tap mtu $mtu)}"
    fi
    ;;
  attach)
    br=$1; vm=$2; mac=$3; mtu=${4:-}
    cur=$(virsh --connect "$CONNECT" domiflist "$vm" | awk -v m="$mac" 'tolower($5)==tolower(m){print $2":"$3}')
    if [[ "$cur" == "bridge:$br" ]]; then echo "$vm: $mac already on $br"; exit 0; fi
    if [[ -n "$cur" ]]; then
      # A running VM may refuse hot-detach (TCG arm64 guests sometimes do); then
      # shut it down, rerun, and start it again.
      virsh --connect "$CONNECT" detach-interface "$vm" "${cur%%:*}" --mac "$mac" --config --live
    fi
    virsh --connect "$CONNECT" attach-interface "$vm" --type bridge --source "$br" \
      --model virtio --mac "$mac" --config --live
    set_tap_mtu "$vm" "$mac" "$mtu"
    echo "$vm: NIC $mac now on $br${mtu:+ (tap mtu $mtu)}"
    ;;
  status)
    br=$1
    ip -br link show master "$br" 2>/dev/null || true
    for d in $(virsh --connect "$CONNECT" list --name); do
      virsh --connect "$CONNECT" domiflist "$d" | awk -v d="$d" -v b="$br" '$3==b{print d": "$5" ("$1")"}'
    done
    ;;
  *) sed -n 2,16p "$0"; exit 1 ;;
esac
