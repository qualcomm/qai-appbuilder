# QAI AppBuilder Install SOP (Reusable)

## Purpose
Install `qai_appbuilder` from PyPI on an Ubuntu 24.04 target
device/environment with QAIRT, then verify the Python package and DSP backend
readiness.

This SOP assumes QAIRT was installed with `QAIRT_SETUP_SOP_linux.md` using
Python 3.12.

For QAIRT 2.48 on the ARM64 remote device, the PyPI wheel installs directly:

```text
qai-appbuilder==2.48.40
```

## Required Inputs
1. `target_device`: `local` or remote host/IP
2. `aienv_sh_path`: absolute path to `aienv.sh`
3. `venv_path`: absolute path to Python virtualenv, usually
   `$QAIRT_SDK_ROOT/pyqairt`

Example from the remote QAIRT 2.48 setup:
- `target_device=10.91.199.207`
- `aienv_sh_path=/home/ubuntu/aienv.sh`
- `venv_path=/home/ubuntu/qairt/2.48.40.260702/qairt/2.48.40.260702/pyqairt`

## 1. Precheck and Environment

```bash
set -euo pipefail

# 1. SoC/model detect
cat /sys/devices/soc0/soc_id
cat /sys/devices/soc0/machine
tr -d '\0' </proc/device-tree/model; echo

# 2. Validate aienv.sh exists
AIENV_SH_PATH="<ABS_PATH_TO_AIENV_SH>"
if [ ! -f "$AIENV_SH_PATH" ]; then
  echo "aienv.sh not found at: $AIENV_SH_PATH"
  echo "Run SOP: QAIRT_SETUP_SOP_linux.md"
  exit 1
fi

# 3. Source QAIRT env and activate the QAIRT venv
source "$AIENV_SH_PATH"
source "${QAIRT_SDK_ROOT}/pyqairt/bin/activate"

python --version
python -m pip --version
echo "QAIRT_SDK_ROOT=${QAIRT_SDK_ROOT}"
echo "QAIRT_DEVICE_BIN=${QAIRT_DEVICE_BIN}"

# QAIRT host/target build dependency check. This must pass before install or
# backend validation; use sudo for the mutating repair command if needed.
"${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh" --dry-run
```

Expected:
- Python is `3.12.x`
- `pip` path is under `$QAIRT_SDK_ROOT/pyqairt`
- `QAIRT_DEVICE_BIN` points to an existing directory under
  `$QAIRT_SDK_ROOT/bin`
- The Linux dependency dry-run reports no missing required packages. If it
  reports missing packages, run:

  ```bash
  sudo "${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh"
  ```

  and confirm the output ends with `Done!!` before continuing.

## 2. Install QAI AppBuilder with pip

Use PyPI as the default install path:

```bash
python -m pip install qai-appbuilder
```

To force the version that matches QAIRT 2.48:

```bash
python -m pip install qai-appbuilder==2.48.40
```

Do not install into system Python. Always source `aienv.sh` and use the QAIRT
virtual environment first.

## 3. Verify Python Package

```bash
python - <<'PY'
import importlib.metadata as md
import qai_appbuilder

print("qai_appbuilder import OK")
print("qai_appbuilder version:", md.version("qai_appbuilder"))
PY

python -m pip show qai_appbuilder | egrep '^(Name|Version|Location|Requires):'
```

Expected:
- Import passes
- Version is reported by `importlib.metadata`
- Install location is inside the QAIRT venv

## 4. Detect DSP Architecture

```bash
VALIDATOR="${QAIRT_SDK_ROOT}/bin/${QAIRT_DEVICE_BIN}/qnn-platform-validator"
"$VALIDATOR" --backend dsp --coreVersion --testBackend --debug
```

Parse:
- `Core Version of the backend DSP: Hexagon Architecture VXX`
- Set `DSP_ARCH=<XX>` (example: `V73` -> `DSP_ARCH=73`)

Set `PRODUCT_SOC` from board name/model:
- Example: `QCS9075` -> `PRODUCT_SOC=9075`

## 5. Export Runtime Variables

```bash
export PRODUCT_SOC="<detected_soc>"
export DSP_ARCH="<detected_dsp_arch_number>"
export ADSP_LIBRARY_PATH="$QAIRT_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"
export LD_LIBRARY_PATH="$QAIRT_SDK_ROOT/lib/${QAIRT_DEVICE_BIN}:${LD_LIBRARY_PATH:-}"
```

## 6. Persist Runtime Variables in `aienv.sh`

Add or update these lines in `aienv.sh` after `QAIRT_SDK_ROOT` and
`QAIRT_DEVICE_BIN` are set:

```bash
export PRODUCT_SOC=<detected_soc>
export DSP_ARCH=<detected_dsp_arch_number>
export ADSP_LIBRARY_PATH=$QAIRT_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned
export LD_LIBRARY_PATH=$QAIRT_SDK_ROOT/lib/${QAIRT_DEVICE_BIN}:$LD_LIBRARY_PATH
```

## 7. Final Setup Checks

Run these checks after install:

```bash
source "<ABS_PATH_TO_AIENV_SH>"
source "${QAIRT_SDK_ROOT}/pyqairt/bin/activate"

python -m pip show qai_appbuilder | egrep '^(Name|Version|Location|Requires):'
python -c "import qai_appbuilder; print('qai_appbuilder import OK')"

"${QAIRT_SDK_ROOT}/bin/envcheck" -c
"${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh" --dry-run
python "${QAIRT_SDK_ROOT}/bin/check-python-dependency" --dry-run

"${QAIRT_SDK_ROOT}/bin/${QAIRT_DEVICE_BIN}/qnn-platform-validator" \
  --backend dsp --coreVersion --testBackend --debug
```

Confirm:
- `qai_appbuilder` import passes
- `pip show` reports the package inside `$QAIRT_SDK_ROOT/pyqairt`
- `envcheck -c` passes
- `check-linux-dependency.sh --dry-run` reports no missing required packages
- `check-python-dependency --dry-run` passes
- Validator loads the matching DSP backend stub for the detected DSP arch

## Run Record (remote QAIRT 2.48)

- Device: remote ARM64 Ubuntu 24.04
- QAIRT version: `2.48.40.260702`
- `QAIRT_SDK_ROOT`: `/home/ubuntu/qairt/2.48.40.260702/qairt/2.48.40.260702`
- Venv: `/home/ubuntu/qairt/2.48.40.260702/qairt/2.48.40.260702/pyqairt`
- Bootstrap: `/home/ubuntu/aienv.sh`
- Installed package: `qai_appbuilder==2.48.40`
- Install method: `python -m pip install qai-appbuilder`
- Import status: passed

## Deprecated Source Build Fallback

Source builds are deprecated for the normal setup path. Use this only when a
matching PyPI wheel is unavailable or a local source patch must be tested.

Source repo:
- `https://github.com/qualcomm/qai-appbuilder.git`

```bash
set -euo pipefail

source "<ABS_PATH_TO_AIENV_SH>"
source "${QAIRT_SDK_ROOT}/pyqairt/bin/activate"

INSTALL_BASE="<ABS_INSTALL_PATH>"
REPO_DIR="$INSTALL_BASE/qai-appbuilder"

mkdir -p "$INSTALL_BASE"

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --recurse-submodules https://github.com/qualcomm/qai-appbuilder.git "$REPO_DIR"
fi

git -C "$REPO_DIR" pull --recurse-submodules
git -C "$REPO_DIR" submodule update --init --recursive

cd "$REPO_DIR"
python setup.py bdist_wheel
WHEEL="$(ls -1t dist/*.whl | head -n 1)"
python -m pip install "$WHEEL"
echo "Installed wheel: $WHEEL"
```

## Troubleshooting

- `aienv.sh missing`: run QAIRT bootstrap SOP first
  (`QAIRT_SETUP_SOP_linux.md`).
- Package installs into system Python:
  - source `aienv.sh`
  - activate `$QAIRT_SDK_ROOT/pyqairt/bin/activate`
  - rerun `python -m pip --version`
- Import fails after install:
  - run `python -m pip show qai_appbuilder`
  - verify `Location` is under `$QAIRT_SDK_ROOT/pyqairt`
- Validator fails to load DSP libs:
  - re-check `DSP_ARCH`
  - verify `ADSP_LIBRARY_PATH` points to `hexagon-v${DSP_ARCH}/unsigned`
  - verify `QAIRT_DEVICE_BIN` points to an existing QAIRT bin directory
- Source clone/build is slow:
  - prefer the PyPI wheel unless a source build is explicitly required
