# nico-dev — Plan of Record: Test & Release Sequence

Agreed 2026-08-18. This is the accountability checklist for producing the
distributable nico-dev golden image and validating the full developer workflow.
Update checkboxes as phases complete; deviations get recorded here with a reason.

**Principle:** the golden image is a snapshot — bake only after everything it
captures is proven. Path 2 (developer workflow) validates the system; Path 1
(golden image) distributes it; the loop closes by running Path 2 on a Path-1
clone.

---

## Phase A — Foundation on the build VM (steps 0–7)

All steps are desk-validated (code reviewed against advanced-user.md) but the
CLEAN RUN has not started — the currently running VM is a leftover from earlier
testing and does not count. Phase A = one clean run on a freshly created VM.

- [x] Step 0–2: Mac prep, VM create/prepare, static IP 192.168.64.126 ✓
      (2026-08-19 — run params: share=~/golden, repo folder=infra-controller
      (upstream name), guest=ubuntu-26.04, no LVM. Findings fixed during the
      run: preflight measured root fs not disk; advisory preflight; ssh-copy-id
      agent flood → key picker; stale known_hosts masked the IP switch)
- [x] Step 3: site created ✓ (2026-08-19 — sjc/ytl, octets 11/12, site yaml
      ~/mac/sites/dev/ytl.yaml, repo folder infra-controller. Findings fixed
      during the run: dc-name length would have overflowed bridge names —
      dc/site name validation added)
- [x] Step 4: fabric deployed + `ndev fabric verify` HEALTHY ✓ (2026-08-19 —
      all BGP Established, loopback mesh, internet+DNS. Finding fixed during
      the run: topo generator hardcoded br-dev-* bridge nodes — functional
      break for any non-dev dc-name, caught pre-deploy)
- [x] Step 5: kubeadm cluster + MetalLB BGP Established ✓ (2026-08-19 — node
      Ready v1.32.13 on 11.132.1.1, kubeconfig sjc-ytl, containerd config_path
      verified line premiered, dpu-1 3/3 established incl. MetalLB peer.
      Findings fixed during the run: exclude-from-external-load-balancers
      label not stripped (nico-sim's MetalLB-mute bug, unported); Mac-path
      hint hardcoded sites/<name>)
- [x] Step 6: images built ✓ (2026-08-21 — tag main-20260821 @ main
      10559130a, clean tree; #5095 synchronousMode fix confirmed merged.
      First live REST image build. Finding fixed during the run: rest-api
      Makefile's manifest step hardcodes amd64+arm64 → direct buildx loop,
      6 images only. Registry verified de facto: deploy preflight checked
      the manifest from the Mac, VM pulled every image.)
- [x] Step 7: full stack deployed ✓ (2026-08-21 — ALL FOUR developer
      surfaces live: 33 pods Running/Completed across nico-system(13),
      nico-rest(9), temporal(6), postgres(4), vault(1). REST API answers
      from the Mac at http://192.168.64.126:30388 (4ms, JSON); site-agent
      executing temporal inventory workflows with gRPC Ok to nico-api.
      NOTE: the REST NodePort speaks plain HTTP, not https — docs fixed.
      Findings fixed during the run — first live execution of file-mode
      vault and the whole REST path:
      vault init flag typo (-threshold); hostPath ignored fsGroup → chown
      init container; vault chart OnDelete strategy → roll stale pod;
      mat gateway octet unrewritten → gateway_within_network crashloop;
      no default StorageClass → rest-postgres PVC Pending forever;
      pg_trgm port omission → GIN migration panic; site-agent
      TEMPORAL_SUBSCRIBE_QUEUE port omission + one-shot gRPC connect
      verify ported; --skip-to/--only preflight added (user request);
      readiness-gate audit closed 3 gaps + dropped the crude 20s sleep;
      kubelet inotify exhaustion → 65536/1M sysctls.)

**PHASE A COMPLETE (2026-08-21).** Next: Phase B (B0 gate + CLIs).

## Phase B — Path 2: developer workflow on the build VM

- [x] B0. **Gate:** site yaml has `vault.mode: file` ✓ (2026-08-21 — and
      proven live in Phase A, not just configured: operator init, keys
      persisted to vault-init-keys, unseal, PKI job all exercised. The
      unsealer sidecar's reboot behavior is validated at B6.)
- [x] B1. CLIs built ✓ COMPLETE (2026-08-21 — nico-admin-cli native +
      installed; machine-a-tron via the container build, 41 MB ELF via the
      share; nicocli built AND its keycloak auth flow validated live:
      in-cluster get-token.sh (issuer-bound JWT), org=ncx from realm
      roles, provider/tenant lazily bootstrapped via the 'current' calls,
      then `vpc list` → clean empty list. Recipe in how-to §10. Findings
      fixed during the run: zigbuild abandoned for container build after
      toolchain friction; protoc missing from plain rust image;
      debug-info bloat.)
- [x] B2. CLIs configured ✓ (2026-08-21 — Vault PKI certs, mat-config,
      run scripts, /etc/hosts. Findings fixed: dev-mode 'root' token
      fossil + curl -f eating Vault's error body; VM path hint dropped
      the dc segment.)
- [x] B3. admin-cli against the live API ✓ (2026-08-21 — VIP route (beats
      corp-VPN default; sshuttle loses, documented), version + real mTLS
      calls (machine show, site-explorer, credentials set). Note:
      'version' alone proves nothing — it is anonymous-allowed.)
- [x] B4. BASELINE MAT COMPLETE ✓ (2026-08-21 — all 6 machines READY:
      2 GB200 hosts + 4 DPUs walked BMC discovery → pre-ingestion →
      DPU init → power cycle → PXE → agent → network config → READY
      against the live API; DPU↔host Redfish serial pairing worked;
      allow_insecure_discovery patch validated live (nico-sim's discovery
      trap never fired). Findings fixed during bring-up — full catalog
      in mat-in-nico-dev.md: 9p 0600 client key → silent certless 403;
      fs.protected_regular killed root's /tmp log; bmc-mock server-cert
      compile-path assumption → REPO_ROOT staging; leaf-mat /24/24
      malformed BGP network statement; root privilege model restored.)
- [x] B5. Dev cycle proof ✓ (2026-08-24 — marker log line in setup.rs →
      build --tag test1 → redeploy (first live run, clean) → marker live
      in cluster → code reverted → build main-20260824 → redeploy →
      marker gone, pods healthy, repo byte-clean. MAJOR finding
      (20260824-#2): the golden checkout had moved to new main mid-cycle,
      so redeploying the OLD tag hit nico's no-schema-downgrade guard
      (migrate crashloop) — revert model is 'going back by going
      forward'; rule + recovery documented in both how-tos.)
- [x] B6. Reboot test ✓ (2026-08-24 — FULL RECOVERY IN 93 SECONDS, zero
      manual actions: +32s apiserver up/node Ready, +63s vault AUTO-
      UNSEALED (unsealer sidecar's first real exam — the file-mode design
      proven), +93s all 51 pods Running/Completed AND the VIP/GUI path
      answering end-to-end. Transient post-boot crashloops converged on
      their own — dependency-order races, exactly as designed.)

**PHASE B COMPLETE (2026-08-24).** Phase C (golden image) is UNLOCKED.

## Phase C — Path 1: golden image

Gate: Phase B fully green. Do not bake earlier.

- [x] C1. `bake-golden-image.sh` — DONE 2026-08-26: four hard gates (pods,
      vault file-mode, fleet t0, allow_insecure_discovery) all green; MAT
      residue wiped; 25G disk after cleanup
- [x] C2. UTM Share (export) → `nico-dev-golden-bake-20260826-1.utm` —
      24 GB bundle (single qcow2 + config.plist + efi_vars). Note: the
      bundle carries the ORIGINAL share path in config.plist — importers
      MUST set their own shared directory before first boot. v2 polish:
      compressed qcow2 (`qemu-img convert -c`) should roughly halve it
- [x] C3a. Import test, warm context (vm1, stranger share) — PASS
      2026-08-26: first-boot 2m28s incl. typing (target ~5min), cluster
      healthy, kubeconfig + admin GUI working from the Mac. FIVE image
      bugs found+fixed in the iteration loop (20260826-#2/#3/#4 +
      config.plist share path + shared host keys) + VIP-route/bridge100
      forensics documented in how-to
- [x] C3b. DONE 2026-08-26 (vm2, v2 master, no reboot per ruling): the
      fixed baked scripts ran clean end-to-end (nested site layout,
      bashrc replace, step-11 finale, host-key regen). One new finding:
      clone cold-start api wedge on the patroni election window
      (20260826-#7) — pod Running-but-not-serving, VIP refused; remedy
      (rollout restart) + endpoints-first diagnosis documented in how-to.
      User ruling: vm2 testing complete; deployments/CLIs already proven
      on vm1, not retested
- [ ] C4. Demo-mode proof — DEFERRED to the C5 validation clone: start
      that import test with `colima stop` (vm2 ran with the registry up,
      though nothing was pulled — all images baked)
- [ ] C5. FINAL distributable = neutral-identity rebuild (user ruling
      2026-08-26): brand-new VM, site dc1/dev1, octets 11/12 — sjc/ytl
      names a REAL datacenter and must not ship; the baked dev.yaml also
      carries the maintainer's nico_mac_folder path. Doubles as the
      full-pipeline from-zero revalidation (create-dev-site → fabric →
      cp → deploy, warm registry). The sjc/ytl v1/v2 masters remain
      internal validation artifacts only.

## Phase D — The join: develop on the clone

- [x] D1. DONE 2026-08-26 (vm1): CLIs built from a fresh latest-main clone
      in the stranger share, configured via configure-clis, admin-cli
      version + machine show answering over mTLS
- [x] D2. DONE 2026-08-26 (vm1): built main-20260826 from the clone's own
      repo checkout → redeploy → migrate rolled the ledger forward, pods
      rolled, allow_insecure_discovery auto-re-applied (20260825-#4 fix
      verified in anger). Bonus find: wedged-image-pull troubleshooting
      entry (colima forward, containerd singleflight, 206 resume)
- [ ] D3. MAT run on the clone — deferred (user ruling: D1+D2 conclude
      vm1; MAT-on-clone rides with a later validation pass)

## Phase E — Future: web UI over ndev

Goal: a rudimentary web app that shells out to `ndev <site> <context> --json`
and renders the results in simple views. Foundations laid 2026-08-18:

- [x] Every ndev context emits `--json` (info, fabric, bgp, dpu, cluster,
      registry, and fabric verify — verify's human output moves to stderr so
      stdout is pure JSON)
- [x] JSON keys stabilized (`cluster`, not `k3s`); collector module renamed
- [x] ndev preinstalled on golden images as `/usr/local/bin/ndev` with the
      site defaulting from `/etc/nico-dev/env` (plain `ndev` works)
- [ ] Web app: framework choice, views, deployment target — not started

## Backlog — VM-side CLIs (user proposal 2026-08-26, ~half day)

`build-nico-clis.py --vm-clis`: also emit Linux/arm64 admin-cli (same
nico-mat-build container, add `-p nico-admin-cli`, warm cache) and
nicocli (`GOOS=linux GOARCH=arm64` host-Go cross-compile, no container)
into `{site}/clis-vm/`. run-admin-cli.sh becomes self-locating with a
uname branch — Mac uses PATH Mach-O, VM uses clis-vm ELF — PLUS the
run-mat-style VM-local cert staging (9p 0600 key trap applies to the
admin client key too). User story: choose CLIs on Mac, VM, or both.
Also document Mac-side CLI build prereqs (rustup, Go) in
GETTING-STARTED/§12 — containers cover images+MAT only; client CLIs are
host-built because they must be Mach-O.

Companion (user design session 2026-08-26, chosen over usermod): kill the
9p 0600 class entirely with a bindfs uid-map layer — 9p mounts raw at
/mnt/.mac-raw, bindfs --map=<mac-uid>/1000 presents it at /mnt/mac.
first-boot gains one prompt ("your Mac uid? [501]", from `id -u`) and one
fstab line. No usermod (impossible mid-first-boot anyway: nico's own
session blocks it), no reboot, adaptive per host. MAT's stage-local
posture stays (performance/reliability), but share reads become
permission-clean — unblocking VM-side CLIs reading certs directly.
Considered alternative (user, same session): bake a 'first' bootstrap
user to run first-boot, freeing nico for an INLINE usermod to the Mac
uid — mechanically simplest (no FUSE layer), but adds a second account
+ credential to the newcomer story and can't self-delete (lock instead).
Fallback if bindfs-on-9p misbehaves in practice; bindfs remains primary.

## build-nico-dev-vm.py — VALIDATED 2026-08-28 (v1 end-to-end)

**Maiden from-scratch run: 1m15s to ssh** (fresh VM, cached image),
cloud-init `done` minutes later, vda 120G / root 116G@3%, uid=502(nico)
= the Mac UID (VirtFS ownership matches on both sides — the 9p 0600
bummer is dead for tool-built VMs; the bindfs backlog stays relevant only
for pre-tool VMs like the golden lineage). Flags: --ip/--user/--password/
--ssh-key/--uid; stage-by-stage (--stage) + --dry-run; reruns idempotent.
docs: how-to §1 Option A. Replaces the manual-install §1 as primary path.

Findings enroute (registry): 20260828-#2 qemu configuration has no share-
path property (share Path = the one GUI step, script pauses for it);
#3 UTM scripting can't resize existing drives → pure-Python qcow2 header
grow (no qemu dep); #4 UTM IMPORTS (copies) source images into the bundle
at create (staging dir = rerun cache only; heal-on-rerun grows the bundle
disk when stopped). vmnet plist is root-only → sudo -n attempt, then
default + loud --ip hint.

Remaining (non-blocking): C5 first-customer run; optional --full chaining
into site/fabric/cp/nico; guest-agent channel (query ip / guest execute)
unexplored. Original proposal + spikes below.

Automate base-VM creation, killing the manual-Ubuntu-install pain of the
advanced-user path (and the 11GB-download pain: colleague's cert run took
hours; the cloud image is 900MB). Feasibility validated 2026-08-28:

- Ubuntu 26.04 arm64 CLOUD image exists: ubuntu-26.04-server-cloudimg-
  arm64.img, 900MB, installer-free, cloud-init-ready (verified via HEAD).
- UTM is scriptable: UTM.sdef has `make` ("Create a new virtual machine",
  backend+architecture+configuration), `update configuration`, a full UTM
  Configuration Suite (drives/shares), start/stop, and BONUS: `query ip` +
  guest execute/push/pull (via qemu-guest-agent, installable by cloud-init)
  — could kill static-IP guessing AND open a guest-automation channel.
- Seed ISO buildable natively: hdiutil makehybrid, CIDATA label. Only brew
  dep: qemu (qemu-img resize).

Design: download/cache cloud image → qemu-img resize (~120G sparse) →
generate cloud-init user-data (nico user, packages: docker.io python3-yaml
rsync bindfs qemu-guest-agent openssh, netplan static IP derived from the
Mac's vmnet plist — kills the foreign-subnet risk at creation, hostname)
→ hdiutil seed ISO → AppleScript: make VM (aarch64, cpu/mem, disk+seed,
VirtFS share) → start → wait for ssh → print next steps (or --full chains
into create-dev-site → fabric → cp → nico).

FIRST CUSTOMER: C5 — the neutral dc1/dev1 VM should be BUILT BY this tool
(tool validated + C5 built, one effort). Long game: scripted VM creation
enables CI-built golden images. Est. 1-2 days; residual unknown is only
AppleScript ergonomics (TCC automation grant needed once per controlling
terminal).

## Backlog — golden image lifespan & drift (user questions 2026-08-26)

Raised when a fresh clone's charts (today's main) met a baked image built
from yesterday's main. Four drift axes, with this week's live evidence:

1. **Charts vs deployed binary**: tolerable for small drift (nico parses
   with deny_unknown_fields=false — new chart config keys are ignored by
   old binaries). Breaks when a chart requires binary behavior that isn't
   there. Manifests as api startup config errors (loud) or silent
   behavioral gaps (quiet — the bad one).
2. **DB ledger vs deployed tag**: hard one-way ratchet (20260824-#2).
   Deploy ≥ ledger: fine. Deploy < ledger: migrate crashloop 'previously
   applied but is missing' — loud, and the most likely first symptom an
   unsuspecting user sees.
3. **Upstream contract drift**: the killer class — silent semantic changes
   (all observed within ONE week: #5227 alias removal, #5229 relay rename,
   #5084 prediction contract). No error, just wrong behavior. This is what
   actually bounds image lifespan.
4. Cached base/image staleness (security patches) — cosmetic for dev use.

**Honest lifespan estimate**: weeks, not months, at current upstream
velocity — recommend a monthly rebake cadence plus rebake-on-known-
breaking-change, stated in GETTING-STARTED ("image built YYYY-MM-DD;
expect a refresh monthly").

**Tool proposal — `ndev drift` (or check-drift.py)** for the informed
user's keep-or-discard decision. Shows, per site:
- deployed image tag + its git sha — DISCOVERY SOLVED for CI images
  (2026-08-27): `curl -k https://<vip>/` prints "Forge <tag-with-sha>"
  (CI builds embed version; local builds print "Forge development
  build", falling back to the tags.log polish from 20260824-#2)
- repo checkout HEAD + commits-ahead count (git rev-list <image-sha>..HEAD)
- chart drift summary (git diff --stat <image-sha>..HEAD -- helm/)
- DB ledger head (latest row of _sqlx_migrations) vs image expectations
- verdict line: ALIGNED / DRIFTED (n commits, m chart files — proceed
  with judgment) / INCOMPATIBLE (ledger ahead of image — do not deploy)

## Phase F — Config tooling (after Phase D)

- [ ] F1. `ndev api config show` / `ndev api config edit` — dump the live
      nico-api TOML from its configmap, edit with $EDITOR, validate with
      tomllib, apply, rollout-restart the pod. Prints the "helm upgrade
      overwrites manual edits" warning every time.
- **Deferred:** upstream drift detection (snapshot chart values, diff at
  redeploy). Revisit if/when nico-dev is widely adopted — too early to
  invest now (decision 2026-08-19).

## Phase G — Linux/x86 host mode — STARTED 2026-08-24

Named consumer arrived: open-sourcing nico-dev requires a Linux edition.
Pass 1 (desk port) done 2026-08-24: full tree copied to `nico-dev-linux/`,
VM/share/Mac machinery deleted (~40% of the code path), single-`nico_repo`
path model, host-arch-parametric builds (x86_64 + arm64), arch-neutral
dockerfiles + local FRR image, single-host run-mat.sh (REPO_ROOT → real
repo), how-to-linux.md written, shared known-issues.md created in both
trees. STATUS: desk-validated only — awaiting its own clean run on a Linux
host (Phase G validation).

Decision 2026-08-19: **full fork, not parameterization** — `nico-dev-mac`
and `nico-dev-linux` as independent trees. Duplication is acceptable
(AI-assisted maintenance); isolation prevents cross-platform breakage.
Operating model:
- Fork from the post-Phase-D fully validated Mac tree (known-good baseline)
- Fixes are demand-driven per platform — no proactive porting, EXCEPT
  security-class fixes, which port immediately
- Every fix appends one line to a shared `known-issues.md` (what fell,
  where fixed, "other tree likely affected — unverified") so the second
  platform's fix is a lookup, not a re-diagnosis
- Linux tree simplifications: no VM wrapper (host IS the node), no 9p/static
  IP/Mac routes, upstream FRR image, localhost registry, x86_64 dockerfiles,
  kea x86_64 path, kubectl arch. Possible payoff: CI smoke-deploys per PR.

## Phase H — VPC datapath simulation (gated: after Phases C/D)

Proposed 2026-08-22 (user feature request): make VPC creation physically
real in the sim — tenant creates a VPC, attaches machines, packets actually
flow (or are isolated) via EVPN/VXLAN through the FRR fabric, realized by a
per-host "vpc-realizer" agent consuming nico's real
get_managed_host_network_config. Full design: vpc-sim-design.md.
Key model fact (verified live): nico auto-creates an admin VPC owned by
org carbide_internal — the "before" state; org scoping is why tenant-org
REST views don't show it. Sizing: REST-in-base magnitude. Stage 2 spike:
the real forge-dpu-agent in the stand-ins (BlueField coupling risk).

## Phase I — DPF-based provisioning via dpf-sim-controller — PUBLIC-RELEASE GATE

Adopted 2026-08-26 (user ruling: "before nico-dev goes public we need to
nail down DPF"). Ordering ruling the same day: golden image first (C2-C4),
then Issue 8 (MAT epic #5364, due 2nd week of Sep), THEN this — followed by
a cheap golden v2 rebake on the validated pipeline. **nico-dev does not go
public without this phase complete.**

Design decision: use upstream's dev/k8s/dpf-sim-controller (issue #3323) —
it plays the DPF operator's half (walks DPU CRs through the authentic phase
sequence incl. the Redfish reboot round-trip against MAT mocks). The REAL
DPF operator stack (kamaji/Argo/keepalived/NGC, manuals/dpf.md) is
explicitly OUT of nico-dev scope: sim and real operator fight over
DPU.status.phase, and MAT hardcodes is_dpf_enabled=true BY DESIGN for the
sim path. Estimated 1-2 days. Reference: setup-machine-a-tron.sh Phase 4b.

- [ ] I1. site.yaml `enable_dpf` flag (default false) → values generator
      emits `[dpf] enabled = true` in the api site config (chart-managed —
      survives redeploys, unlike the 20260825-#4 patch class)
- [ ] I2. build-dev-nico.py builds dpf-sim-controller (Go, native arch)
      into the local registry
- [ ] I3. deploy-dev-nico.py Phase-4b port under the flag: DPF CRDs from
      crates/dpf/crds/ + sim Deployment + carbide-api restart ([dpf] is
      startup-only). NEVER helm-install dpf-operator alongside (CRD
      ownership collision, documented in the sim README)
- [ ] I4. Validation: MAT run to Ready via the DPF path — dpuinit walks
      DPUDevice/DPUNode → DPU CRs to Ready incl. the Rebooting round-trip
      (nico reboots hosts via Redfish against MAT mocks, exercising the
      epic #3796 realistic timings); deprecation warning GONE from the UI
- [ ] I5. Both modes validated side by side: enable_dpf=false site
      unchanged (regression run); docs (how-to + mat-in-nico-dev) updated
- [ ] I6. Golden image v2 rebake with the flag available (off by default)

## Exit criteria

- advanced-user.md covers every step above, validated against the code
- Golden image + how-to/advanced-user distributed to the team
- Known-gaps list (if any) recorded in conversations.md

## Open decisions

- **B4 blocker — MAT binary architecture: DECIDED 2026-08-21 — build in a
  Linux container (revised same day from zigbuild).** First zigbuild
  attempt hit toolchain friction twice (plugin MSRV vs default rustc;
  per-toolchain rustup targets vs the repo's 1.97.1 pin) → ruling: "we
  need a solid solution without assumptions about the build machine."
  Now: docker run rust:<version from the repo's rust-toolchain.toml>
  --platform linux/arm64 — a plain native Linux cargo build on Apple
  Silicon, zero Mac toolchain assumptions (colima is already a prereq).
  Named volumes (nico-mat-target, nico-mat-cargo-registry) keep rebuilds
  incremental. Worktree builds supported (--repo; main .git auto-mounted).
  Delivered via the share to {site}/mat/; run-mat.sh installs + setcaps
  VM-side. (b) build-on-VM rejected: competes with the 33-pod stack for
  16 GB + per-edit source sync + toolchain bloats the golden image.

## Deviations log

- **2026-08-20 — Phase A run HALTED at step 5/6 boundary (deliberate).**
  Discovery: nico-rest is entirely absent from base nico-dev (rest.enabled
  False inherited from nico-sim's scope — a fossil, not a decision). Ruling:
  nico-dev must support all four developer surfaces — nico-admin-cli,
  nico-cli, gRPC API, REST API — "these are the layers we churn daily."
  Rebuild cost is low (~10 min VM, ~30 min to cluster), so: implement
  REST-in-base now, restart the clean run after. Steps 0–5 validation
  results remain valid (scripts unchanged for those steps).

  **IMPLEMENTED same day (desk-validated, live validation on resume):**
  - rest_deploy.py — setup.sh phase-7 port: rest-postgres (ns/CA/issuer/
    dedicated temporal+keycloak DB), keycloak (reference dev IdP),
    temporal (vendored chart, kind values, cloud/site namespaces),
    nico-rest umbrella (ESO-synced DB creds on nico-pg-cluster, workflow
    workers aligned per #3081, single replicas), site-agent (cert
    pre-apply dance, persisted per-site UUID, per-site temporal ns,
    FLOW_GRPC_ENABLED=false)
  - deploy-dev-nico.py: DEPLOY_ORDER += the five REST releases, gated by
    nico-system.rest.enabled (default TRUE, incl. for old site yamls);
    --skip-to/--only/heal support
  - build-dev-nico-mac.py: Step 4b builds REST images via rest-api
    Makefile (Go, buildx, arm64-only, same registry/tag); --skip-rest
  - build-nico-clis.py: builds nicocli (make nico-cli); --skip-nicocli
  - REST API on NodePort 30388 (production parity): http://<vm-ip>:30388
  - Docs: how-to prereqs (go, docker-buildx) + §8; advanced-user 0e +
    step 7; deploying-extras.md REST section → "now part of base";
    template gains nico-system.rest.enabled
  - Flow (7h) stays OFF by default; nico-mcp remains a manual extra

  **Run #2 (restart) progress:** steps 0–2 completed CLEAN on 2026-08-20 —
  zero manual interventions (key picker, static-ip auto-verify with
  known_hosts clear, full init banner: all first-pass fixes held).
  VM: fresh, 120 GiB disk, .13 → .126.

  Steps 3–5 completed 2026-08-20/21 (sjc/ytl, octets 11/12):
  - Step 3: site created; `rest: enabled: true` confirmed in ytl.yaml
    (required a pull of a stale ~/golden/claude-notes clone that predated
    the Phase-R push — process note, not a code finding).
  - Step 4: one finding, FIXED during the run — prepare-vm's ContainerLab
    install used `bash -c "$(curl -sL …)"`; an empty curl became `bash -c ""`
    → silent success with no binary, surfacing later as FileNotFoundError
    in deploy-dev-fabric. Fix (aaff89f): clab/helm installers download to a
    file, verify non-empty, verify binary on PATH before printing ✓;
    deploy-dev-fabric preflights clab/docker/ip/iptables. clab 0.78.2
    installed manually on this VM; retry deployed clean — fabric HEALTHY,
    all BGP Established in 3s, br-sjc-* bridges, internet + DNS green.
  - Step 5: fully clean. Both run-#1 findings now appear as scripted lines:
    `config_path → /etc/containerd/certs.d (verified) ✓` and
    `exclude-from-external-load-balancers label removed ✓` (no manual
    label strip this run). Node Ready v1.32.13 @ 11.132.1.1, kubeconfig
    sjc-ytl.kubeconfig.yaml, MetalLB VIPs 11.133.1.0/27 peer 11.132.1.0.

  **Resume plan (superseded by the restart):** the prior VM's steps 0–5 artifacts are compatible
  (old site yaml defaults rest=true; ensure the exclude-from-external-
  load-balancers label was removed manually). Full restart optional —
  resume at step 6 (build now includes REST images) is equally valid.
  nicocli auth/config flow (keycloak login) is validated in Phase B1.
