# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation-mode enablement flag — pure resolver (DISC-1 §22.3/§22.7).

DISC-1 step3 wires the ``implement`` intent through to an actual
implementation-mode speaker turn (tools unlocked by the conservative
:func:`compute_effective_implementation_tools` intersection, ai_coding sandbox
at runtime, agent-round budget, abort stop).  That ENTIRE execution mode is
guarded by a single OFF-by-default flag read from
``meta["discussion"]["implementation_enabled"]``.

🔴 OFF by default — safety contract:

* The flag is **missing on every existing conversation** → resolves to ``False``
  → the orchestrator runs an ``implement`` turn exactly as a
  ``directed_deep_task`` discussion (step1 behaviour, byte-for-byte: NO tools
  unlocked, NO implementation framing).
* A brand-new conversation seeds the flag ON (用户 2026-06-24 拍板) via the
  front-end's enable-discussion full-config PATCH (TODO-1); the
  ``DiscussionConfigBody`` DTO now exposes this key.  A MISSING key (every legacy
  conversation) still resolves to ``False`` at READ time here, so the OFF default
  is what keeps existing conversations untouched — the "default ON for NEW
  conversations" is realised purely by seeding the key at create/enable time, not
  by changing this resolver.

Layering: ``application/use_cases`` — stdlib only; no ports / domain / adapters
(mirrors :mod:`convergence_defaults` / :mod:`implementation_tool_policy`).

DISC-1 二期-step2 extension: this module additionally hosts the feature-item
**extractor (planner)** knobs (model-resolution key + ladder, timeout, item-count
bounds — §22.4 / §22.12#4), keeping every implementation-mode default in ONE
place.  These are pure constants + a pure resolver; they do NOT call the LLM and
are NOT wired into the execute / ``_run_*`` paths — a later step's planner use
case / adapter reads them.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "IMPLEMENTATION_ENABLED_KEY",
    "resolve_implementation_enabled",
    # DISC-1 二期-step2: feature-item extractor (planner) knobs.
    "IMPLEMENTATION_PLANNER_MODEL_KEY",
    "IMPLEMENTATION_PLANNER_TIMEOUT_MS_KEY",
    "DEFAULT_PLANNER_TIMEOUT_MS",
    "MIN_PLANNER_TIMEOUT_MS",
    "MAX_PLANNER_TIMEOUT_MS",
    "MIN_FEATURE_ITEMS",
    "MAX_FEATURE_ITEMS",
    "resolve_planner_model",
    "resolve_planner_timeout_ms",
    # DISC-1 三期-step5: validator / verify-command knobs (完成判定 B).
    "VALIDATOR_ENABLED_KEY",
    "VALIDATOR_TIMEOUT_MS_KEY",
    "VERIFY_COMMAND_TIMEOUT_MS_KEY",
    "DEFAULT_VALIDATOR_TIMEOUT_MS",
    "MIN_VALIDATOR_TIMEOUT_MS",
    "MAX_VALIDATOR_TIMEOUT_MS",
    "DEFAULT_VERIFY_COMMAND_TIMEOUT_MS",
    "MIN_VERIFY_COMMAND_TIMEOUT_MS",
    "MAX_VERIFY_COMMAND_TIMEOUT_MS",
    "resolve_validator_enabled",
    "resolve_validator_timeout_ms",
    "resolve_verify_command_timeout_ms",
]


#: The ``meta["discussion"]`` key the OFF-by-default execution flag lives under.
IMPLEMENTATION_ENABLED_KEY: Final[str] = "implementation_enabled"


# ---------------------------------------------------------------------------
# Feature-item extractor (planner) config — DISC-1 二期-step2 (§22.4 / §22.12#4)
# ---------------------------------------------------------------------------
#: The ``meta["discussion"]`` key naming the model the extractor should use.
#: When absent the resolver falls through the §22.12#4 ladder (manager model →
#: roster[0] → global lightweight default).  Mirrors
#: :data:`intent_classifier_defaults.INTENT_CLASSIFIER_MODEL_KEY`'s role for the
#: classifier — a per-conversation override that defaults to "let the ladder
#: decide" when unset.
IMPLEMENTATION_PLANNER_MODEL_KEY: Final[str] = "implementation_planner_model"

#: ``meta["discussion"]`` key for the user-tunable planner timeout (TODO-2).
#: Absent ⇒ the constant default below (legacy untouched).
IMPLEMENTATION_PLANNER_TIMEOUT_MS_KEY: Final[str] = (
    "implementation_planner_timeout_ms"
)

#: Default extraction timeout (§22.4).  Extraction is a HEAVIER turn than the
#: grey-zone intent classifier (it must read the conclusion + recent turns and
#: emit a structured multi-item plan), so it is given a longer budget than the
#: classifier's 2000ms.  Tunable: a slower model / longer discussion may warrant
#: raising this; the use case wraps the call in ``asyncio.wait_for`` so a hung
#: extractor never wedges — it degrades to a ``planning_failed`` plan.
DEFAULT_PLANNER_TIMEOUT_MS: Final[int] = 8000

#: Lower bound for a user-supplied planner timeout (a too-small value would make
#: every extraction time out into ``planning_failed``).
MIN_PLANNER_TIMEOUT_MS: Final[int] = 500

#: Upper bound for a user-supplied planner timeout (defence-in-depth: matches the
#: REST DTO ``le`` so a value that bypasses the DTO — e.g. a direct use-case call
#: — can never wedge ``asyncio.wait_for`` on a near-infinite timeout).
MAX_PLANNER_TIMEOUT_MS: Final[int] = 120000

#: §22.4 says a healthy extraction yields ~3–7 feature items.  The LOWER bound is
#: relaxed to 1 here so a genuinely tiny "one concrete task" conclusion is not
#: rejected as a parse failure (over-strict gating would force a needless
#: ``planning_failed`` for a perfectly valid single-item plan).
MIN_FEATURE_ITEMS: Final[int] = 1

#: §22.4 upper bound.  An extractor reply with more items than this is TRUNCATED
#: (kept, not failed) — a chatty model proposing 12 micro-tasks still produces a
#: usable plan rather than a hard failure.
MAX_FEATURE_ITEMS: Final[int] = 7


def resolve_planner_model(
    discussion: dict[str, Any] | None,
    *,
    roster_model: str | None,
) -> str | None:
    """Resolve the model the feature-item extractor should use (§22.12#4).

    Pure function.  Priority ladder (mirrors the intent classifier's §22A.5
    ladder, adapted for the planner):

    1. an explicit, non-blank ``implementation_planner_model`` on the persisted
       discussion config (the manager / per-conversation override);
    2. otherwise ``roster_model`` (the caller passes ``roster[0].model_id`` — the
       discussion's manager / default speaker model).

    Returns ``None`` only when every rung is empty, leaving the adapter / LLM
    port to fall through to its own default (the same "let the downstream
    decide" contract the classifier uses).  The discussion is self-contained —
    it never depends on an external (tab-selected) model id.
    """
    explicit = (discussion or {}).get(IMPLEMENTATION_PLANNER_MODEL_KEY)
    if isinstance(explicit, str):
        stripped = explicit.strip()
        if stripped:
            return stripped
    return roster_model or None


def resolve_planner_timeout_ms(discussion: dict[str, Any] | None) -> int:
    """Resolve the feature-item extractor timeout in ms (TODO-2, §22.4).

    Pure function: a non-numeric / missing value falls back to
    :data:`DEFAULT_PLANNER_TIMEOUT_MS`; a too-small value is raised to
    :data:`MIN_PLANNER_TIMEOUT_MS` (so a hostile / fat-fingered tiny value cannot
    make every extraction time out).  ``bool`` is rejected explicitly.
    """
    value = (discussion or {}).get(IMPLEMENTATION_PLANNER_TIMEOUT_MS_KEY)
    if isinstance(value, bool):
        return DEFAULT_PLANNER_TIMEOUT_MS
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value == value:
        n = int(value)
    else:
        return DEFAULT_PLANNER_TIMEOUT_MS
    return max(MIN_PLANNER_TIMEOUT_MS, min(MAX_PLANNER_TIMEOUT_MS, n))


def resolve_implementation_enabled(discussion: dict[str, Any] | None) -> bool:
    """Return whether implementation-mode execution is enabled (DISC-1 §22.7).

    Pure + deterministic + conservative: ``True`` ONLY when the persisted
    discussion config carries a truthy ``implementation_enabled`` value.  A
    missing key, ``None`` config, or any falsy value (``False`` / ``0`` / ``""``)
    resolves to ``False`` — so the flag defaults OFF and an ``implement`` turn
    stays a discussion until a backend operator explicitly opts in.
    """
    return bool((discussion or {}).get(IMPLEMENTATION_ENABLED_KEY))


# ---------------------------------------------------------------------------
# Validator / verify-command config — DISC-1 三期-step5 + 完成判定 B (§22.4)
# ---------------------------------------------------------------------------
# Two orthogonal, independently-gated quality gates layered on top of the
# simplified A judgement (``done = clean finish AND no tool_error``):
#
#   1. **verify_command (完成判定 B)** — a PER-ITEM command (``FeatureItem.
#      verify_command``).  When set on an item, the orchestrator runs it through
#      the SHARED ai_coding ``exec`` tool channel (timeout / output cap / cwd
#      clamp / denylist all reused — 细则 2 复用>重造) after the agent finishes;
#      a non-zero exit marks the item ``failed``.  This is per-item data, not a
#      discussion knob — it has NO enable flag (a set command always runs; an
#      empty command is simply skipped).  Only its TIMEOUT is a discussion knob.
#   2. **LLM validator (step5)** — an OPTIONAL independent low-temperature LLM
#      review (NO new participant role): reads the item's acceptance_criteria +
#      the agent's result_summary and answers pass/fail.  OFF by default; gated
#      by ``implementation_validator_enabled``.  ALWAYS degrades to "pass" on
#      timeout / error / illegal reply (never blocks the run, never flips a
#      clean item to failed on its own infra hiccup — State-Truth-First).
#
# When BOTH a verify_command and the validator are configured, an item is
# ``done`` only if A (clean + no tool_error) AND B (verify exit 0) AND the
# validator passes; ANY failing gate ⇒ ``failed`` with ``last_error`` recording
# which gate failed (谁不过记谁).

#: ``meta["discussion"]`` key gating the OPTIONAL LLM validator review.  Absent /
#: falsy ⇒ OFF (legacy + every existing conversation untouched: no extra LLM
#: call, judgement stays pure A + any per-item verify_command).
VALIDATOR_ENABLED_KEY: Final[str] = "implementation_validator_enabled"

#: ``meta["discussion"]`` key for the validator LLM call timeout (ms).
VALIDATOR_TIMEOUT_MS_KEY: Final[str] = "implementation_validator_timeout_ms"

#: ``meta["discussion"]`` key for the per-item verify-command exec timeout (ms).
VERIFY_COMMAND_TIMEOUT_MS_KEY: Final[str] = (
    "implementation_verify_command_timeout_ms"
)

#: Default validator review timeout — a single short pass/fail call, budgeted
#: like the extractor (8s).  Wrapped in ``asyncio.wait_for``; a hung validator
#: degrades to "pass" rather than wedging the run.
DEFAULT_VALIDATOR_TIMEOUT_MS: Final[int] = 8000
#: Floor for a user-supplied validator timeout (too-small ⇒ every review times
#: out → degrades to pass, harmless but useless; keep a sane minimum).
MIN_VALIDATOR_TIMEOUT_MS: Final[int] = 500
#: Ceiling for a user-supplied validator timeout (defence-in-depth; matches DTO).
MAX_VALIDATOR_TIMEOUT_MS: Final[int] = 120000

#: Default verify-command exec timeout — verification suites (``pytest`` /
#: ``npm run build``) can be slow, so this is generous (120s).  Still clamped by
#: the ai_coding exec sandbox's own ceiling at runtime.
DEFAULT_VERIFY_COMMAND_TIMEOUT_MS: Final[int] = 120000
#: Floor for a user-supplied verify-command timeout.
MIN_VERIFY_COMMAND_TIMEOUT_MS: Final[int] = 1000
#: Ceiling for a user-supplied verify-command timeout (defence-in-depth; DTO le).
MAX_VERIFY_COMMAND_TIMEOUT_MS: Final[int] = 600000


def resolve_validator_enabled(discussion: dict[str, Any] | None) -> bool:
    """Return whether the OPTIONAL LLM validator review is enabled (step5).

    Pure + conservative, mirroring :func:`resolve_implementation_enabled`:
    ``True`` ONLY when the persisted discussion config carries a truthy
    ``implementation_validator_enabled``.  Missing / ``None`` / falsy ⇒ ``False``
    so the validator stays OFF and judgement remains the simplified A rule (plus
    any per-item ``verify_command``, which is independent of this flag).
    """
    return bool((discussion or {}).get(VALIDATOR_ENABLED_KEY))


def resolve_validator_timeout_ms(discussion: dict[str, Any] | None) -> int:
    """Resolve the validator LLM review timeout in ms (step5).

    Pure: non-numeric / missing ⇒ :data:`DEFAULT_VALIDATOR_TIMEOUT_MS`; a
    too-small value is raised to :data:`MIN_VALIDATOR_TIMEOUT_MS`; ``bool`` is
    rejected explicitly (mirrors :func:`resolve_planner_timeout_ms`).
    """
    value = (discussion or {}).get(VALIDATOR_TIMEOUT_MS_KEY)
    if isinstance(value, bool):
        return DEFAULT_VALIDATOR_TIMEOUT_MS
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value == value:
        n = int(value)
    else:
        return DEFAULT_VALIDATOR_TIMEOUT_MS
    return max(MIN_VALIDATOR_TIMEOUT_MS, min(MAX_VALIDATOR_TIMEOUT_MS, n))


def resolve_verify_command_timeout_ms(discussion: dict[str, Any] | None) -> int:
    """Resolve the per-item verify-command exec timeout in ms (完成判定 B).

    Pure: non-numeric / missing ⇒ :data:`DEFAULT_VERIFY_COMMAND_TIMEOUT_MS`; a
    too-small value is raised to :data:`MIN_VERIFY_COMMAND_TIMEOUT_MS`; ``bool``
    is rejected explicitly.
    """
    value = (discussion or {}).get(VERIFY_COMMAND_TIMEOUT_MS_KEY)
    if isinstance(value, bool):
        return DEFAULT_VERIFY_COMMAND_TIMEOUT_MS
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value == value:
        n = int(value)
    else:
        return DEFAULT_VERIFY_COMMAND_TIMEOUT_MS
    return max(
        MIN_VERIFY_COMMAND_TIMEOUT_MS, min(MAX_VERIFY_COMMAND_TIMEOUT_MS, n)
    )
