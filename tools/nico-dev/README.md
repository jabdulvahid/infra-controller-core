# nico-dev — a complete Infra Controller dev environment on a Mac

One UTM virtual machine on an Apple Silicon Mac, carrying a Kubernetes
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
update them; it overwrites `tools/nico-dev` in place.

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

`--dry-run` first prints the exact, preflight-validated plan without
running anything; `--list` names the steps; any command-line flag beats
the config file.

**What to expect:** an early pause to set the UTM share **Path** in the
GUI (point it at `~/nico-tests/vm1/shared` — the one thing macOS won't
let a script do; instructions are printed), then: VM built from the
Ubuntu cloud image (~90 s), tools and mounts, site config, fabric,
Kubernetes, images (NGC pull, or your source build), deploy, and finally
a Mac `sudo` prompt to route the service VIPs. Done looks like a URL:
`https://<underlay>.133.1.17/admin`. NGC lane at the office: **~30 min
total, zero compilation.**

**If a step fails:** the runner stops, prints that step's known failure
modes, and gives the exact resume command (`dev-up.py --config ...
--from <step>`). Every step is safe to rerun.

## After bring-up

- `ndev.py <share>/sites/<dc>/<site>` — site status; `fabric verify` for
  the full fabric health check (run VM-side for fabric/DPU detail)
- The dev loop on your branch: edit → `build-dev-nico-mac.py <site> --tag t2`
  → `redeploy-dev-nico.py <site> --tag t2` (minutes per cycle)
- Native Mac CLIs, MAT fleet runs, golden-image baking: [how-to.md](how-to.md)
- Inside the VM, `~/mac/<repo>` is a git *worktree* whose metadata lives
  on the Mac — git commands there won't work (deploys only read files;
  do your git on the Mac side)

**Friction = bug.** If a step confused you or an error message didn't
rescue you, that's a defect in this tooling — please report it.
