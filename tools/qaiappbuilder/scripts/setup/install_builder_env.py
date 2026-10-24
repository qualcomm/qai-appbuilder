# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Model-conversion environment installer (standalone, no setup_env.bat).

Installs what ONNX -> DLC -> .bin conversion needs, and nothing else:

  1. python310  x86_64 CPython 3.10 interpreter        (uv python install)
  2. venv       <envs>\.venv_x64_310
  3. deps       converter packages into that venv      (uv pip / pip)
  4. sdk_download  QAIRT SDK zip (~2 GB, resumable)
  5. sdk_extract   unzip to C:\Qualcomm\AIStack\QAIRT\<version>
  6. config     data\config\qairt_env.json
  7. verify     interpreter arch, key imports, SDK marker

Design points
-------------
* Every step is idempotent: a finished step is skipped on the next run, a partial
  download is resumed. "Retry" == run the script again.
* Machine-readable output: ONE JSON object per stdout line (see ``Reporter``).
  Events: ``start`` / ``step`` / ``progress`` / ``log`` / ``result``.
* Unzipping tool order: a 7-Zip already on the machine > download 7za (only now,
  only if needed) > Python ``zipfile`` (slowest, always available).
* SDK validity marker: ``bin\aarch64-windows-msvc\qnn-context-binary-generator.exe``.
* Never touches anything outside --root / --envs-dir / --sdk-root, so a test can
  point all three at a scratch directory.

Exit codes: 0 ok, 1 failed, 2 bad arguments / unsupported OS, 3 cancelled,
4 another instance is already running.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Optional, Sequence

# ─── Constants ────────────────────────────────────────────────────────────────

PY310_SPEC = "cpython-3.10-windows-x86_64"
SDK_MARKER_REL = Path("bin") / "aarch64-windows-msvc" / "qnn-context-binary-generator.exe"
# Written into the SDK root ONLY when this script extracted that SDK itself. The uninstaller offers to delete an SDK
# ONLY when this file is present: an SDK the user (or another tool) installed is never touched.
OWNER_MARKER_NAME = ".installed_by_qai_modelbuilder"
SDK_MIN_ZIP_BYTES = 1_500_000_000  # same floor as setup_env.bat (a smaller file is truncated)
UA = "qai-builder-env-installer/1"
# Below this a transfer counts as stalled: no speed, no ETA. Above MAX_ETA_SECONDS an ETA is not shown.
MIN_ETA_SPEED = 1024.0  # bytes / s
MAX_ETA_SECONDS = 24 * 3600

PYPI_OFFICIAL = "https://pypi.org/simple"
PYPI_CN = "https://pypi.tuna.tsinghua.edu.cn/simple"
PY_STANDALONE_CN = "https://registry.npmmirror.com/-/binary/python-build-standalone/"
TORCH_INDEX_OFFICIAL = "https://download.pytorch.org/whl/cpu"
TORCH_FINDLINKS_CN = "https://mirrors.aliyun.com/pytorch-wheels/cpu/"
SEVENZR_URL = "https://www.7-zip.org/a/7zr.exe"
SEVENZ_EXTRA_URL = "https://www.7-zip.org/a/7z2301-extra.7z"

# Weight of each step in the overall percentage (sum = 100). Rough: deps and the
# SDK download dominate. Only used for the progress bar.
STEP_WEIGHTS: list[tuple[str, int]] = [
    ("python310", 4),
    ("venv", 2),
    ("deps", 40),
    ("sdk_download", 34),
    ("sdk_extract", 14),
    ("config", 1),
    ("verify", 5),
]

# (pip spec, import name, comes from the PyTorch CPU index). Pins copied from
# scripts/setup/setup_qairt_env.py (validated with the pinned QAIRT release).
PACKAGES: list[tuple[str, str, bool]] = [
    ("protobuf", "google.protobuf", False),
    ("numpy==1.26.4", "numpy", False),
    ("ml_dtypes==0.5.1", "ml_dtypes", False),
    ("onnx==1.19.1", "onnx", False),
    ("onnxruntime==1.23.2", "onnxruntime", False),
    ("onnxsim==0.4.36", "onnxsim", False),
    ("pandas", "pandas", False),
    ("torch==2.11.0+cpu", "torch", True),
    ("torchvision==0.26.0+cpu", "torchvision", True),
    ("onnxscript", "onnxscript", False),
    ("tensorflow==2.21.0", "tensorflow", False),
    ("tflite==2.18.0", "tflite", False),
    ("Pillow", "PIL", False),
    ("opencv-python==4.11.0.86", "cv2", False),
    ("scipy", "scipy", False),
    ("matplotlib", "matplotlib", False),
    ("tqdm", "tqdm", False),
    ("pyyaml", "yaml", False),
    ("requests", "requests", False),
]
VERIFY_IMPORTS = ("numpy", "onnx", "onnxruntime", "torch")


class StepError(Exception):
    """A step failed; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─── Reporter (JSON lines) ────────────────────────────────────────────────────


class Reporter:
    """Writes one JSON object per line and tracks the overall percentage."""

    def __init__(self, steps: Sequence[tuple[str, int]], out=None) -> None:
        self.weights = dict(steps)
        self.order = [s for s, _ in steps]
        self.finished: set[str] = set()
        self.current: Optional[str] = None
        self._out = out or sys.stdout
        self._last_progress = 0.0

    def emit(self, **fields) -> None:
        fields["ts"] = round(time.time(), 3)
        self._out.write(json.dumps(fields, ensure_ascii=False) + "\n")
        self._out.flush()

    def overall(self, frac: float = 0.0) -> float:
        total = sum(self.weights.values()) or 1
        done = sum(w for s, w in self.weights.items() if s in self.finished)
        if self.current and self.current not in self.finished:
            done += self.weights.get(self.current, 0) * max(0.0, min(1.0, frac))
        return round(100.0 * done / total, 1)

    def start(self, **info) -> None:
        self.emit(
            event="start",
            steps=[{"id": s, "weight": w} for s, w in self.weights.items()],
            **info,
        )

    def begin(self, step: str, message: str = "") -> None:
        self.current = step
        self.emit(event="step", step=step, state="running", message=message, overall=self.overall())

    def end(self, step: str, *, skipped: bool = False, message: str = "") -> None:
        self.finished.add(step)
        self.emit(
            event="step",
            step=step,
            state="skipped" if skipped else "done",
            message=message,
            overall=self.overall(),
        )

    def progress(
        self,
        step: str,
        done: float,
        total: Optional[float],
        *,
        unit: str = "bytes",
        speed: float = 0.0,
        force: bool = False,
        message: str = "",
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < 0.5:
            return
        self._last_progress = now
        frac = (done / total) if total else 0.0
        eta = int((total - done) / speed) if (total and speed >= MIN_ETA_SPEED and total >= done) else None
        if eta is not None and eta > MAX_ETA_SECONDS:
            eta = None  # "more than a day" is not an estimate; show nothing rather than a nonsense number
        self.emit(
            event="progress",
            step=step,
            unit=unit,
            done=int(done),
            total=int(total) if total else None,
            speed=int(speed),
            eta=eta,
            message=message,
            overall=self.overall(frac),
        )

    def log(self, step: str, line: str) -> None:
        line = line.strip()
        if line:
            self.emit(event="log", step=step, line=line[:300])

    def result(self, ok: bool, code: str = "", message: str = "", step: str = "") -> None:
        self.emit(
            event="result",
            ok=ok,
            code=code,
            message=message,
            step=step,
            overall=100.0 if ok else self.overall(),
        )


# ─── Context ──────────────────────────────────────────────────────────────────


@dataclass
class Ctx:
    root: Path
    arch: str  # "x64" | "arm64"
    envs_dir: Path
    sdk_root: Path
    version: str
    download_url: str
    profile: str = "official"
    proxy: str = ""
    # "auto" | "official" | "cn" as the user asked. Only "auto" may switch source on a failure.
    mirror: str = "auto"
    sdk_zip: Optional[Path] = None
    rep: Reporter = field(default_factory=lambda: Reporter(STEP_WEIGHTS))

    @property
    def venv310(self) -> Path:
        return self.envs_dir / ".venv_x64_310"

    @property
    def venv_python(self) -> Path:
        return self.venv310 / "Scripts" / "python.exe"

    @property
    def marker(self) -> Path:
        return self.sdk_root / SDK_MARKER_REL

    @property
    def config_file(self) -> Path:
        return self.root / "data" / "config" / "qairt_env.json"

    @property
    def dl_dir(self) -> Path:
        return self.root / "data" / "downloads"

    @property
    def uv(self) -> Optional[Path]:
        local = self.root / "data" / "bin" / "uv" / "uv.exe"
        if local.exists():
            return local
        found = shutil.which("uv")
        return Path(found) if found else None

    @property
    def aria2c(self) -> Optional[Path]:
        local = self.root / "data" / "bin" / "aria2c" / "aria2c.exe"
        if local.exists():
            return local
        found = shutil.which("aria2c")
        return Path(found) if found else None


def load_release(root: Path) -> dict:
    with open(root / "scripts" / "qairt_release.json", encoding="utf-8") as fh:
        return json.load(fh)


def detect_arch(root: Path) -> str:
    """--arch flag is handled by the caller; here: data/config/host_arch, then the machine."""
    f = root / "data" / "config" / "host_arch"
    if f.exists():
        v = f.read_text(encoding="utf-8", errors="replace").strip().lower()
        if v in ("x64", "arm64"):
            return v
    m = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine()).lower()
    return "x64" if m in ("amd64", "x86_64") else "arm64"


def default_envs_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "QAIModelBuilder" / "envs"


def build_ctx(args: argparse.Namespace) -> Ctx:
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    release = load_release(root)
    version = args.sdk_version or os.environ.get("QAIRT_VERSION") or release["qairt_version"]
    sdk_root = (
        Path(args.sdk_root)
        if args.sdk_root
        else Path(release["qairt_sdk_root_template"].format(version=version))
    )
    url = (
        args.download_url
        or os.environ.get("QAIRT_DOWNLOAD_URL")
        or release["qairt_download_url_template"].format(version=version)
    )
    return Ctx(
        root=root,
        arch=args.arch or detect_arch(root),
        envs_dir=Path(args.envs_dir) if args.envs_dir else default_envs_dir(),
        sdk_root=sdk_root,
        version=version,
        download_url=url,
        proxy=args.proxy or "",
        mirror=args.mirror or "auto",
        sdk_zip=Path(args.sdk_zip) if args.sdk_zip else None,
    )


# ─── Status (--check) ─────────────────────────────────────────────────────────


def status(ctx: Ctx) -> dict:
    """Is the converter environment usable? JSON first, then verify the disk.

    Must stay equivalent to ``qai.platform.builder_env.detect.detect_builder_env`` (the backend
    uses that one; the two are kept in step by tests/unit/scripts/test_install_builder_env.py).
    ``data\\config\\qairt_env.json`` is what the conversion scripts actually read, so it decides
    WHERE the SDK and the venv are; the disk then decides whether they really exist.
    """
    problems: list[dict] = []
    cfg: dict = {}
    readable = False
    if not ctx.config_file.exists():
        problems.append({"code": "config_missing", "path": str(ctx.config_file)})
    else:
        try:
            loaded = json.loads(ctx.config_file.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                cfg = loaded
                readable = True
            else:
                problems.append({"code": "config_invalid", "path": str(ctx.config_file)})
        except (OSError, ValueError):
            problems.append({"code": "config_invalid", "path": str(ctx.config_file)})

    sdk_root = str(cfg.get("qairt_sdk_root") or "")
    venv = str(cfg.get("python_x64_venv") or "")
    if readable:
        if not sdk_root:
            problems.append({"code": "sdk_not_configured", "key": "qairt_sdk_root"})
        elif not os.path.isabs(sdk_root):
            problems.append({"code": "sdk_path_not_absolute", "path": sdk_root})
        elif not (Path(sdk_root) / SDK_MARKER_REL).is_file():
            problems.append({"code": "sdk_missing", "path": sdk_root})
        if not venv:
            problems.append({"code": "venv_not_configured", "key": "python_x64_venv"})
        elif not os.path.isabs(venv):
            problems.append({"code": "venv_path_not_absolute", "path": venv})
        elif not (Path(venv) / "Scripts" / "python.exe").is_file():
            problems.append({"code": "venv_missing", "path": venv})
    return {
        "ready": not problems,
        "problems": problems,
        "missing": sorted({p["code"].split("_")[0] for p in problems}),
        "venv_310": venv or str(ctx.venv310),
        "sdk_root": sdk_root or str(ctx.sdk_root),
        "sdk_version": ctx.version,
        "config": str(ctx.config_file),
    }


# ─── Network helpers ──────────────────────────────────────────────────────────


def make_opener(proxy: str) -> urllib.request.OpenerDirector:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def probe_ms(url: str, proxy: str, timeout: float = 6.0) -> Optional[int]:
    """Reachability + latency of *url* in ms; None when unreachable."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        make_opener(proxy).open(req, timeout=timeout).close()
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return None
    except (urllib.error.URLError, OSError, socket.timeout):
        return None
    return int((time.monotonic() - t0) * 1000)


def resolve_profile(mirror: str, proxy: str) -> str:
    """official | cn. auto = cn only when official PyPI is unreachable, or >300 ms and >2x slower."""
    if mirror in ("official", "cn"):
        return mirror
    off = probe_ms(PYPI_OFFICIAL + "/pip/", proxy)
    cn = probe_ms(PYPI_CN + "/pip/", proxy)
    if off is None and cn is not None:
        return "cn"
    if off is not None and cn is not None and off > 300 and off > 2 * cn:
        return "cn"
    return "official"


def alt_profile(p: str) -> str:
    return "official" if p == "cn" else "cn"


# Output of a failed uv / pip run that points at the network or at the source (not at our own arguments or the
# dependency graph). Only these are worth retrying or switching source for; anything else fails at once, with
# the real message, instead of being hidden behind pointless retries.
_NETWORK_MARKERS = (
    "403 forbidden", "404 not found", "status client error", "status server error",
    "502 bad gateway", "503 service", "504 gateway",
    "timed out", "timeout", "tcp connect error", "error sending request", "dns error",
    "connection reset", "connection closed", "connection refused", "connection aborted",
    "failed to fetch", "failed to download", "certificate", "temporary failure",
    "max retries exceeded", "newconnectionerror", "readtimeout", "ssl",
)


def is_network_failure(lines: Sequence[str]) -> bool:
    text = " ".join(lines).lower()
    return any(m in text for m in _NETWORK_MARKERS)


def run_with_fallback(
    ctx: Ctx,
    step: str,
    build_cmd: Callable[[str], Sequence],
    *,
    sleep: Optional[Callable[[float], None]] = None,
    same_source_pause: float = 3.0,
) -> tuple[int, list[str]]:
    """Run a uv / pip command; on a NETWORK failure retry, then switch source. Returns (rc, last output lines).

    ``build_cmd(profile)`` returns the argv for that source (it differs per source, e.g. torch).

    1. run it; rc 0 -> done;
    2. the output is not a network failure (resolver conflict, disk full ...) -> stop: retrying cannot help;
    3. network failure -> retry the SAME source once (a single 403 from a CDN edge or a rate limit is usually gone
       seconds later -- seen once with the tuna mirror);
    4. still failing -> the OTHER source, one more try. A source that worked is kept (``ctx.profile``) so one bad
       mirror does not slow every later step.
    A source the user forced with ``--mirror cn|official`` is never switched.
    """
    first = ctx.profile
    pause = sleep if sleep is not None else time.sleep  # looked up at call time, so tests can patch time.sleep
    plan = [first, first]
    if ctx.mirror == "auto":
        plan.append(alt_profile(first))
    rc, tail = 1, []
    for i, prof in enumerate(plan):
        if i == 1:
            ctx.rep.log(step, f"network error ({first}) - retrying the same source once in {same_source_pause:g}s")
            pause(same_source_pause)
        elif i == 2:
            ctx.rep.log(step, f"still failing on the {first} source - switching to {prof}")
        rc, tail = run_stream(ctx, step, build_cmd(prof), tool_env(ctx, prof))
        if rc == 0:
            ctx.profile = prof
            return rc, tail
        if not is_network_failure(tail):
            ctx.rep.log(step, "not a network error - not retrying")
            return rc, tail
    if ctx.mirror != "auto":
        ctx.rep.log(step, f"not switching source ({ctx.mirror} was forced with --mirror)")
    return rc, tail


def tool_env(ctx: Ctx, profile: str) -> dict:
    """Environment for uv / pip child processes."""
    env = dict(os.environ)
    env.setdefault("UV_SYSTEM_CERTS", "1")
    env.setdefault("UV_HTTP_TIMEOUT", "300")
    env.setdefault("UV_HTTP_RETRIES", "5")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONUTF8"] = "1"
    if ctx.proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            env[k] = ctx.proxy
    if profile == "cn":
        env["UV_DEFAULT_INDEX"] = PYPI_CN
        env["PIP_INDEX_URL"] = PYPI_CN
        env["UV_PYTHON_INSTALL_MIRROR"] = PY_STANDALONE_CN
    else:
        env.pop("UV_DEFAULT_INDEX", None)
        env.pop("PIP_INDEX_URL", None)
        env.pop("UV_PYTHON_INSTALL_MIRROR", None)
    return env


# ─── Child processes ──────────────────────────────────────────────────────────

_CHILD: Optional[subprocess.Popen] = None
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_stream(
    ctx: Ctx, step: str, cmd: Sequence[str], env: dict, *, cwd: Optional[Path] = None
) -> tuple[int, list[str]]:
    """Run *cmd*, forward each output line as a ``log`` event, return (rc, last lines)."""
    global _CHILD
    tail: Deque[str] = deque(maxlen=20)
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(cwd) if cwd else None,
        creationflags=_NO_WINDOW,
    )
    _CHILD = proc
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                tail.append(line)
                ctx.rep.log(step, line)
        return proc.wait(), list(tail)
    finally:
        _CHILD = None
        if proc.poll() is None:
            proc.kill()


def kill_child() -> None:
    proc = _CHILD
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=_NO_WINDOW,
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - best effort during shutdown
            proc.kill()


# ─── Downloads ────────────────────────────────────────────────────────────────


class Meter:
    """Speed (EMA) + throttled progress events for one transfer."""

    def __init__(self, ctx: Ctx, step: Optional[str], total: Optional[int]) -> None:
        self.ctx, self.step, self.total = ctx, step, total
        self.speed = 0.0
        self._t = time.monotonic()
        self._n = 0

    def update(self, done: int, *, force: bool = False, speed: Optional[float] = None) -> None:
        now = time.monotonic()
        dt = now - self._t
        if speed is not None:
            # The downloader measured it itself (aria2c's DL: field): use it as is, it is already smoothed.
            self.speed = speed if speed >= MIN_ETA_SPEED else 0.0
            self._t, self._n = now, done
        elif dt >= 1.0 or force:
            inst = (done - self._n) / dt if dt > 0 else 0.0
            self.speed = inst if self.speed == 0 else 0.6 * self.speed + 0.4 * inst
            if self.speed < MIN_ETA_SPEED:
                # The EMA of a stalled transfer decays geometrically (x0.6 per tick) and never reaches 0, so
                # total/speed would blow up (seen: "2947935474527h left"). Below the floor it is simply 0.
                self.speed = 0.0
            self._t, self._n = now, done
        if self.step:
            self.ctx.rep.progress(self.step, done, self.total, speed=self.speed, force=force)


def remote_size(url: str, proxy: str) -> Optional[int]:
    """Content-Length via HEAD, else via a 1-byte ranged GET. None when unknown."""
    op = make_opener(proxy)
    try:
        resp = op.open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=20)
        n = resp.headers.get("Content-Length")
        resp.close()
        if n and n.isdigit() and int(n) > 0:
            return int(n)
    except (urllib.error.URLError, OSError, socket.timeout):
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        resp = op.open(req, timeout=20)
        cr = resp.headers.get("Content-Range", "")
        resp.close()
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1))
    except (urllib.error.URLError, OSError, socket.timeout):
        pass
    return None


def _zip_ok(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


# aria2c prints  [#gid 114688B/31345009B(0%) CN:16 DL:91384B ETA:5m41s]  every --summary-interval seconds when
# --human-readable=false: exact bytes, and its own speed. The first lines are  [#gid 0B/0B CN:1 DL:0B]  (no size yet).
_ARIA_SUMMARY = re.compile(r"\[#\w+ (\d+)B?/(\d+)B?(?:\(\d+%\))? CN:(\d+)(?: DL:(\d+)B?)?")


def _read_aria_progress(log: Path, pos: int) -> tuple[int, Optional[tuple[int, int, float]]]:
    """New bytes of aria2c's stdout log since *pos* -> (new pos, (done, total, speed) of the LAST summary or None)."""
    try:
        with open(log, "rb") as fh:
            fh.seek(pos)
            chunk = fh.read()
    except OSError:
        return pos, None
    # Only complete lines: a summary cut in half at the end of the read is picked up on the next one.
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return pos, None
    text = chunk[: cut + 1].decode("ascii", errors="ignore")
    last = None
    for m in _ARIA_SUMMARY.finditer(text):
        last = (int(m.group(1)), int(m.group(2)), float(m.group(4) or 0))
    return pos + cut + 1, last


def _download_aria2c(ctx: Ctx, exe: Path, url: str, dest: Path, meter: Meter, stall: int) -> bool:
    """One aria2c attempt. True when it exited 0.

    Progress comes from aria2c's own summary lines, NOT from the size of *dest*: with 16 connections the file is
    written sparsely (a late segment landing first makes it jump to ~full size), so its length says nothing about
    how much was really downloaded and a speed derived from it spikes to hundreds of MB/s at the start."""
    cmd = [
        str(exe), "-c", "-x16", "-s16", "-k1M", "--file-allocation=none",
        "--max-tries=5", "--retry-wait=3", "--connect-timeout=15", "--timeout=30",
        "--summary-interval=1", "--human-readable=false", "--console-log-level=error",
        "--download-result=hide", "--allow-overwrite=true", "--auto-file-renaming=false",
        "-d", str(dest.parent), "-o", dest.name,
    ]
    if ctx.proxy:
        cmd += ["--all-proxy", ctx.proxy]
    cmd.append(url)
    log = dest.parent / (dest.name + ".aria2c.log")
    global _CHILD
    with open(log, "wb") as out:  # a file, not a pipe: nothing to drain, aria2c can never block on a full pipe
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    _CHILD = proc
    pos, last_done, last_growth = 0, -1, time.monotonic()
    try:
        while proc.poll() is None:
            time.sleep(1.0)
            pos, prog = _read_aria_progress(log, pos)
            if prog is not None:
                done, total, speed = prog
                if total and not meter.total:
                    meter.total = total
                if done != last_done:
                    last_done, last_growth = done, time.monotonic()
                meter.update(done, speed=speed)
            if time.monotonic() - last_growth > stall:
                ctx.rep.log("download", f"no bytes for {stall}s - restarting aria2c")
                kill_child()
                proc.wait()
                return False
        return proc.returncode == 0
    finally:
        _CHILD = None
        if proc.poll() is None:
            kill_child()
        try:
            log.unlink()
        except OSError:
            pass


def _download_urllib(ctx: Ctx, url: str, dest: Path, meter: Meter) -> None:
    have = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        resp = make_opener(ctx.proxy).open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # nothing left to fetch
            return
        raise
    with resp:
        if have and resp.status == 200:  # server ignored Range -> start over
            have = 0
        with open(dest, "ab" if have else "wb") as fh:
            done = have
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                meter.update(done)


def download(
    ctx: Ctx,
    url: str,
    dest: Path,
    *,
    min_size: int = 0,
    zip_test: bool = False,
    step: Optional[str] = None,
    total: Optional[int] = None,
    stall: int = 90,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Resumable download with aria2c (if present) or urllib. Raises StepError('network')."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= max(min_size, 1) and (not zip_test or _zip_ok(dest)):
        return
    if total is None:
        total = remote_size(url, ctx.proxy)
    meter = Meter(ctx, step, total)
    aria = ctx.aria2c
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            if aria is not None:
                ok = _download_aria2c(ctx, aria, url, dest, meter, stall)
                if not ok:
                    last_err = "aria2c failed or stalled"
            else:
                _download_urllib(ctx, url, dest, meter)
                ok = True
            size = dest.stat().st_size if dest.exists() else 0
            if ok and size >= max(min_size, 1) and (not total or size >= total) and (not zip_test or _zip_ok(dest)):
                meter.update(size, force=True)
                return
            if ok:
                last_err = f"incomplete or corrupt file ({size} bytes)"
                if zip_test and size >= (total or 0) > 0:
                    dest.unlink(missing_ok=True)  # full size but not a zip: a resume cannot fix it
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        ctx.rep.log("download", f"attempt {attempt}/{attempts} failed: {last_err}")
        if attempt < attempts:
            sleep(min(2 * 2 ** (attempt - 1), 40))
    raise StepError("network", f"download failed after {attempts} attempts ({last_err}): {url}")


# ─── Disk space ───────────────────────────────────────────────────────────────


def free_bytes(path: Path) -> int:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def check_disk(ctx: Ctx, need_venv: bool, need_sdk: bool, zip_total: Optional[int]) -> None:
    """Fail early with 'disk_space'. The figures are ESTIMATES (venv ~2.4 GB measured by the
    installer README; SDK = zip kept + extracted copy, assumed ~2x the zip), not guarantees."""
    gib = 1 << 30
    zip_size = zip_total or 2 * gib
    for path, need, what in (
        (ctx.envs_dir, int(2.6 * gib) if need_venv else 0, "converter venv"),
        (ctx.sdk_root.parent, int(2.2 * zip_size) + gib // 2 if need_sdk else 0, "QAIRT SDK"),
    ):
        if need and free_bytes(path) < need:
            raise StepError(
                "disk_space",
                f"not enough free space for the {what} on {path.anchor or path}: "
                f"need about {need / gib:.1f} GiB, have {free_bytes(path) / gib:.1f} GiB",
            )


# ─── Steps ────────────────────────────────────────────────────────────────────


def need_uv(ctx: Ctx) -> Path:
    uv = ctx.uv
    if uv is None:
        raise StepError("uv_missing", "uv.exe not found (expected data\\bin\\uv\\uv.exe or on PATH)")
    return uv


def step_python310(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("python310", "x86_64 Python 3.10")
    if ctx.venv_python.exists():
        rep.end("python310", skipped=True, message="venv already exists")
        return
    uv = need_uv(ctx)
    env = tool_env(ctx, ctx.profile)
    rc, _ = run_stream(ctx, "python310", [uv, "python", "find", "--no-project", PY310_SPEC], env)
    if rc == 0:
        rep.end("python310", skipped=True, message="interpreter already installed")
        return
    rc, tail = run_with_fallback(
        ctx, "python310",
        lambda prof: [uv, "python", "install", "--no-bin", "--no-registry", PY310_SPEC],
    )
    if rc != 0:
        raise StepError("python_install", "uv could not install " + PY310_SPEC + ": " + " | ".join(tail[-5:]))
    rep.end("python310")


def step_venv(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("venv", str(ctx.venv310))
    pip_ok = False
    if ctx.venv_python.exists() and (ctx.venv310 / "Scripts" / "activate.bat").exists():
        rc, _ = run_stream(ctx, "venv", [ctx.venv_python, "-m", "pip", "--version"], os.environ.copy())
        pip_ok = rc == 0
    if pip_ok:
        rep.end("venv", skipped=True)
        return
    uv = need_uv(ctx)
    ctx.envs_dir.mkdir(parents=True, exist_ok=True)
    # --no-project: the repo's pyproject requires 3.13; this venv is deliberately 3.10.
    # --allow-existing: a first attempt that failed half-way (e.g. the pip seed download got a 403) leaves a
    # partial venv behind; without it the retry would die on "a virtual environment already exists".
    rc, tail = run_with_fallback(
        ctx, "venv",
        lambda prof: [uv, "venv", ctx.venv310, "--python", PY310_SPEC, "--seed", "--allow-existing", "--no-project"],
    )
    if rc != 0 or not (ctx.venv310 / "Scripts" / "activate.bat").exists():
        raise StepError("venv", "could not create the 3.10 venv: " + " | ".join(tail[-5:]))
    helper = ctx.root / "scripts" / "setup" / "install_pycache_pth.py"
    if helper.exists():  # best effort: redirect __pycache__ out of the source tree
        run_stream(ctx, "venv", [ctx.venv_python, helper, "--venv", ctx.venv310, "--repo-root", ctx.root],
                   os.environ.copy())
    rep.end("venv")


def missing_packages(ctx: Ctx) -> list[tuple[str, bool]]:
    """Specs whose import name cannot be found in the venv (find_spec: no heavy imports)."""
    names = [imp for _, imp, _ in PACKAGES]
    code = (
        "import importlib.util as u, json, sys\n"
        "out = {}\n"
        "for n in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        out[n] = u.find_spec(n) is not None\n"
        "    except Exception:\n"
        "        out[n] = False\n"
        "print(json.dumps(out))\n"
    )
    try:
        res = subprocess.run(
            [str(ctx.venv_python), "-c", code, json.dumps(names)],
            capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
        )
        have = json.loads(res.stdout.strip().splitlines()[-1]) if res.returncode == 0 else {}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        have = {}
    return [(spec, torch) for spec, imp, torch in PACKAGES if not have.get(imp)]


def _pip_torch_cmd(ctx: Ctx, specs: list[str], source: str) -> list:
    cmd: list = [ctx.venv_python, "-m", "pip", "install", "--progress-bar", "off", *specs]
    if source == "cn":
        cmd += ["--find-links", TORCH_FINDLINKS_CN]
    else:
        cmd += ["--extra-index-url", TORCH_INDEX_OFFICIAL]
    return cmd


def _pip_torch(ctx: Ctx, specs: list[str], env: dict, source: str) -> tuple[int, list[str]]:
    return run_stream(ctx, "deps", _pip_torch_cmd(ctx, specs, source), env)


def step_deps(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("deps", "converter packages")
    todo = missing_packages(ctx)
    if not todo:
        rep.end("deps", skipped=True, message="all packages present")
        return
    uv = need_uv(ctx)
    normal = [s for s, torch in todo if not torch]
    torch_specs = [s for s, torch in todo if torch]
    phases = (1 if normal else 0) + (1 if torch_specs else 0)
    done_phases = 0

    if normal:
        rep.progress("deps", done_phases, phases, unit="phase", force=True, message=f"{len(normal)} packages")
        rc, last = run_with_fallback(
            ctx, "deps", lambda prof: [uv, "pip", "install", "--python", ctx.venv_python, *normal]
        )
        if rc != 0:
            raise StepError("deps", "package install failed: " + " | ".join(last[-5:]))
        done_phases += 1

    if torch_specs:
        rep.progress("deps", done_phases, phases, unit="phase", force=True, message="torch (CPU)")
        # torch's index is chosen by the SOURCE: the aliyun mirror for cn (verified to list torch 2.11.0+cpu /
        # torchvision 0.26.0+cpu cp310 win_amd64), the PyTorch CPU index for official. The shared fallback
        # retries the same one once, then (only when --mirror was not forced) tries the other.
        rc, last = run_with_fallback(
            ctx, "deps", lambda prof: _pip_torch_cmd(ctx, torch_specs, "cn" if prof == "cn" else "official")
        )
        if rc != 0 and ctx.mirror == "auto" and not is_network_failure(last):
            # Not a network error but the chosen index simply lacks that exact version (pip: "No matching
            # distribution" / "Could not find a version"): the OTHER index may have it.
            other = alt_profile(ctx.profile)
            ctx.rep.log("deps", f"torch not found on the {ctx.profile} index - trying {other}")
            rc, last = _pip_torch(ctx, torch_specs, tool_env(ctx, other), "cn" if other == "cn" else "official")
            if rc == 0:
                ctx.profile = other
        if rc != 0:
            raise StepError("deps", "torch install failed: " + " | ".join(last[-5:]))
        done_phases += 1
    rep.progress("deps", phases, phases, unit="phase", force=True)
    rep.end("deps")


# ── SDK ──


def find_extractor(ctx: Ctx) -> Optional[str]:
    """A 7-Zip already on the machine (PATH, Program Files, or our own data\\bin\\7zr\\7za.exe)."""
    for name in ("7z", "7za"):
        p = shutil.which(name)
        if p:
            return p
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            cand = Path(base) / "7-Zip" / "7z.exe"
            if cand.exists():
                return str(cand)
    own = ctx.root / "data" / "bin" / "7zr" / "7za.exe"
    return str(own) if own.exists() else None


def ensure_7za(ctx: Ctx) -> Optional[str]:
    """Download 7zr.exe + 7-Zip Extra and unpack 7za.exe. Only called when nothing better exists.
    Non-fatal: returns None and the caller falls back to Python's zipfile."""
    bindir = ctx.root / "data" / "bin" / "7zr"
    try:
        bindir.mkdir(parents=True, exist_ok=True)
        sevenzr = bindir / "7zr.exe"
        extra = ctx.dl_dir / "_7za_extra.7z"
        download(ctx, SEVENZR_URL, sevenzr, min_size=100_000, attempts=3)
        download(ctx, SEVENZ_EXTRA_URL, extra, min_size=300_000, attempts=3)
        tmp = ctx.dl_dir / "_7za_extra_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        rc, _ = run_stream(ctx, "sdk_extract", [sevenzr, "x", "-y", f"-o{tmp}", extra, "-bso0", "-bsp0"],
                           os.environ.copy())
        got = tmp / "7za.exe"
        if rc == 0 and got.exists():
            shutil.copy2(got, bindir / "7za.exe")
        shutil.rmtree(tmp, ignore_errors=True)
        extra.unlink(missing_ok=True)
        return str(bindir / "7za.exe") if (bindir / "7za.exe").exists() else None
    except (StepError, OSError) as exc:
        ctx.rep.log("sdk_extract", f"7-Zip unavailable ({exc}); using Python zipfile")
        return None


_PCT = re.compile(rb"(\d{1,3})%")


def extract_with_7z(ctx: Ctx, exe: str, zip_path: Path, out: Path) -> bool:
    global _CHILD
    proc = subprocess.Popen(
        [exe, "x", "-y", f"-o{out}", str(zip_path), "-bso0", "-bsp1", "-bse2"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    _CHILD = proc
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            m = _PCT.findall(chunk)
            if m:
                ctx.rep.progress("sdk_extract", int(m[-1]), 100, unit="percent")
        return proc.wait() == 0
    finally:
        _CHILD = None
        if proc.poll() is None:
            kill_child()


def extract_with_zipfile(ctx: Ctx, zip_path: Path, out: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            total = sum(i.file_size for i in infos) or 1
            done = 0
            for info in infos:
                zf.extract(info, out)
                done += info.file_size
                ctx.rep.progress("sdk_extract", done, total)
        return True
    except (zipfile.BadZipFile, OSError):
        return False


def find_sdk_dir(tmp: Path, version: str) -> Optional[Path]:
    """The folder named <version> within three levels of *tmp* (zip layout: qairt/<version>)."""
    for pattern in (version, f"*/{version}", f"*/*/{version}"):
        for p in tmp.glob(pattern):
            if p.is_dir():
                return p
    return None


def write_owner_marker(ctx: Ctx) -> None:
    """Mark ctx.sdk_root as extracted by us (best effort). See OWNER_MARKER_NAME."""
    try:
        (ctx.sdk_root / OWNER_MARKER_NAME).write_text(
            json.dumps({"installer": "install_builder_env.py", "version": ctx.version, "app_root": str(ctx.root)}),
            encoding="utf-8",
        )
    except OSError as exc:
        ctx.rep.log("sdk_extract", f"could not write {OWNER_MARKER_NAME}: {exc}")


def sdk_owned_by_us(ctx: Ctx) -> bool:
    return (ctx.sdk_root / OWNER_MARKER_NAME).is_file()


def backup_launchers(ctx: Ctx) -> int:
    """Copy the extension-less launcher scripts + the generator exe to data\\sdk\\qairt-scripts
    (the contextbin self-heal reads them). Best effort."""
    n = 0
    dest_root = ctx.root / "data" / "sdk" / "qairt-scripts"
    for arch in ("aarch64-windows-msvc", "x86_64-windows-msvc"):
        src = ctx.sdk_root / "bin" / arch
        if not src.is_dir():
            continue
        out = dest_root / arch
        try:
            out.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file() and f.stat().st_size > 0 and (f.suffix == "" or f.name == "qnn-context-binary-generator.exe"):
                    t = out / f.name
                    if not t.exists():
                        shutil.copy2(f, t)
                        n += 1
        except OSError:
            continue
    return n


def pick_sdk_zip(ctx: Ctx) -> tuple[Optional[Path], str]:
    """(zip, owner) - owner: 'user' (never deleted), 'kept' (earlier run), '' when none."""
    if ctx.sdk_zip is not None:
        if not ctx.sdk_zip.is_file():
            raise StepError("sdk_zip_missing", f"--sdk-zip not found: {ctx.sdk_zip}")
        return ctx.sdk_zip, "user"
    vendor = ctx.root / "vendor" / "qairt" / f"v{ctx.version}.zip"
    if vendor.is_file():
        return vendor, "user"
    kept = ctx.root / "data" / "sdk" / "qairt" / f"v{ctx.version}.zip"
    if kept.is_file():
        return kept, "kept"
    return None, ""


def step_sdk(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("sdk_download", f"QAIRT {ctx.version}")
    if ctx.marker.exists():
        rep.end("sdk_download", skipped=True, message="SDK already installed")
        rep.begin("sdk_extract")
        backup_launchers(ctx)
        rep.end("sdk_extract", skipped=True)
        return

    zip_path, owner = pick_sdk_zip(ctx)
    if zip_path is None:
        tmp_zip = ctx.dl_dir / "_qairt_tmp.zip"
        total = remote_size(ctx.download_url, ctx.proxy)
        check_disk(ctx, need_venv=False, need_sdk=True, zip_total=total)
        download(ctx, ctx.download_url, tmp_zip, min_size=SDK_MIN_ZIP_BYTES, zip_test=True,
                 step="sdk_download", total=total, attempts=5)
        zip_path, owner = tmp_zip, "downloaded"
        rep.end("sdk_download")
    else:
        rep.end("sdk_download", skipped=True, message=f"using {zip_path}")

    rep.begin("sdk_extract", "unpacking")
    parent = ctx.sdk_root.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise StepError("permission", f"cannot create {parent}: {exc} (run as administrator or pass --sdk-root)")
    tmp = parent / "_extract_tmp"
    shutil.rmtree(tmp, ignore_errors=True)

    ok = False
    exe = find_extractor(ctx) or ensure_7za(ctx)
    if exe:
        rep.log("sdk_extract", f"extracting with {exe}")
        ok = extract_with_7z(ctx, exe, zip_path, tmp)
        if not ok:
            shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        rep.log("sdk_extract", "extracting with Python zipfile (slower)")
        ok = extract_with_zipfile(ctx, zip_path, tmp)

    src = find_sdk_dir(tmp, ctx.version) if ok else None
    if src is None:
        shutil.rmtree(tmp, ignore_errors=True)
        if owner in ("kept", "downloaded"):
            zip_path.unlink(missing_ok=True)  # damaged copy: the next run fetches a clean one
        raise StepError("extract", f"could not unpack {zip_path.name} (expected a '{ctx.version}' folder inside)")
    if ctx.sdk_root.exists():
        shutil.rmtree(ctx.sdk_root, ignore_errors=True)
    shutil.move(str(src), str(ctx.sdk_root))
    shutil.rmtree(tmp, ignore_errors=True)
    if not ctx.marker.exists():
        raise StepError("extract", f"SDK unpacked but {SDK_MARKER_REL} is missing")
    write_owner_marker(ctx)

    if owner == "downloaded":  # keep the zip so single SDK files can be repaired later
        kept = ctx.root / "data" / "sdk" / "qairt" / f"v{ctx.version}.zip"
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            if kept.exists():
                zip_path.unlink(missing_ok=True)
            else:
                shutil.move(str(zip_path), str(kept))
        except OSError:
            pass
    backup_launchers(ctx)
    rep.end("sdk_extract")


def step_config(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("config", str(ctx.config_file))
    if ctx.arch == "arm64":
        runtime_venv, runtime_subdir = ctx.envs_dir / ".venv_arm64_313", "arm64x-windows-msvc"
    else:
        runtime_venv, runtime_subdir = ctx.envs_dir / ".venv_x64_313", "x86_64-windows-msvc"
    cfg: dict = {}
    if ctx.config_file.exists():  # keep keys other tools added
        try:
            cfg = json.loads(ctx.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg.update({
        "_comment": "Auto-generated by install_builder_env.py. Do not edit manually.",
        "_version": ctx.sdk_root.name,
        "qairt_sdk_root": str(ctx.sdk_root),
        "qairt_download_url": ctx.download_url,
        "python_x64_venv": str(ctx.venv310),
        "python_arm64_venv": str(ctx.envs_dir / ".venv_arm64_313"),
        "python_runtime_venv": str(runtime_venv),
        "qairt_runtime_subdir": runtime_subdir,
        "vs_vcvarsall": "",
        "vc_targets_path": "",
        "vs_cmake_path": "",
        "platform": "windows",
        "arch": "aarch64" if ctx.arch == "arm64" else "x86_64",
        # true only when THIS script extracted qairt_sdk_root (owner marker present). The uninstaller reads it.
        "qairt_sdk_installed_by_app": sdk_owned_by_us(ctx),
    })
    ctx.config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = ctx.config_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ctx.config_file)
    rep.end("config")


def step_verify(ctx: Ctx) -> None:
    rep = ctx.rep
    rep.begin("verify")
    problems: list[str] = []
    code = (
        "import platform, importlib, json, sys\n"
        "r = {'machine': platform.machine(), 'bad': []}\n"
        "for n in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception as e:\n"
        "        r['bad'].append(n + ': ' + type(e).__name__ + ': ' + str(e)[:120])\n"
        "print(json.dumps(r))\n"
    )
    try:
        res = subprocess.run(
            [str(ctx.venv_python), "-c", code, json.dumps(list(VERIFY_IMPORTS))],
            capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW,
        )
        info = json.loads(res.stdout.strip().splitlines()[-1])
        if info["machine"].lower() not in ("amd64", "x86_64"):
            problems.append(f"venv is {info['machine']}, expected x86_64")
        problems += info["bad"]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        problems.append(f"venv python did not run: {exc}")
    if not ctx.marker.exists():
        problems.append(f"missing {SDK_MARKER_REL}")
    if not ctx.config_file.exists():
        problems.append("qairt_env.json missing")
    if problems:
        raise StepError("verify", "; ".join(problems))
    rep.end("verify")


# ─── Single instance ──────────────────────────────────────────────────────────


class SingleInstance:
    """OS-level lock held for the life of the process (released if it is killed)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True


# ─── Entry point ──────────────────────────────────────────────────────────────


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="install_builder_env", description=__doc__.split("\n")[0])
    p.add_argument("--root", help="app root (default: two levels above this script)")
    p.add_argument("--arch", choices=("x64", "arm64"), help="host arch (default: data/config/host_arch, then detect)")
    p.add_argument("--envs-dir", help=r"venv directory (default: %%LOCALAPPDATA%%\QAIModelBuilder\envs)")
    p.add_argument("--sdk-root", help="QAIRT install dir (default from scripts/qairt_release.json)")
    p.add_argument("--sdk-version", help="override the pinned QAIRT version")
    p.add_argument("--download-url", help="override the SDK download URL")
    p.add_argument("--sdk-zip", help="use this local SDK zip instead of downloading")
    p.add_argument("--mirror", choices=("auto", "official", "cn"), default="auto")
    p.add_argument("--proxy", help="HTTP(S) proxy URL")
    p.add_argument("--check", action="store_true", help="print the install status as one JSON line and exit")
    return p.parse_args(argv)


def _on_signal(signum, _frame) -> None:  # noqa: ANN001
    raise KeyboardInterrupt


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        ctx = build_ctx(args)
    except (OSError, KeyError, ValueError) as exc:
        Reporter(STEP_WEIGHTS).result(False, "bad_arguments", str(exc))
        return 2
    rep = ctx.rep

    if args.check:
        print(json.dumps(status(ctx), ensure_ascii=False))
        return 0
    if sys.platform != "win32":
        rep.result(False, "unsupported_os", "the QAIRT SDK for Windows can only be installed on Windows")
        return 2

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_signal)

    lock = SingleInstance(ctx.dl_dir / "builder_env.lock")
    if not lock.acquire():
        rep.result(False, "already_running", "another install_builder_env is already running")
        return 4

    current = ""
    try:
        ctx.profile = resolve_profile(args.mirror, ctx.proxy)
        rep.start(version=ctx.version, arch=ctx.arch, profile=ctx.profile, sdk_root=str(ctx.sdk_root),
                  venv=str(ctx.venv310))
        check_disk(ctx, need_venv=not ctx.venv_python.exists(), need_sdk=False, zip_total=None)
        for current, fn in (
            ("python310", step_python310),
            ("venv", step_venv),
            ("deps", step_deps),
            ("sdk", step_sdk),
            ("config", step_config),
            ("verify", step_verify),
        ):
            fn(ctx)
        rep.result(True, message="model-conversion environment ready")
        return 0
    except StepError as exc:
        rep.result(False, exc.code, exc.message, step=rep.current or current)
        return 1
    except KeyboardInterrupt:
        kill_child()
        rep.result(False, "cancelled", "cancelled", step=rep.current or current)
        return 3
    except Exception as exc:  # noqa: BLE001 - last resort: report, never a bare traceback
        rep.result(False, "internal", f"{type(exc).__name__}: {exc}", step=rep.current or current)
        rep.log("internal", traceback.format_exc()[-1500:])
        return 1
    finally:
        kill_child()


if __name__ == "__main__":
    sys.exit(main())
