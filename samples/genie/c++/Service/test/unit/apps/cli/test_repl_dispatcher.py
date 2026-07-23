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

from apps.cli._repl import SlashDispatcher, build_slash_completer


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


# ---------------------------------------------------------------------------
# Slash-command completer (delivery plan Step 8: "/" now suggests registered
# commands with their help text, instead of behaving like a plain char).
# ---------------------------------------------------------------------------


def _completions_for(completer, text: str) -> list:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    doc = Document(text=text, cursor_position=len(text))
    return list(completer.get_completions(doc, CompleteEvent()))


def test_build_slash_completer_suggests_registered_commands_with_help():
    dispatcher = SlashDispatcher()

    async def _noop(_rest: str) -> bool:
        return True

    dispatcher.register("model", "查看/切换模型", _noop)
    dispatcher.register("help", "显示全部命令", _noop, aliases=("?",))

    completer = build_slash_completer(dispatcher)
    assert completer is not None

    completions = _completions_for(completer, "/mo")
    texts = {c.text for c in completions}
    assert "/model" in texts
    matched = next(c for c in completions if c.text == "/model")
    assert matched.display_meta_text == "查看/切换模型"


def test_build_slash_completer_with_no_commands_suggests_nothing():
    completer = build_slash_completer(SlashDispatcher())
    assert completer is not None
    assert _completions_for(completer, "/") == []


def test_get_ptk_session_returns_none_on_non_tty(monkeypatch):
    import apps.cli._repl as repl_mod

    monkeypatch.setattr(repl_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(repl_mod, "_PTK_TRIED", False)
    assert repl_mod._get_ptk_session(build_slash_completer(SlashDispatcher())) is None
