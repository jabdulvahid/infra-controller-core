# nico-dev — How to Set Up a Dev Environment

Complete guide to building and using a nico-dev environment on a Mac with a UTM ARM64 VM.

**Two paths:**
- **Build a golden image** (image maintainer, §1–§10) — build from scratch, deploy Nico, export VM
- **Use a golden image** (developer, §11) — import, run first-boot.sh, done in ~5 min

---

## Prerequisites (Mac)

Install these before starting:

```bash
# Package manager
brew install helm kubectl python3

# Python YAML library
pip3 install pyyaml

# Go — builds nicocli (the REST CLI); buildx builds the REST images
brew install go docker-buildx
mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx

# Container runtime for image builds — colima (or Docker Desktop, comparably sized)
# Default colima sizing (4 CPU/8GB) OOM-kills the Rust build — validated sizing:
brew install colima docker
colima start --cpu 8 --memory 16 --disk 100
```

---

## §0 — One command: dev-up.py

The whole §1–§9 chain — empty Mac to running nico cluster — as one runner
over the unchanged unit scripts:

```bash
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/dev-up.py --name nico-dev-1
```

Steps `vm → prep → site → fabric → cp → build → registry → nico → route`
(`--list` shows them). Three interactive moments remain by design: the UTM
share **Path** (GUI, not scriptable — 20260828-#2) + Enter, the VM
password once during `prep`, and Mac `sudo` for the VIP route. On any
failure it stops, prints that step's known failure modes, and gives the
exact resume command (`--from <step>`); every unit script is idempotent,
so resuming is always safe. Site identity, octets, repo name, and image
tag are flags (`--dc/--site/--underlay/--overlay/--repo/--tag`).

The sections below are the same chain, one unit at a time — still the
reference for what each step does and how to debug it.

## §1 — Create the VM in UTM

Two paths. **Option A** (script) builds the whole base VM in one command —
900 MB download, no installer walkthrough. **Option B** is the original
manual path (validated longest; use it if A misbehaves).

### Option A — automated: `build-nico-dev-vm.py`

One-time prerequisites on the Mac (no qemu install — UTM does the disk
resize itself via its scripting interface):

- UTM installed and **launched at least once**
- An SSH public key in `~/.ssh` (`ssh-keygen` if you have none)
- Automation permission: the first run pops a macOS dialog "Terminal wants
  to control UTM" — click **Allow** (System Settings → Privacy & Security →
  Automation if you missed it)

Then:

```bash
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/build-nico-dev-vm.py \
  --name nico-dev-c5 \
  --share ~/projects
```

- `--share` is the Mac folder shared into the VM — point it at your repos
  **parent** folder (the folder *containing* your clones, not a clone).
- `--host-num` (default 126) is the last octet of the static IP. The subnet
  itself is read from **this Mac's** vmnet plist, so the IP is always
  on-subnet — no off-subnet risk on foreign Macs. (The plist is root-only;
  the script tries passwordless sudo and otherwise assumes 192.168.64 —
  pass a full `--ip` if your UTM subnet differs.)
- Personalization flags, all optional: `--user` (default `nico`),
  `--password` (default `Welcome123!`), `--ssh-key ~/.ssh/foo.pub`
  (default: first `~/.ssh/id_*.pub`), and `--uid` — which defaults to
  **your Mac UID** so VirtFS share files are owned by you on both sides
  (the classic 9p 0600-permission bummer never happens; pass `--uid 1000`
  for the Ubuntu-classic UID).
- Default footprint: 120 G sparse disk, 6 CPUs, 12 GB RAM (constants at the
  top of the script if you want the §1-manual 8/16 sizing).

What it does, in four stages (`--stage image|seed|vm|boot` stops after any
one; `--dry-run` prints the plan):

1. **image** — downloads the Ubuntu 26.04 arm64 *cloud image* (~900 MB,
   cached in `~/.cache/nico-dev` — free to re-run), copies it to
   `~/UTM-disks/<name>/<name>.qcow2` and grows its virtual size to 120 G
   (pure-Python qcow2 header patch — no qemu install needed; the guest
   root fs expands into it at first boot via growpart).
2. **seed** — writes a cloud-init NoCloud seed ISO (CIDATA): `nico` user
   (passwordless sudo, your SSH key, password `Welcome123!`), base packages
   (docker.io, python3-yaml, rsync, bindfs, qemu-guest-agent, curl), root
   growpart, and a netplan static IP `<vmnet-subnet>.126/24`.
3. **vm** — creates the UTM VM via AppleScript: qemu/aarch64, both drives
   (disk + seed ISO) attached, share mode VirtFS. **One manual step
   follows** —
   AppleScript cannot set the share *path* (UTM scripting limitation):
   UTM → VM Settings → Sharing → Path → Browse → your `--share` folder.
   The script prints this and, in a full run, pauses so you can do it
   before boot.
4. **boot** — `utmctl start`, then waits for SSH (first boot runs
   cloud-init package installs: expect ~3–5 min; 15-min budget, after which
   it points you at `utmctl attach <name>` for the serial console).

Done looks like: `Base VM ready: ssh nico@192.168.64.126`.

> **First run of the script itself?** Go stage by stage — run with
> `--stage image`, then `--stage seed`, then `--stage vm` (check the VM
> looks right in the UTM window: 2 drives, VirtFS mode; set the share
> Path), then plain (boot).
> Each stage's output is inspectable before the next acts. In particular
> the seed's `user-data` is at `~/UTM-disks/<name>/seed/user-data`.

After it finishes, continue with **§2** as usual — but note the VM is
**already static** at `.126`, so:

```bash
bash ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/prepare-vm.sh init \
  --vm-ip 192.168.64.126 --vm-user nico --share share
```

(no `--static-ip` needed — cloud-init already pinned it; passing it anyway
is harmless/idempotent). Cloud-init already handled the §2 nameserver
(gateway) and docker install; prepare-vm.sh layers the rest (ContainerLab,
helm, kubectl, share mounts, MSS clamp) idempotently.

Housekeeping: after the first successful boot you may detach the
`<name>-seed.iso` drive in UTM settings (cloud-init only consumes it once,
keyed by instance-id) — or leave it, it's inert.

### Option B — manual (original validated path)

1. Install [UTM](https://mac.getutm.app/)
2. Download [Ubuntu Server ARM64](https://ubuntu.com/download/server/arm) (`.iso`)
   — validated with `ubuntu-26.04-live-server-arm64.iso`; other recent releases
   (e.g. 24.04 LTS) should work but are not the tested path
3. In UTM: **+** → **Virtualize** → **Linux**
   - **Boot ISO:** select the downloaded Ubuntu Server ISO
   - **Architecture:** ARM64 (default on Apple Silicon)
   - **RAM:** 16384 MB (16 GB)
   - **CPUs:** 8
   - **Disk:** 100 GB
   - **Network:** Shared Network (NAT) — gives VM IP in `192.168.64.0/24`, Mac gateway `192.168.64.1`
4. **Shared Directory:** VM Settings → Sharing → enable shared directory
   - **Path:** your Mac repos folder (e.g. `/Users/yourname/projects`)
   - **Name:** `share` (must be exactly this)
5. Boot and complete the Ubuntu installer
   - Username: `nico`
   - Hostname: any (will be set by first-boot.sh)
   - Enable OpenSSH server when prompted

---

## §2 — Prepare the VM

`prepare-vm.sh init` runs from the **Mac**. It SSHes into the VM, mounts the shared
folder, installs all software, sets up passwordless SSH, and switches the VM to a
static IP — all in one command.

First, get the VM's current DHCP IP (log into the UTM console):

```bash
ip -4 addr show | grep 192.168.64
```

Then, on the Mac:

```bash
bash ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/prepare-vm.sh init \
  --vm-ip <dhcp-ip> \
  --vm-user nico \
  --share share \
  --static-ip 192.168.64.126
```

> **Why `--static-ip`?** This guide builds a VM that will be **exported as a
> golden image**, and kubeadm embeds the node IP in etcd, kube-apiserver, and
> kubelet config files — if the IP changed after cloning, the cluster would
> break on every recipient's Mac. A fixed IP baked in means every clone just
> works. The switch is fully automated (netplan override, validated before
> apply, verified after; cloud-init's own config is left untouched).
> **Building a personal test bed that will never be exported? Omit the flag**
> — everything works on the DHCP address (see advanced-user.md).

Expect the SSH connection to drop once near the end — that is the IP switch; the
script reconnects at `192.168.64.126` and verifies share/gateway/DNS automatically.

You will be prompted for the VM password **once**. After that, passwordless sudo and
SSH key auth are configured automatically.

What it installs and configures:
- Docker + DNS pointed at `192.168.64.1` (required for containers to resolve names —
  corporate networks commonly block port 53 to public DNS; the UTM gateway relays correctly)
- ContainerLab, Helm, kubectl
- System packages: `python3-yaml`, `curl`, `wget`, `git`, `bindfs`, `iptables-persistent`
- 9p shared folder mounted at `/mnt/mac` + bindfs remount at `~/mac`
- Both mounts persistent in `/etc/fstab`
- Passwordless sudo for `nico`
- SSH `PasswordAuthentication yes`
- TCP MSS clamping (fixes PMTUD issues in QEMU NAT)
- (containerd registry config is deliberately NOT done here — it is owned and
  verified by `deploy-dev-cp.py` in §5)

> Revert to DHCP at any time:
> `sudo rm /etc/netplan/99-nico-static.yaml /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg && sudo netplan apply`

After completion, **log out and back in** so the `docker` group takes effect:
```bash
ssh nico@192.168.64.126
```

---

## §3 — Create the site

On the **VM**, generate your site configuration:

```bash
# On VM
python3 ~/mac/infra-controller-core/tools/nico-dev/create-dev-site.py \
  --dc-name dc1 \
  --site-name dev \
  --underlay 7 \
  --overlay 8 \
  --folder ~/mac/sites/dev \
  --nico-vm-folder /home/nico/mac \
  --nico-mac-folder ~/projects \
  --nico-repo-folder infra-controller-core \
  --nico-dev-folder infra-controller-core/tools/nico-dev
```

> `--nico-mac-folder` is the path to the shared folder **as seen from the Mac** (not the VM).
> `--nico-vm-folder` is the path to the shared folder **as seen from the VM** (`/mnt/mac`).

This creates `~/mac/sites/dev/dev.yaml` — the single config file all scripts read.
It pre-fills IP prefixes, VM/Mac folder paths, registry address, and helm values.

Verify the site yaml:
```bash
head -30 ~/mac/sites/dev/dev.yaml
```

---

## §4 — Deploy the ContainerLab fabric

On the **VM**:

```bash
sudo python3 ~/mac/infra-controller-core/tools/nico-dev/deploy-dev-fabric.py ~/mac/sites/dev
```

This creates the virtual network topology:
- Linux bridges for each fabric segment
- FRR switch containers (super-spine, spine, leafs, DPU stand-in) via ContainerLab
- BGP sessions across the fabric
- Installs `nico-dev-fabric.service` — fabric restarts automatically on VM reboot

Verify the fabric is healthy:
```bash
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev fabric verify
```

Expected output:
```
FABRIC HEALTH: ✓ HEALTHY — all containers running, all BGP Established,
               all loopbacks reachable, internet reachable, DNS resolving
```

---

## §5 — Deploy the Kubernetes cluster

On the **VM**:

```bash
sudo python3 ~/mac/infra-controller-core/tools/nico-dev/deploy-dev-cp.py ~/mac/sites/dev
```

What it does:
- Installs `kubeadm`, `kubelet`, `kubectl` (pinned to k8s version in site yaml)
- Configures containerd (SystemdCgroup, swap disabled, kernel modules)
- Runs `kubeadm init` with the VM's fabric IP (`7.132.1.1`) as the node IP
- Installs Flannel CNI
- Removes control-plane taint so pods can schedule on the single node
- Installs MetalLB with BGP peer pointing at the DPU stand-in container
- Writes kubeconfig to `~/mac/sites/dev/dc1-dev.kubeconfig.yaml`
- Adds `KUBECONFIG` export to `~/.bashrc`

Verify:
```bash
source ~/.bashrc
kubectl get nodes
# → nico-dev   Ready   control-plane   ...

kubectl get pods -A
# All system pods (etcd, apiserver, coredns, metallb) should be Running
```

Check overall site status with ndev:
```bash
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev
```

---

## §6 — Build Nico images (Mac)

On the **Mac**, check out the version you want to build from:

```bash
cd <share>/infra-controller-core

# Sync main from upstream — the --tag below is only an image LABEL; the code
# built is whatever this checkout points at. Avoid checking out release tags:
# they may predate required chart fixes (e.g. single-instance postgres, #5095).
git fetch upstream
git checkout main && git merge --ff-only upstream/main
```

Build ARM64 images and push to the local Docker registry:

```bash
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/build-dev-nico-mac.py \
  <share>/sites/dev \
  --tag main-$(date +%Y%m%d)
```

Build sequence:
1. `carbide-build:aarch64` — Rust build container (sccache for speed)
2. `carbide-runtime:aarch64` — minimal runtime base (no fluent-bit)
3. `nico:<tag>` — all Nico binaries

> **First build:** 20–40 minutes (compiles all Rust from source).
> **Subsequent builds:** 2–5 minutes (Docker layer + sccache hit).

The Mac Docker registry is started automatically at `localhost:5000` if not already running.
From the VM it is reachable at `192.168.64.1:5000` (the Mac's UTM gateway address).

---

## §7 — Verify registry from VM

Before deploying, confirm the registry is reachable from the VM and containerd is
correctly configured to pull over HTTP:

```bash
# On VM
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev registry verify
```

Expected:
```
  ✓ 192.168.64.1:5000  reachable
  ✓ containerd  insecure registry configured

  Images:
    carbide-build     aarch64
    carbide-runtime   aarch64
    nico              v2.0.0
```

If `containerd` shows ✗:
```bash
# Fix: set config_path in containerd config
sudo sed -i "/\[plugins\.'io\.containerd\.cri\.v1\.images'\.registry\]/{n;s|config_path = ''|config_path = '/etc/containerd/certs.d'|}" \
    /etc/containerd/config.toml
sudo systemctl restart containerd
```

---

## §8 — Deploy Nico

On the **VM**:

```bash
sudo python3 ~/mac/infra-controller-core/tools/nico-dev/deploy-dev-nico.py ~/mac/sites/dev --tag v2.0.0
```

Deploys the full Nico stack in dependency order:
`local-path-provisioner` → `cert-manager` → `vault` → `external-secrets` →
`postgres-operator` → `nico-prereqs` → `nico` →
**`rest-postgres` → `keycloak` → `temporal` → `nico-rest` → `nico-rest-site-agent`**

The REST stack is a **base feature** — all four developer surfaces work out of
the box (nico-admin-cli, nicocli, gRPC API, REST API). Toggle with
`nico-system.rest.enabled` in the site yaml. The REST API is exposed on
NodePort **30388** (production parity).

Vault is initialised and unsealed automatically on first deploy.

Verify all pods are running:
```bash
kubectl get pods -n nico-system
# All pods should be Running or Completed

kubectl get pods -n nico-rest
kubectl get pods -n temporal
# REST + Temporal pods Running; REST API answers at http://<vm-ip>:30388
```

### Quick smoke test — the admin GUI (no CLIs or certs needed)

The fastest end-to-end check of a new site: nico-api serves an admin web UI
at `/admin` on its external VIP. Loading it from the Mac proves the API pod,
the MetalLB VIP advertisement, and the fabric path in one shot — before any
CLI setup.

```bash
# On Mac — route the VIP range via the VM (re-add after each Mac reboot)
sudo route -n add -net 7.133.1.0/27 192.168.64.126
```

Then open **https://7.133.1.17/admin** in a browser. The certificate is
self-signed — accept the browser warning. The VIP is
`<underlay-octet>.133.1.17` (this guide's example uses octet 7); the root
path answering `Forge development build` in plain text is healthy, not an
error — the GUI lives under `/admin`.

> **Corp-VPN note:** VIP octets like 7.x or 11.x are publicly routable
> address space, so a corporate VPN's default route silently swallows them —
> and VPN clients can grab the traffic before tunneling tools (sshuttle was
> tried and lost this fight). The specific /27 route above beats the VPN's
> default because the most-specific route wins. Re-add it after reboots or
> VPN reconnects; symptom of a missing route is a silent timeout.

> **The route also dies whenever the LAST UTM VM stops** — UTM's shared
> network is a macOS bridge interface (`bridge100`) created with the first
> running VM and destroyed with the last; macOS flushes every route through
> a disappearing interface. Verified live 2026-08-26 (golden-image export:
> VM shutdown at 11:10:21 → `configd: Process interface detaching:
> bridge100` → route gone). So: reboots, VPN reconnects, AND stop-all-VMs
> events all clear it.
>
> **Debugging a hanging GUI/VIP, in order:**
> ```bash
> # 1. Where does the Mac route the VIP now? gateway=<corp/default> ⇒ route lost
> route -n get <u>.133.1.17
> # 2. When did the bridge (and route) die? Each 'interface detach' = a stop-all-VMs event
> log show --last 8h --predicate 'process == "configd" AND composedMessage CONTAINS "bridge100"' | grep -i detach
> # 3. Watch route events live while reproducing (stop the last VM in another window)
> route -n monitor
> # 4. Fix
> sudo route -n add -net <u>.133.1.0/27 <vm-ip>
> ```

Route not an option? Zero-setup fallback that skips MetalLB and tests only
the API pod:

```bash
kubectl port-forward -n nico-system svc/nico-api-external 8443:443
# then browse https://localhost:8443/admin
```

---

## §9 — Mac setup (one-time)

### Set KUBECONFIG

The kubeconfig was written to the shared folder during §5. Point `kubectl` at it:

```bash
# Add to ~/.zshrc or ~/.bash_profile on Mac
export KUBECONFIG=<share>/sites/dev/dc1-dev.kubeconfig.yaml

# Verify
kubectl get nodes
# → nico-dev   Ready   ...
```

### Add route to Nico service VIPs

Nico service VIPs (API, DHCP, DNS, SSH console) live inside the ContainerLab fabric.
The Mac needs a static route to reach them through the VM:

```bash
# On Mac (re-run after network changes or sleep/wake)
sudo route -n add -net 7.133.1.0/27 192.168.64.126

# Verify
netstat -rn | grep 7.133
# → 7.133.1/27   192.168.64.126   UGSc   bridge100

# Test
ping -c 1 7.133.1.17    # Nico API VIP
```

> This route is **not persistent** — macOS drops it on WiFi reconnect or sleep/wake.
> Re-add it each session. Symptom of missing route: `traceroute 7.133.1.17` exits
> via your default gateway instead of `192.168.64.126`.

---

## §10 — Build and configure CLIs

```bash
# On Mac — build nico-admin-cli and machine-a-tron (~5-10 min first run)
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/build-nico-clis.py \
  <share>/sites/dev \
  --install-to ~/.local/bin

# Configure certificates and generate run scripts
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/configure-clis.py <share>/sites/dev

# Test
<share>/sites/dev/run-admin-cli.sh version
```

`configure-clis.py` does:
1. Fetches the site CA from the `nico-roots` k8s secret
2. Issues an admin-cli client cert from Vault PKI
3. Issues a MAT client cert from Vault PKI
4. Generates `run-admin-cli.sh` and `run-mat.sh` in the site folder
5. Adds `7.133.1.17 nico-api.dc1-dev` to `/etc/hosts`

> **MAT** must run on the VM — it binds to `br-dev-internet` which only exists inside the VM.
> Full MAT architecture, plumbing, and failure catalog: [mat-in-nico-dev.md](mat-in-nico-dev.md).

### nicocli — the REST-surface CLI (validated recipe)

nicocli talks to the REST API (NodePort 30388, plain HTTP) with a Keycloak
bearer token. The token MUST be minted by the in-cluster helper — its JWT
issuer must match the internal Keycloak URL, so a port-forwarded token is
rejected:

```bash
export KUBECONFIG=<site>/<dc>-<site>.kubeconfig.yaml
TOKEN=$(bash <nico-repo>/helm-prereqs/keycloak/get-token.sh)

# org is 'ncx' in the reference dev realm (realm roles encode org:role).
# FIRST call ever on a fresh deploy: the two 'current' calls lazily
# bootstrap the org's Infrastructure Provider + Tenant objects — until
# then every other call 403s with "org doesn't have a ... associated".
nicocli --base-url http://<vm-ip>:30388 --token "$TOKEN" --org ncx \
    infrastructure-provider current
nicocli --base-url http://<vm-ip>:30388 --token "$TOKEN" --org ncx \
    tenant current

# then anything works:
nicocli --base-url http://<vm-ip>:30388 --token "$TOKEN" --org ncx vpc list
```

Persistent setup instead of flags: `nicocli init` → edit `~/.nico/config.yaml`
(`api.base`, `api.org: ncx`, auth section) — see `rest-api/cli/README.md`.

---

## §11 — Golden Image: Create and Distribute

For the **image maintainer**. Run this after §8 is complete and all pods are healthy.

### Bake the image

```bash
# On VM, as root — pull latest scripts first
git -C ~/mac/infra-controller-core pull

sudo bash ~/mac/infra-controller-core/tools/nico-dev/bake-golden-image.sh ~/mac/sites/dev/dev.yaml
```

What it does:
1. Saves canonical site yaml to `/etc/nico-dev/dev.yaml` (VM-local)
2. Copies entire nico-dev script bundle to `/usr/local/lib/nico-dev/` and adds to PATH
3. Clears `/etc/nico-dev/env` (first-boot.sh writes it fresh per user)
4. Resets `nico` password to `Welcome123!`
5. Clears SSH `authorized_keys`
6. Enables SSH password authentication
7. **Cleans disk:** Docker build cache, apt cache, journal logs, bash history
8. Verifies all Nico pods are Running and images are cached in containerd

### Export from UTM

In UTM: right-click the VM → **Export** → save as `nico-dev-golden.utm`.

Share the `.utm` file with developers.

---

## §12A — Golden Image: Baking and Exporting (maintainer side)

Producing the image developers receive. Full protocol with rationale:
phase-c-runbook.md. (Rough section — cleanup pending.)

### 1. Preconditions

- All nico-system pods Running/Completed; vault in file mode
- MAT stopped, fleet at t0: `reset-mat-state.py <site> --yes`
- The bake script enforces all of this with hard gates and refuses otherwise

### 2. Bake (on the VM)

```bash
sudo bash ~/mac/infra-controller-core/tools/nico-dev/bake-golden-image.sh ~/mac/sites/<dc>/<site>/<site>.yaml
```

Saves the canonical yaml + script bundle into the image, resets the nico
user for distribution (Welcome123!), wipes MAT runtime residue (staged
binaries, YOUR client certs, logs), cleans disk, then runs the gates.

### 3. Export (on the Mac)

1. Shut the VM down (`sudo shutdown -h now`) — export cold.
2. **Remove the shared directory in the VM's UTM settings** — it is baked
   into config.plist otherwise (importer footgun + your path leaked).
   Re-add it after the export.
3. Right-click the VM → **Share…** (UTM's export) →
   `nico-dev-golden-YYYYMMDD.utm`. It exports a bundle (a folder Finder
   shows as one file): one qcow2 + config.plist (~24 GB as of 2026-08-26).

### 4. Treat the export as a pristine master

Never boot the master. Give each test/import its own copy — instant and
free on APFS (copy-on-write):

```bash
cp -cR nico-dev-golden-YYYYMMDD.utm ~/Downloads/nico-test/vm1/vm1.utm
```

### 4b. Returning the builder VM to developer mode

Baking converts the VM from developer-ready to image-ready; booting it
again post-bake comes up FABRIC-LESS (env cleared → the boot service
deploys nothing; presents as "metallb broken" — 20260826-#6). To use the
builder for development again:

```bash
# VM: restore the site env + fabric
echo 'NICO_DEV_SITE=/home/<user>/mac/sites/<dc>/<site>/<site>.yaml' | sudo tee /etc/nico-dev/env
sudo systemctl restart nico-dev-fabric
# Mac: restore passwordless ssh (bake cleared authorized_keys; pw Welcome123!)
ssh-copy-id nico@<vm-ip>
# Mac: the VIP route (dies with bridge100 whenever all VMs stop)
sudo route -n add -net <u>.133.1.0/27 <vm-ip>
```

MAT staging self-heals on the next run-mat launch; everything else the
bake changed is benign for development.

### 5. Import-test rules (learned the hard way, 2026-08-26)

- **One nico-dev VM booted at a time** — they all carry the same static IP.
- Booting a clone mutates it; first-boot personalizes it. Fresh test =
  fresh copy of the master.
- **Stopping the last UTM VM destroys `bridge100` and macOS silently
  flushes the VIP route with it** — every export cycle triggers this.
  Debugging sequence: see the Corp-VPN note under the admin-GUI smoke test.

---

## §12 — Golden Image: New User Setup

For **developers receiving the golden image**. Takes ~5 minutes.

> **Audience switch:** unlike §1–11, the reader here has done NONE of the
> earlier steps and may have a blank Mac. The standalone version of this
> section — including the tiered requirements table (UTM-only to run;
> colima/docker/kubectl/helm to develop; rustup/Go for Mac CLIs; NGC key
> for pre-built images) — is **GETTING-STARTED.md**, which is what you
> actually ship alongside the image. This section remains the maintainer's
> reference copy.

### 1. Import into UTM

Open UTM → **+** → **Import** → select `nico-dev-golden-YYYYMMDD.utm`
(or just double-click the bundle).

### 2. Set shared directory BEFORE booting

- VM Settings → **Sharing** → change the shared directory path to **your** Mac repos folder
  (e.g. `/Users/yourname/projects`)
- Share name must be `share`

> The image has a static IP (`192.168.64.126`) baked in. No MAC address regeneration needed.

### 3. Boot and SSH in

Boot the VM, then from your Mac terminal:

```bash
ssh nico@192.168.64.126
# Password: Welcome123!
```

### 4. Run first-boot.sh

```bash
sudo bash /usr/local/lib/nico-dev/first-boot.sh
```

You will be prompted for four things:

| Prompt | Example |
|---|---|
| SSH public key (optional) | paste from `cat ~/.ssh/id_ed25519.pub` on Mac |
| VM hostname | `nico-dev` |
| UTM share name | `share` |
| Mac repos folder | `/Users/yourname/projects` |

The script then automatically:
- Sets the hostname
- Installs your SSH public key
- Mounts the Mac share at `/mnt/mac` and `~/mac` (persistent in `/etc/fstab`)
- Copies the site yaml to the share with your folder paths
- Writes the kubeconfig to the share
- Copies the nico-dev scripts to the share
- Restarts the fabric service → Nico cluster starts from cached images (~2 min)
- Writes `~/POST-SETUP.txt` with your personalized Mac-side commands
  (route, KUBECONFIG, CLI build) — the summary below scrolls away, that
  file does not
- Regenerates this clone's SSH host keys — your NEXT ssh warns about a
  changed host key; clear with `ssh-keygen -R 192.168.64.126` on the Mac

Verify:
```bash
kubectl get pods -n nico-system
# All pods Running or Completed
```

> **First-boot expectation:** the cold start elects a database leader;
> pods (nico-api especially) may restart for a few minutes. If the GUI
> VIP still refuses connections after everything shows Running, see
> Troubleshooting → "VIP connection refused on a fresh clone" — one
> `kubectl rollout restart deployment/nico-api -n nico-system` fixes it.

### 5. Mac setup (same as §9 and §10)

```bash
# KUBECONFIG (nested sites/<dc>/<site> convention; exact line is in ~/POST-SETUP.txt on the VM)
export KUBECONFIG=<share>/sites/<dc>/<site>/<dc>-<site>.kubeconfig.yaml
kubectl get nodes

# Route to Nico VIPs
sudo route -n add -net 7.133.1.0/27 192.168.64.126

# CLIs — the nico-dev scripts live in YOUR share (first-boot copied them
# there); the maintainer's own checkout does not exist for you
python3 <your-share>/nico-dev/build-nico-clis.py \
  <your-share>/sites/<dc>/<site> --install-to ~/.local/bin
python3 <your-share>/nico-dev/configure-clis.py <your-share>/sites/<dc>/<site>
<your-share>/sites/<dc>/<site>/run-admin-cli.sh version
```

---

## Day-to-day dev workflow

After initial setup, the typical code-change cycle:

```bash
# 1. Build new image on Mac (fast — uses layer + sccache)
python3 ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev/build-dev-nico-mac.py \
  <share>/sites/dev --tag myfix

# 2. Roll out to running cluster (on VM)
sudo python3 ~/mac/infra-controller-core/tools/nico-dev/redeploy-dev-nico.py \
  ~/mac/sites/dev --tag myfix

# 3. Watch pods roll
kubectl get pods -n nico-system -w
```

`redeploy-dev-nico.py` does a helm upgrade updating only `global.image.tag` — no
values regeneration, no prerequisite redeployment.

Revert to the last stable tag:
```bash
sudo python3 ~/mac/infra-controller-core/tools/nico-dev/redeploy-dev-nico.py \
  ~/mac/sites/dev --tag v2.0.0
```

### How to revert safely — the no-downgrade rule (20260824-#2)

> nico's revert model is **"going back by going forward"** — you never
> redeploy an older binary over a newer database; you rebuild the old code
> state as a NEW tag and deploy that.

Redeploying an **older** tag only works if it was built from the **same repo
checkout lineage** as the tag currently running. Each deploy's migrate job
advances the database's migration ledger, and nico refuses to run an older
binary against a newer ledger — there is NO schema downgrade.

Practical rules:

- **Within one dev cycle, don't move the checkout.** Build the baseline tag
  and your test tags from the same commit; then reverting to the baseline
  tag is always safe.
- **If the checkout moved** (you pulled main between builds), revert by
  REBUILDING a fresh baseline from the *current* checkout and deploying
  that — never by redeploying a pre-pull tag:
  ```bash
  git -C <repo> checkout -- <files-you-changed>       # revert the code
  python3 build-dev-nico-mac.py <site> --tag main-$(date +%Y%m%d)
  python3 redeploy-dev-nico.py  <site> --tag main-$(date +%Y%m%d)
  ```

### If the nico-api-migrate pod crashloops after a redeploy

Symptom: `nico-api-migrate-*` in CrashLoopBackOff with
`migration <id> was previously applied but is missing in the resolved
migrations` — you deployed a binary older than the DB's migration history
(the exact situation above).

Recovery (roll FORWARD):
```bash
# 1. stop the stuck redeploy (Ctrl-C) and clear the crashlooper
kubectl delete job nico-api-migrate -n nico-system

# 2. revert your code changes in the repo (not the binary)
git -C <repo> checkout -- <files-you-changed>

# 3. build + deploy a fresh tag from the CURRENT checkout
python3 build-dev-nico-mac.py <site> --tag main-$(date +%Y%m%d)
python3 redeploy-dev-nico.py  <site> --tag main-$(date +%Y%m%d)
```

---

## Deploying pre-built NGC images (no source build)

**Validated end-to-end 2026-08-27** (vm2, tag v2.2.0-pr-441-gc594e35f3):
pull → retag → push → deploy, migrate no-op via the sha-match trick,
pods rolled to the CI-built image. The run also survived a mid-upgrade
host-disk-full kernel panic (20260827-#1) — helm recovered the
interrupted release on rerun.


**One-command path** (creates the local registry if missing, logs in,
pulls arm64, retags, pushes, deploys):

```bash
python3 <share>/nico-dev/deploy-nico-from-ngc.py <site> <ngc-tag> \
    --token-env NGC_API_TOKEN_DSX_CARBIDE_DEV
# fresh site (first deploy): add --initial
```

The manual steps below are what it automates, kept for reference and
debugging:


For developers who deploy released nico images from NGC (public GA or a
private cache) instead of building from source. nico-dev needs no
changes — the whole core stack shares ONE image (`global.image`), so an
NGC image just enters the local registry under the `nico` name:

**Where NVIDIA's own CI publishes** (from .github/workflows — promotion
copies dev → prod):

| Purpose | Registry path |
|---|---|
| Dev/nightly builds (track main) | `nvcr.io/<org>/<team>` (ask your team for the org/team names) |
| Promoted releases | `nvcr.io/<org>/<promoted-team>` |

Core image name in both: **`nvmetal-carbide`** (the single shared image the
umbrella chart's `global.image` expects; naming predates the forge→carbide→
nico renames). REST images: `nico-rest-*` alongside. Both registries are
private — and NGC keys are **per-org AND per-role**: the key must be
minted for your org with the **team's**
registry-read role (verified 2026-08-26; a key from another org/role —
e.g. a plain DSX service key — authenticates fine but gets UNAUTHORIZED
on these repos). Practical notes from the live test: the promoted
`carbide` repo holds only stale 0.0.1-rc pipeline artifacts — use
`carbide-dev`, which is tagged per PR merge as `v2.2.0-pr-<N>-g<sha>`.
The tags/list JSON sorts LEXICALLY (pr-99 after pr-441) — pick the
newest by PR number, or best of all match a tag's `g<sha>` to your
repo checkout (`git rev-parse --short=9 HEAD`): a sha-matched tag has
an identical migration ledger and deploys with no downgrade risk. Simplest access test: `docker login` + `docker pull` —
docker handles the registry's Bearer-token dance automatically. Raw
`curl -u` against `/v2/...` always 401s; listing tags needs the two-step
(realm `https://nvcr.io/proxy_auth`):

```bash
TOK=$(curl -s -u '$oauthtoken:'"$NGC_API_KEY" \
  "https://nvcr.io/proxy_auth?scope=repository:<org>/<team>/<image>:pull" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))')
curl -s -H "Authorization: Bearer $TOK" \
  https://nvcr.io/v2/<org>/<team>/<image>/tags/list
```

Prefer a recent DEV tag: your site's DB ledger likely tracks main, and the
no-downgrade rule below refuses anything older.

```bash
# 0. Private cache only — NGC auth (GA images pull anonymously)
docker login nvcr.io -u '$oauthtoken' --password-stdin <<< "$NGC_API_KEY"

# Pre-flight: the version MUST publish arm64 (Apple Silicon VM)
docker manifest inspect nvcr.io/<org>/<team>/<image>:<version> | grep architecture

# Pull → retag into the local registry → push
NGC=nvcr.io/<org>/<team>/<image>:<version>
TAG=ngc-<version>
docker pull --platform linux/arm64 "$NGC"
docker tag "$NGC" localhost:5000/nico:"$TAG"
docker push  localhost:5000/nico:"$TAG"

# Deploy — the VM pulls it as 192.168.64.1:5000/nico:<tag>
python3 <share>/nico-dev/redeploy-dev-nico.py <site> --tag "$TAG"
```

Caveats:

- **No-downgrade rule applies (20260824-#2)**: a released version is
  usually OLDER than a site built from main — its migrate job will refuse
  against a newer DB ledger and crashloop. NGC deploys fit fresh sites
  (initial `deploy-dev-nico.py` with the NGC tag on a virgin database) or
  upgrades, never rollbacks.
- **Chart skew**: helm charts come from your repo checkout; for a release
  image, check out the matching release tag first. Newer charts usually
  tolerate older binaries (unknown config fields ignored) but it is not
  guaranteed.
- Only the core `nico` image is covered here; the six REST images follow
  the same pattern (`localhost:5000/nico-rest-<name>:<tag>`) if you deploy
  REST from NGC too.

## Reboot behaviour

After a VM reboot, everything comes back automatically:

1. `nico-dev-fabric.service` → recreates bridges + ContainerLab containers
2. `kubelet` → k8s cluster restarts from etcd state
3. Nico pods → start using cached containerd images (`imagePullPolicy: IfNotPresent`)

No manual action needed. Verify with:
```bash
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev
```

---

## Deploying secondary charts (REST, observability, Keycloak, …)

nico-dev keeps the core deploy minimal by design. For everything the nico repo
ships beyond the core stack — the nico-rest family, the observability stack,
Keycloak, operator extras, and the RMS-is-not-a-chart clarification — see
[deploying-extras.md](deploying-extras.md) for the generic helm-against-the-
site-kubeconfig pattern and the full catalog.

---

## Learning the fabric (explore BGP/EVPN hands-on)

One of nico-dev's purposes is learning how a real datacenter fabric works.
Every switch is a live FRR router you can shell into — and the whole fabric is
a **safe sandbox**: configs are regenerated from the site yaml on every deploy,
so you can break anything and heal it with one command.

> **New to routing?** Start with [networking-primer.md](networking-primer.md) —
> it assumes only "IP, netmask, router IP" and builds up to how the BGP fabric
> and NAT border work, with try-it commands against your live VM and a
> symptom→question debugging table.

```bash
# On the VM — list the switches
ndev fabric shell

# Shell into one (drops you into vtysh, FRR's CLI)
ndev fabric shell spine-1
```

Starter commands inside vtysh:

```
show bgp summary                  # who peers with whom, session state
show ip route                     # the underlay routing table
show ip bgp neighbors             # per-peer detail (timers, prefixes)
show bgp l2vpn evpn               # the EVPN overlay routes (type-2/3/5)
show running-config               # the switch's full FRR config
ping 7.129.0.1                    # loopback of another switch (underlay reachability)
```

Experiment freely — enter `configure terminal`, shut down a BGP neighbor,
watch `ndev fabric verify` turn red, then heal everything:

```bash
sudo systemctl restart nico-dev-fabric   # rebuilds the fabric from the site yaml
```

(A VM reboot does the same. Nothing you do inside the switches persists.)

---

## ndev quick reference

`ndev.py` is the site inspection tool. Run from Mac or VM.

```bash
# Overall status
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev

# Fabric
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev fabric verify
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev fabric info

# BGP
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev bgp info
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev bgp info --detail

# Cluster (aliases: k8s, k3s)
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev cluster info

# Registry — list images/tags, verify containerd config (on VM)
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev registry verify

# DPU stand-in
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev dpu info
```

---

## Script reference

| Script | Where to run | Purpose |
|---|---|---|
| `prepare-vm.sh init` | Mac | Install all VM software via SSH |
| `create-dev-site.py` | VM | Generate site yaml |
| `deploy-dev-fabric.py` | VM (sudo) | Create ContainerLab fabric + boot service |
| `deploy-dev-cp.py` | VM (sudo) | kubeadm cluster + MetalLB BGP |
| `build-dev-nico-mac.py` | Mac | Build ARM64 Nico images + push to registry |
| `deploy-dev-nico.py` | VM (sudo) | Full Nico helm deploy (first time) |
| `redeploy-dev-nico.py` | VM (sudo) | Rolling update to new image tag |
| `build-nico-clis.py` | Mac | Build nico-admin-cli and machine-a-tron |
| `configure-clis.py` | Mac | Issue certs and generate run scripts |
| `bake-golden-image.sh` | VM (sudo) | Prepare VM for golden image export |
| `first-boot.sh` | VM (sudo) | Personalise a cloned golden image (once per user) |
| `ndev.py` | Mac or VM | Site status and verification tool |

---

## Troubleshooting

### VIP/GUI connection refused on a fresh clone — but pods look Running

Seen 2026-08-26 (vm2 first boot). A clone's cold start runs postgres
through a patroni leader election; nico-api connects during the window,
its WorkLockManager pool rejects the read-only sessions, and the pod can
sit **1/1 Running without actually serving** — so the api VIP refuses
connections (a LoadBalancer with no ready endpoints), which looks
exactly like fabric/metallb breakage. It is not. Diagnosis order:

```bash
# 1. FIRST: does the api service have endpoints? none = api not serving (NOT fabric)
kubectl get endpoints -n nico-system | grep api
# 2. Confirm the signature in the api log
kubectl logs -n nico-system deploy/nico-api --tail=10 | grep -i "read-only\|writable"
# 3. Confirm postgres has settled (Leader / running)
kubectl exec -n postgres nico-pg-cluster-0 -c postgres -- patronictl list
# 4. Remedy: fresh connection pool
kubectl rollout restart deployment/nico-api -n nico-system
# VIP answers ~30s later:  curl -k https://<u>.133.1.17  → "Forge development build"
```

### Image pull stuck: pod Pending, single "Pulling" event, no error

Seen 2026-08-26 (vm1, first pull of a new multi-GB nico tag). The blob
stream through colima's ssh port-forward wedged half-dead: connection
ESTABLISHED, no bytes, no error — and containerd singleflights pulls per
image ref, so every retry (kubelet's AND a manual `crictl pull`) silently
attaches to the same stuck operation. Diagnosis + fix ladder, in order:

```bash
# 1. Is data flowing? (Mac) — zero recent blob GETs = wedged, not slow
docker logs --since 2m registry 2>&1 | grep -c "GET /v2/nico/blobs"
# 2. Rule out disk (VM) — a full disk stalls pulls silently too
df -h / && kubectl describe node <node> | grep -iA2 pressure
# 3. Cheapest kill first (Mac) — fixes a server-side wedge
docker restart registry
# 4. Still nothing after ~2 min? The wedge is client-side in containerd's
#    pull op. Restart is safe: running containers are NOT affected.
sudo systemctl restart containerd
sudo crictl pull 192.168.64.1:5000/nico:<tag>
# containerd RESUMES the layer (registry logs show 206 Partial Content) —
# partial download is kept, restart costs seconds
```

Note: a helm --wait deploy running during the stall will time out; re-run
redeploy-dev-nico.py afterward (also re-applies allow_insecure_discovery,
which the timed-out run never reached).



**VM not reachable at 192.168.64.126:**
```bash
# From VM console — check IP is set
ip addr show enp0s1
# Should show 192.168.64.126/24

# Check netplan config
cat /etc/netplan/99-nico-static.yaml
sudo netplan apply
```

**Fabric not healthy after reboot:**
```bash
sudo systemctl status nico-dev-fabric
sudo systemctl restart nico-dev-fabric
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev fabric verify
```

**BGP not Established:**
```bash
# Check FRR logs
docker logs clab-dev-super-spine
# Check BGP from inside container
docker exec clab-dev-spine-1 vtysh -c "show bgp summary"
```

**Image pull fails (http: server gave HTTP response to HTTPS client):**
```bash
# Verify and fix
python3 ~/mac/infra-controller-core/tools/nico-dev/ndev.py ~/mac/sites/dev registry verify

# If containerd shows ✗:
sudo sed -i "/\[plugins\.'io\.containerd\.cri\.v1\.images'\.registry\]/{n;s|config_path = ''|config_path = '/etc/containerd/certs.d'|}" \
    /etc/containerd/config.toml
sudo systemctl restart containerd
```

**Mac registry not running:**
```bash
# On Mac
docker ps | grep registry
# If not running, start it:
docker run -d -p 5000:5000 --restart always --name registry registry:2
```

**Cluster not accessible from Mac:**
```bash
# Verify kubeconfig points to correct IP
grep server <share>/sites/dev/dc1-dev.kubeconfig.yaml
# → https://192.168.64.126:6443

# Test
kubectl get nodes
```

**Route to Nico VIPs missing (Mac):**
```bash
netstat -rn | grep 7.133
# If empty:
sudo route -n add -net 7.133.1.0/27 192.168.64.126
```

**Vault sealed after cluster restart:**
The vault-unsealer sidecar unseals automatically within ~10s. If it stays
sealed, re-running any deploy resolves it (vault init/unseal state is checked
on every run, regardless of --skip-to):
```bash
python3 ~/mac/infra-controller-core/tools/nico-dev/deploy-dev-nico.py \
  ~/mac/sites/dev --skip-to nico
```

**Helm release stuck (pending-install):**
```bash
kubectl -n nico-system delete secret \
  -l owner=helm,name=nico,status=pending-install
```
