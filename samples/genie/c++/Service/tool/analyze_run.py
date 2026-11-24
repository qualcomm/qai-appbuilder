#!/usr/bin/env python
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""analyze_run.py —— 事后分析工具合集（日志 / 前缀 KV 复用 / Transcript / 请求追踪）。

子命令:
  log         解析 *.genie_service.log，汇总提示词膨胀/前缀/Task Memo 指标
  prefix      前缀 KV 复用的跨请求判据：连续性匹配、零复用形态、告警行
  transcript  从 transcript 帧提取 [tool_error]/[exit code: N] 分类工具调用失败
  trace       分析 GenieAPIService 请求追踪 JSONL（debug.request_trace 产物）

用法:
  python tool/analyze_run.py <子命令> [参数...]
  python tool/analyze_run.py <子命令> --help
"""

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sys

# =============================================================================
# 1. 服务端日志分析（原 analyze_genie_service_log.py）
# =============================================================================
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




def main_log(argv=None):
    parser = argparse.ArgumentParser(
        description="解析 *.genie_service.log，输出逐请求膨胀/前缀/Task Memo 曲线")
    parser.add_argument("targets", nargs="+", help="运行目录或 *.genie_service.log 文件")
    parser.add_argument("--json", default=None, help="把逐请求记录与汇总写入该 JSON 文件")
    parser.add_argument("--csv", default=None, help="把逐请求表格写入该 CSV 文件")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不打印逐请求表格")
    args = parser.parse_args(argv)

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




# =============================================================================
# 2. 前缀 KV 复用分析（原 analyze_prefix_reuse.py）
# =============================================================================
_WARN_KEYS = (
    "Prefix cache restore failed", "Prefix checkpoint save failed",
    "Prefix cache miss", "Context overflow", "llama_decode FAILED",
    "Cannot compress further", "local_input_overflow", "Prefill aborted",
    "connection broken", "EOG token detected")

_MISS_KEY = "Prefix cache miss"


def scan_lines(path):
    warnings = []
    misses = []
    index = 0
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, ln in enumerate(fh, 1):
            if _RE_BUDGET.search(ln):
                index += 1
                continue
            stripped = ln.strip()
            for key in _WARN_KEYS:
                if key in ln:
                    warnings.append({"req": index, "line": lineno, "key": key,
                                     "text": stripped[:400]})
            if _MISS_KEY in ln:
                misses.append({"req": index, "line": lineno, "text": stripped})
    return warnings, misses


def build_chain(requests):
    rows = []
    prev_ckpt = None
    for r in requests:
        reused = r.get("reused_tokens")
        ckpt = r.get("checkpoint_at")
        prompt = r.get("prompt_tokens")
        prefill = r.get("prefill_tokens")
        row = {
            "req": r["req"], "prompt_tokens": prompt, "reused_tokens": reused,
            "checkpoint_at": ckpt, "prev_checkpoint_at": prev_ckpt,
            "reuse_pct": r.get("prefix_reuse_pct"),
            "chain": None, "incremental": False, "identity": None,
        }
        if reused is not None and prev_ckpt is not None:
            row["chain"] = (reused == prev_ckpt)
            row["incremental"] = (0 < reused < prev_ckpt)
        if reused is not None and prefill is not None and prompt:
            row["identity"] = (reused + prefill == prompt - 1)
        rows.append(row)
        if ckpt is not None:
            prev_ckpt = ckpt
    return rows


def print_prefix_report(log, requests, meta, rows, warnings, misses, out):
    out.write("log = %s\n" % log)
    out.write("requests = %s | prefix_samples = %s | reuse%%_avg = %s | zero_reuse = %s\n\n" % (
        meta["main_requests"], meta.get("prefix_samples"),
        meta.get("prefix_reuse_pct_avg"), meta.get("prefix_zero_reuse_requests")))

    header = ("%3s %7s %7s %7s %9s %6s %7s %9s" % (
        "req", "pt", "reused", "ckpt", "prev_ckpt", "chain", "reuse%", "identity"))
    out.write(header + "\n")
    out.write("-" * len(header) + "\n")
    for row in rows:
        out.write("%3s %7s %7s %7s %9s %6s %7s %9s\n" % (
            row["req"], _fmt(row["prompt_tokens"]), _fmt(row["reused_tokens"]),
            _fmt(row["checkpoint_at"]), _fmt(row["prev_checkpoint_at"]),
            {True: "YES", False: "NO", None: "-"}[row["chain"]],
            _fmt(row["reuse_pct"]),
            {True: "OK", False: "BROKEN", None: "-"}[row["identity"]]))

    chained = [r for r in rows if r["chain"] is not None]
    out.write("\n== chain: reused == prev checkpoint_at ==\n")
    out.write("holds %d / %d; broken at req %s\n" % (
        sum(1 for r in chained if r["chain"]), len(chained),
        [r["req"] for r in chained if not r["chain"]]))

    incremental = [(r["req"], r["reused_tokens"], r["prev_checkpoint_at"])
                   for r in rows if r["incremental"]]
    out.write("\n== 0 < reused < prev_ckpt (proves incremental matching) ==\n")
    out.write("%s\n" % (incremental if incremental
                        else "NONE -> all-or-nothing behavior consistent"))

    identity = [r for r in rows if r["identity"] is not None]
    out.write("\n== identity: reused + prefill == prompt_tokens - 1 ==\n")
    out.write("holds %d / %d; broken at req %s\n" % (
        sum(1 for r in identity if r["identity"]), len(identity),
        [r["req"] for r in identity if not r["identity"]]))

    out.write("\n== zero-reuse requests ==\n")
    zero = [r for r in rows if r["reused_tokens"] == 0]
    if not zero:
        out.write("NONE\n")
    for row in zero:
        prev = next((x for x in rows if x["req"] == row["req"] - 1), None)
        out.write("req%s: pt=%s ckpt=%s%s\n" % (
            row["req"], _fmt(row["prompt_tokens"]), _fmt(row["checkpoint_at"]),
            (" | prev req%s: pt=%s ckpt=%s" % (
                prev["req"], _fmt(prev["prompt_tokens"]),
                _fmt(prev["checkpoint_at"]))) if prev else " | (first request)"))

    out.write("\n== %s lines (verbatim) ==\n" % _MISS_KEY)
    if not misses:
        out.write("NONE\n")
    for m in misses:
        out.write("req%s L%s: %s\n" % (m["req"], m["line"], m["text"]))

    out.write("\n== warnings per request ==\n")
    if not warnings:
        out.write("NONE\n")
    for w in warnings:
        out.write("req%s L%s [%s] %s\n" % (w["req"], w["line"], w["key"], w["text"]))




def main_prefix(argv=None):
    parser = argparse.ArgumentParser(
        description="前缀 KV 复用的跨请求关系判据（检查点链、增量匹配、零复用形态）")
    parser.add_argument("targets", nargs="+", help="运行目录或 *.genie_service.log 文件")
    parser.add_argument("--json", default=None, help="把逐请求关系表写入该 JSON 文件")
    parser.add_argument("--out", default=None, help="把报告写入该文件，默认写 stdout")
    args = parser.parse_args(argv)

    logs = collect_logs(args.targets)
    if not logs:
        return 2

    out = sys.stdout
    if args.out:
        out = io.open(args.out, "w", encoding="utf-8")
    else:
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    payload = []
    for log in logs:
        requests, meta = parse_log(log)
        rows = build_chain(requests)
        warnings, misses = scan_lines(log)
        print_prefix_report(log, requests, meta, rows, warnings, misses, out)
        out.write("\n")
        payload.append({"log": str(log), "rows": rows, "warnings": warnings,
                        "prefix_cache_miss": misses})

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out:
        out.close()
    return 0




# =============================================================================
# 3. Transcript 失败分析（原 analyze_builder_transcript.py）
# =============================================================================
_FRAME_HEADING = "### 非常规帧 (tool_call/tool_result 等，原始 payload)"
_SECTION_RE = re.compile(r"^## (.+)$", re.M)

_RE_EXIT = re.compile(r"\[exit code:\s*(-?\d+)\]")
_RE_TOOL_ERROR = re.compile(r"\[tool_error\]")

_FAILURE_CLASSES = (
    ("powershell-quoting", re.compile(
        r"Unexpected token|ParserError|UnexpectedToken|is not recognized as (the name of )?a cmdlet|"
        r"The term '.*' is not recognized|Missing (closing|expression)|CommandNotFoundException")),
    ("bad-cli-args", re.compile(
        r"unrecognized arguments|error: argument|usage: |invalid choice|"
        r"required: |unknown option|No such option")),
    ("missing-file-or-path", re.compile(
        r"file not found|No such file|cannot find the (path|file)|does not exist|FileNotFoundError|"
        r"ObjectNotFound|Cannot find path")),
    ("qnn-toolchain-sdk", re.compile(
        r"QnnTypeMacros|QNN_TENSOR_DATA_FORMAT|qnn-model-lib-generator|VCTargetsPath|"
        r"not a supported target|windows-arm64.*target|clang|MSBuild|CMake Error|libQnn.*\.so")),
    ("python-traceback", re.compile(r"Traceback \(most recent call last\)")),
)


def collect_transcripts(targets):
    found = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            found.extend(sorted(p.rglob("conversation_transcript_*.md")))
        elif p.is_file():
            found.append(p)
        else:
            print("not found: %s" % t, file=sys.stderr)
    return found


def parse_frames(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    diag = {
        "transcript": str(path),
        "frame_headings": text.count(_FRAME_HEADING),
        "sections": [m.group(1) for m in _SECTION_RE.finditer(text)],
        "json_blocks": 0,
        "block_parse_failures": [],
    }
    frames = []
    cursor = 0
    while True:
        i = text.find(_FRAME_HEADING, cursor)
        if i < 0:
            break
        j = text.find("```json", i)
        if j < 0:
            break
        k = text.find("\n```", j + 7)
        if k < 0:
            break
        block = text[j + 7:k]
        diag["json_blocks"] += 1
        try:
            frames.extend(json.loads(block))
        except ValueError as exc:
            diag["block_parse_failures"].append({
                "offset": j, "chars": len(block), "error": repr(exc)[:200],
                "tail": block[-200:].replace("\n", "|")})
        cursor = k
    diag["frames"] = len(frames)
    diag["frame_types"] = dict(Counter(f.get("frame_type") for f in frames))
    diag["error_code_frames"] = [
        {"frame_type": f.get("frame_type"),
         "payload": {k: str(v)[:400] for k, v in (f.get("payload") or {}).items()}}
        for f in frames if "error_code" in (f.get("payload") or {})]
    return frames, diag


def build_rows(frames):
    calls = [f for f in frames if f.get("frame_type") == "tool_call"]
    results = [f for f in frames if f.get("frame_type") == "tool_result"]
    finals = [f for f in results if "size" in (f.get("payload") or {})]
    deltas = [f for f in results if "size" not in (f.get("payload") or {})]

    by_id = {}
    for f in finals:
        by_id.setdefault(f["payload"].get("tool_call_id"), []).append(f)

    rows = []
    for idx, call in enumerate(calls, 1):
        cp = call.get("payload") or {}
        matched = by_id.get(cp.get("tool_call_id")) or []
        rp = matched[0]["payload"] if matched else {}
        args = cp.get("arguments") or {}
        rows.append({
            "idx": idx,
            "tool": cp.get("tool_name") or _infer_tool(args),
            "round_index": cp.get("round_index"),
            "args": args,
            "duration_ms": rp.get("duration_ms"),
            "size": rp.get("size"),
            "truncated": rp.get("truncated"),
            "error_code": rp.get("error_code"),
            "result": rp.get("result") or "",
            "has_result": bool(matched),
        })
    stats = {
        "tool_calls": len(calls),
        "tool_results": len(results),
        "terminal_results": len(finals),
        "delta_results": len(deltas),
        "distinct_terminal_ids": len(by_id),
        "duplicate_terminal_ids": {k: len(v) for k, v in by_id.items() if len(v) > 1},
        "calls_without_result": sum(1 for r in rows if not r["has_result"]),
        "tool_histogram": dict(Counter(r["tool"] for r in rows)),
    }
    return rows, stats


def _infer_tool(args):
    for key in ("command", "cmd", "file_path", "path", "pattern"):
        if key in args:
            return "?" + key
    return None


def judge(row):
    result = row["result"] or ""
    if _RE_TOOL_ERROR.search(result):
        return "FAIL", "tool_error"
    codes = [int(c) for c in _RE_EXIT.findall(result)]
    if codes:
        nonzero = [c for c in codes if c != 0]
        if nonzero:
            return "FAIL", "exit=" + ",".join(str(c) for c in nonzero)
        return "OK", "exit=0"
    return "OK", "no-marker"


def classify(row):
    result = row["result"] or ""
    hits = [name for name, rx in _FAILURE_CLASSES if rx.search(result)]
    return hits or ["other"]


def consecutive_runs(rows, minimum=2):
    runs = []
    current = []
    for row in rows:
        if row["verdict"] == "FAIL":
            current.append(row["idx"])
        else:
            if len(current) >= minimum:
                runs.append(list(current))
            current = []
    if len(current) >= minimum:
        runs.append(list(current))
    return runs


def read_repeats(rows):
    paths = {}
    for row in rows:
        if row["tool"] != "read":
            continue
        target = row["args"].get("file_path") or row["args"].get("path")
        if not target:
            continue
        paths.setdefault(target, []).append(
            {"idx": row["idx"], "round_index": row["round_index"],
             "offset": row["args"].get("offset"), "limit": row["args"].get("limit")})
    repeats = {k: v for k, v in paths.items() if len(v) > 1}
    return paths, repeats


def _brief(value, limit):
    return re.sub(r"\s+", " ", str(value))[:limit]


def _brief_args(row, limit):
    args = row["args"]
    value = args.get("command") or args.get("cmd")
    if value is None:
        value = json.dumps(args, ensure_ascii=False)
    return _brief(value, limit)


def print_transcript_report(diag, rows, stats, fails, runs, paths, repeats, args, out):
    out.write("transcript = %s\n" % diag["transcript"])
    out.write("frame_headings=%s json_blocks=%s block_parse_failures=%s frames=%s\n" % (
        diag["frame_headings"], diag["json_blocks"],
        len(diag["block_parse_failures"]), diag["frames"]))
    out.write("frame_types = %s\n" % diag["frame_types"])
    for failure in diag["block_parse_failures"]:
        out.write("BLOCK_PARSE_FAIL %s\n" % failure)
    for frame in diag["error_code_frames"]:
        out.write("ERROR_CODE_FRAME %s\n" % json.dumps(frame, ensure_ascii=False))
    out.write("sections = %d\n" % len(diag["sections"]))
    for section in diag["sections"]:
        out.write("  SECTION %s\n" % section)
    out.write("%s\n" % stats)

    out.write("\n== verdict ==\n")
    out.write("calls=%d fail=%d ok=%d\n" % (
        len(rows), len(fails), len(rows) - len(fails)))
    out.write("seq = %s\n" % "".join(
        "F" if r["verdict"] == "FAIL" else "S" for r in rows))
    histogram = Counter()
    for row in fails:
        for name in row["classes"]:
            histogram[name] += 1
    out.write("class histogram = %s\n" % dict(histogram))
    out.write("consecutive fail runs (>=2) = %s\n" % runs)

    out.write("\n== read targets (transcript 帧是唯一权威源) ==\n")
    for target, hits in sorted(paths.items(), key=lambda kv: -len(kv[1])):
        out.write("%2d x %s %s\n" % (len(hits), target, hits))
    out.write("distinct_read_paths=%d repeated_paths=%d extra_reads=%d\n" % (
        len(paths), len(repeats), sum(len(v) - 1 for v in repeats.values())))

    out.write("\n== failure detail ==\n")
    for row in fails:
        out.write("\n### idx=%s tool=%s round=%s %s cls=%s ms=%s\n" % (
            row["idx"], row["tool"], row["round_index"], row["why"],
            "+".join(row["classes"]), row["duration_ms"]))
        out.write("ARG: %s\n" % _brief_args(row, args.max_chars))
        out.write("OUT: %s\n" % _brief(row["result"], args.max_chars))

    if args.all_calls:
        out.write("\n== all calls ==\n")
        for row in rows:
            out.write("\n--- idx=%s tool=%s round=%s verdict=%s ms=%s size=%s "
                      "truncated=%s error_code=%s\n" % (
                          row["idx"], row["tool"], row["round_index"], row["verdict"],
                          row["duration_ms"], row["size"], row["truncated"],
                          row["error_code"]))
            out.write("  ARG: %s\n" % _brief_args(row, args.max_chars))
            out.write("  OUT: %s\n" % _brief(row["result"], args.max_chars))




def main_transcript(argv=None):
    parser = argparse.ArgumentParser(
        description="反解 transcript 帧并按 [tool_error]/[exit code: N] 分类工具调用失败")
    parser.add_argument("targets", nargs="+",
                        help="运行目录或 conversation_transcript_*.md 文件")
    parser.add_argument("--all-calls", action="store_true", help="打印全部调用，不只失败的")
    parser.add_argument("--max-chars", type=int, default=1400, help="单条命令/输出的截断长度")
    parser.add_argument("--json", default=None, help="把逐调用记录写入该 JSON 文件")
    parser.add_argument("--out", default=None, help="把报告写入该文件，默认写 stdout")
    args = parser.parse_args(argv)

    transcripts = collect_transcripts(args.targets)
    if not transcripts:
        return 2

    out = sys.stdout
    if args.out:
        out = io.open(args.out, "w", encoding="utf-8")
    else:
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    payload = []
    for path in transcripts:
        frames, diag = parse_frames(path)
        rows, stats = build_rows(frames)
        fails = []
        for row in rows:
            row["verdict"], row["why"] = judge(row)
            row["classes"] = classify(row) if row["verdict"] == "FAIL" else []
            if row["verdict"] == "FAIL":
                fails.append(row)
        runs = consecutive_runs(rows)
        paths, repeats = read_repeats(rows)
        print_transcript_report(diag, rows, stats, fails, runs, paths, repeats, args, out)
        out.write("\n")
        payload.append({"diag": diag, "stats": stats, "rows": rows,
                        "consecutive_fail_runs": runs, "read_targets": paths})

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out:
        out.close()
    return 0




# =============================================================================
# 4. 请求追踪分析（原 tool/analyze_request_trace.py）
# =============================================================================
ERROR_PATTERNS = (
    "error", "exception", "traceback", "failed", "not found",
    "no such file", "permission denied", "错误", "失败", "异常",
)


def load_records(path):
    records = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[warn] line {line_no}: 无法解析为 JSON，已跳过: {e}", file=sys.stderr)
    return records


def tool_call_signature(tc):
    name = tc.get("function", {}).get("name") or tc.get("name") or ""
    args = tc.get("function", {}).get("arguments") or tc.get("arguments") or ""
    if isinstance(args, dict):
        args = json.dumps(args, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(str(args).encode("utf-8")).hexdigest()[:16]
    return f"{name}:{h}"


def extract_tool_calls(record):
    tc = record.get("tool_calls")
    if not tc:
        return []
    if isinstance(tc, list):
        return tc
    return [tc]


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def looks_like_file_arg(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return None
    if isinstance(args, dict):
        for key in ("path", "file", "filename", "file_path"):
            if key in args:
                return str(args[key])
    return None


def analyze(records):
    n = len(records)
    report = {"rounds": n}

    if n == 0:
        return report

    timestamps = [r.get("timestamp_ms") for r in records if isinstance(r.get("timestamp_ms"), int)]
    if len(timestamps) >= 2:
        report["wall_clock_ms"] = max(timestamps) - min(timestamps)
    else:
        report["wall_clock_ms"] = None

    prefill_total = 0
    decode_total = 0
    prompt_tokens_total = 0
    gen_tokens_total = 0
    per_round_ms = []
    draft_proposed_total = 0
    draft_accepted_total = 0
    has_backend_metrics = False

    for r in records:
        prefill_ms = r.get("prefill_ms")
        decode_ms = r.get("decode_ms")
        if isinstance(prefill_ms, (int, float)) and isinstance(decode_ms, (int, float)):
            has_backend_metrics = True
            prefill_total += prefill_ms
            decode_total += decode_ms
            per_round_ms.append(prefill_ms + decode_ms)
        pt = r.get("prompt_tokens")
        gt = r.get("gen_tokens")
        if isinstance(pt, (int, float)):
            prompt_tokens_total += pt
        if isinstance(gt, (int, float)):
            gen_tokens_total += gt
        dp = r.get("draft_proposed")
        da = r.get("draft_accepted")
        if isinstance(dp, (int, float)) and isinstance(da, (int, float)):
            draft_proposed_total += dp
            draft_accepted_total += da

    report["has_backend_metrics"] = has_backend_metrics
    report["prefill_ms_total"] = prefill_total
    report["decode_ms_total"] = decode_total
    time_total = prefill_total + decode_total
    report["prefill_time_ratio"] = (prefill_total / time_total) if time_total > 0 else None
    report["decode_time_ratio"] = (decode_total / time_total) if time_total > 0 else None
    report["prompt_tokens_total"] = prompt_tokens_total
    report["gen_tokens_total"] = gen_tokens_total
    token_total = prompt_tokens_total + gen_tokens_total
    report["prompt_token_ratio"] = (prompt_tokens_total / token_total) if token_total > 0 else None
    report["gen_token_ratio"] = (gen_tokens_total / token_total) if token_total > 0 else None
    report["per_round_ms"] = per_round_ms
    if per_round_ms:
        sorted_ms = sorted(per_round_ms)
        report["per_round_ms_min"] = sorted_ms[0]
        report["per_round_ms_max"] = sorted_ms[-1]
        report["per_round_ms_median"] = sorted_ms[len(sorted_ms) // 2]
    report["draft_proposed_total"] = draft_proposed_total
    report["draft_accepted_total"] = draft_accepted_total
    report["draft_acceptance_rate"] = (
        draft_accepted_total / draft_proposed_total if draft_proposed_total > 0 else None
    )

    # 重复工具调用（名称+参数哈希相同）
    call_counts = {}
    file_read_counts = {}
    retry_after_error = []
    prev_had_error = False
    prev_call_sig = None

    for idx, r in enumerate(records):
        calls = extract_tool_calls(r)
        for tc in calls:
            sig = tool_call_signature(tc)
            call_counts[sig] = call_counts.get(sig, 0) + 1

            name = tc.get("function", {}).get("name") or tc.get("name") or ""
            args = tc.get("function", {}).get("arguments") or tc.get("arguments") or ""
            if "read" in name.lower() or "open" in name.lower():
                fpath = looks_like_file_arg(args)
                if fpath:
                    file_read_counts[fpath] = file_read_counts.get(fpath, 0) + 1

            raw_output_lower = str(r.get("raw_output", "")).lower()
            had_error_here = any(p in raw_output_lower for p in ERROR_PATTERNS)
            if prev_had_error and prev_call_sig == sig:
                retry_after_error.append({"round": idx, "signature": sig})
            prev_had_error = had_error_here
            prev_call_sig = sig

    report["duplicate_tool_calls"] = {
        sig: cnt for sig, cnt in call_counts.items() if cnt > 1
    }
    report["duplicate_file_reads"] = {
        path: cnt for path, cnt in file_read_counts.items() if cnt > 1
    }
    report["retry_after_error"] = retry_after_error

    # 相邻两轮 final_prompt 公共前缀长度（估算前缀复用收益，供 step 3 参考）
    prefix_lens = []
    for i in range(1, n):
        a = records[i - 1].get("final_prompt") or ""
        b = records[i].get("final_prompt") or ""
        prefix_lens.append(common_prefix_len(a, b))
    report["adjacent_prompt_common_prefix_chars"] = prefix_lens

    return report


def render_markdown(report, trace_path):
    lines = []
    lines.append(f"# 请求追踪分析报告: {trace_path}")
    lines.append("")
    lines.append(f"- 轮数: {report.get('rounds')}")
    lines.append(f"- 总墙钟时间（首尾 timestamp 差）: {report.get('wall_clock_ms')} ms")
    lines.append(f"- 是否拿到后端性能字段: {report.get('has_backend_metrics')}")
    lines.append("")
    lines.append("## prefill / decode 耗时占比")
    lines.append(f"- prefill 总耗时: {report.get('prefill_ms_total')} ms"
                  f"（占比 {report.get('prefill_time_ratio')}）")
    lines.append(f"- decode 总耗时: {report.get('decode_ms_total')} ms"
                  f"（占比 {report.get('decode_time_ratio')}）")
    lines.append("")
    lines.append("## token 占比")
    lines.append(f"- prompt tokens 合计: {report.get('prompt_tokens_total')}"
                  f"（占比 {report.get('prompt_token_ratio')}）")
    lines.append(f"- 生成 tokens 合计: {report.get('gen_tokens_total')}"
                  f"（占比 {report.get('gen_token_ratio')}）")
    lines.append("")
    lines.append("## 每轮耗时分布（prefill_ms + decode_ms）")
    lines.append(f"- min: {report.get('per_round_ms_min')} ms, "
                  f"median: {report.get('per_round_ms_median')} ms, "
                  f"max: {report.get('per_round_ms_max')} ms")
    lines.append("")
    lines.append("## 投机解码接受率")
    lines.append(f"- proposed 合计: {report.get('draft_proposed_total')}")
    lines.append(f"- accepted 合计: {report.get('draft_accepted_total')}")
    lines.append(f"- acceptance_rate: {report.get('draft_acceptance_rate')}")
    lines.append("")
    lines.append("## 重复工具调用（名称+参数哈希相同，次数>1）")
    dup = report.get("duplicate_tool_calls") or {}
    if dup:
        for sig, cnt in dup.items():
            lines.append(f"- {sig}: {cnt} 次")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 重复读取同一文件（次数>1）")
    dfr = report.get("duplicate_file_reads") or {}
    if dfr:
        for path, cnt in dfr.items():
            lines.append(f"- {path}: {cnt} 次")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 失败后重试同一工具调用")
    retries = report.get("retry_after_error") or []
    if retries:
        for item in retries:
            lines.append(f"- round {item['round']}: {item['signature']}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 相邻两轮 final_prompt 公共前缀长度（字符数，供 step 3 前缀复用收益估算）")
    prefix_lens = report.get("adjacent_prompt_common_prefix_chars") or []
    lines.append(f"- {prefix_lens}")
    lines.append("")
    return "\n".join(lines)




def main_trace(argv=None):
    parser = argparse.ArgumentParser(
        prog="analyze_run.py trace",
        description="分析 GenieAPIService 请求追踪 JSONL（service_config.json 的 debug.request_trace 产物）")
    parser.add_argument("trace_jsonl", help="请求追踪 JSONL 文件路径")
    parser.add_argument("--out", default=None, help="markdown 报告输出路径，默认打印到 stdout")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    trace_path = Path(args.trace_jsonl)
    if not trace_path.exists():
        print(f"[error] 文件不存在: {trace_path}", file=sys.stderr)
        return 1

    records = load_records(trace_path)
    report = analyze(records)
    md = render_markdown(report, trace_path)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[ok] 报告已写入: {args.out}")
    else:
        print(md)



    return 0


# =============================================================================
# 顶层分发入口
# =============================================================================

COMMANDS = {
    "log": (main_log, "解析 *.genie_service.log，汇总提示词膨胀/前缀/Task Memo 指标"),
    "prefix": (main_prefix, "前缀 KV 复用的跨请求判据：连续性匹配、零复用形态、告警行"),
    "transcript": (main_transcript, "从 transcript 帧提取 [tool_error]/[exit code: N] 分类工具调用失败"),
    "trace": (main_trace, "分析 GenieAPIService 请求追踪 JSONL（debug.request_trace 产物）"),
}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("用法: analyze_run.py <子命令> [参数...]\n")
        print("子命令:")
        for cmd, (_, desc) in COMMANDS.items():
            print(f"  {cmd:<12} {desc}")
        print("\n查看具体子命令参数: analyze_run.py <子命令> --help")
        return 0 if argv else 2
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"未知子命令: {cmd}。可选: {', '.join(COMMANDS.keys())}", file=sys.stderr)
        return 2
    runner = COMMANDS[cmd][0]
    res = runner(argv[1:])
    return 0 if res is None else res


if __name__ == "__main__":
    sys.exit(main())
