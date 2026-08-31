# nico-dev — Technical Conversations

Captured discussions and decisions from development sessions. Dated and indexed for reference.

---

## 2026-08-14/15

### nico-dev features

#### kubeadm instead of k3s

**Decision:** Switched CP install from k3s to kubeadm.

k3s was causing problems: `--flannel-iface=br-dev-cp` baked into the systemd service file meant k3s failed to start on reboots when `br-dev-cp` didn't exist yet (ContainerLab not deployed). k3s also has quirks: separate containerd namespace from Docker, bundled flannel config, non-standard service file layout.

kubeadm is standard Kubernetes. Setup:
1. containerd already present via Docker (`containerd.io` package)
2. Install `kubeadm kubelet kubectl` from `pkgs.k8s.io`
3. Configure containerd: `SystemdCgroup = true`, `certs.d` for insecure registry mirror
4. `kubeadm init --pod-network-cidr=10.244.0.0/16`
5. Apply flannel CNI
6. Remove control-plane taint (single-node)

Node IP set via `/etc/default/kubelet`: `KUBELET_EXTRA_ARGS="--node-ip=7.132.1.1"` so MetalLB BGP-peers from the right address. Kubeconfig points to the VM's primary interface (192.168.64.2:6443) — works from both VM and Mac.

---

#### Boot sequence problem: kubelet starts before fabric

After reboot, `br-dev-cp` doesn't exist until ContainerLab is deployed. kubelet was configured with `--node-ip=7.132.1.1` which lives on `br-dev-cp`. Swap was also re-enabled on reboot (`swapoff -a` is not persistent).

Fixes applied:
- `deploy-dev-cp.py`: comment out swap entries in `/etc/fstab` permanently
- `deploy-dev-fabric.py`: run `swapoff -a` before kubelet restart (defensive, handles VMs cloned before the fstab fix)
- `deploy-dev-fabric.py`: restart kubelet at end of fabric deploy (Step 7) — kubelet comes up only after bridges and fabric routes exist

---

#### Self-installing boot service

**Problem:** After reboot, ContainerLab containers are gone (ephemeral). Previously required manual re-run of `deploy-dev-fabric.py` + `systemctl restart kubelet`.

**Solution:** `deploy-dev-fabric.py` installs itself as a systemd service on every run.

- Copies itself + `generate-dev-fabric.py` to `/usr/local/lib/nico-dev/` (SSHFS-independent — works at boot before Mac is mounted)
- Registers `nico-dev-fabric.service` (Type=oneshot, RemainAfterExit=yes)
- On boot: service re-runs the local copy → bridges → clab deploy → routes → kubelet restart
- On every `deploy-dev-fabric.py` run: local copy is refreshed from source (idempotent)

One source of truth: same script, same logic for both interactive deploy and boot service.

Key fix: skip the copy when `src.resolve() == dst.resolve()` (running from local copy → `shutil.SameFileError` otherwise).

---

#### Site path decoupled from service unit (cloud-init readiness)

Site path (`/home/jabdulvahid/sites/dev/dev.yaml`) was baked into the systemd unit file — not portable for golden VM images.

Fix: `EnvironmentFile=/etc/nico-dev/env` containing `NICO_DEV_SITE=<path>`. The service unit references `${NICO_DEV_SITE}` — no username or path in the unit itself.

For cloud-init: golden image bakes the unit file and scripts. cloud-init writes `/etc/nico-dev/env` per instance. That's the only instance-specific file.

---

#### Golden VM image strategy

**Goal:** Zero-friction dev environment. Boot → everything works. No setup required.

**What gets baked into the golden image:**
- OS + Docker + containerd + kubeadm/kubelet/kubectl + ContainerLab
- FRR ARM64 Docker image pre-built
- `/usr/local/lib/nico-dev/` scripts
- `nico-dev-fabric.service` enabled
- `nico-dev-fabric.service` EnvironmentFile at `/etc/nico-dev/env`
- kubeadm cluster initialized (192.168.64.2:6443 — UTM Shared Network is deterministic)
- MetalLB + cert-manager + vault + ESO + postgres-operator deployed
- Nico helm stack deployed
- **Nico images pre-pulled into containerd** (`imagePullPolicy: IfNotPresent`)

Why not bake kubeadm? Originally thought to avoid it (certs tied to IP/hostname). But UTM Shared Network always gives 192.168.64.2 — the IP is deterministic. Single-node cluster has no identity conflicts. So kubeadm can be pre-baked.

**Boot sequence after clone:**
1. `nico-dev-fabric.service` auto-runs → fabric up → kubelet restarts
2. k8s cluster comes back (static pods from etcd)
3. Nico pods start using cached images (`IfNotPresent`)
4. Everything running in minutes, no Mac needed

---

#### Two-mode design: demo vs dev

**Demo mode (default, no Mac needed):**
- Nico pods use pre-pulled `nico:latest` from containerd cache
- `imagePullPolicy: IfNotPresent` skips pull
- Boot → Nico running, learn and test freely

**Dev mode (code change workflow):**
```bash
# Mac — once per session
docker run -d -p 5000:5000 --name registry registry:2

# Mac — after each code change
python3 build-dev-nico.py <site>         # builds nico:<git-sha>, pushes to localhost:5000

# VM — rolling update
helm upgrade nico <helm-dir> -n nico-system \
  --reuse-values --set global.image.tag=<git-sha>

# VM — revert to demo mode (no Mac needed)
helm upgrade nico <helm-dir> -n nico-system \
  --reuse-values --set global.image.tag=latest
```

**Why tag-based (not imagePullPolicy:Always):**
`IfNotPresent` skips pull only when the exact `image:tag` is cached. A new git SHA tag is never in cache, so k8s always pulls it — no pull policy change needed. Switch between modes is one `helm upgrade --set global.image.tag=`.

---

#### ARM64 build toolchain

nico-sim used x86_64 Dockerfiles. nico-dev is ARM64 (Apple Silicon Mac + UTM ARM64 VM).

New files in `nico-dev/nico-dev-docker/`:
- `Dockerfile.runtime-dev`: ARM64 Debian runtime, fluent-bit removed (GPG key drift, not needed in dev). Uses `arm64v8/debian:12-slim`. kea hooks at `/usr/lib/aarch64-linux-gnu/kea/hooks`.
- `Dockerfile.nico-dev`: ARM64 dev build. Skips CI gates (`cargo build --release` instead of `cargo make`). Fixes bmc-mock duplicate `[dev-dependencies]` (same awk trick as x86_64 sim). Separate sccache cache ID (`sccache-aarch64-dev`).

`build-dev-nico.py` copies these into the nico repo temporarily for the build, cleans up after — nico repo is never permanently modified.

Build sequence: `build-container-aarch64` → `runtime-dev` → `nico:<git-sha>`

---

#### Bugs fixed during this session

| Script | Bug | Fix |
|---|---|---|
| `deploy-dev-fabric.py` | `run()` called with `capture=True` kwarg that didn't exist | Removed kwarg — `run()` always captures |
| `deploy-dev-cp.py` | `swapoff -a` not persistent; kubelet fails after reboot | Comment out swap in `/etc/fstab` |
| `deploy-dev-fabric.py` | `shutil.SameFileError` when service runs local copy | Skip copy when `src.resolve() == dst.resolve()` |
| `deploy-dev-cp.py` | `--flannel-iface=br-dev-cp` broke kubelet on restart | Removed flag entirely (single-node doesn't need it) |

---

## 2026-08-15/16

### ARM64 image build (Mac → VM)

#### OOM during cargo build (signal 9)

**Symptom:** BuildKit daemon killed mid-build with `signal: killed` or EOF after ~150s.

**Cause:** Colima's VM ran out of memory during peak parallel Rust compilation. Default Colima config (4 CPUs, 8GB) is insufficient.

**Fixes:**
1. Restart Colima with more resources: `colima stop --force && colima start --cpu 6 --memory 12 --disk 60`
2. Add `--jobs 2` to `cargo build` in `Dockerfile.nico-dev` to cap parallelism:
   ```dockerfile
   cargo build --release --workspace --exclude bmc-mock --jobs 2
   ```

---

#### Image size: 10.7GB uncompressed (2.1GB compressed)

**Cause:** `CARGO_PROFILE_RELEASE_DEBUG=true` in `Dockerfile.nico-dev` embeds full debug symbols into every binary.

**Fix:** Changed to `CARGO_PROFILE_RELEASE_DEBUG=false`. Image dropped from 2.1GB to 265MB compressed — 8× reduction.

**Impact:** No debugger support in dev images. Acceptable tradeoff; debug symbols can be re-enabled per developer if needed.

---

### deploy-dev-nico.py fixes

#### local-path-provisioner: rancher helm repo defunct

**Symptom:** `helm repo add rancher-latest https://releases.rancher.com/server-charts/latest` returns 404.

**Fix:** Switch to containeroo chart (public, no auth, ARM64-compatible):
```python
helm(['repo', 'add', 'containeroo', 'https://charts.containeroo.ch'], kubeconfig)
helm(['upgrade', '--install', 'local-path-provisioner',
      'containeroo/local-path-provisioner', ...], kubeconfig)
```

---

#### postgres-operator: exec format error (x86 image on ARM64)

**Symptom:** `exec /usr/local/bin/postgres-operator: exec format error` — operator pod crashes immediately.

**Cause:** Zalando's default registry (`registry.opensource.zalan.do`) only publishes x86_64 images.

**Fix:** Override image registry to `ghcr.io` (publishes multi-arch including ARM64):
```python
'image': {'registry': 'ghcr.io', 'repository': 'zalando/postgres-operator'}
```
Also bumped chart version from `1.11.0` → `1.15.1` (first version with ARM64 ghcr.io images).

---

#### nico-system namespace: helm ownership conflict

**Symptom:** `helm upgrade --install nico-prereqs` fails with either "namespace already exists" or "namespace not found".

**Cause:** The namespace must exist before helm install (chart resources deploy before the Namespace resource in the manifest), but helm rejects a pre-existing namespace without its ownership labels.

**Fix:** Pre-create namespace with helm labels before running helm:
```python
kubectl(['create', 'namespace', 'nico-system'], kubeconfig, check=False)
kubectl(['label', 'namespace', 'nico-system',
         'app.kubernetes.io/managed-by=Helm', '--overwrite'], kubeconfig, check=False)
kubectl(['annotate', 'namespace', 'nico-system',
         'meta.helm.sh/release-name=nico-prereqs',
         'meta.helm.sh/release-namespace=nico-system', '--overwrite'], kubeconfig, check=False)
```

---

#### vault-pki-config: permission denied on vault tune

**Symptom:** `vault-pki-config` init container fails with permission denied when calling `vault secrets tune`.

**Cause:** nico-prereqs creates `nico-vault-token` secret empty; the vault token is never injected. Root cause: helm chart doesn't populate the token unless passed explicitly.

**Fix (from nico-sim):** Pass `--set vault.token=root` to nico-prereqs helm install. Value read from `nico-dev.yaml`:
```yaml
nico-system:
  vault:
    mode: dev
    dev_root_token: root
```
```python
vault_token = cfg.get('nico-system', {}).get('vault', {}).get('dev_root_token', 'root')
helm(['upgrade', '--install', 'nico-prereqs', ..., '--set', f'vault.token={vault_token}', ...])
```

---

#### postgres database not created (synchronous mode with 1 instance)

**Symptom:** `nico-api-migrate` fails: `database "nico_system_nico" does not exist`.

**Cause:** `helm-prereqs/templates/postgresql.yaml` hardcodes `synchronous_mode: true` and `synchronous_commit: "remote_apply"`. With only 1 postgres instance (no replicas), Patroni strict sync mode blocks ALL writes indefinitely — including the operator's own `CREATE DATABASE`. The database never gets created.

**Fix:** Templated the synchronous fields in `postgresql.yaml`:
```yaml
patroni:
  synchronous_mode: {{ .Values.postgresql.synchronousMode | default true }}
  synchronous_mode_strict: {{ .Values.postgresql.synchronousMode | default true }}
```
Added `synchronousMode: true` to `helm-prereqs/values.yaml` (default unchanged for production).
Pass `synchronousMode: false` from `gen_nico_prereqs` when `nico-dev.yaml` has `synchronous_mode: false`.

**Note:** This required a change to `infra-controller-core/helm-prereqs/` — needs to be committed to the nico repo.

---

### containerd insecure registry (Mac → VM image pull)

#### HTTPS vs HTTP error

**Symptom:** `nico-api-migrate` pod fails with:
```
Head "https://192.168.64.1:5000/v2/nico/manifests/latest": http: server gave HTTP response to HTTPS client
```

**Cause:** containerd defaults to HTTPS. The Mac registry (`docker run registry:2`) serves plain HTTP.

**Fix:** Create `hosts.toml` in containerd's certs.d and set `config_path`:
```bash
sudo mkdir -p /etc/containerd/certs.d/192.168.64.1:5000
printf 'server = "http://192.168.64.1:5000"\n\n[host."http://192.168.64.1:5000"]\n  capabilities = ["pull", "resolve"]\n' | \
  sudo tee /etc/containerd/certs.d/192.168.64.1:5000/hosts.toml
```
Also required setting `config_path` in `/etc/containerd/config.toml` (was empty string by default):
```toml
[plugins.'io.containerd.cri.v1.images'.registry]
  config_path = '/etc/containerd/certs.d'
```

**Test (from VM):** `sudo crictl pull 192.168.64.1:5000/test:latest` — instant if working.

**Note:** `ctr` does NOT respect `certs.d` (bypasses CRI plugin). Always use `crictl` to test k8s-facing pull behaviour.

Added to `prepare-vm.sh` so it's applied automatically on new VMs.

---

### VM sizing and disk

#### LVM not using full disk

**Symptom:** `df -h /` showed 30GB used space limit, but disk was 64GB. After large image pulls, disk pressure caused pod evictions.

**Cause:** Ubuntu cloud image creates LVM with `ubuntu-lv` at 30GB even on larger disks. The remaining ~30GB is unallocated in the VG.

**Fix:**
```bash
sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
sudo resize2fs /dev/mapper/ubuntu--vg-ubuntu--lv
```

Added to `prepare-vm.sh` as automatic step after hardware preflight.

#### Hardware requirements

Added preflight check to `prepare-vm.sh` — aborts with clear error if VM doesn't meet:
- Disk ≥ 100GB
- RAM ≥ 16GB
- CPUs ≥ 8

---

### Troubleshooting commands

#### Registry
```bash
# Check image exists and size (run on Mac or VM)
curl -s http://192.168.64.1:5000/v2/nico/manifests/latest \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" | \
  python3 -c "import json,sys; layers=json.load(sys.stdin)['layers']; print(f'{sum(l[\"size\"] for l in layers)/1024/1024:.0f} MB')"

# Test containerd registry trust (VM)
sudo crictl pull 192.168.64.1:5000/test:latest
```

#### Stuck pods
```bash
# Force delete all pods in a namespace
kubectl -n nico-system delete pod --all --force --grace-period=0

# Prune unused images
sudo crictl rmi --prune

# Check image architecture
sudo crictl inspecti <image> | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['info']['imageSpec']['architecture'])"
```

#### SSH host key
```bash
# Check if secret has wrong format (PKCS#8 instead of OpenSSH)
kubectl get secret ssh-host-key -n nico-system -o jsonpath='{.data.ssh_host_ed25519_key}' | base64 -d | head -1
# Wrong: "-----BEGIN PRIVATE KEY-----"
# Right: "-----BEGIN OPENSSH PRIVATE KEY-----"

# Fix: delete and let deploy-dev-nico.py re-create in correct format
kubectl delete secret ssh-host-key -n nico-system
# Then re-run deploy-dev-nico.py --skip-to nico-prereqs
```

---

### nico-ssh-console-rs CrashLoopBackOff (SSH key format)

**Symptom:**
```
Error: SshServerSpawn(ReadingHostKeyFile { path: "/etc/ssh/ssh_host_ed25519_key",
  error: Encoding(Pem(UnexpectedTypeLabel { expected: "OPENSSH PRIVATE KEY" })) })
```

**Cause:** Helm's built-in `genPrivateKey "ed25519"` produces PKCS#8 PEM (`-----BEGIN PRIVATE KEY-----`). ssh-console-rs requires OpenSSH PEM (`-----BEGIN OPENSSH PRIVATE KEY-----`). `ssh-keygen -t ed25519` always generates the correct OpenSSH format.

**Fix:** Set `sshHostKey.create: false` in nico-prereqs values. Pre-create the secret using `ssh-keygen` before helm install (matching `bootstrap_ssh_host_key.sh` in helm-prereqs/). Added as `ensure_ssh_host_key()` in `deploy-dev-nico.py`, called before nico-prereqs helm install.

---

#### Helm
```bash
# Clear stuck helm release state
kubectl -n nico-system delete secret -l owner=helm,name=nico-prereqs

# Uninstall without error if not present
helm -n nico-system uninstall nico-prereqs --ignore-not-found
```

#### Containerd / kubelet
```bash
# Recover from stale sandbox state (PodReadyToStartContainers stuck False)
sudo systemctl restart containerd
sudo systemctl restart kubelet

# Expand LVM to full disk
sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
sudo resize2fs /dev/mapper/ubuntu--vg-ubuntu--lv
```

---

## 2026-08-16

### nico-dev philosophy

**Decision:** nico-dev is a downgrade of the fabric and cluster only — not of the Nico API service.

The nico-api `siteConfig` TOML in nico-dev must be **identical** to nico-sim's. nico-dev uses fewer leafs, fewer CPs, one DPU — but the Nico feature set is the same. This means:

- `generate_dev_values.py` is a near-copy of `nico-sim/generate-sim-values.py`
- The only differences are fabric-specific (single DPU, different prefixes, ARM64 kea path)
- All FNN, routing profiles, pools, and TOML structure match nico-sim exactly

Rationale: developers testing Nico features need a fully functional Nico — not a simplified one. Only the underlying fabric/hardware is reduced.

---

### generate_dev_values.py — new file

**What:** New file `nico-dev/generate_dev_values.py` that generates all Helm values files for the nico-dev deployment. Near-copy of `nico-sim/generate-sim-values.py` with minimal fabric-specific differences.

**Differences from nico-sim's script:**
- `collect_deny_prefixes`: no OOB/mgmt sections (nico-dev has no libvirt/ContainerLab mgmt net)
- `gen_site_config_toml`: lo-ip pool skips 1 IP (single dev DPU) instead of `num_vms`; reads `fabric.prefixes.overlay` for `site_fabric_prefixes`
- `gen_nico`: reads `registry.host/port/nico_tag`; overrides kea hook path to ARM64 (`/usr/lib/aarch64-linux-gnu/kea/hooks/libdhcp.so`)
- `gen_nico_prereqs`: adds `synchronousMode` for single-instance postgres; sets `sshHostKey.create: false` (pre-created in OpenSSH format)
- `gen_zalando_postgres_op`: overrides image to `ghcr.io/zalando/postgres-operator` (ARM64)
- No `validate_sim` dependency

**How used:** `deploy-dev-nico.py` imports `generate(cfg, outdir)` from this file, replacing all inline value generators.

---

### nico-dev.yaml — missing pools and FNN block

**Problem:** `nico-api` crashed on startup with `resource pool 'vpc-vni' missing`. Comparison with nico-sim revealed several missing pools and the entire `fnn:` block.

**Additions to `nico-dev.yaml`:**

```yaml
fabric:
  prefixes:
    overlay: 8.150.0.0/16   # EVPN/VXLAN tenant space (was missing)

nico-system:
  helm-values:
    pools:
      vpc_vni:          { start: 60101,           end: 60999            }
      vpc_dpu_lo:       { start: "10.255.247.3",  end: "10.255.247.255" }
      external_vpc_vni: { start: 51008,           end: 51011            }
      fnn_asn:          { start: "4268031000",    end: "4268031099"     }

    fnn:
      enabled: true
      admin_vpc_vni: 61000
      routing_profiles:
        INTERNAL:
          route_target_imports: [managed_node_bmc, admin_network]
          route_targets_on_exports: [internal_tenant]
        EXTERNAL:
          route_target_imports: [infra_egress]
          route_targets_on_exports: [external_tenant]
        PRIVILEGED_INTERNAL:
          route_target_imports: [managed_node_bmc, admin_network, site_controller]
          route_targets_on_exports: [internal_tenant]
```

**Root cause of original omission:** nico-dev.yaml was initially created from scratch, not from nico-sim's site config. The generate script now validates that `fnn_asn` pool exists (raises ValueError if missing).

---

### deploy-dev-nico.py — refactor to use generate_dev_values.py

**Before:** Inline value generators (`gen_vault`, `gen_nico_prereqs`, `gen_nico`, etc.) duplicated inside `deploy-dev-nico.py`, diverging from nico-sim.

**After:** All value generation delegated to `generate_dev_values.py`:
```python
sys.path.insert(0, str(Path(__file__).parent))
from generate_dev_values import generate as gen_values
gen_values(cfg, vals_dir)
```

Values written to `{site_folder}/dev-values/` then passed to helm via `-f`.

---

### deploy-dev-nico.py — registry preflight check

**Added:** Curl check against Mac registry before any helm installs. Fails fast with clear message if registry is unreachable or image not pushed yet.

```python
r = subprocess.run(['curl', '-sf',
    f'http://{registry}/v2/nico/manifests/{nico_tag}',
    '-H', 'Accept: application/vnd.docker.distribution.manifest.v2+json'],
    capture_output=True, text=True)
if r.returncode != 0:
    print(f'Error: cannot reach {registry} or image nico:{nico_tag} not found')
    sys.exit(1)
```

**Note:** Uses `curl`, not `ctr` — `ctr` bypasses CRI and does not respect `certs.d`.

---

### deploy-dev-nico.py — wait loops after nico-prereqs

**Problem:** Helm `--wait` on nico-prereqs returns before vault-pki-config job completes and before postgres cluster is fully Ready. Nico install would then fail because the issuer and database weren't ready.

**Fixes added:**

1. **Wait for vault-pki-config job** (polls every 5s, 5min timeout):
   ```python
   while time.time() < deadline:
       r = kubectl(['get', 'job', 'vault-pki-config', '-n', 'nico-system',
                    '-o', 'jsonpath={.status.succeeded}'], kubeconfig, check=False)
       if r.stdout.strip() == '1':
           break
       time.sleep(5)
   ```

2. **Wait for nico-pg-cluster-0 Ready** (polls every 5s, 5min timeout):
   ```python
   while time.time() < deadline:
       r = kubectl(['get', 'pod', 'nico-pg-cluster-0', '-n', 'postgres',
                    '-o', 'jsonpath={.status.conditions[?(@.type=="Ready")].status}'],
                   kubeconfig, check=False)
       if r.stdout.strip() == 'True':
           break
       time.sleep(5)
   ```

3. **20s sleep** between nico-prereqs and nico install (matching nico-sim pattern).

---

### `false | default true` Go template bug

**Symptom:** Setting `synchronousMode: false` in site yaml had no effect — postgres cluster still used synchronous mode.

**Cause:** Go/Helm template evaluates `false` as "empty", so `{{ .Values.postgresql.synchronousMode | default true }}` returns `true` even when the value is explicitly `false`.

**Fix:** Remove `| default true`, use the value directly:
```yaml
synchronous_mode: {{ .Values.postgresql.synchronousMode }}
```
The helm-prereqs `values.yaml` default is `synchronousMode: true` so production is unchanged.

**Files changed:** `infra-controller-core/helm-prereqs/templates/postgresql.yaml`, `helm-prereqs/values.yaml`.

---

### build-nico-clis.py and configure-clis.py — new files

Copied from nico-sim and adapted for nico-dev. Both run on Mac.

**build-nico-clis.py changes from nico-sim:**
- `resolve_repo()`: checks `nico_mac_folder` → `nico_vm_folder` (nico-dev.yaml has no `infra_controller_repo`)
- `apply_setcap()`: detects macOS (`platform.system()`) and skips with a message — setcap is Linux-only; MAT must run with sudo on Mac (or on the VM)
- Next-step message points to `nico-dev/configure-clis.py`

**configure-clis.py changes from nico-sim:**
- `find_kubeconfig()`: first candidate is `{site_folder}/{dc_name}-dev.kubeconfig.yaml` (nico-dev naming convention)
- Vault token `X-Vault-Token: root` already matches nico-dev vault dev mode — no change needed
- `--admin-cli-only` flag added: skip MAT cert/config generation entirely
- MAT config and `run-mat.sh` include a note that MAT runs on the VM (not Mac) — `br-{dc_name}-internet` bridge lives inside the UTM VM, not on the Mac
- Done summary prints `scp` command to copy MAT certs + config to the VM
- Prints route command for Mac if API VIP is not reachable: `sudo route -n add -net <service_vips> 192.168.64.2`

**Run order:**
```bash
# Build (Mac — compiles from infra-controller-core)
python3 ~/claude-notes/nico-dev/build-nico-clis.py ~/sites/dev --install-to ~/.local/bin

# Configure certs (Mac — requires kubectl access to VM at 192.168.64.2:6443)
python3 ~/claude-notes/nico-dev/configure-clis.py ~/sites/dev

# Add Mac route if API VIP not reachable
sudo route -n add -net 7.133.1.0/27 192.168.64.2

# Test
~/sites/dev/run-admin-cli.sh version
```

**Why MAT runs on the VM:** MAT binds to `br-{dc_name}-internet` (the ContainerLab internet bridge) to simulate managed host BMC traffic. That bridge exists inside the UTM VM, not on the Mac. In nico-sim the bridges are Mac-side (libvirt), so MAT ran on the Mac there.

---

### Mac routing to API VIP

**Problem:** `run-admin-cli.sh version` timed out connecting to `7.133.1.17:443`.

**Root causes (two separate issues):**

1. **Wrong gateway IP in route.** The route was added via `192.168.64.2` but the VM is actually at `192.168.64.4`. UTM Shared Network does not always assign `.2` — check the kubeconfig for the real IP:
   ```bash
   grep server ~/sites/dev/dev-dev.kubeconfig.yaml
   # → https://192.168.64.4:6443
   ```

2. **Route not persistent.** macOS `route add` entries are lost on network changes (WiFi reconnect, sleep/wake). The route must be re-added each session.

**Fix:**
```bash
sudo route -n add -net 7.133.1.0/27 192.168.64.4
# verify
netstat -rn | grep 7.133
```

**Symptom that route is missing:** `traceroute 7.133.1.17` exits via `10.191.65.1` (default gateway) instead of `192.168.64.4`.

**Note:** The route gateway must match the VM's actual UTM IP — check `grep server ~/sites/dev/dev-dev.kubeconfig.yaml` to confirm before adding.

---

## 2026-08-17

### Golden image clone workflow — UTM setup and first-boot fixes

#### Vault dev mode loses state after reboot

**Symptom:** After VM reboot, `nico-api` crashes with Vault 403. `nico-dev-fabric.service` had re-deployed fabric but Vault's k8s auth config (written by `deploy-dev-nico.py`) was gone.

**Cause:** Vault was running in dev mode (in-memory), which loses all state on restart. Postgres retained its state; Vault did not — a bad combination.

**Fix:** Switched Vault to file storage with a hostPath volume at `/var/lib/vault` and an auto-unseal sidecar.

- `generate_dev_values.py` `gen_vault()` now supports `mode: file` (new default) and `mode: dev` (legacy).
- File mode config: standalone Vault with `storage "file" { path = "/vault/data" }`, hostPath volume, and `vault-unsealer` sidecar that polls `vault operator unseal` every 10s using the unseal key from a `vault-init-keys` secret.
- `deploy-dev-nico.py` gained `ensure_vault_initialized()`: detects first-init vs re-boot, runs `vault operator init -key-shares=1 -threshold=1`, stores unseal key + root token in `vault-init-keys` k8s secret, immediately unseals.
- `sites/dev/dev.yaml` changed: `vault.mode: file`, removed `dev_root_token`.

---

#### UTM clone — IP and shared folder inherited from original

**Symptom:** Cloned VM had same DHCP IP as original (same MAC address) and shared folder still pointing to original VM's path.

**Cause:** UTM clone copies all settings including MAC address and shared directory path.

**Required steps before booting a cloned VM:**
1. In UTM → VM settings → Network: regenerate MAC address (gives new DHCP IP)
2. In UTM → VM settings → Sharing: set shared folder to user's own Mac path (share name must remain `share`)
3. Add Serial device: Serial → Mode: TCP Server, Port: 4444

Documented in `how-to.md`.

---

#### TCP serial console for first login (paste support)

**Motivation:** UTM's built-in console doesn't support paste, making `first-boot.sh` unusable (SSH key, long paths).

**Solution:** UTM Serial → TCP Server on port 4444 + `serial-getty@ttyAMA0` on the guest side.

- `bake-golden-image.sh` Step 4b: `systemctl enable serial-getty@ttyAMA0.service`
- Connect from Mac: `socat - tcp:localhost:4444`
- Arrow keys don't work (serial limitation). **Use serial console only for first login + IP discovery; SSH for running first-boot.sh.**

Multiple socat flag combinations tried: `raw,echo=0` (no newlines), `crnl` (broke login), `echo=0` (no local echo). Plain `socat - tcp:localhost:4444` gave the best result.

---

#### first-boot.sh — missing IP patches for k8s static pod manifests

**Symptom:** After `first-boot.sh`, `kube-apiserver` and `etcd` crashed. VM had a new DHCP IP (e.g. `.6`) but manifests still had original golden-image IP (e.g. `.4`).

**Root cause:** `first-boot.sh` patched the user kubeconfig and `/etc/default/kubelet` node-ip, but missed four files that embed the cluster IP:

| File | What was hardcoded |
|---|---|
| `/etc/kubernetes/admin.conf` | `server: https://<old-ip>:6443` |
| `/etc/kubernetes/manifests/kube-apiserver.yaml` | `--advertise-address`, `host:` annotation, endpoint annotation |
| `/etc/kubernetes/manifests/etcd.yaml` | `--advertise-client-urls`, `--listen-client-urls`, `--listen-peer-urls`, `--initial-cluster` |
| `/etc/kubernetes/kubelet.conf` | `server: https://<old-ip>:6443` |

**Fix in `first-boot.sh` Step 5:**
```bash
OLD_IP=$(grep -oP 'https://\K[\d.]+(?=:6443)' /etc/kubernetes/admin.conf | head -1 || true)
if [[ -n "$OLD_IP" && "$OLD_IP" != "$VM_IP" ]]; then
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/admin.conf
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/manifests/kube-apiserver.yaml
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/manifests/etcd.yaml
    sed -i "s/${OLD_IP}/${VM_IP}/g" /etc/kubernetes/kubelet.conf
fi
cp /etc/kubernetes/admin.conf "$KUBECONFIG_PATH"   # copy already-patched file
```

Then the existing `systemctl restart kubelet` in Step 9 picks up all changes.

**Note:** `bake-golden-image.sh` Step 1 already re-issues the apiserver TLS cert with SANs for all UTM IPs (192.168.64.2–127), so the cert is valid for any assigned IP — only the config files needed patching.

---

#### frr.conf is a directory (leftover from failed run)

**Symptom:** `nico-dev-fabric.service` failed with `IsADirectoryError: [Errno 21] Is a directory: '/mnt/mac/sites/dev/fabric/frr.conf'`.

**Cause:** A previous run created `frr.conf` as a directory (likely a mkdir path collision).

**Fix:** `sudo rm -rf /mnt/mac/sites/dev/fabric/` then restart the fabric service.

---

#### Helm release stuck in pending-install

**Symptom:** `helm upgrade --install nico` fails: release in `pending-install` state.

**Cause:** Previous interrupted helm install left the release state secret in a terminal state.

**Fix:**
```bash
kubectl delete secret -n nico-system -l "owner=helm,name=nico,status=pending-install"
```
Then re-run deploy.

---

## 2026-08-18

### Switch to static IP VM (key architectural change)

**Decision:** VM now uses a fixed static IP (`192.168.64.126`) configured via netplan, replacing the DHCP approach.

**Why:** kubeadm embeds the node IP in etcd, kube-apiserver manifest, kubelet.conf, and admin.conf. With DHCP, cloned VMs get new IPs and all those files need patching in `first-boot.sh`. UTM Shared Network is a NAT pool — VMs are not guaranteed `.2`. The IP patching in `first-boot.sh` was fragile (wrong IP in `grep`, race conditions, apiserver cert SANs).

With a static IP baked into the golden image, every cloned VM has the same IP — no patching required at all. The kubeconfig, all k8s manifests, and MetalLB BGP config all just work on boot.

**Netplan config written to `/etc/netplan/50-static.yaml`:**
```yaml
network:
  version: 2
  ethernets:
    enp0s1:
      dhcp4: false
      addresses:
        - 192.168.64.126/24
      routes:
        - to: default
          via: 192.168.64.1
      nameservers:
        addresses:
          - 192.168.64.1
```

**DNS:** Must use `192.168.64.1` (UTM gateway) — not `1.1.1.1` or `8.8.8.8`. NVIDIA's network blocks port 53 to public DNS, which would break Docker container DNS. Gateway relays DNS correctly.

**Also:** Docker daemon.json must use the same `192.168.64.1` DNS. Static netplan does not inherit DHCP resolver, so Docker containers use the kernel default (`/etc/resolv.conf`) which is gone without a DNS server.

**Validation:** UTM crashed mid-session → VM restarted → all pods came back cleanly with no intervention, no IP patching. Static IP confirmed correct approach.

**Supersedes:** The 2026-08-17 entries about DHCP IP patching in `first-boot.sh` (`OLD_IP` sed loop, apiserver cert SANs for `.2`–`.127`) are no longer needed. `first-boot.sh` with static IP only needs to set the hostname and mount the shared folder.

---

### DNS check fix in fabric verify

**Problem:** `ndev fabric verify` showed ⚠ DNS warnings for failed pings to `1.1.1.1` and `8.8.8.8`. NVIDIA's network blocks port 53 to those nameservers, causing false warnings for all users inside NVIDIA network.

**Attempts:**
1. Remove explicit nameserver from `nslookup` → FRR container has no `nslookup`
2. Try `dig` → also not in FRR container

**Fix:** Use `python3 -c "import socket; print(socket.gethostbyname('{hostname}'))"` inside the FRR super-spine container. `python3` is available. Uses the container's inherited resolver (UTM gateway `192.168.64.1`), which works correctly.

```python
cmd = f"python3 -c \"import socket; print(socket.gethostbyname('{dns_host}'))\""
r = docker_exec(super_spine, cmd)
```

DNS failure is now a real error (not ⚠), with note `(container resolver used for check)` in the header.

---

### containerd insecure registry — multi-iteration debugging

**Background:** `nico-api-migrate` pod failed with `http: server gave HTTP response to HTTPS client`. Hosts.toml was present. `ndev registry verify` was falsely showing ✓.

**Root cause:** containerd v2 writes `config_path = ''` (empty string) in its default `config.toml`. When empty, containerd ignores `certs.d` entirely and falls back to HTTPS.

**Debugging sequence:**

1. **Wrong plugin name:** `sed` targeted `io.containerd.grpc.v1.cri` (containerd v1 plugin name). containerd v2.3.3 uses `io.containerd.cri.v1.images`. Fix had no effect.

2. **config.d drop-in:** Tried writing a drop-in to `/etc/containerd/config.d/`. containerd v2.3.3 was NOT picking it up (unclear why). Still failing.

3. **Line-number sed:** User's earlier working approach, fragile — IP-baked into line 31, changes if config format changes.

4. **Context-aware sed (final fix):**
   ```bash
   sudo sed -i "/\[plugins\.'io\.containerd\.cri\.v1\.images'\.registry\]/{n;s|config_path = ''|config_path = '/etc/containerd/certs.d'|}" \
       /etc/containerd/config.toml
   ```
   Finds the exact plugin section header, then modifies only the next line. Portable across future config changes.

**ndev registry verify false ✓ — four bugs fixed:**

| Bug | Root cause | Fix |
|---|---|---|
| 1 | Only checked `hosts.toml` existence, not `config_path` | Added `config_path` value extraction |
| 2 | `'config_path' in content` matched even when value was `''` | Used regex to extract actual value |
| 3 | Regex used `"..."` only; containerd v2 uses `'...'` | Changed to `["']([^"']*)["']` |
| 4 | `plugin_config_path = '/etc/nri/conf.d'` (line 152) matched regex | Negative lookbehind `(?<!\w)config_path` |

**Key insight:** `ctr` bypasses the CRI plugin and does NOT respect `certs.d`. Always use `sudo crictl pull` to test k8s-facing pull behaviour.

**Test sequence:** Remove drop-in → `ndev` shows ✗ → apply context-aware sed → `ndev` shows ✓ → `crictl pull` succeeds.

---

### ndev registry verify — new command

**Added:** `ndev <site> registry verify` — lists images/tags in the Mac registry, and (when run on the VM) verifies containerd insecure registry configuration.

**New file:** `collectors/registry.py`
- `_is_on_vm()`: detects VM by checking `/etc/kubernetes/admin.conf` exists
- `_containerd_configured(registry)`: checks `hosts.toml` + `config_path` value (with negative lookbehind)
- `collect(site_data)`: catalog + tags via HTTP API, VM-only containerd check

**Output when on VM:**
```
  ✓ 192.168.64.1:5000  reachable
  ✓ containerd  insecure registry configured

  Images:
    carbide-build     aarch64
    carbide-runtime   aarch64
    nico              v2.0.0
```

**When on Mac:** containerd line not shown (n/a for Mac).

**Usage:** `ndev <site> cluster info` aliased from k8s/k3s (both still work).

---

### k3s context renamed to cluster

**Change:** `ndev <site> k3s ...` context renamed to `cluster` for clarity (k3s implies k3s, but it's actually kubeadm now).

**Aliases still work:** `k3s`, `k8s`, `node` all resolve to `cluster`.

**New usage:** `ndev <site> cluster info`

---

### bake-golden-image.sh — disk cleanup step

**Problem:** Previous golden image builds produced 10GB images (debug symbols + Docker build cache).

**Fix — added Step 5 (before pod verification):**
```bash
docker builder prune -af --filter until=0s 2>/dev/null || true
docker image prune -f 2>/dev/null || true
apt-get clean
rm -rf /var/lib/apt/lists/*
journalctl --vacuum-size=10M 2>/dev/null || true
> /root/.bash_history
> /home/nico/.bash_history 2>/dev/null || true
DISK_USED=$(df -BG / | awk 'NR==2 {print $3}')
echo "  Disk used after cleanup: ${DISK_USED}"
```

Result: image dropped from ~10GB to ~500MB.

Also: `CARGO_PROFILE_RELEASE_DEBUG=false` in `Dockerfile.nico-dev` is the primary reason for the original size. Verified this env var overrides `debug = "line-tables-only"` in the workspace `Cargo.toml`.

---

### create-golden-image.py — cloud-init timeout fix

**Problem:** cloud-init timed out (1200s = 20min) while installing k8s packages from `pkgs.k8s.io` through NVIDIA network. Happened twice.

**Fix:**
- Timeout: 1200s → 3600s (60 min)
- Progress: dots replaced with per-60s status showing last line of `/var/log/cloud-init-output.log`
- On timeout/error: tail of cloud-init log is printed for diagnosis

---

### how-to.md — comprehensive rewrite

**Rewrote** `nico-dev/how-to.md` from scratch. Previous doc was outdated (DHCP IP patching, wrong paths, missing steps).

**New structure (13 sections):**
1. Prerequisites (Mac)
2. Create VM in UTM
3. Configure static IP via netplan
4. Prepare VM (`prepare-vm.sh init`)
5. Create site (`create-dev-site.py`)
6. Deploy fabric (`deploy-dev-fabric.py`)
7. Deploy k8s cluster (`deploy-dev-cp.py`)
8. Build Nico images on Mac (`build-dev-nico-mac.py`)
9. Verify registry (`ndev registry verify`)
10. Deploy Nico (`deploy-dev-nico.py`)
11. Mac setup (KUBECONFIG, route to VIPs)
12. Build and configure CLIs
13. Golden image: create and distribute
14. Golden image: new user setup (`first-boot.sh`)
15. Day-to-day dev workflow
16. ndev quick reference
17. Script reference table
18. Troubleshooting

**Key corrections from old doc:**
- `deploy-dev-nico.py` runs on VM (not Mac)
- Paths: `~/projects/` on Mac, `~/mac/` on VM
- `ndev` command syntax: `ndev <site> cluster info` (not `ndev.py status`)
- `ndev registry verify` gate before deploying Nico
- Static IP setup with why it matters

---

### nico-api-migrate failing again — database doesn't exist

**Symptom (2026-08-18):**
```
Error: error returned from database: database "nico_system_nico" does not exist at line 948
```

**Context:** This issue was previously fixed (2026-08-15/16) by setting `synchronousMode: false` for single-instance postgres. It appeared again in a fresh deploy.

**Investigation in progress:** Checking postgres cluster state and actual databases:

```bash
# Find postgres pod and its namespace
kubectl get pods -A | grep pg

# List databases (note: postgres cluster is in 'postgres' namespace, NOT 'nico-system')
kubectl exec -n postgres nico-pg-cluster-0 -- psql -U postgres -l

# Check CloudNativePG cluster bootstrap config
kubectl get cluster -n postgres -o yaml | grep -A 20 bootstrap
```

**Note:** The postgres pod namespace is `postgres`, not `nico-system`. Commands using `-n nico-system` will fail with "not found". Check `kubectl get pods -A | grep pg` to confirm namespace.

**RESOLVED (same day):**

**Root cause:** The 2026-08-15/16 `helm-prereqs` synchronousMode templating fix was never
committed to the nico repo. The `add-uefi-fix` branch later merged `main` (`19c35060b`),
which reverted `helm-prereqs/templates/postgresql.yaml` to hardcoded
`synchronous_mode: true` + `synchronous_mode_strict: true`. With 1 postgres instance,
Patroni strict sync blocked ALL writes — including the operator's own `CREATE DATABASE`.
The site yaml and rendered values (`synchronousMode: false`) were correct the whole time;
the template just ignored them.

**Diagnosis trail:**
1. `grep synchronous helm-prereqs/templates/postgresql.yaml` → hardcoded `true` (fix gone)
2. Live CRD: `kubectl get postgresql -n postgres -o yaml` → `synchronous_mode: true`
3. `psql -l` → no `nico_system_nico`; operator log showed `creating database` hanging

**Fixes applied:**
1. Re-applied the template fix (`{{ .Values.postgresql.synchronousMode }}`, no `| default`
   — Go templates treat explicit `false` as empty) + `synchronousMode: true` default in
   `values.yaml`. This time with an explanatory comment in the template.
2. Live unblock without full redeploy:
   ```bash
   kubectl patch postgresql nico-pg-cluster -n postgres --type merge \
     -p '{"spec":{"patroni":{"synchronous_mode":false,"synchronous_mode_strict":false}}}'
   # CRD patch alone did NOT update Patroni dynamic config — patch Patroni directly:
   kubectl exec -n postgres nico-pg-cluster-0 -c postgres -- \
     curl -s -XPATCH -d '{"synchronous_mode": false, "synchronous_mode_strict": false}' \
     localhost:8008/config
   ```
   The moment writes unblocked, the operator's hung `CREATE DATABASE` completed on its own.
3. `deploy-dev-nico.py <site> --skip-to nico` → migrate Completed, all nico pods Running.

**Key learnings:**
- The operator's CRD→Patroni config propagation didn't happen on patch; patching Patroni's
  API directly (`localhost:8008/config`) applies immediately.
- Check Patroni live config with `curl localhost:8008/config` inside the postgres pod —
  the CRD can say one thing and Patroni another.
- **The helm-prereqs fix MUST be committed to the nico repo** — this is the second time
  an uncommitted working-tree fix regressed via a merge. (Note: deploy scripts run fine
  from the Mac — kubectl/helm reach the VM cluster via the site kubeconfig.)
