#!/bin/bash
# Enclosure DPU image: provisioning inside the arm64 build VM, which
# build-dpu.sh boots from Ubuntu's noble arm64 cloud image. Runs as root with
# HBN_IMAGE in the environment and the files/ directory uploaded to
# /tmp/enclosure-files (files/images: NICo's arm64 image tarball; files/mft:
# the MFT package for flint).
set -euo pipefail

: "${HBN_IMAGE:?HBN_IMAGE is required (nvcr.io/nvidia/doca/doca_hbn@sha256:...)}"
FILES=/tmp/enclosure-files
LIB=/usr/local/lib/enclosure
install -d -m 0755 "$LIB" /etc/enclosure

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl jq ethtool bridge-utils iproute2 kmod \
    podman uidmap slirp4netns fuse-overlayfs netavark aardvark-dns busybox-static

# podman: every enclosure container runs with --network host, so no podman
# network is ever used. Ubuntu's podman package still drops a CNI bridge config
# while the CNI plugins are not installed, and every podman command then warns
# about it; pin the netavark backend (installed above) and drop that file.
install -d -m 0755 /etc/containers
cat > /etc/containers/containers.conf <<'EOF_CONF'
[network]
network_backend = "netavark"
EOF_CONF
rm -f /etc/cni/net.d/87-podman-bridge.conflist

# MFT for `flint`, which the agent container mounts from the DPU OS
# (/usr/local/bin/flint in enclosure-nico-dpu-agent.service). Only the
# userspace package: no device, so no kernel-mft-dkms.
shopt -s nullglob
mft_debs=("$FILES"/mft/mft_*_arm64.deb)
shopt -u nullglob
(( ${#mft_debs[@]} )) || { echo "no MFT package under files/mft/ (build-dpu.sh fetches it)" >&2; exit 1; }
apt-get install -y --no-install-recommends "${mft_debs[@]}"
flint_bin=$(command -v flint || ls /usr/mst/bin/flint /opt/mellanox/mft/bin/flint 2>/dev/null | head -1 || true)
[[ -n "$flint_bin" ]] || { echo "flint not found after installing MFT" >&2; exit 1; }
ln -sfn "$flint_bin" /usr/local/bin/flint

# The enclosure owns the network: first-boot writes the .link/.network files
# for the three NICs. Keep cloud-init from rendering netplan for whatever NIC
# the build VM had, and drop the one it rendered for this build.
install -d -m 0755 /etc/cloud/cloud.cfg.d
printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-enclosure-network.cfg
rm -f /etc/netplan/50-cloud-init.yaml
systemctl enable systemd-networkd.service

# Kernel modules HBN's legacy firewall tooling and the overlay need
# (vmctl's real-HBN investigation: ip_tables, iptable_filter, iptable_mangle,
# the IPv6 equivalents, ebtables).
install -m 0644 "$FILES/enclosure-hbn-modules.conf" /etc/modules-load.d/enclosure-hbn.conf

# Ubuntu's bgpd/staticd/bfdd/rsyslogd AppArmor profiles deny supervisor
# signals from the container runtime (rsyslogd: PermissionError on shutdown,
# seen on the first child boot); remove them in this disposable guest (same
# accommodation vmctl's slice made; not a production policy).
for daemon in usr.lib.frr.bgpd usr.lib.frr.staticd usr.lib.frr.bfdd usr.sbin.rsyslogd; do
    if [[ -e "/etc/apparmor.d/$daemon" ]]; then
        apparmor_parser -R "/etc/apparmor.d/$daemon" 2>/dev/null || true
        rm -f "/etc/apparmor.d/$daemon"
    fi
done

# Pull the HBN container now so a DPU boot needs no registry, then derive
# the Enclosure's HBN image from it (files/hbn-patch: NVUE accepts the SF
# port names on virtio/veth netdevs). Everything downstream uses the derived
# reference.
podman pull "$HBN_IMAGE"
HBN_LOCAL=localhost/enclosure/doca_hbn:enclosure
podman build --build-arg HBN_IMAGE="$HBN_IMAGE" -t "$HBN_LOCAL" "$FILES/hbn-patch"
printf '%s\n' "$HBN_LOCAL" > "$LIB/hbn-image"

# DPU-OS host paths for HBN (what the agent writes into and the container
# mounts), seeded from the image so a bind mount never hides the container's
# defaults. The image's /hbn_files/<path> holds the host-side defaults HBN's
# own deployment copies out (nl2docad.conf lives only there), layered over
# the container's /<path> (ifupdown2.conf, supervisor programs, the log tree
# supervisord writes into). The agent's default HBN root is /var/lib/hbn.
# Seed before creating any subdirectory: hbn-start's runtime fallback only
# seeds an empty directory.
seed_tree() { # $1 = path inside the image, $2 = host path
    install -d -m 0755 "$2"
    for src in "$1" "/hbn_files$1"; do
        if podman run --rm --entrypoint sh "$HBN_LOCAL" -c "test -d '$src'"; then
            podman run --rm --entrypoint tar "$HBN_LOCAL" -C "$src" -cf - . | tar -C "$2" -xf -
        fi
    done
}
for p in etc/network etc/frr etc/supervisor/conf.d etc/cumulus var/support; do
    seed_tree "/$p" "/var/lib/hbn/$p"
done
seed_tree /var/log/hbn /var/log/doca/hbn
install -d -m 0755 \
    /var/lib/hbn/etc/network/interfaces.d \
    /var/lib/hbn/etc/cumulus/acl/policy.d \
    /var/lib/hbn/var/support/forge-dhcp/conf \
    /var/lib/hbn/var/support/forge-dhcp/logs \
    /var/lib/hbn/var/support/forge-dhcp/bin \
    /opt/forge \
    /data

# NICo's arm64 agent and DHCP-server images, baked so a DPU VM boots with them
# in its local store (no registry is reachable from a DPU's OOB network on a
# dev site). build-dpu.sh copies the tarball into files/images/; a DPU image is
# therefore a per-NICo-version artefact, rebuilt (child layer only) to move
# the agent, the same way a DPF site moves its DPU service on a release.
shopt -s nullglob
nico_tarballs=("$FILES"/images/*.tgz "$FILES"/images/*.tar "$FILES"/images/*.tar.gz)
shopt -u nullglob
if (( ${#nico_tarballs[@]} == 0 )); then
    echo "no NICo image tarball under files/images/ (build-dpu.sh --nico-images); the agent and DHCP units will fail at boot" >&2
    exit 1
fi
for t in "${nico_tarballs[@]}"; do
    echo "podman load $t"
    podman load -i "$t"
done
podman image exists localhost/nico/forge-dpu-agent:enclosure
podman image exists localhost/nico/forge-dhcp-server:enclosure

# Provenance, for `enclosure status` and for anyone asking what a DPU runs.
{
    echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "hbn_version=${HBN_VERSION:-unknown}"
    echo "hbn_image=$HBN_IMAGE"
    for img in localhost/nico/forge-dpu-agent:enclosure localhost/nico/forge-dhcp-server:enclosure; do
        echo "${img#localhost/nico/}=$(podman image inspect "$img" --format '{{.Id}}' | cut -c1-12) $(podman image inspect "$img" --format '{{index .Labels "org.opencontainers.image.version"}}' 2>/dev/null)"
    done
} > "$LIB/provenance"
cat "$LIB/provenance"

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

# Agent and DHCP server units (derived from vmctl's, re-pointed at real HBN
# and at /etc/vm/environment, which first-boot writes).
install -m 0644 "$FILES/enclosure-nico-dpu-agent.service" /etc/systemd/system/enclosure-nico-dpu-agent.service
install -m 0644 "$FILES/enclosure-nico-dhcp-server.service" /etc/systemd/system/enclosure-nico-dhcp-server.service

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
echo "enclosure DPU image provisioned (HBN $HBN_IMAGE as $HBN_LOCAL)"
