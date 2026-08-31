# Running DC Simulation on macOS (Apple Silicon)

## Overview

The simulation was designed for a bare-metal Linux host (jaslinux). On a MacBook
the approach is: create one large Linux VM called **nico-sim** and run everything
inside it — ContainerLab, libvirt VMs, the k8s cluster, and the Nico stack.

Apple Silicon MacBooks use Apple's Hypervisor framework for ARM64 VMs, which is
fast enough to run nested VMs. The nico-sim VM will host its own libvirt/QEMU
instances for the CP and DPU stand-in VMs.

---

## Step 1 — Check your MacBook RAM

The inner VMs (3 CP nodes + 3 DPU stand-ins + registry) need ~34 GB RAM total.

```bash
# Check total unified memory
system_profiler SPHardwareDataType | grep Memory
```

| MacBook RAM | nico-sim RAM to allocate | nico-sim.yaml adjustment needed? |
|-------------|--------------------------|-------------------------------|
| 64 GB+      | 48 GB                    | No — use defaults             |
| 48 GB       | 36 GB                    | No — use defaults             |
| 32 GB       | 24 GB                    | Yes — see Section 6 below     |
| 16 GB       | Not recommended          | Too constrained               |

---

## Step 2 — Install UTM

UTM uses Apple's Hypervisor.framework for ARM64 guests — nearly native performance
and supports nested KVM (needed for libvirt inside nico-sim).

```
https://mac.getutm.app
```

Download and install the free version (not the App Store version — the direct
download supports KVM passthrough which we need for nested VMs).

---

## Step 3 — Download Ubuntu 24.04 ARM64 Server ISO

```
https://ubuntu.com/download/server/arm
```

Download the **Ubuntu Server 24.04 LTS ARM64** ISO (~1.5 GB). Save it somewhere
accessible (e.g., `~/Downloads/ubuntu-24.04-live-server-arm64.iso`).

---

## Step 4 — Create the nico-sim VM in UTM

1. Open UTM → click **+** → **Virtualize** (not Emulate — Virtualize uses the
   Apple Hypervisor for full speed)
2. Select **Linux**
3. Boot ISO: select the Ubuntu 24.04 ARM64 ISO you downloaded
4. Configure hardware:

| Setting | Value |
|---------|-------|
| CPU Cores | 10 (or leave at default, UTM will use available cores) |
| RAM | See table in Step 1 (48 GB recommended if you have it) |
| Storage | 300 GB (qcow2 — only uses actual space, not all at once) |
| Display | Disable (headless — we SSH in) |
| Network | Shared Network (NAT — gives internet access) |

5. Name the VM **nico-sim**
6. Click **Save** then **Play** (▶) to start

---

## Step 5 — Install Ubuntu

The VM boots from the ISO. Follow the Ubuntu Server installer:

- Language: English
- Keyboard: your choice
- Network: leave as-is (DHCP, gets internet via NAT)
- Storage: use entire disk, no LVM needed
- Profile:
  - Your name: anything
  - Server name: **nico-sim**
  - Username: **jabdulvahid** (match your jaslinux username — scripts use `$USER`)
  - Password: choose one
- SSH: **Install OpenSSH server** ✓ (check this)
- Featured snaps: none needed

Installation takes ~5 minutes. When done, UTM will show a login prompt.

---

## Step 6 — Find the VM IP and SSH in

In UTM, click on nico-sim → the IP address is shown under Network. Or from the
VM console, log in and run:

```bash
ip addr show | grep "inet "
```

From your MacBook terminal:
```bash
ssh jabdulvahid@<nico-sim-ip>
```

Add to `~/.ssh/config` for convenience:
```
Host nico-sim
    HostName <nico-sim-ip>
    User jabdulvahid
```

Then just: `ssh nico-sim`

---

## Step 7 — Configure nico-sim (run inside the VM)

### 7a. Enable nested virtualization (required for inner VMs)

```bash
# Verify KVM is available (should print something, not error)
ls /dev/kvm
```

If `/dev/kvm` exists, nested KVM is working. If not:
```bash
# Enable KVM module
sudo modprobe kvm
```

### 7b. Install base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    git python3 python3-pip python3-yaml \
    qemu-kvm libvirt-daemon-system libvirt-clients virt-manager \
    bridge-utils guestfs-tools cloud-image-utils \
    docker.io docker-compose-plugin \
    openssh-client curl wget jq
```

### 7c. Add yourself to required groups

```bash
sudo usermod -aG libvirt,kvm,docker $USER
newgrp libvirt
```

Log out and back in for group changes to take effect.

### 7d. Install ContainerLab

```bash
bash -c "$(curl -sL https://get.containerlab.dev)"
```

### 7e. Install kubectl and helm

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/arm64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

---

## Step 8 — Clone claude-notes and infra-controller-core

```bash
# Your notes/scripts repo
git clone -b nico-dev https://github.com/jabdulvahid/infra-controller-core.git

# Nico source (for helm charts and sim Dockerfiles)
git clone <infra-controller-core-url> ~/projects/infra-controller-core
```

Update `nico-sim.yaml` if needed:
```yaml
infra_controller_repo: /home/jabdulvahid/projects/infra-controller-core
```

---

## Step 9 — Adjust nico-sim.yaml for MacBook (32 GB MacBooks only)

If your MacBook has only 32 GB RAM and nico-sim is allocated 24 GB, reduce the
inner VM sizes so everything fits:

```yaml
control_plane:
  sizing:
    ram_mb: 4096    # was 8192 — saves 12 GB across 3 nodes
    vcpus: 4        # was 8
    disk_gb: 60     # was 100

  dpu_sizing:
    ram_mb: 1024    # was 2048 — saves 3 GB across 3 DPU VMs
    vcpus: 1        # was 2

registry:
  sizing:
    ram_mb: 2048    # was 4096
    vcpus: 2        # was 4
```

This reduces total inner VM RAM from ~34 GB to ~17 GB, leaving headroom for
the host OS and k8s workloads.

---

## Step 10 — Run the simulation

From inside nico-sim, follow the standard flow in `test-run.md`. Everything
from Step 1 (fabric) through Step 8 (Nico deploy) runs identically:

```bash
cd ~/claude-notes/dc-simulation

# 1. Generate fabric
python3 generate-fabric.py nico-sim.yaml

# 2. Deploy ContainerLab fabric
cd output && sudo ./deploy.sh && cd ..

# 3. Generate and deploy VMs
python3 generate-nodes.py nico-sim.yaml
cd vm && sudo ./deploy-nodes.sh && cd ..

# 4. Form k8s cluster
sudo python3 form-k8s-cluster.py nico-sim.yaml

# 5. Configure registry trust
sudo python3 setup-insecure-registry.py nico-sim.yaml

# 6. Build nico images
sudo python3 build-nico-components.py nico-sim.yaml

# 7. Generate sim-values and deploy Nico
python3 generate-sim-values.py nico-sim.yaml
export KUBECONFIG=~/.kube/config-dc-sim
python3 deploy-nico-system.py nico-sim.yaml
```

---

## Performance expectations

| Operation | jaslinux (bare metal) | nico-sim (nested VM) |
|-----------|----------------------|---------------------|
| ContainerLab fabric deploy | ~30s | ~45s |
| CP VM boot (golden image) | ~30s | ~60s |
| kubeadm cluster formation | ~10 min | ~15 min |
| Nico stack deploy | ~25 min | ~35 min |
| Total cold deploy | ~60 min | ~90 min |

Nested VM overhead is ~1.5–2× for VM-heavy operations. ContainerLab
containers are faster (no nesting, runs in the host Linux kernel).

---

## Networking note

The NAT network in UTM means the MacBook can SSH into nico-sim, but nico-sim
cannot be reached from outside the MacBook. This is fine for development.
If you want to access Nico API from the MacBook host directly, add a port
forward in UTM: Host port 11079 → Guest port 1079 (nico-api).
