#!/usr/bin/env bash
# start.sh — Ubuntu 启动入口（reboot supervisor）
# Usage: bash start.sh [--port N]
#
# Ctrl+C gracefully terminates the supervisor and its child process.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_ROOT/envs/venv"

# ---------------------------------------------------------------------------
# Parse --port argument (pass remaining args through to serve)
# ---------------------------------------------------------------------------
# Default backend port comes from the SINGLE SOURCE OF TRUTH
# (factory/config/ports.json ``backend`` key) via the stdlib port-registry reader, so
# this launcher never drifts from the registry. This value is SSO-critical: it
# must equal the Okta-registered redirect_uri port.
#
# NO literal fallback here on purpose: the reader (ports.py) ALREADY degrades a
# missing/corrupt JSON to its built-in DEFAULTS internally, so the only way this
# command fails is a broken Python env — in which case `start.sh` can't launch
# the server anyway. Guessing a port would silently bind one Okta doesn't know
# and make every login fail; failing loudly is the correct behaviour.
PORT="$(PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" "$VENV/bin/python" -c 'from qai.platform.config.ports import backend_port as b; print(b())')" || {
  echo "[start] ERROR: could not resolve backend port from factory/config/ports.json." >&2
  echo "[start]        Is the venv set up? Run 'bash setup.sh' first." >&2
  exit 1
}
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
  echo "[start] ERROR: envs/venv not found at $VENV" >&2
  echo "[start]        Run 'bash setup.sh' first." >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"
export PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT"

echo "[start] QAIModelBuilder starting at http://127.0.0.1:$PORT"
echo "[start] Press Ctrl+C to stop."
echo ""

# ---------------------------------------------------------------------------
# Auto-open the browser once the server is stably serving (parity with
# Start.bat, which backgrounds this same helper).
# ---------------------------------------------------------------------------
# Only when a graphical session exists. That is the SAME discriminator
# settings._default_auth_enabled uses to decide whether the Okta login gate is
# on, so the two stay consistent: a desktop session gets the gate AND a browser
# to satisfy it; a headless box gets neither and this helper is not even
# started (it would just time out after 60s and print noise).
#
# The helper resolves the URL from the endpoint file and rewrites our wide
# ``--host 0.0.0.0`` bind to ``http://localhost:$PORT`` — required, not
# cosmetic: routes/auth.py registers ``http://localhost:<port>/callback`` with
# Okta, so the browser must start on the localhost origin or the session cookie
# the callback sets lands on a different origin than the user's tab.
#
# Backgrounded in a subshell rather than with ``nohup`` so it does not become a
# process-group leader: the ``exec`` below must keep receiving SIGINT/SIGTERM
# directly (see the note on it).
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  (python -m apps.cli._endpoint_helper wait-and-open --timeout 60 >/dev/null 2>&1 &)
  echo "[start] Browser will auto-open once the server is ready..."
fi

# exec replaces this shell so SIGINT/SIGTERM go directly to the Python
# supervisor (apps.cli.serve), which handles POSIX signals internally via
# start_new_session=True on the child process.
exec python -m apps.cli.serve --host 0.0.0.0  --port "$PORT" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
