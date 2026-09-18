#!/usr/bin/env bash
# nico-dev — MAT run monitor with everything hardwired for the vm1 site dc1/dev1.
#
#   (on the VM)  run-monitor-mat.sh            # full-screen, refresh 30 s, q quits
#   (on the VM)  run-monitor-mat.sh --once     # one plain-text snapshot (paste-friendly)
#   (on the VM)  run-monitor-mat.sh --no-tui   # plain text every 30 s
#
# Edit the three settings below for another site. Only MAT logs that exist are
# passed on, so it works before any MAT run (server view only). MAT writes its
# logs as root, so the monitor runs under sudo when a log is not readable.

SITE=/home/nico/mac/sites/dc1/dev1
TOOLS=/home/nico/mac/infra-controller/tools/nico-dev
LOGS=(
    /var/log/machine-a-tron-dc1-base.log     # run-mat-base.sh
    /var/log/machine-a-tron-dc1-dev.log      # run-mat-dev.sh
    /var/log/machine-a-tron-dc1.log          # run-mat.sh
)

args=()
SUDO=""
for log in "${LOGS[@]}"; do
    [[ -e "$log" ]] || continue
    args+=(--mat-log "$log")
    [[ -r "$log" ]] || SUDO=sudo
done
[[ -x "$SITE/run-admin-cli.sh" ]] || { echo "Error: $SITE/run-admin-cli.sh not found — run get-admin-cli.sh $SITE first" >&2; exit 1; }
exec $SUDO python3 "$TOOLS/monitor-mat.py" --admin-cli "$SITE/run-admin-cli.sh" "${args[@]}" "$@"
