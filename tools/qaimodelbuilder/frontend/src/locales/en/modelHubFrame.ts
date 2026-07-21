// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// modelHubFrame: the Model Hub (former aihub-model-run) toolbar mode frame
// (ModeFrameModelHub.vue). Two controls: (1) pick an AI Hub model to download,
// (2) export the downloaded model to App Builder. Keys mirror the structure
// required by tsc-enforced tri-locale parity (see locales/schema.ts).
// =============================================================================

const modelHubFrame = {
  // 1. AI Hub model picker
  pickModel: "Pick a model",
  pickModelHint: "Search and pick a pre-built model from Qualcomm AI Hub",
  pickModelHeader: "Select an AI Hub model",
  pickModelPlaceholder: "e.g. resnet50, whisper-base, yolov8-det",
  pickModelConfirm: "Confirm",
  pickModelDesc:
    "Enter the AI Hub model name; the agent downloads the pre-compiled package and runs it on-device — no conversion needed.",
  pickModelFilledPrompt:
    "Download the pre-compiled \"{model}\" model from Qualcomm AI Hub, then run it on the NPU against a sample input and show the result. Run the whole flow end-to-end.",
  // 2. Promote to App Builder (unified wording with Model Builder — both
  // modules produce the SAME app_pack, so the action reads identically.)
  export: "Promote to App Builder",
  exportTitle: "Promote this AI Hub model to App Builder as a ready-to-import pack",
  exportHeader: "Promote to App Builder",
  exportDesc:
    "After inference is verified, the agent packages the model (same app_pack contract as Model Builder) so it can be imported into App Builder.",
};

export default modelHubFrame;
