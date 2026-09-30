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

健康判定标准与 test_service.py 一致：failed==ignored 且 crashed==0 为健康；
退出码严格 failed==0 && crashed==0（复用 test_service._finalize_and_exit()）。
"""

import argparse
import subprocess
import sys
import time
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
    _finalize_and_exit,
)

import requests  # noqa: E402


# ============================================================================
# 移植自 QAIModelBuilderLocalModelTester 的通用基础设施
# （已验证的 mklink /J 注入 + CSRF + 就绪轮询机制，改写为独立函数）
# ============================================================================
def configure_genie_root(builder, genie_root_path, results, round_num=1):
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


def inject_local_models(builder, models_root, model_dirs, results, snapshot, round_num=1):
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
# 共享 SSE 聊天驱动 helper（提炼自 test_service._builder_send_chat_message，
# 泛化出 conversation_id 参数以支持"多轮 agent 任务"里的追加消息）
# ============================================================================
def send_agent_chat_turn(builder, model_name, prompt_text, conversation_id=None,
                          title="Agent Task", stream_timeout=900):
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
    try:
        # timeout=(connect, read)：read 侧原为 10s，比服务端 15s 心跳间隔
        # （QAIModelBuilder _sse.py 的 _HEARTBEAT_INTERVAL_SECONDS=15.0）更短，
        # 理论上可能在两次心跳之间提前触发 ReadTimeout；调大到 25s 留出余量。
        stream = builder.csrf.request(
            "GET", stream_path, timeout=(10, 25), stream=True,
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
    text = " ".join(__import__("json").dumps(p, ensure_ascii=False) for p in message_events)
    passed = (not busy and stream_error is None and not error_events and not terminal_error
              and terminal_ok and bool(message_events))
    detail = (f"conversation_id={conversation_id}; events={[e for e, _ in events]}; "
              f"frame_types={frame_types}; frame_reasons={frame_reasons}; "
              f"error_events={error_events}; stream_error={stream_error}; busy={busy}")
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
    "请使用 model-builder 技能，把这个已有的 ONNX 模型转换为可在本机 NPU 上运行的 "
    "QNN 格式并完成推理校验：{small_model_path}\n"
    "这是一个很小的模型（用于快速验证转换全流程），请完整走完 Export/Inspect -> "
    "Convert（FP16 精度即可，不需要额外量化）-> Context binary -> Inference + "
    "validation 全部步骤，最后写出 REPORT.md（含 Cosine Similarity Summary）。"
    "请自主完成，不需要在中途停下来确认每一步；如果发现必需的运行环境（如 "
    "python_x64_venv / QAIRT SDK 路径配置）尚未初始化，请自行运行 Setup.bat 完成初始化 "
    "后继续，不需要为此专门询问我。"
)

_MODEL_CONVERSION_FOLLOWUP_PROMPT = (
    "请继续完成刚才的模型转换任务，直到写出 REPORT.md 为止。你已经获得我的授权，"
    "可以自主运行 Setup.bat、执行必要的 pip install，并完成 Core Workflow 里剩余的全部步骤，"
    "不需要再次询问确认。如果确实被某个无法自主解决的问题卡住，请在回复里明确说明卡在哪一步、"
    "具体报错是什么。"
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
                               max_turns=4, per_turn_timeout=900, total_deadline_seconds=1800):
    """任务一：驱动本地模型通过 model-builder 技能真实完成一次小模型转换。

    多轮策略：第一轮发起转换请求；每轮结束后先检查 workspace_root 下是否已产出
    含 "Cosine Similarity Summary" 的 REPORT.md——命中即成功，不再追加轮次。
    未命中且还有轮次预算时，在同一个 conversation_id 上追加一条通用续问
    （见 _MODEL_CONVERSION_FOLLOWUP_PROMPT），处理模型在 Blocking Condition
    上停下来询问确认的情形（B1/B2 等，见 SKILL.md），不新发明协议，只是把
    "继续 + 已获授权" 说清楚，复用模型本就熟悉的多轮对话模式。

    total_deadline_seconds 是本任务的总耗时上限（跨全部轮次），防止单个任务
    无限期占用测试预算；到期后如实按"未完成"收尾，不假装成功。"""
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

    prompt = _MODEL_CONVERSION_INITIAL_PROMPT_TEMPLATE.format(small_model_path=small_model_path)
    conversation_id = None
    transcript = []
    report_path, report_content = None, None
    turns_used = 0
    # busy（existing_run）重试与真实续问轮次分开计数：会话忙不应消耗 max_turns 预算，
    # 但也要有独立上限防止无限期占用总时限（心跳间隔15s，等待窗口按此校准）。
    busy_wait_seconds = 20
    max_busy_retries_per_turn = 20

    for turn in range(1, max_turns + 1):
        remaining = task_deadline - time.time()
        if remaining <= 30:
            transcript.append(f"[turn {turn}] 跳过：总时限已到（剩余 {remaining:.0f}s）")
            break
        turns_used = turn
        turn_timeout = min(per_turn_timeout, max(30, remaining))
        print(f"  [STAGE] turn {turn}/{max_turns} 开始: conversation_id={conversation_id}, "
              f"turn_timeout={turn_timeout:.0f}s, prompt_excerpt={prompt[:80]!r}...", flush=True)

        busy_retries = 0
        while True:
            passed, detail, text, conversation_id, frame_types, frame_reasons, busy = send_agent_chat_turn(
                builder, model_name, prompt, conversation_id=conversation_id,
                title="Agent Task: model conversion", stream_timeout=turn_timeout)
            if not busy:
                break
            busy_retries += 1
            remaining = task_deadline - time.time()
            print(f"  [STAGE] turn {turn}/{max_turns} 检测到 existing_run（会话忙），"
                  f"busy_retries={busy_retries}/{max_busy_retries_per_turn}, "
                  f"等待 {busy_wait_seconds}s 后重新探测同一 conversation", flush=True)
            transcript.append(f"[turn {turn}] busy_retry={busy_retries}; detail={detail}")
            if busy_retries >= max_busy_retries_per_turn or remaining <= busy_wait_seconds + 30:
                transcript.append(f"[turn {turn}] busy 重试预算耗尽或总时限将到，放弃本轮等待")
                break
            time.sleep(busy_wait_seconds)
            # 重新探测同一 conversation 是否已空闲：发一条空探测更容易被服务端立即
            # 再次判 busy 而不会误触发新的一轮生成，这里复用原 prompt 本身即可，
            # 因为只要仍 busy，服务端会在收到 message_events 之前就先关连接。

        print(f"  [STAGE] turn {turn}/{max_turns} 结束: passed={passed}; busy={busy}; "
              f"conversation_id={conversation_id}; frame_types={frame_types}; "
              f"frame_reasons={frame_reasons}", flush=True)
        transcript.append(f"[turn {turn}] passed={passed}; busy={busy}; detail={detail}; text_excerpt={text[:500]!r}")

        report_path, report_content = _find_conversion_report(workspace_root)
        if report_path is not None:
            print(f"  [STAGE] turn {turn}/{max_turns} 检测到有效 REPORT.md: {report_path}", flush=True)
            transcript.append(f"[turn {turn}] 已检测到有效 REPORT.md: {report_path}")
            break
        if not passed and conversation_id is None:
            # 连 conversation 都没能建立（比如 Builder 未就绪），没有意义继续追加轮次。
            transcript.append(f"[turn {turn}] SSE 请求本身失败且未获得 conversation_id，终止本任务")
            break
        prompt = _MODEL_CONVERSION_FOLLOWUP_PROMPT

    elapsed = time.time() - task_started
    ok = report_path is not None
    detail_text = (
        f"turns_used={turns_used}/{max_turns}; elapsed={elapsed:.0f}s; "
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


def run_model_build_probe_task(builder, model_name, results, round_num=1, stream_timeout=120):
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
        stream_timeout=stream_timeout)
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
                        choices=["model_conversion", "model_build_probe"],
                        help="要运行的任务场景：model_conversion 是完整的模型转换多轮任务；"
                             "model_build_probe 是极简 prompt 探测 model-build 模式基础可用性"
                             "（几十秒量级，用于判断 empty_response 是环境/代理层问题还是"
                             "'重系统提示词+复杂任务'组合本身的问题）。俄罗斯方块任务留给后续阶段实现。")
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
    parser.add_argument("--workspace_root", default=_DEFAULT_WORKSPACE_ROOT,
                        help="model-builder 技能的工作目录根（默认 C:\\WoS_AI）")
    parser.add_argument("--max_turns", type=int, default=4, help="单个任务最多追加的 follow-up 轮次")
    parser.add_argument("--per_turn_timeout", type=int, default=900, help="单轮 SSE 请求超时（秒）")
    parser.add_argument("--total_deadline_seconds", type=int, default=1800, help="单个任务总耗时上限（秒）")
    parser.add_argument("--out_dir", default=None, help="结果输出目录，默认 test_results_agent_tasks/<timestamp>")
    return parser


def main():
    args = build_arg_parser().parse_args()

    out_dir = (Path(args.out_dir) if args.out_dir else
               Path(__file__).resolve().parent.parent / "test_results_agent_tasks" /
               datetime.now().strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    args.builder_data_dir = (
        str(Path(args.builder_data_dir).resolve()) if args.builder_data_dir
        else str(out_dir / "qaimodelbuilder_data"))
    builder_python_exe = resolve_builder_python(args.builder_python)

    all_results = []
    all_crash_events = []
    snapshot = ModelDirSnapshot()

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
            if not configure_genie_root(builder, args.exe_dir, all_results):
                raise RuntimeError("configure_genie_root 失败，终止本次运行")
            if not inject_local_models(builder, args.models, [args.model_name], all_results, snapshot):
                raise RuntimeError("inject_local_models 失败，终止本次运行")
            if not start_and_wait_ready(builder, args.model_name, args.genie_port, all_results):
                raise RuntimeError("start_and_wait_ready 失败，终止本次运行")
            print(f"  [STAGE] 本地模型后端已就绪: model={args.model_name}, genie_port={args.genie_port}", flush=True)

            print(f"{'=' * 60}\n运行任务: {args.task}\n{'=' * 60}", flush=True)
            if args.task == "model_conversion":
                run_model_conversion_task(
                    builder, args.model_name, all_results,
                    small_model_path=args.small_model_path,
                    workspace_root=args.workspace_root,
                    max_turns=args.max_turns,
                    per_turn_timeout=args.per_turn_timeout,
                    total_deadline_seconds=args.total_deadline_seconds)
            elif args.task == "model_build_probe":
                run_model_build_probe_task(
                    builder, args.model_name, all_results,
                    stream_timeout=args.per_turn_timeout)
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

    _finalize_and_exit(all_results, [], all_crash_events, out_dir, remote_mode=False,
                       suite_name="builder_agent_tasks", cmdline=" ".join(sys.argv))


if __name__ == "__main__":
    main()
