# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""On-device (``local::``) turns: the service owns the context budget.

Covers the backend half of "own the on-device context budget and skill
access": no client-side compaction / ``/compact`` for local models, no
sub-agent spawning on local turns, the internal ``liveness_pulse`` frame,
the local prompt's host facts + skill preamble, the sub-agent skill block and
the provider-routing terminal-frame ordering. Plain asserts + ``asyncio.run``
so the file needs no async pytest plugin.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters.system_prompt_builder import (
    RichSystemPromptBuilder,
    build_sub_agent_skills_block,
)
from qai.chat.application.use_cases import _stream_guards
from qai.chat.application.use_cases import streaming as streaming_mod
from qai.chat.application.use_cases._compaction_engine import (
    CompactionCheckpointEngine,
)
from qai.chat.application.use_cases.force_compact import (
    ForceCompactChatInput,
    ForceCompactChatUseCase,
)
from qai.chat.application.use_cases.tool_advertise import (
    LOCAL_EXCLUDED_TOOLS,
    compose_advertised_tools,
)
from qai.chat.domain.ids import ConversationId
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.infrastructure.provider_routing_stream import (
    ProviderRoutingLLMStream,
)
from qai.platform.errors import PreconditionFailedError


def _schema(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [s["function"]["name"] for s in schemas]


# ── compaction gate ─────────────────────────────────────────────────────────


def test_maybe_compress_is_off_for_local_hint() -> None:
    engine = object.__new__(CompactionCheckpointEngine)
    engine._compressor = object()  # type: ignore[attr-defined]
    result = asyncio.run(
        engine.maybe_compress(
            checkpoint_key="k",
            assembled_wire=[],
            history_messages_since_anchor=[],
            anchor_index=0,
            anchor_message_id=None,
            completed_rounds=None,
            model_hint="local::qwen",
            context_limit=4096,
        )
    )
    assert result is None


def test_force_compact_refuses_local_model() -> None:
    uc = ForceCompactChatUseCase(conversations=None, compaction_engine=None)  # type: ignore[arg-type]
    with pytest.raises(PreconditionFailedError) as exc:
        asyncio.run(
            uc.execute(
                ForceCompactChatInput(
                    conversation_id=ConversationId("c1"),
                    model_id="local::qwen",
                )
            )
        )
    assert exc.value.code == "chat.compact_local_model_disabled"


# ── tool advertise ──────────────────────────────────────────────────────────


def test_local_turn_never_advertises_agent_but_keeps_skill() -> None:
    advertised = [_schema(n) for n in ("read", "agent", "skill", "exec")]
    assert "agent" in LOCAL_EXCLUDED_TOOLS
    assert "skill" not in LOCAL_EXCLUDED_TOOLS
    local = compose_advertised_tools(
        advertised,
        tool_mode=None,
        is_local=True,
        excluded=frozenset(),
        inject_agent=True,
        agent_schema_factory=lambda: _schema("agent"),
    )
    assert _names(local) == ["read", "skill", "exec"]
    cloud = compose_advertised_tools(
        advertised,
        tool_mode=None,
        is_local=False,
        excluded=frozenset(),
        inject_agent=True,
        agent_schema_factory=lambda: _schema("agent"),
    )
    assert "agent" in _names(cloud)


# ── liveness pulse ──────────────────────────────────────────────────────────


class _NeverSet:
    def is_set(self) -> bool:
        return False


def test_liveness_pulse_is_swallowed_by_abortable_frames() -> None:
    async def src():
        yield StreamFrame.liveness_pulse(
            frame_id="p", sequence=1, status="inference"
        )
        yield StreamFrame.chunk(frame_id="c", sequence=2, text="hi")
        yield StreamFrame.end(frame_id="e", sequence=3)

    async def run() -> list[StreamFrameType]:
        return [
            f.frame_type
            async for f in _stream_guards.abortable_frames(src(), _NeverSet())
        ]

    assert asyncio.run(run()) == [StreamFrameType.CHUNK, StreamFrameType.END]


def test_liveness_pulse_factory_validates_status() -> None:
    frame = StreamFrame.liveness_pulse(frame_id="p", sequence=1)
    assert frame.frame_type is StreamFrameType.LIVENESS_PULSE
    assert frame.payload == {}
    with pytest.raises(TypeError):
        StreamFrame.liveness_pulse(frame_id="p", sequence=1, status=3)  # type: ignore[arg-type]


# ── local prompt ────────────────────────────────────────────────────────────


def _local_prompt(tool_names: list[str]) -> str:
    uc = object.__new__(streaming_mod.StreamChatUseCase)
    uc._system_prompt_builder = None  # type: ignore[attr-defined]
    uc._local_skill_catalog_provider = lambda: (  # type: ignore[attr-defined]
        ("alpha", "does a", "C:/x/alpha/SKILL.md"),
    )
    uc._effective_workspace_root_str = lambda _s: "C:/WoS_AI"  # type: ignore[method-assign,attr-defined]
    request = SimpleNamespace(
        model_hint="local::m",
        conversation_id=None,
        extra={},
        user_message=SimpleNamespace(text="hi"),
    )
    return uc._build_local_system_prompt(
        extra={"tools_schemas": [_schema(n) for n in tool_names]},
        request=request,  # type: ignore[arg-type]
    )


def test_local_prompt_with_exec_and_skill_tools() -> None:
    prompt = _local_prompt(["exec", "skill"])
    assert "## Host Environment" in prompt
    assert "git clone" in prompt
    assert "call the `skill` tool" in prompt
    assert "## Language" in prompt


def test_local_prompt_without_exec_or_skill_tools() -> None:
    prompt = _local_prompt(["read"])
    assert "## Host Environment" not in prompt
    assert "call the `skill` tool" not in prompt
    assert "call `read` directly" in prompt


# ── sub-agent skills block ──────────────────────────────────────────────────


def test_sub_agent_skills_block_renders_and_escapes() -> None:
    assert build_sub_agent_skills_block(()) == ""
    block = build_sub_agent_skills_block([("a", "use <x>", "C:/s/a/SKILL.md")])
    assert "## Available Skills (NOT tools)" in block
    assert "use &lt;x&gt;" in block
    assert "<location>C:/s/a/SKILL.md</location>" in block


def test_sub_agent_concise_prompt_appends_skills_block_only_when_given() -> None:
    plain = RichSystemPromptBuilder.build_sub_agent_concise()
    with_block = RichSystemPromptBuilder.build_sub_agent_concise(
        skills_block="SKILLS-BLOCK"
    )
    assert "SKILLS-BLOCK" not in plain
    assert "SKILLS-BLOCK" in with_block


# ── provider routing: END is held until the inner stream is closed ─────────
# (local only; cloud / query / default pass-through is unchanged)


def _routing(order: list[str], extra: dict[str, Any] | None, inner_frames):
    async def inner():
        try:
            for f in inner_frames:
                order.append("inner_yield:" + f.frame_type.value)
                yield f
        finally:
            order.append("inner_closed")

    class _Routing(ProviderRoutingLLMStream):
        __slots__ = ()

        async def _select_target(self, request):  # type: ignore[override]
            class _T:
                def stream(self_, _r):  # noqa: N805
                    return inner()

            return _T(), SimpleNamespace(extra=extra)

    return object.__new__(_Routing)


def _frames() -> list[StreamFrame]:
    return [
        StreamFrame.chunk(frame_id="c", sequence=1, text="a"),
        StreamFrame.end(frame_id="e", sequence=2),
    ]


async def _drain(routing, order: list[str]) -> list[str]:
    seen: list[str] = []
    async for frame in routing._iter(None):  # type: ignore[arg-type]
        seen.append(frame.frame_type.value)
        order.append("out:" + frame.frame_type.value)
    return seen


def test_routing_stream_local_closes_inner_before_forwarding_end() -> None:
    order: list[str] = []
    routing = _routing(order, {"__is_local_model__": True}, _frames())
    seen = asyncio.run(_drain(routing, order))
    assert seen == ["chunk", "end"]
    assert order == [
        "inner_yield:chunk",
        "out:chunk",
        "inner_yield:end",
        "inner_closed",
        "out:end",
    ]


def test_routing_stream_cloud_is_plain_passthrough() -> None:
    # Cloud (is_local False), query-service / default stream (no flag) must
    # behave exactly as before: END is forwarded while the inner generator is
    # still open, and the inner closes only after the consumer finished.
    for extra in ({"__is_local_model__": False}, {}, None):
        order: list[str] = []
        routing = _routing(order, extra, _frames())
        seen = asyncio.run(_drain(routing, order))
        assert seen == ["chunk", "end"]
        assert order[:4] == [
            "inner_yield:chunk",
            "out:chunk",
            "inner_yield:end",
            "out:end",
        ]
        assert order[-1] == "inner_closed"
