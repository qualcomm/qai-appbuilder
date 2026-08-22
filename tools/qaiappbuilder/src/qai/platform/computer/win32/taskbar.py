# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Taskbar button names + coordinates, read from the shell's accessibility tree.

Why this is built in
--------------------
Taskbar buttons are ~66 px wide and visually near-identical at screenshot scale,
so a model estimating a position from pixels reliably lands on the neighbouring
app: observed in practice as "clicked Notepad++, got Chrome" (92 px off, one
button over). Their accessible names, however, say exactly what they are
("Notepad++ pinned", "Search", "Start"), which makes NAME -> COORDINATE the
right interface. Launching a pinned app or hitting Start/Search is frequent
enough — and the payload small enough (a couple dozen short entries) — to ship
with every screenshot rather than wait for the model to think of querying it.

Why UIAutomation
----------------
Windows 11 renders taskbar buttons as XAML, so they are NOT child HWNDs and
cannot be enumerated with ``FindWindowEx`` / ``ToolbarWindow32`` tricks that
worked on older shells. UIAutomation sees both eras, which is also why it is the
recommended route for in-window controls generally.

Why a subprocess
----------------
UIAutomation is COM. Hosting it in-process would pull COM apartment
initialisation into the capture worker (which is already thread-affine for Win32
input) and risk destabilising it. PowerShell ships the
``UIAutomationClient`` assemblies on every supported Windows, so a short-lived
child process keeps the COM lifetime fully outside our worker for a cost paid
only when a screenshot is taken.

Every failure degrades to an empty list: this is an assist, never a hard
dependency.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from typing import Any

__all__ = ["list_taskbar_buttons", "prewarm"]

#: The query costs ~2.7s (PowerShell startup ~1.0s + .NET assemblies ~0.2s +
#: tree walk ~1.5s, measured), so it is never run on a caller's thread. Taskbar
#: layout only shifts when an app is pinned, launched, or closed, so a short
#: cache serves reads while a background refresh keeps it current. Deliberately
#: short: a stale coordinate is worse than a missing one, and a newly launched
#: app should show up within a turn or two.
_CACHE_TTL_S = 30.0
_cache: list[dict[str, Any]] | None = None
_cache_at: float = 0.0
#: Guards the "is a refresh already running" flag so concurrent screenshots
#: spawn one worker, not one per call.
_warm_lock = threading.Lock()
_warming = False
#: Long accessible names (tray items embed status text and newlines) are trimmed
#: so one verbose entry cannot dominate the tool result.
_MAX_NAME_CHARS = 48

#: Tray/status items are reported by the same UIA query but are not useful click
#: targets for "open this app" — clock, volume, battery, network and IME just
#: describe state, and their names carry long status text. Their AutomationIds
#: mark them, so they are dropped by id rather than by fragile name matching.
#: ``SystemTrayIcon`` also covers "Show Hidden Icons" / "Show Desktop"; a model
#: that genuinely needs those can still find them in the screenshot.
_TRAY_AUTOMATION_IDS: frozenset[str] = frozenset(
    {"SystemTrayIcon", "NotifyItemIcon"}
)
#: Upper bound on reported buttons. A crowded taskbar still fits well inside
#: this; the cap only stops a pathological tree from flooding the payload.
_MAX_BUTTONS = 40
#: UIAutomation via PowerShell is a process spawn plus a tree walk; this ceiling
#: keeps a wedged shell from stalling a screenshot.
_TIMEOUT_S = 20.0

#: Emits one JSON object per taskbar button. ``BoundingRectangle`` is already in
#: physical screen pixels, the same space as input and a native screenshot.
#: English-only source (PS 5.x reads scripts as OEM; non-ASCII would corrupt).
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$root = [Windows.Automation.AutomationElement]::RootElement
$cond = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ClassNameProperty, 'Shell_TrayWnd')
$tray = $root.FindFirst([Windows.Automation.TreeScope]::Children, $cond)
if (-not $tray) { Write-Output '[]'; exit 0 }
$btnCond = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Button)
$buttons = $tray.FindAll([Windows.Automation.TreeScope]::Descendants, $btnCond)
$out = New-Object System.Collections.ArrayList
$seen = New-Object System.Collections.Generic.HashSet[string]
foreach ($b in $buttons) {
    $r = $b.Current.BoundingRectangle
    if ($r.Width -le 0 -or $r.Height -le 0) { continue }
    $name = $b.Current.Name
    if (-not $name) { continue }
    $key = "$([int]$r.X)x$([int]$r.Y)"
    if (-not $seen.Add($key)) { continue }
    [void]$out.Add([pscustomobject]@{
        name      = $name
        automation_id = $b.Current.AutomationId
        x         = [int]$r.X
        y         = [int]$r.Y
        width     = [int]$r.Width
        height    = [int]$r.Height
        center_x  = [int]($r.X + $r.Width / 2)
        center_y  = [int]($r.Y + $r.Height / 2)
    })
}
$out | ConvertTo-Json -Compress -Depth 3
"""


#: Trailing state that Windows appends to a taskbar button's accessible name:
#: an optional " - N running window(s)" and/or " pinned". Stripped as ONE regex
#: rather than a suffix list, because checking suffixes in sequence strips only
#: the first match and would leave "Notepad++ - 1 running window" behind.
_STATE_SUFFIX_RE = re.compile(
    r"(?:\s*-\s*\d+\s+running\s+windows?)?(?:\s+pinned)?\s*$",
    re.IGNORECASE,
)


def _clean_name(raw: str) -> str:
    """Collapse a multi-line accessible name into one short app label.

    Tray items pack status text and newlines into ``Name`` (battery percentage,
    network detail, IME state); the leading fragment identifies the button. The
    trailing pinned/running state is dropped so the label is the app name a model
    already knows.
    """
    first = raw.replace("\r", "\n").split("\n", 1)[0].strip()
    first = _STATE_SUFFIX_RE.sub("", first).strip()
    if len(first) > _MAX_NAME_CHARS:
        first = first[:_MAX_NAME_CHARS].rstrip() + "…"
    return first


def prewarm() -> None:
    """Refresh the cache off the calling thread; never blocks, never raises.

    The query costs ~2.7s, almost all of it PowerShell startup (~1.0s) plus
    .NET assembly load (~0.2s) — measured, and an order of magnitude more than
    the ~190ms screenshot it would otherwise delay. Running it in a daemon
    thread means a caller pays 0ms: the first screenshot reports no taskbar
    buttons, and every subsequent one reads a warm cache. Trading "correct on
    the very first call" for "never stalls a turn" is the right way round, since
    an agent that needs a taskbar coordinate takes another screenshot anyway.
    """
    if sys.platform != "win32":
        return
    global _warming
    with _warm_lock:
        if _warming:
            return
        _warming = True

    def _run() -> None:
        global _warming
        try:
            _query_and_cache()
        finally:
            with _warm_lock:
                _warming = False

    threading.Thread(target=_run, name="taskbar-prewarm", daemon=True).start()


def list_taskbar_buttons() -> list[dict[str, Any]]:
    """Cached taskbar buttons; triggers a background refresh when stale.

    Returns immediately with whatever is cached (possibly ``[]`` on the very
    first call) rather than blocking a turn on the multi-second query — see
    :func:`prewarm`.
    """
    if sys.platform != "win32":
        return []
    now = time.monotonic()
    if _cache is None or (now - _cache_at) >= _CACHE_TTL_S:
        prewarm()
    return _cache or []


def _query_and_cache() -> list[dict[str, Any]]:
    """Run the UIA query and store the result. Blocking; called off-thread."""
    global _cache, _cache_at
    if sys.platform != "win32":
        return []
    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < _CACHE_TTL_S:
        return _cache
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    payload = (proc.stdout or "").strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    # ConvertTo-Json emits a bare object (not a list) for a single result.
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    buttons: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        automation_id = str(entry.get("automation_id") or "")
        if automation_id in _TRAY_AUTOMATION_IDS:
            continue
        name = _clean_name(str(entry.get("name") or ""))
        if not name:
            continue
        try:
            buttons.append({
                "name": name,
                "automation_id": automation_id,
                "x": int(entry["x"]),
                "y": int(entry["y"]),
                "width": int(entry["width"]),
                "height": int(entry["height"]),
                "center_x": int(entry["center_x"]),
                "center_y": int(entry["center_y"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
        if len(buttons) >= _MAX_BUTTONS:
            break
    _cache = buttons
    _cache_at = now
    return buttons
