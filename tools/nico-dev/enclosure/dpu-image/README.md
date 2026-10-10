# Enclosure DPU image: vmctl's `library/dpu` base plus a real-HBN child layer

Design: `claude-notes/nico-dev/design-real-host-vm.md`, sections 7 and 11.

The Enclosure's DPU stand-in is an arm64 VM, run under plain libvirt (TCG on
an x86 host) and driven by `bmc-mock`'s libvirt backend. vmctl is used only
as an image factory, at build time. This directory is the child layer on
top of vmctl's `library/dpu:latest`; it adds the real HBN container, the
interface identity the DPU agent expects, and the boot-time wiring that turns
an enclosure file into a running DPU. No NICo code is changed.

## What the child adds

| Piece | File | Notes |
|---|---|---|
| Real HBN | `files/enclosure-hbn.service`, `files/enclosure-hbn-start` | `doca_hbn` pinned by digest, pulled at build time. Runs as a podman container with host networking and the DPU-OS host paths under `/var/lib/hbn`, which is also the agent's default HBN root. `nl2doca` is stopped (no offload hardware). NVUE's REST API is enabled through the bootstrap startup YAML, as NICo's own DPF configuration does, and the `carbide` NVUE user is created from the enclosure environment. |
| Kernel modules and AppArmor | `files/enclosure-hbn-modules.conf`, `provision.sh` | The netfilter and ebtables modules HBN's tooling expects; Ubuntu's `bgpd`/`staticd`/`bfdd` AppArmor profiles removed so the container runtime may signal FRR (both from vmctl's real-HBN investigation). |
| Interface identity | `files/enclosure-first-boot.sh` | systemd `.link` files generated from the enclosure MACs: `oob_net0`, `p0_if` (altname `p0`), `pf0hpf_if` (altname `pf0hpf`); a `pf0dpu1`/`pf0dpu1_if` veth pair for the metadata service. |
| Agent wiring | `files/enclosure-nico-dpu-agent.service`, `files/enclosure-nico-dhcp-server.service` | vmctl's two units copied and re-pointed: depend on `enclosure-hbn.service` instead of `nico-hardware-shim.service`, NVUE REST at HBN's own `nvued` on `127.0.0.1:8765` (the agent accepts the self-signed certificate). `nico-hardware-shim` is masked, vmctl's units disabled. |
| Inputs | `files/dpu.env.example` | Everything per-DPU arrives at boot in `/etc/enclosure/dpu.env` (written by the Enclosure tool's cloud-init seed), plus the site CA at `/opt/forge/forge_root.pem` and the agent `config.toml`. Nothing per VPC, instance or host is configured in the image. |

## Build

Prerequisites on the build host (jaslinux): vmctl `main` checked out with
`library/dpu:latest` already built (see
`claude-notes/nico-dev/spike-vmctl-dpu-tcg-main.md`), `qemu-img`, outbound
access to `nvcr.io` for the HBN pull.

```bash
bash build.sh                       # vm image build → enclosure/dpu:latest, then a flattened QCOW2
bash build.sh --out /var/lib/libvirt/images/enclosure-dpu.qcow2
```

`build.sh` passes absolute paths (the `vm` daemon resolves relative paths from
`$HOME`), builds `enclosure/dpu:latest` with TCG and `cortex-a72`, then
flattens the store's layered disk with `qemu-img convert` into a standalone
QCOW2 for libvirt. Override the HBN image with `HBN_IMAGE=...@sha256:...`.

## First boot for verification

```bash
bash boot-test.sh                     # domain dpu-test1 from the flattened image, all NICs on libvirt's default network
bash boot-test.sh --name dpu-test1 --destroy
```

`boot-test.sh` stands in for the Enclosure tool: it writes the cloud-init
seed (`dpu.env` from the example's MACs, the `vm` login), an overlay disk over
the golden image under `/var/lib/libvirt/images/enclosure/<name>/`, and the
libvirt domain with the BlueField SMBIOS identity. It prints the commands for
the verify list below. The agent and DHCP-server units fail until arm64
images exist; HBN and the interface identity do not depend on them.

## Boot-time inputs

The Enclosure tool generates a cloud-init seed per DPU with:

- `/etc/enclosure/dpu.env` (see `files/dpu.env.example`): the three MACs, the
  DPU serial, the NVUE password, the agent and DHCP-server image references.
- `/opt/forge/forge_root.pem`: the site's bootstrap CA (what the DPU OS
  fetches from `carbide-pxe.forge/api/v0/tls/root_ca`).
- `/opt/forge/config.toml`: the agent configuration the site's PXE server
  would normally render.
- libvirt `<sysinfo>` on the domain: `product_name` "BlueField SoC",
  `product_serial` = the DPU serial, `sys_vendor` "Nvidia", so the agent's
  real hardware enumeration derives the same machine id site-explorer does.

## Verified and still to verify

Verified 2026-10-10 on jaslinux: `library/dpu` builds under TCG (5h05m, one
time), boots under vmctl in 1m28s and under plain libvirt from a flattened
QCOW2.

Verified 2026-10-10 on the first child boot (`boot-test.sh`): the cloud-init
seed, the first-boot renames (`oob_net0`, `p0_if`, `pf0hpf_if`, the `pf0dpu1`
veth pair), the SMBIOS serial, and the HBN host paths. The 3.4.0 image
carries its host-side defaults under `/hbn_files/{etc/cumulus,etc/frr,etc/network,etc/supervisor/conf.d}`,
which matches the four config mounts; `nl2docad.conf` exists only there, and
supervisord needs the image's `/var/log/hbn` tree behind the log mount. Both
are seeded at build time.

To verify on the first child boot, in this order:

1. Done: the host paths HBN expects (see above).
2. Done: NVUE REST on `:8765` answers as `carbide` after the bootstrap
   YAML applies (`HBN ready` on the first child boot; `useradd -G sudo,nvapply`
   path). Item 4 is moot in `nvue-rest` mode: the agent's health check there
   is `health/nvue.rs` (NVUE `system_info` plus established BGP sessions on
   the uplinks in the uplinks VRF, `min_dpu_functioning_links` of them, default
   2), not supervisord states. `nl2doca` is left as supervisord runs it;
   `nv config apply` restarts it through `nl2doca-reload` anyway. With one
   uplink per Enclosure DPU the site's `min_dpu_functioning_links` must be 1.
2. (original) NVUE REST on `:8765` after the bootstrap YAML's `listening-address 0.0.0.0`
   is applied, and which group the `carbide` user needs for NVUE
   authorization (`nvapply` is the documented HBN group; confirm).
3. Whether HBN with host networking accepts `p0_if`/`pf0hpf_if` as `swp`
   ports without an SF identity adapter. vmctl's investigation needed an
   identity adapter for NVUE port listing; with host networking and plain
   names this may differ.
4. The agent's health checks (`health.rs`): `frr`, `nl2doca`, `rsyslog`
   RUNNING in supervisord. `nl2doca` is stopped here, as in vmctl's slice;
   if the agent insists, replace the supervisor program with a no-op.
5. `ovs-vswitchd.service` restart must succeed (the agent restarts it on
   admin/tenant switches); the base image has Open vSwitch installed.
