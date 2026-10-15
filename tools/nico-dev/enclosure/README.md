# Enclosure: real DPU stand-ins on a nico-dev site

An Enclosure is one server stand-in, one DPU stand-in and their BMCs, run as
plain libvirt VMs on the hypervisor that also hosts the nico-dev site VM. The
DPU stand-in is the arm64 image in [`dpu-image/`](dpu-image/README.md). This
directory holds what connects an enclosure to the site: the cable.

## The cable

A cable is one Linux bridge on the hypervisor that reaches one leaf port inside
the site VM:

```text
hypervisor bridge  ──  extra virtio NIC on the site VM  ──  br-<dc>-encl  ──  leaf-mat:eth3
(patch-encl1)          (fixed MAC = fabric.enclosure.cable_mac)   (in the VM)     (FRR + dhcrelay)
```

Enclosure VMs plug their OOB NIC into the hypervisor bridge. On `leaf-mat:eth3`
the fabric holds the gateway of the enclosure OOB segment and runs `dhcrelay`
toward nico-dhcp, so a DPU stand-in takes its underlay lease from NICo exactly
like a DPU behind a real leaf. Core picks the segment from the relay address.

Site yaml, three additions (the segment must exist in both places):

```yaml
fabric:
  enclosure:
    cable_mac: "52:54:00:ee:01:01"      # the site VM NIC the cable plugs into
  prefixes:
    encl_oob: 11.140.3.0/24             # gateway = first host, on leaf-mat:eth3

nico-system:
  helm-values:
    networks:
      rack-encl-oob:
        prefix: "11.140.3.0/24"
        gateway: "11.140.3.1"
        mtu: 1500
        reserve_first: 2
```

Without `encl_oob` the generator emits the fabric exactly as before. With it:
`generate-dev-fabric.py` adds `eth3`, the `network` statement and the relay
exec on leaf-mat plus the `br-<dc>-encl` bridge node and link;
`deploy-dev-fabric.py` creates the bridge, rebuilds `frr-local` with
`isc-dhcp-relay` if the image lacks it, enslaves the NIC with `cable_mac`, and
routes the segment via the fabric. New sites get `encl_oob` and
`rack-encl-oob` from the template.

Hypervisor side, [`cable.sh`](cable.sh):

```bash
sudo bash cable.sh create patch-encl1 vm-feature1 52:54:00:ee:01:01   # bridge + site VM NIC
sudo bash cable.sh attach patch-encl1 dpu-test1   02:00:00:01:00:03   # DPU OOB NIC onto the bridge
sudo bash cable.sh status patch-encl1
```

Order on an existing site: `cable.sh create`, edit the site yaml, redeploy the
fabric inside the VM (`sudo python3 /usr/local/lib/nico-dev/deploy-dev-fabric.py $NICO_DEV_SITE`),
redeploy the `nico` release so nico-api creates the segment
(`deploy-dev-nico.py <site> --only nico --tag <current tag>`), then
`cable.sh attach` for each enclosure VM. The uplink cable (`leaf-mat:eth4`,
BGP unnumbered) is the next step and is not wired yet.

## The BMCs

[`bmc.sh`](bmc.sh) runs one `bmc-mock` per BMC on the hypervisor, each in its
own network namespace with its own MAC on the cable bridge, so it takes a NICo
lease through the leaf relay like a real BMC. site-explorer probes the address,
matches the host BMC's MAC to an `expected-machine` row, and creates the Host
plus DPU pair from the host mock's Redfish inventory. The DPU stand-in's own
identities (factory MAC, OOB MAC, serial) must equal what the host mock reports
for its DPU; with `--instance-index 1` and a one-DPU profile the mock's
allocation gives DPU BMC `02:00:00:01:00:01`, factory `:02`, OOB `:03`, serial
`MT020000010003`, host BMC `:04` with host serial `020000010004`, which is the
identity the DPU image is seeded with.

```bash
cargo build --release -p bmc-mock                      # the x86 binary, on the hypervisor
sudo bash bmc.sh create encl1 dpu  02:00:00:01:00:01   # DPU BMC
sudo bash bmc.sh create encl1 host 02:00:00:01:00:04   # host BMC (add --libvirt-domain <server VM> once it exists)
sudo bash bmc.sh status encl1
nico-admin-cli expected-machine add --bmc-mac-address 02:00:00:01:00:04 --bmc-username root \
  --bmc-password factory_password --chassis-serial-number 020000010004 -d MT020000010003
```

The mock accepts any credentials unless started with `--redfish-auth`; `root` /
`factory_password` is its factory pair. The DPU VM's board and chassis serials
must be empty: the DPU machine id is a hash of product, board and chassis
serials, and site-explorer fills only the product serial from Redfish.
