#!/usr/bin/env bash
# ============================================================================
# Build script for GenieAPIService on Linux (ARM64)
#
# Prerequisites (the same environment used to build QAI AppBuilder):
#   - Ubuntu 20.04+ aarch64 (or any glibc >= 2.31 distribution)
#   - cmake >= 3.18, build-essential, git
#   - QAIRT (Qualcomm AI Runtime) SDK extracted somewhere on disk
#
# Required environment variable:
#   QNN_SDK_ROOT  ->  path to the extracted QAIRT SDK root
#                     (the directory that contains "include/", "lib/", "bin/")
#
# Optional environment variables:
#   QNN_STUB_VERSION    Default: v73   (one of: v68, v69, v73, v75, v79)
#   QNN_PLATFORM        Default: aarch64-oe-linux-gcc11.2
#   BUILD_TYPE          Default: Release
#   JOBS                Default: $(nproc)
#   USE_MNN             Default: OFF
#   USE_GGUF            Default: OFF
#   BUILD_AS_DLL        Default: OFF
#
# Usage:
#   chmod +x build_linux.sh
#   ./build_linux.sh
# ============================================================================
set -euo pipefail

# --------------------------------------------------------------------------
# Locate this script. Works whether the script is invoked directly or sourced.
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="${SCRIPT_DIR}/Service"

if [[ ! -d "${SERVICE_DIR}" ]]; then
    echo "[ERROR] Cannot find Service directory at: ${SERVICE_DIR}" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Defaults & input validation
# --------------------------------------------------------------------------
: "${BUILD_TYPE:=Release}"
: "${JOBS:=$(nproc 2>/dev/null || echo 4)}"
: "${QNN_STUB_VERSION:=v73}"
: "${QNN_PLATFORM:=aarch64-oe-linux-gcc11.2}"
: "${USE_MNN:=OFF}"
: "${USE_GGUF:=OFF}"
: "${BUILD_AS_DLL:=OFF}"

if [[ -z "${QNN_SDK_ROOT:-}" ]]; then
    echo "[ERROR] QNN_SDK_ROOT is not set." >&2
    echo "        Please export QNN_SDK_ROOT to point at your QAIRT SDK install dir." >&2
    exit 1
fi

if [[ ! -d "${QNN_SDK_ROOT}" ]]; then
    echo "[ERROR] QNN_SDK_ROOT does not exist: ${QNN_SDK_ROOT}" >&2
    exit 1
fi

# Quick sanity check: make sure the platform-specific lib dir is present
if [[ ! -d "${QNN_SDK_ROOT}/lib/${QNN_PLATFORM}" ]]; then
    echo "[WARN] ${QNN_SDK_ROOT}/lib/${QNN_PLATFORM} does not exist." >&2
    echo "       You probably need a Linux QAIRT SDK release that contains the" >&2
    echo "       '${QNN_PLATFORM}' subdirectory under lib/." >&2
fi

# --------------------------------------------------------------------------
# Show summary
# --------------------------------------------------------------------------
echo "=========================================================="
echo "GenieAPIService Linux build"
echo "----------------------------------------------------------"
echo "  Script dir       : ${SCRIPT_DIR}"
echo "  Service dir      : ${SERVICE_DIR}"
echo "  QNN_SDK_ROOT     : ${QNN_SDK_ROOT}"
echo "  QNN_PLATFORM     : ${QNN_PLATFORM}"
echo "  QNN_STUB_VERSION : ${QNN_STUB_VERSION}"
echo "  BUILD_TYPE       : ${BUILD_TYPE}"
echo "  USE_MNN          : ${USE_MNN}"
echo "  USE_GGUF         : ${USE_GGUF}"
echo "  BUILD_AS_DLL     : ${BUILD_AS_DLL}"
echo "  JOBS             : ${JOBS}"
echo "=========================================================="

export QNN_SDK_ROOT

BUILD_DIR="${SERVICE_DIR}/build_linux"

# --------------------------------------------------------------------------
# Configure & build
# --------------------------------------------------------------------------
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

cmake "${SERVICE_DIR}" \
    -DCMAKE_BUILD_TYPE="${BUILD_TYPE}" \
    -DQNN_STUB_VERSION="${QNN_STUB_VERSION}" \
    -DQNN_PLATFORM="${QNN_PLATFORM}" \
    -DUSE_MNN="${USE_MNN}" \
    -DUSE_GGUF="${USE_GGUF}" \
    -DBUILD_AS_DLL="${BUILD_AS_DLL}"

cmake --build . --parallel "${JOBS}"

echo
echo "=========================================================="
echo "[OK] Build finished."
echo "Artifacts:"
ls -1 "${SERVICE_DIR}"/GenieService_v* 2>/dev/null | head -1 | while read -r d; do
    echo "  Output dir: ${d}"
    ls -lh "${d}" || true
done
echo "=========================================================="
echo
echo "To run, set up environment first:"
echo "  export QNN_SDK_ROOT=${QNN_SDK_ROOT}"
echo "  export LD_LIBRARY_PATH=\${QNN_SDK_ROOT}/lib/${QNN_PLATFORM}:<output_dir>:\${LD_LIBRARY_PATH}"
echo "  export ADSP_LIBRARY_PATH=\${QNN_SDK_ROOT}/lib/hexagon-${QNN_STUB_VERSION}/unsigned"
echo "  cd <output_dir> && ./GenieAPIService -c config/<your_model>/config.json -l -p 8910"
echo
