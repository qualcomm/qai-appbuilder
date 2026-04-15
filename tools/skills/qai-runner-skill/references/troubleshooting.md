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
