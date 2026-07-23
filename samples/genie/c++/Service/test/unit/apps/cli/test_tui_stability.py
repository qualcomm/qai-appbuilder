"""Cross-cutting stability tests for the shared persistent shell
(cli-textual-repl revamp, Step 6).

The per-command Textual tests (``test_tui_chat_session.py`` /
``test_tui_build_session.py`` / ``test_tui_app_session.py``) already cover
each command's own dispatcher/turn wiring in isolation. This module instead
drives :class:`apps.cli._tui.app.QaiReplApp` directly with minimal FAKE
``dispatcher_factory``/``run_turn`` closures (no real chat/build/app
business logic) to prove the shared shell's own generic lifecycle — mount,
command menu, turn streaming, two-stage Ctrl+C, and error containment — is
robust independent of any one command's specific integration.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController, SlashDispatcher
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.screens import ExitConfirmScreen
from apps.cli._tui.widgets import ChatTranscript, CommandMenu, ReplInput


def _fake_dispatcher_factory(_app: QaiReplApp) -> SlashDispatcher:
    d = SlashDispatcher()

    async def _ping(_rest: str) -> bool:
        print("pong")
        return True

    async def _exit(_rest: str) -> bool:
        return False

    d.register("ping", "回复 pong", _ping)
    d.register("exit", "退出会话", _exit, aliases=("quit",))
    return d


def _fake_run_turn(*, hang_event: asyncio.Event | None = None, boom: bool = False):
    """Build a minimal ``run_turn`` closure: sleeps briefly, optionally hangs
    on a specific trigger line (until cancelled) or raises, else writes a
    known marker into the transcript via plain ``print`` (redirected to the
    transcript by ``QaiReplApp``'s own ``TranscriptStream``)."""

    async def run_turn(_app: QaiReplApp, line: str) -> None:
        await asyncio.sleep(0.01)
        if hang_event is not None and line == "hang":
            await hang_event.wait()
        if boom:
            raise RuntimeError("run_turn exploded")
        print(f"turn-done:{line}")

    return run_turn


def _app(*, hang_event: asyncio.Event | None = None, boom: bool = False) -> QaiReplApp:
    opts = RenderOptions(color=False, emoji=False)
    return QaiReplApp(
        c=SimpleNamespace(),
        conversation_id="conv-1",
        tab_id="tab-1",
        interrupts=InterruptController(),
        perm_bridge=None,
        opts=opts,
        session_id="conv-1",
        version="9.9.9",
        has_provider=True,
        dispatcher_factory=_fake_dispatcher_factory,
        run_turn=_fake_run_turn(hang_event=hang_event, boom=boom),
    )


def _transcript_text(app: QaiReplApp) -> str:
    log = app.query_one(ChatTranscript)._log
    return "\n".join(strip.text for strip in log.lines)


async def _wait_until_turn_done(pilot, app: QaiReplApp, *, attempts: int = 30) -> None:
    for _ in range(attempts):
        if app._turn_worker is None:
            return
        await pilot.pause()
        await asyncio.sleep(0)


async def test_full_lifecycle_smoke_menu_navigate_select_dispatch():
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "conv-1" in _transcript_text(app)

        menu = pilot.app.query_one(CommandMenu)
        repl_input = pilot.app.query_one(ReplInput)
        assert menu.display is False

        await pilot.press("/")
        assert menu.display is True
        assert {cmd.name for cmd in menu.visible_commands} == {"exit", "ping"}

        await pilot.press("down")
        await pilot.press("enter")
        assert menu.display is False
        assert repl_input.value == "/ping "

        await pilot.press("enter")
        await pilot.pause()

        assert "pong" in _transcript_text(app)
        assert app.is_running
        assert repl_input.value == ""


async def test_natural_language_turn_uses_fake_run_turn_closure():
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "hello shell"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)

        assert "turn-done:hello shell" in _transcript_text(app)
        assert app._turn_worker is None


async def test_ctrl_c_cancels_in_flight_turn_and_input_recovers():
    hang_event = asyncio.Event()
    app = _app(hang_event=hang_event)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "hang"
        await pilot.press("enter")
        await pilot.pause()
        assert app._turn_worker is not None
        assert repl_input.disabled is True

        await pilot.press("ctrl+c")
        await _wait_until_turn_done(pilot, app)

        assert app._turn_worker is None
        assert app.is_running
        assert repl_input.disabled is False

        # Shell stays responsive: a fresh, non-hanging turn still works.
        repl_input.value = "again"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)
        assert "turn-done:again" in _transcript_text(app)


async def test_ctrl_c_two_stage_confirms_then_exits():
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ExitConfirmScreen)

        await pilot.press("ctrl+c")
        for _ in range(10):
            if not app.is_running:
                break
            await pilot.pause()
            await asyncio.sleep(0)
        assert not app.is_running


async def test_run_turn_exception_is_caught_and_shell_survives():
    app = _app(boom=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "hello"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)

        assert app.is_running
        assert app._turn_worker is None
        assert repl_input.disabled is False
        text = _transcript_text(app)
        assert "回合失败" in text
        assert "RuntimeError" in text

        # Shell remains usable after the failure: a follow-up submission is
        # still routed to a new turn (not silently dropped, e.g. by a
        # never-restored input focus after the first failure disabled it).
        repl_input.value = "again"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)
        assert app.is_running
        assert app._turn_worker is None
        assert _transcript_text(app).count("回合失败") == 2
