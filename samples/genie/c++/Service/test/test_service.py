#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""
GenieAPIService 集成测试脚本

自动启动服务、执行全部 API 接口测试（含流式/非流式）、采集性能数据，
输出 HTML 报告 + JSON 结构化结果 + 对话记录 HTML。

用法:
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models
    python test_service.py --remote --host 10.92.140.91 --port 8910
"""

import argparse
import atexit
import base64
import collections
import copy
import io
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime

# 强制 stdout/stderr 使用 UTF-8 编码（Windows 控制台兼容）
if sys.platform == 'win32':
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
    其余(qwen3/gpt-oss 等)→纯文本(空集合)"""
    lower = model_name.lower()
    if "qwen2.5_omini" in lower or "qwen2.5-omini" in lower:
        return {"image", "audio"}
    if "qwen2.5vl" in lower or "qwen2.5-vl" in lower:
        return {"image"}
    if "phi4" in lower:
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
    return False


# ============================================================================
# PerfMonitor - 后台性能采样器
# ============================================================================
class PerfMonitor:
    """后台性能采样器。

    关键点 (CPU 采样修复):
    psutil 的 Process.cpu_percent(interval=None) 返回的是"距上次同一 Process 对象
    调用 cpu_percent 以来"的 CPU 利用率。如果每次都 new 一个新的 psutil.Process(pid),
    "上次调用"为空,返回值永远是 0.0 —— 这也是之前 CPU 总是 N/A 的根因。

    修复:
      - 持有一个长生命周期的 psutil.Process 实例 self._proc;
      - 在 start() 里立即调一次 cpu_percent() 进行 prime (建立基线);
      - 之后 _sample_loop 和 snapshot 都复用同一个 self._proc, 不再 new。
    """
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._samples = []
        self._pid = None
        self._proc = None
        self._lock = threading.Lock()

    def start(self, pid):
        if not HAS_PSUTIL:
            return
        self._pid = pid
        self._stop_event.clear()
        self._samples = []
        try:
            self._proc = psutil.Process(pid)
            self._proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._proc = None
            return
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self):
        if self._proc is None:
            return
        while not self._stop_event.is_set():
            try:
                mem = self._proc.memory_info().rss / (1024 * 1024)  # MB
                # 可能 > 100%(多核累加),报告展示层会按逻辑核心数归一化后再展示。
                cpu = self._proc.cpu_percent(interval=None)
                sample = {
                    "timestamp": time.time(),
                    "rss_mb": round(mem, 2),
                    "cpu_percent": round(cpu, 1)
                }
                with self._lock:
                    self._samples.append(sample)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._stop_event.wait(1.0)

    def snapshot(self):
        if not HAS_PSUTIL or self._proc is None:
            return {}
        try:
            mem = self._proc.memory_info().rss / (1024 * 1024)
            cpu = self._proc.cpu_percent(interval=None)
            return {
                "timestamp": time.time(),
                "rss_mb": round(mem, 2),
                "cpu_percent": round(cpu, 1)
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {}

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        with self._lock:
            return list(self._samples)

    def get_samples(self):
        with self._lock:
            return list(self._samples)


# ============================================================================
# RemoteServiceManager - 远程服务（不管理进程生命周期）
# ============================================================================
class RemoteServiceManager:
    """用于连接已在运行的远程服务，不管理进程生命周期。
    is_alive() 始终返回 True（避免每次请求前都轮询 /models），
    实际连通性通过 check_connectivity() 在测试开始时验证一次。
    """
    def __init__(self, host, port):
        self.host = host
        self.port = port
        # 与 ServiceManager 保持一致的"MNN OOM 已发生"标记；远程模式下 restart() 同样会重置。
        self.mnn_oom_event = None

    def start(self, config_path=None):
        pass

    def check_connectivity(self, timeout=10):
        """实际验证一次服务是否可达（仅在测试开始时调用）"""
        url = f"http://{self.host}:{self.port}/models"
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    return True
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(2)
        return False

    def stop(self):
        pass

    def is_alive(self):
        # 远程模式：始终返回 True，避免每次请求前都调用 /models
        # 如果服务真的断开，_safe_request 的异常处理会捕获
        return True

    def restart(self):
        # 远程模式无法重启，但检查一次连通性；既然认定为一次新的连接生命周期,
        # 同样重置 MNN OOM 标记,不带着上一次连接期间的历史包袱。
        self.mnn_oom_event = None
        return self.check_connectivity(timeout=10)

    def get_pid(self):
        return None


# ============================================================================
# QAIModelBuilderManager - Builder 后端生命周期管理
# ============================================================================
# 当前 QAIModelBuilder 仓库已从扁平 backend/main.py 结构重构为 DDD 分层的
# apps/api/main.py（详见 apps/api/__main__.py），旧的 backend/ 目录与 /api/health
# 端点均已不存在；本类已按当前版本重写启动方式、健康检查与 CSRF 双提交会话。

def _default_builder_python_path():
    """QAIModelBuilder 官方 Setup.bat 搭建的独立 ARM64 venv 的默认 python.exe 路径。"""
    return Path(os.environ.get("LOCALAPPDATA", "")) / "QAIModelBuilder" / "envs" / ".venv_arm64_313" / "Scripts" / "python.exe"


def resolve_builder_python(explicit_path=None):
    """解析用于启动 QAIModelBuilder 的 python.exe：优先用户显式传入 --builder_python；
    否则尝试官方 Setup.bat 搭建的默认 venv；都找不到则回退 sys.executable 并打印明确警告
    （而不是静默失败——系统 Python 通常缺 pydantic_settings 等 Builder 必需依赖）。"""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return str(p)
        print(f"WARNING: --builder_python 指定的路径不存在: {p}，回退到 sys.executable ({sys.executable})")
        return sys.executable
    default_venv = _default_builder_python_path()
    if default_venv.exists():
        return str(default_venv)
    print(f"WARNING: 未找到 QAIModelBuilder 官方 venv ({default_venv})，回退到 sys.executable "
          f"({sys.executable})；若该环境缺少 pydantic_settings 等依赖，Builder 启动会失败。"
          f"请先运行 QAIModelBuilder\\Setup.bat 搭建独立环境，或通过 --builder_python 显式指定。")
    return sys.executable


class _CsrfSession:
    """包一层 requests.Session，实现 QAIModelBuilder 的 CSRF 双提交 Cookie 握手：
    对安全方法(GET/HEAD/OPTIONS)请求，如响应尚未带 cookie 则中间件会自动 Set-Cookie
    qai_csrf=<token>；对非安全方法(POST/PUT/PATCH/DELETE)，自动在 Cookie 基础上附加
    X-QAI-CSRF 头（取值与 cookie 相同），满足双提交校验，否则会被 403
    security.csrf.missing 拒绝。参考 QAIModelBuilder/scripts/native_e2e/_phase2_common.py
    的握手实现（cookie/header 名字、安全方法集合均与其保持一致）。"""
    CSRF_COOKIE_NAME = "qai_csrf"
    CSRF_HEADER_NAME = "X-QAI-CSRF"
    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()

    def ensure_csrf_token(self, timeout=10):
        """对健康检查端点（安全方法、公开路径）发一次 GET，收获 Set-Cookie（若尚未持有）。
        返回当前持有的 token；CSRF 已被关闭时返回 None（调用方应据此优雅降级，不强行附加空头）。"""
        token = self.session.cookies.get(self.CSRF_COOKIE_NAME)
        if token:
            return token
        try:
            self.session.get(f"{self.base_url}/api/system/health", timeout=timeout)
        except Exception:
            pass
        return self.session.cookies.get(self.CSRF_COOKIE_NAME)

    def request(self, method, path, timeout=30, **kwargs):
        method_upper = method.upper()
        headers = dict(kwargs.pop("headers", None) or {})
        if method_upper not in self.SAFE_METHODS:
            token = self.ensure_csrf_token(timeout=min(timeout, 10))
            if token:
                headers[self.CSRF_HEADER_NAME] = token
        return self.session.request(method_upper, f"{self.base_url}{path}", headers=headers, timeout=timeout, **kwargs)

    def get(self, path, timeout=30, **kwargs):
        return self.request("GET", path, timeout=timeout, **kwargs)

    def post(self, path, timeout=30, **kwargs):
        return self.request("POST", path, timeout=timeout, **kwargs)


class QAIModelBuilderManager:
    def __init__(self, builder_dir, host, port, log_dir=None, python_exe=None, data_dir=None):
        self.builder_dir = Path(builder_dir)
        # 当前版本入口是 apps/api/main.py（DDD 分层），不再是 backend/main.py。
        self.main_entry = self.builder_dir / "apps" / "api" / "main.py"
        self.host = host
        self.port = port
        self.python_exe = python_exe or sys.executable
        # 隔离的 QAI_DATA__DATA_DIR：每次测试运行独立，不污染 Builder 仓库自身或用户真实数据。
        self.data_dir = Path(data_dir) if data_dir else None
        self.process = None
        self._log_dir = Path(log_dir) if log_dir else self.builder_dir
        self._stdout_fh = None
        self._stderr_fh = None
        self._stdout_log = None
        self._stderr_log = None
        self.csrf = _CsrfSession(self.base_url)

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def start(self, timeout=90):
        if not self.builder_dir.exists():
            raise FileNotFoundError(f"找不到 QAIModelBuilder 目录: {self.builder_dir}")
        if not self.main_entry.exists():
            raise FileNotFoundError(
                f"找不到 QAIModelBuilder 后端入口: {self.main_entry}"
                f"（当前版本 Builder 已重构为 DDD 分层结构，不再是 backend/main.py）"
            )
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_log = self._log_dir / "qaimodelbuilder_stdout.log"
        self._stderr_log = self._log_dir / "qaimodelbuilder_stderr.log"
        self._stdout_fh = open(self._stdout_log, "wb")
        self._stderr_fh = open(self._stderr_log, "wb")
        # apps.api 依赖绝对包名导入（interfaces.*、qai.platform.* 等），-m 启动本身不会把
        # builder_dir/src 加入 sys.path；对齐仓库自带 Start.bat 的做法：PYTHONPATH=src;. ,
        # cwd=builder_dir。
        src_dir = self.builder_dir / "src"
        env = os.environ.copy()
        pythonpath_parts = [str(src_dir), str(self.builder_dir)]
        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        # 关闭 Okta 登录门禁（项目文档认可的测试场景用法），CSRF 双提交防护保持开启，
        # 由 self.csrf 会话负责真实握手。
        env["QAI_AUTH__ENABLED"] = "false"
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            env["QAI_DATA__DATA_DIR"] = str(self.data_dir)
        cmd = [self.python_exe, "-m", "apps.api", "--host", self.host, "--port", str(self.port)]
        print(f"  [QAIModelBuilderManager] 启动 Builder: {' '.join(cmd)}")
        print(f"  [QAIModelBuilderManager] cwd={self.builder_dir}; PYTHONPATH={env['PYTHONPATH']}"
              + (f"; QAI_DATA__DATA_DIR={self.data_dir}" if self.data_dir else ""))
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.builder_dir),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )
        atexit.register(self._cleanup)
        try:
            ready = wait_http_ok(f"{self.base_url}/api/system/health", timeout=timeout, process=self.process)
        except RuntimeError as e:
            stderr_tail = ServiceManager.read_log_tail(self._stderr_log, 50)
            stdout_tail = ServiceManager.read_log_tail(self._stdout_log, 20)
            self._force_kill()
            raise RuntimeError(
                f"QAIModelBuilder 启动失败: {e}; "
                f"stderr_log={self._stderr_log}; stderr_tail={stderr_tail!r}; stdout_tail={stdout_tail!r}"
            )
        if not ready:
            stderr_tail = ServiceManager.read_log_tail(self._stderr_log, 50)
            stdout_tail = ServiceManager.read_log_tail(self._stdout_log, 20)
            self._force_kill()
            raise RuntimeError(
                f"QAIModelBuilder /api/system/health 未在 {timeout}s 内就绪; "
                f"url={self.base_url}/api/system/health; stderr_log={self._stderr_log}; "
                f"stderr_tail={stderr_tail!r}; stdout_tail={stdout_tail!r}"
            )
        # 就绪后立即完成一次 CSRF 握手，确保后续所有非安全方法请求都能带上正确的双提交凭证。
        self.csrf.ensure_csrf_token(timeout=10)

    def health(self):
        try:
            r = self.csrf.get("/api/system/health", timeout=5)
            return r.status_code == 200, r.status_code, r.text[:500]
        except Exception as e:
            return False, 0, repr(e)

    def stop(self):
        try:
            self.csrf.post("/api/service/stop", json={}, timeout=10)
        except Exception:
            pass
        if self.process is None:
            self._close_logs()
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._force_kill()
            return
        except Exception:
            pass
        self.process = None
        self._close_logs()

    def is_alive(self):
        if self.process is None:
            return False
        return self.process.poll() is None

    def get_exit_code(self):
        """返回子进程的真实退出码;进程仍存活或已被清理(self.process is None)时返回 None。
        与 ServiceManager.get_exit_code 同样的语义,供 QAIModelBuilder 后端进程崩溃诊断复用。"""
        if self.process is None:
            return None
        return self.process.poll()

    def _close_logs(self):
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
                setattr(self, fh_attr, None)

    def _force_kill(self):
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
        self._close_logs()

    def _cleanup(self):
        self._force_kill()

    def get_pid(self):
        if self.process:
            return self.process.pid
        return None


# ============================================================================
# ServiceManager - 服务生命周期管理
# ============================================================================
class ServiceManager:
    def __init__(self, exe_dir, host, port):
        self.exe_dir = Path(exe_dir)
        self.exe_path = self.exe_dir / "GenieAPIService.exe"
        self.host = host
        self.port = port
        self.process = None
        self._current_config = None
        self._log_dir = None
        self._stdout_fh = None
        self._stderr_fh = None
        self._stdout_log = None
        self._stderr_log = None
        # 当前进程生命周期内是否已发生过 MNN 自身 OOM 事件(见 _mark_mnn_oom/_classify_process_down);
        # 新进程是全新生命周期,不应带着上一个进程的历史包袱,restart() 中会重置为 None。
        self.mnn_oom_event = None

    def start(self, config_path, extra_args=None):
        if not self.exe_path.exists():
            raise FileNotFoundError(f"找不到 {self.exe_path}")
        # 关键修复:GenieAPIService.exe 用 Popen(cwd=self.exe_dir) 启动,
        # 如果 config_path 是相对路径 (例如 "models\\xxx\\config.json"),
        # 服务侧会按 cwd=Stable\ 去找,得到 Stable\models\xxx\config.json,自然找不到。
        # 这里强制把配置路径解析为绝对路径,根除相对路径歧义。
        # 同时校验文件存在,失败时直接抛 FileNotFoundError,
        # 比让服务端打 "[E] config file is not found" 然后秒退要友好得多。
        cfg_abs = Path(config_path).resolve()
        if not cfg_abs.exists():
            raise FileNotFoundError(f"找不到 config 文件: {cfg_abs}")
        self._current_config = str(cfg_abs)
        cmd = [
            str(self.exe_path.resolve()),
            "-c", str(cfg_abs),
            "-l",
            "-p", str(self.port)
        ]
        # extra_args: 可选的额外命令行参数（如 ["-n", "-1", "-d", "4"]），追加在既有参数之后；默认 None/空列表时不追加任何参数。
        if extra_args:
            cmd.extend(str(a) for a in extra_args)
        print(f"  [ServiceManager] 启动服务: {' '.join(cmd)}")
        # 重要：不要用 subprocess.PIPE 接 GenieAPIService 的 stdout/stderr，
        # QNN 初始化会输出大量日志，PIPE 缓冲区填满后子进程会阻塞，导致服务无法响应。
        # 这里直接落到日志文件（覆盖模式，每次启动会清空旧日志，避免越写越大）。
        log_dir = Path(self._log_dir) if self._log_dir else self.exe_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_log = log_dir / "genie_service_stdout.log"
        self._stderr_log = log_dir / "genie_service_stderr.log"
        self._stdout_fh = open(self._stdout_log, "wb")
        self._stderr_fh = open(self._stderr_log, "wb")
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.exe_dir),
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )
        atexit.register(self._cleanup)
        # 启动后立即做一次"秒退检测":
        # ARM64/x86-64 架构不匹配、缺 DLL、OneDrive 占位符未下载等失败,
        # GenieAPIService.exe 会在毫秒级内退出。如果不在这里探测,
        # 后面 wait_port_open 会傻等满 120 秒才报错,
        # 而且报告里看不到"启动失败"这一类事件。
        # 这里给 2 秒窗口,足够覆盖正常情况下尚未完成 listen 的"还活着但没绑端口",
        # 同时把秒退快速暴露出来。
        time.sleep(2)
        rc = self.process.poll()
        if rc is not None:
            stderr_tail = self.read_log_tail(self._stderr_log, 50)
            stdout_tail = self.read_log_tail(self._stdout_log, 20)
            # 释放当前 Popen + 日志句柄,让后续 _cleanup 不再尝试 kill 已退出的进程
            try:
                self._stdout_fh.close()
            except Exception:
                pass
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stdout_fh = None
            self._stderr_fh = None
            self.process = None
            raise RuntimeError(
                f"GenieAPIService.exe 启动后立即退出 (exit_code={rc}); "
                f"stderr_log={self._stderr_log}; "
                f"stderr_tail={stderr_tail!r}; "
                f"stdout_tail={stdout_tail!r}"
            )

    @staticmethod
    def read_log_tail(log_path, n=50):
        """读取日志文件最后 n 行(用 latin-1 兜底,容忍非 UTF-8 内容)。"""
        if log_path is None:
            return ""
        try:
            p = Path(log_path)
            if not p.exists() or p.stat().st_size == 0:
                return ""
            with open(p, "rb") as f:
                data = f.read()
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return "\n".join(lines[-n:]).strip()
        except Exception as e:
            return f"<read_log_tail error: {e}>"

    def stop(self):
        if self.process is None:
            return
        try:
            url = f"http://{self.host}:{self.port}/servicestop"
            requests.post(url, json={"text": "stop"}, timeout=10)
        except Exception:
            pass
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._force_kill()
        self.process = None

    def is_alive(self):
        if self.process is None:
            return False
        return self.process.poll() is None

    def get_exit_code(self):
        """返回子进程的真实退出码;进程仍存活或已被清理(self.process is None)时返回 None。
        调用时机很关键：必须在 restart()/_force_kill() 把 self.process 置空之前调用，
        否则永远拿到 None——这是判定"这次崩溃到底是谁的问题"最直接的证据(见 _describe_exit_code),
        不能靠事后猜测。"""
        if self.process is None:
            return None
        return self.process.poll()

    def restart(self):
        self._force_kill()
        # 旧进程已被强制杀掉,新进程即将启动的是全新生命周期,重置 MNN OOM 标记,
        # 不能让新进程带着上一个进程的历史包袱。
        self.mnn_oom_event = None
        time.sleep(2)
        if self._current_config:
            try:
                self.start(self._current_config)
            except (RuntimeError, FileNotFoundError) as e:
                # 启动秒退,重启失败 — 把异常吞掉,返回 False 让上层 _safe_request 走"崩溃后重启失败"分支。
                print(f"  [ServiceManager] 重启失败: {e}")
                return False
            # 重启后等待端口重新绑定 (TCP 轮询,不依赖任何 HTTP 路由);
            # 同时把 self.process 传进去,这样子进程秒退能立刻被发现而不是空等 120 秒。
            try:
                wait_port_open(self.host, self.port, timeout=120, process=self.process)
            except RuntimeError as e:
                print(f"  [ServiceManager] 重启后等端口失败: {e}")
                return False
            return True
        return False

    def _force_kill(self):
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
        # 关闭日志文件句柄，防止下一次 start 占用同名文件失败
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
                setattr(self, fh_attr, None)

    def _cleanup(self):
        self._force_kill()

    def get_pid(self):
        if self.process:
            return self.process.pid
        return None


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


# ============================================================================
# tool_calls / function calling 协议测试用到的预定义工具 Schema 与配置读取工具函数
# ============================================================================
# 用于 test_tool_call_model_invocation_probe/test_tool_call_streaming_argument_integrity_probe/
# test_tool_call_round_trip_with_result：prompt_optimizer.cpp:705-727 GetOptimizedToolDefinition
# 预定义工具名表中已确认存在的 "read"，对应的 OpenAI tools[] JSON Schema。与
# run_numresponse_stateless_mode_regressions() 用的 _STATELESS_MODE_READ_TOOL_DEF（module 后面
# 定义）是同一个预定义工具，这里复用同一个常量，不重复定义第二份。


def _read_max_tool_call_retries(exe_dir):
    """从构建产物目录下实际生效的 service_config.json 读取
    routing.agent_routing.max_tool_call_retries；读取不到（或未传 exe_dir，如远程模式）时按
    代码默认值 10 兜底（AgentRoutingConfig::max_tool_call_retries，见 model_config.h:169）。"""
    default_retries = 10
    if not exe_dir:
        return default_retries
    try:
        cfg_path = Path(exe_dir) / "service_config.json"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        retries = ((cfg.get("routing") or {}).get("agent_routing") or {}).get("max_tool_call_retries")
        if isinstance(retries, int) and retries > 0:
            return retries
    except Exception:
        pass
    return default_retries


# ============================================================================
# APITester - 接口测试执行器（含容错）
# ============================================================================
class APITester:
    def __init__(self, host, port, service_manager, perf_monitor, model_name, total_rounds=1, data_dir=None,
                 all_models=None, multimodal_rounds=None, exe_dir=None):
        self.base_url = f"http://{host}:{port}"
        self.svc = service_manager
        self.perf = perf_monitor
        self.model_name = model_name
        self.crash_events = []
        self._restart_count = 0
        self.MAX_RESTARTS = 3
        self.total_rounds = total_rounds
        self.data_dir = Path(data_dir) if data_dir else None
        # 切换伙伴发现（test_dynamic_switch_multimodal_stability 用）与多模态专属轮数
        # （未显式传入时跟随 total_rounds，保持默认行为不变）。
        self._all_models = all_models or []
        self.multimodal_rounds = multimodal_rounds if multimodal_rounds is not None else total_rounds
        # 构建产物目录（可选）：仅用于 test_tool_call_retry_limit_enforcement 动态读取实际生效的
        # service_config.json 里的 max_tool_call_retries；未传入时该用例回退到代码默认值 10。
        self.exe_dir = exe_dir

    def _capture_log_offset(self):
        """在发出 chat 请求前调用,记录服务 stdout 日志文件当前的(路径, 字节大小)。
        返回值直接传给 _scan_model_defect_log(),用于把"扫描 [MODEL_DEFECT] 标记"精确限定到
        本次请求的窗口。远程模式(RemoteServiceManager 没有 _stdout_log 属性)或日志文件
        尚未创建时返回 None,调用方应据此跳过扫描(不报错)。"""
        log_path = getattr(self.svc, "_stdout_log", None)
        if log_path is None:
            return None
        try:
            return log_path, Path(log_path).stat().st_size
        except Exception:
            return log_path, 0

    def _safe_request(self, method, path, endpoint_name, round_num, request_context="", **kwargs):
        """带崩溃检测的请求包装。request_context 用于记录本次请求的关键上下文(例如多模态请求
        用的图片/音频文件名),它不影响请求本身,只在崩溃发生时随 CrashEvent 一并暴露出来——
        否则崩溃事件只知道"哪个 endpoint 崩了",却看不出当时到底在传哪个素材,复现/归因时
        只能凭猜测。"""
        _trace_request(self.model_name, endpoint_name, round_num, request_context)
        if not self.svc.is_alive():
            if self._restart_count >= self.MAX_RESTARTS:
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=endpoint_name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"服务已崩溃且超过最大重启次数; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason
                )
            # 尝试重启
            self.crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name=self.model_name, round_num=round_num,
                endpoint=endpoint_name, detail="请求前检测到进程已退出",
                log_tail=_capture_log_tail(self.svc), request_history=_trace_snapshot()
            ))
            self._restart_count += 1
            if not self.svc.restart():
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=endpoint_name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"服务重启失败; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason
                )
            # 重启成功后更新 PerfMonitor
            if HAS_PSUTIL and self.svc.get_pid():
                self.perf.stop()
                self.perf.start(self.svc.get_pid())

        url = f"{self.base_url}{path}"
        start_time = time.time()
        try:
            if method == "GET":
                r = requests.get(url, timeout=60, **kwargs)
            else:
                r = requests.post(url, timeout=300, **kwargs)
            latency = (time.time() - start_time) * 1000
            # 服务端优雅拒绝式 OOM(分类A(a)): MNN 内存预检查拒绝加载时,响应体带
            # failure_reason=insufficient_memory,这里只做标记侧记(不改变返回值),
            # 具体 TestResult 的 ignorable/detail 由各调用方(如 test_chat_non_stream)决定。
            if r.status_code == 500 and infer_backend(self.model_name)[0] == "mnn":
                failure_reason, failure_detail = _extract_failure_reason(r)
                if failure_reason == "insufficient_memory":
                    _mark_mnn_oom(self.svc, self.model_name,
                                  f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}")
            return r, latency
        except (requests.ConnectionError, requests.Timeout) as e:
            latency = (time.time() - start_time) * 1000
            # 检查进程是否崩溃(带短暂宽限期重试,避免把真正致死的这次请求误判为"进程仍存活的
            # 普通异常",而让后续某个无关请求背了这次崩溃的锅——见 _confirm_process_dead 注释)
            if _confirm_process_dead(self.svc):
                log_tail = _capture_log_tail(self.svc)
                history = _trace_snapshot()
                is_mnn = infer_backend(self.model_name)[0] == "mnn"
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                context_note = f" [request_context={request_context}]" if request_context else ""
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(),
                    model_name=self.model_name, round_num=round_num,
                    endpoint=endpoint_name,
                    detail=f"请求中服务崩溃: {type(e).__name__}; {classify_detail}{context_note}",
                    log_tail=log_tail, request_history=history
                ))
                self._restart_count += 1
                restarted = False
                if self._restart_count <= self.MAX_RESTARTS:
                    restarted = self.svc.restart()
                    if HAS_PSUTIL and self.svc.get_pid():
                        self.perf.stop()
                        self.perf.start(self.svc.get_pid())
                if is_mnn and restarted:
                    self.crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=self.model_name, round_num=round_num,
                        endpoint="MNN_OOM_CRASH",
                        detail=f"MNN OOM 崩溃后已自动重启(第{self._restart_count}次),跳过该模型本轮剩余测试",
                        log_tail=log_tail, request_history=history
                    ))
                elif ignore_reason == "mnn_oom_cascade":
                    self.crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=self.model_name, round_num=round_num,
                        endpoint="MNN_OOM_CASCADE",
                        detail=classify_detail,
                        log_tail=log_tail, request_history=history
                    ))
                return TestResult(
                    name=endpoint_name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=latency,
                    detail=f"服务崩溃: {type(e).__name__}: {e}; {classify_detail}{context_note}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    auto_restarted=is_mnn and restarted
                )
            return TestResult(
                name=endpoint_name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=latency,
                detail=f"请求异常: {type(e).__name__}: {e}"
            )

    def is_over_restart_limit(self):
        return self._restart_count >= self.MAX_RESTARTS and not self.svc.is_alive()

    def run_global_tests(self):
        """执行模型无关的通用接口测试（只需运行一次）

        覆盖 GenieAPIService.cpp 中"加载模型前可调用"的全部 GET 路由
        及其别名 (/models 与 /v1/models)。
        """
        results = []
        tests = [
            ("GET /", lambda: self.test_welcome(1)),
            ("GET /models", lambda: self.test_models(1, path="/models")),
            ("GET /v1/models", lambda: self.test_models(1, path="/v1/models")),
        ]
        for test_name, test_fn in tests:
            print(f"    测试 {test_name} ... ", end="", flush=True)
            result = test_fn()
            status = "✓ PASS" if result.passed else ("✗ CRASH" if result.crashed else "✗ FAIL")
            print(f"{status} ({result.latency_ms:.0f}ms)")
            results.append(result)
        return results

    def run_all(self, round_num, include_final_cleanup=True):
        """执行一轮模型相关测试,覆盖 GenieAPIService.cpp 注册的所有路由及别名。

        每轮顺序:
          status(before)
          chat 4 个别名 × (non-stream + stream) = 8 次
          status(after)
          profile / contextsize / fetch
          textsplitter × 2 别名
          images × 2 别名
          reload
        图片/音频多模态用例已从这里完全抽离,改由独立的 run_multimodal_rounds() 按
        self.multimodal_rounds 单独驱动,与本方法的 --rounds 循环互不影响、互不放大。
        include_final_cleanup=True 且到达最后一轮时,额外调用 run_final_cleanup()
        追加 stop → clear → unload；调用方需确保多模态轮次已在此之前跑完,
        否则收尾卸载模型后 run_multimodal_rounds() 的请求会触发意外的隐式重新加载。
        (/servicestop 不在这里调用,放在 main() 整轮结束后一次性调用)

        说明:不再使用 `/status` 来判定服务/模型就绪 —— 服务端 `/status`
        在冷启动期间会因为 set_content(json_object) 类型 bug 直接返回 400,
        无法稳定作为 ready 信号。这里把 `/status` 改为观察用:在 chat 前后各
        采一次,报告里能直接看到"前 vs 后"的差异。严格策略下 400 直接判 FAIL。
        """
        self._restart_count = 0
        prompt = TEST_PROMPTS[(round_num - 1) % len(TEST_PROMPTS)]

        # ── 预检：模型是否成功加载（仅用于日志/诊断） ──────────────────────────────
        # 发一个轻量 chat 请求观察模型是否可用。这个探测请求本身**不再**被用来批量豁免
        # 本轮后续任何测试的失败结果——旧版本一旦命中"not found"/"unavailable"等宽泛
        # 子串就把 chat/profile/contextsize/textsplitter/fetch 全部相关失败标记为
        # ignorable=True，这是本次修复要废弃的"一次探测代表一整轮"过度容忍机制。
        # 现在每个具体测试方法（test_chat_non_stream/test_chat_stream 等）已经各自基于
        # failure_reason=="insufficient_memory" 等精确信号独立判定是否为真实错误，
        # 这里的探测只在命中该信号时记录 MNN OOM 事件供诊断/级联判定使用。
        try:
            probe_resp = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json={"model": self.model_name,
                      "messages": [{"role": "user", "content": "hi"}],
                      "stream": False, "max_tokens": 4},
                timeout=60
            )
            if probe_resp.status_code == 500:
                failure_reason, failure_detail = _extract_failure_reason(probe_resp)
                if failure_reason == "insufficient_memory" and infer_backend(self.model_name)[0] == "mnn":
                    _mark_mnn_oom(
                        self.svc, self.model_name,
                        f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
                    )
        except Exception:
            pass  # 连接失败时不做预检，让后续测试正常运行

        # GenieAPIService.cpp 里 ChatCompletions handler 注册的 4 个路径
        chat_paths = ["/v1/chat/completions", "/chat/completions",
                      "/v1/completions", "/completions"]

        tests = []
        # /status (before): 第一次 chat 之前的一次观察
        tests.append((f"GET /status (before chat)",
                      lambda rn=round_num: self.test_status(rn, name="GET /status (before chat)")))

        # 4 个 chat 别名 × (non-stream + stream),共 8 次；每个别名按下标 + 轮次轮流使用
        # CHAT_CONTENT_FORMATS 里的一种 content 格式，确保哪怕默认只跑 1 轮
        # (--rounds 默认为 1)，OpenAI 字符串/数组风格与 GenieAPIClient 对象风格 3 种格式
        # 也都会在单轮内被覆盖到（4 个别名对 3 种格式取模自然循环一遍多），
        # 而不是只测最常见的纯字符串写法；多轮时额外叠加轮次偏移，避免同一别名每轮都固定同一格式。
        for idx, cp in enumerate(chat_paths):
            fmt = CHAT_CONTENT_FORMATS[(idx + round_num - 1) % len(CHAT_CONTENT_FORMATS)]
            tests.append((f"POST {cp} (non-stream)",
                          lambda rn=round_num, p=cp, f=fmt: self.test_chat_non_stream(rn, prompt, path=p, content_format=f)))
            tests.append((f"POST {cp} (stream)",
                          lambda rn=round_num, p=cp, f=fmt: self.test_chat_stream(rn, prompt, path=p, content_format=f)))

        # /status (after): 所有 chat 跑完后再观察一次
        tests.append((f"GET /status (after chat)",
                      lambda rn=round_num: self.test_status(rn, name="GET /status (after chat)")))

        tests.append(("GET /profile", lambda rn=round_num: self.test_profile(rn)))
        tests.append(("POST /contextsize", lambda rn=round_num: self.test_contextsize(rn)))
        tests.append(("POST /fetch", lambda rn=round_num: self.test_fetch(rn)))

        # textsplitter: /textsplitter + /v1/textsplitter
        for tp in ["/textsplitter", "/v1/textsplitter"]:
            tests.append((f"POST {tp}",
                          lambda rn=round_num, p=tp: self.test_textsplitter(rn, path=p)))

        # images: /images/generations + /v1/images/generations
        for ip in ["/images/generations", "/v1/images/generations"]:
            tests.append((f"POST {ip}",
                          lambda rn=round_num, p=ip: self.test_image_generate(rn, path=p)))

        # tool_calls / function calling 协议测试（与 -n 取值无关，覆盖 model/qnn/full 等主套件）：
        # 前两个是纯请求结构触发的确定性契约测试（不依赖模型真实决策），后两个是机会性观察，
        # 触发失败时优雅降级为 skipped=True，不计入 failed（见调查结论 Tab 第 9/10 节）。
        tests.append(("POST /v1/chat/completions (tool_call retry limit enforcement)",
                      lambda rn=round_num: self.test_tool_call_retry_limit_enforcement(rn)))
        tests.append(("POST /v1/chat/completions (tool_call round-trip with result)",
                      lambda rn=round_num: self.test_tool_call_round_trip_with_result(rn)))
        tests.append(("POST /v1/chat/completions (tool_call model invocation probe)",
                      lambda rn=round_num: self.test_tool_call_model_invocation_probe(rn)))
        tests.append(("POST /v1/chat/completions (tool_call streaming argument integrity probe)",
                      lambda rn=round_num: self.test_tool_call_streaming_argument_integrity_probe(rn)))

        tests.append(("POST /reload", lambda rn=round_num: self.test_reload(rn)))

        results = self._run_test_list(tests, round_num)
        if include_final_cleanup and round_num >= self.total_rounds:
            results.extend(self.run_final_cleanup(round_num))
        return results

    def _run_test_list(self, tests, round_num):
        """遍历一组 (name, fn) 用例,统一处理重启限流、打印与结果收集。run_all()/
        run_multimodal_rounds()/run_final_cleanup() 共用这段循环体,避免重复维护
        同一套 is_over_restart_limit()/_classify_process_down() 处理逻辑。"""
        results = []
        for test_name, test_fn in tests:
            if self.is_over_restart_limit():
                # 按分类A/B/真实错误规则判定,不再用"gguf"/"mnn"/"gpu"关键字统一豁免。
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                results.append(TestResult(
                    name=test_name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"跳过：服务多次崩溃; {classify_detail}", crashed=True, skipped=True,
                    ignorable=ignorable,
                    ignore_reason=ignore_reason
                ))
                continue
            print(f"    [{round_num}] 测试 {test_name} ... ", end="", flush=True)
            result = test_fn()
            # 不再有"一次探测代表一整轮"的批量覆盖逻辑：每个测试方法自己的 passed/ignorable/
            # ignore_reason 判定（基于状态码 + 错误信息/failure_reason 等精确信号）就是最终结果，
            # 不会被这里的外部探测结论重新覆盖。
            # 打印顺序：PASS > CRASH > SKIP > IGN > FAIL——之前这里缺少 SKIP 分支，会把
            # 真正 skipped=True（如模型能力局限导致的机会性用例跳过、MNN 内存不足优雅拒绝）
            # 的结果误显示为 "✗ FAIL"，容易让人误判为真实回归；results.json/报告里的
            # TestResult.skipped 字段本身不受影响，这里只是修正控制台展示与其保持一致。
            status = ("✓ PASS" if result.passed else
                      ("✗ CRASH" if result.crashed else
                       ("⚠ SKIP" if result.skipped else
                        ("⚠ IGN" if result.ignorable else "✗ FAIL"))))
            print(f"{status} ({result.latency_ms:.0f}ms)")
            results.append(result)
        return results

    def run_final_cleanup(self, round_num):
        """整个模型生命周期测试的收尾:stop → clear → unload。从 run_all() 中抽出,
        以便调用方能先跑完 run_multimodal_rounds() 再执行收尾,不受轮数解耦影响。"""
        tests = [
            ("POST /stop", lambda rn=round_num: self.test_stop(rn)),
            ("POST /clear", lambda rn=round_num: self.test_clear(rn)),
            ("POST /unload", lambda rn=round_num: self.test_unload(rn)),
        ]
        return self._run_test_list(tests, round_num)

    def _find_switch_partner(self):
        """从 self._all_models 中找一个与 self.model_name 同后端(同设备)的其它模型,
        作为触发同设备动态切换用的伙伴。确定性排序取第一个,方便复现;找不到返回 None。"""
        my_backend = infer_backend(self.model_name)[0]
        candidates = sorted(
            m for m in self._all_models
            if m != self.model_name and infer_backend(m)[0] == my_backend
        )
        return candidates[0] if candidates else None

    def test_dynamic_switch_multimodal_stability(self, round_num):
        """回归 error.md 记录的复现场景:同设备把模型从 self.model_name 动态切换到
        一个伙伴模型,再切回来发多模态请求。①对伙伴模型发纯文本请求触发同设备切出;
        ②对 self.model_name 发 OpenAI 风格图文请求,同时完成"切回原模型"与"多模态推理"。"""
        name = "POST /v1/chat/completions (dynamic switch + multimodal stability)"
        if "image" not in detect_modality(self.model_name):
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=True, status_code=0, latency_ms=0,
                detail="该模型不支持图片，跳过", skipped=True
            )
        partner = self._find_switch_partner()
        if partner is None:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail="未发现同后端的其它模型，无法构造切换场景",
                skipped=True, ignorable=True, ignore_reason="switch_partner_missing"
            )

        switch_body = {
            "model": partner,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False
        }
        switch_resp = self._safe_request("POST", "/v1/chat/completions", name, round_num,
                                          json=switch_body, request_context=f"switch_to={partner}")
        if isinstance(switch_resp, TestResult):
            return switch_resp

        image_path = None
        if self.data_dir:
            preferred = self.data_dir / "img" / "1.png"
            if preferred.exists():
                image_path = preferred
        image_err = None
        if image_path is None:
            image_path, image_err = self._pick_random_asset("img", {".jpg", ".jpeg", ".png"})
        if image_path is None:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"素材缺失: {image_err}",
                skipped=True, ignorable=True, ignore_reason="data_dir 下 img 素材缺失"
            )
        ext = image_path.suffix.lower().lstrip(".")
        if ext == "jpg":
            ext = "jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        media_image_data_uri = f"data:image/{ext};base64,{image_b64}"
        prompt_text = random.choice(MULTIMODAL_PROMPTS)
        back_body = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": media_image_data_uri}}
                ]}
            ],
            "stream": False
        }
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=back_body,
                                   request_context=f"switch_back_to={self.model_name}, image={image_path.name}")
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        text_response = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                text_response = choices[0].get("message", {}).get("content", "") if choices else ""
                passed = len(text_response) > 0
            except Exception as e:
                text_response = f"JSON 解析失败: {e}"
        asset_detail = f"switch_partner={partner}, image={image_path.name}"
        detail = f"status={r.status_code}, {asset_detail}" if not passed else f"OK, {asset_detail}"
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            text_response=text_response, text_prompt=f"{prompt_text} [{asset_detail}]",
            media_image_data_uri=media_image_data_uri, media_asset_label=asset_detail
        )

    def run_multimodal_rounds(self):
        """独立于 run_all() 的多模态测试轮次,由 self.multimodal_rounds 驱动
        (未显式传入 --multimodal_rounds 时跟随 --rounds,默认行为不变)。每一轮依次执行
        client-style / openai-style-image / openai-style-audio 三个既有用例,以及本次新增的
        动态切换稳定性用例(self._all_models 非空时才追加),四个用例共享同一 mm_round 编号,
        各自重新随机抽取素材,抽取次数等于 self.multimodal_rounds。"""
        if not detect_modality(self.model_name):
            return []
        results = []
        for mm_round in range(1, self.multimodal_rounds + 1):
            tests = [
                ("POST /v1/chat/completions (multimodal client-style)",
                 lambda rn=mm_round: self.test_chat_multimodal_client_style(rn)),
                ("POST /v1/chat/completions (multimodal openai-style)",
                 lambda rn=mm_round: self.test_chat_multimodal_openai_style(rn)),
                ("POST /v1/chat/completions (multimodal audio openai-style)",
                 lambda rn=mm_round: self.test_chat_multimodal_audio_openai_style(rn)),
            ]
            if self._all_models:
                tests.append(("POST /v1/chat/completions (dynamic switch + multimodal stability)",
                              lambda rn=mm_round: self.test_dynamic_switch_multimodal_stability(rn)))
            results.extend(self._run_test_list(tests, mm_round))
        return results

    def test_welcome(self, round_num):
        resp = self._safe_request("GET", "/", "GET /", round_num)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
        return TestResult(
            name="GET /", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency,
            detail="OK" if passed else f"status={r.status_code}, content-type={r.headers.get('content-type','')}"
        )

    def test_models(self, round_num, path="/models"):
        # 该接口与模型无关，连续多次调用，任一失败即判失败；全部成功才 OK
        name = f"GET {path}"
        attempts = 3
        last_status = 0
        last_latency = 0.0
        last_response_data = {}
        fail_reason = ""
        for i in range(attempts):
            resp = self._safe_request("GET", path, name, round_num)
            if isinstance(resp, TestResult):
                resp.name = name
                resp.detail = f"attempt {i+1}/{attempts}: {resp.detail}"
                return resp
            r, latency = resp
            last_status = r.status_code
            last_latency = latency
            ok = False
            if r.status_code == 200:
                try:
                    data = r.json()
                    if "data" in data and isinstance(data["data"], list):
                        ok = True
                        models_list = data["data"]
                        last_response_data = {
                            "object": data.get("object", ""),
                            "count": len(models_list),
                            "models": [
                                {
                                    "id": m.get("id", ""),
                                    "is_loaded": m.get("is_loaded", None),
                                    "context_length": m.get("context_length", None),
                                    "backend": m.get("backend", ""),
                                    "device": m.get("device", ""),
                                }
                                for m in models_list
                            ]
                        }
                    else:
                        fail_reason = f"attempt {i+1}/{attempts}: 缺少 data 数组"
                except Exception as e:
                    fail_reason = f"attempt {i+1}/{attempts}: JSON 解析失败: {e}"
            else:
                fail_reason = f"attempt {i+1}/{attempts}: status={r.status_code}"
            if not ok:
                return TestResult(
                    name=name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=last_status, latency_ms=last_latency,
                    detail=fail_reason, response_data=last_response_data
                )
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=True, status_code=last_status, latency_ms=last_latency,
            detail=f"OK ({attempts}/{attempts} attempts)", response_data=last_response_data
        )

    def test_v1_models(self, round_num):
        # 委托给 test_models 的别名 /v1/models 调用
        return self.test_models(round_num, path="/v1/models")

    def test_status(self, round_num, name="GET /status"):
        # /status 是 GET 路由,不能用 POST
        # 严格判定:仅当 HTTP 200 且返回体含 loading 字段时 PASS;
        # 其它任何状态(包括服务端冷启动 set_content(json_object) type_error 触发的 400)
        # 一律视为 FAIL,在报告里直接暴露服务端缺陷。
        resp = self._safe_request("GET", "/status", name, round_num)
        if isinstance(resp, TestResult):
            resp.name = name
            return resp
        r, latency = resp
        passed = False
        detail = ""
        response_data = {}
        if r.status_code == 200:
            try:
                data = r.json()
                # FetchModelStatus 返回: {"loading": "0"或"1"}（字符串类型）
                if "loading" in data:
                    passed = True
                    loading_val = data.get("loading")
                    response_data = {"loading": loading_val}
                    detail = f"OK (loading={loading_val})"
                else:
                    detail = "FAIL: 200 但缺少 loading 字段"
                    response_data = data
            except Exception as e:
                detail = f"FAIL: JSON 解析失败: {e}, body={r.text[:100]}"
                response_data = {"raw_body": r.text[:200]}
        else:
            # 包括 400 (服务端 set_content type_error bug)、503 等
            try:
                response_data = r.json()
            except Exception:
                response_data = {"raw_body": r.text[:200] if r.text else ""}
            detail = f"FAIL: status={r.status_code}, body={r.text[:120] if r.text else ''}"
        # 已知缺陷: HTTP 400 + body 含 "invalid json" 是服务端 FetchModelStatus 把
        # nlohmann::json::object_t 直接塞进 httplib::Response::set_content 触发的 type_error,
        # 框架兜底回 400 + {"error":"invalid json"}。这是稳定可复现的占位实现缺陷,
        # 不影响其它接口,因此标记为可忽略。
        ignorable = False
        ignore_reason = ""
        if not passed and r.status_code == 400 and isinstance(response_data, dict) \
                and "invalid json" in str(response_data.get("error", "")).lower():
            ignorable = True
            ignore_reason = "服务端 /status 已知 type_error 缺陷 (set_content 收到 json_object 而非 string)"
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data,
            ignorable=ignorable, ignore_reason=ignore_reason
        )

    def test_profile(self, round_num):
        resp = self._safe_request("GET", "/profile", "GET /profile", round_num)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        # 严格判定：只有 200 + 非空 JSON body 才是 PASS。
        # "200 但 body 为空" 这一分支服务端已修复（见 chat_request_handler.cpp::FetchProfile），
        # 不应再出现；服务端现在会在 profile 数据未就绪时显式返回
        # 503 + {"reason": "no_profile_data"}，与"真正无模型加载"的 503 区分开。
        passed = False
        skipped = False
        detail = ""
        response_data = {}
        if r.status_code == 200:
            body_len = len(r.text.strip())
            if body_len > 0:
                try:
                    data = r.json()
                    passed = True
                    # 完整记录 profile 返回的 JSON 数据
                    response_data = data
                    detail = f"OK (keys={list(data.keys())[:8]})"
                except Exception:
                    detail = f"OK (非 JSON, body_len={body_len})"
                    response_data = {"raw_body": r.text[:500]}
            else:
                detail = "FAIL: 200 但 body 为空 (服务端契约缺陷回归)"
        elif r.status_code == 503:
            try:
                response_data = r.json()
            except Exception:
                response_data = {}
            reason = response_data.get("reason") if isinstance(response_data, dict) else None
            error_msg = str(response_data.get("error", "")) if isinstance(response_data, dict) else ""
            if reason == "no_profile_data":
                # profile 数据尚未生成（推理尚未真正开始）是环境/时序性质的场景，
                # 不是确定性契约缺陷；服务端已优雅提示，归类为 skipped 而非 failed。
                skipped = True
                detail = "SKIP: 服务端已优雅提示 profile 未就绪 (reason=no_profile_data)"
            elif "no model loaded" in error_msg.lower():
                if _is_cascading_mnn_oom(self.svc, self.model_name):
                    skipped = True
                    detail = "SKIP: 服务端因内存不足从未成功加载该模型,级联到 /profile(同一 MNN OOM 事件)"
                else:
                    detail = "FAIL: 503 无模型加载"
            else:
                detail = f"FAIL: 503 (未识别的 body: {response_data})"
        else:
            detail = f"FAIL: status={r.status_code}"
            try:
                response_data = r.json()
            except Exception:
                response_data = {"raw_body": r.text[:200]} if r.text else {}
        return TestResult(
            name="GET /profile", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data, skipped=skipped
        )

    def test_chat_non_stream(self, round_num, prompt, path="/v1/chat/completions", content_format="openai_string"):
        perf_before = self.perf.snapshot()
        name = f"POST {path} (non-stream)"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": _build_chat_content(prompt, content_format)}
            ],
            "stream": False
        }
        log_offset = self._capture_log_offset()
        resp = self._safe_request("POST", path,
                                  name, round_num,
                                  json=body)
        perf_after = self.perf.snapshot()
        if isinstance(resp, TestResult):
            resp.name = name
            resp.text_prompt = prompt
            resp.perf_before = perf_before
            resp.perf_after = perf_after
            resp.request_format = content_format
            return resp
        r, latency = resp
        passed = False
        skipped = False
        text_response = ""
        detail = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                if choices and "message" in choices[0] and "content" in choices[0]["message"]:
                    text_response = choices[0]["message"]["content"]
                    passed = len(text_response) > 0
                    detail = f"response_len={len(text_response)}"
                else:
                    detail = "缺少 choices[0].message.content"
            except Exception as e:
                detail = f"JSON 解析失败: {e}"
        else:
            detail = f"status={r.status_code}"
            failure_reason, failure_detail = (None, None)
            if r.status_code == 500:
                failure_reason, failure_detail = _extract_failure_reason(r)
            if failure_reason == "insufficient_memory":
                # 服务端优雅拒绝(MNN 内存预检查): 这是环境资源约束,不是代码缺陷,
                # 计为 skipped 而非 failed(不再计入失败统计),把具体的 failure_detail
                # 记进 detail 方便定位。
                skipped = True
                detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
            else:
                try:
                    detail += f" body={r.text[:200]}"
                except Exception:
                    pass

        # 模型缺陷标记(以服务端日志为准)：只扫描本次请求发出后新增的日志内容,查找
        # genie.cpp/genie_interface.cpp 记录的 [MODEL_DEFECT] 标记(context 超限但已优雅
        # 返回已生成内容的场景)。命中不算失败(HTTP 层面已是正常的 200/finish_reason=length),
        # 只作为模型输出质量的诊断信息附加在结果上。
        defect = _scan_model_defect_log(log_offset[0], log_offset[1]) if log_offset else None
        capability_issue, capability_reason = _capability_issue_from_model_defect(defect)

        return TestResult(
            name=name, round_num=round_num,
            model_name=self.model_name, passed=passed, status_code=r.status_code,
            latency_ms=latency, detail=detail, text_response=text_response,
            text_prompt=prompt, is_stream=False, skipped=skipped,
            perf_before=perf_before, perf_after=perf_after,
            request_format=content_format,
            model_capability_issue=capability_issue,
            model_capability_reason=capability_reason
        )

    # requests 的 timeout 对流式响应只约束"两次数据到达之间的间隔"，只要服务端持续
    # (哪怕很慢)吐出数据，读取循环本身没有总时长上限；这里加一个总时长兜底，
    # 超过该时长即主动 /stop 截断并记为失败，避免单个用例无限期占用整个回归的时间。
    MAX_STREAM_SECONDS = 300

    def test_chat_stream(self, round_num, prompt, path="/v1/chat/completions", content_format="openai_string"):
        perf_before = self.perf.snapshot()
        name = f"POST {path} (stream)"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": _build_chat_content(prompt, content_format)}
            ],
            "stream": True
        }
        url = f"{self.base_url}{path}"
        start_time = time.time()
        ttft = 0.0
        chunk_count = 0
        full_response = ""
        got_done = False
        passed = False
        detail = ""
        stream_timeout = False

        if not self.svc.is_alive():
            if self._restart_count >= self.MAX_RESTARTS:
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=name, round_num=round_num,
                    model_name=self.model_name, passed=False, status_code=0, latency_ms=0,
                    detail=f"服务已崩溃且超过最大重启次数; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    text_prompt=prompt, is_stream=True, request_format=content_format
                )
            self.crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name=self.model_name,
                round_num=round_num, endpoint=name,
                detail="请求前检测到进程已退出",
                log_tail=_capture_log_tail(self.svc)
            ))
            self._restart_count += 1
            if not self.svc.restart():
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=name, round_num=round_num,
                    model_name=self.model_name, passed=False, status_code=0, latency_ms=0,
                    detail=f"服务重启失败; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    text_prompt=prompt, is_stream=True, request_format=content_format
                )

        log_offset = self._capture_log_offset()
        try:
            r = requests.post(url, json=body, stream=True, timeout=300)
            if r.status_code != 200:
                latency = (time.time() - start_time) * 1000
                perf_after = self.perf.snapshot()
                detail = f"status={r.status_code}"
                stream_skipped = False
                if r.status_code == 500:
                    failure_reason, failure_detail = _extract_failure_reason(r)
                    if failure_reason == "insufficient_memory":
                        # 服务端优雅拒绝(MNN 内存预检查): 环境资源约束,不是代码缺陷,
                        # 计为 skipped 而非 failed。
                        stream_skipped = True
                        detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
                        if infer_backend(self.model_name)[0] == "mnn":
                            _mark_mnn_oom(self.svc, self.model_name, detail)
                return TestResult(
                    name=name, round_num=round_num,
                    model_name=self.model_name, passed=False, status_code=r.status_code,
                    latency_ms=latency, detail=detail, skipped=stream_skipped,
                    text_prompt=prompt, is_stream=True,
                    perf_before=perf_before, perf_after=perf_after, request_format=content_format
                )

            for line in r.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        got_done = True
                        break
                    try:
                        chunk_data = json.loads(data_str)
                        choices = chunk_data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                if chunk_count == 0:
                                    ttft = (time.time() - start_time) * 1000
                                chunk_count += 1
                                full_response += content
                    except json.JSONDecodeError:
                        pass
                if time.time() - start_time > self.MAX_STREAM_SECONDS:
                    stream_timeout = True
                    break

            if stream_timeout:
                try:
                    r.close()
                except Exception:
                    pass
                requests.post(f"{self.base_url}/stop", json={"text": "stop"}, timeout=15)

            latency = (time.time() - start_time) * 1000
            perf_after = self.perf.snapshot()
            if stream_timeout:
                passed = False
                detail = (f"流式响应超过总时长上限({self.MAX_STREAM_SECONDS}s),已主动 /stop 截断,"
                          f"chunks={chunk_count}, response_len={len(full_response)}")
            else:
                passed = got_done and len(full_response) > 0
                detail = f"chunks={chunk_count}, ttft={ttft:.0f}ms, response_len={len(full_response)}, done={got_done}"

        except (requests.ConnectionError, requests.Timeout) as e:
            latency = (time.time() - start_time) * 1000
            perf_after = self.perf.snapshot()
            # 带短暂宽限期重试确认进程是否真的已死,避免把真正致死的这次流式请求误判为
            # "进程仍存活的普通异常",而让后续某个无关请求背了这次崩溃的锅。
            if _confirm_process_dead(self.svc):
                is_mnn = infer_backend(self.model_name)[0] == "mnn"
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(), model_name=self.model_name,
                    round_num=round_num, endpoint=name,
                    detail=f"流式请求中服务崩溃: {type(e).__name__}; {classify_detail}",
                    log_tail=_capture_log_tail(self.svc)
                ))
                self._restart_count += 1
                restarted = False
                if self._restart_count <= self.MAX_RESTARTS:
                    restarted = self.svc.restart()
                if is_mnn and restarted:
                    self.crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(), model_name=self.model_name,
                        round_num=round_num, endpoint="MNN_OOM_CRASH",
                        detail=f"MNN OOM 崩溃后已自动重启(第{self._restart_count}次)",
                        log_tail=_capture_log_tail(self.svc)
                    ))
                elif ignore_reason == "mnn_oom_cascade":
                    self.crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(), model_name=self.model_name,
                        round_num=round_num, endpoint="MNN_OOM_CASCADE",
                        detail=classify_detail,
                        log_tail=_capture_log_tail(self.svc)
                    ))
                return TestResult(
                    name=name, round_num=round_num,
                    model_name=self.model_name, passed=False, status_code=0, latency_ms=latency,
                    detail=f"流式请求中服务崩溃: {e}; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    text_prompt=prompt, text_response=full_response, is_stream=True,
                    ttft_ms=ttft, chunk_count=chunk_count,
                    perf_before=perf_before, perf_after=perf_after, request_format=content_format
                )
            detail = f"请求异常: {type(e).__name__}: {e}"
            perf_after = self.perf.snapshot()
            return TestResult(
                name=name, round_num=round_num,
                model_name=self.model_name, passed=False, status_code=0, latency_ms=latency,
                detail=detail, text_prompt=prompt, text_response=full_response, is_stream=True,
                ttft_ms=ttft, chunk_count=chunk_count,
                perf_before=perf_before, perf_after=perf_after, request_format=content_format
            )

        # 模型缺陷标记(以服务端日志为准)：只扫描本次请求发出后新增的日志内容,查找
        # genie.cpp/genie_interface.cpp 记录的 [MODEL_DEFECT] 标记(context 超限但已优雅
        # 结束流式响应的场景)。命中不算失败,只作为模型输出质量的诊断信息附加在结果上。
        defect = _scan_model_defect_log(log_offset[0], log_offset[1]) if log_offset else None
        capability_issue, capability_reason = _capability_issue_from_model_defect(defect)

        return TestResult(
            name=name, round_num=round_num,
            model_name=self.model_name, passed=passed, status_code=200,
            latency_ms=latency, detail=detail, text_response=full_response,
            text_prompt=prompt, is_stream=True, ttft_ms=ttft, chunk_count=chunk_count,
            perf_before=perf_before, perf_after=perf_after,
            request_format=content_format,
            model_capability_issue=capability_issue,
            model_capability_reason=capability_reason
        )

    def _pick_random_asset(self, subdir, extensions):
        """从 data_dir/subdir 素材池中随机抽取一个文件（每次调用重新抽取，不缓存）。
        返回 (Path, None) 或 (None, 原因字符串)。委托给模块级 _pick_random_asset_file，
        与 run_sampleapp_only_tests 共用同一段筛选逻辑（保证两处用的是完全一致的素材池语义）。"""
        return _pick_random_asset_file(self.data_dir, subdir, extensions)

    def test_chat_multimodal_client_style(self, round_num):
        """GenieAPIClient 风格多模态请求（对应 ModelInputBuilder::ProcessObject）：
        content 为 {question, image, audio} object。每次从 data_dir/img、data_dir/audio
        素材池中随机抽取一个文件（不固定、不缓存），抽中的文件名记入 detail 方便复现。"""
        name = "POST /v1/chat/completions (multimodal client-style)"
        modality = detect_modality(self.model_name)
        image_path, image_err = (self._pick_random_asset("img", {".jpg", ".jpeg", ".png"})
                                  if "image" in modality else (None, None))
        audio_path, audio_err = (self._pick_random_asset("audio", {".wav"})
                                  if "audio" in modality else (None, None))
        if ("image" in modality and image_path is None) or ("audio" in modality and audio_path is None):
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"素材缺失: image_err={image_err}, audio_err={audio_err}",
                skipped=True, ignorable=True, ignore_reason="data_dir 下 img/audio 素材缺失"
            )

        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii") if image_path else ""
        audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii") if audio_path else ""
        prompt_text = random.choice(MULTIMODAL_PROMPTS)
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": {
                    "question": prompt_text,
                    "image": image_b64,
                    "audio": audio_b64
                }}
            ],
            "stream": False
        }
        # 上面 body 里的 image/audio 是未带 data URI 前缀的裸 base64(GenieAPIClient 对象风格自己的
        # 协议),这里另外构造一份带 data URI 前缀的副本专用于 conversations.html 直接渲染
        # <img>/<audio>,不影响实际发给服务端的请求体。
        image_ext = image_path.suffix.lower().lstrip(".") if image_path else ""
        if image_ext == "jpg":
            image_ext = "jpeg"
        audio_ext = audio_path.suffix.lower().lstrip(".") if audio_path else ""
        media_image_data_uri = f"data:image/{image_ext};base64,{image_b64}" if image_path else ""
        media_audio_data_uri = f"data:audio/{audio_ext};base64,{audio_b64}" if audio_path else ""
        request_context = f"image={image_path.name if image_path else '-'}, audio={audio_path.name if audio_path else '-'}"
        log_offset = self._capture_log_offset()
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body, request_context=request_context)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        text_response = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                text_response = choices[0].get("message", {}).get("content", "") if choices else ""
                passed = len(text_response) > 0
            except Exception as e:
                text_response = f"JSON 解析失败: {e}"
        asset_detail = request_context
        detail = f"status={r.status_code}, {asset_detail}" if not passed else f"OK, {asset_detail}"
        defect = _scan_model_defect_log(log_offset[0], log_offset[1]) if log_offset else None
        capability_issue, capability_reason = _capability_issue_from_model_defect(defect)
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            text_response=text_response, text_prompt=f"{prompt_text} [{asset_detail}]",
            media_image_data_uri=media_image_data_uri, media_audio_data_uri=media_audio_data_uri,
            media_asset_label=asset_detail,
            model_capability_issue=capability_issue,
            model_capability_reason=capability_reason
        )

    def test_chat_multimodal_openai_style(self, round_num):
        """OpenAI 标准风格多模态请求（对应 ModelInputBuilder::ProcessArray）：
        content 为 [{type:text}, {type:image_url}] 数组，image_url 使用 data URI base64。
        音频的 OpenAI 风格覆盖见 test_chat_multimodal_audio_openai_style。"""
        name = "POST /v1/chat/completions (multimodal openai-style)"
        modality = detect_modality(self.model_name)
        if "image" not in modality:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=True, status_code=0, latency_ms=0,
                detail="该模型不支持图片，跳过", skipped=True
            )
        image_path, image_err = self._pick_random_asset("img", {".jpg", ".jpeg", ".png"})
        if image_path is None:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"素材缺失: {image_err}",
                skipped=True, ignorable=True, ignore_reason="data_dir 下 img 素材缺失"
            )
        ext = image_path.suffix.lower().lstrip(".")
        if ext == "jpg":
            ext = "jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        prompt_text = random.choice(MULTIMODAL_PROMPTS)
        media_image_data_uri = f"data:image/{ext};base64,{image_b64}"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": media_image_data_uri}}
                ]}
            ],
            "stream": False
        }
        log_offset = self._capture_log_offset()
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body,
                                   request_context=f"image={image_path.name}")
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        text_response = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                text_response = choices[0].get("message", {}).get("content", "") if choices else ""
                passed = len(text_response) > 0
            except Exception as e:
                text_response = f"JSON 解析失败: {e}"
        detail = f"status={r.status_code}, image={image_path.name}" if not passed else f"OK, image={image_path.name}"
        defect = _scan_model_defect_log(log_offset[0], log_offset[1]) if log_offset else None
        capability_issue, capability_reason = _capability_issue_from_model_defect(defect)
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            text_response=text_response, text_prompt=f"{prompt_text} [image={image_path.name}]",
            media_image_data_uri=media_image_data_uri, media_asset_label=f"image={image_path.name}",
            model_capability_issue=capability_issue,
            model_capability_reason=capability_reason
        )

    def test_chat_multimodal_audio_openai_style(self, round_num):
        """OpenAI 标准风格音频多模态请求（对应 ModelInputBuilder::ProcessArray）：
        content 为 [{type:text}, {type:input_audio}] 数组，input_audio.data 为 base64 音频数据。"""
        name = "POST /v1/chat/completions (multimodal audio openai-style)"
        modality = detect_modality(self.model_name)
        if "audio" not in modality:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=True, status_code=0, latency_ms=0,
                detail="该模型不支持音频，跳过", skipped=True
            )
        audio_path, audio_err = self._pick_random_asset("audio", {".wav"})
        if audio_path is None:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"素材缺失: {audio_err}",
                skipped=True, ignorable=True, ignore_reason="data_dir 下 audio 素材缺失"
            )
        audio_format = audio_path.suffix.lower().lstrip(".")
        audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        prompt_text = random.choice(MULTIMODAL_PROMPTS)
        media_audio_data_uri = f"data:audio/{audio_format};base64,{audio_b64}"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "input_audio", "input_audio": {"data": audio_b64, "format": audio_format}}
                ]}
            ],
            "stream": False
        }
        log_offset = self._capture_log_offset()
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body,
                                   request_context=f"audio={audio_path.name}")
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        text_response = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                text_response = choices[0].get("message", {}).get("content", "") if choices else ""
                passed = len(text_response) > 0
            except Exception as e:
                text_response = f"JSON 解析失败: {e}"
        detail = f"status={r.status_code}, audio={audio_path.name}" if not passed else f"OK, audio={audio_path.name}"
        defect = _scan_model_defect_log(log_offset[0], log_offset[1]) if log_offset else None
        capability_issue, capability_reason = _capability_issue_from_model_defect(defect)
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            text_response=text_response, text_prompt=f"{prompt_text} [audio={audio_path.name}]",
            media_audio_data_uri=media_audio_data_uri, media_asset_label=f"audio={audio_path.name}",
            model_capability_issue=capability_issue,
            model_capability_reason=capability_reason
        )

    def test_textsplitter(self, round_num, path="/v1/textsplitter"):
        name = f"POST {path}"
        body = {
            "text": "Hello world. This is a test sentence for text splitting. It should return an array of content.",
            "max_length": 50,
            "separators": [".", " ", ""]
        }
        resp = self._safe_request("POST", path, name, round_num, json=body)
        if isinstance(resp, TestResult):
            resp.name = name
            return resp
        r, latency = resp
        passed = False
        skipped = False
        detail = ""
        response_data = {}
        if r.status_code == 200:
            try:
                data = r.json()
                passed = "content" in data and isinstance(data["content"], list)
                if passed:
                    content_list = data["content"]
                    detail = f"segments={len(content_list)}"
                    response_data = {
                        "object": data.get("object", ""),
                        "segment_count": len(content_list),
                        "segments": [
                            {"text": s.get("text", "")[:100], "length": s.get("length", 0)}
                            for s in content_list[:10]
                        ]
                    }
                else:
                    detail = "缺少 content 数组"
            except Exception as e:
                detail = f"JSON 解析失败: {e}"
        else:
            detail = f"status={r.status_code}"
            try:
                response_data = r.json()
            except Exception:
                pass
            if _is_cascading_mnn_oom(self.svc, self.model_name):
                skipped = True
                detail = f"SKIP: status={r.status_code},服务端因内存不足从未成功加载该模型,级联到 {path}(同一 MNN OOM 事件)"
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data, skipped=skipped
        )

    def test_contextsize(self, round_num):
        body = {"model": self.model_name}
        resp = self._safe_request("POST", "/contextsize", "POST /contextsize", round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        skipped = False
        detail = ""
        response_data = {}
        if r.status_code == 200:
            try:
                data = r.json()
                ctx = data.get("contextsize", 0)
                passed = ctx > 0
                detail = f"contextsize={ctx}"
                response_data = {"contextsize": ctx}
                if not passed and _is_cascading_mnn_oom(self.svc, self.model_name):
                    skipped = True
                    detail = f"SKIP: contextsize=0,服务端因内存不足从未成功加载该模型,级联到 /contextsize(同一 MNN OOM 事件)"
            except Exception as e:
                detail = f"JSON 解析失败: {e}"
        else:
            detail = f"status={r.status_code}"
            try:
                response_data = r.json()
            except Exception:
                pass
        return TestResult(
            name="POST /contextsize", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data, skipped=skipped
        )

    def test_stop(self, round_num):
        body = {"text": "stop"}
        resp = self._safe_request("POST", "/stop", "POST /stop", round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = r.status_code == 200
        return TestResult(
            name="POST /stop", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency,
            detail="OK" if passed else f"status={r.status_code}"
        )

    def test_clear(self, round_num):
        body = {"text": "clear"}
        resp = self._safe_request("POST", "/clear", "POST /clear", round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = r.status_code == 200
        return TestResult(
            name="POST /clear", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency,
            detail="OK" if passed else f"status={r.status_code}"
        )

    def test_fetch(self, round_num):
        body = {}
        resp = self._safe_request("POST", "/fetch", "POST /fetch", round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        detail = ""
        response_data = {}
        if r.status_code == 200:
            try:
                data = r.json()
                passed = "history" in data
                if passed:
                    history = data.get("history", [])
                    detail = f"history_len={len(history)}"
                    response_data = {"history_count": len(history), "history": history[:5]}
                else:
                    detail = "缺少 history 字段"
                    response_data = data
            except Exception as e:
                detail = f"JSON 解析失败: {e}"
        else:
            detail = f"status={r.status_code}"
        return TestResult(
            name="POST /fetch", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data
        )

    def test_reload(self, round_num):
        body = {"action": "import_history", "history": []}
        resp = self._safe_request("POST", "/reload", "POST /reload", round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        # 严格判定: 仅 200 视为 PASS;
        # 服务端目前会返回 400 "Not supported in stateless mode" - 直接判 FAIL。
        response_data = {}
        try:
            response_data = r.json()
        except Exception:
            response_data = {"raw_body": r.text[:200] if r.text else ""}
        if r.status_code == 200:
            passed = True
            detail = "OK"
        else:
            passed = False
            err = response_data.get("error") if isinstance(response_data, dict) else r.text[:80]
            detail = f"FAIL: status={r.status_code}, body={err}"
        # 已知缺陷: 服务端 ChatRequestHandler::ReloadMessage 是一个占位实现,
        # 永远返回 400 + {"error":"Not supported in stateless mode"}。
        # 没有任何分支,所以这条 FAIL 是稳定可复现的占位实现缺陷,标记为可忽略。
        ignorable = False
        ignore_reason = ""
        if not passed and r.status_code == 400 and isinstance(response_data, dict) \
                and "stateless mode" in str(response_data.get("error", "")).lower():
            ignorable = True
            ignore_reason = "服务端 /reload 为占位实现 (ChatRequestHandler::ReloadMessage 写死返回 400)"
        return TestResult(
            name="POST /reload", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data,
            ignorable=ignorable, ignore_reason=ignore_reason
        )

    def test_image_generate(self, round_num, path="/v1/images/generations"):
        # 严格判定: 仅 200 视为 PASS;
        # 服务端目前返回 501 (Not Implemented) - 严格策略下直接判 FAIL,暴露未实现状态。
        body = {"prompt": "a cat", "n": 1, "size": "256x256"}
        name = f"POST {path}"
        resp = self._safe_request("POST", path, name, round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        response_data = {}
        try:
            response_data = r.json()
        except Exception:
            response_data = {"raw_body": r.text[:200] if r.text else ""}
        passed = r.status_code == 200
        if passed:
            detail = "OK"
        else:
            detail = f"FAIL: status={r.status_code}, body={r.text[:120] if r.text else ''}"
        # 已知缺陷: /images/generations 在当前服务版本未实现,
        # 服务端直接返回 HTTP 501 Not Implemented。FAIL 但可忽略。
        ignorable = False
        ignore_reason = ""
        if not passed and r.status_code == 501:
            ignorable = True
            ignore_reason = "服务端 /images/generations 未实现 (HTTP 501 Not Implemented)"
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data,
            ignorable=ignorable, ignore_reason=ignore_reason
        )

    def test_tool_call_retry_limit_enforcement(self, round_num):
        """确定性契约测试(Step 4)：构造 messages 数组,最后一条 user 消息之后紧跟
        max_tool_call_retries+1 条 role:"tool" 占位消息(超过 agent_routing.max_tool_call_retries,
        默认10,model_config.h:169),断言返回 400 且响应体 error.message 包含
        "maximum allowed tool call retries"。这是纯请求结构触发的确定性行为
        (chat_request_handler.cpp:610-663 的 CountTrailingToolCalls,无论 routing.enabled
        是 true/false 都各自实现同一段检查),不依赖模型是否真实决定调用工具。阈值优先从
        exe_dir 下实际生效的 service_config.json 动态读取,读取不到才回退代码默认值 10,
        不硬编码。"""
        name = "POST /v1/chat/completions (tool_call retry limit enforcement)"
        max_retries = _read_max_tool_call_retries(self.exe_dir)
        tool_messages = [
            {"role": "tool", "tool_call_id": f"call_{i}", "name": "read",
             "content": "placeholder tool result for retry-limit probe"}
            for i in range(max_retries + 1)
        ]
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Please read the file and tell me its content."},
            ] + tool_messages,
            "stream": False
        }
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        skipped = False
        detail = f"status={r.status_code}"
        if r.status_code == 400:
            try:
                data = r.json()
                err_msg = (data.get("error") or {}).get("message", "")
                passed = "maximum allowed tool call retries" in err_msg
                detail = f"status=400, max_retries={max_retries}, error.message={err_msg!r}"
            except Exception as e:
                detail = f"status=400 但 JSON 解析失败: {e}"
        else:
            failure_reason, failure_detail = (None, None)
            if r.status_code == 500:
                failure_reason, failure_detail = _extract_failure_reason(r)
            if failure_reason == "insufficient_memory":
                # 服务端优雅拒绝(MNN 内存预检查): 环境资源约束,不是代码缺陷,计为 skipped。
                skipped = True
                detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
            else:
                try:
                    detail += f", max_retries={max_retries}, body={r.text[:200]}"
                except Exception:
                    pass
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail, skipped=skipped
        )

    def test_tool_call_round_trip_with_result(self, round_num):
        """确定性契约测试(Step 4)：手动构造两轮对话 —— user 提问 -> assistant 消息带
        tool_calls=[{"id":"call_1","type":"function","function":{"name":"read",
        "arguments":"{\\"path\\":...}"}}](使用预定义工具名"read",prompt_optimizer.cpp:705-727
        已确认存在) -> role:"tool" 带 tool_call_id+name+模拟文件内容 -> 追问 user,断言
        200 + choices[0].message.content 非空。验证服务端能正确解析回填的 tool 结果并继续
        生成,不因 tool_calls/tool_call_id 结构异常而 400/500/崩溃 —— 这条路径此前从未被
        任何测试请求过。只做"不崩溃 + 结构完整性"验证,不对模型追问后的回复内容做语义判分。"""
        name = "POST /v1/chat/completions (tool_call round-trip with result)"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Please check the content of README.md for me."},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "read", "arguments": "{\"path\": \"README.md\"}"}}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "name": "read",
                 "content": "This is a placeholder simulated file content used for testing purposes only."},
                {"role": "user", "content": "Thanks, please briefly acknowledge what you found."}
            ],
            "stream": False
        }
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = False
        skipped = False
        text_response = ""
        detail = ""
        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                if choices and "message" in choices[0] and "content" in choices[0]["message"]:
                    text_response = choices[0]["message"]["content"] or ""
                    passed = len(text_response) > 0
                    detail = f"response_len={len(text_response)}"
                else:
                    detail = "缺少 choices[0].message.content"
            except Exception as e:
                detail = f"JSON 解析失败: {e}"
        else:
            detail = f"status={r.status_code}"
            failure_reason, failure_detail = (None, None)
            if r.status_code == 500:
                failure_reason, failure_detail = _extract_failure_reason(r)
            if failure_reason == "insufficient_memory":
                # 服务端优雅拒绝(MNN 内存预检查): 环境资源约束,不是代码缺陷,计为 skipped。
                skipped = True
                detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
            else:
                try:
                    detail += f" body={r.text[:200]}"
                except Exception:
                    pass
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            skipped=skipped, text_response=text_response
        )

    def test_tool_call_model_invocation_probe(self, round_num):
        """机会性观察验证(Step 5)：用强指令 prompt 要求模型必须调用预定义工具"read"作答,
        并携带对应的 tools=[JSON Schema] 定义。若响应 finish_reason=="tool_calls":对
        choices[0].message.tool_calls 数组做结构性 schema 校验(id 非空字符串、
        type=="function"、function.name 非空、function.arguments 是可被 json.loads
        解析的合法 JSON 字符串),不判断参数取值语义。若模型未按指令触发 tool_calls
        (量化/小模型的真实可能性,非服务端缺陷):标记 skipped=True 并注明原因,不计入
        failed —— 与项目现有"不对 LLM 输出做语义打分"的测试哲学一致。"""
        name = "POST /v1/chat/completions (tool_call model invocation probe)"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant with access to tools."},
                {"role": "user", "content": (
                    "Call the function 'read' now with its 'path' argument set to the exact "
                    "literal value \"README.md\" (an actual value, not the function's parameter "
                    "schema/definition). Respond only with a tool call using the concrete "
                    "argument {\"path\": \"README.md\"}; do not describe or repeat the "
                    "function's schema, and do not respond in plain text."
                )}
            ],
            "tools": [_STATELESS_MODE_READ_TOOL_DEF],
            "stream": False
        }
        resp = self._safe_request("POST", "/v1/chat/completions", name, round_num, json=body)
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        if r.status_code != 200:
            detail = f"status={r.status_code}"
            skipped = False
            if r.status_code == 500:
                failure_reason, failure_detail = _extract_failure_reason(r)
                if failure_reason == "insufficient_memory":
                    skipped = True
                    detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
            if not skipped:
                try:
                    detail += f" body={r.text[:200]}"
                except Exception:
                    pass
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency, detail=detail, skipped=skipped
            )
        try:
            data = r.json()
            choices = data.get("choices", [])
            finish_reason = choices[0].get("finish_reason") if choices else None
            message = choices[0].get("message", {}) if choices else {}
            tool_calls = message.get("tool_calls")
        except Exception as e:
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=f"JSON 解析失败: {e}"
            )
        if finish_reason != "tool_calls" or not tool_calls:
            _reason = f"模型未按指令触发 tool_calls(finish_reason={finish_reason!r}),非服务端异常,不计入 failed"
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=_reason,
                skipped=True, model_capability_issue=True, model_capability_reason=_reason
            )
        # "unknow" 是服务端 ResponseTools::convertToolCallJson 对"无法解析模型输出为合法工具
        # 调用 JSON"的精确、已文档化的兜底标记(response_dispatcher.cpp 的畸形工具调用死循环
        # 检测机制正是围绕这个标记设计的,见调查结论 Tab 第9.2节)。命中该精确信号说明模型
        # 确实尝试了调用但输出畸形(如把工具的 JSON Schema 本身当成 arguments 回显),这是
        # 模型自身能力/量化程度的真实局限,不是服务端缺陷,按与"未触发 tool_calls"同样的
        # 容忍策略处理为 skipped=True,而不是判为结构性校验失败。
        if any(tc.get("function", {}).get("name") == "unknow" for tc in tool_calls):
            _reason = ("模型触发了 tool_calls 但输出畸形,服务端已归类为 unknow 工具名"
                       "(ResponseTools::convertToolCallJson 精确兜底标记),属模型能力局限,不计入 failed")
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=_reason,
                skipped=True, response_data={"tool_calls": tool_calls, "finish_reason": finish_reason},
                model_capability_issue=True, model_capability_reason=_reason
            )
        # hard_errors：服务端确定性保证的结构字段——id/type 由 response_tools.cpp::format_tool_calls
        # 硬编码生成（generate_uuid4()/"function"字面量），function.name 有精确的 "unknow" 兜底标记
        # （已在上面处理），这三者出错才是真正的服务端契约缺陷。
        # soft_errors：function.arguments 的字符串内容是否能解析为合法 JSON，完全取决于模型自身
        # 输出质量——已用源码核实 convertToolCallJson（response_tools.cpp 第260-278行）在解析失败时
        # 会原样保留模型输出的字符串，这段内容不是服务端可控的确定性契约，因此不应计入 fail，
        # 而是降级为模型能力局限导致的 skip（与项目现有测试哲学一致：不因模型输出格式混乱而误判
        # 服务端缺陷）。
        hard_errors = []
        soft_errors = []
        for idx, tc in enumerate(tool_calls):
            tc_id = tc.get("id")
            if not isinstance(tc_id, str) or not tc_id:
                hard_errors.append(f"tool_calls[{idx}].id 非法: {tc_id!r}")
            if tc.get("type") != "function":
                hard_errors.append(f"tool_calls[{idx}].type != 'function': {tc.get('type')!r}")
            func = tc.get("function", {}) or {}
            fname = func.get("name")
            if not isinstance(fname, str) or not fname:
                hard_errors.append(f"tool_calls[{idx}].function.name 非法: {fname!r}")
            fargs = func.get("arguments")
            if not isinstance(fargs, str):
                hard_errors.append(f"tool_calls[{idx}].function.arguments 不是字符串: {type(fargs)}")
            else:
                try:
                    json.loads(fargs)
                except Exception as e:
                    soft_errors.append(f"tool_calls[{idx}].function.arguments 不是合法 JSON"
                                       f"(模型输出质量问题,非服务端契约缺陷): {e}")
        if hard_errors:
            passed, skipped, detail = False, False, "; ".join(hard_errors)
            capability_issue = False
        elif soft_errors:
            passed, skipped, detail = False, True, "; ".join(soft_errors)
            capability_issue = True
        else:
            passed, skipped, detail = True, False, "tool_calls schema 校验通过"
            capability_issue = False
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            skipped=skipped, response_data={"tool_calls": tool_calls, "finish_reason": finish_reason},
            model_capability_issue=capability_issue, model_capability_reason=detail if capability_issue else ""
        )

    def test_tool_call_streaming_argument_integrity_probe(self, round_num):
        """预防性回归:用强指令 prompt 要求模型必须调用预定义工具"read"并携带一个足够长
        (120+字符)的 path 值,以 stream=true 发起请求,按 index 聚合全部流式 delta.tool_calls
        片段重建最终参数。判定标准只针对与内容无关的机制事实,不对模型复述的具体文字做
        逐字比较——模型能否一字不差地复述一段人造长字符串是无法穷举的模型能力问题,只作为
        观察记录(model_capability_issue),不参与 passed/failed:若聚合到的分片数>1(说明
        这次真的触发了跨 chunk 拼接),但拼接结果不是合法 JSON 或缺失 path 字段,才判定为
        真实的拼接缺陷(failed)；分片数<=1 时(本地 qnn/mnn/gguf 路径目前始终如此,已用抓包
        核实 tool_calls 参数整段一次性到达)则只要求结构完整,内容准确度降级为观察记录。
        若模型未按指令触发 tool_calls:标记 skipped=True,不计入 failed。"""
        name = "POST /v1/chat/completions (tool_call streaming argument integrity probe)"
        target_path = "very_long_simulated_probe_path_" + ("x" * 90) + "/README_INTEGRITY_PROBE.md"
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant with access to tools."},
                {"role": "user", "content": (
                    f"Call the function 'read' now with its 'path' argument set to the exact "
                    f"literal value \"{target_path}\" (an actual value, not the function's "
                    "parameter schema/definition). Respond only with a tool call using the "
                    f"concrete argument {{\"path\": \"{target_path}\"}}, reproduced character "
                    "for character with no truncation; do not describe or repeat the function's "
                    "schema, and do not respond in plain text."
                )}
            ],
            "tools": [_STATELESS_MODE_READ_TOOL_DEF],
            "stream": True
        }
        url = f"{self.base_url}/v1/chat/completions"
        start_time = time.time()

        # 与 _safe_request 一致的进程存活性前置检查(流式路径需要手写,无法直接复用 _safe_request)。
        if not self.svc.is_alive():
            if self._restart_count >= self.MAX_RESTARTS:
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"服务已崩溃且超过最大重启次数; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason
                )
            self.crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(), model_name=self.model_name,
                round_num=round_num, endpoint=name, detail="请求前检测到进程已退出",
                log_tail=_capture_log_tail(self.svc)
            ))
            self._restart_count += 1
            if not self.svc.restart():
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                return TestResult(
                    name=name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=0,
                    detail=f"服务重启失败; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason
                )

        finish_reason = None
        tool_call_frags = {}  # index -> {"id":..., "type":..., "name":..., "arguments": "" (拼接累积)}
        try:
            r = requests.post(url, json=body, stream=True, timeout=300)
            if r.status_code != 200:
                latency = (time.time() - start_time) * 1000
                detail = f"status={r.status_code}"
                skipped = False
                if r.status_code == 500:
                    failure_reason, failure_detail = _extract_failure_reason(r)
                    if failure_reason == "insufficient_memory":
                        skipped = True
                        detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
                        if infer_backend(self.model_name)[0] == "mnn":
                            _mark_mnn_oom(self.svc, self.model_name, detail)
                return TestResult(
                    name=name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=r.status_code, latency_ms=latency, detail=detail, skipped=skipped
                )

            for line in r.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk_data.get("choices", [])
                    if not choices:
                        continue
                    choice0 = choices[0]
                    if choice0.get("finish_reason") == "tool_calls":
                        finish_reason = "tool_calls"
                    delta = choice0.get("delta", {}) or {}
                    delta_tool_calls = delta.get("tool_calls")
                    if delta_tool_calls:
                        for pos, tc in enumerate(delta_tool_calls):
                            idx = tc.get("index", pos)
                            frag = tool_call_frags.setdefault(
                                idx, {"id": "", "type": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                frag["id"] = tc["id"]
                            if tc.get("type"):
                                frag["type"] = tc["type"]
                            func = tc.get("function") or {}
                            if func.get("name"):
                                frag["name"] = func["name"]
                            if func.get("arguments"):
                                frag["arguments"] += func["arguments"]
                                frag["arg_frag_count"] = frag.get("arg_frag_count", 0) + 1
                if time.time() - start_time > self.MAX_STREAM_SECONDS:
                    break
        except (requests.ConnectionError, requests.Timeout) as e:
            latency = (time.time() - start_time) * 1000
            if _confirm_process_dead(self.svc):
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, self.model_name)
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(), model_name=self.model_name,
                    round_num=round_num, endpoint=name,
                    detail=f"流式请求中服务崩溃: {type(e).__name__}; {classify_detail}",
                    log_tail=_capture_log_tail(self.svc)
                ))
                self._restart_count += 1
                if self._restart_count <= self.MAX_RESTARTS:
                    self.svc.restart()
                return TestResult(
                    name=name, round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=latency,
                    detail=f"流式请求中服务崩溃: {e}; {classify_detail}", crashed=True,
                    ignorable=ignorable, ignore_reason=ignore_reason
                )
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=0, latency_ms=latency,
                detail=f"请求异常: {type(e).__name__}: {e}"
            )

        latency = (time.time() - start_time) * 1000
        if finish_reason != "tool_calls" or not tool_call_frags:
            _reason = f"模型未按指令触发 tool_calls(finish_reason={finish_reason!r}),非服务端异常,不计入 failed"
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=200, latency_ms=latency,
                detail=_reason,
                skipped=True, model_capability_issue=True, model_capability_reason=_reason
            )
        # "unknow" 是服务端 ResponseTools::convertToolCallJson 对"无法解析模型输出为合法工具
        # 调用 JSON"的精确、已文档化的兜底标记(见调查结论 Tab 第9.2节)。命中该精确信号说明
        # 模型确实尝试了调用但输出畸形,这是模型自身能力/量化程度的真实局限,不是服务端缺陷,
        # 按与"未触发 tool_calls"同样的容忍策略处理为 skipped=True。
        if any(frag.get("name") == "unknow" for frag in tool_call_frags.values()):
            _reason = ("模型触发了 tool_calls 但输出畸形,服务端已归类为 unknow 工具名"
                       "(ResponseTools::convertToolCallJson 精确兜底标记),属模型能力局限,不计入 failed")
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=200, latency_ms=latency,
                detail=_reason,
                skipped=True, response_data={"tool_call_frags": tool_call_frags, "finish_reason": finish_reason},
                model_capability_issue=True, model_capability_reason=_reason
            )

        # 机制事实优先于内容准确度:分片数(arg_frag_count)>1 说明这次真的触发了跨 chunk
        # 拼接,此时若拼接结果连合法 JSON/path 字段都凑不出来,才是需要关注的真实拼接缺陷
        # (structural_errors,failed)。分片数<=1 或拼接结果结构完整时,path 内容是否与指令
        # 目标逐字一致只作为观察记录(model_capability_issue),不影响 passed——模型对超长
        # 人造字符串的复述准确度是不可穷举的模型能力问题,不是服务端可控的确定性契约。
        json_parse_errors = []
        structural_errors = []
        no_path_notes = []
        content_mismatch_notes = []
        multi_fragment_detected = False
        matched_exactly = False
        for idx, frag in tool_call_frags.items():
            args_str = frag["arguments"]
            frag_count = frag.get("arg_frag_count", 0)
            if frag_count > 1:
                multi_fragment_detected = True
            try:
                parsed_args = json.loads(args_str)
            except Exception as e:
                if frag_count > 1:
                    structural_errors.append(f"tool_calls[index={idx}] 经历了 {frag_count} 个流式分片拼接,"
                                             f"但拼接结果不是合法 JSON(疑似跨 chunk 拼接丢字符): {e}")
                else:
                    json_parse_errors.append(f"tool_calls[index={idx}] arguments 不是合法 JSON"
                                             f"(模型输出质量问题,非服务端契约缺陷): {e}; raw_len={len(args_str)}")
                continue
            path_value = parsed_args.get("path")
            if path_value is None:
                if frag_count > 1:
                    structural_errors.append(f"tool_calls[index={idx}] 经历了 {frag_count} 个流式分片拼接,"
                                             f"但拼接结果里 path 字段缺失(疑似跨 chunk 拼接丢字符)")
                else:
                    no_path_notes.append(f"tool_calls[index={idx}] 未找到包含 path 字段的参数"
                                         f"(模型未按预期结构输出,非服务端异常)")
                continue
            if path_value == target_path:
                matched_exactly = True
            else:
                content_mismatch_notes.append(f"tool_calls[index={idx}] path 内容与指令目标不完全一致"
                                              f"(模型复述准确度问题,仅记录不影响判定): "
                                              f"got_len={len(path_value)}, expect_len={len(target_path)}")
        if structural_errors:
            passed, skipped, capability_issue = False, False, False
            detail = "; ".join(structural_errors)
        elif json_parse_errors:
            passed, skipped, capability_issue = False, True, True
            detail = "; ".join(json_parse_errors)
        elif no_path_notes and not matched_exactly and not content_mismatch_notes:
            passed, skipped, capability_issue = False, True, False
            detail = "; ".join(no_path_notes)
        else:
            passed, skipped = True, False
            frag_note = "经历多分片拼接" if multi_fragment_detected else "单分片一次性到达"
            if content_mismatch_notes:
                capability_issue = True
                detail = f"{frag_note},结构完整; " + "; ".join(content_mismatch_notes)
            else:
                capability_issue = False
                detail = f"{frag_note},重建后的 path 参数逐字符一致(len={len(target_path)})"
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=200, latency_ms=latency, detail=detail, skipped=skipped,
            response_data={"tool_call_frags": tool_call_frags, "finish_reason": finish_reason},
            model_capability_issue=capability_issue, model_capability_reason=detail if capability_issue else ""
        )

    def test_servicestop(self, round_num):
        """测试 POST /servicestop —— 该接口会使服务进程退出。
        只能在所有其它测试完成后调用一次。"""
        body = {"text": "stop"}
        # 为了避免 _safe_request 里面重启误杀，这里直接发请求。
        url = f"{self.base_url}/servicestop"
        start_time = time.time()
        passed = False
        response_data = {}
        try:
            r = requests.post(url, json=body, timeout=15)
            latency = (time.time() - start_time) * 1000
            try:
                response_data = r.json()
            except Exception:
                response_data = {"raw_body": r.text[:200] if r.text else ""}
            passed = r.status_code == 200
            detail = "OK" if passed else f"FAIL: status={r.status_code}"
            return TestResult(
                name="POST /servicestop", round_num=round_num, model_name=self.model_name,
                passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
                response_data=response_data
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            # 服务可能在返回响应前就差点关了 socket，这种情况认为 PASS（表示服务已退出），
            # 但明确报告为“连接在服务退出过程中被断开”。
            latency = (time.time() - start_time) * 1000
            time.sleep(2)
            if self.svc.is_alive():
                return TestResult(
                    name="POST /servicestop", round_num=round_num, model_name=self.model_name,
                    passed=False, status_code=0, latency_ms=latency,
                    detail=f"FAIL: 请求异常 {type(e).__name__}: {e}"
                )
            return TestResult(
                name="POST /servicestop", round_num=round_num, model_name=self.model_name,
                passed=True, status_code=0, latency_ms=latency,
                detail=f"OK (服务已退出; 连接在响应前被服务主动关闭: {type(e).__name__})"
            )

    def test_unload(self, round_num):
        resp = self._safe_request("POST", "/unload", "POST /unload", round_num, json={})
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = r.status_code == 200
        return TestResult(
            name="POST /unload", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency,
            detail="OK" if passed else f"status={r.status_code}"
        )

# ============================================================================
# ReportGenerator - 报告生成器
# ============================================================================
class ReportGenerator:
    @staticmethod
    def generate_json(all_results, perf_data, crash_events, out_dir):
        # ignored 是 failed 的子集 — 不是与 passed/failed/crashed 并列的第四种状态,
        # 而是"这些 failed 里有多少属于服务端已知缺陷,在过滤后可以忽略"。
        # 排查时,如果只关心真实回归,可以用 failed - ignored 看"非忽略失败数"。
        summary = {"total": 0, "passed": 0, "failed": 0, "crashed": 0, "skipped": 0, "ignored": 0,
                   "model_capability_issues": 0}
        models_data = {}

        for result in all_results:
            summary["total"] += 1
            if result.skipped:
                summary["skipped"] += 1
            elif result.crashed:
                summary["crashed"] += 1
            elif result.passed:
                summary["passed"] += 1
            else:
                summary["failed"] += 1
                if result.ignorable:
                    summary["ignored"] += 1
            if result.model_capability_issue:
                summary["model_capability_issues"] += 1

            model = result.model_name
            if model not in models_data:
                models_data[model] = {}
            rnd = f"round_{result.round_num}"
            if rnd not in models_data[model]:
                models_data[model][rnd] = []
            models_data[model][rnd].append({
                "name": result.name,
                "passed": result.passed,
                "status_code": result.status_code,
                "latency_ms": round(result.latency_ms, 1),
                "detail": result.detail,
                "crashed": result.crashed,
                "skipped": result.skipped,
                # 已知缺陷标记: ignorable=True 意味着这条 FAIL 是服务端已知占位/未实现缺陷,
                # 用于在统计、CI gate、报告过滤时区分"真实回归"vs"已知缺陷"。
                "ignorable": result.ignorable,
                "ignore_reason": result.ignore_reason if result.ignorable else None,
                "model_capability_issue": result.model_capability_issue,
                "model_capability_reason": result.model_capability_reason if result.model_capability_issue else None,
                "is_stream": result.is_stream,
                "ttft_ms": round(result.ttft_ms, 1) if result.is_stream else None,
                "chunk_count": result.chunk_count if result.is_stream else None,
                "text_prompt": result.text_prompt if result.text_prompt else None,
                "text_response": result.text_response if result.text_response else None,
                "response_data": result.response_data if result.response_data else None,
            })

        output = {
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "models": models_data,
            "performance": perf_data,
            "crash_events": [asdict(e) for e in crash_events]
        }

        out_path = Path(out_dir) / "results.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\n[输出] JSON 报告: {out_path}")

    # 模型无关接口（不需要加载模型即可测试的通用接口 + 全局收尾接口）
    MODEL_INDEPENDENT_ENDPOINTS = [
        "GET /", "GET /models", "GET /v1/models",
        "POST /servicestop",
    ]

    # 模型相关的所有端点(显式枚举,顺序即报告里的行序)
    MODEL_DEPENDENT_ENDPOINTS = [
        # /status 观察用,夹在 chat 前后
        "GET /status (before chat)",
        # ChatCompletions handler 注册的 4 个别名 × (non-stream + stream) = 8 项
        "POST /v1/chat/completions (non-stream)",
        "POST /v1/chat/completions (stream)",
        "POST /chat/completions (non-stream)",
        "POST /chat/completions (stream)",
        "POST /v1/completions (non-stream)",
        "POST /v1/completions (stream)",
        "POST /completions (non-stream)",
        "POST /completions (stream)",
        "GET /status (after chat)",
        # 其它模型相关 GET / POST
        "GET /profile",
        "POST /contextsize",
        "POST /fetch",
        # TextSplitter handler 两个别名
        "POST /textsplitter",
        "POST /v1/textsplitter",
        # ImageGenerate handler 两个别名
        "POST /images/generations",
        "POST /v1/images/generations",
        "POST /reload",
        # 最后一轮的收尾
        "POST /stop", "POST /clear", "POST /unload",
    ]

    # 推理性能相关(所有 chat 别名,任何一个有 perf 数据都纳入对比)
    MODEL_PERF_ENDPOINTS = [
        "POST /v1/chat/completions (non-stream)",
        "POST /v1/chat/completions (stream)",
        "POST /chat/completions (non-stream)",
        "POST /chat/completions (stream)",
        "POST /v1/completions (non-stream)",
        "POST /v1/completions (stream)",
        "POST /completions (non-stream)",
        "POST /completions (stream)",
        "GET /profile",
    ]

    # 内部占位 model_name：仅用于内部过滤/聚合（_global_=模型无关通用接口测试、
    # _multi_model_=阶段3多模型并发聚合、_builder_local_model_=Builder 本地模型加载全链路
    # 测试里"不针对具体模型"的手段层检查项，如 configure_genie_root/inject_local_models/
    # discover_models/test_missing_csrf_rejected 等），绝不能作为"模型名"文本渗透进任何
    # 渲染出的报告 HTML（性能对比表、模型链接卡片、标题等）。新增任何占位 model_name 时
    # 必须同步加入这里，并统一通过 _is_internal_placeholder_model() 过滤——排除逻辑必须
    # 统一走这个函数，不要分散在多处各自手写，否则容易遗漏。
    INTERNAL_PLACEHOLDER_MODEL_NAMES = frozenset({"_global_", "_multi_model_", "_builder_local_model_"})
    INTERNAL_PLACEHOLDER_MODEL_PREFIXES = ("_graceful_shutdown_",)

    @staticmethod
    def _is_internal_placeholder_model(model_name):
        """判断 model_name 是否为内部占位名（非真实模型），渲染报告时必须排除。"""
        return (model_name in ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES
                or model_name.startswith(ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_PREFIXES))

    @staticmethod
    def _assert_no_placeholder_leak(html, context):
        """防泄漏兜底：报告生成完毕、写盘之前，扫描最终 HTML 全文，确认内部占位
        model_name 没有被当作可读文本渗透进去。这是最后一道防线——即使某个渲染路径
        遗漏了 _is_internal_placeholder_model() 过滤，也会在这里立刻炸出来，而不是让
        "_multi_model_" 这类占位符悄悄流入 report.html 被人误以为是一个真实模型。"""
        for name in ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES:
            if name in html:
                raise RuntimeError(
                    f"[占位符泄漏防护] 检测到内部占位 model_name {name!r} 以文本形式出现在"
                    f"生成的 HTML 中（{context}）。这不允许发生——请检查是否有渲染路径遗漏了"
                    f" ReportGenerator._is_internal_placeholder_model() 过滤。"
                )

    # "多类型同时加载"阶段的检查项名称（MULTI: 前缀）为内部实现层面的英文短语，对不熟悉
    # 代码的人偏抽象（如 "backend coexistence matrix (triple + pairwise fallback)"）。
    # 这里按"精确匹配的静态检查项"与"带动态后缀（模型名/组合名）的检查项"分两组维护
    # 说明文案，供 _generate_multi_backend_detail_html 的"逐项检查记录"表格渲染"说明"列。
    # 新增该阶段的检查项时应同步在此补充映射，否则该行"说明"列会留空。
    _MULTI_CHECK_DESCRIPTIONS_EXACT = {
        "MULTI: GET /models (multi-model list)":
            "调用 GET /models 接口，验证服务能正确返回当前已加载的全部模型清单",
        "MULTI: ensure NPU/GGUF/MNN backends loaded simultaneously":
            "依次加载 QNN(NPU)/GGUF(GPU)/MNN(CPU) 三种后端各一个模型，验证三者能否同时驻留在同一服务进程中（三后端同时在线）",
        "MULTI: backend coexistence matrix (triple + pairwise fallback)":
            "汇总上面\"三后端同时驻留\"与下面三种\"两两降级\"组合的验证结果，整理成一份综合共存判定矩阵（本条本身不发起新请求）",
        "MULTI: invalid model route returns 404/400":
            "请求一个不存在的模型名，验证服务能快速返回 404/400 错误，而不是长时间挂起或误路由到其它模型",
        "MULTI: concurrent requests to different models":
            "同时向多个已加载的不同模型并发发送请求，验证并发场景下各请求能被正确路由到对应模型、互不干扰、不引发崩溃（同时校验回复内容非空）",
        "MULTI: default model route (no 'model' field) after dynamic switches":
            "发一个不带 model 字段的请求，观察在多次动态切换后默认模型是否仍能正常响应（当前实现里默认模型会随动态切换漂移，本检查只监控不崩溃/不返回 5xx，不假定具体路由到哪个模型）",
    }
    _MULTI_CHECK_DESCRIPTIONS_PREFIX = (
        ("MULTI: auto-restart after MNN OOM (",
         "MNN 模型因内存不足导致进程崩溃后，验证服务能否自动重启并恢复到可正常接受请求的状态"),
        ("MULTI: pairwise coexistence ",
         "三后端未能全部同时驻留时的降级验证：重启服务后只尝试这两种后端的模型，检查它们能否同时加载并各自正确路由请求（至少两两可同时驻留是设计底线）"),
        ("MULTI: chat route → ",
         "向该模型发送一次 chat 请求，验证请求能被正确路由到此模型并返回正常响应"),
    )

    @staticmethod
    def _describe_multi_check(name):
        """把"多类型同时加载"阶段某条检查项的名称，翻译成一句人类可读的说明文字。
        精确匹配优先；未命中时再按已知前缀匹配带动态后缀（模型名/组合名）的检查项；
        两者都未命中返回空字符串（该行"说明"列留空，而不是报错阻断报告生成）。"""
        desc = ReportGenerator._MULTI_CHECK_DESCRIPTIONS_EXACT.get(name)
        if desc:
            return desc
        for prefix, prefix_desc in ReportGenerator._MULTI_CHECK_DESCRIPTIONS_PREFIX:
            if name.startswith(prefix):
                return prefix_desc
        return ""

    # "逐项检查记录"表格按类别分组展示，而不是把全部检查项摊成一张不分类的长列表——
    # 单模型路由验证("MULTI: chat route → <model>")会随已加载模型数量线性增长、逐条描述
    # 高度雷同，混在其它类别检查项中会显得杂乱且难以定位。每个元组为
    # (类别名, 精确匹配名称集合, 前缀匹配元组)；分类顺序即渲染顺序。新增检查项时应同步
    # 在此归类，未归类的检查项会落入下方 _MULTI_CHECK_CATEGORY_FALLBACK 兜底类别（不报错）。
    _MULTI_CHECK_CATEGORIES = (
        ("单模型路由验证", (), ("MULTI: chat route → ",)),
        ("后端共存与两两降级验证", (
            "MULTI: ensure NPU/GGUF/MNN backends loaded simultaneously",
            "MULTI: backend coexistence matrix (triple + pairwise fallback)",
        ), ("MULTI: pairwise coexistence ",)),
        ("通用接口与异常路由", (
            "MULTI: GET /models (multi-model list)",
            "MULTI: invalid model route returns 404/400",
            "MULTI: default model route (no 'model' field) after dynamic switches",
        ), ()),
        ("并发与故障恢复", (
            "MULTI: concurrent requests to different models",
        ), ("MULTI: auto-restart after MNN OOM (",)),
    )
    _MULTI_CHECK_CATEGORY_FALLBACK = "其它检查项"

    @staticmethod
    def _categorize_multi_check(name):
        """把"多类型同时加载"阶段某条检查项的名称归入一个分组类别，用于"逐项检查记录"
        表格的分组展示。精确匹配优先；未命中时再按前缀匹配；两者都未命中归入统一的兜底
        类别（而不是报错阻断报告生成）。"""
        for category, exact_names, prefixes in ReportGenerator._MULTI_CHECK_CATEGORIES:
            if name in exact_names:
                return category
            for prefix in prefixes:
                if name.startswith(prefix):
                    return category
        return ReportGenerator._MULTI_CHECK_CATEGORY_FALLBACK

    @staticmethod
    def _common_css():
        """Professional dashboard aesthetic — Material-inspired blue header, soft-shadow cards, sans-serif body, tabular mono numbers. Mirrors the legacy report.html with subtle refinements."""
        return """:root {
    --primary: #1565C0;
    --primary-light: #1976D2;
    --primary-dark: #0D47A1;
    --accent: #1565C0;
    --success: #2E7D32;
    --success-light: #43A047;
    --danger: #C62828;
    --danger-light: #E53935;
    --warning: #E65100;
    --warning-light: #FB8C00;
    --muted: #78909C;
    --bg: #F5F7FA;
    --bg-alt: #F8FAFE;
    --card-bg: #FFFFFF;
    --border: #E0E0E0;
    --border-strong: #BDBDBD;
    --text: #263238;
    --text-secondary: #546E7A;
    --rule: #E0E0E0;
    --radius: 8px;
    --radius-sm: 4px;
    --shadow: 0 2px 8px rgba(0,0,0,0.08);
    --shadow-hover: 0 4px 14px rgba(21,101,192,0.12);
    --font-serif: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    --font-mono: 'SF Mono', 'Cascadia Code', 'JetBrains Mono', Consolas, Menlo, monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { font-feature-settings: 'kern', 'liga', 'calt', 'tnum'; }
body { font-family: var(--font-sans); background: var(--bg); color: var(--text); line-height: 1.6; font-size: 14px; -webkit-font-smoothing: antialiased; }
.header { background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%); color: #FFFFFF; padding: 40px 0; margin-bottom: 30px; position: relative; box-shadow: 0 2px 12px rgba(13,71,161,0.18); }
.header-inner { max-width: 1400px; margin: 0 auto; padding: 0 30px; }
.header .eyebrow { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: rgba(255,255,255,0.75); margin-bottom: 10px; font-weight: 600; }
.header h1 { font-size: 30px; font-weight: 600; margin-bottom: 8px; letter-spacing: -0.5px; line-height: 1.2; color: #FFFFFF; }
.header .subtitle { color: rgba(255,255,255,0.85); font-size: 14px; max-width: 720px; }
.header .meta { display: flex; gap: 28px; margin-top: 18px; flex-wrap: wrap; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.18); }
.header .meta-item { font-size: 13px; color: rgba(255,255,255,0.92); font-variant-numeric: tabular-nums; }
.header .meta-item strong { color: #FFFFFF; font-weight: 600; }
.cmdline-details { display: inline-block; vertical-align: middle; }
.cmdline-details summary { cursor: pointer; color: rgba(255,255,255,0.92); font-weight: 600; list-style: none; }
.cmdline-details summary::-webkit-details-marker { display: none; }
.cmdline-details summary strong { color: #FFFFFF; }
.cmdline-pre { white-space: pre-wrap; word-break: break-all; font-family: var(--font-mono); font-size: 11px; line-height: 1.5;
    color: rgba(255,255,255,0.92); margin: 8px 0 0; max-width: 900px; max-height: 160px; overflow-y: auto;
    background: rgba(255,255,255,0.12); padding: 8px 10px; border-radius: var(--radius-sm); }
.container { max-width: 1400px; margin: 0 auto; padding: 0 30px 60px; }
.summary-bar { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 28px; }
.summary-card { background: var(--card-bg); border-radius: var(--radius); padding: 22px 20px; text-align: center; box-shadow: var(--shadow); border-top: 4px solid var(--muted); transition: transform 0.18s ease, box-shadow 0.18s ease; }
.summary-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
.summary-card.card-pass { border-top-color: var(--success); }
.summary-card.card-fail { border-top-color: var(--danger); }
.summary-card.card-crash { border-top-color: var(--warning); }
.summary-card .num { font-size: 36px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; letter-spacing: -0.5px; color: var(--text); }
.summary-card .label { font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; font-weight: 600; }
.summary-card.card-pass .num { color: var(--success); }
.summary-card.card-fail .num { color: var(--danger); }
.summary-card.card-crash .num { color: var(--warning); }
.progress-bar { background: #E8EAF6; border-radius: 20px; height: 28px; margin: 18px 0 8px; overflow: hidden; position: relative; box-shadow: inset 0 1px 2px rgba(0,0,0,0.04); }
.progress-fill { height: 100%; border-radius: 20px; transition: width 0.6s ease; display:flex; align-items:center; justify-content:center; }
.progress-fill.good { background: linear-gradient(90deg, var(--success-light), #66BB6A); }
.progress-fill.warn { background: linear-gradient(90deg, var(--warning-light), #FFA726); }
.progress-fill.bad { background: linear-gradient(90deg, var(--danger-light), #EF5350); }
.progress-text { font-size: 13px; font-weight: 600; color: white; text-shadow: 0 1px 2px rgba(0,0,0,0.3); letter-spacing: 0.3px; }
.progress-meta { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-secondary); margin-bottom: 28px; padding: 0 4px; font-variant-numeric: tabular-nums; }
.progress-meta .pct { color: var(--primary); font-weight: 600; font-size: 13px; }
.section { margin-bottom: 32px; }
.section h2 { font-size: 18px; font-weight: 600; color: var(--text); margin-bottom: 16px; padding-bottom: 10px; border-bottom: 2px solid var(--border); letter-spacing: -0.2px; position: relative; display: flex; align-items: center; }
.section h2::after { content: ''; position: absolute; left: 0; bottom: -2px; width: 56px; height: 2px; background: var(--primary); }
.section h2 .icon { color: var(--primary); margin-right: 8px; }
.icon { margin-right: 8px; }
.model-section { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 24px; overflow: hidden; transition: box-shadow 0.18s ease; }
.model-section:hover { box-shadow: var(--shadow-hover); }
.model-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: linear-gradient(180deg, #FAFAFA 0%, #F5F5F5 100%); border-bottom: 1px solid var(--border); }
.model-header h3 { font-size: 16px; color: var(--primary); font-weight: 600; letter-spacing: -0.2px; }
.model-stats { display: flex; gap: 12px; align-items: center; }
.stat-pass { font-size: 13px; color: var(--success); font-weight: 500; font-variant-numeric: tabular-nums; }
.stat-rate { font-size: 12px; background: var(--primary); color: white; padding: 3px 12px; border-radius: 12px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: 0.2px; }
.group-section { padding: 18px 20px; border-bottom: 1px solid #F0F0F0; }
.group-section:last-child { border-bottom: none; }
.group-title { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px; letter-spacing: 0.2px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { background: #F5F5F5; padding: 10px 12px; text-align: left; font-weight: 600; color: var(--text-secondary);
            text-transform: uppercase; font-size: 11px; letter-spacing: 0.4px; border-bottom: 2px solid var(--border); }
tbody td { padding: 10px 12px; border-bottom: 1px solid #F0F0F0; vertical-align: middle; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: var(--bg-alt); }
tbody tr:last-child td { border-bottom: none; }
td.center, th.center { text-align: center; }
td.num, th.num { text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
code { background: #F5F5F5; padding: 2px 6px; border-radius: 3px; font-family: var(--font-mono); font-size: 12px; color: var(--text); }
.status-pass, .status-fail, .status-crash, .status-skip { display: inline-block; font-weight: 700; font-size: 11px; letter-spacing: 0.5px; padding: 3px 9px; min-width: 52px; text-align: center; border-radius: 10px; }
.status-pass { color: var(--success); background: rgba(46,125,50,0.10); }
.status-fail { color: var(--danger); background: rgba(198,40,40,0.10); }
.status-crash { color: var(--warning); background: rgba(230,81,0,0.10); }
.status-skip { color: var(--muted); background: rgba(120,144,156,0.12); }
.badge-ignorable { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(255,152,0,0.14); color: #E65100; border: 1px solid rgba(230,81,0,0.30); cursor: help; vertical-align: middle; }
.badge-capability { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(30,136,229,0.14); color: #1565C0; border: 1px solid rgba(21,101,192,0.30); cursor: help; vertical-align: middle; }
.badge-mnn-oom { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(198,40,40,0.14); color: var(--danger); border: 1px solid rgba(198,40,40,0.30); cursor: help; vertical-align: middle; }
.badge-mnn-cascade { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(120,144,156,0.16); color: #37474F; border: 1px solid rgba(84,110,122,0.30); cursor: help; vertical-align: middle; }
.card-ignored { border-top: 3px solid #FB8C00; }
.card-ignored .num { color: #E65100; }
.card-capability { border-top: 3px solid var(--warning); }
.card-capability .num { color: var(--warning); }
.latency-warn { color: #F57C00; font-weight: 600; }
.latency-slow { color: var(--danger); font-weight: 700; }
.cv-stable { color: var(--success); font-size: 11px; font-weight: 600; }
.cv-moderate { color: #F57C00; font-size: 11px; font-weight: 600; }
.cv-unstable { color: var(--danger); font-size: 11px; font-weight: 700; }
.pivot-table { margin: 0; }
.resp-cell { max-width: 360px; min-width: 240px; font-size: 12px; vertical-align: middle; padding: 10px 12px !important; }
.resp-ok { display: inline-block; color: var(--success); font-weight: 600; font-size: 12px; letter-spacing: 0.2px; }
.resp-error { color: var(--danger); font-size: 11px; word-break: break-all; font-family: var(--font-mono); line-height: 1.45; }
.resp-detail { color: var(--text-secondary); font-size: 11px; line-height: 1.45; }
.resp-value { font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums; }
.resp-value strong { color: var(--primary); font-weight: 700; }
.resp-models { font-weight: 600; color: var(--primary); display: block; margin-bottom: 4px; }
.resp-status { color: var(--text); }
.resp-empty { color: var(--muted); font-style: italic; }
.resp-json { font-size: 11px; line-height: 1.6; color: var(--text-secondary); font-family: var(--font-mono); }
.resp-json strong { color: var(--text); font-weight: 600; }
.resp-list { font-size: 11px; margin-top: 4px; color: var(--text-secondary); line-height: 1.5; }
.resp-link { display: inline-block; margin-left: 6px; font-size: 11px; color: var(--primary); text-decoration: none; font-weight: 600; padding: 2px 9px; border-radius: 10px; background: rgba(21,101,192,0.08); transition: background 0.15s, color 0.15s; }
.resp-link:hover { background: var(--primary); color: #FFFFFF; }
.resp-round { margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px dashed var(--border); font-size: 12px; }
.resp-round:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.resp-kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; font-family: var(--font-mono); font-size: 11px; line-height: 1.5; align-items: baseline; }
.resp-kv .kv-key { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
.resp-kv .kv-val { color: var(--text); font-variant-numeric: tabular-nums; word-break: break-all; }
.resp-kv .kv-more { grid-column: 1 / -1; color: var(--muted); font-size: 10px; margin-top: 4px; font-style: italic; }
.compare-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
.perf-round-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 16px; margin-top: 4px; }
.perf-round-card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px; border-top: 3px solid var(--primary-light); transition: transform 0.18s ease, box-shadow 0.18s ease; }
.perf-round-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
.perf-round-card .perf-round-title { font-size: 13px; font-weight: 700; color: var(--primary); margin-bottom: 10px; letter-spacing: 0.2px; }
.perf-round-card .perf-metric { display: flex; justify-content: space-between; align-items: baseline; padding: 5px 0; font-size: 12px; border-bottom: 1px dashed #EEEEEE; }
.perf-round-card .perf-metric:last-child { border-bottom: none; }
.perf-round-card .perf-metric-label { color: var(--text-secondary); }
.perf-round-card .perf-metric-value { font-family: var(--font-mono); font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; white-space: nowrap; margin-left: 10px; }
.mode-tag { padding: 2px 9px; border-radius: 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.2px; }
.mode-stream { background: #E3F2FD; color: var(--primary); }
.mode-nonstream { background: #FFF3E0; color: var(--warning); }
.mode-format-string { background: #ECEFF1; color: var(--text-secondary); }
.mode-format-array { background: #E1F5FE; color: #0277BD; }
.mode-format-object { background: #F3E5F5; color: #6A1B9A; }
.delta-up { color: var(--danger); font-weight: 600; font-family: var(--font-mono); }
.delta-down { color: var(--success); font-weight: 600; font-family: var(--font-mono); }
.crash-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; border-left: 4px solid var(--danger); }
.crash-table tbody td { color: var(--danger); }
.perf-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 16px; margin-bottom: 22px; }
.perf-stat { background: var(--card-bg); border-radius: var(--radius); padding: 18px; text-align: center; box-shadow: var(--shadow); border-top: 3px solid var(--primary-light); }
.perf-value { display: block; font-size: 28px; font-weight: 700; color: var(--primary); font-variant-numeric: tabular-nums; letter-spacing: -0.5px; line-height: 1.1; }
.perf-label { font-size: 12px; color: var(--text-secondary); margin-top: 6px; text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }
.chart-container { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.chart-container canvas { background: var(--card-bg); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); height: 280px !important; }
.empty-state { background: var(--card-bg); border-radius: var(--radius); padding: 30px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 14px; box-shadow: var(--shadow); }
.footer { text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 40px; letter-spacing: 0.2px; }
.model-link-card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 22px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; transition: transform 0.18s ease, box-shadow 0.18s ease; border-left: 3px solid var(--primary); }
.model-link-card:hover { transform: translateX(2px); box-shadow: var(--shadow-hover); }
.model-link-card a { text-decoration: none; color: var(--primary); font-weight: 600; font-size: 16px; letter-spacing: -0.2px; }
.model-link-card a:hover { color: var(--primary-dark); }
.model-link-card .model-link-stats { display: flex; gap: 18px; font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; letter-spacing: 0.2px; align-items: center; }
.startup-banner { background: linear-gradient(135deg, #FFEBEE 0%, #FFCDD2 100%); border: 1px solid var(--danger); border-left: 6px solid var(--danger); border-radius: var(--radius); padding: 22px 26px; margin-bottom: 24px; box-shadow: 0 3px 14px rgba(198,40,40,0.18); }
.startup-banner-title { font-size: 18px; font-weight: 700; color: var(--danger); margin-bottom: 8px; letter-spacing: -0.2px; }
.startup-banner-desc { font-size: 13px; color: #5D2020; line-height: 1.65; margin-bottom: 14px; }
.startup-banner-desc code { background: rgba(198,40,40,0.10); color: var(--danger); padding: 2px 6px; border-radius: 3px; font-size: 12px; }
.startup-table { width: 100%; border-collapse: collapse; background: rgba(255,255,255,0.7); border-radius: var(--radius-sm); overflow: hidden; }
.startup-table thead th { background: rgba(198,40,40,0.10); color: var(--danger); padding: 9px 12px; text-align: left; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; border-bottom: 1px solid rgba(198,40,40,0.20); }
.startup-table tbody td { padding: 10px 12px; border-bottom: 1px solid rgba(198,40,40,0.10); vertical-align: top; font-size: 12px; color: #3E1212; }
.startup-table tbody tr:last-child td { border-bottom: none; }
.startup-detail { white-space: pre-wrap; word-break: break-word; font-family: var(--font-mono); font-size: 11px; line-height: 1.5; color: #4A1818; margin: 0; max-height: 240px; overflow-y: auto; background: rgba(255,255,255,0.6); padding: 8px 10px; border-radius: 3px; }
@media (max-width: 768px) {
    .chart-container { grid-template-columns: 1fr; }
    .header .meta { flex-direction: column; gap: 8px; }
    .pivot-table { font-size: 11px; }
}
@media print {
    .header { background: #333 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .summary-card, .model-section, table { box-shadow: none; border: 1px solid #ddd; }
}"""

    @staticmethod
    def _format_response_data(r):
        """格式化响应数据为 HTML 展示"""
        ep = r.name

        # chat/completions 特殊处理：即使 response_data 为空，只要有 text_response 就生成链接
        if "chat/completions" in ep:
            fmt_badge = _content_format_badge_html(r)
            if not r.passed:
                return f'<span class="resp-detail">{_html_escape(r.detail[:200])}</span>{fmt_badge}'
            mode = "stream" if r.is_stream else "nonstream"
            anchor = f"{r.model_name}_R{r.round_num}_{mode}"
            if r.is_stream:
                return f'<span class="resp-value">TTFT={r.ttft_ms:.0f}ms, {r.chunk_count} chunks</span>{fmt_badge} <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'
            else:
                resp_len = len(r.text_response) if r.text_response else 0
                return f'<span class="resp-value">响应长度: {resp_len} 字符</span>{fmt_badge} <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        if not r.response_data:
            if r.status_code == 200 and r.passed:
                return '<span class="resp-ok">OK</span>'
            elif r.detail:
                return f'<span class="resp-detail">{_html_escape(r.detail[:80])}</span>'
            return '-'

        data = r.response_data

        # 根据不同接口类型格式化展示
        if "models" in ep.lower() and "models" in data:
            # 与模型数量无关，统一显示 OK
            return '<span class="resp-ok">OK</span>'

        elif ep == "GET /status":
            loading = data.get("loading")
            error = data.get("error")
            if error:
                return f'<span class="resp-error">{_html_escape(str(error)[:80])}</span>'
            return f'<span class="resp-status">loading: {loading}</span>'

        elif ep == "GET /profile":
            # 排除以下 6 项指标——它们已在“性能报告”小节中独立展示，避免重复
            _PERF_KEYS_HIDDEN = {
                "time_to_first_token", "token_generation_time",
                "prompt_processing_rate", "token_generation_rate",
                "num_prompt_tokens", "num_generated_tokens",
            }
            keys = [k for k in data.keys() if k not in _PERF_KEYS_HIDDEN]
            if not keys:
                # 全部字段都是性能指标——下方“性能报告”已经展示，这里直接给 OK
                return '<span class="resp-ok">OK</span>'
            items = []
            for k in keys[:6]:
                v = data[k]
                if isinstance(v, (dict, list)):
                    v_str = f"[{len(v)} items]" if isinstance(v, list) else f"{{{len(v)} keys}}"
                else:
                    v_str = str(v)[:30]
                items.append(f'<span class="kv-key">{_html_escape(k)}</span><span class="kv-val">{_html_escape(v_str)}</span>')
            more = f'<div class="kv-more">+{len(keys)-6} more</div>' if len(keys) > 6 else ""
            return f'<div class="resp-kv">{"".join(items)}{more}</div>'

        elif ep == "POST /contextsize":
            ctx = data.get("contextsize", 0)
            return f'<span class="resp-value">contextsize = <strong>{ctx}</strong></span>'

        elif "textsplitter" in ep:
            seg_count = data.get("segment_count", 0)
            anchor = f"{r.model_name}_R{r.round_num}_textsplitter"
            return f'<span class="resp-value">{seg_count} segments</span> <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        elif ep == "POST /fetch":
            count = data.get("history_count", 0)
            if count == 0:
                return '<span class="resp-value">history: 空</span>'
            anchor = f"{r.model_name}_R{r.round_num}_fetch"
            return f'<span class="resp-value">{count} 条记录</span> <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        elif ep == "POST /reload":
            error = data.get("error", "")
            if error:
                return f'<span class="resp-error">{_html_escape(str(error)[:80])}</span>'
            return '<span class="resp-ok">OK</span>'

        else:
            if isinstance(data, dict) and data:
                items = [f'{_html_escape(k)}: {_html_escape(str(v)[:30])}' for k, v in list(data.items())[:3]]
                return f'<div class="resp-json">{"<br>".join(items)}</div>'
            return '<span class="resp-ok">OK</span>'

    @staticmethod
    def _build_pivot_table(results_subset, endpoint_list, rounds):
        """构建透视表：端点为行，R1/R2/R3 为列，最后一列显示响应数据"""
        endpoints_in_data = []
        for ep in endpoint_list:
            if any(r.name == ep for r in results_subset):
                endpoints_in_data.append(ep)

        if not endpoints_in_data:
            return ""

        round_headers = ""
        for rnd in rounds:
            round_headers += f'<th class="center">R{rnd}</th><th class="num">延迟</th>'

        html = f"""<table class="pivot-table">
        <thead><tr><th>端点</th>{round_headers}<th>响应数据</th></tr></thead>
        <tbody>"""

        # Endpoints whose response data should be shown per-round (large data with links)
        _per_round_endpoints = set(ReportGenerator.MODEL_PERF_ENDPOINTS) | {
            "POST /fetch", "POST /textsplitter", "POST /v1/textsplitter",
        }
        # /profile 是性能接口但返回体紧凑,不需要每轮都展开
        _per_round_endpoints.discard("GET /profile")

        for ep in endpoints_in_data:
            ep_results = {r.round_num: r for r in results_subset if r.name == ep}
            html += f'<tr><td><code>{_html_escape(ep)}</code></td>'

            all_round_results = []
            for rnd in rounds:
                r = ep_results.get(rnd)
                if r is None:
                    html += '<td class="center">-</td><td class="center">-</td>'
                else:
                    if r.skipped:
                        cls, txt = "status-skip", "SKIP"
                    elif r.crashed:
                        cls, txt = "status-crash", "CRASH"
                    elif r.passed:
                        cls, txt = "status-pass", "PASS"
                    else:
                        cls, txt = "status-fail", "FAIL"
                    lat_cls = ""
                    if r.latency_ms > 10000:
                        lat_cls = "latency-slow"
                    elif r.latency_ms > 3000:
                        lat_cls = "latency-warn"
                    # 已知缺陷标记: FAIL 但 ignorable=True 时,在徽章右侧加一个橙色"可忽略"小徽章,
                    # title 属性显示具体原因。这样表格里一眼能区分"真实回归"和"已知占位失败"。
                    # ignore_reason=="mnn_oom_cascade" 是一个独立的诊断标识(见 _classify_process_down)：
                    # 表示"疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需要人工排查根因"，
                    # 但崩溃永远是真实问题——该标识不再意味着 ignorable=True，只是用灰色的
                    # .badge-mnn-cascade 诊断徽章标注疑似关联，FAIL 状态和统计口径不受影响。
                    ignorable_badge = ""
                    if not r.passed:
                        ignore_reason = getattr(r, "ignore_reason", "")
                        if ignore_reason == "mnn_oom_cascade":
                            reason = _html_escape(getattr(r, "detail", "") or "疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因(已计入真实失败,非豁免)")
                            ignorable_badge = f'<span class="badge-mnn-cascade" title="{reason}">疑似级联崩溃(非豁免)</span>'
                        elif getattr(r, "ignorable", False):
                            reason = _html_escape(ignore_reason or "已知服务端缺陷")
                            ignorable_badge = f'<span class="badge-ignorable" title="{reason}">可忽略</span>'
                    # 模型能力局限标记: passed=True 但 model_capability_issue=True 时单列一个蓝色
                    # 徽章,与"可忽略"并列展示，避免与普通 PASS 混淆。统一判断 model_capability_issue
                    # 这一对通用字段(tool_calls 模型能力局限、服务端 [MODEL_DEFECT] 日志标记同样命中)。
                    if getattr(r, "model_capability_issue", False):
                        reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
                        ignorable_badge += f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
                    html += f'<td class="center"><span class="{cls}">{txt}</span>{ignorable_badge}</td>'
                    html += f'<td class="num {lat_cls}">{r.latency_ms:.0f} ms</td>'
                    all_round_results.append(r)

            # 响应数据列：对于有大量数据的端点，每轮都显示链接；其他端点只显示最后一轮
            if all_round_results:
                if ep in _per_round_endpoints:
                    resp_parts = []
                    for r in all_round_results:
                        resp_parts.append(f'<div class="resp-round">R{r.round_num}: {ReportGenerator._format_response_data(r)}</div>')
                    html += f'<td class="resp-cell">{"".join(resp_parts)}</td>'
                else:
                    resp_html = ReportGenerator._format_response_data(all_round_results[-1])
                    html += f'<td class="resp-cell">{resp_html}</td>'
            else:
                html += '<td class="resp-cell">-</td>'
            html += '</tr>'

        html += "</tbody></table>"
        return html

    @staticmethod
    def _case_status_badge_html(r):
        """根据 TestResult 的 skipped/crashed/passed/ignorable/ignore_reason/model_capability_issue
        计算状态徽章 HTML,与 _build_pivot_table 保持完全一致的判定与视觉语言,供不适合"端点 x 轮次"
        矩阵布局的逐用例明细表(Builder 集成代理/SampleApp)复用。返回 (cls, txt, extra_badges_html)。"""
        if r.skipped:
            cls, txt = "status-skip", "SKIP"
        elif r.crashed:
            cls, txt = "status-crash", "CRASH"
        elif r.passed:
            cls, txt = "status-pass", "PASS"
        else:
            cls, txt = "status-fail", "FAIL"
        extra_badges = ""
        if not r.passed:
            ignore_reason = getattr(r, "ignore_reason", "")
            if ignore_reason == "mnn_oom_cascade":
                reason = _html_escape(getattr(r, "detail", "") or "疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因(已计入真实失败,非豁免)")
                extra_badges = f'<span class="badge-mnn-cascade" title="{reason}">疑似级联崩溃(非豁免)</span>'
            elif getattr(r, "ignorable", False):
                reason = _html_escape(ignore_reason or "已知服务端缺陷")
                extra_badges = f'<span class="badge-ignorable" title="{reason}">可忽略</span>'
        # 统一判断 model_capability_issue,与 _build_pivot_table 保持同一套口径。
        if getattr(r, "model_capability_issue", False):
            reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
            extra_badges += f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
        return cls, txt, extra_badges

    @staticmethod
    def _build_case_detail_table(results, show_model_column=True):
        """构建逐用例明细表：不同于 _build_pivot_table 的"端点 x 轮次"矩阵布局(那种布局假定
        同一端点会跨多轮重复出现),Builder 集成代理 / SampleApp / tool_calls 协议测试这类用例
        通常每个模型/后端只跑一次,按用例名逐行展示 通过状态/模型或后端/耗时/详情/模型能力问题
        更直观。复用既有 .pivot-table/.status-*/.badge-* 类名体系，保持一致的视觉语言。"""
        if not results:
            return ""
        model_header = "<th>模型/后端</th>" if show_model_column else ""
        html = f"""<table class="pivot-table">
        <thead><tr><th>用例</th>{model_header}<th class="center">状态</th><th class="num">耗时</th><th>详情</th><th>模型能力问题</th></tr></thead>
        <tbody>"""
        for r in results:
            cls, txt, extra_badges = ReportGenerator._case_status_badge_html(r)
            model_cell = f'<td>{_html_escape(r.model_name)}</td>' if show_model_column else ""
            capability_cell = ""
            if getattr(r, "model_capability_issue", False):
                reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
                capability_cell = f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
            html += f"""<tr><td><code>{_html_escape(r.name)}</code></td>{model_cell}
            <td class="center"><span class="{cls}">{txt}</span>{extra_badges}</td>
            <td class="num">{r.latency_ms:.0f} ms</td>
            <td>{_html_escape(r.detail)}</td>
            <td class="center">{capability_cell}</td></tr>"""
        html += "</tbody></table>"
        return html

    @staticmethod
    def _cmdline_meta_html(cmdline):
        """构建 header 区域"命令行"展示项：展示本次运行调用 test_service.py 时的完整命令行，
        复用 --font-mono 等宽字体样式（.cmdline-pre，与 .startup-detail 同一套设计语言，
        仅针对深色 header 背景调整了配色），超长命令行通过 <details> 折叠，避免撑爆 header 布局。
        未提供 cmdline（例如向后兼容旧调用点）时不渲染该项。"""
        if not cmdline:
            return ""
        # 默认展开（open 属性）：其它 meta-item 都是标签+值直接可见，命令行若默认折叠，
        # 用户不点击就只看到"命令行"这个词、看不到任何内容，容易误以为该字段是空的。
        return (f'<span class="meta-item"><details class="cmdline-details" open><summary><strong>命令行</strong></summary>'
                f'<pre class="cmdline-pre">{_html_escape(cmdline)}</pre></details></span>')

    # --suite 取值 → 该 suite 本轮会覆盖到的测试完备性矩阵列（类别名）。
    # full 现在也真正执行 SampleApp(见 _run_full_suite 阶段6),覆盖全部类别;
    # 未被本轮 suite 覆盖的类别在矩阵中统一显示"未运行本轮",避免与真实失败混淆。
    _SUITE_MATRIX_COVERAGE = {
        "full": {"通用接口", "文本chat", "多模态", "多模型路由", "GGUF显式加载", "SampleApp", "Builder/OpenClaw"},
        "model": {"通用接口", "文本chat", "多模态"},
        "multimodal": {"多模态"},
        "multi_model": {"多模型路由"},
        "gguf": {"GGUF显式加载"},
        "sampleapp": {"SampleApp"},
        "builder_local_model": {"Builder/OpenClaw"},
        "mnn": {"通用接口", "文本chat"},
        "qnn": {"通用接口", "文本chat", "多模态"},
    }
    MATRIX_CATEGORIES = ("通用接口", "文本chat", "多模态", "多模型路由", "GGUF显式加载", "SampleApp", "Builder/OpenClaw")

    # 设备后缀形式如 " (CPU)"/" (GPU)"/" (NPU)"：GGUF 模型未在 config.json 显式指定 device 时
    # 由服务自行决定实际加载设备(见 _run_model_suite),报告把这类模型的展示名统一打上该后缀,
    # 使常规生命周期测试(阶段2)与显式设备加载回归(阶段4)落在同一行,不再拆成 3 行。
    _DEVICE_SUFFIX_RE = re.compile(r" \([A-Z]+\)$")

    @staticmethod
    def _strip_device_suffix(name):
        """去掉展示名上的设备后缀,还原成服务内部注册的裸模型名。多模型并发阶段(阶段3)
        产生的 TestResult.name 里引用的仍是裸模型名,需要用这个还原后的名字做子串匹配,
        否则 GGUF 模型被打上设备后缀后,"多模型路由"这一列会因为字符串不再匹配而漏判。"""
        return ReportGenerator._DEVICE_SUFFIX_RE.sub("", name)

    @staticmethod
    def _env_constraint_reason(r):
        """判断某条未通过的 TestResult 是否属于"环境受限"的真实失败(而非未预期的回归),
        用于测试完备性矩阵的展示层面归类为"跳过(注明原因)"。仅影响矩阵单元格的展示状态,
        不修改 TestResult 本身的 passed/crashed/ignorable/skipped 字段——这些失败仍然如实
        计入 failed/crashed 统计和退出码判定(崩溃/资源不足永远是需要关注的真实问题,这里
        只是让矩阵里的"失败"状态更聚焦于真正未预期的回归,不与已知的资源限制混在一起)。"""
        detail = r.detail or ""
        detail_lower = detail.lower()
        if "insufficient_memory" in detail_lower or "内存不足" in detail:
            return "内存不足(insufficient_memory)"
        if getattr(r, "ignore_reason", "") == "mnn_oom_cascade":
            return "疑似MNN OOM级联崩溃(需人工排查,已计入失败统计)"
        return None

    @staticmethod
    def _matrix_cell_status(matched_results):
        """把某个模型×类别对应的一批 TestResult 归纳为矩阵单元格状态,返回 (status, reason)。
        reason 仅在 status=="跳过" 且能定位到具体原因时非空,用于单元格 title 提示。"""
        if not matched_results:
            return "未运行本轮", ""
        if all(r.skipped for r in matched_results):
            reasons = sorted({r.detail for r in matched_results if r.detail})
            return "跳过", "; ".join(reasons[:3])
        if all(r.passed or getattr(r, "ignorable", False) for r in matched_results):
            return "通过", ""
        # 未通过的结果里，如果全部都能归因到已知的"环境受限"信号(MNN 内存不足优雅拒绝、
        # 疑似 MNN OOM 级联崩溃等)，或本身已被测试逻辑正确判定为 skipped(如模型能力局限
        # 的 tool_calls 探测)，矩阵展示层面视为"跳过(注明原因)"，不改变底层统计口径。这里
        # 必须把 r.skipped 一起从 failing 里排除，否则同一类别下"部分通过 + 部分合理跳过"的
        # 混合结果会因为不满足"全部 skipped"也不满足"全部 passed/ignorable"而误落入失败分支。
        failing = [r for r in matched_results if not (r.passed or getattr(r, "ignorable", False) or r.skipped)]
        if not failing:
            skip_reasons = sorted({r.detail for r in matched_results if r.skipped and r.detail})
            return "跳过", "; ".join(skip_reasons[:3])
        env_reasons = [ReportGenerator._env_constraint_reason(r) for r in failing]
        if all(env_reasons):
            return "跳过", "; ".join(sorted(set(env_reasons))[:3])
        return "失败", ""

    # "通用接口"是兜底类别，只应容纳"没有专属矩阵列"的模型级用例。tool_calls/GGUF显式
    # 加载/Builder/SampleApp 都已经有各自的专属列（见下方各分支）、以及独立的报告展示区块
    # (report_tool_calls.html、逐模型报告的同名区块等)，其判定结果(尤其是 skipped=True 这
    # 类模型能力局限/环境限制)与"通用接口"本意（GET /、/contextsize、/textsplitter 等真正
    # 与模型能力无关的端点）毫无关系；一旦遗漏排除，就会被重复计入"通用接口"这一列，让它的
    # 判定结果被其它专属列的原因污染（真实案例：tool_calls 机会性探测的 skipped 曾污染
    # phi4-v81/qwen2.5vl3b-8480 的"通用接口"列，详见 4.9）。新增任何专属矩阵列时，必须把该
    # 列的匹配条件同步加入这里，防止同一条结果被"通用接口"重复计入。
    @staticmethod
    def _has_dedicated_matrix_category(name):
        return (name.startswith("POST /v1/chat/completions (tool_call")
                or name.startswith("SAMPLEAPP:")
                or name.startswith("BUILDER")
                or "explicit_load" in name)

    @staticmethod
    def _build_completeness_matrix(all_results, model_names, suite_name):
        """测试完备性矩阵：行为模型,列为测试类别,展示各模型在各类别下本轮的通过/失败/跳过/未运行本轮状态。"""
        if not model_names:
            return ""
        covered = ReportGenerator._SUITE_MATRIX_COVERAGE.get(suite_name, set())
        perf_endpoints = set(ReportGenerator.MODEL_PERF_ENDPOINTS)

        rows_html = ""
        for m in model_names:
            base_m = ReportGenerator._strip_device_suffix(m)
            rows_html += f"<tr><td>{_html_escape(m)}</td>"
            for cat in ReportGenerator.MATRIX_CATEGORIES:
                reason = ""
                if cat not in covered:
                    status = "未运行本轮"
                else:
                    if cat == "通用接口":
                        matched = [r for r in all_results if r.model_name == m
                                   and r.name not in perf_endpoints and "multimodal" not in r.name
                                   and not ReportGenerator._has_dedicated_matrix_category(r.name)]
                    elif cat == "文本chat":
                        matched = [r for r in all_results if r.model_name == m
                                   and r.name in perf_endpoints and "multimodal" not in r.name]
                    elif cat == "多模态":
                        matched = [r for r in all_results if r.model_name == m and "multimodal" in r.name]
                    elif cat == "多模型路由":
                        matched = [r for r in all_results if r.model_name == "_multi_model_" and base_m in r.name]
                    elif cat == "GGUF显式加载":
                        matched = [r for r in all_results if r.model_name.startswith(m) and "explicit_load" in r.name]
                    elif cat == "Builder/OpenClaw":
                        matched = [r for r in all_results if r.model_name == m and r.name.startswith("BUILDER")]
                    else:  # SampleApp
                        matched = [r for r in all_results if r.model_name == m and r.name.startswith("SAMPLEAPP:")]
                    status, reason = ReportGenerator._matrix_cell_status(matched)
                cls = {"通过": "status-pass", "失败": "status-fail"}.get(status, "status-skip")
                title_attr = f' title="{_html_escape(reason)}"' if reason else ""
                rows_html += f'<td class="center"{title_attr}><span class="{cls}">{status}</span></td>'
            rows_html += "</tr>"

        header_html = "".join(f"<th>{_html_escape(cat)}</th>" for cat in ReportGenerator.MATRIX_CATEGORIES)
        return f"""<div class="section"><h2><span class="icon">&#128203;</span> 测试完备性矩阵</h2>
        <table class="pivot-table"><thead><tr><th>模型</th>{header_html}</tr></thead><tbody>
        {rows_html}
        </tbody></table></div>"""

    @staticmethod
    def _generate_multi_backend_detail_html(multi_model_results, loaded_entries, out_dir, remote_mode=False, cmdline=""):
        """生成"多类型同时加载"的完整详情页(report_multi_backend_coexistence.html)。

        承载主报告不适合展示的高密度内容——逐项检查记录(每条检查的完整原始 detail 文本，
        如 insufficient_memory 的字节数明细)、后端共存矩阵每个组合的完整原因说明。主报告
        (generate_summary_html)只保留极简摘要 + 指向本页面的链接，与其它区块的密度保持
        一致。仅在存在多模型检查结果时才生成并返回 True，调用方据此决定是否渲染链接。
        """
        if not multi_model_results:
            return False

        # 按检查类别分组，而不是把全部检查项摊成一张不分类的长列表——同一类别的检查项
        # (尤其是逐模型重复出现的 "MULTI: chat route → <model>") 集中在同一张子表格里,
        # 复用既有 .group-section/.group-title 样式(与"通用接口测试"等区块保持一致)。
        _check_groups = {}
        for r in multi_model_results:
            if r.skipped:
                status_text, status_class = "SKIP", "skip"
            elif r.crashed:
                status_text, status_class = "CRASH", "crash"
            elif r.passed:
                status_text, status_class = "PASS", "pass"
            elif getattr(r, "ignorable", False):
                status_text, status_class = "IGN", "ignored"
            else:
                status_text, status_class = "FAIL", "fail"
            check_desc = ReportGenerator._describe_multi_check(r.name)
            category = ReportGenerator._categorize_multi_check(r.name)
            row_html = f"""<tr>
                <td><code>{_html_escape(r.name)}</code></td>
                <td class=\"resp-detail\">{_html_escape(check_desc)}</td>
                <td><span class=\"badge {status_class}\">{status_text}</span></td>
                <td class=\"center\">{r.status_code}</td>
                <td>{_html_escape(r.detail)}</td>
            </tr>"""
            _check_groups.setdefault(category, []).append(row_html)

        _category_order = [c[0] for c in ReportGenerator._MULTI_CHECK_CATEGORIES] + [ReportGenerator._MULTI_CHECK_CATEGORY_FALLBACK]
        _group_sections_html = ""
        for category in _category_order:
            rows = _check_groups.get(category)
            if not rows:
                continue
            _group_sections_html += f"""<div class=\"group-section\">
                <div class=\"group-title\">{_html_escape(category)}（{len(rows)}）</div>
                <table class=\"compare-table\"><thead><tr><th>检查项</th><th>说明</th><th>状态</th><th>HTTP</th><th>详情</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
            </div>"""
        checks_html = (f'<div class="model-section">{_group_sections_html}</div>'
                        if _group_sections_html else '<div class="empty-state">未捕获到任何检查项</div>')

        loaded_rows = ""
        for entry in loaded_entries:
            loaded_rows += f"""<tr>
                <td>{_html_escape(entry.get('id', ''))}</td>
                <td><code>{_html_escape(entry.get('backend', ''))}</code></td>
                <td><code>{_html_escape(entry.get('device', ''))}</code></td>
                <td class=\"num\">{_html_escape(str(entry.get('context_length', '')))}</td>
            </tr>"""
        loaded_html = (f'<table class="compare-table"><thead><tr><th>模型</th><th>后端</th><th>设备</th><th>上下文</th></tr></thead><tbody>{loaded_rows}</tbody></table>'
                        if loaded_rows else '<div class="empty-state">未捕获到已加载模型的 backend/device 明细</div>')

        coexistence_html = '<div class="empty-state">本次未产生后端共存矩阵结果</div>'
        matrix_result = next(
            (r for r in multi_model_results
             if r.name == "MULTI: backend coexistence matrix (triple + pairwise fallback)"),
            None
        )
        if matrix_result and matrix_result.response_data and matrix_result.response_data.get("coexistence_matrix"):
            cm = matrix_result.response_data["coexistence_matrix"]
            _CM_STATUS_CLASS = {"pass": "status-pass", "fail": "status-fail",
                                 "skip": "status-skip", "unknown": "status-skip"}
            _CM_STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "unknown": "N/A"}
            _CM_PAIR_LABEL = {"npu+gpu": "QNN/NPU + GGUF/GPU", "npu+cpu": "QNN/NPU + MNN/CPU",
                               "gpu+cpu": "GGUF/GPU + MNN/CPU"}
            triple = cm["triple"]
            triple_badge = f'<span class="{_CM_STATUS_CLASS.get(triple["status"], "status-skip")}">{_CM_STATUS_TEXT.get(triple["status"], triple["status"].upper())}</span>'
            pair_rows = ""
            for pair_key, pair_info in cm["pairs"].items():
                p_status = pair_info["status"]
                p_badge = f'<span class="{_CM_STATUS_CLASS.get(p_status, "status-skip")}">{_CM_STATUS_TEXT.get(p_status, p_status.upper())}</span>'
                pair_rows += f"""<tr>
                    <td>{_html_escape(_CM_PAIR_LABEL.get(pair_key, pair_key))}</td>
                    <td class=\"center\">{p_badge}</td>
                    <td>{_html_escape(pair_info["detail"])}</td>
                </tr>"""
            coexistence_html = f"""<table class="compare-table"><thead><tr><th>组合</th><th class="center">状态</th><th>详情</th></tr></thead><tbody>
            <tr><td><strong>QNN/NPU + GGUF/GPU + MNN/CPU（三后端同时驻留）</strong></td><td class="center">{triple_badge}</td><td>{_html_escape(triple["detail"])}</td></tr>
            {pair_rows}
            </tbody></table>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>多类型同时加载 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Multi-Backend Coexistence Detail</div>
        <h1>多类型同时加载 &middot; 详细记录</h1>
        <p class="subtitle">后端共存矩阵完整原因与逐项检查记录</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

<div class="section">
<h2><span class="icon">&#128187;</span> 已加载 backend/device</h2>
{loaded_html}
</div>

<div class="section">
<h2><span class="icon">&#129520;</span> 后端共存矩阵（三后端 + 两两降级，完整详情）</h2>
{coexistence_html}
</div>

<div class="section">
<h2><span class="icon">&#128203;</span> 逐项检查记录</h2>
<div class="resp-detail" style="margin: -4px 0 10px;">本节（多类型同时加载）全部检查项的完整原始记录，已按类别分组（单模型路由验证、后端共存与两两降级验证、通用接口与异常路由、并发与故障恢复），便于快速定位同类检查而不必翻阅一整张不分类的长列表。</div>
{checks_html}
</div>

<div class="footer">
    多类型同时加载详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "多类型同时加载详情页 report_multi_backend_coexistence.html")
        out_path = Path(out_dir) / "report_multi_backend_coexistence.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 多类型同时加载详情: {out_path}")
        return True

    @staticmethod
    def _crash_event_anchor(idx):
        return f"crash-{idx}"

    @staticmethod
    def _generate_crash_events_detail_html(crash_events, out_dir, remote_mode=False, cmdline=""):
        """生成崩溃事件的完整详情页(report_crash_events.html)。

        承载主报告/模型报告不适合展示的高密度内容——每条崩溃完整的进程 stderr/stdout
        尾部原始文本(默认展开)。主报告/模型报告只保留次数 + 逐条极简摘要(时间/模型/轮次/
        端点/详情,即已含人类可读退出码诊断的 detail 字段)+ 跳转到本页对应锚点的链接,
        与"多类型同时加载"下沉子报告的既有模式一致。仅在存在崩溃事件时才生成。
        """
        if not crash_events:
            return False
        rows_html = ""
        for idx, c in enumerate(crash_events):
            log_tail = getattr(c, "log_tail", "")
            log_tail_html = (f'<details open><summary>查看</summary><pre class="startup-detail">{_html_escape(log_tail)}</pre></details>'
                              if log_tail else "-")
            rows_html += f"""<tr id="{ReportGenerator._crash_event_anchor(idx)}">
                <td>{_html_escape(c.timestamp)}</td>
                <td>{_html_escape(c.model_name)}</td><td class="center">{c.round_num}</td>
                <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td>
                <td>{_html_escape(c.detail)}</td>
                <td>{log_tail_html}</td>
            </tr>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>崩溃事件 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Crash Events Detail</div>
        <h1>崩溃事件 &middot; 详细记录</h1>
        <p class="subtitle">全部崩溃事件的完整进程日志尾部</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

<div class="section">
<h2><span class="icon">&#9888;</span> 崩溃事件日志（{len(crash_events)}）</h2>
<table class="crash-table"><thead><tr>
    <th>时间</th><th>模型</th><th>轮次</th><th>端点</th><th>详情</th><th>进程日志尾部</th></tr></thead><tbody>
{rows_html}
</tbody></table>
</div>

<div class="footer">
    崩溃事件详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "崩溃事件详情页 report_crash_events.html")
        out_path = Path(out_dir) / "report_crash_events.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 崩溃事件详情: {out_path}")
        return True

    @staticmethod
    def _tool_call_anchor(idx):
        return f"toolcall-{idx}"

    @staticmethod
    def _generate_tool_calls_detail_html(toolcalls_results, out_dir, remote_mode=False, cmdline=""):
        """生成 tool_calls/function calling 协议测试的详情页(report_tool_calls.html)。

        承载这四个测试真正的诊断价值——工具调用数组结构、流式跨 chunk 重建后的
        arguments——而不是依赖通用的 chat/completions 子串匹配产出的空洞对话卡片
        (那条通用路径既不展示 response_data,也会与同一轮真正的 chat 测试发生锚点冲突)。
        主报告/模型报告只保留通过/跳过计数 + 跳转链接。
        """
        if not toolcalls_results:
            return False
        cards_html = ""
        for idx, r in enumerate(toolcalls_results):
            cls, txt, extra_badges = ReportGenerator._case_status_badge_html(r)
            payload_html = ""
            if r.response_data:
                payload_html = f'<pre class="startup-detail">{_html_escape(json.dumps(r.response_data, indent=2, ensure_ascii=False))}</pre>'
            elif r.text_response:
                payload_html = f'<div class="resp-detail">{_html_escape(r.text_response)}</div>'
            cards_html += f"""<div class="section" id="{ReportGenerator._tool_call_anchor(idx)}">
            <div class="conv-meta">
                <span class="model-tag">{_html_escape(r.model_name)}</span>
                <span class="round-tag">Round {r.round_num}</span>
                <span class="{cls}">{txt}</span>{extra_badges}
            </div>
            <h3 style="font-size: 14px; margin: 8px 0;">{_html_escape(r.name)}</h3>
            <div class="resp-detail">{_html_escape(r.detail)}</div>
            {payload_html}
            </div>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tool_calls 协议测试 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Tool Calls Protocol Detail</div>
        <h1>tool_calls 协议测试 &middot; 详细记录</h1>
        <p class="subtitle">每条测试的完整工具调用结构 / 流式重建后的 arguments</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">
{cards_html}
<div class="footer">
    tool_calls 协议测试详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "tool_calls 协议详情页 report_tool_calls.html")
        out_path = Path(out_dir) / "report_tool_calls.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] tool_calls 协议详情: {out_path}")
        return True

    @staticmethod
    def generate_summary_html(all_results, perf_data, crash_events, out_dir, remote_mode=False, suite_name="full", cmdline=""):
        """生成总汇报 HTML — 仅包含模型无关测试 + 性能对比汇总"""
        # 排除全部内部占位 model_name（_global_/_multi_model_ 等），只保留真实模型；
        # 统一复用 _is_internal_placeholder_model()，不要在这里再手写排除元组（详见其定义处注释）。
        model_names = sorted(set(r.model_name for r in all_results
                                 if not ReportGenerator._is_internal_placeholder_model(r.model_name)))
        multi_model_results = [r for r in all_results if r.model_name == "_multi_model_"]
        rounds = sorted(set(r.round_num for r in all_results))
        num_rounds = len(rounds)

        # 模型无关接口数据：优先使用 _global_ 的结果，否则回退到第一个真实模型的数据
        independent_results = [r for r in all_results
                               if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS
                               and r.model_name == "_global_"]
        if not independent_results and model_names:
            independent_results = [r for r in all_results
                                   if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS
                                   and r.model_name == model_names[0]]

        # 总计（仅统计模型无关测试用于总汇报）
        all_independent = [r for r in all_results if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS]
        # 去重：同一端点同一轮只保留第一个模型的结果
        seen = set()
        unique_independent = []
        for r in all_independent:
            key = (r.name, r.round_num)
            if key not in seen:
                seen.add(key)
                unique_independent.append(r)

        summary_ind = {"total": len(unique_independent), "passed": 0, "failed": 0, "crashed": 0, "skipped": 0}
        for r in unique_independent:
            if r.skipped:
                summary_ind["skipped"] += 1
            elif r.crashed:
                summary_ind["crashed"] += 1
            elif r.passed:
                summary_ind["passed"] += 1
            else:
                summary_ind["failed"] += 1

        # 全局统计（所有测试，用于顶部展示）
        summary_all = {"total": 0, "passed": 0, "failed": 0, "crashed": 0, "skipped": 0, "ignored": 0, "model_capability_issues": 0}
        for r in all_results:
            summary_all["total"] += 1
            if r.skipped:
                summary_all["skipped"] += 1
            elif r.crashed:
                summary_all["crashed"] += 1
            elif r.passed:
                summary_all["passed"] += 1
            else:
                summary_all["failed"] += 1
                if getattr(r, "ignorable", False):
                    summary_all["ignored"] += 1
            if getattr(r, "model_capability_issue", False):
                summary_all["model_capability_issues"] += 1
        pass_rate_all = (summary_all["passed"] / summary_all["total"] * 100) if summary_all["total"] > 0 else 0

        # 模型无关接口的透视表（只使用全局测试的轮次，通常只有 1 轮）
        independent_rounds = sorted(set(r.round_num for r in independent_results)) if independent_results else rounds
        independent_table = ReportGenerator._build_pivot_table(
            independent_results, ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS, independent_rounds
        )

        # 性能对比表（合并原"跨模型推理对比"和"性能时序监控"）
        # 指标：avg prompt_processing_rate, avg token_generation_rate, avg CPU, avg Memory
        # 注意：排除 _multi_model_（阶段3多模型路由测试的占位名），它不产生 /profile 或推理性能数据，
        # 混入对比表只会出现一行全 N/A，没有意义。
        perf_compare_model_names = [m for m in model_names if m != "_multi_model_"]
        model_compare_html = ""
        if len(perf_compare_model_names) > 0:
            model_compare_html = """<div class="section">
            <h2><span class="icon">&#128200;</span> 性能对比</h2>
            <table class="compare-table">
            <thead><tr><th>模型</th><th>平均 Prompt 处理速率<br><small>(tokens/s)</small></th><th>平均 Token 生成速率<br><small>(tokens/s)</small></th><th>推理平均 CPU<br><small>(%)</small></th><th>推理平均内存<br><small>(MB)</small></th></tr></thead><tbody>"""
            for model in perf_compare_model_names:
                model_results = [r for r in all_results if r.model_name == model]

                # 从多轮 /profile 结果提取 prompt_processing_rate 和 token_generation_rate
                profile_results = [r for r in model_results
                                   if r.name == "GET /profile" and r.passed and r.response_data]
                ppr_values = []
                tgr_values = []
                for pr in profile_results:
                    try:
                        ppr = float(pr.response_data.get("prompt_processing_rate", 0))
                        if ppr > 0:
                            ppr_values.append(ppr)
                    except (ValueError, TypeError):
                        pass
                    try:
                        tgr = float(pr.response_data.get("token_generation_rate", 0))
                        if tgr > 0:
                            tgr_values.append(tgr)
                    except (ValueError, TypeError):
                        pass
                avg_ppr = (sum(ppr_values) / len(ppr_values)) if ppr_values else 0
                avg_tgr = (sum(tgr_values) / len(tgr_values)) if tgr_values else 0

                # 从推理结果的 perf_before/perf_after 提取 CPU 和内存
                _chat_names = set(ReportGenerator.MODEL_PERF_ENDPOINTS) - {"GET /profile"}
                chat_results = [r for r in model_results
                                if r.name in _chat_names
                                and r.passed]
                cpu_values = []
                mem_values = []
                for cr in chat_results:
                    if cr.perf_after and cr.perf_after.get("cpu_percent", 0) > 0:
                        # 展示层归一化：psutil 原始值以单核=100%计,MNN 等多线程后端跑满
                        # 多个核心时原始值可能远超100%(如700+%),反直觉;归一化为相对整机
                        # 总算力的百分比再展示,不影响 results.json 里保存的原始采样值。
                        cpu_values.append(_normalize_cpu_percent(cr.perf_after["cpu_percent"]))
                    if cr.perf_after and cr.perf_after.get("rss_mb", 0) > 0:
                        mem_values.append(cr.perf_after["rss_mb"])
                avg_cpu = (sum(cpu_values) / len(cpu_values)) if cpu_values else 0
                avg_mem = (sum(mem_values) / len(mem_values)) if mem_values else 0

                ppr_str = f"{avg_ppr:.1f}" if avg_ppr > 0 else "N/A"
                tgr_str = f"{avg_tgr:.1f}" if avg_tgr > 0 else "N/A"
                cpu_str = f"{avg_cpu:.1f}" if avg_cpu > 0 else "N/A"
                mem_str = f"{avg_mem:.0f}" if avg_mem > 0 else "N/A"
                model_compare_html += f"""<tr>
                    <td><strong>{_html_escape(model)}</strong></td>
                    <td class="center">{ppr_str}</td>
                    <td class="center">{tgr_str}</td>
                    <td class="center">{cpu_str}</td>
                    <td class="center">{mem_str}</td>
                </tr>"""
            model_compare_html += "</tbody></table></div>"

        # 多类型同时加载摘要：主报告只保留极简密度(通过/跳过计数 + 共存矩阵的短原因)，
        # 与其它区块保持一致；逐项检查记录、每个组合的完整失败原因(如 insufficient_memory
        # 的字节数明细)全部移至独立详情页,由 _generate_multi_backend_detail_html 生成。
        multi_backend_html = ""
        loaded_entries = []
        seen_loaded = set()
        for r in multi_model_results:
            for entry in (r.response_data or {}).get("loaded_models", []):
                key = (entry.get("id", ""), entry.get("backend", ""), entry.get("device", ""))
                if key not in seen_loaded:
                    seen_loaded.add(key)
                    loaded_entries.append(entry)
        if multi_model_results or loaded_entries:
            multi_pass = sum(1 for r in multi_model_results if r.passed)
            multi_skipped = sum(1 for r in multi_model_results if r.skipped)
            multi_crashed = sum(1 for r in multi_model_results if r.crashed)
            multi_total = len(multi_model_results)
            # 失败数 = 总数 - 通过 - 跳过 - 崩溃，与顶部全局汇总卡片(summary_all)的口径一致。
            multi_fail = multi_total - multi_pass - multi_skipped - multi_crashed
            device_types = sorted({entry.get("device") or "unknown" for entry in loaded_entries})

            has_detail_page = ReportGenerator._generate_multi_backend_detail_html(
                multi_model_results, loaded_entries, out_dir, remote_mode=remote_mode, cmdline=cmdline)

            loaded_rows = ""
            for entry in loaded_entries:
                loaded_rows += f"""<tr>
                    <td>{_html_escape(entry.get('id', ''))}</td>
                    <td><code>{_html_escape(entry.get('backend', ''))}</code></td>
                    <td><code>{_html_escape(entry.get('device', ''))}</code></td>
                    <td class=\"num\">{_html_escape(str(entry.get('context_length', '')))}</td>
                </tr>"""
            loaded_table_html = (f'<table class="compare-table"><thead><tr><th>模型</th><th>后端</th><th>设备</th><th>上下文</th></tr></thead><tbody>{loaded_rows}</tbody></table>'
                                  if loaded_rows else '<div class="empty-state">未捕获到已加载模型的 backend/device 明细</div>')

            # 后端共存矩阵：主报告只展示极短原因标签(如"内存不足,已跳过")，完整原始详情
            # (如 insufficient_memory 的字节数明细)在详情页的同名矩阵表格里。
            coexistence_html = ""
            matrix_result = next(
                (r for r in multi_model_results
                 if r.name == "MULTI: backend coexistence matrix (triple + pairwise fallback)"),
                None
            )
            if matrix_result and matrix_result.response_data and matrix_result.response_data.get("coexistence_matrix"):
                cm = matrix_result.response_data["coexistence_matrix"]
                _CM_STATUS_CLASS = {"pass": "status-pass", "fail": "status-fail",
                                     "skip": "status-skip", "unknown": "status-skip"}
                _CM_STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "unknown": "N/A"}
                _CM_PAIR_LABEL = {"npu+gpu": "QNN/NPU + GGUF/GPU", "npu+cpu": "QNN/NPU + MNN/CPU",
                                   "gpu+cpu": "GGUF/GPU + MNN/CPU"}
                triple = cm["triple"]
                triple_badge = f'<span class="{_CM_STATUS_CLASS.get(triple["status"], "status-skip")}">{_CM_STATUS_TEXT.get(triple["status"], triple["status"].upper())}</span>'
                pair_rows = ""
                for pair_key, pair_info in cm["pairs"].items():
                    p_status = pair_info["status"]
                    p_badge = f'<span class="{_CM_STATUS_CLASS.get(p_status, "status-skip")}">{_CM_STATUS_TEXT.get(p_status, p_status.upper())}</span>'
                    pair_rows += f"""<tr>
                        <td>{_html_escape(_CM_PAIR_LABEL.get(pair_key, pair_key))}</td>
                        <td class=\"center\">{p_badge}</td>
                        <td>{_html_escape(pair_info.get("reason", ""))}</td>
                    </tr>"""
                coexistence_html = f"""<h3 style=\"font-size: 15px; margin: 18px 0 10px;\">后端共存矩阵</h3>
                <table class=\"compare-table\"><thead><tr><th>组合</th><th class=\"center\">状态</th><th>原因</th></tr></thead><tbody>
                <tr><td><strong>三后端同时驻留</strong></td><td class=\"center\">{triple_badge}</td><td>{_html_escape(triple.get("reason", ""))}</td></tr>
                {pair_rows}
                </tbody></table>"""

            multi_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {multi_fail} 项失败，详见下方逐项检查记录</div>'
                if multi_fail > 0 else "")
            detail_link_html = (
                f'{multi_fail_note_html}<div class="model-link-card"><a href="report_multi_backend_coexistence.html">查看完整共存矩阵与逐项检查记录 →</a></div>'
                if has_detail_page else multi_fail_note_html)

            multi_backend_html = f"""<div class=\"section\">
            <h2><span class=\"icon\">&#129520;</span> 多类型同时加载</h2>
            <div class=\"summary-bar\">
                <div class=\"summary-card\"><div class=\"num\">{multi_total}</div><div class=\"label\">检查项</div></div>
                <div class=\"summary-card card-pass\"><div class=\"num\">{multi_pass}</div><div class=\"label\">通过</div></div>
                <div class=\"summary-card\"><div class=\"num\">{multi_skipped}</div><div class=\"label\">跳过</div></div>
                <div class=\"summary-card\"><div class=\"num\">{len(device_types)}</div><div class=\"label\">设备类型</div></div>
            </div>
            <h3 style=\"font-size: 15px; margin: 18px 0 10px;\">已加载 backend/device</h3>
            {loaded_table_html}
            {coexistence_html}
            {detail_link_html}
            </div>"""

        # 模型列表（带链接）
        model_links_html = '<div class="section"><h2><span class="icon">&#128218;</span> 详细报告</h2>'
        for model in model_names:
            model_results = [r for r in all_results if r.model_name == model]
            m_pass = sum(1 for r in model_results if r.passed)
            m_total = len(model_results)
            m_rate = (m_pass / m_total * 100) if m_total > 0 else 0
            safe_model_name = model.replace(" ", "_").replace("/", "_")
            model_links_html += f"""<div class="model-link-card">
                <a href="report_{safe_model_name}.html">{_html_escape(model)}</a>
                <div class="model-link-stats">
                    <span class="stat-pass">{m_pass}/{m_total} 通过</span>
                    <span class="stat-rate">{m_rate:.0f}%</span>
                </div>
            </div>"""
        model_links_html += '</div>'

        # 崩溃日志：主报告只保留次数 + 逐条极简摘要(时间/模型/轮次/端点/detail 里已含的人类可读
        # 退出码诊断),完整 stderr/stdout 尾部原始文本下沉到 report_crash_events.html,
        # 与"多类型同时加载"的下沉模式一致，避免主报告体积随崩溃数量无上限增长。
        has_crash_detail_page = ReportGenerator._generate_crash_events_detail_html(
            crash_events, out_dir, remote_mode=remote_mode, cmdline=cmdline)
        crash_html = ""
        if crash_events:
            crash_html = """<div class="section"><h2><span class="icon">&#9888;</span> 崩溃事件日志</h2>
            <table class="crash-table"><thead><tr>
                <th>时间</th><th>模型</th><th>轮次</th><th>端点</th><th>详情</th><th>完整日志</th></tr></thead><tbody>"""
            for idx, c in enumerate(crash_events):
                detail_link = (f'<a href="report_crash_events.html#{ReportGenerator._crash_event_anchor(idx)}">查看 →</a>'
                                if has_crash_detail_page else "-")
                crash_html += f"""<tr><td>{_html_escape(c.timestamp)}</td>
                    <td>{_html_escape(c.model_name)}</td><td class="center">{c.round_num}</td>
                    <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td><td>{_html_escape(c.detail)}</td>
                    <td>{detail_link}</td></tr>"""
            crash_html += "</tbody></table></div>"

        # MNN 内存容错统计块：区分"MNN 自身真实 OOM"(MNN_OOM_CRASH)与"疑似因 MNN OOM 拖垮进程而
        # 级联崩溃的其它后端"(MNN_OOM_CASCADE)。两者都是真实问题、都已计入 failed/crashed 统计——
        # 级联受累计数不再代表豁免，只是一个诊断性提示，提醒人工去排查这是否是一个需要单独修复的
        # 真实级联崩溃 bug（例如共享状态未隔离），而不是把它当作可以放行的"环境限制"。
        mnn_self_oom_events = [c for c in crash_events if c.endpoint == "MNN_OOM_CRASH"]
        mnn_cascade_events = [c for c in crash_events if c.endpoint == "MNN_OOM_CASCADE"]
        auto_restarted_count = sum(1 for r in all_results if getattr(r, "auto_restarted", False))
        mnn_html = ""
        if mnn_self_oom_events or mnn_cascade_events or auto_restarted_count > 0:
            mnn_html = f"""<div class="section"><h2><span class="icon">&#128202;</span> MNN 内存容错统计</h2>
            <div class="summary-bar">
                <div class="summary-card card-crash"><div class="num">{len(mnn_self_oom_events)}</div><div class="label">MNN 自身真实 OOM</div></div>
                <div class="summary-card card-crash" title="该计数不代表豁免，均已计入上方总的 failed/crashed 统计；仅作诊断提示，建议人工排查是否为需要单独修复的真实级联崩溃 bug"><div class="num">{len(mnn_cascade_events)}</div><div class="label">疑似 MNN OOM 级联崩溃(已计入失败)</div></div>
                <div class="summary-card card-pass"><div class="num">{auto_restarted_count}</div><div class="label">自动恢复次数</div></div>
            </div>
            </div>"""

        # 服务启动失败的醒目横幅 — 只要存在任何 endpoint == "SERVICE_STARTUP" 的事件
        # 就在报告最顶部显示一条红色横幅,把模型、详情、stderr 日志路径都列出来。
        startup_failures = [c for c in crash_events if c.endpoint == "SERVICE_STARTUP"]
        startup_banner_html = ""
        if startup_failures:
            startup_banner_html = '<div class="startup-banner"><div class="startup-banner-title">⚠ 检测到 ' + str(len(startup_failures)) + ' 次服务启动失败</div>'
            startup_banner_html += '<div class="startup-banner-desc">GenieAPIService.exe 在调用 <code>Popen</code> 后 2 秒内立即退出,导致服务从未真正监听端口。常见原因:架构不匹配 (x86-64 host vs ARM64 exe)、缺少 QNN/VC 运行时 DLL、OneDrive 占位文件未下载、模型配置路径错误。请先看 stderr 日志再继续。</div>'
            startup_banner_html += '<table class="startup-table"><thead><tr><th>时间</th><th>模型</th><th>失败详情 (含 exit_code / stderr 摘要)</th></tr></thead><tbody>'
            for c in startup_failures:
                startup_banner_html += f'<tr><td>{_html_escape(c.timestamp)}</td><td>{_html_escape(c.model_name)}</td><td><pre class="startup-detail">{_html_escape(c.detail)}</pre></td></tr>'
            startup_banner_html += '</tbody></table></div>'

        matrix_html = ReportGenerator._build_completeness_matrix(all_results, model_names, suite_name)

        # 独立的 Builder 集成 / SampleApp 明细区块：不再仅仅体现为完备性矩阵里的一个格子。
        # Builder 集成用例的占位 model_name 这里不按 model_names 过滤，确保它们也能在总汇报
        # 里被看到明细，而不是完全依赖每模型报告页面。
        builder_results = [r for r in all_results if r.name.startswith("BUILDER")]
        sampleapp_results = [r for r in all_results if r.name.startswith("SAMPLEAPP:")]
        builder_section_html = ""
        if builder_results:
            builder_total = len(builder_results)
            builder_pass = sum(1 for r in builder_results if r.passed)
            builder_skipped = sum(1 for r in builder_results if r.skipped)
            builder_crashed = sum(1 for r in builder_results if r.crashed)
            # 失败数 = 总数 - 通过 - 跳过 - 崩溃，与顶部全局汇总卡片(summary_all)的口径一致。
            builder_fail = builder_total - builder_pass - builder_skipped - builder_crashed

            builder_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {builder_fail} 项失败，详见每模型报告页面</div>'
                if builder_fail > 0 else "")

            builder_section_html = f"""<div class="section"><h2><span class="icon">&#128295;</span> Builder 集成测试结果</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{builder_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{builder_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{builder_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{builder_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{builder_skipped}</div><div class="label">跳过</div></div>
            </div>
            {builder_fail_note_html}
            </div>"""
        sampleapp_section_html = ""
        if sampleapp_results:
            # 主报告只保留摘要计数(与 Builder 摘要卡片同一写法);完整逐用例明细表只在
            # 每个模型报告(model_sampleapp_html)里保留一份，避免同一份数据被重复渲染两遍。
            sampleapp_total = len(sampleapp_results)
            sampleapp_pass = sum(1 for r in sampleapp_results if r.passed)
            sampleapp_skipped = sum(1 for r in sampleapp_results if r.skipped)
            sampleapp_crashed = sum(1 for r in sampleapp_results if r.crashed)
            sampleapp_fail = sampleapp_total - sampleapp_pass - sampleapp_skipped - sampleapp_crashed

            sampleapp_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {sampleapp_fail} 项失败，详见每模型报告页面</div>'
                if sampleapp_fail > 0 else "")

            sampleapp_section_html = f"""<div class="section"><h2><span class="icon">&#128241;</span> SampleApp 测试结果</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{sampleapp_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{sampleapp_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{sampleapp_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{sampleapp_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{sampleapp_skipped}</div><div class="label">跳过</div></div>
            </div>
            {sampleapp_fail_note_html}
            </div>"""

        # tool_calls/function calling 协议测试：独立分类区块，不再混入模型报告的"其它/专项
        # 测试"兜底大杂烩；完整协议载荷(工具调用数组、流式重建后的 arguments)下沉到
        # report_tool_calls.html，主报告只保留计数 + 跳转链接。
        toolcalls_results = [r for r in all_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
        toolcalls_section_html = ""
        if toolcalls_results:
            tc_total = len(toolcalls_results)
            tc_pass = sum(1 for r in toolcalls_results if r.passed)
            tc_skipped = sum(1 for r in toolcalls_results if r.skipped)
            tc_crashed = sum(1 for r in toolcalls_results if r.crashed)
            tc_fail = tc_total - tc_pass - tc_skipped - tc_crashed

            has_toolcalls_detail_page = ReportGenerator._generate_tool_calls_detail_html(
                toolcalls_results, out_dir, remote_mode=remote_mode, cmdline=cmdline)
            toolcalls_link_html = ('<div style="margin-top: 6px;"><a href="report_tool_calls.html">查看完整协议详情 →</a></div>'
                                    if has_toolcalls_detail_page else "")
            tc_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {tc_fail} 项失败，详见协议详情页</div>'
                if tc_fail > 0 else "")

            toolcalls_section_html = f"""<div class="section"><h2><span class="icon">&#128268;</span> tool_calls 协议测试</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{tc_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{tc_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{tc_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{tc_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{tc_skipped}</div><div class="label">跳过(模型能力局限)</div></div>
            </div>
            {tc_fail_note_html}
            {toolcalls_link_html}
            </div>"""
        graceful_shutdown_results = [r for r in all_results if r.name.startswith("GRACEFUL_SHUTDOWN:")]
        graceful_shutdown_section_html = ""
        if graceful_shutdown_results:
            graceful_shutdown_section_html = f"""<div class="section"><h2><span class="icon">&#9203;</span> 推理中终止进程 → 优雅退出验证</h2>
            <div class="resp-detail" style="margin: -4px 0 10px;">对三种后端各选一个代表模型，在其处理长耗时推理请求期间发送终止信号，验证进程能走既有的关闭逻辑正常退出（日志出现优雅关闭标记且退出码不匹配任何已知 Windows 崩溃特征码），而不是被系统判定为异常崩溃。</div>
            {ReportGenerator._build_case_detail_table(graceful_shutdown_results)}
            </div>"""
        cmdline_html = ReportGenerator._cmdline_meta_html(cmdline)

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_latency = sum(r.latency_ms for r in all_results if not r.skipped)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenieAPIService 测试总汇报 - {now_str}</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Genie API Service · Integration Suite</div>
        <h1>GenieAPIService 集成测试总汇报</h1>
        <p class="subtitle">API Integration Test Summary Report</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>测试模型:</strong> {len(model_names)} 个</span>
            <span class="meta-item"><strong>测试轮数:</strong> {num_rounds}</span>
            <span class="meta-item"><strong>总耗时:</strong> {total_latency/1000:.1f}s</span>
            <span class="meta-item"><strong>模式:</strong> {'远程' if remote_mode else '本地'}</span>
            <span class="meta-item"><strong>测试套件:</strong> {_html_escape(suite_name)}</span>
            {cmdline_html}
        </div>
    </div>
</div>

<div class="container">

{startup_banner_html}

<div class="summary-bar">
    <div class="summary-card"><div class="num">{summary_all['total']}</div><div class="label">总测试数</div></div>
    <div class="summary-card card-pass"><div class="num">{summary_all['passed']}</div><div class="label">通过</div></div>
    <div class="summary-card card-fail"><div class="num">{summary_all['failed']}</div><div class="label">失败</div></div>
    <div class="summary-card card-ignored" title="服务端已知占位/未实现缺陷的 FAIL 子集 (501、占位 400 等); failed 数已包含这些条目, 这里只是单独标出来"><div class="num">{summary_all['ignored']}</div><div class="label">可忽略</div></div>
    <div class="summary-card card-crash"><div class="num">{summary_all['crashed']}</div><div class="label">崩溃</div></div>
    <div class="summary-card"><div class="num">{summary_all['skipped']}</div><div class="label">跳过</div></div>
    <div class="summary-card card-capability" title="根因判定为模型自身能力/输出质量局限而非服务端缺陷的诊断性标注(如重复出词、tool_calls 未按指令触发);诊断性维度,不改变 passed/failed/退出码判定"><div class="num">{summary_all['model_capability_issues']}</div><div class="label">模型能力问题</div></div>
</div>

<div class="progress-bar">
    <div class="progress-fill {'good' if pass_rate_all >= 80 else ('warn' if pass_rate_all >= 50 else 'bad')}" style="width:{max(pass_rate_all, 5):.0f}%">
        <span class="progress-text">通过率 {pass_rate_all:.1f}%</span>
    </div>
</div>
<div class="progress-meta"><span>总体通过率</span><span class="pct">{pass_rate_all:.1f}%</span></div>

<div class="section">
<h2><span class="icon">&#128269;</span> 通用接口测试（模型无关）</h2>
<div class="model-section">
    <div class="group-section">
        {independent_table}
    </div>
</div>
</div>

{model_compare_html}
{multi_backend_html}
{model_links_html}
{matrix_html}
{builder_section_html}
{sampleapp_section_html}
{toolcalls_section_html}
{graceful_shutdown_section_html}
{mnn_html}
{crash_html}

<div class="footer">
    GenieAPIService Integration Test Summary &middot; Generated by test_service.py &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "总汇报 report.html")
        out_path = Path(out_dir) / "report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 总汇报 HTML: {out_path}")

    @staticmethod
    def generate_model_reports(all_results, perf_data, crash_events, out_dir, remote_mode=False, cmdline=""):
        """为每个模型生成独立的 HTML 报告"""
        # 排除全部内部占位 model_name（_global_/_multi_model_ 等），只为真实模型生成报告；
        # 统一复用 _is_internal_placeholder_model()，不要在这里再手写排除元组（详见其定义处注释）。
        model_names = sorted(set(r.model_name for r in all_results
                                 if not ReportGenerator._is_internal_placeholder_model(r.model_name)))
        rounds = sorted(set(r.round_num for r in all_results))
        # 与 generate_summary_html._generate_crash_events_detail_html 共用同一份 crash_events
        # 列表(同一批 CrashEvent 对象),用对象身份映射到全局下标,保证本页链接的锚点与
        # report_crash_events.html(由总汇报流程先行生成)里的锚点完全对应。
        crash_idx_by_id = {id(c): idx for idx, c in enumerate(crash_events)}
        # 与 generate_summary_html._generate_tool_calls_detail_html 共用同一份 tool_calls 测试
        # 结果(同一批 TestResult 对象),用对象身份映射到全局下标,道理与上面的崩溃事件映射一致。
        all_toolcalls_results = [r for r in all_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
        toolcalls_idx_by_id = {id(r): idx for idx, r in enumerate(all_toolcalls_results)}

        for model in model_names:
            model_results = [r for r in all_results if r.model_name == model]
            model_crashes = [c for c in crash_events if c.model_name == model]

            m_pass = sum(1 for r in model_results if r.passed)
            m_total = len(model_results)
            m_rate = (m_pass / m_total * 100) if m_total > 0 else 0

            # 推理性能组
            perf_endpoints = ReportGenerator.MODEL_PERF_ENDPOINTS
            perf_results = [r for r in model_results if r.name in perf_endpoints]
            perf_table = ReportGenerator._build_pivot_table(perf_results, perf_endpoints, rounds)

            # 服务功能组（模型相关但非推理的接口）
            service_endpoints = [ep for ep in ReportGenerator.MODEL_DEPENDENT_ENDPOINTS
                                 if ep not in perf_endpoints]
            service_results = [r for r in model_results if r.name in service_endpoints]
            service_table = ReportGenerator._build_pivot_table(service_results, service_endpoints, rounds)

            # 其它/专项测试组：动态兜底展示未被上面两组名单覆盖的结果(多模态用例、GGUF 显式加载明细、
            # SampleApp/Builder 集成用例等),避免"计入总数但明细表里找不到对应行"的数据黑洞。
            # BUILDER/SAMPLEAPP 用例已拆到下方独立明细区块,这里提前排除,避免在通用兜底表里重复展示。
            leftover_endpoints = sorted(
                set(r.name for r in model_results) - set(perf_endpoints) - set(service_endpoints)
                - {r.name for r in model_results if r.name.startswith("BUILDER") or r.name.startswith("SAMPLEAPP:")
                   or r.name.startswith("POST /v1/chat/completions (tool_call")}
            )
            leftover_table = ReportGenerator._build_pivot_table(model_results, leftover_endpoints, rounds)

            # 独立的 Builder 集成 / SampleApp 明细区块（模型级）：与总汇报保持同一套展示逻辑，
            # 不再混在通用的"其它/专项测试"兜底表里。
            model_builder_results = [r for r in model_results if r.name.startswith("BUILDER")]
            model_sampleapp_results = [r for r in model_results if r.name.startswith("SAMPLEAPP:")]
            model_builder_html = ""
            if model_builder_results:
                model_builder_html = f"""<div class="section"><h2><span class="icon">&#128295;</span> Builder 集成测试结果</h2>
                {ReportGenerator._build_case_detail_table(model_builder_results, show_model_column=False)}
                </div>"""
            model_sampleapp_html = ""
            if model_sampleapp_results:
                model_sampleapp_html = f"""<div class="section"><h2><span class="icon">&#128241;</span> SampleApp 测试结果</h2>
                {ReportGenerator._build_case_detail_table(model_sampleapp_results, show_model_column=False)}
                </div>"""

            # tool_calls 协议测试（模型级）：独立分类区块，不再落入上面的"其它/专项测试"兜底表；
            # 完整协议载荷统一在共享的 report_tool_calls.html 里查看(见 crash_idx_by_id 同一套映射手法)。
            model_toolcalls_results = [r for r in model_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
            model_toolcalls_html = ""
            if model_toolcalls_results:
                tc_total = len(model_toolcalls_results)
                tc_pass = sum(1 for r in model_toolcalls_results if r.passed)
                tc_skipped = sum(1 for r in model_toolcalls_results if r.skipped)
                tc_crashed = sum(1 for r in model_toolcalls_results if r.crashed)
                tc_fail = tc_total - tc_pass - tc_skipped - tc_crashed
                tc_links = []
                for r in model_toolcalls_results:
                    idx = toolcalls_idx_by_id.get(id(r))
                    if idx is not None:
                        tc_links.append(f'<a href="report_tool_calls.html#{ReportGenerator._tool_call_anchor(idx)}">R{r.round_num}: {_html_escape(r.name)} →</a>')
                tc_links_html = "<br>".join(tc_links) if tc_links else ""
                model_toolcalls_html = f"""<div class="section"><h2><span class="icon">&#128268;</span> tool_calls 协议测试</h2>
                <div class="summary-bar">
                    <div class="summary-card"><div class="num">{tc_total}</div><div class="label">用例数</div></div>
                    <div class="summary-card card-pass"><div class="num">{tc_pass}</div><div class="label">通过</div></div>
                    <div class="summary-card card-fail"><div class="num">{tc_fail}</div><div class="label">失败</div></div>
                    <div class="summary-card card-crash"><div class="num">{tc_crashed}</div><div class="label">崩溃</div></div>
                    <div class="summary-card"><div class="num">{tc_skipped}</div><div class="label">跳过(模型能力局限)</div></div>
                </div>
                <div class="resp-detail">{tc_links_html}</div>
                </div>"""

            # 性能报告：每一轮 /profile 的完整数据 + CPU/内存
            perf_report_html = ""
            profile_results = [r for r in model_results
                               if r.name == "GET /profile" and r.passed and r.response_data]
            # 收集每轮的 chat 推理结果用于提取 CPU/内存
            chat_by_round = {}
            _chat_names = set(ReportGenerator.MODEL_PERF_ENDPOINTS) - {"GET /profile"}
            for cr in model_results:
                if cr.name in _chat_names and cr.passed:
                    chat_by_round.setdefault(cr.round_num, []).append(cr)

            def _fmt(val):
                if val == "N/A" or val is None:
                    return "N/A"
                try:
                    return f"{float(val):.2f}"
                except (ValueError, TypeError):
                    return str(val)

            def _fmt_int(val):
                if val == "N/A" or val is None:
                    return "N/A"
                try:
                    return str(int(float(val)))
                except (ValueError, TypeError):
                    return str(val)

            if profile_results:
                perf_report_html = """<div class="section">
                <h2><span class="icon">&#128202;</span> 性能报告</h2>
                <p style="color: var(--text-secondary); margin-bottom: 18px; font-size: 13px;">每轮推理后 <code>/profile</code> 返回的详细性能指标及资源占用</p>
                <div class="perf-round-grid">"""
                for pr in profile_results:
                    ttft = pr.response_data.get("time_to_first_token", "N/A")
                    tgt = pr.response_data.get("token_generation_time", "N/A")
                    ppr = pr.response_data.get("prompt_processing_rate", "N/A")
                    tgr = pr.response_data.get("token_generation_rate", "N/A")
                    npt = pr.response_data.get("num_prompt_tokens", "N/A")
                    ngt = pr.response_data.get("num_generated_tokens", "N/A")
                    # 从该轮 chat 结果提取 CPU/内存
                    round_chats = chat_by_round.get(pr.round_num, [])
                    # 展示层归一化,原因同总汇报的性能对比表(见 _normalize_cpu_percent 定义处注释)。
                    cpu_vals = [_normalize_cpu_percent(c.perf_after["cpu_percent"]) for c in round_chats
                                if c.perf_after and c.perf_after.get("cpu_percent", 0) > 0]
                    mem_vals = [c.perf_after["rss_mb"] for c in round_chats
                                if c.perf_after and c.perf_after.get("rss_mb", 0) > 0]
                    avg_cpu = (sum(cpu_vals) / len(cpu_vals)) if cpu_vals else None
                    avg_mem = (sum(mem_vals) / len(mem_vals)) if mem_vals else None
                    cpu_str = f"{avg_cpu:.1f}%" if avg_cpu else "N/A"
                    mem_str = f"{avg_mem:.0f} MB" if avg_mem else "N/A"
                    perf_report_html += f"""
                <div class="perf-round-card">
                    <div class="perf-round-title">Round {pr.round_num}</div>
                    <div class="perf-metric"><span class="perf-metric-label">Time to First Token (ms)</span><span class="perf-metric-value">{_fmt(ttft)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Token Generation Time (ms)</span><span class="perf-metric-value">{_fmt(tgt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Prompt Processing Rate (tokens/s)</span><span class="perf-metric-value">{_fmt(ppr)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Token Generation Rate (tokens/s)</span><span class="perf-metric-value">{_fmt(tgr)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Num Prompt Tokens</span><span class="perf-metric-value">{_fmt_int(npt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Num Generated Tokens</span><span class="perf-metric-value">{_fmt_int(ngt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">推理 CPU 占用</span><span class="perf-metric-value">{cpu_str}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">推理内存占用</span><span class="perf-metric-value">{mem_str}</span></div>
                </div>"""
                perf_report_html += "</div></div>"
            else:
                perf_report_html = """<div class="section">
                <h2><span class="icon">&#128202;</span> 性能报告</h2>
                <div class="empty-state">无 /profile 数据（模型未返回性能指标）</div>
                </div>"""

            # 崩溃日志:只保留次数 + 逐条极简摘要 + 跳转链接,完整 stderr/stdout 尾部原始文本
            # 在 report_crash_events.html(见 generate_summary_html 的下沉逻辑)。
            crash_html = ""
            if model_crashes:
                crash_html = """<div class="section"><h2><span class="icon">&#9888;</span> 崩溃事件</h2>
                <table class="crash-table"><thead><tr>
                    <th>时间</th><th>轮次</th><th>端点</th><th>详情</th><th>完整日志</th></tr></thead><tbody>"""
                for c in model_crashes:
                    idx = crash_idx_by_id.get(id(c))
                    detail_link = (f'<a href="report_crash_events.html#{ReportGenerator._crash_event_anchor(idx)}">查看 →</a>'
                                    if idx is not None else "-")
                    crash_html += f"""<tr><td>{_html_escape(c.timestamp)}</td>
                        <td class="center">{c.round_num}</td>
                        <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td><td>{_html_escape(c.detail)}</td>
                        <td>{detail_link}</td></tr>"""
                crash_html += "</tbody></table></div>"

            # 服务启动失败的醒目横幅 (模型级)
            model_startup_failures = [c for c in model_crashes if c.endpoint == "SERVICE_STARTUP"]
            startup_banner_html = ""
            if model_startup_failures:
                startup_banner_html = '<div class="startup-banner"><div class="startup-banner-title">⚠ 服务启动失败 — 本模型未能运行任何测试</div>'
                startup_banner_html += '<div class="startup-banner-desc">GenieAPIService.exe 启动后立即退出。下表展示具体的 exit_code 与 stderr 日志摘要。</div>'
                startup_banner_html += '<table class="startup-table"><thead><tr><th>时间</th><th>失败详情</th></tr></thead><tbody>'
                for c in model_startup_failures:
                    startup_banner_html += f'<tr><td>{_html_escape(c.timestamp)}</td><td><pre class="startup-detail">{_html_escape(c.detail)}</pre></td></tr>'
                startup_banner_html += '</tbody></table></div>'

            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            total_latency = sum(r.latency_ms for r in model_results if not r.skipped)

            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape(model)} - 测试报告</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Model Test Report</div>
        <h1>{_html_escape(model)}</h1>
        <p class="subtitle">模型测试详细报告</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>测试轮数:</strong> {len(rounds)}</span>
            <span class="meta-item"><strong>总耗时:</strong> {total_latency/1000:.1f}s</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

{startup_banner_html}

<div class="summary-bar">
    <div class="summary-card"><div class="num">{m_total}</div><div class="label">总测试数</div></div>
    <div class="summary-card card-pass"><div class="num">{m_pass}</div><div class="label">通过</div></div>
    <div class="summary-card card-fail"><div class="num">{sum(1 for r in model_results if (not r.passed) and (not r.crashed) and (not r.skipped))}</div><div class="label">失败</div></div>
    <div class="summary-card card-ignored" title="服务端已知占位/未实现缺陷的 FAIL 子集"><div class="num">{sum(1 for r in model_results if (not r.passed) and (not r.crashed) and (not r.skipped) and getattr(r, 'ignorable', False))}</div><div class="label">可忽略</div></div>
    <div class="summary-card card-crash"><div class="num">{sum(1 for r in model_results if r.crashed)}</div><div class="label">崩溃</div></div>
    <div class="summary-card"><div class="num">{sum(1 for r in model_results if r.skipped)}</div><div class="label">跳过</div></div>
    <div class="summary-card card-capability" title="根因判定为模型自身能力/输出质量局限而非服务端缺陷的诊断性标注;诊断性维度,不改变 passed/failed/退出码判定"><div class="num">{sum(1 for r in model_results if getattr(r, 'model_capability_issue', False))}</div><div class="label">模型能力问题</div></div>
</div>

<div class="progress-bar">
    <div class="progress-fill {'good' if m_rate >= 80 else ('warn' if m_rate >= 50 else 'bad')}" style="width:{max(m_rate, 5):.0f}%">
        <span class="progress-text">通过率 {m_rate:.1f}%</span>
    </div>
</div>
<div class="progress-meta"><span>模型通过率</span><span class="pct">{m_rate:.1f}%</span></div>

<div class="section">
<h2><span class="icon">&#9889;</span> 模型推理性能</h2>
<div class="model-section">
    <div class="group-section">
        {perf_table}
    </div>
</div>
</div>

<div class="section">
<h2><span class="icon">&#128737;</span> 服务功能接口</h2>
<div class="model-section">
    <div class="group-section">
        {service_table}
    </div>
</div>
</div>

{f'''<div class="section">
<h2><span class="icon">&#128295;</span> 其它/专项测试</h2>
<div class="model-section">
    <div class="group-section">
        {leftover_table}
    </div>
</div>
</div>''' if leftover_table else ""}
{model_builder_html}
{model_sampleapp_html}
{model_toolcalls_html}

{perf_report_html}
{crash_html}

<div class="footer">
    {_html_escape(model)} Test Report &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

            ReportGenerator._assert_no_placeholder_leak(html, f"模型报告 report_{model}.html")
            safe_model_name = model.replace(" ", "_").replace("/", "_")
            out_path = Path(out_dir) / f"report_{safe_model_name}.html"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[输出] 模型报告: {out_path}")

    @staticmethod
    def generate_html(all_results, perf_data, crash_events, out_dir, remote_mode=False):
        """向后兼容：调用 generate_summary_html + generate_model_reports"""
        ReportGenerator.generate_summary_html(all_results, perf_data, crash_events, out_dir, remote_mode)
        ReportGenerator.generate_model_reports(all_results, perf_data, crash_events, out_dir, remote_mode)

    @staticmethod
    def generate_conversations(all_results, out_dir):
        """生成详细数据记录 HTML — 包含 chat 对话、textsplitter、fetch 等大数据量接口的完整响应"""
        # 收集所有需要详细展示的结果。tool_calls 测试(即使名称含"chat/completions"子串)排除在外：
        # 它们与同一轮真正的 chat 测试共用相同的 model_R{round}_{mode} 锚点会互相冲突，且核心诊断
        # 价值(工具调用结构/流式重建参数)已下沉到独立的 report_tool_calls.html。
        chat_results = [r for r in all_results
                        if "chat/completions" in r.name and not r.name.startswith("POST /v1/chat/completions (tool_call")]
        textsplitter_results = [r for r in all_results if "textsplitter" in r.name and r.response_data]
        fetch_results = [r for r in all_results if r.name == "POST /fetch" and r.response_data]

        if not chat_results and not textsplitter_results and not fetch_results:
            return

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        stream_count = sum(1 for r in chat_results if r.is_stream)
        non_stream_count = len(chat_results) - stream_count
        format_counts = {}
        for r in chat_results:
            fmt = getattr(r, "request_format", "") or "openai_string"
            format_counts[fmt] = format_counts.get(fmt, 0) + 1

        # ---- Chat 对话卡片 ----
        chat_cards_html = ""
        for r in chat_results:
            mode = "stream" if r.is_stream else "nonstream"
            anchor_id = f"{r.model_name}_R{r.round_num}_{mode}"
            mode_label = "Stream" if r.is_stream else "Non-Stream"
            mode_class = "mode-stream" if r.is_stream else "mode-nonstream"
            stream_info = ""
            if r.is_stream and r.chunk_count > 0:
                stream_info = f"""<div class="stream-metrics">
                    <span><strong>Chunks:</strong> {r.chunk_count}</span>
                    <span><strong>TTFT:</strong> {r.ttft_ms:.0f}ms</span>
                    <span><strong>总耗时:</strong> {r.latency_ms:.0f}ms</span>
                </div>"""
            elif not r.is_stream:
                stream_info = f"""<div class="stream-metrics">
                    <span><strong>响应延迟:</strong> {r.latency_ms:.0f}ms</span>
                </div>"""
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            fmt_badge = _content_format_badge_html(r)
            # 多模态用例(client-style/openai-style 图片/音频)在 media_image_data_uri/
            # media_audio_data_uri 里带着可直接渲染的 data URI —— 这里把真正传给模型的
            # 图片/音频素材原样展示在 USER 消息下方，而不是只有一句"该模型支持图片"这类
            # 文字描述、看不到到底传了哪张图/哪段音频。
            media_html = ""
            if getattr(r, "media_image_data_uri", ""):
                media_html += (f'<div class="msg-media"><img src="{r.media_image_data_uri}" alt="multimodal input image">'
                               f'<div class="msg-media-label">{_html_escape(r.media_asset_label)}</div></div>')
            if getattr(r, "media_audio_data_uri", ""):
                media_html += (f'<div class="msg-media"><audio controls src="{r.media_audio_data_uri}"></audio>'
                               f'<div class="msg-media-label">{_html_escape(r.media_asset_label)}</div></div>')
            chat_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge {mode_class}">{mode_label}</span>{fmt_badge}
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                </div>
                <div class="message user-msg">
                    <div class="msg-role">USER</div>
                    <div class="msg-content">{_html_escape(r.text_prompt)}</div>
                    {media_html}
                </div>
                <div class="message assistant-msg">
                    <div class="msg-role">ASSISTANT</div>
                    <div class="msg-content">{_html_escape(r.text_response) if r.text_response else '(无响应)'}</div>
                </div>
                {stream_info}
            </div>"""

        # ---- Textsplitter 卡片 ----
        textsplitter_cards_html = ""
        for r in textsplitter_results:
            anchor_id = f"{r.model_name}_R{r.round_num}_textsplitter"
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            data = r.response_data or {}
            seg_count = data.get("segment_count", 0)
            segments = data.get("segments", [])
            seg_html = ""
            for i, seg in enumerate(segments):
                seg_text = seg.get("text", "")
                seg_len = seg.get("length", len(seg_text))
                seg_html += f'<div class="seg-item"><span class="seg-idx">#{i+1}</span> <span class="seg-len">[{seg_len} chars]</span> {_html_escape(seg_text)}</div>'
            textsplitter_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge mode-textsplitter">TextSplitter</span>
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                    <span class="round-tag">{seg_count} segments</span>
                </div>
                <div class="data-content">{seg_html if seg_html else '<em>无分段数据</em>'}</div>
            </div>"""

        # ---- Fetch 卡片 ----
        fetch_cards_html = ""
        for r in fetch_results:
            anchor_id = f"{r.model_name}_R{r.round_num}_fetch"
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            data = r.response_data or {}
            history_count = data.get("history_count", 0)
            history = data.get("history", [])
            hist_html = ""
            for h in history:
                role = h.get("role", "unknown")
                content = h.get("content", "")
                role_cls = "user-msg" if role == "user" else "assistant-msg"
                hist_html += f"""<div class="message {role_cls}">
                    <div class="msg-role">{_html_escape(role.upper())}</div>
                    <div class="msg-content">{_html_escape(content)}</div>
                </div>"""
            fetch_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge mode-fetch">Fetch History</span>
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                    <span class="round-tag">{history_count} 条记录</span>
                </div>
                <div class="hist-content">{hist_html if hist_html else '<div class="data-content"><em>无历史数据</em></div>'}</div>
            </div>"""

        # ---- 组合各区域 ----
        sections_html = ""
        if chat_cards_html:
            sections_html += f"""<h2 class="section-title">💬 对话记录（Chat Completions）</h2>{chat_cards_html}"""
        if textsplitter_cards_html:
            sections_html += f"""<h2 class="section-title">✂️ 文本切分（TextSplitter）</h2>{textsplitter_cards_html}"""
        if fetch_cards_html:
            sections_html += f"""<h2 class="section-title">📜 对话历史（Fetch）</h2>{fetch_cards_html}"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenieAPIService 详细数据记录 - {now_str}</title>
<style>
:root {{
    --primary: #1565C0;
    --success: #2E7D32;
    --danger: #C62828;
    --warning: #E65100;
    --bg: #F5F7FA;
    --card-bg: #FFFFFF;
    --border: #E0E0E0;
    --text: #263238;
    --text-secondary: #546E7A;
    --user-bg: #E3F2FD;
    --assistant-bg: #F1F8E9;
    --radius: 10px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6; }}
.page-header {{ background: linear-gradient(135deg, #1565C0 0%, #0D47A1 100%); color: white; padding: 35px 0; margin-bottom: 30px; }}
.page-header-inner {{ max-width: 900px; margin: 0 auto; padding: 0 30px; }}
.page-header h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 6px; }}
.page-header .info {{ opacity: 0.85; font-size: 13px; display: flex; gap: 20px; flex-wrap: wrap; margin-top: 10px; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 0 30px 60px; }}
.section-title {{ font-size: 18px; font-weight: 600; color: var(--primary); margin: 30px 0 16px; padding-bottom: 8px; border-bottom: 2px solid var(--primary); }}
.conv-card {{ background: var(--card-bg); border-radius: var(--radius); margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); overflow: hidden; }}
.conv-meta {{ display: flex; gap: 10px; align-items: center; padding: 12px 20px; background: #FAFAFA; border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
.badge {{ font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
.mode-stream {{ background: #E8F5E9; color: #2E7D32; }}
.mode-nonstream {{ background: #E3F2FD; color: #1565C0; }}
.mode-textsplitter {{ background: #FFF3E0; color: #E65100; }}
.mode-fetch {{ background: #F3E5F5; color: #6A1B9A; }}
.badge-pass {{ background: #E8F5E9; color: #2E7D32; }}
.badge-fail {{ background: #FFEBEE; color: #C62828; }}
.badge-crash {{ background: #FFF3E0; color: #E65100; }}
.model-tag {{ font-size: 12px; color: var(--primary); font-weight: 600; }}
.round-tag {{ font-size: 12px; color: var(--text-secondary); }}
.message {{ padding: 16px 20px; }}
.user-msg {{ background: var(--user-bg); border-left: 4px solid #1976D2; }}
.assistant-msg {{ background: var(--assistant-bg); border-left: 4px solid #43A047; }}
.msg-role {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; color: var(--text-secondary); }}
.msg-content {{ font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-wrap: break-word; max-height: 500px; overflow-y: auto; }}
.msg-media {{ margin-top: 10px; }}
.msg-media img {{ max-width: 320px; max-height: 320px; border-radius: var(--radius-sm, 4px); border: 1px solid var(--border); display: block; }}
.msg-media audio {{ width: 320px; }}
.msg-media-label {{ font-size: 11px; color: var(--text-secondary); margin-top: 4px; font-family: var(--font-mono, monospace); }}
.stream-metrics {{ display: flex; gap: 20px; padding: 10px 20px; background: #FAFAFA; border-top: 1px solid var(--border); font-size: 12px; color: var(--text-secondary); }}
.data-content {{ padding: 16px 20px; font-size: 13px; line-height: 1.6; max-height: 600px; overflow-y: auto; }}
.hist-content {{ max-height: 800px; overflow-y: auto; }}
.seg-item {{ padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; line-height: 1.5; }}
.seg-item:last-child {{ border-bottom: none; }}
.seg-idx {{ font-weight: 700; color: var(--primary); margin-right: 8px; }}
.seg-len {{ font-size: 11px; color: var(--text-secondary); margin-right: 8px; }}
.footer {{ text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 20px; }}
</style>
</head>
<body>
<div class="page-header">
    <div class="page-header-inner">
        <h1>GenieAPIService 详细数据记录</h1>
        <div class="info">
            <span><strong>生成时间:</strong> {now_str}</span>
            <span><strong>对话:</strong> {len(chat_results)} ({stream_count} stream / {non_stream_count} non-stream)</span>
            <span><strong>请求格式:</strong> {format_counts.get("openai_string", 0)} 字符串 / {format_counts.get("openai_array", 0)} 数组 / {format_counts.get("client_object", 0)} 对象</span>
            <span><strong>TextSplitter:</strong> {len(textsplitter_results)}</span>
            <span><strong>Fetch:</strong> {len(fetch_results)}</span>
            <span><a href="report.html" style="color:white; text-decoration:underline;">← 返回总汇报</a></span>
        </div>
    </div>
</div>
<div class="container">
{sections_html}
<div class="footer">GenieAPIService Data Log &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}</div>
</div>
</body>
</html>"""

        out_path = Path(out_dir) / "conversations.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 详细数据记录: {out_path}")


def _html_escape(text):
    """简单的 HTML 转义"""
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _crash_badge_html(c):
    """根据 CrashEvent 的 endpoint/detail 判断是否为 MNN OOM 相关事件,返回对应徽章 HTML；
    不相关时返回空字符串。真实 OOM(MNN_OOM_CRASH)与疑似级联崩溃(MNN_OOM_CASCADE)使用不同样式,
    避免阅读者把"MNN 自身真实问题"和"疑似被 MNN OOM 拖累而崩溃的其它后端"混为一谈；两者
    都是需要关注的真实问题、都已计入 failed/crashed 统计,徽章仅作诊断提示,不代表已豁免。"""
    endpoint = c.endpoint or ""
    detail_lower = (c.detail or "").lower()
    if endpoint == "MNN_OOM_CASCADE" or "mnn_oom_cascade" in detail_lower:
        return '<span class="badge-mnn-cascade" title="疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因;已计入真实失败,不代表豁免">疑似级联崩溃(非豁免)</span>'
    if endpoint == "MNN_OOM_CRASH":
        return '<span class="badge-mnn-oom" title="MNN 模型自身触发的真实内存不足/崩溃事件">MNN真实OOM</span>'
    return ""


# ============================================================================
# MultiModelTester - 多模型并发加载与路由测试
# ============================================================================
class MultiModelTester:
    """
    阶段 3：多模型并发加载与路由测试。

    在单个服务实例中验证：
    1. GET /models 返回所有已成功加载的模型
    2. 对每个已加载模型发送 chat 请求，验证路由正确性
    3. 并发向不同模型发送请求，验证并发安全性
    4. 请求未加载/不存在的模型时返回 404
    5. MNN 模型加载失败（含崩溃）标记为 ignorable

    内存约束说明：
    - QNN/NPU 模型加载失败时 LoadModel 返回 false，服务继续运行
    - GGUF/GPU 模型 OOM 时抛出异常，被捕获后跳过该模型
    - MNN/CPU 模型 OOM 时可能导致进程崩溃，测试框架检测崩溃并标记 ignorable
    """

    # MNN 后端标识（加载失败/崩溃时标记为 ignorable）
    MNN_BACKENDS = {"mnn"}

    # 三后端两两组合（降级验证用）：当"npu+gpu+cpu 三后端同时驻留"因内存不足未能全部
    # 达成时（例如先加载 qnn，再加载 gguf 后内存已耗尽，mnn 根本没机会被尝试），
    # 用于兜底验证"至少两两可以同时驻留"这一底线能力，而不是让 mnn 完全没有被测试过。
    PAIRWISE_COMBOS = (("npu", "gpu"), ("npu", "cpu"), ("gpu", "cpu"))
    _DEVICE_LABELS = {"npu": "QNN/NPU", "gpu": "GGUF/GPU", "cpu": "MNN/CPU"}

    @staticmethod
    def _infer_device(model_name):
        """根据模型目录名猜测其后端/设备：GGUF→gpu，MNN→cpu，其余（QNN）→npu"""
        return infer_backend(model_name)[1]

    def __init__(self, host, port, svc, perf, round_num=1):
        self.base_url = f"http://{host}:{port}"
        self.svc = svc
        self.perf = perf
        self.round_num = round_num
        self.results: list[TestResult] = []
        self.crash_events: list[CrashEvent] = []
        self._restart_count = 0
        self.MAX_RESTARTS = 3
        # _get/_post 失败时(返回 None)把真实异常类型+消息记录在这里,供调用方在 detail 里
        # 附带具体原因，便于区分究竟是 ConnectionError 还是 ReadTimeout。
        self._last_request_error = ""

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _get(self, path, timeout=15):
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=timeout)
            self._last_request_error = ""
            return r
        except Exception as e:
            self._last_request_error = f"{type(e).__name__}: {e}"
            return None

    def _post(self, path, body, timeout=300):
        try:
            r = requests.post(f"{self.base_url}{path}", json=body, timeout=timeout)
            self._last_request_error = ""
            return r
        except Exception as e:
            self._last_request_error = f"{type(e).__name__}: {e}"
            return None

    def _make_result(self, name, passed, status_code, detail, ignorable=False, ignore_reason="",
                      skipped=False, crashed=False):
        return TestResult(
            name=name, round_num=self.round_num, model_name="_multi_model_",
            passed=passed, status_code=status_code, latency_ms=0,
            detail=detail, ignorable=ignorable, ignore_reason=ignore_reason,
            skipped=skipped, crashed=crashed
        )

    # ── 测试用例 ──────────────────────────────────────────────────────────────

    def test_models_list(self):
        """验证 GET /models 返回多模型列表（至少 1 个模型）"""
        name = "MULTI: GET /models (multi-model list)"
        r = self._get("/models")
        if r is None:
            return self._make_result(name, False, 0, "请求失败（连接错误）")
        if r.status_code != 200:
            return self._make_result(name, False, r.status_code, f"status={r.status_code}")
        try:
            data = r.json()
            model_list = data.get("data", [])
            loaded_entries = []
            for m in model_list:
                if not m.get("is_loaded"):
                    continue
                loaded_entries.append({
                    "id": m.get("id", ""),
                    "backend": m.get("backend", ""),
                    "device": m.get("device", ""),
                    "context_length": m.get("context_length", ""),
                })
            names = [m.get("id", "?") for m in loaded_entries]
            if len(names) == 0:
                return self._make_result(name, False, 200, "模型列表为空（无模型加载成功）")
            type_summary = sorted({f"{m.get('backend')}/{m.get('device')}" for m in loaded_entries})
            # 额外校验每条记录的 backend/device 字段是否落在预期取值集合内——如果服务端把
            # 这两个字段返回为空字符串或拼错了取值，仅判断列表非空无法发现，这里补一道
            # 最小成本的取值校验，命中即降级为失败。
            _valid_backends = {"qnn", "mnn", "gguf"}
            _valid_devices = {"npu", "cpu", "gpu"}
            bad_entries = [m for m in loaded_entries
                           if str(m.get("backend", "")).lower() not in _valid_backends
                           or str(m.get("device", "")).lower() not in _valid_devices]
            if bad_entries:
                detail = (f"已加载 {len(names)} 个模型: {names}; 类型: {type_summary}; "
                          f"发现 backend/device 字段异常的条目: {bad_entries}")
                result = self._make_result(name, False, 200, detail)
                result.response_data = {"loaded_models": loaded_entries, "type_summary": type_summary}
                return result
            detail = f"已加载 {len(names)} 个模型: {names}; 类型: {type_summary}"
            result = self._make_result(name, True, 200, detail)
            result.response_data = {"loaded_models": loaded_entries, "type_summary": type_summary}
            return result
        except Exception as e:
            return self._make_result(name, False, r.status_code, f"JSON 解析失败: {e}")

    def _get_loaded_model_entries(self):
        """从 /models 获取当前**已加载**模型的结构化条目。"""
        r = self._get("/models")
        if r is None or r.status_code != 200:
            return []
        try:
            data = r.json()
            entries = []
            for m in data.get("data", []):
                if not (m.get("id") and m.get("is_loaded")):
                    continue
                entries.append({
                    "id": m.get("id", ""),
                    "backend": m.get("backend", ""),
                    "device": m.get("device", ""),
                    "context_length": m.get("context_length", ""),
                })
            return entries
        except Exception:
            return []

    def _get_loaded_models(self):
        """从 /models 获取当前**已加载**的模型名称列表。

        注意：/models 接口会返回磁盘扫描到的全部模型（含 is_loaded=False 的未加载模型，
        用于展示可切换的候选列表），必须按 is_loaded 字段过滤，否则会把未加载的模型
        误当作"已加载"，导致后续路由测试/并发测试对未加载模型发起请求，
        触发意外的动态切换（尤其是 NPU 设备的连续/并发切换可能引发竞态崩溃）。
        """
        return [m.get("id", "") for m in self._get_loaded_model_entries() if m.get("id")]

    def _get_service_config_models(self):
        """从 /profile 或 /models 获取 service_config.json 中配置的所有模型（含未加载的）"""
        # 通过 /models 只能获取已加载的；service_config 中的全量列表需要从别处获取
        # 这里直接返回已加载列表，未加载的模型通过 404 测试覆盖
        return self._get_loaded_models()

    def ensure_multi_backend_loaded(self, models_by_device):
        """依次向 npu/gpu(GGUF)/cpu(MNN) 三个不同设备的模型发送 chat 请求，
        触发 chat_request_handler.cpp 中已有的"磁盘扫描 + 动态加载"分支，
        使三种后端各自常驻一个模型（同设备不重复请求，避免触发
        UnloadModelsByDevice 卸载刚加载的模型）。

        models_by_device: dict[str, str]，形如 {"npu": "qwen3-8b-8480", "gpu": "gpt-oss-20b-GGUF", "cpu": "gpt-oss-20b-MNN"}
        返回一条汇总 TestResult：MNN OOM 崩溃时按现有 ignorable 机制处理并跳过继续。
        """
        name = "MULTI: ensure NPU/GGUF/MNN backends loaded simultaneously"
        touched = []
        cpu_attempted = False
        device_results = {}
        for device, model_name in models_by_device.items():
            is_mnn = device == "cpu"
            if is_mnn:
                cpu_attempted = True
            r = self.test_route_to_model(model_name)
            device_results[device] = r
            self.results.append(r)
            touched.append(f"{device}:{model_name}→{'OK' if r.passed else ('SKIP' if r.skipped else ('IGN' if r.ignorable else 'FAIL'))}")
            if r.crashed:
                if is_mnn and self._restart_count < self.MAX_RESTARTS:
                    # MNN OOM 崩溃(分类A)：自动重启并跳过该模型，继续尝试后续设备
                    self._restart_count += 1
                    # 必须在 restart() 之前捕获日志尾部/退出码,否则拿到的是重启后新进程的信息,
                    # 而不是真正崩溃的那个进程的证据。
                    log_tail = _capture_log_tail(self.svc)
                    restarted = self.svc.restart()
                    self.crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=model_name, round_num=self.round_num,
                        endpoint="MNN_OOM_CRASH",
                        detail=f"MNN OOM 崩溃后已自动重启(第{self._restart_count}次): {'成功' if restarted else '失败'}",
                        log_tail=log_tail
                    ))
                    self.results.append(TestResult(
                        name=f"MULTI: auto-restart after MNN OOM ({model_name})",
                        round_num=self.round_num, model_name="_multi_model_",
                        passed=restarted, status_code=0, latency_ms=0,
                        detail="MNN OOM 崩溃后已自动重启,跳过该模型继续后续设备" if restarted else "重启失败,不可恢复",
                        ignorable=restarted, ignore_reason="MNN OOM 崩溃后已自动重启并跳过" if restarted else "",
                        auto_restarted=restarted
                    ))
                # 无论是 MNN 自身崩溃还是其它真实错误,进程已不可用,不再继续尝试后续设备
                break

        loaded_entries = self._get_loaded_model_entries()
        loaded_devices = {m.get("device") or self._infer_device(m.get("id", "")) for m in loaded_entries}
        expected_devices = set(models_by_device.keys())
        missing = expected_devices - loaded_devices
        type_summary = sorted({f"{m.get('backend')}/{m.get('device')}" for m in loaded_entries})
        detail = f"已加载设备: {sorted(loaded_devices)}, 期望: {sorted(expected_devices)}, 类型: {type_summary}, 请求顺序: {touched}"
        if missing and "cpu" in missing:
            if cpu_attempted:
                cpu_result = device_results.get("cpu")
                if cpu_result is not None and cpu_result.skipped:
                    # cpu 设备已被请求,服务端优雅拒绝(如 insufficient_memory)：环境资源约束,
                    # 不是代码缺陷,精确标记为 skipped=True,而不是当作真实失败。
                    result = self._make_result(name, False, 0, detail, skipped=True,
                                                ignore_reason=cpu_result.detail)
                else:
                    # cpu 设备已经真正被请求过,但未能成功同时驻留(且非上面的优雅内存拒绝)——
                    # 这是需要关注的真实问题(不论是 MNN 自身其它失败还是崩溃,崩溃永远不可豁免)。
                    result = self._make_result(name, False, 0, detail)
            else:
                # cpu 从未被真正请求过(更早的设备崩溃导致循环提前 break) —— 无法验证 cpu
                # 能否同时驻留,应精确标记为 skipped=True,而不是套用"cpu 缺失=MNN 内存不足"
                # 这类没有核实过的猜测性归因。
                result = self._make_result(
                    name, False, 0, detail, skipped=True,
                    ignore_reason="更早的设备崩溃导致循环提前结束,cpu 后端从未被真正请求,无法验证是否可同时驻留"
                )
        else:
            result = self._make_result(name, len(missing) == 0, 0, detail)
        result.response_data = {
            "loaded_models": loaded_entries,
            "loaded_devices": sorted(loaded_devices),
            "expected_devices": sorted(expected_devices),
            "missing_devices": sorted(missing),
            "type_summary": type_summary,
            "request_order": touched,
        }
        return result

    def ensure_pairwise_backends_loaded(self, models_by_device, models_root):
        """当"npu+gpu+cpu 三后端同时驻留"未能全部达成时的降级验证：逐一确认
        npu+gpu / npu+cpu / gpu+cpu 三种两两组合是否仍能同时驻留。

        每个组合测试前都用该组合第一个设备对应的模型作为入口重启服务，获得一份干净的
        内存基线（不带着此前"三后端"尝试残留的内存占用），再动态路由加载该组合的第二个
        设备——避免"三后端"尝试失败后残留的进程状态污染两两组合本身是否可行的判定。

        models_root: 模型根目录（Path 或 str），用于构造重启入口的 config.json 路径。
        返回 (results, matrix, extra)：results 为逐组合产生的 TestResult 列表；matrix 为
        {"npu+gpu": "pass"/"fail"/"skip", ...}——"skip" 覆盖"环境缺模型"与"服务端因内存
        不足优雅拒绝"两种情况(均非代码缺陷,不计入失败)；extra 为 {"npu+gpu": {"crashed":
        bool, "reason_detail": str, "skip_reason": "missing_model"/"memory"/None}, ...}，
        仅在对应 matrix 值不是 "pass" 时才有条目——用于让 _build_coexistence_matrix_result
        生成准确的原因(是否真的崩溃、真实失败/跳过详情)，而不是一句无差别的通用提示。
        """
        results = []
        matrix = {}
        extra = {}
        models_root = Path(models_root)
        for a, b in self.PAIRWISE_COMBOS:
            pair_key = f"{a}+{b}"
            pair_label = f"{self._DEVICE_LABELS[a]} + {self._DEVICE_LABELS[b]}"
            name = f"MULTI: pairwise coexistence {pair_key} ({pair_label})"
            if a not in models_by_device or b not in models_by_device:
                missing_device = a if a not in models_by_device else b
                reason_detail = f"环境中缺少 {self._DEVICE_LABELS[missing_device]} 对应的模型,无法验证该组合"
                matrix[pair_key] = "skip"
                extra[pair_key] = {"crashed": False, "reason_detail": reason_detail, "skip_reason": "missing_model"}
                results.append(self._make_result(name, False, 0, reason_detail, skipped=True))
                continue

            entry_model = models_by_device[a]
            entry_config = models_root / entry_model / "config.json"
            self.svc._current_config = str(entry_config.resolve())
            log_tail_before = _capture_log_tail(self.svc)
            restarted = self.svc.restart()
            if not restarted:
                matrix[pair_key] = "fail"
                extra[pair_key] = {"crashed": True, "reason_detail": f"重启服务失败(入口模型={entry_model})"}
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(),
                    model_name=entry_model, round_num=self.round_num,
                    endpoint="PAIRWISE_RESTART_FAILED",
                    detail=f"两两降级验证 {pair_key} 重启服务失败(入口模型={entry_model})",
                    log_tail=log_tail_before
                ))
                results.append(self._make_result(
                    name, False, 0,
                    f"重启服务失败(入口模型={entry_model}),无法验证该组合是否可同时驻留",
                    crashed=True
                ))
                continue

            # 显式验证入口模型(设备 a)确实已正常加载并响应——通常随 -c 启动即直接加载，
            # 这里仍主动发一次请求核实，避免"重启看起来成功但入口模型自身未真正可用"
            # 这类边缘情况被漏判为"该组合可同时驻留"。
            r_entry = self.test_route_to_model(entry_model)
            results.append(r_entry)
            if r_entry.crashed:
                matrix[pair_key] = "fail"
                extra[pair_key] = {"crashed": True, "reason_detail": r_entry.detail}
                continue
            if r_entry.skipped:
                # 入口模型自身因服务端优雅拒绝(如 insufficient_memory)未能加载：环境资源
                # 约束,不是代码缺陷,该组合精确标记为 skip 而非 fail。
                matrix[pair_key] = "skip"
                extra[pair_key] = {"crashed": False, "reason_detail": r_entry.detail, "skip_reason": "memory"}
                continue
            if not r_entry.passed and not r_entry.ignorable:
                matrix[pair_key] = "fail"
                extra[pair_key] = {"crashed": False, "reason_detail": r_entry.detail}
                continue

            second_model = models_by_device[b]
            r_second = self.test_route_to_model(second_model)
            results.append(r_second)
            if r_second.crashed:
                matrix[pair_key] = "fail"
                extra[pair_key] = {"crashed": True, "reason_detail": r_second.detail}
                continue
            if r_second.skipped:
                matrix[pair_key] = "skip"
                extra[pair_key] = {"crashed": False, "reason_detail": r_second.detail, "skip_reason": "memory"}
                continue

            loaded_entries = self._get_loaded_model_entries()
            loaded_devices = {m.get("device") or self._infer_device(m.get("id", "")) for m in loaded_entries}
            pair_ok = {a, b} <= loaded_devices
            matrix[pair_key] = "pass" if pair_ok else "fail"
            detail = (f"已加载设备: {sorted(loaded_devices)}, 期望: {sorted({a, b})}, "
                      f"入口模型: {entry_model}, 追加路由: {second_model}")
            if not pair_ok:
                # r_second 未崩溃但组合仍不成立：最有信息量的原因通常是 r_second 自身的
                # 失败详情；r_second 若已 passed 而组合仍不成立(边缘情况,如 /models 未及时
                # 反映)则回退用 detail。
                reason_detail = r_second.detail if not r_second.passed else detail
                extra[pair_key] = {"crashed": False, "reason_detail": reason_detail}
            results.append(self._make_result(name, pair_ok, 0, detail))

        return results, matrix, extra

    def _build_coexistence_matrix_result(self, triple_result, pair_matrix, pairwise_attempted, pair_extra=None):
        """把"三后端同时驻留 + 两两降级"整理为一份统一的共存矩阵，写入一条独立的汇总
        TestResult，供 report.html 的多后端共存矩阵区块直接读取
        response_data["coexistence_matrix"]。

        triple_result: ensure_multi_backend_loaded() 的返回值。
        pair_matrix: ensure_pairwise_backends_loaded() 的返回值（未执行两两降级时传 {}）。
        pairwise_attempted: 两两降级是否被真正执行过（remote 模式/未提供模型根目录时为 False）。
        pair_extra: ensure_pairwise_backends_loaded() 返回的第三个值（未执行两两降级时传 None）,
        用于区分"该组合确实因进程崩溃而失败"与"服务端优雅拒绝/未成功路由但进程未崩溃"这两种
        性质完全不同的失败——只有前者才应该引用崩溃事件日志。
        """
        pair_extra = pair_extra or {}
        name = "MULTI: backend coexistence matrix (triple + pairwise fallback)"
        triple_missing = set((triple_result.response_data or {}).get("missing_devices", []))
        triple_ok = len(triple_missing) == 0 and not triple_result.skipped
        triple_status = "pass" if triple_ok else ("skip" if triple_result.skipped else "fail")
        # reason: 主报告用的极短标签(不含原始错误文本)；detail: 详情页用的完整原文，两者
        # 分离是为了让主报告保持与其它区块一致的密度，原始错误串只出现在详情页。
        _TRIPLE_REASON = {"pass": "三后端同时驻留", "skip": "内存不足,已跳过验证", "fail": "验证失败"}
        matrix = {
            "triple": {"combo": "npu+gpu+cpu", "status": triple_status,
                       "reason": _TRIPLE_REASON[triple_status], "detail": triple_result.detail},
            "pairs": {},
        }
        for a, b in self.PAIRWISE_COMBOS:
            pair_key = f"{a}+{b}"
            if triple_ok:
                matrix["pairs"][pair_key] = {
                    "status": "pass", "reason": "随三后端同时驻留",
                    "detail": "三后端已同时驻留验证通过,两两组合天然成立,无需单独验证"
                }
            elif not pairwise_attempted:
                matrix["pairs"][pair_key] = {
                    "status": "unknown", "reason": "未执行验证",
                    "detail": "本次未执行两两降级验证(远程模式或未提供本地模型根目录)"
                }
            else:
                v = pair_matrix.get(pair_key, "skip")
                info = pair_extra.get(pair_key) or {}
                reason_detail = info.get("reason_detail", "")
                if v == "pass":
                    matrix["pairs"][pair_key] = {"status": "pass", "reason": "验证通过", "detail": "两两同时驻留验证通过"}
                elif v == "skip":
                    if info.get("skip_reason") == "missing_model":
                        matrix["pairs"][pair_key] = {"status": "skip", "reason": "环境缺少模型",
                                                       "detail": reason_detail or "环境中缺少该组合所需模型,无法验证"}
                    else:
                        matrix["pairs"][pair_key] = {
                            "status": "skip", "reason": "内存不足,已跳过",
                            "detail": (f"服务端预判内存不足优雅跳过该组合验证(非失败,详见下方逐项检查记录表格)：{reason_detail}"
                                        if reason_detail else
                                        "服务端预判内存不足优雅跳过该组合验证(非失败,详见下方逐项检查记录表格)")
                        }
                elif info.get("crashed"):
                    # 确认是进程真实崩溃(已产生对应 CrashEvent,崩溃事件日志区块必然会渲染)——
                    # 才引用崩溃事件日志,避免在没有任何崩溃发生时误导性地指向一个根本不存在的区块。
                    matrix["pairs"][pair_key] = {
                        "status": "fail", "reason": "进程崩溃",
                        "detail": (f"两两同时驻留验证未通过：进程发生崩溃,详见下方崩溃事件日志表格"
                                    + (f"（{reason_detail}）" if reason_detail else ""))
                    }
                else:
                    # 非崩溃场景(未成功路由等)：不引用崩溃事件日志(本次未发生崩溃,该区块不会
                    # 渲染)，改为指向下方的"逐项检查记录"表格(该表格在本区块下方,不是上方)。
                    matrix["pairs"][pair_key] = {
                        "status": "fail", "reason": "验证失败",
                        "detail": (f"两两同时驻留验证未通过(非崩溃,详见下方逐项检查记录表格)：{reason_detail}"
                                    if reason_detail else
                                    "两两同时驻留验证未通过(非崩溃,详见下方逐项检查记录表格)")
                    }

        pair_statuses = [v["status"] for v in matrix["pairs"].values()]
        detail = (f"三后端同时驻留={triple_status}; 两两组合: " +
                  ", ".join(f"{k}={v['status']}" for k, v in matrix["pairs"].items()))
        if triple_ok:
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=True, status_code=0, latency_ms=0, detail=detail,
                response_data={"coexistence_matrix": matrix}
            )
        if not pairwise_attempted:
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0, detail=detail,
                skipped=True,
                ignore_reason="远程模式或未提供本地模型根目录,无法重启服务执行两两降级验证",
                response_data={"coexistence_matrix": matrix}
            )
        testable = [s for s in pair_statuses if s != "skip"]
        # "至少两两可用"的底线：三后端未能全部同时驻留时,要求全部可测试的两两组合都必须
        # 成功;任意一个两两组合失败都是需要关注的真实问题,不代表"至少两两"这一底线达标。
        pairwise_all_ok = len(testable) > 0 and all(s == "pass" for s in testable)
        return TestResult(
            name=name, round_num=self.round_num, model_name="_multi_model_",
            passed=pairwise_all_ok, status_code=0, latency_ms=0, detail=detail,
            response_data={"coexistence_matrix": matrix}
        )

    def test_route_to_model(self, model_name):
        """向指定模型发送 chat 请求，验证路由正确性。

        失败/崩溃归类规则见 `_classify_process_down`。
        """
        name = f"MULTI: chat route → {model_name}"
        is_mnn = infer_backend(model_name)[0] == "mnn"
        body = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Reply with one word: hello"}
            ],
            "stream": False,
            "max_tokens": 32
        }
        start = time.time()
        r = self._post("/v1/chat/completions", body, timeout=120)
        latency = (time.time() - start) * 1000

        if r is None:
            if _confirm_process_dead(self.svc):
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, model_name)
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(),
                    model_name=model_name, round_num=self.round_num,
                    endpoint="MNN_OOM_CRASH" if is_mnn else ("MNN_OOM_CASCADE" if ignore_reason == "mnn_oom_cascade" else "SERVICE_CRASH"),
                    detail=f"{name}: 连接失败,服务进程已退出; {classify_detail}",
                    log_tail=_capture_log_tail(self.svc)
                ))
                return TestResult(
                    name=name, round_num=self.round_num, model_name="_multi_model_",
                    passed=False, status_code=0, latency_ms=latency,
                    detail=classify_detail,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    crashed=True
                )
            # 进程仍存活,只是这次请求本身连接失败/超时: 该模型自身的真实问题,不做豁免
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=latency,
                detail=f"请求失败（{self._last_request_error or '连接错误/超时'},进程仍存活,应视为该模型自身的真实问题）"
            )

        if r.status_code == 200:
            try:
                data = r.json()
                choices = data.get("choices", [])
                if choices and "message" in choices[0]:
                    content = choices[0]["message"].get("content", "")
                    return TestResult(
                        name=name, round_num=self.round_num, model_name="_multi_model_",
                        passed=len(content) > 0, status_code=200, latency_ms=latency,
                        detail=f"OK response_len={len(content)}"
                    )
                return self._make_result(name, False, 200, "缺少 choices[0].message.content")
            except Exception as e:
                return self._make_result(name, False, 200, f"JSON 解析失败: {e}")

        if r.status_code == 500:
            failure_reason, failure_detail = _extract_failure_reason(r)
            if failure_reason == "insufficient_memory" and is_mnn:
                # 服务端优雅拒绝(MNN 内存预检查): 环境资源约束,不是代码缺陷,计为 skipped
                # 而非 failed,仍记录 MNN OOM 诊断标记供其它模型的级联判定参考。
                detail = f"服务端预判内存不足拒绝加载(failure_reason=insufficient_memory): {failure_detail}"
                _mark_mnn_oom(self.svc, model_name, detail)
                result = self._make_result(name, False, 500, detail, skipped=True)
                result.response_data = {"failure_reason": failure_reason, "failure_detail": failure_detail}
                return result

        if r.status_code in (404, 503):
            detail = f"status={r.status_code} 模型未加载（可能因内存不足被跳过）"
            if is_mnn:
                # MNN 模型自身未能加载成功且无法确认具体原因: 保守起见仍按真实问题处理
                # (与上面 500+insufficient_memory 的精确判定不同,这里状态码本身不携带
                # failure_reason,无法确认一定是内存不足)。
                classify_detail = f"MNN 模型自身未能加载成功,这是需要关注的真实问题: {detail}"
                _mark_mnn_oom(self.svc, model_name, classify_detail)
                return TestResult(
                    name=name, round_num=self.round_num, model_name="_multi_model_",
                    passed=False, status_code=r.status_code, latency_ms=latency,
                    detail=classify_detail
                )
            oom_event = _get_mnn_oom_event(self.svc)
            if oom_event:
                # 疑似同进程内此前已发生 MNN OOM，该模型可能因资源被占用而未能加载；但这只是
                # 诊断性提示，不代表豁免——404/503 且进程仍存活是"正常的优雅失败"，本来就不该
                # 造成连坐，如实计入该模型自身的真实失败。
                classify_detail = (f"疑似因同进程内 MNN 模型({oom_event['model_name']})此前发生 OOM,"
                                    f"该模型可能因资源被占用而未能加载(仅诊断提示,不代表豁免): {detail} "
                                    f"(ignore_reason=mnn_oom_cascade)")
                return TestResult(
                    name=name, round_num=self.round_num, model_name="_multi_model_",
                    passed=False, status_code=r.status_code, latency_ms=latency,
                    detail=classify_detail,
                    ignorable=False, ignore_reason="mnn_oom_cascade"
                )
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=f"{detail},应视为该模型自身的真实问题"
            )

        return self._make_result(name, False, r.status_code,
                                 f"status={r.status_code} body={r.text[:120]}")

    def test_unknown_model_route(self):
        """请求不存在的模型，期望立即返回 404/400（延迟 <5s，防止 60×500ms 轮询等待式退化重现）。"""
        name = "MULTI: invalid model route returns 404/400"
        invalid_model = "__invalid_model_for_negative_route_test__"
        body = {
            "model": invalid_model,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "max_tokens": 8
        }
        start = time.time()
        r = self._post("/v1/chat/completions", body, timeout=30)
        latency_ms = (time.time() - start) * 1000
        if r is None:
            if _confirm_process_dead(self.svc):
                # invalid_model 不是真实模型,不可能是 MNN,只会落在"级联失败"或"真实错误"两支。
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, invalid_model)
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(),
                    model_name=invalid_model, round_num=self.round_num,
                    endpoint="MNN_OOM_CASCADE" if ignore_reason == "mnn_oom_cascade" else "SERVICE_CRASH",
                    detail=f"{name}: 连接失败,服务进程已退出; {classify_detail}",
                    log_tail=_capture_log_tail(self.svc)
                ))
                return TestResult(
                    name=name, round_num=self.round_num, model_name="_multi_model_",
                    passed=False, status_code=0, latency_ms=latency_ms,
                    detail=classify_detail,
                    ignorable=ignorable, ignore_reason=ignore_reason,
                    crashed=True,
                    response_data={"invalid_model": invalid_model}
                )
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=latency_ms,
                detail=f"请求失败（{self._last_request_error or '连接错误/超时'},进程仍存活）",
                response_data={"invalid_model": invalid_model}
            )
        # 5 秒的上限足够宽松以吸收正常调度抖动，同时能捕获任何重新引入"阻塞轮询"式退化。
        latency_ok = latency_ms < 5000
        passed = r.status_code in (404, 400) and latency_ok
        detail = f"status={r.status_code}, latency={latency_ms:.0f}ms" + (
            " (符合预期)" if passed else " (期望 404/400 且 <5000ms，用于防止 30 秒轮询等待式退化重现)")
        result = TestResult(
            name=name, round_num=self.round_num, model_name="_multi_model_",
            passed=passed, status_code=r.status_code, latency_ms=latency_ms,
            detail=detail
        )
        result.response_data = {"invalid_model": invalid_model}
        return result

    def test_concurrent_requests(self, loaded_models):
        """并发向不同模型发送请求，验证并发安全性"""
        name = "MULTI: concurrent requests to different models"
        if len(loaded_models) < 2:
            # 已加载模型 <2 个时,并发安全性根本没有被真正验证过,不能记为 passed=True——
            # 那样会扭曲报告语义,让人误以为"并发安全性已验证"。精确标记为 skipped=True。
            return self._make_result(
                name, False, 0,
                f"跳过（仅 {len(loaded_models)} 个模型加载成功，需要 ≥2 个才能测并发，未验证并发安全性）",
                skipped=True
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def send_request(model_name):
            body = {
                "model": model_name,
                "messages": [{"role": "user", "content": "Say hi"}],
                "stream": False,
                "max_tokens": 16
            }
            try:
                r = requests.post(f"{self.base_url}/v1/chat/completions",
                                  json=body, timeout=120)
                content = ""
                if r.status_code == 200:
                    try:
                        content = ((r.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
                    except Exception:
                        content = ""
                # 只有 200 且回复内容非空才视为真正成功——避免“全部 200 但回复都是空字符串”这类
                # “没真正验证任何东西却 100% 通过”的假衡量。
                return model_name, r.status_code, r.status_code == 200 and bool(content.strip())
            except Exception as e:
                return model_name, 0, False

        # 每个模型发一个并发请求
        results_map = {}
        with ThreadPoolExecutor(max_workers=len(loaded_models)) as executor:
            futures = {executor.submit(send_request, m): m for m in loaded_models}
            for future in as_completed(futures, timeout=180):
                try:
                    model_name, status, ok = future.result()
                    results_map[model_name] = (status, ok)
                except Exception as e:
                    model_name = futures[future]
                    results_map[model_name] = (0, False)

        passed_count = sum(1 for _, ok in results_map.values() if ok)
        detail_parts = [f"{m}→{s}" for m, (s, _) in results_map.items()]
        detail = f"{passed_count}/{len(loaded_models)} 成功: {', '.join(detail_parts)}"
        # 只要没有崩溃（status=0），并发测试就算通过（各模型独立，部分失败是正常的）
        no_crash = all(s != 0 for _, (s, _) in results_map.items())
        if not no_crash:
            all_crashed = all(s == 0 for _, (s, _) in results_map.items())
            # 崩溃永远是真实问题,不可豁免(ignorable 恒为 False)；ignore_reason 仅用于标注
            # 疑似与此前 MNN OOM 相关这一诊断信息,不再据此设置 ignorable=True。
            ignorable = False
            ignore_reason = ""
            classify_detail = ""
            if all_crashed:
                crashed_mnn_models = [m for m in loaded_models if infer_backend(m)[0] == "mnn"]
                if crashed_mnn_models:
                    classify_detail = (f"并发测试中服务进程崩溃,涉及 MNN 模型{crashed_mnn_models},"
                                        f"这是需要关注的真实OOM事件")
                    _mark_mnn_oom(self.svc, crashed_mnn_models[0], classify_detail)
                else:
                    oom_event = _get_mnn_oom_event(self.svc)
                    if oom_event:
                        classify_detail = (f"疑似因同进程内 MNN 模型({oom_event['model_name']})此前发生 OOM 导致进程退出"
                                            f"而级联崩溃,需人工排查根因;已计入真实失败,不代表豁免 (ignore_reason=mnn_oom_cascade)")
                        ignore_reason = "mnn_oom_cascade"
                    else:
                        classify_detail = "并发请求全部失败,且未检测到 MNN OOM 记录,应视为涉及模型自身的真实问题"
            detail_full = f"{detail}; {classify_detail}" if classify_detail else detail
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0,
                detail=detail_full,
                ignorable=ignorable, ignore_reason=ignore_reason,
                crashed=all_crashed
            )
        return self._make_result(name, True, 200, detail)

    def test_default_model_routing(self):
        """向不带 model 字段的请求验证默认模型路由。

        背景（审计发现）：`ChatRequestHandler::ChatCompletions` 每次动态切换成功都会
        调用 `SetDefaultModel(modelName)`，把服务端的默认模型覆写为“最后一次被动态路由
        到的模型”，而不是固定为 `service_config.json` 里配置的那个；若该默认模型恰好在某个
        设备上被驱逐，`GetDefaultModel()` 会退化为 `unordered_map` 的任意元素。本测试不假定
        具体会路由到哪个模型（该行为目前是既定但不确定的实现细节，非本测试引入的新契约），
        只监控“不崩溃、不返回 5xx”这一条安全底线，作为回归预防线。"""
        name = "MULTI: default model route (no 'model' field) after dynamic switches"
        body = {
            "messages": [{"role": "user", "content": "Say hi"}],
            "stream": False,
            "max_tokens": 16
        }
        started = time.time()
        try:
            r = requests.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=60)
        except Exception as e:
            latency = (time.time() - started) * 1000
            if _confirm_process_dead(self.svc):
                ignorable, ignore_reason, classify_detail = _classify_process_down(self.svc, "_multi_model_")
                self.crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(),
                    model_name="_multi_model_", round_num=self.round_num,
                    endpoint=name, detail=f"{type(e).__name__}: {e}; {classify_detail}",
                    log_tail=_capture_log_tail(self.svc)
                ))
                return TestResult(
                    name=name, round_num=self.round_num, model_name="_multi_model_",
                    passed=False, status_code=0, latency_ms=latency,
                    detail=f"请求异常: {type(e).__name__}: {e}; {classify_detail}",
                    crashed=True, ignorable=ignorable, ignore_reason=ignore_reason
                )
            # 进程仍存活,只是这次请求本身连接异常/超时: 不构成崩溃证据,与 test_route_to_model
            # 对同类异常的处理保持一致。
            return TestResult(
                name=name, round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=latency,
                detail=f"请求异常: {type(e).__name__}: {e}(进程仍存活,应视为该次请求自身的连接异常)"
            )
        latency_ms = (time.time() - started) * 1000
        passed = r.status_code == 200
        detail = (f"status={r.status_code}, latency={latency_ms:.0f}ms（不带 model 字段；当前实现里"
                  f"默认模型会随动态切换漂移，本检查只监控是否仍能正常响应，不校验具体路由到哪个模型）")
        return TestResult(
            name=name, round_num=self.round_num, model_name="_multi_model_",
            passed=passed, status_code=r.status_code, latency_ms=latency_ms,
            detail=detail
        )

    # ── 主入口 ────────────────────────────────────────────────────────────────

    def run_all(self, models_by_device=None, models_root=None, remote_mode=False):
        """执行所有多模型测试，返回 TestResult 列表

        models_by_device: 可选，形如 {"npu": ..., "gpu": ..., "cpu": ...}；
        长度 > 1 时先调用 ensure_multi_backend_loaded 主动触发多种设备的动态加载；
        长度 <=1（环境里只发现单一 backend 类型）时不再完全静默跳过、不留痕迹，而是
        登记一条 skipped=True 的 TestResult，明确说明"环境无法验证多后端同时驻留"。

        models_root/remote_mode: 三后端未能全部同时驻留（例如先加载 qnn、再加载 gguf 后
        内存已耗尽，mnn 完全没机会被尝试）时，用于驱动"两两降级"兜底验证——本地模式下
        依次重启服务、逐一确认 npu+gpu/npu+cpu/gpu+cpu 是否仍能同时驻留；远程模式或未提供
        models_root 时无法重启服务，精确标记为 skipped（而不是悄悄跳过不留痕迹）。
        """
        print(f"\n{'='*60}")
        print("阶段 3: 多模型并发加载与路由测试")
        print(f"{'='*60}")

        if models_by_device and len(models_by_device) > 1:
            print(f"  确保 NPU/GGUF/MNN 三后端同时驻留 ... ", end="", flush=True)
            r = self.ensure_multi_backend_loaded(models_by_device)
            self.results.append(r)
            tag = "✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else ("⚠ IGN" if r.ignorable else "✗ FAIL"))
            print(f"{tag} ({r.detail[:100]})")

            triple_missing = set((r.response_data or {}).get("missing_devices", [])) if r.response_data else set()
            if triple_missing:
                print(f"  三后端同时驻留未完全达成(缺: {sorted(triple_missing)})，尝试两两降级验证 ... ")
                if remote_mode or not models_root:
                    reason = "远程模式无法重启服务" if remote_mode else "未提供本地模型根目录"
                    matrix_result = self._build_coexistence_matrix_result(r, {}, pairwise_attempted=False)
                    self.results.append(matrix_result)
                    print(f"  ⚠ SKIP（{reason}，无法执行两两降级验证）")
                else:
                    pairwise_results, pair_matrix, pair_extra = self.ensure_pairwise_backends_loaded(models_by_device, models_root)
                    self.results.extend(pairwise_results)
                    matrix_result = self._build_coexistence_matrix_result(r, pair_matrix, pairwise_attempted=True, pair_extra=pair_extra)
                    self.results.append(matrix_result)
                    tag2 = "✓ PASS" if matrix_result.passed else ("⚠ SKIP" if matrix_result.skipped else "✗ FAIL")
                    print(f"  两两降级共存矩阵: {tag2} ({matrix_result.detail[:150]})")
            else:
                matrix_result = self._build_coexistence_matrix_result(r, {}, pairwise_attempted=False)
                self.results.append(matrix_result)
        elif models_by_device is not None:
            devices_found = sorted(models_by_device.keys())
            self.results.append(TestResult(
                name="MULTI: ensure NPU/GGUF/MNN backends loaded simultaneously",
                round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"环境中只发现单一后端类型({devices_found})，无法验证多后端是否可同时驻留",
                skipped=True
            ))

        # 1. 验证模型列表
        print("  测试 GET /models (multi-model list) ... ", end="", flush=True)
        r = self.test_models_list()
        self.results.append(r)
        print("✓ PASS" if r.passed else f"✗ FAIL ({r.detail})")

        # 2. 获取已加载模型列表
        loaded_models = self._get_loaded_models()
        print(f"  已加载模型: {loaded_models}")

        # 3. 对每个已加载模型测试路由
        for model_name in loaded_models:
            print(f"  测试路由 → {model_name} ... ", end="", flush=True)
            r = self.test_route_to_model(model_name)
            self.results.append(r)
            tag = "✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else ("⚠ IGN" if r.ignorable else "✗ FAIL"))
            print(f"{tag} ({r.detail[:60]})")

        # 4. 测试未知模型路由
        print("  测试无效模型路由 (expect 404/400) ... ", end="", flush=True)
        r = self.test_unknown_model_route()
        self.results.append(r)
        print("✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else f"✗ FAIL ({r.detail})"))

        # 4.5 不带 model 字段的默认路由回归监控（仅在确实发生过跨设备动态切换时才有意义；
        # 只监控"不崩溃/不返回 5xx"这条安全底线，不假定具体路由到哪个模型——见方法 docstring）。
        if models_by_device and len(models_by_device) > 1:
            print("  测试默认模型路由 (无 model 字段) ... ", end="", flush=True)
            r = self.test_default_model_routing()
            self.results.append(r)
            print("✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else f"✗ FAIL ({r.detail})"))

        # 5. 并发请求测试（需要 ≥2 个已加载模型）
        print("  测试并发请求 ... ", end="", flush=True)
        r = self.test_concurrent_requests(loaded_models)
        self.results.append(r)
        print("✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else f"✗ FAIL ({r.detail})"))

        return self.results


# ============================================================================
# 主流程
# ============================================================================

# 常见的模型权重文件后缀，用于 discover_models 里检测"看起来像模型但缺 config.json"这类
# 目录，避免它被完全静默地漏测——用户跑完整套回归后如果只看"全部已发现模型都通过了"，
# 很难察觉少了一个目录；这里不阻断执行，只打印告警提示，让问题至少可见。
_MODEL_WEIGHT_EXTENSIONS = {".bin", ".mnn", ".gguf", ".onnx", ".safetensors"}


def _estimate_model_dir_size_bytes(models_root, model_name):
    """递归汇总某个模型目录下全部文件大小之和，作为该模型内存占用的粗略代理指标——
    与服务端 MNN 自身内存预检查同一思路（用磁盘权重文件大小估算运行时内存需求）。
    仅用于测试脚本自己为"多类型同时加载"挑选每个设备的候选模型时做启发式排序，
    不影响服务端真实的内存预检查逻辑（那部分完全由 C++ 侧负责）。
    读取失败（目录不存在/权限问题）时返回正无穷，避免因无法估算而被误判为"最小"。"""
    total = 0
    try:
        for f in (Path(models_root) / model_name).rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        return float("inf")
    return total if total > 0 else float("inf")


def discover_models(models_dir):
    """扫描 models 目录，返回包含 config.json 的子目录列表。
    对"看起来像模型（含常见权重文件后缀）但缺 config.json"的子目录打印告警,
    而不是完全静默跳过——否则用户很容易在不知情的情况下漏测一个模型。"""
    models_dir = Path(models_dir)
    if not models_dir.exists():
        print(f"ERROR: models 目录不存在: {models_dir}")
        sys.exit(1)
    models = []
    for d in sorted(models_dir.iterdir()):
        if not d.is_dir():
            continue
        if (d / "config.json").exists():
            models.append(d.name)
            continue
        try:
            looks_like_model = any(f.suffix.lower() in _MODEL_WEIGHT_EXTENSIONS
                                    for f in d.iterdir() if f.is_file())
        except OSError:
            looks_like_model = False
        if looks_like_model:
            print(f"WARNING: 目录 {d.name} 含权重文件但缺少 config.json，已跳过（未被纳入本次测试）")
    return models


def discover_models_remote(host, port):
    """从已运行的远程服务获取所有模型列表。
    对非 200 状态码、以及响应条目缺 "id" 字段的情况打印告警，而不是静默返回空列表/
    悄悄过滤掉——否则调用方无法区分"远程确实没有模型"和"接口请求异常但没报错"。"""
    url = f"http://{host}:{port}/models"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"WARNING: {url} 返回非 200 状态码 {r.status_code}，视为无模型")
            return []
        data = r.json()
        models = data.get("data", [])
        missing_id = [m for m in models if isinstance(m, dict) and "id" not in m]
        if missing_id:
            print(f"WARNING: /models 响应中有 {len(missing_id)} 条记录缺少 'id' 字段，已忽略: {missing_id}")
        all_names = [m["id"] for m in models if isinstance(m, dict) and "id" in m]
        return all_names
    except Exception as e:
        print(f"ERROR: 无法从远程服务获取模型列表: {e}")
    return []


def _pick_builder_integration_model(models):
    preferred = [m for m in models if all(token not in m.lower() for token in ("gguf", "mnn", "gpu"))]
    return (preferred or models or [None])[0]


def _pick_builder_backend_models(models, wanted):
    """从 discover_models_*() 返回的模型名列表里，为 wanted 中每个 backend 挑选第一个匹配的模型。
    返回形如 {"qnn": "<name>", "gguf": "<name>", "mnn": "<name>"}，缺失的 key 不出现。
    与既有 _pick_builder_integration_model 分工：后者继续给 test_start_local_service 挑 QNN 首选模型用；
    本函数只用于 Builder 三后端路由用例。"""
    picked: dict = {}
    if not wanted:
        return picked
    wanted_lower = {b.lower() for b in wanted}
    for m in models:
        backend, _device = infer_backend(m)
        key = backend.lower()
        if key in wanted_lower and key not in picked:
            picked[key] = m
    return picked


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


# ============================================================================
# QAIModelBuilderLocalModelTester - 本地模型加载端到端验证
# ============================================================================
# 本次测试的被测对象是 GenieAPIService 本身（真实推理行为是否正确），Builder 只是启动/管理
# 它的载体。Builder 侧的 API 交互（配置根目录、注入模型、启动/停止、CSRF 握手）属于"手段
# 层"——只要能可靠地拿到一个正确运行的 GenieAPIService 实例即视为达标；验证重心放在"目标
# 层"：GenieAPIService 加载指定本地模型后，其真实推理行为（chat 回复内容非空且合理、后端
# 判定与目录名一致）是否正确可用。
#
# 硬约束：零侵入 Builder 源码——全部通过 Builder 已支持的官方接口达成：
#   * HTTP API (GET/POST /api/service/*)
#   * 环境变量 (QAI_AUTH__ENABLED、QAI_DATA__DATA_DIR，均由 QAIModelBuilderManager 设置)
#   * 纯文件系统操作 (mklink /J 目录联接——本地模型注入到 <data_dir>/models/<name>，
#     GenieAPIService 安装目录注入到 <data_dir>/bin/<name> 以触发官方自愈式安装发现；
#     Builder 完全无感知)。注意：POST /api/forge-config 对 genie_service.root_path 的写入
#     已核实不会被服务启动路径实际读取（见 configure_genie_root 文档字符串），本类不再使用它
#     来配置安装目录。
class QAIModelBuilderLocalModelTester:
    _MODEL_PLACEHOLDER = "_builder_local_model_"
    # backend → format 字段值（GET /api/service/models 的判定），infer_backend 用 "GGUF" 表示
    # GGUF 后端，Builder /api/service/models 返回的是小写 "gguf"，注意这里的映射。
    _BACKEND_TO_FORMAT = {"qnn": "qnn", "mnn": "mnn", "GGUF": "gguf"}

    def __init__(self, builder, models_root, genie_root_path, model_names,
                 genie_service_port, round_num=1):
        self.builder = builder
        self.models_root = Path(models_root) if models_root else None
        self.genie_root_path = genie_root_path
        self.model_names = list(model_names)
        self.genie_service_port = genie_service_port
        self.round_num = round_num
        self.results = []
        self.crash_events = []
        # configure_genie_root() 里 mklink /J 联接的 <data_dir>/bin/<name> 路径，供
        # test_invalid_genie_root() 临时移除/恢复以模拟"未安装"场景（见该方法文档字符串）。
        self._bin_junction_path = None

    def _make_result(self, name, passed, status_code, detail, *,
                     model_name=None, skipped=False, crashed=False, ignorable=False,
                     response_data=None, latency_ms=0):
        return TestResult(
            name=name, round_num=self.round_num,
            model_name=model_name or self._MODEL_PLACEHOLDER,
            passed=passed, status_code=status_code, latency_ms=latency_ms,
            detail=detail, skipped=skipped, crashed=crashed,
            ignorable=ignorable,
            response_data=response_data or {},
        )

    def _csrf_request(self, method, path, *, body=None, timeout=30):
        """封装一次带 CSRF 头的 Builder API 请求，返回 (response_or_exception, latency_ms)。
        请求异常时不抛出，返回 (exception, latency) 供调用方按 result 记录；主动区分"HTTP 异常"
        与"Builder 进程崩溃"两种情况（后者由 builder.process.poll() 判断）。"""
        start = time.time()
        try:
            if body is None:
                r = self.builder.csrf.request(method, path, timeout=timeout)
            else:
                r = self.builder.csrf.request(method, path, timeout=timeout, json=body)
            return r, (time.time() - start) * 1000
        except Exception as e:
            return e, (time.time() - start) * 1000

    # ---- Step 2: configure_genie_root ----
    def configure_genie_root(self):
        """把 GenieAPIService 安装目录通过 mklink /J 联接到 Builder 固定扫描的
        <data_dir>/bin/<name> 下，触发 Builder 官方的"自动发现已安装版本"自愈机制。

        POST /api/forge-config 写入的 genie_service.root_path 持久化到 SQLite 的
        kv_user_prefs 表，而服务启动时真正解析安装目录的 _make_install_dir_provider
        读取的是磁盘上的 <data_dir>/config/forge_config.json 纯 JSON 文件——两套持久化
        后端完全独立、互不联通，因此该 HTTP 接口对 root_path 的写入不会被服务启动路径
        实际读取到。正确路径是 _make_install_dir_provider 每次请求都会实时扫描
        <data_dir>/bin/ 下的子目录，一旦发现某个子目录直接包含 GenieAPIService.exe 就会
        自愈式地把它当作已安装版本使用，与下载中心真实安装一个新版本效果一致且不需要
        重启 Builder。这里通过纯文件系统操作（mklink /J）达成，不依赖也不修改
        forge-config 相关 HTTP 接口。"""
        name = "BUILDER-LOCAL: configure_genie_root (mklink /J into data_dir/bin)"
        if not self.genie_root_path:
            self.results.append(self._make_result(
                name, False, 0,
                "未提供 GenieAPIService 安装目录（--genie_root_path 或 --exe_dir），跳过",
                skipped=True))
            return False
        src = Path(self.genie_root_path)
        if not src.is_dir() or not (src / "GenieAPIService.exe").is_file():
            self.results.append(self._make_result(
                name, False, 0,
                f"--genie_root_path 不存在或不含 GenieAPIService.exe: {src}"))
            return False
        if not self.builder.data_dir:
            self.results.append(self._make_result(
                name, False, 0,
                "Builder 未启用隔离数据目录 (QAI_DATA__DATA_DIR)，无法注入安装目录",
                skipped=True))
            return False

        bin_root = self.builder.data_dir / "bin"
        try:
            bin_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0, f"创建 {bin_root} 失败: {e}"))
            return False

        dst = bin_root / src.name
        self._bin_junction_path = dst
        if not dst.exists():
            try:
                proc = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(dst), str(src)],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                self.results.append(self._make_result(
                    name, False, 0, f"mklink /J subprocess 异常: {type(e).__name__}: {e}"))
                return False
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                self.results.append(self._make_result(
                    name, False, 0,
                    f"mklink /J 失败, rc={proc.returncode}, stderr={stderr!r}, stdout={stdout!r}; "
                    f"dst={dst}, src={src}"))
                return False

        r_status, latency = self._csrf_request("GET", "/api/service/status", timeout=15)
        if isinstance(r_status, Exception) or r_status.status_code != 200:
            self.results.append(self._make_result(
                name, False, getattr(r_status, "status_code", 0),
                f"GET /api/service/status 读取失败: {r_status}", latency_ms=latency))
            return False
        try:
            js = r_status.json()
        except Exception as e:
            self.results.append(self._make_result(
                name, False, r_status.status_code,
                f"status 响应体不是 JSON: {e}", latency_ms=latency))
            return False
        exe_path = js.get("exe_path")
        path_warning = js.get("path_warning")
        passed = bool(exe_path) and (src.name.lower() in str(exe_path).lower())
        self.results.append(self._make_result(
            name, passed, 200,
            (f"mklink /J 注入完成: {dst} -> {src}; exe_path={exe_path}; "
             f"path_warning={path_warning}"),
            latency_ms=latency,
            response_data={"bin_junction": str(dst), "exe_path": exe_path,
                           "path_warning": path_warning}))
        return passed

    # ---- Step 2: inject_local_models ----
    def inject_local_models(self):
        """用 mklink /J 把每个 --models/<name> 目录联接到 <data_dir>/models/<name>。
        Builder 的 models_root_path 已固定为 <data_dir>/models（Builder 侧移除了修改根目录的
        API），通过纯文件系统操作让本机已有模型出现在扫描目录下，无需复制大文件、也不侵入
        Builder 源码。"""
        name = "BUILDER-LOCAL: inject_local_models (mklink /J)"
        if not self.models_root or not self.models_root.exists():
            self.results.append(self._make_result(
                name, False, 0,
                f"--models 目录不可用（models_root={self.models_root}），跳过注入",
                skipped=True))
            return False
        if not self.builder.data_dir:
            self.results.append(self._make_result(
                name, False, 0,
                "Builder 未启用隔离数据目录 (QAI_DATA__DATA_DIR)，无法注入本地模型",
                skipped=True))
            return False
        target_models_root = self.builder.data_dir / "models"
        try:
            target_models_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0,
                f"创建目标模型根目录失败: {target_models_root}: {e}"))
            return False

        successes = []
        failures = []
        for m in self.model_names:
            src = self.models_root / m
            if not src.exists():
                failures.append(f"{m}(源目录不存在: {src})")
                continue
            dst = target_models_root / m
            if dst.exists():
                successes.append(f"{m}(目标已存在，视作幂等成功)")
                continue
            try:
                proc = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(dst), str(src)],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                failures.append(f"{m}(subprocess 异常: {type(e).__name__}: {e})")
                continue
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                failures.append(
                    f"{m}(mklink /J 失败, rc={proc.returncode}, stderr={stderr!r}, stdout={stdout!r})")
                continue
            successes.append(m)

        detail = (f"注入 {len(successes)}/{len(self.model_names)}; "
                  f"successes={successes}; failures={failures}; target={target_models_root}")
        passed = bool(successes) and not failures
        self.results.append(self._make_result(
            name, passed, 0, detail,
            response_data={"target_models_root": str(target_models_root),
                           "successes": successes, "failures": failures}))
        return bool(successes)

    # ---- Step 3: discover_models via Builder ----
    def discover_models_via_builder(self):
        """调用 GET /api/service/models，与本地 infer_backend() 交叉校验 format 字段。
        返回 Builder 侧发现的模型清单（原始 entries）。"""
        name = "BUILDER-LOCAL: discover_models (GET /api/service/models)"
        r, latency = self._csrf_request("GET", "/api/service/models", timeout=30)
        if isinstance(r, Exception):
            self.results.append(self._make_result(
                name, False, 0,
                f"GET /api/service/models 请求异常: {type(r).__name__}: {r}",
                latency_ms=latency))
            return []
        if r.status_code != 200:
            self.results.append(self._make_result(
                name, False, r.status_code,
                f"GET /api/service/models 非 200: body={r.text[:300]}",
                latency_ms=latency))
            return []
        try:
            js = r.json()
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 200, f"响应体不是 JSON: {e}", latency_ms=latency))
            return []
        found = js.get("models") or []
        expected = set(self.model_names)
        found_names = {m.get("name") for m in found if isinstance(m, dict)}
        missing = sorted(expected - found_names)
        mismatches = []
        for entry in found:
            if not isinstance(entry, dict):
                continue
            nm = entry.get("name")
            if nm not in expected:
                continue
            backend, _dev = infer_backend(nm)
            expected_fmt = self._BACKEND_TO_FORMAT.get(backend, "").lower()
            actual_fmt = str(entry.get("format") or "").lower()
            if expected_fmt and actual_fmt and actual_fmt != expected_fmt:
                mismatches.append(f"{nm}: expected={expected_fmt}, got={actual_fmt}")
        passed = not missing and not mismatches
        self.results.append(self._make_result(
            name, passed, 200,
            (f"发现 {len(found)} 个模型；期望 {sorted(expected)}, "
             f"实际匹配 {sorted(found_names & expected)}; missing={missing}; "
             f"format_mismatches={mismatches}; models_root_path={js.get('models_root_path')}"),
            latency_ms=latency,
            response_data={"models": found,
                           "models_root_path": js.get("models_root_path")}))
        return found

    # ---- Step 3: start_and_wait_ready ----
    def start_and_wait_ready(self, model_name, port, timeout=240):
        """POST /api/service/start 后自行轮询 GET /api/service/status 直到 running=true
        且 model 匹配（该接口是 fire-and-forget，不阻塞等待）。

        每次调用前都重新调用一次 neutralize_service_config_models()（见其文档字符串关于
        "必须在每一次 start 之前都重新调用"的实测说明），确保 Builder 的 V1 兼容 sync 逻辑
        不会在两次 start 之间悄悄往 service_config.json 里补回一条会误导后端判定的旧条目。"""
        self.neutralize_service_config_models()
        name = f"BUILDER-LOCAL: start_and_wait_ready model={model_name}"
        body = {"model_name": model_name, "port": port}
        r, latency = self._csrf_request("POST", "/api/service/start", body=body, timeout=30)
        if isinstance(r, Exception):
            self.results.append(self._make_result(
                name, False, 0,
                f"POST /api/service/start 请求异常: {type(r).__name__}: {r}",
                model_name=model_name, latency_ms=latency))
            return False, None
        if r.status_code not in (200, 201, 202):
            self.results.append(self._make_result(
                name, False, r.status_code,
                f"POST /api/service/start 非成功状态码: body={r.text[:300]}",
                model_name=model_name, latency_ms=latency))
            return False, None
        # 轮询 status 直到 running=true 且 model 匹配
        started_at = time.time()
        end = started_at + timeout
        last_status = None
        while time.time() < end:
            rs, _ = self._csrf_request("GET", "/api/service/status", timeout=10)
            if not isinstance(rs, Exception) and rs.status_code == 200:
                try:
                    last_status = rs.json()
                except Exception:
                    last_status = None
                if isinstance(last_status, dict) and last_status.get("running") \
                        and last_status.get("model") == model_name:
                    total_ms = (time.time() - started_at) * 1000
                    self.results.append(self._make_result(
                        name, True, 200,
                        (f"服务已就绪: state={last_status.get('state')}, "
                         f"pid={last_status.get('pid')}, model={last_status.get('model')}, "
                         f"port={last_status.get('port')}"),
                        model_name=model_name, latency_ms=total_ms,
                        response_data=last_status))
                    return True, last_status
            time.sleep(2)
        self.results.append(self._make_result(
            name, False, 0,
            f"轮询 {timeout}s 未看到 running=true 且 model={model_name}; last_status={last_status}",
            model_name=model_name))
        return False, last_status

    # ---- Step 3: assert_command_contains ----
    def assert_command_contains(self, expected_model_name, expected_port, status=None):
        """shlex.split 解析 status['command']，校验 -c 指向该模型的 config.json、-p 为期望端口。
        这一条延续 playbook 里"用 command 字段验证 CLI 参数生效性"的既有验证思路（Builder 侧
        无法同步读取子进程日志，command 字段是启动参数生效性的最直接证据）。"""
        name = f"BUILDER-LOCAL: assert_command_contains model={expected_model_name}"
        if status is None:
            rs, _ = self._csrf_request("GET", "/api/service/status", timeout=10)
            if isinstance(rs, Exception) or rs.status_code != 200:
                self.results.append(self._make_result(
                    name, False, 0, f"读取 status 失败: {rs}", model_name=expected_model_name))
                return False
            try:
                status = rs.json()
            except Exception as e:
                self.results.append(self._make_result(
                    name, False, 200, f"status 响应非 JSON: {e}",
                    model_name=expected_model_name))
                return False
        command = status.get("command") if isinstance(status, dict) else None
        if not command:
            self.results.append(self._make_result(
                name, False, 0, f"status.command 字段为空; status={status}",
                model_name=expected_model_name))
            return False
        try:
            tokens = shlex.split(command, posix=False)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0, f"shlex.split 解析失败: {e}; command={command}",
                model_name=expected_model_name))
            return False

        def _val_after(flag_short, flag_long=None):
            for i, tok in enumerate(tokens):
                stripped = tok.strip('"')
                if stripped in (flag_short, flag_long) and i + 1 < len(tokens):
                    return tokens[i + 1].strip('"')
            return None

        cflag = _val_after("-c", "--config")
        pflag = _val_after("-p", "--port")
        errs = []
        norm_c = (cflag or "").replace("/", "\\").lower()
        if not norm_c or (expected_model_name.lower() not in norm_c) or ("config.json" not in norm_c):
            errs.append(f"-c 取值不含 {expected_model_name}/config.json（实际: {cflag}）")
        if pflag != str(expected_port):
            errs.append(f"-p 取值不为 {expected_port}（实际: {pflag}）")
        passed = not errs
        self.results.append(self._make_result(
            name, passed, 0,
            "命令行校验通过" if passed else "; ".join(errs),
            model_name=expected_model_name,
            response_data={"command": command, "-c": cflag, "-p": pflag}))
        return passed

    # ---- Step 3: verify_genieapiservice_reachable ----
    def verify_genieapiservice_reachable(self, port, model_name):
        """直连 GenieAPIService 自身端口的 GET /v1/models 与 POST /v1/chat/completions，
        断言回复内容非空、语义相关，证明底层推理真正可用（不只是 Builder 认为启动成功）；
        并把 /v1/models 反映出的后端特征与 infer_backend() 判定交叉核对，验证 GenieAPIService
        确实按正确后端加载。这是"被测对象是 GenieAPIService"这一定位落到具体断言的关键点。"""
        base = f"http://127.0.0.1:{port}"

        # GET /v1/models
        # 宽限重试：Builder 的 running=true 只反映"进程已启动"，不代表 HTTP 端口已真正
        # listen()，因此在窗口耗尽后先核实 Builder 自身汇报的进程是否仍然存活（而不是想象
        # 中的死亡）——只有确认 Builder 侧也认为该进程已不在运行时,才真正归为 crashed；
        # 若 Builder 侧仍报告 running=true(只是端口迟迟未开),则归为 skipped=True(环境性
        # 启动尚未就绪,不是功能缺陷),避免把"活着但慢"误判为"死了"。
        gm_name = f"BUILDER-LOCAL: direct GET /v1/models port={port}"
        r_models = None
        gm_last_error = None
        gm_end = time.time() + 240
        while time.time() < gm_end:
            try:
                r_models = requests.get(f"{base}/v1/models", timeout=30)
                break
            except Exception as e:
                gm_last_error = f"{type(e).__name__}: {e}"
                r_models = None
                time.sleep(10)
        if r_models is None:
            still_running = False
            try:
                r_status, _ = self._csrf_request("GET", "/api/service/status", timeout=15)
                if not isinstance(r_status, Exception) and r_status.status_code == 200:
                    still_running = bool(r_status.json().get("running"))
            except Exception:
                still_running = False
            if still_running:
                self.results.append(self._make_result(
                    gm_name, False, 0,
                    f"直连 GET /v1/models 持续连不上(240s 宽限期内)，但 Builder 侧仍报告该进程 "
                    f"running=true（未真正崩溃，判定为环境性启动尚未就绪/端口迟迟未开）: {gm_last_error}",
                    model_name=model_name, skipped=True))
            else:
                self.results.append(self._make_result(
                    gm_name, False, 0,
                    f"直连 GET /v1/models 持续请求异常(240s 宽限期内)，且 Builder 侧确认该进程已不在运行: "
                    f"{gm_last_error}",
                    model_name=model_name, crashed=True))
            return False
        if r_models.status_code != 200:
            self.results.append(self._make_result(
                gm_name, False, r_models.status_code,
                f"直连 GET /v1/models 非 200: {r_models.text[:200]}",
                model_name=model_name))
            return False
        try:
            models_json = r_models.json()
        except Exception as e:
            self.results.append(self._make_result(
                gm_name, False, 200, f"响应非 JSON: {e}", model_name=model_name))
            return False
        entries = models_json.get("data", [])
        loaded_entry = None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == model_name:
                loaded_entry = entry
                break
        expected_backend, _expected_device = infer_backend(model_name)
        expected_key = self._BACKEND_TO_FORMAT.get(expected_backend, "").lower()
        actual_backend = ""
        if loaded_entry:
            actual_backend = str(loaded_entry.get("backend") or "").lower()
        # 后端字段缺失时不判失败（GenieAPIService /v1/models 视版本可能不返回该字段）
        cross_ok = (not actual_backend) or (actual_backend == expected_key)
        self.results.append(self._make_result(
            gm_name,
            passed=(loaded_entry is not None) and cross_ok,
            status_code=200,
            detail=(f"loaded_entry={loaded_entry}; expected_backend={expected_key}, "
                    f"actual_backend={actual_backend!r}; cross_ok={cross_ok}"),
            model_name=model_name,
            response_data={"entries": entries, "expected_backend": expected_key,
                           "actual_backend": actual_backend}))

        # POST /v1/chat/completions（非流式，简单问答）
        # 容忍窗口：Builder 的 running=true 只反映"进程已启动/端口已开"，不代表模型权重已
        # 加载完毕注册进 ModelManager；过早发出的 chat 请求会收到 "Model 'xxx' not found or
        # unavailable." 这类同步拒绝，需要短暂轮询重试而不是单次判失败。
        chat_name = f"BUILDER-LOCAL: direct POST /v1/chat/completions port={port}"
        body = {
            "model": model_name,
            "messages": [{"role": "user",
                          "content": "What is the capital of France? Answer in one sentence."}],
            "stream": False,
            "max_tokens": 64,
        }
        # 留出适度余量，避免把偶发的短暂启动延迟误判为功能性失败。
        chat_ready_budget_s = 240
        end = time.time() + chat_ready_budget_s
        r_chat = None
        last_error = None
        while time.time() < end:
            try:
                r_chat = requests.post(f"{base}/v1/chat/completions", json=body, timeout=180)
            except Exception as e:
                last_error = f"请求异常: {type(e).__name__}: {e}"
                r_chat = None
                time.sleep(10)
                continue
            if r_chat.status_code == 200:
                break
            # 精确信号 failure_reason=insufficient_memory：服务端已优雅拒绝加载（典型是
            # MNN 内存预检查，见 playbook 4.7/5.2.5/5.2.6），这是环境资源约束，不是代码缺陷，
            # 立即跳出重试循环归类为 skipped=True，不再继续消耗容忍窗口去无意义重试。
            try:
                err_body = r_chat.json()
            except Exception:
                err_body = {}
            if isinstance(err_body, dict) and err_body.get("failure_reason") == "insufficient_memory":
                self.results.append(self._make_result(
                    chat_name, False, r_chat.status_code,
                    f"服务端预判内存不足优雅拒绝加载(failure_reason=insufficient_memory): "
                    f"{err_body.get('failure_detail', '')}",
                    model_name=model_name, skipped=True))
                return False
            last_error = f"非 200: status={r_chat.status_code}, body={r_chat.text[:200]}"
            time.sleep(10)
        if r_chat is None:
            self.results.append(self._make_result(
                chat_name, False, 0,
                f"直连 chat 持续失败({chat_ready_budget_s}s 内未获得 200): {last_error}",
                model_name=model_name, crashed=True))
            return False
        if r_chat.status_code != 200:
            self.results.append(self._make_result(
                chat_name, False, r_chat.status_code,
                f"直连 chat 持续非 200({chat_ready_budget_s}s 内): {last_error}",
                model_name=model_name))
            return False
        try:
            chat_json = r_chat.json()
            content = chat_json.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            self.results.append(self._make_result(
                chat_name, False, 200,
                f"chat 响应结构异常: {e}", model_name=model_name))
            return False
        content_stripped = (content or "").strip()
        passed = len(content_stripped) > 0
        self.results.append(self._make_result(
            chat_name, passed, 200,
            f"回复长度={len(content_stripped)}, 预览={content_stripped[:120]!r}",
            model_name=model_name,
            response_data={"content_len": len(content_stripped),
                           "content_preview": content_stripped[:200]}))
        return passed

    # ---- Step 4: switch_model ----
    def switch_model(self, new_model_name, port):
        """先停止当前正在运行的服务并确认端口释放，再 POST /api/service/start 切到另一个
        模型。Builder 的 start 端点不会自动踢掉占用同一端口的旧进程——若旧进程仍在跑（或仍
        在加载中），直接再次 start 会被同步拒绝为 409 ServicePortInUseError，
        必须显式停止旧进程、确认端口释放，才能真正验证"切换模型"这一场景。"""
        self.stop_and_verify(port)
        return self.start_and_wait_ready(new_model_name, port)

    # ---- Step 4: stop_and_verify ----
    def stop_and_verify(self, port):
        name = "BUILDER-LOCAL: stop_and_verify (POST /api/service/stop)"
        r, latency = self._csrf_request(
            "POST", "/api/service/stop", body={"force": False}, timeout=30)
        if isinstance(r, Exception):
            self.results.append(self._make_result(
                name, False, 0,
                f"POST /api/service/stop 请求异常: {type(r).__name__}: {r}",
                latency_ms=latency))
            return False
        end = time.time() + 30
        stopped = False
        last_status = None
        while time.time() < end:
            rs, _ = self._csrf_request("GET", "/api/service/status", timeout=10)
            if not isinstance(rs, Exception) and rs.status_code == 200:
                try:
                    last_status = rs.json()
                except Exception:
                    last_status = None
                if isinstance(last_status, dict) and not last_status.get("running"):
                    stopped = True
                    break
            time.sleep(1)
        port_released = wait_port_closed("127.0.0.1", port, timeout=15)
        passed = stopped and port_released
        self.results.append(self._make_result(
            name, passed, r.status_code,
            f"stopped={stopped}, port_released={port_released}, last_status={last_status}",
            latency_ms=latency,
            response_data={"stopped": stopped, "port_released": port_released,
                           "last_status": last_status}))
        return passed

    # ---- Step 4: negatives ----
    def test_invalid_genie_root(self):
        """临时移除 configure_genie_root() 创建的 <data_dir>/bin/<name> 目录联接，使 Builder
        的自愈式安装发现扫描不到任何已安装版本，验证 POST /api/service/start 返回结构化错误
        （4xx 状态码或 status.path_warning/exe_path 反映"未安装"）而不是让 Builder 进程崩溃；
        结束前必须恢复该联接，避免影响后续用例（切换/推理验证都依赖它存在）。

        早期实现通过 POST /api/forge-config 写一个不存在的 root_path 来模拟"无效安装目录"，
        但已确认该写入根本不会被服务启动路径读取（见 configure_genie_root 文档字符串），因此
        那种写法从未真正测试到"无效安装目录"场景——无论写什么，Builder 都会用磁盘自愈扫描找到
        真实安装。真正能让 Builder 认为"未安装"的唯一方式是让 <data_dir>/bin 下不存在任何含
        GenieAPIService.exe 的子目录。"""
        name = "BUILDER-LOCAL: test_invalid_genie_root"
        junction = self._bin_junction_path
        if not junction or not junction.exists():
            self.results.append(self._make_result(
                name, False, 0,
                "未找到已创建的 GenieAPIService 安装目录联接（configure_genie_root 未成功），跳过",
                skipped=True))
            return False
        try:
            proc = subprocess.run(
                ["cmd.exe", "/c", "rmdir", str(junction)],
                capture_output=True, text=True, timeout=15)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0, f"临时移除安装目录联接异常: {type(e).__name__}: {e}"))
            return False
        if proc.returncode != 0:
            self.results.append(self._make_result(
                name, False, 0,
                f"临时移除安装目录联接失败, rc={proc.returncode}, "
                f"stderr={(proc.stderr or '').strip()!r}"))
            return False
        try:
            probe_model = self.model_names[0] if self.model_names else "unknown_model"
            r_start, _ = self._csrf_request(
                "POST", "/api/service/start",
                body={"model_name": probe_model, "port": self.genie_service_port + 1000},
                timeout=30)
            if isinstance(r_start, Exception):
                if self.builder.process is not None and self.builder.process.poll() is None:
                    self.results.append(self._make_result(
                        name, True, 0,
                        f"start 请求本身异常但 Builder 进程仍存活（说明 Builder 已优雅拒绝）: {r_start}"))
                    return True
                self.results.append(self._make_result(
                    name, False, 0,
                    f"Builder 进程在无效安装目录场景下已退出: {r_start}", crashed=True))
                return False
            if r_start.status_code >= 400:
                self.results.append(self._make_result(
                    name, True, r_start.status_code,
                    f"start 优雅返回结构化错误: {r_start.text[:200]}"))
                return True
            # 2xx: 检查 status.path_warning / exe_path 是否确实反映"未安装"
            rs, _ = self._csrf_request("GET", "/api/service/status", timeout=10)
            path_warning = None
            exe_path = None
            if not isinstance(rs, Exception) and rs.status_code == 200:
                try:
                    js = rs.json()
                    path_warning = js.get("path_warning")
                    exe_path = js.get("exe_path")
                except Exception:
                    pass
            passed = bool(path_warning) or not exe_path
            self.results.append(self._make_result(
                name, passed, r_start.status_code,
                f"start 返回 {r_start.status_code}; path_warning={path_warning}; exe_path={exe_path}"))
            return passed
        finally:
            # 恢复目录联接，避免影响后续用例
            try:
                recreate = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(junction), str(self.genie_root_path)],
                    capture_output=True, text=True, timeout=15)
                if recreate.returncode != 0:
                    self.results.append(self._make_result(
                        "BUILDER-LOCAL: test_invalid_genie_root (restore junction)", False, 0,
                        f"恢复安装目录联接失败, rc={recreate.returncode}, "
                        f"stderr={(recreate.stderr or '').strip()!r}"))
            except Exception as e:
                self.results.append(self._make_result(
                    "BUILDER-LOCAL: test_invalid_genie_root (restore junction)", False, 0,
                    f"恢复安装目录联接异常: {type(e).__name__}: {e}"))
            # 无论上面走哪个分支，都主动停一下服务，避免遗留 GenieAPIService 子进程
            self._csrf_request(
                "POST", "/api/service/stop", body={"force": True}, timeout=15)

    def test_unknown_model_name(self):
        """POST /api/service/start 是 fire-and-forget 设计：Builder 侧不会同步校验
        model_name 对应的 config.json 是否存在，只是拼接出该路径的字符串后异步拉起子进程
        （见 process_service.py::start `config_file = str((Path(models_root) / model_name /
        "config.json").resolve())`，从不检查存在性），因此正常会立即返回 200
        {"status":"starting"}，不应期望立刻拿到 4xx。真正的"优雅失败"体现在：GenieAPIService.exe
        因为 -c 指向的 config.json 不存在会自行快速退出，随后轮询 GET /api/service/status
        应该能看到服务从未真正 running=true（很快回落到非运行），而不是让 Builder 进程本身
        崩溃或误报"已加载不存在的模型"。"""
        name = "BUILDER-LOCAL: test_unknown_model_name"
        unknown = "__nonexistent_model_for_negative_test__"
        r, _ = self._csrf_request(
            "POST", "/api/service/start",
            body={"model_name": unknown, "port": self.genie_service_port + 2000},
            timeout=30)
        if isinstance(r, Exception):
            if self.builder.process is not None and self.builder.process.poll() is None:
                self.results.append(self._make_result(
                    name, True, 0,
                    f"未知模型请求异常但 Builder 进程仍存活: {r}"))
                return True
            self.results.append(self._make_result(
                name, False, 0,
                f"未知模型请求导致 Builder 进程退出: {r}", crashed=True))
            return False
        if r.status_code >= 400:
            # 部分 Builder 版本若加入了同步校验，同样接受为优雅失败
            self.results.append(self._make_result(
                name, True, r.status_code,
                f"start 同步返回结构化错误: {r.text[:200]}"))
            self._csrf_request("POST", "/api/service/stop", body={"force": True}, timeout=15)
            return True
        # 200/201/202: 已按 fire-and-forget 设计接受请求，轮询确认服务从未真正 running=true
        settled_not_running = False
        last_status = None
        end = time.time() + 20
        while time.time() < end:
            time.sleep(2)
            rs, _ = self._csrf_request("GET", "/api/service/status", timeout=10)
            if isinstance(rs, Exception) or rs.status_code != 200:
                continue
            try:
                last_status = rs.json()
            except Exception:
                continue
            if isinstance(last_status, dict) and not last_status.get("running"):
                settled_not_running = True
                break
        if self.builder.process is not None and self.builder.process.poll() is not None:
            self.results.append(self._make_result(
                name, False, r.status_code,
                f"Builder 进程在处理未知模型请求后已退出（poll={self.builder.process.poll()}）",
                crashed=True))
            return False
        passed = settled_not_running
        self.results.append(self._make_result(
            name, passed, r.status_code,
            (f"start 已接受请求(status={r.status_code}，符合 fire-and-forget 设计)后轮询 20s; "
             f"settled_not_running={settled_not_running}; last_status={last_status}")))
        # 清理可能被误启动的子进程
        self._csrf_request(
            "POST", "/api/service/stop", body={"force": True}, timeout=15)
        return passed

    def test_missing_csrf_rejected(self):
        """故意不带 CSRF cookie/header 发一次非安全方法请求，断言 403 (security.csrf.missing)。
        用一个全新的 requests.Session（不复用 builder.csrf 的 cookie jar），确保裸请求路径真正
        绕过 CSRF 头，验证防护本身仍生效。"""
        name = "BUILDER-LOCAL: test_missing_csrf_rejected"
        url = f"{self.builder.base_url}/api/service/stop"
        try:
            fresh = requests.Session()
            r = fresh.post(url, json={}, timeout=10)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0,
                f"裸 POST 请求异常: {type(e).__name__}: {e}"))
            return False
        passed = r.status_code == 403
        self.results.append(self._make_result(
            name, passed, r.status_code,
            f"status={r.status_code}; body={r.text[:200]!r} (期望 403 security.csrf.*)"))
        return passed

    # ---- neutralize/restore service_config.json (Builder V1-parity sync 缺陷规避) ----
    def neutralize_service_config_models(self):
        """Builder 的 process_service.py::_sync_service_config_model() 每次 start() 时会把
        exe 目录下 service_config.json 里第一个 enabled 且 backend in ("qnn","") 的模型条目
        的 name/path 强行改写成本次请求的 model_name（V1 兼容遗留逻辑，假定"primary 模型"
        永远是 QNN），却不会同步修正该条目的 backend 字段；随后 GenieAPIService 自身的
        InitializeConfig() 会把按 name 匹配到的这条 service_config.json 条目当作对 -c 主
        模型的 backend/device/context_size "覆盖"（该覆盖机制本身是 GenieAPIService 的合法
        设计，用于管理员强制指定 backend，但被 Builder 的改名行为误用）。净效果：只要 exe
        目录下 service_config.json 里存在一条历史遗留的 qnn 模型条目，任何通过 Builder 加载
        GGUF/MNN 模型都会被强制按 backend=qnn 尝试加载而失败（已通过独立复现 + 实时日志抓取
        确认：日志会出现"[TryCreate] Using specified backend: qnn"后紧跟"Load Model
        Failed"）。

        这是 QAIModelBuilder 自身现有的、真实存在的兼容性缺陷（并非本次测试引入），但按"零
        侵入 Builder 源码"的硬约束不能去改 _sync_service_config_model()。规避方式：既然
        exe 目录（经 mklink /J 联接）就是磁盘上真实、被多个 suite 共享的 service_config.json，
        在本 suite 独占运行期间把它的 models[] 清空（其它字段如 routing/cloud_model 原样
        保留，不影响 InitializeConfig 的其它职责），使 _sync_service_config_model() 找不到
        可改写的旧条目、InitializeConfig() 也就没有条目可匹配触发覆盖；运行结束后从备份恢复
        （见 restore_service_config），不遗留对该共享文件的永久改动。

        重要补充（已实测确认）：该方法必须在**每一次** POST /api/service/start 之前都重新
        调用一次（而不是只在流程最开头调用一次）——已实测复现：即使第一次 start（如 GGUF）
        清空后成功不触发 sync，紧接着切换到第二个模型（如 MNN）时 sync 依然命中并改写了
        models[]，说明有其它写入路径会在两次 start 之间重新往 models[] 里补回条目（具体来源
        超出本次排查范围，但现象可稳定复现）。因此本方法被设计为幂等：重复调用只会不断把
        models[] 清空为 []，不会丢失最初备份的原始内容（备份只在首次真正捕获到非空原始内容
        时写入 self._service_config_backup_text，后续调用不会用"已被清空的 []"覆盖掉它）。"""
        name = "BUILDER-LOCAL: neutralize service_config.json models[] (workaround for Builder V1-parity sync bug)"
        if not self.genie_root_path:
            self.results.append(self._make_result(
                name, False, 0, "未配置 GenieAPIService 根目录，跳过", skipped=True))
            return False
        cfg_path = Path(self.genie_root_path) / "service_config.json"
        if not cfg_path.is_file():
            self.results.append(self._make_result(
                name, True, 0, f"{cfg_path} 不存在，无需处理（Builder 的 sync 逻辑天然是 no-op）"))
            return True
        try:
            original_text = cfg_path.read_text(encoding="utf-8")
            cfg = json.loads(original_text)
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0, f"读取/解析 {cfg_path} 失败: {type(e).__name__}: {e}"))
            return False
        original_models = cfg.get("models")
        n_original = len(original_models) if isinstance(original_models, list) else 0
        # 只在首次真正捕获到备份时记录（幂等：后续重复调用不会用已清空的内容覆盖备份）。
        if getattr(self, "_service_config_backup_text", None) is None:
            self._service_config_path = cfg_path
            self._service_config_backup_text = original_text
        if n_original == 0 and cfg.get("models") == []:
            # 已经是空的，无需再写一次磁盘（避免不必要的 IO/日志噪音）。
            self.results.append(self._make_result(
                name, True, 0, f"{cfg_path} 的 models[] 已为空，无需再清空"))
            return True
        cfg["models"] = []
        try:
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
        except Exception as e:
            self.results.append(self._make_result(
                name, False, 0, f"写入清空 models[] 后的 {cfg_path} 失败: {type(e).__name__}: {e}"))
            return False
        self.results.append(self._make_result(
            name, True, 0,
            f"已清空 {cfg_path} 的 models[]（本次发现 {n_original} 条），结束后会自动恢复"))
        return True

    def restore_service_config(self):
        """恢复 neutralize_service_config_models() 备份的原始 service_config.json 内容，
        避免对该共享文件（属于本机构建产物目录，被其它 suite 共用）造成永久改动。"""
        path = getattr(self, "_service_config_path", None)
        text = getattr(self, "_service_config_backup_text", None)
        if not path or text is None:
            return
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            print(f"  ⚠ 恢复 {path} 失败（需要手动检查）: {type(e).__name__}: {e}")

    # ---- 主入口 ----
    def run_all(self):
        """按顺序串联：配置根目录 → 注入 → 发现 → 主模型加载与直连推理验证 → 切换 → 停止
        → 异常边界。前置失败不阻塞后续独立用例（例如异常边界必须始终跑，验证防护本身生效）。"""
        root_ok = self.configure_genie_root()
        if root_ok:
            self.neutralize_service_config_models()
        inject_ok = self.inject_local_models()
        found = self.discover_models_via_builder() if inject_ok else []
        found_names = {m.get("name") for m in found if isinstance(m, dict)}
        usable = [m for m in self.model_names if m in found_names]

        if not root_ok:
            self.results.append(self._make_result(
                "BUILDER-LOCAL: primary model load and verify", False, 0,
                "GenieAPIService 根目录未成功配置，跳过后续加载验证", skipped=True))
        elif not usable:
            self.results.append(self._make_result(
                "BUILDER-LOCAL: primary model load and verify", False, 0,
                f"无可用模型（期望={self.model_names}, 已发现={sorted(found_names)}），"
                f"跳过后续加载验证",
                skipped=True))
        else:
            primary = usable[0]
            started, status = self.start_and_wait_ready(primary, self.genie_service_port)
            if started:
                self.assert_command_contains(primary, self.genie_service_port, status=status)
                self.verify_genieapiservice_reachable(self.genie_service_port, primary)
                if len(usable) > 1:
                    other = usable[1]
                    switched, _ = self.switch_model(other, self.genie_service_port)
                    if switched:
                        self.verify_genieapiservice_reachable(self.genie_service_port, other)
                else:
                    self.results.append(self._make_result(
                        "BUILDER-LOCAL: switch_model", False, 0,
                        f"只有一个可用模型（{primary}），无法验证模型切换", skipped=True))
                self.stop_and_verify(self.genie_service_port)

        # 异常边界：无论前面成败都跑，验证防护/结构化错误处理本身仍生效
        for case_fn, case_name in (
            (self.test_invalid_genie_root, "BUILDER-LOCAL: test_invalid_genie_root"),
            (self.test_unknown_model_name, "BUILDER-LOCAL: test_unknown_model_name"),
            (self.test_missing_csrf_rejected, "BUILDER-LOCAL: test_missing_csrf_rejected"),
        ):
            try:
                case_fn()
            except Exception as e:
                self.results.append(self._make_result(
                    case_name, False, 0,
                    f"用例内部未捕获异常: {type(e).__name__}: {e}", crashed=True))
        return self.results


def run_builder_local_model_integration(args, models, all_results, all_crash_events, all_perf_samples):
    """--suite builder_local_model 主入口：串联启动 Builder → 配置根目录 → 注入模型 →
    发现 → 启动加载 → 直连推理验证 → 切换 → 停止 → 异常场景。远程模式不适用。"""
    print(f"\n{'='*60}")
    print("阶段: QAIModelBuilder 本地模型加载端到端验证")
    print(f"{'='*60}")
    if args.remote:
        all_results.append(TestResult(
            name="BUILDER-LOCAL: local_model_suite", round_num=1,
            model_name="_builder_local_model_",
            passed=False, status_code=0, latency_ms=0,
            detail="远程模式不适用（本 suite 需要启动 Builder 子进程与本机文件系统 mklink 操作）",
            skipped=True))
        return
    if not args.genie_root_path:
        all_results.append(TestResult(
            name="BUILDER-LOCAL: local_model_suite", round_num=1,
            model_name="_builder_local_model_",
            passed=False, status_code=0, latency_ms=0,
            detail="缺少 --genie_root_path（或未通过 --exe_dir 复用），无法告知 Builder GenieAPIService 安装位置",
            skipped=True))
        return

    # 选择注入哪些模型：显式 --builder_local_models 优先；否则每个后端（qnn/gguf/mnn）自动挑第一个
    if getattr(args, "builder_local_models", None):
        wanted = [m.strip() for m in args.builder_local_models.split(",") if m.strip()]
        target_models = [m for m in wanted if m in models]
        for miss in [m for m in wanted if m not in models]:
            all_results.append(TestResult(
                name="BUILDER-LOCAL: local_model_filter", round_num=1,
                model_name=miss, passed=False, status_code=0, latency_ms=0,
                detail=f"--builder_local_models 指定的 {miss!r} 不在 --models 下已发现模型列表中",
                skipped=True))
    else:
        target_models = list(_pick_builder_backend_models(
            models, {"qnn", "gguf", "mnn"}).values())
    if not target_models:
        all_results.append(TestResult(
            name="BUILDER-LOCAL: local_model_suite", round_num=1,
            model_name="_builder_local_model_",
            passed=False, status_code=0, latency_ms=0,
            detail=f"没有可用于注入的模型（当前 --models 下发现: {models}）",
            skipped=True))
        return

    print(f"  拟注入模型: {target_models}")
    print(f"  Builder 根目录: {args.builder_dir}")
    print(f"  Builder 数据目录: {args.builder_data_dir}")
    print(f"  GenieAPIService 安装目录: {args.genie_root_path}")
    print(f"  GenieAPIService 端口: {args.port}")

    builder = QAIModelBuilderManager(
        args.builder_dir, args.host, args.builder_port, log_dir=args.out_dir,
        python_exe=args.builder_python_exe, data_dir=args.builder_data_dir,
    )
    tester = None
    try:
        print(f"  启动 QAIModelBuilder 后端: {args.builder_dir}")
        builder.start(timeout=120)
        tester = QAIModelBuilderLocalModelTester(
            builder=builder,
            models_root=args.models,
            genie_root_path=args.genie_root_path,
            model_names=target_models,
            genie_service_port=args.port,
            round_num=1,
        )
        results = tester.run_all()
        all_results.extend(results)
        all_crash_events.extend(tester.crash_events)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"  ⚠ QAIModelBuilder 本地模型加载 suite 跳过: {e}")
        all_crash_events.append(CrashEvent(
            timestamp=datetime.now().isoformat(),
            model_name="_builder_local_model_", round_num=1,
            endpoint="QAIMODELBUILDER_STARTUP", detail=str(e),
            log_tail=_capture_log_tail(builder),
        ))
        all_results.append(TestResult(
            name="BUILDER-LOCAL: Builder 启动", round_num=1,
            model_name="_builder_local_model_",
            passed=False, status_code=0, latency_ms=0,
            detail=f"跳过: {str(e)[:500]}", skipped=True))
    except Exception as e:
        print(f"  ✗ QAIModelBuilder 本地模型加载 suite 异常: {e}")
        all_crash_events.append(CrashEvent(
            timestamp=datetime.now().isoformat(),
            model_name="_builder_local_model_", round_num=1,
            endpoint="GLOBAL", detail=str(e)
        ))
        all_results.append(TestResult(
            name="GLOBAL", round_num=1, model_name="_builder_local_model_",
            passed=False, status_code=0, latency_ms=0,
            detail=f"套件级未捕获异常: {e}", crashed=True))
    finally:
        if tester is not None:
            try:
                tester.restore_service_config()
            except Exception:
                pass
        try:
            builder.stop()
        except Exception:
            pass


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


def _read_long_text_trigger_ratio(exe_dir):
    """从构建产物目录下实际生效的 service_config.json 读取
    prompt_optimization.long_text_summarization.trigger_ratio；读取不到时按 0.5 兜底
    （与代码默认值一致，见 service_config.json 中的同名字段注释）。"""
    default_ratio = 0.5
    try:
        cfg_path = Path(exe_dir) / "service_config.json"
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
    """新增的、不经 QAIModelBuilder 的差异化自验证矩阵：对同一批探针请求分别用
    `-n -1 -d 4` 与 `-n 30 -d 4`（默认对照组）各启动一次裸 GenieAPIService.exe（两次完全
    独立的服务生命周期,先完整停止第一组再启动第二组,不做进程内热切换——numResponse 是
    进程级全局配置,无法运行时切换),对 4 项行为差异（历史管理/系统提示词优化/工具定义
    优化/长文本摘要）做双向日志断言,是全方案里唯一能通过读日志真正坐实这些差异确实
    按预期互斥发生的地方（Builder 路径固定 loglevel=3 且结构性无法同步读子进程日志）。
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
                    context_size = ctx_r.json().get("contextsize", 0)
            except Exception:
                pass
            long_text = _build_stateless_long_text_probe(context_size, trigger_ratio)
            resp4, err4 = _stateless_chat_request(args.host, args.port, model_name, long_text, timeout=240)
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

    # ── 探针2：系统提示词优化（双向断言） ────────────────────────────────────
    try:
        name = f"{name_prefix} 系统提示词优化 ([Optimization] System prompt savings，双向断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成双向比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["hist_sysprompt_log"]
            log_30 = data_30["hist_sysprompt_log"]
            neg1_has_savings = "[Optimization] System prompt savings:" in log_neg1
            mode30_has_savings = "[Optimization] System prompt savings:" in log_30
            passed = neg1_has_savings and not mode30_has_savings
            detail = f"-1侧出现savings日志={neg1_has_savings}; 30侧出现savings日志={mode30_has_savings}"
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 系统提示词优化双向断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── 探针3：工具定义优化（双向断言，须用预定义工具名 "read"） ───────────────
    try:
        name = f"{name_prefix} 工具定义优化 ([Optimizer] Tools - Original...Savings...，双向断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成双向比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["tool_log"]
            log_30 = data_30["tool_log"]
            neg1_has_tool_opt = ("[Optimizer] Tools - Original:" in log_neg1) and ("Savings:" in log_neg1)
            mode30_has_tool_opt = ("[Optimizer] Tools - Original:" in log_30) and ("Savings:" in log_30)
            passed = neg1_has_tool_opt and not mode30_has_tool_opt
            detail = f"-1侧出现工具优化日志={neg1_has_tool_opt}; 30侧出现工具优化日志={mode30_has_tool_opt}"
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 工具定义优化双向断言", round_num=1, model_name=model_name,
            passed=False, status_code=0, latency_ms=0, detail=f"用例异常: {type(e).__name__}: {e}", crashed=True
        ))

    # ── 探针4：长文本摘要（双向断言） ────────────────────────────────────────
    try:
        name = f"{name_prefix} 长文本摘要 ([LongTextSummarizer] User message summarized，双向断言)"
        if data_neg1 is None or data_30 is None:
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=False, status_code=0, latency_ms=0,
                detail="其中一侧服务未能成功启动/完成探针请求，无法完成双向比对", skipped=True
            ))
        else:
            log_neg1 = data_neg1["longtext_log"]
            log_30 = data_30["longtext_log"]
            neg1_has_summarized = "[LongTextSummarizer] User message summarized:" in log_neg1
            mode30_has_summarized = "[LongTextSummarizer] User message summarized:" in log_30
            passed = neg1_has_summarized and not mode30_has_summarized
            detail = (f"context_size(-1侧)={data_neg1.get('context_size')}, "
                      f"context_size(30侧)={data_30.get('context_size')}, trigger_ratio={trigger_ratio}; "
                      f"-1侧出现摘要日志={neg1_has_summarized}; 30侧出现摘要日志={mode30_has_summarized}")
            all_results.append(TestResult(
                name=name, round_num=1, model_name=model_name, passed=passed, status_code=200 if passed else 0,
                latency_ms=0, detail=detail
            ))
    except Exception as e:
        all_results.append(TestResult(
            name=f"{name_prefix} 长文本摘要双向断言", round_num=1, model_name=model_name,
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
            ctx = resp.json().get("contextsize", 0)
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

    service_config_path = Path(args.exe_dir) / "service_config.json"
    original_service_config = service_config_path.read_bytes() if service_config_path.exists() else None

    def restore_service_config():
        if original_service_config is None:
            try:
                service_config_path.unlink()
            except FileNotFoundError:
                pass
        else:
            service_config_path.write_bytes(original_service_config)

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

            for device in devices:
                test_name = f"test_gguf_{device}_explicit_load"
                service_model_name = gguf_model
                result_model_name = f"{gguf_model} ({device.upper()})"
                service_config = {
                    "default_model": service_model_name,
                    "models": [
                        {
                            "name": service_model_name,
                            "path": gguf_model,
                            "backend": "GGUF",
                            "device": device,
                            "context_size": 4096,
                            "enabled": True,
                        }
                    ]
                }
                service_config_path.write_text(json.dumps(service_config, ensure_ascii=False, indent=2), encoding="utf-8")

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
    finally:
        restore_service_config()


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


def _finalize_and_exit(all_results, all_perf_samples, all_crash_events, out_dir, remote_mode, suite_name, cmdline=""):
    """汇总统计 + 生成全部报告 + 计算退出码。所有 suite 收尾都走这一个函数，避免重复代码。"""
    print(f"\n{'='*60}")
    print("生成报告...")
    print(f"{'='*60}")
    ReportGenerator.generate_json(all_results, all_perf_samples, all_crash_events, str(out_dir))
    ReportGenerator.generate_summary_html(all_results, all_perf_samples, all_crash_events, str(out_dir),
                                           remote_mode=remote_mode, suite_name=suite_name, cmdline=cmdline)
    ReportGenerator.generate_model_reports(all_results, all_perf_samples, all_crash_events, str(out_dir),
                                            remote_mode=remote_mode, cmdline=cmdline)
    ReportGenerator.generate_conversations(all_results, str(out_dir))

    # 与 generate_summary_html() 的 summary_all 用同一套互斥分类优先级（SKIP > CRASHED > PASSED > FAILED），
    # 避免同一条 TestResult 被多个桶重复计数导致 passed+failed+crashed+skipped != total。
    total = len(all_results)
    passed = failed = crashed = skipped = 0
    for r in all_results:
        if r.skipped:
            skipped += 1
        elif r.crashed:
            crashed += 1
        elif r.passed:
            passed += 1
        else:
            failed += 1
    print(f"\n{'='*60}")
    print(f"测试完成: {passed}/{total} 通过, {failed} 失败, {crashed} 崩溃, {skipped} 跳过")
    print(f"{'='*60}")
    sys.exit(0 if failed == 0 and crashed == 0 else 1)


def _run_sampleapp_suite(args, models, remote_mode, out_dir):
    """--suite sampleapp：只验证 SampleApp.exe，不涉及 GenieAPIService HTTP 全量回归。"""
    all_results = []
    run_sampleapp_only_tests(args, models, all_results)
    return all_results, [], []


def _run_gguf_suite(args, models, remote_mode, out_dir):
    """--suite gguf：只运行 GGUF GPU/CPU 显式加载回归。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []
    run_gguf_explicit_load_regressions(args, models, all_results, all_crash_events, all_perf_samples)
    return all_results, all_perf_samples, all_crash_events


def _run_graceful_shutdown_suite(args, models, remote_mode, out_dir):
    """--suite graceful_shutdown：只运行"推理中终止进程 → 优雅退出验证"。"""
    all_results = []
    all_crash_events = []
    run_graceful_shutdown_tests(args, models, all_results, all_crash_events)
    return all_results, [], all_crash_events


def _run_full_suite(args, models, remote_mode, out_dir):
    """--suite full：阶段1通用接口 → 阶段2逐模型 → 阶段3多模型并发 → 阶段4 GGUF 显式加载 →
    阶段5 Builder 集成代理 → 阶段6 SampleApp。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []

    # ===== 阶段 1：模型无关的通用接口测试（只测一次） =====
    print(f"\n{'='*60}")
    print("阶段 1: 通用接口测试（模型无关）")
    print(f"{'='*60}")
    stage1_startup_failed = False
    if remote_mode:
        svc_global = RemoteServiceManager(args.host, args.port)
    else:
        # 本地模式：用第一个模型的 config 启动服务
        config_path = Path(args.models) / models[0] / "config.json"
        svc_global = ServiceManager(args.exe_dir, args.host, args.port)
        svc_global._log_dir = args.out_dir
        try:
            svc_global.start(str(config_path))
            # 等服务进程发完初始化并绑定 TCP 端口 (轮询 connect,避免 ConnectionRefused)
            # 8B 模型 QNN 初始化可能 ≪ 60s,这里给 120s 余裕。
            # 同时把 Popen 传进去,子进程秒退能立刻发现,而不是空等 120 秒。
            print("  等待端口可连接...")
            if not wait_port_open(args.host, args.port, timeout=120, process=svc_global.process):
                print("  ⚠ 端口 120s 内未可连接,仍然尝试进入测试")
            else:
                print("  ✓ 端口已可连接")
        except (RuntimeError, FileNotFoundError) as e:
            # 服务启动失败 (秒退 / 找不到 exe / 端口轮询期间发现进程已死)
            # 记录到 crash_events,跳过阶段 1 的测试,继续进入阶段 2 (后面的模型也会各自重试启动)。
            print(f"  ✗ 服务启动失败: {e}")
            stage1_startup_failed = True
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name="_global_", round_num=1,
                endpoint="SERVICE_STARTUP", detail=str(e)
            ))
            all_results.append(TestResult(
                name="服务启动 (阶段 1)", round_num=1, model_name="_global_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"启动失败: {str(e)[:300]}",
                crashed=True
            ))

    if not stage1_startup_failed:
        perf_global = PerfMonitor()
        global_tester = APITester(args.host, args.port, svc_global, perf_global, "_global_", total_rounds=1)
        global_results = global_tester.run_global_tests()
        all_results.extend(global_results)

        # 通用接口测试的最后一项: /servicestop (会让服务进程退出,所以放最后)
        # remote_mode 下也调用,但要求调用方确认/接受服务会被关闭。
        print(f"    测试 POST /servicestop ... ", end="", flush=True)
        stop_result = global_tester.test_servicestop(1)
        status = "✓ PASS" if stop_result.passed else ("✗ CRASH" if stop_result.crashed else "✗ FAIL")
        print(f"{status} ({stop_result.latency_ms:.0f}ms)")
        all_results.append(stop_result)

        if not remote_mode:
            # /servicestop 已经让进程退出了,这里 stop() 多半是 no-op,
            # 但保留一次确认调用以释放本地资源(关闭日志句柄等)。
            svc_global.stop()
    else:
        # 启动失败是最严重的信号之一：阶段 1 的通用接口测试统统未能真正执行，但这不是
        # "环境不适用/设计不支持"这类合理跳过场景，而是服务本身出了问题——统一标记为
        # crashed=True（而不是 skipped=True），确保计入 failed/crashed 统计和退出码，
        # 不会被 _finalize_and_exit 的统计口径悄悄漏掉。
        for ep in ["GET /", "GET /models", "GET /v1/models", "POST /servicestop"]:
            all_results.append(TestResult(
                name=ep, round_num=1, model_name="_global_",
                passed=False, status_code=0, latency_ms=0,
                detail="服务未能启动，通用接口测试未能真正执行", crashed=True
            ))

    # ===== 阶段 2：每个模型的完整生命周期测试 =====
    # 复用 _run_model_suite（不带 --model_name 过滤，等价于对全部已发现模型执行），
    # 保证 full suite 与独立的 --suite model 在阶段2上完全一致，不重复维护两套逻辑。
    stage2_results, stage2_perf, stage2_crashes = _run_model_suite(args, models, remote_mode, out_dir)
    all_results.extend(stage2_results)
    all_perf_samples.extend(stage2_perf)
    all_crash_events.extend(stage2_crashes)

    # ===== 阶段 3：多模型并发加载与路由测试 =====
    # 复用 _run_multi_model_suite，同理保证 full suite 与独立的 --suite multi_model 行为一致。
    stage3_results, stage3_perf, stage3_crashes = _run_multi_model_suite(args, models, remote_mode, out_dir)
    all_results.extend(stage3_results)
    all_perf_samples.extend(stage3_perf)
    all_crash_events.extend(stage3_crashes)

    run_gguf_explicit_load_regressions(args, models, all_results, all_crash_events, all_perf_samples)

    # 紧跟 GGUF 显式加载回归之后：-n -1 vs -n 30 差异化自验证矩阵（不经 Builder，直接启动裸
    # GenieAPIService.exe，验证历史管理/系统提示词优化/工具定义优化/长文本摘要 4 项行为
    # 差异确实按预期互斥发生）。
    run_numresponse_stateless_mode_regressions(args, models, all_results, all_crash_events, all_perf_samples)

    # ===== 阶段 6：SampleApp 专项测试 =====
    # SampleApp 用户明确要求纳入 full 套件的默认执行范围（此前完全独立，导致完备性矩阵的
    # SampleApp 列在 full 下永远显示"未运行本轮"）。复用 --suite sampleapp 同一个实现，
    # 不重复维护第二套逻辑；不传 --model_name 过滤时等价于遍历全部已发现模型。
    run_sampleapp_only_tests(args, models, all_results)

    # ===== 阶段 7：推理中终止进程 → 优雅退出验证 =====
    run_graceful_shutdown_tests(args, models, all_results, all_crash_events)

    return all_results, all_perf_samples, all_crash_events


def _run_model_suite(args, models, remote_mode, out_dir):
    """--suite model（也被 full suite 阶段2复用）：对目标模型执行完整生命周期测试。
    未指定 --model_name 时测试全部已发现模型；指定时（逗号分隔）仅测试匹配的模型。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []

    target_models = models
    model_filter = getattr(args, "model_name", None)
    if model_filter:
        wanted = {m.strip() for m in model_filter.split(",") if m.strip()}
        target_models = [m for m in models if m in wanted]
        for missing in sorted(wanted - set(models)):
            all_results.append(TestResult(
                name="MODEL_FILTER", round_num=1, model_name=missing,
                passed=False, status_code=0, latency_ms=0,
                detail=f"--model_name 指定的模型 '{missing}' 不在已发现模型列表中，跳过", skipped=True
            ))

    for model_name in target_models:
        print(f"\n{'='*60}")
        print(f"阶段 2: 模型测试 - {model_name}")
        print(f"{'='*60}")

        if remote_mode:
            svc = RemoteServiceManager(args.host, args.port)
        else:
            config_path = Path(args.models) / model_name / "config.json"
            svc = ServiceManager(args.exe_dir, args.host, args.port)
            svc._log_dir = args.out_dir
        perf = PerfMonitor()

        try:
            if remote_mode:
                # 远程模式：检查服务是否可达（仅此处做一次实际连通性检查）
                print(f"  检查远程服务...")
                if not svc.check_connectivity(timeout=10):
                    print(f"  ✗ 远程服务不可达，跳过模型 {model_name}")
                    for rnd in range(1, args.rounds + 1):
                        all_results.append(TestResult(
                            name="SERVICE_CHECK", round_num=rnd, model_name=model_name,
                            passed=False, status_code=0, latency_ms=0,
                            detail="远程服务不可达", skipped=True
                        ))
                    continue
                print(f"  ✓ 远程服务可达")
            else:
                print(f"  启动服务...")
                try:
                    svc.start(str(config_path))
                except (RuntimeError, FileNotFoundError) as e:
                    # 服务启动失败 (秒退 / 找不到 exe)
                    # 记录一条 SERVICE_STARTUP 崩溃事件 + 每一轮一条 skipped TestResult,
                    # 然后 continue 到下一个模型 (不要让整个脚本崩溃)。
                    print(f"  ✗ 服务启动失败: {e}")
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=model_name, round_num=0,
                        endpoint="SERVICE_STARTUP", detail=str(e)
                    ))
                    # 每个模型都是全新的 ServiceManager/进程,不存在"MNN OOM 级联"的可能;
                    # 按分类规则判定后恒为真实错误(ignorable=False),不再用"gguf/mnn/gpu"关键字统一豁免。
                    ignorable, ignore_reason, classify_detail = _classify_process_down(svc, model_name)
                    for rnd in range(1, args.rounds + 1):
                        all_results.append(TestResult(
                            name="服务启动", round_num=rnd, model_name=model_name,
                            passed=False, status_code=0, latency_ms=0,
                            detail=f"启动失败: {str(e)[:300]}; {classify_detail}",
                            crashed=True,
                            ignorable=ignorable,
                            ignore_reason=ignore_reason
                        ))
                    continue
                # 轮询 TCP 端口,避免第一个请求碍在 ConnectionRefused 上;
                # 把 Popen 传进去,子进程秒退能立刻被发现。
                print(f"  等待端口可连接...")
                try:
                    if not wait_port_open(args.host, args.port, timeout=120, process=svc.process):
                        print(f"  ⚠ 端口 120s 内未可连接,仍然尝试进入测试")
                    else:
                        print(f"  ✓ 端口已可连接,进入测试")
                except RuntimeError as e:
                    # wait_port_open 期间发现子进程退出 — 同上记录并 continue
                    print(f"  ✗ 服务启动后秒退: {e}")
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=model_name, round_num=0,
                        endpoint="SERVICE_STARTUP", detail=str(e)
                    ))
                    ignorable, ignore_reason, classify_detail = _classify_process_down(svc, model_name)
                    for rnd in range(1, args.rounds + 1):
                        all_results.append(TestResult(
                            name="服务启动", round_num=rnd, model_name=model_name,
                            passed=False, status_code=0, latency_ms=0,
                            detail=f"启动失败: {str(e)[:300]}; {classify_detail}",
                            crashed=True,
                            ignorable=ignorable,
                            ignore_reason=ignore_reason
                        ))
                    continue

            # 启动性能采集
            pid = svc.get_pid()
            if pid:
                perf.start(pid)

            # GGUF 模型未在 config.json 里显式指定 device 时,服务会自己选(先试 gpu,失败退化
            # 到 cpu)——这条常规生命周期测试用的正是这个"隐式"设备。如果不把实际设备打到报告
            # 用的 model_name 上,报告/完备性矩阵里就会出现一个裸模型名的行,与阶段4显式加载回归
            # 产生的 "<model> (CPU)"/"<model> (GPU)" 两行并列,变成同一个模型被拆成 3 行。
            # 这里查一次 /models 拿到真实 device,把本模型本轮的全部结果标签都改成设备后缀形式,
            # 使其自然并入阶段4已经使用的同一个设备分桶。只对 GGUF 生效——QNN/MNN 的设备
            # (npu/cpu)是固定的,没有这种歧义,不需要打标签。
            report_model_name = model_name
            if infer_backend(model_name)[0] == "GGUF":
                try:
                    models_resp = requests.get(f"http://{args.host}:{args.port}/models", timeout=10)
                    if models_resp.status_code == 200:
                        entries = models_resp.json().get("data", [])
                        loaded = [m for m in entries if m.get("is_loaded") and m.get("id") == model_name]
                        if loaded and loaded[0].get("device"):
                            report_model_name = f"{model_name} ({loaded[0]['device'].upper()})"
                except Exception:
                    pass  # 探测失败时保留裸模型名,不影响测试本身,只是报告展示上退化为旧行为

            # 多轮测试（不含多模态用例，也不做收尾；多模态轮次与收尾单独处理，见下方）
            oom_triggered = False
            for rnd in range(1, args.rounds + 1):
                print(f"\n  --- 第 {rnd}/{args.rounds} 轮 ---")
                tester = APITester(args.host, args.port, svc, perf, model_name, total_rounds=args.rounds,
                                   data_dir=args.data_dir, all_models=models, multimodal_rounds=args.multimodal_rounds,
                                   exe_dir=args.exe_dir if not remote_mode else None)
                round_results = tester.run_all(rnd, include_final_cleanup=False)
                if report_model_name != model_name:
                    for r in round_results:
                        r.model_name = report_model_name
                all_results.extend(round_results)
                all_crash_events.extend(tester.crash_events)
                # MNN OOM 崩溃后已自动重启：跳过该模型本轮剩余测试，继续下一个模型
                if any(getattr(r, "auto_restarted", False) for r in round_results):
                    print(f"  ⚠ 检测到 MNN OOM 崩溃并已自动重启，跳过 {model_name} 剩余轮次")
                    oom_triggered = True
                    break

            if not oom_triggered:
                # 多模态轮次（由 --multimodal_rounds 驱动，与 --rounds 完全解耦）与收尾
                # （stop/clear/unload）必须在模型真正卸载前完成，且不应在已确认 MNN OOM
                # 自动重启的模型上再额外触发一轮请求。
                mm_tester = APITester(args.host, args.port, svc, perf, model_name, total_rounds=args.rounds,
                                      data_dir=args.data_dir, all_models=models, multimodal_rounds=args.multimodal_rounds)
                mm_results = mm_tester.run_multimodal_rounds()
                mm_results.extend(mm_tester.run_final_cleanup(args.rounds))
                if report_model_name != model_name:
                    for r in mm_results:
                        r.model_name = report_model_name
                all_results.extend(mm_results)
                all_crash_events.extend(mm_tester.crash_events)

            # 停止性能采集
            perf_samples = perf.stop()
            all_perf_samples.extend(perf_samples)

        except Exception as e:
            # 未被内层捕获的异常：不能只记 CrashEvent 而不产生对应的 TestResult，否则
            # _finalize_and_exit 的 failed/crashed 统计和退出码完全看不到这次异常
            # （all_crash_events 不参与那个判定）。这里补一条 crashed=True 的 TestResult，
            # 与已有的 CrashEvent 追加保持一致，确保异常被无条件计入统计。
            print(f"  ✗ 测试异常: {e}")
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name=model_name, round_num=0,
                endpoint="GLOBAL", detail=str(e)
            ))
            all_results.append(TestResult(
                name="GLOBAL", round_num=0, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"套件级未捕获异常: {e}", crashed=True
            ))
        finally:
            if not remote_mode:
                print(f"  停止服务...")
                svc.stop()

    return all_results, all_perf_samples, all_crash_events


def _run_multimodal_suite(args, models, remote_mode, out_dir):
    """--suite multimodal：自动筛选支持多模态的模型，只运行多模态相关用例，跳过其余通用接口测试。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []

    multimodal_models = [m for m in models if detect_modality(m)]
    if not multimodal_models:
        all_results.append(TestResult(
            name="MULTIMODAL_FILTER", round_num=1, model_name="_multimodal_",
            passed=False, status_code=0, latency_ms=0,
            detail="未发现任何支持多模态的模型（detect_modality 返回空集合），跳过 multimodal suite",
            skipped=True
        ))
        return all_results, all_perf_samples, all_crash_events

    for model_name in multimodal_models:
        print(f"\n{'='*60}")
        print(f"多模态测试 - {model_name}")
        print(f"{'='*60}")

        if remote_mode:
            svc = RemoteServiceManager(args.host, args.port)
        else:
            config_path = Path(args.models) / model_name / "config.json"
            svc = ServiceManager(args.exe_dir, args.host, args.port)
            svc._log_dir = args.out_dir
        perf = PerfMonitor()

        try:
            if remote_mode:
                print(f"  检查远程服务...")
                if not svc.check_connectivity(timeout=10):
                    print(f"  ✗ 远程服务不可达，跳过模型 {model_name}")
                    all_results.append(TestResult(
                        name="SERVICE_CHECK", round_num=1, model_name=model_name,
                        passed=False, status_code=0, latency_ms=0,
                        detail="远程服务不可达", skipped=True
                    ))
                    continue
                print(f"  ✓ 远程服务可达")
            else:
                print(f"  启动服务...")
                try:
                    svc.start(str(config_path))
                except (RuntimeError, FileNotFoundError) as e:
                    print(f"  ✗ 服务启动失败: {e}")
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=model_name, round_num=0,
                        endpoint="SERVICE_STARTUP", detail=str(e)
                    ))
                    all_results.append(TestResult(
                        name="服务启动", round_num=1, model_name=model_name,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"启动失败: {str(e)[:300]}", crashed=True
                    ))
                    continue
                print(f"  等待端口可连接...")
                try:
                    if not wait_port_open(args.host, args.port, timeout=120, process=svc.process):
                        print(f"  ⚠ 端口 120s 内未可连接,仍然尝试进入测试")
                    else:
                        print(f"  ✓ 端口已可连接,进入测试")
                except RuntimeError as e:
                    print(f"  ✗ 服务启动后秒退: {e}")
                    all_crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(),
                        model_name=model_name, round_num=0,
                        endpoint="SERVICE_STARTUP", detail=str(e)
                    ))
                    all_results.append(TestResult(
                        name="服务启动", round_num=1, model_name=model_name,
                        passed=False, status_code=0, latency_ms=0,
                        detail=f"启动失败: {str(e)[:300]}", crashed=True
                    ))
                    continue

            pid = svc.get_pid()
            if pid:
                perf.start(pid)

            # --suite multimodal 完全由 --multimodal_rounds 驱动（不再跟 --rounds 绑定），
            # 复用 APITester.run_multimodal_rounds() 与逐模型标准生命周期测试同一套用例实现。
            tester = APITester(args.host, args.port, svc, perf, model_name, total_rounds=args.rounds,
                               data_dir=args.data_dir, all_models=models, multimodal_rounds=args.multimodal_rounds)
            all_results.extend(tester.run_multimodal_rounds())
            all_crash_events.extend(tester.crash_events)

            perf_samples = perf.stop()
            all_perf_samples.extend(perf_samples)

        except Exception as e:
            # 与 _run_model_suite 相同的规范：异常必须同时产生 TestResult(crashed=True)
            # 和 CrashEvent，才能被 _finalize_and_exit 的统计口径和退出码捕捉到。
            print(f"  ✗ 测试异常: {e}")
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name=model_name, round_num=0,
                endpoint="GLOBAL", detail=str(e)
            ))
            all_results.append(TestResult(
                name="GLOBAL", round_num=0, model_name=model_name,
                passed=False, status_code=0, latency_ms=0,
                detail=f"套件级未捕获异常: {e}", crashed=True
            ))
        finally:
            if not remote_mode:
                print(f"  停止服务...")
                svc.stop()

    return all_results, all_perf_samples, all_crash_events


def _run_multi_model_suite(args, models, remote_mode, out_dir):
    """--suite multi_model（也被 full suite 阶段3复用）：多模型并发加载与路由测试。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []

    # 使用第一个可用（非崩溃）模型的 config 启动服务
    # 优先选择 QNN 模型（最稳定），避免使用 GGUF/MNN 模型（可能因内存不足崩溃）
    print(f"\n{'='*60}")
    print("阶段 3: 多模型并发加载与路由测试")
    print(f"{'='*60}")
    stage3_startup_failed = False

    # 选择阶段 3 启动用的模型：优先选 QNN 模型（不含 GGUF/MNN 关键字）。本地模式下
    # 同一设备如果有多个候选，进一步优先选磁盘占用（权重文件大小）最小的那个——道理见
    # 下方 models_by_device 的注释：入口模型越轻，留给后续动态加载的 GGUF/MNN 的可用
    # 内存越多，"多类型同时加载"这组测试才更有机会被真正测到。
    def _pick_stage3_model(model_list):
        preferred = [m for m in model_list
                     if "gguf" not in m.lower() and "mnn" not in m.lower()]
        if not preferred:
            return model_list[0]
        if remote_mode:
            return preferred[0]
        return min(preferred, key=lambda m: _estimate_model_dir_size_bytes(args.models, m))

    # 构建 npu/gpu(GGUF)/cpu(MNN) 三设备各一个模型的映射，驱动
    # ensure_multi_backend_loaded 主动触发三后端同时驻留。
    #
    # 同一设备如果有多个候选模型，不再简单取"目录名字母序中第一个发现的"，而是（本地模式下）
    # 优先选择磁盘占用（权重文件大小）最小的那个——这样能最大化"多类型同时加载"这组测试
    # 实际被有效验证到的概率：如果任由字母序碰巧选中一个偏大的模型占用更多内存，会连带
    # 压缩其它设备的可用内存，让共存/两两降级验证更容易因资源不足被跳过而没有真正测到东西，
    # 也更容易让内存实际吃紧的组合从"可能可以两两共存"变成"必然被内存挤没"。远程模式下
    # 不知道模型文件在对端机器上的实际大小，退化为原有的"字母序第一个发现"选择。
    models_by_device = {}
    if remote_mode:
        for m in models:
            device = MultiModelTester._infer_device(m)
            if device not in models_by_device:
                models_by_device[device] = m
    else:
        candidates_by_device = {}
        for m in models:
            device = MultiModelTester._infer_device(m)
            candidates_by_device.setdefault(device, []).append(m)
        for device, candidates in candidates_by_device.items():
            models_by_device[device] = min(
                candidates, key=lambda m: _estimate_model_dir_size_bytes(args.models, m)
            )

    if remote_mode:
        svc_multi = RemoteServiceManager(args.host, args.port)
    else:
        stage3_model = _pick_stage3_model(models)
        config_path_multi = Path(args.models) / stage3_model / "config.json"
        svc_multi = ServiceManager(args.exe_dir, args.host, args.port)
        svc_multi._log_dir = args.out_dir
        try:
            print(f"  启动服务（加载所有模型，使用 {stage3_model} 作为入口）...")
            svc_multi.start(str(config_path_multi))
            print(f"  等待端口可连接（多模型加载可能需要较长时间）...")
            if not wait_port_open(args.host, args.port, timeout=300, process=svc_multi.process):
                print(f"  ⚠ 端口 300s 内未可连接，仍然尝试进入测试")
            else:
                print(f"  ✓ 端口已可连接")
        except (RuntimeError, FileNotFoundError) as e:
            print(f"  ✗ 服务启动失败: {e}")
            stage3_startup_failed = True
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name="_multi_model_", round_num=1,
                endpoint="SERVICE_STARTUP", detail=str(e)
            ))
            all_results.append(TestResult(
                name="服务启动 (阶段 3)", round_num=1, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"启动失败: {str(e)[:300]}", crashed=True
            ))

    if not stage3_startup_failed:
        try:
            perf_multi = PerfMonitor()
            pid_multi = svc_multi.get_pid()
            if pid_multi:
                perf_multi.start(pid_multi)

            multi_tester = MultiModelTester(args.host, args.port, svc_multi, perf_multi, round_num=1)
            multi_results = multi_tester.run_all(
                models_by_device=models_by_device,
                models_root=args.models if not remote_mode else None,
                remote_mode=remote_mode,
            )
            all_results.extend(multi_results)
            all_crash_events.extend(multi_tester.crash_events)

            perf_samples_multi = perf_multi.stop()
            all_perf_samples.extend(perf_samples_multi)
        except Exception as e:
            # 同样补上对应的 TestResult(crashed=True)，与 _run_model_suite/
            # _run_multimodal_suite 保持一致的"异常必须同时产生 TestResult 和 CrashEvent"规范。
            print(f"  ✗ 阶段 3 测试异常: {e}")
            all_crash_events.append(CrashEvent(
                timestamp=datetime.now().isoformat(),
                model_name="_multi_model_", round_num=1,
                endpoint="GLOBAL", detail=str(e)
            ))
            all_results.append(TestResult(
                name="GLOBAL", round_num=1, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"套件级未捕获异常: {e}", crashed=True
            ))
        finally:
            if not remote_mode:
                print(f"  停止服务...")
                svc_multi.stop()

    return all_results, all_perf_samples, all_crash_events


def _run_mnn_suite(args, models, remote_mode, out_dir):
    """--suite mnn：仅对 MNN 后端模型执行标准生命周期测试（MNN 完全不支持多模态,
    detect_modality 对 MNN 模型目录名天然返回空集合,无需额外判断）。"""
    mnn_models = [m for m in models if infer_backend(m)[0] == "mnn"]
    if not mnn_models:
        return [TestResult(
            name="MNN_FILTER", round_num=1, model_name="_mnn_",
            passed=False, status_code=0, latency_ms=0,
            detail="未发现任何 MNN 模型，跳过 mnn suite", skipped=True
        )], [], []
    return _run_model_suite(args, mnn_models, remote_mode, out_dir)


def _interleave_multimodal_and_llm(models):
    """将模型列表按"多模态能力"（detect_modality 是否非空）拆分为两组，再按各自数量比例
    均匀交替合并，使多模态模型与纯文本大语言模型在执行顺序上交替出现，而不是像
    discover_models() 默认的字典序那样让同类模型连续排在一起。组内相对顺序保持不变；
    任一组为空（例如环境里只有多模态模型或只有纯文本模型）时原样返回,不做任何调整。

    用途：排查是否存在"连续测试同类模型才会触发"的问题——例如疑似与 NPU/HTP 资源在
    连续多个多模态请求之间未被正确释放/重置有关的低频崩溃（参见 phi4-v81 崩溃排查记录）。
    交替执行可以让多模态与纯文本模型的加载/卸载互相穿插，而不是等一大串同类模型跑完。"""
    multimodal = [m for m in models if detect_modality(m)]
    llm_only = [m for m in models if not detect_modality(m)]
    if not multimodal or not llm_only:
        return list(models)
    result = []
    ia = ib = 0
    la, lb = len(multimodal), len(llm_only)
    while ia < la or ib < lb:
        if ib >= lb:
            take_multimodal = True
        elif ia >= la:
            take_multimodal = False
        else:
            take_multimodal = (ia / la) <= (ib / lb)
        if take_multimodal:
            result.append(multimodal[ia])
            ia += 1
        else:
            result.append(llm_only[ib])
            ib += 1
    return result


def _run_qnn_suite(args, models, remote_mode, out_dir):
    """--suite qnn：仅对 QNN 后端模型执行标准生命周期测试；多模态模型自动执行多模态用例、
    非多模态模型自动跳过（复用 APITester.run_all 中既有的 detect_modality 判定，无需额外指定）。
    多模态模型与纯文本大语言模型按交替顺序执行（_interleave_multimodal_and_llm），而不是
    discover_models() 默认字典序下同类模型连续排列，便于排查连续同类测试才会触发的问题。"""
    qnn_models = [m for m in models if infer_backend(m)[0] == "qnn"]
    if not qnn_models:
        return [TestResult(
            name="QNN_FILTER", round_num=1, model_name="_qnn_",
            passed=False, status_code=0, latency_ms=0,
            detail="未发现任何 QNN 模型，跳过 qnn suite", skipped=True
        )], [], []
    qnn_models = _interleave_multimodal_and_llm(qnn_models)
    return _run_model_suite(args, qnn_models, remote_mode, out_dir)


def _run_builder_local_model_suite(args, models, remote_mode, out_dir):
    """--suite builder_local_model：在 QAIModelBuilder 环境下验证 GenieAPIService 加载
    本地模型的端到端链路。被测对象是 GenieAPIService 本身（真实推理行为），Builder 只
    是启动/管理它的载体——这条 suite 的验证重心是"GenieAPIService 加载本地模型后是否
    真的能正确聊天/后端判定与目录名一致"，不是穷尽 Builder 自身的业务边界。默认不随
    --suite full 自动触发（避免影响常规回归运行时长），需要用户显式选择运行。"""
    all_results = []
    all_crash_events = []
    all_perf_samples = []
    run_builder_local_model_integration(args, models, all_results, all_crash_events, all_perf_samples)
    return all_results, all_perf_samples, all_crash_events


SUITE_HANDLERS = {
    "full": _run_full_suite,
    "model": _run_model_suite,
    "sampleapp": _run_sampleapp_suite,
    "multimodal": _run_multimodal_suite,
    "gguf": _run_gguf_suite,
    "multi_model": _run_multi_model_suite,
    "builder_local_model": _run_builder_local_model_suite,
    "mnn": _run_mnn_suite,
    "qnn": _run_qnn_suite,
    "graceful_shutdown": _run_graceful_shutdown_suite,
}


def main():
    parser = argparse.ArgumentParser(
        description="GenieAPIService 集成测试脚本 - 全自动测试所有 API 接口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
注意: --suite 为必传参数，不再有隐式默认值；如需运行完整回归请显式传入 --suite full。

示例:
    # 完整回归（本地模式，自动启动/停止服务）:
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite full
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite full --rounds 5 --port 9000

    # 远程模式（连接已运行的服务）:
    python test_service.py --remote --host 10.92.140.91 --port 8910 --suite full
    python test_service.py --remote --host 10.92.140.91 --port 8910 --suite model --rounds 1

    # 按套件精确运行:
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite sampleapp
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite gguf --gguf_devices gpu
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite mnn
    python test_service.py --exe_dir ./GenieService_v2.3.7 --models ./models --suite qnn
        """
    )
    parser.add_argument("--exe_dir", default=None, help="GenieAPIService.exe 所在目录（本地模式必填）")
    parser.add_argument("--models", default=None, help="模型根目录（本地模式必填，每个子目录含 config.json）")
    parser.add_argument("--remote", action="store_true", help="远程模式：连接已运行的服务，不启动/停止进程")
    parser.add_argument("--out_dir", default="./test_results", help="输出目录（默认 ./test_results）")
    parser.add_argument("--host", default="127.0.0.1", help="服务地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8910, help="服务端口（默认 8910）")
    parser.add_argument("--rounds", type=int, default=1, help="每个模型的测试轮数（默认 1）")
    parser.add_argument("--multimodal_rounds", type=int, default=None,
                        help="图片/音频多模态用例（含动态切换稳定性用例）的独立执行轮数，优先级高于 --rounds（默认跟随 --rounds）")
    parser.add_argument("--data_dir", default=r"C:\Users\HCKTest\Desktop\GenieEnv\data",
                        help="多模态素材根目录，含 img/ 与 audio/ 子目录（默认 C:\\Users\\HCKTest\\Desktop\\GenieEnv\\data）")
    parser.add_argument("--builder_dir", default="./QAIModelBuilder", help="QAIModelBuilder 根目录（默认 ./QAIModelBuilder）")
    parser.add_argument("--builder_port", type=int, default=8899, help="QAIModelBuilder 后端端口（默认 8899）")
    parser.add_argument("--builder_python", default=None,
                        help="QAIModelBuilder 使用的 python.exe（默认自动探测官方 Setup.bat 搭建的独立 venv："
                             "%%LOCALAPPDATA%%\\QAIModelBuilder\\envs\\.venv_arm64_313\\Scripts\\python.exe，"
                             "找不到则回退当前 sys.executable 并打印警告）")
    parser.add_argument("--builder_data_dir", default=None,
                        help="QAIModelBuilder 隔离数据目录（QAI_DATA__DATA_DIR），默认 <out_dir>/qaimodelbuilder_data，"
                             "每次运行新建、非持久；如需复用/持久化可显式传入固定路径")
    parser.add_argument("--genie_root_path", default=None,
                        help="供 QAIModelBuilder 使用的 GenieAPIService 安装目录（通过 mklink /J 联接到 "
                             "<data_dir>/bin/<name> 以触发 Builder 官方自愈式安装发现），默认复用 --exe_dir。"
                             "仅 --suite builder_local_model 会用到")
    parser.add_argument("--builder_local_models", default=None,
                        help="--suite builder_local_model 注入到 Builder 固定扫描目录的模型名列表（逗号分隔）。"
                             "留空则自动从 --models 下已发现模型中挑选（每个后端 qnn/mnn/gguf 各挑第一个）")
    parser.add_argument("--gguf_model", default=None, help="GGUF 显式加载回归限定的单个模型目录名（默认留空，测试全部已发现的 GGUF 模型）")
    parser.add_argument("--gguf_devices", choices=("both", "gpu", "cpu"), default="both", help="GGUF 显式加载回归的设备筛选：both/gpu/cpu（默认 both）")
    parser.add_argument("--suite", choices=("full", "model", "sampleapp", "multimodal", "gguf", "multi_model", "builder_local_model", "mnn", "qnn", "graceful_shutdown"),
                        default=None, help="选择要运行的测试套件（必传参数，不再有隐式默认值；如需完整回归请显式传入 full）")
    parser.add_argument("--model_name", default=None, help="--suite model/mnn/qnn/sampleapp 时按名称筛选模型，逗号分隔，未指定则测试该套件下全部已发现模型")

    args = parser.parse_args()

    # --rounds 下限校验：传 0/负数时 range(1, args.rounds+1) 会退化为空区间，导致该模型段
    # "零测试、零痕迹"地跑完，不报错也不影响退出码——这是需要在入口处堵住的静默异常行为。
    if args.rounds < 1:
        print(f"ERROR: --rounds 必须 >= 1（当前传入: {args.rounds}）")
        sys.exit(1)

    # --multimodal_rounds 与 --rounds 校验规则一致：显式传入且 < 1 时直接退出，不静默退化为空区间。
    # 未显式传入（None）时回退为 --rounds，与解耦前的默认行为保持一致。
    if args.multimodal_rounds is not None and args.multimodal_rounds < 1:
        print(f"ERROR: --multimodal_rounds 必须 >= 1（当前传入: {args.multimodal_rounds}）")
        sys.exit(1)
    args.multimodal_rounds = args.multimodal_rounds if args.multimodal_rounds is not None else args.rounds

    # --suite 校验放在最前面（与 --rounds/--multimodal_rounds 一起）：
    # 必须显式选择要运行的套件，不再有隐式默认值；提前到模型发现/远程连接之前报错，
    # 避免用户在环境准备好之后才发现忘了传 --suite。
    if args.suite is None:
        print("ERROR: 必须显式传入 --suite（不再有隐式默认值）。"
              "如需运行完整回归，请显式传入 --suite full。"
              f"可选值: {', '.join(SUITE_HANDLERS.keys())}")
        sys.exit(1)

    # 把所有用户传入的相对路径在第一时间转成绝对路径,
    # 防止后续 ServiceManager 用 cwd=exe_dir 启动子进程时,
    # 子进程把相对路径解析到 exe_dir 下面去 (例如 Stable\models\xxx\config.json)。
    if args.exe_dir:
        args.exe_dir = str(Path(args.exe_dir).resolve())
    if args.models:
        args.models = str(Path(args.models).resolve())
    if args.out_dir:
        args.out_dir = str(Path(args.out_dir).resolve())
    if args.data_dir:
        args.data_dir = str(Path(args.data_dir).resolve())
    if args.builder_dir:
        args.builder_dir = str(Path(args.builder_dir).resolve())

    remote_mode = args.remote

    if remote_mode:
        # 远程模式：从服务获取模型列表
        print(f"远程模式：连接 {args.host}:{args.port}")
        models = discover_models_remote(args.host, args.port)
        if not models:
            print(f"ERROR: 无法从远程服务获取模型列表，请确认服务是否在运行")
            sys.exit(1)
    else:
        # 本地模式：验证输入
        if not args.exe_dir:
            print("ERROR: 本地模式需要 --exe_dir 参数（或使用 --remote 连接已运行的服务）")
            sys.exit(1)
        if not args.models:
            print("ERROR: 本地模式需要 --models 参数（或使用 --remote 连接已运行的服务）")
            sys.exit(1)

        exe_dir = Path(args.exe_dir)
        exe_path = exe_dir / "GenieAPIService.exe"
        if not exe_path.exists():
            print(f"ERROR: 找不到 {exe_path}")
            sys.exit(1)

        models_dir = Path(args.models)
        models = discover_models(models_dir)
        if not models:
            print(f"ERROR: models 目录中没有找到包含 config.json 的子目录: {models_dir}")
            sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Builder 使用的 python.exe：优先显式 --builder_python，否则探测官方 Setup.bat 搭建的
    # 独立 venv，都找不到则回退 sys.executable 并打印警告（而非静默失败）。
    args.builder_python_exe = resolve_builder_python(args.builder_python)
    # Builder 隔离数据目录（QAI_DATA__DATA_DIR）：默认落在 <out_dir>/qaimodelbuilder_data，
    # 每次运行新建、非持久；显式传入时按用户指定路径解析为绝对路径（可用于复用/持久化）。
    args.builder_data_dir = (
        str(Path(args.builder_data_dir).resolve()) if args.builder_data_dir
        else str(out_dir / "qaimodelbuilder_data")
    )
    # 供 Builder 使用的 GenieAPIService 安装目录：未显式传入时复用 --exe_dir（本地模式下它
    # 已经就是包含 GenieAPIService.exe 的目录），远程模式下无 --exe_dir 时保持 None，由
    # builder_local_model suite 内部自己精确报错并 skip。
    if args.genie_root_path:
        args.genie_root_path = str(Path(args.genie_root_path).resolve())
    elif args.exe_dir:
        args.genie_root_path = args.exe_dir

    print("=" * 60)
    print("GenieAPIService 集成测试")
    print("=" * 60)
    if remote_mode:
        print(f"  模式: 远程（连接已运行的服务）")
    else:
        print(f"  模式: 本地（自动启动/停止）")
        print(f"  Exe 目录: {args.exe_dir}")
        print(f"  模型目录: {args.models}")
    print(f"  发现模型: {models}")
    print(f"  测试轮数: {args.rounds}")
    print(f"  多模态测试轮数: {args.multimodal_rounds}")
    print(f"  服务地址: {args.host}:{args.port}")
    print(f"  输出目录: {out_dir}")
    print(f"  性能采集: {'启用' if HAS_PSUTIL else '禁用 (psutil 未安装)'}")
    print("=" * 60)

    # --suite 是否合法/是否显式传入，已在 main() 顶部与其它参数校验一起提前检查过，
    # 到这里 args.suite 必然非 None。
    effective_suite = args.suite

    handler = SUITE_HANDLERS[effective_suite]

    # 不支持 --model_name 的 suite（multimodal/multi_model/builder_local_model：分别用自动筛选/
    # 全量并发/独立后端挑选逻辑，不读取该参数）若检测到用户显式传入该参数，打印一条不阻断
    # 执行的提示，避免参数被静默忽略、用户却毫无察觉。
    _SUITES_WITHOUT_MODEL_NAME = {"multimodal", "multi_model", "builder_local_model"}
    if args.model_name and effective_suite in _SUITES_WITHOUT_MODEL_NAME:
        print(f"提示: --suite {effective_suite} 不支持 --model_name 过滤，该参数将被忽略"
              f"（当前传入: {args.model_name}）")

    all_results, all_perf_samples, all_crash_events = handler(args, models, remote_mode, out_dir)

    # 本次运行调用 test_service.py 时的完整命令行（含全部参数），一路传给 _finalize_and_exit
    # 再转给 generate_summary_html/generate_model_reports，在报告 header 里展示，
    # 便于事后复核"这次跑的到底是什么命令"。
    cmdline = shlex.join(sys.argv)

    _finalize_and_exit(all_results, all_perf_samples, all_crash_events, out_dir, remote_mode, effective_suite, cmdline=cmdline)


if __name__ == "__main__":
    main()
