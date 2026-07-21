# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Help-text formatters for channel commands (S9 PR-093 §2.4 L-5).

Restores the three Chinese help strings (``format_main_help`` /
``format_cc_help`` / ``format_oc_help``) that the legacy
``backend/channels/help_text.py`` provided to WeChat / Feishu /
WebUI Chat users.  Without this adapter ``/help`` / ``/cc help`` /
``/oc help`` would fall through to the unknown-command path —
addressed by parity-audit row §2.4 L-5.

Pure functions, no I/O, no globals; the channel dispatch bridge
(:mod:`apps.api._channel_dispatch_bridge`) imports
:func:`format_main_help` etc. and feeds the returned string straight
into the realtime delivery service.

Channel-specific divergence
---------------------------
The legacy code accepted a ``channel`` parameter (``"wechat"`` /
``"feishu"`` / ``"webui"``) but rendered identical text for all
three; the parameter is preserved here so callers don't need to
change but the body is the single shared text.  Future per-channel
tweaks (e.g. removing ``/reboot`` from ``webui``) can be added by
branching on ``channel`` without breaking callers.
"""

from __future__ import annotations

__all__ = [
    "format_main_help",
    "format_cc_help",
    "format_oc_help",
]


def format_main_help(channel: str = "wechat") -> str:
    """Return the main ``/help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_main_help` from
    the legacy code path verbatim so existing channel users see no
    text regression.  ``channel`` is accepted for forward-compat but
    currently unused — every channel renders the same body.
    """
    _ = channel  # accepted for parity; future per-channel branching hook
    return (
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
        "  /compact [轮次]  (/c [轮次])\n"
        "    裁剪当前会话的历史消息，保留最近 N 轮对话。\n"
        "    /compact        查看当前全局默认轮次设置\n"
        "    /compact <n>    裁剪当前会话，只保留最近 n 轮对话\n"
        "    /compact 0      使用全局默认值裁剪\n"
        "    全局默认值可在 Settings > App Config > Channels 中配置。\n"
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
    )


def format_cc_help(channel: str = "wechat") -> str:
    """Return the ``/cc help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_cc_help`.
    """
    _ = channel
    return (
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
    )


def format_oc_help(channel: str = "wechat") -> str:
    """Return the ``/oc help`` reply text (S9 PR-093 §2.4 L-5).

    Mirrors :func:`backend.channels.help_text.format_oc_help`.
    """
    _ = channel
    return (
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
    )
