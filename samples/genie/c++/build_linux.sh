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
# Optional environment variables (these are aligned with the QAI AppBuilder
# build, so the same values you use for `python -m build -w` work here):
#   QAI_TOOLCHAINS      Toolchain subdir name under <QNN_SDK_ROOT>/lib/.
#                       Default: aarch64-oe-linux-gcc11.2
#                       Examples: aarch64-oe-linux-gcc11.2,
#                                 aarch64-ubuntu-gcc9.4
#
# Lower-level overrides (used internally; you usually don't need to set them):
#   QNN_PLATFORM        If set, overrides QAI_TOOLCHAINS.  
#
# Other knobs:
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
    echo "[*] Cleaning build artefacts ..."

    # 1. The CMake build directory (contains all .o files).
    rm -rf "${SERVICE_DIR}/build_linux"

    # 2. Remove output directories matching the naming pattern.
    _APP_VER=""
    _ver_file="${SERVICE_DIR}/scripts/version.cmake"
    if [[ -f "${_ver_file}" ]]; then
        _major=$(grep -oP 'QAI_APP_BUILDER_MAJOR_VERSION\s+\K[0-9]+' "${_ver_file}" || echo "")
        _minor=$(grep -oP 'QAI_APP_BUILDER_MINOR_VERSION\s+\K[0-9]+' "${_ver_file}" || echo "")
        _patch=$(grep -oP 'QAI_APP_BUILDER_PATCH_VERSION\s+\K[0-9]+' "${_ver_file}" || echo "")
        if [[ -n "${_major}" && -n "${_minor}" && -n "${_patch}" ]]; then
            _APP_VER="${_major}.${_minor}.${_patch}"
        fi
    fi

    if [[ -n "${_APP_VER}" ]]; then
        # Compute same suffix cmake uses: _qnn<ver>[_gguf]
        _gguf_suffix=""
        if [[ "${USE_GGUF}" == "ON" ]]; then
            _gguf_suffix="_gguf"
        fi
        _target_dir="${SERVICE_DIR}/GenieService_v${_APP_VER}_qnn${_QNN_SDK_VER}${_gguf_suffix}"
        if [[ -d "${_target_dir}" ]]; then
            echo "    removing: ${_target_dir}"
            rm -rf "${_target_dir}"
        else
            echo "    (target dir does not exist, nothing to remove: ${_target_dir})"
        fi
    else
        # Fallback: couldn't parse version.cmake, just warn
        echo "    [WARN] Could not determine app version from version.cmake." >&2
        echo "           Skipping output directory removal. Use --clean-all to remove everything." >&2
    fi

    # 3. ExternalProject_Add(libappbuilder) builds in-source at REPO_ROOT.
    #    Wipe its CMake cache so a fresh configure happens next time.
    rm -rf "${REPO_ROOT}/CMakeFiles" \
           "${REPO_ROOT}/CMakeCache.txt" \
           "${REPO_ROOT}/cmake_install.cmake" \
    fi

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

    # 4. Restore submodule source trees (remove build residue only).
    if command -v git >/dev/null 2>&1; then
        for sub in External/libsamplerate External/curl External/MNN External/llama.cpp; do
            sub_dir="${SCRIPT_DIR}/${sub}"
            if [[ -d "${sub_dir}/.git" || -f "${sub_dir}/.git" ]]; then
                echo "    cleaning submodule: ${sub}"
                ( cd "${sub_dir}" && git clean -fdx >/dev/null 2>&1 || true )
                ( cd "${sub_dir}" && git reset --hard >/dev/null 2>&1 || true )
            fi
        done
    else
        echo "    [WARN] git not found - skipping submodule cleanup." >&2
    fi

    echo "[OK] Clean done."
}

# --------------------------------------------------------------------------
# Defaults & input validation (resolved BEFORE clean so we can compute the
# exact output directory name for the current QNN SDK version).
#
# Resolution order (matches QAI AppBuilder for cross-project consistency):
#   1. QNN_PLATFORM       lower-level override (rarely needed)
#   2. QAI_TOOLCHAINS     same env var qai_appbuilder uses
#   3. fallback default   "aarch64-oe-linux-gcc11.2"
# --------------------------------------------------------------------------
: "${BUILD_TYPE:=Release}"
: "${JOBS:=$(nproc 2>/dev/null || echo 4)}"
: "${USE_MNN:=OFF}"
: "${USE_GGUF:=OFF}"
: "${BUILD_AS_DLL:=OFF}"

# Resolve platform / toolchain.
if [[ -z "${QNN_PLATFORM:-}" ]]; then
    if [[ -n "${QAI_TOOLCHAINS:-}" ]]; then
        QNN_PLATFORM="${QAI_TOOLCHAINS}"
    else
        QNN_PLATFORM="aarch64-oe-linux-gcc11.2"
    fi
fi

if [[ -z "${QNN_SDK_ROOT:-}" ]]; then
    echo "[ERROR] QNN_SDK_ROOT is not set." >&2
    echo "        Please export QNN_SDK_ROOT to point at your QAIRT SDK install dir." >&2
    exit 1
fi

if [[ ! -d "${QNN_SDK_ROOT}" ]]; then
    echo "[ERROR] QNN_SDK_ROOT does not exist: ${QNN_SDK_ROOT}" >&2
    exit 1
fi

# Compute the QNN SDK version string (e.g. "2.45.40") from the SDK path,
# matching what the CMakeLists does for BUILD_PATH naming.
_QNN_SDK_VER="unknown"
if [[ "${QNN_SDK_ROOT}" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    _QNN_SDK_VER="${BASH_REMATCH[1]}"
fi

# --------------------------------------------------------------------------
# Now run clean if requested — using the resolved version info to only
# delete the CURRENT version's output directory, leaving other versions
# (e.g. different QNN SDK) intact.
# --------------------------------------------------------------------------
if [[ ${DO_CLEAN} -eq 1 ]]; then
    clean_build
fi

if [[ ${DO_BUILD} -eq 0 ]]; then
    exit 0
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
echo "  Script dir         : ${SCRIPT_DIR}"
echo "  Service dir        : ${SERVICE_DIR}"
echo "  QNN_SDK_ROOT       : ${QNN_SDK_ROOT}"
echo "  QAI_TOOLCHAINS     : ${QAI_TOOLCHAINS:-<unset>}    -> QNN_PLATFORM=${QNN_PLATFORM}"
echo "  BUILD_TYPE         : ${BUILD_TYPE}"
echo "  USE_MNN            : ${USE_MNN}"
echo "  USE_GGUF           : ${USE_GGUF}"
echo "  BUILD_AS_DLL       : ${BUILD_AS_DLL}"
echo "  JOBS               : ${JOBS}"
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
    -DQNN_PLATFORM="${QNN_PLATFORM}" \
    -DUSE_MNN="${USE_MNN}" \
    -DUSE_GGUF="${USE_GGUF}" \
    -DBUILD_AS_DLL="${BUILD_AS_DLL}"

cmake --build . --parallel "${JOBS}"

echo
echo "=========================================================="
echo "[OK] Build finished."

# Compute the exact output directory name (same formula as CMakeLists.txt).
_gguf_suffix=""
if [[ "${USE_GGUF}" == "ON" ]]; then
    _gguf_suffix="_gguf"
fi
# We need the app version from version.cmake
_APP_VER=""
_ver_file="${SERVICE_DIR}/scripts/version.cmake"
if [[ -f "${_ver_file}" ]]; then
    _major=$(grep -oP 'QAI_APP_BUILDER_MAJOR_VERSION\s+\K[0-9]+' "${_ver_file}" 2>/dev/null || echo "")
    _minor=$(grep -oP 'QAI_APP_BUILDER_MINOR_VERSION\s+\K[0-9]+' "${_ver_file}" 2>/dev/null || echo "")
    _patch=$(grep -oP 'QAI_APP_BUILDER_PATCH_VERSION\s+\K[0-9]+' "${_ver_file}" 2>/dev/null || echo "")
    if [[ -n "${_major}" && -n "${_minor}" && -n "${_patch}" ]]; then
        _APP_VER="${_major}.${_minor}.${_patch}"
    fi
fi

if [[ -n "${_APP_VER}" ]]; then
    OUT_DIR="${SERVICE_DIR}/GenieService_v${_APP_VER}_qnn${_QNN_SDK_VER}${_gguf_suffix}"
else
    # Fallback: find any matching dir
    OUT_DIR="$(find "${SERVICE_DIR}" -maxdepth 1 -type d -name 'GenieService_v*' \
                2>/dev/null | sort | tail -1)"
fi

if [[ -n "${OUT_DIR}" && -d "${OUT_DIR}" ]]; then
    echo "Output dir: ${OUT_DIR}"
    echo "Contents:"
    ls -lh "${OUT_DIR}"
else
    echo "[WARN] Could not locate output dir: ${OUT_DIR:-<unknown>}" >&2
fi
echo "=========================================================="
echo
echo "To deploy to a target board with QAIRT SDK already installed,"
echo "copy these 2 files:"
if [[ -n "${OUT_DIR}" && -d "${OUT_DIR}" ]]; then
    echo "  ${OUT_DIR}/GenieAPIService"
    echo "  ${OUT_DIR}/libappbuilder.so"
fi
echo
echo "Then on the target board:"
echo "  export LD_LIBRARY_PATH=\${QNN_SDK_ROOT}/lib/${QNN_PLATFORM}:<deploy_dir>:\${LD_LIBRARY_PATH}"
echo "  export ADSP_LIBRARY_PATH=\${QNN_SDK_ROOT}/lib/hexagon-vXX/unsigned  # e.g. v73, v75"
echo "  cd <deploy_dir> && ./GenieAPIService -c <model>/config.json -l -p 8910"
echo
