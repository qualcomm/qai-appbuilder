# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# Repo-root-independent import: this file lives next to _host_arch.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _host_arch import sdk_bin_subdir  # noqa: E402
from _soc_targets import resolve_target_platform  # noqa: E402


def _arch_dir_for_host() -> str:
    """SDK bin/lib subdirectory for the CURRENT host.

    Delegates to _host_arch.sdk_bin_subdir() -- the single source of truth.
    Do NOT re-implement the mapping here: the copy that used to live in this
    function drifted and returned aarch64-linux-gcc (unquoted here on
    purpose -- a regression test greps for the quoted literal), a directory
    that exists in NO QAIRT SDK. The real ARM64-Linux host toolchain dir is
    aarch64-oe-linux-gcc11.2 -- the only aarch64 dir shipping the host tools
    (qairt-dlc-info / qairt-converter / qairt-quantizer); -oe-linux-gcc9.3
    and -ubuntu-gcc9.4 carry device runtime only.
    """
    return sdk_bin_subdir()


def _backend_library_name(backend: str, is_windows: bool) -> str:
    if is_windows:
        return "QnnHtp.dll" if backend == "htp" else "QnnCpu.dll"
    return "libQnnHtp.so" if backend == "htp" else "libQnnCpu.so"


def _qnn_model_library_for_dlc(is_windows: bool) -> str:
    return "QnnModelDlc.dll" if is_windows else "libQnnModelDlc.so"


def _netrun_extensions_name(is_windows: bool) -> str:
    return (
        "QnnHtpNetRunExtensions.dll" if is_windows
        else "libQnnHtpNetRunExtensions.so"
    )


def _dlc_utils_supported_pytags(sdk_root: str) -> set:
    """Python tags (e.g. {"310", "312"}) the SDK ships dlc_utils binaries for.

    ``qairt-dlc-info`` wraps a NATIVE extension (``libDlModelToolsPy<TAG>``)
    built only for specific Python versions; under any other interpreter it
    exits 1 with "Failed to find necessary package".
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
    """Pick a Python interpreter matching the SDK dlc_utils build, or None."""
    tags = _dlc_utils_supported_pytags(sdk_root)
    if not tags:
        return sys.executable

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


def _dlc_graph_names(sdk_root: str, dlc_path: str) -> list[str]:
    """Best-effort extraction of graph names from a .dlc via qairt-dlc-info.

    A generated HTP backend-extensions config needs a valid ``graph_names``
    entry; the DLC file stem is NOT a reliable source for it. Returns [] when
    the names cannot be determined, and the caller then skips config
    generation rather than emitting a config that the backend would reject.

    Note ``qairt-dlc-info`` ships as an extension-less PYTHON SCRIPT, which on
    Windows cannot be spawned directly (WinError 193) and must be run through an
    explicit interpreter -- and that interpreter's version must match the SDK's
    ``libDlModelToolsPy<TAG>`` native module, or the import fails with exit 1.
    """
    exe = "qairt-dlc-info"
    candidates = []
    for arch in (_arch_dir_for_host(), "x86_64-windows-msvc",
                 "x86_64-linux-clang"):
        bin_dir = os.path.join(sdk_root, "bin", arch)
        for cand in (exe + ".exe", exe):
            p = os.path.join(bin_dir, cand)
            if p not in candidates:
                candidates.append(p)
    tool = next((c for c in candidates if os.path.exists(c)), None)
    if tool is None:
        return []

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
        proc = subprocess.run(
            argv + ["-i", dlc_path],
            capture_output=True, text=True, env=env, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    # Real qairt-dlc-info output carries one "Info of graph: <name>" line per
    # graph. Verified against QAIRT 2.48.40 on single- and multi-graph DLCs.
    names: list[str] = []
    marker = "info of graph:"
    for line in (proc.stdout or "").splitlines():
        idx = line.lower().find(marker)
        if idx < 0:
            continue
        name = line[idx + len(marker):].strip()
        if name and name not in names:
            names.append(name)
    return names


def _write_htp_config(
    sdk_root: str,
    out_dir: str,
    graph_names: list[str],
    is_windows: bool,
    vtcm_mb: int | None,
    soc_model: int | None,
    dsp_arch: str | None,
    opt_level: int,
    pd_session: str | None,
) -> str:
    """Write htp_config + backend_extensions JSON; return the extensions path.

    Field names follow the HTP backend-extensions schema
    (docs/QAIRT-Docs/QNN/general/htp/htp_backend.html). ``soc_id`` is written
    alongside ``soc_model`` because ``soc_id`` is still accepted while being
    deprecated in favour of ``soc_model``; writing both is version-proof.
    ``perf_profile`` is deliberately omitted -- the schema marks it as used by
    qnn-net-run only, not by qnn-context-binary-generator.
    """
    graph_entry: dict = {"graph_names": list(graph_names), "O": opt_level}
    if vtcm_mb is not None:
        graph_entry["vtcm_mb"] = int(vtcm_mb)

    device_entry: dict = {"device_id": 0}
    if soc_model is not None:
        device_entry["soc_id"] = int(soc_model)
        device_entry["soc_model"] = int(soc_model)
    if dsp_arch:
        device_entry["dsp_arch"] = dsp_arch
    # Default applied HERE, not on the parameter: pd_session's sentinel must stay
    # None so the "did the user request a target?" test in run_generator (which
    # is `v is not None` over every target flag) does not fire on a default.
    # A non-None default there made the plain CPU route and a lone --config_file
    # both exit 2, blaming flags the user never passed.
    device_entry["pd_session"] = pd_session or "unsigned"

    htp_config = {"graphs": [graph_entry], "devices": [device_entry]}

    os.makedirs(out_dir, exist_ok=True)
    tag = dsp_arch or "auto"
    htp_cfg_path = os.path.join(out_dir, f"htp_config_{tag}.json")
    with open(htp_cfg_path, "w") as handle:
        json.dump(htp_config, handle, indent=4)

    ext_lib = os.path.join(
        sdk_root, "lib", _arch_dir_for_host(),
        _netrun_extensions_name(is_windows),
    )
    ext_config = {
        "backend_extensions": {
            "shared_library_path": ext_lib,
            "config_file_path": htp_cfg_path,
        }
    }
    ext_path = os.path.join(out_dir, "backend_extensions.json")
    with open(ext_path, "w") as handle:
        json.dump(ext_config, handle, indent=4)

    print(f"[INFO] Generated HTP config: {htp_cfg_path}")
    print(f"[INFO] Generated backend extensions: {ext_path}")
    return ext_path


def _normalize_binary_basename(output_path: str | None, input_path: str) -> str:
    if output_path:
        name = Path(output_path).name
    else:
        model_name = Path(input_path).name
        # Keep legacy naming behavior from original script but avoid .bin.bin
        name = Path(model_name).stem

    if name.lower().endswith(".bin"):
        name = name[:-4]
    return name


def run_generator(
    model_path: str | None = None,
    dlc_path: str | None = None,
    output_path: str | None = None,
    backend: str = "htp",
    profiling: bool = False,
    config_file: str | None = None,
    soc_model: int | None = None,
    vtcm_mb: int | None = None,
    dsp_arch: str | None = None,
    opt_level: int = 3,
    pd_session: str | None = None,
) -> None:
    sdk_root = os.environ.get("QAIRT_SDK_ROOT")
    if not sdk_root:
        print("Error: QAIRT_SDK_ROOT environment variable is not set.")
        sys.exit(1)

    if bool(model_path) == bool(dlc_path):
        print("Error: provide exactly one of --model or --dlc.")
        sys.exit(2)

    arch_dir = _arch_dir_for_host()
    is_windows = platform.system().lower() == "windows"
    generator_exe = "qnn-context-binary-generator.exe" if is_windows else "qnn-context-binary-generator"

    generator_path = os.path.join(sdk_root, "bin", arch_dir, generator_exe)
    backend_lib = _backend_library_name(backend, is_windows)
    backend_path = os.path.join(sdk_root, "lib", arch_dir, backend_lib)

    if not os.path.exists(generator_path):
        print(f"Error: generator not found at {generator_path}")
        sys.exit(2)
    if not os.path.exists(backend_path):
        print(f"Error: backend library not found at {backend_path}")
        sys.exit(2)

    command = [generator_path, "--backend", backend_path]
    input_for_naming = ""

    if model_path:
        model_path = os.path.abspath(model_path)
        if not os.path.exists(model_path):
            print(f"Error: model not found: {model_path}")
            sys.exit(2)
        input_for_naming = model_path
        command.extend(["--model", model_path])
    else:
        dlc_path = os.path.abspath(dlc_path or "")
        if not os.path.exists(dlc_path):
            print(f"Error: dlc not found: {dlc_path}")
            sys.exit(2)
        dlc_model_lib = os.path.join(sdk_root, "lib", arch_dir, _qnn_model_library_for_dlc(is_windows))
        if not os.path.exists(dlc_model_lib):
            print(f"Error: DLC model library not found at {dlc_model_lib}")
            sys.exit(2)
        input_for_naming = dlc_path
        command.extend(["--model", dlc_model_lib, "--dlc_path", dlc_path])

    binary_name = _normalize_binary_basename(output_path, input_for_naming)
    command.extend(["--binary_file", binary_name])

    # HTP target selection. An explicit --config_file always wins. Otherwise a
    # config is generated ONLY when the caller asked for a specific target
    # (soc_model / vtcm_mb / dsp_arch); with none of them set the command line
    # stays byte-for-byte what it was before this feature existed.
    #
    # NOTE --config_file and the target flags are mutually exclusive rather than
    # additive: the config already carries soc_id/soc_model/vtcm_mb/dsp_arch, so
    # honouring both would mean two disagreeing sources for the same setting.
    # Reject the combination instead of silently ignoring one of them.
    # The trigger set MUST list every target flag, pd_session included: a flag
    # missing here is silently dropped (it is only ever written inside
    # _write_htp_config, which runs only when this test passes) and is also
    # skipped by the mutual-exclusion check above. Keep this tuple identical to
    # _dlc_wants_config in qai_dev_gen_contextbin.py so both hosts agree.
    _target_flags = (soc_model, vtcm_mb, dsp_arch, pd_session)
    effective_config = config_file
    if config_file is not None and any(v is not None for v in _target_flags):
        print("Error: --config_file cannot be combined with "
              "--soc_model/--vtcm_mb/--dsp_arch/--pd_session; the config file "
              "already specifies the target. Pass one or the other.")
        sys.exit(2)
    if effective_config is None and any(v is not None for v in _target_flags):
        if backend != "htp":
            print("Error: --soc_model/--vtcm_mb/--dsp_arch/--pd_session "
                  "require --backend htp.")
            sys.exit(2)
        cfg_dir = (
            os.path.dirname(os.path.abspath(output_path)) if output_path
            else os.path.dirname(os.path.abspath(input_for_naming))
        )
        graph_names: list[str] = []
        if dlc_path:
            graph_names = _dlc_graph_names(sdk_root, dlc_path)
            if not graph_names:
                print("Error: could not determine graph names from the DLC via "
                      "qairt-dlc-info; cannot emit a valid HTP config. Pass an "
                      "explicit --config_file instead.")
                sys.exit(2)
        else:
            graph_names = [Path(input_for_naming).stem]
        effective_config = _write_htp_config(
            sdk_root, cfg_dir, graph_names, is_windows,
            vtcm_mb, soc_model, dsp_arch, opt_level, pd_session,
        )

    if effective_config:
        effective_config = os.path.abspath(effective_config)
        if not os.path.exists(effective_config):
            print(f"Error: config file not found: {effective_config}")
            sys.exit(2)
        command.extend(["--config_file", effective_config])
    elif opt_level is not None:
        # opt_level rides inside the HTP config's graph entry, so with neither a
        # config nor a target flag it has nowhere to land. Warn rather than drop
        # it silently -- the ARM64 sibling prints the same kind of [WARNING].
        print("[WARNING] --opt_level is ignored: it is written into the HTP "
              "backend config, and none is in play (no --config_file and no "
              "--soc_model/--vtcm_mb/--dsp_arch/--pd_session).")

    if profiling:
        command.extend(["--profiling_level", "detailed", "--profiling_option", "optrace"])

    print(f"Host arch dir: {arch_dir}")
    print(f"Backend: {backend}")
    print(f"Input type: {'dlc' if dlc_path else 'model-lib'}")
    print(f"Executing: {' '.join(command)}")

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        print(f"Error executing command: {error}")
        sys.exit(1)

    generated = os.path.join("output", f"{binary_name}.bin")
    if not os.path.exists(generated):
        # Fallback search. It MUST filter the way the ARM64 sibling does
        # (see qai_dev_gen_contextbin.py's "intermediate-file exclusion"):
        # a bare sorted()[0] happily picked a stale .bin from a previous run or
        # a 0-byte placeholder, moved it to the output path and printed
        # "Output: ..." with rc=0 -- handing the user a binary that is not the
        # one they just built.
        candidates = []
        if os.path.isdir("output"):
            for path in sorted(Path("output").rglob("*.bin")):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size <= 1024:
                    continue  # placeholder / truncated intermediate
                if path.parent == Path("output") and path.stem != binary_name:
                    continue  # a different model's binary sitting in output/
                candidates.append((size, str(path)))
        if not candidates:
            print("Error: output context binary not found in ./output")
            print(f"  Looked for '{binary_name}.bin' (and any .bin > 1024 bytes "
                  "belonging to it). Stale or 0-byte files are ignored on "
                  "purpose so a previous run's binary is never mistaken for "
                  "this one.")
            sys.exit(1)
        # Largest wins: a real context binary dwarfs any leftover stub.
        generated = max(candidates)[1]

    if output_path:
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        shutil.move(generated, output_path)
        print(f"Output: {output_path}")
    else:
        print(f"Output: {os.path.abspath(generated)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QNN Context Binary Generator (x86/CPU+HTP aware)")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--model", help="Path to the model .dll/.so file")
    input_group.add_argument("--dlc", help="Path to the input .dlc file")
    parser.add_argument("--output", help="Output path for context binary")
    parser.add_argument("--backend", choices=["htp", "cpu"], default="htp", help="QNN backend library to use")
    parser.add_argument("--profiling", action="store_true", help="Enable HTP optrace profiling")
    parser.add_argument("--config_file", help="Explicit backend_extensions.json (overrides auto-generation)")
    parser.add_argument("--soc_model", type=int, default=None,
                        help="Target SoC id, e.g. 117 for IPQ9650. Written as both "
                             "soc_id and soc_model. Implies HTP config generation.")
    parser.add_argument("--vtcm_mb", type=int, default=None,
                        help="Target VTCM budget in MB, e.g. 2. Implies HTP config generation.")
    parser.add_argument("--dsp_arch", default=None,
                        help="Target DSP arch string, e.g. v81. Implies HTP config generation.")
    parser.add_argument("--opt_level", type=int, default=3,
                        help="Graph optimization level O (default: 3)")
    parser.add_argument("--pd_session", default=None,
                        help="PD session attribute (default: unsigned). Passing it "
                             "counts as requesting an explicit HTP target.")
    parser.add_argument("--target_platform", default=None,
                        help="Platform NAME instead of raw numbers, e.g. IPQ9650 "
                             "or SC8480XP. Fills in --soc_model/--dsp_arch from "
                             "_soc_targets; explicit flags still win.")

    args = parser.parse_args()

    # Resolve a platform name into the two numbers the HTP config needs, so a
    # caller who knows their platform does not have to hand-carry a soc id and
    # an arch string. An unknown name is a HARD error: silently falling back to
    # "no HTP target" would emit a generic .bin that only fails on the device.
    if args.target_platform:
        _soc_id, _arch, _err = resolve_target_platform(args.target_platform)
        if _err:
            print(f"[ERROR] {_err}", file=sys.stderr)
            sys.exit(2)
        if args.soc_model is None:
            args.soc_model = _soc_id
        if args.dsp_arch is None:
            args.dsp_arch = _arch
        print(f"[INFO] --target_platform {args.target_platform} -> "
              f"soc_model={args.soc_model} dsp_arch={args.dsp_arch}")

    run_generator(args.model, args.dlc, args.output, args.backend, args.profiling,
                  config_file=args.config_file, soc_model=args.soc_model,
                  vtcm_mb=args.vtcm_mb, dsp_arch=args.dsp_arch,
                  opt_level=args.opt_level, pd_session=args.pd_session)
