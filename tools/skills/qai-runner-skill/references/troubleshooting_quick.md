# AIPC Troubleshooting Quick Reference

## Common Errors & Solutions

### 1. QNN Model Not Found Error

**Error:**
```
Error initializing QNN Function Pointers: could not load model: model.onnx
```

**Cause:** The `aipc` wrapper cannot find a matching QNN model file for the `.onnx` path.

**Solution:** Copy the context binary to match ONNX naming in the same directory:
```powershell
# Windows
Copy-Item qairt_output\model.dll.bin .\model.onnx.dll.bin
# OR
Copy-Item qairt_output\model.dll.bin .\model.dll.bin

# Linux
cp output/model.so.bin ./model.onnx.so.bin
# OR
cp output/model.so.bin ./model.so.bin
```

**Verification:** Run with the wrapper:
```bash
python aipc inference.py
```

**See also:** `references/inference.md` → Model File Resolution for full search order.

---

### 2. Dynamic Input Shape Errors

**Error:**
```
Missing command line inputs for dynamic inputs ['input'] in the model.
```

**Solution:**
Specify fixed input dimensions using `--input-dims`:
```bash
python aipc_convert_fp.py --onnx model.onnx --output-root output --precision 16 \
  --input-dims input:1,3,64,64
```

To check if your model has dynamic inputs:
```bash
python -c "import onnx; m=onnx.load('model.onnx'); print([d.dim_value for d in m.graph.input[0].type.tensor_type.shape.dim])"
```
If you see `0` values, those axes are dynamic.

---

### 2. Access Denied / Permission Errors

**Error:**
```
[WinError 5] Access is denied: 'output_path'
```

**Solution:**
- Use absolute file path, not directory:
  ```bash
  # Wrong
  --output_path ./output/
  
  # Correct
  --output_path C:/path/to/output/model_name
  ```
- Ensure output directory exists and is writable
- Close any processes that might have the file open

---

### 3. Missing .cpp File Extension

**Error:**
```
Unable to find the model source file, invalid path: model.cpp
is not a cpp model file
```

**Solution:**
The converter may output a file without `.cpp` extension. Check and rename:
```bash
# Check what files were created
dir output\model*

# Rename if needed
ren model model.cpp
```

**Note:** The updated `aipc_convert_fp.py` now auto-fixes this issue.

---

### 4. QAIRT SDK Not Found

**Error:**
```
QAIRT_SDK_ROOT environment variable not set
```

**Solution:**
Activate the QAIRT environment before running scripts:
```powershell
# Windows
.\aienv.ps1

# Linux
source aienv.sh
```

Verify:
```bash
echo $QAIRT_SDK_ROOT  # Linux
echo %QAIRT_SDK_ROOT% # Windows
```

---

### 5. Unsupported Operator Errors

**Error:**
```
Unsupported operator: Einsum
```

**Solution:**
1. Run dry-run first to identify issues:
   ```bash
   qnn-onnx-converter --input_network model.onnx --dry_run
   ```

2. Patch the model before export (in PyTorch):
   ```python
   # Replace problematic ops with QNN-compatible alternatives
   torch.onnx.export(model, ..., opset_version=13)
   ```

3. Use ONNX simplifier:
   ```bash
   python -m onnxsim model.onnx model_simplified.onnx
   ```

---

### 6. HTP Provider Warnings (Non-Blocking)

**Warning:**
```
DSP_INFO UNSUPPORTED_KEY: 49
DSP_INFO UNSUPPORTED_KEY: 50
<E> Error 0x200: failed to close queue
```

**Action:** None - Safe to ignore. These are known warnings during HTP initialization on Windows ARM emulation. Inference will succeed.

---

### 7. Context Binary Generator Argument Errors

**Error:**
```
error: the following arguments are required: --model
```

**Solution:**
Use `--model` or `--model_lib` (both now supported):
```bash
python aipc_dev_gen_contextbin.py --model model.dll --output model.dll.bin
```

---

### 8. Model Library Not Found After Conversion

**Error:**
```
Warning: did not find a .so or .dll under the output directory
```

**Solution:**
Check the output subdirectories:
```bash
# Windows
dir output\x64\*.dll       # x86_64
dir output\ARM64\*.dll     # ARM64

# Linux
find output -name "*.so"
```

---

## Quick Diagnostic Commands

### Check QAIRT Environment
```bash
echo $QAIRT_SDK_ROOT
ls $QAIRT_SDK_ROOT/bin/x86_64-linux-clang/qnn-onnx-converter  # Linux
dir %QAIRT_SDK_ROOT%\bin\x86_64-windows-msvc\qnn-onnx-converter  # Windows
```

### Inspect ONNX Model
```bash
python aipc_inspect_onnxio.py model.onnx
```

### Run Converter Dry-Run
```bash
python %QAIRT_SDK_ROOT%\bin\x86_64-windows-msvc\qnn-onnx-converter \
  --input_network model.onnx --dry_run
```

### Verify Converted Model
```bash
# Check files exist
dir output\model.bin
dir output\model.cpp
dir output\ARM64\model.dll

# Check file sizes (should be non-zero)
```

---

## Escalation Checklist

If issues persist after trying the above:

- [ ] Record exact error message
- [ ] Note which phase failed (export/convert/generate/inference)
- [ ] Save ONNX file snapshot
- [ ] Save converter logs
- [ ] Document QAIRT SDK version: `cat $QAIRT_SDK_ROOT/version.txt`
- [ ] Document host OS and architecture

---

## Reference

- Full troubleshooting guide: `references/troubleshooting.md`
- QNN conversion guide: `references/qnn_conversion.md`
- Model export guide: `references/model_export_validation.md`
