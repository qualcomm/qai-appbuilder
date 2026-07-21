// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
// zh-CN 版：与 en/modeIntro.ts key 结构完全一致（parity 由 tsc/测试强制）。
// =============================================================================

import type { MessageSchema } from "../schema";

const modeIntro: MessageSchema["modeIntro"] = {
  dontShowAgain: "不再显示此提示",
  triggerLabel: "引导",
  appBuilder: {
    title: "App Builder 快速指引",
    subtitle: "把已训练好的模型 + 前后处理逻辑打包为可安装的 App Builder pack。",
    step1: "在模型菜单中勾选要用来构建的已导入模型。",
    step2: "描述你希望这个 App 做什么，Agent 会自动搭好代码骨架。",
    step3: "打开「我的 Apps」运行、打包或删除已生成的应用。",
    chipMyApps: "打开我的 Apps",
    chipPromote: "推送到 App Builder",
  },
  gomaster: {
    title: "GoMaster 快速指引",
    subtitle: "云端驱动的一键 ONNX 最佳化：上传 → 优化 → 下载。",
    step1: "点击「开始最佳化」，选择你的 ONNX 模型文件。",
    step2: "任务在云端运行，进度会实时推送到抽屉里。",
    step3: "完成后即可下载优化后的模型与性能报告。",
    chipOptimize: "开始最佳化",
  },
  modelBuilder: {
    title: "Model Builder 快速指引",
    subtitle: "把源模型转换为 QNN/SNPE 运行包并推送到 App Builder。",
    step1: "上传源模型文件，或指定一个模型工作目录。",
    step2: "选择量化精度（fp16 / w8a8 等）。",
    step3: "使用「推送到 App Builder」将转换后的 pack 注册给 App Builder 使用。",
    chipPromote: "推送到 App Builder",
    emptyExamplesTitle: "试试这些",
    ex1Label: "把 ONNX 模型转换为 w8a8",
    ex1Prompt: "把我的 ONNX 模型转换为 QNN DLC context binary，精度用 w8a8，然后在 NPU 上验证推理。如果需要模型路径请向我询问。",
    ex2Label: "量化并对比精度",
    ex2Prompt: "把我的模型分别转换为 fp16 和 w8a8，用一个样例输入各跑一次推理，并报告两者输出的余弦相似度，让我判断精度损失。",
    ex3Label: "把已转换的模型导出到 App Builder",
    ex3Prompt: "我的工作目录里已经有一个转换好的模型，请把它打包成 app_pack 并推送到 App Builder，方便我在其上构建应用。",
  },
  modelHub: {
    title: "模型市场 快速指引",
    subtitle: "从 Qualcomm AI Hub 下载预编译模型，一键推送到 App Builder。",
    step1: "在 Qualcomm AI Hub 中浏览或搜索预编译模型。",
    step2: "将模型包直接下载到你的工作目录。",
    step3: "使用「推送到 App Builder」把它注册给 App Builder 使用。",
    chipPromote: "推送到 App Builder",
    emptyExamplesTitle: "试试这些",
    ex1Label: "下载 ResNet50 并分类图片",
    ex1Prompt: "从 Qualcomm AI Hub 下载预编译的 ResNet50 图像分类模型，然后在 NPU 上对一张测试图片做分类推理并打印 Top-5 结果。请一次性跑完整个流程。",
    ex2Label: "下载 YOLOv8 做目标检测",
    ex2Prompt: "从 Qualcomm AI Hub 下载预编译的 YOLOv8 目标检测模型，在 NPU 上对一张样例图片做检测并画出检测框。",
    ex3Label: "下载模型并推送到 App Builder",
    ex3Prompt: "从 Qualcomm AI Hub 下载一个预编译的图像分类模型（如 Inception v3），在 NPU 上验证推理通过后，把它打包并推送到 App Builder。",
  },
  pro: {
    title: "增强模式（Pro）快速指引",
    subtitle: "把繁重的模型转换任务交给远程 GPU Agent 自动完成。",
    step1: "点「设置」配置远程 GPU Agent 的地址、账号与端口。",
    step2: "点「连接」——系统会自动挑一台空闲机器建立会话。",
    step3: "像日常聊天一样描述转换需求，Agent 会自动接管全流程。",
    chipSettings: "打开设置",
    chipConnect: "连接 GPU Agent",
  },
  code: {
    title: "编程模式快速指引",
    subtitle: "让模型读懂你的代码，帮你分析、修改、生成代码。支持任意 OpenAI 兼容 API。",
    step1: "选一个「专家角色」（Persona），决定模型的思考侧重。",
    step2: "上传文件或粘贴 Git 仓库 URL，让模型看到代码上下文。",
    step3: "在对话框里描述任务，模型会读代码并给出改动建议或补丁。",
    chipPersona: "选择专家角色",
    chipContext: "上传代码 / 仓库",
  },
};

export default modeIntro;
