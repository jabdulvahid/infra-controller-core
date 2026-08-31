# nico-sim — Technical Conversations

Captured Q&A discussions from development sessions. Dated and indexed for reference.

---

## 2026-08-13

### VRF — What is it?

**Q:** Typically a router has only one routing table; but VRF allows it to have multiple tables. Is this a correct assumption?

**A:** Yes, exactly right. A VRF (Virtual Routing and Forwarding) is essentially a separate, isolated routing table within the same router/device.

**Without VRF:** One routing table. A packet arrives, kernel looks up the destination, forwards it. All interfaces and routes share the same table.

**With VRF:** Multiple routing tables, one per VRF. A packet arrives on an interface that "belongs to" a VRF, kernel looks up the destination only in that VRF's routing table, not in others.

**Key properties:**
- Interfaces are assigned to a VRF — traffic entering that interface is looked up only in that VRF's table
- Routes in VRF A are invisible to VRF B by default
- Route leaking is the explicit, controlled act of copying a route from one VRF's table into another
- VRFs are the foundation of network slicing/isolation — multiple tenants can use overlapping IP ranges because each lives in its own VRF

In our context:
- `vpc_60000` VRF = site-controller's dedicated routing table for CP/SMN traffic
- `default` VRF = the regular routing table
- Route leak between them = explicitly sharing the BMC routes (RT 900) from the EVPN VRF into the default table so Nico-api can reach them

Think of VRFs as separate routing namespaces — same concept as Linux network namespaces but implemented inside a single FRR/NVUE process.

---

### VRF — How does kernel pick which VRF? Can an interface be in multiple VRFs?

**Q:** How does the kernel decide which VRF to use? The answer is — the association is based on the interface through which the packet comes in. Can an interface be part of multiple VRFs? Which command will show me this association?

**A:** Correct — the kernel selects the VRF based on which interface the packet arrives on.

**Can an interface be in multiple VRFs?** No — an interface belongs to exactly **one** VRF at a time (or the default VRF if not assigned). This is a hard constraint. That's why route leaking exists — to share routes across VRF boundaries without moving the interface.

**Commands to show the association:**

Linux kernel:
```bash
ip vrf show                        # list all VRFs and their member interfaces
ip link show eth0                  # look for 'master <vrf>' field
ip link show | grep -E "^[0-9]|master"  # all interfaces with VRF membership
```

FRR (vtysh):
```bash
show vrf                           # list all VRFs FRR knows about
show bgp vrf all summary           # BGP summary across all VRFs
show ip route vrf vpc_60000        # routing table for a specific VRF
```

Mental model summary:
- Packet arrives on `eth3` (assigned to VRF `bmc`) → kernel uses `bmc` routing table only
- Packet arrives on `eth0` (default VRF) → kernel uses the default routing table
- Route leak = kernel copies a route entry from one table into another; interfaces stay where they are

---

### Managed Host OOB routing — underlay vs overlay

**Q:** The rack-mat-hosts prefix — is this underlay or overlay? If underlay we need to pre-wire routes; if overlay somebody needs to announce them.

**A:** Your reasoning is correct.

`rack-mat-hosts` is defined as `type=underlay` in Nico's siteConfig TOML — it is a physical infrastructure network (BMC discovery, OOB management). In production, the rack leaf switch has the underlay network on an interface, creating a connected route that FRR advertises via BGP.

In the sim, `leaf-mat:eth3` connects to `br-dc1-internet` with IP `9.130.0.10/31` (from the dpu_fabric pool). It has **no** IP in the `9.140.2.0/24` range, so FRR can't advertise the prefix — the fabric has no route for it. That's why Nico-api times out connecting to MAT BMC mocks.

**Fix (underlay_local mode):** Add the gateway IP (`9.140.2.1/24`) to `leaf-mat:eth3`. FRR gets a connected route and advertises `9.140.2.0/24` into BGP. The rest of the fabric learns it.

**Production path (overlay_evpn):** BMC prefixes are exported into EVPN as type-5 routes with RT `{datacenter_asn}:900`. DPU stand-ins import them into `vpc_{ControlPlaneVNI}` VRF and leak to default VRF. This is the SMN (Site Management Network) architecture — see `startupSMN.template`.

---

### BGP NoNeg — FRR collision detection display artifact

**Q:** `nsim bgp info --detail` incorrectly shows CP→leaf BGP as not established. Can we fix this?

**A:** Yes. When two BGP routers both initiate TCP connections simultaneously (which happens when both are configured to peer with each other), FRR's **collision detection** (RFC 4271 §6.8) closes the "losing" connection. The router with the higher BGP router-id keeps its incoming connection.

In our sim: DPU stand-in (loopback 9.131.0.x) has a higher router-id than the leaf (loopback 9.129.0.x), so the DPU's outgoing connection wins. Data flows via the DPU's accepted connection. But the leaf's **own outgoing attempt** stays in `NoNeg` state in `show bgp summary` — a known FRR display quirk.

The data plane is fully functional (prefixes exchanged, VIPs propagate, Nico-api is reachable). `NoNeg` with non-zero uptime = collision-resolved session, not a real failure.

Fix in nsim: treat `NoNeg` + non-zero uptime as quasi-established (⚠ warning, not ✗ failure). Updated `bgp.py` and `verify_fabric.py` collectors + `table.py` renderer.

---

### machine-id duplication — DHCP Client ID collision in DPU VMs

**Q:** Why don't DPU-1 and DPU-3 show in `virsh net-dhcp-leases ytl-cp`? All three DPU VMs show `b6d0256ed1f04a128f0e424126c65763` as machine-id.

**A:** Root cause: all DPU stand-in VMs clone from the same `noble-server-cloudimg-amd64.img` backing file which has a **baked-in `/etc/machine-id`**. `systemd-networkd` derives the DHCP Client ID from `machine-id` — so all DPU VMs appear as the **same DHCP client** to dnsmasq. When DPU-3 renews its lease, it overwrites DPU-1's entry in the lease file (same Client ID).

**Why CP host VMs don't have this problem:** The golden image build process (`create-golden-image.py`) ran cloud-init during the build, which regenerated `machine-id`. When CP host VMs boot from the golden image with a fresh seed ISO, cloud-init runs again and they get unique IDs.

**Fix:** Add to DPU stand-in `bootcmd` in `gen_dpu_user_data`:
```yaml
bootcmd:
  - [cloud-init-per, once, machine-id-regen, bash, -c,
    "truncate -s0 /etc/machine-id && systemd-machine-id-setup"]
```

**Why just truncating isn't enough:** `systemd-networkd` caches the DUID separately. After regenerating `machine-id`, also need to clear the DUID cache and force DHCP renewal:
```bash
sudo truncate -s0 /etc/machine-id && sudo systemd-machine-id-setup
sudo networkctl renew eth0
```

---

### Static DHCP entries not appearing in `virsh net-dhcp-leases`

**Q:** We added static DHCP host entries (`<host mac="..." ip="..."/>`) to the libvirt network XML. But `virsh net-dhcp-leases` doesn't show all VMs. Why?

**A:** `virsh net-dhcp-leases` reads dnsmasq's **lease file** (e.g. `/var/lib/libvirt/dnsmasq/ytl-cp.leases`). For **static** DHCP entries (`dhcp-host` in dnsmasq config), dnsmasq hands out the IP but may **not write it to the lease file** — static assignments are sometimes handled without a lease file entry. Dynamic leases always appear; static ones may not.

Result: the VM has the correct static IP (confirmed by ARP/SSH) but `virsh net-dhcp-leases` shows nothing for it.

Fix in `verify_vms.py`: fall back to the static IP from `control_plane.vms[].dpu_oob_ip` when the lease lookup returns nothing but the VM is running.

---

### deploy-nodes.sh veth destruction — why eth3 went down on leaf switches

**Q:** Why did leaf-1/2/3 eth3 interfaces go down after VMs were deployed, breaking DPU-to-leaf BGP?

**A:** Root cause: `deploy.sh` (fabric deploy) has a veth flush step that runs before `clab deploy`. If `deploy.sh` was re-run after VMs were already deployed, the flush deleted the ContainerLab veth pairs (`eth-cp1/2/3`) connecting the CP leaves to the CP bridges. When a veth is deleted, **both ends vanish** — the leaf containers lost their `eth3` interfaces.

The VM `vnet` interfaces on the bridges survived because they're not in the flush list. But ContainerLab's veths were gone, so `leaf-1/2/3:eth3` simply disappeared.

Fix: the deploy scripts now **abort if resources already exist** instead of silently destroying them. Use `--force` to override, or run `destroy-site.py` to clean up first.

---

### Why `virsh net-dhcp-leases` showed wrong hostname for DPU VMs

**Q:** `virsh net-dhcp-leases ytl-cp | grep dpu` showed `dc1-ytl-cp-dpu-3` at `192.168.220.18` but `hostname` inside showed `dc1-ytl-cp-dpu-2`. Why?

**A:** The DHCP lease table stores the hostname **as reported by the DHCP client at the time the lease was issued**. If a VM's hostname changes (e.g. cloud-init sets it after the first DHCP request), or if IP addresses floated before static DHCP entries were added, the lease table entry can have a stale hostname.

dnsmasq associates a lease with the Client ID (DUID), not the hostname. With all DPU VMs sharing the same `machine-id` (see above), they shared the same Client ID — so the hostname in the lease table could belong to a different VM than the one currently holding that Client ID.

Fix: static `machine-id` per VM (bootcmd fix above) + static OOB IP entries in `control_plane.vms`.

---

### Lease time not 7 days — `<lease expiry="7" unit="days"/>` not applied

**Q:** `virsh net-dumpxml ytl-cp | grep -i lease` returns empty. DPU VM leases expire in ~1 hour.

**A:** The `<lease expiry="7" unit="days"/>` element in `oob-network.xml` is either not supported by the libvirt version on jaslinux, or was silently ignored when the network was defined. The running dnsmasq uses its default lease time (1 hour).

The 1-hour lease causes DPU entries to periodically disappear from `virsh net-dhcp-leases` between renewals, even when the VM is running normally and holds the correct IP. Not a functional issue — the static `dhcp-host` entries guarantee the same IP on every renewal.

---

### FRR NoNeg BGP state — what it means

**Q:** `nsim bgp info --detail` shows `NoNeg` for leaf→DPU BGP sessions but DPU shows the session as Established with 8+ hours uptime. How?

**A:** FRR's `show bgp summary` displays each **configured peer's outgoing connection attempt** state. When collision detection resolves in favor of the DPU's incoming connection, the leaf's own **outgoing TCP attempt** is marked `NoNeg` (no BGP negotiation completed on that connection). The actual data flows through the **accepted** incoming connection from the DPU, which is established.

`NoNeg` with non-zero uptime is the FRR way of showing "collision-resolved session — the other side's connection won." The prefix counts in `show bgp summary` reflect the DPU's accepted connection, not the leaf's own outgoing attempt. This is a known FRR display quirk, not a real failure.

---

## 2026-08-12

### Gateway bug — `gateway_within_network` DB constraint

**Q:** nico-api is crash-looping with `gateway_within_network` constraint violation.

**A:** The ytl.yaml had `gateway: "7.140.x.1"` (dc-sim octets) but prefix `9.140.x.0/24` (ytl underlay octet). Nico's DB constraint requires the gateway to be within the network prefix. Fix: sed-replace the gateway IPs in ytl.yaml to `9.140.x.1`. Also: `generate-sim-values.py` always regenerates values from the site yaml on every deploy (no `--regen-values` flag needed) — this was the root cause of the silent stale-values bug.

---

### MetalLB BGPPeer nodeSelector mismatch

**Q:** MetalLB FRR shows "BGP instance not found" — VIP routes never propagate to sim-host.

**A:** MetalLB BGPPeer `nodeSelectors` used short names (`cp-1`, `cp-2`, `cp-3`) but actual k8s node hostnames are `dc1-ytl-cp-1` etc. No nodes matched → MetalLB never configured FRR → VIP routes never advertised → sim-host had no route to 9.133.1.x.

Fix in `generate-sim-values.py`: derive `name_pfx` from `dc_name` + `sitename` and apply to BGPPeer nodeSelectors.

---

### UEFI credential `already exists` on fresh deploy

**Q:** `credential add-uefi` fails with "not support to reset DPU UEFI credential" on a fresh cluster.

**A:** `nico-prereqs` seeds an **empty** UEFI credential in Vault (`machines/all_dpus/site_default/uefi-metadata-items/auth`) at deploy time. The `add-uefi` guard checks `is_some()` (entry exists) rather than `!password.is_empty()` (entry has content), so it treats the empty seed as a real credential and refuses to set a new one.

**Workaround:**
```bash
kubectl exec -n vault vault-0 -- vault kv delete -mount=secrets machines/all_dpus/site_default/uefi-metadata-items/auth
kubectl exec -n vault vault-0 -- vault kv delete -mount=secrets machines/all_hosts/site_default/uefi-metadata-items/auth
```

**Fix:** `crates/api-core/src/handlers/credential.rs` — change guard from `is_some()` to checking `!password.is_empty()` for `UsernamePassword` credentials. PR opened (branch `add-uefi-fix`).

---

### service_vips route missing from sim-host routing table

**Q:** `9.133.1.0/27` (MetalLB VIP pool) not in sim-host routing table after `deploy-fabric.py`.

**A:** `generate-fabric.py` was reading `service_vips` from `fabric.prefixes` but the value lives in `nico-system.helm-values.net-plan.service_vips`. `cfg.service_vips` was always `None`, so the route was never added to the sim-host.

Fix: also check `nico-system.helm-values.net-plan` for `service_vips` in `_load_cfg`.

---

### MAT BMC mocks unreachable from Nico — two-part fix

**Q:** Nico's site-explorer gets "connection refused" to MAT BMC mock addresses (9.140.2.x:443). From jaslinux (sim-host) the mocks are reachable; from CP VMs they are not.

**Root cause 1 — no route in fabric:**
`leaf-mat`'s FRR had `network 9.140.2.0/24` in its BGP config but no connected route for that prefix (no gateway IP on eth3). FRR only advertises prefixes that exist in the kernel routing table. Without it, no fabric node knew how to reach the MAT BMC subnet.

Fix: add `oob_routing_mode: underlay_local` under `managed_hosts` in site yaml. `generate-fabric.py` now adds a ContainerLab exec cmd to set the gateway IP on leaf-mat:eth3 at deploy time, giving FRR a connected route.

**Root cause 2 — iptables blocking port 443:**
The SYN packets from CP VMs reached `br-dc1-internet` (confirmed by tcpdump) but MAT never responded. Docker adds a DROP rule at the end of the iptables INPUT chain. Port 53 (DNS) was already opened; port 443 was not.

Fix: add to `deploy.sh` generation in `generate-fabric.py`:
```bash
iptables -C INPUT -i br-dc1-internet -p tcp --dport 443 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT -i br-dc1-internet -p tcp --dport 443 -j ACCEPT
```

---

### allow_insecure_discovery — three wrong guesses, one TOML scoping bug

**Q:** MAT DPU discover_machine fails with PermissionDenied "selected interface and discovery source IP do not belong to the same host". There's a `allow_insecure_discovery` flag in nico-api. Why doesn't setting it help?

**Wrong guess 1 — wrong section:** Added under `[machines]` section in TOML. But the field is top-level in `CarbideConfig`, confirmed by test cases in `crates/api-core/src/cfg/file.rs` which use bare `allow_insecure_discovery = true` with no section header.

**Wrong guess 2 — wrong config key:** Patched `nico-api-config.toml` key in the ConfigMap but the pod loads from `carbide-api-config.toml` (legacy path). Need to patch both keys.

**Wrong guess 3 — kubectl apply silently fails:** Used `kubectl apply -f -` with the full ConfigMap JSON (including `resourceVersion`, `managedFields`) which fails or merges incorrectly. The ConfigMap showed count=1 but the pod file showed 0. Fix: use `kubectl patch --type=merge` instead.

**Real bug — TOML scoping:**
The config file ends with:
```toml
[rms]
api_url = "..."
enforce_tls = true

allow_insecure_discovery = true   ← WRONG: belongs to [rms], not root!
```
In TOML, once a `[section]` header appears, ALL subsequent key-value pairs belong to that section until the next header. The setting must appear **before any section headers** to be a root-level field.

Fix: insert `allow_insecure_discovery = true` before the first `[...]` line in the config file.

**What the flag actually does (from source):** When `true`, skips the IP-ownership check in `discover_machine`. Instead of validating that the caller's remote IP belongs to the machine interface, it trusts the `machine_interface_id` provided in the `MachineDiscoveryInfo` payload. MAT always sends this field.

**Lesson:** Always verify TOML key placement by checking how the Rust struct is defined (not just the README table) and by testing that the value is actually in scope by looking at what section it follows in the file.

---

### Managed host OOB routing — underlay vs overlay design decision

**Q:** Is rack-mat-hosts an underlay or overlay network? Why does Nico need a route to the MAT BMC IPs?

**A:** `rack-mat-hosts` is defined as `type=underlay` in Nico's siteConfig TOML — it's a physical infrastructure network (BMC discovery, OOB management). In production, the rack leaf switch has a connected route for the underlay prefix and advertises it into BGP so the fabric routes to it.

**Production architecture (overlay_evpn):** BMC underlay prefixes ARE exported into EVPN as type-5 routes with RT `{DatacenterASN}:900` (`FnnCommonManagedNodeBmcRouteTarget`). DPU stand-ins import this via `vpc_{ControlPlaneVNI}` VRF (from startupSMN.template) and leak to the default VRF. Nico-api then reaches BMCs via the overlay. This is the SMN (Site Management Network) architecture.

**Sim implementation:** Added `oob_routing_mode: underlay_local | overlay_evpn` config under `managed_hosts`. `underlay_local` (implemented) adds the gateway IP to leaf-mat at deploy time. `overlay_evpn` (deferred) requires VRF + VXLAN config on leaf-mat and DPU stand-ins — see context-2026-08-13.md for the full FRR design.

---

### Full end-to-end MAT validation — 2026-08-13

Both simulated managed hosts reached `Ready` state. Full pipeline validated:

```
create-new-site → deploy-fabric → deploy-nodes → form-k8s-cluster → deploy-nico → MAT → Ready ✓
```

Key states the MAT state machine went through:
1. `DPUInitializing/INIT` — initial discovery
2. `DPUInitializing/WAITINGFORPLATFORMPOWERCYCLE` — Nico-driven power cycle
3. `HostInitializing/WaitingForLockdown` — BMC lockdown sequence
4. `Ready` — fully provisioned

---

### config-size param and site file consolidation

**Q:** `create-new-site.py` previously created two files (`{site}.yaml` and `{site}-mac.yaml`). Want a single mandatory param to select the profile.

**A:** Added `--config-size large|small` (required) to `create-new-site.py`. `large` uses `nico-sim.yaml` (full jaslinux sim — 3 CP nodes, MH VMs, full RAM). `small` uses `nico-sim-mac.yaml` (Mac/low-resource — 1 CP node, no MH VMs, halved sizing). Both output a single `{site_nick}.yaml`. The `-mac` suffix is gone.

---

### sim-values moved to site folder

**Q:** sim-values were written to `{infra_controller_repo}/nico-sim/{dc_name}/sim-values/` — inside the nico code repo. Doesn't belong there; breaks for root users (different home dir).

**A:** Changed default output to `{site_folder}/sim-values/`. Site folder now contains everything:

```
sites/ytl/
  ytl.yaml              ← site config
  ytl.kubeconfig.yaml   ← k8s access
  sim-values/           ← generated helm values
  fabric/               ← ContainerLab topology
  vm/                   ← VM cloud-init + deploy scripts
  certs/                ← TLS certs
  mat/                  ← MAT config
```

Three-file change: `generate-sim-values.py` default outdir, `deploy-nico-system.py` `values_dir` in `resolve_paths()`, `destroy-site.py` adds `sim-values/` to cleanup.

---

### dev config-size — Mac without nested virtualization

**Q:** Colleagues without M4 MacBooks can't run nested KVM. Can we support a sim that doesn't need nested VMs?

**A:** Yes. Proposed `--config-size dev` mode:

| | small | dev |
|---|---|---|
| CP node | Separate KVM VM | Linux VM itself (kubeadm on localhost) |
| DPU stand-in | KVM VM running FRR | ContainerLab FRR container |
| Requires nested virt | Yes | No |
| Extra RAM for CP+DPU | ~5GB | ~0 (already running) |

**Design decision:** Use kubeadm directly on the Linux VM (not k3s — nothing wrong with kubeadm, it's already what we use). DPU stand-ins become ContainerLab FRR containers wired directly to CP leafs — no bridges, no KVM.

**Script isolation:** `deploy-dev.py` is a new separate script. Existing scripts (`deploy-fabric.py`, `deploy-nodes.py`, `deploy-nico-system.py`) are UNTOUCHED to prevent regression. dev gets its own path.

**Status:** Deferred. User will test `small` on their M4 Mac first (supports nested virt), then implement `dev`.

---

## 2026-08-18

### create-golden-image.py — colleague's build timed out silently (passphrase-protected SSH key)

**Symptom:** On a colleague's machine, `create-golden-image.py` consistently burned the
full cloud-init timeout and died with no diagnosis:

```
Step 5: Network
  Waiting for builder VM DHCP lease.. → 192.168.199.36 (10s)
Step 6: Package installation (cloud-init)
  [60s] ...
  [120s] ...
  ...
RuntimeError: cloud-init timed out after 1200s
  Last log lines:
(unavailable)
```

On the author's machine the same script worked first try.

**Diagnosis trail:**
- DHCP lease arrived in 10s → VM booted fine with working network
- Every 60s progress line showed `...` and the final log dump `(unavailable)` →
  **not one SSH probe into the builder VM ever succeeded** — the problem was
  host→VM SSH, not cloud-init (which may well have completed unobserved)
- The script SSHes with `BatchMode=yes` (no prompts) using a private key
  auto-discovered from `~/.ssh/`. A **passphrase-protected key fails instantly
  and silently** under BatchMode — every probe, for the entire timeout
- "Works for me, fails for them": author's key had no passphrase; colleague's did

**Root cause confirmed:** colleague's `~/.ssh/id_ed25519` is passphrase-protected.

**Script hardening applied (commit 499ed87):**
1. Passphrase-protected keys rejected at startup (`ssh-keygen -y -P ''` probe)
   with the remedy printed — the failure is now a 2-second error, not a silent hour
2. Injected public key is always derived from the private key (`ssh-keygen -y`),
   eliminating stale-`.pub`/private-key mismatches; `--ssh-key` accepts either half
3. Fail-fast: if no SSH probe succeeds within 180s, abort with host-side diagnosis
   (manual ssh command to reproduce, virsh console hint)
4. Builder VM is kept on failure for inspection (cleanup used to destroy the only
   evidence); next run cleans it up automatically
5. New `--work-dir` for builder disk/seed placement + free-space warning
   (builder disk can grow to 40G; `/tmp` is a small tmpfs on some distros)
6. More logging throughout (key path, image sizes, disk/seed paths, VM specs,
   console hint, polling scheme, `(ssh unreachable)` progress markers)

**Recommended fix for users hitting the passphrase error:** create a dedicated
unencrypted key rather than stripping the passphrase from a personal key — the key
only accesses a throwaway builder VM on a NAT network:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_nico_sim
sudo python3 create-golden-image.py nico-sim.yaml --ssh-key ~/.ssh/id_nico_sim
```

**Also observed:** a fresh build on a fast box (NVMe + fast internal mirror)
completed cloud-init in 31s and the whole build in under 5 minutes — suspiciously
fast, but legitimate: the exported image was 1262 MB vs the 615 MB base
(~650 MB of packages actually installed). Note `cloud-init status` can report
`done` even when modules failed, so a fast box and a fast failure look identical
from outside. Verify an image without booting it:

```bash
sudo virt-ls -a /var/lib/libvirt/images/<image>.qcow2 /usr/bin/ | grep -E '^(kubeadm|kubelet|kubectl|containerd)'
```

**Implemented (same day):** the script now verifies expected binaries via SSH after
cloud-init completes and before export — new Step 7 "Verify image contents"
(cp: kubeadm/kubelet/kubectl/containerd + version printout; mh: minimal toolset).
Missing binaries fail the build loudly with the cloud-init log tail, and the
builder VM is kept for inspection. The done-with-errors ambiguity is now a hard
pass/fail; the manual `virt-ls` check above remains useful for auditing
already-exported images.
