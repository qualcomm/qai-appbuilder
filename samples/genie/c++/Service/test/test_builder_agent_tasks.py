#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#=============================================================================
#
# Copyright (c) 2026, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""
test_builder_agent_tasks.py —— QAIModelBuilder + 本地模型真实多轮 agent 任务测试。

取代 test_service.py 里已过时的 QAIModelBuilderLocalModelTester / --suite
builder_local_model（该机制只验证"能不能聊天"这类单点行为，场景专属断言早已过时）。
本脚本改为驱动一个真实本地弱模型，通过 QAIModelBuilder 真实 SSE 聊天接口，完成
真实世界的多步骤任务（如模型转换、编写小程序），断言最终产物真实存在且可用——
而不是断言某个中间步骤的响应格式。

**核心架构前提**（已在上一步 explore 阶段确认，不要重新验证）：QAIModelBuilder
服务端 `SingleAgentTurnKernel.run()` 已经实现了完整的"模型调用工具 -> 工具执行 ->
结果喂回模型 -> 模型继续"闭环，全部在一次 SSE 请求生命周期内自动完成。本脚本
因此不需要在 Python 侧解析/执行 tool_calls，只需要发 prompt、把 SSE 流读到
done/error 终止帧即可。多轮 agent 任务里的"多轮"，指的是本脚本在一轮 SSE 对话
结束后（例如模型遇到 Blocking Condition 停下来问询）追加发一轮 follow-up 消息，
不是在单轮 SSE 内部自己驱动 tool_calls。

基础设施复用策略（对应 test_service.md 里记录的技术决策）：
  * `_CsrfSession`/`QAIModelBuilderManager`/`ModelDirSnapshot`/`TestResult`/
    `CrashEvent`/`infer_backend`/`detect_modality`/`discover_models`/
    `ReportGenerator`/`resolve_builder_python`/`_finalize_and_exit` 直接从
    test_service 模块 import 复用（已确认 test_service 模块级代码无副作用，
    import 是安全的：唯一的模块级动作是幂等的 stdout/stderr UTF-8 重新包装，
    argparse 解析被 `if __name__ == "__main__":` 完整守护）。
  * `configure_genie_root`/`inject_local_models`/`_wait_local_backend_ready`/
    `start_and_wait_ready`/聊天 SSE 驱动这几个方法在旧代码里是绑定在即将被删除
    的 QAIModelBuilderLocalModelTester 巨类实例状态上的方法（依赖 self.results/
    self.builder 等），本脚本不整体 import 该类（避免与其占位模型名注册机制、
    大量场景专属方法产生耦合），而是移植为本文件内接受显式参数的独立函数，
    核心 mklink /J + CSRF + SSE 解析逻辑保持与已验证实现一致。

用法示例:
    python test_builder_agent_tasks.py --task model_conversion \\
        --builder_dir ..\\third\\QAIModelBuilder \\
        --exe_dir ..\\build\\GenieService-win-arm64 \\
        --models C:\\Users\\HCKTest\\Desktop\\GenieEnv\\models \\
        --model_name qwen3-8b-8480 \\
        --small_model_path C:\\Users\\HCKTest\\Desktop\\GenieEnv\\Video_Model\\Video\\remote_npu_sanity_matmul.onnx \\
        --out_dir .\\test_results_agent_tasks

    用户指定的 inception_v3 FP16/W8A8 对比验收任务（见 run_inception_precision_compare_task()）：
    python test_builder_agent_tasks.py --task inception_precision_compare \\
        --builder_dir ..\\third\\QAIModelBuilder \\
        --exe_dir ..\\build\\GenieService-win-arm64 \\
        --models C:\\Users\\HCKTest\\Desktop\\GenieEnv\\models \\
        --model_name qwen3-8b-8480 \\
        --total_deadline_seconds 5400 \\
        --out_dir .\\test_results_agent_tasks

健康判定标准与 test_service.py 一致：failed==ignored 且 crashed==0 为健康；
退出码严格 failed==0 && crashed==0（复用 test_service._finalize_and_exit()）。
"""

import argparse
import json
import py_compile
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import (  # noqa: E402
    _CsrfSession,
    QAIModelBuilderManager,
    ModelDirSnapshot,
    TestResult,
    CrashEvent,
    infer_backend,
    detect_modality,
    discover_models,
    ReportGenerator,
    resolve_builder_python,
    cleanup_junctions,
    _finalize_and_exit,
)

import requests  # noqa: E402


# ============================================================================
# 移植自 QAIModelBuilderLocalModelTester 的通用基础设施
# （已验证的 mklink /J 注入 + CSRF + 就绪轮询机制，改写为独立函数）
# ============================================================================
def configure_genie_root(builder, genie_root_path, results, round_num=1, junction_paths=None):
    """把 GenieAPIService 安装目录通过 mklink /J 联接到 Builder 固定扫描的
    <data_dir>/bin/<name> 下，触发 Builder 官方"自动发现已安装版本"自愈机制。
    逻辑与 test_service.QAIModelBuilderLocalModelTester.configure_genie_root() 一致
    （该方法文档字符串详细记录了为什么不能走 POST /api/forge-config）。"""
    name = "AGENT-TASK: configure_genie_root (mklink /J into data_dir/bin)"
    src = Path(genie_root_path)
    if not src.is_dir() or not (src / "GenieAPIService.exe").is_file():
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0,
            detail=f"--genie_root_path 不存在或不含 GenieAPIService.exe: {src}"))
        return False
    if not builder.data_dir:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0,
            detail="Builder 未启用隔离数据目录 (QAI_DATA__DATA_DIR)，无法注入安装目录",
            skipped=True))
        return False

    bin_root = builder.data_dir / "bin"
    try:
        bin_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0, detail=f"创建 {bin_root} 失败: {e}"))
        return False

    dst = bin_root / src.name
    if junction_paths is not None:
        junction_paths.append(dst)
    if not dst.exists():
        try:
            proc = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(dst), str(src)],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            results.append(TestResult(
                name=name, round_num=round_num, model_name="_agent_task_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"mklink /J subprocess 异常: {type(e).__name__}: {e}"))
            return False
        if proc.returncode != 0:
            results.append(TestResult(
                name=name, round_num=round_num, model_name="_agent_task_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"mklink /J 失败, rc={proc.returncode}, stderr={proc.stderr!r}"))
            return False

    try:
        r = builder.csrf.get("/api/service/status", timeout=15)
    except requests.RequestException as e:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0,
            detail=f"GET /api/service/status 请求异常: {e}"))
        return False
    if r.status_code != 200:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=r.status_code, latency_ms=0,
            detail=f"GET /api/service/status 非 200: {r.text[:300]}"))
        return False
    exe_path = r.json().get("exe_path")
    passed = bool(exe_path) and (src.name.lower() in str(exe_path).lower())
    results.append(TestResult(
        name=name, round_num=round_num, model_name="_agent_task_",
        passed=passed, status_code=200, latency_ms=0,
        detail=f"mklink /J 注入完成: {dst} -> {src}; exe_path={exe_path}"))
    return passed


def inject_local_models(builder, models_root, model_dirs, results, snapshot, round_num=1, junction_paths=None):
    """用 mklink /J 把每个 models_root/<name> 联接到 <data_dir>/models/<name>，
    并对源目录关键小文件拍快照（ModelDirSnapshot，见其文档字符串）防止联接被
    Builder 安装/更新路径透明穿透误改。逻辑与
    test_service.QAIModelBuilderLocalModelTester.inject_local_models() 一致。"""
    name = "AGENT-TASK: inject_local_models (mklink /J)"
    if not builder.data_dir:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0,
            detail="Builder 未启用隔离数据目录 (QAI_DATA__DATA_DIR)，无法注入本地模型",
            skipped=True))
        return False
    target_models_root = builder.data_dir / "models"
    try:
        target_models_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        results.append(TestResult(
            name=name, round_num=round_num, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0, detail=f"创建 {target_models_root} 失败: {e}"))
        return False

    src_dirs = []
    for model_name in model_dirs:
        src = Path(models_root) / model_name
        dst = target_models_root / model_name
        src_dirs.append(src)
        if junction_paths is not None:
            junction_paths.append(dst)
        if dst.exists():
            continue
        try:
            proc = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(dst), str(src)],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            results.append(TestResult(
                name=name, round_num=round_num, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"mklink /J subprocess 异常: {type(e).__name__}: {e}"))
            return False
        if proc.returncode != 0:
            results.append(TestResult(
                name=name, round_num=round_num, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"mklink /J 失败, rc={proc.returncode}, stderr={proc.stderr!r}"))
            return False

    snapshot.snapshot(src_dirs)
    results.append(TestResult(
        name=name, round_num=round_num, model_name="_agent_task_",
        passed=True, status_code=0, latency_ms=0,
        detail=f"已注入 {len(model_dirs)} 个模型目录: {model_dirs}"))
    return True


def start_and_wait_ready(builder, model_name, port, results, round_num=1, timeout=240):
    """POST /api/service/start（fire-and-forget）后自行轮询 GET /api/service/status
    直到 running=true 且 model 匹配。逻辑与
    test_service.QAIModelBuilderLocalModelTester.start_and_wait_ready() 一致。"""
    name = f"AGENT-TASK: start_and_wait_ready model={model_name}"
    try:
        r = builder.csrf.post("/api/service/start", json={"model_name": model_name, "port": port}, timeout=30)
    except requests.RequestException as e:
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"POST /api/service/start 请求异常: {e}"))
        return False
    if r.status_code not in (200, 201, 202):
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=r.status_code, latency_ms=0,
            detail=f"POST /api/service/start 非成功状态码: body={r.text[:300]}"))
        return False

    started_at = time.time()
    end = started_at + timeout
    last_status = None
    while time.time() < end:
        try:
            rs = builder.csrf.get("/api/service/status", timeout=10)
            if rs.status_code == 200:
                last_status = rs.json()
                if last_status.get("running") and last_status.get("model") == model_name:
                    results.append(TestResult(
                        name=name, round_num=round_num, model_name=model_name,
                        passed=True, status_code=200, latency_ms=(time.time() - started_at) * 1000,
                        detail=f"服务已就绪: pid={last_status.get('pid')}, port={last_status.get('port')}"))
                    return True
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2)
    results.append(TestResult(
        name=name, round_num=round_num, model_name=model_name,
        passed=False, status_code=0, latency_ms=timeout * 1000,
        detail=f"轮询 {timeout}s 未看到 running=true 且 model={model_name}; last_status={last_status}"))
    return False


def wait_local_backend_ready(genie_service_port, model_name, timeout=300):
    """轮询直连 GenieAPIService 的 GET /v1/models，直到模型真正注册完成才认为就绪；
    返回 (ready, last_error)。原因见 test_service._wait_local_backend_ready() 文档
    字符串：Builder running=true 只反映进程已拉起，权重加载/端口 listen 可能滞后。"""
    base = f"http://127.0.0.1:{genie_service_port}"
    end = time.time() + timeout
    last_error = None
    while time.time() < end:
        try:
            rm = requests.get(f"{base}/v1/models", timeout=30)
            if rm.status_code == 200:
                data = rm.json().get("data", [])
                if any(isinstance(e, dict) and e.get("id") == model_name for e in data):
                    return True, None
                last_error = f"GET /v1/models=200 但 model={model_name} 尚未注册进 data"
            else:
                last_error = f"GET /v1/models 状态码={rm.status_code}"
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(5)
    return False, last_error


# ============================================================================
# 完整对话转录（诊断用，供人工事后阅读判断模型推理内容是否正常）
# ============================================================================
def _extract_turn_content(message_events):
    """从 message 事件的 frame payload 中提取三样东西：
      1) full_text —— 拼接全部 frame_type=="chunk" 的 payload.text（真实生成文本，
         不是事件名/整帧 JSON），这是判断模型推理内容是否正常的核心依据；
      2) frame_counts —— 按 frame_type 计数的摘要（替代过去 events=[名字,名字,...]
         这种对长轮次会膨胀成几十万字符却零诊断价值的重复列表）；
      3) notable_frames —— 除 chunk/tool_mode_changed/end 之外的完整帧（如
         tool_call/tool_result 等），原样保留而不猜测其具体字段名，避免因猜错
         字段名丢失诊断信息（本次真机运行范围内从未见过 tool_call 帧，具体结构
         未知，保留原始 payload 是唯一稳妥做法）。"""
    counts = Counter()
    text_parts = []
    notable_frames = []
    for frame in message_events:
        if not isinstance(frame, dict):
            continue
        ftype = frame.get("frame_type")
        counts[ftype] += 1
        if ftype == "chunk":
            payload = frame.get("payload") or {}
            t = payload.get("text")
            if isinstance(t, str):
                text_parts.append(t)
        elif ftype not in ("tool_mode_changed", "end"):
            notable_frames.append(frame)
    return "".join(text_parts), dict(counts), notable_frames


def _append_transcript_entry(transcript_path, turn_label, prompt_text, full_text,
                          conversation_id, busy, passed, frame_counts, notable_frames,
                          stream_error):
    """把一轮真实 SSE 交互（发送的完整 prompt + 模型生成的完整文本 + 识别出的
    非常规帧 + 关键状态）追加写入独立的转录文件（Markdown，UTF-8），供人工事后
    完整阅读核实推理内容——不做任何截断，允许单轮内容很大。"""
    if not transcript_path:
        return
    path = Path(transcript_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## {turn_label} — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- conversation_id: `{conversation_id}`",
        f"- busy: {busy}",
        f"- passed: {passed}",
        f"- frame_counts: {frame_counts}",
        f"- stream_error: {stream_error}",
        "",
        "### Prompt (完整)",
        "```",
        prompt_text,
        "```",
        "",
        "### 模型生成文本 (完整，未截断)",
        "```",
        full_text if full_text else "(空，本轮未产生任何 chunk 文本)",
        "```",
        "",
    ]
    if notable_frames:
        lines += [
            "### 非常规帧 (tool_call/tool_result 等，原始 payload)",
            "```json",
            json.dumps(notable_frames, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    lines.append("---\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _append_transcript_summary(transcript_path, title, summary_lines):
    """在转录 Markdown 文件末尾追加一个独立的小结区块（如 token 生成速度统计），
    与 _append_transcript_entry() 写逐轮条目同一份文件、同一种追加写法（UTF-8，
    不截断），确保人工阅读转录文件时能在末尾直接看到任务级聚合结论，不需要
    再去对照 results.json。"""
    if not transcript_path:
        return
    path = Path(transcript_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"## {title}", ""] + list(summary_lines) + ["", "---\n"]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;])\s+|\n+")
_CODE_FENCE_RE = re.compile(r"```.*?(```|$)", re.S)


class RepetitionWatchdog:
    """Sentence-level loop detector over the streamed chunk text."""

    def __init__(self, min_sentence_chars=40, min_repeats=4, tail_chars=2000,
                 tail_coverage=0.5, min_total_chars=1200, check_every_chars=256):
        self.min_sentence_chars = min_sentence_chars
        self.min_repeats = min_repeats
        self.tail_chars = tail_chars
        self.tail_coverage = tail_coverage
        self.min_total_chars = min_total_chars
        self.check_every_chars = check_every_chars
        self._parts = []
        self._length = 0
        self._next_check = min_total_chars
        self.verdict = None

    @staticmethod
    def _normalize(sentence):
        return re.sub(r"\s+", " ", sentence).strip().lower()

    def feed(self, text):
        if self.verdict or not text:
            return self.verdict
        self._parts.append(text)
        self._length += len(text)
        if self._length >= self._next_check:
            self._next_check = self._length + self.check_every_chars
            self.verdict = self._check()
        return self.verdict

    def _check(self):
        prose = _CODE_FENCE_RE.sub(" ", "".join(self._parts))
        sentences = [self._normalize(s) for s in _SENTENCE_SPLIT_RE.split(prose)]
        sentences = [s for s in sentences if len(s) >= self.min_sentence_chars]
        if not sentences:
            return None
        counts = Counter(sentences)
        looping = {s for s, c in counts.items() if c >= self.min_repeats}
        if not looping:
            return None
        tail_len, looped_len = 0, 0
        for s in reversed(sentences):
            if tail_len >= self.tail_chars:
                break
            tail_len += len(s)
            if s in looping:
                looped_len += len(s)
        if looped_len < self.tail_coverage * tail_len:
            return None
        top, top_count = counts.most_common(1)[0]
        return {"at_char": self._length, "top_sentence": top[:200], "top_count": top_count,
                "tail_coverage": round(looped_len / tail_len, 2)}


# ============================================================================
# 共享 SSE 聊天驱动 helper（提炼自 test_service._builder_send_chat_message，
# 泛化出 conversation_id 参数以支持"多轮 agent 任务"里的追加消息）
# ============================================================================
def send_agent_chat_turn(builder, model_name, prompt_text, conversation_id=None,
                          title="Agent Task", stream_timeout=900,
                          transcript_path=None, turn_label=None, watchdog=None):
    """发起（conversation_id=None 时新建）或继续一轮 Builder 真实 SSE 聊天代理链路
    （GET /api/chat/conversations/{id}/stream），解析 event:/data: 帧，读到
    done/error 终止帧为止。返回：
        (passed, detail, joined_text, conversation_id, frame_types, frame_reasons, busy)
    与 test_service._builder_send_chat_message() 同款事件解析逻辑（终止帧识别、
    frame_type/reason 逐层下钻），只是把 conversation 创建做成可选，以支持在
    同一个 conversation_id 上追加发送 follow-up 消息（复用同一个真实端点，
    区别只在于是否携带已存在的 conversation_id）。

    `busy=True` 表示服务端在会话锁仍被上一轮占用时发出的 `event: existing_run`
    （payload 只有 {run_id, attach_path}，不含 frame_type/reason 键，与正常
    message/done/error 帧结构完全不同），随即立即关闭连接——这不是失败也不是
    成功，只是当前 conversation 仍在忙，调用方应该等待后重新探测同一
    conversation，而不是把它当作一次真正的对话轮次消耗掉。"""
    if conversation_id is None:
        try:
            conv = builder.csrf.post("/api/chat/conversations", json={"title": title}, timeout=30)
        except requests.RequestException as e:
            return False, f"创建 conversation 异常: {e}", "", None, [], [], False
        if conv.status_code not in (200, 201):
            return False, f"创建 conversation 非成功状态码: {conv.text[:300]}", "", None, [], [], False
        conversation_id = conv.json().get("id")
        if not conversation_id:
            return False, f"创建 conversation 缺少 id: {conv.text[:300]}", "", None, [], [], False

    tab_id = f"agent-task-tab-{conversation_id}"
    stream_path = f"/api/chat/conversations/{conversation_id}/stream"
    events = []
    stream_error = None
    busy = False
    stream_deadline = time.time() + stream_timeout
    stream = None
    loop_verdict = None
    try:
        # timeout=(connect, read)：read 侧原为 25s，与服务端 15s 心跳间隔
        # （QAIModelBuilder _sse.py 的 _HEARTBEAT_INTERVAL_SECONDS=15.0）只留 10s
        # 缓冲，长耗时真实任务（如 27B 模型下载/QNN 量化转换，期间后端忙于本地
        # 计算，心跳帧本身也可能被延迟）下该余量明显不够、会把真正瓶颈误报成
        # ReadTimeout；调大到 60s 留出远大于心跳间隔的安全余量。connect 侧维持
        # 较小值，网络层连不上应该快速失败，不需要同样放宽。
        stream = builder.csrf.request(
            "GET", stream_path, timeout=(15, 60), stream=True,
            params={"tab_id": tab_id, "prompt": prompt_text, "model_id": f"local::{model_name}"})
        if not 200 <= stream.status_code < 300:
            stream_error = f"HTTP 非 2xx: {stream.status_code}; body={stream.text[:300]}"
        else:
            event_name, data_lines = None, []
            for line in stream.iter_lines(decode_unicode=True):
                if time.time() >= stream_deadline:
                    stream_error = f"SSE 总时限 {stream_timeout} 秒已到"
                    break
                line = line.strip() if line else ""
                if not line:
                    if event_name:
                        payload_raw = "\n".join(data_lines)
                        try:
                            payload = __import__("json").loads(payload_raw) if payload_raw else {}
                        except ValueError:
                            payload = {"raw": payload_raw}
                        events.append((event_name, payload))
                        if (watchdog is not None and event_name == "message"
                                and isinstance(payload, dict) and payload.get("frame_type") == "chunk"):
                            loop_verdict = watchdog.feed((payload.get("payload") or {}).get("text"))
                            if loop_verdict:
                                stream_error = f"repetition_loop_detected: {loop_verdict}"
                                break
                        if event_name == "existing_run":
                            # 会话锁被上一轮占用，服务端发完这一帧就会立即关闭连接，
                            # 不会再有 done/error；不要继续等，直接跳出。
                            busy = True
                            break
                        if event_name in ("done", "error"):
                            break
                    event_name, data_lines = None, []
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
    except requests.RequestException as e:
        stream_error = f"{type(e).__name__}: {e}"
    finally:
        if stream is not None:
            try:
                stream.close()
            except requests.RequestException:
                pass
    if loop_verdict:
        try:
            stop = builder.csrf.post("/api/chat/stop", json={"tab_id": tab_id, "reason": "repetition_loop"},
                                     timeout=30)
            loop_verdict["server_stop"] = f"{stop.status_code} {stop.text[:200]}"
        except requests.RequestException as e:
            loop_verdict["server_stop"] = f"{type(e).__name__}: {e}"
        stream_error = f"repetition_loop_detected: {loop_verdict}"
        print(f"  [WATCHDOG] {stream_error}", flush=True)

    message_events = [payload for event, payload in events if event == "message"]
    error_events = [payload for event, payload in events if event == "error"]
    frame_types, frame_reasons = [], []
    for _event, payload in events:
        frame = payload
        while isinstance(frame, dict):
            if "frame_type" in frame or "reason" in frame:
                reason = frame.get("reason")
                if reason is None and isinstance(frame.get("payload"), dict):
                    reason = frame["payload"].get("reason")
                frame_types.append(frame.get("frame_type"))
                frame_reasons.append(reason)
                break
            nested = next((frame[k] for k in ("data", "frame", "payload", "message")
                           if isinstance(frame.get(k), dict)), None)
            if nested is None:
                break
            frame = nested
    terminal_ok = any(ft == "end" and rs in ("completed", "success", "done")
                      for ft, rs in zip(frame_types, frame_reasons))
    terminal_error = any(ft == "error" or rs == "failed"
                         for ft, rs in zip(frame_types, frame_reasons))
    # text 是模型真实生成的文本（拼接全部 chunk 帧的 payload.text），不是事件名列表
    # 也不是整帧 JSON dump——后两者对判断"模型是否真的在正常推理"毫无价值，只是
    # 纯粹的字符串膨胀（长轮次曾观测到 ~291KB 全是重复的 'message' 字符串）。
    text, frame_counts, notable_frames = _extract_turn_content(message_events)
    passed = (not busy and stream_error is None and not error_events and not terminal_error
              and terminal_ok and bool(message_events))
    detail = (f"conversation_id={conversation_id}; frame_counts={frame_counts}; "
              f"frame_types={frame_types}; frame_reasons={frame_reasons}; "
              f"error_events={error_events}; stream_error={stream_error}; busy={busy}")
    _append_transcript_entry(
        transcript_path, turn_label or f"turn (conversation {conversation_id})",
        prompt_text, text, conversation_id, busy, passed, frame_counts, notable_frames,
        stream_error)
    return passed, detail, text, conversation_id, frame_types, frame_reasons, busy


# ============================================================================
# 任务一：转换一个小模型（model-builder skill）
# ============================================================================
# 默认转换目标：远程机器上已确认真实存在的极小 ONNX 模型（104 字节，单个 MatMul 算子，
# 原本用作 NPU 健全性检查素材），而不是 PyTorch checkpoint —— 直接以 .onnx 起步可以跳过
# SKILL.md Step1 "Export to ONNX" 对 torch/torchvision 的依赖，只需 onnx+numpy+QAIRT
# 自带转换工具，大幅降低本次验证对未预置转换环境（python_x64_venv/Setup.bat）的敏感度。
_DEFAULT_SMALL_MODEL_PATH = r"C:\Users\HCKTest\Desktop\GenieEnv\Video_Model\Video\remote_npu_sanity_matmul.onnx"
_DEFAULT_WORKSPACE_ROOT = r"C:\WoS_AI"

_MODEL_CONVERSION_INITIAL_PROMPT_TEMPLATE = (
    "任务：用 model-builder 技能把 ONNX 模型 {small_model_path} 转换为本机 NPU 可运行的 QNN 格式"
    "（FP16 即可，不量化），完成推理校验并写出含 Cosine Similarity Summary 的 REPORT.md。"
    "这是单个 MatMul 的极小模型，已是 ONNX，跳过 Export。已授权你自主运行 Setup.bat 和 pip install，"
    "不要问我确认。\n\n"
    "现在只做第一步：立即调用 exec 工具运行\n"
    "python -c \"import onnx; m=onnx.load(r'{small_model_path}'); "
    "print([(i.name, i.type) for i in m.graph.input]); print([(o.name, o.type) for o in m.graph.output])\"\n"
    "拿到输出后再决定下一个工具调用。\n\n"
    "规则：每次回复必须以一个工具调用开始；不要复述任务或计划，不要写\"我需要继续……\"。"
    "思考不超过三句话。"
)

_MODEL_CONVERSION_FOLLOWUP_PROMPT_TEMPLATE = (
    "不要复述计划。立即调用一个工具执行转换流程里下一个尚未完成的具体步骤"
    "（例如用 exec 运行 model-builder 的 run_pipeline.py，或用 read 查看上一步的报错日志）。"
    "思考不超过三句话。\n\n"
    "提示：工作目录（存放转换产物/REPORT.md 的地方）是 {workspace_root}，"
    "转换目标 ONNX 文件是 {small_model_path}。"
    "run_pipeline.py 不在工作目录下，它是 model-builder 技能自带的固定脚本，"
    "确切绝对路径是 {run_pipeline_path}——直接用这个路径调用，不要去 {workspace_root} 下找它。"
    "如果你不确定其它脚本/日志文件的绝对路径，不要凭记忆猜测，"
    "更不要停下来问我确认路径——立即调用 exec 工具自己查找"
    "（例如 dir /s /b {workspace_root} 查找产物/日志；脚本本身用上面给的 run_pipeline_path）。"
    "找到后继续执行下一步。如果被无法自主解决的问题卡住，只用一句话说明卡在哪一步和具体报错。"
)


def _find_conversion_report(workspace_root, deadline_hint=None):
    """在 workspace_root 下递归查找含 "Cosine Similarity Summary" 的 REPORT.md。
    不假设固定的 <model_name> 子目录名（模型名可能被 agent 自主命名），只按内容特征匹配。
    返回 (report_path_or_None, content_or_None)。"""
    root = Path(workspace_root)
    if not root.is_dir():
        return None, None
    try:
        candidates = sorted(root.rglob("REPORT.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None, None
    for report_path in candidates:
        try:
            content = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Cosine Similarity Summary" in content:
            return report_path, content
    return None, None


def run_model_conversion_task(builder, model_name, results, round_num=1,
                               small_model_path=_DEFAULT_SMALL_MODEL_PATH,
                               workspace_root=_DEFAULT_WORKSPACE_ROOT,
                               run_pipeline_path=None,
                               max_turns=4, per_turn_timeout=900, total_deadline_seconds=1800,
                               busy_poll_interval_seconds=45, transcript_path=None):
    """任务一：驱动本地模型通过 model-builder 技能真实完成一次小模型转换。

    多轮策略：第一轮发起转换请求；每轮结束后先检查 workspace_root 下是否已产出
    含 "Cosine Similarity Summary" 的 REPORT.md——命中即成功，不再追加轮次。
    未命中且还有真实续问轮次预算（max_turns）时，在同一个 conversation_id 上追加
    一条通用续问（见 _MODEL_CONVERSION_FOLLOWUP_PROMPT_TEMPLATE，每轮都显式重申
    workspace_root/small_model_path 绝对路径，避免模型多轮后忘记路径转而反复
    停下来问用户确认——2026-10-01 mc_run12 真机实测复现过这个失败模式），处理模型在 Blocking
    Condition 上停下来询问确认的情形（B1/B2 等，见 SKILL.md），不新发明协议，
    只是把"继续 + 已获授权"说清楚，复用模型本就熟悉的多轮对话模式。

    run_pipeline_path（2026-10-01 mc_run15 真机实测修复）：run_pipeline.py 实际物理位于
    model-builder 技能自身的 scripts/ 目录下（<builder_dir>/factory/chat_features/
    model-builder/scripts/run_pipeline.py），不在 workspace_root 下——${WORKSPACE} 只是
    model-builder 技能存放转换产物（REPORT.md 等）的会话工作目录，README.md 146-159 行已写明
    这一点。mc_run15 的 prompt 没有区分两者，导致模型在 workspace_root 下递归搜索
    run_pipeline.py 一直返回空、却仍不断在 workspace_root 下徒劳 dir /s /b。未显式传入时
    回退到 workspace_root 下的猜测路径（兼容旧调用方），调用方应始终显式传入真实路径。

    会话忙（existing_run）等待策略：服务端上一轮真实工作（pip install / 转换
    脚本执行等）可能持续远超单个 turn 的超时窗口；一旦探测到 busy，不把等待
    预算按 max_turns 切分成多个"瞬间失败"的独立 turn（那样只会白白消耗 max_turns
    预算却什么都没等到），改为在同一个探测循环里用**剩余的全部
    total_deadline_seconds**耐心轮询（每 busy_poll_interval_seconds 秒探测一次），
    直到会话空闲或总预算真正耗尽为止；busy 轮询本身不计入 max_turns，只有真正拿到
    非busy响应才算一次"真实轮次"。

    total_deadline_seconds 是本任务的总耗时上限（跨全部真实轮次+全部busy等待），
    防止单个任务无限期占用测试预算；到期后如实按"未完成"收尾，不假装成功。"""
    name = f"AGENT-TASK: model_conversion model={model_name} target={small_model_path}"
    task_started = time.time()
    task_deadline = task_started + total_deadline_seconds

    if not Path(small_model_path).is_file():
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"转换目标 ONNX 文件不存在: {small_model_path}"))
        return False

    ready, ready_err = wait_local_backend_ready(builder_genie_port_of(builder), model_name)
    if not ready:
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"后端未就绪，跳过任务: {ready_err}", crashed=True))
        return False

    effective_run_pipeline_path = run_pipeline_path or str(Path(workspace_root) / "run_pipeline.py")
    prompt = _MODEL_CONVERSION_INITIAL_PROMPT_TEMPLATE.format(small_model_path=small_model_path)
    conversation_id = None
    transcript = []
    report_path, report_content = None, None
    real_turns_used = 0
    attempts_used = 0  # 含 busy 探测本身的总请求次数（诊断用途，不占预算）
    gave_up_while_busy = False
    passed, detail, text, frame_types, frame_reasons, busy = (
        False, "未发起任何请求（总时限在第一轮之前已耗尽）", "", [], [], False)

    while True:
        remaining = task_deadline - time.time()
        if remaining <= 30:
            transcript.append(f"[总时限] 剩余 {remaining:.0f}s，终止")
            break
        if real_turns_used >= max_turns:
            transcript.append(f"[真实续问轮次] 已用完 max_turns={max_turns}，终止")
            break

        real_turns_used += 1
        print(f"  [STAGE] 真实轮次 {real_turns_used}/{max_turns} 开始: conversation_id={conversation_id}, "
              f"prompt_excerpt={prompt[:80]!r}...", flush=True)

        # 耐心 busy 等待循环：用剩余的全部 deadline 反复探测同一 conversation，
        # 不受 max_turns 或单轮 timeout 切分影响（见函数 docstring）。
        while True:
            attempts_used += 1
            remaining = task_deadline - time.time()
            if remaining <= 30:
                break
            call_timeout = min(per_turn_timeout, max(30, remaining))
            passed, detail, text, conversation_id, frame_types, frame_reasons, busy = send_agent_chat_turn(
                builder, model_name, prompt, conversation_id=conversation_id,
                title="Agent Task: model conversion", stream_timeout=call_timeout,
                transcript_path=transcript_path,
                turn_label=f"model_conversion real_turn={real_turns_used} attempt={attempts_used}",
                watchdog=RepetitionWatchdog())
            if not busy:
                break
            remaining = task_deadline - time.time()
            print(f"  [STAGE] 探测到 existing_run（会话忙），attempts_used={attempts_used}, "
                  f"剩余总预算 {remaining:.0f}s，等待 {busy_poll_interval_seconds}s 后重新探测", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] busy; attempts_used={attempts_used}; "
                               f"remaining={remaining:.0f}s; detail={detail}")
            if remaining <= busy_poll_interval_seconds + 30:
                transcript.append(f"[real_turn {real_turns_used}] 总时限即将耗尽，放弃继续等待 busy 状态解除")
                break
            time.sleep(busy_poll_interval_seconds)
            # 重新探测同一 conversation 是否已空闲：只要仍 busy，服务端会在收到
            # message_events 之前就先关连接，复用原 prompt 本身即可。

        print(f"  [STAGE] 真实轮次 {real_turns_used}/{max_turns} 结束: passed={passed}; busy={busy}; "
              f"conversation_id={conversation_id}; frame_types={frame_types}; "
              f"frame_reasons={frame_reasons}", flush=True)
        transcript.append(f"[real_turn {real_turns_used}] passed={passed}; busy={busy}; detail={detail}; "
                           f"text_excerpt={text[:500]!r}")

        if busy:
            # 一直忙到总预算耗尽也没能等到空闲：模型从未真正处理这条 prompt，
            # 不算一次有意义的续问，直接结束整个任务（不再追加新 followup）。
            gave_up_while_busy = True
            transcript.append(f"[real_turn {real_turns_used}] 会话持续忙碌直至总时限耗尽，任务终止")
            break

        report_path, report_content = _find_conversion_report(workspace_root)
        if report_path is not None:
            print(f"  [STAGE] 真实轮次 {real_turns_used}/{max_turns} 检测到有效 REPORT.md: {report_path}", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] 已检测到有效 REPORT.md: {report_path}")
            break
        if not passed and conversation_id is None:
            # 连 conversation 都没能建立（比如 Builder 未就绪），没有意义继续追加轮次。
            transcript.append(f"[real_turn {real_turns_used}] SSE 请求本身失败且未获得 conversation_id，终止本任务")
            break
        prompt = _MODEL_CONVERSION_FOLLOWUP_PROMPT_TEMPLATE.format(
            workspace_root=workspace_root, small_model_path=small_model_path,
            run_pipeline_path=effective_run_pipeline_path)

    elapsed = time.time() - task_started
    ok = report_path is not None
    detail_text = (
        f"real_turns_used={real_turns_used}/{max_turns}; attempts_used={attempts_used}; "
        f"gave_up_while_busy={gave_up_while_busy}; elapsed={elapsed:.0f}s; "
        f"report_found={ok}; report_path={report_path}; "
        f"transcript=\n" + "\n".join(transcript)
    )
    if ok:
        cosine_excerpt = ""
        idx = report_content.find("Cosine Similarity Summary")
        if idx >= 0:
            cosine_excerpt = report_content[idx:idx + 300]
        detail_text += f"\ncosine_excerpt={cosine_excerpt!r}"
    results.append(TestResult(
        name=name, round_num=round_num, model_name=model_name,
        passed=ok, status_code=0, latency_ms=elapsed * 1000,
        detail=detail_text,
        # 首次真实驱动此任务：环境（python_x64_venv/QAIRT 转换工具链）此前从未在目标机器上
        # 初始化过，全流程能否在有限轮次/时限内跑完本身就是本次验证的一部分；未完成时按
        # 真实失败上报，不做 ignorable 豁免（不是"服务端已知缺陷"，是"任务尚未验证通过"）。
        response_data={"conversation_id": conversation_id, "report_path": str(report_path) if report_path else None}))
    return ok


_PROBE_PROMPT = "请确认你现在处于 model-build 模式，简单说明你看到了哪些工具。"


def run_model_build_probe_task(builder, model_name, results, round_num=1, stream_timeout=120,
                                transcript_path=None):
    """极简 model-build 模式基础可用性探测：只发一条不涉及任何真实转换工作的
    简单 prompt，用来判断此前观察到的 empty_response 是否在最简单场景下也复现——
    复现说明是环境/代理层问题，不复现说明是"重系统提示词+复杂任务"组合本身的问题。
    不追加任何 follow-up，一轮结束即收尾。"""
    name = f"AGENT-TASK: model_build_probe model={model_name}"
    ready, ready_err = wait_local_backend_ready(builder_genie_port_of(builder), model_name)
    if not ready:
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"后端未就绪，跳过探测: {ready_err}", crashed=True))
        return False
    started = time.time()
    print(f"  [STAGE] probe 开始: prompt={_PROBE_PROMPT!r}", flush=True)
    passed, detail, text, conversation_id, frame_types, frame_reasons, busy = send_agent_chat_turn(
        builder, model_name, _PROBE_PROMPT, title="Agent Task: model-build probe",
        stream_timeout=stream_timeout, transcript_path=transcript_path, turn_label="model_build_probe")
    elapsed = time.time() - started
    print(f"  [STAGE] probe 结束: passed={passed}; busy={busy}; frame_types={frame_types}; "
          f"frame_reasons={frame_reasons}; text_excerpt={text[:200]!r}", flush=True)
    is_empty_response = any(fr == "empty_response" for fr in frame_reasons) or "empty_response" in detail
    results.append(TestResult(
        name=name, round_num=round_num, model_name=model_name,
        passed=passed, status_code=0, latency_ms=elapsed * 1000,
        detail=f"is_empty_response={is_empty_response}; {detail}",
        # empty_response 复现与否是本探测本身要观察的现象，不代表脚本/环境缺陷，
        # 复现时不应算作需要排查的新增失败——按 ignorable 处理，交由回复文本判读。
        ignorable=is_empty_response))
    return passed


# ============================================================================
# 任务二：编写一个可运行的俄罗斯方块小程序（write/exec 通用工具，无需 model-builder
# 专属技能流程；重复循环空转问题已在 model_conversion 任务上定位并通过
# RepetitionWatchdog + "立即行动"prompt 解决，本任务直接复用同一套机制）。
# ============================================================================
_DEFAULT_TETRIS_PATH = _DEFAULT_WORKSPACE_ROOT + r"\tetris\tetris.py"

# 措辞刻意规避带嵌套引号的 shell one-liner（如 `python -c "..."`）：上一轮
# model_conversion 真机验证发现模型在复现这类嵌套转义时会生成缺 <tool_call> 标签、
# 转义错误的畸形 JSON，导致工具从未被真正调用（见 test_builder_agent_tasks.py.notes.md）。
# tetris 任务的 write/exec 指令全部只涉及普通文件路径参数，不复刻这个坑。
_TETRIS_INITIAL_PROMPT_TEMPLATE = (
    "任务：用 write 工具创建 {tetris_path}（只用 Python 标准库 + tkinter，不依赖任何第三方包）。"
    "该文件必须支持命令行参数 --selftest：加了该参数时跳过 GUI 初始化，headless 运行约 200 步"
    "游戏逻辑（方块下落、旋转、消行），全部完成后打印 SELFTEST OK 并正常退出；不加该参数时才"
    "启动 tkinter 窗口正常游玩（方向键移动/旋转/下落，能消行，能显示分数）。写完后用 exec 工具运行"
    "python {tetris_path} --selftest 验证，如果报错就修复后重新运行 exec 验证，直到看到 SELFTEST OK。\n\n"
    "现在只做第一步：立即调用 write 工具创建这个文件的初始版本。\n\n"
    "规则：每次回复必须以一个工具调用开始；不要复述任务或计划，不要写\"我需要……\"。"
    "思考不超过三句话。"
)

_TETRIS_FOLLOWUP_PROMPT_TEMPLATE = (
    "不要复述计划。立即调用 exec 工具运行 python {tetris_path} --selftest 验证刚才写的代码，"
    "如果报错就立即调用 write 工具修复对应问题，然后重新调用 exec 验证，直到看到 SELFTEST OK 为止。"
    "思考不超过三句话。如果被无法自主解决的问题卡住，只用一句话说明卡在哪一步和具体报错。"
)

_TETRIS_STATIC_CHECKS = {
    "rotate_logic": re.compile(r"def\s+\w*rotat\w*\s*\(|\brotat\w*\s*\(", re.I),
    "clear_lines_logic": re.compile(r"def\s+\w*clear\w*\s*\(|clear[_ ]?(line|row)s?", re.I),
    "selftest_flag": re.compile(r"--selftest"),
    "tkinter_import": re.compile(r"^\s*(import\s+tkinter\b|from\s+tkinter\b)", re.I | re.M),
}

# static_ok=False 时的续问反馈措辞（2026-10-01 tetris_run8 真机实测复现：模型写出一个
# 68 行占位实现——TetrisGUI 方向键绑定全是 lambda e: None 空操作，--selftest 只是
# run_steps(200) 空转从不触碰任何真实游戏规则——却仍打印 SELFTEST OK 全身而退；
# 此前 followup_extra 只覆盖 compile 失败/selftest 失败两个分支，selftest_ok=True
# 但 static_ok=False 这种“假通过”完全没有对应反馈，模型连续两轮产出一字不差的
# 占位实现，因为它根本不知道哪里被判定不合格）。
_TETRIS_STATIC_MARKER_HINTS = {
    "rotate_logic": "方块旋转（需要真正实现旋转变换的函数/逻辑，不能是占位符）",
    "clear_lines_logic": "消行（需要检测满行并真正清除的函数/逻辑）",
    "selftest_flag": "--selftest 命令行开关",
    "tkinter_import": "tkinter 窗口/GUI",
}


def _check_tetris_static(content):
    """静态检查核心游戏逻辑关键特征是否存在（旋转/消行相关代码、--selftest 开关、
    tkinter 引用）。只做关键词/简单正则匹配，不做真正的 AST 语义分析——足以区分
    "完全没写游戏逻辑的空壳（比如只是打印 SELFTEST OK 就退出的取巧实现）"与
    "至少真的尝试实现了核心机制"，不追求精确匹配真实游戏规则。
    返回 (all_ok, markers_dict)。"""
    markers = {k: bool(pattern.search(content)) for k, pattern in _TETRIS_STATIC_CHECKS.items()}
    return all(markers.values()), markers


def _check_tetris_file(tetris_path):
    """检查目标文件是否存在，并用 py_compile 校验语法有效性（只检查能否编译，不实际执行）。
    返回 (file_exists, compiles_ok, compile_err)。"""
    p = Path(tetris_path)
    if not p.is_file():
        return False, False, None
    try:
        py_compile.compile(str(p), doraise=True)
        return True, True, None
    except py_compile.PyCompileError as e:
        return True, False, str(e)
    except (OSError, SyntaxError, ValueError) as e:
        return True, False, f"{type(e).__name__}: {e}"


def _run_tetris_selftest(tetris_path, python_exe="python", timeout=60):
    """脚本自己（不依赖模型自称"已验证通过"）执行
    `<python_exe> <tetris_path> --selftest`（带超时），要求进程 exit code 为 0
    且 stdout 含 "SELFTEST OK"——这是"产物真实可用"的硬判据。返回 (ok, detail)。"""
    try:
        proc = subprocess.run(
            [python_exe, str(tetris_path), "--selftest"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"selftest 执行超时(>{timeout}s)"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"selftest 子进程异常: {type(e).__name__}: {e}"
    ok = proc.returncode == 0 and "SELFTEST OK" in proc.stdout
    detail = (f"exit_code={proc.returncode}; stdout_tail={proc.stdout[-500:]!r}; "
              f"stderr_tail={proc.stderr[-500:]!r}")
    return ok, detail


def run_tetris_task(builder, model_name, results, round_num=1,
                     tetris_path=_DEFAULT_TETRIS_PATH, python_exe="python",
                     max_turns=3, per_turn_timeout=600, total_deadline_seconds=1200,
                     busy_poll_interval_seconds=30, transcript_path=None):
    """任务二：驱动本地模型用 write/exec 工具写出一个可运行的俄罗斯方块小程序。

    判定标准（四项，均落地为实际代码检查，不是模型自称"已经验证通过"就算数）：
      1. 目标文件真实存在；
      2. py_compile 通过（语法有效，只检查能否编译，不实际执行）；
      3. 本脚本自己（不经过模型）执行 `python <path> --selftest`（带超时），
         要求 exit code 为 0 且 stdout 含 "SELFTEST OK"；
      4. 静态检查核心游戏逻辑关键特征存在（旋转/消行相关代码、--selftest 开关、
         tkinter 引用），防止"文件里只打印了 SELFTEST OK 但根本没有游戏逻辑"这种
         取巧实现被误判为成功。

    多轮/busy-wait 策略与 run_model_conversion_task 完全一致（见其文档字符串），
    复用同一套 send_agent_chat_turn + RepetitionWatchdog 机制；续问 prompt 会把上一轮
    具体的编译/selftest 报错带上，帮模型定位问题而不是盲目重试。"""
    name = f"AGENT-TASK: tetris model={model_name} target={tetris_path}"
    task_started = time.time()
    task_deadline = task_started + total_deadline_seconds

    ready, ready_err = wait_local_backend_ready(builder_genie_port_of(builder), model_name)
    if not ready:
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"后端未就绪，跳过任务: {ready_err}", crashed=True))
        return False

    prompt = _TETRIS_INITIAL_PROMPT_TEMPLATE.format(tetris_path=tetris_path)
    conversation_id = None
    transcript = []
    real_turns_used = 0
    attempts_used = 0
    gave_up_while_busy = False
    passed, detail, text, frame_types, frame_reasons, busy = (
        False, "未发起任何请求（总时限在第一轮之前已耗尽）", "", [], [], False)

    file_exists = compiles_ok = selftest_ok = static_ok = False
    compile_err = selftest_detail = None
    static_markers = {}

    while True:
        remaining = task_deadline - time.time()
        if remaining <= 30:
            transcript.append(f"[总时限] 剩余 {remaining:.0f}s，终止")
            break
        if real_turns_used >= max_turns:
            transcript.append(f"[真实续问轮次] 已用完 max_turns={max_turns}，终止")
            break

        real_turns_used += 1
        print(f"  [STAGE] tetris 真实轮次 {real_turns_used}/{max_turns} 开始: conversation_id={conversation_id}, "
              f"prompt_excerpt={prompt[:80]!r}...", flush=True)

        while True:
            attempts_used += 1
            remaining = task_deadline - time.time()
            if remaining <= 30:
                break
            call_timeout = min(per_turn_timeout, max(30, remaining))
            passed, detail, text, conversation_id, frame_types, frame_reasons, busy = send_agent_chat_turn(
                builder, model_name, prompt, conversation_id=conversation_id,
                title="Agent Task: tetris", stream_timeout=call_timeout,
                transcript_path=transcript_path,
                turn_label=f"tetris real_turn={real_turns_used} attempt={attempts_used}",
                watchdog=RepetitionWatchdog())
            if not busy:
                break
            remaining = task_deadline - time.time()
            print(f"  [STAGE] tetris 探测到 existing_run（会话忙），attempts_used={attempts_used}, "
                  f"剩余总预算 {remaining:.0f}s，等待 {busy_poll_interval_seconds}s 后重新探测", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] busy; attempts_used={attempts_used}; "
                               f"remaining={remaining:.0f}s; detail={detail}")
            if remaining <= busy_poll_interval_seconds + 30:
                transcript.append(f"[real_turn {real_turns_used}] 总时限即将耗尽，放弃继续等待 busy 状态解除")
                break
            time.sleep(busy_poll_interval_seconds)

        print(f"  [STAGE] tetris 真实轮次 {real_turns_used}/{max_turns} 结束: passed={passed}; busy={busy}; "
              f"conversation_id={conversation_id}; frame_types={frame_types}; "
              f"frame_reasons={frame_reasons}", flush=True)
        transcript.append(f"[real_turn {real_turns_used}] passed={passed}; busy={busy}; detail={detail}; "
                           f"text_excerpt={text[:500]!r}")

        if busy:
            gave_up_while_busy = True
            transcript.append(f"[real_turn {real_turns_used}] 会话持续忙碌直至总时限耗尽，任务终止")
            break

        file_exists, compiles_ok, compile_err = _check_tetris_file(tetris_path)
        if file_exists and compiles_ok:
            # 静态检查独立于 selftest 结果无条件执行：selftest_ok 反映"模型自己写的
            # 自验证脚本是否通过"，static_ok 反映"源码里是否真的含核心游戏逻辑关键特征"，
            # 这是两个正交的诊断维度——此前 static_ok/static_markers 被嵌在
            # `if selftest_ok:` 分支内，selftest 失败时永远短路为初始默认值 (False, {})，
            # 导致"完全没写游戏逻辑"和"写了但 selftest 本身有 bug"这两种截然不同的情形
            # 在报告里无法区分（真机验证已实测复现：模型产出的 tetris.py 核心逻辑完整
            # 但其自撰写的 selftest() 从未调用 move/rotate 导致提前 Game Over）。
            try:
                content = Path(tetris_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            static_ok, static_markers = _check_tetris_static(content)
            selftest_ok, selftest_detail = _run_tetris_selftest(tetris_path, python_exe)
            print(f"  [STAGE] tetris 真实轮次 {real_turns_used}/{max_turns} selftest_ok={selftest_ok}; "
                  f"static_ok={static_ok}; markers={static_markers}", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] selftest_ok={selftest_ok}; "
                               f"static_ok={static_ok}; markers={static_markers}; "
                               f"selftest_detail={selftest_detail}")
            if selftest_ok and static_ok:
                break
        elif file_exists:
            transcript.append(f"[real_turn {real_turns_used}] 文件存在但 py_compile 失败: {compile_err}")
        else:
            transcript.append(f"[real_turn {real_turns_used}] 目标文件尚不存在: {tetris_path}")

        if not passed and conversation_id is None:
            transcript.append(f"[real_turn {real_turns_used}] SSE 请求本身失败且未获得 conversation_id，终止本任务")
            break

        followup_extra = ""
        if file_exists and not compiles_ok:
            followup_extra = f"\n\n上次 py_compile 报错：{compile_err}"
        elif file_exists and compiles_ok and not selftest_ok:
            followup_extra = f"\n\n上次运行 --selftest 失败：{selftest_detail}"
        elif file_exists and compiles_ok and selftest_ok and not static_ok:
            missing_hints = [_TETRIS_STATIC_MARKER_HINTS.get(k, k)
                              for k, marker_ok in static_markers.items() if not marker_ok]
            followup_extra = (
                "\n\n上次 --selftest 打印了 SELFTEST OK，但这是假通过：静态检查发现代码里"
                f"缺少关键游戏逻辑（缺失: {'; '.join(missing_hints)}）。不要只让 --selftest "
                "空转几步、伪造一个 SELFTEST OK 就退出——必须真正实现方块旋转、下落、碰撞检测、"
                "消行等核心机制，并让 --selftest 真实驱动这些逻辑跑一遍后再打印 SELFTEST OK。"
                "立即用 write 工具重写补全缺失的游戏逻辑，不要原样重复上一次的实现。")
        prompt = _TETRIS_FOLLOWUP_PROMPT_TEMPLATE.format(tetris_path=tetris_path) + followup_extra

    elapsed = time.time() - task_started
    ok = file_exists and compiles_ok and selftest_ok and static_ok
    detail_text = (
        f"real_turns_used={real_turns_used}/{max_turns}; attempts_used={attempts_used}; "
        f"gave_up_while_busy={gave_up_while_busy}; elapsed={elapsed:.0f}s; "
        f"file_exists={file_exists}; compiles_ok={compiles_ok}; compile_err={compile_err}; "
        f"selftest_ok={selftest_ok}; selftest_detail={selftest_detail}; "
        f"static_ok={static_ok}; static_markers={static_markers}; "
        f"transcript=\n" + "\n".join(transcript)
    )
    results.append(TestResult(
        name=name, round_num=round_num, model_name=model_name,
        passed=ok, status_code=0, latency_ms=elapsed * 1000,
        detail=detail_text,
        # 与 model_conversion 一致：首次真实驱动，未完成时按真实失败上报，不做
        # ignorable 豁免（不是"服务端已知缺陷"，是"任务尚未验证通过"）。
        response_data={"conversation_id": conversation_id, "tetris_path": tetris_path}))
    return ok


_INCEPTION_PRECISION_COMPARE_PROMPT = (
    "帮我下载原始的 inception_v3 模型，分别转成 FP16 与 W8A8 两种精度的 QNN 模型并进行推理，"
    "对比两者的差异。测试图片使用项目自带的 samples/images/flower.jpg（相对项目根目录）。"
    "请一次性自动跑完整个流程，无需中途确认。"
)

# 命中疑似 Blocking Condition（模型主动停下来问询，而不是正常推进/完成）时才追加的
# 唯一一轮"最简中性"继续指令——不代替用户做决策，只是让它继续；其余过程必须让
# QAIModelBuilder 自主完成（用户明确要求，见模块docstring本次新增任务的设计原则）。
_INCEPTION_NEUTRAL_CONTINUE_PROMPT = "请继续自主完成剩余步骤，你已获得完整授权，不需要进一步确认。"

_INCEPTION_BLOCKING_QUESTION_MARKERS = (
    "？", "?", "请确认", "是否继续", "需要我", "你希望", "请告知", "请指示", "请问",
)


def _looks_like_blocking_question(text):
    """粗略判定模型是否在这一轮结束时停下来向用户提问（命中某个 Blocking
    Condition），而不是正常完成或仍在自主推进——只看生成文本结尾一段是否出现
    中/英文问号或常见确认性短语。这只是一个启发式信号，用于决定是否追加
    唯一一轮中性续问；判定失误（漏判/误判）本身也是一条值得记录的真实发现，
    不追求绝对精确。"""
    if not text:
        return False
    tail = text.strip()[-300:]
    return any(marker in tail for marker in _INCEPTION_BLOCKING_QUESTION_MARKERS)


def _find_inception_artifacts(workspace_root, task_started_ts):
    """在 workspace_root 下查找 inception_v3 FP16/W8A8 两种精度模型产物目录，
    以及本次任务期间（mtime >= task_started_ts）新产生的、引用 flower.jpg 的
    真实推理输出证据。返回字典（而非单一布尔值），供调用方逐项判定——
    model-hub/models/inception_v3/NOTES.md 里原本就记有一份历史验证结果，
    判定逻辑必须要求"新鲜"证据（mtime 晚于本次任务开始时间），不能让模型
    凭 NOTES.md 里本来就合理的旧数字蒙混过关（SKILL.md 第129行纪律）。"""
    root = Path(workspace_root)
    result = {
        "fp16_model_dir": None, "w8a8_model_dir": None,
        "fresh_inference_evidence": None, "comparison_statement_hint": None,
    }
    if not root.is_dir():
        return result
    model_exts = (".dlc", ".bin", ".so", ".onnx", ".serialized")
    try:
        all_dirs = [p for p in root.rglob("*") if p.is_dir()]
    except OSError:
        all_dirs = []
    for d in all_dirs:
        name_lower = d.name.lower()
        if "inception" not in str(d.relative_to(root)).lower():
            continue
        try:
            has_model_file = any(f.suffix.lower() in model_exts for f in d.iterdir() if f.is_file())
        except OSError:
            continue
        if not has_model_file:
            continue
        if result["fp16_model_dir"] is None and ("float" in name_lower or "fp16" in name_lower):
            result["fp16_model_dir"] = d
        if result["w8a8_model_dir"] is None and "w8a8" in name_lower:
            result["w8a8_model_dir"] = d
    try:
        candidates = sorted((p for p in root.rglob("*") if p.is_file()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        candidates = []
    for p in candidates:
        if p.suffix.lower() not in (".txt", ".md", ".log", ".json"):
            continue
        try:
            if p.stat().st_mtime < task_started_ts:
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "flower" in content.lower() and any(
                k in content for k in ("Top-1", "Top1", "概率", "logits", "class", "分类")):
            result["fresh_inference_evidence"] = str(p)
            if (any(k in content for k in ("FP16", "fp16", "W8A8", "w8a8"))
                    and any(k in content for k in ("差异", "对比", "相比", "vs", "VS", "diff"))):
                result["comparison_statement_hint"] = str(p)
            break
    return result


# ============================================================================
# Token 生成速度统计（从 _ServiceLogTail 采集到的 GenieAPIService 完整 stdout 中解析）
# ============================================================================
# 后端无关行：response_dispatcher.cpp::PrintProfile()（主推理路径）与
# model_input_builder.h 的摘要推理路径，均在每次查询完成后无条件打印这一行；
# QNN/GGUF/MNN 三后端的 HandleProfile() 都统一填充同名 token_generation_rate 字段，
# 格式逐字相同，故一条正则即可覆盖全部后端——这是本次任务实际驱动的 qwen3-8b-8480
# （QNN 后端）唯一会产生样本的来源。
_TOKEN_GEN_RATE_RE_GENERIC = re.compile(r"Token Generation Rate:\s*([\d.]+)\s*toks/sec")
# GGUF/llama.cpp 专属行：llama_cpp.cpp::Impl::Query() 完成时额外打印的内部诊断
# （含投机解码细节）。与上面那行是两条独立日志，QNN/MNN 后端不会出现它；仅在
# 本脚本未来被用于驱动 GGUF 模型时才会产生样本，这里一并解析是为了不让该场景
# 下的统计悄悄留空（llama_cpp.cpp.notes.md 已记录投机模式下 llama_perf_context
# 的 n_eval 计数口径失真，这条独立行是 GGUF 侧更可靠的真实吞吐来源）。
_TOKEN_GEN_RATE_RE_LLAMACPP = re.compile(r"\[LLAMACpp\][^\n]*?gen_rate=([\d.]+)\s*tok/s")


def _rate_stats(rates):
    """聚合统计（样本数/平均/最大/最小），round 到 2 位小数。空列表时如实返回
    sample_count=0 与全 None（不编造 0.0），供调用方判断"本次确实没有采集到速率
    样本"与"确实采集到了 0.0 tok/s"的区别。"""
    if not rates:
        return {"sample_count": 0, "avg_tok_s": None, "min_tok_s": None, "max_tok_s": None}
    return {
        "sample_count": len(rates),
        "avg_tok_s": round(sum(rates) / len(rates), 2),
        "min_tok_s": round(min(rates), 2),
        "max_tok_s": round(max(rates), 2),
    }


def _summarize_token_generation_rates(log_text):
    """从一次任务期间采集到的完整 GenieAPIService stdout 文本中提取全部 token
    生成速度样本，按来源分组返回聚合统计（见上方两条正则的注释）。两组互不合并
    （同一条真实查询在 GGUF 投机模式下两边数值口径不同，混在一起会掩盖哪一组
    更可信），调用方按实际驱动的后端自行判断该看哪一组。"""
    generic_rates = [float(m.group(1)) for m in _TOKEN_GEN_RATE_RE_GENERIC.finditer(log_text or "")]
    llamacpp_rates = [float(m.group(1)) for m in _TOKEN_GEN_RATE_RE_LLAMACPP.finditer(log_text or "")]
    return {
        "generic_backend_agnostic": _rate_stats(generic_rates),
        "gguf_llamacpp_specific": _rate_stats(llamacpp_rates),
    }


class _ServiceLogTail:
    """全程持续消费 Builder `GET /api/service/logs` SSE（GenieAPIService stdout），断线即按已收行数重连。

    这是任务结束时统计 token 生成速度（见 _summarize_token_generation_rates()）与
    持久化完整后端日志（见调用方把 stop() 的返回值写入 <transcript_path>.genie_service.log）
    的唯一数据来源，不是调试旁路——R7/R8 两轮诊断（见 docstring 顶部的
    run_inception_precision_compare_task 任务）已经靠这份落盘的 .genie_service.log
    还原出子代理真实的工具调用序列，证明该机制稳定可用。"""

    def __init__(self, csrf_session):
        self.csrf = csrf_session
        self.lines = []
        self._stop = threading.Event()
        self._thread = None

    def _worker(self):
        while not self._stop.is_set():
            try:
                resp = self.csrf.request("GET", "/api/service/logs", timeout=(10, 120), stream=True,
                                         params={"skip": len(self.lines)})
                with resp:
                    for raw in resp.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if not raw or not raw.startswith("data:"):
                            continue
                        try:
                            obj = json.loads(raw[5:].strip())
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and "line" in obj:
                            self.lines.append(str(obj["line"]))
            except requests.RequestException:
                pass
            self._stop.wait(2)

    def start(self):
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return "\n".join(self.lines)


def _wait_background_subagents(builder, conversation_id, deadline, poll_seconds=60):
    import sqlite3
    db_path = Path(builder.data_dir) / "db" / "qai.db" if builder.data_dir else None
    history = []
    if not db_path or not conversation_id:
        return history
    while time.time() < deadline - 30:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                rows = con.execute(
                    "SELECT id, status, rounds, updated_at FROM chat_subagent_session "
                    "WHERE root_conversation_id = ?", (conversation_id,)).fetchall()
                active_runs = con.execute(
                    "SELECT COUNT(*) FROM chat_turn_run WHERE conversation_id = ? "
                    "AND status NOT IN ('completed', 'failed', 'interrupted', 'cancelled')",
                    (conversation_id,)).fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error as exc:
            rows, active_runs = [], 0
            history.append(f"sqlite_error={exc}")
        active = [r for r in rows if r[1] in ("running", "background", "pending")]
        snapshot = f"{datetime.now():%H:%M:%S} subagents={rows} active_turn_runs={active_runs}"
        history.append(snapshot)
        print(f"  [STAGE] inception 等待后台子代理: {snapshot}", flush=True)
        if not active and not active_runs:
            break
        time.sleep(poll_seconds)
    return history


def run_inception_precision_compare_task(builder, model_name, results, round_num=1,
                                          workspace_root=_DEFAULT_WORKSPACE_ROOT,
                                          max_turns=3, per_turn_timeout=7200,
                                          total_deadline_seconds=14400,
                                          busy_poll_interval_seconds=60,
                                          transcript_path=None):
    """用户指定的最高优先级验收任务：驱动本地模型一次性完成 inception_v3
    FP16/W8A8 两种精度 QNN 模型的下载+推理+对比，并显式验证"一次性自动跑完,
    无需中途确认"这句话本身是否成立——这正是本函数要测试的现象本身，不是
    附带效果（third/QAIModelBuilder/CONTINUE.md 第199-206行已把这个确切 prompt
    列为既定待办）。

    与 run_model_conversion_task 的关键设计差异（用户明确澄清过的边界）：
      * 只在首轮发送用户原文 prompt（_INCEPTION_PRECISION_COMPARE_PROMPT），
        之后不主动追加任何"通用续问"模板；
      * 探测到 busy（existing_run）时按既有 busy-poll 机制耐心等待（不计入
        max_turns 预算），与 run_model_conversion_task 一致；
      * 探测到非busy的正常结束但判定三要素仍未就位时，用
        _looks_like_blocking_question() 判断模型是否命中了某个 Blocking
        Condition 主动停下来问询——命中才追加唯一一轮最简中性的继续指令
        （不代替用户做决策），并记录命中次数与原文片段；未命中则如实停止，
        不臆测继续，因为"其余过程必须让 QAIModelBuilder 自主完成"。

    判定三要素（缺一不可，见 _find_inception_artifacts()）：
      1) FP16 与 W8A8 两个模型产物目录均存在；
      2) 存在 mtime >= 本次任务开始时间的新鲜推理输出证据（引用 flower.jpg
         且含分类/概率特征，不是 NOTES.md 里原本就有的旧数字复用）；
      3) 该新鲜证据里能看到对两种精度的明确对比陈述关键词。

    CONTINUE.md 第199-206行列出的 4 项官方验证点中，②历史持久化（刷新网页）
    与④提示词面板显示是纯前端 UI 行为；本函数走后端 SSE API 驱动，无法无头
    验证这两项——如实标注在 detail_text 里"本轮未覆盖"，不悄悄跳过不提。

    任务结束时的标准产出（均为稳定主流程，不是调试旁路）：
      1) <transcript_path> —— 逐轮完整 prompt/生成文本/非常规帧（_append_transcript_entry），
         文件末尾追加一个 token 生成速度小结区块（_append_transcript_summary）；
      2) <transcript_path 同名 .genie_service.log> —— 全程持续采集的完整 GenieAPIService
         stdout（_ServiceLogTail），R7/R8 两轮诊断已验证靠它能还原子代理真实工具调用序列；
      3) response_data['token_generation_rate'] —— 对 (2) 做正则统计后的聚合结果
         （见 _summarize_token_generation_rates()），同时写入 results.json 供机器读取。"""
    name = f"AGENT-TASK: inception_precision_compare model={model_name}"
    task_started = time.time()
    task_deadline = task_started + total_deadline_seconds

    ready, ready_err = wait_local_backend_ready(builder_genie_port_of(builder), model_name)
    if not ready:
        results.append(TestResult(
            name=name, round_num=round_num, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"后端未就绪，跳过任务: {ready_err}", crashed=True))
        return False

    log_probe = _ServiceLogTail(builder.csrf)
    log_probe.start()
    task_started_dt_label = datetime.now().isoformat(timespec="seconds")
    prompt = _INCEPTION_PRECISION_COMPARE_PROMPT
    conversation_id = None
    transcript = []
    real_turns_used = 0
    attempts_used = 0
    gave_up_while_busy = False
    blocking_condition_hits = []
    stream_timeout_continuation_hits = []
    artifacts = {}
    passed, detail, text, frame_types, frame_reasons, busy = (
        False, "未发起任何请求（总时限在第一轮之前已耗尽）", "", [], [], False)

    while True:
        remaining = task_deadline - time.time()
        if remaining <= 30:
            transcript.append(f"[总时限] 剩余 {remaining:.0f}s，终止")
            break
        if real_turns_used >= max_turns:
            transcript.append(f"[真实续问轮次] 已用完 max_turns={max_turns}，终止")
            break

        real_turns_used += 1
        print(f"  [STAGE] inception 真实轮次 {real_turns_used}/{max_turns} 开始: "
              f"conversation_id={conversation_id}, prompt_excerpt={prompt[:80]!r}...", flush=True)

        while True:
            attempts_used += 1
            remaining = task_deadline - time.time()
            if remaining <= 30:
                break
            call_timeout = min(per_turn_timeout, max(30, remaining))
            passed, detail, text, conversation_id, frame_types, frame_reasons, busy = send_agent_chat_turn(
                builder, model_name, prompt, conversation_id=conversation_id,
                title="Agent Task: inception_v3 FP16/W8A8 对比", stream_timeout=call_timeout,
                transcript_path=transcript_path,
                turn_label=f"inception_precision_compare real_turn={real_turns_used} attempt={attempts_used}",
                watchdog=RepetitionWatchdog())
            if not busy:
                break
            remaining = task_deadline - time.time()
            print(f"  [STAGE] inception 探测到 existing_run（会话忙），attempts_used={attempts_used}, "
                  f"剩余总预算 {remaining:.0f}s，等待 {busy_poll_interval_seconds}s 后重新探测", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] busy; attempts_used={attempts_used}; "
                               f"remaining={remaining:.0f}s; detail={detail}")
            if remaining <= busy_poll_interval_seconds + 30:
                transcript.append(f"[real_turn {real_turns_used}] 总时限即将耗尽，放弃继续等待 busy 状态解除")
                break
            time.sleep(busy_poll_interval_seconds)

        print(f"  [STAGE] inception 真实轮次 {real_turns_used}/{max_turns} 结束: passed={passed}; busy={busy}; "
              f"conversation_id={conversation_id}; frame_types={frame_types}; "
              f"frame_reasons={frame_reasons}", flush=True)
        transcript.append(f"[real_turn {real_turns_used}] passed={passed}; busy={busy}; detail={detail}; "
                           f"text_excerpt={text[:500]!r}")

        if busy:
            gave_up_while_busy = True
            transcript.append(f"[real_turn {real_turns_used}] 会话持续忙碌直至总时限耗尽，任务终止")
            break

        if "subagent_start" in frame_types:
            wait_log = _wait_background_subagents(builder, conversation_id, task_deadline)
            transcript.append(f"[real_turn {real_turns_used}] background_subagent_wait:\n  "
                              + "\n  ".join(wait_log))

        artifacts = _find_inception_artifacts(workspace_root, task_started)
        if artifacts["fp16_model_dir"] and artifacts["w8a8_model_dir"] and artifacts["comparison_statement_hint"]:
            transcript.append(f"[real_turn {real_turns_used}] 判定三要素均已满足: {artifacts}")
            break
        if not passed and conversation_id is None:
            transcript.append(f"[real_turn {real_turns_used}] SSE 请求本身失败且未获得 conversation_id，终止本任务")
            break

        if _looks_like_blocking_question(text):
            hit = {"real_turn": real_turns_used, "text_tail": text.strip()[-300:]}
            blocking_condition_hits.append(hit)
            print(f"  [STAGE] inception 命中疑似 Blocking Condition（模型主动续问）: {hit}", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] 命中疑似 Blocking Condition，"
                               f"追加唯一一轮最简中性继续指令: {hit}")
            prompt = _INCEPTION_NEUTRAL_CONTINUE_PROMPT
            continue

        # "SSE 总时限已到"是测试脚本自己的单轮墙钟预算到期，不是模型/协议本身的失败
        # （busy=False、未命中 Blocking Condition），只要本轮确实产出过活跃帧
        # （text 非空或识别出过 frame_type/reason，排除真正卡死无进展的情形）且
        # total_deadline_seconds/max_turns 仍有余量，就应继续同一个未完成的任务，
        # 而不是白白放弃剩余预算——这不是人工决策介入，纯粹是技术性续杯。
        if "stream_error=SSE 总时限" in detail and (text.strip() or frame_types):
            hit = {"real_turn": real_turns_used, "detail_excerpt": detail[-300:]}
            stream_timeout_continuation_hits.append(hit)
            print(f"  [STAGE] inception 命中单轮 stream_timeout 切断（本轮仍健康活跃），"
                  f"追加续问继续同一任务: {hit}", flush=True)
            transcript.append(f"[real_turn {real_turns_used}] 命中单轮 stream_timeout 切断且本轮仍健康活跃，"
                               f"追加中性继续指令: {hit}")
            prompt = _INCEPTION_NEUTRAL_CONTINUE_PROMPT
            continue

        # 未命中 busy、未命中 Blocking Condition、未命中健康的 stream_timeout 切断、
        # 判定三要素也未就位：按设计原则不臆测继续追加通用续问（这正是在检验
        # "一次性自动跑完"这句话本身是否成立），如实停止。
        transcript.append(f"[real_turn {real_turns_used}] 本轮正常结束但判定三要素未满足，且未检测到明确续问信号，"
                           f"按设计原则不主动追加续问，任务到此为止")
        break

    service_log_text = log_probe.stop()
    if transcript_path:
        service_log_path = Path(transcript_path).with_suffix(".genie_service.log")
        service_log_path.write_text(service_log_text, encoding="utf-8")
        transcript.append(f"[service_log] {len(service_log_text)} chars -> {service_log_path}")
    token_rate_stats = _summarize_token_generation_rates(service_log_text)
    transcript.append(f"[token_generation_rate] {token_rate_stats}")
    _append_transcript_summary(
        transcript_path, "Token 生成速度统计 (Token Generation Rate)",
        [f"- 任务开始时间: {task_started_dt_label}",
         f"- generic_backend_agnostic（QNN/GGUF/MNN 通用，本次任务实际驱动的"
         f"后端应从这组读数）: {token_rate_stats['generic_backend_agnostic']}",
         f"- gguf_llamacpp_specific（仅 GGUF/llama.cpp 后端会产生样本，QNN 下预期"
         f"sample_count=0，不代表异常): {token_rate_stats['gguf_llamacpp_specific']}",
         "- 数据来源: 完整 GenieAPIService stdout（见上文 [service_log] 条目指向的"
         ".genie_service.log），由 _summarize_token_generation_rates() 正则统计。"])
    elapsed = time.time() - task_started
    ok = bool(artifacts.get("fp16_model_dir") and artifacts.get("w8a8_model_dir")
              and artifacts.get("comparison_statement_hint"))
    detail_text = (
        f"real_turns_used={real_turns_used}/{max_turns}; attempts_used={attempts_used}; "
        f"gave_up_while_busy={gave_up_while_busy}; elapsed={elapsed:.0f}s; "
        f"blocking_condition_hits={len(blocking_condition_hits)}; "
        f"blocking_condition_detail={blocking_condition_hits}; "
        f"stream_timeout_continuation_hits={len(stream_timeout_continuation_hits)}; "
        f"stream_timeout_continuation_detail={stream_timeout_continuation_hits}; "
        f"fp16_model_dir={artifacts.get('fp16_model_dir')}; "
        f"w8a8_model_dir={artifacts.get('w8a8_model_dir')}; "
        f"fresh_inference_evidence={artifacts.get('fresh_inference_evidence')}; "
        f"comparison_statement_hint={artifacts.get('comparison_statement_hint')}; "
        f"token_generation_rate={token_rate_stats}; "
        "frontend_only_checks_not_covered=['历史持久化（刷新网页）', '提示词面板显示'] "
        "(CONTINUE.md 第199-206行②④，纯前端行为，本函数走后端API驱动无法无头验证); "
        "transcript=\n" + "\n".join(transcript)
    )
    results.append(TestResult(
        name=name, round_num=round_num, model_name=model_name,
        passed=ok, status_code=0, latency_ms=elapsed * 1000,
        detail=detail_text,
        # 首次真实驱动此任务：未完成时按真实失败上报，不做 ignorable 豁免
        # （不是"服务端已知缺陷"，是"任务尚未验证通过"），与 model_conversion/tetris 一致。
        response_data={"conversation_id": conversation_id,
                        "blocking_condition_hits": blocking_condition_hits,
                        "stream_timeout_continuation_hits": stream_timeout_continuation_hits,
                        "artifacts": {k: (str(v) if v else None) for k, v in artifacts.items()},
                        "token_generation_rate": token_rate_stats}))
    return ok


def builder_genie_port_of(builder):
    """从 QAIModelBuilderManager 实例反推它当前代理的 GenieAPIService 端口。
    Builder 侧固定通过 /api/service/status 的 port 字段暴露真实端口，这里做一次同步查询
    （而不是让调用方各自缓存一份，避免和 Builder 自身状态产生不一致）。"""
    r = builder.csrf.get("/api/service/status", timeout=10)
    r.raise_for_status()
    return r.json()["port"]


# ============================================================================
# main
# ============================================================================
def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="QAIModelBuilder + 本地模型真实多轮 agent 任务测试",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--task", default="model_conversion",
                        choices=["model_conversion", "model_build_probe", "tetris",
                                 "inception_precision_compare"],
                        help="要运行的任务场景：model_conversion 是完整的模型转换多轮任务；"
                             "model_build_probe 是极简 prompt 探测 model-build 模式基础可用性"
                             "（几十秒量级，用于判断 empty_response 是环境/代理层问题还是"
                             "'重系统提示词+复杂任务'组合本身的问题）；tetris 是用 write/exec 通用"
                             "工具驱动模型编写并自验证一个可运行的俄罗斯方块小程序；"
                             "inception_precision_compare 是用户指定的验收任务：一次性 prompt 驱动"
                             "下载 inception_v3 并各推理 FP16/W8A8 两种精度 QNN 模型、对比差异，"
                             "同时验证'一次性自动跑完,无需中途确认'这句话本身是否成立。")
    parser.add_argument("--exe_dir", required=True, help="GenieAPIService 安装目录（含 GenieAPIService.exe）")
    parser.add_argument("--models", required=True, help="本地模型根目录（--models/<name>/config.json）")
    parser.add_argument("--model_name", default="qwen3-8b-8480",
                        help="要加载并驱动完成任务的本地模型目录名")
    parser.add_argument("--builder_dir", required=True, help="QAIModelBuilder 仓库根目录")
    parser.add_argument("--builder_python", default=None, help="QAIModelBuilder 专属 venv 的 python.exe 路径")
    parser.add_argument("--builder_data_dir", default=None,
                        help="Builder 隔离数据目录，默认 <out_dir>/qaimodelbuilder_data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--builder_port", type=int, default=8010)
    parser.add_argument("--genie_port", type=int, default=8901, help="GenieAPIService 监听端口")
    parser.add_argument("--small_model_path", default=_DEFAULT_SMALL_MODEL_PATH,
                        help="model_conversion 任务的转换目标 ONNX 文件（远程机器本地路径）")
    parser.add_argument("--tetris_path", default=_DEFAULT_TETRIS_PATH,
                        help="tetris 任务的目标输出文件路径（远程机器本地路径）")
    parser.add_argument("--python_exe", default="python",
                        help="tetris 任务用来执行 --selftest 自验证的 Python 解释器")
    parser.add_argument("--workspace_root", default=_DEFAULT_WORKSPACE_ROOT,
                        help="model-builder 技能的工作目录根（默认 C:\\WoS_AI）")
    # 以下四个预算参数默认均为 None：不同任务场景（model_conversion/tetris/
    # inception_precision_compare）各自函数签名的默认值天差地别（如 tetris
    # 的 per_turn_timeout=600 对 inception 任务严重不足，inception 的
    # per_turn_timeout=3600 对 tetris 又明显过大），若 argparse 另设一套统一的
    # 全局默认值，main() 会无条件传参、静默覆盖掉函数自身更贴合该场景的默认值
    # （曾实际发生：函数默认值已针对 inception 场景放大，但命令行不显式传参时
    # 仍被这里的全局默认值砍回去）。保持 None，未显式传参时让各任务函数自己的
    # 默认值生效，只有用户真正需要覆盖时才传。
    parser.add_argument("--max_turns", type=int, default=None,
                        help="单个任务最多追加的真实 follow-up 轮次（不含 busy 等待期间的探测重试）；"
                             "省略时使用各任务函数自身默认值")
    parser.add_argument("--per_turn_timeout", type=int, default=None,
                        help="单轮 SSE 请求超时（秒）；省略时使用各任务函数自身默认值"
                             "（inception_precision_compare 默认 3600，覆盖 27B 模型 QNN 量化转换的真实耗时）")
    parser.add_argument("--total_deadline_seconds", type=int, default=None,
                        help="单个任务总耗时上限（秒，跨全部真实轮次+全部busy等待）；"
                             "省略时使用各任务函数自身默认值（inception_precision_compare 默认 7200，"
                             "覆盖 27B 模型下载+两次 QNN 转换+两次推理+对比的小时级真实耗时）")
    parser.add_argument("--busy_poll_interval_seconds", type=int, default=None,
                        help="检测到 existing_run（会话忙）后的轮询间隔（秒），"
                             "用剩余的全部 total_deadline_seconds 耐心等待，不按 max_turns 切分；"
                             "省略时使用各任务函数自身默认值")
    parser.add_argument("--out_dir", default=None, help="结果输出目录，默认 test_results_agent_tasks/<timestamp>")
    parser.add_argument("--transcript_path", default=None,
                        help="完整对话转录文件路径（Markdown，未截断），默认 <out_dir>/conversation_transcript_<task>_<timestamp>.md；"
                             "inception_precision_compare 任务还会在同名 .genie_service.log 落盘完整后端 stdout，"
                             "并统计 token 生成速度写入该 .md 文件末尾与 results.json（均为标准产出，非调试旁路）")
    return parser


def main():
    args = build_arg_parser().parse_args()

    out_dir = (Path(args.out_dir).resolve() if args.out_dir else
               Path(__file__).resolve().parent.parent / "test_results_agent_tasks" /
               datetime.now().strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    args.builder_data_dir = (
        str(Path(args.builder_data_dir).resolve()) if args.builder_data_dir
        else str(out_dir / "qaimodelbuilder_data"))
    builder_python_exe = resolve_builder_python(args.builder_python)
    transcript_path = (
        Path(args.transcript_path) if args.transcript_path
        else out_dir / f"conversation_transcript_{args.task}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    print(f"  [STAGE] 完整对话转录将写入: {transcript_path}", flush=True)

    all_results = []
    all_crash_events = []
    snapshot = ModelDirSnapshot()
    junction_paths = []

    print(f"{'=' * 60}\n启动 QAIModelBuilder ({args.builder_dir})\n{'=' * 60}")
    builder = QAIModelBuilderManager(
        args.builder_dir, args.host, args.builder_port, log_dir=str(out_dir),
        python_exe=builder_python_exe, data_dir=args.builder_data_dir)
    try:
        builder.start(timeout=90)
        print("  [STAGE] Builder 已启动且健康检查通过 (/api/system/health)", flush=True)
    except (RuntimeError, FileNotFoundError) as e:
        all_crash_events.append(CrashEvent(
            timestamp=datetime.now().isoformat(), model_name="_agent_task_", round_num=1,
            endpoint="BUILDER_STARTUP", detail=str(e)))
        all_results.append(TestResult(
            name="AGENT-TASK: builder startup", round_num=1, model_name="_agent_task_",
            passed=False, status_code=0, latency_ms=0, detail=f"启动失败: {e}", crashed=True))
        _finalize_and_exit(all_results, [], all_crash_events, out_dir, remote_mode=False,
                           suite_name="builder_agent_tasks", cmdline=" ".join(sys.argv))

    try:
        available_models = discover_models(args.models)
        if args.model_name not in available_models:
            all_results.append(TestResult(
                name="AGENT-TASK: model availability check", round_num=1, model_name=args.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"--model_name={args.model_name} 不在 --models 目录下已发现的模型中: {available_models}"))
        else:
            if not configure_genie_root(builder, args.exe_dir, all_results, junction_paths=junction_paths):
                raise RuntimeError("configure_genie_root 失败，终止本次运行")
            if not inject_local_models(builder, args.models, [args.model_name], all_results, snapshot,
                                        junction_paths=junction_paths):
                raise RuntimeError("inject_local_models 失败，终止本次运行")
            if not start_and_wait_ready(builder, args.model_name, args.genie_port, all_results):
                raise RuntimeError("start_and_wait_ready 失败，终止本次运行")
            print(f"  [STAGE] 本地模型后端已就绪: model={args.model_name}, genie_port={args.genie_port}", flush=True)

            print(f"{'=' * 60}\n运行任务: {args.task}\n{'=' * 60}", flush=True)
            # 四个预算参数在 argparse 侧默认为 None，这里只在用户显式传参时才
            # 加入 kwargs，省略时让各任务函数自己的签名默认值生效（不同任务场景
            # 耗时量级差异巨大，不能用一套全局默认值覆盖）。
            budget_kwargs = {
                k: v for k, v in (
                    ("max_turns", args.max_turns),
                    ("per_turn_timeout", args.per_turn_timeout),
                    ("total_deadline_seconds", args.total_deadline_seconds),
                    ("busy_poll_interval_seconds", args.busy_poll_interval_seconds),
                ) if v is not None
            }
            if args.task == "model_conversion":
                run_model_conversion_task(
                    builder, args.model_name, all_results,
                    small_model_path=args.small_model_path,
                    workspace_root=args.workspace_root,
                    run_pipeline_path=str(Path(args.builder_dir) / "factory" / "chat_features" /
                                           "model-builder" / "scripts" / "run_pipeline.py"),
                    transcript_path=transcript_path, **budget_kwargs)
            elif args.task == "model_build_probe":
                probe_kwargs = ({"stream_timeout": args.per_turn_timeout}
                                if args.per_turn_timeout is not None else {})
                run_model_build_probe_task(
                    builder, args.model_name, all_results,
                    transcript_path=transcript_path, **probe_kwargs)
            elif args.task == "tetris":
                run_tetris_task(
                    builder, args.model_name, all_results,
                    tetris_path=args.tetris_path,
                    python_exe=args.python_exe,
                    transcript_path=transcript_path, **budget_kwargs)
            elif args.task == "inception_precision_compare":
                run_inception_precision_compare_task(
                    builder, args.model_name, all_results,
                    workspace_root=args.workspace_root,
                    transcript_path=transcript_path, **budget_kwargs)
    except RuntimeError as e:
        print(f"  ✗ {e}")
    finally:
        violations = snapshot.verify()
        for v in violations:
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name="_agent_task_", round_num=1,
                endpoint="MODEL_DIR_SNAPSHOT", detail=v))
            all_results.append(TestResult(
                name="AGENT-TASK: model_dir_snapshot_verify", round_num=1, model_name="_agent_task_",
                passed=False, status_code=0, latency_ms=0, detail=v, crashed=True))
        builder.stop()
        # 必须无条件执行（覆盖正常/异常两条路径），否则残留联接会被远程同步
        # git clean -fd 顺着删除真实目录下的文件（docs/known-issues.md 7.1b 根因③）。
        cleanup_junctions(junction_paths)

    _finalize_and_exit(all_results, [], all_crash_events, out_dir, remote_mode=False,
                       suite_name="builder_agent_tasks", cmdline=" ".join(sys.argv))


if __name__ == "__main__":
    main()
