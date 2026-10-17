#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_request_trace.py

离线分析 GenieAPIService 逐轮诊断追踪 JSONL（service_config.json 的
debug.request_trace 开启后产出），统计：
  - 轮数、总墙钟时间（按记录 timestamp_ms 的首尾差近似）
  - 每轮耗时分布（prefill_ms + decode_ms，若后端提供）
  - prefill 与 decode 的耗时占比、token 占比
  - 重复工具调用（名称 + 参数哈希相同）
  - 对同一文件的重复读取（工具名含 read/open 且参数里有路径字段）
  - 失败后重试同一命令（工具结果文本含常见错误特征）
  - 相邻两轮 final_prompt 的公共前缀长度（估算前缀复用收益，供 step 3 用）

只用 Python stdlib，不依赖第三方库。

用法：
    python tool/analyze_request_trace.py <trace.jsonl> [--out report.md]
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_jsonl", help="请求追踪 JSONL 文件路径")
    parser.add_argument("--out", default=None, help="markdown 报告输出路径，默认打印到 stdout")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    trace_path = Path(args.trace_jsonl)
    if not trace_path.exists():
        print(f"[error] 文件不存在: {trace_path}", file=sys.stderr)
        sys.exit(1)

    records = load_records(trace_path)
    report = analyze(records)
    md = render_markdown(report, trace_path)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[ok] 报告已写入: {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
