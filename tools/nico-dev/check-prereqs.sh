#!/usr/bin/env bash
# Platform dispatcher — implementations: check-prereqs_mac.sh / check-prereqs_linux.sh
case "$(uname -s)" in
    Darwin) SUF=_mac ;;
    Linux)  SUF=_linux ;;
    *) echo "Unsupported host platform: $(uname -s)" >&2; exit 1 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(basename "$0" .sh)"
TARGET="${HERE}/${BASE}${SUF}.sh"
[[ -f "$TARGET" ]] || { echo "${BASE}${SUF}.sh is not implemented yet on this platform" >&2; exit 1; }
exec bash "$TARGET" "$@"
