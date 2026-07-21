# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ProbeCloudModelPermissionsUseCase`` — cheap, catalog-wide permission scan.

Motivation
----------
The chat model dropdown lists every model configured for every cloud provider
(see :class:`ListCloudModelsUseCase`), but a user's API key may only grant
access to a subset of them. Sending a turn to a denied model returns HTTP 403
mid-stream (see ``qai.chat.infrastructure.llm_stream``:722-723 → ``retry_disposition
= "never"``). Rather than surprise the user at send time, this use case scans
the catalog once at startup and records which (provider, model) pairs the
account actually has access to. The dropdown reads the snapshot and hides
denied models; unknown / allowed keep showing (never-preset-unavailable
principle).

Design
------
* **One request per provider, not per model** — the OpenAI-compatible
  ``GET /v1/models`` endpoint (already probed by :class:`ProbeProviderUseCase`
  via :class:`HttpProviderProbe`) returns the model ids the current key has
  access to. Comparing the returned set against the configured list yields the
  allowed / denied split with a single lightweight call per provider (no
  ``POST /v1/chat/completions`` needed → zero-billing probe).
* **Concurrency-bounded** — probes fan out under an :class:`asyncio.Semaphore`
  (default 3) so an install with many providers cannot storm the network.
* **Never raises** — every provider is guarded; a transport error, timeout, or
  malformed response keeps that provider's models at ``UNKNOWN`` (dropdown
  shows them). This use case is safe to schedule from ``lifespan`` without
  worrying about crashing the app.
* **Snapshot store** — results are written into an injected
  :class:`PermissionSnapshotStore` (in-memory dict wrapped for typing). Reset
  on process restart by design: the app never persists ``denied`` state to
  disk, so the next boot re-verifies.

Layer discipline
----------------
This module stays in the *application* layer: it imports only ports
(``ProviderRegistryPort`` / ``ProviderProbePort``) and the platform
``SecretStore`` (allowed by ``.importlinter`` layered-model_catalog +
context-isolation contracts — ``qai.**`` may import ``qai.platform.**``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from qai.model_catalog.application.ports import (
    ProviderProbePort,
    ProviderRegistryPort,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore


__all__ = [
    "PermissionStatus",
    "PermissionSnapshotStore",
    "InMemoryPermissionSnapshotStore",
    "ProbeCloudModelPermissionsUseCase",
]


#: SecretStore namespace shared with :class:`ProbeProviderUseCase`
#: (``qai.model_catalog.provider`` / ``<provider_id>``) — see the module
#: docstring on ``probe_provider.py``. Duplicated here (not imported) so the
#: two use cases stay decoupled at compile time.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"


class PermissionStatus(str, Enum):
    """Coarse per-(provider, model) permission status.

    Kept as a plain ``str`` enum so the wire form is trivially JSON-safe
    (the HTTP route just serialises ``.value``). ``UNKNOWN`` covers both
    "not probed yet" and "probe failed" — the dropdown treats both as
    "show" (never-preset-unavailable).
    """

    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    DENIED = "denied"


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


class PermissionSnapshotStore:
    """Port for the process-lifetime permission snapshot.

    Kept as a plain class (not a :class:`typing.Protocol`) because there is
    exactly one implementation (:class:`InMemoryPermissionSnapshotStore`) and
    the tests instantiate it directly. Domain-purity is unaffected: the store
    lives in the application layer.
    """

    def get(
        self, provider_id: str, model_id: str
    ) -> PermissionStatus:  # pragma: no cover — overridden
        raise NotImplementedError

    def get_snapshot(
        self,
    ) -> dict[str, dict[str, PermissionStatus]]:  # pragma: no cover
        raise NotImplementedError

    def replace(
        self,
        snapshot: Mapping[str, Mapping[str, PermissionStatus]],
    ) -> None:  # pragma: no cover — overridden
        raise NotImplementedError


class InMemoryPermissionSnapshotStore(PermissionSnapshotStore):
    """Thread-safe-enough in-memory snapshot (single-process app).

    A dict-of-dicts keyed by provider then model. Writes are atomic at the
    top level (whole snapshot replaced) so a concurrent reader never sees a
    half-updated view; individual point reads never mutate state.
    """

    __slots__ = ("_snapshot",)

    def __init__(self) -> None:
        self._snapshot: dict[str, dict[str, PermissionStatus]] = {}

    def get(self, provider_id: str, model_id: str) -> PermissionStatus:
        return self._snapshot.get(provider_id, {}).get(
            model_id, PermissionStatus.UNKNOWN
        )

    def get_snapshot(self) -> dict[str, dict[str, PermissionStatus]]:
        # Return a defensive copy so callers cannot mutate live state.
        return {p: dict(m) for p, m in self._snapshot.items()}

    def replace(
        self,
        snapshot: Mapping[str, Mapping[str, PermissionStatus]],
    ) -> None:
        self._snapshot = {
            p: dict(m) for p, m in snapshot.items()
        }


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeCloudModelPermissionsResult:
    """Aggregate result of one scan (returned for tests / manual refresh)."""

    snapshot: dict[str, dict[str, PermissionStatus]] = field(
        default_factory=dict
    )
    probed_providers: tuple[str, ...] = field(default_factory=tuple)
    skipped_providers: tuple[str, ...] = field(default_factory=tuple)


class ProbeCloudModelPermissionsUseCase:
    """Scan every configured cloud provider once and record per-model status.

    ``execute()`` iterates provider configs, probes each one via the injected
    :class:`ProviderProbePort` (one ``GET /v1/models``), and derives per-model
    :class:`PermissionStatus` from the returned model-id set. The snapshot is
    written to the store (whole-snapshot replace) so a concurrent reader
    always sees either the previous or the new view — never a partial one.

    The use case NEVER raises. Any per-provider failure keeps that provider's
    models at ``UNKNOWN``; the top-level ``execute()`` still returns
    successfully. This is a hard requirement for the ``lifespan`` background
    task — a permission scan must not crash the app.
    """

    #: Default cap on concurrent provider probes (network fairness).
    DEFAULT_CONCURRENCY: int = 3

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        probe: ProviderProbePort,
        store: PermissionSnapshotStore,
        secret_store: "SecretStore | None" = None,
        concurrency: int | None = None,
    ) -> None:
        self._registry = registry
        self._probe = probe
        self._store = store
        self._secret_store = secret_store
        self._concurrency = (
            self.DEFAULT_CONCURRENCY
            if concurrency is None or concurrency <= 0
            else int(concurrency)
        )

    async def execute(self) -> ProbeCloudModelPermissionsResult:
        rows = await self._safe_list_providers()
        if not rows:
            # No providers configured (external edition or fresh install): the
            # snapshot stays empty, the dropdown falls back to "show all".
            return ProbeCloudModelPermissionsResult()

        sem = asyncio.Semaphore(self._concurrency)
        tasks: list[asyncio.Task[tuple[str, dict[str, PermissionStatus] | None]]] = []
        for row in rows:
            provider_id = _read_provider_id(row)
            config = _read_config(row)
            if provider_id == "" or config is None:
                continue
            tasks.append(
                asyncio.create_task(
                    self._probe_one(sem, provider_id, config),
                    name=f"probe-permissions-{provider_id}",
                )
            )

        if not tasks:
            return ProbeCloudModelPermissionsResult()

        # ``return_exceptions=True`` is redundant (``_probe_one`` never raises)
        # but is a defence-in-depth guarantee so a future refactor cannot
        # accidentally propagate an exception up to lifespan.
        results = await asyncio.gather(*tasks, return_exceptions=True)

        snapshot: dict[str, dict[str, PermissionStatus]] = {}
        probed: list[str] = []
        skipped: list[str] = []
        for res in results:
            if isinstance(res, BaseException):
                # Defensive: _probe_one guarantees no exception, but if one
                # slips through the task result is dropped here.
                continue
            provider_id, per_model = res
            if per_model is None:
                skipped.append(provider_id)
                continue
            snapshot[provider_id] = per_model
            probed.append(provider_id)

        # Whole-snapshot replace: readers atomically see the new view.
        self._store.replace(snapshot)
        return ProbeCloudModelPermissionsResult(
            snapshot=snapshot,
            probed_providers=tuple(probed),
            skipped_providers=tuple(skipped),
        )

    # ── internals ──────────────────────────────────────────────────────

    async def _safe_list_providers(self) -> list[dict[str, object]]:
        """Return provider rows or ``[]`` on registry failure (never raises)."""
        try:
            rows = await self._registry.list_provider_configs()
        except Exception:  # noqa: BLE001 — snapshot must never break on registry error
            return []
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    async def _probe_one(
        self,
        sem: asyncio.Semaphore,
        provider_id: str,
        config: Mapping[str, object],
    ) -> tuple[str, dict[str, PermissionStatus] | None]:
        """Probe ONE provider and return its (allowed / denied / unknown) map.

        Returns ``(provider_id, None)`` when the provider is skipped (no
        api_key, no base_url, no configured models); otherwise returns the
        per-model status map. Never raises.
        """
        configured_ids = _configured_model_ids(config)
        if not configured_ids:
            return (provider_id, None)

        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            # Provider has no base_url configured → cannot probe → unknown.
            return (
                provider_id,
                {mid: PermissionStatus.UNKNOWN for mid in configured_ids},
            )

        api_key = self._resolve_key(provider_id)
        if not api_key:
            # No credential for this provider yet (e.g. qgenie first-launch
            # before the user pastes their API key). Skip: the "no key"
            # onboarding UI is the appropriate signal, not a "denied" mark.
            return (provider_id, None)

        async with sem:
            try:
                result = await self._probe.probe(
                    base_url=base_url, api_key=api_key
                )
            except Exception:  # noqa: BLE001 — probe must never break the scan
                return (
                    provider_id,
                    {mid: PermissionStatus.UNKNOWN for mid in configured_ids},
                )

        if not result.ok:
            # 403 on the probe itself means the KEY has no access at all →
            # every model configured under this provider is denied (rare;
            # normally an invalid key returns 401 which is a separate flow).
            if result.status == 403:
                return (
                    provider_id,
                    {mid: PermissionStatus.DENIED for mid in configured_ids},
                )
            # Any other failure (transport / 500 / malformed body) → unknown.
            return (
                provider_id,
                {mid: PermissionStatus.UNKNOWN for mid in configured_ids},
            )

        # Success: derive allowed / denied by comparing configured ids vs the
        # ids the upstream returned. The upstream MAY return either the raw
        # model id (``claude-4-8-opus``) OR the provider-prefixed id
        # (``anthropic::claude-4-8-opus``, matching how this app stores
        # ``model_id`` in the provider config). Match both forms so the diff
        # is stable across providers that follow either convention.
        returned = set(result.model_ids)
        if not returned:
            # Upstream returned an empty list — we cannot distinguish
            # allowed from denied. Fall back to unknown for safety
            # (never-preset-unavailable).
            return (
                provider_id,
                {mid: PermissionStatus.UNKNOWN for mid in configured_ids},
            )

        per_model: dict[str, PermissionStatus] = {}
        for configured in configured_ids:
            if _matches_returned(configured, returned):
                per_model[configured] = PermissionStatus.ALLOWED
            else:
                per_model[configured] = PermissionStatus.DENIED
        return (provider_id, per_model)

    def _resolve_key(self, provider_id: str) -> str | None:
        """Return the api_key for ``provider_id`` or ``None`` (mirrors
        :meth:`ProbeProviderUseCase._resolve_key`)."""
        store = self._secret_store
        if store is None:
            return None
        try:
            if not store.exists(_PROVIDER_SECRET_SERVICE, provider_id):
                return None
            value = store.get(_PROVIDER_SECRET_SERVICE, provider_id)
        except Exception:  # noqa: BLE001 — treat any failure as "no key"
            return None
        return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Row parsing helpers (module-level so tests can exercise them directly)
# ---------------------------------------------------------------------------


def _read_provider_id(row: Mapping[str, object]) -> str:
    value = row.get("provider_id")
    return value if isinstance(value, str) else ""


def _read_config(
    row: Mapping[str, object],
) -> Mapping[str, object] | None:
    config = row.get("config")
    return config if isinstance(config, Mapping) else None


def _configured_model_ids(config: Mapping[str, object]) -> list[str]:
    """Extract the ``model_id`` values from ``config["models"]`` (best-effort)."""
    models = config.get("models")
    if not isinstance(models, Iterable) or isinstance(models, (str, bytes)):
        return []
    out: list[str] = []
    for raw in models:
        if not isinstance(raw, Mapping):
            continue
        mid = raw.get("model_id")
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def _matches_returned(configured: str, returned: set[str]) -> bool:
    """Return True iff ``configured`` names a model in ``returned``.

    Handles both wire conventions the upstream may follow:

    * exact match (``anthropic::claude-4-8-opus`` in both places), and
    * suffix match after a provider-namespace prefix (``anthropic::claude-4-8-opus``
      is configured, upstream returns bare ``claude-4-8-opus``).

    Symmetric so ``configured`` and ``returned`` can each carry the prefix.
    """
    if configured in returned:
        return True
    # Try stripping our stored prefix (``anthropic::``, ``azure::``, ``vertexai::``,
    # ``query::``) → does the bare id appear? qgenie's TOML seed uses
    # ``<vendor>::<name>`` while some OpenAI-compat endpoints echo just the
    # bare ``<name>``.
    if "::" in configured:
        bare = configured.split("::", 1)[1]
        if bare and bare in returned:
            return True
    # Or: upstream returned ``<vendor>::<name>`` while our config stores the
    # bare name. Symmetric case.
    for ret in returned:
        if "::" in ret and ret.split("::", 1)[1] == configured:
            return True
    return False
