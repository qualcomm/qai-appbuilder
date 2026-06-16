# Troubleshooting Reference

## Conversion failures
1. Run dry-run first and capture logs.
2. Identify first blocking op/error.
3. Patch model graph/export path.
4. Re-export ONNX and retry conversion.

## Common blocker: unsupported Einsum
- Symptom: converter error with specific `Einsum` equation.
- Action:
  - patch/rewrite unsupported einsum path to primitive ops
  - validate patched ONNX
  - rerun dry-run and conversion
- **Full guide**: See [In-Memory Operator Patching](operator_patching.md) for detailed patching templates and validation steps.

## Dynamic input errors (SNPE)
- Symptom: `Missing command line inputs for dynamic inputs [...]`
- Action: pass input dims using:
  - wrapper: `--source-model-input-shape <name> <dims>`
  - direct: `--source_model_input_shape <name> <dims>`

## Inference runtime validation failures
- Check runtime/backend compatibility for generated DLC/lib.
- Re-check I/O layout, data type, and pre/post-processing consistency.
- Test with minimal input list and known-good sample.

## HTP transport/version mismatch (Linux ARM)

**Symptoms**:
- `Stub lib id mismatch: expected (...), detected (...)`
- `Failed to create transport for device, error: 1008`
- `Failed to load skel` / `Transport layer setup failed`
- Segmentation fault shortly after QNN session creation

**Likely cause**:
- Mixed QAIRT/QNN runtime components are being loaded on target (version/path mismatch across user-space libs and DSP-side libs).

**Action**:
1. Ensure target env uses a single QAIRT SDK root:
   ```bash
   export QAIRT_SDK_ROOT=/path/to/qairt/<version>
   export QNN_SDK_ROOT="${QNN_SDK_ROOT:-$QAIRT_SDK_ROOT}"
   ```
2. Set SoC + DSP arch and DSP library path:
   ```bash
   # Replace with your target values (examples only).
   export PRODUCT_SOC=<your_soc_id>        # e.g., 9075, 8650, ...
   export DSP_ARCH=<your_dsp_arch>         # e.g., 73, 75, ...
   export ADSP_LIBRARY_PATH="$QNN_SDK_ROOT/lib/hexagon-v${DSP_ARCH}/unsigned"
   ```
   If unsure, use the target's known platform config and keep `PRODUCT_SOC` and `DSP_ARCH` matched.
3. Ensure ARM64 runtime libs are on loader path:
   ```bash
   export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
   ```
4. Re-source env and rerun inference via wrapper:
   ```bash
   . /home/ubuntu/aienv.sh
   python aipc infer_qnn.py
   ```
5. If still failing, print and verify path precedence:
   - `echo $QAIRT_SDK_ROOT`
   - `echo $QNN_SDK_ROOT`
   - `echo $ADSP_LIBRARY_PATH`
   - `echo $LD_LIBRARY_PATH`

**Expected result after fix**:
- `stub lib id mismatch` and transport `1008` errors disappear.
- HTP inference proceeds; non-fatal power-config warnings may remain.

## One-command triage bundle (Linux ARM, wrapper mode)

Use this bundle when you see:
- `Failed to load skel`
- `Transport layer setup failed: 14001`
- `Unsupported SoC model`
- `Invalid dsp arch`
- unexpected wrapper artifact selection

### Command template

```bash
# Run on target (or remote target via SSH)
OUT=triage_$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUT"

# 1) wrapper selection + env snapshot
{
  echo "QAI_QNN_RUNTIME=$QAI_QNN_RUNTIME"
  echo "QAI_QNN_LIBS_DIR=$QAI_QNN_LIBS_DIR"
  echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
  echo "ADSP_LIBRARY_PATH=$ADSP_LIBRARY_PATH"
  echo "PRODUCT_SOC=$PRODUCT_SOC"
  echo "DSP_ARCH=$DSP_ARCH"
} > "$OUT/env_snapshot.txt"

# 2) validator baseline
qnn-platform-validator --backend dsp --coreVersion > "$OUT/validator_core.txt" 2>&1 || true
qnn-platform-validator --backend dsp --testBackend > "$OUT/validator_test.txt" 2>&1 || true

# 3) runtime libs actually loaded (critical)
export LD_DEBUG=libs
timeout 15 python -u aipc onnx_inference.py > "$OUT/lddebug.log" 2>&1 || true
grep -E 'libQnnHtp\\.so|libQnnSystem\\.so' "$OUT/lddebug.log" > "$OUT/loaded_qnn_libs.txt" || true

# 4) rpc daemons
journalctl -u adsprpcd -u cdsprpcd -n 300 --no-pager > "$OUT/rpcd_journal.txt" 2>&1 || true

# 5) dmesg (if sudo available)
sudo dmesg -T | egrep -i 'fastrpc|adsprpc|cdsp|rpc|qnn|htp|glink|remoteproc' \
  > "$OUT/dmesg_filtered.txt" 2>&1 || true

echo "Triage bundle saved to: $OUT"
```

### Quick interpretation
- If wrapper-selected artifact is `.onnx` → artifact naming/resolution issue.
- If `libQnnHtp.so` and `libQnnSystem.so` are from different parent directories → mixed runtime stack.
- If validator passes but inference fails → check wrapper artifact + loaded libs before changing model/quantization.

## onnxwrapper auto-selection + QNN libs dir mismatch (Linux ARM, context-binary mode)

**Symptoms**:
- `onnxwrapper` selects the wrong artifact (e.g. stale `*.so.bin` or even `.onnx` interpreted as model lib)
- `Failed to load skel, error: 4000`
- `Transport layer setup failed: 14001`
- `Failed to parse platform config: 14001`
- `Unsupported SoC model ...` / `Invalid dsp arch. Cannot determine stub`
- CPU/HTP both fail when wrapper auto-picks wrong file

**Likely causes**:
1. Stale context binary/model file in working directory is matched first by wrapper naming rules.
2. Wrapper loads an unintended QNN runtime folder (for example `aarch64-ubuntu-gcc9.4`) while deployment expects `aarch64-oe-linux-gcc11.2`.
3. DSP daemon expects `libQnnHtpV73Skel.so.2` but only `libQnnHtpV73Skel.so` exists.

**Action (verified fix sequence)**:
1. Remove/backup stale matched context files near ONNX:
   ```bash
   cd <workdir>
   mkdir -p _ctx_backup
   mv -f <model>.so.bin _ctx_backup/ 2>/dev/null || true
   mv -f <model>.onnx.so.bin _ctx_backup/ 2>/dev/null || true
   mv -f <model>.htp.bin _ctx_backup/ 2>/dev/null || true
   ```
2. Deploy context binary using ONNX-matching name (recommended for wrapper):
   ```bash
   cp <generated_context>.bin <model>.onnx.so.bin
   ```
3. Force wrapper runtime libs dir explicitly:
   ```bash
   export QAI_QNN_LIBS_DIR="$QAIRT_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2"
   export LD_LIBRARY_PATH="$QAI_QNN_LIBS_DIR:$LD_LIBRARY_PATH"
   export ADSP_LIBRARY_PATH="$QAIRT_SDK_ROOT/lib/hexagon-v73/unsigned"
   ```
4. Ensure skel SONAME-compatible link exists:
   ```bash
   cd "$QAIRT_SDK_ROOT/lib/hexagon-v73/unsigned"
   sudo ln -sf libQnnHtpV73Skel.so libQnnHtpV73Skel.so.2
   ```
5. Run inference only through wrapper:
   ```bash
   export QAI_QNN_RUNTIME=HTP
   python aipc onnx_inference.py
   ```

**Validation**:
- Log should show selected model file as `<model>.onnx.so.bin`
- Inference should complete (`inference_ok`) instead of transport/skel errors

## Escalate when
- same failure persists after patch + retry
- converter fails on required op with no feasible rewrite
- runtime rejects graph post-conversion

Escalation bundle:
- ONNX (original + patched)
- conversion command
- dry-run log
- conversion log
- minimal reproduce steps

## PowerShell Variable Expansion (Windows)

**Symptom**: Commands fail with errors like:
- `:PATH is not recognized...`
- `/usr/bin/bash.PSIsContainer is not recognized...`
- Variables silently expanded to wrong values

**Cause**: Bash interprets PowerShell variables (`$_`, `$env:`, `!`) before PowerShell receives them.

**Solutions** (in order of preference):

1. **Use Python instead of shell** (recommended):
   ```python
   import glob
   files = glob.glob("output/**/*.dll", recursive=True)
   ```

2. **Write PowerShell to temp file**:
   ```python
   import tempfile, subprocess, os
   with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False) as f:
       f.write("Get-ChildItem -Recurse | Where-Object {!$_.PSIsContainer}")
       ps1 = f.name
   subprocess.run(["powershell", "-File", ps1])
   os.unlink(ps1)
   ```

3. **Single-quote the command** (fragile, not recommended for complex scripts):
   ```bash
   powershell -Command 'Get-ChildItem | ForEach-Object { $_.FullName }'
   ```

## Subprocess Encoding Errors on Windows (qnn-onnx-converter / qnn-model-lib-generator)

**Symptom**:
```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa5 in position 8: invalid start byte
Exception in thread Thread-3 (_readerthread)
```
or conversion script fails with encoding-related crash.

**Cause**:
QAIRT CLI tools (`qnn-onnx-converter`, `qnn-model-lib-generator`) mix binary data (progress bar control characters, HTP compilation artifacts) into stdout/stderr text streams on Windows. When Python's `subprocess.run()` decodes the output as UTF-8, it encounters invalid byte sequences and throws `UnicodeDecodeError`.

**Solution**:
Always pass `encoding='utf-8', errors='replace'` to `subprocess.run()` when invoking QAIRT tools:

```python
subprocess.run(cmd, check=True, encoding='utf-8', errors='replace')
```

`errors='replace'` substitutes undecodable bytes with `\ufffd`  instead of crashing. The lost binary output is irrelevant — it's only progress animation and internal timing data.

For scripts that use `subprocess.Popen` with reader threads (like `qnn-model-lib-generator` invoked via `qnn-model-lib-generator` Python wrapper), the same issue can occur in the reader thread. The workaround is to set `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` in the environment, or ensure the subprocess stdout is opened in binary mode:

```python
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
for line in proc.stdout:
    print(line.decode('utf-8', errors='replace').rstrip())
```

**References**:
- `skills/aipc-toolkit/scripts/aipc_convert_fp.py` line 217 — existing workaround
- `skills/aipc-toolkit/scripts/aipc_convert_int.py` line 300 — same workaround

---

## Console Print UnicodeEncodeError on Windows ('cp950' / 'cp437')

**Symptom**:
```
UnicodeEncodeError: 'cp950' codec can't encode character '\u2705' in position 88: illegal multibyte sequence
```
or similar `UnicodeEncodeError` when running model export (`export_onnx.py`) or diagnostic scripts in command-line environments.

**Cause**:
Python's standard output `sys.stdout` uses the terminal's active code page (such as CP950 or CP437) by default on Windows. When third-party libraries (e.g. PyTorch's ONNX exporter) print non-ASCII or UTF-8 characters (like status checkmarks `✅`), Python tries to encode them into the terminal's regional encoding, resulting in a crash.

**Solution**:
Reconfigure `sys.stdout` and `sys.stderr` to enforce UTF-8 encoding at the entry point of your script (or within `main()`):

```python
import sys
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
```

---

## Snapdragon NPU System Driver Version Mismatch (Windows)

**Symptoms**:
- Context loading fails: `Can't read future blob. Newest blob version supported: X.X.X. Current blob version: Y.Y.Y. Skel failed to process context binary.`
- JIT compilation fails: `validateNativeOps master op validator ... failed 3110 ... User driver upgrade required to support new ops.`

**Cause**:
The model is compiled with a newer version of the QAIRT SDK (which uses a newer HTP compiler and operator schema version) than the Qualcomm NPU system driver (`qcdsp2.dll` / `skel` driver) currently installed on the target Windows system.

**Solutions / Workarounds**:
1. **Update system NPU driver**:
   - Install the latest Qualcomm chipset/NPU driver and system BIOS from the OEM support portal (e.g. Lenovo, Dell, HP).
   - Alternatively, obtain developers NPU driver packages directly from the Qualcomm Software Center.
2. **Execute via QNN CPU software fallback**:
   - Compile the model with **FP32** precision (`--precision 32`) and `--preserve-io-mode layout` (to avoid inserting unsupported float16 transpose layers on CPU).
   - Copy the compiled C++ library `esrgan.dll` directly to the workspace as `esrgan.dll.bin` (since the wrapper on Windows expects `.dll.bin`).
   - Run the direct QNN CPU software net-run:
     `qnn-net-run.exe --backend QnnCpu.dll --model esrgan.dll.bin --input_list input_list.txt`

---

## snpe-net-run --use_dsp segfault on ARM64 Windows (exit code 3221225477 / 0xC0000005)

**Symptom**:
- snpe-net-run.exe --use_dsp --container model.dlc crashes with exit code **3221225477** (0xC0000005 = STATUS_ACCESS_VIOLATION).
- No error message other than Logging level is : SNPE_LOG_LEVEL_ERROR.
- Running without --use_dsp produces: No backend could validate Op=... Type=Transpose.

**Root cause: Architecture mismatch on ARM64 Windows with x86_64 emulated Python**:
- Python runs under x86_64 emulation: platform.machine() returns AMD64, platform.processor() returns ARMv8 (... Qualcomm).
- snpe-net-run.exe is a **pure AMD64** binary at in/x86_64-windows-msvc/snpe-net-run.exe.
- When --use_dsp is passed, it tries to load QnnHtp.dll / SNPE.dll to access HTP hardware.
- The AMD64 QnnHtp.dll at lib/x86_64-windows-msvc/QnnHtp.dll **cannot access HTP hardware** — x86_64 emulation doesn't forward NPU driver ioctls.
- The ARM64X (CHPE hybrid) QnnHtp.dll at lib/arm64x-windows-msvc/QnnHtp.dll can access HTP but **cannot be loaded by a pure AMD64 binary** — the CHPE loader is only available in the native ARM64 Windows loader, not the x86_64 emulation layer.
- Result: access violation crash.

**Detection**:
`python
import platform
print(platform.machine())    # AMD64 on emulated Python
print(platform.processor())  # ARMv8 (64-bit) ... Qualcomm

# Check PE architecture of snpe-net-run.exe
import struct
with open('snpe-net-run.exe', 'rb') as f:
    f.seek(struct.unpack('<I', f.read(64)[0x3C:0x40])[0])
    f.read(4)
    m = struct.unpack('<H', f.read(2))[0]
# 0x8664 = AMD64, 0xAA64 = ARM64, 0x01C4 = ARM64X(CHPE)
`

**Resolution**: Use the ipc wrapper (via qai_appbuilder / onnxwrapper.py) instead of snpe-net-run.exe:
`powershell
# The wrapper auto-discovers .dlc alongside .onnx and handles ARM64X bridging
python aipc infer_onnx.py -- --model model.onnx --input input.jpg --output output.png
`

The wrapper uses qai_appbuilder Python package which properly loads the ARM64X (CHPE) hybrid runtime DLLs from lib/arm64x-windows-msvc/, bridging x86_64 emulated Python to native HTP hardware.

**Notes**:
- The [QNN] Selected QNN model file (runtime=HTP): ... log confirms the wrapper found the .dlc and routed correctly.
- The benign Error 0x200: failed to close queue at exit can be ignored.
