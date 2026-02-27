# The Wrapper of ONNX Runtime to QNN Runtime (QAI AppBuilder)

## Introduction

This tool provides a transparent runtime replacement that allows existing **ONNX Runtime (ORT)** Python inference scripts to run on **Qualcomm QNN (QAI AppBuilder)** backends **without modifying the original inference code**.

## Core Idea

- Use a hot-patch mechanism to replace `onnxruntime` with a custom wrapper implemented on top of QNN.
- Reuse existing ONNX sample scripts and their input/output logic.
- Automatically locate and load corresponding QNN models.

## Typical Use Cases

- Migrating ONNX Runtime inference to Qualcomm **HTP / CPU** backends.
- Reusing reference ONNX inference scripts for performance evaluation.
- Rapid validation of QNN models generated from ONNX or downloaded from **AI Hub**.

## Usage Overview
There are two usages for this tool:  
**Usage 1:**
1. Copy `onnxexec.py` and `onnxwrapper.py` into your ONNX sample code directory.
2. Run your original ONNX inference script via:

```bash
python onnxexec.py your_onnx_sample.py
```
**Usage 2:**  
Import the wrapper explicitly at the beginning of your script:

```python
from qai_appbuilder import onnxwrapper
```

No other changes to your ONNX inference code are required.

## Technical Implementation

### Overall Architecture

```
+---------------------------+
| Original ONNX Script      |
| (import onnxruntime)      |
+-------------+-------------+
              |
              | hot-patch
              v
+---------------------------+
| onnxwrapper.py            |
| (ORT-compatible wrapper)  |
+-------------+-------------+
              |
              | QNNContext APIs
              v
+---------------------------+
| Qualcomm QNN Runtime      |
| (HTP / CPU)               |
+---------------------------+
```

The implementation consists of two main components:

- **onnxexec.py**: Runtime launcher and module injector. It acts as a lightweight execution wrapper around an existing Python script.
- **onnxwrapper.py**: ONNX Runtime–compatible wrapper backed by QNN.

## onnxexec.py

### Key Responsibilities

- Parses the target script name and arguments.
- Inserts the local directory into `sys.path`.
- Imports `onnxwrapper` and registers it as `onnxruntime` via `sys.modules`.
- Executes the target script using `runpy.run_path`.

### Core Mechanism

```python
import onnxwrapper as _ort
import sys
sys.modules["onnxruntime"] = _ort
```

This ensures that any statement such as:

```python
import onnxruntime as ort
```

will transparently resolve to the QNN-backed implementation.

## onnxwrapper.py

**onnxwrapper.py — ONNX Runtime Compatibility Layer**

`onnxwrapper.py` provides a drop-in subset implementation of ONNX Runtime APIs while internally delegating execution to QNN.

### Key Design Goals

- Maintain compatibility with common ORT inference patterns.
- Support multiple model types (CV, multimodal, video).
- Minimize assumptions about input layout and dtype.
- Provide automatic configuration when explicit configs are absent.

### ORT API Surface Emulation

The wrapper implements the following ORT-like interfaces:

- `InferenceSession`
- `SessionOptions`
- `get_inputs()` / `get_outputs()`
- `run()`
- `get_available_providers()`

This allows most ONNX Runtime examples to run unmodified.

### QNN Model Resolution

When an ONNX model path is provided, the wrapper attempts to locate a matching QNN model automatically.

**Search strategy includes:**

- Runtime-specific variants.
- Platform-specific binaries.
- ONNX-derived naming conventions.

If no QNN model is found, the original path is retained and a warning is logged.

### IO Configuration Handling

#### YAML-Based Configuration (Optional)

The wrapper supports an optional YAML file to describe:

- Input/output tensor names.
- Data types.
- Layout (NCHW / NHWC / NCTHW / NTHWC).
- Optional inputs.
- Batch handling behavior.

YAML can be provided via:

- `QAI_IO_CONFIG` environment variable.
- Model-adjacent `.yaml` files.
- Auto-generated cache files.

#### Automatic IO Config Generation

If no YAML is found, the wrapper:

- Queries QNN model metadata (shapes, dtypes).
- Infers tensor layout heuristically.
- Generates a conservative IO config.
- Optionally saves it to disk for reuse.

This behavior is controlled by:

- `QAI_IO_AUTOGEN_SAVE` (default: enabled)
- `QAI_AUTOGEN_OPTIONAL_HEURISTIC`

### Layout and Data Type Handling

The wrapper performs:

- Automatic dtype conversion (FP32 / FP16 / INT8, etc.).
- Rank normalization (auto add/remove batch dimension).
- Layout conversion for:
  - 4D tensors: NCHW ↔ NHWC
  - 5D video tensors: NCTHW ↔ NTHWC

Conversions are applied only when necessary and logged for traceability.

### Inference Flow

- Validate and collect provided inputs.
- Skip optional inputs if not supplied.
- Apply dtype, rank, and layout normalization.
- Invoke `QNNContext.Inference`.
- Apply output layout conversion if configured.
- Return NumPy arrays consistent with ORT behavior.

### Runtime & Performance Control

Supported runtime controls via environment variables:

- `QAI_QNN_RUNTIME`: `HTP` (default) or `CPU`
- `QAI_QNN_PERF_PROFILE`: Performance profile (e.g. `BURST`)

The wrapper initializes QNN only once per configuration to reduce overhead.

## Summary

This solution enables:

- Zero-change migration from ONNX Runtime to QNN.
- Reuse of existing ONNX inference scripts.
- Automatic model and IO configuration handling.
- Support for advanced layouts and optional inputs.

It is designed for **engineering validation**, **performance bring-up**, and **rapid prototyping** on Qualcomm platforms.
