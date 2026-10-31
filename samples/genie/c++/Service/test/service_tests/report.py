#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import json
import re
from dataclasses import asdict
from pathlib import Path
from datetime import datetime

from .common import _content_format_badge_html, _normalize_cpu_percent


# ============================================================================
# ReportGenerator - 报告生成器
# ============================================================================
class ReportGenerator:
    @staticmethod
    def generate_json(all_results, perf_data, crash_events, out_dir):
        # ignored 是 failed 的子集 — 不是与 passed/failed/crashed 并列的第四种状态,
        # 而是"这些 failed 里有多少属于服务端已知缺陷,在过滤后可以忽略"。
        # 排查时,如果只关心真实回归,可以用 failed - ignored 看"非忽略失败数"。
        summary = {"total": 0, "passed": 0, "failed": 0, "crashed": 0, "skipped": 0, "ignored": 0,
                   "model_capability_issues": 0}
        models_data = {}

        for result in all_results:
            summary["total"] += 1
            if result.skipped:
                summary["skipped"] += 1
            elif result.crashed:
                summary["crashed"] += 1
            elif result.passed:
                summary["passed"] += 1
            else:
                summary["failed"] += 1
                if result.ignorable:
                    summary["ignored"] += 1
            if result.model_capability_issue:
                summary["model_capability_issues"] += 1

            model = result.model_name
            if model not in models_data:
                models_data[model] = {}
            rnd = f"round_{result.round_num}"
            if rnd not in models_data[model]:
                models_data[model][rnd] = []
            models_data[model][rnd].append({
                "name": result.name,
                "passed": result.passed,
                "status_code": result.status_code,
                "latency_ms": round(result.latency_ms, 1),
                "detail": result.detail,
                "crashed": result.crashed,
                "skipped": result.skipped,
                # 已知缺陷标记: ignorable=True 意味着这条 FAIL 是服务端已知占位/未实现缺陷,
                # 用于在统计、CI gate、报告过滤时区分"真实回归"vs"已知缺陷"。
                "ignorable": result.ignorable,
                "ignore_reason": result.ignore_reason if result.ignorable else None,
                "model_capability_issue": result.model_capability_issue,
                "model_capability_reason": result.model_capability_reason if result.model_capability_issue else None,
                "is_stream": result.is_stream,
                "ttft_ms": round(result.ttft_ms, 1) if result.is_stream else None,
                "chunk_count": result.chunk_count if result.is_stream else None,
                "text_prompt": result.text_prompt if result.text_prompt else None,
                "text_response": result.text_response if result.text_response else None,
                "response_data": result.response_data if result.response_data else None,
            })

        output = {
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "models": models_data,
            "performance": perf_data,
            "crash_events": [asdict(e) for e in crash_events]
        }

        out_path = Path(out_dir) / "results.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\n[输出] JSON 报告: {out_path}")

    # 模型无关接口（不需要加载模型即可测试的通用接口 + 全局收尾接口）
    MODEL_INDEPENDENT_ENDPOINTS = [
        "GET /", "GET /models", "GET /v1/models",
        "POST /servicestop",
    ]

    # 模型相关的所有端点(显式枚举,顺序即报告里的行序)
    MODEL_DEPENDENT_ENDPOINTS = [
        # /status 观察用,夹在 chat 前后
        "GET /status (before chat)",
        # ChatCompletions handler 注册的 4 个别名 × (non-stream + stream) = 8 项
        "POST /v1/chat/completions (non-stream)",
        "POST /v1/chat/completions (stream)",
        "POST /chat/completions (non-stream)",
        "POST /chat/completions (stream)",
        "POST /v1/completions (non-stream)",
        "POST /v1/completions (stream)",
        "POST /completions (non-stream)",
        "POST /completions (stream)",
        "GET /status (after chat)",
        # 其它模型相关 GET / POST
        "GET /profile",
        "POST /contextsize",
        "POST /fetch",
        # TextSplitter handler 两个别名
        "POST /textsplitter",
        "POST /v1/textsplitter",
        # ImageGenerate handler 两个别名
        "POST /images/generations",
        "POST /v1/images/generations",
        "POST /reload",
        # 最后一轮的收尾
        "POST /stop", "POST /clear", "POST /unload",
    ]

    # 推理性能相关(所有 chat 别名,任何一个有 perf 数据都纳入对比)
    MODEL_PERF_ENDPOINTS = [
        "POST /v1/chat/completions (non-stream)",
        "POST /v1/chat/completions (stream)",
        "POST /chat/completions (non-stream)",
        "POST /chat/completions (stream)",
        "POST /v1/completions (non-stream)",
        "POST /v1/completions (stream)",
        "POST /completions (non-stream)",
        "POST /completions (stream)",
        "GET /profile",
    ]

    # 内部占位 model_name：仅用于内部过滤/聚合（_global_=模型无关通用接口测试、
    # _multi_model_=阶段3多模型并发聚合、_builder_local_model_=QAIModelBuilderLocalModelTester
    # 里"不针对具体模型"的手段层检查项（仍被 skill_capacity 的 builder 模式复用），如
    # configure_genie_root/inject_local_models/discover_models_via_builder 等，_skill_capacity_ / _skill_capacity_builder_
    # =skill_capacity 套件里"整套件级/环境前置"的检查项，如两个目标模型都没匹配到时的
    # suite precondition），绝不能作为"模型名"文本渗透进任何
    # 渲染出的报告 HTML（性能对比表、模型链接卡片、标题等）。新增任何占位 model_name 时
    # 必须同步加入这里，并统一通过 _is_internal_placeholder_model() 过滤——排除逻辑必须
    # 统一走这个函数，不要分散在多处各自手写，否则容易遗漏。
    #
    # ⚠ 曾漏注册的实例（本机干跑渲染时被 _assert_no_placeholder_leak 之外的显式核验抓到）：
    # skill_capacity 套件的 _skill_capacity_ 系列占位名一度未登记，于是它作为"模型"渗进了
    # 主报告的模型链接卡片。新增套件时若引入了新的占位 model_name，务必同步登记到下面两个
    # 常量，否则防泄漏断言也扫不到它（该断言只认已登记的名字）。
    INTERNAL_PLACEHOLDER_MODEL_NAMES = frozenset({"_global_", "_multi_model_", "_builder_local_model_",
                                                  "_skill_capacity_"})
    INTERNAL_PLACEHOLDER_MODEL_PREFIXES = ("_graceful_shutdown_", "_skill_capacity_")

    @staticmethod
    def _is_internal_placeholder_model(model_name):
        """判断 model_name 是否为内部占位名（非真实模型），渲染报告时必须排除。"""
        return (model_name in ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES
                or model_name.startswith(ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_PREFIXES))

    @staticmethod
    def _assert_no_placeholder_leak(html, context):
        """防泄漏兜底：报告生成完毕、写盘之前，扫描最终 HTML 全文，确认内部占位
        model_name 没有被当作可读文本渗透进去。这是最后一道防线——即使某个渲染路径
        遗漏了 _is_internal_placeholder_model() 过滤，也会在这里立刻炸出来，而不是让
        "_multi_model_" 这类占位符悄悄流入 report.html 被人误以为是一个真实模型。"""
        for name in ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES:
            if name in html:
                raise RuntimeError(
                    f"[占位符泄漏防护] 检测到内部占位 model_name {name!r} 以文本形式出现在"
                    f"生成的 HTML 中（{context}）。这不允许发生——请检查是否有渲染路径遗漏了"
                    f" ReportGenerator._is_internal_placeholder_model() 过滤。"
                )

    # "多类型同时加载"阶段的检查项名称（MULTI: 前缀）为内部实现层面的英文短语，对不熟悉
    # 代码的人偏抽象（如 "backend coexistence matrix (triple + pairwise fallback)"）。
    # 这里按"精确匹配的静态检查项"与"带动态后缀（模型名/组合名）的检查项"分两组维护
    # 说明文案，供 _generate_multi_backend_detail_html 的"逐项检查记录"表格渲染"说明"列。
    # 新增该阶段的检查项时应同步在此补充映射，否则该行"说明"列会留空。
    _MULTI_CHECK_DESCRIPTIONS_EXACT = {
        "MULTI: GET /models (multi-model list)":
            "调用 GET /models 接口，验证服务能正确返回当前已加载的模型",
        "MULTI: sequential backend switching (npu/gpu/cpu each independently loadable)":
            "依次向 QNN(NPU)/GGUF(GPU)/MNN(CPU) 三种后端各一个模型发送请求，验证 ModelManager 按设备族（npu/gpu/cpu）各自维护一个驻留槛位、互不踢除——三者加载完成后应同时驻留，而不仅是依次切换成功",
        "MULTI: invalid model route returns 404/400":
            "请求一个不存在的模型名，验证服务能快速返回 404/400 错误，而不是长时间挂起或误路由到其它模型",
        "MULTI: concurrent requests to different models":
            "同时向多个已加载的不同模型并发发送请求，验证并发场景下各请求能被正确路由到对应模型、互不干扰、不引发崩溃（同时校验回复内容非空；已加载模型 <2 个时无法验证并发安全性，标记为 skipped）",
        "MULTI: default model route (no 'model' field) after dynamic switches":
            "发一个不带 model 字段的请求，观察在多次动态切换后默认模型是否仍能正常响应（当前实现里默认模型会随动态切换漂移，本检查只监控不崩溃/不返回 5xx，不假定具体路由到哪个模型）",
    }
    _MULTI_CHECK_DESCRIPTIONS_PREFIX = (
        ("MULTI: auto-restart after MNN OOM (",
         "MNN 模型因内存不足导致进程崩溃后，验证服务能否自动重启并恢复到可正常接受请求的状态"),
        ("MULTI: chat route → ",
         "向该模型发送一次 chat 请求，验证请求能被正确路由到此模型并返回正常响应"),
    )

    @staticmethod
    def _describe_multi_check(name):
        """把"多类型同时加载"阶段某条检查项的名称，翻译成一句人类可读的说明文字。
        精确匹配优先；未命中时再按已知前缀匹配带动态后缀（模型名/组合名）的检查项；
        两者都未命中返回空字符串（该行"说明"列留空，而不是报错阻断报告生成）。"""
        desc = ReportGenerator._MULTI_CHECK_DESCRIPTIONS_EXACT.get(name)
        if desc:
            return desc
        for prefix, prefix_desc in ReportGenerator._MULTI_CHECK_DESCRIPTIONS_PREFIX:
            if name.startswith(prefix):
                return prefix_desc
        return ""

    # "逐项检查记录"表格按类别分组展示，而不是把全部检查项摊成一张不分类的长列表——
    # 单模型路由验证("MULTI: chat route → <model>")会随已加载模型数量线性增长、逐条描述
    # 高度雷同，混在其它类别检查项中会显得杂乱且难以定位。每个元组为
    # (类别名, 精确匹配名称集合, 前缀匹配元组)；分类顺序即渲染顺序。新增检查项时应同步
    # 在此归类，未归类的检查项会落入下方 _MULTI_CHECK_CATEGORY_FALLBACK 兜底类别（不报错）。
    _MULTI_CHECK_CATEGORIES = (
        ("单模型路由验证", (), ("MULTI: chat route → ",)),
        ("多后端顺序切换验证", (
            "MULTI: sequential backend switching (npu/gpu/cpu each independently loadable)",
        ), ()),
        ("通用接口与异常路由", (
            "MULTI: GET /models (multi-model list)",
            "MULTI: invalid model route returns 404/400",
            "MULTI: default model route (no 'model' field) after dynamic switches",
        ), ()),
        ("并发与故障恢复", (
            "MULTI: concurrent requests to different models",
        ), ("MULTI: auto-restart after MNN OOM (",)),
    )
    _MULTI_CHECK_CATEGORY_FALLBACK = "其它检查项"

    @staticmethod
    def _categorize_multi_check(name):
        """把"多类型同时加载"阶段某条检查项的名称归入一个分组类别，用于"逐项检查记录"
        表格的分组展示。精确匹配优先；未命中时再按前缀匹配；两者都未命中归入统一的兜底
        类别（而不是报错阻断报告生成）。"""
        for category, exact_names, prefixes in ReportGenerator._MULTI_CHECK_CATEGORIES:
            if name in exact_names:
                return category
            for prefix in prefixes:
                if name.startswith(prefix):
                    return category
        return ReportGenerator._MULTI_CHECK_CATEGORY_FALLBACK

    @staticmethod
    def _common_css():
        """Professional dashboard aesthetic — Material-inspired blue header, soft-shadow cards, sans-serif body, tabular mono numbers. Mirrors the legacy report.html with subtle refinements."""
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
    --font-serif: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
.header .subtitle { color: rgba(255,255,255,0.85); font-size: 14px; max-width: 720px; }
.header .meta { display: flex; gap: 28px; margin-top: 18px; flex-wrap: wrap; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.18); }
.header .meta-item { font-size: 13px; color: rgba(255,255,255,0.92); font-variant-numeric: tabular-nums; }
.header .meta-item strong { color: #FFFFFF; font-weight: 600; }
.cmdline-details { display: inline-block; vertical-align: middle; }
.cmdline-details summary { cursor: pointer; color: rgba(255,255,255,0.92); font-weight: 600; list-style: none; }
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
.summary-card.card-crash { border-top-color: var(--warning); }
.summary-card .num { font-size: 36px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; letter-spacing: -0.5px; color: var(--text); }
.summary-card .label { font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; font-weight: 600; }
.summary-card.card-pass .num { color: var(--success); }
.summary-card.card-fail .num { color: var(--danger); }
.summary-card.card-crash .num { color: var(--warning); }
.progress-bar { background: #E8EAF6; border-radius: 20px; height: 28px; margin: 18px 0 8px; overflow: hidden; position: relative; box-shadow: inset 0 1px 2px rgba(0,0,0,0.04); }
.progress-fill { height: 100%; border-radius: 20px; transition: width 0.6s ease; display:flex; align-items:center; justify-content:center; }
.progress-fill.good { background: linear-gradient(90deg, var(--success-light), #66BB6A); }
.progress-fill.warn { background: linear-gradient(90deg, var(--warning-light), #FFA726); }
.progress-fill.bad { background: linear-gradient(90deg, var(--danger-light), #EF5350); }
.progress-text { font-size: 13px; font-weight: 600; color: white; text-shadow: 0 1px 2px rgba(0,0,0,0.3); letter-spacing: 0.3px; }
.progress-meta { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-secondary); margin-bottom: 28px; padding: 0 4px; font-variant-numeric: tabular-nums; }
.progress-meta .pct { color: var(--primary); font-weight: 600; font-size: 13px; }
.section { margin-bottom: 32px; }
.section h2 { font-size: 18px; font-weight: 600; color: var(--text); margin-bottom: 16px; padding-bottom: 10px; border-bottom: 2px solid var(--border); letter-spacing: -0.2px; position: relative; display: flex; align-items: center; }
.section h2::after { content: ''; position: absolute; left: 0; bottom: -2px; width: 56px; height: 2px; background: var(--primary); }
.section h2 .icon { color: var(--primary); margin-right: 8px; }
.icon { margin-right: 8px; }
.model-section { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 24px; overflow: hidden; transition: box-shadow 0.18s ease; }
.model-section:hover { box-shadow: var(--shadow-hover); }
.model-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: linear-gradient(180deg, #FAFAFA 0%, #F5F5F5 100%); border-bottom: 1px solid var(--border); }
.model-header h3 { font-size: 16px; color: var(--primary); font-weight: 600; letter-spacing: -0.2px; }
.model-stats { display: flex; gap: 12px; align-items: center; }
.stat-pass { font-size: 13px; color: var(--success); font-weight: 500; font-variant-numeric: tabular-nums; }
.stat-rate { font-size: 12px; background: var(--primary); color: white; padding: 3px 12px; border-radius: 12px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: 0.2px; }
.group-section { padding: 18px 20px; border-bottom: 1px solid #F0F0F0; }
.group-section:last-child { border-bottom: none; }
.group-title { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px; letter-spacing: 0.2px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { background: #F5F5F5; padding: 10px 12px; text-align: left; font-weight: 600; color: var(--text-secondary);
            text-transform: uppercase; font-size: 11px; letter-spacing: 0.4px; border-bottom: 2px solid var(--border); }
tbody td { padding: 10px 12px; border-bottom: 1px solid #F0F0F0; vertical-align: middle; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: var(--bg-alt); }
tbody tr:last-child td { border-bottom: none; }
td.center, th.center { text-align: center; }
td.num, th.num { text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
code { background: #F5F5F5; padding: 2px 6px; border-radius: 3px; font-family: var(--font-mono); font-size: 12px; color: var(--text); }
.status-pass, .status-fail, .status-crash, .status-skip { display: inline-block; font-weight: 700; font-size: 11px; letter-spacing: 0.5px; padding: 3px 9px; min-width: 52px; text-align: center; border-radius: 10px; }
.status-pass { color: var(--success); background: rgba(46,125,50,0.10); }
.status-fail { color: var(--danger); background: rgba(198,40,40,0.10); }
.status-crash { color: var(--warning); background: rgba(230,81,0,0.10); }
.status-skip { color: var(--muted); background: rgba(120,144,156,0.12); }
.badge-ignorable { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(255,152,0,0.14); color: #E65100; border: 1px solid rgba(230,81,0,0.30); cursor: help; vertical-align: middle; }
.badge-capability { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(30,136,229,0.14); color: #1565C0; border: 1px solid rgba(21,101,192,0.30); cursor: help; vertical-align: middle; }
.badge-mnn-oom { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(198,40,40,0.14); color: var(--danger); border: 1px solid rgba(198,40,40,0.30); cursor: help; vertical-align: middle; }
.badge-mnn-cascade { display: inline-block; margin-left: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; padding: 2px 7px; border-radius: 8px; background: rgba(120,144,156,0.16); color: #37474F; border: 1px solid rgba(84,110,122,0.30); cursor: help; vertical-align: middle; }
.card-ignored { border-top: 3px solid #FB8C00; }
.card-ignored .num { color: #E65100; }
.card-capability { border-top: 3px solid var(--warning); }
.card-capability .num { color: var(--warning); }
.latency-warn { color: #F57C00; font-weight: 600; }
.latency-slow { color: var(--danger); font-weight: 700; }
.cv-stable { color: var(--success); font-size: 11px; font-weight: 600; }
.cv-moderate { color: #F57C00; font-size: 11px; font-weight: 600; }
.cv-unstable { color: var(--danger); font-size: 11px; font-weight: 700; }
.pivot-table { margin: 0; }
.resp-cell { max-width: 360px; min-width: 240px; font-size: 12px; vertical-align: middle; padding: 10px 12px !important; }
.resp-ok { display: inline-block; color: var(--success); font-weight: 600; font-size: 12px; letter-spacing: 0.2px; }
.resp-error { color: var(--danger); font-size: 11px; word-break: break-all; font-family: var(--font-mono); line-height: 1.45; }
.resp-detail { color: var(--text-secondary); font-size: 11px; line-height: 1.45; }
.resp-value { font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums; }
.resp-value strong { color: var(--primary); font-weight: 700; }
.resp-models { font-weight: 600; color: var(--primary); display: block; margin-bottom: 4px; }
.resp-status { color: var(--text); }
.resp-empty { color: var(--muted); font-style: italic; }
.resp-json { font-size: 11px; line-height: 1.6; color: var(--text-secondary); font-family: var(--font-mono); }
.resp-json strong { color: var(--text); font-weight: 600; }
.resp-list { font-size: 11px; margin-top: 4px; color: var(--text-secondary); line-height: 1.5; }
.resp-link { display: inline-block; margin-left: 6px; font-size: 11px; color: var(--primary); text-decoration: none; font-weight: 600; padding: 2px 9px; border-radius: 10px; background: rgba(21,101,192,0.08); transition: background 0.15s, color 0.15s; }
.resp-link:hover { background: var(--primary); color: #FFFFFF; }
.resp-round { margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px dashed var(--border); font-size: 12px; }
.resp-round:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.resp-kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; font-family: var(--font-mono); font-size: 11px; line-height: 1.5; align-items: baseline; }
.resp-kv .kv-key { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
.resp-kv .kv-val { color: var(--text); font-variant-numeric: tabular-nums; word-break: break-all; }
.resp-kv .kv-more { grid-column: 1 / -1; color: var(--muted); font-size: 10px; margin-top: 4px; font-style: italic; }
.compare-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
.perf-round-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 16px; margin-top: 4px; }
.perf-round-card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px; border-top: 3px solid var(--primary-light); transition: transform 0.18s ease, box-shadow 0.18s ease; }
.perf-round-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
.perf-round-card .perf-round-title { font-size: 13px; font-weight: 700; color: var(--primary); margin-bottom: 10px; letter-spacing: 0.2px; }
.perf-round-card .perf-metric { display: flex; justify-content: space-between; align-items: baseline; padding: 5px 0; font-size: 12px; border-bottom: 1px dashed #EEEEEE; }
.perf-round-card .perf-metric:last-child { border-bottom: none; }
.perf-round-card .perf-metric-label { color: var(--text-secondary); }
.perf-round-card .perf-metric-value { font-family: var(--font-mono); font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; white-space: nowrap; margin-left: 10px; }
.mode-tag { padding: 2px 9px; border-radius: 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.2px; }
.mode-stream { background: #E3F2FD; color: var(--primary); }
.mode-nonstream { background: #FFF3E0; color: var(--warning); }
.mode-format-string { background: #ECEFF1; color: var(--text-secondary); }
.mode-format-array { background: #E1F5FE; color: #0277BD; }
.mode-format-object { background: #F3E5F5; color: #6A1B9A; }
.delta-up { color: var(--danger); font-weight: 600; font-family: var(--font-mono); }
.delta-down { color: var(--success); font-weight: 600; font-family: var(--font-mono); }
.crash-table { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; border-left: 4px solid var(--danger); }
.crash-table tbody td { color: var(--danger); }
.perf-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 16px; margin-bottom: 22px; }
.perf-stat { background: var(--card-bg); border-radius: var(--radius); padding: 18px; text-align: center; box-shadow: var(--shadow); border-top: 3px solid var(--primary-light); }
.perf-value { display: block; font-size: 28px; font-weight: 700; color: var(--primary); font-variant-numeric: tabular-nums; letter-spacing: -0.5px; line-height: 1.1; }
.perf-label { font-size: 12px; color: var(--text-secondary); margin-top: 6px; text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }
.chart-container { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.chart-container canvas { background: var(--card-bg); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); height: 280px !important; }
.empty-state { background: var(--card-bg); border-radius: var(--radius); padding: 30px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 14px; box-shadow: var(--shadow); }
.footer { text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 40px; letter-spacing: 0.2px; }
.model-link-card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 22px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; transition: transform 0.18s ease, box-shadow 0.18s ease; border-left: 3px solid var(--primary); }
.model-link-card:hover { transform: translateX(2px); box-shadow: var(--shadow-hover); }
.model-link-card a { text-decoration: none; color: var(--primary); font-weight: 600; font-size: 16px; letter-spacing: -0.2px; }
.model-link-card a:hover { color: var(--primary-dark); }
.model-link-card .model-link-stats { display: flex; gap: 18px; font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; letter-spacing: 0.2px; align-items: center; }
.startup-banner { background: linear-gradient(135deg, #FFEBEE 0%, #FFCDD2 100%); border: 1px solid var(--danger); border-left: 6px solid var(--danger); border-radius: var(--radius); padding: 22px 26px; margin-bottom: 24px; box-shadow: 0 3px 14px rgba(198,40,40,0.18); }
.startup-banner-title { font-size: 18px; font-weight: 700; color: var(--danger); margin-bottom: 8px; letter-spacing: -0.2px; }
.startup-banner-desc { font-size: 13px; color: #5D2020; line-height: 1.65; margin-bottom: 14px; }
.startup-banner-desc code { background: rgba(198,40,40,0.10); color: var(--danger); padding: 2px 6px; border-radius: 3px; font-size: 12px; }
.startup-table { width: 100%; border-collapse: collapse; background: rgba(255,255,255,0.7); border-radius: var(--radius-sm); overflow: hidden; }
.startup-table thead th { background: rgba(198,40,40,0.10); color: var(--danger); padding: 9px 12px; text-align: left; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; border-bottom: 1px solid rgba(198,40,40,0.20); }
.startup-table tbody td { padding: 10px 12px; border-bottom: 1px solid rgba(198,40,40,0.10); vertical-align: top; font-size: 12px; color: #3E1212; }
.startup-table tbody tr:last-child td { border-bottom: none; }
.startup-detail { white-space: pre-wrap; word-break: break-word; font-family: var(--font-mono); font-size: 11px; line-height: 1.5; color: #4A1818; margin: 0; max-height: 240px; overflow-y: auto; background: rgba(255,255,255,0.6); padding: 8px 10px; border-radius: 3px; }
@media (max-width: 768px) {
    .chart-container { grid-template-columns: 1fr; }
    .header .meta { flex-direction: column; gap: 8px; }
    .pivot-table { font-size: 11px; }
}
@media print {
    .header { background: #333 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .summary-card, .model-section, table { box-shadow: none; border: 1px solid #ddd; }
}"""

    @staticmethod
    def _format_response_data(r):
        """格式化响应数据为 HTML 展示"""
        ep = r.name

        # chat/completions 特殊处理：即使 response_data 为空，只要有 text_response 就生成链接
        if "chat/completions" in ep:
            fmt_badge = _content_format_badge_html(r)
            if not r.passed:
                return f'<span class="resp-detail">{_html_escape(r.detail[:200])}</span>{fmt_badge}'
            mode = "stream" if r.is_stream else "nonstream"
            anchor = f"{r.model_name}_R{r.round_num}_{mode}"
            if r.is_stream:
                return f'<span class="resp-value">TTFT={r.ttft_ms:.0f}ms, {r.chunk_count} chunks</span>{fmt_badge} <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'
            else:
                resp_len = len(r.text_response) if r.text_response else 0
                return f'<span class="resp-value">响应长度: {resp_len} 字符</span>{fmt_badge} <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        if not r.response_data:
            if r.status_code == 200 and r.passed:
                return '<span class="resp-ok">OK</span>'
            elif r.detail:
                return f'<span class="resp-detail">{_html_escape(r.detail[:80])}</span>'
            return '-'

        data = r.response_data

        # 根据不同接口类型格式化展示
        if "models" in ep.lower() and "models" in data:
            # 与模型数量无关，统一显示 OK
            return '<span class="resp-ok">OK</span>'

        elif ep == "GET /status":
            loaded_models = data.get("loaded_models")
            error = data.get("error")
            if error:
                return f'<span class="resp-error">{_html_escape(str(error)[:80])}</span>'
            return f'<span class="resp-status">loaded_models: {loaded_models}</span>'

        elif ep == "GET /profile":
            # 排除以下 6 项指标——它们已在“性能报告”小节中独立展示，避免重复
            _PERF_KEYS_HIDDEN = {
                "time_to_first_token", "token_generation_time",
                "prompt_processing_rate", "token_generation_rate",
                "num_prompt_tokens", "num_generated_tokens",
            }
            keys = [k for k in data.keys() if k not in _PERF_KEYS_HIDDEN]
            if not keys:
                # 全部字段都是性能指标——下方“性能报告”已经展示，这里直接给 OK
                return '<span class="resp-ok">OK</span>'
            items = []
            for k in keys[:6]:
                v = data[k]
                if isinstance(v, (dict, list)):
                    v_str = f"[{len(v)} items]" if isinstance(v, list) else f"{{{len(v)} keys}}"
                else:
                    v_str = str(v)[:30]
                items.append(f'<span class="kv-key">{_html_escape(k)}</span><span class="kv-val">{_html_escape(v_str)}</span>')
            more = f'<div class="kv-more">+{len(keys)-6} more</div>' if len(keys) > 6 else ""
            return f'<div class="resp-kv">{"".join(items)}{more}</div>'

        elif ep == "POST /contextsize":
            ctx = data.get("contextsize", 0)
            return f'<span class="resp-value">contextsize = <strong>{ctx}</strong></span>'

        elif "textsplitter" in ep:
            seg_count = data.get("segment_count", 0)
            anchor = f"{r.model_name}_R{r.round_num}_textsplitter"
            return f'<span class="resp-value">{seg_count} segments</span> <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        elif ep == "POST /fetch":
            count = data.get("history_count", 0)
            if count == 0:
                return '<span class="resp-value">history: 空</span>'
            anchor = f"{r.model_name}_R{r.round_num}_fetch"
            return f'<span class="resp-value">{count} 条记录</span> <a href="conversations.html#{anchor}" class="resp-link">查看详情 →</a>'

        elif ep == "POST /reload":
            error = data.get("error", "")
            if error:
                return f'<span class="resp-error">{_html_escape(str(error)[:80])}</span>'
            return '<span class="resp-ok">OK</span>'

        else:
            if isinstance(data, dict) and data:
                items = [f'{_html_escape(k)}: {_html_escape(str(v)[:30])}' for k, v in list(data.items())[:3]]
                return f'<div class="resp-json">{"<br>".join(items)}</div>'
            return '<span class="resp-ok">OK</span>'

    @staticmethod
    def _build_pivot_table(results_subset, endpoint_list, rounds):
        """构建透视表：端点为行，R1/R2/R3 为列，最后一列显示响应数据"""
        endpoints_in_data = []
        for ep in endpoint_list:
            if any(r.name == ep for r in results_subset):
                endpoints_in_data.append(ep)

        if not endpoints_in_data:
            return ""

        round_headers = ""
        for rnd in rounds:
            round_headers += f'<th class="center">R{rnd}</th><th class="num">延迟</th>'

        html = f"""<table class="pivot-table">
        <thead><tr><th>端点</th>{round_headers}<th>响应数据</th></tr></thead>
        <tbody>"""

        # Endpoints whose response data should be shown per-round (large data with links)
        _per_round_endpoints = set(ReportGenerator.MODEL_PERF_ENDPOINTS) | {
            "POST /fetch", "POST /textsplitter", "POST /v1/textsplitter",
        }
        # /profile 是性能接口但返回体紧凑,不需要每轮都展开
        _per_round_endpoints.discard("GET /profile")

        for ep in endpoints_in_data:
            ep_results = {r.round_num: r for r in results_subset if r.name == ep}
            html += f'<tr><td><code>{_html_escape(ep)}</code></td>'

            all_round_results = []
            for rnd in rounds:
                r = ep_results.get(rnd)
                if r is None:
                    html += '<td class="center">-</td><td class="center">-</td>'
                else:
                    if r.skipped:
                        cls, txt = "status-skip", "SKIP"
                    elif r.crashed:
                        cls, txt = "status-crash", "CRASH"
                    elif r.passed:
                        cls, txt = "status-pass", "PASS"
                    else:
                        cls, txt = "status-fail", "FAIL"
                    lat_cls = ""
                    if r.latency_ms > 10000:
                        lat_cls = "latency-slow"
                    elif r.latency_ms > 3000:
                        lat_cls = "latency-warn"
                    # 已知缺陷标记: FAIL 但 ignorable=True 时,在徽章右侧加一个橙色"可忽略"小徽章,
                    # title 属性显示具体原因。这样表格里一眼能区分"真实回归"和"已知占位失败"。
                    # ignore_reason=="mnn_oom_cascade" 是一个独立的诊断标识(见 _classify_process_down)：
                    # 表示"疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需要人工排查根因"，
                    # 但崩溃永远是真实问题——该标识不再意味着 ignorable=True，只是用灰色的
                    # .badge-mnn-cascade 诊断徽章标注疑似关联，FAIL 状态和统计口径不受影响。
                    ignorable_badge = ""
                    if not r.passed:
                        ignore_reason = getattr(r, "ignore_reason", "")
                        if ignore_reason == "mnn_oom_cascade":
                            reason = _html_escape(getattr(r, "detail", "") or "疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因(已计入真实失败,非豁免)")
                            ignorable_badge = f'<span class="badge-mnn-cascade" title="{reason}">疑似级联崩溃(非豁免)</span>'
                        elif getattr(r, "ignorable", False):
                            reason = _html_escape(ignore_reason or "已知服务端缺陷")
                            ignorable_badge = f'<span class="badge-ignorable" title="{reason}">可忽略</span>'
                    # 模型能力局限标记: passed=True 但 model_capability_issue=True 时单列一个蓝色
                    # 徽章,与"可忽略"并列展示，避免与普通 PASS 混淆。统一判断 model_capability_issue
                    # 这一对通用字段(tool_calls 模型能力局限、服务端 [MODEL_DEFECT] 日志标记同样命中)。
                    if getattr(r, "model_capability_issue", False):
                        reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
                        ignorable_badge += f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
                    html += f'<td class="center"><span class="{cls}">{txt}</span>{ignorable_badge}</td>'
                    html += f'<td class="num {lat_cls}">{r.latency_ms:.0f} ms</td>'
                    all_round_results.append(r)

            # 响应数据列：对于有大量数据的端点，每轮都显示链接；其他端点只显示最后一轮
            if all_round_results:
                if ep in _per_round_endpoints:
                    resp_parts = []
                    for r in all_round_results:
                        resp_parts.append(f'<div class="resp-round">R{r.round_num}: {ReportGenerator._format_response_data(r)}</div>')
                    html += f'<td class="resp-cell">{"".join(resp_parts)}</td>'
                else:
                    resp_html = ReportGenerator._format_response_data(all_round_results[-1])
                    html += f'<td class="resp-cell">{resp_html}</td>'
            else:
                html += '<td class="resp-cell">-</td>'
            html += '</tr>'

        html += "</tbody></table>"
        return html

    @staticmethod
    def _case_status_badge_html(r):
        """根据 TestResult 的 skipped/crashed/passed/ignorable/ignore_reason/model_capability_issue
        计算状态徽章 HTML,与 _build_pivot_table 保持完全一致的判定与视觉语言,供不适合"端点 x 轮次"
        矩阵布局的逐用例明细表(Builder 集成代理/SampleApp)复用。返回 (cls, txt, extra_badges_html)。"""
        if r.skipped:
            cls, txt = "status-skip", "SKIP"
        elif r.crashed:
            cls, txt = "status-crash", "CRASH"
        elif r.passed:
            cls, txt = "status-pass", "PASS"
        else:
            cls, txt = "status-fail", "FAIL"
        extra_badges = ""
        if not r.passed:
            ignore_reason = getattr(r, "ignore_reason", "")
            if ignore_reason == "mnn_oom_cascade":
                reason = _html_escape(getattr(r, "detail", "") or "疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因(已计入真实失败,非豁免)")
                extra_badges = f'<span class="badge-mnn-cascade" title="{reason}">疑似级联崩溃(非豁免)</span>'
            elif getattr(r, "ignorable", False):
                reason = _html_escape(ignore_reason or "已知服务端缺陷")
                extra_badges = f'<span class="badge-ignorable" title="{reason}">可忽略</span>'
        # 统一判断 model_capability_issue,与 _build_pivot_table 保持同一套口径。
        if getattr(r, "model_capability_issue", False):
            reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
            extra_badges += f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
        return cls, txt, extra_badges

    @staticmethod
    def _build_case_detail_table(results, show_model_column=True):
        """构建逐用例明细表：不同于 _build_pivot_table 的"端点 x 轮次"矩阵布局(那种布局假定
        同一端点会跨多轮重复出现),Builder 集成代理 / SampleApp / tool_calls 协议测试这类用例
        通常每个模型/后端只跑一次,按用例名逐行展示 通过状态/模型或后端/耗时/详情/模型能力问题
        更直观。复用既有 .pivot-table/.status-*/.badge-* 类名体系，保持一致的视觉语言。"""
        if not results:
            return ""
        model_header = "<th>模型/后端</th>" if show_model_column else ""
        html = f"""<table class="pivot-table">
        <thead><tr><th>用例</th>{model_header}<th class="center">状态</th><th class="num">耗时</th><th>详情</th><th>模型能力问题</th></tr></thead>
        <tbody>"""
        for r in results:
            cls, txt, extra_badges = ReportGenerator._case_status_badge_html(r)
            model_cell = f'<td>{_html_escape(r.model_name)}</td>' if show_model_column else ""
            capability_cell = ""
            if getattr(r, "model_capability_issue", False):
                reason = _html_escape(getattr(r, "model_capability_reason", "") or "模型能力局限")
                capability_cell = f'<span class="badge-capability" title="{reason}">模型能力局限</span>'
            html += f"""<tr><td><code>{_html_escape(r.name)}</code></td>{model_cell}
            <td class="center"><span class="{cls}">{txt}</span>{extra_badges}</td>
            <td class="num">{r.latency_ms:.0f} ms</td>
            <td>{_html_escape(r.detail)}</td>
            <td class="center">{capability_cell}</td></tr>"""
        html += "</tbody></table>"
        return html

    @staticmethod
    def _cmdline_meta_html(cmdline):
        """构建 header 区域"命令行"展示项：展示本次运行调用 test_service.py 时的完整命令行，
        复用 --font-mono 等宽字体样式（.cmdline-pre，与 .startup-detail 同一套设计语言，
        仅针对深色 header 背景调整了配色），超长命令行通过 <details> 折叠，避免撑爆 header 布局。
        未提供 cmdline（例如向后兼容旧调用点）时不渲染该项。"""
        if not cmdline:
            return ""
        # 默认展开（open 属性）：其它 meta-item 都是标签+值直接可见，命令行若默认折叠，
        # 用户不点击就只看到"命令行"这个词、看不到任何内容，容易误以为该字段是空的。
        return (f'<span class="meta-item"><details class="cmdline-details" open><summary><strong>命令行</strong></summary>'
                f'<pre class="cmdline-pre">{_html_escape(cmdline)}</pre></details></span>')

    # --suite 取值 → 该 suite 本轮会覆盖到的测试完备性矩阵列（类别名）。
    # full 现在也真正执行 SampleApp(见 _run_full_suite 阶段6),覆盖全部类别;
    # 未被本轮 suite 覆盖的类别在矩阵中统一显示"未运行本轮",避免与真实失败混淆。
    _SUITE_MATRIX_COVERAGE = {
        "full": {"通用接口", "文本chat", "多模态", "多模型路由", "GGUF显式加载", "SampleApp", "Builder/OpenClaw"},
        "model": {"通用接口", "文本chat", "多模态"},
        "multimodal": {"多模态"},
        "multi_model": {"多模型路由"},
        "gguf": {"GGUF显式加载"},
        "sampleapp": {"SampleApp"},
        "mnn": {"通用接口", "文本chat"},
        "qnn": {"通用接口", "文本chat", "多模态"},
        "prompt_fidelity": {"提示词保真"},
    }
    MATRIX_CATEGORIES = ("通用接口", "文本chat", "多模态", "多模型路由", "GGUF显式加载", "SampleApp", "Builder/OpenClaw", "提示词保真")

    # 设备后缀形式如 " (CPU)"/" (GPU)"/" (NPU)"：GGUF 模型未在 config.json 显式指定 device 时
    # 由服务自行决定实际加载设备(见 _run_model_suite),报告把这类模型的展示名统一打上该后缀,
    # 使常规生命周期测试(阶段2)与显式设备加载回归(阶段4)落在同一行,不再拆成 3 行。
    _DEVICE_SUFFIX_RE = re.compile(r" \([A-Z]+\)$")

    @staticmethod
    def _strip_device_suffix(name):
        """去掉展示名上的设备后缀,还原成服务内部注册的裸模型名。多模型并发阶段(阶段3)
        产生的 TestResult.name 里引用的仍是裸模型名,需要用这个还原后的名字做子串匹配,
        否则 GGUF 模型被打上设备后缀后,"多模型路由"这一列会因为字符串不再匹配而漏判。"""
        return ReportGenerator._DEVICE_SUFFIX_RE.sub("", name)

    @staticmethod
    def _env_constraint_reason(r):
        """判断某条未通过的 TestResult 是否属于"环境受限"的真实失败(而非未预期的回归),
        用于测试完备性矩阵的展示层面归类为"跳过(注明原因)"。仅影响矩阵单元格的展示状态,
        不修改 TestResult 本身的 passed/crashed/ignorable/skipped 字段——这些失败仍然如实
        计入 failed/crashed 统计和退出码判定(崩溃/资源不足永远是需要关注的真实问题,这里
        只是让矩阵里的"失败"状态更聚焦于真正未预期的回归,不与已知的资源限制混在一起)。"""
        detail = r.detail or ""
        detail_lower = detail.lower()
        if "insufficient_memory" in detail_lower or "内存不足" in detail:
            return "内存不足(insufficient_memory)"
        if getattr(r, "ignore_reason", "") == "mnn_oom_cascade":
            return "疑似MNN OOM级联崩溃(需人工排查,已计入失败统计)"
        return None

    @staticmethod
    def _matrix_cell_status(matched_results):
        """把某个模型×类别对应的一批 TestResult 归纳为矩阵单元格状态,返回 (status, reason)。
        reason 仅在 status=="跳过" 且能定位到具体原因时非空,用于单元格 title 提示。"""
        if not matched_results:
            return "未运行本轮", ""
        if all(r.skipped for r in matched_results):
            reasons = sorted({r.detail for r in matched_results if r.detail})
            return "跳过", "; ".join(reasons[:3])
        if all(r.passed or getattr(r, "ignorable", False) for r in matched_results):
            return "通过", ""
        # 未通过的结果里，如果全部都能归因到已知的"环境受限"信号(MNN 内存不足优雅拒绝、
        # 疑似 MNN OOM 级联崩溃等)，或本身已被测试逻辑正确判定为 skipped(如模型能力局限
        # 的 tool_calls 探测)，矩阵展示层面视为"跳过(注明原因)"，不改变底层统计口径。这里
        # 必须把 r.skipped 一起从 failing 里排除，否则同一类别下"部分通过 + 部分合理跳过"的
        # 混合结果会因为不满足"全部 skipped"也不满足"全部 passed/ignorable"而误落入失败分支。
        failing = [r for r in matched_results if not (r.passed or getattr(r, "ignorable", False) or r.skipped)]
        if not failing:
            skip_reasons = sorted({r.detail for r in matched_results if r.skipped and r.detail})
            return "跳过", "; ".join(skip_reasons[:3])
        env_reasons = [ReportGenerator._env_constraint_reason(r) for r in failing]
        if all(env_reasons):
            return "跳过", "; ".join(sorted(set(env_reasons))[:3])
        return "失败", ""

    # "通用接口"是兜底类别，只应容纳"没有专属矩阵列"的模型级用例。tool_calls/GGUF显式
    # 加载/Builder/SampleApp 都已经有各自的专属列（见下方各分支）、以及独立的报告展示区块
    # (report_tool_calls.html、逐模型报告的同名区块等)，其判定结果(尤其是 skipped=True 这
    # 类模型能力局限/环境限制)与"通用接口"本意（GET /、/contextsize、/textsplitter 等真正
    # 与模型能力无关的端点）毫无关系；一旦遗漏排除，就会被重复计入"通用接口"这一列，让它的
    # 判定结果被其它专属列的原因污染（真实案例：tool_calls 机会性探测的 skipped 曾污染
    # phi4-v81/qwen2.5vl3b-8480 的"通用接口"列，详见 4.9）。新增任何专属矩阵列时，必须把该
    # 列的匹配条件同步加入这里，防止同一条结果被"通用接口"重复计入。
    @staticmethod
    def _has_dedicated_matrix_category(name):
        return (name.startswith("POST /v1/chat/completions (tool_call")
                or name.startswith("SAMPLEAPP:")
                or name.startswith("BUILDER")
                or name.startswith("PROMPT_FIDELITY:")
                or "explicit_load" in name)

    @staticmethod
    def _build_completeness_matrix(all_results, model_names, suite_name):
        """测试完备性矩阵：行为模型,列为测试类别,展示各模型在各类别下本轮的通过/失败/跳过/未运行本轮状态。"""
        if not model_names:
            return ""
        covered = ReportGenerator._SUITE_MATRIX_COVERAGE.get(suite_name, set())
        perf_endpoints = set(ReportGenerator.MODEL_PERF_ENDPOINTS)

        rows_html = ""
        for m in model_names:
            base_m = ReportGenerator._strip_device_suffix(m)
            rows_html += f"<tr><td>{_html_escape(m)}</td>"
            for cat in ReportGenerator.MATRIX_CATEGORIES:
                reason = ""
                if cat not in covered:
                    status = "未运行本轮"
                else:
                    if cat == "通用接口":
                        matched = [r for r in all_results if r.model_name == m
                                   and r.name not in perf_endpoints and "multimodal" not in r.name
                                   and not ReportGenerator._has_dedicated_matrix_category(r.name)]
                    elif cat == "文本chat":
                        matched = [r for r in all_results if r.model_name == m
                                   and r.name in perf_endpoints and "multimodal" not in r.name]
                    elif cat == "多模态":
                        matched = [r for r in all_results if r.model_name == m and "multimodal" in r.name]
                    elif cat == "多模型路由":
                        matched = [r for r in all_results if r.model_name == "_multi_model_" and base_m in r.name]
                    elif cat == "GGUF显式加载":
                        matched = [r for r in all_results if r.model_name.startswith(m) and "explicit_load" in r.name]
                    elif cat == "Builder/OpenClaw":
                        matched = [r for r in all_results if r.model_name == m and r.name.startswith("BUILDER")]
                    elif cat == "提示词保真":
                        matched = [r for r in all_results if r.model_name == m and r.name.startswith("PROMPT_FIDELITY:")]
                    else:  # SampleApp
                        matched = [r for r in all_results if r.model_name == m and r.name.startswith("SAMPLEAPP:")]
                    status, reason = ReportGenerator._matrix_cell_status(matched)
                cls = {"通过": "status-pass", "失败": "status-fail"}.get(status, "status-skip")
                title_attr = f' title="{_html_escape(reason)}"' if reason else ""
                rows_html += f'<td class="center"{title_attr}><span class="{cls}">{status}</span></td>'
            rows_html += "</tr>"

        header_html = "".join(f"<th>{_html_escape(cat)}</th>" for cat in ReportGenerator.MATRIX_CATEGORIES)
        return f"""<div class="section"><h2><span class="icon">&#128203;</span> 测试完备性矩阵</h2>
        <table class="pivot-table"><thead><tr><th>模型</th>{header_html}</tr></thead><tbody>
        {rows_html}
        </tbody></table></div>"""

    @staticmethod
    def _generate_multi_backend_detail_html(multi_model_results, loaded_entries, out_dir, remote_mode=False, cmdline=""):
        """生成"多类型同时加载"的完整详情页(report_multi_backend_coexistence.html)。

        承载主报告不适合展示的高密度内容——逐项检查记录(每条检查的完整原始 detail 文本，
        如 insufficient_memory 的字节数明细)、后端共存矩阵每个组合的完整原因说明。主报告
        (generate_summary_html)只保留极简摘要 + 指向本页面的链接，与其它区块的密度保持
        一致。仅在存在多模型检查结果时才生成并返回 True，调用方据此决定是否渲染链接。
        """
        if not multi_model_results:
            return False

        # 按检查类别分组，而不是把全部检查项摊成一张不分类的长列表——同一类别的检查项
        # (尤其是逐模型重复出现的 "MULTI: chat route → <model>") 集中在同一张子表格里,
        # 复用既有 .group-section/.group-title 样式(与"通用接口测试"等区块保持一致)。
        _check_groups = {}
        for r in multi_model_results:
            if r.skipped:
                status_text, status_class = "SKIP", "skip"
            elif r.crashed:
                status_text, status_class = "CRASH", "crash"
            elif r.passed:
                status_text, status_class = "PASS", "pass"
            elif getattr(r, "ignorable", False):
                status_text, status_class = "IGN", "ignored"
            else:
                status_text, status_class = "FAIL", "fail"
            check_desc = ReportGenerator._describe_multi_check(r.name)
            category = ReportGenerator._categorize_multi_check(r.name)
            row_html = f"""<tr>
                <td><code>{_html_escape(r.name)}</code></td>
                <td class=\"resp-detail\">{_html_escape(check_desc)}</td>
                <td><span class=\"badge {status_class}\">{status_text}</span></td>
                <td class=\"center\">{r.status_code}</td>
                <td>{_html_escape(r.detail)}</td>
            </tr>"""
            _check_groups.setdefault(category, []).append(row_html)

        _category_order = [c[0] for c in ReportGenerator._MULTI_CHECK_CATEGORIES] + [ReportGenerator._MULTI_CHECK_CATEGORY_FALLBACK]
        _group_sections_html = ""
        for category in _category_order:
            rows = _check_groups.get(category)
            if not rows:
                continue
            _group_sections_html += f"""<div class=\"group-section\">
                <div class=\"group-title\">{_html_escape(category)}（{len(rows)}）</div>
                <table class=\"compare-table\"><thead><tr><th>检查项</th><th>说明</th><th>状态</th><th>HTTP</th><th>详情</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
            </div>"""
        checks_html = (f'<div class="model-section">{_group_sections_html}</div>'
                        if _group_sections_html else '<div class="empty-state">未捕获到任何检查项</div>')

        loaded_rows = ""
        for entry in loaded_entries:
            loaded_rows += f"""<tr>
                <td>{_html_escape(entry.get('id', ''))}</td>
                <td><code>{_html_escape(entry.get('backend', ''))}</code></td>
                <td><code>{_html_escape(entry.get('device', ''))}</code></td>
                <td class=\"num\">{_html_escape(str(entry.get('context_length', '')))}</td>
            </tr>"""
        loaded_html = (f'<table class="compare-table"><thead><tr><th>模型</th><th>后端</th><th>设备</th><th>上下文</th></tr></thead><tbody>{loaded_rows}</tbody></table>'
                        if loaded_rows else '<div class="empty-state">未捕获到已加载模型的 backend/device 明细</div>')

        coexistence_html = '<div class="empty-state">本次未产生后端共存矩阵结果</div>'
        matrix_result = next(
            (r for r in multi_model_results
             if r.name == "MULTI: backend coexistence matrix (triple + pairwise fallback)"),
            None
        )
        if matrix_result and matrix_result.response_data and matrix_result.response_data.get("coexistence_matrix"):
            cm = matrix_result.response_data["coexistence_matrix"]
            _CM_STATUS_CLASS = {"pass": "status-pass", "fail": "status-fail",
                                 "skip": "status-skip", "unknown": "status-skip"}
            _CM_STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "unknown": "N/A"}
            _CM_PAIR_LABEL = {"npu+gpu": "QNN/NPU + GGUF/GPU", "npu+cpu": "QNN/NPU + MNN/CPU",
                               "gpu+cpu": "GGUF/GPU + MNN/CPU"}
            triple = cm["triple"]
            triple_badge = f'<span class="{_CM_STATUS_CLASS.get(triple["status"], "status-skip")}">{_CM_STATUS_TEXT.get(triple["status"], triple["status"].upper())}</span>'
            pair_rows = ""
            for pair_key, pair_info in cm["pairs"].items():
                p_status = pair_info["status"]
                p_badge = f'<span class="{_CM_STATUS_CLASS.get(p_status, "status-skip")}">{_CM_STATUS_TEXT.get(p_status, p_status.upper())}</span>'
                pair_rows += f"""<tr>
                    <td>{_html_escape(_CM_PAIR_LABEL.get(pair_key, pair_key))}</td>
                    <td class=\"center\">{p_badge}</td>
                    <td>{_html_escape(pair_info["detail"])}</td>
                </tr>"""
            coexistence_html = f"""<table class="compare-table"><thead><tr><th>组合</th><th class="center">状态</th><th>详情</th></tr></thead><tbody>
            <tr><td><strong>QNN/NPU + GGUF/GPU + MNN/CPU（三后端同时驻留）</strong></td><td class="center">{triple_badge}</td><td>{_html_escape(triple["detail"])}</td></tr>
            {pair_rows}
            </tbody></table>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>多类型同时加载 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Multi-Backend Coexistence Detail</div>
        <h1>多类型同时加载 &middot; 详细记录</h1>
        <p class="subtitle">后端共存矩阵完整原因与逐项检查记录</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

<div class="section">
<h2><span class="icon">&#128187;</span> 已加载 backend/device</h2>
{loaded_html}
</div>

<div class="section">
<h2><span class="icon">&#129520;</span> 后端共存矩阵（三后端 + 两两降级，完整详情）</h2>
{coexistence_html}
</div>

<div class="section">
<h2><span class="icon">&#128203;</span> 逐项检查记录</h2>
<div class="resp-detail" style="margin: -4px 0 10px;">本节（多类型同时加载）全部检查项的完整原始记录，已按类别分组（单模型路由验证、后端共存与两两降级验证、通用接口与异常路由、并发与故障恢复），便于快速定位同类检查而不必翻阅一整张不分类的长列表。</div>
{checks_html}
</div>

<div class="footer">
    多类型同时加载详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "多类型同时加载详情页 report_multi_backend_coexistence.html")
        out_path = Path(out_dir) / "report_multi_backend_coexistence.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 多类型同时加载详情: {out_path}")
        return True

    # ------------------------------------------------------------------
    # skill_capacity 套件（技能装载前沿）报告
    # ------------------------------------------------------------------
    # 设计约束：这里**不硬编码任何前沿数值**，全部从本次运行的 TestResult.response_data
    # 里读（数据契约见 _run_skill_capacity_suite 里 `{arm}_frontier` 结果的 data 字段）。
    # 报告表述纪律（不得夸大也不得埋没）：
    #   * 只有 usable_for_gain_ratio=True（converged 且前沿点未预算饱和）的数字才允许
    #     以精确值/精确倍数呈现；
    #   * 未收敛或饱和一律渲染成「≥N 下界」并附机械原因，禁止四舍五入成看起来精确的倍数；
    #   * repeat 次数一并展示，且固定附「repeat=1 时不声称统计显著」的口径说明。
    # 历史作废数字（旧技能池 44/77/1.75x、tie_aware_l2=true 下的 111/2.5227）只在
    # 「追溯」小节以「已作废/已被取代」的措辞出现，永不进结论位置。
    _SC_ARM_LABEL = {
        "legacy": "改造前 legacy（全部新开关关闭）",
        "optimized": "改造后 optimized（出厂默认）",
    }

    _SC_DISCIPLINE_HTML = """<ul class="sc-note">
    <li><strong>可引用口径</strong>：容量池（目标技能分数严格最高、不存在同分 tie-break，且池组成跨 N 严格前缀稳定）+ 直连 GenieAPIService（<code>-n -1 -g -d 3</code>）+ 出厂默认 <code>skill_disclosure.tie_aware_l2=false</code>。</li>
    <li><strong>统计强度</strong>：本套件默认 <code>--skill_capacity_repeat</code> 次采样取多数票。<em>repeat=1 时未做重复性检验，只能表述为「效应量极大且方向明确」，不声称统计显著。</em></li>
    <li><strong>真伪三字段</strong>：只有同时满足 <code>Skills-Kept == Skills-Total</code>（一条技能都没被丢弃）、<code>skills_kept_at_budget_cap=False</code>（未咬预算上限）、<code>Tokens-Out</code> 随 N 单调递增，才能说<em>本次测量未观察到</em>「预算耗尽导致保留条数被钉死」这种旁路式假提升的迹象（这是「未观察到迹象」，不是因果层面的「已排除」）——历史上曾因 skills 预算被砍导致保留条数恒定、体量不随 N 增长，测出过一个假的高前沿值。</li>
    <li><strong>已作废、不得引用的数字</strong>：旧技能池的前沿 44 / 77 与 1.75x 倍数（旧池组成随 N 跳变，是构造产物而非容量差）；<code>tie_aware_l2=true</code> 下的 111 / 2.5227（已被该开关默认值翻转为 false 后的新测量取代，仅供追溯）。</li>
    <li><strong>不构成旧数字的复活</strong>：容量池 legacy 与旧池 legacy 撞到同一个前沿值是<em>机制同源的必然</em>——两池共用同一套技能 description/正文生成器，legacy 档全部按 L2 全量渲染时单条目录条目体量几乎相同，容量边界由「N × 单条体量」直接决定。禁止跨池互相引用。</li>
    <li><strong>环境性跳过如实标注</strong>：目标模型或其 <code>config.json</code>/<code>genie_config.json</code> 缺失时精确跳过并标注环境原因，不伪造数字（<code>qwen2.5_omini</code> 历史上即因两处目录缺 <code>config.json</code> 被跳过）。</li>
    </ul>"""

    _SC_TIE_PATHOLOGY_HTML = """<ul class="sc-note">
    <li><strong>结论（与容量数字独立，不参与倍数计算）</strong>：当多个候选技能相关性得分<em>完全相同</em>时，<strong>技能目录里的排列顺序单独决定模型去读哪一条</strong>。判别实验：固定技能数、分数、正文与全部开关，<em>仅</em>把目标技能改名使其在同分组内排到首位，目标被选中率由 <code>0.000</code> 变为 <code>1.000</code>。</li>
    <li>与之配套被严格证伪的三条竞争解释：「提前满足（未 read 就直接作答）」、「工具调用阈值过高（不敢调用工具）」、「池内位置偏置」——实测模型 <strong>100% 发起 read</strong>，只是读了排序在目标之前的同分干扰项。</li>
    <li><strong><code>skill_disclosure.tie_aware_l2</code> 默认值为 false 的实测依据</strong>：该开关会把整个同分组一起提升到 L2；在「整池同分」的守门用例（N=64）下显式置为 <code>true</code> 会直接触发 <code>HTTP 422 (local input overflow)</code>，而默认 <code>false</code> 时 <code>Skills-Kept == Skills-Total</code> 且提示词体量远低于上下文上限。该开关另已被其自身预设判据证伪（目标 5/5 次成功升到 L2 而选中率仍为 0.000），<em>保留为默认安全开关，不得作为瓶颈修复呈现</em>。</li>
    </ul>"""

    @staticmethod
    def _sc_collect_frontiers(all_results):
        """把 skill_capacity 的前沿结果聚合成 {model: {arm: entry}}。

        识别依据是 response_data 里有 frontier_skills 字段（而不是靠用例名做字符串
        猜测），这样即使日后新增档位/改名也不会静默漏掉。"""
        frontiers = {}
        for r in all_results:
            if not r.name.startswith("SKILL_CAPACITY:"):
                continue
            data = r.response_data or {}
            if "frontier_skills" not in data:
                continue
            model = data.get("model") or r.model_name
            arm = data.get("arm") or "unknown"
            curve = data.get("probe_curve") or []
            frontier = data.get("frontier_skills")
            point = next((e for e in reversed(curve) if e.get("n") == frontier), None)
            frontiers.setdefault(model, {})[arm] = {
                "frontier": frontier,
                "converged": bool(data.get("converged")),
                "saturated": bool(data.get("frontier_saturated")),
                "usable": bool(data.get("usable_for_gain_ratio")),
                "note": data.get("note") or "",
                "curve": curve,
                "point": point,
                "result": r,
            }
        return frontiers

    @staticmethod
    def _sc_frontier_text(entry):
        """单档前沿值的展示文本 + 机械原因。未收敛/饱和一律写成「≥N 下界」。"""
        if not entry:
            return "N/A", "本次未产生该档位的前沿搜索结果"
        n = entry["frontier"]
        if entry["usable"]:
            return str(n), ""
        reasons = []
        if not entry["converged"]:
            reasons.append("未收敛（倍增探测到 --skill_capacity_max 仍全部通过，真实前沿在上界之外）")
        if entry["saturated"]:
            reasons.append("前沿点预算饱和（Skills-Kept < Skills-Total，保留条数由预算而非 N 决定，与装载能力解耦）")
        return f"&ge; {n}（下界）", "；".join(reasons) or "usable_for_gain_ratio=False"

    @staticmethod
    def _sc_gain_text(legacy, optimized):
        """提升倍数：两档都 usable 才允许给精确倍数，否则只给下界或直接 N/A。"""
        if not legacy or not optimized:
            return "N/A", "缺少对照档位（需要 --skill_capacity_arms both）"
        lf, of = legacy["frontier"], optimized["frontier"]
        if not isinstance(lf, int) or not isinstance(of, int) or lf <= 0:
            return "N/A", f"legacy 前沿为 {lf}，无法作为倍数分母"
        ratio = of / lf
        if legacy["usable"] and optimized["usable"]:
            return f"{ratio:.4f}x", ""
        return (f"&ge; {ratio:.2f}x（下界）",
                "至少一档 usable_for_gain_ratio=False，按纪律只能给下界，不得呈现为精确倍数")

    @staticmethod
    def _sc_capability_rows(frontiers):
        """能力矩阵表体（主报告卡片与详情页共用同一份渲染，避免两处口径漂移）。"""
        rows = ""
        for model in sorted(frontiers):
            arms = frontiers[model]
            legacy, optimized = arms.get("legacy"), arms.get("optimized")
            legacy_text, legacy_note = ReportGenerator._sc_frontier_text(legacy)
            opt_text, opt_note = ReportGenerator._sc_frontier_text(optimized)
            gain_text, gain_note = ReportGenerator._sc_gain_text(legacy, optimized)
            ref = optimized or legacy
            point = (ref or {}).get("point") or {}
            tokens_out, ctx = point.get("tokens_out"), point.get("context_size")
            if isinstance(tokens_out, int) and isinstance(ctx, int) and ctx > 0:
                ctx_text = f"{tokens_out / ctx * 100:.1f}%<br><small>{tokens_out} / {ctx} tokens</small>"
            else:
                ctx_text = "N/A"
            kept, total = point.get("skills_kept"), point.get("skills_total")
            if isinstance(kept, int) and isinstance(total, int) and total > 0:
                keep_text = f"{kept / total * 100:.0f}%<br><small>{kept} / {total}</small>"
            else:
                keep_text = "N/A"
            trials = point.get("trials")
            repeat_text = f"repeat={trials}" if isinstance(trials, int) else "repeat=?"
            note_bits = [b for b in (
                (f"legacy: {legacy_note}" if legacy_note else ""),
                (f"optimized: {opt_note}" if opt_note else ""),
                (f"倍数: {gain_note}" if gain_note else ""),
            ) if b]
            note_html = (f'<div class="resp-detail" style="margin-top:4px;">{_html_escape("；".join(note_bits))}</div>'
                         if note_bits else "")
            rows += f"""<tr>
                <td><strong>{_html_escape(model)}</strong><div class="resp-detail">{_html_escape(repeat_text)}</div>{note_html}</td>
                <td class="center">{legacy_text}</td>
                <td class="center"><strong>{opt_text}</strong></td>
                <td class="center">{gain_text}</td>
                <td class="center">{keep_text}</td>
                <td class="center">{ctx_text}</td>
            </tr>"""
        return rows

    _SC_CAPABILITY_HEADER = ("<thead><tr><th>模型</th><th class=\"center\">改造前前沿<br><small>legacy</small></th>"
                             "<th class=\"center\">改造后前沿<br><small>optimized</small></th>"
                             "<th class=\"center\">提升倍数</th><th class=\"center\">技能保留率<br><small>前沿点 Kept/Total</small></th>"
                             "<th class=\"center\">上下文占用率<br><small>前沿点 Tokens-Out/Context</small></th></tr></thead>")

    @staticmethod
    def _sc_curve_table_html(entry):
        """单档二分搜索曲线：N × 判据 × 全部账本字段（含 422 与紧急截断）。"""
        rows = ""
        for e in entry["curve"]:
            err = e.get("error") or ""
            is_422 = "422" in err
            def _flag(v, good="是", bad="否"):
                if v is None:
                    return '<span class="status-skip">未知</span>'
                return (f'<span class="status-pass">{good}</span>' if v
                        else f'<span class="status-fail">{bad}</span>')
            verdict = ('<span class="badge pass">PASS</span>' if e.get("passed")
                       else ('<span class="badge ignored">PARTIAL</span>' if e.get("partial")
                             else '<span class="badge fail">FAIL</span>'))
            is_frontier = (e.get("n") == entry["frontier"])
            row_style = ' style="background: rgba(25,118,210,0.08);"' if is_frontier else ""
            kept, total = e.get("skills_kept"), e.get("skills_total")
            kept_text = f"{kept} / {total}"
            if isinstance(kept, int) and isinstance(total, int) and kept < total:
                kept_text += ' <span class="status-fail">(&lt;)</span>'
            tiers = "/".join("?" if e.get(k) is None else str(e.get(k))
                             for k in ("skills_l2", "skills_l1", "skills_l0"))
            tokens_out, ctx = e.get("tokens_out"), e.get("context_size")
            occupancy = (f"{tokens_out} / {ctx}" if tokens_out is not None and ctx is not None else "N/A")
            rows += f"""<tr{row_style}>
                <td class="num"><strong>{e.get('n')}</strong>{' &#9733;' if is_frontier else ''}</td>
                <td class="center">{verdict}</td>
                <td class="center">{_flag(e.get('answered'))}</td>
                <td class="center">{_flag(e.get('picked'))}</td>
                <td class="center">{_flag(e.get('target_in_prompt'))}</td>
                <td class="center"><code>{_html_escape(str(e.get('target_form') or '-'))}</code></td>
                <td class="center">{kept_text}</td>
                <td class="center">{_html_escape(tiers)}</td>
                <td class="center">{_html_escape(str(e.get('skills_budget_tokens')))}</td>
                <td class="center">{_html_escape(occupancy)}</td>
                <td class="center">{_flag(e.get('emergency_truncated'), good='是', bad='否')}</td>
                <td class="center">{'<span class="status-fail">422</span>' if is_422 else '-'}</td>
                <td class="resp-detail">{_html_escape(err[:200])}</td>
            </tr>"""
        head = ("<thead><tr><th>N</th><th class=\"center\">判定</th><th class=\"center\">答对</th>"
                "<th class=\"center\">选对</th><th class=\"center\">目标在提示词</th><th class=\"center\">目标披露形态</th>"
                "<th class=\"center\">Skills<br>Kept/Total</th><th class=\"center\">L2/L1/L0</th>"
                "<th class=\"center\">Skills<br>Budget-Tokens</th><th class=\"center\">Tokens-Out<br>/ Context-Size</th>"
                "<th class=\"center\">紧急截断</th><th class=\"center\">422</th><th>错误原文</th></tr></thead>")
        return f'<table class="compare-table">{head}<tbody>{rows}</tbody></table>'

    @staticmethod
    def _generate_skill_capacity_detail_html(all_results, out_dir, remote_mode=False, cmdline=""):
        """生成技能装载能力前沿的完整详情页(report_skill_capacity.html)。

        与 _generate_multi_backend_detail_html 同一范式：主报告只留能力矩阵卡片 +
        链接，二分搜索的完整曲线（每个 N 的判据与全部账本字段）、表述纪律与追溯、
        同分顺序病理、逐项检查记录全部下沉到本页。仅在本次运行确实产生了
        SKILL_CAPACITY 用例时才生成并返回 True，调用方据此决定是否渲染链接。
        """
        sc_results = [r for r in all_results if r.name.startswith("SKILL_CAPACITY:")]
        if not sc_results:
            return False

        frontiers = ReportGenerator._sc_collect_frontiers(all_results)
        matrix_html = (f'<table class="compare-table">{ReportGenerator._SC_CAPABILITY_HEADER}'
                       f'<tbody>{ReportGenerator._sc_capability_rows(frontiers)}</tbody></table>'
                       if frontiers else
                       '<div class="empty-state">本次未产生任何前沿搜索结果（可能因模型/配置缺失被精确跳过）</div>')

        curves_html = ""
        for model in sorted(frontiers):
            sections = ""
            for arm in ("legacy", "optimized"):
                entry = frontiers[model].get(arm)
                if not entry:
                    continue
                label = ReportGenerator._SC_ARM_LABEL.get(arm, arm)
                note = _html_escape(entry["note"]) if entry["note"] else ""
                note_html = (f'<div class="resp-detail" style="margin: -4px 0 8px;">{note}</div>'
                             if note else "")
                sections += (f'<div class="group-section"><div class="group-title">'
                             f'{_html_escape(label)} &middot; 前沿值 '
                             f'{ReportGenerator._sc_frontier_text(entry)[0]}（曲线 {len(entry["curve"])} 点，'
                             f'&#9733; 标记前沿点）</div>{note_html}'
                             f'{ReportGenerator._sc_curve_table_html(entry)}</div>')
            for arm in sorted(set(frontiers[model]) - {"legacy", "optimized"}):
                entry = frontiers[model][arm]
                sections += (f'<div class="group-section"><div class="group-title">'
                             f'{_html_escape(arm)}</div>'
                             f'{ReportGenerator._sc_curve_table_html(entry)}</div>')
            curves_html += (f'<div class="model-section"><h3 style="font-size: 15px; margin: 4px 0 10px;">'
                            f'{_html_escape(model)}</h3>{sections}</div>')
        if not curves_html:
            curves_html = '<div class="empty-state">本次未产生二分搜索曲线</div>'

        check_rows = ""
        for r in sc_results:
            if r.skipped:
                status_text, status_class = "SKIP", "skip"
            elif r.crashed:
                status_text, status_class = "CRASH", "crash"
            elif r.passed:
                status_text, status_class = "PASS", "pass"
            elif getattr(r, "ignorable", False):
                status_text, status_class = "IGN", "ignored"
            else:
                status_text, status_class = "FAIL", "fail"
            check_rows += f"""<tr>
                <td><code>{_html_escape(r.name)}</code></td>
                <td class="center"><span class="badge {status_class}">{status_text}</span></td>
                <td>{_html_escape(r.detail)}</td>
            </tr>"""
        checks_html = (f'<table class="compare-table"><thead><tr><th>用例</th><th class="center">状态</th>'
                       f'<th>详情</th></tr></thead><tbody>{check_rows}</tbody></table>')

        # Builder 真实驱动端到端确认（--skill_capacity_mode builder/both 才会产生）。
        # 背书边界的措辞按外部审查逐条收紧过：这条通道验证的是「由真实 QAIModelBuilder
        # 启动并运行的 GenieAPIService 实例在 N 上仍选对 + 答对」，**不**验证 Builder 的
        # 聊天协议/请求组帧/tools 注入/日志链路——因为容量池的 N 个合成 <available_skills>
        # 与自定义 tools 结构性无法经 Builder 聊天帧协议注入，请求只能直发服务端口。
        # 全部文案取自该用例 response_data 的 known_limitations，报告层不另写一份结论。
        builder_html = ""
        b_res = next((r for r in sc_results
                      if (r.response_data or {}).get("builder_e2e")), None)
        if b_res is not None:
            b = b_res.response_data or {}
            ev = b.get("evidence") or {}
            limits = "".join(f"<li>{_html_escape(str(x))}</li>"
                             for x in (b.get("known_limitations") or []))
            builder_html = f"""<div class="section">
<h2><span class="icon">&#128279;</span> Builder 真实驱动端到端确认（N={b.get('n')}）</h2>
<ul class="sc-note">
<li><strong>判定</strong>：选对={ev.get('picked')} / 答对={ev.get('answered')}；
<code>Skills-Kept/Total={ev.get('skills_kept')}/{ev.get('skills_total')}</code>、
<code>L2/L1/L0={ev.get('skills_l2')}/{ev.get('skills_l1')}/{ev.get('skills_l0')}</code>、
<code>Tokens-Out/Context={ev.get('tokens_out')}/{ev.get('context_size')}</code>。</li>
<li><strong>Builder 侧实测</strong>：<code>settings.auth.enabled={b.get('auth_enabled')}</code>
（未修改 Builder 任何源码）；Builder 拉起的真实命令行
<code>{_html_escape(str(b.get('builder_command')))}</code>；本次可观测账本字段
{_html_escape(str(b.get('ledger_headers_observed')))}。</li>
{limits}
</ul>
</div>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>技能装载能力前沿 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
.sc-note {{ margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.75; color: var(--text-secondary); }}
.sc-note li {{ margin-bottom: 6px; }}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Skill Capacity Frontier Detail</div>
        <h1>技能装载能力前沿 &middot; 详细记录</h1>
        <p class="subtitle">二分搜索完整曲线、账本字段与表述纪律</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

<div class="section">
<h2><span class="icon">&#128202;</span> 能力矩阵（模型 &times; 改造前/后前沿值 &times; 保留率 &times; 上下文占用 &times; 提升倍数）</h2>
<div class="resp-detail" style="margin: -4px 0 10px;">前沿值 = 该档位下「同时装载 N 个技能仍能选对目标技能并答出其正文里唯一答案码」的最大 N（二分搜索求得，前沿点已用 &#9733; 标注在下方曲线里）。倍数只在两档均收敛且前沿点未预算饱和时给精确值，否则一律呈现为「&ge;N 下界」。</div>
{matrix_html}
</div>

<div class="section">
<h2><span class="icon">&#128207;</span> 表述纪律与追溯</h2>
{ReportGenerator._SC_DISCIPLINE_HTML}
</div>

<div class="section">
<h2><span class="icon">&#128200;</span> 二分搜索完整曲线</h2>
<div class="resp-detail" style="margin: -4px 0 10px;">「目标披露形态」取值 L2（全量条目）/ L1（摘要 + 触发条件）/ L0（仅名字 + 一行摘要）/ absent（未进最终提示词）；判据由目标 description 尾部唯一 canary 是否可见机械判定，不靠人工读日志。</div>
{curves_html}
</div>

<div class="section">
<h2><span class="icon">&#9878;</span> 同分顺序病理（独立结论，不参与容量倍数）</h2>
{ReportGenerator._SC_TIE_PATHOLOGY_HTML}
</div>

{builder_html}

<div class="section">
<h2><span class="icon">&#128203;</span> 逐项用例记录（{len(sc_results)}）</h2>
{checks_html}
</div>

<div class="footer">
    技能装载能力前沿详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "技能装载前沿详情页 report_skill_capacity.html")
        out_path = Path(out_dir) / "report_skill_capacity.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 技能装载前沿详情: {out_path}")
        return True

    @staticmethod
    def _crash_event_anchor(idx):
        return f"crash-{idx}"

    @staticmethod
    def _generate_crash_events_detail_html(crash_events, out_dir, remote_mode=False, cmdline=""):
        """生成崩溃事件的完整详情页(report_crash_events.html)。

        承载主报告/模型报告不适合展示的高密度内容——每条崩溃完整的进程 stderr/stdout
        尾部原始文本(默认展开)。主报告/模型报告只保留次数 + 逐条极简摘要(时间/模型/轮次/
        端点/详情,即已含人类可读退出码诊断的 detail 字段)+ 跳转到本页对应锚点的链接,
        与"多类型同时加载"下沉子报告的既有模式一致。仅在存在崩溃事件时才生成。
        """
        if not crash_events:
            return False
        rows_html = ""
        for idx, c in enumerate(crash_events):
            log_tail = getattr(c, "log_tail", "")
            log_tail_html = (f'<details open><summary>查看</summary><pre class="startup-detail">{_html_escape(log_tail)}</pre></details>'
                              if log_tail else "-")
            rows_html += f"""<tr id="{ReportGenerator._crash_event_anchor(idx)}">
                <td>{_html_escape(c.timestamp)}</td>
                <td>{_html_escape(c.model_name)}</td><td class="center">{c.round_num}</td>
                <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td>
                <td>{_html_escape(c.detail)}</td>
                <td>{log_tail_html}</td>
            </tr>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>崩溃事件 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Crash Events Detail</div>
        <h1>崩溃事件 &middot; 详细记录</h1>
        <p class="subtitle">全部崩溃事件的完整进程日志尾部</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

<div class="section">
<h2><span class="icon">&#9888;</span> 崩溃事件日志（{len(crash_events)}）</h2>
<table class="crash-table"><thead><tr>
    <th>时间</th><th>模型</th><th>轮次</th><th>端点</th><th>详情</th><th>进程日志尾部</th></tr></thead><tbody>
{rows_html}
</tbody></table>
</div>

<div class="footer">
    崩溃事件详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "崩溃事件详情页 report_crash_events.html")
        out_path = Path(out_dir) / "report_crash_events.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 崩溃事件详情: {out_path}")
        return True

    @staticmethod
    def _tool_call_anchor(idx):
        return f"toolcall-{idx}"

    @staticmethod
    def _generate_tool_calls_detail_html(toolcalls_results, out_dir, remote_mode=False, cmdline=""):
        """生成 tool_calls/function calling 协议测试的详情页(report_tool_calls.html)。

        承载这四个测试真正的诊断价值——工具调用数组结构、流式跨 chunk 重建后的
        arguments——而不是依赖通用的 chat/completions 子串匹配产出的空洞对话卡片
        (那条通用路径既不展示 response_data,也会与同一轮真正的 chat 测试发生锚点冲突)。
        主报告/模型报告只保留通过/跳过计数 + 跳转链接。
        """
        if not toolcalls_results:
            return False
        cards_html = ""
        for idx, r in enumerate(toolcalls_results):
            cls, txt, extra_badges = ReportGenerator._case_status_badge_html(r)
            payload_html = ""
            if r.response_data:
                payload_html = f'<pre class="startup-detail">{_html_escape(json.dumps(r.response_data, indent=2, ensure_ascii=False))}</pre>'
            elif r.text_response:
                payload_html = f'<div class="resp-detail">{_html_escape(r.text_response)}</div>'
            cards_html += f"""<div class="section" id="{ReportGenerator._tool_call_anchor(idx)}">
            <div class="conv-meta">
                <span class="model-tag">{_html_escape(r.model_name)}</span>
                <span class="round-tag">Round {r.round_num}</span>
                <span class="{cls}">{txt}</span>{extra_badges}
            </div>
            <h3 style="font-size: 14px; margin: 8px 0;">{_html_escape(r.name)}</h3>
            <div class="resp-detail">{_html_escape(r.detail)}</div>
            {payload_html}
            </div>"""

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tool_calls 协议测试 - 详细记录</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Tool Calls Protocol Detail</div>
        <h1>tool_calls 协议测试 &middot; 详细记录</h1>
        <p class="subtitle">每条测试的完整工具调用结构 / 流式重建后的 arguments</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>模式:</strong> {"远程" if remote_mode else "本地"}</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">
{cards_html}
<div class="footer">
    tool_calls 协议测试详细记录 &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "tool_calls 协议详情页 report_tool_calls.html")
        out_path = Path(out_dir) / "report_tool_calls.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] tool_calls 协议详情: {out_path}")
        return True

    @staticmethod
    def generate_summary_html(all_results, perf_data, crash_events, out_dir, remote_mode=False, suite_name="full", cmdline=""):
        """生成总汇报 HTML — 仅包含模型无关测试 + 性能对比汇总"""
        # 排除全部内部占位 model_name（_global_/_multi_model_ 等），只保留真实模型；
        # 统一复用 _is_internal_placeholder_model()，不要在这里再手写排除元组（详见其定义处注释）。
        model_names = sorted(set(r.model_name for r in all_results
                                 if not ReportGenerator._is_internal_placeholder_model(r.model_name)))
        multi_model_results = [r for r in all_results if r.model_name == "_multi_model_"]
        rounds = sorted(set(r.round_num for r in all_results))
        num_rounds = len(rounds)

        # 模型无关接口数据：优先使用 _global_ 的结果，否则回退到第一个真实模型的数据
        independent_results = [r for r in all_results
                               if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS
                               and r.model_name == "_global_"]
        if not independent_results and model_names:
            independent_results = [r for r in all_results
                                   if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS
                                   and r.model_name == model_names[0]]

        # 总计（仅统计模型无关测试用于总汇报）
        all_independent = [r for r in all_results if r.name in ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS]
        # 去重：同一端点同一轮只保留第一个模型的结果
        seen = set()
        unique_independent = []
        for r in all_independent:
            key = (r.name, r.round_num)
            if key not in seen:
                seen.add(key)
                unique_independent.append(r)

        summary_ind = {"total": len(unique_independent), "passed": 0, "failed": 0, "crashed": 0, "skipped": 0}
        for r in unique_independent:
            if r.skipped:
                summary_ind["skipped"] += 1
            elif r.crashed:
                summary_ind["crashed"] += 1
            elif r.passed:
                summary_ind["passed"] += 1
            else:
                summary_ind["failed"] += 1

        # 全局统计（所有测试，用于顶部展示）
        summary_all = {"total": 0, "passed": 0, "failed": 0, "crashed": 0, "skipped": 0, "ignored": 0, "model_capability_issues": 0}
        for r in all_results:
            summary_all["total"] += 1
            if r.skipped:
                summary_all["skipped"] += 1
            elif r.crashed:
                summary_all["crashed"] += 1
            elif r.passed:
                summary_all["passed"] += 1
            else:
                summary_all["failed"] += 1
                if getattr(r, "ignorable", False):
                    summary_all["ignored"] += 1
            if getattr(r, "model_capability_issue", False):
                summary_all["model_capability_issues"] += 1
        pass_rate_all = (summary_all["passed"] / summary_all["total"] * 100) if summary_all["total"] > 0 else 0

        # 模型无关接口的透视表（只使用全局测试的轮次，通常只有 1 轮）
        independent_rounds = sorted(set(r.round_num for r in independent_results)) if independent_results else rounds
        independent_table = ReportGenerator._build_pivot_table(
            independent_results, ReportGenerator.MODEL_INDEPENDENT_ENDPOINTS, independent_rounds
        )

        # 性能对比表（合并原"跨模型推理对比"和"性能时序监控"）
        # 指标：avg prompt_processing_rate, avg token_generation_rate, avg CPU, avg Memory
        # 注意：排除 _multi_model_（阶段3多模型路由测试的占位名），它不产生 /profile 或推理性能数据，
        # 混入对比表只会出现一行全 N/A，没有意义。
        perf_compare_model_names = [m for m in model_names if m != "_multi_model_"]
        model_compare_html = ""
        if len(perf_compare_model_names) > 0:
            model_compare_html = """<div class="section">
            <h2><span class="icon">&#128200;</span> 性能对比</h2>
            <table class="compare-table">
            <thead><tr><th>模型</th><th>平均 Prompt 处理速率<br><small>(tokens/s)</small></th><th>平均 Token 生成速率<br><small>(tokens/s)</small></th><th>推理平均 CPU<br><small>(%)</small></th><th>推理平均内存<br><small>(MB)</small></th></tr></thead><tbody>"""
            for model in perf_compare_model_names:
                model_results = [r for r in all_results if r.model_name == model]

                # 从多轮 /profile 结果提取 prompt_processing_rate 和 token_generation_rate
                profile_results = [r for r in model_results
                                   if r.name == "GET /profile" and r.passed and r.response_data]
                ppr_values = []
                tgr_values = []
                for pr in profile_results:
                    try:
                        ppr = float(pr.response_data.get("prompt_processing_rate", 0))
                        if ppr > 0:
                            ppr_values.append(ppr)
                    except (ValueError, TypeError):
                        pass
                    try:
                        tgr = float(pr.response_data.get("token_generation_rate", 0))
                        if tgr > 0:
                            tgr_values.append(tgr)
                    except (ValueError, TypeError):
                        pass
                avg_ppr = (sum(ppr_values) / len(ppr_values)) if ppr_values else 0
                avg_tgr = (sum(tgr_values) / len(tgr_values)) if tgr_values else 0

                # 从推理结果的 perf_before/perf_after 提取 CPU 和内存
                _chat_names = set(ReportGenerator.MODEL_PERF_ENDPOINTS) - {"GET /profile"}
                chat_results = [r for r in model_results
                                if r.name in _chat_names
                                and r.passed]
                cpu_values = []
                mem_values = []
                for cr in chat_results:
                    if cr.perf_after and cr.perf_after.get("cpu_percent", 0) > 0:
                        # 展示层归一化：psutil 原始值以单核=100%计,MNN 等多线程后端跑满
                        # 多个核心时原始值可能远超100%(如700+%),反直觉;归一化为相对整机
                        # 总算力的百分比再展示,不影响 results.json 里保存的原始采样值。
                        cpu_values.append(_normalize_cpu_percent(cr.perf_after["cpu_percent"]))
                    if cr.perf_after and cr.perf_after.get("rss_mb", 0) > 0:
                        mem_values.append(cr.perf_after["rss_mb"])
                avg_cpu = (sum(cpu_values) / len(cpu_values)) if cpu_values else 0
                avg_mem = (sum(mem_values) / len(mem_values)) if mem_values else 0

                ppr_str = f"{avg_ppr:.1f}" if avg_ppr > 0 else "N/A"
                tgr_str = f"{avg_tgr:.1f}" if avg_tgr > 0 else "N/A"
                cpu_str = f"{avg_cpu:.1f}" if avg_cpu > 0 else "N/A"
                mem_str = f"{avg_mem:.0f}" if avg_mem > 0 else "N/A"
                model_compare_html += f"""<tr>
                    <td><strong>{_html_escape(model)}</strong></td>
                    <td class="center">{ppr_str}</td>
                    <td class="center">{tgr_str}</td>
                    <td class="center">{cpu_str}</td>
                    <td class="center">{mem_str}</td>
                </tr>"""
            model_compare_html += "</tbody></table></div>"

        # 多类型同时加载摘要：主报告只保留极简密度(通过/跳过计数 + 共存矩阵的短原因)，
        # 与其它区块保持一致；逐项检查记录、每个组合的完整失败原因(如 insufficient_memory
        # 的字节数明细)全部移至独立详情页,由 _generate_multi_backend_detail_html 生成。
        multi_backend_html = ""
        loaded_entries = []
        seen_loaded = set()
        for r in multi_model_results:
            for entry in (r.response_data or {}).get("loaded_models", []):
                key = (entry.get("id", ""), entry.get("backend", ""), entry.get("device", ""))
                if key not in seen_loaded:
                    seen_loaded.add(key)
                    loaded_entries.append(entry)
        if multi_model_results or loaded_entries:
            multi_pass = sum(1 for r in multi_model_results if r.passed)
            multi_skipped = sum(1 for r in multi_model_results if r.skipped)
            multi_crashed = sum(1 for r in multi_model_results if r.crashed)
            multi_total = len(multi_model_results)
            # 失败数 = 总数 - 通过 - 跳过 - 崩溃，与顶部全局汇总卡片(summary_all)的口径一致。
            multi_fail = multi_total - multi_pass - multi_skipped - multi_crashed
            device_types = sorted({entry.get("device") or "unknown" for entry in loaded_entries})

            has_detail_page = ReportGenerator._generate_multi_backend_detail_html(
                multi_model_results, loaded_entries, out_dir, remote_mode=remote_mode, cmdline=cmdline)

            loaded_rows = ""
            for entry in loaded_entries:
                loaded_rows += f"""<tr>
                    <td>{_html_escape(entry.get('id', ''))}</td>
                    <td><code>{_html_escape(entry.get('backend', ''))}</code></td>
                    <td><code>{_html_escape(entry.get('device', ''))}</code></td>
                    <td class=\"num\">{_html_escape(str(entry.get('context_length', '')))}</td>
                </tr>"""
            loaded_table_html = (f'<table class="compare-table"><thead><tr><th>模型</th><th>后端</th><th>设备</th><th>上下文</th></tr></thead><tbody>{loaded_rows}</tbody></table>'
                                  if loaded_rows else '<div class="empty-state">未捕获到已加载模型的 backend/device 明细</div>')

            # 后端共存矩阵：主报告只展示极短原因标签(如"内存不足,已跳过")，完整原始详情
            # (如 insufficient_memory 的字节数明细)在详情页的同名矩阵表格里。
            coexistence_html = ""
            matrix_result = next(
                (r for r in multi_model_results
                 if r.name == "MULTI: backend coexistence matrix (triple + pairwise fallback)"),
                None
            )
            if matrix_result and matrix_result.response_data and matrix_result.response_data.get("coexistence_matrix"):
                cm = matrix_result.response_data["coexistence_matrix"]
                _CM_STATUS_CLASS = {"pass": "status-pass", "fail": "status-fail",
                                     "skip": "status-skip", "unknown": "status-skip"}
                _CM_STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "unknown": "N/A"}
                _CM_PAIR_LABEL = {"npu+gpu": "QNN/NPU + GGUF/GPU", "npu+cpu": "QNN/NPU + MNN/CPU",
                                   "gpu+cpu": "GGUF/GPU + MNN/CPU"}
                triple = cm["triple"]
                triple_badge = f'<span class="{_CM_STATUS_CLASS.get(triple["status"], "status-skip")}">{_CM_STATUS_TEXT.get(triple["status"], triple["status"].upper())}</span>'
                pair_rows = ""
                for pair_key, pair_info in cm["pairs"].items():
                    p_status = pair_info["status"]
                    p_badge = f'<span class="{_CM_STATUS_CLASS.get(p_status, "status-skip")}">{_CM_STATUS_TEXT.get(p_status, p_status.upper())}</span>'
                    pair_rows += f"""<tr>
                        <td>{_html_escape(_CM_PAIR_LABEL.get(pair_key, pair_key))}</td>
                        <td class=\"center\">{p_badge}</td>
                        <td>{_html_escape(pair_info.get("reason", ""))}</td>
                    </tr>"""
                coexistence_html = f"""<h3 style=\"font-size: 15px; margin: 18px 0 10px;\">后端共存矩阵</h3>
                <table class=\"compare-table\"><thead><tr><th>组合</th><th class=\"center\">状态</th><th>原因</th></tr></thead><tbody>
                <tr><td><strong>三后端同时驻留</strong></td><td class=\"center\">{triple_badge}</td><td>{_html_escape(triple.get("reason", ""))}</td></tr>
                {pair_rows}
                </tbody></table>"""

            multi_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {multi_fail} 项失败，详见下方逐项检查记录</div>'
                if multi_fail > 0 else "")
            detail_link_html = (
                f'{multi_fail_note_html}<div class="model-link-card"><a href="report_multi_backend_coexistence.html">查看完整共存矩阵与逐项检查记录 →</a></div>'
                if has_detail_page else multi_fail_note_html)

            multi_backend_html = f"""<div class=\"section\">
            <h2><span class=\"icon\">&#129520;</span> 多类型同时加载</h2>
            <div class=\"summary-bar\">
                <div class=\"summary-card\"><div class=\"num\">{multi_total}</div><div class=\"label\">检查项</div></div>
                <div class=\"summary-card card-pass\"><div class=\"num\">{multi_pass}</div><div class=\"label\">通过</div></div>
                <div class=\"summary-card\"><div class=\"num\">{multi_skipped}</div><div class=\"label\">跳过</div></div>
                <div class=\"summary-card\"><div class=\"num\">{len(device_types)}</div><div class=\"label\">设备类型</div></div>
            </div>
            <h3 style=\"font-size: 15px; margin: 18px 0 10px;\">已加载 backend/device</h3>
            {loaded_table_html}
            {coexistence_html}
            {detail_link_html}
            </div>"""

        # 技能装载能力前沿（--suite skill_capacity）：主报告只保留能力矩阵卡片
        # （模型 × 改造前/后前沿值 × 提升倍数 × 保留率 × 上下文占用率）+ 指向详情页
        # 的链接，二分搜索完整曲线与表述纪律全部下沉到 report_skill_capacity.html，
        # 与"多类型同时加载"的下沉模式一致。倍数的可引用性由 _sc_gain_text() 按
        # usable_for_gain_ratio 机械判定，报告层不做任何数值硬编码。
        skill_capacity_html = ""
        sc_results = [r for r in all_results if r.name.startswith("SKILL_CAPACITY:")]
        if sc_results:
            has_sc_detail = ReportGenerator._generate_skill_capacity_detail_html(
                all_results, out_dir, remote_mode=remote_mode, cmdline=cmdline)
            sc_frontiers = ReportGenerator._sc_collect_frontiers(all_results)
            sc_pass = sum(1 for r in sc_results if r.passed)
            sc_skipped = sum(1 for r in sc_results if r.skipped)
            sc_crashed = sum(1 for r in sc_results if r.crashed)
            sc_fail = len(sc_results) - sc_pass - sc_skipped - sc_crashed
            sc_matrix_html = (
                f'<table class="compare-table">{ReportGenerator._SC_CAPABILITY_HEADER}'
                f'<tbody>{ReportGenerator._sc_capability_rows(sc_frontiers)}</tbody></table>'
                if sc_frontiers else
                '<div class="empty-state">本次未产生前沿搜索结果（模型或配置缺失时按设计精确跳过，不伪造数字）</div>')
            sc_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {sc_fail} 项失败，详见详情页逐项用例记录</div>'
                if sc_fail > 0 else "")
            sc_link_html = (
                f'{sc_fail_note_html}<div class="model-link-card"><a href="report_skill_capacity.html">查看二分搜索完整曲线、表述纪律与同分顺序病理 →</a></div>'
                if has_sc_detail else sc_fail_note_html)
            # Builder 端到端确认的一行摘要。措辞按外部审查收紧：只说「由真实 Builder
            # 启动并运行的服务实例在 N 上仍选对+答对」，绝不写成「已由 Builder 链路
            # 端到端确认」——那会被读成 Builder 的聊天/转发链路也被覆盖，而实测请求
            # 是直发服务端口的（Builder 在请求面已退出因果链）。
            sc_builder_note_html = ""
            b_res = next((r for r in sc_results if (r.response_data or {}).get("builder_e2e")), None)
            if b_res is not None:
                b = b_res.response_data or {}
                bev = b.get("evidence") or {}
                sc_builder_note_html = (
                    f'<div class="resp-detail" style="margin: 8px 0 0;">'
                    f'<strong>Builder 真实驱动端到端确认</strong>：由真实 QAIModelBuilder 启动并运行的 '
                    f'GenieAPIService 实例，在 N={b.get("n")} 上选对={bev.get("picked")} / '
                    f'答对={bev.get("answered")}（<code>settings.auth.enabled='
                    f'{b.get("auth_enabled")}</code>，未修改 Builder 源码）。'
                    f'该确认<strong>不覆盖</strong> Builder 自身的聊天协议 / 请求组帧 / tools 注入 / '
                    f'日志链路——容量池的合成 <code>&lt;available_skills&gt;</code> 与自定义 '
                    f'<code>tools</code> 无法经 Builder 聊天帧协议注入，验证请求为直发服务端口。</div>')
            skill_capacity_html = f"""<div class="section">
            <h2><span class="icon">&#128202;</span> 技能装载能力前沿（能力矩阵）</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{len(sc_results)}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{sc_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{sc_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{sc_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{sc_skipped}</div><div class="label">跳过</div></div>
            </div>
            <div class="resp-detail" style="margin: 4px 0 10px;">前沿值 = 该档位下「同时装载 N 个技能仍能选对目标技能并答出其正文里唯一答案码」的最大 N。提升倍数只在两档均收敛（converged）且前沿点未预算饱和时给精确值，否则一律呈现为「&ge;N 下界」；采样次数见「模型」列，<strong>repeat=1 时未做重复性检验，只能表述为「效应量极大且方向明确」，不声称统计显著</strong>。</div>
            {sc_matrix_html}
            {sc_builder_note_html}
            {sc_link_html}
            </div>"""

        # 模型列表（带链接）
        model_links_html = '<div class="section"><h2><span class="icon">&#128218;</span> 详细报告</h2>'
        for model in model_names:
            model_results = [r for r in all_results if r.model_name == model]
            m_pass = sum(1 for r in model_results if r.passed)
            m_total = len(model_results)
            m_rate = (m_pass / m_total * 100) if m_total > 0 else 0
            safe_model_name = model.replace(" ", "_").replace("/", "_")
            model_links_html += f"""<div class="model-link-card">
                <a href="report_{safe_model_name}.html">{_html_escape(model)}</a>
                <div class="model-link-stats">
                    <span class="stat-pass">{m_pass}/{m_total} 通过</span>
                    <span class="stat-rate">{m_rate:.0f}%</span>
                </div>
            </div>"""
        model_links_html += '</div>'

        # 崩溃日志：主报告只保留次数 + 逐条极简摘要(时间/模型/轮次/端点/detail 里已含的人类可读
        # 退出码诊断),完整 stderr/stdout 尾部原始文本下沉到 report_crash_events.html,
        # 与"多类型同时加载"的下沉模式一致，避免主报告体积随崩溃数量无上限增长。
        has_crash_detail_page = ReportGenerator._generate_crash_events_detail_html(
            crash_events, out_dir, remote_mode=remote_mode, cmdline=cmdline)
        crash_html = ""
        if crash_events:
            crash_html = """<div class="section"><h2><span class="icon">&#9888;</span> 崩溃事件日志</h2>
            <table class="crash-table"><thead><tr>
                <th>时间</th><th>模型</th><th>轮次</th><th>端点</th><th>详情</th><th>完整日志</th></tr></thead><tbody>"""
            for idx, c in enumerate(crash_events):
                detail_link = (f'<a href="report_crash_events.html#{ReportGenerator._crash_event_anchor(idx)}">查看 →</a>'
                                if has_crash_detail_page else "-")
                crash_html += f"""<tr><td>{_html_escape(c.timestamp)}</td>
                    <td>{_html_escape(c.model_name)}</td><td class="center">{c.round_num}</td>
                    <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td><td>{_html_escape(c.detail)}</td>
                    <td>{detail_link}</td></tr>"""
            crash_html += "</tbody></table></div>"

        # MNN 内存容错统计块：区分"MNN 自身真实 OOM"(MNN_OOM_CRASH)与"疑似因 MNN OOM 拖垮进程而
        # 级联崩溃的其它后端"(MNN_OOM_CASCADE)。两者都是真实问题、都已计入 failed/crashed 统计——
        # 级联受累计数不再代表豁免，只是一个诊断性提示，提醒人工去排查这是否是一个需要单独修复的
        # 真实级联崩溃 bug（例如共享状态未隔离），而不是把它当作可以放行的"环境限制"。
        mnn_self_oom_events = [c for c in crash_events if c.endpoint == "MNN_OOM_CRASH"]
        mnn_cascade_events = [c for c in crash_events if c.endpoint == "MNN_OOM_CASCADE"]
        auto_restarted_count = sum(1 for r in all_results if getattr(r, "auto_restarted", False))
        mnn_html = ""
        if mnn_self_oom_events or mnn_cascade_events or auto_restarted_count > 0:
            mnn_html = f"""<div class="section"><h2><span class="icon">&#128202;</span> MNN 内存容错统计</h2>
            <div class="summary-bar">
                <div class="summary-card card-crash"><div class="num">{len(mnn_self_oom_events)}</div><div class="label">MNN 自身真实 OOM</div></div>
                <div class="summary-card card-crash" title="该计数不代表豁免，均已计入上方总的 failed/crashed 统计；仅作诊断提示，建议人工排查是否为需要单独修复的真实级联崩溃 bug"><div class="num">{len(mnn_cascade_events)}</div><div class="label">疑似 MNN OOM 级联崩溃(已计入失败)</div></div>
                <div class="summary-card card-pass"><div class="num">{auto_restarted_count}</div><div class="label">自动恢复次数</div></div>
            </div>
            </div>"""

        # 服务启动失败的醒目横幅 — 只要存在任何 endpoint == "SERVICE_STARTUP" 的事件
        # 就在报告最顶部显示一条红色横幅,把模型、详情、stderr 日志路径都列出来。
        startup_failures = [c for c in crash_events if c.endpoint == "SERVICE_STARTUP"]
        startup_banner_html = ""
        if startup_failures:
            startup_banner_html = '<div class="startup-banner"><div class="startup-banner-title">⚠ 检测到 ' + str(len(startup_failures)) + ' 次服务启动失败</div>'
            startup_banner_html += '<div class="startup-banner-desc">GenieAPIService.exe 在调用 <code>Popen</code> 后 2 秒内立即退出,导致服务从未真正监听端口。常见原因:架构不匹配 (x86-64 host vs ARM64 exe)、缺少 QNN/VC 运行时 DLL、OneDrive 占位文件未下载、模型配置路径错误。请先看 stderr 日志再继续。</div>'
            startup_banner_html += '<table class="startup-table"><thead><tr><th>时间</th><th>模型</th><th>失败详情 (含 exit_code / stderr 摘要)</th></tr></thead><tbody>'
            for c in startup_failures:
                startup_banner_html += f'<tr><td>{_html_escape(c.timestamp)}</td><td>{_html_escape(c.model_name)}</td><td><pre class="startup-detail">{_html_escape(c.detail)}</pre></td></tr>'
            startup_banner_html += '</tbody></table></div>'

        matrix_html = ReportGenerator._build_completeness_matrix(all_results, model_names, suite_name)

        # 独立的 Builder 集成 / SampleApp 明细区块：不再仅仅体现为完备性矩阵里的一个格子。
        # Builder 集成用例的占位 model_name 这里不按 model_names 过滤，确保它们也能在总汇报
        # 里被看到明细，而不是完全依赖每模型报告页面。
        builder_results = [r for r in all_results if r.name.startswith("BUILDER")]
        sampleapp_results = [r for r in all_results if r.name.startswith("SAMPLEAPP:")]
        builder_section_html = ""
        if builder_results:
            builder_total = len(builder_results)
            builder_pass = sum(1 for r in builder_results if r.passed)
            builder_skipped = sum(1 for r in builder_results if r.skipped)
            builder_crashed = sum(1 for r in builder_results if r.crashed)
            # 失败数 = 总数 - 通过 - 跳过 - 崩溃，与顶部全局汇总卡片(summary_all)的口径一致。
            builder_fail = builder_total - builder_pass - builder_skipped - builder_crashed

            builder_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {builder_fail} 项失败，详见每模型报告页面</div>'
                if builder_fail > 0 else "")

            builder_section_html = f"""<div class="section"><h2><span class="icon">&#128295;</span> Builder 集成测试结果</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{builder_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{builder_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{builder_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{builder_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{builder_skipped}</div><div class="label">跳过</div></div>
            </div>
            {builder_fail_note_html}
            </div>"""
        sampleapp_section_html = ""
        if sampleapp_results:
            # 主报告只保留摘要计数(与 Builder 摘要卡片同一写法);完整逐用例明细表只在
            # 每个模型报告(model_sampleapp_html)里保留一份，避免同一份数据被重复渲染两遍。
            sampleapp_total = len(sampleapp_results)
            sampleapp_pass = sum(1 for r in sampleapp_results if r.passed)
            sampleapp_skipped = sum(1 for r in sampleapp_results if r.skipped)
            sampleapp_crashed = sum(1 for r in sampleapp_results if r.crashed)
            sampleapp_fail = sampleapp_total - sampleapp_pass - sampleapp_skipped - sampleapp_crashed

            sampleapp_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {sampleapp_fail} 项失败，详见每模型报告页面</div>'
                if sampleapp_fail > 0 else "")

            sampleapp_section_html = f"""<div class="section"><h2><span class="icon">&#128241;</span> SampleApp 测试结果</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{sampleapp_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{sampleapp_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{sampleapp_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{sampleapp_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{sampleapp_skipped}</div><div class="label">跳过</div></div>
            </div>
            {sampleapp_fail_note_html}
            </div>"""

        # tool_calls/function calling 协议测试：独立分类区块，不再混入模型报告的"其它/专项
        # 测试"兜底大杂烩；完整协议载荷(工具调用数组、流式重建后的 arguments)下沉到
        # report_tool_calls.html，主报告只保留计数 + 跳转链接。
        toolcalls_results = [r for r in all_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
        toolcalls_section_html = ""
        if toolcalls_results:
            tc_total = len(toolcalls_results)
            tc_pass = sum(1 for r in toolcalls_results if r.passed)
            tc_skipped = sum(1 for r in toolcalls_results if r.skipped)
            tc_crashed = sum(1 for r in toolcalls_results if r.crashed)
            tc_fail = tc_total - tc_pass - tc_skipped - tc_crashed

            has_toolcalls_detail_page = ReportGenerator._generate_tool_calls_detail_html(
                toolcalls_results, out_dir, remote_mode=remote_mode, cmdline=cmdline)
            toolcalls_link_html = ('<div style="margin-top: 6px;"><a href="report_tool_calls.html">查看完整协议详情 →</a></div>'
                                    if has_toolcalls_detail_page else "")
            tc_fail_note_html = (
                f'<div style="color: var(--danger); font-size: 12px; font-weight: 600; margin-bottom: 6px;">'
                f'含 {tc_fail} 项失败，详见协议详情页</div>'
                if tc_fail > 0 else "")

            toolcalls_section_html = f"""<div class="section"><h2><span class="icon">&#128268;</span> tool_calls 协议测试</h2>
            <div class="summary-bar">
                <div class="summary-card"><div class="num">{tc_total}</div><div class="label">用例数</div></div>
                <div class="summary-card card-pass"><div class="num">{tc_pass}</div><div class="label">通过</div></div>
                <div class="summary-card card-fail"><div class="num">{tc_fail}</div><div class="label">失败</div></div>
                <div class="summary-card card-crash"><div class="num">{tc_crashed}</div><div class="label">崩溃</div></div>
                <div class="summary-card"><div class="num">{tc_skipped}</div><div class="label">跳过(模型能力局限)</div></div>
            </div>
            {tc_fail_note_html}
            {toolcalls_link_html}
            </div>"""
        graceful_shutdown_results = [r for r in all_results if r.name.startswith("GRACEFUL_SHUTDOWN:")]
        graceful_shutdown_section_html = ""
        if graceful_shutdown_results:
            graceful_shutdown_section_html = f"""<div class="section"><h2><span class="icon">&#9203;</span> 推理中终止进程 → 优雅退出验证</h2>
            <div class="resp-detail" style="margin: -4px 0 10px;">对三种后端各选一个代表模型，在其处理长耗时推理请求期间发送终止信号，验证进程能走既有的关闭逻辑正常退出（日志出现优雅关闭标记且退出码不匹配任何已知 Windows 崩溃特征码），而不是被系统判定为异常崩溃。</div>
            {ReportGenerator._build_case_detail_table(graceful_shutdown_results)}
            </div>"""
        cmdline_html = ReportGenerator._cmdline_meta_html(cmdline)

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_latency = sum(r.latency_ms for r in all_results if not r.skipped)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenieAPIService 测试总汇报 - {now_str}</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Genie API Service · Integration Suite</div>
        <h1>GenieAPIService 集成测试总汇报</h1>
        <p class="subtitle">API Integration Test Summary Report</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>测试模型:</strong> {len(model_names)} 个</span>
            <span class="meta-item"><strong>测试轮数:</strong> {num_rounds}</span>
            <span class="meta-item"><strong>总耗时:</strong> {total_latency/1000:.1f}s</span>
            <span class="meta-item"><strong>模式:</strong> {'远程' if remote_mode else '本地'}</span>
            <span class="meta-item"><strong>测试套件:</strong> {_html_escape(suite_name)}</span>
            {cmdline_html}
        </div>
    </div>
</div>

<div class="container">

{startup_banner_html}

<div class="summary-bar">
    <div class="summary-card"><div class="num">{summary_all['total']}</div><div class="label">总测试数</div></div>
    <div class="summary-card card-pass"><div class="num">{summary_all['passed']}</div><div class="label">通过</div></div>
    <div class="summary-card card-fail"><div class="num">{summary_all['failed']}</div><div class="label">失败</div></div>
    <div class="summary-card card-ignored" title="服务端已知占位/未实现缺陷的 FAIL 子集 (501、占位 400 等); failed 数已包含这些条目, 这里只是单独标出来"><div class="num">{summary_all['ignored']}</div><div class="label">可忽略</div></div>
    <div class="summary-card card-crash"><div class="num">{summary_all['crashed']}</div><div class="label">崩溃</div></div>
    <div class="summary-card"><div class="num">{summary_all['skipped']}</div><div class="label">跳过</div></div>
    <div class="summary-card card-capability" title="根因判定为模型自身能力/输出质量局限而非服务端缺陷的诊断性标注(如重复出词、tool_calls 未按指令触发);诊断性维度,不改变 passed/failed/退出码判定"><div class="num">{summary_all['model_capability_issues']}</div><div class="label">模型能力问题</div></div>
</div>

<div class="progress-bar">
    <div class="progress-fill {'good' if pass_rate_all >= 80 else ('warn' if pass_rate_all >= 50 else 'bad')}" style="width:{max(pass_rate_all, 5):.0f}%">
        <span class="progress-text">通过率 {pass_rate_all:.1f}%</span>
    </div>
</div>
<div class="progress-meta"><span>总体通过率</span><span class="pct">{pass_rate_all:.1f}%</span></div>

<div class="section">
<h2><span class="icon">&#128269;</span> 通用接口测试（模型无关）</h2>
<div class="model-section">
    <div class="group-section">
        {independent_table}
    </div>
</div>
</div>

{model_compare_html}
{multi_backend_html}
{skill_capacity_html}
{model_links_html}
{matrix_html}
{builder_section_html}
{sampleapp_section_html}
{toolcalls_section_html}
{graceful_shutdown_section_html}
{mnn_html}
{crash_html}

<div class="footer">
    GenieAPIService Integration Test Summary &middot; Generated by test_service.py &middot; {now_str}
</div>

</div>
</body>
</html>"""

        ReportGenerator._assert_no_placeholder_leak(html, "总汇报 report.html")
        out_path = Path(out_dir) / "report.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 总汇报 HTML: {out_path}")

    @staticmethod
    def generate_model_reports(all_results, perf_data, crash_events, out_dir, remote_mode=False, cmdline=""):
        """为每个模型生成独立的 HTML 报告"""
        # 排除全部内部占位 model_name（_global_/_multi_model_ 等），只为真实模型生成报告；
        # 统一复用 _is_internal_placeholder_model()，不要在这里再手写排除元组（详见其定义处注释）。
        model_names = sorted(set(r.model_name for r in all_results
                                 if not ReportGenerator._is_internal_placeholder_model(r.model_name)))
        rounds = sorted(set(r.round_num for r in all_results))
        # 与 generate_summary_html._generate_crash_events_detail_html 共用同一份 crash_events
        # 列表(同一批 CrashEvent 对象),用对象身份映射到全局下标,保证本页链接的锚点与
        # report_crash_events.html(由总汇报流程先行生成)里的锚点完全对应。
        crash_idx_by_id = {id(c): idx for idx, c in enumerate(crash_events)}
        # 与 generate_summary_html._generate_tool_calls_detail_html 共用同一份 tool_calls 测试
        # 结果(同一批 TestResult 对象),用对象身份映射到全局下标,道理与上面的崩溃事件映射一致。
        all_toolcalls_results = [r for r in all_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
        toolcalls_idx_by_id = {id(r): idx for idx, r in enumerate(all_toolcalls_results)}

        for model in model_names:
            model_results = [r for r in all_results if r.model_name == model]
            model_crashes = [c for c in crash_events if c.model_name == model]

            m_pass = sum(1 for r in model_results if r.passed)
            m_total = len(model_results)
            m_rate = (m_pass / m_total * 100) if m_total > 0 else 0

            # 推理性能组
            perf_endpoints = ReportGenerator.MODEL_PERF_ENDPOINTS
            perf_results = [r for r in model_results if r.name in perf_endpoints]
            perf_table = ReportGenerator._build_pivot_table(perf_results, perf_endpoints, rounds)

            # 服务功能组（模型相关但非推理的接口）
            service_endpoints = [ep for ep in ReportGenerator.MODEL_DEPENDENT_ENDPOINTS
                                 if ep not in perf_endpoints]
            service_results = [r for r in model_results if r.name in service_endpoints]
            service_table = ReportGenerator._build_pivot_table(service_results, service_endpoints, rounds)

            # 其它/专项测试组：动态兜底展示未被上面两组名单覆盖的结果(多模态用例、GGUF 显式加载明细、
            # SampleApp/Builder 集成用例等),避免"计入总数但明细表里找不到对应行"的数据黑洞。
            # BUILDER/SAMPLEAPP 用例已拆到下方独立明细区块,这里提前排除,避免在通用兜底表里重复展示。
            leftover_endpoints = sorted(
                set(r.name for r in model_results) - set(perf_endpoints) - set(service_endpoints)
                - {r.name for r in model_results if r.name.startswith("BUILDER") or r.name.startswith("SAMPLEAPP:")
                   or r.name.startswith("POST /v1/chat/completions (tool_call")}
            )
            leftover_table = ReportGenerator._build_pivot_table(model_results, leftover_endpoints, rounds)

            # 独立的 Builder 集成 / SampleApp 明细区块（模型级）：与总汇报保持同一套展示逻辑，
            # 不再混在通用的"其它/专项测试"兜底表里。
            model_builder_results = [r for r in model_results if r.name.startswith("BUILDER")]
            model_sampleapp_results = [r for r in model_results if r.name.startswith("SAMPLEAPP:")]
            model_builder_html = ""
            if model_builder_results:
                model_builder_html = f"""<div class="section"><h2><span class="icon">&#128295;</span> Builder 集成测试结果</h2>
                {ReportGenerator._build_case_detail_table(model_builder_results, show_model_column=False)}
                </div>"""
            model_sampleapp_html = ""
            if model_sampleapp_results:
                model_sampleapp_html = f"""<div class="section"><h2><span class="icon">&#128241;</span> SampleApp 测试结果</h2>
                {ReportGenerator._build_case_detail_table(model_sampleapp_results, show_model_column=False)}
                </div>"""

            # tool_calls 协议测试（模型级）：独立分类区块，不再落入上面的"其它/专项测试"兜底表；
            # 完整协议载荷统一在共享的 report_tool_calls.html 里查看(见 crash_idx_by_id 同一套映射手法)。
            model_toolcalls_results = [r for r in model_results if r.name.startswith("POST /v1/chat/completions (tool_call")]
            model_toolcalls_html = ""
            if model_toolcalls_results:
                tc_total = len(model_toolcalls_results)
                tc_pass = sum(1 for r in model_toolcalls_results if r.passed)
                tc_skipped = sum(1 for r in model_toolcalls_results if r.skipped)
                tc_crashed = sum(1 for r in model_toolcalls_results if r.crashed)
                tc_fail = tc_total - tc_pass - tc_skipped - tc_crashed
                tc_links = []
                for r in model_toolcalls_results:
                    idx = toolcalls_idx_by_id.get(id(r))
                    if idx is not None:
                        tc_links.append(f'<a href="report_tool_calls.html#{ReportGenerator._tool_call_anchor(idx)}">R{r.round_num}: {_html_escape(r.name)} →</a>')
                tc_links_html = "<br>".join(tc_links) if tc_links else ""
                model_toolcalls_html = f"""<div class="section"><h2><span class="icon">&#128268;</span> tool_calls 协议测试</h2>
                <div class="summary-bar">
                    <div class="summary-card"><div class="num">{tc_total}</div><div class="label">用例数</div></div>
                    <div class="summary-card card-pass"><div class="num">{tc_pass}</div><div class="label">通过</div></div>
                    <div class="summary-card card-fail"><div class="num">{tc_fail}</div><div class="label">失败</div></div>
                    <div class="summary-card card-crash"><div class="num">{tc_crashed}</div><div class="label">崩溃</div></div>
                    <div class="summary-card"><div class="num">{tc_skipped}</div><div class="label">跳过(模型能力局限)</div></div>
                </div>
                <div class="resp-detail">{tc_links_html}</div>
                </div>"""

            # 性能报告：每一轮 /profile 的完整数据 + CPU/内存
            perf_report_html = ""
            profile_results = [r for r in model_results
                               if r.name == "GET /profile" and r.passed and r.response_data]
            # 收集每轮的 chat 推理结果用于提取 CPU/内存
            chat_by_round = {}
            _chat_names = set(ReportGenerator.MODEL_PERF_ENDPOINTS) - {"GET /profile"}
            for cr in model_results:
                if cr.name in _chat_names and cr.passed:
                    chat_by_round.setdefault(cr.round_num, []).append(cr)

            def _fmt(val):
                if val == "N/A" or val is None:
                    return "N/A"
                try:
                    return f"{float(val):.2f}"
                except (ValueError, TypeError):
                    return str(val)

            def _fmt_int(val):
                if val == "N/A" or val is None:
                    return "N/A"
                try:
                    return str(int(float(val)))
                except (ValueError, TypeError):
                    return str(val)

            if profile_results:
                perf_report_html = """<div class="section">
                <h2><span class="icon">&#128202;</span> 性能报告</h2>
                <p style="color: var(--text-secondary); margin-bottom: 18px; font-size: 13px;">每轮推理后 <code>/profile</code> 返回的详细性能指标及资源占用</p>
                <div class="perf-round-grid">"""
                for pr in profile_results:
                    ttft = pr.response_data.get("time_to_first_token", "N/A")
                    tgt = pr.response_data.get("token_generation_time", "N/A")
                    ppr = pr.response_data.get("prompt_processing_rate", "N/A")
                    tgr = pr.response_data.get("token_generation_rate", "N/A")
                    npt = pr.response_data.get("num_prompt_tokens", "N/A")
                    ngt = pr.response_data.get("num_generated_tokens", "N/A")
                    # 从该轮 chat 结果提取 CPU/内存
                    round_chats = chat_by_round.get(pr.round_num, [])
                    # 展示层归一化,原因同总汇报的性能对比表(见 _normalize_cpu_percent 定义处注释)。
                    cpu_vals = [_normalize_cpu_percent(c.perf_after["cpu_percent"]) for c in round_chats
                                if c.perf_after and c.perf_after.get("cpu_percent", 0) > 0]
                    mem_vals = [c.perf_after["rss_mb"] for c in round_chats
                                if c.perf_after and c.perf_after.get("rss_mb", 0) > 0]
                    avg_cpu = (sum(cpu_vals) / len(cpu_vals)) if cpu_vals else None
                    avg_mem = (sum(mem_vals) / len(mem_vals)) if mem_vals else None
                    cpu_str = f"{avg_cpu:.1f}%" if avg_cpu else "N/A"
                    mem_str = f"{avg_mem:.0f} MB" if avg_mem else "N/A"
                    perf_report_html += f"""
                <div class="perf-round-card">
                    <div class="perf-round-title">Round {pr.round_num}</div>
                    <div class="perf-metric"><span class="perf-metric-label">Time to First Token (ms)</span><span class="perf-metric-value">{_fmt(ttft)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Token Generation Time (ms)</span><span class="perf-metric-value">{_fmt(tgt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Prompt Processing Rate (tokens/s)</span><span class="perf-metric-value">{_fmt(ppr)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Token Generation Rate (tokens/s)</span><span class="perf-metric-value">{_fmt(tgr)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Num Prompt Tokens</span><span class="perf-metric-value">{_fmt_int(npt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">Num Generated Tokens</span><span class="perf-metric-value">{_fmt_int(ngt)}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">推理 CPU 占用</span><span class="perf-metric-value">{cpu_str}</span></div>
                    <div class="perf-metric"><span class="perf-metric-label">推理内存占用</span><span class="perf-metric-value">{mem_str}</span></div>
                </div>"""
                perf_report_html += "</div></div>"
            else:
                perf_report_html = """<div class="section">
                <h2><span class="icon">&#128202;</span> 性能报告</h2>
                <div class="empty-state">无 /profile 数据（模型未返回性能指标）</div>
                </div>"""

            # 崩溃日志:只保留次数 + 逐条极简摘要 + 跳转链接,完整 stderr/stdout 尾部原始文本
            # 在 report_crash_events.html(见 generate_summary_html 的下沉逻辑)。
            crash_html = ""
            if model_crashes:
                crash_html = """<div class="section"><h2><span class="icon">&#9888;</span> 崩溃事件</h2>
                <table class="crash-table"><thead><tr>
                    <th>时间</th><th>轮次</th><th>端点</th><th>详情</th><th>完整日志</th></tr></thead><tbody>"""
                for c in model_crashes:
                    idx = crash_idx_by_id.get(id(c))
                    detail_link = (f'<a href="report_crash_events.html#{ReportGenerator._crash_event_anchor(idx)}">查看 →</a>'
                                    if idx is not None else "-")
                    crash_html += f"""<tr><td>{_html_escape(c.timestamp)}</td>
                        <td class="center">{c.round_num}</td>
                        <td><code>{_html_escape(c.endpoint)}</code>{_crash_badge_html(c)}</td><td>{_html_escape(c.detail)}</td>
                        <td>{detail_link}</td></tr>"""
                crash_html += "</tbody></table></div>"

            # 服务启动失败的醒目横幅 (模型级)
            model_startup_failures = [c for c in model_crashes if c.endpoint == "SERVICE_STARTUP"]
            startup_banner_html = ""
            if model_startup_failures:
                startup_banner_html = '<div class="startup-banner"><div class="startup-banner-title">⚠ 服务启动失败 — 本模型未能运行任何测试</div>'
                startup_banner_html += '<div class="startup-banner-desc">GenieAPIService.exe 启动后立即退出。下表展示具体的 exit_code 与 stderr 日志摘要。</div>'
                startup_banner_html += '<table class="startup-table"><thead><tr><th>时间</th><th>失败详情</th></tr></thead><tbody>'
                for c in model_startup_failures:
                    startup_banner_html += f'<tr><td>{_html_escape(c.timestamp)}</td><td><pre class="startup-detail">{_html_escape(c.detail)}</pre></td></tr>'
                startup_banner_html += '</tbody></table></div>'

            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            total_latency = sum(r.latency_ms for r in model_results if not r.skipped)

            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape(model)} - 测试报告</title>
<style>
{ReportGenerator._common_css()}
</style>
</head>
<body>
<div class="header">
    <div class="header-inner">
        <div class="eyebrow">Model Test Report</div>
        <h1>{_html_escape(model)}</h1>
        <p class="subtitle">模型测试详细报告</p>
        <div class="meta">
            <span class="meta-item"><strong>生成时间:</strong> {now_str}</span>
            <span class="meta-item"><strong>测试轮数:</strong> {len(rounds)}</span>
            <span class="meta-item"><strong>总耗时:</strong> {total_latency/1000:.1f}s</span>
            <span class="meta-item"><a href="report.html" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent); font-weight: 600;">← 返回总汇报</a></span>
            {ReportGenerator._cmdline_meta_html(cmdline)}
        </div>
    </div>
</div>

<div class="container">

{startup_banner_html}

<div class="summary-bar">
    <div class="summary-card"><div class="num">{m_total}</div><div class="label">总测试数</div></div>
    <div class="summary-card card-pass"><div class="num">{m_pass}</div><div class="label">通过</div></div>
    <div class="summary-card card-fail"><div class="num">{sum(1 for r in model_results if (not r.passed) and (not r.crashed) and (not r.skipped))}</div><div class="label">失败</div></div>
    <div class="summary-card card-ignored" title="服务端已知占位/未实现缺陷的 FAIL 子集"><div class="num">{sum(1 for r in model_results if (not r.passed) and (not r.crashed) and (not r.skipped) and getattr(r, 'ignorable', False))}</div><div class="label">可忽略</div></div>
    <div class="summary-card card-crash"><div class="num">{sum(1 for r in model_results if r.crashed)}</div><div class="label">崩溃</div></div>
    <div class="summary-card"><div class="num">{sum(1 for r in model_results if r.skipped)}</div><div class="label">跳过</div></div>
    <div class="summary-card card-capability" title="根因判定为模型自身能力/输出质量局限而非服务端缺陷的诊断性标注;诊断性维度,不改变 passed/failed/退出码判定"><div class="num">{sum(1 for r in model_results if getattr(r, 'model_capability_issue', False))}</div><div class="label">模型能力问题</div></div>
</div>

<div class="progress-bar">
    <div class="progress-fill {'good' if m_rate >= 80 else ('warn' if m_rate >= 50 else 'bad')}" style="width:{max(m_rate, 5):.0f}%">
        <span class="progress-text">通过率 {m_rate:.1f}%</span>
    </div>
</div>
<div class="progress-meta"><span>模型通过率</span><span class="pct">{m_rate:.1f}%</span></div>

<div class="section">
<h2><span class="icon">&#9889;</span> 模型推理性能</h2>
<div class="model-section">
    <div class="group-section">
        {perf_table}
    </div>
</div>
</div>

<div class="section">
<h2><span class="icon">&#128737;</span> 服务功能接口</h2>
<div class="model-section">
    <div class="group-section">
        {service_table}
    </div>
</div>
</div>

{f'''<div class="section">
<h2><span class="icon">&#128295;</span> 其它/专项测试</h2>
<div class="model-section">
    <div class="group-section">
        {leftover_table}
    </div>
</div>
</div>''' if leftover_table else ""}
{model_builder_html}
{model_sampleapp_html}
{model_toolcalls_html}

{perf_report_html}
{crash_html}

<div class="footer">
    {_html_escape(model)} Test Report &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}
</div>

</div>
</body>
</html>"""

            ReportGenerator._assert_no_placeholder_leak(html, f"模型报告 report_{model}.html")
            safe_model_name = model.replace(" ", "_").replace("/", "_")
            out_path = Path(out_dir) / f"report_{safe_model_name}.html"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[输出] 模型报告: {out_path}")

    @staticmethod
    def generate_html(all_results, perf_data, crash_events, out_dir, remote_mode=False):
        """向后兼容：调用 generate_summary_html + generate_model_reports"""
        ReportGenerator.generate_summary_html(all_results, perf_data, crash_events, out_dir, remote_mode)
        ReportGenerator.generate_model_reports(all_results, perf_data, crash_events, out_dir, remote_mode)

    @staticmethod
    def generate_conversations(all_results, out_dir):
        """生成详细数据记录 HTML — 包含 chat 对话、textsplitter、fetch 等大数据量接口的完整响应"""
        # 收集所有需要详细展示的结果。tool_calls 测试(即使名称含"chat/completions"子串)排除在外：
        # 它们与同一轮真正的 chat 测试共用相同的 model_R{round}_{mode} 锚点会互相冲突，且核心诊断
        # 价值(工具调用结构/流式重建参数)已下沉到独立的 report_tool_calls.html。
        chat_results = [r for r in all_results
                        if "chat/completions" in r.name and not r.name.startswith("POST /v1/chat/completions (tool_call")]
        textsplitter_results = [r for r in all_results if "textsplitter" in r.name and r.response_data]
        fetch_results = [r for r in all_results if r.name == "POST /fetch" and r.response_data]

        if not chat_results and not textsplitter_results and not fetch_results:
            return

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        stream_count = sum(1 for r in chat_results if r.is_stream)
        non_stream_count = len(chat_results) - stream_count
        format_counts = {}
        for r in chat_results:
            fmt = getattr(r, "request_format", "") or "openai_string"
            format_counts[fmt] = format_counts.get(fmt, 0) + 1

        # ---- Chat 对话卡片 ----
        chat_cards_html = ""
        for r in chat_results:
            mode = "stream" if r.is_stream else "nonstream"
            anchor_id = f"{r.model_name}_R{r.round_num}_{mode}"
            mode_label = "Stream" if r.is_stream else "Non-Stream"
            mode_class = "mode-stream" if r.is_stream else "mode-nonstream"
            stream_info = ""
            if r.is_stream and r.chunk_count > 0:
                stream_info = f"""<div class="stream-metrics">
                    <span><strong>Chunks:</strong> {r.chunk_count}</span>
                    <span><strong>TTFT:</strong> {r.ttft_ms:.0f}ms</span>
                    <span><strong>总耗时:</strong> {r.latency_ms:.0f}ms</span>
                </div>"""
            elif not r.is_stream:
                stream_info = f"""<div class="stream-metrics">
                    <span><strong>响应延迟:</strong> {r.latency_ms:.0f}ms</span>
                </div>"""
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            fmt_badge = _content_format_badge_html(r)
            # 多模态用例(client-style/openai-style 图片/音频)在 media_image_data_uri/
            # media_audio_data_uri 里带着可直接渲染的 data URI —— 这里把真正传给模型的
            # 图片/音频素材原样展示在 USER 消息下方，而不是只有一句"该模型支持图片"这类
            # 文字描述、看不到到底传了哪张图/哪段音频。
            media_html = ""
            if getattr(r, "media_image_data_uri", ""):
                media_html += (f'<div class="msg-media"><img src="{r.media_image_data_uri}" alt="multimodal input image">'
                               f'<div class="msg-media-label">{_html_escape(r.media_asset_label)}</div></div>')
            if getattr(r, "media_audio_data_uri", ""):
                media_html += (f'<div class="msg-media"><audio controls src="{r.media_audio_data_uri}"></audio>'
                               f'<div class="msg-media-label">{_html_escape(r.media_asset_label)}</div></div>')
            chat_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge {mode_class}">{mode_label}</span>{fmt_badge}
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                </div>
                <div class="message user-msg">
                    <div class="msg-role">USER</div>
                    <div class="msg-content">{_html_escape(r.text_prompt)}</div>
                    {media_html}
                </div>
                <div class="message assistant-msg">
                    <div class="msg-role">ASSISTANT</div>
                    <div class="msg-content">{_html_escape(r.text_response) if r.text_response else '(无响应)'}</div>
                </div>
                {stream_info}
            </div>"""

        # ---- Textsplitter 卡片 ----
        textsplitter_cards_html = ""
        for r in textsplitter_results:
            anchor_id = f"{r.model_name}_R{r.round_num}_textsplitter"
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            data = r.response_data or {}
            seg_count = data.get("segment_count", 0)
            segments = data.get("segments", [])
            seg_html = ""
            for i, seg in enumerate(segments):
                seg_text = seg.get("text", "")
                seg_len = seg.get("length", len(seg_text))
                seg_html += f'<div class="seg-item"><span class="seg-idx">#{i+1}</span> <span class="seg-len">[{seg_len} chars]</span> {_html_escape(seg_text)}</div>'
            textsplitter_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge mode-textsplitter">TextSplitter</span>
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                    <span class="round-tag">{seg_count} segments</span>
                </div>
                <div class="data-content">{seg_html if seg_html else '<em>无分段数据</em>'}</div>
            </div>"""

        # ---- Fetch 卡片 ----
        fetch_cards_html = ""
        for r in fetch_results:
            anchor_id = f"{r.model_name}_R{r.round_num}_fetch"
            status_class = "badge-pass" if r.passed else ("badge-crash" if r.crashed else "badge-fail")
            status_text = "PASS" if r.passed else ("CRASH" if r.crashed else "FAIL")
            data = r.response_data or {}
            history_count = data.get("history_count", 0)
            history = data.get("history", [])
            hist_html = ""
            for h in history:
                role = h.get("role", "unknown")
                content = h.get("content", "")
                role_cls = "user-msg" if role == "user" else "assistant-msg"
                hist_html += f"""<div class="message {role_cls}">
                    <div class="msg-role">{_html_escape(role.upper())}</div>
                    <div class="msg-content">{_html_escape(content)}</div>
                </div>"""
            fetch_cards_html += f"""
            <div class="conv-card" id="{_html_escape(anchor_id)}">
                <div class="conv-meta">
                    <span class="badge mode-fetch">Fetch History</span>
                    <span class="badge {status_class}">{status_text}</span>
                    <span class="model-tag">{_html_escape(r.model_name)}</span>
                    <span class="round-tag">Round {r.round_num}</span>
                    <span class="round-tag">{history_count} 条记录</span>
                </div>
                <div class="hist-content">{hist_html if hist_html else '<div class="data-content"><em>无历史数据</em></div>'}</div>
            </div>"""

        # ---- 组合各区域 ----
        sections_html = ""
        if chat_cards_html:
            sections_html += f"""<h2 class="section-title">💬 对话记录（Chat Completions）</h2>{chat_cards_html}"""
        if textsplitter_cards_html:
            sections_html += f"""<h2 class="section-title">✂️ 文本切分（TextSplitter）</h2>{textsplitter_cards_html}"""
        if fetch_cards_html:
            sections_html += f"""<h2 class="section-title">📜 对话历史（Fetch）</h2>{fetch_cards_html}"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenieAPIService 详细数据记录 - {now_str}</title>
<style>
:root {{
    --primary: #1565C0;
    --success: #2E7D32;
    --danger: #C62828;
    --warning: #E65100;
    --bg: #F5F7FA;
    --card-bg: #FFFFFF;
    --border: #E0E0E0;
    --text: #263238;
    --text-secondary: #546E7A;
    --user-bg: #E3F2FD;
    --assistant-bg: #F1F8E9;
    --radius: 10px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6; }}
.page-header {{ background: linear-gradient(135deg, #1565C0 0%, #0D47A1 100%); color: white; padding: 35px 0; margin-bottom: 30px; }}
.page-header-inner {{ max-width: 900px; margin: 0 auto; padding: 0 30px; }}
.page-header h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 6px; }}
.page-header .info {{ opacity: 0.85; font-size: 13px; display: flex; gap: 20px; flex-wrap: wrap; margin-top: 10px; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 0 30px 60px; }}
.section-title {{ font-size: 18px; font-weight: 600; color: var(--primary); margin: 30px 0 16px; padding-bottom: 8px; border-bottom: 2px solid var(--primary); }}
.conv-card {{ background: var(--card-bg); border-radius: var(--radius); margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); overflow: hidden; }}
.conv-meta {{ display: flex; gap: 10px; align-items: center; padding: 12px 20px; background: #FAFAFA; border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
.badge {{ font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
.mode-stream {{ background: #E8F5E9; color: #2E7D32; }}
.mode-nonstream {{ background: #E3F2FD; color: #1565C0; }}
.mode-textsplitter {{ background: #FFF3E0; color: #E65100; }}
.mode-fetch {{ background: #F3E5F5; color: #6A1B9A; }}
.badge-pass {{ background: #E8F5E9; color: #2E7D32; }}
.badge-fail {{ background: #FFEBEE; color: #C62828; }}
.badge-crash {{ background: #FFF3E0; color: #E65100; }}
.model-tag {{ font-size: 12px; color: var(--primary); font-weight: 600; }}
.round-tag {{ font-size: 12px; color: var(--text-secondary); }}
.message {{ padding: 16px 20px; }}
.user-msg {{ background: var(--user-bg); border-left: 4px solid #1976D2; }}
.assistant-msg {{ background: var(--assistant-bg); border-left: 4px solid #43A047; }}
.msg-role {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; color: var(--text-secondary); }}
.msg-content {{ font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-wrap: break-word; max-height: 500px; overflow-y: auto; }}
.msg-media {{ margin-top: 10px; }}
.msg-media img {{ max-width: 320px; max-height: 320px; border-radius: var(--radius-sm, 4px); border: 1px solid var(--border); display: block; }}
.msg-media audio {{ width: 320px; }}
.msg-media-label {{ font-size: 11px; color: var(--text-secondary); margin-top: 4px; font-family: var(--font-mono, monospace); }}
.stream-metrics {{ display: flex; gap: 20px; padding: 10px 20px; background: #FAFAFA; border-top: 1px solid var(--border); font-size: 12px; color: var(--text-secondary); }}
.data-content {{ padding: 16px 20px; font-size: 13px; line-height: 1.6; max-height: 600px; overflow-y: auto; }}
.hist-content {{ max-height: 800px; overflow-y: auto; }}
.seg-item {{ padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; line-height: 1.5; }}
.seg-item:last-child {{ border-bottom: none; }}
.seg-idx {{ font-weight: 700; color: var(--primary); margin-right: 8px; }}
.seg-len {{ font-size: 11px; color: var(--text-secondary); margin-right: 8px; }}
.footer {{ text-align: center; padding: 30px; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); margin-top: 20px; }}
</style>
</head>
<body>
<div class="page-header">
    <div class="page-header-inner">
        <h1>GenieAPIService 详细数据记录</h1>
        <div class="info">
            <span><strong>生成时间:</strong> {now_str}</span>
            <span><strong>对话:</strong> {len(chat_results)} ({stream_count} stream / {non_stream_count} non-stream)</span>
            <span><strong>请求格式:</strong> {format_counts.get("openai_string", 0)} 字符串 / {format_counts.get("openai_array", 0)} 数组 / {format_counts.get("client_object", 0)} 对象</span>
            <span><strong>TextSplitter:</strong> {len(textsplitter_results)}</span>
            <span><strong>Fetch:</strong> {len(fetch_results)}</span>
            <span><a href="report.html" style="color:white; text-decoration:underline;">← 返回总汇报</a></span>
        </div>
    </div>
</div>
<div class="container">
{sections_html}
<div class="footer">GenieAPIService Data Log &middot; <a href="report.html">返回总汇报</a> &middot; {now_str}</div>
</div>
</body>
</html>"""

        out_path = Path(out_dir) / "conversations.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[输出] 详细数据记录: {out_path}")


def _html_escape(text):
    """简单的 HTML 转义"""
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _crash_badge_html(c):
    """根据 CrashEvent 的 endpoint/detail 判断是否为 MNN OOM 相关事件,返回对应徽章 HTML；
    不相关时返回空字符串。真实 OOM(MNN_OOM_CRASH)与疑似级联崩溃(MNN_OOM_CASCADE)使用不同样式,
    避免阅读者把"MNN 自身真实问题"和"疑似被 MNN OOM 拖累而崩溃的其它后端"混为一谈；两者
    都是需要关注的真实问题、都已计入 failed/crashed 统计,徽章仅作诊断提示,不代表已豁免。"""
    endpoint = c.endpoint or ""
    detail_lower = (c.detail or "").lower()
    if endpoint == "MNN_OOM_CASCADE" or "mnn_oom_cascade" in detail_lower:
        return '<span class="badge-mnn-cascade" title="疑似因同进程内 MNN 模型 OOM 拖垮进程而级联崩溃,需人工排查根因;已计入真实失败,不代表豁免">疑似级联崩溃(非豁免)</span>'
    if endpoint == "MNN_OOM_CRASH":
        return '<span class="badge-mnn-oom" title="MNN 模型自身触发的真实内存不足/崩溃事件">MNN真实OOM</span>'
    return ""
