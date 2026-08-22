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
"""

from __future__ import annotations

import os
import sys

__all__ = ["is_admin"]


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
