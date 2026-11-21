# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Detect whether the current process runs with administrator privileges.

Why this module exists
----------------------
The service must refuse to run elevated: files written by an elevated
process (venv caches, ``data/``, endpoint file, logs) become owned by
Administrators / root, and a later non-elevated run then fails to update
them — a foot-gun that manifests days after installation as opaque
permission errors far from the elevated launch. Cheaper to reject the
launch up front than to diagnose the fallout.

Cross-platform detection
------------------------
* **Windows** — ``ctypes.windll.shell32.IsUserAnAdmin()`` returns non-zero
  when the process token has the Administrators SID enabled (i.e. UAC
  did not filter it). It is the same API the shell / MSI installers use;
  ships with every Windows install, needs no ``pywin32``.
* **POSIX (Linux / macOS)** — ``os.geteuid() == 0``. The *effective* UID
  is what actually decides privileged syscalls; ``os.getuid()`` reflects
  the login UID and would miss a setuid-root binary. We follow the
  kernel's own criterion.

Both probes are stdlib-only and cheap enough to call on every launch.
Failure modes fall open: if the check itself raises (missing ``shell32``,
``geteuid`` unavailable), we assume "not admin" rather than blocking
launch on a broken probe.

The refusal only makes sense where a non-elevated run is possible.
:func:`elevation_is_unavoidable` detects the Windows sessions where it is
not (UAC off, built-in Administrator account) so the caller can let those
through instead of locking the user out.
"""

from __future__ import annotations

import os
import sys

__all__ = ["elevation_is_unavoidable", "is_admin"]

# TOKEN_INFORMATION_CLASS.TokenElevationType and its TOKEN_ELEVATION_TYPE values.
_TOKEN_ELEVATION_TYPE_CLASS = 18
_TOKEN_ELEVATION_TYPE_DEFAULT = 1  # no split token: UAC off, or the built-in Administrator
_TOKEN_QUERY = 0x0008


def is_admin() -> bool:
    """Return ``True`` iff the current process has administrator privileges.

    * On Windows this is the ``IsUserAnAdmin()`` shell API — non-zero means
      the Administrators group is *enabled* in the current process token
      (UAC has not filtered it out).
    * On POSIX this is ``os.geteuid() == 0`` — the effective UID is what
      the kernel checks for privileged operations.

    Fails open: any unexpected error from the underlying probe returns
    ``False`` so a broken check never blocks a legitimate non-admin launch.
    """
    try:
        if sys.platform == "win32":
            # Imported lazily so importing this module on POSIX does not pull
            # in ctypes.windll (which raises AttributeError off-Windows).
            import ctypes  # noqa: PLC0415 — Windows-only; keep off POSIX import path

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        # POSIX (Linux / macOS): effective UID is the true privilege source.
        # ``geteuid`` is absent on Windows (already handled above) but
        # ``getattr`` keeps the fallback safe on exotic platforms.
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            return False
        return geteuid() == 0
    except Exception:  # noqa: BLE001 — fail-open by design; see module docstring
        return False


def elevation_is_unavoidable() -> bool:
    """Return ``True`` when this Windows session cannot run anything unelevated.

    That is the case when the process token is elevated but NOT the elevated
    half of a UAC split token (``TokenElevationTypeDefault``): UAC is turned
    off, or the user signed in with the built-in Administrator account. Every
    process there runs elevated, so there is no later non-elevated run whose
    files could clash, and refusing to start would only lock the user out.

    ``False`` everywhere else: an explicit "Run as administrator" under UAC
    (``TokenElevationTypeFull``), any POSIX root (``sudo`` is avoidable), or a
    failed probe — this check only ever relaxes the refusal, so it fails
    closed.
    """
    # A standard (non-admin) user has no split token either, so the token type
    # alone is ambiguous: it only means "UAC off / built-in Administrator" when
    # the process is also elevated.
    if sys.platform != "win32" or not is_admin():
        return False
    try:
        import ctypes  # noqa: PLC0415 — Windows-only; keep off POSIX import path
        from ctypes import wintypes  # noqa: PLC0415

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            return False
        try:
            elevation_type = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token, _TOKEN_ELEVATION_TYPE_CLASS, ctypes.byref(elevation_type),
                ctypes.sizeof(elevation_type), ctypes.byref(returned),
            ):
                return False
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 — fail closed: keep the refusal on a broken probe
        return False
    return elevation_type.value == _TOKEN_ELEVATION_TYPE_DEFAULT
