#!/usr/bin/env bash
# setup.sh — Ubuntu 一键初始化（x86_64 & aarch64）
# Usage: bash setup.sh [--no-frontend]
#
# Idempotent: safe to run multiple times; only fills in what is missing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_FRONTEND=0

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --no-frontend) NO_FRONTEND=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[setup] $*"; }
warn()  { echo "[setup] WARNING: $*" >&2; }
error() { echo "[setup] ERROR: $*" >&2; exit 1; }

detect_arch() {
  case "$(uname -m)" in
    x86_64)        echo "x86_64" ;;
    aarch64|arm64) echo "aarch64" ;;
    *)             echo "unsupported" ;;
  esac
}

# Version number from "X.Y.Z ..." style output
version_major() { echo "$1" | grep -oE '[0-9]+' | head -1; }

# ---------------------------------------------------------------------------
# Step 01 — Python 3.12
# ---------------------------------------------------------------------------
info "Step 01: checking Python 3.12..."
if ! command -v python3.12 &>/dev/null; then
  error "python3.12 not found.
  Install with:
    Ubuntu 22.04/24.04: sudo apt install python3.12 python3.12-venv
    (python3.12 is in the official universe repository)"
fi
PYTHON=$(command -v python3.12)
info "  found: $($PYTHON --version)"

# ---------------------------------------------------------------------------
# Step 02 — Node.js >= 22
# ---------------------------------------------------------------------------
if [[ "$NO_FRONTEND" -eq 0 ]]; then
  info "Step 02: checking Node.js >= 22..."
  if ! command -v node &>/dev/null; then
    error "node not found.
  Install with (replace 'amd64' with 'arm64' for aarch64):
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt install -y nodejs
  Or use nvm: curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
             nvm install 22"
  fi
  NODE_MAJOR=$(version_major "$(node --version)")
  if [[ "$NODE_MAJOR" -lt 22 ]]; then
    error "Node.js >= 22 required (found $(node --version)). Please upgrade."
  fi
  info "  found: $(node --version)"

  # Step 03 — pnpm >= 9
  info "Step 03: checking pnpm >= 9..."
  if ! command -v pnpm &>/dev/null; then
    error "pnpm not found.
  Install with: npm install -g pnpm"
  fi
  PNPM_MAJOR=$(version_major "$(pnpm --version)")
  if [[ "$PNPM_MAJOR" -lt 9 ]]; then
    error "pnpm >= 9 required (found $(pnpm --version)). Run: npm install -g pnpm"
  fi
  info "  found: pnpm $(pnpm --version)"
else
  info "Step 02/03: skipped (--no-frontend)"
fi

# ---------------------------------------------------------------------------
# Step 04 — detect CPU architecture
# ---------------------------------------------------------------------------
info "Step 04: detecting CPU architecture..."
ARCH=$(detect_arch)
if [[ "$ARCH" == "unsupported" ]]; then
  warn "Unrecognised architecture: $(uname -m). Continuing; pip will decide wheel compatibility."
else
  info "  architecture: $ARCH"
fi

# ---------------------------------------------------------------------------
# Step 05 — create virtualenv (or verify existing one uses Python 3.12)
# ---------------------------------------------------------------------------
info "Step 05: creating virtual environment (envs/venv)..."
mkdir -p "$REPO_ROOT/envs"
# Migrate legacy .venv / envs/.venv → envs/venv for existing installations
if [[ -d "$REPO_ROOT/envs/.venv" && ! -d "$REPO_ROOT/envs/venv" ]]; then
  warn "Detected legacy envs/.venv — moving to envs/venv ..."
  mv "$REPO_ROOT/envs/.venv" "$REPO_ROOT/envs/venv"
  info "  migrated: envs/.venv → envs/venv"
elif [[ -d "$REPO_ROOT/.venv" && ! -d "$REPO_ROOT/envs/venv" ]]; then
  warn "Detected legacy .venv — moving to envs/venv ..."
  mv "$REPO_ROOT/.venv" "$REPO_ROOT/envs/venv"
  info "  migrated: .venv → envs/venv"
fi
VENV="$REPO_ROOT/envs/venv"
_recreate=0
if [[ -d "$VENV" ]]; then
  # Check that the existing venv was built with Python 3.12.x
  _venv_py_ver=$("$VENV/bin/python" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "unknown")
  if [[ "$_venv_py_ver" == "3.12" ]]; then
    info "  .venv already exists (Python $_venv_py_ver), skipping creation"
  else
    warn ".venv exists but uses Python $_venv_py_ver (need 3.12). Recreating..."
    rm -rf "$VENV"
    _recreate=1
  fi
else
  _recreate=1
fi
if [[ "$_recreate" -eq 1 ]]; then
  "$PYTHON" -m venv "$VENV"
  info "  .venv created at $VENV"
fi
# Activate for the rest of this script
# shellcheck source=/dev/null
source "$VENV/bin/activate"

# ---------------------------------------------------------------------------
# Step 06 — install Python dependencies
# ---------------------------------------------------------------------------
info "Step 06: installing Python dependencies (pip install -e '.[dev,e2e]')..."
# NOTE: do NOT pass --find-links vendor/whl — that directory contains
# win_arm64 wheels only and is irrelevant on Linux.
pip install --quiet --upgrade pip
pip install -e ".[dev,e2e]"
info "  Python dependencies installed"

# ---------------------------------------------------------------------------
# Step 07 — aarch64: verify no win_arm64 wheels are present
# ---------------------------------------------------------------------------
if [[ "$ARCH" == "aarch64" ]]; then
  info "Step 07: verifying no win_arm64 wheels in venv (aarch64 check)..."
  SITE_PKGS="$VENV/lib/python3.12/site-packages"
  # .dist-info/WHEEL files record the tag; a win_arm64 wheel will have
  # Tag: cp3xx-cp3xx-win_arm64 in its WHEEL metadata.
  BAD_WHEELS=$(find "$SITE_PKGS" -name "WHEEL" -exec grep -l "win_arm64" {} \; 2>/dev/null || true)
  if [[ -n "$BAD_WHEELS" ]]; then
    error "win_arm64 wheel(s) found in venv — this means a Windows-only wheel
  was installed. Remove .venv and re-run setup.sh.
  Affected packages:
$BAD_WHEELS"
  fi
  info "  no win_arm64 wheels found"
else
  info "Step 07: skipped (x86_64 — win_arm64 wheel check is aarch64-only)"
fi

# ---------------------------------------------------------------------------
# Step 08 — frontend: install dependencies
# ---------------------------------------------------------------------------
if [[ "$NO_FRONTEND" -eq 0 ]]; then
  info "Step 08: installing frontend dependencies (pnpm install)..."
  pnpm -C "$REPO_ROOT/frontend" install
  info "  frontend dependencies installed"
else
  info "Step 08: skipped (--no-frontend)"
fi

# ---------------------------------------------------------------------------
# Step 09 — frontend: build
# ---------------------------------------------------------------------------
if [[ "$NO_FRONTEND" -eq 0 ]]; then
  info "Step 09: building frontend (pnpm build)..."
  pnpm -C "$REPO_ROOT/frontend" build
  info "  frontend built → frontend/dist/"
else
  info "Step 09: skipped (--no-frontend)"
fi

# ---------------------------------------------------------------------------
# Step 10 — resolve secret backend, then initialise data/
# ---------------------------------------------------------------------------
# On Linux (headless/SSH/Docker), skip keyring entirely — GNOME Keyring may
# appear available but fail to unlock without a GUI session.  Use the
# Fernet-encrypted file backend unconditionally on Linux.
info "Step 10: resolving secret backend..."
if [[ "$(uname -s)" == "Linux" ]]; then
  _SECRET_BACKEND="file"
  info "  Linux platform: using file secret backend (Fernet-encrypted)"
else
  _SECRET_BACKEND="auto"
  info "  non-Linux: using auto (keyring preferred, file fallback)"
fi

info "Step 10: initialising data/ directory (secret-backend: $_SECRET_BACKEND)..."
PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" \
  python -m scripts.init.install \
    --factory-root "$REPO_ROOT/factory" \
    --data-root "$REPO_ROOT/data" \
    --sql-migrations "$REPO_ROOT/src/qai/platform/persistence/migrations_sql" \
    --secret-backend "$_SECRET_BACKEND" \
    --skip compile_factory \
    --apply
info "  data/ initialised (db + seeds + secret namespaces)"

# ---------------------------------------------------------------------------
# Secure the secrets directory
# ---------------------------------------------------------------------------

# Secure the secrets directory
if [[ -d "$REPO_ROOT/data/secrets" ]]; then
  chmod 700 "$REPO_ROOT/data/secrets"
  info "  data/secrets permissions set to 700"
fi

# ---------------------------------------------------------------------------
# Step 11 — generate data/config/qairt_env.json
# ---------------------------------------------------------------------------
info "Step 11: generating QAIRT environment config (setup_qairt_env.py --gen-config)..."
SETUP_QAIRT="$REPO_ROOT/scripts/setup/setup_qairt_env.py"
QAIRT_CONFIG="$REPO_ROOT/data/config/qairt_env.json"

if [[ ! -f "$SETUP_QAIRT" ]]; then
  warn "setup_qairt_env.py not found at $SETUP_QAIRT — skipping QAIRT config generation"
else
  PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" \
    python "$SETUP_QAIRT" --gen-config --root "$REPO_ROOT" \
    && info "  $QAIRT_CONFIG generated" \
    || warn "setup_qairt_env.py --gen-config failed — QAIRT model conversion may not work.
  You can re-run manually: python $SETUP_QAIRT --gen-config"
fi

# ---------------------------------------------------------------------------
# Step 11b — install QAIRT converter deps (python_x64_venv, Python 3.10)
# ---------------------------------------------------------------------------
info "Step 11b: installing QAIRT converter deps (python_x64_venv, Python 3.10)..."
if [[ ! -f "$SETUP_QAIRT" ]]; then
  warn "setup_qairt_env.py not found — skipping Step 11b"
else
  PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" \
    python "$SETUP_QAIRT" --install-python-deps --root "$REPO_ROOT" \
    && info "  python_x64_venv ready" \
    || warn "install-python-deps failed — model conversion may not work.
  Re-run: python $SETUP_QAIRT --install-python-deps --root $REPO_ROOT"
fi

# ---------------------------------------------------------------------------
# Step 11c — install inference deps (python_arm64_venv, aarch64 only)
# ---------------------------------------------------------------------------
if [[ "$ARCH" == "aarch64" ]]; then
  info "Step 11c: installing inference deps (python_arm64_venv, Python 3.12)..."
  if [[ ! -f "$SETUP_QAIRT" ]]; then
    warn "setup_qairt_env.py not found — skipping Step 11c"
  else
    PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" \
      python "$SETUP_QAIRT" --install-inference-deps --root "$REPO_ROOT" \
      && info "  python_arm64_venv ready" \
      || warn "install-inference-deps failed — NPU inference may not work.
  Re-run: python $SETUP_QAIRT --install-inference-deps --root $REPO_ROOT"
  fi
else
  info "Step 11c: skipped (python_arm64_venv is aarch64-only on Linux)"
fi

# ---------------------------------------------------------------------------
# Step 11d — QAIRT SDK Python dependencies via check-python-dependency
# ---------------------------------------------------------------------------
info "Step 11d: installing QAIRT SDK Python dependencies..."
_QAIRT_SDK_ROOT=""
_VENV_X64_PYTHON=""
if [[ -f "$QAIRT_CONFIG" ]]; then
  _QAIRT_SDK_ROOT=$(python -c \
    "import json; d=json.load(open('$QAIRT_CONFIG')); print(d.get('qairt_sdk_root',''))" \
    2>/dev/null || true)
  _VENV_X64_PYTHON=$(python -c \
    "import json; d=json.load(open('$QAIRT_CONFIG')); print(d.get('python_x64_venv','') + '/bin/python')" \
    2>/dev/null || true)
fi
_QAIRT_CHECK_DEP="$_QAIRT_SDK_ROOT/bin/check-python-dependency"
if [[ -n "$_QAIRT_SDK_ROOT" && -f "$_QAIRT_CHECK_DEP" && -x "$_VENV_X64_PYTHON" ]]; then
  "$_VENV_X64_PYTHON" "$_QAIRT_CHECK_DEP" \
    && info "  QAIRT SDK Python dependencies satisfied" \
    || warn "check-python-dependency reported issues — check output above"
elif [[ -z "$_QAIRT_SDK_ROOT" || ! -f "$_QAIRT_CHECK_DEP" ]]; then
  warn "QAIRT SDK not found ('$_QAIRT_SDK_ROOT') — skipping Step 11d.
  Install QAIRT SDK and re-run: python $SETUP_QAIRT --gen-config --root $REPO_ROOT"
else
  warn "python_x64_venv not executable ('$_VENV_X64_PYTHON') — skipping Step 11d"
fi

# ---------------------------------------------------------------------------
# Step 11e — extra Python packages (onnx / onnxruntime / onnxsim, pinned)
# ---------------------------------------------------------------------------
info "Step 11e: installing extra Python packages (onnx / onnxruntime / onnxsim)..."
if [[ -x "$_VENV_X64_PYTHON" ]]; then
  for _pkg in "onnx==1.19.1" "onnxruntime==1.23.2" "onnxsim==0.6.2"; do
    "$_VENV_X64_PYTHON" -m pip install --quiet "$_pkg" \
      && info "  $_pkg installed" \
      || warn "  failed to install $_pkg (non-fatal)"
  done
else
  warn "python_x64_venv not found — skipping Step 11e"
fi

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
info "Smoke test: importing settings..."
PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" \
  python -c "from qai.platform.config.settings import load_settings; load_settings()" \
  && info "  settings import OK" \
  || error "Settings import failed. Check the output above for details."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  QAIModelBuilder setup complete"
echo "  Architecture : $ARCH"
echo "  Python       : $($PYTHON --version)"
echo "  Venv         : $VENV (Python 3.12, main backend)"
echo "  Conv venv    : $REPO_ROOT/envs/venv_x86_64_310 (Python 3.10)"
if [[ "$ARCH" == "aarch64" ]]; then
  echo "  Infer venv   : $REPO_ROOT/envs/venv_aarch64_312 (Python 3.12)"
else
  echo "  Infer venv   : skipped (aarch64-only)"
fi
if [[ "$NO_FRONTEND" -eq 0 ]]; then
  echo "  Frontend     : frontend/dist/ (built)"
else
  echo "  Frontend     : skipped (--no-frontend)"
fi
echo ""
echo "  Next steps:"
# Backend port comes from factory/config/ports.json (backend); mirror what start.sh binds.
# Purely cosmetic here (a hint URL), so if the read fails we print the placeholder
# rather than a stale literal — no port is authored in this launcher.
_BACKEND_PORT="$(PYTHONPATH="$(pwd)/src:$(pwd)" python -c 'from qai.platform.config.ports import backend_port as b; print(b())' 2>/dev/null || echo '<backend-port>')"
echo "    1. Fill in API keys: open http://127.0.0.1:${_BACKEND_PORT} → Settings → Secrets"
echo "    2. Start the server: bash start.sh"
echo "============================================================"
