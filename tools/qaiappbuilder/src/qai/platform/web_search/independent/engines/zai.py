# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Z.AI Web Search Prime engine (keyed HTTP API over remote MCP).

Z.AI exposes web search not as a plain REST endpoint but as a **remote MCP
server** at ``https://api.z.ai/api/mcp/web_search_prime/mcp``. Every call is a
JSON-RPC 2.0 POST carrying a bearer token, and the reference implementation's three-step handshake
is
reproduced verbatim:

1. ``initialize`` — protocol version ``2025-03-26`` plus ``clientInfo``; the
   response's ``Mcp-Session-Id`` header is threaded through the rest of the
   conversation.
2. ``notifications/initialized`` — a JSON-RPC *notification* (no ``id`` member,
   so no response is expected or read).
3. ``tools/call`` — ``{"name": "web_search_prime", "arguments": {...}}``.

Responses may arrive either as plain JSON or as ``text/event-stream``; both are
accepted (``Accept: application/json, text/event-stream``) and
:func:`_parse_mcp_response` decodes SSE by taking the last ``data:`` line that
parses as JSON, exactly as the reference implementation does.

The tool's argument name has drifted upstream, so the reference implementation probes three shapes
in
order — ``{query, count}``, ``{search_query, count}``, then
``{search_query, search_engine: "search-prime", count}`` — retrying only when
the failure looks like an argument-shape rejection: a bare HTTP 400, or a
message mentioning an invalid/unknown argument. That ladder is preserved.

Recency is not supported: the ``web_search_prime`` tool arguments carry no
time-window field, so ``EngineQuery.recency`` is ignored silently.

A JSON-RPC transport that succeeds at the HTTP layer can still carry a refusal
in the body (``{"error": {...}}``, ``{"success": false, ...}``, or a tool result
with ``isError: true``) — all arrive as **HTTP 200**. Those raise rather than
mapping to an empty list, so the aggregator never mistakes a refusal for
"legitimately found nothing".

The same rule covers a subtler case: a tool result whose ``content[].text`` is
plain prose (a quota notice, say) with no ``search_result`` container anywhere.
An *empty* container is a legitimate zero and returns ``[]``; a *missing* one
means the payload was never a search result, and raises.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.errors import (
    EngineAuthError,
    EngineBlockedError,
    EngineError,
    EngineHttpError,
)
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["ZaiEngine"]

_ENGINE_ID = "zai"
_ENDPOINT = "https://api.z.ai/api/mcp/web_search_prime/mcp"
_TOOL_NAME = "web_search_prime"

_MCP_PROTOCOL_VERSION = "2025-03-26"
#: MCP ``clientInfo`` is a self-reported identity, not a protocol-reserved value,
#: so it names THIS product. It is the only field in this request that leaves our
#: process carrying a product name — an upstream operator reading their logs
#: should see who is actually calling.
_MCP_CLIENT_INFO = {"name": "qai-model-builder", "version": "1.0.0"}

_DEFAULT_COUNT = 10

_SSE_DATA_PREFIX = "data:"

# Substrings that mark an upstream failure as an argument-shape rejection,
# i.e. worth retrying with the next argument spelling (reference parity).
_ARG_ERROR_MARKERS = ("invalid", "argument", "search_query", "query")

# the reference implementation treats a bare HTTP 400 as an argument-shape rejection regardless of
# text,
# because the gateway often rejects an unknown argument name with no detail
# (the reference's zai.ts:277).
_HTTP_BAD_REQUEST = 400


def _as_str(value: Any) -> str:
    """Return ``value`` trimmed when it is a non-blank string, else ``""``."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _parse_mcp_response(raw_text: str) -> Any:
    """Decode an MCP response body that may be SSE or plain JSON.

    Collects every ``data:`` line that parses as JSON and returns the **last**
    message (the JSON-RPC response trailing any progress notifications). Falls
    back to parsing the whole body as JSON when there are no SSE frames.
    """
    messages: list[Any] = []
    for line in raw_text.split("\n"):
        trimmed = line.strip()
        if not trimmed.startswith(_SSE_DATA_PREFIX):
            continue
        data = trimmed[len(_SSE_DATA_PREFIX) :].strip()
        if not data:
            continue
        try:
            messages.append(json.loads(data))
        except json.JSONDecodeError:
            # Ignore non-JSON data events (keep-alives, comments).
            continue

    if not messages:
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise EngineBlockedError(
                _ENGINE_ID, "failed to parse the Z.AI MCP response body"
            ) from exc

    return messages[-1]


def _read_jsonrpc_payload(parsed: Any) -> dict[str, Any]:
    """Validate a decoded MCP message and return its JSON-RPC envelope.

    Raises on both flavours of upstream refusal that arrive as HTTP 200: Z.AI's
    plain ``{"success": false, "msg": ...}`` envelope and a JSON-RPC
    ``{"error": {...}}`` object.
    """
    if not isinstance(parsed, dict):
        raise EngineBlockedError(
            _ENGINE_ID, "Z.AI MCP response was not a JSON-RPC object"
        )

    # Z.AI's gateway rejects some requests with its own (non JSON-RPC) envelope.
    direct_message = (
        _as_str(parsed.get("msg"))
        or _as_str(parsed.get("message"))
        or _as_str(parsed.get("error_message"))
    )
    if parsed.get("success") is False and direct_message:
        code = parsed.get("code")
        suffix = f" ({code})" if isinstance(code, int) else ""
        raise EngineBlockedError(
            _ENGINE_ID, f"Z.AI API error{suffix}: {direct_message}"
        )

    error = parsed.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        suffix = f" ({code})" if isinstance(code, int) else ""
        message = _as_str(error.get("message")) or "Unknown error"
        raise _classify_tool_failure(f"Z.AI MCP error{suffix}: {message}")

    return parsed


def _classify_tool_failure(message: str) -> EngineError:
    """Map an upstream JSON-RPC / tool failure message onto our taxonomy.

    Credential problems must reach the user as :class:`EngineAuthError` even
    when the transport answered 200, because only fixing the key clears them.
    """
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in ("unauthor", "api key", "apikey", "token", "forbidden", "credential")
    ):
        return EngineAuthError(_ENGINE_ID, message)
    if any(marker in lowered for marker in ("quota", "credits", "insufficient")):
        return EngineAuthError(_ENGINE_ID, message)
    return EngineBlockedError(_ENGINE_ID, message)


def _search_results(value: Any) -> list[Any] | None:
    """Return the result list one candidate carries, or ``None`` if it has none.

    ``None`` and ``[]`` are deliberately distinct: an empty *container* is a
    legitimate zero-result search, whereas no container at all means the payload
    was never a search result (prose, a status envelope, a quota notice).
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("search_result", "results"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return None


def _extract_payload(raw_result: Any) -> tuple[list[Any] | None, str]:
    """Walk every place Z.AI has been observed to put the result list.

    The tool result may hold results directly, under ``structuredContent`` /
    ``data`` / ``result``, or JSON-encoded inside a ``content[].text`` part.

    Returns ``(results, prose)``. ``results`` is ``None`` when no candidate
    carried a result container at all; the accompanying ``prose`` is whatever
    ``content[].text`` said, so the caller can explain the refusal.
    """
    candidates: list[Any] = [raw_result]
    text_parts: list[str] = []

    if isinstance(raw_result, dict):
        for key in ("structuredContent", "data", "result"):
            nested = raw_result.get(key)
            if nested is not None:
                candidates.append(nested)

        content = raw_result.get("content")
        if isinstance(content, list):
            for part in content:
                text = _as_str(part.get("text")) if isinstance(part, dict) else ""
                if not text:
                    continue
                text_parts.append(text)
                try:
                    candidates.append(json.loads(text))
                except json.JSONDecodeError:
                    # Not a JSON payload; keep it as prose for the error message.
                    continue

    prose = "\n\n".join(text_parts).strip()
    # the reference implementation returns the first *non-empty* container (zai.ts:335-345); an
    # empty one
    # is only conclusive once every candidate has been tried.
    saw_container = False
    for candidate in candidates:
        results = _search_results(candidate)
        if results is None:
            continue
        saw_container = True
        if results:
            return results, prose
    return ([] if saw_container else None), prose


class ZaiEngine:
    """Z.AI Web Search Prime over its remote MCP endpoint (JSON-RPC 2.0)."""

    engine_id: str = _ENGINE_ID
    engine_type: EngineType = "http_api"

    __slots__ = ("_credential", "_http")

    def __init__(
        self,
        *,
        credential: str | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        if credential is None:
            raise EngineAuthError(_ENGINE_ID, f"{_ENGINE_ID} requires credential")
        self._credential = credential
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        count = query.count if query.count > 0 else _DEFAULT_COUNT
        # ``recency`` is intentionally unused: web_search_prime accepts no
        # time-window argument (see module docstring).
        raw_result = await self._call_tool_with_fallbacks(query.query, count)
        results, prose = _extract_payload(raw_result)
        if results is None:
            # No result container anywhere in the payload: the tool answered, but
            # not with a search result. Returning ``[]`` would tell the
            # aggregator this engine legitimately found nothing, leaving its
            # health score untouched and never retrying elsewhere.
            raise EngineBlockedError(
                _ENGINE_ID,
                f"web_search_prime answered without any result list: {prose}"
                if prose
                else "web_search_prime returned no result list and no text",
            )
        hits: list[EngineHit] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = _as_str(item.get("link")) or _as_str(item.get("url"))
            if not url:
                continue
            hits.append(
                EngineHit(
                    title=_as_str(item.get("title")) or url,
                    url=url,
                    snippet=_as_str(item.get("content")),
                    rank=len(hits),
                )
            )
            if len(hits) == count:
                break
        return hits

    async def _call_tool_with_fallbacks(self, query: str, count: int) -> Any:
        """Probe the three argument spellings the reference implementation uses, in order.

        Retries only while the failure looks like an argument-shape rejection;
        anything else (auth, rate limit, transport) propagates immediately so a
        real problem is not masked by two more doomed round-trips.
        """
        attempts: list[dict[str, Any]] = [
            {"query": query, "count": count},
            {"search_query": query, "count": count},
            {
                "search_query": query,
                "search_engine": "search-prime",
                "count": count,
            },
        ]

        last_error: EngineError | None = None
        for index, arguments in enumerate(attempts):
            try:
                return await self._call_tool(arguments)
            except EngineAuthError:
                # A rejected credential is not an argument problem; two more
                # doomed round-trips would only delay the real diagnosis.
                raise
            except EngineError as exc:
                last_error = exc
                is_last = index == len(attempts) - 1
                if is_last or not self._looks_like_arg_error(exc):
                    raise
        raise last_error or EngineError(_ENGINE_ID, "Z.AI search failed")

    @staticmethod
    def _looks_like_arg_error(exc: EngineError) -> bool:
        """Mirror the reference implementation's ``looksLikeArgError``: bare 400, or arg-shaped
        wording."""
        if isinstance(exc, EngineHttpError) and exc.status == _HTTP_BAD_REQUEST:
            return True
        lowered = str(exc).lower()
        return any(marker in lowered for marker in _ARG_ERROR_MARKERS)

    async def _call_tool(self, arguments: dict[str, Any]) -> Any:
        """Run the full initialize / initialized / tools-call MCP handshake."""
        initialized, session_id = await self._post(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _MCP_CLIENT_INFO,
            },
            session_id=None,
            expect_response=True,
        )
        if initialized is not None:
            _read_jsonrpc_payload(initialized)

        _, session_id = await self._post(
            "notifications/initialized",
            {},
            session_id=session_id,
            expect_response=False,
        )

        parsed, _ = await self._post(
            "tools/call",
            {"name": _TOOL_NAME, "arguments": arguments},
            session_id=session_id,
            expect_response=True,
        )
        payload = _read_jsonrpc_payload(parsed)
        result = payload.get("result")
        if isinstance(result, dict) and result.get("isError") is True:
            raise _classify_tool_failure(
                self._tool_error_text(result) or "Z.AI MCP tool call failed"
            )
        if "result" in payload:
            return result
        return parsed

    @staticmethod
    def _tool_error_text(result: dict[str, Any]) -> str:
        content = result.get("content")
        if not isinstance(content, list):
            return ""
        texts = [
            _as_str(item.get("text")) for item in content if isinstance(item, dict)
        ]
        return "\n".join(text for text in texts if text).strip()

    async def _post(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str | None,
        expect_response: bool,
    ) -> tuple[Any, str | None]:
        """POST one JSON-RPC message; return ``(decoded_body, session_id)``.

        A notification (``expect_response=False``) omits the ``id`` member per
        JSON-RPC 2.0 and its body is discarded.
        """
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if expect_response:
            body["id"] = str(uuid.uuid4())

        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        next_session_id = response.headers.get("Mcp-Session-Id") or session_id
        if not expect_response:
            return None, next_session_id
        return _parse_mcp_response(response.text), next_session_id
