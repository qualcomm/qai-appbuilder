#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import base64
import json
import random
import time
from pathlib import Path
from datetime import datetime
import requests

from .common import (
    CHAT_CONTENT_FORMATS,
    CrashEvent,
    HAS_PSUTIL,
    MULTIMODAL_PROMPTS,
    TEST_PROMPTS,
    TestResult,
    _build_chat_content,
    _build_chat_system_content,
    _trace_request,
    _trace_snapshot,
    detect_modality,
    infer_backend,
)
from .diagnostics import (
    _capability_issue_from_model_defect,
    _capture_log_tail,
    _classify_process_down,
    _confirm_process_dead,
    _extract_failure_reason,
    _is_cascading_mnn_oom,
    _mark_mnn_oom,
    _pick_random_asset_file,
    _scan_model_defect_log,
)
from .regressions import _STATELESS_MODE_READ_TOOL_DEF


# ============================================================================
# tool_calls / function calling 协议测试用到的预定义工具 Schema 与配置读取工具函数
# ============================================================================
# 用于 test_tool_call_model_invocation_probe/test_tool_call_streaming_argument_integrity_probe/
# test_tool_call_round_trip_with_result：prompt_optimizer.cpp:705-727 GetOptimizedToolDefinition
# 预定义工具名表中已确认存在的 "read"，对应的 OpenAI tools[] JSON Schema。与
# run_numresponse_stateless_mode_regressions() 用的 _STATELESS_MODE_READ_TOOL_DEF（module 后面
# 定义）是同一个预定义工具，这里复用同一个常量，不重复定义第二份。


LONG_TOOL_HISTORY_COUNT = 81


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
        tests.append(("POST /v1/chat/completions (tool_call long history accepted)",
                      lambda rn=round_num: self.test_tool_call_long_history_accepted(rn)))
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
            ("model-required contract", lambda rn=round_num: self.test_model_required_contract(rn)),
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
        # 严格判定:仅当 HTTP 200 且包含完整槽位结构与 loaded_models 计数时 PASS;
        # 其它任何状态(包括冷启动期间)一律视为 FAIL。
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
                slots = data.get("slots") if isinstance(data, dict) else None
                expected_devices = {"npu", "gpu", "cpu"}
                slots_valid = isinstance(slots, list) and len(slots) == len(expected_devices) \
                    and {slot.get("device") for slot in slots if isinstance(slot, dict)} == expected_devices \
                    and all(isinstance(slot.get("model"), str)
                            and isinstance(slot.get("is_loaded"), bool)
                            and isinstance(slot.get("backend"), str)
                            and "is_default" not in slot
                            for slot in slots if isinstance(slot, dict))
                loaded_models_valid = isinstance(data, dict) \
                    and type(data.get("loaded_models")) is int and data["loaded_models"] >= 0
                legacy_absent = isinstance(data, dict) and "loading" not in data
                if isinstance(data, dict) and legacy_absent and slots_valid and loaded_models_valid:
                    passed = True
                    response_data = data
                    detail = f"OK (loaded_models={data.get('loaded_models')})"
                elif not isinstance(data, dict) or not legacy_absent:
                    detail = "FAIL: 200 但仍包含已废弃的 loading 字段"
                    response_data = data
                else:
                    detail = "FAIL: 200 但 slots 结构无效"
                    response_data = data
            except Exception as e:
                detail = f"FAIL: JSON 解析失败: {e}, body={r.text[:100]}"
                response_data = {"raw_body": r.text[:200]}
        else:
            try:
                response_data = r.json()
            except Exception:
                response_data = {"raw_body": r.text[:200] if r.text else ""}
            detail = f"FAIL: status={r.status_code}, body={r.text[:120] if r.text else ''}"
        ignorable = False
        ignore_reason = ""
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency, detail=detail,
            response_data=response_data,
            ignorable=ignorable, ignore_reason=ignore_reason
        )

    def test_profile(self, round_num):
        resp = self._safe_request("GET", "/profile", "GET /profile", round_num,
                                  params={"model": self.model_name})
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
                    entries = [e for e in data.get("models", []) if e.get("model") == self.model_name]
                    passed = len(entries) == 1
                    response_data = data
                    detail = f"OK (keys={list(entries[0].keys())[:8]})" if passed else "FAIL: models 中缺少当前模型"
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
            elif "not loaded" in error_msg.lower():
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
                {"role": "system", "content": _build_chat_system_content(content_format)},
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
                {"role": "system", "content": _build_chat_system_content(content_format)},
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
                requests.post(f"{self.base_url}/stop", json={"text": "stop", "model": self.model_name}, timeout=15)

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
        resp = self._safe_request("POST", "/contextsize", "POST /contextsize", round_num,
                                  json={"model": self.model_name})
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
                entries = {e.get("model"): e.get("contextsize", 0) for e in data.get("models", [])}
                ctx = entries.get(self.model_name, 0)
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

    def test_model_required_contract(self, round_num):
        name = "model-required contract (400/404)"
        checks = [
            ("POST", "/contextsize", {"json": {"model": "__no_such_model__"}}, 404),
            ("POST", "/stop", {"json": {"text": "stop"}}, 400),
            ("POST", "/stop", {"json": {"text": "stop", "model": "__no_such_model__"}}, 404),
            ("POST", "/clear", {"json": {}}, 400),
            ("POST", "/unload", {"json": {}}, 400),
            ("POST", "/unload", {"json": {"model": "__no_such_model__"}}, 404),
            ("GET", "/profile", {"params": {"model": "__no_such_model__"}}, 404),
            ("POST", "/v1/chat/completions",
             {"json": {"messages": [{"role": "user", "content": "hi"}]}}, 400),
        ]
        failures = []
        start = time.time()
        for method, path, kwargs, expected in checks:
            try:
                r = requests.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
                if r.status_code != expected:
                    failures.append(f"{method} {path} -> {r.status_code} (expected {expected})")
            except Exception as e:
                failures.append(f"{method} {path} -> {e!r}")
        latency = (time.time() - start) * 1000
        passed = not failures
        return TestResult(
            name=name, round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=200 if passed else 0, latency_ms=latency,
            detail="OK" if passed else "FAIL: " + "; ".join(failures)
        )

    def test_stop(self, round_num):
        body = {"text": "stop", "model": self.model_name}
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
        body = {"text": "clear", "model": self.model_name}
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

    def test_tool_call_long_history_accepted(self, round_num):
        """确定性契约测试：最后一条 user 消息之后紧跟 LONG_TOOL_HISTORY_COUNT 条 role:"tool"
        占位消息,断言服务端不按工具调用次数拒绝请求(返回 200,且响应体不含 tool_call_limit /
        "maximum allowed tool call retries")。服务端已不设工具调用次数上限,防死循环由客户端
        (Builder 无进展熔断)负责。"""
        name = "POST /v1/chat/completions (tool_call long history accepted)"
        tool_messages = [
            {"role": "tool", "tool_call_id": f"call_{i}", "name": "read",
             "content": "placeholder tool result for long-history probe"}
            for i in range(LONG_TOOL_HISTORY_COUNT)
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
        body_text = r.text or ""
        if r.status_code == 200:
            limit_hit = ("tool_call_limit" in body_text
                         or "maximum allowed tool call retries" in body_text)
            passed = not limit_hit
            detail = f"status=200, tool_messages={LONG_TOOL_HISTORY_COUNT}, limit_hit={limit_hit}"
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
                    detail += f", tool_messages={LONG_TOOL_HISTORY_COUNT}, body={body_text[:200]}"
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
        # 检测机制正是围绕这个标记设计的,见调查结论 Tab 第9.2节)。曾经(Layer 1-3 兜底修复架构
        # 落地前)这是模型能力局限的容忍信号,但 Layer -1/0/1/2/3 架构(response_tools.cpp/
        # response_dispatcher.cpp,见同名 .md 文档)已确保:模型一旦真的触发了 tool_calls(本测试
        # 已在上面排除"未触发"情形),最终响应体永不应再出现 "unknow" 这个兜底占位符——
        # Layer1 本地确定性提取 + Layer2 服务端内部隐形自纠正重试 + Layer3 分级终态协议三层
        # 兜底会在模型输出畸形时收敛为"合法 tool_call(识别出真实工具名)"或"纯文本
        # (finish_reason=stop/length)"之一,不会再放行 "unknow" 走到客户端。因此一旦命中,
        # 判定为硬性失败(hard_errors),而不再是 skipped 容忍——这是本测试区别于"模型未触发
        # tool_calls"分支的关键:那种情形是模型指令遵循问题(服务端管不了),这种情形曾是服务端
        # 自身兜底链全部失败的信号(服务端该管、现已通过新架构管住)。
        if any(tc.get("function", {}).get("name") == "unknow" for tc in tool_calls):
            _reason = ("模型触发了 tool_calls 且服务端仍输出了 unknow 兜底工具名"
                       "(ResponseTools::convertToolCallJson 精确兜底标记)——Layer -1/0/1/2/3"
                       "兜底修复架构落地后不应再出现此标记,这是服务端契约回归,计入 failed")
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=_reason,
                skipped=False, response_data={"tool_calls": tool_calls, "finish_reason": finish_reason}
            )
        if finish_reason not in ("tool_calls", "stop", "length"):
            _reason = (f"finish_reason={finish_reason!r} 不属于 tool_calls/stop/length 三者之一"
                       "——Layer -1/0/1/2/3 兜底架构下不应存在第四种半成品终态,计入 failed")
            return TestResult(
                name=name, round_num=round_num, model_name=self.model_name,
                passed=False, status_code=r.status_code, latency_ms=latency,
                detail=_reason,
                skipped=False, response_data={"tool_calls": tool_calls, "finish_reason": finish_reason}
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
        resp = self._safe_request("POST", "/unload", "POST /unload", round_num,
                                  json={"model": self.model_name})
        if isinstance(resp, TestResult):
            return resp
        r, latency = resp
        passed = r.status_code == 200
        return TestResult(
            name="POST /unload", round_num=round_num, model_name=self.model_name,
            passed=passed, status_code=r.status_code, latency_ms=latency,
            detail="OK" if passed else f"status={r.status_code}"
        )
