# MAT in nico-dev — Why It Runs Where It Runs, and All the Plumbing

Machine-a-tron (MAT) simulates racks of servers and DPUs — mock Redfish BMCs,
fake DHCP, PXE, discovery — against the real nico gRPC API. Getting the
*baseline* MAT running on nico-dev (2026-08-21) surfaced roughly a dozen
hidden assumptions that nico-sim never met, because on nico-sim the build
machine, the runtime machine, and the network bridge owner were all the same
Linux box (jaslinux). nico-dev splits those three roles across the Mac, a
build container, and the VM — and every seam had a surprise in it.

This doc records why the architecture is what it is, every piece of plumbing
`build-nico-clis.py` / `configure-clis.py` / `run-mat.sh` do on MAT's behalf,
and the full failure catalog with root causes — so the next person debugs by
lookup, not re-diagnosis.

---

## 1. Why MAT must run on the VM (not the Mac)

Three reasons, in decreasing order of hardness:

1. **The fabric owns the BMC subnet.** MAT doesn't just *call* the API — it
   impersonates a rack: it puts mock-BMC IP aliases on an interface and
   *listens*, and nico's site-explorer connects INBOUND to those BMCs. The
   BMC IPs come from `rack-mat-hosts` (`<u>.140.2.0/24`), which leaf-mat
   advertises into BGP and which terminates on `br-<dc>-internet` — a bridge
   that exists only inside the VM. A MAT on the Mac would hold aliases no
   fabric route points at; nico's connections would sail into the bridge and
   find nobody.
2. **MAT's plumbing is Linux-only.** It creates IP aliases with Linux
   network tooling and (in k8s mode) speaks raw UDP DHCP. macOS has none of
   that plumbing.
3. **Precedent.** MAT has only ever run on the machine owning the bridge:
   jaslinux in nico-sim, the VM here.

Rule of thumb: *admin-cli and nicocli are clients — they run anywhere with a
route in. MAT is a server wearing fabric addresses — it must live where those
addresses route to.*

The ergonomic cost is one command: `ssh nico@<vm> '~/mac/sites/<dc>/<site>/run-mat.sh'`.

## 2. How MAT is built (and why a container)

MAT must be a **Linux ARM64 (ELF)** binary. The Mac builds Mach-O. Three
approaches were tried in one evening:

| Approach | Outcome |
|---|---|
| Native `cargo build` on Mac (inherited from nico-sim) | Mach-O — can never run on the VM; only "worked" in nico-sim because jaslinux was Linux |
| `cargo-zigbuild` cross-compile | Worked in principle, but hit toolchain friction twice in the first hour (plugin MSRV vs default rustc; rustup targets are per-toolchain vs the repo's `rust-toolchain.toml` pin) — too many assumptions about the operator's Mac |
| **Build inside a `linux/arm64` container (final)** | Native Linux build on Apple Silicon, zero Mac toolchain assumptions — only prereq is colima, which nico-dev already requires |

The container build (`build-nico-clis.py`):

- Image `nico-mat-build:<version>` derived once from `rust:<version>`, where
  the version is read from **the repo's own `rust-toolchain.toml`** — the
  toolchain always matches the code, automatically.
- The derived layer adds what the workspace's build scripts need:
  `protobuf-compiler libprotobuf-dev cmake pkg-config libssl-dev`.
  (Discovered live: the plain rust image lacks `protoc` for `carbide-rpc` —
  the Mac had it installed invisibly. The container makes the dependency
  explicit and versioned.)
- Named volumes `nico-mat-target` + `nico-mat-cargo-registry` keep rebuilds
  incremental (~1–2 min after the first).
- `CARGO_PROFILE_RELEASE_DEBUG=false` (same knob as the nico image build):
  41 MB binary instead of 229 MB with debug info.
- `--repo <dir>` builds from a feature worktree instead of the site-yaml
  repo; if `<dir>/.git` is a worktree pointer file, the main clone's `.git`
  is auto-mounted so git-embedding build scripts still work.

## 3. Delivery: the share is transport, never runtime

The binary lands in `{site}/mat/machine-a-tron` — the VM sees it at
`~/mac/sites/<dc>/<site>/mat/` with no scp. But **MAT's runtime touches zero
share files**: `run-mat.sh` copies everything VM-local first. Two reasons,
both learned the hard way:

- The 9p share presents files with the **Mac owner's uid**. MAT's client
  key is 0600 — unreadable to the VM user. MAT then *silently* connects
  without its client cert → anonymous principal → HTTP 403 on its first
  real call (`get_desired_firmware_versions`), with no hint why.
  (Diagnostic from nico-sim notes that still applies: `client_num_certs=0`
  in nico-api spans = the client sent no cert.)
- Executing/reading off 9p has enough permission edge cases that "copy
  local, run local" is simply the reliable posture.

## 4. Privileges: MAT runs as root

`run-mat.sh` launches MAT under `sudo` (env vars passed via `env`, immune to
sudoers `setenv` policy). Root covers, in one honest move, everything MAT
needs:

- **bind port 443** — nico hardcodes `DEFAULT_BMC_HTTPS_PORT=443` for BMC
  mocks (`bmc_mock_port = 443` in the config is mandatory)
- **create IP aliases** on `br-<dc>-internet` (CAP_NET_ADMIN)

A `setcap cap_net_bind_service` attempt covered only the first and was
abandoned — capability whack-a-mouse loses to the nico-sim-validated model
(MAT always ran as root there).

Corollary that cost an hour: **root + `/tmp` logs don't mix.** The VM sets
`fs.protected_regular=2` (Ubuntu default hardening), which denies
`O_CREAT` on another user's file in sticky world-writable dirs — *even for
root*. MAT-as-root opening the nico-user-owned `/tmp/mat-<dc>.log` died with
a bare `Error: Os { code: 13 }` **before its logger existed** (verdict via
strace). Logs now live at **`/var/log/machine-a-tron-<dc>.log`**.

## 5. Two sets of certificates (don't conflate them)

| | Client certs (MAT → nico API) | Server certs (nico → MAT's BMC mocks) |
|---|---|---|
| Purpose | mTLS identity for gRPC calls | The mock BMCs' Redfish HTTPS listener |
| Issued by | Vault PKI (`nicoca/issue/nico-cluster`), SPIFFE URI `spiffe://nico.local/nico-system/sa/machine-a-tron` | Shipped dev certs in the repo: `crates/bmc-mock/tls.{crt,key}` |
| Provisioned by | `configure-clis.py` (reads the real root token from the `vault-init-keys` secret — the static `root` token is a dev-mode fossil) | `configure-clis.py` stages them into `{site}/mat/` |
| At runtime | `/etc/machine-a-tron/<dc>/mat-{ca,client,client-key}.pem` via `FORGE_ROOT_CA_PATH` / `CLIENT_CERT_PATH` / `CLIENT_KEY_PATH` | `/etc/machine-a-tron/<dc>/repo-root/crates/bmc-mock/` via `REPO_ROOT` |

The `REPO_ROOT` dance exists because bmc-mock resolves its server cert via
`CARGO_MANIFEST_DIR` — a **compile-time** path (`/src/crates/bmc-mock`
inside the build container) that exists nowhere at runtime — then
`/opt/carbide`, then `$REPO_ROOT`. On jaslinux the compile path *was* the
runtime path, so nobody ever noticed.

## 6. Network prerequisites (fabric side)

All handled by the deploy scripts, listed here because each was once a live
failure:

- **leaf-mat must advertise `<u>.140.2.0/24` into BGP** — it carries the
  gateway IP on its bridge-facing interface AND a `network` statement. A
  malformed statement (`/24/24`, generator double-append) meant the prefix
  never entered BGP: mocks reachable from the VM's bridge, connection
  refused from nico. Check: `ndev bgp info --detail` or
  `ip route get <u>.140.2.1` from the VM.
- **iptables must accept 443 (and 53) inbound on `br-<dc>-internet`** —
  docker's FORWARD/INPUT DROP policies otherwise eat the site-explorer's
  Redfish SYNs (nico-sim lesson, ported into the fabric deploy).
- **`nico-api.<dc>-<sitename>` must resolve on the VM** — MAT dials the API
  by hostname; `run-mat.sh` ensures the `/etc/hosts` entry (`<u>.133.1.17`).
- **The VM must own the whole BMC prefix (`<u>.140.2.0/24`)** — upstream MAT
  (post-Aug-2026 refactor) no longer adds per-machine IP aliases; it serves
  ALL mock BMCs from a single `0.0.0.0:443` listener and routes by Host
  header (machines register under their DHCP-assigned IP). Packets to the
  prefix are accepted via kernel **local routes** — invisible in `ip addr`,
  gone after a reboot. `run-mat.sh` ensures them on every launch as a
  7-CIDR set covering `.2-.255` that EXCLUDES the fabric gateway `.1`: a
  whole-/24 local route hijacks the gateway and breaks discovery-phase
  DHCP (issues 20260825-#2/#3; old binaries built before the refactor
  instead added real per-machine aliases themselves).

## 7. What run-mat.sh actually does

The script's interface is **two paths at the top** — `MAT_BIN` (the Linux
binary) and `MAT_CONFIG` (the fleet toml). Everything below the "no edits
below this line" marker is plumbing whose main job is hiding the cert
locations, so they never have to be spelled on a command line:

1. sanity: `$MAT_BIN` exists and is ELF (catches a stale Mach-O from an old
   build script)
2. installs the binary → `/usr/local/bin/machine-a-tron.<variant>` (only
   when changed; `<variant>` = `MAT_BIN`'s parent folder name, so script
   copies pointing at different builds never overwrite each other)
3. syncs client certs + the config + bmc-mock server certs →
   `/etc/machine-a-tron/<dc>/` (root-owned, 0700); rewrites `log_file` in
   the STAGED config to `/var/log/machine-a-tron-<dc><script-suffix>.log`
   (`run-mat-foo.sh` → `-foo`) so parallel variants never overwrite each
   other's logs — the source toml is untouched
4. ensures the API hostname in `/etc/hosts`
5. `sudo env FORGE_ROOT_CA_PATH=… CLIENT_CERT_PATH=… CLIENT_KEY_PATH=…
   REPO_ROOT=… <staged bin> <staged config>`

Self-locating (`$SITE` = its own directory), so the same file works from
the share path on the VM and the Mac path alike. Regenerate it any time
with `configure-clis.py` — it is fully derived from the site yaml.

## 8. Failure catalog (symptom → cause → fix location)

| Symptom | Root cause | Fixed in |
|---|---|---|
| `exec format error` / binary won't run on VM | Mach-O from a native Mac build | container build, `build-nico-clis.py` |
| `can't find crate for core` mid-build | rustup target added to the default toolchain, repo pins its own (`rust-toolchain.toml`) | obsolete — container build |
| `Could not find protoc` in container build | plain rust image lacks it; Mac had it invisibly | `nico-mat-build` derived image |
| HTTP 403, `grpc-status header missing`, on first API call | client key 0600/Mac-uid on 9p → unreadable → MAT silently certless → anonymous (only `forge/Version` allowed) | certs copied VM-local, `run-mat.sh` |
| Bare `Error: Os { code: 13 }` before any log | `fs.protected_regular=2` blocks root O_CREAT on user-owned `/tmp` file | log moved to `/var/log`, `configure-clis.py` |
| panic `Could not find the crt file for bmc-mock: NotPresent` | bmc-mock server-cert lookup via compile-time `CARGO_MANIFEST_DIR`; `REPO_ROOT` unset | server certs staged + `REPO_ROOT`, both scripts |
| mocks reachable from VM, `connection refused` from nico | MAT prefix not advertised into BGP (leaf-mat) | `generate-dev-fabric.py` |
| ALL endpoints `connection refused` (SITEEXPLORER-101), MAT healthy, single `0.0.0.0:443` listener, `ip addr` shows no BMC IPs | new-architecture MAT (no per-machine aliases) + missing kernel local route for the BMC prefix — typically after a VM reboot | `run-mat.sh` ensures the local route (20260825-#2) |
| `Vault cert issue failed:` (empty) in configure-clis | static `root` token (dev-mode fossil) + `curl -f` eating the 403 body | `configure-clis.py` |
| machines stall at discovery, PermissionDenied "interface and source IP" | `allow_insecure_discovery` placed after a TOML `[section]` → scoped into that section, not root-level | values generator (nico-sim lesson; verify in the live configmap) |

## 9. The feature-development loop (e.g. epic #3796)

```bash
# baseline (from the site yaml's repo, e.g. ~/golden/infra-controller @ main)
python3 build-nico-clis.py <site> --mat-only --skip-nicocli

# feature build (from your worktree) — --out-dir is REQUIRED with --repo, so
# the feature binary can never overwrite the baseline in {site}/mat/
python3 build-nico-clis.py <site> --mat-only --skip-nicocli --repo ~/projects/nico-mat --out-dir <site>/mat-dev

# on the VM: one run script per build (copy run-mat.sh, change MAT_BIN/MAT_CONFIG)
~/mac/sites/<dc>/<site>/run-mat.sh          # baseline
~/mac/sites/<dc>/<site>/run-mat-dev.sh      # feature
```

For a clean A/B the baseline must come from the feature branch's OWN base
commit, not from whatever the site's checkout happens to be: a detached
worktree at that commit (`git worktree add --detach <dir> <base-sha>`) built
with `--repo <dir> --out-dir <site>/mat-base` differs from the feature build
by exactly the feature's diff. Each source checkout gets its own cargo target
volume (`nico-mat-target-<id>`), so the first build per checkout is cold;
sharing one volume across checkouts reused stale crates (20260918-#1).

Baseline and feature runs use the identical launch path, so any behavior
difference is your code, not the harness. Incremental rebuilds are ~1–2 min
(warm named volumes). Logs: `sudo tail -f /var/log/machine-a-tron-<dc>.log`.
Watch progression: `run-monitor-mat.sh` (both sides on one screen, see
`clis-mat-in-nico-dev.md` §5), or by hand with `run-admin-cli.sh
site-explorer get-report endpoint`, `run-admin-cli.sh machine show`, or the
admin GUI at `https://<u>.133.1.17/admin`.

## 10. Custom builds: your own binary, the site's certs

For feature work you often want to run a **custom build** (from a repo other
than the site yaml's — e.g. a worktree) *directly*, without touching what a
typical user's site contains. The certs issued by `configure-clis.py` are
identity, not build artifacts — **reuse them as-is** for any binary you build.
The recipe is deliberately dumb: copy the run script, change two paths.

### Custom MAT

```bash
# 1. Build from your worktree — --out-dir is REQUIRED with --repo, so the
#    baseline in {site}/mat/ is never touched
python3 build-nico-clis.py <site> --mat-only --skip-nicocli \
    --repo ~/projects/nico-mat --out-dir ~/projects/nico-mat/out

# 2. Put the binary somewhere the VM can see (NOT {site}/mat/):
mkdir -p <site>/mat-dev
cp ~/projects/nico-mat/out/machine-a-tron <site>/mat-dev/

# 3. (optional) a custom config for your experiment:
cp <site>/mat/mat-config.toml <site>/mat-dev/mat-config.toml
#    edit it — e.g. add acceleration_factor / [.. .timing_overrides].
#    (No need to touch log_file: the run script derives the log name from
#    its OWN name, so variants never overwrite each other's logs.)

# 4. Copy the run script and change the TWO paths at the top:
cp <site>/run-mat.sh <site>/run-mat-dev.sh
#    MAT_BIN="$SITE/mat/machine-a-tron"       → "$SITE/mat-dev/machine-a-tron"
#    MAT_CONFIG="$SITE/mat/mat-config.toml"   → "$SITE/mat-dev/mat-config.toml"

# 5. On the VM:
~/mac/sites/<dc>/<site>/run-mat-dev.sh
```

Everything below the two paths — cert sync, /etc/hosts, REPO_ROOT, sudo —
carries over unchanged from the copied script; don't touch it. The staged
binary/config names carry the variant tag (`machine-a-tron.mat` vs
`machine-a-tron.mat-dev`), so baseline and dev never overwrite each other.
Only one MAT can run at a time anyway (port 443, the bridge aliases). To go
back to baseline, just run the original `run-mat.sh`.

### Custom admin-cli (or any client CLI)

Client CLIs run on the Mac natively — no container needed, plain cargo in
your worktree:

```bash
cd ~/projects/nico-mat && cargo build --release -p nico-admin-cli
cp <site>/run-admin-cli.sh <site>/run-admin-cli-dev.sh
#    change the last line: nico-admin-cli "$@" →
#    ~/projects/nico-mat/target/release/nico-admin-cli "$@"
```

Same certs, same API URL, your binary. (Don't `--install-to ~/.local/bin`
from a feature build — that overwrites the baseline CLI the same way
{site}/mat/ would for MAT.)

## 11. Where everything lives

| Thing | Mac | VM |
|---|---|---|
| MAT binary (delivery) | `{site}/mat/machine-a-tron` | `~/mac/sites/<dc>/<site>/mat/…` (same file) |
| MAT binary (runtime) | — | `/usr/local/bin/machine-a-tron` |
| client certs (delivery) | `{site}/certs/mat/` | same via share |
| everything (runtime) | — | `/etc/machine-a-tron/<dc>/` |
| config (source of truth) | `{site}/mat/mat-config.toml` (generated) | copied to runtime dir per run |
| logs | — | `/var/log/machine-a-tron-<dc>.log` |
| build caches | docker volumes `nico-mat-target`, `nico-mat-cargo-registry` | — |

## 12. What MAT fakes and what it does not (read before building on it)

MAT is often described as "fake machines". That is true at some layers and
false at others, and knowing which is which decides what can be built on top
of it.

**Faithful: the API and workflow layer.** MAT never writes to the database
and calls no hidden test endpoint. It drives nico's public gRPC API, and
nico's own workflows run unmodified: site-explorer, ingestion, DHCP records,
every machine state transition. A MAT machine is indistinguishable from a
real one once it exists, which is why anything that consumes nico's view of a
machine (the network config, the instance, the VPC membership) works on MAT
machines without special cases.

**Stand-in: three layers real hardware never touches.**

1. *Identity.* MAT authenticates with one client certificate, SPIFFE
   `machine-a-tron`, and the API's internal RBAC table
   (`crates/api-core/src/auth/internal_rbac_rules.rs`) grants that principal
   a union of roles no real component holds at once: the DHCP server's
   `DiscoverDhcp` and `ExpireDhcpLease`; Scout's `DiscoveryCompleted`,
   `MachineValidationCompleted`, `RebootCompleted`, `CleanupMachineCompleted`
   and `ForgeAgentControl`; the DPU agent's `RecordDpuNetworkStatus`; the
   PXE service's `GetPxeInstructions`; the admin CLI's force-deletes,
   `CreateVpc`, `SetDynamicConfig`, switch and power-shelf creation. A real
   BMC, host or DPU has no client certificate at discovery time; nico-dhcp
   and nico-pxe call the API on their behalf, and the DPU agent later gets
   its own per-DPU `Agent` identity.
2. *DHCP without a packet.* In production a BMC or DPU broadcasts, the OOB
   switch relays, nico-dhcp receives and calls `DiscoverDhcp`. MAT's
   `dhcp_relay` task calls `DiscoverDhcp` directly with a synthetic MAC,
   relay address and circuit id; the API sees a relayed request that was
   never on a wire.
3. *Inventory from templates, lifecycle without a boot.* `DiscoverMachine`
   is an anonymous RPC, because real Scout and real DPU agents call it before
   they have any identity, so the hook itself is normal. The payload is not:
   MAT posts as the Scout reporter for the host and the DpuAgent reporter for
   the DPU, with `create_machine: true` and template-generated serial, MAC
   addresses and TPM EK certificate. No PXE boot and no Scout run follow;
   MAT's per-host state machine calls the completion RPCs itself, while the
   BMC mock answers Redfish power and boot commands so nico's workflows
   proceed. Nothing boots, nothing is wiped, nothing is measured.

**The seam that matters for the datapath.** MAT fetches each machine's
network configuration with `GetManagedHostNetworkConfig`, the same call the
production DPU agent makes, and acknowledges it with `RecordDpuNetworkStatus`,
copying the configuration version back so nico sees the DPU as converged.
Between the fetch and the acknowledgement nothing is applied anywhere: the
machine has an admin address, a VPC and a VNI in the database, and no
interface, no VRF, no packets. The VPC datapath simulation
(`vpc-sim-design.md`) fills exactly that gap: same fetch, a real apply into a
per-machine FRR stand-in on the fabric, same acknowledgement. Layers 1 to 3
stay as they are; MAT remains the API actor for every machine.

One-line version: MAT is faithful at the API and workflow layer and a
stand-in at the identity, packet and physical layers.

## 13. The DPF path (the default since 2026-09-15) and the simulator

NICo provisions DPUs through DPF: the machine controller writes DPUDevice and
DPUNode resources and waits for the DPF operator to walk a DPU resource to
Ready; while the DPU is in `Rebooting` NICo power-cycles the host over
Redfish and clears the reboot annotation. In nico-dev the operator's half is
played by upstream's `dev/k8s/dpf-sim-controller`, deployed by
`deploy-dpf-sim.py` as the `dpf` bring-up step. Nothing is flashed and no
DPU OS boots; only the status transitions NICo observes are reproduced, on a
per-phase timer (`nico-system.dpf.sim.phase_dwell` in the site yaml).

What decides the path is per host: the site flag (`[dpf] enabled` in the API
config, from `dpf:` in bringup.yaml) AND the host's own DPF flag. MAT
registers every host with DPF on, so on a DPF site the whole fleet takes the
DPF path. `run-admin-cli.sh dpf disable <host>` before ingestion sends that
host down the iPXE path instead; NICo refuses it once the host was ingested
via DPF.

Three pieces have to agree, and the tooling installs them in this order
because nico-api fails hard otherwise: the DPF CRDs and the operator
namespace (before the nico release), `[dpf] enabled = true` plus the
nico-api Role on DPF resources (chart-rendered from the site yaml), and the
simulator (last; hosts only need it once they reach `dpuinit`).

| Symptom | Cause | Fix |
|---|---|---|
| nico-api crash-loops right after deploy, log says `failed to initialize DPF SDK` | `[dpf]` enabled but the CRDs or the operator namespace are missing | `deploy-dpf-sim.py <site> --crds-only`, then `kubectl -n nico-system rollout restart deployment/nico-api`. deploy-dev-nico.py does this before the release; a hand-edited TOML can bypass it |
| every host sits in `dpuinit`; DPUDevice and DPUNode exist, no DPU resource appears | no simulator, or it is not Running | `deploy-dpf-sim.py <site>`; `kubectl -n dpf-operator-system get pods` |
| a DPU parks in `Rebooting` | NICo has not cleared the reboot annotation: the host's BMC mock did not complete the power cycle | MAT log on the VM; `run-admin-cli.sh machine show <host>` |
| `dpf disable` refused | the host was ingested via DPF | `reset-mat-state.py <site> --yes`, then disable before the next ingestion |
| simulator pod Pending | CPU committed on the node | it asks for 100m / 128Mi (site yaml `nico-system.dpf.sim.resources`); free CPU or resize the VM |

The simulator and the real DPF operator must never share a cluster, since
both write `DPU.status.phase`; `deploy-dpf-sim.py` refuses when it finds the
operator. The simulator's Go types are pinned to the doca-platform release
whose CRDs ship in `crates/dpf/crds`; both come from the same checkout, so
they agree as long as the site is built from one tree.

## 14. Running the admin CLI from the VM

The admin CLI belongs on the VM for the same reason MAT does: everything it
needs is already there. The API container ships a Linux build of
`nico-admin-cli`, the API VIP lives on the fabric inside the VM, so no route
has to be added on the Mac, and the site's kubeconfig in the site folder is
enough to issue the client certificate from Vault. Nothing is compiled.

```bash
ssh nico@192.168.64.126
get-admin-cli.sh ~/mac/sites/<dc>/<site>
~/mac/sites/<dc>/<site>/run-admin-cli.sh version
```

`get-admin-cli.sh` refuses to run on the Mac, because the binary it extracts
is a Linux ELF. It does two things:

1. Picks a **running** `nico-api` pod (the label also matches the completed
   `nico-api-migrate` job pod, which cannot be exec'ed into), copies
   `/opt/carbide/nico-admin-cli` out of it with `exec` + `cat` (the container
   has no `tar`, so `kubectl cp` does not work) and installs it as
   `/usr/local/bin/nico-admin-cli`.
2. Runs `configure-clis.py <site> --admin-cli-only` on the VM. That issues the
   admin client certificate from Vault into `<site>/certs/admin/`, writes the
   wrapper `<site>/run-admin-cli.sh`, and adds `nico-api.<dc>-<site>` to the
   VM's `/etc/hosts`, pointing at the API VIP.

The wrapper is small and worth knowing. It exports `API_URL`
(`https://nico-api.<dc>-<site>:443`), `ROOT_CA_PATH`, `CLIENT_CERT_PATH` and
`CLIENT_KEY_PATH` from the site's `certs/admin/` folder and then calls
`nico-admin-cli` from `$PATH` with your arguments. Always call the wrapper:
the bare binary dials the API by its in-cluster name and fails outside the
cluster. The `version` subcommand prints `IGNORING SERVER CERT`; that is
expected, every other subcommand verifies the server against the site CA.

Two things to know about the wrapper file itself:

- **It bakes the absolute site path of the machine that generated it.**
  `configure-clis.py` writes the same file name whether it runs on the Mac or
  on the VM, and the share shows one copy to both. If you later run
  `configure-clis.py <site>` on the Mac, for MAT, the wrapper is rewritten
  with the Mac path and stops working on the VM (`machine show` says the
  certificate files do not exist). Run `get-admin-cli.sh` again on the VM, or
  `configure-clis.py <site> --admin-cli-only` there; either restores the VM
  paths in seconds. The MAT files are untouched by the `--admin-cli-only` run.
- **The binary should match the API.** After deploying a new nico tag, run
  `get-admin-cli.sh` again so the CLI is the one that shipped with that API;
  a CLI from an older image usually still works but can lack new subcommands
  or print fields the API no longer has.

Commands you will use during a MAT run:

```bash
S=~/mac/sites/<dc>/<site>
$S/run-admin-cli.sh machine show                          # the fleet, with lifecycle state (no argument = all)
$S/run-admin-cli.sh machine show <machine-id>             # one machine in full
$S/run-admin-cli.sh dpf disable <host-machine-id>         # send one host down the iPXE path (before ingestion; section 13)
$S/run-admin-cli.sh site-explorer get-report endpoint     # what the explorer has found, with its errors
```

Where things live on the VM: the binary at `/usr/local/bin/nico-admin-cli`,
the certificate and key in `<site>/certs/admin/` on the share (readable from
the Mac too; they are client credentials for the site, treat them as such),
and the wrapper at `<site>/run-admin-cli.sh`. Building your own admin CLI
from a worktree is section 10.

**nicocli, the REST CLI, follows the same idea with one difference.** The
`nico-rest-api` image ships it at `/app/nicocli`, but that image is
distroless: no shell, no `cat`, no `tar`, so nothing can be copied out of a
running pod. `get-nicocli.sh <site>` therefore runs on the Mac and takes the
binary from the image in docker's store (`docker create` + `docker cp`,
nothing runs), pulling it from the local registry first on the source-build
lane where `buildx --push` leaves no local copy. It writes
`<site>/run-nicocli.sh`, a self-locating wrapper that exports
`NICO_BASE_URL` (the REST NodePort, `http://<vm-ip>:30388`), `NICO_ORG`
(`ncx`) and `NICO_TOKEN`. The token comes from
`helm-prereqs/keycloak/get-token.sh`, which mints a client-credentials token
for the `ncx-service` client by running a curl pod inside the cluster
(Keycloak is ClusterIP only), and is cached in `<site>/.nicocli-token` for 25
minutes of its 30-minute lifetime. The wrapper always uses the site's own
kubeconfig, whatever `KUBECONFIG` the shell carries, so it works from a Mac
shell that points at another cluster. On the VM it runs the extracted Linux
binary; on a Mac it runs a `nicocli` from PATH if you built one with
`build-nico-clis.py`. `--bootstrap` runs the two `current` calls a fresh site
needs; `--refresh-token` discards the cache.
