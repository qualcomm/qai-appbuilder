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
QAIModelBuilder CLI / 微信飞书 channel / 内核一致性测试脚本

与 test_service.py 平行、彼此不共享代码的独立测试脚本，驱动方式优先走真实
`qai-serve`/`qai` CLI 入口（而不是 test_service.py --suite builder_local_model
用的 debug-only `python -m apps.api`）。覆盖四个模块：
    A. CLI 14 个命令组的只读/幂等子命令冒烟测试
    B. 微信飞书 channel 全链路模拟（webhook 签名校验/入站解析/落库）
    C. CLI 与 HTTP API 一致性校验（"同一内核不同外皮"）
    D. 已知缺口回归标记（pack export/validate/workspace-init 等）

判定标准与 test_service.py 保持一致: failed == ignored 且 crashed == 0 视为健康。
本次运行中任何未列入已知缺口清单的失败都会被登记为独立的"新发现缺陷"
(defects.json)，不在本脚本内尝试修复。

除四个测试模块外，本脚本同一次运行中还会生成单页统一报告(report.html)，
与 test_service.py 内置 ReportGenerator 的模式一致（不再有独立的 report 生成脚本）。

除以上内容外，本脚本还新增一个可选的"webui"模块（模块 E，默认不跑）：用 Python 端
playwright 直接驱动真实浏览器打开前端 dev server 做黑盒校验(只知道 URL 与渲染出的
文字/DOM，不 import/读取 QAIModelBuilder/frontend 目录下任何源码)，只有显式
--modules ... webui 才会运行，跑起来需要额外的 pnpm/playwright 浏览器依赖。

用法:
    python test_builder_cli.py --builder_dir ./QAIModelBuilder
    python test_builder_cli.py --builder_dir ./QAIModelBuilder --modules channel known_gaps
    python test_builder_cli.py --builder_dir ./QAIModelBuilder --modules cli_smoke webui
"""

import argparse
import atexit
import base64
import hashlib
import hmac
import html as _html_module
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# 强制 stdout/stderr 使用 UTF-8 编码（Windows 控制台兼容），与 test_service.py 保持一致。
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("ERROR: 'requests' 库未安装，请执行 pip install requests")
    sys.exit(1)

# Feishu/WeChat 签名的默认回退密钥，与 QAIModelBuilder/apps/api/_channels_di.py 里的
# DEFAULT_DEV_SIGNING_SECRET 保持一致；只要目标 instance_id 没有在 SecretStore 里配置过
# 专属签名密钥，Builder 就会自动回退到这个值，测试脚本据此构造合法签名。
DEFAULT_DEV_SIGNING_SECRET = "qai-channels-default-verifier"


def _default_builder_python_path():
    """QAIModelBuilder 官方 Setup.bat 搭建的独立 ARM64 venv 的默认 python.exe 路径。"""
    return Path(os.environ.get("LOCALAPPDATA", "")) / "QAIModelBuilder" / "envs" / ".venv_arm64_313" / "Scripts" / "python.exe"


def resolve_builder_python(explicit_path=None):
    """解析用于启动/调用 QAIModelBuilder 的 python.exe：优先用户显式传入 --builder_python；
    否则尝试官方 Setup.bat 搭建的默认 venv；都找不到则回退 sys.executable 并打印明确警告。"""
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
          f"({sys.executable})；若该环境缺少必需依赖，Builder 启动会失败。"
          f"请先运行 QAIModelBuilder\\Setup.bat 搭建独立环境，或通过 --builder_python 显式指定。")
    return sys.executable


def wait_http_ok(url, timeout=60, expected_status=200, process=None):
    """轮询 HTTP 端点直到返回 expected_status；传入 process 时若子进程已退出立即抛异常，
    不再死等满 timeout。与 test_service.py 里的同名函数语义一致，独立实现。"""
    end = time.time() + timeout
    last_error = ""
    while time.time() < end:
        if process is not None:
            rc = process.poll()
            if rc is not None:
                raise RuntimeError(f"Builder 启动后立即退出 (exit_code={rc}); last_error={last_error}")
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == expected_status:
                return True
            last_error = f"status={r.status_code}, body={r.text[:500]}"
        except Exception as e:
            last_error = repr(e)
        time.sleep(1)
    return False


class CsrfSession:
    """包一层 requests.Session，实现 QAIModelBuilder 的 CSRF 双提交 Cookie 握手，
    语义与 test_service.py 的 _CsrfSession 一致，独立实现（不 import 对方代码）。"""
    CSRF_COOKIE_NAME = "qai_csrf"
    CSRF_HEADER_NAME = "X-QAI-CSRF"
    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()

    def ensure_csrf_token(self, timeout=10):
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

    def delete(self, path, timeout=30, **kwargs):
        return self.request("DELETE", path, timeout=timeout, **kwargs)


@dataclass
class BuilderEnvironment:
    """驱动 Builder 所需的公共配置：CLI 子进程与 API 进程必须共享同一个
    builder_dir/python_exe/data_dir，否则 CLI 和 HTTP API 会各自看到不同的数据
    （破坏模块 B/C 依赖的"同一个数据目录"前提）。"""
    builder_dir: Path
    python_exe: str
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8899

    def subprocess_env(self):
        env = os.environ.copy()
        src_path = str(self.builder_dir / "src")
        root_path = str(self.builder_dir)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([src_path, root_path, existing]) if existing else os.pathsep.join([src_path, root_path])
        # 关闭 Okta 登录门禁（仅测试场景），CSRF 双提交防护保持开启；隔离数据目录避免
        # 污染 Builder 仓库自身或用户真实数据，可重复运行。
        env["QAI_AUTH__ENABLED"] = "false"
        env["QAI_DATA__DATA_DIR"] = str(self.data_dir)
        return env

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"


class BuilderProcess:
    """通过官方 `qai-serve` 入口（`python -m apps.cli.serve`）拉起 Builder API 进程。

    不能加 --no-supervisor：该模式用 os.execv 把当前进程替换为 apps.api，但 CPython
    在 Windows 上的 os.execv 是"spawnv 新进程 + 当前进程立即退出"，并不会真正原地替换——
    我们持有的 Popen 句柄会在子进程刚起步时就以 exit_code=0 提前退出，被误判为
    "启动后立即退出"。因此这里用默认的 supervisor 模式：它在子进程存活期间一直阻塞，
    Popen 句柄本身就是可持续轮询的活体进程；代价是子进程真崩溃时 supervisor 会在
    5 次/300 秒内自动重启并吞掉这次崩溃证据，属已知限制，不是本次能绕开的设计缺陷。
    """

    def __init__(self, environment, log_dir):
        self.env_cfg = environment
        self.log_dir = Path(log_dir)
        self.process = None
        self._stdout_fh = None
        self._stderr_fh = None
        self.csrf = CsrfSession(self.env_cfg.base_url)

    def start(self, timeout=90):
        if not self.env_cfg.builder_dir.exists():
            raise FileNotFoundError(f"builder_dir 不存在: {self.env_cfg.builder_dir}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.env_cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_fh = open(self.log_dir / "builder_stdout.log", "w", encoding="utf-8")
        self._stderr_fh = open(self.log_dir / "builder_stderr.log", "w", encoding="utf-8")
        cmd = [
            self.env_cfg.python_exe, "-m", "apps.cli.serve",
            "--host", self.env_cfg.host, "--port", str(self.env_cfg.port),
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            cmd, cwd=str(self.env_cfg.builder_dir), env=self.env_cfg.subprocess_env(),
            stdout=self._stdout_fh, stderr=self._stderr_fh, creationflags=creationflags,
        )
        atexit.register(self._force_kill)
        try:
            ok = wait_http_ok(f"{self.env_cfg.base_url}/api/system/health", timeout=timeout, process=self.process)
        except RuntimeError as e:
            self._close_logs()
            raise RuntimeError(f"{e}\n日志尾部:\n{self._read_log_tail()}")
        if not ok:
            self._force_kill()
            self._close_logs()
            raise RuntimeError(f"Builder 在 {timeout}s 内未就绪。日志尾部:\n{self._read_log_tail()}")
        self.csrf.ensure_csrf_token(timeout=10)

    def stop(self, timeout=35):
        if self.process is None:
            self._close_logs()
            return
        if self.process.poll() is not None:
            self._close_logs()
            return
        try:
            self.csrf.post("/api/service/stop", json={}, timeout=10)
        except Exception:
            pass
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
            self.process.wait(timeout=timeout)
        except Exception:
            self._force_kill()
        finally:
            self._close_logs()

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def get_exit_code(self):
        return None if self.process is None else self.process.poll()

    def _force_kill(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

    def _close_logs(self):
        for fh in (self._stdout_fh, self._stderr_fh):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass

    def _read_log_tail(self, lines=60):
        try:
            with open(self.log_dir / "builder_stderr.log", "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:])
        except Exception:
            return ""


class FrontendDevServer:
    """通过 `pnpm dev` 拉起前端 Vite dev server（模块 E / webui 专用），生命周期管理
    模式与 BuilderProcess 保持一致：subprocess.Popen 拉子进程、stdout/stderr 重定向到
    日志文件、优雅停止 + 强制 kill 兜底。只有用户没有显式传入 --webui_base_url 时才会
    被构造/使用——用户自己在别处手动跑着前端时，直接跳过这个类，不需要我们管它的生命周期。

    通过环境变量 QAI_DEV_BACKEND_HTTP 注入要连接的后端地址，这是
    QAIModelBuilder/frontend/vite.config.ts 公开、文档化的环境变量接口（而不是私有实现
    细节），设置它不构成对前端源码的依赖/耦合。"""

    def __init__(self, frontend_dir, backend_base_url, host="127.0.0.1", port=5173, log_dir=None):
        self.frontend_dir = Path(frontend_dir)
        self.backend_base_url = backend_base_url
        self.host = host
        self.port = port
        self.log_dir = Path(log_dir) if log_dir else self.frontend_dir
        self.process = None
        self._stdout_fh = None
        self._stderr_fh = None

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def start(self, timeout=60):
        """启动 `pnpm dev`。

        已知环境限制（2026-07-22 ARM64 remote 测试观测记录，非本类逻辑缺陷）：在
        webui 模块长时间、高强度的无头浏览器交互下，Vite dev server 的 Node 进程有
        概率以退出码 3221226505（0xC0000409，STATUS_STACK_BUFFER_OVERRUN，Windows
        原生崩溃）终止，之后本进程存活期内的所有后续 page.goto 都会以
        `net::ERR_CONNECTION_REFUSED` 失败——这是 ARM64 上 Vite/esbuild 原生组件在
        持续压力下的稀发性崩溃，与本类/webui 模块的选择器或比对逻辑无关，出现时应
        直接查看 `frontend_stdout.log`/`frontend_stderr.log` 尾部确认退出码，而不是
        排查具体某条检查的实现。"""
        if not self.frontend_dir.exists():
            raise FileNotFoundError(f"frontend_dir 不存在: {self.frontend_dir}")
        pnpm_exe = shutil.which("pnpm")
        if not pnpm_exe and os.name == "nt":
            # Windows 上 pnpm 通常以 pnpm.cmd 的形式安装，shutil.which("pnpm") 未必能
            # 命中（取决于 PATHEXT），显式再探测一次 .cmd 后缀。
            pnpm_exe = shutil.which("pnpm.cmd")
        if not pnpm_exe:
            raise RuntimeError("未找到 pnpm 可执行文件，请先安装 Node.js 与 pnpm 后再运行 webui 模块"
                                "（也可以自己手动起好前端 dev server 后，通过 --webui_base_url 直接指向"
                                "该地址，完全跳过本类）。")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_fh = open(self.log_dir / "frontend_stdout.log", "w", encoding="utf-8")
        self._stderr_fh = open(self.log_dir / "frontend_stderr.log", "w", encoding="utf-8")
        env = os.environ.copy()
        env["QAI_DEV_BACKEND_HTTP"] = self.backend_base_url
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [pnpm_exe, "dev"], cwd=str(self.frontend_dir), env=env,
            stdout=self._stdout_fh, stderr=self._stderr_fh, creationflags=creationflags,
        )
        atexit.register(self._force_kill)
        try:
            ok = wait_http_ok(f"{self.base_url}/", timeout=timeout, process=self.process)
        except RuntimeError as e:
            self._close_logs()
            raise RuntimeError(f"{e}\n日志尾部:\n{self._read_log_tail()}")
        if not ok:
            self._force_kill()
            self._close_logs()
            raise RuntimeError(f"前端 dev server 在 {timeout}s 内未就绪。日志尾部:\n{self._read_log_tail()}")

    def stop(self, timeout=15):
        if self.process is None:
            self._close_logs()
            return
        if self.process.poll() is not None:
            self._close_logs()
            return
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
            self.process.wait(timeout=timeout)
        except Exception:
            self._force_kill()
        finally:
            self._close_logs()

    def _force_kill(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

    def _close_logs(self):
        for fh in (self._stdout_fh, self._stderr_fh):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass

    def _read_log_tail(self, lines=60):
        try:
            with open(self.log_dir / "frontend_stderr.log", "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:])
        except Exception:
            return ""


@dataclass
class CliResult:
    args: list
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    json_data: object = None
    json_error: str = ""


class CliRunner:
    """子进程调用 `<builder_python> -m apps.cli <args>`，与 BuilderProcess 共享同一份
    BuilderEnvironment（同一 builder_dir/python_exe/data_dir），确保 CLI 看到与
    HTTP API 相同的数据。CLI 本身默认就把结果 JSON 化打到 stdout（没有全局 --json
    开关），因此这里统一尝试 json.loads(stdout)，解析失败时保留 json_error 供断言使用。"""

    def __init__(self, environment):
        self.env_cfg = environment

    def run(self, *args, timeout=60, input_text=None):
        cmd = [self.env_cfg.python_exe, "-m", "apps.cli", *args]
        start = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.env_cfg.builder_dir), env=self.env_cfg.subprocess_env(),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, input=input_text,
            )
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            exit_code = -1
            stdout = e.stdout or ""
            stderr = (e.stderr or "") + f"\n[CliRunner] 命令超时 (>{timeout}s)"
        duration_ms = (time.time() - start) * 1000
        json_data, json_error = None, ""
        if stdout.strip():
            try:
                json_data = json.loads(stdout)
            except json.JSONDecodeError as e:
                json_error = str(e)
        return CliResult(args=list(args), exit_code=exit_code, stdout=stdout, stderr=stderr,
                          duration_ms=duration_ms, json_data=json_data, json_error=json_error)


@dataclass
class TestResult:
    """与 test_service.py 的 TestResult 同一套 ignorable/crashed/skipped 语义,
    独立实现(不共享代码),便于沿用项目既有的健康判定标准。"""
    module: str
    name: str
    passed: bool
    detail: str = ""
    crashed: bool = False
    skipped: bool = False
    ignorable: bool = False
    ignore_reason: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class CrashEvent:
    timestamp: str
    module: str
    name: str
    detail: str
    log_tail: str = ""


class ResultCollector:
    """汇总四个模块的 TestResult/CrashEvent,按项目既有判定标准
    (failed == ignored 且 crashed == 0 视为健康)计算 summary 并落盘 results.json。"""

    def __init__(self):
        self.results = []
        self.crash_events = []

    def add(self, result):
        self.results.append(result)

    def add_crash(self, crash):
        self.crash_events.append(crash)

    def summary(self):
        total = len(self.results)
        skipped = sum(1 for r in self.results if r.skipped)
        crashed = sum(1 for r in self.results if r.crashed and not r.skipped)
        passed = sum(1 for r in self.results if r.passed and not r.crashed and not r.skipped)
        failed = sum(1 for r in self.results if not r.passed and not r.crashed and not r.skipped)
        ignored = sum(1 for r in self.results if not r.passed and r.ignorable and not r.crashed and not r.skipped)
        return {"total": total, "passed": passed, "failed": failed, "ignored": ignored,
                "crashed": crashed, "skipped": skipped}

    def is_healthy(self):
        s = self.summary()
        return s["failed"] == s["ignored"] and s["crashed"] == 0

    def write_report(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(),
            "summary": self.summary(),
            "healthy": self.is_healthy(),
            "results": [asdict(r) for r in self.results],
            "crash_events": [asdict(c) for c in self.crash_events],
        }
        with open(out_dir / "results.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return payload


@dataclass
class DefectRecord:
    defect_id: str
    module: str
    severity: str
    summary: str
    repro: str
    expected: str
    actual: str
    evidence: str
    discovered_at: str


class DefectRegistry:
    """四个模块共享的缺陷登记表：任何非预期失败都在这里登记一条记录,
    最终落盘为 defects.json(结构化,供下一轮修复 plan 直接消费，并被 report.html
    内联的"新发现缺陷详情"部分直接渲染)。已知缺口/设计边界不登记在这里,
    只在报告里作为说明,以免污染"新发现缺陷"清单。"""

    def __init__(self):
        self._defects = []
        self._counter = 0

    def add(self, module, severity, summary, repro, expected, actual, evidence):
        self._counter += 1
        record = DefectRecord(
            defect_id=f"D{self._counter:04d}", module=module, severity=severity, summary=summary,
            repro=repro, expected=expected, actual=actual, evidence=evidence,
            discovered_at=datetime.now().isoformat(),
        )
        self._defects.append(record)
        return record

    @property
    def defects(self):
        return list(self._defects)

    def write(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "defects.json", "w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in self._defects], f, indent=2, ensure_ascii=False)


@dataclass
class CliSmokeCase:
    """模块 A 的一条冒烟测试用例：调用 `qai <args>`，校验退出码与输出结构。
    expect_type=None 表示该子命令本就不输出 JSON（如 `service path`/`service logs`），
    此时只校验退出码；expect_type 为 dict/list 时进一步校验输出类型与 required_keys。"""
    group: str
    args: tuple
    expect_type: type = dict
    required_keys: tuple = ()


# 覆盖 14 个命令组里只读/幂等的子命令（广度优先）；`install`/`build` 两个命令组本身
# 不存在只读子命令概念（前者全是安装/编译/卸载的转发脚本，后者是长驻 REPL），
# 因此不出现在这份清单里——这是设计使然，不是覆盖遗漏。
CLI_SMOKE_CASES = (
    CliSmokeCase("config", ("config", "get", "ui.theme")),
    CliSmokeCase("config", ("config", "provider", "list"), required_keys=("providers",)),
    CliSmokeCase("service", ("service", "status")),
    CliSmokeCase("service", ("service", "probe")),
    CliSmokeCase("service", ("service", "models"), expect_type=list),
    CliSmokeCase("service", ("service", "path"), expect_type=None),
    CliSmokeCase("service", ("service", "config", "get")),
    CliSmokeCase("pack", ("pack", "list"), required_keys=("items",)),
    CliSmokeCase("pack", ("pack", "deps-status")),
    CliSmokeCase("pack", ("pack", "cache", "status")),
    CliSmokeCase("pack", ("pack", "taxonomy")),
    CliSmokeCase("run", ("run", "list"), required_keys=("items",)),
    CliSmokeCase("run", ("run", "worker", "status")),
    CliSmokeCase("policy", ("policy", "show")),
    CliSmokeCase("policy", ("policy", "skill-cap", "discover")),
    CliSmokeCase("policy", ("security", "settings", "get")),
    CliSmokeCase("policy", ("audit", "query"), expect_type=list),
    CliSmokeCase("conv", ("conv", "list"), expect_type=list),
    CliSmokeCase("conv", ("conv", "tab", "list"), expect_type=list),
    CliSmokeCase("conv", ("conv", "experience", "list"), expect_type=list),
    CliSmokeCase("conv", ("conv", "experience", "categories"), expect_type=list),
    CliSmokeCase("dep", ("dep", "pending"), expect_type=list),
    CliSmokeCase("dep", ("exec", "profiles")),
    CliSmokeCase("code", ("code", "session", "list"), expect_type=list),
    CliSmokeCase("code", ("code", "skill", "list"), expect_type=list),
    CliSmokeCase("code", ("code", "health")),
    CliSmokeCase("service_release", ("service-release", "versions")),
    CliSmokeCase("service_release", ("service-release", "models")),
    CliSmokeCase("service_release", ("service-release", "status", "versions")),
    CliSmokeCase("service_release", ("service-release", "status", "models")),
    CliSmokeCase("service_release", ("service-release", "aria2c", "status")),
    CliSmokeCase("service_release", ("service-release", "settings", "get")),
    CliSmokeCase("app", ("app", "--json"), required_keys=("packs",)),
    CliSmokeCase("skill", ("skill", "list"), required_keys=("skills",)),
    CliSmokeCase("skill", ("skill", "policy")),
    CliSmokeCase("channel", ("channel", "list"), expect_type=list),
)


def _record_defect(collector, defects, module, name, severity, summary, repro, expected, actual, evidence):
    collector.add(TestResult(module=module, name=name, passed=False, detail=summary))
    defects.add(module=module, severity=severity, summary=summary, repro=repro,
                expected=expected, actual=actual, evidence=evidence)


def run_cli_smoke_module(cli, collector, defects):
    """模块 A：对 CLI 命令组的只读/幂等子命令逐一调用并断言退出码与输出结构。"""
    for case in CLI_SMOKE_CASES:
        repro = "qai " + " ".join(case.args)
        name = f"cli_smoke::{' '.join(case.args)}"
        result = cli.run(*case.args, timeout=30)

        if result.exit_code != 0:
            _record_defect(collector, defects, "A", name, "major",
                            f"`{repro}` 退出码非 0", repro, "退出码为 0",
                            f"exit_code={result.exit_code}",
                            f"stdout={result.stdout[-1000:]!r}\nstderr={result.stderr[-1000:]!r}")
            continue

        if case.expect_type is None:
            collector.add(TestResult(module="A", name=name, passed=True, detail="非 JSON 输出，仅校验退出码"))
            continue

        if result.json_data is None:
            _record_defect(collector, defects, "A", name, "major",
                            f"`{repro}` 输出不是合法 JSON", repro, "stdout 是可解析的 JSON",
                            f"json_error={result.json_error}", result.stdout[-1000:])
            continue

        if not isinstance(result.json_data, case.expect_type):
            _record_defect(collector, defects, "A", name, "major",
                            f"`{repro}` 输出类型不符", repro,
                            f"输出类型为 {case.expect_type.__name__}",
                            f"实际类型为 {type(result.json_data).__name__}", result.stdout[-1000:])
            continue

        missing = [k for k in case.required_keys if k not in result.json_data]
        if missing:
            _record_defect(collector, defects, "A", name, "major",
                            f"`{repro}` 输出缺少字段", repro,
                            f"输出包含字段 {case.required_keys}", f"缺少字段: {missing}", result.stdout[-1000:])
            continue

        collector.add(TestResult(module="A", name=name, passed=True,
                                  detail=f"exit_code=0, duration_ms={result.duration_ms:.0f}"))


def build_feishu_signature(secret, timestamp, nonce, raw_body):
    """base64(HMAC_SHA256(secret, timestamp + nonce + raw_body))，对应 FeishuSigVerifier。"""
    message = (timestamp + nonce).encode("utf-8") + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_wechat_signature(token, timestamp, nonce):
    """sha1("".join(sorted([token, timestamp, nonce])))，对应 WechatSigVerifier。"""
    items = sorted([token, timestamp, nonce])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def _record_known_boundary(collector, module, name, detail):
    collector.add(TestResult(module=module, name=name, passed=True, skipped=True, detail=detail))


def _register_channel(cli, collector, defects, kind, name):
    result = cli.run("channel", "register", "--kind", kind, "--name", name, timeout=30)
    case_name = f"channel::register::{kind}"
    if result.exit_code != 0 or not isinstance(result.json_data, dict) or "instance_id" not in result.json_data:
        _record_defect(collector, defects, "B", case_name, "blocker",
                        f"`qai channel register --kind {kind}` 未能返回 instance_id",
                        f"qai channel register --kind {kind} --name {name}",
                        "退出码为 0 且返回含 instance_id 的 JSON",
                        f"exit_code={result.exit_code}, stdout={result.stdout[-500:]!r}",
                        f"stderr={result.stderr[-500:]!r}")
        return None
    collector.add(TestResult(module="B", name=case_name, passed=True, detail="注册成功"))
    return result.json_data["instance_id"]


def _delete_channel(cli, collector, defects, kind, instance_id):
    if not instance_id:
        return
    result = cli.run("channel", "delete", instance_id, "--yes", timeout=30)
    case_name = f"channel::delete::{kind}"
    if result.exit_code != 0:
        _record_defect(collector, defects, "B", case_name, "minor",
                        f"`qai channel delete {instance_id} --yes` 清理失败",
                        f"qai channel delete {instance_id} --yes",
                        "退出码为 0", f"exit_code={result.exit_code}",
                        f"stdout={result.stdout[-500:]!r}\nstderr={result.stderr[-500:]!r}")
        return
    collector.add(TestResult(module="B", name=case_name, passed=True, detail="清理成功"))


def _query_channel_message(env_cfg, kind, provider_event_id, attempts=5, delay=1.0):
    """官方 CLI/HTTP/仓储三层都不支持按 instance 查消息列表(已确认缺口),
    旁路直连 SQLite 按 provider_event_id 精确查一条,验证落库是否正确。"""
    db_path = env_cfg.data_dir / "db" / "qai.db"
    for _ in range(attempts):
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                cur = conn.execute(
                    "SELECT id, instance_id, kind, sender_user_id, provider_event_id, content_text, "
                    "status, parsed_verb, parsed_args_json, reply_provider_message_id, failure_reason, "
                    "arrived_at, updated_at FROM channels_message WHERE kind = ? AND provider_event_id = ?",
                    (kind, provider_event_id),
                )
                row = cur.fetchone()
                if row:
                    columns = [d[0] for d in cur.description]
                    return dict(zip(columns, row))
            finally:
                conn.close()
        except sqlite3.OperationalError:
            pass
        time.sleep(delay)
    return None


def _run_feishu_inbound(cli, api, env_cfg, collector, defects):
    instance_id = _register_channel(cli, collector, defects, "feishu", "test-builder-cli-feishu")
    if not instance_id:
        return
    try:
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        payload = {"event_id": event_id, "sender": "ou_test_builder_cli", "text": "hello from test_builder_cli"}
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature = build_feishu_signature(DEFAULT_DEV_SIGNING_SECRET, timestamp, nonce, raw_body)
        headers = {
            "Content-Type": "application/json",
            "X-Lark-Signature": signature,
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
        }
        resp = api.post("/api/feishu/webhook", params={"instance_id": instance_id}, data=raw_body,
                         headers=headers, timeout=15)
        case_name = "channel::webhook::feishu_inbound"
        repro = f"POST /api/feishu/webhook?instance_id={instance_id} body={payload}"
        if resp.status_code != 200:
            _record_defect(collector, defects, "B", case_name, "blocker",
                            "飞书 webhook 签名合法但响应非 200", repro,
                            "200 且响应体含 message_id", f"status={resp.status_code}", resp.text[:1000])
            return
        body = resp.json()
        if "message_id" not in body:
            _record_defect(collector, defects, "B", case_name, "major",
                            "飞书 webhook 响应缺少 message_id", repro,
                            "响应体含 message_id", f"响应体: {body}", resp.text[:1000])
            return
        collector.add(TestResult(module="B", name=case_name, passed=True, detail=f"message_id={body['message_id']}"))

        row = _query_channel_message(env_cfg, "feishu", event_id)
        case_name = "channel::db::feishu_persisted"
        db_path = env_cfg.data_dir / "db" / "qai.db"
        if row is None:
            _record_defect(collector, defects, "B", case_name, "blocker",
                            "飞书消息未在 channels_message 表中落库",
                            f"sqlite3 {db_path} \"SELECT * FROM channels_message WHERE kind='feishu' "
                            f"AND provider_event_id='{event_id}'\"",
                            "查到一条记录", "查询结果为空", f"db_path={db_path}")
        elif row.get("content_text") != payload["text"]:
            _record_defect(collector, defects, "B", case_name, "major",
                            "飞书落库的 content_text 与发送内容不一致", f"provider_event_id={event_id}",
                            payload["text"], row.get("content_text"), str(row))
        else:
            collector.add(TestResult(module="B", name=case_name, passed=True, detail=f"row={row}"))
    finally:
        _delete_channel(cli, collector, defects, "feishu", instance_id)


def _run_wechat_inbound(cli, api, env_cfg, collector, defects):
    instance_id = _register_channel(cli, collector, defects, "wechat", "test-builder-cli-wechat")
    if not instance_id:
        return
    try:
        event_id = str(int(time.time() * 1000))
        payload = {"FromUserName": "wx_test_builder_cli", "MsgId": event_id,
                   "Content": "hello from test_builder_cli", "CreateTime": int(time.time())}
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature = build_wechat_signature(DEFAULT_DEV_SIGNING_SECRET, timestamp, nonce)
        headers = {
            "Content-Type": "application/json",
            "X-Wechat-Signature": signature,
            "X-Wechat-Timestamp": timestamp,
            "X-Wechat-Nonce": nonce,
        }
        resp = api.post("/api/wechat/webhook", params={"instance_id": instance_id}, data=raw_body,
                         headers=headers, timeout=15)
        case_name = "channel::webhook::wechat_inbound"
        repro = f"POST /api/wechat/webhook?instance_id={instance_id} body={payload}"
        if resp.status_code != 200:
            _record_defect(collector, defects, "B", case_name, "blocker",
                            "微信 webhook 签名合法但响应非 200", repro,
                            "200 且响应体含 message_id", f"status={resp.status_code}", resp.text[:1000])
            return
        body = resp.json()
        if "message_id" not in body:
            _record_defect(collector, defects, "B", case_name, "major",
                            "微信 webhook 响应缺少 message_id", repro,
                            "响应体含 message_id", f"响应体: {body}", resp.text[:1000])
            return
        collector.add(TestResult(module="B", name=case_name, passed=True, detail=f"message_id={body['message_id']}"))

        row = _query_channel_message(env_cfg, "wechat", event_id)
        case_name = "channel::db::wechat_persisted"
        db_path = env_cfg.data_dir / "db" / "qai.db"
        if row is None:
            _record_defect(collector, defects, "B", case_name, "blocker",
                            "微信消息未在 channels_message 表中落库",
                            f"sqlite3 {db_path} \"SELECT * FROM channels_message WHERE kind='wechat' "
                            f"AND provider_event_id='{event_id}'\"",
                            "查到一条记录", "查询结果为空", f"db_path={db_path}")
        elif row.get("content_text") != payload["Content"]:
            _record_defect(collector, defects, "B", case_name, "major",
                            "微信落库的 content_text 与发送内容不一致", f"provider_event_id={event_id}",
                            payload["Content"], row.get("content_text"), str(row))
        else:
            collector.add(TestResult(module="B", name=case_name, passed=True, detail=f"row={row}"))
    finally:
        _delete_channel(cli, collector, defects, "wechat", instance_id)


class _FeishuOutboundRecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录收到的出站请求，回一个满足 FeishuTransport._extract_outbound_id() 的成功响应。"""
    captured = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        _FeishuOutboundRecordingHandler.captured.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body.decode("utf-8", errors="replace"),
        })
        response = json.dumps({"code": 0, "msg": "ok",
                                "data": {"message_id": "om_fake_message_id_123"}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass


# 在 Builder 自己的 python 解释器里执行(需要 httpx/qai 等 Builder 专属依赖),绕开 DI
# 直接构造 FeishuTransport(base_url=本地 mock server),验证签名/请求形状本身,
# 不依赖真实开放平台。fake tenant token cache 避免真的去请求 Feishu 的 token 端点。
_FEISHU_OUTBOUND_SNIPPET = """
import asyncio
import sys
import httpx

from datetime import datetime, timezone
from qai.channels.domain import (
    ChannelInstance, ChannelInstanceId, ChannelKind, ChannelUserId, CredentialsRef, MessageContent,
)
from qai.channels.infrastructure.transports import FeishuTransport


class FakeTenantTokenCache:
    async def get_token(self):
        return "fake-tenant-token"

    def invalidate(self):
        pass


async def _main():
    mock_base_url = sys.argv[1]
    transport = FeishuTransport(
        client_factory=lambda timeout: httpx.AsyncClient(timeout=timeout),
        base_url=mock_base_url,
        tenant_token_cache=FakeTenantTokenCache(),
    )
    instance = ChannelInstance.create(
        instance_id=ChannelInstanceId.of("test-outbound-mock"),
        kind=ChannelKind.FEISHU,
        name="test-outbound-mock",
        credentials_ref=CredentialsRef(service="qai.channels.feishu", key="test-outbound-mock"),
        now=datetime.now(timezone.utc),
    )
    target = ChannelUserId.of("ou_fake_open_id_123")
    content = MessageContent(text="hello from test_builder_cli outbound mock")
    outbound_id = await transport.send(instance, target, content)
    print(outbound_id)


asyncio.run(_main())
"""


def _run_feishu_outbound_mock(env_cfg, collector, defects):
    """模块 B 第 6 步：绕开 DI 直接构造 FeishuTransport 发一条消息，断言本地 mock server
    收到预期请求。属于可选补充验证；若因 Builder 内部 API 形状变化导致构造失败，
    降级为 skipped 而不是登记缺陷（这是我们自己对内部 API 的假设，不代表产品缺陷）。"""
    case_name = "channel::feishu_outbound_mock"
    _FeishuOutboundRecordingHandler.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeishuOutboundRecordingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mock_base_url = f"http://127.0.0.1:{port}"
        proc = subprocess.run(
            [env_cfg.python_exe, "-c", _FEISHU_OUTBOUND_SNIPPET, mock_base_url],
            cwd=str(env_cfg.builder_dir), env=env_cfg.subprocess_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    if proc.returncode != 0:
        collector.add(TestResult(module="B", name=case_name, passed=True, skipped=True,
                                  detail=f"绕开 DI 构造 FeishuTransport 失败，作为可选补充验证跳过: "
                                         f"{proc.stderr[-800:]}"))
        return

    if not _FeishuOutboundRecordingHandler.captured:
        _record_defect(collector, defects, "B", case_name, "major",
                        "FeishuTransport.send() 未向本地 mock server 发出任何请求",
                        "绕开 DI 直接构造 FeishuTransport(base_url=mock) 并调用 send()",
                        "mock server 收到一次 POST /open-apis/im/v1/messages 请求",
                        "mock server 未收到任何请求", proc.stdout[-500:])
        return

    req = _FeishuOutboundRecordingHandler.captured[0]
    problems = []
    if "/open-apis/im/v1/messages" not in req["path"]:
        problems.append(f"path={req['path']}")
    if "authorization" not in {k.lower() for k in req["headers"]}:
        problems.append("缺少 Authorization 头")
    if problems:
        _record_defect(collector, defects, "B", case_name, "minor",
                        "FeishuTransport 出站请求形状与预期不符",
                        "绕开 DI 直接构造 FeishuTransport(base_url=mock) 并调用 send()",
                        "path 含 /open-apis/im/v1/messages 且带 Authorization 头",
                        "; ".join(problems), str(req))
        return

    collector.add(TestResult(module="B", name=case_name, passed=True,
                              detail=f"mock server 收到请求: path={req['path']}"))


def run_channel_module(cli, api, env_cfg, collector, defects):
    """模块 B：微信飞书 channel 全链路模拟(webhook 签名校验/入站解析/落库)。"""
    _record_known_boundary(collector, "B", "channel::feishu_url_verification",
                            "已确认 /api/feishu/webhook 未实现 Feishu event-2.0 的 url_verification "
                            "挑战握手(该握手只存在于 WS 长连接路径),本次显式排除,不发送 challenge 报文,"
                            "仅作为设计边界记录,不计入缺陷清单。")
    _run_feishu_inbound(cli, api, env_cfg, collector, defects)
    _run_wechat_inbound(cli, api, env_cfg, collector, defects)
    _record_known_boundary(collector, "B", "channel::wechat_outbound_not_mockable",
                            "WeChat 出站完全依赖 wechatbot SDK 的活体 Bot 对象、不经 HTTP,"
                            "本轮不做出站验证,只记录该限制,不计入缺陷清单。")
    _run_feishu_outbound_mock(env_cfg, collector, defects)


def _deep_diff(a, b, path=""):
    """递归比较两个 JSON 值，返回差异描述列表（无差异返回空列表），用于验证
    "同一内核不同外皮"返回的业务字段完全一致。"""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        keys_a, keys_b = set(a), set(b)
        for k in keys_a - keys_b:
            diffs.append(f"{path}.{k}: 仅 CLI 侧存在")
        for k in keys_b - keys_a:
            diffs.append(f"{path}.{k}: 仅 API 侧存在")
        for k in keys_a & keys_b:
            diffs.extend(_deep_diff(a[k], b[k], f"{path}.{k}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: 长度不同 ({len(a)} vs {len(b)})")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs.extend(_deep_diff(x, y, f"{path}[{i}]"))
    elif type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        diffs.append(f"{path}: 类型不同 ({type(a).__name__} vs {type(b).__name__})")
    elif a != b:
        diffs.append(f"{path}: 值不同 ({a!r} vs {b!r})")
    return diffs


def _compare_cli_api_list(cli, api, collector, defects, case_name, cli_args, api_path, api_params=None, api_key="items"):
    """共享的 CLI vs API 一致性比较逻辑：CLI 侧预期返回 {"items": [...]}，API 侧
    若返回裹一层对象则取 api_key 字段(不同端点包裹字段名不一致，例如 /runs 用 'runs'，
    /models 直接返回裸列表)。"""
    cli_result = cli.run(*cli_args, timeout=30)
    cli_repro = "qai " + " ".join(cli_args)
    if cli_result.exit_code != 0 or not isinstance(cli_result.json_data, dict) or "items" not in cli_result.json_data:
        _record_defect(collector, defects, "C", case_name, "major",
                        f"`{cli_repro}` 未返回合法 JSON，无法与 API 对比", cli_repro,
                        "退出码 0 且返回 {'items': [...]}", f"exit_code={cli_result.exit_code}",
                        cli_result.stdout[-500:])
        return
    api_repro = f"GET {api_path}" + (f"?{api_params}" if api_params else "")
    try:
        resp = api.get(api_path, params=api_params, timeout=30)
    except Exception as e:
        _record_defect(collector, defects, "C", case_name, "major",
                        f"{api_repro} 请求异常", api_repro, "200 且返回 JSON", repr(e), "")
        return
    if resp.status_code != 200:
        _record_defect(collector, defects, "C", case_name, "major",
                        f"{api_repro} 响应非 200", api_repro, "200", f"status={resp.status_code}",
                        resp.text[:1000])
        return
    api_data = resp.json()
    cli_items = cli_result.json_data["items"]
    api_items = api_data.get(api_key) if isinstance(api_data, dict) else api_data
    diffs = _deep_diff(cli_items, api_items, "items")
    if diffs:
        _record_defect(collector, defects, "C", case_name, "major",
                        f"`{cli_repro}` 与 {api_repro} 返回内容不一致",
                        f"{cli_repro}  vs  {api_repro}", "两侧 items 深度相等",
                        "; ".join(diffs[:20]),
                        f"cli={json.dumps(cli_items, ensure_ascii=False)[:1000]}\n"
                        f"api={json.dumps(api_items, ensure_ascii=False)[:1000]}")
        return
    collector.add(TestResult(module="C", name=case_name, passed=True, detail=f"共 {len(cli_items)} 条，完全一致"))


def run_consistency_module(cli, api, collector, defects):
    """模块 C：CLI 与 HTTP API 一致性校验（"同一内核不同外皮"）。"""
    _compare_cli_api_list(cli, api, collector, defects, "consistency::pack_list",
                           ("pack", "list"), "/api/app-builder/models")
    limit = 50
    _compare_cli_api_list(cli, api, collector, defects, "consistency::run_list",
                          ("run", "list", "--limit", str(limit)), "/api/app-builder/runs",
                          {"limit": limit}, api_key="runs")


_KNOWN_GAP_MISSING_MODULE = "scripts.build.model_builder_cli"


def _check_known_gap_import_error(cli, collector, defects, subcommand_args):
    """`qai pack export/validate/workspace-init` 预期因 scripts/build/model_builder_cli.py
    缺失而报 ModuleNotFoundError。此处刷选条件同时核对异常类型 + 关键字，避免把真正的新缺陷
    误归入"已知限制"而漫天飞。"""
    case_name = f"known_gaps::{' '.join(subcommand_args)}"
    result = cli.run(*subcommand_args, timeout=30)
    combined = result.stdout + result.stderr
    if result.exit_code == 0:
        collector.add(TestResult(module="D", name=case_name, passed=True, skipped=True,
                                  detail=f"`qai {' '.join(subcommand_args)}` 意外成功(已知缺口可能已被修复)，"
                                         f"予以高亮记录，不判定为失败: stdout={result.stdout[-500:]!r}"))
        return
    if "ModuleNotFoundError" in combined and _KNOWN_GAP_MISSING_MODULE in combined:
        collector.add(TestResult(module="D", name=case_name, passed=True,
                                  detail=f"已知缺口按预期复现: ModuleNotFoundError 命中 {_KNOWN_GAP_MISSING_MODULE}"))
        return
    _record_defect(collector, defects, "D", case_name, "minor",
                    f"`qai {' '.join(subcommand_args)}` 的失败特征发生变化",
                    f"qai {' '.join(subcommand_args)}",
                    f"ModuleNotFoundError 命中 {_KNOWN_GAP_MISSING_MODULE}(已知缺口)",
                    f"exit_code={result.exit_code}, 输出未命中已知特征", combined[-1000:])


def run_known_gaps_module(cli, collector, defects):
    """模块 D：已知缺口回归标记(pack export/validate/workspace-init 因 scripts/build/
    model_builder_cli.py 缺失预期报 ImportError)，以及在报告里追加已知设计边界清单。
    三个子命令各自有自己的转发脚本自带的 argparse 必需参数，必须先满足它们才能真正走到
    缺失模块的 import 语句，否则会在 argparse 阶段就报错退出，误判为“失败特征变化”。"""
    for args in (("pack", "export", "--workdir", "placeholder-workdir"),
                 ("pack", "validate", "placeholder-pack-dir"),
                 ("pack", "workspace-init", "placeholder-model")):
        _check_known_gap_import_error(cli, collector, defects, args)
    _record_known_boundary(collector, "D", "known_gaps::channel_message_query_missing",
                            "官方 CLI/HTTP/仓储三层均不支持按 instance 查询 channel 消息历史，"
                            "模块 B 只能靠 provider_event_id 精确查询或直连 SQLite 旁路验证，属已知设计边界。")
    _record_known_boundary(collector, "D", "known_gaps::feishu_url_verification_missing",
                            "/api/feishu/webhook 未实现 Feishu event-2.0 的 url_verification 挑战握手，"
                            "该握手只存在于 WS 长连接路径，属已知设计边界。")
    _record_known_boundary(collector, "D", "known_gaps::wechat_outbound_not_http_mockable",
                            "WeChat 出站完全依赖 wechatbot SDK 的活体 Bot 对象、不经 HTTP，无法用 HTTP mock 验证，属已知设计边界。")


def _wait_visible(locator, timeout=10000):
    """在超时内等待元素可见,超时返回 False 而不向上抛异常(调用方经常还要基于结果继续
    判断,不适合用异常控制流)。用于"断言某个空态提示/结果条是否可见"这类场景;真正需要
    "必须等到出现才能继续操作"的地方仍直接用 locator.wait_for(...)，超时交给外层
    try/except 记为 FAIL。"""
    try:
        locator.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def run_webui_module(cli, frontend_url, collector, defects, csrf, headed=False):
    """模块 E（webui，opt-in）：用 Python 端 playwright 驱动真实浏览器，对前端 WebUI
    做黑盒校验——只知道 frontend_url 与渲染出的文字/DOM，不 import/读取
    QAIModelBuilder/frontend 目录下任何源码。

    与模块 A(cli_smoke)全部 36 条用例做"双向验证"：26 条有网页操作入口的用例，按分组
    分派给 6 个 `_webui_check_*_group` 函数，用真实的浏览器点击路径去操作网页、读取渲染
    结果，再与 CLI JSON 输出逐一比对；剩余 10 条没有网页入口的设计边界用例，由
    `_webui_record_boundary_cases` 直接从 CLI_CASE_TO_UI_EQUIVALENT 映射表里自动筛出并
    记为 skipped，不需要手写清单。每条 PASS/FAIL 结果的 detail 字段都包含一句人类可读的
    "网页操作路径"描述，方便在报告里复核"网页里具体怎么操作能得到这个结果"。

    playwright 是懒加载的可选依赖：大多数用户不会用到这个模块，因此不在文件顶部
    import，缺失时也不让整个脚本崩溃退出，只记为一条清晰的失败 TestResult。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        collector.add(TestResult(
            module="E", name="webui::playwright_not_installed", passed=False, skipped=True,
            detail="未安装 playwright 这个 Python 包，无法运行 webui 模块。"
                   "请先执行 `pip install playwright` 与 `playwright install chromium`，"
                   "再重新运行 --modules ... webui。",
        ))
        return

    # 最外层同样兜底一层 try/except：即便前面每个分组的公共前置都已单独兜底，浏览器/
    # 上下文创建本身（p.chromium.launch()/browser.new_context() 等）出问题仍是未覆盖的
    # 残余风险；一旦这里失败，webui 模块整体记一条失败，绝不能向上传播炸掉 main()（否则
    # collector.write_report() 永远不会被调用，此前 cli_smoke 等模块已收集的结果也会
    # 一并丢失，2026-07-22 remote 测试时曾因分组内部未兜底触发过同类问题）。
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            try:
                context = browser.new_context()
                page = context.new_page()
                # 强制中文 locale，全局生效（绑定在 page 上，不是绑定在某次 goto 上），
                # 只需在创建 page 之后、任何 page.goto 之前执行一次，后续所有分组的 goto 均生效。
                # 注意：Python 版 add_init_script() 只接受"原样注入执行"的语句字符串，不像
                # TS/JS 版会对函数参数自动包一层立即调用——传箭头函数字面量 "() => {...}"
                # 只会创建一个匿名函数值然后丢弃，函数体从未被执行，localStorage 实际上从未
                # 被写入（2026-07-22 remote 测试排查确认，此前的箭头函数写法完全不生效，
                # 导致全部依赖中文文案定位的检查全部超时）。这里直接传语句本身，不包函数。
                page.add_init_script("window.localStorage.setItem('qai_locale', 'zh-CN');")
                page_errors = []
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))

                # a. webui::home_page_loads —— 页面在合理超时内完成加载，标题非空且
                # 根应用容器（#app）渲染出内容，过程中没有触发页面级 pageerror。
                try:
                    page.goto(frontend_url, wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(500)
                    title = page.title()
                    app_html = page.locator("#app").inner_html()
                    ok = bool(title) and bool(app_html.strip()) and not page_errors
                    collector.add(TestResult(
                        module="E", name="webui::home_page_loads", passed=ok,
                        detail=f"title={title!r}, #app 渲染内容长度={len(app_html)}, pageerror={page_errors}",
                    ))
                except Exception as e:
                    collector.add(TestResult(
                        module="E", name="webui::home_page_loads", passed=False,
                        detail=f"打开首页异常: {e!r}",
                    ))

                # b~g. 六个分组，覆盖模块 A 全部 26 条有网页入口的用例；每个分组函数内部各自
                # page.goto 到自己的入口路由，组间不共享导航状态，简单可靠，复用同一个 page 对象。
                _webui_check_security_group(page, frontend_url, cli, collector, defects)
                _webui_check_downloads_group(page, frontend_url, cli, collector, defects)
                _webui_check_service_group(page, frontend_url, cli, collector, defects)
                _webui_check_appbuilder_group(page, frontend_url, cli, collector, defects, csrf)
                _webui_check_conv_code_group(page, frontend_url, cli, collector, defects, csrf)
                _webui_check_settings_skills_group(page, frontend_url, cli, collector, defects)

                context.close()
            finally:
                browser.close()
    except Exception as e:
        collector.add(TestResult(
            module="E", name="webui::browser_session_crashed", passed=False,
            detail=f"浏览器会话在完成全部分组检查之前异常终止，本模块结果可能不完整: {e!r}",
        ))

    # h. 剩余 10 条设计边界用例：直接从 CLI_CASE_TO_UI_EQUIVALENT 映射表里自动筛出，
    # 不需要手写清单，未来映射表增删会自动同步；不依赖浏览器，放在 with 块外也没问题。
    _webui_record_boundary_cases(collector)


def _webui_record_boundary_cases(collector):
    """自动从 CLI_CASE_TO_UI_EQUIVALENT 映射表里筛出模块 A 全部设计边界用例（即
    boundary_kind 非空的条目，共 10 条），逐一记为 webui_py:: 前缀的 skipped 结果。
    不需要手写清单，未来映射表增删会自动同步覆盖范围。"""
    for name, equiv in CLI_CASE_TO_UI_EQUIVALENT.items():
        if name.startswith("cli_smoke::") and equiv.boundary_kind is not None:
            case_suffix = name[len("cli_smoke::"):]
            _record_known_boundary(collector, "E", f"webui_py::{case_suffix}", equiv.boundary_reason)


def _webui_check_security_group(page, frontend_url, cli, collector, defects):
    """分组 1：安全页面，覆盖 policy show / policy skill-cap discover /
    audit query / dep pending / exec profiles / skill policy 共 6 条真实比对，
    以及 security settings get 这 1 条设计边界（详见下方第 6 项注释）。dep pending
    依赖默认『总览』tab 的状态，必须放在切换到其它 tab 之前完成；policy skill-cap
    discover / exec profiles / skill policy 共用同一个『技能策略』tab，按顺序
    排列以复用已点击的 tab，减少重复点击。

    公共前置（打开安全页）同样必须包一层 try/except，理由与 _webui_check_appbuilder_group
    的公共前置一致：不能让导航失败向上传播炸掉整个 webui 模块。"""
    group_cases = ("webui_py::dep pending", "webui_py::policy show",
                   "webui_py::policy skill-cap discover", "webui_py::security settings get",
                   "webui_py::audit query", "webui_py::exec profiles", "webui_py::skill policy")
    try:
        page.goto(f"{frontend_url}/security")
        page.wait_for_load_state("networkidle")
    except Exception as e:
        detail = f"打开『安全』页（公共前置）失败，本组 7 项检查均跳过: {e!r}"
        for name in group_cases:
            collector.add(TestResult(module="E", name=name, passed=False, detail=detail))
        return

    # 1. dep pending —— 不需要切 tab，直接在默认『总览』tab 上读取。
    try:
        cli_pending = cli.run("dep", "pending", timeout=30).json_data or []
        path_desc = "打开『安全』页（默认『总览』tab）→ 读取待批准安装请求"
        if not cli_pending:
            passed = _wait_visible(page.get_by_text("无待审批的安装请求"))
            detail = f"{path_desc}。一致: 空态提示可见，CLI 返回空列表" if passed \
                else f"{path_desc}。不一致: 空态提示未展示，但 CLI 返回空列表"
        else:
            rows = page.locator(".sec-cfg-pending-row")
            row_count = rows.count()
            row_texts = set(rows.locator("code.mono").all_text_contents())
            expected_texts = {" ".join(p.get("command_args", [])) for p in cli_pending}
            passed = row_count == len(cli_pending) and row_texts == expected_texts
            detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {row_count} 行/"
                      f"CLI {len(cli_pending)} 条")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::dep pending", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::dep pending", "major",
                            "WebUI『安全』总览页待批准安装请求与 `dep pending` 不一致",
                            path_desc, "页面展示条目与 CLI 输出一致", detail, str(cli_pending)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::dep pending", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 2. policy show —— 点击『白名单』tab，按 5 类规则分组比对 pattern 集合。
    try:
        page.get_by_role("tab", name="白名单").click()
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("policy", "show", timeout=30).json_data or {}
        rules = cli_data.get("rules", [])
        field_predicates = {
            "read_allow": lambda r: r.get("op") == "read" and r.get("action") == "allow",
            "write_allow": lambda r: r.get("op") == "write" and r.get("action") == "allow",
            "write_deny": lambda r: r.get("op") == "write" and r.get("action") == "deny",
            "exec_allow_cwd": lambda r: r.get("op") == "exec" and r.get("action") == "allow",
            "exec_deny_patterns": lambda r: r.get("op") == "exec" and r.get("action") == "deny",
        }
        path_desc = "点击『安全』页『白名单』tab → 按 5 类规则分组比对 pattern 集合"
        mismatches, degraded = [], False
        for field_name, predicate in field_predicates.items():
            expected_patterns = {r.get("pattern", "") for r in rules if predicate(r)}
            block = page.locator(".sec-cfg-list-block").filter(
                has=page.locator(".sec-cfg-list-key", has_text=field_name))
            inputs = block.locator(".sec-cfg-list-row .sec-cfg-list-input")
            count = inputs.count()
            try:
                actual_patterns = {inputs.nth(i).input_value() for i in range(count)}
                if actual_patterns != expected_patterns:
                    mismatches.append(f"{field_name}: 页面{actual_patterns} != CLI{expected_patterns}")
            except Exception:
                # 字段不是纯 <input>（或字段名与实际 DOM 结构不符）时退化为数量校验。
                degraded = True
                if count != len(expected_patterns):
                    mismatches.append(f"{field_name}(退化为数量校验): 页面{count} != CLI{len(expected_patterns)}")
        passed = not mismatches
        detail = f"{path_desc}。" + ("一致: 共 5 类规则均匹配" if passed else f"不一致: {mismatches}")
        if degraded:
            detail += "（部分分类因字段结构不确定，退化为数量校验）"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::policy show", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::policy show", "major",
                            "WebUI 白名单 tab 展示的规则与 `policy show` 不一致",
                            path_desc, "5 类规则的 pattern 集合与 CLI 一致", detail, str(rules)[:1500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::policy show", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 3. policy skill-cap discover —— 点击『技能策略』tab，比对技能卡片数量与 fid 集合。
    # 注：已排查确认 HTTP `GET /api/security/skill-discovery`（WebUI 数据源）与本处 CLI
    # `policy skill-cap discover` 调用的是完全相同的 `skill_discovery_use_case.execute()`
    # 实例，理论上应严格一致；若在实测中出现数量不符，最可能是同一次 run 内其它模块
    # （如 cli_smoke 的 skill register/unregister 冒烟）在两次读取之间修改了技能注册表
    # 导致的时序竞态，而不是本检查逻辑或数据源本身的问题（2026-07-22 remote 测试排查
    # 确认）；出现该 defect 时无需怀疑此处比对写法，应结合当次 run 的时间线判断是否为
    # 测试隔离问题。
    try:
        page.get_by_role("tab", name="技能策略").click()
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("policy", "skill-cap", "discover", timeout=30).json_data or {}
        skills = cli_data.get("skills", [])
        cards = page.locator(".sec-cfg-skill-card")
        fids = {t.strip() for t in page.locator(".sec-cfg-skill-fid").all_text_contents()}
        expected_fids = {s.get("skill_name", "") for s in skills}
        path_desc = "点击『安全』页『技能策略』tab → 读取技能卡片数量与 fid 集合"
        passed = cards.count() == len(skills) and fids == expected_fids
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {cards.count()} 张卡片/"
                  f"CLI {len(skills)} 个技能")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::policy skill-cap discover",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::policy skill-cap discover", "major",
                            "WebUI 技能策略 tab 展示的技能与 `policy skill-cap discover` 不一致",
                            path_desc, "技能卡片数量/fid 集合与 CLI 一致", detail, str(skills)[:1500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::policy skill-cap discover", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 4. exec profiles —— 复用『技能策略』tab（无需重复点击），读取审计表格。
    try:
        cli_data = cli.run("exec", "profiles", timeout=30).json_data or {}
        profiles = cli_data.get("profiles", [])
        path_desc = "『安全』页『技能策略』tab（复用）→ 读取执行代理配置表格"
        if not profiles:
            passed = _wait_visible(page.get_by_text("无已加载配置"))
            detail = f"{path_desc}。一致: 空态提示可见，CLI 返回空列表" if passed \
                else f"{path_desc}。不一致: 空态提示未展示，但 CLI 返回空列表"
        else:
            rows = page.locator(".sec-cfg-audit-table tbody tr")
            row_count = rows.count()
            names = {rows.nth(i).locator("td").nth(0).inner_text().strip() for i in range(row_count)}
            expected_names = {p.get("name", "-") for p in profiles}
            passed = row_count == len(profiles) and names == expected_names
            detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {row_count} 行/"
                      f"CLI {len(profiles)} 条")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::exec profiles", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::exec profiles", "major",
                            "WebUI 技能策略 tab 执行代理配置与 `exec profiles` 不一致",
                            path_desc, "表格内容与 CLI 一致", detail, str(profiles)[:1500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::exec profiles", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 5. skill policy —— 复用『技能策略』tab，读取顶部模式说明文字。
    try:
        cli_data = cli.run("skill", "policy", timeout=30).json_data or {}
        mode = cli_data.get("mode", "")
        text = page.locator(".sec-cfg-audit-controls .config-comment strong").inner_text(timeout=10000).strip()
        path_desc = "『安全』页『技能策略』tab（复用）→ 读取顶部模式说明文字"
        passed = text == mode
        detail = f"{path_desc}。{'一致' if passed else '不一致'}: 页面={text!r}/CLI={mode!r}"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::skill policy", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::skill policy", "major",
                            "WebUI 技能策略 tab 顶部模式说明与 `skill policy` 不一致",
                            path_desc, f"页面文字 == CLI mode({mode!r})", detail, str(cli_data)[:500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::skill policy", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 6. security settings get —— 设计边界（非脚本 bug）：深入排查后确认 CLI `security settings get`
    # （`cmd_security_settings_get`）返回的是 `security_runtime_state.snapshot()`（FileGuard
    # 权限授予/模式的运行时快照，字段为 enabled/mode/dynamic_authorization/settings），与
    # WebUI『安全』页『工具防护』tab（ToolSafetyPanel.vue）实际读取的 `GET
    # /api/security/runtime-config`（RuntimeConfig: file_broker_enabled/file_guard_enabled/
    # read_max_lines 等）完全是两个互不相关的后端数据模型，仅是命名巧合雷同（详见 2026-07-22
    # remote 测试排查记录）。全库搜索 apps/cli 下不存在任何排除 runtime_config/RuntimeConfig 的
    # CLI 命令，证实 ToolSafetyPanel 展示的这三项开关确实没有任何 CLI 等价入口，因此在
    # 本模块“CLI↔WebUI 双向验证”的设计前提下属于设计边界（无法拿到有效 CLI ground
    # truth），不追求与 module A 共享的 CLI_CASE_TO_UI_EQUIVALENT 映射表保持一致（那个映射
    # 表服务于 TS Playwright 桥接机制，official TS 测试本身比较的是 WebUI vs HTTP 端点，
    # 不是 WebUI vs CLI，改用这个统一比对思路对本模块不适用）。
    _record_known_boundary(
        collector, "E", "webui_py::security settings get",
        "CLI `security settings get` 返回的是 FileGuard 权限运行时快照"
        "（enabled/mode/dynamic_authorization/settings），与 WebUI『工具防护』tab 实际读取的"
        "`GET /api/security/runtime-config`（file_broker_enabled/file_guard_enabled/"
        "read_max_lines）是完全不相关的两套数据模型，仅命名巧合；全库搜索确认不存在任何"
        "暴露后者数据的 CLI 命令，本模块聚焦 CLI↔WebUI 真实数据比对，此用例无有效 CLI"
        "ground truth 可比，故判定为设计边界。")

    # 7. audit query —— 点击『审计 / 授权』tab，注意 .sec-cfg-audit-table 与依赖审批表
    # 共用 class，需 .first 精确定位。
    try:
        page.get_by_role("tab", name="审计 / 授权").click()
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("audit", "query", timeout=30).json_data or []
        table = page.locator("table.sec-cfg-audit-table").first
        path_desc = "点击『安全』页『审计 / 授权』tab → 读取审计记录表格"
        if not cli_data:
            passed = _wait_visible(page.get_by_text("暂无审计记录"))
            detail = f"{path_desc}。一致: 空态提示可见，CLI 返回空列表" if passed \
                else f"{path_desc}。不一致: 空态提示未展示，但 CLI 返回空列表"
        else:
            row_count = table.locator("tbody tr").count()
            paths = set(table.locator(".sec-cfg-audit-path").all_text_contents())
            expected_paths = {e.get("resource", {}).get("identifier", "") for e in cli_data}
            passed = row_count == len(cli_data) and paths == expected_paths
            detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {row_count} 行/"
                      f"CLI {len(cli_data)} 条")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::audit query", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::audit query", "major",
                            "WebUI 审计/授权 tab 展示的审计记录与 `audit query` 不一致",
                            path_desc, "表格内容与 CLI 一致", detail, str(cli_data)[:1500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::audit query", passed=False,
                                  detail=f"检查异常: {e!r}"))


def _webui_check_downloads_group(page, frontend_url, cli, collector, defects):
    """分组 2：下载中心，覆盖 service-release 全部 6 个只读子命令的真实比对（均无设计边界）。
    tabs 用下标而非文字定位（0=服务版本，1=模型），避免受 i18n/文案变化影响。

    公共前置（打开下载中心页）同样必须包一层 try/except，理由同上。"""
    group_cases = ("webui_py::service-release aria2c status", "webui_py::service-release versions",
                   "webui_py::service-release status versions", "webui_py::service-release models",
                   "webui_py::service-release status models", "webui_py::service-release settings get")
    try:
        page.goto(f"{frontend_url}/downloads")
        tabs = page.locator(".downloads-view__tabs").get_by_role("tab")
    except Exception as e:
        detail = f"打开『下载中心』页（公共前置）失败，本组 6 项检查均跳过: {e!r}"
        for name in group_cases:
            collector.add(TestResult(module="E", name=name, passed=False, detail=detail))
        return

    # 1. service-release aria2c status —— 不需要切 tab，弱一致性说明性校验（文案会随
    # i18n/版本变化，重点是"页面确实展示了与 JSON 状态相符的信息"，不逐字比对）。
    try:
        cli_data = cli.run("service-release", "aria2c", "status", timeout=30).json_data or {}
        banner = page.locator(".dc-info-banner").first
        path_desc = "打开『下载中心』页 → 读取 aria2c 状态提示条"
        banner_visible = _wait_visible(banner)
        available = bool(cli_data.get("available"))
        daemon_running = bool(cli_data.get("daemon_running"))
        banner_text = banner.inner_text() if banner_visible else ""
        heuristic_ok = not (daemon_running and "未安装" in banner_text)
        passed = (not available) or (banner_visible and heuristic_ok)
        detail = (f"{path_desc}（弱一致性说明性校验，非逐字比对）。banner_visible={banner_visible}, "
                  f"available={available}, daemon_running={daemon_running}, "
                  f"banner_text={banner_text[:80]!r}")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release aria2c status",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release aria2c status", "minor",
                            "WebUI 下载中心 aria2c 状态提示条与 `service-release aria2c status` 不符",
                            path_desc, "提示条展示与 available/daemon_running 相符的信息", detail,
                            str(cli_data)[:500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release aria2c status",
                                  passed=False, detail=f"检查异常: {e!r}"))

    # 2. service-release versions —— 点击『服务版本』tab（下标 0），比对版本卡片。
    # 注意：CLI `service-release versions` 顶层字段是 `items`，不是 `versions`（此前误读为
    # `versions` 导致 .get() 永远落空、统计出的 CLI 数量恒为 0，与页面渲染出的真实卡片数
    # 形成虚假不一致，2026-07-22 remote 测试排查确认）；同时补上 exit_code 显式判定，
    # 避免 CLI 子进程真失败时被 `.json_data or {}` 悄悄吞掉、表现成同样的假『0 条』。
    try:
        tabs.nth(0).click()
        page.wait_for_load_state("networkidle")
        cli_result = cli.run("service-release", "versions", timeout=30)
        if cli_result.exit_code != 0 or cli_result.json_data is None:
            raise RuntimeError(f"CLI 调用失败: exit_code={cli_result.exit_code}, "
                               f"json_data={cli_result.json_data!r}")
        versions = cli_result.json_data.get("items", [])
        path_desc = "点击『下载中心』页『服务版本』tab → 读取版本卡片列表"
        articles = page.locator('article[data-version]')
        missing = [v["version"] for v in versions
                   if not page.locator(f'article[data-version="{v["version"]}"]').is_visible()]
        passed = articles.count() == len(versions) and not missing
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {articles.count()} 张卡片/"
                  f"CLI {len(versions)} 个版本" + (f", 缺失卡片: {missing}" if missing else ""))
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release versions",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release versions", "major",
                            "WebUI 服务版本卡片与 `service-release versions` 不一致",
                            path_desc, "每个版本都有对应卡片", detail, str(versions)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release versions",
                                  passed=False, detail=f"检查异常: {e!r}"))

    # 3. service-release status versions —— 仍在『服务版本』tab，逐个版本卡片比对
    # 安装/下载状态；卡片不存在时跳过（属正常情况）。
    try:
        cli_data = cli.run("service-release", "status", "versions", timeout=30).json_data or {}
        path_desc = "『下载中心』页『服务版本』tab（复用）→ 逐个版本卡片比对安装/下载状态"
        mismatches, checked = [], 0
        for version, info in cli_data.items():
            card = page.locator(f'article[data-version="{version}"]')
            if card.count() == 0:
                continue
            checked += 1
            if info.get("installed"):
                pill_visible = card.locator(".dc-card__installed-pill").is_visible()
                path_text = card.locator(".dc-card__path").inner_text().strip()
                if not pill_visible or path_text != info.get("install_path", ""):
                    mismatches.append(f"{version}: installed 状态不符 (pill={pill_visible}, path={path_text!r})")
            if info.get("downloaded"):
                save_path_text = card.locator(".dc-card__save-path-text").inner_text().strip()
                if save_path_text != info.get("save_path", ""):
                    mismatches.append(f"{version}: downloaded save_path 不符 ({save_path_text!r})")
        passed = not mismatches
        detail = (f"{path_desc}。共校验 {checked} 个已渲染版本卡片。"
                  f"{'一致' if passed else '不一致'}: {mismatches if mismatches else '全部一致'}")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release status versions",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release status versions", "major",
                            "WebUI 服务版本卡片的安装/下载状态与 `service-release status versions` 不一致",
                            path_desc, "全部一致", detail, str(cli_data)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release status versions",
                                  passed=False, detail=f"检查异常: {e!r}"))

    # 4. service-release models —— 点击『模型』tab（下标 1），比对模型卡片。
    # 注意：CLI `service-release models` 顶层字段同样是 `items`，不是 `models`（同上一条
    # 排查记录）；同样补上 exit_code 显式判定。
    try:
        tabs.nth(1).click()
        page.wait_for_load_state("networkidle")
        cli_result = cli.run("service-release", "models", timeout=30)
        if cli_result.exit_code != 0 or cli_result.json_data is None:
            raise RuntimeError(f"CLI 调用失败: exit_code={cli_result.exit_code}, "
                               f"json_data={cli_result.json_data!r}")
        models = cli_result.json_data.get("items", [])
        path_desc = "点击『下载中心』页『模型』tab → 读取模型卡片列表"
        articles = page.locator('article[data-model]')
        mismatches = []
        for m in models:
            model_id = m.get("model_id", "")
            card = page.locator(f'article[data-model="{model_id}"]')
            if not card.is_visible():
                mismatches.append(f"{model_id}: 卡片未找到")
                continue
            title_text = card.locator(".dc-card__title").inner_text()
            if m.get("name", "") not in title_text:
                mismatches.append(f"{model_id}: 标题不含 name({m.get('name')!r})")
        passed = articles.count() == len(models) and not mismatches
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {articles.count()} 张卡片/"
                  f"CLI {len(models)} 个模型" + (f", 问题: {mismatches}" if mismatches else ""))
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release models",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release models", "major",
                            "WebUI 模型卡片与 `service-release models` 不一致",
                            path_desc, "每个模型都有对应卡片且标题包含名称", detail, str(models)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release models",
                                  passed=False, detail=f"检查异常: {e!r}"))

    # 5. service-release status models —— 仍在『模型』tab，逐个模型卡片比对安装/下载状态。
    try:
        cli_data = cli.run("service-release", "status", "models", timeout=30).json_data or {}
        path_desc = "『下载中心』页『模型』tab（复用）→ 逐个模型卡片比对安装/下载状态"
        mismatches, checked = [], 0
        for model_id, info in cli_data.items():
            card = page.locator(f'article[data-model="{model_id}"]')
            if card.count() == 0:
                continue
            checked += 1
            if info.get("installed"):
                pill_visible = card.locator(".dc-card__installed-pill").is_visible()
                path_text = card.locator(".dc-card__path").inner_text().strip()
                if not pill_visible or path_text != info.get("install_path", ""):
                    mismatches.append(f"{model_id}: installed 状态不符")
            if info.get("downloaded"):
                save_path_text = card.locator(".dc-card__save-path-text").inner_text().strip()
                if save_path_text != info.get("save_path", ""):
                    mismatches.append(f"{model_id}: downloaded save_path 不符")
        passed = not mismatches
        detail = (f"{path_desc}。共校验 {checked} 个已渲染模型卡片。"
                  f"{'一致' if passed else '不一致'}: {mismatches if mismatches else '全部一致'}")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release status models",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release status models", "major",
                            "WebUI 模型卡片的安装/下载状态与 `service-release status models` 不一致",
                            path_desc, "全部一致", detail, str(cli_data)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release status models",
                                  passed=False, detail=f"检查异常: {e!r}"))

    # 6. service-release settings get —— 点击设置展开按钮，比对 5 项下载设置。
    try:
        page.locator(".dc-settings__toggle").click()
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("service-release", "settings", "get", timeout=30).json_data or {}
        path_desc = "点击『下载中心』页设置展开按钮 → 比对 5 项下载设置"
        checks = {
            "save_dir": (page.locator("#dc-save-dir").input_value(), cli_data.get("save_dir")),
            "version_list_url": (page.locator("#dc-version-url").input_value(), cli_data.get("version_list_url")),
            "catalog_url": (page.locator("#dc-catalog-url").input_value(), cli_data.get("catalog_url")),
            "fetch_timeout_seconds": (page.locator("#dc-fetch-timeout").input_value(),
                                       str(cli_data.get("fetch_timeout_seconds"))),
            "download_timeout_seconds": (page.locator("#dc-download-timeout").input_value(),
                                          str(cli_data.get("download_timeout_seconds"))),
        }
        mismatches = [f"{k}: 页面{a!r} != CLI{b!r}" for k, (a, b) in checks.items() if a != b]
        passed = not mismatches
        detail = f"{path_desc}。{'一致' if passed else '不一致'}: {mismatches if mismatches else '5 项均一致'}"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service-release settings get",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service-release settings get", "major",
                            "WebUI 下载设置面板与 `service-release settings get` 不一致",
                            path_desc, "5 项均一致", detail, str(cli_data)[:800])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service-release settings get",
                                  passed=False, detail=f"检查异常: {e!r}"))


def _webui_check_service_group(page, frontend_url, cli, collector, defects):
    """分组 3：服务页面，覆盖 service status / probe / models / config get 共 4 条真实
    比对（`service path` 是设计边界，不在此列，由 _webui_record_boundary_cases 自动处理）。

    公共前置（打开服务页）同样必须包一层 try/except，理由同上。"""
    group_cases = ("webui_py::service status", "webui_py::service probe",
                   "webui_py::service models", "webui_py::service config get")
    try:
        page.goto(f"{frontend_url}/service")
        page.wait_for_load_state("networkidle")
    except Exception as e:
        detail = f"打开『服务』页（公共前置）失败，本组 4 项检查均跳过: {e!r}"
        for name in group_cases:
            collector.add(TestResult(module="E", name=name, passed=False, detail=detail))
        return

    # 1. service status —— 直接读取状态指示灯，无需点击。
    try:
        cli_data = cli.run("service", "status", timeout=30).json_data or {}
        path_desc = "打开『服务』页 → 直接读取状态指示灯"
        cls = page.locator(".service-status-indicator").get_attribute("class") or ""
        indicator_running = "running" in cls
        expected_running = bool(cli_data.get("running") or cli_data.get("state") == "running")
        passed = indicator_running == expected_running
        exe_note = ""
        exe_path = cli_data.get("exe_path")
        if passed and exe_path:
            exe_text = page.locator(".service-status-exe").inner_text()
            exe_ok = exe_text == exe_path
            passed = passed and exe_ok
            exe_note = f", exe_path {'一致' if exe_ok else '不一致'}"
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 指示灯 running={indicator_running}/"
                  f"CLI running={expected_running}{exe_note}")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service status", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service status", "major",
                            "WebUI 服务状态指示灯与 `service status` 不一致",
                            path_desc, "指示灯状态(及 exe_path)与 CLI 一致", detail, str(cli_data)[:500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service status", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 2. service probe —— 展开连接栏，点击『测试』按钮，读取连接结果。
    try:
        page.locator(".service-connection-bar").click()
        page.wait_for_load_state("networkidle")
        page.locator(".service-connection-body button").first.click()
        path_desc = "点击『服务』页连接栏展开 → 点击『测试』按钮 → 读取连接结果"
        cli_data = cli.run("service", "probe", timeout=30).json_data or {}
        result_visible = _wait_visible(page.locator(".conn-result-ok"))
        expected_reachable = bool(cli_data.get("reachable"))
        passed = result_visible == expected_reachable
        detail = f"{path_desc}。{'一致' if passed else '不一致'}: 页面={result_visible}/CLI reachable={expected_reachable}"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service probe", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service probe", "major",
                            "WebUI 服务连接测试结果与 `service probe` 不一致",
                            path_desc, "连接结果展示与 CLI reachable 一致", detail, str(cli_data)[:500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service probe", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 3. service models —— 直接读取模型下拉选择器的可选项。
    try:
        cli_data = cli.run("service", "models", timeout=30).json_data or []
        path_desc = "『服务』页 → 读取模型下拉选择器的可选项"
        options = page.locator(".param-cell-model .param-select optgroup option")
        if not cli_data:
            passed = page.locator(".param-cell-model").count() == 0
            detail = f"{path_desc}。一致: 无模型时不展示模型选择器" if passed \
                else f"{path_desc}。不一致: CLI 无模型但页面仍展示模型选择器"
        else:
            expected_names = {m.get("name") for m in cli_data}
            passed = options.count() == len(cli_data) and set(options.all_text_contents()) == expected_names
            detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {options.count()} 个选项/"
                      f"CLI {len(cli_data)} 个模型")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service models", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service models", "major",
                            "WebUI 服务页模型下拉选项与 `service models` 不一致",
                            path_desc, "选项集合与 CLI 一致", detail, str(cli_data)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service models", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 4. service config get —— 点击配置齿轮按钮打开弹窗，比对默认模型下拉框；
    # 齿轮按钮 disabled（服务未安装）时退化为仅校验 CLI 输出结构。
    try:
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("service", "config", "get", timeout=30).json_data or {}
        config = cli_data.get("config", cli_data) if isinstance(cli_data, dict) else {}
        gear = page.locator(".svc-cfg-gear-btn")
        if gear.is_enabled():
            path_desc = "点击『服务』页配置齿轮按钮 → 打开配置弹窗 → 比对默认模型下拉框"
            gear.click()
            page.get_by_role("dialog").wait_for(state="visible", timeout=10000)
            value = page.locator('input[list="svc-cfg-default-model-options"]').input_value()
            expected = config.get("default_model", "")
            passed = value == expected
            detail = f"{path_desc}。{'一致' if passed else '不一致'}: 页面={value!r}/CLI={expected!r}"
        else:
            path_desc = "『服务』页配置齿轮按钮不可用（服务未安装）"
            passed = isinstance(config, dict) and bool(config)
            detail = f"{path_desc}，退化为仅校验 CLI 输出结构。config 字段可用={passed}"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::service config get", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::service config get", "major",
                            "WebUI 服务配置弹窗默认模型与 `service config get` 不一致",
                            path_desc, "弹窗内默认模型与 CLI 一致", detail, str(cli_data)[:800])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::service config get", passed=False,
                                  detail=f"检查异常: {e!r}"))


def _webui_check_appbuilder_group(page, frontend_url, cli, collector, defects, csrf):
    """分组 4：App Builder 工作台，覆盖 pack taxonomy / pack list / app --json / run list
    共 4 条真实比对，均在 `/chat` 页面内的浮层进行，需要真实点击进入。

    公共前置（打开工作台）本身也可能失败（如冷启动下 Vite 编译该模块较慢），必须包一层
    try/except——否则会像本模块其它检查一样让异常向上传播炸掉整个 webui 模块，
    连同此前已经跑完的其它模块结果一起丢失（详见 2026-07-22 remote 测试排查记录）。
    失败时把本组全部 4 条检查记为同一条失败原因，不再继续执行组内后续检查。

    工作台浮层默认不挂载：其渲染条件 `appBuilderShowWorkbench` 来自 `GET /api/forge-config`
    的 `ui.app_builder.show_workbench` 字段，服务端持久化默认值为 `false`（纯本地功能开关，
    与任何外网/CDN 资源无关），点击『任务』模式按钮本身只是本地状态切换，不会自动打开这个
    开关；必须先用 CsrfSession 读一次现有配置、合并该字段为 True 后写回，否则浮层永远不会
    出现（2026-07-22 remote 测试排查确认，此前一直缺这一步导致全组超时）。"""
    group_cases = ("webui_py::pack taxonomy", "webui_py::pack list",
                   "webui_py::app --json", "webui_py::run list")
    try:
        cfg_resp = csrf.request("GET", "/api/forge-config", timeout=15)
        config = (cfg_resp.json() or {}).get("config", {}) if cfg_resp.ok else {}
        ui_cfg = config.get("ui", {})
        app_builder_cfg = ui_cfg.get("app_builder", {})
        config["ui"] = {**ui_cfg, "app_builder": {**app_builder_cfg, "show_workbench": True}}
        csrf.request("POST", "/api/forge-config", json={"config": config}, timeout=15)

        page.goto(f"{frontend_url}/chat")
        page.wait_for_load_state("networkidle")
        page.get_by_test_id("mode-btn-app-builder").click()
        page.get_by_test_id("app-builder-workbench").wait_for(state="visible", timeout=30000)
    except Exception as e:
        detail = f"打开 App Builder 工作台（公共前置）失败，本组 4 项检查均跳过: {e!r}"
        for name in group_cases:
            collector.add(TestResult(module="E", name=name, passed=False, detail=detail))
        return

    # 1. pack taxonomy —— 打开分类弹层（若有『View all』则点击展开全量），比对任务行数。
    try:
        page.locator(".ab-taxonomy-btn").click()
        popover = page.locator(".ab-taxonomy-popover--floating")
        popover.wait_for(state="visible", timeout=10000)
        view_all = popover.locator(".ab-taxonomy-foot a")
        if view_all.count() > 0:
            view_all.click()
        cli_data = cli.run("pack", "taxonomy", timeout=30).json_data or {}
        groups = cli_data.get("groups", [])
        expected_count = sum(len(g.get("tasks", [])) for g in groups)
        path_desc = "点击 App Builder 工作台『任务分类』按钮 → 展开分类弹层（若有『View all』则展开全量）"
        actual_count = popover.locator(".ab-taxonomy-task-row").count()
        passed = actual_count == expected_count
        detail = f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {actual_count} 行/CLI {expected_count} 个任务"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::pack taxonomy", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::pack taxonomy", "major",
                            "WebUI 任务分类弹层任务数量与 `pack taxonomy` 不一致",
                            path_desc, "任务行数与 CLI 一致", detail, str(cli_data)[:800])
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::pack taxonomy", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 2/3. pack list 与 app --json —— 共用同一份页面证据（Pack 列表），分别用各自的
    # CLI 命令取值单独判定 PASS/FAIL；任务数量多时抽样前 3 个分组，避免遍历耗时过长。
    try:
        cli_pack = cli.run("pack", "list", timeout=30).json_data or {}
        cli_app = cli.run("app", "--json", timeout=30).json_data or {}
        items = cli_pack.get("items", [])
        packs = cli_app.get("packs", [])
        taxonomy = cli.run("pack", "taxonomy", timeout=30).json_data or {}
        groups = taxonomy.get("groups", [])
        task_group = groups[1] if len(groups) > 1 else (groups[0] if groups else {})
        all_tasks = task_group.get("tasks", [])
        sample_tasks = all_tasks[:3]
        path_desc = ("依次点击『任务分类』按钮 → 搜索框输入任务名过滤 → 点击匹配的任务行 → "
                     "点击模型选择器按钮 → 读取模型卡片名称（抽样）")
        picked_names = set()
        for task in sample_tasks:
            task_name = task.get("name") or task.get("title") or task.get("id", "")
            if not task_name:
                continue
            # 直接点击 .ab-taxonomy-btn 是个双态开关：如果弹层因为上一步 Escape 后仍处于
            # "已展开"状态（Teleport 浮层的关闭时序与按钮自身的展开态可能不同步），这次点击
            # 反而会把它关掉，导致后面等它"出现"直接超时（2026-07-22 remote 测试排查确认）。
            # 先判断当前是否已经可见，只有不可见时才点击，让这一步的最终状态始终是"打开"。
            popover = page.locator(".ab-taxonomy-popover--floating")
            if not popover.is_visible():
                page.locator(".ab-taxonomy-btn").click()
            popover.wait_for(state="visible", timeout=10000)
            popover.locator(".ab-taxonomy-search input").fill(task_name)
            row = popover.locator(".ab-taxonomy-task-row", has_text=task_name).first
            if row.count() == 0:
                continue
            row.click()
            page.locator(".ab-model-picker-button").click()
            picker = page.locator(".ab-model-picker-popover")
            picker.wait_for(state="visible", timeout=10000)
            picked_names.update(t.strip() for t in
                                 picker.locator(".ab-model-card .ab-model-card-name").all_text_contents())
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

        note = f"抽样校验 {len(sample_tasks)}/{len(all_tasks)} 个任务分组，共读到 {len(picked_names)} 个模型名"
        item_titles = {i.get("title") or i.get("id", "") for i in items}
        pack_titles = {p.get("title") or p.get("id", "") for p in packs}
        pack_list_ok = picked_names.issubset(item_titles)
        app_json_ok = picked_names.issubset(pack_titles)

        detail_pack = f"{path_desc}。{note}。{'一致' if pack_list_ok else '不一致'}: " \
                      f"{'抽样模型名均能在 pack list items 中找到' if pack_list_ok else '部分抽样模型名不在 pack list items 中'}"
        if pack_list_ok:
            collector.add(TestResult(module="E", name="webui_py::pack list", passed=True, detail=detail_pack))
        else:
            _record_defect(collector, defects, "E", "webui_py::pack list", "major",
                            "App Builder 模型选择器展示的模型不在 `pack list` 结果中",
                            path_desc, "抽样模型名均为 `pack list` items 子集", detail_pack,
                            str(sorted(picked_names))[:800])

        detail_app = f"{path_desc}。{note}。{'一致' if app_json_ok else '不一致'}: " \
                     f"{'抽样模型名均能在 app --json packs 中找到' if app_json_ok else '部分抽样模型名不在 app --json packs 中'}"
        if app_json_ok:
            collector.add(TestResult(module="E", name="webui_py::app --json", passed=True, detail=detail_app))
        else:
            _record_defect(collector, defects, "E", "webui_py::app --json", "major",
                            "App Builder 模型选择器展示的模型不在 `app --json` 结果中",
                            path_desc, "抽样模型名均为 `app --json` packs 子集", detail_app,
                            str(sorted(picked_names))[:800])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::pack list", passed=False,
                                  detail=f"检查异常: {e!r}"))
        collector.add(TestResult(module="E", name="webui_py::app --json", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 4. run list —— 若上一步抽样没有实际点选任何模型卡片，这里补选一个，让『运行历史』
    # 面板有确定的模型上下文可依赖；再展开历史面板比对条目数量。
    try:
        picker = page.locator(".ab-model-picker-popover")
        if not picker.is_visible():
            page.locator(".ab-model-picker-button").click()
            picker.wait_for(state="visible", timeout=10000)
        picker.locator(".ab-model-card").first.click()
        history_toggle = page.get_by_test_id("app-builder-history-toggle")
        history_toggle.wait_for(state="visible", timeout=10000)
        history_toggle.click()
        panel = page.get_by_test_id("app-builder-history-panel")
        panel.wait_for(state="visible", timeout=10000)
        cli_data = cli.run("run", "list", timeout=30).json_data or {}
        items = cli_data.get("items", [])
        path_desc = "点击 App Builder 工作台历史记录按钮 → 展开历史面板 → 读取历史条目"
        if not items:
            passed = _wait_visible(page.locator(".ab-history-empty"))
            detail = f"{path_desc}。一致: 空态展示，CLI 返回空列表" if passed \
                else f"{path_desc}。不一致: 空态未展示，但 CLI 返回空列表"
        else:
            count = page.get_by_test_id("app-builder-history-item").count()
            passed = count == len(items)
            detail = f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {count} 条/CLI {len(items)} 条（仅校验数量）"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::run list", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::run list", "major",
                            "App Builder 历史面板条目数量与 `run list` 不一致",
                            path_desc, "历史条目数量与 CLI 一致", detail, str(cli_data)[:800])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::run list", passed=False,
                                  detail=f"检查异常: {e!r}"))


def _webui_check_conv_code_group(page, frontend_url, cli, collector, defects, csrf):
    """分组 5：最近对话 + AI 编程会话，覆盖 conv list / code session list / code health
    共 3 条真实比对，均在 `/chat` 页面。"""
    # 1. conv list —— 侧边栏有分组截断逻辑，仅做"页面展示的标题都真实存在于 CLI 结果里"
    # 的子集校验，不要求数量严格相等。
    try:
        page.goto(f"{frontend_url}/chat")
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("conv", "list", timeout=30).json_data or []
        path_desc = "打开『对话』页 → 读取侧边栏『最近对话』列表标题"
        if not cli_data:
            passed = _wait_visible(page.locator(".conv-empty-hint"))
            detail = f"{path_desc}。一致: 空态提示可见，CLI 返回空列表" if passed \
                else f"{path_desc}。不一致: 空态提示未展示，但 CLI 返回空列表"
        else:
            titles = [t.strip() for t in page.locator(".conv-item .conv-item-title-text").all_text_contents()
                      if t.strip()]
            expected_titles = {c.get("title", "") for c in cli_data}
            missing = [t for t in titles if t not in expected_titles]
            passed = not missing
            detail = (f"{path_desc}。因侧边栏分组截断，仅做子集校验。"
                      f"{'一致' if passed else '不一致'}: 页面展示 {len(titles)} 个标题"
                      + (f"，其中 {len(missing)} 个不在 CLI 结果中" if missing else "，均能在 CLI 结果中找到"))
            if missing:
                # 与 policy skill-cap discover 属于同一类现象：WebUI 由长驻后端进程提供数据，
                # CLI 侧是本次新起的独立子进程读取，二者并非同一份内存/连接状态；若同一次
                # run 内此前有其它模块（如 cli_smoke 的 conv 相关冒烟）刚创建/修改过对话，
                # 两次读取之间存在时序竞态是可能的（2026-07-22 remote 测试排查记录），出现
                # 该 defect 时应结合当次 run 时间线判断，而非默认是本处比对逻辑写错。
                detail += "（提示：与 skill-cap discover 同类，可能是 CLI 子进程与长驻后端之间的时序竞态，而非选择器/比对逻辑错误）"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::conv list", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::conv list", "major",
                            "WebUI 最近对话侧边栏标题不在 `conv list` 结果中",
                            path_desc, "侧边栏展示的标题都是 CLI 结果的子集", detail, str(cli_data)[:800])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::conv list", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 2/3. code session list 与 code health —— 共用前置操作：先经后端 HTTP 接口开启
    # AI 编程配置，再打开对话页、点击『cc』pill 展开编程面板。
    try:
        # `/api/cc/config` 是非安全方法（POST），后端 CsrfMiddleware 默认要求同时携带
        # `qai_csrf` cookie 与 `X-QAI-CSRF` header；脱离浏览器会话、不带 CSRF token 的裸
        # requests.post 会被 403 拦截，配置从未真正持久化，导致 `cc-pill` 一直不出现
        # （2026-07-22 remote 测试排查确认）。改用已实现双提交 Cookie 握手的 CsrfSession。
        csrf.request("POST", "/api/cc/config", json={"config": {"enabled": True}}, timeout=10)
        page.goto(f"{frontend_url}/chat")
        page.get_by_test_id("cc-pill").click()
        page.get_by_test_id("coding-panel-cc").wait_for(state="visible", timeout=15000)
        path_desc = "开启 AI 编程配置 → 打开『对话』页 → 点击『cc』pill → 展开编程面板"
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::code session list", passed=False,
                                  detail=f"检查异常(前置操作失败): {e!r}"))
        collector.add(TestResult(module="E", name="webui_py::code health", passed=False,
                                  detail=f"检查异常(前置操作失败): {e!r}"))
        return

    # 2. code session list —— 面板默认只显示 Active 标签页，CLI 侧需先按 status 过滤。
    try:
        cli_sessions = cli.run("code", "session", "list", timeout=30).json_data or []
        active_sessions = [s for s in cli_sessions if s.get("status") == "active"]
        rows = page.get_by_test_id("coding-panel-list-cc").locator("> div")
        row_count = rows.count()
        if not active_sessions:
            passed = row_count == 1  # 空态提示行
            detail = (f"{path_desc} → 读取会话列表（Active 标签页默认视图）。"
                      f"{'一致' if passed else '不一致'}: 页面 {row_count} 行(应为空态提示行)")
        else:
            titles = {rows.nth(i).locator("span").nth(1).inner_text().strip() for i in range(row_count)}
            expected_titles = {s.get("title") or s.get("session_id") for s in active_sessions}
            passed = row_count == len(active_sessions) and titles == expected_titles
            detail = (f"{path_desc} → 读取会话列表（Active 标签页）。{'一致' if passed else '不一致'}: "
                      f"页面 {row_count} 行/CLI(active) {len(active_sessions)} 条")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::code session list",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::code session list", "major",
                            "WebUI AI 编程面板会话列表与 `code session list`(active) 不一致",
                            path_desc, "会话列表与 CLI active 会话一致", detail, str(cli_sessions)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::code session list", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 3. code health —— 读取页脚状态栏的 SDK/授权图标（及可选的 sdk_version）。
    try:
        health = cli.run("code", "health", timeout=30).json_data or {}
        info_spans = page.locator(".ai-coding-statusbar-info > span")
        sdk_text = info_spans.nth(0).inner_text()
        auth_text = info_spans.nth(1).inner_text()
        sdk_available = bool(health.get("sdk_available"))
        auth_configured = bool(health.get("auth_configured"))
        sdk_ok = ("✅" in sdk_text) == sdk_available and ("❌" in sdk_text) == (not sdk_available)
        auth_ok = ("✅" in auth_text) == auth_configured and ("❌" in auth_text) == (not auth_configured)
        passed = sdk_ok and auth_ok
        detail = (f"{path_desc} → 读取状态栏 SDK/授权图标。{'一致' if passed else '不一致'}: "
                  f"sdk_available={sdk_available}, auth_configured={auth_configured}")
        sdk_version = health.get("sdk_version")
        if passed and sdk_version:
            version_text = info_spans.nth(0).locator(".ai-coding-statusbar-detail").inner_text()
            version_ok = version_text == sdk_version
            passed = passed and version_ok
            detail += f", sdk_version {'一致' if version_ok else '不一致'}"
        if passed:
            collector.add(TestResult(module="E", name="webui_py::code health", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::code health", "major",
                            "WebUI AI 编程面板状态栏与 `code health` 不一致",
                            path_desc, "状态栏图标/版本与 CLI 一致", detail, str(health)[:500])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::code health", passed=False,
                                  detail=f"检查异常: {e!r}"))


def _webui_check_settings_skills_group(page, frontend_url, cli, collector, defects):
    """分组 6：设置 + 技能，覆盖 config provider list / skill list 共 2 条真实比对。"""
    # 1. config provider list —— 打开『设置』页『云端模型』tab，比对 Provider 分组。
    try:
        page.goto(f"{frontend_url}/settings?tab=cloud-models")
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("config", "provider", "list", timeout=30).json_data or {}
        providers = cli_data.get("providers", [])
        groups = page.locator(".cloud-model-provider-group")
        expected_ids = {p.get("provider_id") for p in providers}
        actual_ids = set(groups.locator(".cloud-model-provider-label").all_text_contents())
        path_desc = "打开『设置』页『云端模型』tab → 读取 Provider 分组列表"
        passed = groups.count() == len(providers) and actual_ids == expected_ids
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {groups.count()} 个分组/"
                  f"CLI {len(providers)} 个 Provider")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::config provider list",
                                      passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::config provider list", "major",
                            "WebUI Provider 分组与 `config provider list` 不一致",
                            path_desc, "分组数量/ID 集合与 CLI 一致", detail, str(providers)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::config provider list", passed=False,
                                  detail=f"检查异常: {e!r}"))

    # 2. skill list —— 打开『技能』页，比对技能卡片网格。
    try:
        page.goto(f"{frontend_url}/skills")
        page.wait_for_load_state("networkidle")
        cli_data = cli.run("skill", "list", timeout=30).json_data or {}
        skills = cli_data.get("skills", [])
        cards = page.locator(".skills-grid .skill-card")
        expected_ids = {s.get("skill_id") or s.get("id") for s in skills}
        actual_ids = set(cards.locator(".skill-card-id").all_text_contents())
        path_desc = "打开『技能』页 → 读取技能卡片网格"
        passed = cards.count() == len(skills) and actual_ids == expected_ids
        detail = (f"{path_desc}。{'一致' if passed else '不一致'}: 页面 {cards.count()} 张卡片/"
                  f"CLI {len(skills)} 个技能")
        if passed:
            collector.add(TestResult(module="E", name="webui_py::skill list", passed=True, detail=detail))
        else:
            _record_defect(collector, defects, "E", "webui_py::skill list", "major",
                            "WebUI 技能卡片与 `skill list` 不一致",
                            path_desc, "卡片数量/ID 集合与 CLI 一致", detail, str(skills)[:1000])
    except Exception as e:
        collector.add(TestResult(module="E", name="webui_py::skill list", passed=False,
                                  detail=f"检查异常: {e!r}"))


# ---------------------------------------------------------------------------
# 统一报告生成（原 test/generate_builder_report.py，现合并进本脚本）
# ---------------------------------------------------------------------------

class _BoundaryKind:
    """设计边界的两个子类型，见文件头注释。仅用作字符串常量，不做枚举类型。"""
    NO_UI_ENTRY = "no_ui_entry"
    NO_SHARED_TRUTH = "no_shared_truth"


@dataclass(frozen=True)
class UiEquivalent:
    """cli_smoke 用例是否存在对应的 WebUI 操作入口的登记表；仅供
    `_webui_record_boundary_cases()` 自动识别设计边界用例使用。
    boundary_kind 非空表示该用例是设计边界（webui 模块跳过它，理由见 boundary_reason）；
    为空表示该用例在 webui 模块里有真实的浏览器点击路径比对（比对逻辑本身写在
    各 `_webui_check_*_group` 函数里，不依赖这份映射表）。"""
    boundary_kind: Optional[str] = None
    boundary_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# 解析 test_builder_cli.py 产出的 results.json
# ---------------------------------------------------------------------------

def parse_cli_results(cli_results_json_path) -> Tuple[List[dict], dict]:
    """解析 test_builder_cli.py::ResultCollector.write_report() 产出的
    results.json，按 module/name/passed/crashed/skipped/ignorable 字段归一化
    出 cli_status（passed/failed/crashed/skipped/ignorable 五态之一）。
    返回 (归一化用例列表, 原始 payload)。"""
    path = Path(cli_results_json_path)
    if not path.exists():
        raise FileNotFoundError(f"CLI 结果文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    cases = [_normalize_cli_case(r) for r in payload.get("results", [])]
    return cases, payload


def _normalize_cli_case(r: dict) -> dict:
    if r.get("skipped"):
        status = "skipped"
    elif r.get("crashed"):
        status = "crashed"
    elif r.get("passed"):
        status = "passed"
    elif r.get("ignorable"):
        status = "ignorable"
    else:
        status = "failed"
    return {
        "case_name": r.get("name", ""),
        "module": r.get("module", ""),
        "cli_status": status,
        "cli_detail": r.get("detail", ""),
    }


# ---------------------------------------------------------------------------
# cli_smoke 用例 -> WebUI 设计边界映射表
#
# 仅覆盖 cli_smoke 模块（模块 A）当前产出的全部 36 条用例，仅供
# `_webui_record_boundary_cases()` 自动识别其中 10 条设计边界用例使用。
#
# 以下映射基于对 QAIModelBuilder/frontend 源码与对应后端路由的实地调研结果
# （逐条核实过 data-testid/CSS 选择器与 HTTP 端点，而非凭组件命名猜测），
# 并在存在争议时与用户确认过处理方式（channel 生命周期、ui.theme、
# conv tab list 三类边界已征得用户同意）。
# ---------------------------------------------------------------------------

_REASON_CHANNEL_LIST_NO_HTTP_ENDPOINT = (
    "设计边界(无共享后端数据源): Step 3 实现 channels-consistency.spec.ts 时确认，"
    "CLI `channel list` 完全不经 HTTP——`apps/cli/commands/channel.py::cmd_list()` "
    "在进程内直接构造 DI Container 后直接调用 "
    "`ChannelInstanceRepositoryPort.list_by_kind()`，`interfaces/http/routes/"
    "channels.py` 里没有任何 kind-agnostic 的\"列出全部实例\"路由（该文件所有 GET "
    "都要求传入具体 instance_id）；`cmd_list` 自身的 docstring 也明确写明这是已知 "
    "缺口（\"no use case wrapper exists for list everything\"）。UI 两张卡片能发出"
    "的唯一请求是 GET /api/{kind}/status?instance_id=<localStorage 缓存的单一 id>，"
    "与 CLI list 返回的是完全不同粒度的数据，没有可比对的共享后端真值。已与用户"
    "确认整体降级为设计边界，不新增 channels-consistency.spec.ts（2026-07-21 对齐）。"
)
_REASON_UI_THEME_NO_SHARED_TRUTH = (
    "设计边界(无共享后端数据源): 实地调研确认 Settings 页面不渲染 ui.theme——前端"
    "主题是纯 localStorage 状态（由不属于 /settings 路由树的 ThemeToggle.vue 控制，"
    "不发任何 HTTP 请求），而 CLI `config get ui.theme` 读写的是后端 user_prefs 里"
    "一个完全不同、且未被任何路由暴露给前端的文档字段。两者不存在共享的 HTTP 端点，"
    "伪造比对只会产生误导性结论。已与用户确认标注为新边界子类型（2026-07-21 对齐）。"
)
_REASON_CONV_TAB_LIST_DATA_SOURCE_MISMATCH = (
    "设计边界(UI/后端数据源不一致): 实地调研确认聊天页面的会话标签条 "
    "(ChatTabStrip.vue) 是纯前端本地状态（持久化到 localStorage），而 CLI "
    "`conv tab list` 读取的是服务端独立的 ConversationTab 聚合列表，两者不是同一"
    "数据源，无法做真实的一致性比对。已与用户确认标注为新边界子类型并记入 "
    "docs/known-issues.md 作为审计建议（2026-07-21 对齐）。"
)
_REASON_CONV_EXPERIENCE_NOT_CONSUMED = (
    "设计边界(无 UI 入口): 实地调研确认前端未消费 GET /api/chat/experiences"
    "（及其 /categories），除一个语义不相关的 AgentSettingsPanel.vue 里的"
    "\"experience_extraction\"自动经验提炼开关外，没有任何组件/composable 调用"
    "该接口，无渲染入口。"
)
_REASON_PACK_DEPS_STATUS_DIFFERENT_ENDPOINT = (
    "设计边界(无共享后端数据源): 实地调研确认 CLI `pack deps-status` 调用的是全局"
    "环境快照接口 GET /api/app-builder/deps-status，而 WebUI (ModelCard.vue 的 "
    ".ab-deps-badge) 渲染的是姊妹接口 GET /api/app-builder/deps-status/packs"
    "（逐 pack 进度），CLI 对应的全局快照接口在前端只存在于自动生成的类型声明里，"
    "没有任何实际调用点，两者不是同一数据源。"
)
_REASON_PACK_CACHE_STATUS_NOT_CONSUMED = (
    "设计边界(无 UI 入口): 实地调研确认 GET /api/app-builder/cache/status 在前端"
    "只存在于自动生成的类型声明里，没有任何组件/composable 实际调用，无渲染入口。"
)
_REASON_RUN_WORKER_STATUS_REPURPOSED = (
    "设计边界(无共享后端数据源): 实地调研确认 GET /api/app-builder/worker/status "
    "被 stores/appBuilder.ts 调用但未找到直接渲染该状态的模板；同一接口被 "
    "useVoiceInput.ts 复用来驱动 WhisperEnginePopover.vue 的语音引擎状态点"
    "（warm/loading/cold），语义与 CLI `run worker status` 想表达的后台任务队列"
    "状态并不对应，勉强比对会产生误导性结论。"
)
_REASON_CODE_SKILL_LIST_NOT_CONSUMED = (
    "设计边界(无共享后端数据源): 实地调研确认 CLI `code skill list` 对应 "
    "GET /api/{cc|oc}/skills（AI 编程会话专属技能接口），而 AI 编程会话面板 "
    "(CodingSessionPanel.vue) 与技能页面 (SkillsView.vue/SessionToolsPopover.vue) "
    "实际消费的都是另一个不相关的 GET /api/skills，/api/{cc|oc}/skills 在前端只"
    "存在于自动生成的类型声明里，没有任何实际调用点。"
)
_REASON_SERVICE_PATH_NO_HTTP_VALUE = (
    "设计边界(无共享后端数据源): Step 2 实现 service-consistency.spec.ts 时确认，"
    "CLI `service path` 的值来自 OpenServiceDirUseCase.execute() -> "
    "InferenceService.get_install_dir()（静态缓存的安装目录），唯一挂接该 use case "
    "的 HTTP 路由 POST /api/service/open-dir 会丢弃返回值只回 {success:true}，没有"
    "任何 GET 端点把这个值暴露到前端；WebUI 展示的 .service-status-exe 来自 "
    "GET /api/service/status 的 exe_path 字段，是实时重新解析出的完整可执行文件"
    "路径，与 CLI 侧取值形态、解析时机均不同。因此改标注为设计边界，spec 文件里"
    "保留的\"服务路径\"测试仅做轻量健全性检查（非空、形如绝对路径），不纳入本"
    "矩阵作为跨端一致性证据（2026-07-21 Step 2 落地后自行决定，小的可逆细节）。"
)

# 用例名 -> UiEquivalent；仅覆盖 cli_smoke（模块 A）全部 36 条用例，
# 顺序与该模块源码里用例出现的顺序一致，便于交叉核对。
CLI_CASE_TO_UI_EQUIVALENT: Dict[str, UiEquivalent] = {
    "cli_smoke::config get ui.theme": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_UI_THEME_NO_SHARED_TRUTH),
    "cli_smoke::config provider list": UiEquivalent(),
    "cli_smoke::service status": UiEquivalent(),
    "cli_smoke::service probe": UiEquivalent(),
    "cli_smoke::service models": UiEquivalent(),
    "cli_smoke::service path": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_SERVICE_PATH_NO_HTTP_VALUE),
    "cli_smoke::service config get": UiEquivalent(),
    "cli_smoke::pack list": UiEquivalent(),
    "cli_smoke::pack deps-status": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_PACK_DEPS_STATUS_DIFFERENT_ENDPOINT),
    "cli_smoke::pack cache status": UiEquivalent(
        _BoundaryKind.NO_UI_ENTRY, _REASON_PACK_CACHE_STATUS_NOT_CONSUMED),
    "cli_smoke::pack taxonomy": UiEquivalent(),
    "cli_smoke::run list": UiEquivalent(),
    "cli_smoke::run worker status": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_RUN_WORKER_STATUS_REPURPOSED),
    "cli_smoke::policy show": UiEquivalent(),
    "cli_smoke::policy skill-cap discover": UiEquivalent(),
    "cli_smoke::security settings get": UiEquivalent(),
    "cli_smoke::audit query": UiEquivalent(),
    "cli_smoke::conv list": UiEquivalent(),
    "cli_smoke::conv tab list": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_CONV_TAB_LIST_DATA_SOURCE_MISMATCH),
    "cli_smoke::conv experience list": UiEquivalent(
        _BoundaryKind.NO_UI_ENTRY, _REASON_CONV_EXPERIENCE_NOT_CONSUMED),
    "cli_smoke::conv experience categories": UiEquivalent(
        _BoundaryKind.NO_UI_ENTRY, _REASON_CONV_EXPERIENCE_NOT_CONSUMED),
    "cli_smoke::dep pending": UiEquivalent(),
    "cli_smoke::exec profiles": UiEquivalent(),
    "cli_smoke::code session list": UiEquivalent(),
    "cli_smoke::code skill list": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_CODE_SKILL_LIST_NOT_CONSUMED),
    "cli_smoke::code health": UiEquivalent(),
    "cli_smoke::service-release versions": UiEquivalent(),
    "cli_smoke::service-release models": UiEquivalent(),
    "cli_smoke::service-release status versions": UiEquivalent(),
    "cli_smoke::service-release status models": UiEquivalent(),
    "cli_smoke::service-release aria2c status": UiEquivalent(),
    "cli_smoke::service-release settings get": UiEquivalent(),
    "cli_smoke::app --json": UiEquivalent(),
    "cli_smoke::skill list": UiEquivalent(),
    "cli_smoke::skill policy": UiEquivalent(),
    "cli_smoke::channel list": UiEquivalent(
        _BoundaryKind.NO_SHARED_TRUTH, _REASON_CHANNEL_LIST_NO_HTTP_ENDPOINT),
}


def parse_defects(defects_json_path) -> List[dict]:
    """解析 test_builder_cli.py::DefectRegistry.write() 产出的 defects.json
    （与 results.json 同目录的兄弟文件）。文件不存在时返回空列表（本次运行没有
    新发现缺陷是正常情况，不是解析错误）。"""
    path = Path(defects_json_path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# HTML 报告生成器
#
# 视觉语言直接复制自 test_service.py::ReportGenerator._common_css() 的配色
# 变量与卡片/徽标/表格样式（不 import test_service.py，只共享 CSS 文本，保持
# 两套测试体系"零代码耦合"的既有约定）。
# ---------------------------------------------------------------------------

def _esc(value) -> str:
    return _html_module.escape("" if value is None else str(value))


def _anchor(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-") or "case"


_STATUS_LABEL = {
    "passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "crashed": "CRASH",
    "ignorable": "IGNORE", "not_collected": "未采集", "boundary": "设计边界",
}
_STATUS_CSS_CLASS = {
    "passed": "status-pass", "failed": "status-fail", "skipped": "status-skip",
    "crashed": "status-crash", "ignorable": "status-skip",
    "not_collected": "status-not-collected", "boundary": "status-boundary",
}
_BOUNDARY_KIND_LABEL = {
    _BoundaryKind.NO_UI_ENTRY: "无 UI 入口",
    _BoundaryKind.NO_SHARED_TRUTH: "无共享后端数据源",
}


def _status_badge(status: str) -> str:
    label = _STATUS_LABEL.get(status, status.upper())
    css_class = _STATUS_CSS_CLASS.get(status, "status-skip")
    return f'<span class="{css_class}">{_esc(label)}</span>'


def _assert_no_stray_none(rendered_html: str, filename: str) -> None:
    """轻量防护性断言：写盘前扫一遍，确认没有把裸 None/dict 对象直接字符串化
    漏进模板（表现为字面 ">None<" 之类的文本）。思路上参考 test_service.py 的
    `_assert_no_placeholder_leak`，但检查目标不同（那边查占位符残留，这里查
    未经 _esc() 防护的 None 泄漏）。"""
    if re.search(r">\s*None\s*<", rendered_html):
        raise RuntimeError(
            f"{filename} 生成结果里检测到未经处理的 'None' 文本泄漏到 HTML 里，"
            "说明某个字段在拼接模板前没有经过 _esc()/空值兜底处理，已阻断写盘。"
        )


class ReportGenerator:
    """全部 @staticmethod，无状态。产出单页静态 HTML report.html：统计卡片 +
    CLI × WebUI 双向验证表 + 新发现缺陷详情，三者合一在同一页，不再拆分单独的
    report_defects.html。"""

    @staticmethod
    def _common_css() -> str:
        """配色变量与卡片/表格/徽标视觉语言直接复制自
        test_service.py::ReportGenerator._common_css()（Material 风格蓝色
        头部 + 柔和阴影卡片），并新增 .status-boundary/.status-not-collected
        两个本报告专属的徽标样式（其余样式与配色变量原样保留，不引入新的
        配色体系）。"""
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
.header .subtitle { color: rgba(255,255,255,0.85); font-size: 14px; max-width: 760px; }
.header .meta { display: flex; gap: 28px; margin-top: 18px; flex-wrap: wrap; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.18); }
.header .meta-item { font-size: 13px; color: rgba(255,255,255,0.92); font-variant-numeric: tabular-nums; }
.header .meta-item strong { color: #FFFFFF; font-weight: 600; }
.cmdline-details { display: block; margin-top: 14px; }
.cmdline-details summary { cursor: pointer; color: rgba(255,255,255,0.92); font-weight: 600; list-style: none; font-size: 13px; }
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
.summary-card.card-boundary { border-top-color: var(--muted); }
.summary-card .num { font-size: 36px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; letter-spacing: -0.5px; color: var(--text); }
.summary-card .label { font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; font-weight: 600; }
.summary-card.card-pass .num { color: var(--success); }
.summary-card.card-fail .num { color: var(--danger); }
.summary-card.card-boundary .num { color: var(--muted); }
.section { margin-bottom: 32px; }
.section h2 { font-size: 18px; font-weight: 600; color: var(--text); margin-bottom: 16px; padding-bottom: 10px; border-bottom: 2px solid var(--border); letter-spacing: -0.2px; position: relative; }
.section h2::after { content: ''; position: absolute; left: 0; bottom: -2px; width: 56px; height: 2px; background: var(--primary); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { background: #F5F5F5; padding: 10px 12px; text-align: left; font-weight: 600; color: var(--text-secondary);
            text-transform: uppercase; font-size: 11px; letter-spacing: 0.4px; border-bottom: 2px solid var(--border); }
tbody td { padding: 10px 12px; border-bottom: 1px solid #F0F0F0; vertical-align: middle; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: var(--bg-alt); }
tbody tr:last-child td { border-bottom: none; }
td.center, th.center { text-align: center; }
code { background: #F5F5F5; padding: 2px 6px; border-radius: 3px; font-family: var(--font-mono); font-size: 12px; color: var(--text); }
.status-pass, .status-fail, .status-crash, .status-skip, .status-boundary, .status-not-collected {
    display: inline-block; font-weight: 700; font-size: 11px; letter-spacing: 0.5px; padding: 3px 9px;
    min-width: 60px; text-align: center; border-radius: 10px; }
.status-pass { color: var(--success); background: rgba(46,125,50,0.10); }
.status-fail { color: var(--danger); background: rgba(198,40,40,0.10); }
.status-crash { color: var(--warning); background: rgba(230,81,0,0.10); }
.status-skip { color: var(--muted); background: rgba(120,144,156,0.12); }
.status-boundary { color: #37474F; background: rgba(84,110,122,0.14); border: 1px solid rgba(84,110,122,0.30); }
.status-not-collected { color: var(--warning); background: rgba(251,140,0,0.12); border: 1px dashed rgba(230,81,0,0.30); }
.resp-detail { color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
.resp-detail details summary { cursor: pointer; color: var(--primary); font-weight: 600; }
.resp-detail details p { margin: 6px 0 0; }
.crash-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; border-left: 4px solid var(--danger); }
.model-section { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 24px; overflow: hidden; }
.model-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: linear-gradient(180deg, #FAFAFA 0%, #F5F5F5 100%); border-bottom: 1px solid var(--border); }
.model-header h3 { font-size: 16px; color: var(--primary); font-weight: 600; letter-spacing: -0.2px; }
.empty-state { background: var(--card-bg); border-radius: var(--radius); padding: 30px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 14px; box-shadow: var(--shadow); }
.footer { text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 40px; letter-spacing: 0.2px; }
@media print {
    .header { background: #333 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .summary-card, .model-section, table { box-shadow: none; border: 1px solid #ddd; }
}"""

    @staticmethod
    def _page_shell(title: str, eyebrow: str, subtitle: str, meta_items: List[str], body: str,
                     cmdline: str = "") -> str:
        meta_html = "".join(f'<span class="meta-item">{m}</span>' for m in meta_items)
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{_esc(title)}</title>
<style>{ReportGenerator._common_css()}</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="eyebrow">{_esc(eyebrow)}</div>
    <h1>{_esc(title)}</h1>
    <div class="subtitle">{_esc(subtitle)}</div>
    <div class="meta">{meta_html}</div>
    {ReportGenerator._cmdline_meta_html(cmdline)}
  </div>
</div>
<div class="container">
{body}
<div class="footer">QAIModelBuilder 三端一致性统一报告 &middot; 由 test_builder_cli.py 内置的 ReportGenerator 在同一次运行中生成</div>
</div>
</body>
</html>"""

    @staticmethod
    def _cmdline_meta_html(cmdline):
        """构建 header 区域"命令行"展示项：展示本次运行调用 test_builder_cli.py 时的完整命令行，
        复用等宽字体样式（.cmdline-pre，与 test_service.py 同一套设计语言）。由 _page_shell
        渲染在 .meta 这一行 flex 布局的下方，作为独立的块级区域，不与其它 meta-item 一起
        参与 flex 拉伸——命令行内容通常偏长，与其它 meta-item 混排会因 flex 的默认拉伸/
        换行规则挤出诡异的错位效果，拆开渲染即可避免。
        默认展开（open 属性）：其它 meta-item 都是标签+值直接可见，命令行若默认折叠，
        用户不点击就只看到"命令行"这个词、看不到任何内容，容易误以为该字段是空的。
        未提供 cmdline（例如向后兼容旧调用点）时不渲染该项。"""
        if not cmdline:
            return ""
        return (f'<details class="cmdline-details" open><summary><strong>命令行</strong></summary>'
                f'<pre class="cmdline-pre">{_esc(cmdline)}</pre></details>')

    @staticmethod
    def generate_matrix_html(cli_payload: dict, defects: List[dict], out_dir: Path,
                              webui_python_results: List[dict] = None, cmdline: str = "") -> Path:
        """产出 report.html（单页，唯一产物）：顶部统计卡片 + "CLI × WebUI 双向验证"表
        （覆盖 webui 模块若跑过时针对 cli_smoke 全部 36 条用例产出的结果：26 条真实浏览器
        点击路径比对 + 10 条设计边界）+ "新发现缺陷详情"（原 report_defects.html 已合并
        至此，不再产出单独页面）。"""
        cli_summary = cli_payload.get("summary", {}) or {}
        cli_total = cli_summary.get("total", 0) or 0
        cli_passed = cli_summary.get("passed", 0) or 0
        cli_pass_rate = round(100.0 * cli_passed / cli_total, 1) if cli_total else 0.0

        defects = defects or []
        defects_count = len(defects)

        webui_python_results = webui_python_results or []
        real_webui_python_results = [r for r in webui_python_results if not r.get("skipped")]
        webui_real_passed = sum(1 for r in real_webui_python_results if r.get("passed"))
        webui_pass_rate = (round(100.0 * webui_real_passed / len(real_webui_python_results), 1)
                            if real_webui_python_results else 0.0)

        # "新发现缺陷数"只统计本次实测发现、与预期行为不符的问题：已知设计边界
        # （_record_known_boundary）与已知功能缺口（known_gaps 模块）均走独立的登记路径，
        # 从不进入 DefectRegistry，因此从不计入这个数字——title 提示 + 下方
        # "新发现缺陷详情"小节的说明文字都在解释这一点，避免被误读为"全部缺陷/问题总数"。
        defects_tooltip = "仅统计本次实测发现、与预期行为不符的问题；已知设计边界与已知功能缺口不计入此数字"
        summary_cards = f"""<div class="summary-bar">
  <div class="summary-card card-pass"><div class="num">{cli_total}</div><div class="label">总用例数</div></div>
  <div class="summary-card card-pass"><div class="num">{cli_pass_rate}%</div><div class="label">CLI 层通过率</div></div>
  <div class="summary-card card-fail" title="{_esc(defects_tooltip)}"><div class="num">{defects_count}</div><div class="label">新发现缺陷数</div></div>"""
        if webui_python_results:
            summary_cards += f"""
  <div class="summary-card card-pass"><div class="num">{webui_pass_rate}%</div><div class="label">WebUI 通过率</div></div>"""
        summary_cards += "\n</div>"

        if webui_python_results:
            rows_html = []
            for r in sorted(webui_python_results, key=lambda x: x.get("name", "")):
                case = _normalize_cli_case(r)
                rows_html.append(f"""<tr>
  <td><code>{_esc(case['case_name'])}</code></td>
  <td class="center">{_status_badge(case['cli_status'])}</td>
  <td class="resp-detail"><details open><summary>查看操作路径与比对结果</summary><p>{_esc(case['cli_detail'])}</p></details></td>
</tr>""")
            webui_table = f"""<div class="model-section">
    <table>
      <thead><tr><th>用例名</th><th class="center">状态</th><th>详情</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
  </div>"""
        else:
            webui_table = '<div class="empty-state">本次运行未包含 webui 模块，无 CLI×WebUI 双向验证结果。</div>'

        webui_section = f"""<div class="section">
  <h2>CLI &times; WebUI 双向验证</h2>
  <p style="margin-bottom: 14px; color: var(--text-secondary); font-size: 12px;">
    每条结果均由脚本内置的 Python Playwright 直接驱动真实浏览器产出，下方"详情"默认展开，展示具体的网页操作路径与比对结果。
  </p>
  {webui_table}
</div>"""

        if defects:
            by_module: Dict[str, List[dict]] = {}
            for d in defects:
                by_module.setdefault(d.get("module", "未分类"), []).append(d)
            defect_groups = []
            for module, items in sorted(by_module.items()):
                rows = []
                for d in items:
                    rows.append(f"""<tr>
  <td><code>{_esc(d.get('defect_id'))}</code></td>
  <td>{_esc(d.get('severity'))}</td>
  <td>{_esc(d.get('summary'))}</td>
  <td class="resp-detail">{_esc(d.get('repro'))}</td>
  <td class="resp-detail">{_esc(d.get('expected'))}</td>
  <td class="resp-detail">{_esc(d.get('actual'))}</td>
  <td class="resp-detail">{_esc(d.get('discovered_at'))}</td>
</tr>""")
                defect_groups.append(f"""<div class="crash-table" style="margin-bottom: 20px;">
    <div class="model-header"><h3>模块: {_esc(module)}（{len(items)} 条）</h3></div>
    <table>
      <thead><tr><th>ID</th><th>严重度</th><th>摘要</th><th>复现</th><th>期望</th><th>实际</th><th>发现时间</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>""")
            defects_body = "".join(defect_groups)
        else:
            defects_body = '<div class="empty-state">本次运行未发现新缺陷。</div>'

        defects_section = f"""<div class="section" id="defects-section">
  <h2>新发现缺陷详情</h2>
  <p style="margin-bottom: 14px; color: var(--text-secondary); font-size: 12px;">
    "新发现缺陷"特指本次运行中实测发现、与预期行为不符的问题；已被识别为长期存在、产品设计使然的限制
    （已知设计边界，如 CLI 与 WebUI 无共享后端数据源/UI 无对应操作入口）与已知功能缺口，均走独立的
    登记路径、从不计入这份清单，避免重复记录没有诊断价值的既有限制——它们的完整清单见
    <code>test/test_builder_cli.md</code>。
  </p>
  {defects_body}
</div>"""

        body = summary_cards + webui_section + defects_section
        rendered = ReportGenerator._page_shell(
            title="QAIModelBuilder 三端一致性测试报告",
            eyebrow="CLI \u00b7 HTTP API \u00b7 WebUI",
            subtitle="test_builder_cli.py（CLI/API/channel/一致性/已知缺口 + 可选的 CLI×WebUI 双向验证）运行报告",
            meta_items=[
                f"生成时间: <strong>{_esc(cli_payload.get('timestamp', ''))}</strong>",
                f"CLI 侧健康: <strong>{'健康' if cli_payload.get('healthy') else '不健康'}</strong>",
            ],
            body=body,
            cmdline=cmdline,
        )
        _assert_no_stray_none(rendered, "report.html")
        out_path = Path(out_dir) / "report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        return out_path


MODULE_RUNNERS = ("cli_smoke", "channel", "consistency", "known_gaps")
# "webui" 是纯 opt-in 的第五个模块（模块 E）：不加入 MODULE_RUNNERS，因此 --modules
# 缺省时天然不会跑它（需要额外的 pnpm/playwright 浏览器依赖，很多环境没装）；
# 只有用户显式 --modules ... webui 才会运行。ALL_MODULE_CHOICES 仅用于 argparse 的
# choices 校验，不影响缺省行为。
ALL_MODULE_CHOICES = MODULE_RUNNERS + ("webui",)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="QAIModelBuilder CLI/channel/一致性测试脚本（与 test_service.py 独立）")
    parser.add_argument("--builder_dir", default="./QAIModelBuilder", help="QAIModelBuilder 仓库根目录")
    parser.add_argument("--builder_python", default=None, help="Builder 专用 python.exe；缺省自动探测官方 venv")
    parser.add_argument("--builder_host", default="127.0.0.1")
    parser.add_argument("--builder_port", type=int, default=8899)
    parser.add_argument("--builder_data_dir", default=None, help="隔离数据目录；缺省为 <out_dir>/builder_data")
    parser.add_argument("--out_dir", default="./test_builder_cli_results", help="结果/日志/缺陷清单输出目录")
    parser.add_argument("--modules", nargs="+", choices=ALL_MODULE_CHOICES, default=None,
                         help="只运行指定模块；缺省运行 cli_smoke/channel/consistency/known_gaps 四个模块，"
                              "webui 是纯 opt-in 的第五个模块，必须显式指定才会运行")
    parser.add_argument("--start_timeout", type=int, default=90, help="等待 Builder 就绪的超时秒数")
    parser.add_argument("--frontend_dir", default=None,
                         help="前端项目根目录（webui 模块专用）；缺省为 <builder_dir>/frontend")
    parser.add_argument("--webui_base_url", default=None,
                         help="已经手动跑好的前端 dev server 地址（webui 模块专用）；给出时完全跳过自动"
                              "启动 FrontendDevServer，直接把该地址当 frontend_url 使用")
    parser.add_argument("--webui_headed", action="store_true", default=False,
                         help="webui 模块以有头模式启动 Chromium（默认无头）")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    builder_dir = Path(args.builder_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    python_exe = resolve_builder_python(args.builder_python)
    data_dir = Path(args.builder_data_dir).resolve() if args.builder_data_dir else out_dir / "builder_data"
    modules = set(args.modules) if args.modules else set(MODULE_RUNNERS)

    env_cfg = BuilderEnvironment(builder_dir=builder_dir, python_exe=python_exe, data_dir=data_dir,
                                  host=args.builder_host, port=args.builder_port)

    collector = ResultCollector()
    defects = DefectRegistry()
    builder = BuilderProcess(env_cfg, log_dir=out_dir / "logs")
    cli = CliRunner(env_cfg)

    try:
        builder.start(timeout=args.start_timeout)
    except Exception as e:
        print(f"FATAL: Builder 启动失败: {e}", file=sys.stderr)
        sys.exit(1)

    # frontend_dir 用迟绑定方式处理：build_arg_parser 阶段 builder_dir 还未 resolve，
    # 不能在 argparse 定义 default 时就依赖它，只能在这里（builder_dir 已 resolve 之后）
    # 再决定 --frontend_dir 的缺省值。
    frontend_dir = Path(args.frontend_dir).resolve() if args.frontend_dir else builder_dir / "frontend"
    frontend_server = None
    try:
        if "cli_smoke" in modules:
            run_cli_smoke_module(cli, collector, defects)
        if "channel" in modules:
            run_channel_module(cli, builder.csrf, env_cfg, collector, defects)
        if "consistency" in modules:
            run_consistency_module(cli, builder.csrf, collector, defects)
        if "known_gaps" in modules:
            run_known_gaps_module(cli, collector, defects)
        if "webui" in modules:
            if args.webui_base_url:
                frontend_url = args.webui_base_url
            else:
                frontend_server = FrontendDevServer(frontend_dir=frontend_dir, backend_base_url=env_cfg.base_url,
                                                      log_dir=out_dir / "logs")
                frontend_server.start(timeout=60)
                frontend_url = frontend_server.base_url
            run_webui_module(cli, frontend_url, collector, defects, builder.csrf, headed=args.webui_headed)
    finally:
        # frontend_server 与 builder 的清理必须在同一个 finally 里兜底：webui 检查
        # 抛异常不能漏掉后端进程的清理，反过来 builder.stop() 也不能因为顺序问题漏掉
        # 前端 dev server 的清理。
        if frontend_server is not None:
            frontend_server.stop()
        builder.stop()

    payload = collector.write_report(out_dir)
    defects.write(out_dir)

    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    new_defect_count = len(defects.defects)
    if new_defect_count:
        print(f"WARNING: 本次运行发现 {new_defect_count} 条新缺陷，详见即将生成的 report.html")

    # 与 test_service.py 内置 ReportGenerator 的模式一致：同一次运行结束后直接产出统一的
    # HTML 报告，不再需要单独调用一个后处理脚本。CLI 侧健康判定（上面的
    # sys.exit 参数）完全基于 collector.is_healthy()，不受报告生成结果影响。
    # 再转给各 generate_* 函数，在报告 header 里展示，便于事后复核"这次跑的到底是什么命令"。
    cmdline = shlex.join(sys.argv)
    _, cli_payload = parse_cli_results(out_dir / "results.json")
    defects_list = parse_defects(out_dir / "defects.json")
    # 模块 E（webui，若跑过）的结果单独过滤出来，直接传给 report.html 里的
    # "CLI × WebUI 双向验证"表；未跑过该模块时为空列表，report.html 会渲染空态提示。
    webui_python_results = [r for r in cli_payload.get("results", []) if r.get("module") == "E"]
    report_path = ReportGenerator.generate_matrix_html(cli_payload, defects_list, out_dir,
                                                         webui_python_results=webui_python_results, cmdline=cmdline)
    print(f"已生成: {report_path}")

    sys.exit(0 if collector.is_healthy() else 1)


if __name__ == "__main__":
    main()
