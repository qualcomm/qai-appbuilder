# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared async HTTP client for HTTP-based engines.

A thin wrapper over ``httpx.AsyncClient`` giving every keyless-scrape and keyed
-API engine one consistent transport: a browser-like navigation header set, a
hard per-request timeout, one transport-level retry (never on 4xx/5xx), and
uniform mapping of failures onto the engine error taxonomy so the health scorer
classifies them.

The browser-backed engine (Google) does NOT use this — it drives Playwright.
"""

from __future__ import annotations

import logging
import threading
from importlib.util import find_spec
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from collections.abc import Callable
from typing import Any

import httpx

from qai.platform.web_search.independent.errors import (
    EngineAuthError,
    EngineBlockedError,
    EngineHttpError,
    EngineTimeoutError,
    EngineTlsError,
    ssl_cause_in_chain,
)
from qai.platform.web_search.independent.url_utils import is_http_url

_LOGGER = logging.getLogger(__name__)


#: Query-parameter names whose VALUE is a credential. Matched case-insensitively
#: against the whole name, so ``api_key`` / ``apiKey`` / ``subscription-token``
#: all hit. Providers differ on where the secret goes: most use a header, but
#: SerpApi takes ``api_key`` in the query string — logging that URL verbatim
#: writes a live secret into the application log.
_SECRET_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "token",
        "access_token",
        "auth",
        "auth_token",
        "password",
        "secret",
        "subscription_token",
        "subscription-token",
        "exaapikey",
        "x-api-key",
    }
)

_REDACTED = "<redacted>"


def _redact_url(url: str) -> str:
    """Return ``url`` with credential-bearing query values replaced.

    Keeps the parameter NAME (knowing a key was sent is useful when reading the
    log) and drops only the value. Also strips userinfo from the netloc, which is
    the other place a secret can hide in a URL.

    Unparsable input degrades to a blanket redaction rather than risking a
    passthrough of the raw string.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return _REDACTED
    netloc = parts.netloc
    if "@" in netloc:
        # user:pass@host -> host (never log embedded credentials)
        netloc = netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [
                (k, _REDACTED if k.lower() in _SECRET_QUERY_KEYS else v)
                for k, v in pairs
            ]
        )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))

__all__ = [
    "HttpClient",
    "browser_headers",
    "get_search_ssl_verify",
    "set_search_ssl_verify",
    "set_search_proxy_provider",
]


class _SearchHttpConfig:
    """Injectable outbound-TLS config for the search engines' HTTP client.

    Mirrors the ``web`` web_fetch handler's ``set_ssl_verify`` seam: ``apps/api``
    installs the unified ``Settings.ssl_verify`` value here so this shared-kernel
    module stays config-source-agnostic (it must not import the config /
    security context).  Default ``False`` aligns with reference impl behaviour (Bun's fetch
    skips TLS verification by default) and avoids CERTIFICATE_VERIFY_FAILED in
    enterprise environments with TLS-intercepting gateways whose MITM CA the
    default trust store rejects.
    """

    __slots__ = ("ssl_verify",)

    def __init__(self) -> None:
        self.ssl_verify: bool = False


_CONFIG = _SearchHttpConfig()


def set_search_ssl_verify(value: bool) -> None:
    """Install the unified ``Settings.ssl_verify`` value (called by apps/api)."""
    _CONFIG.ssl_verify = bool(value)


def get_search_ssl_verify() -> bool:
    return _CONFIG.ssl_verify


_HARD_TIMEOUT_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 10.0

_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_PAYMENT_REQUIRED = 402
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300

# Dynamic per-request browser fingerprint. Bot-detection heavily penalizes a
# single static navigation header set repeated across every request; a
# ``browserforge``-generated, internally-consistent header set (UA + Accept +
# Accept-Language + sec-ch-ua all agreeing on one real browser/OS) blends in.
# ``browserforge`` is optional (the ``search`` extra); the import guard below
# falls back to a fixed desktop-Chrome set when it is not installed so a build
# without the dependency degrades gracefully instead of crashing.
try:  # pragma: no cover - import guard exercised only where the extra is missing
    from browserforge.headers import HeaderGenerator as _HeaderGenerator

    _BROWSERFORGE_IMPORT_OK = True
except ImportError:  # pragma: no cover - depends on install profile
    _HeaderGenerator = None  # type: ignore[assignment,misc]
    _BROWSERFORGE_IMPORT_OK = False

# Browsers/OSes the generator is allowed to mimic (desktop only — the scrapers
# request desktop SERPs).
_FINGERPRINT_BROWSERS = ("chrome", "firefox", "edge", "safari")
_FINGERPRINT_OS = ("windows", "macos", "linux")

# The fixed desktop-Chrome navigation fingerprint used as a fallback when
# ``browserforge`` is unavailable (import failed or the generator could not be
# built) or when a caller explicitly requests a stable identity.
_CHROME_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


class _HeaderGeneratorSingleton:
    """Lazily-built process-wide ``HeaderGenerator`` holder.

    Constructing a ``HeaderGenerator`` parses bundled fingerprint data and is
    not free, so it is built once and reused (mirrors the reference lazy
    ``getHeaderGenerator`` + ``generatorUnavailable`` pattern). A sticky
    ``_unavailable`` flag ensures a construction failure degrades to the static
    fallback without retrying; a lock keeps the one-time build thread-safe.
    """

    __slots__ = ("_generator", "_lock", "_unavailable")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generator: Any | None = None
        self._unavailable = False

    def get(self) -> Any | None:
        """Return the generator, building it once, or ``None`` if unusable."""
        if not _BROWSERFORGE_IMPORT_OK or self._unavailable:
            return None
        if self._generator is not None:
            return self._generator
        with self._lock:
            if self._generator is None and not self._unavailable:
                try:
                    self._generator = _HeaderGenerator(
                        browser=list(_FINGERPRINT_BROWSERS),
                        os=list(_FINGERPRINT_OS),
                        device="desktop",
                    )
                except Exception:  # noqa: BLE001 - any build failure ⇒ static fallback
                    self._unavailable = True
                    return None
            return self._generator


_GENERATOR = _HeaderGeneratorSingleton()


def _static_headers() -> dict[str, str]:
    """The fixed desktop-Chrome navigation header set (fallback / stable id)."""
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": (
            '"Google Chrome";v="129", "Chromium";v="129", ";Not A Brand";v="99"'
        ),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": _CHROME_FALLBACK_UA,
    }


def _dynamic_headers() -> dict[str, str] | None:
    """Generate a random, self-consistent browser fingerprint, or ``None``.

    Returns ``None`` when the generator is unavailable or produced a set with no
    ``User-Agent`` (so the caller falls back to the static set).
    """
    generator = _GENERATOR.get()
    if generator is None:
        return None
    try:
        generated = generator.generate()
    except Exception:  # noqa: BLE001 - any generation failure ⇒ static fallback
        return None
    if not generated or "User-Agent" not in generated:
        return None
    # ``generate()`` yields a plain str->str dict; copy so callers may mutate.
    return dict(generated)


def _decodable_encodings() -> str:
    """Return an ``Accept-Encoding`` listing ONLY codecs we can actually decode.

    Advertising a codec we cannot decode is silently catastrophic: the server
    honours it, ``httpx`` returns the still-compressed bytes as ``.text``, and
    the HTML parser finds zero elements — so the engine reports "0 hits" while
    looking perfectly healthy. Observed live: Baidu answered with
    ``content-encoding: br`` and 148 KB of undecoded binary that parsed to
    nothing, and the engine logged ``succeeded: 0 hits``.

    ``httpx`` only registers ``br`` / ``zstd`` when the optional ``brotli`` /
    ``brotlicffi`` / ``zstandard`` packages are importable, and ``browserforge``
    generates a *realistic* Chrome header set that names them unconditionally.
    So the advertised set is derived from what is importable right now. Deviating
    slightly from Chrome's exact list is a far smaller fingerprint cost than
    being unable to read the response body at all.
    """
    encodings = ["gzip", "deflate"]
    if find_spec("brotli") is not None or find_spec("brotlicffi") is not None:
        encodings.append("br")
    if find_spec("zstandard") is not None:
        encodings.append("zstd")
    return ", ".join(encodings)


#: Resolved once at import — the installed codec set cannot change mid-process.
_ACCEPT_ENCODING = _decodable_encodings()


def browser_headers(
    *, referer: str | None = None, randomized: bool = True
) -> dict[str, str]:
    """Return a browser-like navigation header set.

    With ``randomized=True`` (default) each call returns a fresh, internally
    consistent fingerprint from ``browserforge`` (UA/Accept/Accept-Language/
    sec-ch-ua all agreeing on one real browser/OS), defeating the static-header
    signature that trips bot detection. Pass ``randomized=False`` for the fixed
    desktop-Chrome set when a call needs a stable identity across requests. When
    ``browserforge`` is unavailable the static set is returned regardless.

    Every header set — static or generated — has its ``Accept-Encoding`` forced
    to the decodable subset (see :func:`_decodable_encodings`). This is the one
    funnel every engine's headers pass through, so overriding here covers the
    randomized path too; ``browserforge`` otherwise advertises ``br``/``zstd``
    whether or not those codecs are installed.
    """
    headers = _dynamic_headers() if randomized else None
    if headers is None:
        headers = _static_headers()
    # Drop any casing variant the generator produced before setting our own, so
    # a lower-case "accept-encoding" cannot shadow the canonical key.
    for key in [k for k in headers if k.lower() == "accept-encoding"]:
        del headers[key]
    headers["Accept-Encoding"] = _ACCEPT_ENCODING
    if referer is not None:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    return headers



#: Optional live proxy provider injected by the app wiring layer
#: (``apps/api/_chat_di.wire_web_search_tool_into_chat``). When set, it is
#: called on every new ``HttpClient`` creation so a runtime proxy change takes
#: effect on the next search without a restart. ``None`` means fall back to
#: ``proxy_helper`` (which is also fine once it has been initialised).
_search_proxy_provider: "Callable[[], str | None] | None" = None


def set_search_proxy_provider(provider: "Callable[[], str | None] | None") -> None:
    """Install a live proxy-URL provider for the search HTTP clients.

    Called once at app startup by the DI wiring layer. The provider is a
    zero-argument callable that reads the current proxy URL from the live
    Settings object at call time, so a runtime change in Settings → Proxy
    takes effect on the next search without a restart.
    """
    global _search_proxy_provider  # noqa: PLW0603
    _search_proxy_provider = provider
    _LOGGER.info(
        "web_search.proxy_provider_installed: provider=%s",
        "set" if provider is not None else "cleared",
    )


def _get_proxy_kwargs() -> dict[str, Any]:
    """Return httpx kwargs for the proxy configuration (if any).

    Priority:
    1. Live provider (``_search_proxy_provider``) — installed by the DI wiring
       layer; reads the current proxy at call time so runtime changes apply.
    2. Empty dict — direct connection (``trust_env=True`` still honours the
       ``HTTP_PROXY`` / ``HTTPS_PROXY`` env vars).

    A ``qai.chat.infrastructure.proxy_helper`` fallback used to sit between the
    two. It was removed because NOTHING in the application ever initialised that
    module: it unconditionally answered "no proxy", so the branch could only ever
    return an empty dict while implying a second source of proxy truth existed.
    """
    if _search_proxy_provider is None:
        return {}
    try:
        proxy_url = _search_proxy_provider()
    except Exception:  # noqa: BLE001 — proxy is optional; never break search
        return {}
    return {"proxy": proxy_url} if proxy_url else {}


class HttpClient:
    """Per-engine async HTTP facade with uniform error mapping.

    ``engine_id`` tags every raised :class:`EngineError` so the aggregator
    attributes penalties correctly. Construct one per engine instance; it owns
    a lazily-created ``httpx.AsyncClient``.
    """

    __slots__ = ("_client", "_client_factory", "_engine_id", "_last_proxy")

    def __init__(
        self,
        engine_id: str,
        *,
        client_factory: Any | None = None,
    ) -> None:
        self._engine_id = engine_id
        self._client: httpx.AsyncClient | None = None
        self._client_factory = client_factory
        self._last_proxy: str | None = None  # proxy URL at last client build

    def _get_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

        # For production clients, rebuild when the proxy setting has changed so
        # a runtime proxy toggle takes effect on the next request without a
        # restart. The check is cheap (one function call + string compare).
        proxy_kwargs = _get_proxy_kwargs()
        current_proxy = str(proxy_kwargs.get("proxy", ""))
        if self._client is None or current_proxy != self._last_proxy:
            if self._client is not None:
                # Close the stale client asynchronously-safe: schedule close
                # but do not await here (we are in a sync method). The old
                # client will be GC'd; connections drain naturally.
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(self._client.aclose())
                except Exception:  # noqa: BLE001
                    pass
            ssl_verify = get_search_ssl_verify()
            if proxy_kwargs.pop("verify", True) is False:
                ssl_verify = False
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _HARD_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS
                ),
                follow_redirects=True,
                verify=ssl_verify,
                trust_env=True,
                **proxy_kwargs,
            )
            self._last_proxy = current_proxy
            _LOGGER.info(
                "web_search.http_client.built: engine=%s proxy=%s ssl_verify=%s",
                self._engine_id,
                current_proxy or "(none — direct)",
                ssl_verify,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if _HTTP_OK_MIN <= status < _HTTP_OK_MAX:
            return
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise EngineAuthError(self._engine_id, f"authorization failed ({status})")
        if status == _HTTP_PAYMENT_REQUIRED:
            raise EngineAuthError(self._engine_id, "quota exhausted (402)")
        if status == _HTTP_TOO_MANY_REQUESTS:
            raise EngineBlockedError(self._engine_id, "rate limited (429)")
        raise EngineHttpError(self._engine_id, status)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Perform one request with a single transport-level retry.

        Retries once on ``httpx.TransportError`` (connection reset / DNS blip),
        never on an HTTP status. Maps timeouts and non-2xx onto engine errors.
        """
        client = self._get_client()
        last_transport_exc: httpx.TransportError | None = None
        for attempt in range(2):
            try:
                response = await client.request(
                    method, url, headers=headers, params=params, data=data, json=json
                )
            except httpx.TimeoutException as exc:
                raise EngineTimeoutError(
                    self._engine_id, "request timed out"
                ) from exc
            except httpx.TransportError as exc:
                # A TLS certificate-verify failure surfaces as a TransportError
                # (ConnectError) wrapping an ``ssl.SSLError`` in its __cause__.
                # It is deterministic — retrying cannot make an untrusted CA
                # trusted — so map it to EngineTlsError immediately and keep the
                # ssl exception on the __cause__ chain (``from exc``) so the
                # chat classifier can walk down to ssl.SSLCertVerificationError
                # and drive the existing "configure TLS and retry" flow.
                #
                # NOTE: the browser engine (google_browser) is unaffected — its
                # cert handling is Playwright's (``ignore_https_errors=not
                # verify``) and raises ``net::ERR_CERT_*`` Playwright errors,
                # not ``ssl.SSLError``; it never routes through this client.
                if ssl_cause_in_chain(exc) is not None:
                    raise EngineTlsError(
                        self._engine_id,
                        "TLS certificate verification failed",
                    ) from exc
                last_transport_exc = exc
                if attempt == 0:
                    continue
                raise
            else:
                # Per-request trace. The three fields below are exactly what was
                # needed to diagnose the "engine succeeded with 0 hits" class of
                # failure: the FINAL url (geo-redirects silently rewrite the
                # host), the status, and the content-encoding (an undecodable
                # codec yields a body that parses to nothing while the request
                # itself looks perfectly healthy).
                #
                # The URL MUST be redacted: some providers take the credential as
                # a query parameter (SerpApi's ``api_key``), so logging it raw
                # writes a live secret into the application log in plaintext.
                _LOGGER.info(
                    "engine %s <- %s %s | encoding=%s | %d bytes",
                    self._engine_id,
                    response.status_code,
                    _redact_url(str(response.url))[:200],
                    response.headers.get("content-encoding", "identity"),
                    len(response.content),
                )
                self._raise_for_status(response)
                return response
        # Unreachable: the loop either returns or raises, but satisfy the type
        # checker.
        raise last_transport_exc  # type: ignore[misc]

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def redirect_target(self, url: str) -> str | None:
        """Return the ``Location`` of a single redirect hop, without following it.

        Some engines hand back an opaque redirect wrapper instead of the real
        target (Baidu's ``/link?url=<token>``). Resolving it needs a request that
        (a) does NOT follow the redirect and (b) does NOT treat the 3xx as an
        error — neither of which :meth:`request` does, since every other caller
        wants the followed final response and a non-2xx mapped to an engine
        error.

        Lives here rather than in the engine so the lookup still inherits the
        shared proxy / TLS / timeout policy; an engine building its own client
        would silently bypass the corporate proxy and MITM CA handling.

        Returns ``None`` when the response is not a redirect, carries no usable
        ``Location``, or the request fails — callers treat this as best-effort.
        """
        client = self._get_client()
        try:
            response = await client.request(
                "HEAD", url, headers=browser_headers(), follow_redirects=False
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return None
        location = response.headers.get("location", "")
        return location if is_http_url(location) else None
