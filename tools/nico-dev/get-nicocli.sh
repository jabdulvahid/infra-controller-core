#!/usr/bin/env bash
# nico-dev — nicocli (the REST CLI) WITHOUT building anything.
#
#   (on the VM, like get-admin-cli.sh)  get-nicocli.sh ~/mac/sites/dc1/dev1
#   (or on the Mac / Linux host)        get-nicocli.sh <share>/sites/dc1/dev1
#
# The nico-rest-api image ships nicocli at /app/nicocli ("as a debugging
# convenience", per its Dockerfile), built from the same commit as the REST
# API it talks to. Unlike the admin CLI it cannot be copied out of a RUNNING
# pod: the image is distroless (no shell, no cat, no tar), so `kubectl exec`
# and `kubectl cp` have nothing to run. So this takes it from the IMAGE. On
# the VM the image is in containerd (the bring-up delivers every image there
# for kubelet): `ctr images mount` exposes its filesystem, no container runs.
# On the host it is in the docker store (the NGC lane pulls it, the source
# lane pushes it to the local registry and it is pulled back if needed):
# docker create + docker cp, no container runs either.
#
# Writes:
#   <site>/nicocli/nicocli    Linux ELF for the VM's architecture (arm64 on an
#                             Apple Silicon Mac) — run it ON THE VM through the
#                             wrapper; on a Linux host the arch matches, so the
#                             wrapper runs it locally.
#   <site>/run-nicocli.sh     wrapper: base URL, org and a cached Keycloak token
#                             baked in; self-locating, works from the share path
#                             on either side.
#
# Afterwards (on the VM):
#   <site>/run-nicocli.sh --bootstrap      # once per fresh site: creates the org's
#                                          #   provider + tenant objects (403s otherwise)
#   <site>/run-nicocli.sh vpc list

set -euo pipefail

SITE="${1:?usage: get-nicocli.sh <site-folder e.g. ~/nico-tests/vm1/shared/sites/dc1/dev1>}"
SITE="$(cd "$SITE" && pwd)"
command -v python3 >/dev/null || { echo "Error: python3 with pyyaml is required" >&2; exit 1; }

SITE_YAML="$(ls "$SITE"/*.yaml 2>/dev/null | grep -v '\.kubeconfig\.yaml$' | head -1)"
[[ -n "$SITE_YAML" ]] || { echo "Error: no site yaml in $SITE" >&2; exit 1; }

# ── read what we need from the site yaml ─────────────────────────────────────
read -r REST_TAG REG_PORT REG_HOST VM_IP REPO_FOLDER VM_SITE MAC_SITE < <(python3 - "$SITE_YAML" "$SITE" <<'PYEOF'
import sys, os, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
img = c.get('images') or {}
tags = img.get('tags') or {}
tag = tags.get('rest') or img.get('tag') or (c.get('registry') or {}).get('nico_tag') or ''
port = (c.get('registry') or {}).get('port', 5000)
reg_host = (c.get('registry') or {}).get('host') or '192.168.64.1'   # the registry as the VM sees it
vm_ip = (c.get('vm') or {}).get('ip') or '192.168.64.126'
repo = c.get('nico_repo_folder') or 'infra-controller-core'
# the same site folder as the VM sees it: <nico_mac_folder>/x -> <nico_vm_folder>/x,
# and the other way round when this runs on the VM (to name the Mac command).
site = os.path.realpath(sys.argv[2])
mac_root = os.path.realpath(os.path.expanduser(c.get('nico_mac_folder', ''))).rstrip('/')
vm_root = (c.get('nico_vm_folder') or '').rstrip('/')
vm_site = vm_root + site[len(mac_root):] if mac_root and vm_root and site.startswith(mac_root + '/') else '<site-on-the-VM>'
mac_site = mac_root + site[len(vm_root):] if mac_root and vm_root and site.startswith(vm_root + '/') else ''
print(tag, port, reg_host, vm_ip, repo, vm_site, mac_site or '-')
PYEOF
)
[[ -n "$REST_TAG" ]] || { echo "Error: the site yaml records no images tag yet — deploy nico first" >&2; exit 1; }

OUT_DIR="$SITE/nicocli"
mkdir -p "$OUT_DIR"
echo "nico-dev — nicocli from the REST API image"
echo "  site   : $SITE"

if [[ "$MAC_SITE" != "-" ]]; then
    # ── On the VM: the image is in containerd, delivered there for kubelet ──
    # The site folder sits under nico_vm_folder, so this is the VM. containerd
    # holds the image under the registry name the VM pulls from; match on the
    # repository and tag so a different registry host still works.
    command -v ctr >/dev/null || { echo "Error: ctr (containerd's CLI) is required on the VM" >&2; exit 1; }
    echo "  where  : VM (containerd)"
    echo
    echo "Step 1: Locating the image in containerd..."
    IMAGE="$(sudo ctr -n k8s.io images ls -q 2>/dev/null | grep -E "/nico-rest-api:${REST_TAG}\$" | head -1 || true)"
    if [[ -z "$IMAGE" ]]; then
        echo "Error: no nico-rest-api:${REST_TAG} image in the VM's containerd — is the REST tag deployed?" >&2
        echo "  (expected ${REG_HOST}:${REG_PORT}/nico-rest-api:${REST_TAG}; sudo ctr -n k8s.io images ls -q | grep nico-rest-api)" >&2
        exit 1
    fi
    echo "  $IMAGE ✓"
    # ── Step 2: mount the image's filesystem and copy the binary (nothing runs) ─
    echo "Step 2: Extracting /app/nicocli..."
    MNT="$(mktemp -d)"
    trap 'sudo ctr -n k8s.io images unmount "$MNT" >/dev/null 2>&1 || true; rmdir "$MNT" 2>/dev/null || true' EXIT
    sudo ctr -n k8s.io images mount "$IMAGE" "$MNT" >/dev/null
    cp "$MNT/app/nicocli" "$OUT_DIR/nicocli"
    sudo ctr -n k8s.io images unmount "$MNT" >/dev/null; rmdir "$MNT"; trap - EXIT
    ARCH="linux/$(uname -m)"
else
    # ── On the host: the image is in the docker store or the local registry ──
    command -v docker >/dev/null || { echo "Error: docker is required on the host (this reads the nico-rest-api image from the docker store)" >&2; exit 1; }
    IMAGE="localhost:${REG_PORT}/nico-rest-api:${REST_TAG}"
    echo "  where  : host (docker)"
    echo "  image  : $IMAGE"
    echo
    echo "Step 1: Locating the image..."
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "  in the local docker store ✓"
    else
        echo "  not in the local store — pulling from the local registry (buildx --push keeps no local copy)"
        docker pull "$IMAGE" >/dev/null || { echo "Error: $IMAGE is neither local nor in the registry — is the REST tag deployed?" >&2; exit 1; }
        echo "  pulled ✓"
    fi
    ARCH="$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')"
    # ── Step 2: copy the binary out (docker create + cp — nothing runs) ─────
    echo "Step 2: Extracting /app/nicocli..."
    CID="$(docker create "$IMAGE")"
    trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
    docker cp "$CID:/app/nicocli" "$OUT_DIR/nicocli"
    docker rm -f "$CID" >/dev/null; trap - EXIT
fi
chmod 755 "$OUT_DIR/nicocli"
echo "  $OUT_DIR/nicocli ✓ ($ARCH, $(du -h "$OUT_DIR/nicocli" | cut -f1))"

# ── Step 3: the wrapper ──────────────────────────────────────────────────────
echo "Step 3: Writing run-nicocli.sh..."
WRAPPER="$SITE/run-nicocli.sh"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# nicocli for this site — generated by get-nicocli.sh; regenerate rather than edit.
#
#   run-nicocli.sh --bootstrap        # once per fresh site (provider + tenant objects)
#   run-nicocli.sh --refresh-token    # drop the cached token, then run the command
#   run-nicocli.sh vpc list           # any nicocli command
#
# Base URL   : http://${VM_IP}:30388   (the REST API NodePort on the VM)
# Org        : ncx
# Token      : minted in-cluster by helm-prereqs/keycloak/get-token.sh (client
#              ncx-service, 30-minute lifetime), cached in \$SITE/.nicocli-token
#              for 25 minutes. Needs kubectl + the site's kubeconfig; both are on
#              the VM and on the Mac.
# The binary in \$SITE/nicocli/ is a Linux ELF for the VM. On a Mac this wrapper
# uses a \`nicocli\` from PATH instead (build-nico-clis.py can build one).

set -euo pipefail
SITE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
SHARE="\$(cd "\$SITE/../../.." && pwd)"                 # <share>/sites/<dc>/<site> → <share>
GET_TOKEN="\$SHARE/${REPO_FOLDER}/helm-prereqs/keycloak/get-token.sh"
TOKEN_FILE="\$SITE/.nicocli-token"
TOKEN_MAX_AGE_MIN=25

if [[ "\$(uname -s)" == "Darwin" ]]; then
    BIN="\$(command -v nicocli || true)"
    [[ -n "\$BIN" ]] || { echo "Error: \$SITE/nicocli/nicocli is a Linux binary. Run this wrapper on the VM, or build a Mac nicocli with build-nico-clis.py (needs Go)." >&2; exit 1; }
else
    BIN="\$SITE/nicocli/nicocli"
fi

# Always the site's own cluster, whatever KUBECONFIG the shell carries.
SITE_KUBECONFIG="\$(ls "\$SITE"/*.kubeconfig.yaml 2>/dev/null | head -1)"
[[ -n "\$SITE_KUBECONFIG" ]] && export KUBECONFIG="\$SITE_KUBECONFIG"

refresh=false
if [[ "\${1:-}" == "--refresh-token" ]]; then refresh=true; shift; fi
if \$refresh || [[ ! -s "\$TOKEN_FILE" ]] || [[ -n "\$(find "\$TOKEN_FILE" -mmin +\$TOKEN_MAX_AGE_MIN 2>/dev/null)" ]]; then
    [[ -x "\$GET_TOKEN" || -f "\$GET_TOKEN" ]] || { echo "Error: token script not found at \$GET_TOKEN (is the checkout at <share>/${REPO_FOLDER}?)" >&2; exit 1; }
    umask 077
    if ! bash "\$GET_TOKEN" > "\$TOKEN_FILE.tmp" 2> "\$TOKEN_FILE.err"; then
        rm -f "\$TOKEN_FILE.tmp"
        echo "Error: could not mint a token (cluster reachable? nico-rest deployed?). The token script said:" >&2
        sed 's/^/    /' "\$TOKEN_FILE.err" >&2; rm -f "\$TOKEN_FILE.err"
        exit 1
    fi
    rm -f "\$TOKEN_FILE.err"; mv "\$TOKEN_FILE.tmp" "\$TOKEN_FILE"
fi

export NICO_BASE_URL="http://${VM_IP}:30388"
export NICO_ORG="ncx"
NICO_TOKEN="\$(cat "\$TOKEN_FILE")"
export NICO_TOKEN

if [[ "\${1:-}" == "--bootstrap" ]]; then
    # A fresh site has no provider/tenant objects for the org; every other call
    # returns 403 until these two have run once.
    "\$BIN" infrastructure-provider current
    "\$BIN" tenant current
    exit \$?
fi
exec "\$BIN" "\$@"
EOF
chmod 755 "$WRAPPER"
echo "  $WRAPPER ✓"

echo
echo "Done. On the VM (ssh $VM_IP):"
echo "  $VM_SITE/run-nicocli.sh --bootstrap     # once per fresh site"
echo "  $VM_SITE/run-nicocli.sh vpc list"
echo "On the Mac the same wrapper works if a Mac nicocli is on your PATH:"
echo "  ${SITE/#$HOME/~}/run-nicocli.sh vpc list"
