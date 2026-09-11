#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# =============================================================================
# Uninstall.command — Remove everything Setup.command installed on macOS
#
# Reads data/setup_mac_manifest.txt to know exactly what was installed,
# then removes only those components. Safe to run multiple times.
#
# Does NOT remove:
#   - The project source tree itself (user's code)
#   - data/ directory contents (qai.db, logs, config — user data)
#   - conda itself (user installed it, not us)
#   - system Python (obviously)
#
# Usage:
#   Double-click Uninstall.command
#   Uninstall.command --yes        (skip confirmation prompt)
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
MANIFEST="$REPO_ROOT/data/setup_mac_manifest.txt"

echo ""
echo "  +------------------------------------------+"
echo "  |   QAI AppBuilder — macOS Uninstall       |"
echo "  +------------------------------------------+"
echo ""

# Parse arguments
AUTO_YES=0
for arg in "$@"; do
    case "$arg" in
        --yes) AUTO_YES=1 ;;
    esac
done

if [ ! -f "$MANIFEST" ]; then
    echo "[INFO] No manifest found at: $MANIFEST"
    echo "       Either Setup.command was never run, or it was already uninstalled."
    echo ""
    echo "       If you still want to clean up manually, remove these:"
    echo "         - $REPO_ROOT/envs/venv/"
    echo "         - $REPO_ROOT/vendor/nltk_data/"
    echo "         - $REPO_ROOT/models/whisper-base/"
    echo "         - $REPO_ROOT/models/zipformer-zh/"
    echo "         - $REPO_ROOT/models/melotts-zh/"
    echo "         - ~/Library/Caches/ms-playwright/ (if installed by us)"
    echo ""
    exec </dev/tty
    read -n 1 -s -r -p "按任意键关闭窗口 ..."
    echo
    exit 0
fi

echo "  Found manifest: $MANIFEST"
echo ""
echo "  The following will be REMOVED:"
echo ""

# Read manifest into variables
VENV_DIR=""
CONDA_ENV=""
REMOVE_PLAYWRIGHT_PKG=0
REMOVE_PLAYWRIGHT_BROWSER=0
REMOVE_TTS_DATA=0
VOICE_MODELS=""

while IFS='=' read -r key value; do
    # Skip empty lines and comments
    [ -z "$key" ] && continue
    case "$key" in
        venv_dir)
            VENV_DIR="$value"
            echo "    • Python venv: $VENV_DIR"
            ;;
        conda_env)
            CONDA_ENV="$value"
            echo "    • conda environment: $CONDA_ENV"
            ;;
        playwright_pkg)
            [ "$value" = "1" ] && REMOVE_PLAYWRIGHT_PKG=1 && echo "    • playwright Python package"
            ;;
        playwright_browser)
            [ "$value" = "1" ] && REMOVE_PLAYWRIGHT_BROWSER=1 && echo "    • Playwright Chromium browser"
            ;;
        tts_data)
            [ "$value" = "1" ] && REMOVE_TTS_DATA=1 && echo "    • TTS runtime data (NLTK/jieba/g2p_en)"
            ;;
        voice_models)
            if [ "$value" != "none" ] && [ -n "$value" ]; then
                VOICE_MODELS="$value"
                echo "    • Voice model weights: $value"
            fi
            ;;
    esac
done < "$MANIFEST"

echo ""
echo "  The following will NOT be removed:"
echo "    • Project source code"
echo "    • data/ directory (database, logs, config)"
echo "    • conda installation itself"
echo "    • system Python"
echo ""

if [ "$AUTO_YES" -eq 0 ]; then
    echo -n "  Proceed? [y/N] "
    read -r answer
    case "$answer" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Cancelled."; exit 0 ;;
    esac
fi

echo ""

# =========================================================================
# 1. Remove voice model weights
# =========================================================================
if [ -n "$VOICE_MODELS" ]; then
    echo "-- Removing voice model weights ----------------------------------------"
    for model_tag in $VOICE_MODELS; do
        case "$model_tag" in
            whisper_medium)
                rm -rf "$REPO_ROOT/models/whisper-base"
                echo "[OK]   Removed models/whisper-base/"
                ;;
            zipformer)
                rm -rf "$REPO_ROOT/models/zipformer-zh"
                echo "[OK]   Removed models/zipformer-zh/"
                ;;
            melotts_zh)
                rm -rf "$REPO_ROOT/models/melotts-zh"
                echo "[OK]   Removed models/melotts-zh/"
                ;;
        esac
    done
    echo ""
fi

# =========================================================================
# 2. Remove TTS runtime data
# =========================================================================
if [ "$REMOVE_TTS_DATA" -eq 1 ]; then
    echo "-- Removing TTS runtime data -------------------------------------------"
    rm -rf "$REPO_ROOT/vendor/nltk_data"
    echo "[OK]   Removed vendor/nltk_data/"
    echo ""
fi

# =========================================================================
# 3. Remove Playwright Chromium browser
# =========================================================================
if [ "$REMOVE_PLAYWRIGHT_BROWSER" -eq 1 ]; then
    echo "-- Removing Playwright Chromium browser --------------------------------"
    PW_CACHE_DIR="$HOME/Library/Caches/ms-playwright"
    if [ -d "$PW_CACHE_DIR" ]; then
        rm -rf "$PW_CACHE_DIR"
        echo "[OK]   Removed $PW_CACHE_DIR"
    else
        echo "[SKIP] Chromium cache directory not found."
    fi
    echo ""
fi


# =========================================================================
# 4. Remove playwright Python package (uninstall from venv)
# =========================================================================
if [ "$REMOVE_PLAYWRIGHT_PKG" -eq 1 ]; then
    echo "-- Removing playwright Python package ----------------------------------"
    if [ -n "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/pip" ]; then
        "$VENV_DIR/bin/pip" uninstall -y playwright 2>/dev/null || true
        echo "[OK]   Uninstalled playwright from venv."
    else
        echo "[SKIP] venv pip not found."
    fi
    echo ""
fi

# =========================================================================
# 5. Remove conda environment (if we created one)
# =========================================================================
if [ -n "$CONDA_ENV" ]; then
    echo "-- Removing conda environment ------------------------------------------"
    if command -v conda >/dev/null 2>&1; then
        conda env remove -n "$CONDA_ENV" -y 2>/dev/null || true
        echo "[OK]   Removed conda environment: $CONDA_ENV"
    else
        echo "[WARN] conda not found — cannot remove environment '$CONDA_ENV'."
        echo "       Remove it manually: conda env remove -n $CONDA_ENV"
    fi
    echo ""
fi

# =========================================================================
# 6. Remove Python venv (last, since uninstalling packages above needs it)
# =========================================================================
if [ -n "$VENV_DIR" ]; then
    echo "-- Removing Python venv ------------------------------------------------"
    if [ -d "$VENV_DIR" ]; then
        rm -rf "$VENV_DIR"
        echo "[OK]   Removed $VENV_DIR"
    else
        echo "[SKIP] venv directory not found."
    fi
    echo ""
fi

# =========================================================================
# 7. Remove manifest itself
# =========================================================================
echo "-- Cleaning up manifest ------------------------------------------------"
rm -f "$MANIFEST"
echo "[OK]   Removed $MANIFEST"
echo ""

# =========================================================================
# Done
# =========================================================================
echo "============================================================"
echo "  Uninstall complete ✓"
echo ""
echo "  The project source tree and data/ directory are intact."
echo "  To fully remove the project, delete the folder manually."
echo "============================================================"
echo ""
exec </dev/tty
read -n 1 -s -r -p "按任意键关闭窗口 ..."
echo
