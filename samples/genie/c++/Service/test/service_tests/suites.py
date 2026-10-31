#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import sys
from pathlib import Path
from datetime import datetime
import requests

from .api_tester import APITester
from .common import CrashEvent, TestResult, detect_modality, infer_backend, wait_port_open
from .diagnostics import _classify_process_down
from .managers import PerfMonitor, RemoteServiceManager, ServiceManager
from .multi_model import MultiModelTester, _estimate_model_dir_size_bytes
from .regressions import (
    run_gguf_explicit_load_regressions,
    run_graceful_shutdown_tests,
    run_numresponse_stateless_mode_regressions,
    run_sampleapp_only_tests,
)
from .report import ReportGenerator


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
