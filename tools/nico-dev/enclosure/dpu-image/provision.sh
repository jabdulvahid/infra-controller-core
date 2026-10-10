#!/bin/bash
# Enclosure DPU child layer: provisioning inside the arm64 build VM.
# Runs as root (sudo -E) with HBN_IMAGE in the environment and the
# files/ directory uploaded to /tmp/enclosure-files.
set -euo pipefail

: "${HBN_IMAGE:?HBN_IMAGE is required (nvcr.io/nvidia/doca/doca_hbn@sha256:...)}"
FILES=/tmp/enclosure-files
LIB=/usr/local/lib/enclosure
install -d -m 0755 "$LIB" /etc/enclosure

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl jq ethtool bridge-utils iproute2 kmod

# Kernel modules HBN's legacy firewall tooling and the overlay need
# (vmctl's real-HBN investigation: ip_tables, iptable_filter, iptable_mangle,
# the IPv6 equivalents, ebtables).
install -m 0644 "$FILES/enclosure-hbn-modules.conf" /etc/modules-load.d/enclosure-hbn.conf

# Ubuntu's bgpd/staticd/bfdd AppArmor profiles deny supervisor signals from
# the container runtime; remove them in this disposable guest (same
# accommodation vmctl's slice made; not a production policy).
for daemon in usr.lib.frr.bgpd usr.lib.frr.staticd usr.lib.frr.bfdd; do
    if [[ -e "/etc/apparmor.d/$daemon" ]]; then
        apparmor_parser -R "/etc/apparmor.d/$daemon" 2>/dev/null || true
        rm -f "/etc/apparmor.d/$daemon"
    fi
done

# DPU-OS host paths for HBN (what the agent writes into and the container
# mounts). The agent's default HBN root is /var/lib/hbn.
install -d -m 0755 \
    /var/lib/hbn/etc/network/interfaces.d \
    /var/lib/hbn/etc/frr \
    /var/lib/hbn/etc/supervisor/conf.d \
    /var/lib/hbn/etc/cumulus/acl/policy.d \
    /var/lib/hbn/var/support/forge-dhcp/conf \
    /var/lib/hbn/var/support/forge-dhcp/logs \
    /var/lib/hbn/var/support/forge-dhcp/bin \
    /var/log/doca/hbn \
    /opt/forge

# Pull the HBN container now so a DPU boot needs no registry.
podman pull "$HBN_IMAGE"
printf '%s\n' "$HBN_IMAGE" > "$LIB/hbn-image"

# HBN bootstrap startup YAML: enables NVUE's REST listener the way NICo's own
# DPF service definition does (system api listening-address 0.0.0.0) and
# declares the ports the agent will configure. Applied once by
# enclosure-hbn-start on the first container start.
install -m 0644 "$FILES/enclosure-hbn-bootstrap.yaml" "$LIB/hbn-bootstrap.yaml"

# Scripts and units.
install -m 0755 "$FILES/enclosure-hbn-start" "$LIB/hbn-start"
install -m 0755 "$FILES/enclosure-first-boot.sh" "$LIB/first-boot"
install -m 0644 "$FILES/enclosure-hbn.service" /etc/systemd/system/enclosure-hbn.service
install -m 0644 "$FILES/enclosure-first-boot.service" /etc/systemd/system/enclosure-first-boot.service
install -m 0644 "$FILES/dpu.env.example" /etc/enclosure/dpu.env.example

# Agent and DHCP server: vmctl's units re-pointed at real HBN (full copies,
# because a drop-in cannot remove their Requires=nico-hardware-shim.service).
install -m 0644 "$FILES/enclosure-nico-dpu-agent.service" /etc/systemd/system/enclosure-nico-dpu-agent.service
install -m 0644 "$FILES/enclosure-nico-dhcp-server.service" /etc/systemd/system/enclosure-nico-dhcp-server.service
systemctl disable vm-nico-dpu-agent.service vm-nico-dhcp-server.service 2>/dev/null || true

# The shim is replaced by real HBN; keep the binary (harmless), never start it.
# vmctl installed the unit under /etc/systemd/system, where mask wants to put
# its symlink, so remove the file first.
systemctl disable nico-hardware-shim.service 2>/dev/null || true
rm -f /etc/systemd/system/nico-hardware-shim.service
systemctl mask nico-hardware-shim.service

systemctl daemon-reload
# first-boot enables enclosure-hbn and the agent units once dpu.env exists.
systemctl enable enclosure-first-boot.service

# Sysctls HBN expects on DPU OS (from NICo's DPU cloud-init), minus hugepages.
cat > /etc/sysctl.d/98-enclosure-hbn.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF

apt-get clean
rm -rf /var/lib/apt/lists/* "$FILES"
echo "enclosure/dpu child layer provisioned (HBN $HBN_IMAGE)"
