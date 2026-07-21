# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure sub-agent frame / meta helpers extracted from ``streaming.py``.

ARCH-1 / A-3 cohesion split (zero behaviour change): these module-level
pure functions used to live at the top of
:mod:`qai.chat.application.use_cases.streaming`.  They are byte-for-byte
identical -- only relocated to a sibling module so the orchestrating
``StreamChatUseCase`` file shrinks below the cohesion advisory ceiling
(AGENTS.md §3.6).  They depend only on their arguments + chat domain
types (``StreamFrame`` / ``StreamFrameType`` / ``MessageRole`` /
``MessageContent``), never on any use-case ``self`` state.

* :data:`SUBAGENT_FRAME_TYPES` -- the sub-agent frame types the parent
  stream folds into a persistable block.
* :func:`now_ms` -- integer-ms wall clock via the injected clock.
* :func:`drop_trailing_current_user` -- drop a duplicated trailing
  current-user turn from history.
* :func:`accumulate_sub_agent_block` -- fold one ``subagent_*`` frame
  into the per-index block accumulator.
* :func:`subagent_event_to_frame` -- translate one sub-agent dict event
  into a typed ``SUBAGENT_*`` frame.
* :func:`build_assistant_meta` -- assemble the V1-parity ``meta``
  envelope for a finalised assistant turn.
"""

from __future__ import annotations

from typing import Any

from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType

# ---------------------------------------------------------------------------
# V1-parity reload-extras helpers (request_id / image_url / perf /
# subAgentBlocks persistence — see Message.meta + migration 021)
# ---------------------------------------------------------------------------
# The sub-agent frame types that carry the data the frontend folds into a
# rendered sub-agent block (V1 useChat.js:1348-1397). Server-side we fold the
# same frames into a persistable block so a history reload re-renders them.
SUBAGENT_FRAME_TYPES = frozenset(
    {
        StreamFrameType.SUBAGENT_START,
        StreamFrameType.SUBAGENT_OUTPUT,
        StreamFrameType.SUBAGENT_TOOL,
        StreamFrameType.SUBAGENT_TOOL_RESULT,
        StreamFrameType.SUBAGENT_DONE,
        StreamFrameType.SUBAGENT_ERROR,
    }
)


def now_ms(clock: Any) -> int:
    """Current wall-clock in integer milliseconds via the injected clock."""
    return int(clock.now().timestamp() * 1000)


def drop_trailing_current_user(
    messages: tuple[Any, ...], *, current: "MessageContent"
) -> tuple[Any, ...]:
    """Return *messages* without a trailing current-user turn.

    The streaming use case persists the current user message into the
    conversation (so a history reload renders it) and *also* passes it as
    the ``prompt`` of the LLM request. The LLM adapters append ``prompt``
    after ``history``, so including the trailing copy here would send the
    user message twice. This drops that last element when it is a USER
    message whose text matches the current prompt — mirroring V1's single
    consolidated-messages semantics (``backend/chat_handler.py``). All other
    cases (empty history, last message not the current user turn) are left
    untouched.
    """
    if not messages:
        return messages
    last = messages[-1]
    role = getattr(last, "role", None)
    is_user = role == MessageRole.USER or getattr(role, "value", None) == "user"
    if not is_user:
        return messages
    last_content = getattr(last, "content", None)
    last_text = getattr(last_content, "text", None)
    if last_text is not None and last_text == current.text:
        return messages[:-1]
    return messages


def _subagent_turn_for_round(
    block: dict[str, Any], round_index: int | None
) -> dict[str, Any]:
    """Return (creating if needed) the ordered turn for ``round_index``.

    A sub-agent block keeps its narration + tool cards in ordered ``turns``
    (``[{round_index, content, tools[]}]``) — one per agentic round — so the
    renderer shows "text → tools → text → tools" in the SAME order the
    sub-agent produced them (identical to the main agent's per-round message
    rendering), instead of piling all text after all tools. A frame whose
    ``round_index`` is absent folds into the latest turn (or a fresh round 0
    when none exists yet) so legacy/unstamped frames never crash.
    """
    turns: list[dict[str, Any]] = block["turns"]
    if round_index is None:
        if turns:
            return turns[-1]
        round_index = 0
    for turn in turns:
        if turn["round_index"] == round_index:
            return turn
    turn = {"round_index": round_index, "content": "", "tools": []}
    turns.append(turn)
    # Keep turns ordered by round so out-of-order frames still render in
    # round order (the kernel emits in order, but this makes it robust).
    turns.sort(key=lambda t: t["round_index"])
    return turn


def accumulate_sub_agent_block(
    blocks: dict[Any, dict[str, Any]],
    frame: StreamFrame,
) -> None:
    """Fold one ``subagent_*`` frame into the per-dispatch block accumulator.

    The persisted block shape (``meta.subAgentBlocks``) matches the
    live-stream :class:`SubAgentBlock` the renderer expects on reload:
    ``{index, total, prompt_preview, turns, rounds, status, error?,
    _collapsed}``. ``turns`` is the ordered per-round timeline
    (``[{round_index, content, tools[]}]``) — narration text and tool cards
    of the SAME round live together so the block renders them interleaved in
    real time and on reload (the main-agent per-round rendering model), rather
    than the old flat ``content`` + ``tools`` that lost their relative order.
    ``rounds`` stays the integer round COUNT (for the "(N rounds)" header).

    SUBAGENT-DISPATCH-KEY (2026-07-02, P1 fix for the "multi-round dispatch
    dict-key collision" reported in HANDOFF §4 #1): the accumulator's dict
    key is a **composite** ``(parent_round_index, index)`` tuple — NOT just
    ``index``. Rationale: ``index`` is the sub-agent's ordinal WITHIN A
    SINGLE dispatch round (``_dispatch_agent_calls_streaming`` enumerates
    ``agent_tc_frames`` starting at ``0`` for each round it runs in). So a
    turn that dispatches a sub-agent in round R and ANOTHER in round R+K
    would produce two ``SUBAGENT_START`` frames BOTH carrying ``index=0``.
    Keying by ``index`` alone made the R+K block overwrite R's block —
    losing R's transcript on reload (backend-only; the frontend routes by
    ``roundSubAgentMessageIds[ri]`` and stays correct live). The composite
    key isolates the two dispatches.

    Legacy fallback (``round_index`` absent on ``SUBAGENT_START`` — old
    emitter / stub tests): key falls to ``(-1, index)``, byte-for-byte
    equivalent to the pre-fix ``int``-only key for a single-dispatch turn.

    Follow-up frames (``SUBAGENT_OUTPUT`` / ``TOOL`` / ``TOOL_RESULT`` /
    ``DONE`` / ``ERROR``) do not always carry ``round_index`` — the wire
    frame is unchanged (no ``round_index`` on ``subagent_done`` /
    ``subagent_error`` factories, AGENTS.md §3.1). They locate their target
    block via ``blocks["_alias_by_index"][idx]`` — a per-accumulator dict
    the ``SUBAGENT_START`` handler maintains, mapping the sub-agent's
    ``index`` to the composite key of the MOST RECENT block bearing that
    index. A second dispatch's ``SUBAGENT_START`` (same idx, different
    round) simply UPDATES the alias to its new composite key; the prior
    block stays intact under its own key. The alias itself lives under a
    reserved string key (``"_alias_by_index"``) and is stripped by the
    serializer (:func:`build_assistant_meta` / callers of the block dict)
    so it never leaks into ``meta.subAgentBlocks``.
    """
    payload = frame.payload
    idx = int(payload.get("index", 0))

    _round_raw = payload.get("round_index")
    round_index = (
        int(_round_raw)
        if isinstance(_round_raw, int) and not isinstance(_round_raw, bool)
        else None
    )

    ftype = frame.frame_type

    # Alias registry (index → composite key of the most recent block bearing
    # that index). Lazily initialised on the first frame so a caller can
    # inspect the same ``blocks`` shape it always had, plus this one reserved
    # string key. Live blocks are keyed by ``tuple`` and the alias by ``str``,
    # so callers iterating on ``blocks`` need to filter the string key
    # (:func:`iter_subagent_blocks`) — every serializer + inserter in the
    # codebase already does this via that helper.
    alias: dict[int, tuple[int, int]] = blocks.setdefault(  # type: ignore[assignment]
        "_alias_by_index", {},
    )

    if ftype is StreamFrameType.SUBAGENT_START:
        # New dispatch: mint a fresh composite key. If the caller omits
        # ``round_index`` (legacy path / stub tests) fall back to the
        # ``-1`` sentinel — a legacy single-dispatch turn then keeps
        # ``(-1, 0)`` as its key, indistinguishable from the pre-fix
        # single-``int`` key semantics.
        composite_key: tuple[int, int] = (
            round_index if round_index is not None else -1,
            idx,
        )
        block = blocks.get(composite_key)
        if block is None:
            block = {
                "index": idx,
                "total": int(payload.get("total", 1)) or 1,
                "prompt_preview": "",
                "turns": [],
                "rounds": 0,
                "status": "running",
                "_collapsed": True,
            }
            blocks[composite_key] = block
        # SUBAGENT-PER-ROUND-INSERT (2026-07-02): record the parent agent's
        # round_index at which THIS sub-agent was dispatched, so
        # :meth:`StreamChatUseCase._insert_subagent_summary_messages_per_round`
        # can group blocks BY parent round and insert one INDEPENDENT
        # ``subagent_summary`` message DIRECTLY AFTER each parent round's
        # per-round message on reload. Legacy path (no ``round_index`` on
        # the frame) leaves this unset, and the inserter falls back to
        # end-of-turn append (byte-for-byte pre-fix behaviour).
        if "parent_round_index" not in block and round_index is not None:
            block["parent_round_index"] = round_index

        block["total"] = int(payload.get("total", block["total"])) or block["total"]
        block["prompt_preview"] = str(payload.get("prompt_preview", ""))
        # Update the alias so follow-up frames with matching ``index`` land
        # on THIS (most-recent) block, not on a prior dispatch's block that
        # shared the same ``index``.
        alias[idx] = composite_key
        # Carry the resumable id from the START frame too (it now arrives at
        # start, not only at done). Lets a reloaded RUNNING block keep its
        # open/wake handle. Absent on legacy data / unwired persistence.
        sa_id_start = payload.get("subagent_id")
        if isinstance(sa_id_start, str) and sa_id_start:
            block["subagent_id"] = sa_id_start
        # Carry the profile name + human-readable label (§3.1 tail-appended,
        # V2 UX enhancement) so a reloaded block shows the same type badge +
        # title the live stream did. Absent on legacy data (older frames /
        # spawns without a name) — the UI then falls back to the generic
        # ``SubAgent N`` label + no type badge.
        sa_type_start = payload.get("subagent_type")
        if isinstance(sa_type_start, str) and sa_type_start:
            block["subagent_type"] = sa_type_start
        sa_name_start = payload.get("name")
        if isinstance(sa_name_start, str) and sa_name_start:
            block["name"] = sa_name_start
        return

    # Non-START frame: locate the target block. Priority:
    #   1. Frame carries ``round_index`` → composite key ``(round, idx)``.
    #   2. Alias points at the most recent block with matching ``index``.
    #   3. Bootstrap fallback (no START ever seen, no alias) — mint a
    #      minimal running block under ``(-1, idx)`` so the fold does not
    #      crash on an out-of-order frame. Matches the pre-fix shape.
    target_key: tuple[int, int] | None = None
    if round_index is not None:
        candidate = (round_index, idx)
        if candidate in blocks:
            target_key = candidate
    if target_key is None and idx in alias:
        target_key = alias[idx]
    if target_key is None:
        target_key = (
            round_index if round_index is not None else -1,
            idx,
        )
        blocks[target_key] = {
            "index": idx,
            "total": int(payload.get("total", 1)) or 1,
            "prompt_preview": "",
            "turns": [],
            "rounds": 0,
            "status": "running",
            "_collapsed": True,
        }
        alias[idx] = target_key
    block = blocks[target_key]

    if ftype is StreamFrameType.SUBAGENT_OUTPUT:
        turn = _subagent_turn_for_round(block, round_index)
        turn["content"] = str(turn["content"]) + str(payload.get("content", ""))
    elif ftype is StreamFrameType.SUBAGENT_TOOL:
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            tool_args = payload.get("tool_args")
            tool_entry: dict[str, Any] = {
                "name": tool_name,
                "args": tool_args if isinstance(tool_args, dict) else {},
            }
            # Carry the call id (optional) so the matching result frame can
            # pair to THIS row by id on history replay.
            tcid = payload.get("tool_call_id")
            if isinstance(tcid, str) and tcid:
                tool_entry["tool_call_id"] = tcid
            # Persist the upstream wall-clock (``emitted_at_ms``) as ``ts``
            # so a reloaded history block gives each sub-agent tool card the
            # SAME real start time the live stream had — the frontend
            # ``ToolExecPanel`` uses it as the unmount-survival anchor for
            # the elapsed timer (parity with the main-agent tool card's
            # ``ChatToolCall.timestamp``). Without this, a browser-tab
            # switch / scroll-out remount resets elapsed to 00:00. Absent
            # when the upstream frame was minted before the stamping change
            # (legacy data) — the UI then falls back to a remount-local
            # anchor (pre-fix behaviour, no regression).
            emitted = payload.get("emitted_at_ms")
            # `isinstance(True, int) is True` in Python (bool inherits int) —
            # exclude bools defensively, mirroring `_frame_emitted_at_ms` in
            # `_streaming_helpers.py:404` so the two ts-extraction paths stay
            # symmetric. `_now_ms` never produces a bool, but a payload that
            # was mutated upstream by a misbehaving relay could.
            if isinstance(emitted, int) and not isinstance(emitted, bool):
                tool_entry["ts"] = emitted
            turn = _subagent_turn_for_round(block, round_index)
            turn["tools"].append(tool_entry)
    elif ftype is StreamFrameType.SUBAGENT_TOOL_RESULT:
        # Fill the executed result onto the matching tool row (by id when
        # present, else the most-recent row of the same name still missing a
        # result). Searched ACROSS all turns' tool lists so a result frame
        # still pairs even if its round_index hint differs. Mirrors the live
        # frontend `handleSubagentToolResult` so the persisted block
        # re-renders the collapsible result panel on reload.
        tname = payload.get("tool_name")
        tcid = payload.get("tool_call_id")
        result_text = payload.get("result", "")
        ok = payload.get("ok", True)
        all_tools: list[dict[str, Any]] = [
            row for turn in block["turns"] for row in turn["tools"]
        ]
        target = None
        if isinstance(tcid, str) and tcid:
            for row in all_tools:
                if row.get("tool_call_id") == tcid and "result" not in row:
                    target = row
                    break
        if target is None and isinstance(tname, str) and tname:
            for row in reversed(all_tools):
                if row.get("name") == tname and "result" not in row:
                    target = row
                    break
        if target is not None:
            target["result"] = (
                result_text if isinstance(result_text, str) else str(result_text)
            )
            target["ok"] = bool(ok)
            if "size" in payload:
                target["size"] = payload.get("size")
            if "truncated" in payload:
                target["truncated"] = payload.get("truncated")
            if "duration_ms" in payload:
                target["duration_ms"] = payload.get("duration_ms")
    elif ftype is StreamFrameType.SUBAGENT_DONE:
        block["rounds"] = int(payload.get("rounds", block["rounds"]))
        block["status"] = "done"
        # Persist the resumable sub-agent session id (optional, §3.1) so a
        # reloaded history block still carries the handle to open / wake the
        # sub-agent. Absent on legacy data / when persistence is unwired.
        sa_id = payload.get("subagent_id")
        if isinstance(sa_id, str) and sa_id:
            block["subagent_id"] = sa_id
    elif ftype is StreamFrameType.SUBAGENT_ERROR:
        block["status"] = "error"
        block["error"] = str(payload.get("message", "unknown")) or "unknown"


def iter_subagent_blocks(
    blocks: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the live sub-agent blocks in stable dispatch order.

    SUBAGENT-DISPATCH-KEY (2026-07-02, P1) — the accumulator stores blocks
    under composite ``(parent_round_index, index)`` tuples PLUS a reserved
    string key ``"_alias_by_index"`` carrying a per-index alias map (see
    :func:`accumulate_sub_agent_block`). This helper is the single place
    that yields "just the blocks, in order":

    * Filters out the alias sentinel (``"_alias_by_index"``) so the alias
      never leaks into ``meta.subAgentBlocks`` on the wire / DB.
    * Returns blocks sorted by their composite key — natural tuple sort
      yields dispatch-order (round asc, then intra-round index asc). This
      matches the previous ``sorted(sub_agent_blocks)`` semantics for the
      common single-dispatch case (``(-1, 0), (-1, 1), ...`` — legacy) AND
      makes multi-dispatch turns render in dispatch order on reload.
    * A malformed key (non-tuple, non-string) is skipped defensively so a
      stray write never crashes the finalize path.

    All existing call sites (``build_assistant_meta``,
    ``_build_subagent_summary_message``,
    ``_insert_subagent_summary_messages_per_round``) route through this
    helper — the composite-key change is transparent to their callers.
    """
    entries: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for key, value in blocks.items():
        if isinstance(key, tuple) and len(key) == 2 and isinstance(value, dict):
            entries.append((key, value))
        # Any other key type (``"_alias_by_index"`` sentinel, or an
        # unexpected write) is skipped — the alias must never be
        # serialized as a block.
    entries.sort(key=lambda kv: kv[0])
    return [v for _, v in entries]


def subagent_event_to_frame(
    event: dict[str, Any],
    *,
    idx: int,
    total: int,
    seq: int,
) -> StreamFrame | None:
    """Translate one sub-agent dict event into a typed ``SUBAGENT_*`` frame.

    Pure mapping extracted from
    :meth:`StreamChatUseCase._dispatch_agent_calls_streaming` (B2 cohesion
    split).  Returns ``None`` for event types the parent stream does not
    relay (the ``round`` debug field, ``subagent_tool`` with an empty
    name, or any unknown ``type``); the caller skips ``None`` results
    without advancing its sequence counter — preserving the original
    switch's behaviour byte-for-byte.
    """
    etype = event.get("type")
    if etype == "subagent_start":
        # ``subagent_id`` (optional, §3.1) — the backend now resolves the
        # sub-agent id BEFORE the start event so the UI can show the
        # open/stop affordances on the RUNNING block immediately. Absent when
        # persistence is unwired (legacy/stub parity).
        sa_id = event.get("subagent_id")
        # ``subagent_type`` + ``name`` (optional, §3.1 tail-appended, V2 UX
        # enhancement) — the resolved profile name and the LLM-supplied
        # human-readable task label so the RUNNING card renders its type
        # badge + real title immediately. Omitted when the caller did not
        # provide them (legacy / no-name / no-profile spawn).
        sa_type = event.get("subagent_type")
        sa_name = event.get("name")
        # ``round_index`` (optional, §3.1 tail-appended, V2 UX FIX) — the
        # parent agent's round number at which this sub-agent was
        # dispatched. Front-end routes SUBAGENT_START to a per-round
        # message when set (via ``ensureSubAgentMessage(tab, ctx, ri)``);
        # without it two sub-agents spawned in different rounds of the
        # same parent turn (A in round 0, B in round 1) collapse onto
        # the SAME message and B's ``index=0`` de-dup drops A. Same
        # ``round`` field name as ``subagent_output`` / ``subagent_tool``
        # for symmetry.
        sa_round = event.get("round")
        return StreamFrame.subagent_start(
            frame_id=f"sa-st-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            total=int(event.get("total", total)),
            prompt_preview=str(event.get("prompt_preview", "")),
            subagent_id=sa_id if isinstance(sa_id, str) and sa_id else None,
            subagent_type=(
                sa_type if isinstance(sa_type, str) and sa_type else None
            ),
            name=sa_name if isinstance(sa_name, str) and sa_name else None,
            round_index=(
                int(sa_round)
                if isinstance(sa_round, int) and not isinstance(sa_round, bool)
                else None
            ),
        )
    if etype == "subagent_output":
        content = event.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        _round = event.get("round")
        return StreamFrame.subagent_output(
            frame_id=f"sa-out-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            content=content,
            round_index=int(_round) if isinstance(_round, int) else None,
        )
    if etype == "subagent_tool":
        tname = event.get("tool_name", "")
        if not isinstance(tname, str) or not tname:
            return None
        targs = event.get("tool_args") or {}
        if not isinstance(targs, dict):
            targs = {}
        tcid = event.get("tool_call_id")
        _round = event.get("round")
        return StreamFrame.subagent_tool(
            frame_id=f"sa-tool-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            tool_name=tname,
            tool_args=targs,
            tool_call_id=tcid if isinstance(tcid, str) and tcid else None,
            round_index=int(_round) if isinstance(_round, int) else None,
        )
    if etype == "subagent_tool_result":
        # V2 enhancement: the sub-agent counterpart of the parent TOOL_RESULT
        # frame, so the UI renders a collapsible result panel under the
        # matching ``subagent_tool`` row (parity with a main-agent tool card)
        # instead of letting the model re-narrate the raw output as text.
        tname = event.get("tool_name", "")
        if not isinstance(tname, str) or not tname:
            return None
        tcid = event.get("tool_call_id")
        dur = event.get("duration_ms")
        _round = event.get("round")
        return StreamFrame.subagent_tool_result(
            frame_id=f"sa-tres-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            tool_name=tname,
            result=str(event.get("result", "")),
            ok=bool(event.get("ok", True)),
            tool_call_id=tcid if isinstance(tcid, str) and tcid else None,
            duration_ms=int(dur) if isinstance(dur, int) else None,
            round_index=int(_round) if isinstance(_round, int) else None,
        )
    if etype == "subagent_done":
        # ``subagent_id`` (optional appended, §3.1): the resumable persisted
        # session id; present only when sub-agent persistence is wired. Passed
        # through so the wire frame carries the wake/open handle.
        sa_id = event.get("subagent_id")
        return StreamFrame.subagent_done(
            frame_id=f"sa-done-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            result=str(event.get("result", "")),
            rounds=int(event.get("rounds", 0)),
            subagent_id=(
                sa_id if isinstance(sa_id, str) and sa_id else None
            ),
        )
    if etype == "subagent_error":
        msg = str(event.get("message", "unknown"))
        if not msg:
            msg = "unknown"
        return StreamFrame.subagent_error(
            frame_id=f"sa-err-{seq}",
            sequence=seq,
            index=int(event.get("index", idx)),
            message=msg,
        )
    return None


def build_assistant_meta(
    *,
    request_id: str | None,
    ttft_ms: int | None,
    turn_started_ms: int,
    now_ms: int,
    usage: dict[str, Any] | None,
    sub_agent_blocks: dict[Any, dict[str, Any]],
    tool_rounds: int | None = None,
) -> dict[str, Any] | None:
    """Assemble the V1-parity ``meta`` envelope for a finalised assistant turn.

    Persists the client-renderable extras (V1 useChat.js:_buildMsgForPersist
    :46-70) so a history reload restores them:

    * ``request_id`` — re-shows the "Prompt Snapshot" button.
    * ``perf`` — ``{ttft_ms, total_ms, input_tokens?, output_tokens?,
      tool_rounds?}`` so the perf line survives reload.  The fine-grained
      tok/sec rates (``input_tps`` / ``output_tps``) are derived from raw
      values by the frontend historyMapper on reload (V1 useChat.js:2377).
    * ``subAgentBlocks`` — the sorted-by-index fold blocks.

    Returns ``None`` when the turn carried no extras (so ``meta_json`` stays
    NULL for a plain text turn).
    """
    meta: dict[str, Any] = {}
    if request_id:
        meta["request_id"] = request_id

    perf: dict[str, Any] = {"total_ms": max(0, now_ms - turn_started_ms)}
    if ttft_ms is not None:
        perf["ttft_ms"] = max(0, ttft_ms)
    if isinstance(usage, dict):
        _in = usage.get("prompt_tokens")
        _out = usage.get("completion_tokens")
        if isinstance(_in, int):
            perf["input_tokens"] = _in
        if isinstance(_out, int):
            perf["output_tokens"] = _out
    if isinstance(tool_rounds, int) and tool_rounds > 0:
        perf["tool_rounds"] = tool_rounds
    meta["perf"] = perf

    if sub_agent_blocks:
        # SUBAGENT-DISPATCH-KEY: iterate blocks via the composite-key-aware
        # helper (skips the ``"_alias_by_index"`` sentinel and sorts by
        # dispatch order — ``(parent_round_index, index)``). For a
        # single-dispatch turn this yields the same order the previous
        # ``sorted(sub_agent_blocks)`` did (all keys share round -1 or R,
        # so intra-index order wins). Multi-dispatch turns render in
        # (round, index) dispatch order — the P1 fix bar.
        _blocks_list = iter_subagent_blocks(sub_agent_blocks)
        if _blocks_list:
            meta["subAgentBlocks"] = _blocks_list

    return meta or None
