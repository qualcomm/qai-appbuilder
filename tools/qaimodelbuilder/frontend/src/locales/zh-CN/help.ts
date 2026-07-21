// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 真值源说明：本项目 i18n 已无自动生成管道（旧的 _L8-locale-gen.py 与
// _migrated/*.json 均未保留在仓库）。因此本文件就是当前唯一真值源，
// 必须手工维护。修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 编码，禁止双重编码损坏）。
//
// 类型：en/{ns}.ts 经主入口 en.ts 组装后由 typeof 推导出 MessageSchema；
// zh-CN / zh-TW 的同名子文件须保持与 en 完全一致的 key 结构（由 locale
// parity 测试 + tsc 强制）。
// =============================================================================

const help = {
  about: "关于",
  ccPanelHtml: "(请见设置中的 Claude 帮助)",
  ccText: "🤖 Claude Code 指令帮助\n\n  /cc new <目录路径> [会话名称]\n    创建新的 Claude Code 会话，绑定到指定项目目录。\n    例：/cc new C:\\Projects\\MyApp 认证功能开发\n\n  /cc list  (/cc l)\n    列出你的所有 Claude Code 会话（ID、名称、状态）。\n\n  /cc use <序号>  (/cc u <序号>)\n    按 /cc list 序号切换会话（如 /cc use 1）。\n\n  /cc use <ID前缀>\n    按 ID 前缀切换会话（输入 ID 前 8 位即可）。\n\n  /cc status  (/cc s)\n    查看当前 Claude Code 会话状态、对话轮次和工具调用次数。\n\n  /cc models  (/cc ms)\n    查看 Claude Code 可用模型列表（带序号），并显示当前选中的模型。\n\n  /cc model  (/cc m)\n    查看当前 Claude Code 使用的模型。\n\n  /cc model <编号>  (/cc m <编号>)\n    按 /cc models 序号切换 Claude Code 模型（如 /cc model 1）。\n\n  /cc fork  (/cc f)\n    Fork 当前会话为新分支（保留原历史，下次发消息时生成新会话 ID）。\n    适合在关键节点保存进度，然后尝试不同方向。\n\n  /cc stop  (/cc st)\n    停止当前正在执行的 Claude Code 任务，停止后可立即发送新消息继续对话。\n\n  /cc cd [目录路径]\n    查看当前工作目录（无参数），或修改当前 CC 会话绑定的工作目录。\n\n  /cc rename <新名称>  (/cc r <新名称>)\n    重命名当前 Claude Code 会话。\n\n  /cc close  (/cc c)\n    退出 CC 模式（会话保留，可用 /cc use 重新进入）。\n\n  /cc delete  (/cc d)\n    彻底删除当前 Claude Code 会话（不可恢复）。\n\n  /cc help  (/cc h)\n    显示此帮助。\n\n  <普通消息>（CC 会话激活时）\n    直接发消息即可与 Claude Code 对话，无需 /cc 前缀。\n    发送 /new 可切回普通 AI 对话模式（不影响 CC 会话）。\n\n💡 创建会话后，直接发消息即可与 Claude Code 对话\n💡 /cc fork 可在关键节点保存进度，然后尝试不同方向\n💡 /cc stop 停止后可立即发送新消息继续对话\n💡 /cc close 退出后，会话仍保留，随时可用 /cc use 重新进入\n💡 发送 /new 可切回普通 AI 对话模式",
  commands: "命令",
  mainText: "📖 微信 / 飞书 / Chat 指令帮助\n\n⌨️ 普通对话指令：\n\n  /help  (/h)\n    显示此帮助信息。\n\n  /new  (/n)\n    保存当前会话历史后开启新会话，历史记录保留在 Chat 界面可查看。\n\n  /clear  (/cl)\n    删除当前会话历史（不保存）后开启新会话，历史记录将被永久移除。\n\n  /list [N]  (/l [N])\n    查看最近 N 条历史会话（默认 5 条），显示名称、时间和对话轮数。\n\n  /use <编号>  (/u <编号>)\n    切换到指定编号的历史会话继续对话。\n\n  /status  (/s)\n    查看当前会话状态（名称、对话轮数）。\n\n  /rename <新名称>  (/rn <新名称>)\n    重命名当前会话。\n\n  /delete  (/del)\n    删除当前会话（不可恢复），并开启新会话。\n\n  /stop  (/st)\n    立即停止当前正在执行的任务（普通对话或 Claude Code 任务均支持）。\n\n  /models  (/ms)\n    查看所有可用模型列表（本地 + 云端），并显示当前正在使用的模型。\n\n  /model  (/m)\n    查看当前会话使用的模型。\n\n  /model <编号>  (/m <编号>)\n    按 /models 编号切换模型，也可直接输入 model_id。发送 /model 0 恢复跟随全局设置。\n\n  /compact [轮次]  (/c [轮次])\n    裁剪当前会话的历史消息，保留最近 N 轮对话（WebUI Chat、微信、飞书均支持）。\n    /compact        查看当前全局默认轮次设置\n    /compact <n>    裁剪当前会话，只保留最近 n 轮对话\n    /compact 0      使用全局默认值裁剪\n    全局默认值可在 Settings > App Config > Channels 中配置。\n\n  /reboot  (/r)\n    重启 QAIModelBuilder 服务，重启完成后微信通道将自动重连。\n\n🤖 Claude Code AI 编程助手指令：\n\n  /cc new <目录路径> [会话名称]\n    创建新的 Claude Code 会话，绑定到指定项目目录。\n    例：/cc new C:\\Projects\\MyApp 认证功能开发\n\n  /cc list  (/cc l)\n    列出你的所有 Claude Code 会话（ID、名称、状态）。\n\n  /cc use <序号>  (/cc u <序号>)\n    按 /cc list 序号切换会话（如 /cc use 1）。\n\n  /cc use <ID前缀>\n    按 ID 前缀切换会话（输入 ID 前 8 位即可）。\n\n  /cc status  (/cc s)\n    查看当前 Claude Code 会话状态、对话轮次和工具调用次数。\n\n  /cc models  (/cc ms)\n    查看 Claude Code 可用模型列表（带序号），并显示当前选中的模型。\n\n  /cc model  (/cc m)\n    查看当前 Claude Code 使用的模型。\n\n  /cc model <编号>  (/cc m <编号>)\n    按 /cc models 序号切换 Claude Code 模型（如 /cc model 1）。\n\n  /cc fork  (/cc f)\n    Fork 当前会话为新分支（保留原历史，下次发消息时生成新会话 ID）。\n\n  /cc stop  (/cc st)\n    停止当前正在执行的 Claude Code 任务。\n\n  /cc cd [目录路径]\n    查看或修改当前 CC 会话绑定的工作目录。\n\n  /cc rename <新名称>  (/cc r <新名称>)\n    重命名当前 Claude Code 会话。\n\n  /cc close  (/cc c)\n    退出 CC 模式（会话保留，可用 /cc use 重新进入）。\n\n  /cc delete  (/cc d)\n    彻底删除当前 Claude Code 会话（不可恢复）。\n\n  /cc help  (/cc h)\n    显示 Claude Code 指令帮助。\n\n🔷 Open Code AI 编程助手指令：\n\n  /oc new <目录路径> [会话名称]\n  /oc list  (/oc l)\n  /oc use <序号>  (/oc u <序号>)\n  /oc status  (/oc s)\n  /oc models  (/oc ms)\n  /oc model  (/oc m)\n  /oc stop  (/oc st)\n  /oc rename <新名称>  (/oc r <新名称>)\n  /oc close  (/oc c)\n  /oc delete  (/oc d)\n  /oc help  (/oc h)\n\n💡 提示：本地模型不可用时，若已配置云端模型，系统会自动切换并提前通知。\n💡 Claude Code 需在 Settings > AI Coding 中启用并配置认证信息。\n💡 Open Code 需在 Settings > AI Coding > Open Code 中启用并配置服务地址。",
  ocPanelHtml: "(请见设置中的 OpenCode 帮助)",
  ocText: "🔷 Open Code 指令帮助\n\n  /oc new <目录路径> [会话名称]\n    创建新的 Open Code 会话，绑定到指定项目目录。\n    例：/oc new C:\\Projects\\MyApp 认证功能开发\n\n  /oc list  (/oc l)\n    列出你的所有 Open Code 会话（ID、名称、状态、模型）。\n\n  /oc use <序号>  (/oc u <序号>)\n    按 /oc list 序号切换会话（如 /oc use 1）。\n\n  /oc use <ID前缀>\n    按 ID 前缀切换会话（输入 ID 前 8 位即可）。\n\n  /oc status  (/oc s)\n    查看当前 Open Code 会话状态、对话轮次和工具调用次数。\n\n  /oc models  (/oc ms)\n    查看 Open Code 可用模型列表（带序号），并显示当前选中的模型。\n\n  /oc model  (/oc m)\n    查看当前 Open Code 使用的模型。\n\n  /oc model <编号>  (/oc m <编号>)\n    按 /oc models 序号切换 Open Code 模型（如 /oc model 1）。\n\n  /oc stop  (/oc st)\n    停止当前正在执行的 Open Code 任务，停止后可立即发送新消息继续对话。\n\n  /oc rename <新名称>  (/oc r <新名称>)\n    重命名当前 Open Code 会话。\n\n  /oc close  (/oc c)\n    退出 OC 模式（会话保留，可用 /oc use 重新进入）。\n\n  /oc delete  (/oc d)\n    彻底删除当前 Open Code 会话（不可恢复）。\n\n  /oc help  (/oc h)\n    显示此帮助。\n\n  <普通消息>（OC 会话激活时）\n    直接发消息即可与 Open Code 对话，无需 /oc 前缀。\n    发送 /new 可切回普通 AI 对话模式（不影响 OC 会话）。\n\n💡 创建会话后，直接发消息即可与 Open Code 对话\n💡 /oc stop 停止后可立即发送新消息继续对话\n💡 /oc close 退出后，会话仍保留，随时可用 /oc use 重新进入\n💡 发送 /new 可切回普通 AI 对话模式\n💡 Open Code 需在 Settings > AI Coding > Open Code 中启用并配置服务地址",
  shortcuts: "快捷键",
  title: "帮助",
  version: "版本",
  // WebUI Chat 引导提示：/cc /oc 斜杠命令仅在微信/飞书通道使用；
  // 网页端请点输入框上方的「Claude Code」/「Open Code」模式按钮，
  // 在弹出的会话面板里新建/切换/重命名/删除会话（功能比斜杠命令更全）。
  webuiCcHint:
    "💡 网页端无需 /cc 命令：请点输入框上方的「Claude Code」按钮进入 CC 模式，在会话面板里新建 / 切换 / 重命名 / 删除会话。\n（/cc 系列命令在微信 / 飞书通道仍可使用。）",
  webuiOcHint:
    "💡 网页端无需 /oc 命令：请点输入框上方的「Open Code」按钮进入 OC 模式，在会话面板里新建 / 切换 / 重命名 / 删除会话。\n（/oc 系列命令在微信 / 飞书通道仍可使用。）",
};

export default help;
