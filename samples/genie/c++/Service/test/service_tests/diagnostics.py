#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import random
import re
import time
from pathlib import Path
from datetime import datetime

from .common import infer_backend


_MODEL_DEFECT_MARKER = "[MODEL_DEFECT] reason="


def _parse_model_defect_line(line):
    """解析一行服务端日志,提取 [MODEL_DEFECT] 标记携带的 reason/model/token_size。
    两种已知形式(genie.cpp 的 sdk_context_exceeded 带 model= 字段,genie_interface.cpp 的
    self_estimated_limit_exceeded 不带)字段顺序不完全固定,因此分别独立 search 而不是要求
    整行匹配一个固定顺序的正则。命中 reason= 与 token_size= 才视为有效(model= 可选)。"""
    if _MODEL_DEFECT_MARKER not in line:
        return None
    reason_m = re.search(r"reason=(\S+)", line)
    token_m = re.search(r"token_size=(\d+)", line)
    if not reason_m or not token_m:
        return None
    model_m = re.search(r"model=(\S+)", line)
    return {
        "reason": reason_m.group(1),
        "model": model_m.group(1) if model_m else None,
        "token_size": int(token_m.group(1)),
    }


def _scan_model_defect_text(text):
    """在一段文本(可能多行)里查找第一条 [MODEL_DEFECT] 标记并解析,未命中返回 None。
    供 _scan_model_defect_log()(按字节偏移读取服务日志)与 SampleApp 场景(直接扫描
    子进程自身捕获到的完整 stdout)共用同一份解析逻辑。"""
    if not text:
        return None
    for line in text.splitlines():
        if _MODEL_DEFECT_MARKER in line:
            parsed = _parse_model_defect_line(line)
            if parsed:
                return parsed
    return None


def _scan_model_defect_log(log_path, offset_before):
    """只扫描服务 stdout 日志文件里 offset_before 之后新增的字节,查找本次请求是否触发了
    [MODEL_DEFECT] 标记(见 genie.cpp/genie_interface.cpp 的 ERROR 级日志)。这样可以精确
    限定到"这一次请求的窗口",不会误判为同一进程生命周期内更早请求残留的标记,也不用担心
    与仍在写入日志的服务进程发生读取竞争(按字节偏移只读已落盘的旧内容)。
    log_path 为 None(如远程模式,或 RemoteServiceManager 没有本地日志文件)时直接返回 None。
    若日志文件在两次调用之间被重启截断(ServiceManager.start() 以"wb"模式重新打开同名文件),
    当前文件大小可能小于 offset_before,此时退化为从文件开头扫描,不抛异常。"""
    if log_path is None:
        return None
    try:
        p = Path(log_path)
        if not p.exists():
            return None
        size = p.stat().st_size
        start = offset_before if (offset_before is not None and offset_before <= size) else 0
        with open(p, "rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    return _scan_model_defect_text(text)


def _capability_issue_from_model_defect(defect):
    """把 _scan_model_defect_log()/_scan_model_defect_text() 返回的 dict 转换为
    (model_capability_issue, model_capability_reason) 这对通用诊断字段的取值,
    供各 chat 测试方法构造 TestResult 时直接复用。defect 为 None 时返回 (False, "")。"""
    if not defect:
        return False, ""
    model_part = f", model={defect['model']}" if defect.get("model") else ""
    reason = f"模型缺陷(服务端日志): reason={defect['reason']}, token_size={defect['token_size']}{model_part}"
    return True, reason


# 常见的 Windows 进程异常退出码(NTSTATUS,以有符号 32 位整数表示)含义对照表。
# 崩溃发生时把这个"实锤"证据直接附带到诊断信息里,让报告能立即回答"进程到底是怎么退出的",
# 不需要事后翻日志、更不需要猜测——这是本次修复要解决的核心可追溯性缺口。
_WINDOWS_EXIT_CODE_HINTS = {
    -1073741819: "STATUS_ACCESS_VIOLATION(0xC0000005): 空指针/越界内存访问,程序自身代码缺陷",
    -1073740791: "STATUS_STACK_BUFFER_OVERRUN(0xC0000409): 栈缓冲区溢出保护(/GS)触发的 FailFast",
    -1073741571: "STATUS_STACK_OVERFLOW(0xC00000FD): 栈溢出,常见于无限/过深递归",
    -1073740940: "STATUS_HEAP_CORRUPTION(0xC0000374): 堆损坏",
    -1073741515: "STATUS_DLL_NOT_FOUND(0xC0000135): 依赖的 DLL 缺失",
    -1073741701: "STATUS_DLL_INIT_FAILED(0xC0000142): DLL 初始化失败",
    3: "abort()/CRT 断言失败触发的终止",
}

# 与 GenieAPIService.cpp 中 GenieService::ServiceStop() 的关闭看门狗保持一致：底层推理调用
# (如 MNN 在内存压力下的 generate())未在阈值内响应 Stop() 信号时,看门狗会以该退出码强制
# 终止进程。这是已知的兜底路径,不是真实崩溃,必须与 _WINDOWS_EXIT_CODE_HINTS 中的 NTSTATUS
# 崩溃特征码区分开,避免被 graceful_shutdown 测试误判为"崩溃"。
_SHUTDOWN_WATCHDOG_EXIT_CODE = 124
_SHUTDOWN_WATCHDOG_LOG_MARKER = "[shutdown watchdog]"


def _describe_exit_code(exit_code):
    """把进程退出码翻译成人类可读的诊断信息,而不是留一个裸数字或空白让人去猜。
    exit_code 为 None 表示调用时进程已被清理/仍存活,拿不到真实退出码——必须明确说明这一点,
    而不是悄悄留空误导成"没有崩溃"。"""
    if exit_code is None:
        return "未知(进程已被清理或仍存活,无法回溯真实退出码)"
    hint = _WINDOWS_EXIT_CODE_HINTS.get(exit_code)
    try:
        hex_str = f"0x{exit_code & 0xFFFFFFFF:08X}"
    except TypeError:
        hex_str = "?"
    return f"{exit_code} ({hex_str}): {hint}" if hint else f"{exit_code} ({hex_str})"


def _pick_random_asset_file(data_dir, subdir, extensions):
    """从 data_dir/subdir 素材池中随机抽取一个文件(每次调用重新抽取,不缓存)。
    返回 (Path, None) 或 (None, 原因字符串)。供 APITester._pick_random_asset 与
    run_sampleapp_only_tests 共用,避免同一段素材筛选逻辑被复制两份。"""
    if not data_dir:
        return None, "未指定 --data_dir"
    pool_dir = Path(data_dir) / subdir
    if not pool_dir.is_dir():
        return None, f"{pool_dir} 不存在"
    candidates = [f for f in pool_dir.iterdir()
                  if f.is_file() and f.suffix.lower() in extensions]
    if not candidates:
        return None, f"{pool_dir} 下没有找到 {extensions} 素材"
    return random.choice(candidates), None


# SampleApp.cpp 自身定义的、受控的"正常失败"退出码：0=成功,1=模型加载失败,2=推理异常被
# catch 后主动返回,3=打不开输入文件。凡是不在这个集合里的退出码(尤其是大的负数,对应
# Windows NTSTATUS,如 0xC0000005 access violation)都代表进程被操作系统强制终止——
# 这才是真正的崩溃,必须标记 crashed=True,不能和"程序自己判断失败后正常退出"混为一谈。
_SAMPLEAPP_KNOWN_EXIT_CODES = {0, 1, 2, 3}


def _is_sampleapp_crash_exit_code(returncode):
    return returncode not in _SAMPLEAPP_KNOWN_EXIT_CODES


def _capture_log_tail(svc):
    """尝试读取服务进程 stderr/stdout 日志尾部,并把进程真实退出码一并附带,用于崩溃事件诊断。
    退出码是判定"这次崩溃到底是谁的问题"最直接的证据(例如 0xC0000005 access violation
    直接指向程序自身代码缺陷,而不是驱动/环境问题);只有显式实现了 get_exit_code() 的管理类
    (ServiceManager/QAIModelBuilderManager)才会附带这行,RemoteServiceManager 等没有本地
    进程/日志文件的场景下 getattr 拿不到对应属性,行为与之前完全一致(返回空字符串)。"""
    get_exit_code = getattr(svc, "get_exit_code", None)
    exit_code_line = f"exit_code: {_describe_exit_code(get_exit_code())}\n" if callable(get_exit_code) else ""
    read_tail = getattr(svc, "read_log_tail", None)
    if not callable(read_tail):
        return exit_code_line.rstrip("\n")
    stderr_log = getattr(svc, "_stderr_log", None)
    stdout_log = getattr(svc, "_stdout_log", None)
    return f"{exit_code_line}stderr: {read_tail(stderr_log, 30)}\nstdout: {read_tail(stdout_log, 10)}"


# MNN 内存预检查(MnnVerifier)拒绝加载时,无论是 HTTP 响应体的 failure_detail 字段还是
# SampleApp/GenieAPILibrary 进程自身的 stdout 日志,服务端 C++ 侧都统一使用这段文本描述
# 原因,是判定"环境资源约束导致的优雅失败"的精确信号来源,唯一维护点。
_MNN_INSUFFICIENT_MEMORY_MARKER = "insufficient memory to load MNN model"


def _extract_failure_reason(response):
    """从 chat 请求的 HTTP 响应体中提取服务端新增的 failure_reason/failure_detail 字段。
    目前仅 MNN 内存预检查判定内存不足拒绝加载模型时,服务端才会附带这两个字段
    (`{"error": ..., "failure_reason": "insufficient_memory", "failure_detail": "..."}`)；
    这是程序化可识别的权威信号,优先用它判定"服务端优雅拒绝式 OOM",
    而不是继续靠字符串猜测模型名。非 JSON 响应体或不含该字段时返回 (None, None)。"""
    try:
        data = response.json()
    except Exception:
        return None, None
    if isinstance(data, dict):
        reason = data.get("failure_reason")
        if reason:
            return reason, data.get("failure_detail", "")
    return None, None


def _mark_mnn_oom(svc, model_name, detail):
    """在服务进程对象(ServiceManager/RemoteServiceManager 实例)上记录一次 MNN 自身 OOM 事件,
    供同一进程生命周期内其它后端(GGUF/QNN)请求失败时判定"是否为级联受累"提供依据。
    该状态天然与"当前服务进程生命周期"绑定,进程重启时由 restart() 清空。"""
    svc.mnn_oom_event = {
        "timestamp": datetime.now().isoformat(),
        "model_name": model_name,
        "detail": detail,
    }


def _get_mnn_oom_event(svc):
    """读取 svc 上记录的"MNN OOM 已发生"标记,未发生过则返回 None。"""
    return getattr(svc, "mnn_oom_event", None)


def _is_cascading_mnn_oom(svc, model_name):
    """判定这次失败是否是"同一个 MNN 模型此前已被内存预检查(MnnVerifier)优雅拒绝、从未
    加载成功"的级联表现。/profile、/contextsize、/textsplitter 等辅助接口本身的响应体不会
    重新携带 failure_reason(只有直接触发模型加载的请求才会),但它们的失败只是同一次 OOM 的
    后续症状,不应被拆散计成多条独立的 failed(违反"避免错误连坐"的判定原则,见 4.7)。仅当
    该模型是 mnn 后端、且此前(同一进程生命周期内)已经记录过针对这个具体模型名的
    insufficient_memory 事件时才成立。"""
    if infer_backend(model_name)[0] != "mnn":
        return False
    oom_event = _get_mnn_oom_event(svc)
    return bool(oom_event) and oom_event.get("model_name") == model_name


# 进程真正崩溃到 process.poll()/连接被 OS 完全回收之间存在短暂延迟(Windows 上尤其明显,
# 例如崩溃转储采集、句柄清理都需要一点时间)。如果在捕获到 ConnectionError/Timeout 后只做
# 一次立即的 is_alive() 探测就得出结论,真正触发崩溃的那次请求可能因为探测过早而被误判为
# "进程仍存活,普通请求异常"(不计入 crashed),而只有稍晚发出的下一条请求才会命中"进程已死"
# 的判定——结果报告里显示"崩溃于 GET /profile"这类无辜的后续探测,而不是实际造成崩溃的那次
# chat 请求,造成崩溃归因错位、让人误以为是别的接口的问题。这里给一个短暂宽限期,在期限内反复
# 重新探测,让真正致死的那次请求本身就能被正确标记为 crashed=True。
_CRASH_CONFIRM_GRACE_SECONDS = 2.0
_CRASH_CONFIRM_POLL_INTERVAL = 0.2


def _confirm_process_dead(svc, grace_seconds=_CRASH_CONFIRM_GRACE_SECONDS,
                           poll_interval=_CRASH_CONFIRM_POLL_INTERVAL):
    """在捕获到请求异常(ConnectionError/Timeout)后判定服务进程是否真的已经退出。
    立即探测一次为 True 直接返回;否则在宽限期内轮询重试,避免探测早于进程真正终止而
    把这次崩溃错误归因到后续某个无关请求上。宽限期内始终未探测到进程退出则返回 False
    (确实只是这次请求自身的连接异常,进程仍然存活)。"""
    if not svc.is_alive():
        return True
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        time.sleep(poll_interval)
        if not svc.is_alive():
            return True
    return False


def _classify_process_down(svc, model_name):
    """服务进程当前不存活(或已确认自身导致优雅拒绝)时,判定这次失败该如何归类:

    崩溃永远是异常信号，不存在因同进程内 MNN OOM 拖垮进程而级联失败的自动豁免分支。
    正常的、非崩溃的优雅失败（MNN 内存预检查通过 failure_reason=insufficient_memory
    精确识别）不会造成其它模型的连带失败；只有进程真的崩溃才会连带，而崩溃本身必须
    始终被视为需要排查的真实问题，不能被当作可依赖的设计事实去豁免。

    - 当前模型是 MNN: MNN 自身导致的真实OOM事件/崩溃,ignorable=False,
      并在 svc 上记录"MNN OOM 已发生"标记(仅用于下面的诊断标注)。
    - 当前模型非 MNN: 该模型自身的真实问题,ignorable=False。若检测到此前同进程内发生过
      MNN OOM 事件，仅在 detail 中追加"疑似与此前 MNN OOM 相关，需要人工排查根因"这类
      诊断性提示（ignore_reason 标注为 "mnn_oom_cascade" 供报告侧诊断徽章识别），但不
      设置 ignorable=True——一旦真的观测到这种级联崩溃，说明存在需要排查的真实
      崩溃 bug，必须如实计入 failed/crashed 统计。

    返回 (ignorable, ignore_reason, detail)。
    """
    is_mnn = infer_backend(model_name)[0] == "mnn"
    if is_mnn:
        detail = f"MNN 模型自身导致服务进程崩溃/OOM,这是需要关注的真实OOM事件（模型={model_name}）"
        _mark_mnn_oom(svc, model_name, detail)
        return False, "", detail
    oom_event = _get_mnn_oom_event(svc)
    if oom_event:
        detail = (f"疑似与此前 MNN 模型({oom_event['model_name']}) 于 {oom_event['timestamp']} 发生的 OOM 相关"
                   f"而级联崩溃,但崩溃永远是真实问题,不代表豁免,需要人工排查根因 "
                   f"(model={model_name}, ignore_reason=mnn_oom_cascade 仅作诊断标注)")
        return False, "mnn_oom_cascade", detail
    detail = f"服务进程崩溃,且未检测到此前的 MNN OOM 事件,应视为 {model_name} 自身的真实问题"
    return False, "", detail
