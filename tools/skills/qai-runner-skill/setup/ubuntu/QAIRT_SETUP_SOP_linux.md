# QAIRT Setup SOP (Reusable)

## Purpose
Install and configure Qualcomm AI Runtime (QAIRT) on Ubuntu 24.04 with a
Python 3.12 virtual environment and reusable shell bootstrap.

This SOP follows Qualcomm's current Linux setup guidance:
- Reference:
  https://docs.qualcomm.com/nav/home/linux_setup.html?product=1601111740009302
- Supported x86 Linux host: Ubuntu 24.04 with Python 3.12.
- Supported ARM Linux host/target: Ubuntu 24.04 with Python 3.12.
- Ubuntu 20.04 is no longer supported.
- On ARM Linux targets, PyTorch/TensorFlow/TFLite model conversion is not
  supported. Use ONNX conversion.

Do not edit QAIRT SDK files for Ubuntu 24.04 support. Current QAIRT releases
document Ubuntu 24.04 support directly; if a vendor dependency script fails,
capture the SDK version and full error output.

## Mandatory Inputs (Ask Before Install)
Collect all inputs before running download/install commands:
1. `QAIRT version` (for example, `2.50.0.260828`)
2. `QAIRT zip or download URL`
3. `target` (`local` or `remote`)
4. `install path` (absolute path)
5. `remote host` (only when target is `remote`)

Do not assume defaults if any are missing.

## QAIRT Versions from `qai-appbuilder` Releases
Before choosing a QAIRT version, first check the Qualcomm `qai-appbuilder`
GitHub releases page for the newest release that includes a `QAIRT_v*.zip`
asset. Use the SDK version from that asset name.

Release page:
- https://github.com/qualcomm/qai-appbuilder/releases

The list below is a snapshot from the release page. Existing values in this file
may become old as Qualcomm publishes new releases.

Known release assets:
- `2.50.0.260828` from `v2.50.0`
- `2.49.0.260730` from `v2.49.0`
- `2.48.40.260702` from `v2.48.40`

Prefer the newest available QAIRT asset unless the target project requires an
older SDK for compatibility.

## Prerequisites
- Ubuntu 24.04
- `curl`, `wget`, `unzip`
- `python3.12`, `python3.12-venv`, `python3-distutils`, `libpython3.12`
- `sudo` access is required for the system dependency installer

Install the base packages:

```bash
sudo apt-get update
sudo apt-get install -y curl wget unzip python3.12 python3.12-venv python3-distutils libpython3.12
```

## 1) Resolve version, package, target, install path

```bash
# Example input values
QAIRT_VERSION="2.50.0.260828"
QAIRT_ZIP_OR_URL="<ABS_PATH_OR_URL_TO_QAIRT_ZIP>"
INSTALL_TARGET="local"          # local or remote
TARGET_ROOT="/home/ubuntu/Test/qairt"
REMOTE_HOST=""                  # set only if INSTALL_TARGET=remote
```

## 2) Download and extract QAIRT

```bash
set -euo pipefail

mkdir -p "$TARGET_ROOT"

if [ "$INSTALL_TARGET" = "local" ]; then
  cd "$TARGET_ROOT"

  if [ -f "$QAIRT_ZIP_OR_URL" ]; then
    ZIP_NAME="$(basename "$QAIRT_ZIP_OR_URL")"
    cp "$QAIRT_ZIP_OR_URL" "$TARGET_ROOT/$ZIP_NAME"
  else
    ZIP_NAME="v${QAIRT_VERSION}.zip"
    wget -c "$QAIRT_ZIP_OR_URL" -O "$ZIP_NAME"
  fi

  unzip -o "$ZIP_NAME" -d "${QAIRT_VERSION}"
  export QAIRT_SDK_ROOT="${TARGET_ROOT}/${QAIRT_VERSION}/qairt/${QAIRT_VERSION}"
else
  TMP_ZIP="/tmp/v${QAIRT_VERSION}.zip"

  if [ -f "$QAIRT_ZIP_OR_URL" ]; then
    scp "$QAIRT_ZIP_OR_URL" "${REMOTE_HOST}:${TMP_ZIP}"
  else
    wget -c "$QAIRT_ZIP_OR_URL" -O "$TMP_ZIP"
    scp "$TMP_ZIP" "${REMOTE_HOST}:${TMP_ZIP}"
  fi

  ssh "$REMOTE_HOST" "mkdir -p '$TARGET_ROOT' && unzip -o '$TMP_ZIP' -d '$TARGET_ROOT/${QAIRT_VERSION}'"
  export QAIRT_SDK_ROOT="${TARGET_ROOT}/${QAIRT_VERSION}/qairt/${QAIRT_VERSION}"
fi

echo "QAIRT_SDK_ROOT=$QAIRT_SDK_ROOT"
```

## 3) Source QAIRT environment

```bash
cd "${QAIRT_SDK_ROOT}/bin"
source ./envsetup.sh
```

This sets the session environment:
- `QAIRT_SDK_ROOT`
- `PYTHONPATH`
- `PATH`
- `LD_LIBRARY_PATH`

`QNN_SDK_ROOT` and `SNPE_ROOT` may still be set by the SDK for backward
compatibility, but new workflows should use `QAIRT_SDK_ROOT`.

## 4) Run QAIRT Linux dependency checks

`check-linux-dependency.sh` is supplied by the QAIRT SDK. Do not copy or edit
the vendor script; run the version belonging to the selected
`QAIRT_SDK_ROOT`. On Ubuntu 24.04 x86_64, the script checks the compiler and
runtime/build packages needed by QNN model-library generation, including
`make`, `clang`, `libc++-dev`, `libc++abi-dev`, `flatbuffers-compiler`,
`libflatbuffers-dev`, `rename`, and `libllvm14t64`. On aarch64 it selects the
GCC/libstdc++ toolchain instead of the x86_64 Clang/libc++ packages.

Run a non-mutating check first:

```bash
"${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh" --dry-run
```

If packages are missing, install them with the SDK script as documented by
Qualcomm. The script uses `apt` and therefore must be run with `sudo`:

```bash
sudo "${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh"
"${QAIRT_SDK_ROOT}/bin/envcheck" -c
```

Expected:
- `check-linux-dependency.sh` finishes with `Done!!`
- `envcheck -c` verifies the required toolchain

For a direct Ubuntu 24.04 repair of the common x86_64 missing packages:

```bash
sudo apt-get update
sudo apt-get install -y libc++-dev libllvm14t64
```

Then rerun the SDK script with `--dry-run` and the full check. A dry-run that
reports missing packages is not a pass; record the complete output and stop
before conversion or deployment until the full check succeeds.

## 5) Create and activate Python venv

Create the venv under the SDK root so `aienv.sh` can remain version-specific:

```bash
cd "${QAIRT_SDK_ROOT}"
python3.12 -m venv pyqairt --without-pip
source pyqairt/bin/activate
python3.12 -m ensurepip --upgrade
python3.12 -m pip install --upgrade pip wheel setuptools
```

Verify that `pip3` is inside the venv:

```bash
which pip3
python3.12 --version
```

Expected Python version: `3.12.x`.

## 6) Run QAIRT Python dependency checks

Use the SDK dependency checker as the source of truth for the default/tested
Python library versions for the selected QAIRT release. Qualcomm documents that
recommended package versions are verified to work; other versions may or may not
work.

```bash
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency"
```

To include optional packages:

```bash
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency" --with-optional
```

If the script reports a package version issue, resolve that package inside the
active venv, then rerun the dependency check. Do not assume newer is safer;
prefer the SDK-tested version unless the project has a specific reason to use a
newer package and import tests still pass.

To show the SDK's expected package versions without changing the venv:

```bash
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency" --dry-run
```

## 7) Install extra ML packages in venv

Install only the packages required for the model format you will convert.
After installing model-framework packages, rerun the SDK dependency dry-run and
import-test the packages you need. Framework installs can update shared
dependencies such as `numpy`, `packaging`, or `rich`.

For ONNX workflows:

```bash
python3.12 -m pip install \
  onnx==1.19.1 \
  onnxruntime==1.23.2 \
  onnxsim==0.6.2
```

For PyTorch workflows on supported hosts:

```bash
python3.12 -m pip install \
  torch==2.4.1 \
  torchvision==0.19.1
```

For TensorFlow/TFLite workflows on supported hosts:

```bash
python3.12 -m pip install \
  tensorflow==2.18.0 \
  tflite==2.18.0
```

On ARM Linux, use ONNX conversion. Do not assume PyTorch, TensorFlow, or TFLite
conversion support.

Verify the intended framework packages:

```bash
"${QAIRT_SDK_ROOT}/bin/envcheck" -a
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency" --dry-run
python3.12 - <<'PY'
import importlib

for name in ("numpy", "onnx", "onnxruntime", "onnxsim"):
    module = importlib.import_module(name)
    print("{} {}".format(name, getattr(module, "__version__", "")))
PY
```

## 8) Create reusable shell bootstrap

Create `/home/ubuntu/aienv.sh`:

```bash
#!/usr/bin/env bash
# QAIRT environment bootstrap
set -e

export QAIRT_SDK_ROOT="/home/ubuntu/Test/qairt/2.50.0.260828/qairt/2.50.0.260828"
source "${QAIRT_SDK_ROOT}/pyqairt/bin/activate"
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
    echo "No supported aarch64 QAIRT bin directory found under ${QAIRT_SDK_ROOT}/bin" >&2
    return 1 2>/dev/null || exit 1
  fi
else
  echo "Unsupported host architecture: $(uname -m)" >&2
  return 1 2>/dev/null || exit 1
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
source /home/ubuntu/aienv.sh

echo "$QAIRT_SDK_ROOT"
python3.12 -V
python3.12 -m pip show onnx onnxruntime onnxsim
echo "$QAIRT_DEVICE_BIN"
echo "$PATH" | cut -d: -f1
"${QAIRT_SDK_ROOT}/bin/envcheck" -c
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency" --dry-run
```

Expected:
- `QAIRT_SDK_ROOT` points to selected install/version
- Python is `3.12.x` from venv
- Required ML package versions are installed
- The SDK Linux dependency check has completed with `Done!!`
- `check-python-dependency --dry-run` exits `0`; warnings about newer package
  versions should be reviewed against import tests and the Qualcomm page's
  verified-version guidance
- `QAIRT_DEVICE_BIN` resolves by architecture
- PATH head is `${QAIRT_SDK_ROOT}/bin/${QAIRT_DEVICE_BIN}`
- `envcheck -c` passes
