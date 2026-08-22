# Troubleshooting Reference

This file is a **quick index** into the authoritative troubleshooting sub-SKILLs,
plus the small set of unique content not covered by any of them.

## Navigation: problem type → authoritative sub-SKILL

| Problem type | Authoritative sub-SKILL | One-line content |
|------|------|------|
| Conversion failures (dry-run, unsupported ops, Einsum blocker) | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/conversion-troubleshooting/SKILL.md` | Converter errors, first-blocking-op triage, ONNX re-export/retry, escalation bundle |
| Inference runtime failures / wrong results / low cosine / **multi-model same-process** | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/inference-troubleshooting/SKILL.md` | QNNContext load crash & `QNNConfig.Config()` ordering, NCHW/NHWC, stale-artifact / low-cosine, multi-model sticky-worker rules (unique `model_name`, per-context `DataType.NATIVE` for best perf). **Full multi-model rules + copy-paste code (NATIVE setup) → `references/inference.md § Multi-model same-process (sticky worker) rules`.** |
| Operator patching (Einsum & other unsupported ops) | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/operator-patching/SKILL.md` | Patch/rewrite unsupported op paths to primitives, validate patched ONNX, rerun dry-run |
| Environment / HTP transport / version mismatch | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/env-troubleshooting/SKILL.md` | QAIRT/QNN SDK root, SoC/DSP arch, `ADSP_LIBRARY_PATH`/`LD_LIBRARY_PATH`, stub-lib/transport-1008 fixes |
| Export / packaging | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/export-troubleshooting/SKILL.md` | Export path, manifest/output.type, packaging validation |
| SDK integrity / write-protection / damaged SDK file recovery | `${APP_ROOT}/factory/chat_features/model-builder/troubleshooting/sdk-integrity-recovery/SKILL.md` | B9 SDK write-protection discipline, 0-byte/`WinError 193` generator diagnosis, single-file recovery from kept SDK zip (no 2 GB reinstall) |

## Unique content

### Dynamic input errors (SNPE)
- Symptom: `Missing command line inputs for dynamic inputs [...]`
- Action: pass input dims using:
  - wrapper: `--source-model-input-shape <name> <dims>`
  - direct: `--source_model_input_shape <name> <dims>`

### PowerShell Variable Expansion (Windows)

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

> Historical note: earlier `run_pipeline.bat` issues (QAIRT_SDK_ROOT not read, DLL search picking QnnHtp.dll, context binary exit-code false failure) are all fixed in the current version.
