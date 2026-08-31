# nico-dev Networking Primer — How Packets Move (and How to Think When They Don't)

You already know three things about a machine's network config: its **IP
address**, its **netmask**, and its **router IP** (default gateway). This
document starts from exactly those three, shows what they *really* mean, and
builds up — one idea at a time — to how the BGP fabric inside nico-dev works
and how NAT connects it to the real world. Nothing here requires prior
routing knowledge.

Every concept ends with a **Try it** command you can run against your live
nico-dev VM. Poke things. That's what this environment is for.

Throughout, `7` is the example underlay octet (as in the how-to); substitute
your site's octet (e.g. `11` for a site created with `--underlay 11`), and
`192.168.64.126` is the VM.

---

## 1. What those three settings actually do

When any machine — your Mac, a Linux server, a pod — wants to send a packet,
it makes exactly one decision, using exactly your three settings:

```
Is the destination inside my own subnet?   (my IP + netmask tell me this)
├── YES → deliver directly (ask "who has this IP?" on the local link — ARP)
└── NO  → hand the packet to my router IP and make it THEIR problem
```

That's the entire algorithm a basic host runs. The "router IP" is a
declaration of faith: *"I don't know how to reach the rest of the world, but
this box does."*

Two things follow that most people never get told:

1. Your three settings are really a two-line **routing table** — every
   machine has one, even your Mac:

   ```
   destination          gateway            meaning
   192.168.0.0/24       (directly on-link) "my subnet — deliver myself"
   default (0.0.0.0/0)  192.168.0.1        "everything else — hand to router"
   ```

2. The table can hold **more than two lines**. Add a third line and you've
   taught your machine something new about the world. That is all a route is.

**Try it (Mac):**
```bash
netstat -rn -f inet | head -20     # your Mac's routing table, right now
route -n get 8.8.8.8               # "which line matches this destination?"
```

## 2. The rule that decides everything: most-specific wins

When multiple lines match a destination, the kernel picks the one with the
**longest netmask** — the most specific. `default` (/0) matches everything,
so it only wins when nothing better exists.

This one rule is why the nico-dev GUI access works even on a corporate VPN:

```
default        → utun6 (the VPN — matches everything, mask length 0)
7.133.1.0/27   → 192.168.64.126 (your added route — mask length 27)
```

A packet to `7.133.1.17` matches both lines; /27 beats /0; the packet goes
to the VM, not the VPN. No negotiation, no priority settings — just mask
length. (This is also why the VIP traffic *silently vanishes* if you forget
the route: the VPN's default swallows it, and nothing reports an error.)

**Try it (Mac):**
```bash
route -n get 7.133.1.17     # with the route added: gateway 192.168.64.126
                            # without it: your default (VPN or home router)
```

## 3. A "router" is not a product — it's a kernel flag

Here is the insight the whole machinery hangs on. A router does two jobs,
and they are separable:

- **Forwarding (the data plane):** a packet arrives that is *not addressed
  to me*; look up its destination in my routing table; send it out the right
  interface. The Linux kernel does this natively — it is just **disabled by
  default**. One flag enables it:

  ```
  net.ipv4.ip_forward = 1
  ```

  With that flag, any Linux box IS a router. Without it, the same box
  silently drops every packet not addressed to itself — the polite behavior
  for a laptop, the collapse of everything for a gateway.

- **Table-filling (the control plane):** getting entries INTO the routing
  table. By hand (`route add` / `ip route add` — a *static route*), or
  automatically by routing protocols like BGP (section 5). The kernel
  doesn't know or care which way an entry arrived; forwarding treats them
  identically.

"Router software" (FRR, BIRD, a Cisco box's OS) is control-plane software —
it fills tables. The forwarding itself is the kernel. This is why the
nico-dev VM routes your GUI traffic without running any router product: its
table was filled by our deploy scripts (statically), and `ip_forward=1` does
the rest.

**Try it (VM):**
```bash
sysctl net.ipv4.ip_forward          # 1 — this box forwards
ip route                            # the border router's table: note the
                                    # 7.x entries "via 7.132.0.2" — static
                                    # entries our deploy scripts added
```

## 4. nico-dev's two worlds and the border between them

nico-dev builds an **alternate reality**: a simulated datacenter numbered
`7.x.x.x`, existing only as containers and virtual links *inside* the VM.
Your Mac lives in the real world (`192.168.64.x`). The VM has one foot in
each — a real interface (`enp0s1`, 192.168.64.126) and a fabric interface
(`br-<dc>-cp`, 7.132.1.1) — which makes it the **border router**.

```
 REAL WORLD                     │            ALTERNATE REALITY (inside the VM)
                                │
 Mac ──────────► VM enp0s1      │  br-<dc>-cp ◄──── dpu-1 ◄── leaf-cp ◄── spine-1 ◄── super-spine
 192.168.64.x    192.168.64.126 │  7.132.1.1
                        └───── ip_forward=1 ─────┘
                          (the border crossing)
```

Everything crossing between the worlds passes through the VM's kernel. Both
of this doc's remaining big ideas are about that crossing:

- Mac → fabric (the GUI test): solved with **routes** on both sides.
- Fabric → internet (switches reaching archive.ubuntu.com): solved with
  **NAT**, because the real world cannot be taught routes back into a
  private simulation (section 6).

## 5. BGP: what runs *between* the edges

So far, every routing table was filled by hand. That works at the edges —
your Mac (one route) and the VM (six routes) — because *you own those boxes
and the routes rarely change*. It cannot work inside a datacenter fabric:
hundreds of switches, links failing and recovering, VIPs appearing when
services deploy. Nobody can type fast enough.

**BGP (Border Gateway Protocol) is how routers fill each other's tables by
talking.** Strip away the acronyms and the protocol is three behaviors:

1. **Peering.** Two routers are configured with each other's IP and AS
   number (an AS is just an ID for "who I am"). They open a TCP connection
   (port 179) and keep it alive. A working session shows as `Established` —
   the word you see all over `ndev fabric verify`.

2. **Advertising.** Each router tells its peers: *"I can reach these
   prefixes."* Each listener adds those to its table with the advertiser as
   next hop — the machine equivalent of `route add`, executed continuously
   in both directions. When you see `Estab (4 pfx)` in `ndev bgp info`,
   that session has installed 4 prefixes learned from that peer.

3. **Withdrawing.** When a router loses a route (link dies, service goes
   away), it tells its peers, who remove the entry and fall back to their
   next-best path. This is why a fabric self-heals without anyone typing.

That's it. BGP is `route add` and `route delete`, automated over TCP, with
the routes flowing hop by hop across the fabric: super-spine ↔ spine-1 ↔
leaf-cp ↔ dpu-1, each link one BGP session (see them all: `ndev bgp info
--detail`).

**The punchline for nico-dev: MetalLB is a BGP speaker in disguise.** When
Kubernetes creates a LoadBalancer service, MetalLB — running on the VM's
node — advertises that service's VIP (e.g. `7.133.1.17/32`) over its BGP
session to dpu-1. dpu-1 tells leaf-cp, leaf-cp tells spine-1, and within
seconds every switch in the fabric knows: *"7.133.1.17 lives toward the
VM."* No human added that route anywhere. Deploy a service, a route is
born; delete it, the route is withdrawn. That is the entire value
proposition of running BGP.

Hand-filled routes at the edges, BGP in the middle — this is not a nico-dev
quirk; it is exactly how production datacenters are built. Your Mac's
`route add` plays the role of the office network's edge config; the FRR
containers play the role of the datacenter switches.

**Try it (VM):**
```bash
ndev bgp info --detail                    # every session, every prefix count
ndev fabric shell leaf-cp                 # drop into a real router's CLI
# inside vtysh:
show ip bgp                               # the fabric's view of the world —
                                          # find 7.133.1.17/32: learned from
                                          # dpu-1, which learned it from MetalLB
show ip route bgp                         # BGP-learned entries in the kernel table
```

## 6. NAT/MASQUERADE: lying about the source, for a living

Routes require cooperation from both sides: the Mac learned where `7.133.x`
lives, and the fabric (via BGP) knows to send replies back toward
`192.168.64.x`... wait — does it? Check the direction that CAN'T work:
super-spine pings `1.1.1.1`. The packet can leave (default routes walk it to
the border, `ip_forward` pushes it out), but its **source address is
7.129.0.1** — an address the real internet either can't route back or,
worse, routes to whoever really owns that space. The reply is lost the
moment the packet escapes. You cannot `route add` your way out of this; you
don't own the internet's routers.

**NAT (Network Address Translation) solves it by rewriting the source at
the border.** The rule our fabric deploy installs:

```
iptables -t nat -A POSTROUTING -o enp0s1 -j MASQUERADE
```

Reads: "any packet leaving through the real-world interface — replace its
source with that interface's own address." Super-spine's ping now leaves
stamped `192.168.64.126`, an address the real world happily replies to.

The rewrite alone would break replies (they come back addressed to the VM —
who forwards them to super-spine?). The magic is **conntrack**: the kernel
records every translated flow in a connection-tracking table —
*(7.129.0.1:port → 1.1.1.1) was rewritten to (192.168.64.126:port')* — and
when the reply arrives, reverses the translation automatically. No return
rule needed. If two fabric nodes collide on a port, conntrack silently
assigns different translated ports; one address can front an entire network.

Vocabulary, quickly:
- **SNAT** — rewrite the *source* to a fixed address you specify.
- **MASQUERADE** — SNAT to *whatever the outgoing interface's address
  currently is*. Survives the interface changing addresses; the
  set-and-forget choice for a border.
- **DNAT** — rewrite the *destination* on the way in. You've already used
  it: kube-proxy translating VIP `7.133.1.17` to the nico-api pod's address
  is DNAT. Same conntrack machinery, opposite direction.

And a detail worth savoring: super-spine's ping to 1.1.1.1 is NATed **three
times**, nested like dolls — the VM (7.129.0.1 → 192.168.64.126), UTM's own
NAT layer (→ your Mac's LAN address), and your home/office router (→ your
public IP). Each border keeps its own conntrack table; the reply unwinds the
stack in reverse, each router un-lying in turn. No coordination anywhere —
each NAT layer is fully self-contained, which is why NAT conquered the world.

**The rule of thumb that unifies sections 2–6:**

> **When you can teach both sides routes, route. When the far side can't
> possibly route back to you, NAT.**

Routes preserve addresses and are debuggable end to end; NAT hides your
identity and costs you visibility. nico-dev uses each exactly where it must:
routes for Mac ↔ fabric, NAT for fabric → internet.

**Try it (VM):**
```bash
sudo iptables -t nat -L POSTROUTING -v   # the MASQUERADE rule, with hit counters
sudo conntrack -L | grep 7.129 | head     # live flows being translated right now
                                          # (apt install conntrack if missing)
docker exec clab-<dc>-super-spine ping -c1 1.1.1.1   # generate one, then look again
```

## 7. The journey of one GUI click (everything above, in 12 hops)

You add the route and open `https://7.133.1.17/admin`. What actually happens:

| # | Where | What happens | Which idea |
|---|-------|-------------|------------|
| 1 | Mac | 7.133.1.17 isn't local; table lookup | §1 |
| 2 | Mac | /27 route beats VPN's default | §2 |
| 3 | Mac → VM | packet handed to 192.168.64.126 | §1 (gateway) |
| 4 | VM | not addressed to me — forward it | §3 (`ip_forward`) |
| 5 | VM | table says 7.133.1.0/27 via 7.132.0.2 | §3 (static route) |
| 6 | super-spine → spine-1 → leaf-cp | each forwards via BGP-learned routes | §5 |
| 7 | dpu-1 | knows 7.133.1.17/32 from MetalLB's advertisement | §5 |
| 8 | dpu-1 → VM (7.132.1.1) | next hop is the VM's *fabric-side* interface — the packet enters the VM a second time | §4 |
| 9 | VM (kube-proxy) | DNAT: 7.133.1.17 → nico-api pod IP | §6 |
| 10 | pod | nico-api serves /admin | — |
| 11 | reply | conntrack reverses the DNAT | §6 |
| 12 | reply | retraces the fabric and the Mac route home | §2–5 |

One click exercises every concept in this document. If the page loads, all
of it works; when it doesn't, the next section tells you which hop to blame.

## 8. Thinking in the right way: symptom → relevant question

The skill isn't memorizing answers — it's hearing which question a symptom
is asking. The big three:

| Symptom | What it means | The relevant question |
|---|---|---|
| **Silent timeout** (hangs, then gives up) | Packets are being *lost* — sent somewhere and never returned | "Whose routing table lost them — or is a return path missing?" Trace hop by hop: `route -n get <ip>` on the Mac, `ip route get <ip>` on the VM, `show ip route <ip>` in the fabric. Remember the corp-VPN trap (§2) and that a missing *return* route times out identically to a missing forward route. |
| **Connection refused** (fails instantly) | Routing WORKED — a real machine answered "nothing listens on that port." | "I reached a machine — was it the right one, and is the service up / the port right?" Don't debug routes; check the service, the port number, http vs https (a TLS-to-plaintext mismatch also fails fast). |
| **Works from the VM, not from the Mac** | The inner world is fine; the *border* is the problem | "Is it the Mac's route, `ip_forward`, or a firewall at the crossing?" This split-test is the single most useful move you have — it eliminates half the system in one command. |

Supporting moves, in the order that eliminates the most per step:

1. **Split at the border first.** `curl` from the VM, then from the Mac.
   Same result → inner problem; different → border problem.
2. **Ask the routing table, not your memory.** `route -n get` / `ip route
   get <dst>` answers "where would this packet go *right now*" — including
   surprises like a VPN owning it.
3. **`ping` tests routing; `curl` tests routing + service.** Ping works but
   curl refused → the network is innocent.
4. **In the fabric, ask BGP before pinging.** `ndev fabric verify` /
   `ndev bgp info`: a session not `Established` explains every missing
   route beyond it; prefix count 0 where you expect more means advertisement
   stopped (is the service/MetalLB alive?).
5. **For NAT mysteries, read conntrack.** `sudo conntrack -L | grep <ip>` —
   entries present means translation is happening; absent means packets
   never reached the border.

## 9. Where to poke next

The fabric is disposable and rebuildable in minutes (`deploy-dev-fabric.py`)
— you cannot break anything that matters. Ideas, in rough order of insight
per minute:

- Delete your Mac route and reload the GUI: experience the silent timeout,
  then `route -n get 7.133.1.17` to see the VPN eating it. Re-add; instant fix.
- `ndev fabric shell leaf-cp` → `show ip bgp neighbors 7.130.0.1` — the full
  gory state of one BGP session (timers, message counts, prefixes).
- Kill a link: `docker exec clab-<dc>-spine-1 ip link set eth2 down`, then
  watch `ndev bgp info` show the session drop and `show ip route` on
  super-spine lose everything behind it. Bring it back up; watch BGP heal
  with no human help — the whole point of the protocol, live.
- Watch MetalLB do its thing: `kubectl delete svc nico-dhcp-external -n
  nico-system` (helm will recreate it on next deploy) and watch the /32
  vanish from `show ip bgp` on dpu-1 within seconds.
- Read `how-to.md` § "Learning the fabric" for the vtysh starter commands.
