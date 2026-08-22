# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure / weakly-coupled helpers extracted from ``streaming.py``.

ARCH-1 / A-3 cohesion split (zero behaviour change): these functions
used to be ``StreamChatUseCase`` methods that depended on no (or only a
couple of explicitly-passable) ``self`` fields.  They are byte-for-byte
identical -- only relocated to a sibling module + turned into module-level
functions whose former ``self.<field>`` reads are now explicit parameters
so the orchestrating ``StreamChatUseCase`` file shrinks below the
cohesion advisory ceiling (AGENTS.md §3.6).  ``StreamChatUseCase`` keeps
thin method wrappers that forward the relevant ``self`` fields, so the
public method surface (used by tests + the route layer) is unchanged.

They depend only on their arguments + chat domain / application-port
types, never on use-case state beyond the explicitly-passed values.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from qai.chat.application.ports import (
    LLMStreamRequest,  # noqa: F401  (re-exported type ref for callers' typing)
    RetryCategory,
    SystemPromptBuilderPort,
    SystemPromptRequest,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.ids import ConversationId, MessageId
from qai.chat.domain.message import Message
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

_log = get_logger(__name__)

# Per-conversation turn-count warning thresholds (V1 parity).  Mirrors
# ``backend/main.py:1326-1344`` (``_compute_webui_turn_warning``): the
# threshold sequence is ``30, 40, 50, 60, ...``.
TURN_WARNING_START: int = 30
TURN_WARNING_STEP: int = 10


# Internal sentinel a tool-call round's ASSISTANT message persists in its
# ``content`` when the model went straight to tools with no lead-in text
# (see ``build_tool_call_message`` -> ``MessageContent(text="[tool_calls]")``).
# It must NEVER reach the model as real text on a later turn. Single source of
# truth = ``_agentic_kernel.TOOL_CALLS_CONTENT_SENTINEL`` so the main loop and
# the sub-agent loop (``agent_tool``) use the IDENTICAL value.
from qai.chat.application.use_cases._agentic_kernel import (
    TOOL_CALLS_CONTENT_SENTINEL as _TOOL_CALLS_CONTENT_SENTINEL,
    SUBAGENT_SUMMARY_CONTENT_SENTINEL as _SUBAGENT_SUMMARY_CONTENT_SENTINEL,
)

# Wire envelope prefix used when folding a ``role=system_notice`` message
# down onto the four core wire roles.  Provider APIs (OpenAI / Anthropic /
# Bedrock / …) only accept ``system|user|assistant|tool`` — the internal
# transcript keeps the true origin (SYSTEM_NOTICE) while the wire builder
# renders each such row as a user message whose text opens with this
# marker.  The marker MUST be stable and machine-recognisable so a later
# reload / follow-up turn can still identify these as system-authored
# even after they've been round-tripped through a provider that returns
# no role metadata.  Rationale for user-role-plus-prefix over a real
# ``system`` row: ``docs/70-multi-agent/complete-solution-plan-2026-08-08.md``
# §12.3 constraints 1-2.
#
# ``role=user`` IS THE DECISION — do NOT "upgrade" it to ``developer``.
# Measured 2026-08-09 against the gateway this product actually targets,
# same 5-message probe per role:
#
#     role         Anthropic   Azure/gpt   VertexAI   local
#     user            200         200         200      200   <- only all-green
#     system          200         200         200      500
#     developer       400         200         200      500
#
# Anthropic rejects it outright: ``ValidationException: Value 'developer' at
# 'messages.N.member.role' failed to satisfy constraint`` — and Anthropic is
# this product's primary family, so ``developer`` would break the main path.
# The reference design's ``developer`` is an INTERNAL role that its
# per-provider dialect layer rewrites to ``user`` before it reaches
# Anthropic; it never puts ``developer`` on an Anthropic request either.  We
# have no dialect layer (roles pass straight through ``llm_stream``), so the
# internal abstraction lives in ``MessageRole.SYSTEM_NOTICE`` and the wire
# form is ``user`` + this prefix.  Attribution does not depend on the role:
# a batch body carrying ``jobId``/label was answered correctly by 4/4
# providers in the same measurement.
SYSTEM_NOTICE_WIRE_PREFIX: str = "[System notification] "


def rebuild_history_wire_messages(
    history: tuple[Any, ...],
) -> list[dict[str, Any]]:
    """Rebuild a ``Message`` history into the OpenAI wire shape, tool calls intact.

    V1 parity (``backend/chat_handler.py:_build_system_messages`` ->
    ``result.extend(messages)`` + ``_sanitize_tool_messages``): the cloud
    request must replay a prior tool-using round as a proper
    ``assistant{content, tool_calls}`` entry followed by one ``role:tool``
    message per call (carrying that call's output), NOT as a flat
    ``{role, content}`` pair.

    The previous flat conversion (``{"role": msg.role.value, "content":
    msg.content.text}``) dropped every historical ``tool_calls`` linkage AND
    leaked the internal ``"[tool_calls]"`` content sentinel to the model on
    the *next* turn of a multi-turn conversation, so the model both saw
    garbage text and lost all memory of what it had done with tools in prior
    turns.  This rebuilds the wire shape so multi-turn agentic context
    survives (mirrors the per-round reconstruction already used by
    ``_save_prompt_snapshot``).

    Each element is one of:

    * a ``Message`` carrying ``tool_calls`` -> an ``assistant`` entry whose
      OpenAI ``tool_calls`` array contains ONLY the calls that recorded an
      ``output`` (sentinel content blanked) + exactly one ``role:tool`` reply
      per such call, so the array is always 1:1 with the replies;
    * any other ``Message`` -> a flat ``{"role", "content"}`` entry;
    * a plain dict (compressed history) -> passed through unchanged.

    A call whose ``output`` was lost (e.g. a sub-``agent`` round whose
    TOOL_RESULT frame never reached the persisted card) is dropped from the
    ``tool_calls`` array entirely rather than emitted unpaired. This is the
    root-cause fix for the ``empty_response`` bug where an
    ``assistant.tool_calls`` with N calls but only M<N paired ``role:tool``
    replies slipped past ``sanitize_tool_messages`` (whose Pass-2 only checks
    "≥1 reply exists", not per-id) and made the upstream proxy mask the
    protocol violation as an empty HTTP 200 on every follow-up turn.
    """
    out: list[dict[str, Any]] = []
    # CROSS-TURN-HISTORY-DIAG (diagnostic only, no behaviour change): count how
    # many assistant-with-tool_calls rounds we rebuild, and how many individual
    # tool_call entries are DROPPED because their persisted ``output`` is None
    # (the line 143-145 ``continue`` below). A non-zero ``dropped_*`` means the
    # model will NOT see that prior tool call on this turn — the "main agent
    # forgets it派过子 Agent / 改过文件" symptom. ``agent`` is flagged separately
    # since its synthetic TOOL_RESULT frame historically lacked a tool_call_id
    # (pairs into ``output=None`` → dropped here). Read after a repro:
    #   * ``dropped_tools`` containing "agent" with ``dropped_agent>0`` ⇒ the
    #     agent-tool tool_call_id pairing root cause is firing.
    #   * ``dropped_tools`` containing "edit"/other ⇒ a普通 tool also lost its
    #     output pairing upstream (different root cause — investigate persistence).
    _diag_rounds_with_tool_calls = 0
    _diag_dropped_tools: list[str] = []
    _diag_dropped_agent = 0
    _diag_agent_calls_kept = 0
    for msg in history:
        # Plain-dict history (e.g. compressed) — pass through unchanged.
        if isinstance(msg, dict):
            out.append(dict(msg))
            continue

        role = getattr(getattr(msg, "role", None), "value", None) or "user"
        text = getattr(getattr(msg, "content", None), "text", "") or ""
        tool_calls = tuple(getattr(msg, "tool_calls", None) or ())

        # SYSTEM_NOTICE fold: background-task completion notices persist
        # with ``role=system_notice`` (a first-class transcript type, so
        # history reload / user-turn analytics stay honest).  Provider
        # APIs only accept ``system|user|assistant|tool``, and cloud
        # ``role:system`` is a single-header slot (Anthropic + Bedrock
        # enforce this), so we fold system-notice down onto ``role:user``
        # with a stable ``[System notification]`` envelope prefix.  The
        # envelope keeps the origin machine-recognisable to any
        # downstream reader (e.g. a compacted transcript summariser) and
        # tells the model unambiguously that this text came from the
        # system, not the user.  ``SYSTEM_NOTICE`` never carries tool
        # calls, so we short-circuit the tool-call fork below.
        if role == MessageRole.SYSTEM_NOTICE.value:
            wire_text = f"{SYSTEM_NOTICE_WIRE_PREFIX}{text}"
            out.append({"role": "user", "content": wire_text})
            continue

        if not tool_calls:
            # SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG (2026-07-02): a persisted
            # ``subagent_summary`` message carries the ``[subagent_summary]``
            # sentinel (never real text) + UI-only fold blocks in
            # ``meta.subAgentBlocks``. The sub-agent run's actual output
            # already reached the main agent via the parent ``agent``
            # tool_call's synthetic ``TOOL_RESULT`` on the tool-call round's
            # message — re-sending it as an assistant utterance would both
            # leak the sentinel and duplicate context. SKIP entirely.
            if text == _SUBAGENT_SUMMARY_CONTENT_SENTINEL:
                continue
            out.append({"role": role, "content": text})
            continue

        # Tool-call round: blank the internal sentinel so it never reaches the
        # model as text; keep any real lead-in text.
        content = "" if text == _TOOL_CALLS_CONTENT_SENTINEL else text
        _diag_rounds_with_tool_calls += 1
        wire_calls: list[dict[str, Any]] = []
        tool_replies: list[dict[str, Any]] = []
        for i, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            call_id = (
                tc.get("id")
                or tc.get("tool_call_id")
                or f"call_{i}"
            )
            args = tc.get("args")
            try:
                args_str = json.dumps(
                    args if isinstance(args, dict) else {},
                    ensure_ascii=False,
                )
            except (TypeError, ValueError):
                args_str = "{}"
            # Only emit a wire ``tool_calls`` entry for a call that actually
            # recorded an ``output`` (i.e. has a paired ``role:tool`` reply).
            # OpenAI / Anthropic require EVERY id in ``assistant.tool_calls``
            # to have a matching ``role:tool`` message — emitting an entry for
            # a call whose result was lost (e.g. a sub-``agent`` round whose
            # TOOL_RESULT frame never reached the card, leaving ``output``
            # absent) produces an assistant with N tool_calls but only M<N
            # tool replies. That partial-orphan passes ``sanitize_tool_messages``
            # (its Pass-2 only checks "≥1 reply exists", not per-id), so the
            # malformed sequence reaches the upstream proxy, which masks the
            # protocol error as an empty HTTP 200 → ``empty_response`` on every
            # follow-up turn of this conversation (AGENTS.md 🔴 State-Truth-First
            # / 🟡🟡 "发现缺陷必须修"). Dropping the unpaired call keeps the
            # array 1:1 with the replies; the call's intent still survives in
            # the assistant lead-in ``content``.
            output = tc.get("output")
            if output is None:
                # CROSS-TURN-HISTORY-DIAG: this prior tool call is being dropped
                # from the rebuilt wire because it has no persisted output, so
                # the model will not see it on this turn (the "forgets what it
                # did" symptom). Record which tool it was.
                _dropped_name = (
                    (tc.get("tool") or "") if isinstance(tc, dict) else ""
                )
                _diag_dropped_tools.append(str(_dropped_name))
                if _dropped_name == "agent":
                    _diag_dropped_agent += 1
                continue
            _wire_call: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tc.get("tool", "") or "",
                    "arguments": args_str,
                },
            }
            # VERIFY (Problem 2 — amnesia fix): count agent tool_calls that
            # SURVIVE the rebuild (output paired → not dropped above). After the
            # tool_call_id fix this should be >0 whenever the prior turn
            # dispatched a sub-agent — the positive confirmation that the main
            # agent now SEES it did so on the next turn (vs the silent absence
            # of ``tool_calls_dropped`` which only proves it was not dropped).
            if (tc.get("tool") or "") == "agent":
                _diag_agent_calls_kept += 1
            # Full-unification (Step 5) / PR-090 C-1 parity: carry the Vertex AI
            # ``thought_signature`` from the structured card back onto the
            # rebuilt wire ``tool_calls`` entry so a cross-turn replay (main
            # agent) OR a sub-agent resume/take-over rebuild echoes it back
            # losslessly (AGENTS.md 拍板 "存签名"). Absent for non-Vertex calls →
            # the entry is byte-for-byte unchanged (no regression).
            _sig = tc.get("thought_signature")
            if _sig:
                _wire_call["thought_signature"] = _sig
            wire_calls.append(_wire_call)
            tool_replies.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": output
                    if isinstance(output, str)
                    else json.dumps(output, ensure_ascii=False),
                }
            )
        if wire_calls:
            out.append(
                {
                    "role": role,
                    "content": content,
                    "tool_calls": wire_calls,
                }
            )
            out.extend(tool_replies)
        else:
            # No emittable call (all entries were non-dicts, or none recorded
            # an ``output`` so all were skipped above) → flat entry, still
            # blanking the sentinel. Never leaves an ``assistant.tool_calls``
            # without a 1:1 set of ``role:tool`` replies.
            out.append({"role": role, "content": content})
    # CROSS-TURN-HISTORY-DIAG: emit ONE summary per rebuild. ``dropped_total>0``
    # is the smoking gun for "main agent forgot a prior tool call". When the
    # dropped list contains "agent" (``dropped_agent>0``) the root cause is the
    # agent-tool synthetic TOOL_RESULT frame lacking a ``tool_call_id`` (so its
    # output never paired in ``build_tool_call_message`` → ``output=None`` here).
    # A non-agent name in the list points at a different upstream pairing loss.
    if _diag_dropped_tools:
        _log.warning(
            "chat.rebuild_history.tool_calls_dropped",
            rounds_with_tool_calls=_diag_rounds_with_tool_calls,
            dropped_total=len(_diag_dropped_tools),
            dropped_agent=_diag_dropped_agent,
            dropped_tools=_diag_dropped_tools,
            history_len=len(history),
        )
    # VERIFY (Problem 2 — amnesia fix, positive confirmation): when a prior
    # turn dispatched a sub-agent, this records that its agent tool_call was
    # carried into the rebuilt wire (the main agent SEES it dispatched one).
    # ``agent_calls_kept>0`` with NO matching ``tool_calls_dropped`` for "agent"
    # = the fix is working; ``agent_calls_kept=0`` while a sub-agent ran last
    # turn = still being dropped.
    if _diag_agent_calls_kept:
        _log.info(
            "chat.rebuild_history.agent_calls_kept",
            agent_calls_kept=_diag_agent_calls_kept,
            history_len=len(history),
        )
    return out


def repair_orphan_tool_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repair (not drop) orphan ``role=tool`` rows so the wire stays valid.

    Root-cause fix for the sub-agent *take-over* ``empty_response`` bug
    (AGENTS.md 🔴 State-Truth-First / 🟡🟡 "发现缺陷必须修"): a take-over
    seeds the LLM request with a sub-agent's persisted ``wire_messages``.
    If that history contains a ``role=tool`` message whose nearest
    preceding non-tool message is an ``assistant`` WITHOUT a ``tool_calls``
    array (a *bad-predecessor orphan*) — or a ``role=tool`` with NO
    preceding assistant at all (a *no-predecessor orphan*) — the
    infrastructure pre-send sanitiser
    (:func:`qai.chat.infrastructure.message_sanitizer.sanitize_tool_messages`)
    *deletes* it. The deletion then strands the model with a broken
    message sequence and a proxy can mask the resulting upstream error as
    an empty HTTP 200, which trips the ``empty_response`` health check.

    This pre-pass keeps the conversational INFORMATION instead of dropping
    it: an orphan ``role=tool`` message is folded into a plain-text
    ``[Tool Result] ...`` block appended to the nearest preceding
    ``assistant`` message's ``content`` (or, if there is none, promoted to
    a standalone ``assistant`` text message). The offending ``tool_call_id``
    pairing is removed so the row is no longer a ``role=tool`` message at
    all — the downstream sanitiser then sees a structurally valid sequence
    and deletes nothing, so the upstream request never breaks.

    Properly-paired tool rows (preceded by an ``assistant`` that carries a
    matching ``tool_calls`` array) pass through UNCHANGED, so a normal
    take-over of a clean sub-agent history is byte-for-byte unaffected.

    Pure: the input list is not mutated; a new list is returned.
    """
    if not messages:
        return messages

    n = len(messages)
    # Classify each role:tool row: orphan (needs repair) vs. paired (keep).
    #
    # An orphan is a role:tool whose ``tool_call_id`` is NOT one of the
    # ``tool_calls[].id`` opened by the assistant turn it belongs to. We must
    # check the ID PAIRING, not merely the positional predecessor: a
    # take-over's persisted sub-agent wire can carry a role:tool whose nearest
    # preceding assistant DOES have a ``tool_calls`` array, yet that array does
    # NOT contain this row's id (e.g. an assistant issued ONE toolUse but the
    # wire then has TWO toolResults). Bedrock/Anthropic rejects that with
    # "The number of toolResult blocks ... exceeds the number of toolUse blocks
    # of previous turn" (HTTP 400). The earlier positional-only check passed
    # such a row as "paired" → the 400 (the reported sub-agent take-over bug).
    #
    # Rule: the OPEN id set is the tool_calls ids of the most recent
    # ``assistant{tool_calls}``. Each following role:tool must consume one of
    # those open ids (and only once); a role:tool whose id is absent / already
    # consumed is an orphan → folded to text. A non-tool message resets the
    # open set (the turn ended). This mirrors what the upstream enforces.
    is_orphan_tool = [False] * n
    _open_ids: set[str] = set()
    for i in range(n):
        role = messages[i].get("role")
        if role == "assistant":
            tcs = messages[i].get("tool_calls")
            if isinstance(tcs, list) and tcs:
                _open_ids = {
                    str(tc.get("id"))
                    for tc in tcs
                    if isinstance(tc, dict) and tc.get("id")
                }
            else:
                _open_ids = set()
        elif role == "tool":
            tcid = messages[i].get("tool_call_id")
            tcid = str(tcid) if tcid is not None else ""
            if tcid and tcid in _open_ids:
                # Valid pairing: consume the id so a duplicate toolResult for
                # the SAME id (the exact "more toolResults than toolUses" case)
                # is flagged as an orphan.
                _open_ids.discard(tcid)
            else:
                is_orphan_tool[i] = True
        else:
            # Any other role (user/system) ends the prior assistant turn.
            _open_ids = set()

    if not any(is_orphan_tool):
        # Fast path: nothing to repair → return a shallow copy (the callers
        # treat the result as freshly owned, matching the slow path).
        return [dict(m) for m in messages]

    def _result_text(msg: dict[str, Any]) -> str:
        name = msg.get("name")
        content = msg.get("content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError):
                content = str(content)
        label = f"[Tool Result: {name}]" if name else "[Tool Result]"
        return f"{label} {content}".rstrip()

    out: list[dict[str, Any]] = []
    for i in range(n):
        msg = messages[i]
        if is_orphan_tool[i]:
            block = _result_text(msg)
            # Fold the orphan's text into the LAST already-emitted message
            # (preferring its ``content``) instead of emitting a new role:tool
            # row. CRITICAL: we must NOT create a standalone *trailing*
            # ``assistant`` turn — an Anthropic-family upstream rejects a wire
            # that ends with an assistant message ("does not support assistant
            # message prefill; the conversation must end with a user message",
            # HTTP 400). The earlier "promote to standalone assistant" branch
            # did exactly that for an orphan whose predecessor carried
            # ``tool_calls`` (e.g. a DUPLICATE toolResult for an already-consumed
            # id — the reported sub-agent take-over 400). So:
            #   * if the previous emitted message is an assistant (with OR
            #     without tool_calls) → append the block to ITS content (a
            #     tool_calls assistant keeps its array; we only extend text);
            #   * if it is a role:tool → append the block to that tool reply's
            #     content (keeps it a tool row; no new turn, no role flip);
            #   * only when there is NO previous message at all do we emit a
            #     standalone assistant (cannot end the wire — it is the first).
            # Net: an orphan never adds a trailing assistant turn, so the wire's
            # final role is preserved (the valid tool/user tail stays intact).
            if out:
                prev = out[-1]
                prev_content = prev.get("content", "")
                if not isinstance(prev_content, str):
                    # Non-string content (e.g. vision blocks) — don't corrupt
                    # it; skip folding text into it (the orphan's info is lost
                    # but the wire stays valid; this is a rare defensive edge).
                    prev_content = None
                if prev_content is not None:
                    merged = (
                        f"{prev_content}\n\n{block}".strip()
                        if prev_content
                        else block
                    )
                    out[-1] = {**prev, "content": merged}
                # else: leave prev untouched (defensive; orphan dropped).
            else:
                # First message is an orphan tool → standalone assistant (it
                # cannot be a trailing turn since more messages follow / it is
                # the head). Keeps the information.
                out.append({"role": "assistant", "content": block})
            continue
        out.append(dict(msg))
    return out


def classify_error_frame(frame: StreamFrame) -> RetryCategory | None:
    """Map an ERROR frame's ``code`` to a :class:`RetryCategory`.

    The use case relies on the adapter to embed a stable string
    code in the ERROR frame's payload (``payload["code"]``).  The
    code → category mapping is table-driven (see the module-level
    sets below) and is kept consistent with the wire-facing
    ``retry_disposition`` in
    :mod:`qai.chat.domain.error_disposition` — both derive from the
    same error codes so the coarse frontend hint and the retry
    category never drift apart.

    * ``"prompt_too_long"`` — :data:`RetryCategory.PROMPT_TOO_LONG`
      (single retry + compress; unchanged).
    * ``"throttling"``      — :data:`RetryCategory.THROTTLING`
      (bounded, honours ``Retry-After``; unchanged).
    * ``chat.llm.connect_error`` / ``chat.llm.timeout`` /
      ``chat.llm.read_error`` / ``chat.llm.dns_error`` /
      ``chat.llm.connection_refused`` / ``chat.llm.host_unreachable`` —
      :data:`RetryCategory.NETWORK` (escalating backoff 3-5-10-30 s,
      wall-clock-budget-capped at ~10 min). DNS / connection_refused /
      host_unreachable were moved here from ``BOUNDED_FAST`` on 2026-07-30:
      in a desktop app these are almost always transient WiFi/VPN blips
      that self-heal within seconds to a few minutes, so the old 3-attempt
      fast schedule (14 s total) failed the turn while the network was
      still recovering. Kept in sync with
      :mod:`qai.chat.domain.error_disposition`.
    * ``chat.llm.server_error`` (HTTP 5xx) —
      :data:`RetryCategory.BOUNDED_SERVER` (a few jittered attempts
      then terminal).

    Anything else (or non-ERROR frames) returns ``None``, signalling
    "no retry action required; treat as normal / terminal".

    Note: ``chat.llm.network_error`` is deliberately NOT retried — it is
    the ``retryable=False`` catch-all httpx fallback; it was previously
    (mis)listed in the network set and retried forever despite its
    ``retryable=False`` hint (a double bug). It is now terminal.
    Likewise ``empty_response`` is NOT network-retryable — an HTTP 200
    with no content is a successful connection that produced an empty
    completion.
    """
    if frame.frame_type is not StreamFrameType.ERROR:
        return None
    code = frame.payload.get("code")
    if code == "prompt_too_long":
        return RetryCategory.PROMPT_TOO_LONG
    if code == "throttling":
        return RetryCategory.THROTTLING
    if code in _NETWORK_ERROR_CODES:
        return RetryCategory.NETWORK
    if code in _BOUNDED_FAST_ERROR_CODES:
        return RetryCategory.BOUNDED_FAST
    if code in _BOUNDED_SERVER_ERROR_CODES:
        return RetryCategory.BOUNDED_SERVER
    return None


#: Adapter ERROR ``code`` values that denote a transient network failure that
#: MAY take a while to self-heal (``llm_stream.py`` httpx exception branches).
#: These drive the escalating-backoff network auto-retry — bounded by a
#: wall-clock budget in the policy (~10 min), no longer infinite.
#:
#: 2026-07-30 update: ``dns_error`` / ``connection_refused`` /
#: ``host_unreachable`` moved here from :data:`_BOUNDED_FAST_ERROR_CODES`.
#: In a desktop app talking to a cloud LLM these are almost always caused by
#: transient WiFi / VPN blips that self-heal within seconds to a few minutes,
#: not by permanently mistyped hostnames. The old ``bounded_fast`` schedule
#: (1 s → 3 s → 10 s then give up after 14 s total) was too short — the
#: assistant would fail the turn while the user's WiFi was still coming
#: back up. Kept in sync with
#: :data:`~qai.chat.domain.error_disposition._CODE_TO_DISPOSITION`.
#:
#: NOTE: ``chat.llm.network_error`` was REMOVED from this set — it is the
#: ``retryable=False`` httpx catch-all and must terminate (previously it was
#: retried infinitely despite ``retryable=False``).
_NETWORK_ERROR_CODES: frozenset[str] = frozenset(
    {
        "chat.llm.connect_error",
        "chat.llm.timeout",
        "chat.llm.read_error",
        "chat.llm.dns_error",
        "chat.llm.connection_refused",
        "chat.llm.host_unreachable",
    }
)

#: Connectivity faults that recover fast or are effectively permanent — kept
#: for future use / test-only overrides; currently empty because every
#: connectivity-fault code above is treated as ``network_wait`` instead.
_BOUNDED_FAST_ERROR_CODES: frozenset[str] = frozenset()

#: Upstream 5xx → a few jittered attempts then terminal.
_BOUNDED_SERVER_ERROR_CODES: frozenset[str] = frozenset(
    {
        "chat.llm.server_error",
    }
)


def compute_turn_warning_threshold(
    thresholds: dict[str, int],
    conversation_id: ConversationId,
    turn_count: int,
    *,
    enabled: bool = True,
    start: int | None = None,
    step: int | None = None,
) -> int:
    """Return the threshold to warn at, or ``0`` for "no warning".

    V1 parity (``backend/main.py:1331-1344`` ``_compute_webui_turn_warning``).
    The threshold sequence is ``30, 40, 50, 60, ...`` (start
    ``TURN_WARNING_START`` step ``TURN_WARNING_STEP``); on the first call
    where ``turn_count`` meets a higher band than any previously warned for
    this conversation, returns the new threshold and records it in
    ``thresholds`` so we never re-warn for the same band.  Otherwise returns
    ``0``.  ``thresholds`` is the caller's process-lifetime dict (formerly
    ``self._turn_warning_thresholds``).

    Optional kwargs allow user-configurable thresholds:
      - ``enabled``: if False, always returns 0 (warnings disabled).
      - ``start``: override TURN_WARNING_START.
      - ``step``: override TURN_WARNING_STEP.
    """
    if not enabled:
        return 0
    _start = start if start is not None else TURN_WARNING_START
    _step = step if step is not None else TURN_WARNING_STEP
    if _step < 1:
        _step = 1
    if turn_count < _start:
        return 0
    steps = (turn_count - _start) // _step
    current_threshold = _start + steps * _step
    key = conversation_id.value
    last_warned = thresholds.get(key, 0)
    if current_threshold > last_warned:
        thresholds[key] = current_threshold
        return current_threshold
    return 0


def detect_effective_mode(
    *,
    system_prompt_builder: SystemPromptBuilderPort | None,
    request: Any,
    requested_tool_mode: str | None,
) -> str | None:
    """Resolve the effective tool mode via the system-prompt builder.

    Detection slice of :meth:`StreamChatUseCase._run` (B1 cohesion split).
    Calls the builder to discover the effective tool mode (which may differ
    from the explicit ``request.tool_mode`` due to auto-detection),
    passing ``tool_mode`` / ``tool_params`` as first-class
    ``SystemPromptRequest`` fields so the translate / code / ppt
    branches are reachable.  Falls back to ``requested_tool_mode``
    when the builder is absent or raises (byte-for-byte parity with
    the inline block).
    """
    if system_prompt_builder is None:
        return requested_tool_mode
    try:
        _detect_extra: dict[str, Any] = (
            dict(request.extra) if request.extra else {}
        )
        # Ensure the builder's auto-detect guard can see the latest
        # user message (it inspects ``extra["latest_user_message"]``).
        if "latest_user_message" not in _detect_extra:
            _latest = getattr(request.user_message, "text", None)
            if isinstance(_latest, str) and _latest:
                _detect_extra["latest_user_message"] = _latest
        _sp_result = system_prompt_builder.build(
            SystemPromptRequest(
                tool_mode=_detect_extra.get("tool_mode"),
                tool_params=_detect_extra.get("tool_params"),
                extra=_detect_extra,
            ),
        )
        return _sp_result.effective_tool_mode
    except Exception:  # noqa: BLE001 — best-effort detection
        return requested_tool_mode


def build_message(
    ids: IdGenerator,
    *,
    role: MessageRole,
    content: MessageContent,
    now: datetime,
    parent_id: MessageId | None,
    usage: dict[str, Any] | None = None,
    model_id: str | None = None,
    model_provider: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Message:
    return Message(
        id=MessageId.generate(ids),
        role=role,
        content=content,
        created_at=now,
        parent_id=parent_id,
        usage=usage,
        model_id=model_id,
        model_provider=model_provider,
        meta=meta,
    )


def _frame_emitted_at_ms(frame: StreamFrame) -> int | None:
    """Return a TOOL_CALL frame's real emit time (ms epoch), if stamped.

    The use case stamps ``emitted_at_ms`` onto each round's TOOL_CALL frame
    at collection time (``streaming.py``/``_stamp_emitted_at``). Returns
    ``None`` for old persisted turns / frames minted before this field
    existed, so the caller can fall back to the turn-completion time.
    """
    v = frame.payload.get("emitted_at_ms")
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    return None


def _emitted_at_to_datetime(
    emitted_at_ms: int | None, fallback: datetime,
) -> datetime:
    """Convert a ms-epoch emit time to a ``datetime`` in ``fallback``'s tz.

    Keeping the same tzinfo as the turn-completion ``fallback`` (the clock's
    timezone) means the value serialises identically to the other turn
    messages (``conversation_repository`` uses ``.isoformat()``). Returns
    ``fallback`` unchanged when no real emit time was captured (old data) or
    if the conversion fails for any reason.
    """
    if not isinstance(emitted_at_ms, int) or isinstance(emitted_at_ms, bool):
        return fallback
    if emitted_at_ms <= 0:
        return fallback
    try:
        return datetime.fromtimestamp(emitted_at_ms / 1000.0, tz=fallback.tzinfo)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return fallback


def build_tool_call_message(
    ids: IdGenerator,
    *,
    now: datetime,
    user_msg: Message,
    conv: Any,
    tc_frames: list[StreamFrame],
    tr_frames: list[StreamFrame],
    text_by_round: dict[int, str] | None = None,
    ids_by_round: dict[int, str] | None = None,
) -> Message | None:
    """Persist the agentic-loop tool calls as per-round ASSISTANT messages.

    Tool-persistence slice of :meth:`StreamChatUseCase._run` (B1 cohesion
    split).  When the followup loop collected TOOL_CALL / TOOL_RESULT
    frames, this builds **one ``role=ASSISTANT`` message per agentic round**
    — V1 parity (``useChat.js:2460-2470``: every LLM round pushes its own
    assistant message carrying that round's lead-in ``content`` + that
    round's ``tool_calls``). A *round boundary* is a TOOL_CALL frame that
    carries a non-empty ``payload["lead_in"]`` (the text the model streamed
    before invoking that round's tools — see llm_stream / local_model_stream;
    only the round's first tool_call frame carries it). The first frame always
    starts round 0 even with no lead_in (model went straight to tools).

    Splitting per round (instead of merging the whole turn into one message)
    fixes two reload regressions:

    * **lead-in text piling up before all the cards** — each round's lead-in
      now stays attached to *its own* tool cards instead of all cards being
      lumped after the first round's text.
    * **lost lead-ins of rounds 2..N** — every round's text is persisted, not
      just the first.

    Each message's ``tool_calls`` tuple holds the frontend ``ChatToolCall``
    shape (``{id, tool, args, output, status, isError, outputSize, truncated,
    ts}``) so a reload rehydrates ToolExecPanel cards. Results are paired to
    calls by **id** (the final TOOL_RESULT frame carries ``tool_call_id``
    back), robust to streaming (exec) tools emitting several partial frames.
    All messages are appended to ``conv``; returns the LAST one (used as the
    parent of the final assistant text message) or ``None`` when no tool
    frames were collected. Mirrors V1
    ``backend/chat_handler.py:661-691 / 791-856``.
    """
    if not tc_frames:
        return None
    # Pair each TOOL_CALL frame with its TOOL_RESULT frame by **id** (the
    # final result frame carries ``tool_call_id`` back). Pairing by id (not
    # positional index) is V1 parity (useChat.js:2444/2576) and is robust to
    # streaming (exec) tools that emit several partial frames before their
    # final one — the prior positional pairing mis-bound results to the wrong
    # card and left some cards empty after a reload.
    tr_by_id: dict[str, StreamFrame] = {}
    tr_no_id: list[StreamFrame] = []
    for tr_f in tr_frames:
        tcid = tr_f.payload.get("tool_call_id")
        if isinstance(tcid, str) and tcid:
            tr_by_id[tcid] = tr_f
        else:
            tr_no_id.append(tr_f)

    # Per-tool timestamps: V1 stamps each tool message with its own
    # ``Date.now()`` (useChat.js:2583) so reloaded cards show distinct times
    # (ToolExecPanel.js:99-101 history mode = ``formatTime(timestamp)``).
    # Each TOOL_CALL frame now carries its REAL emit time (``emitted_at_ms``,
    # stamped by ``streaming.py``/``_stamp_emitted_at`` when collected), so a
    # card uses ITS OWN frame's real wall-clock time — distinct per call and
    # per round, never the single turn-completion time (the bug). Falls back
    # to a monotonically increasing series anchored at ``now`` for old
    # persisted turns / frames minted before the field existed (distinct and
    # order-preserving, never all-identical).
    base_ms = int(now.timestamp() * 1000)
    n_tools = len(tc_frames)

    def _card(idx: int, tc_f: StreamFrame) -> dict[str, Any]:
        tool_name = tc_f.payload.get("tool_name") or ""
        arguments = tc_f.payload.get("arguments") or {}
        tc_id = tc_f.payload.get("tool_call_id")
        _real_ts = _frame_emitted_at_ms(tc_f)
        entry: dict[str, Any] = {
            "id": tc_f.frame_id,
            "tool": tool_name,
            "args": arguments if isinstance(arguments, dict) else {},
            "status": "done",
            "ts": _real_ts
            if _real_ts is not None
            else base_ms - (n_tools - 1 - idx),
        }
        # Pair: prefer id match. Positional fallback applies ONLY to calls
        # that never carried an id (older / non-streaming emitters) — a call
        # that HAS an id but whose id-result is missing must stay unpaired
        # (output empty) rather than fall back to ``tr_no_id[idx]``: ``idx`` is
        # the global tc index while ``tr_no_id`` holds only the id-less
        # results, so a mixed-id batch would mis-bind a wrong result onto it.
        tr_f: StreamFrame | None = None
        has_id = isinstance(tc_id, str) and bool(tc_id)
        if has_id:
            if tc_id in tr_by_id:
                tr_f = tr_by_id[tc_id]
            # else: id-result lost → leave unpaired (no positional guess).
        elif idx < len(tr_no_id):
            tr_f = tr_no_id[idx]
        if tr_f is not None:
            result_text = tr_f.payload.get("result")
            is_error = isinstance(result_text, str) and (
                result_text.startswith("[tool_error]")
                or result_text.startswith("[guardrail_blocked]")
            )
            # V1 parity (useChat.js:1137): a tool that succeeds with no
            # stdout persists the literal "(no output)" so the reloaded
            # card shows that instead of an empty output box.
            if not is_error and (
                result_text is None
                or (isinstance(result_text, str) and result_text == "")
            ):
                result_text = "(no output)"
            entry["output"] = result_text
            entry["status"] = "error" if is_error else "done"
            entry["isError"] = is_error
            # Persist the per-call-cancel marker (§3.1 tail-only) so a reloaded
            # history card still renders the "已取消/cancelled" distinction —
            # the ``[cancelled]`` text does NOT match the error sentinels, so
            # without this the card would reload as a plain "done".
            if tr_f.payload.get("cancelled") is True:
                entry["cancelled"] = True
            size = tr_f.payload.get("size")
            if size is not None:
                entry["outputSize"] = size
            truncated = tr_f.payload.get("truncated")
            if truncated is not None:
                entry["truncated"] = truncated
            # Persist the tool's wall-clock run time (ms) so a reloaded
            # history card shows "took N ms" in BOTH live and history modes
            # (V2 enhancement: V1 never persisted the duration — its history
            # cards fell back to a wall-clock timestamp).
            duration_ms = tr_f.payload.get("duration_ms")
            if duration_ms is not None:
                entry["durationMs"] = duration_ms
        # Total time = argument-generation time (from the TOOL_CALL frame's
        # ``generation_ms``, present only when the model streamed a long
        # tool-call argument) + execution time (``duration_ms``).  Persisted as
        # ``totalMs`` + the ``timedFromGeneration`` flag so a RELOADED history
        # card shows the same "generation + execution" total the live card did
        # — otherwise reload would regress to the execution-only ``durationMs``
        # (e.g. an "11ms" write that actually took 20s to generate).  Computed
        # here (backend, single source) since the frontend cannot reconstruct
        # the generation time after a reload.
        generation_ms = tc_f.payload.get("generation_ms")
        if isinstance(generation_ms, (int, float)):
            exec_ms = entry.get("durationMs")
            exec_ms_val = exec_ms if isinstance(exec_ms, (int, float)) else 0
            entry["totalMs"] = int(generation_ms) + int(exec_ms_val)
            entry["timedFromGeneration"] = True
        # Vertex AI thought_signature — persist it onto the structured card so a
        # cross-turn replay / DB-reload rebuild (``rebuild_history_wire_messages``
        # :160-162) can echo it back on the wire ``assistant.tool_calls[i]``.
        # Without this the MAIN agent's persisted tool round loses the signature
        # (the frame payload carries it via ``StreamFrame.tool_call``, but it was
        # never copied here), so a Vertex thinking model's 2nd+ turn (or a reload
        # then continue) would rebuild a signature-less tool_calls block →
        # ``flatten_tool_calls_without_signature`` degrades the structured tool
        # cards to plain text (or the API 400s). This mirrors the sub-agent card
        # builder ``wire_card_from_call`` (:945-949) and the take-over fix
        # (SUBAGENT-TAKEOVER-SIG-1); the main-agent persistence path was the last
        # builder missing it. Absent for non-Vertex calls → byte-for-byte
        # unchanged (no regression).
        _sig = tc_f.payload.get("thought_signature")
        if _sig:
            entry["thought_signature"] = _sig
        return entry

    # Group tc_frames into rounds. The authoritative key is the
    # ``round_index`` the use case stamps on every TOOL_CALL frame (the
    # 0-based agentic-loop round = the LLM call that issued the call —
    # ``streaming.py``/``StreamFrame.with_round_index``). Grouping by it
    # makes the persisted split **byte-for-byte identical** to the
    # frontend's live grouping (both key off the same ``round_index``), so
    # a stream and a reload render the same message boundaries with ZERO
    # inference — fixing the "same-round inter-tool narration mis-ordered"
    # bug the old ``lead_in``-boundary heuristic had (a tool → narration →
    # tool sequence from ONE LLM call was wrongly split when the narration
    # surfaced as a second frame's lead_in).
    #
    # SUBAGENT-PER-ROUND-INSERT (2026-07-02) — coverage extension: the
    # "byte-for-byte identical" claim above now ALSO covers the
    # ``subagent_summary`` message boundary. The live stream inserts one
    # ``subagent_summary`` message directly after each parent round that
    # dispatched a sub-agent (``frontend/src/stores/chatTabs/frameHandlers.ts``
    # ``handleSubagentStart`` — an independent per-round message via
    # ``roundSubAgentMessageIds[ri]``). The persist path mirrors that via
    # :meth:`StreamChatUseCase._insert_subagent_summary_messages_per_round`
    # (keyed off the same ``parent_round_index`` the block accumulator
    # records from the SUBAGENT_START frame's ``round_index``), so the
    # stream ↔ reload equivalence holds for turns that dispatch sub-agents
    # as well as for plain tool rounds.
    #
    # Each round's lead-in text is taken from its FIRST tool_call frame's
    # ``lead_in`` payload (the text the model streamed before that round's
    # tools — only the round's first frame carries it).
    #
    # Backward compatibility (AGENTS.md §3.1): when ``round_index`` is
    # absent on every frame (old persisted turns / pre-stamp emitters) we
    # fall back to the prior ``lead_in``-boundary heuristic so existing
    # data still rehydrates unchanged.
    have_round_index = any(
        isinstance(tc_f.payload.get("round_index"), int)
        and not isinstance(tc_f.payload.get("round_index"), bool)
        for tc_f in tc_frames
    )
    rounds: list[dict[str, Any]] = []
    if have_round_index:
        # Authoritative path: one round per distinct ``round_index`` value,
        # in first-seen order (the stream order is already round-ascending).
        round_by_index: dict[int, dict[str, Any]] = {}
        for idx, tc_f in enumerate(tc_frames):
            ri_raw = tc_f.payload.get("round_index")
            ri = (
                ri_raw
                if isinstance(ri_raw, int) and not isinstance(ri_raw, bool)
                else -1
            )
            rnd = round_by_index.get(ri)
            if rnd is None:
                li = tc_f.payload.get("lead_in")
                lead_in = li if (isinstance(li, str) and li.strip()) else ""
                # Per-round prompt-snapshot id (V1 parity): each agentic round
                # saves its OWN snapshot; the use case stamps that round's
                # ``request_id`` onto the round's TOOL_CALL frames
                # (``streaming.py``/``_stamp_request_id``).  Persisting it into
                # the round message's ``meta.request_id`` makes a reloaded tool
                # card's 📄 button open ITS round's prompt — different rounds
                # show different prompts (the whole point of per-round
                # snapshots).  The frontend reads ``meta.request_id`` per
                # message (ChatMessageList.vue tool-card ``:request-id``).
                rid_raw = tc_f.payload.get("request_id")
                request_id = (
                    rid_raw if (isinstance(rid_raw, str) and rid_raw) else None
                )
                rnd = {
                    "lead_in": lead_in,
                    "cards": [],
                    "request_id": request_id,
                    "emitted_at_ms": _frame_emitted_at_ms(tc_f),
                    # The agentic-loop round this message belongs to (the kernel
                    # ``round_no`` == the TOOL_CALL frame's ``round_index``).
                    # Stamped into ``meta.round_index`` below so
                    # ``StreamChatUseCase._reinsert_injected_messages`` can place
                    # a mid-turn user injection at its correct inter-round
                    # position on a reload (the injection recorded with
                    # ``round_no == R`` sits immediately BEFORE this round's
                    # message). ``-1`` for the legacy fallback path (no usable
                    # round key) — reinsert then falls back to arrival order.
                    "round_index": ri if ri >= 0 else None,
                }
                round_by_index[ri] = rnd
                rounds.append(rnd)
            rnd["cards"].append(_card(idx, tc_f))

        # Self-contained agent (``query::*`` — MB Pro) interleaving: the agent's
        # answer text arrives as standalone CHUNK frames carrying their OWN
        # ``round_index`` (a text run between/after tools is its own round), NOT
        # as a tool_call ``lead_in``. ``text_by_round`` maps each such round to
        # its text. Merge it so a RELOAD reproduces the live arrival order:
        #   * a TOOL round whose own text-round has text → use it as the round's
        #     lead-in (the narration that preceded that round's tool card);
        #   * a TEXT-ONLY round (a round index present in ``text_by_round`` with
        #     no tool_call) → synthesise a card-less round so it persists as its
        #     own assistant message at the right inter-tool position.
        # Without this, ALL the text fell through to the single final assistant
        # message and reloaded AFTER every tool card (the reported bug). The
        # trailing text-only round (after the last tool) is intentionally left
        # OUT here (``_finalize_turn`` still emits it as the final assistant
        # message via ``assistant_text_parts``) so the keystone usage/perf meta
        # lands on it exactly as for a normal turn.
        if text_by_round:
            tool_round_indices = set(round_by_index.keys())
            last_tool_ri = max(tool_round_indices) if tool_round_indices else -1
            for ri, txt in text_by_round.items():
                if not (isinstance(txt, str) and txt.strip()):
                    continue
                existing = round_by_index.get(ri)
                if existing is not None:
                    # Tool round: fill its lead-in from its own round's text
                    # (only when the tool_call didn't already carry one).
                    if not existing["lead_in"]:
                        existing["lead_in"] = txt
                elif ri > last_tool_ri:
                    # Trailing text after the last tool → leave for the final
                    # assistant message (keystone meta lands there).
                    continue
                else:
                    # Inter-tool text-only round → its own card-less message.
                    round_by_index[ri] = {
                        "lead_in": txt,
                        "cards": [],
                        "request_id": None,
                        "emitted_at_ms": None,
                        "round_index": ri,
                    }
            # Re-sort rounds by ``round_index`` so newly inserted text-only
            # rounds slot into their correct position (tc-only build appended
            # in first-seen order, which is already ascending, but the inserts
            # must be merged in).
            rounds = [
                round_by_index[ri] for ri in sorted(round_by_index.keys())
            ]
    else:
        # Legacy fallback: a frame with a non-empty lead_in starts a NEW
        # round; frames without lead_in belong to the current round. The
        # very first frame always opens round 0 regardless of lead_in.
        for idx, tc_f in enumerate(tc_frames):
            li = tc_f.payload.get("lead_in")
            lead_in = li if (isinstance(li, str) and li.strip()) else ""
            if not rounds or lead_in:
                rid_raw = tc_f.payload.get("request_id")
                request_id = (
                    rid_raw if (isinstance(rid_raw, str) and rid_raw) else None
                )
                rounds.append(
                    {
                        "lead_in": lead_in,
                        "cards": [],
                        "request_id": request_id,
                        "emitted_at_ms": _frame_emitted_at_ms(tc_f),
                        # Positional round index for the legacy (no
                        # ``round_index`` frame key) path — the boundary order
                        # is the round order, so the list position is a faithful
                        # 0-based round index for injection placement.
                        "round_index": len(rounds),
                    }
                )
            rounds[-1]["cards"].append(_card(idx, tc_f))

    # Emit the per-round ASSISTANT messages via the SHARED frame-agnostic core
    # (full-unification: the SAME builder the sub-agent loop calls with its own
    # per-round data, so a main-agent reload and a sub-agent transcript produce
    # byte-for-byte identical Message shapes). ``build_tool_call_message`` is now
    # the FRAME ADAPTER: it groups frames → ``rounds`` dicts (above) and hands
    # them to the core, which owns the Message construction + ``conv`` append.
    return build_round_messages(
        ids,
        now=now,
        rounds=rounds,
        first_parent_id=user_msg.id,
        conv=conv,
        ids_by_round=ids_by_round,
    )


def build_round_messages(
    ids: IdGenerator,
    *,
    now: datetime,
    rounds: list[dict[str, Any]],
    first_parent_id: MessageId | None,
    conv: Any = None,
    ids_by_round: dict[int, str] | None = None,
) -> Message | None:
    """Frame-agnostic core: build one ASSISTANT ``Message`` per agentic round.

    Full-unification SHARED builder.  See prior docstring for the field
    contract of each ``rounds`` entry.

    §concurrent-writer refactor 2026-08-07 — ``ids_by_round`` is an
    OPTIONAL mutable mapping ``round_index → message_id_value`` used to
    stabilise the message id assigned to each round across REPEATED calls
    on the same turn (:meth:`_persist_completed_rounds` calls this once
    per completed round; :meth:`_finalize_turn` may call it again at
    turn end).  On each round:

    * If ``ids_by_round`` has a cached id for the round's ``round_index``
      → reuse it (the persisted row updates in place via ``save_messages``
      upsert-by-id).
    * Otherwise mint a fresh id via ``MessageId.generate(ids)`` AND
      populate the cache so any later rebuild sees the same id.

    Passing ``None`` (the sub-agent loop / one-shot callers) skips the
    cache entirely and behaves like the pre-refactor path — a new id
    on every call (byte-for-byte parity with the historical behaviour
    for those single-call sites).
    """
    if not rounds:
        return None
    last_msg: Message | None = None
    for rnd in rounds:
        lead_in_text = rnd["lead_in"]
        _round_rid = rnd.get("request_id")
        _meta = (
            {"request_id": _round_rid}
            if isinstance(_round_rid, str) and _round_rid
            else None
        )
        _round_index = rnd.get("round_index")
        if isinstance(_round_index, int) and not isinstance(_round_index, bool):
            _meta = {**(_meta or {}), "round_index": _round_index}
        _round_created_at = _emitted_at_to_datetime(
            rnd.get("emitted_at_ms"), now,
        )
        _round_usage = rnd.get("usage")
        # Stable id per round — see docstring.  ``round_index`` may be
        # ``None`` on the legacy fallback (no per-round key); cache only
        # when we have a real int key.
        msg_id: MessageId
        if (
            ids_by_round is not None
            and isinstance(_round_index, int)
            and not isinstance(_round_index, bool)
            and _round_index in ids_by_round
        ):
            msg_id = MessageId.of(ids_by_round[_round_index])
        else:
            msg_id = MessageId.generate(ids)
            if (
                ids_by_round is not None
                and isinstance(_round_index, int)
                and not isinstance(_round_index, bool)
            ):
                ids_by_round[_round_index] = msg_id.value
        msg = Message(
            id=msg_id,
            role=MessageRole.ASSISTANT,
            content=MessageContent(text=lead_in_text or "[tool_calls]"),
            created_at=_round_created_at,
            parent_id=last_msg.id if last_msg is not None else first_parent_id,
            tool_calls=tuple(rnd["cards"]),
            usage=dict(_round_usage) if isinstance(_round_usage, dict) else None,
            meta=_meta,
        )
        if conv is not None:
            conv.append_message(msg)
        last_msg = msg
    return last_msg


def wire_card_from_call(
    call: dict[str, Any],
    results_by_id: dict[str, str],
    durations_by_id: dict[str, int],
    thought_signatures: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Map one OpenAI wire ``tool_calls`` entry → a front-end ``ChatToolCall``.

    Full-unification converter (mirrors the route's ``_wire_tool_calls_to_render``
    so the structured card a resume/render reads is byte-for-byte what the tab
    previously showed): ``{id, type:"function", function:{name, arguments},
    thought_signature?}`` → ``{id, tool, args, output?, status, outputSize?,
    durationMs?, thought_signature?}``. The executed result (``role:tool``
    content, paired by ``tool_call_id``) folds onto ``output``. The Vertex
    ``thought_signature`` (when the wire carried it OR the caller supplies it via
    ``thought_signatures``) is preserved verbatim so a later resume rebuild
    echoes it back losslessly. Returns ``None`` for a malformed entry (no
    ``function.name``).
    """
    fn = call.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    args_raw = fn.get("arguments")
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            args = {"_raw": args_raw}
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        args = {"_value": args}
    cid = call.get("id")
    result_text = results_by_id.get(cid) if isinstance(cid, str) else None
    is_error = isinstance(result_text, str) and (
        result_text.startswith("[tool_error]")
        or result_text.startswith("[guardrail_blocked]")
    )
    entry: dict[str, Any] = {
        "tool": name,
        "args": args,
        "status": "error" if is_error else "done",
    }
    if isinstance(cid, str) and cid:
        entry["id"] = cid
        entry["callId"] = cid
    if result_text is not None:
        entry["output"] = result_text
        entry["outputSize"] = len(result_text)
    if isinstance(cid, str) and cid:
        dur = durations_by_id.get(cid)
        if isinstance(dur, int):
            entry["durationMs"] = dur
    # Vertex AI thought_signature — preserved verbatim so a resume rebuild
    # (``rebuild_history_wire_messages``) can echo it back (signature-lossless
    # full unification, AGENTS.md 拍板 "存签名"). Prefer the value carried on the
    # wire entry itself: the kernel's built-in wire-growing now writes the
    # signature onto ``assistant.tool_calls[i]`` (``_single_agent_turn.py``:
    # 843-860, ``build_assistant_tool_calls_block(tool_metas,
    # thought_signatures=_round_signatures)``), so the wire entry normally
    # carries it. Fall back to the per-run ``thought_signatures`` map
    # (call_id → sig) the loop captured from the TOOL_CALL frame payloads as a
    # redundant safety net (double-cover; still correct if the wire entry ever
    # lacks it).
    sig = call.get("thought_signature")
    if not sig and thought_signatures and isinstance(cid, str) and cid:
        sig = thought_signatures.get(cid)
    if sig:
        entry["thought_signature"] = sig
    return entry


def wire_to_structured_messages(
    wire: list[dict[str, Any]],
    *,
    ids: IdGenerator,
    now: datetime,
    thought_signatures: dict[str, str] | None = None,
) -> list[Message]:
    """Convert a sub-agent's OpenAI wire → an authoritative structured ``Message``
    list.

    Full-unification single converter (replaces the route's
    ``_wire_to_messages`` reverse-fold): the persisted authoritative transcript
    is built ONCE here, in the SAME ``Message`` shape the main agent stores into
    ``Conversation.messages`` — so the detail route serialises it directly and
    the feed-the-model wire is rebuilt from it via
    ``rebuild_history_wire_messages`` (口径 parity with the main agent's
    cross-turn rebuild).

    Lives in the application layer so BOTH the sub-agent loop (``agent_tool`` —
    autonomous + resume) and the take-over path (``streaming``) build the SAME
    structured transcript from the SAME wire, with zero divergence.

    * ``system`` / ``user`` turns → standalone ``Message`` (role preserved, so a
      resume rebuild re-prepends the system prompt + keeps the task turns).
    * ``assistant{tool_calls}`` rounds → one ASSISTANT ``Message`` per round via
      the SHARED ``build_round_messages`` core; each call's executed output is
      folded onto its card (paired by ``tool_call_id``), thought_signature
      preserved. The per-round ``request_id`` (📄 button) + ``usage`` (token
      line) the loop stamped onto the assistant turn ride into the message.
    * plain ``assistant`` text turns (final answer / interrupt marker) →
      standalone ASSISTANT ``Message``.
    * ``role:tool`` turns are folded onto their originating call (never emitted
      standalone — main-agent history parity).

    The whole list is chained by ``parent_id`` in wire order.
    """
    # Index each tool result + its display duration by call id.
    results_by_id: dict[str, str] = {}
    durations_by_id: dict[str, int] = {}
    for turn in wire:
        if isinstance(turn, dict) and turn.get("role") == "tool":
            tcid = turn.get("tool_call_id")
            if isinstance(tcid, str) and tcid:
                content = turn.get("content")
                results_by_id[tcid] = (
                    content if isinstance(content, str) else str(content or "")
                )
                dur = turn.get("duration_ms")
                if isinstance(dur, int):
                    durations_by_id[tcid] = dur

    out: list[Message] = []
    last_id: MessageId | None = None
    _turn_index = 0

    def _turn_emitted_ms(base_ms: int, idx: int) -> int:
        """Monotonic per-turn ms-epoch time: ``base + idx`` (1 ms per turn).

        Gives a reopened tab DISTINCT, ORDERED per-turn times (display parity
        with the prior wire-derived ``created_at + i`` synthesis) instead of
        every turn collapsing to the single finalize ``now``. We use a uniform
        monotonic series anchored at ``now`` rather than mixing in the wire's own
        per-turn ``created_at`` (only user turns carry one), because mixing real
        + synthesised times can REORDER turns (a real early user time vs the
        finalize-anchored rest) — a uniform series preserves wire order.
        """
        return base_ms + idx

    _base_ms = int(now.timestamp() * 1000)

    for turn in wire:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role == "tool":
            # Folded onto its originating call's card above.
            continue
        _ts_ms = _turn_emitted_ms(_base_ms, _turn_index)
        _turn_index += 1
        content_raw = turn.get("content")
        text = content_raw if isinstance(content_raw, str) else str(
            content_raw or ""
        )
        raw_calls = turn.get("tool_calls")
        if role == "assistant" and isinstance(raw_calls, list) and raw_calls:
            cards = [
                c
                for c in (
                    wire_card_from_call(
                        call, results_by_id, durations_by_id, thought_signatures,
                    )
                    for call in raw_calls
                    if isinstance(call, dict)
                )
                if c is not None
            ]
            _rid = turn.get("request_id")
            _usage = turn.get("usage")
            rnd: dict[str, Any] = {
                "lead_in": text,
                "cards": cards,
                "request_id": _rid if isinstance(_rid, str) and _rid else None,
                "round_index": None,
                # Monotonic per-turn time (ms-epoch) so the round message's
                # ``created_at`` is distinct + ordered, not the uniform ``now``.
                "emitted_at_ms": _ts_ms,
                "usage": _usage if isinstance(_usage, dict) else None,
            }
            built = build_round_messages(
                ids,
                now=now,
                rounds=[rnd],
                first_parent_id=last_id,
                conv=None,
            )
            if built is not None:
                out.append(built)
                last_id = built.id
            continue
        # Standalone system / user / plain-assistant turn.
        try:
            msg_role = MessageRole(str(role)) if role is not None else (
                MessageRole.ASSISTANT
            )
        except ValueError:
            msg_role = MessageRole.ASSISTANT
        # ``MessageContent`` rejects empty / whitespace-only text. A turn that
        # carries NEITHER text NOR tool_calls (e.g. a wire ``assistant`` with
        # ``content=None`` + empty ``tool_calls``) holds nothing renderable —
        # the prior wire-derived ``_wire_to_messages`` path emitted an empty
        # bubble the front-end mapper hid anyway, so we simply SKIP it here.
        # (The earlier ``text=" "`` fallback was a latent crash: a single space
        # strips to empty and ``MessageContent`` raises ``validation.string.
        # empty`` — AGENTS.md 🟡🟡 "发现缺陷必须修", fixed under SUBAGENT-UNIFY-6.)
        if not text.strip():
            continue
        # Carry the per-message ``request_id`` (📄 button) + ``usage`` (token
        # line) the loop stamps onto a plain assistant turn's wire entry, the
        # SAME main-agent-parity affordances the tool-round branch preserves
        # above. The standalone final-answer assistant turn is where the loop
        # stamps the keystone snapshot id + the round's measured usage; dropping
        # them here (the prior behaviour) silently lost the standalone tab's
        # per-message snapshot button + token line once the structured
        # transcript became the SOLE truth source (SUBAGENT-UNIFY-6). Absent on
        # system / user turns (and runs that recorded none) → no meta / usage.
        _rid_s = turn.get("request_id")
        _usage_s = turn.get("usage")
        _meta_s = (
            {"request_id": _rid_s}
            if isinstance(_rid_s, str) and _rid_s
            else None
        )
        msg = Message(
            id=MessageId.generate(ids),
            role=msg_role,
            content=MessageContent(text=text),
            created_at=_emitted_at_to_datetime(_ts_ms, now),
            parent_id=last_id,
            usage=_usage_s if isinstance(_usage_s, dict) else None,
            meta=_meta_s,
        )
        out.append(msg)
        last_id = msg.id
    return out


def assemble_multimodal_messages(
    *,
    extra: dict[str, Any],
    history: tuple[Any, ...],
) -> None:
    """PR-097 K-1: assemble the multimodal LLM message list in ``extra``.

    When the caller (apps-layer channels dispatch bridge) supplies
    pre-decoded OpenAI-Vision content blocks via
    ``extra["image_content_blocks"]``, build the full message list so
    the user turn reaching the model is a single multimodal entry
    whose ``content`` is the merged block list.  The LLM adapter
    honours ``extra["messages"]`` as a complete override, so the
    assembled list is slotted there and the channels-specific key is
    popped so the adapter never sees it.  When no blocks are present
    the only effect is popping the (absent) key — behaviour unchanged.
    """
    image_content_blocks = extra.pop("image_content_blocks", None)
    if not (
        isinstance(image_content_blocks, list)
        and image_content_blocks
        and "messages" not in extra
    ):
        return
    # Rebuild prior tool-using rounds as assistant{tool_calls} + role:tool
    # (same fix as the text path's ``_build_base_wire_messages`` /
    # ``_build_llm_request``): a flat replay here would drop the historical
    # tool linkage AND leak the internal "[tool_calls]" content sentinel to
    # the model on a multimodal turn that follows a tool round.
    assembled: list[dict[str, Any]] = rebuild_history_wire_messages(history)
    # The new multimodal user turn merges any caller-supplied text
    # caption with the image blocks; if the prompt text is the
    # placeholder "[image]" injected by the channels parser
    # (PR-097 §2 R-8) we drop it so the synthesised "请描述这张图片"
    # inside the blocks reaches the model cleanly.
    assembled.append(
        {"role": "user", "content": list(image_content_blocks)}
    )
    extra["messages"] = assembled


def build_synthetic_retry_history(
    *,
    conv: Any,
    compressed_history: tuple[Any, ...] | None,
    synthetic_user_text: str,
) -> tuple[Any, ...]:
    """Append a synthetic user nudge to the current history.

    Used by the PR-091 H-9 empty-completion guard: when an agent
    round finishes with ``finish_reason == "stop"`` but produced
    zero visible text content after a tool call (Gemini's
    completion-tokens=0 quirk), we inject a synthetic user message
    asking the model to summarise the tool output, then re-issue
    the round.  The synthetic message lives in the
    ``compressed_history`` override; we never mutate the
    :class:`Conversation` aggregate.
    """
    # Start from whatever history we'd send next (compressed or live).
    if compressed_history is not None:
        base: list[Any] = list(compressed_history)
    else:
        # Convert live conv messages to dicts so the LLM adapter's
        # downstream sanitiser handles them uniformly.
        base = []
        for msg in conv.messages:
            # SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG: a subagent_summary
            # message is UI-only (fold blocks in meta.subAgentBlocks; the
            # sub-agent's textual result already reached the model via the
            # parent agent tool_call's synthetic TOOL_RESULT). Skip entirely
            # — same treatment as :func:`rebuild_history_wire_messages`.
            _text_raw = (
                msg.content.text
                if hasattr(msg.content, "text") and msg.content.text
                else ""
            )
            if _text_raw == _SUBAGENT_SUMMARY_CONTENT_SENTINEL:
                continue
            d: dict[str, Any] = {"role": msg.role.value}
            if _text_raw:
                d["content"] = _text_raw
            else:
                d["content"] = ""
            base.append(d)
    base.append({"role": "user", "content": synthetic_user_text})
    return tuple(base)


def split_atomic_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a flat wire list into atomic trim units (P3 §7.1 helper).

    Mirrors :func:`qai.chat.adapters.context_compressor._split_atomic_groups`
    verbatim; duplicated here so the ``application``-layer callers do not
    import from the ``adapters`` layer (``layered-chat`` import-linter
    contract). The two implementations MUST stay in sync — a unit test in
    ``test_turn_prefix_atomic_boundary`` cross-checks the prefix cut against
    the shared implementation to catch drift.

    An atomic group is the smallest set of messages that must move together
    to keep the wire self-consistent: an ``assistant`` message that
    announces ``tool_calls`` + ALL the immediately-following ``role=tool``
    replies -> one group; any other single message -> its own group.
    """
    groups: list[list[dict[str, Any]]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            group = [msg]
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                group.append(messages[j])
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([msg])
            i += 1
    return groups


def compute_turn_prefix_at_boundary(
    wire: list[dict[str, Any]],
    *,
    context_limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(prefix_wire, user_message_id)`` for the last long turn.

    Walks ``wire`` from the end back to the LAST ``role: user`` message
    (the "current turn" boundary — turns start on a user role, same rule
    as :func:`qai.chat.adapters.context_compressor._parse_turns`), then
    estimates that turn's bytes/4 token count. When it exceeds
    ``LONG_TURN_PREFIX_RATIO × PROTECT_WINDOW_RATIO × context_limit`` the
    turn is deemed "long" and its PREFIX is returned — everything from the
    user message up to (but NOT including) the LAST atomic group inside the
    turn. Splitting the turn's tail via :func:`split_atomic_groups`
    guarantees the cut lands on an atomic-group boundary: an
    ``assistant{tool_calls}`` and its paired ``role: tool`` replies MUST
    move together, so we NEVER separate them.

    Returns ``([], None)`` when the last turn is short, no user message
    is present, or the turn contains only one atomic group (nothing to
    summarise — the whole turn IS the tail).

    Shared by the streaming path (``StreamChatUseCase._compress_via_checkpoint``)
    and the force path (``ForceCompactChatUseCase.execute``) so a user-issued
    ``/compact`` fires the SAME P3 gate an auto-compaction does.
    """
    # Deferred import: the kernel module imports from ``ports``, and this
    # helper module is loaded very early by ``streaming``.
    from qai.chat.application.use_cases._agentic_kernel import (
        LONG_TURN_PREFIX_RATIO,
        PROTECT_WINDOW_RATIO,
        estimate_wire_tokens,
    )

    if not wire:
        return [], None
    # Locate the last user boundary. Iterate in reverse so the FIRST
    # match is the latest turn boundary.
    last_user_idx: int | None = None
    last_user_id: str | None = None
    for idx in range(len(wire) - 1, -1, -1):
        msg = wire[idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = idx
            # ``user_message_id`` on the wire dict is optional (most wire
            # rows don't carry ids); fall back to an index-derived
            # synthetic id so the checkpoint entry is still addressable.
            raw_id = msg.get("id") or msg.get("message_id")
            last_user_id = (
                str(raw_id) if raw_id is not None else f"turn-idx-{idx}"
            )
            break
    if last_user_idx is None or last_user_id is None:
        return [], None
    last_turn = wire[last_user_idx:]
    # Estimate the turn's tokens using the same bytes/4 estimator the
    # compaction gate uses (so P3's threshold is口径-consistent with P0).
    turn_tokens = estimate_wire_tokens(last_turn, model_hint=None)
    # Long-turn threshold: LONG_TURN_PREFIX_RATIO × PROTECT_WINDOW_RATIO ×
    # context_limit. PROTECT_WINDOW_RATIO defaults to 0.35 (~0.21 of
    # context_limit for a 200k window ≈ 42 000 tokens). Any turn that
    # consumes more than 60% of the protection budget on its own is long
    # enough that its prefix is worth summarising.
    threshold = int(
        LONG_TURN_PREFIX_RATIO * PROTECT_WINDOW_RATIO * context_limit
    )
    if turn_tokens < threshold:
        return [], None
    # Split the turn on atomic-group boundaries. Cut just BEFORE the last
    # atomic group: that group is the "suffix" (recent work — usually
    # the final assistant reply + any trailing tool cascade), the rest
    # is the "prefix" the summariser will fold into a system note.
    groups = split_atomic_groups(last_turn)
    if len(groups) < 2:
        return [], None
    prefix_groups = groups[:-1]
    prefix_wire: list[dict[str, Any]] = [
        msg for group in prefix_groups for msg in group
    ]
    return prefix_wire, last_user_id
