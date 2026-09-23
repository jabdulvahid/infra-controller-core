# Running the CLIs and MAT on a nico-dev site, step by step

This page is the procedure. It assumes your site is up: you followed
`how-to-mac.md` to the end and the admin UI answers. It tells you which
command to run, where to run it, and what you should see. The reasons behind
each step, the failure catalog and the custom-build recipes are in
`mat-in-nico-dev.md`; you do not need them to get a first run going.

Two words you will meet on every step:

- **The admin CLI** is `nico-admin-cli`, the operator's command-line tool for
  the NICo API. You use it to look at machines and to change site settings.
- **MAT** is `machine-a-tron`, the simulator that plays a fleet of managed
  hosts: their BMCs, their DPUs, their DHCP requests and their boot. NICo
  discovers and ingests the fleet exactly as it would real servers.

Every command below shows `<site>`. Replace it with your site folder:

| Where you type | Your site folder |
|---|---|
| on the Mac | `<share>/sites/dc1/dev1`, for example `/Users/you/nico-tests/vm1/shared/sites/dc1/dev1` |
| on the VM | `~/mac/sites/dc1/dev1` |

They are the same folder, seen from the two sides of the share. The scripts
live in `tools/nico-dev` of the checkout in the share; the how-to put that
directory on your PATH on both sides.

## 1. The admin CLI, on the VM

You do not compile the admin CLI. The NICo API container already contains the
binary, and one script copies it out, issues the client certificates and
writes a wrapper. Do this once per site, on the VM:

```bash
ssh nico@192.168.64.126
get-admin-cli.sh ~/mac/sites/dc1/dev1
~/mac/sites/dc1/dev1/run-admin-cli.sh version
```

The last command prints the CLI version and the API version. If it does, the
CLI reaches the API. Always call the wrapper `run-admin-cli.sh`; the bare
binary dials the API by its in-cluster name and fails outside the cluster.

The wrapper dials the API as `nico-api.<dc>-<site>`, a name that exists only
in `/etc/hosts`. The first call on each side of the share may ask for your
password once: the wrapper adds that name to `/etc/hosts` if it is missing.
The same wrapper works from the Mac and from the VM.

## 2. Build MAT, on the Mac

MAT is built inside a Linux container on the Mac, because it runs on the VM
and the VM is Linux. Nothing has to be installed on the Mac beyond Docker,
which the how-to already required.

```bash
build-nico-clis.py <site> --mat-only
```

The first build compiles the whole crate and takes a while, twenty minutes or
more; later builds reuse the cache and take a few minutes. When it finishes,
the binary is at `<site>/mat/machine-a-tron`. That path is in the share, so
the VM sees it immediately.

## 3. Configure MAT, on the Mac

```bash
configure-clis.py <site>
```

This writes everything MAT needs, all derived from the site yaml:

- `<site>/certs/mat/`: the client certificate MAT presents to the API.
- `<site>/mat/mat-config.toml`: the fleet definition. By default it describes
  two hosts with one DPU each.
- `<site>/run-mat.sh`: the script that starts MAT on the VM.
- an `/etc/hosts` entry on the Mac for the API hostname; it asks for `sudo`.

You can run it again at any time; it regenerates the same files from the
site yaml. If you want a different fleet, edit `mat-config.toml` after this
step and before starting MAT; the run script copies it to the VM at start.

## 4. Start MAT, on the VM

MAT runs on the VM only. It puts the addresses of its mock BMCs on the fabric
bridge inside the VM, and the NICo site explorer connects to them there. Open
a terminal for MAT and leave it open; MAT runs in the foreground and logs to
that terminal.

```bash
ssh nico@192.168.64.126
~/mac/sites/dc1/dev1/run-mat.sh
```

The script copies the binary, the certificates and the configuration from
the share to local disk, installs the binary, stages everything under
`/etc/machine-a-tron/dc1/`, adds the API hostname to the VM's `/etc/hosts`
and launches MAT under `sudo`. It asks for your password once.

You know it is running when the log shows the BMCs coming up and, a little
later, DHCP requests being answered. The same log is written to
`/var/log/machine-a-tron-dc1.log`.

## 5. Watch the fleet appear, from a second terminal on the VM

```bash
ssh nico@192.168.64.126
~/mac/sites/dc1/dev1/run-admin-cli.sh machine show
```

`machine show` without an argument lists every machine; with a machine ID it
shows one in full. Run it every minute or so. Hosts and DPUs appear first as
discovered endpoints, visible with `run-admin-cli.sh site-explorer get-report
endpoint`, then as machines walking through their lifecycle, and finally with
the state `Ready`. The default fleet simulates GB200 NVL hosts, the slowest
platform profile MAT has: a simulated power-on alone takes ten minutes, so
expect fifteen to twenty-five minutes before the whole fleet is `Ready`. The
MAT terminal shows the same progress from the fleet's side.

To follow the log without the MAT terminal:

```bash
sudo tail -f /var/log/machine-a-tron-dc1.log
```

The admin UI shows the same machines in its browser view.

**One screen for the whole run.** `monitor-mat.py` asks the admin CLI for the
expected machines, the explorer's endpoints, the machines and the DPUs,
asks Kubernetes for the DPF resources, reads the MAT log for MAT's own view
of each mock, and redraws every 30 seconds. For each machine it shows the
state NICo reports, the lifecycle milestone that state belongs to, and how
many milestones remain before `Ready`. For each DPU it shows NICo's view
(`dpu status`, `dpf show`) and DPF's view: the DPU resource's phase, where
that phase sits on the simulator's path, how many phases remain, how long it
has been there, and whether the host has a reboot pending that NICo must
perform. The simulator pod's state is on the same line, so a stuck DPF path
is visible at a glance. On the VM:

```bash
run-monitor-mat.sh            # full screen; q quits, r refreshes now
run-monitor-mat.sh --once     # one plain-text snapshot, good for pasting
```

The full-screen view has pages. Page 0 is the overview above with every
section expanded except MAT, which is collapsed to a count line because it has
its own page; `e m u d l` collapse or expand any section there. Pages 1 to 5 show one section alone and
scroll when it does not fit: 1 endpoints, 2 machines, 3 DPUs as NICo sees
them, 4 DPF, 5 MAT. Press the digit, or step with `←`/`→`, Tab, `n`/`p`;
scroll with `↑`/`↓`, PgUp/PgDn, Home/End. The footer names the pages and
marks the current one. A fleet larger than the screen, or a long DPF table,
is read on its own page instead of being cut off at the bottom of page 0.
`?` (or `h`) opens a help page with the pages, keys and column meanings; any
page key returns. When the launcher passes several MAT logs (base, dev,
plain), the MAT page opens on the most recently written one; `[` and `]`
step to the others and `a` shows them all, so an older run's log can be
compared without restarting the monitor.

`run-monitor-mat.sh` has the site path for `dc1/dev1` written in; edit its
first lines for another site, or call `monitor-mat.py` directly with
`--admin-cli <site>/run-admin-cli.sh` and one `--mat-log <file>` per log. It
finds the MAT logs itself: every `/var/log/machine-a-tron-<dc_name>*.log`,
where `<dc_name>` is `fabric.dc_name` from the site yaml and the name the
generated `run-mat*.sh` scripts log under (`run-mat-dev.sh` writes
`machine-a-tron-<dc_name>-dev.log`). Pass `--mat-log <file>` to the launcher
to pin exactly the log you want instead. The DPF section finds the kubeconfig
next to the wrapper; `--no-dpf` skips it on a site without DPF. It works
before any MAT run too, showing the server side alone. MAT logs are
root-owned, so the launcher uses `sudo` when it has to.

## 6. Which provisioning path your hosts take

A nico-dev site created by the how-to has DPF enabled, so every host MAT
registers is provisioned through the DPF path: NICo hands each DPU to the
DPF simulator, which walks it through its phases on a timer, and NICo
power-cycles the host when the simulator asks for it. Nothing is flashed;
only the status transitions are reproduced.

If you want one host to take the older iPXE path instead, disable DPF for
that host before NICo ingests it, that is, right after it appears in
`machine show` and before it leaves its first states:

```bash
~/mac/sites/dc1/dev1/run-admin-cli.sh dpf disable <host-machine-id>
```

NICo refuses the change once the host has been ingested through DPF. In that
case reset the fleet, section 8, and disable earlier on the next run.

## 7. Stop MAT

Press Ctrl-C in the MAT terminal. MAT deletes its machines from NICo on the
way out. Check that `machine show` lists nothing afterwards; if it does, the
shutdown was interrupted before cleanup, and the reset in the next section
puts things right.

## 8. Reset the fleet between runs, on the Mac

NICo remembers the fleet it has seen: the endpoints it explored, the machines
it expected, and the BMC credentials it rotated on each mock. A new MAT run
brings fresh mocks with the same addresses and the same MAC addresses, and
NICo would lock them out as impostors. So, after stopping MAT and before
starting it again, always run:

```bash
reset-mat-state.py <site> --yes
```

It wipes the fleet state and leaves the site's day-1 configuration in
place. On a brand-new site you can skip it before the very first run.

If you also want the site-wide default credentials recreated, add `--full`
and a `--password`; you rarely need that.

## 9. Run a MAT you built yourself

When you change MAT's source, build from your own worktree into a separate
folder so the site's baseline binary stays untouched:

```bash
build-nico-clis.py <site> --mat-only --repo ~/projects/my-mat-worktree --out-dir <site>/mat-dev
```

Then copy `run-mat.sh` to `run-mat-dev.sh` and change the two paths at its
top, `MAT_BIN` and `MAT_CONFIG`, to the `mat-dev` folder. The copy logs to
its own file, `/var/log/machine-a-tron-dc1-dev.log`, so the baseline and
your build never overwrite each other. The details, including a custom
`mat-config.toml` with timing overrides, are in `mat-in-nico-dev.md`,
section 10.

## 10. nicocli, the REST CLI

Everything above talks to NICo's core API. The REST API has its own CLI,
`nicocli`, and the REST API image ships it, so you do not compile this one
either. On the Mac:

```bash
get-nicocli.sh <site>
```

This copies the binary out of the REST API image into `<site>/nicocli/` and
writes `<site>/run-nicocli.sh`. The binary is a Linux build, so use the
wrapper on the VM. The first call mints an access token inside the cluster,
which takes a few seconds, and caches it for 25 minutes.

```bash
ssh nico@192.168.64.126
~/mac/sites/dc1/dev1/run-nicocli.sh --bootstrap    # once per fresh site
~/mac/sites/dc1/dev1/run-nicocli.sh vpc list
```

`--bootstrap` creates the organisation's provider and tenant objects. Until
it has run once, every other call fails with "Org does not have a Tenant
associated". If a command fails with an authentication error after the site
was redeployed, put `--refresh-token` in front of it once.

## 11. When something does not look right

| What you see | What it usually means | Where to look |
|---|---|---|
| `run-mat.sh` stops at once saying the binary is not ELF | the build did not finish, or an old Mac binary is in `<site>/mat/` | rebuild with section 2 |
| machines appear, then every endpoint shows a lockout error | the fleet was not reset since the last run | section 8, then restart MAT |
| hosts sit in `dpuinit` and never move | the DPF simulator is not running | `kubectl -n dpf-operator-system get pods` on the VM; `mat-in-nico-dev.md` section 13 |
| `machine show` says the API is unreachable | the wrapper was generated on the other side of the share, or the API VIP route is gone | rerun section 1 on the VM; `how-to-mac.md` section 7 for the route |
| a DPU parks in `Rebooting` | the host's mock did not finish its power cycle | the MAT log; `run-admin-cli.sh machine show <host>` |
| `run-admin-cli.sh` prints "not in `/etc/hosts` and could not be added" and exits | the wrapper could not run `sudo` on this side | run the one-liner it printed, then retry |
| `run-admin-cli.sh` hangs without output | a wrapper generated before 2026-09-21, which did not check the hosts entry | regenerate it: section 1 on the VM, or `configure-clis.py <site> --admin-cli-only` |

Everything else, symptom by symptom, is in the failure catalog of
`mat-in-nico-dev.md`, section 8.
