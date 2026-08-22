# x64 Host — Local Inference

**Read this only if BOTH:** `HOST_OS` is `windows-x64` / `linux-x64`, AND the user asked to run inference on the current machine. Otherwise skip — x64 default is ADB (Path B, `adb_runner.py`).

`windows-x64` and `linux-x64` behave identically; the SDK ships the same backend libraries for both.

---

## §1 Compatibility matrix

| Model | QnnCpu | QnnHtp simulator | ADB → ARM64 device |
|-------|:------:|:----------------:|:------------------:|
| `.dlc` **fp32** | ✅ if all ops supported | ✅ | ✅ |
| `.dlc` **fp16** | ❌ (no fp16 compute) | ✅ | ✅ |
| `.dlc` **any quantized** (W8A16 / W8A8 / W8A8B8 / W4A16 / W4A8) | ✅ if all ops supported | ✅ | ✅ |
| `.bin` (QNN context binary) | ❌ (HTP-only binary) | ✅ | ✅ |

- QnnCpu ✅ means the precision check passes — model may still fail at graph compose if it uses an op QnnCpu doesn't implement (Transpose is a common one).
- QnnHtp simulator runs the HTP graph on host CPU: functional validation only — numerical equivalence with real HTP is **not guaranteed** (fixed-point rounding / saturation / non-linear approximations may differ), and it is very slow (tens of seconds/inference for complex pipelines).
- ADB is the only path that produces **authoritative accuracy and performance numbers** (real-device execution).

---

## §2 Workflow

1. Determine model format (`.dlc` precision, or `.bin`).
2. Pick a compatible backend from §1 using the `question` tool (in user's language). Do NOT choose silently.
   - **Only one backend viable** (e.g. `.bin` or `.dlc` fp16 → only QnnHtp is compatible locally, plus ADB): present the viable one + ADB.
   - **Both QnnCpu and QnnHtp viable** (`.dlc` fp32 or any quantized): recommend **QnnHtp simulator** as the default local option — all QNN ops supported, so it will not fail at Graph Compose. Offer **QnnCpu** as "faster iteration, but may fail at Graph Compose on ops QnnCpu doesn't implement (e.g. Transpose); only known when you run it." Always include **ADB** as the authoritative option.
   - Mark the recommended option as `(recommended)` in the `question` card.
3. Route:
   - QnnCpu / QnnHtp → **Path C** (§3).
   - ADB → **Path B** (`adb_runner.py`, see `references/adb_execution.md`).
4. **If QnnCpu was chosen and fails at Graph Compose** (`OpConfig validation failed` / unsupported op): report the failing op to the user and offer to retry with QnnHtp simulator. Do NOT silently switch.
5. After any Path C run: emit the closing statement (§4).
6. If the user asks to interpret Path C latency as real-device performance: STOP → **B11**.

---

## §3 Path C execution

- Venv: `python_runtime_venv` (→ `.venv_x64_313`).
- CLI: `qai_runner.py --backend htp|cpu ...`.
- `Runtime.HTP` / `Runtime.CPU` auto-resolve to `x86_64/QnnHtp.dll` / `x86_64/QnnCpu.dll` (Linux: `libQnn*.so`).
- **⚠ Legacy wrapper `--backend` default:** some x64 wrapper scripts (`run_pipeline.py`, `qai_dev_gen_contextbin_x86.py`, `qai_runner.py`) historically default to `--backend cpu` on x64; the correct default should be `htp`. Until fixed, **always pass `--backend htp` explicitly** (or `cpu` when the user selected QnnCpu).

---

## §4 Closing statement (MANDATORY after any Path C run)

Include in the final turn summary AND in `REPORT.md`, in the user's language:

> ⚠️ Ran on the x64 local execution path (backend: HTP functional simulator `x86_64/QnnHtp.dll`, or CPU reference backend `x86_64/QnnCpu.dll` — whichever was selected). This run verifies **pipeline execution and pre/post-processing correctness only**; numerical equivalence with real HTP is **not guaranteed** (the simulator's fixed-point rounding / saturation / non-linear approximations may differ from device silicon), and latency is **not representative of real hardware** (both backends execute on the host CPU). For authoritative accuracy and performance figures, route via ADB to a real ARM64 Snapdragon device.

ARM64 hosts do NOT emit this statement.

---

## §5 B11 — do not report Path C latency as real HTP performance

**Trigger:** Path C run + user asks to treat the numbers as real-device / real HTP performance.
**Action:** STOP. Offer: (a) run on `windows-arm64` / `linux-aarch64` (real HTP), or (b) push via `adb_runner.py` to an ARM64 device.

---

## §6 Common errors

| Signal | Cause | Fix |
|--------|-------|-----|
| `[QNN_CPU] OpConfig validation failed for <op>` / `Graph Compose failure` (Transpose etc.) | Op not implemented in QnnCpu | Switch to QnnHtp or ADB |
| `Context de-serialization failed` loading `.bin` under `Runtime.CPU` | `.bin` is HTP-only | Switch to QnnHtp or ADB |
| `[QNN_CPU]` fp16 / compute-precision errors | QnnCpu has no fp16 compute | Use fp32/quantized `.dlc`, or QnnHtp, or ADB |
| `libcdsprpc.dll cannot be loaded` during offline `.bin` gen | On-device RPC lib, irrelevant offline | Ignore |
| Path C run takes tens of seconds/inference | Expected for QnnHtp simulator | Not an error; apply B11 if real-device numbers needed |
