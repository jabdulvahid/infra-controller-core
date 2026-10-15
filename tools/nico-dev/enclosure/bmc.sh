#!/usr/bin/env bash
# Enclosure BMCs, hypervisor side: one bmc-mock process per BMC, each in its own
# network namespace with its own MAC on the enclosure cable bridge, taking its
# address from NICo's DHCP through the leaf relay like a real BMC. site-explorer
# then probes the address, matches the host BMC MAC to an expected machine, and
# creates the Host + DPU pair from the host mock's Redfish inventory.
#
# Identities come from bmc-mock's --instance-index (pool 02:00:00:<i>:00:00, first
# address :01, DPUs allocated before the host: DPU k = bmc, factory, oob; host =
# bmc, non-dpu). With --instance-index 1 and a one-DPU profile:
#   DPU BMC 02:00:00:01:00:01  factory 02  oob 03  serial MT020000010003
#   host BMC 02:00:00:01:00:04  host serial 020000010004
#
#   bmc.sh create  <encl> host|dpu <mac> [options]
#   bmc.sh status  <encl>
#   bmc.sh destroy <encl> [host|dpu]
# Options: --bridge NAME (patch-encl1)  --profile P (nvidia_dgx_h100)  --instance N (1)
#          --dpu-index K (0)  --bin PATH  --certs DIR  --libvirt-domain NAME (host power via libvirt)
#
# Example (jaslinux):
#   sudo bash bmc.sh create encl1 dpu  02:00:00:01:00:01
#   sudo bash bmc.sh create encl1 host 02:00:00:01:00:04
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
cmd=${1:-}; encl=${2:-}; shift 2 || true
role=""; mac=""; bridge=patch-encl1; profile=nvidia_dgx_h100; instance=1; dpu_index=0
bin="$repo/target/release/bmc-mock"; certs="$repo/crates/bmc-mock"; domain=""
if [[ "$cmd" == create ]]; then role=${1:?role}; mac=${2:?mac}; shift 2; fi
if [[ "$cmd" == destroy ]]; then role=${1:-}; shift || true; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge) bridge=$2; shift 2 ;; --profile) profile=$2; shift 2 ;;
    --instance) instance=$2; shift 2 ;; --dpu-index) dpu_index=$2; shift 2 ;;
    --bin) bin=$2; shift 2 ;; --certs) certs=$2; shift 2 ;;
    --libvirt-domain) domain=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
ns() { echo "${encl}-${1}bmc"; }                 # netns, e.g. encl1-hostbmc
veth() { echo "${encl}-${1:0:1}b0"; }            # bridge-side veth, e.g. encl1-hb0 (<= 15 chars)
unit() { echo "enclosure-${encl}-${1}bmc"; }
state=/var/lib/enclosure/$encl

create() {
  [[ -x "$bin" ]] || { echo "bmc-mock binary not found at $bin (build: cargo build --release -p bmc-mock)" >&2; exit 1; }
  [[ -f "$certs/tls.crt" && -f "$certs/tls.key" ]] || { echo "tls.crt/tls.key not in $certs" >&2; exit 1; }
  ip link show "$bridge" >/dev/null
  local n v; n=$(ns "$role"); v=$(veth "$role")
  mkdir -p "$state"
  if ! ip netns list | grep -qx "$n"; then
    ip netns add "$n"
    ip link add "$v" type veth peer name eth0 netns "$n"
    ip link set "$v" master "$bridge" up
    ip -n "$n" link set eth0 address "$mac"
    ip -n "$n" link set lo up
    ip -n "$n" link set eth0 up
    echo "netns $n: eth0 $mac on $bridge via $v"
  fi
  # Lease from NICo (relayed by the leaf). dhclient stays resident for renewals.
  if ! ip -n "$n" -4 addr show eth0 | grep -q 'inet '; then
    if command -v dhclient >/dev/null; then
      ip netns exec "$n" dhclient -4 -nw -pf "$state/$role-dhclient.pid" -lf "$state/$role-dhclient.leases" eth0
    else
      ip netns exec "$n" busybox udhcpc -b -i eth0 -p "$state/$role-udhcpc.pid" -S
    fi
    for _ in $(seq 60); do ip -n "$n" -4 addr show eth0 | grep -q 'inet ' && break; sleep 1; done
  fi
  local addr; addr=$(ip -n "$n" -4 -o addr show eth0 | awk '{print $4}' | head -1)
  echo "lease: ${addr:-none yet}"
  # bmc-mock under a transient systemd unit, inside the namespace, on 443.
  local args=(--machine-role "$role" --hardware-profile "$profile" --instance-index "$instance" --port 443 --cert-path "$certs")
  [[ "$role" == dpu ]] && args+=(--dpu-index "$dpu_index")
  if [[ -n "$domain" ]]; then args+=(--state-backend libvirt --libvirt-domain "$domain"); else args+=(--state-backend internal); fi
  if systemctl is-active --quiet "$(unit "$role")"; then
    echo "$(unit "$role") already running"
  else
    systemd-run --unit "$(unit "$role")" --collect -p Restart=on-failure -p RestartSec=5 \
      -E RUST_LOG=info -E REPO_ROOT="$repo" \
      ip netns exec "$n" "$bin" "${args[@]}"
    echo "started $(unit "$role"): bmc-mock ${args[*]}"
  fi
}

status() {
  for r in host dpu; do
    local n; n=$(ns "$r")
    ip netns list | grep -qx "$n" || continue
    local addr; addr=$(ip -n "$n" -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | head -1)
    local macaddr; macaddr=$(ip -n "$n" -o link show eth0 | awk '{print $17}')
    printf '%-5s ns=%s mac=%s addr=%s unit=%s' "$r" "$n" "$macaddr" "${addr:-none}" "$(systemctl is-active "$(unit "$r")" 2>/dev/null || true)"
    if [[ -n "$addr" ]]; then
      printf ' redfish=%s' "$(ip netns exec "$n" curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "https://${addr%/*}/redfish/v1" || true)"
    fi
    echo
  done
}

destroy() {
  local roles=(host dpu); [[ -n "$role" ]] && roles=("$role")
  for r in "${roles[@]}"; do
    systemctl stop "$(unit "$r")" 2>/dev/null || true
    local n; n=$(ns "$r")
    if ip netns list | grep -qx "$n"; then
      [[ -f "$state/$r-dhclient.pid" ]] && kill "$(cat "$state/$r-dhclient.pid")" 2>/dev/null || true
      [[ -f "$state/$r-udhcpc.pid" ]] && kill "$(cat "$state/$r-udhcpc.pid")" 2>/dev/null || true
      ip netns del "$n"
      echo "removed $n"
    fi
  done
}

case "$cmd" in
  create) create ;; status) status ;; destroy) destroy ;;
  *) sed -n 2,22p "$0"; exit 1 ;;
esac
