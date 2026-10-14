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
