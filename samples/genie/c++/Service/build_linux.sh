#!/usr/bin/env bash
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
#
# One-click build script for GenieAPIService on Linux (aarch64).
#
# Required environment variables:
#   QNN_SDK_ROOT   Path to the Qualcomm AI Runtime (QAIRT) SDK root.
#
# Optional environment variables (with defaults):
#   QNN_STUB_VERSION   Hexagon DSP stub version(s), ';'-separated (default: v73;v81)
#   QNN_PLATFORM       QNN platform string        (default: aarch64-oe-linux-gcc11.2)
#   BUILD_TYPE         CMake build type           (default: Release)
#   JOBS               Parallel build jobs        (default: nproc)
#   BUILD_AS_DLL       Build only the GenieAPILibrary .so, excluding the exe
#                      (default: OFF, which builds exe + dll together)
#
# Usage:
#   export QNN_SDK_ROOT=/path/to/qairt/2.x.x.x
#   bash build_linux.sh
#=============================================================================

set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
: "${QNN_STUB_VERSION:=v73;v81}"
: "${QNN_PLATFORM:=aarch64-oe-linux-gcc11.2}"
: "${BUILD_TYPE:=Release}"
: "${JOBS:=$(nproc)}"
: "${BUILD_AS_DLL:=OFF}"
BUILD_DIR="${SERVICE_DIR}/build-linux"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ -z "${QNN_SDK_ROOT:-}" ]]; then
    echo "[ERROR] QNN_SDK_ROOT is not set. Please export it before running this script."
    echo "        Example: export QNN_SDK_ROOT=/opt/qcom/aistack/qairt/2.x.x.x"
    exit 1
fi

echo "============================================================"
echo " GenieAPIService — Linux build"
echo "============================================================"
echo "  QNN_SDK_ROOT     : ${QNN_SDK_ROOT}"
echo "  QNN_PLATFORM     : ${QNN_PLATFORM}"
echo "  QNN_STUB_VERSION : ${QNN_STUB_VERSION}"
echo "  BUILD_TYPE       : ${BUILD_TYPE}"
echo "  BACKENDS         : QNN/Genie only"
echo "  BUILD_AS_DLL     : ${BUILD_AS_DLL}"
echo "  JOBS             : ${JOBS}"
echo "  BUILD_DIR        : ${BUILD_DIR}"
echo "============================================================"

# ── Configure ─────────────────────────────────────────────────────────────────
cmake -S "${SERVICE_DIR}" \
      -B "${BUILD_DIR}" \
      -DCMAKE_BUILD_TYPE="${BUILD_TYPE}" \
      -DBUILD_AS_DLL="${BUILD_AS_DLL}" \
      -DQNN_STUB_VERSION="${QNN_STUB_VERSION}" \
      -DQNN_PLATFORM="${QNN_PLATFORM}"

# ── Build ─────────────────────────────────────────────────────────────────────
cmake --build "${BUILD_DIR}" --config "${BUILD_TYPE}" -j "${JOBS}"

echo ""
echo "============================================================"
echo " Build complete."
echo " Output: ${BUILD_DIR}/GenieService-linux-arm64/GenieAPIService"
echo ""
echo " Deploy/runtime reminder:"
echo "   export LD_LIBRARY_PATH=\"<deploy_dir>:\${LD_LIBRARY_PATH:-}\""
echo "   export ADSP_LIBRARY_PATH=\"<deploy_dir>\""
echo "============================================================"
