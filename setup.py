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
#   [windows]
#     set QNN_SDK_ROOT=C:/Qualcomm/AIStack/QAIRT/2.42.0.251225/
#     set QAI_TOOLCHAINS=aarch64-windows-msvc (For ARM64 Windows Python) [or] set QAI_TOOLCHAINS=arm64x-windows-msvc (For AMD(X64) Windows Python)
#     set QAI_HEXAGONARCH=81
#
#     python -m build -w
#
#   [linux]
#     export QNN_SDK_ROOT=~/QAIRT/2.38.0.250901/
#     python -m build -w
#
# Also works with legacy setup.py invocation:
#   python setup.py bdist_wheel --toolchains aarch64-windows-msvc --hexagonarch 81
#
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
VERSION = "2.42.0"
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

def _detect_arch() -> str:
    """
    Keep your original behavior:
    - On Windows:
        - If machine == AMD64 OR 'AMD64' in sys.version => target ARM64EC
        - Else => ARM64
    - On Linux aarch64 => aarch64
    """
    machine = platform.machine()
    sysinfo = sys.version
    if machine == "aarch64":
        return "aarch64"
    # Windows / others
    arch = "ARM64"
    if machine == "AMD64" or ("AMD64" in sysinfo):
        arch = "ARM64EC"
    return arch


def _default_generator_and_args(arch: str):
    """
    Return (generator_args:list[str], is_multi_config:bool)
    """
    if arch == "aarch64":
        return ([], False)

    # Visual Studio generator (multi-config)
    gen = ["-G", "Visual Studio 17 2022", "-A", arch]
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


def _get_dsp_arch(hexagonarch: Optional[str] = None) -> str:
    if hexagonarch:
        return str(hexagonarch)
    if _is_windows():
        return "81"  # 73 for older QAIRT prior to 2.38.1 (your original note)
    return "68"      # TODO: linux or android defaults (kept as-is)

def _compute_version_with_dsp_suffix(default_base: str) -> str:
    """
    VERSION = <SDK X.Y.Z from QNN_SDK_ROOT> + '.' + <hexagon arch>
    Priority:
      1) If env/arg provides QAI_HEXAGONARCH / --hexagonarch, use it.
      2) Otherwise, use _get_dsp_arch() default.
    """
    base = _get_base_version_from_qnn_sdk_root(default_base)
    explicit_hex = os.environ.get("QAI_HEXAGONARCH") or _get_hexagonarch_from_argv()
    dsp_arch = str(explicit_hex) if explicit_hex else _get_dsp_arch(None)
    return f"{base}.{dsp_arch}"

# Re-compute VERSION for wheel metadata (and zip naming) based on environment/args.
VERSION = _compute_version_with_dsp_suffix(VERSION)

def _package_zip_name(arch: str) -> str:
    if arch == "ARM64EC":
        return f"QAI_AppBuilder-win_arm64ec-QNN{VERSION}-{CONFIG}.zip"
    if arch == "aarch64":
        return f"QAI_AppBuilder-linux_arm64-QNN{VERSION}-{CONFIG}.zip"
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
    dsp_arch = _get_dsp_arch(hexagonarch)

    # Decide LIB_PATH (your original priority/order)
    if toolchain is None:
        if _is_windows():
            lib_path = qnn_root / "lib" / "aarch64-windows-msvc"
            if arch == "ARM64EC":
                lib_path = qnn_root / "lib" / "arm64x-windows-msvc"
        else:
            # linux/android probing (kept same)
            cand1 = qnn_root / "lib" / "aarch64-oe-linux-gcc11.2"
            cand2 = qnn_root / "lib" / "aarch64-android"
            cand3 = qnn_root / "lib"
            if (cand1 / "libGenie.so").exists():
                lib_path = cand1
            elif (cand2 / "libGenie.so").exists():
                lib_path = cand2
            elif (cand3 / "libGenie.so").exists():
                lib_path = cand3
            else:
                raise RuntimeError('Failed to find "libGenie.so" in QNN SDK lib paths')
    else:
        lib_path = qnn_root / "lib" / toolchain

    dsp_lib_path = qnn_root / "lib" / f"hexagon-v{dsp_arch}" / "unsigned"

    # Where to put QNN libs
    def _do_copy_into(pkg_dir: Path):
        libs_dir = pkg_dir / "libs"

        # Windows Genie
        _copy_if_exists(lib_path / "Genie.dll", pkg_dir / "Genie.dll")
        # Keep "lib/Release" staging like your old script
        (_project_root() / "lib" / "Release").mkdir(parents=True, exist_ok=True)
        _copy_if_exists(lib_path / "Genie.dll", _project_root() / "lib" / "Release" / "Genie.dll")
        _copy_if_exists(lib_path / "Genie.lib", _project_root() / "lib" / "Release" / "Genie.lib")

        # DSP files
        _copy_if_exists(dsp_lib_path / f"libqnnhtpV{dsp_arch}.cat", libs_dir / f"libqnnhtpV{dsp_arch}.cat")
        _copy_if_exists(dsp_lib_path / f"libQnnHtpV{dsp_arch}Skel.so", libs_dir / f"libQnnHtpV{dsp_arch}Skel.so")

        # Windows QNN dlls
        _copy_if_exists(lib_path / "QnnHtp.dll", libs_dir / "QnnHtp.dll")
        _copy_if_exists(lib_path / "QnnCpu.dll", libs_dir / "QnnCpu.dll")
        _copy_if_exists(lib_path / "QnnHtpNetRunExtensions.dll", libs_dir / "QnnHtpNetRunExtensions.dll")
        _copy_if_exists(lib_path / "QnnHtpPrepare.dll", libs_dir / "QnnHtpPrepare.dll")
        _copy_if_exists(lib_path / f"QnnHtpV{dsp_arch}Stub.dll", libs_dir / f"QnnHtpV{dsp_arch}Stub.dll")
        _copy_if_exists(lib_path / "QnnSystem.dll", libs_dir / "QnnSystem.dll")

        # Linux/Android .so variants
        _copy_if_exists(lib_path / "libGenie.so", pkg_dir / "libGenie.so")
        _copy_if_exists(lib_path / "libGenie.so", _project_root() / "lib" / "Release" / "libGenie.so")
        _copy_if_exists(lib_path / "libQnnHtp.so", libs_dir / "libQnnHtp.so")
        _copy_if_exists(lib_path / "libQnnCpu.so", libs_dir / "libQnnCpu.so")
        _copy_if_exists(lib_path / "libQnnHtpNetRunExtensions.so", libs_dir / "libQnnHtpNetRunExtensions.so")
        _copy_if_exists(lib_path / "libQnnHtpPrepare.so", libs_dir / "libQnnHtpPrepare.so")
        _copy_if_exists(lib_path / f"libQnnHtpV{dsp_arch}Stub.so", libs_dir / f"libQnnHtpV{dsp_arch}Stub.so")
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

    # Headers
    _copy_if_exists(root / "src" / "LibAppBuilder.hpp", include_path / "LibAppBuilder.hpp")
    _copy_if_exists(root / "src" / "Lora.hpp", include_path / "Lora.hpp")

    _zip_dir(tmp_path, pkg_zip)


def _clean_artifacts():
    """
    Equivalent to your original build_clean(), but safe/robust.
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
        "libappbuilder.so", "Genie.dll", "libGenie.so"
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
    package_data={"": ["*.dll", "*.pdb", "*.exe", "*.so", "*.cat"]},
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
    url="https://github.com/quic/ai-engine-direct-helper",
    author="quic-zhanweiw",
    author_email="quic_zhanweiw@quicinc.com",
    license="BSD-3-Clause",
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Qualcomm CE",
        "Operating System :: Windows On Snapdragon",
        "Programming Language :: Python :: 3.12",
    ],
)
