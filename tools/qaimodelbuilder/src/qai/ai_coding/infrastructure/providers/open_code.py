# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OpenCode provider adapter (PR-046, PR-102, PR-OC-real-stream).

Talks to a local OpenCode HTTP server (``http://127.0.0.1:8765`` by
default).

Streaming model (why this overrides ``stream()``)
-------------------------------------------------
OpenCode's HTTP surface is NOT a single "POST a URL → Anthropic-shaped
SSE byte stream" endpoint that the shared :meth:`HttpCodingProviderBase
._real_stream` loop assumes.  Its real conversation lifecycle is:

1. ``POST /session`` (body ``{}``) → ``{"id": "ses_...", ...}`` — create
   the upstream session once, lazily, and cache the ``ses_...`` id on the
   per-session handle (mirrors V1 ``opencode_session_manager
   ._ensure_oc_session`` lazy-create at first send).
2. Subscribe to ``GET /event`` (SSE) AND ``POST /session/{ses_id}/message``
   *concurrently* (2-H5).  The model's reply arrives incrementally over
   the event stream — ``message.part.delta`` text tokens,
   ``message.part.updated`` tool parts, ``step-finish`` token/cost, and a
   terminal ``session.idle`` — so the user sees real streaming instead of
   one post-hoc JSON blob (V1 ``opencode_session_manager._send_and_stream``
   ``:601-758``).

Because the response is an event stream (not an Anthropic SSE byte
stream), this adapter overrides :meth:`stream` and consumes the routed
events via :meth:`_oc_consume_events`, mapping each into a
:class:`CodingStreamFrame` and finishing with an explicit ``END`` that
carries the accumulated token/cost ``usage``.  Errors are surfaced as a
single ``ERROR`` frame carrying the unified :class:`QaiError` envelope
(``{type, code, message, details}``) followed by ``END`` — symmetric
with the Claude Code adapter's error contract.

HTTP is performed via :meth:`_oc_create_session` /
:meth:`_oc_open_event_stream` / :meth:`_oc_send_message`, thin
``httpx``-backed seams that tests monkeypatch so the streaming loop is
exercised without a live OpenCode server (CI parity).

API key lookup
--------------
Local OpenCode deployments often run without authentication, but the
adapter still respects :class:`SecretStore` for parity with the
cloud-backed Claude Code adapter.  An empty-string key means
"unauthenticated" (no ``Authorization`` header sent); a missing
SecretStore entry surfaces ``provider_not_available`` as an ERROR
frame (V1 parity: an unconfigured provider returns a clear error
rather than fake data).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from qai.ai_coding.domain import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    Provider,
    ProviderNotAvailableError,
    StreamFrameKind,
    Workspace,
)
from qai.platform.errors import InfrastructureError, NotFoundError
from qai.platform.persistence.secrets import SecretStore

from .base import HttpCodingProviderBase, ProviderHttpConfig
from .http_transport import HttpTransportPort

__all__ = ["OPEN_CODE_DEFAULT_CONFIG", "OpenCodeProvider"]


# SecretStore service the OC credentials panel writes to (mirrors
# ``qai.ai_coding.application.use_cases.manage_coding_credentials.
# OC_SECRET_SERVICE``).  Hard-coded here — not imported — so the
# infrastructure layer does not depend on the application layer (the
# same trade-off ``claude_code._CC_CRED_SERVICE`` makes for CC).
_OC_CRED_SERVICE = "ai_coding_oc"
# The OC-specific env var checked first.  We deliberately do NOT read
# the shared ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` *env* vars here
# so a CC-only env key never leaks into OC; those names are only honoured
# when stored in OC's own panel credential namespace below.
_OC_API_KEY_ENV = "OPENCODE_API_KEY"
# The credential variable names the OC panel may store, in resolution
# priority order (mirrors ``OC_CREDENTIAL_VARS``'s api-key entries).
_OC_API_KEY_NAMES = ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")

# Fallback ``providerID`` / ``modelID`` sent to OpenCode when the session
# config carries no explicit selection.  OpenCode resolves its own
# configured default provider/model server-side, so these are only a
# best-effort hint — the server may reply using a different model and we
# faithfully map whatever parts it returns (this is OC behaviour, not a
# bug).  ``opencode`` is OpenCode's own self-hosted provider id.
_OC_DEFAULT_PROVIDER_ID = "opencode"
_OC_DEFAULT_MODEL_ID = ""


OPEN_CODE_DEFAULT_CONFIG = ProviderHttpConfig(
    base_url="http://127.0.0.1:8765",
    api_key_service="qai.ai_coding.open_code",
    api_key_name="api_key",
    connect_timeout_s=5.0,
    read_timeout_s=30.0,
)


class OpenCodeProvider(HttpCodingProviderBase):
    """:class:`CodingProviderPort` adapter for the local OpenCode backend."""

    __slots__ = ()

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        config: ProviderHttpConfig | None = None,
        transport: HttpTransportPort | None = None,
    ) -> None:
        super().__init__(
            provider=Provider.OPEN_CODE,
            config=config or OPEN_CODE_DEFAULT_CONFIG,
            secret_store=secret_store,
            transport=transport,
        )

    def _resolve_catalog_api_key(self) -> str | None:
        """Resolve the OC api key from the operator credential namespace.

        Mirrors :meth:`ClaudeCodeProvider._resolve_catalog_api_key`
        (the P1-10 fix): the OC credentials panel writes the key to
        SecretStore ``(ai_coding_oc, OPENCODE_API_KEY)`` (see
        ``manage_coding_credentials.OC_SECRET_SERVICE`` /
        ``OC_CREDENTIAL_VARS``), but the base streaming fallback reads
        the distinct ``(qai.ai_coding.open_code, api_key)`` namespace.
        Without unifying them the panel-saved key is invisible to the
        streaming path → ``provider_not_available``.  Priority: the
        OC-specific ``OPENCODE_API_KEY`` env var > the panel credential
        namespace (api-key vars in order).  Shared env vars
        (``ANTHROPIC_API_KEY`` etc.) are intentionally NOT read from the
        process env so a CC-only env key never leaks into OC.
        """
        env_val = os.environ.get(_OC_API_KEY_ENV)
        if env_val and env_val.strip():
            return env_val.strip()
        for key_name in _OC_API_KEY_NAMES:
            try:
                val = self._secret_store.get(_OC_CRED_SERVICE, key_name)
            except NotFoundError:
                continue
            if val and val.strip():
                return val.strip()
        return None

    def _resolve_api_key_chain(self) -> str | None:
        """Streaming-path key resolution (overrides base default).

        Delegates to :meth:`_resolve_catalog_api_key` so streaming reads
        the same operator credential namespace the OC panel writes,
        symmetric with the CC P1-10 fix.  Returning ``None`` lets the
        base fall back to the legacy ``(qai.ai_coding.open_code,
        api_key)`` streaming namespace, preserving any pre-existing
        operator deployments that wrote a key there directly.
        """
        return self._resolve_catalog_api_key()

    def _build_spawn_payload(
        self,
        *,
        handle: dict[str, Any],
        workspace: Workspace,
    ) -> dict[str, Any]:
        handle["session_endpoint"] = (
            f"{self._config.base_url.rstrip('/')}/sessions"
        )
        return handle

    async def is_available(self) -> bool:
        """Best-effort liveness probe for the local OpenCode service.

        Mirrors V1 ``opencode_session_manager._check_health_endpoint``
        (``backend/ai_coding/opencode_session_manager.py:1046-1066``):
        GET ``{base_url}/global/health`` with a 5s timeout; any
        ``status_code < 500`` is treated as "service is up", and any
        exception (``ConnectionError`` / DNS / ssl / timeout / ImportError)
        collapses into ``False``.

        This probe is **best-effort** and never raises — the folded
        ``/api/{cc|oc}/health`` route consults it via duck-typing in
        :class:`HealthStatusUseCase` to compute the per-provider
        ``ProviderInfo.available`` flag.  When ``httpx`` is not
        importable (minimal install) we conservatively report
        unavailable, matching the offline behaviour of
        :meth:`available_models`.
        """
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError:
            return False

        url = f"{self._config.base_url.rstrip('/')}/global/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                return int(response.status_code) < 500
        except Exception:  # noqa: BLE001 — best-effort; OC may be offline.
            return False

    async def available_models(self) -> list[dict[str, Any]]:
        """Best-effort model catalog from the local OpenCode service.

        Mirrors V1 ``opencode_session_manager.get_providers``: GET
        ``{base_url}/config/providers`` and flatten each provider's
        ``models`` map into ``[{id, name, provider_id: "open_code"}]``.

        The local OpenCode HTTP server is frequently offline (binary
        not installed / not started); in that case — or on any HTTP /
        parse error — we return an empty list rather than raising, so
        the folded ``/health`` response degrades gracefully.
        """
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError:
            return []

        url = f"{self._config.base_url.rstrip('/')}/config/providers"
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_s,
            read=self._config.read_timeout_s,
            write=10.0,
            pool=5.0,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                if response.status_code >= 400:
                    return []
                data = response.json()
        except Exception:  # noqa: BLE001 — best-effort; OC may be offline.
            return []

        return self._parse_providers(data)

    @staticmethod
    def _parse_providers(data: Any) -> list[dict[str, Any]]:
        """Flatten OpenCode ``/config/providers`` payload into model rows.

        OpenCode returns ``{"providers": [{"models": {"<id>": {...}}}]}``
        (the legacy V1 ``get_providers`` shape).  Each provider's
        ``models`` dict maps a model id to a metadata object that may
        carry a human-readable ``name``.  We collapse all providers'
        models into a single flat list tagged with the OpenCode
        provider id, matching :class:`ModelInfo`'s flat-list contract.
        """
        rows: list[dict[str, Any]] = []
        providers: Any = []
        if isinstance(data, dict):
            providers = data.get("providers") or []
        elif isinstance(data, list):
            providers = data
        if not isinstance(providers, list):
            return rows
        for prov in providers:
            if not isinstance(prov, dict):
                continue
            models = prov.get("models")
            if isinstance(models, dict):
                items: Any = list(models.items())
            elif isinstance(models, list):
                items = [
                    (m.get("id"), m) for m in models if isinstance(m, dict)
                ]
            else:
                continue
            for mid, meta in items:
                if not mid:
                    continue
                name = str(mid)
                if isinstance(meta, dict):
                    name = str(meta.get("name") or meta.get("id") or mid)
                rows.append(
                    {
                        "id": str(mid),
                        "name": name,
                        "provider_id": Provider.OPEN_CODE.value,
                    }
                )
        return rows

    # ------------------------------------------------------------------
    # OpenCode real session lifecycle (overrides base SSE loop)
    # ------------------------------------------------------------------
    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        """Consume one OpenCode turn and surface it as stream frames.

        Overrides :meth:`HttpCodingProviderBase.stream` because
        OpenCode's wire shape (``POST /session/{id}/message`` →
        complete ``{"info", "parts"}`` JSON) does not fit the base
        Anthropic-SSE loop.  We:

        1. resolve the api key (missing key ⇒ ``provider_not_available``
           ERROR + END, matching V1's "no fake data" rule);
        2. lazily create the upstream OpenCode session (``POST
           /session``) and cache its ``ses_...`` id on the handle;
        3. ``POST /session/{ses_id}/message`` with the accumulated user
           messages, the resolved provider/model and tool parts;
        4. map each returned ``part`` into a :class:`CodingStreamFrame`
           (``text`` → TEXT, tool parts → TOOL_CALL / TOOL_RESULT) and
           finish with an explicit END.

        The method is an async generator so callers iterate it the
        same way they iterate the base loop's result.
        """
        return self._oc_stream(session_id)

    async def _oc_stream(
        self, session_id: CodingSessionId
    ) -> AsyncIterator[CodingStreamFrame]:
        # 1) api key (empty string = unauthenticated; missing = error).
        try:
            api_key = self._require_api_key()
        except ProviderNotAvailableError as exc:
            yield CodingStreamFrame(
                kind=StreamFrameKind.ERROR,
                payload=exc.to_dict(),
                sequence=self._next_sequence(session_id),
            )
            yield CodingStreamFrame(
                kind=StreamFrameKind.END,
                payload={},
                sequence=self._next_sequence(session_id),
            )
            return

        sent_terminal = False
        try:
            oc_session_id = await self._oc_ensure_session(
                session_id=session_id, api_key=api_key
            )
            body = self._build_message_body(session_id=session_id)
            async for frame in self._oc_consume_events(
                session_id=session_id,
                oc_session_id=oc_session_id,
                body=body,
                api_key=api_key,
            ):
                yield frame
                if frame.kind is StreamFrameKind.END:
                    sent_terminal = True
        except ProviderNotAvailableError as exc:
            yield CodingStreamFrame(
                kind=StreamFrameKind.ERROR,
                payload=exc.to_dict(),
                sequence=self._next_sequence(session_id),
            )
        except InfrastructureError as exc:
            # Already a typed QaiError (e.g. raised by _oc_post_json /
            # _oc_ensure_session) — forward its envelope unchanged.
            yield CodingStreamFrame(
                kind=StreamFrameKind.ERROR,
                payload=exc.to_dict(),
                sequence=self._next_sequence(session_id),
            )
        except Exception as exc:  # noqa: BLE001 — surface every failure typed.
            err = InfrastructureError(
                code="ai_coding.upstream_stream_failed",
                message=str(exc) or type(exc).__name__,
                details={"provider": Provider.OPEN_CODE.value},
            )
            yield CodingStreamFrame(
                kind=StreamFrameKind.ERROR,
                payload=err.to_dict(),
                sequence=self._next_sequence(session_id),
            )
        finally:
            if not sent_terminal:
                yield CodingStreamFrame(
                    kind=StreamFrameKind.END,
                    payload={},
                    sequence=self._next_sequence(session_id),
                )

    async def _oc_consume_events(
        self,
        *,
        session_id: CodingSessionId,
        oc_session_id: str,
        body: dict[str, Any],
        api_key: str,
    ) -> AsyncIterator[CodingStreamFrame]:
        """Drive one OpenCode turn over the ``/event`` SSE stream (2-H5).

        Mirrors V1 ``opencode_session_manager._send_and_stream``
        (``opencode_session_manager.py:601-758``): the message POST and
        the ``GET /event`` SSE subscription run concurrently on
        independent clients; events are routed by ``sessionID`` and
        incrementally mapped to frames, so text arrives as
        ``message.part.delta`` token chunks (real streaming) instead of
        a single post-hoc JSON blob:

        * ``message.part.delta`` (field ``text``) → :data:`TEXT` chunk;
        * ``message.part.updated`` tool part → :data:`TOOL_CALL` on
          ``running`` / :data:`TOOL_RESULT` on ``completed`` /
          :data:`ERROR` on tool error (dedup by call id);
        * ``step-finish`` → accumulate token / cost ``usage`` (carried on
          the terminal frame so the use case folds it into the aggregate);
        * ``session.idle`` (for this session) → terminal END (carries the
          accumulated ``usage``);
        * ``session.error`` (for this session) → ERROR + stop.

        The two seams (:meth:`_oc_open_event_stream` /
        :meth:`_oc_send_message`) are monkeypatched by tests so the loop
        runs without a live OpenCode server (CI parity).  ``usage`` is the
        ONLY payload addition — no new :class:`StreamFrameKind` is
        introduced (§3.1 SSE frame format preserved).
        """
        import asyncio

        url = (
            f"{self._config.base_url.rstrip('/')}"
            f"/session/{oc_session_id}/message"
        )
        tool_states: dict[str, str] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        message_ids: list[str] = []

        send_task: Any = None
        try:
            # Fire the message POST concurrently with the SSE subscription
            # (V1 created the send task alongside the SSE reader).  We start
            # it eagerly so it is in flight before the first event arrives;
            # a single httpx client cannot stream and POST at once, hence
            # the two independent seams.
            send_task = asyncio.ensure_future(
                self._oc_send_message(url=url, body=body, api_key=api_key)
            )
            async for raw in self._oc_open_event_stream(api_key=api_key):
                ev = self._parse_sse_data(raw)
                if ev is None:
                    continue
                etype = str(ev.get("type") or "")
                props = ev.get("properties") if isinstance(ev, dict) else None
                if not isinstance(props, dict):
                    props = {}

                if etype == "message.part.delta":
                    if props.get("sessionID") not in (oc_session_id, None):
                        continue
                    if str(props.get("field") or "") == "text":
                        delta = str(props.get("delta") or "")
                        if delta:
                            yield CodingStreamFrame(
                                kind=StreamFrameKind.TEXT,
                                payload={"text": delta},
                                sequence=self._next_sequence(session_id),
                            )
                    continue

                if etype == "message.part.updated":
                    if props.get("sessionID") not in (oc_session_id, None):
                        continue
                    part = props.get("part")
                    if not isinstance(part, dict):
                        continue
                    # Track upstream message ids for native revert (2-H3).
                    mid = part.get("messageID") or part.get("messageId")
                    if isinstance(mid, str) and mid and mid not in message_ids:
                        message_ids.append(mid)
                    frame = self._map_oc_event_part(
                        part, tool_states, session_id
                    )
                    if frame is not None:
                        kind, payload = frame
                        if kind is None:
                            # step-finish: accumulate usage, emit nothing.
                            self._accumulate_step_usage(part, usage)
                            continue
                        yield CodingStreamFrame(
                            kind=kind,
                            payload=payload,
                            sequence=self._next_sequence(session_id),
                        )
                        # 2-H11: synthesise sub-task frames around a Task
                        # tool call/result (shared base hook; default no-op
                        # for non-Task tools).
                        for extra_kind, extra_payload in self._subtask_frames(
                            kind=kind, payload=payload
                        ):
                            yield CodingStreamFrame(
                                kind=extra_kind,
                                payload=extra_payload,
                                sequence=self._next_sequence(session_id),
                            )
                    continue

                if etype in {"session.idle", "session.idle.v2"}:
                    if props.get("sessionID") in (oc_session_id, None):
                        if message_ids:
                            self._handles.setdefault(
                                session_id.value, {}
                            )["oc_message_ids"] = message_ids
                        yield CodingStreamFrame(
                            kind=StreamFrameKind.END,
                            payload={"usage": dict(usage)},
                            sequence=self._next_sequence(session_id),
                        )
                        return

                if etype == "session.error":
                    if props.get("sessionID") in (oc_session_id, None):
                        error = props.get("error")
                        name = (
                            error.get("name")
                            if isinstance(error, dict)
                            else str(error)
                        ) or "SessionError"
                        yield CodingStreamFrame(
                            kind=StreamFrameKind.ERROR,
                            payload={
                                "code": "ai_coding.upstream_stream_failed",
                                "message": str(name),
                            },
                            sequence=self._next_sequence(session_id),
                        )
                        return
        finally:
            if send_task is not None:
                if send_task.done():
                    # Surface (but don't re-raise) a send failure — the
                    # event stream already carried any session.error.
                    send_task.exception()
                else:
                    # The SSE loop finished (idle / error / exhausted)
                    # before the POST resolved — give it a brief chance to
                    # complete so the request is actually issued (tests +
                    # real servers ack the POST near-instantly), then
                    # cancel if it is still pending.
                    try:
                        await asyncio.wait_for(send_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        send_task.cancel()
                    except BaseException:  # noqa: BLE001 — best-effort
                        pass

    def _map_oc_event_part(
        self,
        part: dict[str, Any],
        tool_states: dict[str, str],
        session_id: CodingSessionId,  # noqa: ARG002 — symmetry / future use
    ) -> tuple[StreamFrameKind | None, dict[str, Any]] | None:
        """Map one ``message.part.updated`` part to a frame (2-H5).

        Returns ``(kind, payload)`` for a content part, ``(None, {})`` for
        a ``step-finish`` marker (the caller accumulates its usage and
        emits nothing), or ``None`` to drop the part entirely.  Tool parts
        are deduped on their call id so a multi-update tool (running →
        completed) only emits one TOOL_CALL and one TOOL_RESULT.
        """
        ptype = str(part.get("type") or "")
        if ptype == "text":
            return StreamFrameKind.TEXT, {"text": str(part.get("text") or "")}
        if ptype == "tool":
            state = part.get("state")
            if not isinstance(state, dict):
                state = {}
            status = str(state.get("status") or "")
            tool_name = str(part.get("tool") or "")
            call_id = part.get("callID") or part.get("id") or ""
            prev = tool_states.get(str(call_id))
            if status == "running" and prev != "running":
                tool_states[str(call_id)] = "running"
                return StreamFrameKind.TOOL_CALL, {
                    "tool": tool_name,
                    "args": dict(state.get("input") or {}),
                    "call_id": call_id,
                }
            if status == "completed" and prev != "completed":
                tool_states[str(call_id)] = "completed"
                return StreamFrameKind.TOOL_RESULT, {
                    "tool": tool_name,
                    "output": state.get("output") or "",
                    "call_id": call_id,
                }
            if status == "error" and prev != "error":
                tool_states[str(call_id)] = "error"
                return StreamFrameKind.ERROR, {
                    "message": str(state.get("error") or "Tool error"),
                    "tool": tool_name,
                    "call_id": call_id,
                }
            return None
        if ptype in {"step-finish", "step_finish"}:
            return None, {}
        return None

    @staticmethod
    def _accumulate_step_usage(
        part: dict[str, Any], usage: dict[str, Any]
    ) -> None:
        """Fold one ``step-finish`` part's tokens / cost into ``usage``.

        V1 ``opencode_session_manager.py:741-746``: each ``step-finish``
        carried per-step ``tokens`` (input / output) + ``cost`` which the
        manager summed across the turn.  We mirror the accumulate so the
        terminal END frame's ``usage`` payload lets the streaming use case
        write back the cumulative counters (2-H2 chain).
        """
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            try:
                usage["input_tokens"] += int(tokens.get("input", 0) or 0)
                usage["output_tokens"] += int(tokens.get("output", 0) or 0)
            except (TypeError, ValueError):
                pass
        try:
            usage["cost"] += float(part.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _parse_sse_data(raw: Any) -> dict[str, Any] | None:
        """Normalise one SSE data payload into a dict event, or ``None``.

        Accepts either a pre-decoded dict (test seam convenience) or a
        raw JSON string (the real ``data:`` line body).  Blank / ``[DONE]``
        / un-parseable payloads collapse to ``None`` so the consumer loop
        simply skips them (V1 ``opencode_session_manager.py:296-306``).
        """
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text or text == "[DONE]":
            return None
        import json as _json

        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    async def _oc_open_event_stream(
        self, *, api_key: str
    ) -> AsyncIterator[Any]:
        """Yield raw ``data:`` payloads from ``GET /event`` (2-H5 seam).

        Injectable seam: tests monkeypatch this to replay a canned event
        sequence so :meth:`_oc_consume_events` runs without a live
        OpenCode server.  The real implementation opens an httpx SSE
        stream and yields each ``data:`` line body, mirroring V1
        ``opencode_session_manager._send_and_stream``'s SSE reader.
        """
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover — minimal install.
            raise InfrastructureError(
                code="ai_coding.upstream_stream_failed",
                message="httpx is required for the OpenCode adapter",
                details={"provider": Provider.OPEN_CODE.value},
            ) from exc

        url = f"{self._config.base_url.rstrip('/')}/event"
        timeout = httpx.Timeout(
            None, connect=self._config.connect_timeout_s
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET",
                url,
                headers={
                    **self._oc_headers(api_key),
                    "Accept": "text/event-stream",
                },
            ) as resp:
                buffer: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        buffer.append(line[5:].strip())
                    elif line == "":
                        if buffer:
                            yield "\n".join(buffer)
                            buffer = []

    async def _oc_send_message(
        self, *, url: str, body: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        """POST the user message body (fire-and-forget for 2-H5).

        Independent of the SSE subscription (a single httpx client cannot
        stream and POST concurrently — V1 used two clients).  Delegates to
        :meth:`_oc_post_json`; the returned JSON is unused (content flows
        through the event stream) but surfaced for tests that assert the
        request shape.
        """
        return await self._oc_post_json(url=url, body=body, api_key=api_key)

    async def abort(self, *, session_id: CodingSessionId) -> bool:
        """Abort the in-flight OpenCode turn via the native endpoint.

        2-H4 (OC native abort).  Mirrors V1
        ``opencode_session_manager.abort_session``
        (``opencode_session_manager.py:1108-1136``): POST
        ``/session/{oc_session_id}/abort`` to the OpenCode HTTP server,
        then tear down the local handle.  Best-effort — a missing
        upstream session id, missing api key, or a provider-side HTTP
        failure all degrade to the local terminate fallback (the use
        case still flips the aggregate to IDLE).

        Returns ``True`` when the native abort POST was issued,
        ``False`` when only the local terminate ran.
        """
        record = self._handles.get(session_id.value, {})
        oc_session_id = record.get("oc_session_id")
        issued = False
        if isinstance(oc_session_id, str) and oc_session_id:
            try:
                api_key = self._require_api_key()
            except ProviderNotAvailableError:
                api_key = ""
            url = (
                f"{self._config.base_url.rstrip('/')}"
                f"/session/{oc_session_id}/abort"
            )
            try:
                await self._oc_post_json(url=url, body={}, api_key=api_key)
                issued = True
            except Exception:  # noqa: BLE001 — best-effort; never raise.
                issued = False
        # Always tear down the local handle (V1 set status=idle + cleared
        # the interrupt event); the use case owns the aggregate flip.
        await self.terminate(session_id=session_id)
        return issued

    async def rewind_files(
        self,
        *,
        session_id: CodingSessionId,
        marker_index: int,
    ) -> bool:
        """Revert OpenCode workspace files to a prior message (2-H3).

        Mirrors V1 ``opencode_session_manager.revert_message``
        (``opencode_session_manager.py:1138-1169``): POST
        ``/session/{oc_session_id}/revert`` with ``{"messageID": ...}``
        to roll back the OpenCode server's workspace + message state.

        V2's aggregate stores message *text* only, so the native revert
        needs an OpenCode ``messageID`` that this adapter learns and
        caches per turn (``record["oc_message_ids"]``, a parallel list
        to the accumulated user-message history populated by the stream
        loop as the server returns message ids).  When a message id is
        known for ``marker_index`` the native revert is issued;
        otherwise we honestly return ``False`` (message-only rewind)
        rather than guessing an id — V1 likewise returned ``None`` when
        it could not resolve the target.  Best-effort: never raises.
        """
        record = self._handles.get(session_id.value, {})
        oc_session_id = record.get("oc_session_id")
        if not isinstance(oc_session_id, str) or not oc_session_id:
            return False
        message_ids = record.get("oc_message_ids")
        if not isinstance(message_ids, list):
            return False
        if marker_index < 0 or marker_index >= len(message_ids):
            return False
        message_id = message_ids[marker_index]
        if not isinstance(message_id, str) or not message_id:
            return False
        try:
            api_key = self._require_api_key()
        except ProviderNotAvailableError:
            api_key = ""
        url = (
            f"{self._config.base_url.rstrip('/')}"
            f"/session/{oc_session_id}/revert"
        )
        try:
            await self._oc_post_json(
                url=url, body={"messageID": message_id}, api_key=api_key
            )
            return True
        except Exception:  # noqa: BLE001 — best-effort; never raise.
            return False

    async def _oc_ensure_session(
        self, *, session_id: CodingSessionId, api_key: str
    ) -> str:
        """Return the upstream ``ses_...`` id, creating it on first use.

        Mirrors V1 ``opencode_session_manager._ensure_oc_session``: the
        upstream session is lazily created at first send and cached on
        the per-session handle so subsequent turns reuse it.
        """
        record = self._handles.setdefault(session_id.value, {})
        existing = record.get("oc_session_id")
        if isinstance(existing, str) and existing:
            return existing
        data = await self._oc_create_session(api_key=api_key)
        oc_session_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(oc_session_id, str) or not oc_session_id:
            raise InfrastructureError(
                code="ai_coding.upstream_stream_failed",
                message=f"OpenCode /session response missing 'id': {data!r}",
                details={"provider": Provider.OPEN_CODE.value},
            )
        record["oc_session_id"] = oc_session_id
        return oc_session_id

    def set_oc_selection(
        self,
        *,
        session_id: CodingSessionId,
        provider: str | None,
        model: str | None,
    ) -> None:
        """Seed the OpenCode provider/model selection on the handle (2-H10).

        Called by the application layer when (a) the operator changes the
        ``/oc model`` selection and (b) a session is restored from
        persistence carrying ``oc_current_provider`` / ``oc_current_model``
        (V1 ``opencode_session_models.py:67-68``).  The values are stored
        under dedicated handle keys (``current_provider`` /
        ``current_model``) so :meth:`_resolve_model_selection` reads them
        as first-class fields rather than spelunking ``extra_args``.
        """
        record = self._handles.setdefault(session_id.value, {})
        if provider is not None:
            record["current_provider"] = provider
        if model is not None:
            record["current_model"] = model

    def get_oc_selection(
        self, session_id: CodingSessionId
    ) -> tuple[str | None, str | None]:
        """Return the (provider, model) selection cached on the handle.

        2-H10.  The application layer reads this after a turn to persist
        the selection on the aggregate (so it survives a restart).
        """
        record = self._handles.get(session_id.value, {})
        provider = record.get("current_provider")
        model = record.get("current_model")
        return (
            provider if isinstance(provider, str) else None,
            model if isinstance(model, str) else None,
        )

    def get_oc_message_ids(self, session_id: CodingSessionId) -> list[str]:
        """Return the OpenCode-native message ids learned this process (2-H3).

        RE-OC-7: the application layer reads this after a turn to persist
        the ordered id list onto the aggregate
        (:meth:`CodingSession.record_oc_message_ids`) so a later native
        revert works even after a daemon restart.  Returns ``[]`` when no
        turn has streamed in this process for the session.
        """
        record = self._handles.get(session_id.value, {})
        ids = record.get("oc_message_ids")
        if isinstance(ids, list):
            return [str(m) for m in ids if m]
        return []

    def seed_oc_message_ids(
        self, *, session_id: CodingSessionId, message_ids: list[str]
    ) -> None:
        """Rehydrate the per-handle OpenCode message-id list (2-H3 / RE-OC-7).

        Called by the revert / rewind use cases with the ids persisted on
        the aggregate so :meth:`rewind_files` can resolve a
        ``marker_index`` to the right native ``messageID`` even when this
        process never streamed the original turn (post-restart / restored
        session).  Mirrors :meth:`set_oc_selection`'s seed-from-aggregate
        pattern.
        """
        if not message_ids:
            return
        record = self._handles.setdefault(session_id.value, {})
        record["oc_message_ids"] = [str(m) for m in message_ids if m]

    def _resolve_model_selection(
        self, session_id: CodingSessionId
    ) -> tuple[str, str]:
        """Resolve ``(providerID, modelID)`` for the message body.

        2-H10: reads the dedicated per-session selection fields
        (``current_provider`` / ``current_model``) seeded by
        :meth:`set_oc_selection` (from the operator's choice / the
        restored aggregate) as the highest-priority source, falling back
        to the legacy ``provider_id`` / ``model_id`` handle keys, then the
        :class:`CodingSessionConfig.extra_args` escape hatch, then
        OpenCode's self-hosted defaults.  OpenCode resolves its own
        configured default when ``modelID`` is empty, so a missing
        selection is a valid (and common) state.
        """
        record = self._handles.get(session_id.value, {})
        provider_id = (
            record.get("current_provider")
            or record.get("provider_id")
            or _OC_DEFAULT_PROVIDER_ID
        )
        model_id = record.get("current_model") or record.get("model_id") or ""
        cfg = record.get("config")
        if isinstance(cfg, CodingSessionConfig):
            extra = cfg.extra_args or {}
            model_id = model_id or extra.get("model_id") or ""
            provider_id = (
                record.get("current_provider")
                or record.get("provider_id")
                or extra.get("provider_id")
                or _OC_DEFAULT_PROVIDER_ID
            )
        return str(provider_id), str(model_id or _OC_DEFAULT_MODEL_ID)

    def _build_message_body(
        self, *, session_id: CodingSessionId
    ) -> dict[str, Any]:
        """Assemble the ``POST /session/{id}/message`` JSON body.

        OpenCode accepts a free-form ``parts`` array of content parts;
        we stitch the accumulated user messages into a single text part
        (newest turns are appended by :meth:`send_message`).  ``modelID``
        is omitted when empty so OpenCode falls back to its own
        configured default.
        """
        record = self._handles.get(session_id.value, {})
        messages = [str(m) for m in (record.get("messages") or [])]
        text = "\n".join(messages) if messages else "(no message)"
        provider_id, model_id = self._resolve_model_selection(session_id)
        body: dict[str, Any] = {
            "providerID": provider_id,
            "parts": [{"type": "text", "text": text}],
        }
        if model_id:
            body["modelID"] = model_id
        # 2-H9: replay any operator permission decision (with edits) onto
        # the resume request so the OpenCode server applies the corrected
        # tool input + per-tool permission overrides on the next turn
        # (V1 ``opencode`` honoured the ``tools`` permission map +
        # message-level overrides; V2 forwards the queued decision so the
        # operator's edits take effect without re-sending the message).
        replay = self._consume_permission_replay(session_id)
        if replay:
            body["permission_decisions"] = replay
            # OpenCode's per-tool permission map lives under ``tools``;
            # merge the most recent decision's updated_permissions so the
            # native server enforces the override on this turn.
            latest = replay[-1]
            updated = latest.get("updated_permissions") or []
            if updated:
                tools_map: dict[str, Any] = {}
                for entry in updated:
                    if isinstance(entry, dict):
                        name = entry.get("tool") or entry.get("name")
                        rule = entry.get("rule") or entry.get("decision")
                        if name and rule:
                            tools_map[str(name)] = rule
                if tools_map:
                    body["tools"] = tools_map
        return body

    @staticmethod
    def _oc_headers(api_key: str) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        # Empty-string key = unauthenticated (local OpenCode parity);
        # only attach Authorization when there is a real value.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _oc_create_session(self, *, api_key: str) -> dict[str, Any]:
        """``POST /session`` → the created session object (incl. ``id``).

        Injectable seam: unit/integration tests monkeypatch this so the
        streaming loop runs without a live OpenCode server.
        """
        url = f"{self._config.base_url.rstrip('/')}/session"
        return await self._oc_post_json(
            url=url, body={}, api_key=api_key
        )

    async def _oc_post_json(
        self, *, url: str, body: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        """Single ``httpx`` JSON POST seam shared by the OC HTTP calls.

        Kept as the only place that imports / touches ``httpx`` so tests
        can monkeypatch one method and exercise the full streaming loop
        offline (CI parity).  Raises :class:`InfrastructureError` on a
        missing ``httpx`` install or any non-2xx response, which the
        :meth:`_oc_stream` loop turns into a typed ERROR frame.
        """
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover — minimal install.
            raise InfrastructureError(
                code="ai_coding.upstream_stream_failed",
                message="httpx is required for the OpenCode adapter",
                details={"provider": Provider.OPEN_CODE.value},
            ) from exc

        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_s,
            read=self._config.read_timeout_s,
            write=30.0,
            pool=5.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, json=body, headers=self._oc_headers(api_key)
            )
            if int(response.status_code) >= 400:
                raise InfrastructureError(
                    code="ai_coding.upstream_stream_failed",
                    message=(
                        f"OpenCode POST {url} failed: "
                        f"HTTP {response.status_code}"
                    ),
                    details={
                        "provider": Provider.OPEN_CODE.value,
                        "status_code": int(response.status_code),
                    },
                )
            return response.json()

    @staticmethod
    def _map_oc_part(
        part: dict[str, Any],
    ) -> tuple[StreamFrameKind, dict[str, Any]] | None:
        """Map one OpenCode response ``part`` into a frame, or ``None``.

        OpenCode's ``parts`` array carries structural markers
        (``step-start`` / ``step-finish``) interleaved with the real
        content.  We surface:

        * ``type == "text"`` → :data:`StreamFrameKind.TEXT` (the real
          model text lives in ``.text``);
        * tool invocation parts (``tool`` / ``tool-invocation`` /
          ``tool_use``) → :data:`StreamFrameKind.TOOL_CALL`;
        * tool result parts (``tool-result`` / ``tool_result``) →
          :data:`StreamFrameKind.TOOL_RESULT`;
        * ``step-finish`` whose ``reason`` is terminal → END;
        * everything else (``step-start`` etc.) is dropped (``None``).
        """
        ptype = str(part.get("type") or "")
        if ptype == "text":
            return StreamFrameKind.TEXT, {"text": str(part.get("text") or "")}
        if ptype in {"tool", "tool-invocation", "tool_use", "tool-call"}:
            tool = part.get("tool") or part.get("name") or ""
            args = part.get("input") or part.get("args") or part.get("state") or {}
            return StreamFrameKind.TOOL_CALL, {
                "tool": str(tool),
                "args": dict(args) if isinstance(args, dict) else {"value": args},
                "call_id": part.get("callID") or part.get("call_id") or part.get("id"),
            }
        if ptype in {"tool-result", "tool_result"}:
            return StreamFrameKind.TOOL_RESULT, {
                "tool": str(part.get("tool") or part.get("name") or ""),
                "output": part.get("output") or part.get("result") or "",
                "call_id": part.get("callID") or part.get("call_id") or part.get("id"),
            }
        if ptype in {"step-finish", "step_finish", "finish"}:
            reason = str(part.get("reason") or "")
            if reason in {"stop", "end", "completed", "length"}:
                return StreamFrameKind.END, {}
            return None
        if ptype in {"error"}:
            return StreamFrameKind.ERROR, {
                "message": str(part.get("error") or part.get("message") or "")
            }
        # step-start and any unknown structural part carry no user-facing
        # content — drop them so the frontend only sees real frames.
        return None
