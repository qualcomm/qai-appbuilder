#!/usr/bin/env python
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""verify_genie_service_log_parser.py —— analyze_genie_service_log.py 的解析口径自检。

对每类日志行用解析脚本自己的正则直接 grep 计数，与 parse_log() 的逐请求聚合求和比对；
改动服务端日志行文案或本脚本正则后先跑它。口径与判据边界见同名文档
verify_genie_service_log_parser.md。

用法:
    python verify_genie_service_log_parser.py <log_file|run_dir> [--baseline round3] [--out FILE]
"""

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_genie_service_log as A

_GREP = {
    "fit_ok": A._RE_FIT_OK,
    "fit_trunc": A._RE_FIT_TRUNC,
    "fit_emergency": A._RE_FIT_EMERGENCY,
    "fit_emergency_ok": A._RE_FIT_EMERGENCY_OK,
    "fit_all_protected": A._RE_FIT_PROTECTED,
    "skills": A._RE_SKILLS,
    "keywords": A._RE_KEYWORDS,
    "tools": A._RE_TOOLS,
    "final_prompt": A._RE_FINAL_PROMPT,
    "budget": A._RE_BUDGET,
    "query_done": A._RE_QUERY_DONE,
}

_DERIVED = ("final_prompt_available", "final_prompt_exceeded",
            "query_done_main", "query_done_sub")

_LAST_WINS = ("skills", "tools", "keywords", "final_prompt",
              "final_prompt_available", "final_prompt_exceeded", "fit_ok")

_AGGREGATE_KEYS = (
    "main_requests", "main_queries_done", "main_prefill_tokens",
    "main_gen_tokens", "main_total_ms", "sub_queries",
    "sub_prefill_tokens", "sub_gen_tokens", "sys_prompt_static_tok",
    "sys_prompt_pct_max", "memo_pct_by_delta_max",
    "memo_pct_of_context_max", "memo_samples", "memo_bound_page_anchor",
    "memo_bound_terminator", "memo_dump_truncated_requests",
    "prefix_samples", "prefix_reuse_pct_avg", "prefix_zero_reuse_requests",
    "gen_rate_avg", "gen_rate_min", "gen_rate_max", "model_context_size",
    "memo_budget_tok", "retry_cap", "local_input_overflow",
    "cannot_compress_further")

_BASELINES = {
    "round3": {
        "lines": {
            "fit_ok": 41, "fit_trunc": 31, "fit_emergency": 22,
            "fit_emergency_ok": 22, "fit_all_protected": 22, "skills": 42,
            "keywords": 126, "tools": 84, "final_prompt": 41,
            "final_prompt_available": 25, "final_prompt_exceeded": 16,
            "query_done": 55, "query_done_main": 41, "query_done_sub": 14,
            "budget": 42,
        },
        "meta": {
            "main_queries_done": 41, "main_requests": 42,
            "main_prefill_tokens": 500247, "main_gen_tokens": 5298,
            "sub_queries": 14, "sys_prompt_static_tok": 3553, "retry_cap": 40,
        },
        "sys_prompt_tok_max": 6177,
        "context_tok": [13108],
    },
}


def grep_counts(path):
    counts = {k: 0 for k in _GREP}
    for k in _DERIVED:
        counts[k] = 0
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            for key, rx in _GREP.items():
                m = rx.search(ln)
                if not m:
                    continue
                counts[key] += 1
                if key == "final_prompt":
                    if m.group(2) == "Available":
                        counts["final_prompt_available"] += 1
                    else:
                        counts["final_prompt_exceeded"] += 1
                elif key == "query_done":
                    if m.group(1).startswith("MAIN"):
                        counts["query_done_main"] += 1
                    else:
                        counts["query_done_sub"] += 1
    return counts


def parsed_counts(requests, meta):
    agg = {
        "fit_ok": sum(1 for r in requests if r["fit_tokens"] is not None),
        "fit_trunc": sum(r["fit_truncations"] for r in requests),
        "fit_emergency": sum(len(r["fit_emergency"]) for r in requests),
        "fit_emergency_ok": sum(r["fit_emergency_ok"] for r in requests),
        "fit_all_protected": sum(r["fit_all_protected"] for r in requests),
        "skills": sum(1 for r in requests if r["skills_used_tok"] is not None),
        "keywords": sum(r["keywords_calls"] for r in requests),
        "tools": sum(1 for r in requests if r["tool_kept"] is not None),
        "final_prompt": sum(1 for r in requests if r["final_prompt_tok"] is not None),
        "final_prompt_available": sum(
            1 for r in requests if r["final_prompt_available"] is not None),
        "final_prompt_exceeded": sum(
            1 for r in requests if r["final_prompt_exceeded"] is not None),
        "budget": meta["main_requests"],
        "query_done_main": meta["main_queries_done"],
        "query_done_sub": meta["sub_queries"],
    }
    agg["query_done"] = agg["query_done_main"] + agg["query_done_sub"]
    return agg


def compare(grep, parsed, failures, out):
    out.write("key                       grep  parsed  relation  verdict\n")
    for key in sorted(grep):
        gv, pv = grep[key], parsed[key]
        if key in _LAST_WINS:
            relation = "parsed<=grep"
            ok = pv <= gv and (pv > 0 or gv == 0)
        else:
            relation = "parsed==grep"
            ok = pv == gv
        verdict = "OK" if ok else "FAIL"
        if not ok:
            failures.append(("line:" + key, "%s %s" % (relation, gv), gv, pv))
        out.write("%-24s %6d %7d  %-12s %s\n" % (key, gv, pv, relation, verdict))


def dump_aggregates(requests, meta, out):
    out.write("\n-- aggregates --\n")
    for key in _AGGREGATE_KEYS:
        out.write("%-28s = %s\n" % (key, meta.get(key)))
    out.write("context_tok(distinct)        = %s\n" % sorted(
        {r["context_tok"] for r in requests if r["context_tok"]}))
    out.write("fit_emergency_freed_tok(sum) = %s\n" % sum(
        r["fit_emergency_freed_tok"] for r in requests))
    out.write("fit_emergency_ok(reqs>0)     = %s\n" % sum(
        1 for r in requests if r["fit_emergency_ok"] > 0))


def cross_check(name, grep, requests, meta, failures, out):
    spec = _BASELINES[name]
    out.write("\n-- baseline cross-check (%s) --\n" % name)
    for key, want in sorted(spec["lines"].items()):
        got = grep[key]
        ok = got == want
        if not ok:
            failures.append(("baseline:line:" + key, want, got, got))
        out.write("%-24s expected=%-10s grep=%-10s %s\n" % (
            key, want, got, "OK" if ok else "FAIL"))
    for key, want in sorted(spec["meta"].items()):
        got = meta.get(key)
        ok = got == want
        if not ok:
            failures.append(("baseline:meta:" + key, want, got, got))
        out.write("%-24s expected=%-10s got=%-10s %s\n" % (
            key, want, got, "OK" if ok else "FAIL"))
    sys_vals = [r["sys_prompt_tok"] for r in requests if r["sys_prompt_tok"]]
    sys_max = max(sys_vals) if sys_vals else None
    want = spec["sys_prompt_tok_max"]
    ok = sys_max == want
    if not ok:
        failures.append(("baseline:sys_prompt_tok_max", want, sys_max, sys_max))
    out.write("%-24s expected=%-10s got=%-10s %s\n" % (
        "sys_prompt_tok(max)", want, sys_max, "OK" if ok else "FAIL"))
    ctxs = sorted({r["context_tok"] for r in requests if r["context_tok"]})
    want = spec["context_tok"]
    ok = ctxs == want
    if not ok:
        failures.append(("baseline:context_tok", want, ctxs, ctxs))
    out.write("%-24s expected=%-10s got=%-10s %s\n" % (
        "context_tok(distinct)", want, ctxs, "OK" if ok else "FAIL"))


def verify(log, baseline, out):
    out.write("log = %s\n" % log)
    grep = grep_counts(log)
    requests, meta = A.parse_log(log)
    parsed = parsed_counts(requests, meta)
    failures = []
    compare(grep, parsed, failures, out)
    dump_aggregates(requests, meta, out)
    if baseline:
        cross_check(baseline, grep, requests, meta, failures, out)
    else:
        out.write("\n-- baseline cross-check --\n")
        out.write("SKIPPED: 未做基线交叉校验（未指定 --baseline，"
                  "只做了 grep 与 parse_log 聚合的一致性自检）\n")
    out.write("\nFAILURES=%d\n" % len(failures))
    for f in failures:
        out.write("FAIL %s expected=%s grep=%s parsed=%s\n" % f)
    return failures


def main():
    parser = argparse.ArgumentParser(
        description="自检 analyze_genie_service_log.py 的解析口径是否与日志行计数一致")
    parser.add_argument("targets", nargs="+", help="运行目录或 *.genie_service.log 文件")
    parser.add_argument("--baseline", choices=sorted(_BASELINES),
                        help="额外做一次真机基线交叉校验；不给则跳过，不算失败")
    parser.add_argument("--out", default=None, help="把报告写入该文件，默认写 stdout")
    args = parser.parse_args()

    logs = A.collect_logs(args.targets)
    if not logs:
        return 2

    if args.out:
        out = io.open(args.out, "w", encoding="utf-8")
    else:
        out = sys.stdout
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    total = 0
    for log in logs:
        total += len(verify(log, args.baseline, out))
        out.write("\n")
    if args.out:
        out.close()
    print("FAILURES=%d%s" % (total, " -> %s" % args.out if args.out else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
