# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Social / lightweight-path response policy — central constants + resolver.

DISC-2 P4-step1 (§22A.7): the lightweight path (Path A — a single brief
responder for a greeting / thanks / acknowledgement) gains a user-configurable
**response policy** that actually changes what the responder does:

* ``silent`` — do NOT emit a turn at all (a legal empty round: zero
  ``speaker_changed`` / ``chunk`` frames, the orchestrator still emits the
  unconditional ``END(completed)`` frame so the frontend never hangs);
* ``single_brief_reply`` — the DEFAULT == the phase-1 behaviour, byte-for-byte
  (one brief reply using ``policy.framing_mode``);
* ``single_closing_reply`` — force the wrap-up framing so the reply has a
  closing tone (even when the planner picked ``social_mode``);
* ``continue_last_topic`` — force the follow-up framing so the reply carries
  the previous topic forward instead of a fresh greeting.

DISC-1 TODO-3 (用户 2026-06-24 拍板) adds two *meta* policies that resolve to one
of the concrete policies above at turn time:

* ``random`` — pick one of the three NON-silent concrete policies uniformly at
  random per turn (never ``silent`` — a random silence would look like a bug);
* ``ai_decide`` — ask a lightweight LLM which of the three concrete policies
  fits this turn (opt-in; one extra low-temperature call). Times out / fails /
  illegal reply ⇒ degrade to :data:`DEFAULT_SOCIAL_RESPONSE_POLICY` (never
  blocks). When OFF (any other policy) NO LLM is called — zero cost.

Stored at ``meta["discussion"]["social_response_policy"]`` (§3.1 tail-append).

**Read-side semantics (critical — keeps every existing social / discussion test
byte-for-byte):** a *missing* / illegal ``social_response_policy`` resolves to
:data:`DEFAULT_SOCIAL_RESPONSE_POLICY` (= ``single_brief_reply``), i.e. the
phase-1 behaviour.  There is therefore intentionally NO
``NEW_CONVERSATION_*_DEFAULTS`` dict: the new-conversation default == the
read-side default == ``single_brief_reply``.

Layering: ``application/use_cases`` — pure constants + a pure function over
stdlib only.  No ports / domain / adapters, so the layering contracts hold.
"""

from __future__ import annotations

import random as _random

__all__ = [
    "SOCIAL_RESPONSE_POLICY_KEY",
    "DEFAULT_SOCIAL_RESPONSE_POLICY",
    "SOCIAL_RESPONSE_POLICIES",
    "CONCRETE_SOCIAL_RESPONSE_POLICIES",
    "RANDOM_SELECTABLE_POLICIES",
    "resolve_social_response_policy",
    "select_random_social_policy",
    "coerce_concrete_social_policy",
]


# ── Key name (wire snake_case == meta["discussion"] key) ─────────────────────
SOCIAL_RESPONSE_POLICY_KEY = "social_response_policy"


# ── Default / legal values ───────────────────────────────────────────────────
#: The read-side default AND the new-conversation default.  Resolving a missing
#: or illegal value to this keeps the phase-1 lightweight-path behaviour intact.
DEFAULT_SOCIAL_RESPONSE_POLICY = "single_brief_reply"

#: The three CONCRETE policies a reply turn can actually run (``silent`` is also
#: concrete but excluded from the random/ai selectable set — see below).
CONCRETE_SOCIAL_RESPONSE_POLICIES: tuple[str, ...] = (
    "single_brief_reply",
    "single_closing_reply",
    "continue_last_topic",
)

#: The set ``random`` / ``ai_decide`` pick from — the three NON-silent concrete
#: policies (a random / AI-chosen SILENCE would look like a bug, so it is never
#: selectable; the user must pick ``silent`` explicitly to get silence).
RANDOM_SELECTABLE_POLICIES: tuple[str, ...] = CONCRETE_SOCIAL_RESPONSE_POLICIES

#: The closed set of legal policies (4 concrete + 2 meta).  Anything outside
#: this tuple coerces back to :data:`DEFAULT_SOCIAL_RESPONSE_POLICY`.
SOCIAL_RESPONSE_POLICIES: tuple[str, ...] = (
    "silent",
    "single_brief_reply",
    "single_closing_reply",
    "continue_last_topic",
    # DISC-1 TODO-3 meta policies (resolve to a concrete one at turn time).
    "random",
    "ai_decide",
)


def resolve_social_response_policy(discussion: dict | None) -> str:
    """Resolve the social response policy from a ``meta["discussion"]`` dict.

    A ``None`` / empty dict, a missing ``social_response_policy`` key, or any
    illegal / out-of-set value resolves to :data:`DEFAULT_SOCIAL_RESPONSE_POLICY`
    (= ``single_brief_reply``), keeping legacy/existing conversations + every
    social-path test byte-for-byte unchanged.  May return a META policy
    (``random`` / ``ai_decide``) — the orchestrator resolves those to a concrete
    policy at turn time (see :func:`select_random_social_policy` /
    the ai_decide LLM call).
    """
    d = discussion or {}
    value = d.get(SOCIAL_RESPONSE_POLICY_KEY)
    if isinstance(value, str) and value in SOCIAL_RESPONSE_POLICIES:
        return value
    return DEFAULT_SOCIAL_RESPONSE_POLICY


def select_random_social_policy(
    rng: _random.Random | None = None,
) -> str:
    """Pick one of the three NON-silent concrete policies uniformly at random.

    Pure-ish: accepts an optional injected ``Random`` for deterministic tests;
    defaults to the module RNG otherwise.  Never returns ``silent``.
    """
    r = rng or _random
    return r.choice(RANDOM_SELECTABLE_POLICIES)


def coerce_concrete_social_policy(value: object) -> str:
    """Coerce an arbitrary value to a legal CONCRETE (non-meta) policy.

    Used to validate an ``ai_decide`` LLM reply: a value in the concrete set is
    accepted verbatim; anything else (a meta policy, junk, non-str) degrades to
    :data:`DEFAULT_SOCIAL_RESPONSE_POLICY` so the AI path never blocks.
    """
    if isinstance(value, str) and value in CONCRETE_SOCIAL_RESPONSE_POLICIES:
        return value
    return DEFAULT_SOCIAL_RESPONSE_POLICY
