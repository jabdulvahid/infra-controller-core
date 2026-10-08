# nico-dev on a Linux host, step by step

Updated 2026-10-01.

nico-dev gives you a complete NICo development environment inside one virtual
machine on a Linux host. The VM runs under libvirt/KVM and contains a
Kubernetes cluster, the NICo software stack, a simulated datacenter network
fabric built from FRR routers, and MAT, the simulator that plays a rack of
servers for NICo to manage. At the end of Step 7 you have a working NICo site
that you can develop against, break, and rebuild.

NICo runs as a set of container images inside the VM's Kubernetes cluster.
There are two ways to get those images:

- **Build them yourself.** The bring-up compiles the NICo source in your
  checkout into images and pushes them to a small registry on the host, which
  the VM pulls from. Everything is local; you need no account anywhere. The
  first build of a checkout takes 20 to 40 minutes on a current desktop,
  later ones minutes, because the compiler's output is kept between builds
  and only what changed is recompiled. Use this when you change NICo code,
  or when you have no NGC access.
- **Use pre-built images from NGC.** NVIDIA's CI publishes every merged
  build to the NGC container registry, `nvcr.io`. The bring-up pulls the tag
  you name, retags it into the same local registry, and deploys it. Nothing
  is compiled, so a site is up in about 13 minutes on the corporate network,
  but you need an NGC API key with read access to the team that publishes
  them, and you can only run what CI has built.

The steps below follow the first way: build the VM yourself and deploy NICo
from images built out of your own checkout. The NGC way differs in one
block of one yaml file and one step of the run; Appendix A has it. After it,
come back to Step 6. Golden images, a ready-made VM that a colleague imports
and boots, exist for the Mac lane today (`how-to-mac.md`, Appendix A); there
is no Linux equivalent yet.

Supported hosts are Ubuntu 22.04 and 24.04, x86_64 or aarch64, with
hardware virtualization.

**How nico-dev is put together.** Two machines take part: your Linux host
and one Ubuntu VM that libvirt runs on it. The host does the heavy lifting
that does not need to be inside the cluster: it builds the NICo images and
the MAT binary in Docker containers, runs the small image registry the VM
pulls from, and runs the nico-dev scripts that drive the whole bring-up over
ssh. The VM holds everything that has to run together as a site: the
Kubernetes cluster, the NICo services, the simulated network fabric, and MAT
with its mock BMCs, which need the fabric's bridges and therefore cannot run
on the host. The two are joined by a shared folder on the host that the VM
mounts over virtiofs, and that folder is where every artifact crosses over:
your source checkout with the nico-dev tools inside it, the site's
configuration and certificates, the kubeconfig, the MAT binary and its
config, and image tarballs on their way into the VM. A script started on the
host can therefore hand a file to the VM by writing it to the share, and a
script on the VM finds the same file under `~/mac`; the mount name is
historical, from the Mac lane, and means "the host". For that to work in
both directions, files need the same owner on both sides. virtiofs passes
real user ids through, so the VM's login user, `nico`, is created with the
same numeric user id as your host account; the VM builder reads your UID and
passes it into the VM's cloud-init, and `bringup.yaml` has a `uid` key only
for the rare case where you want another value. A file the VM writes into
the share therefore shows up on the host as yours, and a file you write on
the host is `nico`'s in the VM, with no permission fixing on either side.
Your host reaches the services inside the VM through one route to the VM's
address, so `kubectl` and the admin UI work from the host as if the cluster
were local, and from your laptop through an ssh tunnel to the host.

A few words used throughout:

- **The share** is a folder on your host that the VM mounts. Files you put
  there are visible inside the VM, and files the VM writes there are visible
  on the host. Everything nico-dev creates lives in the share.
- **A site** is one deployed NICo installation, named by a datacenter name
  and a site name, for example `dc1/feature1`.
- **A lane** is where the NICo container images come from: built from your
  own source checkout (this page's main path), or pulled pre-built from
  NVIDIA's NGC registry.
- **A VIP** is a virtual IP address inside the VM that a NICo service
  answers on. Your host reaches them through one route, added in Step 6.
- **MAT**, machine-a-tron, is the simulator that plays the hardware NICo
  manages. It runs on the VM and presents a rack of mock servers: for each
  one a Redfish BMC with power control, firmware and virtual media, a DPU,
  and the DHCP and boot traffic a real host would produce. NICo discovers,
  ingests and provisions them exactly as it would real machines, and MAT
  also drives the switches and power shelves of the simulated rack. Its
  fleet is defined in `mat-config.toml` (Step 10).
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
  and runs through the wrapper `run-nicocli.sh` (Step 9).
- **DPF**, the DOCA Platform Framework, is how NICo provisions the DPU in
  each host. A nico-dev site runs a DPF simulator instead of the real
  operator (Appendix B).

**Two configuration files.** You write one, `bringup.yaml`, in Step 3. It
holds the few decisions that are yours: the VM's name and size, your
datacenter and site names, two network octets, where the images come from,
and a handful of switches such as DPF or the firmware simulation. Copy it
from `bringup-example.yaml`, which documents every key. From it the `site`
step generates the second file, the **site yaml**, at
`<share>/sites/<dc>/<site>/<site>.yaml`. That is the complete description of
the site: every network prefix, the service VIPs, the Helm values, the MAT
fleet, the DPF and firmware settings. Every later script, on the host and on
the VM, reads the site yaml, never `bringup.yaml`. You normally do not edit
it; advanced settings that have no `bringup.yaml` key live there, and after
editing one you regenerate the values with `bring-up.py --from nico --until
nico`, as Step 11 shows for the firmware versions.

**One checkout.** The site is built from one checkout, the `repo:` of your
`bringup.yaml`: the images, the Helm charts, the DPF simulator, MAT and the
CLIs all come from it, on whatever branch it has checked out. Any folder or
git worktree inside the share works, so the branch you are developing on
can be the site's checkout, with nothing copied between trees: edit there,
rebuild only what changed, run. Step 2 creates it that way, and Step 10 and
Step 12 are the rebuild loops for MAT and for NICo.

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
checkout still look the way nico-dev assumes, and whether upstream main has
changed `setup.sh`, `helm/` or `helm-prereqs/` since your branch left it.

**The addresses.** The VM sits on a libvirt NAT network of its own,
`nico-nat`, bridge `virbr-nico`, which the VM builder creates and records.
It is `192.168.64.0/24` by default and the VM is `<subnet>.<host_num>`;
`host_num` is a `bringup.yaml` key with the default 126, so this page writes
`192.168.64.126` throughout, and a second VM gets another `host_num`. The
host is `.1` on that network, which is where the VM finds the image
registry. If `192.168.64.0/24` is taken on your host, set `subnet:` in
`bringup.yaml` to another first three octets, for example `192.168.77`; the
builder and every later step use it. Everything inside the simulated site
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
`O.135.0.0/16`. Pick two octets that nothing on your host or VPN uses; the
dry run warns if the host already routes them. A second site on the same
host needs its own pair, its own VM name and its own `host_num`.

## Assumptions

1. A Linux host running Ubuntu 22.04 or 24.04 with hardware virtualization
   enabled. Two things on it share the host's cores and memory: Docker,
   which builds the images, and the libvirt VM that runs the site. The build
   uses the host directly, so start from what the host has:

   ```bash
   lscpu | grep -E 'Model name|^CPU\(s\)'; free -g | awk '/Mem:/ {print $2 " GB RAM"}'; lsblk -d -o NAME,ROTA,SIZE
   ```

   `ROTA 0` is an SSD; the build and the VM both want one. The build runs
   cargo with as many parallel jobs as the host allows, capped at one job
   per 3 GB of memory, and prints the number it chose; a 62 GB host gets 20
   jobs. Size the VM in Step 3 from this table:

   | Host | Lane | VM (`vm:` in bringup.yaml) | Notes |
   |---|---|---|---|
   | 16 GB, 4 cores | NGC only, REST off | `cpus: 4`, `mem_mb: 8192` | runs Core, DPF and MAT; set `nico-system.rest.enabled: false` in the site yaml before the first deploy (the REST stack adds about 3 GB inside the VM); do not source-build here |
   | 32 GB, 8+ modern cores, NVMe | NGC or source, full stack | `cpus: 6`, `mem_mb: 12288` | a sound development machine |
   | 32-48 GB, 4 cores of 2012 vintage (i7-3xxx) | NGC or source, full stack | `cpus: 6`, `mem_mb: 20480` | works, slowly: the first source build is 2 to 3 hours instead of 20 to 40 minutes (a 2012 core is about a third of a current core, and hyperthreads add little), and Keycloak's first start is the step most likely to trip its liveness probe (Appendix E). Memory is not the limit here, so REST can stay on |
   | 64 GB, 24 threads (i9-13900K class), NVMe | source, several sites | `cpus: 12`, `mem_mb: 24576` | this page's example: the first build about 20 minutes, redeploy cycles and MAT at full size |

   What the 16 GB line is based on (2026-09-26/27, a 2017 quad-core laptop):
   the first source build swapped the VM's memory and hung the desktop; the
   full stack with REST then needed more than the 8 GB the VM could be given
   while leaving the host 6 GB, and the VM's kernel OOM-killed pods during
   the REST releases. Core plus DPF plus MAT fits; the REST stack does not.

   The VM values are ceilings, not reservations, but the host must hold the
   VM plus about 8 GB for a source build plus its own OS. Disk: the VM disk
   is sparse, 120 GB nominal, about 25 GB after an NGC bring-up; the source
   build's caches take tens of GB more. Plan 150 GB free for development.
2. **Ubuntu Server, not Desktop.** A desktop session takes 1.5 to 2 GB and
   CPU the build needs, and its idle timer suspends the machine. If you must
   use a desktop install, switch to text mode:
   `sudo systemctl set-default multi-user.target`.
3. **No suspend, ever, while a site runs.** A suspended host freezes the VM;
   on resume the guest clock jumps, etcd leases and leader locks expire and
   the control plane restarts. Step 1 masks it and ignores the lid.
4. You can reach `github.com/dsx-ai-factory/infra-controller`, and you have
   an SSH keypair in `~/.ssh`, or you generate one in Step 1. The public key
   is installed into the VM so the scripts log in without a password.
5. This page assumes one nico-dev VM on the host. Several can coexist, but
   each needs its own address on `nico-nat` (`host_num`), its own VM name,
   its own folder, and its own `underlay` and `overlay` octets; two VMs that
   share any of these collide. If you have an old one you no longer need,
   tear it down first (Step 13).
6. Rust, cargo and Go are **not** required on the host. Every build nico-dev
   performs, the NICo images, the MAT binary and the two command-line
   clients, runs inside a Docker container, and on Linux the clients it
   builds run on the host as they are. Step 8 and Step 9 get both clients
   without building anything at all.

## Step 1 - Install the host tools

On the host:

```bash
# libvirt runs the VM; virt-install creates it; cloud-image-utils builds the first-boot seed;
# virtiofsd serves the share. git and python3 run the nico-dev scripts; docker builds the images.
sudo apt install libvirt-daemon-system libvirt-clients virtinst cloud-image-utils qemu-utils virtiofsd docker.io git python3 python3-yaml
sudo usermod -aG libvirt,kvm,docker $USER
# Log out and back in, not just a new terminal, so the group memberships apply.

# The source build also needs docker buildx on the host, and helm and kubectl to talk to the cluster.
sudo apt install docker-buildx && sudo snap install kubectl --classic && sudo snap install helm --classic

# Virtualization must be on in the firmware. If this says "KVM acceleration can NOT be used",
# enable Intel VT-x or AMD SVM in the BIOS and reboot.
kvm-ok

# Never let the host sleep under a running site.
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/; s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf && sudo systemctl restart systemd-logind

# Only if you have no SSH keypair yet. The public key is installed into the VM.
ssh-keygen -t ed25519
```

A `ufw` default-deny host works unchanged: Docker and libvirt insert their
own accept rules ahead of it.

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
worktree on the branch you want to run. That worktree is the site's
checkout: what you build, deploy and edit is one tree. On the host:

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

Note: the fetch matters. The images you deploy, the Helm charts, the DPF
simulator's RBAC and the simulator's own source all come from this
worktree, so all of them are from the same commit; that is the point of one
checkout. What can still drift is upstream: `check-prereqs.sh` below reports
whether upstream main has changed the deployment inputs, `setup.sh`,
`helm/` and `helm-prereqs/`, since your branch left it, and prints the
rebase command. Being ahead of main on your own branch is normal and is not
reported.

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
check-prereqs.sh             # base tier: libvirt, KVM, docker, network, 40 GB disk
check-prereqs.sh --build     # also the source-build tier: kubectl, helm, docker buildx, 100 GB disk
```

You should see every line marked ✓. The check only reads; it changes
nothing. Fix every ✗ line; each says how.

Note: the last section of the check, "checkout parity", is about the
repository, not your host. The nico-dev scripts re-implement parts of the
upstream setup and therefore assume certain chart paths, resource names and
defaults in the checkout. A ✗ there means a nico-dev script needs updating,
not your repository; report it. The informational line about upstream main
lists deployment-input files upstream changed since your branch left main;
Appendix A says when that matters.

## Step 3 - Write bringup.yaml

Everything the bring-up needs to know goes into one file. Copy the example
and edit it; the example documents every key. On the host:

```bash
cp bringup-example.yaml bringup-feature1.yaml
vi bringup-feature1.yaml
```

For the source-build lane it looks like this:

```yaml
name: vm-feature1
user: nico
password: Welcome123!
ssh_key: ~/.ssh/id_ed25519.pub

vm:
  cpus: 12            # sized in the Assumptions: 12 CPUs and 24 GB on a 62 GB, 24-thread host
  mem_mb: 24576
  disk_gb: 120

redeploy:
  on_insufficient_cpu: scale-down-first   # lets a rolling redeploy proceed on a tight node

dpf: true           # default: DPF provisioning + the DPF simulator; false = legacy iPXE path
firmware_sim: true  # nico-api gets a firmware definition and autoupdate, MAT reports old versions (Step 11); default false
build_profile: dev  # cargo profile of the source build, required: dev = Tilt build (minutes per change), release = the shipped build (Step 12)

dc: dc1
site: feature1
underlay: 11        # first octets of the fabric prefixes: pick two your host and VPN do not use
overlay: 12

# subnet: 192.168.77  # only if 192.168.64.0/24 is taken on your host
# tag: feature1-abc   # the label for the images you build; default `git describe` of the checkout (= the NICo version string)
```

What the fields mean:

- `name`, `user`, `password`, `ssh_key`: the VM's name in libvirt, the login
  account created inside it, and the public key that account accepts. The
  password is normally never asked; the VM builder authorizes your key.
- `vm`: the VM's size. Use the table in the Assumptions.
- `redeploy`: what to do when a later redeploy cannot fit a new pod on a
  full node. `scale-down-first` lets it proceed (Step 12).
- `dpf`: `true`, the default, gives you NICo's DPF provisioning path
  together with the DPF simulator. `false` gives the older iPXE path.
  Appendix B explains both.
- `firmware_sim`: a scenario switch that configures both sides. `true`
  gives nico-api a firmware definition for the mock GB200 with
  `firmware_global.autoupdate` on, and makes MAT hosts report older
  versions, so ingestion runs the upgrade chain. `false` leaves nico-api
  without any firmware definition, the standard MAT flow. Leave it out
  unless you work on that (Step 11).
- `firmware_sim_delivery`: how nico-api gets that definition. `legacy`,
  the default, writes it into the pod with an init container. `api` writes
  nothing: you load it after bring-up through the REST API, the way a real
  site is configured (Step 11).
- `build_profile`: the cargo profile of the source build, and the runner
  refuses to build without it. `dev` is the Tilt build, debug and
  incremental, minutes per change; `release` is the shipped, optimized build
  and several times slower. Not used with `ngc:` (Step 12).
- `dc`, `site`: the names of your datacenter and site. They appear in
  folder names and in the cluster. `dc` is 1-3 characters, `site` 1-8.
- `underlay`, `overlay`: two numbers that become the first octet of every
  network prefix inside the VM. Pick two numbers that nothing on your host
  or VPN uses. With `underlay: 11` the admin UI ends up at `11.133.1.17`.
  The dry run warns you if your host already routes them.
- `subnet`, `host_num`: the VM's address is `<subnet>.<host_num>`,
  `192.168.64.126` by default. Set `subnet` only when that network is taken
  on your host; set `host_num` for a second VM.
- `repo`: the site's checkout inside the share, by default the folder these
  tools were grafted into, which is the worktree from Step 2. Name another
  folder or worktree inside the share to build and deploy from that one
  instead.
- `tag`: a label for the images you build. Without it, bring-up uses the
  checkout's `git describe --tags --always --dirty`, for example
  `v2.4.0-pr-102-g8c8c94d9f`, which is also the string NICo reports as its
  version in the admin UI, so the registry tag and the running version
  always agree; `-dirty` marks uncommitted changes, and a new commit is a
  new tag by itself. Set it to rebuild the same commit under another name;
  a deployed tag is never rebuilt or redeployed.

## Step 4 - Dry run

On the host:

```bash
bring-up.py --config bringup-feature1.yaml --dry-run
```

You should see every prerequisite marked ✓, ✗ or ⚠, the numbered plan with
the exact commands, and the verdict `READY`. Nothing is executed during a dry
run. `NOT READY` lists the lines to fix; fix them and run the dry run again.

## Step 5 - Bring the site up

On the host:

```bash
bring-up.py --config bringup-feature1.yaml
```

The run goes through 21 steps: `vm` → `prep` → `site` → `fabric` → `cp` →
`build` → `registry` → one step per NICo Helm release, seven `core` ones
(local-path-provisioner, cert-manager, vault, external-secrets,
postgres-operator, nico-prereqs, nico) then five `rest` ones (rest-postgres,
keycloak, temporal, nico-rest, nico-rest-site-agent) → `dpf` → `route`.
`bring-up.py --list` shows them. The `build` step compiles the NICo images
from your worktree: 20 to 40 minutes the first time, minutes afterwards.

The run stops and waits for you once, for `sudo` on the host at the end, to
add the route to the service addresses. There is no GUI step; `virt-install`
sets the share path. Nothing else asks: the VM builder's cloud-init seed
authorizes your SSH key and gives the user passwordless sudo, so `prep` logs
in with the key; the `password:` in the config is only a fallback for a VM
built another way. The host-key question is answered automatically for a new
address, and the `iptables-persistent` save dialogs inside the VM are
pre-answered.

The runner's output is the raw output of every command it runs, which is
long and says little about where the run stands. Open a second terminal:

```bash
bring-up-status.py --config bringup-feature1.yaml          # redraws every 2 s; Ctrl-C leaves the run alone
bring-up-status.py --config bringup-feature1.yaml --once   # one snapshot, for pasting
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
the one most likely to fail on a slow or loaded host; Appendix E has the
recovery.

You should see the run end with a URL, `https://11.133.1.17/admin` for
`underlay: 11`.

## Step 6 - kubectl, the route, and where things are

On the host, point kubectl at the cluster. The route to the service
addresses was added by the run; this is the command for when it is gone:

```bash
export KUBECONFIG=~/nico-tests/vm-feature1/shared/sites/dc1/feature1/dc1-feature1.kubeconfig.yaml   # in your shell profile
kubectl get nodes

sudo ip route replace 11.133.1.0/27 via 192.168.64.126   # route to the service VIPs
ip route get 11.133.1.17                                 # via 192.168.64.126
```

Note: **the route does not survive a host reboot.** Re-add it with the same
command; `restart-ordered.sh` prints it for you.

Where things are, seen from both sides:

| | Host | Inside the VM |
|---|---|---|
| share root | `~/nico-tests/vm-feature1/shared` | `~/mac` (the mount name is historical) |
| repo worktree | `<share>/infra-controller` | `~/mac/infra-controller` |
| site folder | `<share>/sites/dc1/feature1` | `~/mac/sites/dc1/feature1` |
| image tarballs in transit | `<site>/images/*.tar` (deleted after import) | same path under `~/mac` |
| kubeconfig | `<site>/dc1-feature1.kubeconfig.yaml` | same path under `~/mac` |
| local registry | `localhost:5000` | `192.168.64.1:5000` |

Inside the VM the repository is a git worktree whose metadata lives on the
host. Do your git work on the host, not in the VM.

`ndev.py` shows status. Run it on the host for cluster status, or on the VM
for the full fabric health, because the fabric only exists inside the VM:

```bash
ndev.py <site>                                                       # host: cluster status; fabric/BGP/DPU n/a (VM-side)
ssh nico@192.168.64.126 'ndev.py ~/mac/sites/dc1/feature1 fabric verify' # VM: full fabric health
```

The subcommands are `fabric verify|info|shell [switch]`, `bgp info
[--detail]`, `cluster info`, `registry verify`, and `dpu info`.

## Step 7 - Verify

On the host:

```bash
kubectl -n nico-system get pods        # all Running or Completed
curl -k https://11.133.1.17/           # "Forge development build"
```

The admin UI is `https://11.133.1.17/admin`. On a headless host, reach it
from your laptop through the host:

```bash
ssh -L 8443:11.133.1.17:443 <linux-host>     # then https://localhost:8443/admin
sshuttle -r <linux-host> 11.133.1.0/27       # if the login flow redirects to the VIP itself
```

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
`clis-mat-in-nico-dev.md`; Steps 8, 9 and 10 are the summary.

Note: on a Linux host the CLIs can also be built. `build-nico-clis.py <site>
--install-to ~/.local/bin` builds `nico-admin-cli`, `nicocli` and MAT in
containers from the site's checkout; the two clients are Linux binaries
that run on the host, and `configure-clis.py <site>` then issues their
certificates, writes `run-admin-cli.sh` and `run-mat.sh`, and adds the API
hostname to `/etc/hosts`, so the wrapper works on the host too. Use it when
you change a CLI; otherwise this step and Step 9 are quicker.

## Step 9 - nicocli, without compiling anything

The REST API has its own CLI, `nicocli`, and the REST API image ships it,
built from the same commit as the API. Four steps get it working.

Extract the CLI, the same way as the admin CLI in Step 8. The image is
distroless, so the binary cannot be copied out of the running pod; the
script mounts the image's filesystem instead, copies the binary out and
writes a wrapper. It takes a few seconds and runs on either side: on the VM
it takes the image from containerd, on the host from docker's store:

```bash
get-nicocli.sh <site>                           # host, from docker
ssh nico@192.168.64.126 'get-nicocli.sh ~/mac/sites/dc1/feature1'   # or on the VM, from containerd
```

You should see three steps end with a check mark, then a "Done" block with
the two commands to run next. The results are `<site>/nicocli/nicocli`, a
Linux build that runs on the host and in the VM alike, and
`<site>/run-nicocli.sh`, the wrapper, both on the share.

Bootstrap the organisation, once per fresh site:

```bash
<site>/run-nicocli.sh --bootstrap
```

The first call pauses a few seconds while a token is minted inside the
cluster. Then two commands run, `infrastructure-provider current` and
`tenant current`, each printing a JSON object: they create the
organisation's provider and tenant. Until this has run once, every other
command answers "Org does not have a Tenant associated".

Use it:

```bash
<site>/run-nicocli.sh vpc list
<site>/run-nicocli.sh --help
```

Always go through the wrapper. It supplies the API address, the organisation
`ncx` and the token; the bare binary knows none of them. The token is cached
for 25 minutes and renewed on its own. If a command ever fails with an
authentication error, put `--refresh-token` in front of it once.

When to repeat what: extract again after you deploy a new nico tag, so the
CLI matches the API. Bootstrap never again for this site. Nothing to do
between MAT runs; the REST side is untouched by the fleet reset.

## Step 10 - Build and run MAT

MAT is built in a container on the host and delivered to the VM through the
share. On the host:

```bash
build-nico-clis.py <site> --mat-only       # machine-a-tron only
configure-clis.py <site>                   # certs for admin-cli and MAT, mat-config.toml, run-mat.sh, /etc/hosts entry
```

The first builds the MAT binary into `<site>/mat/machine-a-tron` from the
site's checkout; the first build compiles the whole crate and takes ten to
twenty minutes, later ones a minute or two, since the build keeps its cargo
target directory in a Docker volume per checkout. Next to the binary it
writes `BUILD_INFO`, one line naming the commit, branch and time it was
built from. The second issues certificates for the admin CLI and MAT, writes
the fleet definition `<site>/mat/mat-config.toml`, generates the wrapper
`<site>/run-mat.sh`, and adds the API hostname to `/etc/hosts`.

MAT runs on the VM only. It puts the addresses of its mock BMCs on the fabric
bridge `br-dc1-internet`, which exists only inside the VM, and NICo's
site-explorer connects to those addresses. On the VM:

```bash
ssh nico@192.168.64.126 '~/mac/sites/dc1/feature1/run-mat.sh'
```

`run-mat.sh` prints the `BUILD_INFO` line, copies the binary, the
certificates and the configuration to local disk on the VM, installs the
binary, stages everything under `/etc/machine-a-tron/dc1/`, and starts MAT
under `sudo` in the background; the terminal then shows the MAT run monitor,
so the run is watched from its first second. Leaving the monitor with `q`
does not stop MAT: `run-mat.sh --status` says whether it runs, `run-mat.sh
--stop` stops it, and `run-mat.sh --no-monitor` starts it without the
monitor. The log is `/var/log/machine-a-tron-dc1.log` on the VM. You should
see machines appear with `run-admin-cli.sh machine show` (no argument lists
them all). To bring the monitor back, or to watch from a second VM terminal:

```bash
~/mac/infra-controller/tools/nico-dev/run-monitor-mat.sh ~/mac/sites/dc1/feature1
```

It shows expected machines, endpoints, machine states with the milestones
still to go, DPUs, DPF phases and MAT's own view, one page per section
(digits or ←→ switch pages, `?` for help), refreshing every 10 s. The header
says whether a MAT process is running and which commit the binary was built
from, and the MAT page shows how long ago the log was last written, so a MAT
that died or was stopped does not look like a quiet one. Page 7 is the
timeline: per object, its states in order with how long each was held. For
machines, hosts and DPUs alike, it reads NICo's own state history
(`machine show <id> -c 250`), which is complete, exactly timed and starts
with the machine, so a run that began before the monitor is shown in full.
Endpoints and DPF phases have no history in NICo; for those the monitor keeps
a diary of what each 10 s poll saw, page 6 lists it in order, and
`<site>/monitor-mat-history.log` keeps it for reading after the run, which
is how you reconstruct a host's path through the pre-ingestion firmware
states. `reset-mat-state.py` writes a run marker into that file so runs do
not blend. Machine ids carry the BMC address a person remembers,
`[host 11.140.2.3]` or `[dpu 11.140.2.2]`, and `/` on page 6 or 7 narrows
them to one object: type the address and you get the endpoint and the
machine it became. Each object's current state carries its expected hold,
from the GB200 profile scaled by `acceleration_factor` in `mat-config.toml`
plus NICo's 30 s passes, and is coloured on time, over, or well over, so a
stall is visible without knowing the timings by heart. It finds the MAT log
by the site's `dc` name; `--mat-log <file>` pins a specific one.

**Between MAT runs**, after stopping MAT and before starting it again, reset
the fleet, on the host:

```bash
reset-mat-state.py <site> --yes
```

NICo remembers the fleet it has seen: the endpoints it explored, the
machines it expected, and the BMC credentials it rotated. Without the reset,
a new run against fresh mocks is locked out. MAT's generated config also has
`cleanup_on_quit = true`, so a MAT stopped with Ctrl-C deletes its machines
from NICo on the way out; the reset still clears what that leaves behind.

Note: **working on MAT's source.** The site's checkout is the branch you are
developing on, so there is nothing to copy: edit in the worktree, then
rebuild only MAT:

```bash
build-nico-clis.py <site> --mat-only     # MAT from the checkout's current HEAD → <site>/mat/
```

`run-mat.sh` installs whatever `<site>/mat/` holds and prints which commit
it came from; the monitor shows the same line, so a binary older than your
HEAD is visible. For a custom `mat-config.toml`, for example an
`acceleration_factor` to shorten a run that is not about timing, or a
`[machines.<group>.timing_overrides]` block, edit the generated file or
point the `MAT_CONFIG` line of `run-mat.sh` at another one under the site
folder. When the feature touches NICo itself, Step 12 rebuilds the images
from the same checkout. The details and the failure catalog are in
`mat-in-nico-dev.md`.

## Step 11 - Firmware upgrade simulation

By default nico-api has no firmware definition for the mock GB200 and
`firmware_global.autoupdate` is off, so ingestion never uploads anything,
whatever versions MAT reports. `firmware_sim: true` in `bringup.yaml`
(Step 3) changes that for every host: MAT reports the `initial` BMC and UEFI
versions and nico-api gets a firmware definition for the mock GB200 requiring
the `desired` ones, written into the chart's firmware volume by an init
container, and it turns on `firmware_global.autoupdate` in nico-api's site
config, which gates the preingestion upload as well as later updates;
without it nico-api logs the check and quietly marks the endpoint complete.
The upgrade then runs per host while the endpoint is still in preingestion,
before a machine exists: the site explorer's `Pre-ingestion State` column
walks `Initial` → `InitialBMCReset` → `SetNtpServers` / `TimeSyncReset` →
`UpgradeFirmwareWait` (the Redfish task is polled) → `ResetForNewFirmware`
(a BMC reset for BMC firmware, a host power cycle for UEFI) →
`RecheckVersions`, and only at `Complete` does the host go on to become a
machine and reach Ready. The MAT monitor records every one of those
transitions with its time in `<site>/monitor-mat-history.log` (Step 10),
which is the record to read afterwards. Live, in the nico-api log on the
host:

```bash
kubectl -n nico-system logs deploy/nico-api --timestamps | grep -E "preingestion minimum|firmware upload|task not yet complete|satisfies preingestion"
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

### Loading the definition through the API instead

A real site has no init container: its firmware catalog is loaded after
deployment through the Host Firmware Config API, and the shipped definition
for the mock GB200 lives in the repository at
`helm-prereqs/host-firmware/gb200-nvl-simulation.yaml`. To run the
simulation that way, set `firmware_sim_delivery: api` in `bringup.yaml`. MAT
still reports the `initial` versions and `autoupdate` is still on, but
nico-api starts without any definition, so nothing is uploaded until you
load one. After Step 9 has set up `run-nicocli.sh`, on the host:

```bash
<repo>/helm-prereqs/load-host-firmware-config.py <repo>/helm-prereqs/host-firmware --nicocli <site>/run-nicocli.sh
```

It validates the file, finds the one site the REST API knows, and upserts the
definition; the output names the vendor, model and versions it loaded. Then
start MAT as usual. The `--check` flag validates and prints the request
without calling anything, and `--delete Nvidia 'GB200 NVL'` removes the
definition again. Without `--nicocli` the script runs `nicocli` from `PATH`,
which is what a real site does once `nicocli init` has been run.

The definition's artifacts are two files of this repository on GitHub,
pinned to a commit: the mock ignores the bytes, but nico-api downloads them
before the upload, so the VM needs the same internet access it needed to
pull images, and the URLs must be `https`. The legacy init container writes
local dummy files instead, which is why its definition cannot be loaded
through the API unchanged.

## Step 12 - The dev loop

The source-build cycle: change code in the worktree, build images, roll the
cluster onto them. On the host:

```bash
build-dev-nico.py    <site> --profile dev   # images for the host arch, pushed to the local registry; tag = git describe
redeploy-dev-nico.py <site>                 # helm upgrade of the nico release only, to that same tag
kubectl -n nico-system get pods -w
```

or, through the runner from the same checkout, `bring-up.py --config
bringup-feature1.yaml --from build`, which builds, pushes and redeploys with
a tag derived from the commit and the profile from `build_profile:`.

Six rules:

- **Pick the profile deliberately; the build script insists on it.**
  `--profile dev` is the Tilt build: debug profile, incremental compilation,
  only the packages whose binaries the image ships, no sccache. A one-crate
  change rebuilds in a few minutes and the code runs unoptimized, which the
  simulators do not notice. `--profile release` is the shipped build:
  `cargo build --release` of the whole workspace, optimized, several times
  slower. Use `dev` for the loop and `release` when you want to test what a
  release ships. The profile is recorded on the image as the label
  `io.nico-dev.profile`, and the same-tag refusal below names it.
- **Rebuilds are incremental.** The nico image keeps its cargo target
  directory in a Docker cache mount, one per source checkout, so a rebuild
  after a code change recompiles only the crates that changed and finishes
  in minutes; only the first build of a checkout is the full 20 to 40. The
  two profiles keep separate build outputs in that cache, so switching
  profile once costs a full build, switching back does not.
  `docker builder prune` drops the caches when disk gets tight.
- **Regrafting the tools does not rebuild.** The `nico` image copies the
  whole checkout, and the grafted tools live inside it; since 2026-09-27
  `build-dev-nico.py` keeps `tools/nico-dev/` out of the build context, so
  only source changes recompile. A rebuild that compiles although nothing in
  `crates/` changed means the tools are older than that fix.
- **Use a new tag every time.** Both scripts refuse to rebuild or redeploy a
  tag that is already deployed. If they did not, the cluster would silently
  keep running the old image under the same name. Without `--tag`, all three
  scripts and the runner use the checkout's `git describe --tags --always
  --dirty`, the same string NICo reports as its version, so a new commit is
  a new tag by itself and `machine show`, the admin UI and the registry name
  the same build; the explicit `--tag` is for rebuilding the same commit.
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
--tag <t>` with `--skip-to <release>` or `--only <release>`. Stuck Helm
releases are healed automatically. Never delete the `nico-system` namespace
to recover from a problem; it holds the Helm release state.

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
host route from Step 6. `--dry-run` prints the plan.

Note: a host that suspends freezes the VM, and the recovery afterwards is
the same ordered restart; Step 1 masks suspend for that reason. After a host
reboot, start the VM with `virsh -c qemu:///system start vm-feature1`, re-add
the route, and expect the ordered restart.

To tear down, on the host:

```bash
dev-down.py --config bringup-feature1.yaml                  # domain, volumes, host route, ledger entry
dev-down.py --config bringup-feature1.yaml --remove-infra   # also nico-nat and the pool, when no nico VMs remain
```

Your site folder and worktree are never deleted. Everything nico-dev created
is listed in `~/.nico-dev/vms/<vm>.yaml`.

---

## Appendix A - Deploy pre-built images from NGC

Use this instead of the source build when you do not change NICo code and
want a site quickly: nothing is compiled, and a site is up in about 13
minutes on the corporate network. You need an NGC API key with read access
to the registry of the organisation and team that publishes the NICo images;
ask your team for both. Everything else on this page stays the same; the
differences are one block in `bringup.yaml` and one step of the run.

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

To find a deployable tag, one that tracks main and is published for your
host's architecture:

```bash
ngc-tags.py --config bringup-feature1.yaml                 # newest PR builds tracking main, with host-arch availability
ngc-tags.py --config bringup-feature1.yaml --before v2.3.0 # page back
ngc-tags.py --config bringup-feature1.yaml --group rest    # tags of the REST images
```

Prefer a recent development tag. A fresh site's database schema follows
main, and the migration job refuses to run an older version against a newer
schema, so an old tag on a new site can fail.

Note: the checkout in your share still matters on this lane. The Helm
charts, the DPF simulator and its RBAC come from that checkout, while the
images come from CI's build of a newer upstream. A checkout behind upstream
pairs older charts with newer images, so a chart may lack a value, RBAC rule
or CRD the newer nico-api expects. This is what the "deployment inputs vs
upstream main" line of `check-prereqs.sh` (Step 2) is for: it lists the
`helm/`, `helm-prereqs/` and `setup.sh` files upstream changed since your
branch left main and prints the rebase command. Bring the checkout current
before an NGC bring-up.

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

## Appendix B - DPF and the two provisioning modes

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
code, and `--uninstall` to remove it. Advanced settings live in the site
yaml under `nico-system.dpf`. What the simulator reproduces, what it does
not, and its failure catalog are in section 13 of `mat-in-nico-dev.md`.

## Appendix C - Add-ons after bring-up

Optional components are installed after the site is up, one script per
component, run from the host, at your discretion. Each add-on has its own
config file, complete on its own: nothing in it is taken from `bringup.yaml`
or the site yaml, so a site deployed from NGC can run a locally built add-on
and the other way round. NICo Flow is the first:

```bash
cp flow-example.yaml ~/nico-tests/vm-feature1/shared/flow.yaml    # then edit: source, ngc.registry, ngc.tag
vi ~/nico-tests/vm-feature1/shared/flow.yaml
deploy-flow.py <site> --config ~/nico-tests/vm-feature1/shared/flow.yaml --dry-run
deploy-flow.py <site> --config ~/nico-tests/vm-feature1/shared/flow.yaml
deploy-flow.py <site> --status                             # what is installed, from Helm
deploy-flow.py <site> --uninstall
```

`<site>` is the site folder; the script takes only the kubeconfig from it.
The config names the image source, `ngc` or `build`, and everything that
source needs. Add-ons write nothing into the site yaml: Helm is the record
of what is installed, and removal is the chart's own uninstall. The add-on
implements the current chart and refuses an older checkout with "refresh the
worktree".

The design, the config format shared by every add-on, and the list of other
candidates are in `ADDONS.md` and `deploying-extras.md`. DPF is not an
add-on; it is part of the base site (Appendix B).

## Appendix D - Learning the fabric

The simulated fabric is a good place to learn EVPN networking. On the VM,
`ndev.py ~/mac/sites/dc1/feature1 fabric shell` lists the switches, and
`fabric shell spine-1` drops you into that switch's vtysh console. Try
`show bgp summary`, `show ip route`, `show bgp l2vpn evpn` and
`show running-config`. Break anything you like; `sudo systemctl restart
nico-dev-fabric` rebuilds the whole fabric from the site yaml. Newcomers
should start with `networking-primer.md`.

## Appendix E - Troubleshooting

- **The VIP refuses connections on a fresh site while all pods are
  Running.** Check `kubectl -n nico-system get endpoints nico-api`. If it is
  empty, the API is not serving, not the fabric. Run `kubectl -n nico-system
  rollout restart deployment/nico-api`; the VIP answers about 30 seconds
  later.
- **The route is missing** after a host reboot. `ip route get 11.133.1.17`
  should say `via 192.168.64.126`; if not, re-add the route from Step 6.
- **The `registry` step times out** (not "connection refused"): a firewall
  blocks the VM's path to `192.168.64.1:5000`.
  `sudo ufw route allow in on virbr-nico to any port 5000 proto tcp`.
- **The `keycloak` step fails, the pod restarts, `kubectl -n nico-rest describe
  pod -l app=keycloak` shows `Container keycloak failed liveness probe, will be
  restarted`.** The upstream Deployment's liveness probe (60 s delay, 30 s
  period, three failures) kills the container about 150 s after start if
  port 8080 is not open yet, and Keycloak's first start on a slow or starved
  host takes longer than that: Quarkus augmentation alone is 75 s on a
  healthy VM. Each kill restarts from zero, and the setup script's 180 s
  rollout wait fails. A kill that lands during the schema migration also
  produces the next symptom. Recover by resetting the database, widening the
  probes and letting one clean start happen (kubectl on the host or the VM):

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
  kill above, host swap, suspend, a VM reset) part way through its schema
  migration, so one change was applied but not recorded, and every start
  re-applies it. It is the dev identity provider with nothing in it yet: the
  same drop-and-recreate as above, then the same resume. A clean Keycloak
  start takes about 90 s on a healthy VM.
- **An image pull is stuck**, with the pod Pending and a single "Pulling"
  event. The scripts deliver every image into the VM's containerd through
  the share (`image_delivery.py`) and set containerd's progress watchdog to
  30 minutes, so kubelet finds the image present and never pulls. If it
  happens anyway: `image_delivery.py <site> <the image ref from the pod's
  events>` puts the image in place; then `sudo systemctl restart containerd`
  on the VM drops the stuck pull. Running containers are not affected.
- **The image import into the VM looks stuck.** It is slow, not stuck. The
  VM reads the tarball through the share, and the 10 GB core image takes
  minutes to copy and then to unpack its 9 GB layer. The delivery prints an
  estimate before it starts, then one progress line per minute. For a
  closer look, run the command it prints on the VM:
  `bash /home/nico/mac/infra-controller/tools/nico-dev/monitor-import.sh <tarball>`.
  Only a content store that does not grow for two samples in a row is a
  real stall.
- **A pull fails with "HTTP response to HTTPS client".** Run `ndev.py <site>
  registry verify` on the VM. If containerd shows ✗, the insecure-registry
  `config_path` is missing; the fix is printed.
- **The registry is not running on the host.** Check with `docker ps | grep
  registry`. `ensure-registry.py` starts it.
- **Vault is sealed** after a restart. The unsealer normally resolves it
  within seconds. If not, `deploy-dev-nico.py <site> --skip-to nico`.
- **The host suspended** under a running site, or its clock jumped: the
  control plane restarts and pods sit in `Unknown`. `sudo restart-ordered.sh`
  on the VM (Step 13), and mask suspend (Step 1) so it does not recur.
- **Console without ssh:** `virsh -c qemu:///system console vm-feature1`.
- **The VM has no address.** On the console, run `ip addr show`, then look
  at `cat /etc/netplan/*.yaml` and run `sudo netplan apply`.

## Appendix F - Maintainers

- **Smoke test** before pushing any change to the cloud-init seed, the VM
  creation record, or `prepare-vm`: `smoke-test.sh` takes about two minutes
  on a throwaway VM.
- **Tag a validated tip** after a full bring-up:
  `git tag validated-YYYYMMDD && git push origin validated-YYYYMMDD`. The
  stable graft channel serves the newest such tag.

## Appendix G - Script reference

| Script | Runs on | Does |
|---|---|---|
| `check-prereqs.sh [--build]` | host | read-only prerequisite check, includes checkout parity |
| `check-parity.py [repo] [--quiet]` | host | does the checkout still match what the nico-dev scripts assume; which deployment inputs upstream main changed since the branch left it |
| `image_delivery.py <site> <ref>… [--check]` | host | put images into the VM's containerd through the share (never through the registry tunnel) |
| `bring-up.py --config X [--dry-run] [--from step]` | host | the whole bring-up |
| `bring-up-status.py --config X [--once]` | host | high-level progress of that bring-up in a second terminal: steps done/running/failed, per-step time, what the current step is doing, ssh/kubeconfig/URL, resume command |
| `dev-down.py --config X [--remove-infra]` | host | the whole teardown |
| `ngc-tags.py --config X` | host | deployable NGC tags |
| `build-dev-nico.py <site> --tag T --profile dev\|release` | host | build images, push to the local registry; incremental per checkout; `dev` = Tilt build (minutes), `release` = shipped build |
| `deploy-dev-nico.py <site> --tag T` | host | full helm deploy, resumable |
| `redeploy-dev-nico.py <site> --tag T` | host | roll the nico release to a tag |
| `deploy-flow.py <site> --config flow.yaml [--status\|--uninstall]` | host | Flow add-on, from its own standalone config |
| `deploy-dpf-sim.py <site> [--phase-dwell T] [--os-install-dwell T] [--uninstall]` | host | DPF simulator (default site; the `dpf` bring-up step) |
| `build-nico-clis.py <site> [--mat-only] [--install-to DIR]` | host | MAT, admin-cli and nicocli in containers from the site's checkout; writes `BUILD_INFO` next to each binary |
| `configure-clis.py <site>` | host | certs, MAT config, wrappers, /etc/hosts |
| `get-admin-cli.sh <site>` | VM | admin CLI from the API container, no build |
| `get-nicocli.sh <site>` | host or VM | REST CLI from the REST API image (docker on the host, containerd on the VM), no build; writes `run-nicocli.sh` |
| `run-admin-cli.sh`, `run-nicocli.sh`, `run-mat.sh` | host / VM | generated wrappers in the site folder |
| `reset-mat-state.py <site> --yes` | host | fleet back to time zero; marks the run boundary in the monitor's diary |
| `monitor-mat.py --admin-cli W [--mat-log F]… [--no-dpf]` / `run-monitor-mat.sh <site>` | VM | a MAT run on one overview page plus one page per section (endpoints, machines, DPUs, DPF, MAT, history, timeline): expected machines, endpoints, machine states with milestones to go, DPUs (NICo and DPF phases), MAT's own view, every transition with its time; refresh 10 s |
| `ndev.py <site> [sub]` | host or VM | status, fabric, BGP, registry, DPU |
| `restart-ordered.sh [--cold]` | VM | ordered recovery after a reboot |
| `smoke-test.sh` | host | maintainers: boot-path check on a throwaway VM |

Friction is a bug. If a step confused you, or an error message did not get
you out of trouble, that is a defect in this tooling. Please report it.
