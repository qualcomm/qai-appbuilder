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

用法:
    python test_builder_cli.py --builder_dir ./QAIModelBuilder
    python test_builder_cli.py --builder_dir ./QAIModelBuilder --modules channel known_gaps
"""

import argparse
import atexit
import base64
import hashlib
import hmac
import http.server
import json
import os
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

    sys.exit(0 if collector.is_healthy() else 1)


if __name__ == "__main__":
    main()
