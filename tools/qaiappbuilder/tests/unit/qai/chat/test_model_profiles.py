# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.chat.domain.model_profiles`` — family defaults + override layers.

Covers the external-skill ``max_tokens`` bug (2026-11): Qwen/DeepSeek turns
were silently capped at 4096, truncating large tool-call arguments (e.g. a
long ``write``) mid-stream. This file locks in:

* the raised, named-family hardcoded defaults (regression guard — don't
  silently slide back to 4096);
* the new ``glm`` family (previously fell through to ``unknown``, i.e.
  "no max_tokens sent" by accident rather than by design);
* the ``family_overrides`` layer (``config/model_family_overrides.json``,
  threaded in by ``apps/api/_chat_di.py``) that lets an operator retune — or,
  via an explicit ``null`` default, turn OFF — a family's ``max_tokens``
  without a code change;
* the previously-dead ``explicit_overrides["default"]`` (per-model catalog
  ``params.max_tokens.default``) now actually being read by
  ``resolve_max_tokens``.

Previously this module had zero unit tests (only indirect e2e coverage via
``tests/e2e/test_chat_cloud_key.py`` against a generic, unknown-family
"e2e-model" id, which never touched the family table itself).
"""

from __future__ import annotations

from qai.chat.domain.model_profiles import get_model_profile


# ── named-family hardcoded defaults (regression guard) ──────────────────────


def test_qwen_family_default_is_no_longer_4096() -> None:
    profile = get_model_profile("qwen3.7-max")
    assert profile.family_name == "qwen"
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 131072


def test_qwen_reasoner_family_default_is_no_longer_4096() -> None:
    profile = get_model_profile("qwq-32b")
    assert profile.family_name == "qwen_reasoner"
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 131072


def test_deepseek_family_default_is_no_longer_4096() -> None:
    profile = get_model_profile("deepseek-v3.1")
    assert profile.family_name == "deepseek"
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 65536


def test_deepseek_reasoner_family_default_is_no_longer_4096() -> None:
    profile = get_model_profile("deepseek-r1")
    assert profile.family_name == "deepseek_reasoner"
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 65536


def test_glm_now_has_its_own_family_instead_of_falling_through_to_unknown() -> None:
    profile = get_model_profile("glm-4.6")
    assert profile.family_name == "glm"
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 131072


def test_unknown_model_still_sends_no_max_tokens_by_default() -> None:
    # Pinned by tests/e2e/test_chat_cloud_key.py::
    # test_with_no_override_the_cloud_request_carries_the_default_temperature_only
    # — must stay untouched by the family-table changes above.
    profile = get_model_profile("e2e-model")
    assert profile.family_name == "unknown"
    assert profile.resolve_max_tokens() is None
    assert profile.resolve_temperature() == 0.7


def test_max_tokens_zero_falls_through_to_family_default_unchanged_behavior() -> None:
    # Pre-existing behavior, UNCHANGED by this fix: resolve_max_tokens() does
    # not special-case 0 as "omit" -- it's simply "no value supplied here", so
    # it cascades to the next-lower-priority source, same as ``None`` would.
    # For "unknown" (no family default) that cascade bottoms out at ``None``
    # -- that's what tests/e2e/test_chat_cloud_key.py::
    # test_max_tokens_zero_means_no_limit_and_is_not_sent actually pins (its
    # model_id is "e2e-model", unknown family). For a NAMED family the SAME
    # cascade bottoms out at that family's default instead -- true even with
    # the old 4096 default; raising it to 32768 only changes the value it
    # lands on, not the cascade.
    profile = get_model_profile("qwen3.7-max")
    assert profile.resolve_max_tokens(user_value=0) == 32768


# ── family_overrides layer (config/model_family_overrides.json) ─────────────


def test_family_override_raises_a_familys_default_above_hardcoded() -> None:
    profile = get_model_profile(
        "qwen3.7-max",
        family_overrides={"qwen": {"max_tokens_default": 20000, "max_tokens_max": 40000}},
    )
    assert profile.max_tokens_default == 20000
    assert profile.max_tokens_max == 40000
    assert profile.resolve_max_tokens() == 20000


def test_family_override_can_explicitly_disable_max_tokens_for_one_family() -> None:
    # Q1's "let the model decide" experiment: an operator can flip this one
    # family off (e.g. to test whether qai-service/the vendor's own default
    # is more generous) without touching code.
    profile = get_model_profile(
        "qwen3.7-max",
        family_overrides={"qwen": {"max_tokens_default": None}},
    )
    assert profile.max_tokens_default is None
    assert profile.resolve_max_tokens() is None
    # max_tokens_max wasn't mentioned in the override → stays the hardcoded
    # family value (presence-of-key, not truthiness, drives the merge).
    assert profile.max_tokens_max == 131072


def test_family_override_missing_entry_falls_back_to_hardcoded() -> None:
    profile = get_model_profile(
        "qwen3.7-max",
        family_overrides={"deepseek": {"max_tokens_default": 1}},
    )
    assert profile.max_tokens_default == 32768
    assert profile.max_tokens_max == 131072


def test_family_overrides_none_is_a_no_op() -> None:
    assert get_model_profile("qwen3.7-max", family_overrides=None).max_tokens_default == 32768
    assert get_model_profile("qwen3.7-max", family_overrides={}).max_tokens_default == 32768


def test_a_different_family_is_unaffected_by_an_unrelated_override() -> None:
    profile = get_model_profile(
        "claude-sonnet-4",
        family_overrides={"qwen": {"max_tokens_default": 1}},
    )
    assert profile.family_name == "claude_sonnet"
    assert profile.max_tokens_default == 16384  # unchanged claude_sonnet default


# ── explicit_overrides["default"] (per-model catalog params.max_tokens.default) ──
# Previously accepted by get_model_profile() into ModelProfile.explicit_overrides
# but never consulted by resolve_max_tokens() -- a per-model config "default"
# could never actually raise the outgoing value. Priority after the fix:
# user_value > explicit_default (caller arg) > explicit_overrides["default"]
# (per-model config) > family default.


def test_explicit_params_default_now_raises_the_value_above_family_default() -> None:
    profile = get_model_profile(
        "qwen3.7-max",
        explicit_params={"default": 24000},
    )
    assert profile.resolve_max_tokens() == 24000


def test_explicit_params_default_loses_to_an_actual_user_value() -> None:
    profile = get_model_profile("qwen3.7-max", explicit_params={"default": 24000})
    assert profile.resolve_max_tokens(user_value=500) == 500


def test_explicit_params_default_loses_to_the_resolve_call_explicit_default_arg() -> None:
    profile = get_model_profile("qwen3.7-max", explicit_params={"default": 24000})
    assert profile.resolve_max_tokens(explicit_default=777) == 777


def test_explicit_params_default_is_still_clamped_by_explicit_params_max() -> None:
    profile = get_model_profile(
        "qwen3.7-max",
        explicit_params={"default": 24000, "max": 9000},
    )
    assert profile.resolve_max_tokens() == 9000


def test_explicit_params_and_family_overrides_can_combine() -> None:
    # Per-model config (explicit_params) sits ABOVE the per-family override
    # table in priority -- both layers are documented in get_model_profile's
    # four-layer precedence.
    profile = get_model_profile(
        "qwen3.7-max",
        explicit_params={"default": 24000},
        family_overrides={"qwen": {"max_tokens_default": 1, "max_tokens_max": 50000}},
    )
    assert profile.max_tokens_default == 1  # family_overrides merged into the profile
    assert profile.resolve_max_tokens() == 24000  # but explicit_params default still wins
