#!/bin/bash
# Start.command — macOS launcher for QAIModelBuilder
#
# Double-click this file in Finder to launch the QAIModelBuilder server.
# It mirrors the Windows Start.bat:
#   1. Resolves the repo + app directory
#   2. Creates a local .venv on first run and installs the package
#      (pip install -e .) — subsequent runs reuse it
#   3. Sets PYTHONPATH for the src layout
#   4. Cleans up any stale endpoint from a previous run
#   5. Opens the browser automatically once the server is ready
#   6. Runs the supervisor in the foreground (Ctrl+C / close window stops it)
#
# Usage:
#   Double-click Start.command
#   Start.command --reload      # hot-reload (development)
#
# NOTE: macOS has no Setup.bat equivalent. The first launch creates the venv
# and installs from PyPI (the win_arm64 wheels in vendor/whl are Windows-only).
# Requires Python >= 3.12 (managed 3.13.12 works).

set -e

# --- 1. Resolve directories ------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/tools/qaimodelbuilder"

if [ ! -d "$APP_DIR" ]; then
    echo "[ERROR] Cannot find app directory: $APP_DIR"
    exit 1
fi

cd "$APP_DIR"

# --- 2. Resolve Python (prefer .venv; fall back to a system >=3.12 for bootstrap) ---
VENV_DIR="$APP_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python3"

PYTHON=""
if [ -x "$VENV_PY" ]; then
    # .venv already built — use its interpreter directly (no system-python check).
    PYTHON="$VENV_PY"
else
    # No .venv yet: find a Python >= 3.12 to create it with (managed 3.13.12 preferred).
    for cand in \
        "/Users/zhuxiaodong/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
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
    if [ -z "$PYTHON" ]; then
        echo "[ERROR] Python >= 3.12 required to create .venv (found $(python3 --version 2>&1 || echo unknown)). Install Python 3.12+ first."
        exit 1
    fi
    echo "[INFO] Using $PYTHON to bootstrap the venv (first run only)..."
fi

# --- 3. venv bootstrap (first run only) ------------------------------------
if [ ! -x "$VENV_PY" ]; then
    echo "[INFO] No .venv found at $VENV_DIR — creating one with $PYTHON ..."
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    echo "[INFO] Installing QAIModelBuilder (pip install -e .) — this may take a few minutes..."
    "$VENV_DIR/bin/pip" install -e .
fi

# Always run with the venv interpreter from here on.
PYTHON="$VENV_PY"

# --- 4. Environment ---------------------------------------------------------
export PYTHONPATH="$APP_DIR/src:$APP_DIR"

# --- 5. Stale endpoint cleanup ---------------------------------------------
echo "[INFO] Cleaning up any stale endpoint from a previous run..."
"$PYTHON" -m apps.cli._endpoint_helper cleanup-stale >/dev/null 2>&1 || true

# --- 6. Resolve display addresses + free the requested port (macOS) --------
PORT=4099
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

# Free a TCP port on macOS before launch. The project's Python cleanup-stale
# only sweeps ports on Windows, so on macOS a stale listener (e.g. a previous
# QAI server still bound to $PORT) would otherwise force the supervisor onto a
# fallback port. Mirrors the project's "a fresh start always wins" policy.
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

# --- 7. Launch server (background) + show clickable WebUI URL --------------
echo ""
echo "  +------------------------------------------+"
echo "  |   QAI ModelBuilder  -  Starting...       |"
echo "  +------------------------------------------+"
echo ""
echo "[INFO] Launching server on port $PORT (bind $HOST) ..."
echo "[INFO] Keep this window open. Close it (or press Ctrl+C) to stop the server."

# Graceful shutdown: kill the supervisor + its API child when the window
# closes or Ctrl+C is pressed.
cleanup() {
    echo ""
    echo "[INFO] Shutting down QAI ModelBuilder..."
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
