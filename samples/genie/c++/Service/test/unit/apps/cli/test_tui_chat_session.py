"""Headless lifecycle coverage for the persistent
:class:`apps.cli._tui.app.QaiReplApp` (cli-textual-repl revamp, Step 3).

Mounts the real app via Textual's own ``App.run_test()``/``Pilot`` testing
API and drives it through: a natural-language turn streaming into
:class:`~apps.cli._tui.widgets.ChatTranscript`, a slash command rendering
into the transcript, and Ctrl+C cancelling an in-flight turn without
crashing the app. Fakes ``c.chat.stream_chat_use_case`` the same way
``test_chat_model_command.py``'s ``test_stream_turn_threads_model_hint_into_request``
does (a minimal async-generator stand-in), rather than inventing a new
pattern.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.screens import ExitConfirmScreen
from apps.cli._tui.widgets import ChatTranscript, CommandMenu, ReplInput
from apps.cli.commands import chat as chat_mod


class _Frame:
    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


def _stream_chat_c(frames: list, *, hang_event: asyncio.Event | None = None) -> SimpleNamespace:
    class _StreamChatUseCase:
        async def execute(self, request):
            async def _gen():
                for frame in frames:
                    yield frame
                if hang_event is not None:
                    await hang_event.wait()

            return _gen()

    return SimpleNamespace(chat=SimpleNamespace(stream_chat_use_case=_StreamChatUseCase()))


def _app(c: SimpleNamespace) -> QaiReplApp:
    opts = RenderOptions(color=False, emoji=False)
    return QaiReplApp(
        c=c,
        conversation_id="conv-1",
        tab_id="tab-1",
        interrupts=InterruptController(),
        perm_bridge=None,
        opts=opts,
        session_id="conv-1",
        version="9.9.9",
        has_provider=True,
        dispatcher_factory=chat_mod._make_dispatcher_factory(c=c, opts=opts),
        run_turn=chat_mod._make_run_turn(c=c, opts=opts),
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


async def test_natural_language_turn_streams_into_transcript():
    c = _stream_chat_c(
        [_Frame("chunk", {"text": "hello world"}), _Frame("end", {"usage": {}})]
    )
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "hi there"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)

        assert "hello world" in _transcript_text(app)


async def test_slash_command_renders_into_transcript():
    c = SimpleNamespace()
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "/help"
        await pilot.pause()
        pilot.app.query_one(CommandMenu).display = False
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert "可用命令" in _transcript_text(app)


async def test_ctrl_c_cancels_in_flight_turn_without_crashing():
    hang_event = asyncio.Event()
    c = _stream_chat_c([_Frame("chunk", {"text": "partial"})], hang_event=hang_event)
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "hi there"
        await pilot.press("enter")
        await pilot.pause()
        assert app._turn_worker is not None

        await pilot.press("ctrl+c")
        await _wait_until_turn_done(pilot, app)

        assert app._turn_worker is None
        assert app.is_running


async def test_ctrl_c_with_no_turn_shows_exit_confirm_then_second_exits():
    c = SimpleNamespace()
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert any(
            isinstance(screen, ExitConfirmScreen) for screen in pilot.app.screen_stack
        )
        assert isinstance(pilot.app.screen, ExitConfirmScreen)

        await pilot.press("ctrl+c")
        for _ in range(10):
            if not app.is_running:
                break
            await pilot.pause()
            await asyncio.sleep(0)

        assert not app.is_running
