"""Unit tests for ``apps.cli.commands.chat``'s slash-command dispatcher and
zero-provider banner hint (delivery plan Phase 2 §Step 4, redesigned §Step 9).

Mirrors ``test_build_repl.py``'s pattern: drives
:func:`apps.cli.commands.chat._build_dispatcher` through the public
``SlashDispatcher.dispatch`` surface and asserts each generic handler
(``/help``/``/history``/``/clear``/``/show``/``/exit``) behaves the same as
its ``commands/build.py`` counterpart; plus dedicated tests for
:func:`apps.cli.commands.chat._print_banner`'s zero-provider hint. Since
Step 9, entering this session never blocks on / auto-triggers local-model
activation (see ``test_chat_local_activation.py``); that is now only ever
reachable interactively via ``/model`` (see ``test_chat_model_command.py``).
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli.commands import chat as chat_mod


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


def _renderer(opts: RenderOptions) -> StreamFrameRenderer:
    return StreamFrameRenderer(opts, out=io.StringIO(), err=io.StringIO())


def _dispatcher(*, c=None, opts=None, renderer=None):
    opts = opts or _opts()
    renderer = renderer or _renderer(opts)
    dispatcher = chat_mod._build_dispatcher(c=c, renderer=renderer, opts=opts)
    return dispatcher, renderer


def test_build_extra_has_no_tool_mode() -> None:
    # See chat.py module docstring ("tool_mode choice"): a non-empty
    # tool_mode string would trigger the backend's degraded "专项任务"
    # feature-mode framing instead of the plain default chat prompt.
    extra = chat_mod.build_extra()
    assert extra == {"tool_mode": None, "tool_params": {}}


async def test_help_lists_registered_commands(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/help")
    assert handled is True
    assert keep is True
    assert "可用命令" in capsys.readouterr().out


async def test_history_unavailable_when_use_case_missing(capsys):
    c = SimpleNamespace(chat=SimpleNamespace())
    dispatcher, _ = _dispatcher(c=c)
    handled, keep = await dispatcher.dispatch("/history")
    assert (handled, keep) == (True, True)
    assert "尚未接通" in capsys.readouterr().out


async def test_exit_returns_keep_running_false():
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/exit")
    assert handled is True
    assert keep is False


async def test_unknown_command_reports_and_keeps_running(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/bogus")
    assert (handled, keep) == (True, True)
    assert "未知命令" in capsys.readouterr().out


async def test_non_slash_line_is_not_handled():
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("hello agent")
    assert (handled, keep) == (False, True)


class _Frame:
    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


async def test_show_with_nothing_folded_prints_message(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/show")
    assert (handled, keep) == (True, True)
    assert "没有可展开的内容" in capsys.readouterr().out


async def test_show_with_non_integer_argument_errors(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/show notanumber")
    assert (handled, keep) == (True, True)
    assert "用法: /show" in capsys.readouterr().err


async def test_show_valid_index_opens_pager(monkeypatch, capsys):
    opts = _opts()
    renderer = _renderer(opts)
    long_text = "\n".join(f"line{i}" for i in range(30))
    renderer.render(_Frame("tool_result", {"result": long_text}))
    capsys.readouterr()  # discard the renderer's own transcript output

    calls = []

    async def _fake_show_pager(text, *, title=""):
        calls.append((text, title))

    monkeypatch.setattr(chat_mod, "show_pager", _fake_show_pager)

    dispatcher, _ = _dispatcher(opts=opts, renderer=renderer)
    handled, keep = await dispatcher.dispatch("/show")
    assert (handled, keep) == (True, True)
    assert len(calls) == 1
    assert calls[0][0] == long_text
    assert renderer.folded(1) == long_text


# ---------------------------------------------------------------------------
# Zero-provider banner hint (Step 9 redesign)
#
# ``_run_chat`` no longer blocks entry or auto-triggers activation on a
# zero-provider session (see test_chat_local_activation.py's
# ``test_run_chat_never_auto_triggers_activation`` for that ordering
# guarantee, and test_chat_model_command.py for the `/model`-triggered
# activation path itself) — the precheck result now only feeds a one-line
# hint into the welcome banner.
# ---------------------------------------------------------------------------


def test_print_banner_shows_hint_when_no_provider(capsys):
    chat_mod._print_banner("conv-1", _opts(), has_provider=False)
    out = capsys.readouterr().out
    assert "尚未配置任何模型" in out
    assert "/model" in out


def test_print_banner_omits_hint_when_provider_present(capsys):
    chat_mod._print_banner("conv-1", _opts(), has_provider=True)
    out = capsys.readouterr().out
    assert "尚未配置任何模型" not in out
