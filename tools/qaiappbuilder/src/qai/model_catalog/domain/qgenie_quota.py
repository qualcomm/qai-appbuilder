# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""QGenie dual-bucket quota value objects.

QGenie meters every request into one of two *independent* quota buckets and
the upstream picks the bucket purely from the outbound ``User-Agent``: a UA
containing ``claude-cli`` / ``claude-code`` / ``claudecode`` (case-insensitive
substring) lands in the ``UI`` bucket, anything else in ``Api``. Host,
endpoint, auth scheme and the ``anthropic-*`` headers make no difference —
verified over 19 A/B request pairs against ``/v1/usage/cost`` counters.

The two buckets carry genuinely separate allowances (measured: ``gpt-5.5``
grants the ``UI`` side 5x the daily tokens of ``Api``, while
``claude-4-5-sonnet`` grants it only half the RPM), so one can be exhausted
while the other is untouched. That asymmetry is the whole reason this feature
exists: when the bucket in use runs dry the user can keep working on the other
one.

What this module deliberately does NOT model:

* **Per-model cost.** The upstream spec states the observe-only writer
  "intentionally does not store model identifiers", so spend is only ever
  knowable per traffic class for the whole account. :class:`QGenieCost`
  therefore has no model dimension — and the UI must say so, or a reader will
  assume the figure belongs to the selected model.
* **Anything beyond the daily window for exhaustion.** RPM/TPM are rolling
  windows that self-heal in under a minute, so treating them as "exhausted"
  would flap the bucket back and forth. Only :attr:`QGenieBucket.day` decides
  exhaustion; the weekly figure is display-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "QGENIE_ERR_BAD_BASE_URL",
    "QGENIE_ERR_NOT_CONFIGURED",
    "QGENIE_ERR_NO_API_KEY",
    "QGENIE_ERR_RATE_LIMITED",
    "QGENIE_ERR_UNREACHABLE",
    "QGenieBucket",
    "QGenieCost",
    "QGenieCounter",
    "QGenieModelQuota",
    "QGenieQuotaSnapshot",
    "TrafficClass",
]


class TrafficClass(Enum):
    """Which metering bucket a request is billed to.

    The values are the wire strings the upstream ``RateLimitType`` schema
    uses, so they can be used as dict keys against a raw API response with no
    translation step. The display names the QGenie console shows
    (``API/SDK`` / ``IDE/CLI``) and the ones its request log shows
    (``API`` / ``ClaudeCode``) are three spellings of these same two values;
    presentation owns that mapping, not the domain.
    """

    API = "Api"
    IDE = "UI"

    @property
    def other(self) -> TrafficClass:
        """The bucket to fall back to when this one is exhausted."""
        return TrafficClass.IDE if self is TrafficClass.API else TrafficClass.API


# NOTE: the outbound ``User-Agent`` that selects the bucket is built in
# ``qai.chat.infrastructure.llm_stream`` (``_PRODUCT_UA`` / ``_IDE_UA`` /
# ``_resolve_user_agent``), NOT here. It lived in this module briefly and was
# removed: the transport is the only caller, a second definition of the
# externally-imposed ``claude-cli`` marker invites the two spellings to drift
# (and the failure mode is silent — usage keeps landing in one bucket while the
# UI claims otherwise), and importing this bounded context from ``qai.chat``
# has no precedent in the tree.


@dataclass(frozen=True, slots=True, kw_only=True)
class QGenieCounter:
    """One rate-limit window's usage.

    ``remaining`` is taken from the upstream response rather than derived from
    ``limit - used``: the two can legitimately disagree while a window rolls,
    and the server's own figure is the one its throttle actually enforces.
    """

    limit: int
    used: int
    remaining: int
    reset_in_seconds: int

    @property
    def exhausted(self) -> bool:
        """Whether this window has nothing left.

        A non-positive ``limit`` counts as exhausted: "no allowance" and "no
        allowance left" are the same thing to a caller deciding whether it can
        send, and it avoids a division-by-zero in :attr:`used_ratio`.
        """
        return self.limit <= 0 or self.remaining <= 0

    @property
    def used_ratio(self) -> float:
        """Share consumed in ``[0.0, 1.0]``."""
        if self.limit <= 0:
            return 1.0
        return min(1.0, max(0.0, self.used / self.limit))


@dataclass(frozen=True, slots=True, kw_only=True)
class QGenieBucket:
    """The four rate-limit windows for one model in one traffic class.

    Every window is optional because the upstream omits some per model (e.g.
    ``claude-4-5-sonnet`` reports no weekly counter on the ``Api`` side), and
    a missing counter must not be read as a zeroed one.
    """

    day: QGenieCounter | None = None
    week: QGenieCounter | None = None
    rpm: QGenieCounter | None = None
    tpm: QGenieCounter | None = None

    @property
    def exhausted(self) -> bool:
        """Whether the *daily* allowance is spent.

        Daily only, by design (see module docstring): RPM/TPM recover on their
        own within a minute, so letting them declare exhaustion would bounce
        the active bucket back and forth for no user benefit. An absent daily
        counter reads as "not exhausted" — refusing to send on missing data
        would be worse than trying and letting the upstream decide.
        """
        return self.day is not None and self.day.exhausted

    def at_or_over(self, warn_ratio: float) -> bool:
        """Whether daily use reached ``warn_ratio`` (0.0-1.0)."""
        return self.day is not None and self.day.used_ratio >= warn_ratio


@dataclass(frozen=True, slots=True, kw_only=True)
class QGenieModelQuota:
    """Both buckets for a single model."""

    model_id: str
    api: QGenieBucket
    ide: QGenieBucket

    def bucket(self, traffic_class: TrafficClass) -> QGenieBucket:
        return self.api if traffic_class is TrafficClass.API else self.ide

    def usable(self, traffic_class: TrafficClass) -> bool:
        return not self.bucket(traffic_class).exhausted

    def both_exhausted(self) -> bool:
        """Whether neither bucket can serve a request.

        This is the guard that stops the switch logic from looping: with both
        sides dry the only honest move is to tell the user to pick another
        model, never to retry a third time.
        """
        return self.api.exhausted and self.ide.exhausted


@dataclass(frozen=True, slots=True, kw_only=True)
class QGenieCost:
    """Account-wide spend, split only by traffic class.

    Has no model dimension on purpose — the upstream cannot provide one (see
    module docstring). Presentation MUST label this as the account total
    across all QGenie models so it is not mistaken for the selected model's
    spend.
    """

    currency: str = "USD"
    day_usd: float = 0.0
    day_by_class: dict[str, float] | None = None
    month_usd: float = 0.0
    tier: str | None = None
    day_cap_usd: float | None = None
    month_cap_usd: float | None = None


#: Why a quota read produced nothing (or only stale data).
#:
#: Named constants rather than free-form strings so the UI can branch on them
#: and the set stays greppable. Each maps to a different user action:
#:   ``no_api_key``    — paste a key in Settings -> Cloud Models
#:   ``rate_limited``  — wait; the endpoint throttles at roughly 60 s
#:   ``unreachable``   — check the network / VPN
#:   ``not_configured``— this build has no QGenie provider (external edition)
#:   ``bad_base_url``  — the provider's base_url is not a usable URL
QGENIE_ERR_NO_API_KEY: Final[str] = "no_api_key"
QGENIE_ERR_RATE_LIMITED: Final[str] = "rate_limited"
QGENIE_ERR_UNREACHABLE: Final[str] = "unreachable"
QGENIE_ERR_NOT_CONFIGURED: Final[str] = "not_configured"
QGENIE_ERR_BAD_BASE_URL: Final[str] = "bad_base_url"


@dataclass(frozen=True, slots=True, kw_only=True)
class QGenieQuotaSnapshot:
    """One point-in-time read of the account's QGenie quota.

    ``stale`` marks a snapshot served from cache after a refresh failed. The
    UI keeps rendering it (with its ``fetched_at``) rather than blanking the
    gauge: when the network is down the LLM is unusable anyway, so the last
    known figures are strictly more useful than an empty widget.

    ``error`` says WHY there is nothing (or only stale data) to show. Without
    it every failure looks identical to the user — "no gauge" reads the same
    whether they never pasted a key, the endpoint is throttling them for
    another 40 seconds, or the gateway is unreachable — and those call for
    completely different actions. ``None`` on a healthy read.
    """

    user: str = ""
    fetched_at: str = ""
    stale: bool = False
    error: str | None = None
    models: dict[str, QGenieModelQuota] | None = None
    cost: QGenieCost | None = None

    def for_model(self, model_id: str) -> QGenieModelQuota | None:
        """Look up a model, tolerating provider-prefixed ids.

        Chat carries ids in several shapes depending on the layer
        (``anthropic::claude-4-5-haiku``, a ``qgenie::``-prefixed form, or the
        bare name), while the upstream keys this map by the two-segment
        vendor form. Matching on the longest common suffix keeps every caller
        working without each one having to normalise first.
        """
        table = self.models or {}
        if model_id in table:
            return table[model_id]
        bare = model_id.rsplit("::", 1)[-1]
        for key, value in table.items():
            if key.rsplit("::", 1)[-1] == bare:
                return value
        return None
