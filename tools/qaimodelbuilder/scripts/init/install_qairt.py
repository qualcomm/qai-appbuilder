# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

r"""``scripts.init.install_qairt`` — QAIRT SDK install orchestrator (PR-902).

Replaces the legacy ``Setup_Builder_Env.bat`` workflow with a Python
script that fits the Clean Cutover release-pipeline contract. The
script is a single-purpose orchestrator; installation steps are
implemented as small, testable functions.

What the script does
--------------------
1. **Prerequisites**: verify that ``uv.exe`` and the ARM64 inference
   venv (``.venv_arm64_313``) exist; warn but continue if either is
   missing so the operator can run a partial install.
2. **x86_64 Python 3.10 venv** (``.venv_x64_310``): created via
   ``uv venv --python cpython-3.10-windows-x86_64``. Required for
   QAIRT model conversion (the converter ships only x86_64 binaries).
3. **QAIRT SDK <QAIRT SDK version>**: download via ``aria2c`` (preferred,
   16-thread + resume) or ``Invoke-WebRequest`` fallback; extract to
   ``%QAIRT_SDK_ROOT%`` (default ``C:\Qualcomm\AIStack\QAIRT\<ver>``).
   If a vendored zip exists at ``vendor/qairt/v<ver>.zip``, it is used
   directly without redownload.
4. **qairt_env.json**: write to
   ``%LOCALAPPDATA%\QAIModelBuilder\config\qairt_env.json`` (the
   canonical Clean-Cutover location per S8 audit P0-L2). The file
   tells the SKILL system where to find the SDK and which Python venv
   to use for conversion.

Cross-platform
--------------
QAIRT SDK is Windows-only; on non-Windows hosts the script logs a
clear "not supported" message and exits 0 (a no-op so CI containers
don't fail). All Windows-specific calls are guarded by
``sys.platform == "win32"`` checks.

Out of scope (delegated to other PRs)
-------------------------------------
* Inference dep installation into the ARM64 venv (numpy / Pillow /
  torch / etc.) — handled at runtime by the model_runtime context's
  install hooks; not the supervisor's job.
* App-builder per-pack pip dep aggregation — handled by L3 lane
  (PR-307 / Sticky Worker warm pool).
* SKILL discovery / loader — handled by L1 lane.

Console script registration (PR-902 §10 hand-off to I1; updated CLI D3
2026-06-10): the standalone ``qai-install-qairt`` console-script was
retired by Desktop App Plan §2.4 and is now exposed via the unified
``qai`` dispatcher::

    qai install-qairt                       # invoke through the dispatcher
    python -m scripts.init.install_qairt    # direct module invocation
    python -m scripts.init.install_qairt --sdk-root "D:\QAIRT\<version>"
    python -m scripts.init.install_qairt --check       # verify only

Exit codes
----------
* 0  — success (or non-Windows no-op)
* 1  — install failed (download / extract / write_config)
* 2  — argparse rejected arguments
* 3  — prerequisite missing (uv.exe absent and operator did not pass
       ``--allow-missing-uv``)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# ----- Constants ------------------------------------------------------------

# Single source of truth for the default QAIRT version: scripts/qairt_release.json
# (see scripts/qairt_version.py). Read directly by path so this works whether the
# module is imported as ``scripts.init.install_qairt`` or loaded standalone in tests.
def _load_default_qairt_version() -> str:
    release_json = Path(__file__).resolve().parent.parent / "qairt_release.json"
    with open(release_json, encoding="utf-8") as fh:
        return json.load(fh)["qairt_version"]


DEFAULT_QAIRT_VERSION = _load_default_qairt_version()
DEFAULT_QAIRT_SDK_ROOT_TEMPLATE = r"C:\Qualcomm\AIStack\QAIRT\{version}"
DEFAULT_QAIRT_DOWNLOAD_URL_TEMPLATE = (
    "https://softwarecenter.qualcomm.com/api/download/software/sdks/"
    "Qualcomm_AI_Runtime_Community/All/{version}/v{version}.zip"
)

# Canonical install locations (S8 audit P0-L2):
LOCALAPPDATA_RELATIVE = Path("QAIModelBuilder")
ARM64_VENV_RELATIVE = LOCALAPPDATA_RELATIVE / "envs" / ".venv_arm64_313"
X64_VENV_RELATIVE = LOCALAPPDATA_RELATIVE / "envs" / ".venv_x64_310"
QAIRT_CONFIG_RELATIVE = LOCALAPPDATA_RELATIVE / "config" / "qairt_env.json"

# Visual Studio 2022 toolchain locations (parity with
# scripts/setup/setup_qairt_env.py — the model-builder pipeline's QNN
# conversion/compilation steps need these to init the VS ARM64 build env).
# Ported here so ``qairt_env.json`` written by this installer carries the
# SAME complete schema the pipeline scripts expect (previously this writer
# emitted only a partial schema, omitting the vs_* keys and using different
# python key names — the model-build "missing keys" regression).
VS_COMMUNITY_BASE = r"C:\Program Files\Microsoft Visual Studio\2022\Community"
VS_VCVARSALL = rf"{VS_COMMUNITY_BASE}\VC\Auxiliary\Build\vcvarsall.bat"
VS_VC_TARGETS = rf"{VS_COMMUNITY_BASE}\MSBuild\Microsoft\VC\v170"


def _detect_vc_targets_path(vs_install_path: str) -> str:
    """Return ``<vs>/MSBuild/Microsoft/VC/<vNNN>/`` (newest versioned dir).

    Parity with setup_qairt_env.py: scans for v170/v180/… instead of
    hardcoding v170, falling back to v170 when none found.
    """
    vc_base = os.path.join(vs_install_path, "MSBuild", "Microsoft", "VC")
    try:
        versioned = sorted(
            d
            for d in os.listdir(vc_base)
            if d.lower().startswith("v") and d[1:].isdigit()
        )
    except OSError:
        versioned = []
    chosen = versioned[-1] if versioned else "v170"
    return os.path.join(vc_base, chosen) + "\\"


def _detect_vs_cmake_path(vs_install_path: str) -> str:
    """Return the VS-bundled CMake ``bin`` dir if present, else ""."""
    candidate = os.path.join(
        vs_install_path,
        "Common7",
        "IDE",
        "CommonExtensions",
        "Microsoft",
        "CMake",
        "CMake",
        "bin",
    )
    return candidate if os.path.isdir(candidate) else ""


# ----- Data classes ---------------------------------------------------------


@dataclass(frozen=True)
class InstallPaths:
    """Resolved filesystem locations used by the orchestrator."""

    repo_root: Path
    localappdata: Path
    arm64_venv: Path
    x64_venv: Path
    qairt_sdk_root: Path
    qairt_config_path: Path
    uv_exe: Path | None
    aria2c_exe: Path | None


# ----- Path discovery -------------------------------------------------------


def _localappdata() -> Path:
    """Return the user's ``%LOCALAPPDATA%`` directory.

    On non-Windows hosts (where ``LOCALAPPDATA`` is undefined), fall
    back to ``~/.local/share`` so the script remains testable on
    Linux / macOS CI runners. The QAIRT install path itself stays
    Windows-only; only the ``qairt_env.json`` writer can run anywhere.
    """

    env = os.environ.get("LOCALAPPDATA")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share"


def _detect_repo_root() -> Path:
    """Repo root = parent of ``scripts/`` (this file's grand-parent twice)."""

    here = Path(__file__).resolve()
    return here.parent.parent.parent  # scripts/init/install_qairt.py -> repo


def _resolve_paths(args: argparse.Namespace) -> InstallPaths:
    repo = Path(args.repo_root).resolve() if args.repo_root else _detect_repo_root()
    lad = _localappdata()
    arm64 = lad / "QAIModelBuilder" / "envs" / ".venv_arm64_313"
    x64 = lad / "QAIModelBuilder" / "envs" / ".venv_x64_310"

    sdk_root_str = args.sdk_root or os.environ.get("QAIRT_SDK_ROOT") or (
        DEFAULT_QAIRT_SDK_ROOT_TEMPLATE.format(version=args.qairt_version)
    )
    sdk_root = Path(sdk_root_str)

    config_path_str = args.config_path or str(
        lad / "QAIModelBuilder" / "config" / "qairt_env.json"
    )
    config_path = Path(config_path_str)

    uv_exe = repo / "data" / "bin" / "uv" / "uv.exe"
    if not uv_exe.is_file():
        uv_exe = None  # type: ignore[assignment]
    aria2c_exe = repo / "bin" / "aria2c" / "aria2c.exe"
    if not aria2c_exe.is_file():
        aria2c_exe = None  # type: ignore[assignment]

    return InstallPaths(
        repo_root=repo,
        localappdata=lad,
        arm64_venv=arm64,
        x64_venv=x64,
        qairt_sdk_root=sdk_root,
        qairt_config_path=config_path,
        uv_exe=uv_exe,
        aria2c_exe=aria2c_exe,
    )


# ----- Stage: x64 Python 3.10 venv -----------------------------------------


def install_x64_venv(paths: InstallPaths, *, dry_run: bool = False) -> bool:
    """Create the x86_64 Python 3.10 venv used for QAIRT model conversion.

    Idempotent: if the venv is already COMPLETE (python.exe + activate.bat
    + working pip), the function returns immediately with True. A half-built
    venv (e.g. an interrupted ``uv venv --seed``) is NOT trusted -- it is
    recreated so the later converter-deps install does not silently fail.
    """

    target_python = paths.x64_venv / "Scripts" / "python.exe"
    if _venv_is_complete(paths.x64_venv):
        _info(f"x64 venv already present and complete: {paths.x64_venv}")
        return True
    if target_python.is_file():
        _warn(
            f"x64 venv exists but is incomplete (missing activate.bat / pip); "
            f"recreating: {paths.x64_venv}"
        )

    if paths.uv_exe is None:
        _err("uv.exe not found; cannot create x64 venv. "
             "Run Setup.bat first to populate data/bin/uv/.")
        return False

    if dry_run:
        _info(f"[dry-run] would create x64 venv at {paths.x64_venv}")
        return True

    paths.x64_venv.parent.mkdir(parents=True, exist_ok=True)

    _info("Installing cpython-3.10-windows-x86_64 via uv...")
    rc = _run([str(paths.uv_exe), "python", "install", "cpython-3.10-windows-x86_64"])
    if rc != 0:
        _err(f"uv python install failed (rc={rc})")
        return False

    _info(f"Creating x64 venv at {paths.x64_venv}...")
    rc = _run(
        [
            str(paths.uv_exe),
            "venv",
            str(paths.x64_venv),
            "--python",
            "cpython-3.10-windows-x86_64",
            "--seed",
            "--allow-existing",
        ]
    )
    if rc != 0:
        _err(f"uv venv failed (rc={rc})")
        return False

    # Verify the venv is COMPLETE, not merely that python.exe landed
    # (State-Truth-First: a created-but-incomplete venv would later fail
    # silently when converter deps are installed into it).
    if not _venv_is_complete(paths.x64_venv):
        _err(
            f"x64 venv created but is incomplete (python.exe / activate.bat / "
            f"pip not all present) at {paths.x64_venv}"
        )
        return False

    _ok(f"x64 venv ready: {paths.x64_venv}")
    return True


# ----- Stage: QAIRT SDK -----------------------------------------------------


def install_qairt_sdk(
    paths: InstallPaths,
    *,
    qairt_version: str,
    download_url: str,
    dry_run: bool = False,
) -> bool:
    """Ensure the QAIRT SDK is available at ``paths.qairt_sdk_root``.

    Order of attempts:
      1. SDK already extracted: tool binary present → skip
      2. Vendored zip in ``vendor/qairt/v<ver>.zip`` → extract
      3. Download via aria2c if available
      4. Download via PowerShell ``Invoke-WebRequest``
    """

    sentinel = (
        paths.qairt_sdk_root
        / "bin"
        / "aarch64-windows-msvc"
        / "qnn-context-binary-generator.exe"
    )
    if sentinel.is_file():
        _info(f"QAIRT SDK already installed: {paths.qairt_sdk_root}")
        return True

    if dry_run:
        _info(f"[dry-run] would install QAIRT SDK to {paths.qairt_sdk_root}")
        return True

    if sys.platform != "win32":
        _info(
            "QAIRT SDK install is Windows-only; skipping on "
            f"{sys.platform} (no-op)."
        )
        return True

    vendor_zip = (
        paths.repo_root / "vendor" / "qairt" / f"v{qairt_version}.zip"
    )
    download_dir = paths.repo_root / "data" / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    download_zip = download_dir / "_qairt_tmp.zip"

    src_zip: Path
    if vendor_zip.is_file():
        _info(f"Found vendored QAIRT zip: {vendor_zip}")
        src_zip = vendor_zip
    else:
        if paths.aria2c_exe and paths.aria2c_exe.is_file():
            _info(
                f"Downloading QAIRT SDK {qairt_version} via aria2c "
                "(16 threads, resume on)..."
            )
            rc = _run(
                [
                    str(paths.aria2c_exe),
                    "-x16",
                    "-s16",
                    "-k10M",
                    "-c",
                    "--file-allocation=none",
                    "-d",
                    str(download_dir),
                    "-o",
                    "_qairt_tmp.zip",
                    download_url,
                ]
            )
        else:
            _info(
                "aria2c.exe not found; falling back to PowerShell "
                "Invoke-WebRequest (single-threaded)..."
            )
            rc = _run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    (
                        "$ProgressPreference='SilentlyContinue'; "
                        "[Net.ServicePointManager]::SecurityProtocol="
                        "[Net.SecurityProtocolType]::Tls12; "
                        f"Invoke-WebRequest -Uri '{download_url}' "
                        f"-OutFile '{download_zip}' -UseBasicParsing"
                    ),
                ]
            )
        if rc != 0 or not download_zip.is_file():
            _err(
                f"QAIRT SDK download failed (rc={rc}). "
                f"You can manually download from {download_url} and "
                f"place at {vendor_zip} then re-run."
            )
            return False
        src_zip = download_zip

    paths.qairt_sdk_root.parent.mkdir(parents=True, exist_ok=True)
    _info(f"Extracting QAIRT SDK to {paths.qairt_sdk_root}...")
    extract_tmp = paths.qairt_sdk_root.parent / "_extract_tmp"
    if extract_tmp.exists():
        shutil.rmtree(extract_tmp, ignore_errors=True)

    rc = _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                f"Expand-Archive -Path '{src_zip}' "
                f"-DestinationPath '{extract_tmp}' -Force"
            ),
        ]
    )
    if rc != 0:
        _err(f"Failed to extract QAIRT SDK (rc={rc}).")
        return False

    # Find the versioned subfolder inside the extract tree.
    versioned_dir = _find_versioned_subdir(extract_tmp, qairt_version)
    if versioned_dir is None:
        _err(
            f"Extracted QAIRT zip does not contain a '{qairt_version}' "
            f"subfolder under {extract_tmp}."
        )
        return False

    if paths.qairt_sdk_root.exists():
        shutil.rmtree(paths.qairt_sdk_root, ignore_errors=True)
    shutil.move(str(versioned_dir), str(paths.qairt_sdk_root))
    shutil.rmtree(extract_tmp, ignore_errors=True)

    if not vendor_zip.is_file() and download_zip.is_file():
        download_zip.unlink(missing_ok=True)

    if not sentinel.is_file():
        _warn(
            f"SDK extracted but expected tool not found at {sentinel}; "
            f"the zip layout may have changed."
        )
        return False

    _ok(f"QAIRT SDK installed: {paths.qairt_sdk_root}")
    return True


def _find_versioned_subdir(root: Path, version: str) -> Path | None:
    if not root.is_dir():
        return None
    direct = root / version
    if direct.is_dir():
        return direct
    for sub in root.iterdir():
        if sub.is_dir() and (sub / version).is_dir():
            return sub / version
    return None


# ----- Stage: qairt_env.json ------------------------------------------------


def write_qairt_env_json(
    paths: InstallPaths,
    *,
    qairt_version: str,
    dry_run: bool = False,
) -> bool:
    """Write the SKILL-discovered ``qairt_env.json``.

    Schema (Clean Cutover) — a SUPERSET that satisfies every consumer so
    no key is missing for any of them (parity with the legacy
    ``setup_qairt_env.py --gen-config`` output PLUS the daemon's keys):

      * **model-builder pipeline** (factory/.../scripts/*): reads
        ``qairt_sdk_root`` / ``python_x64_venv`` / ``python_arm64_venv`` /
        ``vs_vcvarsall`` / ``vc_targets_path`` / ``vs_cmake_path`` /
        ``qairt_download_url``.
      * **daemon runtime** (App Builder / GenieAPIService interpreter
        resolver): reads ``venv_python`` / ``qairt_root``.
      * **this installer's own ``verify()``**: reads ``schema_version`` /
        ``qairt_version``.

    ``qairt_root`` and ``qairt_sdk_root`` are kept in sync (same value);
    ``venv_python`` mirrors ``python_arm64_venv``'s interpreter so the
    daemon and the pipeline agree on the ARM64 runtime. The VS toolchain
    paths are auto-detected (same logic as setup_qairt_env.py); missing VS
    degrades to the documented default locations / "" (the pipeline only
    needs them for the compile step, which surfaces its own error if VS is
    absent).
    """

    vs_base = VS_COMMUNITY_BASE
    vcvarsall = VS_VCVARSALL
    vc_targets = VS_VC_TARGETS + "\\"
    vs_cmake_path = ""
    if os.path.isdir(vs_base):
        cand_vcvarsall = os.path.join(
            vs_base, "VC", "Auxiliary", "Build", "vcvarsall.bat"
        )
        if os.path.isfile(cand_vcvarsall):
            vcvarsall = cand_vcvarsall
            vc_targets = _detect_vc_targets_path(vs_base)
        vs_cmake_path = _detect_vs_cmake_path(vs_base)

    sdk_root_fwd = str(paths.qairt_sdk_root).replace("\\", "/")
    arm64_python = str(paths.arm64_venv / "Scripts" / "python.exe").replace(
        "\\", "/"
    )
    x64_python = str(paths.x64_venv / "Scripts" / "python.exe").replace(
        "\\", "/"
    )
    download_url = DEFAULT_QAIRT_DOWNLOAD_URL_TEMPLATE.format(
        version=qairt_version
    )

    config_payload = {
        "_comment": (
            "Auto-generated by install_qairt.py. Superset schema consumed by "
            "the model-builder pipeline, the daemon runtime, and this "
            "installer's verify(). Do not edit manually."
        ),
        # ----- installer verify() keys -----
        "schema_version": 2,
        "qairt_version": qairt_version,
        "_version": qairt_version,
        "platform": sys.platform,
        # ----- pipeline keys (parity with setup_qairt_env.py --gen-config) -----
        "qairt_sdk_root": sdk_root_fwd,
        "qairt_download_url": download_url,
        "python_x64_venv": str(paths.x64_venv).replace("\\", "/"),
        "python_arm64_venv": str(paths.arm64_venv).replace("\\", "/"),
        "vs_vcvarsall": vcvarsall,
        "vc_targets_path": vc_targets,
        "vs_cmake_path": vs_cmake_path,
        # ----- daemon runtime keys (interpreter_resolver / di.py) -----
        "qairt_root": sdk_root_fwd,
        "venv_python": arm64_python,
        # ----- back-compat interpreter pointers (kept from prior schema) -----
        "venv_arm64_python": arm64_python,
        "venv_x64_python": x64_python,
    }

    if dry_run:
        _info(
            f"[dry-run] would write {paths.qairt_config_path} with payload:\n"
            + json.dumps(config_payload, indent=2)
        )
        return True

    paths.qairt_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.qairt_config_path.write_text(
        json.dumps(config_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    _ok(f"Wrote {paths.qairt_config_path}")
    return True


# ----- Verification ---------------------------------------------------------


def verify(paths: InstallPaths, *, qairt_version: str) -> bool:
    """Verify that all install artefacts exist; returns True iff OK."""

    issues: list[str] = []

    arm64_python = paths.arm64_venv / "Scripts" / "python.exe"
    if not arm64_python.is_file():
        issues.append(f"ARM64 venv missing: {arm64_python}")
    elif not _venv_is_complete(paths.arm64_venv):
        issues.append(
            f"ARM64 venv incomplete (python.exe present but activate.bat / "
            f"pip missing): {paths.arm64_venv}"
        )

    x64_python = paths.x64_venv / "Scripts" / "python.exe"
    if not x64_python.is_file():
        issues.append(f"x64 venv missing: {x64_python}")
    elif not _venv_is_complete(paths.x64_venv):
        issues.append(
            f"x64 venv incomplete (python.exe present but activate.bat / "
            f"pip missing): {paths.x64_venv}"
        )

    sentinel = (
        paths.qairt_sdk_root
        / "bin"
        / "aarch64-windows-msvc"
        / "qnn-context-binary-generator.exe"
    )
    # The sentinel exists only on Windows installs; off-Windows we
    # only validate the JSON config.
    if sys.platform == "win32" and not sentinel.is_file():
        issues.append(f"QAIRT SDK tool missing: {sentinel}")

    if not paths.qairt_config_path.is_file():
        issues.append(f"qairt_env.json missing: {paths.qairt_config_path}")
    else:
        try:
            data = json.loads(paths.qairt_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"qairt_env.json unreadable: {exc}")
            data = None
        if data is not None:
            if data.get("schema_version") != 2:
                issues.append(
                    f"qairt_env.json schema_version != 2 (got "
                    f"{data.get('schema_version')!r})"
                )
            if data.get("qairt_version") != qairt_version:
                issues.append(
                    f"qairt_env.json qairt_version mismatch (expected "
                    f"{qairt_version!r}, got {data.get('qairt_version')!r})"
                )

    if issues:
        for issue in issues:
            _err(f"verify: {issue}")
        return False

    _ok("verify: all artefacts present and valid")
    return True


# ----- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_qairt",
        description=(
            "Install QAIRT SDK + create x64 venv + write qairt_env.json. "
            "Replaces Setup_Builder_Env.bat (P0-L2)."
        ),
    )
    parser.add_argument("--repo-root", type=str, default=None)
    parser.add_argument(
        "--qairt-version", default=DEFAULT_QAIRT_VERSION,
        help=f"QAIRT SDK version. Default: {DEFAULT_QAIRT_VERSION}.",
    )
    parser.add_argument(
        "--sdk-root", default=None,
        help=(
            "QAIRT SDK install root. Default reads $QAIRT_SDK_ROOT or "
            "C:\\Qualcomm\\AIStack\\QAIRT\\<version>."
        ),
    )
    parser.add_argument(
        "--config-path", default=None,
        help=(
            "Where to write qairt_env.json. Default: "
            "%%LOCALAPPDATA%%\\QAIModelBuilder\\config\\qairt_env.json."
        ),
    )
    parser.add_argument(
        "--download-url", default=None,
        help="Override QAIRT SDK download URL (default uses softwarecenter).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Only verify install state; do not install.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print actions without installing.",
    )
    parser.add_argument(
        "--skip-x64-venv", action="store_true",
        help="Skip x64 Python 3.10 venv creation.",
    )
    parser.add_argument(
        "--skip-sdk", action="store_true",
        help="Skip QAIRT SDK download/extract (use existing install).",
    )
    parser.add_argument(
        "--allow-missing-uv", action="store_true",
        help="Continue even if data/bin/uv/uv.exe is absent (write config only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = _resolve_paths(args)

    download_url = args.download_url or DEFAULT_QAIRT_DOWNLOAD_URL_TEMPLATE.format(
        version=args.qairt_version
    )

    _info(f"repo_root         = {paths.repo_root}")
    _info(f"localappdata      = {paths.localappdata}")
    _info(f"arm64_venv        = {paths.arm64_venv}")
    _info(f"x64_venv          = {paths.x64_venv}")
    _info(f"qairt_sdk_root    = {paths.qairt_sdk_root}")
    _info(f"qairt_config_path = {paths.qairt_config_path}")
    _info(f"uv_exe            = {paths.uv_exe}")
    _info(f"aria2c_exe        = {paths.aria2c_exe}")

    if args.check:
        return 0 if verify(paths, qairt_version=args.qairt_version) else 1

    if sys.platform != "win32":
        _info(
            f"Non-Windows host ({sys.platform}); writing qairt_env.json "
            "only (SDK install + venv creation are Windows-only no-ops)."
        )
        if not write_qairt_env_json(
            paths, qairt_version=args.qairt_version, dry_run=args.dry_run
        ):
            return 1
        return 0

    if paths.uv_exe is None and not args.allow_missing_uv:
        _err(
            "data/bin/uv/uv.exe not found. Run Setup.bat first, or pass "
            "--allow-missing-uv to skip uv-dependent steps."
        )
        return 3

    if not args.skip_x64_venv and not install_x64_venv(paths, dry_run=args.dry_run):
        return 1

    if not args.skip_sdk and not install_qairt_sdk(
        paths,
        qairt_version=args.qairt_version,
        download_url=download_url,
        dry_run=args.dry_run,
    ):
        return 1

    if not write_qairt_env_json(
        paths, qairt_version=args.qairt_version, dry_run=args.dry_run
    ):
        return 1

    if not args.dry_run and not verify(paths, qairt_version=args.qairt_version):
        return 1

    _ok("install_qairt: complete.")
    return 0


# ----- Helpers --------------------------------------------------------------


def _run(cmd: Sequence[str]) -> int:
    """Run ``cmd`` synchronously, streaming output to the parent stdio."""

    try:
        return subprocess.call(list(cmd))  # noqa: S603 — operator-supplied paths
    except OSError as exc:
        _err(f"failed to invoke {cmd[0]}: {exc}")
        return 1


def _venv_is_complete(venv_dir: Path) -> bool:
    """Return True only when ``venv_dir`` is a COMPLETE, usable venv.

    State-Truth-First (AGENTS.md 铁律1): a venv is only usable when it has
    BOTH ``Scripts\\python.exe`` AND ``Scripts\\activate.bat`` AND a working
    pip. Probing ``python.exe`` existence alone is a weak proxy -- an
    interrupted ``uv venv --seed`` leaves python.exe behind without the
    activation scripts / pip, and that half-built venv would be wrongly
    treated as "ready" (the same bug class fixed in Setup.bat Step 3).
    """

    python_exe = venv_dir / "Scripts" / "python.exe"
    activate = venv_dir / "Scripts" / "activate.bat"
    if not python_exe.is_file() or not activate.is_file():
        return False
    try:
        rc = subprocess.call(  # noqa: S603 — operator-supplied venv path
            [str(python_exe), "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return rc == 0


def _info(msg: str) -> None:
    _stderr(f"[install_qairt] {msg}\n")


def _ok(msg: str) -> None:
    _stderr(f"[install_qairt] OK: {msg}\n")


def _warn(msg: str) -> None:
    _stderr(f"[install_qairt] WARN: {msg}\n")


def _err(msg: str) -> None:
    _stderr(f"[install_qairt] ERROR: {msg}\n")


def _stderr(msg: str) -> None:
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except OSError:
        pass


__all__ = [
    "DEFAULT_QAIRT_VERSION",
    "InstallPaths",
    "install_qairt_sdk",
    "install_x64_venv",
    "main",
    "verify",
    "write_qairt_env_json",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())