# nico-dev — Getting Started (golden image)

You received `nico-dev-golden-YYYYMMDD.utm` — a complete NVIDIA Infra
Controller (nico) dev environment in one VM: Kubernetes cluster, the full
nico stack, a simulated FRR datacenter fabric, and the MAT fleet simulator.
Target: **working cluster ~5 minutes after boot.**

## Requirements

Tiered by what you want to do — start with Tier 0, add tiers when needed:

| Tier | You want to… | You need |
|---|---|---|
| **0 — Run it** (demo, GUI, kubectl-from-VM, MAT runs) | just use the sim | **Apple Silicon Mac** (M1+; Intel cannot run this image), **UTM** (Mac App Store or https://mac.getutm.app), **~40 GB free disk** |
| **1 — Develop** (build nico from source, redeploy) | the dev cycle | + **colima** + **docker** CLI (`brew install colima docker`, `colima start --cpu 4 --memory 8`), **kubectl** + **helm**, **python3 + pyyaml**, **git** — and budget **~100 GB free disk** (images, registry, build caches grow fast; `docker builder prune -af` reclaims periodically) |
| **2 — Mac-side CLIs** (nico-admin-cli, nicocli on your Mac) | native CLIs | + **rustup** (the repo pins its own toolchain version) and **Go** — client CLIs are host-built (they must be Mach-O); everything else builds in containers |
| **3 — Deploy pre-built NGC images** (no source build) | skip building | + an **NGC API key** with registry-read on your image's org/team (see how-to.md → "Deploying pre-built NGC images") |

## 1. Import

Double-click the `.utm` bundle (or UTM → Open). **Before booting**,
VM Settings → Sharing — BOTH fields matter:
1. **Directory Share Mode: `VirtFS`** (the default WebDAV mode will NOT
   work — the VM expects 9p)
2. **Path: click Browse and SELECT a folder** (e.g. `~/nico` — an empty
   folder is fine, but the Path field must NOT be left empty). The share
   name is detected automatically inside the VM — any name works.

> If Path is empty, the VM boots normally but has no share — first-boot
> stops with "the Mac shared folder is NOT mounted" and these same
> instructions. Set both fields, **reboot the VM**, rerun first-boot.

## 2. Boot and personalize

Boot the VM, then from your Mac terminal:

```bash
ssh nico@192.168.64.126        # password: Welcome123!
sudo bash /usr/local/lib/nico-dev/first-boot.sh
```

Answer the four prompts (SSH key, hostname, share name, your Mac folder
path). The script personalizes the VM, mounts your share, and restarts the
stack. Your **next** ssh will warn about a changed host key — expected
(the clone generated its own): `ssh-keygen -R 192.168.64.126` on the Mac.

**First-boot expectation:** pods (especially `nico-api`) may restart for a
few minutes while the database elects a leader. Settled = all pods
Running/Completed:

```bash
kubectl get pods -n nico-system
```

## 3. Mac-side setup

Everything below is also in `~/POST-SETUP.txt` on the VM, personalized:

```bash
# Route to the nico service VIPs (re-add after reboot, VPN reconnect,
# or whenever ALL UTM VMs have been stopped — macOS drops it silently)
sudo route -n add -net 11.133.1.0/27 192.168.64.126
```

Then open **https://11.133.1.17/admin** — the admin GUI (accept the
self-signed cert). Plain-text `Forge development build` at the root path
is healthy.

## 4. If something's wrong

| Symptom | Cause → fix |
|---|---|
| No SSH at all, ever | Your Mac's UTM subnet differs from the baked IP. Serial console: `/Applications/UTM.app/Contents/MacOS/utmctl attach <vm>` → adjust `/etc/netplan` to your host's vmnet subnet |
| GUI/VIP times out from the Mac | The route (step 3) is missing — re-add it |
| GUI/VIP **connection refused**, pods all Running | Clone cold-start quirk: `kubectl rollout restart deployment/nico-api -n nico-system`, wait ~30 s |
| Pods stuck Pending/ImagePull | Shouldn't happen — all images are baked in. If it does, report it |

## 5. What next

On the VM, `ndev` shows site status (`ndev fabric verify` for the full
fabric health check). The full guide — building nico from source, dev
cycles, running the MAT fleet simulator, CLIs — is `how-to.md` in the
`nico-dev/` folder that first-boot copied into your share.

**Please note anything confusing or broken — your friction is our bug
tracker.**
