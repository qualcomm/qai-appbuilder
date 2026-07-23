---
skill_id: conversion-troubleshooting
tier: base
triggers: ["Graph Compose failure", "unable to find graphName", "Wrong number of Parameters 5", "Conv2d failed 3110", "loadRemoteSymbols 4000", "0x80000406", "arm64x", "aarch64", "arch mismatch"]
sources: ["references/context_binary.md", "references/qnn_conversion.md"]
---

# Conversion Troubleshooting (base)

> 🧭 诊断骨架见 [`../_diagnosis-framework.md`](../_diagnosis-framework.md)；本 SKILL 覆盖"转换/编译失败"。

## Responsibility

Diagnose and fix failures in the ONNX → C++/bin → DLL → context-binary (`.bin`) chain on WoS ARM64:
graph-name mismatches, missing VS ARM64 environment, missing HTP runtime files, architecture
mismatches, and 0-byte/`bins/` output traps. Root causes are usually **environment/config** — check those first before touching the graph.

## Trigger signals

- `Graph Compose failure` / `unable to find graphName:<x>` / `MODEL_INVALID_ARGUMENT_ERROR`
- `Wrong number of Parameters 5` / `Op specific validation failed` / `Conv2d failed 3110`
- `No CMAKE_C_COMPILER` / `VCTargetsPath.vcxproj` / `BaseOutputPath not set`
- `loadRemoteSymbols failed with err 4000` / `DspTransport.openSession qnn_open failed, 0x80000406`
- `arm64x` vs `aarch64` DLL load errors

## Core knowledge

### Structured troubleshooting flow

```
1. Windows ARM → context binary MANDATORY.  Linux ARM → optional (.so works), can skip.
2. Read the error: operator name / code (0xc26) / "unsupported" / "validation failed".
3. If it's an operator → hand off to operator-patching skill.
4. If it's env/config → fix per tables below, re-convert.
5. All patterns exhausted → escalate B7 / B8; consider .dlc or CPU/GPU alternative.
```

### #1 cause: graph_names mismatch

`graph_names` in `htp_backend_config_v{73|81}.json` **must exactly match** the DLL graph name (Flow C only; Flow A uses `.dlc` + `--soc_model`). Graph name = **stem of `--output_path`** (`output/my_model.cpp` → `"my_model"`).

```
[ERROR] getQnnGraphConfigFromInfo() unable to find graphName:qnn_model ...
[ERROR] ... got MODEL_INVALID_ARGUMENT_ERROR
Graph Compose failure
```
**Fix:** `--auto-config`, or set `graph_names` = output_path stem.

### `Wrong number of Parameters 5` / `Conv2d failed 3110` — MISSING VS ARM64 ENV

Almost always missing ARM64 build env, not an operator issue. Tools need `vcvarsall.bat arm64`.

- Run inside `.bat` with `call "%_VCVARSALL%" arm64` at top. `cmd /c` does **NOT** inherit env.
- For **DLC→bin**: same error = `QnnHtpV73Stub.dll` or `QnnHtpPrepare.dll` missing from CWD.
- Only patch `.cpp` if error persists **after** correct env.

### HTP runtime files must be in working directory

Generator resolves `.cat`/`Skel.so` relative to **process CWD**. `qai_dev_gen_contextbin.py` handles this automatically.

- **v73:** `QnnHtp.dll`, `libqnnhtpv73.cat`, `libQnnHtpV73Skel.so`
- **v81:** `QnnHtp.dll`, `QnnHtpV81Stub.dll`, `libqnnhtpv81.cat`, `libQnnHtpV81Skel.so`
- **DLC→bin adds:** `QnnModelDlc.dll`, `QnnHtp*Stub.dll`, `QnnHtpPrepare.dll`, `QnnHtpNetRunExtensions.dll`

**Missing →** `loadRemoteSymbols failed with err 4000` / `0x80000406`. (DLC→bin: `4000` alone = non-fatal.)

### `arm64x` ≠ `aarch64`

`lib/arm64x-windows-msvc/` = ARM64EC. Generator **cannot load arm64x DLLs** → always use `lib/aarch64-windows-msvc/`.
v81: `--backend` MUST be `QnnHtp.dll`, NOT `QnnHtpV81Stub.dll`.

### 0-byte / `bins/` trap

Same `--output_dir` for multiple models → `bins/` subdir for first, 0-byte placeholders for rest.
**Fix:** dedicated `--output_dir` per model. Always verify `.bin` size (valid = several MB).

> `qnn-context-binary-generator.exe` returns non-zero even on success and emits `Unknown Key` warnings.
> Check `.bin` exists and is non-empty; don't rely on exit code.

### Common Errors Quick Reference

| Error | Cause | Fix |
|-------|-------|-----|
| `unable to find graphName` | graph_names ≠ DLL graph name | `--auto-config`; or = `--output_path` stem |
| `Graph Compose failure` | config mismatch or unsupported op | check graph_names; check operator support |
| `No CMAKE_C_COMPILER` | VS ARM64 env not initialized | `vcvarsall.bat arm64` in same `.bat` |
| `Unable to load backend: QnnHtp.dll` | DLL not in working dir | copy `QnnHtp.dll` to working dir |
| `Backend version mismatch` | wrong SDK version | same SDK version for all steps |
| `Wrong number of Parameters 5` | missing VS ARM64 env or `.cat`/`Skel.so` | vcvarsall arm64; HTP files in CWD |
| `VCTargetsPath` / `BaseOutputPath not set` | BuildTools not Community | `vc_targets_path` from `qairt_env.json`; VS 2022 **Community** |

### Architecture preflight (mandatory)

Do **not** use `platform.machine()` / `$env:PROCESSOR_ARCHITECTURE` (emulation-affected).
Use `(Get-WmiObject Win32_Processor).Architecture` (12=ARM64, 9=x64) or `dumpbin /headers`.

| OS | Host | Model lib | Action |
|----|------|-----------|--------|
| Linux | x86_64 | aarch64 `.so` | Blocked — run on ARM target |
| Linux | aarch64 | aarch64 `.so` | Allowed |
| Windows | ARM64 | ARM64 `.dll` | Allowed |
| Windows | AMD64 | ARM64 `.dll` | Blocked — run on ARM target |

### Tool path rules (WoS ARM64, QAIRT 2.45+)

**Legacy Flow C (DLL pipeline) toolchain paths** — the DLC pipeline (Flow A, default) uses `qairt-converter` + `qairt-quantizer` from `bin/<host_arch>/` and skips DLL compilation entirely, so this table only matters when the user runs `run_pipeline_legacy.py`:

| Step | Tool | Arch dir |
|------|------|----------|
| ONNX → C++/bin (Flow C) | `qnn-onnx-converter` | `bin/x86_64-windows-msvc/` (Python, x86 emulation) |
| C++/bin → DLL (Flow C) | `qnn-model-lib-generator` | `bin/aarch64-windows-msvc/` (**NOT x86_64** — most common mistake) |
| DLL/DLC → `.bin` (both flows) | `qnn-context-binary-generator.exe` | `bin/aarch64-windows-msvc/` (native ARM64) |

Prefer wrappers (`run_pipeline.py` for DLC path, `run_pipeline_legacy.py` for DLL path, `qai_dev_gen_contextbin.py --auto-config`) — they handle env init, HTP file copy, and arch dirs automatically.

Mismatch → **do not run** the generator locally; instruct user to run on target device.

## HTP 硬约束失败根因表

> 芯片无关约束。**失败 ≠ 不可转**——多数可切分/改写/降规模绕过。

| 症状 | 根因 | 应对 |
|---|---|---|
| `qnn-context-binary-generator failed on HTP` | 图超出 HTP 编译器规模 | 切图移 CPU；降分辨率；排查 int32/5D Gather |
| HTP 拒绝 int32 Gather | HTP 不接受 int32 索引 | 改索引 dtype / one-hot×MatMul / embedding 留 CPU |
| 5D Gather 低效/编译失败 | 5D scalar-index 效率极低 | → **Slice + Squeeze** |
| `unsupported operator` | 不支持的算子 | 等价重写 / 切出 CPU / 调整 opset |
| `qairt-converter error` | 图结构不被接受 | 固定 shape / shape_inference / 切出 |
| `qnn-net-run failed on device` | 运行时执行失败 | 反查 I/O 契约；切图定位；降规模 |
| Export/convert timeout | 图过大/动态循环 | 降尺寸；切子图；固定 shape |

### 转换前 HTP 友好改写规则

每条改写后**必须验证输出 cosine≈1.0**（数学恒等改写），否则回退：

| 改写 | 内容 | 规避的硬约束 |
|---|---|---|
| **5D Gather → Slice + Squeeze** | 5D scalar-index Gather 等价替换 | 直接规避"5D Gather 低效" |
| **Slice(step=-1) → 固定索引 Gather** | 反向切片改为固定索引 Gather | HTP 对固定索引 Gather 更高效 |
| **Slice 常量折叠** | `Shape→Gather→Div` 动态 ends 在输入 shape 固定时折叠为常量 | 消除动态 shape |
| **Where→Add**（attention mask） | `Where(Equal(mask,0),-1e4,s)` → `(s+1e4)*mask-1e4` | 消除 HTP 上昂贵的 Where |

> 纪律：不改写 qairt 内部融合模式；减算子数 ≠ 更易编译；MHA→SHA 拆分开销常超收益；只 patch 部分 MatMul 破坏数值。验收标准="cosine≈1 且编译通过"。

### 自检清单

1. int32-index Gather → HTP 拒，改写或留 CPU
2. 5D scalar-index Gather → Slice+Squeeze
3. 动态 shape → 固定 + 常量折叠
4. 大图/巨型注意力 → 提前切图
5. 检测/分割后处理头 → 切出 CPU
6. 非标准 attention / SSD anchor → 等价重写或切出

> *int32 Gather→改写；5D Gather→拆；动态 shape→固定；大图→切图；后处理→CPU。*

## Blocking Conditions

- **B8** — context binary fails on Windows ARM. Do not degrade to `.dll`; do not retry x86_64 generator on ARM64 DLL. See `sdk-integrity-recovery` for 0-byte self-heal.
- **B5** — target device unavailable → stop, ask user.

## Escalation

Escalate when: failure persists after env fix + patch + retry; no rewrite for required op; runtime rejects graph. Bundle: original + patched ONNX, commands, logs, repro steps.

Full commands + backend config → `references/context_binary.md` and `references/qnn_conversion.md`.
