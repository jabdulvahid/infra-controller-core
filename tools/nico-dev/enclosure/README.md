# Enclosure: a managed DPU (and its server) for a NICo site, without hardware

An enclosure is one DPU stand-in and its two BMCs, run as plain libvirt on a
hypervisor and plugged into a NICo site's fabric: an arm64 VM with real HBN
and the real NICo DPU agent, a DPU BMC and a host BMC (two `bmc-mock`
processes). NICo discovers it, registers the DPU, configures HBN through the
agent, and walks the host through the DPF path exactly as with a rack. The
server stand-in follows in a later step.

The tool, `enclosure.py`, knows nothing about the site beyond two inputs: the
site's root CA and, optionally, its API URL. Everything else the DPU needs
arrives by DHCP from the site. Registering the enclosure with the site (the
expected machine) is done outside the tool, from the identity table it
prints. Creating the hypervisor bridges that reach the site is also outside
the tool; for nico-dev that is [`cable.sh`](cable.sh), described at the end.

Status: the DPU half is implemented; the first `create` run against a live
site is in progress. Report anything the page leaves you guessing.

## Contents

1. [Prerequisites on the hypervisor](#prerequisites-on-the-hypervisor)
2. [The enclosure file](#the-enclosure-file)
3. [Identities](#identities)
4. [Commands](#commands)
5. [After create: the site side](#after-create-the-site-side)
6. [Definition of done](#definition-of-done)
7. [Known gaps](#known-gaps)
8. [nico-dev: connecting an enclosure to the site VM](#nico-dev-connecting-an-enclosure-to-the-site-vm)

## Prerequisites on the hypervisor

An x86 Linux host with libvirt. Everything runs as root (`sudo`) because it
owns VMs, bridges and network namespaces.

| Need | What | Where it comes from |
|---|---|---|
| libvirt + tools | `virsh`, `virt-install`, `qemu-img`, `qemu-system-aarch64`, aarch64 UEFI firmware | `apt-get install -y libvirt-daemon-system virtinst qemu-utils qemu-system-arm qemu-efi-aarch64` |
| seed ISO tool | one of `cloud-localds`, `genisoimage`, `xorriso` | `apt-get install -y cloud-image-utils` |
| Python | `python3` with PyYAML | `apt-get install -y python3-yaml` |
| the DPU image | the golden QCOW2 | [`dpu-image/README.md`](dpu-image/README.md), one command, ~15 min |
| `bmc-mock` | the x86 binary and its TLS pair | `cargo build --release -p bmc-mock` in a NICo checkout → `target/release/bmc-mock`; the certificate pair ships in `crates/bmc-mock/` |
| two bridges | `oob` and `uplink`, already reaching the site's leaf ports | site-specific; nico-dev: [`cable.sh`](cable.sh) below |
| the site's root CA | PEM file | site-specific; nico-dev: one `kubectl` line below |
| `.forge` DNS on the site | the site serves `carbide-pxe.forge` etc. to DPUs | helm-prereqs README step 6; nico-dev: `unbound: true` in bringup.yaml |

The agent resolves `carbide-pxe.forge` and `carbide-ntp.forge` and exits its
main loop when it cannot, so the last row is not optional. nico-dev's values
generator refuses a site with an enclosure configured and unbound off.

## The enclosure file

One YAML file per enclosure. [`enclosure-example.yaml`](enclosure-example.yaml)
is a complete, commented copy to start from. Every key with a default may be
omitted.

```yaml
name: encl2
bmc:
  index: 2
  host_profile: lenovo_gb300_nvl
  bmc_mock: /path/to/target/release/bmc-mock
  cert_dir: /path/to/crates/bmc-mock
  dpu_firmware: {bmc: BF-25.10-24, uefi: 4.13.2-12-g943a91640d, nic: 32.47.3576, cec: 00.02.0195.0000_n02}
dpu:
  image: /var/lib/libvirt/images/enclosure-dpu.qcow2
  vcpus: 4
  memory_mb: 4096
  ssh_authorized_keys: []
  agent_image: localhost/nico/forge-dpu-agent:enclosure
  dhcp_image: localhost/nico/forge-dhcp-server:enclosure
  nvue_password: ""
  login_password: ""
networks:
  oob: patch-encl1
  uplink: patch-enclup1
  hostlink: ""
site:
  root_ca: /path/to/site-ca.pem
  api_url: https://carbide-api.forge
hypervisor:
  libvirt: qemu:///system
  state_dir: /var/lib/enclosure
```

### `name`

Required. The enclosure's name, `[a-z0-9-]`, 1 to 12 characters, starting
with a letter. It prefixes everything the tool creates: the DPU VM
`<name>-dpu`, the BMC namespaces `<name>-dpubmc` and `<name>-hostbmc`, their
systemd units `enclosure-<name>-*bmc`, the host-link bridge `<name>-hl`, and
the state directory `<state_dir>/<name>/`. Pick a name you can type into
`virsh` and `systemctl` without thinking.

### `bmc`

| Key | Default | How to fill it |
|---|---|---|
| `index` | required | `bmc-mock`'s instance index, 1 to 255. Every identity below derives from it (see [Identities](#identities)), so it must be unique per site. Index 0 is MAT's pool; enclosures start at 1. The tool refuses an index another enclosure in the same state directory uses; across hypervisors, keep a short list next to the site definition. |
| `host_profile` | `lenovo_gb300_nvl` | The `bmc-mock` hardware profile of the host BMC. It must be a profile NICo resets over Redfish and that carries exactly one BlueField-3 DPU: `lenovo_gb300_nvl` or `supermicro_gb300_nvl`. Not `nvidia_dgx_h100`: NICo force-restarts that vendor over IPMI, which a standalone mock does not serve. The tool refuses anything else. |
| `bmc_mock` | required | Path to the x86 `bmc-mock` binary. Build it once in a NICo checkout with `cargo build --release -p bmc-mock`; it is independent of the DPU image. |
| `cert_dir` | required | Directory holding `tls.crt` and `tls.key` for the mocks' Redfish listener. The pair ships in NICo's `crates/bmc-mock/`; any checkout works. |
| `dpu_firmware` | the four values shown | What the DPU BMC mock reports as firmware. NICo's DPF gate requires a BMC firmware of 24.10 or newer or it pins the host to the legacy path at creation, which cannot be undone without deleting the host. Leave the defaults unless you are testing the gate. |

### `dpu`

| Key | Default | How to fill it |
|---|---|---|
| `image` | `/var/lib/libvirt/images/enclosure-dpu.qcow2` | The golden image from `dpu-image/build-dpu-image.sh`. Each enclosure gets its own overlay; the golden file is never written. Never replace a golden file that a running enclosure overlays; build to a new name instead. |
| `vcpus` | `4` | vCPUs of the DPU VM. It runs under TCG on an x86 host; fewer than 4 makes HBN's start visibly slower. |
| `memory_mb` | `4096` | Memory of the DPU VM. HBN plus the agent fit in 4 GiB. |
| `ssh_authorized_keys` | `[]` | Extra public keys for the `vm` login on the DPU. The tool always adds its own per-enclosure key, kept in the state directory, which `status` and `dpu-config` use. |
| `agent_image`, `dhcp_image` | `localhost/nico/forge-dpu-agent:enclosure`, `localhost/nico/forge-dhcp-server:enclosure` | The NICo container images the DPU runs, as the image build baked them. Change only to point at a registry the DPU can reach. |
| `nvue_password` | generated | The NVUE REST password the agent uses against HBN. Generated on first create and kept in the state directory (mode 0600); a later `create` reuses it. Set it here or in `ENCLOSURE_NVUE_PASSWORD`, which wins, if you need a known value. |
| `login_password` | generated | Console password of the `vm` user, same handling, `ENCLOSURE_LOGIN_PASSWORD`. |

### `networks`

| Key | Default | How to fill it |
|---|---|---|
| `oob` | required | An existing bridge on the hypervisor that reaches the site's OOB (BMC) segment. The DPU's OOB NIC and both BMC namespaces plug in here, and all three take their address from the site's DHCP through the site's relay. nico-dev: `patch-encl1`, created by `cable.sh`. |
| `uplink` | required | An existing bridge that reaches a leaf port with a BGP unnumbered neighbour. The DPU's `p0` plugs in at MTU 9000. One enclosure per uplink bridge: unnumbered peering is one peer per leaf interface. nico-dev: `patch-enclup1`. |
| `hostlink` | `<name>-hl` | The private bridge between the DPU's host-facing port and the server VM. Left empty, the tool creates `<name>-hl`, records it as its own, and removes it on `destroy`. Name an existing bridge to use it instead. If a bridge named `<name>-hl` exists but was not created by this enclosure, `create` stops; pass `--reuse-bridge` to take it over. |

### `site`

| Key | Default | How to fill it |
|---|---|---|
| `root_ca` | required | The site's bootstrap CA in PEM form, copied to the DPU as the agent's root of trust. How you obtain it is the site's business. nico-dev: `kubectl -n nico-system get secret nico-api-certificate -o jsonpath='{.data.ca\.crt}' | base64 -d > site-ca.pem` with the site's kubeconfig. |
| `api_url` | `https://carbide-api.forge` | The API the agent talks to. The default is the agent's own default, and a correctly set up site serves that name through the resolver it hands out by DHCP. Set it only for a site that serves the API under another name. |

### `hypervisor`

| Key | Default | How to fill it |
|---|---|---|
| `libvirt` | `qemu:///system` | The libvirt connection. |
| `state_dir` | `/var/lib/enclosure` | Where the tool keeps per-enclosure state under `<state_dir>/<name>/`: the identity table, the generated passwords, the ssh key, the seed, the disk overlay, the list of bridges it created. `destroy` removes it. |

## Identities

Everything the mocks and the DPU present derives from `bmc.index`, because
`bmc-mock` derives its own identities from that index and nothing in the
file could override what it reports. For index `N`, written as two hex digits `NN`:

| Identity | Value | Used for |
|---|---|---|
| DPU BMC MAC | `02:00:00:NN:00:01` | the DPU BMC mock on `oob` |
| DPU factory MAC | `02:00:00:NN:00:02` | the host-facing PF; NICo mints the host's admin address for it |
| DPU OOB MAC | `02:00:00:NN:00:03` | the DPU VM's `oob_net0` |
| DPU serial | `MT020000NN0003` | SMBIOS product serial; `expected-machine -d` |
| DPU uplink MAC | `02:00:00:NN:00:10` | the DPU VM's `p0_if` on `uplink` |
| DPU host-link MAC | `02:00:00:NN:00:11` | the DPU VM's `pf0hpf_if` on `hostlink` |
| host BMC MAC | `02:00:00:NN:00:04` | the host BMC mock; `expected-machine --bmc-mac-address` |
| host chassis serial | `020000NN0004` | `expected-machine --chassis-serial-number` |

`create` and `status` print this table for the enclosure. The DPU VM's two
own NICs sit at `:10` and `:11`, above anything the mock's sequential
allocation reaches for a one-DPU profile, so they never collide with the BMC
identities.

## Commands

All as root. `<file>` is the enclosure file.

```bash
sudo python3 enclosure.py create  <file> [--reuse-bridge] [--wait SECONDS]
sudo python3 enclosure.py status  <file>
sudo python3 enclosure.py destroy <file>
sudo python3 enclosure.py dpu-config <file> --set SECTION.KEY=VALUE [--set ...] [--no-restart-agent]
```

**`create`** is idempotent: rerunning it converges and reports what already
existed. In order: it validates the file and the host and stops with the
exact key or path if anything is missing, without having changed anything;
records the identities and the secrets in the state directory; creates the
host-link bridge; writes the cloud-init seed (`dpu.env`, the `vm` login, the
root CA, an agent config only when `api_url` differs from the default);
creates the disk overlay and defines and starts the DPU VM with the
BlueField SMBIOS identity and three NICs in the order OOB, uplink, host link;
starts the two mocks through `bmc.sh` with the firmware flags and the host
profile; then waits (`--wait`, default 600 s) until both mocks hold a lease
and answer Redfish and the DPU holds its OOB lease, reading the DPU over the
OOB bridge's IPv6 link-local address with the per-enclosure key. It ends
with the status report, the identity table and the registration line for the
site. A DPU that has not leased yet is normal in the first minutes: the
first boot renames the NICs and reboots once, and TCG is slow.

**`status`** prints the same report without changing anything: the bridges
and whether each is tool-created or external, the VM state, the mocks'
addresses and Redfish status, the DPU's OOB address and unit states, and the
DPU image's provenance (build time, HBN release, NICo image ids).

**`destroy`** removes the VM and its overlay, both mocks and their
namespaces, the bridges this enclosure created (never the ones named in the
file), and the state directory.

**`dpu-config`** edits the agent's `/opt/forge/config.toml` on the DPU over
ssh and restarts the agent. It exists for one nico-dev quirk, described
next; a production-like site needs it for nothing.

## After create: the site side

The tool has made a DPU and two BMCs that behave like hardware on the
site's OOB segment. The site now has to be told the host exists, the way an
operator tells it about a rack: an expected machine with the host BMC's
MAC, the mock's factory credentials, the host chassis serial and the DPU
serial, all from the identity table. `create` prints the exact
`nico-admin-cli expected-machine add` line; run it from wherever the site's
admin CLI is configured. Site-explorer then probes both mocks, matches the
host BMC MAC, creates the host and DPU pair, and the agent on the DPU
registers against the DPU record.

**nico-dev only: the interface id.** nico-dev runs the API with insecure
discovery on, for MAT's benefit, and in that mode the API refuses a DPU
registration that does not name its own interface. On a site with secure
discovery the API identifies the DPU by the source address of its connection
and this step does not exist. On nico-dev, after site-explorer has created
the DPU, read the id of its OOB interface (`machine show` lists it as the
DPU's primary interface) and push it:

```bash
sudo python3 enclosure.py dpu-config encl2.yaml --set machine.interface-id=<uuid>
```

From here NICo does the rest: the DPU registers, pulls its network config,
HBN applies it and peers with the leaf, the host walks the DPF path and
parks at `HostInitializing/WaitingForDiscovery` until a server VM exists.

## Definition of done

The DPU stand-in is proven by two tests, in this order:

1. **Data path with the existing config.** A server VM PXE-boots on the
   DPU's host port, takes its address from the DPU's DHCP server, chains
   iPXE from the site's PXE service through HBN, is discovered by scout and
   reaches `Ready`.
2. **Reconfiguration.** An instance is created on the machine in a VPC.
   NICo pushes a new network config, the agent applies it through NVUE
   without any restart, the leaf learns the tenant routes, the host moves to
   the tenant subnet; deleting the instance moves it back.

The first needs the server half of the enclosure; the second needs the
first.

## Known gaps

- **LLDP.** The containerized agent expects a sidecar to publish an LLDP
  snapshot at `/data/lldp/current`; the agent image carries no `lldpd`, so
  the agent logs an LLDP collection error on every loop. Cosmetic for the
  tests above. The fix is whatever DPF's service definition runs as that
  sidecar.
- **One uplink.** The DPU has one `p0`; NICo's default requires two
  functioning uplinks. The site has to set `min_dpu_functioning_links = 1`
  under `[machines]` in the API's site config (nico-dev:
  `nico-system.site_config_extra`). The alert for the idle `p1_if` stays.
- **podman, not containerd.** On a real DPU, HBN and the agent are pods on
  containerd and `crictl` is the operator's tool; here they are podman
  containers with host networking. The agent in DPF mode never touches the
  runtime (NVUE REST for config, NVUE health, no container management), so
  NICo cannot tell the difference, but the DPU-OS legacy path cannot be
  tested here, and DPF-level behaviour (pod specs, operator-driven upgrades)
  is not modelled. The enclosure models NICo's DPU, not DPF's.
- **Server half.** Not yet; `power` and `attach` arrive with it.

## nico-dev: connecting an enclosure to the site VM

Everything in this section is specific to a nico-dev site, where the fabric
runs inside a VM. It produces the two bridges and the site inputs the
enclosure file needs.

### The site VM prerequisites

In bringup.yaml: `unbound: true` (the `.forge` zone, see the prerequisites
table). In the site yaml: `fabric.enclosure.cable_mac` and
`uplink_cable_mac` (the two site VM NICs the cables plug into),
`fabric.prefixes.encl_oob` and the matching `rack-encl-oob` network, and
`min_dpu_functioning_links = 1` in `nico-system.site_config_extra`:

```yaml
fabric:
  enclosure:
    cable_mac: "52:54:00:ee:01:01"
    uplink_cable_mac: "52:54:00:ee:01:02"
  prefixes:
    encl_oob: 11.140.3.0/24
nico-system:
  helm-values:
    networks:
      rack-encl-oob: {prefix: "11.140.3.0/24", gateway: "11.140.3.1", mtu: 1500, reserve_first: 2}
  site_config_extra: |
    [machines]
    min_dpu_functioning_links = 1
```

With `encl_oob` set, `generate-dev-fabric.py` gives leaf-mat an `eth3`
holding the segment's gateway and running `dhcrelay` toward nico-dhcp, an
`eth4` with router advertisements and a BGP unnumbered neighbour for IPv4
unicast and L2VPN EVPN, and two bridge nodes `br-<dc>-encl` and
`br-<dc>-enclup`; `deploy-dev-fabric.py` creates the bridges, enslaves the
two site VM NICs found by MAC, and routes the segment via the fabric. Core
maps a relayed lease to the segment from the relay address.

### The cables

A cable is a hypervisor bridge plugged into the site VM as an extra virtio
NIC with the MAC from the site yaml. [`cable.sh`](cable.sh):

```bash
sudo bash cable.sh create patch-encl1   <site-vm> 52:54:00:ee:01:01        # OOB cable
sudo bash cable.sh create patch-enclup1 <site-vm> 52:54:00:ee:01:02 9000   # uplink cable, MTU 9000
sudo bash cable.sh status patch-encl1
```

Order on an existing site: `cable.sh create` for both, edit the site yaml,
redeploy the fabric inside the VM
(`sudo python3 /usr/local/lib/nico-dev/deploy-dev-fabric.py $NICO_DEV_SITE`),
redeploy the `nico` release so nico-api creates the segment
(`deploy-dev-nico.py <site> --only nico --tag <current tag>`). `enclosure.py`
then plugs the DPU VM and the BMC namespaces into those bridges itself.

### The site inputs

- `site.root_ca`: `kubectl --kubeconfig <site>/dc1-<site>.kubeconfig.yaml -n nico-system get secret nico-api-certificate -o jsonpath='{.data.ca\.crt}' | base64 -d > site-ca.pem`
- `bmc.bmc_mock` and `bmc.cert_dir`: `cargo build --release -p bmc-mock` in
  the site's checkout; `target/release/bmc-mock` and `crates/bmc-mock/`.
- registration: the site folder's generated `run-admin-cli.sh` runs the
  `expected-machine add` line `create` prints; the DPU's interface id comes
  from `machine show` and goes in with `dpu-config` as described above.

### The BMC mocks on their own

[`bmc.sh`](bmc.sh) is the primitive `enclosure.py` calls: one `bmc-mock` per
BMC in its own network namespace with its own MAC on the OOB bridge, taking
a NICo lease through the relay like a real BMC. It can be driven by hand:

```bash
sudo bash bmc.sh create  encl1 dpu  02:00:00:01:00:01 --profile lenovo_gb300_nvl -- --dpu-bmc-firmware BF-25.10-24
sudo bash bmc.sh create  encl1 host 02:00:00:01:00:04 --profile lenovo_gb300_nvl
sudo bash bmc.sh status  encl1
sudo bash bmc.sh destroy encl1
```

The mock accepts any credentials unless started with `--redfish-auth`; the
registration line uses its factory pair.
