# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

MESSAGES: dict[str, str] = {
    "_meta.locale": "简体中文",
    # -----------------------------------------------------------------
    # cli.build.* — apps/cli/commands/build.py user-facing strings.
    # -----------------------------------------------------------------
    "cli.build.help.summary": "Model Builder 交互式会话（agentic 模型转换 REPL）",
    "cli.build.help.description": (
        "进入 Model Builder agentic 聊天会话：用自然语言 + 斜杠命令引导云端 "
        "Agent 完成模型转换。--model-file 指定要转换的模型文件（可多次），"
        "--llm 指定 Agent 使用的云端大模型。会话内用 /model /precision "
        "/dataset /mode /run 调整参数，/help 查看全部命令。"
    ),
    "cli.build.help.model_file": (
        "要转换的模型文件路径（→ tool_params.model_paths）。可重复传入多个。"
        "注意：这是被转换的文件，不是 Agent 的大模型（后者用 --llm）。"
    ),
    "cli.build.help.llm": "Agent 使用的云端大模型 id（→ model_hint）。CLI 不支持本地 LLM。",
    "cli.build.help.precision": (
        "量化精度（→ tool_params.quant_precision），如 fp16 或 fp16,w8a8。"
        "可选级别: {levels}。"
    ),
    "cli.build.help.dataset": "校准/评测数据集路径（→ tool_params.dataset_path）。",
    "cli.build.help.mode": "初始模式：interactive（默认）逐轮交互；batch 偏向一次性转换。",
    "cli.build.help.resume": (
        "续接已有的 model-build 会话：给出 conversation-id 续接指定会话；"
        "不带参数则尽力续接最近一个（best-effort）。"
    ),
    "cli.build.label.unset": "(未设置)",
    "cli.build.params.summary": (
        "  模型文件 : {files}\n"
        "  量化精度 : {prec}\n"
        "  数据集   : {ds}\n"
        "  模式     : {mode}"
    ),
    "cli.build.error.no_cloud_provider": "未检测到云端模型，请先运行 qai config setup 配置 provider\n",
    "cli.build.banner.ready": "Model Builder 会话已就绪。\n",
    "cli.build.banner.conv_id": "  会话 id  : {conv_id}\n",
    "cli.build.banner.agent_llm": "  Agent LLM: {model_hint}\n",
    "cli.build.banner.hint": "输入自然语言与 Agent 对话，或用斜杠命令调整参数（/help 查看全部）。\n",
    "cli.build.hint.turn_interrupted": "\n（已中断当前回合）\n",
    "cli.build.error.turn_failed": "\n回合失败: {exc_type}: {exc}\n",
    "cli.build.model.current": "当前模型文件: {cur}\n",
    "cli.build.model.set": "已设置模型文件: {files}\n",
    "cli.build.precision.current": "当前量化精度: {prec}\n",
    "cli.build.precision.invalid": "无效精度级别: {invalid}；可选: {levels}\n",
    "cli.build.precision.set": "已设置量化精度: {prec}\n",
    "cli.build.dataset.current": "当前数据集: {ds}\n",
    "cli.build.dataset.set": "已设置数据集: {ds}\n",
    "cli.build.params.header": "当前会话参数:\n",
    "cli.build.mode.current": "当前模式: {mode}（用 /mode batch|interactive 切换）\n",
    "cli.build.mode.set": "已切换模式: {mode}\n",
    "cli.build.error.no_model": "尚未设置模型文件，请先 /model <path> 或启动时用 --model-file。\n",
    "cli.build.error.no_last_message": "没有可重发的上一条消息。\n",
    "cli.build.stop.requested": "已请求中止当前回合。\n",
    "cli.build.clear.new": "已开启新会话: {conv_id}\n",
    "cli.build.history.unavailable": "历史读取尚未接通（CLI 暂不可达该用例）。\n",
    "cli.build.error.history_failed": "读取历史失败: {exc_type}: {exc}\n",
    "cli.build.history.empty": "（暂无历史消息）\n",
    "cli.build.status.unavailable": (
        "运行状态查询尚未接通（依赖 Model Builder 后端用例）。"
        "可用 /params 查看当前参数。\n"
    ),
    "cli.build.workspace.unavailable": "工作区查看尚未接通（依赖 Model Builder 后端用例）。\n",
    "cli.build.promote.unavailable": (
        "导出/晋升为 pack 尚未在 CLI 接通。"
        "转换产物就绪后可运行: qai pack import <artifact>\n"
    ),
    "cli.build.cmd.help.help": "显示全部命令",
    "cli.build.cmd.help.model": "<path...> 设置/查看要转换的模型文件",
    "cli.build.cmd.help.precision": "<csv> 设置/查看量化精度",
    "cli.build.cmd.help.dataset": "<path> 设置/查看数据集",
    "cli.build.cmd.help.params": "查看当前模型/精度/数据集/模式",
    "cli.build.cmd.help.mode": "batch|interactive 切换模式",
    "cli.build.cmd.help.run": "用当前参数发起一次标准转换指令",
    "cli.build.cmd.help.retry": "重发上一条用户消息",
    "cli.build.cmd.help.stop": "中止当前回合",
    "cli.build.cmd.help.status": "查看运行状态（尚未接通）",
    "cli.build.cmd.help.workspace": "查看工作区（尚未接通）",
    "cli.build.cmd.help.promote": "导出为 pack（提示用 qai pack import）",
    "cli.build.cmd.help.history": "打印会话历史消息",
    "cli.build.cmd.help.clear": "开启新会话",
    "cli.build.cmd.help.exit": "退出会话",
    "cli.build.run.head": "请将模型文件 {files} 转换为可在 NPU 上运行的格式",
    "cli.build.run.precision_suffix": "，量化精度 {precision}",
    "cli.build.run.dataset_suffix": "，使用数据集 {dataset} 做校准/评测",
    "cli.build.repl.goodbye": "再见。\n",
    "cli.build.repl.ctrl_c_hint": "\n（再次按 Ctrl+C 退出）\n",
    "cli.build.prompt.repl": "build › ",
    # -----------------------------------------------------------------
    # cli.config.* — apps/cli/commands/config.py user-facing strings.
    # -----------------------------------------------------------------
    "cli.config.provider.label.anthropic": "Anthropic (Claude)",
    "cli.config.provider.label.openai_compat": "OpenAI 兼容",
    "cli.config.provider.label.ollama": "Ollama",
    "cli.config.provider.label.generic_cloud": "通用云端",
    "cli.config.provider.prompt.type_header": "provider 类型: ",
    "cli.config.provider.prompt.type_choice": "选择 (回车默认 anthropic) › ",
    "cli.config.provider.prompt.endpoint": "Endpoint (回车用默认 {base_url}) › ",
    "cli.config.provider.prompt.default_model": "默认模型 (回车用 {default_model}) › ",
    "cli.config.provider.warn.saved_probe_failed": (
        "⚠ provider {provider_id} 已保存但连通性测试失败: {error}\n"
    ),
    "cli.config.setup.error.needs_tty": (
        "qai config setup 需要交互式终端；脚本场景请用 "
        "qai config provider add --type ... --api-key-stdin\n"
    ),
    "cli.config.setup.welcome": "欢迎使用 QAIModelBuilder 配置向导。我将逐步帮你完成配置。\n\n",
    "cli.config.setup.step1_header": "[1/2] 云端模型 Provider\n",
    "cli.config.setup.prompt.add_provider": "  你要添加云端 AI 模型吗？(用于 Chat / Model Builder Agent)",
    "cli.config.setup.prompt.add_another": "  还要添加另一个 provider 吗？",
    "cli.config.setup.step2_header": "\n[2/2] 默认偏好\n",
    "cli.config.setup.prompt.language": "  界面语言 [简体中文] › ",
    "cli.config.setup.done": (
        "  ✓ 完成！配置已保存。运行 qai config provider 查看，"
        "qai build / qai app 开始使用。\n"
    ),
    "cli.config.setup.skipped_no_id": "  (跳过：未提供 provider id)\n",
    "cli.config.setup.probing": "  ✓ 正在测试连通性…\n",
    "cli.config.setup.probe_ok": (
        "  ✓ 已保存 provider {provider_id}（{status} OK，可用模型: {models}）\n"
    ),
    "cli.config.setup.no_model_list": "(无模型列表)",
    "cli.config.setup.probe_failed": (
        "  ⚠ provider {provider_id} 已保存，但连通性测试失败: {error}\n"
    ),
    # -----------------------------------------------------------------
    # cli.app.* — apps/cli/commands/app.py user-facing strings.
    # -----------------------------------------------------------------
    "cli.app.hint.input_kind": "  输入类型: {kind}（用 --{flag}）\n",
    "cli.app.hint.variants": "  可用变体 (--variant): {ids}\n",
    "cli.app.hint.params_header": "  可用参数 (--param key=val):\n",
    "cli.app.ok.wrote_artifact": "已写入产物: {dest}\n",
    "cli.app.ok.wrote_output_json": "已写入输出 JSON: {dest}\n",
    "cli.app.ok.wrote_annotated": "已写入标注产物: {dest}\n",
    "cli.app.error.no_annotated": "未生成标注产物（该 Pack 不产出标注图）\n",
    "cli.app.error.pack_not_found": "qai app: 未找到 Pack '{pack}'。运行 `qai app`（不带参数）查看可用 Pack。\n",
    "cli.app.hint.cancel_prompt": "\n(已取消，/exit 退出)\n",
    "cli.app.hint.repl_banner": "App Builder 会话 — Pack: {pack} (输入类型: {kind})\n直接输入内容（路径或文本）回车运行；/help 查看命令，/exit 退出。\n",
    "cli.app.error.file_missing": "找不到文件: {value}\n请输入存在的文件路径（可把文件拖入终端，或用 Explorer“复制为路径”）。\n",
    "cli.app.hint.turn_interrupted": "\n(本轮已中断)\n",
    "cli.app.ok.current_pack": "当前 Pack: {pack}\n",
    "cli.app.ok.switched_pack": "已切换 Pack: {pack}\n",
    "cli.app.ok.variant_set": "变体: {variant}\n",
    "cli.app.hint.variant_default": "(默认)",
    "cli.app.ok.params_set": "参数: {params}\n",
    "cli.app.ok.params_status": "当前参数: {params}\n当前变体: {variant}\n",
    "cli.app.hint.no_examples": "该 Pack 没有内置示例。\n",
    "cli.app.hint.examples_usage": "用 /examples <序号> 运行内置示例，\n或 /examples <序号> <你的文件路径> 用该示例的参数跑你自己的输入。\n",
    "cli.app.error.bad_example_index": "无效的示例序号: {idx}（用 /examples 查看可用序号）\n",
    "cli.app.error.no_input_override": "该 Pack 的输入类型不支持命令行覆盖，请直接用 /param 调参。\n",
    "cli.app.error.example_files_missing_header": "内置示例 [{idx}] 的样例文件未随 Pack 附带：\n",
    "cli.app.error.example_files_missing_item": "  缺失 {m}\n",
    "cli.app.hint.example_override_usage": "可改用自己的文件运行该示例的参数：\n  /examples {idx} <你的文件路径>\n或直接在提示符输入你的文件路径回车运行。\n",
    "cli.app.error.history_unavailable": "运行历史不可用（容器未接入 list_runs）。\n",
    "cli.app.hint.no_history": "暂无运行历史。\n",
    "cli.app.hint.no_last_result": "还没有运行结果。\n",
    "cli.app.hint.no_output_to_export": "没有可导出的输出。\n",
    "cli.app.hint.out_usage": "用法: /out <path>\n",
    "cli.app.help.model": "切换 Pack: /model <pack>",
    "cli.app.help.variant": "设置变体: /variant <id>",
    "cli.app.help.param": "设置参数: /param key=val",
    "cli.app.help.params": "查看当前参数/变体",
    "cli.app.help.examples": "列出/运行内置示例: /examples [序号]",
    "cli.app.help.history": "查看运行历史",
    "cli.app.help.last": "重新打印上次输出",
    "cli.app.help.out": "导出上次输出: /out <path>",
    "cli.app.help.help": "查看命令列表",
    "cli.app.help.exit": "退出会话",
    # -----------------------------------------------------------------
    # channels.tool.* — src/qai/channels/adapters/channel_tool_formatter.py
    # user-facing tool-progress lines (D5 wave 4).
    # -----------------------------------------------------------------
    "channels.tool.icon.success": "\u2705",
    "channels.tool.icon.error": "\u274c",
    "channels.tool.icon.pending": "\u23f3",
    "channels.tool.batch_header": "\U0001f504 工具调用进度（第 {batch_index} 批）：",
    "channels.tool.action.read": "\U0001f4d6 读取: {path}",
    "channels.tool.action.write": "\u270f\ufe0f 写入: {path}",
    "channels.tool.action.edit": "\U0001f527 编辑: {path}",
    "channels.tool.action.multi_edit": "\U0001f527 多处编辑: {path}",
    "channels.tool.action.glob": "\U0001f50d 搜索: {pattern}",
    "channels.tool.action.grep": "\U0001f50e 查找: {pattern}",
    "channels.tool.action.bash": "\U0001f4bb 执行: {command}",
    "channels.tool.action.ls": "\U0001f4c2 列目录: {path}",
    "channels.tool.action.todo_write": "\U0001f4dd 更新 Todo",
    "channels.tool.action.todo_read": "\U0001f4cb 读取 Todo",
    "channels.tool.action.web_fetch": "\U0001f310 获取网页: {url}",
    "channels.tool.action.web_search": "\U0001f50d 网络搜索: {query}",
    "channels.tool.action.bg_process": "\U0001f680 后台进程 {label}{secondary}",
    "channels.tool.action.fallback": "\U0001f528 {name}",
    # -----------------------------------------------------------------
    # channels.help.* — src/qai/channels/adapters/channel_help_text.py
    # user-facing /help / /cc help / /oc help reply bodies (D1 wave 4).
    # -----------------------------------------------------------------
    "channels.help.main": (
        "\U0001f4d6 微信 / 飞书 / Chat 指令帮助\n"
        "\n"
        "\u2328\ufe0f 普通对话指令：\n"
        "\n"
        "  /help  (/h)\n"
        "    显示此帮助信息。\n"
        "\n"
        "  /new  (/n)\n"
        "    保存当前会话历史后开启新会话，历史记录保留在 Chat 界面可查看。\n"
        "\n"
        "  /clear  (/cl)\n"
        "    删除当前会话历史（不保存）后开启新会话，历史记录将被永久移除。\n"
        "\n"
        "  /list [N]  (/l [N])\n"
        "    查看最近 N 条历史会话（默认 5 条），显示名称、时间和对话轮数。\n"
        "\n"
        "  /use <编号>  (/u <编号>)\n"
        "    切换到指定编号的历史会话继续对话。\n"
        "\n"
        "  /status  (/s)\n"
        "    查看当前会话状态（名称、对话轮数）。\n"
        "\n"
        "  /rename <新名称>  (/rn <新名称>)\n"
        "    重命名当前会话。\n"
        "\n"
        "  /delete  (/del)\n"
        "    删除当前会话（不可恢复），并开启新会话。\n"
        "\n"
        "  /stop  (/st)\n"
        "    立即停止当前正在执行的任务（普通对话或 Claude Code 任务均支持）。\n"
        "\n"
        "  /models  (/ms)\n"
        "    查看所有可用模型列表（本地 + 云端），并显示当前正在使用的模型。\n"
        "\n"
        "  /model  (/m)\n"
        "    查看当前会话使用的模型。\n"
        "\n"
        "  /model <编号>  (/m <编号>)\n"
        "    按 /models 编号切换模型，也可直接输入 model_id。发送 /model 0 恢复跟随全局设置。\n"
        "\n"
        "  /compact  (/c)\n"
        "    压缩当前会话的上下文。\n"
        "    /compact           立即强制压缩，显示压缩前后 token 对比\n"
        "    /compact status    查询当前 token 用量、digest 大小、升级提示\n"
        "    /compact migrate   基于当前 digest 派生新会话（P5 handoff）\n"
        "    /compact clear     清空压缩检查点（digest + ledger + 计数）\n"
        "\n"
        "  /reboot  (/r)\n"
        "    重启 QAIModelBuilder 服务，重启完成后微信通道将自动重连。\n"
        "\n"
        "\U0001f510 文件访问授权指令（FileGuard）：\n"
        "\n"
        "  /grant read <路径>  (/g read <路径>)\n"
        "    为当前会话授予指定路径的读取权限（会话结束后自动清除）。\n"
        "\n"
        "  /grant write <路径>  (/g write <路径>)\n"
        "    为当前会话授予指定路径的写入权限。\n"
        "\n"
        "  /grant exec <路径>  (/g exec <路径>)\n"
        "    为当前会话授予在指定路径执行命令的权限。\n"
        "\n"
        "  /grant list  (/g list)\n"
        "    查看当前会话已授权的路径列表。\n"
        "\n"
        "  /grant revoke <op> <路径>\n"
        "    撤销指定授权。例：/grant revoke read C:/WoS_AI/data\n"
        "\n"
        "  \U0001f4a1 授权仅在当前会话内有效，会话结束后自动清除。\n"
        "  \U0001f4a1 若 AI 工具调用被拒绝，可用 /grant 预授权路径，或联系管理员在\n"
        "     Settings > Security > IM 通道授权设置 中开启 WebUI 弹窗审批。\n"
        "\n"
        "\U0001f916 Claude Code AI 编程助手指令（别名 /code）：\n"
        "\n"
        "  /cc new <目录路径> [会话名称]\n"
        "    创建新的 Claude Code 会话，绑定到指定项目目录。\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    列出你的所有 Claude Code 会话（ID、名称、状态）。\n"
        "\n"
        "  /cc use <序号>  (/cc u <序号>)\n"
        "    按 /cc list 序号切换会话（如 /cc use 1）。\n"
        "\n"
        "  /cc use <ID前缀>\n"
        "    按 ID 前缀切换会话（输入 ID 前 8 位即可）。\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    查看当前 Claude Code 会话状态、对话轮次和工具调用次数。\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    查看 Claude Code 可用模型列表（带序号），并显示当前选中的模型。\n"
        "\n"
        "  /cc model  (/cc m)\n"
        "    查看当前 Claude Code 使用的模型。\n"
        "\n"
        "  /cc model <编号>  (/cc m <编号>)\n"
        "    按 /cc models 序号切换 Claude Code 模型。\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork 当前会话为新分支（保留原历史，下次发消息时生成新会话 ID）。\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    停止当前正在执行的 Claude Code 任务，停止后可立即发送新消息继续对话。\n"
        "\n"
        "  /cc cd [目录路径]\n"
        "    查看当前工作目录（无参数），或修改当前 CC 会话绑定的工作目录。\n"
        "\n"
        "  /cc rename <新名称>  (/cc r <新名称>)\n"
        "    重命名当前 Claude Code 会话。\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    退出 CC 模式（会话保留，可用 /cc use 重新进入）。\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    彻底删除当前 Claude Code 会话（不可恢复）。\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    显示 Claude Code 指令帮助。\n"
        "\n"
        "  <普通消息>（CC 会话激活时）\n"
        "    直接发消息即可与 Claude Code 对话，无需 /cc 前缀。\n"
        "    发送 /new 可切回普通 AI 对话模式（不影响 CC 会话）。\n"
        "\n"
        "\U0001f537 OpenCode AI 编程助手指令：\n"
        "\n"
        "  /oc new <目录路径> [会话名称]\n"
        "    创建新的 OpenCode 会话，绑定到指定项目目录。\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    列出你的所有 OpenCode 会话（ID、名称、状态、模型）。\n"
        "\n"
        "  /oc use <序号>  (/oc u <序号>)\n"
        "    按 /oc list 序号切换会话。\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    查看当前 OpenCode 会话状态、对话轮次和工具调用次数。\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    查看 OpenCode 可用模型列表。\n"
        "\n"
        "  /oc model [编号]  (/oc m [编号])\n"
        "    查看 / 切换 OpenCode 模型。\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    停止当前 OpenCode 任务。\n"
        "\n"
        "  /oc rename <新名称>  (/oc r <新名称>)\n"
        "    重命名当前 OpenCode 会话。\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    退出 OC 模式。\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    彻底删除当前 OpenCode 会话（不可恢复）。\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    显示 OpenCode 指令帮助。\n"
        "\n"
        "\U0001f4a1 提示：本地模型不可用时，若已配置云端模型，系统会自动切换并提前通知。\n"
        "\U0001f4a1 Claude Code 需在 Settings > AI Coding 中启用并配置认证信息。\n"
        "\U0001f4a1 OpenCode 需在 Settings > AI Coding > OpenCode 中启用并配置服务地址。"
    ),
    "channels.help.cc": (
        "\U0001f916 Claude Code 指令帮助（别名 /code）\n"
        "\n"
        "  /cc new <目录路径> [会话名称]\n"
        "    创建新的 Claude Code 会话，绑定到指定项目目录。\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    列出你的所有 Claude Code 会话。\n"
        "\n"
        "  /cc use <序号>  (/cc u <序号>)\n"
        "    按 /cc list 序号切换会话。\n"
        "\n"
        "  /cc use <ID前缀>\n"
        "    按 ID 前缀切换会话（输入 ID 前 8 位即可）。\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    查看当前会话状态、对话轮次和工具调用次数。\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    查看 Claude Code 可用模型列表（带序号），并显示当前选中的模型。\n"
        "\n"
        "  /cc model [编号]  (/cc m [编号])\n"
        "    查看 / 切换 Claude Code 模型。\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork 当前会话为新分支（保留原历史）。\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    停止当前正在执行的 Claude Code 任务。\n"
        "\n"
        "  /cc cd [目录路径]\n"
        "    查看当前工作目录，或修改 CC 会话绑定的工作目录。\n"
        "\n"
        "  /cc rename <新名称>  (/cc r <新名称>)\n"
        "    重命名当前会话。\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    退出 CC 模式（会话保留）。\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    彻底删除当前会话（不可恢复）。\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    显示此帮助。\n"
        "\n"
        "\U0001f4a1 创建会话后，直接发消息即可与 Claude Code 对话\n"
        "\U0001f4a1 /cc fork 可在关键节点保存进度，然后尝试不同方向\n"
        "\U0001f4a1 /cc stop 停止后可立即发送新消息继续对话\n"
        "\U0001f4a1 /cc close 退出后，会话仍保留，随时可用 /cc use 重新进入\n"
        "\U0001f4a1 发送 /new 可切回普通 AI 对话模式"
    ),
    "channels.help.oc": (
        "\U0001f537 OpenCode 指令帮助\n"
        "\n"
        "  /oc new <目录路径> [会话名称]\n"
        "    创建新的 OpenCode 会话。\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    列出你的所有 OpenCode 会话（ID、名称、状态、模型）。\n"
        "\n"
        "  /oc use <序号>  (/oc u <序号>)\n"
        "    按 /oc list 序号切换会话。\n"
        "\n"
        "  /oc use <ID前缀>\n"
        "    按 ID 前缀切换会话。\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    查看当前会话状态、对话轮次和工具调用次数。\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    查看 OpenCode 可用模型列表。\n"
        "\n"
        "  /oc model [编号]  (/oc m [编号])\n"
        "    查看 / 切换 OpenCode 模型。\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    停止当前正在执行的 OpenCode 任务。\n"
        "\n"
        "  /oc rename <新名称>  (/oc r <新名称>)\n"
        "    重命名当前会话。\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    退出 OC 模式（会话保留）。\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    彻底删除当前会话（不可恢复）。\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    显示此帮助。\n"
        "\n"
        "\U0001f4a1 创建会话后，直接发消息即可与 OpenCode 对话\n"
        "\U0001f4a1 /oc stop 停止后可立即发送新消息继续对话\n"
        "\U0001f4a1 /oc close 退出后，会话仍保留，随时可用 /oc use 重新进入\n"
        "\U0001f4a1 发送 /new 可切回普通 AI 对话模式\n"
        "\U0001f4a1 OpenCode 需在 Settings > AI Coding > OpenCode 中启用并配置服务地址"
    ),
    # -----------------------------------------------------------------
    # channels.conversation.* — qai/channels/application/use_cases/conversation_commands.py user-facing strings.
    # -----------------------------------------------------------------
    "channels.conversation.error.unknown_verb": "⚠️ 未知会话命令: /{verb}",
    "channels.conversation.error.invalid_limit": "⚠️ 参数必须为正整数，例如：/list 10",
    "channels.conversation.error.list_failed": "⚠️ 查询历史记录失败：{exc}",
    "channels.conversation.error.invalid_index": "⚠️ 编号必须为正整数，例如：/use 2",
    "channels.conversation.error.index_min": "⚠️ 编号从 1 开始",
    "channels.conversation.error.use_failed": "⚠️ 切换会话失败：{exc}",
    "channels.conversation.error.status_failed": "⚠️ 获取状态失败：{exc}",
    "channels.conversation.error.empty_name": "⚠️ 名称不能为空",
    "channels.conversation.error.rename_failed": "⚠️ 重命名失败：{exc}",
    "channels.conversation.error.delete_failed": "⚠️ 删除失败：{exc}",
    "channels.conversation.hint.no_history": "📋 暂无历史会话记录\n\n发送 /new 开启新会话",
    "channels.conversation.hint.use_usage": "用法：/use <编号>\n\n发送 /list 查看历史会话列表",
    "channels.conversation.hint.rename_usage": "用法：/rename <新名称>\n\n例如：/rename 项目讨论",
    "channels.conversation.list.header": "📋 历史会话（最近 {count} 条）：\n",
    "channels.conversation.list.round_suffix": " · {round_count}轮",
    "channels.conversation.list.footer_marker": "▶=当前会话",
    "channels.conversation.list.footer_hint": "💡 /use <编号> 切换到指定会话",
    # -----------------------------------------------------------------
    # channels.conversation_adapter.* — apps/api/_conversation_command_adapter.py
    # user-facing conversation-command replies (D5/wave-4 C4).
    # -----------------------------------------------------------------
    "channels.conversation_adapter.label.untitled": "（未命名）",
    "channels.conversation_adapter.label.follow_global": "跟随全局设置",
    "channels.conversation_adapter.list.empty": "📋 暂无历史会话记录",
    "channels.conversation_adapter.error.index_out_of_range": (
        "⚠️ 编号 {index} 超出范围（共 {total} 条历史）\n\n"
        "发送 /list 查看列表"
    ),
    "channels.conversation_adapter.ok.switched": (
        "✅ 已切换到会话：{title}\n"
        "共 {rounds} 轮对话历史\n\n"
        "直接发消息即可继续对话。"
    ),
    "channels.conversation_adapter.status.no_active": "📊 当前没有活跃会话\n\n直接发消息即可开始新对话。",
    "channels.conversation_adapter.status.header": "📊 当前会话状态",
    "channels.conversation_adapter.status.name": "名称：{title}",
    "channels.conversation_adapter.status.rounds": "对话轮数：{rounds} 轮",
    "channels.conversation_adapter.status.model": "当前模型：{model}",
    "channels.conversation_adapter.status.tool_calls": "工具调用：{count} 次",
    "channels.conversation_adapter.status.context": "上下文大小：{tokens} tokens（含历史）",
    "channels.conversation_adapter.status.conv_id": "会话 ID：{conv_id}…",
    "channels.conversation_adapter.status.hint": "\n💡 /rename <名称> 重命名  /delete 删除当前会话",
    "channels.conversation_adapter.error.no_active_conv": "⚠️ 当前没有活跃会话，请先发送一条消息开始对话",
    "channels.conversation_adapter.error.rename_unavailable": "⚠️ 重命名功能当前不可用",
    "channels.conversation_adapter.ok.renamed": "✅ 当前会话已重命名为：{new_name}",
    "channels.conversation_adapter.ok.deleted": "🗑 当前会话已删除，已开启新会话。",
    "channels.conversation_adapter.time.unknown": "未知时间",
    "channels.conversation_adapter.time.just_now": "刚刚",
    "channels.conversation_adapter.time.minutes_ago": "{n} 分钟前",
    "channels.conversation_adapter.time.hours_ago": "{n} 小时前",
    "channels.conversation_adapter.time.days_ago": "{n} 天前",
    # -----------------------------------------------------------------
    # channels.ai_coding.* — apps/api/_ai_coding_channel_bridge.py user-facing strings.
    # -----------------------------------------------------------------
    "channels.ai_coding.session.stopped_send_new": "\u23f9\ufe0f 当前任务已停止，可以发送新消息继续。",
    "channels.ai_coding.session.no_active_task": "\u2139\ufe0f 当前没有正在执行的 AI 编程任务。",
    "channels.ai_coding.session.usage_new": "\u2753 用法：/{verb} new <目录路径> [会话名称]",
    "channels.ai_coding.session.dir_not_found": "\u26a0\ufe0f 目录不存在：{workspace_path}",
    "channels.ai_coding.session.created": "\u2705 已创建 /{verb} 会话 {sid}\n工作目录：{workspace_path}",
    "channels.ai_coding.session.switched": "\u2705 已切换到会话 {session_id}",
    "channels.ai_coding.session.no_active": "\u2139\ufe0f 当前没有活动的 AI 编程会话。",
    "channels.ai_coding.session.usage_rename": "\u2753 用法：/{verb} rename [会话id] <新标题>",
    "channels.ai_coding.session.renamed": "\u2705 已重命名为：{new_title}",
    "channels.ai_coding.session.closed": "\u2705 已关闭会话 {sid}（历史保留，可用 /{verb} list 重新打开）",
    "channels.ai_coding.session.deleted": "\u2705 已删除会话 {sid}",
    "channels.ai_coding.session.usage_cd": "\u2753 用法：/{verb} cd <新工作目录>",
    "channels.ai_coding.session.current_dir_usage": "\U0001f4c1 当前工作目录：{cur_dir}\n\n用法：/{verb} cd <新工作目录>",
    "channels.ai_coding.session.cd_ok": "\u2705 已切换工作目录：{new_path}",
    "channels.ai_coding.session.no_upstream_no_fork": "\u26a0\ufe0f 当前会话尚未开始对话（未建立连接），无法 fork",
    "channels.ai_coding.session.forked": "\u2705 已 fork 出新会话 {new_sid}",
    "channels.ai_coding.session.stopped": "\u23f9\ufe0f 当前任务已停止。",
    "channels.ai_coding.session.opencode_aborted": "\u23f9\ufe0f 已中止 OpenCode 任务。可以发送新消息继续对话。",
    "channels.ai_coding.session.no_running_task": "\u2139\ufe0f 当前没有正在执行的任务。",
    "channels.ai_coding.session.models_header": "\U0001f4cb /{verb} 可用模型：",
    "channels.ai_coding.session.model_not_in_list": "\n\u2139\ufe0f 当前模型：{current}（不在列表中）",
    "channels.ai_coding.session.models_hint": "\n发送 /{verb} model <序号> 或 /{verb} model <模型名> 切换模型",
    "channels.ai_coding.session.model_current": "\U0001f5a5\ufe0f /{verb} 当前模型：{current}\n\n发送 /{verb} models 查看可用模型\n发送 /{verb} model <序号|模型名> 切换",
    "channels.ai_coding.session.model_switched": "\u2705 /{verb} 模型已切换为：{new_model}",
    "channels.ai_coding.error.command_failed": "\u26a0\ufe0f /{verb} {sub} 执行失败: {exc}",
    "channels.ai_coding.error.index_out_of_range": "\u26a0\ufe0f 序号 {ref} 超出范围（当前共 {count} 个会话）。发送 /cc list 查看。",
    "channels.ai_coding.error.multiple_matches": "\u26a0\ufe0f 匹配到多个会话，请提供更长的 ID 前缀。",
    "channels.ai_coding.error.model_index_out_of_range": "\u26a0\ufe0f 序号 {arg} 超出范围（当前共 {count} 个模型）。发送 /{verb} models 查看。",
    "channels.ai_coding.error.model_unavailable": "\u26a0\ufe0f /{verb} model 暂不可用（配置未注册）。",
    "channels.ai_coding.usage.no_content": "（AI 编程助手未返回内容）",
    "channels.ai_coding.usage.turn_warning_with_count": "⚠️ 当前会话已达到 {tc} 轮对话，建议尽快创建新会话。",
    "channels.ai_coding.usage.turn_warning_generic": "⚠️ 当前会话轮次较多，建议尽快创建新会话。",
    "channels.ai_coding.progress.subagent_started_desc": "\U0001f916 子任务已启动：{desc}",
    "channels.ai_coding.progress.subagent_started": "\U0001f916 子任务已启动",
    "channels.ai_coding.progress.subagent_done_desc": "\u2705 子任务完成：{desc}",
    "channels.ai_coding.progress.subagent_done": "\u2705 子任务完成",
    "channels.ai_coding.progress.subagent_error_desc": "\u26a0\ufe0f 子任务出错：{err}",
    "channels.ai_coding.progress.subagent_error": "\u26a0\ufe0f 子任务出错",
    "channels.ai_coding.progress.tool_count": "{tool_uses} 工具",
    "channels.ai_coding.progress.subagent_running": "\u23f3 子任务进行中 [{task_short}]",
    # TODO: channels.chat.* pending translation (see docs/30-ui-ux/i18n-implementation-plan.md)
    "channels.chat.compact.unavailable_module": "\u26a0\ufe0f /compact 当前不可用：chat 模块未启用",
    "channels.chat.compact.unavailable": "\u26a0\ufe0f /compact 当前不可用",
    "channels.chat.compact.failed": "\u26a0\ufe0f /compact 失败: {error}",
    "channels.chat.compact.done": "\u2705 /compact: {before_tokens} \u2192 {after_tokens} tokens（节省 {savings}）；已用 {budget_tokens} 预算的 {ratio_after_pct}%{escalation_suffix}。",
    "channels.chat.compact.status_ok": "\U0001f4ca /compact status: {used_tokens} / {budget_tokens} tokens（{ratio_pct}%）。Digest：{digest_bytes} 字节。Ledger：{ledger_count} 条。",
    "channels.chat.compact.status_escalate": "\u26a0\ufe0f /compact status: {used_tokens} / {budget_tokens} tokens（{ratio_pct}%）。Digest：{digest_bytes} 字节。Ledger：{ledger_count} 条。建议使用 /compact migrate 迁移到新会话。",
    "channels.chat.compact.migrate_ok": "\u2705 /compact migrate: 已基于当前 digest 派生新会话（{new_conversation_id}）。",
    "channels.chat.compact.migrate_no_digest": "\u26a0\ufe0f /compact migrate: 当前会话尚未生成会话摘要，无法迁移。请先发送 /compact 或让会话继续增长以生成摘要。",
    "channels.chat.compact.clear_ok": "\u2705 /compact clear: 已清空压缩检查点（digest、ledger、计数）。",
    "channels.chat.compact.clear_none": "\u2139\ufe0f /compact clear: 当前没有可清空的压缩检查点。",
    "channels.chat.compact.no_savings": "\u2139\ufe0f /compact: 会话已充分压缩，无可继续节省的空间（{tokens} tokens，已用 {budget_tokens} 预算的 {ratio_pct}%）。",
    "channels.chat.compact.empty_history": "\u2139\ufe0f /compact: 当前会话为空，无需压缩。",
    "channels.chat.compact.invalid": "\u26a0\ufe0f /compact: 未知子命令。可用：/compact、/compact status、/compact migrate、/compact clear。",
    "channels.chat.compact.escalation_suffix": "（建议使用 /compact migrate 迁移到新会话）",
    "channels.chat.compact.no_conversation": "\u2139\ufe0f /compact: 当前用户尚无活跃会话，请先发送一条消息。",
    "channels.chat.warning.turn_count": "\u26a0\ufe0f 当前会话已达到 {turn_count} 轮对话，建议尽快清理历史或创建新会话。",
    "channels.chat.warning.turn_generic": "\u26a0\ufe0f 当前会话轮次较多，建议尽快创建新会话。",
    "channels.chat.subagent.label_multi": "【子Agent {index}/{total}】",
    "channels.chat.subagent.label_solo": "【子Agent {index}】",
    "channels.chat.subagent.start_line": "\n{label} 开始执行...\n任务：{preview}\n",
    "channels.chat.subagent.done": "  \u2705 子Agent {index} 完成（{rounds} 轮）\n",
    "channels.chat.subagent.error": "  \u274c 子Agent {index} 出错：{message}\n",
    "channels.chat.agent_summary.header": "\n---\n\U0001f4cb 主Agent总结：\n",
    "channels.chat.label.wechat": "\u5fae\u4fe1",
    "channels.chat.label.feishu": "\u98de\u4e66",
    # -----------------------------------------------------------------
    # channels.dispatch.* — apps/api/_channel_dispatch_bridge.py user-facing
    # strings (D2 wave 4).
    # -----------------------------------------------------------------
    "channels.dispatch.error.invalid_command": "\u26a0\ufe0f 命令格式错误: {exc}",
    "channels.dispatch.error.bridge_unavailable": "\u26a0\ufe0f 处理服务暂时不可用，请稍后重试。",
    "channels.dispatch.error.application": "\u26a0\ufe0f {exc}",
    "channels.dispatch.error.unhandled": "处理消息时出错，请稍后重试。",
    "channels.dispatch.error.conversation_uc_unavailable": "\u26a0\ufe0f 会话管理命令当前不可用",
    "channels.dispatch.thinking_ack": "正在思考…",
    "channels.dispatch.stop.stopped": "\u23f9\ufe0f 当前任务已停止，可以发送新消息继续。",
    "channels.dispatch.stop.no_task": "\u2139\ufe0f 当前没有正在执行的任务。",
    "channels.dispatch.chat.empty_reply": "（模型未返回内容）",
    "channels.dispatch.ai_coding.empty_reply": "（AI 编程助手未返回内容）",
    "channels.dispatch.new.cleared": "当前会话已清除 \U0001f5d1",
    "channels.dispatch.new.opened": "已开启新会话 \u2728",
    "channels.dispatch.model.unavailable": "\u26a0\ufe0f /model 当前不可用",
    "channels.dispatch.model.default_label": "默认（未指定）",
    "channels.dispatch.model.show": (
        "\U0001f5a5\ufe0f 当前模型：{cur}\n\n"
        "发送 /models 查看可用模型\n"
        "发送 /model <序号|模型名> 切换\n"
        "发送 /model 0 恢复平台默认"
    ),
    "channels.dispatch.model.index_out_of_range": (
        "\u26a0\ufe0f 序号 {arg} 超出范围"
        "（当前共 {total} 个模型）。发送 /models 查看。"
    ),
    "channels.dispatch.model.failed": "\u26a0\ufe0f /model 失败: {exc}",
    "channels.dispatch.model.reset_ok": "\u2705 已恢复平台默认模型",
    "channels.dispatch.model.switched": "\u2705 已切换模型: {model_id}{auto_load_msg}",
    "channels.dispatch.model.autoload": "\n\n正在加载模型 {model_name}…",
    "channels.dispatch.model.autoload_with_status": "\n\n正在加载模型 {model_name}（{status}）…",
    "channels.dispatch.model.local_unavailable_fallback": "\u26a0\ufe0f 本地模型当前不可用，已自动切换到云端模型：{fallback_id}",
    "channels.dispatch.models.none": "\u2139\ufe0f 当前没有可用模型。",
    "channels.dispatch.models.header": "\U0001f4cb 可用模型（共 {total}）：",
    "channels.dispatch.models.local_header": "【本地模型】",
    "channels.dispatch.models.cloud_header": "【云端模型】",
    "channels.dispatch.models.status_running": "运行中",
    "channels.dispatch.models.status_unloaded": "未加载",
    "channels.dispatch.models.item": "  [{idx}] {name}  ({status})",
    "channels.dispatch.models.cloud_item": "  [{idx}] {cid}",
    "channels.dispatch.models.current": "\n当前模型：{current}",
    "channels.dispatch.models.hint": "\n发送 /model <序号> 切换模型；/model 0 恢复默认",
    "channels.dispatch.grant.unavailable": "\u26a0\ufe0f /grant 当前不可用：安全模块未接入",
    "channels.dispatch.grant.usage": (
        "\u26a0\ufe0f /grant 用法：\n"
        "  /grant read <路径>    — 授予读取权限\n"
        "  /grant write <路径>   — 授予写入权限\n"
        "  /grant exec <路径>    — 授予执行权限\n"
        "  /grant list           — 查看当前授权\n"
        "  /grant revoke <op> <路径> — 撤销授权\n\n"
        "  例：/grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.grant.revoke_usage": (
        "\u26a0\ufe0f /grant revoke 用法：\n"
        "  /grant revoke <op> <path>\n"
        "  其中 op ∈ read / write / exec"
    ),
    "channels.dispatch.grant.invalid_op": (
        "\u26a0\ufe0f 无效的操作类型：{op}"
        "（应为 read / write / exec）"
    ),
    "channels.dispatch.grant.needs_op_and_path": "\u26a0\ufe0f /grant 需要指定路径和操作 (read / write / exec)",
    "channels.dispatch.grant.usage_add": (
        "\u26a0\ufe0f /grant 用法：/grant <op> <path>\n"
        "  其中 op ∈ read / write / exec\n"
        "  例：/grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.reboot.unavailable": "\u26a0\ufe0f /reboot 当前不可用",
    "channels.dispatch.reboot.failed": "\u26a0\ufe0f /reboot 失败: {exc}",
    "channels.dispatch.reboot.scheduled": "\u2705 已请求重启服务，进程将在数秒内退出并由守护进程拉起。",
    "channels.dispatch.image.download_failed": "收到一张图片（下载失败，无法显示）",
    "channels.ai_coding_notify.sync.question": "[WebUI 提问]\n{question}",
    "channels.ai_coding_notify.sync.reply": "[Claude Code 回复]\n{reply}",
    "channels.ai_coding_notify.task_done.with_summary": "\u2705 任务完成\n{summary}",
    "channels.ai_coding_notify.task_done.plain": "\u2705 任务完成",
    "channels.grant.error.no_active_session": "\u26a0\ufe0f /grant 需要先 /cc 或 /oc 启动会话",
    "channels.grant.error.needs_path": "\u26a0\ufe0f /grant 需要指定路径",
    "channels.grant.error.needs_op": "\u26a0\ufe0f /grant 需要指定操作 (read / write / exec)",
    "channels.grant.error.needs_op_and_path": "\u26a0\ufe0f /grant 需要指定路径和操作 (read / write / exec)",
    "channels.grant.error.revoke_needs_path": "\u26a0\ufe0f /grant revoke 需要指定路径",
    "channels.grant.error.module_disabled": "\u26a0\ufe0f 安全模块未启用 /grant 功能",
    "channels.grant.error.grant_failed": "\u26a0\ufe0f /grant 失败: {exc}",
    "channels.grant.error.revoke_failed": "\u26a0\ufe0f /grant revoke 失败: {exc}",
    "channels.grant.error.list_failed": "\u26a0\ufe0f /grant list 失败: {exc}",
    "channels.grant.list.empty": "\u2139\ufe0f 当前会话没有授权的路径。",
    "channels.grant.list.header": "\U0001f4cb 当前会话授权列表：",
    "channels.grant.ok.granted": "\u2705 已授权: {path} ({op})",
    "channels.grant.ok.revoked": "\u2705 已撤销: {path}",
    "channels.grant.revoke.not_found": "\u2139\ufe0f 当前会话没有 {path} 的授权。",
    # ── Context-overflow recovery (chat streaming) ────────────────────────
    "chat.context_recovery.compressing": "上下文已满 \u2014 正在压缩对话历史并自动重试\u2026",
    "chat.context_recovery.succeeded": "上下文已压缩（{before_tokens} \u2192 {after_tokens} tokens），你的消息已完整重新发送。较早的历史已被摘要，因此本次回复稍慢。",
    "chat.context_recovery.failed_no_room": "上下文已满且无法进一步压缩。请发送 /compact 手动压缩历史，或发送 /new 开启新会话继续 \u2014 你的消息没有丢失。",
    "chat.context_recovery.unavailable_mid_tool": "上下文在工具执行过程中被占满，这种情况无法自动压缩。等本轮结束后，请发送 /compact 压缩历史，或发送 /new 开启新会话继续。",
}
