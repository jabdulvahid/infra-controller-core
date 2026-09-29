# nico-dev on a Mac, step by step

Updated 2026-09-29.

nico-dev gives you a complete NICo development environment inside one virtual
machine on an Apple Silicon Mac. The VM runs under UTM and contains a
Kubernetes cluster, the NICo software stack, a simulated datacenter network
fabric built from FRR routers, and MAT, the simulator that plays a rack of
servers for NICo to manage. At the end of Step 7 you have a working NICo site
that you can develop against, break, and rebuild.

NICo runs as a set of container images inside the VM's Kubernetes cluster.
There are two ways to get those images:

- **Build them yourself.** The bring-up compiles the NICo source in your
  checkout into images and pushes them to a small registry on your Mac, which
  the VM pulls from. Everything is local; you need no account anywhere. The
  first build takes 20 to 40 minutes, later ones minutes. Use this when you
  change NICo code, or when you have no NGC access.
- **Use pre-built images from NGC.** NVIDIA's CI publishes every merged
  build to the NGC container registry, `nvcr.io`. The bring-up pulls the tag
  you name, retags it into the same local registry, and deploys it. Nothing
  is compiled, so a site is up in about 30 minutes, but you need an NGC API
  key with read access to the team that publishes them, and you can only run
  what CI has built.

The steps below follow the first way: build the VM yourself and deploy NICo
from images built out of your own checkout. The NGC way differs in one
block of one yaml file and one step of the run; Appendix B has it. A
third way, a golden-image ZIP from a colleague that already contains a
running site, is Appendix A. After either appendix, come back to Step 6.

Intel Macs are not supported.

**How nico-dev is put together.** Two machines take part: your Mac and one
Ubuntu VM that UTM runs on it. The Mac does the heavy lifting that does not
need to be inside the cluster: it builds the NICo images and the MAT binary
in containers on colima, runs the small image registry the VM pulls from,
and runs the nico-dev scripts that drive the whole bring-up over ssh. The VM
holds everything that has to run together as a site: the Kubernetes cluster,
the NICo services, the simulated network fabric, and MAT with its mock BMCs,
which need the fabric's bridges and therefore cannot run on the Mac. The two
are joined by a shared folder on the Mac that the VM mounts, and that folder
is where every artifact crosses over: your source worktree with the nico-dev
tools inside it, the site's configuration and certificates, the kubeconfig,
the MAT binary and its config, and image tarballs on their way into the VM.
A script started on the Mac can therefore hand a file to the VM by writing
it to the share, and a script on the VM finds the same file under `~/mac`.
For that to work in both directions, files need the same owner on both
sides, so
the VM's login user, `nico`, is created with the same numeric user id as
your Mac account; the VM builder reads your UID and passes it into the VM's
cloud-init, and `bringup.yaml` has a `uid` key only for the rare case where
you want another value. A file the VM writes into the share therefore shows
up on the Mac as yours, and a file you write on the Mac is `nico`'s in the
VM, with no permission fixing on either side.
Your Mac reaches the services inside the VM through one route to the VM's
address, so `kubectl` and the admin UI work from the Mac as if the cluster
were local.

A few words used throughout:

- **The share** is a folder on your Mac that the VM mounts. Files you put
  there are visible inside the VM, and files the VM writes there are visible
  on the Mac. Everything nico-dev creates lives in the share.
- **A site** is one deployed NICo installation, named by a datacenter name
  and a site name, for example `dc1/feature1`.
- **A lane** is where the NICo container images come from: built from your
  own source checkout (this page's main path), or pulled pre-built from
  NVIDIA's NGC registry.
- **A VIP** is a virtual IP address inside the VM that a NICo service
  answers on. Your Mac reaches them through one route, added in Step 6.
- **MAT**, machine-a-tron, is the simulator that plays the hardware NICo
  manages. It runs on the VM and presents a rack of mock servers: for each
  one a Redfish BMC with power control, firmware and virtual media, a DPU,
  and the DHCP and boot traffic a real host would produce. NICo discovers,
  ingests and provisions them exactly as it would real machines, and MAT
  also drives the switches and power shelves of the simulated rack. Its
  fleet is defined in `mat-config.toml` (Step 9).
- **The fabric** is the simulated datacenter network inside the VM: FRR
  routers acting as spine and leaf switches, joined by Linux bridges, with
  the EVPN and BGP configuration a real site has. NICo's network controllers
  talk to it as they would to real switches, and MAT's mock BMCs live on it.
- **The admin CLI**, `nico-admin-cli`, is NICo's operator command line. It
  talks to the NICo API over gRPC with a client certificate and does the
  day-1 site setup, machine and credential management, and inspection. On a
  nico-dev site you run it through the generated wrapper `run-admin-cli.sh`
  (Step 8).
- **nicocli** is the command line of the REST API, the tenant-facing side of
  NICo: organisations, VPCs, instances. It authenticates through Keycloak
  and runs through the wrapper `run-nicocli.sh` (Step 11).
- **DPF**, the DOCA Platform Framework, is how NICo provisions the DPU in
  each host. A nico-dev site runs a DPF simulator instead of the real
  operator (Appendix C).

**Two configuration files.** You write one, `bringup.yaml`, in Step 3. It
holds the few decisions that are yours: the VM's name and size, your
datacenter and site names, two network octets, where the images come from,
and a handful of switches such as DPF or the firmware simulation. Copy it
from `bringup-example.yaml`, which documents every key. From it the `site`
step generates the second file, the **site yaml**, at
`<share>/sites/<dc>/<site>/<site>.yaml`. That is the complete description of
the site: every network prefix, the service VIPs, the Helm values, the MAT
fleet, the DPF and firmware settings. Every later script, on the Mac and on
the VM, reads the site yaml, never `bringup.yaml`. You normally do not edit
it; advanced settings that have no `bringup.yaml` key live there, and after
editing one you regenerate the values with `bring-up.py --from nico --until
nico`, as Step 10 shows for the firmware versions.

**How NICo gets deployed.** nico-dev has no deployment of its own. It
installs the same Helm charts that production uses, taken from the checkout
in your share: `helm-prereqs/`, the `nico-prereqs` chart with the shared
PostgreSQL cluster, the Vault configuration and the secrets wiring; `helm/`,
the `nico` umbrella chart with nico-api, DHCP, DNS, PXE, the BMC proxy, the
SSH console and hardware health; and `helm/rest/`, the REST API and its site
agent. Around them it installs the same third-party pieces upstream's
`helm-prereqs/setup.sh` installs, in the same order: local-path-provisioner,
cert-manager, Vault, External Secrets, the Zalando PostgreSQL operator, and
for the REST stack its own PostgreSQL, Keycloak and Temporal. What nico-dev
adds is the values. `generate_dev_values.py` reads the site yaml and writes
one values file per chart into `<site>/dev-values/` (`cert-manager.yaml`,
`vault.yaml`, `eso.yaml`, `zalando-postgres-op.yaml`, `nico-prereqs.yaml`,
`nico.yaml`, which embeds nico-api's site-config TOML, and
`nico-rest-dev.yaml`), and `deploy-dev-nico.py` runs `helm install` or
`helm upgrade` for the twelve releases in dependency order, healing a
release that a previous run left half-installed. Each release is one step
of the bring-up, so a failure resumes at exactly that release. The result is
a real NICo installation: `helm list -A` shows the releases, and everything
you know about debugging NICo with `kubectl` applies unchanged. Because
nico-dev re-implements the values and the order rather than calling
`setup.sh`, `check-parity.py` verifies on every run that the charts in your
checkout still look the way nico-dev assumes.

**The addresses.** The VM itself sits on UTM's shared network, which is
`192.168.64.0/24` on most Macs, at the address `<UTM subnet>.<host_num>`;
`host_num` is a `bringup.yaml` key with the default 126, so this page writes
`192.168.64.126` throughout, and a second VM gets another `host_num`. The
Mac is `.1` on that network, which is where the VM finds the image registry.
If your UTM uses a different subnet, set `ip` in `bringup.yaml` to the VM's
address as the Mac reaches it. Everything inside the simulated site
is derived from the two octets you choose in `bringup.yaml`, `underlay` and
`overlay`, 11 and 12 in this page's examples. With `underlay: U` the fabric
uses `U.128.0.0/16` for the switch underlay, `U.129.0.0/16` for switch
loopbacks, `U.130.0.0/16` and `U.131.0.0/16` for the DPU fabric and DPU
loopbacks, `U.132.0.0/30` and `U.132.1.0/31` for the internet uplink and the
control-plane link, `U.140.2.0/24` for the network MAT's mock BMCs answer
on, and `U.133.1.0/27` for the service VIPs: the DHCP VIP is `U.133.1.0`,
PXE `.2`, the SSH console `.4`, NTP `.5` to `.7`, the API and admin UI `.17`,
DNS `.19` and `.20`, the same layout the production tooling uses. With
`overlay: O` the tenant overlay is `O.150.0.0/16` and the admin network
`O.135.0.0/16`. Pick two octets that
nothing on your Mac or VPN uses; the dry run warns if the Mac already routes
them. A second site on the same Mac needs its own pair, its own VM name and
its own `host_num`.

**Golden images.** Once you have a working site, you can turn the VM into a
golden image: a `.utm` bundle, zipped, that a colleague imports and has
running in about five minutes with no build at all. `bake-golden-image.sh`
prepares the VM for that (Appendix I): it saves the site yaml and the
nico-dev tools inside the image, resets the `nico` user to the default
password with no SSH keys, cleans caches and logs, and checks that every pod
is Running and every image is cached in the VM's containerd. You shut the VM
down, detach the shared folder, and export the bundle from UTM. On the
receiving Mac, `onboard-golden.sh` (Appendix A) imports it, sets the shared
folder, and runs `first-boot.sh` inside the VM, which installs that person's
SSH key, points the VM at their share, and starts the fabric. What the image
carries is the VM: the cluster, the deployed NICo release at the tag it was
baked with, the fabric, and the cached images. What it does not carry is
anything in the share, so the recipient still needs a checkout with the
nico-dev tools grafted, and a golden image is a snapshot of one NICo
version: to move forward they redeploy a newer tag (Step 12) or take a newer
image.

## Assumptions

1. An Apple Silicon Mac. Pick the row that matches what you plan to do:

   | Purpose | Mac RAM | VM | Notes |
   |---|---|---|---|
   | build-and-play: golden image or NGC lane, no redeploys | 16 GB | 6 CPUs, 8 GB | the running stack uses about 5 GB |
   | development: source builds, redeploy cycles, MAT | 32 GB+ | 8+ CPUs, 16 GB+ | the UTM VM and colima time-share the cores |

   Disk: about 250 GB free for the development tier. The UTM disk is 120 GB
   by default, the colima disk is 100 GB, and images and caches take the
   rest. Both disks are sparse and only consume what is written.
2. Homebrew is installed.
3. You can reach `github.com/dsx-ai-factory/infra-controller`, and you have
   an SSH keypair in `~/.ssh`, or you generate one in Step 1. The public key
   is installed into the VM so the scripts log in without a password.
4. This page assumes one nico-dev VM on the Mac. Several can coexist, but
   each needs its own address on UTM's network (`host_num`), its own VM
   name, its own folder, and its own `underlay` and `overlay` octets; two
   VMs that share any of these collide. If you have an old one you no longer
   need, tear it down first (Step 13).
5. Rust, cargo and Go are **not** required on the Mac. Every build nico-dev
   performs, the NICo images and the MAT binary, runs inside a container on
   colima. The one exception is compiling the two command-line clients
   `nico-admin-cli` and `nicocli` natively on the Mac; Step 8 and Step 11
   show how to get both without a compiler.

## Step 1 - Install the Mac tools

On the Mac:

```bash
# UTM runs the VM. Launch it once after installing so macOS registers it.
brew install --cask utm

# git and python3 run the nico-dev scripts; helm and kubectl talk to the cluster;
# colima is the Docker engine on the Mac and docker its command-line client.
brew install git python3 helm kubectl colima docker
pip3 install pyyaml

# Start the Docker engine with enough resources. colima's default of 4 CPUs and
# 8 GB is too small: the Rust build of the NICo images gets killed for lack of memory.
colima start --cpu 8 --memory 16 --disk 100

# Only if you have no SSH keypair yet. The public key is installed into the VM.
ssh-keygen -t ed25519
```

colima and docker together build the container images and run the small
local image registry that the VM pulls from; nothing else on the Mac is
needed to build.

## Step 2 - Folder, worktree, tools

The rest of this page assumes you are familiar with the NICo development
environment: the repository, how it is built and tested, and its
contribution workflow. If you are not, set that up first by following
[`CONTRIBUTING.md`](https://github.com/dsx-ai-factory/infra-controller/blob/main/CONTRIBUTING.md)
in the infra-controller repository; that gives you the primary clone this
step starts from.

This page uses git worktrees for feature development: each feature gets its
own worktree, created from your primary clone, and the primary clone stays
where it lives. A worktree is a second checkout of the same repository. It
shares history with the clone but has its own files, so several branches can
be checked out side by side, each in its own VM.

Let us assume the branch you want to work on is `feature1`. The objective is
to make your changes on that branch and deploy them into a nico-dev site
named `feature1`, running in a VM named `vm-feature1`. Each VM gets one
folder; its `shared/` is the folder the VM mounts, and inside it lives the
worktree on the branch you want to run. On the Mac:

```bash
# One folder per VM. shared/ is what the VM mounts; inside the VM this folder
# appears as /home/nico/mac, so a file written here is visible there at once.
mkdir -p ~/nico-tests/vm-feature1/shared

# Your primary clone, set up as CONTRIBUTING.md describes: origin is your fork
# and upstream is NVIDIA's repository. Check with git remote -v.
cd ~/projects/infra-controller
git remote -v

# Fetch first, so the new branch starts from TODAY's NVIDIA main rather than
# from your last fetch.
git fetch upstream main

# Create the branch and its worktree inside the share in one command.
git worktree add -b feature1 ~/nico-tests/vm-feature1/shared/infra-controller upstream/main
```

Note: if `feature1` already exists, for example because you pushed it from
another machine, leave out `-b` and the start point:
`git worktree add ~/nico-tests/vm-feature1/shared/infra-controller feature1`.

Note: the fetch matters. The images you deploy are built from current main,
and the Helm charts, the DPF simulator's RBAC and the simulator's own source
come from this worktree, so all of them should be from the same day. A
worktree cut from a stale main pairs old charts and permissions with newer
images; one such case left every host stuck in `DPUInitializing` because the
simulator's role predated its code. `check-prereqs.sh` below reports how far
the checkout is behind upstream main and prints the command that brings it
current.

Now add the nico-dev tools to that worktree. This is called grafting. The
tools land in `tools/nico-dev` as untracked, git-ignored files, so they never
show up in `git status`, in your commits, or in your pull requests. Run the
same command again whenever you want to update them:

```bash
cd ~/nico-tests/vm-feature1/shared/infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash -s -- --edge
```

`--edge` fetches the newest version of the tools. Without it you get the
stable channel, the newest `validated-*` tag, which is currently older than
several features this page relies on. Use `--edge` until a newer tag exists.

Put the tools on your `PATH` and run the prerequisite check:

```bash
cd tools/nico-dev
export PATH="$PATH:$(pwd)"   # in your shell profile
check-prereqs.sh             # run-the-sim tier
check-prereqs.sh --build     # also the source-build tier
```

You should see every line marked ✓. The check only reads; it changes
nothing. Fix every ✗ line; each says how. The first time a script drives
UTM, macOS shows the dialog "Terminal wants to control UTM". Click Allow.

Note: the last section of the check, "checkout parity", is about the
repository, not your Mac. The nico-dev scripts re-implement parts of the
upstream setup and therefore assume certain chart paths, resource names and
defaults in the checkout. A ✗ there means a nico-dev script needs updating,
not your repository; report it. The line about upstream main is
informational for a source build; Appendix B says when it matters.

## Step 3 - Write bringup.yaml

Everything the bring-up needs to know goes into one file. Copy the example
and edit it; the example documents every key. On the Mac:

```bash
cp bringup-example.yaml bringup-mysite.yaml
vi bringup-mysite.yaml
```

For the source-build lane it looks like this:

```yaml
name: vm-feature1
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
firmware_sim: true  # only for firmware-upgrade work (Step 10); default false

dc: dc1
site: feature1
underlay: 11        # first octets of the fabric prefixes: pick two your Mac and VPN do not use
overlay: 12

tag: main-20260929  # the label for the images you build; a new one for every rebuild
```

What the fields mean:

- `name`, `user`, `password`, `ssh_key`: the VM's name in UTM, the login
  account created inside it, and the public key that account accepts. The
  password is normally never asked; the VM builder authorizes your key.
- `vm`: the VM's size. Use the table in the Assumptions.
- `redeploy`: what to do when a later redeploy cannot fit a new pod on a
  full node. `scale-down-first` lets it proceed (Step 12).
- `dpf`: `true`, the default, gives you NICo's DPF provisioning path
  together with the DPF simulator. `false` gives the older iPXE path.
  Appendix C explains both.
- `firmware_sim`: `true` makes MAT hosts start with outdated firmware so
  ingestion runs the upgrade chain. Leave it out unless you work on that
  (Step 10).
- `dc`, `site`: the names of your datacenter and site. They appear in
  folder names and in the cluster. `dc` is 1-3 characters, `site` 1-8.
- `underlay`, `overlay`: two numbers that become the first octet of every
  network prefix inside the VM. Pick two numbers that nothing on your Mac or
  VPN uses. With `underlay: 11` the admin UI ends up at `11.133.1.17`. The
  dry run warns you if your Mac already routes them.
- `tag`: a label for the images you build. Both image groups share it. Use a
  new one for every rebuild; a deployed tag is never rebuilt or redeployed.

Leave `ip` unset unless UTM's shared network is not `192.168.64.0/24`, which
is rare; the VM's address is `<UTM subnet>.<host_num>`. If you need a
different last octet, set `host_num`; the default is 126.

## Step 4 - Dry run

On the Mac:

```bash
bring-up.py --config bringup-mysite.yaml --dry-run
```

You should see every prerequisite marked ✓, ✗ or ⚠, the numbered plan with
the exact commands, and the verdict `READY`. Nothing is executed during a dry
run. `NOT READY` lists the lines to fix; fix them and run the dry run again.

## Step 5 - Bring the site up

On the Mac:

```bash
bring-up.py --config bringup-mysite.yaml
```

The run goes through 21 steps: `vm` → `prep` → `site` → `fabric` → `cp` →
`build` → `registry` → one step per NICo Helm release, seven `core` ones
(local-path-provisioner, cert-manager, vault, external-secrets,
postgres-operator, nico-prereqs, nico) then five `rest` ones (rest-postgres,
keycloak, temporal, nico-rest, nico-rest-site-agent) → `dpf` → `route`.
`bring-up.py --list` shows them. The `build` step compiles the NICo images
from your worktree: 20 to 40 minutes the first time, minutes afterwards.

The run stops and waits for you twice. This is by design:

1. **The UTM share path.** UTM does not let a script set the shared folder
   of a VM. The runner pauses, tells you to open the VM's Sharing settings
   and point them at `~/nico-tests/vm-feature1/shared`, and waits for you to press
   Enter. It then checks what UTM recorded: if the Path is missing or
   different it says so and asks again, so a missed click does not surface
   minutes later as a failed first boot. Type `skip` to boot without a share
   (throwaway VMs only).
2. **`sudo` on the Mac**, at the end, for the route to the service addresses.

Nothing else asks. The VM builder's cloud-init seed authorizes your SSH key
and gives the user passwordless sudo, so `prep` logs in with the key; the
`password:` in the config is only a fallback for a VM built another way. The
host-key question is answered automatically for a new address, and the
`iptables-persistent` save dialogs inside the VM are pre-answered.

The runner's output is the raw output of every command it runs, which is
long and says little about where the run stands. Open a second terminal:

```bash
bring-up-status.py --config bringup-mysite.yaml          # redraws every 2 s; Ctrl-C leaves the run alone
bring-up-status.py --config bringup-mysite.yaml --once   # one snapshot, for pasting
```

It shows every step as done, running, failed or not yet, the time each took,
what the current step is doing inside it (which of the three core images or
six REST images is building, which Helm release is installing), how to ssh to
the VM, the kubeconfig, the URL you will get at the end, and after a failure
the resume command. The events live in `<share>/.bring-up/<vm-name>.jsonl`.

If a step fails, the runner prints the known failure modes of that step and
the exact command to resume, `--from <step>`, which for the releases is the
release name (`--from keycloak`). Every step is safe to run again; a release
step is a fast idempotent Helm upgrade when it is already installed, and a
resume keeps the earlier steps in the status viewer. The `keycloak` step is
the one most likely to fail on a loaded Mac; Appendix F has the recovery.

You should see the run end with a URL, `https://11.133.1.17/admin` for
`underlay: 11`.

## Step 6 - kubectl, the route, and where things are

On the Mac, point kubectl at the cluster and add the route to the service
addresses:

```bash
export KUBECONFIG=~/nico-tests/vm-feature1/shared/sites/dc1/feature1/dc1-feature1.kubeconfig.yaml   # in your shell profile
kubectl get nodes

sudo route -n add -net 11.133.1.0/27 192.168.64.126     # route to the service VIPs
route -n get 11.133.1.17                                # gateway: 192.168.64.126
```

Note: **the route does not last.** macOS removes it whenever the last UTM VM
stops, because UTM's network bridge `bridge100` disappears and macOS flushes
every route through it. The route also goes on sleep and wake, and when a
VPN connects or disconnects. Re-add it with the same command; `restart-ordered.sh` prints it for you.

Where things are, seen from both sides:

| | Mac | Inside the VM |
|---|---|---|
| share root | `~/nico-tests/vm-feature1/shared` | `~/mac` |
| repo worktree | `<share>/infra-controller` | `~/mac/infra-controller` |
| site folder | `<share>/sites/dc1/feature1` | `~/mac/sites/dc1/feature1` |
| image tarballs in transit | `<site>/images/*.tar` (deleted after import) | same path under `~/mac` |
| kubeconfig | `<site>/dc1-feature1.kubeconfig.yaml` | same path under `~/mac` |
| local registry | `localhost:5000` (colima) | `192.168.64.1:5000` |

Inside the VM the repository is a git worktree whose metadata lives on the
Mac. Do your git work on the Mac, not in the VM.

`ndev.py` shows status. Run it on the Mac for cluster status, or on the VM for
the full fabric health, because the fabric only exists inside the VM:

```bash
ndev.py <site>                                                       # Mac: cluster status; fabric/BGP/DPU n/a (VM-side)
ssh nico@192.168.64.126 'ndev.py ~/mac/sites/dc1/feature1 fabric verify' # VM: full fabric health
```

The subcommands are `fabric verify|info|shell [switch]`, `bgp info
[--detail]`, `cluster info`, `registry verify`, and `dpu info`.

## Step 7 - Verify

On the Mac:

```bash
kubectl -n nico-system get pods        # all Running or Completed
curl -k https://11.133.1.17/           # "Forge development build"
open https://11.133.1.17/admin
```

Note: on a corporate VPN, the VPN client may claim the address range before
your route does. Then tunnel through SSH instead: run
`ssh -L 8443:11.133.1.17:443 nico@192.168.64.126` and open
`https://localhost:8443/admin`.

## Step 8 - The admin CLI, without compiling anything

The NICo API container already contains the admin CLI binary. This script
copies it out, issues the client certificates it needs, and writes a wrapper
script. On the VM:

```bash
ssh nico@192.168.64.126
get-admin-cli.sh ~/mac/sites/dc1/feature1      # extracts the binary from the API container, issues certs, writes the wrapper
~/mac/sites/dc1/feature1/run-admin-cli.sh version
```

Always use the wrapper `run-admin-cli.sh`. The bare binary dials the API by
its in-cluster name and fails outside the cluster. The full step-by-step for
the CLIs and MAT, with what to expect at each stage, is
`clis-mat-in-nico-dev.md`; Steps 8, 9 and 11 are the summary.

## Step 9 - Build and run MAT

MAT is built in a container on the Mac and delivered to the VM through the
share. On the Mac:

```bash
build-nico-clis.py <site> --mat-only       # machine-a-tron only, no host toolchain needed
configure-clis.py <site>                   # certs for admin-cli and MAT, mat-config.toml, run-mat.sh, /etc/hosts entry
```

The first builds the MAT binary into `<site>/mat/machine-a-tron`; the first
build compiles the whole crate and takes twenty minutes or more, later ones
minutes. The second issues certificates for the admin CLI and MAT, writes the
fleet definition `<site>/mat/mat-config.toml`, generates the wrapper
`<site>/run-mat.sh`, and adds the API hostname to `/etc/hosts`.

Note: without `--mat-only`, `build-nico-clis.py` also compiles
`nico-admin-cli` and `nicocli` on the Mac, which needs cargo and Go installed
there. Use Step 8 and Step 11 instead unless you need to test your own
changes to a CLI.

MAT runs on the VM only. It puts the addresses of its mock BMCs on the fabric
bridge `br-dc1-internet`, which exists only inside the VM, and NICo's
site-explorer connects to those addresses. On the VM:

```bash
ssh nico@192.168.64.126 '~/mac/sites/dc1/feature1/run-mat.sh'
```

`run-mat.sh` copies the binary, the certificates and the configuration to
local disk on the VM, installs the binary, stages everything under
`/etc/machine-a-tron/dc1/`, and launches MAT under `sudo`. The log is
`/var/log/machine-a-tron-dc1.log` on the VM. You should see machines appear
with `run-admin-cli.sh machine show` (no argument lists them all). For a live
overview of the whole run, in a second VM terminal:

```bash
~/mac/infra-controller/tools/nico-dev/run-monitor-mat.sh ~/mac/sites/dc1/feature1
```

It shows expected machines, endpoints, machine states with the milestones
still to go, DPUs, DPF phases and MAT's own view, one page per section
(digits or ←→ switch pages, `?` for help), refreshing every 30 s. It finds
the MAT log by the site's `dc` name; `--mat-log <file>` pins a specific one.

**Between MAT runs**, after stopping MAT and before starting it again, reset
the fleet, on the Mac:

```bash
reset-mat-state.py <site> --yes
```

NICo remembers the fleet it has seen: the endpoints it explored, the
machines it expected, and the BMC credentials it rotated. Without the reset,
a new run against fresh mocks is locked out.

Note: **running a MAT you built yourself.** When you change MAT's source,
build from your own worktree into a separate folder so the site's baseline
binary stays untouched. `--out-dir` is required with `--repo` for that reason:

```bash
build-nico-clis.py <site> --mat-only --repo ~/projects/my-mat-worktree --out-dir <site>/mat-dev
cp <site>/run-mat.sh <site>/run-mat-dev.sh
```

Then change the two paths at the top of `run-mat-dev.sh`, `MAT_BIN` and
`MAT_CONFIG`, to the `mat-dev` folder, and copy `<site>/mat/mat-config.toml`
to `<site>/mat-dev/mat-config.toml` if you want a custom config, for example
an `acceleration_factor` or a `[machines.<group>.timing_overrides]` block.
The run script derives its log name from its own name, so
`run-mat-dev.sh` logs to `/var/log/machine-a-tron-dc1-dev.log` and the
baseline and your build never overwrite each other. The details and the
failure catalog are in `mat-in-nico-dev.md`.

## Step 10 - Firmware upgrade simulation

By default MAT hosts report firmware that already matches what NICo wants, so
ingestion never uploads anything. `firmware_sim: true` in `bringup.yaml`
(Step 3) changes that for every host: MAT reports the `initial` BMC and UEFI
versions and nico-api gets a firmware definition for the mock GB200 requiring
the `desired` ones, written into the chart's firmware volume by an init
container. Ingestion then runs the full chain per host: preingestion finds
the versions below the minimum, uploads through `SimpleUpdate`, polls the
Redfish task to `Completed`, power-cycles, reads the new version back, and
only then continues to Ready. Watch it in the nico-api log, on the Mac:

```bash
kubectl -n nico-system logs deploy/nico-api | grep -E "preingestion minimum|firmware upload|Firmware version satisfies"
```

To confirm the definition reached nico-api:

```bash
kubectl -n nico-system exec deploy/nico-api -- cat /opt/nico/firmware/gb200-sim/metadata.toml
```

The versions are `nico-system.firmware_sim` in the site yaml (`initial` 1.0,
`desired` 2.0); the mock accepts any string, so keep initial below desired.
Changing them after bring-up means `bring-up.py --config X --from nico
--until nico` to regenerate the values and upgrade the nico release, and
`configure-clis.py <site>` for the MAT config, then `reset-mat-state.py` and
a fresh MAT run. `false` renders exactly what the site rendered before the
option existed.

## Step 11 - nicocli, without compiling anything

The REST API has its own CLI, `nicocli`, and the REST API image ships it,
built from the same commit as the API. Four steps get it working.

On the Mac, extract the CLI. This copies the binary out of the REST API image
already in docker's store and writes a wrapper; it takes a few seconds:

```bash
get-nicocli.sh <share>/sites/dc1/feature1
```

You should see three steps end with a check mark, then a "Done" block with
the two commands to run next. The results are `<site>/nicocli/nicocli`, a
Linux build, and `<site>/run-nicocli.sh`, the wrapper, both on the share.

On the VM, bootstrap the organisation, once per fresh site:

```bash
ssh nico@192.168.64.126
~/mac/sites/dc1/feature1/run-nicocli.sh --bootstrap
```

The first call pauses a few seconds while a token is minted inside the
cluster. Then two commands run, `infrastructure-provider current` and
`tenant current`, each printing a JSON object: they create the
organisation's provider and tenant. Until this has run once, every other
command answers "Org does not have a Tenant associated".

On the VM, use it:

```bash
~/mac/sites/dc1/feature1/run-nicocli.sh vpc list
~/mac/sites/dc1/feature1/run-nicocli.sh --help
```

Always go through the wrapper. It supplies the API address, the organisation
`ncx` and the token; the bare binary knows none of them. The token is cached
for 25 minutes and renewed on its own. If a command ever fails with an
authentication error, put `--refresh-token` in front of it once.

When to repeat what: extract again after you deploy a new nico tag, so the
CLI matches the API. Bootstrap never again for this site. Nothing to do
between MAT runs; the REST side is untouched by the fleet reset. On the Mac
the same wrapper works too, if you built a Mac `nicocli` with
`build-nico-clis.py`; otherwise it tells you to use the VM.

## Step 12 - The dev loop

The source-build cycle: change code, build images, roll the cluster onto
them. On the Mac:

```bash
build-dev-nico.py    <site> --tag t2      # arm64 images, pushed to the colima registry
redeploy-dev-nico.py <site> --tag t2      # helm upgrade of the nico release only
kubectl -n nico-system get pods -w
```

Four rules:

- **Regrafting the tools does not rebuild.** The `nico` image copies the
  whole checkout, and the grafted tools live inside it; since 2026-09-27
  `build-dev-nico.py` keeps `tools/nico-dev/` out of the build context, so
  only source changes recompile. A rebuild that compiles although nothing in
  `crates/` changed means the tools are older than that fix.
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

## Step 13 - Reboots, recovery, tear down

After the VM reboots, the fabric service recreates its bridges and switches,
kubelet restarts the cluster, and every pod starts from its cached image.
Kubernetes has no notion of start order, so all pods start at once and some
lose the race for their dependencies. If the site looks Running but does not
work, or pods sit in `Unknown`, run the ordered restart on the VM:

```bash
sudo restart-ordered.sh          # verify infrastructure, restart every consumer in dependency order
sudo restart-ordered.sh --cold   # scale all to zero first, then up in order
```

The script checks the infrastructure first, restarts every component in
dependency order, ends with a pass or fail verdict, and reminds you of the
Mac route from Step 6.

Note: a Mac that sleeps freezes the VM. On wake, the VM's clock jumps, its
systemd watchdogs fire, and ssh logins can fail for a few minutes while
logind recovers. Keep the Mac awake during long runs, and expect the ordered
restart afterwards.

To tear down: stop and delete the VM in UTM, then remove
`~/nico-tests/vm-feature1/*.utm`. A one-command `dev-down.py` exists for Linux
hosts; the Mac version is planned. Neither step touches your site folder or
your worktree.

---

## Appendix A - Golden image: a ready-made VM in three commands

You received `nico-dev-golden-YYYYMMDD.utm.zip` from a colleague. It is a
complete VM with a running site inside, so Steps 3 to 5 do not apply; Steps 1
and 2 do. These commands turn the ZIP into a working site, on the Mac:

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
will need again later. Continue at Step 6.

The ZIP is kept. To start over, stop and delete the VM in UTM, remove
`~/nico-tests/vm1/*.utm`, and run the same command again with the same
`--zip`.

Note: `onboard-golden.sh` never reads a bringup yaml, and `bring-up.py`
never touches a golden ZIP. The two do not mix.

**Manual fallback**, if the scripted shared-folder step does not stick:

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
   route and `KUBECONFIG` as in Step 6.

## Appendix B - Deploy pre-built images from NGC

Use this instead of the source build when you do not change NICo code and
want a site quickly: nothing is compiled, and a site is up in about 30
minutes. You need an NGC API key with read access to the registry of the
organisation and team that publishes the NICo images; ask your team for
both. Everything else on this page stays the same; the differences are one
block in `bringup.yaml` and one step of the run.

Put your NGC API key in an environment variable, in your shell profile. The
yaml names the variable, never the key itself, and the key is never printed
or stored:

```bash
export NGC_API_KEY='...'
```

In `bringup.yaml` (Step 3), leave `tag` out and add this block instead.
`tag` is the default for every base image; the optional `tags` map gives one
image group a different tag: `core` is the NICo core image, `rest` the six
REST images, each group one Helm release. `core_image` and `images` are the
names NGC publishes under; the names the Helm charts expect locally are
fixed, and only the core differs on NGC (`nvmetal-carbide` against `nico`).

```yaml
ngc:
  registry: nvcr.io/<org>/<team>       # ask your team
  tag: <tag>                           # default tag for every image; ngc-tags.py, below
  # tags:                              # optional: a different tag for one image group
  #   rest: <tag>
  core_image: nvmetal-carbide          # NGC's name for the core image
  # images:                            # optional: NGC names, if NGC publishes an image
  #   rest: {nico-rest-api: <ngc name>}  #   under another name (local chart name: NGC name)
  token_env: NGC_API_KEY               # NAME of the env var holding your key
```

To find a deployable tag, one that tracks main and is published for arm64:

```bash
ngc-tags.py --config bringup-mysite.yaml                 # newest PR builds tracking main
ngc-tags.py --config bringup-mysite.yaml --before v2.3.0
ngc-tags.py --config bringup-mysite.yaml --group rest    # tags of the REST images
```

Prefer a recent development tag. A fresh site's database schema follows
main, and the migration job refuses to run an older version against a newer
schema, so an old tag on a new site can fail.

Note: the checkout in your share still matters on this lane. The Helm
charts, the DPF simulator and its RBAC come from that checkout, while the
images come from CI's build of a newer upstream. A checkout behind upstream
pairs older charts with newer images, so a chart may lack a value, RBAC rule
or CRD the newer nico-api expects. `check-prereqs.sh` (Step 2) prints the
command that brings the checkout current; run it before the bring-up.

With an `ngc:` block, the `build` step and the twelve release steps
collapse into one `ngc` step that pulls, retags and deploys the pre-built
images; 9 steps in all. A resume past a failed release on that lane is
`deploy-dev-nico.py <site> --tag <images.tag from the site yaml> --skip-to
<release>`, because the releases run inside the one `ngc` step.

The `ngc` step logs in to `nvcr.io` with the key, pulls each image for the
host architecture, retags it into the local registry under the name the
Helm charts expect, pushes it, and then deploys the releases exactly as the
source lane does. `bring-up-status.py` shows the release stages inside that
one step.

## Appendix C - DPF and the two provisioning modes

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
`--os-install-dwell 5m` to hold each DPU in OS Installing that long while the
other phases keep their pace, `--rebuild` after you changed the simulator's
code, `--repo <checkout>` to build it from another checkout such as a feature
worktree, and `--uninstall` to remove it. Advanced settings live in the site
yaml under `nico-system.dpf`. What the simulator reproduces, what it does
not, and its failure catalog are in section 13 of `mat-in-nico-dev.md`.

## Appendix D - Add-ons after bring-up

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
add-on; it is part of the base site (Appendix C).

## Appendix E - Learning the fabric

The simulated fabric is a good place to learn EVPN networking. On the VM,
`ndev.py ~/mac/sites/dc1/dev1 fabric shell` lists the switches, and
`fabric shell spine-1` drops you into that switch's vtysh console. Try
`show bgp summary`, `show ip route` and `show bgp l2vpn evpn`. Break anything
you like; `sudo systemctl restart nico-dev-fabric` rebuilds the whole fabric
from the site yaml. Newcomers should start with `networking-primer.md`.

## Appendix F - Troubleshooting

- **The VIP refuses connections on a fresh site while all pods are
  Running.** Check `kubectl -n nico-system get endpoints nico-api`. If it is
  empty, the API is not serving. Run `kubectl -n nico-system rollout restart
  deployment/nico-api`; the VIP answers about 30 seconds later.
  `first-boot.sh` and `onboard-golden.sh` already do this once.
- **The route is missing.** If `netstat -rn | grep 11.133` prints nothing,
  re-add the route from Step 6. It disappears every time the last VM stops.
- **The `keycloak` step fails, the pod restarts, `kubectl -n nico-rest describe
  pod -l app=keycloak` shows `Container keycloak failed liveness probe, will be
  restarted`.** The upstream Deployment's liveness probe (60 s delay, 30 s
  period, three failures) kills the container about 150 s after start if
  port 8080 is not open yet, and Keycloak's first start on a starved VM (a
  Mac under load, a VM below the sizing in the Assumptions) takes longer
  than that: Quarkus augmentation alone is 75 s on a healthy VM. Each kill
  restarts from zero, and the setup script's 180 s rollout wait fails. A
  kill that lands during the schema migration also produces the next
  symptom. Recover by resetting the database, widening the probes and
  letting one clean start happen (kubectl on the Mac or the VM):

  ```bash
  kubectl -n nico-rest scale deploy/keycloak --replicas=0 && kubectl -n nico-rest wait --for=delete pod -l app=keycloak --timeout=120s
  PG=$(kubectl get pods -n postgres -l app=postgres -o jsonpath='{.items[0].metadata.name}')
  kubectl exec -n postgres "$PG" -- psql -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='keycloak';" -c "DROP DATABASE keycloak;" -c "CREATE DATABASE keycloak;" -c "GRANT ALL PRIVILEGES ON DATABASE keycloak TO keycloak;"
  kubectl exec -n postgres "$PG" -- psql -U postgres -d keycloak -c "GRANT ALL ON SCHEMA public TO keycloak;"
  kubectl -n nico-rest patch deploy keycloak --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":300},{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":120}]'
  kubectl -n nico-rest scale deploy/keycloak --replicas=1 && kubectl -n nico-rest rollout status deploy/keycloak --timeout=600s
  ```

  then resume **past** Keycloak: `bring-up.py --config X --from temporal` on
  the source lane, or `deploy-dev-nico.py <site> --tag <images.tag from the
  site yaml> --skip-to temporal` on the NGC lane, where the releases run
  inside the one `ngc` step. The `keycloak` step is only the upstream
  script, and re-running it would re-apply the 60 s probe and roll the pod
  again. Tracked as issues.md 20260927-#4 (nico-dev to patch the probes
  itself; upstream asked for a `startupProbe`).
- **Keycloak in CrashLoopBackOff** and `kubectl -n nico-rest logs
  deploy/keycloak` ends in `Failed to update database … column "…" of
  relation "…" already exists`: a first start was interrupted (the probe
  kill above, a Mac sleep, a VM reset) part way through its schema
  migration, so one change was applied but not recorded, and every start
  re-applies it. It is the dev identity provider with nothing in it yet: the
  same drop-and-recreate as above, then the same resume. A clean Keycloak
  start takes about 90 s on a healthy VM.
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

## Appendix G - Maintainers

- **Bake and export a golden image.** Appendix I, step by step.
- **Smoke test** before pushing any change to the cloud-init seed, the VM
  creation record, or `prepare-vm`: `smoke-test.sh` takes about six minutes
  on a throwaway VM.
- **Tag a validated tip** after a full bring-up:
  `git tag validated-YYYYMMDD && git push origin validated-YYYYMMDD`. The
  stable graft channel serves the newest such tag.

## Appendix H - Script reference

| Script | Runs on | Does |
|---|---|---|
| `check-prereqs.sh [--build]` | Mac | read-only prerequisite check, includes checkout parity |
| `check-parity.py [repo] [--quiet]` | Mac | does the checkout still match what the nico-dev scripts assume |
| `image_delivery.py <site> <ref>… [--check]` | Mac | put images into the VM's containerd through the share (never through the registry tunnel) |
| `onboard-golden.sh --zip Z --dest D` | Mac | golden image ZIP to running site, hands off |
| `bring-up.py --config X [--dry-run] [--from step]` | Mac | the whole bring-up |
| `bring-up-status.py --config X [--once]` | Mac | high-level progress of that bring-up in a second terminal: steps done/running/failed, per-step time, what the current step is doing, ssh/kubeconfig/URL, resume command |
| `ngc-tags.py --config X` | Mac | deployable NGC tags |
| `build-dev-nico.py <site> --tag T` | Mac | build images, push to the colima registry |
| `deploy-dev-nico.py <site> --tag T` | Mac | full helm deploy, resumable |
| `redeploy-dev-nico.py <site> --tag T` | Mac | roll the nico release to a tag |
| `deploy-flow.py <site> --config flow.yaml [--status\|--uninstall]` | Mac | Flow add-on, from its own standalone config |
| `deploy-dpf-sim.py <site> [--phase-dwell T] [--os-install-dwell T] [--repo DIR] [--uninstall]` | Mac | DPF simulator (default site; the `dpf` bring-up step) |
| `build-nico-clis.py <site> [--mat-only] [--repo DIR --out-dir DIR]` | Mac | MAT in a container; admin-cli and nicocli with host toolchains |
| `configure-clis.py <site>` | Mac | certs, MAT config, wrappers, /etc/hosts |
| `get-admin-cli.sh <site>` | VM | admin CLI from the API container, no build |
| `get-nicocli.sh <site>` | Mac | REST CLI from the REST API image, no build; writes `run-nicocli.sh` |
| `run-admin-cli.sh`, `run-nicocli.sh`, `run-mat.sh` | Mac / VM | generated wrappers in the site folder |
| `reset-mat-state.py <site> --yes` | Mac | fleet back to time zero |
| `monitor-mat.py --admin-cli W [--mat-log F]… [--no-dpf]` / `run-monitor-mat.sh` | VM | a MAT run on one overview page plus one page per section (endpoints, machines, DPUs, DPF, MAT; digits or ←→ switch, ↑↓ scroll): expected machines, endpoints, machine states with milestones to go, DPUs (NICo and DPF phases), MAT's own view; refresh 30 s |
| `ndev.py <site> [sub]` | Mac or VM | status, fabric, BGP, registry, DPU |
| `restart-ordered.sh [--cold]` | VM | ordered recovery after a reboot |
| `first-boot.sh` | VM | personalise a golden clone |
| `bake-golden-image.sh <site yaml>` | VM | prepare a VM for export |
| `smoke-test.sh` | Mac | maintainers: boot-path check on a throwaway VM |

## Appendix I - Make a golden image

Turn a working site into a `.utm` bundle that a colleague imports with
Appendix A. The whole cycle is bake on the VM, export on the Mac, keep the
export pristine.

Preconditions, all checked by the bake script, which refuses otherwise: every
pod Running or Completed, Vault in file mode (the default), MAT stopped and
the fleet reset with `reset-mat-state.py <site> --yes`.

Bake, on the VM:

```bash
sudo bash ~/mac/infra-controller/tools/nico-dev/bake-golden-image.sh ~/mac/sites/dc1/feature1/feature1.yaml
```

It saves a canonical copy of the site yaml to `/etc/nico-dev/dev.yaml` and
the whole nico-dev script bundle to `/usr/local/lib/nico-dev/`, so the
recipient can run `first-boot.sh` before any share is mounted. It clears
`/etc/nico-dev/env`, which `first-boot.sh` writes fresh per user. It resets
the `nico` user for distribution: password `Welcome123!`, empty
`authorized_keys`, password authentication on. It wipes MAT's runtime
residue (staged binaries, your client certificates, logs), cleans the Docker
build cache, the apt cache, the journal and the shell history, and ends with
the gates: pods Running, images cached in containerd.

Export, on the Mac:

1. Shut the VM down cold: `ssh nico@192.168.64.126 sudo shutdown -h now`.
2. **Remove the shared directory from the VM's UTM settings** before
   exporting. Otherwise your Mac path is baked into the bundle's
   `config.plist`, which both leaks it and trips the importer. Add it back
   after the export.
3. In UTM, right-click the VM and choose **Share…**, UTM's export, and save
   it as `nico-dev-golden-YYYYMMDD.utm`. The result is a bundle, a folder
   Finder shows as one file, holding one qcow2 disk and `config.plist`,
   about 24 GB. Zip it for distribution.

Treat the export as a pristine master. Never boot it: booting a copy mutates
it and `first-boot.sh` personalises it. Give every test or import its own
copy, which is instant and free on APFS because it is copy-on-write:

```bash
cp -cR nico-dev-golden-YYYYMMDD.utm ~/nico-tests/golden-test/golden-test.utm
```

Import-test rules, learned the hard way: only one nico-dev VM booted at a
time, since they all carry the same address; a fresh test means a fresh copy
of the master; and stopping the last UTM VM destroys `bridge100`, so macOS
flushes the VIP route with it, every export cycle.

Note: **returning the builder VM to development.** Baking turns the VM from
developer-ready into image-ready. Booted again after the bake it comes up
without a fabric, because the cleared env makes the boot service deploy
nothing, which looks like "metallb broken". To use it for development again,
on the VM restore the site env and the fabric, and on the Mac restore
passwordless ssh and the route:

```bash
echo 'NICO_DEV_SITE=/home/nico/mac/sites/dc1/feature1/feature1.yaml' | sudo tee /etc/nico-dev/env && sudo systemctl restart nico-dev-fabric
```

```bash
ssh-copy-id nico@192.168.64.126 && sudo route -n add -net 11.133.1.0/27 192.168.64.126
```

MAT's staging heals itself on the next `run-mat.sh`; everything else the
bake changed is harmless for development.

Friction is a bug. If a step confused you, or an error message did not get
you out of trouble, that is a defect in this tooling. Please report it.
