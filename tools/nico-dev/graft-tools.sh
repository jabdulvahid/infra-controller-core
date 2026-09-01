#!/usr/bin/env bash
# nico-dev — graft (or update) tools/nico-dev into the CURRENT git checkout.
#
#   bash graft-tools.sh              # STABLE: newest validated-* tag
#   bash graft-tools.sh --edge       # branch tip (maintainers / the brave)
#   bash graft-tools.sh --ref <tag|branch>    # exactly that ref
#   bash graft-tools.sh <fork-url> [--edge|--ref X]   # non-default source
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

FORK_URL="https://github.com/jabdulvahid/infra-controller-core.git"
BRANCH="nico-dev"
REF=""
MODE="stable"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --edge)  MODE="edge"; shift ;;
        --ref)   MODE="ref"; REF="$2"; shift 2 ;;
        http*|git@*|/*) FORK_URL="$1"; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Stable channel: newest validated-* tag on the fork; falls back to the
# branch tip when none exist yet.
if [[ "$MODE" == "stable" ]]; then
    # ls-remote lists annotated tags twice — the tag AND its peeled form
    # (name^{}), which is not a fetchable refspec and sorts last
    # (20260901-#8, found on the x86 maiden bootstrap). Drop peeled rows.
    LATEST=$(git ls-remote --tags "$FORK_URL" 'refs/tags/validated-*' 2>/dev/null              | awk -F/ '{print $NF}' | grep -v '\^{}$' | sort | tail -1)
    if [[ -n "$LATEST" ]]; then
        REF="$LATEST"
        echo "Channel: STABLE ($REF) — use --edge for the branch tip."
    else
        REF="$BRANCH"
        echo "Channel: no validated tags yet — using branch tip ($BRANCH)."
    fi
elif [[ "$MODE" == "edge" ]]; then
    REF="$BRANCH"
    echo "Channel: EDGE (branch tip)."
else
    echo "Channel: pinned ref ($REF)."
fi

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: not inside a git checkout — cd into your nico clone/worktree." >&2
    exit 1
}
cd "$TOP"

echo "Fetching ${REF} from ${FORK_URL}..."
git fetch "$FORK_URL" "$REF"

echo "Extracting tools/nico-dev into ${TOP}..."
git checkout FETCH_HEAD -- tools/nico-dev
git reset -q tools/nico-dev

# Do NOT exclude when tools/nico-dev is TRACKED here (the maintainer's
# own fork checkout on the nico-dev branch) — an exclude there hides
# tracked-file work from add -A (bit its own maintainer, 2026-09-01).
if git ls-files --error-unmatch tools/nico-dev >/dev/null 2>&1; then
    echo "tools/nico-dev is tracked in this checkout — skipping the ignore."
else
    EXCLUDE="$(git rev-parse --git-common-dir)/info/exclude"
    if ! grep -qx 'tools/nico-dev/' "$EXCLUDE" 2>/dev/null; then
        echo 'tools/nico-dev/' >> "$EXCLUDE"
        echo "Added tools/nico-dev/ to $(basename "$EXCLUDE") (local-only ignore)."
    fi
fi

SHA="$(git rev-parse --short FETCH_HEAD)"
echo ""
echo "tools/nico-dev grafted (source: ${REF} @ ${SHA}) — untracked and"
echo "git-ignored; your branch and status are untouched. Next:"
echo "  cd tools/nico-dev && export PATH=\"\$PATH:\$(pwd)\" && check-prereqs.sh"
