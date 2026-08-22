---
skill_id: export-troubleshooting
tier: base
triggers: ["ModuleNotFoundError functional_tensor", "basicsr", "ReshapeOp::calculateShape", "aux branch", "training-only branch", "No usable NPU weight", "model_builder.missing_context_bin", "precision binary not found"]
sources: ["references/model_export_validation.md", "references/context_binary.md", "references/pack_export.md"]
---

# Export Troubleshooting (base)

> 🧭 Universal diagnosis framework (four phases + three iron laws + reverse tracing + defense-in-depth): `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/_diagnosis-framework.md`; this SKILL is the symptom library for the "ONNX export compatibility" domain.

## Responsibility

Fix failures that occur while exporting a PyTorch model to ONNX (before QNN conversion): the
`basicsr` / `functional_tensor` import error, and conversion failures caused by training-only
branches (auxiliary classifiers) that emit dynamic-shape operators QAIRT 2.45 cannot convert
(`ReshapeOp::calculateShape`). Fixes are in-memory / source-adjacent only — never break reproducibility.
Also covers the **Pack-export / App Builder import** end of the pipeline: a converted model whose
context binary cannot be found (`No usable NPU weight` / `model_builder.missing_context_bin`).

## Trigger signals

| Signal | Phase | Go to |
|---|---|---|
| `ModuleNotFoundError: No module named 'torchvision.transforms.functional_tensor'` (common with `basicsr`-dependent models, e.g. Real-ESRGAN) | ONNX export | § `basicsr` import error |
| `ValueError: modeltools::ops::ReshapeOp::calculateShape: Unable to calculate ReshapeOp output shape ... shape params dont result in same cumulative total sum` with a garbage value (e.g. `89401 != -1104974904`) | ONNX export | § Auxiliary / training-only branches |
| A model has aux / training-only branches | ONNX export | § Auxiliary / training-only branches |
| Error code `model_builder.missing_context_bin`, or a message starting with `No usable NPU weight` | Pack export / App Builder import | § Pack export: no usable NPU weight |
| User reports "conversion finished but App Builder says the precision binary was not found" | Pack export / App Builder import | § Pack export: no usable NPU weight |

## Core knowledge

### `basicsr` → `torchvision.transforms.functional_tensor` import error

`basicsr/data/degradations.py:8` hard-imports `functional_tensor`, removed in torchvision ≥0.16.
Patch that one line to support both paths:
```python
try:
    from torchvision.transforms.functional_tensor import rgb_to_grayscale
except ImportError:
    from torchvision.transforms.functional import rgb_to_grayscale
```

### Auxiliary / training-only branches → `ReshapeOp::calculateShape`

Some models have aux classifier branches used only in training. They often contain ops (`Gather`,
`Reshape` with dynamic shapes) that QAIRT 2.45 `qnn-onnx-converter` cannot handle. The garbage value
(e.g. `-1104974904`) indicates an unresolved dynamic shape.

**Fix — disable training-only branches before export:**
```python
model = SomeModel(weights=SomeModel_Weights.DEFAULT)
if hasattr(model, 'aux_logits'):
    model.aux_logits = False   # disable aux branch flag
if hasattr(model, 'AuxLogits'):
    model.AuxLogits = None     # remove aux classifier module
model.eval()
torch.onnx.export(model, dummy_input, "model.onnx", opset_version=18)
```
> ⚠️ Some constructors enforce `aux_logits=True` when loading pretrained weights — set the attribute
> **after** loading, not via the constructor argument.

**Other training-only branches to check:** always call `model.eval()` before export (disables Dropout, freezes BatchNorm running stats); verify the eval path of any custom `forward()` with `if self.training:` branches.

### Related export hygiene (avoids downstream conversion failures)

- **Always export FP32** (never FP16) — `qnn-onnx-converter` expects FP32; PyTorch CPU has no FP16 `Conv2d`. FP16 is applied later via `--precision 16` in QAIRT.
- **Always `opset_version=18`** for torch 2.x (lower auto-upgrades; downgrade fails on `Resize` etc.). Also `pip install onnxscript`.
- `do_constant_folding=False` — the converter does its own folding; True can add 15-30 min / OOM on large models.
- torch 2.x splits large models into `<model>.onnx` + `<model>.onnx.data` — **both must stay in the same dir**; pass the `.onnx` path (converter finds `.data`). A small `.onnx` is normal — check for the sibling `.onnx.data` before running diagnostics.

### Validation after export (mandatory, especially after patching)

Compare the ONNX output against the original PyTorch output on the same preprocessed input (`onnxruntime` with **`CPUExecutionProvider` only** — allowed as a CPU baseline). Small numerical differences are common; confirm acceptability with the user. For CV tasks also do a task-specific check (annotated-image visual compare + bbox/label/score compare) — patches can introduce off-by-one / axis errors that raw numerics miss.

### Pack export: `No usable NPU weight` (precision binary not found on import)

Raised when Pack export / the App Builder import wizard finds no loadable weight for the model.
This is **almost never a re-conversion problem** — the weight usually exists but sits where the
caller did not look, or carries a name the caller cannot classify.

**Read the diagnostic block first — it is already in the error message.** Fields:

| Field | Meaning |
|---|---|
| `searched:` | `recursively` or `top-level only`, plus the accepted extensions (`.bin`, `.dlc`) |
| `weights present for:` | precisions that *were* resolved (present only on a precision-specific miss) |
| `sub-directories:` | child dirs of the search root — where a per-precision layout hides the real file |
| `weight-like files rejected:` | each `.bin`/`.dlc` that was skipped, **with its byte count and the reason** (`too small (< 1,048,576 bytes — stub/placeholder)` = 0-byte placeholder; `present but not selected (precision mismatch)` = near miss) |
| `other extensions present:` | e.g. `.onnx` / `.dlc` — tells you how far the pipeline actually got |
| `next:` | the single most useful action, already inferred from the evidence above |

If `next:` names a concrete action, do that first; the checks below only add what the block cannot see.

**Self-check — MUST be bounded.** Use `glob` / `list` for `*.bin` / `*.dlc` **only** inside
`${WORKSPACE}\<model_name>\` and its `output\` subtree, **depth ≤ 3**:
```
${WORKSPACE}\<model_name>\output\*.bin
${WORKSPACE}\<model_name>\output\*\*.bin        REM per-precision subdirs, bins/, etc.
${WORKSPACE}\<model_name>\*.bin                 REM stray weight beside the workspace root
```
> 🛑 **IRON LAW — never recursively scan `C:\` or `C:\WoS_AI`.** An unbounded scan of a drive root
> or of the workspace root hangs for 30+ minutes and kills the session (model-hub SKILL Issue 18).
> The workspace is always `C:/WoS_AI/<model>/` — that bounded subtree is the *only* place to look.

**Fix by scenario:**

- **(a) Top-level `.bin` is a 0-byte placeholder, the real weight is in a subdirectory / `bins\`**
  (`weight-like files rejected` shows `too small`). Cause: one shared `--output_dir` across
  precisions. Re-run context-binary generation with a **separate `--output_dir` per precision**
  → `references/context_binary.md § Batch generation`. Deleting the placeholder is not enough if
  the good file is also incomplete — verify each `.bin` is ≥ 1 MiB.
- **(b) Weight file name has no precision suffix, or the model name ≠ the workspace directory name**
  (e.g. workspace `yolov8` holding `yolov8n_fp16.bin`, or an AI Hub `output/yolov8n.dlc`).
  The filename scan cannot classify it — **write / correct `<workdir>\inference_manifest.json`**,
  which the import side treats as the authoritative source. Only two fields are needed to unblock:
  ```json
  { "precision": "fp16", "context_binary": "output/yolov8n_fp16.bin" }
  ```
  Rules the path MUST satisfy: relative to the workdir, **inside `output/`** (a path escaping it is
  ignored), suffix `.bin` or `.dlc`, file size **≥ 1 MiB**. Then tell the user to click Import again
  — no re-conversion, no re-export. Full field spec → `references/pack_export.md § 1`.
- **(c) No weight of any kind** (`no .bin/.dlc files of any size were found`, or only `.onnx` under
  `other extensions present`). The context-binary step never completed. Re-run it:
  ```bat
  python ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_dev_gen_contextbin.py ^
    --model <abs path to model.dlc> --output_dir output\<precision> ^
    --binary_file <model>_<precision> --auto-config
  ```
  On WoS ARM64 this MUST run from a `.bat` that calls `vcvarsall.bat arm64` first
  → `references/context_binary.md`.
- **(d) Only `.dlc`, no `.bin`.** **Not an error — import the `.dlc` directly** (QNNContext loads it,
  and it is cross-target safe). Do **not** regenerate anything; if the wizard did not offer it,
  point `context_binary` at the `.dlc` per (b).

**Recognised precision tokens** (filename suffix or manifest `precision`): `fp16`, `fp32`, `bf16`,
`w8a8` (= `int8`), `w8a16`, `w4a16`, `w4a8` (= `int4`), `w16a16`, plus any custom
`w<N>a<N>[b<N>]` emitted by `run_pipeline.py` for free-form `--act_bw` / `--weight_bw` runs.
> ℹ️ An **unrecognised** suffix is **defaulted to `fp16`, not discarded** — so "weird precision name"
> is never the reason a file is missing. If a file is absent from the picker, it is a *path* or a
> *size* problem (scenarios a–c), never a naming problem.

## Related Blocking Conditions

- **B4** — if disabling a branch or applying an operator patch changes model semantics, stop and ask the user to approve.
- Unsupported operators surfaced during export/conversion → hand off to the `operator-patching` skill (B3/B4/B7 apply there).

## Escalation path

If disabling training-only branches does not resolve `ReshapeOp::calculateShape` and the offending op cannot be patched to supported ops, or the export-vs-original validation fails and the numerical difference is not clearly acceptable → stop and report to the user (B4 / hand off to operator-patching for B7).

Full export performance rules, optimized template, and validation workflow → `references/model_export_validation.md`.
