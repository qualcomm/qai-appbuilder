"""Unit tests for ``apps.cli.commands.chat``'s ``/model`` command and the
per-turn ``model_hint`` it feeds into ``StreamChatInput`` (delivery plan
Step 8: "Agent 感" strengthening of the default chat entry point; catalog
selection added in Step 10).

Covers: listing configured providers (empty / populated / use-case error),
listing the remote catalog for a zero-provider session so the user can
CHOOSE which model to activate (by index or literal ``model_id``), and
switching the active hint once providers exist; then verifies
``_stream_turn`` actually threads the switched hint into the backend
request instead of always sending ``None``.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli._repl import InterruptController
from apps.cli.commands import chat as chat_mod
from qai.service_release.domain.value_objects import CatalogModel, ModelHardware


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


def _dispatcher(*, rows=None, error: Exception | None = None, models=None):
    opts = _opts()
    renderer = _renderer(opts)
    c = SimpleNamespace(
        model_catalog=SimpleNamespace(
            list_provider_configs_use_case=_AsyncResult(rows or [], error=error)
        ),
        service_release=SimpleNamespace(
            list_catalog_models_use_case=_AsyncResult(models or [])
        ),
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


class _SequencedAsyncResult:
    """``list_provider_configs_use_case`` stand-in returning successive values.

    Used by the zero-provider ``/model`` tests below: the *first* call (the
    initial listing) must see an empty catalog to reach the activation
    branch; the *second* call (the refresh right after a successful
    activation) must see the newly-registered provider, mirroring what a
    real ``UpdateProviderConfigUseCase`` write would make visible.
    """

    def __init__(self, values: list) -> None:
        self._values = list(values)

    async def execute(self, *args, **kwargs):
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def _catalog_model(model_id: str, *, name: str | None = None) -> CatalogModel:
    return CatalogModel(
        model_id=model_id,
        name=name or model_id,
        hardware=ModelHardware.NPU,
    )


async def test_model_no_args_with_zero_providers_shows_catalog_for_selection(
    monkeypatch, capsys
):
    """Step 10 redesign: bare ``/model`` with zero providers no longer
    auto-activates a hard-coded default — it lists the remote catalog so
    the user can pick (see the two tests below for the pick itself).
    """

    activate_calls: list = []

    async def _activate(_c, _opts, *, model_id=None):
        activate_calls.append(model_id)
        return True

    monkeypatch.setattr(chat_mod, "_activate_local_model", _activate)
    dispatcher = _dispatcher(
        rows=[],
        models=[_catalog_model("qwen2.5-3b"), _catalog_model("llama-3.2-3b")],
    )

    handled, keep = await dispatcher.dispatch("/model")

    assert (handled, keep) == (True, True)
    assert activate_calls == []  # no auto-activation
    out = capsys.readouterr().out
    assert "qwen2.5-3b" in out
    assert "llama-3.2-3b" in out
    assert chat_mod._ID_HOLDER["model_catalog"][0].model_id == "qwen2.5-3b"


async def test_model_with_literal_model_id_and_zero_providers_activates_that_model(
    monkeypatch, capsys
):
    """``/model <model_id>`` (zero providers) activates exactly that model,
    not whatever the old default-pick fallback would have chosen.
    """

    opts = _opts()
    renderer = _renderer(opts)
    c = SimpleNamespace(
        model_catalog=SimpleNamespace(
            list_provider_configs_use_case=_SequencedAsyncResult(
                [[], [_provider_row("local-genie", model_id="llama-3.2-3b")]]
            )
        ),
        service_release=SimpleNamespace(list_catalog_models_use_case=_AsyncResult([])),
    )
    activate_calls: list = []

    async def _activate(_c, _opts, *, model_id=None):
        activate_calls.append(model_id)
        return True

    monkeypatch.setattr(chat_mod, "_activate_local_model", _activate)
    dispatcher = chat_mod._build_dispatcher(c=c, renderer=renderer, opts=opts)

    handled, keep = await dispatcher.dispatch("/model llama-3.2-3b")

    assert (handled, keep) == (True, True)
    assert activate_calls == ["llama-3.2-3b"]
    out = capsys.readouterr().out
    assert "local-genie" in out


async def test_model_with_cached_index_and_zero_providers_resolves_to_model_id(
    monkeypatch, capsys
):
    """``/model <n>`` resolves against the catalog listing a prior bare
    ``/model`` cached (:func:`chat._resolve_catalog_choice`).
    """

    dispatcher = _dispatcher(
        rows=[],
        models=[_catalog_model("qwen2.5-3b"), _catalog_model("llama-3.2-3b")],
    )
    handled, keep = await dispatcher.dispatch("/model")
    assert (handled, keep) == (True, True)
    capsys.readouterr()  # drain the catalog listing output

    activate_calls: list = []

    async def _activate(_c, _opts, *, model_id=None):
        activate_calls.append(model_id)
        return True

    monkeypatch.setattr(chat_mod, "_activate_local_model", _activate)

    handled, keep = await dispatcher.dispatch("/model 2")

    assert (handled, keep) == (True, True)
    assert activate_calls == ["llama-3.2-3b"]


async def test_model_with_arg_and_zero_providers_activation_failure_prints_guidance(
    monkeypatch, capsys
):
    dispatcher = _dispatcher(rows=[], models=[])

    async def _activate(_c, _opts, *, model_id=None):
        return False

    monkeypatch.setattr(chat_mod, "_activate_local_model", _activate)

    handled, keep = await dispatcher.dispatch("/model qwen2.5-3b")

    assert (handled, keep) == (True, True)
    err = capsys.readouterr().err
    assert "当前没有可用的模型" in err


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
