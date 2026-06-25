# QAI AppBuilder Install SOP (Reusable)

## Purpose
Install `qai_appbuilder` from source (`qai-appbuilder`) on a target device/environment with QAIRT, then verify DSP backend readiness.

## Required Inputs (ask before install)
1. `target_device`: `local` or remote host/IP
2. `aienv_sh_path`: absolute path to `aienv.sh`
3. `venv_path`: absolute path to Python virtualenv
4. `install_path`: absolute base path where repo/build should live

Example from this run:
- `target_device=local`
- `aienv_sh_path=/home/ubuntu/aienv.sh`
- `venv_path=/home/ubuntu/Test/qairt/pyqairt`
- `install_path=/home/ubuntu/Test/qaiappbuilder`

## Source Repo
- `https://github.com/qualcomm/qai-appbuilder.git`

## 1) Precheck and Environment
```bash
set -e

# 1. SoC/model detect
cat /sys/devices/soc0/soc_id
cat /sys/devices/soc0/machine
tr -d '\0' </proc/device-tree/model; echo

# 2. Validate aienv.sh exists
AIENV_SH_PATH="<ABS_PATH_TO_AIENV_SH>"
if [ ! -f "$AIENV_SH_PATH" ]; then
  echo "aienv.sh not found at: $AIENV_SH_PATH"
  echo "Run SOP: QAIRT_SETUP_SOP.md"
  exit 1
fi

# 3. Source QAIRT env
source "$AIENV_SH_PATH"
source "${QAIRT_SDK_ROOT}/bin/envsetup.sh"
```

## 2) Detect DSP Architecture (Do Not Hardcode)
```bash
VALIDATOR="${QAIRT_SDK_ROOT}/bin/aarch64-ubuntu-gcc9.4/qnn-platform-validator"
"$VALIDATOR" --backend dsp --coreVersion --testBackend --debug
```

Parse:
- `Core Version of the backend DSP: Hexagon Architecture VXX`
- Set `DSP_ARCH=<XX>` (example: `V73` -> `DSP_ARCH=73`)

Set `PRODUCT_SOC` from board name/model:
- Example: `QCS9075` -> `PRODUCT_SOC=9075`

## 3) Export Runtime Variables
```bash
export PRODUCT_SOC="<detected_soc>"
export DSP_ARCH="<detected_dsp_arch_number>"
export ADSP_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}:$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
```

## 4) Clone, Build, Install
```bash
set -euo pipefail

INSTALL_BASE="<ABS_INSTALL_PATH>"
REPO_DIR="$INSTALL_BASE/qai-appbuilder"
VENV_PATH="<ABS_VENV_PATH>"

mkdir -p "$INSTALL_BASE"

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --recurse-submodules https://github.com/qualcomm/qai-appbuilder.git "$REPO_DIR"
fi

git -C "$REPO_DIR" pull --recurse-submodules
git -C "$REPO_DIR" submodule update --init --recursive

source "$VENV_PATH/bin/activate"
cd "$REPO_DIR"
python3 setup.py bdist_wheel
WHEEL="$(ls -1t dist/*.whl | head -n 1)"
pip3 install "$WHEEL"
echo "Installed wheel: $WHEEL"
```

## 5) Verification
```bash
source "<ABS_PATH_TO_AIENV_SH>"
source "<ABS_VENV_PATH>/bin/activate"

python -c "import qai_appbuilder; print('qai_appbuilder import OK')"
python -m pip show qai_appbuilder | egrep '^(Name|Version|Location):'

"${QAIRT_SDK_ROOT}/bin/aarch64-ubuntu-gcc9.4/qnn-platform-validator" \
  --backend dsp --coreVersion --testBackend --debug
```

Confirm:
- Import passes
- `pip show` returns expected package info
- Validator loads matching stub for your DSP (example: `libQnnHtpV73...` for `DSP_ARCH=73`)
- Unit test passes

## 6) Persist in `aienv.sh`
Add/update these lines in `aienv.sh`:
```bash
export PRODUCT_SOC=<detected_soc>
export DSP_ARCH=<detected_dsp_arch_number>
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
```

## Run Record (this machine)
- SoC/model: `QCS9075` (`soc_id=676`)
- DSP architecture: `V73` (`DSP_ARCH=73`)
- Installed version: `qai_appbuilder 2.45.0`
- Install location: `/home/ubuntu/Test/qairt/pyqairt/lib/python3.10/site-packages`
- Repo path: `/home/ubuntu/Test/qaiappbuilder/qai-appbuilder`

## Troubleshooting
- `aienv.sh missing`: run QAIRT bootstrap SOP first (`QAIRT_SETUP_SOP.md`).
- Validator fails to load DSP libs:
  - re-check `DSP_ARCH`
  - verify `ADSP_LIBRARY_PATH` points to `hexagon-v${DSP_ARCH}/unsigned`
- Import fails after install:
  - ensure venv is activated
  - run `python -m pip show qai_appbuilder` in same venv
- Submodule clone is slow:
  - wait for large dependencies (`MNN`, `llama.cpp`) to complete.
