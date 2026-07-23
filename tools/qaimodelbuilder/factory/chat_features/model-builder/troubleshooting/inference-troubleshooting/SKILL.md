---
skill_id: inference-troubleshooting
tier: base
triggers: ["QNNContext exit 1", "0xC0000005", "stale artifact", "异常低 cosine", "Incorrect amount of Input Buffers", "Stub lib id mismatch", "transport 1008", "NCHW", "NHWC"]
sources: ["references/troubleshooting.md", "references/inference.md"]
---

# Inference Troubleshooting (base)

> 🧭 通用诊断骨架见 [`../_diagnosis-framework.md`](../_diagnosis-framework.md)；本 SKILL 是"运行时崩溃/错误结果"领域的症状库。

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

## 非崩溃 API 错误码诊断骨架

> **适用**：QNN/HTP 运行期非崩溃失败——DSP 没有 abort/SSR/segfault，而是 QNN API 返回非零错误码。典型根因：不兼容特性组合、缺失内部状态、API 调用顺序错误。
> **不适用**：有 CDSP 崩溃 dump / SSR（走稳定性排障）；纯精度退化（→ B6/accuracy）。

**核心心法**：在 verbose.log 追踪完整 API 调用序列，定位"失败 API 期望什么状态"，回头找"哪个早 API 没填好该状态"。**晚失败的 API，缺的往往是早 API 没填好的状态。** 只读 ERROR 行不够——根因通常在错误之前的 VERBOSE 行里。

**骨架五步**：
1. **提取错误签名**：`grep "\[ ERROR \]\|<E>"` / `grep "err = \|status.*0x"` / `grep "QnnContext_\|QnnGraph_\|QnnMem_"`。通用码：`QNN_GRAPH_ERROR_INVALID_ARGUMENT`、`QNN_CONTEXT_ERROR_MEM_ALLOC`、`NO_ERROR(0) 但输出错`、`数字码+"failed…null"`。
2. **绘制特性矩阵**：grep verbose.log 判断激活特性（回调式 context / 权重共享 mmap-vs-DMA / 多 context / 选择性图加载 / 多线程反序列化 / 持久化二进制），摆成勾选矩阵。
3. **重建完整 API 时序**：`grep "QnnContext_create\|_free\|QnnGraph_retrieve\|_execute\|QnnMem_register\|_deRegister"`。对每个资源记录：创建方法、资源加载、注册/映射、创建后操作。
4. **正常 vs 异常路径 diff**：关掉某特性能跑则两份 verbose.log 并排 diff（`Allocate\|Map\|const pool\|buffer\|register`）。正常路径有无缓存某状态？异常路径这步是否被跳过？
5. **审查调用方代码**：config 生命周期（栈上 `QnnContext_Config_t` 悬垂）、回调实现（偏移对齐/页边界）、缓冲区生命周期（fd/DMA 过早关闭）、是否检查了每个 API 返回值。

**通用陷阱**：混淆同步式与回调式 context 创建；只读 ERROR 不追 VERBOSE；没验证"关掉某特性后能否跑通"；栈上 config 悬垂；偏移混用十六/十进制。

## Related Blocking Conditions

- **B6** — cosine below threshold after quantization → see `accuracy/quantization-accuracy` skill.
- **B8** — `.dlc` direct load is usually better fallback than `.dll` when `.bin` generation failed (~21-27% slower p50).

## Escalation path

Stop when: silent crash persists after Config-first + fresh-artifact checks; low cosine unexplained by stale artifact or NCHW/NHWC (→ B6); Linux transport errors persist after env alignment; runtime rejects the graph. Never substitute ONNX/CPU inference for a failed QNN/HTP run.

Full API signatures, templates, and IO-config details → `references/inference.md` and `references/troubleshooting.md`.
