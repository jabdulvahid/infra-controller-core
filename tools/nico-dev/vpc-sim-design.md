# VPC Datapath Simulation — Design Sketch (future phase)

Proposed 2026-08-22. Goal: make VPC creation **physically real** in the
simulated datacenter — tenant creates a VPC, attaches machines, and packets
actually flow (or are actually isolated) through the FRR fabric via
EVPN/VXLAN, exactly as in an NVIDIA-recommended datacenter. With this,
nico-dev/nico-sim becomes a complete learning vehicle: every abstraction in
the GUI has inspectable packets, routes, and FRR state behind it.

Status: **design only — not scheduled.** Enters the POR as a gated future
phase after the golden-image path (Phases C/D) completes.

---

## 1. The starting state (corrected model — verified in the live DB)

nico auto-creates an **admin VPC** at initialization, owned by the internal
organization `carbide_internal`:

```
vpcs: 1ae2d0e8-… | admin | carbide_internal
```

Every READY machine's admin interface (`<ov>.135.0.x`) belongs to it. This
is the "before" picture: all machines in one default VPC, mutually visible.
Two facts about it that shaped this design:

- **Org scoping splits the views.** The admin GUI (operator view on the
  core DB) shows the admin VPC and everything else; `nicocli vpc list` as a
  tenant org (e.g. `ncx`) correctly shows nothing until that org creates
  VPCs. GUI answers "what exists"; REST answers "what does my org own."
- **The visibility is control-plane only.** MAT machines are actors in one
  process — no real interfaces, no packets. "All machines see each other in
  the admin VPC" is true in the database and false on the wire, because
  there is no wire.

The simulation's job: create the wire.

## 2. What already exists (the scaffolding is waiting)

| Asset | State today |
|---|---|
| EVPN address family | Live on every fabric BGP session — `Estab (0 pfx)`. An empty stage: first VNI advertisement lights up `ndev bgp info` |
| Overlay address space | Reserved per site: `<ov>.150.0.0/16` |
| VNI pools | In the site yaml / nico config: `vni 1024500–1024800`, `vpc_vni 60101–60999` — nico allocates from these on VPC create |
| `overlay_evpn` design | Deferred nico-sim work (VRF + VXLAN on leaf-mat + DPU stand-ins) — see nico-sim `context-2026-08-13.md` |
| VPC control plane | Fully working: tenant/VPC/segment create via gRPC (admin-cli), REST (nicocli — org-scoped), GUI; MAT can exercise it (`vpc_count`, `allocate_instance`) |
| Real DPU agent binary | `forge-dpu-agent` ships in the nico image (BlueField-coupled — see Stage 2) |
| Fabric as sandbox | Regenerated from site yaml on every deploy; breakage is free |

## 3. Architecture (staged by realism)

### Stage 1 — real datapath, small agent ("vpc-realizer")

New topology elements, generated per managed host from the site yaml:

```
                          fabric (existing)
                     leaf-cp ─── spine ─── leaf-mat
                        │
                 dpu-h1 (FRR, EVPN)        ← per-host DPU stand-in, BGP-peered
                        │                     like dpu-1 is today
                 br-h1 (bridge)
                        │
                 host-h1 (container/netns) ← the "managed host": plain Linux,
                                              one interface, gets tenant IPs
```

Per DPU stand-in, a small **vpc-realizer** agent (python or go, ~200 lines):

1. Polls nico's real `get_managed_host_network_config` gRPC — the same call
   the production DPU agent makes — using a vault-issued client cert (same
   pattern as MAT's).
2. Realizes the returned config with plain Linux + FRR:
   - VPC → VRF (`ip link add vrf-<vni> type vrf …`)
   - VNI → VXLAN device bound to the VRF, bridged to the host-side port
   - FRR: `advertise-all-vni` under the EVPN address family → type-2/type-3
     routes flow into the fabric
3. Calls `record_dpu_network_status` with the applied config version —
   closing the loop so nico sees the config as converged (the same
   version-ack protocol MAT fakes today).

Observable outcomes (the learning payoff):

- Tenant creates VPC + attaches machines h1, h2 → `ping` h1→h2 **works**,
  crossing two DPU stand-ins and the fabric via VXLAN.
- Machine h3 in a different VPC → ping **fails**, and you can see exactly
  why: different VRF, different VNI, no shared EVPN routes.
- `ndev bgp info` shows EVPN prefixes climbing from 0; `ndev fabric shell
  leaf-cp` → `show bgp l2vpn evpn` shows the type-2 MAC/IP routes per VNI;
  `tcpdump -i br-<dc>-cp 'udp port 4789'` shows the actual VXLAN
  encapsulation. Every GUI abstraction now has packets behind it.

### Stage 2 — the real `forge-dpu-agent` (feasibility spike)

Swap the realizer for the production agent inside the DPU stand-in.
Expected friction: BlueField coupling (OVS/HBN/DOCA assumptions, hardware
interface names). Time-boxed spike AFTER Stage 1 works — Stage 1 gives the
behavior; the real agent would add fidelity to the production code path.
If the coupling is deep, the realizer remains the sim's permanent agent and
the spike's findings document the delta.

### Relationship to MAT

MAT keeps its role: BMC/discovery/lifecycle simulation (control plane).
The VPC datapath lives beside it in separate stand-ins. Convergence
(MAT machines pointing at real stand-in interfaces) is possible later but
NOT required — trying to merge them up front couples two hard problems.

## 4. What must be built (sizing)

| Piece | Est. size |
|---|---|
| Topology generation: per-host DPU stand-in + host container + bridges + BGP peering (extends generate-dev-fabric) | medium |
| vpc-realizer agent (gRPC poll → VRF/VXLAN/FRR apply → status ack) | medium |
| Cert issuance for realizers (configure-clis pattern reuse) | small |
| ndev: `vpc` context (VNIs, VRFs, EVPN routes per stand-in) | small-medium |
| Security-group realization: NSG rules → nftables in the DPU stand-in (poll same config channel; enforcement point mirrors production DPU) | medium |
| Teaching material: networking-primer §EVPN extension + guided exercises | medium |
| VM sizing validation (each stand-in ≈ one FRR container + netns; target 4–8 hosts on 16 GB) | small |

Magnitude: comparable to REST-in-base. Not a weekend.

## 5. Open decisions

1. **Vehicle: nico-dev or nico-sim?** nico-dev (single VM, distributable
   golden image) is the natural learning vehicle; nico-sim (jaslinux,
   virsh) has more headroom for scale. Leaning nico-dev-first with the
   generator kept portable.
2. **Machine↔VPC cardinality**: start 1:1 (per the feature request);
   the config model shouldn't preclude 1:N later (instances with multiple
   prefixes).
3. **Realizer language**: python (matches nico-dev tooling, fast to
   iterate) vs go/rust (matches product). Leaning python for Stage 1.
4. **How machines get tenant IPs**: realize nico's DHCP path (DPU relay,
   realistic) vs static-assign from the VPC prefix (simple). Stage 1:
   static; DHCP realism later.
5. **Admin-VPC datapath too?** Making the *default* state physically real
   (all hosts reachable via the admin VPC before any tenant work) is the
   same machinery with VNI=admin — decide whether Stage 1 includes it or
   starts tenant-only.

## 6. Exit criteria (when scheduled)

The bar is a complete **end-to-end datacenter use case** — everything nico
can do, in one VM, with no DPU and no hardware — executed as a tenant would
and verified on the wire:

1. **Tenant onboarding**: org bootstrapped, tenant created (nicocli
   provider/tenant `current`), visible via REST and GUI.
2. **Network segments**: tenant network segments created and realized.
3. **Multiple VPCs (≥ 2)**: created via nicocli/GUI, each allocated its own
   VNI from the pool; machines attached 1:1 (instances).
4. **Intra-VPC connectivity VERIFIED on the wire**: two hosts in VPC-A ping
   each other through the fabric (VXLAN across the DPU stand-ins), packets
   visible in `tcpdump 'udp port 4789'`.
5. **Inter-VPC isolation VERIFIED**: a host in VPC-B provably cannot reach
   VPC-A — and the *reason* is inspectable (separate VRFs, disjoint EVPN
   routes in `show bgp l2vpn evpn`).
6. **Security groups enforced in the datapath**: create an NSG, apply a
   rule (e.g. deny ICMP within VPC-A) → the previously working ping now
   fails; permit it → works again. Realized as nftables rules in the DPU
   stand-in, mirroring enforcement at the DPU in production.
7. **Observability throughout**: `ndev bgp info` shows per-VNI EVPN
   prefixes; a new `ndev vpc` context maps VPC → VNI → VRF → routes;
   fabric verify gains an overlay check.
8. **The guided exercise**: a documented walkthrough — "a day in a
   simulated datacenter" — tenant → segments → VPCs → instances → security
   groups → connectivity proof, each step showing the GUI/REST view AND
   the packets/routes behind it. This is the learning deliverable: all of
   nico's tenant-facing surface, exercised end-to-end, without a DPU.

---

## 7. Refinements (2026-09-08)

Review of Stages 1–2 against the upstream code, and a scoping decision.

### 7.1 Scope ruling: the realizer, not the real agent

**Ruling:** HBN, NVUE and the production `forge-dpu-agent` are
implementation details of a BlueField. What the simulation must prove is:
*can the managed host's current network configuration be retrieved from the
API and applied to the simulated fabric?* Stage 1 (the realizer) is the
plan. Stage 2 (real agent in the stand-in) is dropped as a goal; it may be
revisited as a curiosity, never as a gate.

Findings from the code that informed the ruling (recorded so nobody re-does
the survey):

- `forge-dpu-agent` has a dev-only **fake-DPU mode** (`[machine]
  is_fake_dpu = true`): synthetic BlueField identity, no hardware
  enumeration, no client certificate required. Identity is hard-coded to
  one MAC/serial, so at most one such agent per site without an upstream
  change. It still requires HBN (via `crictl` or NVUE REST) to apply config.
- The agent supports **NVUE REST** (`--hbn-config-mode nvue-rest`,
  `NVUE_HTTPS_ADDRESS`/`NVUE_USERNAME`/`NVUE_PASSWORD`). The HBN version
  gate expects a build string prefixed `HBN`; interface names are
  BlueField's (`p0_if`, `p1_if`, `pf0hpf_if`, `pf0vfN_if`). Running it
  against Cumulus VX or the `doca_hbn` container on a generic VM would be
  a spike with real unknowns — the reason for the ruling above.
- **MAT already impersonates the agent's entire control-plane leg**:
  `discover_machine`, `dpu_info`, `get_managed_host_network_config`,
  `record_dpu_network_status`, `dpu_agent_network_observation`. The seam
  between MAT and a realizer is therefore known: the realizer consumes the
  same `ManagedHostNetworkConfigResponse` and realizes it; MAT remains the
  API actor.
- `bmc-mock` has a **libvirt mode** (power control + virtual media on a
  named domain). MAT does not use it today. It is the piece a future
  "real VM host" phase would build on; not needed for Stage 1.

### 7.2 The translation is specified by the agent's own renderer

The realizer re-implements what `crates/agent/src/ethernet_virtualization.rs`
+ `templates/nvue_startup_fnn.conf` do, targeting plain Linux + FRR instead
of NVUE. Field by field, per `ManagedHostNetworkConfigResponse`:

| API field | Realization in the stand-in |
|---|---|
| `managed_host_config.loopback_ip` (lo-ip pool) | `lo` address, VXLAN source, BGP router-id, underlay eBGP to the leaf (unchanged from the existing dpu-1 pattern) |
| `vpc_vni` / `use_admin_network` | one VRF `vpc_<vni>` with L3VNI `<vni>` (`ip link add vrf-… type vrf`, `vxlan… ` bound to it); admin VPC when `use_admin_network`, tenant VPC otherwise; **rebuild on change, never edit in place** (the agent re-renders the whole config every poll) |
| per-VPC loopback (`vpc-dpu-lo` pool) | loopback inside the VRF (EVPN next hop for that VRF's type-5 routes) |
| `tenant_interfaces[]` / `admin_interface`: `gateway`, `interface_prefix`, `ip`, `vlan_id` | host-facing veth in the VRF carrying the **gateway address with the segment mask** (not a /31 — that is the site-controller pattern); the host container's end takes `ip` |
| `datacenter_asn`, `asn` (fnn-asn pool) | FRR `router bgp <asn>`; RTs formed as `<datacenter_asn>:<n>` |
| routing profile `route_target_imports` / `route_targets_on_exports` + native `<dc_asn>:<vpc_vni>` + `additional_route_target_imports` | `route-target import/export` lists in the VRF's `l2vpn evpn` AF; `advertise ipv4 unicast` |
| `tenant_host_asn` + routing profile `allowed_anycast_prefixes` | **passive eBGP neighbour toward the host** in the VRF, inbound prefix-list permitting /32s inside the allowed anycast prefixes, community tag for the leak — required for the anycast exercise (7.4), not optional |
| `leak_*` flags | VRF↔default route-import with the community/prefix-gated route-map |
| `network_security_groups`, `deny_prefixes` | nftables in the stand-in — **deferred** behind connectivity/isolation/anycast |
| `dhcp_servers` | Stage 1: static host addresses from `ip`; later: DHCP relay from the stand-in toward nico-dhcp (realistic path) |

Fidelity risk: the realizer is a second implementation of a translation the
agent owns; it will lag agent changes. Mitigation: generate both renders
from one variable set and diff them structurally (the same render-diff idea
proposed for the site-controller template). One rendering library, two
consumers.

### 7.3 Host unit: a container, not a bare namespace, not a VM

A user must be able to ssh into a host, ping, tcpdump, and *install a
service*. A bare `ip netns` cannot host that; a VM per host is too heavy
(GBs vs tens of MB; eight hosts must fit on the 16 GB tier beside the site).
**Unit = lightweight Linux container per host** (same family as the fabric's
FRR containers): sshd, iproute2, ping, tcpdump, curl, and FRR available for
7.4. `ndev host shell <h>` enters from the VM side; ssh between hosts inside
a VPC proves connectivity from the tenant's point of view.

A VM host is the right unit only when the exercise is the OS lifecycle
itself (PXE from nico-pxe, scout inventory, real Ubuntu install, reboots).
That is a later phase built on `bmc-mock`'s libvirt mode.

### 7.4 Fourth exercise: anycast VIP without a cluster

nico supports tenant anycast natively: a routing profile's
`allowed_anycast_prefixes` lets a host announce /32s inside those prefixes
to its DPU over BGP; the DPU tags and leaks/exports them. In the sim:

1. Two hosts in a *provider* VPC each run FRR with `network <vip>/32`
   toward their stand-in (the passive tenant neighbour of 7.2).
2. Each stand-in installs the /32 with the host as next hop; two hosts →
   two next hops → ECMP at the importer.
3. A *consumer* VPC reaches the VIP only if its routing profile imports
   the right RT (or the leak path permits it) — the "who imports what"
   lesson, on packets. Kill one announcer and watch the ECMP set shrink.

Exit criteria (section 6) gain this as item 5a. It exercises the piece of
the realizer most likely to be skipped (the host-facing BGP session) and
the piece of nico most often misunderstood (RT-gated reachability).

### 7.5 Decisions taken from section 5

1. Vehicle: nico-dev; generator kept portable. 2. Cardinality: 1:1 first.
3. Realizer language: Python. 4. Host IPs: static first, DHCP later.
5. Admin-VPC datapath **included from day one** — it is the first config
every MAT machine receives, and makes the "before" state physically real.

### 7.6 Sizing and first step

With HBN and the real agent out of scope there is no research risk left;
the remaining risk is translation fidelity and MAT convergence. Estimate:
one to two weeks of ordinary work for connectivity + isolation + anycast;
NSG realization and DHCP realism after. Ordering (POR): after the DPU-sim
gate (Phase I). **First concrete step:** a half-day proof — one hand-built
stand-in, one hand-run `get_managed_host_network_config`, one hand-applied
VRF — before any generator work.
