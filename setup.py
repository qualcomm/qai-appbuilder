# =============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
# =============================================================================
#
# Modern build notes:
# - Prefer:  python -m build  (PEP 517)  instead of  python setup.py bdist_wheel
# - Still supported: python setup.py bdist_wheel
#
# Example:
#   [windows - WOS ARM64 device]
#     set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.42.0.251225/
#     set QAI_TOOLCHAINS=aarch64-windows-msvc (For ARM64 Windows Python) [or] set QAI_TOOLCHAINS=arm64x-windows-msvc (For AMD(X64) Windows Python)
#     set QAI_HEXAGONARCH=81
#
#     python -m build -w
#
#   [windows - x86 PC (non-WOS)]
#     set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.42.0.251225/
#     set QAI_TOOLCHAINS=x86_64-windows-msvc 
#     python -m build -w
#
#   [linux]
#     export QNN_SDK_ROOT=~/QAIRT/2.38.0.250901/
#     python -m build -w
#
#   [linux - cross-compile for aarch64]
#     export QNN_SDK_ROOT=~/QAIRT/2.38.0.250901/
#     export QAI_TOOLCHAINS=aarch64-oe-linux-gcc11.2
#     export QAI_CMAKE_TOOLCHAIN_FILE=~/toolchain-aarch64.cmake
#     export QAI_DSP_ARCHES=73,75,79 (optional - defaults to a per-OS list)
#     python -m build -w

# =============================================================================

import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
import warnings
from typing import Optional

from setuptools import Extension, setup, find_packages
from setuptools.command.build_ext import build_ext
from setuptools.command.bdist_wheel import bdist_wheel


# ---------------------------
# Project constants
# ---------------------------
VERSION = "2.47.0"
CONFIG = "Release"  # Release, RelWithDebInfo
PACKAGE_NAME = "qai_appbuilder"

# -----------------------------------------------------------------------------
# Silence setuptools warning banner when invoking legacy "python setup.py ..."
#
# When running:
#   python setup.py bdist_wheel ...
# setuptools emits a noisy SetuptoolsDeprecationWarning:
#   "setup.py install is deprecated."
# with a long banner.
#
# We only filter it when this file is executed as a script (__main__),
# so PEP517 builds (e.g. "python -m build -w") are unaffected.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    warnings.filterwarnings(
        "ignore",
        message=r".*setup\.py install is deprecated\..*",
        category=Warning,
    )

# ---------------------------
# Helpers
# ---------------------------
def _extract_semver3_from_text(text: str) -> Optional[str]:
    """
    Extract first 'X.Y.Z' (three numeric dot-separated components) from a string.
    Example: 'C:/.../2.42.0.251225/' -> '2.42.0'
    """
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

def _extract_semver4_from_text(text: str) -> Optional[str]:
    """
    Extract 'X.Y.Z.DATE' (4 numeric dot-separated components) from a string.
    Example: 'C:/.../2.42.0.251225/' -> '2.42.0.251225'
    """
    if not text:
        return None
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1) if m else None

def _get_base_version_from_qnn_sdk_root(default: str) -> str:
    """
    Prefer extracting base version from QNN_SDK_ROOT path; fallback to provided default.
    """
    qnn_root = os.environ.get("QNN_SDK_ROOT", "")
    v = _extract_semver3_from_text(qnn_root)
    return v if v else default

def _get_hexagonarch_from_argv() -> Optional[str]:
    """
    Parse legacy setup.py options early so wheel metadata version can include DSP suffix.
    Supports: --hexagonarch 81  OR  --hexagonarch=81
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--hexagonarch" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--hexagonarch="):
            return a.split("=", 1)[1]
    return None

def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _is_windows() -> bool:
    return sys.platform.startswith("win")

def _require_cmake():
    """Fail fast with a readable error if cmake is not available."""
    if shutil.which("cmake") is None:
        raise RuntimeError("cmake executable not found in PATH. Please install CMake and ensure it's available.")

def _is_wos_device() -> bool:
    """
    Detect whether we are running on a Windows on Snapdragon (WOS) device.
    On WOS, the underlying CPU is ARM64 (Qualcomm Snapdragon), but x64 Python
    reports platform.machine() == 'AMD64' due to emulation.
    We distinguish WOS from a real x86_64 PC by checking the processor string
    and PROCESSOR_IDENTIFIER / PROCESSOR_ARCHITECTURE environment variables.
    Returns True if running on a WOS (ARM64) device, False on a real x86_64 PC.
    """
    # Explicit user request via QAI_TOOLCHAINS overrides host detection.
    # This is the path CI uses: cross-build ARM64EC / ARM64 wheels from an
    # x86_64 hosted runner. Without this hook, _detect_arch() would pick
    # "x86_64" on the x64 build machine and request a non-existent
    # x86_64-windows-msvc SDK toolchain.
    qai_toolchains = os.environ.get("QAI_TOOLCHAINS", "").lower()
    if qai_toolchains in ("arm64x-windows-msvc", "aarch64-windows-msvc"):
        return True

    # Check PROCESSOR_ARCHITECTURE env var (set by Windows)
    proc_arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
    proc_id = os.environ.get("PROCESSOR_IDENTIFIER", "").upper()
    processor = platform.processor().upper()
    print(f"proc_arch:{proc_arch}")
    print(f"proc_id:{proc_id}")
    print(f"processor:{processor}")
    # ARM indicators in processor info
    arm_keywords = ("ARM", "QUALCOMM", "SNAPDRAGON", "ORYON")
    for kw in arm_keywords:
        if kw in proc_id or kw in processor:
            return True

    # On WOS, PROCESSOR_ARCHITECTURE is typically "ARM64" even for x64 processes
    if proc_arch == "ARM64":
        return True

    return False


def _detect_arch() -> str:
    """
    Detect the target build architecture:
    - On Linux: aarch64 or x86_64
    - On Windows WOS (ARM64 Snapdragon) device:
        - If running x64 Python (AMD64) => "ARM64EC"
        - Else => "ARM64"
    - On Windows x86_64 PC (non-WOS) => "x86_64"
    """
    machine = platform.machine()
    sysinfo = sys.version
    print(f"machine={machine}")
    print(f"sysinfo={sysinfo}")

    if not _is_windows():
        if machine in {"aarch64", "arm64"}:
            return "aarch64"
        if machine in {"x86_64", "AMD64"}:
            return "x86_64-linux"
    # Windows: distinguish WOS (ARM64 CPU) from real x86_64 PC
    if _is_wos_device():
        # WOS device: ARM64 CPU
        if machine == "AMD64" or ("AMD64" in sysinfo):
            return "ARM64EC"
        return "ARM64"
    else:
        # Real x86_64 Windows PC (non-WOS)
        return "x86_64"


def _default_generator_and_args(arch: str):
    """
    Return (generator_args:list[str], is_multi_config:bool)
    """
    if not _is_windows():
        return ([], False)

    # Allow overriding the VS generator (e.g. "Visual Studio 18 2026").
    # If unset, omit -G entirely so CMake auto-detects the newest installed
    # Visual Studio (works for VS 2022, 2026, ... without hardcoding a version).
    vs_gen = os.environ.get("QAI_VS_GENERATOR")
    gen_flag = ["-G", vs_gen] if vs_gen else []

    if arch == "x86_64":
        # x86_64 Windows PC: x64 platform
        gen = gen_flag + ["-A", "x64"]
        return (gen, True)

    # WOS ARM64 / ARM64EC: multi-config VS generator
    gen = gen_flag + ["-A", arch]
    return (gen, True)


def _cmake_python_hints_args() -> list:
    """
    Preserve your ARM64EC + x64 Python workaround:
    - Force pybind11 to compat mode (avoid FindPython arch check)
    - Provide Python executable via multiple variable names
    """
    py = os.environ.get("PYTHON_X64_EXECUTABLE", sys.executable)
    py = str(Path(py))
    return [
        "-DPYBIND11_FINDPYTHON=COMPAT",
        f"-DPYBIND11_PYTHON_VERSION={sys.version_info.major}.{sys.version_info.minor}",
        f"-DPython_EXECUTABLE={py}",
        f"-DPython3_EXECUTABLE={py}",
        f"-DPYTHON_EXECUTABLE={py}",
    ]


def _safe_rmtree(p: Path):
    shutil.rmtree(p, ignore_errors=True)


def _ensure_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _copy_if_exists(src: Path, dst: Path):
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _zip_dir(dirpath: Path, out_fullname: Path):
    out_fullname.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_fullname, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in dirpath.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(dirpath))


def _get_qnn_sdk_root() -> Path:
    v = os.environ.get("QNN_SDK_ROOT")
    if not v:
        raise RuntimeError('QNN_SDK_ROOT environmental variable not set')
    p = Path(v)
    if not p.exists() or not p.is_dir():
        raise RuntimeError(f'QNN_SDK_ROOT="{v}" does not exist or is not a directory')
    return p


def _parse_toolchain_system_processor(toolchain_file: str) -> Optional[tuple]:
    """
    Parse CMAKE_SYSTEM_NAME and CMAKE_SYSTEM_PROCESSOR from a CMake toolchain
    file. Returns (system, processor) or None if either is absent.
    Only looks at set() calls with literal string arguments - does not evaluate CMake.
    """
    try:
        text = Path(toolchain_file).read_text(encoding="utf-8")
    except OSError:
        return None
    def _cmake_var(name: str) -> Optional[str]:
        m = re.search(rf'set\s*\(\s*{name}\s+"?([^"\s\)]+)"?', text)
        return m.group(1) if m else None
    system = _cmake_var("CMAKE_SYSTEM_NAME")
    processor = _cmake_var("CMAKE_SYSTEM_PROCESSOR")
    if not system or not processor:
        return None
    return system, processor


def _host_platform_from_toolchain(toolchain_file: str) -> Optional[str]:
    """
    Return a distutils platform string (e.g. 'linux-aarch64') derived from
    the toolchain file's CMAKE_SYSTEM_NAME/CMAKE_SYSTEM_PROCESSOR, or None
    if either is absent or the combination is unrecognised.
    """
    parsed = _parse_toolchain_system_processor(toolchain_file)
    if not parsed:
        return None
    system, processor = parsed
    if system.lower() == "linux":
        return f"linux-{processor}"
    if system.lower() == "android":
        return f"linux-{processor}"
    if system.lower() == "windows":
        return f"win-{processor}"
    return None


def _pybind11_cross_ext_args(toolchain_file: str) -> list:
    """
    pybind11's FindPythonLibsNew.cmake (PYBIND11_FINDPYTHON=COMPAT) determines
    PYTHON_MODULE_EXTENSION by executing PYTHON_EXECUTABLE and reading its
    EXT_SUFFIX - but PYTHON_EXECUTABLE is the build-host interpreter, so that
    suffix is for the host arch, not the cross-compilation target. Compute the
    target suffix from the running interpreter's version plus the toolchain's
    target triplet, and force it via cmake args (mirrors the PYBIND11_PYTHONLIBS_OVERWRITE
    escape hatch documented in FindPythonLibsNew.cmake).
    """
    parsed = _parse_toolchain_system_processor(toolchain_file)
    if not parsed:
        return []
    system, processor = parsed
    if system.lower() != "linux":
        return []
    ext_suffix = f".cpython-{sys.version_info.major}{sys.version_info.minor}-{processor}-linux-gnu.so"
    return [
        "-DPYBIND11_PYTHONLIBS_OVERWRITE=OFF",
        f"-DPYTHON_MODULE_EXTENSION={ext_suffix}",
    ]

def _get_dsp_arches(toolchain: Optional[str] = None, hexagonarch: Optional[str] = None) -> list[str]:
    """Return a list of Hexagon DSP arch versions to package.

    QAI_DSP_ARCHES overrides the default list (comma/whitespace-separated,
    e.g. "68,73,75,79"), for packaging a custom set of DSP arches.
    """
    env_arches = os.environ.get("QAI_DSP_ARCHES")
    if env_arches:
        return [a for a in re.split(r"[,\s]+", env_arches.strip()) if a]
    if _is_windows():
        return ["73", "81"]
    else:
        return ["68", "73", "75", "79"]

def _compute_version_with_dsp_suffix(default_base: str) -> str:
    """
    VERSION = <SDK X.Y.Z from QNN_SDK_ROOT> + '.' + <hexagon arch>
    Priority:
      1) If env/arg provides QAI_HEXAGONARCH / --hexagonarch, use it.
      2) Otherwise, use _get_dsp_arch() default.
    """
    base = _get_base_version_from_qnn_sdk_root(default_base)
    return f"{base}"

def _patch_setup_py_version(version3: str) -> None:
    """
    Rewrite the literal `VERSION = "X.Y.Z"` constant in this setup.py on disk
    so it stays in sync with the SDK version extracted from QNN_SDK_ROOT.

    Only the first top-level assignment (the one in the 'Project constants'
    block) is changed; the later `VERSION = _compute_version_with_dsp_suffix(...)`
    re-assignment is left untouched because it does not match the literal pattern.
    version3: 3-part semver (e.g. '2.46.0')
    """
    setup_py = Path(__file__).resolve()
    text = setup_py.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'^VERSION\s*=\s*"[^"]*"',
        f'VERSION = "{version3}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n and new_text != text:
        setup_py.write_text(new_text, encoding="utf-8")
        print(f'-- Patched setup.py VERSION = "{version3}"')

def _patch_cpp_version_files(version3: str, version4: str) -> None:
    """
    Rewrite BuildId.hpp and common.h with the current SDK version.
    version3: 3-part semver (e.g. '2.46.0')
    version4: 4-part semver (e.g. '2.46.0.260424')
    """
    root = _project_root()

    # Patch BuildId.hpp
    build_id = root / "src" / "Utils" / "BuildId.hpp"
    text = build_id.read_text(encoding="utf-8")
    text = re.sub(
        r'return std::string\("v[^"]*"\)',
        f'return std::string("v{version4}")',
        text
    )
    build_id.write_text(text, encoding="utf-8")

    # Patch common.h
    common_h = root / "pybind" / "common.h"
    text = common_h.read_text(encoding="utf-8")
    text = re.sub(
        r'#define APPBUILDER_VERSION "[^"]*"',
        f'#define APPBUILDER_VERSION "{version3}"',
        text
    )
    common_h.write_text(text, encoding="utf-8")

    # Patch setup.py's own VERSION constant
    _patch_setup_py_version(version3)

# Re-compute VERSION for wheel metadata (and zip naming) based on environment/args.
VERSION = _compute_version_with_dsp_suffix(VERSION)

def _package_zip_name(arch: str) -> str:
    if arch == "ARM64EC":
        return f"QAI_AppBuilder-win_arm64ec-QNN{VERSION}-{CONFIG}.zip"
    if arch == "aarch64":
        return f"QAI_AppBuilder-linux_arm64-QNN{VERSION}-{CONFIG}.zip"
    if arch == "x86_64":
        return f"QAI_AppBuilder-win_x86_64-QNN{VERSION}-{CONFIG}.zip"
    if arch == "x86_64-linux":
        return f"QAI_AppBuilder-linux_x86_64-QNN{VERSION}-{CONFIG}.zip"
    return f"QAI_AppBuilder-win_arm64-QNN{VERSION}-{CONFIG}.zip"

def _ensure_runtime_pkg_dirs(source_pkg_dir: Path, build_pkg_dir: Path):
    """
    Ensure libs directory exists and has __init__.py in BOTH:
    - source tree (for editable/dev convenience)
    - build_lib tree (for wheel content)
    """
    for pkg_dir in (source_pkg_dir, build_pkg_dir):
        libs = pkg_dir / "libs"
        libs.mkdir(parents=True, exist_ok=True)
        _ensure_file(
            libs / "__init__.py",
            "# This file marks this directory as a Python package.\n",
        )


def _copy_runtime_artifacts(
    *,
    arch: str,
    toolchain: Optional[str] = None,
    hexagonarch: Optional[str] = None,
    source_pkg_dir: Path,
    build_pkg_dir: Path,
):
    """
    Copy Genie/QNN runtime libraries into:
    - <package>/ (Genie.dll/so, app svc, libappbuilder)
    - <package>/libs (QNN libs, cat, skel, etc.)
    Matching your original behavior.
    """
    qnn_root = _get_qnn_sdk_root()
    # Determine effective toolchain for DSP arch selection (needed for multi-arch Windows packaging).
    effective_toolchain = toolchain
    if effective_toolchain is None and _is_windows():
        if arch == "ARM64EC":
            effective_toolchain = "arm64x-windows-msvc"
        elif arch == "x86_64":
            effective_toolchain = "x86_64-windows-msvc"
        else:
            effective_toolchain = "aarch64-windows-msvc"
    dsp_arches = _get_dsp_arches(toolchain=effective_toolchain, hexagonarch=hexagonarch)

    # Decide LIB_PATH (your original priority/order)
    if toolchain is None:
        if _is_windows():
            if arch == "ARM64EC":
                lib_path = qnn_root / "lib" / "arm64x-windows-msvc"
            elif arch == "x86_64":
                lib_path = qnn_root / "lib" / "x86_64-windows-msvc"
            else:
                lib_path = qnn_root / "lib" / "aarch64-windows-msvc"
        else:
            # linux/android probing; prefer QAIRT envsetup defaults
            candidates: list[Path] = []

            if arch == "aarch64":
                candidates = [
                    qnn_root / "lib" / "aarch64-oe-linux-gcc11.2",
                    qnn_root / "lib" / "aarch64-android",
                ]
            elif arch == "x86_64-linux":
                # QAIRT envsetup.sh uses x86_64-linux-clang for PATH/LD_LIBRARY_PATH
                candidates = [
                    qnn_root / "lib" / "x86_64-linux-clang",
                ]

            # Fallbacks
            candidates.append(qnn_root / "lib")

            lib_path = None
            for cand in candidates:
                if (cand / "libGenie.so").exists():
                    lib_path = cand
                    break
            if lib_path is None:
                raise RuntimeError(
                    'Failed to find "libGenie.so" in QNN SDK lib paths. '
                    'Set QAI_TOOLCHAINS to the correct <QNN_SDK_ROOT>/lib/<toolchain> subdir.'
                )
    else:
        lib_path = qnn_root / "lib" / toolchain

    # Where to put QNN libs
    def _do_copy_into(pkg_dir: Path):
        libs_dir = pkg_dir / "libs"

        # Windows Genie
        _copy_if_exists(lib_path / "Genie.dll", pkg_dir / "Genie.dll")
        # Keep "lib/Release" staging like your old script
        (_project_root() / "lib" / "Release").mkdir(parents=True, exist_ok=True)
        _copy_if_exists(lib_path / "Genie.dll", _project_root() / "lib" / "Release" / "Genie.dll")
        _copy_if_exists(lib_path / "Genie.lib", _project_root() / "lib" / "Release" / "Genie.lib")

        # -------------------------
        # DSP-arch-specific files
        # -------------------------
        # Expect dsp_arches like ["73","81"] (instead of single dsp_arch string)
        for _dsp in dsp_arches:
            dsp_lib_path = qnn_root / "lib" / f"hexagon-v{_dsp}" / "unsigned"

            # cat + skel
            _copy_if_exists(
                dsp_lib_path / f"libqnnhtpV{_dsp}.cat",
                libs_dir / f"libqnnhtpV{_dsp}.cat",
            )
            _copy_if_exists(
                dsp_lib_path / f"libQnnHtpV{_dsp}Skel.so",
                libs_dir / f"libQnnHtpV{_dsp}Skel.so",
            )

            # Per-arch stubs (Windows/Linux)
            _copy_if_exists(
                lib_path / f"QnnHtpV{_dsp}Stub.dll",
                libs_dir / f"QnnHtpV{_dsp}Stub.dll",
            )
            _copy_if_exists(
                lib_path / f"libQnnHtpV{_dsp}Stub.so",
                libs_dir / f"libQnnHtpV{_dsp}Stub.so",
            )

        # -------------------------
        # Non-DSP-specific QNN libs
        # -------------------------
        # Windows QNN dlls
        _copy_if_exists(lib_path / "QnnHtp.dll", libs_dir / "QnnHtp.dll")
        _copy_if_exists(lib_path / "QnnCpu.dll", libs_dir / "QnnCpu.dll")
        _copy_if_exists(lib_path / "QnnGpu.dll", libs_dir / "QnnGpu.dll")
        _copy_if_exists(lib_path / "QnnHtpNetRunExtensions.dll", libs_dir / "QnnHtpNetRunExtensions.dll")
        _copy_if_exists(lib_path / "QnnHtpPrepare.dll", libs_dir / "QnnHtpPrepare.dll")
        _copy_if_exists(lib_path / "QnnSystem.dll", libs_dir / "QnnSystem.dll")

        # Linux/Android .so variants
        _copy_if_exists(lib_path / "libGenie.so", pkg_dir / "libGenie.so")
        _copy_if_exists(lib_path / "libGenie.so", _project_root() / "lib" / "Release" / "libGenie.so")
        _copy_if_exists(lib_path / "libQnnHtp.so", libs_dir / "libQnnHtp.so")
        _copy_if_exists(lib_path / "libQnnCpu.so", libs_dir / "libQnnCpu.so")
        _copy_if_exists(lib_path / "libQnnGpu.so", libs_dir / "libQnnGpu.so")
        _copy_if_exists(lib_path / "libQnnHtpNetRunExtensions.so", libs_dir / "libQnnHtpNetRunExtensions.so")
        _copy_if_exists(lib_path / "libQnnHtpPrepare.so", libs_dir / "libQnnHtpPrepare.so")
        _copy_if_exists(lib_path / "libQnnSystem.so", libs_dir / "libQnnSystem.so")

    _do_copy_into(source_pkg_dir)
    _do_copy_into(build_pkg_dir)



def _build_root_cmake_project(arch: str, source_pkg_dir: Path, build_pkg_dir: Path, toolchain: Optional[str] = None, hexagonarch: Optional[str] = None,) -> None:
    """
    Equivalent to your original build_cmake(), but:
    - no chdir side effects leaking outside
    - uses list-args subprocess
    - copies artifacts into both source package dir and build_lib package dir
    """
    root = _project_root()
    _require_cmake()
    build_dir = root / "build"
    lib_dir = root / "lib"

    generator_args, is_multi_config = _default_generator_and_args(arch)

    build_dir.mkdir(parents=True, exist_ok=True)

    cmake_configure = ["cmake", "--no-warn-unused-cli", str(root)] + generator_args + _cmake_python_hints_args()
    _cmake_toolchain = os.environ.get("QAI_CMAKE_TOOLCHAIN_FILE")
    if _cmake_toolchain:
        cmake_configure.append(f"-DCMAKE_TOOLCHAIN_FILE={_cmake_toolchain}")
        cmake_configure += _pybind11_cross_ext_args(_cmake_toolchain)
    subprocess.run(cmake_configure, cwd=str(build_dir), check=True)

    cmake_build = ["cmake", "--build", str(build_dir)]
    if is_multi_config:
        cmake_build += ["--config", CONFIG]
    subprocess.run(cmake_build, cwd=str(build_dir), check=True)

    # Copy produced binaries (your original logic)
    # Windows outputs: lib/<CONFIG>/QAIAppSvc.exe etc
    if (lib_dir / CONFIG / "QAIAppSvc.exe").exists():
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.dll", source_pkg_dir / "libappbuilder.dll")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.exe", source_pkg_dir / "QAIAppSvc.exe")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.pdb", source_pkg_dir / "QAIAppSvc.pdb")
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.pdb", source_pkg_dir / "libappbuilder.pdb")

        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.dll", build_pkg_dir / "libappbuilder.dll")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.exe", build_pkg_dir / "QAIAppSvc.exe")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.pdb", build_pkg_dir / "QAIAppSvc.pdb")
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.pdb", build_pkg_dir / "libappbuilder.pdb")

    # Linux output
    _copy_if_exists(lib_dir / "libappbuilder.so", source_pkg_dir / "libappbuilder.so")
    _copy_if_exists(lib_dir / "libappbuilder.so", build_pkg_dir / "libappbuilder.so")

    # Linux QAIAppSvc service executable (cross-process inference). It is built
    # alongside libappbuilder.so in lib/. Copy it into the package and make sure
    # it keeps the executable bit so posix_spawnp can launch it.
    if (lib_dir / "QAIAppSvc").exists():
        for pkg_dir in (source_pkg_dir, build_pkg_dir):
            dst = pkg_dir / "QAIAppSvc"
            _copy_if_exists(lib_dir / "QAIAppSvc", dst)
            if dst.exists():
                os.chmod(dst, 0o755)

    # Ensure libs/__init__.py exists
    _ensure_runtime_pkg_dirs(source_pkg_dir, build_pkg_dir)

    # Copy QNN/Genie runtime libs
    _copy_runtime_artifacts(
        arch=arch,
        toolchain=toolchain,
        hexagonarch=hexagonarch,
        source_pkg_dir=source_pkg_dir,
        build_pkg_dir=build_pkg_dir,
    )


def _build_release_zip(arch: str):
    """
    Equivalent to your original build_release().
    It packages 'lib/package' and writes to dist/<PACKAGE_ZIP>.
    """
    root = _project_root()
    tmp_path = root / "lib" / "package"
    include_path = tmp_path / "include"
    dist_dir = root / "dist"
    pkg_zip = dist_dir / _package_zip_name(arch)

    tmp_path.mkdir(parents=True, exist_ok=True)
    include_path.mkdir(parents=True, exist_ok=True)

    lib_dir = root / "lib"

    # Windows artifacts
    if (lib_dir / CONFIG / "QAIAppSvc.exe").exists():
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.dll", tmp_path / "libappbuilder.dll")
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.lib", tmp_path / "libappbuilder.lib")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.exe", tmp_path / "QAIAppSvc.exe")
        _copy_if_exists(lib_dir / CONFIG / "libappbuilder.pdb", tmp_path / "libappbuilder.pdb")
        _copy_if_exists(lib_dir / CONFIG / "QAIAppSvc.pdb", tmp_path / "QAIAppSvc.pdb")

    # Linux artifact
    _copy_if_exists(lib_dir / "libappbuilder.so", tmp_path / "libappbuilder.so")
    if (lib_dir / "QAIAppSvc").exists():
        _copy_if_exists(lib_dir / "QAIAppSvc", tmp_path / "QAIAppSvc")
        if (tmp_path / "QAIAppSvc").exists():
            os.chmod(tmp_path / "QAIAppSvc", 0o755)

    # Headers
    _copy_if_exists(root / "src" / "LibAppBuilder.hpp", include_path / "LibAppBuilder.hpp")
    _copy_if_exists(root / "src" / "Lora.hpp", include_path / "Lora.hpp")

    _zip_dir(tmp_path, pkg_zip)


def _clean_artifacts():
    """
    NOTE: We DO NOT delete dist/ because wheel output is there.
    """
    root = _project_root()
    source_pkg_dir = root / "script" / PACKAGE_NAME
    libs_dir = source_pkg_dir / "libs"

    # egg-info/build/lib
    _safe_rmtree(root / "build")
    _safe_rmtree(root / "lib")
    _safe_rmtree(root / "script" / f"{PACKAGE_NAME}.egg-info")

    # Remove known binaries under source package dir
    for fname in [
        "libappbuilder.dll", "QAIAppSvc.exe", "QAIAppSvc.pdb", "libappbuilder.pdb",
        "libappbuilder.so", "QAIAppSvc", "Genie.dll", "libGenie.so"
    ]:
        p = source_pkg_dir / fname
        if p.exists():
            p.unlink()

    # Remove runtime QNN libs copied into source libs dir (keep __init__.py)
    if libs_dir.exists():
        for p in libs_dir.iterdir():
            if p.is_file() and p.name != "__init__.py":
                p.unlink()


# ---------------------------
# CMake extension & commands
# ---------------------------
class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath((Path(sourcedir).resolve()))


class QaiCMakeBuild(build_ext):
    """
    - Runs root CMake build (your old build_cmake) before building the pybind extension
    - Supports --toolchains / --hexagonarch as setuptools command options
    - Also reads env vars QAI_TOOLCHAINS / QAI_HEXAGONARCH for PEP517 friendliness
    """

    user_options = build_ext.user_options + [
        ("toolchains=", None, "QNN toolchain subdir name under <QNN_SDK_ROOT>/lib/ (e.g. aarch64-windows-msvc)"),
        ("hexagonarch=", None, "Hexagon DSP arch version (e.g. 81/73/68)"),
    ]

    def initialize_options(self):
        super().initialize_options()
        self.toolchains = None
        self.hexagonarch = None

    def finalize_options(self):
        super().finalize_options()
        # env var fallback (PEP517-friendly)
        if self.toolchains is None:
            self.toolchains = os.environ.get("QAI_TOOLCHAINS")
        if self.hexagonarch is None:
            self.hexagonarch = os.environ.get("QAI_HEXAGONARCH")

    def run(self):
        root = _project_root()
        arch = _detect_arch()
        print(f"-- Arch: {arch}")

        # Auto-patch C++ version headers from QNN_SDK_ROOT
        qnn_root_str = os.environ.get("QNN_SDK_ROOT", "")
        v3 = _extract_semver3_from_text(qnn_root_str)
        v4 = _extract_semver4_from_text(qnn_root_str)
        if not v3 or not v4:
            raise RuntimeError(
                f'Cannot extract version from QNN_SDK_ROOT="{qnn_root_str}". '
                'Expected a path containing a version like "2.46.0.260424".'
            )
        _patch_cpp_version_files(v3, v4)
        print(f"-- Version: {v4}")

        # Ensure build_lib package dirs
        build_py_cmd = self.get_finalized_command("build_py")
        build_lib = Path(build_py_cmd.build_lib)

        source_pkg_dir = root / "script" / PACKAGE_NAME
        build_pkg_dir = build_lib / PACKAGE_NAME

        _ensure_runtime_pkg_dirs(source_pkg_dir, build_pkg_dir)

        # Root CMake build + copy runtime libs (your old build_cmake)
        _build_root_cmake_project(
            arch=arch,
            toolchain=self.toolchains,
            hexagonarch=self.hexagonarch,
            source_pkg_dir=source_pkg_dir,
            build_pkg_dir=build_pkg_dir,
        )

        # Now build the actual extension via cmake (pybind/)
        super().run()

    def build_extension(self, ext: CMakeExtension) -> None:
        """
        Your original CMakeBuild.build_extension logic, modernized:
        - uses list args (no shell quoting issues)
        - preserves ARM64EC generator behavior and Python hints
        """
        arch = _detect_arch()
        print(f"build_extension-- Arch: {arch}")
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()

        cfg = CONFIG
        cmake_generator_env = os.environ.get("CMAKE_GENERATOR", "")

        # Base CMake args
        cmake_args = [
            f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}",
            f"-DCMAKE_BUILD_TYPE={cfg}",
            *_cmake_python_hints_args(),
        ]

        # generator / arch selection
        generator_args, is_multi_config = _default_generator_and_args(arch)

        # If user explicitly set CMAKE_GENERATOR, don't force ours, but keep arch behavior
        # (mimic your original logic about "single-config" and "contains_arch")
        single_config = any(x in cmake_generator_env for x in {"NMake", "Ninja"})
        contains_arch = any(x in cmake_generator_env for x in {"ARM", "Win64"})

        if arch != "aarch64" and not single_config and not contains_arch:
            # We already provide -A in generator_args (VS). Keep consistent.
            pass

        if is_multi_config:
            cmake_args.append(f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{cfg.upper()}={extdir}")

        # Build args
        build_args = []
        if is_multi_config:
            build_args += ["--config", cfg]

        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            if hasattr(self, "parallel") and self.parallel:
                build_args += [f"-j{self.parallel}"]

        build_temp = Path(self.build_temp) / ext.name
        build_temp.mkdir(parents=True, exist_ok=True)

        # Configure & build
        cmake_configure = ["cmake", "--no-warn-unused-cli", ext.sourcedir] + generator_args + cmake_args
        _cmake_toolchain = os.environ.get("QAI_CMAKE_TOOLCHAIN_FILE")
        if _cmake_toolchain:
            cmake_configure.append(f"-DCMAKE_TOOLCHAIN_FILE={_cmake_toolchain}")
            cmake_configure += _pybind11_cross_ext_args(_cmake_toolchain)
        subprocess.run(cmake_configure, cwd=str(build_temp), check=True)

        cmake_build = ["cmake", "--build", "."] + build_args
        subprocess.run(cmake_build, cwd=str(build_temp), check=True)


class QaiBdistWheel(bdist_wheel):
    """
    Preserve old behavior:
    - accept legacy CLI options on bdist_wheel: --hexagonarch / --toolchains
    - propagate them to build_ext via env vars (PEP517-friendly)
    - build wheel
    - build release zip
    - clean artifacts
    """

    user_options = bdist_wheel.user_options + [
        ("toolchains=", None, "QNN toolchain subdir name under <QNN_SDK_ROOT>/lib/ (e.g. aarch64-windows-msvc)"),
        ("hexagonarch=", None, "Hexagon DSP arch version (e.g. 81/73/68)"),
    ]

    def initialize_options(self):
        super().initialize_options()
        self.toolchains = None
        self.hexagonarch = None

    def finalize_options(self):
        # Derive _PYTHON_HOST_PLATFORM from QAI_CMAKE_TOOLCHAIN_FILE before
        # super().finalize_options() calls get_platform() to set plat_name.
        if not os.environ.get("_PYTHON_HOST_PLATFORM"):
            toolchain_file = os.environ.get("QAI_CMAKE_TOOLCHAIN_FILE", "")
            if toolchain_file:
                host_plat = _host_platform_from_toolchain(toolchain_file)
                if host_plat:
                    os.environ["_PYTHON_HOST_PLATFORM"] = host_plat
                    print(f"-- _PYTHON_HOST_PLATFORM={host_plat} (derived from toolchain)")
        super().finalize_options()
        # env var fallback
        if self.toolchains is None:
            self.toolchains = os.environ.get("QAI_TOOLCHAINS")
        if self.hexagonarch is None:
            self.hexagonarch = os.environ.get("QAI_HEXAGONARCH")

    def run(self):
        # Propagate to env vars so build_ext can see them
        if self.toolchains:
            os.environ["QAI_TOOLCHAINS"] = str(self.toolchains)
        if self.hexagonarch:
            os.environ["QAI_HEXAGONARCH"] = str(self.hexagonarch)

        arch = _detect_arch()
        print(f"run-- Arch: {arch}")
        # On x86_64 Windows PC (non-WOS), auto-set QAI_TOOLCHAINS to x86_64-windows-msvc
        # if not already specified by the user.
        if not os.environ.get("QAI_TOOLCHAINS"):
            if arch == "x86_64":
                os.environ["QAI_TOOLCHAINS"] = "x86_64-windows-msvc"
                self.toolchains = "x86_64-windows-msvc"
            elif arch == "ARM64EC":
                os.environ["QAI_TOOLCHAINS"] = "arm64x-windows-msvc"
                self.toolchains = "arm64x-windows-msvc"			
            elif arch == "aarch64":
                os.environ["QAI_TOOLCHAINS"] = "aarch64-oe-linux-gcc11.2"
                self.toolchains = "aarch64-oe-linux-gcc11.2"	
            elif arch == "ARM64":
                os.environ["QAI_TOOLCHAINS"] = "aarch64-windows-msvc"
                self.toolchains = "aarch64-windows-msvc"
            elif arch == "x86_64-linux":
                os.environ["QAI_TOOLCHAINS"] = "x86_64-linux-clang"
                self.toolchains = "x86_64-linux-clang"			
            else:
                print(f"please set environment QAI_TOOLCHAINS for this arch:{arch}!!!")

        # Build wheel first
        super().run()

        # Create release zip (same as old script)
        try:
            _build_release_zip(arch)
        except Exception as e:
            # Do not fail wheel build if release zip fails (optional safety)
            print(f"[WARN] build_release_zip failed: {e}")

        # Clean (same as old script)
        try:
            _clean_artifacts()
        except Exception as e:
            print(f"[WARN] clean_artifacts failed: {e}")


# ---------------------------
# setup()
# ---------------------------
with open("README.md", "r", encoding="utf-8", errors="ignore") as fh:
    long_description = fh.read()

setup(
    name=PACKAGE_NAME,
    version=VERSION,
    packages=find_packages(where="script"),
    package_dir={"": "script"},
    package_data={"": ["*.dll", "*.pdb", "*.exe", "*.so", "*.cat", "QAIAppSvc"]},
    ext_modules=[CMakeExtension("qai_appbuilder.appbuilder", "pybind")],
    cmdclass={
        "build_ext": QaiCMakeBuild,
        "bdist_wheel": QaiBdistWheel,
    },
    zip_safe=False,
    description=(
        "AppBuilder is Python & C++ extension that simplifies the process of developing "
        "AI prototype & App on WoS. It provides several APIs for running QNN models in "
        "WoS CPU & HTP, making it easier to manage AI models."
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/qualcomm/qai-appbuilder",
    author="quic-zhanweiw",
    author_email="quic_zhanweiw@quicinc.com",
    license="BSD-3-Clause",
    python_requires=">=3.10",
    install_requires=[
        # qai_appbuilder/__init__.py unconditionally imports onnxwrapper,
        # whose top-level imports require numpy + pyyaml. Without these,
        # `pip install qai-appbuilder` in a clean venv fails to import.
        "numpy",
        "pyyaml",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.12",
    ],
)
