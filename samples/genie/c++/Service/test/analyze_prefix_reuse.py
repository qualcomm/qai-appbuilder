#!/usr/bin/env python
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""analyze_prefix_reuse.py —— GGUF 前缀 KV 复用的跨请求关系判据。

analyze_genie_service_log.py 只给逐请求的 reused_tokens/checkpoint_at/prefix_reuse_pct；
本脚本补上只有把相邻请求放在一起才能得出的判据（检查点首尾相接、增量匹配是否发生、
零复用请求的上游形态），口径与边界见同名文档 analyze_prefix_reuse.md。

用法:
    python analyze_prefix_reuse.py <log_file|run_dir> [...] [--json out.json]
"""

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_genie_service_log as A

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
            if A._RE_BUDGET.search(ln):
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


def print_report(log, requests, meta, rows, warnings, misses, out):
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
            row["req"], A._fmt(row["prompt_tokens"]), A._fmt(row["reused_tokens"]),
            A._fmt(row["checkpoint_at"]), A._fmt(row["prev_checkpoint_at"]),
            {True: "YES", False: "NO", None: "-"}[row["chain"]],
            A._fmt(row["reuse_pct"]),
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
            row["req"], A._fmt(row["prompt_tokens"]), A._fmt(row["checkpoint_at"]),
            (" | prev req%s: pt=%s ckpt=%s" % (
                prev["req"], A._fmt(prev["prompt_tokens"]),
                A._fmt(prev["checkpoint_at"]))) if prev else " | (first request)"))

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


def main():
    parser = argparse.ArgumentParser(
        description="前缀 KV 复用的跨请求关系判据（检查点链、增量匹配、零复用形态）")
    parser.add_argument("targets", nargs="+", help="运行目录或 *.genie_service.log 文件")
    parser.add_argument("--json", default=None, help="把逐请求关系表写入该 JSON 文件")
    parser.add_argument("--out", default=None, help="把报告写入该文件，默认写 stdout")
    args = parser.parse_args()

    logs = A.collect_logs(args.targets)
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
        requests, meta = A.parse_log(log)
        rows = build_chain(requests)
        warnings, misses = scan_lines(log)
        print_report(log, requests, meta, rows, warnings, misses, out)
        out.write("\n")
        payload.append({"log": str(log), "rows": rows, "warnings": warnings,
                        "prefix_cache_miss": misses})

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
