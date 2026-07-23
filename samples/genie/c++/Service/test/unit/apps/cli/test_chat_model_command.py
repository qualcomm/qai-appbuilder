"""Unit tests for ``apps.cli.commands.chat``'s ``/model`` command and the
per-turn ``model_hint`` it feeds into ``StreamChatInput`` (delivery plan
Step 8: "Agent 感" strengthening of the default chat entry point).

Covers: listing configured providers (empty / populated / use-case error)
and switching the active hint, then verifies ``_stream_turn`` actually
threads the switched hint into the backend request instead of always
sending ``None``.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli._repl import InterruptController
from apps.cli.commands import chat as chat_mod


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


def _renderer(opts: RenderOptions) -> StreamFrameRenderer:
    return StreamFrameRenderer(opts, out=io.StringIO(), err=io.StringIO())


class _AsyncResult:
    def __init__(self, value=None, *, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    async def execute(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._value


def _dispatcher(*, rows=None, error: Exception | None = None):
    opts = _opts()
    renderer = _renderer(opts)
    c = SimpleNamespace(
        model_catalog=SimpleNamespace(
            list_provider_configs_use_case=_AsyncResult(rows or [], error=error)
        )
    )
    dispatcher = chat_mod._build_dispatcher(c=c, renderer=renderer, opts=opts)
    return dispatcher


def _provider_row(provider_id: str, *, model_id: str, has_api_key: bool = True) -> dict:
    return {
        "provider_id": provider_id,
        "config": {
            "base_url": f"https://{provider_id}.example/v1",
            "models": [{"model_id": model_id, "name": model_id}],
            "has_api_key": has_api_key,
        },
    }


async def test_model_no_args_lists_configured_providers(capsys):
    dispatcher = _dispatcher(rows=[_provider_row("openai", model_id="gpt-4o")])
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    out = capsys.readouterr().out
    assert "openai" in out
    assert "gpt-4o" in out


async def test_model_no_args_with_zero_providers_prints_hint(capsys):
    dispatcher = _dispatcher(rows=[])
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    assert "尚未配置任何 provider" in capsys.readouterr().out


async def test_model_lookup_failure_reports_error_not_crash(capsys):
    dispatcher = _dispatcher(error=RuntimeError("db down"))
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    assert "读取 provider 列表失败" in capsys.readouterr().err


async def test_model_with_arg_switches_hint(capsys):
    chat_mod._ID_HOLDER["model_hint"] = None
    dispatcher = _dispatcher(rows=[_provider_row("openai", model_id="gpt-4o")])
    handled, keep = await dispatcher.dispatch("/model gpt-4o")
    assert (handled, keep) == (True, True)
    assert chat_mod._ID_HOLDER["model_hint"] == "gpt-4o"
    assert "后续回合将使用模型: gpt-4o" in capsys.readouterr().out


async def test_stream_turn_threads_model_hint_into_request(monkeypatch):
    chat_mod._ID_HOLDER["model_hint"] = "gpt-4o"
    captured: list = []

    class _StreamChatUseCase:
        async def execute(self, request):
            captured.append(request)

            async def _empty():
                return
                yield  # pragma: no cover - makes this an async generator

            return _empty()

    c = SimpleNamespace(chat=SimpleNamespace(stream_chat_use_case=_StreamChatUseCase()))
    opts = _opts()
    renderer = _renderer(opts)

    await chat_mod._stream_turn(
        c=c,
        text="hello",
        conversation_id="conv-1",
        tab_id="tab-1",
        renderer=renderer,
        interrupts=InterruptController(),
        opts=opts,
    )

    assert len(captured) == 1
    assert captured[0].model_hint == "gpt-4o"
