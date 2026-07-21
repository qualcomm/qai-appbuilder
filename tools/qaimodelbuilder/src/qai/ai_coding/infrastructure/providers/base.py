# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared base for ai_coding HTTP-based provider adapters (PR-046, PR-102).

Both :class:`ClaudeCodeProvider` and :class:`OpenCodeProvider` extend
this class.  The base captures the per-instance bookkeeping the
domain expects (mapping a ``CodingSessionId`` to whatever opaque
handle the upstream API returns) and the streaming loop shared by
both adapters.

PR-102 replaces the placeholder ``_scripted_stream`` 4-frame canned
sequence with real Anthropic Messages SSE / OpenCode SSE consumption
through the injectable :class:`HttpTransportPort` (see
:mod:`http_transport`).  Subclasses that don't wire a transport
(``transport=None``) keep the legacy 4-frame fallback so historical
tests / offline tooling still work.

Cross-cutting concerns
----------------------
* **API keys** are resolved through :class:`qai.platform.persistence.secrets.SecretStore`
  at provider construction time.  The key never appears in code
  literals, settings file content, or log output.  An unknown key
  surfaces as :class:`ProviderNotAvailableError` so the route layer
  produces a clean ``409``.
* **HTTP timeouts / retries** are read from a small typed config
  passed at construction; the defaults match the project-wide
  cautious values (15s connect / 60s read).
* **Streaming** uses the upstream's SSE / streamed-JSON endpoint;
  each frame is mapped into a :class:`CodingStreamFrame` with
  monotonically-increasing ``sequence`` so the aggregate's
  ``record_stream_frame`` invariant holds.

The two concrete adapters in this package supply only:

* the ``Provider`` enum value they advertise;
* the URL builders for ``spawn``, ``stream``, ``send_message`` and
  ``terminate``;
* a small mapper turning the upstream's frame envelope into
  :class:`StreamFrameKind`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from qai.ai_coding.domain import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    MessageContent,
    Provider,
    ProviderNotAvailableError,
    StreamFrameKind,
    Workspace,
)
from qai.platform.errors import InfrastructureError, NotFoundError
from qai.platform.logging import get_logger
from qai.platform.persistence.secrets import SecretStore

from .http_transport import (
    HttpStreamError,
    HttpTransportPort,
    parse_sse_bytes,
)
from ..os_hint_builder import build_os_hint

__all__ = [
    "DEFAULT_HTTP_CONFIG",
    "HttpCodingProviderBase",
    "HttpStreamError",
    "ProviderHttpConfig",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderHttpConfig:
    """HTTP transport settings for a coding provider adapter."""

    base_url: str
    api_key_service: str
    api_key_name: str = "api_key"
    connect_timeout_s: float = 15.0
    read_timeout_s: float = 60.0
    max_retries: int = 0


DEFAULT_HTTP_CONFIG = ProviderHttpConfig(
    base_url="https://api.anthropic.com",
    api_key_service="qai.ai_coding.claude_code",
)


class HttpCodingProviderBase:
    """Common machinery for HTTP-backed coding provider adapters.

    Subclasses must:

    * pass their :class:`Provider` value to ``__init__``;
    * override :meth:`_build_spawn_payload` to shape the ``spawn``
      body for the upstream API;
    * override :meth:`_map_stream_frame` to translate the upstream's
      streaming envelope into a :class:`StreamFrameKind` + payload
      pair (no-op transforms allowed).

    The streaming loop is itself implementation-agnostic — concrete
    adapters can override :meth:`stream` for full custom dispatch
    if their upstream does not fit the default loop.
    """

    __slots__ = (
        "_api_key_cache",
        "_config",
        "_handles",
        "_pending_images",
        "_pending_permission_futures",
        "_provider",
        "_secret_store",
        "_sequence",
        "_session_histories",
        "_transport",
    )

    def __init__(
        self,
        *,
        provider: Provider,
        config: ProviderHttpConfig,
        secret_store: SecretStore,
        transport: HttpTransportPort | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._secret_store = secret_store
        self._transport = transport
        self._handles: dict[str, dict[str, Any]] = {}
        # Per-session monotonic frame sequence counters.
        self._sequence: dict[str, int] = {}
        # Per-session multi-turn message history (Anthropic messages format).
        # Each entry is {"role": "user", "content": ...}.
        self._session_histories: dict[str, list[dict[str, Any]]] = {}
        # PR-095 / S9 H-19 — pending inline image attachment, keyed by
        # session id.  ``send_message`` consumes (and clears) the entry
        # so the next user-content block carries multimodal parts.
        # Shape: ``{"b64": "...", "mime": "image/png"}``.
        self._pending_images: dict[str, dict[str, str]] = {}
        # Tiny cache so we don't hit the keyring on every request.
        self._api_key_cache: str | None = None
        # 2-H9: per-session pending permission decision replay.  The base
        # ``forward_permission_decision`` already stashes the decision (+
        # edits) on the handle under ``permission_decisions``; concrete
        # adapters read it back in ``_build_stream_body`` so the next
        # stream resume carries the operator's updated_input /
        # updated_permissions into the upstream call (V1
        # ``session_manager.py:1588-1637`` replayed it into the in-flight
        # ``can_use_tool`` callback).
        self._pending_permission_futures: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public port surface
    def available_providers(self) -> Iterable[Provider]:
        return (self._provider,)

    async def spawn(
        self,
        *,
        provider: Provider,
        workspace: Workspace,
        initial_prompt: MessageContent | None,
        session_id: CodingSessionId | None = None,
        config: CodingSessionConfig | None = None,
    ) -> dict[str, Any]:
        if provider is not self._provider:
            raise ProviderNotAvailableError(
                message=(
                    f"adapter {self._provider.value} cannot spawn "
                    f"sessions for {provider.value}"
                ),
                details={
                    "expected": self._provider.value,
                    "received": provider.value,
                },
            )
        # NOTE: a real adapter performs an HTTP call here that requires
        # the API key.  Until that real call is wired in (S5+) we
        # tolerate a missing key so unit tests / offline tooling can
        # exercise the spawn happy path; ``send_message`` and the real
        # streaming loop still call ``_require_api_key`` at the point
        # they actually need it.
        handle: dict[str, Any] = {
            "provider": self._provider.value,
            "workspace": workspace.path,
            "initial_prompt": (
                initial_prompt.text if initial_prompt is not None else None
            ),
            "endpoint": self._config.base_url,
            # PR-095 / S9 H-11: stash the OS hint at spawn time so
            # downstream system-prompt construction (in the provider's
            # stream body builder or the SDK-backed CC provider)
            # can prepend it without re-detecting the platform.
            "os_hint_system": self._build_os_hint_system(),
        }
        # PR-107: When the use case pre-allocates the session id and
        # passes a :class:`CodingSessionConfig`, stash both in
        # :attr:`_handles` so :meth:`_build_stream_body` (and the
        # SDK-backed CC provider) can read the SDK 12-item config back.
        if session_id is not None:
            session_handle = self._handles.setdefault(session_id.value, {})
            session_handle["config"] = config or CodingSessionConfig()
            session_handle.setdefault("messages", [])
            # PR-095 / S9 H-11 (C-1 fix): persist the OS-context system
            # prompt addendum on the runtime handle too — the spawn return
            # value above goes to the use case / aggregate, but the
            # provider's streaming body / SDK options builders read back
            # from ``self._handles`` at stream time, so the hint must live
            # there to actually reach the upstream ``system`` field (V1
            # ``session_manager.py:1390-1432`` injected it into every turn's
            # system prompt).  Single truth source: stored once at spawn.
            session_handle["os_hint_system"] = handle["os_hint_system"]
        return self._build_spawn_payload(handle=handle, workspace=workspace)

    def session_config(
        self, session_id: CodingSessionId
    ) -> CodingSessionConfig:
        """Return the :class:`CodingSessionConfig` stashed at spawn time.

        Returns the default empty config when no config was supplied
        (PR-046 spawn path), so callers can always count on the
        return value being a real value object.

        This is the read-side hook the SDK-backed CC provider uses to
        consume the config fields it honours (effort / thinking /
        session_env / add_dirs / extra_args / cli_path /
        enable_file_checkpointing), and the seam through which the
        local-only reserved fields (hooks / output_format /
        setting_sources / plugins) are read back without importing the
        provider-internal handle dict shape.
        """
        record = self._handles.get(session_id.value, {})
        cfg = record.get("config")
        if isinstance(cfg, CodingSessionConfig):
            return cfg
        return CodingSessionConfig()

    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        # When a real :class:`HttpTransportPort` is wired we ALWAYS run
        # the real upstream loop.  If no API key is configured the real
        # loop surfaces an explicit ERROR frame (``provider_not_available``)
        # via :meth:`_require_api_key` — matching V1 behaviour where an
        # unconfigured provider returns a clear error rather than fake
        # data.  Only when NO transport is wired (offline unit tests that
        # deliberately omit a transport) do we fall back to the
        # deterministic scripted sequence so those contract tests keep
        # working without hitting the network.
        if self._transport is not None:
            return self._real_stream(session_id)
        return self._scripted_stream(session_id)

    async def send_message(
        self,
        *,
        session_id: CodingSessionId,
        content: MessageContent,
    ) -> None:
        # Record message in the legacy handle for backward compatibility.
        record = self._handles.setdefault(session_id.value, {})
        record.setdefault("messages", []).append(content.text)
        # Append to multi-turn session history (Anthropic messages format).
        history = self._session_histories.setdefault(session_id.value, [])
        # PR-095 / S9 H-19: when the application layer staged a pending
        # image via :meth:`attach_image`, build a multimodal content
        # block for this turn instead of a plain text string.  The
        # block follows Anthropic's multimodal schema:
        # ``[{"type":"image","source":{"type":"base64",
        # "media_type":"image/png","data":"..."}},
        # {"type":"text","text":"..."}]``.
        pending = self._pending_images.pop(session_id.value, None)
        if pending is not None and pending.get("b64") and pending.get("mime"):
            history.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": pending["mime"],
                                "data": pending["b64"],
                            },
                        },
                        {"type": "text", "text": content.text},
                    ],
                }
            )
        else:
            history.append({"role": "user", "content": content.text})

    def attach_image(
        self,
        *,
        session_id: CodingSessionId,
        image_b64: str,
        image_mime: str,
    ) -> None:
        """Stage a base64 image for the next :meth:`send_message` turn.

        PR-095 / S9 H-19.  The application layer calls this helper
        immediately before :meth:`send_message` when the inbound
        :class:`SendUserMessageCommand` carries ``image_b64`` +
        ``image_mime``.  The pending image is consumed by exactly one
        ``send_message`` call (single-shot) so a follow-up text-only
        turn does not silently re-attach the same picture.
        """
        if not image_b64 or not image_mime:
            return
        self._pending_images[session_id.value] = {
            "b64": image_b64,
            "mime": image_mime,
        }

    async def terminate(self, *, session_id: CodingSessionId) -> None:
        self._handles.pop(session_id.value, None)
        self._sequence.pop(session_id.value, None)
        self._session_histories.pop(session_id.value, None)
        self._pending_images.pop(session_id.value, None)
        # 2-H9: resolve / drop any pending permission future so an
        # awaiting subclass coroutine does not hang after the session is
        # torn down.
        fut = self._pending_permission_futures.pop(session_id.value, None)
        if fut is not None and not getattr(fut, "done", lambda: True)():
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    async def abort(self, *, session_id: CodingSessionId) -> bool:
        """Default abort: no upstream native abort, fall back to terminate.

        2-H4.  The base / Claude Code path has no separate native abort
        endpoint (V1 CC used the SDK interrupt, surfaced by the
        application-layer soft interrupt), so the base implementation
        tears down the local handle and reports ``False`` (no upstream
        abort issued).  :class:`OpenCodeProvider` overrides this to call
        the native ``POST /session/{id}/abort`` endpoint.
        """
        await self.terminate(session_id=session_id)
        return False

    async def rewind_files(
        self,
        *,
        session_id: CodingSessionId,
        marker_index: int,
    ) -> bool:
        """Default rewind: no native file restoration (2-H3).

        The base / Claude Code HTTP provider has no live SDK client to
        call ``rewind_files(sdk_uuid)`` on, so it reports ``False`` and
        :class:`RewindCheckpointUseCase` performs a message-only rewind —
        never raising, never fabricating a file rollback.  The native file
        restoration is provided by the SDK-backed CC provider
        (:class:`ClaudeCodeSdkProvider`, ``cc_backend=sdk`` +
        ``enable_file_checkpointing``, V1 ``session_manager.py:2604-2706``
        parity) and by :class:`OpenCodeProvider` (native
        ``POST /session/{id}/revert``), both of which override this.
        """
        return False

    async def fork_session(self, *, session_id: CodingSessionId) -> bool:
        """Drop the cached upstream session id so the next turn forks.

        2-H6.  V1 forked by clearing the cached upstream id and letting
        the SDK create a fresh backend conversation on the next send
        (``session_manager.py:1663-1669``).  V2's base honours both
        provider conventions in one place so concrete adapters need not
        re-implement: a CC handle caches ``claude_session_id`` (used for
        ``resume: true``) and an OC handle caches ``oc_session_id`` (the
        ``ses_...`` upstream id reused per turn).  Removing whichever is
        present means :meth:`stream` lazily creates a brand-new upstream
        session next turn — a real fork, not an informational flag.

        Best-effort and never raises: a missing handle / already-cleared
        id simply reports ``False`` (next turn already starts fresh).
        """
        record = self._handles.get(session_id.value)
        if not isinstance(record, dict):
            return False
        forked = False
        for key in ("claude_session_id", "oc_session_id"):
            if record.pop(key, None):
                forked = True
        return forked

    async def forward_permission_decision(
        self,
        *,
        session_id: CodingSessionId,
        request_id: Any,
        decision: Any,
        updated_input: dict[str, Any] | None = None,
        updated_permissions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Ferry a permission decision (+ edits) to the upstream stream.

        PR-095 / S9 H-13 + 2-H9.  Called by
        :class:`DecidePermissionUseCase` after the aggregate transition
        is persisted.  The base implementation:

        1. stashes the normalised payload on the per-session handle
           under ``permission_decisions`` (a queue) so
           :meth:`_build_stream_body` replays the operator's
           ``updated_input`` / ``updated_permissions`` into the next
           stream resume — this is the V2 HTTP-loop analogue of V1's
           in-flight ``can_use_tool`` replay
           (``session_manager.py:1588-1637``);
        2. resolves any pending permission future a subclass is awaiting
           (live-callback adapters), so an in-flight upstream call that
           parked on the gate resumes immediately with the decision.

        2-H9: making the base resolve the future + queue the replay
        means concrete CC/OC adapters only need a thin override to
        register the future; the dynamic-rule replay itself is shared.
        """
        record = self._handles.setdefault(session_id.value, {})
        decisions = record.setdefault("permission_decisions", [])
        payload = {
            "request_id": str(request_id),
            "decision": getattr(decision, "value", str(decision)),
            "updated_input": dict(updated_input or {}),
            "updated_permissions": list(updated_permissions or []),
        }
        decisions.append(payload)
        # 2-H9: wake any subclass coroutine parked on the gate so the
        # in-flight upstream call resumes with the operator's edits.
        fut = self._pending_permission_futures.get(session_id.value)
        if fut is not None and not getattr(fut, "done", lambda: True)():
            try:
                fut.set_result(payload)
            except Exception:  # noqa: BLE001 — already resolved / cancelled
                pass

    def _consume_permission_replay(
        self, session_id: CodingSessionId
    ) -> list[dict[str, Any]]:
        """Pop and return the queued permission-decision replay payloads.

        2-H9.  :meth:`_build_stream_body` calls this to fold the
        operator's ``updated_input`` / ``updated_permissions`` into the
        next upstream request, then clears the queue so a decision is
        replayed exactly once (a later turn does not re-apply a stale
        override).  Returns an empty list when no decision is pending.
        """
        record = self._handles.get(session_id.value)
        if not isinstance(record, dict):
            return []
        decisions = record.pop("permission_decisions", None)
        if not isinstance(decisions, list):
            return []
        return list(decisions)

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------
    def _build_spawn_payload(
        self,
        *,
        handle: dict[str, Any],
        workspace: Workspace,
    ) -> dict[str, Any]:
        """Return the dict :meth:`spawn` hands back to the use case."""
        return handle

    def _map_stream_frame(
        self,
        *,
        envelope: dict[str, Any],
    ) -> tuple[StreamFrameKind, dict[str, Any]]:
        """Translate an upstream envelope to a ``StreamFrameKind`` pair."""
        kind_value = str(envelope.get("kind") or "text")
        try:
            kind = StreamFrameKind(kind_value)
        except ValueError:
            kind = StreamFrameKind.TEXT
        payload = dict(envelope.get("payload") or {})
        return kind, payload

    def _subtask_frames(
        self,
        *,
        kind: StreamFrameKind,
        payload: dict[str, Any],
    ) -> list[tuple[StreamFrameKind, dict[str, Any]]]:
        """Return sub-task frames to emit alongside a mapped frame (2-H11).

        Default: none.  Concrete adapters override to synthesise
        ``TASK_STARTED`` / ``TASK_NOTIFICATION`` frames when they map a
        Task / sub-agent tool call/result (the native Anthropic wire does
        not emit task lifecycle events — V1 produced them from SDK Task
        messages, ``session_manager.py:2044-2079``).  The base keeps the
        13-enum frame contract intact (§3.1) by only ever returning
        frames whose kinds are already in :class:`StreamFrameKind`.
        """
        return []

    def _build_stream_url(self) -> str:
        """Return the URL :meth:`_real_stream` POSTs to.

        Subclasses override to point at the upstream's streaming
        endpoint (Anthropic ``/v1/messages``, OpenCode
        ``/sessions/{id}/run``).  Default raises so an accidental
        fallback to the base never silently calls the wrong URL.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _build_stream_url"
        )

    async def _resolve_stream_url(self) -> str:
        """Async seam :meth:`_real_stream` uses to obtain the stream URL.

        The base implementation simply delegates to the synchronous
        :meth:`_build_stream_url`, so adapters whose URL is static (e.g.
        OpenCode) need not change.  Adapters whose streaming endpoint
        depends on a *dynamically resolved* base_url — the operator's
        ``forge_config`` value rather than a construction-time default —
        override this to read that config and build the URL from the
        resolved base.  This mirrors the dynamic resolution the catalog
        probe already does (V1 parity: catalog and streaming share a
        single ``ANTHROPIC_BASE_URL`` truth source).
        """
        return self._build_stream_url()

    def _build_stream_headers(
        self,
        *,
        api_key: str,
        session_id: CodingSessionId | None = None,
    ) -> dict[str, str]:
        """Headers for the streaming request.  Subclasses override.

        ``session_id`` (PR-107) lets subclasses tailor headers to the
        per-session :class:`CodingSessionConfig` (e.g. Anthropic
        ``anthropic-beta`` header when ``mcp_servers`` is non-empty).
        Defaults to ``None`` so subclasses written before PR-107
        keep working unchanged.
        """
        return {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
        }

    def _build_stream_body(
        self, *, session_id: CodingSessionId, api_key: str
    ) -> dict[str, Any]:
        """JSON body for the streaming request.

        Subclasses pull conversation state from ``self._handles`` and
        format whatever the upstream expects.  The base default just
        echoes the recorded messages so the contract is exercised.
        """
        record = self._handles.get(session_id.value, {})
        return {
            "session_id": session_id.value,
            "messages": list(record.get("messages") or []),
        }

    # ------------------------------------------------------------------
    # Multi-turn conversation helpers
    # ------------------------------------------------------------------
    def _get_session_history(
        self, session_id: CodingSessionId
    ) -> list[dict[str, Any]]:
        """Return the multi-turn message history for *session_id*.

        Returns an empty list for sessions that have not yet called
        :meth:`send_message`.  The returned list is a mutable reference
        — callers may append directly.
        """
        return self._session_histories.setdefault(session_id.value, [])

    def _record_assistant_response(
        self,
        session_id: CodingSessionId,
        *,
        text: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append the assistant turn to session history after streaming.

        Accumulates text and tool-use blocks into a single assistant
        message that matches Anthropic's ``messages`` schema::

            {"role": "assistant", "content": [
                {"type": "text", "text": "..."},
                {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
            ]}

        Called by the streaming consumer (typically the use-case or
        the provider's own post-stream hook) once all frames for one
        LLM turn have been collected.
        """
        history = self._session_histories.setdefault(session_id.value, [])
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in (tool_calls or []):
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc.get("tool") or tc.get("name", ""),
                "input": tc.get("args") or tc.get("input") or {},
            })
        if content_blocks:
            history.append({"role": "assistant", "content": content_blocks})

    def _build_os_hint_system(self) -> str:
        """Return the OS-context system-prompt addendum (PR-095 H-11).

        Thin pass-through to :func:`build_os_hint` so subclasses that
        construct upstream system prompts can include the hint with a
        single call.  Defined on the base so unit tests can monkey-
        patch a single method when they need a deterministic hint
        across host platforms.
        """
        return build_os_hint()

    def _os_hint_for_session(self, session_id: CodingSessionId) -> str:
        """Return the OS-context system prompt stashed at spawn time (C-1).

        The OS hint is computed once in :meth:`spawn` and stored on the
        per-session handle so the streaming body / SDK options builders
        can prepend it to the upstream ``system`` field without
        re-detecting the platform.  Falls back to a live
        :meth:`_build_os_hint_system` when no hint was stored (e.g. a
        session created by a hand-rolled test that bypassed
        :meth:`spawn`, or a session restored from persistence without a
        fresh spawn) so the hint is never silently dropped.
        """
        record = self._handles.get(session_id.value, {})
        hint = record.get("os_hint_system")
        if isinstance(hint, str) and hint:
            return hint
        return self._build_os_hint_system()

    def _inject_tool_results(
        self,
        session_id: CodingSessionId,
        tool_results: list[dict[str, Any]],
    ) -> None:
        """Inject tool results into session history for the next turn.

        Each entry in *tool_results* should have the shape::

            {"tool_use_id": "...", "content": "..."}

        This appends a single ``{"role": "user", "content": [...]}``
        message with ``tool_result`` blocks, matching Anthropic's
        conversation schema for multi-turn tool use.
        """
        if not tool_results:
            return
        history = self._session_histories.setdefault(session_id.value, [])
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tr.get("tool_use_id", ""),
                "content": tr.get("content", ""),
            }
            for tr in tool_results
        ]
        history.append({"role": "user", "content": blocks})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_api_key_chain(self) -> str | None:
        """Subclass extension point: V1-parity api_key resolution chain.

        Default returns ``None`` — the base then falls back to the
        single ``api_key_service``/``api_key_name`` SecretStore entry
        (the V2-original "streaming-only key" namespace).  Subclasses
        that need to mirror V1's multi-source resolver (env-var ->
        operator credential namespace -> ...) should override and
        return the resolved key, or ``None`` to delegate to the
        streaming namespace fallback.

        V1 anchor: ``backend/ai_coding/api_routes.py:145-169``
        ``_cc_resolve_api_key()`` was the single shared resolver for
        all callers (health probe, catalog probe, streaming).  V2
        unifies via this extension point so the streaming path picks
        up the same auth-panel writes the catalog probe already
        respects (see :meth:`ClaudeCodeProvider._resolve_catalog_api_key`).
        """
        return None

    def _has_api_key(self) -> bool:
        """Lightweight key probe used by :meth:`stream` to pick a path.

        Returns ``False`` when neither the subclass V1-parity chain
        nor the SecretStore streaming-namespace entry yields a key,
        so the legacy scripted-stream fallback runs in unconfigured
        deployments / unit tests.  Does NOT raise.
        """
        if self._api_key_cache is not None:
            return True
        chain_value = self._resolve_api_key_chain()
        if chain_value:
            self._api_key_cache = chain_value
            return True
        try:
            value = self._secret_store.get(
                self._config.api_key_service,
                self._config.api_key_name,
            )
        except NotFoundError:
            return False
        self._api_key_cache = value
        return True

    def _require_api_key(self) -> str:
        if self._api_key_cache is not None:
            return self._api_key_cache
        chain_value = self._resolve_api_key_chain()
        if chain_value:
            self._api_key_cache = chain_value
            return chain_value
        try:
            value = self._secret_store.get(
                self._config.api_key_service,
                self._config.api_key_name,
            )
        except NotFoundError as exc:
            raise ProviderNotAvailableError(
                message=(
                    f"API key for provider {self._provider.value} not "
                    "configured in SecretStore"
                ),
                details={
                    "provider": self._provider.value,
                    "secret_service": self._config.api_key_service,
                    "secret_key": self._config.api_key_name,
                },
            ) from exc
        self._api_key_cache = value
        return value

    def _next_sequence(self, session_id: CodingSessionId) -> int:
        last = self._sequence.get(session_id.value, -1)
        nxt = last + 1
        self._sequence[session_id.value] = nxt
        return nxt

    async def _scripted_stream(
        self, session_id: CodingSessionId
    ) -> AsyncIterator[CodingStreamFrame]:
        # Minimal contract-preserving sequence: 2 text frames, a tool
        # call, then a synthesised ``permission_request`` (so the offline
        # / no-API-key dev environment can light up the approval card UI
        # end-to-end), followed by an explicit END.  The ``request_id`` is
        # predictable ("scripted-perm-<session_id>") yet session-scoped so
        # it never collides with the globally-unique
        # ``ai_coding_permission_request.id`` primary key when more than
        # one demo session runs.  The frontend reads the id straight off
        # the frame and POSTs it back to
        # ``/permissions/<request_id>/decide`` (2xx) — the
        # StreamCodingSessionUseCase registers the request on the
        # aggregate when it sees this PERMISSION_REQUEST frame.  Real
        # adapters override :meth:`stream` to forward the upstream's
        # actual event stream.
        scripted_request_id = f"scripted-perm-{session_id.value}"
        for envelope in (
            {"kind": "text", "payload": {"text": "hello"}},
            {"kind": "text", "payload": {"text": "world"}},
            {"kind": "tool_call", "payload": {"tool": "echo", "args": {"x": 1}}},
            {
                "kind": "permission_request",
                "payload": {
                    "request_id": scripted_request_id,
                    "tool_name": "echo",
                    "args": {"x": 1},
                    "suggestions": ["scripted demo approval"],
                },
            },
            {"kind": "end", "payload": {}},
        ):
            kind, payload = self._map_stream_frame(envelope=envelope)
            yield CodingStreamFrame(
                kind=kind,
                payload=payload,
                sequence=self._next_sequence(session_id),
            )

    async def _real_stream(
        self, session_id: CodingSessionId
    ) -> AsyncIterator[CodingStreamFrame]:
        """Real upstream SSE streaming loop (PR-102).

        Calls the injected :class:`HttpTransportPort` with the
        URL / headers / body assembled by ``_build_stream_*`` hooks,
        feeds the resulting bytes through :func:`parse_sse_bytes`, and
        emits :class:`CodingStreamFrame` instances using
        :meth:`_map_stream_frame` for kind translation.

        Always yields a terminal :class:`StreamFrameKind.END` frame —
        even on transport error (in which case the END follows an
        :class:`StreamFrameKind.ERROR` frame so consumers can surface
        the failure to the user).
        """
        if self._transport is None:  # pragma: no cover — guarded by stream()
            raise RuntimeError(
                "_real_stream invoked without a transport"
            )
        try:
            api_key = self._require_api_key()
        except ProviderNotAvailableError as exc:
            # Surface the unified QaiError envelope ({type, code,
            # message, details}) so the SSE ``event: error`` frame matches
            # the route contract (ai_coding.py docstring) and the frontend
            # ``isApiErrorPayload`` check builds a proper ApiError carrying
            # ``ai_coding.provider_not_available`` — letting the UI render a
            # clear "configure API key" message (V1 parity) instead of a
            # malformed-envelope fallback.
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

        url = await self._resolve_stream_url()
        headers = self._build_stream_headers(
            api_key=api_key, session_id=session_id
        )
        body = self._build_stream_body(session_id=session_id, api_key=api_key)

        sent_terminal = False
        try:
            byte_iter = self._transport.stream_post(
                url=url,
                headers=headers,
                json_body=body,
                connect_timeout_s=self._config.connect_timeout_s,
                read_timeout_s=self._config.read_timeout_s,
            )
            async for ev in parse_sse_bytes(byte_iter):
                # Translate the SSE event into the upstream envelope
                # shape that ``_map_stream_frame`` expects.  Concrete
                # adapters override the mapper, but the wire-level
                # event name and JSON body are forwarded here so
                # subclasses can dispatch on either ``type`` (Anthropic)
                # or ``event`` (OpenCode).
                envelope = dict(ev.data)
                envelope.setdefault("type", ev.event)
                envelope.setdefault("event", ev.event)
                kind, payload = self._map_stream_frame(envelope=envelope)
                yield CodingStreamFrame(
                    kind=kind,
                    payload=payload,
                    sequence=self._next_sequence(session_id),
                )
                # 2-H11: a concrete adapter may synthesise sub-task frames
                # (task_started / task_notification) around a Task-tool
                # call/result it just mapped — Anthropic's HTTP SSE wire
                # never emits these natively, so the production path is the
                # adapter's responsibility (V1 ``session_manager.py:2044-
                # 2079`` synthesised them from SDK Task messages).
                for extra_kind, extra_payload in self._subtask_frames(
                    kind=kind, payload=payload
                ):
                    yield CodingStreamFrame(
                        kind=extra_kind,
                        payload=extra_payload,
                        sequence=self._next_sequence(session_id),
                    )
                if kind is StreamFrameKind.END:
                    sent_terminal = True
        except HttpStreamError as exc:
            logger.warning(
                "ai_coding.provider.stream_failed",
                provider=self._provider.value,
                session_id=str(session_id),
                error=str(exc),
            )
            # Surface the unified QaiError envelope ({type, code, message,
            # details}) — the same shape the no-key branch above and the
            # route contract (ai_coding.py docstring) require — so the
            # frontend ``isApiErrorPayload`` check builds a proper ApiError
            # and renders the real upstream failure (V1 parity: every error
            # reaches the user as a typed, friendly message instead of a
            # malformed-envelope fallback).
            err = InfrastructureError(
                code="ai_coding.upstream_stream_failed",
                message=str(exc),
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
