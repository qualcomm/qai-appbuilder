#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# =============================================================================
# Setup.command — macOS environment setup for QAI AppBuilder
#
# PRIMARY INSTALLER. Run this first to set up everything:
#   Step 1: Python environment (conda or system Python >= 3.12 + venv)
#   Step 2: QAI AppBuilder pip dependencies
#   Step 3: Playwright + Chromium browser  (web search feature)
#   Step 4: TTS runtime data               (NLTK corpora / jieba / g2p_en)
#
# Design principles:
#   - Every step checks before installing — reuses whatever is already present
#   - Writes manifest after EACH step (so partial installs are trackable)
#   - Everything this script installs can be removed by Uninstall.command
#   - No system-level software is installed (everything stays in venv or
#     the project tree)
#   - Idempotent: safe to run multiple times
#
# Usage:
#   Double-click Setup.command
#
# NOTE: This script does not build the frontend. After Setup.command,
#       run Build.command once to produce frontend/dist, then Start.command.
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
    echo "[ERROR] Cannot find pyproject.toml in: $REPO_ROOT"
    exit 1
fi

cd "$REPO_ROOT"

# Manifest: records what this setup actually installed.
# Written incrementally after each step, so a partial install is trackable.
# Uninstall.command reads this to know what to remove.
MANIFEST="$REPO_ROOT/data/setup_mac_manifest.txt"
mkdir -p "$REPO_ROOT/data"

# Start fresh manifest each run
: > "$MANIFEST"

echo ""
echo "  +------------------------------------------+"
echo "  |   QAI AppBuilder — macOS Setup           |"
echo "  +------------------------------------------+"
echo ""

# =========================================================================
# Step 1: Python environment (conda or system Python >= 3.12)
# =========================================================================
echo "-- Step 1: Python environment ------------------------------------------"

VENV_DIR="$REPO_ROOT/envs/venv"
VENV_PY="$VENV_DIR/bin/python3"

if [ -x "$VENV_PY" ]; then
    echo "[SKIP] Python venv already exists: $VENV_DIR"
    echo "venv_dir=$VENV_DIR" >> "$MANIFEST"
else
    PYTHON=""

    # Strategy A: conda
    if command -v conda >/dev/null 2>&1; then
        CONDA_ENV_NAME="qaiappbuilder"
        CONDA_BASE="$(conda info --base 2>/dev/null)"
        CONDA_PY="$CONDA_BASE/envs/$CONDA_ENV_NAME/bin/python3"
        if [ -x "$CONDA_PY" ]; then
            echo "[INFO] conda environment '$CONDA_ENV_NAME' already exists."
            PYTHON="$CONDA_PY"
        else
            echo "[INFO] conda found — creating environment '$CONDA_ENV_NAME' with Python 3.12..."
            conda create -n "$CONDA_ENV_NAME" python=3.12 -y
            PYTHON="$CONDA_PY"
        fi
        echo "conda_env=$CONDA_ENV_NAME" >> "$MANIFEST"
        echo "[INFO] Using conda Python: $PYTHON"
    fi

    # Strategy B: system Python >= 3.12
    if [ -z "$PYTHON" ]; then
        for cand in \
            "$(command -v python3.13 || true)" \
            "$(command -v python3.12 || true)" \
            "$(command -v python3 || true)"; do
            [ -z "$cand" ] && continue
            [ -x "$cand" ] || continue
            ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
            [ -z "$ver" ] && continue
            if [ "$(printf '%s\n%s\n' "3.12" "$ver" | sort -V | head -n1)" = "3.12" ]; then
                PYTHON="$cand"
                break
            fi
        done
    fi

    if [ -z "$PYTHON" ]; then
        echo "[ERROR] Neither conda nor Python >= 3.12 found on this machine."
        echo "        Please install conda (https://docs.conda.io) or Python 3.12+ first."
        rm -f "$MANIFEST"
        exit 1
    fi

    echo "[INFO] Creating .venv with $PYTHON ..."
    mkdir -p "$REPO_ROOT/envs"
    "$PYTHON" -m venv "$VENV_DIR"
    echo "venv_dir=$VENV_DIR" >> "$MANIFEST"
    echo "[OK]   Python venv created."
fi

PYTHON="$VENV_PY"
echo ""

# =========================================================================
# Step 2: QAI AppBuilder pip dependencies
# =========================================================================
echo "-- Step 2: QAI AppBuilder pip dependencies ------------------------------"

if "$PYTHON" -c "import fastapi, uvicorn, structlog" 2>/dev/null; then
    echo "[SKIP] Core dependencies already installed."
else
    echo "[INFO] Installing QAI AppBuilder (pip install -e .) — this may take a few minutes..."
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -e .
    echo "[OK]   Dependencies installed."
fi
echo "pip_deps=installed" >> "$MANIFEST"
echo ""

# =========================================================================
# Step 2b: Initialize data/ tree (install pipeline — seed factory defaults)
# =========================================================================
# This is the step that populates data/db/qai.db with factory defaults:
#   - cloud-gateway provider (qai-service), default model, toolbar prefs, etc.
#   - SQLite schema migrations
#   - SecretStore namespaces
# Without this step the app starts with an empty database — no cloud models,
# no provider routes, no user preferences seeded.
echo "-- Step 2b: Initialize data/ tree (factory seeds + migrations) ----------"

INSTALL_SCRIPT="$REPO_ROOT/scripts/init/install.py"
if [ -f "$INSTALL_SCRIPT" ]; then
    echo "[INFO] Running data/ initialisation (seed factory defaults)..."
    "$PYTHON" -m scripts.init.install --apply \
        --factory-root "$REPO_ROOT/factory" \
        --data-root "$REPO_ROOT/data" \
        --sql-migrations "$REPO_ROOT/src/qai/platform/persistence/migrations_sql" \
        --secret-backend auto \
        --skip compile_factory
    if [ $? -eq 0 ]; then
        echo "[OK]   data/ initialised (qai.db + factory seeds + secret namespaces)."
    else
        echo "[WARN] data/ initialisation reported errors. Some factory defaults"
        echo "[WARN] may not be seeded. The UI is still usable; you can re-run with:"
        echo "[WARN]     $PYTHON -m scripts.init.install --apply \\"
        echo "[WARN]         --factory-root $REPO_ROOT/factory \\"
        echo "[WARN]         --data-root $REPO_ROOT/data \\"
        echo "[WARN]         --sql-migrations $REPO_ROOT/src/qai/platform/persistence/migrations_sql \\"
        echo "[WARN]         --secret-backend auto --skip compile_factory"
    fi
else
    echo "[WARN] Install script not found: $INSTALL_SCRIPT"
    echo "[WARN] data/ will NOT be initialised. Cloud models and provider routes"
    echo "[WARN] will be missing. Please ensure the factory/ directory is complete."
fi
echo "data_init=done" >> "$MANIFEST"
echo ""

# =========================================================================
# Step 3: Playwright + Chromium
# =========================================================================
echo "-- Step 3: Playwright (web search) -------------------------------------"

PW_PKG_INSTALLED=0
if "$PYTHON" -c "import playwright" 2>/dev/null; then
    echo "[SKIP] playwright package already installed."
else
    echo "[INFO] Installing playwright..."
    "$VENV_DIR/bin/pip" install "playwright>=1.40,<2.0"
    PW_PKG_INSTALLED=1
fi

PW_BROWSER_INSTALLED=0
PW_CACHE_DIR="$HOME/Library/Caches/ms-playwright"
if [ -d "$PW_CACHE_DIR" ] && ls "$PW_CACHE_DIR"/chromium-*/chrome-mac*/Chromium.app >/dev/null 2>&1; then
    echo "[SKIP] Chromium browser already present in Playwright cache."
else
    echo "[INFO] Downloading Chromium browser for Playwright (~150MB)..."
    "$PYTHON" -m playwright install chromium
    PW_BROWSER_INSTALLED=1
fi

echo "playwright_pkg=$PW_PKG_INSTALLED" >> "$MANIFEST"
echo "playwright_browser=$PW_BROWSER_INSTALLED" >> "$MANIFEST"

echo ""

# =========================================================================
# Step 4: TTS runtime data (NLTK corpora / jieba / g2p_en)
# =========================================================================
echo "-- Step 4: TTS runtime data (NLTK / jieba / g2p_en) --------------------"

TTS_SENTINEL="$REPO_ROOT/vendor/nltk_data/.predeploy.ok"
TTS_DEPLOYED=0
if [ -f "$TTS_SENTINEL" ]; then
    echo "[SKIP] TTS runtime data already deployed."
else
    TTS_SCRIPT="$REPO_ROOT/scripts/setup/predeploy_tts_runtime.py"
    if [ -f "$TTS_SCRIPT" ]; then
        echo "[INFO] Deploying TTS runtime data (NLTK corpora, jieba dict, g2p_en)..."
        "$PYTHON" -W ignore::SyntaxWarning "$TTS_SCRIPT" || echo "[WARN] TTS pre-deploy had issues (non-fatal)."
        TTS_DEPLOYED=1
    else
        echo "[WARN] TTS pre-deploy script not found: $TTS_SCRIPT"
    fi
fi

echo "tts_data=$TTS_DEPLOYED" >> "$MANIFEST"
echo ""

# =========================================================================
# Done
# =========================================================================
echo "============================================================"
echo "  Setup complete ✓"
echo ""
echo "  Installed components (manifest: data/setup_mac_manifest.txt):"
cat "$MANIFEST" | while IFS= read -r line; do
    echo "    $line"
done
echo ""
echo "  Next: run Build.command to compile the frontend, then"
echo "        double-click Start.command to launch the server."
echo "  To undo: run Uninstall.command"
echo "============================================================"
echo ""
exec </dev/tty
read -n 1 -s -r -p "按任意键关闭窗口 ..."
echo
