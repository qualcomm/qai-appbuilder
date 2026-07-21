# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-powered experience extractor (PR-091, audit H-6).

Migrates :func:`backend.experience_extractor._try_extract_experience`
(legacy ``backend/experience_extractor.py:107-301``) into a context-
isolated adapter.  Used by :class:`StreamChatUseCase` after a
successful agentic turn (``round_index > 2`` AND at least one
``tool_call`` ran) to mine reusable knowledge snippets from the
conversation and drop them into the
:class:`~qai.chat.application.ports.ExperienceRepositoryPort`.

The extractor is invoked **fire-and-forget** via
``asyncio.create_task(...)`` so it never blocks the streaming user
response.  Every failure path is swallowed and logged at WARNING.

Design notes:

* Only an :class:`LLMStreamPort` is required for the extraction call
  itself; the use case threads in the same port already wired into
  the chat container, so no new endpoint configuration is needed.
* The repository is optional — when omitted, the extracted
  :class:`Experience` is just returned (useful for tests).  When
  wired, the extractor saves the experience as a side effect and
  still returns it.
* The trigger conditions live in :meth:`should_extract` so callers
  can short-circuit without instantiating the extractor.

Reference: legacy ``backend/experience_extractor.py:107-301``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from qai.chat.application.ports import (
    ExperienceRepositoryPort,
    LLMStreamPort,
    LLMStreamRequest,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.experience import Experience
from qai.chat.domain.ids import ConversationId, ExperienceId, TabId
from qai.chat.domain.message import Message
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger


__all__ = [
    "LLMExperienceExtractor",
    "ExperienceExtractionPrompt",
]


_log = get_logger(__name__)


_EXTRACT_PROMPT_TEMPLATE = (
    "你是一个任务经验分析助手。请分析以下对话，"
    "提取可复用的经验，以 JSON 格式输出。\n\n"
    "## 待分析的对话摘要：\n{conversation_summary}\n\n"
    "请输出以下 JSON（所有字段基于对话内容动态生成；"
    "若对话不值得沉淀，把 worth_saving 设为 false 即可）：\n"
    "{{\n"
    '  "worth_saving": true,\n'
    '  "category": "...",\n'
    '  "topic": "...",\n'
    '  "summary": "...",\n'
    '  "key_steps": ["...", "..."],\n'
    '  "reusable_insights": "...",\n'
    '  "tags": ["...", "..."]\n'
    "}}\n"
    "只输出 JSON，不加任何前缀或解释。"
)


class ExperienceExtractionPrompt:
    """Static helper that renders the extraction prompt text."""

    @staticmethod
    def render(conversation_summary: str) -> str:
        return _EXTRACT_PROMPT_TEMPLATE.format(
            conversation_summary=conversation_summary,
        )


class LLMExperienceExtractor:
    """Extract a reusable :class:`Experience` from an agentic conversation.

    Invocation::

        extractor = LLMExperienceExtractor(
            llm=llm,
            repository=experiences,
            ids=ids,
        )
        if extractor.should_extract(round_index, tool_call_count):
            asyncio.create_task(extractor.extract(messages))

    The :meth:`extract` method:

    1. Builds a textual conversation summary (truncating tool outputs
       to 300 chars and other roles to 1000 chars to bound the
       prompt).
    2. Calls :class:`LLMStreamPort.stream` with a JSON-shaped prompt.
    3. Drains the resulting stream and concatenates every CHUNK
       frame into a single response string.
    4. Parses the response as JSON (tolerant of ``\u200b```json fences).
    5. When ``worth_saving`` is true, materialises an
       :class:`Experience`, persists it via the repository (when
       wired), and returns it.

    Returns ``None`` on every failure (including ``worth_saving=false``
    or malformed JSON) — the extractor is intentionally best-effort.
    """

    __slots__ = (
        "_ids",
        "_llm",
        "_model_hint",
        "_repository",
    )

    def __init__(
        self,
        *,
        llm: LLMStreamPort,
        ids: IdGenerator,
        repository: ExperienceRepositoryPort | None = None,
        model_hint: str | None = None,
    ) -> None:
        self._llm = llm
        self._ids = ids
        self._repository = repository
        self._model_hint = model_hint

    # ------------------------------------------------------------------
    # Trigger helpers
    # ------------------------------------------------------------------
    @staticmethod
    def should_extract(
        *,
        round_index: int,
        tool_call_count: int,
    ) -> bool:
        """Return True iff the extractor should run for this turn.

        Mirrors the legacy gating (``round_index > 2`` AND at least one
        tool call ran).  The use case wires this in the followup loop
        immediately before scheduling :meth:`extract` as a background
        task.
        """
        return round_index > 2 and tool_call_count >= 1

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def extract(
        self,
        conversation: list[Message],
        *,
        # Optional context the LLMStreamRequest needs but the extractor
        # does not actually use semantically (the LLM call is one-shot).
        # Callers may pass ``None`` and we'll synthesise placeholders.
        conversation_id: ConversationId | None = None,
        tab_id: TabId | None = None,
    ) -> Experience | None:
        """Run extraction; return the saved :class:`Experience` or ``None``."""
        try:
            summary = self._build_summary(conversation)
            if not summary:
                return None

            prompt_text = ExperienceExtractionPrompt.render(summary)
            response_text = await self._call_llm(
                prompt_text=prompt_text,
                conversation_id=conversation_id,
                tab_id=tab_id,
            )
            if not response_text or not response_text.strip():
                return None

            parsed = _parse_json_object(response_text)
            if parsed is None:
                return None
            if not parsed.get("worth_saving"):
                _log.info("chat.experience_not_worth_saving")
                return None

            experience = self._build_experience(parsed)
            if experience is None:
                return None

            if self._repository is not None:
                try:
                    await self._repository.save(experience)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _log.warning(
                        "chat.experience_save_failed",
                        error=str(exc),
                    )
                    return None

            _log.info(
                "chat.experience_extracted",
                category=experience.category,
                content_preview=experience.content[:80],
            )
            return experience
        except Exception as exc:  # noqa: BLE001 — fire-and-forget guard
            _log.warning(
                "chat.experience_extraction_failed",
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _build_summary(messages: list[Message]) -> str:
        """Render a length-bounded textual summary of the conversation."""
        parts: list[str] = []
        for msg in messages:
            role = msg.role.value
            content = ""
            if hasattr(msg.content, "text") and msg.content.text:
                content = msg.content.text
            if not content:
                continue
            limit = 300 if role == "tool" else 1000
            content = content[:limit]
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)

    async def _call_llm(
        self,
        *,
        prompt_text: str,
        conversation_id: ConversationId | None,
        tab_id: TabId | None,
    ) -> str:
        """Stream a one-shot LLM call and return the concatenated text."""
        # Synthesise placeholder ids if the caller did not pass them in.
        # The LLM adapter only uses these for telemetry; semantics are
        # unaffected.
        conv_id = conversation_id or ConversationId("exp-extract")
        the_tab = tab_id or TabId("exp-extract")
        request = LLMStreamRequest(
            conversation_id=conv_id,
            tab_id=the_tab,
            prompt=MessageContent(text=prompt_text),
            history=(),
            model_hint=self._model_hint,
            extra={"system_prompt": "You are a JSON-only assistant."},
        )

        chunks: list[str] = []
        async for frame in self._llm.stream(request):
            if frame.frame_type is StreamFrameType.CHUNK:
                txt = frame.payload.get("text", "")
                if isinstance(txt, str):
                    chunks.append(txt)
            elif frame.frame_type is StreamFrameType.END:
                break
            elif frame.frame_type is StreamFrameType.ERROR:
                _log.warning(
                    "chat.experience_extraction_llm_error",
                    code=frame.payload.get("code"),
                )
                # Don't return early — the LLM may still emit chunks
                # before the END frame, but we won't get useful output
                # after an error.  Drain remaining frames defensively.
        return "".join(chunks)

    def _build_experience(self, parsed: dict[str, Any]) -> Experience | None:
        """Materialise an :class:`Experience` from the parsed JSON."""
        category_raw = parsed.get("category") or "未分类"
        category = str(category_raw).strip()[:64]
        if not category:
            category = "未分类"

        # Combine summary + key_steps + insights + tags into a single
        # ``content`` string (the chat Experience aggregate has one
        # ``content`` field, not a structured payload).
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            return None

        key_steps_raw = parsed.get("key_steps") or []
        if isinstance(key_steps_raw, list):
            key_steps = [str(s) for s in key_steps_raw if str(s).strip()]
        else:
            key_steps = []
        insights = str(parsed.get("reusable_insights") or "").strip()
        tags_raw = parsed.get("tags") or []
        if isinstance(tags_raw, list):
            tags = [str(t) for t in tags_raw if str(t).strip()]
        else:
            tags = []

        content_lines: list[str] = [summary]
        if key_steps:
            content_lines.append("")
            content_lines.append("Key steps:")
            content_lines.extend(f"- {s}" for s in key_steps)
        if insights:
            content_lines.append("")
            content_lines.append(f"Insights: {insights}")
        content = "\n".join(content_lines)[:8192]

        metadata: dict[str, Any] = {
            "topic": str(parsed.get("topic") or "").strip()[:128],
            "tags": tags,
            "extracted_by": "LLMExperienceExtractor",
        }

        return Experience(
            id=ExperienceId(self._ids.new_id()),
            category=category,
            content=content,
            created_at=datetime.now(timezone.utc),
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_FENCED_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object found in *text*; tolerant of fences.

    Strips ```json ... ``` fences, then finds the slice between the
    first ``{`` and the last ``}``.  Returns ``None`` on any failure
    (caller treats that as "experience not extractable").
    """
    if not text:
        return None
    fenced = _FENCED_RE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj
