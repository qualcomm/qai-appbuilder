# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Grey-zone LLM intent-classifier feature flags — central constants + resolver.

DISC-2 P2-step1 (§22A.5): on top of the pure-heuristic ``classify_intent``
(§21.3), a low-temperature LLM classifier may refine the verdict **only in the
grey zone** (heuristic-first + timeout fallback + conservative gating).  This
module is the SINGLE source of truth for:

* the wire/meta **key names** of the three classifier knobs;
* the **defaults** (timeout 2000ms — §22A.5; LLM confidence floor 0.65);
* :func:`resolve_intent_classifier_config` — the orchestrator's unified read
  entry that coerces a ``meta["discussion"]`` dict into a frozen
  :class:`IntentClassifierConfig`.

**Read-side semantics (critical — keeps the existing intent / discussion tests
byte-for-byte):** a *missing* ``intent_classifier_enabled`` key resolves to
**OFF**.  When the flag is OFF the orchestrator uses the pure-heuristic verdict
verbatim and NEVER calls the LLM — zero latency, zero token cost, behaviour
identical to the heuristic-only implementation.  This read-side OFF default is
what keeps **legacy conversations** (whose ``meta["discussion"]`` has no such
key) unchanged.

**New-conversation default (用户 2026-06-24 拍板 — 新建默认开):** a brand-new
conversation seeds ``intent_classifier_enabled = true`` (alongside
``implementation_enabled``) via the front-end's enable-discussion full-config
PATCH (``DiscussionConfig`` / ``DEFAULT_DISCUSSION_CONFIG`` → ``DiscussionConfigBody``
→ this meta key).  The READ default here stays OFF so that absent-key (legacy)
conversations are untouched — the "default ON for NEW conversations" is realised
purely by seeding the key at create/enable time, not by changing this resolver.
(There is therefore intentionally NO ``NEW_CONVERSATION_*_DEFAULTS`` dict here:
the seeding lives in the front-end default config, this stays absent⇒OFF.)

Layering: ``application/use_cases`` — pure constants + a pure function over
stdlib only.  No ports / domain / adapters, so the layering contracts hold.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "INTENT_CLASSIFIER_ENABLED_KEY",
    "INTENT_CLASSIFIER_MODEL_KEY",
    "INTENT_CLASSIFIER_TIMEOUT_MS_KEY",
    "DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS",
    "MIN_INTENT_CLASSIFIER_TIMEOUT_MS",
    "LLM_CONFIDENCE_FLOOR",
    "IntentClassifierConfig",
    "resolve_intent_classifier_config",
]


# ── Flag key names (wire snake_case == meta["discussion"] key) ───────────────
INTENT_CLASSIFIER_ENABLED_KEY = "intent_classifier_enabled"
INTENT_CLASSIFIER_MODEL_KEY = "intent_classifier_model"
INTENT_CLASSIFIER_TIMEOUT_MS_KEY = "intent_classifier_timeout_ms"


# ── Defaults / bounds ────────────────────────────────────────────────────────
#: Default classifier timeout (§22A.5 — 1.5~2.5s window; 2000ms midpoint).  On
#: timeout the orchestrator falls straight back to the heuristic verdict so a
#: slow / hung classifier never drags the conversation.
DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS = 2000

#: Lower bound on the configured timeout.  A value below this (or non-positive /
#: illegal) is coerced UP to the default — a sub-200ms budget is almost never a
#: real preference and would make the classifier useless (every call times out).
MIN_INTENT_CLASSIFIER_TIMEOUT_MS = 200

#: Gating floor (§22A.5): an LLM verdict with ``confidence`` below this is NOT
#: trusted — the orchestrator keeps the heuristic verdict.  Combined with the
#: "never escalate the grey zone to full without a strong heuristic signal" rule
#: (§21.11), the LLM can only refine WITHIN the conservative envelope.
LLM_CONFIDENCE_FLOOR = 0.65


@dataclass(frozen=True, slots=True)
class IntentClassifierConfig:
    """The resolved grey-zone classifier config for ONE conversation.

    Produced by :func:`resolve_intent_classifier_config`.  ``enabled`` defaults
    to ``False`` (= OFF for a missing key, AND the new-conversation default —
    see module docstring), ``model`` defaults to ``None`` (let the orchestrator
    fall through its model-resolution ladder), and ``timeout_ms`` defaults to
    :data:`DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS`.
    """

    enabled: bool = False
    model: str | None = None
    timeout_ms: int = DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS


def _coerce_bool(value: object) -> bool:
    """Coerce a meta value to bool (missing/None/falsey/illegal → ``False``)."""
    return bool(value)


def _coerce_model(value: object) -> str | None:
    """Coerce a meta value to a model id string (missing/blank → ``None``)."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _coerce_timeout_ms(value: object) -> int:
    """Coerce a meta value to a legal timeout (illegal/missing/too-small →
    :data:`DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS`).
    """
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass — reject it explicitly so a stray
        # ``True``/``False`` does not become a 1ms / 0ms timeout.
        return DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, float):
        candidate = int(value)
    else:
        return DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS
    if candidate < MIN_INTENT_CLASSIFIER_TIMEOUT_MS:
        return DEFAULT_INTENT_CLASSIFIER_TIMEOUT_MS
    return candidate


def resolve_intent_classifier_config(
    discussion: dict | None,
) -> IntentClassifierConfig:
    """Resolve the classifier config from a ``meta["discussion"]`` dict.

    The orchestrator's unified read entry.  A ``None`` / empty dict, or a
    missing ``intent_classifier_enabled`` key, resolves to OFF (``model`` →
    ``None``, ``timeout_ms`` → the default), keeping legacy/existing
    conversations + every heuristic-only intent test byte-for-byte unchanged.
    An illegal / out-of-range ``intent_classifier_timeout_ms`` coerces back to
    the default.
    """
    d = discussion or {}
    return IntentClassifierConfig(
        enabled=_coerce_bool(d.get(INTENT_CLASSIFIER_ENABLED_KEY)),
        model=_coerce_model(d.get(INTENT_CLASSIFIER_MODEL_KEY)),
        timeout_ms=_coerce_timeout_ms(d.get(INTENT_CLASSIFIER_TIMEOUT_MS_KEY)),
    )
