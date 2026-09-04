#!/usr/bin/env bash
# =============================================================================
# nico-dev — restart-ordered.sh: bring the cluster up in provisioning order
#
# Runs ON THE VM. Last-resort recovery after a reboot (or any time the site
# "looks Running but does not work"). Kubernetes has no pod start order:
# after a VM reboot kubelet re-admits every pod at once and containerd starts
# them as sandboxes happen to come up, so which race you hit is luck. Two
# known non-converging outcomes (issues.md 20260826-#6, 20260903-#4,
# 20260904-#1): metallb-speaker parked in a long CrashLoopBackOff, and
# nico-api 1/1 Running with a poisoned read-only connection pool, never
# serving the VIP. This script replays the order provisioning used
# (DEPLOY_ORDER in deploy-dev-nico.py) so dependencies are healthy BEFORE
# their consumers start.
#
# Two modes:
#   default  infra gate, then sequential `rollout restart` of every consumer
#            in dependency order, waiting for Ready at each step.
#   --cold   infra gate, then scale ALL consumers to 0 (dependents first),
#            wait for them to be gone, then scale up in dependency order.
#            Previous replica counts are recorded in an annotation and
#            restored. This is the hammer; use it when default was not enough.
#
# Infrastructure (kube-system, metallb, local-path, cert-manager, vault,
# external-secrets, the postgres operator and BOTH postgres clusters) is
# never blanket-restarted: each is verified with a bounded wait and repaired
# (rollout restart) only when unhealthy. Databases are never scaled down —
# the api trouble was consumers connecting during the leader election, so
# the fix is gating consumers on the leader, not bouncing the database.
#
# Usage (on the VM):
#   sudo restart-ordered.sh [--cold] [--dry-run] [--skip-infra] [--yes]
#                           [--on-insufficient-cpu scale-down-first|wait]
#   KUBECONFIG defaults to /etc/kubernetes/admin.conf.
#   On a fully committed single node a rolling restart of a maxSurge-1
#   Deployment cannot place its surge pod; scale-down-first (default here)
#   flips that one rollout to maxSurge 0 / maxUnavailable 1 on the symptom
#   and restores the chart's strategy after (issues.md 20260903-#2).
#
# Consumers = Deployments/StatefulSets in nico-system, nico-rest, temporal,
# flow. Workloads already at 0 replicas with no restore annotation are left
# alone (someone scaled them down on purpose). Unknown workloads in those
# namespaces are handled AFTER the known ones of their namespace and flagged
# — that is the chart-drift signal to update the ORDER list below.
# =============================================================================
set -euo pipefail

COLD=0; DRY=0; SKIP_INFRA=0; YES=0
# Rolling mode on a fully committed single node: a Deployment with maxSurge 1
# cannot place its surge pod ("Insufficient cpu", issues.md 20260903-#2).
# scale-down-first = on that symptom only, switch the deployment to
# maxSurge 0 / maxUnavailable 1 for this rollout and restore the chart's
# strategy afterwards. wait = report and move on. Symptom-triggered, so a
# cluster with room never sees the patch.
ON_INSUFFICIENT_CPU=scale-down-first
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cold)       COLD=1 ;;
        --dry-run)    DRY=1 ;;
        --skip-infra) SKIP_INFRA=1 ;;
        --yes|-y)     YES=1 ;;
        --on-insufficient-cpu) shift; ON_INSUFFICIENT_CPU="${1:-}"
            [[ "$ON_INSUFFICIENT_CPU" =~ ^(wait|scale-down-first)$ ]] || { echo "--on-insufficient-cpu wait|scale-down-first" >&2; exit 2; } ;;
        -h|--help)    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
if [[ ! -r "$KUBECONFIG" ]]; then
    echo "cannot read $KUBECONFIG — run with sudo, or export KUBECONFIG" >&2; exit 1
fi

if [[ -t 1 ]]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'; else G=; R=; Y=; B=; N=; fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s⚠%s %s\n' "$Y" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
hdr()  { printf '\n%s== %s ==%s\n' "$B" "$*" "$N"; }
die()  { bad "$*"; exit 1; }

K() { kubectl "$@"; }
FAILURES=0

# ── Dependency order of consumers (namespace  glob) ──────────────────────────
# Derived from the chart templates: nico-api needs vault+postgres; nico-dns and
# nico-pxe need the api; machine-a-tron needs nico-dhcp; the site-agent tries
# its nico-core gRPC connection exactly ONCE at startup (rest_deploy.py), so it
# must come up after the api; flow needs api + temporal. Leaves first.
ORDER=(
    "nico-system  nico-unbound*"
    "nico-system  nico-ntp*"
    "nico-system  nico-dhcp*"
    "nico-system  nico-hardware-health*"
    "nico-system  nico-ssh-console*"
    "nico-system  nico-dsx-exchange*"
    "nico-system  nico-api*"
    "nico-system  nico-dns*"
    "nico-system  nico-pxe*"
    "nico-system  nico-bmc-proxy*"
    "nico-system  *machine-a-tron*"
    "nico-system  *"
    "nico-rest    keycloak*"
    "temporal     temporal-frontend*"
    "temporal     temporal-history*"
    "temporal     temporal-matching*"
    "temporal     temporal-worker*"
    "temporal     temporal-web*"
    "temporal     temporal-admintools*"
    "temporal     *"
    "nico-rest    nico-rest-api*"
    "nico-rest    nico-rest-cert-manager*"
    "nico-rest    nico-rest-cloud-worker*"
    "nico-rest    nico-rest-site-manager*"
    "nico-rest    nico-rest-site-worker*"
    "nico-rest    nico-rest-site-agent*"     # tries its nico-core gRPC connection ONCE at startup — after api and temporal
    "nico-rest    *"
    "flow         flow*"
    "flow         *"
)
CONSUMER_NS=(nico-system nico-rest temporal flow)
ANNOT="nico-dev.io/restore-replicas"

# ── Inventory: "ns kind name replicas" for every consumer workload ───────────
INV=()
inventory() {
    INV=()
    local ns line
    for ns in "${CONSUMER_NS[@]}"; do
        K get ns "$ns" >/dev/null 2>&1 || continue
        while IFS= read -r line; do
            [[ -n "$line" ]] && INV+=("$ns $line")
        done < <(K get deploy,sts -n "$ns" -o jsonpath='{range .items[*]}{.kind}{" "}{.metadata.name}{" "}{.spec.replicas}{" "}{.metadata.annotations.'"${ANNOT//./\\.}"'}{"\n"}{end}' 2>/dev/null)
    done
}

# Emit the inventory sorted by ORDER; unknown ones sit at their namespace's
# catch-all slot and are flagged.
ORDERED=()
QUIET_ORDER=0   # set to 1 on re-reads so drift warnings print once
order_inventory() {
    ORDERED=()
    local -A seen=()
    local slot ns pat e ens kind name rep ann key
    for slot in "${ORDER[@]}"; do
        read -r ns pat <<<"$slot"
        for e in "${INV[@]}"; do
            read -r ens kind name rep ann <<<"$e"
            key="$ens/$kind/$name"
            [[ "$ens" == "$ns" && -z "${seen[$key]:-}" ]] || continue
            # shellcheck disable=SC2053
            [[ "$name" == $pat ]] || continue
            seen[$key]=1
            [[ "$pat" == "*" && $QUIET_ORDER == 0 ]] && warn "unlisted workload in $ns: $kind/$name (ordered last in its namespace — update ORDER if it has dependencies)"
            ORDERED+=("$e")
        done
    done
}

# ── Generic wait/repair helper for infrastructure ────────────────────────────
# ensure_workload <ns> <kind/name> <timeout-s>: wait for rollout; if it does
# not settle, rollout-restart once and wait again. Missing workload = warn.
# rollout_wait <ns> <kind/name> <timeout-s>: `kubectl rollout status` for
# Deployments/DaemonSets. StatefulSets are polled on readyReplicas instead:
# `rollout status` REFUSES OnDelete StatefulSets (the vault chart's default),
# which made vault a false failure on the first live run (20260904-#2).
rollout_wait() {
    local ns="$1" ref="$2" t="$3"
    if [[ "$ref" != sts/* && "$ref" != statefulset/* ]]; then
        K rollout status -n "$ns" "$ref" --timeout="${t}s" >/dev/null 2>&1
        return
    fi
    local deadline=$((SECONDS+t)) want have
    while :; do
        want=$(K get -n "$ns" "$ref" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")
        have=$(K get -n "$ns" "$ref" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
        [[ -n "$want" && "${have:-0}" == "$want" ]] && return 0
        [[ $SECONDS -ge $deadline ]] && return 1
        sleep 5
    done
}

# restart_workload <ns> <kind/name>: rollout restart; for OnDelete
# StatefulSets that changes only the template, so delete the pods too
# (the controller recreates them from the new revision).
restart_workload() {
    local ns="$1" ref="$2" name="${2#*/}" strat pods
    K rollout restart -n "$ns" "$ref" >/dev/null
    if [[ "$ref" == sts/* || "$ref" == statefulset/* ]]; then
        strat=$(K get -n "$ns" "$ref" -o jsonpath='{.spec.updateStrategy.type}' 2>/dev/null || true)
        if [[ "$strat" == OnDelete ]]; then
            pods=$(K get pods -n "$ns" -o jsonpath='{range .items[?(@.metadata.ownerReferences[0].name=="'"$name"'")]}{.metadata.name}{" "}{end}' 2>/dev/null || true)
            # shellcheck disable=SC2086
            [[ -n "$pods" ]] && K delete pod -n "$ns" $pods --wait=false >/dev/null
        fi
    fi
}

ensure_workload() {
    local ns="$1" ref="$2" t="$3"
    if ! K get -n "$ns" "$ref" >/dev/null 2>&1; then
        warn "$ns $ref not found — skipped"; return 0
    fi
    if rollout_wait "$ns" "$ref" "$t"; then
        ok "$ns $ref ready"; return 0
    fi
    warn "$ns $ref not ready in ${t}s — restarting"
    [[ $DRY == 1 ]] && return 0
    restart_workload "$ns" "$ref"
    if rollout_wait "$ns" "$ref" "$t"; then
        ok "$ns $ref ready after restart"
    else
        bad "$ns $ref still not ready — continuing, but expect failures downstream"
        FAILURES=$((FAILURES+1))
    fi
}

# Pods of a daemonset/deployment stuck in CrashLoopBackOff?
crashlooping() {
    local ns="$1" sel="$2"
    K get pods -n "$ns" -l "$sel" -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{end}' 2>/dev/null \
        | grep -q CrashLoopBackOff
}

# ── Phase 1: infrastructure gate ─────────────────────────────────────────────
infra_gate() {
    hdr "Phase 1: infrastructure gate (verify, repair only if unhealthy)"

    say "  control plane"
    K get --raw /readyz >/dev/null 2>&1 && ok "apiserver /readyz" || die "apiserver not ready — this script cannot help until kubelet/etcd are up (journalctl -u kubelet)"
    local node_ready
    node_ready=$(K get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}')
    [[ "$node_ready" == True ]] && ok "node Ready" || die "node not Ready (kubectl describe node) — hostname changed? see issues.md 20260903-#4"
    ensure_workload kube-system deploy/coredns 120

    say "  fabric + metallb"
    if ip link show 2>/dev/null | grep -q 'br-.*-cp'; then ok "fabric control-plane bridge present"; else warn "no br-*-cp bridge — is nico-dev-fabric running? (systemctl status nico-dev-fabric)"; fi
    ensure_workload metallb-system deploy/metallb-controller 120
    if crashlooping metallb-system app.kubernetes.io/component=speaker; then
        warn "metallb-speaker CrashLoopBackOff — rollout restart (skips the backoff)"
        [[ $DRY == 1 ]] || K rollout restart -n metallb-system ds/metallb-speaker >/dev/null
    fi
    ensure_workload metallb-system ds/metallb-speaker 120

    say "  storage + PKI"
    ensure_workload local-path-storage deploy/local-path-provisioner 120
    ensure_workload cert-manager deploy/cert-manager 120
    ensure_workload cert-manager deploy/cert-manager-cainjector 120
    ensure_workload cert-manager deploy/cert-manager-webhook 120

    say "  vault"
    ensure_workload vault sts/vault 180
    # file-mode vault is unsealed by its sidecar within ~70s of the pod
    # starting (generate_dev_values.py). Wait for that rather than restart.
    local i sealed=unknown
    for i in $(seq 1 18); do
        sealed=$(K exec -n vault vault-0 -c vault -- vault status -format=json 2>/dev/null | grep -o '"sealed": *[a-z]*' | grep -o '[a-z]*$' || echo unknown)
        [[ "$sealed" == false ]] && break
        sleep 5
    done
    if [[ "$sealed" == false ]]; then ok "vault unsealed"
    else bad "vault sealed/unreachable after 90s (kubectl -n vault logs vault-0 -c vault-unsealer; is vault-init-keys present?)"; FAILURES=$((FAILURES+1)); fi

    say "  external-secrets"
    ensure_workload external-secrets deploy/external-secrets 120
    ensure_workload external-secrets deploy/external-secrets-webhook 120
    ensure_workload external-secrets deploy/external-secrets-cert-controller 120

    say "  postgres"
    ensure_workload postgres deploy/postgres-operator 120
    ensure_workload postgres sts/postgres 300            # REST postgres (keycloak/temporal DBs)
    # nico-pg-cluster is operator-managed (patroni); require a running leader.
    local leader=""
    for i in $(seq 1 36); do
        leader=$(K exec -n postgres nico-pg-cluster-0 -c postgres -- patronictl list -f json 2>/dev/null \
                 | grep -o '"Role": *"Leader"[^}]*"State": *"[a-z ]*"' | grep -o '"State": *"[a-z ]*"' | head -1 || true)
        [[ "$leader" == *running* ]] && break
        sleep 5
    done
    if [[ "$leader" == *running* ]]; then ok "nico-pg-cluster has a running Leader"
    else bad "nico-pg-cluster: no running Leader after 3 min (kubectl exec -n postgres nico-pg-cluster-0 -c postgres -- patronictl list)"; FAILURES=$((FAILURES+1)); fi

    say "  ExternalSecrets synced"
    local notready
    for i in $(seq 1 24); do
        notready=$(K get externalsecrets -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}={.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -v '=True$' || true)
        [[ -z "$notready" ]] && break
        sleep 5
    done
    if [[ -z "$notready" ]]; then ok "all ExternalSecrets Ready"
    else warn "ExternalSecrets not Ready: $(echo "$notready" | tr '\n' ' ')"; fi

    if [[ $FAILURES -gt 0 ]]; then
        warn "$FAILURES infrastructure check(s) failed — consumers may not recover; continuing anyway"
    fi
}

# ── Phase 2/3 helpers ────────────────────────────────────────────────────────
kind_short() { case "$1" in Deployment) echo deploy ;; StatefulSet) echo sts ;; *) echo "$1" ;; esac; }

wait_ready() {   # <ns> <kind/name> <timeout>
    if rollout_wait "$1" "$2" "$3"; then ok "$1 $2 ready"
    else bad "$1 $2 not ready in $3s (kubectl -n $1 describe $2)"; FAILURES=$((FAILURES+1)); fi
}

# JSON literal for a maxSurge/maxUnavailable value ("25%" → quoted, 1 → bare)
json_val() { if [[ "$1" =~ ^[0-9]+$ ]]; then printf '%s' "$1"; else printf '"%s"' "$1"; fi; }

# rolling restart of ONE Deployment with the Insufficient-cpu policy applied
# on symptom: a Pending pod of this deployment whose PodScheduled message
# names "Insufficient cpu". Strategy is always restored, even on failure.
restart_deploy_watched() {   # <ns> <name> <timeout>
    local ns="$1" name="$2" t="$3" ref="deploy/$2"
    local deadline=$((SECONDS+t)) patched=0 os ou msg
    restart_workload "$ns" "$ref"
    while :; do
        if rollout_wait "$ns" "$ref" 15; then ok "$ns $ref ready"; break; fi
        if [[ $patched == 0 ]]; then
            msg=$(K get pods -n "$ns" -o jsonpath='{range .items[?(@.status.phase=="Pending")]}{.metadata.name}{"|"}{.status.conditions[?(@.type=="PodScheduled")].message}{"\n"}{end}' 2>/dev/null \
                  | grep "^$name-" | grep -m1 "Insufficient cpu" || true)
            if [[ -n "$msg" ]]; then
                if [[ "$ON_INSUFFICIENT_CPU" == scale-down-first ]]; then
                    os=$(K get -n "$ns" "$ref" -o jsonpath='{.spec.strategy.rollingUpdate.maxSurge}')
                    ou=$(K get -n "$ns" "$ref" -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}')
                    warn "$ns $ref: surge pod Pending on Insufficient cpu — scale-down-first: maxSurge 0 / maxUnavailable 1 for this rollout (chart: ${os:-default}/${ou:-default})"
                    K patch -n "$ns" "$ref" --type merge -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}' >/dev/null
                    patched=1
                else
                    warn "$ns $ref: surge pod Pending on Insufficient cpu (policy wait) — unstick: kubectl -n $ns patch $ref --type merge -p '{\"spec\":{\"strategy\":{\"rollingUpdate\":{\"maxSurge\":0,\"maxUnavailable\":1}}}}' then restore"
                    patched=2   # reported once
                fi
            fi
        fi
        if [[ $SECONDS -ge $deadline ]]; then
            bad "$ns $ref not ready in ${t}s (kubectl -n $ns describe $ref)"; FAILURES=$((FAILURES+1)); break
        fi
    done
    if [[ $patched == 1 ]]; then
        local p
        if [[ -z "$os" && -z "$ou" ]]; then
            p='{"spec":{"strategy":{"rollingUpdate":null}}}'   # chart set neither → back to k8s defaults
        else
            p='{"spec":{"strategy":{"rollingUpdate":{'
            [[ -n "$os" ]] && p+='"maxSurge":'"$(json_val "$os")"','
            [[ -n "$ou" ]] && p+='"maxUnavailable":'"$(json_val "$ou")"','
            p="${p%,}}}}}"
        fi
        K patch -n "$ns" "$ref" --type merge -p "$p" >/dev/null && say "    restored $ref rollout strategy to the chart's"
    fi
}

# default mode: sequential rollout restart in order
warm_restart() {
    hdr "Phase 2: rolling restart of consumers in dependency order"
    local e ns kind name rep ann ref
    for e in "${ORDERED[@]}"; do
        read -r ns kind name rep ann <<<"$e"
        ref="$(kind_short "$kind")/$name"
        if [[ "${rep:-0}" == 0 ]]; then say "  - $ns $ref at 0 replicas — left alone"; continue; fi
        say "  → $ns $ref"
        [[ $DRY == 1 ]] && continue
        if [[ "$kind" == Deployment ]]; then
            restart_deploy_watched "$ns" "$name" 180
        else
            restart_workload "$ns" "$ref"
            wait_ready "$ns" "$ref" 180
        fi
    done
}

# --cold: scale everything to 0 (dependents first), then up in order
cold_restart() {
    hdr "Phase 2: quiesce — scale all consumers to 0 (dependents first)"
    local i e ns kind name rep ann ref
    for (( i=${#ORDERED[@]}-1; i>=0; i-- )); do
        read -r ns kind name rep ann <<<"${ORDERED[$i]}"
        ref="$(kind_short "$kind")/$name"
        if [[ "${rep:-0}" == 0 && -z "$ann" ]]; then say "  - $ns $ref already 0, no restore marker — left alone"; continue; fi
        if [[ "${rep:-0}" != 0 ]]; then
            say "  ↓ $ns $ref ($rep → 0)"
            [[ $DRY == 1 ]] && continue
            K annotate -n "$ns" "$ref" "$ANNOT=$rep" --overwrite >/dev/null
            K scale -n "$ns" "$ref" --replicas=0 >/dev/null
        else
            say "  ↓ $ns $ref already 0 (restore marker $ann kept)"
        fi
    done
    if [[ $DRY == 0 ]]; then
        say "  waiting for consumer pods to terminate…"
        local deadline=$((SECONDS+180)) left
        while :; do
            left=0
            for ns in "${CONSUMER_NS[@]}"; do
                K get ns "$ns" >/dev/null 2>&1 || continue
                left=$((left + $(K get pods -n "$ns" --no-headers 2>/dev/null | grep -vcE 'Completed|Succeeded' || true)))
            done
            [[ $left -eq 0 ]] && { ok "all consumer pods gone"; break; }
            [[ $SECONDS -ge $deadline ]] && { warn "$left pod(s) still terminating after 3 min — continuing (Jobs/hooks are not scaled)"; break; }
            sleep 5
        done
    fi

    hdr "Phase 3: bring consumers up in dependency order"
    if [[ $DRY == 0 ]]; then
        QUIET_ORDER=1; inventory; order_inventory   # re-read: replicas are 0 now, annotations carry the targets
    fi
    for e in "${ORDERED[@]}"; do
        read -r ns kind name rep ann <<<"$e"
        ref="$(kind_short "$kind")/$name"
        [[ $DRY == 1 && -z "$ann" ]] && ann="$rep"   # dry run wrote no markers; preview with current replicas
        [[ -z "$ann" || "$ann" == 0 ]] && { say "  - $ns $ref no restore marker — left at 0"; continue; }
        say "  ↑ $ns $ref (0 → $ann)"
        [[ $DRY == 1 ]] && continue
        K scale -n "$ns" "$ref" --replicas="$ann" >/dev/null
        wait_ready "$ns" "$ref" 180
        K annotate -n "$ns" "$ref" "$ANNOT-" >/dev/null 2>&1 || true
    done
}

# ── Phase 4: does the site actually answer? ──────────────────────────────────
final_probe() {
    hdr "Phase 4: verdict"
    local eps vip code
    eps=$(K get endpoints -n nico-system nico-api -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
    if [[ -n "$eps" ]]; then ok "nico-api service has endpoints ($eps)"
    else bad "nico-api service has NO endpoints — api pod Running but not serving (issues.md 20260826-#6)"; FAILURES=$((FAILURES+1)); fi
    # the VIP lives on the chart's external service (nico-api-external), not the ClusterIP one
    vip=$(K get svc -n nico-system nico-api-external -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "$vip" ]]; then
        code=$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "https://$vip/admin" 2>/dev/null || true)
        if [[ "$code" =~ ^[23][0-9][0-9]$ || "$code" == 401 || "$code" == 403 ]]; then ok "admin UI answers at https://$vip/admin (HTTP $code)"
        else bad "admin UI not answering at https://$vip/admin (HTTP ${code:-000}) — from the VM itself, so this is not the Mac route"; FAILURES=$((FAILURES+1)); fi
    else
        warn "nico-api-external has no LoadBalancer IP yet (metallb) — kubectl -n nico-system get svc nico-api-external"
    fi
    local not_running
    not_running=$(K get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed|Succeeded' || true)
    if [[ -z "$not_running" ]]; then ok "every pod in the cluster is Running/Completed"
    else warn "pods not Running:"; echo "$not_running" | sed 's/^/      /'; fi
    echo
    if [[ $FAILURES -eq 0 ]]; then printf '%sSITE HEALTHY%s\n' "$G" "$N"
    else printf '%s%d CHECK(S) FAILED%s — see ✗ lines above\n' "$R" "$FAILURES" "$N"; exit 1; fi
}

# ── main ─────────────────────────────────────────────────────────────────────
hdr "restart-ordered.sh — mode: $([[ $COLD == 1 ]] && echo COLD '(scale to 0, then up in order)' || echo 'rolling (restart in order)')$([[ $DRY == 1 ]] && echo ' [DRY RUN]')"
say "  KUBECONFIG=$KUBECONFIG"
inventory
[[ ${#INV[@]} -gt 0 ]] || die "no consumer workloads found in ${CONSUMER_NS[*]} — is this a nico-dev VM?"
order_inventory
say "  plan (${#ORDERED[@]} workloads, in this order):"
for e in "${ORDERED[@]}"; do read -r ns kind name rep ann <<<"$e"; printf '    %-12s %-12s %-32s replicas=%s%s\n' "$ns" "$kind" "$name" "${rep:-0}" "${ann:+ (restore marker $ann)}"; done

if [[ $DRY == 0 && $YES == 0 && $COLD == 1 ]]; then
    printf '\n  --cold takes the whole site down for a few minutes. Continue? [y/N] '
    read -r a; [[ "$a" =~ ^[Yy]$ ]] || { say "aborted"; exit 0; }
fi

[[ $SKIP_INFRA == 1 ]] && warn "--skip-infra: not verifying infrastructure" || infra_gate
if [[ $COLD == 1 ]]; then cold_restart; else warm_restart; fi
[[ $DRY == 1 ]] && { say; say "dry run — nothing changed"; exit 0; }
final_probe
