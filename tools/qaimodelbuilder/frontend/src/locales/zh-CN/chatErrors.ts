// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// chatErrors 命名空间 —— 声明式聊天错误注册表文案（标题、面向用户的简明消息、
// 操作按钮标签）+ TLS 安全警告对话框。由 chatErrorActions.ts（消息/标签 key）与
// useChatErrorActions.ts（对话框 + toast key）消费。en / zh-CN / zh-TW 的 key
// 结构须完全一致（由 tsc + locale parity 测试强制）。
// =============================================================================

const chatErrors = {
  generic: "请求失败。你可以重试，或复制诊断信息以查看详情。",
  messages: {
    tlsCertUntrusted:
      "无法验证模型服务的 TLS 证书（可能是自签名或企业网关证书）。关闭校验会降低安全性，请仅在你信任该服务时使用。",
    tlsHostnameMismatch: "证书与服务地址不匹配，通常是 base_url 主机名有误。",
    tlsCertExpired: "模型服务的 TLS 证书已过期，请联系服务方或修正 base_url。",
    tlsHandshakeFailed: "与模型服务的 TLS 握手失败，请检查 base_url 和网络。",
    dnsError: "无法解析模型服务地址，请检查 base_url、VPN 或网络。",
    connectionRefused:
      "模型服务拒绝连接，请确认服务已启动、端口/base_url 正确。",
    hostUnreachable: "无法连通模型服务主机，请检查 base_url 和网络。",
    networkExhausted: "网络长时间未恢复，已停止自动重连。",
    serverError: "模型服务暂时故障，已重试多次仍失败。",
    authFailed: "认证失败，API Key 无效或已过期。",
    permissionDenied: "无权访问该模型（可能未授权或区域限制）。",
    modelUnavailable: "未找到该模型或接口，请更换模型或检查配置。",
    unsupportedParam:
      "该模型不支持某个采样参数，请在云模型设置中关闭它。",
    promptTooLong: "提示词超出模型的上下文窗口，请压缩对话后重试。",
    throttling: "被模型服务限流，请稍后重试。",
    contentFiltered: "请求被模型的内容过滤拦截。",
  },
  actions: {
    disableTlsAndRetry: "仅信任并关闭校验后重试",
    openProviderSettings: "打开云模型设置",
    setApiKey: "设置 API Key",
    selectModel: "选择模型",
    switchModel: "换个模型重试",
    compressContext: "压缩上下文",
    copyDiagnostics: "复制诊断信息",
  },
  tlsWarning: {
    title: "关闭 TLS 校验？",
    message:
      "关闭后将不再验证服务器证书，存在中间人攻击风险。仅在你确认该服务可信时继续。",
    confirm: "关闭并重试",
    cancel: "取消",
    disabledToast: "已关闭 TLS 校验，正在重试…",
  },
  compressHint: "在聊天中发送 /compact 压缩对话历史，然后重试。",
  diagnosticsCopied: "诊断信息已复制到剪贴板",
  diagnosticsCopyFailed: "无法复制到剪贴板",
};

export default chatErrors;
