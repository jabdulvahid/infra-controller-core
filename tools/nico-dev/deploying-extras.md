# nico-dev — Deploying Secondary Charts (Observability, Flow, nico-mcp, …)

The base deploy covers the four developer surfaces (nico-admin-cli, nicocli,
gRPC API, REST API — the REST stack became base on 2026-08-20). Anything
beyond that is deliberately NOT built in — this documents the generic pattern
for adding optional stacks after the base is up, plus a catalog of what the
nico repo ships.

## The generic pattern

Everything a secondary chart needs, nico-dev already provides the plumbing for:

```bash
# 1. Aim at the cluster (from the Mac; VM works too)
export KUBECONFIG=~/projects/sites/<dc>/<site>/<dc>-<site>.kubeconfig.yaml

# 2. Keep YOUR values in the SITE folder, never in the nico repo
mkdir -p <site>/extra-values
$EDITOR <site>/extra-values/<thing>.yaml

# 3. Plain helm against the repo's chart path
helm upgrade --install <release> <nico-repo>/<chart-path> \
  -n <namespace> --create-namespace \
  -f <site>/extra-values/<thing>.yaml --wait
```

Rules that keep this painless:

- **The site folder is the home for everything site-specific** — values,
  generated certs, notes. The nico repo stays pristine (same rule as the
  core deploy).
- **Check what's already there before installing dependencies.** nico-dev
  provides: cert-manager, vault (file mode), external-secrets,
  postgres-operator + `nico-pg-cluster` (single instance), MetalLB, and
  local-path StorageClass. Secondary charts often list these as prereqs —
  you already have them.
- **Mind the VM's 16 GB.** The core stack + fabric uses a good share of it.
  Heavy extras (observability especially) may need trimmed values
  (single replicas, small retention) or a bigger VM.
- Same helm recovery rules as the core deploy apply (stuck
  `pending-install` → the release-secret cleanup; never delete a namespace
  that holds helm release state).

## Catalog: what the nico repo ships beyond the core stack

### RMS — not a chart (common confusion)

RMS is the **component-manager backend inside nico-api**: `rms.enabled` in
`helm/charts/nico-api/values.yaml` plus the `[rms]` section of the site-config
TOML (the same section nico-dev's `allow_insecure_discovery` patch touches).
Backends per role (`computeTray`/`nvSwitch`/`powerShelf`: `rms | nsm/psm/core |
mock`) and state-controller dispatch are chart values. To exercise RMS in
nico-dev: set the values in the site yaml's helm-values and redeploy nico —
nothing separate to install. (Production RMS-managed tenant clusters
additionally use the kamaji operator — `helm-prereqs/operators/values/kamaji.yaml`.)

### nico-rest family — NOW PART OF BASE nico-dev (2026-08-20)

The REST stack (rest-postgres, Keycloak, Temporal, the `nico-rest` umbrella,
site-agent) is deployed by `deploy-dev-nico.py` as base releases — all four
developer surfaces (nico-admin-cli, nicocli, gRPC API, REST API) work out of
the box. Toggle off with `nico-system.rest.enabled: false` in the site yaml.
Only `nico-mcp` remains a manual extra (`helm/rest/nico-mcp`).

### Observability — `helm-prereqs/observability/`

Loki + Tempo + OTEL collectors + kube-prometheus-stack (Grafana with
datasources and auto-loaded dashboards; Prometheus scrapes the `carbide_*`
metrics via the NICo ServiceMonitors). Fully site-local.

- Entry point: `helm-prereqs/observability/install-observability.sh`
  (idempotent, self-contained — the one extra that ships its own installer)
- Warning: this is the heaviest optional stack — on a 16 GB VM expect to trim
  retention/replicas, or size the VM up

### Keycloak — `helm-prereqs/keycloak/`

SSO for the REST API. Entry point: `helm-prereqs/keycloak/setup.sh` (+ realm
configmap, token helper).

### Operator extras — `helm-prereqs/operators/` + `helmfile.yaml`

argo-cd, kamaji, NFD, DPF operator values live here; production installs them
via `helm-prereqs/helmfile.yaml` release selectors. In nico-dev, cherry-pick
with plain helm + the values file from `operators/values/` if a feature needs
one.

### Core-chart toggles (not separate installs)

`helm/charts/` subcharts are switched via the main nico values (nico-dev
disables `nico-pxe` and `nico-dsx-exchange-consumer` by default; `nico-flow`,
`unbound`, `nico-bmc-proxy` are similarly toggleable in the site yaml's
helm-values).
