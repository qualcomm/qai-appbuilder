#!/usr/bin/env python
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""analyze_builder_transcript.py —— 从 conversation_transcript_*.md 反解帧并分类工具失败。

transcript 的「非常规帧」JSON 块是"某个工具被调了几次、某个文件被读了几次"的唯一权威源；
工具调用成败只认 Builder 自带的 [tool_error] / [exit code: N] 标记。口径与反面教训见同名
文档 analyze_builder_transcript.md。

用法:
    python analyze_builder_transcript.py <run_dir|transcript.md> [...] [--all-calls] [--json out.json]
"""

import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

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


def print_report(diag, rows, stats, fails, runs, paths, repeats, args, out):
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


def main():
    parser = argparse.ArgumentParser(
        description="反解 transcript 帧并按 [tool_error]/[exit code: N] 分类工具调用失败")
    parser.add_argument("targets", nargs="+",
                        help="运行目录或 conversation_transcript_*.md 文件")
    parser.add_argument("--all-calls", action="store_true", help="打印全部调用，不只失败的")
    parser.add_argument("--max-chars", type=int, default=1400, help="单条命令/输出的截断长度")
    parser.add_argument("--json", default=None, help="把逐调用记录写入该 JSON 文件")
    parser.add_argument("--out", default=None, help="把报告写入该文件，默认写 stdout")
    args = parser.parse_args()

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
        print_report(diag, rows, stats, fails, runs, paths, repeats, args, out)
        out.write("\n")
        payload.append({"diag": diag, "stats": stats, "rows": rows,
                        "consecutive_fail_runs": runs, "read_targets": paths})

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
