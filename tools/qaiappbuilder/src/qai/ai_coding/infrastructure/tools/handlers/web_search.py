# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Web-search tool handler (``web_search``).

Resolves a query (and optional ``provider`` id + ``count``) through an injected
search-provider registry and returns a structured, ranked result list. The
registry is the pluggable extension point; its registry + the multi-engine
``web`` provider live in the shared-kernel package ``qai.platform.web_search``
and ship to BOTH editions, so the ``apps/api`` DI seam builds a non-empty
registry internally AND externally → ``web_search`` is available under both
editions. (The intranet ``cebot`` provider remains internal-only, added only
when ``settings.is_internal``.) The tool is registered whenever a
``search_registry`` is injected (see ``registry.build_default_tool_handlers``).

This module deliberately does NOT import any provider package: the handler
speaks to the registry purely through its ``search(...)`` duck-type, so it
never triggers an ImportError merely by importing this handler module.
"""

from __future__ import annotations

from typing import Any

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import _ok
from qai.platform.logging import get_logger

_log = get_logger(__name__)

_DEFAULT_COUNT = 5
_MAX_COUNT = 50

#: TLS-distrust substrings (parity with the LLM path's classifier) matched on
#: an ssl.SSLError message when the concrete exception type is not the specific
#: SSLCertVerificationError subclass.
_TLS_DISTRUST_SUBSTRINGS = (
    "certificate verify failed",
    "self-signed",
    "self signed",
    "unable to get local issuer",
    "unknownissuer",
    "unknown_issuer",
)


def _is_tls_distrust(exc: BaseException) -> bool:
    """True when ``exc``'s cause/context chain carries a cert-distrust error.

    Walks ``__cause__`` and ``__context__`` (with a seen-guard) looking for an
    :class:`ssl.SSLCertVerificationError`, or an :class:`ssl.SSLError` whose
    message matches a known distrust phrase. Mirrors the LLM stream classifier
    so a search SSL failure maps to the same ``chat.llm.tls_cert_untrusted``
    affordance. Does not import the edition package (external-safe).
    """
    import ssl

    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if isinstance(cur, ssl.SSLError):
            reason = str(cur).lower()
            if any(s in reason for s in _TLS_DISTRUST_SUBSTRINGS):
                return True
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
    return False


async def tool_web_search(
    args: dict[str, Any],
    *,
    search_registry: Any | None = None,
) -> dict[str, Any]:
    # ``file_guard`` used to be a parameter for signature parity with the
    # file-touching tool handlers; ``web_search`` performs no filesystem
    # access and the param was always dropped (``_ = file_guard``). Removed
    # (L-Py-5, 2026-07-28) — the tool wiring no longer forwards it.

    if search_registry is None:
        # Defensive: the tool should not be registered at all when no registry
        # is wired (external edition). If it somehow is, fail clearly.
        raise ToolError(
            "web_search: no search provider is configured in this build"
        )

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("web_search: 'query' argument is required")

    count_raw = args.get("count")
    if count_raw is None:
        count = _DEFAULT_COUNT
    else:
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            count = _DEFAULT_COUNT
        if count <= 0:
            count = _DEFAULT_COUNT
        count = min(count, _MAX_COUNT)

    provider_raw = args.get("provider")
    provider = (
        provider_raw if isinstance(provider_raw, str) and provider_raw else None
    )


    # M10 (tool streaming preview): emit a keep-alive so the UI's tool
    # card shows "searching …" instead of a silent freeze while the
    # network call runs (30s+ common for LLM-backed providers).
    from qai.platform.tool_progress import emit_progress
    provider_label_for_hint = provider or "default"
    emit_progress(
        f"searching web ({provider_label_for_hint}) for “{query.strip()[:60]}”…",
        "network",
    )
    try:
        results = await search_registry.search(
            query.strip(), count=count, provider=provider
        )
    except ToolError:
        raise
    except LookupError as exc:
        # Unknown / unregistered provider id — surface the registry's clear
        # "available providers" message rather than a silent empty result.
        raise ToolError(f"web_search: {exc}") from exc
    except Exception as exc:  # network / upstream failures (incl. TLS distrust)
        # SSL certificate distrust (enterprise TLS-intercepting gateway): the
        # engine chain preserves ssl.SSLCertVerificationError. Surface it as a
        # RESULT envelope carrying the SAME error code the LLM path uses
        # (chat.llm.tls_cert_untrusted) so the chat tool card can offer the
        # existing "disable SSL verification + retry" affordance — instead of
        # raising a ToolError whose code registry.py would overwrite. A tool
        # failure does not abort the turn, so this stays a result, not a raise.
        if _is_tls_distrust(exc):
            _log.warning(
                "web_search.tls_distrust",
                extra={"error": str(exc)},
            )
            return {
                "ok": False,
                "error_code": "chat.llm.tls_cert_untrusted",
                "message": (
                    "web_search: 无法建立安全连接——检测到企业代理/网关证书"
                    "未受信任。可在设置中关闭 TLS 校验后重试。"
                ),
            }
        raise ToolError(f"web_search: search failed: {exc}") from exc

    rendered = [
        {
            "title": getattr(r, "title", ""),
            "url": getattr(r, "url", ""),
            "snippet": getattr(r, "snippet", ""),
            "score": getattr(r, "score", None),
            "source": getattr(r, "source", ""),
        }
        for r in results
    ]

    # Surface quota warning (if any) from the independent web provider.
    # The contextvar is set by IndependentSearchProvider._try_keyed_engine()
    # when a usage threshold is crossed.
    quota_warning_data: dict[str, Any] | None = None
    try:
        from qai.platform.web_search.independent.provider import last_quota_warning

        qw = last_quota_warning.get()
        if qw is not None:
            quota_warning_data = {
                "engine_id": qw.engine_id,
                "kind": qw.kind,
                "usage_count": qw.usage_count,
                "monthly_limit": qw.monthly_limit,
                "message": qw.message,
            }
    except Exception:  # noqa: BLE001 — quota is advisory, never break search
        pass

    provider_label = provider or "default"
    envelope = _ok(
        f"web_search ok ({len(rendered)} result(s), provider={provider_label})",
        query=query.strip(),
        provider=provider_label,
        count=len(rendered),
        results=rendered,
    )
    if quota_warning_data is not None:
        envelope["quota_warning"] = quota_warning_data
    return envelope
