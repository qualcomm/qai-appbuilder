# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
setup_qairt_env.py - AIPC QAIRT Environment Setup Helper

Handles environment detection, configuration generation, and verification
for the aipc integration (x64 Python 3.10 only).

Usage:
    python setup_qairt_env.py --gen-config [--root ROOT_DIR]
    python setup_qairt_env.py --verify
    python setup_qairt_env.py --check-all
    python setup_qairt_env.py --install-python-deps
"""

import argparse
import json
import os
import subprocess
import sys
import platform
from pathlib import Path


# ─── Constants ────────────────────────────────────────────────────────────────

# Default QAIRT SDK version — override via env var QAIRT_VERSION or --sdk-version CLI arg
QAIRT_SDK_VERSION = os.environ.get("QAIRT_VERSION", "2.45.40.260406")
QAIRT_SDK_DEFAULT = os.environ.get(
    "QAIRT_SDK_ROOT",
    rf"C:\Qualcomm\AIStack\QAIRT\{QAIRT_SDK_VERSION}"
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _root_dir(override=None):
    """Return project root directory."""
    if override:
        return Path(override).resolve()
    # This script lives in scripts/, so root is one level up
    return Path(__file__).resolve().parent.parent


def _config_path(root):
    return root / "config" / CONFIG_FILENAME


def _venv_310_path(root):
    return root / "venv" / ".venv_x64_310"


def _print_ok(msg):
    print(f"  [OK]   {msg}")


def _print_warn(msg):
    print(f"  [WARN] {msg}")


def _print_err(msg):
    print(f"  [ERR]  {msg}")


def _print_info(msg):
    print(f"  [INFO] {msg}")


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
    python = venv / "Scripts" / "python.exe"
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


# ─── Generate config ──────────────────────────────────────────────────────────

def gen_config(root, sdk_root=None):
    """Generate config/qairt_env.json."""
    root = Path(root)
    sdk = Path(sdk_root or QAIRT_SDK_DEFAULT)

    # Detect actual VS vcvarsall path
    vcvarsall = VS_VCVARSALL
    vc_targets = VS_VC_TARGETS + "\\"

    # Check if VS is installed at a different path via vswhere
    if Path(VSWHERE).exists():
        ok, out, _ = _run(f'"{VSWHERE}" -latest -property installationPath')
        if ok and out:
            vs_path = out.strip()
            candidate_vcvarsall = os.path.join(vs_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
            candidate_vc_targets = os.path.join(vs_path, "MSBuild", "Microsoft", "VC", "v170") + "\\"
            if os.path.exists(candidate_vcvarsall):
                vcvarsall = candidate_vcvarsall
                vc_targets = candidate_vc_targets

    config = {
        "_comment": "Auto-generated by setup_qairt_env.py. Do not edit manually.",
        "_version": QAIRT_SDK_VERSION,
        "qairt_sdk_root": str(sdk),
        "qairt_download_url": QAIRT_DOWNLOAD_URL,
        "python_x64_venv": str(_venv_310_path(root)),
        "vs_vcvarsall": vcvarsall,
        "vc_targets_path": vc_targets,
    }

    config_file = _config_path(root)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"[OK] Generated: {config_file}")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    return config


# ─── Install Python deps ──────────────────────────────────────────────────────

def install_python_deps(root):
    """Install QAIRT converter dependencies into venv_310 (x86_64 Python 3.10).

    Packages are installed in batches to avoid dependency resolution timeouts
    when all 20+ packages are resolved simultaneously (torch + tensorflow +
    numpy constraints cause SAT solver backtracking).

    Package versions are pinned to match the validated QAIRT 2.45 toolchain.
    """
    venv = _venv_310_path(root)
    python = venv / "Scripts" / "python.exe"

    if not python.exists():
        _print_err(f"venv_310 not found: {python}")
        return False

    # ── Batched package list for QAIRT 2.45 converter environment ─────────────
    # Packages are split into batches to keep dependency resolution fast.
    # Each batch is resolved independently by uv.
    #
    # Format per batch: list of (pip_spec, import_name)
    # fmt: off
    batches = [
        # ── Batch 1: qai_appbuilder (URL wheel, install first) ────────────
        {
            "label": "qai_appbuilder (x64 wheel)",
            "packages": [
                ("https://github.com/qualcomm/qai-appbuilder/releases/download/v2.45.0/qai_appbuilder-2.45.0-cp310-cp310-win_amd64.whl", "qai_appbuilder"),
            ],
        },
        # ── Batch 2: Core numerical + ONNX (pin numpy first) ──────────────
        {
            "label": "numpy + ONNX ecosystem",
            "packages": [
                ("numpy==1.26.4",            "numpy"),        # locked: validated with onnx 1.19.1 + ml_dtypes 0.5.1
                ("ml_dtypes==0.5.1",        "ml_dtypes"),    # locked: required by onnx 1.19.1 at import time
                ("protobuf",                "google.protobuf"),
                ("onnx==1.19.1",            "onnx"),
                ("onnxruntime==1.23.2",     "onnxruntime"),
                ("onnxsim==0.4.36",         "onnxsim"),
                ("pandas",                  "pandas"),       # required by qnn-onnx-converter arch_linter
            ],
        },
        # ── Batch 3: PyTorch (large, separate resolution) ──────────────────
        {
            "label": "PyTorch + torchvision",
            "packages": [
                ("torch==2.11.0",           "torch"),        # validated: torch 2.x ~4x faster ONNX export
                ("torchvision==0.26.0",     "torchvision"),  # validated: matches torch 2.11.0
                ("onnxscript",              "onnxscript"),   # REQUIRED for torch 2.x ONNX exporter
            ],
        },
        # ── Batch 4: TensorFlow (large, separate resolution) ──────────────
        {
            "label": "TensorFlow + TFLite",
            "packages": [
                ("tensorflow==2.21.0",      "tensorflow"),
                ("tflite==2.18.0",          "tflite"),
            ],
        },
        # ── Batch 5: Image processing + utilities (lightweight) ────────────
        {
            "label": "Image processing + utilities",
            "packages": [
                ("Pillow",                  "PIL"),
                ("opencv-python==4.11.0.86", "cv2"),         # locked: last version supporting numpy<2
                ("scipy",                   "scipy"),
                ("matplotlib",              "matplotlib"),
                ("tqdm",                    "tqdm"),
                ("pyyaml",                  "yaml"),         # required by qai_inspect_onnxio.py
                ("requests",                "requests"),
            ],
        },
    ]
    # fmt: on

    uv = root / "bin" / "uv" / "uv.exe"

    # Collect all import names for batch availability check
    all_packages = []
    for batch in batches:
        all_packages.extend(batch["packages"])

    import_names = [imp for _, imp in all_packages]
    available = _batch_check_imports(python, import_names)

    # Report already-installed packages
    total_to_install = 0
    for batch in batches:
        for pip_spec, import_name in batch["packages"]:
            if import_name in available:
                _print_ok(f"{import_name} already installed")
            else:
                total_to_install += 1

    if total_to_install == 0:
        _print_ok("All packages already installed")
        return True

    print(f"[INFO] {total_to_install} packages to install across {len(batches)} batches...")

    # Install batch by batch
    for i, batch in enumerate(batches, 1):
        to_install = [
            pip_spec for pip_spec, import_name in batch["packages"]
            if import_name not in available
        ]
        if not to_install:
            continue

        label = batch["label"]
        print(f"\n[INFO] Batch {i}/{len(batches)}: {label} ({len(to_install)} packages)...")

        if uv.exists():
            specs = " ".join(f'"{s}"' for s in to_install)
            ok, _, _ = _run(f'"{uv}" pip install {specs} --python "{python}"', capture=False)
            if not ok:
                _print_warn(f"Batch {i} failed, retrying one-by-one...")
                for spec in to_install:
                    print(f"[INFO] Installing {spec}...")
                    ok2, _, _ = _run(f'"{uv}" pip install "{spec}" --python "{python}"', capture=False)
                    if not ok2:
                        _print_err(f"Failed to install {spec} (non-fatal)")
        else:
            _print_warn(f"uv.exe not found at {uv}, falling back to pip...")
            for pip_spec in to_install:
                print(f"[INFO] Installing {pip_spec}...")
                ok, _, _ = _run(f'"{python}" -m pip install "{pip_spec}" --quiet', capture=False)
                if not ok:
                    _print_err(f"Failed to install {pip_spec} (non-fatal)")

    print()
    _print_ok("Python dependency installation complete")
    return True


# ─── Verify ───────────────────────────────────────────────────────────────────

def verify_all(root, sdk_root=None):
    """Run all checks and report status."""
    root = Path(root)
    print("\n" + "=" * 60)
    print("  AIPC Environment Verification")
    print("=" * 60)

    results = {}

    print("\n[1] x86_64 Python 3.10 venv (for model conversion):")
    results["venv_310"] = check_venv_310(root)

    print("\n[2] QAIRT SDK 2.45.40.260406:")
    results["qairt_sdk"] = check_qairt_sdk(sdk_root)

    print("\n[3] VS 2022 Community (ARM64 compilation):")
    results["vs2022"] = check_vs2022()

    print("\n" + "=" * 60)
    all_ok = all(results.values())
    if all_ok:
        print("  [OK] All checks passed! AIPC environment is ready.")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  [WARN] Some checks failed: {', '.join(failed)}")
        print("  Run Setup_Env.bat to fix missing dependencies.")
    print("=" * 60 + "\n")

    return all_ok


# ─── Check all (JSON output) ──────────────────────────────────────────────────

def check_all_json(root, sdk_root=None):
    """Check all dependencies and output JSON status."""
    root = Path(root)

    def _exists(p):
        return Path(p).exists()

    venv_310 = _venv_310_path(root)
    sdk = Path(sdk_root or QAIRT_SDK_DEFAULT)

    status = {
        "venv_310": {
            "path": str(venv_310),
            "exists": _exists(venv_310 / "Scripts" / "python.exe"),
        },
        "qairt_sdk": {
            "path": str(sdk),
            "exists": _exists(sdk / "bin" / "aarch64-windows-msvc" / "qnn-context-binary-generator.exe"),
        },
        "vs2022": {
            "vcvarsall": VS_VCVARSALL,
            "vcvarsall_exists": _exists(VS_VCVARSALL),
            "arm64_platform": VS_ARM64_PLATFORM,
            "arm64_platform_exists": _exists(VS_ARM64_PLATFORM),
        },
        "config": {
            "path": str(_config_path(root)),
            "exists": _exists(_config_path(root)),
        },
    }

    print(json.dumps(status, indent=2))
    return status


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Declare globals before any use
    global QAIRT_SDK_VERSION, QAIRT_SDK_DEFAULT, QAIRT_DOWNLOAD_URL

    parser = argparse.ArgumentParser(description="AIPC QAIRT Environment Setup Helper")
    parser.add_argument("--root", help="Project root directory (default: parent of this script)")
    parser.add_argument("--sdk-root", help=f"QAIRT SDK root (default: {QAIRT_SDK_DEFAULT})")
    parser.add_argument("--sdk-version", help=f"QAIRT SDK version (default: {QAIRT_SDK_VERSION}). "
                        "Also sets QAIRT_VERSION env var for this process.")
    parser.add_argument("--download-url", help="QAIRT SDK download URL override. "
                        "Also sets QAIRT_DOWNLOAD_URL env var for this process.")
    parser.add_argument("--gen-config", action="store_true", help="Generate config/qairt_env.json")
    parser.add_argument("--verify", action="store_true", help="Verify all dependencies")
    parser.add_argument("--check-all", action="store_true", help="Check all dependencies (JSON output)")
    parser.add_argument("--install-python-deps", action="store_true", help="Install Python deps into venv_310")

    args = parser.parse_args()

    # Apply version/URL overrides before using module-level constants
    if args.sdk_version:
        os.environ["QAIRT_VERSION"] = args.sdk_version
        QAIRT_SDK_VERSION = args.sdk_version
        if not args.sdk_root:
            QAIRT_SDK_DEFAULT = rf"C:\Qualcomm\AIStack\QAIRT\{QAIRT_SDK_VERSION}"
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
