#!/usr/bin/env bash
# nico-dev — prerequisites check, Linux host edition (libvirt/KVM).
# READ-ONLY: probes only; creates and changes nothing.
#
#   check-prereqs.sh            # via the platform dispatcher
#   check-prereqs.sh --build    # + build-from-source tier
#
# Exit 0 = required checks passed; each ✗ says how to fix.

set -u
BUILD=0
[ "${1:-}" = "--build" ] && BUILD=1
FAIL=0

ok()   { printf '  \342\234\223 %s\n' "$1"; }
bad()  { printf '  \342\234\227 %s\n     fix: %s\n' "$1" "$2"; FAIL=1; }
note() { printf '  \342\232\240 %s\n' "$1"; }

echo "── nico-dev prerequisites (Linux host) ──"

# Arch (both are fine — images/builds follow the host arch)
case "$(uname -m)" in
  x86_64|aarch64) ok "arch: $(uname -m)" ;;
  *) bad "unsupported arch: $(uname -m)" "x86_64 or aarch64 required" ;;
esac

# KVM
if [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
  ok "KVM available (/dev/kvm accessible)"
else
  bad "/dev/kvm missing or not accessible" \
      "enable VT-x/AMD-V in BIOS; sudo usermod -aG kvm \$USER (relogin)"
fi

# libvirt reachable WITHOUT sudo (system connection)
if command -v virsh >/dev/null 2>&1; then
  if virsh -c qemu:///system list >/dev/null 2>&1; then
    ok "libvirt reachable without sudo (qemu:///system)"
  else
    bad "virsh cannot reach qemu:///system without sudo" \
        "sudo usermod -aG libvirt \$USER (then relogin)"
  fi
else
  bad "virsh not found" \
      "sudo apt install libvirt-daemon-system libvirt-clients"
fi

# virt-install + cloud-localds (seed) + qemu-img
command -v virt-install >/dev/null 2>&1 \
  && ok "virt-install present" \
  || bad "virt-install not found" "sudo apt install virtinst"
command -v cloud-localds >/dev/null 2>&1 \
  && ok "cloud-localds present (cloud-init seed builder)" \
  || bad "cloud-localds not found" "sudo apt install cloud-image-utils"
command -v qemu-img >/dev/null 2>&1 \
  && ok "qemu-img present" \
  || bad "qemu-img not found" "sudo apt install qemu-utils"
# virtiofs: the share transport on Linux (9p is wrong under system-mode
# QEMU — guest-written files would be owned by libvirt-qemu on the host)
if [ -x /usr/libexec/virtiofsd ] || [ -x /usr/lib/qemu/virtiofsd ] || command -v virtiofsd >/dev/null 2>&1; then
  ok "virtiofsd present (share transport)"
else
  bad "virtiofsd not found" "sudo apt install virtiofsd"
fi

# Subnet plan: nico-nat wants 192.168.64.0/24 — is it already taken?
SUBNET_IN_ROUTES=$(ip route 2>/dev/null | grep -c '192\.168\.64\.' || true)
SUBNET_IN_NETS=$(virsh -c qemu:///system net-list --all --name 2>/dev/null \
  | while read -r n; do [ -n "$n" ] && virsh -c qemu:///system net-dumpxml "$n" 2>/dev/null; done \
  | grep -c "192\.168\.64\." || true)
if [ "${SUBNET_IN_ROUTES:-0}" -eq 0 ] && [ "${SUBNET_IN_NETS:-0}" -eq 0 ]; then
  ok "subnet 192.168.64.0/24 free (nico-nat can claim it)"
else
  if virsh -c qemu:///system net-dumpxml nico-nat 2>/dev/null | grep -q '192\.168\.64\.'; then
    ok "subnet 192.168.64.0/24 in use by nico-nat (ours) ✓"
  else
    note "192.168.64.0/24 already present on this host (route or libvirt net)"
    note "— the VM builder will need --subnet <other> here"
  fi
fi

# Disk
AVAIL_GB=$(df -BG --output=avail "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
NEED=40; [ "$BUILD" = 1 ] && NEED=100
if [ "${AVAIL_GB:-0}" -ge "$NEED" ]; then
  ok "disk: ${AVAIL_GB}GB free in \$HOME (need ~${NEED}GB)"
else
  bad "disk: only ${AVAIL_GB:-?}GB free (need ~${NEED}GB)" "free up space"
fi

# libvirt default storage pool (VM disks live in a pool, not \$HOME —
# system-mode QEMU cannot read your home directory)
if virsh -c qemu:///system pool-info default >/dev/null 2>&1; then
  ok "libvirt storage pool 'default' exists"
else
  note "no 'default' storage pool — the VM builder will create a nico-dev pool"
fi

# SSH key + python + git (same as Mac tier)
if ls "$HOME"/.ssh/id_*.pub >/dev/null 2>&1; then
  ok "SSH public key present ($(ls "$HOME"/.ssh/id_*.pub | head -1 | xargs basename))"
else
  bad "no SSH keypair in ~/.ssh" "ssh-keygen -t ed25519"
fi
python3 -c 'import yaml' >/dev/null 2>&1 \
  && ok "python3 + pyyaml" \
  || bad "python3 pyyaml missing" "sudo apt install python3-yaml"
command -v git >/dev/null 2>&1 \
  && ok "git $(git --version | awk '{print $3}')" \
  || bad "git not found" "sudo apt install git"

if [ "$BUILD" = 1 ]; then
  echo "── build-from-source tier ──"
  for tool in docker kubectl helm; do
    command -v "$tool" >/dev/null 2>&1 \
      && ok "$tool installed" \
      || bad "$tool not found" "install $tool (docker.io via apt; kubectl/helm per their docs)"
  done
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
  else
    bad "docker daemon not reachable" \
        "sudo systemctl start docker; sudo usermod -aG docker \$USER (relogin)"
  fi
  docker buildx version >/dev/null 2>&1 \
    && ok "docker buildx present" \
    || bad "docker buildx not found" "sudo apt install docker-buildx"
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "All checks passed."
else
  echo "Fix the ✗ items above, then rerun."
fi
exit $FAIL
