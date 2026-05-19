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
#   ./build_linux.sh                 # configure & build
#   ./build_linux.sh --clean         # remove all build artefacts and exit
#   ./build_linux.sh --rebuild       # clean first, then build from scratch
# ============================================================================
set -euo pipefail

# --------------------------------------------------------------------------
# Parse simple CLI flags
# --------------------------------------------------------------------------
DO_CLEAN=0
DO_BUILD=1
for arg in "$@"; do
    case "${arg}" in
        --clean)
            DO_CLEAN=1
            DO_BUILD=0
            ;;
        --rebuild)
            DO_CLEAN=1
            DO_BUILD=1
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: ${arg}" >&2
            exit 1
            ;;
    esac
done

# --------------------------------------------------------------------------
# Locate this script. Works whether the script is invoked directly or sourced.
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="${SCRIPT_DIR}/Service"
# The repository root sits 3 levels above (samples/genie/c++ -> repo root).
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [[ ! -d "${SERVICE_DIR}" ]]; then
    echo "[ERROR] Cannot find Service directory at: ${SERVICE_DIR}" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# check_submodules - make sure every git submodule the build relies on has
#                    actually been initialised. Empty submodule directories
#                    cause cryptic CMake / linker errors much later, so we
#                    stop early with a clear, actionable message.
# --------------------------------------------------------------------------
check_submodules()
{
    # marker_file => human-readable description shown when missing
    local -a names=(
        "External/CLI11/include/CLI/CLI.hpp|CLI11"
        "External/cpp-httplib/httplib.h|cpp-httplib"
        "External/json/single_include/nlohmann/json.hpp|nlohmann/json"
        "External/libsamplerate/CMakeLists.txt|libsamplerate"
        "External/dr_libs/dr_wav.h|dr_libs"
        "External/stb/stb_image.h|stb"
        "External/LibrosaCpp/librosa/librosa.h|LibrosaCpp"
    )
    local missing=0
    local list=""
    for entry in "${names[@]}"; do
        local file="${entry%%|*}"
        local desc="${entry##*|}"
        # Allow either CLI11/ or cli11/ for the case-insensitive submodule.
        if [[ "${desc}" == "CLI11" ]]; then
            if [[ ! -f "${SCRIPT_DIR}/${file}" \
               && ! -f "${SCRIPT_DIR}/External/cli11/include/CLI/CLI.hpp" ]]; then
                missing=$((missing+1))
                list="${list}  - ${desc}  (expected ${SCRIPT_DIR}/${file})\n"
            fi
        else
            if [[ ! -f "${SCRIPT_DIR}/${file}" ]]; then
                missing=$((missing+1))
                list="${list}  - ${desc}  (expected ${SCRIPT_DIR}/${file})\n"
            fi
        fi
    done

    if [[ ${missing} -gt 0 ]]; then
        echo "[ERROR] ${missing} required submodule(s) appear to be missing:" >&2
        printf "${list}" >&2
        echo >&2
        echo "Please initialise the git submodules first:" >&2
        echo "    cd ${REPO_ROOT}" >&2
        echo "    git submodule update --init --recursive" >&2
        echo >&2
        exit 1
    fi
}

# --------------------------------------------------------------------------
# clean_build  - remove every artefact that the previous build may have left
#                behind, both inside Service/ and in the repo-root locations
#                that ExternalProject_Add(BUILD_IN_SOURCE) writes to.
# --------------------------------------------------------------------------
clean_build()
{
    echo "[*] Cleaning previous build artefacts ..."

    # 1. The CMake build directory we use for GenieAPIService.
    rm -rf "${SERVICE_DIR}/build_linux"

    # 2. Final packaging directory (Service/GenieService_v<ver>/).
    rm -rf "${SERVICE_DIR}"/GenieService_v* 2>/dev/null || true

    # 3. ExternalProject_Add(libappbuilder) builds in-source at REPO_ROOT.
    #    Wipe its CMake cache so a fresh configure happens next time.
    rm -rf "${REPO_ROOT}/CMakeFiles" \
           "${REPO_ROOT}/CMakeCache.txt" \
           "${REPO_ROOT}/cmake_install.cmake" \
           "${REPO_ROOT}/Makefile" \
           "${REPO_ROOT}/lib"
    rm -rf "${REPO_ROOT}/src/CMakeFiles" \
           "${REPO_ROOT}/src/CMakeCache.txt" \
           "${REPO_ROOT}/src/cmake_install.cmake" \
           "${REPO_ROOT}/src/Makefile"

    # 4. ExternalProject_Add(libsamplerate / curl) also builds in-source on
    #    Windows. We CANNOT manually rm the source dirs (they belong to the
    #    submodule). Instead, ask git to wipe ALL untracked files inside each
    #    submodule and reset any tracked files that may have been modified by
    #    the configure step. This safely restores the original source tree.
    if command -v git >/dev/null 2>&1; then
        for sub in External/libsamplerate External/curl External/MNN External/llama.cpp; do
            sub_dir="${SCRIPT_DIR}/${sub}"
            # Only act on directories that are populated git submodules.
            if [[ -d "${sub_dir}/.git" || -f "${sub_dir}/.git" ]]; then
                echo "    cleaning submodule: ${sub}"
                ( cd "${sub_dir}" && git clean -fdx >/dev/null 2>&1 || true )
                ( cd "${sub_dir}" && git reset --hard >/dev/null 2>&1 || true )
            fi
        done
    else
        echo "    [WARN] git not found - skipping submodule cleanup. If the" >&2
        echo "           libsamplerate / curl / MNN / llama.cpp source trees" >&2
        echo "           contain stale build files, please run:" >&2
        echo "             git submodule foreach --recursive 'git clean -fdx && git reset --hard'" >&2
    fi

    echo "[OK] Clean done."
}

if [[ ${DO_CLEAN} -eq 1 ]]; then
    clean_build
fi

if [[ ${DO_BUILD} -eq 0 ]]; then
    exit 0
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

# Verify all required git submodules are initialised before we start cmake.
check_submodules

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
