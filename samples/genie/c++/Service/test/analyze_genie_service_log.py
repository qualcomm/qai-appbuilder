#!/usr/bin/env python
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""analyze_genie_service_log.py —— 事后解析 *.genie_service.log，输出逐请求膨胀/前缀曲线。

口径、字段来源与已验证数字见同名文档 analyze_genie_service_log.md。

用法:
    python analyze_genie_service_log.py <run_dir|log_file> [...] [--json out.json] [--csv out.csv]
"""

import argparse
import json
import re
import sys
from pathlib import Path

_RE_BUDGET = re.compile(
    r"\[PreFilter\] Budget: sys_prompt=(\d+) tok, msg_history=(\d+) tok, "
    r"used=(\d+) tok, context=(\d+) tok \((\d+)% used\)")
_RE_PREFILTER_FINAL = re.compile(
    r"\[PreFilter\] Final: msg_history=(\d+) tok, used=(\d+)/(\d+) tok "
    r"\((\d+)% used\), msgs=(\d+)")
_RE_EVICTION = re.compile(
    r"\[PreFilter\] Eviction: sticky=(\d+), dropped=(\d+), boundary=(-?\d+), "
    r"tokens=(\d+), reset_reason=(\S+)")
_RE_TOOL_ENTRY_CAP = re.compile(
    r"\[PreFilter\] Tool entry cap: capped=(\d+), cap=(\d+) tok, tokens=(\d+)")
_RE_SURVIVOR_FLOOR = re.compile(r"\[PreFilter\] Survivor floor hit:")
_RE_FIT_OK = re.compile(
    r"\[FitMessagesToContext\] Success - Messages: (\d+), Tokens: (\d+), "
    r"Available: (-?\d+), Dropped: (\d+)")
_RE_FIT_TRUNC = re.compile(r"\[FitMessagesToContext\] Phase \d+: truncated")
_RE_FIT_EMERGENCY = re.compile(
    r"\[FitMessagesToContext\] Phase \d+: Emergency truncation check: "
    r"overflow=(\d+) tok, safety_margin=(\d+) tok, tokens_to_free=(\d+) tok, "
    r"candidates=(\d+), largest=(\d+) tok \(max_ratio=([\d.]+)\)")
_RE_FIT_EMERGENCY_OK = re.compile(
    r"\[FitMessagesToContext\] Phase \d+: Emergency truncation succeeded "
    r"\(targets=(\d+)\(tool=(\d+), asst=(\d+)\), freed=(\d+) tok, largest=(\d+) tok\)")
_RE_FIT_PROTECTED = re.compile(r"\[FitMessagesToContext\] All remaining messages are protected")
_RE_FIT_GIVEUP = re.compile(r"Cannot compress further")
_RE_SKILLS = re.compile(
    r"\[AssignSkillDetailLevels\] (\d+) candidate\(s\) -> (\d+) kept "
    r"\(L2=(\d+), L1=(\d+), L0=(\d+), zero_score_l0=(\d+), empty_keywords=(\d+), "
    r"dropped=(\d+)\) within skills_token_budget=(\d+) \(used=(\d+)\)")
_RE_KEYWORDS = re.compile(
    r"\[BuildRelevanceKeywords\] Extracted (\d+) keyword\(s\) from recent_window=(\d+) "
    r"user message\(s\) \(window_users=(\d+), anchor=(\d+)\)")
_RE_KEYWORDS_ALIAS = re.compile(
    r"\[BuildRelevanceKeywords\] intent alias expansion: (\d+) -> (\d+) keyword\(s\)")
_RE_TOOLS = re.compile(
    r"\[FilterToolsByRelevance\] (\d+) candidate\(s\) -> (\d+) kept within "
    r"token_budget=(\d+) \(scored=(\d+), protected=(\d+)/(\d+)\)")
_RE_FINAL_PROMPT = re.compile(
    r"\[Normal\] Final prompt - Tokens: (\d+), (Available|Exceeded): (\d+), Length: (\d+) chars")
_RE_PREFIX = re.compile(
    r"\[LLAMACpp\] \[(\w+)\] Prefix cache: enabled=(\d+) \| reused_tokens=(\d+) \| "
    r"checkpoint_at=(-?\d+) \| prompt_tokens=(\d+)")
_RE_QUERY_DONE = re.compile(
    r"\[LLAMACpp\] \[(\w+)\] Query DONE \| total_ms=(\d+) \| prefill_tokens=(\d+) \| "
    r"gen_tokens=(\d+) \| gen_rate=([\d.]+) tok/s")
_RE_DRAFT = re.compile(r"draft_proposed=(\d+) \| draft_accepted=(\d+)")
_RE_PREFIX_RESTORE_FAIL = re.compile(r"Prefix cache restore failed")
_RE_MEMO_CONFIG = re.compile(
    r"\[TaskMemo\] Config loaded: enabled=(\d+), model_layer_enabled=(\d+), "
    r"token_budget_ratio=([\d.]+)")
_RE_MODEL_CTX = re.compile(
    r"Prompt tokens: (\d+), Context size: (\d+), Max output tokens: (\d+)")
_RE_DUMP_OMITTED = re.compile(r"chars omitted")
_RE_RETRY_CAP = re.compile(r"Tool call retries exceeded maximum \((\d+)\)")
_RE_LOCAL_OVERFLOW = re.compile(r"local_input_overflow")

_MEMO_HEADING = "## Task Memo"
_MEMO_TERMINATORS = ("# Tools", "<|im_end|>", "<|im_start|>", "<tool_response>")
_MEMO_PAGE_SECTION = "Evicted message pages"
_RE_MEMO_PAGE_ITEM = re.compile(r"^\s*- page \d+ \(")
_RE_MEMO_FAILURE_NOTE = re.compile(r"the last \d+ tool result\(s\) came back empty or failed")

_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F), (0xFF00, 0xFFEF))
_CJK_BYTES_PER_TOKEN = 3.0
_ASCII_BYTES_PER_TOKEN = 4.0


def estimate_cjk_aware_tokens(text):
    cjk_bytes = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _CJK_RANGES):
            cjk_bytes += n
    total_bytes = len(text.encode("utf-8"))
    other_bytes = total_bytes - cjk_bytes
    return (total_bytes,
            cjk_bytes / _CJK_BYTES_PER_TOKEN + other_bytes / _ASCII_BYTES_PER_TOKEN)


def _new_request(index):
    return {
        "req": index,
        "sys_prompt_tok": None, "msg_history_tok": None, "used_tok": None,
        "context_tok": None, "used_pct": None,
        "final_msg_history_tok": None, "final_used_tok": None, "final_pct": None,
        "final_msgs": None,
        "evictions": [], "tool_entry_caps": [], "survivor_floor_hits": 0,
        "fit_messages": None, "fit_tokens": None, "fit_available": None,
        "fit_dropped": None, "fit_truncations": 0, "fit_emergency": [],
        "fit_all_protected": 0, "fit_cannot_compress": 0,
        "fit_emergency_freed_tok": 0, "fit_emergency_ok": 0,
        "fit_emergency_largest_tok": 0, "keywords_calls": 0,
        "skill_candidates": None, "skill_kept": None, "skill_l2": None,
        "skill_l1": None, "skill_l0": None, "skill_zero_score_l0": None,
        "skill_empty_keywords": None, "skill_dropped": None,
        "skills_token_budget": None, "skills_used_tok": None,
        "keywords": None, "keywords_before_alias": None, "keywords_after_alias": None,
        "recent_window": None, "window_users": None, "anchor": None,
        "tool_candidates": None, "tool_kept": None, "tools_token_budget": None,
        "tools_scored": None, "tools_protected": None, "tools_protected_total": None,
        "final_prompt_tok": None, "final_prompt_available": None,
        "final_prompt_exceeded": None, "final_prompt_chars": None,
        "prefix_enabled": None, "reused_tokens": None, "checkpoint_at": None,
        "prompt_tokens": None, "prefix_restore_failed": 0,
        "total_ms": None, "prefill_tokens": None, "gen_tokens": None,
        "gen_rate": None, "draft_proposed": None, "draft_accepted": None,
        "sub_queries": 0, "sub_prefill_tokens": 0, "sub_gen_tokens": 0,
        "sub_total_ms": 0,
        "memo_chars": None, "memo_bytes": None, "memo_lines": None,
        "memo_tok_est": None, "memo_dump_truncated": None, "memo_bound": None,
        "model_context_size": None, "max_output_tokens": None,
    }


def _slice_task_memo(lines, start):
    body = []
    truncated = False
    page_anchor = -1
    for offset, ln in enumerate(lines[start + 1:]):
        stripped = ln.strip()
        if any(stripped == t or stripped.startswith(t) for t in _MEMO_TERMINATORS):
            break
        if _RE_DUMP_OMITTED.search(ln):
            truncated = True
        body.append(ln)
        if stripped.startswith(_MEMO_PAGE_SECTION):
            page_anchor = offset
    if page_anchor >= 0:
        end = page_anchor + 1
        while end < len(body) and _RE_MEMO_PAGE_ITEM.match(body[end]):
            end += 1
        body = body[:end]
        truncated = any(_RE_DUMP_OMITTED.search(x) for x in body)
        return body, truncated, "page_anchor"
    return body, truncated, "terminator"


def parse_log(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    requests = []
    cur = None
    memo_orphans = []
    meta = {
        "log": str(path), "lines": len(lines),
        "memo_config": None, "retry_cap": None,
        "local_input_overflow": 0, "cannot_compress_further": 0,
        "memo_headings": 0,
    }
    memo_orphan_reasons = {}

    for i, ln in enumerate(lines):
        m = _RE_MEMO_CONFIG.search(ln)
        if m and meta["memo_config"] is None:
            meta["memo_config"] = {
                "enabled": int(m.group(1)), "model_layer_enabled": int(m.group(2)),
                "token_budget_ratio": float(m.group(3))}
            continue

        m = _RE_RETRY_CAP.search(ln)
        if m:
            meta["retry_cap"] = int(m.group(1))
        if _RE_LOCAL_OVERFLOW.search(ln):
            meta["local_input_overflow"] += 1
        if _RE_FIT_GIVEUP.search(ln):
            meta["cannot_compress_further"] += 1

        if _MEMO_HEADING in ln and ln.strip().startswith(_MEMO_HEADING):
            meta["memo_headings"] += 1
            body, truncated, bound = _slice_task_memo(lines, i)
            text_body = "\n".join(body)
            memo_bytes, memo_tok = estimate_cjk_aware_tokens(text_body)
            memo = {
                "memo_chars": len(text_body),
                "memo_bytes": memo_bytes,
                "memo_lines": len(body),
                "memo_tok_est": int(round(memo_tok)),
                "memo_bound": bound,
                "memo_dump_truncated": truncated}
            if cur is not None and cur["memo_chars"] is None:
                cur.update(memo)
            else:
                if cur is None:
                    reason = "no_request"
                elif any(_RE_MEMO_FAILURE_NOTE.search(x) for x in body):
                    reason = "failure_note_echo"
                else:
                    reason = "duplicate_in_request"
                key = "%s/%s" % (reason, bound)
                memo_orphan_reasons[key] = memo_orphan_reasons.get(key, 0) + 1
                memo_orphans.append(memo)
            continue

        m = _RE_BUDGET.search(ln)
        if m:
            cur = _new_request(len(requests) + 1)
            requests.append(cur)
            cur["sys_prompt_tok"] = int(m.group(1))
            cur["msg_history_tok"] = int(m.group(2))
            cur["used_tok"] = int(m.group(3))
            cur["context_tok"] = int(m.group(4))
            cur["used_pct"] = int(m.group(5))
            continue

        if cur is None:
            continue

        m = _RE_MODEL_CTX.search(ln)
        if m:
            cur["model_context_size"] = int(m.group(2))
            cur["max_output_tokens"] = int(m.group(3))
            continue

        m = _RE_PREFILTER_FINAL.search(ln)
        if m:
            cur["final_msg_history_tok"] = int(m.group(1))
            cur["final_used_tok"] = int(m.group(2))
            cur["final_pct"] = int(m.group(4))
            cur["final_msgs"] = int(m.group(5))
            continue

        m = _RE_EVICTION.search(ln)
        if m:
            cur["evictions"].append({
                "sticky": int(m.group(1)), "dropped": int(m.group(2)),
                "boundary": int(m.group(3)), "tokens": int(m.group(4)),
                "reset_reason": m.group(5)})
            continue

        m = _RE_TOOL_ENTRY_CAP.search(ln)
        if m:
            cur["tool_entry_caps"].append({
                "capped": int(m.group(1)), "cap": int(m.group(2)),
                "tokens": int(m.group(3))})
            continue

        if _RE_SURVIVOR_FLOOR.search(ln):
            cur["survivor_floor_hits"] += 1
            continue

        m = _RE_FIT_OK.search(ln)
        if m:
            cur["fit_messages"] = int(m.group(1))
            cur["fit_tokens"] = int(m.group(2))
            cur["fit_available"] = int(m.group(3))
            cur["fit_dropped"] = int(m.group(4))
            continue

        if _RE_FIT_TRUNC.search(ln):
            cur["fit_truncations"] += 1
            continue

        m = _RE_FIT_EMERGENCY.search(ln)
        if m:
            cur["fit_emergency"].append({
                "overflow": int(m.group(1)), "safety_margin": int(m.group(2)),
                "tokens_to_free": int(m.group(3)), "candidates": int(m.group(4)),
                "largest": int(m.group(5)), "max_ratio": float(m.group(6))})
            continue

        m = _RE_FIT_EMERGENCY_OK.search(ln)
        if m:
            cur["fit_emergency_ok"] += 1
            cur["fit_emergency_freed_tok"] += int(m.group(4))
            cur["fit_emergency_largest_tok"] = max(
                cur["fit_emergency_largest_tok"], int(m.group(5)))
            continue

        if _RE_FIT_PROTECTED.search(ln):
            cur["fit_all_protected"] += 1
            continue

        if _RE_FIT_GIVEUP.search(ln):
            cur["fit_cannot_compress"] += 1
            continue

        m = _RE_SKILLS.search(ln)
        if m:
            g = [int(x) for x in m.groups()]
            (cur["skill_candidates"], cur["skill_kept"], cur["skill_l2"], cur["skill_l1"],
             cur["skill_l0"], cur["skill_zero_score_l0"], cur["skill_empty_keywords"],
             cur["skill_dropped"], cur["skills_token_budget"], cur["skills_used_tok"]) = g
            continue

        m = _RE_KEYWORDS.search(ln)
        if m:
            cur["keywords"] = int(m.group(1))
            cur["keywords_calls"] += 1
            cur["recent_window"] = int(m.group(2))
            cur["window_users"] = int(m.group(3))
            cur["anchor"] = int(m.group(4))
            continue

        m = _RE_KEYWORDS_ALIAS.search(ln)
        if m:
            cur["keywords_before_alias"] = int(m.group(1))
            cur["keywords_after_alias"] = int(m.group(2))
            continue

        m = _RE_TOOLS.search(ln)
        if m:
            cur["tool_candidates"] = int(m.group(1))
            cur["tool_kept"] = int(m.group(2))
            cur["tools_token_budget"] = int(m.group(3))
            cur["tools_scored"] = int(m.group(4))
            cur["tools_protected"] = int(m.group(5))
            cur["tools_protected_total"] = int(m.group(6))
            continue

        m = _RE_FINAL_PROMPT.search(ln)
        if m:
            cur["final_prompt_tok"] = int(m.group(1))
            if m.group(2) == "Available":
                cur["final_prompt_available"] = int(m.group(3))
            else:
                cur["final_prompt_exceeded"] = int(m.group(3))
            cur["final_prompt_chars"] = int(m.group(4))
            continue

        m = _RE_PREFIX.search(ln)
        if m:
            if m.group(1).startswith("MAIN"):
                cur["prefix_enabled"] = int(m.group(2))
                cur["reused_tokens"] = int(m.group(3))
                cur["checkpoint_at"] = int(m.group(4))
                cur["prompt_tokens"] = int(m.group(5))
            continue

        if _RE_PREFIX_RESTORE_FAIL.search(ln):
            cur["prefix_restore_failed"] += 1
            continue

        m = _RE_QUERY_DONE.search(ln)
        if m:
            if m.group(1).startswith("MAIN"):
                cur["total_ms"] = int(m.group(2))
                cur["prefill_tokens"] = int(m.group(3))
                cur["gen_tokens"] = int(m.group(4))
                cur["gen_rate"] = float(m.group(5))
                d = _RE_DRAFT.search(ln)
                if d:
                    cur["draft_proposed"] = int(d.group(1))
                    cur["draft_accepted"] = int(d.group(2))
            else:
                cur["sub_queries"] += 1
                cur["sub_total_ms"] += int(m.group(2))
                cur["sub_prefill_tokens"] += int(m.group(3))
                cur["sub_gen_tokens"] += int(m.group(4))
            continue

    meta["memo_blocks_unmatched"] = len(memo_orphans)
    meta["memo_orphan_reasons"] = memo_orphan_reasons
    _derive(requests, meta)
    return requests, meta


def _derive(requests, meta):
    model_ctx = [r["model_context_size"] for r in requests
                 if r["model_context_size"] is not None]
    meta["model_context_size"] = max(model_ctx) if model_ctx else None
    ratio = (meta["memo_config"] or {}).get("token_budget_ratio")
    meta["memo_budget_tok"] = (int(meta["model_context_size"] * ratio)
                               if meta["model_context_size"] and ratio else None)
    sys_vals = [r["sys_prompt_tok"] for r in requests if r["sys_prompt_tok"] is not None]
    baseline = min(sys_vals) if sys_vals else None
    meta["sys_prompt_static_tok"] = baseline
    for r in requests:
        ctx = r["context_tok"]
        sysp = r["sys_prompt_tok"]
        if ctx and sysp is not None:
            r["sys_prompt_pct_of_context"] = round(100.0 * sysp / ctx, 1)
            if baseline is not None:
                r["memo_tok_by_delta"] = sysp - baseline
                r["memo_pct_by_delta"] = round(100.0 * (sysp - baseline) / ctx, 1)
        if r["memo_chars"] and ctx:
            r["memo_pct_of_context"] = round(100.0 * r["memo_tok_est"] / ctx, 1)
            if meta.get("memo_budget_tok"):
                r["memo_pct_of_memo_budget"] = round(
                    100.0 * r["memo_tok_est"] / meta["memo_budget_tok"], 1)
        if r["reused_tokens"] is not None and r["prompt_tokens"]:
            r["prefix_reuse_pct"] = round(100.0 * r["reused_tokens"] / r["prompt_tokens"], 1)
        if r["total_ms"] and r["gen_tokens"]:
            r["decode_tok_s_wall"] = round(r["gen_tokens"] / (r["total_ms"] / 1000.0), 2)

    meta["main_requests"] = len(requests)
    meta["main_queries_done"] = sum(1 for r in requests if r["total_ms"] is not None)
    meta["main_prefill_tokens"] = sum(r["prefill_tokens"] or 0 for r in requests)
    meta["main_gen_tokens"] = sum(r["gen_tokens"] or 0 for r in requests)
    meta["main_total_ms"] = sum(r["total_ms"] or 0 for r in requests)
    meta["sub_queries"] = sum(r["sub_queries"] for r in requests)
    meta["sub_prefill_tokens"] = sum(r["sub_prefill_tokens"] for r in requests)
    meta["sub_gen_tokens"] = sum(r["sub_gen_tokens"] for r in requests)
    pct = [r["sys_prompt_pct_of_context"] for r in requests
           if r.get("sys_prompt_pct_of_context") is not None]
    meta["sys_prompt_pct_max"] = max(pct) if pct else None
    memo = [r["memo_pct_by_delta"] for r in requests if r.get("memo_pct_by_delta") is not None]
    meta["memo_pct_by_delta_max"] = max(memo) if memo else None
    meta["memo_pct_monotonic"] = (
        all(b >= a for a, b in zip(memo, memo[1:])) if len(memo) > 1 else None)
    memo_est = [r["memo_pct_of_context"] for r in requests
                if r.get("memo_pct_of_context") is not None]
    meta["memo_samples"] = len(memo_est)
    meta["memo_pct_of_context_max"] = max(memo_est) if memo_est else None
    meta["memo_pct_of_context_last"] = memo_est[-1] if memo_est else None
    meta["memo_pct_of_context_monotonic"] = (
        all(b >= a for a, b in zip(memo_est, memo_est[1:])) if len(memo_est) > 1 else None)
    meta["memo_dump_truncated_requests"] = sum(
        1 for r in requests if r.get("memo_dump_truncated"))
    meta["memo_bound_page_anchor"] = sum(
        1 for r in requests if r.get("memo_bound") == "page_anchor")
    meta["memo_bound_terminator"] = sum(
        1 for r in requests if r.get("memo_bound") == "terminator")
    reuse = [r["prefix_reuse_pct"] for r in requests if r.get("prefix_reuse_pct") is not None]
    meta["prefix_samples"] = len(reuse)
    meta["prefix_reuse_pct_avg"] = round(sum(reuse) / len(reuse), 1) if reuse else None
    meta["prefix_zero_reuse_requests"] = sum(
        1 for r in requests if r.get("reused_tokens") == 0)
    rates = [r["gen_rate"] for r in requests if r.get("gen_rate") is not None]
    meta["gen_rate_avg"] = round(sum(rates) / len(rates), 2) if rates else None
    meta["gen_rate_min"] = min(rates) if rates else None
    meta["gen_rate_max"] = max(rates) if rates else None


_TABLE_COLUMNS = (
    ("req", "req"),
    ("sys_prompt_tok", "sys_tok"),
    ("sys_prompt_pct_of_context", "sys%"),
    ("memo_tok_by_delta", "memo_d"),
    ("memo_pct_by_delta", "memo_d%"),
    ("memo_tok_est", "memo_tok"),
    ("memo_pct_of_context", "memo%"),
    ("memo_pct_of_memo_budget", "memo/bgt%"),
    ("memo_bound", "memo_bound"),
    ("msg_history_tok", "hist_tok"),
    ("used_tok", "used"),
    ("context_tok", "ctx"),
    ("final_prompt_tok", "fp_tok"),
    ("fit_tokens", "fit_tok"),
    ("fit_dropped", "fit_drop"),
    ("fit_truncations", "trunc"),
    ("skills_used_tok", "skl_used"),
    ("skills_token_budget", "skl_budget"),
    ("skill_l2", "L2"),
    ("skill_l1", "L1"),
    ("skill_l0", "L0"),
    ("keywords", "kw"),
    ("keywords_after_alias", "kw_alias"),
    ("tool_kept", "tools"),
    ("tools_token_budget", "tool_budget"),
    ("reused_tokens", "reused"),
    ("prompt_tokens", "pt"),
    ("prefix_reuse_pct", "reuse%"),
    ("prefill_tokens", "prefill"),
    ("gen_tokens", "gen"),
    ("gen_rate", "tok/s"),
    ("total_ms", "ms"),
)


def _fmt(v):
    return "-" if v is None else str(v)


def print_table(requests, stream):
    widths = [max(len(label), *(len(_fmt(r.get(key))) for r in requests)) if requests
              else len(label) for key, label in _TABLE_COLUMNS]
    header = "  ".join(label.rjust(w) for (_, label), w in zip(_TABLE_COLUMNS, widths))
    stream.write(header + "\n")
    stream.write("  ".join("-" * w for w in widths) + "\n")
    for r in requests:
        stream.write("  ".join(_fmt(r.get(key)).rjust(w)
                               for (key, _), w in zip(_TABLE_COLUMNS, widths)) + "\n")


def print_summary(meta, stream):
    stream.write("\n== summary ==\n")
    for k in ("log", "lines", "main_requests", "main_queries_done", "sub_queries",
              "main_prefill_tokens", "main_gen_tokens", "main_total_ms",
              "sub_prefill_tokens", "sub_gen_tokens",
              "model_context_size", "memo_budget_tok",
              "sys_prompt_static_tok", "sys_prompt_pct_max",
              "memo_pct_by_delta_max", "memo_pct_monotonic",
              "memo_samples", "memo_pct_of_context_max", "memo_pct_of_context_last",
              "memo_pct_of_context_monotonic", "memo_dump_truncated_requests",
              "memo_bound_page_anchor", "memo_bound_terminator",
              "memo_headings", "memo_blocks_unmatched", "memo_orphan_reasons",
              "prefix_samples", "prefix_reuse_pct_avg", "prefix_zero_reuse_requests",
              "gen_rate_avg", "gen_rate_min", "gen_rate_max",
              "memo_config", "retry_cap", "local_input_overflow",
              "cannot_compress_further"):
        stream.write("%-28s %s\n" % (k, meta.get(k)))


def collect_logs(targets):
    logs = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            logs.extend(sorted(p.rglob("*.genie_service.log")))
        elif p.is_file():
            logs.append(p)
        else:
            print("not found: %s" % t, file=sys.stderr)
    return logs


def main():
    parser = argparse.ArgumentParser(
        description="解析 *.genie_service.log，输出逐请求膨胀/前缀/Task Memo 曲线")
    parser.add_argument("targets", nargs="+", help="运行目录或 *.genie_service.log 文件")
    parser.add_argument("--json", default=None, help="把逐请求记录与汇总写入该 JSON 文件")
    parser.add_argument("--csv", default=None, help="把逐请求表格写入该 CSV 文件")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不打印逐请求表格")
    args = parser.parse_args()

    logs = collect_logs(args.targets)
    if not logs:
        return 2

    out = sys.stdout
    try:
        out.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    payload = []
    for log in logs:
        requests, meta = parse_log(log)
        out.write("\n=== %s ===\n" % log)
        if not args.quiet:
            print_table(requests, out)
        print_summary(meta, out)
        payload.append({"meta": meta, "requests": requests})

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["log"] + [key for key, _ in _TABLE_COLUMNS])
            for item in payload:
                for r in item["requests"]:
                    w.writerow([item["meta"]["log"]]
                               + [r.get(key) for key, _ in _TABLE_COLUMNS])
    return 0


if __name__ == "__main__":
    sys.exit(main())
