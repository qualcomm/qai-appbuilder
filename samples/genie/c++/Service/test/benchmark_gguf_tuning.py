#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轻量级 GGUF 配置调优基准测试脚本。

直连 GenieAPIService（不经 QAIModelBuilder），对比不同配置维度下的真实出词速度
（prefill/decode tok/s，从服务日志 [LLAMACpp] ... Query DONE 行解析，不依赖客户端
侧字符数估算）与回答质量（正则/关键词/数值判定，不依赖主观判断），并启发式检测模型
"消极怠工"（用元描述短语代替真正执行/产出，见 detect_slacking_off()）。

可控维度：
  - config.json 的 dialog.context.size（上下文大小）
  - config.json 顶层 device 字段（gpu/cpu）
  - config.json 的 draft_model 块是否存在（是否启用投机解码）
  - 进程级 -t/--enable_thinking CLI flag（思考模式开关；不是 config.json 字段）

设计决策与踩坑见同名 benchmark_gguf_tuning.py.notes.md。

运行环境假设：本脚本运行在 GenieAPIService 实际所在的机器上（与 test_service.py/
test_builder_agent_tasks.py 相同的约定），直接管理 GenieAPIService.exe 子进程。

用法示例：
  # 冒烟测试（不改动 config.json，验证脚本本身可用）：
  python benchmark_gguf_tuning.py --exe_dir <exe目录> --models_root <models根目录> ^
      --model_name gpt-oss-20b-GGUF --smoke

  # 跑内置预设对比（Step 6 场景）：
  python benchmark_gguf_tuning.py --exe_dir <exe目录> --models_root <models根目录> ^
      --model_name qwen3.8-27b-q4_0 --budget_seconds 2400 --presets all

  # 单个自定义配置（Step 6 按需追加）：
  python benchmark_gguf_tuning.py --exe_dir <exe目录> --models_root <models根目录> ^
      --model_name qwen3.8-27b-q4_0 --single --context_size 16384 --device gpu
"""
import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Callable, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import ServiceManager, wait_port_open, wait_http_ok, wait_port_closed  # noqa: E402
from test_builder_agent_tasks import RepetitionWatchdog  # noqa: E402


# ============================================================================
# 题目与判定规则（本阶段确定，Step 6 直接复用，不再重新设计）
# 判定规则均为确定性规则（正则/关键词/数值提取），不依赖主观评判。
# ============================================================================

JudgeFn = Callable[[str], Tuple[bool, str]]


def _judge_length_range(lo: int, hi: int) -> JudgeFn:
    def _f(text: str):
        n = len(text)
        return lo <= n <= hi, f"length={n} (期望 {lo}-{hi})"
    return _f


def _judge_regex_all(patterns: List[str]) -> JudgeFn:
    compiled = [re.compile(p) for p in patterns]

    def _f(text: str):
        missing = [p.pattern for p in compiled if not p.search(text)]
        return (not missing), (f"missing_patterns={missing}" if missing else "全部命中")
    return _f


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _judge_english_translation(keywords: List[str]) -> JudgeFn:
    def _f(text: str):
        has_cjk = bool(_CJK_RE.search(text))
        lower = text.lower()
        missing = [k for k in keywords if k.lower() not in lower]
        ok = (not has_cjk) and (not missing)
        return ok, f"has_cjk={has_cjk}, missing_keywords={missing}"
    return _f


def _judge_integer_equals(expected: int) -> JudgeFn:
    def _f(text: str):
        nums = re.findall(r"-?\d+", text)
        ok = str(expected) in nums
        return ok, f"found_integers={nums}, expected={expected}"
    return _f


# ============================================================================
# 模型"消极怠工"（lazy / refusal-to-execute）启发式检测
#
# 业界背景（联网调研，完整依据见同名 .notes.md）：OpenAI 2023 年底官方承认过
# "GPT-4 Turbo laziness"，模型用占位注释/描述代替真正执行，随后发模型更新专门
# 缓解；常见外在表现是用元描述短语（"你可以这样"/"建议你"/"步骤如下"/
# "the rest of the code here"等）替代真正的实质产出。本检测只做轻量启发式：
# 命中元描述短语 且 产出长度明显低于预期阈值，才判定为疑似消极怠工——单独出现
# 元描述短语不足以判定（正常长文本里也可能偶然出现类似连接词），必须同时长度
# 不足，避免对正常回答产生误报。
# ============================================================================

_LAZY_META_PHRASES = [
    r"你可以这样", r"你可以尝试", r"你可以参考", r"建议你", r"思路如下", r"大纲如下",
    r"步骤如下", r"示例思路", r"此处省略", r"类似实现", r"其余.{0,6}类似", r"具体实现省略",
    r"\bTODO\b", r"the rest of (the|your)", r"placeholder", r"here'?s how you (could|can)",
    r"here'?s a (general|rough) (approach|outline)", r"I(?:'ll| will) outline",
    r"you can (do this|achieve this) by",
]
_LAZY_META_RE = [re.compile(p, re.IGNORECASE) for p in _LAZY_META_PHRASES]


def detect_slacking_off(text: str, min_substantive_len: int = 80) -> Optional[str]:
    """命中元描述短语且产出长度 < min_substantive_len 才判定为疑似消极怠工，
    避免对正常长度的实质回答（可能偶然包含类似连接词）产生误报。"""
    if len(text) >= min_substantive_len:
        return None
    hits = [p.pattern for p in _LAZY_META_RE if p.search(text)]
    if not hits:
        return None
    return f"长度仅{len(text)}字（期望>={min_substantive_len}），命中元描述短语{hits}"


@dataclass
class Question:
    qid: str
    category: str  # "speed" | "quality"
    prompt: str
    judge: JudgeFn
    description: str = ""
    lazy_check_min_len: int = 80


QUESTION_BANK: List[Question] = [
    Question(
        qid="speed_photosynthesis",
        category="speed",
        prompt="请用大约150到200个汉字，介绍光合作用的基本原理和主要过程。只输出说明文正文本身，"
               "不要输出标题或列表符号。",
        judge=_judge_length_range(60, 500),
        description="固定长度说明文生成，主要用于测速；长度判定故意放宽，避免因思考模式/模型个体"
                    "差异误杀——速度数据以服务日志 gen_rate 为准，长度只是基本可用性兜底。",
    ),
    Question(
        qid="speed_largest_countries",
        category="speed",
        prompt="请列出世界上面积最大的5个国家（按面积从大到小排列），每个国家后面附上大致面积"
               "（平方公里），每个国家一行，不要输出其它内容。",
        judge=_judge_regex_all([r"俄罗斯", r"加拿大", r"中国", r"美国", r"巴西"]),
        description="固定条目生成，兼测速与基本事实正确性（面积前5大国家是确定事实）。",
    ),
    Question(
        qid="quality_math_reasoning",
        category="quality",
        prompt="一个水果篮里苹果和橘子共23个，苹果比橘子多7个。苹果和橘子各有多少个？只输出最终"
               "答案，格式严格为：苹果=X，橘子=Y（X、Y用阿拉伯数字）。",
        judge=_judge_regex_all([r"苹果\s*=\s*15", r"橘子\s*=\s*8"]),
        description="二元一次方程推理题，答案固定唯一（15/8），测推理能力。",
    ),
    Question(
        qid="quality_instruction_translate",
        category="quality",
        prompt="请将下面这句话翻译成英文，只输出翻译结果本身，不要输出任何解释、引号或其它内容：\n"
               "人工智能正在改变世界。",
        judge=_judge_english_translation(["artificial intelligence", "world"]),
        description="指令遵循（只输出译文本身，不含中文/解释）+ 基本翻译正确性，测指令遵循能力。",
    ),
    Question(
        qid="quality_code_understanding",
        category="quality",
        prompt=(
            "阅读下面的 Python 函数：\n"
            "def f(n):\n"
            "    total = 0\n"
            "    for i in range(1, n):\n"
            "        total += i\n"
            "    return total\n"
            "调用 f(5) 的返回值是多少？只输出一个整数，不要输出任何其它文字。"
        ),
        judge=_judge_integer_equals(10),
        description="range(1,5)=1,2,3,4 求和=10，测代码理解能力，答案唯一确定。",
    ),
    Question(
        qid="quality_direct_execution_no_steps",
        category="quality",
        prompt="请直接输出一段不少于120个汉字的中文短文正文，主题是水循环的基本过程。严格要求："
               "直接给出最终短文正文本身，禁止输出写作思路、大纲、步骤说明，或任何类似"
               "“你可以这样写”“建议你”的元描述性文字——只允许出现短文正文内容本身。",
        judge=_judge_length_range(60, 500),
        lazy_check_min_len=120,
        description="专门用于触发/验证消极怠工检测：若模型用元描述短语代替真正的短文正文"
                    "（而非给出完整内容），detect_slacking_off() 应标记为疑似消极怠工；"
                    "若模型遵从指令直接给出正文，该检测不应误报。",
    ),
]


def get_questions(categories: Optional[List[str]] = None,
                   qids: Optional[List[str]] = None) -> List[Question]:
    qs = QUESTION_BANK
    if categories:
        qs = [q for q in qs if q.category in categories]
    if qids:
        wanted = set(qids)
        qs = [q for q in qs if q.qid in wanted]
    return qs


# ============================================================================
# 服务日志解析：从 ServiceManager._stdout_log 的字节窗口里找 Query DONE 行
# ============================================================================

_FIELD_PATTERNS = {
    "total_ms": re.compile(r"total_ms=(\d+)"),
    "prefill_tokens": re.compile(r"prefill_tokens=(\d+)"),
    "gen_tokens": re.compile(r"gen_tokens=(\d+)"),
    "gen_rate": re.compile(r"gen_rate=([\d.]+)\s*tok/s"),
    "draft_proposed": re.compile(r"draft_proposed=(\d+)"),
    "draft_accepted": re.compile(r"draft_accepted=(\d+)"),
}


def _read_log_window(svc: "ServiceManager", offset: int) -> str:
    log_path = getattr(svc, "_stdout_log", None)
    if log_path is None:
        return ""
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_query_done(log_window: str) -> Optional[dict]:
    """在日志窗口里找 [LLAMACpp] ... Query DONE 行并逐字段解析。
    用独立正则逐字段提取而非一个大正则整体匹配，容忍 llama_perf 部分（无投机解码时
    该部分格式不同）的格式漂移；窗口内可能出现多条（如内部重试），取最后一条。"""
    candidates = [l for l in log_window.splitlines() if "[LLAMACpp]" in l and "Query DONE" in l]
    if not candidates:
        return None
    line = candidates[-1]
    out = {"raw_line": line.strip()}
    for key, rx in _FIELD_PATTERNS.items():
        m = rx.search(line)
        if m is None:
            out[key] = None
        elif key == "gen_rate":
            out[key] = float(m.group(1))
        else:
            out[key] = int(m.group(1))
    return out


# ============================================================================
# config.json 可控维度编辑（基于备份文件的幂等 patch，可回滚）
# ============================================================================

class ModelConfigEditor:
    """对模型目录下的单模型配置文件做可回滚的字段级修改。

    只覆盖本任务已核实为"现成可配置、不需要改代码"的三个维度：
      - dialog.context.size（model_manager.cpp ParseContextSizeFromConfigJson）
      - 顶层 device（model_manager.cpp ParseBackendDeviceFromConfigJson）
      - draft_model 块存在与否（llama_speculative::DraftModelConfig::ParseFrom）
    思考模式不走这里——它是进程级 CLI flag（-t/--enable_thinking），由
    BenchmarkRunner 通过 ServiceManager 的 extra_args 控制，不在配置文件内。

    每次 apply() 都从"备份的原始文件"重新计算，不是在上一次 patch 结果上累加，
    保证各配置变体互相独立、不会残留上一个变体的修改。
    """

    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        # genie_config.json 优先于 config.json（File::ResolveModelConfigPath 的三端一致
        # 规则），这里必须复用同一优先级，否则写错文件会被服务端静默忽略。
        genie_cfg = self.model_dir / "genie_config.json"
        self.config_path = genie_cfg if genie_cfg.exists() else self.model_dir / "config.json"
        self.backup_path = self.config_path.with_name(self.config_path.name + ".bench_orig_backup")

    def backup(self):
        if not self.backup_path.exists():
            shutil.copyfile(self.config_path, self.backup_path)

    def restore(self):
        if self.backup_path.exists():
            shutil.copyfile(self.backup_path, self.config_path)

    def discard_backup(self):
        if self.backup_path.exists():
            self.backup_path.unlink()

    def _load_base(self) -> dict:
        src = self.backup_path if self.backup_path.exists() else self.config_path
        with open(src, "r", encoding="utf-8") as f:
            return json.load(f)

    def apply(self, context_size: Optional[int] = None, device: Optional[str] = None,
              draft_model_enabled: Optional[bool] = None) -> dict:
        base = self._load_base()
        if context_size is not None:
            base.setdefault("dialog", {}).setdefault("context", {})["size"] = int(context_size)
        if device is not None:
            base["device"] = device
        if draft_model_enabled is not None:
            if draft_model_enabled:
                if "draft_model" not in base and "_draft_model_disabled" in base:
                    base["draft_model"] = base.pop("_draft_model_disabled")
            else:
                if "draft_model" in base:
                    base["_draft_model_disabled"] = base.pop("draft_model")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=4, ensure_ascii=False)
        return base


# ============================================================================
# 基准配置 / 问题结果 / 配置结果
# ============================================================================

@dataclass
class BenchmarkConfig:
    name: str
    context_size: Optional[int] = None
    device: Optional[str] = None
    draft_model_enabled: Optional[bool] = None
    enable_thinking: bool = False
    note: str = ""


@dataclass
class QuestionResult:
    qid: str
    category: str
    passed: Optional[bool]
    judge_detail: str
    ttft_ms: Optional[float]
    client_total_ms: Optional[float]
    gen_rate: Optional[float]
    prefill_tokens: Optional[int]
    gen_tokens: Optional[int]
    draft_proposed: Optional[int]
    draft_accepted: Optional[int]
    watchdog_hit: Optional[dict]
    error: Optional[str]
    answer_preview: str
    slacking_off_hit: Optional[str] = None


@dataclass
class ConfigResult:
    config: BenchmarkConfig
    started: bool
    start_error: Optional[str] = None
    questions: List[QuestionResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: Optional[str] = None
    wall_seconds: float = 0.0


# ============================================================================
# 墙钟预算
# ============================================================================

class Deadline:
    def __init__(self, budget_seconds: Optional[float]):
        self.start = time.time()
        self.budget = budget_seconds

    def remaining(self) -> Optional[float]:
        if self.budget is None:
            return None
        return self.budget - (time.time() - self.start)

    def expired(self) -> bool:
        r = self.remaining()
        return r is not None and r <= 0


# ============================================================================
# 核心驱动
# ============================================================================

class BenchmarkRunner:
    def __init__(self, exe_dir, model_name, model_dir, host, port, out_dir,
                 per_question_timeout=180, idle_timeout=45):
        self.exe_dir = Path(exe_dir)
        self.model_name = model_name
        self.model_dir = Path(model_dir)
        self.host = host
        self.port = port
        self.out_dir = Path(out_dir)
        self.per_question_timeout = per_question_timeout
        self.idle_timeout = idle_timeout
        self.base_url = f"http://{host}:{port}"
        self.editor = ModelConfigEditor(self.model_dir)

    def _start_service(self, bconfig: BenchmarkConfig) -> ServiceManager:
        wait_port_closed(self.host, self.port, timeout=15)
        svc = ServiceManager(str(self.exe_dir), self.host, self.port)
        svc._log_dir = str(self.out_dir / "service_logs" / bconfig.name)
        extra_args = ["-t"] if bconfig.enable_thinking else None
        svc.start(str(self.editor.config_path), extra_args=extra_args)
        # -l 使模型加载在 HTTP 服务开始监听前同步完成（见 test_service.py 既有用法），
        # 端口可连接后即可直接发请求，不需要再轮询 /v1/models 等待"注册"。
        if not wait_port_open(self.host, self.port, timeout=180, process=svc.process):
            raise RuntimeError(f"端口 {self.host}:{self.port} 在 180s 内未就绪")
        wait_http_ok(f"{self.base_url}/v1/models", timeout=60, process=svc.process)
        return svc

    def _ask(self, svc: ServiceManager, question: Question) -> QuestionResult:
        log_path = getattr(svc, "_stdout_log", None)
        offset = 0
        if log_path is not None:
            try:
                offset = Path(log_path).stat().st_size
            except Exception:
                offset = 0

        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": question.prompt}],
            "stream": True,
        }
        watchdog = RepetitionWatchdog()
        full_text = ""
        ttft_ms = None
        start = time.time()
        error = None
        watchdog_hit = None
        got_done = False
        r = None
        try:
            # timeout=(connect, read)：read 侧即两个 chunk 之间允许的最大静默时间，
            # 不是整条流的总时限——这样"长时间无响应"能在 idle_timeout 内被 requests
            # 自己的 ReadTimeout 捕获，不需要额外起一个监控线程。
            r = requests.post(f"{self.base_url}/v1/chat/completions", json=body,
                               stream=True, timeout=(10, self.idle_timeout))
            # text/event-stream 响应头通常不带 charset，requests 对 text/* 默认退回
            # ISO-8859-1（见 requests.utils.get_encoding_from_headers），decode_unicode=True
            # 会据此把 UTF-8 字节误读成 Latin-1 再转 str，导致中文内容乱码（曾在冒烟测试里
            # 实测复现：双重编码乱码直接导致正则 judge 对 "苹果=15" 这类正确答案判定失败）；
            # 必须显式声明实际编码。
            r.encoding = "utf-8"
            if r.status_code != 200:
                error = f"HTTP {r.status_code}: {r.text[:300]}"
            else:
                for line in r.iter_lines(decode_unicode=True):
                    now = time.time()
                    if now - start > self.per_question_timeout:
                        error = f"单题总时限 {self.per_question_timeout}s 已到，主动截断（不空等）"
                        break
                    if line is None:
                        continue
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        got_done = True
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content", "")
                    if content:
                        if ttft_ms is None:
                            ttft_ms = (now - start) * 1000
                        full_text += content
                        verdict = watchdog.feed(content)
                        if verdict:
                            watchdog_hit = verdict
                            error = f"RepetitionWatchdog 命中（连续重复/消极怠工），立即终止: {verdict}"
                            break
        except requests.exceptions.RequestException as e:
            error = f"{type(e).__name__}: {e}"
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            if error is not None:
                # 主动截断：命中异常/watchdog/超时时都显式发 /stop，不依赖服务端自己收尾，
                # 避免未结束的生成拖慢下一题/下一配置。
                try:
                    requests.post(f"{self.base_url}/stop", json={"text": "stop"}, timeout=15)
                except Exception:
                    pass

        client_total_ms = (time.time() - start) * 1000
        log_window = _read_log_window(svc, offset)
        parsed = parse_query_done(log_window) or {}

        passed = None
        judge_detail = "未判定（请求异常/被中断，不计入有效数据）"
        slacking_hit = None
        if error is None and got_done:
            slacking_hit = detect_slacking_off(full_text, question.lazy_check_min_len)
            if slacking_hit:
                error = f"疑似消极怠工（元描述短语+内容不足），标记为跑偏: {slacking_hit}"
                judge_detail = f"未判定（疑似消极怠工，不计入有效数据）: {slacking_hit}"
            else:
                passed, judge_detail = question.judge(full_text)

        return QuestionResult(
            qid=question.qid, category=question.category, passed=passed,
            judge_detail=judge_detail, ttft_ms=ttft_ms, client_total_ms=client_total_ms,
            gen_rate=parsed.get("gen_rate"), prefill_tokens=parsed.get("prefill_tokens"),
            gen_tokens=parsed.get("gen_tokens"), draft_proposed=parsed.get("draft_proposed"),
            draft_accepted=parsed.get("draft_accepted"), watchdog_hit=watchdog_hit,
            error=error, answer_preview=full_text[:200], slacking_off_hit=slacking_hit,
        )

    def run_config(self, bconfig: BenchmarkConfig, questions: List[Question],
                   deadline: Deadline, patch_config: bool = True) -> ConfigResult:
        t0 = time.time()
        result = ConfigResult(config=bconfig, started=False)

        if patch_config:
            self.editor.backup()
            self.editor.apply(context_size=bconfig.context_size, device=bconfig.device,
                               draft_model_enabled=bconfig.draft_model_enabled)

        svc = None
        try:
            svc = self._start_service(bconfig)
            result.started = True
        except Exception as e:
            result.start_error = f"{type(e).__name__}: {e}"
            result.wall_seconds = time.time() - t0
            return result

        try:
            for q in questions:
                if deadline.expired():
                    result.abort_reason = "墙钟预算耗尽，提前停止（剩余题目未测，不外推结论）"
                    break
                qr = self._ask(svc, q)
                result.questions.append(qr)
                if qr.error is not None:
                    result.aborted = True
                    result.abort_reason = (f"题目 {q.qid} 出现异常/重复/跑偏，"
                                            f"立即终止当前配置: {qr.error}")
                    break
        finally:
            if svc is not None:
                svc.stop()
        result.wall_seconds = time.time() - t0
        return result

    def restore_config(self):
        self.editor.restore()


# ============================================================================
# 预设配置（Step 6 价值优先级：上下文大小 > 思考模式 > 其它参数，见 user_plan）
# ============================================================================

def default_presets() -> List[BenchmarkConfig]:
    return [
        BenchmarkConfig(name="ctx8192_gpu", context_size=8192, device="gpu",
                         note="当前默认值 + GPU（SystemPartitionCommitLimitPercentage=80 已生效时的路径）"),
        BenchmarkConfig(name="ctx32768_gpu", context_size=32768, device="gpu",
                         note="投机分支原硬编码上限，对比更大上下文的吞吐/质量变化"),
        BenchmarkConfig(name="ctx8192_cpu", context_size=8192, device="cpu",
                         note="CPU 优雅降级路径（即使 GPU 可用也强制走 CPU 对比）"),
        BenchmarkConfig(name="ctx8192_gpu_think_on", context_size=8192, device="gpu",
                         enable_thinking=True, note="开启思考模式（-t），对比质量/速度变化"),
        BenchmarkConfig(name="ctx8192_gpu_nodraft", context_size=8192, device="gpu",
                         draft_model_enabled=False, note="关闭投机解码，测单模型基线速度"),
    ]


_PRESET_MAP = {p.name: p for p in default_presets()}


# ============================================================================
# 报告输出
# ============================================================================

def write_report(results: List[ConfigResult], out_dir: Path, cmdline: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"benchmark_report_{ts}.json"
    md_path = out_dir / f"benchmark_summary_{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"cmdline": cmdline, "results": [asdict(r) for r in results]},
                   f, indent=2, ensure_ascii=False)

    lines = [f"# GGUF 配置调优基准报告 ({ts})", "", f"命令行: `{cmdline}`", ""]
    for r in results:
        lines.append(f"## 配置: {r.config.name}")
        lines.append(f"- context_size={r.config.context_size} device={r.config.device} "
                      f"draft_model_enabled={r.config.draft_model_enabled} "
                      f"enable_thinking={r.config.enable_thinking}")
        if r.config.note:
            lines.append(f"- 备注: {r.config.note}")
        if not r.started:
            lines.append(f"- **启动失败**: {r.start_error}")
            lines.append("")
            continue
        if r.abort_reason:
            lines.append(f"- 提前终止: {r.abort_reason}")
        lines.append(f"- 墙钟耗时: {r.wall_seconds:.1f}s")
        lines.append("")
        lines.append("| qid | category | passed | gen_rate(tok/s) | ttft(ms) | prefill_tokens | "
                      "gen_tokens | draft_accept/proposed | error |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for q in r.questions:
            accept = (f"{q.draft_accepted}/{q.draft_proposed}"
                      if q.draft_proposed is not None else "n/a")
            ttft = round(q.ttft_ms, 1) if q.ttft_ms is not None else None
            lines.append(
                f"| {q.qid} | {q.category} | {q.passed} | {q.gen_rate} | {ttft} | "
                f"{q.prefill_tokens} | {q.gen_tokens} | {accept} | {q.error or ''} |"
            )
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[REPORT] JSON: {json_path}")
    print(f"[REPORT] Markdown: {md_path}")
    return json_path, md_path


# ============================================================================
# main
# ============================================================================

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="轻量级 GGUF 配置调优基准测试脚本（直连 GenieAPIService）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--exe_dir", required=True, help="GenieAPIService 安装目录（含 GenieAPIService.exe）")
    parser.add_argument("--models_root", required=True, help="模型根目录（<models_root>/<model_name>/config.json）")
    parser.add_argument("--model_name", required=True, help="要测试的模型目录名")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8933)
    parser.add_argument("--out_dir", default=None,
                         help="结果输出目录，默认 test_results_benchmark_gguf/<timestamp>")
    parser.add_argument("--budget_seconds", type=float, default=None,
                         help="总墙钟预算（秒）；默认不限（仅本脚本自身调试用，Step 6 应显式传入 2400）")
    parser.add_argument("--per_question_timeout", type=int, default=180, help="单题总时限（秒）")
    parser.add_argument("--idle_timeout", type=int, default=45,
                         help="两次 SSE chunk 之间允许的最大静默秒数（超过视为卡死，立即终止该题）")

    parser.add_argument("--smoke", action="store_true",
                         help="冒烟模式：不改动 config.json，用模型现有配置跑一小部分题目，"
                              "用于验证脚本本身（tok/s 解析、判定规则、watchdog）是否工作正常")
    parser.add_argument("--presets", default="all",
                         help=f"要运行的内置预设名（逗号分隔）或 all；可选值: "
                              f"{','.join(_PRESET_MAP.keys())}")
    parser.add_argument("--list_presets", action="store_true", help="列出内置预设后退出")

    parser.add_argument("--single", action="store_true",
                         help="只运行一个由下面参数指定的自定义配置，忽略 --presets")
    parser.add_argument("--name", default="custom", help="--single 模式下该配置的名字")
    parser.add_argument("--context_size", type=int, default=None)
    parser.add_argument("--device", default=None, choices=["gpu", "cpu"])
    parser.add_argument("--draft_model", default=None, choices=["on", "off"],
                         help="是否启用 draft_model 块（投机解码）")
    parser.add_argument("--enable_thinking", action="store_true")

    parser.add_argument("--categories", default=None,
                         help="只跑指定题目类别（逗号分隔：speed,quality），默认全跑")
    parser.add_argument("--no_restore", action="store_true",
                         help="结束后不恢复 config.json 原始内容（Step 6 最终写回推荐配置时使用）")
    parser.add_argument("--restore_only", action="store_true",
                         help="只执行一次 config.json 恢复（从备份文件还原）后退出，不跑任何测试；"
                              "用于清理上一次异常中断遗留的已 patch 状态")
    return parser


def main():
    args = build_arg_parser().parse_args()

    if args.list_presets:
        for name, p in _PRESET_MAP.items():
            print(f"{name}: context_size={p.context_size} device={p.device} "
                  f"draft_model_enabled={p.draft_model_enabled} enable_thinking={p.enable_thinking} "
                  f"-- {p.note}")
        return

    model_dir = Path(args.models_root) / args.model_name
    out_dir = (Path(args.out_dir) if args.out_dir else
               Path(__file__).resolve().parent.parent / "test_results_benchmark_gguf" /
               datetime.now().strftime("%Y%m%d_%H%M%S"))

    if args.restore_only:
        editor = ModelConfigEditor(model_dir)
        if editor.backup_path.exists():
            editor.restore()
            print(f"[RESTORE_ONLY] 已从 {editor.backup_path} 还原 {editor.config_path}")
        else:
            print(f"[RESTORE_ONLY] 未找到备份文件 {editor.backup_path}，无需还原")
        return

    runner = BenchmarkRunner(args.exe_dir, args.model_name, model_dir, args.host, args.port, out_dir,
                              per_question_timeout=args.per_question_timeout,
                              idle_timeout=args.idle_timeout)

    deadline = Deadline(args.budget_seconds)
    results: List[ConfigResult] = []

    try:
        if args.smoke:
            questions = get_questions(qids=["speed_photosynthesis", "quality_math_reasoning",
                                             "quality_direct_execution_no_steps"])
            bconfig = BenchmarkConfig(name="smoke_asis", note="冒烟测试，不改动 config.json，使用模型现有配置")
            print(f"{'='*60}\n冒烟测试: model={args.model_name}（不改动配置文件）\n{'='*60}")
            results.append(runner.run_config(bconfig, questions, deadline, patch_config=False))
        elif args.single:
            draft_enabled = {"on": True, "off": False, None: None}[args.draft_model]
            bconfig = BenchmarkConfig(name=args.name, context_size=args.context_size,
                                       device=args.device, draft_model_enabled=draft_enabled,
                                       enable_thinking=args.enable_thinking)
            categories = args.categories.split(",") if args.categories else None
            questions = get_questions(categories=categories)
            print(f"{'='*60}\n自定义配置: {bconfig}\n{'='*60}")
            results.append(runner.run_config(bconfig, questions, deadline))
        else:
            names = (list(_PRESET_MAP.keys()) if args.presets == "all"
                     else [n.strip() for n in args.presets.split(",") if n.strip()])
            categories = args.categories.split(",") if args.categories else None
            questions = get_questions(categories=categories)
            for name in names:
                if name not in _PRESET_MAP:
                    print(f"  [WARN] 未知预设名: {name}，跳过")
                    continue
                if deadline.expired():
                    print(f"  [BUDGET] 墙钟预算已耗尽，预设 {name} 及之后的预设均标记为未测")
                    results.append(ConfigResult(config=_PRESET_MAP[name], started=False,
                                                 start_error="墙钟预算耗尽，未测（不外推结论）"))
                    continue
                bconfig = _PRESET_MAP[name]
                print(f"\n{'='*60}\n配置: {bconfig.name} (剩余预算: {deadline.remaining()})\n{'='*60}")
                results.append(runner.run_config(bconfig, questions, deadline))
    finally:
        if not args.no_restore and not args.smoke:
            runner.restore_config()
            print(f"[CLEANUP] 已恢复 {runner.editor.config_path} 原始内容")

    json_path, md_path = write_report(results, out_dir, " ".join(sys.argv))

    print(f"\n{'='*60}\n汇总\n{'='*60}")
    for r in results:
        n_pass = sum(1 for q in r.questions if q.passed)
        n_total = len(r.questions)
        status = "启动失败" if not r.started else ("中断" if r.aborted else "完成")
        print(f"  {r.config.name}: {status}, {n_pass}/{n_total} 题通过, "
              f"耗时 {r.wall_seconds:.1f}s")


if __name__ == "__main__":
    main()
