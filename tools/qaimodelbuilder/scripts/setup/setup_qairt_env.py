# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
setup_qairt_env.py - QAIModelBuilder QAIRT Environment Setup Helper

Handles environment detection, configuration generation, and verification
for the model-builder integration.

Usage:
    python setup_qairt_env.py --gen-config [--root ROOT_DIR]
    python setup_qairt_env.py --verify
    python setup_qairt_env.py --check-all
    python setup_qairt_env.py --install-python-deps
    python setup_qairt_env.py --install-inference-deps        # also auto-runs app-builder dep aggregation
    python setup_qairt_env.py --install-app-builder-deps      # standalone re-run of Pack dep aggregation
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import platform
from pathlib import Path


# ─── Platform detection ───────────────────────────────────────────────────────

def _detect_platform() -> str:
    """Return 'windows', 'linux', or 'unsupported'."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _detect_arch() -> str:
    """Return 'x86_64', 'aarch64', or 'unknown'."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return "unknown"


# ─── Constants ────────────────────────────────────────────────────────────────

# Single source of truth for the default QAIRT version: scripts/qairt_release.json.
# This file is run BOTH as ``python -m scripts.setup.setup_qairt_env`` and as a
# bare file path (Setup.bat passes "%SETUP_HELPER%"), so we read the manifest
# directly by path rather than relying on ``scripts`` being importable.
def _load_default_qairt_version() -> str:
    release_json = Path(__file__).resolve().parent.parent / "qairt_release.json"
    with open(release_json, encoding="utf-8") as fh:
        return json.load(fh)["qairt_version"]


_DEFAULT_QAIRT_VERSION = _load_default_qairt_version()

# Default QAIRT SDK version — override via env var QAIRT_VERSION or --sdk-version CLI arg
QAIRT_SDK_VERSION = os.environ.get("QAIRT_VERSION", _DEFAULT_QAIRT_VERSION)
QAIRT_SDK_DEFAULT = os.environ.get(
    "QAIRT_SDK_ROOT",
    rf"C:\Qualcomm\AIStack\QAIRT\{QAIRT_SDK_VERSION}"
    if sys.platform == "win32"
    else f"/opt/qcom/aistack/qairt/{QAIRT_SDK_VERSION}"
)
QAIRT_DOWNLOAD_URL = os.environ.get(
    "QAIRT_DOWNLOAD_URL",
    f"https://softwarecenter.qualcomm.com/api/download/software/sdks/"
    f"Qualcomm_AI_Runtime_Community/All/{QAIRT_SDK_VERSION}/v{QAIRT_SDK_VERSION}.zip"
)
VS_COMMUNITY_BASE = r"C:\Program Files\Microsoft Visual Studio\2022\Community"
VS_VCVARSALL = rf"{VS_COMMUNITY_BASE}\VC\Auxiliary\Build\vcvarsall.bat"
VS_VC_TARGETS = rf"{VS_COMMUNITY_BASE}\MSBuild\Microsoft\VC\v170"
VS_ARM64_PLATFORM = rf"{VS_VC_TARGETS}\Platforms\ARM64"
VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"

CONFIG_FILENAME = "qairt_env.json"

# SDK validity markers per platform
QAIRT_SDK_VALID_MARKER_WIN   = Path("bin") / "x86_64-windows-msvc" / "qnn-onnx-converter"
QAIRT_SDK_VALID_MARKER_LINUX = Path("bin") / "x86_64-linux-clang"  / "qnn-onnx-converter"
QAIRT_MIN_VERSION_LINUX_AARCH64 = "2.47.0"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _detect_vc_targets_path(vs_install_path: str) -> str:
    """Auto-detect the VCTargetsPath under a VS installation.

    Scans <vs_install_path>/MSBuild/Microsoft/VC/ for versioned directories
    (v170, v180, etc.) and returns the highest version found with a trailing
    backslash (as MSBuild expects).

    Falls back to v170 if detection fails (backwards compatible with VS 2022).
    """
    vc_base = os.path.join(vs_install_path, "MSBuild", "Microsoft", "VC")
    if not os.path.isdir(vc_base):
        # Fallback: assume v170 (VS 2022)
        return os.path.join(vs_install_path, "MSBuild", "Microsoft", "VC", "v170") + "\\"

    # Find all vNNN directories
    versions = []
    for entry in os.listdir(vc_base):
        if entry.startswith("v") and entry[1:].isdigit():
            versions.append(entry)

    if not versions:
        return os.path.join(vc_base, "v170") + "\\"

    # Sort numerically and pick the highest
    versions.sort(key=lambda v: int(v[1:]))
    best = versions[-1]
    return os.path.join(vc_base, best) + "\\"


def _detect_vs_cmake_path(vs_install_path: str) -> str:
    """Find the cmake.exe bundled with a VS installation.

    Returns the directory containing cmake.exe, or empty string if not found.
    Standard location: <vs>/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/
    """
    cmake_dir = os.path.join(
        vs_install_path, "Common7", "IDE", "CommonExtensions",
        "Microsoft", "CMake", "CMake", "bin"
    )
    if os.path.isfile(os.path.join(cmake_dir, "cmake.exe")):
        return cmake_dir
    return ""


def _root_dir(override=None):
    """Return QAIModelBuilder root directory."""
    if override:
        return Path(override).resolve()
    # This script lives in tools/, so root is one level up
    return Path(__file__).resolve().parent.parent.parent


def _config_path(root):
    return root / "data" / "config" / CONFIG_FILENAME


def _resolve_venv(root, venv_name):
    """Resolve venv path with multiple fallback strategies.

    Tries:
      Windows:
        1. Introspect sys.executable (if we're already IN this venv)
        2. USERPROFILE/AppData/Local/QAIModelBuilder/envs (sandbox-safe)
        3. LOCALAPPDATA env var (redirected in sandbox, unreliable)
        4. Project-local root/envs/<name>
      Linux:
        1. Introspect sys.executable (if we're already IN this venv)
        2. Project-local root/envs/<name> (primary — co-located with repo)
        3. $XDG_DATA_HOME/QAIModelBuilder/envs/<name> (if set)
        4. $HOME/.local/share/QAIModelBuilder/envs/<name> (fallback for pre-existing)

    Returns the first existing path, or the highest-priority candidate if none exist.
    """
    candidates = []

    # 1. sys.executable introspection (all platforms)
    try:
        exe_parts = Path(sys.executable).resolve().parts
        if venv_name in exe_parts:
            idx = exe_parts.index(venv_name)
            candidates.append(Path(*exe_parts[:idx + 1]))
    except Exception:
        pass

    if _detect_platform() == "linux":
        # 2. Project-local envs/ (primary — co-located with repo)
        candidates.append(Path(root) / "envs" / venv_name)
        # 3. XDG_DATA_HOME (if set)
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            candidates.append(Path(xdg) / "QAIModelBuilder" / "envs" / venv_name)
        # 4. ~/.local/share (fallback for pre-existing installations)
        candidates.append(Path.home() / ".local" / "share" / "QAIModelBuilder" / "envs" / venv_name)
    else:
        # 2. USERPROFILE-based (sandbox-safe: USERPROFILE is typically not redirected)
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            candidates.append(
                Path(userprofile) / "AppData" / "Local" / "QAIModelBuilder" / "envs" / venv_name
            )
        # 3. LOCALAPPDATA env var (may be redirected in sandbox)
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append(Path(local_app_data) / "QAIModelBuilder" / "envs" / venv_name)

        # 4. Project-local envs/ fallback (all non-Linux platforms)
        candidates.append(Path(root) / "envs" / venv_name)

    # Return first existing, or first candidate if none exist
    for c in candidates:
        if _python_exe(c).exists():
            return c
    return candidates[0] if candidates else Path(root) / "envs" / venv_name


def _venv_310_path(root):
    """x64/x86_64 Python 3.10 venv for QAIRT model conversion (external path)."""
    name = "venv_x86_64_310" if _detect_platform() == "linux" else ".venv_x64_310"
    return _resolve_venv(root, name)


def _venv_arm64_path(root):
    """ARM64/aarch64 Python 3.12 venv for WebUI backend (external path)."""
    name = "venv_aarch64_312" if _detect_platform() == "linux" else ".venv_arm64_313"
    return _resolve_venv(root, name)


def _python_exe(venv: Path) -> Path:
    """Return the python executable path for a venv, platform-aware."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _print_ok(msg):
    print(f"  [OK]   {msg}")


def _print_warn(msg):
    print(f"  [WARN] {msg}")


def _print_err(msg):
    print(f"  [ERR]  {msg}")


def _print_info(msg):
    print(f"  [INFO] {msg}")


def _check_linux_aarch64_version(sdk: Path):
    """Verify QAIRT SDK on Linux aarch64 meets minimum version (2.47).

    Parses version from sdk directory name, then falls back to _version /
    qairt-version.txt inside the SDK. Exits with code 1 if version is known
    and too low; only warns if version cannot be determined.
    """
    ver_str = None

    # Try directory name first (e.g. "2.48.0.260626")
    version_re = re.compile(r"^(\d+\.\d+(?:\.\d+)*)")
    m = version_re.match(sdk.name)
    if m:
        ver_str = m.group(1)

    # Fallback: look for version files inside SDK
    if ver_str is None:
        for vfile in ["_version", "qairt-version.txt"]:
            candidate = sdk / vfile
            try:
                if candidate.is_file():
                    content = candidate.read_text(encoding="utf-8", errors="ignore").strip()
                    mv = version_re.match(content)
                    if mv:
                        ver_str = mv.group(1)
                        break
            except OSError:
                pass

    if ver_str is None:
        _print_warn(
            "Cannot determine SDK version; skipping version check. "
            f"Ensure QAIRT SDK >= {QAIRT_MIN_VERSION_LINUX_AARCH64} for Linux aarch64 NPU inference."
        )
        return

    try:
        ver_tuple = tuple(int(x) for x in ver_str.split("."))
        min_tuple = tuple(int(x) for x in QAIRT_MIN_VERSION_LINUX_AARCH64.split("."))
    except ValueError:
        _print_warn(f"Cannot parse SDK version '{ver_str}'; skipping version check.")
        return

    if ver_tuple < min_tuple:
        _print_err(
            f"QAIRT SDK {ver_str} does not support Linux aarch64 NPU inference "
            f"(minimum: {QAIRT_MIN_VERSION_LINUX_AARCH64})"
        )
        sys.exit(1)


def _check_file(path, label):
    exists = Path(path).exists()
    if exists:
        _print_ok(f"{label}: {path}")
    else:
        _print_err(f"{label} NOT FOUND: {path}")
    return exists


def _run(cmd, capture=True):
    try:
        result = subprocess.run(
            cmd, capture_output=capture, text=True, shell=True
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        return result.returncode == 0, stdout, stderr
    except Exception as e:
        return False, "", str(e)


def _batch_check_imports(python, import_names):
    """Check importability of multiple packages in a single subprocess call.

    Uses importlib.util.find_spec — no module initialization, so it's fast
    even for heavy packages like torch or tensorflow.
    Returns a set of import names that are available.
    """
    parts = ["import importlib.util as _u, json as _j", "_r={}"]
    for name in import_names:
        parts.append(f"_r['{name}']=_u.find_spec('{name}') is not None")
    parts.append("print(_j.dumps(_r))")
    script = "; ".join(parts)
    ok, out, _ = _run(f'"{python}" -c "{script}"')
    if not ok or not out:
        return set()
    try:
        return {k for k, v in json.loads(out).items() if v}
    except Exception:
        return set()


# ─── Check functions ──────────────────────────────────────────────────────────

def check_venv_310(root):
    """Check x86_64 Python 3.10 venv."""
    venv = _venv_310_path(root)
    python = _python_exe(venv)
    if not python.exists():
        _print_err(f"venv_310 not found: {python}")
        return False

    ok, out, _ = _run(f'"{python}" -c "import platform; print(platform.machine(), platform.python_version())"')
    if ok:
        _print_ok(f"venv_310 Python: {out}")
        if "AMD64" not in out and "x86_64" not in out.lower():
            _print_warn("venv_310 may not be x86_64 architecture")
    else:
        _print_err(f"venv_310 Python not executable: {python}")
        return False
    return True


def check_qairt_sdk(sdk_root=None):
    """Check QAIRT SDK installation."""
    if _detect_platform() == "linux":
        arch = _detect_arch()
        if arch != "aarch64":
            _print_info("[SKIP] QAIRT SDK not applicable on Linux x86_64 (no NPU)")
            return True
        # Linux aarch64: check SDK exists and version
        sdk = Path(sdk_root or QAIRT_SDK_DEFAULT)
        if not (sdk / QAIRT_SDK_VALID_MARKER_LINUX).exists():
            _print_warn(f"QAIRT SDK not found: {sdk}")
            return False
        _check_linux_aarch64_version(sdk)
        _print_ok(f"QAIRT SDK: {sdk}")
        return True

    sdk = Path(sdk_root or QAIRT_SDK_DEFAULT)
    ok = True

    tools = [
        sdk / "bin" / "x86_64-windows-msvc" / "qnn-onnx-converter",
        sdk / "bin" / "aarch64-windows-msvc" / "qnn-model-lib-generator",
        sdk / "bin" / "aarch64-windows-msvc" / "qnn-context-binary-generator.exe",
        sdk / "lib" / "aarch64-windows-msvc" / "QnnHtp.dll",
        sdk / "lib" / "aarch64-windows-msvc" / "QnnHtpNetRunExtensions.dll",
        sdk / "lib" / "python",
    ]

    for t in tools:
        if not t.exists():
            _print_err(f"QAIRT tool missing: {t}")
            ok = False
        else:
            _print_ok(f"QAIRT tool: {t.name}")

    return ok


def check_vs2022(verbose=True):
    """Check VS 2022 Community with ARM64 support."""
    if _detect_platform() != "windows":
        _print_info("[SKIP] Visual Studio not applicable on Linux")
        return True

    ok = True

    if not Path(VSWHERE).exists():
        if verbose:
            _print_warn("vswhere.exe not found - VS 2022 may not be installed")
            _print_info("Install VS 2022 Community:")
            _print_info("  winget install Microsoft.VisualStudio.2022.Community")
        return False

    if not Path(VS_VCVARSALL).exists():
        if verbose:
            _print_err(f"vcvarsall.bat not found: {VS_VCVARSALL}")
            _print_info("Install 'Desktop development with C++' workload in VS 2022")
        ok = False
    else:
        _print_ok(f"vcvarsall.bat: {VS_VCVARSALL}")

    if not Path(VS_ARM64_PLATFORM).exists():
        if verbose:
            _print_err(f"VS ARM64 platform not found: {VS_ARM64_PLATFORM}")
            _print_info("Install 'MSVC v143 - VS 2022 C++ ARM64 build tools' via VS Installer:")
            _print_info(
                r'  "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vs_installer.exe" '
                r'modify --installPath "C:\Program Files\Microsoft Visual Studio\2022\Community" '
                r'--add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --quiet'
            )
        ok = False
    else:
        _print_ok(f"VS ARM64 platform: {VS_ARM64_PLATFORM}")

    return ok


def check_qai_appbuilder(root):
    """Check qai_appbuilder in ARM64 .venv."""
    if _detect_platform() == "linux" and _detect_arch() != "aarch64":
        _print_info("[SKIP] qai_appbuilder not available on Linux x86_64")
        return True

    venv = _venv_arm64_path(root)
    python = _python_exe(venv)
    if not python.exists():
        _print_err(f"ARM64 .venv not found: {python}")
        _print_info("Run Setup.bat first")
        return False

    ok, out, err = _run(
        f'"{python}" -c "import qai_appbuilder; '
        f'print(getattr(qai_appbuilder, \'__version__\', \'installed\'))"'
    )
    if ok:
        _print_ok(f"qai_appbuilder: {out}")
    else:
        _print_err(f"qai_appbuilder not available: {err}")
        _print_info("Run Setup.bat to install qai_appbuilder")
        return False
    return True


def check_inference_deps(root):
    """Check Pillow and numpy in ARM64 .venv."""
    venv = _venv_arm64_path(root)
    python = _python_exe(venv)
    if not python.exists():
        return False

    all_ok = True
    for pkg in ["PIL", "numpy"]:
        ok, out, _ = _run(f'"{python}" -c "import {pkg}; print({pkg}.__version__)"')
        if ok:
            _print_ok(f"{pkg}: {out}")
        else:
            _print_warn(f"{pkg} not installed in .venv")
            all_ok = False
    return all_ok


# ─── Generate config ──────────────────────────────────────────────────────────

def gen_config(root, sdk_root=None):
    """Generate data/config/qairt_env.json."""
    root = Path(root)
    # Re-evaluate QAIRT_SDK_ROOT env var at call time (not just module import time)
    # so callers setting os.environ["QAIRT_SDK_ROOT"] before calling gen_config see
    # the updated value without needing to also pass --sdk-root explicitly.
    if sdk_root is None:
        _sdk_root_env = os.environ.get("QAIRT_SDK_ROOT", "")
        if _sdk_root_env:
            sdk = Path(_sdk_root_env)
        else:
            sdk = Path(QAIRT_SDK_DEFAULT)
    else:
        sdk = Path(sdk_root)
    config_file = _config_path(root)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # ── Linux path ────────────────────────────────────────────────────────────
    if _detect_platform() == "linux":
        arch = _detect_arch()
        if not (sdk / QAIRT_SDK_VALID_MARKER_LINUX).exists():
            if arch == "aarch64":
                _print_warn(
                    "QAIRT SDK not found on Linux aarch64; NPU inference will be unavailable"
                )
            else:
                _print_warn(
                    "QAIRT SDK not found on Linux x86_64; model conversion features will be unavailable"
                )
        elif arch == "aarch64":
            _check_linux_aarch64_version(sdk)

        _actual_version = sdk.name if sdk.exists() else QAIRT_SDK_VERSION
        config = {
            "_comment": "Auto-generated by setup_qairt_env.py. Do not edit manually.",
            "_version": _actual_version,
            "qairt_sdk_root": str(sdk),
            "qairt_download_url": QAIRT_DOWNLOAD_URL,
            "python_x64_venv": str(_venv_310_path(root)),
            "python_arm64_venv": str(_venv_arm64_path(root)),
            "vs_vcvarsall": "",
            "vc_targets_path": "",
            "vs_cmake_path": "",
            "qairt_lib_dir": "",
            "platform": "linux",
            "arch": arch,
        }
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"[OK] Generated: {config_file}")
        print(f"     QAIRT SDK root: {config.get('qairt_sdk_root', '?')}")
        return config

    # ── Windows path: SDK path validation — NO fallback ───────────────────────
    # The configured version (from scripts/qairt_release.json) is the ONLY SDK
    # this project is validated against. If it is not present/complete on disk,
    # FAIL LOUDLY instead of silently retargeting a different on-disk version:
    # another QAIRT version may be ABI/operator-incompatible with our toolchain,
    # so quietly pointing qairt_env.json at it would produce a config that
    # "works" but yields wrong/broken conversions. The operator must install the
    # pinned version (or pass an explicit --sdk-root) before config generation.
    _sdk_valid_marker = sdk / QAIRT_SDK_VALID_MARKER_WIN
    if not _sdk_valid_marker.exists():
        _print_err(f"QAIRT SDK not found or incomplete: {sdk}")
        _print_err(f"Expected converter tool: {_sdk_valid_marker}")
        _print_err(
            "Refusing to fall back to a different on-disk QAIRT version — it may "
            "be incompatible with this project's toolchain."
        )
        _print_err(
            "Install the pinned QAIRT SDK (see scripts/qairt_release.json), or "
            "pass --sdk-root pointing at a valid install, then re-run."
        )
        # Non-zero exit so Setup.bat / callers can detect the failure and stop
        # rather than proceed with a bogus config.
        sys.exit(1)

    # Detect actual VS vcvarsall path
    vcvarsall = VS_VCVARSALL
    vc_targets = VS_VC_TARGETS + "\\"
    vs_cmake_path = ""

    # Check if VS is installed at a different path via vswhere
    if Path(VSWHERE).exists():
        ok, out, _ = _run(f'"{VSWHERE}" -latest -property installationPath')
        if ok and out:
            vs_path = out.strip()
            candidate_vcvarsall = os.path.join(vs_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
            # Auto-detect VC targets version (v170, v180, etc.) instead of hardcoding
            candidate_vc_targets = _detect_vc_targets_path(vs_path)
            if os.path.exists(candidate_vcvarsall):
                vcvarsall = candidate_vcvarsall
                vc_targets = candidate_vc_targets
            # Detect VS-bundled cmake (needed when system cmake is too old for this VS version)
            vs_cmake_path = _detect_vs_cmake_path(vs_path)

    # By here the pinned SDK exists on disk (else we exited above), so its
    # directory name is the authoritative installed version.
    _actual_version = sdk.name

    config = {
        "_comment": "Auto-generated by setup_qairt_env.py. Do not edit manually.",
        "_version": _actual_version,
        "qairt_sdk_root": str(sdk),
        "qairt_download_url": QAIRT_DOWNLOAD_URL,
        "python_x64_venv": str(_venv_310_path(root)),
        "python_arm64_venv": str(_venv_arm64_path(root)),
        "vs_vcvarsall": vcvarsall,
        "vc_targets_path": vc_targets,
        "vs_cmake_path": vs_cmake_path,
        "platform": "windows",
        "arch": _detect_arch(),
    }

    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"[OK] Generated: {config_file}")
    # Print only a compact summary instead of dumping the full multi-line JSON
    # (the complete config is already persisted to config_file). This keeps the
    # Setup.bat install log readable; inspect config_file for full details.
    print(f"     QAIRT SDK root: {config.get('qairt_sdk_root', '?')}")
    return config


# ─── Install Python deps ──────────────────────────────────────────────────────

def install_python_deps(root):
    """Install QAIRT converter dependencies into venv_310 (x86_64 Python 3.10).

    Package versions are pinned to match the validated QAIRT 2.45 toolchain.
    torch/torchvision use the CPU-only PyTorch index (x86_64 venv_310 is for
    model conversion only, not GPU inference).
    """
    venv = _venv_310_path(root)
    python = _python_exe(venv)

    if not python.exists():
        _python310 = shutil.which("python3.10")
        if not _python310:
            _print_err(
                "python3.10 not found on PATH — cannot create venv_310.\n"
                "  Install with: sudo apt install python3.10 python3.10-venv"
            )
            return False
        _print_info(f"Creating venv_310 at {venv} ...")
        venv.mkdir(parents=True, exist_ok=True)
        _ok, _, _err = _run(f'"{_python310}" -m venv "{venv}"', capture=False)
        if not _ok or not python.exists():
            _print_err(f"Failed to create venv_310: {_err}")
            return False
        _print_ok(f"venv_310 created: {venv}")

    # ── Pinned package list for QAIRT 2.45 converter environment ─────────────
    # venv_310 is x86_64 Python 3.10 — PyPI provides win_amd64 wheels for ALL
    # packages including torch/torchvision. NO --index-url needed here.
    #
    # Version selection rationale (validated against QAIRT 2.45):
    #   torch (latest)   : torch 2.x uses new ONNX exporter (~4x faster export for large models)
    #                      opset 18 is minimum for torch 2.x; numpy 1.26.x compatible
    #   torchvision (latest): matches latest torch
    #   onnxscript       : REQUIRED for torch 2.x new ONNX exporter (torch.onnx.export)
    #   numpy<2          : pinned to 1.26.x; onnx 1.19.1 + ml_dtypes 0.5.x validated combination
    #   tensorflow (latest): 2.16+ required; works with numpy 1.26.x
    #   onnx==1.19.1     : validated combination with numpy<2 + ml_dtypes>=0.5.0
    #   ml_dtypes>=0.5.0 : required by onnx 1.19.1 at import time; 0.5.x validated with numpy 1.26.4
    #   onnxruntime==1.23.2: latest; numpy 1.26.x compatible
    #   onnxsim==0.4.36  : validated ONNX simplification
    #   tflite==2.18.0   : validated TFLite model export
    #   opencv-python==4.11.0.86: last version supporting numpy<2 (4.12+ requires numpy>=2)
    #   NOTE: basicsr users must patch degradations.py for torchvision >= 0.16
    #         (functional_tensor removed; use try/except ImportError fallback)
    #
    # Format: (pip_spec, import_name)
    # fmt: off
    packages = [
        # ── protobuf ──────────────────────────────────────────────────────
        # Listed explicitly for visibility; uv resolves the compatible version
        # automatically alongside onnx and tensorflow in a single batch install.
        ("protobuf",                "google.protobuf"),
        # ── QAIRT converter core ──────────────────────────────────────────
        # onnx 1.19.1 + ml_dtypes>=0.5.0 + numpy<2: validated combination.
        # ml_dtypes is listed explicitly because onnx 1.19.1 imports it at
        # __init__ time; pip/uv pulls it automatically but explicit pinning
        # prevents accidental upgrade to a future incompatible version.
        ("numpy==1.26.4",            "numpy"),        # locked: validated with onnx 1.19.1 + ml_dtypes 0.5.1 + opencv 4.11
        ("ml_dtypes==0.5.1",        "ml_dtypes"),    # locked: required by onnx 1.19.1 at import time
        ("onnx==1.19.1",            "onnx"),
        ("onnxruntime==1.23.2",     "onnxruntime"),
        ("onnxsim==0.4.36",         "onnxsim"),
        ("pandas",                  "pandas"),       # required by qnn-onnx-converter arch_linter
        # ── Model export (pinned versions) ───────────────────────────────
        # NOTE: x86_64 venv_310 uses standard PyPI (win_amd64 wheels available)
        # NO --index-url needed for x86_64; only ARM64 .venv needs --index-url for torch/torchvision
        ("torch==2.11.0",           "torch"),        # validated: torch 2.x ~4x faster ONNX export vs 1.13.1
        ("torchvision==0.26.0",     "torchvision"),  # validated: matches torch 2.11.0
        ("onnxscript",              "onnxscript"),   # REQUIRED for torch 2.x ONNX exporter
        # ── TensorFlow / TFLite ──────────────────────────────────────────
        ("tensorflow==2.21.0",              "tensorflow"),
        ("tflite==2.18.0",          "tflite"),
        # ── Image pre/post-processing ─────────────────────────────────────
        ("Pillow",                  "PIL"),
        ("opencv-python==4.11.0.86", "cv2"),         # locked: last version supporting numpy<2; 4.12+ requires numpy>=2
        # ── Data science / utilities ──────────────────────────────────────
        ("scipy",                   "scipy"),
        ("matplotlib",              "matplotlib"),
        ("tqdm",                    "tqdm"),
        ("pyyaml",                  "yaml"),         # required by qai_inspect_onnxio.py
        ("requests",                "requests"),
    ]
    # fmt: on

    # Linux: substitute opencv-python → opencv-python-headless (no GUI deps)
    if _detect_platform() == "linux":
        packages = [
            (spec.replace("opencv-python==", "opencv-python-headless=="), imp)
            if spec.startswith("opencv-python==") else (spec, imp)
            for spec, imp in packages
        ]

    uv_linux = root / "data" / "bin" / "uv" / "uv"
    uv_win = root / "data" / "bin" / "uv" / "uv.exe"
    if _detect_platform() == "linux":
        _uv_which = shutil.which("uv")
        uv = uv_linux if uv_linux.exists() else (Path(_uv_which) if _uv_which else None)
    else:
        uv = uv_win if uv_win.exists() else None

    import_names = [imp for _, imp in packages]
    available = _batch_check_imports(python, import_names)

    to_install = []
    for pip_spec, import_name in packages:
        if import_name in available:
            _print_ok(f"{pip_spec} already installed")
        else:
            to_install.append(pip_spec)

    if not to_install:
        _print_ok("All packages already installed")
        return True

    print(f"[INFO] Installing {len(to_install)} packages into venv_310...")

    if uv and uv.exists():
        specs = " ".join(f'"{s}"' for s in to_install)
        print(f"[INFO] Batch-installing {len(to_install)} packages via uv...")
        ok, _, _ = _run(f'"{uv}" pip install {specs} --python "{python}"', capture=False)
        if not ok:
            _print_warn("Batch install failed, retrying one-by-one...")
            for spec in to_install:
                print(f"[INFO] Installing {spec}...")
                ok2, _, _ = _run(f'"{uv}" pip install "{spec}" --python "{python}"', capture=False)
                if not ok2:
                    _print_err(f"Failed to install {spec} (non-fatal)")
    else:
        _uv_label = "uv" if _detect_platform() == "linux" else "uv.exe"
        _print_warn(f"{_uv_label} not found, falling back to pip (slower)...")
        for pip_spec in to_install:
            print(f"[INFO] Installing {pip_spec}...")
            ok, _, err = _run(f'"{python}" -m pip install "{pip_spec}" --quiet', capture=False)
            if not ok:
                _print_err(f"Failed to install {pip_spec} (non-fatal)")

    return True


def install_inference_deps(root):
    """Install inference dependencies into ARM64 .venv."""
    venv = _venv_arm64_path(root)
    python = _python_exe(venv)

    if not python.exists():
        if _detect_platform() == "linux":
            _python312 = shutil.which("python3.12")
            if not _python312:
                _print_err(
                    "python3.12 not found on PATH — cannot create venv_aarch64_312.\n"
                    "  Install with: sudo apt install python3.12 python3.12-venv"
                )
                return False
            _print_info(f"Creating venv_aarch64_312 at {venv} ...")
            venv.mkdir(parents=True, exist_ok=True)
            _ok, _, _err = _run(f'"{_python312}" -m venv "{venv}"', capture=False)
            if not _ok or not python.exists():
                _print_err(f"Failed to create venv_aarch64_312: {_err}")
                return False
            _print_ok(f"venv_aarch64_312 created: {venv}")
        else:
            # Windows: venv must be pre-created by Setup.bat; not created here.
            _print_err(f"ARM64 .venv not found: {python}")
            return False

    # Find uv (platform-aware)
    if _detect_platform() == "linux":
        uv_local = root / "data" / "bin" / "uv" / "uv"
        _uv_which = shutil.which("uv")
        uv = uv_local if uv_local.exists() else (Path(_uv_which) if _uv_which else None)
        if not uv:
            _print_err("uv not found; install uv or add it to PATH")
            return False
    else:
        uv = root / "data" / "bin" / "uv" / "uv.exe"
        if not uv.exists():
            _print_err(f"uv.exe not found: {uv}")
            return False

    # ── Local ARM64 wheels (Windows only; not available for Linux aarch64 on PyPI) ──
    if _detect_platform() != "linux":
        whl_dir = root / "vendor" / "whl"
        arm64_local_wheels = [
            # (whl_filename,                                                import_name)
            ("numpy-2.3.1-cp313-cp313-win_arm64.whl",                      "numpy"),
            ("opencv_python_headless-4.10.0.84-cp313-cp313-win_arm64.whl", "cv2"),
            ("MarkupSafe-2.1.5-cp313-cp313-win_arm64.whl",                 "markupsafe"),
            ("pyclipper-1.4.0-cp313-cp313-win_arm64.whl",                  "pyclipper"),
            ("soundfile-0.13.1-cp313-cp313-win_arm64.whl",                  "soundfile"),
            ("kaldi_native_fbank-1.22.3-cp313-cp313-win_arm64.whl",         "kaldi_native_fbank"),
        ]
        local_import_names = [imp for _, imp in arm64_local_wheels]
        available_local = _batch_check_imports(python, local_import_names)
        for whl_file, import_name in arm64_local_wheels:
            if import_name in available_local:
                _print_ok(f"{whl_file} already installed")
                continue
            whl_path = whl_dir / whl_file
            if not whl_path.exists():
                _print_warn(f"Local ARM64 wheel not found: {whl_path} — skipping")
                continue
            print(f"[INFO] Installing {whl_file} (local ARM64 wheel)...")
            ok, _, err = _run(f'"{uv}" pip install "{whl_path}" --python "{python}"', capture=False)
            if not ok:
                _print_err(f"Failed to install {whl_file}: {err}")
                print(f"[WARN] Skipping {whl_file} (non-fatal)")
                continue
            _print_ok(f"{whl_file} installed")

    # Common inference + post-processing libraries for ARM64 .venv
    # Format: (pip_spec, import_name, use_torch_index)
    # use_torch_index=True for torch/torchvision when ARM64 Windows wheels are needed.
    # On Linux: torch/torchvision aarch64 CPU wheels use the same PyTorch index.
    # fmt: off
    if _detect_platform() == "linux":
        packages = [
            # ── Image processing ──────────────────────────────────────────────
            ("Pillow",              "PIL",          False),
            ("opencv-python-headless", "cv2",       False),
            # ── Numerical / data ──────────────────────────────────────────────
            ("numpy",               "numpy",        False),
            ("scipy",               "scipy",        False),
            # ── Visualization / utilities ─────────────────────────────────────
            ("matplotlib",          "matplotlib",   False),
            ("tqdm",                "tqdm",         False),
            ("pyyaml",              "yaml",         False),
            ("requests",            "requests",     False),
            # ── onnxruntime (CPU; onnxruntime-qnn is Windows ARM64 only) ──────
            ("onnxruntime",         "onnxruntime",  False),
            # ── PyTorch (aarch64 Linux CPU wheels via PyTorch index) ───────────
            ("torch",               "torch",        True),
            ("torchvision",         "torchvision",  True),
        ]
        # qai_appbuilder is Windows ARM64 only — skip on Linux
        _print_info("[SKIP] qai_appbuilder not available on Linux")
    else:
        packages = [
            # ── Image processing ──────────────────────────────────────────────
            ("Pillow",          "PIL",          False),
            # opencv-python: installed from vendor/whl/ (ARM64 local wheel), not PyPI
            # ── Numerical / data ──────────────────────────────────────────────
            ("numpy",           "numpy",        False),
            ("scipy",           "scipy",        False),
            # ── Visualization / utilities ─────────────────────────────────────
            ("matplotlib",      "matplotlib",   False),
            ("tqdm",            "tqdm",         False),
            ("pyyaml",          "yaml",         False),
            ("requests",        "requests",     False),
            # ── ONNXRuntime-QNN (NPU + CPU execution providers) ───────────────
            # Provides QNNExecutionProvider so PRECOMPILED_QNN_ONNX / EPContext
            # `.onnx` models load on the Hexagon NPU (CPU fallback included). The
            # import name is plain ``onnxruntime``; in the ARM64 inference venv we
            # never install the *standard* ``onnxruntime`` (that one lives in the
            # x64 conversion venv), so probing ``onnxruntime`` here is safe — a hit
            # means the QNN build is already present. ARM64 Windows cp313 wheel is
            # on PyPI, so NO --index-url. Mutually exclusive with standard
            # ``onnxruntime`` (installing one uninstalls the other).
            ("onnxruntime-qnn==1.24.4", "onnxruntime", False),
            # ── PyTorch (ARM64 Windows — ONLY these need --index-url) ─────────
            # ARM64 Windows wheels are NOT on PyPI; must use PyTorch ARM64 index
            ("torch",           "torch",        True),
            ("torchvision",     "torchvision",  True),
        ]
    # fmt: on
    print(f"[INFO] Installing {len(packages)} inference packages into .venv...")

    import_names = [imp for _, imp, _ in packages]
    available = _batch_check_imports(python, import_names)

    for pip_spec, import_name, use_torch_index in packages:
        if import_name in available:
            _print_ok(f"{pip_spec} already installed")
            continue

        print(f"[INFO] Installing {pip_spec}...")
        if use_torch_index:
            # torch/torchvision must use the PyTorch index. The index layout
            # differs by platform: Windows ARM64 wheels are served from the
            # root index (/whl); Linux uses the CPU-only sub-index (/whl/cpu).
            # Do NOT force /cpu on Windows — win_arm64 torch wheels are not
            # published there and the install would fail (regression).
            if _detect_platform() == "linux":
                _torch_index = "https://download.pytorch.org/whl/cpu"
            else:
                _torch_index = "https://download.pytorch.org/whl"
            install_cmd = (
                f'"{uv}" pip install "{pip_spec}" '
                f'--python "{python}" '
                f'--index-url {_torch_index}'
            )
        else:
            # All other packages: standard PyPI, no --index-url
            install_cmd = f'"{uv}" pip install "{pip_spec}" --python "{python}"'

        ok, out, err = _run(install_cmd, capture=False)
        if not ok:
            _print_err(f"Failed to install {pip_spec}: {err}")
            print(f"[WARN] Skipping {pip_spec} (non-fatal)")
            continue
        _print_ok(f"{pip_spec} installed")

    # Auto-chain App Builder Pack dependency aggregation. Done here (rather
    # than from a separate .bat step) so the install flow — Setup.bat Step 8,
    # which only invokes `--install-inference-deps` — automatically gets
    # the merged Pack deps without any .bat edits. The aggregator is
    # best-effort and never fails this function.
    try:
        install_app_builder_deps(root)
    except Exception as e:  # noqa: BLE001 — never let aggregation crash setup
        _print_warn(f"App Builder Pack deps aggregation skipped: {e}")

    return True


# ─── App Builder Pack dep aggregation ─────────────────────────────────────────
#
# The pure aggregation logic (parse / normalize / dedup / skip / --no-deps
# bucketing) was sunk into ``scripts/setup/_pack_deps.py`` so this module and
# the install-time thin entry ``scripts/setup/install_app_builder_deps.py``
# share a single, unit-tested, platform-neutral implementation (refactor
# docs/85-tasks/install-uninstall-v1-alignment-plan.md, D-1). The actual install
# is delegated to that thin entry's ``main()`` which also applies ``--no-deps``
# for openai-whisper (V1 既有缺陷修正, D-2).


def install_app_builder_deps(root):
    """Aggregate every Pack's requirements.txt and install into the ARM64 venv.

    Delegates to the canonical thin entry
    ``scripts.setup.install_app_builder_deps:main`` (shared aggregation via
    :mod:`scripts.setup._pack_deps`, with ``--no-deps`` handling for
    openai-whisper). Kept as a thin shim so the historical auto-chain from
    :func:`install_inference_deps` keeps working without a separate .bat step.

    Non-fatal: individual package failures only warn; this returns True unless
    a prerequisite (venv / uv.exe) is missing.
    """
    root = Path(root)
    venv = _venv_arm64_path(root)
    python = _python_exe(venv)
    if not python.exists():
        _print_err(f"ARM64 .venv not found: {python}")
        return False

    try:
        from scripts.setup.install_app_builder_deps import main as _pack_main
    except ModuleNotFoundError:
        sys.path.insert(0, str(root))
        from scripts.setup.install_app_builder_deps import main as _pack_main

    _pack_main()
    return True


# ─── Verify ───────────────────────────────────────────────────────────────────

def verify_all(root, sdk_root=None):
    """Run all checks and report status."""
    root = Path(root)
    print("\n" + "=" * 60)
    print("  model-builder Environment Verification")
    print("=" * 60)

    results = {}

    print("\n[1] x86_64 Python 3.10 venv (for model conversion):")
    results["venv_310"] = check_venv_310(root)

    print(f"\n[2] QAIRT SDK {QAIRT_SDK_VERSION}:")
    results["qairt_sdk"] = check_qairt_sdk(sdk_root)

    print("\n[3] VS 2022 Community (ARM64 compilation):")
    results["vs2022"] = check_vs2022()

    print("\n[4] qai_appbuilder in ARM64 .venv (for inference):")
    results["qai_appbuilder"] = check_qai_appbuilder(root)

    print("\n[5] Inference dependencies (Pillow, numpy):")
    results["inference_deps"] = check_inference_deps(root)

    print("\n" + "=" * 60)
    all_ok = all(results.values())
    if all_ok:
        print("  [OK] All checks passed! model-builder is ready.")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  [WARN] Some checks failed: {', '.join(failed)}")
        print("  Run Setup.bat (Step 8) to fix missing dependencies.")
    print("=" * 60 + "\n")

    return all_ok


# ─── Check all (JSON output) ──────────────────────────────────────────────────


def _inspect_qairt_sdk(sdk: Path):
    """Return rich info about QAIRT SDK status.

    Distinguishes between "not found" and "permission denied", and enumerates
    sibling version directories in the parent so AI agents / users can tell
    whether the requested version is actually installed but unreadable, vs
    genuinely missing.
    """
    sdk = Path(sdk)
    info = {
        "path": str(sdk),
        "exists": False,
        "parent_accessible": None,
        "available_versions": [],
        "valid_versions": [],
        "suggested": None,
        "error": None,
    }
    exe_rel = Path("bin") / "aarch64-windows-msvc" / "qnn-context-binary-generator.exe"

    # 1) Check whether the requested SDK itself has the expected binary
    try:
        info["exists"] = (sdk / exe_rel).exists()
    except (PermissionError, OSError) as e:
        info["error"] = f"permission_denied: {e}"

    # 2) Enumerate sibling versions in the parent directory
    parent = sdk.parent
    version_re = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
    try:
        parent_exists = parent.exists()
    except (PermissionError, OSError) as e:
        info["parent_accessible"] = False
        if info["error"] is None:
            info["error"] = f"parent_permission_denied: {e}"
        parent_exists = False

    if parent_exists:
        try:
            entries = [p for p in parent.iterdir() if p.is_dir() and version_re.match(p.name)]
            info["parent_accessible"] = True

            def _ver_key(name):
                parts = name.split(".")
                try:
                    return tuple(int(x) for x in parts)
                except ValueError:
                    return tuple()

            # Sort descending by numeric version tuple
            entries.sort(key=lambda p: _ver_key(p.name), reverse=True)
            info["available_versions"] = [p.name for p in entries]

            valid = []
            for p in entries:
                try:
                    if (p / exe_rel).exists():
                        valid.append(p.name)
                except (PermissionError, OSError):
                    # Skip unreadable subdir but don't fail overall
                    continue
            info["valid_versions"] = valid

            # Pick suggested: prefer requested SDK name if it's valid, else highest valid
            requested = sdk.name
            if requested in valid:
                info["suggested"] = str(parent / requested)
            elif valid:
                info["suggested"] = str(parent / valid[0])
        except (PermissionError, OSError) as e:
            info["parent_accessible"] = False
            if info["error"] is None:
                info["error"] = f"parent_permission_denied: {e}"
    else:
        if info["parent_accessible"] is None:
            info["parent_accessible"] = False
        if info["error"] is None:
            info["error"] = "not_found"

    return info


def check_all_json(root, sdk_root=None):
    """Check all dependencies and output JSON status."""
    root = Path(root)

    def _exists(p):
        return Path(p).exists()

    venv_310 = _venv_310_path(root)
    venv_arm64 = _venv_arm64_path(root)
    sdk = Path(sdk_root or QAIRT_SDK_DEFAULT)

    status = {
        "venv_310": {
            "path": str(venv_310),
            "exists": _exists(_python_exe(venv_310)),
        },
        "venv_arm64": {
            "path": str(venv_arm64),
            "exists": _exists(_python_exe(venv_arm64)),
        },
        "qairt_sdk": _inspect_qairt_sdk(sdk),
        "vs2022": (
            {
                "vcvarsall": VS_VCVARSALL,
                "vcvarsall_exists": _exists(VS_VCVARSALL),
                "arm64_platform": VS_ARM64_PLATFORM,
                "arm64_platform_exists": _exists(VS_ARM64_PLATFORM),
            }
            if _detect_platform() == "windows"
            else {"applicable": False, "reason": "Visual Studio is Windows-only"}
        ),
        "config": {
            "path": str(_config_path(root)),
            "exists": _exists(_config_path(root)),
        },
        "platform": _detect_platform(),
    }

    print(json.dumps(status, indent=2))
    return status


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Declare globals before any use
    global QAIRT_SDK_VERSION, QAIRT_SDK_DEFAULT, QAIRT_DOWNLOAD_URL

    parser = argparse.ArgumentParser(description="QAIModelBuilder QAIRT Environment Setup Helper")
    parser.add_argument("--root", help="QAIModelBuilder root directory (default: parent of this script)")
    parser.add_argument("--sdk-root", help=f"QAIRT SDK root (default: {QAIRT_SDK_DEFAULT})")
    parser.add_argument("--sdk-version", help=f"QAIRT SDK version (default: {QAIRT_SDK_VERSION}). "
                        "Also sets QAIRT_VERSION env var for this process.")
    parser.add_argument("--download-url", help="QAIRT SDK download URL override. "
                        "Also sets QAIRT_DOWNLOAD_URL env var for this process.")
    parser.add_argument("--gen-config", action="store_true", help="Generate data/config/qairt_env.json")
    parser.add_argument("--verify", action="store_true", help="Verify all dependencies")
    parser.add_argument("--check-all", action="store_true", help="Check all dependencies (JSON output)")
    parser.add_argument("--install-python-deps", action="store_true", help="Install Python deps into venv_310")
    parser.add_argument("--install-inference-deps", action="store_true", help="Install inference deps into .venv")
    parser.add_argument(
        "--install-app-builder-deps",
        action="store_true",
        help="Aggregate factory/app_builder/models/*/requirements.txt and "
             "install into the ARM64 .venv (called automatically by "
             "--install-inference-deps; this flag is for re-running standalone)."
    )

    args = parser.parse_args()

    # Apply version/URL overrides before using module-level constants
    if args.sdk_version:
        os.environ["QAIRT_VERSION"] = args.sdk_version
        QAIRT_SDK_VERSION = args.sdk_version
        if not args.sdk_root:
            if sys.platform == "win32":
                QAIRT_SDK_DEFAULT = rf"C:\Qualcomm\AIStack\QAIRT\{QAIRT_SDK_VERSION}"
            else:
                QAIRT_SDK_DEFAULT = f"/opt/qcom/aistack/qairt/{QAIRT_SDK_VERSION}"
        QAIRT_DOWNLOAD_URL = (
            f"https://softwarecenter.qualcomm.com/api/download/software/sdks/"
            f"Qualcomm_AI_Runtime_Community/All/{QAIRT_SDK_VERSION}/v{QAIRT_SDK_VERSION}.zip"
        )
    if args.download_url:
        os.environ["QAIRT_DOWNLOAD_URL"] = args.download_url
        QAIRT_DOWNLOAD_URL = args.download_url

    root = _root_dir(args.root)

    if args.gen_config:
        gen_config(root, args.sdk_root)
    elif args.verify:
        ok = verify_all(root, args.sdk_root)
        sys.exit(0 if ok else 1)
    elif args.check_all:
        check_all_json(root, args.sdk_root)
    elif args.install_python_deps:
        ok = install_python_deps(root)
        sys.exit(0 if ok else 1)
    elif args.install_inference_deps:
        ok = install_inference_deps(root)
        sys.exit(0 if ok else 1)
    elif args.install_app_builder_deps:
        ok = install_app_builder_deps(root)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
