# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""OS-level "parent dies -> child dies" safeguard for spawned subprocesses.

This is the *extra* teardown rail mandated by ``AGENTS.md`` 🔴
State-Truth-First **铁律 5**: a spawned subprocess must have a fallback that
reaps it even when the parent (this API process) is *force-killed* (Task
Manager / SIGKILL / power loss) and never gets a chance to run its graceful
``stop()`` / lifespan-shutdown path.

Design (cross-platform, per ``AGENTS.md`` 🟠 跨平台前瞻):

* **Windows** — use a Win32 **Job Object** with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The job handle is held open by this
  (parent) process; the spawned child is assigned to the job. When the parent
  exits *for any reason* — graceful or hard-killed — the OS closes the last
  handle to the job and terminates every process still in it. This is a pure
  OS-level guarantee that does not depend on any Python teardown running.
* **Non-Windows** — a graceful no-op. POSIX has its own mechanisms (process
  groups / ``prctl(PR_SET_PDEATHSIG)``) but the current single supported
  platform is Windows ARM64; the no-op keeps imports working and never raises
  on Linux/macOS so a future CI lane can run ``-m "not windows_only"`` cleanly.

This rail is **additive** and orthogonal to the existing graceful
``ProcessBackedInferenceService.stop()`` (CTRL_BREAK -> terminate -> kill) and
to the in-process ``poll()`` status truth source (铁律 1). It never changes
start/stop/status semantics; it only ensures no orphan survives a hard parent
death.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("qai.platform.process.kill_group")


class ProcessKillGroup:
    """Owns a "kill-on-parent-close" group and assigns children to it.

    On Windows this wraps a Job Object configured with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. On other platforms every method is
    a graceful no-op (``available`` stays ``False``).

    A single instance is created lazily and held for the lifetime of the
    process. Assigning a child is idempotent and best-effort: any failure is
    logged at WARNING and swallowed, because this is a *defence-in-depth*
    fallback — the primary graceful stop path must keep working even if the
    Job Object machinery is unavailable (e.g. running under an outer job that
    forbids breakaway).
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        self._available = False
        if sys.platform == "win32":
            self._create_windows_job()

    @property
    def available(self) -> bool:
        """True iff a real OS kill-group is backing this instance."""
        return self._available

    def assign(self, pid: int) -> bool:
        """Assign the process ``pid`` to the kill-group.

        Returns ``True`` if the assignment succeeded (Windows, job created and
        ``AssignProcessToJobObject`` returned non-zero). Returns ``False`` on
        any non-Windows platform or any failure — callers treat this as
        "the extra rail is not active", never as a fatal error.
        """
        if not self._available or self._handle is None:
            return False
        if sys.platform != "win32":  # pragma: no cover - defensive
            return False
        return self._assign_windows(pid)

    # ------------------------------------------------------------------
    # Windows implementation (ctypes / kernel32)
    # ------------------------------------------------------------------

    def _create_windows_job(self) -> None:
        """Create a Job Object with KILL_ON_JOB_CLOSE; store its handle.

        Mirrors the canonical Win32 idiom:
        ``CreateJobObjectW`` -> ``SetInformationJobObject`` with
        ``JobObjectExtendedLimitInformation`` carrying
        ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Best-effort: any failure
        leaves ``available = False`` so callers fall back to the graceful
        path alone.
        """
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # JOBOBJECT_BASIC_LIMIT_INFORMATION + JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        # Win32 constants
        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x0000_2000
        # JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK — lets a child created with
        # ``CREATE_BREAKAWAY_FROM_JOB`` (or its own children) leave this job
        # without erroring. Critical for defence-in-depth correctness when the
        # API process is ALREADY inside an outer job (common under service
        # hosts / some launchers): without breakaway-ok, nested-job semantics
        # can make ``AssignProcessToJobObject`` fail, or — worse — couple our
        # worker's lifetime to the outer job so that closing THAT job reaps our
        # API process too (surfacing as a spurious 0xFFFFFFFF crash). Setting
        # this flag keeps our KILL_ON_JOB_CLOSE guarantee for children we DO
        # assign while allowing clean breakaway everywhere else.
        job_object_limit_silent_breakaway_ok = 0x0000_0800

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [
            wintypes.LPVOID,
            wintypes.LPCWSTR,
        ]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            err = ctypes.get_last_error()
            logger.warning(
                "CreateJobObjectW failed (err=%d); orphan-kill fallback "
                "disabled, graceful stop still active",
                err,
            )
            return

        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = (
            job_object_limit_kill_on_job_close
            | job_object_limit_silent_breakaway_ok
        )

        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        ok = kernel32.SetInformationJobObject(
            handle,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            logger.warning(
                "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed (err=%d); "
                "orphan-kill fallback disabled",
                err,
            )
            kernel32.CloseHandle(handle)
            return

        self._handle = int(handle)
        self._available = True
        logger.info("Process kill-group ready (Job Object, KILL_ON_JOB_CLOSE)")

    def _assign_windows(self, pid: int) -> bool:
        """Assign ``pid`` to the Job Object. Best-effort, never raises."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        process_set_quota = 0x0100
        process_terminate = 0x0001

        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        proc_handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
        if not proc_handle:
            err = ctypes.get_last_error()
            logger.warning(
                "OpenProcess(pid=%d) failed (err=%d); child not assigned to kill-group",
                pid,
                err,
            )
            return False
        try:
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            ok = kernel32.AssignProcessToJobObject(wintypes.HANDLE(self._handle), proc_handle)
            if not ok:
                err = ctypes.get_last_error()
                logger.warning(
                    "AssignProcessToJobObject(pid=%d) failed (err=%d); "
                    "child not in kill-group (graceful stop still active)",
                    pid,
                    err,
                )
                return False
            logger.debug("Assigned pid=%d to kill-group", pid)
            return True
        finally:
            kernel32.CloseHandle(proc_handle)


__all__ = ["ProcessKillGroup"]
