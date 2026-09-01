# Advanced-user walkthrough — fork publication draft (reviewed 2026-08-29)

Target: nico-dev socialized as branch `nico-dev` on the PUBLIC fork
github.com/jabdulvahid/infra-controller-core, tooling at `tools/nico-dev`,
NEVER merged upstream. Both fork and upstream (renamed NVIDIA/
infra-controller) verified public via unauthenticated API — anything
pushed is world-readable, hence the scrub gate below.

Jasmeer drafted each step; Claude reviewed.

**REWORKED 2026-09-01 — the worktree model (supersedes the sequence
below; README carries the user-facing version; validation run pending):**
1. `mkdir -p ~/nico-tests/vm1/shared` — a folder per VM.
2. Primary nico clone anywhere (e.g. ~/projects/infra-controller) — no
   longer required to live in a share.
3. `git worktree add -b vm1-work ~/nico-tests/vm1/shared/infra-controller
   origin/main` — one branch per VM, disposable.
4. Graft the tools into the worktree: `curl -fsSL <raw fork
   URL>/graft-tools.sh | bash` (or the 4 git commands it wraps —
   fetch FETCH_HEAD checkout of tools/nico-dev, reset, common-dir
   exclude). Untracked + ignored = PR-safe. Rerun = update.
5. `cd tools/nico-dev`, PATH export, check-prereqs.sh; copy
   devup-example.yaml → devup-<site>.yaml, EDIT THESE block, one deploy
   mode; ngc-tags.py picks the NGC tag (dates, arm64, --before paging);
   preflight warns on octet clashes with the Mac's routing table.
6. `dev-up.py --config devup-<site>.yaml`.
Rulings baked in en route: tools distributed by graft (fork branch =
distribution channel, not a source repo); primary-clone freedom; source
mode builds the WORKTREE branch; upstream clone (NVIDIA/infra-controller)
is the no-clone-yet starting point.

**Status: EXECUTED AND PASSED 2026-08-31** — fresh clone of the pushed
branch into ~/nico-tests/vm1/shared, config file (devup-vm1.yaml, NGC
mode ngc: group), one dev-up command; dev-up.py's maiden end-to-end
voyage. Empty context → https://11.133.1.17/admin, Forge
v2.2.0-pr-441-gc594e35f3, dc1/dev1 on the 11/12 defaults, zero source
builds (all seven images pulled from NGC). Findings fixed en route (see
issues.md): 20260831-#1 (bare-ssh MaxAuthTries → pinned identity, plus
known_hosts scrub / preflight / config-aware resume / prep --ssh-key),
#2 (REST images were never shipped by the NGC path — now pulled from NGC
same-tag, --rest-only checkout build as fallback), #3 (script-created
VMs lacked display+serial devices). Steps below preserved as reviewed;
the README carries the user-facing version.

## Step 2 — prerequisites validation (runs FROM the clone; reordered 2026-08-30)

Ordering ruling (Jasmeer): check-prereqs.sh lives in the repo, so the
clone must come first. Only pre-clone need is git itself (macOS CLT
self-prompts). Final order: clone → cd → check-prereqs → dev-up.

```bash
uname -m                 # arm64 (Apple Silicon only)
df -h ~                  # 40GB run / 100GB build — trust Avail
ls /Applications/UTM.app/Contents/MacOS/utmctl   # UTM installed + launched once
# first build run pops "Terminal wants to control UTM" → Allow
ls ~/.ssh/id_*.pub       # else ssh-keygen -t ed25519
python3 -c 'import yaml; print("ok")'   # else pip3 install pyyaml
git --version
```

Build-from-source additionally: colima (start --cpu 4 --memory 8 — default
sizing OOMs the Rust build), docker, kubectl, helm
(`brew install colima docker kubectl helm`). Optional tier: rustup + Go
(native CLIs). Backlog: check-prereqs.sh automating exactly this, callable
as dev-up's true step zero.

## Step 1 (unchanged) — clone (into the share folder)

```bash
mkdir -p ~/nico-tests/vm1/shared
cd ~/nico-tests/vm1/shared
git clone -b nico-dev https://github.com/jabdulvahid/infra-controller-core.git
cd infra-controller-core
ls tools/nico-dev
```

Review: -p required; branch must be named (main never has tools/nico-dev);
https over ssh (public repo, zero setup). Accepted design: ONE clone = nico
source AND tooling → the branch needs a freshness rhythm vs upstream main
(§6 builds from this checkout).

## Step 1b — enter the tooling (folds into step 1 in the README)

```bash
cd ~/nico-tests/vm1/shared/infra-controller-core/tools/nico-dev
```

Findings: script exec bits must survive into the branch; layout makes
self-locating defaults possible (share = parents[2], repo = parents[1].name,
nico-dev-rel derivable) — PENDING script change, kills --repo/--nico-dev-rel
from step 3.

## Step 3 — one command, empty Mac → cluster (former 3+4)

Decide: VM name, user/password, ssh key, dc/site names, octets (11/12 default;
they become Mac-routed prefixes — avoid colliding octets).

```bash
python3 dev-up.py \
  --name nico-vm1 \
  --share ~/nico-tests/vm1/shared \
  --user nico \
  --password 'Welcome123!' \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --dc dc1 --site dev1 \
  --underlay 11 --overlay 12 \
  --repo infra-controller-core \
  --nico-dev-rel infra-controller-core/tools/nico-dev
```

Review rulings baked in: combined vm-creation+bring-up is THE recommended
flow (passthrough flags added, commit dfd2234); password single-quoted
(! history expansion); --ip OMITTED deliberately (auto-detect is the
feature; doc teaches --ip only when the printed subnet is wrong;
--host-num for a different last octet — two VMs can't share .126);
--tag defaults main-YYYYMMDD; --from <step> is the RECOVERY story, not a
required flag. Expectations line: share-Path GUI pause early, maybe VM
password at prep (usually not — key pre-authorized), build 20–40 min
first run, Mac sudo at route, ~1h total.

## Curation pass — pending (Claude's next task)

Build the branch locally in a worktree for Jasmeer's review (HIS push):
- copy scripts to tools/nico-dev + THIS walkthrough as the entry doc
- scrub for world-public: NGC org id/roles/registry details,
  internal-host URLs, colleague usernames, issues.md registry,
  POR/runbooks, real-DC names (sjc/ytl), any baked internal context
- script fixes: self-locating defaults, exec bits, check-prereqs.sh,
  dev-up defaults (--nico-dev-rel default still claude-notes/nico-dev)
- dev-up.py maiden end-to-end run = the vm1 recreation
