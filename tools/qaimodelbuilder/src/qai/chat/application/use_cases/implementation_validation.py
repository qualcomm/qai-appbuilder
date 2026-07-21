# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Item completion gates — verify-command (判定 B) + LLM validator (step5).

DISC-1 三期-step5 + 完成判定 B layer two OPTIONAL, independently-gated quality
gates on top of the simplified A judgement
(``done = clean kernel finish AND no tool_error``):

* **verify_command (完成判定 B)** — a per-item shell command the user configures
  (``FeatureItem.verify_command``).  After the implementing agent finishes the
  orchestrator runs it through the SHARED ai_coding ``exec`` tool channel (timeout
  / output cap / cwd clamp / denylist all reused — 细则 2 复用>重造) and inspects
  the exit code: a non-zero exit ⇒ the item ``failed``.

* **LLM validator (step5)** — an OPTIONAL low-temperature independent review (NO
  new participant role): reads the item's ``acceptance_criteria`` + the agent's
  ``result_summary`` and answers pass/fail.

This module is the **pure logic** for both gates: it does NOT call the LLM, does
NOT invoke tools, does NOT touch the orchestrator's ``_run_*`` paths.  It only
(a) builds the validator prompt text, (b) parses the validator's free-text reply
into a pass/fail verdict, and (c) interprets an exec result string into a
verify-command verdict.  The orchestrator wires these into the per-item
done/failed block (§22.4) and supplies the real LLM stream / tool invocation.

Layering: ``application/use_cases`` — stdlib only; no ports / domain / adapters /
cross-context imports (mirrors :mod:`implementation_defaults` /
:mod:`implementation_tool_policy`).

State-Truth-First (AGENTS.md §🔴): the parsers are TOTAL and defensive.  A
malformed / empty / ambiguous validator reply degrades to **pass** (the validator
is an OPTIONAL extra gate — its own infra hiccup must never flip a clean item to
failed; that would punish the agent for the reviewer's flakiness).  Conversely an
unparseable / missing exec result for a configured verify_command degrades to
**fail** (an OBJECTIVE gate the user explicitly asked for: if we cannot prove the
command passed, do not claim ``done``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "VerifyVerdict",
    "ValidatorVerdict",
    "build_validator_prompt",
    "parse_validator_reply",
    "interpret_verify_result",
    "MAX_VALIDATOR_REASON_LEN",
]

#: Hard cap on the SHORT reason we keep from a verdict (control-plane —
#: ``last_error`` is bounded; full reasoning lives in the message/log system).
MAX_VALIDATOR_REASON_LEN = 300

#: The single-line token the validator prompt asks the model to start its reply
#: with.  Case-insensitive on parse.
_PASS_TOKEN = "PASS"
_FAIL_TOKEN = "FAIL"

#: ``[exit code: N]`` marker the exec tool result renderer appends ONLY when the
#: command exited non-zero (``apps/api/_chat_tool_result_render.py``).  Its
#: presence ⇒ the verify command failed.
_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]")
#: The orchestrator's tool executor prefixes a failed/raised invocation with
#: ``[tool_error] …`` (same marker the implementation loop already uses for
#: ``had_error``).  Its presence ⇒ the command could not be run ⇒ fail.
_TOOL_ERROR_PREFIX = "[tool_error]"


@dataclass(frozen=True, slots=True)
class VerifyVerdict:
    """Outcome of running a per-item ``verify_command`` (完成判定 B)."""

    passed: bool
    #: SHORT human reason (e.g. ``"exit code 1"`` / ``"verify command timed out"``).
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ValidatorVerdict:
    """Outcome of the OPTIONAL LLM validator review (step5)."""

    passed: bool
    #: SHORT human reason taken from the validator's reply (bounded).
    reason: str = ""


def _truncate_reason(text: str) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= MAX_VALIDATOR_REASON_LEN else text[
        :MAX_VALIDATOR_REASON_LEN
    ]


def build_validator_prompt(
    *,
    title: str,
    description: str,
    acceptance_criteria: tuple[str, ...],
    result_summary: str,
) -> str:
    """Build the independent validator review prompt (step5).

    Tri-lingual-friendly, low-ceremony: gives the reviewer the item's intent +
    acceptance criteria + the implementing agent's self-reported result, and
    asks for a single-line ``PASS`` / ``FAIL`` verdict followed by a short
    reason.  All inputs are already short control-plane strings; we still cap
    each block defensively so a fat ``result_summary`` cannot blow the prompt.
    """
    crit_lines = "\n".join(
        f"  - {c.strip()}"
        for c in acceptance_criteria
        if isinstance(c, str) and c.strip()
    )
    criteria_block = (
        f"验收标准 / Acceptance criteria:\n{crit_lines}"
        if crit_lines
        else "验收标准 / Acceptance criteria: (未提供 / none provided)"
    )
    return (
        "你是一个独立的复核者，负责判断一个功能项的实施产出是否满足其验收标准。"
        "请只依据下面提供的信息客观判断，不要臆测未提供的内容。\n"
        "You are an INDEPENDENT reviewer judging whether a feature item's "
        "implementation output satisfies its acceptance criteria. Judge "
        "objectively from the information below only.\n\n"
        f"功能项 / Item: {(title or '').strip()[:200]}\n"
        f"说明 / Description: {(description or '').strip()[:800]}\n"
        f"{criteria_block}\n\n"
        "实施角色自述的产出 / The implementing agent's self-reported result:\n"
        f"{(result_summary or '').strip()[:1200]}\n\n"
        "请在回复的第一行只写 PASS 或 FAIL（全大写），随后可附一行简短理由。\n"
        "Answer with EXACTLY 'PASS' or 'FAIL' on the FIRST line (uppercase), "
        "optionally followed by one short reason line."
    )


def parse_validator_reply(reply: str | None) -> ValidatorVerdict:
    """Parse a validator free-text reply into a pass/fail verdict (step5).

    Total + defensive (State-Truth-First).  Rule:

    * An explicit ``FAIL`` token anywhere ⇒ fail (with the reply as the reason).
    * Otherwise an explicit ``PASS`` token ⇒ pass.
    * An empty / ``None`` / token-less reply ⇒ **pass** (degrade-to-pass: an
      OPTIONAL reviewer's silence must not flip a clean item to failed).

    Tokens are matched on word boundaries, case-insensitively, so "PASS"
    /"pass."/"Verdict: FAIL" all match while "passed the build" does NOT
    accidentally trip on a substring — we anchor on the bare token.
    """
    if not reply or not reply.strip():
        return ValidatorVerdict(passed=True, reason="")
    upper = reply.upper()
    has_fail = re.search(rf"\b{_FAIL_TOKEN}\b", upper) is not None
    has_pass = re.search(rf"\b{_PASS_TOKEN}\b", upper) is not None
    if has_fail:
        return ValidatorVerdict(passed=False, reason=_truncate_reason(reply))
    if has_pass:
        return ValidatorVerdict(passed=True, reason=_truncate_reason(reply))
    # No explicit verdict token — degrade to pass (do not punish the agent for
    # an unparseable reviewer reply).
    return ValidatorVerdict(passed=True, reason="")


def interpret_verify_result(
    *, ok: bool, result_text: str | None
) -> VerifyVerdict:
    """Interpret an exec-tool invocation result into a verify-command verdict.

    ``ok`` is the ``ToolInvocationResult.ok`` flag (False when the handler
    raised / the tool errored — the command could not be run).  ``result_text``
    is the rendered exec output, which (per
    ``apps/api/_chat_tool_result_render.py``) carries ``[exit code: N]`` ONLY
    when the command exited non-zero.

    Verdict rules (State-Truth-First — an objective gate the user asked for, so
    we degrade to FAIL when we cannot prove success):

    * ``ok is False`` ⇒ fail (``"verify command could not run"``).
    * a ``[tool_error] …`` result ⇒ fail.
    * a ``[exit code: N]`` marker with N != 0 ⇒ fail (``"exit code N"``).
    * otherwise ⇒ pass (a clean exec with no non-zero marker = exit 0).
    """
    if not ok:
        return VerifyVerdict(
            passed=False, reason="verify command could not run"
        )
    text = result_text or ""
    if text.lstrip().startswith(_TOOL_ERROR_PREFIX):
        return VerifyVerdict(
            passed=False, reason=_truncate_reason(text)
        )
    match = _EXIT_CODE_RE.search(text)
    if match is not None:
        try:
            code = int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover — regex guarantees int
            code = 1
        if code != 0:
            return VerifyVerdict(passed=False, reason=f"exit code {code}")
    return VerifyVerdict(passed=True, reason="")
