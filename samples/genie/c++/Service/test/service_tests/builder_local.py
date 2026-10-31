#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from .common import TestResult, infer_backend, wait_port_closed
from .managers import cleanup_junctions
from .suite_prompt_fidelity import _PF_PROMPT_BLOCK_RE


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
class ModelDirSnapshot:
    """进入 Builder 联动前对每个被 mklink /J 联接的模型目录拍一份关键小文件快照
    （仅 config.json/genie_config.json/.qai-install.json，不拷权重），退出时
    （含 finally 异常路径）逐一比对是否仍存在且字节相同；缺失或变化即视为
    不可忽略的 crashed 级别失败，绝不静默放过。

    背景：inject_local_models() 用 mklink /J 建立的是目录联接（junction），Windows 对联接
    路径的文件系统操作是透明穿透的——任何针对联接路径（Builder 侧 <data_dir>/models/<name>）
    的安装/更新/删除操作都会直接作用到联接指向的真实目录（--models/<name>）。如果测试过程
    中触发了任何一次这类路径，就会直接改写/删除真实模型目录下的配置文件而不留痕迹。本类只做
    最小侵入的"拍快照 -> 收尾比对"防护：只看几个关键小文件，缺失或内容变化即判定为不可忽略
    的失败，由调用方计入 crashed 级别的 TestResult。"""

    _WATCH_FILES = ("config.json", "genie_config.json", ".qai-install.json")

    def __init__(self):
        # model_dir(Path) -> {filename: sha256_hex_or_None}；None 表示快照时该文件不存在
        # （新出现的文件不列入校验范围，避免误报——见 verify() 的语义）。
        self._snapshots = {}

    @staticmethod
    def _hash_file(path):
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def snapshot(self, model_dirs):
        """对每个 model_dir 下 _WATCH_FILES 中存在的文件计算内容哈希并记录。
        model_dirs 应传入真实模型目录路径（--models/<name>），不是 Builder 侧的联接路径——
        联接对文件系统操作透明穿透，直接对源目录拍照语义等价，且不依赖联接是否仍然存在。"""
        for d in model_dirs:
            d = Path(d)
            files = {}
            for fname in self._WATCH_FILES:
                fp = d / fname
                files[fname] = self._hash_file(fp) if fp.is_file() else None
            self._snapshots[d] = files

    def verify(self):
        """返回违规描述字符串列表，空列表=通过。
        已快照且存在的文件：现在必须仍存在且哈希不变，否则记一条违规。
        已快照但当时不存在的文件：不做要求（新出现的文件不列入校验范围，避免误报）。"""
        violations = []
        for d, files in self._snapshots.items():
            for fname, old_hash in files.items():
                if old_hash is None:
                    continue  # 快照时不存在，不校验（避免对新出现的文件误报）
                fp = d / fname
                if not fp.is_file():
                    violations.append(
                        f"模型目录 {d} 下的 {fname} 已丢失（快照时存在，现在不存在）")
                    continue
                new_hash = self._hash_file(fp)
                if new_hash != old_hash:
                    violations.append(
                        f"模型目录 {d} 下的 {fname} 内容已变化（快照 sha256={old_hash}, "
                        f"当前 sha256={new_hash}）")
        return violations


class _BuilderLogProbe:
    """通过 Builder 官方 `GET /api/service/logs` SSE 接口做「本次请求新增日志」取证通道
    （Step6 新增，供场景 A/B/C 复用）。

    Builder 自己代理转发的聊天请求不经过我们直连测试时使用的本机日志文件路径——Builder 的
    `ProcessBackedInferenceService` 只把子进程 `GenieAPIService.exe` 的 stdout 读进内存 deque，
    不落盘文件——因此走 Builder 代理链路时 `_PromptLogProbe` 的「按文件字节偏移量增量读取」方式
    失效，必须换成这条 Builder 自带的 SSE 通道；`mark()`/`read_new()` 语义与 `_PromptLogProbe`
    对齐，只是用「先清空缓冲区」代替「记偏移量」（`GET /api/service/logs` 本身不支持按偏移量
    做增量读取，只能整体清空重来）。"""

    def __init__(self, csrf_session):
        self.csrf = csrf_session
        self._skip_from = 0
        self._tail_thread = None
        self._tail_lines = []
        self._tail_stop = None
        self._tail_resp = None

    def mark(self):
        """`POST /api/service/logs/clear`：清空 Builder 侧保留的 GenieAPIService stdout 缓冲区，
        之后 `read_new()` 读到的都是本次 `mark()` 之后新产生的日志，避免上一场景埋的 canary/
        `[Prompt]` 块污染下一场景的断言（同一个 Builder 代理子进程、同一份内存缓冲区）。"""
        try:
            r = self.csrf.post("/api/service/logs/clear", json={}, timeout=15)
            self._skip_from = int(r.json().get("skip_from", 0)) if r.status_code == 200 else 0
        except Exception:
            self._skip_from = 0

    def start_live_tail(self):
        """Step7 远程实测新增：与 `read_new()`（事后一次性抓取）不同，本方法在动作发起**之前**
        就开一个后台线程持续消费 `GET /api/service/logs` 的 SSE 流并逐行累积到内存列表。

        根因背景：`ProcessBackedInferenceService._log_buffer` 是 `deque(maxlen=6000)`（行数
        上限，非字符数），带图的多模态请求单次即可产生远超此深度的日志行——`IEmbedding::
        BuildPrompt()` 用 `My_Log(completed_prompt.c_str(), kInfo)` 把整份含 skill 目录/对话
        历史/System Prompt 的完整提示词当一次调用打印，其内部每个 `\n` 都会在 Python 侧
        `proc.stdout.readline()` 逐行读取时变成 deque 里独立的一"行"；只要这份提示词加上
        模型自身逐 token 生成日志的总行数超过 6000，`read_new()`（生成完全结束后才发起一次
        新连接抓取）读到的必然是"deque 已经回卷过的、只剩最新尾部"的残缺历史，早段（往往正是
        canary 所在处）已被回卷丢弃——这不是模型/取证正则的 bug，是"事后抓取"这种取证方式本身
        对深度有限的滚动缓冲区结构性不适配。改为"动作发起前就开始监听"从根本上规避这个问题：
        只要消费速度不明显慢于产生速度，我们自己在内存里攒的列表就不受 Builder 侧 deque 容量
        限制。"""
        self._tail_lines = []
        self._tail_stop = threading.Event()

        def _worker():
            resp = None
            try:
                resp = self.csrf.request(
                    "GET", "/api/service/logs", timeout=(10, 5), stream=True,
                    params={"skip": self._skip_from})
                self._tail_resp = resp
                if resp.status_code != 200:
                    return
                for raw in resp.iter_lines(decode_unicode=True):
                    if self._tail_stop.is_set():
                        break
                    if not raw or not raw.startswith("data:"):
                        continue
                    payload = raw[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and "line" in obj:
                        self._tail_lines.append(str(obj["line"]))
                    elif isinstance(obj, dict) and "error" in obj:
                        break
            except Exception:
                pass
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

        self._tail_thread = threading.Thread(target=_worker, daemon=True)
        self._tail_thread.start()
        # 给后台线程留出真正建立好 SSE 连接的时间，避免调用方紧接着发起的动作在
        # 连接就绪前就已经写日志，产生一段"抢跑"的取证盲区。
        time.sleep(0.5)

    def stop_live_tail(self, grace=5.0):
        """停止 `start_live_tail()` 开的后台监听线程并关闭连接，返回累积到的全部日志文本
        （用 `"\\n"` 拼接，与 `read_new()` 返回值同构，调用方可直接互换使用）。"""
        if self._tail_stop is not None:
            self._tail_stop.set()
        if self._tail_resp is not None:
            try:
                self._tail_resp.close()
            except Exception:
                pass
        if self._tail_thread is not None:
            self._tail_thread.join(timeout=grace)
        return "\n".join(self._tail_lines)

    def read_new(self, idle_timeout=10.0, total_timeout=30.0):
        """连接 `GET /api/service/logs?skip=<mark 时的 skip_from>`，收集新增行直到看到
        `[DONE]` 帧、空闲超时或总超时。返回拼接后的全部行文本（best-effort，连接/超时异常
        均静默吞掉，调用方以「抓不到就判 skip/降级」处理，不抛异常打断场景流程）。"""
        lines = []
        deadline = time.time() + total_timeout
        resp = None
        try:
            resp = self.csrf.request(
                "GET", "/api/service/logs", timeout=(10, idle_timeout), stream=True,
                params={"skip": self._skip_from})
            if resp.status_code != 200:
                return ""
            for raw in resp.iter_lines(decode_unicode=True):
                if time.time() >= deadline:
                    break
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if isinstance(obj, dict) and "line" in obj:
                    lines.append(str(obj["line"]))
                elif isinstance(obj, dict) and "error" in obj:
                    break
        except Exception:
            pass
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        return "\n".join(lines)

    def last_prompt_block(self):
        """本次 `mark()` 之后产生的最后一个最终提示词块；抓不到返回空字符串（调用方据此判
        skip/降级，与 `_PromptLogProbe.last_prompt_block()` 语义一致，复用同一条正则）。"""
        blocks = _PF_PROMPT_BLOCK_RE.findall(self.read_new())
        return blocks[-1] if blocks else ""


class QAIModelBuilderLocalModelTester:
    _MODEL_PLACEHOLDER = "_builder_local_model_"
    # backend → format 字段值（GET /api/service/models 的判定），infer_backend 用 "GGUF" 表示
    # GGUF 后端，Builder /api/service/models 返回的是小写 "gguf"，注意这里的映射。
    _BACKEND_TO_FORMAT = {"qnn": "qnn", "mnn": "mnn", "GGUF": "gguf"}

    def __init__(self, builder, models_root, genie_root_path, model_names,
                 genie_service_port, round_num=1, data_dir=None):
        self.builder = builder
        self.models_root = Path(models_root) if models_root else None
        self.genie_root_path = genie_root_path
        self.model_names = list(model_names)
        self.genie_service_port = genie_service_port
        self.round_num = round_num
        self.data_dir = data_dir
        self.results = []
        self.crash_events = []
        # configure_genie_root() 里 mklink /J 联接的 <data_dir>/bin/<name> 路径（当前无读取方，
        # 原消费方 test_invalid_genie_root() 已随过时的 builder_local_model 套件一并删除，
        # 见 test_builder_agent_tasks.py 的真实多轮 agent 测试；此字段保留供未来同类用例复用）。
        self._bin_junction_path = None
        # 本次创建的全部 mklink /J 联接目标（bin + models），cleanup_junctions() 消费；
        # 见该方法文档字符串，必须在 finally 里无条件调用，否则会复现 docs/known-issues.md
        # 7.1b 根因③（残留联接被远程同步 git clean -fd 顺着删除真实目录下的文件）。
        self._junction_paths = []
        # 模型目录安全网：inject_local_models() 成功后拍快照，_sc_case_builder_e2e() 的
        # finally 块无论用例成败都会调用 verify()，防止 mklink /J 联接被 Builder 安装/更新/
        # 删除路径透明穿透地误改/误删真实模型目录下的配置文件。
        self.model_dir_snapshot = ModelDirSnapshot()

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
        self._junction_paths.append(dst)
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
        snapshot_targets = []  # 联接成功（含幂等已存在）的真实模型目录，供 ModelDirSnapshot.snapshot() 使用
        for m in self.model_names:
            src = self.models_root / m
            if not src.exists():
                failures.append(f"{m}(源目录不存在: {src})")
                continue
            dst = target_models_root / m
            self._junction_paths.append(dst)
            if dst.exists():
                successes.append(f"{m}(目标已存在，视作幂等成功)")
                snapshot_targets.append(src)
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
            snapshot_targets.append(src)

        # 模型目录安全网：联接一旦建立（无论新建还是幂等复用），Windows 对该联接路径的任何
        # 文件系统操作都会透明穿透到 src（真实模型目录）——此处对 src 而非 dst 拍照，
        # 语义上等价且不依赖联接本身是否在收尾时仍然存在。
        if snapshot_targets:
            self.model_dir_snapshot.snapshot(snapshot_targets)

        detail = (f"注入 {len(successes)}/{len(self.model_names)}; "
                  f"successes={successes}; failures={failures}; target={target_models_root}")
        passed = bool(successes) and not failures
        self.results.append(self._make_result(
            name, passed, 0, detail,
            response_data={"target_models_root": str(target_models_root),
                           "successes": successes, "failures": failures}))
        return bool(successes)

    def cleanup_junctions(self):
        """收尾：移除本实例 configure_genie_root()/inject_local_models() 创建的全部
        联接（委托模块级 cleanup_junctions()）。调用方须在 finally 里无条件调用。"""
        cleanup_junctions(self._junction_paths)

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
        且 model 匹配（该接口是 fire-and-forget，不阻塞等待）。"""
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
