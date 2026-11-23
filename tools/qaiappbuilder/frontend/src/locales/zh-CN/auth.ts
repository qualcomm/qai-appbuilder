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

const auth = {
  account_menu: "账户菜单",
  backend_log_copied: "后端日志已复制到剪贴板",
  backend_log_copy_failed: "后端日志复制失败",
  backend_log_exported: "后端日志已导出",
  backend_log_export_failed: "后端日志导出失败",
  contact_support: "联系支持",
  copy_backend_log: "拷贝日志",
  copy_email: "复制邮箱",
  email_copied: "支持邮箱已复制",
  export_backend_log: "导出日志",
  no_mail_client_message: "这台电脑上没有找到邮件程序。请把问题发送到：\n\n{email}\n\n提示：可以用本菜单里的「拷贝日志」，再把日志粘贴到邮件里。",
  no_mail_client_title: "联系支持",
  prompt_hint: "您将被跳转到 Qualcomm 登录页面。",
  prompt_message: "请使用您的 Qualcomm 账号登录后继续使用 QAI AppBuilder。",
  prompt_title: "需要登录",
  redirecting: "正在跳转…",
  register_prompt: "还没有账号？点此注册 Qualcomm ID →",
  sign_in: "登录",
  sign_out: "登出",
  sign_out_confirm: "确定要登出吗？",
  signed_in: "已登录",
  welcome_back: "欢迎回来，{name}",
};

export default auth;
