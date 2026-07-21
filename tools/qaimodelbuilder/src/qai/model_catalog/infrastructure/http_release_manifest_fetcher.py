# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP-backed :class:`ManifestFetcherPort` (PR-044).

Uses :mod:`httpx` (already a project-level dependency) to download the
release manifest JSON, decode it, and adapt it to a fully-validated
:class:`ReleaseManifest` aggregate.

Error mapping
-------------

Per the port docstring, the adapter classifies failures into two
buckets:

* **Transport errors** (DNS, connection refused, 5xx status, timeout)
  → :class:`InfrastructureError` so callers can retry.
* **Content errors** (malformed JSON, missing required fields, schema
  mismatch) → :class:`ReleaseManifestUnavailableError` so callers know
  the upstream cache is poisoned and a manual fix is needed.

Configuration
-------------

The fetcher takes the manifest URL plus optional timeout / retry knobs
in its constructor. Production wiring sources these from
``Settings.model_catalog`` (or sensible defaults if those settings
fields don't exist yet) so deployments can override the upstream URL
without code changes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from qai.platform.errors import (
    ExternalServiceError,
    InfrastructureError,
    TimeoutError_,
)
from qai.platform.time import Clock

from qai.model_catalog.domain.entities import (
    ReleaseManifest,
    ReleaseManifestEntry,
)
from qai.model_catalog.domain.errors import ReleaseManifestUnavailableError
from qai.model_catalog.domain.ids import ModelEntryId, ModelVersionId
from qai.model_catalog.domain.value_objects import (
    Checksum,
    ChecksumAlgorithm,
    SizeBytes,
    SourceUrl,
)


__all__ = ["HttpReleaseManifestFetcher"]


_DEFAULT_TIMEOUT_S: float = 10.0


class HttpReleaseManifestFetcher:
    """:class:`ManifestFetcherPort` backed by ``httpx.AsyncClient``."""

    __slots__ = (
        "_url",
        "_timeout_s",
        "_clock",
        "_client_factory",
        "_proxy_provider",
        "_ssl_verify_provider",
    )

    def __init__(
        self,
        *,
        url: str,
        clock: Clock,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client_factory: Any | None = None,
        proxy_provider: Callable[[], str | None] | None = None,
        ssl_verify_provider: Callable[[], bool] | None = None,
    ) -> None:
        """Create a fetcher rooted at ``url``.

        ``client_factory`` exists purely for testability — the production
        path leaves it ``None`` and we build a fresh
        :class:`httpx.AsyncClient` per request, which keeps the adapter
        stateless. Tests inject a callable that returns a
        :class:`httpx.AsyncClient` already wired to a
        :mod:`respx`/:mod:`httpx.MockTransport` mock so no real network
        I/O happens.

        ``proxy_provider`` (缺口 7 — global proxy mechanism B) is an
        optional zero-arg callable returning the live global-proxy URL
        (or ``None``). The release manifest is a *file download* class
        request, so it routes through the configured global proxy (V1
        parity: manifest fetches went through ``get_httpx_proxy_kwargs``).
        Read at request time so a runtime-config edit hot-applies; ``None``
        / empty → direct connection (proxy never forced; State-Truth-First).
        Wired by the apps DI layer so this adapter stays import-isolated
        from the settings object (context isolation §3.2). Ignored when a
        ``client_factory`` is supplied (the test already owns transport).
        """
        if not url:
            raise ValueError("url must be a non-empty string")
        self._url = url
        self._timeout_s = float(timeout_s)
        self._clock = clock
        self._client_factory = client_factory
        self._proxy_provider = proxy_provider
        # 缺口 fix — this fetcher previously hardcoded ``verify=False`` (ignored
        # the global SSL toggle). Route it through the live Settings.ssl_verify
        # provider (apps/api._global_proxy.build_ssl_verify_provider) so the
        # global toggle governs it. Read at request time so a runtime toggle
        # hot-applies; when unset we preserve the prior ``verify=False``.
        self._ssl_verify_provider = ssl_verify_provider

    async def fetch_latest(self) -> ReleaseManifest:
        try:
            async with self._open_client() as client:
                response = await client.get(self._url)
                response.raise_for_status()
                payload_text = response.text
        except httpx.TimeoutException as exc:
            raise TimeoutError_(
                "model_catalog.release_manifest.timeout",
                f"timed out fetching release manifest from {self._url!r}",
                timeout_s=self._timeout_s,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ExternalServiceError(
                "model_catalog.release_manifest.http_error",
                (
                    f"upstream returned {exc.response.status_code} for "
                    f"{self._url!r}"
                ),
                service="model_catalog.release_manifest",
                status=exc.response.status_code,
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise InfrastructureError(
                "model_catalog.release_manifest.transport_error",
                f"transport error fetching {self._url!r}: {exc}",
                cause=exc,
            ) from exc

        return self._decode(payload_text)

    # ── Internals ──────────────────────────────────────────────────────

    def _open_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            client = self._client_factory()
            if not isinstance(client, httpx.AsyncClient):  # pragma: no cover
                raise TypeError(
                    "client_factory must return httpx.AsyncClient; "
                    f"got {type(client).__name__}"
                )
            return client
        # Live read of the global SSL toggle (prior default preserved: no
        # provider → verify=False). A runtime toggle hot-applies to each fetch.
        verify = (
            self._ssl_verify_provider()
            if self._ssl_verify_provider is not None
            else False
        )
        return httpx.AsyncClient(
            timeout=self._timeout_s, verify=verify, **self._proxy_kwargs()
        )

    def _proxy_kwargs(self) -> dict[str, object]:
        """Return ``{"proxy": url}`` when a global proxy is configured.

        缺口 7 — the release manifest is a *file download* class request, so it
        routes through the mechanism-B global proxy (V1 parity). The provider
        is read at call time so a runtime-config edit hot-applies. Any embedded
        ``user:pass@`` auth is already spliced in by the provider
        (``apps.api._global_proxy.build_global_proxy_provider``). Empty / None
        → no proxy kwarg → direct connection (proxy never forced).
        """
        if self._proxy_provider is None:
            return {}
        try:
            proxy = (self._proxy_provider() or "").strip()
        except Exception:  # noqa: BLE001 — never break a fetch on proxy read
            proxy = ""
        return {"proxy": proxy} if proxy else {}

    def _decode(self, payload_text: str) -> ReleaseManifest:
        try:
            payload: dict[str, Any] = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ReleaseManifestUnavailableError(
                f"upstream manifest is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ReleaseManifestUnavailableError(
                "upstream manifest must decode to a JSON object"
            )

        manifest_version = payload.get("manifest_version")
        if not isinstance(manifest_version, str) or not manifest_version:
            raise ReleaseManifestUnavailableError(
                "manifest payload missing required str 'manifest_version'"
            )

        fetched_at_raw = payload.get("fetched_at")
        if isinstance(fetched_at_raw, str) and fetched_at_raw:
            try:
                fetched_at = datetime.fromisoformat(fetched_at_raw)
            except ValueError as exc:
                raise ReleaseManifestUnavailableError(
                    f"manifest 'fetched_at' is not ISO-8601: {exc}"
                ) from exc
        else:
            # Upstream has no ``fetched_at``; stamp the local clock.
            fetched_at = self._clock.now()

        entries_raw = payload.get("entries", [])
        if not isinstance(entries_raw, list):
            raise ReleaseManifestUnavailableError(
                "manifest 'entries' must be a JSON array"
            )

        entries: list[ReleaseManifestEntry] = []
        for idx, raw in enumerate(entries_raw):
            if not isinstance(raw, dict):
                raise ReleaseManifestUnavailableError(
                    f"manifest 'entries[{idx}]' is not a JSON object"
                )
            try:
                entries.append(self._decode_entry(raw))
            except (KeyError, ValueError, TypeError) as exc:
                raise ReleaseManifestUnavailableError(
                    f"manifest entry #{idx} is invalid: {exc}"
                ) from exc

        # ReleaseManifest enforces tz-awareness; ensure_aware happens in
        # ``__post_init__``.
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return ReleaseManifest(
            manifest_version=manifest_version,
            fetched_at=fetched_at,
            entries=tuple(entries),
        )

    @staticmethod
    def _decode_entry(raw: dict[str, Any]) -> ReleaseManifestEntry:
        model_id = str(raw["model_id"])
        version_id = str(raw["version_id"])
        algo_text = str(raw.get("checksum_algorithm", "sha256"))
        checksum_value = str(raw["checksum_value"])
        size_bytes = int(raw["size_bytes"])
        download_url = str(raw["download_url"])
        return ReleaseManifestEntry(
            model_id=ModelEntryId(model_id),
            version_id=ModelVersionId(version_id),
            checksum=Checksum(
                algorithm=ChecksumAlgorithm(algo_text),
                value=checksum_value,
            ),
            size_bytes=SizeBytes(value=size_bytes),
            download_url=SourceUrl(value=download_url),
        )
