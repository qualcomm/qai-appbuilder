#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import argparse
import collections
import io
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

# 强制 stdout/stderr 使用 UTF-8 编码（Windows 控制台兼容）
if sys.platform == 'win32' and getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
    # 幂等包装：若已是 utf-8(例如被 test_watermark.py 之类的脚本 import 前已包装过一次),
    # 不要重复包装,否则旧 TextIOWrapper 被 GC 时会关闭共享的底层 buffer,
    # 导致后续 print() 报 "I/O operation on closed file"。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("ERROR: 'requests' 库未安装，请执行 pip install requests")
    sys.exit(1)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("WARNING: 'psutil' 未安装，性能采集将被跳过。pip install psutil")


# 逻辑核心数：用于把 psutil Process.cpu_percent() 的原始值（单核=100%，多线程/多核累加可能
# 远超 100%，例如 MNN 用满 7-8 个核心时能到 700+%）归一化为"占整机总算力的百分比"展示，
# 避免报告里出现反直觉的大数字。仅用于展示层归一化：PerfMonitor 采集/写入 results.json 的
# 原始样本值不受影响，保持向后兼容。
_LOGICAL_CPU_COUNT = (psutil.cpu_count(logical=True) or 1) if HAS_PSUTIL else 1


def _normalize_cpu_percent(raw_percent):
    """把 Process.cpu_percent() 的原始值归一化为相对整机总算力的百分比（除以逻辑核心数）。
    raw_percent 为 None/0 时原样返回，不做除法。"""
    if not raw_percent:
        return raw_percent
    return raw_percent / _LOGICAL_CPU_COUNT


# ============================================================================
# 预定义测试 Prompts（3 轮使用不同问题）
# ============================================================================
TEST_PROMPTS = [
    "What is the capital of France? Please answer in one sentence.",
    "Explain what a neural network is in simple terms.",
    "Write a short Python function that calculates the factorial of a number.",
]

# 多模态测试用的图文/图音问题
MULTIMODAL_PROMPTS = [
    "What is in this image? Please describe it briefly.",
    "Describe the main content of this image in one sentence.",
]


def detect_modality(model_name):
    """根据模型目录名前缀判断多模态能力: qwen2.5vl→图片, qwen2.5_omini→图片+音频, phi4→图片,
    qwen3_vl→图片, 其余(纯 qwen3/gpt-oss 等)→纯文本(空集合)"""
    lower = model_name.lower()
    if "qwen2.5_omini" in lower or "qwen2.5-omini" in lower:
        return {"image", "audio"}
    if "qwen2.5vl" in lower or "qwen2.5-vl" in lower:
        return {"image"}
    if "phi4" in lower:
        return {"image"}
    if "qwen3_vl" in lower or "qwen3-vl" in lower:
        return {"image"}
    return set()


def infer_backend(model_name):
    """根据模型目录名猜测其后端与设备：GGUF→(GGUF, gpu)，MNN→(mnn, cpu)，其余（QNN）→(qnn, npu)。
    MNN 与 GGUF(llama.cpp) 后端完全不支持多模态，其目录名从不匹配 detect_modality() 的前缀规则。"""
    lower = model_name.lower()
    if "gguf" in lower:
        return "GGUF", "gpu"
    if "mnn" in lower:
        return "mnn", "cpu"
    return "qnn", "npu"


# 纯文本 chat 请求的 3 种 content 格式，按轮次轮流使用，让回归覆盖到服务端支持的全部输入方式，
# 而不是只测最常见的一种。对应关系（详见 examples/GenieAPIClient/GenieAPIClient.cpp 与
# ModelInputBuilder::ProcessArray/ProcessObject）：
#   openai_string: content 为纯字符串——GenieAPIClient 纯文本请求（无 --img）与最基础的 OpenAI
#                  用法一致，走 ModelInputBuilder 的纯字符串分支。
#   openai_array:  content 为 [{"type":"text","text":...}] 数组——OpenAI 标准的多段 content 格式，
#                  走 ModelInputBuilder::ProcessArray（多模态用例已用此路径测图片/音频，这里补上纯文本场景）。
#   client_object: content 为 {"question": ...} 扁平对象——GenieAPIClient 风格（对应其 --img 时构造的
#                  json j; j["question"]=...; j["image"]=...），走 ModelInputBuilder::ProcessObject，
#                  这里只填 question 字段，验证该风格在纯文本(无 image/audio)场景下同样受支持。
CHAT_CONTENT_FORMATS = ("openai_string", "openai_array", "client_object")


def _build_chat_content(prompt, content_format):
    """按 content_format 构造 chat 请求 messages[].content 的值,三种格式语义见 CHAT_CONTENT_FORMATS 上方注释。"""
    if content_format == "openai_array":
        return [{"type": "text", "text": prompt}]
    if content_format == "client_object":
        return {"question": prompt}
    return prompt  # openai_string（默认，向后兼容旧行为）


SYSTEM_PROMPT = "You are a helpful assistant."


def _build_chat_system_content(content_format):
    """system 消息的 content 同样按 content_format 轮换格式,与 _build_chat_content 用途一致,
    确保 GenieRoutingGateway 等读取 system 消息 content 的代码路径也能被三种格式覆盖到
    (GenieAPIClient.exe 默认请求格式对 system/user 消息是同时恒定使用数组形式的)。"""
    return _build_chat_content(SYSTEM_PROMPT, content_format)


def _content_format_label(content_format):
    """content_format → 报告展示用的中文短标签 + CSS 修饰类名后缀。"""
    return {
        "openai_array": ("OpenAI数组风格", "array"),
        "client_object": ("GenieAPIClient对象风格", "object"),
    }.get(content_format, ("OpenAI字符串风格", "string"))


def _content_format_badge_html(r):
    """把 TestResult.request_format 渲染为报告里的小标签(复用 .mode-tag 体系,与既有的
    stream/non-stream 标签同一视觉语言)。request_format 为空(非 chat 用例)时返回空字符串。"""
    content_format = getattr(r, "request_format", "")
    if not content_format:
        return ""
    label, css_suffix = _content_format_label(content_format)
    return f' <span class="mode-tag mode-format-{css_suffix}" title="本次请求 content 字段使用的格式">{label}</span>'


# ============================================================================
# 数据结构
# ============================================================================
@dataclass
class TestResult:
    name: str
    round_num: int
    model_name: str
    passed: bool
    status_code: int
    latency_ms: float
    detail: str
    crashed: bool = False
    skipped: bool = False
    text_response: str = ""
    text_prompt: str = ""
    is_stream: bool = False
    ttft_ms: float = 0.0
    chunk_count: int = 0
    # 本次 chat 请求使用的 content 格式（见 CHAT_CONTENT_FORMATS）：openai_string(默认,向后兼容)/
    # openai_array/client_object。空字符串表示该测试不涉及 chat content 格式（如非 chat 接口）。
    request_format: str = ""
    perf_before: dict = field(default_factory=dict)
    perf_after: dict = field(default_factory=dict)
    response_data: dict = field(default_factory=dict)  # 各接口返回的结构化数据
    # 已知缺陷标记: 当 passed=False 但该失败属于"服务端已知问题、可以忽略"时,
    # 把 ignorable 置为 True 并填 ignore_reason 说明为什么可以忽略。
    # 例如: /images/generations 501 (未实现)、/reload 400 "Not supported in stateless mode"
    # (占位实现)、/status 400 "invalid json" (服务端 set_content type_error bug)。
    # 报告里这类失败仍然显示为 FAIL 但带有"可忽略"小徽章 + tooltip 说明,
    # 总计里单独再算一个 ignored 计数,便于过滤"真实失败 vs 已知缺陷"。
    ignorable: bool = False
    ignore_reason: str = ""
    # 模型能力问题标记(与 ignorable/ignore_reason 同一种成对字段设计)：该测试的结果/检测到的
    # 异常,根因判定为模型自身能力或输出质量局限而非服务端缺陷(如重复出词、tool_calls 未按
    # 指令触发、function.arguments 非合法 JSON)。与 passed/skipped/ignorable 是正交的诊断
    # 维度,不改变任何既有判定逻辑与退出码口径,只是把该结论从纯文本 detail 提升为结构化字段。
    model_capability_issue: bool = False
    model_capability_reason: str = ""
    # MNN OOM 崩溃后已自动重启服务并跳过该模型剩余轮次，不计入 failed/crashed。
    auto_restarted: bool = False
    # 多模态请求实际使用的素材(图片/音频),供 conversations.html 渲染真实内容,而不是只有
    # 一句"该模型支持图片"这类文字描述、看不到到底传了哪张图/哪段音频。存完整 data URI
    # (浏览器可直接用于 <img src=.../<audio src=...>)。这两个字段只在内存中的 TestResult
    # 对象上使用；generate_json()/results.json 是精简过的结构化摘要，不落这两个字段，
    # 避免 JSON 文件因内嵌大量 base64 媒体数据而膨胀。
    media_image_data_uri: str = ""
    media_audio_data_uri: str = ""
    media_asset_label: str = ""


@dataclass
class CrashEvent:
    timestamp: str
    model_name: str
    round_num: int
    endpoint: str
    detail: str
    # 进程崩溃时的 stderr/stdout 日志尾部,便于诊断。远程模式(RemoteServiceManager)
    # 没有本地日志文件时留空,不报错。
    log_tail: str = ""
    # 崩溃发生前最近若干次请求的快照(模型/端点/轮次/请求上下文),用于追溯崩溃发生时
    # 的上下文关联。由 _trace_snapshot() 生成,可能为空字符串(尚无历史记录或调用方未接入)。
    request_history: str = ""


# ============================================================================
# 全局请求追踪环形缓冲区 —— 崩溃可追溯性基础设施
# ============================================================================
# 记录最近若干次 HTTP 请求(跨模型/跨轮次,同一进程生命周期内全局共享,而不是每个 APITester
# 实例各自维护一份),供崩溃发生时回溯"崩溃前到底发生了什么"：测过哪些模型、按什么顺序切换、
# 多模态请求具体用的是哪个素材文件。这是排查"交替执行大语言模型和多模态模型是否导致上下文
# 未清理干净"、"是否某个特定图片诱发崩溃"这类假设时必需的证据链，而不是事后凭猜测归因。
_REQUEST_TRACE_MAXLEN = 12
_request_trace_lock = threading.Lock()
_request_trace = collections.deque(maxlen=_REQUEST_TRACE_MAXLEN)


def _trace_request(model_name, endpoint_name, round_num, context=""):
    """记录一次即将发出的请求(在实际发送前调用,即使随后失败/崩溃也已经被记录下来)。"""
    with _request_trace_lock:
        _request_trace.append({
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "model": model_name, "endpoint": endpoint_name,
            "round": round_num, "context": context,
        })


def _trace_snapshot():
    """返回当前追踪缓冲区的快照文本(按时间顺序,最近的在最后),用于写入 CrashEvent.request_history。"""
    with _request_trace_lock:
        items = list(_request_trace)
    if not items:
        return ""
    return " -> ".join(
        f"[{e['ts']}] {e['model']}/R{e['round']} {e['endpoint']}" + (f" ({e['context']})" if e['context'] else "")
        for e in items
    )


def wait_port_open(host, port, timeout=120, process=None):
    """轮询 TCP 端口直到可连接;不依赖任何 HTTP 路由。

    用于服务启动后等待 `httplib::Server::listen` 真正完成端口绑定,
    避免后续 HTTP 请求出现 ConnectionRefused。

    如果传入 ``process`` (subprocess.Popen),每次轮询前先 poll() 一次:
    一旦发现子进程已退出,立即 raise RuntimeError(带 exit code),
    避免在子进程秒退的情况下还死等满 timeout 秒。
    """
    import socket
    end = time.time() + timeout
    while time.time() < end:
        if process is not None:
            rc = process.poll()
            if rc is not None:
                raise RuntimeError(
                    f"GenieAPIService.exe 启动后立即退出 (exit_code={rc})；"
                    f"端口 {host}:{port} 永远不会监听"
                )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
        time.sleep(0.5)
    return False


def wait_port_closed(host, port, timeout=15):
    """轮询直到 TCP 端口不再可连接(即上一个进程已释放监听)。用于两次连续启动之间
    (如 run_graceful_shutdown_tests 连续对 qnn/GGUF/mnn 三个后端各重启一次)避免
    上一个进程的端口释放存在延迟(TIME_WAIT/OS 调度)导致下一次 start() 命中
    "service already exist."(isPortAvailable() 判定端口被占用而立即退出,exit_code=0)。
    超时仍视为"端口未确认释放"返回 False,但不阻塞太久 —— 调用方仍会继续尝试启动,
    真正的失败原因(端口占用)会在 start() 自身的日志里体现,不会被这里的判断掩盖。"""
    import socket
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            s.close()
        except (ConnectionRefusedError, OSError, socket.timeout):
            return True
        time.sleep(0.5)
    return False


def wait_http_ok(url, timeout=60, expected_status=200, process=None):
    end = time.time() + timeout
    last_error = ""
    while time.time() < end:
        if process is not None:
            rc = process.poll()
            if rc is not None:
                raise RuntimeError(f"HTTP 服务启动后立即退出 (exit_code={rc}); last_error={last_error}")
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == expected_status:
                return True
            last_error = f"status={r.status_code}, body={r.text[:500]}"
        except Exception as e:
            last_error = repr(e)
        time.sleep(1)
    raise RuntimeError(f"HTTP 服务在 {timeout}s 内未就绪: {url}; last_error={last_error}")


def _str2bool(v):
    """argparse 用的宽松 bool 解析：把 true/false/yes/no/1/0（大小写不敏感）转为 bool。"""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "t", "yes", "y", "1", "on"):
        return True
    if s in ("false", "f", "no", "n", "0", "off"):
        return False
    raise argparse.ArgumentTypeError(f"无法解析为 bool: {v!r}")
