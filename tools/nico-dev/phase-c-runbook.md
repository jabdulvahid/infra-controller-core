# Phase C runbook — golden image bake, export, and clean-room validation

State at writing (2026-08-26): Phase C1 script hardened (four bake gates),
about to execute. Ordering ruling: golden (C2–C4) → Issue 8 (#5364, due 2nd
week Sep) → Phase I DPF-sim → golden v2 rebake. Full context: this session /
por-test-plan.md.

## C1+C2 — bake and export

1. VM: stop MAT (`pgrep -af machine-a-tron || echo stopped`)
2. Mac: `python3 nico-dev/reset-mat-state.py ~/golden/sites/sjc/ytl --yes`
3. VM: `sudo bash ~/mac/claude-notes/nico-dev/bake-golden-image.sh ~/mac/sites/sjc/ytl/ytl.yaml`
   — four hard gates must print ✓: pods healthy, vault file-mode, fleet t0,
   allow_insecure_discovery present. Also wipes MAT runtime residue
   (maintainer certs/binaries/logs) and resets nico user (Welcome123!).
4. Shut the VM down; **REMOVE the shared directory in the VM's UTM
   settings** (user finding 2026-08-26: config.plist otherwise ships the
   maintainer's share path — importer footgun + path leak; re-add it after
   the export). Then UTM → right-click VM → **Share…** (UTM's export) →
   `nico-dev-golden-YYYYMMDD.utm` (cold, date-stamped). Record size.
   NOTE: the 20260826 v1 export predates this rule and carries the path —
   harmless for the local C3 runs; fix at the v2 rebake.

## C3a — first import test, NO Mac reboot (iteration loop)

User ruling 2026-08-26: validate the image in the current desktop context
first — any image bug gets fixed and re-baked without losing the working
environment ("after reboot I need to get back to my comfort zone").

1. `mkdir ~/nico-dev-test` — a stranger's share folder (fresh repo clone or
   empty, to see what first-boot asks for).
2. Import the .utm as a NEW VM (new name); set its shared directory to
   `~/nico-dev-test`. **Keep the original VM powered off** — both carry the
   same baked static IP.
3. Boot → `ssh nico@<static-ip>` (Welcome123!) →
   `sudo bash /usr/local/lib/nico-dev/first-boot.sh`
4. Target: working cluster in ~5 min. Stopwatch it for the POR.
5. Any failure: fix → re-bake on the ORIGINAL VM → re-export → retry here.

## C3b — v2-master repeat, NO reboot (ruling 2026-08-26)

The Mac reboot is DROPPED: every identified Mac-side state is either
self-clearing (VIP routes die with bridge100 when all VMs stop — proven),
explicitly controllable (`colima stop` for C4), or reboot-proof anyway
(/etc/hosts, ~/.local/bin — always the colleague run's job). Nested-virt
note kept for history: macOS guests get no nested virt (Linux only,
M3+/macOS 15) — UTM-in-a-macOS-VM is unusable TCG.

1. On the ORIGINAL VM: git pull golden checkout → build + redeploy latest
   nico → re-bake (gates re-verify) → remove share → Share… → v2 master.
2. `colima stop` (registry down = C4 precondition, deliberate).
3. Import vm2 from the v2 master (fresh APFS copy), own shared folder,
   only VM running → boot → first-boot — this tests the FIXED baked
   scripts, i.e. the actual distributable.
4. C4 evidence for free: cluster up with registry dead; route re-add
   exercises the how-to bridge100 debugging section.
5. Clean-hardware certification stays with the colleague run.

Known accepted contaminations (same-Mac test): Mac /etc/hosts nico-api
entry persists across reboot; ~/.local/bin CLIs persist. Neither affects
VM-side validation; the colleague run covers them.

## C4 — demo-mode proof

With the Mac registry STOPPED (colima not running), all nico pods on the
clone must come up from cached containerd images — zero Mac dependency.

## Certification run — PASSED 2026-08-28 (v2 image, foreign Mac)

A colleague took the v2 image + GETTING-STARTED on their own Mac:
first-boot succeeded end-to-end, working cluster, "everything worked as
expected." Usability of nico-dev validated by an independent user on
independent hardware. The run's stumbles all became fixes within the
hour: VirtFS-mode + empty-Path doc precision, repos-PARENT prompt
rewording, first-boot mountpoint hard gate, and virtio mount_tag share
auto-detection (20260828-#1). Bonus datum: their vmnet also assigned
192.168.64.0/24 — the baked-static-IP risk did not fire on a second Mac
(sample size now 2). The C5 neutral image inherits every fix; a re-cert
against C5 is cheap and optional.

## Certification run (before public) — original plan

Hand a colleague: the .utm + a one-pager (import, share setup, first-boot
command, Welcome123!, 5-min expectation) — draft the one-pager from this
doc after local C3 passes. Their confusion points are documentation bugs.

Known risk unique to a foreign Mac (the reason the colleague run cannot
be simulated locally): UTM's shared-network subnet is vmnet-assigned per
host (persisted in com.apple.vmnet.plist). Ours is 192.168.64.0/24; a Mac
with a conflict (e.g. Wi-Fi on that range) gets a different subnet and
the image's BAKED STATIC IP is then off-subnet — no SSH, looks bricked.
Escape hatch: `utmctl attach <vm>` (serial console) → fix netplan to the
host's actual subnet. Belongs in the one-pager's troubleshooting box.

## Resume note

The working Claude session survives Mac reboots: `claude --continue` from
~/projects/claude-notes.
