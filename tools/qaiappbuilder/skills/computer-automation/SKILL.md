---
name: computer-automation
description: Use when working with the `computer` tool — driving the Windows desktop, clicking or dragging anything on screen, opening or focusing an app, or locating a UI element — and especially when a computer action did not do what was expected — a click or drag missed a target that looked right, a drag turned into a selection box, typing went to the wrong place, a shortcut had no effect, or the target cannot be pinpointed from the screenshot.
tags: computer, computer-tool, desktop, automation, windows, coordinates, click, drag, taskbar, ui_snapshot, refs, background-delivery
use_for: Companion reference for the `computer` tool — locating a UI element precisely with ui_snapshot refs, clicking/dragging on the Windows desktop or inside an app window, sending input without stealing the user's focus, opening or focusing an app, and diagnosing a computer action that missed its target or had no visible effect
---

# Computer Automation

**Companion reference for the `computer` tool.** The tool does the acting
(screenshot, `ui_snapshot`, click, drag, type, keypress); this skill covers what
the tool cannot tell you by itself — which targeting mechanism to reach for, and
what to do when an action did not land where it looked like it should.

## When to Use

While driving the `computer` tool:

- You are about to **click, drag or type** and want a target you can trust
  rather than a coordinate estimated off a screenshot.
- You need a control **inside** a window — a button, menu item, list row, or
  text field.
- The **user is working at the machine** and you must not move their mouse or
  steal focus.
- A **shell-level action** (show desktop, launch/focus an app) needs to be
  reliable.

After a `computer` action misbehaved:

- A click or drag missed a target that looked right in the screenshot.
- A drag turned into a rubber-band selection instead of moving something.
- Typing went nowhere, or landed in the wrong application.
- A keyboard shortcut (e.g. `Win+D`) had no visible effect.

## Targeting: Use the Strongest Mechanism Available

Work down this list and stop at the first one that covers your target. Each step
down is measurably less reliable than the one above it.

| # | Mechanism | Use for |
|---|---|---|
| 1 | **`ui_snapshot` → `ref`** | Anything inside a window. Returns named controls with `[ref=eN]`; act with `{"type":"click","ref":"e7"}` and the platform supplies the exact live position. |
| 2 | **Coordinates already in the tool result** | Desktop icon centres, taskbar buttons by app name, window rects. Use the numbers verbatim. |
| 3 | **`window`-relative `x`/`y`** | A spot inside a known window with no addressable control (a canvas, a custom-drawn area). Survives the window being moved. |
| 4 | **Absolute `x`/`y` from the screenshot** | Last resort only — a visual estimate of a small target is the single largest source of silent misses. |

Never convert a `ref` into pixels yourself. The point of a ref is that the
coordinate is resolved at action time, so it stays correct even if the control
scrolled or the window moved since the snapshot.

**Refs expire.** Each `ui_snapshot` supersedes the previous one; a ref from an
earlier snapshot is rejected rather than silently resolving to a different
control. Re-snapshot after the UI changes.

**A minimized window has no usable coordinates.** Windows parks it around
`(-32000,-32000)`, and every control reports a rect measured from there, so
`ui_snapshot` refuses it outright. Restore it first — click its taskbar button
(the tool result lists those centres by app name), then snapshot. The same
applies to plain coordinates: nothing in a minimized window can be clicked.

**A window frame is not the screen.** `{"type":"screenshot","window":<id>}`
returns the WINDOW's pixels; the result says `WINDOW frame WxH` and states where
that window sits on screen. To act on something you see in it, either pass
`window=<id>` with window-relative `x`/`y`, or add the window's screen offset.
Do not treat the frame's width/height as the display size.

## Not Every Control Appears in `ui_snapshot`

`ui_snapshot` reports what the application exposes through accessibility. Some
targets are genuinely absent:

- **Canvas-style content** — a game viewport, a drawing surface, a
  custom-rendered chart. Nothing is exposed because nothing is a control.
- **Images and video** inside a page or document.
- **Anonymous layout containers** are deliberately omitted: they carry no name,
  so a ref on one would tell you nothing and clicking it does nothing useful.

When the control you need is missing, drop to `window`-relative coordinates with
a window-scoped screenshot (`{"type":"screenshot","window":<id>}`), which is
immune to other windows covering the target.

## Sending Input Without Disturbing the User

`delivery:"background"` posts input to a window's message queue: the user's
pointer never moves, focus never changes, and z-order is irrelevant. It needs a
`window` or `ref`.

**It does not work everywhere, and the boundary is sharp:**

| Application kind | Background delivery |
|---|---|
| Classic Win32 controls (`EDIT`, buttons, list views) | Works |
| Scintilla-based editors (Notepad++) | Works |
| **WinUI / XAML apps — including Windows 11's own Notepad** | **Ignored entirely** |
| Games, DirectInput clients | Ignored |

WinUI controls are not message-processing windows, so posted input has nothing
to arrive at — `WM_CHAR`, `WM_SETTEXT` and `EM_REPLACESEL` are all no-ops there.
For those apps:

- **Typing:** use `{"type":"type","text":...,"ref":"eN"}`. With a ref the tool
  sets the control's value through accessibility, which does work on WinUI and
  needs no focus at all. This is the most reliable way to fill a field.
- **Clicking:** use foreground delivery (the default).

Background typing is verified when a ref is available: if the text did not
arrive, the action fails with `InputFailed` rather than reporting a success that
did not happen. Retry without `delivery:"background"`.

## Why a Coordinate Silently Misses

- **A small target's true centre isn't where it looks.** A desktop icon's grab
  box is roughly 114x76 px on a 1440p panel; an estimate off by a few dozen
  pixels lands on empty desktop. On the Explorer desktop that turns a drag into
  a rubber-band selection — which looks like "drag is broken" when the gesture
  was fine and only the start point was wrong.
- **The window under that pixel changed.** Time passes between a screenshot and
  an action. Prefer a `ref` (resolved at action time) or
  `delivery:"background"` (immune to z-order) over re-screenshotting and hoping.
- **The desktop rearranged itself.** "Auto arrange icons" snaps a dropped icon
  back to a grid slot, so a drag can succeed and still look like nothing moved.
  Re-check the icon's position before concluding the drag failed.

## Reliable Drag / Right-Click

- A drag's start point must land exactly on the target. Locate it first
  (`ui_snapshot`, or the result's `desktop_icons`); adding delays or retries
  cannot fix a start point that was never on the target.
- A right-click is `click` with `button:"right"` — there is no separate
  right-click action type.
- `drag` takes absolute or `window`-relative coordinates, not refs: a drag is a
  path, and only its endpoints are controls.

## Windows Shortcuts That Don't Always Reach the Shell

Synthetic key events do not reliably trigger shell-level hotkeys such as
`Win+D` (show desktop). Drive the Shell COM object instead via `exec`
(`(New-Object -ComObject Shell.Application).MinimizeAll()`) — the programmatic
equivalent, independent of hotkey delivery.

## Common Mistakes

- Estimating pixels from the screenshot when `ui_snapshot` would have given an
  exact ref.
- Converting a ref's reported centre into `x`/`y` — pass the ref itself, so the
  position is resolved at action time.
- Reusing a ref after the UI changed; take a fresh `ui_snapshot` first.
- Expecting `delivery:"background"` to work on a WinUI app (Windows 11 Notepad,
  Settings, Photos). Use a `ref` for typing, foreground delivery for clicks.
- Adding sleeps or retries to "fix" a drag that started on the wrong pixel.
- Requesting a full tree (`interactive_only:false`) when the default already
  lists every actionable control — it is far larger for no added reach.
- Assuming a screenshot is a live view; it is a snapshot, and the foreground can
  change before your next action.
