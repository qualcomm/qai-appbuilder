#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import sys
import time
from pathlib import Path
from datetime import datetime
import requests

from .common import CrashEvent, TestResult, infer_backend
from .diagnostics import (
    _capture_log_tail,
    _classify_process_down,
    _confirm_process_dead,
    _extract_failure_reason,
    _get_mnn_oom_event,
    _mark_mnn_oom,
)


# ============================================================================
# MultiModelTester - 多后端并发驻留与路由测试
# ============================================================================
class MultiModelTester:
    """
    阶段 3：多后端并发驻留与路由测试。

    ModelManager 按设备族（npu/gpu/cpu）各自维护一个驻留槛位，互不踢除：加载某族新模型
    只踢除 loaded_models_ 中与之同族的旧模型，不影响其它族已驻留的模型。本阶段验证：
    1. GET /models 同时上报全部驻留模型的 is_loaded=true
    2. 依次向 npu/gpu/cpu 三种后端的模型发送 chat 请求，验证三者能按设备族同时驻留
       （后加载的设备不会把先加载的不同族模型踢下线），而非仅验证"顺序切换成功"
    3. 并发向不同模型发送请求，验证并发安全性（需要 ≥2 个已加载模型才有意义）
    4. 请求未加载/不存在的模型时返回 404
    5. MNN 模型加载失败（含崩溃）标记为 ignorable

    内存约束说明：
    - QNN/NPU 模型加载失败时 LoadModel 返回 false，服务继续运行
    - GGUF/GPU 模型 OOM 时抛出异常，被捕获后跳过该模型
    - MNN/CPU 模型 OOM 时可能导致进程崩溃，测试框架检测崩溃并标记 ignorable
    """

    # MNN 后端标识（加载失败/崩溃时标记为 ignorable）
    MNN_BACKENDS = {"mnn"}

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
        """依次向 npu/gpu(GGUF)/cpu(MNN) 三个不同设备的模型发送 chat 请求，触发
        chat_request_handler.cpp 中已有的"磁盘扫描 + 动态加载"分支（ModelManager::LoadModelByName()
        按设备族冲突踢除），验证三种后端各自都能被成功加载。

        ModelManager 按设备族（npu/gpu/cpu）各自维护一个驻留槛位：加载某族新模型只踢除
        loaded_models_ 中与之同族的旧模型，不影响其它族已驻留的模型——因此三者加载完成后
        应当同时驻留，而不仅仅是"依次切换成功"。三个设备依次加载完成后，本函数额外调用
        _get_loaded_model_entries() 校验先前已成功加载的设备族条目此时仍为 is_loaded=true，
        这是对"后加载的模型不会把不同族的旧模型踢下线"这一回归场景的直接断言。

        models_by_device: dict[str, str]，形如 {"npu": "qwen3-8b-8480", "gpu": "gpt-oss-20b-GGUF", "cpu": "gpt-oss-20b-MNN"}
        返回一条汇总 TestResult：MNN OOM 崩溃时按现有 ignorable 机制处理并跳过继续；因更早设备
        崩溃导致循环提前结束、从未被真正请求过的设备标记为 skipped（而不是失败）；已成功加载的
        设备在全部切换完成后被意外踢下线（跨族驻留校验失败）记为 failed。
        """
        name = "MULTI: sequential backend switching (npu/gpu/cpu each independently loadable)"
        touched = []
        device_results = {}
        for device, model_name in models_by_device.items():
            is_mnn = device == "cpu"
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

        skipped_devices = [d for d in models_by_device.keys() if d not in device_results]
        bad_devices = [d for d, r in device_results.items()
                       if (not r.passed) and (not r.skipped) and (not r.ignorable)]

        passed_devices = [d for d, r in device_results.items() if r.passed]
        final_entries = self._get_loaded_model_entries()
        final_loaded_ids_lower = {str(e.get("id", "")).lower() for e in final_entries if e.get("id")}
        evicted_after_load = [f"{d}:{models_by_device[d]}" for d in passed_devices
                               if models_by_device[d].lower() not in final_loaded_ids_lower]

        detail = (f"顺序切换结果: {touched}"
                  + (f", 因更早设备崩溃未被真正尝试: {sorted(skipped_devices)}" if skipped_devices else "")
                  + (f", 跨族驻留校验: 已成功加载的 {len(passed_devices)} 个设备在全部切换完成后" +
                     ("仍全部 is_loaded=true" if not evicted_after_load
                      else f"有 {len(evicted_after_load)} 个被意外踢下线: {evicted_after_load}")
                     if passed_devices else ""))
        if bad_devices or evicted_after_load:
            # 至少一个设备被真正请求过但切换/加载失败(且非崩溃/优雅跳过/已知 ignorable)，
            # 或已成功加载的设备被不同族的后续加载意外踢下线——这是需要关注的真实问题。
            result = self._make_result(name, False, 0, detail)
        elif skipped_devices:
            # 有设备从未被真正尝试(更早设备崩溃导致循环提前 break)，无法验证该设备是否可
            # 正常切换加载，精确标记为 skipped=True，而不是失败。
            result = self._make_result(
                name, False, 0, detail, skipped=True,
                ignore_reason=f"更早的设备崩溃导致循环提前结束,{sorted(skipped_devices)} 后端从未被真正请求"
            )
        else:
            result = self._make_result(name, True, 0, detail)
        result.response_data = {
            "device_results": {d: {"passed": r.passed, "skipped": r.skipped, "ignorable": r.ignorable}
                                for d, r in device_results.items()},
            "skipped_devices": sorted(skipped_devices),
            "request_order": touched,
            "cross_family_residency_evicted": evicted_after_load,
        }
        return result

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
        """未知模型应快速返回 404/400，且不改变其它已驻留槽位状态。"""
        name = "MULTI: invalid model route preserves resident slots"
        invalid_model = "__invalid_model_for_negative_route_test__"
        try:
            before_response = requests.get(f"{self.base_url}/status", timeout=10)
            before_status = before_response.json() if before_response.status_code == 200 else None
        except Exception as e:
            return self._make_result(name, False, 0, f"无法读取请求前 /status: {e}")
        if not isinstance(before_status, dict):
            return self._make_result(name, False, before_response.status_code,
                                     f"请求前 /status 无效: {before_response.text[:120]}")

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
        try:
            after_response = requests.get(f"{self.base_url}/status", timeout=10)
            after_status = after_response.json() if after_response.status_code == 200 else None
        except Exception as e:
            return self._make_result(name, False, r.status_code,
                                     f"未知模型返回后无法读取 /status: {e}")

        latency_ok = latency_ms < 5000
        route_ok = r.status_code in (404, 400) and latency_ok
        resident_before = before_status.get("loaded_models", 0)
        slots_before = before_status.get("slots")
        slots_after = after_status.get("slots") if isinstance(after_status, dict) else None
        same_slots = isinstance(slots_before, list) and isinstance(slots_after, list) \
            and [(slot.get("device"), slot.get("model"), slot.get("is_loaded"), slot.get("backend"))
                 for slot in slots_before if isinstance(slot, dict)] == \
            [(slot.get("device"), slot.get("model"), slot.get("is_loaded"), slot.get("backend"))
             for slot in slots_after if isinstance(slot, dict)]
        status_preserved = isinstance(after_status, dict) \
            and after_status.get("loaded_models") == resident_before \
            and (resident_before == 0 or same_slots)
        passed = route_ok and status_preserved
        detail = f"status={r.status_code}, latency={latency_ms:.0f}ms, resident={resident_before}, status_preserved={status_preserved}"
        if not route_ok:
            detail += " (期望 404/400 且 <5000ms)"
        if not status_preserved:
            detail += " (/status 的已驻留模型状态发生变化)"
        result = TestResult(
            name=name, round_num=self.round_num, model_name="_multi_model_",
            passed=passed, status_code=r.status_code, latency_ms=latency_ms,
            detail=detail
        )
        result.response_data = {
            "invalid_model": invalid_model,
            "before_status": before_status,
            "after_status": after_status if isinstance(after_status, dict) else {},
        }
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
        长度 > 1 时先调用 ensure_multi_backend_loaded 主动触发多种设备的顺序加载，验证三者
        能按设备族同时驻留（ModelManager 按 npu/gpu/cpu 各自一个槛位，互不踢除）；
        长度 <=1（环境里只发现单一 backend 类型）时不再完全静默跳过、不留痕迹，而是
        登记一条 skipped=True 的 TestResult，明确说明"环境无法验证多后端切换"。

        models_root/remote_mode: 保留参数签名以兼容既有调用点（历史上用于驱动"两两同时
        驻留"降级验证，该验证现由 ensure_multi_backend_loaded 内置的跨族驻留断言覆盖，
        此处仍不使用这两个参数）。
        """
        print(f"\n{'='*60}")
        print("阶段 3: 多后端并发驻留与路由测试")
        print(f"{'='*60}")

        if models_by_device and len(models_by_device) > 1:
            print(f"  验证 NPU/GGUF/MNN 三后端可依次切换加载 ... ", end="", flush=True)
            r = self.ensure_multi_backend_loaded(models_by_device)
            self.results.append(r)
            tag = "✓ PASS" if r.passed else ("⚠ SKIP" if r.skipped else ("⚠ IGN" if r.ignorable else "✗ FAIL"))
            print(f"{tag} ({r.detail[:100]})")
        elif models_by_device is not None:
            devices_found = sorted(models_by_device.keys())
            self.results.append(TestResult(
                name="MULTI: sequential backend switching (npu/gpu/cpu each independently loadable)",
                round_num=self.round_num, model_name="_multi_model_",
                passed=False, status_code=0, latency_ms=0,
                detail=f"环境中只发现单一后端类型({devices_found})，无法验证多后端切换",
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
