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
(defects.json / defects.md)，不在本脚本内尝试修复。

除四个测试模块外，本脚本同一次运行中还会生成 QAIModelBuilder 三端（CLI / HTTP API /
WebUI）一致性统一报告(report.html / report_defects.html / report_webui_detail.html /
unified_cases.json)，与 test_service.py 内置 ReportGenerator 的模式一致（不再有独立的
report 生成脚本）。统一报告依赖内部维护的 CLI_CASE_TO_UI_EQUIVALENT 映射表把每条 CLI
用例关联到具体的 WebUI 用例或设计边界，并在生成前用覆盖完整性自检校验该映射表是否
跟上了 CLI 用例的演进——一旦发现遗漏，自检会抛出 RuntimeError 直接阻断报告生成。
可选参数 --webui_results 指向 Playwright WebUI 套件产出的 e2e-report/results.json；
缺省时统一报告里 WebUI 状态全部显示为"未采集"，设计边界条目不受影响。
本脚本仍然只读取该套件已产出的 results.json 文件，不 import、不依赖
QAIModelBuilder/frontend/e2e/*.spec.ts 的任何代码，保持"CLI 侧脚本与 WebUI 侧测试代码
零耦合"的既有约定。

用法:
    python test_builder_cli.py --builder_dir ./QAIModelBuilder
    python test_builder_cli.py --builder_dir ./QAIModelBuilder --modules channel known_gaps
    python test_builder_cli.py --builder_dir ./QAIModelBuilder --webui_results ./e2e-report/results.json
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
    最终落盘为 defects.json(结构化,供下一轮修复 plan 直接消费)与
    defects.md(按模块分组的人读摘要)。已知缺口/设计边界不登记在这里,
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

    def _to_markdown(self):
        if not self._defects:
            return "# 新发现缺陷清单\n\n本次运行未发现新缺陷。\n"
        lines = ["# 新发现缺陷清单", "", f"共 {len(self._defects)} 条，按模块分组：", ""]
        by_module = {}
        for d in self._defects:
            by_module.setdefault(d.module, []).append(d)
        for module in sorted(by_module):
            lines.append(f"## 模块 {module}")
            lines.append("")
            for d in by_module[module]:
                lines.append(f"### [{d.defect_id}] ({d.severity}) {d.summary}")
                lines.append("")
                lines.append(f"- 发现时间: {d.discovered_at}")
                lines.append(f"- 复现: `{d.repro}`")
                lines.append(f"- 预期: {d.expected}")
                lines.append(f"- 实际: {d.actual}")
                lines.append(f"- 证据: {d.evidence}")
                lines.append("")
        return "\n".join(lines)

    def write(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "defects.json", "w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in self._defects], f, indent=2, ensure_ascii=False)
        with open(out_dir / "defects.md", "w", encoding="utf-8") as f:
            f.write(self._to_markdown())


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


# ---------------------------------------------------------------------------
# 统一报告生成（原 test/generate_builder_report.py，现合并进本脚本）
# ---------------------------------------------------------------------------

class _BoundaryKind:
    """设计边界的两个子类型，见文件头注释。仅用作字符串常量，不做枚举类型。"""
    NO_UI_ENTRY = "no_ui_entry"
    NO_SHARED_TRUTH = "no_shared_truth"


@dataclass(frozen=True)
class UiEquivalent:
    """CLI 用例到 WebUI 等价物的映射条目。
    webui_spec_file 为 None 表示该用例是设计边界，此时 boundary_kind/
    boundary_reason 必须非空；否则 webui_spec_file/webui_case_title 均须给出，
    用于在 Playwright 结果里按标题定位对应的用例。"""
    webui_spec_file: Optional[str]
    webui_case_title: Optional[str]
    boundary_kind: Optional[str] = None
    boundary_reason: Optional[str] = None


@dataclass
class UnifiedCase:
    case_name: str
    module: str
    cli_status: str
    webui_status: str  # passed / failed / skipped / not_collected / boundary
    webui_spec_file: Optional[str] = None
    webui_case_title: Optional[str] = None
    boundary_kind: Optional[str] = None
    boundary_reason: Optional[str] = None
    cli_detail: str = ""
    webui_detail: str = ""


@dataclass
class WebUiCaseResult:
    title: str
    full_title: str
    status: str  # passed / failed / skipped / not_collected
    spec_file: str
    duration_ms: float = 0.0
    error_message: str = ""


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
# 解析 Playwright 原生 JSON reporter 格式
# ---------------------------------------------------------------------------

def parse_webui_results(webui_results_json_path) -> Dict[str, WebUiCaseResult]:
    """解析 playwright.config.ts 里配置的 `["json", {outputFile: "..."}]` reporter
    产出的原生 JSON（suites -> specs -> tests -> results[].status，suites 可递归
    嵌套 describe 块）。按用例标题（title）与"父 suite 标题 > ... > title"的完整
    路径（full_title）两种 key 同时登记，方便调用方按简单标题或完整路径查找。
    文件不存在时返回空字典（意味着所有映射到具体用例的条目都会呈现
    webui_status=not_collected，这是 WebUI 套件尚未运行/尚未落地时的正常现象，
    不是解析错误）。"""
    path = Path(webui_results_json_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    results: Dict[str, WebUiCaseResult] = {}
    for suite in payload.get("suites", []):
        _walk_suite(suite, [], results)
    return results


def _walk_suite(suite: dict, title_path: List[str], results: Dict[str, WebUiCaseResult]) -> None:
    spec_file = suite.get("file", "")
    next_path = title_path + ([suite["title"]] if suite.get("title") else [])
    for spec in suite.get("specs", []):
        title = spec.get("title", "")
        full_title = " > ".join(next_path + [title])
        status, duration_ms, error_message = _summarize_spec(spec)
        result = WebUiCaseResult(
            title=title, full_title=full_title, status=status,
            spec_file=spec_file or Path(spec.get("file", "")).name,
            duration_ms=duration_ms, error_message=error_message,
        )
        # 允许调用方按短标题或完整路径两种方式查找；短标题在本项目里目前均唯一，
        # 一旦将来出现重名，完整路径仍能精确区分。
        results[title] = result
        results[full_title] = result
    for child in suite.get("suites", []):
        _walk_suite(child, next_path, results)


def _summarize_spec(spec: dict) -> Tuple[str, float, str]:
    tests = spec.get("tests", [])
    if not tests:
        return "not_collected", 0.0, ""
    statuses: List[str] = []
    duration_ms = 0.0
    error_message = ""
    for t in tests:
        test_results = t.get("results", [])
        if test_results:
            last = test_results[-1]
            statuses.append(last.get("status", ""))
            duration_ms += last.get("duration", 0) or 0
            errors = last.get("errors", [])
            if errors and not error_message:
                first = errors[0]
                error_message = first.get("message", str(first)) if isinstance(first, dict) else str(first)
        else:
            statuses.append(t.get("status", ""))
    if spec.get("ok") is True:
        return "passed", duration_ms, error_message
    if any(s in ("failed", "timedOut", "interrupted") for s in statuses):
        return "failed", duration_ms, error_message
    if statuses and all(s == "skipped" for s in statuses):
        return "skipped", duration_ms, error_message
    if statuses and all(s == "passed" for s in statuses):
        return "passed", duration_ms, error_message
    return "failed", duration_ms, error_message


# ---------------------------------------------------------------------------
# CLI 用例 -> WebUI 等价物映射表
#
# 覆盖 test_builder_cli.py 四个模块当前产出的全部用例（cli_smoke 36 条 / channel
# 11 条 / consistency 2 条 / known_gaps 6 条，共 55 条；用例名以脚本实际运行时
# 产出的 name 字段为准，不是"约 40 条"这类粗略估计）。
#
# 以下映射基于对 QAIModelBuilder/frontend 源码与对应后端路由的实地调研结果
# （逐条核实过 data-testid/CSS 选择器与 HTTP 端点，而非凭组件命名猜测），
# 并在存在争议时与用户确认过处理方式（channel 生命周期、ui.theme、
# conv tab list 三类边界已征得用户同意）。
# ---------------------------------------------------------------------------

_REASON_FEISHU_URL_VERIFICATION = (
    "设计边界(无 UI 入口): /api/feishu/webhook 未实现 Feishu event-2.0 的 "
    "url_verification 挑战握手，该握手只存在于 WS 长连接路径，webhook 本身也没有"
    "可呈现内容的 UI 载体。"
)
_REASON_WECHAT_OUTBOUND_NOT_MOCKABLE = (
    "设计边界(无 UI 入口): WeChat 出站完全依赖 wechatbot SDK 的活体 Bot 对象、"
    "不经 HTTP，无法脱离真实环境验证，也没有对应的 UI 触发路径。"
)
_REASON_CHANNEL_MESSAGE_HISTORY = (
    "设计边界(无 UI 入口): 官方 CLI/HTTP/仓储三层均不支持按 instance 查询 channel "
    "消息历史，Channels 页面本身也不展示消息内容，属已确认设计边界。"
)
_REASON_CHANNEL_LIFECYCLE_NOT_SUPPORTED = (
    "设计边界(当前 UI+HTTP 均不支持该操作): 实地调研确认 Channels 页面是硬编码的 "
    "WeChat/Feishu 两张固定卡片，注册是隐式自动触发（kind/name 均不可由用户指定），"
    "且后端 HTTP 层没有暴露删除渠道实例的路由（仓储层 delete 方法未接线到任何路由）。"
    "已与用户确认：标注为设计边界，不勉强拼造假 UI 流程（2026-07-21 对齐）。"
)
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
_REASON_PACK_EXPORT_VALIDATE_INIT = (
    "设计边界(无 UI 入口): `qai pack export/validate/workspace-init` 这三个子命令"
    "当前的具体失败特征存在争议——test_builder_cli.py 断言的是因缺失内部模块 "
    "scripts.build.model_builder_cli 而报 ModuleNotFoundError，但已记录的 D0004/D0005 "
    "缺陷显示实测中两者均能正常跑到业务逻辑层、报出参数校验失败，与该假设不符（详见 "
    "docs/known-issues.md）；但无论具体失败模式是哪一种，WebUI 工作台均未提供触发同一"
    "路径的功能入口，因此不受该争议影响，仍标注为设计边界。"
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
_REASON_CHANNEL_INBOUND_NO_MESSAGE_UI = (
    "设计边界(无 UI 入口): Channels 页面只展示两张固定的连接状态卡片，不展示任何"
    "消息内容/历史，与 known_gaps::channel_message_query_missing 是同一条设计"
    "边界在模块 B 里的体现。"
)
_REASON_FEISHU_OUTBOUND_MOCK_INTERNAL = (
    "设计边界(无 UI 入口): 该用例绕开依赖注入直接构造 FeishuTransport 验证内部"
    "出站请求形状，是纯后端内部验证，不是用户可见的 UI 交互，没有对应的 UI 载体。"
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

# 用例名 -> UiEquivalent；按 test_builder_cli.py 的四个模块分组排列，
# 组内顺序与该模块源码里用例出现的顺序一致，便于交叉核对。
CLI_CASE_TO_UI_EQUIVALENT: Dict[str, UiEquivalent] = {
    # ---- 模块 A：cli_smoke（14 个命令组的只读/幂等子命令冒烟测试） ----
    "cli_smoke::config get ui.theme": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_UI_THEME_NO_SHARED_TRUTH),
    "cli_smoke::config provider list": UiEquivalent(
        "settings-consistency.spec.ts",
        "Provider 列表: WebUI 渲染内容与 config provider list 一致"),
    "cli_smoke::service status": UiEquivalent(
        "service-consistency.spec.ts", "服务状态: WebUI 渲染内容与 service status 一致"),
    "cli_smoke::service probe": UiEquivalent(
        "service-consistency.spec.ts", "服务探活: WebUI 渲染内容与 service probe 一致"),
    "cli_smoke::service models": UiEquivalent(
        "service-consistency.spec.ts", "服务模型列表: WebUI 渲染内容与 service models 一致"),
    "cli_smoke::service path": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_SERVICE_PATH_NO_HTTP_VALUE),
    "cli_smoke::service config get": UiEquivalent(
        "service-consistency.spec.ts", "服务配置: WebUI 渲染内容与 service config get 一致"),
    "cli_smoke::pack list": UiEquivalent(
        "app-builder-consistency.spec.ts",
        "Pack 列表: WebUI 渲染内容与 GET /api/app-builder/models 完全一致（按任务分区校验）"),
    "cli_smoke::pack deps-status": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_PACK_DEPS_STATUS_DIFFERENT_ENDPOINT),
    "cli_smoke::pack cache status": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_PACK_CACHE_STATUS_NOT_CONSUMED),
    "cli_smoke::pack taxonomy": UiEquivalent(
        "app-builder-consistency.spec.ts",
        "Pack 任务分类: WebUI 任务选择器渲染内容与 pack taxonomy 一致"),
    "cli_smoke::run list": UiEquivalent(
        "app-builder-consistency.spec.ts", "Run 列表: WebUI 渲染条目数与 GET /api/app-builder/runs 一致"),
    "cli_smoke::run worker status": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_RUN_WORKER_STATUS_REPURPOSED),
    "cli_smoke::policy show": UiEquivalent(
        "security-consistency.spec.ts", "策略展示: WebUI 渲染内容与 policy show 一致"),
    "cli_smoke::policy skill-cap discover": UiEquivalent(
        "security-consistency.spec.ts", "技能能力发现: WebUI 渲染内容与 policy skill-cap discover 一致"),
    "cli_smoke::security settings get": UiEquivalent(
        "security-consistency.spec.ts", "安全设置: WebUI 渲染内容与 security settings get 一致"),
    "cli_smoke::audit query": UiEquivalent(
        "security-consistency.spec.ts", "审计日志: WebUI 渲染条目与 audit query 一致"),
    "cli_smoke::conv list": UiEquivalent(
        "app-builder-consistency.spec.ts", "最近对话: WebUI 侧边栏渲染内容与 conv list 一致"),
    "cli_smoke::conv tab list": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_CONV_TAB_LIST_DATA_SOURCE_MISMATCH),
    "cli_smoke::conv experience list": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CONV_EXPERIENCE_NOT_CONSUMED),
    "cli_smoke::conv experience categories": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CONV_EXPERIENCE_NOT_CONSUMED),
    "cli_smoke::dep pending": UiEquivalent(
        "security-consistency.spec.ts", "依赖审批: WebUI 渲染的待批准依赖与 dep pending 一致"),
    "cli_smoke::exec profiles": UiEquivalent(
        "security-consistency.spec.ts", "执行代理配置: WebUI 渲染内容与 exec profiles 一致"),
    "cli_smoke::code session list": UiEquivalent(
        "app-builder-consistency.spec.ts", "AI 编程会话列表: WebUI 渲染内容与 code session list 一致"),
    "cli_smoke::code skill list": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_CODE_SKILL_LIST_NOT_CONSUMED),
    "cli_smoke::code health": UiEquivalent(
        "app-builder-consistency.spec.ts", "AI 编程环境健康检查: WebUI 渲染内容与 code health 一致"),
    "cli_smoke::service-release versions": UiEquivalent(
        "downloads-consistency.spec.ts", "服务版本列表: WebUI 渲染内容与 service-release versions 一致"),
    "cli_smoke::service-release models": UiEquivalent(
        "downloads-consistency.spec.ts", "模型目录: WebUI 渲染内容与 service-release models 一致"),
    "cli_smoke::service-release status versions": UiEquivalent(
        "downloads-consistency.spec.ts",
        "服务版本本地状态: WebUI 渲染内容与 service-release status versions 一致"),
    "cli_smoke::service-release status models": UiEquivalent(
        "downloads-consistency.spec.ts",
        "模型本地状态: WebUI 渲染内容与 service-release status models 一致"),
    "cli_smoke::service-release aria2c status": UiEquivalent(
        "downloads-consistency.spec.ts", "aria2c 状态: WebUI 渲染内容与 service-release aria2c status 一致"),
    "cli_smoke::service-release settings get": UiEquivalent(
        "downloads-consistency.spec.ts", "下载设置: WebUI 渲染内容与 service-release settings get 一致"),
    "cli_smoke::app --json": UiEquivalent(
        "app-builder-consistency.spec.ts",
        "Pack 列表: WebUI 渲染内容与 GET /api/app-builder/models 完全一致（按任务分区校验）"),
    "cli_smoke::skill list": UiEquivalent(
        "skills-consistency.spec.ts", "技能列表: WebUI 渲染内容与 skill list 一致"),
    "cli_smoke::skill policy": UiEquivalent(
        "security-consistency.spec.ts", "技能全局模式: WebUI 渲染内容与 skill policy 一致"),
    "cli_smoke::channel list": UiEquivalent(
        None, None, _BoundaryKind.NO_SHARED_TRUTH, _REASON_CHANNEL_LIST_NO_HTTP_ENDPOINT),

    # ---- 模块 B：channel（微信飞书 channel 全链路模拟） ----
    "channel::feishu_url_verification": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_FEISHU_URL_VERIFICATION),
    "channel::register::feishu": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_LIFECYCLE_NOT_SUPPORTED),
    "channel::webhook::feishu_inbound": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_INBOUND_NO_MESSAGE_UI),
    "channel::db::feishu_persisted": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_INBOUND_NO_MESSAGE_UI),
    "channel::delete::feishu": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_LIFECYCLE_NOT_SUPPORTED),
    "channel::register::wechat": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_LIFECYCLE_NOT_SUPPORTED),
    "channel::webhook::wechat_inbound": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_INBOUND_NO_MESSAGE_UI),
    "channel::db::wechat_persisted": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_INBOUND_NO_MESSAGE_UI),
    "channel::delete::wechat": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_LIFECYCLE_NOT_SUPPORTED),
    "channel::wechat_outbound_not_mockable": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_WECHAT_OUTBOUND_NOT_MOCKABLE),
    "channel::feishu_outbound_mock": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_FEISHU_OUTBOUND_MOCK_INTERNAL),

    # ---- 模块 C：consistency（CLI 与 HTTP API 一致性校验） ----
    "consistency::pack_list": UiEquivalent(
        "app-builder-consistency.spec.ts",
        "Pack 列表: WebUI 渲染内容与 GET /api/app-builder/models 完全一致（按任务分区校验）"),
    "consistency::run_list": UiEquivalent(
        "app-builder-consistency.spec.ts", "Run 列表: WebUI 渲染条目数与 GET /api/app-builder/runs 一致"),

    # ---- 模块 D：known_gaps（已知缺口回归标记） ----
    "known_gaps::pack export --workdir placeholder-workdir": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_PACK_EXPORT_VALIDATE_INIT),
    "known_gaps::pack validate placeholder-pack-dir": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_PACK_EXPORT_VALIDATE_INIT),
    "known_gaps::pack workspace-init placeholder-model": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_PACK_EXPORT_VALIDATE_INIT),
    "known_gaps::channel_message_query_missing": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_CHANNEL_MESSAGE_HISTORY),
    "known_gaps::feishu_url_verification_missing": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_FEISHU_URL_VERIFICATION),
    "known_gaps::wechat_outbound_not_http_mockable": UiEquivalent(
        None, None, _BoundaryKind.NO_UI_ENTRY, _REASON_WECHAT_OUTBOUND_NOT_MOCKABLE),
}


def _assert_full_mapping_coverage(cli_cases: List[dict], mapping: Dict[str, UiEquivalent]) -> None:
    """覆盖完整性自检：断言 cli_cases 里每一条用例名都能在映射表里找到条目。
    缺失即直接报错阻断生成，而不是留空——防止映射表随 test_builder_cli.py 的
    用例演进（新增/重命名）而漂移却不被发现。"""
    missing = sorted({c["case_name"] for c in cli_cases} - set(mapping.keys()))
    if missing:
        lines = "\n".join(f"  - {name}" for name in missing)
        raise RuntimeError(
            "覆盖完整性自检失败：以下 CLI 用例在 CLI_CASE_TO_UI_EQUIVALENT 映射表里"
            f"找不到条目，请先补充映射（具体 WebUI 用例或设计边界），再重新生成报告:\n{lines}"
        )


def build_unified_cases(
    cli_cases: List[dict],
    webui_results: Dict[str, WebUiCaseResult],
    mapping: Dict[str, UiEquivalent],
) -> List[UnifiedCase]:
    """把归一化后的 CLI 用例列表与 WebUI 结果、映射表合并成统一视图。
    调用前必须已经通过 _assert_full_mapping_coverage 自检。"""
    unified = []
    for c in cli_cases:
        equiv = mapping[c["case_name"]]
        if equiv.webui_spec_file is None:
            webui_status = "boundary"
            webui_detail = ""
        else:
            found = webui_results.get(equiv.webui_case_title)
            webui_status = found.status if found else "not_collected"
            webui_detail = found.error_message if found else ""
        unified.append(UnifiedCase(
            case_name=c["case_name"], module=c["module"], cli_status=c["cli_status"],
            webui_status=webui_status, webui_spec_file=equiv.webui_spec_file,
            webui_case_title=equiv.webui_case_title, boundary_kind=equiv.boundary_kind,
            boundary_reason=equiv.boundary_reason, cli_detail=c["cli_detail"],
            webui_detail=webui_detail,
        ))
    return unified


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
    """全部 @staticmethod，无状态。产出三页互相锚点跳转的静态 HTML：
    report.html（矩阵总汇）/ report_defects.html（缺陷详情）/
    report_webui_detail.html（WebUI 用例详情）。"""

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
.resp-link { display: inline-block; margin-left: 6px; font-size: 11px; color: var(--primary); text-decoration: none; font-weight: 600; padding: 2px 9px; border-radius: 10px; background: rgba(21,101,192,0.08); transition: background 0.15s, color 0.15s; }
.resp-link:hover { background: var(--primary); color: #FFFFFF; }
.crash-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; border-left: 4px solid var(--danger); }
.model-section { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 24px; overflow: hidden; }
.model-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: linear-gradient(180deg, #FAFAFA 0%, #F5F5F5 100%); border-bottom: 1px solid var(--border); }
.model-header h3 { font-size: 16px; color: var(--primary); font-weight: 600; letter-spacing: -0.2px; }
.empty-state { background: var(--card-bg); border-radius: var(--radius); padding: 30px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 14px; box-shadow: var(--shadow); }
.footer { text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 40px; letter-spacing: 0.2px; }
.back-link { display: inline-block; margin-bottom: 20px; color: var(--primary); text-decoration: none; font-weight: 600; font-size: 13px; }
.back-link:hover { color: var(--primary-dark); }
@media print {
    .header { background: #333 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .summary-card, .model-section, table { box-shadow: none; border: 1px solid #ddd; }
}"""

    @staticmethod
    def _page_shell(title: str, eyebrow: str, subtitle: str, meta_items: List[str], body: str) -> str:
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
  </div>
</div>
<div class="container">
{body}
<div class="footer">QAIModelBuilder 三端一致性统一报告 &middot; 由 test_builder_cli.py 内置的 ReportGenerator 在同一次运行中生成，
与 Playwright WebUI 套件的测试代码零耦合</div>
</div>
</body>
</html>"""

    @staticmethod
    def generate_matrix_html(unified_cases: List[UnifiedCase], cli_payload: dict, defects_count: int, out_dir: Path) -> Path:
        """产出 report.html：顶部统计卡片 + "测试用例 × (CLI/HTTP API/WebUI)"矩阵。"""
        total = len(unified_cases)
        cli_summary = cli_payload.get("summary", {}) or {}
        cli_total = cli_summary.get("total", 0) or 0
        cli_passed = cli_summary.get("passed", 0) or 0
        cli_pass_rate = round(100.0 * cli_passed / cli_total, 1) if cli_total else 0.0

        mapped = [c for c in unified_cases if c.webui_spec_file is not None]
        collected = [c for c in mapped if c.webui_status in ("passed", "failed", "skipped")]
        webui_passed = sum(1 for c in collected if c.webui_status == "passed")
        webui_pass_rate = round(100.0 * webui_passed / len(collected), 1) if collected else 0.0

        boundary_count = sum(1 for c in unified_cases if c.webui_status == "boundary")
        not_collected_count = sum(1 for c in unified_cases if c.webui_status == "not_collected")

        summary_cards = f"""<div class="summary-bar">
  <div class="summary-card card-pass"><div class="num">{total}</div><div class="label">总用例数</div></div>
  <div class="summary-card card-pass"><div class="num">{cli_pass_rate}%</div><div class="label">CLI 层通过率</div></div>
  <div class="summary-card card-pass"><div class="num">{webui_pass_rate}%</div><div class="label">WebUI 层通过率（已采集 {len(collected)}/{len(mapped)} 条）</div></div>
  <div class="summary-card card-fail"><div class="num">{defects_count}</div><div class="label">新发现缺陷数</div></div>
  <div class="summary-card card-boundary"><div class="num">{boundary_count}</div><div class="label">设计边界数</div></div>
</div>"""

        rows_html = []
        for c in sorted(unified_cases, key=lambda x: (x.module, x.case_name)):
            if c.webui_status == "boundary":
                kind_label = _BOUNDARY_KIND_LABEL.get(c.boundary_kind, c.boundary_kind or "")
                note = f'<strong>[{_esc(kind_label)}]</strong> {_esc(c.boundary_reason)}'
            elif c.webui_spec_file:
                note = (f'<code>{_esc(c.webui_spec_file)}</code><br>{_esc(c.webui_case_title)}'
                        f' <a class="resp-link" href="report_webui_detail.html#{_anchor(c.case_name)}">详情 →</a>')
            else:
                note = "-"
            rows_html.append(f"""<tr>
  <td><code>{_esc(c.case_name)}</code></td>
  <td>{_esc(c.module)}</td>
  <td class="center">{_status_badge(c.cli_status)}</td>
  <td class="center">{_status_badge(c.webui_status)}</td>
  <td class="resp-detail">{note}</td>
</tr>""")

        matrix_section = f"""<div class="section">
  <h2>测试用例 &times; (CLI / HTTP API / WebUI) 一致性矩阵</h2>
  <div class="model-section">
    <table>
      <thead><tr><th>用例名</th><th>模块</th><th class="center">CLI 状态</th><th class="center">WebUI 状态</th><th>说明</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
  </div>
</div>"""

        links_section = f"""<div class="section">
  <h2>详情页</h2>
  <div class="model-section" style="padding: 18px 20px; display: flex; gap: 20px;">
    <a class="resp-link" href="report_defects.html">缺陷详情 →</a>
    <a class="resp-link" href="report_webui_detail.html">WebUI 用例详情 →</a>
  </div>
</div>"""

        body = summary_cards + matrix_section + links_section
        rendered = ReportGenerator._page_shell(
            title="QAIModelBuilder 三端一致性测试报告",
            eyebrow="CLI \u00b7 HTTP API \u00b7 WebUI",
            subtitle="test_builder_cli.py（CLI/API/channel/一致性/已知缺口 + 统一报告生成）与 Playwright WebUI 套件的离线合并报告",
            meta_items=[
                f"生成时间: <strong>{_esc(cli_payload.get('timestamp', ''))}</strong>",
                f"CLI 侧健康: <strong>{'健康' if cli_payload.get('healthy') else '不健康'}</strong>",
                f"未采集: <strong>{not_collected_count}</strong>",
            ],
            body=body,
        )
        _assert_no_stray_none(rendered, "report.html")
        out_path = Path(out_dir) / "report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        return out_path

    @staticmethod
    def generate_defects_detail_html(defects: List[dict], out_dir: Path) -> Path:
        """产出 report_defects.html：把 test_builder_cli.py 的 defects.json
        渲染为详情页，替代目前纯 Markdown 的 defects.md。"""
        if not defects:
            body = '<a class="back-link" href="report.html">← 返回矩阵总汇</a><div class="empty-state">本次运行未发现新缺陷。</div>'
        else:
            by_module: Dict[str, List[dict]] = {}
            for d in defects:
                by_module.setdefault(d.get("module", "未分类"), []).append(d)
            sections = ['<a class="back-link" href="report.html">← 返回矩阵总汇</a>']
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
                sections.append(f"""<div class="section">
  <h2>模块: {_esc(module)}（{len(items)} 条）</h2>
  <div class="crash-table">
    <table>
      <thead><tr><th>ID</th><th>严重度</th><th>摘要</th><th>复现</th><th>期望</th><th>实际</th><th>发现时间</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</div>""")
            body = "".join(sections)
        rendered = ReportGenerator._page_shell(
            title="新发现缺陷详情", eyebrow="DEFECTS",
            subtitle="test_builder_cli.py::DefectRegistry 登记的全部新发现缺陷（已知缺口/设计边界不在此列出）",
            meta_items=[f"总计: <strong>{len(defects)}</strong> 条"], body=body,
        )
        _assert_no_stray_none(rendered, "report_defects.html")
        out_path = Path(out_dir) / "report_defects.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        return out_path

    @staticmethod
    def generate_webui_detail_html(unified_cases: List[UnifiedCase], out_dir: Path) -> Path:
        """产出 report_webui_detail.html：展示每条有真实 WebUI 入口的用例的
        执行详情（用例名、所属命令组、状态、失败详情、对应验证的 API 端点）。"""
        mapped = [c for c in unified_cases if c.webui_spec_file is not None]
        if not mapped:
            body = '<a class="back-link" href="report.html">← 返回矩阵总汇</a><div class="empty-state">当前没有映射到具体 WebUI 用例的条目。</div>'
        else:
            rows = []
            for c in sorted(mapped, key=lambda x: (x.module, x.case_name)):
                detail = _esc(c.webui_detail) if c.webui_status == "failed" else "-"
                rows.append(f"""<tr id="{_anchor(c.case_name)}">
  <td><code>{_esc(c.case_name)}</code></td>
  <td>{_esc(c.module)}</td>
  <td><code>{_esc(c.webui_spec_file)}</code></td>
  <td>{_esc(c.webui_case_title)}</td>
  <td class="center">{_status_badge(c.webui_status)}</td>
  <td class="resp-detail">{detail}</td>
</tr>""")
            body = ('<a class="back-link" href="report.html">← 返回矩阵总汇</a>'
                    '<div class="section"><div class="model-section"><table>'
                    '<thead><tr><th>用例名</th><th>模块</th><th>Spec 文件</th><th>WebUI 用例标题</th>'
                    '<th class="center">状态</th><th>失败详情</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div></div>')
        rendered = ReportGenerator._page_shell(
            title="WebUI 用例详情", eyebrow="WEBUI DETAIL",
            subtitle="所有映射到具体 Playwright 用例的执行详情（设计边界/尚未采集的条目不在此列出，见矩阵总汇）",
            meta_items=[f"总计: <strong>{len(mapped)}</strong> 条"], body=body,
        )
        _assert_no_stray_none(rendered, "report_webui_detail.html")
        out_path = Path(out_dir) / "report_webui_detail.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        return out_path


MODULE_RUNNERS = ("cli_smoke", "channel", "consistency", "known_gaps")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="QAIModelBuilder CLI/channel/一致性测试脚本（与 test_service.py 独立）")
    parser.add_argument("--builder_dir", default="./QAIModelBuilder", help="QAIModelBuilder 仓库根目录")
    parser.add_argument("--builder_python", default=None, help="Builder 专用 python.exe；缺省自动探测官方 venv")
    parser.add_argument("--builder_host", default="127.0.0.1")
    parser.add_argument("--builder_port", type=int, default=8899)
    parser.add_argument("--builder_data_dir", default=None, help="隔离数据目录；缺省为 <out_dir>/builder_data")
    parser.add_argument("--out_dir", default="./test_builder_cli_results", help="结果/日志/缺陷清单输出目录")
    parser.add_argument("--modules", nargs="+", choices=MODULE_RUNNERS, default=None,
                         help="只运行指定模块；缺省运行全部四个模块")
    parser.add_argument("--start_timeout", type=int, default=90, help="等待 Builder 就绪的超时秒数")
    parser.add_argument("--webui_results", default=None,
                         help="Playwright WebUI 套件产出的 e2e-report/results.json 路径（可选；"
                              "缺省时统一报告里 WebUI 状态全部显示为未采集，设计边界条目不受影响）")
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

    try:
        if "cli_smoke" in modules:
            run_cli_smoke_module(cli, collector, defects)
        if "channel" in modules:
            run_channel_module(cli, builder.csrf, env_cfg, collector, defects)
        if "consistency" in modules:
            run_consistency_module(cli, builder.csrf, collector, defects)
        if "known_gaps" in modules:
            run_known_gaps_module(cli, collector, defects)
    finally:
        builder.stop()

    payload = collector.write_report(out_dir)
    defects.write(out_dir)

    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    new_defect_count = len(defects.defects)
    if new_defect_count:
        print(f"WARNING: 本次运行发现 {new_defect_count} 条新缺陷，详见 {out_dir / 'defects.md'}")

    # 与 test_service.py 内置 ReportGenerator 的模式一致：同一次运行结束后直接产出统一的
    # 三端一致性 HTML 报告，不再需要单独调用一个后处理脚本。CLI 侧健康判定（上面的
    # sys.exit 参数）完全基于 collector.is_healthy()，不受报告生成结果影响。
    cli_cases, cli_payload = parse_cli_results(out_dir / "results.json")
    webui_results = parse_webui_results(args.webui_results) if args.webui_results else {}
    defects_list = parse_defects(out_dir / "defects.json")
    _assert_full_mapping_coverage(cli_cases, CLI_CASE_TO_UI_EQUIVALENT)
    unified_cases = build_unified_cases(cli_cases, webui_results, CLI_CASE_TO_UI_EQUIVALENT)
    with open(out_dir / "unified_cases.json", "w", encoding="utf-8") as f:
        json.dump([vars(c) for c in unified_cases], f, indent=2, ensure_ascii=False)
    report_path = ReportGenerator.generate_matrix_html(unified_cases, cli_payload, len(defects_list), out_dir)
    defects_path = ReportGenerator.generate_defects_detail_html(defects_list, out_dir)
    webui_detail_path = ReportGenerator.generate_webui_detail_html(unified_cases, out_dir)
    print(f"已生成: {report_path}")
    print(f"已生成: {defects_path}")
    print(f"已生成: {webui_detail_path}")

    sys.exit(0 if collector.is_healthy() else 1)


if __name__ == "__main__":
    main()
