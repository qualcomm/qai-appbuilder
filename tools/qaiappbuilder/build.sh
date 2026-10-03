#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# =============================================================================
# build.sh  —  Compile the V2 WebUI frontend into the production SPA bundle
#              (frontend/dist) that start.sh / apps.api serves at runtime.
#
# The Python backend is interpreted (no compile step); only the Vue/Vite
# frontend needs building. After changing frontend source, run this script
# once to refresh frontend/dist, then (re)launch start.sh. After changing
# ONLY backend code, you can skip this script and just restart start.sh.
#
# Usage:
#   ./build.sh               Fast build: vite build only (skips typecheck /
#                             lint / unit tests). Best for the iteration loop.
#   ./build.sh --full        Full verified build: gen:types + typecheck + lint +
#                             unit tests + build. Use before sharing / releasing.
#   ./build.sh --install     Force pnpm install first (e.g. after deps change).
#                             By default install is skipped when node_modules is
#                             already present (fastest iteration).
#   ./build.sh --clean       Wipe node_modules + pnpm-lock.yaml integrity, then
#                             do a fresh pnpm install. Use when node_modules is
#                             corrupt (e.g. ERR_MODULE_NOT_FOUND for a transitive
#                             dep, or when copied across OS/arch boundaries).
#   ./build.sh --help, -h    Show this help and exit (builds nothing).
#
# Notes:
#   * Flags compose, e.g. `./build.sh --full --install`.
#   * Project-local Node.js >= 22 and pnpm >= 9 are bootstrapped automatically
#     (scripts/setup/node_bootstrap.sh) -- no system Node install needed.
#   * setup.sh installs the full backend environment (Python venv, QAIRT SDK,
#     data/) -- run it first if you haven't already.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"

# -- Flags -------------------------------------------------------------------
MODE="fast"
DO_INSTALL=0
DO_CLEAN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      echo ""
      echo " QAI ModelBuilder - build.sh"
      echo " Compiles the V2 WebUI frontend (Vue/Vite -> frontend/dist)."
      echo " The Python backend is interpreted and needs NO build step; restart"
      echo " start.sh to pick up backend-only changes."
      echo ""
      echo " USAGE:"
      echo "     ./build.sh [options]"
      echo ""
      echo " OPTIONS:"
      echo "     (no flag)      Fast frontend build: \`vite build\` only (skips"
      echo "                    typecheck / lint / unit tests). Best for the"
      echo "                    iteration loop. Refreshes frontend/dist."
      echo ""
      echo "     --full         Full verified frontend build:"
      echo "                        gen:types + typecheck + lint + test + build."
      echo "                    Use before sharing / releasing."
      echo ""
      echo "     --install      Force \`pnpm install\` first (e.g. after deps change)."
      echo "                    By default install is skipped when node_modules is"
      echo "                    already present (fastest iteration)."
      echo ""
      echo "     --clean        Wipe node_modules + reinstall from scratch. Use when"
      echo "                    node_modules is corrupt (e.g. ERR_MODULE_NOT_FOUND"
      echo "                    for a transitive dep, or copied across OS/arch)."
      echo "                    Implies --install."
      echo ""
      echo "     --help, -h     Show this help and exit (builds nothing)."
      echo ""
      echo " EXAMPLES:"
      echo "     ./build.sh                  Fast iteration build of the Web UI."
      echo "     ./build.sh --full           Fully-verified Web UI build."
      echo "     ./build.sh --clean          Heal a corrupt node_modules."
      echo ""
      echo " After a frontend build, (re)launch start.sh to serve the new bundle."
      echo ""
      exit 0
      ;;
    --full)    MODE="full" ;;
    --install) DO_INSTALL=1 ;;
    --clean)   DO_CLEAN=1; DO_INSTALL=1 ;;
    --desktop*)
      echo "[WARN] --desktop / --desktop-dev are Windows-only (Tauri). Ignored on Linux." >&2
      ;;
    *)
      echo "[WARN] Unknown option: $1 (ignored)" >&2
      ;;
  esac
  shift
done

# -- Project-local Node.js / pnpm bootstrap ----------------------------------
echo ""
echo "  +------------------------------------------+"
echo "  |   QAI ModelBuilder  -  Building WebUI    |"
echo "  +------------------------------------------+"
echo ""

NODE_BOOTSTRAP="$REPO_ROOT/scripts/setup/node_bootstrap.sh"
if [[ ! -f "$NODE_BOOTSTRAP" ]]; then
  echo "[ERROR] Node bootstrap helper not found: $NODE_BOOTSTRAP" >&2
  exit 1
fi
export REPO_ROOT
# shellcheck source=scripts/setup/node_bootstrap.sh
source "$NODE_BOOTSTRAP"
echo "[INFO] Using Node.js $(node --version) ($(command -v node))"
echo "[INFO] Using pnpm $(pnpm --version) ($(command -v pnpm))"

# -- Frontend directory -------------------------------------------------------
if [[ ! -d "$FRONTEND_DIR" ]]; then
  echo "[ERROR] Frontend directory not found: $FRONTEND_DIR"
  echo "        Are you running this script from the repository root?"
  exit 1
fi
cd "$FRONTEND_DIR"

# -- Install dependencies ----------------------------------------------------
# Detection of corrupted / missing node_modules (adapted from Build.bat):
# Probe for known-critical actual files (not just directory entries), because
# `test -d` returns true even for a broken symlink target.  If any probe
# fails, force a fresh install.
if [[ ! -f "node_modules/.bin/vite" ]]; then        DO_INSTALL=1; fi
if [[ ! -f "node_modules/vite/bin/vite.js" ]]; then  DO_INSTALL=1; fi
if [[ ! -f "node_modules/esbuild/package.json" ]]; then DO_INSTALL=1; fi
if [[ ! -f "node_modules/vue/package.json" ]]; then    DO_INSTALL=1; fi

# --clean: nuke node_modules entirely.
if [[ "$DO_CLEAN" -eq 1 && -d "node_modules" ]]; then
  echo "[INFO] --clean: removing node_modules..."
  rm -rf node_modules
  # Also clean pnpm state files to force a true from-scratch install.
  rm -f node_modules/.modules.yaml node_modules/.pnpm-workspace-state-v1.json 2>/dev/null || true
fi

if [[ "$DO_INSTALL" -eq 1 ]]; then
  echo "[INFO] pnpm install"
  pnpm install --config.confirmModulesPurge=false
else
  echo "[INFO] node_modules present; skipping install (use --install to force, --clean to wipe+reinstall)"
fi

# -- Build -------------------------------------------------------------------
case "$MODE" in
  full)
    echo "[INFO] Full verified build (gen:types + typecheck + lint + test + build)"
    pnpm gen:types
    pnpm typecheck
    pnpm lint
    pnpm test
    pnpm build
    ;;
  fast|*)
    echo "[INFO] Fast build (vite build only; skipping typecheck/lint/test)"
    pnpm exec vite build
    ;;
esac

cd "$REPO_ROOT"
echo ""
echo "[INFO] Build complete. frontend/dist refreshed."
echo "[INFO] (Re)launch start.sh to serve the new bundle."
echo ""
