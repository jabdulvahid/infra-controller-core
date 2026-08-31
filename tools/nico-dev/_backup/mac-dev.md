# nico-dev — Mac Developer Setup Guide

Complete guide to setting up a nico-dev simulation environment on a MacBook
(Apple Silicon M-series). No nested virtualization required.

---

## Architecture

```
Mac (development host)
  ├── IDE, git, GitHub/GitLab auth
  ├── cargo build → ARM64 binaries
  ├── docker build + local registry (port 5000)
  ├── run-admin-cli.sh, run-mat.sh, nsim — run natively
  └── ~/projects/ (mounted in VM via SSHFS)
        ├── infra-controller-core/
        └── claude-notes/

UTM Linux VM (mac-sim-host-vm) — simulation host
  ├── ContainerLab fabric (super-spine → spine → leaf-cp + leaf-mat)
  ├── DPU stand-in (FRR container, wired to leaf-cp)
  ├── k3s single-node cluster (VM IS the CP node)
  └── Nico helm stack
```

---

## Prerequisites — Mac

### 1. Install UTM

Download from https://mac.getutm.app/ (free) or Mac App Store.

### 2. Create Ubuntu ARM64 VM in UTM

- **Backend:** Apple Virtualization (not QEMU emulation — much faster)
- **OS:** Ubuntu 24.04 Server ARM64
- **RAM:** 8 GB minimum (12 GB recommended)
- **vCPUs:** 4
- **Disk:** 60 GB
- **Network:** Shared Network (NAT) — required for internet access

> **Important:** Use "Shared Network" mode. "Bridged" mode breaks when
> you change networks (office vs home). Shared Network always gives the
> VM gateway `192.168.64.1` regardless of Mac's network.

### 3. Enable SSH on Mac

System Settings → General → Sharing → **Remote Login → ON**

This allows the VM to mount Mac sources via SSHFS.

### 4. Start a local Docker registry on Mac

```bash
docker run -d -p 5000:5000 --restart=always --name registry registry:2
```

Nico images built on Mac are pushed here and pulled by the VM.

---

## VM Setup (one-time)

SSH into the VM or use UTM console.

### 5. Pull and run the base setup script

> **Note:** Run git operations on Mac, not the VM (SSHFS is read-only for git).

On **Mac:**
```bash
cd ~/projects/claude-notes && git pull
```

On **VM:**
```bash
bash /mnt/mac/claude-notes/nico-dev/prepare-vm.sh
```

If the script fails partway (e.g. ContainerLab or kubectl), run these manually:

```bash
# ContainerLab
bash -c "$(curl -sL https://get.containerlab.dev)"
sudo usermod -aG clab_admins $USER

# Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# kubectl (download to /tmp to avoid SSHFS write errors)
cd /tmp
curl -L -o kubectl \
  "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/arm64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
cd -

# SSHFS config
grep -q "user_allow_other" /etc/fuse.conf || \
  echo "user_allow_other" | sudo tee -a /etc/fuse.conf

# Mount point
mkdir -p ~/mac
```

**Log out and back in** after adding to `clab_admins` group.

### 6. Mount Mac sources in VM

```bash
sshfs <your-mac-username>@192.168.64.1:/Users/<your-mac-username>/projects /home/$USER/mac \
  -o allow_other,default_permissions,reconnect,ServerAliveInterval=15
```

Verify:
```bash
ls ~/mac/
# Should show: claude-notes  infra-controller-core
```

To make the mount persistent, add to `/etc/fstab`:
```
<mac-user>@192.168.64.1:/Users/<mac-user>/projects /home/<vm-user>/mac \
  fuse.sshfs allow_other,default_permissions,reconnect,ServerAliveInterval=15,_netdev 0 0
```

---

## Networking Notes

- **VM gateway (Mac):** `192.168.64.1` — always fixed in UTM Shared Network
- **VM IP:** typically `192.168.64.2` (check with `ip addr show enp0s1`)
- **SSH from Mac to VM:** `ssh <user>@192.168.64.2`
- **ping from VM to 8.8.8.8 may fail** — NVIDIA corporate firewall blocks ICMP.
  Test connectivity with `curl -s http://1.1.1.1` instead.
- **Mac firewall** (company-managed): only blocks ICMP; TCP/UDP work fine.

---

## Create a dev site

On **Mac:**
```bash
cd ~/projects/claude-notes
python3 nico-sim/create-new-site.py \
  --config-size dev \
  --dc-name dc1 \
  --site-name dev \
  --underlay 7 \
  --overlay 8 \
  --folder ~/sites/dev
```

Edit `~/sites/dev/dev.yaml` and update `infra_controller_repo` to the
path as seen from the VM (e.g. `/home/<vm-user>/mac/infra-controller-core`).

---

## Deploy

### Step 1 — Fabric (on VM, as root)

```bash
sudo python3 ~/mac/claude-notes/nico-dev/deploy-dev-fabric.py ~/mac/sites/dev
```

### Step 2 — k3s + MetalLB (on VM, as root)

```bash
sudo python3 ~/mac/claude-notes/nico-dev/deploy-dev-cp.py ~/mac/sites/dev
```

### Step 3 — Build Nico images (on Mac)

```bash
cd ~/projects/infra-controller-core
cargo build -p nico --release

# Build and push to local registry
python3 ~/projects/claude-notes/nico-sim/build-nico-components.py ~/sites/dev
```

### Step 4 — Deploy Nico (on Mac)

```bash
python3 ~/projects/claude-notes/nico-dev/deploy-dev-nico.py ~/sites/dev
```

### Step 5 — Configure CLIs (on Mac)

```bash
python3 ~/projects/claude-notes/nico-sim/configure-clis.py ~/sites/dev
```

### Step 6 — Run admin-cli and MAT (on Mac)

```bash
# Test API
~/sites/dev/run-admin-cli.sh version

# Run MAT
~/sites/dev/run-mat.sh
```

To reach Nico VIPs (7.133.1.x) from Mac, use sshuttle:
```bash
sudo sshuttle -r <vm-user>@192.168.64.2 7.133.1.0/27
```

---

## Teardown

On **VM:**
```bash
sudo python3 ~/mac/claude-notes/nico-dev/destroy-dev.py ~/mac/sites/dev
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `ping 8.8.8.8` fails from VM | Use `curl http://1.1.1.1` — ICMP is blocked by corporate firewall, TCP works |
| `ping 192.168.64.1` fails | UTM might be on Bridged not Shared Network — change in UTM VM settings |
| `git pull` in `/mnt/mac/...` fails | Run git on Mac, not VM — SSHFS mount is read-only for git operations |
| `curl -LO kubectl` fails | You're in the SSHFS mount dir — `cd /tmp` first |
| ContainerLab install `-v` error | Run `bash -c "$(curl -sL https://get.containerlab.dev)"` directly |
| `clab` permission denied | `sudo usermod -aG clab_admins $USER` then log out/in |
| Docker not found as non-root | `newgrp docker` or log out/in after `usermod -aG docker $USER` |
