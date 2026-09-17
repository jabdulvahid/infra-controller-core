# nico-dev on a Mac — how to

nico-dev gives you a complete NICo development environment inside one virtual
machine on an Apple Silicon Mac. The virtual machine runs under UTM and
contains four things: a Kubernetes cluster, the NICo software stack, a
simulated datacenter network fabric built from FRR routers, and MAT, the
simulator that plays a rack of servers for NICo to manage. When the bring-up
is finished you have a working NICo site that you can develop against, break,
and rebuild.

This page is the current guide for Macs. The older `how-to.md` is the full
historical reference; look there when a step here does not explain enough.
Intel Macs are not supported.

**How to read this page.** Sections 1 and 2 apply to everybody. Section 3
asks one question, which of two workflows you are on, and tells you which
sections to read next. From section 7 on, the page applies to everybody
again.

A few words used throughout:

- **The share** is a folder on your Mac that the VM mounts. Files you put
  there are visible inside the VM, and files the VM writes there are visible
  on the Mac. Everything nico-dev creates lives in the share.
- **A site** is one deployed NICo installation, named by a datacenter name
  and a site name, for example `dc1/dev1`.
- **A lane** is where the NICo container images come from: pulled pre-built
  from NVIDIA's NGC registry, or built from your own source checkout.
- **A VIP** is a virtual IP address inside the VM that a NICo service
  answers on. Your Mac reaches them through one route, described in section 7.

---

## 1. Mac prerequisites

Install the tools with Homebrew, start colima, and make sure you have an SSH
key:

```bash
brew install --cask utm            # then launch it once
brew install git python3 helm kubectl colima docker
pip3 install pyyaml
colima start --cpu 8 --memory 16 --disk 100    # default 4 CPU / 8 GB OOM-kills the Rust build
ssh-keygen -t ed25519              # if you have no keypair
```

**What each tool is for.** UTM runs the virtual machine. colima provides a
Docker engine on the Mac, and docker is its command-line client. Together
they build container images and run a small local image registry that the
VM pulls from. helm and kubectl talk to the Kubernetes cluster inside the VM.
python3 with pyyaml runs the nico-dev scripts. The SSH key is installed into
the VM so the scripts can log in without a password.

**The toolchain rule: containers yes, compilers no.** You do not need Rust,
cargo or Go on your Mac. Every build that nico-dev performs, the NICo images
and the MAT binary, runs inside a container on colima. There is one
exception, which you can ignore on a first run: if you want to compile the
two command-line clients `nico-admin-cli` and `nicocli` on the Mac itself,
those must be native Mac binaries and therefore need cargo and Go installed
on the Mac. Section 9 shows the alternative, which fetches a ready-made admin
CLI from inside the cluster and needs no compiler at all.

**How big a machine you need.** Choose the row that matches what you plan to
do:

| Purpose | Mac RAM | VM | Notes |
|---|---|---|---|
| build-and-play: golden image or NGC lane, no redeploys | 16 GB | 6 CPUs, 8 GB | the running stack uses about 5 GB |
| development: source builds, redeploy cycles, MAT | 32 GB+ | 8+ CPUs, 16 GB+ | the UTM VM and colima time-share the cores |

Disk: about 250 GB free for the development tier. The UTM disk is 100 GB,
the colima disk is 100 GB, and images and caches take the rest. Both disks
are sparse, so they only consume what is actually written.

Run one nico-dev VM at a time on a Mac. Every nico-dev VM uses the same
address, `192.168.64.126`, unless you change it in the configuration.

**For the NGC lane only**, you need an NGC API key with read access to the
registry of the organisation and team that publishes the NICo images. Put it
in an environment variable:

```bash
export NGC_API_KEY='...'     # in your shell profile
```

## 2. Folder, worktree, tools

Your main clone of the NICo repository stays where it already is. For each
VM you create one folder. Inside it, `shared/` is the share the VM mounts,
and inside the share you place a git worktree on the branch you want to run.
A worktree is a second checkout of the same repository, so it shares history
with your main clone but has its own files:

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/projects/infra-controller     # no clone yet? git clone https://github.com/NVIDIA/infra-controller.git
git fetch origin main              # so the worktree starts from TODAY's main, not the last fetch
git worktree add -b vm1-work ~/nico-tests/vm1/shared/infra-controller origin/main
```

The fetch matters: the NICo images you deploy are built from current main,
and the Helm charts come from this worktree, so both should be from the same
day. A worktree cut from a stale local copy of main pairs old charts with new
images.

Next, add the nico-dev tools to that worktree. This is called grafting. The
tools land in `tools/nico-dev` as untracked, git-ignored files, so they never
show up in `git status`, in your commits, or in your pull requests. Run the
same command again whenever you want to update them:

```bash
cd ~/nico-tests/vm1/shared/infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash -s -- --edge
```

The `--edge` flag fetches the newest version of the tools. Without it you
get the stable channel, which is the newest `validated-*` tag. As of
2026-09-10 the stable tag is older than several features this page relies
on: `onboard-golden.sh`, the non-interactive `first-boot.sh`, the `ngc:`
configuration block, `restart-ordered.sh`, and the DPF support. Use `--edge`
until a newer tag exists.

Then put the tools on your `PATH` and run the prerequisite check:

```bash
cd tools/nico-dev
export PATH="$PATH:$(pwd)"   # in your shell profile
check-prereqs.sh             # run-the-sim tier
check-prereqs.sh --build     # also the source-build tier
```

The check only reads; it changes nothing. Fix every line marked ✗. Each
line says how. The first time a script drives UTM, macOS shows the dialog
"Terminal wants to control UTM". Click Allow.

The last section of the check, "checkout parity", is about the repository
rather than your Mac. The nico-dev scripts re-implement parts of the
upstream setup and therefore assume certain chart paths, resource names and
defaults in the checkout. `check-parity.py` verifies all of them, so an
upstream change that would break or silently divert a bring-up shows up
here. A ✗ in that section means a nico-dev script needs updating, not your
repository; report it.

## 3. Which workflow are you on?

There are two ways to get a site. They share nothing until the site is
running, so pick one and read only its sections.

| | Workflow A: golden image | Workflow B: build your own VM |
|---|---|---|
| You have | a `nico-dev-golden-YYYYMMDD.utm.zip` from a colleague | nothing but this repository and, for the NGC lane, an NGC key |
| What happens | the ZIP is a ready-made VM with a site already deployed inside; one script imports it, personalises it and opens the admin UI | a VM is created, Kubernetes and the fabric are installed, NICo is deployed from images |
| Where the images come from | already inside the VM | **NGC lane**: pulled pre-built from NGC. **Source-build lane**: built from your worktree |
| You write | nothing | one `bringup-<site>.yaml` |
| Time | minutes, nothing to build | NGC lane about 30 minutes; source-build lane adds a 20 to 40 minute first build |
| Read | section 4, then continue at section 7 | sections 5 and 6, then continue at section 7 |

The two workflows do not mix. The script `onboard-golden.sh` belongs to
Workflow A and never reads a bringup yaml. The script `bring-up.py` and the
bringup yaml belong to Workflow B and never touch a golden ZIP. Within
Workflow B, the two lanes differ in one block of the bringup yaml and one
build step. Everything else is identical.

From section 7 onward the page applies to both workflows. A running site
looks the same however it got there.

**Already have a nico-dev VM on this Mac?** Tear it down first, following
section 15, or give the new one its own folder, VM name and `host_num`.
Two VMs cannot share the folder `~/nico-tests/vm1` or the address
`192.168.64.126`, and this page assumes a Mac without one.

## 4. Workflow A: golden image, three commands

This section is for Workflow A only. If you are building your own VM, skip
to section 5.

You received `nico-dev-golden-YYYYMMDD.utm.zip`. It is a complete VM with a
running site inside. These commands turn it into a working site on your Mac:

```bash
mkdir -p ~/nico-tests/vm1/shared && cd ~/nico-tests/vm1/shared
git clone https://github.com/NVIDIA/infra-controller.git     # the folder name must stay infra-controller
cd infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash -s -- --edge
tools/nico-dev/onboard-golden.sh --zip ~/Downloads/nico-dev-golden-YYYYMMDD.utm.zip --dest ~/nico-tests/vm1
```

The script runs without your help, except for two things macOS insists on.
The first time, macOS asks for an Accessibility and Automation permission,
because the script drives UTM's user interface to set the shared folder.
Near the end, it asks for your `sudo` password once, to add the route to the
service addresses. When it finishes, the admin UI is open in your browser,
and the script has printed the kubeconfig path and the route command you
will need again later.

The ZIP is kept. To start over, stop and delete the VM in UTM, remove
`~/nico-tests/vm1/*.utm`, and run the same command again with the same
`--zip`.

**Manual fallback.** If the scripted shared-folder step does not stick, do
these steps by hand:

1. In UTM, click **+**, then **Import**, and choose the `.utm` bundle.
2. Open the VM's settings, go to **Sharing**, and set the path to
   `~/nico-tests/vm1/shared` with the share name `share`. Do this before the
   first boot.
3. Boot the VM. Then log in with `ssh nico@192.168.64.126`, password
   `Welcome123!`.
4. On the VM, run the personalisation script:
   ```bash
   sudo bash /usr/local/lib/nico-dev/first-boot.sh --ssh-key ~/.ssh/id_ed25519.pub \
       --mac-folder /Users/<you>/nico-tests/vm1/shared --yes
   ```
   `--mac-folder` is the share root, not the repository inside it. The script
   never changes the hostname, because the hostname is the Kubernetes node
   name. Expect `metallb-speaker` and `nico-api` to restart once at the end.
   That is the script doing its job, not a fault.
5. Back on the Mac, run `ssh-keygen -R 192.168.64.126`. The imported VM
   generated new SSH host keys, so the old entry must go. Then set up the
   route and `KUBECONFIG` as described in section 7.

Continue at section 7. Sections 5 and 6 are not for you.

## 5. Workflow B: describe the site

This section is for Workflow B only, both lanes. Golden-image users have
nothing to do here and should continue at section 7.

Everything the bring-up needs to know goes into one file. Copy the example
and edit it:

```bash
cp bringup-example.yaml bringup-mysite.yaml
vi bringup-mysite.yaml
```

```yaml
name: nico-vm1
user: nico
password: Welcome123!
ssh_key: ~/.ssh/id_ed25519.pub

vm:
  cpus: 8
  mem_mb: 16384
  disk_gb: 120

redeploy:
  on_insufficient_cpu: scale-down-first   # lets a rolling redeploy proceed on a tight node

dpf: true           # default: DPF provisioning + the DPF simulator; false = legacy iPXE path

dc: dc1
site: dev1
underlay: 11        # first octets of the fabric prefixes: pick two your Mac and VPN do not use
overlay: 12

# NGC lane: enable this block. Source-build lane: leave it out and set tag: instead.
ngc:
  registry: nvcr.io/<org>/<team>       # ask your team
  tag: <tag>                           # default tag for every image; ngc-tags.py, below
  # tags:                              # optional: a different tag for one image group
  #   rest: <tag>                      #   core = the NICo core image, rest = the six REST
  #                                    #   images; unnamed groups use tag
  core_image: nvmetal-carbide          # NGC's name for the core image
  # images:                            # optional: NGC names, if NGC publishes an image
  #   rest: {nico-rest-api: <ngc name>}  #   under another name (local chart name: NGC name)
  token_env: NGC_API_KEY               # NAME of the env var holding your key
# tag: main-20260910                   # source-build lane: the image label
```

What the fields mean:

- `name`, `user`, `password`, `ssh_key`: the VM's name in UTM, the login
  account created inside it, and the public key that account will accept.
- `vm`: the VM's size. Use the table in section 1 to choose.
- `redeploy`: what to do when a later redeploy cannot fit a new pod on a
  full node. `scale-down-first` lets it proceed. Section 10 has the details.
- `dpf`: `true` is the default and gives you NICo's DPF provisioning path
  together with the DPF simulator. `false` gives the older iPXE path.
  Section 9 explains both.
- `dc`, `site`: the names of your datacenter and site. They appear in
  folder names and in the cluster.
- `underlay`, `overlay`: two numbers that become the first octet of every
  network prefix inside the VM. Pick two numbers that nothing on your Mac or
  VPN uses. With `underlay: 11` the admin UI ends up at `11.133.1.17`.
- `ngc`: present for the NGC lane, absent for the source-build lane. `tag`
  is the default for every base image. The optional `tags` map gives one
  image group a different tag: `core` is the NICo core image, `rest` the six
  REST images. Each group is one Helm release, so a tag applies to a whole
  group. Groups you do not name use `tag`. For the source-build lane, set the
  top-level `tag` instead; it is only a label for the images you build, and
  both groups share it. Optional charts such as Flow are not configured
  here; each has its own file, see section 11.
- `core_image` and `images`: the names NGC publishes under. The names the
  Helm charts expect in the local registry are fixed; only the core differs
  on NGC, `nvmetal-carbide` against `nico` locally. If NGC ever publishes a
  REST image under a new name, map it in `images` as local name to NGC name,
  and nothing else changes.

Do not set an `ip` field. The VM's address comes from UTM's own subnet. If
you need a different last octet, set `host_num`; the default is 126. The
bring-up's preflight warns you if your Mac already routes the octets you
picked.

**Choosing an NGC tag.** The `ngc-tags.py` script lists tags that are
deployable, that is, recent builds that track main and are published for
arm64. By default it lists the core image; `--group rest` or `--group flow`
lists that group instead, for filling in `tags`:

```bash
ngc-tags.py --config bringup-mysite.yaml                 # newest PR builds tracking main, arm64 availability
ngc-tags.py --config bringup-mysite.yaml --before v2.3.0
ngc-tags.py --config bringup-mysite.yaml --group rest    # tags of the REST images
```

Prefer a recent development tag. A fresh site's database schema follows
main, and the migration job refuses to run an older version against a newer
schema, so an old tag on a new site can fail. The image must be published
for arm64.

## 6. Workflow B: bring the site up

This section is for Workflow B only, both lanes.

Run the dry run first, then the real thing:

```bash
bring-up.py --config bringup-mysite.yaml --dry-run   # preflight ✓/✗/⚠, numbered plan, READY or NOT READY
bring-up.py --config bringup-mysite.yaml
```

The dry run checks every prerequisite and marks each ✓, ✗ or ⚠. It prints
the numbered plan with the exact commands, and ends with a verdict: READY, or
NOT READY with the lines to fix. Nothing is executed during a dry run.

The real run goes through these steps in order: `vm`, `prep`, `site`,
`fabric`, `cp`, `build` (source lane only), `registry`, `nico`, `dpf`,
`route`. It builds the VM, prepares it, writes the site configuration,
deploys the network fabric, installs Kubernetes, gets the images ready,
deploys NICo, deploys the DPF simulator, and finally routes the service
addresses on your Mac.

The run stops and waits for you three times. This is by design:

1. **The UTM share path.** UTM does not let a script set the shared folder
   of a VM. The runner pauses, tells you to open the VM's Sharing settings
   and point them at `~/nico-tests/vm1/shared`, and waits for you to press
   Enter.
2. **The VM password, once.** During `prep`, before your SSH key has been
   installed.
3. **`sudo` on the Mac**, at the end, for the route to the service addresses.

If a step fails, the runner prints the known failure modes of that step and
the exact command to resume, `--from <step>`. Every step is safe to run
again. A successful run ends with a URL, for example
`https://11.133.1.17/admin` for `underlay: 11`.

## 7. Both workflows: Mac-side setup and where things are

Point kubectl at the cluster and add the route to the service addresses:

```bash
export KUBECONFIG=~/nico-tests/vm1/shared/sites/dc1/dev1/dc1-dev1.kubeconfig.yaml   # in your shell profile
kubectl get nodes

sudo route -n add -net 11.133.1.0/27 192.168.64.126     # route to the service VIPs
route -n get 11.133.1.17                                # gateway: 192.168.64.126
```

**The route does not last.** macOS removes it whenever the last UTM VM
stops, because UTM's network bridge `bridge100` disappears and macOS flushes
every route through it. The route also goes on sleep and wake, and when a
VPN connects or disconnects. Re-add it with the same command. The scripts
`restart-ordered.sh` and `onboard-golden.sh` print the command for you.

**Where things are**, seen from both sides:

| | Mac | Inside the VM |
|---|---|---|
| share root | `~/nico-tests/vm1/shared` | `~/mac` |
| repo worktree | `<share>/infra-controller` | `~/mac/infra-controller` |
| site folder | `<share>/sites/dc1/dev1` | `~/mac/sites/dc1/dev1` |
| image tarballs in transit | `<site>/images/*.tar` (deleted after import) | same path under `~/mac` |
| kubeconfig | `<site>/dc1-dev1.kubeconfig.yaml` | same path under `~/mac` |
| local registry | `localhost:5000` (colima) | `192.168.64.1:5000` |

Inside the VM the repository is a git worktree whose metadata lives on the
Mac. Do your git work on the Mac, not in the VM.

**Checking on the site.** `ndev.py` shows status. Run it on the Mac for
cluster status, or on the VM for the full fabric health, because the fabric
only exists inside the VM:

```bash
ndev.py <site>                                                       # Mac: cluster status; fabric/BGP/DPU n/a (VM-side)
ssh nico@192.168.64.126 'ndev.py ~/mac/sites/dc1/dev1 fabric verify' # VM: full fabric health
```

The subcommands are `fabric verify|info|shell [switch]`, `bgp info
[--detail]`, `cluster info`, `registry verify`, and `dpu info`.

## 8. Verify

```bash
kubectl -n nico-system get pods        # all Running or Completed
curl -k https://11.133.1.17/           # "Forge development build"
open https://11.133.1.17/admin
```

On a corporate VPN, the VPN client may claim the address range before your
route does. Then tunnel through SSH instead: run
`ssh -L 8443:11.133.1.17:443 nico@192.168.64.126` and open
`https://localhost:8443/admin`.

## 9. CLIs and MAT

The full step-by-step, with what to expect at each stage, is
`clis-mat-in-nico-dev.md`. This section is the summary.

**The admin CLI, without compiling anything.** The NICo API container
already contains the admin CLI binary. This script copies it out, issues the
client certificates it needs, and writes a wrapper script. Run it on the VM:

```bash
ssh nico@192.168.64.126
get-admin-cli.sh ~/mac/sites/dc1/dev1      # extracts the binary from the API container, issues certs, writes the wrapper
~/mac/sites/dc1/dev1/run-admin-cli.sh version
```

Always use the wrapper `run-admin-cli.sh`. The bare binary dials the API by
its in-cluster name and fails outside the cluster.

**MAT.** MAT is built in a container on the Mac and delivered to the VM
through the share. Two commands on the Mac:

```bash
build-nico-clis.py <site> --mat-only       # machine-a-tron only, no host toolchain needed
configure-clis.py <site>                   # certs for admin-cli and MAT, mat-config.toml, run-mat.sh, /etc/hosts entry
```

The first builds the MAT binary. The second issues certificates for the admin
CLI and MAT, writes the fleet definition `mat-config.toml`, generates the
wrapper `run-mat.sh`, and adds the API hostname to `/etc/hosts`.

Without `--mat-only`, `build-nico-clis.py` also compiles `nico-admin-cli`
and `nicocli` on the Mac, which needs cargo and Go installed there. This is
the one place where the toolchain rule from section 1 is not yet met. Use
`get-admin-cli.sh` instead, unless you need to test your own changes to the
CLI.

MAT runs on the VM only. It puts the addresses of its mock BMCs on the
fabric bridge `br-dc1-internet`, which exists only inside the VM, and NICo's
site-explorer connects to those addresses. Start it like this:

```bash
ssh nico@192.168.64.126 '~/mac/sites/dc1/dev1/run-mat.sh'
```

`run-mat.sh` copies the binary, the certificates and the configuration to
local disk on the VM, installs the binary, stages everything under
`/etc/machine-a-tron/dc1/`, and launches MAT under `sudo`. The log is
`/var/log/machine-a-tron-dc1.log` on the VM. Watch machines appear with
`run-admin-cli.sh machine show` (no argument lists them all).

**Between MAT runs**, after stopping MAT and before starting it again, reset
the fleet:

```bash
reset-mat-state.py <site> --yes
```

NICo remembers the fleet it has seen: the endpoints it explored, the
machines it expected, and the BMC credentials it rotated. Without the reset,
a new run against fresh mocks is locked out. Custom MAT builds and the
failure catalog are in `mat-in-nico-dev.md`.

### DPF and the two provisioning modes

NICo provisions the DPU in each managed host through DPF, the DOCA Platform
Framework. In production, NICo creates DPF resources in Kubernetes and the
DPF operator does the work: it flashes the DPU, boots its operating system,
and reports progress by updating the status of a DPU resource. NICo watches
that status and moves the host along when the DPU reports Ready.

A nico-dev site brought up with `dpf: true`, the default, runs the same NICo
code path. Instead of the real DPF operator, which would need real DPU
hardware, it runs a simulator called `dpf-sim-controller` in the
`dpf-operator-system` namespace. The simulator watches the same resources
NICo creates and advances the DPU status through the same sequence of
phases, including the point where NICo must power-cycle the host through its
BMC, which MAT's mock answers. Nothing is flashed and no DPU boots. Every
MAT host therefore passes through the `dpuinit` state and comes out Ready,
exactly as a real host would. Watch it happen:

```bash
kubectl -n dpf-operator-system get dpudevice,dpunode,dpu -w
kubectl -n dpf-operator-system logs deployment/dpf-sim-controller -f
```

The simulator is built from your worktree during bring-up and pushed to the
local registry. Nothing DPF-related is pulled from NGC, and no real DPF
component is installed. Never install the real DPF operator on a nico-dev
site; it and the simulator would both update the DPU status and fight.

The older iPXE provisioning path is still available in two ways:

- **For chosen hosts.** Run `<site>/run-admin-cli.sh dpf disable <host>`
  after the host has been discovered and before it is ingested. NICo refuses
  to disable DPF on a host that was already ingested through DPF, so decide
  before the run reaches `dpuinit`. MAT registers every host with DPF on.
- **For the whole site.** Set `dpf: false` in the bringup yaml. No DPF
  resources are installed, no simulator runs, and NICo logs a warning that
  iPXE provisioning is deprecated.

To redeploy the simulator later, run `deploy-dpf-sim.py <site>`. Useful
options are `--phase-dwell 30s` for a slower walk through the phases,
`--rebuild` after you changed the simulator's code, and `--uninstall` to
remove it. Advanced settings live in the site yaml under `nico-system.dpf`.
What the simulator reproduces, what it does not, and its failure catalog are
in section 13 of `mat-in-nico-dev.md`.

**nicocli.** The REST-surface CLI needs a token that is minted inside the
cluster:

```bash
TOKEN=$(bash <repo>/helm-prereqs/keycloak/get-token.sh)
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx infrastructure-provider current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx tenant current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx vpc list
```

On a fresh site the two `current` calls must come first. They create the
organisation's provider and tenant objects; every other call returns 403
until they exist.

## 10. The dev loop

This is the source-build lane's cycle: change code, build images, roll the
cluster onto them.

```bash
build-dev-nico.py    <site> --tag t2      # arm64 images, pushed to the colima registry
redeploy-dev-nico.py <site> --tag t2      # helm upgrade of the nico release only
kubectl -n nico-system get pods -w
```

Three rules:

- **Use a new tag every time.** Both scripts refuse to rebuild or redeploy a
  tag that is already deployed. If they did not, the cluster would silently
  keep running the old image under the same name.
- **Never go backwards.** Each deploy advances the database's migration
  ledger, and an older binary refuses to run against a newer ledger. To
  revert, go forward: restore the code, build a fresh tag from the current
  checkout, and deploy that. If the `nico-api-migrate` job crash-loops with
  "migration … was previously applied but is missing", delete the job,
  revert the code, then build and deploy a new tag.
- **On a full node**, a rolling redeploy may not find room for its new pod.
  `redeploy: { on_insufficient_cpu: scale-down-first }` in the bringup yaml
  lets it proceed by removing the old pod first.

For a full deploy, or one release at a time, use `deploy-dev-nico.py <site>
--tag <t>` with `--skip-to <release>` or `--only <release>`. Never delete
the `nico-system` namespace to recover from a problem; it holds the Helm
release state.

## 11. Add-ons after bring-up

Optional components are installed after the site is up, one script per
component, run from the Mac, at your discretion. Each add-on has its own
config file, complete on its own: nothing in it is taken from `bringup.yaml`
or the site yaml, so a site deployed from NGC can run a locally built add-on
and the other way round. NICo Flow is the first:

```bash
cp flow-example.yaml ~/nico-tests/vm1/shared/flow.yaml    # then edit: source, ngc.registry, ngc.tag
vi ~/nico-tests/vm1/shared/flow.yaml
deploy-flow.py <site> --config ~/nico-tests/vm1/shared/flow.yaml --dry-run
deploy-flow.py <site> --config ~/nico-tests/vm1/shared/flow.yaml
deploy-flow.py <site> --status                             # what is installed, from Helm
deploy-flow.py <site> --uninstall
```

`<site>` is the site folder; the script takes only the kubeconfig from it.
The config names the image source, `ngc` or `build`, and everything that
source needs. Add-ons write nothing into the site yaml: Helm is the record
of what is installed, and removal is the chart's own uninstall.

The design, the config format shared by every add-on, and the list of other
candidates are in `ADDONS.md` and `deploying-extras.md`. DPF is not an
add-on; it is part of the base site, see section 9.

## 12. Reboots and recovery

After the VM reboots, the fabric service recreates its bridges and switches,
kubelet restarts the cluster, and every pod starts from its cached image.
Kubernetes has no notion of start order, so all pods start at once and some
lose the race for their dependencies. If the site looks Running but does not
work, run the ordered restart on the VM:

```bash
sudo restart-ordered.sh          # verify infrastructure, restart every consumer in dependency order
sudo restart-ordered.sh --cold   # scale all to zero first, then up in order
```

The script checks the infrastructure first, restarts every component in
dependency order, ends with a pass or fail verdict, and reminds you of the
Mac route from section 7.

## 13. Learning the fabric

The simulated fabric is a good place to learn EVPN networking. On the VM,
`ndev.py ~/mac/sites/dc1/dev1 fabric shell` lists the switches, and
`fabric shell spine-1` drops you into that switch's vtysh console. Try
`show bgp summary`, `show ip route` and `show bgp l2vpn evpn`. Break anything
you like; `sudo systemctl restart nico-dev-fabric` rebuilds the whole fabric
from the site yaml. Newcomers should start with `networking-primer.md`.

## 14. Troubleshooting

- **The VIP refuses connections on a fresh site while all pods are
  Running.** Check `kubectl -n nico-system get endpoints nico-api`. If it is
  empty, the API is not serving. Run `kubectl -n nico-system rollout restart
  deployment/nico-api`; the VIP answers about 30 seconds later.
  `first-boot.sh` and `onboard-golden.sh` already do this once.
- **The route is missing.** If `netstat -rn | grep 11.133` prints nothing,
  re-add the route from section 7. It disappears every time the last VM
  stops.
- **An image pull is stuck**, with the pod Pending and a single "Pulling"
  event. Two causes, both fixed at the source since 2026-09-16 but still
  possible for an image that was not delivered through the share. Either
  the VM is pulling through colima's ssh port forwarder, which stalls, or a
  huge layer's unpack outlived containerd's progress watchdog. The scripts
  now deliver every image into the VM's containerd through the share
  (`image_delivery.py`) and set the watchdog to 30 minutes, so kubelet finds
  the image present and never pulls. If it happens anyway:
  `image_delivery.py <site> <the image ref from the pod's events>` puts the
  image in place; then `sudo systemctl restart containerd` on the VM drops
  the stuck pull. Running containers are not affected.
- **The image import into the VM looks stuck.** It is slow, not stuck. The
  VM reads the tarball through the share at about 15 MB/s, so the 10 GB
  core image takes 10 to 12 minutes to copy, and then the unpack of its 9 GB
  layer takes several more. The delivery prints that estimate before it
  starts, then one progress line per minute from the Mac. For a closer
  look, run the command it prints on the VM:
  `bash /home/nico/mac/infra-controller/tools/nico-dev/monitor-import.sh <tarball>`.
  It shows the containerd content store growing while the copy runs, then
  the snapshotter growing while the unpack runs. Only a content store that
  does not grow for two samples in a row is a real stall.
- **A pull fails with "HTTP response to HTTPS client".** Run `ndev.py <site>
  registry verify` on the VM. If containerd shows ✗, the insecure-registry
  `config_path` is missing; the fix is printed.
- **The registry is not running on the Mac.** Check with `docker ps | grep
  registry`. `ensure-registry.py` starts it.
- **Vault is sealed** after a restart. The unsealer normally resolves it
  within seconds. If not, `deploy-dev-nico.py <site> --skip-to nico`.
- **The VM has no address.** On the UTM console, run `ip addr show enp0s1`,
  then look at `cat /etc/netplan/99-nico-static.yaml` and run
  `sudo netplan apply`.

## 15. Tear down

Stop and delete the VM in UTM, then remove `~/nico-tests/vm1/*.utm`. A
one-command `dev-down.py` exists for Linux hosts; the Mac version is
planned. Neither step touches your site folder or your worktree.

## 16. Maintainers

- **Bake and export a golden image.** On the VM, with every pod Running,
  MAT stopped and the fleet reset, run `sudo bash bake-golden-image.sh <site
  yaml>`. Shut the VM down. Remove the shared directory from the VM's UTM
  settings. In UTM, right-click the VM and choose **Share…** to export the
  `.utm` bundle, then zip it. Never boot the master copy; give each import
  its own APFS copy with `cp -cR`. To return the builder VM to development
  afterwards, see `how-to.md` §12A.4b.
- **Smoke test** before pushing any change to the cloud-init seed, the VM
  creation record, or `prepare-vm`: `smoke-test.sh` takes about six minutes
  on a throwaway VM.
- **Tag a validated tip** after a full bring-up:
  `git tag validated-YYYYMMDD && git push origin validated-YYYYMMDD`. The
  stable graft channel serves the newest such tag.

## 17. Script reference

| Script | Runs on | Does |
|---|---|---|
| `check-prereqs.sh [--build]` | Mac | read-only prerequisite check, includes checkout parity |
| `check-parity.py [repo] [--quiet]` | Mac | does the checkout still match what the nico-dev scripts assume |
| `image_delivery.py <site> <ref>… [--check]` | Mac | put images into the VM's containerd through the share (never through the registry tunnel) |
| `onboard-golden.sh --zip Z --dest D` | Mac | golden image ZIP to running site, hands off |
| `bring-up.py --config X [--dry-run] [--from step]` | Mac | the whole bring-up |
| `ngc-tags.py --config X` | Mac | deployable NGC tags |
| `build-dev-nico.py <site> --tag T` | Mac | build images, push to the colima registry |
| `deploy-dev-nico.py <site> --tag T` | Mac | full helm deploy, resumable |
| `redeploy-dev-nico.py <site> --tag T` | Mac | roll the nico release to a tag |
| `deploy-flow.py <site> --config flow.yaml [--status\|--uninstall]` | Mac | Flow add-on, from its own standalone config |
| `deploy-dpf-sim.py <site> [--phase-dwell T] [--uninstall]` | Mac | DPF simulator (default site; the `dpf` bring-up step) |
| `build-nico-clis.py <site> [--mat-only]` | Mac | MAT in a container; admin-cli and nicocli with host toolchains |
| `configure-clis.py <site>` | Mac | certs, MAT config, wrappers, /etc/hosts |
| `get-admin-cli.sh <site>` | VM | admin CLI from the API container, no build |
| `run-admin-cli.sh`, `run-mat.sh` | Mac / VM | generated wrappers in the site folder |
| `reset-mat-state.py <site> --yes` | Mac | fleet back to time zero |
| `ndev.py <site> [sub]` | Mac or VM | status, fabric, BGP, registry, DPU |
| `restart-ordered.sh [--cold]` | VM | ordered recovery after a reboot |
| `first-boot.sh` | VM | personalise a golden clone |
| `bake-golden-image.sh <site yaml>` | VM | prepare a VM for export |
| `smoke-test.sh` | Mac | maintainers: boot-path check on a throwaway VM |

Friction is a bug. If a step confused you, or an error message did not get
you out of trouble, that is a defect in this tooling. Please report it.
