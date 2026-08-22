# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""ADB deploy + on-device qnn-net-run inference for aarch64 targets.

Supports Linux x86_64 host only (P1). Pushes model, QNN runtime libs (including
the hexagon-<dsp> skel dir and qnn-net-run binary itself from the SDK), input
.raw files, then runs qnn-net-run on device via adb shell and pulls results back.

Device OS → default target_arch mapping:
  android  → aarch64-android           (bin/aarch64-android/qnn-net-run)
  linux    → aarch64-oe-linux-gcc11.2  (bin/aarch64-oe-linux-gcc11.2/qnn-net-run)
"""

import argparse
import glob
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------

def _assert_ubuntu_x64() -> None:
    """Raise SystemExit with a clear message if not running on Linux x86_64."""
    machine = platform.machine()
    system = platform.system()
    if system != "Linux" or machine not in ("x86_64", "amd64"):
        sys.exit(
            f"[ERROR] adb_runner.py requires a Linux x86_64 host.\n"
            f"        Detected: {system} / {machine}\n"
            f"        Windows and aarch64 hosts are not supported in P1."
        )


# ---------------------------------------------------------------------------
# HTP runtime libs pushed from the reference script pattern
# ---------------------------------------------------------------------------

_HTP_RUNTIME_LIBS = [
    "libQnnHtp.so",
    "libQnnSystem.so",
    "libQnnHtpPrepare.so",
    "libQnnHtpNetRunExtensions.so",
    "libQnnHtpOptraceProfilingReader.so",
]

# Stub libs vary by SoC version; we probe all known versions and push what exists.
_HTP_STUB_VARIANTS = [
    ("libQnnHtpV73Stub.so", "libQnnHtpV73CalculatorStub.so"),
    ("libQnnHtpV79Stub.so", "libQnnHtpV79CalculatorStub.so"),
    ("libQnnHtpV81Stub.so", "libQnnHtpV81CalculatorStub.so"),
]

_CPU_LIBS = ["libQnnCpu.so"]
_GPU_LIBS = ["libQnnGpu.so"]

_DLC_MODEL_LIB = "libQnnModelDlc.so"

# ── Injection guard ───────────────────────────────────────────────────────────
# Strings passed to `adb shell "<cmd>"` are executed by the DEVICE shell, so any
# untrusted token interpolated into a shell command (model stem, input basename,
# target_arch, dsp_version) must be validated against a strict allow-list before
# use. A filename like ``x'; rm -rf /data;'.raw`` would otherwise break out of
# the quoting and run arbitrary device-side commands. Names/paths here are simple
# identifiers, so a conservative allow-list is safe and sufficient.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


def _assert_safe_token(value: str, what: str) -> str:
    """Reject any token that is not a simple safe identifier before it reaches
    a device-side `adb shell` command. Returns the value unchanged when safe."""
    if not value or not _SAFE_TOKEN_RE.match(value):
        raise ValueError(
            f"Unsafe {what} {value!r}: only letters, digits, '.', '_', '+', '-' "
            "are allowed (rejected to prevent device-side shell injection)."
        )
    return value


# device_os → default target_arch (SDK directory name used for both lib/ and bin/)
_DEVICE_OS_ARCH = {
    "android": "aarch64-android",
    "linux": "aarch64-oe-linux-gcc11.2",
}

_BACKEND_SO = {
    "htp": "libQnnHtp.so",
    "cpu": "libQnnCpu.so",
    "gpu": "libQnnGpu.so",
}


class AdbRunner:
    def __init__(
        self,
        sdk_root: str,
        device_id: str | None = None,
        adb_host: str | None = None,
        device_workdir: str = "/data/local/tmp/qai_run",
        qnn_net_run_path: str | None = None,
        backend: str = "htp",
        device_os: str = "android",
        target_arch: str | None = None,
        dsp_version: str = "v73",
        timeout: int = 300,
        push_timeout: int = 120,
        quiet: bool = False,
    ) -> None:
        """
        Args:
            sdk_root:           QAIRT SDK root (host-side, for finding runtime libs).
            device_id:          ADB device serial; None = auto-detect single device.
            adb_host:           ADB server host (-H flag); None = localhost.
            device_workdir:     Root work directory on device.
            qnn_net_run_path:   Absolute path to qnn-net-run on device after push; None = auto.
            backend:            "htp" | "cpu" | "gpu".
            device_os:          "android" or "linux" — selects default target_arch and
                                the SDK bin/ sub-directory used to push qnn-net-run.
            target_arch:        Explicit arch dir name under sdk_root/lib/ and sdk_root/bin/.
                                When None, derived from device_os:
                                  android → aarch64-android
                                  linux   → aarch64-oe-linux-gcc11.2
            dsp_version:        Hexagon DSP version string used for the skel dir (e.g. "v73").
            timeout:            qnn-net-run execution timeout in seconds.
            push_timeout:       adb push timeout per file in seconds.
            quiet:              Suppress [ADB] progress messages.
        """
        _assert_ubuntu_x64()
        self.sdk_root = sdk_root
        self.device_id = device_id
        self.adb_host = adb_host
        self.device_workdir = device_workdir
        self.qnn_net_run_path = qnn_net_run_path
        self.backend = backend.lower()
        self.device_os = device_os.lower()
        if target_arch is not None:
            self.target_arch = target_arch
        else:
            if self.device_os not in _DEVICE_OS_ARCH:
                raise ValueError(
                    f"Unknown device_os '{device_os}'. Expected 'android' or 'linux'. "
                    f"Use target_arch to set a custom SDK arch directory."
                )
            self.target_arch = _DEVICE_OS_ARCH[self.device_os]
        # target_arch / dsp_version are interpolated into device-side shell
        # commands (paths, LD_LIBRARY_PATH, dir names) — validate them.
        _assert_safe_token(self.target_arch, "target_arch")
        self.dsp_version = dsp_version
        _assert_safe_token(self.dsp_version, "dsp_version")
        self.timeout = timeout
        self.push_timeout = push_timeout
        self.quiet = quiet
        self._resolved_device: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[ADB] {msg}", flush=True)

    def _adb(self, *args: str) -> list[str]:
        """Build an adb command list with optional -H and -s flags."""
        cmd = ["adb"]
        if self.adb_host:
            cmd += ["-H", self.adb_host]
        if self._resolved_device:
            cmd += ["-s", self._resolved_device]
        cmd += list(args)
        return cmd

    def _run(self, cmd: list[str], capture: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # FR-02: device check
    # ------------------------------------------------------------------

    def _check_adb(self) -> None:
        """Raise RuntimeError if adb is not on PATH."""
        if not shutil.which("adb"):
            raise RuntimeError(
                "adb not found on PATH. Install android-tools-adb:\n"
                "  sudo apt-get install -y android-tools-adb"
            )

    def _check_device(self) -> str:
        """Return resolved device serial, raise RuntimeError if unavailable."""
        self._check_adb()
        cmd = ["adb"]
        if self.adb_host:
            cmd += ["-H", self.adb_host]
        cmd.append("devices")
        result = self._run(cmd)
        lines = result.stdout.strip().splitlines()
        # Lines look like:  "<serial>\tdevice"  or  "<serial>\toffline"
        devices = [
            parts[0]
            for line in lines[1:]
            if "\t" in line
            for parts in [line.split("\t")]
            if parts[1].strip() == "device"
        ]
        if not devices:
            raise RuntimeError(
                "No ADB devices connected.\n"
                "  • Check USB cable / TCP connection.\n"
                "  • Enable USB debugging on the device.\n"
                "  • Run 'adb devices' to verify."
            )
        if self.device_id:
            if self.device_id not in devices:
                raise RuntimeError(
                    f"Device '{self.device_id}' not found or offline.\n"
                    f"  Connected devices: {devices}"
                )
            self._resolved_device = self.device_id
        else:
            if len(devices) > 1:
                raise RuntimeError(
                    f"Multiple devices connected: {devices}\n"
                    "  Please specify --device_id <serial>."
                )
            self._resolved_device = devices[0]
        self._log(f"Using device: {self._resolved_device}")
        return self._resolved_device

    # ------------------------------------------------------------------
    # FR-03: push runtime libs (follows run_inference_qnn.sh pattern)
    # ------------------------------------------------------------------

    def _push_runtime_libs(self, device_lib_dir: str, device_dir: str, model_format: str = "bin") -> None:
        """Push QNN runtime libs from sdk_root to device_lib_dir on device.

        Also pushes the hexagon-<dsp> skel directory and qnn-net-run binary
        following the pattern in run_inference_qnn.sh.
        """
        host_lib_dir = os.path.join(self.sdk_root, "lib", self.target_arch)

        def push_lib(fname: str) -> None:
            src = os.path.join(host_lib_dir, fname)
            if os.path.exists(src):
                self._log(f"Pushing {fname}")
                res = self._run(
                    self._adb("push", src, device_lib_dir + "/"),
                    capture=True,
                    timeout=self.push_timeout,
                )
                if res.returncode != 0:
                    print(f"[WARN] Failed to push {fname}: {res.stderr.strip()}", file=sys.stderr)
            else:
                print(f"[WARN] {fname} not found in SDK ({host_lib_dir}), skipping.", file=sys.stderr)

        if self.backend == "htp":
            for lib in _HTP_RUNTIME_LIBS:
                push_lib(lib)
            for stub, calc_stub in _HTP_STUB_VARIANTS:
                push_lib(stub)
                push_lib(calc_stub)

            # Push hexagon-<dsp> skel directory (contains unsigned/ subdir)
            hexagon_dir = os.path.join(self.sdk_root, "lib", f"hexagon-{self.dsp_version}")
            if os.path.isdir(hexagon_dir):
                self._log(f"Pushing hexagon-{self.dsp_version}/ skel dir")
                res = self._run(
                    self._adb("push", hexagon_dir, device_dir + "/"),
                    capture=True,
                    timeout=self.push_timeout,
                )
                if res.returncode != 0:
                    print(f"[WARN] hexagon-{self.dsp_version} push failed: {res.stderr.strip()}", file=sys.stderr)
            else:
                print(f"[WARN] hexagon-{self.dsp_version} not found at {hexagon_dir}, skipping.", file=sys.stderr)

            if model_format == "dlc":
                push_lib(_DLC_MODEL_LIB)

        elif self.backend == "cpu":
            for lib in _CPU_LIBS:
                push_lib(lib)
        elif self.backend == "gpu":
            for lib in _GPU_LIBS:
                push_lib(lib)

        # Push qnn-net-run binary from SDK — mandatory, device is assumed not to have it
        qnn_bin_src = os.path.join(self.sdk_root, "bin", self.target_arch, "qnn-net-run")
        if not os.path.exists(qnn_bin_src):
            raise RuntimeError(
                f"qnn-net-run not found in SDK at {qnn_bin_src}.\n"
                f"  Check --sdk_root and --target_arch / --device_os."
            )
        self._log(f"Pushing qnn-net-run from SDK ({self.target_arch})")
        res = self._run(
            self._adb("push", qnn_bin_src, device_dir + "/"),
            capture=True,
            timeout=self.push_timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(f"adb push qnn-net-run failed: {res.stderr.strip()}")
        self._run(self._adb("shell", "chmod +x " + shlex.quote(f"{device_dir}/qnn-net-run")), capture=True)

    # ------------------------------------------------------------------
    # FR-04: push model + inputs
    # ------------------------------------------------------------------

    def push(self, model_path: str, input_files: list[str]) -> str:
        """Push model, inputs, and runtime libs to device. Returns device model workdir."""
        if self._resolved_device is None:
            self._check_device()

        model_stem = _assert_safe_token(Path(model_path).stem, "model name")
        device_model_dir = f"{self.device_workdir}/{model_stem}"
        device_lib_dir = f"{device_model_dir}/{self.target_arch}"
        device_inputs_dir = f"{device_model_dir}/inputs"
        device_outputs_dir = f"{device_model_dir}/output"

        # Create directory tree on device (quote each path for the device shell)
        self._log(f"Creating device dirs under {device_model_dir}")
        self._run(
            self._adb(
                "shell",
                "mkdir -p "
                + " ".join(
                    shlex.quote(p)
                    for p in (device_lib_dir, device_inputs_dir, device_outputs_dir)
                ),
            ),
            capture=True,
        )

        # Push model
        self._log(f"Pushing model: {model_path}")
        res = self._run(
            self._adb("push", model_path, device_model_dir + "/"),
            capture=True,
            timeout=self.push_timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(f"adb push model failed: {res.stderr.strip()}")

        # Push input files + build input_list.txt on device
        device_input_paths = []
        for inp in input_files:
            fname = _assert_safe_token(os.path.basename(inp), "input filename")
            dest = f"{device_inputs_dir}/{fname}"
            self._log(f"Pushing input: {fname}")
            res = self._run(
                self._adb("push", inp, dest),
                capture=True,
                timeout=self.push_timeout,
            )
            if res.returncode != 0:
                raise RuntimeError(f"adb push input failed ({fname}): {res.stderr.strip()}")
            device_input_paths.append(dest)

        # Write input_list.txt (space-separated paths, matching onnxwrapper_x86
        # format). Build it as a HOST temp file and `adb push` it — never via a
        # device-side `echo '...'`, so input paths never reach the device shell.
        input_list_content = " ".join(device_input_paths)
        import tempfile

        _tmp = tempfile.NamedTemporaryFile(
            "w", suffix="_input_list.txt", delete=False, encoding="utf-8", newline="\n"
        )
        try:
            _tmp.write(input_list_content + "\n")
            _tmp.close()
            res = self._run(
                self._adb("push", _tmp.name, f"{device_inputs_dir}/input_list.txt"),
                capture=True,
                timeout=self.push_timeout,
            )
            if res.returncode != 0:
                raise RuntimeError(f"adb push input_list.txt failed: {res.stderr.strip()}")
        finally:
            try:
                os.unlink(_tmp.name)
            except OSError:
                pass
        self._log(f"Created input_list.txt with {len(device_input_paths)} input(s)")

        # Push runtime libs
        model_format = "dlc" if model_path.endswith(".dlc") else "bin"
        self._push_runtime_libs(device_lib_dir, device_model_dir, model_format=model_format)

        return device_model_dir

    # ------------------------------------------------------------------
    # FR-05: run on device
    # ------------------------------------------------------------------

    def _resolve_qnn_net_run(self, device_model_dir: str) -> str:
        """Return the path to qnn-net-run on device.

        Priority:
          1. Explicit --qnn_net_run override
          2. The copy we just pushed from SDK: <device_model_dir>/qnn-net-run
        """
        if self.qnn_net_run_path:
            return self.qnn_net_run_path
        return f"{device_model_dir}/qnn-net-run"

    def run(
        self,
        model_path: str,
        input_files: list[str],
        output_dir: str,
        backend: str | None = None,
        profiling_level: str = "basic",
        perf_profile: str = "burst",
    ) -> list[str]:
        """Push, execute qnn-net-run on device, pull outputs. Returns list of local .raw paths."""
        if self._resolved_device is None:
            self._check_device()

        effective_backend = (backend or self.backend).lower()
        model_stem = _assert_safe_token(Path(model_path).stem, "model name")
        device_model_dir = self.push(model_path, input_files)
        device_lib_dir = f"{device_model_dir}/{self.target_arch}"

        qnn_exe = self._resolve_qnn_net_run(device_model_dir)
        model_fname = _assert_safe_token(os.path.basename(model_path), "model filename")
        is_context = model_path.endswith(".bin")
        is_dlc = model_path.endswith(".dlc")
        if is_context:
            model_args = f"--retrieve_context $PROJECT/{model_fname}"
        elif is_dlc:
            model_args = (
                f"--model {self.target_arch}/{_DLC_MODEL_LIB} "
                f"--dlc_path $PROJECT/{model_fname}"
            )
        else:
            model_args = f"--model $PROJECT/{model_fname}"
        backend_so = _BACKEND_SO.get(effective_backend, f"libQnn{effective_backend.capitalize()}.so")

        # Build shell command matching run_inference_qnn.sh pattern
        shell_cmd = (
            f"export PROJECT={device_model_dir} && "
            f"export LD_LIBRARY_PATH=$PROJECT/{self.target_arch}:$LD_LIBRARY_PATH && "
            f"export ADSP_LIBRARY_PATH=$PROJECT/hexagon-{self.dsp_version}/unsigned && "
            f"export PATH=$PROJECT:$PROJECT/{self.target_arch}:$PATH && "
            f"cd $PROJECT && "
            f"{qnn_exe} "
            f"{model_args} "
            f"--backend {self.target_arch}/{backend_so} "
            f"--input_list inputs/input_list.txt "
            f"--output_dir output "
            f"--profiling_level {profiling_level} "
            f"--perf_profile {perf_profile}"
        )

        self._log(f"Executing qnn-net-run on device ({self._resolved_device})")
        self._log(f"  model_args={model_args!r}  backend={backend_so}")

        # Stream device stdout/stderr live to the console (capture_output=False)
        # so long runs show progress; on failure the device diagnostics are
        # therefore visible above this error line.
        res = subprocess.run(
            self._adb("shell", shell_cmd),
            capture_output=False,
            text=True,
            timeout=self.timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"qnn-net-run failed (exit={res.returncode}); see device output above "
                "for diagnostics."
            )

        print(f"[ADB Execute] qnn-net-run exit=0", flush=True)
        return self._pull_outputs(model_stem, output_dir)

    # ------------------------------------------------------------------
    # FR-06: pull outputs
    # ------------------------------------------------------------------

    def _pull_outputs(self, model_stem: str, local_output_dir: str) -> list[str]:
        """Pull device output/ dir to host. Returns list of local .raw paths."""
        os.makedirs(local_output_dir, exist_ok=True)
        device_output_dir = f"{self.device_workdir}/{model_stem}/output"
        self._log(f"Pulling outputs from {device_output_dir}")
        res = self._run(
            self._adb("pull", device_output_dir + "/", local_output_dir + "/"),
            capture=True,
            timeout=self.push_timeout,
        )
        if res.returncode != 0:
            print(f"[WARN] adb pull stderr: {res.stderr.strip()}", file=sys.stderr)

        raw_files = sorted(glob.glob(os.path.join(local_output_dir, "**", "*.raw"), recursive=True))
        if not raw_files:
            raise RuntimeError(
                "No .raw output files found after adb pull. Check qnn-net-run logs above."
            )
        for f in raw_files:
            print(f"[ADB Pull] {device_output_dir} → {f}", flush=True)
        return raw_files

    # ------------------------------------------------------------------
    # FR-07: cleanup
    # ------------------------------------------------------------------

    def cleanup(self, model_stem: str) -> None:
        """Remove device work directory for model_stem."""
        device_dir = f"{self.device_workdir}/{model_stem}"
        self._log(f"Cleaning up {device_dir} on device")
        self._run(self._adb("shell", "rm -rf " + shlex.quote(device_dir)), capture=True)


# ---------------------------------------------------------------------------
# Convenience: list connected devices (used by CLI --list_devices)
# ---------------------------------------------------------------------------

def list_devices(adb_host: str | None = None) -> list[dict]:
    """Return a list of dicts with keys 'serial' and 'state'."""
    _assert_ubuntu_x64()
    if not shutil.which("adb"):
        raise RuntimeError("adb not found on PATH.")
    cmd = ["adb"]
    if adb_host:
        cmd += ["-H", adb_host]
    cmd.append("devices")
    result = subprocess.run(cmd, capture_output=True, text=True)
    devices = []
    for line in result.stdout.strip().splitlines()[1:]:
        if "\t" in line:
            serial, state = line.split("\t", 1)
            devices.append({"serial": serial.strip(), "state": state.strip()})
    return devices


# ---------------------------------------------------------------------------
# FR-08: CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ADB deploy + qnn-net-run on-device inference",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--model",
        help=(
            "Host-side model file: "
            ".bin (context binary, uses --retrieve_context) or "
            ".dlc (DLC, uses --model libQnnModelDlc.so --dlc_path)"
        ),
    )
    p.add_argument("--inputs", nargs="+", metavar="FILE", help="Input .raw file(s)")
    p.add_argument("--output_dir", default="./adb_outputs", help="Local dir to receive results (default: ./adb_outputs)")
    p.add_argument("--sdk_root", default=os.environ.get("QAIRT_SDK_ROOT", ""), help="QAIRT SDK root (or $QAIRT_SDK_ROOT)")
    p.add_argument("--backend", default="htp", choices=["htp", "cpu", "gpu"], help="QNN backend (default: htp)")
    p.add_argument("--device_os", default="android", choices=["android", "linux"],
                   help="Target device OS: 'android' → aarch64-android, 'linux' → aarch64-oe-linux-gcc11.2 (default: android)")
    p.add_argument("--target_arch", default=None,
                   help="Override SDK arch dir name under sdk/lib/ and sdk/bin/ (default: derived from --device_os)")
    p.add_argument("--dsp_version", default="v73", help="DSP/hexagon version, e.g. v73, v79, v81 (default: v73)")
    p.add_argument("--device_id", default=None, help="ADB device serial (optional if only one device)")
    p.add_argument("--adb_host", default=None, metavar="HOST", help="ADB server host for -H flag (optional)")
    p.add_argument("--qnn_net_run", default=None, dest="qnn_net_run_path", help="Override qnn-net-run path on device")
    p.add_argument("--device_workdir", default="/data/local/tmp/qai_run", help="Work root on device (default: /data/local/tmp/qai_run)")
    p.add_argument("--timeout", type=int, default=300, help="qnn-net-run execution timeout in seconds (default: 300)")
    p.add_argument("--push_timeout", type=int, default=120, help="adb push timeout per file in seconds (default: 120)")
    p.add_argument("--profiling_level", default="basic", help="qnn-net-run --profiling_level (default: basic)")
    p.add_argument("--perf_profile", default="burst", help="qnn-net-run --perf_profile (default: burst)")
    p.add_argument("--no_cleanup", action="store_true", help="Keep device temp files after run")
    p.add_argument("--quiet", action="store_true", help="Suppress [ADB] progress output")
    p.add_argument("--list_devices", action="store_true", help="List connected ADB devices and exit")
    return p


def main() -> None:
    _assert_ubuntu_x64()
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_devices:
        devices = list_devices(adb_host=getattr(args, "adb_host", None))
        if not devices:
            print("No ADB devices connected.", file=sys.stderr)
            sys.exit(1)
        print(f"{'SERIAL':<30} STATE")
        print("-" * 40)
        for d in devices:
            print(f"{d['serial']:<30} {d['state']}")
        return

    if not args.model:
        parser.error("--model is required")
    if not args.inputs:
        parser.error("--inputs is required")
    if not args.sdk_root:
        parser.error("--sdk_root is required (or set $QAIRT_SDK_ROOT)")
    if not os.path.isfile(args.model):
        sys.exit(f"[ERROR] Model file not found: {args.model}")
    missing = [f for f in args.inputs if not os.path.isfile(f)]
    if missing:
        sys.exit(f"[ERROR] Input file(s) not found: {missing}")

    runner = AdbRunner(
        sdk_root=args.sdk_root,
        device_id=args.device_id,
        adb_host=args.adb_host,
        device_workdir=args.device_workdir,
        qnn_net_run_path=args.qnn_net_run_path,
        backend=args.backend,
        device_os=args.device_os,
        target_arch=args.target_arch,
        dsp_version=args.dsp_version,
        timeout=args.timeout,
        push_timeout=args.push_timeout,
        quiet=args.quiet,
    )

    try:
        output_files = runner.run(
            model_path=args.model,
            input_files=args.inputs,
            output_dir=args.output_dir,
            profiling_level=args.profiling_level,
            perf_profile=args.perf_profile,
        )
        print(f"\n[Result] {len(output_files)} output file(s) written to {args.output_dir}")
        for f in output_files:
            print(f"  {f}")
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"[ERROR] {exc}")
    except subprocess.TimeoutExpired as exc:
        sys.exit(f"[ERROR] adb operation timed out: {exc}")
    finally:
        if not args.no_cleanup:
            try:
                model_stem = _assert_safe_token(Path(args.model).stem, "model name")
                runner.cleanup(model_stem)
            except ValueError:
                # Unsafe model name never got pushed — nothing to clean up.
                pass


if __name__ == "__main__":
    main()
