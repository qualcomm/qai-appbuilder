# Pack Export & inference_manifest.json Reference

This reference carries the full detail for two things the main SKILL.md only summarizes:

1. The complete `inference_manifest.json` field specification (consumed by `qai_pack_export.py`).
2. The complete Pack Export (Phase 7 · App Builder integration) workflow.

> The main SKILL.md keeps only the MANDATORY "must create this file" requirement, the
> `output.type` rule (it decides which runner template is generated), and a one-line Quick
> Command + pointer here. Everything below is the authoritative detail.

---

## 1. `inference_manifest.json` — Full Specification

After inference runs successfully and produces correct results, the agent MUST create:
`${WORKSPACE}\{MODEL_NAME}\inference_manifest.json`

This file is consumed by `qai_pack_export.py` to generate a fully working App Builder runner.
Without it, the exported runner may have incorrect input dimensions or missing label files.

> ℹ️ It is also the **authoritative source for the import side**: App Builder's scan-bins step reads
> `precision` + `context_binary` from this manifest *first* and trusts them over any filename-based
> guess. That matters when the weight file name carries no precision suffix (e.g.
> `output/{MODEL_NAME}.dlc`) or when the model name differs from the workspace directory name — the
> filename scan cannot classify those, the manifest can. So write it correctly **even if you never
> run the export script**; `context_binary` must be workdir-relative, inside `output/`, end in
> `.bin`/`.dlc`, and point at a file ≥ 1 MiB.

### Required JSON format

```json
{
  "model_name": "{MODEL_NAME}",
  "precision": "{PRECISION}",
  "inference_script": "infer_{MODEL_NAME}.py",
  "context_binary": "output/{MODEL_NAME}_{PRECISION}.bin",
  "vendor": "Original model author/organization (e.g. Google, Ultralytics, Meta). Leave empty string if unknown.",

  "input": {
    "shape": [1, 3, 224, 224],
    "format": "NCHW",
    "dtype": "float32",
    "preprocessing": {
      "resize_method": "shortest_edge_then_center_crop",
      "resize_size": 224,
      "normalize": true,
      "mean": [0.485, 0.456, 0.406],
      "std": [0.229, 0.224, 0.225],
      "scale": 255.0
    }
  },

  "output": {
    "type": "classification",
    "num_classes": 1000,
    "postprocessing": "softmax_topk"
  },

  "assets": [
    {
      "file": "imagenet_classes.txt",
      "type": "labels",
      "description": "ImageNet 1000 class names"
    }
  ],

  "notes": "Any additional information about the model's behavior"
}
```

### Field descriptions

- `vendor`: The original author or organization of the model (e.g., "Google" for Inception, "Ultralytics" for YOLO, "Meta" for Llama). If unknown, use empty string `""`. Do NOT fill "QAIModelBuilder" — that is the export tool, not the model author.
- `input.shape`: Exact input tensor shape as the model expects (NCHW or NHWC)
- `input.format`: "NCHW" or "NHWC"
- `input.preprocessing.resize_method`: How to resize input:
  - `"shortest_edge_then_center_crop"` — for classification
  - `"resize_to_exact"` — for SR, detection, segmentation
  - `"pad_to_square"` — for some detection models
- `input.preprocessing.normalize`: Whether to apply mean/std normalization
- `input.preprocessing.mean/std`: Normalization values (omit if normalize=false)
- `input.preprocessing.scale`: Divisor before normalization (255.0 for uint8 images)
- `output.type`: `"classification"` | `"super_resolution"` | `"detection"` | `"segmentation"` | `"text"` | `"audio"` | `"raw"`
  - **This field is CRITICAL** — it determines which runner template is generated:
    - `"classification"` → softmax + Top-K
    - `"detection"` → YOLO-style NMS + bounding boxes
    - `"super_resolution"` → image upscale with tiling
    - `"segmentation"` → argmax mask + colorize
    - Others → generic passthrough runner
- `output.num_classes`: Number of output classes (classification/detection only)
- `output.postprocessing`: `"softmax_topk"` | `"nms"` | `"argmax_mask"` | `"image_rescale"` | `"ctc_decode"` | `"none"`
- `postprocessing` (top-level, for detection models):
  - `confidence_threshold`: float (default 0.45) — filter detections below this score
  - `nms_iou_threshold`: float (default 0.7) — NMS IoU suppression threshold
  - `max_detections`: int (default 300) — maximum output boxes
- `assets[]`: List of files in the workspace that the runner needs (labels, vocab, etc.)
  - Must be relative to the workspace root (`${WORKSPACE}\{MODEL_NAME}\`)
  - Only include files that are NEEDED for inference postprocessing
  - Common examples: `imagenet_classes.txt` (CV), `coco.names` (detection), `ppocr_keys_v1.txt` (OCR), `tokens.txt` (ASR)

---

## 2. Pack Export (Phase 7 · App Builder Integration)

> After Phase 6 validation passes, optionally export a ready-to-import App Builder Pack candidate.

### When to Use

- `inference_manifest.json` exists in the workspace (generated after Phase 5 inference)
- Phase 6 is Done (cosine meets threshold, `REPORT.md` written with `END_TIME`)
- User wants to bring this model into the App Builder module for demo / benchmarking
- Alternatively, use the "Promote to App Builder" button in the UI (auto-triggers export)

### Quick Command

```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_export.py ^
  --workdir ${WORKSPACE}\{MODEL_NAME} ^
  --model-name {MODEL_NAME} ^
  --precision {PRECISION}
```

> All other parameters (category, display name, input/output kinds) are auto-inferred from
> `inference_manifest.json` and model name. Override with `--category`, `--display-name`, etc. if needed.

### Output

Creates `${WORKSPACE}\{MODEL_NAME}\app_pack\` with:
- `manifest.json` (ModelManifest schema_version=1 with provenance)
- `runner.py` (working runner generated from model category — classify/detect/SR/segment/generic)
- `requirements.txt`, `examples/`, `assets/`, `provenance/`
- `_candidate.json` (`ready: true` when structural checks pass)

### Validation

```bat
<python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_pack_validate.py ^
  ${WORKSPACE}\{MODEL_NAME}\app_pack
```

### Import to App Builder

After validation passes, use the "Promote to App Builder" button in the Model Builder UI, or call `POST /api/appbuilder/import/commit` directly.

### Script Index (Pack Export)

| Subcommand | Purpose | Python env |
|--------|---------|-----------|
| `qai_pack_export.py` | Generate App Builder Pack candidate from validated project | x64 |
| `qai_pack_validate.py` | Validate candidate structural integrity | x64 |
