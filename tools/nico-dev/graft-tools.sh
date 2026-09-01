#!/usr/bin/env bash
# nico-dev — graft (or update) tools/nico-dev into the CURRENT git checkout.
#
#   bash graft-tools.sh              # from anywhere inside your clone/worktree
#   bash graft-tools.sh <fork-url>   # non-default tools source
#
# Pulls only tools/nico-dev from the fork's nico-dev branch into this
# working tree as UNTRACKED, git-excluded files: invisible to git status,
# impossible to sweep into a commit or PR. Works in plain clones and in
# git worktrees (the exclude entry lands in the COMMON git dir, so it
# covers every worktree of the repo, once).
#
# Rerunning updates the tools IN PLACE — local edits under tools/nico-dev
# are overwritten. First-time bootstrap (before you have this script):
# run the four commands below by hand, or curl this file from the fork.

set -euo pipefail

FORK_URL="${1:-https://github.com/jabdulvahid/infra-controller-core.git}"
BRANCH="nico-dev"

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: not inside a git checkout — cd into your nico clone/worktree." >&2
    exit 1
}
cd "$TOP"

echo "Fetching ${BRANCH} from ${FORK_URL}..."
git fetch "$FORK_URL" "$BRANCH"

echo "Extracting tools/nico-dev into ${TOP}..."
git checkout FETCH_HEAD -- tools/nico-dev
git reset -q tools/nico-dev

EXCLUDE="$(git rev-parse --git-common-dir)/info/exclude"
if ! grep -qx 'tools/nico-dev/' "$EXCLUDE" 2>/dev/null; then
    echo 'tools/nico-dev/' >> "$EXCLUDE"
    echo "Added tools/nico-dev/ to $(basename "$EXCLUDE") (local-only ignore)."
fi

SHA="$(git rev-parse --short FETCH_HEAD)"
echo ""
echo "tools/nico-dev grafted (source: ${BRANCH} @ ${SHA}) — untracked and"
echo "git-ignored; your branch and status are untouched. Next:"
echo "  cd tools/nico-dev && export PATH=\"\$PATH:\$(pwd)\" && check-prereqs.sh"
