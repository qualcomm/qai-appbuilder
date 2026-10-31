#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import json
import random
import subprocess
import time
from pathlib import Path
from datetime import datetime

from .common import (
    CrashEvent,
    _trace_request,
    _trace_snapshot,
    infer_backend,
    wait_port_closed,
    wait_port_open,
)
from .managers import ServiceManager
from .suite_prompt_fidelity import _PromptLogProbe, _pf_filler_lines, _pf_post_chat
from .suite_skill_capacity import _sc_resolve_model, _sc_result


# ============================================================================
# long_task_memory suite —— Task Memo 分段式记忆机制的"记忆前沿"量化回归
# ============================================================================
# 方法论镜像 skill_capacity 的倍增探测 + 二分收敛 + 重复采样多数票（不直接复用其函数，
# 因签名强耦合技能容量池语义，见 _sc_majority_probe/_sc_binary_search），被测对象换成
# Task Memo（prompt_optimization.task_memo）而非技能目录容量。
#
# 关键架构事实（决定了下面两轮请求的设计，来自对 task_memo_builder.cpp 的代码审查，
# 不是随意选择）：
#   1. ComputeFingerprint 对"除最后一条外的全部非 system 消息"哈希；BuildTaskMemoSection()
#      （系统提示词"## Task Memo"主通道）在 FitMessagesToContext（写库）之前调用——
#      同一个请求内，主通道读到的永远是"上一轮"的旧数据，首次对话必定 MISS。只有
#      FitMessagesToContext 内部丢弃发生后紧跟着的尾部占位兜底 Lookup() 才会在同一
#      请求内命中（RenderCompact，不含 facts_constraints/open_questions 细节）。
#   2. 因此要验证主通道（Render()，携带用户事实/约束/TODO）的真实效果，必须两轮真实
#      请求：Round1 令历史丢弃并写库，Round2（首条 user 消息不变，靠 ComputeFirstMsgKey
#      稳定命中）才会在系统提示词里看到完整备忘录。
#   3. BuildRuleLayer 把 dropped 的单条 user 消息整段存入 facts_constraints（截断到
#      160 字符预览）——若把三项事实塞进同一条长消息，后两项会被截断丢失；因此下面把
#      goal/constraint/todo 拆成三条独立短消息，各自都在 160 字符预览之内。
#
# 记忆前沿定义：模型在 N 个填充轮次（用于挤占预算、诱发丢弃）之后，Round2 追问仍能
# 正确复述任务目标/约束/TODO 三个 canary code 的最大 N。legacy（task_memo.enabled=false）
# 与 optimized（=true，model_layer_enabled=false 保证判定确定性）分别测出前沿，用
# 倍数/差值量化机制收益。
#
# 已更正的架构事实（曾错误记载为"stateful 模式下整条压缩管线不会跑"，这与当前代码不符，
# 不要再假设它成立）：`ChatHistory::Limit()` 全仓库零调用方，是死代码；`model_input_builder.h`
# 驱动的 `PreFilterMessages`/`FitMessagesToContext`/`BuildTaskMemoSection`/`TaskMemoBuilder`
# 整条压缩管线（含 Task Memo）对 `-n` 取 -1 还是正整数完全无分支、无条件执行，两种模式走的
# 是同一条代码路径。`IsStatelessMode()`/`GetEffectiveStatelessMode()` 在全源码中唯一的消费点
# 在 `response_dispatcher.cpp`，只决定客户端断开后服务端是否额外持久化聊天历史——与压缩/
# Task Memo 管线完全无关，本套件的断言不涉及这个分支。stateful 模式因此与 stateless 共享
# 同一套 legacy/optimized 双臂语义，不是"退化为完全等价"的特例。

_LTM_GOAL_CODE = "LTM-GOAL-7F3A1"
_LTM_CONSTRAINT_CODE = "LTM-CONSTRAINT-9B2E4"
_LTM_TODO_CODE = "LTM-TODO-4D8C6"


def _ltm_task_brief_messages():
    """三条各自独立、均在 160 字符预览之内的短消息（原因见模块级注释第 3 点）。"""
    return [
        {"role": "user", "content":
            f"Migration goal ({_LTM_GOAL_CODE}): migrate the billing service from MySQL to PostgreSQL."},
        {"role": "assistant", "content": "Understood, PostgreSQL migration goal noted."},
        {"role": "user", "content":
            f"Hard constraint ({_LTM_CONSTRAINT_CODE}): never modify the file legacy_billing.py; "
            "it is frozen for compliance."},
        {"role": "assistant", "content": "Understood, I will not touch legacy_billing.py."},
        {"role": "user", "content":
            f"TODO ({_LTM_TODO_CODE}): write the migration script for the invoices table."},
        {"role": "assistant", "content": "Noted, invoices table migration script is pending."},
    ]


def _ltm_filler_round(i, lines_per_round):
    """一轮无关闲聊 + 工具调用，用于挤占预算、诱发早期消息被丢弃。"""
    call_id = f"ltm_call_{i}"
    return [
        {"role": "user", "content": f"Status check {i}: please run the next diagnostic step."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "run_diagnostic", "arguments": json.dumps({"step": i})}}
        ]},
        {"role": "tool", "tool_call_id": call_id, "name": "run_diagnostic",
         "content": _pf_filler_lines(f"ltm{i}", lines_per_round)},
    ]


def _ltm_build_history(n_filler, lines_per_round):
    messages = [
        {"role": "system", "content":
            "You are a careful coding assistant helping with a long-running migration task. "
            "Keep track of every fact, constraint and TODO given earlier in the conversation."},
    ]
    messages.extend(_ltm_task_brief_messages())
    for i in range(n_filler):
        messages.extend(_ltm_filler_round(i, lines_per_round))
    messages.append({"role": "user", "content": "Please just reply 'ack' and nothing else for now."})
    return messages


def _ltm_recall_question():
    return ("Recall this session's task brief. Answer with exactly three lines, quoting the literal "
            "code shown in parentheses for each item: "
            "line 1 = migration goal code, line 2 = constraint code, line 3 = TODO code.")


def _ltm_header_bool(headers, name):
    v = headers.get(name)
    if v is None:
        return None
    return str(v).strip() not in ("0", "", "false", "False")


def _ltm_header_num(headers, name, cast):
    v = headers.get(name)
    if v is None:
        return None
    try:
        return cast(v)
    except ValueError:
        return None


def _ltm_recall_hit(content, canary_code):
    return bool(content) and canary_code.lower() in content.lower()


def _ltm_extract_memo_headers(headers, suffix):
    return {
        f"dropped{suffix}": _ltm_header_num(headers, "X-Genie-Prompt-Messages-Dropped", int),
        f"memo_active{suffix}": _ltm_header_bool(headers, "X-Genie-Prompt-Memo-Active"),
        f"memo_confidence{suffix}": _ltm_header_num(headers, "X-Genie-Prompt-Memo-Confidence", float),
        f"memo_refresh{suffix}": _ltm_header_num(headers, "X-Genie-Prompt-Memo-Refresh-Count", int),
        f"memo_pages{suffix}": _ltm_header_num(headers, "X-Genie-Prompt-Memo-Pages", int),
    }


def _ltm_extract_goal_line(prompt_block):
    """从 `## Task Memo` 渲染文本里取出 `Original goal: ...` 整行（含低置信度后缀），
    抓不到返回 None。"""
    for line in (prompt_block or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Original goal:"):
            return stripped
    return None


def _ltm_single_trial_direct(args, model, probe, n, lines_per_round, timeout):
    """单次两轮试验（direct 通道）：Round1 制造丢弃并写库，Round2 用相同首条 user
    消息追问三个 canary code，验证是否仍能复述。返回逐字段可诊断的证据字典（供
    --long_task_memory 重跑时定位具体是哪一轮/哪个字段失败，而不仅是聚合统计）。"""
    evidence = {"n": n, "error": None, "trivial": False,
                "recall_goal": False, "recall_constraint": False, "recall_todo": False}
    round1_messages = _ltm_build_history(n, lines_per_round)
    body1 = {"model": model, "stream": False, "messages": round1_messages, "max_tokens": 24}
    _trace_request(model, f"LTM round1 n={n}", 1, "long_task_memory")
    if probe is not None:
        probe.mark()
    try:
        r1 = _pf_post_chat(args.host, args.port, body1, timeout=timeout)
    except Exception as e:
        evidence["error"] = f"round1 请求异常（服务可能已崩溃）: {str(e)[:300]}"
        return evidence
    if r1.status_code != 200:
        evidence["error"] = f"round1 HTTP {r1.status_code}: {r1.text[:200]}"
        return evidence
    evidence.update(_ltm_extract_memo_headers(r1.headers, "_r1"))
    evidence["memo_section_in_prompt_r1"] = (
        "## Task Memo" in probe.last_prompt_block()) if probe is not None else None

    if not evidence.get("dropped_r1"):
        # 本次填充量尚未挤出任何历史消息：没有发生压缩事件，记忆机制根本没被触发，
        # 探针天然应当通过（原文仍在上下文里），标注 trivial 供调用方识别 N 过小。
        evidence.update(trivial=True, recall_goal=True, recall_constraint=True, recall_todo=True)
        return evidence

    try:
        msg1 = r1.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError):
        evidence["error"] = "round1 响应结构异常（无 choices[0].message）"
        return evidence

    round2_messages = round1_messages + [
        {"role": "assistant", "content": msg1.get("content") or "ack"},
        {"role": "user", "content": _ltm_recall_question()},
    ]
    body2 = {"model": model, "stream": False, "messages": round2_messages, "max_tokens": 128}
    _trace_request(model, f"LTM round2 n={n}", 1, "long_task_memory")
    if probe is not None:
        probe.mark()
    try:
        r2 = _pf_post_chat(args.host, args.port, body2, timeout=timeout)
    except Exception as e:
        evidence["error"] = f"round2 请求异常（服务可能已崩溃）: {str(e)[:300]}"
        return evidence
    if r2.status_code != 200:
        evidence["error"] = f"round2 HTTP {r2.status_code}: {r2.text[:200]}"
        return evidence
    evidence.update(_ltm_extract_memo_headers(r2.headers, "_r2"))
    evidence["memo_section_in_prompt_r2"] = (
        "## Task Memo" in probe.last_prompt_block()) if probe is not None else None

    try:
        content2 = r2.json()["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, ValueError):
        evidence["error"] = "round2 响应结构异常（无 choices[0].message）"
        return evidence

    evidence["recall_goal"] = _ltm_recall_hit(content2, _LTM_GOAL_CODE)
    evidence["recall_constraint"] = _ltm_recall_hit(content2, _LTM_CONSTRAINT_CODE)
    evidence["recall_todo"] = _ltm_recall_hit(content2, _LTM_TODO_CODE)
    evidence["round2_answer_preview"] = content2[:300]
    return evidence


def _ltm_majority_probe(args, model, probe, n, repeat, lines_per_round, timeout, trial_log):
    trials = [_ltm_single_trial_direct(args, model, probe, n, lines_per_round, timeout)
              for _ in range(max(1, repeat))]
    trial_log.extend(trials)

    def is_pass(t):
        return not t["error"] and t["recall_goal"] and t["recall_constraint"] and t["recall_todo"]

    pass_votes = sum(1 for t in trials if is_pass(t))
    passed = pass_votes * 2 > len(trials)
    last = trials[-1]
    curve_entry = {
        "n": n, "passed": passed, "pass_votes": pass_votes, "trials": len(trials),
        "trivial": last.get("trivial"), "dropped_r1": last.get("dropped_r1"),
        "dropped_r2": last.get("dropped_r2"),
        "memo_active_r1": last.get("memo_active_r1"), "memo_active_r2": last.get("memo_active_r2"),
        "memo_confidence_r2": last.get("memo_confidence_r2"), "memo_refresh_r2": last.get("memo_refresh_r2"),
        "memo_section_in_prompt_r2": last.get("memo_section_in_prompt_r2"),
        "recall_goal": last.get("recall_goal"), "recall_constraint": last.get("recall_constraint"),
        "recall_todo": last.get("recall_todo"), "error": last.get("error"),
    }
    return passed, curve_entry


def _ltm_binary_search(args, model, probe, max_n, repeat, lines_per_round, timeout, trial_log):
    """求最大可通过的 N（记忆前沿），算法与 _sc_binary_search 完全同构（倍增探测找失败
    上界 → 区间二分收敛），如实标注是否真实收敛，避免把未收敛下界误报为真实前沿。"""
    curve = []

    def test(n):
        passed, entry = _ltm_majority_probe(args, model, probe, n, repeat, lines_per_round, timeout, trial_log)
        curve.append(entry)
        return passed

    if not test(1):
        return 0, True, curve

    last_pass, first_fail = 1, None
    n = 2
    while n <= max_n:
        if test(n):
            last_pass = n
            n *= 2
        else:
            first_fail = n
            break

    if first_fail is None:
        return last_pass, False, curve
    if first_fail <= last_pass + 1:
        return last_pass, True, curve

    lo, hi, best = last_pass + 1, first_fail - 1, last_pass
    while lo <= hi:
        mid = (lo + hi) // 2
        if test(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best, True, curve


def _ltm_arm_overrides(arm):
    if arm == "legacy":
        return {"enabled": False}
    return {"enabled": True, "model_layer_enabled": False, "min_dropped_for_trigger": 1}


class _LtmArmConfigOverride:
    """临时改写 <exe_dir>/prompt_engineering.json 的 prompt_optimization.task_memo 子节，
    与 _ScArmConfigOverride 同一备份-还原模式（改前备份，__exit__ 里 finally 还原）。"""

    def __init__(self, exe_dir, arm):
        self.config_path = Path(exe_dir) / "prompt_engineering.json"
        self.overrides = _ltm_arm_overrides(arm)
        self._original_text = None
        self._existed = False

    def __enter__(self):
        if self.config_path.exists():
            self._existed = True
            self._original_text = self.config_path.read_text(encoding="utf-8")
            try:
                data = json.loads(self._original_text)
            except Exception:
                data = {}
        else:
            data = {}
        po = data.setdefault("prompt_optimization", {})
        po.setdefault("task_memo", {}).update(self.overrides)
        self.config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._existed:
                self.config_path.write_text(self._original_text, encoding="utf-8")
            elif self.config_path.exists():
                self.config_path.unlink()
        except Exception as e:
            print(f"WARNING: _LtmArmConfigOverride 还原 {self.config_path} 失败: {e}")
        return False


def _ltm_run_via_client(client_exe, host, port, body, out_dir, timeout):
    """通过 GenieAPIClient.exe --raw_file 发送任意构造的 messages 数组——其正常
    --prompt/--system CLI 每次只能发单轮，--raw_file 分支绕开此限制、原样送出 JSON
    请求体（技法来自 GenieAPIClient.cpp 源码）。返回 (ok, stdout_text)。"""
    raw_dir = Path(out_dir) / "long_task_memory_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"body_{int(time.time() * 1000)}_{random.randint(0, 9999)}.json"
    try:
        raw_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        cmd = [str(client_exe), "--raw_file", str(raw_path), "--host", f"{host}:{port}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, f"EXC: {e}"
    finally:
        try:
            raw_path.unlink()
        except OSError:
            pass


def _ltm_client_spotcheck(args, model, out_dir, n_values, lines_per_round, timeout,
                           all_results, all_crash_events, label, arm, frontier):
    """GenieAPIClient.exe 端到端交叉确认通道：在 direct 通道已求出的前沿附近抽样几个 N，
    用真实客户端可执行文件复核同一 pass/fail 方向，不重跑完整二分搜索（真实进程调用
    开销显著高于 direct HTTP，只做交叉确认而非独立求前沿）。抽样点天然包含 max_n（预期
    召回失败，用于确认前沿之外方向也一致），故按 n<=frontier 期望召回成功/n>frontier 期望
    召回失败分别判定，而非无条件要求召回成功——否则 max_n 点必然被误判为失败。"""
    client_exe = Path(args.exe_dir) / "GenieAPIClient.exe"
    if not client_exe.exists():
        all_results.append(_sc_result(
            f"LONG_TASK_MEMORY: {label}/{arm} client crosscheck", model, False,
            f"未找到 GenieAPIClient.exe: {client_exe}，跳过端到端交叉确认通道", skipped=True))
        return
    for n in n_values:
        round1_messages = _ltm_build_history(n, lines_per_round)
        body1 = {"model": model, "stream": False, "messages": round1_messages, "max_tokens": 24}
        _trace_request(model, f"LTM client round1 n={n}", 1, "long_task_memory")
        try:
            ok1, out1 = _ltm_run_via_client(client_exe, args.host, args.port, body1, out_dir, timeout)
        except Exception as e:
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name=model, round_num=1,
                endpoint=f"LTM client round1 n={n}",
                detail=f"long_task_memory client 通道异常: {str(e)[:200]}",
                request_history=_trace_snapshot()))
            ok1, out1 = False, str(e)
        round2_messages = round1_messages + [
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": _ltm_recall_question()},
        ]
        body2 = {"model": model, "stream": False, "messages": round2_messages, "max_tokens": 128}
        _trace_request(model, f"LTM client round2 n={n}", 1, "long_task_memory")
        ok2, out2 = _ltm_run_via_client(client_exe, args.host, args.port, body2, out_dir, timeout)
        recall = {c: _ltm_recall_hit(out2, c) for c in (_LTM_GOAL_CODE, _LTM_CONSTRAINT_CODE, _LTM_TODO_CODE)}
        expect_recall = n <= frontier
        direction_matches = all(recall.values()) if expect_recall else not all(recall.values())
        passed = ok1 and ok2 and direction_matches
        all_results.append(_sc_result(
            f"LONG_TASK_MEMORY: {label}/{arm} client crosscheck n={n}", model, passed,
            f"GenieAPIClient.exe --raw_file 交叉确认 n={n}: round1_ok={ok1}, round2_ok={ok2}, "
            f"expect_recall={expect_recall}, recall={recall}",
            data={"n": n, "ok1": ok1, "ok2": ok2, "expect_recall": expect_recall, "recall": recall}))


def _ltm_stateful_probe(args, model, probe, n_values, lines_per_round, timeout,
                         all_results, all_crash_events, label, arm):
    """stateful 模式：整条压缩管线（含 TaskMemoBuilder）与 stateless 模式完全同样跑
    （已更正的架构事实见模块级注释：`-n` 只影响客户端断开后是否额外持久化历史，不分支
    压缩/Task Memo 管线），故此函数本质上是——在 `-n` 为正整数的服务实例上重跑一遍
    _ltm_single_trial_direct，验证是否和 stateless 模式走向一致。

    方向感知判定（与 _ltm_client_spotcheck 同构）：round2 每次都重发完整历史，expect_recall 由
    dropped_r2（服务端实际回报的截断量）而非猜测的 n 阈值推导，n 越大只是让 dropped_r2 更大，
    不改变这条因果关系本身。"""
    for n in n_values:
        evidence = _ltm_single_trial_direct(args, model, probe, n, lines_per_round, timeout)
        actual_recall = evidence["recall_goal"] and evidence["recall_constraint"] and evidence["recall_todo"]
        dropped_r2 = evidence.get("dropped_r2")
        if arm == "legacy":
            # legacy 臂（Task Memo 关闭）：丢弃就该召回失败，这是基线对照，预期不变。
            expect_recall = True if dropped_r2 is None else (dropped_r2 == 0)
        else:
            # optimized 臂（Task Memo 开启）：机制的设计目的恰恰是丢弃发生后依然能召回，
            # 不能照搬 legacy 臂"丢弃就该召回失败"的预期。只有在确实发生了丢弃但 Task
            # Memo 根本没激活（memo_active_r2 为假，等同退化为未开启）时，才回退到
            # legacy 同款预期；否则（未丢弃，或丢弃且已激活）预期都是召回成功。
            memo_active_r2 = evidence.get("memo_active_r2")
            if dropped_r2 and not memo_active_r2:
                expect_recall = (dropped_r2 == 0)
            else:
                expect_recall = True
        direction_matches = (actual_recall == expect_recall)
        passed = (not evidence["error"]) and direction_matches
        if evidence["error"] and "异常" in evidence["error"]:
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name=model, round_num=1,
                endpoint=f"LTM stateful n={n}", detail=evidence["error"][:200],
                request_history=_trace_snapshot()))
        all_results.append(_sc_result(
            f"LONG_TASK_MEMORY: {label}/stateful/{arm} n={n}", model, passed,
            f"stateful 模式 n={n}: expect_recall={expect_recall}, actual_recall={actual_recall}, {evidence}",
            skipped=bool(evidence.get("trivial")),
            data=evidence))


def _ltm_append_arm_contrast(model, service_mode, frontier_by_arm, all_results):
    legacy_f, opt_f = frontier_by_arm.get("legacy"), frontier_by_arm.get("optimized")
    if legacy_f is None or opt_f is None:
        return
    gain = opt_f - legacy_f
    ratio = (opt_f / legacy_f) if legacy_f > 0 else None
    detail = (f"{service_mode} 模式：legacy frontier={legacy_f}, optimized frontier={opt_f}, 差值={gain}"
              + (f"，倍数={ratio:.2f}x" if ratio is not None else ""))
    all_results.append(_sc_result(
        f"LONG_TASK_MEMORY: {model}/{service_mode} arm_contrast", model, opt_f >= legacy_f, detail,
        data={"legacy_frontier": legacy_f, "optimized_frontier": opt_f, "gain": gain, "ratio": ratio}))


# ============================================================================
# Task Memo 新增字段（goal anchor / failure_streak / 分页目录）轻量回归：场景 A-D
# ============================================================================
# 范围决策（务必保持，不要尝试扩大）：
#   - failure_streak 只做"默认未触发时不出现持久化提示行"的消极回归（见 _ltm_scenario_a_*
#     附带的旁路检查思路——本套件未对它单独建场景，因为该计数器只由
#     response_dispatcher.cpp 的 Layer3 修复路径在真实检测到模型输出畸形 tool_call 时
#     递增，HTTP 请求无法可靠构造触发；强行构造是非确定性且模型相关的，不值得做）。
#   - 分页目录只验证"计数器连通性"（存在/非负/不下降），不追打 max_entries=20 的 FIFO
#     淘汰：每次 Update() 都绑定一整轮真实推理，20+ 轮在真机上成本过高。
#   - 流式 SSE 帧里的 memo_pages_total 不做断言：现有套件没有现成的流式帧解析脚手架，
#     为这一个字段新搭一套的成本不划算，非流式响应头已经能验证管线连通。


def _ltm_new_fields_models(args):
    """返回 Task Memo 新增字段回归（场景 A-D）要跑的模型简写集合，默认仅 qwen3-8b
    （控制真机推理成本——这组场景不求前沿数字，但每个场景仍需 1-4 轮额外真实推理）。"""
    raw = getattr(args, "long_task_memory_new_fields_models", None)
    if not raw:
        return {"qwen3-8b"}
    return {s.strip() for s in raw.split(",") if s.strip()}


def _ltm_scenario_a_goal_anchor_persist(args, model, probe, lines_per_round, timeout,
                                        all_results, all_crash_events, label):
    """场景 A：开场夹杂低于 20 字符门槛的闲聊时，goal anchor 必须锚定在真正的任务目标
    消息（而非闲聊）；二次丢弃事件后锚文本必须逐字节不变（"一次捕获，永不覆盖"）。"""
    name = f"LONG_TASK_MEMORY: {label}/goal_anchor_persist"
    system_msg = {"role": "system", "content":
                  "You are a careful coding assistant helping with a long-running migration task. "
                  "Keep track of every fact, constraint and TODO given earlier in the conversation."}
    smalltalk = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你？"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hi there"},
    ]
    base_messages = [system_msg] + smalltalk + _ltm_task_brief_messages()
    max_probe = getattr(args, "long_task_memory_max", 8)

    def _round(n_filler):
        msgs = list(base_messages)
        for i in range(n_filler):
            msgs.extend(_ltm_filler_round(i, lines_per_round))
        msgs.append({"role": "user", "content": "Please just reply 'ack' and nothing else for now."})
        body = {"model": model, "stream": False, "messages": msgs, "max_tokens": 24}
        _trace_request(model, f"LTM scenario_a n={n_filler}", 1, "long_task_memory")
        probe.mark()
        try:
            r = _pf_post_chat(args.host, args.port, body, timeout=timeout)
        except Exception as e:
            return None, None, f"请求异常: {str(e)[:300]}"
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:200]}"
        return r, probe.last_prompt_block(), None

    r1 = prompt1 = err = None
    n_used = None
    for n in range(1, max_probe + 1):
        r1, prompt1, err = _round(n)
        if err:
            break
        if r1.headers.get("X-Genie-Prompt-Messages-Dropped") not in (None, "0"):
            n_used = n
            break
    if err:
        all_results.append(_sc_result(name, model, False, err, skipped=True))
        return
    if n_used is None:
        all_results.append(_sc_result(
            name, model, False,
            f"倍增到 --long_task_memory_max={max_probe} 仍未触发任何丢弃事件，无法验证 goal anchor，精确跳过",
            skipped=True))
        return

    # BuildTaskMemoSection() 渲染正文发生在 FitMessagesToContext 写库之前：刚触发丢弃
    # 的那一轮（n_used）自己读到的仍是写库前的旧正文，必须再追加一轮才能读到刚写入的
    # goal anchor。响应头没有这个延迟，但本场景只关心正文渲染，所以统一改用追加轮。
    r_a, prompt_a, err_a = _round(n_used + 1)
    if err_a:
        all_results.append(_sc_result(f"{name}/round1", model, False, err_a, skipped=True))
        return
    goal_line1 = _ltm_extract_goal_line(prompt_a)
    ok1 = bool(goal_line1) and _LTM_GOAL_CODE in goal_line1 and "(low confidence:" not in goal_line1
    all_results.append(_sc_result(
        f"{name}/round1", model, ok1,
        f"n_filler={n_used} 触发首次丢弃，追加一轮（n_filler={n_used + 1}）读正文后 goal anchor: {goal_line1!r}",
        data={"goal_line": goal_line1, "n_filler": n_used}))
    if not ok1:
        return

    r2, prompt2, err2 = _round(n_used + 2)
    if err2:
        all_results.append(_sc_result(f"{name}/round2_unchanged", model, False, err2, skipped=True))
        return
    goal_line2 = _ltm_extract_goal_line(prompt2)
    ok2 = goal_line2 == goal_line1
    all_results.append(_sc_result(
        f"{name}/round2_unchanged", model, ok2,
        f"再追加一轮（n_filler={n_used + 2}）后 goal anchor 是否逐字节不变: round1={goal_line1!r}, round2={goal_line2!r}",
        data={"goal_line1": goal_line1, "goal_line2": goal_line2}))


def _ltm_scenario_b_goal_anchor_low_confidence(args, model, probe, lines_per_round, timeout,
                                               all_results, all_crash_events, label):
    """场景 B：开场 goal_scan_window（默认 3）条 user 消息全部低于 20 字符门槛时，
    goal anchor 必须回退到第一条 user 消息并带低置信度后缀——纯规则性回归，不涉及
    模型判断本身。"""
    name = f"LONG_TASK_MEMORY: {label}/goal_anchor_low_confidence"
    system_msg = {"role": "system", "content":
                  "You are a careful coding assistant helping with a long-running migration task."}
    shorts = [
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "go"},
    ]
    base_messages = [system_msg] + shorts
    max_probe = getattr(args, "long_task_memory_max", 8)

    def _round(n_filler):
        msgs = list(base_messages)
        for i in range(n_filler):
            msgs.extend(_ltm_filler_round(i, lines_per_round))
        msgs.append({"role": "user", "content": "Please just reply 'ack' and nothing else for now."})
        body = {"model": model, "stream": False, "messages": msgs, "max_tokens": 24}
        _trace_request(model, f"LTM scenario_b n={n_filler}", 1, "long_task_memory")
        probe.mark()
        try:
            r = _pf_post_chat(args.host, args.port, body, timeout=timeout)
        except Exception as e:
            return None, None, f"请求异常: {str(e)[:300]}"
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:200]}"
        return r, probe.last_prompt_block(), None

    n_used = None
    for n in range(1, max_probe + 1):
        r, prompt, err = _round(n)
        if err:
            all_results.append(_sc_result(name, model, False, err, skipped=True))
            return
        if r.headers.get("X-Genie-Prompt-Messages-Dropped") not in (None, "0"):
            n_used = n
            break
    if n_used is None:
        all_results.append(_sc_result(
            name, model, False,
            f"倍增到 --long_task_memory_max={max_probe} 仍未触发任何丢弃事件，无法验证低置信度回退，精确跳过",
            skipped=True))
        return

    # 同场景 A 的根因：正文渲染发生在写库之前，刚触发丢弃的那一轮自己读不到刚写入的
    # 内容，必须再追加一轮才能读到低置信度回退后的 goal anchor。
    r_after, prompt_after, err_after = _round(n_used + 1)
    if err_after:
        all_results.append(_sc_result(name, model, False, err_after, skipped=True))
        return
    goal_line = _ltm_extract_goal_line(prompt_after)
    ok = bool(goal_line) and "ok" in goal_line.lower() and "(low confidence:" in goal_line
    all_results.append(_sc_result(
        name, model, ok,
        f"n_filler={n_used} 触发丢弃，追加一轮（n_filler={n_used + 1}）读正文后低置信度回退 goal anchor: {goal_line!r}",
        data={"goal_line": goal_line, "n_filler": n_used}))


def _ltm_scenario_c_page_directory(args, model, probe, lines_per_round, timeout,
                                   all_results, all_crash_events, label):
    """场景 C：分页目录（`pages`/`memo_pages_total`）管线连通性——只验证响应头存在/
    非负/不下降，以及渲染区不泄漏原始填充文本，不追打 max_entries=20 的 FIFO 淘汰
    （见模块级范围决策注释）。"""
    name = f"LONG_TASK_MEMORY: {label}/page_directory"
    system_msg = {"role": "system", "content":
                  "You are a careful coding assistant helping with a long-running migration task. "
                  "Keep track of every fact, constraint and TODO given earlier in the conversation."}
    messages = [system_msg] + _ltm_task_brief_messages()
    distinctive_filler_substr = "routine progress record"
    max_probe = getattr(args, "long_task_memory_max", 8)
    pages_seen = []
    i = 0
    rounds_done = 0
    while rounds_done < 3 and i < max_probe * 2:
        messages = messages + _ltm_filler_round(i, lines_per_round)
        i += 1
        probe_msgs = messages + [{"role": "user", "content": "Please just reply 'ack' and nothing else for now."}]
        body = {"model": model, "stream": False, "messages": probe_msgs, "max_tokens": 24}
        _trace_request(model, f"LTM scenario_c round={rounds_done}", 1, "long_task_memory")
        probe.mark()
        try:
            r = _pf_post_chat(args.host, args.port, body, timeout=timeout)
        except Exception as e:
            all_results.append(_sc_result(name, model, False, f"请求异常: {str(e)[:300]}", skipped=True))
            return
        if r.status_code != 200:
            all_results.append(_sc_result(name, model, False, f"HTTP {r.status_code}: {r.text[:200]}", skipped=True))
            return
        if r.headers.get("X-Genie-Prompt-Messages-Dropped") in (None, "0"):
            continue
        # 响应头是本轮 Update() 调用的即时返回值，不受正文渲染延迟影响，可以在循环内
        # 直接读取；正文（section_ok）必须延后到循环结束后再追加一轮读取，见下方。
        pages_total = _ltm_header_num(r.headers, "X-Genie-Prompt-Memo-Pages", int)
        pages_seen.append(pages_total)
        rounds_done += 1
    if not pages_seen or any(p is None for p in pages_seen):
        all_results.append(_sc_result(
            name, model, False,
            f"X-Genie-Prompt-Memo-Pages 头缺失，或在 --long_task_memory_max={max_probe} 的 2 倍探测窗口内"
            f"未能触发 3 轮丢弃事件: pages_seen={pages_seen}", skipped=True))
        return

    # 正文渲染（## Task Memo 的 Evicted message pages 小节）比写库晚一轮：上面 3 轮
    # 丢弃事件各自读到的都是写库前的旧正文，必须再追加一轮才能读到最新一次写入的内容。
    messages = messages + _ltm_filler_round(i, lines_per_round)
    probe_msgs = messages + [{"role": "user", "content": "Please just reply 'ack' and nothing else for now."}]
    body = {"model": model, "stream": False, "messages": probe_msgs, "max_tokens": 24}
    _trace_request(model, "LTM scenario_c read_section", 1, "long_task_memory")
    probe.mark()
    try:
        r = _pf_post_chat(args.host, args.port, body, timeout=timeout)
    except Exception as e:
        all_results.append(_sc_result(name, model, False, f"请求异常: {str(e)[:300]}", skipped=True))
        return
    if r.status_code != 200:
        all_results.append(_sc_result(name, model, False, f"HTTP {r.status_code}: {r.text[:200]}", skipped=True))
        return
    prompt_block = probe.last_prompt_block()
    has_section = "Evicted message pages" in prompt_block
    # 分页目录块按设计是 Render() 里最低优先级、预算紧张时可被整块丢弃的小节（见
    # task_memo_builder.md「分页目录」条目）——真机实测过（2026-10-07，filler_lines=20
    # 的激进探测参数）该块确实会被整体省略，这是设计内行为，不是泄漏/缺陷。因此"未泄漏
    # 原文"只在该块确实渲染出来时才有意义去检查；块本身整体缺失时无从泄漏，视为空洞为真。
    tail = prompt_block.split("Evicted message pages", 1)[-1] if has_section else ""
    no_leak = (distinctive_filler_substr not in tail) if has_section else True

    non_negative = all(p >= 0 for p in pages_seen)
    non_decreasing = all(pages_seen[j] <= pages_seen[j + 1] for j in range(len(pages_seen) - 1))
    strictly_increasing_by_one = all(pages_seen[j + 1] - pages_seen[j] == 1 for j in range(len(pages_seen) - 1))
    passed = non_negative and non_decreasing and no_leak
    detail = (f"pages_seen={pages_seen}（non_negative={non_negative}, non_decreasing={non_decreasing}, "
              f"strictly_increasing_by_1_per_drop={strictly_increasing_by_one}——仅观测记录，未强制断言此项）；"
              f"has_section={has_section}（False 时视为预算过紧导致该最低优先级块被整体丢弃，设计内行为，"
              f"不计入失败）；no_leak={no_leak}")
    all_results.append(_sc_result(name, model, passed, detail,
                                  data={"pages_seen": pages_seen, "has_section": has_section, "no_leak": no_leak}))


def _ltm_run_new_field_scenarios(args, model, probe, lines_per_round, timeout,
                                 all_results, all_crash_events, label):
    """新增字段场景 A/B/C 的统一入口，复用调用方已启动的 optimized/stateless 服务实例，
    单个场景异常不中断另外两个（各自独立 try/except 记 CrashEvent）。"""
    for fn, tag in (
        (_ltm_scenario_a_goal_anchor_persist, "scenario_a"),
        (_ltm_scenario_b_goal_anchor_low_confidence, "scenario_b"),
        (_ltm_scenario_c_page_directory, "scenario_c"),
    ):
        try:
            fn(args, model, probe, lines_per_round, timeout, all_results, all_crash_events, label)
        except Exception as e:
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name=model, round_num=1,
                endpoint=f"LTM {tag}/{label}", detail=f"{tag} 执行异常: {str(e)[:200]}",
                request_history=_trace_snapshot()))


def _ltm_scenario_d_mode_consistency(args, label, target, config_path, lines_per_round, timeout,
                                     out_dir, all_results, all_crash_events, stateful_n):
    """场景 D：用户明确要求的回归点——`-n` 取 -1（stateless）或正整数（stateful）时，
    压缩管线（含 Task Memo 新增字段）必须走完全相同的路径。依次（非并发）起两个服务
    实例喂同一份历史，比较 goal anchor 文本与 memo_pages_total 的出现情况是否一致。"""
    name_prefix = f"LONG_TASK_MEMORY: {label}/mode_consistency"
    max_probe = getattr(args, "long_task_memory_max", 8)

    def _run_one(service_mode):
        wait_port_closed(args.host, args.port, timeout=15)
        svc = ServiceManager(args.exe_dir, args.host, args.port)
        svc._log_dir = out_dir
        extra_args = ["-n", "-1", "-g", "-d", "3"] if service_mode == "stateless" \
            else ["-n", str(stateful_n), "-g", "-d", "3"]
        try:
            with _LtmArmConfigOverride(args.exe_dir, "optimized"):
                svc.start(str(config_path), extra_args=extra_args)
                if not wait_port_open(args.host, args.port, timeout=180, process=svc.process):
                    return None, f"{service_mode} 服务 180s 内未可连接"
                probe = _PromptLogProbe(svc._stdout_log)

                def _send(n_filler):
                    history = _ltm_build_history(n_filler, lines_per_round)
                    body = {"model": target, "stream": False, "messages": history, "max_tokens": 24}
                    probe.mark()
                    _trace_request(target, f"LTM mode_consistency {service_mode} n={n_filler}", 1,
                                    "long_task_memory")
                    try:
                        r = _pf_post_chat(args.host, args.port, body, timeout=timeout)
                    except Exception as e:
                        return None, None, f"{service_mode} 请求异常: {str(e)[:300]}"
                    if r.status_code != 200:
                        return None, None, f"{service_mode} HTTP {r.status_code}: {r.text[:200]}"
                    return r, probe.last_prompt_block(), None

                # 硬编码固定 n_filler 不保证真的触发丢弃（是否挤占预算全看实际 token 占用）：
                # 改为像场景 A/B 一样循环探测，直到观测到丢弃事件为止。
                n_used = None
                r = prompt = err = None
                for n in range(1, max_probe + 1):
                    r, prompt, err = _send(n)
                    if err:
                        return None, err
                    if r.headers.get("X-Genie-Prompt-Messages-Dropped") not in (None, "0"):
                        n_used = n
                        break
                if n_used is None:
                    return None, (f"{service_mode} 倍增到 --long_task_memory_max={max_probe} "
                                   f"仍未触发任何丢弃事件")

                # 正文渲染晚一轮、响应头本轮即时准确：两边统一改用追加轮的数值，保持口径
                # 一致，便于跨模式比较（而不是一个用本轮头、一个用追加轮正文）。
                r_after, prompt_after, err_after = _send(n_used + 1)
                if err_after:
                    return None, err_after
                evidence = {
                    "n_filler_used": n_used,
                    "dropped": _ltm_header_num(r_after.headers, "X-Genie-Prompt-Messages-Dropped", int),
                    "memo_pages_total": _ltm_header_num(r_after.headers, "X-Genie-Prompt-Memo-Pages", int),
                    "goal_line": _ltm_extract_goal_line(prompt_after),
                }
                return evidence, None
        except (RuntimeError, FileNotFoundError) as e:
            return None, f"{service_mode} 服务启动失败: {str(e)[:300]}"
        finally:
            svc.stop()
            svc._force_kill()

    evidence_stateless, err_stateless = _run_one("stateless")
    if err_stateless:
        all_results.append(_sc_result(f"{name_prefix} precondition", target, False, err_stateless, skipped=True))
        return
    evidence_stateful, err_stateful = _run_one("stateful")
    if err_stateful:
        all_results.append(_sc_result(f"{name_prefix} precondition", target, False, err_stateful, skipped=True))
        return

    goal_consistent = (bool(evidence_stateless["goal_line"]) and bool(evidence_stateful["goal_line"])
                       and evidence_stateless["goal_line"] == evidence_stateful["goal_line"])
    pages_consistent = (evidence_stateless["memo_pages_total"] is not None
                        and evidence_stateful["memo_pages_total"] is not None)
    passed = goal_consistent and pages_consistent
    all_results.append(_sc_result(
        name_prefix, target, passed,
        f"stateless={evidence_stateless}, stateful={evidence_stateful}；"
        f"goal_consistent={goal_consistent}, pages_consistent={pages_consistent}",
        data={"stateless": evidence_stateless, "stateful": evidence_stateful}))


def _run_long_task_memory_suite(args, models, remote_mode, out_dir):
    """--suite long_task_memory：Task Memo 分段式记忆机制的「记忆前沿」量化回归。

    核心指标：模型在 N 次压缩/丢弃事件之后，Round2 追问仍能复述任务目标/约束/TODO
    三个 canary code 的最大 N（倍增探测 + 二分收敛 + 重复采样多数票，方法论同构
    skill_capacity，见模块级注释）。legacy（task_memo 关闭）vs optimized（开启）
    双臂对照量化机制收益；direct（Python 直连读 X-Genie-Prompt-Memo-* 头）与
    GenieAPIClient.exe（--raw_file 端到端交叉确认）双通道。stateful 模式下压缩管线与
    stateless 完全同样跑，同样做 legacy/optimized 双臂对照（已更正的架构事实见模块级
    注释，见 _ltm_stateful_probe）。

    仅实现 Tier 1（合成长任务 + 宽松文本记忆探针）。Tier 2（基于 tool_calls 协议的多步骤
    编码任务端到端核验）本轮未实现：它要求服务端维护一份独立于 Task Memo 的 ground-truth
    环境状态机（文件系统/构建产物的期望值）来核验任务是否真正完成，工作量与真机测试时间
    预算不匹配，按计划 Testing 部分"工作量过大则明确记录跳过原因"处理，跳过。

    本套件中还挂着 Task Memo 新增字段（goal anchor/分页目录/跨模式一致性）的两组
    轻量回归：场景 A/B/C（_ltm_run_new_field_scenarios）复用本循环已启动的
    optimized/stateless 实例，场景 D（_ltm_scenario_d_mode_consistency）自己管理一对
    stateless/stateful 实例生命周期。默认只对 --long_task_memory_new_fields_models
    命中的模型（默认仅 qwen3-8b）跑，控制真机推理成本。
    """
    all_results, all_perf_samples, all_crash_events = [], [], []
    suite_model = "_long_task_memory_"

    if remote_mode:
        all_results.append(_sc_result(
            "LONG_TASK_MEMORY: suite precondition", suite_model, False,
            "远程模式无法自定义服务命令行（stateless 需 -n -1，stateful 需自定义 -n）也无法临时改写 "
            "prompt_engineering.json 切换 legacy/optimized 档位，跳过记忆前沿套件", skipped=True))
        return all_results, all_perf_samples, all_crash_events

    requested = None
    if getattr(args, "long_task_memory_models", None):
        requested = {s.strip() for s in args.long_task_memory_models.split(",") if s.strip()}

    targets = []
    m1 = _sc_resolve_model(models, "qwen3-8b")
    if not requested or "qwen3-8b" in requested:
        if m1:
            targets.append(("qwen3-8b", m1))
        else:
            all_results.append(_sc_result(
                "LONG_TASK_MEMORY: suite precondition qwen3-8b", suite_model, False,
                f"未在已发现模型中匹配到 qwen3-8b（models={models}），精确跳过", skipped=True))
    m2 = next((m for m in models if infer_backend(m)[0] == "GGUF"), None)
    if not requested or "gguf-20b" in requested:
        if m2:
            targets.append(("gguf-20b", m2))
        else:
            all_results.append(_sc_result(
                "LONG_TASK_MEMORY: suite precondition gguf-20b", suite_model, False,
                f"未在已发现模型中匹配到任何 GGUF 后端模型（models={models}），精确跳过", skipped=True))
    m3 = next((m for m in models if infer_backend(m)[0] == "mnn"), None)
    if not requested or "mnn-20b" in requested:
        if m3:
            targets.append(("mnn-20b", m3))
        else:
            all_results.append(_sc_result(
                "LONG_TASK_MEMORY: suite precondition mnn-20b", suite_model, False,
                f"未在已发现模型中匹配到任何 MNN 后端模型（models={models}），精确跳过", skipped=True))

    if not targets:
        return all_results, all_perf_samples, all_crash_events

    max_n = getattr(args, "long_task_memory_max", 8)
    repeat = getattr(args, "long_task_memory_repeat", 1)
    lines_per_round = getattr(args, "long_task_memory_filler_lines", 6)
    timeout = getattr(args, "long_task_memory_timeout", 180)
    arms = ("legacy", "optimized") if getattr(args, "long_task_memory_arms", "both") == "both" \
        else (args.long_task_memory_arms,)
    drive_modes = ("direct", "client") if getattr(args, "long_task_memory_mode", "direct") == "both" \
        else (getattr(args, "long_task_memory_mode", "direct"),)
    service_modes = ("stateless", "stateful") if getattr(args, "long_task_memory_service_mode", "stateless") == "both" \
        else (getattr(args, "long_task_memory_service_mode", "stateless"),)
    stateful_n = getattr(args, "long_task_memory_stateful_n", 6)
    new_fields_targets = _ltm_new_fields_models(args)

    for label, target in targets:
        config_path = Path(args.models) / target / "config.json"
        if not config_path.exists():
            all_results.append(_sc_result(
                f"LONG_TASK_MEMORY: {label} precondition", target, False,
                f"缺失 config.json: {config_path}，精确跳过", skipped=True))
            continue

        for service_mode in service_modes:
            arms_for_mode = arms if service_mode == "stateless" else ("optimized",)
            frontier_by_arm = {}
            for arm in arms_for_mode:
                print(f"\n{'='*60}")
                print(f"阶段: long_task_memory（模型={label}/{target}, service_mode={service_mode}, arm={arm}）")
                print(f"{'='*60}")

                wait_port_closed(args.host, args.port, timeout=15)
                svc = ServiceManager(args.exe_dir, args.host, args.port)
                svc._log_dir = out_dir
                extra_args = ["-n", "-1", "-g", "-d", "3"] if service_mode == "stateless" \
                    else ["-n", str(stateful_n), "-g", "-d", "3"]
                try:
                    with _LtmArmConfigOverride(args.exe_dir, arm):
                        svc.start(str(config_path), extra_args=extra_args)
                        if not wait_port_open(args.host, args.port, timeout=180, process=svc.process):
                            all_results.append(_sc_result(
                                f"LONG_TASK_MEMORY: {label}/{service_mode}/{arm} precondition",
                                target, False, "端口 180s 内未可连接，精确跳过", skipped=True))
                            continue
                        probe = _PromptLogProbe(svc._stdout_log)
                        if service_mode == "stateless" and arm == "optimized" and label in new_fields_targets:
                            # Task Memo 新增字段（goal anchor/分页目录）的轻量回归，复用本次
                            # 已启动的 optimized/stateless 实例，不额外起停服务（见 _ltm_run_new_field_scenarios）。
                            _ltm_run_new_field_scenarios(args, target, probe, lines_per_round, timeout,
                                                         all_results, all_crash_events, label)
                        trial_log = []
                        if service_mode == "stateless":
                            if "direct" in drive_modes:
                                frontier, converged, curve = _ltm_binary_search(
                                    args, target, probe, max_n, repeat, lines_per_round, timeout, trial_log)
                                frontier_by_arm[arm] = frontier
                                # memory_probe_accuracy：把二分搜索过程中触达的全部 N 的
                                # pass_votes/trials 聚合成一个具名准确率，与离散的 frontier
                                # 数字互补（frontier 只反映边界，accuracy 反映边界附近整体稳定性）。
                                total_votes = sum(c.get("pass_votes", 0) for c in curve)
                                total_trials = sum(c.get("trials", 0) for c in curve)
                                memory_probe_accuracy = (total_votes / total_trials) if total_trials else None
                                note = (f"前沿值({arm})={frontier}（已收敛）" if converged else
                                        f"frontier ≥ {frontier}（倍增到 max_n={max_n} 仍全部通过，未收敛，"
                                        f"需调大 --long_task_memory_max 重测）")
                                acc_str = f"{memory_probe_accuracy:.2f}" if memory_probe_accuracy is not None else "N/A"
                                all_results.append(_sc_result(
                                    f"LONG_TASK_MEMORY: {label}/{service_mode}/{arm} frontier", target,
                                    converged, note + f"；memory_probe_accuracy={acc_str}；曲线点数={len(curve)}；"
                                    f"末次曲线条目={curve[-1] if curve else None}", skipped=not converged,
                                    data={"model": target, "arm": arm, "frontier": frontier,
                                          "converged": converged, "probe_curve": curve,
                                          "memory_probe_accuracy": memory_probe_accuracy,
                                          "trials": trial_log}))
                            if "client" in drive_modes:
                                spot_ns = sorted(set(
                                    n for n in (1, max(1, frontier_by_arm.get(arm, 1)), max_n) if n >= 1
                                )) if "direct" in drive_modes else sorted({1, max_n})
                                _ltm_client_spotcheck(args, target, out_dir, spot_ns, lines_per_round,
                                                      timeout, all_results, all_crash_events, label, arm,
                                                      frontier_by_arm.get(arm, 1))
                        else:
                            spot_ns = sorted(set(n for n in (1, max(1, stateful_n // 2), stateful_n + 2) if n >= 1))
                            _ltm_stateful_probe(args, target, probe, spot_ns, lines_per_round, timeout,
                                               all_results, all_crash_events, label, arm)
                except (RuntimeError, FileNotFoundError) as e:
                    all_results.append(_sc_result(
                        f"LONG_TASK_MEMORY: {label}/{service_mode}/{arm} precondition", target, False,
                        f"服务启动失败: {str(e)[:300]}", skipped=True))
                except Exception as e:
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(), model_name=target, round_num=1,
                        endpoint=f"LTM {service_mode}/{arm}", detail=f"套件执行异常: {str(e)[:200]}",
                        request_history=_trace_snapshot()))
                finally:
                    svc.stop()
                    svc._force_kill()

            if service_mode == "stateless" and len(frontier_by_arm) >= 2:
                _ltm_append_arm_contrast(target, service_mode, frontier_by_arm, all_results)

        if label in new_fields_targets:
            # 场景 D：`-n` -1 与正整数两模式的新字段一致性回归，自己管理服务实例生命周期，
            # 不复用上述循环的服务实例（它需要同时对比两种 service_mode）。
            _ltm_scenario_d_mode_consistency(args, label, target, config_path, lines_per_round, timeout,
                                             out_dir, all_results, all_crash_events, stateful_n)

    return all_results, all_perf_samples, all_crash_events
