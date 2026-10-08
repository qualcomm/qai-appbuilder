# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Regression tests for the slow-on-device-inference optimisations.

Covers the changes described in ``EDGE-INFERENCE-OPTIMIZATION.zh-CN.md``:
P0 (serialised local inference), P2a (escalating overflow recovery), P2b
(small-window compaction trigger), P2c (real per-model context window),
P4 (lean prompt / tool set for loopback endpoints), P5 (SKILL.md read
protection), P6 (digest skip-not-cancel), P7 (``exceed_context_size_error``
classification) and P10 (handoff suggestion after a hard overflow).

Deliberately self-contained: it builds the objects under test directly rather
than going through ``tests.unit.qai.chat.fakes`` (absent from this checkout) or
the DI container, so it runs with only the package's runtime dependencies.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import types
from typing import Any

import pytest

from apps.api._context_window_bridge import make_context_window_resolver
from qai.chat.adapters.context_compressor import (
    ThreeLevelContextCompressor,
    _is_skill_file_read,
    _turn_has_skill_file_read,
)
from qai.chat.adapters.tool_result_truncator import (
    DEFAULT_WINDOW_RESULT_RATIO,
    AdaptiveToolResultTruncator,
)
from qai.chat.application.ports import ToolResultTruncationRequest
from qai.chat.adapters.error_classifier import is_prompt_too_long_error
from qai.chat.adapters.local_model_stream import LocalModelStreamAdapter
from qai.chat.application.use_cases._agentic_kernel import (
    SMALL_CONTEXT_COMPRESS_THRESHOLD_RATIO,
    SMALL_CONTEXT_WINDOW_THRESHOLD,
    resolve_inter_round_threshold_ratio,
)
from qai.chat.application.use_cases._compaction_engine import (
    CompactionCheckpointEngine,
)
from qai.chat.application.use_cases.streaming import (
    StreamChatUseCase,
    _is_loopback_base_url,
    _prefers_lean_prompt,
)
from qai.chat.application.use_cases.tool_advertise import (
    LOCAL_EXCLUDED_TOOLS,
    TOOL_ORDER,
    compose_advertised_tools,
    schema_tool_name,
)
from qai.chat.domain.model_profiles import get_context_limit
from qai.chat.infrastructure.llm_stream import (
    _CHAT_CONTROL_KEYS,
    _CONTENT_STALL_TEXT_TURN_SECONDS,
    _CONTENT_STALL_TIMEOUT_SECONDS,
    HttpOpenAICompatibleLLMStream,
    _is_loopback_url,
)

# The real 400 body observed from GenieAPIService / llama.cpp server.
REAL_OVERFLOW_BODY = (
    'upstream returned HTTP 400: {"error":{"code":400,"message":"request '
    '(38061 tokens) exceeds the available context size (36864 tokens), try '
    'increasing it","type":"exceed_context_size_error",'
    '"n_prompt_tokens":38061,"n_ctx":36864}}'
)


# ---------------------------------------------------------------------------
# P7 — the rejection must classify as prompt-too-long, not a generic HTTP error
# ---------------------------------------------------------------------------


def test_p7_genie_overflow_body_classifies_as_prompt_too_long() -> None:
    """The real body must classify, or recovery never runs and the task dies."""
    assert is_prompt_too_long_error(REAL_OVERFLOW_BODY) is True


def test_p7_machine_readable_type_alone_is_enough() -> None:
    """The prose may be reworded upstream; the ``type`` field is the contract."""
    assert is_prompt_too_long_error('{"type":"exceed_context_size_error"}')


def test_p7_max_tokens_param_rejection_still_not_prompt_too_long() -> None:
    """Guard the exclusion: a max_tokens bound is NOT a context overflow."""
    assert not is_prompt_too_long_error(
        "max_tokens above maximum value, expected <= 32000"
    )


# ---------------------------------------------------------------------------
# P2b — small windows compact earlier, and the clamp is one-way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "window", "expected"),
    [
        # Large windows keep the configured ratio untouched.
        (0.80, 200_000, 0.80),
        (0.80, 131_072, 0.80),
        (0.80, SMALL_CONTEXT_WINDOW_THRESHOLD + 1, 0.80),
        # At/below the threshold the ratio is tightened.
        (0.80, SMALL_CONTEXT_WINDOW_THRESHOLD, SMALL_CONTEXT_COMPRESS_THRESHOLD_RATIO),
        (0.80, 32_768, SMALL_CONTEXT_COMPRESS_THRESHOLD_RATIO),
        # ONE-WAY: an operator who already tuned lower keeps their value.
        (0.50, 32_768, 0.50),
        (0.10, 32_768, 0.10),
        # Unknown window ⇒ no basis to tighten, caller's behaviour preserved.
        (0.80, None, 0.80),
        (0.80, 0, 0.80),
        (0.80, -1, 0.80),
    ],
)
def test_p2b_threshold_ratio_resolution(
    configured: float, window: int | None, expected: float
) -> None:
    assert resolve_inter_round_threshold_ratio(configured, window) == expected


def test_p2b_never_raises_ratio_above_configured() -> None:
    """Property: the resolver can only make compaction fire sooner."""
    for configured in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95, 1.0):
        for window in (512, 4096, 32_768, 65_536, 65_537, 131_072, 1_000_000):
            assert (
                resolve_inter_round_threshold_ratio(configured, window)
                <= configured
            )


def test_p2b_glymur_case_trigger_becomes_reachable() -> None:
    """The concrete failure: a 32768-token window with a 0.80 trigger.

    Under the family-table guess (131072) the trigger sat at 104857 tokens —
    unreachable. With the real window AND the small-window ratio it lands at
    ~21K, comfortably inside the window.
    """
    real_window = 32_768
    ratio = resolve_inter_round_threshold_ratio(0.80, real_window)
    trigger_at = int(real_window * ratio)
    assert trigger_at < real_window
    # Leaves room for a ~20K-token tool result landing right after the check.
    assert real_window - trigger_at > 10_000


# ---------------------------------------------------------------------------
# P2a — escalating recovery targets strictly decrease and terminate
# ---------------------------------------------------------------------------


def test_p2a_escalation_strictly_decreases_then_stops() -> None:
    nxt = StreamChatUseCase._next_overflow_recovery_ratio
    ratio = 0.175
    seen = [ratio]
    for _ in range(50):
        following = nxt(ratio)
        if following is None:
            break
        assert following < ratio, "each attempt must aim strictly lower"
        ratio = following
        seen.append(ratio)
    else:  # pragma: no cover - would mean the ladder never terminates
        pytest.fail("escalation did not terminate")
    assert len(seen) >= 2


def test_p2a_escalation_stops_instead_of_repeating_a_target() -> None:
    """A non-decreasing target would re-send the wire that was just rejected."""
    nxt = StreamChatUseCase._next_overflow_recovery_ratio
    from qai.chat.application.use_cases import streaming as _s

    floor = _s._OVERFLOW_RECOVERY_ESCALATION_FLOOR
    # Already at/below the floor => nothing lower to try.
    assert nxt(floor) is None
    assert nxt(floor / 2) is None
    # Just above the floor still yields one more (strictly lower) attempt.
    nxt_above = nxt(floor * 4)
    assert nxt_above is not None and nxt_above < floor * 4


# ---------------------------------------------------------------------------
# P4 — loopback detection + lean tool set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8910/v1", True),
        ("http://localhost:8910/v1", True),
        ("http://LOCALHOST:8910/v1", True),
        ("http://127.5.5.5:1234", True),
        ("http://[::1]:8910/v1", True),
        ("https://api.example.com/v1", False),
        ("http://10.0.0.5:8910/v1", False),
        ("", False),
        (None, False),
        ("not a url", False),
    ],
)
def test_p4_loopback_base_url_detection(url: str | None, expected: bool) -> None:
    assert _is_loopback_base_url(url) is expected


def test_p4_prefers_lean_prompt_sources() -> None:
    # The ``local::`` route alone is enough.
    assert _prefers_lean_prompt("local::qwen", None) is True
    # A cloud-routed model with the loopback flag set also qualifies.
    assert _prefers_lean_prompt("Glymur_QWEN3.8", {"_is_loopback_endpoint": True})
    # A genuine cloud model is untouched.
    assert not _prefers_lean_prompt("claude-x", {"_is_loopback_endpoint": False})
    assert not _prefers_lean_prompt("claude-x", {})
    assert not _prefers_lean_prompt("claude-x", None)


def test_p4_loopback_flag_is_filtered_off_the_wire() -> None:
    """An internal ``extra`` key must be registered, not just underscored.

    The payload builder forwards every unrecognised ``extra`` key into the
    request body, so an unregistered key is sent to the provider verbatim.
    """
    assert "_is_loopback_endpoint" in _CHAT_CONTROL_KEYS


def test_p4_lean_tool_set_is_the_eight_essentials() -> None:
    advertised = [
        {"type": "function", "function": {"name": name, "parameters": {}}}
        for name in TOOL_ORDER
    ]
    lean = compose_advertised_tools(
        advertised,
        tool_mode="app-builder",
        is_local=True,
        excluded=LOCAL_EXCLUDED_TOOLS,
        inject_agent=True,
        agent_schema_factory=lambda: {
            "type": "function",
            "function": {"name": "agent", "parameters": {}},
        },
    )
    assert [schema_tool_name(s) for s in lean] == [
        "read", "edit", "write", "exec", "glob", "grep", "list", "agent",
    ]


def test_p4_cloud_turn_tool_set_is_unchanged_by_the_lean_set() -> None:
    """A cloud turn must still see the full set — no accidental narrowing."""
    advertised = [
        {"type": "function", "function": {"name": name, "parameters": {}}}
        for name in TOOL_ORDER
    ]
    cloud = compose_advertised_tools(
        advertised,
        tool_mode="code",
        is_local=False,
        excluded=frozenset(),
        inject_agent=True,
        agent_schema_factory=lambda: {
            "type": "function",
            "function": {"name": "agent", "parameters": {}},
        },
    )
    names = {schema_tool_name(s) for s in cloud}
    assert {"web_search", "browser", "skill", "todowrite"} <= names
    assert len(names) > 8


def test_p4_excluded_names_all_exist() -> None:
    """A typo here would silently fail to exclude a tool."""
    assert set(TOOL_ORDER) >= LOCAL_EXCLUDED_TOOLS


# ---------------------------------------------------------------------------
# P5 — SKILL.md reads survive compaction
# ---------------------------------------------------------------------------


def _read_call(path: str, call_id: str = "c1") -> dict[str, Any]:

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"path": path})},
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills/model-builder/SKILL.md", True),
        ("skills/x/skill.md", True),
        ("SKILL.md", True),
        (r"C:\proj\skills\a\SKILL.MD", True),
        ("a/SKILL.md:120-200", True),
        ("src/main.py", False),
        ("a/MYSKILL.md", False),
        ("skills/readme.md", False),
    ],
)
def test_p5_skill_read_matching(path: str, expected: bool) -> None:
    assert _is_skill_file_read(_read_call(path)) is expected


def test_p5_non_read_tools_do_not_match() -> None:

    for tool in ("list", "grep", "write", "edit"):
        call = {
            "id": "c1",
            "type": "function",
            "function": {
                "name": tool,
                "arguments": json.dumps({"path": "a/SKILL.md"}),
            },
        }
        assert _is_skill_file_read(call) is False


def test_p5_malformed_calls_are_not_matched() -> None:
    assert _is_skill_file_read("garbage") is False
    assert _is_skill_file_read({"function": {"name": "read"}}) is False
    assert (
        _is_skill_file_read(
            {"function": {"name": "read", "arguments": "{not json"}}
        )
        is False
    )


def _skill_turn() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "use the model-builder skill"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_read_call("skills/model-builder/SKILL.md")],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "SKILL BODY " * 200},
    ]


def _bulky_turn(marker: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"do {marker}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_read_call(f"src/{marker}.py", call_id=marker)],
        },
        {"role": "tool", "tool_call_id": marker, "content": "X" * 4000},
    ]


def test_p5_drop_scan_keeps_the_skill_turn_and_drops_others() -> None:
    compressor = ThreeLevelContextCompressor()
    turns = [_bulky_turn("a"), _skill_turn(), _bulky_turn("b"), _bulky_turn("c")]
    kept = compressor._drop_oldest_turns(
        turns, target_tokens=400, fixed_tokens=0, tok_per_byte=0.25,
    )
    assert any(_turn_has_skill_file_read(t) for t in kept), (
        "the SKILL.md turn must survive the drop scan"
    )
    # The bulky turns are the ones sacrificed.
    assert len(kept) < len(turns)


def test_p5_drop_scan_unchanged_when_no_skill_turn_present() -> None:
    """Without a skill read this must behave exactly like the old front-pop."""
    compressor = ThreeLevelContextCompressor()
    turns = [_bulky_turn(m) for m in ("a", "b", "c", "d")]

    def _tok(turn: list[dict[str, Any]]) -> int:
        return compressor._bytes_to_tokens(
            compressor._estimate_bytes(turn), 0.25,
        )

    target, fixed = 2000, 0
    expected = list(turns)
    total = sum(_tok(t) for t in expected)
    while expected and fixed + total > target:
        total -= _tok(expected[0])
        expected.pop(0)

    kept = compressor._drop_oldest_turns(
        turns, target_tokens=target, fixed_tokens=fixed, tok_per_byte=0.25,
    )
    assert kept == expected


def test_p5_strip_phase_keeps_skill_messages_and_their_replies() -> None:
    """Message-level protection: the skill read survives, scaffolding does not."""
    compressor = ThreeLevelContextCompressor()
    stripped = compressor._strip_tool_interactions([_skill_turn()])
    flat = [m for t in stripped for m in t]
    # The skill body is still there...
    assert any(
        m.get("role") == "tool" and "SKILL BODY" in str(m.get("content"))
        for m in flat
    )
    # ...and the assistant row that requested it, so call/reply stays paired.
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in flat)
    ids = {
        c["id"]
        for m in flat
        for c in (m.get("tool_calls") or ())
    }
    replies = {m.get("tool_call_id") for m in flat if m.get("role") == "tool"}
    assert ids == replies, "every kept tool_call must keep its reply"


def test_p5_strip_phase_still_strips_ordinary_scaffolding() -> None:
    compressor = ThreeLevelContextCompressor()
    stripped = compressor._strip_tool_interactions([_bulky_turn("a")])
    flat = [m for t in stripped for m in t]
    assert all(m.get("role") != "tool" for m in flat)
    assert all(not m.get("tool_calls") for m in flat)


def test_p5_single_mega_turn_still_compacts() -> None:
    """REGRESSION (2026-09-30): one user message + many tool rounds = ONE turn.

    Turn-level protection made this whole conversation untouchable, so every
    phase was a no-op (``retain_ratio=1.000``), ``/compact`` reported nothing to
    reclaim, and the prompt stayed at ~30K of a 32768-token window until the
    reply was truncated for lack of room. Message-level protection must reclaim
    the tool scaffolding while still keeping the skill body.
    """
    compressor = ThreeLevelContextCompressor()
    # One user message, then 18 tool rounds — plus a SKILL.md read in the middle.
    mega: list[dict[str, Any]] = [{"role": "user", "content": "convert the model"}]
    mega += _skill_turn()[1:]
    for i in range(18):
        mega += _bulky_turn(f"step{i}")[1:]

    before = compressor._bytes_to_tokens(compressor._estimate_bytes(mega), 0.25)
    stripped = compressor._strip_tool_interactions([mega])
    after = compressor._bytes_to_tokens(
        compressor._estimate_bytes([m for t in stripped for m in t]), 0.25
    )
    assert after < before * 0.6, (
        f"Phase 3 must reclaim the bulk of a mega-turn: {before} -> {after}"
    )
    # ...while the skill instructions survive.
    assert any(
        m.get("role") == "tool" and "SKILL BODY" in str(m.get("content"))
        for t in stripped
        for m in t
    )


def test_p5_mega_turn_compress_reaches_target() -> None:
    """End-to-end on the shape from the real log: 1 turn, way over target."""
    compressor = ThreeLevelContextCompressor()
    wire: list[dict[str, Any]] = [{"role": "user", "content": "convert the model"}]
    wire += _skill_turn()[1:]
    for i in range(18):
        wire += _bulky_turn(f"step{i}")[1:]

    out = asyncio.run(
        compressor.compress(
            wire,
            preserve_tail=4,
            budget_tokens=32_768,
            target_window_ratio=0.35,
            protect_ratio=0.35,
        )
    )
    before = compressor._bytes_to_tokens(compressor._estimate_bytes(wire), 0.25)
    after = compressor._bytes_to_tokens(compressor._estimate_bytes(out), 0.25)
    assert after < before, f"compaction must reclaim something: {before} -> {after}"
    assert any(
        m.get("role") == "tool" and "SKILL BODY" in str(m.get("content"))
        for m in out
    ), "the skill instructions must still be in the compacted wire"


def test_p5_skill_body_survives_a_full_compress_pass() -> None:
    """End-to-end through the real 4-phase compressor."""
    compressor = ThreeLevelContextCompressor()
    wire: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    wire += _bulky_turn("a")
    wire += _skill_turn()
    for marker in ("b", "c", "d", "e", "f"):
        wire += _bulky_turn(marker)

    out = asyncio.run(
        compressor.compress(
            wire,
            preserve_tail=2,
            budget_tokens=32_768,
            target_window_ratio=0.05,
            protect_ratio=0.05,
        )
    )
    assert any(
        isinstance(m.get("content"), str) and "SKILL BODY" in m["content"]
        for m in out
    ), "the skill instructions must still be in the compacted wire"


# ---------------------------------------------------------------------------
# P6 / P10 — compaction engine behaviour
# ---------------------------------------------------------------------------


def _engine(refresh_uc: Any = None) -> CompactionCheckpointEngine:
    return CompactionCheckpointEngine(
        compressor=None,
        threshold_ratio=0.8,
        target_ratio=0.5,
        preserve_tail=4,
        refresh_digest_uc=refresh_uc,
    )


def test_p6_second_kick_is_skipped_while_the_first_still_runs() -> None:
    """The in-flight summary must be left alone, not cancelled and restarted."""
    started = 0
    release = asyncio.Event()

    class _SlowDigest:
        async def execute(self, _input: Any) -> None:
            nonlocal started
            started += 1
            await release.wait()

    async def _scenario() -> tuple[int, bool]:
        engine = _engine(_SlowDigest())
        engine.kick_digest_refresh(checkpoint_key="conv1", input=object())
        first = engine.digest_refresh_task("conv1")
        await asyncio.sleep(0)  # let the task start
        # A second compaction fires while the first summary is mid-flight.
        engine.kick_digest_refresh(checkpoint_key="conv1", input=object())
        second = engine.digest_refresh_task("conv1")
        same_task = first is second
        release.set()
        if second is not None:
            await second
        return started, same_task

    runs, same_task = asyncio.run(_scenario())
    assert same_task, "the running digest task must not be replaced"
    assert runs == 1, "the second kick must not start a competing summary"


def test_p6_kick_after_completion_starts_a_fresh_summary() -> None:
    """Skipping applies only while one is RUNNING — not forever."""
    started = 0

    class _FastDigest:
        async def execute(self, _input: Any) -> None:
            nonlocal started
            started += 1

    async def _scenario() -> int:
        engine = _engine(_FastDigest())
        for _ in range(2):
            engine.kick_digest_refresh(checkpoint_key="conv1", input=object())
            task = engine.digest_refresh_task("conv1")
            if task is not None:
                await task
        return started

    assert asyncio.run(_scenario()) == 2


def test_p10_force_counter_reaches_the_handoff_gate() -> None:
    engine = _engine()
    assert engine.force_consecutive_mid_turn_at_least("conv1", 3) == 3


def test_p10_force_counter_is_monotonic() -> None:
    engine = _engine()
    for _ in range(5):
        engine.increment_consecutive_mid_turn("conv1")
    assert engine.force_consecutive_mid_turn_at_least("conv1", 3) == 5


def test_p10_handoff_still_requires_a_digest_to_carry_over() -> None:
    """Without a digest there is nothing to migrate, so stay quiet."""
    engine = _engine()
    engine.force_consecutive_mid_turn_at_least("conv1", 3)
    assert engine.should_suggest_handoff("conv1") is False


# ---------------------------------------------------------------------------
# P2c — real per-model context window beats the name-keyed family guess
# ---------------------------------------------------------------------------


class _FakeListModels:
    def __init__(self, models: list[Any]) -> None:
        self._models = models

    async def execute(self, *, models_root: Any = None) -> list[Any]:
        return self._models


class _FakeListCloud:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    async def execute(self) -> list[dict[str, Any]]:
        return self._entries


class _LocalModel:
    def __init__(self, name: str, context_length: int) -> None:
        self.name = name
        self.context_length = context_length


def _container(
    *, local: list[Any] | None = None, cloud: list[dict[str, Any]] | None = None
) -> Any:

    return types.SimpleNamespace(
        model_runtime=types.SimpleNamespace(
            list_models_use_case=_FakeListModels(local or []),
        ),
        model_catalog=types.SimpleNamespace(
            list_cloud_models_use_case=_FakeListCloud(cloud or []),
        ),
    )


def test_p2c_resolves_from_the_local_model_scan() -> None:

    resolve = make_context_window_resolver(
        _container(local=[_LocalModel("Glymur_QWEN3.8", 32_768)])
    )
    assert asyncio.run(resolve("Glymur_QWEN3.8")) == 32_768
    # The ``local::`` routing prefix must not defeat the lookup.
    assert asyncio.run(resolve("local::Glymur_QWEN3.8")) == 32_768


def test_p2c_falls_back_to_the_cloud_catalog() -> None:

    resolve = make_context_window_resolver(
        _container(cloud=[{"model_id": "Glymur_QWEN3.8", "context_length": 40_960}])
    )
    assert asyncio.run(resolve("Glymur_QWEN3.8")) == 40_960


def test_p2c_local_scan_wins_over_the_catalog() -> None:
    """A loopback model's own artefact metadata is the authoritative window."""
    resolve = make_context_window_resolver(
        _container(
            local=[_LocalModel("M", 32_768)],
            cloud=[{"model_id": "M", "context_length": 131_072}],
        )
    )
    assert asyncio.run(resolve("M")) == 32_768


def test_p2c_unknown_model_reports_a_miss() -> None:
    """A miss must be ``None`` so the CALLER applies the family-table fallback."""
    resolve = make_context_window_resolver(_container())
    assert asyncio.run(resolve("NeverHeardOf")) is None
    assert asyncio.run(resolve(None)) is None
    assert asyncio.run(resolve("")) is None


@pytest.mark.parametrize("bogus", [0, -1, 7, True, "32768", None, 10**9])
def test_p2c_implausible_configured_values_are_rejected(bogus: Any) -> None:
    """A nonsense window must not be trusted over the family guess."""
    resolve = make_context_window_resolver(
        _container(local=[_LocalModel("M", bogus)])
    )
    assert asyncio.run(resolve("M")) is None


def test_p2c_source_failures_degrade_to_a_miss() -> None:

    class _Boom:
        async def execute(self, **_kw: Any) -> Any:
            raise RuntimeError("catalog unavailable")

    container = types.SimpleNamespace(
        model_runtime=types.SimpleNamespace(list_models_use_case=_Boom()),
        model_catalog=types.SimpleNamespace(list_cloud_models_use_case=_Boom()),
    )
    assert asyncio.run(make_context_window_resolver(container)("M")) is None


def test_p2c_missing_contexts_degrade_to_a_miss() -> None:
    """A minimal container (neither context wired) must not raise."""
    assert (
        asyncio.run(make_context_window_resolver(types.SimpleNamespace())("M"))
        is None
    )


def _limit_resolver(resolver: Any) -> Any:
    """A minimal stand-in exposing just what ``_resolve_context_limit`` uses.

    ``_family_context_limit`` must stay a ``staticmethod`` on the stub — the
    real class declares it as one, and re-binding it as a plain attribute would
    make ``self._family_context_limit(hint)`` pass ``self`` as well.
    """
    class _Stub:
        _family_context_limit = staticmethod(
            StreamChatUseCase._family_context_limit
        )

        def __init__(self) -> None:
            self._context_window_resolver = resolver
            self._resolved_context_limits: dict[str, int] = {}

    return _Stub()


def test_p2c_resolve_context_limit_prefers_the_real_window() -> None:
    async def resolver(_hint: str | None) -> int:
        return 32_768

    stub = _limit_resolver(resolver)
    got = asyncio.run(
        StreamChatUseCase._resolve_context_limit(stub, "SomeCustomModel")
    )
    assert got == 32_768
    # And it is cached for the sync badge reader.
    assert (
        StreamChatUseCase._resolve_context_limit_cached(stub, "SomeCustomModel")
        == 32_768
    )


def test_p2c_resolve_context_limit_falls_back_on_a_miss() -> None:

    async def resolver(_hint: str | None) -> None:
        return None

    stub = _limit_resolver(resolver)
    got = asyncio.run(StreamChatUseCase._resolve_context_limit(stub, "Whatever"))
    assert got == get_context_limit("Whatever")


def test_p2c_resolve_context_limit_survives_a_raising_resolver() -> None:

    async def resolver(_hint: str | None) -> int:
        raise RuntimeError("boom")

    stub = _limit_resolver(resolver)
    got = asyncio.run(StreamChatUseCase._resolve_context_limit(stub, "Whatever"))
    assert got == get_context_limit("Whatever")


def test_p2c_unwired_resolver_is_pure_family_table() -> None:

    stub = _limit_resolver(None)
    got = asyncio.run(StreamChatUseCase._resolve_context_limit(stub, "qwen3-max"))
    assert got == get_context_limit("qwen3-max")


def test_p2c_custom_named_local_model_is_the_real_over_estimate() -> None:
    """Documents WHICH mis-sizing this build actually suffers from.

    Unlike the internal report (where a ``qwen3``-matching name was guessed at
    131072), this build's family table already maps ``qwen3`` to 32768. The
    over-estimate here comes from a custom-registered name that matches NO
    family at all and lands on the 200000 ``__unknown__`` bucket — 6x too large
    for a 32K on-device model, which is exactly what makes the compaction
    trigger and the emergency valve unreachable.
    """
    assert get_context_limit("Glymur_Local_27B") == 200_000

    async def resolver(_hint: str | None) -> int:
        return 32_768

    stub = _limit_resolver(resolver)
    assert (
        asyncio.run(
            StreamChatUseCase._resolve_context_limit(stub, "Glymur_Local_27B")
        )
        == 32_768
    )



# ---------------------------------------------------------------------------
# P11 — loopback endpoints must not be held to the tight text-turn stall budget
# ---------------------------------------------------------------------------
# Root cause of the 2026-09-30 field failure: a summary prompt is sent WITHOUT
# tools, so it fell under the 60s "plain text streams smoothly" budget. On a
# local engine the pre-first-token wait is PREFILL (~320s for the observed 26K
# prompt), so all 4 digest attempts were killed with meaningful_chunks=0.


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8910/v1", True),
        ("http://localhost:8910/v1", True),
        ("http://[::1]:8910/v1", True),
        ("https://api.example.com/v1", False),
        ("http://10.0.0.5:8910/v1", False),
        (None, False),
        ("", False),
    ],
)
def test_p11_loopback_url_detection(url: str | None, expected: bool) -> None:
    assert _is_loopback_url(url) is expected


def _transport(url: str | None, **kw: Any) -> HttpOpenAICompatibleLLMStream:
    class _Ids:
        def new_id(self, prefix: str = "") -> str:
            return "id"

    return HttpOpenAICompatibleLLMStream(
        base_url=url, api_key="k", model="m", ids=_Ids(), **kw
    )


def test_p11_loopback_text_turn_gets_the_generous_budget() -> None:
    """A no-tools turn to a local engine must NOT get the 60s budget.

    This is the exact case that failed in the field: a summary prompt carries no
    tools, so it took the tight branch and was killed at 60s while still
    prefilling.
    """
    local = _transport("http://127.0.0.1:8910/v1")
    assert local._is_loopback_endpoint is True
    budget = local._resolve_stall_budget(has_tools=False)
    assert budget >= 320, (
        "must exceed the measured ~320s prefill of a 26K-token summary prompt"
    )
    assert budget != _CONTENT_STALL_TEXT_TURN_SECONDS


def test_p11_cloud_text_turn_keeps_the_tight_budget() -> None:
    """Regression guard: a real cloud gateway must still fail fast."""
    cloud = _transport("https://api.example.com/v1")
    assert cloud._is_loopback_endpoint is False
    assert cloud._resolve_stall_budget(has_tools=False) == 60.0
    assert _CONTENT_STALL_TEXT_TURN_SECONDS == 60.0


def test_p11_tool_turns_keep_the_generous_budget_everywhere() -> None:
    """Unchanged behaviour: an agentic turn was already generous."""
    for url in ("http://127.0.0.1:8910/v1", "https://api.example.com/v1"):
        t = _transport(url)
        assert t._resolve_stall_budget(has_tools=True) == (
            _CONTENT_STALL_TIMEOUT_SECONDS
        )


def test_p11_configured_budget_applies_to_the_generous_branch() -> None:
    local = _transport(
        "http://127.0.0.1:8910/v1", content_stall_budget_seconds=1500.0
    )
    assert local._resolve_stall_budget(has_tools=False) == 1500.0
    assert local._resolve_stall_budget(has_tools=True) == 1500.0
    # ...but never to a cloud plain-text turn.
    cloud = _transport(
        "https://api.example.com/v1", content_stall_budget_seconds=1500.0
    )
    assert cloud._resolve_stall_budget(has_tools=False) == 60.0


def test_p11_configured_budget_is_honoured() -> None:
    local = _transport("http://127.0.0.1:8910/v1", content_stall_budget_seconds=1500.0)
    assert local._content_stall_budget_seconds == 1500.0


@pytest.mark.parametrize("bogus", [0, -1, None])
def test_p11_bogus_budget_falls_back_to_the_default(bogus: Any) -> None:
    local = _transport("http://127.0.0.1:8910/v1", content_stall_budget_seconds=bogus)
    assert local._content_stall_budget_seconds is None

# ---------------------------------------------------------------------------
# P0-REVERT — abandoning a round's stream must NOT block the next round
# ---------------------------------------------------------------------------
# A previous revision serialised per-endpoint requests with a semaphore held
# across the stream generator's lifetime. That deadlocked the agentic loop: the
# tool-call handoff in ``streaming.py`` breaks out of the drain loop and hands
# ``stream_frames`` to the follow-up loop, so the generator stays suspended
# inside the ``async with`` AND referenced (hence never finalized) — and the
# next round blocked on the permit forever. Field symptom: the turn died on its
# first tool-calling round, 30 min later ``frame_stream_stalled 1800s``.
#
# These tests lock the invariant in: whatever concurrency control is used, an
# abandoned round must never wedge the endpoint.


def _abandon_then_reopen(adapter: Any) -> float:
    """Consume 2 frames, break (keeping the generator alive), reopen, time it."""

    async def _scenario() -> float:
        stream_frames = adapter.stream(object())
        got = []
        async for f in stream_frames:
            got.append(f)
            if len(got) == 2:
                break  # exactly what streaming.py's tool-call handoff does
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        async def _second() -> None:
            async for _ in adapter.stream(object()):
                pass

        await asyncio.wait_for(_second(), timeout=10)
        elapsed = loop.time() - t0
        _ = stream_frames  # keep it referenced to the very end (as the real loop does)
        return elapsed

    return asyncio.run(_scenario())


def test_p0revert_local_adapter_abandoned_round_does_not_block_next() -> None:
    class _Adapter(LocalModelStreamAdapter):
        async def _run(self, request: Any):  # type: ignore[override]
            for i in range(5):
                await asyncio.sleep(0)
                yield f"frame{i}"

    adapter = _Adapter(base_url="http://127.0.0.1:8910/v1", ids=None)
    elapsed = _abandon_then_reopen(adapter)
    assert elapsed < 5, f"next round must start promptly, took {elapsed:.1f}s"


def test_p0revert_cloud_transport_abandoned_round_does_not_block_next() -> None:
    class _Ids:
        def new_id(self, prefix: str = "") -> str:
            return "id"

    class _Adapter(HttpOpenAICompatibleLLMStream):
        async def _iter(self, request: Any):  # type: ignore[override]
            for i in range(5):
                await asyncio.sleep(0)
                yield f"frame{i}"

    # Loopback specifically: that is the endpoint class the reverted mutex
    # applied to, so it is the one that used to deadlock.
    adapter = _Adapter(
        base_url="http://127.0.0.1:8080/v1", api_key=None, model="m", ids=_Ids()
    )
    elapsed = _abandon_then_reopen(adapter)
    assert elapsed < 5, f"next round must start promptly, took {elapsed:.1f}s"


def test_p0revert_no_stream_scoped_lock_remains() -> None:
    """Guard against re-introducing a generator-lifetime-scoped permit."""
    class _Ids:
        def new_id(self, prefix: str = "") -> str:
            return "id"

    local = LocalModelStreamAdapter(base_url="http://127.0.0.1:8910/v1", ids=None)
    cloud = HttpOpenAICompatibleLLMStream(
        base_url="http://127.0.0.1:8080/v1", api_key=None, model="m", ids=_Ids()
    )
    for adapter in (local, cloud):
        assert not hasattr(adapter, "_inflight_semaphore"), (
            "a per-stream semaphore deadlocks the agentic loop — see the "
            "REVERTED note in the adapter"
        )


# ---------------------------------------------------------------------------
# P0-SAFE — background summaries yield to a live main turn
# ---------------------------------------------------------------------------
# Root cause of the 1002 `empty_response`: the engine runs `-c 32768` with
# `kv_unified=true` and 4 slots, so ALL slots share ONE 32768-token KV pool.
# Three concurrent requests (main 12683 + digest 14088 + turn-prefix 6232 =
# 33003) overran it by 235 tokens — and the engine's first failure was literally
# "failed to find a memory slot for batch of size 235". All three then died with
# "Context size has been exceeded", which reaches the client as an empty HTTP 200.
#
# Fix direction matters: the BACKGROUND task waits, not the main turn. A
# coroutine owns its lifetime so it can wait and give up; holding a lock on the
# main turn's generator deadlocked (see the P0-REVERT tests above).


def _engine_with_probe(probe, uc, budget=2.0):
    return CompactionCheckpointEngine(
        compressor=None,
        threshold_ratio=0.8,
        target_ratio=0.5,
        preserve_tail=4,
        refresh_digest_uc=uc,
        main_turn_active_probe=probe,
        background_wait_budget_seconds=budget,
    )


class _RecordingDigest:
    def __init__(self) -> None:
        self.runs = 0

    async def execute(self, _input: Any) -> None:
        self.runs += 1


def test_p0safe_digest_runs_immediately_when_no_turn_is_active() -> None:
    uc = _RecordingDigest()

    async def _scenario() -> int:
        engine = _engine_with_probe(lambda: False, uc)
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        return uc.runs

    assert asyncio.run(_scenario()) == 1


def test_p0safe_digest_waits_for_the_main_turn_then_runs() -> None:
    uc = _RecordingDigest()
    active = {"v": True}

    async def _scenario() -> tuple[int, bool]:
        engine = _engine_with_probe(lambda: active["v"], uc, budget=10.0)
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        await asyncio.sleep(0.2)
        ran_while_busy = uc.runs > 0        # must still be 0
        active["v"] = False                 # main turn finishes
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        return uc.runs, ran_while_busy

    runs, ran_while_busy = asyncio.run(_scenario())
    assert not ran_while_busy, "the digest must NOT fire while a turn is streaming"
    assert runs == 1, "it must run once the turn finishes"


def test_p0safe_digest_skips_instead_of_hanging_when_turn_never_ends() -> None:
    """Bounded wait: a stuck 'active' flag must not hang the task forever."""
    uc = _RecordingDigest()

    async def _scenario() -> int:
        engine = _engine_with_probe(lambda: True, uc, budget=1.0)
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        if task is not None:
            await asyncio.wait_for(task, timeout=10)   # must COMPLETE, not hang
        return uc.runs

    assert asyncio.run(_scenario()) == 0, "must skip, and must not hang"


def test_p0safe_a_raising_probe_never_blocks_background_work() -> None:
    uc = _RecordingDigest()

    def _boom() -> bool:
        raise RuntimeError("probe exploded")

    async def _scenario() -> int:
        engine = _engine_with_probe(_boom, uc)
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        return uc.runs

    assert asyncio.run(_scenario()) == 1


def test_p0safe_unwired_probe_is_byte_for_byte_prior_behaviour() -> None:
    uc = _RecordingDigest()

    async def _scenario() -> int:
        engine = CompactionCheckpointEngine(
            compressor=None, threshold_ratio=0.8, target_ratio=0.5,
            preserve_tail=4, refresh_digest_uc=uc,
        )
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        return uc.runs

    assert asyncio.run(_scenario()) == 1


def _active_turn_stub(silent_for: float = 0.0, stale_after: float | None = None):
    """Stub exposing just what ``is_main_turn_active`` reads."""
    from qai.chat.application.use_cases import streaming as _s

    class _Stub:
        is_main_turn_active = _s.StreamChatUseCase.is_main_turn_active

        def __init__(self) -> None:
            self._active_main_turns = 1
            self._last_turn_activity_ts = time.monotonic() - silent_for
            self._turn_active_stale_s = (
                _s._TURN_ACTIVE_STALE_SECONDS if stale_after is None else stale_after
            )

    return _Stub()


def test_p0safe_turn_active_mark_self_heals_when_stale() -> None:
    """A leaked counter must degrade to 'concurrent again', never starve."""
    from qai.chat.application.use_cases import streaming as _s

    assert _active_turn_stub(silent_for=0.0).is_main_turn_active() is True
    stale = _active_turn_stub(silent_for=_s._TURN_ACTIVE_STALE_SECONDS + 1)
    assert stale.is_main_turn_active() is False, (
        "a stale mark must release background work, not starve it forever"
    )


def test_p0safe_silent_prefill_does_not_look_idle() -> None:
    """REGRESSION (the error_4 failure): prefill emits NO frames.

    Field timeline on the Glymur box: the main turn launched at 18:00.558 and was
    still prefilling at 20:03.389 (``progress = 0.93, t = 122.46 s``) — zero
    output tokens, so zero frames. With a 120s staleness bound the mark expired
    at 120s, two waiting summaries were released, and all three requests
    collided on the shared 32768-token KV pool (12911 + 3655 + 16991 = 33557)
    and died with "Context size has been exceeded".
    """
    for silent_seconds in (60.0, 120.0, 123.0, 180.0, 300.0):
        stub = _active_turn_stub(silent_for=silent_seconds)
        assert stub.is_main_turn_active() is True, (
            f"a turn silent for {silent_seconds}s is still PREFILLING, not idle"
        )


def test_p0safe_stale_bound_covers_the_content_stall_budget() -> None:
    """The bound must be >= the app's own limit on legitimate silence.

    Both express the same fact. If the staleness bound were the smaller of the
    two, the coordinator would declare a turn dead that the transport is still
    happily waiting on — which is exactly the error_4 regression.
    """
    from qai.chat.application.use_cases import streaming as _s
    from qai.platform.config.settings import ChatSettings

    assert _s._TURN_ACTIVE_STALE_SECONDS >= (
        ChatSettings().llm_content_stall_budget_seconds
    )


def test_p0safe_stale_bound_cannot_be_configured_below_the_default() -> None:
    """A caller passing something small must not re-open the regression."""
    from qai.chat.application.use_cases import streaming as _s

    stub = _active_turn_stub(silent_for=200.0, stale_after=max(
        _s._TURN_ACTIVE_STALE_SECONDS, 10.0))
    assert stub.is_main_turn_active() is True


def test_p0safe_counter_never_goes_negative() -> None:
    from qai.chat.application.use_cases import streaming as _s

    class _Stub:
        _enter_main_turn = _s.StreamChatUseCase._enter_main_turn
        _exit_main_turn = _s.StreamChatUseCase._exit_main_turn
        is_main_turn_active = _s.StreamChatUseCase.is_main_turn_active

        def __init__(self) -> None:
            self._active_main_turns = 0
            self._last_turn_activity_ts = 0.0
            self._turn_active_stale_s = _s._TURN_ACTIVE_STALE_SECONDS

    stub = _Stub()
    stub._exit_main_turn()
    stub._exit_main_turn()
    assert stub._active_main_turns == 0
    stub._enter_main_turn()
    assert stub.is_main_turn_active() is True


def test_p0safe_new_main_turn_cancels_inflight_background_summary() -> None:
    """The reverse collision: a summary already streaming when a turn starts."""
    started = asyncio.Event()
    finished = {"v": False}

    class _SlowDigest:
        async def execute(self, _input: Any) -> None:
            started.set()
            await asyncio.sleep(30)          # a ~10-min digest, in miniature
            finished["v"] = True

    async def _scenario() -> tuple[int, bool, bool]:
        engine = CompactionCheckpointEngine(
            compressor=None, threshold_ratio=0.8, target_ratio=0.5,
            preserve_tail=4, refresh_digest_uc=_SlowDigest(),
        )
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        task = engine.digest_refresh_task("c")
        await asyncio.wait_for(started.wait(), timeout=5)
        n = engine.cancel_background_summaries(reason="main_turn_started")
        await asyncio.sleep(0)
        done = task is not None and task.done()
        return n, done, finished["v"]

    n, done, completed = asyncio.run(_scenario())
    assert n == 1, "the in-flight digest must be cancelled"
    assert done, "cancellation must actually land"
    assert not completed, "it must not have run to completion"


def test_p0safe_cancel_is_a_noop_when_nothing_is_running() -> None:
    engine = CompactionCheckpointEngine(
        compressor=None, threshold_ratio=0.8, target_ratio=0.5, preserve_tail=4,
    )
    assert engine.cancel_background_summaries(reason="x") == 0


def test_p0safe_stop_waits_for_cancellation_to_actually_land() -> None:
    """Cancel-and-forget loses the race; the turn must await the unwind.

    ``cancel_background_summaries`` only REQUESTS cancellation — the summaries
    keep their HTTP connections (and therefore the engine's KV slots) until the
    loop resumes them. If the main request goes out in that gap the shared pool
    overflows exactly as before, so ``stop_background_summaries`` awaits them.
    """
    running = {"n": 0}
    started = asyncio.Event()

    class _SlowDigest:
        async def execute(self, _input: Any) -> None:
            running["n"] += 1
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                running["n"] -= 1

    async def _scenario() -> tuple[int, int]:
        engine = CompactionCheckpointEngine(
            compressor=None, threshold_ratio=0.8, target_ratio=0.5,
            preserve_tail=4, refresh_digest_uc=_SlowDigest(),
        )
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        await asyncio.wait_for(started.wait(), timeout=5)
        assert running["n"] == 1
        n = await asyncio.wait_for(
            engine.stop_background_summaries(reason="main_turn_started"), timeout=10)
        # By the time stop() returns, nothing may still be holding the engine.
        return n, running["n"]

    cancelled, still_running = asyncio.run(_scenario())
    assert cancelled == 1
    assert still_running == 0, (
        "stop_background_summaries must not return while a summary still holds "
        "the engine"
    )


def test_p0safe_stop_is_bounded_when_a_task_ignores_cancellation() -> None:
    """A task that swallows CancelledError must not block the turn forever."""

    class _Stubborn:
        async def execute(self, _input: Any) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(30)          # refuses to die

    async def _scenario() -> float:
        engine = CompactionCheckpointEngine(
            compressor=None, threshold_ratio=0.8, target_ratio=0.5,
            preserve_tail=4, refresh_digest_uc=_Stubborn(),
        )
        engine.kick_digest_refresh(checkpoint_key="c", input=object())
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.wait_for(
            engine.stop_background_summaries(
                reason="x", timeout_seconds=0.5), timeout=10)
        return loop.time() - t0

    elapsed = asyncio.run(_scenario())
    assert elapsed < 5, f"stop must be bounded, took {elapsed:.1f}s"


def test_p0safe_stop_is_a_noop_when_nothing_is_running() -> None:
    async def _scenario() -> int:
        engine = CompactionCheckpointEngine(
            compressor=None, threshold_ratio=0.8, target_ratio=0.5, preserve_tail=4)
        return await engine.stop_background_summaries(reason="x")

    assert asyncio.run(_scenario()) == 0


# ---------------------------------------------------------------------------
# P9 — one tool result may not occupy a large fraction of a small window
# ---------------------------------------------------------------------------
# Field root cause: with a 32768-token window (a hard laptop ceiling) the read
# backstop allowed 80109 chars = 20027 tok = 61% of the WHOLE window in a single
# result. No inter-round trigger at 0.50-0.65 can survive a round that adds 0.61
# of the window, which is why compaction "could not keep up" however it was tuned.

WINDOW = 32_768


def _req(text: str, tool: str = "read", window: int = 0,
         start: int = 1) -> ToolResultTruncationRequest:
    return ToolResultTruncationRequest(
        model_id="Qwen3.8", tool_name=tool, result_text=text,
        slice_start_line=start, context_length=window,
    )


def _big(n_lines: int = 3000) -> str:
    return "\n".join(f"{i}\tline {i} " + "x" * 60 for i in range(1, n_lines))


def test_p9_scaled_cap_is_a_fraction_of_the_real_window() -> None:
    t = AdaptiveToolResultTruncator()
    cap = t._window_cap_chars(WINDOW)
    assert cap is not None
    assert cap // 4 == int(WINDOW * DEFAULT_WINDOW_RESULT_RATIO)
    assert (cap // 4) / WINDOW <= 0.16, "one result must stay ~1/7 of the window"


def test_p9_read_result_is_bounded_when_the_window_is_known() -> None:
    t = AdaptiveToolResultTruncator()
    out = t.truncate(_req(_big(), window=WINDOW))
    assert out.truncated is True
    assert out.final_length // 4 <= int(WINDOW * DEFAULT_WINDOW_RESULT_RATIO) + 1


def test_p9_bounded_read_still_carries_a_forward_offset() -> None:
    """The tail cut replaces read's own footer, so it must supply a usable one."""
    t = AdaptiveToolResultTruncator()
    out = t.truncate(_req(_big(), window=WINDOW))
    assert "offset=" in out.text
    m = re.search(r"offset=(\d+)", out.text)
    assert m is not None and int(m.group(1)) > 1


def test_p9_deep_slice_offset_still_points_forward() -> None:
    t = AdaptiveToolResultTruncator()
    out = t.truncate(_req(_big(), window=WINDOW, start=800))
    m = re.search(r"offset=(\d+)", out.text)
    assert m is not None and int(m.group(1)) > 800, (
        "must not point back into content the model already saw"
    )


def test_p9_no_window_supplied_is_prior_behaviour() -> None:
    """Every existing caller (context_length=0) must be unchanged."""
    t = AdaptiveToolResultTruncator()
    text = _big()
    scaled = t.truncate(_req(text, window=WINDOW))
    legacy = t.truncate(_req(text))
    assert legacy.final_length > scaled.final_length
    assert legacy.final_length == t.ordered_slice_cap


def test_p9_generic_family_budget_is_also_scaled() -> None:
    t = AdaptiveToolResultTruncator()
    unscaled = t._resolve_budget(model_id="claude-sonnet-4")
    scaled = t._resolve_budget(model_id="claude-sonnet-4", context_length=WINDOW)
    assert scaled < unscaled
    assert scaled == int(WINDOW * DEFAULT_WINDOW_RESULT_RATIO * 4)


def test_p9_scaling_never_makes_results_uselessly_small() -> None:
    """A tiny/odd window must not shrink read into a pagination thrash loop."""
    t = AdaptiveToolResultTruncator()
    for bogus in (1000, 0, -1):
        assert t._window_cap_chars(bogus) is None
    assert t._resolve_budget(
        model_id="claude-sonnet-4", context_length=1000
    ) == t._resolve_budget(model_id="claude-sonnet-4")


def test_p9_scaling_only_ever_tightens() -> None:
    t = AdaptiveToolResultTruncator()
    for window in (32_768, 65_536, 131_072, 200_000, 1_000_000):
        for model in ("claude-sonnet-4", "gpt-4o", "unknown-model"):
            assert t._resolve_budget(
                model_id=model, context_length=window
            ) <= t._resolve_budget(model_id=model)


# ---------------------------------------------------------------------------
# P8-fix — the emergency valve must fire while a REPLY can still fit
# ---------------------------------------------------------------------------


def _valve(window: int, max_out: int) -> int:
    from qai.chat.application.use_cases import streaming as _s

    return min(
        int(window * _s._EMERGENCY_COMPRESS_RATIO),
        window - max_out - _s._EMERGENCY_OUTPUT_RESERVE_TOKENS,
    )


def test_p8fix_ratio_alone_fires_past_the_point_of_no_return() -> None:
    """Documents the bug: 0.92 x 32768 is beyond the usable prompt ceiling."""
    from qai.chat.application.use_cases import streaming as _s

    assert int(WINDOW * _s._EMERGENCY_COMPRESS_RATIO) > WINDOW - 4096, (
        "this is why the valve was unreachable in any useful sense at 32K"
    )


def test_p8fix_output_aware_threshold_fires_before_the_doom_line() -> None:
    max_out = 4096
    threshold = _valve(WINDOW, max_out)
    assert threshold < WINDOW - max_out, "must fire while a reply still fits"
    assert WINDOW - threshold >= max_out, "must leave room for the whole reply"


def test_p8fix_valve_always_reserves_the_reply_budget() -> None:
    """The invariant, at every window size: the reply must still fit.

    Note this makes the valve slightly EARLIER than the bare 0.92 ratio even on
    large windows (200K/16384 -> 181568 vs 184000, a 1.2% difference). That is
    intended: reserving the reply budget is correct at any size, and the whole
    point of the valve is to act while a reply can still be produced.
    """
    for window, max_out in (
        (32_768, 4096), (65_536, 8192), (131_072, 16_384), (200_000, 16_384),
    ):
        threshold = _valve(window, max_out)
        assert threshold > 0
        assert window - threshold >= max_out, (
            f"window={window} max_out={max_out}: valve at {threshold} leaves "
            f"{window - threshold} for a {max_out}-token reply"
        )


def test_p8fix_max_output_tokens_resolution_order() -> None:
    resolve = StreamChatUseCase._resolve_max_output_tokens
    assert resolve(None, extra={"max_tokens": 1234}, model_hint="Qwen3.8") == 1234
    for bogus in ({"max_tokens": 0}, {"max_tokens": True}, {}, None):
        assert resolve(None, extra=bogus, model_hint="Qwen3.8") > 0
    assert resolve(None, extra=None, model_hint=None) > 0


# ---------------------------------------------------------------------------
# The whole budget must close on a fixed 32K window
# ---------------------------------------------------------------------------


def test_budget_closes_on_a_fixed_32k_window() -> None:
    """Property: trigger + one worst-case capped round still fits the prompt."""
    max_out = 4096
    trigger = int(WINDOW * resolve_inter_round_threshold_ratio(0.80, WINDOW))
    worst_round = int(WINDOW * DEFAULT_WINDOW_RESULT_RATIO)
    usable_prompt = WINDOW - max_out
    assert trigger + worst_round < usable_prompt, (
        f"trigger {trigger} + one capped round {worst_round} must stay under the "
        f"usable prompt ceiling {usable_prompt}"
    )
    assert _valve(WINDOW, max_out) < usable_prompt
