#!/usr/bin/env bash
# nico-dev — nico-admin-cli on the VM WITHOUT building anything.
#
#   (on the VM)  get-admin-cli.sh ~/mac/sites/<dc>/<site>
#
# The api container ships the admin-cli binary (Linux arm64 — hence VM,
# not Mac). This extracts it to /usr/local/bin, then runs
# configure-clis.py --admin-cli-only HERE so the certs and the
# run-admin-cli.sh wrapper carry VM-side paths (the wrapper bakes the
# absolute site path at generation time).
#
# Afterwards:  <site>/run-admin-cli.sh version

set -euo pipefail

if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "Error: run this ON THE VM (the extracted binary is Linux arm64)." >&2
    echo "Mac-native CLIs are the build path: build-nico-clis.py (how-to §10)." >&2
    exit 1
fi

SITE="${1:?usage: get-admin-cli.sh <site-folder e.g. ~/mac/sites/dc1/dev1>}"
SITE="$(cd "$SITE" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"

KUBECONFIG_FILE="$(ls "$SITE"/*.kubeconfig.yaml 2>/dev/null | head -1)"
[[ -n "$KUBECONFIG_FILE" ]] || { echo "Error: no *.kubeconfig.yaml in $SITE" >&2; exit 1; }
export KUBECONFIG="$KUBECONFIG_FILE"

echo "Step 1: Extracting nico-admin-cli from the api container..."
POD="$(kubectl -n nico-system get pods -l app.kubernetes.io/name=nico-api \
       -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$POD" ]] || { echo "Error: no nico-api pod found — is nico deployed?" >&2; exit 1; }
TMP="$(mktemp)"
# exec+cat instead of `kubectl cp` — no tar dependency in the container
kubectl -n nico-system exec "$POD" -- cat /opt/carbide/nico-admin-cli > "$TMP"
[[ -s "$TMP" ]] || { echo "Error: extraction produced an empty file" >&2; exit 1; }
sudo install -m 755 "$TMP" /usr/local/bin/nico-admin-cli
rm -f "$TMP"
echo "  /usr/local/bin/nico-admin-cli ✓ ($(du -h /usr/local/bin/nico-admin-cli | cut -f1))"

echo "Step 2: Certs + run script (configure-clis.py --admin-cli-only)..."
python3 "$HERE/configure-clis.py" "$SITE" --admin-cli-only

echo ""
echo "Done. Try it:"
echo "  $SITE/run-admin-cli.sh version"
