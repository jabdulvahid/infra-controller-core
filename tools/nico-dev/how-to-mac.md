# nico-dev on a Mac — how to

A complete NICo dev environment in one UTM VM on an Apple Silicon Mac:
Kubernetes, the NICo stack, a simulated FRR datacenter fabric, and the MAT
fleet simulator. This page is the current-state guide for Macs. `how-to.md`
is the full historical reference; consult it when a step here does not
explain enough. Intel Macs are not a supported target.

There are two different workflows on this page, and they share nothing
until the site is running. Section 3 tells you which one you are on and which
sections to read. Sections 1 and 2 apply to both.

---

## 1. Mac prerequisites

```bash
brew install --cask utm            # then launch it once
brew install git python3 helm kubectl colima docker
pip3 install pyyaml
colima start --cpu 8 --memory 16 --disk 100    # default 4 CPU / 8 GB OOM-kills the Rust build
ssh-keygen -t ed25519              # if you have no keypair
```

Containers yes, toolchains no: colima and docker are required, rustup, cargo
and Go are not. NICo images and MAT build inside containers. The one
exception today is building `nico-admin-cli` and `nicocli` on the Mac
itself, which needs the host's cargo and Go because they must be Mach-O
binaries; the no-toolchain path for the admin CLI is on the VM (section 9).

| Purpose | Mac RAM | VM | Notes |
|---|---|---|---|
| build-and-play: golden image or NGC lane, no redeploys | 16 GB | 6 CPUs, 8 GB | the running stack uses about 5 GB |
| development: source builds, redeploy cycles, MAT | 32 GB+ | 8+ CPUs, 16 GB+ | the UTM VM and colima time-share the cores |

Disk: about 250 GB free for the development tier (UTM disk 100 GB, colima
100 GB, images and cache; both disks are sparse). One nico-dev VM booted at
a time per Mac: they share the address `192.168.64.126`.

For the NGC lane you need an NGC API key with registry-read on the org/team
that publishes the NICo images, in an environment variable:

```bash
export NGC_API_KEY='...'     # in your shell profile
```

## 2. Folder, worktree, tools

Your primary clone stays where it lives. Each VM gets one folder whose
`shared/` the VM mounts, holding a worktree on the branch you want to run:

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/projects/infra-controller     # no clone yet? git clone https://github.com/NVIDIA/infra-controller.git
git worktree add -b vm1-work ~/nico-tests/vm1/shared/infra-controller origin/main
```

Graft the tools into that worktree. They land in `tools/nico-dev` as
untracked, git-ignored files: your `git status`, commits and PRs never see
them. Rerun the same command to update them in place.

```bash
cd ~/nico-tests/vm1/shared/infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash -s -- --edge
```

`--edge` fetches the branch tip. Without it you get the stable channel, the
newest `validated-*` tag. As of 2026-09-10 that tag predates
`onboard-golden.sh`, the non-interactive `first-boot.sh`, the `ngc:` config
block and `restart-ordered.sh`, so use `--edge` until a newer tag exists.

```bash
cd tools/nico-dev
export PATH="$PATH:$(pwd)"   # in your shell profile
check-prereqs.sh             # run-the-sim tier
check-prereqs.sh --build     # also the source-build tier
```

The check is read-only. Fix every ✗; each line says how. The first run that
drives UTM pops "Terminal wants to control UTM": click Allow.

## 3. Which workflow are you on?

| | Workflow A: golden image | Workflow B: build your own VM |
|---|---|---|
| You have | a `nico-dev-golden-YYYYMMDD.utm.zip` from a colleague | nothing but this repo and, for the NGC lane, an NGC key |
| What happens | the ZIP is a baked VM with a site already deployed inside; one script imports it, personalises it and opens the admin UI | a VM is created, Kubernetes and the fabric are installed, NICo is deployed from images |
| Where the images come from | already inside the VM | **NGC lane**: pulled pre-built from NGC. **Source-build lane**: built from your worktree |
| You write | nothing | one `bringup-<site>.yaml` |
| Time | minutes, nothing to build | NGC lane about 30 minutes; source-build lane plus a 20 to 40 minute first build |
| Read | section 4, then continue at section 7 | sections 5 and 6, then continue at section 7 |

The two workflows do not mix. `onboard-golden.sh` is Workflow A only and
never reads a bringup yaml. `bring-up.py` and the bringup yaml are Workflow B only
and never touch a golden ZIP. Within Workflow B the two lanes differ in one
block of the bringup yaml and one build step; everything else is identical.

From section 7 onward the page applies to both workflows: the running site
looks the same however it got there.

## 4. Workflow A: golden image, three commands

Workflow A only. If you are building your own VM, skip to section 5.

You received `nico-dev-golden-YYYYMMDD.utm.zip`, a baked VM with a running
site inside:

```bash
mkdir -p ~/nico-tests/vm1/shared && cd ~/nico-tests/vm1/shared
git clone https://github.com/NVIDIA/infra-controller.git     # the folder name must stay infra-controller
cd infra-controller
curl -fsSL https://raw.githubusercontent.com/jabdulvahid/infra-controller-core/nico-dev/tools/nico-dev/graft-tools.sh | bash -s -- --edge
tools/nico-dev/onboard-golden.sh --zip ~/Downloads/nico-dev-golden-YYYYMMDD.utm.zip --dest ~/nico-tests/vm1
```

Hands off except what macOS insists on: a one-time Accessibility and
Automation permission for the UI-scripted share step, and one `sudo` for the
VIP route. It ends with the admin UI open, the kubeconfig path, and the route
command you will need again. The ZIP is kept; to retest, stop and delete the
VM in UTM, remove `~/nico-tests/vm1/*.utm`, rerun with the same `--zip`.

Manual fallback, if the scripted share step does not stick:

1. UTM → **+** → **Import** the `.utm` bundle.
2. VM settings → **Sharing** → path `~/nico-tests/vm1/shared`, share name
   `share`. Set it before the first boot.
3. Boot, then `ssh nico@192.168.64.126`, password `Welcome123!`.
4. On the VM:
   ```bash
   sudo bash /usr/local/lib/nico-dev/first-boot.sh --ssh-key ~/.ssh/id_ed25519.pub \
       --mac-folder /Users/<you>/nico-tests/vm1/shared --yes
   ```
   `--mac-folder` is the share root, not the repo inside it. The hostname is
   never changed: it is the kubeadm node name. Expect `metallb-speaker` and
   `nico-api` to restart once at the end; that is the script, not a fault.
5. On the Mac: `ssh-keygen -R 192.168.64.126` (the clone regenerated its host
   keys), then the route and KUBECONFIG from section 7.

Continue at section 7. Sections 5 and 6 are not for you.

## 5. Workflow B: describe the site

Workflow B only, both lanes. Golden-image users have nothing to do here;
continue at section 7.

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
  tag: <tag>                           # ngc-tags.py, below
  core_image: nvmetal-carbide          # NGC's name for the core image
  token_env: NGC_API_KEY               # NAME of the env var holding your key
# tag: main-20260910                   # source-build lane: the image label
```

Do not set `ip`; the VM address derives from UTM's own subnet, `host_num`
changes the last octet (default 126). bring-up's preflight warns if the Mac
already routes your octets.

```bash
ngc-tags.py --config bringup-mysite.yaml                 # newest PR builds tracking main, arm64 availability
ngc-tags.py --config bringup-mysite.yaml --before v2.3.0
```

Prefer a recent dev tag; a fresh site's database ledger tracks main and the
migration job refuses downgrades. The image must publish arm64.

## 6. Workflow B: bring the site up

Workflow B only, both lanes.

```bash
bring-up.py --config bringup-mysite.yaml --dry-run   # preflight ✓/✗/⚠, numbered plan, READY or NOT READY
bring-up.py --config bringup-mysite.yaml
```

Steps: vm → prep → site → fabric → cp → build (source lane only) → registry →
nico → dpf → route. Three interactive moments, by design:

1. **UTM share Path**: UTM does not let a script set the shared directory of
   a QEMU VM. The runner pauses, tells you to point Sharing at
   `~/nico-tests/vm1/shared`, and waits for Enter.
2. The VM password once, during `prep`, before your key is installed.
3. `sudo` on the Mac for the VIP route.

On failure the runner prints that step's known failure modes and the resume
command (`--from <step>`). Every step is safe to rerun. Done looks like a
URL: `https://11.133.1.17/admin` for `underlay: 11`.

## 7. Both workflows: Mac-side setup and where things are

```bash
export KUBECONFIG=~/nico-tests/vm1/shared/sites/dc1/dev1/dc1-dev1.kubeconfig.yaml   # in your shell profile
kubectl get nodes

sudo route -n add -net 11.133.1.0/27 192.168.64.126     # route to the service VIPs
route -n get 11.133.1.17                                # gateway: 192.168.64.126
```

The route dies whenever the last UTM VM stops, because macOS removes
`bridge100` and flushes routes through it, and also on sleep/wake and VPN
changes. Re-add it with the same command; `restart-ordered.sh` and
`onboard-golden.sh` print it for you.

| | Mac | Inside the VM |
|---|---|---|
| share root | `~/nico-tests/vm1/shared` | `~/mac` |
| repo worktree | `<share>/infra-controller` | `~/mac/infra-controller` |
| site folder | `<share>/sites/dc1/dev1` | `~/mac/sites/dc1/dev1` |
| kubeconfig | `<site>/dc1-dev1.kubeconfig.yaml` | same path under `~/mac` |
| local registry | `localhost:5000` (colima) | `192.168.64.1:5000` |

Inside the VM the repo is a git worktree whose metadata lives on the Mac; do
your git on the Mac.

```bash
ndev.py <site>                                                       # Mac: cluster status; fabric/BGP/DPU n/a (VM-side)
ssh nico@192.168.64.126 'ndev.py ~/mac/sites/dc1/dev1 fabric verify' # VM: full fabric health
```

`ndev.py` subcommands: `fabric verify|info|shell [switch]`, `bgp info
[--detail]`, `cluster info`, `registry verify`, `dpu info`.

## 8. Verify

```bash
kubectl -n nico-system get pods        # all Running or Completed
curl -k https://11.133.1.17/           # "Forge development build"
open https://11.133.1.17/admin
```

On a corporate VPN the VPN client may grab the VIP range before the route;
`ssh -L 8443:11.133.1.17:443 nico@192.168.64.126` then
`https://localhost:8443/admin` sidesteps it.

## 9. CLIs and MAT

The admin CLI without a toolchain, on the VM:

```bash
ssh nico@192.168.64.126
get-admin-cli.sh ~/mac/sites/dc1/dev1      # extracts the binary from the API container, issues certs, writes the wrapper
~/mac/sites/dc1/dev1/run-admin-cli.sh version
```

MAT, built in a container on the Mac (docker via colima) and delivered
through the share:

```bash
build-nico-clis.py <site> --mat-only       # machine-a-tron only, no host toolchain needed
configure-clis.py <site>                   # certs for admin-cli and MAT, mat-config.toml, run-mat.sh, /etc/hosts entry
```

Without `--mat-only`, `build-nico-clis.py` also builds `nico-admin-cli` and
`nicocli` with the Mac's cargo and Go, which is the one place the toolchain
rule is not yet met; use `get-admin-cli.sh` instead unless you need a
feature build of the CLI.

MAT runs on the VM only: it puts mock-BMC addresses on the fabric bridge
`br-dc1-internet`, which exists inside the VM, and NICo's site-explorer
connects inbound to them.

```bash
ssh nico@192.168.64.126 '~/mac/sites/dc1/dev1/run-mat.sh'
```

`run-mat.sh` copies binary, certs and config VM-local, installs the binary,
stages everything under `/etc/machine-a-tron/dc1/`, and launches MAT under
`sudo`. Log: `/var/log/machine-a-tron-dc1.log` on the VM. Watch with
`run-admin-cli.sh machine list`. Between runs, after stopping MAT:

```bash
reset-mat-state.py <site> --yes
```

NICo keeps the fleet's footprint (explored endpoints, expected-machine
registrations, rotated BMC credentials); without the reset a rerun against
fresh mocks locks the endpoints out. Custom MAT builds and the failure
catalog: `mat-in-nico-dev.md`.

### DPF and the two provisioning modes

A site brought up with `dpf: true` (the default) runs NICo with DPF as the
DPU-provisioning path and carries the DPF simulator, `dpf-sim-controller`, in
the `dpf-operator-system` namespace. Every MAT host then passes through
`dpuinit`: NICo writes DPUDevice and DPUNode resources, the simulator walks a
DPU resource through the real phase sequence, including the reboot round-trip
against the BMC mocks, and the host comes Ready. Watch it:

```bash
kubectl -n dpf-operator-system get dpudevice,dpunode,dpu -w
kubectl -n dpf-operator-system logs deployment/dpf-sim-controller -f
```

The legacy iPXE path is still available in two ways:

- **Chosen hosts**: `<site>/run-admin-cli.sh dpf disable <host>` after the
  host is discovered and before it is ingested. NICo refuses to disable DPF on
  a host that was ingested via DPF, so decide before the run reaches
  `dpuinit`. MAT registers all hosts with DPF on.
- **Whole site**: `dpf: false` in bringup.yaml. No CRDs, no simulator, and
  NICo logs its iPXE deprecation warning.

`deploy-dpf-sim.py <site>` redeploys the simulator, for example with
`--phase-dwell 30s` for a slower walk or `--rebuild` after a code change;
`--uninstall` removes it. Tuning lives in the site yaml under
`nico-system.dpf`. What the simulator does and does not reproduce, and its
failure catalog: `mat-in-nico-dev.md` §13.

`nicocli`, the REST-surface CLI, needs a token minted inside the cluster:

```bash
TOKEN=$(bash <repo>/helm-prereqs/keycloak/get-token.sh)
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx infrastructure-provider current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx tenant current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx vpc list
```

The two `current` calls must come first on a fresh site.

## 10. The dev loop

```bash
build-dev-nico.py    <site> --tag t2      # arm64 images, pushed to the colima registry
redeploy-dev-nico.py <site> --tag t2      # helm upgrade of the nico release only
kubectl -n nico-system get pods -w
```

- **Bump the tag every time.** Both scripts refuse a same-tag rebuild or
  redeploy, because the cluster would silently keep the old image.
- **No downgrades.** Each deploy advances the database's migration ledger
  and an older binary refuses to run against a newer one. Revert by going
  forward: restore the code, build a fresh tag from the current checkout,
  deploy that. If `nico-api-migrate` crashloops with "migration … was
  previously applied but is missing", delete the job, revert the code, build
  and deploy a new tag.
- **Tight node.** `redeploy: { on_insufficient_cpu: scale-down-first }` in
  the bringup yaml handles `Insufficient cpu` during a rollout.

Full deploy or one release: `deploy-dev-nico.py <site> --tag <t>`,
`--skip-to <release>`, `--only <release>`. Never delete the `nico-system`
namespace to recover; it holds the release state.

## 11. Add-ons after bring-up

```bash
deploy-flow.py <site> --ngc          # NICo Flow, images from NGC at the REST tag
deploy-flow.py <site> --build
deploy-flow.py <site> --uninstall
```

One script per chart, run from the Mac after the site is up. Design and the
catalog of other charts: `ADDONS.md`, `deploying-extras.md`.

## 12. Reboots and recovery

After a VM reboot the fabric service recreates bridges and switches, kubelet
restarts the cluster, and pods start from cached images. Kubernetes has no
pod start order, so a reboot is a lottery of races. If the site "looks
Running" but does not work, on the VM:

```bash
sudo restart-ordered.sh          # verify infrastructure, restart every consumer in dependency order
sudo restart-ordered.sh --cold   # scale all to zero first, then up in order
```

Ends with a pass/fail verdict, and reminds you of the Mac route.

## 13. Learning the fabric

On the VM, `ndev.py ~/mac/sites/dc1/dev1 fabric shell` lists the switches
and `fabric shell spine-1` drops you into vtysh. `show bgp summary`, `show ip
route`, `show bgp l2vpn evpn`. Break anything; `sudo systemctl restart
nico-dev-fabric` rebuilds the fabric from the site yaml. Newcomers start with
`networking-primer.md`.

## 14. Troubleshooting

- **VIP refuses connections on a fresh site while pods are Running.**
  `kubectl -n nico-system get endpoints nico-api` empty means the API is not
  serving. `kubectl -n nico-system rollout restart deployment/nico-api`; the
  VIP answers about 30 s later. `first-boot.sh` and `onboard-golden.sh`
  already apply this once.
- **Route missing**: `netstat -rn | grep 11.133` empty → re-add it (section
  7). It goes every time the last VM stops.
- **Image pull stuck**, pod Pending with one "Pulling" event: the blob stream
  through colima's port-forward wedged. `docker logs --since 2m registry |
  grep -c blobs` on the Mac (zero means wedged), `docker restart registry`,
  and if still stuck `sudo systemctl restart containerd` on the VM. Running
  containers are unaffected; the download resumes.
- **Pull fails with "HTTP response to HTTPS client"**: `ndev.py <site>
  registry verify` on the VM; if containerd shows ✗, the insecure-registry
  `config_path` is missing (fix printed).
- **Registry not running on the Mac**: `docker ps | grep registry`;
  `ensure-registry.py` starts it.
- **Vault sealed** after a restart: the unsealer resolves it within seconds;
  otherwise `deploy-dev-nico.py <site> --skip-to nico`.
- **VM has no address**: on the UTM console, `ip addr show enp0s1`, then
  `cat /etc/netplan/99-nico-static.yaml` and `sudo netplan apply`.

## 15. Tear down

Stop and delete the VM in UTM, then remove `~/nico-tests/vm1/*.utm`. A
one-command `dev-down.py` for the Mac is planned; today the dispatcher serves
Linux only. Your site folder and worktree are never touched by either.

## 16. Maintainers

- **Bake and export a golden image**: on the VM, with all pods Running, MAT
  stopped and the fleet reset, `sudo bash bake-golden-image.sh <site yaml>`;
  shut down; remove the shared directory from the VM's UTM settings; UTM →
  right-click → **Share…** exports the `.utm` bundle; zip it. Never boot the
  master; give each import its own APFS copy (`cp -cR`). Returning the
  builder VM to development afterwards: `how-to.md` §12A.4b.
- **Smoke test** before pushing anything that touches the cloud-init seed,
  the VM creation record or `prepare-vm`: `smoke-test.sh`, about six minutes
  on a throwaway VM.
- **Tag a validated tip** after a full bring-up:
  `git tag validated-YYYYMMDD && git push origin validated-YYYYMMDD`. That is
  what the stable graft channel serves.

## 17. Script reference

| Script | Runs on | Does |
|---|---|---|
| `check-prereqs.sh [--build]` | Mac | read-only prerequisite check |
| `onboard-golden.sh --zip Z --dest D` | Mac | golden image ZIP to running site, hands off |
| `bring-up.py --config X [--dry-run] [--from step]` | Mac | the whole bring-up |
| `ngc-tags.py --config X` | Mac | deployable NGC tags |
| `build-dev-nico.py <site> --tag T` | Mac | build images, push to the colima registry |
| `deploy-dev-nico.py <site> --tag T` | Mac | full helm deploy, resumable |
| `redeploy-dev-nico.py <site> --tag T` | Mac | roll the nico release to a tag |
| `deploy-flow.py <site> --ngc\|--build` | Mac | Flow add-on |
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

Friction is a bug. If a step confused you or an error message did not rescue
you, that is a defect in this tooling; please report it.
