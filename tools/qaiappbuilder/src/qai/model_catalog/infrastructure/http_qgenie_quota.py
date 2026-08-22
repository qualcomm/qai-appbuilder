# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP-backed :class:`QGenieQuotaPort`.

Reads the two endpoints that together describe a QGenie account's standing:

* ``GET {host}/v1/rate-limit/usage`` — every model x both traffic classes x
  four rate-limit windows, each with ``limit``/``used``/``remaining``. One
  call covers the whole account (~30 KB, measured ~860 ms), which is why the
  per-model endpoint is never used: these endpoints share a ~60 s rate limit,
  so fetching 40 models individually would take 40 minutes.
* ``GET {host}/v1/usage/cost`` — account spend split by traffic class.

Three upstream behaviours shape this adapter:

1. **The rate limit answers HTTP 429 with a plain-text body.** Exceeding it
   returns ``Rate limit exceeded for usage endpoint. Try again in 60 seconds.``
   as ``text/plain``, so the status check rejects it before any parse is
   attempted; a non-JSON body is treated the same way. Either degrades to the
   cached snapshot.
2. **All three endpoints share that one limit budget.** A refresh spends
   three calls (usage + cost + cost-cap), so back-to-back refreshes (a user
   sending messages faster than the cooldown) will hit it — hence
   :data:`_MIN_REFRESH_INTERVAL_S` and the cache.
3. **All three known hosts are equivalent.** ``qgenie-chat``, ``qgenie-api``
   and ``qpilot-api`` were each verified to serve all four endpoints and to
   return an identical 34-model set, so the quota host is derived from the
   provider's configured ``base_url`` rather than hard-coded. Whatever the user
   points chat at also answers quota questions.

Never raises: a caller rendering a sidebar gauge must not have to guard, and a
quota read failing is never a reason to interrupt a chat.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from qai.model_catalog.domain.qgenie_quota import (
    QGENIE_ERR_BAD_BASE_URL,
    QGENIE_ERR_NO_API_KEY,
    QGENIE_ERR_RATE_LIMITED,
    QGENIE_ERR_UNREACHABLE,
    QGenieBucket,
    QGenieCost,
    QGenieCounter,
    QGenieModelQuota,
    QGenieQuotaSnapshot,
    TrafficClass,
)

__all__ = ["HttpQGenieQuota"]

_DEFAULT_TIMEOUT_S: Final[float] = 20.0

#: Shortest gap between real upstream reads.
#:
#: The endpoints enforce roughly 60 s and a refresh spends two of them, so 90 s
#: leaves headroom for the forced read the exhaustion check needs plus a second
#: window the user may have open. Daily counters are slow-moving; finer
#: granularity would buy nothing and risk getting throttled exactly when the
#: answer matters.
_MIN_REFRESH_INTERVAL_S: Final[float] = 90.0

_USAGE_PATH: Final[str] = "/v1/rate-limit/usage"
_COST_PATH: Final[str] = "/v1/usage/cost"
_COST_CAP_PATH: Final[str] = "/v1/rate-limit/cost-cap/usage"

# Upstream window key -> our field name.
_WINDOW_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("tokens_per_day", "day"),
    ("tokens_per_week", "week"),
    ("requests_per_minute", "rpm"),
    ("tokens_per_minute", "tpm"),
)


def _identity(base_url: str, api_key: str) -> tuple[str, str]:
    """Stable identity for a cached snapshot: which gateway, whose credential.

    The key is HASHED rather than stored: the tuple only ever needs to support
    equality, and keeping the plaintext on a long-lived process singleton would
    put a credential somewhere a heap dump or a debugger frame could surface it.
    """
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return (base_url.strip().rstrip("/"), digest)


class HttpQGenieQuota:
    """:class:`QGenieQuotaPort` backed by ``httpx.AsyncClient``."""

    __slots__ = (
        "_cache",
        "_cache_key",
        "_client_factory",
        "_fetched_at",
        "_lock",
        "_ssl_verify",
        "_ssl_verify_provider",
        "_timeout_s",
    )

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        ssl_verify: bool = True,
        ssl_verify_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._client_factory = client_factory
        self._ssl_verify = ssl_verify
        # Live ``Settings.ssl_verify`` provider (apps/api._global_proxy
        # .build_ssl_verify_provider). Read at client-build time so a runtime
        # SSL toggle hot-applies; the frozen bool is the back-compat fallback.
        # Matters here because the internal edition talks to QGenie through an
        # enterprise MITM gateway whose cert would otherwise fail verification.
        self._ssl_verify_provider = ssl_verify_provider
        self._cache: QGenieQuotaSnapshot | None = None
        self._fetched_at: float = 0.0
        # Identity the cached snapshot belongs to.
        #
        # Without it, changing the QGenie key or base_url would keep serving the
        # PREVIOUS account's figures for up to the cooldown — showing one
        # account's spend under another's credential. Compared, never logged;
        # the key is hashed so a snapshot of this object cannot leak it.
        self._cache_key: tuple[str, str] | None = None
        # Serialises refreshes. Both refresh triggers (switching onto a QGenie
        # model, and the end of a turn) can land together — switch models then
        # immediately send — and each refresh spends three calls against a
        # ~60 s budget. Without this, two concurrent callers would burn six and
        # guarantee the throttling this cache exists to avoid, and the slower
        # one could overwrite the newer snapshot (making ``fetched_at`` go
        # backwards in a UI that shows it).
        self._lock = asyncio.Lock()

    async def fetch(
        self, *, base_url: str, api_key: str | None, force: bool = False
    ) -> QGenieQuotaSnapshot:
        if not api_key:
            # No credential configured: report unavailable rather than
            # inventing zeroes, so the gauge hides instead of claiming the
            # account is exhausted. Named so the UI can point at the fix
            # (paste a key) instead of leaving the user guessing.
            return QGenieQuotaSnapshot(error=QGENIE_ERR_NO_API_KEY)

        identity = _identity(base_url, api_key)
        if not force and self._is_fresh(identity):
            assert self._cache is not None  # noqa: S101 — narrowed by _is_fresh
            return self._cache

        host = _quota_host(base_url)
        if not host:
            return self._stale_or_empty(identity, QGENIE_ERR_BAD_BASE_URL)

        async with self._lock:
            # Re-check under the lock: a caller that queued behind a refresh
            # should use its result rather than spend three more upstream calls
            # on the same question. `force` still re-reads — its whole purpose
            # is to guarantee the exhaustion check sees current numbers.
            if not force and self._is_fresh(identity):
                assert self._cache is not None  # noqa: S101 — narrowed above
                return self._cache

            headers = {"x-api-key": api_key, "Accept": "application/json"}
            try:
                client = self._open_client()
                async with client:
                    usage_raw, usage_err = await _get_json(
                        client, host + _USAGE_PATH, headers
                    )
                    if usage_raw is None:
                        # Throttled or malformed: keep showing the last good
                        # read instead of blanking a gauge the user was
                        # watching, and say WHY it is old.
                        return self._stale_or_empty(identity, usage_err)
                    cost_raw, _ = await _get_json(
                        client, host + _COST_PATH, headers
                    )
                    cap_raw, _ = await _get_json(
                        client, host + _COST_CAP_PATH, headers
                    )
            except Exception:  # noqa: BLE001 — a read must never break chat
                return self._stale_or_empty(identity, QGENIE_ERR_UNREACHABLE)

            # Cost is fetched AFTER usage and shares the same throttle budget,
            # so it is the first thing to be refused. When it is, carry the last
            # known spend forward instead of publishing the zeroes a "no data"
            # parse produces: a tooltip reading "today $0.00" is a wrong number
            # presented as authoritative, which is worse than a slightly old
            # one. Merged per-field rather than snapshot-wide because the usage
            # half DID come back fresh and must not be held back by cost.
            #
            # Only carried forward for the SAME account: reusing another
            # credential's spend would attribute one user's money to another.
            cost = _parse_cost(cost_raw, cap_raw)
            if cost is None and self._cache_key == identity:
                cost = self._cache.cost if self._cache is not None else None

            snapshot = QGenieQuotaSnapshot(
                user=str(usage_raw.get("user") or ""),
                fetched_at=datetime.now(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                stale=False,
                models=_parse_models(usage_raw),
                cost=cost,
            )
            self._cache = snapshot
            self._cache_key = identity
            self._fetched_at = time.monotonic()
            return snapshot

    def _is_fresh(self, identity: tuple[str, str]) -> bool:
        """Whether a cached snapshot for THIS identity is inside the cooldown.

        Identity-checked so a changed key / base_url forces a real read instead
        of serving the previous account's numbers under the new credential.
        """
        if self._cache is None or self._cache_key != identity:
            return False
        return (time.monotonic() - self._fetched_at) < _MIN_REFRESH_INTERVAL_S

    def _stale_or_empty(
        self,
        identity: tuple[str, str] | None = None,
        error: str | None = None,
    ) -> QGenieQuotaSnapshot:
        """Serve the last good snapshot, flagged stale, or an empty one.

        Only serves the cache when it belongs to the SAME identity: after the
        user changes their key or gateway, the previous account's figures are
        not a degraded view of the new one, they are the wrong numbers.

        The ``models`` dict is copied rather than aliased: this adapter is a
        process singleton, so handing consumers a reference to its own cache
        would let one careless in-place edit corrupt every later read. The
        dataclass being frozen protects the field bindings, not the dict.
        """
        cached = self._cache
        if cached is None:
            return QGenieQuotaSnapshot(error=error)
        if identity is not None and self._cache_key != identity:
            return QGenieQuotaSnapshot(error=error)
        return QGenieQuotaSnapshot(
            user=cached.user,
            fetched_at=cached.fetched_at,
            stale=True,
            error=error,
            models=dict(cached.models) if cached.models is not None else None,
            cost=cached.cost,
        )

    def _open_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        # Live read so a runtime SSL toggle hot-applies to the next quota read.
        verify = (
            self._ssl_verify_provider()
            if self._ssl_verify_provider is not None
            else self._ssl_verify
        )
        return httpx.AsyncClient(timeout=self._timeout_s, verify=verify)


def _quota_host(base_url: str) -> str:
    """Reduce a provider ``base_url`` to the scheme+host the quota API lives on.

    Provider base_urls carry an API path (``https://host/v1``) while the quota
    paths are absolute from the root, so the path must be dropped rather than
    appended to — otherwise ``/v1`` + ``/v1/rate-limit/usage`` yields a 404.
    """
    try:
        parts = urlsplit(base_url.strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


async def _get_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> tuple[dict[str, Any] | None, str | None]:
    """GET and parse, returning ``(payload, error_code)``.

    The reason is carried out rather than collapsed into a bare ``None`` so the
    UI can tell "you are being throttled, wait about a minute" apart from "the
    gateway is unreachable". Those call for different user actions, and a
    featureless "no data" tells them neither.

    The throttle reply is HTTP 429 with a ``text/plain`` body, so the status is
    checked before any parse is attempted.
    """
    try:
        resp = await client.get(url, headers=headers)
    except Exception:  # noqa: BLE001 — transport failures degrade to cache
        return (None, QGENIE_ERR_UNREACHABLE)
    if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
        return (None, QGENIE_ERR_RATE_LIMITED)
    if resp.status_code != httpx.codes.OK:
        return (None, f"http_{resp.status_code}")
    try:
        parsed = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return (None, QGENIE_ERR_UNREACHABLE)
    if not isinstance(parsed, dict):
        return (None, QGENIE_ERR_UNREACHABLE)
    return (parsed, None)


def _counter(raw: Any) -> QGenieCounter | None:
    """Parse one window, or ``None`` when the upstream did not report it.

    The upstream omits whole windows for some models (the field is ``null``),
    and ``None`` is what makes :attr:`QGenieBucket.exhausted` read that as "not
    exhausted" rather than "spent". A window whose object is present but whose
    ``limit`` / ``remaining`` are missing is treated the same way: coercing
    those to ``0`` would manufacture a counter that looks exhausted, and an
    exhausted-looking daily counter is exactly what makes the UI tell the user
    both buckets are dry and to switch models. Only an explicit numeric ``0``
    means "no allowance".
    """
    if not isinstance(raw, dict):
        return None
    limit = raw.get("limit")
    remaining = raw.get("remaining")
    if not isinstance(limit, int) or not isinstance(remaining, int):
        return None
    try:
        return QGenieCounter(
            limit=limit,
            used=int(raw.get("used") or 0),
            remaining=remaining,
            reset_in_seconds=int(raw.get("reset_in_seconds") or 0),
        )
    except (TypeError, ValueError):
        return None


def _bucket(entry: dict[str, Any]) -> QGenieBucket:
    return QGenieBucket(
        **{field: _counter(entry.get(key)) for key, field in _WINDOW_FIELDS}
    )


def _parse_models(usage_raw: dict[str, Any]) -> dict[str, QGenieModelQuota]:
    """Group the flat ``model_usage`` list into one entry per model.

    The upstream lists each model twice — once per traffic class — so the two
    rows are folded together here. A model missing one class gets an empty
    bucket for it, which reads as "not exhausted" and lets the upstream be the
    judge.
    """
    by_model: dict[str, dict[TrafficClass, QGenieBucket]] = {}
    for entry in usage_raw.get("model_usage") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        raw_class = entry.get("limit_type")
        if not isinstance(name, str) or not name:
            continue
        try:
            traffic_class = TrafficClass(raw_class)
        except ValueError:
            continue
        by_model.setdefault(name, {})[traffic_class] = _bucket(entry)

    return {
        name: QGenieModelQuota(
            model_id=name,
            api=classes.get(TrafficClass.API, QGenieBucket()),
            ide=classes.get(TrafficClass.IDE, QGenieBucket()),
        )
        for name, classes in by_model.items()
    }


def _parse_cost(
    cost_raw: dict[str, Any] | None, cap_raw: dict[str, Any] | None
) -> QGenieCost | None:
    """Build the account spend view from the cost + cost-cap responses.

    Returns ``None`` when NEITHER response arrived, so "unknown spend" stays
    distinguishable from "spent nothing" — the caller carries the previous
    figures forward rather than publishing zeroes as fresh truth. A partial
    result (cost but no cap, or vice versa) still yields an object: the halves
    are independent and the fields that did arrive are worth showing.
    """
    if cost_raw is None and cap_raw is None:
        return None

    day_usd = 0.0
    month_usd = 0.0
    by_class: dict[str, float] = {}

    for period in (cost_raw or {}).get("periods") or []:
        if not isinstance(period, dict):
            continue
        window = period.get("window")
        total = period.get("total_estimated_cost_usd")
        total_f = float(total) if isinstance(total, int | float) else 0.0
        if window == "day":
            day_usd = total_f
            for item in period.get("breakdown") or []:
                if not isinstance(item, dict):
                    continue
                klass = item.get("traffic_class")
                amount = item.get("estimated_cost_usd")
                if isinstance(klass, str) and isinstance(amount, int | float):
                    by_class[klass] = float(amount)
        elif window == "month":
            month_usd = total_f

    tier: str | None = None
    day_cap: float | None = None
    month_cap: float | None = None
    if cap_raw:
        raw_tier = cap_raw.get("tier")
        tier = raw_tier if isinstance(raw_tier, str) else None
        day_cap = _usd(cap_raw.get("cost_per_day"))
        month_cap = _usd(cap_raw.get("cost_per_month"))

    return QGenieCost(
        currency=str((cost_raw or {}).get("currency") or "USD"),
        day_usd=day_usd,
        day_by_class=by_class or None,
        month_usd=month_usd,
        tier=tier,
        day_cap_usd=day_cap,
        month_cap_usd=month_cap,
    )


def _usd(raw: Any) -> float | None:
    """Pull the ``usd`` figure out of a cost-cap amount object."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("usd")
    return float(value) if isinstance(value, int | float) else None
