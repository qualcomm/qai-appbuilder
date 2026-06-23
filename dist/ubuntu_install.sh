#!/usr/bin/env bash
set -euo pipefail

echo "[INFO] Offline installation starting..."

# DEBUG
echo "[DEBUG] Current dir: $(pwd)"
echo "[DEBUG] Files:"
ls -R

# Step 1: install fastrpc
echo "[INFO] Installing fastrpc..."
dpkg -i debs/*.deb || apt-get install -f -y

# Step 2: find wheel
WHEEL=$(find . -name "*.whl" | head -n 1)

if [[ -z "$WHEEL" ]]; then
  echo "[ERROR] No .whl found"
  exit 1
fi

echo "[INFO] Found wheel: $WHEEL"

# Step 3: install wheel（关键修复点）
echo "[INFO] Installing Python wheel..."
python3 -m pip install \
  --no-cache-dir \
  --break-system-packages \
  "$WHEEL"

# Step 4: verify
echo "[INFO] Verifying..."
python3 -c "import qai_appbuilder" || {
  echo "[ERROR] Import failed"
  exit 1
}

echo "[SUCCESS] Installation completed"