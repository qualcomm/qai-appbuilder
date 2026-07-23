"""Unit tests for ``apps.cli.commands.app``'s REPL slash-command dispatcher
(cli-render-redesign plan, Step 2).

Same intent as ``test_build_repl.py``: drive
:func:`apps.cli.commands.app._build_repl_dispatcher` through the public
``SlashDispatcher.dispatch`` surface and assert every handler still fires
under the same conditions / returns the same value / mutates the same
session state — only the printing mechanism changed. ``c`` is a minimal
duck-typed stub since these handlers never need a real container for the
branches exercised here (manifest-less pack, no run history).
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli.commands import app as app_mod


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


def _container() -> SimpleNamespace:
    return SimpleNamespace(
        app_builder=SimpleNamespace(
            get_pack_manifest_use_case=None,
            list_runs_use_case=None,
        )
    )


def _dispatcher(*, c=None, state=None, opts=None):
    opts = opts or _opts()
    c = c or _container()
    state = state or app_mod._AppReplState("whisper-base")
    dispatcher = app_mod._build_repl_dispatcher(c, state, opts)
    return dispatcher, state


async def test_model_query_returns_current_pack(capsys):
    dispatcher, state = _dispatcher()
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    assert state.pack == "whisper-base"
    assert "whisper-base" in capsys.readouterr().out


async def test_model_set_switches_pack_and_resets_variant(capsys):
    dispatcher, state = _dispatcher()
    state.variant = "fast"
    handled, keep = await dispatcher.dispatch("/model other-pack")
    assert (handled, keep) == (True, True)
    assert state.pack == "other-pack"
    assert state.variant is None
    assert "已切换 Pack: other-pack" in capsys.readouterr().out


async def test_variant_set_and_clear(capsys):
    dispatcher, state = _dispatcher()
    handled, keep = await dispatcher.dispatch("/variant fast")
    assert (handled, keep) == (True, True)
    assert state.variant == "fast"
    assert "fast" in capsys.readouterr().out

    handled, keep = await dispatcher.dispatch("/variant")
    assert (handled, keep) == (True, True)
    assert state.variant is None
    assert "(默认)" in capsys.readouterr().out


async def test_param_invalid_assignment_reports_error(capsys):
    dispatcher, state = _dispatcher()
    handled, keep = await dispatcher.dispatch("/param not-a-kv-pair")
    assert (handled, keep) == (True, True)
    assert state.params == {}
    assert "expected key=val" in capsys.readouterr().out


async def test_param_valid_assignment_updates_state(capsys):
    dispatcher, state = _dispatcher()
    handled, keep = await dispatcher.dispatch("/param threshold=0.5")
    assert (handled, keep) == (True, True)
    assert state.params == {"threshold": "0.5"}
    assert "threshold" in capsys.readouterr().out


async def test_params_prints_current_params_and_variant(capsys):
    dispatcher, state = _dispatcher()
    state.params = {"k": "v"}
    state.variant = "fast"
    handled, keep = await dispatcher.dispatch("/params")
    assert (handled, keep) == (True, True)
    out = capsys.readouterr().out
    assert "fast" in out and "k" in out


async def test_examples_with_no_manifest_examples(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/examples")
    assert (handled, keep) == (True, True)
    assert "没有内置示例" in capsys.readouterr().out


async def test_history_unavailable_when_use_case_missing(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/history")
    assert (handled, keep) == (True, True)
    assert "运行历史不可用" in capsys.readouterr().out


async def test_last_with_no_result_reports_message(capsys):
    dispatcher, state = _dispatcher()
    assert state.last_result is None
    handled, keep = await dispatcher.dispatch("/last")
    assert (handled, keep) == (True, True)
    assert "还没有运行结果" in capsys.readouterr().out


async def test_out_with_no_result_reports_message(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/out somewhere.json")
    assert (handled, keep) == (True, True)
    assert "没有可导出的输出" in capsys.readouterr().out


async def test_help_lists_registered_commands(capsys):
    dispatcher, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/help")
    assert (handled, keep) == (True, True)
    assert "可用命令" in capsys.readouterr().out


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
