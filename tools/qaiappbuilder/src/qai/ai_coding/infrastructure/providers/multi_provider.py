# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregating provider adapter (PR-046).

Combines individual :class:`HttpCodingProviderBase` instances into a
single :class:`CodingProviderPort` implementation.  The route layer
needs *one* provider port advertising *both* Claude Code and
OpenCode (the URL-prefix dispatch logic in
``interfaces/http/routes/ai_coding.py`` translates the prefix into a
:class:`Provider` value, which is then handed to the use case).

The aggregator routes each call to the underlying single-provider
adapter that advertises the requested :class:`Provider`.  If no
adapter advertises the requested provider the call surfaces
:class:`ProviderNotAvailableError` so the route layer produces a
clean ``409``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from qai.ai_coding.application.ports import CodingProviderPort
from qai.ai_coding.domain import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    MessageContent,
    Provider,
    ProviderNotAvailableError,
    Workspace,
)
from qai.platform.logging import get_logger

__all__ = ["MultiProviderCodingAdapter"]

logger = get_logger(__name__)

# Duck-typed read-only lookup: ``session_id_value -> Provider | None``.
# Wired at the apps/api layer to the session repository so the aggregator
# can recover a session's *persisted* provider when its in-memory ownership
# record is missing (process restart / session loaded from DB without a
# preceding ``spawn``).  Returns ``None`` when the session is unknown so the
# aggregator can surface a clean ``ProviderNotAvailableError`` instead of
# guessing.
SessionProviderLookup = Callable[[str], Awaitable["Provider | None"]]


class MultiProviderCodingAdapter:
    """Composite :class:`CodingProviderPort` over N single-provider adapters."""

    __slots__ = ("_by_provider", "_session_owner", "_provider_lookup")

    def __init__(
        self,
        *,
        providers: Iterable[CodingProviderPort],
        provider_lookup: SessionProviderLookup | None = None,
    ) -> None:
        self._by_provider: dict[Provider, CodingProviderPort] = {}
        for adapter in providers:
            for provider in adapter.available_providers():
                if provider in self._by_provider:
                    raise ValueError(
                        f"duplicate provider registration: {provider.value}"
                    )
                self._by_provider[provider] = adapter
        # Track which adapter owns which active session so the
        # streaming / send_message / terminate calls route correctly.
        self._session_owner: dict[str, CodingProviderPort] = {}
        # Optional read-only callback that resolves a session id to its
        # *persisted* provider.  When the in-memory owner record is
        # missing (process restart / session loaded from DB without a
        # preceding ``spawn`` / ``bind_session``) the aggregator falls
        # back to this lookup rather than blindly picking the first
        # registered adapter — which would mis-route e.g. an OpenCode
        # session to the Claude Code adapter and surface a spurious
        # ``API key for provider claude_code not configured`` error.
        self._provider_lookup = provider_lookup

    def available_providers(self) -> Iterable[Provider]:
        return tuple(self._by_provider.keys())

    async def available_models(
        self, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Aggregate model catalogs from sub-adapters (U-6).

        Duck-typed: only sub-adapters that advertise an
        ``available_models`` method contribute rows.  Each sub-adapter
        is best-effort (e.g. OpenCode returns ``[]`` when its local
        service is offline); a failing sub-adapter never aborts the
        aggregate.  The result is the flat concatenation of every
        sub-adapter's rows, de-duplicated across adapters that may both
        register the same provider/model pair (shouldn't happen given
        the duplicate-registration guard, but cheap to keep stable).

        ``force_refresh`` (C1) is forwarded to sub-adapters that accept
        it (the CC adapter bypasses its 5-minute upstream cache);
        adapters predating C1 ignore it.
        """
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, Any]] = []
        for adapter in dict.fromkeys(self._by_provider.values()):
            fn = getattr(adapter, "available_models", None)
            if not callable(fn):
                continue
            try:
                try:
                    result = fn(force_refresh=force_refresh)
                except TypeError:
                    result = fn()
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[misc]
            except Exception:  # noqa: BLE001 — best-effort per sub-adapter.
                continue
            if not isinstance(result, list):
                continue
            for entry in result:
                if not isinstance(entry, dict):
                    continue
                key = (
                    str(entry.get("provider_id", "")),
                    str(entry.get("id", "")),
                )
                if not key[1] or key in seen:
                    continue
                seen.add(key)
                rows.append(dict(entry))
        return rows

    async def is_available(self, *, provider: Provider) -> bool:
        """Forward the provider liveness probe (P1-5).

        Sub-adapter contract (duck-typed): ``async def is_available()``
        returns ``True`` when the underlying backend is reachable.
        Adapters predating P1-5 (e.g. the Claude Code adapter, which
        relies on the SDK probe injected at the apps/api layer) do not
        implement the method; for them we fall back to the static
        ``available_providers()`` advertisement so existing behaviour
        is preserved (matches V1: CC has no analogous HTTP health
        endpoint and so its ``available`` flag stays static).

        Best-effort: any sub-adapter exception collapses into ``False``.
        """
        adapter = self._by_provider.get(provider)
        if adapter is None:
            return False
        probe = getattr(adapter, "is_available", None)
        if not callable(probe):
            return provider in self.available_providers()
        try:
            result = probe()
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — best-effort per sub-adapter.
            return False
        return bool(result)

    async def model_source_meta(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Forward the V1 model-source badge metadata (C1).

        Returns the first sub-adapter's ``model_source_meta`` (in
        practice the Claude Code adapter — OpenCode has no upstream
        ``/v1/models`` enumeration).  Empty dict when no sub-adapter
        advertises the probe.
        """
        for adapter in dict.fromkeys(self._by_provider.values()):
            fn = getattr(adapter, "model_source_meta", None)
            if not callable(fn):
                continue
            try:
                try:
                    result = fn(force_refresh=force_refresh)
                except TypeError:
                    result = fn()
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[misc]
            except Exception:  # noqa: BLE001 — best-effort.
                continue
            if isinstance(result, dict):
                return result
        return {}

    async def spawn(
        self,
        *,
        provider: Provider,
        workspace: Workspace,
        initial_prompt: MessageContent | None,
        session_id: CodingSessionId | None = None,
        config: CodingSessionConfig | None = None,
    ) -> dict[str, Any]:
        adapter = self._require_adapter(provider)
        result = await adapter.spawn(
            provider=provider,
            workspace=workspace,
            initial_prompt=initial_prompt,
            session_id=session_id,
            config=config,
        )
        # PR-107: when the use case pre-allocates the session id we
        # can bind ownership immediately so subsequent stream /
        # send_message / terminate calls don't fall back to the
        # "first registered adapter" heuristic.
        if session_id is not None:
            self._session_owner[session_id.value] = adapter
        # The use case generates the session id immediately after this
        # call returns, so historical (PR-046) call sites still rely
        # on ``bind_session`` for late binding.
        return result

    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        adapter = await self._resolve_owner(session_id)
        return await adapter.stream(session_id=session_id)

    async def send_message(
        self,
        *,
        session_id: CodingSessionId,
        content: MessageContent,
    ) -> None:
        adapter = await self._resolve_owner(session_id)
        await adapter.send_message(session_id=session_id, content=content)

    async def terminate(self, *, session_id: CodingSessionId) -> None:
        adapter = self._session_owner.pop(session_id.value, None)
        if adapter is None:
            # Idempotent — also fan out to all adapters so downstream
            # state (per-session dicts) is cleaned up regardless of
            # which adapter actually held the handle.
            for a in self._by_provider.values():
                await a.terminate(session_id=session_id)
            return
        await adapter.terminate(session_id=session_id)

    async def abort(self, *, session_id: CodingSessionId) -> bool:
        """Route a hard-abort to the owning adapter (2-H4).

        Resolves the session's owning adapter (the persisted-provider
        lookup recovers it across a process restart) and delegates to
        its ``abort`` — the OpenCode adapter calls its native
        ``/session/{id}/abort`` endpoint while the Claude Code adapter
        falls back to ``terminate``.  Mirrors :meth:`terminate`'s
        owner-pop semantics so the local handle is released afterwards.
        Best-effort: a missing owner fans the local terminate out to all
        adapters and reports ``False``.
        """
        adapter = self._session_owner.get(session_id.value)
        if adapter is None:
            try:
                adapter = await self._resolve_owner(session_id)
            except ProviderNotAvailableError:
                adapter = None
        if adapter is None:
            for a in self._by_provider.values():
                await a.terminate(session_id=session_id)
            return False
        abort = getattr(adapter, "abort", None)
        if callable(abort):
            issued = await abort(session_id=session_id)
        else:  # pragma: no cover — all bundled adapters define abort.
            await adapter.terminate(session_id=session_id)
            issued = False
        self._session_owner.pop(session_id.value, None)
        return bool(issued)

    async def rewind_files(
        self,
        *,
        session_id: CodingSessionId,
        marker_index: int,
    ) -> bool:
        """Route a file-rewind to the owning adapter (2-H3).

        Resolves the session's owning adapter (persisted-provider
        lookup recovers it across a restart) and delegates to its
        ``rewind_files`` — OpenCode issues the native
        ``/session/{id}/revert`` while the Claude Code skeleton returns
        ``False`` (message-only rewind until the SDK harness lands).
        Best-effort: an unknown session returns ``False`` without
        touching adapter state (unlike abort/terminate this is a
        read-side rollback, so we do NOT pop the owner).
        """
        adapter = self._session_owner.get(session_id.value)
        if adapter is None:
            try:
                adapter = await self._resolve_owner(session_id)
            except ProviderNotAvailableError:
                return False
        rewind = getattr(adapter, "rewind_files", None)
        if not callable(rewind):  # pragma: no cover — bundled adapters define it.
            return False
        return bool(
            await rewind(session_id=session_id, marker_index=marker_index)
        )

    async def forward_permission_decision(
        self,
        *,
        session_id: CodingSessionId,
        request_id: object,
        decision: object,
        updated_input: dict[str, object] | None = None,
        updated_permissions: list[dict[str, object]] | None = None,
    ) -> None:
        """Route a permission decision (+ edits) to the owning adapter (2-H9).

        Resolves the session's owning adapter and delegates to its
        ``forward_permission_decision`` so the operator's
        ``updated_input`` / ``updated_permissions`` are queued for the
        next stream resume (V1 in-flight ``can_use_tool`` replay parity).
        Best-effort: an unknown session / an adapter without the hook is
        a silent no-op (the aggregate decision already persisted).
        """
        adapter = self._session_owner.get(session_id.value)
        if adapter is None:
            try:
                adapter = await self._resolve_owner(session_id)
            except ProviderNotAvailableError:
                return
        forward = getattr(adapter, "forward_permission_decision", None)
        if callable(forward):
            await forward(
                session_id=session_id,
                request_id=request_id,
                decision=decision,
                updated_input=updated_input,
                updated_permissions=updated_permissions,
            )

    async def fork_session(self, *, session_id: CodingSessionId) -> bool:
        """Route a fork to the owning adapter (2-H6).

        Resolves the session's owning adapter and delegates to its
        ``fork_session`` so the cached upstream id is dropped and the
        next turn forks a fresh backend conversation.  Best-effort: an
        unknown session / an adapter without the hook reports ``False``.
        """
        adapter = self._session_owner.get(session_id.value)
        if adapter is None:
            try:
                adapter = await self._resolve_owner(session_id)
            except ProviderNotAvailableError:
                return False
        fork = getattr(adapter, "fork_session", None)
        if not callable(fork):
            return False
        return bool(await fork(session_id=session_id))

    def bind_session(
        self, *, session_id: CodingSessionId, provider: Provider
    ) -> None:
        """Tag ``session_id`` as owned by the adapter for ``provider``.

        The application layer calls this immediately after a
        successful :meth:`spawn` so subsequent streaming / message /
        terminate calls can be routed without a per-call lookup.
        """
        adapter = self._require_adapter(provider)
        self._session_owner[session_id.value] = adapter

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_adapter(self, provider: Provider) -> CodingProviderPort:
        adapter = self._by_provider.get(provider)
        if adapter is None:
            raise ProviderNotAvailableError(
                message=f"provider {provider.value} not available",
                details={"provider": provider.value},
            )
        return adapter

    async def _resolve_owner(
        self, session_id: CodingSessionId
    ) -> CodingProviderPort:
        adapter = self._session_owner.get(session_id.value)
        if adapter is not None:
            return adapter
        if not self._by_provider:
            raise ProviderNotAvailableError(
                message="no coding providers registered",
                details={"session_id": str(session_id)},
            )
        # In-memory ownership missed.  Recover the session's *persisted*
        # provider via the injected lookup (e.g. after a process restart
        # the DB still records ``open_code`` even though no ``spawn`` ran
        # in this process).  Route to that adapter and memoise the
        # ownership so subsequent calls in this process skip the lookup.
        provider = await self._lookup_provider(session_id)
        if provider is not None:
            owner = self._by_provider.get(provider)
            if owner is not None:
                self._session_owner[session_id.value] = owner
                return owner
            # The session's persisted provider is not registered in this
            # deployment — fail clearly rather than mis-routing.
            raise ProviderNotAvailableError(
                message=f"provider {provider.value} not available",
                details={
                    "provider": provider.value,
                    "session_id": str(session_id),
                },
            )
        # No lookup wired, or the session is unknown to the repository.
        # A single-provider deployment can still route unambiguously to
        # its sole adapter; with N>1 providers we refuse to guess (the
        # old "first registered adapter" heuristic mis-routed OC sessions
        # to the CC adapter).
        if len(self._by_provider) == 1:
            return next(iter(self._by_provider.values()))
        logger.warning(
            "ai_coding.multi_provider.unresolved_session_owner",
            session_id=str(session_id),
            registered_providers=[p.value for p in self._by_provider],
        )
        raise ProviderNotAvailableError(
            message=(
                "cannot resolve provider for session "
                f"{session_id.value}: no in-memory owner and the session "
                "is not persisted"
            ),
            details={"session_id": str(session_id)},
        )

    async def _lookup_provider(
        self, session_id: CodingSessionId
    ) -> Provider | None:
        """Resolve a session's persisted provider via the injected lookup.

        Best-effort: a missing lookup or any lookup failure collapses to
        ``None`` so the caller falls through to its own resolution rules
        rather than crashing the stream / send path.
        """
        lookup = self._provider_lookup
        if lookup is None:
            return None
        try:
            return await lookup(session_id.value)
        except Exception as exc:  # noqa: BLE001 — best-effort recovery.
            logger.warning(
                "ai_coding.multi_provider.provider_lookup_failed",
                session_id=str(session_id),
                error=repr(exc),
            )
            return None
