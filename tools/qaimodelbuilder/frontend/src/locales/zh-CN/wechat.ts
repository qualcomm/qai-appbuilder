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

const wechat = {
  agentId: "应用 ID",
  autoConnect: "服务启动时自动连接微信通道",
  btn: {
    connect: "连接微信",
    disconnect: "断开连接",
    refreshQr: "↻ 刷新二维码",
    regetQr: "重新获取二维码",
    retry: "重试连接",
    saveTitle: "保存配置",
  },
  configSaved: "微信配置已保存",
  connectionFailed: "连接失败",
  connectionOk: "连接成功",
  corpId: "企业 ID",
  enable: "启用微信通道",
  encodingAesKey: "消息加密密钥",
  globalProxyNotConfigured: "全局代理未配置，无法同步",
  idle: {
    hint: "用手机微信扫码即可连接，开始收发消息",
  },
  label: {
    aiModel: "AI 模型",
  },
  loginFailed: "微信登录失败",
  model: {
    default: "默认（跟随全局设置）",
  },
  modelDefault: "默认（跟随全局设置）",
  modelSaved: "微信模型已保存",
  modelSavedToast: "微信模型已保存",
  placeholder: {
    searchModel: "搜索模型...",
  },
  proxy: {
    address: "代理地址",
    label: "代理",
    noAuthHint: "留空则无认证",
    password: "密码",
    sectionTitle: "代理设置（企业网络可选）",
    syncGlobal: "同步全局代理",
    syncTitle: "从 设置 > 应用配置 > 网络代理 同步代理地址和用户名",
    title: "代理设置",
    username: "用户名",
  },
  proxyGlobalNotConfigured: "全局代理未配置，无法同步",
  proxySaved: "代理设置已保存",
  proxySavedToast: "代理设置已保存",
  proxySyncedFromGlobal: "已从全局代理同步地址和用户名，请输入密码后保存",
  proxySyncedFromGlobalToast: "已从全局代理同步地址和用户名，请输入密码后保存",
  qr: {
    alt: "微信登录二维码",
    countdown: "{seconds}s 后自动刷新",
  },
  qrIssueFailed: "发起二维码登录失败",
  registerFailed: "注册微信失败",
  saveConfig: "保存配置",
  saveConfigFailed: "保存微信配置失败",
  saveFailed: "保存失败",
  saveFailedToast: "保存失败：{msg}",
  secret: "密钥",
  status: {
    connected: "微信已连接，正在接收消息",
    error: "通道出现错误",
    expired: "二维码已过期",
    scanHint: "用手机微信扫描上方二维码",
    scanned: "✅ 已扫码，请在手机上确认",
  },
  syncDisabled: "已关闭微信同步",
  syncDisabledToast: "已关闭微信同步",
  syncEnabled: "已开启微信同步",
  syncEnabledToast: "已开启微信同步",
  syncFailed: "同步失败",
  syncFailedToast: "同步失败：{msg}",
  syncSetFailed: "设置微信同步失败",
  syncSetFailedToast: "设置微信同步失败: {msg}",
  testConnection: "测试连接",
  title: "微信配置",
  token: "Token",
};

export default wechat;
