# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Engine-layer error taxonomy.

Every engine raises one of these on failure so the aggregator and the health
scorer can classify an outcome. The classification maps directly onto the
scoring event types (blocked / network / timeout / http_5xx / auth / unknown),
so a failure carries enough structure to decide how much to penalize an engine.

An engine returning an empty result list is NOT an error — it is a legitimate
"searched, found nothing" outcome (scored as a mild-positive ``empty``).
"""

from __future__ import annotations

import ssl

import httpx

__all__ = [
    "EngineAuthError",
    "EngineBlockedError",
    "EngineError",
    "EngineHttpError",
    "EngineTimeoutError",
    "EngineTlsError",
    "outcome_type_for",
    "ssl_cause_in_chain",
]

_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600


class EngineError(Exception):
    """Base class for any engine failure.

    ``engine_id`` identifies which engine failed so the scorer attributes the
    penalty correctly even inside the aggregator's parallel fan-out.
    """

    #: Scoring event type this error maps to (see :mod:`scoring`).
    outcome_type: str = "unknown"

    def __init__(self, engine_id: str, message: str) -> None:
        self.engine_id = engine_id
        super().__init__(f"[{engine_id}] {message}")


class EngineHttpError(EngineError):
    """A non-2xx HTTP response that is not a more specific class below.

    5xx maps to ``http_5xx`` (transient server fault); other unexpected
    statuses fall back to ``unknown``.
    """

    def __init__(self, engine_id: str, status: int, message: str = "") -> None:
        self.status = status
        server_error = _HTTP_SERVER_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MAX
        self.outcome_type = "http_5xx" if server_error else "unknown"
        super().__init__(engine_id, message or f"HTTP {status}")


class EngineBlockedError(EngineError):
    """The engine served a bot-detection / anti-scrape challenge.

    Distinct from a plain HTTP error because engines commonly return HTTP 200
    with a challenge body (CAPTCHA / "unusual traffic" / proof-of-work wall),
    which only the body reveals. Heavily penalized: a block is a design-level
    refusal that rarely clears within one scoring window.
    """

    outcome_type = "blocked"


class EngineAuthError(EngineError):
    """Credential rejected (HTTP 401/403 or an explicit quota/auth body).

    Cannot recover without the user fixing the credential, so it is the
    heaviest single penalty.
    """

    outcome_type = "auth"


class EngineTimeoutError(EngineError):
    """The engine did not respond within its hard per-request deadline."""

    outcome_type = "timeout"


class EngineTlsError(EngineError):
    """Outbound TLS certificate verification / handshake failure.

    Raised when an engine's HTTPS request fails because the server's
    certificate could not be verified (self-signed / unknown issuer / expired
    / hostname mismatch), which typically means an enterprise MITM gateway is
    in the path and ``Settings.ssl_verify`` is still on.

    Distinct from a plain ``network`` failure because the fix is deterministic
    and user-actionable (trust the CA or relax ``ssl_verify``), NOT a retry.
    The scorer treats every failure equally (``record_outcome`` does not weight
    by ``outcome_type``), so ``"tls"`` is used purely for logging / diagnostics
    and does not perturb health scoring.

    Crucially, the raiser MUST chain the originating ``ssl.SSLError`` via
    ``raise EngineTlsError(...) from ssl_exc`` so the ``__cause__`` chain still
    carries the ``ssl.SSLCertVerificationError``. The chat error classifier
    (:func:`qai.chat.infrastructure.llm_stream._classify_connect_error`) walks
    that chain to emit ``chat.llm.tls_cert_untrusted``, which drives the
    frontend's existing "configure TLS and retry" flow.
    """

    outcome_type = "tls"


def ssl_cause_in_chain(exc: BaseException) -> ssl.SSLError | None:
    """Return the first ``ssl.SSLError`` in ``exc``'s ``__cause__``/``__context__``
    chain (or ``exc`` itself), else ``None``.

    httpx wraps the underlying ``ssl.SSLCertVerificationError`` as the
    ``__cause__`` of its ``httpx.ConnectError``, so a cert-verify failure lives
    one hop below the transport exception. Walks with a ``seen`` guard so a
    self-referential ``__context__`` cannot loop forever.
    """
    seen: set[int] = set()
    queue: list[BaseException | None] = [exc]
    while queue:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return cur
        queue.append(cur.__cause__)
        queue.append(cur.__context__)
    return None


def outcome_type_for(exc: BaseException) -> str:
    """Map an exception to a scoring outcome type.

    Engine errors carry their own ``outcome_type``. Transport-level failures
    (connection reset / DNS failure / timeout) are classified from the httpx
    exception hierarchy; anything else is ``unknown``.

    A raw ``httpx.TransportError`` whose chain hides an ``ssl.SSLError``
    (cert-verify failure) is classified ``"tls"`` — even when it has not yet
    been mapped to :class:`EngineTlsError` — so the aggregator can surface the
    SSL cause for the chat TLS-retry flow.
    """
    if isinstance(exc, EngineError):
        return exc.outcome_type
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        # A cert-verify failure surfaces as ConnectError wrapping an
        # ssl.SSLError; prefer "tls" over the generic "network" so the cause
        # is actionable (trust the CA / relax ssl_verify), not a blind retry.
        if ssl_cause_in_chain(exc) is not None:
            return "tls"
        # ConnectError / ReadError / RST / DNS failure all live here.
        return "network"
    return "unknown"
