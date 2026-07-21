# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Default streaming retry policy (PR-401b / S7.5 lane L4).

Migrates the inline retry / backoff logic at
``backend/chat_handler.py:476-540`` into a dedicated adapter behind
:class:`qai.chat.application.ports.RetryPolicyPort`.

Two failure categories are recognised, each with its own schedule:

* :data:`RetryCategory.PROMPT_TOO_LONG` — the upstream LLM rejected the
  request because the prompt exceeded its context window.  The legacy
  behaviour is to retry **once**, with no delay, after compressing the
  conversation to ``target_ratio = 0.50``.  The compression itself is
  performed by the chat use case (it knows the context-size estimator
  and the compressed-message API); this policy only signals **whether**
  to retry and **what** target ratio to use.
* :data:`RetryCategory.THROTTLING` — the upstream returned a 429 / 503
  / "rate limit" / "overloaded" / etc.  The legacy schedule is up to
  3 attempts with exponential backoff base 1.5 s ± 20 % jitter:

      attempt 1 → 1.5 s × (0.8 - 1.2)   ≈ 1.2 - 1.8 s
      attempt 2 → 3.0 s × (0.8 - 1.2)   ≈ 2.4 - 3.6 s
      attempt 3 → 6.0 s × (0.8 - 1.2)   ≈ 4.8 - 7.2 s

  Total worst-case wait ≈ 12.6 s before giving up; matches legacy
  ``_MAX_THROTTLE_RETRIES`` / ``_THROTTLE_BACKOFF_BASE``.

The policy is **deterministic** by design: the random jitter is drawn
through an injected ``rng`` callable (default ``random.random``) so
PR-401c tests can supply a fixed sequence and verify exact delays.
"""

from __future__ import annotations

import random as _random
from collections.abc import Callable
from dataclasses import dataclass
import math

from qai.chat.application.ports import (
    RetryCategory,
    RetryDecision,
    RetryPolicyPort,
)


# Legacy constants (byte-for-byte port of backend/chat_handler.py:2353-2354).
MAX_THROTTLE_RETRIES: int = 3
THROTTLE_BACKOFF_BASE_SECONDS: float = 1.5
THROTTLE_JITTER_RATIO: float = 0.4
"""±20 % jitter is implemented as ``base * (1 - ratio/2 + ratio*rand)``;
``ratio = 0.4`` therefore covers ``[0.8, 1.2]``."""

# Prompt-too-long: single retry, force-compress to half the budget.
PROMPT_TOO_LONG_TARGET_RATIO: float = 0.50
PROMPT_TOO_LONG_MAX_RETRIES: int = 1

# Network (transient connection failure to the model service that MAY take a
# while to self-heal — connect error / read-write timeout / mid-stream socket
# drop). The Nth retry waits
# ``NETWORK_BACKOFF_SCHEDULE_SECONDS[min(N-1, last)]`` and every attempt past
# the table reuses the final (capped) value. Previously this retried
# INDEFINITELY (a never-recovering link would loop forever). It is now bounded
# by a WALL-CLOCK budget: once the caller's cumulative retry time exceeds
# ``NETWORK_WALL_CLOCK_BUDGET_SECONDS`` the policy returns should_retry=False
# (terminal). When the caller cannot measure elapsed time (``elapsed_s`` is
# None) an attempt-count proxy (``NETWORK_MAX_ATTEMPTS_PROXY``) caps it instead
# — chosen so the cumulative 30s-tail waits reach ~the same wall-clock budget.
NETWORK_BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (3.0, 5.0, 10.0, 30.0)
NETWORK_WALL_CLOCK_BUDGET_SECONDS: float = 600.0
"""10 minutes: after this much cumulative retry time a network fault that has
not recovered is treated as terminal (was: infinite retry)."""
NETWORK_MAX_ATTEMPTS_PROXY: int = 24
"""Attempt-count fallback when ``elapsed_s`` is unavailable. With the schedule
above the cumulative wait crosses ~600s around this many attempts
(3+5+10 + 21×30 ≈ 648s), so it approximates the wall-clock budget."""

# Bounded-fast (DNS failure / connection refused / host unreachable): these
# either clear almost immediately or are effectively permanent, so retry a few
# times FAST then give up (terminal). No point waiting minutes for a refused
# port or an unresolvable host.
BOUNDED_FAST_SCHEDULE_SECONDS: tuple[float, ...] = (1.0, 3.0, 10.0)
BOUNDED_FAST_MAX_RETRIES: int = 3

# Bounded-server (HTTP 5xx): a few jittered attempts then terminal.
BOUNDED_SERVER_SCHEDULE_SECONDS: tuple[float, ...] = (2.0, 5.0, 15.0)
BOUNDED_SERVER_MAX_RETRIES: int = 3
BOUNDED_SERVER_JITTER_RATIO: float = 0.4
"""±20 % jitter, same convention as throttling (``[0.8, 1.2]``)."""


@dataclass(slots=True)
class DefaultStreamRetryPolicy(RetryPolicyPort):
    """Default :class:`RetryPolicyPort` implementation.

    All knobs are public attributes so an adapter wired in tests can
    pin a deterministic ``rng`` or override the schedule without
    subclassing.
    """

    max_throttle_retries: int = MAX_THROTTLE_RETRIES
    throttle_backoff_base_seconds: float = THROTTLE_BACKOFF_BASE_SECONDS
    throttle_jitter_ratio: float = THROTTLE_JITTER_RATIO
    prompt_too_long_target_ratio: float = PROMPT_TOO_LONG_TARGET_RATIO
    prompt_too_long_max_retries: int = PROMPT_TOO_LONG_MAX_RETRIES
    network_backoff_schedule_seconds: tuple[float, ...] = (
        NETWORK_BACKOFF_SCHEDULE_SECONDS
    )
    network_wall_clock_budget_seconds: float = NETWORK_WALL_CLOCK_BUDGET_SECONDS
    network_max_attempts_proxy: int = NETWORK_MAX_ATTEMPTS_PROXY
    bounded_fast_schedule_seconds: tuple[float, ...] = (
        BOUNDED_FAST_SCHEDULE_SECONDS
    )
    bounded_fast_max_retries: int = BOUNDED_FAST_MAX_RETRIES
    bounded_server_schedule_seconds: tuple[float, ...] = (
        BOUNDED_SERVER_SCHEDULE_SECONDS
    )
    bounded_server_max_retries: int = BOUNDED_SERVER_MAX_RETRIES
    bounded_server_jitter_ratio: float = BOUNDED_SERVER_JITTER_RATIO
    rng: Callable[[], float] = _random.random
    """Returns a value in ``[0, 1)``.  Replaced with a deterministic
    sequence in unit tests."""

    def next_attempt(
        self,
        *,
        category: RetryCategory,
        attempt_number: int,
        server_advised_delay_s: float | None = None,
        elapsed_s: float | None = None,
    ) -> RetryDecision:
        if attempt_number < 1:
            raise ValueError(
                f"attempt_number must be >= 1 (got {attempt_number})",
            )

        if category is RetryCategory.PROMPT_TOO_LONG:
            return self._prompt_too_long(attempt_number=attempt_number)
        if category is RetryCategory.THROTTLING:
            return self._throttling(
                attempt_number=attempt_number,
                server_advised_delay_s=server_advised_delay_s,
            )
        if category is RetryCategory.NETWORK:
            return self._network(
                attempt_number=attempt_number,
                elapsed_s=elapsed_s,
            )
        if category is RetryCategory.BOUNDED_FAST:
            return self._bounded_fast(attempt_number=attempt_number)
        if category is RetryCategory.BOUNDED_SERVER:
            return self._bounded_server(attempt_number=attempt_number)
        # Defensive fallthrough — Enum constraint already excludes others.
        raise ValueError(f"unsupported retry category: {category!r}")

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------
    def _prompt_too_long(self, *, attempt_number: int) -> RetryDecision:
        if attempt_number > self.prompt_too_long_max_retries:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                compress_target_ratio=None,
                attempt_number=attempt_number,
            )
        return RetryDecision(
            should_retry=True,
            delay_seconds=0.0,
            compress_target_ratio=self.prompt_too_long_target_ratio,
            attempt_number=attempt_number,
        )

    def _throttling(
        self,
        *,
        attempt_number: int,
        server_advised_delay_s: float | None = None,
    ) -> RetryDecision:
        if attempt_number > self.max_throttle_retries:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                attempt_number=attempt_number,
            )
        # Rate-limit aware: when the upstream advised a concrete delay via a
        # ``Retry-After`` header (already parsed + clamped by the caller),
        # honour it verbatim — no jitter, and crucially the ``rng`` is NOT
        # consumed so a deterministic test sequence stays aligned with the
        # exponential path. Reject non-finite / negative values and fall
        # through to the exponential schedule (defensive; the parser never
        # emits those).
        if (
            server_advised_delay_s is not None
            and math.isfinite(server_advised_delay_s)
            and server_advised_delay_s >= 0.0
        ):
            return RetryDecision(
                should_retry=True,
                delay_seconds=float(server_advised_delay_s),
                attempt_number=attempt_number,
            )
        # Exponential backoff with symmetric jitter.
        base = self.throttle_backoff_base_seconds * (2 ** (attempt_number - 1))
        # rng() in [0, 1) → factor in [1 - ratio/2, 1 + ratio/2)
        factor = 1.0 - self.throttle_jitter_ratio / 2.0 + self.throttle_jitter_ratio * self.rng()
        delay = base * factor
        return RetryDecision(
            should_retry=True,
            delay_seconds=delay,
            attempt_number=attempt_number,
        )

    def _network(
        self,
        *,
        attempt_number: int,
        elapsed_s: float | None = None,
    ) -> RetryDecision:
        # Transient network failure that MAY take a while to self-heal. Still
        # "logically recoverable" (a flaky link can come back), but no longer
        # retried forever: a WALL-CLOCK budget makes a never-recovering
        # network terminate. The delay escalates through the schedule then
        # holds at its capped tail: attempt 1 -> 3s, 2 -> 5s, 3 -> 10s,
        # 4+ -> 30s.
        #
        # Budget enforcement, in order of preference:
        #   1. Wall-clock (preferred): if the caller passed cumulative
        #      ``elapsed_s`` and it exceeds the budget, terminate.
        #   2. Attempt-count proxy (fallback): if the caller cannot measure
        #      elapsed time (``elapsed_s is None``), cap the attempt count at
        #      a value whose cumulative 30s-tail waits reach ~the same budget.
        if (
            elapsed_s is not None
            and elapsed_s >= self.network_wall_clock_budget_seconds
        ):
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                attempt_number=attempt_number,
            )
        if (
            elapsed_s is None
            and attempt_number > self.network_max_attempts_proxy
        ):
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                attempt_number=attempt_number,
            )
        schedule = self.network_backoff_schedule_seconds or (30.0,)
        idx = min(attempt_number - 1, len(schedule) - 1)
        return RetryDecision(
            should_retry=True,
            delay_seconds=schedule[idx],
            attempt_number=attempt_number,
        )

    def _bounded_fast(self, *, attempt_number: int) -> RetryDecision:
        # DNS failure / connection refused / host unreachable: a few FAST
        # attempts (1s -> 3s -> 10s) then terminal. These faults clear almost
        # immediately (transient DNS blip / service just starting) or are
        # effectively permanent (wrong host, service down) — waiting minutes
        # helps neither, so cap at ``bounded_fast_max_retries``.
        if attempt_number > self.bounded_fast_max_retries:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                attempt_number=attempt_number,
            )
        schedule = self.bounded_fast_schedule_seconds or (1.0,)
        idx = min(attempt_number - 1, len(schedule) - 1)
        return RetryDecision(
            should_retry=True,
            delay_seconds=schedule[idx],
            attempt_number=attempt_number,
        )

    def _bounded_server(self, *, attempt_number: int) -> RetryDecision:
        # HTTP 5xx: a few jittered attempts (2s -> 5s -> 15s ±20%) then
        # terminal. Jitter (same convention as throttling) spreads retries so
        # a briefly-overloaded upstream is not hammered in lock-step.
        if attempt_number > self.bounded_server_max_retries:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                attempt_number=attempt_number,
            )
        schedule = self.bounded_server_schedule_seconds or (2.0,)
        idx = min(attempt_number - 1, len(schedule) - 1)
        base = schedule[idx]
        # rng() in [0, 1) → factor in [1 - ratio/2, 1 + ratio/2)
        factor = (
            1.0
            - self.bounded_server_jitter_ratio / 2.0
            + self.bounded_server_jitter_ratio * self.rng()
        )
        return RetryDecision(
            should_retry=True,
            delay_seconds=base * factor,
            attempt_number=attempt_number,
        )


__all__ = [
    "DefaultStreamRetryPolicy",
    "MAX_THROTTLE_RETRIES",
    "THROTTLE_BACKOFF_BASE_SECONDS",
    "THROTTLE_JITTER_RATIO",
    "PROMPT_TOO_LONG_TARGET_RATIO",
    "PROMPT_TOO_LONG_MAX_RETRIES",
    "NETWORK_BACKOFF_SCHEDULE_SECONDS",
    "NETWORK_WALL_CLOCK_BUDGET_SECONDS",
    "NETWORK_MAX_ATTEMPTS_PROXY",
    "BOUNDED_FAST_SCHEDULE_SECONDS",
    "BOUNDED_FAST_MAX_RETRIES",
    "BOUNDED_SERVER_SCHEDULE_SECONDS",
    "BOUNDED_SERVER_MAX_RETRIES",
    "BOUNDED_SERVER_JITTER_RATIO",
]
