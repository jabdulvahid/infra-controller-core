# nico-dev — Advanced User Guide: Build Your Own Test Bed

Build a nico-dev environment **from scratch** on your own Mac — no golden image.
For developers who need more than the standard golden-image setup ([how-to.md](how-to.md) §11–§12),
e.g. **multiple independent test beds** on one Mac, or full control over every layer.

Key difference from the golden-image path: **no hardcoded IP**. The VM keeps its
UTM-assigned DHCP address (or any static IP you choose) — every step below is
IP-agnostic. Where an address appears, it is either the UTM gateway
(`192.168.64.1`, identical on every Mac) or a value you substitute.

> This guide is validated step-by-step against the scripts it invokes.
> Status: **work in progress** — steps are appended as they are reviewed.

---

## Step 0 — Preparation

*(Living section — dependencies are appended here as the step-by-step validation
finds them.)*

### 0a. Supported platform

- **Apple Silicon Mac only** (M1–M4). Everything is ARM64: the UTM VM, the Nico
  images, the build toolchain. Intel Macs and other OSes are not supported by
  this guide.
- macOS 14+ recommended (UTM requires macOS 11+).
- **Guest OS:** nico-dev is validated and tested with
  `ubuntu-26.04-live-server-arm64.iso`. Other recent Ubuntu Server ARM64
  releases (e.g. 24.04 LTS) are expected to work but are not the tested path.

### 0b. Check your Mac's resources

The UTM VM alone needs 16 GB RAM / 8 CPUs / 100 GB disk, and image builds run in
a separate Linux VM (colima) sized at another 16 GB / 8 CPUs / 100 GB.

```bash
# RAM in GB (need ≥32; 48+ comfortable if you run builds and the VM concurrently)
echo $(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )) GB

# CPU cores (need ≥8; the UTM VM gets 8 and colima gets 8 — they time-share)
sysctl -n hw.ncpu

# Free disk in your home volume (need ~250 GB free:
#   UTM VM disk 100 GB + colima VM 100 GB + base images and build cache;
#   both disks are sparse — they grow toward these sizes, not allocated upfront)
df -h ~ | tail -1
```

Tight on RAM? Don't run image builds (colima) and the UTM VM at full load
simultaneously — build first, boot after, or size colima down.

### 0c. Install UTM

```bash
brew install --cask utm
```

(Alternatives: [Mac App Store](https://apps.apple.com/app/utm-virtual-machines/id1538878817)
— identical, supports the developer — or download from [mac.getutm.app](https://mac.getutm.app).)

### 0d. Container runtime — colima

This guide uses **colima** (lightweight, no license concerns, scriptable) rather
than Docker Desktop. Docker Desktop also works if you already run it — skip this
subsection, but give it comparable resources in its settings.

```bash
brew install colima docker

# Sizing matters: the default (4 CPUs / 8 GB) OOM-kills the Rust build.
# Validated sizing:
colima start --cpu 8 --memory 16 --disk 100
```

Verify:
```bash
docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
# → linux/arm64
```

> colima does not auto-start at login by default. After a reboot:
> `colima start` (it remembers the sizing). Symptom of colima not running:
> `Cannot connect to the Docker daemon`.

### 0e. CLI tools

```bash
brew install helm kubectl python3 go docker-buildx
pip3 install pyyaml
mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
# go builds nicocli (REST CLI); buildx builds the REST images
```

### 0f. Clone the repos into one shared folder

Both repos in a single folder that will be shared with the VM (e.g. `~/projects`):

```bash
mkdir -p ~/projects && cd ~/projects
git clone <nico-dev-source-remote>          # the repo containing the nico-dev folder
git clone <infra-controller-core-remote> infra-controller-core
```

> The repo folder name is flexible: an unrenamed upstream clone
> (`infra-controller`) works too — you state the name explicitly at Step 3
> (`--nico-repo-folder`), it is validated to exist, and recorded in the site
> yaml for every later script. Nothing is guessed.

### 0g. Network notes (corporate networks / VPN)

- Many corporate networks and VPNs **block port 53 to public DNS (1.1.1.1,
  8.8.8.8)**. Everything in this guide therefore uses the UTM gateway
  (`192.168.64.1`) as DNS inside the VM — already handled by the scripts, noted
  here so you don't "fix" it back to a public resolver.
- First image build downloads Rust crates and base images — expect it to be slow
  on VPN.

---

## Step 1 — Create and prepare the VM

### 1a. Create the VM in UTM

1. UTM → **+** → **Virtualize** → **Linux**, boot from the
   [Ubuntu Server ARM64](https://ubuntu.com/download/server/arm) ISO
   (validated: `ubuntu-26.04-live-server-arm64.iso` — see Step 0a)
   - **RAM:** 16 GB · **CPUs:** 8 · **Disk:** 100 GB
     (recommended sizing — the preflight warns below 16 GB/8/100 GB and asks
     whether to proceed; pushing the limits is permitted, at your own risk)
   - **Network:** Shared Network (NAT)
2. VM Settings → **Sharing** → add a shared directory:
   - **Path:** the folder containing both repos (e.g. `/Users/<you>/projects`)
   - **Name:** `share` — must be exactly this unless you pass `--share <name>` in 1c
3. Install Ubuntu: username `nico`, enable OpenSSH server when prompted.

### 1b. Find the VM's IP

Log into the VM console (UTM window) and run:

```bash
ip -4 addr show | grep 192.168.64
```

Note the address — referred to as `<vm-ip>` below. It stays stable in practice
(UTM's DHCP leases by MAC address), and nothing in this guide bakes it into the
cluster.

### 1c. Prepare the VM (one command, from the Mac)

```bash
bash ~/projects/claude-notes/nico-dev/prepare-vm.sh init \
  --vm-ip <vm-ip> \
  --vm-user nico
```

You will be prompted for the VM password about twice (remote sudo, then `ssh-copy-id`).

> **Building a golden image?** Add `--static-ip 192.168.64.126` to the command above.
> After the normal preparation, the script switches the VM to that static IP
> (auto-detecting interface/MAC/gateway, validating the netplan config before
> applying, and verifying SSH + share + DNS at the new address). A distributable
> image needs a fixed IP because kubeadm bakes the node IP into etcd, the
> apiserver manifest, kubelet.conf, and admin.conf. For a personal test bed,
> skip the flag — everything is IP-agnostic.
> Revert to DHCP at any time:
> ```bash
> ssh nico@192.168.64.126 "sudo rm /etc/netplan/99-nico-static.yaml \
>   /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg && sudo netplan apply"
> ```

What it does:
- Hardware preflight (recommends 100 GB disk / 16 GB RAM / 8 CPUs — warns and
  asks to confirm if under; `NICO_DEV_ALLOW_UNDERSIZED=1` for non-interactive runs)
- System packages, Docker (with DNS → `192.168.64.1` — corporate networks commonly
  block port 53 to public DNS; the UTM gateway relays correctly), ContainerLab, Helm, kubectl
- Expands LVM to the full disk (Ubuntu's installer under-allocates)
- Mounts the shared folder: 9p at `/mnt/mac`, bindfs at `~/mac` — both persistent in `/etc/fstab`
- Passwordless sudo for `nico`, passwordless SSH from your Mac
- TCP MSS clamp to 1200 (fixes a QEMU-NAT PMTUD blackhole that breaks large transfers)

**Note:** containerd registry configuration is deliberately NOT done here — it is
owned entirely by `deploy-dev-cp.py` (the cluster-deploy step later in this guide),
which generates, patches, and **verifies** the containerd config in one place.

After completion, log out/in once so the `docker` group takes effect:

```bash
ssh nico@<vm-ip>
```

---

## Step 2 — Validate the VM before proceeding

Nothing is deployed yet — this checkpoint costs two minutes and catches
preparation problems while they are still trivial to fix. `<vm-ip>` below is
the static IP if you used `--static-ip`, otherwise the DHCP address from 1b.

All from the **Mac**:

```bash
# 1. Passwordless SSH works (no password prompt)
ssh nico@<vm-ip> true && echo "ssh ✓"

# 2. Passwordless sudo works
ssh nico@<vm-ip> "sudo -n true" && echo "sudo ✓"

# 3. Shared folder mounted, repos visible from the VM
ssh nico@<vm-ip> "mountpoint -q /mnt/mac && ls ~/mac"
# → expect: claude-notes  infra-controller-core  (plus anything else you share)

# 4. Docker works, user is in the docker group (requires the log-out/in from 1c)
ssh nico@<vm-ip> "docker run --rm hello-world" | grep "Hello from Docker"
# This one test covers: docker daemon, group membership, DNS, internet, and the
# MSS clamp (the image pull is a large-enough transfer to hit PMTUD issues)

# 5. Resources match the recommended sizing
ssh nico@<vm-ip> "df -h / | tail -1; free -g | head -2; nproc"
# → disk ≥100G total, RAM ~15-16G, 8 CPUs
```

If you used `--static-ip`, optionally name the VM on the Mac so you can
`ssh nico@nico-dev`:

```bash
echo '192.168.64.126  nico-dev' | sudo tee -a /etc/hosts
```

If any check fails, fix it before moving on — every later step assumes all five.
(`docker run` failing with a DNS error usually means Docker's daemon.json is not
pointing at `192.168.64.1` — re-run 1c, it is idempotent.)

> **Manual netplan editing (not recommended):** the static-ip mode exists because
> hand-editing `/etc/netplan/50-cloud-init.yaml` fights the file's owner
> (cloud-init can regenerate it), requires hand-copying the per-VM MAC address
> and interface name, and a YAML typo + `netplan apply` over SSH strands the VM.
> If a VM ends up unreachable after an IP change, use the UTM console:
> `sudo rm /etc/netplan/99-nico-static.yaml && sudo netplan apply` reverts to DHCP.

---

## Step 3 — Create the site

Generates the site yaml — the single config file every later script reads
(IP prefixes, folder paths, registry address, helm values).

**Parameters** (explained here, not inline — comments after line-continuation
backslashes break the shell):

| Parameter | Meaning | Example |
|---|---|---|
| `--dc-name` / `--site-name` | Names for this test bed (bridge names, kubeconfig, helm siteConfig) | `dc1` / `dev` |
| `--underlay` | First octet for fabric/infrastructure IPs | `7` → `7.x.x.x` |
| `--overlay` | First octet for tenant/overlay IPs (must differ from underlay) | `8` → `8.x.x.x` |
| `--folder` | Where site artifacts are written (site yaml, then `fabric/`, `dev-values/`, kubeconfig). **Must be inside the shared folder** so both VM and Mac can read it | `~/mac/sites/dev` |
| `--nico-vm-folder` | Shared-folder root **as seen from the VM** (the nico repo is visible under it) | `/home/nico/mac` |
| `--nico-mac-folder` | Shared-folder root **as seen from the Mac** (the UTM share path from Step 1) | `/Users/<you>/projects` |
| `--nico-repo-folder` | nico repo dir inside the share — whatever YOUR clone is named (`infra-controller-core` fork convention, `infra-controller` upstream default). Validated to exist at site creation | `infra-controller-core` |
| `--nico-dev-folder` | nico-dev scripts dir inside the share | `claude-notes/nico-dev` |

On the **VM**:

```bash
python3 ~/mac/claude-notes/nico-dev/create-dev-site.py \
  --dc-name dc1 --site-name dev \
  --underlay 7 --overlay 8 \
  --folder ~/mac/sites/dev \
  --nico-vm-folder /home/nico/mac \
  --nico-mac-folder /Users/<you>/projects \
  --nico-repo-folder infra-controller-core \
  --nico-dev-folder claude-notes/nico-dev
```

This writes `~/mac/sites/dev/dev.yaml` (named after `--site-name`). Add
`--dry-run` to preview the derived prefixes without writing anything.

Verify:
```bash
head -30 ~/mac/sites/dev/dev.yaml
# → dc_name/sitename set, nico_vm_folder/nico_mac_folder are YOUR paths,
#   prefixes derived from your octets
```

> **Multiple test beds on one Mac:** give each test bed (each VM) its own
> `--underlay`/`--overlay` octet pair — e.g. bed 1 = 7/8, bed 2 = 9/10,
> bed 3 = 11/12 — and its own `--folder`. Distinct octets mean each bed's
> service VIPs (`<underlay>.133.1.0/27`) get their own Mac route pointing at
> that bed's VM, so the beds never collide. The registry (`192.168.64.1:5000`)
> is shared by design — images built once serve every bed.

> **Vault note:** the site is generated with `vault.mode: file` (persistent,
> auto-unsealed). Do not switch it to `dev` — in-memory Vault loses all state
> on pod restart and the API crashes with 403s after every reboot.

---

## Step 4 — Deploy the ContainerLab fabric

On the **VM**:

```bash
sudo python3 ~/mac/claude-notes/nico-dev/deploy-dev-fabric.py ~/mac/sites/dev
```

What it does (everything derived from the site yaml — no hardcoded addresses):
- Generates the topology + per-switch FRR configs into `<site>/fabric/`
- Creates the Linux bridges (`br-<dc>-cp`, `br-<dc>-internet` — `br-dc1-*` for
  the Step 3 example)
- Enables IP forwarding, NAT/MASQUERADE for the fabric's internet uplink, and
  the host-side fabric routes
- Builds the ARM64 FRR image locally (`frr-arm64:local` — upstream FRR ships no
  ARM64 image), then deploys the fabric: super-spine → spine → leaf-cp +
  leaf-mat, plus the DPU stand-in (`dpu-1`) that MetalLB will BGP-peer with
- Attaches the VM itself to the CP bridge (the VM *is* the CP node)
- Installs `nico-dev-fabric.service` so the fabric auto-recreates on every
  reboot (ordered after docker AND the share mounts)

Verify — **run this on the VM** (it inspects local Docker containers, so it
cannot run from the Mac):

```bash
python3 ~/mac/claude-notes/nico-dev/ndev.py ~/mac/sites/dev fabric verify
# → FABRIC HEALTH: ✓ HEALTHY — all containers running, all BGP Established,
#                  all loopbacks reachable, internet reachable, DNS resolving
```

Re-running the deploy is safe and is also the repair procedure: it tears down
and recreates the fabric containers (expect a brief BGP flap), heals leftover
state from failed runs (including the docker bind-mount trap where a missing
`frr.conf` got created as a directory), and refreshes the boot service.

---

## Step 5 — Deploy the Kubernetes cluster

On the **VM** (takes several minutes):

```bash
sudo python3 ~/mac/claude-notes/nico-dev/deploy-dev-cp.py ~/mac/sites/dev
```

What it does:
- Installs kubeadm/kubelet/kubectl pinned to the site yaml's k8s version
- Generates the containerd config, sets `config_path`, and **verifies** it
  (the historical source of image-pull failures — now fails loudly here
  instead of mysteriously at deploy time)
- `kubeadm init` with the **fabric address** (`7.132.1.1`) as the kubelet
  node IP — MetalLB BGP-peers from the fabric, not the UTM NAT address
- Flannel CNI, control-plane taint removed (single node), waits for Ready
- MetalLB installed and BGP-configured: VIP pool = `<underlay>.133.1.0/27`,
  peer = the `dpu-1` stand-in container
- Writes the kubeconfig into the site folder as
  `<dc>-<sitename>.kubeconfig.yaml` (Step 3 example → `dc1-dev.kubeconfig.yaml`)
  and exports `KUBECONFIG` in the VM user's `~/.bashrc`

### Verify on the VM

```bash
source ~/.bashrc
kubectl get nodes
# → <hostname>   Ready   control-plane

kubectl get pods -n kube-system      # etcd, apiserver, coredns, flannel Running
kubectl get pods -n metallb-system   # controller + speaker Running
kubectl get bgppeers -n metallb-system
# → dpu-1

# BGP session actually Established (asks the DPU stand-in switch):
python3 ~/mac/claude-notes/nico-dev/ndev.py ~/mac/sites/dev bgp info
```

### Verify from the Mac

The kubeconfig lands in the shared folder, so the Mac sees it immediately —
no copying:

```bash
# One-time: add to ~/.zshrc (adjust for your share path and site names)
export KUBECONFIG=~/projects/sites/dev/dc1-dev.kubeconfig.yaml

kubectl get nodes
# → same node, Ready — served from https://<vm-ip>:6443
```

`ndev` works from the Mac for the k8s-facing contexts:

```bash
python3 ~/projects/claude-notes/nico-dev/ndev.py ~/projects/sites/dev cluster info
```

> **Which ndev contexts work where:** `cluster` (kubectl-based) works from Mac
> or VM; `fabric`, `bgp`, and `dpu` inspect the VM's local Docker containers,
> so they are **VM-only**; `registry verify`'s catalog check works from both,
> its containerd check only on the VM.

> **Note on the API endpoint:** kubeadm advertises the VM's UTM address, so the
> kubeconfig points at `https://<vm-ip>:6443`. On a DHCP test bed this pins the
> cluster to the IP the VM had at init time — UTM's DHCP is stable per MAC in
> practice, but if the IP ever changes the cluster breaks (that is exactly why
> golden images use the static IP from Step 1c).

---

## Step 6 — Build Nico images (Mac)

On the **Mac**. First, put the nico repo on the commit you want to build —
**the `--tag` is only a label; the code is whatever the checkout points at.**

```bash
cd ~/projects/infra-controller-core
git fetch upstream
git checkout main && git merge --ff-only upstream/main
```

Make sure the checkout includes the single-instance-postgres chart fix
(issue #5095) — without it, `nico-api-migrate` fails with
`database "nico_system_nico" does not exist` on the site's 1-instance postgres:

```bash
grep -q 'synchronousMode' helm-prereqs/templates/postgresql.yaml \
  && echo "chart fix present ✓" || echo "MISSING — use a branch containing the #5095 fix"
```

Then build (note the **Mac path** `~/projects/...`, not the VM's `~/mac/...`):

```bash
python3 ~/projects/claude-notes/nico-dev/build-dev-nico-mac.py \
  ~/projects/sites/dev --tag v2.0.0
```

- **First build: 20–40 min** (full Rust workspace). Subsequent builds: ~2–5 min
  (Docker layer cache + sccache).
- The script prints the branch/commit it is building and warns if the tree has
  uncommitted changes; it starts the local registry automatically if needed.
- It also prints the final image size and **warns above 4 GB** — that means
  debug symbols crept back in (`CARGO_PROFILE_RELEASE_DEBUG` must stay `false`
  in `Dockerfile.nico-dev`; a bloated image cannot be pulled reliably over the
  UTM NAT link).
- Build sequence: `carbide-build:aarch64` (compiler) → `carbide-runtime:aarch64`
  (runtime base) → `nico:<tag>`, all pushed to `localhost:5000`.

Verify:

```bash
# From Mac — lists images/tags in the registry
python3 ~/projects/claude-notes/nico-dev/ndev.py ~/projects/sites/dev registry verify

# From VM — same, PLUS checks containerd's insecure-registry config
# (must show "✓ containerd  insecure registry configured" before Step 7)
python3 ~/mac/claude-notes/nico-dev/ndev.py ~/mac/sites/dev registry verify
```

Expected on the VM:
```
  ✓ 192.168.64.1:5000  reachable
  ✓ containerd  insecure registry configured

  Images:
    carbide-build     aarch64
    carbide-runtime   aarch64
    nico              v2.0.0
```

Also confirm the cluster is still reachable from the Mac before deploying:

```bash
python3 ~/projects/claude-notes/nico-dev/ndev.py ~/projects/sites/dev cluster info
# → node Ready, kube-system pods Running
```

> **Tag discipline for multiple test beds:** the registry is shared across all
> beds on this Mac — tag meaningfully (`v2.0.0`, `myfix-3796`), not just
> `latest`, so each bed can pin what it runs.

---

## Step 7 — Deploy the Nico stack

On the **Mac** (works from the VM too — helm/kubectl reach the cluster through
the site kubeconfig either way):

```bash
python3 ~/projects/claude-notes/nico-dev/deploy-dev-nico.py \
  ~/projects/sites/dev --tag v2.0.0
```

### What it does, in order

1. **Preflight:** verifies the registry is reachable and `nico:<tag>` exists —
   fails fast before touching the cluster.
2. **Generates all helm values** into `<site>/dev-values/` (rewritten on every
   run from the site yaml — edit the site yaml, not these files):
   `cert-manager.yaml`, `vault.yaml`, `eso.yaml`, `zalando-postgres-op.yaml`,
   `nico-prereqs.yaml`, `nico.yaml` (this one embeds the full nico-api
   siteConfig TOML — FNN, routing profiles, pools).
3. **Deploys in dependency order:**
   `local-path-provisioner` (StorageClass) → `cert-manager` → `vault` →
   `external-secrets` → `postgres-operator` → `nico-prereqs` → `nico` →
   `rest-postgres` → `keycloak` → `temporal` → `nico-rest` →
   `nico-rest-site-agent` (the REST stack is base — all four developer
   surfaces; NodePort 30388; toggle via `nico-system.rest.enabled`).
4. **Vault (file mode):** on first run, initializes Vault, stores the unseal
   key + root token in the `vault-init-keys` k8s secret, and unseals; an
   unsealer sidecar re-unseals automatically after any restart. This
   init/unseal check runs on EVERY invocation, regardless of `--skip-to`.
5. **nico-prereqs extras:** creates/adopts the `nico-system` namespace with
   helm ownership labels (waiting out a Terminating namespace if a previous
   recovery deleted it), pre-creates the SSH host key in OpenSSH format
   (helm's generator emits the wrong format), passes the vault token, then
   waits for the `vault-pki-config` job and `nico-pg-cluster-0` Ready.
6. **nico:** installs the main chart, then patches
   `allow_insecure_discovery=true` into the api configmap (dev-only relaxation).

### Verify

```bash
kubectl get pods -n nico-system
# All Running or Completed: nico-api, nico-dhcp, nico-dns-0/1, nico-ntp-0/1/2,
# nico-hardware-health, nico-ssh-console-rs, nico-api-migrate (Completed)
```

### Recovery — when a step fails

The script self-heals the common traps: stuck helm releases
(`pending-install`/`failed` — previously required manual secret deletion) are
cleared automatically before each install, and the `nico-system` namespace is
created/adopted safely.

- **Resume after a failure:** fix the cause, then re-run with
  `--skip-to <release>` — skips everything *before* that release, deploys it
  and everything after.
- **Redeploy exactly one release:** `--only <release>` (e.g.
  `--only nico-prereqs`) — everything else is skipped.
- **Do NOT delete the `nico-system` namespace to recover.** It holds the helm
  release state for BOTH `nico-prereqs` and `nico` — deleting it destroys both
  releases' history, and re-installing races against namespace termination
  (this is the exists/absent flip-flop: helm first complains the namespace
  exists without ownership, and after deletion everything fails while it is
  Terminating). If you already deleted it: just re-run with
  `--skip-to nico-prereqs` — the script waits for termination to finish and
  rebuilds from there.

---

*(Steps 8+ appended as they are validated.)*

> **Beyond the core stack:** secondary charts (nico-rest, observability,
> Keycloak, operator extras) are deliberately not built into nico-dev — see
> [deploying-extras.md](deploying-extras.md) for the documented pattern.
