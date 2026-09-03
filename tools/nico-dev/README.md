# nico-dev — a complete Infra Controller dev environment on a Mac or Linux host

One VM — UTM on an Apple Silicon Mac, or libvirt/KVM on a Linux host —
carrying a Kubernetes
cluster, the full nico stack, a simulated FRR datacenter fabric, and the
MAT fleet simulator. Built for the laptop-only dev loop: edit on the Mac,
build on the Mac, deploy into the VM, watch machines come Ready.

The model: **your own clone and branch are first-class.** The tools live
on the `nico-dev` branch of this fork (never merged upstream) and get
grafted into *your* checkout as untracked, git-ignored files — your
branch, your PRs, and `git status` never see them. [how-to.md](how-to.md)
is the deep reference for every step, dev cycles, CLIs, troubleshooting.

## Quick start — from your clone to a running cluster

**1. A folder for the VM, and a worktree of YOUR repo in it.** Your
primary nico clone stays wherever it lives (say `~/projects/infra-controller`
— no clone yet? `git clone https://github.com/NVIDIA/infra-controller.git`
first). Each VM gets its own folder, whose `shared/` the VM will mount,
holding a worktree on the branch you'll work on:

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/projects/infra-controller
git worktree add -b vm1-work ~/nico-tests/vm1/shared/infra-controller origin/main
```

(`-b vm1-work`: your new branch — one branch per worktree, git enforces
it; `origin/main`: what to base it on — use your feature branch instead
to test existing work. Experiments are disposable:
`git worktree remove` + delete the VM.)

**2. Graft the nico-dev tools into the worktree** — one command, run
inside it:

```bash
cd ~/nico-tests/vm1/shared/infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash
```

Prefer to see the moving parts (also the cut-paste alternative)? It's
exactly this:

```bash
git fetch https://github.com/jabdulvahid/infra-controller-core.git nico-dev
git checkout FETCH_HEAD -- tools/nico-dev
git reset -q tools/nico-dev
echo 'tools/nico-dev/' >> "$(git rev-parse --git-common-dir)/info/exclude"
```

The tools land untracked and locally ignored — impossible to sweep into a
commit. Rerun `graft-tools.sh` (it's in the folder now) any time to
update them; it overwrites `tools/nico-dev` in place. By default it
fetches the **stable channel** — the newest `validated-*` tag, a state
that passed a full bring-up; `graft-tools.sh --edge` fetches the branch
tip (latest fixes, maintainer-grade risk).

**3. Tools on PATH, prerequisites checked:**

```bash
cd tools/nico-dev
export PATH="$PATH:$(pwd)"   # add to your shell profile to keep it
check-prereqs.sh             # just run the sim (NGC pre-built images)
check-prereqs.sh --build     # also build nico from source
```

Fix any ✗ (each line says how). Note: the first VM-building run pops a
macOS dialog "Terminal wants to control UTM" — click **Allow**.

**4. Describe your site.** Copy the commented example and edit the EDIT
THESE block — VM name, user/password, SSH key, dc/site names, two IP
octets — and choose ONE deploy mode:

```bash
cp devup-example.yaml devup-mysite.yaml
vi devup-mysite.yaml
```

- **NGC pre-built** (`ngc:` block: `nico_tag`, `nico_image`, `token_env`)
  — the quickest path: pulls ready-made images (core + REST, same tag)
  instead of compiling. Needs an NGC API key with registry-read on the
  image's org/team. To pick a tag:
  ```bash
  ngc-tags.py --config devup-mysite.yaml        # latest builds + dates + arm64
  ngc-tags.py --config devup-mysite.yaml --before v2.3.0   # page back
  ```
- **Source build** (`tag:`, the default when `ngc:` is absent) — builds
  **your worktree's branch** and deploys it; the full dev loop
  (20–40 min first build, minutes after).

Octets become Mac-routed prefixes — pick ones your Mac doesn't use
(VPN/LAN); dev-up preflight warns if the Mac already routes them. Do NOT
set `ip` — the VM address derives from your Mac's own UTM subnet;
`host_num` changes the last octet (default 126; two VMs can't share one).

**5. Go:**

```bash
dev-up.py --config devup-mysite.yaml
```

`--dry-run` first: it lists every preflight check (✓/✗/⚠), the numbered
plan with the exact commands, and ends with a verdict — green **READY**
plus the command to run, or red **NOT READY** with the ✗ lines to fix.
Nothing is executed. `--list` names the steps; any command-line flag
beats the config file.

**What to expect:** an early pause to set the UTM share **Path** in the
GUI (point it at `~/nico-tests/vm1/shared` — the one thing macOS won't
let a script do; instructions are printed), then: VM built from the
Ubuntu cloud image (~90 s), tools and mounts, site config, fabric,
Kubernetes, images (NGC pull, or your source build), deploy, and finally
a Mac `sudo` prompt to route the service VIPs. Done looks like a URL:
`https://<underlay>.133.1.17/admin`. NGC lane, zero compilation: **~30 min
on a Mac at the office; 13 min on a Linux host on the corp network**
(measured 2026-09-02, empty host to Done).

**If a step fails:** the runner stops, prints that step's known failure
modes, and gives the exact resume command (`dev-up.py --config ...
--from <step>`). Every step is safe to rerun.

## Linux hosts (libvirt/KVM)

Same commands, same config file, same addressing — `dev-up.py`,
`smoke-test.sh`, `check-prereqs.sh`, `build-nico-dev-vm.py` and
`dev-down.py` are platform dispatchers that run the `_linux` (or `_mac`)
implementation for your host. What differs on Linux:

- **Prereqs**: `sudo apt install libvirt-daemon-system libvirt-clients
  virtinst cloud-image-utils qemu-utils virtiofsd` + `sudo usermod -aG
  libvirt,kvm $USER` (relogin). `check-prereqs.sh --build` grades the box.
- **No GUI step**: the share path is set by `virt-install`; the run never
  pauses. Share transport is virtiofs (mounted at the same `/mnt/mac` →
  `~/mac` — the name is historical, kept on purpose). virtiofs passes real
  uids through, and the VM user is created with YOUR uid, so files the
  guest writes into the share are yours on the host (the `~/mac` view is
  mounted with `create-for-user`, so even sudo-run deploy scripts write
  as you).
- **Firewall**: a `ufw` default-deny host is fine as-is. The VM reaches
  the host registry on `192.168.64.1:5000` through Docker's own
  published-port rules, and libvirt inserts its own accept rules for the
  `nico-nat` subnet (DHCP/DNS). A `timed out` on the registry step, as
  opposed to `connection refused`, is the sign of a firewall that does
  block it: `sudo ufw route allow in on virbr-nico to any port 5000 proto tcp`.
- **Docker flavour**: Docker with the containerd image store pushes OCI
  manifests into the local registry (the "Not all multiplatform-content
  is present" Info lines are normal — only your host's arch was pulled).
- **Networking**: the builder creates a dedicated libvirt NAT network
  `nico-nat` @ `192.168.64.0/24` (bridge `virbr-nico`) — identical
  addressing to the Mac, never touching your existing networks — and a
  `nico-dev` storage pool for VM disks (system QEMU can't read `$HOME`).
  Both are created loudly and recorded; `--subnet` overrides if that
  range is taken (`check-prereqs.sh` checks).
- **CLIs without toolchains**: `build-nico-clis.py` builds `nico-admin-cli`
  and `nicocli` inside rust/golang containers on a Linux host (the ELF runs
  on the host directly), so rustup and Go are not needed on the box; the
  Mac keeps host builds because it needs Mach-O binaries.
- **Console**: `virsh console <vm>` (a getty is enabled in the seed).
- **Route**: `sudo ip route replace <underlay>.133.1.0/27 via <vm-ip>`
  (dev-up does it; not persistent across host reboots).
- **Several sites on one box**: give each a distinct `host_num`, octets,
  and `name` (default `nico-<dc>-<site>` coming in Phase 3). Everything
  created is namespaced and listed in `~/.nico-dev/vms/<vm>.yaml`.
- **Teardown**: `dev-down.py --config devup-mysite.yaml` — the one
  command: domain, its volumes, its route, its ledger entry. Your site
  folder and worktree are never deleted. `--remove-infra` drops
  `nico-nat`/pool when no nico VMs remain.

## After bring-up

- **Admin CLI, no build needed** (novice path — the binary ships inside
  the api container; it's a Linux binary for the VM's arch, so this runs
  on the VM):
  ```bash
  ssh <user>@<vm-ip>
  ~/mac/<repo>/tools/nico-dev/get-admin-cli.sh ~/mac/sites/<dc>/<site>
  ~/mac/sites/<dc>/<site>/run-admin-cli.sh version
  ```
  (extracts the binary to /usr/local/bin, issues client certs via the
  cluster's vault, and writes a VM-side run-admin-cli.sh wrapper)
- `ndev.py <share>/sites/<dc>/<site>` — site status. Run on the host it
  shows the cluster (via kubeconfig) and reports fabric/BGP/DPU as
  `n/a (VM-side)`; run it on the VM (`~/mac/sites/<dc>/<site>`) for those,
  and `fabric verify` for the full fabric health check
- **Admin UI from a headless Linux host**: tunnel from your laptop,
  `ssh -L 8443:<underlay>.133.1.17:443 <linux-host>` → `https://localhost:8443/admin`
  (if the login flow redirects to the VIP itself, use
  `sshuttle -r <linux-host> <underlay>.133.1.0/27` instead)
- The dev loop on your branch: edit → `build-dev-nico.py <site> --tag t2`
  → `redeploy-dev-nico.py <site> --tag t2` (minutes per cycle)
- Native Mac CLIs, MAT fleet runs, golden-image baking: [how-to.md](how-to.md)
- Inside the VM, `~/mac/<repo>` is a git *worktree* whose metadata lives
  on the Mac — git commands there won't work (deploys only read files;
  do your git on the Mac side)

**Friction = bug.** If a step confused you or an error message didn't
rescue you, that's a defect in this tooling — please report it.

## Maintainers

- `smoke-test.sh` — ~6-minute unattended boot-path check (throwaway VM:
  create → boot → static IP early → cloud-init done → arch → delete).
  RULE: no push touching the cloud-init seed, the VM creation record, or
  prepare-vm's guest section without a green smoke run.
- After a full bring-up validates the tip, tag it:
  `git tag validated-YYYYMMDD && git push origin validated-YYYYMMDD` —
  that's what the stable graft channel serves.
