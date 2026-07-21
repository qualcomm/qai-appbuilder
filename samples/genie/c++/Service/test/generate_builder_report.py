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
QAIModelBuilder 三端（CLI / HTTP API / WebUI）一致性统一报告生成脚本

与 test_builder_cli.py（CLI/API/channel/一致性/已知缺口）、
QAIModelBuilder/frontend/e2e/*.spec.ts（WebUI 一致性用例）两套独立测试体系
零耦合的纯后处理脚本：只读取两者各自已产出的 results.json，离线合并渲染。
不 import、不修改任何一方的代码，符合两套测试体系"互不感知"的既有约定。

本文件是骨架阶段（Delivery Step 1）：具备解析两套件结果、按用例名归一化、
维护 CLI 用例 -> WebUI 等价物映射表、以及覆盖完整性自检的能力；
尚不产出最终 HTML（HTML 生成器见 Delivery Step 4 追加的 ReportGenerator）。

CLI 用例 -> WebUI 等价物映射的三种可能结果：
    1) 有真实 WebUI 入口：映射到具体的 (spec 文件, 用例标题)，覆盖完整性自检
       会在 Playwright 结果里按标题查找对应状态。
    2) 设计边界：webui_spec_file=None，boundary_reason 说明原因，不伪造 UI 步骤。
       边界又分两个子类型（见 _BoundaryKind）：
         - NO_UI_ENTRY：产品本身没有对应的 UI 入口/只读子命令概念。
         - NO_SHARED_TRUTH：UI 和 CLI 各自有渲染/输出，但两者读取的不是同一份
           后端数据（不同接口、不同数据源、或语义已分岔），伪造比对只会产生
           误导性的"一致性"结论，因此同样不做比对，只记录原因。
    3) 尚未采集：映射到的 WebUI 用例暂时在 Playwright 结果里找不到（多为
       Step 1 阶段的正常现象，届时对应 spec 文件还未落地）。

用法:
    python test/generate_builder_report.py \
        --cli_results <cli_out_dir>/results.json \
        --webui_results <webui_out_dir>/e2e-report/results.json \
        --out_dir <合并报告输出目录>
"""

import argparse
import html as _html_module
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 强制 stdout/stderr 使用 UTF-8 编码（Windows 控制台兼容），与 test_builder_cli.py/
# test_service.py 保持一致。
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 数据模型
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
<div class="footer">QAIModelBuilder 三端一致性统一报告 &middot; 由 test/generate_builder_report.py 离线合并生成，
与 test_builder_cli.py / Playwright WebUI 套件均零代码耦合</div>
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
            subtitle="test_builder_cli.py（CLI/API/channel/一致性/已知缺口）与 Playwright WebUI 套件的离线合并报告",
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QAIModelBuilder 三端一致性统一报告生成脚本（与 test_builder_cli.py / "
                     "Playwright WebUI 套件均零耦合，只读取各自的 results.json）")
    parser.add_argument("--cli_results", required=True, help="test_builder_cli.py 产出的 results.json 路径")
    parser.add_argument("--webui_results", required=True,
                         help="Playwright WebUI 套件产出的 e2e-report/results.json 路径")
    parser.add_argument("--out_dir", default="./test_builder_report_results", help="统一报告输出目录")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    cli_cases, cli_payload = parse_cli_results(args.cli_results)
    webui_results = parse_webui_results(args.webui_results)
    defects = parse_defects(Path(args.cli_results).parent / "defects.json")

    _assert_full_mapping_coverage(cli_cases, CLI_CASE_TO_UI_EQUIVALENT)

    unified_cases = build_unified_cases(cli_cases, webui_results, CLI_CASE_TO_UI_EQUIVALENT)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "unified_cases.json", "w", encoding="utf-8") as f:
        json.dump([vars(c) for c in unified_cases], f, indent=2, ensure_ascii=False)

    report_path = ReportGenerator.generate_matrix_html(unified_cases, cli_payload, len(defects), out_dir)
    defects_path = ReportGenerator.generate_defects_detail_html(defects, out_dir)
    webui_detail_path = ReportGenerator.generate_webui_detail_html(unified_cases, out_dir)

    print(f"CLI 侧健康状态: healthy={cli_payload.get('healthy')}, summary={cli_payload.get('summary')}")
    print(f"已生成: {report_path}")
    print(f"已生成: {defects_path}")
    print(f"已生成: {webui_detail_path}")


if __name__ == "__main__":
    main()
