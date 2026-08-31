#!/usr/bin/env bash
# DC Simulation — cleanup script
# Destroys the ContainerLab topology and removes generated output files.
#
# Usage:
#   ./cleanup.sh                     # uses ./output as the topology dir
#   ./cleanup.sh /path/to/output     # specify custom output dir

set -euo pipefail

OUTPUT_DIR="${1:-$(dirname "$0")/output}"
TOPO="$OUTPUT_DIR/topo.clab.yml"

echo "=== DC Simulation Cleanup ==="
echo "Output dir: $OUTPUT_DIR"
echo

# ── Destroy ContainerLab topology ────────────────────────────────────────────
if [ -f "$TOPO" ]; then
    echo "Destroying ContainerLab topology..."
    sudo clab destroy -t "$TOPO" --cleanup
    echo "Done."
else
    echo "No topology file found at $TOPO — skipping clab destroy."
fi

echo

# ── Remove generated output directory ────────────────────────────────────────
if [ -d "$OUTPUT_DIR" ]; then
    read -rp "Remove generated output directory '$OUTPUT_DIR'? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        rm -rf "$OUTPUT_DIR"
        echo "Removed $OUTPUT_DIR"
    else
        echo "Skipped removal of $OUTPUT_DIR"
    fi
fi

echo

# ── Remove Linux bridges ──────────────────────────────────────────────────────
echo "Removing Linux bridges (br-cp1, br-cp2, br-cp3, br-helper)..."
for br in br-cp1 br-cp2 br-cp3 br-helper; do
    if ip link show "$br" &>/dev/null; then
        sudo ip link set "$br" down
        sudo ip link delete "$br" type bridge
        echo "  removed $br"
    else
        echo "  $br not found — skipping"
    fi
done

echo
echo "Cleanup complete."
