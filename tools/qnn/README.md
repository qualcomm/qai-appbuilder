# qnn_model_verify.py

This repository contains a small **QAI AppBuilder + QNN** verification script: [qnn_model_verify.py](qnn_model_verify.py). It loads a QNN model (e.g., `.bin`), prints the model I/O metadata, runs a single inference (with either random inputs or user-provided raw tensors), and saves the outputs to `.bin` files. If there is no error, then you can add necessary data preprocess and postprocess, before and after inference this QNN model.


---

## 1) Prerequisites

- Python environment with:
  - `numpy`
  - `qai_appbuilder` available on `PYTHONPATH`
- A QNN runtime environment that can run **HTP** via QAI AppBuilder.

The script configures AppBuilder globally using:

```python
QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
```

By default `qnn_dir` is an empty string in the script (`qnn_dir = ""`). Ensure your environment is set up so QAI AppBuilder can locate the required QNN libraries/runtime (or adjust the script to point `qnn_dir` to your QNN install path if your setup requires it).

---

## 2) How to run

### 2.1 Basic (default FLOAT path)

```bash
python qnn_model_verify.py --model_path <path_to_model.bin>
```

This uses the default AppBuilder I/O data type behavior (the script creates `QNNModel("model", model_path)` without forcing `DataType.NATIVE`). 

### 2.2 Use native I/O data type

```bash
python qnn_model_verify.py --model_path <path_to_model.bin> --io_data_type native
```

When `--io_data_type native` is selected, the script constructs the model with:

```python
input_data_type=DataType.NATIVE
output_data_type=DataType.NATIVE
```


### 2.3 Feed raw input tensors from files

Provide one or more input raw files using `;` as a separator:

```bash
python qnn_model_verify.py \
  --model_path <path_to_model.bin> \
  --raw_paths "in0.raw;in1.raw" \
  --raw_dtype float16
```

- `--raw_paths` is parsed as `;`-separated paths.
- `--raw_dtype` controls the numpy dtype used by `np.fromfile(...)`.


### 2.4 Keep original input tensor shapes (don’t flatten)

By default, inputs are flattened to 1D buffers. Use `--keep_shape` to keep (or reshape back to) the model’s expected input shapes:

```bash
python qnn_model_verify.py --model_path <path_to_model.bin> --keep_shape
```

If `--keep_shape` is used together with `--raw_paths`, the script will **attempt to reshape** each raw input to the corresponding model input shape when the element counts match; otherwise it keeps the raw data as 1D and prints a warning. 

---

## 3) Command-line arguments

[qnn_model_verify.py](qnn_model_verify.py) supports:

- `--model_path` : Path to the model file to run (e.g., `.bin`).
- `--raw_paths`  : `;`-separated list of raw input files. 
- `--io_data_type` : One of `float` or `native`.
  - `float`  → default behavior
  - `native` → force `DataType.NATIVE` for input/output 
- `--raw_dtype` : Numpy dtype for raw inputs (e.g., `float32`, `float16`, `int32`, `int16`, `int8`, `uint8`). If omitted, the script **tries** to infer a dtype from the model’s input datatype. 
- `--keep_shape` : Keep/reshape input tensors to model shapes. Otherwise flatten to 1D. 

---

## 4) What you should expect to see after running

- Printed input/output shapes and datatypes
- One inference execution
- QNN runtime timing logs (e.g., `model_initialize`, `model_inference`, `model_destroy`) and related warnings
- Output `.bin` files written in the current working directory


---

## 5) Quick troubleshooting tips

- **Got dtype/shape mismatches with raw inputs**: Try adding `--keep_shape` and ensure your raw file element count matches the model’s expected input shape (the script prints warnings if it cannot reshape). 
- **Need to control raw dtype**: Explicitly pass `--raw_dtype float16` / `int32` etc. to match your raw generation pipeline. 
- **SSO / corporate environment**: If your QNN/QAI environment depends on corporate paths or wrappers, ensure those are set before running (the script only calls `QNNConfig.Config(...)` with `qnn_dir` set in-code). 

---

## 6) Examples

### 6.1 Default float path

```bash
python qnn_model_verify.py --model_path real_esrgan_x4plus.bin
```

### 6.2 Native I/O + keep shape

```bash
python qnn_model_verify.py --model_path real_esrgan_x4plus.bin --io_data_type native --keep_shape
```
