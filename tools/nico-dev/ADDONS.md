# nico-dev add-ons — optional charts after bring-up

Investigation and design, 2026-09-03. Ruling (Jasmeer): add-ons are a
**post-nico-dev deployment option**, one script per chart, each able to
take its images from NGC or from a local build, and **no existing script is
modified**. `deploy-flow.py` is the first one and the template for the rest.

## Why a base nico-dev site has no Flow

nico-dev deliberately renders `flow.enabled: false` into the nico-prereqs
values (`generate_dev_values.py`), disables the `nico-flow` sub-chart of the
umbrella, and installs the REST site-agent with `FLOW_GRPC_ENABLED=false`
(`rest_deploy.py`). The base deploy therefore has none of Flow's
prerequisites: no `flow`/`psm`/`nsm` databases on `nico-pg-cluster`, no
ESO-synced DB credentials, no psm/nsm Vault tokens, no `flow` Temporal
namespace, no `flow` Kubernetes namespace. Everything Flow needs is
**rendered by charts that are already installed**, so an add-on can switch
it on without touching nico-dev's scripts.

## What Flow needs, and where each piece comes from

| Need | Source in the repo | How the add-on gets it |
|---|---|---|
| images `nico-flow`, `nico-psm`, `nico-nsm` | `rest-api/docker/production/Dockerfile.<image>`; part of the REST `REST_IMAGES` list, published to NGC **at the REST tag** | `--build`: the same buildx loop `build-dev-nico.py` uses for REST images, restricted to these three; `--ngc`: pull `<base>/<image>:<tag>` for the host arch, retag into the local registry |
| databases + users `flow.nico`, `psm.nico`, `nsm.nico` | `helm-prereqs/templates/postgresql.yaml` under `flow.enabled` | `helm upgrade nico-prereqs --reuse-values --set flow.enabled=true` (Zalando operator adds them to the running cluster) |
| DB credential secrets in the `flow` namespace | `helm-prereqs/templates/eso-external-secrets.yaml`: `flow-db-eso`, `psm-db-eso`, `nsm-db-eso` ClusterExternalSecrets | same upgrade; the script waits for the three secrets |
| `psm-vault-token`, `nsm-vault-token` | `helm-prereqs/templates/flow-vault-tokens-job.yaml`, a **post-install,post-upgrade** hook that also creates and labels the `flow` namespace | same upgrade (the hook runs on upgrade); the script waits for both secrets |
| Temporal namespace `flow` | `temporal operator namespace create` via `temporal-admintools` | reuses `rest_deploy.ensure_temporal_namespace()` by import, unmodified |
| `flow-certificate` (SPIFFE, `vault-nico-issuer`) and `temporal-client-certs` (`nico-rest-ca-issuer`) | `helm/charts/nico-flow/templates/certificate.yaml` | pre-applied with `helm template --show-only`, adopted with helm annotations, waited Ready, exactly as `setup.sh` 7h does to avoid the FailedMount race |
| the chart | `helm/charts/nico-flow` (namespace `flow`, one pod, three containers on 50051/50052/50053) | `helm upgrade --install flow … --set global.image.repository=<registry> --set global.image.tag=<tag> --set flowEnv=development` |
| site-agent talking to Flow | `helm/rest/nico-rest-site-agent` `envConfig.FLOW_GRPC_ENABLED` | `helm upgrade nico-rest-site-agent --reuse-values --set envConfig.FLOW_GRPC_ENABLED=true` |

Two facts that shape the script:

- **Tag discipline.** Flow ships on the REST release line, not the core one.
  The default tag is therefore whatever `nico-rest` currently runs (read
  from its helm values), so a `--ngc` pull fetches the matching NGC tag and
  a `--build` produces a same-tag set.
- **`--reuse-values` is what makes "no existing script modified" work.** The
  nico-prereqs release keeps every value nico-dev set (including
  `vault.token`); the add-on flips one flag and lets the chart render the
  rest.

## Chart drift, found on the first live run

Upstream commit `856d2e227` (2026-08-31, "remove NSM/PSM from deployment,
#5325") turned the flow pod into a single container and **deleted**
`helm-prereqs/templates/flow-vault-tokens-job.yaml`; only `flow-db-eso`
remains. The user's worktree (`main`, 2026-09-01) has that shape; the fork
branch's copy of the charts predates it. The first run therefore waited
five minutes for a `psm-vault-token` secret that no template produces.
Ruling (Jasmeer): the script implements the **current** chart; it does not
emulate older ones. Supporting both shapes would mean coding for an
imaginary tree that is part today, part last month. So `deploy-flow.py`
targets the single-container Flow, pre-applies the chart's `namespace.yaml`
itself (the deleted hook job used to create the namespace), and its
preflight **refuses** a checkout that still has the PSM/NSM containers or
the vault-token template, with one instruction: refresh the worktree.
Lesson for every add-on: write against `origin/main`'s chart (the fork
branch's own `helm/` copy lags and misled this script), and gate on the
chart generation rather than adapting to it.

## Verdict for Flow

Easy, and **verified live** (2026-09-03, Linux site, NGC lane at the REST
tag): pull ~10 s (cached), nico-prereqs upgrade to revision 3, credential
secret synced, Temporal namespace, both certificates Ready, chart installed,
site-agent upgraded, `flow-…` pod Running — about 45 s end to end after the
image was present. Everything is chart-rendered, `setup.sh` phase 7h is a
faithful recipe, and the whole add-on is one host-side script with no new
prerequisites.

Risks to verify on the first real run (not testable offline):

1. The nico-prereqs `--reuse-values` upgrade re-renders the whole chart;
   the `vault-pki-config` job and other hooks may re-run. They are
   idempotent in `setup.sh` reruns, so this should be harmless, but watch
   `kubectl get jobs -n nico-system`.
2. The Zalando operator must add three users/databases to a running
   single-instance cluster; it does this on spec change, no restart.
3. CPU: the flow pod requests 300m across three containers. On a 6-CPU VM
   with ~90% committed (issues.md 20260903-#2) it may sit Pending. The
   add-on does not use `scale-down-first` (fresh install, no rollout); size
   the VM for development or free CPU first.
4. `flowEnv=development` is assumed acceptable for a dev site; production
   uses `production`.

## Uninstall

`deploy-flow.py <site> --uninstall` removes the `flow` release and sets the
site-agent back to `FLOW_GRPC_ENABLED=false`. It keeps the databases and
secrets the prereqs upgrade created (harmless, and removing them would mean
another prereqs upgrade with `flow.enabled=false`); the `flow` namespace
stays for the ESO-synced secrets.

## The pattern for the next add-ons

`deploy-<chart>.py <site> [--ngc|--build] [--tag T] [--uninstall] [--dry-run]`:
resolve the site the same way every nico-dev script does (both share
views), read the deployed tag to default `--tag`, get images (build loop
or NGC pull + retag), enable prerequisites through `--reuse-values`
upgrades of releases nico-dev already owns, pre-apply certificates, install
the chart, flip any consumer flags, print the endpoints. Reuse helpers from
existing modules by **import**, never by editing them.

## About "rms"

There is no RMS chart in this repository. RMS (Rack Manager Service) is an
**external** service that nico-api talks to over mTLS; NICo configures it in
the site config TOML (`[rms]` block, `[component_manager]` backends) — see
`docs/configuration/component-manager-rms.md`. The repo carries an `rms`
client crate but no server or mock (the mocks present are `mock-core`,
`mock-flow`, `bmc-mock`, `mockdpa`, `ufm-mock`). So an "rms add-on" for
nico-dev would have to be one of: a mock RMS server (to be written), or
configuration wiring toward a real RMS elsewhere. Neither is a helm-chart
add-on today; worth a separate decision.

Other candidates that **are** charts in this repo and currently disabled in
nico-dev: `nico-pxe`, `nico-dsx-exchange-consumer` (umbrella sub-charts,
`components.*: false` in the site yaml), `nico-machine-a-tron` (the MAT
chart; nico-dev runs MAT on the VM instead), observability
(`helm/observability`, `setup.sh --with-observability`), and DPF
(`docs/manuals/dpf.md`). Each fits the same one-script pattern; PXE and the
DSX consumer are umbrella values flips, observability is a separate chart
set with its own prerequisites.
