#!/usr/bin/env bash
# nico-dev — watch a `ctr images import` progress on the VM.
#
#   bash monitor-import.sh [<tarball>] [<interval-seconds>]
#
# Run ON THE VM while image_delivery.py is importing (the delivery output
# prints the exact command). Every interval it prints the containerd content
# store size, how much it grew, the ingest rate, and — when the tarball path
# is given — a rough time-to-go for the copy. The unpack into overlayfs that
# follows the copy shows up as snapshotter growth with the content store flat.
# Exits when no `ctr … images import` process is left.

set -u
TAR="${1:-}"
INTERVAL="${2:-30}"
CONTENT=/var/lib/containerd/io.containerd.content.v1.content
SNAPS=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs

bytes() { sudo du -sb "$1" 2>/dev/null | cut -f1; }
gb()    { awk -v b="$1" 'BEGIN{printf "%.1f GB", b/1024/1024/1024}'; }

tar_bytes=0
if [[ -n "$TAR" ]]; then
    tar_bytes=$(stat -c %s "$TAR" 2>/dev/null || echo 0)
    echo "tarball : $TAR ($(gb "$tar_bytes"))"
fi
echo "sampling every ${INTERVAL}s — Ctrl-C to stop"
echo

c0=$(bytes "$CONTENT"); s0=$(bytes "$SNAPS"); t0=$(date +%s)
c_prev=$c0; t_prev=$t0
printf '%-9s %-14s %-14s %-12s %s\n' "elapsed" "content" "snapshots" "rate" "ctr"
while :; do
    ctr_line=$(ps -o etime=,pcpu= -C ctr 2>/dev/null | head -1)
    c=$(bytes "$CONTENT"); s=$(bytes "$SNAPS"); t=$(date +%s)
    dt=$(( t - t_prev )); (( dt == 0 )) && dt=1
    rate=$(( (c - c_prev) / dt ))                       # bytes/s over the last interval
    eta=""
    if (( tar_bytes > 0 && rate > 0 )); then
        remaining=$(( tar_bytes - (c - c0) ))            # copied so far = growth since we started
        (( remaining > 0 )) && eta=" ~$(( remaining / rate / 60 )) min to go for the copy (from this monitor's start)"
    fi
    printf '%-9s %-14s %-14s %-12s %s%s\n' \
        "$(( (t - t0) / 60 ))m$(( (t - t0) % 60 ))s" "$(gb "$c")" "$(gb "$s")" \
        "$(( rate / 1024 / 1024 )) MB/s" "${ctr_line:-(none)}" "$eta"
    if [[ -z "$ctr_line" ]]; then
        echo; echo "no ctr import running — done (or not started yet)."
        exit 0
    fi
    c_prev=$c; t_prev=$t
    sleep "$INTERVAL"
done
