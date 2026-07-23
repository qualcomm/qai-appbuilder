"""Unit tests for ``apps.cli._tui.app.QaiReplApp``'s branded banner
(cli-textual-repl revamp, Step 1; behaviour updated for Step 3).

Since Step 3, ``QaiReplApp`` is the persistent whole-session shell (see
``test_tui_chat_session.py``) rather than a splash that exits on
Enter/Escape — the banner is now the first block written into
:class:`~apps.cli._tui.widgets.ChatTranscript` on mount. This module keeps a
lightweight, standalone way to assert just the banner-rendering aspect.
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.widgets import ChatTranscript
from apps.cli.commands import chat as chat_mod


def _app() -> QaiReplApp:
    c = SimpleNamespace()
    opts = RenderOptions(color=False, emoji=False)
    return QaiReplApp(
        c=c,
        conversation_id="conv-123",
        tab_id="tab-1",
        interrupts=InterruptController(),
        perm_bridge=None,
        opts=opts,
        session_id="conv-123",
        version="9.9.9",
        has_provider=True,
        dispatcher_factory=chat_mod._make_dispatcher_factory(c=c, opts=opts),
        run_turn=chat_mod._make_run_turn(c=c, opts=opts),
    )


def _transcript_text(app: QaiReplApp) -> str:
    log = app.query_one(ChatTranscript)._log
    return "\n".join(strip.text for strip in log.lines)


async def test_banner_shown_in_transcript_on_mount():
    async with _app().run_test() as pilot:
        await pilot.pause()
        text = _transcript_text(pilot.app)
        assert "conv-123" in text
        assert "9.9.9" in text


async def test_input_has_focus_on_mount():
    from apps.cli._tui.widgets import ReplInput

    async with _app().run_test() as pilot:
        await pilot.pause()
        assert pilot.app.query_one(ReplInput).has_focus
