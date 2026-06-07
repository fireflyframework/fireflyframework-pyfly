#!/usr/bin/env bash
# Build wrapper: ensures Homebrew's cairo/pango are loadable, then runs the build
# inside the isolated 3.12 venv. Pass-through args go to build.py (e.g. --epub-only).
set -euo pipefail
BOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
export DYLD_FALLBACK_LIBRARY_PATH="${BREW_PREFIX}/lib:/usr/local/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"
exec "${BOOK_DIR}/.venv/bin/python" "${BOOK_DIR}/build/build.py" "$@"
