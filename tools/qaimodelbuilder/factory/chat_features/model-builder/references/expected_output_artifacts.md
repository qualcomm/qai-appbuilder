# Expected Output Artifacts

After a successful full pipeline run, the workspace should contain the following
artifacts. Use this as a completion checklist when validating that a Flow has
finished correctly.

## Flow A (DLC→bin, default — `run_pipeline.py`)

| Phase | Artifacts |
|-------|-----------|
| Export | `{MODEL_NAME}.onnx`, `export_onnx.py` |
| Inspect | `{MODEL_NAME}.yaml` |
| Convert FP DLC | `output/{MODEL_NAME}.dlc` (FP16/FP32/bf16, or FP32 intermediate for quantized paths) |
| Quantize (optional) | `output/{MODEL_NAME}_<precision>.dlc` (e.g. `_w8a8`, `_w8a16`; encoding JSON if `--dump_encoding`) |
| Context Binary | `output/{MODEL_NAME}_<precision>.bin` (HTP context, >= 1 MB) |
| Inference | `infer_{MODEL_NAME}.py`, output images/results |
| Validation | `REPORT.md` (cosine, latency, pass/fail) |

## Flow B (SNPE)

| Phase | Artifacts |
|-------|-----------|
| Export | `{MODEL_NAME}.onnx`, `export_onnx.py` |
| Inspect | `{MODEL_NAME}.yaml` |
| Convert | `output/{MODEL_NAME}.dlc` |
| Quantize | `output/{MODEL_NAME}_quantized.dlc` (if INT) |
| Inference | `infer_{MODEL_NAME}.py`, output results |
| Validation | `REPORT.md` (cosine, latency, pass/fail) |

## Flow C (DLL→bin, legacy — `run_pipeline_legacy.py`)

> Only produced when the user explicitly asks for the DLL pipeline or specifically needs the `.dll` artifact.

| Phase | Artifacts |
|-------|-----------|
| Export | `{MODEL_NAME}.onnx`, `export_onnx.py` |
| Inspect | `{MODEL_NAME}.yaml` |
| Convert FP | `output/{MODEL_NAME}.cpp`, `output/{MODEL_NAME}.bin`, `output/{MODEL_NAME}_net.json` |
| Compile DLL | `output/{MODEL_NAME}.dll` (ARM64) |
| Quantize INT | `output/{MODEL_NAME}_<precision>.cpp`, `output/{MODEL_NAME}_<precision>.bin` (if INT) |
| Context Binary | `output/{MODEL_NAME}_<precision>.bin` (HTP context, >= 1 MB) |
| Inference | `infer_{MODEL_NAME}.py`, output images/results |
| Validation | `REPORT.md` (cosine, latency, pass/fail) |

---

## Validation Report — Task-Specific Metrics, Latency & Regression Detail

> `references/core_workflow.md § Step 8` keeps inline the MANDATORY ONNX baseline comparison, the
> Cosine Similarity Summary format (a hard parser contract), the B6 threshold rule, and the
> "must not stop in batch mode" rule. The supplementary detail below (per-task accuracy
> thresholds, latency breakdown, regression test) lives here.

### Task-specific accuracy (choose the metric by model type)

| Model Type | Metric | Acceptable Drop |
|-----------|--------|-----------------|
| Image classification | Top-1 Accuracy | <= 1% vs FP32 baseline |
| Object detection | mAP@0.5 | <= 1% vs FP32 baseline |
| Super-resolution | PSNR / SSIM | <= 0.5dB PSNR / <= 0.01 SSIM |
| Speech recognition | WER (Word Error Rate) | <= 1% absolute increase |
| Translation / generation | BLEU | <= 1 point drop |
| Segmentation | mIoU | <= 1% vs FP32 baseline |

### Latency benchmark (on target device)

- Cold start latency (first inference after model load)
- Warm latency: p50 and p95 percentiles (over >= 20 runs)
- Throughput: inferences/second at batch=1
- Peak memory usage (if measurable)

### Regression test

- Run >= 3 known-good inputs and verify output stability
- All outputs must produce cosine >= threshold vs ONNX baseline

### `REPORT.md` should also include

- Task-specific metric and baseline comparison
- Latency breakdown (cold/p50/p95/throughput)
- Pass/fail verdict
- Top predictions (classification) or sample outputs (other tasks)
