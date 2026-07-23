"""Unit tests for ``apps.cli._tui``'s command menu (cli-textual-repl revamp,
Step 2; adapted for Step 3).

Since Step 3, ``QaiInputApp`` (the ephemeral per-input-round app) is gone —
:class:`apps.cli._tui.app.QaiReplApp` now owns the command menu directly for
its entire session lifetime. Drives :class:`apps.cli._tui.widgets.CommandMenu`
standalone (mirroring the throwaway-host pattern from
``test_repl_dispatcher.py``) for the plain parity assertion, and
``QaiReplApp`` itself for the menu's live filter/write-back behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

from textual.app import App, ComposeResult

from apps.cli._render import RenderOptions
from apps.cli._repl import InterruptController, SlashDispatcher
from apps.cli._tui.app import QaiReplApp
from apps.cli._tui.widgets import CommandMenu, ReplInput
from apps.cli.commands import chat as chat_mod


async def _noop(_rest: str) -> bool:
    return True


def _build_dispatcher() -> SlashDispatcher:
    dispatcher = SlashDispatcher()
    dispatcher.register("model", "查看/切换模型", _noop)
    dispatcher.register("help", "显示全部命令", _noop, aliases=("?",))
    dispatcher.register("clear", "开启新会话", _noop)
    return dispatcher


class _MenuHostApp(App):
    """Minimal host so ``CommandMenu`` can be mounted/queried in isolation."""

    def __init__(self, commands) -> None:
        super().__init__()
        self._commands = commands

    def compose(self) -> ComposeResult:
        yield CommandMenu(self._commands)


async def test_command_menu_matches_dispatcher_commands():
    dispatcher = _build_dispatcher()
    commands = dispatcher.commands()

    async with _MenuHostApp(commands).run_test() as pilot:
        menu = pilot.app.query_one(CommandMenu)
        menu_names = {cmd.name for cmd in menu.visible_commands}

    dispatcher_names = {cmd.name for cmd in commands}
    assert menu_names == dispatcher_names

    help_text = dispatcher.render_help()
    for name in dispatcher_names:
        assert f"/{name}" in help_text


def _repl_app() -> QaiReplApp:
    c = SimpleNamespace()
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


async def test_qai_repl_app_menu_matches_its_own_dispatcher():
    """Same parity guarantee ``dispatcher.commands()`` gives ``/help``, now
    sourced from ``QaiReplApp``'s own live dispatcher."""
    app = _repl_app()
    async with app.run_test() as pilot:
        menu = pilot.app.query_one(CommandMenu)
        menu_names = {cmd.name for cmd in menu.visible_commands}
    assert menu_names == {cmd.name for cmd in app.dispatcher.commands()}


async def test_typing_slash_shows_and_narrows_menu():
    app = _repl_app()
    async with app.run_test() as pilot:
        menu = pilot.app.query_one(CommandMenu)
        assert menu.display is False

        await pilot.press("/")
        assert menu.display is True
        assert {cmd.name for cmd in menu.visible_commands} == {
            cmd.name for cmd in app.dispatcher.commands()
        }

        for ch in "he":
            await pilot.press(ch)
        assert {cmd.name for cmd in menu.visible_commands} == {"help"}


async def test_enter_writes_back_then_dispatches():
    app = _repl_app()
    async with app.run_test() as pilot:
        menu = pilot.app.query_one(CommandMenu)
        repl_input = pilot.app.query_one(ReplInput)

        for ch in "/he":
            await pilot.press(ch)
        assert menu.display is True

        await pilot.press("enter")
        assert menu.display is False
        assert repl_input.value == "/help "

        await pilot.press("enter")
        assert app.is_running
        assert repl_input.value == ""
