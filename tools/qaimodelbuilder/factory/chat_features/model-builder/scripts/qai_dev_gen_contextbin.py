# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""
QNN Context Binary Generator

Generates hardware-specific context binaries for QNN models.
Required for Windows ARM64 deployment with HTP runtime.

Supports QAIRT SDK 2.45+ on Windows on Snapdragon (WoS) ARM64 devices.

QAIRT 2.45 WoS ARM64 note:
  - qnn-context-binary-generator.exe is normally in bin/aarch64-windows-msvc/  (pure ARM64, NOT arm64x)
  - Backend: QnnHtp.dll from lib/aarch64-windows-msvc/                  (pure ARM64, NOT arm64x)
  - arm64x-windows-msvc/ contains ARM64EC (compatibility layer) binaries -- DO NOT use for context binary generation
  - Use --config_file backend_extensions.json for HTP configuration (v73 or v81)

0-byte / corrupt generator handling (Windows):
  - bin/aarch64-windows-msvc/qnn-context-binary-generator.exe may be 0 bytes or
    corrupt, raising `[WinError 193]` / non-zero exit when launched.
  - NOTE: a 0-byte generator is most often NOT a shipping defect but a file that
    was overwritten/truncated by a stray command after install (e.g. a `>`
    redirection or copy landing on it). See SKILL.md B9. The whole
    ``C:\Qualcomm`` tree is now write-protected for the agent
    (``qai.platform.protected_paths``), so this should no longer happen.
  - SELF-HEAL (incident 2026-06-16): before launching, ``_ensure_generator_healthy``
    verifies the generator is a valid PE; if it was truncated/corrupted it is
    repaired by re-extracting JUST that one file from the KEPT SDK zip
    (``data/sdk/qairt/v<version>.zip`` kept by Setup, or the vendor-preplaced
    ``vendor/qairt/v<version>.zip``) — no ~2 GB re-download. Binaries are NOT
    mirrored to a side dir (the model does not edit binaries); the kept zip is
    the repair source for any corrupt/truncated SDK binary. If no usable zip /
    entry is found, the script prints a clear, actionable error and exits — NO
    system "This app can't run on your PC" dialog.
  - It still does NOT fall back to the x86_64 generator: an x86_64 process
    cannot LOAD an ARM64 model DLL in-process, so that path fails too (confirmed
    against SDK 2.46). On any generator failure the script exits non-zero;
    run_pipeline.py then degrades gracefully — it skips the `.bin` and uses the
    `.dll` directly for inference (numerically identical, higher cold-start
    latency), exactly as V1 did.

Usage:
  # Linux - input: libmodel.so
  python qai_dev_gen_contextbin.py --model libmodel.so --output libmodel.so.bin

  # Windows - input: model.dll
  python qai_dev_gen_contextbin.py --model model.dll --output model.dll.bin

  # Windows with backend config (QAIRT 2.45 WoS V73)
  python qai_dev_gen_contextbin.py --model model.dll --output model.dll.bin \\
    --config_file backend_extensions.json

  # With output directory
  python qai_dev_gen_contextbin.py --model model.dll --output_dir output \\
    --binary_file my_model --config_file backend_extensions.json

  # With auto-generated backend config (WoS ARM64 only)
  python qai_dev_gen_contextbin.py --model model.dll --output_dir output \\
    --binary_file my_model --auto-config

  # With profiling
  python qai_dev_gen_contextbin.py --model model.dll --output model.dll.bin --profiling

Note:
  - Windows ARM64: Context binary is REQUIRED for inference
  - Linux: Optional, use for specific SoC deployment without on-device compilation
  - Input must be absolute path
  - Output = input filename + '.bin' postfix (default)

Args:
  --model, --model_lib: Path to .dll or .so file
  --output: Output path for context binary (default: <model>.bin)
  --output_dir: Output directory (used with --binary_file, mirrors qnn-context-binary-generator)
  --binary_file: Output binary name without extension (used with --output_dir)
  --config_file: Path to backend_extensions.json (QAIRT 2.45 WoS V73 HTP config)
  --auto-config: Auto-generate backend_extensions.json and htp_backend_config_v73.json (WoS ARM64 only)
  --profiling: Enable HTP optrace profiling
"""

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Preflight gate 1: VS ARM64 environment
#
# qnn-context-binary-generator.exe is a native ARM64 binary.  Without the VS
# ARM64 runtime environment it fails with:
#   "Wrong number of Parameters 5" / "Conv2d failed 3110"
# which looks like an operator error but is actually a missing-env error.
#
# This function is a HARD GATE: it either ensures the env is active or
# calls sys.exit(1).  It never silently continues with a broken env.
# ---------------------------------------------------------------------------

def _ensure_vs_arm64_env(cfg: dict) -> None:
    """
    Guarantee that the current process has a VS ARM64 build environment
    before any ARM64 native binary is launched.

    Steps:
      1. Check VSCMD_ARG_TGT_ARCH: if already "arm64" -> done.
      2. Run vcvarsall.bat arm64, merge env vars into os.environ.
      3. Re-check VSCMD_ARG_TGT_ARCH: if still not "arm64" -> sys.exit(1).

    This is called unconditionally on Windows before the generator runs.
    Failure here is always fatal -- there is no point continuing.
    """
    if os.environ.get("VSCMD_ARG_TGT_ARCH", "").lower() == "arm64":
        print("[INFO] VS ARM64 env already active (VSCMD_ARG_TGT_ARCH=arm64)")
        return

    vcvarsall = cfg.get("vs_vcvarsall", "")
    vc_targets = cfg.get("vc_targets_path", "")

    if not vcvarsall or not Path(vcvarsall).exists():
        print("[ERROR] VS ARM64 env is NOT active and vcvarsall.bat was not found.")
        print("  vs_vcvarsall in qairt_env.json = " + repr(vcvarsall))
        print("  Fix: install Visual Studio 2022 Community (not BuildTools) and")
        print("  re-run Setup.bat to update data/config/qairt_env.json.")
        sys.exit(1)

    print("[INFO] Initializing VS ARM64 env from: " + vcvarsall)

    # FIX Bug1: use a list-based cmd so subprocess handles quoting correctly,
    # then capture the resulting environment via "set".
    # We run:  cmd.exe /c "vcvarsall.bat" arm64 >nul 2>&1 && set
    # Using shell=True with a list is not valid on Windows; instead build the
    # full command string carefully with the path quoted by shlex.quote
    # equivalent for Windows (double-quote the path).
    quoted = '"' + vcvarsall + '"'
    cmd = 'cmd /c "' + quoted + ' arm64 >nul 2>&1 && set"'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    except Exception as exc:
        print("[ERROR] Failed to run vcvarsall.bat: " + str(exc))
        sys.exit(1)

    if result.returncode != 0:
        print("[WARN] vcvarsall.bat returned exit code " + str(result.returncode) + " (may still be OK)")

    count = 0
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            os.environ[k] = v
            count += 1

    # Force VCTargetsPath to Community path so MSBuild uses the right toolset
    if vc_targets:
        os.environ["VCTargetsPath"] = vc_targets

    # --- Hard preflight gate: verify the env actually took effect ---
    arch = os.environ.get("VSCMD_ARG_TGT_ARCH", "")
    if arch.lower() != "arm64":
        print("[ERROR] VS ARM64 env init FAILED -- VSCMD_ARG_TGT_ARCH=" + repr(arch) + " (expected 'arm64')")
        print("  vcvarsall.bat merged " + str(count) + " vars but did not set target arch to arm64.")
        print("  Ensure vs_vcvarsall in qairt_env.json points to VS 2022 Community")
        print("  (not BuildTools) and that the ARM64 workload is installed.")
        sys.exit(1)

    print("[INFO] VS ARM64 env ready (" + str(count) + " vars merged, arch=arm64)")


# ---------------------------------------------------------------------------
# Preflight gate 2: HTP runtime files in CWD
#
# qnn-context-binary-generator.exe resolves files relative to its CWD.
# Required files differ by HTP version:
#
#   v73 (default):
#     QnnHtp.dll              -- HTP backend library (also passed via --backend)
#     libqnnhtpv73.cat        -- V73 DSP catalog  (DSP transport init)
#     libQnnHtpV73Skel.so     -- V73 DSP skeleton (DSP session)
#
#   v81:
#     QnnHtp.dll              -- HTP backend library (also passed via --backend)
#     QnnHtpV81Stub.dll       -- V81 stub (forwarding layer, loaded by QnnHtp.dll)
#     libqnnhtpv81.cat        -- V81 DSP catalog
#     libQnnHtpV81Skel.so     -- V81 DSP skeleton
#
#   ALL files must come from lib/aarch64-windows-msvc/ or hexagon-v*/unsigned/
#   NEVER use lib/arm64x-windows-msvc/ -- arm64x is ARM64EC and cannot be
#   loaded by the pure-ARM64 qnn-context-binary-generator.exe.
#
# Missing files cause:
#   DspTransport.openSession qnn_open failed, 0x80000406
# which cascades into "Wrong number of Parameters 5" / "Conv2d failed 3110".
#
# This function copies the files from the SDK, then verifies every file is
# present in dst_dir before returning.  If any required file is still missing
# after the copy attempt it calls sys.exit(1).
# ---------------------------------------------------------------------------

# FIX Bug2: build paths at call-time inside the function (not at module import
# time) so they always use the correct OS path separator for the running host.
# The relative sub-paths are plain strings; Path() joins them at runtime.
#
# V81 configuration: --htp_version=v81 uses libqnnhtpv81.cat, libQnnHtpV81Skel.so
# (The per-version required-file lists are built inside
# _copy_and_verify_htp_runtime_files.)


def _copy_and_verify_htp_runtime_files(
    sdk_root: str,
    dst_dir: str,
    htp_version: str = "v73",
    host_arch_dir: str = "aarch64-windows-msvc",
    is_dlc: bool = False,
) -> None:
    """
    Copy HTP runtime files from the QAIRT SDK into dst_dir, then verify
    that every required file is present.

    This is a HARD PREFLIGHT GATE called before subprocess.run(generator).
    If any required file is missing after the copy attempt, sys.exit(1).

    Args:
        sdk_root: Path to the QAIRT SDK root (e.g. C:/Qualcomm/AIStack/QAIRT/2.45.x)
        dst_dir:  Target directory -- must be the CWD passed to the generator.
        htp_version: HTP version to use ("v73" or "v81")
        host_arch_dir: SDK host-arch lib dir for the backend DLL(s). On WoS
            ARM64 this is always ``aarch64-windows-msvc`` (the generator and
            the model DLL are both ARM64).
        is_dlc: When True, also copy the extra DLLs required by the DLC->bin
            flow (QnnModelDlc.dll, QnnHtpV{ver}Stub.dll, QnnHtpPrepare.dll,
            QnnHtpNetRunExtensions.dll). See references/context_binary.md
            § SNPE/DLC Context Binary Generation.
    """
    sdk_path = Path(sdk_root)
    dst_path = Path(dst_dir)
    dst_path.mkdir(parents=True, exist_ok=True)

    # Select HTP runtime files based on version. The host backend DLL(s)
    # (QnnHtp.dll / *Stub.dll) come from ``host_arch_dir`` so they match the
    # generator's architecture; the hexagon ``.cat`` / ``.so`` skel files are
    # device-side (Hexagon) and arch-neutral w.r.t. the host generator.
    if htp_version == "v81":
        htp_files = [
            (f"lib/{host_arch_dir}/QnnHtp.dll", True),
            (f"lib/{host_arch_dir}/QnnHtpV81Stub.dll", True),
            ("lib/hexagon-v81/unsigned/libqnnhtpv81.cat", True),
            ("lib/hexagon-v81/unsigned/libQnnHtpV81Skel.so", True),
        ]
        print(f"[INFO] Using HTP runtime: {htp_version}")
    else:
        htp_files = [
            (f"lib/{host_arch_dir}/QnnHtp.dll", True),
            ("lib/hexagon-v73/unsigned/libqnnhtpv73.cat", True),
            ("lib/hexagon-v73/unsigned/libQnnHtpV73Skel.so", True),
        ]
        print(f"[INFO] Using HTP runtime: v73")

    # DLC->bin flow needs extra DLLs (the .dll->bin flow does not). Without
    # QnnHtpV{ver}Stub.dll / QnnHtpPrepare.dll the generator fails with
    # "Wrong number of Parameters 5" / "PrepareLibLoader Failed".
    if is_dlc:
        stub = "QnnHtpV81Stub.dll" if htp_version == "v81" else "QnnHtpV73Stub.dll"
        htp_files.extend([
            (f"lib/{host_arch_dir}/QnnModelDlc.dll", True),
            (f"lib/{host_arch_dir}/{stub}", True),
            (f"lib/{host_arch_dir}/QnnHtpPrepare.dll", True),
            (f"lib/{host_arch_dir}/QnnHtpNetRunExtensions.dll", True),
        ])
        print("[INFO] DLC->bin flow: including extra DLC runtime DLLs.")

    print("[INFO] Preflight: copying HTP runtime files to generator CWD: " + dst_dir)

    # FIX Bug3: wrap shutil.copy2 in try/except so copy failures are caught
    # and recorded rather than crashing with an unhandled OSError.
    # Collect names of files that are missing or failed to copy.
    missing = []

    for rel, required in htp_files:
        # Build path at runtime so Path() uses the correct OS separator
        src = sdk_path / Path(rel)
        name = src.name
        dst = dst_path / name

        if dst.exists():
            # Compare size with the SDK source to detect stale files from
            # a previous SDK version (e.g. old skel copied by an older whl).
            # If sizes differ, overwrite with the current SDK copy.
            if src.exists() and dst.stat().st_size == src.stat().st_size:
                print("[INFO]   already present (up-to-date): " + name)
                continue
            elif src.exists():
                print("[INFO]   stale (" + str(dst.stat().st_size) + " B) vs SDK ("
                      + str(src.stat().st_size) + " B), refreshing: " + name)
                # fall through to copy below
            else:
                print("[INFO]   already present (SDK source absent, keeping): " + name)
                continue

        if not src.exists():
            if required:
                missing.append(name)
                print("[ERROR]   source not found: " + str(src))
            else:
                print("[WARN]    source not found (optional): " + str(src))
            continue

        # Source exists -- attempt copy
        try:
            shutil.copy2(str(src), str(dst))
            print("[INFO]   copied: " + name)
        except OSError as exc:
            if required:
                missing.append(name)
                print("[ERROR]   copy failed: " + name + " -- " + str(exc))
            else:
                print("[WARN]    copy failed (optional): " + name + " -- " + str(exc))

    # --- Hard preflight gate: verify every required file is now in dst_dir ---
    # Re-check dst_path directly as the single source of truth.
    # This covers: (a) files that were already present, (b) files just copied,
    # (c) any edge case where the copy appeared to succeed but the file is absent.
    still_missing = [
        name
        for _, (rel, required) in enumerate(htp_files)
        if required and not (dst_path / Path(rel).name).exists()
    ]

    if still_missing:
        print("[ERROR] Preflight FAILED -- the following HTP runtime files are")
        print("[ERROR] missing from the generator CWD (" + dst_dir + "):")
        for name in still_missing:
            print("[ERROR]   MISSING: " + name)
        print("[ERROR] Without these files qnn-context-binary-generator.exe will fail with:")
        print("[ERROR]   DspTransport.openSession qnn_open failed, 0x80000406")
        print("[ERROR] Check that QAIRT SDK at '" + sdk_root + "' is complete")
        print("[ERROR] and contains lib/hexagon-v81/unsigned/.")
        sys.exit(1)

    print("[INFO] Preflight OK -- all HTP runtime files present in CWD.")


# ---------------------------------------------------------------------------
# Backend config auto-generation
# ---------------------------------------------------------------------------

def _auto_gen_backend_config(
    sdk_root: str,
    output_dir: str,
    graph_name: str,
    htp_version: str = "v73",
    host_arch_dir: str = "aarch64-windows-msvc",
) -> str:
    """
    Auto-generate backend_extensions.json and htp_backend_config_{version}.json.
    Returns path to backend_extensions.json.

    ``host_arch_dir`` selects the SDK lib dir for the extensions DLL. On WoS
    ARM64 this is always ``aarch64-windows-msvc``.
    """
    # Select HTP config filename based on version
    config_name = f"htp_backend_config_{htp_version}.json"
    htp_config = {
        "graphs": [{"graph_names": [graph_name], "vtcm_mb": 8, "O": 3}],
        "devices": [{"cores": [{"rpc_control_latency": 100, "perf_profile": "burst"}]}]
    }
    htp_config_path = os.path.join(output_dir, config_name)
    with open(htp_config_path, "w") as f:
        json.dump(htp_config, f, indent=2)

    # Select backend DLL based on version.
    # IMPORTANT: shared_library_path in backend_extensions.json must be
    # QnnHtpNetRunExtensions.dll (the extensions loader), NOT QnnHtpV81Stub.dll.
    # The HTP version (v73/v81) is selected at runtime by the HTP provider based
    # on the hardware; the Stub DLL is loaded internally by QnnHtp.dll, not here.
    backend_dll = "QnnHtpNetRunExtensions.dll"
    backend_lib_dir = host_arch_dir

    ext_config = {
        "backend_extensions": {
            "shared_library_path": os.path.join(
                sdk_root, "lib", backend_lib_dir, backend_dll
            ),
            "config_file_path": htp_config_path
        }
    }
    ext_config_path = os.path.join(output_dir, "backend_extensions.json")
    with open(ext_config_path, "w") as f:
        json.dump(ext_config, f, indent=2)

    print("[INFO] Auto-generated: " + htp_config_path)
    print("[INFO] Auto-generated: " + ext_config_path)
    return ext_config_path


# ---------------------------------------------------------------------------
# QAIRT env config discovery
# ---------------------------------------------------------------------------

def _find_qairt_env_config() -> dict:
    """Auto-discover data/config/qairt_env.json by traversing up the directory tree."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "data" / "config" / "qairt_env.json"
        if not candidate.exists():
            candidate = current / "config" / "qairt_env.json"
        if candidate.exists():
            try:
                with open(candidate, encoding="utf-8") as f:
                    cfg = json.load(f)
                print("[INFO] Loaded QAIRT env config: " + str(candidate))
                return cfg
            except Exception as e:
                print("[WARN] Failed to parse " + str(candidate) + ": " + str(e))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _apply_env_config(cfg: dict) -> None:
    """Apply settings from qairt_env.json to the current process environment."""
    if not cfg:
        return
    sdk_root = cfg.get("qairt_sdk_root", "")
    if sdk_root and not os.environ.get("QAIRT_SDK_ROOT"):
        os.environ["QAIRT_SDK_ROOT"] = sdk_root
        print("[INFO] Set QAIRT_SDK_ROOT from env_config: " + sdk_root)
    vc_targets = cfg.get("vc_targets_path", "")
    if vc_targets and not os.environ.get("VCTargetsPath"):
        os.environ["VCTargetsPath"] = vc_targets
    if sdk_root:
        qairt_pylib = os.path.join(sdk_root, "lib", "python")
        pythonpath = os.environ.get("PYTHONPATH", "")
        if qairt_pylib not in pythonpath:
            os.environ["PYTHONPATH"] = (
                qairt_pylib + os.pathsep + pythonpath if pythonpath else qairt_pylib
            )


# ---------------------------------------------------------------------------
# Generator self-heal from the kept SDK zip (incident 2026-06-16)
#
# The QAIRT ``qnn-context-binary-generator.exe`` was once truncated to 0 bytes
# by a stray write into the SDK tree. When the pipeline next launched it,
# Windows raised ``[WinError 193] %1 is not a valid Win32 application`` (+ the
# GUI "This app can't run on your PC" dialog), breaking on-device builds.
#
# Repair strategy (user decision 2026-06-20): binaries (.exe/.dll/.so/.cat) are
# NOT mirrored to a side directory — the model does not edit binaries, so a
# corrupt generator is repaired by re-extracting JUST that one file from the
# KEPT SDK zip (``data/sdk/qairt/v<version>.zip`` or the vendor-preplaced
# ``vendor/qairt/v<version>.zip``). No ~2 GB re-download, no big bin/lib mirror.
# (The model's editable launcher SCRIPTS are backed up separately by Setup.bat
# to ``data/sdk/qairt-scripts/`` — a different concern from this binary repair.)
# ---------------------------------------------------------------------------

def _find_data_root() -> Path | None:
    """Locate the project ``data/`` dir by walking up from this script."""
    current = Path(__file__).resolve().parent
    for _ in range(12):
        candidate = current / "data"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _find_repo_root() -> Path | None:
    """Locate the repo root (the dir that contains both ``data`` and ``vendor``)."""
    data_root = _find_data_root()
    if data_root is not None:
        return data_root.parent
    return None


def _is_healthy_pe(path: Path) -> bool:
    """True iff ``path`` exists, is non-empty, and starts with a PE ``MZ`` header."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size <= 0:
            return False
        with open(path, "rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def _kept_sdk_zip() -> Path | None:
    """Return the kept QAIRT SDK zip, or None.

    Looks for ``data/sdk/qairt/v<version>.zip`` (Setup-kept download) first,
    then ``vendor/qairt/v<version>.zip`` (vendor-preplaced). The version comes
    from ``QAIRT_SDK_ROOT`` basename when available, else any ``v*.zip`` in
    those dirs (newest by mtime).
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        return None
    candidates_dirs = [repo_root / "data" / "sdk" / "qairt", repo_root / "vendor" / "qairt"]
    sdk_root = os.environ.get("QAIRT_SDK_ROOT", "")
    version = os.path.basename(sdk_root.rstrip("\\/")) if sdk_root else ""
    # 1) exact versioned name if we know the version.
    if version:
        for d in candidates_dirs:
            cand = d / ("v" + version + ".zip")
            if cand.is_file():
                return cand
    # 2) fall back to the newest v*.zip in either dir.
    found: list[Path] = []
    for d in candidates_dirs:
        if d.is_dir():
            found.extend(d.glob("v*.zip"))
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _restore_generator_from_script_backup(generator_path: str) -> bool:
    """Restore generator from the Setup.bat file-level backup.

    The backup lives at ``data/sdk/qairt-scripts/aarch64-windows-msvc/
    qnn-context-binary-generator.exe``. This is a lightweight fallback (~4 MB
    file copy) when the kept SDK zip is unavailable or extraction fails.
    Returns True on a verified healthy restore; False otherwise.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        return False
    backup = (
        repo_root / "data" / "sdk" / "qairt-scripts"
        / "aarch64-windows-msvc" / os.path.basename(generator_path)
    )
    if not backup.is_file():
        return False
    if not _is_healthy_pe(backup):
        print("[RECOVER] script-backup exists but is not a healthy PE: " + str(backup))
        return False
    gen = Path(generator_path)
    gen.parent.mkdir(parents=True, exist_ok=True)
    os.environ["QAI_PROTECTED_PATHS_BYPASS"] = "1"
    try:
        shutil.copy2(str(backup), str(gen))
    except OSError as exc:
        os.environ.pop("QAI_PROTECTED_PATHS_BYPASS", None)
        print("[RECOVER] failed to restore generator from script backup: " + str(exc))
        return False
    finally:
        os.environ.pop("QAI_PROTECTED_PATHS_BYPASS", None)
    if _is_healthy_pe(gen):
        print("[RECOVER] restored generator from script backup: " + str(backup))
        return True
    return False


def _restore_generator_from_zip(generator_path: str) -> bool:
    """Re-extract the single generator exe from the kept SDK zip.

    Matches the archive entry whose path ends with
    ``bin/aarch64-windows-msvc/qnn-context-binary-generator.exe`` (the zip nests
    everything under a ``QAIRT/<version>/`` prefix). Returns True on a verified
    healthy restore. Best-effort; returns False if no zip / no matching entry /
    extraction failed.
    """
    import zipfile

    zip_path = _kept_sdk_zip()
    if zip_path is None:
        return False
    leaf = os.path.basename(generator_path)
    wanted_suffix = ("bin/aarch64-windows-msvc/" + leaf).lower()
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            member = None
            for name in zf.namelist():
                if name.replace("\\", "/").lower().endswith(wanted_suffix):
                    member = name
                    break
            if member is None:
                print("[RECOVER] generator not found inside kept zip: " + str(zip_path))
                return False
            gen = Path(generator_path)
            gen.parent.mkdir(parents=True, exist_ok=True)
            # The protected-paths guard blocks writes into C:\Qualcomm; this
            # restore is an app-controlled exception, so set the explicit narrow
            # bypass only around the single write, then clear it.
            os.environ["QAI_PROTECTED_PATHS_BYPASS"] = "1"
            try:
                with zf.open(member) as src, open(generator_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            finally:
                os.environ.pop("QAI_PROTECTED_PATHS_BYPASS", None)
        print(
            "[RECOVER] restored generator from kept SDK zip: " + str(zip_path)
        )
        return _is_healthy_pe(Path(generator_path))
    except (OSError, zipfile.BadZipFile) as exc:
        os.environ.pop("QAI_PROTECTED_PATHS_BYPASS", None)
        print("[RECOVER] failed to restore generator from zip: " + str(exc))
        return False


def _ensure_generator_healthy(generator_path: str) -> None:
    """Verify the generator is a valid executable; self-heal from the kept zip.

    1. If the generator is a valid PE, do nothing.
    2. If it is missing / 0-byte / not a valid PE, re-extract just that one file
       from the kept SDK zip (data/sdk/qairt or vendor/qairt).
    3. If no usable zip / entry, print a clear, actionable error (NO system GUI
       dialog) and ``sys.exit(2)``.
    """
    gen = Path(generator_path)
    if _is_healthy_pe(gen):
        return

    cur_size = gen.stat().st_size if gen.exists() else -1
    print(
        "[RECOVER] generator is corrupt/missing (size=" + str(cur_size) + "): "
        + generator_path
    )
    if _restore_generator_from_zip(generator_path):
        print("[RECOVER] generator restored from kept zip and verified healthy.")
        return

    # Fallback: try the file-level backup made by Setup.bat.
    if _restore_generator_from_script_backup(generator_path):
        print("[RECOVER] generator restored from script backup and verified healthy.")
        return

    # No usable repair source.
    print(
        "[ERROR] generator is a 0-byte / corrupt file and could not be restored "
        "from a kept SDK zip (data/sdk/qairt/v<version>.zip or "
        "vendor/qairt/v<version>.zip)."
    )
    print(
        "  The QAIRT SDK generator was overwritten/truncated (see SKILL.md B8/B9) "
        "or the install is incomplete."
    )
    print(
        "  Fix: re-extract bin/aarch64-windows-msvc/qnn-context-binary-generator.exe "
        "from the kept SDK zip, or re-run Setup.bat to reinstall the QAIRT SDK."
    )
    sys.exit(2)


def _select_linux_arch_dir(sdk_root: str) -> str:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        candidates = [
            "aarch64-oe-linux-gcc11.2",
            "aarch64-ubuntu-gcc9.4",
            "aarch64-oe-linux-gcc9.3",
            "aarch64-oe-linux-gcc8.2",
        ]
    else:
        candidates = ["x86_64-linux-clang"]

    for arch_dir in candidates:
        gen = os.path.join(sdk_root, "bin", arch_dir, "qnn-context-binary-generator")
        be = os.path.join(sdk_root, "lib", arch_dir, "libQnnHtp.so")
        if os.path.exists(gen) and os.path.exists(be):
            return arch_dir

    return "x86_64-linux-clang"


# ---------------------------------------------------------------------------
# Main generator function
# ---------------------------------------------------------------------------

def run_generator(model_path, output_path=None, output_dir=None, binary_file=None,
                  profiling=False, config_file=None, auto_config=False,
                  htp_version="v73"):
    """
    Run qnn-context-binary-generator to produce a context binary.

    Args:
        model_path:   Path to .dll or .so model library
        output_path:  Full output path for context binary (e.g. model.dll.bin)
        output_dir:   Output directory (used with binary_file)
        binary_file:  Binary name without extension (used with output_dir)
        profiling:    Enable HTP optrace profiling
        config_file:  Path to backend_extensions.json (QAIRT 2.45 WoS V73)
        auto_config:  Auto-generate backend config files (WoS ARM64 only)
        htp_version: HTP version to use ("v73" or "v81")
    """
    model_path = os.path.abspath(model_path)
    if output_path:
        output_path = os.path.abspath(output_path)
    if output_dir:
        output_dir = os.path.abspath(output_dir)
    if config_file:
        config_file = os.path.abspath(config_file)

    if not os.path.exists(model_path):
        print("Error: model file not found: " + model_path)
        sys.exit(2)

    # Auto-discover and apply QAIRT env config
    cfg = _find_qairt_env_config()
    _apply_env_config(cfg)

    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT environment variable is not set.")
        print("  Option 1: set QAIRT_SDK_ROOT=<path to QAIRT SDK>")
        print("  Option 2: Run Setup.bat (reads from data\\config\\qairt_env.json)")
        sys.exit(1)

    system = platform.system().lower()

    # -----------------------------------------------------------------------
    # PREFLIGHT GATE 1: VS ARM64 environment (Windows only)
    # Must run BEFORE any ARM64 native binary is launched.
    # Hard exit if environment cannot be established.
    # -----------------------------------------------------------------------
    if system == "windows":
        _ensure_vs_arm64_env(cfg)

    # FIX Bug4: replace silent else-fallback with explicit error for unknown platforms.
    if system == "linux":
        arch_dir = _select_linux_arch_dir(sdk_root)
        generator_exe = "qnn-context-binary-generator"
        backend_name = "libQnnHtp.so"
        backend_lib_dir = arch_dir  # FIX: was missing, caused NameError on L741
        # Auto-set QNN_AARCH64_UBUNTU_GCC_94 for aarch64 Linux toolchain
        _machine = platform.machine().lower()
        if _machine in ("aarch64", "arm64") and "QNN_AARCH64_UBUNTU_GCC_94" not in os.environ:
            print("[WARN] QNN_AARCH64_UBUNTU_GCC_94 not set; defaulting to '/'")
            os.environ["QNN_AARCH64_UBUNTU_GCC_94"] = "/"
    elif system == "windows":
        # Context binary generator is normally in aarch64-windows-msvc.
        arch_dir = "aarch64-windows-msvc"
        generator_exe = "qnn-context-binary-generator.exe"
        # Select backend DLL based on htp_version.
        # IMPORTANT: always use QnnHtp.dll as the --backend argument to qnn-context-binary-generator.
        # For v81, QnnHtpV81Stub.dll is referenced only via backend_extensions.json
        # (shared_library_path), NOT as the --backend argument directly.
        # Passing QnnHtpV81Stub.dll as --backend causes "Unable to load backend" because
        # the Stub DLL cannot be loaded standalone -- it requires QnnHtp.dll as the loader.
        backend_name = "QnnHtp.dll"
        backend_lib_dir = "aarch64-windows-msvc"
    else:
        print("[ERROR] Unsupported platform: " + system)
        print("  qai_dev_gen_contextbin.py supports Windows and Linux only.")
        sys.exit(1)

    generator_path = os.path.join(sdk_root, "bin", arch_dir, generator_exe)
    backend_path = os.path.join(sdk_root, "lib", backend_lib_dir, backend_name)

    # NOTE on SDK integrity: we deliberately do NOT try to validate the
    # generator here (e.g. "is it 0 bytes?"). SDK corruption takes many forms
    # (truncated/zero-byte exe, broken deps, arch mismatch, version skew) and
    # pre-checking one specific failure mode is both fragile and incomplete.
    # Instead the generator is simply executed below; ANY failure (non-zero
    # exit / launch error / WinError) is surfaced verbatim to the caller
    # (run_pipeline.py step 3), which treats "context binary generation failed"
    # uniformly — it falls back to direct .dll inference (V1 parity: V1 likewise
    # skipped the .bin and loaded the .dll when the generator could not run).
    # We keep only a cheap "file exists" check to give a clear message for the
    # common "tool entirely absent" case; real validation = the run result.
    if not os.path.exists(generator_path):
        print("Error: generator not found at " + generator_path)
        sys.exit(2)
    if not os.path.exists(backend_path):
        print("Error: backend library not found at " + backend_path)
        sys.exit(2)

    # Self-heal: ensure the generator is a valid executable (back up the first
    # healthy copy to data/sdk; restore from backup if it was truncated to
    # 0 bytes / corrupted — the 2026-06-16 incident). This turns the abrupt
    # ``[WinError 193]`` + system GUI dialog into either a transparent recovery
    # or a clear, actionable error.
    if system == "windows":
        _ensure_generator_healthy(generator_path)

    # Resolve bin_name and gen_output_dir
    base = os.path.basename(model_path)
    name_without_ext = os.path.splitext(base)[0]

    if binary_file:
        bin_name = binary_file
    elif output_path:
        bin_name = os.path.basename(output_path)
        if bin_name.endswith(".bin"):
            bin_name = bin_name[:-4]
    else:
        bin_name = name_without_ext

    if output_dir:
        gen_output_dir = output_dir
        os.makedirs(gen_output_dir, exist_ok=True)
    elif output_path:
        gen_output_dir = os.path.dirname(output_path) or os.path.dirname(os.path.abspath(model_path))
        os.makedirs(gen_output_dir, exist_ok=True)
    else:
        gen_output_dir = os.path.dirname(os.path.abspath(model_path))
        os.makedirs(gen_output_dir, exist_ok=True)

    # Detect DLC input. A .dlc cannot be loaded directly as --model (it is not
    # a PE/DLL); the DLC->bin flow uses QnnModelDlc.dll as --model and passes
    # the .dlc via --dlc_path. See references/context_binary.md
    # § SNPE/DLC Context Binary Generation.
    is_dlc_input = model_path.lower().endswith(".dlc")

    # Map htp_version -> soc_model for DLC->bin (used instead of a config_file).
    # v73 = Snapdragon X Elite (SC8380XP) = 60; v81 = Snapdragon X2 Elite
    # (SC8480XP) = 88. See references/context_binary.md § soc_model Reference.
    soc_model = "88" if htp_version == "v81" else "60"

    # Auto-generate backend config if requested.
    # NOTE: DLC->bin does NOT use a config_file. With a config_file the HTP
    # backend extension requires a valid 'graph_names', but DLC graph names are
    # not predictable from the file stem ("Valid 'graph_names' must be
    # specified" failure). The documented minimal DLC->bin command instead uses
    # --soc_model (see references/context_binary.md). So skip auto-config for DLC.
    if auto_config and not config_file and system == "windows" and not is_dlc_input:
        graph_name = os.path.splitext(os.path.basename(model_path))[0]
        config_file = _auto_gen_backend_config(
            sdk_root, gen_output_dir, graph_name, htp_version,
            host_arch_dir=backend_lib_dir,
        )
        config_file = os.path.abspath(config_file)

    # -----------------------------------------------------------------------
    # PREFLIGHT GATE 2: HTP runtime files in generator CWD (Windows only)
    #
    # Copy the required runtime files from the SDK into gen_output_dir, then
    # verify every file is present.  Hard exit if any are still missing.
    # The generator is launched with cwd=gen_output_dir so it finds them.
    # DLC inputs need extra DLLs (is_dlc flag).
    # -----------------------------------------------------------------------
    if system == "windows":
        _copy_and_verify_htp_runtime_files(
            sdk_root, gen_output_dir, htp_version,
            host_arch_dir=backend_lib_dir, is_dlc=is_dlc_input,
        )

    # Build generator command. DLC inputs use QnnModelDlc.dll + --dlc_path +
    # --soc_model (no config_file); DLL inputs use --model directly.
    if is_dlc_input:
        dlc_loader_name = "libQnnModelDlc.so" if system == "linux" else "QnnModelDlc.dll"
        dlc_loader_path = os.path.join(sdk_root, "lib", backend_lib_dir, dlc_loader_name)
        if not os.path.exists(dlc_loader_path):
            print(f"[ERROR] {dlc_loader_name} not found at: " + dlc_loader_path)
            print("  Cannot generate context binary from .dlc without this loader library.")
            sys.exit(2)
        command = [
            generator_path,
            "--backend", backend_path,
            "--model", dlc_loader_path,
            "--dlc_path", model_path,
            "--output_dir", gen_output_dir,
            "--binary_file", bin_name,
            "--soc_model", soc_model,
        ]
    else:
        command = [
            generator_path,
            "--backend", backend_path,
            "--model", model_path,
            "--output_dir", gen_output_dir,
            "--binary_file", bin_name,
        ]

    if config_file:
        if not os.path.exists(config_file):
            print("Warning: config_file not found: " + config_file)
        else:
            command.extend(["--config_file", config_file])
            print("[INFO] Using backend config: " + config_file)

    if profiling:
        command.extend(["--profiling_level", "detailed", "--profiling_option", "optrace"])

    # FIX Bug5: use shlex.join so paths with spaces are quoted in the log output,
    # making it unambiguous which tokens are separate arguments.
    print("Executing: " + shlex.join(command))
    print("[INFO] Generator: " + arch_dir + "/" + generator_exe)
    print("[INFO] Backend:   " + arch_dir + "/" + backend_name)
    print("[INFO] CWD:       " + gen_output_dir)

    # FIX Bug6: use check=False (the generator returns non-zero even on success).
    # Log the exit code for diagnostics but do NOT treat non-zero as failure here.
    # Success is determined solely by whether the output .bin file exists and has
    # real content -- that check happens immediately after this call.
    result = subprocess.run(command, cwd=gen_output_dir)
    if result.returncode != 0:
        print("[WARN] Generator exited with code " + str(result.returncode) +
              " -- this is normal for qnn-context-binary-generator.exe.")
        print("       Verifying output file existence...")

    # -----------------------------------------------------------------------
    # Locate the generated .bin file.
    #
    # Priority 1: expected path <gen_output_dir>/<bin_name>.bin
    # Priority 2: fallback recursive search, with exclusion of Step-1
    #             intermediate files (files in the root dir whose stem does
    #             NOT match bin_name, e.g. inception_v3.bin when bin_name
    #             is inception_v3_fp16).
    #
    # Files in subdirectories are always accepted -- the generator sometimes
    # places the real binary in a bins/ subfolder.
    # -----------------------------------------------------------------------
    gen_output_dir_resolved = Path(gen_output_dir).resolve()

    expected_output = os.path.join(gen_output_dir, bin_name + ".bin")
    expected_size = os.path.getsize(expected_output) if os.path.exists(expected_output) else 0

    if os.path.exists(expected_output) and expected_size > 1024:
        final_path = expected_output
        if output_path and os.path.abspath(output_path) != os.path.abspath(expected_output):
            os.makedirs(
                os.path.dirname(output_path) if os.path.dirname(output_path) else ".",
                exist_ok=True,
            )
            shutil.move(expected_output, output_path)
            final_path = output_path
        print("Output: " + os.path.abspath(final_path) + " (" + str(os.path.getsize(final_path)) + " bytes)")
        return

    # Fallback: recursive search with intermediate-file exclusion
    files = []
    for root, dirs, filenames in os.walk(gen_output_dir):
        root_path = Path(root).resolve()
        in_subdir = root_path != gen_output_dir_resolved
        for f in filenames:
            if not f.endswith(".bin"):
                continue
            full_path = os.path.join(root, f)
            size = os.path.getsize(full_path)
            if size <= 1024:
                continue
            stem = Path(f).stem
            if not in_subdir and stem != bin_name:
                print(
                    "[INFO] Fallback search: skipping Step-1 intermediate file "
                    "(stem=" + repr(stem) + " != bin_name=" + repr(bin_name) + "): " + full_path
                )
                continue
            files.append((full_path, size))

    files.sort(key=lambda x: x[1], reverse=True)

    if files:
        actual_output, actual_size = files[0]
        if len(files) > 1:
            print("[WARN] Multiple non-empty .bin files found; using largest: " +
                  actual_output + " (" + str(actual_size) + " bytes)")
            for p, s in files[1:]:
                print("       Ignored: " + p + " (" + str(s) + " bytes)")
        if output_path and os.path.abspath(output_path) != os.path.abspath(actual_output):
            os.makedirs(
                os.path.dirname(output_path) if os.path.dirname(output_path) else ".",
                exist_ok=True,
            )
            shutil.move(actual_output, output_path)
            print("Output: " + os.path.abspath(output_path) + " (" + str(os.path.getsize(output_path)) + " bytes)")
        else:
            print("Output: " + os.path.abspath(actual_output) + " (" + str(actual_size) + " bytes)")
        return

    # No valid .bin found
    placeholder_files = []
    for root, dirs, filenames in os.walk(gen_output_dir):
        for f in filenames:
            if f.endswith(".bin"):
                placeholder_files.append(os.path.join(root, f))
    if placeholder_files:
        print("Error: Only empty/placeholder .bin files found in " + gen_output_dir + ":")
        for p in placeholder_files:
            print("  " + p + " (" + str(os.path.getsize(p)) + " bytes)")
        print("This may indicate the generator failed silently or was interrupted.")
    else:
        print("Error: Output .bin file not found in " + gen_output_dir)
    print("Check generator logs above for errors.")
    sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run QNN Context Binary Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage (Windows WoS ARM64)
  python qai_dev_gen_contextbin.py --model model.dll --output model.dll.bin

  # With backend config (QAIRT 2.45 WoS V73)
  python qai_dev_gen_contextbin.py --model model.dll --output model.dll.bin \\
    --config_file backend_extensions.json

  # With output directory (mirrors qnn-context-binary-generator CLI)
  python qai_dev_gen_contextbin.py --model model.dll \\
    --output_dir output --binary_file my_model \\
    --config_file backend_extensions.json

  # Linux
  python qai_dev_gen_contextbin.py --model libmodel.so --output libmodel.so.bin
        """
    )
    parser.add_argument("--model", "--model_lib", dest="model", required=True,
                        help="Path to the model .dll/.so file")
    parser.add_argument("--output", help="Output path for context binary (e.g. model.dll.bin)")
    parser.add_argument("--output_dir", help="Output directory for context binary (used with --binary_file)")
    parser.add_argument("--binary_file", help="Output binary name without .bin extension (used with --output_dir)")
    parser.add_argument("--config_file",
                        help="Path to backend_extensions.json for HTP configuration (QAIRT 2.45 WoS V73)")
    parser.add_argument("--auto-config", action="store_true",
                        help="Auto-generate backend_extensions.json and htp_backend_config_v73.json (WoS ARM64 only)")
    parser.add_argument("--profiling", action="store_true", help="Enable HTP optrace profiling")
    parser.add_argument("--htp_version", default="v73",
                        help="HTP version to use ('v73' or 'v81', default: v73)")

    args = parser.parse_args()

    run_generator(
        args.model,
        output_path=args.output,
        output_dir=args.output_dir,
        binary_file=args.binary_file,
        profiling=args.profiling,
        config_file=args.config_file,
        auto_config=args.auto_config,
        htp_version=args.htp_version,
    )
