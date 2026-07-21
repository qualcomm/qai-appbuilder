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

const channels = {
  callbackUrl: "回调地址",
  configure: "配置",
  connect: "连接",
  connected: "已连接",
  copied: "回调地址已复制",
  autoStart: "服务启动时自动开启此通道",
  autoStartSaveFailed: "更新自动开启设置失败",
  configSaved: "飞书配置已保存",
  configSaveFailed: "保存飞书配置失败",
  settingsSaved: "通道设置已保存",
  settingsSaveFailed: "保存通道设置失败",
  settingsLoadFailed: "加载通道设置失败",
  legacy_notice: "此视图使用的是旧版组件路径，请更新到 FeishuConfigPanel。",
  modelLabel: "AI 模型",
  modelFollowGlobal: "默认（跟随全局设置）",
  modelSearch: "搜索模型…",
  modelNoCandidates: "尚未配置云端模型",
  modelSaved: "通道模型已保存",
  modelSaveFailed: "保存通道模型失败",
  proxyTitle: "代理设置",
  proxyLabel: "代理",
  proxyAddress: "代理地址",
  proxyAddressPlaceholder: "http://proxy:8080",
  proxyUsername: "用户名",
  proxyUsernamePlaceholder: "（可选）",
  proxyPassword: "密码",
  proxyPasswordPlaceholder: "（不修改）",
  proxySyncGlobal: "同步全局代理",
  proxySynced: "已从全局代理同步（请记得保存）",
  proxySaved: "代理设置已保存",
  proxySaveFailed: "保存代理设置失败",
  settings_btn: "设置",
  start: "启动",
  starting: "正在启动飞书通道…",
  started: "飞书通道已启动。",
  stop: "停止",
  stopping: "正在停止飞书通道…",
  stopped: "飞书通道已停止。",
  disableChannel: "禁用通道",
  disconnect: "断开",
  disconnected: "未连接",
  enableChannel: "启用通道",
  scan_wechat: "用手机微信扫描上方二维码",
  qr_scanned: "已扫码，请在手机上确认",
  qr_expired: "二维码已过期",
  qr_countdown: "{seconds}s 后自动刷新",
  qr_refresh: "刷新二维码",
  qr_reget: "重新获取二维码",
  feishu: {
    cardDesc: "通过飞书开放平台 WebSocket 长连接接入，无需公网 IP",
    connect: "连接飞书",
    connectedMsg: "飞书已连接，正在接收消息",
    connectingMsg: "正在连接飞书服务器…",
    errorMsg: "通道出错",
    idleHint: "填写飞书应用凭证以通过 WebSocket 长连接接入，无需公网 IP",
    introLine1: "通过飞书开放平台 WebSocket 长连接接收消息，无需公网 IP。<br>",
    // introLine2 拆分：把「飞书开放平台」外链从段落里抽出到独立成行的按钮式外链，
    // 段落只保留说明文本（无 <a>），按钮由 FeishuConfigPanel 模板自行构造。
    introLine2Prefix: "请在飞书开放平台创建自建应用，开启机器人能力，申请 <code>im:message</code>、<code>im:message:send_as_bot</code> 权限，并在「事件订阅」中选择 WebSocket 长连接模式，订阅 <code>im.message.receive_v1</code> 事件。",
    openPlatformLabel: "打开飞书开放平台",
    openPlatformTooltip: "跳转到飞书开放平台",
    refreshStatus: "刷新状态",
    runningHint: "飞书通道运行中，机器人已连接飞书服务器，可接收消息。",
    info: {
      btnTitle: "飞书通道说明",
      subtitle: "飞书开放平台 · WebSocket 长连接",
      title: "飞书通道说明",
      stepsTitle: "⚙️ 配置步骤",
      step1: "在 <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">飞书开放平台</a> 创建自建应用",
      step2: "开启<strong>机器人</strong>能力",
      step3: "申请 <code>im:message</code>、<code>im:message:send_as_bot</code> 权限",
      step4: "在「事件订阅」中选择 <strong>WebSocket 长连接</strong>模式",
      step5: "订阅 <code>im.message.receive_v1</code> 事件",
      step6: "将 App ID 和 App Secret 填入配置并保存",
      advantagesTitle: "✅ 优势",
      adv1: "<strong>无需公网 IP</strong>：通过 WebSocket 长连接主动接收消息，无需配置服务器",
      adv2: "<strong>无需扫码</strong>：使用应用凭证（App ID + Secret）直接认证，重启自动重连",
      adv3: "<strong>支持图片</strong>：AI 可自动识别并描述飞书消息中的图片内容",
      adv4: "<strong>自动切换模型</strong>：本地模型不可用时自动切换到云端模型并提前通知",
      notesTitle: "⚠️ 注意事项",
      note1: "每位飞书用户的对话历史<strong>独立保存</strong>，可在 Chat 界面查看历史记录",
      note2: "若企业网络有代理，请在「代理设置」中配置代理地址",
      note3: "App Secret 为敏感字段，保存后显示为 ****，不会明文存储",
    },
    name: "飞书",
    status: {
      error: "出错",
      running: "已连接",
      starting: "连接中…",
      stopped: "未连接",
    },
  },
  feishuDesc: "连接飞书机器人消息",
  guideBtn: "使用指南",
  settingsBtn: "通道设置",
  guide: {
    subtitle: "微信 · 飞书 · 通用指令",
    title: "机器人通道使用说明",
    smartTitle: "🔄 智能模型切换",
    smart1: "当通道配置的模型为<strong>本地模型</strong>（或跟随全局设置且全局选择的是本地模型）时，若本地模型服务<strong>未启动或不可用</strong>，系统会自动检测并尝试回退。",
    smart2: "若检测到有<strong>可用的云端模型</strong>，系统会在回复前先发送一条提示：<br><em style=\"color:var(--text-secondary)\">⚠️ 本地模型当前不可用，已自动切换到云端模型：xxx</em>",
    smart3: "若<strong>没有配置任何云端模型</strong>，则仍会尝试路由到本地服务，此时消息可能会失败。建议提前配置至少一个云端模型作为备选。",
    smart4: "本地模型恢复后，可发送 <strong>/model 0</strong> 恢复跟随全局设置。",
  },
  cmd: {
    sectionTitle: "⌨️ 普通对话指令",
    help: "显示完整指令帮助",
    new: "保存当前会话后开启新会话",
    clear: "删除当前会话历史（不保存）后开启新会话",
    list: "查看最近 N 条历史会话（默认 5 条）",
    use: "切换到指定历史会话",
    status: "查看当前会话状态（名称、对话轮数）",
    rename: "重命名当前会话",
    delete: "删除当前会话（不可恢复），并开启新会话",
    stop: "立即停止当前正在执行的任务",
    models: "查看可用模型列表",
    model: "查看/切换模型；/model 0 恢复全局设置",
    compact: "查看/临时修改当前会话历史轮次",
    reboot: "重启整个服务，重启后微信和飞书通道将自动重连",
    rebootWechat: "重启服务，重启后微信通道自动重连",
    rebootFeishu: "重启服务，重启后飞书通道自动重连",
    helpFull:
      "显示此使用说明（所有可用指令列表）。<br><em>回复：完整的指令帮助信息</em>",
    newFull:
      "<strong>保存</strong>当前会话历史后开启新会话，历史记录保留在 Chat 界面可查看。<br><em>回复：已开启新会话 ✨</em>",
    clearFull:
      "<strong>删除</strong>当前会话历史（不保存）后开启新会话，Chat 界面中该记录将被永久移除。<br><em>回复：当前会话已清除 🗑</em>",
    listFull: "查看最近 N 条历史会话（默认 5 条），显示名称、时间和对话轮数。",
    useFull: "切换到指定编号的历史会话继续对话。",
    statusFull: "查看当前会话状态（名称、对话轮数、上下文大小）。",
    renameFull: "重命名当前会话。",
    deleteFull: "删除当前会话（不可恢复），并开启新会话。",
    stopFull:
      "<strong>立即停止</strong>当前正在执行的任务（普通对话或 Claude Code 任务均支持）。<br><em>回复：⏹️ 当前任务已停止，可以发送新消息继续。</em>",
    modelsFull:
      "查看当前所有<strong>可用模型列表</strong>（本地 + 云端），并显示当前正在使用的模型。<br><em>回复：带编号的模型列表</em>",
    modelFull:
      "按 <strong>/models</strong> 列表中的编号<strong>切换模型</strong>，也可直接输入 model_id。发送 <strong>/model 0</strong> 恢复跟随全局设置。<br><em>回复：✅ 已切换到模型：xxx</em>",
    compactFull:
      "查看或<strong>临时修改</strong>当前会话保留的历史轮次。<br><em>回复：✅ 当前会话历史轮次已设为 n 轮</em>",
    rebootFull:
      "重启整个 QAIModelBuilder 服务，重启完成后微信和飞书通道将自动重连。<br><em>回复：系统正在重启，请稍候... 🔄</em>",
  },
  help: "帮助",
  settings: {
    history: {
      desc: "飞书 + 微信通道共用。超出后自动删除最旧的完整轮次。",
      hint: "飞书/微信通道保留的最大对话轮次。一轮 = 用户提问一次 + 期间所有工具调用 + 最终 AI 回复。",
      label: "对话历史轮次",
      unit: "轮",
    },
    subtitle: "飞书 · 微信通道共用参数",
    title: "通道公共设置",
  },
  status: "状态",
  subtitle: "管理消息通道，连接外部平台",
  title: "通道",
  wechat: {
    cardDesc: "通过 iLink Bot 接入个人微信，收发文本与图片消息",
    info: {
      btnTitle: "微信通道说明",
      subtitle: "iLink Bot · 个人微信接入",
      title: "微信通道说明",
      notesTitle: "⚠️ 注意事项",
      note1: "本通道通过 <strong>iLink Bot</strong> 接入个人微信，需在手机上扫码授权。",
      note2: "凭证保存在本地，重启服务后通常可<strong>自动重连</strong>，无需重复扫码。",
      note3: "若长时间未使用或微信账号在其他设备登录，凭证可能失效，需重新扫码。",
      note4: "每位微信用户的对话历史<strong>独立保存</strong>，可在 Chat 界面查看历史记录。",
      note5: "支持发送<strong>图片消息</strong>，AI 将自动识别并描述图片内容。",
    },
    name: "微信（个人号）",
    connect: "连接微信",
    rescan: "重新扫码登录",
    rescanTitle: "强制重新扫码登录（忽略已保存凭证）",
    connectedMsg: "微信已连接，正在接收消息",
    refreshStatus: "刷新状态",
    runningHint: "微信通道运行中，机器人已连接，可接收消息。",
    errorMsg: "通道出现错误",
    idleHint: "用手机微信扫码即可连接，开始收发消息",
    status: {
      connected: "已连接",
      error: "出错",
      expired: "已过期",
      idle: "未连接",
      logging_in: "等待扫码",
      scanned: "已扫码",
    },
  },
  wechatDesc: "连接企业微信机器人消息",
};

export default channels;
