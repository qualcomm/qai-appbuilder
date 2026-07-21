# QAI ModelBuilder — Agent Workspace Notes

> This file is for LLM Agent context and progress tracking only.
> It is NOT consumed by any code or scripts.
> Technical parameters for App Builder export are in `inference_manifest.json` (generated after inference).

---

## Project Config

```
MODEL_NAME    = <!-- model identifier, e.g. yolov8n, whisper-tiny, inception_v3 -->
FLOW          = <!-- QNN (default) or SNPE -->
PRECISION     = <!-- FP16 (default) / FP32 / INT8 / A16W8 -->
TARGET_DEVICE = <!-- ARM WIN (default) / X86 LINUX / ARM LINUX -->
MODE          = <!-- batch (default) / interactive -->

CALIBRATION_DATA   = <!-- calibration source (required for INT/A16W8 only): image folder / raw folder / list file -->
RETMOE_DEVICE_INFO = <!-- Optional. Path to file with SSH info for remote target execution. Leave empty for local only. -->

HOST_OS            = <!-- auto-detected: windows-arm64 / linux-x64 / linux-aarch64 -->
ADB_DEVICE_ID      = <!-- Optional: ADB device serial, required if multiple devices connected -->
ADB_DEVICE_OS      = <!-- Optional: android (default) / linux — drives target_arch in adb_runner.py -->
ADB_DSP_VERSION    = <!-- Optional: v73 (default) / v75 / v79 / v81 — HTP skel version on target board -->
ADB_TARGET_ARCH    = <!-- Optional: override SDK arch dir, e.g. aarch64-android (non-standard SDK layout only) -->

START_TIME    = <!-- YYYY-MM-DD HH:MM (filled when work begins) -->
END_TIME      = <!-- YYYY-MM-DD HH:MM (filled at Phase 6 completion) -->
```

---

## Progress Summary

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Model Export (ONNX) | ⬜ Not Started |
| 2 | Model Inspection | ⬜ Not Started |
| 3 | Conversion (FP) or Quantization (INT) | ⬜ Not Started |
| 4 | Context Binary Generation (QNN only) | ⬜ Not Started |
| 5 | Inference + `inference_manifest.json` | ⬜ Not Started |
| 6 | Validation & REPORT.md | ⬜ Not Started |
| 7 | App Builder Export (Optional) | ⬜ Not Started |

---

## Operator Patching State

```
PATCH_NEEDED       = <!-- Yes / No — set after Phase 2 inspection -->
PATCH_OPS          = <!-- comma-separated list of unsupported ops found -->
PATCH_ITERATIONS   = <!-- 0 — increment after each patch attempt -->
```

### Iteration Log

| # | Unsupported Op | Approach | Pattern Used | Result | New Ops Found |
|---|----------------|----------|-------------|--------|---------------|
| | | | | | |

> Continue adding rows. No iteration limit. Escalate only on B3/B4/B7.

---

## Issue Log

<!-- Record decisions, errors, and resolutions here during execution -->

| # | Phase | Issue | Resolution | Date |
|---|-------|-------|-----------|------|
| | | | | |
