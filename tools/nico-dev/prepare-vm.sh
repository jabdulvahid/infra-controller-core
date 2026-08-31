#!/usr/bin/env bash
# nico-dev — Base VM setup script
#
# Two modes:
#
#   init  — Run on the MAC. SSHes into the VM, mounts the shared folder,
#            runs this script remotely, then sets up passwordless SSH.
#            With --static-ip it also switches the VM to that IP at the end
#            (golden-image builds; see static-ip mode below).
#
#            bash prepare-vm.sh init --vm-ip 192.168.64.4 [--vm-user USER] \
#                 [--static-ip 192.168.64.126]
#
#            Share name defaults to "share", mount points are fixed:
#              /mnt/mac  (9p)  and  ~/mac  (bindfs)
#
#   <share-name>  — Run on the VM (called automatically by init, or manually).
#
#            bash prepare-vm.sh share
#
# After this completes, see nico-dev's how-to.md for next steps.

set -euo pipefail

# ── init subcommand (runs on Mac) ─────────────────────────────────────────────
if [[ "${1:-}" == "init" ]]; then
    shift
    VM_IP=""
    VM_USER="${USER}"
    SHARE_NAME="share"
    STATIC_IP=""
    SSH_KEY_ARG=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --vm-ip)     VM_IP="$2";      shift 2 ;;
            --vm-user)   VM_USER="$2";    shift 2 ;;
            --share)     SHARE_NAME="$2"; shift 2 ;;
            --static-ip) STATIC_IP="$2";  shift 2 ;;
            --ssh-key)   SSH_KEY_ARG="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    if [[ -z "${VM_IP}" ]]; then
        echo "Error: --vm-ip is required" >&2
        echo "Usage: bash prepare-vm.sh init --vm-ip 192.168.64.4 [--vm-user USER] [--static-ip 192.168.64.126]" >&2
        exit 1
    fi

    # Detect this script's path on the Mac to derive the share-relative path
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    echo "=== nico-dev init ==="
    echo "  VM         : ${VM_USER}@${VM_IP}"
    echo "  share name : ${SHARE_NAME}"
    echo "  script     : ${SCRIPT_DIR}/prepare-vm.sh"
    echo ""
    echo "Step 1: Mounting shared folder on VM and running prepare-vm.sh..."
    echo "  (you will be prompted for the VM user password)"
    echo ""

    # Mount the 9p share on the VM, then run this script from within it.
    # The script's location inside the share is DISCOVERED, not hardcoded —
    # nico-dev may live at <repo>/tools/nico-dev, nico-dev, or anywhere else.
    # -t allocates a TTY so sudo password prompts work interactively.
    ssh -t "${VM_USER}@${VM_IP}" \
        "sudo mkdir -p /mnt/mac && \
         (mountpoint -q /mnt/mac || sudo mount -t 9p -o trans=virtio,version=9p2000.L,rw '${SHARE_NAME}' /mnt/mac) && \
         REMOTE_SCRIPT=\$(find /mnt/mac -maxdepth 4 -type f -name prepare-vm.sh 2>/dev/null | head -1) && \
         if [ -z \"\$REMOTE_SCRIPT\" ]; then echo 'ERROR: prepare-vm.sh not found in the share — is the nico-dev folder inside the shared directory?' >&2; exit 1; fi && \
         echo \"  running: \$REMOTE_SCRIPT\" && \
         bash \"\$REMOTE_SCRIPT\" '${SHARE_NAME}'"

    echo ""
    echo "Step 2: Setting up passwordless SSH from Mac to VM..."

    # Pick ONE key explicitly. Bare ssh-copy-id uses the agent's keys
    # (ssh-add -L): during its login attempt ssh offers them all, the server
    # rejects each, and sshd closes the connection ("Too many authentication
    # failures") BEFORE password auth gets a turn — no prompt, silent failure.
    if [[ -n "${SSH_KEY_ARG}" ]]; then
        SSH_KEY_ARG="${SSH_KEY_ARG/#\~/$HOME}"
        if [[ ! -f "${SSH_KEY_ARG}" ]]; then
            echo "Error: --ssh-key ${SSH_KEY_ARG} not found" >&2
            exit 1
        fi
        CANDIDATES=("${SSH_KEY_ARG}")
    else
    CANDIDATES=()
    for k in "${HOME}/.ssh/id_nico_sim.pub" "${HOME}/.ssh/id_ed25519.pub" \
             "${HOME}/.ssh/id_rsa.pub" "${HOME}"/.ssh/*.pub; do
        [[ -f "$k" ]] || continue
        dup=0
        for c in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
            [[ "$c" == "$k" ]] && dup=1 && break
        done
        [[ "$dup" -eq 0 ]] && CANDIDATES+=("$k")
    done

    if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
        echo "  No SSH key found — generating ed25519 key..."
        ssh-keygen -t ed25519 -N "" -f "${HOME}/.ssh/id_ed25519"
        CANDIDATES=("${HOME}/.ssh/id_ed25519.pub")
    fi
    fi   # end of key auto-discovery (skipped when --ssh-key given)

    if [[ ${#CANDIDATES[@]} -eq 1 ]]; then
        PUB="${CANDIDATES[0]}"
    else
        echo "  Multiple SSH keys found — pick ONE to install on the VM:"
        i=1
        for k in "${CANDIDATES[@]}"; do
            echo "    $i) $k"
            i=$((i+1))
        done
        read -rp "  Key number [1]: " sel
        sel="${sel:-1}"
        if ! [[ "$sel" =~ ^[0-9]+$ ]] || (( sel < 1 || sel > ${#CANDIDATES[@]} )); then
            echo "Error: invalid selection '$sel'" >&2
            exit 1
        fi
        PUB="${CANDIDATES[$((sel-1))]}"
    fi
    PRIV="${PUB%.pub}"
    echo "  Installing key: ${PUB}"
    echo "  (you may be prompted for the VM password once)"

    ssh-copy-id -i "${PUB}" "${VM_USER}@${VM_IP}" || true

    # Verify with exactly that identity — IdentitiesOnly avoids the agent flood
    if ! ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "${PRIV}" \
             -o ConnectTimeout=5 "${VM_USER}@${VM_IP}" true 2>/dev/null; then
        echo ""
        echo "ERROR: passwordless SSH to ${VM_USER}@${VM_IP} is not working after ssh-copy-id." >&2
        echo "  Fix it manually, then re-run this init command (it is idempotent):" >&2
        echo "    ssh-copy-id -i ${PUB} ${VM_USER}@${VM_IP}" >&2
        echo "    ssh -i ${PRIV} -o IdentitiesOnly=yes ${VM_USER}@${VM_IP} true && echo OK" >&2
        exit 1
    fi
    echo "  passwordless SSH verified ✓ (${PUB})"

    # Optional: switch the VM to a static IP (golden-image builds).
    # Runs after ssh-copy-id because static-ip's verification uses key auth.
    FINAL_IP="${VM_IP}"
    if [[ -n "${STATIC_IP}" ]]; then
        echo ""
        echo "Step 3: Switching VM to static IP ${STATIC_IP}..."
        bash "${SCRIPT_DIR}/prepare-vm.sh" static-ip \
            --vm-ip "${VM_IP}" --new-ip "${STATIC_IP}" --vm-user "${VM_USER}" \
            --priv-key "${PRIV}"
        FINAL_IP="${STATIC_IP}"
    fi

    echo ""
    echo "============================================="
    echo "  init complete ✓"
    echo ""
    echo "  Test passwordless SSH:"
    echo "    ssh ${VM_USER}@${FINAL_IP}"
    echo ""
    echo "  Next step — log into the VM and create your site"
    echo "  (see advanced-user.md Step 3; state your repo/nico-dev folder names explicitly):"
    echo "    ssh ${VM_USER}@${FINAL_IP}"
    echo "    python3 ~/mac/<path-to>/nico-dev/create-dev-site.py \\"
    echo "      --dc-name dc1 --site-name dev \\"
    echo "      --underlay <n> --overlay <m> \\"
    echo "      --folder ~/mac/sites/dev \\"
    echo "      --nico-vm-folder /home/${VM_USER}/mac \\"
    echo "      --nico-mac-folder <your Mac share path, e.g. /Users/<you>/projects> \\"
    echo "      --nico-repo-folder <nico repo dir in the share, e.g. infra-controller-core> \\"
    echo "      --nico-dev-folder <nico-dev dir in the share, e.g. infra-controller-core/tools/nico-dev>"
    echo "============================================="
    exit 0
fi

# ── static-ip subcommand (runs on Mac) ────────────────────────────────────────
# Change the VM to a static IP without hand-editing netplan (needed only when
# baking a golden image). Auto-detects interface/MAC/gateway on the VM, writes
# an override file (cloud-init's 50-cloud-init.yaml is left untouched), syntax-
# checks with `netplan generate` BEFORE applying, applies detached from the SSH
# session, then verifies the new IP and the shared folder from the Mac.
if [[ "${1:-}" == "static-ip" ]]; then
    shift
    VM_IP=""
    NEW_IP=""
    VM_USER="${USER}"
    PRIV_KEY=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --vm-ip)    VM_IP="$2";    shift 2 ;;
            --new-ip)   NEW_IP="$2";   shift 2 ;;
            --vm-user)  VM_USER="$2";  shift 2 ;;
            --priv-key) PRIV_KEY="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    if [[ -z "${VM_IP}" || -z "${NEW_IP}" ]]; then
        echo "Usage: bash prepare-vm.sh static-ip --vm-ip <current-ip> --new-ip <static-ip> [--vm-user USER] [--priv-key ~/.ssh/id_x]" >&2
        exit 1
    fi

    # Pin the identity when given — a bare BatchMode ssh offers every agent
    # key and can fail on 'too many authentication failures'.
    ID_OPTS=()
    if [[ -n "${PRIV_KEY}" ]]; then
        ID_OPTS=(-o IdentitiesOnly=yes -i "${PRIV_KEY}")
    fi

    echo "=== nico-dev static-ip ==="
    echo "  VM      : ${VM_USER}@${VM_IP}"
    echo "  new IP  : ${NEW_IP}"
    echo ""

    # The target IP must be FREE — if something already answers (typically an
    # old golden VM still running), two machines would fight over the address.
    if ping -c 1 -t 2 "${NEW_IP}" >/dev/null 2>&1; then
        echo "Error: ${NEW_IP} is already in use — is an old VM still running at that IP?" >&2
        echo "  Shut it down in UTM, then re-run." >&2
        exit 1
    fi

    # Remote script: everything auto-detected on the VM, nothing hand-typed.
    # netplan apply runs via systemd-run so it survives the SSH disconnect.
    ssh "${VM_USER}@${VM_IP}" "sudo bash -s -- '${NEW_IP}'" <<'REMOTE'
set -euo pipefail
NEW_IP="$1"

IFACE=$(ip route show default | awk '{print $5; exit}')
GW=$(ip route show default | awk '{print $3; exit}')
MAC=$(cat "/sys/class/net/${IFACE}/address")

echo "  detected: iface=${IFACE} mac=${MAC} gateway=${GW}"

cat > /etc/netplan/99-nico-static.yaml <<EOF
# Written by prepare-vm.sh static-ip — overrides cloud-init's DHCP config.
# To revert to DHCP: rm this file and /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg, then netplan apply.
network:
  version: 2
  ethernets:
    ${IFACE}:
      match:
        macaddress: ${MAC}
      set-name: ${IFACE}
      dhcp4: false
      dhcp6: false
      addresses:
        - ${NEW_IP}/24
      routes:
        - to: default
          via: ${GW}
      nameservers:
        addresses: [${GW}]
EOF
chmod 600 /etc/netplan/99-nico-static.yaml

# Stop cloud-init from ever re-rendering network config over this
mkdir -p /etc/cloud/cloud.cfg.d
echo 'network: {config: disabled}' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg

# Validate BEFORE applying — a syntax error here would strand the VM
netplan generate
echo "  netplan config valid ✓"

# Apply detached so the SSH disconnect doesn't kill it
systemd-run --unit nico-netplan-apply --no-block sh -c 'sleep 2; netplan apply'
echo "  netplan apply scheduled — SSH connection will drop now"
REMOTE

    # A previous VM at this IP leaves a stale known_hosts entry — the new
    # host key would make ssh refuse the connection (looks like 'unreachable').
    # We EXPECT a new host at this address, so clear the old entry.
    ssh-keygen -R "${NEW_IP}" >/dev/null 2>&1 || true

    echo ""
    echo "Waiting for VM at ${NEW_IP}..."
    OK=0
    for _ in $(seq 1 30); do
        sleep 2
        if ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
               ${ID_OPTS[@]+"${ID_OPTS[@]}"} "${VM_USER}@${NEW_IP}" true 2>/dev/null; then
            OK=1; break
        fi
    done
    if [[ "${OK}" -ne 1 ]]; then
        echo "Error: VM not reachable via SSH at ${NEW_IP} after 60s." >&2
        if ping -c 1 -t 2 "${NEW_IP}" >/dev/null 2>&1; then
            echo "  The host DOES answer ping — the IP switch worked; SSH auth/hostkey is the problem." >&2
            echo "  Try:  ssh-keygen -R ${NEW_IP}" >&2
            echo "        ssh ${ID_OPTS[*]:-} ${VM_USER}@${NEW_IP} true" >&2
        else
            echo "  No ping response either — inspect from the UTM console:" >&2
            echo "    ip addr; cat /etc/netplan/99-nico-static.yaml; systemctl status nico-netplan-apply" >&2
            echo "  Revert from console:  sudo rm /etc/netplan/99-nico-static.yaml && sudo netplan apply" >&2
        fi
        exit 1
    fi

    echo "Verifying share and connectivity on the VM..."
    ssh -o BatchMode=yes ${ID_OPTS[@]+"${ID_OPTS[@]}"} "${VM_USER}@${NEW_IP}" \
        "mountpoint -q /mnt/mac && echo '  shared folder mounted ✓' || echo '  WARNING: /mnt/mac not mounted'; \
         ping -c1 -W2 \$(ip route show default | awk '{print \$3; exit}') >/dev/null && echo '  gateway reachable ✓'; \
         getent hosts archive.ubuntu.com >/dev/null && echo '  DNS resolving ✓' || echo '  WARNING: DNS not resolving'"

    echo ""
    echo "============================================="
    echo "  static-ip complete ✓  VM is now ${NEW_IP}"
    echo ""
    echo "  Optional — name it in /etc/hosts on the Mac:"
    echo "    echo '${NEW_IP}  nico-dev' | sudo tee -a /etc/hosts"
    echo "============================================="
    exit 0
fi

# ── VM setup (runs on VM) ─────────────────────────────────────────────────────
REAL_USER="${USER}"
REAL_HOME="${HOME}"
SHARE_NAME="${1:-}"
MOUNT_9P="/mnt/mac"
MOUNT_USER="${REAL_HOME}/mac"

echo "=== nico-dev base VM setup ==="
echo "  user : ${REAL_USER}"
echo "  home : ${REAL_HOME}"

# ── Require share name ────────────────────────────────────────────────────────
if [[ -z "${SHARE_NAME}" ]]; then
    if [[ -t 0 ]]; then
        echo ""
        echo "Enter the UTM shared folder name as shown in UTM settings"
        echo "(e.g. 'share', 'Mac', 'myshare'):"
        read -r SHARE_NAME
    else
        echo "Error: share name required as first argument." >&2
        echo "Usage: bash prepare-vm.sh <utm-share-name>" >&2
        exit 1
    fi
fi
echo "  share: ${SHARE_NAME} → ${MOUNT_9P} → ${MOUNT_USER}"
echo ""

# ── Hardware preflight (advisory — recommended, not required) ─────────────────
echo "=== Checking hardware (recommended: 100GB disk / 16GB RAM / 8 CPUs) ==="
BELOW=0

# Measure the raw disk, not the root filesystem — partitions + ext4 overhead
# eat ~3-5% (a correct 100GB disk shows ~97GB in df), and some hypervisors
# provision decimal GB (100 GB = 93 GiB). Accept ≥93 GiB of physical disk.
DISK_GIB=$(lsblk -b -dn -o SIZE,TYPE 2>/dev/null | awk '$2=="disk"{s+=$1} END{print int(s/1073741824)}')
if [[ -z "${DISK_GIB}" || "${DISK_GIB}" -lt 93 ]]; then
    echo "  WARNING: total disk is ${DISK_GIB:-unknown} GiB — 100GB recommended (image builds + caches are disk-hungry)"
    BELOW=1
else
    echo "  disk   : ${DISK_GIB} GiB ✓"
fi

RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [[ "${RAM_GB}" -lt 15 ]]; then
    echo "  WARNING: RAM is ${RAM_GB}GB — 16GB recommended (k8s + fabric + Nico pods)"
    BELOW=1
else
    echo "  RAM    : ${RAM_GB}GB ✓"
fi

CPUS=$(nproc)
if [[ "${CPUS}" -lt 8 ]]; then
    echo "  WARNING: CPUs is ${CPUS} — 8 recommended"
    BELOW=1
else
    echo "  CPUs   : ${CPUS} ✓"
fi

if [[ "${BELOW}" -eq 1 ]]; then
    echo ""
    echo "This VM is below the recommended sizing. It may work, but expect"
    echo "slower deploys and possible OOM/disk-pressure evictions under load."
    echo "To resize: shut down the VM and edit it in UTM, then re-run this script."
    if [[ -t 0 ]]; then
        read -rp "Proceed anyway? [y/N]: " reply
        if [[ "${reply,,}" != "y" ]]; then
            echo "Exiting — resize the VM and re-run."
            exit 1
        fi
        echo "  Proceeding below recommended sizing (your call) ✓"
    else
        # Non-interactive (no TTY): require explicit opt-in
        if [[ "${NICO_DEV_ALLOW_UNDERSIZED:-0}" != "1" ]]; then
            echo "Non-interactive run: set NICO_DEV_ALLOW_UNDERSIZED=1 to proceed anyway." >&2
            exit 1
        fi
        echo "  NICO_DEV_ALLOW_UNDERSIZED=1 — proceeding ✓"
    fi
fi
echo ""

# ── System packages ───────────────────────────────────────────────────────────
echo "=== Installing system packages ==="
sudo apt-get update -q
sudo apt-get install -y -q \
  python3-yaml \
  python3-pip \
  curl \
  wget \
  git \
  jq \
  bindfs \
  openssh-server \
  iptables \
  iptables-persistent \
  netfilter-persistent \
  iproute2 \
  net-tools \
  dnsutils
echo "  System packages installed ✓"

# ── Allow fuse mounts by non-root users ───────────────────────────────────────
if ! grep -q "^user_allow_other" /etc/fuse.conf 2>/dev/null; then
    echo "user_allow_other" | sudo tee -a /etc/fuse.conf > /dev/null
fi
echo "  fuse user_allow_other ✓"

# ── Docker ────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installing Docker ==="
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sudo bash
    sudo usermod -aG docker "${REAL_USER}"
    sudo systemctl enable --now docker
    echo "  Docker installed ✓"
else
    echo "  Docker already installed ✓"
    # Ensure user is in docker group
    if ! groups "${REAL_USER}" | grep -q docker; then
        sudo usermod -aG docker "${REAL_USER}"
        echo "  Added ${REAL_USER} to docker group ✓"
    fi
fi

# Configure Docker DNS to use UTM gateway (192.168.64.1).
# Required when VM uses a static IP — static netplan does not inherit DHCP DNS
# so Docker containers would otherwise fail to resolve names.
sudo mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]] || ! grep -q "192.168.64.1" /etc/docker/daemon.json; then
    echo '{"dns": ["192.168.64.1"]}' | sudo tee /etc/docker/daemon.json > /dev/null
    sudo systemctl restart docker
    echo "  Docker DNS → 192.168.64.1 ✓"
else
    echo "  Docker DNS already configured ✓"
fi

# ── ContainerLab ──────────────────────────────────────────────────────────────
echo ""
echo "=== Installing ContainerLab ==="
if ! command -v clab &>/dev/null; then
    # Download to a file and CHECK it — `bash -c "$(curl -sL …)"` with a failed
    # curl becomes `bash -c ""` (exit 0) and reports success while installing
    # nothing. That exact silent failure shipped a clab-less VM once.
    curl -fsSL https://get.containerlab.dev -o /tmp/clab-install.sh
    if [[ ! -s /tmp/clab-install.sh ]]; then
        echo "  ERROR: could not download the ContainerLab installer" >&2
        exit 1
    fi
    bash /tmp/clab-install.sh
    rm -f /tmp/clab-install.sh
fi
# Verify regardless of which path ran — a ✓ here must mean the binary works
if ! command -v clab &>/dev/null; then
    echo "  ERROR: clab not on PATH after install" >&2
    exit 1
fi
echo "  ContainerLab installed ✓ ($(clab version 2>/dev/null | grep -m1 -oE 'version:?[^ ]* *[0-9.]+' || echo present))"

# ── Helm ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installing Helm ==="
if ! command -v helm &>/dev/null; then
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 -o /tmp/get-helm-3
    if [[ ! -s /tmp/get-helm-3 ]]; then
        echo "  ERROR: could not download the Helm installer" >&2
        exit 1
    fi
    bash /tmp/get-helm-3
    rm -f /tmp/get-helm-3
fi
if ! command -v helm &>/dev/null; then
    echo "  ERROR: helm not on PATH after install" >&2
    exit 1
fi
echo "  Helm installed ✓ ($(helm version --short 2>/dev/null || echo present))"

# ── kubectl ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Installing kubectl ==="
if ! command -v kubectl &>/dev/null; then
    KUBE_VER=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
    curl -fsSL -o /tmp/kubectl \
        "https://dl.k8s.io/release/${KUBE_VER}/bin/linux/arm64/kubectl"
    sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
    rm /tmp/kubectl
    echo "  kubectl installed ✓"
else
    echo "  kubectl already installed ✓"
fi

# ── SSH server ────────────────────────────────────────────────────────────────
echo ""
echo "=== Configuring SSH ==="
sudo systemctl enable --now ssh
# Ensure PasswordAuthentication is on (needed for initial ssh-copy-id from Mac)
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo systemctl reload ssh
echo "  SSH server enabled, PasswordAuthentication on ✓"

# ── Expand LVM to use full disk ───────────────────────────────────────────────
echo ""
echo "=== Expanding LVM to use full disk ==="
if command -v lvdisplay &>/dev/null; then
    LV_PATH=$(lvdisplay 2>/dev/null | awk '/LV Path/ {print $3}' | head -1 || true)
    if [[ -n "${LV_PATH}" ]]; then
        sudo lvextend -l +100%FREE "${LV_PATH}" 2>/dev/null && \
            sudo resize2fs "${LV_PATH}" 2>/dev/null && \
            echo "  LV expanded ✓" || echo "  LV already at full size ✓"
    else
        echo "  No LVM volumes found, skipping ✓"
    fi
else
    echo "  lvm2 not installed, skipping ✓"
fi

# ── Containerd registry config: owned by deploy-dev-cp.py ────────────────────
# Deliberately NOT configured here. At this point containerd is running from the
# Docker-packaged stub config (CRI disabled, no registry section) — any sed
# against it silently matches nothing, and deploy-dev-cp.py regenerates the
# whole config.toml anyway. deploy-dev-cp.py Step 2 is the single owner: it
# generates the config, sets config_path + hosts.toml, and VERIFIES the result.
# Check with: ndev.py <site> registry verify

# ── TCP MSS clamping (fixes PMTUD blackhole in QEMU NAT) ─────────────────────
# QEMU's NAT silently drops TCP segments that exceed the path MTU and does not
# forward ICMP "fragmentation needed" messages back. Clamping the MSS on
# incoming SYN/SYN-ACK packets forces the VM to send smaller segments, which
# avoids the drop and is required for docker pull (and other large transfers)
# to work reliably.
echo ""
echo "=== Configuring TCP MSS clamping ==="
if ! sudo iptables -t mangle -C PREROUTING -p tcp --tcp-flags SYN,RST SYN \
     -j TCPMSS --set-mss 1200 2>/dev/null; then
    sudo iptables -t mangle -A PREROUTING -p tcp --tcp-flags SYN,RST SYN \
        -j TCPMSS --set-mss 1200
fi
sudo netfilter-persistent save
echo "  TCP MSS clamp → 1200 (persistent) ✓"

# ── Passwordless sudo ─────────────────────────────────────────────────────────
echo ""
echo "=== Configuring passwordless sudo ==="
SUDOERS_FILE="/etc/sudoers.d/nico-dev-${REAL_USER}"
echo "${REAL_USER} ALL=(ALL) NOPASSWD:ALL" | sudo tee "${SUDOERS_FILE}" > /dev/null
sudo chmod 440 "${SUDOERS_FILE}"
echo "  ${REAL_USER} can now sudo without password ✓"

# ── 9p shared folder mount ────────────────────────────────────────────────────
echo ""
echo "=== Mounting shared folder ==="
sudo mkdir -p "${MOUNT_9P}"
mkdir -p "${MOUNT_USER}"

# Mount 9p share at /mnt/mac
if ! mountpoint -q "${MOUNT_9P}"; then
    sudo mount -t 9p -o trans=virtio,version=9p2000.L,rw "${SHARE_NAME}" "${MOUNT_9P}"
    echo "  ${SHARE_NAME} → ${MOUNT_9P} mounted ✓"
else
    echo "  ${MOUNT_9P} already mounted ✓"
fi

# Bindfs remount at ~/mac with correct user ownership
if ! mountpoint -q "${MOUNT_USER}"; then
    sudo bindfs --map=root/"${REAL_USER}":@root/@"${REAL_USER}" \
        "${MOUNT_9P}" "${MOUNT_USER}"
    echo "  ${MOUNT_9P} → ${MOUNT_USER} (bindfs) mounted ✓"
else
    echo "  ${MOUNT_USER} already mounted ✓"
fi

# Sanity: the share should contain the repos. The exact names are what you
# pass to create-dev-site.py as --nico-repo-folder / --nico-dev-folder
# (validated there) — this listing shows you what to pass.
echo "  Share contents:"
ls -1 "${MOUNT_9P}" 2>/dev/null | head -10 | sed 's/^/    /'
if [ -z "$(ls -A "${MOUNT_9P}" 2>/dev/null)" ]; then
    echo "  WARNING: shared folder is EMPTY — check the UTM Shared Directory path."
    echo "  It must contain the nico repo and the nico-dev scripts folder."
fi

# ── Make mounts persistent via /etc/fstab ─────────────────────────────────────
echo ""
echo "=== Making mounts persistent ==="

# 9p entry
FSTAB_9P="${SHARE_NAME}  ${MOUNT_9P}  9p  trans=virtio,version=9p2000.L,rw,nofail,_netdev  0  0"
if ! grep -qF "${MOUNT_9P}" /etc/fstab; then
    echo "${FSTAB_9P}" | sudo tee -a /etc/fstab > /dev/null
    echo "  9p entry added to /etc/fstab ✓"
else
    echo "  9p entry already in /etc/fstab ✓"
fi

# bindfs entry
FSTAB_BINDFS="${MOUNT_9P}  ${MOUNT_USER}  fuse.bindfs  map=root/${REAL_USER}:@root/@${REAL_USER},nofail,x-systemd.requires=${MOUNT_9P}  0  0"
if ! grep -qF "${MOUNT_USER}" /etc/fstab; then
    echo "${FSTAB_BINDFS}" | sudo tee -a /etc/fstab > /dev/null
    echo "  bindfs entry added to /etc/fstab ✓"
else
    echo "  bindfs entry already in /etc/fstab ✓"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
VM_IP=$(ip route get 192.168.64.1 2>/dev/null | grep -oP 'src \K\S+' || echo "192.168.64.2")

echo ""
echo "============================================="
echo "  Base VM setup complete ✓"
echo ""
echo "  Shared folder:"
echo "    ${SHARE_NAME} → ${MOUNT_9P} → ${MOUNT_USER}"
echo ""
echo "  On Mac — set up passwordless SSH:"
echo "    ssh-copy-id ${REAL_USER}@${VM_IP}"
echo ""
# This script's own location, translated to the user-friendly bindfs path
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HINT_DIR="${SELF_DIR/#\/mnt\/mac/${MOUNT_USER}}"
echo "  Then log out and back in (docker group), and create your site"
echo "  (state your repo/nico-dev folder names explicitly; see advanced-user.md Step 3):"
echo "    python3 ${HINT_DIR}/create-dev-site.py \\"
echo "      --dc-name dc1 --site-name dev \\"
echo "      --underlay <n> --overlay <m> \\"
echo "      --folder ${MOUNT_USER}/sites/dev \\"
echo "      --nico-vm-folder ${MOUNT_USER} \\"
echo "      --nico-mac-folder <your Mac share path, e.g. /Users/<you>/projects> \\"
echo "      --nico-repo-folder <nico repo dir in the share> \\"
echo "      --nico-dev-folder <nico-dev dir in the share>"
echo "============================================="
