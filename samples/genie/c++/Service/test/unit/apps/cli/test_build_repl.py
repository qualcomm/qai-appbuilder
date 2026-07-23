"""Unit tests for ``apps.cli.commands.build``'s slash-command dispatcher
(cli-render-redesign plan, Step 2).

Drives :func:`apps.cli.commands.build._build_dispatcher` through the public
``SlashDispatcher.dispatch`` surface (mirroring how ``_repl_loop`` calls it)
and asserts each handler still fires under the same conditions, returns the
same ``(handled, keep_running)``/session-state side effects it did before the
Rich-console migration — only "how a line gets printed" changed. Output is
captured via ``capsys`` because the handlers build their console against
``sys.stdout``/``sys.stderr`` directly (matching the pre-migration code,
which also wrote straight to those streams rather than an injected sink).
"""

from __future__ import annotations

import io

from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli._repl import InterruptController
from apps.cli.commands import build as build_mod


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


def _renderer(opts: RenderOptions) -> StreamFrameRenderer:
    return StreamFrameRenderer(opts, out=io.StringIO(), err=io.StringIO())


def _dispatcher(*, c=None, session=None, opts=None, renderer=None):
    opts = opts or _opts()
    session = session or build_mod.BuildSession()
    renderer = renderer or _renderer(opts)
    dispatcher = build_mod._build_dispatcher(
        c=c,
        session=session,
        conversation_id="conv-1",
        tab_id="tab-1",
        renderer=renderer,
        model_hint=None,
        interrupts=InterruptController(),
        opts=opts,
    )
    return dispatcher, session, renderer


async def test_help_lists_registered_commands(capsys):
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/help")
    assert handled is True
    assert keep is True
    assert "可用命令" in capsys.readouterr().out


async def test_model_query_when_unset(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    assert session.model_paths == []
    assert "(未设置)" in capsys.readouterr().out


async def test_model_set_updates_session(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/model a.onnx b.onnx")
    assert (handled, keep) == (True, True)
    assert session.model_paths == ["a.onnx", "b.onnx"]
    assert "已设置模型文件" in capsys.readouterr().out


async def test_precision_invalid_level_rejected(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/precision bogus")
    assert (handled, keep) == (True, True)
    assert session.quant_precision is None
    err = capsys.readouterr().err
    assert "无效精度级别" in err


async def test_precision_valid_level_sets_session(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/precision fp16,w8a8")
    assert (handled, keep) == (True, True)
    assert session.quant_precision == "fp16,w8a8"
    assert "已设置量化精度" in capsys.readouterr().out


async def test_dataset_set_updates_session(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/dataset ./calib")
    assert (handled, keep) == (True, True)
    assert session.dataset_path == "./calib"
    assert "已设置数据集" in capsys.readouterr().out


async def test_params_prints_summary(capsys):
    dispatcher, session, _ = _dispatcher()
    session.quant_precision = "fp16"
    handled, keep = await dispatcher.dispatch("/params")
    assert (handled, keep) == (True, True)
    assert "fp16" in capsys.readouterr().out


async def test_mode_invalid_value_is_ignored(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/mode bogus")
    assert (handled, keep) == (True, True)
    assert session.mode == "interactive"
    assert "当前模式" in capsys.readouterr().out


async def test_mode_valid_value_updates_session(capsys):
    dispatcher, session, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/mode batch")
    assert (handled, keep) == (True, True)
    assert session.mode == "batch"
    assert "已切换模式" in capsys.readouterr().out


async def test_run_without_model_paths_errors(capsys):
    dispatcher, session, _ = _dispatcher()
    assert session.model_paths == []
    handled, keep = await dispatcher.dispatch("/run")
    assert (handled, keep) == (True, True)
    assert "尚未设置模型文件" in capsys.readouterr().err


async def test_retry_without_previous_message_errors(capsys):
    dispatcher, session, _ = _dispatcher()
    assert session.last_user_message is None
    handled, keep = await dispatcher.dispatch("/retry")
    assert (handled, keep) == (True, True)
    assert "没有可重发的上一条消息" in capsys.readouterr().err


async def test_status_workspace_promote_are_placeholders(capsys):
    dispatcher, _, _ = _dispatcher()
    for cmd, expected in (
        ("/status", "运行状态查询尚未接通"),
        ("/workspace", "工作区查看尚未接通"),
        ("/promote", "导出/晋升为 pack 尚未在 CLI 接通"),
    ):
        handled, keep = await dispatcher.dispatch(cmd)
        assert (handled, keep) == (True, True)
        assert expected in capsys.readouterr().out


async def test_exit_returns_keep_running_false():
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/exit")
    assert handled is True
    assert keep is False


async def test_unknown_command_reports_and_keeps_running(capsys):
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/bogus")
    assert (handled, keep) == (True, True)
    assert "未知命令" in capsys.readouterr().out


async def test_non_slash_line_is_not_handled():
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("hello agent")
    assert (handled, keep) == (False, True)


# ---------------------------------------------------------------------------
# /show
# ---------------------------------------------------------------------------


class _Frame:
    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


async def test_show_with_nothing_folded_prints_message(capsys):
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/show")
    assert (handled, keep) == (True, True)
    assert "没有可展开的内容" in capsys.readouterr().out


async def test_show_with_out_of_range_index_prints_message(capsys):
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/show 99")
    assert (handled, keep) == (True, True)
    assert "没有可展开的内容" in capsys.readouterr().out


async def test_show_with_non_integer_argument_errors(capsys):
    dispatcher, _, _ = _dispatcher()
    handled, keep = await dispatcher.dispatch("/show notanumber")
    assert (handled, keep) == (True, True)
    assert "用法: /show" in capsys.readouterr().err


async def test_show_valid_index_opens_pager(monkeypatch, capsys):
    opts = _opts()
    renderer = _renderer(opts)
    long_text = "\n".join(f"line{i}" for i in range(30))
    # Register a fold via the real StreamFrameRenderer path (same mechanism
    # the running REPL uses), then verify /show retrieves it.
    renderer.render(_Frame("tool_result", {"result": long_text}))
    capsys.readouterr()  # discard the renderer's own transcript output

    calls = []

    async def _fake_show_pager(text, *, title=""):
        calls.append((text, title))

    monkeypatch.setattr(build_mod, "show_pager", _fake_show_pager)

    dispatcher, _, _ = _dispatcher(opts=opts, renderer=renderer)
    handled, keep = await dispatcher.dispatch("/show")
    assert (handled, keep) == (True, True)
    assert len(calls) == 1
    assert calls[0][0] == long_text
    assert renderer.folded(1) == long_text


async def test_show_explicit_index_matches_fold(monkeypatch):
    opts = _opts()
    renderer = _renderer(opts)
    long_text = "\n".join(f"line{i}" for i in range(30))
    renderer.render(_Frame("tool_result", {"result": long_text}))

    calls = []

    async def _fake_show_pager(text, *, title=""):
        calls.append(text)

    monkeypatch.setattr(build_mod, "show_pager", _fake_show_pager)

    dispatcher, _, _ = _dispatcher(opts=opts, renderer=renderer)
    handled, keep = await dispatcher.dispatch("/show 1")
    assert (handled, keep) == (True, True)
    assert calls == [long_text]
