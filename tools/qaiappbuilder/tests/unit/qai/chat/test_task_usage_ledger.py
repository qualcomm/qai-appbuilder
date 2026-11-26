# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Per-user-task token ledger: per-round billed口径 + per-task merge."""

from __future__ import annotations

from qai.chat.application.task_usage_ledger import (
    TaskUsageLedger,
    billed_round_usage,
)

CLAUDE = "claude-sonnet"
GPT = "gpt-4o"


# ── per-round billed口径 ────────────────────────────────────────────────
def test_claude_uncorrected_adds_cache_split_back() -> None:
    # prompt_tokens = non-cached delta; cache read split out.
    u = {"prompt_tokens": 10, "completion_tokens": 5, "cache_read_tokens": 900}
    assert billed_round_usage(u, CLAUDE) == {
        "input": 910, "output": 5, "cache_read": 900, "cache_write": 0,
        "reasoning": 0,
    }


def test_claude_uncorrected_cache_write_in_details() -> None:
    u = {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "cache_read_tokens": None,
        "prompt_tokens_details": {"cache_write_tokens": 500},
    }
    assert billed_round_usage(u, CLAUDE) == {
        "input": 502, "output": 3, "cache_read": 0, "cache_write": 500,
        "reasoning": 0,
    }


def test_corrected_prompt_already_includes_cache() -> None:
    # _extract_usage corrected prompt to total-completion: cache_read zeroed,
    # observed fields carry the display breakdown — must NOT be added again.
    u = {
        "prompt_tokens": 416853,
        "completion_tokens": 1740,
        "cache_read_tokens": None,
        "cache_write_observed": 416849,
        "prompt_includes_cache": True,
    }
    assert billed_round_usage(u, CLAUDE) == {
        "input": 416853, "output": 1740, "cache_read": 0, "cache_write": 416849,
        "reasoning": 0,
    }


def test_native_anthropic_top_level_cache_creation() -> None:
    # Native Anthropic: no total_tokens → no correction; cache write only in
    # cache_write_observed (top-level cache_creation_input_tokens).
    u = {
        "prompt_tokens": 4,
        "completion_tokens": 9,
        "cache_read_tokens": 1000,
        "cache_write_observed": 300,
    }
    assert billed_round_usage(u, CLAUDE) == {
        "input": 1304, "output": 9, "cache_read": 1000, "cache_write": 300,
        "reasoning": 0,
    }


def test_openai_cached_tokens_inside_prompt() -> None:
    u = {"prompt_tokens": 1000, "completion_tokens": 50, "cache_read_tokens": 800}
    assert billed_round_usage(u, GPT) == {
        "input": 1000, "output": 50, "cache_read": 800, "cache_write": 0,
        "reasoning": 0,
    }


def test_reasoning_is_portion_of_output_and_summed_per_task() -> None:
    # Real route-1 round: reasoning counted INSIDE completion_tokens.
    u = {"prompt_tokens": 39299, "completion_tokens": 50,
         "cache_read_tokens": 39168, "reasoning_tokens": 13}
    assert billed_round_usage(u, GPT)["reasoning"] == 13
    led = TaskUsageLedger()
    led.open_task("t", conversation_id="c", started_ms=0, model=GPT)
    led.add_round("t", u, model_id=GPT)
    led.begin_subagent("t", "sa", "x")
    led.add_round("t", {"prompt_tokens": 10, "completion_tokens": 40,
                        "reasoning_tokens": 41}, model_id=GPT, subagent_id="sa")
    s = led.snapshot("t", now_ms=1)
    assert s["output_tokens"] == 90
    assert s["reasoning_tokens"] == 13 + 40  # clamped to its round's output
    assert s["total_tokens"] == 39299 + 10 + 90  # reasoning not re-added


def test_no_usage() -> None:
    assert billed_round_usage(None, GPT) is None
    assert billed_round_usage({"prompt_tokens": 0, "completion_tokens": 0}, GPT) is None


# ── per-task merge ──────────────────────────────────────────────────────
def _ledger() -> TaskUsageLedger:
    led = TaskUsageLedger()
    led.open_task("t1", conversation_id="c1", started_ms=1_000, model=GPT)
    return led


def test_every_round_summed_not_last_round() -> None:
    led = _ledger()
    # 3 main rounds re-sending a growing prompt: billed = SUM of each round.
    for pt in (100, 200, 300):
        led.add_round("t1", {"prompt_tokens": pt, "completion_tokens": 10}, model_id=GPT)
    s = led.snapshot("t1", now_ms=22_400)
    assert s is not None
    assert s["input_tokens"] == 600
    assert s["output_tokens"] == 30
    assert s["total_tokens"] == 630
    assert s["main_rounds"] == 3
    assert s["duration_ms"] == 21_400  # wall clock, not a sum


def test_subagents_summed_and_resumed_history_excluded() -> None:
    led = _ledger()
    led.add_round("t1", {"prompt_tokens": 100, "completion_tokens": 10}, model_id=GPT)
    # A resumed sub-agent whose session has huge historical usage: only the
    # rounds it runs for THIS task are booked (ledger never reads history).
    led.begin_subagent("t1", "sa1", "reviewer")
    led.add_round("t1", {"prompt_tokens": 50, "completion_tokens": 5},
                  model_id=GPT, subagent_id="sa1")
    led.begin_subagent("t1", "sa2", "explorer")
    led.add_round("t1", {"prompt_tokens": 70, "completion_tokens": 7},
                  model_id=CLAUDE, subagent_id="sa2")
    s = led.snapshot("t1", now_ms=2_000)
    assert s is not None
    assert s["total_tokens"] == 110 + 55 + 77
    assert s["main_tokens"] == 110
    assert {x["id"]: x["tokens"] for x in s["sub_agents"]} == {"sa1": 55, "sa2": 77}
    assert s["complete"] is False
    led.end_subagent("t1", "sa1")
    led.end_subagent("t1", "sa2")
    assert led.snapshot("t1", now_ms=3_000)["complete"] is True


def test_rounds_of_other_tasks_do_not_leak() -> None:
    led = _ledger()
    led.open_task("t2", conversation_id="c1", started_ms=5_000, model=GPT)
    led.begin_subagent("t1", "sa1", "a")
    led.add_round("t1", {"prompt_tokens": 10, "completion_tokens": 1},
                  model_id=GPT, subagent_id="sa1")
    # Same sub-agent woken later by task t2: books into t2 only.
    led.begin_subagent("t2", "sa1", "a")
    led.add_round("t2", {"prompt_tokens": 40, "completion_tokens": 4},
                  model_id=GPT, subagent_id="sa1")
    assert led.snapshot("t1", now_ms=9_000)["total_tokens"] == 11
    assert led.snapshot("t2", now_ms=9_000)["total_tokens"] == 44
    assert led.task_for_subagent("sa1") == "t2"
    assert led.latest_task("c1") == "t2"


def test_unknown_task_is_noop() -> None:
    led = TaskUsageLedger()
    led.add_round(None, {"prompt_tokens": 1, "completion_tokens": 1}, model_id=GPT)
    led.add_round("nope", {"prompt_tokens": 1, "completion_tokens": 1}, model_id=GPT)
    assert led.snapshot("nope", now_ms=0) is None


def test_eviction_bounded() -> None:
    led = TaskUsageLedger(max_tasks=2)
    for i in range(3):
        led.open_task(f"t{i}", conversation_id="c", started_ms=0, model=None)
    assert not led.has_task("t0")
    assert led.has_task("t2")
