"""Headless lifecycle coverage for the persistent
:class:`apps.cli._tui.app.QaiReplApp` hosting ``qai app``'s one-shot
pack-run REPL session (cli-textual-repl revamp, Step 5 — migrating the last
of the three REPL commands, ``qai app``, onto the generic shell).

Mirrors ``test_tui_build_session.py``'s structure/fakes: mounts the real app
via Textual's own ``App.run_test()``/``Pilot`` testing API. Unlike chat/build
(a multi-turn agent conversation), one submitted line here drives exactly
one :func:`apps.cli.commands.app._run_once` pack-run call, so the fake stubs
``c.app_builder.run_app_use_case`` the same minimal async-generator pattern
``test_tui_build_session.py``/``test_chat_model_command.py`` use for their
own streaming use cases, rather than inventing a new one. A slash command
(``/params``) and Ctrl+C cancelling an in-flight run are covered the same
way as the build session's tests; the :class:`~apps.cli._tui.widgets.ProgressPanel`
coverage is new (there is no equivalent in chat/build, which have no
one-shot-run progress concept).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.widgets import ChatTranscript, CommandMenu, ProgressPanel, ReplInput
from apps.cli.commands import app as app_mod


class _Frame:
    __slots__ = ("payload",)

    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _container(*, list_runs_use_case=None) -> SimpleNamespace:
    return SimpleNamespace(
        get_pack_manifest_use_case=None,
        list_runs_use_case=list_runs_use_case,
    )


def _run_app_c(frames: list, *, hang_event: asyncio.Event | None = None) -> SimpleNamespace:
    class _RunAppUseCase:
        async def execute(self, *, model_id, inputs):
            async def _gen():
                for frame in frames:
                    yield frame
                if hang_event is not None:
                    await hang_event.wait()

            return _gen()

    return SimpleNamespace(app_builder=SimpleNamespace(
        run_app_use_case=_RunAppUseCase(), **_container().__dict__
    ))


def _app(
    c: SimpleNamespace, *, state: app_mod._AppReplState | None = None
) -> tuple[QaiReplApp, ProgressPanel]:
    state = state if state is not None else app_mod._AppReplState("whisper-base")
    opts = RenderOptions(color=False, emoji=False)
    progress_panel = ProgressPanel()
    app = QaiReplApp(
        c=c,
        conversation_id=None,
        tab_id=None,
        interrupts=InterruptController(),
        perm_bridge=None,
        opts=opts,
        session_id="sess-1",
        version="9.9.9",
        extra_widgets=[progress_panel],
        dispatcher_factory=app_mod._make_dispatcher_factory(c=c, state=state, opts=opts),
        run_turn=app_mod._make_run_turn(
            c=c, state=state, opts=opts, progress_panel=progress_panel
        ),
    )
    return app, progress_panel


def _transcript_text(app: QaiReplApp) -> str:
    log = app.query_one(ChatTranscript)._log
    return "\n".join(strip.text for strip in log.lines)


async def _wait_until_turn_done(pilot, app: QaiReplApp, *, attempts: int = 30) -> None:
    for _ in range(attempts):
        if app._turn_worker is None:
            return
        await pilot.pause()
        await asyncio.sleep(0)


async def test_natural_language_turn_streams_result_into_transcript():
    frames = [
        _Frame({"event": "status", "state": "starting"}),
        _Frame({"event": "result", "output": {"text": "hello pack result"}}),
    ]
    c = _run_app_c(frames)
    app, _panel = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "some input text"
        await pilot.press("enter")
        await _wait_until_turn_done(pilot, app)

        assert "hello pack result" in _transcript_text(app)


async def test_progress_panel_receives_status_and_progress_updates():
    hang_event = asyncio.Event()

    class _RunAppUseCase:
        async def execute(self, *, model_id, inputs):
            async def _gen():
                yield _Frame({"event": "status", "state": "starting"})
                yield _Frame({"event": "progress", "stage": "infer", "percent": 40})
                await hang_event.wait()
                yield _Frame({"event": "result", "output": {"ok": True}})

            return _gen()

    c = SimpleNamespace(app_builder=SimpleNamespace(
        run_app_use_case=_RunAppUseCase(), **_container().__dict__
    ))
    app, panel = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "some input text"
        await pilot.press("enter")
        await pilot.pause()
        await asyncio.sleep(0)
        await pilot.pause()

        assert panel.display is True
        assert panel._bar.progress == 40

        hang_event.set()
        await _wait_until_turn_done(pilot, app)

        assert panel.display is False


async def test_params_slash_command_renders_into_transcript():
    c = SimpleNamespace(app_builder=_container())
    app, _panel = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "/params"
        await pilot.pause()
        pilot.app.query_one(CommandMenu).display = False
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        text = _transcript_text(app)
        assert "当前参数" in text
        assert "默认" in text


async def test_ctrl_c_cancels_in_flight_turn_without_crashing():
    hang_event = asyncio.Event()
    c = _run_app_c([_Frame({"event": "status", "state": "starting"})], hang_event=hang_event)
    app, _panel = _app(c)
    async with app.run_test() as pilot:
        await pilot.pause()
        repl_input = pilot.app.query_one(ReplInput)
        repl_input.value = "some input text"
        await pilot.press("enter")
        await pilot.pause()
        assert app._turn_worker is not None

        await pilot.press("ctrl+c")
        await _wait_until_turn_done(pilot, app)

        assert app._turn_worker is None
        assert app.is_running
