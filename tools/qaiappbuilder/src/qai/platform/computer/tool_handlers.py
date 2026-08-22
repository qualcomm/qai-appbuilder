# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM tool dispatch handler for the ``computer`` tool.

:func:`handle_computer` parses + validates the batch, runs it through the
injected :class:`DesktopControllerPort`, and returns a result envelope.
The screenshot reaches the model via the project's existing multimodal
image pipeline: the PNG is persisted through the injected image sink,
which returns a ``/api/images/files/...`` URL, and the handler embeds
that URL as ``![screenshot](url)`` markdown in the ``message`` field.
The chat streaming loop decodes that markdown into a vision block — no
new image channel is created (book 02 §4).

Approval helpers (:func:`computer_approval` / :func:`format_approval_
details`) let the chat bridge route input batches through the existing
authorization path.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any

from .ports import DesktopControllerPort
from .tool_schemas import parse_actions
from .types import Action, Capture, DesktopError

__all__ = [
    "ScreenshotSaver",
    "computer_approval",
    "format_approval_details",
    "handle_computer",
]

#: Async callable that persists a base64 PNG and returns its fetch URL
#: (or ``None`` on failure). The chat bridge supplies a closure over the
#: image-upload store; the platform handler never imports the chat
#: context, keeping the ``context-isolation`` contract intact. Args are
#: ``(base64_png, conversation_id, message_id)``.
ScreenshotSaver = Callable[[str, str, str], Awaitable["str | None"]]


def _list_desktop_icons() -> list[dict[str, Any]]:
    """Exact desktop-icon rectangles, or ``[]`` when unavailable.

    Imported lazily and guarded so this module stays importable (and the tool
    stays usable) on platforms without a Win32 desktop list view. An assist that
    cannot be provided must never fail the tool call.
    """
    try:
        from .win32.desktop_icons import list_desktop_icons

        return list_desktop_icons()
    except Exception:  # noqa: BLE001 — icon lookup is best-effort
        return []


def _describe_windows() -> list[dict[str, Any]]:
    """Visible top-level windows (foreground first), or ``[]`` when unavailable."""
    try:
        from .win32.windows_info import describe_windows

        return describe_windows()
    except Exception:  # noqa: BLE001 — window metadata is best-effort
        return []


def _taskbar_rect() -> dict[str, int] | None:
    """Taskbar rectangle, or ``None`` when unavailable."""
    try:
        from .win32.windows_info import taskbar_rect

        return taskbar_rect()
    except Exception:  # noqa: BLE001
        return None


def _taskbar_buttons() -> list[dict[str, Any]]:
    """Taskbar button names + centres, or ``[]`` when unavailable."""
    try:
        from .win32.taskbar import list_taskbar_buttons

        return list_taskbar_buttons()
    except Exception:  # noqa: BLE001 — taskbar lookup is best-effort
        return []


def computer_approval(args: dict) -> str:
    """Classify a batch as ``"read"`` (screenshot/wait only) or ``"exec"``.

    Any input action (click/type/drag/...) makes the whole batch
    ``"exec"`` (needs authorization); a batch of only ``screenshot`` /
    ``wait`` is ``"read"`` (auto-approved). An unparseable batch is
    conservatively ``"exec"`` so the guard sees it (fail-closed).
    """
    try:
        actions = parse_actions(args)
    except DesktopError:
        return "exec"
    return "exec" if any(a.is_input() for a in actions) else "read"


_MAX_DETAIL_LINES = 12


def format_approval_details(args: dict) -> list[str]:
    """Render each action as one human-readable approval line.

    Caps at :data:`_MAX_DETAIL_LINES` lines, appending ``+N more`` when
    truncated; each line is length-bounded to avoid dialog overflow.
    """
    try:
        actions = parse_actions(args)
    except DesktopError as exc:
        return [f"invalid actions: {exc}"]
    lines = [_render_action(a) for a in actions]
    if len(lines) > _MAX_DETAIL_LINES:
        extra = len(lines) - _MAX_DETAIL_LINES
        lines = lines[:_MAX_DETAIL_LINES] + [f"+{extra} more"]
    return lines


def _render_action(a: Action) -> str:
    def _clip(text: str, limit: int = 60) -> str:
        return text if len(text) <= limit else text[:limit] + "…"

    if a.type == "screenshot":
        return "screenshot" if a.window is None else f"screenshot of window {a.window}"
    if a.type == "wait":
        return "wait"
    if a.type == "ui_snapshot":
        scope = "foreground window" if a.window is None else f"window {a.window}"
        return f"read UI tree of {scope}"
    suffix = _mods(a) + _routing(a)
    if a.type == "move":
        return f"move to {_where(a)}"
    if a.type == "click":
        return f"click button={a.button} at {_where(a)}{suffix}"
    if a.type == "double_click":
        return f"double_click at {_where(a)}{suffix}"
    if a.type == "drag":
        pts = "->".join(f"({p.x},{p.y})" for p in (a.path or ()))
        return _clip(f"drag path={pts}{suffix}")
    if a.type == "scroll":
        return f"scroll ({a.scroll_x},{a.scroll_y}) at {_where(a)}{suffix}"
    if a.type == "type":
        return _clip(f'type text="{a.text}"{_routing(a)}')
    if a.type == "keypress":
        return _clip(f"keypress {list(a.keys or ())}{_routing(a)}")
    return a.type


def _where(a: Action) -> str:
    """How the action names its target, as shown in an approval line.

    The user is authorising a real click, so the line must say what is actually
    being targeted: a ref resolves elsewhere than the literal numbers, and
    window-relative coordinates are not screen coordinates.
    """
    if a.ref is not None:
        return f"UI element {a.ref}"
    if a.window is not None:
        return f"({a.x},{a.y}) inside window {a.window}"
    return f"({a.x},{a.y})"


def _routing(a: Action) -> str:
    """Note non-default delivery: it changes whether the user's focus moves."""
    if a.delivery != "background":
        return ""
    target = f"window {a.window}" if a.window is not None else f"owner of {a.ref}"
    return f" [posted to {target}, no cursor/focus change]"


def _mods(a: Action) -> str:
    return f" +{list(a.keys)}" if a.keys else ""


async def handle_computer(
    params: dict,
    *,
    controller: DesktopControllerPort,
    save_screenshot: ScreenshotSaver | None = None,
    conversation_id: str = "",
    message_id: str = "",
    safety_pending: bool = False,
) -> dict[str, Any]:
    """Dispatch a ``computer`` tool invocation.

    Args:
        params: LLM tool-call arguments (``{"actions": [...]}``).
        controller: Live desktop controller (the subprocess supervisor).
        save_screenshot: Async callable persisting the PNG and returning a
            fetch URL so the model can see the pixels; when ``None`` the
            PNG is returned base64 in ``details`` only (no markdown ref).
        conversation_id / message_id: Grouping keys for the saver.
        safety_pending: When ``True`` an unresolved safety check exists;
            an input batch is refused (fail-closed).

    Returns:
        A result envelope dict (``ok`` + ``message`` + ``details`` and,
        for a persisted screenshot, an image markdown ref in ``message``).
    """
    # 1) parse + validate (fail-closed)
    try:
        actions = parse_actions(params)
    except DesktopError as exc:
        return {
            "ok": False,
            "error_code": "computer.invalid_action",
            "message": f"invalid action: {exc.message}",
        }

    # 2) safety gate: refuse INPUT batches while a safety check is pending
    if safety_pending and any(a.is_input() for a in actions):
        return {
            "ok": False,
            "error_code": "computer.safety_pending",
            "message": (
                "refused: an unresolved safety check requires explicit "
                "approval before any desktop input"
            ),
        }

    # 3) execute
    try:
        capture = await controller.execute(actions)
    except DesktopError as exc:
        return {
            "ok": False,
            "error_code": f"computer.{exc.name}",
            "message": exc.message,
        }

    # 4) build the envelope (image via existing markdown pipeline).
    #    ``audience`` decides whether the screenshot is FED TO THE MODEL:
    #    - "user"        → shown to the user + pushed to channels, but the
    #                      tool result carries NO image ref, so no vision
    #                      block is generated and the model's context is not
    #                      charged (the user just wanted to SEE the screen).
    #    - "model"/"both"→ the image ref rides in the result so the model
    #                      also gets the pixels (and it is shown too).
    audience = params.get("audience")
    if audience not in ("user", "model", "both"):
        audience = "both"
    return await _build_envelope(
        capture=capture,
        actions=actions,
        save_screenshot=save_screenshot,
        conversation_id=conversation_id,
        message_id=message_id,
        audience=audience,
    )


async def _build_envelope(
    *,
    capture: Capture,
    actions: list[Action],
    save_screenshot: ScreenshotSaver | None,
    conversation_id: str,
    message_id: str,
    audience: str = "both",
) -> dict[str, Any]:
    b64 = base64.b64encode(capture.data).decode("ascii")
    action_types = [a.type for a in actions]
    # Is this a WINDOW frame or a DESKTOP frame? ``capture_window`` reports a
    # single panel whose id is namespaced, which is the only reliable marker.
    # The distinction is not cosmetic: a window frame's pixels are relative to
    # that window, so calling its size "screen" made the model believe the
    # display was 1728x1466 and compute every absolute coordinate in the wrong
    # space — observed in a real session where it then clicked outside the
    # window repeatedly.
    window_frame = (
        len(capture.displays) == 1
        and capture.displays[0].id.startswith("window:")
    )
    # The MESSAGE is what the model reads (``details`` is for callers), so the
    # geometry it needs to reason about coordinates has to appear here. With more
    # than one monitor the composite spans them all, and without the per-panel
    # pixel ranges the model cannot tell which region belongs to which screen.
    if window_frame:
        panel = capture.displays[0]
        summary = (
            f"executed {len(actions)} action(s); "
            f"WINDOW frame {capture.width}x{capture.height} of {panel.name!r} "
            f"— these pixels are the WINDOW, not the screen. The window sits at "
            f"({panel.x},{panel.y}) on screen. To click a point you see here, "
            f"either pass window={panel.id.removeprefix('window:')} with "
            f"window-relative x/y, or add ({panel.x},{panel.y}) to get screen "
            f"coordinates"
        )
    else:
        summary = (
            f"executed {len(actions)} action(s); "
            f"screen {capture.width}x{capture.height}"
        )
        # State the monitor layout ALWAYS, not only when there are several.
        # "How many displays do I have?" is a question the model cannot answer
        # from pixels, and staying silent on the single-monitor case taught it to
        # go shell out to PowerShell for something we already know — observed in
        # a real session. One extra clause is far cheaper than that detour, and
        # it also makes a Win+P switch or an unplugged monitor visible at once.
        count = len(capture.displays)
        # Win+P "Duplicate" mirrors one image onto several panels, so every
        # panel reports the SAME rect. Saying "2 monitors" with two identical
        # pixel ranges would invite a hunt for a second addressable area that
        # does not exist — in Duplicate there is only one set of coordinates.
        mirrored = count > 1 and len({
            (d.pixel_x, d.pixel_y, d.pixel_width, d.pixel_height)
            for d in capture.displays
        }) == 1
        if mirrored:
            names = ", ".join(d.name for d in capture.displays)
            summary += (
                f"; {count} monitors MIRRORED (Win+P Duplicate: {names}) — they"
                " show the same image, so there is only one coordinate space"
            )
        else:
            panels = "; ".join(
                f"{d.name} {d.width}x{d.height} at image x="
                f"{d.pixel_x}..{d.pixel_x + d.pixel_width - 1}"
                f" y={d.pixel_y}..{d.pixel_y + d.pixel_height - 1}"
                f"{' (primary)' if d.is_primary else ''}"
                for d in capture.displays
            )
            summary += (
                f"; {count} monitor{'s' if count != 1 else ''} in this image"
                f" — {panels}"
            )
            if count > 1:
                summary += (
                    ". Coordinates are pixels of THIS whole image, so a target"
                    " on a secondary screen is addressed by its pixel here"
                )
    details: dict[str, Any] = {
        "width": capture.width,
        "height": capture.height,
        "backend": capture.backend,
        "capture_permission": capture.capture_permission,
        "input_permission": capture.input_permission,
        "displays": [d.to_dict() for d in capture.displays],
        "actions": action_types,
        "audience": audience,
        "frame": "window" if window_frame else "desktop",
    }

    # Exact desktop-icon geometry, straight from the shell. A model can only
    # ESTIMATE a small target's centre from a screenshot, and a desktop icon's
    # grab box is ~114x76 px: an estimate off by a few dozen pixels lands on
    # empty desktop, where Explorer rubber-band SELECTS instead of dragging — the
    # failure looks like "drag is broken" when the gesture was fine and only the
    # start point was wrong. Handing over the true rectangles removes the guess.
    # Cheap (a few hundred characters) next to the screenshot's vision tokens,
    # and empty on any non-Windows / non-classic-desktop setup.
    #
    # SCREEN-space only. On a window frame the image origin is the window, so
    # mixing screen coordinates into the same result invites exactly the
    # confusion that made a real session click outside its target window.
    icons = [] if window_frame else _list_desktop_icons()
    if icons:
        details["desktop_icons"] = icons
        listed = "; ".join(
            f"{i['name']}=({i['center_x']},{i['center_y']})" for i in icons
        )
        summary += (
            "\nexact desktop icon centres (screen coordinates — use directly "
            f"instead of estimating from the image): {listed}"
        )

    # Which window has focus, and where it sits. The model cannot infer this
    # from pixels: two windows can look identical in a screenshot while only one
    # accepts input (the Explorer desktop in particular ignores input unless it
    # is foreground). Only the FOREGROUND window goes in the message — a full
    # window list is mostly idle background apps and would bury the useful
    # lines; callers that want it read ``details["windows"]``.
    #
    # The window ID is included because it is the handle for the precise paths:
    # window-scoped capture, a scoped ui_snapshot, and background delivery all
    # need it, and the model has no other way to obtain one.
    windows = _describe_windows()
    if windows:
        details["windows"] = windows
        fg = next((w for w in windows if w["is_foreground"]), None)
        if fg is not None:
            summary += (
                f"\nforeground window (only this one receives foreground "
                f"input): {fg['title']!r} ({fg['class_name']}) "
                f"at ({fg['x']},{fg['y']}) {fg['width']}x{fg['height']}"
                f", window={fg.get('hwnd', 0)}"
            )
        others = [w for w in windows if not w["is_foreground"]][:6]
        if others:
            listed = "; ".join(
                f"{w['title'][:40]!r} window={w.get('hwnd', 0)}" for w in others
            )
            summary += (
                f"\nother windows (pass window=<id> to capture, snapshot or "
                f"send input without raising them): {listed}"
            )
        summary += f"\n{len(windows)} visible windows total"
    # Screen-space too; suppressed on a window frame for the same reason.
    bar = None if window_frame else _taskbar_rect()
    if bar is not None:
        details["taskbar"] = bar
        summary += (
            f"\ntaskbar occupies ({bar['x']},{bar['y']}) "
            f"{bar['width']}x{bar['height']} (screen coordinates)"
        )

    # Taskbar button centres by accessible NAME. These buttons are ~66 px wide
    # and look nearly identical at screenshot scale, so a pixel estimate lands
    # on the neighbouring app (observed: aiming for Notepad++, hitting Chrome one
    # button over). The names say exactly what each one is, making name ->
    # coordinate the reliable interface for "open/focus this app", "Start", and
    # "Search".
    buttons = [] if window_frame else _taskbar_buttons()
    if buttons:
        details["taskbar_buttons"] = buttons
        listed = "; ".join(
            f"{b['name']}=({b['center_x']},{b['center_y']})" for b in buttons
        )
        summary += f"\ntaskbar buttons (click these centres): {listed}"

    # The accessibility tree, when a ui_snapshot ran. Placed LAST because it is
    # the largest and most specific block: the model reads the geometry above to
    # orient itself, then works from named controls with exact ref handles. This
    # is the payload that replaces pixel estimation, so it goes in the message
    # rather than only in ``details``.
    if capture.ax_text:
        details["ax_text"] = capture.ax_text
        summary += (
            "\n\n" + capture.ax_text + "\n"
            "act on these by ref, e.g. {\"type\":\"click\",\"ref\":\"e7\"} — "
            "the exact live position is used, so do not convert them to pixels. "
            "Refs expire on the next ui_snapshot."
        )

    image_url: str | None = None
    if save_screenshot is not None:
        try:
            image_url = await save_screenshot(b64, conversation_id, message_id)
        except Exception:  # noqa: BLE001 — degrade to text-only envelope
            image_url = None

    feed_model = audience in ("model", "both")

    if image_url:
        # The screenshot URL is ALWAYS embedded in ``message`` so the tool card
        # (ToolExecPanel) can render the thumbnail regardless of audience, and
        # it is also exposed in ``details`` for the WeChat/Feishu push path. The
        # EMBED FORM decides model visibility: ``![screenshot](url)`` markdown
        # (model/both) is what the vision-block resolver keys off, so the model
        # sees the pixels; the ``<!--screenshot:url-->`` comment (user) carries
        # NO markdown ref, so the shot never enters the model context (no token
        # cost) yet the card still displays it.
        details["screenshot_url"] = image_url
        if feed_model:
            message = f"{summary}\n\n![screenshot]({image_url})"
        else:
            # user audience: expose the URL in a NON-markdown marker so the
            # tool card can still render the thumbnail, while the vision-block
            # resolver (keys off ``![](url)`` markdown) sees no ref and the
            # pixels never enter the model context (no token cost). The HTML
            # comment is invisible in rendered text.
            message = (
                f"{summary}; screenshot shown to the user "
                f"(not fed to the model per audience=user)"
                f"<!--screenshot:{image_url}-->"
            )
    else:
        # No saver wired (or it failed): still expose the bytes in details
        # so callers / smoke scripts can decode; the model sees the text.
        message = summary
        details["image_base64"] = b64
        details["image_mime"] = "image/png"

    return {
        "ok": True,
        "message": message,
        "details": details,
    }
