#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import atexit
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import requests

from .common import HAS_PSUTIL, wait_http_ok, wait_port_open

if HAS_PSUTIL:
    import psutil


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


def cleanup_junctions(paths):
    """移除 `mklink /J` 创建的目录联接本身（不触碰联接指向的真实目录）。

    根因（见 docs/known-issues.md 7.1b 根因③）：这些联接把 `<out_dir>/qaimodelbuilder_data/
    {models,bin}/<name>` 指向真实的 `GenieEnv\\models\\<name>`/GenieAPIService 安装目录；
    `<out_dir>` 若未被 `.gitignore` 覆盖，远程同步的 `git clean -fd` 会顺着联接钻进去删除
    真实目录下未被忽略的文件（典型是 config.json）。`.gitignore` 规则是第一道防线（整个
    输出目录被跳过），这里是第二道（纵深防御）：即使某次输出目录意外未被忽略，联接本身
    也不会残留到下一次同步。调用方必须在 `finally` 里无条件调用，覆盖正常/异常两条路径。

    判存在性不能用 `Path.exists()`：Windows 目录联接一旦目标被删除（悬空联接），
    `Path.exists()`/`os.path.exists()` 会沿联接解析到不存在的目标后返回 False，导致悬空
    联接被静默跳过清理——与本函数的清理承诺直接矛盾（已用本地构造的悬空联接验证过这个
    差异）。`os.path.lexists()` 不解析链接本身，联接节点存在就返回 True，无论目标是否存在，
    因此改用它。即使路径根本不存在，下面的 `rmdir` 也只会返回非零退出码，不会抛异常。
    """
    for p in paths:
        try:
            path = Path(p)
            if os.path.lexists(path):
                subprocess.run(["cmd.exe", "/c", "rmdir", str(path)],
                                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


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
        # 本类拉起的 Builder 是隔离的一次性实例(供 skill_capacity builder 模式等测试复用),
        # 不需要任何文件防护;关掉进程内 AI 文件工具护栏与 guard64.dll OS 级钩子,避免其对被
        # 拉起的 GenieAPIService.exe 子进程链路产生任何干扰(protected_paths 只注入 Python
        # 子进程、关不掉,但本就不影响原生 exe)。
        env["QAI_SECURITY__FILE_GUARD_ENABLED"] = "false"
        env["QAI_SECURITY__NATIVE_FILE_GUARD_ENABLED"] = "false"
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
            wait_http_ok(f"{self.base_url}/api/system/health", timeout=timeout, process=self.process)
        except RuntimeError as e:
            stderr_tail = ServiceManager.read_log_tail(self._stderr_log, 50)
            stdout_tail = ServiceManager.read_log_tail(self._stdout_log, 20)
            self._force_kill()
            raise RuntimeError(
                f"QAIModelBuilder 启动失败: {e}; "
                f"stderr_log={self._stderr_log}; stderr_tail={stderr_tail!r}; stdout_tail={stdout_tail!r}"
            )
        # 就绪后立即完成一次 CSRF 握手，确保后续所有非安全方法请求都能带上正确的双提交凭证。
        self.csrf.ensure_csrf_token(timeout=10)

    def health(self):
        try:
            r = self.csrf.get("/api/system/health", timeout=5)
            return r.status_code == 200, r.status_code, r.text[:500]
        except Exception as e:
            return False, 0, repr(e)

    # ---- 技能面板 API 封装（供 skill_capacity builder 模式等真实多轮 agent 测试复用）----
    # 均沿用 health() 同一套风格：返回 (success_bool, status_code, response_json_or_text)，
    # 复用 self.csrf 已完成的 CSRF 双提交握手，不重新发明 HTTP 调用方式；请求异常不抛出，
    # 折算为 (False, 0, repr(e))。真实路由已核实存在于 interfaces/http/routes/user_prefs.py。
    def get_skills_policy(self, timeout=15):
        """GET /api/skills/policy：读取当前技能启用状态（哪些技能启用、当前模式 manual/auto）。"""
        try:
            r = self.csrf.get("/api/skills/policy", timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
        except Exception as e:
            return False, 0, repr(e)

    def set_skills_mode(self, mode, timeout=15):
        """POST /api/skills/set_mode：切换技能启用模式，body={"mode": "manual"|"auto"}。"""
        try:
            r = self.csrf.post("/api/skills/set_mode", json={"mode": mode}, timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
        except Exception as e:
            return False, 0, repr(e)

    def toggle_skill(self, skill_name, enabled, timeout=15):
        """POST /api/skills/toggle：显式启用/禁用指定技能（如 "weather"）。
        字段名已核实（本机只读 clone `interfaces/http/routes/user_prefs.py` 的
        `SkillToggleRequest`）：body={"skill_name": <name>, "enabled": <bool>}。"""
        try:
            r = self.csrf.post("/api/skills/toggle",
                               json={"skill_name": skill_name, "enabled": bool(enabled)}, timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
        except Exception as e:
            return False, 0, repr(e)

    def reload_skills(self, timeout=30):
        """POST /api/skills/reload：触发技能重新发现（无 body，空 JSON 对象）。"""
        try:
            r = self.csrf.post("/api/skills/reload", json={}, timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
        except Exception as e:
            return False, 0, repr(e)

    def set_skill_run_mode(self, skill_id, mode, timeout=15):
        """POST /api/skills/{skill_id}/set_mode：真正决定某技能是否进入聊天 prompt 的接口
        (`frontend/src/stores/skills.ts` 的 Skills 面板用的就是这一个)。mode ∈
        {"off","cloud","local","both"}。

        本轮核实发现的重要 drift：上面 `set_skills_mode()`/`toggle_skill()` 命中的
        `/api/skills/set_mode`（无 id）与 `/api/skills/toggle` 实际是 AI-Coding 能力策略子系统
        （Security > Skill 面板，管全局 auto/manual 模式与只读/写路径白名单），与"某技能是否在
        普通聊天里对模型可见"完全无关；`resolve_skill_mode()`
        (`qai/platform/skills/discovery.py:63`) 是 Skills 面板/Security 面板/聊天 prompt 门控
        共享的唯一解析函数，但显式 `mode` 字段优先级高于 legacy `enabled` 布尔——因此要让本地
        模型场景稳定看到某技能，必须调用这个按 skill_id 走路径参数的端点并传 "local"/"both"，
        不能依赖 `toggle_skill()` 写入的 `enabled` 兜底（那条兜底只会解析成 "cloud"）。"""
        try:
            r = self.csrf.post(f"/api/skills/{skill_id}/set_mode", json={"mode": mode}, timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
        except Exception as e:
            return False, 0, repr(e)

    def list_skills(self, timeout=15):
        """GET /api/skills：v1 风格技能清单（含每个技能已解析出的 `mode`），用于复核
        `set_skill_run_mode()` 调用后目标技能的解析结果，而不是只信任 set_mode 响应的 200。"""
        try:
            r = self.csrf.get("/api/skills", timeout=timeout)
            return r.status_code == 200, r.status_code, (r.json() if r.status_code == 200 else r.text[:500])
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

    def start(self, config_path, extra_args=None, extra_env=None):
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
        env = {**os.environ, **(extra_env or {})}
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
            env=env,
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
