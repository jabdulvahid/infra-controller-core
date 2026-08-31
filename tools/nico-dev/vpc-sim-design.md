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
