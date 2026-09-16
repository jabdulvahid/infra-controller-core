# nico-dev on a Linux host — how to

A complete NICo dev environment in one VM under libvirt/KVM: Kubernetes, the
NICo stack, a simulated FRR datacenter fabric, and the MAT fleet simulator.
This page is the current-state guide for Linux hosts (Ubuntu 22.04/24.04,
x86_64 or aarch64). `how-to.md` is the full historical reference; consult it
when a step here does not explain enough.

Two ways to get NICo images into the site:

- **NGC lane** (default, no compilation): pre-built images pulled from NGC.
  Empty host to admin UI in about 13 minutes on the corp network.
- **Source-build lane**: your worktree's branch, built into images on the
  host and deployed. First build 20 to 40 minutes, minutes afterwards.

Both share every other step.

---

## 1. Host prerequisites

```bash
sudo apt install libvirt-daemon-system libvirt-clients virtinst cloud-image-utils qemu-utils virtiofsd docker.io git python3 python3-yaml
sudo usermod -aG libvirt,kvm,docker $USER
# log out and back in so the groups apply
```

No rustup, cargo or Go on the host. Every binary, NICo images included, is
built inside containers; Docker is the only toolchain.

| Purpose | CPUs | RAM | Notes |
|---|---|---|---|
| build-and-play: NGC lane, use the site, no redeploys | 6 | 8 GB | the running stack uses about 5 GB |
| development: source builds, redeploy cycles, MAT | 8+ | 16 GB+ | a full site commits about 90% of a 6-CPU node's CPU requests |

The VM disk is sparse, 120 GB nominal, about 25 GB after an NGC bring-up.
A `ufw` default-deny host works unchanged: Docker and libvirt insert their
own accept rules ahead of it.

For the NGC lane you need an NGC API key with registry-read on the org/team
that publishes the NICo images. Keep it in an environment variable; the tools
take the variable's *name* and never print the value.

```bash
export NGC_API_KEY='...'     # in your shell profile
```

## 2. Folder, worktree, tools

Your primary clone stays where it lives. Each VM gets one folder whose
`shared/` the VM mounts, holding a worktree on the branch you want to run:

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/projects/infra-controller     # no clone yet? git clone https://github.com/NVIDIA/infra-controller.git
git fetch origin main              # so the worktree starts from TODAY's main, not the last fetch
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
newest `validated-*` tag. As of 2026-09-10 that tag predates the `ngc:`
config block below and `restart-ordered.sh`, so use `--edge` until a newer
tag exists.

```bash
cd tools/nico-dev
export PATH="$PATH:$(pwd)"   # in your shell profile
check-prereqs.sh             # NGC lane tier: libvirt, KVM, docker, network, disk
check-prereqs.sh --build     # also the source-build tier
```

The check is read-only. Fix every ✗; each line says how.

## 3. Describe the site

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
underlay: 11        # first octets of the fabric prefixes: pick two your host and VPN do not use
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

Do not set `ip`. The VM gets `192.168.64.<host_num>` (default 126) on a
dedicated libvirt NAT network `nico-nat`, bridge `virbr-nico`, which the
builder creates and records; `--subnet` overrides if that range is taken.
Several VMs on one host need distinct `name`, `host_num` and octets.

Choosing an NGC tag. `tag` is the default for every image; the optional
`tags` map gives one image group (`core`, `rest`, `flow`) a different tag,
and `--group` lists that group's tags:

```bash
ngc-tags.py --config bringup-mysite.yaml                 # newest PR builds tracking main, with host-arch availability
ngc-tags.py --config bringup-mysite.yaml --before v2.3.0 # page back
ngc-tags.py --config bringup-mysite.yaml --group rest    # tags of the REST images
```

Prefer a recent dev tag. A fresh site's database ledger tracks main and the
migration job refuses downgrades.

## 4. Bring the site up

```bash
bring-up.py --config bringup-mysite.yaml --dry-run   # preflight ✓/✗/⚠, numbered plan, READY or NOT READY
bring-up.py --config bringup-mysite.yaml
```

Steps: vm → prep → site → fabric → cp → build (source lane only) → registry →
nico → dpf → route. No GUI step; `virt-install` sets the share path. One `sudo`
prompt at the end for the host route to the service VIPs. On failure the
runner prints that step's known failure modes and the resume command
(`--from <step>`). Every step is safe to rerun.

Done looks like a URL: `https://11.133.1.17/admin` for `underlay: 11`.

Reaching the admin UI from your laptop when the host is headless:

```bash
ssh -L 8443:11.133.1.17:443 <linux-host>     # then https://localhost:8443/admin
sshuttle -r <linux-host> 11.133.1.0/27       # if the login flow redirects to the VIP itself
```

The host route is `sudo ip route replace 11.133.1.0/27 via 192.168.64.126`.
bring-up adds it; it does not survive a host reboot.

## 5. Where things are

| | Host | Inside the VM |
|---|---|---|
| share root | `~/nico-tests/vm1/shared` | `~/mac` (mount name is historical) |
| repo worktree | `<share>/infra-controller` | `~/mac/infra-controller` |
| site folder | `<share>/sites/dc1/dev1` | `~/mac/sites/dc1/dev1` |
| image tarballs in transit | `<site>/images/*.tar` (deleted after import) | same path under `~/mac` |
| kubeconfig | `<site>/dc1-dev1.kubeconfig.yaml` | same path under `~/mac` |

The share is virtiofs and passes your uid through: files the guest writes are
yours on the host. Inside the VM the repo is a git worktree whose metadata
lives on the host; do your git on the host.

```bash
export KUBECONFIG=~/nico-tests/vm1/shared/sites/dc1/dev1/dc1-dev1.kubeconfig.yaml
kubectl get nodes
kubectl -n nico-system get pods              # all Running or Completed

ndev.py <site>                               # host: cluster status; fabric/BGP/DPU show n/a (VM-side)
ssh nico@192.168.64.126 'ndev.py ~/mac/sites/dc1/dev1 fabric verify'   # VM: full fabric health
```

`ndev.py` subcommands: `fabric verify|info|shell [switch]`, `bgp info
[--detail]`, `cluster info`, `registry verify`, `dpu info`.

## 6. CLIs and MAT

All three tools build in containers from the checkout named in the site yaml,
and the outputs are chowned to you:

```bash
build-nico-clis.py <site> --install-to ~/.local/bin
```

- `nico-admin-cli` and `nicocli` are Linux ELF binaries that run on the host.
- `machine-a-tron` (MAT) is delivered through the share to
  `<site>/mat/machine-a-tron`. It runs on the VM only: it puts mock-BMC
  addresses on the fabric bridge `br-dc1-internet`, which exists inside the
  VM, and NICo's site-explorer connects inbound to them.

Certificates, config and wrappers:

```bash
configure-clis.py <site>
<site>/run-admin-cli.sh version
```

This fetches the site CA, issues client certificates for admin-cli and MAT
from the cluster's Vault, writes `<site>/mat/mat-config.toml` (the fleet
definition), generates `run-admin-cli.sh` and `run-mat.sh`, and adds
`11.133.1.17 nico-api.dc1-dev1` to `/etc/hosts`. Always use the wrapper; the
bare binary dials the in-cluster URL.

No-build alternative for the admin CLI, on the VM:
`get-admin-cli.sh ~/mac/sites/dc1/dev1` extracts the binary shipped in the
API container, version-matched by construction.

`nicocli`, the REST-surface CLI, needs a token minted inside the cluster:

```bash
TOKEN=$(bash <repo>/helm-prereqs/keycloak/get-token.sh)
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx infrastructure-provider current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx tenant current
nicocli --base-url http://192.168.64.126:30388 --token "$TOKEN" --org ncx vpc list
```

The two `current` calls must come first on a fresh site; they create the
org's provider and tenant objects, and everything else returns 403 until
they exist.

### Running MAT

MAT impersonates a rack of managed hosts, mock BMCs with Redfish plus DHCP
clients, so machines appear in NICo, get explored and come Ready.

```bash
ssh nico@192.168.64.126 '~/mac/sites/dc1/dev1/run-mat.sh'
```

`run-mat.sh` copies binary, certs and config VM-local, installs the binary
under `/usr/local/bin`, stages everything under `/etc/machine-a-tron/dc1/`,
ensures the API hostname in `/etc/hosts`, and launches MAT under `sudo`. Log:
`/var/log/machine-a-tron-dc1.log` on the VM. Watch from the host with
`<site>/run-admin-cli.sh machine list`.

Between runs, after stopping MAT and before starting it again:

```bash
reset-mat-state.py <site> --yes
```

NICo keeps the fleet's footprint (explored endpoints, expected-machine
registrations, rotated BMC credentials); without the reset a rerun against
fresh mocks locks the endpoints out.

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

Your own MAT build against the site's certificates: build with
`--repo <worktree> --out-dir <dir>` so the site's baseline binary is
untouched, copy `run-mat.sh`, and change the two paths at its top. Details
and the failure catalog: `mat-in-nico-dev.md`.

## 7. The dev loop

```bash
build-dev-nico.py    <site> --tag t2      # images for the host arch, pushed to the local registry
redeploy-dev-nico.py <site> --tag t2      # helm upgrade of the nico release only
kubectl -n nico-system get pods -w
```

Rules that save an afternoon:

- **Bump the tag every time.** Both scripts refuse a same-tag rebuild or
  redeploy, because the cluster would silently keep the old image.
- **No downgrades.** Each deploy advances the database's migration ledger,
  and an older binary refuses to run against a newer ledger. Revert by going
  forward: restore the code, build a fresh tag from the current checkout,
  deploy that. If `nico-api-migrate` crashloops with "migration … was
  previously applied but is missing", you deployed an older binary; delete
  the job, revert the code, build and deploy a new tag.
- **Tight node.** On a 6-CPU VM a rollout can stick on `Insufficient cpu`;
  `redeploy: { on_insufficient_cpu: scale-down-first }` in the bringup yaml
  rolls the stuck deployment old-pod-first for that rollout only.

Full deploy or one release: `deploy-dev-nico.py <site> --tag <t>`,
`--skip-to <release>`, `--only <release>`. Stuck helm releases are healed
automatically. Never delete the `nico-system` namespace to recover; it holds
the release state.

## 8. Add-ons after bring-up

Optional charts are one script each, run from the host after the site is up,
at your discretion, from a standalone config file: nothing in it comes from
`bringup.yaml` or the site yaml, so a site deployed from NGC can run a
locally built add-on and vice versa. Flow is the first:

```bash
cp flow-example.yaml ~/nico-tests/vm1/shared/flow.yaml    # then edit: source, ngc.registry, ngc.tag
deploy-flow.py <site> --config ~/nico-tests/vm1/shared/flow.yaml --dry-run
deploy-flow.py <site> --config ~/nico-tests/vm1/shared/flow.yaml
deploy-flow.py <site> --status
deploy-flow.py <site> --uninstall
```

The add-on implements the current chart and refuses an older checkout with
"refresh the worktree". Add-ons write nothing into the site yaml; Helm is
the record. Design notes, the shared config format and the catalog of other
charts: `ADDONS.md`, `deploying-extras.md`.

## 9. Reboots and recovery

After a VM reboot the fabric service recreates bridges and switches, kubelet
restarts the cluster, and pods start from cached images. Kubernetes has no
pod start order, so a reboot is a lottery of races. If the site "looks
Running" but does not work, on the VM:

```bash
sudo restart-ordered.sh          # verify infrastructure, restart every consumer in dependency order
sudo restart-ordered.sh --cold   # scale all to zero first, then up in order
```

Ends with a pass/fail verdict. `--dry-run` prints the plan.

## 10. Learning the fabric

Every switch is a live FRR router. On the VM:

```bash
ndev.py ~/mac/sites/dc1/dev1 fabric shell            # list switches
ndev.py ~/mac/sites/dc1/dev1 fabric shell spine-1    # vtysh on one
```

Inside vtysh: `show bgp summary`, `show ip route`, `show bgp l2vpn evpn`,
`show running-config`. Break anything; `sudo systemctl restart
nico-dev-fabric` rebuilds the fabric from the site yaml. Newcomers start with
`networking-primer.md`.

## 11. Troubleshooting

- **VIP refuses connections on a fresh site while pods are Running.**
  `kubectl -n nico-system get endpoints nico-api` empty means the API is not
  serving, not a fabric fault. `kubectl -n nico-system rollout restart
  deployment/nico-api`; the VIP answers about 30 s later.
- **Registry step times out** (not "connection refused"): a firewall blocks
  the VM's path to `192.168.64.1:5000`.
  `sudo ufw route allow in on virbr-nico to any port 5000 proto tcp`.
- **Image pull stuck**, pod Pending with one "Pulling" event: check the
  registry container's logs for blob GETs, then `docker restart registry` on
  the host; if still stuck, `sudo systemctl restart containerd` on the VM.
  Running containers are unaffected and the layer download resumes.
- **Pull fails with "HTTP response to HTTPS client"**: `ndev.py <site>
  registry verify` on the VM; if containerd shows ✗, the insecure-registry
  `config_path` is missing (fix printed).
- **Vault sealed** after a restart: the unsealer resolves it within seconds;
  otherwise `deploy-dev-nico.py <site> --skip-to nico`.
- **Console** without ssh: `virsh -c qemu:///system console nico-vm1`.
- **VM has no address**: on the console, `ip addr show`, then
  `cat /etc/netplan/*.yaml` and `sudo netplan apply`.

## 12. Tear down

```bash
dev-down.py --config bringup-mysite.yaml                  # domain, volumes, host route, ledger entry
dev-down.py --config bringup-mysite.yaml --remove-infra   # also nico-nat and the pool, when no nico VMs remain
```

Your site folder and worktree are never deleted. Everything nico-dev created
is listed in `~/.nico-dev/vms/<vm>.yaml`.

## 13. Script reference

| Script | Runs on | Does |
|---|---|---|
| `check-prereqs.sh [--build]` | host | read-only prerequisite check, includes checkout parity |
| `check-parity.py [repo] [--quiet]` | host | does the checkout still match what the nico-dev scripts assume |
| `image_delivery.py <site> <ref>… [--check]` | host | put images into the VM's containerd through the share (never through the registry tunnel) |
| `bring-up.py --config X [--dry-run] [--from step]` | host | the whole bring-up |
| `dev-down.py --config X [--remove-infra]` | host | the whole teardown |
| `ngc-tags.py --config X` | host | deployable NGC tags |
| `build-dev-nico.py <site> --tag T` | host | build images, push to local registry |
| `deploy-dev-nico.py <site> --tag T` | host | full helm deploy, resumable |
| `redeploy-dev-nico.py <site> --tag T` | host | roll the nico release to a tag |
| `deploy-flow.py <site> --config flow.yaml [--status\|--uninstall]` | host | Flow add-on, from its own standalone config |
| `deploy-dpf-sim.py <site> [--phase-dwell T] [--uninstall]` | host | DPF simulator (default site; the `dpf` bring-up step) |
| `build-nico-clis.py <site>` | host | admin-cli, nicocli, MAT in containers |
| `configure-clis.py <site>` | host | certs, MAT config, wrappers, /etc/hosts |
| `run-admin-cli.sh`, `run-mat.sh` | host / VM | generated wrappers in the site folder |
| `get-admin-cli.sh <site>` | VM | admin CLI from the API container, no build |
| `reset-mat-state.py <site> --yes` | host | fleet back to time zero |
| `ndev.py <site> [sub]` | host or VM | status, fabric, BGP, registry, DPU |
| `restart-ordered.sh [--cold]` | VM | ordered recovery after a reboot |
| `smoke-test.sh` | host | maintainers: 2-minute boot-path check on a throwaway VM |

Friction is a bug. If a step confused you or an error message did not rescue
you, that is a defect in this tooling; please report it.
