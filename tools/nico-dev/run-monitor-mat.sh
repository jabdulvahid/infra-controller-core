#!/usr/bin/env bash
# nico-dev — MAT run monitor with the site hardwired for vm1 (dc1/dev1).
#
#   (on the VM)  run-monitor-mat.sh                      # full-screen, refresh 30 s, q quits
#   (on the VM)  run-monitor-mat.sh --once               # one plain-text snapshot (paste-friendly)
#   (on the VM)  run-monitor-mat.sh --no-tui             # plain text every 30 s
#   (on the VM)  run-monitor-mat.sh --mat-log /var/log/machine-a-tron-dc1-dev.log   # pin one log
#
# MAT logs: without --mat-log, every /var/log/machine-a-tron-<dc_name>*.log is
# passed, where <dc_name> is fabric.dc_name from the site yaml — the name the
# generated run-mat*.sh scripts write their logs under (run-mat-dev.sh →
# machine-a-tron-<dc_name>-dev.log). monitor-mat.py opens on the most recently
# written one; [ ] a switch on its MAT page. Give --mat-log (repeatable) to pin
# exactly the log(s) you want instead. MAT writes its logs as root, so the
# monitor runs under sudo when a log is not readable.
#
# Needs $SITE/run-admin-cli.sh (get-admin-cli.sh writes it): that wrapper is
# how the monitor asks NICo for machines, endpoints and DPUs. Edit SITE and
# TOOLS for another site.

SITE=/home/nico/mac/sites/dc1/dev1
TOOLS=/home/nico/mac/infra-controller/tools/nico-dev

[[ -x "$SITE/run-admin-cli.sh" ]] || { echo "Error: $SITE/run-admin-cli.sh not found — run get-admin-cli.sh $SITE first" >&2; exit 1; }

# --mat-log given? Then pass the arguments through untouched.
pinned=0
for a in "$@"; do [[ "$a" == "--mat-log" || "$a" == --mat-log=* ]] && pinned=1; done

logs=()
if (( ! pinned )); then
    # fabric.dc_name from the site yaml (the one file that is not a kubeconfig);
    # configure-clis.py defaults it to "dev" when the key is absent.
    site_yaml=$(ls "$SITE"/*.yaml 2>/dev/null | grep -v '\.kubeconfig\.yaml$' | head -1)
    dc_name=""
    if [[ -n "$site_yaml" ]]; then
        # pyyaml when present, a plain regex otherwise (no heredoc: bash 3.2 on
        # macOS mis-parses quotes inside a heredoc within $(...))
        dc_name=$(python3 -c "
import re, sys
try:
    import yaml
    print((yaml.safe_load(open(sys.argv[1])).get('fabric') or {}).get('dc_name', 'dev'))
except Exception:
    m = re.search(r'^\s*dc_name:\s*[\"\']?([^\"\'\s#]+)', open(sys.argv[1]).read(), re.M)
    print(m.group(1) if m else 'dev')
" "$site_yaml" 2>/dev/null)
    fi
    dc_name=${dc_name:-dev}
    shopt -s nullglob
    logs=(/var/log/machine-a-tron-"$dc_name"*.log)
    shopt -u nullglob
    (( ${#logs[@]} )) || echo "note: no /var/log/machine-a-tron-$dc_name*.log yet — server view only (a MAT run creates it)" >&2
fi

args=()
SUDO=""
for log in "${logs[@]}"; do
    args+=(--mat-log "$log")
    [[ -r "$log" ]] || SUDO=sudo
done
for a in "$@"; do
    # a pinned log that is not readable needs sudo too
    [[ "$a" == /var/log/* && -e "$a" && ! -r "$a" ]] && SUDO=sudo
done
[[ -n "${MONITOR_ALL_LOGS:-}" ]] && args+=(--all-logs)
exec $SUDO python3 "$TOOLS/monitor-mat.py" --admin-cli "$SITE/run-admin-cli.sh" "${args[@]}" "$@"
