#!/bin/bash
# Enclosure DPU first boot: turn /etc/enclosure/dpu.env (delivered by the
# cloud-init seed) into interface names, the agent's environment file and the
# enabled units. Runs once; a reboot applies the .link renames before HBN and
# the agent start.
set -euo pipefail

ENV=/etc/enclosure/dpu.env
DONE=/etc/enclosure/.first-boot-done
[[ -f "$ENV" ]] || { echo "$ENV missing; the enclosure seed did not deliver it" >&2; exit 1; }
[[ -e "$DONE" ]] && exit 0
# shellcheck disable=SC1090
. "$ENV"
for v in DPU_OOB_MAC DPU_UPLINK_MAC DPU_HOSTLINK_MAC DPU_FACTORY_MAC DPU_SERIAL NVUE_PASSWORD NICO_DPU_AGENT_IMAGE NICO_DHCP_SERVER_IMAGE; do
    [[ -n "${!v:-}" ]] || { echo "$v is required in $ENV" >&2; exit 1; }
done

link() { # $1 = mac, $2 = name, $3 = altname (optional)
    {
        printf '[Match]\nMACAddress=%s\n\n[Link]\nName=%s\n' "$1" "$2"
        [[ -n "${3:-}" ]] && printf 'AlternativeName=%s\n' "$3"
    } > "/etc/systemd/network/10-enclosure-$2.link"
}
link "$DPU_OOB_MAC" oob_net0
link "$DPU_UPLINK_MAC" p0_if p0
link "$DPU_HOSTLINK_MAC" pf0hpf_if pf0hpf

# OOB gets its address from NICo's DHCP (the relay on the leaf); the uplink and
# host link are managed by HBN, not by networkd.
cat > /etc/systemd/network/20-enclosure-oob.network <<EOF
[Match]
Name=oob_net0

[Network]
DHCP=ipv4
EOF
cat > /etc/systemd/network/20-enclosure-unmanaged.network <<EOF
[Match]
Name=p0_if pf0hpf_if pf0dpu1 pf0dpu1_if

[Link]
Unmanaged=yes
EOF

# Metadata-service veth: the DPU side (pf0dpu1) and the HBN-facing side
# (pf0dpu1_if). Addresses are assigned by the agent (169.254.169.254/30).
cat > /etc/systemd/network/30-enclosure-pf0dpu1.netdev <<EOF
[NetDev]
Name=pf0dpu1
Kind=veth

[Peer]
Name=pf0dpu1_if
EOF

# vmctl's agent and DHCP units read /etc/vm/environment.
umask 077
cat > /etc/vm/environment <<EOF
DPU_FACTORY_MAC=$DPU_FACTORY_MAC
NVUE_USERNAME=${NVUE_USERNAME:-carbide}
NVUE_PASSWORD=$NVUE_PASSWORD
NICO_DPU_AGENT_IMAGE=$NICO_DPU_AGENT_IMAGE
NICO_DHCP_SERVER_IMAGE=$NICO_DHCP_SERVER_IMAGE
EOF
umask 022

# The agent's real enumeration reads the serial from DMI; the Enclosure tool
# sets it through libvirt <sysinfo>. Fail loudly if the two disagree.
dmi=$(cat /sys/class/dmi/id/product_serial 2>/dev/null || true)
if [[ -n "$dmi" && "$dmi" != "$DPU_SERIAL" ]]; then
    echo "DMI product_serial '$dmi' differs from DPU_SERIAL '$DPU_SERIAL'; fix the libvirt <sysinfo>" >&2
    exit 1
fi

systemctl enable systemd-networkd.service enclosure-hbn.service \
    enclosure-nico-dhcp-server.service enclosure-nico-dpu-agent.service
touch "$DONE"
echo "enclosure first boot done; rebooting to apply interface names"
systemctl reboot
