# Context Binary Generation Reference

Generate hardware-specific HTP context binaries for on-device deployment.
Use absolute paths for all model file arguments.

> 🚦 **Two input types**: `.dlc` (**default**, from Flow A / `run_pipeline.py`) and `.dll` (**legacy**, Flow C, `windows-arm64` only). Both go through `qai_dev_gen_contextbin.py`:
> - `windows-arm64` / `linux-aarch64`: native HTP generator (VS ARM64 + aarch64 HTP runtime files).
> - `windows-x64` / `linux-x64`: script auto-delegates to `qai_dev_gen_contextbin_x86.py`. `.dll` input is rejected (aarch64 DLL can't load in an x64 process); pass `.dlc`. Wrapper backend-flag legacy default → `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` §3.
>
> **DLC path is the default** — no VS ARM64 env, no DLL compilation, cross-arch-safe. Reserve `.dll` input for explicit legacy-Flow-C debugging on WoS.

> ℹ️ `qnn-context-binary-generator.exe` returns non-zero exit code even on success. `qai_dev_gen_contextbin.py` handles this by checking file existence instead — no manual workaround needed, and `run_pipeline.py` inherits the behaviour because it delegates to that script.

## Troubleshooting Flow

If context binary generation fails, follow this structured flow:

```
Step 1: What is `HOST_OS`?
  ├─ `windows-arm64` / `linux-aarch64` → HTP `.bin` MANDATORY → Step 2
  ├─ `windows-x64` / `linux-x64` → QNN HTP context binary (`.bin`); loading rules → `x64-host-notes.md` → Step 2
  └─ (real HTP on x64 hosts is only available by pushing via `adb_runner.py`)

Step 2: What does the error say?
  Look for: operator name, error code (e.g., 0xc26), "unsupported", "validation failed"
  └─→ Match the error in the Error → Action table in operator_patching.md

Step 3: For each failing operator
  3a. Identify the operator name and input types from the error log
  3b. Check input types: TopK output = INT64, Conv output = FLOAT, Constant dtype in Netron
  3c. Follow the Error → Action table in operator_patching.md to patch
  3d. Validate: onnx.checker.check_model(patched.onnx)

Step 4: Re-convert the patched model
   4a. python qai_convert_fp.py --onnx patched.onnx ...
  4b. Re-generate context binary

Step 5: If all patterns exhausted
  → Escalate as Blocking Condition B7
  → Consider: SNPE/DLC flow or CPU/GPU backend alternative
```

---

## ⚠️ CRITICAL: Context Binary Requirements

**Context binary support by `HOST_OS`:**

| `HOST_OS` | `.bin` | Backend | Notes |
|---|---|---|---|
| `windows-arm64` | **Recommended** | HTP | Best perf; HTP-optimized on-device |
| `windows-x64`   | **Supported** | HTP | Loaded by HTP simulator or via ADB — see `x64-host-notes.md` |
| `linux-aarch64` | **Optional** | HTP | `.so` also works directly |
| `linux-x64`     | **Supported** | HTP | Loaded by HTP simulator or via ADB — see `x64-host-notes.md` |
| cross-target (any host) | **Use `.dlc`** | — | `.bin` locked to one HTP version + arch |

> `.bin` vs `.dlc`: same-machine → `.bin`; cross-target → `.dlc`; user picks → obey. Full: `inference.md § Format selection`.

> Context binary output = `<binary_file>.bin`. The older `{model}.dll.bin` naming is qai_runner.py's search order convention — with qai_appbuilder directly, just use the `.bin` file.

> ⚠️ **Batch generation — use separate `--output_dir` per model** to avoid 0-byte placeholders:
> ```bat
> REM ✅ CORRECT
> qnn-context-binary-generator.exe --output_dir "%WORK_DIR%\fp16"  --binary_file model_fp16
> qnn-context-binary-generator.exe --output_dir "%WORK_DIR%\w8a16" --binary_file model_w8a16
>
> REM ❌ WRONG — shared output_dir causes file conflicts (0-byte files, real binary in bins/ subdir)
> qnn-context-binary-generator.exe --output_dir "%WORK_DIR%" --binary_file model_fp16
> qnn-context-binary-generator.exe --output_dir "%WORK_DIR%" --binary_file model_w8a16
> ```
> **Verify**: check `.bin` file size after generation — 0-byte/8-byte means real binary is elsewhere.
>
> ℹ️ **Both layouts import correctly.** App Builder discovers weights by **recursively** enumerating
> `.bin` / `.dlc` under `output\`, so the flat layout (`output\<model>_<prec>.bin`) and the
> per-precision layout (`output\<prec>\<model>_<prec>.bin`) are equally recognised on import.
> 0-byte/stub placeholders (< 1 MiB) are skipped automatically and the real file in the
> subdirectory is selected instead. Separate `--output_dir` therefore remains the recommendation
> (it prevents the placeholders in the first place), not an import-compatibility requirement.

**If generation fails:** Windows → verify VS ARM64 env + HTP runtime files; if still fails → B8 blocker; use `.dlc` as fallback. Linux → skip context binary, proceed with `.so`, log reason.

---

## ⚠️ CRITICAL: `graph_names` Must Match DLL Graph Name

**This is the #1 cause of `Graph Compose failure` on WoS ARM64.**

The `graph_names` field in `htp_backend_config_{version}.json` (e.g. `htp_backend_config_v73.json` or `htp_backend_config_v81.json`, referenced by `backend_extensions.json`) **must exactly match** the graph name embedded in the `.dll`.

**How the graph name is determined:**
- The graph name = **stem of `--output_path`** passed to `qnn-onnx-converter`
- Example: `--output_path output/qnn_model.cpp` → graph name = `"qnn_model"`
- Example: `--output_path output/my_model.cpp` → graph name = `"my_model"`

**Recommendation: Use the model name as the output stem** for clarity:
```bat
qnn-onnx-converter --output_path output\<model>.cpp ...
```
Then set `graph_names` to `["<model>"]` in the config (matching the stem).

**Error when graph_names is wrong:**
```
[ ERROR ] getQnnGraphConfigFromInfo() unable to find graphName:qnn_model in provided graphsConfigInfo object.
[ ERROR ] getQnnGraphConfigFromInfo(...) expected MODEL_NO_ERROR, got MODEL_INVALID_ARGUMENT_ERROR
Graph Compose failure
```

**Fix:** set `graph_names` in `htp_backend_config_v73.json` to the actual graph name (read it with `qairt-dlc-info`; never guess from the file stem).

---

## ⚠️ CRITICAL: HTP Runtime Files Required in Working Directory (WoS ARM64)

Before running `qnn-context-binary-generator.exe` on WoS ARM64, copy the required files to the **working directory**. `qai_dev_gen_contextbin.py` does this automatically (table-driven, any `--htp_version`); the lists below are the manual equivalent for the two most common versions.

### v73 (default)
```bat
copy %QNN_SDK_ROOT%lib\aarch64-windows-msvc\QnnHtp.dll          <working_dir>\
copy %QNN_SDK_ROOT%lib\aarch64-windows-msvc\QnnHtpV73Stub.dll    <working_dir>\
copy %QNN_SDK_ROOT%lib\hexagon-v73\unsigned\libqnnhtpv73.cat     <working_dir>\
copy %QNN_SDK_ROOT%lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so  <working_dir>\
```

### v81
```bat
copy %QNN_SDK_ROOT%lib\aarch64-windows-msvc\QnnHtp.dll           <working_dir>\
copy %QNN_SDK_ROOT%lib\aarch64-windows-msvc\QnnHtpV81Stub.dll    <working_dir>\
copy %QNN_SDK_ROOT%lib\hexagon-v81\unsigned\libqnnhtpv81.cat      <working_dir>\
copy %QNN_SDK_ROOT%lib\hexagon-v81\unsigned\libQnnHtpV81Skel.so   <working_dir>\
```

### Other versions (v68 / v69 / v75 / v79)

Same shape with the version number substituted — **except that only v73 and v81 ship a `.cat`**
(`libqnnhtp<V>.cat`). For v68/v69/v75/v79 there is no catalog file to copy; its absence is normal,
**not** a damaged SDK.

Which versions a host can build for is limited by the **Stub** it ships (`lib/<host_arch>/`):

| Host lib dir | Stubs available |
|---|---|
| `aarch64-oe-linux-gcc11.2` | v68 v69 v73 v75 v79 v81 (also v66, but v66 is DSP-backend, not a valid `--htp_version`) |
| `aarch64-windows-msvc` | v68 v73 v81 only |
| `arm64x-windows-msvc` | v73 v81 |
| `x86_64-linux-clang` | none needed — offline prepare uses `libHtpPrepare.so` |

> ⚠️ **`--backend` must be `QnnHtp.dll`, never `QnnHtpV<N>Stub.dll`** (any version) — the Stub is a
> forwarding layer that cannot be loaded standalone; it is referenced only via
> `backend_extensions.json` → `shared_library_path`. Passing it as `--backend` causes:
> ```
> Unable to load backend. dlerror(): load library failed
> ```

> ⚠️ **`arm64x` ≠ `aarch64`:** `lib/arm64x-windows-msvc/` DLLs are ARM64EC (compatibility layer) and
> CANNOT be loaded by `qnn-context-binary-generator.exe`, a pure ARM64 binary. Always copy from
> `lib/aarch64-windows-msvc/`.

**File purposes:** `QnnHtp.dll` = main backend (use as `--backend`). `QnnHtpV<N>Stub.dll` = CPU-side proxy loaded via `backend_extensions.json` (never pass as `--backend`). `libqnnhtpv*.cat` = DSP transport init. `libQnnHtpV*Skel.so` = DSP session.

> ⚠️ **Generator MUST run with `cwd=<working_dir>`** — resolves `.cat`/`.so` relative to process CWD, not `PATH`. `qai_dev_gen_contextbin.py` handles this automatically.

**Symptoms when files missing:** `loadRemoteSymbols failed with err 4000` / `DspTransport.openSession qnn_open failed, 0x80000406`.

---

## ⚠️ CRITICAL: VS ARM64 Environment Required for Context Binary Generation

`qnn-context-binary-generator.exe` is a native ARM64 executable that requires the VS ARM64 build environment.

**Rule: Always run inside a `.bat` file that calls `vcvarsall.bat arm64` at the top.**

```bat
@echo off
REM Read paths from ${APP_ROOT}\data\config\qairt_env.json (generated by Setup.bat).
REM Do NOT hardcode SDK or VS paths -- they vary per machine.
set _CFG=${APP_ROOT}\data\config\qairt_env.json
for /f "delims=" %%V in ('powershell -NoProfile -Command "(Get-Content %_CFG% | ConvertFrom-Json).vs_vcvarsall"') do set _VCVARSALL=%%V
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-Content %_CFG% | ConvertFrom-Json).vc_targets_path"') do set VCTargetsPath=%%T
for /f "delims=" %%S in ('powershell -NoProfile -Command "$c=Get-Content %_CFG%|ConvertFrom-Json; if($c.qairt_root){$c.qairt_root}else{$c.qairt_sdk_root}"') do set QNN_SDK_ROOT=%%S
for /f "delims=" %%P in ('powershell -NoProfile -Command "(Get-Content %_CFG% | ConvertFrom-Json).python_x64_venv"') do set PYTHON_X64=%%P\Scripts\python.exe

REM Initialize VS ARM64 environment (REQUIRED)
call "%_VCVARSALL%" arm64

set PATH=%QNN_SDK_ROOT%\lib\aarch64-windows-msvc;%QNN_SDK_ROOT%\bin\aarch64-windows-msvc;%PATH%

REM Recommended: use qai_dev_gen_contextbin.py (handles HTP file copy + auto-config)
"%PYTHON_X64%" scripts\qai_dev_gen_contextbin.py ^
    --model C:\absolute\path\to\model.dll ^
    --output_dir output ^
    --binary_file my_model ^
    --auto-config
```

> One `for /f` per field is deliberate: collapsing them into a nested `for %%K in (...)` loop or a
> single multi-line PowerShell call breaks on `for /f` quote escaping (verified — every variable
> comes back empty). Keep the four lines.

**`cmd /c` does NOT inherit `vcvarsall.bat` env — use a `.bat` file.** Symptom: `Wrong number of Parameters 5` / `validateNativeOps ... Conv2d failed 3110` (looks like op issue but = missing ARM64 env).

---

## QNN Context Binary Generation

Use `scripts/qai_dev_gen_contextbin.py` to generate a QNN context binary from a compiled model library (`.so` on Linux / `.dll` on Windows).

### Mandatory preflight — architecture check

Verify native host arch matches model library. Context binary compilation failures (e.g., `Failed to compile layer 'Einsum_123'`) often = unsupported ONNX ops → see [operator_patching.md](operator_patching.md) §Stage 4.

**Linux:**
```bash
uname -m
file /absolute/path/to/libmodel.so
```

**Windows** (don't use `platform.machine()` / `$env:PROCESSOR_ARCHITECTURE` — affected by emulation):
```powershell
# Reliable: WMI Win32_Processor.Architecture
(Get-WmiObject Win32_Processor).Architecture
# 0 = x86, 5 = ARM, 9 = x64 (AMD64), 12 = ARM64

# Or use dumpbin to check DLL architecture:
dumpbin /headers C:\path\to\model.dll | find "machine"
```

| `HOST_OS` | Input `.dll` arch | Input `.dlc` | Action |
|---|---|---|---|
| `windows-arm64` | ARM64 | ✅ | ✅ Native HTP generator. |
| `windows-x64`   | ARM64 | ✅ | ✅ `.dlc` only (auto-delegate; see `x64-host-notes.md`). ARM64 `.dll` rejected. |
| `linux-aarch64` | aarch64 (`.so`) | ✅ | ✅ Native HTP generator. |
| `linux-x64`     | aarch64 (`.so`) | ✅ | `.so` blocked on x64 host (run on ARM device); `.dlc` → x86 host generator (see `x64-host-notes.md`). |

`run_pipeline.py` handles this routing automatically. Only worry about it when calling the generator scripts directly.

---

## Usage

### Linux:
```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_dev_gen_contextbin.py \
  --model /absolute/path/to/libmodel.so \
  --output model_context.bin
```

### Windows (basic):
```powershell
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/qai_dev_gen_contextbin.py \
  --model C:\absolute\path\to\model.dll \
  --output model_context.dll.bin
```

### Windows WoS ARM64 with backend config (QAIRT 2.45 V73):

*Use this for the **basic scenario**: compiling a `.dll` model library to `.bin` (no DLC-specific DLLs needed). For DLC input see [SNPE/DLC Context Binary Generation](#snpedlc-context-binary-generation) below.*
```bat
python ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_dev_gen_contextbin.py ^
  --model C:\absolute\path\to\model.dll ^
  --output_dir output ^
  --binary_file my_model ^
  --auto-config
```

This mirrors the direct CLI call:
```bat
%QAIRT_SDK_ROOT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe ^
  --backend QnnHtp.dll ^
  --model C:\absolute\path\to\model.dll ^
  --output_dir output ^
  --binary_file my_model ^
  --config_file backend_extensions.json
```

---

## Backend Config Files (QAIRT 2.45 WoS)

> ⚠️ **Each model project MUST have its own `backend_extensions.json` and `htp_backend_config_*.json`.**
> Never reuse config files from another project — the `config_file_path` inside `backend_extensions.json`
> is an absolute path that must point to the correct file for the current project.
> Place both files under `${WORKSPACE}\<model_name>\output\`.

### v73 config

**`backend_extensions.json`**
```json
{
  "backend_extensions": {
    "shared_library_path": "<QAIRT_SDK_ROOT>\\lib\\aarch64-windows-msvc\\QnnHtpNetRunExtensions.dll",
    "config_file_path": "${WORKSPACE}\\<model_name>\\output\\htp_backend_config_v73.json"
  }
}
```

**`htp_backend_config_v73.json`**
```json
{
  "graphs": [{"graph_names": ["<model_stem>"], "vtcm_mb": 8, "O": 3}],
  "devices": [{"cores": [{"rpc_control_latency": 100, "perf_profile": "burst"}]}]
}
```

> ℹ️ The `"devices"` section may also include `"htp_arch"` to specify the target HTP architecture
> (e.g. `"htp_arch": "v73"` or `"htp_arch": "v68"`). This is optional on WoS ARM64 where the
> HTP version is auto-detected:
> ```json
> {
>   "graphs": [{"graph_names": ["<model_stem>"], "vtcm_mb": 2}],
>   "devices": [{"htp_arch": "v73"}]
> }
> ```

### v81 config

**`backend_extensions.json`** (v81) — **same as the v73 `backend_extensions.json` above**, except `config_file_path` points to `htp_backend_config_v81.json` (not `..._v73.json`). `shared_library_path` is unchanged (`QnnHtpNetRunExtensions.dll`).

> ⚠️ **v81 `shared_library_path` must be `QnnHtpNetRunExtensions.dll`**, NOT `QnnHtpV81Stub.dll`.
> The extensions loader (`QnnHtpNetRunExtensions.dll`) is the correct value for both v73 and v81.
> Using `QnnHtpV81Stub.dll` here causes `Unable to initialize backend extensions`.

**`htp_backend_config_v81.json`** — **byte-identical to `htp_backend_config_v73.json` above**; only the referencing filename differs (`..._v81.json` vs `..._v73.json`). HTP version is chosen at runtime by the `.cat`/`.so` files in the CWD, not by config contents.

> **Note**: `"Unknown Key"` warnings during generation are **non-fatal**, and the generator returns a
> non-zero exit code even on success (see top-of-file note). Verify the `.bin` instead of the exit
> code.

---

## Options

| Option | Description |
|--------|-------------|
| `--model` | Absolute path to compiled model library (`.so` on Linux, `.dll` on Windows) |
| `--output` | Output path for the context binary (`.bin`) |
| `--output_dir` | Output directory (used with `--binary_file`, mirrors tool's `--output_dir`) |
| `--binary_file` | Output binary name without `.bin` extension |
| `--config_file` | Path to `backend_extensions.json` for HTP configuration (any `--htp_version`; also accepted by the x86 generator). In `run_pipeline.py` this flag is spelled `--config` |
| `--profiling` | Enable HTP optrace profiling (`--profiling_level detailed --profiling_option optrace`) |

---

## x86 Host Support (`windows-x64` / `linux-x64` / `linux-aarch64`)

`qai_dev_gen_contextbin_x86.py` is the host-side context binary generator invoked automatically by `run_pipeline.py` and `qai_dev_gen_contextbin.py` on non-native-HTP hosts. On x64 hosts, backend selection (HTP simulator vs CPU) and the current wrapper-default state are documented in `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` §3. Direct call:

```bash
# DLC input (recommended, cross-arch)
python qai_dev_gen_contextbin_x86.py --dlc /abs/model.dlc --output /abs/model.bin --backend htp

# .so (linux-aarch64 only)
python qai_dev_gen_contextbin_x86.py --model /abs/libmodel.so --output /abs/model.bin --backend htp

# Cross-compile for a non-host SoC by NAME (fills in soc_model 117 / dsp_arch v81)
python qai_dev_gen_contextbin_x86.py --dlc /abs/model.dlc --output /abs/model.bin \
  --target_platform IPQ9650 --vtcm_mb 2 --pd_session unsigned
```

Rules:
- `QAIRT_SDK_ROOT` must be set.
- `--backend`: see `x64-host-notes.md` §3 for x64 backend-selection rules and current wrapper defaults. On `linux-aarch64` `--backend htp` uses the native aarch64 HTP.
- Exactly one of `--model` (compiled lib) or `--dlc`.
- `.dll` input on `windows-x64`: rejected by the parent dispatcher `qai_dev_gen_contextbin.py` (aarch64 `.dll` can't load in an x64 process). Calling `_x86.py --model x.dll` directly on `windows-x64` will still fail at load time; always pass `.dlc` on x64 hosts.
- Omitting `--output` writes `./output/<name>.bin` in the current directory.

### Explicit target selection (cross-SoC offline compile)

> ✅ **An x86-64 Linux host is a first-class conversion host, and the only one that can build for
> *every* ARM64 target.** Having no local HTP is precisely what makes this work: nothing overrides the
> requested SoC (offline prepare via `lib/x86_64-linux-clang/libHtpPrepare.so` — **no `Qnn` prefix**,
> unlike every other HTP lib, so grepping `libQnnHtpPrepare.so` finds nothing). ARM64 hosts can only
> build for their own attached SoC (`socModel: 2147483647`, below). This is the normal way to produce
> `.bin` files for IoT / networking / automotive parts.

**Preferred: name the platform, not the numbers.** `--target_platform <NAME>` fills in
`--soc_model`/`--dsp_arch` from `scripts/_soc_targets.py`; explicit flags still win. Accepted by both
`run_pipeline.py` and `qai_dev_gen_contextbin_x86.py`:

```bash
python run_pipeline.py --model m.onnx --precision w8a8 --calib_list calib.txt \
  --htp_version v81 --target_platform IPQ9650 --vtcm_mb 2
```

Unknown name → exit 2 listing the known platforms. A part with a documented id but **no** documented
arch (e.g. IPQ9574) → exit 2 asking for `--dsp_arch`, never a guess inferred from a sibling part.

`--soc_model` / `--vtcm_mb` / `--dsp_arch` / `--pd_session` / `--opt_level` remain available for a
target that is not in the table. Behaviour:

- Passing **any** target flag auto-generates `htp_config_<dsp_arch>.json` + `backend_extensions.json`
  next to the output, and requires `--backend htp` (with `--backend cpu` → exit 2).
- Passing **none** leaves the command line byte-for-byte as before — no config emitted.
- A **target** flag (`--soc_model`/`--vtcm_mb`/`--dsp_arch`/`--pd_session`) with nowhere to land is a
  **hard error (exit 2)**, not a warning — it used to vanish silently and surface as `err:14` on the
  device. `--opt_level` alone is the one exception: it only rides inside a config's graph entry, so
  with no config and no target flag it is a `[WARNING]` (both scripts behave alike).
- `--config_file` always wins and suppresses auto-generation. **This workflow** rejects it together
  with the target flags (exit 2) — **our design choice, not an SDK limit** (the SDK accepts the
  combination and lets the config win). Flag is `--config` in `run_pipeline.py`.
- Pair `--soc_model` with the matching `--dsp_arch`: a mismatched pair compiles (rc=0) and fails only
  on the device.
- `soc_id` **and** `soc_model` are both written (same value; `soc_id` deprecated but still accepted).
- `graph_names` comes from `qairt-dlc-info`, never the file stem; unreadable names → exit 2 rather
  than a config the backend would reject.
- `perf_profile` is **not** written — `qnn-net-run` consumes it, `qnn-context-binary-generator` does
  not, so it has zero effect on the `.bin`. (The ARM64 script still writes `perf_profile:"burst"`;
  leave it — byte-for-byte legacy compatibility only.)

> ⚠️ `qairt-dlc-info` is an extension-less **Python script** (not an exe), absent from
> `bin/aarch64-windows-msvc`, and needs an interpreter matching the SDK's `libDlModelToolsPy<TAG>`
> native module (QAIRT 2.48 ships **310** and **312** only — not 3.13). Both scripts find one
> automatically; override with `QAIRT_DLC_INFO_PYTHON`.

---

## SNPE/DLC Context Binary Generation

> ✅ **Preferred:** pass `.dlc` to `qai_dev_gen_contextbin.py` — auto-detects DLC, handles extra DLLs, derives `soc_model` from `--htp_version`, skips `--config_file`:
> ```bat
> <python_x64_venv>\Scripts\python.exe ${APP_ROOT}\factory\chat_features\model-builder\scripts\qai_dev_gen_contextbin.py ^
>   --model ${WORKSPACE}\<model>\output_dlc\<model>.dlc ^
>   --output_dir ${WORKSPACE}\<model>\output ^
>   --binary_file <model>_w8a8 ^
>   --htp_version v73 --auto-config
> ```
> The `htp_version -> soc_model` default is the **representative SoC** for that Hexagon version
> (`v73 -> SC8380XP = 60`, `v81 -> SC8480XP = 88`; table in `scripts/_soc_targets.py`), overridable
> with `--soc_model <id>` for a target that is not the host SoC (e.g. `--soc_model 117` for IPQ9650).
> When the target is a *different* part of the same Hexagon version, pass `--soc_model` / `--dsp_arch`
> explicitly rather than relying on the representative.
> **Never** pass `.dlc` to generator's `--model` directly (tries `LoadLibrary` → `load library failed` / `bad image 0xc000012f`).

Manual invocation below (for reference). Use **absolute paths**.

### Windows on Snapdragon (WoS ARM64) — Full Working Command

> ⚠️ **DLC mode requires additional DLL files** beyond the standard `.bin`-from-`.dll` flow — all must be in the CWD.

Copy **all base HTP runtime files** from [§ HTP Runtime Files](#️-critical-htp-runtime-files-required-in-working-directory-wos-arm64) (`QnnHtp.dll`, `QnnHtpV<N>Stub.dll` for your target, `libqnnhtpv<N>.cat`, `libQnnHtpV<N>Skel.so`), **plus** these DLC-only DLLs from `lib\aarch64-windows-msvc\`:

- `QnnModelDlc.dll`, `QnnHtpPrepare.dll`, `QnnHtpNetRunExtensions.dll` — both v73 and v81

All of them must come from `aarch64-windows-msvc`, **NOT** `arm64x-windows-msvc`.

Targets: v73 = Snapdragon X Elite SC8380XP; v81 = Snapdragon X2 Elite SC8480XP.

> ⚠️ **Error diagnosis for DLC→bin on WoS:**
> - `Wrong number of Parameters 5` / `Conv2d failed 3110` → `QnnHtpV<N>Stub.dll` or `QnnHtpPrepare.dll` missing from CWD
> - `PrepareLibLoader Failed loading QnnHtpPrepare.dll` → `QnnHtpPrepare.dll` not in CWD
> - `loadRemoteSymbols failed with err 4000` → non-fatal warning, safe to ignore

```bat
REM %QAIRT_SDK_ROOT% is set by Setup.bat (or read from data\config\qairt_env.json
REM "qairt_sdk_root"). Do NOT hardcode a versioned path here — it goes stale on
REM SDK upgrade.
set QAIRT=%QAIRT_SDK_ROOT%
set OUT=${WORKSPACE}\<model>\output_dlc

REM Copy ALL required runtime files to output directory
copy %QAIRT%\lib\aarch64-windows-msvc\QnnHtp.dll                 %OUT%\
copy %QAIRT%\lib\aarch64-windows-msvc\QnnModelDlc.dll            %OUT%\
copy %QAIRT%\lib\aarch64-windows-msvc\QnnHtpV73Stub.dll          %OUT%\
copy %QAIRT%\lib\aarch64-windows-msvc\QnnHtpPrepare.dll          %OUT%\
copy %QAIRT%\lib\aarch64-windows-msvc\QnnHtpNetRunExtensions.dll %OUT%\
copy %QAIRT%\lib\hexagon-v73\unsigned\libqnnhtpv73.cat            %OUT%\
copy %QAIRT%\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so         %OUT%\

cd /d %OUT%

%QAIRT%\bin\aarch64-windows-msvc\qnn-context-binary-generator.exe ^
  --model QnnModelDlc.dll ^
  --backend QnnHtp.dll ^
  --dlc_path %OUT%\<model>.dlc ^
  --binary_file <model>_fp16 ^
  --output_dir %OUT% ^
  --config_file %OUT%\backend_extensions.json ^
  --soc_model 60
```

> ℹ️ **`--config_file` is OPTIONAL for DLC->bin conversion (verified on QAIRT 2.45 WoS).**
> The minimal working command only needs `--soc_model` -- no `backend_extensions.json` or
> `htp_backend_config_v73.json` required. The generated `.bin` is identical in size and content.
>
> Use `--config_file` only when you need HTP tuning (`vtcm_mb`, `dsp_arch`, `pd_session`) or a
> non-host target SoC. `qai_dev_gen_contextbin.py` generates one automatically **only** when you
> pass `--vtcm_mb` / `--dsp_arch` / `--pd_session`; the plain DLC path is unchanged and still uses
> bare `--soc_model`.
>
> When a config IS used, `graph_names` must be the **real** DLC graph name. Do NOT guess it from the
> file stem — read it with `qairt-dlc-info -i <model>.dlc` (look for `Info of graph: <name>`), which
> is what the scripts do. A wrong or absent name fails with `Valid 'graph_names' must be specified`.

### Linux (aarch64)

> ⚠️ **Three traps the Windows command does not have.** `qai_dev_gen_contextbin.py` handles all three;
> reach for the raw command only when you must.
> 1. **`--backend` and `--model` must be ABSOLUTE paths.** A bare `libQnnHtp.so` exits **9** with
>    nothing but `Initialization failure` — the generator does not resolve them from the CWD. Reported
>    from a device build; the scripts here always pass absolute paths, which is why they never hit it.
> 2. **The device-side Hexagon runtime must be in the CWD** (`libqnnhtpv73.cat`,
>    `libQnnHtpV73Skel.so` for v73). FastRPC resolves the skel through the process CWD and nothing else
>    stages it, so a device-side build otherwise dies in the generator with a `DspTransport` error —
>    the same failure the Windows preflight gate exists to prevent.
> 3. **Drop `--soc_model` on a host with local HTP.** The attached hardware already set the SoC, so the
>    flag buys nothing and often logs `<E> SoC cannot be set more than once`. It is **not** fatal (rc=0,
>    `.bin` written) — see § *soc_model / dsp_arch Reference* below before reading it as a rejection.

```bash
# 1. stage the device-side Hexagon runtime for the target HTP version in the CWD
cp ${QAIRT_SDK_ROOT}/lib/hexagon-v73/unsigned/libqnnhtpv73.cat    <output_dir>/
cp ${QAIRT_SDK_ROOT}/lib/hexagon-v73/unsigned/libQnnHtpV73Skel.so <output_dir>/
cd <output_dir>

# 2. absolute paths for --backend and --model
${QAIRT_SDK_ROOT}/bin/aarch64-oe-linux-gcc11.2/qnn-context-binary-generator \
  --backend ${QAIRT_SDK_ROOT}/lib/aarch64-oe-linux-gcc11.2/libQnnHtp.so \
  --model   ${QAIRT_SDK_ROOT}/lib/aarch64-oe-linux-gcc11.2/libQnnModelDlc.so \
  --dlc_path /absolute/path/to/model.dlc \
  --output_dir <output_dir> \
  --binary_file model
```

With `--output_dir` the binary is `<output_dir>/model.bin`; without it the generator writes to
`./output/`. For another HTP version replace `v73` throughout (`hexagon-v81/unsigned/libqnnhtpv81.cat`,
`libQnnHtpV81Skel.so`). Not every version ships a `.cat` — v68/v69/v75/v79 do not in 2.48.40, and its
absence there is normal rather than a damaged SDK (`scripts/_soc_targets.py`).

### Context binary generation for no-embed `.so`

A model library built without embedded weights cannot be used with
`qnn-context-binary-generator` on a different host. The generator loads the model with
`dlopen` and initializes the graph; when the weights were not embedded into the
`.so`, that initialization requires the separate `.bin` weight file and fails if the
runtime cannot resolve it.

Use this path when `qnn-model-lib-generator` was run without its binary-embedding
argument, for example without `-b model.bin` or `--bin model.bin`.

Required files on the target device:

- ARM target model library, for example `libmodel.so`.
- Matching converter weight file, for example `model.bin`, copied beside the
  model library unless the model code expects another relative path.
- HTP backend library for the target runtime.
- HTP skel/cat files staged in the current working directory as described above.
- Backend config file, when the target flow requires explicit graph/backend settings.

Generate the context binary on the target device:

```bash
# On the ARM target device
cd <output_dir>

${QAIRT_SDK_ROOT}/bin/<target-bin-toolchain>/qnn-context-binary-generator \
  --backend ${QAI_QNN_LIBS_DIR}/libQnnHtp.so \
  --model /absolute/path/to/libmodel.so \
  --binary_file model \
  --output_dir /absolute/path/to/ctx_out \
  --config_file /absolute/path/to/htp_backend.json
```

Example target toolchain folders include `aarch64-oe-linux-gcc11.2` for current ARM Linux
SDKs and `aarch64-ubuntu-gcc9.4` for older Ubuntu-targeted SDK layouts. Use the folder
that exists in the active `QAIRT_SDK_ROOT/bin` and matches the target runtime libraries.

Do not treat this as a host-side context-generation workaround. If the host cannot load the
no-embed model library together with its external weights, move generation to the target device
or rebuild a host-loadable model library with embedded weights.

### soc_model / dsp_arch Reference

`--soc_model` selects the target SoC; `--dsp_arch` the Hexagon architecture. **They must agree** — a
valid id with the wrong arch compiles fine (rc=0) and fails only when the device loads the `.bin`, as
a bare `err:14`.

**The table lives in code, once: `scripts/_soc_targets.py` (`SOC_TARGETS`, 30+ parts).** Read it there
or use `--target_platform <NAME>` instead of copying numbers into a script or into this file — a
duplicated mapping is how `v81 -> SM8750` survived. Rows sourced from a platform owner rather than the
SDK are marked `PROVENANCE_CUSTOMER` and are skipped by the SDK cross-check tests.

**Authoritative SDK sources** (read when a platform is absent from the table; they override this file).
Paths relative to `<qairt_sdk_root>/`; strip HTML tags before grepping
(`re.sub(r"<[^>]*>", " ", raw)` + `html.unescape`) — a long regex over raw HTML gives false negatives:

| Source | What it gives |
|---|---|
| `docs/QAIRT-Docs/QNN/general/overview.html` § *Supported Snapdragon devices* | **THE authoritative table**: device \| toolchains \| SOC Model \| Hexagon Arch. Two tables share that heading — consumer/mobile/IoT, then automotive. |
| `docs/QAIRT-Docs/QNN/general/htp/htp_backend.html` § *QNN HTP TARGET CONFIG TABLE* | `Target Name` \| `dsp_arch` \| `soc_id` side by side (automotive subset). |
| `include/QNN/QnnTypes.h` enum `Qnn_SocModel_t` | Only machine-readable list, but **`@deprecated` and stops at 87** — never treat it as an upper bound (overview.html already lists 88/90/96/105). Sole source for the IPQ ids. |

**Traps worth knowing before you pick a value:**

- ⚠️ **Snapdragon 8 Elite (SM8750) is `v79`, not `v81`** — the most common mix-up. The `v81` parts are
  SM8850 (8 Elite Gen 5), SC8480XP (X2 Elite), SW6100 and SA8797.
- SM8650 = **57**, not 43 (43 = SM8550). SoC ids are **not unique**: 52 covers SA8650/SA8775/SA8255,
  67 covers SA7255/SA8610/SA8620, 21 covers SM8250/QRB5165.
- `v66` parts run on the **legacy `QnnDsp` backend**, not HTP, so `v66` is not a valid
  `--htp_version` (`lib/hexagon-v66/unsigned/` ships `libQnnDspV66Skel.so`, no HTP skel).
- `--soc_model 0` = auto-detect (the backend decides).
- IPQ parts appear in **no** `overview.html` table, so the SDK documents no Hexagon arch for them;
  `_soc_targets.py` leaves those arches empty rather than inferring one from a sibling part number.

> ℹ️ **`--soc_model` has NO effect on a host with local HTP** (`has_local_htp()`: Windows ARM64 /
> aarch64 Linux) — it is documented as a ***simulated*** value (`general/tools.html`), so the attached
> hardware supplies the SoC. Either way the run **exits 0 and writes a `.bin`** whose metadata reads
> `socModel: 2147483647` (measured on QAIRT 2.48.40 for `60`, `88` and `117` alike). Sometimes silently,
> sometimes with **`<E> SoC cannot be set more than once and must be called before any other API
> call`** — which is NOT a flag conflict, just the same one-shot global already set by the hardware.
> **Read either as "wrong host for cross-SoC compilation", never as an id being rejected.**
>
> 💡 Other documented entry points for a non-host cache: `--htp_socs` (comma-separated ASIC ids) with
> `--vtcm_override`, plus `--optimization_level_override` / `--dlbc_override` /
> `--hvx_threads_override`, which the docs note take **precedence over the backend extension config**.

---

## Verification checklist

- [ ] Host arch matches model lib arch (or running on target device)
- [ ] Absolute paths used for all model file arguments
- [ ] Context binary (`.bin`) exists and is **non-zero size** (typically several MB; a 0-/8-byte file means the real binary is in a `bins/` subdir — see the batch-generation warning above)
- [ ] Binary loads correctly on `{TARGET_DEVICE}`
- [ ] `backend_extensions.json` exists, plus the HTP config next to it: `htp_backend_config_<htp_version>.json` (ARM64 script, e.g. `..._v79.json`) or `htp_config_<dsp_arch>.json` (x86 script, e.g. `htp_config_v81.json`)
- [ ] Cross-SoC build only: `qnn-context-binary-utility` reports the **requested** `socModel`/`dspArch`/`vtcmSize` — `socModel: 2147483647` means the values were silently replaced (wrong host, see above)


## See Also

- `scripts/qai_dev_gen_contextbin.py` — Wrapper script that handles both `.so` (Linux) and `.dll` (Windows) model libraries, supports `--config_file` for any `--htp_version`
- `qai_dev_gen_contextbin_x86.py` — Host-arch-aware wrapper for generating QNN context binaries on x86/ARM hosts
- `scripts/model_config.json` — Template with `backend_extensions_template` and `htp_config_v73_template`


---

## VTCM Sweep

Host-generated context binaries always succeed for any `vtcm_mb`; failures only appear at runtime. Sweep and select the maximum passing value:

> 💡 **Large models (weights > 500 MB): don't stop at the first passing value.**
> Higher VTCM pressure means even a config that loads and runs at `vtcm_mb=0` may
> still get faster on real HTP at a higher budget (fewer DDR spills). A lower
> value passing does **not** imply a higher one fails — always finish the full
> sweep (`vtcm_mb=0,1,2,3,4,8`) and pick the **maximum passing** value, not the
> first. Record both the preferred value (if it failed, with its error) and the
> selected fallback (max passing) used for final deployment.
>
> Runtime failure fingerprints for a bad `vtcm_mb`: `Failed to create context
> from binary with err 0x138d` and `Failed to register context to device and
> backend`. Both point at the VTCM budget, not a generic OOM/compile failure —
> don't route these to `references/model-architectures/model_split.md`'s memory-based split
> heuristic; a VTCM sweep on the existing binary is the fix, not re-splitting the graph.

| vtcm_mb | Load | Latency | Error |
|---------|------|---------|-------|
| 0 | | | |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 8 | | | |

Preflight — verify host library architecture before generating:
```bash
file lib{MODEL}.so        # expect: x86-64
dumpbin /headers {MODEL}.dll | findstr machine  # expect: x64
```
