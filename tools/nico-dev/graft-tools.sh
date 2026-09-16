#!/usr/bin/env bash
# nico-dev — graft (or update) tools/nico-dev into the CURRENT git checkout.
#
#   bash graft-tools.sh              # STABLE: newest validated-* tag
#   bash graft-tools.sh --edge       # branch tip (maintainers / the brave)
#   bash graft-tools.sh --ref <tag|branch>    # exactly that ref
#   bash graft-tools.sh <fork-url> [--edge|--ref X]   # non-default source
#
# Pulls only tools/nico-dev from the fork's nico-dev branch into this
# working tree as UNTRACKED, git-ignored files: invisible to git status,
# impossible to sweep into a commit or PR. The ignore is a `.gitignore`
# INSIDE tools/nico-dev (containing `*`, so it hides itself too): it
# applies to this checkout only. An earlier version wrote the ignore into
# the clone's shared .git/info/exclude, which also hid the directory in
# every OTHER worktree of the same clone — including one where the tools
# are tracked (the fork branch) — so that line is removed when found.
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

# Do NOT ignore when tools/nico-dev is TRACKED here (the maintainer's
# own fork checkout on the nico-dev branch) — an ignore there hides
# tracked-file work from add -A (bit its own maintainer, 2026-09-01).
if git ls-files --error-unmatch tools/nico-dev >/dev/null 2>&1; then
    echo "tools/nico-dev is tracked in this checkout — skipping the ignore."
else
    # Per-directory ignore: `*` hides every grafted file and the .gitignore itself,
    # and it applies to THIS checkout only.
    printf '# grafted by graft-tools.sh — untracked on purpose; never commit this directory\n*\n' \
        > tools/nico-dev/.gitignore
    echo "tools/nico-dev/.gitignore written (ignores the grafted files in this checkout)."
fi
# Migration: the shared info/exclude entry an earlier graft wrote hid the
# directory in every worktree of this clone (2026-09-16 finding); drop it.
EXCLUDE="$(git rev-parse --git-common-dir)/info/exclude"
if grep -qx 'tools/nico-dev/' "$EXCLUDE" 2>/dev/null; then
    grep -vx 'tools/nico-dev/' "$EXCLUDE" > "$EXCLUDE.tmp" && mv "$EXCLUDE.tmp" "$EXCLUDE"
    echo "Removed the clone-wide tools/nico-dev/ entry from $(basename "$EXCLUDE") (replaced by the per-directory .gitignore)."
fi

SHA="$(git rev-parse --short FETCH_HEAD)"
echo ""
echo "tools/nico-dev grafted (source: ${REF} @ ${SHA}) — untracked and"
echo "git-ignored; your branch and status are untouched. Next:"
echo "  cd tools/nico-dev && export PATH=\"\$PATH:\$(pwd)\" && check-prereqs.sh"
