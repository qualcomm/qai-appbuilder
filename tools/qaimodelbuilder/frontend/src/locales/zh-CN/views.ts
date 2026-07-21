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

const views = {
  channels: {
    description: "管理消息通道，连接外部平台",
    title: "通道",
  },
  chat: {
    description: "多标签对话工作区。标签、流式输出与工具调用将在 PR-054 接入。",
    placeholder: "对话工作区将显示在这里。",
    title: "聊天",
  },
  downloads: {
    description: "模型与资源下载。通过 SSE 流式上报进度。",
    title: "下载",
  },
  security: {
    description: "FileGuard 安全策略与权限管理",
    title: "安全",
  },
  service: {
    description: "本地服务守护进程 — 状态、重启、日志。",
    title: "服务",
  },
  settings: {
    build_info_error: "加载构建信息失败。",
    build_info_heading: "构建信息",
    build_info_loading: "正在加载构建信息…",
    description: "应用偏好与构建信息。",
    field_data_dir: "数据目录",
    field_edition: "版本类型",
    field_name: "名称",
    field_python_path: "Python 路径",
    field_version: "版本",
    title: "设置",
  },
  skills: {
    description: "技能注册与审核队列。在 PR-053 与 Chat 一起实现。",
    title: "技能",
  },
};

export default views;
