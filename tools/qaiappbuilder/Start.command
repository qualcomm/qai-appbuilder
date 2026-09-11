#!/bin/bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# =============================================================================
# Start.command — macOS launcher for QAI AppBuilder
#
# STARTER SCRIPT. Checks for existing Python environment, bootstraps if missing.
#   1. Resolves the repo root directory
#   2. Checks for existing venv (created by Setup.command)
#   3. If no venv found, bootstraps minimal Python environment (fallback)
#   4. Sets PYTHONPATH for the src layout
#   5. Cleans up any stale endpoint from a previous run
#   6. Launches the server in foreground (Ctrl+C / close window stops it)
#
# Usage:
#   Double-click Start.command
#   Start.command --reload      # hot-reload (development)
#
# NOTE: For full setup (Playwright, TTS, voice models), run Setup.command first.
# NOTE: The WebUI needs frontend/dist to exist — run Build.command once before
#       the first launch (and again after changing frontend source).
# =============================================================================

set -e

# --- 1. Resolve directories ------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
    echo "[ERROR] Cannot find pyproject.toml in: $REPO_ROOT"
    exit 1
fi

cd "$REPO_ROOT"

# --- 2. Check for existing venv or bootstrap fallback ----------------------
VENV_DIR="$REPO_ROOT/envs/venv"
VENV_PY="$VENV_DIR/bin/python3"

if [ -x "$VENV_PY" ]; then
    echo "[INFO] Using existing Python venv: $VENV_DIR"
else
    echo "[INFO] No venv found. Bootstrapping minimal Python environment..."
    echo "[INFO] (For full setup including Playwright/TTS/voice, run Setup.command)"
    echo ""

    PYTHON=""

    # Strategy A: conda
    if command -v conda >/dev/null 2>&1; then
        CONDA_ENV_NAME="qaiappbuilder"
        CONDA_BASE="$(conda info --base 2>/dev/null)"
        CONDA_PY="$CONDA_BASE/envs/$CONDA_ENV_NAME/bin/python3"
        if [ -x "$CONDA_PY" ]; then
            PYTHON="$CONDA_PY"
            echo "[INFO] Using existing conda environment: $CONDA_ENV_NAME"
        else
            echo "[INFO] conda found — creating environment '$CONDA_ENV_NAME' with Python 3.12..."
            conda create -n "$CONDA_ENV_NAME" python=3.12 -y
            PYTHON="$CONDA_PY"
        fi
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
        exit 1
    fi

    echo "[INFO] Creating venv with $PYTHON ..."
    mkdir -p "$REPO_ROOT/envs"
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    echo "[INFO] Installing QAI AppBuilder (pip install -e .)..."
    "$VENV_DIR/bin/pip" install -e .
    echo "[OK]   Minimal environment ready."
    echo ""
fi

PYTHON="$VENV_PY"

# --- 3. Environment ---------------------------------------------------------
export PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT"

# --- 4. Stale endpoint cleanup ---------------------------------------------
echo "[INFO] Cleaning up any stale endpoint from a previous run..."
"$PYTHON" -m apps.cli._endpoint_helper cleanup-stale >/dev/null 2>&1 || true

# --- 5. Resolve display addresses + free the requested port (macOS) --------
# Read backend port from factory/config/ports.json (single source of truth,
# same as Start.bat). Falls back to 8989 if the file is missing.
PORTS_JSON="$REPO_ROOT/factory/config/ports.json"
if [ -f "$PORTS_JSON" ]; then
    PORT="$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['backend'])" "$PORTS_JSON" 2>/dev/null || true)"
fi
PORT="${PORT:-8989}"
HOST="0.0.0.0"   # bind on all interfaces so the LAN IP link is actually reachable

# Best-effort LAN IPv4 (en0=Wi-Fi, en1/eth0=wired); falls back to first non-loopback.
get_lan_ip() {
    local ip=""
    for iface in en0 en1 en2 eth0; do
        ip="$(ipconfig getifaddr "$iface" 2>/dev/null)"
        [ -n "$ip" ] && break
    done
    if [ -z "$ip" ]; then
        ip="$(/sbin/ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print $2; exit}')"
    fi
    echo "$ip"
}
LAN_IP="$(get_lan_ip)"

# Free a TCP port on macOS before launch.
free_port_mac() {
    local p="$1" pid
    pid="$(/usr/sbin/lsof -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null | head -1)"
    if [ -n "$pid" ]; then
        echo "[INFO] Port $p is held by pid $pid — stopping it for a clean start..."
        kill "$pid" 2>/dev/null
        local i=0
        while [ "$i" -lt 15 ]; do
            /usr/sbin/lsof -tiTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 || break
            sleep 0.3
            i=$((i + 1))
        done
    fi
}

# --- 6. Launch server (background) + show clickable WebUI URL --------------
echo ""
echo "  +------------------------------------------+"
echo "  |   QAI AppBuilder  -  Starting...         |"
echo "  +------------------------------------------+"
echo ""
echo "[INFO] Launching server on port $PORT (bind $HOST) ..."
echo "[INFO] Keep this window open. Close it (or press Ctrl+C) to stop the server."
echo "[INFO] Browser will auto-open once the server is ready..."

# Graceful shutdown: kill the supervisor + its API child when the window
# closes or Ctrl+C is pressed.
cleanup() {
    echo ""
    echo "[INFO] Shutting down QAI AppBuilder..."
    [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
    pkill -f "apps.cli.serve" 2>/dev/null
    pkill -f "apps.api" 2>/dev/null
}
trap cleanup EXIT INT TERM HUP

# Ensure the requested port is free, then start.
free_port_mac "$PORT"

"$PYTHON" -m apps.cli.serve --host "$HOST" --port "$PORT" "$@" &
SERVER_PID=$!

# Block until the runtime endpoint file appears; capture the real URL so we
# display the ACTUAL port (the supervisor may auto-pick a fallback port).
echo "[INFO] Waiting for WebUI to be ready..."
EP_URL="$("$PYTHON" -m apps.cli._endpoint_helper print-url --timeout 90 2>/dev/null || true)"
if [ -n "$EP_URL" ]; then
    ACT_PORT="$(printf '%s' "$EP_URL" | sed -E 's#^[^:]+://[^:/]+:([0-9]+).*$#\1#' || true)"
    [ -z "$ACT_PORT" ] && ACT_PORT="$PORT"
    echo ""
    echo "============================================================"
    echo "  WebUI 已启动 ✓"
    echo ""
    if [ -n "$LAN_IP" ]; then
        echo "  局域网访问:  http://$LAN_IP:$ACT_PORT"
    else
        echo "  (未检测到局域网 IP，仅本机可访问)"
    fi
    echo "  本机访问:    http://127.0.0.1:$ACT_PORT"
    echo ""
    echo "  ↑ 以上链接可直接点击，在浏览器中打开使用"
    echo "============================================================"
    # Auto-open browser (same as Start.bat wait-and-open)
    OPEN_URL="http://127.0.0.1:$ACT_PORT"
    if command -v open >/dev/null 2>&1; then
        open "$OPEN_URL" 2>/dev/null &
    fi
else
    echo ""
    echo "[WARN] 服务器在 90s 内未就绪，请查看上方日志排查问题。"
fi

# Keep the window open while the server runs.
wait "$SERVER_PID"

# Server ended — let the user read any final output before the window closes.
exec </dev/tty
read -n 1 -s -r -p "服务器已停止。按任意键关闭窗口 ..."
echo
