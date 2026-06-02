# QAIRT Setup SOP (Reusable)

## Purpose
Install and configure Qualcomm AI Runtime (QAIRT) on Ubuntu 24.04 with a Python 3.10 virtual environment and reusable shell bootstrap.

## Mandatory Inputs (Ask Before Install)
Collect all 3 before running download/install commands:
1. `QAIRT version` (`2.42.0.251225` or `2.45.0.260326`)
2. `target` (`local` or `remote`)
3. `install path` (absolute path)

Do not assume defaults if any are missing.

## Supported QAIRT Versions
- `2.42.0.251225`
- `2.45.0.260326`

## Prerequisites
- Ubuntu 24.04
- `curl`, `wget`, `unzip`
- `python3.10`
- `sudo` access recommended for system dependency installer

## 1) Resolve version, target, install path

```bash
# Example input values
QAIRT_VERSION="2.45.0.260326"
INSTALL_TARGET="local"          # local or remote
TARGET_ROOT="/home/ubuntu/Test/qairt"
REMOTE_HOST=""                  # set only if INSTALL_TARGET=remote

case "$QAIRT_VERSION" in
  "2.42.0.251225")
    QAIRT_URL="https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.42.0.251225/v2.42.0.251225.zip"
    ;;
  "2.45.0.260326")
    QAIRT_URL="https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.45.0.260326/v2.45.0.260326.zip"
    ;;
  *)
    echo "Unsupported QAIRT_VERSION: $QAIRT_VERSION"
    exit 1
    ;;
esac
```

## 2) Download and extract QAIRT

```bash
mkdir -p "$TARGET_ROOT"
cd "$TARGET_ROOT"

if [ "$INSTALL_TARGET" = "local" ]; then
  wget -c "$QAIRT_URL"
  unzip -o "v${QAIRT_VERSION}.zip" -d "${QAIRT_VERSION}"
  export QAIRT_SDK_ROOT="${TARGET_ROOT}/${QAIRT_VERSION}/qairt/${QAIRT_VERSION}"
else
  TMP_ZIP="/tmp/v${QAIRT_VERSION}.zip"
  wget -c "$QAIRT_URL" -O "$TMP_ZIP"
  scp "$TMP_ZIP" "${REMOTE_HOST}:${TMP_ZIP}"
  ssh "$REMOTE_HOST" "mkdir -p '$TARGET_ROOT' && unzip -o '$TMP_ZIP' -d '$TARGET_ROOT/${QAIRT_VERSION}'"
  export QAIRT_SDK_ROOT="${TARGET_ROOT}/${QAIRT_VERSION}/qairt/${QAIRT_VERSION}"
fi

echo "QAIRT_SDK_ROOT=$QAIRT_SDK_ROOT"
```

## 3) Patch `check-linux-dependency.sh` for Ubuntu 24.04

File:
- `${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh`

Required behavior:
- In `setup_op_package_dependencies()`, use `libncurses6` on Ubuntu 24.04
- Allow 24.04 in supported Ubuntu warning check

Reference logic:

```bash
version=$(lsb_release -rs)
if [ "$version" == "24.04" ] ; then
  pkgs_to_check=('libncurses6')
fi

if [ "$version" != "20.04" ] && [ "$version" != "22.04" ] && [ "$version" != "24.04" ]; then
  echo "Warning: Ubuntu Versions 20.04, 22.04 and 24.04 are supported. Detected Ubuntu version $version"
fi
```

## 4) Patch `check-python-dependency` for Ubuntu 24.04

File:
- `${QAIRT_SDK_ROOT}/bin/check-python-dependency`

Add Ubuntu 24.04 mapping to Python 3.10:

```python
ubuntu_to_python_version = {
    "22.04": version310.version,
    "24.04": version310.version,
    "20.04": version38.version
}
```

## 5) Create and activate Python venv

```bash
python3.10 -m venv "${TARGET_ROOT}/pyqairt"
source "${TARGET_ROOT}/pyqairt/bin/activate"
python3.10 -m pip install --upgrade pip wheel
```

If `pkg_resources` issues occur, pin setuptools:

```bash
python3.10 -m pip install --force-reinstall "setuptools==80.9.0"
```

## 6) Run QAIRT dependency checks

```bash
# System deps (needs sudo)
sudo "${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh"

# If passwordless sudo is unavailable, run dry-run (non-blocking check)
"${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh" -n

# Guard against unbound vars in envsetup
export PYTHONPATH="${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
source "${QAIRT_SDK_ROOT}/bin/envsetup.sh"

python3.10 "${QAIRT_SDK_ROOT}/bin/check-python-dependency"
```

## 7) Install extra ML packages in venv

```bash
python3.10 -m pip install \
  torch==2.4.1 \
  torchvision==0.19.1 \
  onnx==1.19.1 \
  onnxruntime==1.23.2 \
  onnxsim==0.4.36
```

## 8) Create reusable shell bootstrap

Create `/home/ubuntu/aienv.sh`:

```bash
#!/usr/bin/env bash
# QAIRT environment bootstrap
export QAIRT_SDK_ROOT="/home/ubuntu/Test/qairt/2.45.0.260326/qairt/2.45.0.260326"
source /home/ubuntu/Test/qairt/pyqairt/bin/activate
export PYTHONPATH="${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
source "${QAIRT_SDK_ROOT}/bin/envsetup.sh"

# Select QAIRT device/toolchain bin path based on host environment.
if [ "$(uname -m)" = "x86_64" ]; then
  QAIRT_DEVICE_BIN="x86_64-linux-clang"
elif [ "$(uname -m)" = "aarch64" ]; then
  if [ -d "${QAIRT_SDK_ROOT}/bin/aarch64-ubuntu-gcc9.4" ]; then
    QAIRT_DEVICE_BIN="aarch64-ubuntu-gcc9.4"
  elif [ -d "${QAIRT_SDK_ROOT}/bin/aarch64-oe-linux-gcc11.2" ]; then
    QAIRT_DEVICE_BIN="aarch64-oe-linux-gcc11.2"
  elif [ -d "${QAIRT_SDK_ROOT}/bin/aarch64-oe-linux-gcc9.3" ]; then
    QAIRT_DEVICE_BIN="aarch64-oe-linux-gcc9.3"
  else
    QAIRT_DEVICE_BIN="aarch64-oe-linux-gcc8.2"
  fi
else
  QAIRT_DEVICE_BIN="x86_64-linux-clang"
fi

export QAIRT_DEVICE_BIN
export PATH="${QAIRT_SDK_ROOT}/bin/${QAIRT_DEVICE_BIN}:${PATH}"
```

Then:

```bash
chmod +x /home/ubuntu/aienv.sh
source /home/ubuntu/aienv.sh
```

## 9) Validation checklist

```bash
echo "$QAIRT_SDK_ROOT"
python3.10 -V
python3.10 -m pip show torch torchvision onnx onnxruntime onnxsim
echo "$QAIRT_DEVICE_BIN"
echo "$PATH" | cut -d: -f1
```

Expected:
- `QAIRT_SDK_ROOT` points to selected install/version
- Python is `3.10.x` from venv
- Required ML package versions are installed
- `QAIRT_DEVICE_BIN` resolves by architecture
- PATH head is `${QAIRT_SDK_ROOT}/bin/${QAIRT_DEVICE_BIN}`

## Notes from this run
- Version used: `2.45.0.260326`
- Local install path used: `/home/ubuntu/Test/qairt`
- On this host, `QAIRT_DEVICE_BIN` resolved to `aarch64-ubuntu-gcc9.4`
- `envsetup.sh` required both `PYTHONPATH` and `LD_LIBRARY_PATH` guards to avoid unbound-variable exits
