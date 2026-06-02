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
