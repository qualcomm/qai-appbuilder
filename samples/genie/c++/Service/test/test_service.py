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
import shlex
import sys
from pathlib import Path

from service_tests.builder_local import ModelDirSnapshot
from service_tests.common import (
    CrashEvent,
    HAS_PSUTIL,
    TestResult,
    detect_modality,
    infer_backend,
    wait_http_ok,
    wait_port_closed,
    wait_port_open,
)
from service_tests.managers import (
    QAIModelBuilderManager,
    ServiceManager,
    _CsrfSession,
    cleanup_junctions,
    resolve_builder_python,
)
from service_tests.multi_model import discover_models, discover_models_remote
from service_tests.report import ReportGenerator
from service_tests.suite_long_task_memory import _run_long_task_memory_suite
from service_tests.suite_prompt_fidelity import _run_prompt_fidelity_suite
from service_tests.suite_skill_capacity import _run_skill_capacity_suite
from service_tests.suites import (
    _finalize_and_exit,
    _run_full_suite,
    _run_gguf_suite,
    _run_graceful_shutdown_suite,
    _run_mnn_suite,
    _run_model_suite,
    _run_multi_model_suite,
    _run_multimodal_suite,
    _run_qnn_suite,
    _run_sampleapp_suite,
)


SUITE_HANDLERS = {
    "full": _run_full_suite,
    "model": _run_model_suite,
    "sampleapp": _run_sampleapp_suite,
    "multimodal": _run_multimodal_suite,
    "gguf": _run_gguf_suite,
    "multi_model": _run_multi_model_suite,
    "mnn": _run_mnn_suite,
    "qnn": _run_qnn_suite,
    "graceful_shutdown": _run_graceful_shutdown_suite,
    "prompt_fidelity": _run_prompt_fidelity_suite,
    "skill_capacity": _run_skill_capacity_suite,
    "long_task_memory": _run_long_task_memory_suite,
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
    parser.add_argument("--builder_port", type=int, default=8989, help="QAIModelBuilder 后端端口（默认 8989）")
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
                             "供 skill_capacity 的 builder 模式使用")
    parser.add_argument("--gguf_model", default=None, help="GGUF 显式加载回归限定的单个模型目录名（默认留空，测试全部已发现的 GGUF 模型）")
    parser.add_argument("--gguf_devices", choices=("both", "gpu", "cpu"), default="both", help="GGUF 显式加载回归的设备筛选：both/gpu/cpu（默认 both）")
    parser.add_argument("--skill_capacity_models", default=None,
                        help="--suite skill_capacity 限定测试的目标模型简写，逗号分隔（qwen3-8b/qwen2.5_omini），"
                             "留空则两个目标模型都测（各自缺失时精确跳过，不影响另一个）")
    parser.add_argument("--skill_capacity_max", type=int, default=64,
                        help="--suite skill_capacity 倍增探测+二分搜索的硬上限 N（默认 64）；"
                             "倍增到该上限仍全部通过时结果标 converged=False，需调大重测，不当真实前沿使用")
    parser.add_argument("--skill_capacity_repeat", type=int, default=3,
                        help="--suite skill_capacity 每个 N 重复试探取多数票的次数（默认 3）")
    parser.add_argument("--skill_capacity_mode", choices=("direct", "builder", "both"), default="direct",
                        help="--suite skill_capacity 驱动路径：direct=直连取证前沿搜索通道（唯一能测出前沿数字的通道）；"
                             "builder=真实 QAIModelBuilder 端到端确认通道（在直连已测出的前沿 N 上确认选对+答对，"
                             "需要 --builder_dir/--genie_root_path 具备，环境不具备时精确跳过）；"
                             "both=两者都跑（Builder 分支先跑并自管 Builder/服务生命周期）。默认 direct")
    parser.add_argument("--skill_capacity_arms", choices=("legacy", "optimized", "both"), default="legacy",
                        help="--suite skill_capacity 档位对照：legacy=改造前基线（临时改写 prompt_engineering.json 的 "
                             "fidelity.{cjk,ascii}_chars_per_token=4.0 + budget_partition.enabled=false，改前备份、"
                             "finally 还原）；optimized=Step2 改造后的默认档位（不写覆盖，直接用 C++ 侧新默认值）；"
                             "both=依次跑两档并在 data 里各自记录 frontier_skills，供报告算提升倍数")
    parser.add_argument("--suite", choices=("full", "model", "sampleapp", "multimodal", "gguf", "multi_model", "mnn", "qnn", "graceful_shutdown", "prompt_fidelity", "skill_capacity", "long_task_memory"),
                        default=None, help="选择要运行的测试套件（必传参数，不再有隐式默认值；如需完整回归请显式传入 full）")
    parser.add_argument("--long_task_memory_models", default=None,
                        help="--suite long_task_memory 限定测试的目标模型简写，逗号分隔（qwen3-8b/gguf-20b/mnn-20b），"
                             "留空则三个目标模型都测（各自缺失时精确跳过，不影响其他）")
    parser.add_argument("--long_task_memory_max", type=int, default=8,
                        help="--suite long_task_memory 倍增探测+二分搜索的硬上限 N（默认 8，真机 LLM 推理成本远高于 "
                             "skill_capacity 的纯提示词判定，默认值偏小以控制真机耗时；倍增到该上限仍全部通过时 "
                             "结果标 converged=False，需调大重测，不当真实前沿使用）")
    parser.add_argument("--long_task_memory_repeat", type=int, default=1,
                        help="--suite long_task_memory 每个 N 重复试探取多数票的次数（默认 1，每次试探即 2 轮真实 "
                             "推理，真机耗时下先用 1 验证方法论，充裕时再调大）")
    parser.add_argument("--long_task_memory_filler_lines", type=int, default=6,
                        help="--suite long_task_memory 每个填充轮工具输出的行数（默认 6，越大越快挤占预算触发丢弃）")
    parser.add_argument("--long_task_memory_timeout", type=int, default=180,
                        help="--suite long_task_memory 单次 HTTP 请求超时秒数（默认 180；真机推理为秒级到十几秒级，"
                             "不能套用纯单测的短超时，但仍需留有余量避免真正挂起拖死整个套件）")
    parser.add_argument("--long_task_memory_arms", choices=("legacy", "optimized", "both"), default="both",
                        help="--suite long_task_memory 档位对照：legacy=task_memo.enabled=false（当前默认纯数字 "
                             "占位）；optimized=task_memo.enabled=true（model_layer_enabled=false 保证判定确定性）；"
                             "both=依次跑两档并记录 frontier，供报告算提升倍数/差值（默认 both）")
    parser.add_argument("--long_task_memory_mode", choices=("direct", "client", "both"), default="direct",
                        help="--suite long_task_memory 驱动路径：direct=Python 直连读 X-Genie-Prompt-Memo-* 头做 "
                             "细粒度断言与二分搜索前沿（唯一能测出前沿数字的通道）；client=GenieAPIClient.exe "
                             "--raw_file 端到端交叉确认（只在 direct 已测出的前沿附近抽样，不独立求前沿）；"
                             "both=两者都跑。默认 direct")
    parser.add_argument("--long_task_memory_service_mode", choices=("stateless", "stateful", "both"), default="stateless",
                        help="--suite long_task_memory 服务模式：stateless=-n -1；stateful=常规 -n。"
                             "已更正的架构事实：压缩管线（含 TaskMemoBuilder）对这两种模式完全无分支、等价运行，"
                             "`-n` 只决定客户端断开后服务端是否额外持久化历史（和本套件断言无关），因此 stateful 也同样做 "
                             "legacy/optimized 双臂对照；both=两者都跑。默认 stateless")
    parser.add_argument("--long_task_memory_stateful_n", type=int, default=6,
                        help="--suite long_task_memory stateful 模式下传给服务的 -n 值（默认 6）")
    parser.add_argument("--long_task_memory_new_fields_models", default="qwen3-8b",
                        help="--suite long_task_memory 中 Task Memo 新增字段（goal anchor/分页目录/跨模式一致性，"
                             "场景 A-D）这组轻量确定性回归限定测试的目标模型简写，逗号分隔，默认仅 qwen3-8b（控制"
                             "真机推理成本；这组场景不求前沿数字，每个场景只需额外 1-4 轮真实推理）")
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
    # 需要它的 suite（如 skill_capacity 的 builder 模式）内部自己精确报错并 skip。
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

    # 不支持 --model_name 的 suite（multimodal/multi_model：分别用自动筛选/全量并发逻辑，
    # 不读取该参数）若检测到用户显式传入该参数，打印一条不阻断执行的提示，避免参数被
    # 静默忽略、用户却毫无察觉。
    _SUITES_WITHOUT_MODEL_NAME = {"multimodal", "multi_model"}
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
