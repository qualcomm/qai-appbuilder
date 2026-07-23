"""Unit tests for ``apps.cli._repl.SlashDispatcher``'s console integration
(cli-render-redesign plan, Step 2).

Registration/dispatch logic is unchanged from before this step; this only
covers the new optional ``console`` constructor argument used for the
"unknown command" message so ``qai build``/``qai app`` can route it through
the shared themed console. Passing no console preserves the original
``sys.stdout``-based behaviour.
"""

from __future__ import annotations

import io

from rich.console import Console

from apps.cli._repl import SlashDispatcher


async def test_unknown_command_without_console_writes_to_stdout(capsys):
    dispatcher = SlashDispatcher()
    handled, keep = await dispatcher.dispatch("/nope")
    assert (handled, keep) == (True, True)
    assert "未知命令" in capsys.readouterr().out


async def test_unknown_command_with_console_uses_it():
    out = io.StringIO()
    console = Console(file=out, no_color=True, force_terminal=False)
    dispatcher = SlashDispatcher(console=console)
    handled, keep = await dispatcher.dispatch("/nope")
    assert (handled, keep) == (True, True)
    assert "未知命令" in out.getvalue()


async def test_dispatch_unchanged_for_registered_command():
    dispatcher = SlashDispatcher()

    async def _handler(rest: str) -> bool:
        return bool(rest)

    dispatcher.register("greet", "say hi", _handler)
    handled, keep = await dispatcher.dispatch("/greet world")
    assert handled is True
    assert keep is True

    handled, keep = await dispatcher.dispatch("/greet")
    assert handled is True
    assert keep is False
