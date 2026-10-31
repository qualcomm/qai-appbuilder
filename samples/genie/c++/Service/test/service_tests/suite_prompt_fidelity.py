#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import json
import re
import time
from pathlib import Path
from datetime import datetime
import requests

from .common import (
    CrashEvent,
    TestResult,
    _trace_request,
    _trace_snapshot,
    detect_modality,
    infer_backend,
    wait_port_closed,
    wait_port_open,
)
from .managers import ServiceManager


# ============================================================================
# prompt_fidelity suite —— 提示词压缩「保真度 + 可观测」量化回归
# ============================================================================
# 设计要点（改动前必读，避免把断言建在不成立的取证通道上）：
#   1. 取证通道只认 `[Prompt] [...]:\n<最终提示词>\n------------` 这一段（三后端统一由
#      genie_interface.cpp/mnn.cpp/llama_cpp.cpp 无条件打印），它是真正喂给推理引擎的文本。
#      绝不能整份日志做子串搜索：-g -g（level 2）会把**原始请求 JSON** 打进日志，原文里当然
#      带着 canary，直接搜日志必然假阳性。因此本套件只传单个 -g（level 1）。
#   2. 默认日志级别是 kWarning（config.h:63），`[Normal] Final prompt - Tokens: N` 是 kInfo，
#      不显式抬高级别就抓不到 token 预算证据 —— 启动时固定追加 -d 3。
#   3. **必须以 -n -1 启动**：整条压缩链（PreFilterMessages + FitMessagesToContext + 工具定义
#      Tier 降级 + Phase -1 摘要）只在 `IsStatelessMode()`（numResponse == -1，见
#      model_instance_config.h:69）时执行，而默认值是 30（model_config.h:721）。不传 -n -1 时
#      本套件测到的是「压缩被设计性关闭」的分支，所有保真断言都没有意义。
#   4. 必须自己起服务（需要 stdout 落盘 + 自定义命令行），远程模式精确跳过而不是假装通过。
_PF_CANARY_HEAD = "CANARY-HEAD-8F31A2"
_PF_CANARY_MID = "CANARY-MID-5C7B90"
_PF_CANARY_TAIL = "CANARY-TAIL-EXIT-CODE-137"
_PF_CANARY_JSON_FIRST = "CANARY-JSON-FIRST-11AA"
_PF_CANARY_JSON_LAST = "CANARY-JSON-LAST-99ZZ"
_PF_CANARY_ZH_TAIL = "CANARY-ZH-TAIL-EXIT-137"
_PF_CANARY_IMG_TEXT = "CANARY-IMG-TEXT-4D2E1"
# 场景 C 专用：round A（纯文本+skill）自己的请求内容 canary，与 round B 的
# `_PF_CANARY_IMG_TEXT` 对称。Step6 本轮实测发现直接拿 "weather" 关键字判定跨轮
# 串扰是一个测试设计缺陷——weather 技能在 scenario_a_skill_weather() 里被
# `set_skill_run_mode(..., "both")` 设成了全局持久状态，一旦设置就在整个测试会话
# 剩余时间里对所有模型的所有请求可见，与"是否发生了跨模型串扰"完全无关；用一个
# 嵌入在 round A 用户消息文本里的、不受相关性过滤影响的唯一 canary 才是与 round B
# 对称的正确判据（同 round B 的 `_PF_CANARY_IMG_TEXT` 一样，是消息 content 本身的
# 一部分，不经过技能相关性过滤这条已知有零分清空 bug 的路径）。
_PF_CANARY_TXT_A = "CANARY-TXT-A-8B3F0"
_PF_DROP_PLACEHOLDER_HINT = "earlier message(s) omitted"

# 最小 1x1 像素透明 PNG（占位图，与真实一致的合法 PNG 二进制，无需任何素材文件），
# 仅用于验证多模态 content-parts 请求链路本身能正常处理，不要求模型真的看懂图片内容。
_PF_MIN_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                   "+A8AAQUBAScY42YAAAAASUVORK5CYII=")

# 最终提示词块：非贪婪匹配到分隔线为止。
# 注意：GenieAPIService.exe 的 stdout 被 Windows CRT 以文本模式重定向到文件时，每个
# 源码里的 "\n" 都会被自动转换成 "\r\n"（这不是我们代码的行为，是 CRT 标准输出的文本
# 模式转换）；`\n` 两侧都必须放宽为 `\r?\n`，否则 `-{6,}\n` 在真实遇到 `-{6,}\r\n` 时会
# 匹配失败，导致误判为"取证通道失效"（此前一版曾误诊为模型分支问题，已核实并非如此）。
_PF_PROMPT_BLOCK_RE = re.compile(r"\[Prompt\] \[[^\r\n]*\r?\n(.*?)\r?\n-{6,}\r?\n", re.S)
# token 预算证据行（Normal/Harmony 两条路径同格式）。
_PF_FINAL_TOKENS_RE = re.compile(r"Final prompt - Tokens: (\d+)")

# PromptLedger 响应头（Step 4 引入）。基线阶段这些头不存在，对应用例如实判失败，
# 这正是「现状不可观测」这条差距的机械证据。
_PF_LEDGER_HEADERS = (
    "X-Genie-Prompt-Context-Size",
    "X-Genie-Prompt-Tokens-In",
    "X-Genie-Prompt-Tokens-Out",
    "X-Genie-Prompt-Messages-In",
    "X-Genie-Prompt-Messages-Kept",
    "X-Genie-Prompt-Messages-Dropped",
    "X-Genie-Prompt-Messages-Truncated",
    "X-Genie-Prompt-Tools-Total",
    "X-Genie-Prompt-Tools-Kept",
    "X-Genie-Prompt-Tools-Tier",
    "X-Genie-Prompt-Skills-Total",
    "X-Genie-Prompt-Skills-Kept",
    "X-Genie-Prompt-Emergency-Truncated",
    "X-Genie-Prompt-Summarized",
    # D3（Step2）新增：ComputeRelevanceTokenBudget(kSkills) 实际算出的 skills 分区预算。
    # legacy 档（budget_partition.enabled=false）与 optimized 档（竞争时分区生效）下
    # 该值必然不同，是「D3 开关真实生效」的机械证据，不需要读日志判断。
    "X-Genie-Prompt-Skills-Budget-Tokens",
    # D2（Step3）新增：技能目录三档渐进披露的各档计数。三项之和恒等于 Skills-Kept；
    # skill_disclosure.enabled=false 时逐字节退化为 L2==Kept、L1/L0 恒为 0，
    # 这是「三档开关真实生效」的机械证据（不需要读日志判断）。
    "X-Genie-Prompt-Skills-L2",
    "X-Genie-Prompt-Skills-L1",
    "X-Genie-Prompt-Skills-L0",
)


class _PromptLogProbe:
    """按字节偏移增量读取服务 stdout 日志，把「本次请求产生的日志」与历史日志隔离开。

    每次请求前 mark()，请求后 read_new() 只拿新增内容 —— 否则上一个用例埋的 canary
    会污染下一个用例的断言（同一个服务进程、同一份日志文件）。
    """

    def __init__(self, log_path):
        self.log_path = Path(log_path) if log_path else None
        self._offset = 0

    def mark(self):
        self._offset = self._size()

    def _size(self):
        try:
            return self.log_path.stat().st_size if (self.log_path and self.log_path.exists()) else 0
        except OSError:
            return 0

    def read_new(self):
        if not self.log_path or not self.log_path.exists():
            return ""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError as e:
            return f"<read error: {e}>"
        return data.decode("utf-8", errors="replace")

    def last_prompt_block(self):
        """本次请求产生的最后一个最终提示词块；抓不到返回空字符串（调用方据此判 skip）。"""
        blocks = _PF_PROMPT_BLOCK_RE.findall(self.read_new())
        return blocks[-1] if blocks else ""

    def final_prompt_tokens(self):
        """本次请求日志里最后一次 `Final prompt - Tokens: N`；抓不到返回 None。"""
        hits = _PF_FINAL_TOKENS_RE.findall(self.read_new())
        return int(hits[-1]) if hits else None


def _pf_filler_lines(tag, count):
    """生成体量填充行（每行约 100 字符），内容里刻意不含任何高信号词，
    确保「无高信号行」等价性用例的语义成立。"""
    return "\n".join(
        f"[{tag}] line {i:05d} routine progress record, nothing notable here, padding padding padding"
        for i in range(count)
    )


def _pf_build_long_tool_output():
    """约 80K 字符的超长工具输出：头/中/尾三处唯一 canary，尾部是真实构建失败常见形态
    （报错行 + 退出码），这正是当前「保头弃尾」截断必然丢掉的部分。"""
    head = f">>> build log start marker {_PF_CANARY_HEAD}\n"
    mid_marker = f"[checkpoint] halfway marker {_PF_CANARY_MID}\n"
    tail = (
        "npm ERR! Build step failed with a non-zero status.\n"
        "npm ERR! Traceback (most recent call last):\n"
        "npm ERR!   File \"/app/build.py\", line 412, in <module>\n"
        "npm ERR! RuntimeError: linker terminated abnormally\n"
        f"process exited with {_PF_CANARY_TAIL}\n"
    )
    body_a = _pf_filler_lines("pre", 400)
    body_b = _pf_filler_lines("post", 400)
    return head + body_a + "\n" + mid_marker + body_b + "\n" + tail


def _pf_build_json_array_tool_output():
    """JSON 数组型工具响应：首项与末项各埋唯一标记。当前实现只保留前缀元素，
    末项（往往是汇总/失败项）必然被丢 —— 这条用例就是该差距的直接回归。"""
    items = [{"index": 0, "name": _PF_CANARY_JSON_FIRST, "status": "ok",
              "note": "first entry of the result array"}]
    for i in range(1, 299):
        items.append({"index": i, "name": f"entry-{i:04d}", "status": "ok",
                      "note": "routine entry padding padding padding padding padding padding"})
    items.append({"index": 299, "name": _PF_CANARY_JSON_LAST, "status": "failed",
                  "note": "summary entry: 1 failed, exit code 137"})
    return json.dumps(items, ensure_ascii=False)


def _pf_build_chinese_long_text():
    """中文长文本：字符/token 比与英文差异显著，用于回归「阈值按字符算导致压缩力度不可控」。"""
    para = ("本段用于构造中文长文本样本，验证按字符计算的压缩阈值在中文语料下是否仍能把最终提示词"
            "控制在上下文预算之内。这里刻意使用连续的中文叙述文本，不含任何英文关键字。")
    return "\n".join(f"第{i:04d}段：{para}" for i in range(260)) + f"\n最终结论：{_PF_CANARY_ZH_TAIL}\n"


def _pf_build_tool_schemas(count=17):
    """count 个完整 JSON Schema 工具定义，逼出 PromptOptimizer 的 Tier 降级。
    工具名沿用 prompt_optimizer.cpp 已内置的通用名（read/write/...）之外再补足数量，
    实现层不做任何调用方判别，这里也只是「一份体量足够大的通用请求」。"""
    base_names = ["read", "write", "edit", "search", "list_dir", "run_command", "grep",
                  "fetch_url", "create_file", "delete_file", "move_file", "copy_file",
                  "git_status", "git_commit", "run_tests", "format_code", "analyze_deps"]
    tools = []
    for name in base_names[:count]:
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (f"Perform the {name} operation. This description is intentionally verbose "
                                f"so that the full JSON schema of all tools cannot fit into a small "
                                f"context window without tier degradation."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or relative target path."},
                        "content": {"type": "string", "description": "Payload used by the operation."},
                        "recursive": {"type": "boolean", "description": "Whether to apply recursively."},
                        "limit": {"type": "integer", "description": "Maximum number of entries to process."},
                    },
                    "required": ["path"],
                },
            },
        })
    return tools


def _pf_tool_roundtrip_messages(tool_content, user_followup):
    """构造 assistant(tool_calls) → tool(超长响应) → user(追问) 的合法三段结构。"""
    return [
        {"role": "system", "content": "You are a build assistant. Answer briefly."},
        {"role": "user", "content": "Please read the build log and tell me what happened."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_pf_1", "type": "function",
             "function": {"name": "read", "arguments": "{\"path\": \"build.log\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_pf_1", "name": "read", "content": tool_content},
        {"role": "user", "content": user_followup},
    ]


def _pf_long_history_messages(rounds=24):
    """20+ 轮长历史，触发 FitMessagesToContext 的「整条丢弃」路径。"""
    msgs = [{"role": "system", "content": "You are a helpful assistant. Answer briefly."}]
    for i in range(rounds):
        msgs.append({"role": "user", "content":
                     f"Round {i:02d} question: {_pf_filler_lines(f'q{i}', 12)}"})
        msgs.append({"role": "assistant", "content":
                     f"Round {i:02d} answer: {_pf_filler_lines(f'a{i}', 12)}"})
    msgs.append({"role": "user", "content": "Now summarize in one short sentence."})
    return msgs


def _pf_build_min_png_data_uri():
    """最小 1x1 像素 PNG 占位图（透明像素）的 OpenAI content-parts data URI，
    用于「纯图片单问」用例——不要求模型真的看懂图片内容，只验证请求链路本身
    能正常处理多模态 content 并保留伴随的文字说明。"""
    return f"data:image/png;base64,{_PF_MIN_PNG_B64}"


def _pf_result(name, model_name, passed, status_code, latency_ms, detail,
               response_data=None, skipped=False):
    return TestResult(
        name=name, round_num=1, model_name=model_name,
        passed=passed, status_code=status_code, latency_ms=latency_ms,
        detail=detail, skipped=skipped, ignorable=False,
        response_data=response_data or {},
    )


def _pf_post_chat(host, port, body, timeout=900):
    return requests.post(f"http://{host}:{port}/v1/chat/completions", json=body, timeout=timeout)


def _pf_check_ledger_headers(headers):
    """返回 (缺失字段列表, 非法值字段列表, 已解析的数值字典)。"""
    missing, invalid, values = [], [], {}
    for h in _PF_LEDGER_HEADERS:
        raw = headers.get(h)
        if raw is None:
            missing.append(h)
            continue
        try:
            values[h] = int(str(raw).strip())
        except ValueError:
            invalid.append(f"{h}={raw!r}")
    return missing, invalid, values


def _run_prompt_fidelity_suite(args, models, remote_mode, out_dir):
    """--suite prompt_fidelity：提示词压缩的「信息保真度 + 对调用方可观测」量化回归。

    全部断言可机械核验，不依赖人工读日志、也不判模型答得对不对：
      1. 超长工具输出的**尾部** canary 是否仍在最终提示词里（当前「保头弃尾」的最大漏洞）；
      2. JSON 数组型响应的**末项**是否仍在（当前只保留前缀元素）；
      3. 长历史整条丢弃后是否留下占位痕迹；
      4. 17 个完整工具 Schema + 中文长文本下最终 token 是否落在上下文预算内；
      5. 非流式响应头 / 流式 status 帧是否回报同一套压缩账本；
      6. 中文问英文命名工具（纯天气单问）/ 纯图片单问两类最小化核心场景是否仍保真。
    """
    all_results = []
    all_crash_events = []
    all_perf_samples = []
    suite_model = "_prompt_fidelity_"

    if remote_mode:
        all_results.append(_pf_result(
            "PROMPT_FIDELITY: suite precondition", suite_model, False, 0, 0,
            "远程模式无法自定义服务命令行（需 -n -1 -g -d 3）也拿不到 stdout 日志，跳过提示词保真套件",
            skipped=True))
        return all_results, all_perf_samples, all_crash_events

    name_filter = {n.strip() for n in args.model_name.split(",")} if args.model_name else None
    candidates = [m for m in models if not name_filter or m in name_filter]
    # 最终提示词全文三后端都会打印，但 QNN 是本项目的常开主路径，优先选它，保证基线可复现。
    target = next((m for m in candidates if infer_backend(m)[0] == "qnn"), None) or (
        candidates[0] if candidates else None)
    if not target:
        all_results.append(_pf_result(
            "PROMPT_FIDELITY: suite precondition", suite_model, False, 0, 0,
            f"未发现可用模型（--model_name={args.model_name}）", skipped=True))
        return all_results, all_perf_samples, all_crash_events

    config_path = Path(args.models) / target / "config.json"
    if not config_path.exists():
        all_results.append(_pf_result(
            "PROMPT_FIDELITY: suite precondition", suite_model, False, 0, 0,
            f"缺失 config.json: {config_path}", skipped=True))
        return all_results, all_perf_samples, all_crash_events

    print(f"\n{'='*60}")
    print(f"阶段: 提示词压缩保真度与可观测性回归（模型: {target}）")
    print(f"{'='*60}")

    wait_port_closed(args.host, args.port, timeout=15)
    svc = ServiceManager(args.exe_dir, args.host, args.port)
    svc._log_dir = args.out_dir
    try:
        # -n -1：**关键**。整条压缩链只在 IsStatelessMode()（numResponse == -1）时执行，
        # 默认值 30 下 PreFilterMessages/FitMessagesToContext/工具 Tier 降级全部不跑，
        # 提示词会原样送进引擎并由引擎侧 "Context Size was exceeded." 默默截断。
        # -g：prompt 压缩调试日志 level 1（**不能用 -g -g**，level 2 会把原始请求 JSON
        # 打进日志，canary 断言必然假阳性）；-d 3：抬到 Info，否则 Final prompt token 行被过滤。
        svc.start(str(config_path), extra_args=["-n", "-1", "-g", "-d", "3"])
        if not wait_port_open(args.host, args.port, timeout=180, process=svc.process):
            all_results.append(_pf_result(
                "PROMPT_FIDELITY: suite precondition", suite_model, False, 0, 0,
                "端口 180s 内未可连接", skipped=True))
            return all_results, all_perf_samples, all_crash_events

        probe = _PromptLogProbe(svc._stdout_log)
        context_size = 0
        try:
            r = requests.post(f"http://{args.host}:{args.port}/contextsize",
                              json={"model": target}, timeout=120)
            if r.status_code == 200:
                context_size = int(next((e.get("contextsize", 0) for e in r.json().get("models", [])
                                         if e.get("model") == target), 0))
        except Exception as e:
            print(f"  [prompt_fidelity] 获取 contextsize 失败: {e}")
        print(f"  [prompt_fidelity] contextsize={context_size}")

        _pf_case_tail_canary(args, target, probe, all_results, all_crash_events)
        _pf_case_json_tail_item(args, target, probe, all_results, all_crash_events)
        _pf_case_drop_placeholder(args, target, probe, all_results, all_crash_events)
        _pf_case_tools_budget(args, target, probe, context_size, all_results, all_crash_events)
        _pf_case_chinese_budget(args, target, probe, context_size, all_results, all_crash_events)
        _pf_case_no_high_signal(args, target, probe, all_results, all_crash_events)
        _pf_case_ledger_headers(args, target, probe, context_size, all_results, all_crash_events)
        _pf_case_image_single_question(args, target, probe, all_results, all_crash_events)
        _pf_case_cross_language_relevance(args, target, probe, all_results, all_crash_events)
        _pf_case_relevance_still_filters(args, target, probe, all_results, all_crash_events)
        _pf_case_stream_ledger(args, target, all_results, all_crash_events)
    except (RuntimeError, FileNotFoundError) as e:
        all_results.append(_pf_result(
            "PROMPT_FIDELITY: suite precondition", suite_model, False, 0, 0,
            f"服务启动失败: {str(e)[:300]}", skipped=True))
    finally:
        svc.stop()
        svc._force_kill()

    return all_results, all_perf_samples, all_crash_events


def _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate):
    """统一的「发一次请求 → 抓本次日志 → 交给 evaluate 判定」外壳，
    把 HTTP/日志异常与断言逻辑分开，避免每个用例各写一遍 try/except。"""
    start = time.time()
    _trace_request(model, name, 1, "prompt_fidelity")
    if probe is not None:
        probe.mark()
    try:
        resp = _pf_post_chat(args.host, args.port, body)
    except Exception as e:
        latency = (time.time() - start) * 1000
        all_results.append(_pf_result(name, model, False, 0, latency,
                                      f"请求异常（服务可能已崩溃）: {str(e)[:300]}"))
        all_crash_events.append(CrashEvent(
            timestamp=datetime.now().isoformat(), model_name=model, round_num=1,
            endpoint=name, detail=f"prompt_fidelity 请求异常: {str(e)[:200]}",
            request_history=_trace_snapshot()))
        return
    latency = (time.time() - start) * 1000
    prompt_text = probe.last_prompt_block() if probe is not None else ""
    try:
        passed, detail, data = evaluate(resp, prompt_text, probe)
    except Exception as e:
        all_results.append(_pf_result(name, model, False, resp.status_code, latency,
                                      f"断言执行异常: {str(e)[:300]}"))
        return
    all_results.append(_pf_result(name, model, passed, resp.status_code, latency, detail, data))


def _pf_case_tail_canary(args, model, probe, all_results, all_crash_events):
    """场景1：约 80K 字符超长工具输出，尾部含退出码。断言尾部 canary 仍在最终提示词里。"""
    name = "PROMPT_FIDELITY: tail canary survives oversized tool output"
    tool_content = _pf_build_long_tool_output()
    body = {
        "model": model,
        "messages": _pf_tool_roundtrip_messages(
            tool_content, "What exit code did the build finish with? Answer in one short sentence."),
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能从服务日志抓取到最终提示词块（[Prompt] ... 分隔线），取证通道失效", {}
        head_in = _PF_CANARY_HEAD in prompt_text
        mid_in = _PF_CANARY_MID in prompt_text
        tail_in = _PF_CANARY_TAIL in prompt_text
        compressed = len(prompt_text) < len(tool_content)
        data = {"tool_output_chars": len(tool_content), "final_prompt_chars": len(prompt_text),
                "head_canary": head_in, "mid_canary": mid_in, "tail_canary": tail_in,
                "compressed": compressed}
        if not compressed:
            return False, (f"最终提示词未被压缩（{len(prompt_text)} chars ≥ 工具输出 "
                           f"{len(tool_content)} chars），该用例失去意义，请换更小上下文的模型"), data
        detail = (f"tool_output={len(tool_content)} chars → final_prompt={len(prompt_text)} chars; "
                  f"head={head_in}, mid={mid_in}, tail={tail_in}")
        return tail_in, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_json_tail_item(args, model, probe, all_results, all_crash_events):
    """场景2：JSON 数组型工具响应，断言首项与末项均保留、中部带省略标记。"""
    name = "PROMPT_FIDELITY: json array keeps last item"
    tool_content = _pf_build_json_array_tool_output()
    body = {
        "model": model,
        "messages": _pf_tool_roundtrip_messages(
            tool_content, "Did any entry fail? Answer in one short sentence."),
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        first_in = _PF_CANARY_JSON_FIRST in prompt_text
        last_in = _PF_CANARY_JSON_LAST in prompt_text
        omitted_mark = "items omitted" in prompt_text
        compressed = len(prompt_text) < len(tool_content)
        data = {"tool_output_chars": len(tool_content), "final_prompt_chars": len(prompt_text),
                "first_item": first_in, "last_item": last_in, "omitted_marker": omitted_mark,
                "compressed": compressed}
        if not compressed:
            return False, (f"最终提示词未被压缩（{len(prompt_text)} ≥ {len(tool_content)} chars），"
                           f"该用例失去意义"), data
        detail = (f"json_array={len(tool_content)} chars → final_prompt={len(prompt_text)} chars; "
                  f"first={first_in}, last={last_in}, omitted_marker={omitted_mark}")
        return (first_in and last_in), detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_drop_placeholder(args, model, probe, all_results, all_crash_events):
    """场景3：24 轮长历史触发整条丢弃，断言保留区首部出现丢弃占位 +（Step 4 后）响应头
    Messages-Dropped > 0。丢弃占位是让模型知道「这里曾有内容」的唯一线索。"""
    name = "PROMPT_FIDELITY: dropped messages leave a placeholder"
    body = {
        "model": model,
        "messages": _pf_long_history_messages(24),
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        placeholder_in = _PF_DROP_PLACEHOLDER_HINT in prompt_text
        missing, invalid, values = _pf_check_ledger_headers(resp.headers)
        dropped = values.get("X-Genie-Prompt-Messages-Dropped")
        data = {"final_prompt_chars": len(prompt_text), "drop_placeholder": placeholder_in,
                "ledger_headers_missing": missing, "messages_dropped": dropped}
        detail = (f"drop_placeholder={placeholder_in}; messages_dropped="
                  f"{dropped if dropped is not None else 'header 缺失'}; "
                  f"final_prompt={len(prompt_text)} chars")
        if dropped is not None and dropped == 0:
            # 没触发丢弃 → 占位缺席是正确行为，本轮无从验证，如实跳过而不是误判失败。
            return False, detail + "；本轮未触发整条丢弃，无法验证占位痕迹", data
        return placeholder_in, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_tools_budget(args, model, probe, context_size, all_results, all_crash_events):
    """场景4：17 个完整工具 Schema，断言最终 token 落在上下文预算内、且响应头账本齐全。"""
    name = "PROMPT_FIDELITY: 17 tool schemas stay within budget"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a coding agent. Use tools when needed."},
            {"role": "user", "content": "List the files under the project root."},
        ],
        "tools": _pf_build_tool_schemas(17),
        "stream": False,
        "max_tokens": 64,
    }

    def evaluate(resp, prompt_text, probe_obj):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        final_tokens = probe_obj.final_prompt_tokens() if probe_obj else None
        log_text = probe_obj.read_new() if probe_obj else ""
        exceeds_msg = "exceeds context size" in log_text
        missing, invalid, values = _pf_check_ledger_headers(resp.headers)
        data = {"final_prompt_tokens": final_tokens, "context_size": context_size,
                "system_prompt_exceeds_log": exceeds_msg,
                "ledger_headers_missing": missing, "ledger_headers_invalid": invalid,
                "tools_tier": values.get("X-Genie-Prompt-Tools-Tier")}
        if final_tokens is None:
            return False, "未能从日志抓取 `Final prompt - Tokens: N`（是否漏传 -d 3？）", data
        within = context_size <= 0 or final_tokens <= context_size
        detail = (f"final_prompt_tokens={final_tokens}, context_size={context_size}, "
                  f"within_budget={within}, exceeds_log={exceeds_msg}, "
                  f"tools_tier={data['tools_tier'] if data['tools_tier'] is not None else 'header 缺失'}")
        return (within and not exceeds_msg), detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_chinese_budget(args, model, probe, context_size, all_results, all_crash_events):
    """场景5：中文长文本（字符/token 比与英文差异大），断言 token 口径下最终 token 不越界。
    这是「阈值按字符算导致压缩力度不可控」这条差距的直接回归。"""
    name = "PROMPT_FIDELITY: chinese long text stays within token budget"
    zh = _pf_build_chinese_long_text()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个中文助手，回答要简短。"},
            {"role": "user", "content": zh},
            {"role": "user", "content": "请用一句话说明最终结论。"},
        ],
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, probe_obj):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        final_tokens = probe_obj.final_prompt_tokens() if probe_obj else None
        tail_in = _PF_CANARY_ZH_TAIL in prompt_text if prompt_text else False
        data = {"zh_chars": len(zh), "final_prompt_tokens": final_tokens,
                "context_size": context_size, "zh_tail_canary": tail_in}
        if final_tokens is None:
            return False, "未能从日志抓取 `Final prompt - Tokens: N`", data
        within = context_size <= 0 or final_tokens <= context_size
        detail = (f"zh_input={len(zh)} chars, final_prompt_tokens={final_tokens}, "
                  f"context_size={context_size}, within_budget={within}, zh_tail={tail_in}")
        return within, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_no_high_signal(args, model, probe, all_results, all_crash_events):
    """边界：内容里没有任何高信号行（纯正常输出）时，行为必须与关闭高信号提取时一致 ——
    仍然是「头部保留 + 不越界 + 200」，不能因为找不到高信号行而崩溃或吐空提示词。"""
    name = "PROMPT_FIDELITY: no high-signal lines behaves like baseline"
    tool_content = _pf_filler_lines("plain", 800)
    body = {
        "model": model,
        "messages": _pf_tool_roundtrip_messages(
            tool_content, "Summarize the log in one short sentence."),
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        head_kept = "[plain] line 00000" in prompt_text
        data = {"final_prompt_chars": len(prompt_text), "head_kept": head_kept}
        return head_kept, f"head_kept={head_kept}, final_prompt={len(prompt_text)} chars", data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_ledger_headers(args, model, probe, context_size, all_results, all_crash_events):
    """场景：非流式响应必须带全套 X-Genie-Prompt-* 账本响应头，且全部为合法整数、
    Tokens-Out ≤ Context-Size。空 tools/skills 场景下字段应为 0 而不是缺失。"""
    name = "PROMPT_FIDELITY: non-stream reports ledger via response headers"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Answer briefly."},
            {"role": "user", "content": "Say hello in one short sentence."},
        ],
        "stream": False,
        "max_tokens": 32,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        missing, invalid, values = _pf_check_ledger_headers(resp.headers)
        tokens_out = values.get("X-Genie-Prompt-Tokens-Out")
        hdr_ctx = values.get("X-Genie-Prompt-Context-Size")
        data = {"missing": missing, "invalid": invalid, "values": values,
                "contextsize_endpoint": context_size}
        if missing or invalid:
            return False, (f"账本响应头缺失 {len(missing)}/{len(_PF_LEDGER_HEADERS)} 项: "
                           f"{', '.join(missing[:6])}{'...' if len(missing) > 6 else ''}"
                           + (f"; 非法值: {', '.join(invalid)}" if invalid else "")), data
        within = hdr_ctx is None or hdr_ctx <= 0 or tokens_out <= hdr_ctx
        detail = (f"全部 {len(_PF_LEDGER_HEADERS)} 项账本响应头齐全且为合法整数; "
                  f"tokens_out={tokens_out}, context_size={hdr_ctx}, within_budget={within}")
        return within, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_image_single_question(args, model, probe, all_results, all_crash_events):
    """场景：纯图片单问——一张最小占位图 + 一句极短中文提问（OpenAI 多段 content-parts）。
    断言伴随图片的文字说明未被压缩链路当作可随意丢弃的普通历史消息处理（用唯一 canary
    标记该说明文本，断言其仍在最终提示词块里），且请求返回 200；不要求模型真的看懂图片
    内容。仅在目标模型具备视觉能力时运行——文本模型收到 image_url content 会被
    genie_interface.cpp 的 IEmbedding::set_content 直接 throw ReportError{"not support
    vision mode"}，对本场景无意义，如实跳过而不是误判失败。"""
    name = "PROMPT_FIDELITY: pure image single question keeps accompanying text"
    if "image" not in detect_modality(model):
        all_results.append(_pf_result(
            name, model, False, 0, 0,
            f"目标模型 {model} 不支持视觉模态（detect_modality 未命中 image），跳过纯图片单问用例",
            skipped=True))
        return
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个助手，可以看图回答问题。"},
            {"role": "user", "content": [
                {"type": "text", "text": f"{_PF_CANARY_IMG_TEXT} 这张图片怎么样？"},
                {"type": "image_url", "image_url": {"url": _pf_build_min_png_data_uri()}},
            ]},
        ],
        "stream": False,
        "max_tokens": 48,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        text_kept = _PF_CANARY_IMG_TEXT in prompt_text
        data = {"final_prompt_chars": len(prompt_text), "accompanying_text_kept": text_kept}
        detail = f"accompanying_text_kept={text_kept}, final_prompt={len(prompt_text)} chars"
        return text_kept, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_relevance_tools():
    """一个英文 `weather` 工具 + 几个明确无关的工具。工具名与描述均为纯英文，
    用于验证中文提问能否仍看得到它（相关性过滤的跳语言盲区）。"""
    def _t(name, desc):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object",
                            "properties": {"query": {"type": "string", "description": "Query string."}},
                            "required": ["query"]}}}
    return [
        _t("weather", "Query the current weather and forecast for a given city."),
        _t("stock_quote", "Look up the latest trading price of a listed company."),
        _t("translate_text", "Translate a piece of text between two languages."),
        _t("currency_convert", "Convert an amount from one currency into another."),
    ]


def _pf_case_cross_language_relevance(args, model, probe, all_results, all_crash_events):
    """场景：中文提问「上海的天气怎么样」+ 纯英文 `weather` 工具。

    当前实现下中文被逐字切词（上/海/的/天/气/...），与英文工具名及英文描述一个都
    命中不了 → 全部候选得 0 分 → FilterToolsByRelevance 返回空集，工具定义从提示词整段
    消失，模型压根不知道自己能查天气（用户实测现象：显式点名 weather 就能工作，只问
    天气就不行）。断言最终提示词里仍出现 `weather`。
    """
    name = "PROMPT_FIDELITY: chinese query still sees english-named tool"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个助手，需要时可以调用工具。"},
            {"role": "user", "content": "上海的天气怎么样？"},
        ],
        "tools": _pf_relevance_tools(),
        "stream": False,
        "max_tokens": 64,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        weather_in = "weather" in prompt_text.lower()
        any_tool_in = any(t["function"]["name"] in prompt_text
                          for t in _pf_relevance_tools())
        data = {"weather_visible": weather_in, "any_tool_visible": any_tool_in,
                "final_prompt_chars": len(prompt_text)}
        detail = (f"weather_visible={weather_in}, any_tool_visible={any_tool_in}, "
                  f"final_prompt={len(prompt_text)} chars")
        return weather_in, detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_relevance_still_filters(args, model, probe, all_results, all_crash_events):
    """反向用例：候选里存在真实命中项时，不相关项必须仍被筛掉。

    防止「全零分兜底」被实现成「永不过滤」——那样虽然上一条用例会变绿，但相关性
    过滤本身就彻底失效了。英文提问 weather 时 `weather` 必须在、且至少一个明确无关
    的工具不在。"""
    name = "PROMPT_FIDELITY: relevance filter still drops unrelated tools"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an assistant. Use tools when needed."},
            {"role": "user", "content": "What is the weather in Shanghai right now?"},
        ],
        "tools": _pf_relevance_tools(),
        "stream": False,
        "max_tokens": 64,
    }

    def evaluate(resp, prompt_text, _probe):
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", {}
        if not prompt_text:
            return False, "未能抓取到最终提示词块，取证通道失效", {}
        lower = prompt_text.lower()
        weather_in = "weather" in lower
        unrelated = [n for n in ("stock_quote", "currency_convert", "translate_text")
                     if n in lower]
        data = {"weather_visible": weather_in, "unrelated_still_visible": unrelated}
        detail = (f"weather_visible={weather_in}, unrelated_still_visible={unrelated} "
                  f"(期望至少筛掉一个不相关工具，证明兜底未退化为「永不过滤」)")
        return (weather_in and len(unrelated) < 3), detail, data

    _pf_run_request(args, model, probe, name, body, all_results, all_crash_events, evaluate)


def _pf_case_stream_ledger(args, model, all_results, all_crash_events):
    """场景6：流式路径的账本回报。流式响应头在 Build() 之前就已发出，物理上无法承载账本，
    因此约定用既有 status 帧（status=prompt_optimized）承载同一套字段。"""
    name = "PROMPT_FIDELITY: stream reports the same ledger via status frame"
    body = {
        "model": model,
        "messages": _pf_tool_roundtrip_messages(
            _pf_build_long_tool_output(), "What exit code did the build finish with?"),
        "stream": True,
        "max_tokens": 48,
    }
    start = time.time()
    _trace_request(model, name, 1, "prompt_fidelity stream")
    ledger_frame = None
    try:
        with requests.post(f"http://{args.host}:{args.port}/v1/chat/completions",
                           json=body, stream=True, timeout=900) as r:
            status_code = r.status_code
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                # status/status_message 及 PromptLedger 字段均在 choices[0] 内（与
                # ResponseTools::statusDataJson 的既有 "preparing"/"summarizing" 等状态帧
                # 同一层级约定），不在顶层 —— 顶层只有 id/object/created/model/choices。
                choices = obj.get("choices") if isinstance(obj, dict) else None
                choice0 = choices[0] if isinstance(choices, list) and choices else None
                if isinstance(choice0, dict) and choice0.get("status") == "prompt_optimized":
                    ledger_frame = choice0
                    break
    except Exception as e:
        latency = (time.time() - start) * 1000
        all_results.append(_pf_result(name, model, False, 0, latency,
                                      f"流式请求异常: {str(e)[:300]}"))
        all_crash_events.append(CrashEvent(
            timestamp=datetime.now().isoformat(), model_name=model, round_num=1,
            endpoint=name, detail=f"prompt_fidelity 流式请求异常: {str(e)[:200]}",
            request_history=_trace_snapshot()))
        return
    latency = (time.time() - start) * 1000
    got = ledger_frame is not None
    detail = ("收到 status=prompt_optimized 帧: "
              f"{json.dumps(ledger_frame, ensure_ascii=False)[:300]}" if got
              else "流式响应中未收到 status=prompt_optimized 账本帧（调用方在流式路径上完全无感）")
    all_results.append(_pf_result(name, model, got, status_code, latency, detail,
                                  {"ledger_frame": ledger_frame or {}}))
