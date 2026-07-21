// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// modeIntro (Plan §7 decision 5 — C+D): the collapsible mode-intro card
// rendered by ChatMessageList at the top of the message list when the tab
// is in App Builder / GoMaster / Model Builder mode AND already has messages
// (empty-state case is covered by the mode's dedicated empty-state screen).
// =============================================================================

const modeIntro = {
  dontShowAgain: "Don't show again",
  triggerLabel: "Guide",
  appBuilder: {
    title: "App Builder — quick reference",
    subtitle: "Package trained models + pre/post-processing into an installable App Builder pack.",
    step1: "Select the imported models you want to build with from the model menu.",
    step2: "Describe what the app should do; the Agent will scaffold the code.",
    step3: "Open \"My Apps\" to run, package, or delete generated apps.",
    chipMyApps: "Open My Apps",
    chipPromote: "Promote to App Builder",
  },
  gomaster: {
    title: "GoMaster — quick reference",
    subtitle: "Cloud-driven one-click ONNX optimization. Upload → optimize → download.",
    step1: "Click \"Start optimize\" and pick your ONNX model file.",
    step2: "The task runs in the cloud; progress streams into the drawer.",
    step3: "Download the optimized model + performance report when finished.",
    chipOptimize: "Start optimize",
  },
  modelBuilder: {
    title: "Model Builder — quick reference",
    subtitle: "Convert a source model into a QNN/SNPE runtime pack and promote it to App Builder.",
    step1: "Upload a source model or point at a workspace directory.",
    step2: "Pick a quantization precision (fp16, w8a8, …).",
    step3: "Use \"Promote to App Builder\" to register the converted pack for use in App Builder mode.",
    chipPromote: "Promote to App Builder",
    emptyExamplesTitle: "Try one of these",
    ex1Label: "Convert an ONNX model to w8a8",
    ex1Prompt: "Convert my ONNX model to a QNN DLC context binary at w8a8 precision, then validate inference on the NPU. Ask me for the model path if you need it.",
    ex2Label: "Quantize & compare accuracy",
    ex2Prompt: "Convert my model to both fp16 and w8a8, run inference on a sample input, and report the cosine similarity between the two so I can judge the accuracy drop.",
    ex3Label: "Export a converted model to App Builder",
    ex3Prompt: "I already have a converted model in my workspace. Package it into an app_pack and promote it to App Builder so I can build an app on top of it.",
  },
  modelHub: {
    title: "Model Hub — quick reference",
    subtitle: "Download pre-compiled models from Qualcomm AI Hub and promote them to App Builder in one click.",
    step1: "Browse or search Qualcomm AI Hub for a pre-compiled model.",
    step2: "Download the model package straight to your workspace.",
    step3: "Use \"Promote to App Builder\" to register it for use in App Builder mode.",
    chipPromote: "Promote to App Builder",
    emptyExamplesTitle: "Try one of these",
    ex1Label: "Download ResNet50 & classify an image",
    ex1Prompt: "Download the pre-compiled ResNet50 image-classification model from Qualcomm AI Hub, then run it on the NPU to classify a test image and print the Top-5 results. Run the whole flow end-to-end.",
    ex2Label: "Download YOLOv8 for object detection",
    ex2Prompt: "Download the pre-compiled YOLOv8 object-detection model from Qualcomm AI Hub and run detection on a sample image on the NPU, drawing the detected boxes.",
    ex3Label: "Download a model & promote to App Builder",
    ex3Prompt: "Download a pre-compiled image-classification model (e.g. Inception v3) from Qualcomm AI Hub, verify inference on the NPU, then package it and promote it to App Builder.",
  },
  pro: {
    title: "Pro mode — quick reference",
    subtitle: "Offload heavy model-conversion pipelines to a remote GPU Agent.",
    step1: "Open Settings to configure the remote GPU-Agent host, account, and port.",
    step2: "Click Connect — the pool auto-picks an idle machine and opens a session.",
    step3: "Describe your conversion job in chat; the Agent takes over end-to-end.",
    chipSettings: "Open settings",
    chipConnect: "Connect GPU Agent",
  },
  code: {
    title: "Code mode — quick reference",
    subtitle: "Let the model read your codebase and help you analyze, modify, or generate code. Works with any OpenAI-compatible API.",
    step1: "Pick a persona to steer the model's focus (reviewer, refactorer, tester, …).",
    step2: "Upload files or paste a Git repo URL so the model has the code context.",
    step3: "Describe the task in chat; the model reads the code and suggests edits or patches.",
    chipPersona: "Pick a persona",
    chipContext: "Upload code / repo",
  },
};

export default modeIntro;
