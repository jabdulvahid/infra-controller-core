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

# feature build (from your worktree)
python3 build-nico-clis.py <site> --mat-only --skip-nicocli --repo ~/projects/nico-mat

# either way, on the VM — run-mat.sh detects the changed binary and reinstalls
~/mac/sites/<dc>/<site>/run-mat.sh
```

Baseline and feature runs use the identical launch path, so any behavior
difference is your code, not the harness. Incremental rebuilds are ~1–2 min
(warm named volumes). Logs: `sudo tail -f /var/log/machine-a-tron-<dc>.log`.
Watch progression: `run-admin-cli.sh site-explorer get-report endpoint`,
`run-admin-cli.sh machine show`, or the admin GUI at
`https://<u>.133.1.17/admin`.

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
