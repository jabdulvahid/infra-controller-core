# nico-dev — a complete Infra Controller dev environment on a Mac

One UTM virtual machine on an Apple Silicon Mac, carrying a Kubernetes
cluster, the full nico stack, a simulated FRR datacenter fabric, and the
MAT fleet simulator. Built for the laptop-only dev loop: edit on the Mac,
build on the Mac, deploy into the VM, watch machines come Ready.

This lives on the `nico-dev` branch (never merged to main) under
`tools/nico-dev`. The scripts are the product; [how-to.md](how-to.md) is
the deep reference for every step, dev cycles, CLIs, and troubleshooting.

## Quick start — empty Mac to running cluster (~1 hour, mostly unattended)

**1. Clone into a share folder** (the folder the VM will mount — the
PARENT of your repo clones; if you're reading this you may be done).
Cloning needs only `git` — a fresh Mac offers to install it on first use:

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/nico-tests/vm1/shared
git clone -b nico-dev https://github.com/jabdulvahid/infra-controller-core.git
cd infra-controller-core/tools/nico-dev
```

**2. Check prerequisites** (Apple Silicon Mac; UTM; disk):

```bash
bash check-prereqs.sh            # just run the sim
bash check-prereqs.sh --build    # also build nico from source
```

Fix any ✗ (each line says how). Note: the first VM-building run pops a
macOS dialog "Terminal wants to control UTM" — click **Allow**.

**3. Describe your setup in a config file, then one command.** Copy the
commented example and edit — VM name, user/password, SSH key, dc/site
names, two IP octets, and your deploy mode:

```bash
cp devup-example.yaml my-vm.yaml
vi my-vm.yaml
python3 dev-up.py --config my-vm.yaml
```

Any option can still be overridden on the command line
(`--site other` beats the file); `--dry-run` prints the exact plan
without running; `--list` names the steps.

**Two deploy modes** (choose in the config):

- **NGC pre-built** (`ngc:` block with `nico_tag`, `nico_image`) — the quickest onboarding: pulls a
  ready-made image instead of compiling, cutting the 20–40 min source
  build out of the first run. Needs an NGC API key (env var named by
  `token_env`) with registry-read on the image's org/team, and the image
  repo via `nico_image` or `NICO_NGC_IMAGE`.
- **Source build** (the default) — builds nico from this very checkout
  and deploys it; the full dev loop.

The share folder and repo layout are auto-detected from where the script
lives. Do NOT set `ip` — the VM's address is derived from your Mac's own
UTM subnet (that's the point); `host_num` changes the last octet
(default 126; two VMs can't share one).

**What to expect:** an early pause to set the UTM share **Path** in the
GUI (the one thing macOS won't let a script do — instructions are
printed), then: VM built from the Ubuntu cloud image (~90 s), tools and
mounts, site config, fabric, Kubernetes, **nico image build (20–40 min
the first time)**, deploy, and finally a Mac `sudo` prompt to route the
service VIPs. Done looks like a URL: `https://<underlay>.133.1.17/admin`.

**If a step fails:** the runner stops, prints that step's known failure
modes, and gives the exact resume command (`dev-up.py --name ... --from
<step>`). Every step is safe to rerun. `dev-up.py --list` shows the steps;
each is also independently runnable — see [how-to.md](how-to.md).

## After bring-up

- `ndev.py <share>/sites/<dc>/<site>` — site status; `fabric verify` for the
  full fabric health check
- Dev cycle (rebuild + redeploy), native Mac CLIs, MAT fleet runs,
  golden-image baking: all in [how-to.md](how-to.md)

**Friction = bug.** If a step confused you or an error message didn't
rescue you, that's a defect in this tooling — please report it.
