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
