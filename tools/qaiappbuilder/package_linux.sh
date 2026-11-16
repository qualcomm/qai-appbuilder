#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# package_linux.sh - Build the Linux release package (run on the dev machine).
#
# Builds the web UI (frontend/dist, via build.sh) and packs it together with
# the git-tracked sources into one tar.gz. The user unpacks it and runs
# `bash setup.sh` as usual; because frontend/dist is already inside, setup.sh
# skips Node/pnpm and the frontend build.
#
# Usage: bash package_linux.sh [--version X.Y.Z] [--out DIR] [--skip-build] [--full]
#
#   --version X.Y.Z  package version used in the file name (default: dev)
#   --out DIR        output directory (default: dist/linux)
#   --skip-build     reuse the existing frontend/dist instead of rebuilding
#   --full           frontend build with typecheck/lint/tests (build.sh --full)
#
# The version is only part of the file name; no version file is modified.
# The package is architecture independent (sources + static web UI); the
# Python/QAIRT environments are created on the user's machine by setup.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="dev"
OUT_DIR="$REPO_ROOT/dist/linux"
SKIP_BUILD=0
FULL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)    VERSION="${2:?--version needs a value}"; shift 2 ;;
    --out)        OUT_DIR="${2:?--out needs a value}"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --full)       FULL=1; shift ;;
    -h|--help)    sed -n '/^# Usage/,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[package] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

info()  { echo "[package] $*"; }
error() { echo "[package] ERROR: $*" >&2; exit 1; }

command -v git >/dev/null || error "git is required (the file list comes from git ls-files)"
command -v tar >/dev/null || error "tar is required"
git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || error "$REPO_ROOT is not inside a git work tree"

cd "$REPO_ROOT"

# --- 1. frontend -----------------------------------------------------------
if [[ "$SKIP_BUILD" -eq 1 ]]; then
  info "--skip-build: reusing existing frontend/dist"
else
  if [[ "$FULL" -eq 1 ]]; then
    info "building frontend (build.sh --full)..."
    bash "$REPO_ROOT/build.sh" --full
  else
    info "building frontend (build.sh)..."
    bash "$REPO_ROOT/build.sh"
  fi
fi

[[ -f frontend/dist/index.html ]] || error "frontend/dist/index.html missing - frontend build failed?"
[[ -n "$(ls -A frontend/dist/assets 2>/dev/null)" ]] || error "frontend/dist/assets is empty - frontend build failed?"

# Record which commit the bundle was built from (informational).
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
[[ -z "$(git status --porcelain -- . 2>/dev/null)" ]] || DIRTY="-dirty"
printf 'version=%s\ncommit=%s%s\nbuilt_at=%s\n' \
  "$VERSION" "$COMMIT" "$DIRTY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > frontend/dist/.build_info
[[ -z "$DIRTY" ]] || info "WARNING: working tree has uncommitted changes (recorded as ${COMMIT}-dirty)"

# --- 2. file list: tracked sources + built frontend ------------------------
NAME="QAIAppBuilder-${VERSION}-linux"
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/${NAME}.tar.gz"
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT

# Tracked files that still exist (a deleted-but-unstaged file must not break tar),
# minus Windows-only build outputs, plus the (untracked, git-ignored) web UI.
{
  git ls-files -z -- . | while IFS= read -r -d '' f; do
    [[ -e "$f" ]] && printf '%s\0' "$f"
  done
  find frontend/dist -type f -print0
} | sort -z -u > "$LIST"

COUNT="$(tr -cd '\0' < "$LIST" | wc -c)"
info "packing $COUNT files -> $ARCHIVE"

# Everything goes under one top-level directory so unpacking never litters cwd.
tar --null -T "$LIST" \
    --transform "s,^,${NAME}/," \
    --owner=0 --group=0 --numeric-owner \
    -cf - | gzip -9 > "$ARCHIVE"

# --- 3. sanity check -------------------------------------------------------
# List into a variable first: `grep -q` closing the pipe early would make the
# upstream command fail under `pipefail`.
LISTING="$(gzip -dc "$ARCHIVE" | tar -t)"
grep -qx "${NAME}/frontend/dist/index.html" <<<"$LISTING" \
  || error "archive does not contain frontend/dist/index.html"
grep -qx "${NAME}/setup.sh" <<<"$LISTING" \
  || error "archive does not contain setup.sh"
SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo ""
echo "============================================================"
echo "  Linux package ready"
echo "  File    : $ARCHIVE ($SIZE)"
echo "  Commit  : ${COMMIT}${DIRTY}"
echo ""
echo "  On the target machine:"
echo "    tar -xzf $(basename "$ARCHIVE")"
echo "    cd ${NAME} && bash setup.sh && bash start.sh"
echo "============================================================"
