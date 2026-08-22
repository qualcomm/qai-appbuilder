# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire the ``web_search`` tool into chat.

Registers the conditional ``web_search`` tool onto the chat-side
:class:`RegistryBackedToolInvocation` registry so an LLM-emitted ``tool_call``
resolves to a REAL search (via the
:class:`~qai.platform.web_search.SearchProviderRegistry`) instead of
falling through to ``chat.tool_not_registered``.

Why a separate bridge (mirrors ``_chat_appbuilder_tool_bridge``)
----------------------------------------------------------------
``web_search`` is conditional. The base
``register_ai_coding_tools_into_chat`` (``_chat_tool_bridge``) calls
``build_default_tool_handlers`` WITHOUT a ``search_registry``, so it
deliberately does not register ``web_search`` (the conditional gate). This
bridge is the dedicated post-build hook that adds it — exactly like
``register_appbuilder_tools_into_chat`` adds the conditional App Builder tools
— and is invoked whenever the DI seam yields a (non-empty) registry, which now
happens on BOTH editions (the ``web`` provider ships externally; CEBot is
internal-only).

Cross-context discipline: lives in ``apps/api`` (the only layer allowed to
compose contexts). It reuses the ai_coding ``tool_web_search`` handler + the
``TOOL_SCHEMAS["web_search"]`` schema (both already in ``qai.ai_coding``) and
the ``render_tool_result_text`` projection, so neither ``qai.chat`` nor
``qai.ai_coding`` learns about ``qai.platform.web_search``. The handler
speaks to the registry purely through its ``search(...)`` duck-type.
"""

from __future__ import annotations

from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest, ToolInvocationResult
from qai.platform.logging import get_logger

__all__ = ["register_web_search_tool_into_chat"]

_log = get_logger(__name__)


#: Provider ids for the two search sources. ``web`` is the multi-engine public
#: provider (always present); ``cebot`` is the internal RAG (internal edition
#: only). Kept as module constants so the routing logic reads declaratively.
_PROVIDER_CEBOT = "cebot"
_PROVIDER_WEB = "web"

#: ``forge_config.chat.search.mode`` values. ``web_only`` is the DEFAULT for a
#: fresh install (see :func:`_resolve_search_plan` for why).
_MODE_CEBOT_ONLY = "cebot_only"
_MODE_WEB_ONLY = "web_only"
_MODE_BOTH = "both"


def _resolve_search_plan(
    args: dict[str, Any], cfg: dict[str, Any] | None
) -> list[str | None]:
    """Return the ordered list of provider ids to query for this call.

    ``cfg`` is the live ``forge_config.chat.search`` block (or ``None`` when the
    user has never saved the setting). The DEFAULT — a missing/empty cfg or an
    unrecognised ``mode`` — is ``web_only``.

    Why web-only by default: CEBot is an internal knowledge index, so on a
    public-information question it contributes a *stale* answer rather than no
    answer (observed: it asserted the 2025 Nobel Physics prize "has not yet been
    announced" and offered the 2024 laureates, while the web engines had already
    returned the official nobelprize.org press release). Because the merge
    concatenates both providers, that stale text landed in the same result set as
    the correct pages and read as a contradiction. Users who want the internal
    index can still select it in Settings → Search.

    * an explicit ``args['provider']`` (set by the model) always wins → that one
      provider only;
    * ``mode == "cebot_only"`` → ``[cebot]``;
    * ``mode == "web_only"`` / missing / unknown → ``[web]``;
    * ``mode == "both"`` → both sources, in ``default_provider``-first order.
      ``auto`` and any other value order web-first.

    Returned ids are filtered to those the registry actually has by the caller;
    a plan may name ``cebot`` on an external build where it is absent, and the
    caller simply skips it.
    """
    explicit = args.get("provider")
    if isinstance(explicit, str) and explicit:
        return [explicit]
    mode = (cfg or {}).get("mode")
    if mode == _MODE_CEBOT_ONLY:
        return [_PROVIDER_CEBOT]
    if mode == _MODE_BOTH:
        default_provider = (cfg or {}).get("default_provider")
        if default_provider == _PROVIDER_CEBOT:
            return [_PROVIDER_CEBOT, _PROVIDER_WEB]
        return [_PROVIDER_WEB, _PROVIDER_CEBOT]
    # web_only (explicit, missing, or unknown) → the multi-engine public search.
    return [_PROVIDER_WEB]


def _merge_tool_results(
    per_provider: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge several ``tool_web_search`` result envelopes into one.

    ``per_provider`` is ``[(provider_id, envelope), ...]`` in query order. Only
    successful envelopes (``ok`` truthy with a ``results`` list) contribute
    rows; rows are concatenated in order and de-duplicated by ``url`` (first
    occurrence wins, so the higher-priority provider's copy is kept). ``source``
    is preserved from each row (the provider tags it).

    If NO provider produced a usable result, the LAST envelope is returned
    verbatim so a failure envelope (e.g. a TLS-distrust code) still surfaces.
    """
    merged_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    providers_used: list[str] = []
    any_ok = False
    for provider_id, env in per_provider:
        if not (isinstance(env, dict) and env.get("ok") and isinstance(env.get("results"), list)):
            continue
        any_ok = True
        providers_used.append(provider_id)
        for row in env["results"]:
            url = row.get("url") if isinstance(row, dict) else None
            key = url if isinstance(url, str) and url else object()
            if isinstance(key, str):
                if key in seen:
                    continue
                seen.add(key)
            merged_rows.append(row)
    if not any_ok:
        return per_provider[-1][1] if per_provider else {"ok": False, "results": []}
    label = "+".join(providers_used)
    return {
        "ok": True,
        "provider": label,
        "count": len(merged_rows),
        "results": merged_rows,
        "text": f"web_search ok ({len(merged_rows)} result(s), providers={label})",
    }



def register_web_search_tool_into_chat(
    *,
    tools: RegistryBackedToolInvocation,
    search_registry: Any,
    search_config_reader: Any | None = None,
    file_guard: Any | None = None,  # retained for caller BC (legacy no-op)
) -> tuple[str, ...]:
    """Register ``web_search`` on the chat registry, backed by ``search_registry``.

    ``search_config_reader`` (optional) is a zero-arg callable returning the live
    ``forge_config.chat.search`` dict (``{mode, default_provider}``); it is
    re-read on every tool call so a Settings-panel search-source switch takes
    effect on the NEXT turn WITHOUT a restart. Absent it, the registry default
    provider is used (legacy behaviour).

    Returns the names registered (``("web_search",)`` on success, ``()`` when
    the tools port is not the registry-backed adapter, no ``search_registry``
    was supplied, or the ai_coding handler/schema cannot be imported).
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()
    if search_registry is None:
        return ()

    try:
        from qai.ai_coding.infrastructure.tools.handlers import (
            TOOL_SCHEMAS,
            tool_web_search,
        )
    except Exception:  # noqa: BLE001 — best-effort cross-context wiring
        return ()

    schema = (
        TOOL_SCHEMAS.get("web_search") if isinstance(TOOL_SCHEMAS, dict) else None
    )

    async def _read_search_cfg() -> dict[str, Any] | None:
        if search_config_reader is None:
            return None
        try:
            import asyncio

            result = search_config_reader()
            # Support both sync and async readers for backwards compat.
            if asyncio.iscoroutine(result):
                cfg = await result
            else:
                cfg = result
        except Exception:  # noqa: BLE001 — routing policy must never break search
            return None
        return cfg if isinstance(cfg, dict) else None

    async def _chat_web_search(request: ToolInvocationRequest) -> Any:
        base_args = dict(request.arguments or {})
        cfg = await _read_search_cfg()
        plan = _resolve_search_plan(base_args, cfg)
        # Keep only providers the registry actually has (e.g. ``cebot`` is
        # absent on an external edition); preserve plan order.
        available = set(search_registry.provider_ids())
        providers = [p for p in plan if p in available]
        if not providers:
            # Nothing in the plan is registered — fall back to the registry
            # default so the tool still runs (extreme/misconfig case).
            providers = [None]
        _log.info(
            "chat.web_search.route",
            extra={
                "mode": (cfg or {}).get("mode"),
                "default_provider": (cfg or {}).get("default_provider"),
                "providers": [p or "<registry-default>" for p in providers],
            },
        )

        # Query each planned provider (both sources on the default ``both``
        # mode) and merge. A per-provider failure does not sink the others.
        per_provider: list[tuple[str, dict[str, Any]]] = []
        for provider_id in providers:
            call_args = dict(base_args)
            if provider_id is not None:
                call_args["provider"] = provider_id
            _log.info(
                "chat.web_search.calling_provider",
                extra={"provider_id": provider_id, "call_args_keys": list(call_args.keys())},
            )
            env = await tool_web_search(call_args, search_registry=search_registry)
            result_count = len(env.get("results", [])) if isinstance(env, dict) else 0
            _log.info(
                "chat.web_search.provider_result",
                extra={
                    "provider_id": provider_id,
                    "ok": env.get("ok") if isinstance(env, dict) else None,
                    "result_count": result_count,
                    "error": env.get("error") if isinstance(env, dict) else str(env)[:200],
                },
            )
            per_provider.append((provider_id or "default", env))

        result = (
            per_provider[0][1]
            if len(per_provider) == 1
            else _merge_tool_results(per_provider)
        )
        rendered = render_tool_result_text_with_hints(result)
        # A user-fixable failure envelope (e.g. ``web_search`` behind an
        # enterprise TLS gateway returns ``{ok: False, error_code:
        # "chat.llm.tls_cert_untrusted", message}``) must carry its
        # ``error_code`` through to the ``tool_result`` frame so the chat tool
        # card can render the "disable TLS verification and retry" affordance.
        # (On a merged both-mode call this only triggers when EVERY provider
        # failed — ``_merge_tool_results`` returns the last failure envelope.)
        if isinstance(result, dict) and result.get("ok") is False:
            code = result.get("error_code")
            return ToolInvocationResult(
                tool_name=request.tool_name,
                ok=False,
                result=rendered,
                error_code=code if isinstance(code, str) and code else None,
                error_message=(
                    result.get("message")
                    if isinstance(result.get("message"), str)
                    else None
                ),
            )
        return rendered

    try:
        tools.register("web_search", _chat_web_search, schema=schema)
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning("chat.web_search_tool.register_failed", exc_info=True)
        return ()
    return ("web_search",)
