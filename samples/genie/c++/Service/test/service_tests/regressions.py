#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import base64
import json
import os
import random
import signal
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime
import requests

from .common import (
    CrashEvent,
    MULTIMODAL_PROMPTS,
    TestResult,
    detect_modality,
    infer_backend,
    wait_port_closed,
    wait_port_open,
)
from .diagnostics import (
    _MNN_INSUFFICIENT_MEMORY_MARKER,
    _SHUTDOWN_WATCHDOG_EXIT_CODE,
    _SHUTDOWN_WATCHDOG_LOG_MARKER,
    _WINDOWS_EXIT_CODE_HINTS,
    _capability_issue_from_model_defect,
    _capture_log_tail,
    _describe_exit_code,
    _is_sampleapp_crash_exit_code,
    _pick_random_asset_file,
    _scan_model_defect_text,
)
from .managers import PerfMonitor, ServiceManager
from .multi_model import _pick_builder_integration_model


# ============================================================================
# -n -1 vs -n 30 差异化自验证矩阵（直接启动路径，不经 QAIModelBuilder）
# ============================================================================
# Builder 启动 GenieAPIService.exe 时固定 loglevel=3(kInfo)，且结构性无法同步读取子进程
# 自身的日志文件（只有 SSE 流），因此下面 4 项行为差异（历史管理/系统提示词优化/工具定义
# 优化/长文本摘要，均只在 numResponse==-1 时触发）只能在这条不经 Builder、由测试脚本自己
# 用 ServiceManager 直接启动裸 GenieAPIService.exe、可自由传 -d 4 的路径里，通过读取
# stdout 日志文件真正坐实。

_STATELESS_MODE_READ_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read the content of a file at the given path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
    },
}


_STATELESS_MODE_SYSPROMPT_MARKER = "[Optimization] System prompt rebuilt:"
_STATELESS_MODE_LONGTEXT_TRIGGER_MARKER = "[LongTextSummarizer][Map] chunk 1/"
_STATELESS_MODE_LONGTEXT_SUCCESS_MARKER = "[LongTextSummarizer] User message summarized:"
_STATELESS_MODE_LONGTEXT_REDUCE_FAIL_MARKER = "[LongTextSummarizer] Reduce failed"
_STATELESS_MODE_LONGTEXT_TIMEOUT_S = 900


def _read_long_text_trigger_ratio(exe_dir):
    """从构建产物目录下实际生效的 prompt_engineering.json 读取
    prompt_optimization.long_text_summarization.trigger_ratio；读取不到时按 0.5 兜底
    （与代码默认值一致，见 prompt_engineering.json 中的同名字段注释）。"""
    default_ratio = 0.5
    try:
        cfg_path = Path(exe_dir) / "config" / "prompt_engineering.json"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ratio = ((cfg.get("prompt_optimization") or {}).get("long_text_summarization") or {}).get("trigger_ratio")
        if isinstance(ratio, (int, float)) and ratio > 0:
            return float(ratio)
    except Exception:
        pass
    return default_ratio


def _build_stateless_long_text_probe(context_size, trigger_ratio):
    """按 context_size × trigger_ratio 换算出安全超阈值的重复文本长度（字符数）。用一个
    宁可偏大的 token→字符换算系数(约2字符/token)叠加安全系数，确保稳定超过触发阈值，
    不卡在临界值附近；context_size 读取失败/为 0 时退回一个保守默认值。"""
    if not context_size or context_size <= 0:
        context_size = 4096
    filler = "The quick brown fox jumps over the lazy dog while testing long text summarization triggers. "
    chars_needed = int(context_size * trigger_ratio * 2.0 * 1.3)
    chars_needed = max(chars_needed, len(filler) * 4)
    reps = chars_needed // len(filler) + 2
    return (filler * reps)[:chars_needed]


def _stateless_chat_request(host, port, model_name, user_content, tools=None, timeout=180):
    """直接向裸启动的 GenieAPIService.exe 发一次非流式 chat 请求(不经 Builder,不带任何
    OpenClaw header),返回 (requests.Response|None, error_str|None)。"""
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": user_content}],
        "stream": False,
        "max_tokens": 64,
    }
    if tools:
        body["tools"] = tools
    try:
        r = requests.post(f"http://{host}:{port}/v1/chat/completions", json=body, timeout=timeout)
        return r, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def run_numresponse_stateless_mode_regressions(args, models, all_results, all_crash_events, all_perf_samples):
    """不经 QAIModelBuilder 的 `-n -1` vs `-n 30` 自验证矩阵：对同一批探针请求分别用
    `-n -1 -d 4` 与 `-n 30 -d 4`（默认对照组）各启动一次裸 GenieAPIService.exe（两次完全
    独立的服务生命周期,先完整停止第一组再启动第二组,不做进程内热切换——numResponse 是
    进程级全局配置,无法运行时切换）。架构事实（已读 model_input_builder.h 源码核实）：
    压缩管线（系统提示词优化/工具定义优化/长文本摘要）对两种模式完全统一调用、无任何
    `-n` 分支，三者按"一致性断言"（两侧必须同时出现日志）核验；只有历史管理
    （`[History] Skipped` vs `✓ Added`）是真实存在的差异——`IsStatelessMode()` 唯一的
    用途是在 response_dispatcher.cpp 决定客户端断开后是否把历史持久化在服务端，与压缩
    管线无关——该探针按"双向断言"（互斥出现）核验。全方案里唯一能通过读日志真正坐实
    这些差异/一致性确实按预期发生的地方（Builder 路径固定 loglevel=3 且结构性无法同步
    读子进程日志）。
    """
    print(f"\n{'='*60}")
    print("阶段: -n -1 vs -n 30 差异化自验证矩阵（历史管理/系统提示词优化/工具定义优化/长文本摘要）")
    print(f"{'='*60}")
    name_prefix = "STATELESS-MODE:"
    placeholder_model = "_stateless_mode_"
    model_name = _pick_builder_integration_model(models)

    if args.remote:
        all_results.append(TestResult(
            name=f"{name_prefix} -n -1 vs -n 30 差异化验证", round_num=1,
            model_name=model_name or placeholder_model,
            passed=False, status_code=0, latency_ms=0,
            detail="远程模式无法临时切换本地服务启动参数(-n/-d)，跳过差异化自验证矩阵",
            skipped=True
        ))
        return
    if not model_name:
        all_results.append(TestResult(
            name=f"{name_prefix} -n -1 vs -n 30 差异化验证", round_num=1, model_name=placeholder_model,
            passed=False, status_code=0, latency_ms=0,
            detail="未发现可用于该差异化验证矩阵的模型", skipped=True
        ))
        return

    config_path = Path(args.models) / model_name / "config.json"
    if not config_path.exists():
        all_results.append(TestResult(
            name=f"{name_prefix} -n -1 vs -n 30 差异化验证", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0,
            detail=f"找不到 config.json: {config_path}", skipped=True
        ))
        return

    read_tool = [_STATELESS_MODE_READ_TOOL_DEF]
    trigger_ratio = _read_long_text_trigger_ratio(args.exe_dir)

    modes = (("-1", ["-n", "-1", "-d", "4"]), ("30", ["-n", "30", "-d", "4"]))
    mode_data = {}

    for mode_label, extra_args in modes:
        svc = ServiceManager(args.exe_dir, args.host, args.port)
        svc._log_dir = str(Path(args.out_dir) / f"stateless_mode_n_{mode_label.replace('-', 'minus')}")
        perf = PerfMonitor()
        try:
            print(f"  启动服务 (-n {mode_label} -d 4): {model_name}")
            svc.start(str(config_path), extra_args=extra_args)
            if not wait_port_open(args.host, args.port, timeout=120, process=svc.process):
                raise RuntimeError(f"端口 120s 内未可连接 (-n {mode_label})")
            pid = svc.get_pid()
            if pid:
                perf.start(pid)

            data = {}

            # 探针1+2：历史管理 + 系统提示词优化（复用同一次请求产生的日志）
            resp1, err1 = _stateless_chat_request(args.host, args.port, model_name,
                                                    "请记住这句话，并用一句话简短确认。")
            data["hist_sysprompt_resp"] = resp1
            data["hist_sysprompt_err"] = err1
            data["hist_sysprompt_log"] = svc.read_log_tail(svc._stdout_log, 600)

            # 探针3：工具定义优化（必须用预定义工具名 "read" 才能保证产生确定性差异）
            resp3, err3 = _stateless_chat_request(args.host, args.port, model_name,
                                                    "Please call the read tool to read README.md.",
                                                    tools=read_tool)
            data["tool_resp"] = resp3
            data["tool_err"] = err3
            data["tool_log"] = svc.read_log_tail(svc._stdout_log, 600)

            # 探针4：长文本摘要（先查真实 context_size，再按 trigger_ratio 动态换算超阈值长度）
            context_size = 0
            try:
                ctx_r = requests.post(f"http://{args.host}:{args.port}/contextsize",
                                       json={"model": model_name}, timeout=30)
                if ctx_r.status_code == 200:
                    context_size = next((e.get("contextsize", 0) for e in ctx_r.json().get("models", [])
                                         if e.get("model") == model_name), 0)
            except Exception:
                pass
            long_text = _build_stateless_long_text_probe(context_size, trigger_ratio)
            resp4, err4 = _stateless_chat_request(args.host, args.port, model_name, long_text,
                                                    timeout=_STATELESS_MODE_LONGTEXT_TIMEOUT_S)
            data["longtext_resp"] = resp4
            data["longtext_err"] = err4
            data["longtext_log"] = svc.read_log_tail(svc._stdout_log, 800)
            data["context_size"] = context_size

            # /clear /fetch /contextsize：验证该模式下基础接口不因"是否维护历史"这一行为
            # 差异而返回异常状态码/结构。
            try:
                data["clear_resp"] = requests.post(f"http://{args.host}:{args.port}/clear",
                                                     json={"text": "clear"}, timeout=30)
            except Exception as e:
                data["clear_resp"] = None
                data["clear_err"] = f"{type(e).__name__}: {e}"
            try:
                data["fetch_resp"] = requests.post(f"http://{args.host}:{args.port}/fetch",
                                                     json={}, timeout=30)
            except Exception as e:
                data["fetch_resp"] = None
                data["fetch_err"] = f"{type(e).__name__}: {e}"
            try:
                data["contextsize_resp"] = requests.post(f"http://{args.host}:{args.port}/contextsize",
                                                            json={"model": model_name}, timeout=30)
            except Exception as e:
                data["contextsize_resp"] = None
                data["contextsize_err"] = f"{type(e).__name__}: {e}"

            mode_data[mode_label] = data
        except (RuntimeError, FileNotFoundError) as e:
            print(f"  ✗ (-n {mode_label}) 服务启动失败: {e}")
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name=model_name, round_num=1,
                endpoint="STATELESS_MODE_SERVICE_STARTUP", detail=str(e),
                log_tail=_capture_log_tail(svc),
            ))
            all_results.append(TestResult(
                name=f"{name_prefix} 服务启动 (-n {mode_label})", round_num=1, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"启动失败: {str(e)[:300]}", crashed=True
            ))
            mode_data[mode_label] = None
        except Exception as e:
            print(f"  ✗ (-n {mode_label}) 差异化验证阶段异常: {e}")
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name=model_name, round_num=1,
                endpoint="STATELESS_MODE_REGRESSION", detail=str(e),
                log_tail=_capture_log_tail(svc),
            ))
            all_results.append(TestResult(
                name=f"{name_prefix} 探针执行 (-n {mode_label})", round_num=1, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"测试异常: {str(e)[:300]}", crashed=True
            ))
            mode_data[mode_label] = None
        finally:
            perf_samples = perf.stop()
            all_perf_samples.extend(perf_samples)
            print(f"  停止服务 (-n {mode_label})...")
            svc.stop()
            # 两次生命周期必须完全独立：确认端口真正释放后才进入下一组启动，
            # 避免上一个进程刚被结束、TCP 端口仍处于释放延迟阶段就抢跑下一次 start。
            wait_port_closed(args.host, args.port, timeout=15)

    data_neg1 = mode_data.get("-1")
    data_30 = mode_data.get("30")

    # ── 探针1：历史管理（双向断言：该出现的必须出现，该不出现的必须不出现） ──────
    try:
        name = f"{name_prefix} 历史管理 ([History] Skipped vs ✓ Added，双向断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成双向比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["hist_sysprompt_log"]
            log_30 = data_30["hist_sysprompt_log"]
            neg1_has_skipped = "[History] Skipped (numResponse == -1, client manages history)" in log_neg1
            neg1_has_added = "[History] ✓ Added assistant message" in log_neg1
            mode30_has_skipped = "[History] Skipped (numResponse == -1, client manages history)" in log_30
            mode30_has_added = "[History] ✓ Added assistant message" in log_30
            passed = neg1_has_skipped and not neg1_has_added and mode30_has_added and not mode30_has_skipped
            detail = (f"-1侧: skipped={neg1_has_skipped}, added={neg1_has_added}; "
                      f"30侧: skipped={mode30_has_skipped}, added={mode30_has_added}")
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 历史管理双向断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── 探针2：系统提示词优化（一致性断言：压缩管线不分 -n 分支，两侧必须同时出现日志） ──
    # 架构事实（已读 model_input_builder.h 源码核实，`BuildTaskMemoSection`/`ApplyFitMessagesToContext`
    # 等调用点在 General/Harmony 两路径均对称存在，无任何 `numResponse()==-1`/`IsStatelessMode()` 判断）：
    # PreFilterMessages/FitMessagesToContext/系统提示词优化等压缩管线对 `-n -1`（无状态）与 `-n` 正整数
    # （有状态）两种模式完全统一调用，没有分支；`IsStatelessMode()`/`GetEffectiveStatelessMode()`
    # 唯一的真实用途在 response_dispatcher.cpp，只决定是否在客户端断开后把历史持久化在服务端（即
    # 上面探针1验证的 `[History] Skipped` vs `✓ Added` 差异），与本探针验证的压缩管线完全无关。
    # 曾错误断言 `30` 侧不应出现该日志（旧假设：压缩管线按 -n 分支），已被真机回归证伪并纠正于此。
    # 判据标记取自 model_input_builder.h 的实际 kInfo 输出，不要改回已不存在的 savings 文案
    # （理由见 test_service.md「探针标记必须对齐源码实际输出」）。
    try:
        name = f"{name_prefix} 系统提示词优化 ([Optimization] System prompt rebuilt，一致性断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成一致性比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["hist_sysprompt_log"]
            log_30 = data_30["hist_sysprompt_log"]
            neg1_has_savings = _STATELESS_MODE_SYSPROMPT_MARKER in log_neg1
            mode30_has_savings = _STATELESS_MODE_SYSPROMPT_MARKER in log_30
            passed = neg1_has_savings and mode30_has_savings
            detail = f"-1侧出现rebuilt日志={neg1_has_savings}; 30侧出现rebuilt日志={mode30_has_savings}（两侧应一致）"
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 系统提示词优化一致性断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── 探针3：工具定义优化（一致性断言，须用预定义工具名 "read"；原理同探针2，压缩管线不分 -n 分支）──
    try:
        name = f"{name_prefix} 工具定义优化 ([Optimizer] Tools - Original...Savings...，一致性断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成一致性比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["tool_log"]
            log_30 = data_30["tool_log"]
            neg1_has_tool_opt = ("[Optimizer] Tools - Original:" in log_neg1) and ("Savings:" in log_neg1)
            mode30_has_tool_opt = ("[Optimizer] Tools - Original:" in log_30) and ("Savings:" in log_30)
            passed = neg1_has_tool_opt and mode30_has_tool_opt
            detail = f"-1侧出现工具优化日志={neg1_has_tool_opt}; 30侧出现工具优化日志={mode30_has_tool_opt}（两侧应一致）"
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 工具定义优化一致性断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── 探针4：长文本摘要（一致性断言；原理同探针2/3，压缩管线不分 -n 分支） ──────────
    # 判据是"Map 阶段被触发"而不是"摘要成功"：成功标记只在 Reduce 收敛时才打印，而 Reduce 能否在
    # 服务端 300s 推理预算内收敛取决于模型、与 -n 无关（实测两次运行一次成功一次撞预算回退原文）。
    # 请求超时仍需覆盖整条 Map-Reduce 链，否则客户端先断开、探针会在标记落盘前读快照。
    # 实测数字与判据选择理由见 test_service.md「探针4 的判据与超时」。
    try:
        name = f"{name_prefix} 长文本摘要 ([LongTextSummarizer][Map] chunk 1/N，一致性断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成一致性比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["longtext_log"]
            log_30 = data_30["longtext_log"]
            neg1_triggered = _STATELESS_MODE_LONGTEXT_TRIGGER_MARKER in log_neg1
            mode30_triggered = _STATELESS_MODE_LONGTEXT_TRIGGER_MARKER in log_30
            passed = neg1_triggered and mode30_triggered
            detail = (f"context_size(-1侧)={data_neg1.get('context_size')}, "
                      f"context_size(30侧)={data_30.get('context_size')}, trigger_ratio={trigger_ratio}; "
                      f"-1侧 Map 被触发={neg1_triggered}; 30侧 Map 被触发={mode30_triggered}（两侧应一致）; "
                      f"Reduce 成功(-1侧)={_STATELESS_MODE_LONGTEXT_SUCCESS_MARKER in log_neg1}, "
                      f"Reduce 成功(30侧)={_STATELESS_MODE_LONGTEXT_SUCCESS_MARKER in log_30}, "
                      f"Reduce 回退原文(-1侧)={_STATELESS_MODE_LONGTEXT_REDUCE_FAIL_MARKER in log_neg1}, "
                      f"Reduce 回退原文(30侧)={_STATELESS_MODE_LONGTEXT_REDUCE_FAIL_MARKER in log_30}; "
                      f"timeout={_STATELESS_MODE_LONGTEXT_TIMEOUT_S}s, 请求异常(-1侧)={data_neg1.get('longtext_err')}, "
                      f"请求异常(30侧)={data_30.get('longtext_err')}")
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 长文本摘要一致性断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── /clear /fetch /contextsize：两组均需返回预期状态码/结构 ────────────────
    def _check_basic_endpoint(endpoint_label, resp_key_prefix, judge_fn):
        try:
            name = f"{name_prefix} {endpoint_label}（-1/30 两组均需通过）"
            if data_neg1 is None or data_30 is None:
                all_results.append(TestResult(
                    name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                    detail="其中一侧服务未能成功启动/完成探针请求，无法完成校验", skipped=True
                ))
                return
            resp_neg1 = data_neg1.get(f"{resp_key_prefix}_resp")
            resp_30 = data_30.get(f"{resp_key_prefix}_resp")
            ok_neg1, detail_neg1 = judge_fn(resp_neg1)
            ok_30, detail_30 = judge_fn(resp_30)
            passed = ok_neg1 and ok_30
            detail = f"-1侧: {detail_neg1}; 30侧: {detail_30}"
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed,
                status_code=200 if passed else 0, latency_ms=0, detail=detail
            ))
        except Exception as e:
            all_results.append(TestResult(
                name=f"{name_prefix} {endpoint_label}", round_num=1, model_name=model_name,
                passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
            ))

    def _judge_clear(resp):
        if resp is None:
            return False, "请求失败"
        return resp.status_code == 200, f"status={resp.status_code}"

    def _judge_fetch(resp):
        if resp is None:
            return False, "请求失败"
        if resp.status_code != 200:
            return False, f"status={resp.status_code}"
        try:
            body = resp.json()
            return "history" in body, f"status=200, 含history字段={('history' in body)}"
        except Exception as e:
            return False, f"JSON解析失败: {e}"

    def _judge_contextsize(resp):
        if resp is None:
            return False, "请求失败"
        if resp.status_code != 200:
            return False, f"status={resp.status_code}"
        try:
            ctx = next((e.get("contextsize", 0) for e in resp.json().get("models", [])
                        if e.get("model") == model_name), 0)
            return ctx > 0, f"status=200, contextsize={ctx}"
        except Exception as e:
            return False, f"JSON解析失败: {e}"

    _check_basic_endpoint("POST /clear", "clear", _judge_clear)
    _check_basic_endpoint("POST /fetch", "fetch", _judge_fetch)
    _check_basic_endpoint("POST /contextsize", "contextsize", _judge_contextsize)


def run_gguf_explicit_load_regressions(args, models, all_results, all_crash_events, all_perf_samples):
    """阶段 4：对全部已发现的 GGUF 模型（或 --gguf_model 限定的单个模型），分别用
    GGUF device=gpu / device=cpu 启动服务并验证 /models + chat。"""
    print(f"\n{'='*60}")
    print("阶段 4: GGUF GPU/CPU 显式加载回归")
    print(f"{'='*60}")

    devices = ("gpu", "cpu") if args.gguf_devices == "both" else (args.gguf_devices,)
    placeholder_model = args.gguf_model or "gpt-oss-20b-GGUF"
    if args.remote:
        for device in devices:
            all_results.append(TestResult(
                name=f"test_gguf_{device}_explicit_load", round_num=1, model_name=f"{placeholder_model} ({device.upper()})",
                passed=False, status_code=0, latency_ms=0,
                detail="远程模式无法临时切换 service_config.json，跳过 GGUF 显式设备加载回归",
                skipped=True
            ))
        return

    gguf_models = [m for m in models if infer_backend(m)[0] == "GGUF"]
    if args.gguf_model:
        gguf_models = [m for m in gguf_models if m == args.gguf_model]
    if not gguf_models:
        for device in devices:
            all_results.append(TestResult(
                name=f"test_gguf_{device}_explicit_load", round_num=1, model_name=f"{placeholder_model} ({device.upper()})",
                passed=False, status_code=0, latency_ms=0,
                detail="未发现 GGUF 模型目录，跳过 GGUF 显式设备加载回归",
                skipped=True
            ))
        return

    try:
        for gguf_model in gguf_models:
            gguf_config_path = Path(args.models) / gguf_model / "config.json"
            if not gguf_config_path.exists():
                for device in devices:
                    all_results.append(TestResult(
                        name=f"test_gguf_{device}_explicit_load", round_num=1, model_name=f"{gguf_model} ({device.upper()})",
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"找不到 GGUF config.json: {gguf_config_path}", skipped=True
                    ))
                continue

            # 显式 backend/device 现在是每个模型自己 config.json 的可选字段（Step 1：并发多模型
            # 托管与 service_config.json 的 models 数组已删除），不再通过 service_config.json 注入。
            original_gguf_config = gguf_config_path.read_bytes()

            def restore_gguf_config():
                gguf_config_path.write_bytes(original_gguf_config)

            for device in devices:
                test_name = f"test_gguf_{device}_explicit_load"
                service_model_name = gguf_model
                result_model_name = f"{gguf_model} ({device.upper()})"
                try:
                    model_config = json.loads(original_gguf_config.decode("utf-8")) if original_gguf_config.strip() else {}
                except json.JSONDecodeError:
                    model_config = {}
                model_config["backend"] = "GGUF"
                model_config["device"] = device
                gguf_config_path.write_text(json.dumps(model_config, ensure_ascii=False, indent=2), encoding="utf-8")

                svc = ServiceManager(args.exe_dir, args.host, args.port)
                svc._log_dir = args.out_dir
                perf = PerfMonitor()
                try:
                    print(f"  启动 GGUF 显式 {device.upper()} 配置: {gguf_model}")
                    start_time = time.time()
                    svc.start(str(gguf_config_path))
                    if not wait_port_open(args.host, args.port, timeout=300, process=svc.process):
                        all_results.append(TestResult(
                            name=test_name, round_num=1, model_name=result_model_name,
                            passed=False, status_code=0, latency_ms=(time.time() - start_time) * 1000,
                            detail="端口 300s 内未可连接"
                        ))
                        continue

                    pid = svc.get_pid()
                    if pid:
                        perf.start(pid)

                    models_resp = requests.get(f"http://{args.host}:{args.port}/models", timeout=30)
                    actual_backend = ""
                    actual_device = ""
                    if models_resp.status_code == 200:
                        entries = models_resp.json().get("data", [])
                        loaded_entries = [m for m in entries if m.get("is_loaded") and m.get("id") == service_model_name]
                        if loaded_entries:
                            actual_backend = loaded_entries[0].get("backend", "")
                            actual_device = loaded_entries[0].get("device", "")

                    chat_body = {
                        "model": service_model_name,
                        "messages": [{"role": "user", "content": "Reply with one word: hello"}],
                        "stream": False,
                        "max_tokens": 32,
                    }
                    # 与 APITester.test_chat_non_stream 保持一致的 perf_before/perf_after 采样方式，
                    # 否则这条结果永远没有 CPU/内存数据，报告里的"性能对比"表只能显示 N/A。
                    perf_before = perf.snapshot()
                    chat_start = time.time()
                    chat_resp = requests.post(f"http://{args.host}:{args.port}/v1/chat/completions", json=chat_body, timeout=300)
                    perf_after = perf.snapshot()
                    latency = (time.time() - start_time) * 1000
                    chat_latency = (time.time() - chat_start) * 1000
                    chat_ok = False
                    response_text = ""
                    if chat_resp.status_code == 200:
                        try:
                            choices = chat_resp.json().get("choices", [])
                            response_text = choices[0].get("message", {}).get("content", "") if choices else ""
                            chat_ok = len(response_text) > 0
                        except Exception as e:
                            response_text = f"JSON 解析失败: {e}"

                    backend_ok = actual_backend == "GGUF"
                    if device == "gpu":
                        device_ok = actual_device in ("gpu", "cpu")
                        fallback_note = "；GPU 加载 fallback 到 CPU" if actual_device == "cpu" else ""
                    else:
                        device_ok = actual_device == "cpu"
                        fallback_note = ""
                    passed = models_resp.status_code == 200 and chat_resp.status_code == 200 and backend_ok and device_ok and chat_ok
                    detail = (
                        f"requested_device={device}, actual_backend={actual_backend}, actual_device={actual_device}"
                        f"{fallback_note}, /models={models_resp.status_code}, chat={chat_resp.status_code}, "
                        f"chat_latency_ms={chat_latency:.1f}, response_len={len(response_text)}"
                    )
                    all_results.append(TestResult(
                        name=test_name, round_num=1, model_name=result_model_name,
                        passed=passed, status_code=chat_resp.status_code, latency_ms=latency,
                        detail=detail, text_response=response_text, text_prompt=chat_body["messages"][0]["content"],
                        perf_before=perf_before, perf_after=perf_after,
                        response_data={
                            "requested_device": device,
                            "actual_backend": actual_backend,
                            "actual_device": actual_device,
                            "models_status": models_resp.status_code,
                            "chat_status": chat_resp.status_code,
                        }
                    ))
                except (RuntimeError, FileNotFoundError) as e:
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=gguf_model, round_num=1,
                        endpoint=test_name, detail=str(e)
                    ))
                    # GPU 相关的服务启动崩溃仍计入真实失败(crashed=True)，不会因为请求的设备是
                    # gpu 就无条件豁免；命中内存/显存相关关键词时只附加诊断性标注(ignore_reason)，
                    # 不代表已被豁免——崩溃永远是真实问题,正常错误才不会导致连坐。
                    _err_lower = str(e).lower()
                    _mem_hint = any(kw in _err_lower for kw in (
                        "out of memory", "insufficient_memory", "insufficient memory",
                        "cuda", "vram", "allocation failed"
                    ))
                    all_results.append(TestResult(
                        name=test_name, round_num=1, model_name=gguf_model,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"启动失败: {str(e)[:300]}", crashed=True,
                        ignorable=False,
                        ignore_reason=("疑似 GPU 显存/驱动相关(命中内存关键词)，仅作诊断标注，不代表豁免"
                                        if (device == "gpu" and _mem_hint) else "")
                    ))
                except Exception as e:
                    all_results.append(TestResult(
                        name=test_name, round_num=1, model_name=gguf_model,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"测试异常: {e}"
                    ))
                finally:
                    perf_samples = perf.stop()
                    all_perf_samples.extend(perf_samples)
                    print(f"  停止 GGUF 显式 {device.upper()} 服务...")
                    svc.stop()
                    restore_gguf_config()
    finally:
        pass


def run_sampleapp_only_tests(args, models, all_results):
    """SampleApp 专项测试（--suite sampleapp）：只验证 SampleApp.exe（复用
    GenieAPILibrary 引擎的 CLI 客户端），不启动/依赖 GenieAPIService 的 HTTP 服务。

    设计要点（见 .junie/plans/redesign-sampleapp-config-driven-test.md）：
    - 遍历 discover_models 找到的每一个模型（qnn/mnn/GGUF），逐个用 `-c <model>/config.json` 跑一次基本问答；
      SampleApp 每次只加载一个模型（加载前会卸载上一个），所以后一个模型的运行不受前一个影响。
    - 支持 `--model_name a,b` 过滤到指定的一个或若干模型，与 model/mnn/qnn suite 的筛选风格一致。
    - 缺 `SampleApp.exe` → 对每个目标模型各记一条 skipped。
    - 单模型隔离场景下失败即真实失败；仅当命中服务端 MnnVerifier 内存预检查拒绝加载的精确
      文本信号(与 HTTP 端点 failure_reason=insufficient_memory 同一根因)时才归类为 skipped
      (环境资源约束,不是代码缺陷),其它任何原因的失败都不标 ignorable/skipped。
    """
    print(f"\n{'='*60}")
    print("SampleApp 专项测试（--suite sampleapp）")
    print(f"{'='*60}")

    basic_chat_name = "SAMPLEAPP: basic_chat"

    # 按 --model_name（逗号分隔）过滤目标模型；未指定则遍历全部已发现模型。
    target_models = list(models)
    model_filter = getattr(args, "model_name", None)
    if model_filter:
        wanted = {m.strip() for m in model_filter.split(",") if m.strip()}
        target_models = [m for m in models if m in wanted]
        for missing in sorted(wanted - set(models)):
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=missing,
                passed=False, status_code=0, latency_ms=0,
                detail=f"--model_name 指定的模型 '{missing}' 不在已发现模型列表中，跳过", skipped=True
            ))

    if not target_models:
        all_results.append(TestResult(
            name=basic_chat_name, round_num=1, model_name="_sampleapp_",
            passed=False, status_code=0, latency_ms=0,
            detail="未发现任何目标模型（检查 --models / --model_name）", skipped=True
        ))
        return

    sampleapp_exe = Path(args.exe_dir) / "SampleApp.exe"
    if not sampleapp_exe.exists():
        for m in target_models:
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=m,
                passed=False, status_code=0, latency_ms=0,
                detail=f"未找到 {sampleapp_exe}，可能尚未构建该 target，跳过 SampleApp 专项测试",
                skipped=True
            ))
        return

    input_payload = {
        "OptionParams": {"temperature": 0, "top_p": 0.95, "top_k": 40, "n_predict": 512, "n_ctx": 8192},
        "stream": False,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "You are a helpful AI assistant."}]},
            {"role": "user", "content": [{"type": "text", "text": "Please introduce yourself briefly."}]}
        ]
    }
    input_path = Path(args.out_dir) / "_sampleapp_input.json"
    input_path.write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_text = input_payload["messages"][1]["content"][0]["text"]

    for model_name in target_models:
        backend, _device = infer_backend(model_name)
        # GGUF 的实际加载设备(gpu/cpu)由服务在运行时决定(先试 gpu,失败退化到 cpu),SampleApp
        # 是独立的单次 CLI 进程,没有 /models 之类的接口可查询实际设备。如果本轮运行(如 full
        # 套件)里其它阶段(阶段2/4/Builder 集成代理)已经为同一个 GGUF 模型打上了设备后缀,这里复用
        # 那个已观测到的设备标签,与既有行合并，避免完备性矩阵/报告里再多出一个孤立裸名行；
        # 若本轮找不到任何已知标签(如单独跑 --suite sampleapp 时)，则保留裸模型名，不影响判定。
        report_model_name = model_name
        if backend == "GGUF":
            existing = next((r.model_name for r in all_results
                              if r.model_name.startswith(f"{model_name} (")), None)
            if existing:
                report_model_name = existing
        config_path = Path(args.models) / model_name / "config.json"
        if not config_path.exists():
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=report_model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"缺失 config.json: {config_path}", skipped=True
            ))
            continue

        print(f"  运行 SampleApp 基本问答: {model_name}（backend={backend}）")
        try:
            start_time = time.time()
            proc = subprocess.run(
                [str(sampleapp_exe), "--config", str(config_path), str(input_path)],
                cwd=args.exe_dir, capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace"
            )
            latency = (time.time() - start_time) * 1000
            # SampleApp.cpp 自身只会主动 return 0/1/2/3；任何其它返回码(尤其是大的负数,对应
            # Windows NTSTATUS)代表进程被 OS 强制终止,是真正的崩溃,必须标记 crashed=True,
            # 不能和"程序自己判断失败后正常退出"混为一谈——否则报告里"崩溃"计数会漏掉这种情况。
            is_crash = _is_sampleapp_crash_exit_code(proc.returncode)
            proc_stdout = proc.stdout or ""
            passed = proc.returncode == 0 and len(proc_stdout.strip()) > 0
            skipped = False
            detail = f"backend={backend}, returncode={proc.returncode}, stdout_len={len(proc_stdout)}"
            if is_crash:
                detail += f", exit_code_hint={_describe_exit_code(proc.returncode)}"
            if not passed:
                detail += f", stderr={(proc.stderr or '')[:300]}"
                if not is_crash and backend == "mnn" and _MNN_INSUFFICIENT_MEMORY_MARKER in proc_stdout:
                    # 与 HTTP 端点 failure_reason=insufficient_memory 同一根因:
                    # MnnVerifier 在进程自身 stdout 日志中直接记录了这段精确文本,
                    # 是环境资源约束,不是代码缺陷,归类为 skipped 而非 failed。
                    skipped = True
                    detail += "; SKIP: 命中 MnnVerifier 内存不足拒绝加载的精确文本信号"
            # 模型缺陷标记(以服务端日志为准)：SampleApp 子进程的 stdout 已完整捕获在
            # proc_stdout 里(不同于 HTTP 场景需要按偏移量扫描共享日志文件),直接对这段
            # 完整文本扫描 [MODEL_DEFECT] 标记即可,天然只属于这一次调用,不会与其它请求混淆。
            defect = _scan_model_defect_text(proc_stdout)
            capability_issue, capability_reason = _capability_issue_from_model_defect(defect)
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=report_model_name,
                passed=passed, status_code=proc.returncode, latency_ms=latency,
                detail=detail, text_response=proc_stdout[:2000], text_prompt=prompt_text,
                crashed=is_crash, skipped=skipped,
                model_capability_issue=capability_issue, model_capability_reason=capability_reason
            ))
        except subprocess.TimeoutExpired as e:
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=report_model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"SampleApp 执行超时: {e}", crashed=True
            ))
        except Exception as e:
            all_results.append(TestResult(
                name=basic_chat_name, round_num=1, model_name=report_model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"测试异常: {e}"
            ))

        # ---- 差异化对照测试：GenieAPIService(HTTP,exe) vs GenieAPILibrary(SampleApp,dll) ----
        # 用途：QNN/多模态请求在 GenieAPIService 侧崩溃时，不能凭猜测把根因归咎于驱动/SDK
        # 或某个宿主特有的问题；这里用与 test_chat_multimodal_openai_style 完全相同的 OpenAI
        # 数组风格 content(文本+图片 data URI)构造输入，经 SampleApp.cpp 里"整段 JSON 原样
        # dump 后传给 api_Generate"的转发路径(见 SampleApp.cpp 第 271-337 行)，喂给同一个
        # ModelInputBuilder::ProcessArray 引擎代码——与 HTTP 端点是同一份共享源码，只是宿主
        # 不同(exe 用 SERVICE_SOURCES 编译，dll 额外链接 GenieAPILibrary.cpp 胶水层)。
        # 只要本轮任何 suite 已经跑过该模型的多模态用例(阶段2/full)，两边用的都是同一批
        # data_dir 素材池，具备可比性；单独跑 --suite sampleapp 时同样会独立抽取一次素材。
        # 判定规则：
        #   - 两边都崩溃 → 是共享引擎代码本身的真实 bug，与宿主无关；
        #   - 只有一边崩溃 → 该次崩溃与某个宿主特有的路径相关(而不是驱动/SDK)，需要针对性排查；
        #   - 两边都不崩溃 → 这次未复现，不代表问题不存在(崩溃可能是非确定性的)。
        # 报告里不在这里下结论，只如实记录 crashed/exit_code_hint，由后续汇总环节比对两边结果。
        multimodal_image_name = "SAMPLEAPP: multimodal_image (vs GenieAPIService 对照)"
        modality = detect_modality(model_name)
        if "image" in modality:
            image_path, image_err = _pick_random_asset_file(args.data_dir, "img", {".jpg", ".jpeg", ".png"})
            if image_path is None:
                all_results.append(TestResult(
                    name=multimodal_image_name, round_num=1, model_name=report_model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"素材缺失: {image_err}",
                    skipped=True, ignorable=True, ignore_reason="data_dir 下 img 素材缺失"
                ))
            else:
                ext = image_path.suffix.lower().lstrip(".")
                if ext == "jpg":
                    ext = "jpeg"
                image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
                mm_payload = {
                    "OptionParams": {"temperature": 0, "top_p": 0.95, "top_k": 40, "n_predict": 512, "n_ctx": 8192},
                    "stream": False,
                    "messages": [
                        {"role": "user", "content": [
                            {"type": "text", "text": random.choice(MULTIMODAL_PROMPTS)},
                            {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{image_b64}"}}
                        ]}
                    ]
                }
                mm_input_path = Path(args.out_dir) / f"_sampleapp_multimodal_input_{model_name.replace('/', '_')}.json"
                mm_input_path.write_text(json.dumps(mm_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  运行 SampleApp 多模态对照测试: {model_name}（image={image_path.name}）")
                try:
                    mm_start = time.time()
                    mm_proc = subprocess.run(
                        [str(sampleapp_exe), "--config", str(config_path), str(mm_input_path)],
                        cwd=args.exe_dir, capture_output=True, text=True, timeout=300,
                        encoding="utf-8", errors="replace"
                    )
                    mm_latency = (time.time() - mm_start) * 1000
                    mm_is_crash = _is_sampleapp_crash_exit_code(mm_proc.returncode)
                    mm_stdout = mm_proc.stdout or ""
                    mm_passed = mm_proc.returncode == 0 and len(mm_stdout.strip()) > 0
                    mm_detail = (f"backend={backend}, image={image_path.name}, "
                                  f"returncode={mm_proc.returncode}, stdout_len={len(mm_stdout)}")
                    if mm_is_crash:
                        mm_detail += f", exit_code_hint={_describe_exit_code(mm_proc.returncode)}"
                    if not mm_passed:
                        mm_detail += f", stderr={(mm_proc.stderr or '')[:300]}"
                    mm_defect = _scan_model_defect_text(mm_stdout)
                    mm_capability_issue, mm_capability_reason = _capability_issue_from_model_defect(mm_defect)
                    all_results.append(TestResult(
                        name=multimodal_image_name, round_num=1, model_name=report_model_name,
                        passed=mm_passed, status_code=mm_proc.returncode, latency_ms=mm_latency,
                        detail=mm_detail, text_response=mm_stdout[:2000],
                        text_prompt=mm_payload["messages"][0]["content"][0]["text"],
                        crashed=mm_is_crash,
                        model_capability_issue=mm_capability_issue, model_capability_reason=mm_capability_reason
                    ))
                except subprocess.TimeoutExpired as e:
                    all_results.append(TestResult(
                        name=multimodal_image_name, round_num=1, model_name=report_model_name,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"SampleApp 多模态对照测试执行超时: image={image_path.name}, {e}", crashed=True
                    ))
                except Exception as e:
                    all_results.append(TestResult(
                        name=multimodal_image_name, round_num=1, model_name=report_model_name,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"测试异常: image={image_path.name}, {e}"
                    ))


def run_graceful_shutdown_tests(args, models, all_results, all_crash_events):
    """验证三种后端在推理请求进行中被终止时，能走既有的优雅关闭路径正常退出，而不是被
    Windows 判定为异常崩溃（如 STATUS_ACCESS_VIOLATION 等 NTSTATUS 崩溃特征码）。每种
    后端各选一个已发现的代表模型，用 CTRL_BREAK_EVENT（Windows 上唯一能只定向到本进程、
    不影响同控制台其它进程的信号；服务已注册 SetConsoleCtrlHandler 复用与 Ctrl+C 相同的
    ServiceStop() 路径）在其正在处理一次长耗时流式推理请求期间终止服务进程。仅支持本地
    模式（需要直接控制服务进程发送信号），远程模式/非 Windows 环境下精确跳过。"""
    print(f"\n{'='*60}")
    print("阶段: 推理中终止进程 → 优雅退出验证")
    print(f"{'='*60}")

    test_name = "GRACEFUL_SHUTDOWN: terminate during inference"

    if args.remote:
        all_results.append(TestResult(
            name=test_name, round_num=1, model_name="_graceful_shutdown_",
            passed=False, status_code=0, latency_ms=0,
            detail="远程模式无法直接控制服务进程发送终止信号，跳过优雅关闭验证", skipped=True
        ))
        return
    if os.name != "nt" or not hasattr(signal, "CTRL_BREAK_EVENT"):
        all_results.append(TestResult(
            name=test_name, round_num=1, model_name="_graceful_shutdown_",
            passed=False, status_code=0, latency_ms=0,
            detail="当前环境不支持 CTRL_BREAK_EVENT（非 Windows），跳过优雅关闭验证", skipped=True
        ))
        return

    targets = {}
    for backend_key in ("qnn", "GGUF", "mnn"):
        candidates = sorted(m for m in models if infer_backend(m)[0] == backend_key)
        if candidates:
            targets[backend_key] = candidates[0]

    for backend_key in ("qnn", "GGUF", "mnn"):
        model_name = targets.get(backend_key)
        result_model_name = f"_graceful_shutdown_ ({backend_key})"
        if not model_name:
            all_results.append(TestResult(
                name=test_name, round_num=1, model_name=result_model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"未发现 {backend_key} 后端模型，跳过该后端的优雅关闭验证", skipped=True
            ))
            continue

        config_path = Path(args.models) / model_name / "config.json"
        if not config_path.exists():
            all_results.append(TestResult(
                name=test_name, round_num=1, model_name=result_model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"缺失 config.json: {config_path}", skipped=True
            ))
            continue

        # 三个后端在本函数内连续重启,上一个后端的进程刚被 _force_kill() 结束时,其监听端口
        # 可能尚处于 TIME_WAIT/延迟释放阶段;不等待就直接 start() 会命中 isPortAvailable()
        # 判定为占用、打印"service already exist."并以 exit_code=0 立即退出，被误判为崩溃。
        wait_port_closed(args.host, args.port, timeout=15)
        svc = ServiceManager(args.exe_dir, args.host, args.port)
        svc._log_dir = args.out_dir
        start_time = time.time()
        try:
            print(f"  [{backend_key}] 启动服务用于优雅关闭验证: {model_name}")
            svc.start(str(config_path))
            if not wait_port_open(args.host, args.port, timeout=180, process=svc.process):
                all_results.append(TestResult(
                    name=test_name, round_num=1, model_name=result_model_name,
                    passed=False, status_code=0, latency_ms=(time.time() - start_time) * 1000,
                    detail="端口 180s 内未可连接", skipped=True
                ))
                continue

            pid = svc.process.pid
            chat_body = {
                "model": model_name,
                "messages": [{"role": "user", "content":
                    "Write a very long, detailed step-by-step essay about the history of computing, "
                    "at least 3000 words."}],
                "stream": True,
                "max_tokens": 2048,
            }

            first_chunk_event = threading.Event()

            def _do_stream():
                try:
                    with requests.post(f"http://{args.host}:{args.port}/v1/chat/completions",
                                        json=chat_body, stream=True, timeout=120) as r:
                        for line in r.iter_lines():
                            if line:
                                first_chunk_event.set()
                                break
                        for _ in r.iter_lines():  # 继续消费剩余流,直到进程被终止打断连接
                            pass
                except Exception:
                    pass  # 进程即将被终止,连接异常是预期结果,这里只负责驱动请求,不关心具体异常

            stream_thread = threading.Thread(target=_do_stream, daemon=True)
            stream_thread.start()

            if not first_chunk_event.wait(timeout=60):
                all_results.append(TestResult(
                    name=test_name, round_num=1, model_name=result_model_name,
                    passed=False, status_code=0, latency_ms=(time.time() - start_time) * 1000,
                    detail="60s 内未收到推理流式响应的第一个数据块，无法确认推理是否已开始", skipped=True
                ))
                continue

            time.sleep(1.5)  # 确保终止信号发出时推理正在进行中,而不是刚开始加载/已经结束
            print(f"  [{backend_key}] 向 PID {pid} 发送 CTRL_BREAK_EVENT（推理进行中）...")
            os.kill(pid, signal.CTRL_BREAK_EVENT)

            try:
                exit_code = svc.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                exit_code = None
            stream_thread.join(timeout=5)
            latency = (time.time() - start_time) * 1000

            if exit_code is None:
                all_results.append(TestResult(
                    name=test_name, round_num=1, model_name=result_model_name,
                    passed=False, status_code=0, latency_ms=latency,
                    detail="推理进行中发送终止信号后 30s 内未退出（挂起，非崩溃但同样是需要关注的真实缺陷）",
                    crashed=True, ignorable=False
                ))
                all_crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(), model_name=model_name, round_num=1,
                    endpoint=test_name, detail="终止信号发出后 30s 内进程未退出（挂起）",
                    log_tail=svc.read_log_tail(svc._stderr_log, 50)
                ))
                continue

            log_tail = svc.read_log_tail(svc._stdout_log, 80)
            log_tail_lower = log_tail.lower()
            graceful_marker_present = "service stopped successfully" in log_tail_lower
            watchdog_triggered = (exit_code == _SHUTDOWN_WATCHDOG_EXIT_CODE or
                                   _SHUTDOWN_WATCHDOG_LOG_MARKER in log_tail_lower)
            crash_hint = _WINDOWS_EXIT_CODE_HINTS.get(exit_code)
            if watchdog_triggered:
                # 看门狗兜底强制终止：底层推理调用未在阈值内响应 ModelManager::UnloadModel()
                # 发出的 Stop() 信号，进程被主动强制终止而不是无限期挂起——这是已知的兜底
                # 路径，不是崩溃（不落在 _WINDOWS_EXIT_CODE_HINTS 崩溃特征码表内），也没有走完
                # 常规的优雅关闭日志，因此单独归类：不算 passed（未能优雅关闭），也不算 crashed。
                all_results.append(TestResult(
                    name=test_name, round_num=1, model_name=result_model_name,
                    passed=False, status_code=exit_code, latency_ms=latency,
                    detail=(f"backend={backend_key}, model={model_name}, "
                            f"exit_code={_describe_exit_code(exit_code)}, "
                            f"看门狗兜底强制终止(非崩溃): 底层推理调用未在观察窗口内响应停止信号"),
                    crashed=False, ignorable=False,
                    response_data={"backend": backend_key, "exit_code": exit_code, "watchdog_forced_exit": True}
                ))
                continue
            is_graceful = crash_hint is None and graceful_marker_present
            detail = (f"backend={backend_key}, model={model_name}, exit_code={_describe_exit_code(exit_code)}, "
                      f"graceful_shutdown_log_marker={'found' if graceful_marker_present else 'NOT found'}")
            all_results.append(TestResult(
                name=test_name, round_num=1, model_name=result_model_name,
                passed=is_graceful, status_code=exit_code, latency_ms=latency,
                detail=detail, crashed=(crash_hint is not None), ignorable=False,
                response_data={"backend": backend_key, "exit_code": exit_code,
                                "exit_code_hint": crash_hint or "", "graceful_marker": graceful_marker_present}
            ))
            if crash_hint is not None:
                all_crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(), model_name=model_name, round_num=1,
                    endpoint=test_name,
                    detail=f"终止信号发出后进程以崩溃特征退出: {_describe_exit_code(exit_code)}",
                    log_tail=log_tail
                ))
        except (RuntimeError, FileNotFoundError) as e:
            all_results.append(TestResult(
                name=test_name, round_num=1, model_name=result_model_name,
                passed=False, status_code=0, latency_ms=(time.time() - start_time) * 1000,
                detail=f"启动失败: {str(e)[:300]}", crashed=True, ignorable=False
            ))
        except Exception as e:
            all_results.append(TestResult(
                name=test_name, round_num=1, model_name=result_model_name,
                passed=False, status_code=0, latency_ms=(time.time() - start_time) * 1000,
                detail=f"测试异常: {e}"
            ))
        finally:
            svc._force_kill()
