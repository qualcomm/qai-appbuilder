// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
// 命名空间：builderEnv —— 模型转换环境（x64 Python 3.10 + QAIRT SDK）的下载页
// 安装卡片与聊天页顶部提示。key 结构必须与 en 完全一致（tsc + parity 测试强制）。
// =============================================================================

const builderEnv = {
  title: "模型转换环境",
  ready: "模型转换环境已安装，可以使用。",
  missing:
    "模型转换环境（Python 3.10 + QAIRT SDK）尚未安装。只有转换模型时才需要它，其他功能不受影响。",
  missingParts: "缺少",
  versionMismatch: "当前配置指向 QAIRT SDK {installed}，此版本需要 {expected}。",
  sizeHint:
    "需要下载约 4–5 GB（QAIRT SDK 约 2 GB，Python 依赖约 2.5 GB），支持断点续传，可随时取消，之后接着下载。",
  unavailable: "当前版本无法自动安装。",
  installing: "正在安装模型转换环境",
  keepUsing: "安装期间可以继续使用本应用。",
  failed: "安装失败",
  failedAt: "失败步骤",
  cancelled: "安装已取消。",
  resumeHint: "已下载的内容会保留，重试时会从中断处继续。",
  install: "安装",
  retry: "重试",
  cancel: "取消",
  eta: "剩余",
  options: "网络选项",
  mirror: "下载源",
  mirrorAuto: "自动选择（推荐）",
  mirrorOfficial: "官方源",
  mirrorCn: "国内镜像",
  proxy: "代理（可选）",
  proxyInvalid: "代理地址必须以 http://、https:// 或 socks5:// 开头",
  log: "安装输出",
  step: {
    python310: "Python 3.10",
    venv: "虚拟环境",
    deps: "Python 依赖包",
    sdk_download: "下载 QAIRT SDK",
    sdk_extract: "解压 QAIRT SDK",
    config: "写入配置",
    verify: "校验",
  },
  part: {
    config: "配置文件",
    sdk: "QAIRT SDK",
    venv: "Python 3.10 环境",
  },
  error: {
    network: "下载时网络出错。请检查网络、下载源或代理后重试。",
    disk_space: "磁盘可用空间不足。",
    uv_missing: "缺少 uv 工具，请先运行 setup_env.bat。",
    python_install: "无法安装 Python 3.10。",
    venv: "无法创建 Python 3.10 环境。",
    deps: "安装 Python 依赖包失败。",
    extract: "无法解压 QAIRT SDK。",
    permission: "没有写入 SDK 目录的权限，请以管理员身份运行。",
    verify: "已安装的环境未通过校验。",
    already_running: "已有另一个安装任务正在运行。",
    cancelled: "已取消。",
    spawn_failed: "无法启动安装程序。",
    unsupported_os: "只能在 Windows 上安装。",
    internal: "安装程序出现意外错误。",
  },
  banner: {
    missing: "模型转换需要一个额外的环境，目前尚未安装。",
    installing: "正在安装模型转换环境… {percent}%",
    failed: "模型转换环境安装失败。",
    cancelled: "模型转换环境的安装已取消。",
    goInstall: "前往下载页",
    viewProgress: "查看进度",
  },
};

export default builderEnv;
