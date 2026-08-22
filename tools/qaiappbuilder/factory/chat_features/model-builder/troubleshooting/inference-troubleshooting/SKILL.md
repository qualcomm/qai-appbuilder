---
skill_id: inference-troubleshooting
tier: base
triggers: ["QNNContext exit 1", "0xC0000005", "stale artifact", "abnormally low cosine", "Incorrect amount of Input Buffers", "Stub lib id mismatch", "transport 1008", "NCHW", "NHWC"]
sources: ["references/troubleshooting.md", "references/inference.md"]
---

# Inference Troubleshooting (base)

> 🧭 Diagnosis framework: `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/_diagnosis-framework.md`; this SKILL is the symptom library for the "runtime crash / wrong results" domain.

## Responsibility

Diagnose runtime failures and wrong results when running `.bin`/`.dlc`/`.dll` through
`qai_appbuilder` / `QNNContext`: silent load crashes, stale-artifact low cosine, NCHW/NHWC
input mismatch, multi-model buffer collisions, and Linux HTP transport/version mismatch.
Inference on NPU MUST go through `qai_appbuilder`/`QNNContext` — never `onnxruntime`
(CPUExecutionProvider allowed **only** for baseline comparison in a separate process).

## Trigger signals

- `QNNContext(...)` exits on load (exit 1 / `0xC0000005`, no traceback)
- Inference runs but cosine is abnormally low (e.g. 0.83)
- `Incorrect amount of Input Buffers for graphIdx: 0. Expected: N, received: M`
- `Stub lib id mismatch` / `Failed to create transport ... error: 1008` / `Failed to load skel`
- Wrong predictions (e.g. "window screen" instead of "Samoyed") → suspect NCHW/NHWC

## Core knowledge

### WoS ARM64: QNNContext silent crash / abnormally low cosine

| Symptom | Root cause | Action |
|---------|------------|--------|
| `QNNContext(...)` exits on load (exit 1 / 0xC0000005) | ① `QNNConfig.Config()` not called first; ② manually passed SDK `QnnHtp.dll`/`QnnSystem.dll` conflicting with bundled versions | Call `QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)` first (qai_appbuilder 2.47 — **no lib-dir arg**, bundled `libs/` used automatically); do **NOT** pass an SDK path |
| Cosine abnormally low (e.g. 0.83) | **Stale old artifact** (old `.dll`/`.bin` from different ONNX/patch/aux branch) used by mistake | Confirm loaded artifacts are **freshly generated this run** (compare timestamps/sizes); old artifacts are backed up by `qai_workspace_init.py` |

### NCHW vs NHWC — #1 cause of wrong results

| Conversion flag | QNN model input | Required input |
|-----------------|-----------------|----------------|
| `--preserve_io` (default in `qai_convert_fp.py`) | **NCHW** `[1,C,H,W]` | pass NCHW directly |
| No `--preserve_io` | **NHWC** `[1,H,W,C]` | `np.transpose(x,(0,2,3,1))` |

**Always** check `model.getInputShapes()`: `[1,3,H,W]` → NCHW; `[1,H,W,3]` → NHWC. Passing NHWC to NCHW model = completely wrong results. Verify against PyTorch/ONNX CPU baseline; if Top-1 differs, check input format first.

### Multi-model same-process (sticky worker) rules

To avoid `Incorrect amount of Input Buffers` when multiple QNN models run in one process:

1. **`model_name` must be globally unique** — `QNNContext` uses it as internal key; duplicates reuse the first's graph. Use `{model_id}_{stem}` (e.g. `whisper-base_encoder`).
2. **`QNNConfig.Config()` exactly once per process** — repeated calls may corrupt loaded graphs. Guard with module-level flag. Canonical: `QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)`.
3. **`input_data_type`/`output_data_type` is per-context** — `DataType.NATIVE` for best perf (must feed model's native dtype); `DataType.FLOAT` converts internally.

> Full rules + code examples → `references/inference.md § Multi-model same-process (sticky worker) rules`.

### Linux ARM: HTP transport / version mismatch

**Symptoms:** `Stub lib id mismatch: expected(...) detected(...)`, `Failed to create transport ... error: 1008`, `Failed to load skel`, segfault after session creation.
**Cause:** mixed QAIRT/QNN runtime components (version/path mismatch).
**Action:**
1. Single QAIRT root: `export QAIRT_SDK_ROOT=/path/to/qairt/<version>; export QNN_SDK_ROOT="${QNN_SDK_ROOT:-$QAIRT_SDK_ROOT}"`
2. Match SoC + DSP arch: `export PRODUCT_SOC=<id>; export DSP_ARCH=<n>; export ADSP_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"`
3. Loader path: `export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"`
4. Re-source env, rerun: `python qai_runner.py infer_qnn.py`
5. Still failing → verify path precedence of all four env vars.

**Expected after fix:** `stub lib id mismatch` and transport `1008` disappear; non-fatal power-config warnings may remain.

### Correct qai_appbuilder API (QAIRT 2.45 WoS)

| Item | Correct | Wrong |
|------|---------|-------|
| Config call | before `QNNContext(...)` | after / omitted |
| lib-dir arg | not passed (removed in 2.47) | passing `""` or SDK path → `backend library does not exist: Qnn.dll` |
| Config args | `(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)` (enums) | `("", ...)` or ints/strings |
| Context signature | `QNNContext("name", "model.bin")` | `QNNContext(model_path, config)` |
| Model priority | `.bin` > `.dlc` > `.dll` (all work; `.bin` best perf) | assuming only `.bin` |
| Inference API | `model.Inference([inp])` | `model.Execute([inp])` |
| Perf mode | `PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)` / `RelPerfProfileGlobal()` | `perf_profile=` in `Inference()` |
| Cleanup | `del model` | leaving in memory |

### Model file resolution & wrong-output checklist

- `qai_runner.py`: pass `.onnx` path; it searches for QNN model in same dir (`.htp.bin`→`.dll.bin`→`.dll`→`.bin`). Any `*.bin` = context binary. If needed, copy `esrgan.dll.bin` → `esrgan.onnx.dll.bin` to match.
- QNN may reorder I/O — wrapper uses IO-config YAML (`{model}.{runtime}.autogen.yaml`) to remap names/dtypes/layouts. Inspect if outputs are wrong.
- Wrong output steps: (1) check output shape with `infer_generic.py`; (2) verify NHWC vs NCHW; (3) adapt post-processing; (4) quantized model → `--io_data_type native`.

## Non-crash API Error Code Diagnosis Framework

> **Applies to:** non-crash QNN/HTP runtime failures — DSP did not abort/SSR/segfault, but QNN API returned a non-zero error code. Typical root causes: incompatible feature combinations, missing internal state, wrong API call order.
> **Does not apply to:** CDSP crash dump / SSR (use stability troubleshooting); pure accuracy degradation (→ B6/accuracy).

**Core insight:** trace the full API call sequence in `verbose.log` to identify "what state the failing API expected", then trace back to find "which earlier API failed to set up that state". **A late-failing API is usually missing state that an earlier API failed to populate.** Reading only ERROR lines is insufficient — the root cause is usually in the VERBOSE lines that precede the error.

**Five-step framework:**
1. **Extract the error signature:** `grep "\[ ERROR \]\|<E>"` / `grep "err = \|status.*0x"` / `grep "QnnContext_\|QnnGraph_\|QnnMem_"`. Common codes: `QNN_GRAPH_ERROR_INVALID_ARGUMENT`, `QNN_CONTEXT_ERROR_MEM_ALLOC`, `NO_ERROR(0) but wrong output`, `numeric code + "failed…null"`.
2. **Build the feature matrix:** grep `verbose.log` to identify active features (callback-style context / weight-sharing mmap-vs-DMA / multi-context / selective graph loading / multi-threaded deserialization / persistent binary); arrange as a checkbox matrix.
3. **Reconstruct the full API sequence:** `grep "QnnContext_create\|_free\|QnnGraph_retrieve\|_execute\|QnnMem_register\|_deRegister"`. For each resource record: creation method, resource loading, registration/mapping, post-creation operations.
4. **Diff working vs failing path:** if disabling a feature allows a run, diff two `verbose.log` files side by side (`Allocate\|Map\|const pool\|buffer\|register`). Does the working path cache some state? Is this step skipped in the failing path?
5. **Audit caller code:** config lifetime (stack-allocated `QnnContext_Config_t` dangling), callback implementation (offset alignment / page boundary), buffer lifetime (fd/DMA closed too early), whether every API return value is checked.

**Common pitfalls:** confusing synchronous vs callback-style context creation; reading only ERROR lines without tracing VERBOSE; not verifying whether disabling a feature allows a run; stack-allocated config dangling; mixing hex/decimal for offsets.

## Related Blocking Conditions

- **B6** — cosine below threshold after quantization → see `accuracy/quantization-accuracy` skill.
- **B8** — `.dlc` direct load is usually better fallback than `.dll` when `.bin` generation failed (~21-27% slower p50).

## Escalation path

Stop when: silent crash persists after Config-first + fresh-artifact checks; low cosine unexplained by stale artifact or NCHW/NHWC (→ B6); Linux transport errors persist after env alignment; runtime rejects the graph. Never substitute ONNX/CPU inference for a failed QNN/HTP run.

Full API signatures, templates, and IO-config details → `references/inference.md` and `references/troubleshooting.md`.
