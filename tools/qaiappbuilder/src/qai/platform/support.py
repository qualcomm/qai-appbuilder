# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User support helpers: "contact support" e-mail and the backend-log excerpt.

* :func:`open_support_mail` opens the default mail client on a pre-filled
  message to :data:`SUPPORT_EMAIL`. It runs in the backend on purpose: the
  backend sits on the user's machine for both the browser and the desktop
  edition, and the desktop window refuses non-http(s) navigations, so a
  ``mailto:`` link in the page would do nothing there. It first checks that a
  ``mailto`` handler is registered and returns ``False`` otherwise, so the UI
  can show the address instead of a dead click (or Windows' "pick an app").
* :func:`backend_log_text` returns the tail of ``data/logs/backend.log`` (plus
  the desktop shell's ``desktop-backend.log``), credentials masked, for the
  "copy / export backend log" menu items.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from qai.platform.logging import get_logger
from qai.platform.logging.redact_log import redact_log_text

__all__ = [
    "SUPPORT_EMAIL",
    "backend_log_text",
    "build_support_mailto",
    "mail_client_available",
    "open_support_mail",
    "tail_lines",
]

_log = get_logger(__name__)

SUPPORT_EMAIL = "qaiappbuilder.support@qti.qualcomm.com"

#: Lines of ``desktop-backend.log`` appended after the main log (it has no rotation).
_DESKTOP_LOG_LINES = 200


# ── contact support ────────────────────────────────────────────────────────


def build_support_mailto(
    *, version: str, edition: str, system: str, machine: str, release: str
) -> str:
    """``mailto:`` URL with a subject and a short environment summary as the body.

    No log text: a mailto URL is length-limited, so the body tells the user to
    paste the copied backend log instead.
    """
    subject = f"QAI AppBuilder feedback (v{version})"
    body = "\r\n".join(
        [
            "Please describe the problem (what you did, what happened, what you expected):",
            "",
            "",
            "",
            "---",
            f"QAI AppBuilder {version} ({edition})",
            f"OS: {system} {release} ({machine})",
            "",
            "Tip: sidebar user menu > Copy log, then paste it here.",
        ]
    )
    return f"mailto:{SUPPORT_EMAIL}?subject={quote(subject)}&body={quote(body)}"


#: Executables Windows reports when no real mail client is the default ("pick an app").
_WINDOWS_NO_HANDLER = {"openwith.exe", "launchwinapp.exe"}


def _windows_default_handler() -> str:
    """Executable of the user's DEFAULT ``mailto`` handler.

    ``""`` when Windows would ask "pick an app" instead.

    That also happens when the default points at an app that is gone (a stale
    store-app choice), even though a client such as Outlook is installed.
    """
    import ctypes  # noqa: PLC0415 — Windows-only
    from ctypes import wintypes  # noqa: PLC0415

    assoc_query = ctypes.windll.shlwapi.AssocQueryStringW  # type: ignore[attr-defined]
    assoc_query.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    assoc_query.restype = ctypes.c_long  # HRESULT
    assocf_is_protocol = 0x00001000
    assocstr_executable = 2
    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    hr = assoc_query(
        assocf_is_protocol, assocstr_executable, "mailto", None, buf, ctypes.byref(size)
    )
    exe = buf.value.strip() if hr == 0 else ""
    if not exe or Path(exe).name.lower() in _WINDOWS_NO_HANDLER:
        return ""
    return exe


def _windows_registered_command() -> str:
    """``HKCR\\mailto\\shell\\open\\command`` of an installed client (e.g. Outlook), or ``""``.

    Used when the user's default is broken: the client registered the protocol
    itself, so its own command line can still open a new message.
    """
    import winreg  # noqa: PLC0415 — Windows-only

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"mailto\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return ""
    command = str(command or "").strip()
    exe = command[1:].split('"', 1)[0] if command.startswith('"') else command.split(" ", 1)[0]
    if (
        "%1" not in command
        or not Path(exe).is_file()
        or Path(exe).name.lower() in _WINDOWS_NO_HANDLER
    ):
        return ""
    return command


def mail_client_available() -> bool:
    """``True`` when a mail client can open ``mailto:`` links."""
    try:
        if sys.platform == "win32":
            return bool(_windows_default_handler() or _windows_registered_command())
        if sys.platform == "darwin":
            return True  # Mail.app is always there
        xdg_mime = shutil.which("xdg-mime")
        if not xdg_mime or not shutil.which("xdg-open"):
            return False
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [xdg_mime, "query", "default", "x-scheme-handler/mailto"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001 — "unknown" is treated as "no client": the UI then shows the address
        _log.warning("support.mail_client_check_failed", exc_info=True)
        return False


def _open_on_windows(mailto_url: str) -> bool:
    if _windows_default_handler():
        os.startfile(mailto_url)  # type: ignore[attr-defined]  # noqa: S606
        return True
    command = _windows_registered_command()
    if not command:
        return False
    # The registered command line quotes "%1" itself; the URL has no quotes
    # in it (quote() encodes them).
    subprocess.Popen(command.replace("%1", mailto_url))  # noqa: S603 — command from the client's own registration
    return True


def open_support_mail(mailto_url: str) -> bool:
    """Open ``mailto_url`` in a mail client; ``False`` when there is none or it failed."""
    try:
        if sys.platform == "win32":
            return _open_on_windows(mailto_url)
        if not mail_client_available():
            return False
        if sys.platform == "darwin":
            subprocess.Popen(["open", mailto_url])  # noqa: S603, S607
        else:
            subprocess.Popen([shutil.which("xdg-open") or "xdg-open", mailto_url])  # noqa: S603
    except Exception:  # noqa: BLE001
        _log.warning("support.open_mail_failed", exc_info=True)
        return False
    return True


# ── backend log ────────────────────────────────────────────────────────────


def tail_lines(path: Path, count: int, *, chunk: int = 64 * 1024) -> list[str]:
    """Last ``count`` lines of ``path`` (oldest first), reading backwards from the end.

    Never loads the whole file; a missing / unreadable file yields ``[]``.
    """
    if count <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-count:]


def backend_log_text(
    logs_dir: Path,
    *,
    lines: int,
    header: Sequence[str] = (),
    now: datetime | None = None,
) -> str:
    """The last ``lines`` lines of the backend log, credentials masked.

    ``backend.log`` is topped up from the rotated ``backend.log.1`` when it is
    shorter than requested (just after a rotation). The desktop shell's
    ``desktop-backend.log`` tail is appended under its own heading when present.
    """
    main = tail_lines(logs_dir / "backend.log", lines)
    if len(main) < lines:
        main = tail_lines(logs_dir / "backend.log.1", lines - len(main)) + main
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    parts = [
        *header,
        f"Generated: {stamp}",
        f"Log: {logs_dir / 'backend.log'} (last {len(main)} lines)",
        "",
    ]
    parts.extend(main or ["(backend.log is empty or missing)"])
    desktop = tail_lines(logs_dir / "desktop-backend.log", _DESKTOP_LOG_LINES)
    if desktop:
        parts += ["", f"===== desktop-backend.log (last {len(desktop)} lines) =====", *desktop]
    return redact_log_text("\n".join(parts)) + "\n"
