# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP catalog source: fetch + parse release manifest and model catalog.

Ports the V1 parsing/容错 logic from ``backend/version_manager.py``
(``_parse_release_manifest`` / ``_parse_version_list_text``) and
``backend/model_catalog_manager.py`` (``_parse_model_catalog``) verbatim
in behaviour, re-expressed against the ``service_release`` domain VOs.

Transport failures map to platform infrastructure errors; content/config
problems map to :class:`CatalogUnavailableError`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from qai.platform.errors import ExternalServiceError, InfrastructureError, TimeoutError_
from qai.service_release.application.ports import (
    DownloadSettingsPort,
    ServiceCatalogSourcePort,
)
from qai.service_release.domain.errors import CatalogUnavailableError
from qai.service_release.domain.value_objects import (
    CatalogModel,
    ModelFormat,
    ModelHardware,
    ModelVariant,
    ServicePackage,
    ServiceVersion,
)

_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
_MODEL_ID_RE = re.compile(r"^[\w\-\.]+$")
_VALID_FORMATS = {"qnn", "gguf", "mnn"}
_VALID_HARDWARE = {"npu", "gpu", "cpu"}

# Connect-phase ceiling (seconds). The configurable ``fetch_timeout_seconds``
# (default 15s) is the OVERALL/read budget, but a separate, short connect
# timeout is what prevents a long stall when the host is unreachable (corporate
# network that can't reach github.com). DNS resolution + TCP/TLS connect must
# fail fast; the read budget can stay generous for slow-but-reachable links.
_CONNECT_TIMEOUT_SECONDS = 5.0


class HttpCatalogSource(ServiceCatalogSourcePort):
    """Fetches manifests over HTTP using a settings-driven URL/timeout."""

    __slots__ = ("_settings", "_client_factory", "_proxy_provider")

    def __init__(
        self,
        *,
        settings: DownloadSettingsPort,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        proxy_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._settings = settings
        # NOTE: ``client_factory`` is retained for construction-compat but is no
        # longer used by ``_fetch_text`` — fetching now runs a SYNCHRONOUS
        # ``httpx.Client`` inside ``asyncio.to_thread`` to keep DNS/connect off
        # the event loop (see ``_fetch_text``). Kept so existing DI call sites
        # that pass it do not break.
        self._client_factory = client_factory or httpx.AsyncClient
        # H-2: returns the live global proxy URL (or None) at call time so a
        # runtime-config change hot-applies. Injected by the apps DI layer
        # (keeps service_release import-isolated from settings/chat).
        self._proxy_provider = proxy_provider

    def _proxy_kwargs(self) -> dict[str, object]:
        """Return ``{"proxy": url}`` when a global proxy is configured.

        H-2 — V1 ``model_catalog_manager.py:321-327`` routed catalog fetches
        through ``get_httpx_proxy_kwargs()``. The V2 ``global_proxy`` setting
        already carries any inline auth, so we pass it straight to httpx.
        """
        if self._proxy_provider is None:
            return {}
        try:
            proxy = (self._proxy_provider() or "").strip()
        except Exception:  # noqa: BLE001 - never break a fetch on proxy read
            proxy = ""
        return {"proxy": proxy} if proxy else {}

    async def _fetch_text(self, url: str, *, timeout: int, ssl_verify: bool) -> str:
        # ── Event-loop safety (root-cause fix) ──────────────────────────────
        # Although ``httpx.AsyncClient.get`` is awaitable, httpx performs the
        # initial DNS resolution (``getaddrinfo``) and TCP/TLS connect on the
        # CALLING thread — i.e. the asyncio event-loop thread. On a network
        # that cannot reach github.com (corporate / offline), that synchronous
        # getaddrinfo BLOCKS THE ENTIRE EVENT LOOP for seconds (observed
        # event-loop stalls of ~2.8s), freezing every endpoint
        # and the chat stream. Fetching with a SYNCHRONOUS ``httpx.Client``
        # inside ``asyncio.to_thread`` moves DNS + connect + read entirely onto
        # a worker thread, so the loop is never blocked no matter how slow or
        # unreachable the host is. A short connect timeout makes the worker
        # thread itself fail fast rather than pinning for the full read budget.
        proxy_kwargs = self._proxy_kwargs()

        def _blocking_fetch() -> tuple[int, str]:
            # ``httpx.Timeout(connect=…, read=…, write=…, pool=…)`` — bound the
            # connect phase tightly while allowing a generous read for slow but
            # reachable mirrors. Runs on a worker thread (off the loop).
            timeouts = httpx.Timeout(
                float(timeout),
                connect=_CONNECT_TIMEOUT_SECONDS,
            )
            with httpx.Client(
                timeout=timeouts,
                follow_redirects=True,
                verify=ssl_verify,
                **proxy_kwargs,
            ) as client:
                resp = client.get(url)
                # Read the body inside the worker thread too (resp.text would
                # otherwise lazily read on access — keep all IO off the loop).
                return resp.status_code, resp.text

        try:
            status_code, body = await asyncio.to_thread(_blocking_fetch)
        except httpx.ConnectError as exc:
            raise ExternalServiceError(
                "service_release.catalog_connect_error",
                f"Network error: cannot connect to {url}: {exc}",
                service="catalog_source",
                cause=exc,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError_(
                "service_release.catalog_timeout",
                f"Request timed out after {timeout}s for {url}",
                timeout_s=float(timeout),
            ) from exc
        except httpx.RequestError as exc:
            raise InfrastructureError(
                "service_release.catalog_request_error",
                f"HTTP request error: {exc}",
                cause=exc,
            ) from exc

        if status_code != 200:
            raise CatalogUnavailableError(
                f"Server returned HTTP {status_code} for catalog URL: {url}"
            )
        text = body.strip()
        if not text:
            raise CatalogUnavailableError("Catalog file is empty")
        return text

    # ── Service versions ───────────────────────────────────────────────

    async def fetch_service_versions(self) -> list[ServiceVersion]:
        cfg = await self._settings.read()
        url = (cfg.version_list_url or "").strip()
        if not url:
            raise CatalogUnavailableError(
                "version_check.version_list_url is not configured"
            )
        text = await self._fetch_text(
            url, timeout=cfg.fetch_timeout_seconds, ssl_verify=cfg.ssl_verify
        )
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CatalogUnavailableError(f"Invalid JSON manifest: {exc}") from exc
            entries = _parse_release_manifest(data)
        else:
            entries = _parse_version_list_text(text)
        if not entries:
            raise CatalogUnavailableError(
                "No valid versions found. Expected JSON release_manifest or "
                "plain-text '<version> <url>' lines."
            )
        return entries

    # ── Model catalog ──────────────────────────────────────────────────

    async def fetch_catalog_models(self) -> list[CatalogModel]:
        cfg = await self._settings.read()
        url = (cfg.catalog_url or "").strip()
        if not url:
            raise CatalogUnavailableError("model_catalog.catalog_url is not configured")
        text = await self._fetch_text(
            url, timeout=cfg.fetch_timeout_seconds, ssl_verify=cfg.ssl_verify
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogUnavailableError(f"Invalid JSON catalog: {exc}") from exc
        return _parse_model_catalog(data)


# ---------------------------------------------------------------------------
# Parsers (V1 behaviour parity)
# ---------------------------------------------------------------------------

#: Languages a localised-text mapping may carry. Used only to validate the
#: shape (the frontend resolves the active one); unknown keys are tolerated.
_KNOWN_LANGS = frozenset({"zh-CN", "en", "zh-TW"})


def _localized(value: Any) -> str | dict[str, str]:
    """Pass a translatable field through as ``str`` OR a ``{lang: str}`` map.

    A remote catalog/manifest may give a plain string (legacy / untranslated)
    or a per-language object. We keep a dict intact (so the frontend can pick
    the active UI language at render time) and coerce anything else to a
    trimmed string. A dict is normalised to ``{str: str}`` and dropped to a
    plain string only when it has no usable entries.
    """
    if isinstance(value, Mapping):
        out: dict[str, str] = {}
        for k, v in value.items():
            ks = str(k).strip()
            if ks and isinstance(v, str) and v:
                out[ks] = v
        return out if out else ""
    if value is None:
        return ""
    return str(value)


def _parse_packages(raw_packages: Any) -> tuple[ServicePackage, ...]:
    if not isinstance(raw_packages, list):
        return ()
    out: list[ServicePackage] = []
    for raw in raw_packages:
        if not isinstance(raw, Mapping):
            continue
        url = str(raw.get("download_url", "")).strip()
        if not url.lower().startswith("http"):
            continue
        out.append(
            ServicePackage(
                platform=str(raw.get("platform", "")),
                platform_id=str(raw.get("platform_id", "")),
                description=_localized(raw.get("description", "")),
                download_url=url,
                min_driver_version=str(raw.get("min_driver_version", "")),
                is_default=bool(raw.get("is_default", False)),
            )
        )
    return tuple(out)


def _parse_release_manifest(data: Any) -> list[ServiceVersion]:
    if not isinstance(data, Mapping):
        raise CatalogUnavailableError("manifest root must be a JSON object")
    releases = data.get("releases")
    if not isinstance(releases, list):
        raise CatalogUnavailableError("manifest 'releases' must be a list")
    out: list[ServiceVersion] = []
    seen: set[str] = set()
    for raw in releases:
        if not isinstance(raw, Mapping):
            continue
        version = str(raw.get("version", "")).strip()
        download_url = str(raw.get("download_url", "")).strip()
        packages = _parse_packages(raw.get("packages"))
        if not _VERSION_RE.match(version):
            continue
        # A version is valid if it has a top-level http url OR ≥1 package.
        if not download_url.lower().startswith("http") and not packages:
            continue
        if version in seen:
            continue
        seen.add(version)
        tags = tuple(str(t) for t in raw.get("tags", []) if isinstance(t, str))
        out.append(
            ServiceVersion(
                version=version,
                download_url=download_url,
                release_date=str(raw.get("release_date", "")),
                checksum_sha256=str(raw.get("checksum_sha256", "")),
                size_bytes=int(raw.get("size_bytes", 0) or 0),
                is_recommended=bool(raw.get("is_recommended", False)),
                min_driver_version=str(raw.get("min_driver_version", "")),
                changelog=_localized(raw.get("changelog", "")),
                tags=tags,
                description=_localized(raw.get("description", "")),
                packages=packages,
            )
        )
    return out


def _parse_version_list_text(text: str) -> list[ServiceVersion]:
    out: list[ServiceVersion] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        version, url = parts[0].strip(), parts[1].strip()
        if not _VERSION_RE.match(version) or not url.lower().startswith("http"):
            continue
        if version in seen:
            continue
        seen.add(version)
        out.append(ServiceVersion(version=version, download_url=url))
    return out


def _parse_variants(raw_variants: Any) -> tuple[ModelVariant, ...]:
    if not isinstance(raw_variants, list):
        return ()
    out: list[ModelVariant] = []
    for raw in raw_variants:
        if not isinstance(raw, Mapping):
            continue
        url = str(raw.get("download_url", "")).strip()
        if not url.lower().startswith("http"):
            continue
        out.append(
            ModelVariant(
                variant_id=str(raw.get("variant_id", "")),
                platform=str(raw.get("platform", "")),
                chip=str(raw.get("chip", "")),
                description=_localized(raw.get("description", "")),
                min_driver_version=str(raw.get("min_driver_version", "")),
                download_url=url,
                size_bytes=int(raw.get("size_bytes", 0) or 0),
                checksum_sha256=str(raw.get("checksum_sha256", "")),
            )
        )
    return tuple(out)


def _parse_model_catalog(data: Any) -> list[CatalogModel]:
    if not isinstance(data, Mapping):
        raise CatalogUnavailableError("catalog root must be a JSON object")
    models = data.get("models")
    if not isinstance(models, list):
        raise CatalogUnavailableError("catalog 'models' must be a list")
    out: list[CatalogModel] = []
    seen: set[str] = set()
    for raw in models:
        if not isinstance(raw, Mapping):
            continue
        model_id = str(raw.get("model_id", "")).strip()
        name = str(raw.get("name", "")).strip()
        fmt = str(raw.get("format", "")).strip().lower()
        hw = str(raw.get("hardware", "")).strip().lower()
        if not _MODEL_ID_RE.match(model_id) or not name:
            continue
        if fmt not in _VALID_FORMATS or hw not in _VALID_HARDWARE:
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        variants = _parse_variants(raw.get("variants"))
        download_url = str(raw.get("download_url", "")).strip()
        # V1: top-level url may be backfilled from variants[0] for the
        # legacy download API.
        if not download_url and variants:
            download_url = variants[0].download_url
        features = tuple(
            _localized(f)
            for f in raw.get("features", [])
            if isinstance(f, (str, Mapping))
        )
        tags = tuple(str(t) for t in raw.get("tags", []) if isinstance(t, str))
        out.append(
            CatalogModel(
                model_id=model_id,
                name=name,
                family=str(raw.get("family", "")),
                parameter_size=str(raw.get("parameter_size", "")),
                model_format=ModelFormat(fmt),
                hardware=ModelHardware(hw),
                context_length=int(raw.get("context_length", 0) or 0),
                download_url=download_url,
                model_type=str(raw.get("type", "llm")) or "llm",
                min_driver_version=str(raw.get("min_driver_version", "")),
                quantization=str(raw.get("quantization", "")),
                short_description=_localized(raw.get("short_description", "")),
                description=_localized(raw.get("description", "")),
                features=features,
                tags=tags,
                size_bytes=int(raw.get("size_bytes", 0) or 0),
                checksum_sha256=str(raw.get("checksum_sha256", "")),
                variants=variants,
            )
        )
    return out


__all__ = ["HttpCatalogSource"]
