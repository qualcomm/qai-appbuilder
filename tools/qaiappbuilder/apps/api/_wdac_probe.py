# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Best-effort probe for Windows code-integrity enforcement (WDAC / Smart
App Control).

Why this exists
---------------
uv's managed CPython and, more importantly, the Qualcomm QAIRT SDK's native
converter extensions (e.g. ``libPyIrGraph310.pyd``) are UNSIGNED third-party
binaries. On a machine whose Enterprise WDAC policy enforces user-mode code
integrity (UMCI), or with Smart App Control (SAC) on, the kernel refuses to
load those unsigned DLLs (``python.exe`` exits ``0xC0E90002``; CodeIntegrity
event 3033/3077). The practical effect for THIS app: **model conversion
(Model Builder) cannot work** because the converter ``.pyd`` load is blocked,
and the SDK lives under the write-protected ``C:\\Qualcomm`` tree so it cannot
be re-signed.

This module reports whether such enforcement is active so the Web UI can show
a heads-up on the chat welcome screen. It is a *diagnostic hint only* — it
never changes behaviour.

Design
------
* Windows-only. On any non-Windows host (or any probe error) it reports
  ``enabled = False`` — a **fail-open** default: a warning banner must never
  cry wolf on a machine we could not positively confirm is enforcing.
* Cached for the process lifetime (the policy does not change under our feet
  within a run; a real change requires a reboot / re-login).
* UMCI is read from ``Win32_DeviceGuard``
  (``UsermodeCodeIntegrityPolicyEnforcementStatus``: 0=Off 1=Audit 2=Enforced)
  via a single short PowerShell one-shot with a hard timeout — there is no
  pure-stdlib reader for that WMI class. SAC is read from the registry
  (``HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy``
  ``VerifiedAndReputablePolicyState``: 0=Off 1=Eval 2=On) via ``winreg`` (no
  subprocess).
* ``enabled`` is True when UMCI is Enforced (2) OR SAC is On/Eval (2/1) — any
  of those blocks the unsigned converter ``.pyd``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class WdacStatus:
    """Resolved code-integrity enforcement snapshot."""

    enabled: bool
    #: UsermodeCodeIntegrityPolicyEnforcementStatus (0/1/2), or -1 if unknown.
    umci: int
    #: SAC VerifiedAndReputablePolicyState (0/1/2), or -1 if unknown.
    sac: int


_cached: WdacStatus | None = None


def _probe_umci() -> int:
    """Return UMCI enforcement status (0/1/2) or -1 when it cannot be read."""
    # No stdlib reader for Win32_DeviceGuard; use a single short PowerShell
    # one-shot. NoProfile keeps it fast; a hard timeout keeps startup snappy
    # even on a wedged WMI.
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        (
            "$g = Get-CimInstance -Namespace 'root/Microsoft/Windows/DeviceGuard' "
            "-ClassName Win32_DeviceGuard -ErrorAction Stop; "
            "[int]$g.UsermodeCodeIntegrityPolicyEnforcementStatus"
        ),
    ]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if out.returncode != 0:
        return -1
    token = (out.stdout or "").strip().splitlines()
    if not token:
        return -1
    try:
        return int(token[0].strip())
    except ValueError:
        return -1


def _probe_sac() -> int:
    """Return SAC VerifiedAndReputablePolicyState (0/1/2) or -1 if unreadable."""
    try:
        import winreg  # noqa: PLC0415 — Windows-only import
    except ImportError:
        return -1
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\CI\Policy",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")
            return int(value)
    except (OSError, ValueError):
        # Key/value absent (feature not present) or unreadable.
        return -1


def get_wdac_status(*, refresh: bool = False) -> WdacStatus:
    """Resolve (and cache) the code-integrity enforcement snapshot.

    Fail-open: any non-Windows host or probe error yields ``enabled=False``.
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached

    if sys.platform != "win32":
        _cached = WdacStatus(enabled=False, umci=-1, sac=-1)
        return _cached

    umci = _probe_umci()
    sac = _probe_sac()
    # Any enforcing code-integrity surface blocks the unsigned converter .pyd.
    enabled = (umci == 2) or (sac in (1, 2))
    _cached = WdacStatus(enabled=enabled, umci=umci, sac=sac)
    return _cached
