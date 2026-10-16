# Enclosure DPU image: Ubuntu arm64 cloud image plus real HBN and the NICo agent

Design: `claude-notes/nico-dev/design-real-host-vm.md`, sections 7 and 11.

The Enclosure's DPU stand-in is an arm64 VM, run under plain libvirt (TCG on
an x86 host) and driven by `bmc-mock`. The image is Ubuntu's arm64 cloud
image plus the real HBN container, NICo's arm64 agent and DHCP-server
images, MFT for `flint`, the interface identity the DPU agent expects, and
the boot-time wiring that turns an enclosure file into a running DPU. It is
built with plain qemu and cloud-init (`build-dpu.sh`); vmctl is not involved.
No NICo code is changed.

## What the image adds on top of the cloud image

| Piece | File | Notes |
|---|---|---|
| Real HBN | `files/enclosure-hbn.service`, `files/enclosure-hbn-start` | `doca_hbn` pinned by digest, pulled at build time. Runs as a podman container with host networking and the DPU-OS host paths under `/var/lib/hbn`, which is also the agent's default HBN root. `nl2doca` exits without offload hardware; the start script creates its ACL sync control files so `cl-acltool` (and therefore every NVUE apply carrying NICo's ACLs) succeeds without it. NVUE's REST API is enabled through the bootstrap startup YAML, as NICo's own DPF configuration does, and the `carbide` NVUE user is created from the enclosure environment. |
| Kernel modules and AppArmor | `files/enclosure-hbn-modules.conf`, `provision.sh` | The netfilter and ebtables modules HBN's tooling expects; Ubuntu's `bgpd`/`staticd`/`bfdd` AppArmor profiles removed so the container runtime may signal FRR (both from vmctl's real-HBN investigation). |
| Interface identity | `files/enclosure-first-boot.sh` | systemd `.link` files generated from the enclosure MACs: `oob_net0`, `p0_if` (altname `p0`), `pf0hpf_if` (altname `pf0hpf`); a `pf0dpu1`/`pf0dpu1_if` veth pair for the metadata service. |
| Agent wiring | `files/enclosure-nico-dpu-agent.service`, `files/enclosure-nico-dhcp-server.service` | Two units (derived from vmctl's) that run the containers against real HBN: NVUE REST at HBN's own `nvued` on `127.0.0.1:8765` (the agent accepts the self-signed certificate). |
| Inputs | `files/dpu.env.example` | Everything per-DPU arrives at boot in `/etc/enclosure/dpu.env` (written by the Enclosure tool's cloud-init seed), plus the site CA at `/opt/forge/forge_root.pem` and the agent `config.toml`. Nothing per VPC, instance or host is configured in the image. |

## Build

Two steps on one x86 Linux host (jaslinux), both scripted, no vmctl:

| Step | Script | Produces | Time | When |
|---|---|---|---|---|
| 1 | `build-dpu-nico-images.sh` | `nico-arm64-images.tgz`: NICo's arm64 `forge-dpu-agent` + `forge-dhcp-server` images | ~20 min | per NICo version |
| 2 | `build-dpu.sh` | the golden QCOW2: Ubuntu 24.04 arm64 + real HBN + the NICo images + MFT (`flint`) + the enclosure wiring | 30–60 min under TCG | per NICo or HBN version |

```bash
bash build-dpu-nico-images.sh --repo ~/build/nico --ref upstream/main --out /tmp/nico-arm64-images.tgz
bash build-dpu.sh --nico-images /tmp/nico-arm64-images.tgz --out /var/lib/libvirt/images/enclosure-dpu.qcow2
```

Host prerequisites: `qemu-system-aarch64`, `qemu-img`, `qemu-efi-aarch64`
(AAVMF), `cloud-image-utils` (or `genisoimage`/`xorriso`), `ssh`, `curl`,
`docker` and `cargo-make` for step 1.

Step 1 builds from the NICo repository's own definitions: the x86 cross
container CI uses for the arm64 binaries, then the two Dockerfiles, which
only copy onto a distroless base, so no arm64 emulation is involved. Build
from the commit the site's Core runs (`--ref`), or upstream main at the time
the site was brought up. The script fetches the MFT package the agent image
needs for `flint`, checks the binaries for 64 KiB page-size compatibility,
tags the images `localhost/nico/forge-<name>:enclosure` as `dpu.env`
expects, and `docker save`s them.

Step 2 downloads Ubuntu's noble arm64 cloud image (checksum-verified, cached
under `~/.cache/enclosure-dpu-build`), boots it once under
`qemu-system-aarch64` (TCG on x86; KVM or HVF where the host is arm64) with a
cloud-init seed and a throw-away build key, uploads `files/` (including the
NICo tarball and the MFT package it fetches) and runs `provision.sh` over
ssh, seals the image (cloud-init clean, build key and machine-id removed so
every DPU VM mints its own identity), powers it off and flattens the disk
with `qemu-img convert`. Override the HBN image with
`HBN_IMAGE=...@sha256:...`. The image records what it carries in
`/usr/local/lib/enclosure/provenance` (build time, HBN digest, the two NICo
image ids). The build directory with the serial and provisioning logs is
kept for inspection.

A DPU image is therefore a per-NICo-version artefact. Moving the agent means
steps 1 and 2 again and recreating the DPU VMs from the new image, the way a
DPF site moves its DPU service only on a release.

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

Verify list, first child boot (2026-10-10):

1. Done: the host paths HBN expects (see above).
2. Done: NVUE REST on `:8765` answers as `carbide` after the bootstrap YAML
   applies (`HBN ready` on the first child boot; the `useradd -G sudo,nvapply`
   path).
3. Done, moot in `nvue-rest` mode: the agent's health check there is
   `health/nvue.rs` (NVUE `system_info` plus established BGP sessions on the
   uplinks in the uplinks VRF, `min_dpu_functioning_links` of them, default 2),
   not supervisord states. `nl2doca` is left as supervisord runs it; `nv config
   apply` restarts it through `nl2doca-reload` anyway. With one uplink per
   Enclosure DPU the site's `min_dpu_functioning_links` must be 1.
4. Done, with one accommodation: NVUE classifies a link as `swp` only when
   the kernel reports its parent device as an `mlx5_core.sf` subfunction
   (`cue_netlink_v1/translated.py`, `_check_link_kind`); a virtio NIC or veth
   matches no kind and every interface query is a 500. The HBN image used
   here is derived at build time (`files/hbn-patch/`) so that the SF port
   names also qualify. With that, `p0_if`, `pf0hpf_if` and `pf0dpu1_if` show
   as `swp`, and ifupdown2 brings the first two up (MTU 9216, link-local).
   Kernel-side alternative if other HBN components read `parentdev`: a DKMS
   module re-parenting the netdevs under platform devices named
   `mlx5_core.sf.N`.
5. Done: `ovs-vswitchd.service` restarts cleanly.

Also found on the way: HBN must run in its own PID namespace (with
`--pid host`, `nvued --daemon` forks after startup and supervisord respawns
it endlessly, orphans surviving `podman rm -f`), and Ubuntu's `rsyslogd`
AppArmor profile denies supervisord's signals like the FRR ones.

Next: arm64 `forge-dpu-agent` and `forge-dhcp-server` images, then the
Enclosure tool (seed, domain, cables, bmc-mock) around `boot-test.sh`.
