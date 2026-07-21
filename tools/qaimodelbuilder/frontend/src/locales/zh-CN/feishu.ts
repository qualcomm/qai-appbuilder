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

const feishu = {
  acknowledgeFailed: "确认飞书错误失败",
  appId: "应用 ID",
  appSecret: "应用密钥",
  appSecretDesc: "敏感字段，保存后显示为 ****",
  autoConnect: "服务启动时自动连接飞书通道",
  bindFailed: "绑定失败",
  bindFailedToast: "绑定失败：{msg}",
  btn: {
    cancel: "取消",
    connect: "连接飞书",
    disconnect: "断开连接",
    retry: "重试连接",
    saveTitle: "保存配置",
  },
  channelControl: "通道控制",
  channelTitle: "飞书通道",
  configSaved: "飞书配置已保存",
  configSavedToast: "飞书配置已保存",
  connectionFailed: "连接失败",
  connectionOk: "连接成功",
  enable: "启用飞书通道",
  encryptKey: "加密密钥",
  encryptKeyDesc: "若飞书应用开启了事件加密，填写加密密钥；否则留空",
  globalProxyNotConfigured: "全局代理未配置，无法同步",
  idle: {
    hint: "配置飞书应用凭证后即可连接，通过 WebSocket 长连接接收消息，无需公网 IP",
  },
  info: "飞书信息",
  intro: {
    line1: "通过飞书开放平台 WebSocket 长连接接收消息，无需公网 IP。<br>",
    line2: "请在 <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">飞书开放平台</a> 创建自建应用，开启机器人能力，申请 <code>im:message</code>、<code>im:message:send_as_bot</code> 权限，并在「事件订阅」中选择 WebSocket 长连接模式，订阅 <code>im.message.receive_v1</code> 事件。",
  },
  label: {
    aiModel: "AI 模型",
    appId: "App ID",
    appSecret: "App Secret",
    appSecretSaved: "已保存",
    encryptKey: "Encrypt Key",
    verifyToken: "Verify Token",
  },
  model: {
    default: "默认（跟随全局设置）",
  },
  modelDefault: "默认（跟随全局设置）",
  modelSaved: "飞书模型已保存",
  notifyBound: "飞书通知已绑定到 {id}",
  notifyBoundToast: "飞书通知已绑定到 {id}",
  notifyCleared: "飞书通知已清除",
  notifyClearedToast: "飞书通知已清除",
  optionalSuffix: "（可选）",
  placeholder: {
    appSecret: "输入应用密钥",
    encryptKey: "开启事件加密时填写（可选）",
    encryptKeyShort: "开启事件加密时填写",
    saved: "已保存（输入新值以更新）",
    searchModel: "搜索模型...",
    verifyToken: "事件验证 Token（可选）",
  },
  proxy: {
    address: "代理地址",
    label: "代理",
    noAuthHint: "留空则无认证",
    password: "密码",
    sectionTitle: "代理设置（企业网络可选）",
    syncGlobal: "同步全局代理",
    syncTitle: "从全局代理同步",
    title: "代理设置",
    username: "用户名",
  },
  proxySaved: "代理设置已保存",
  proxySyncedFromGlobal: "已从全局代理同步地址和用户名，请输入密码后保存",
  refreshStatus: "刷新状态",
  registerFailed: "注册飞书失败",
  runningHint: "飞书通道运行中，机器人已连接飞书服务器，可接收消息。",
  saveConfig: "保存配置",
  saveConfigFailed: "保存飞书配置失败",
  saveFailed: "保存失败",
  saveFailedToast: "保存失败：{msg}",
  savingShort: "保存中…",
  start: "启动",
  startFailed: "飞书通道启动失败",
  startFailedFallback: "启动失败",
  startFailedToast: "飞书通道启动失败：{msg}",
  starting: "飞书通道启动中…",
  startingToast: "飞书通道启动中…",
  status: {
    connected: "飞书已连接，正在接收消息",
    connecting: "正在连接飞书服务器…",
    error: "通道出现错误",
  },
  statusText: {
    errorShort: "错误",
    running: "运行中",
    starting: "启动中…",
    stopped: "已停止",
  },
  stop: "停止",
  stopChannelFailed: "停止飞书失败",
  stopFailed: "停止失败",
  stopFailedToast: "停止失败：{msg}",
  stopped: "飞书通道已停止",
  stoppedToast: "飞书通道已停止",
  syncDisabled: "已关闭飞书同步",
  syncEnabled: "已开启飞书同步",
  syncFailed: "同步失败",
  syncSetFailed: "设置飞书同步失败",
  testConnection: "测试连接",
  title: "飞书配置",
  verificationToken: "验证 Token",
};

export default feishu;
