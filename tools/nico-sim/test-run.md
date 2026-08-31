# nico-sim — Setup and Test Run

Step-by-step guide to deploying a full nico-sim on a Linux server (bare-metal or VM with KVM).

**Reference host:** jaslinx — Ubuntu 24.04, i9-13900K, 62 GB RAM
**Scripts:** all live in `claude-notes/nico-sim/`

---

## Prerequisites

```bash
# Install required packages
sudo apt install -y python3-yaml git docker.io cloud-image-utils

# KVM/libvirt stack (deploy-nodes.py, create-golden-image.py use virsh/virt-install/qemu-img)
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst qemu-utils

# Recommended: guestfs-tools (virt-ls) — inspect golden images without booting them,
# e.g. verify packages actually landed:
#   sudo virt-ls -a /var/lib/libvirt/images/cp-golden.qcow2 /usr/bin/ | grep -E '^(kubeadm|kubelet|kubectl|containerd)'
# (package is named libguestfs-tools on Ubuntu ≤22.04)
sudo apt install -y guestfs-tools

# Add yourself to docker and libvirt groups (log out and back in after)
sudo usermod -aG docker $USER
sudo usermod -aG libvirt $USER

# Install ContainerLab
bash -c "$(curl -sL https://get.containerlab.dev)"

# Install kubectl
KUBE_VER=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/${KUBE_VER}/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl

# Install helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Pull FRR switch image
docker pull frrouting/frr:latest

# Ubuntu cloud image for VMs (check if already present)
ls /var/lib/libvirt/images/noble-server-cloudimg-amd64.img
# If not present:
# wget -P /var/lib/libvirt/images/ \
#   https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

# Clone repos
git clone -b nico-dev https://github.com/jabdulvahid/infra-controller-core.git
git clone <infra-controller-core-url> ~/projects/infra-controller-core
```

---

## Golden Image (one-time setup)

`deploy-nodes.py` (Step 3) can boot VMs from a pre-baked golden image that already has
containerd, kubelet, kubeadm, and kubectl installed. Without a golden image each VM
installs packages from scratch — 3–5 minutes per VM. With a golden image boot takes ~30s.

Run once per server. The **cp** image (control-plane nodes, k8s prereqs) is required.
The **mh** image is optional — MH VMs default to `provisioning: bare_metal` (they boot
dark for PXE ingestion and never use a golden image); build it only if you want to SSH
into MH VMs for debugging with `provisioning: golden_image`.

```bash
cd ~/claude-notes/nico-sim

# Build CP golden image (containerd + kubeadm prereqs, ~10 min)
sudo python3 create-golden-image.py nico-sim.yaml --target cp

# Optional: MH golden image (minimal Linux tools, ~5 min) — only for MH SSH debugging
sudo python3 create-golden-image.py nico-sim.yaml --target mh
```

The script prints the output path when it finishes, e.g.:
```
Golden image ready.
  Path: /var/lib/libvirt/images/cp-golden.qcow2
```

After both images are built, verify they are referenced in `nico-sim.yaml`:
```yaml
golden_image:
  control_plane:
    path: /var/lib/libvirt/images/cp-golden.qcow2
  managed_hosts:
    path: /var/lib/libvirt/images/mh-golden.qcow2
```

The golden images only need to be rebuilt if the k8s version changes or packages change.
They are safe to reuse across site teardowns and re-deployments.

---

## Step 1 — Create a new site

`create-new-site.py` generates the site yaml that all subsequent scripts read.

```bash
cd ~/claude-notes/nico-sim

python3 create-new-site.py \
  --dc-name dc1 \
  --site-name site1 \
  --folder ~/sites/dc1 \
  --underlay 7 \
  --overlay 8 \
  --config-size large
```

This creates `~/sites/dc1/<site-nick>.yaml` with all IP prefixes, VM sizing, and paths
pre-filled. The filename uses the **site nick** (first 4 chars of the site name unless
`--site-nick` is given) — for the example above that is `~/sites/dc1/site.yaml`, and the
same nick appears in the kubeconfig name (`site.kubeconfig.yaml`) and VM names
(`dc1-site-cp-1`).

**Key flags:**
- `--dc-name` / `--site-name` — human-readable names (any length)
- `--folder` — where to write the site yaml
- `--underlay` — first IP octet for fabric/infra (e.g. 7 → `7.128.0.0/16`, `7.130.0.0/16`, etc.)
- `--overlay` — first IP octet for tenant/overlay IPs (must differ from underlay)
- `--config-size` — required: `large` = full sim (nico-sim.yaml template), `small` = Mac/low-resource (nico-sim-mac.yaml template)
- `--dc-nick` / `--site-nick` — optional ≤4 char nicknames for bridge and VM names (defaults to first 4 chars of dc/site name)

Edit `~/sites/dc1/dc1.yaml` to set `infra_controller_repo` to your local clone:
```yaml
infra_controller_repo: /home/<you>/projects/infra-controller-core
```

---

## Step 2 — Deploy ContainerLab fabric

Generates FRR configs and deploys the ContainerLab switch topology.

```bash
python3 deploy-fabric.py ~/sites/dc1
```

What it does:
- Calls `generate-fabric.py` to write FRR configs and `topo.clab.yml` to `~/sites/dc1/fabric/`
- Creates Linux bridges (`br-dc1-internet`, `br-dc1-cp`, etc.)
- Runs `clab deploy`

Verify fabric health (takes the site folder, not the topo file):
```bash
python3 verify-fabric.py ~/sites/dc1
# → FABRIC HEALTH: ✓ HEALTHY — all containers running, all BGP peers Established, all loopbacks reachable
```

Quick check (skip loopback pings):
```bash
python3 verify-fabric.py ~/sites/dc1 --no-ping
```

---

## Step 3 — Deploy VMs (CP nodes + DPU stand-ins + MH hosts + registry)

Creates and boots the libvirt VMs using the Ubuntu cloud image. The large config
also boots 4 dark managed-host (MH) VMs for PXE ingestion.

```bash
sudo python3 deploy-nodes.py ~/sites/dc1
```

Monitor boot progress in a second terminal:
```bash
python3 check-vms.py ~/sites/dc1 --watch
```

This takes 3–5 minutes. When complete all VMs are up, cloud-init is done, and the
registry VM has a Docker registry running at `7.132.0.6:5000` (registry_link `.6`).

---

## Step 4 — Form the Kubernetes cluster

Runs kubeadm across the 3 CP VMs: init on cp-1, join on cp-2 and cp-3.

```bash
python3 form-k8s-cluster.py ~/sites/dc1
```

On completion the kubeconfig is written to `~/sites/dc1/<site-nick>.kubeconfig.yaml`.

Verify:
```bash
export KUBECONFIG=~/sites/dc1/site.kubeconfig.yaml
kubectl get nodes
# NAME    STATUS   ROLES           AGE   VERSION
# cp-1    Ready    control-plane   ...
# cp-2    Ready    control-plane   ...
# cp-3    Ready    control-plane   ...
```

---

## Step 5 — Configure insecure registry trust

Configures containerd on all CP VMs and Docker on the host to trust the
registry VM (`7.132.0.6:5000`) over plain HTTP.

```bash
python3 setup-insecure-registry.py ~/sites/dc1
```

Safe to re-run — idempotent. The script verifies its own work: it asserts
`config_path` is really set in each VM's containerd config (fails loudly if
not), then tests the actual k8s pull path with `crictl` — expect
`✓ containerd pull path OK` per VM. A `✗ HTTPS fallback` line means containerd
is not honoring certs.d; the error includes the inspect command.

---

## Step 6 — Build Nico images

Builds Nico from `infra-controller-core` source and pushes to the registry VM.

```bash
python3 build-nico-components.py ~/sites/dc1
```

This compiles all Rust components — takes 10–20 minutes on first run.
Subsequent runs use Docker layer cache and are much faster.

Verify the image was pushed:
```bash
curl -s http://7.132.0.6:5000/v2/nico/tags/list
# → {"name":"nico","tags":["latest"]}
# (tag defaults to nico-system.image.local_nico_tag in the site yaml; override with --tag)
```

---

## Step 7 — Deploy Nico

Installs the full Nico stack via helm in dependency order:
metallb → local-path-provisioner → cert-manager → Vault → ESO → postgres-operator → nico-prereqs → nico.
(These are also the release names accepted by `--skip-to`.)

```bash
python3 deploy-nico-system.py ~/sites/dc1
```

Vault runs in **dev mode** by default (`nico-system.vault.mode: dev` in the site yaml):
in-memory, auto-initialised and auto-unsealed — no unseal keys exist and **all Vault
state is lost when the pod restarts**. Only in production mode (`mode: production`)
does the script init/unseal Vault and store the keys in the `vault-unseal-keys` k8s secret.

Verify:
```bash
kubectl get pods -n nico-system
# All pods should be Running or Completed
```

---

## Step 8 — Build and configure CLIs

Build `nico-admin-cli` and `machine-a-tron`, then configure certificates.

```bash
# Build (compiles from infra-controller-core — first run ~5 min)
python3 build-nico-clis.py ~/sites/dc1 --install-to ~/.local/bin

# Configure certs and run scripts
python3 configure-clis.py ~/sites/dc1
```

Test:
```bash
~/sites/dc1/run-admin-cli.sh version
```

---

## Re-unseal Vault after cluster restart (production mode only)

If Vault runs in production mode and the k8s cluster restarts, Vault needs to be unsealed:

```bash
python3 deploy-nico-system.py ~/sites/dc1 --unseal
```

In the default dev mode this is neither needed nor possible (no `vault-unseal-keys`
secret exists) — but Vault state is gone after a restart; re-run `deploy-nico-system.py`.

---

## Teardown

```bash
python3 destroy-site.py ~/sites/dc1
```

---

## Troubleshooting

**Fabric not healthy — BGP not Established:**
```bash
# Check FRR container logs
docker logs clab-dc1-spine-1
# Check BGP from inside a container
docker exec clab-dc1-spine-1 vtysh -c "show bgp summary"
```

**VM not booting / cloud-init stuck:**
```bash
python3 check-vms.py ~/sites/dc1 --watch
# Check VM console (VM names use the dc/site nicks)
sudo virsh console dc1-site-cp-1
```

**Helm release stuck (pending-install):**
```bash
kubectl -n nico-system delete secret -l owner=helm,name=nico,status=pending-install
```

**Image pull errors:**
```bash
# SSH into a CP VM (ssh-vm.py looks up the VM's IP), then curl the registry
python3 ssh-vm.py ~/sites/dc1/site.yaml cp-1 --via oob
# inside the VM:
curl -s http://7.132.0.6:5000/v2/nico/tags/list
```
