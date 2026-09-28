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

# Host-arch guard: this native-ARM64 generator (VS ARM64 env + aarch64
# HTP runtime files in CWD) only runs on windows-arm64 / linux-aarch64.
# On any other host we delegate to qai_dev_gen_contextbin_x86.py, which
# calls qnn-context-binary-generator with the x86_64 CPU backend. This
# keeps a single canonical entry point for run_pipeline.py and direct
# callers alike.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _host_arch import has_local_htp  # noqa: E402
from _soc_targets import (  # noqa: E402
    DEFAULT_HTP_VERSION,
    HTP_VERSIONS,
    hexagon_runtime_files,
    host_stub_name,
    htp_version_to_target_soc_model,
    normalize_htp_version,
    soc_target,
)


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
# The per-version required-file lists are built inside
# _copy_and_verify_htp_runtime_files, table-driven from _soc_targets
# (hexagon_runtime_files / host_stub_name) -- there is no per-version literal
# here to keep in sync.


def _copy_and_verify_htp_runtime_files(
    sdk_root: str,
    dst_dir: str,
    htp_version: str = "v73",
    host_arch_dir: str = "aarch64-windows-msvc",
    is_dlc: bool = False,
    include_host_libs: bool = True,
) -> None:
    """
    Copy HTP runtime files from the QAIRT SDK into dst_dir, then verify
    that every required file is present.

    This is a HARD PREFLIGHT GATE called before subprocess.run(generator).
    If any required file is missing after the copy attempt, sys.exit(1).

    Args:
        sdk_root: Path to the QAIRT SDK root (e.g. C:/Qualcomm/AIStack/QAIRT/2.45.x)
        dst_dir:  Target directory -- must be the CWD passed to the generator.
        htp_version: HTP version to use -- any of _soc_targets.HTP_VERSIONS
            ("v68" ... "v81"; v66 excluded -- legacy QnnDsp backend). Which ones a host can actually build for is
            limited by the Stub libs that host's lib dir ships.
        host_arch_dir: SDK host-arch lib dir for the backend DLL(s). On WoS
            ARM64 this is always ``aarch64-windows-msvc`` (the generator and
            the model DLL are both ARM64).
        is_dlc: When True, also copy the extra DLLs required by the DLC->bin
            flow (QnnModelDlc.dll, QnnHtpV{ver}Stub.dll, QnnHtpPrepare.dll,
            QnnHtpNetRunExtensions.dll). See references/on_device_context_binary.md
            § SNPE/DLC Context Binary Generation.
        include_host_libs: When False, copy ONLY the device-side Hexagon files
            (.cat / Skel) and leave the host-side backend + Stub libraries
            alone. Used on Linux-with-local-HTP, where the generator loads
            ``libQnnHtp.so`` from ``--backend``'s absolute path and its Stub
            from the dynamic loader path -- requiring copies of those in the
            CWD there would hard-exit a build that works today.
    """
    sdk_path = Path(sdk_root)
    dst_path = Path(dst_dir)
    dst_path.mkdir(parents=True, exist_ok=True)

    # Select HTP runtime files based on version. The host backend DLL(s)
    # (QnnHtp.dll / *Stub.dll) come from ``host_arch_dir`` so they match the
    # generator's architecture; the hexagon ``.cat`` / ``.so`` skel files are
    # device-side (Hexagon) and arch-neutral w.r.t. the host generator.
    #
    # Version handling is TABLE-DRIVEN via _soc_targets, not a v73/v81 if-else.
    # The old two-branch form silently mapped every non-v81 value (v75, v79, ...)
    # onto the v73 file set: the run produced a .bin with rc=0 carrying v73 skel
    # files for a v75 target, failing only on the device.
    #
    # Stub policy: copy the selected version's stub plus the OTHER versions'
    # stubs that the SDK happens to ship. An unused stub is inert (QnnHtp loads
    # the one matching the hardware), while a missing one costs "Wrong number of
    # Parameters 5" / "PrepareLibLoader Failed". Ref: QAIRT docs
    # Delegates/TfLite/qnn_libs.html ("recommend to put all libraries to the
    # environment").
    #
    # DO NOT make the non-selected stubs required=True: host arches ship
    # different subsets (aarch64-windows-msvc has only v68/v73/v81) so a
    # required=True would hard-exit(1) on a build that currently works.
    #
    # x86_64 hosts have no Stub->Skel chain (libQnnHtp is an emulator there) and
    # lib/x86_64-linux-clang/ ships no libQnnHtpV*Stub.so -- nothing to copy.
    _version = normalize_htp_version(htp_version)
    if not _version:
        print(f"[ERROR] unknown htp_version: {htp_version!r}. "
              f"Supported: {', '.join(HTP_VERSIONS)}")
        sys.exit(2)

    _is_windows = host_arch_dir.endswith("windows-msvc")
    _backend_lib = "QnnHtp.dll" if _is_windows else "libQnnHtp.so"
    _selected_stub = host_stub_name(_version, _is_windows)

    htp_files = []
    if include_host_libs:
        htp_files += [
            (f"lib/{host_arch_dir}/{_backend_lib}", True),
            (f"lib/{host_arch_dir}/{_selected_stub}", True),
        ]
    # Device-side Hexagon files. The .cat is required only for the versions
    # that actually ship one (v73/v81 in QAIRT 2.48.40); for v68/v69/v75/v79
    # its absence is normal, NOT a damaged SDK -- see _soc_targets.
    htp_files += [
        (rel, required)
        for rel, required in hexagon_runtime_files(_version)
    ]
    print(f"[INFO] Using HTP runtime: {_version}")

    # The non-selected stubs: best-effort, never fatal.
    for _other in HTP_VERSIONS if include_host_libs else ():
        if _other == _version:
            continue
        _other_stub = host_stub_name(_other, _is_windows)
        if _other_stub:
            htp_files.append((f"lib/{host_arch_dir}/{_other_stub}", False))

    # DLC->bin flow needs extra DLLs (the .dll->bin flow does not). Without
    # QnnHtpV{ver}Stub.dll / QnnHtpPrepare.dll the generator fails with
    # "Wrong number of Parameters 5" / "PrepareLibLoader Failed".
    # Both stubs are already in htp_files above (selected one required, the
    # other optional), so this only adds the DLC-specific DLLs.
    if is_dlc:
        htp_files.extend([
            (f"lib/{host_arch_dir}/QnnModelDlc.dll", True),
            (f"lib/{host_arch_dir}/QnnHtpPrepare.dll", True),
            (f"lib/{host_arch_dir}/QnnHtpNetRunExtensions.dll", True),
        ])
        print("[INFO] DLC->bin flow: including extra DLC runtime DLLs.")

    print("[INFO] Preflight: copying HTP runtime files to generator CWD: " + dst_dir)

    # Copy failures are reported inline below; the authoritative check is the
    # preflight gate further down, which re-reads dst_dir instead of trusting
    # any bookkeeping done here. (A `missing` accumulator used to be built here
    # and then never read -- misleading for the next reader.)

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
                print("[ERROR]   copy failed: " + name + " -- " + str(exc))
            else:
                print("[WARN]    copy failed (optional): " + name + " -- " + str(exc))

    # --- Hard preflight gate: verify every required file is now in dst_dir ---
    # Re-check dst_path directly as the single source of truth.
    # This covers: (a) files that were already present, (b) files just copied,
    # (c) any edge case where the copy appeared to succeed but the file is absent.
    # NOTE: `name` MUST be bound by this comprehension itself.  It used to read
    # a leaked `name` from the copy loop above (Python leaks the loop variable of
    # a `for` statement, though not of a comprehension), so every MISSING line
    # printed the LAST file the loop touched instead of the file truly absent --
    # sending the reader after a file that is present.
    still_missing = [
        Path(rel).name
        for rel, required in htp_files
        if required and not (dst_path / Path(rel).name).exists()
    ]

    if still_missing:
        print("[ERROR] Preflight FAILED -- the following HTP runtime files are")
        print("[ERROR] missing from the generator CWD (" + dst_dir + "):")
        for name in still_missing:
            print("[ERROR]   MISSING: " + name)
        print("[ERROR] Without these files qnn-context-binary-generator will fail with:")
        print("[ERROR]   DspTransport.openSession qnn_open failed, 0x80000406")
        print("[ERROR] Check that QAIRT SDK at '" + sdk_root + "' is complete")
        print("[ERROR] and contains lib/hexagon-" + _version + "/unsigned/.")
        sys.exit(1)

    print("[INFO] Preflight OK -- all HTP runtime files present in CWD.")


def _dlc_utils_supported_pytags(sdk_root: str) -> set:
    """Python tags (e.g. {"310", "312"}) the SDK ships dlc_utils binaries for.

    ``qairt-dlc-info`` is a thin Python wrapper around a NATIVE extension
    (``libDlModelToolsPy<TAG>.pyd`` / ``.so``) that only exists for the Python
    versions the SDK was built against. Under any other interpreter it fails
    with "Failed to find necessary package ... cannot import name
    libDlModelToolsPy<TAG>" and exit code 1, so the interpreter must be chosen
    to match rather than assumed.
    """
    import glob as _glob
    base = os.path.join(sdk_root, "lib", "python", "qti", "aisw", "dlc_utils")
    tags = set()
    for pat in ("libDlModelToolsPy*.pyd", "libDlModelToolsPy*.so"):
        for hit in _glob.glob(os.path.join(base, "*", pat)):
            stem = os.path.basename(hit).split(".")[0]
            tag = stem.replace("libDlModelToolsPy", "")
            if tag.isdigit():
                tags.add(tag)
    return tags


def _interpreter_for_dlc_utils(sdk_root: str):
    """Pick a Python interpreter whose version matches the SDK dlc_utils build.

    Prefers the running interpreter when it already matches, so the common case
    costs no extra subprocess. Otherwise probes the QAIModelBuilder venvs.
    ``QAIRT_DLC_INFO_PYTHON`` overrides the whole search.
    """
    tags = _dlc_utils_supported_pytags(sdk_root)
    if not tags:
        return sys.executable  # unknown layout: try anyway and let it report

    override = os.environ.get("QAIRT_DLC_INFO_PYTHON")
    if override and os.path.exists(override):
        return override

    if "%d%d" % sys.version_info[:2] in tags:
        return sys.executable

    envs_root = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "QAIModelBuilder", "envs"
    )
    candidates = []
    if os.path.isdir(envs_root):
        for entry in sorted(os.listdir(envs_root)):
            for rel in (os.path.join("Scripts", "python.exe"),
                        os.path.join("bin", "python3"),
                        os.path.join("bin", "python")):
                cand = os.path.join(envs_root, entry, rel)
                if os.path.exists(cand):
                    candidates.append(cand)

    for cand in candidates:
        try:
            probe = subprocess.run(
                [cand, "-c", "import sys;print('%d%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() in tags:
            return cand
    return None


def _dlc_graph_names_via_tool(sdk_root: str, dlc_path: str,
                              host_arch_dir: str) -> list:
    """Read graph names out of a .dlc using qairt-dlc-info.

    Returns [] when the tool is missing or the names cannot be parsed; callers
    must treat that as "cannot emit a config" rather than guessing a name from
    the file stem (a wrong graph_names makes the HTP backend reject the config).

    Three SDK layout facts drive the implementation:
      * ``qairt-dlc-info`` is an extension-less PYTHON SCRIPT, not a native
        executable. On Windows it cannot be spawned directly (WinError 193);
        it must be run as ``<python> <script>``.
      * it does NOT ship in ``bin/aarch64-windows-msvc``. On WoS ARM64 the
        only copy lives in ``bin/x86_64-windows-msvc`` -- being a pure Python
        script, that copy runs fine under an ARM64 interpreter.
      * it needs an interpreter matching the SDK's dlc_utils native build
        (see _interpreter_for_dlc_utils).
    """
    candidates = []
    for arch in (host_arch_dir, "x86_64-windows-msvc", "x86_64-linux-clang"):
        bin_dir = os.path.join(sdk_root, "bin", arch)
        for cand in ("qairt-dlc-info.exe", "qairt-dlc-info"):
            p = os.path.join(bin_dir, cand)
            if p not in candidates:
                candidates.append(p)
    tool = next((c for c in candidates if os.path.exists(c)), None)
    if tool is None:
        return []

    # A .exe is spawned directly; the extension-less script needs an explicit
    # interpreter whose version matches the SDK's dlc_utils native module.
    if tool.lower().endswith(".exe"):
        argv = [tool]
    else:
        interp = _interpreter_for_dlc_utils(sdk_root)
        if interp is None:
            return []
        argv = [interp, tool]

    env = dict(os.environ)
    py_lib = os.path.join(sdk_root, "lib", "python")
    if os.path.isdir(py_lib):
        env["PYTHONPATH"] = py_lib + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(argv + ["-i", dlc_path], capture_output=True,
                              text=True, env=env, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    # Real qairt-dlc-info output carries one "Info of graph: <name>" line per
    # graph. Verified against QAIRT 2.48.40 on single- and multi-graph DLCs.
    names = []
    marker = "info of graph:"
    for line in (proc.stdout or "").splitlines():
        idx = line.lower().find(marker)
        if idx < 0:
            continue
        name = line[idx + len(marker):].strip()
        if name and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Backend config auto-generation
# ---------------------------------------------------------------------------

def _auto_gen_backend_config(
    sdk_root: str,
    output_dir: str,
    graph_name: str,
    htp_version: str = "v73",
    host_arch_dir: str = "aarch64-windows-msvc",
    vtcm_mb: int = 8,
    opt_level: int = 3,
    soc_model: int | None = None,
    dsp_arch: str | None = None,
    pd_session: str | None = None,
) -> str:
    """
    Auto-generate backend_extensions.json and htp_backend_config_{version}.json.
    Returns path to backend_extensions.json.

    ``host_arch_dir`` selects the SDK lib dir for the extensions DLL. On WoS
    ARM64 this is always ``aarch64-windows-msvc``.

    ``vtcm_mb`` / ``opt_level`` default to the values this function has always
    emitted (8 MB, O=3). ``soc_model`` / ``dsp_arch`` / ``pd_session`` default
    to None and then contribute NO keys at all, so with no explicit target the
    emitted JSON is byte-for-byte identical to the pre-parameterization output.
    ``soc_id`` is written next to ``soc_model`` because the former is still
    accepted while deprecated in favour of the latter.
    """
    # Select HTP config filename based on version
    config_name = f"htp_backend_config_{htp_version}.json"
    # Key order matters: the legacy --auto-config path must stay byte-identical,
    # and json.dump preserves insertion order, so vtcm_mb goes in BEFORE "O".
    # vtcm_mb=None omits the key so the HTP backend picks the device maximum
    # (htp_backend.html: vtcm_mb 0/absent = use max). Only the legacy
    # --auto-config path passes 8 explicitly.
    graph_entry: dict = {"graph_names": [graph_name]}
    if vtcm_mb is not None:
        graph_entry["vtcm_mb"] = int(vtcm_mb)
    graph_entry["O"] = int(opt_level)
    device_entry: dict = {
        "cores": [{"rpc_control_latency": 100, "perf_profile": "burst"}]
    }
    if soc_model is not None:
        device_entry["soc_id"] = int(soc_model)
        device_entry["soc_model"] = int(soc_model)
    if dsp_arch:
        device_entry["dsp_arch"] = dsp_arch
    if pd_session:
        device_entry["pd_session"] = pd_session

    htp_config = {"graphs": [graph_entry], "devices": [device_entry]}
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
    sdk_root = cfg.get("qairt_root", "") or cfg.get("qairt_sdk_root", "")
    if sdk_root:
        os.environ["QAIRT_ROOT"] = sdk_root
        os.environ["QAIRT_SDK_ROOT"] = sdk_root
        os.environ["QNN_SDK_ROOT"] = sdk_root
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
                  htp_version="v73", soc_model=None, vtcm_mb=None,
                  dsp_arch=None, opt_level=3, pd_session=None):
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
        htp_version: HTP version to use -- any of _soc_targets.HTP_VERSIONS
            ("v68" ... "v81"; v66 excluded -- legacy QnnDsp backend). Which ones a host can actually build for is
            limited by the Stub libs that host's lib dir ships.
        soc_model:    Explicit target SoC id. None -> the representative SoC
                      for htp_version (see _soc_targets.SOC_TARGETS).
        vtcm_mb:      Explicit VTCM budget in MB. None -> 8 (historical value).
        dsp_arch:     Explicit dsp_arch string, e.g. "v81". None -> key omitted.
        opt_level:    Graph optimization level O (default 3, unchanged).
        pd_session:   PD session attribute. None -> key omitted.
    """
    model_path = os.path.abspath(model_path)
    if output_path:
        output_path = os.path.abspath(output_path)
    if output_dir:
        output_dir = os.path.abspath(output_dir)
    if config_file:
        config_file = os.path.abspath(config_file)
        # A config_file and the individual target flags are two disagreeing
        # sources for the same setting (see the mutual-exclusion note at the
        # --soc_model emission site). run_pipeline.py, the x86 sibling and the
        # delegation gate all reject the combination with exit 2; run_generator
        # is reachable directly (and via the ARM64 in-process path, which the
        # gate does NOT cover because has_local_htp() is True there), so it must
        # reject it too rather than silently dropping the flag.
        # ALL FOUR target flags must be listed: the ones missing here used to be
        # dropped without a word (they only ever land inside
        # _auto_gen_backend_config, which is skipped when config_file is set).
        _conflicting = [
            name for name, value in (
                ("--soc_model", soc_model),
                ("--vtcm_mb", vtcm_mb),
                ("--dsp_arch", dsp_arch),
                ("--pd_session", pd_session),
            ) if value is not None
        ]
        if _conflicting:
            print("[ERROR] --config_file cannot be combined with "
                  + "/".join(_conflicting)
                  + "; the config file already specifies the target "
                    "(soc_id/soc_model/dsp_arch/vtcm_mb/pd_session). "
                    "Pass one or the other.")
            sys.exit(2)
        # Fail loudly on a missing config: continuing would silently produce a
        # .bin for the WRONG target (previously only a Warning, further down).
        if not os.path.exists(config_file):
            print("[ERROR] config_file not found: " + config_file)
            sys.exit(2)

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
    # the .dlc via --dlc_path. See references/on_device_context_binary.md
    # § SNPE/DLC Context Binary Generation.
    is_dlc_input = model_path.lower().endswith(".dlc")

    # Map htp_version -> soc_model for DLC->bin (used instead of a config_file).
    # Table-driven via _soc_targets: the representative SoC for that Hexagon
    # version (e.g. v73 -> SC8380XP = 60, v81 -> SC8480XP = 88). The old inline
    # "88 if v81 else 60" silently gave every other version v73's id.
    # See references/on_device_context_binary.md § soc_model Reference.
    # An explicit soc_model overrides the mapping.
    #
    # CAVEAT: --soc_model is documented as "Specifies SIMULATED soc model value.
    # Default: 0 (use default soc model set by the backend)" (QNN docs,
    # general/tools.html), so it only takes effect on a host WITHOUT local HTP.
    # On a real HTP host the backend takes the SoC from the attached hardware and
    # the .bin reports socModel 2147483647 regardless of the value passed --
    # measured on QAIRT 2.48.40 for 60, 88 and 117 alike. The warning for that
    # lives further down, where the flag is actually emitted.
    if soc_model is not None:
        soc_model_arg = str(int(soc_model))
    else:
        _rep = htp_version_to_target_soc_model(htp_version)
        _rep_entry = soc_target(_rep) if _rep else None
        if _rep_entry is None:
            print(f"[ERROR] no default soc_model for htp_version={htp_version!r}; "
                  "pass --soc_model explicitly.")
            sys.exit(2)
        soc_model_arg = str(_rep_entry[0])

    # Auto-generate backend config if requested.
    # NOTE: DLC->bin historically does NOT use a config_file. With a config_file
    # the HTP backend extension requires a valid 'graph_names', and the DLC file
    # stem is not a reliable source for it ("Valid 'graph_names' must be
    # specified" failure). The documented minimal DLC->bin command instead uses
    # --soc_model (see references/on_device_context_binary.md). That default is preserved:
    # a DLC only gets a config when the caller explicitly asked for HTP tuning
    # (vtcm_mb / dsp_arch / pd_session), and then graph_names is read from the
    # DLC itself via qairt-dlc-info instead of being guessed.
    _dlc_wants_config = is_dlc_input and any(
        v is not None for v in (vtcm_mb, dsp_arch, pd_session)
    )
    if auto_config and not config_file and not is_dlc_input:
        # NOT gated on system == "windows" (see the DLC branch below): --auto-config
        # was silently ignored on linux-aarch64, a valid local-HTP host, and the
        # .bin came out built for the htp_version default SoC with rc=0.
        graph_name = os.path.splitext(os.path.basename(model_path))[0]
        config_file = _auto_gen_backend_config(
            sdk_root, gen_output_dir, graph_name, htp_version,
            host_arch_dir=backend_lib_dir,
            vtcm_mb=8 if vtcm_mb is None else int(vtcm_mb),
            opt_level=opt_level,
            soc_model=soc_model,
            dsp_arch=dsp_arch,
            pd_session=pd_session,
        )
        config_file = os.path.abspath(config_file)
    elif _dlc_wants_config and not config_file:
        # NOT gated on system == "windows": linux-aarch64 is an equally valid
        # local-HTP host (_host_arch.has_local_htp()), and these target flags are
        # platform-independent. Gating them on Windows silently dropped an
        # explicit --dsp_arch/--vtcm_mb there and shipped a .bin built for the
        # htp_version default SoC, rc=0 -- exactly the failure this work removes.
        _names = _dlc_graph_names_via_tool(sdk_root, model_path, backend_lib_dir)
        if not _names:
            print("[ERROR] --vtcm_mb/--dsp_arch/--pd_session need a backend config, "
                  "but graph names could not be read from the DLC via "
                  "qairt-dlc-info. Pass an explicit --config_file instead.")
            sys.exit(2)
        config_file = _auto_gen_backend_config(
            sdk_root, gen_output_dir, _names[0], htp_version,
            host_arch_dir=backend_lib_dir,
            vtcm_mb=vtcm_mb,
            opt_level=opt_level,
            soc_model=soc_model,
            dsp_arch=dsp_arch,
            pd_session=pd_session,
        )
        config_file = os.path.abspath(config_file)

    # RISK B4: --opt_level can ONLY reach the generator through the "O" key of a
    # backend config -- qnn-context-binary-generator has no -O / --opt_level
    # switch of its own. So when no config is generated and none was supplied,
    # a non-default --opt_level has nowhere to go and used to vanish without a
    # word. Warn instead of silently inventing a config: fabricating one would
    # change the produced .bin for existing callers (see the DLC->bin note
    # above), which the backward-compatibility contract forbids.
    # Keyed on "differs from the default 3" so a plain run stays silent.
    if not config_file and int(opt_level) != 3:
        print(f"[WARNING] --opt_level={int(opt_level)} is being IGNORED: no "
              f"backend config is generated for this invocation (no "
              f"--soc_model/--vtcm_mb/--dsp_arch/--pd_session and no "
              f"--config_file), and qnn-context-binary-generator has no "
              f"command-line optimization-level switch -- 'O' can only be set "
              f"inside a backend config. To make it take effect, pass a target "
              f"flag (e.g. --vtcm_mb) or an explicit --config_file containing "
              f"an \"O\": {int(opt_level)} entry.", file=sys.stderr)

    # -----------------------------------------------------------------------
    # PREFLIGHT GATE 2: HTP runtime files in generator CWD
    #
    # Copy the required runtime files from the SDK into gen_output_dir, then
    # verify every file is present.  Hard exit if any are still missing.
    # The generator is launched with cwd=gen_output_dir so it finds them.
    # DLC inputs need extra DLLs (is_dlc flag).
    #
    # Linux with local HTP (aarch64) needs the same gate: the Hexagon skel is
    # resolved through FastRPC, whose search path includes the process CWD, and
    # nothing else stages it there -- so a device-side build failed in the
    # generator with the same DspTransport error the Windows gate exists to
    # prevent. Only the device-side files are staged there (see
    # ``include_host_libs``), and ``is_dlc`` stays False because the DLC extras
    # are Windows DLL names -- the Linux DLC flow loads libQnnModelDlc.so from
    # the SDK by absolute path below.
    #
    # Linux x86_64 is deliberately untouched: it has no local HTP, so it never
    # opens a DSP session and lib/x86_64-linux-clang ships no skel to stage.
    # -----------------------------------------------------------------------
    if system == "windows":
        _copy_and_verify_htp_runtime_files(
            sdk_root, gen_output_dir, htp_version,
            host_arch_dir=backend_lib_dir, is_dlc=is_dlc_input,
        )
    elif system == "linux" and has_local_htp():
        _copy_and_verify_htp_runtime_files(
            sdk_root, gen_output_dir, htp_version,
            host_arch_dir=backend_lib_dir, include_host_libs=False,
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
        ]
        # --soc_model and a backend-extensions config are treated as MUTUALLY
        # EXCLUSIVE by this workflow: both name the target SoC, so honouring
        # both would mean two disagreeing sources for one setting. This is a
        # design decision, not an SDK restriction -- measured on QAIRT 2.48.40,
        # the generator accepts the combination (rc=0) and the config wins.
        #
        # Separately: on a host WITH local HTP, passing --soc_model at ALL makes
        # the backend log
        #   <E> SoC cannot be set more than once and must be called before any
        #       other API call
        # because the SoC was already set from the attached hardware (the SoC is
        # a one-shot global -- QNN_GLOBAL_CONFIG_OPTION_SOC_MODEL in
        # include/QNN/QnnGlobalConfig.h, "must be called before any other API
        # call"). Measured with --soc_model 60 / 88 / 117, with AND without a
        # config file; a bare run with no --soc_model is clean. The generator
        # still exits 0 and writes a .bin, whose socModel reads 2147483647.
        if not config_file:
            command.extend(["--soc_model", soc_model_arg])
            # --soc_model sets a SIMULATED soc model (QNN docs, tools.html) and
            # is overridden by the attached hardware on a host with local HTP.
            # Warn only when the caller asked for a specific target, since the
            # htp_version-derived default is the host's own SoC anyway. Keyed on
            # the host, not on the id's magnitude: every value behaves this way.
            if soc_model is not None and has_local_htp():
                print(f"[WARN] --soc_model {soc_model} sets a *simulated* SoC, "
                      f"which this host's local HTP overrides -- the .bin will "
                      f"report socModel 2147483647. Cross-SoC compilation needs "
                      f"a host without local HTP; on this host pass "
                      f"--dsp_arch/--vtcm_mb (backend config) or --config_file "
                      f"to control the target instead.")
    else:
        # A .dll/.so model library with target flags but no config has NOWHERE to
        # put them: the DLC branch above emits --soc_model, --auto-config builds
        # a config, but this branch does neither -- so the flags used to vanish
        # and the .bin was built for the default target with rc=0, discovered
        # only as err:14 on the device. Fail loud instead of guessing.
        _orphan_flags = [
            name for name, value in (
                ("--soc_model", soc_model),
                ("--vtcm_mb", vtcm_mb),
                ("--dsp_arch", dsp_arch),
                ("--pd_session", pd_session),
            ) if value is not None
        ]
        if _orphan_flags and not config_file:
            print("[ERROR] " + "/".join(_orphan_flags) + " cannot be applied to a "
                  "model library input (" + os.path.basename(model_path) + ").")
            print("  These target settings only reach the generator through a "
                  "backend config. Either add --auto-config, pass an explicit "
                  "--config_file, or convert the model to .dlc first.")
            sys.exit(2)
        command = [
            generator_path,
            "--backend", backend_path,
            "--model", model_path,
            "--output_dir", gen_output_dir,
            "--binary_file", bin_name,
        ]

    if config_file:
        # Existence was validated at entry; an auto-generated config is written
        # by _auto_gen_backend_config() and therefore also exists.
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
    # Direct-call host guard. This generator opens a native ARM64 .dll and
    # requires VS ARM64 env + aarch64 HTP runtime files. On any non-HTP host
    # transparently forward argv to the CPU-backend sibling; arg schemas
    # differ, so we translate.
    if not has_local_htp():
        import runpy
        sib = Path(__file__).resolve().parent / "qai_dev_gen_contextbin_x86.py"
        _pre = argparse.ArgumentParser(add_help=False)
        # The alias MUST be mirrored from the real parser below (--model,
        # --model_lib): without it a documented `--model_lib x.dlc` invocation
        # died here with "--model is required" while the user had in fact passed
        # the model -- and the very same command worked on an ARM64 host.
        _pre.add_argument("--model", "--model_lib", dest="model")
        # choices mirrors the real parser: without it an invalid version slipped
        # through the pre-parse and got derived into --dsp_arch below.
        _pre.add_argument("--htp_version", choices=list(HTP_VERSIONS),
                          default=DEFAULT_HTP_VERSION)
        _pre.add_argument("--output")
        _pre.add_argument("--output_dir")
        _pre.add_argument("--binary_file")
        _pre.add_argument("--profiling", action="store_true")
        # HTP target-selection args are forwarded rather than dropped. Before
        # this, every one of these was silently discarded here and the sibling
        # was always invoked with a hard-coded CPU backend, so a caller asking
        # for a specific SoC got a CPU-backend binary with no diagnostic.
        _pre.add_argument("--backend", choices=["htp", "cpu"], default=None)
        _pre.add_argument("--config_file")
        _pre.add_argument("--soc_model")
        _pre.add_argument("--vtcm_mb")
        _pre.add_argument("--dsp_arch")
        _pre.add_argument("--opt_level")
        _pre.add_argument("--pd_session")
        _known, _ = _pre.parse_known_args()
        if not _known.model:
            print("[ERROR] --model is required.")
            sys.exit(2)
        low = _known.model.lower()
        if not low.endswith(".dlc"):
            print(f"[ERROR] Non-HTP host cannot load ARM64 .dll model libs; "
                  f"pass a .dlc instead. Got: {_known.model}")
            sys.exit(2)
        _out = _known.output
        if not _out and _known.output_dir and _known.binary_file:
            _out = str(Path(_known.output_dir) / f"{_known.binary_file}.bin")
        # Any explicit HTP target request implies the HTP backend; otherwise the
        # historical CPU default is preserved exactly. A --config_file is itself
        # such a request (it names the target), but it is mutually exclusive with
        # the individual target flags -- the sibling rejects that combination, so
        # do not forward both.
        _targets = (_known.soc_model, _known.vtcm_mb, _known.dsp_arch,
                    _known.pd_session)
        if _known.config_file is not None and any(v is not None for v in _targets):
            print("[ERROR] --config_file cannot be combined with "
                  "--soc_model/--vtcm_mb/--dsp_arch/--pd_session; the config "
                  "file already "
                  "specifies the target. Pass one or the other.")
            sys.exit(2)
        # --htp_version has no counterpart on the sibling (there the Hexagon arch
        # is --dsp_arch), so parse_known_args() used to swallow it and the target
        # silently reverted to the backend default. Derive --dsp_arch from it,
        # matching run_pipeline.step3_context_binary so both entry points behave
        # identically. Only for a non-default version, and never over an explicit
        # --dsp_arch or a --config_file that already names the target.
        _dsp_arch = _known.dsp_arch
        if (_known.htp_version and _dsp_arch is None
                and _known.config_file is None
                and _known.htp_version != DEFAULT_HTP_VERSION):
            print(f"[INFO] --htp_version {_known.htp_version} maps to --dsp_arch "
                  f"{_known.htp_version} on the x86 generator.")
            _dsp_arch = _known.htp_version
        _wants_htp = any(
            v is not None for v in
            (_known.config_file, _known.soc_model, _known.vtcm_mb, _dsp_arch,
             _known.pd_session)
        )
        _backend = _known.backend or ("htp" if _wants_htp else "cpu")
        argv = [str(sib), "--dlc", _known.model, "--backend", _backend]
        if _out:
            argv += ["--output", _out]
        if _known.profiling:
            argv.append("--profiling")
        for _flag, _val in (("--config_file", _known.config_file),
                            ("--soc_model", _known.soc_model),
                            ("--vtcm_mb", _known.vtcm_mb),
                            ("--dsp_arch", _dsp_arch),
                            ("--opt_level", _known.opt_level),
                            ("--pd_session", _known.pd_session)):
            if _val is not None:
                argv += [_flag, str(_val)]
        sys.argv = argv
        runpy.run_path(str(sib), run_name="__main__")
        sys.exit(0)

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
                        help="Path to backend_extensions.json for HTP configuration "
                             "(any --htp_version; mutually exclusive with --soc_model)")
    parser.add_argument("--auto-config", action="store_true",
                        help="Auto-generate backend_extensions.json and "
                             "htp_backend_config_<htp_version>.json (WoS ARM64 only)")
    parser.add_argument("--profiling", action="store_true", help="Enable HTP optrace profiling")
    parser.add_argument("--htp_version", default=DEFAULT_HTP_VERSION,
                        choices=list(HTP_VERSIONS),
                        help="Target Hexagon/HTP version (default: "
                             + DEFAULT_HTP_VERSION + "). Which versions this "
                             "host can build for depends on the Stub libs its "
                             "SDK lib dir ships.")
    parser.add_argument("--soc_model", type=int, default=None,
                        help="Explicit target SoC id, e.g. 117 for IPQ9650. "
                             "Default: the representative SoC for "
                             "--htp_version (see _soc_targets.SOC_TARGETS).")
    parser.add_argument("--vtcm_mb", type=int, default=None,
                        help="Target VTCM budget in MB, e.g. 2. Default: 8.")
    parser.add_argument("--dsp_arch", default=None,
                        help="Target DSP arch string written into the HTP config, e.g. v81.")
    parser.add_argument("--opt_level", type=int, default=3,
                        help="Graph optimization level O (default: 3)")
    parser.add_argument("--pd_session", default=None,
                        help="PD session attribute, e.g. unsigned (default: key omitted)")

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
        soc_model=args.soc_model,
        vtcm_mb=args.vtcm_mb,
        dsp_arch=args.dsp_arch,
        opt_level=args.opt_level,
        pd_session=args.pd_session,
    )
