# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""QAI Service token pool — id_token exchange + process-wide JWT holder.

QAI Service is an OpenAI-compatible LLM broker that meters token usage per
*signed-in user* rather than per static API key. It exists for external
developers: instead of asking the user to obtain, paste and rotate a vendor
key, the app trades the Okta SSO identity the user already established for a
short-lived per-user credential.

Flow
----
1. The user completes the app's normal Okta SSO login (unchanged).
   ``interfaces/http/routes/auth.py`` verifies the ``id_token`` at
   ``/callback`` — historically it discarded the token right after.
2. We POST that verified ``id_token`` to ``{base_url}/api/auth/exchange``.
   The broker re-verifies it (same issuer/client_id the app uses) and mints
   its own session JWT (``expires_in`` ≈ 8 h).
3. :func:`store_service_jwt` keeps it here; the chat model-resolution bridge
   reads it via :func:`get_service_jwt` and sends it as the outbound
   ``Authorization`` bearer to ``{base_url}/v1/...``.

Design notes
------------
* **Persisted across restarts (OS-keyring backed).** The JWT is written to the
  platform :class:`SecretStore` under ``(service="qai.service.session",
  key="jwt")`` alongside its expiry, and read back on the first access after a
  process restart. This is the ONE thing that makes a backend restart
  transparent: the exchange only happens inside the Okta ``/callback``, so
  without persistence a restart drops the token while the browser's session
  cookie stays valid — the user looks signed in but the pool has no bearer
  until the next full login. An ~8 h credential is worth persisting precisely
  because it outlives a dev-loop restart; storing it in the OS keyring (not
  config, not logs) keeps it protected. A process with no SecretStore wired
  (CLI / test container) degrades to in-memory-only, i.e. the prior behaviour.
* **Wall-clock expiry.** Because the deadline must survive a process restart it
  is an absolute ``time.time()`` epoch, NOT ``time.monotonic()``. The tradeoff
  is deliberate: monotonic cannot be persisted (it resets every boot), so the
  historical "a system-clock change cannot resurrect an expired token"
  guarantee is given up. A large backward clock step could make an expired
  token read as live; the broker still rejects it upstream (401 → the user
  re-authenticates), so the failure mode is a clean auth error, not misuse.
* **A single value, not a per-user map.** This is a single-user desktop
  deployment — the same reasoning (and the same ``threading.Lock`` shape) as
  ``middleware/auth.py``'s ``_last_login_name`` holder. A per-user dict would
  be the multi-user machinery AGENTS.md explicitly calls dead weight here.
* **Expiry skew.** The stored deadline is pulled ``_EXPIRY_SKEW_SECONDS``
  earlier than the broker's own, so a token that would die mid-turn reads as
  already gone and the caller degrades cleanly instead of taking a surprise
  401 halfway through a streamed answer.
* **Exchange failure is loud, never fatal.** A user who signs in but has no
  broker account is a defect on our side, so the failure is logged at ERROR
  with the HTTP status for diagnosis. It still does not block the login: the
  local session is valid and every non-pool model keeps working.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

logger = get_logger(__name__)

__all__ = [
    "ExchangeError",
    "clear_service_jwt",
    "exchange_id_token",
    "get_service_jwt",
    "set_secret_store",
    "store_service_jwt",
]

#: Treat the JWT as expired this many seconds BEFORE the broker's own expiry,
#: so we fail to a clean "no token" state pre-emptively rather than sending a
#: credential that dies mid-request.
_EXPIRY_SKEW_SECONDS = 60.0

#: Fallback lifetime when the broker omits / malforms ``expires_in``. Matches
#: the observed 8 h issuance; deliberately not longer than the real one.
_DEFAULT_EXPIRES_IN = 28800

#: HTTP status the exchange endpoint returns on success.
_HTTP_OK = 200

#: SecretStore location the JWT is persisted at so it survives a process
#: restart (see the module docstring). ``key`` is fixed — a single-user
#: deployment holds one token.
_SECRET_SERVICE = "qai.service.session"
_SECRET_KEY = "jwt"

#: Process-wide holder: ``(jwt, deadline_epoch)`` or ``None``. Guarded by
#: ``_lock`` — the event loop and worker threads both touch it. The deadline is
#: an absolute ``time.time()`` epoch (NOT monotonic) so it can round-trip
#: through the SecretStore across restarts. Single value by design (single-user
#: app); see the module docstring.
_lock = threading.Lock()
_token: tuple[str, float] | None = None

#: Optional persistent backing store, injected once at startup by the
#: composition root (``apps/api/main.py`` after the DI container builds it).
#: ``None`` ⇒ in-memory-only, preserving the prior behaviour for a CLI / test
#: container that never mounts one. Guarded by ``_lock`` like ``_token``.
_secret_store: "SecretStore | None" = None
#: True once we have attempted to hydrate ``_token`` from the store, so the
#: keyring is read at most once per process (the hot path stays in memory).
_hydrated = False


def set_secret_store(store: "SecretStore | None") -> None:
    """Wire the persistent backing store (called once at startup).

    Idempotent and safe to call with ``None`` (keeps the in-memory behaviour).
    Injected rather than imported so this ``interfaces`` module never reaches
    into ``apps``/DI, and so tests can run without a keyring.
    """
    global _secret_store, _hydrated
    with _lock:
        _secret_store = store
        # Force a fresh hydrate attempt against the newly-wired store.
        _hydrated = False



class ExchangeError(RuntimeError):
    """Raised when QAI Service rejects or cannot process the id_token.

    Carries the HTTP ``status`` (0 for transport-level failures) so the caller
    can log a precise reason. Deliberately never carries the id_token or the
    minted JWT — this exception text reaches the log.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


async def exchange_id_token(
    id_token: str,
    *,
    base_url: str,
    ssl_verify: bool,
    timeout: float,
) -> tuple[str, int]:
    """Trade a verified Okta ``id_token`` for a QAI Service JWT.

    Returns ``(jwt, expires_in_seconds)``.

    Raises :class:`ExchangeError` on every failure path — 401 (invalid
    token), 403 (email domain not allowed), 400 (no email claim), 5xx,
    transport error, or a 200 whose body does not carry an
    ``access_token``. The ``id_token`` itself is never logged.
    """
    if not id_token:
        raise ExchangeError("empty id_token")
    origin = (base_url or "").rstrip("/")
    if not origin:
        raise ExchangeError("qai_service.base_url is not configured")

    url = f"{origin}/api/auth/exchange"
    try:
        async with httpx.AsyncClient(verify=ssl_verify, timeout=timeout) as client:
            response = await client.post(url, json={"id_token": id_token})
    except httpx.HTTPError as exc:
        # ``repr`` and not ``str``: httpx's transport exceptions frequently
        # carry EMPTY args (``ReadError()``, some ``RemoteProtocolError``), so
        # ``str(exc)`` renders as nothing at all and the log line degrades to
        # "transport error calling <url>: " — losing the one field that
        # identifies the failure mode, the exception TYPE. That distinction is
        # the whole diagnosis: ``ConnectError`` means nothing is listening or
        # the port is filtered, ``ReadError`` means the connection was reset
        # mid-response (request delivered, broker may have already minted and
        # charged a token), ``ProxyError`` means the env proxy is in the way,
        # ``ReadTimeout`` means the broker is slow. Each needs a different fix.
        raise ExchangeError(f"transport error calling {url}: {exc!r}") from exc

    if response.status_code != _HTTP_OK:
        # The broker distinguishes 401 invalid / 403 domain / 400 no-email,
        # each with a JSON ``detail``. Surface status + a short detail so an
        # operator can tell them apart without dumping the whole body.
        detail = ""
        try:
            payload: Any = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("error") or "")
        except ValueError:
            detail = response.text
        raise ExchangeError(
            f"exchange rejected (HTTP {response.status_code}): {detail[:200]}",
            status=response.status_code,
        )

    try:
        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", _DEFAULT_EXPIRES_IN))
    except (ValueError, KeyError, TypeError) as exc:
        raise ExchangeError(f"malformed exchange response: {exc}") from exc
    if not token:
        raise ExchangeError("exchange response carried an empty access_token")
    return token, expires_in


def _persist(jwt: str, deadline_epoch: float) -> None:
    """Best-effort write of the token + deadline to the SecretStore.

    Serialised as JSON so the absolute expiry travels with the token — without
    it a restart would recover a token but no way to know if it is still live.
    A store failure is swallowed (logged): the in-memory copy still works, so
    persistence failing degrades to the prior in-memory-only behaviour rather
    than breaking login.
    """
    store = _secret_store
    if store is None:
        return
    try:
        blob = json.dumps({"jwt": jwt, "deadline": deadline_epoch})
        store.set(_SECRET_SERVICE, _SECRET_KEY, blob)
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning("qai_service.token.persist_failed", error=str(exc))


def _load_from_store() -> tuple[str, float] | None:
    """Read the persisted ``(jwt, deadline_epoch)``, or ``None``.

    Returns ``None`` on every failure path — no record, unreadable store, or a
    malformed / expired blob — so a corrupt entry can never hand out a bad
    credential. Never raises: callers are on the chat hot path.
    """
    store = _secret_store
    if store is None:
        return None
    try:
        if not store.exists(_SECRET_SERVICE, _SECRET_KEY):
            return None
        raw = store.get(_SECRET_SERVICE, _SECRET_KEY)
    except Exception:  # noqa: BLE001 — a broken store is "no token"
        return None
    try:
        obj: Any = json.loads(raw)
        jwt = str(obj["jwt"])
        deadline = float(obj["deadline"])
    except (ValueError, KeyError, TypeError):
        # Malformed blob (schema drift / half-written / hand-edited). Drop it
        # so future reads do not keep re-parsing the same garbage on the hot
        # path; the operator recovers on the next successful exchange.
        _delete_from_store()
        return None
    if not jwt or time.time() >= deadline:
        # Expired / poisoned entry: drop it from the store so subsequent
        # reads short-circuit at ``exists()`` instead of parsing + re-judging
        # the same stale row on every hot-path call. Best-effort — a failure
        # to delete is not worth escalating; the next ``store_service_jwt``
        # write would overwrite it anyway.
        _delete_from_store()
        return None
    return (jwt, deadline)


def store_service_jwt(jwt: str, expires_in: int) -> None:
    """Hold ``jwt`` until ``expires_in`` (minus the safety skew) elapses.

    A blank token is ignored rather than stored as a poison value. Uses an
    absolute ``time.time`` deadline (not ``monotonic``) so the value can be
    persisted to the SecretStore and survive a process restart; see the module
    docstring on the wall-clock tradeoff. Writes through to the store so the
    next process start recovers it.
    """
    if not jwt:
        return
    lifetime = max(0.0, float(expires_in)) - _EXPIRY_SKEW_SECONDS
    deadline = time.time() + max(0.0, lifetime)
    global _token, _hydrated
    with _lock:
        _token = (jwt, deadline)
        _hydrated = True
        _persist(jwt, deadline)


def get_service_jwt() -> str | None:
    """Return the held JWT, or ``None`` when absent / past its deadline.

    On the first call after a process restart the in-memory holder is empty; we
    hydrate it once from the SecretStore (the keyring is read at most once, so
    the chat hot path stays in memory thereafter). An expired entry is dropped
    on read — in memory AND in the store — so a stale credential can never be
    handed out twice. Never raises: callers are on the chat hot path.
    """
    global _token, _hydrated
    with _lock:
        if _token is None and not _hydrated:
            _token = _load_from_store()
            _hydrated = True
        held = _token
        if held is None:
            return None
        jwt, deadline = held
        if time.time() >= deadline:
            _token = None
            _delete_from_store()
            return None
        return jwt


def clear_service_jwt() -> None:
    """Drop the held JWT (called on logout). Idempotent.

    Clears BOTH the in-memory copy and the persisted one: a logout means the
    identity the token was bound to is gone, so leaving an ~8 h bearer on disk
    would let the pool keep billing that user after they signed off — and would
    silently resurrect on the next start. ``_hydrated`` stays ``True`` so a
    subsequent read does not re-hydrate the just-deleted value.
    """
    global _token, _hydrated
    with _lock:
        _token = None
        _hydrated = True
        _delete_from_store()


def _delete_from_store() -> None:
    """Best-effort removal of the persisted token. Caller holds ``_lock``."""
    store = _secret_store
    if store is None:
        return
    try:
        if store.exists(_SECRET_SERVICE, _SECRET_KEY):
            store.delete(_SECRET_SERVICE, _SECRET_KEY)
    except Exception as exc:  # noqa: BLE001 — deletion is best-effort
        logger.warning("qai_service.token.delete_failed", error=str(exc))
