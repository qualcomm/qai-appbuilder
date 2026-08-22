# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aria2c daemon lifecycle + auto-install + JSON-RPC client (infrastructure).

Ports the process / RPC / auto-install machinery of V1
``backend/aria2c_downloader.py`` into a single-responsibility infrastructure
object. Unlike V1 (which folded daemon management *and* the download stream
into one ``Aria2cDownloader`` class), this object owns ONLY:

* binary discovery (PATH + ``bin_dir`` + ``bin_dir/aria2c/`` sub-dir),
* lazy auto-install of the Windows ARM64 / AMD64 build into
  ``bin_dir/aria2c/aria2c.exe`` (V1 ``_auto_install_aria2c``),
* RPC daemon start / stop on port 6800 (``--enable-rpc``),
* thin JSON-RPC helpers (``addUri`` / ``tellStatus`` / ``tellActive`` /
  ``remove`` / ``shutdown`` / ``getVersion``),
* a status snapshot (``available`` / ``can_auto_install`` / ``daemon_running``
  / ``daemon_pid`` / ``install_status`` / ``install_error`` …).

The *download stream* (poll → DownloadProgress) lives in the download engine
(:class:`HttpxDownloadEngine`), which calls this object — keeping the V2
hexagon clean (engine = streaming concern; daemon = process concern).

V1 source-of-truth: ``backend/aria2c_downloader.py`` (whole file).
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

from qai.service_release.domain.value_objects import Aria2cInstallStatus

logger = logging.getLogger("qai.service_release.aria2c")

# ── Constants (V1 aria2c_downloader.py:54-67) ──────────────────────────────

RPC_PORT = 6800
_RPC_URL = f"http://127.0.0.1:{RPC_PORT}/jsonrpc"
_CONNECTIONS = 16  # -x / -s
_CHUNK_SIZE = "1M"  # -k / min-split-size
_START_TIMEOUT = 10  # seconds to wait for RPC readiness
POLL_INTERVAL = 0.5  # progress poll interval (seconds)

# Windows-only auto-install URLs. ARM64 = minnyres build; AMD64 = official.
_INSTALL_URLS: dict[str, str] = {
    "ARM64": (
        "https://github.com/minnyres/aria2-windows-arm64/releases/download/"
        "v1.37.0/aria2_1.37.0_arm64.zip"
    ),
    "AMD64": (
        "https://github.com/aria2/aria2/releases/download/release-1.37.0/"
        "aria2-1.37.0-win-64bit-build1.zip"
    ),
}


def _machine_arch() -> str:
    """Normalised machine arch: ``ARM64`` or ``AMD64`` (V1 :72-77)."""
    machine = platform.machine().upper()
    if machine in ("ARM64", "AARCH64"):
        return "ARM64"
    return "AMD64"


def _find_aria2c(extra_dirs: list[str] | None = None) -> str | None:
    """Locate aria2c (PATH → extra_dirs → extra_dirs/aria2c/ sub-dir).

    V1 ``_find_aria2c`` (:156-181).
    """
    found = shutil.which("aria2c")
    if found:
        return found
    for d in extra_dirs or []:
        for name in ("aria2c.exe", "aria2c"):
            candidate = Path(d) / name
            if candidate.is_file():
                return str(candidate)
            candidate_sub = Path(d) / "aria2c" / name
            if candidate_sub.is_file():
                return str(candidate_sub)
    return None


def _auto_install_aria2c(bin_dir: Path, proxy: str | None = None) -> str | None:
    """Download + unzip the arch-specific aria2c.exe into ``bin_dir/aria2c/``.

    Windows only. Returns the exe path on success, ``None`` on failure.
    V1 ``_auto_install_aria2c`` (:80-153).

    缺口 9 — the aria2c binary download is a "file download" class request, so
    it routes through the mechanism-B global proxy when *proxy* is supplied
    (an ``http(s)://[user:pass@]host:port`` URL, already carrying any embedded
    auth). ``None`` / empty → direct connection (proxy never forced;
    State-Truth-First).
    """
    if sys.platform != "win32":
        logger.info("aria2c auto-install is Windows-only (platform=%s)", sys.platform)
        return None

    arch = _machine_arch()
    url = _INSTALL_URLS.get(arch)
    if not url:
        logger.warning("no aria2c install URL for arch %s", arch)
        return None

    install_dir = bin_dir / "aria2c"
    install_dir.mkdir(parents=True, exist_ok=True)
    zip_path = install_dir / "aria2c_install.zip"
    exe_dest = install_dir / "aria2c.exe"

    logger.info("auto-installing aria2c (%s): %s -> %s", arch, url, install_dir)

    # Download zip (lenient SSL — enterprise networks; V1 parity).
    try:
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # 缺口 9 — route through the global proxy when configured. urllib uses
        # a ProxyHandler; an empty / None proxy yields a direct-connection
        # opener (no proxy forced).
        proxy_clean = (proxy or "").strip()
        if proxy_clean:
            handlers: list[urllib.request.BaseHandler] = [
                urllib.request.ProxyHandler(
                    {"http": proxy_clean, "https": proxy_clean}
                ),
                urllib.request.HTTPSHandler(context=ctx),
            ]
        else:
            handlers = [urllib.request.HTTPSHandler(context=ctx)]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            url, headers={"User-Agent": "QAIModelBuilder/1.0"}
        )
        with opener.open(req, timeout=120) as resp:
            zip_path.write_bytes(resp.read())
        logger.info("aria2c install zip downloaded: %s", zip_path)
    except Exception as exc:  # noqa: BLE001 — V1 swallows + logs, returns None
        logger.error("aria2c download failed: %s", exc)
        zip_path.unlink(missing_ok=True)
        return None

    # Extract aria2c.exe (may be nested).
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            exe_entries = [
                n for n in zf.namelist() if n.lower().endswith("aria2c.exe")
            ]
            if not exe_entries:
                logger.error(
                    "aria2c.exe not found in zip; entries=%s", zf.namelist()[:10]
                )
                return None
            exe_entry = min(exe_entries, key=lambda x: x.count("/"))
            with zf.open(exe_entry) as src, open(str(exe_dest), "wb") as dst:
                dst.write(src.read())
        logger.info("aria2c.exe extracted: %s", exe_dest)
    except Exception as exc:  # noqa: BLE001
        logger.error("aria2c extraction failed: %s", exc)
        return None
    finally:
        zip_path.unlink(missing_ok=True)

    if exe_dest.is_file():
        logger.info("aria2c auto-install succeeded: %s", exe_dest)
        return str(exe_dest)
    logger.error("aria2c exe missing after install: %s", exe_dest)
    return None


def _rpc_call_sync(method: str, params: list | None = None) -> dict:
    """Synchronous JSON-RPC call (daemon health checks / shutdown)."""
    import urllib.request

    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "qai", "method": method, "params": params or []}
    ).encode()
    req = urllib.request.Request(
        _RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _assign_kill_on_close_job(pid: int) -> object | None:
    """Assign ``pid`` to a Windows Job Object with ``KILL_ON_JOB_CLOSE``.

    M-9 (铁律5) — guarantees the aria2c child is reaped if the API process
    dies abruptly (task-manager kill / crash): when the Job handle closes
    (because our process exited), Windows terminates every process in the
    job. Returns the opened Job handle (the CALLER must keep a strong
    reference so the handle stays open for the lifetime of our process);
    returns ``None`` on non-Windows or any failure (graceful — atexit still
    provides a softer fallback). Platform-guarded for the Linux-CI posture.
    """
    if sys.platform != "win32":  # POSIX: no Job Object concept
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        hproc = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid)
        )
        if not hproc:
            kernel32.CloseHandle(job)
            return None
        try:
            if not kernel32.AssignProcessToJobObject(job, hproc):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(hproc)
        return job
    except Exception:  # noqa: BLE001 - best-effort; atexit still backstops
        logger.debug("aria2c job-object assignment failed", exc_info=True)
        return None


class Aria2cDaemon:
    """aria2c binary readiness + RPC daemon lifecycle (single instance).

    Lazily auto-installs the binary on first use (Windows), starts one
    long-lived RPC daemon shared by all downloads, and exposes async RPC
    helpers + a status snapshot. The download engine drives the per-task
    poll loop using :meth:`rpc` against this daemon.
    """

    __slots__ = (
        "_bin_dir",
        "_exe",
        "_proc",
        "_lock",
        "_install_lock",
        "_install_status",
        "_install_error",
        "_installer",
        "_finder",
        "_proxy_provider",
        "_job",
        "_atexit_registered",
    )

    def __init__(
        self,
        *,
        bin_dir: Path,
        # Injectable for tests (default = real implementations).
        finder=_find_aria2c,
        installer=_auto_install_aria2c,
        proxy_provider=None,
    ) -> None:
        self._bin_dir = bin_dir
        self._finder = finder
        self._installer = installer
        # 缺口 9 — optional zero-arg callable returning the live mechanism-B
        # global-proxy URL (or None) so the binary auto-install download can
        # route through the configured proxy. Read at install time so a
        # runtime-config edit hot-applies; None / empty → direct connection
        # (proxy never forced; State-Truth-First). Injected by the apps DI
        # layer so this infra object stays import-isolated from settings.
        self._proxy_provider = proxy_provider
        self._exe: str | None = finder([str(bin_dir)])
        self._proc: subprocess.Popen | None = None
        self._lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()
        self._install_status = Aria2cInstallStatus.IDLE
        self._install_error = ""
        # M-9 — strong ref to the Windows Job Object (KILL_ON_JOB_CLOSE) the
        # daemon is assigned to; kept alive for our process lifetime so the
        # OS reaps the aria2c child if we die abruptly. ``None`` until spawn /
        # on non-Windows.
        self._job: object | None = None
        self._atexit_registered = False

    # ── Availability snapshot ──────────────────────────────────────────────

    @property
    def exe_path(self) -> str:
        return self._exe or ""

    @property
    def bin_dir(self) -> Path:
        return self._bin_dir

    @property
    def install_status(self) -> Aria2cInstallStatus:
        return self._install_status

    @property
    def install_error(self) -> str:
        return self._install_error

    @property
    def available(self) -> bool:
        """aria2c usable now or auto-installable (V1 ``available`` :246-259)."""
        if self._exe is not None:
            return True
        if (
            sys.platform == "win32"
            and self._bin_dir is not None
            and self._install_status != Aria2cInstallStatus.FAILED
        ):
            return True
        return False

    @property
    def can_auto_install(self) -> bool:
        """Not yet installed but installable (V1 ``can_auto_install`` :687-691)."""
        return (
            self._exe is None
            and sys.platform == "win32"
            and self._bin_dir is not None
            and self._install_status
            not in (Aria2cInstallStatus.DONE, Aria2cInstallStatus.FAILED)
        )

    def is_rpc_alive(self) -> bool:
        """Check whether the RPC daemon answers (V1 ``_is_rpc_alive`` :316-322)."""
        try:
            result = _rpc_call_sync("aria2.getVersion")
            return "result" in result
        except Exception:  # noqa: BLE001 — any error means "not alive"
            return False

    def daemon_pid(self) -> int | None:
        if self._proc and self._proc.poll() is None:
            return self._proc.pid
        return None

    # ── Auto-install ───────────────────────────────────────────────────────

    def _install_with_proxy(self, proxy: str | None) -> str | None:
        """Invoke the installer, gracefully passing ``proxy`` when supported.

        Real installer (``_auto_install_aria2c``) accepts an optional ``proxy``
        kwarg; older / test-injected installers may only accept ``bin_dir``.
        We try the proxy-aware shape first and fall back to the single-arg
        shape so existing tests / hand-rolled stubs keep working unchanged.
        """
        try:
            return self._installer(self._bin_dir, proxy=proxy)
        except TypeError:
            # Legacy installer signature — no proxy support, call as before.
            return self._installer(self._bin_dir)

    async def ensure_binary(self) -> bool:
        """Ensure aria2c.exe exists, auto-installing once if needed.

        Returns True when ready, False otherwise (reason in ``install_error``).
        V1 ``_ensure_aria2c`` (:267-314).
        """
        if self._exe:
            return True
        if not self._bin_dir or sys.platform != "win32":
            self._install_error = "aria2c not found and auto-install unavailable"
            return False

        async with self._install_lock:
            if self._exe:
                return True
            if self._install_status == Aria2cInstallStatus.FAILED:
                return False

            self._install_status = Aria2cInstallStatus.INSTALLING
            logger.info("aria2c not on PATH; auto-installing to %s", self._bin_dir)
            # 缺口 9 — resolve the live global proxy (best-effort) and pass it
            # to the installer so the binary download routes through it.
            proxy = None
            if self._proxy_provider is not None:
                try:
                    proxy = self._proxy_provider()
                except Exception:  # noqa: BLE001 — never break install on proxy read
                    proxy = None
            try:
                result = await asyncio.to_thread(
                    self._install_with_proxy, proxy
                )
            except Exception as exc:  # noqa: BLE001
                self._install_status = Aria2cInstallStatus.FAILED
                self._install_error = f"aria2c auto-install exception: {exc}"
                logger.error("aria2c auto-install exception: %s", exc)
                return False

            if result:
                self._exe = result
                self._install_status = Aria2cInstallStatus.DONE
                logger.info("aria2c auto-install done: %s", result)
                return True
            self._install_status = Aria2cInstallStatus.FAILED
            self._install_error = (
                f"aria2c auto-install failed; place aria2c.exe in {self._bin_dir}"
            )
            logger.warning(self._install_error)
            return False

    # ── Daemon lifecycle ─────────────────────────────────────────────────────

    def ensure_daemon(self) -> bool:
        """Start the RPC daemon if not already running (V1 ``ensure_daemon``).

        Reuses a live process / external daemon when the RPC answers; else
        spawns ``aria2c --enable-rpc`` and waits up to ``_START_TIMEOUT`` for
        readiness. Synchronous (called via ``asyncio.to_thread``).
        """
        if not self._exe:
            return False
        if self._proc and self._proc.poll() is None and self.is_rpc_alive():
            return True
        if self.is_rpc_alive():
            logger.info("aria2c RPC already reachable on %d (external)", RPC_PORT)
            return True

        log_dir = Path(os.environ.get("TEMP", "/tmp")) / "qai_aria2c"
        log_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._exe,
            "--enable-rpc",
            f"--rpc-listen-port={RPC_PORT}",
            "--rpc-allow-origin-all",
            "--daemon=false",
            "--file-allocation=none",
            "--quiet=true",
            "--log-level=warn",
        ]
        logger.info("starting aria2c daemon: %s", " ".join(cmd))
        try:
            stdout_fh = open(
                log_dir / "aria2c.log", "w", encoding="utf-8", errors="replace"
            )
            stderr_fh = open(
                log_dir / "aria2c.err.log", "w", encoding="utf-8", errors="replace"
            )
            try:
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                )
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    creationflags=creationflags,
                    # Pin a stable cwd (the aria2c bin dir) instead of inheriting
                    # the API server's cwd. Download targets use ABSOLUTE ``dir``
                    # options so this no longer affects output location, but a
                    # deterministic cwd avoids any relative-path surprises in
                    # logs / session files (AGENTS.md 🔴 §铁律4).
                    cwd=str(self._bin_dir),
                )
            finally:
                stdout_fh.close()
                stderr_fh.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to start aria2c: %s", exc)
            return False

        # M-9 (铁律5) — orphan-proof the daemon. (1) Assign it to a Windows
        # Job Object with KILL_ON_JOB_CLOSE so a task-manager kill / crash of
        # the API process takes the aria2c child down with it (Windows only).
        # (2) Register an atexit hook as a softer cross-platform backstop for
        # the normal-exit path. Both are best-effort and never raise.
        try:
            self._job = _assign_kill_on_close_job(self._proc.pid)
        except Exception:  # noqa: BLE001
            self._job = None
        if not self._atexit_registered:
            try:
                atexit.register(self._atexit_stop)
                self._atexit_registered = True
            except Exception:  # noqa: BLE001
                pass

        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self.is_rpc_alive():
                logger.info("aria2c daemon ready (pid=%d)", self._proc.pid)
                return True
            time.sleep(0.3)
        logger.error("aria2c daemon not ready within %ds", _START_TIMEOUT)
        return False

    def stop_daemon(self) -> None:
        """Gracefully stop the daemon (RPC shutdown → process kill fallback).

        V1 ``stop_daemon`` (:404-436). Synchronous.

        Fast path (perf, 2026-06-10): when this process never started a daemon
        (``_proc is None``), skip the RPC liveness probe entirely. The probe
        (:meth:`is_rpc_alive` → ``_rpc_call_sync`` → ``urllib.urlopen(timeout=5)``)
        otherwise blocks for the full 5s connect timeout against a port nobody
        is listening on — which dominated app shutdown latency (~6s of an ~11s
        Desktop close) in the common case where downloads were never used. If
        we didn't spawn a daemon there is nothing of ours to reap; another
        process owning a daemon on the shared RPC port is not ours to shut down.
        """
        if self._proc is None:
            # Nothing we started; don't pay the 5s RPC-probe timeout on a
            # likely-dead port during shutdown.
            return

        if self.is_rpc_alive():
            try:
                _rpc_call_sync("aria2.shutdown")
                logger.info("aria2c shutdown sent via RPC")
                time.sleep(1.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("aria2c RPC shutdown failed: %s", exc)

        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                logger.warning("aria2c did not exit; killing")
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
                except OSError as exc:
                    logger.warning("error killing aria2c: %s", exc)
            self._proc = None
            logger.info("aria2c daemon stopped")
        elif not self.is_rpc_alive():
            self._proc = None

        # M-9 — release the Job Object handle once the daemon is stopped so a
        # subsequent restart gets a fresh job (and we don't leak the handle).
        if self._job is not None and sys.platform == "win32":
            try:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

    def _atexit_stop(self) -> None:
        """atexit backstop (M-9) — best-effort kill of a still-running daemon.

        Runs on normal interpreter exit. The Windows Job Object already
        covers the abrupt-kill case; this handles the graceful-exit path
        where ``stop_daemon`` wasn't called (e.g. an embedding script that
        never invoked lifespan shutdown). Never raises.
        """
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - interpreter is tearing down
            pass

    # ── Async RPC helpers (used by the download engine poll loop) ──────────

    async def rpc(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: list | None = None,
    ) -> dict:
        """Async JSON-RPC call (V1 ``_rpc_call_async`` :203-216)."""
        payload = {
            "jsonrpc": "2.0",
            "id": "qai",
            "method": method,
            "params": params or [],
        }
        resp = await client.post(_RPC_URL, json=payload, timeout=5.0)
        return resp.json()

    @staticmethod
    def add_uri_options(save_dir: Path, filename: str) -> dict[str, str]:
        """aria2.addUri options (V1 :507-516).

        ``allow-overwrite=true`` is added on top of the V1 set to fix a real
        defect inherited from V1 (refactor-plan 重构总原则第 5 条 "顺手修掉"):
        when an orphan target file is left from a previous interrupted run but
        its ``.aria2`` control file is gone, aria2c otherwise aborts with
        "File ... exists, but a control file(*.aria2) does not exist. Download
        was canceled ...". With ``continue=true`` aria2c still resumes whenever
        the ``.aria2`` control file is present (no wasted bytes); the overwrite
        only kicks in for the orphan-file case, so the user no longer has to
        manually delete the stale file and retry.

        ROBUSTNESS PARAMS (2026-06-19, paired with download_engine.py stall
        watchdog): the five options below were missing from V1 and earlier V2
        revisions, which let aria2c spin forever on flaky links without ever
        giving up (the user-visible "Setup.bat / download center looks frozen"
        bug). They give aria2c its OWN sense of "this attempt is dead" so it
        eventually exits with an error frame; the download_engine outer
        watchdog is the second line of defence for the case where aria2c
        is alive-but-not-progressing and so wouldn't trip its own retries.

        * ``max-tries=5`` — give up after 5 internal retries (default = 5; we
          set explicitly so future aria2c default changes can't regress us).
        * ``retry-wait=3`` — wait 3 s between aria2c's own retries.
        * ``connect-timeout=15`` — TCP connect must complete within 15 s, else
          retry (default 60 s is too tolerant for a Setup loop).
        * ``timeout=30`` — per-segment read timeout 30 s.
        * ``lowest-speed-limit=50K`` — if the average download speed drops
          below 50 KB/s for ``timeout`` seconds, aria2c treats the connection
          as dead. Critical for the "stalled at 99%" scenario where the TCP
          stream is alive but stops sending bytes.
        """
        return {
            # ABSOLUTE dir (resolve()): the aria2c daemon is spawned WITHOUT an
            # explicit cwd, so it inherits the API server's working directory —
            # which is NOT guaranteed to be the project root (e.g. the desktop
            # shell launches the backend from a different cwd). A *relative*
            # ``dir`` (the default ``data_dir`` is the relative ``Path("data")``)
            # would then be resolved against aria2c's cwd, making it read/write
            # a DIFFERENT ``data/downloads/...`` than the rest of the app — the
            # root cause of the spurious "File ... exists, but a control file
            # (*.aria2) does not exist" error against a directory that looks
            # empty in the real data dir. Resolving to absolute pins aria2c to
            # the exact same location (truth-from-real-state, AGENTS.md 🔴 §铁律4).
            "dir": str(Path(save_dir).resolve()),
            "out": filename,
            "max-connection-per-server": str(_CONNECTIONS),
            "split": str(_CONNECTIONS),
            "min-split-size": _CHUNK_SIZE,
            "continue": "true",
            "allow-overwrite": "true",
            "file-allocation": "none",
            "auto-file-renaming": "false",
            # Robustness params (2026-06-19) — see docstring above.
            "max-tries": "10",
            "retry-wait": "5",
            "connect-timeout": "20",
            "timeout": "60",
            "lowest-speed-limit": "10K",
        }


__all__ = ["Aria2cDaemon", "RPC_PORT", "POLL_INTERVAL"]
