"""Headless lifecycle coverage for the persistent
:class:`apps.cli._tui.app.QaiReplApp` hosting ``qai build``'s Model Builder
session (cli-textual-repl revamp, Step 5 — generalizing the shell + migrating
``qai build`` onto it).

Mirrors ``test_tui_chat_session.py``'s structure/fakes: mounts the real app
via Textual's own ``App.run_test()``/``Pilot`` testing API and drives it
through a natural-language turn streaming into
:class:`~apps.cli._tui.widgets.ChatTranscript`, a Model-Builder-specific slash
command rendering into the transcript, ``/show`` opening
:class:`~apps.cli._tui.screens.FoldedContentScreen`, and Ctrl+C cancelling an
in-flight turn without crashing the app. Fakes ``c.chat.stream_chat_use_case``
the same minimal async-generator stand-in ``test_tui_chat_session.py``/
``test_chat_model_command.py`` use, rather than inventing a new pattern.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.screens import FoldedContentScreen
from apps.cli._tui.widgets import ChatTranscript, CommandMenu, ReplInput
from apps.cli.commands import build as build_mod


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


def _app(
    c: SimpleNamespace,
    *,
    session: build_mod.BuildSession | None = None,
    model_hint: str | None = None,
) -> QaiReplApp:
    session = session if session is not None else build_mod.BuildSession()
    opts = RenderOptions(color=False, emoji=False)
    interrupts = InterruptController()
    return QaiReplApp(
        c=c,
        conversation_id="conv-1",
        tab_id="tab-1",
        interrupts=interrupts,
        perm_bridge=None,
        opts=opts,
        session_id="conv-1",
        version="9.9.9",
        dispatcher_factory=build_mod._make_dispatcher_factory(
            c=c,
            session=session,
            conversation_id="conv-1",
            tab_id="tab-1",
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        ),
        run_turn=build_mod._make_run_turn(
            c=c, session=session, model_hint=model_hint, opts=opts
        ),
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
        [_Frame("chunk", {"text": "hello builder"}), _Frame("end", {"usage": {}})]
    )
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "convert this model"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)

        assert "hello builder" in _transcript_text(app)


async def test_precision_slash_command_renders_into_transcript():
    c = SimpleNamespace()
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "/precision fp16"
        await pilot.pause()
        pilot.app.query_one(CommandMenu).display = False
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert "已设置量化精度" in _transcript_text(app)


async def test_show_opens_folded_content_screen():
    c = SimpleNamespace()
    session = build_mod.BuildSession()
    app = _app(c, session=session)
    async with app.run_test() as pilot:
        await pilot.pause()

        long_text = "\n".join(f"line{i}" for i in range(30))
        app.renderer.render(_Frame("tool_result", {"result": long_text}))

        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "/show"
        await pilot.pause()
        pilot.app.query_one(CommandMenu).display = False
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert any(
            isinstance(screen, FoldedContentScreen) for screen in pilot.app.screen_stack
        )


async def test_ctrl_c_cancels_in_flight_turn_without_crashing():
    hang_event = asyncio.Event()
    c = _stream_chat_c([_Frame("chunk", {"text": "partial"})], hang_event=hang_event)
    app = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "convert this model"
        await pilot.press("enter")
        await pilot.pause()
        assert app._turn_worker is not None

        await pilot.press("ctrl+c")
        await _wait_until_turn_done(pilot, app)

        assert app._turn_worker is None
        assert app.is_running
