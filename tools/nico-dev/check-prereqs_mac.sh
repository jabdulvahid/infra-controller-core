#!/usr/bin/env bash
# nico-dev — prerequisites check (walkthrough step 0, automated)
#
#   bash check-prereqs.sh            # base tier: run VMs / bring up the sim
#   bash check-prereqs.sh --build    # + build-from-source tier (colima etc.)
#
# Exit code: 0 = required checks passed, 1 = something to fix (each ✗ line
# says what). The Automation permission for UTM cannot be pre-checked —
# the first VM-building run pops "Terminal wants to control UTM": click
# Allow (recover via System Settings → Privacy & Security → Automation).

set -u
BUILD=0
[ "${1:-}" = "--build" ] && BUILD=1
FAIL=0

ok()   { printf '  \342\234\223 %s\n' "$1"; }
bad()  { printf '  \342\234\227 %s\n     fix: %s\n' "$1" "$2"; FAIL=1; }

echo "── nico-dev prerequisites (base) ──"

case "$(uname -m)" in
  arm64)  ok "Apple Silicon (arm64)" ;;
  x86_64) ok "Intel Mac (x86_64) — best-effort, NOT a supported target (arch plumbing shared with the Linux port); expect slowness" ;;
  *)      bad "unsupported arch: $(uname -m)" "nico-dev supports arm64 and x86_64 Macs" ;;
esac

AVAIL_GB=$(df -g "$HOME" | awk 'NR==2 {print $4}')
NEED=40; [ "$BUILD" = 1 ] && NEED=100
if [ "${AVAIL_GB:-0}" -ge "$NEED" ]; then
  ok "disk: ${AVAIL_GB}GB free (need ~${NEED}GB)"
else
  bad "disk: only ${AVAIL_GB}GB free (need ~${NEED}GB)" \
      "free up space (docker builder prune -af reclaims build caches)"
fi

if [ -x "/Applications/UTM.app/Contents/MacOS/utmctl" ]; then
  ok "UTM installed"
else
  bad "UTM not found" "install from https://mac.getutm.app or the App Store, and launch it once"
fi

if ls "$HOME"/.ssh/id_*.pub >/dev/null 2>&1; then
  ok "SSH public key present ($(ls "$HOME"/.ssh/id_*.pub | head -1 | xargs basename))"
else
  bad "no SSH keypair in ~/.ssh" "ssh-keygen -t ed25519"
fi

if python3 -c 'import yaml' >/dev/null 2>&1; then
  ok "python3 + pyyaml"
else
  bad "python3 pyyaml missing" "pip3 install pyyaml"
fi

command -v git >/dev/null 2>&1 \
  && ok "git $(git --version | awk '{print $3}')" \
  || bad "git not found" "xcode-select --install (or brew install git)"

if [ "$BUILD" = 1 ]; then
  echo "── build-from-source tier ──"
  for tool in colima docker kubectl helm; do
    command -v "$tool" >/dev/null 2>&1 \
      && ok "$tool installed" \
      || bad "$tool not found" "brew install $tool"
  done
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable (colima running)"
  else
    bad "docker daemon not reachable" \
        "colima start --cpu 4 --memory 8   # default sizing OOMs the Rust build"
  fi
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "All checks passed."
else
  echo "Fix the ✗ items above, then rerun."
fi
exit $FAIL
