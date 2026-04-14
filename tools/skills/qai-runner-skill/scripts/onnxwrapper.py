# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
Hot-patch replacement for `onnxruntime` using Qualcomm QAI/QNN or SNPE runtime.
 
⚠️ **NOTICE**: This module supports BOTH QNN and SNPE backends, but some naming
conventions and class names are derived from the QNN implementation.

**Supported Backends:**
- **QNN**: Via `qai_appbuilder.QNNContext` (QNN HTP/CPU/GPU)
- **SNPE**: Via `snpe-net-run` CLI tool (SNPE DSP/CPU/GPU)

⚠️ **NOTE on qai_appbuilder**: The `qai_appbuilder` Python package supports BOTH 
QNN and SNPE runtimes. It provides unified APIs (`QNNContext`, `QNNConfig`, etc.) 
that work with both QNN (.bin/.dll/.so) and SNPE (.dlc) model formats. The naming 
convention uses "QNN" for historical reasons, but the underlying implementation 
supports both runtimes depending on the model format and platform.

**Key capabilities:**
- Run existing scripts unchanged by injecting this module as "onnxruntime".
- Resolve and load QNN model (.bin/.dll.bin/.so) or SNPE DLC from an ONNX path.
- Log which model file is loaded and the model IO specs.
 
**Naming Conventions (QNN-derived but works for both):**
- `QNNConfig`, `QNNContext`, `Runtime`, `PerfProfile` - qai_appbuilder (supports both QNN/SNPE)
- `QnnRunner` - Uses QNN backend via `qnn-net-run`
- `SnpeRunner` - Uses SNPE backend via `snpe-net-run`
- `InferenceSession` - Wraps QNNContext (supports both QNN/SNPE via qai_appbuilder)
- `run_inference()` - QNN-focused helper (use `run_inference_snpe()` for SNPE)
- For SNPE inference via CLI, use `SnpeRunner` class directly

**Usage:**
```python
# For QNN inference (recommended):
from onnxwrapper import run_inference
outputs = run_inference(model_path, input_data, runtime="HTP")

# For SNPE inference (CLI-based):
from onnxwrapper import SnpeRunner, save_tensor_to_raw
runner = SnpeRunner()
results = runner._run(dlc_path, [input_raw], output_dir, backend="cpu")

# For SNPE inference (qai_appbuilder - if supported in your SDK version):
from onnxwrapper import InferenceSession
session = InferenceSession(dlc_path)  # DLC file
outputs = session.run(None, {input_name: input_data})
```

Generalization upgrades:
- Generic for CV + multimodal + video models.
- Per-input config by tensor name (dtype/layout/add_batch/optional).
- Optional inputs can be skipped at runtime (backend supports missing inputs).
- Layout conversion supports 4D (NCHW<->NHWC) and 5D video (NCTHW<->NTHWC; aliases NCDHW/NDHWC).
- Auto-generate an IO config when none is provided, and optionally cache it on disk.
 
Env:
- QAI_QNN_RUNTIME=HTP|CPU
- QAI_QNN_PERF_PROFILE=BURST|OFF
- QAI_QNN_LIBS_DIR=<path>
 
IO config (optional):
- QAI_IO_CONFIG=<path to yaml>
Auto-generation controls:
- QAI_IO_AUTOGEN_SAVE=1|0      (default 1)
- QAI_AUTOGEN_OPTIONAL_HEURISTIC=1|0 (default 0)
 
"""
 
from __future__ import annotations
 
import os
import time
import logging
import platform
import re
import shutil
import subprocess
import glob
import os.path as osp
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Union, Tuple
 
import numpy as np
import yaml
 
try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from typing import Mapping  # type: ignore
 
import qai_appbuilder
from qai_appbuilder import QNNConfig, QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile

# Setup logging with console handler
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Add console handler if not already present
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(console_handler)
    
    # Also configure root logger to show QNN backend logs
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.ERROR)
    if not root_logger.handlers:
        root_handler = logging.StreamHandler()
        root_handler.setLevel(logging.ERROR)
        root_handler.setFormatter(logging.Formatter(
            '%(message)s'
        ))
        root_logger.addHandler(root_handler)

# Module-level default severity (mirrors onnxruntime default of 3=ERROR)
# 0=VERBOSE, 1=INFO, 2=WARNING, 3=ERROR, 4=FATAL
_default_logger_severity: int = 3

# ORT severity -> Python logging level
_ORT_SEVERITY_TO_PY_LEVEL = {
    0: logging.DEBUG,
    1: logging.INFO,
    2: logging.WARNING,
    3: logging.ERROR,
    4: logging.CRITICAL,
}


def set_default_logger_severity(severity: int) -> None:
    """Set the default logging severity for this module (onnxruntime API shim).

    Mirrors ``onnxruntime.set_default_logger_severity(severity)``.

    Args:
        severity: Integer severity level.
            0 = VERBOSE (DEBUG), 1 = INFO, 2 = WARNING, 3 = ERROR, 4 = FATAL.
            The default is 3 (ERROR), which suppresses INFO/WARNING messages.
    """
    global _default_logger_severity
    _default_logger_severity = int(severity)

    py_level = _ORT_SEVERITY_TO_PY_LEVEL.get(_default_logger_severity, logging.ERROR)

    # Update module logger and all its handlers
    logger.setLevel(py_level)
    for handler in logger.handlers:
        handler.setLevel(py_level)

    # Update root logger and all its handlers so QNN backend messages follow the same level
    root_logger = logging.getLogger()
    root_logger.setLevel(py_level)
    for handler in root_logger.handlers:
        handler.setLevel(py_level)


# -------------------- ORT API shims --------------------
@dataclass(frozen=True)
class TensorInfo(Mapping[str, Any]):
    name: str
    shape: Optional[List[int]] = None
    type: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        if key in ("name", "shape", "type"):
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "name"
        yield "shape"
        yield "type"

    def __len__(self) -> int:
        return 3


NodeArg = TensorInfo


# -------------------- small helpers --------------------
def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def _env_bool(name: str, default: str = "0") -> bool:
    v = _env(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _default_qai_libs_dir() -> str:
    return os.path.join(os.path.dirname(qai_appbuilder.__file__), "libs")


def _ensure_path_contains(dir_path: str) -> None:
    if not dir_path:
        return
    cur = os.environ.get("PATH", "")
    if dir_path.lower() not in cur.lower():
        os.environ["PATH"] = dir_path + ";" + cur


def _normalize_layout(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip().upper()
    alias = {
        "BHWC": "NHWC",
        "BCHW": "NCHW",
        "NCDHW": "NCTHW",
        "NDHWC": "NTHWC",
        "BCTHW": "NCTHW",
        "BTCHW": "NTHWC",
    }
    return alias.get(s, s)


def _convert_layout(x: np.ndarray, src: Optional[str], dst: Optional[str]) -> np.ndarray:
    if x is None:
        return x
    src = _normalize_layout(src)
    dst = _normalize_layout(dst)
    if not src or not dst or src == dst:
        return x

    arr = np.asarray(x)

    # 4D: NCHW <-> NHWC
    if arr.ndim == 4:
        if src == "NCHW" and dst == "NHWC":
            return np.transpose(arr, (0, 2, 3, 1)).copy()
        if src == "NHWC" and dst == "NCHW":
            return np.transpose(arr, (0, 3, 1, 2)).copy()
        return x

    # 5D: NCTHW <-> NTHWC
    if arr.ndim == 5:
        if src == "NCTHW" and dst == "NTHWC":
            return np.transpose(arr, (0, 2, 3, 4, 1)).copy()
        if src == "NTHWC" and dst == "NCTHW":
            return np.transpose(arr, (0, 4, 1, 2, 3)).copy()
        return x

    return x


def _as_list_of_ints(v: Any) -> Optional[List[int]]:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        out: List[int] = []
        for e in v:
            try:
                out.append(int(e))
            except Exception:
                return None
        return out
    return None


def _ort_type_from_dtype(dtype: Optional[str]) -> str:
    if not dtype:
        return "tensor(float)"
    s = str(dtype).strip().lower()
    m = {
        "float32": "tensor(float)",
        "fp32": "tensor(float)",
        "float": "tensor(float)",
        "float16": "tensor(float16)",
        "fp16": "tensor(float16)",
        "half": "tensor(float16)",
        "uint8": "tensor(uint8)",
        "u8": "tensor(uint8)",
        "int8": "tensor(int8)",
        "i8": "tensor(int8)",
        "uint16": "tensor(uint16)",
        "u16": "tensor(uint16)",
        "int16": "tensor(int16)",
        "i16": "tensor(int16)",
        "int32": "tensor(int32)",
        "i32": "tensor(int32)",
        "int64": "tensor(int64)",
        "i64": "tensor(int64)",
        "bool": "tensor(bool)",
        "boolean": "tensor(bool)",
    }
    return m.get(s, "tensor(float)")


def _np_dtype_from_str(dtype: Optional[str]) -> np.dtype:
    if not dtype:
        return np.float32
    s = str(dtype).strip().lower()
    m = {
        "float32": np.float32,
        "fp32": np.float32,
        "float": np.float32,
        "float16": np.float16,
        "fp16": np.float16,
        "half": np.float16,
        "uint8": np.uint8,
        "u8": np.uint8,
        "int8": np.int8,
        "i8": np.int8,
        "uint16": np.uint16,
        "u16": np.uint16,
        "int16": np.int16,
        "i16": np.int16,
        "int32": np.int32,
        "i32": np.int32,
        "int64": np.int64,
        "i64": np.int64,
        "bool": np.bool_,
        "boolean": np.bool_,
    }
    return m.get(s, np.float32)


def get_np_dtype(dtype_str: str) -> np.dtype:
    """Convert dtype string to numpy dtype."""
    return _np_dtype_from_str(dtype_str)


def _dtype_str_from_any(dt: Any) -> str:
    s = str(dt).strip().lower()
    if "float16" in s or "fp16" in s or s.endswith("16"):
        return "float16"
    if "float32" in s or "fp32" in s or "float" in s:
        return "float32"
    if "int64" in s or "i64" in s:
        return "int64"
    if "int32" in s or "i32" in s:
        return "int32"
    if "int8" in s or "i8" in s:
        return "int8"
    if "uint8" in s or "u8" in s:
        return "uint8"
    if "bool" in s:
        return "bool"
    return "float32"


def _tensor_brief(a: Any) -> str:
    if a is None:
        return "None"
    arr = np.asarray(a)
    return f"shape={arr.shape}, dtype={arr.dtype}"


def _is_normalized_01(arr: np.ndarray) -> bool:
    """Heuristic: float tensor values look like normalized image in [0,1]."""
    try:
        a = np.asarray(arr)
        if a.dtype.kind != 'f':
            return False
        mx = float(np.nanmax(a))
        mn = float(np.nanmin(a))
        # allow tiny negatives from preprocessing
        return mn >= -1e-3 and mx <= 1.5
    except Exception:
        return False


def _shape_eq(shape, ref) -> bool:
    try:
        if shape is None:
            return False
        if not isinstance(shape, (list, tuple)):
            return False
        if len(shape) != len(ref):
            return False
        for a, b in zip(shape, ref):
            try:
                if int(a) != int(b):
                    return False
            except Exception:
                return False
        return True
    except Exception:
        return False


def _model_looks_like_param_regression_265(out_shapes: Optional[List[Any]]) -> bool:
    """Heuristic: model has an output with last dim == 265 (e.g., 3DMM parameters)."""
    try:
        if not out_shapes:
            return False
        for s in out_shapes:
            if isinstance(s, (list, tuple)) and len(s) >= 1:
                try:
                    if int(s[-1]) == 265:
                        return True
                except Exception:
                    continue
        return False
    except Exception:
        return False


def _auto_scale_01_to_255_needed(item: dict, exp_in_shape: Any, exp_out_shapes: Optional[List[Any]], arr: np.ndarray) -> bool:
    """Automatic, conservative scaling for QNN compiled models.

    Goal: when a script feeds normalized [0,1] float image tensor but the compiled
    QNN model expects pixel-domain [0,255], scale by 255.

    We avoid model-name checks. Instead we only trigger when:
      - No explicit scale/offset is configured in IO config for this input, AND
      - The input looks like normalized [0,1], AND
      - The model's outputs look like a 265-dim parameter regression (rare; minimizes impact).

    This keeps the behavior automatic for the 3DMM-style models while not affecting
    typical classifiers/detectors.
    """
    if item and (('scale' in item) or ('offset' in item)):
        return False
    if not _is_normalized_01(arr):
        return False
    if not _model_looks_like_param_regression_265(exp_out_shapes):
        return False

    # If expected input shape is known, require the common image shape (1,3,128,128)
    # or (1,128,128,3). Otherwise, fall back to current tensor shape.
    if exp_in_shape is not None:
        if _shape_eq(exp_in_shape, (1, 3, 128, 128)) or _shape_eq(exp_in_shape, (1, 128, 128, 3)):
            return True
        return False

    a = np.asarray(arr)
    if a.ndim == 4 and (a.shape == (1, 3, 128, 128) or a.shape == (1, 128, 128, 3)):
        return True
    return False


def _ensure_rank(arr: np.ndarray, expected_rank: Optional[int], add_batch: bool = True) -> np.ndarray:
    if expected_rank is None:
        return arr
    if add_batch and arr.ndim == expected_rank - 1:
        return np.ascontiguousarray(np.expand_dims(arr, 0))
    if arr.ndim == expected_rank + 1 and arr.shape[0] == 1:
        return np.ascontiguousarray(arr[0])
    return arr


def _infer_layout_from_shape(shape: Any) -> Optional[str]:
    if not isinstance(shape, (list, tuple)):
        return None
    s = list(shape)

    def _is_c(v):
        try:
            iv = int(v)
            return iv in (1, 3, 4)
        except Exception:
            return False

    if len(s) == 4:
        if _is_c(s[1]) and not _is_c(s[3]):
            return "NCHW"
        if _is_c(s[3]) and not _is_c(s[1]):
            return "NHWC"
        return None

    if len(s) == 5:
        if _is_c(s[1]) and not _is_c(s[4]):
            return "NCTHW"
        if _is_c(s[4]) and not _is_c(s[1]):
            return "NTHWC"
        return None

    return None

# -------------------- Qairt profile Wrapper --------------------

def save_tensor_to_raw(tensor, filepath):
    """Save a NumPy array to a raw binary file."""
    tensor = np.ascontiguousarray(tensor, dtype=np.float32)
    tensor.tofile(filepath)
    logger.debug("Saved tensor shape %s to %s", tensor.shape, filepath)


def load_tensor_from_raw(filepath, shape, dtype=np.float32):
    """Load a raw binary file into a NumPy array."""
    tensor = np.fromfile(filepath, dtype=dtype).reshape(shape)
    logger.debug("Loaded tensor shape %s from %s", tensor.shape, filepath)
    return tensor


def delete_directory(path):
    """Safely delete a directory if it exists."""
    target = Path(path)
    if target.exists() and target.is_dir():
        shutil.rmtree(target)
        logger.debug("Deleted directory: %s", path)


def parse_dlc_info(file_path):
    """
    Parse DLC info text and extract graph/input/output metadata.

    Returns:
        dict | None: Parsed metadata, or None when the file is missing.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        logger.error("DLC info file not found: %s", file_path)
        return None

    info_dict = {"graph_name": None, "inputs": [], "outputs": []}

    graph_name_match = re.search(r"Info of graph: (.*)", content)
    if graph_name_match:
        info_dict["graph_name"] = graph_name_match.group(1).strip()

    input_table_regex = re.compile(
        r"\| Input Name\s+\| Dimensions\s+\| Type\s+\| Encoding Info\s+\|\n-+\n"
        r"((?:\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|\n)+)"
    )
    output_table_regex = re.compile(
        r"\| Output Name\s+\| Dimensions\s+\| Type\s+\| Encoding Info\s+\|\n-+\n"
        r"((?:\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|\n)+)"
    )

    def _parse_table(match, key):
        if not match:
            return

        rows = match.group(1).strip().splitlines()
        for row in rows:
            parts = [part.strip() for part in row.split("|") if part.strip()]
            if len(parts) < 3:
                continue

            dims = tuple(int(part.strip()) for part in parts[1].split(","))
            info_dict[key].append(
                {
                    "name": parts[0],
                    "shape": dims,
                    "dtype": get_np_dtype(parts[2]),
                }
            )

    _parse_table(input_table_regex.search(content), "inputs")
    _parse_table(output_table_regex.search(content), "outputs")

    return info_dict



class BaseRunner:
    def __init__(self, sdk_env_var, sdk_name):
        self.sdk_root = os.environ.get(sdk_env_var)
        if not self.sdk_root or not os.path.exists(self.sdk_root):
            raise RuntimeError(f"{sdk_env_var} is not set or path does not exist: {self.sdk_root}")

        self.is_windows = os.name == "nt"
        self.arch = self._get_actual_architecture()
        
        self.toolchain = self._get_toolchain()
        self.sdk_name = sdk_name
        print("arch:", self.arch)
        print("toolchain:", self.toolchain)

    def _get_actual_architecture(self):
        """Get the actual architecture, handling Windows ARM64 emulation quirks."""
        if not self.is_windows:
            return platform.machine()
         
        machine = platform.machine()
        # On Windows ARM64, platform.machine() might return "AMD64" due to emulation
        # Check multiple indicators for actual architecture
        if machine == "AMD64":
            # Check if we're actually on ARM64
            arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
            arch64 = os.environ.get("PROCESSOR_ARCHITEW6432", "").upper()
            if arch == "ARM64" or arch64 == "ARM64":
                return "ARM64"
            # Additional check for ARM64
            if os.environ.get("NUMBER_OF_PROCESSORS"):
                try:
                    # ARM64 systems often have specific processor identifiers in registry
                    import winreg
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                            identifier = winreg.QueryValueEx(key, "Identifier")[0]
                            if "ARM" in identifier.upper():
                                return "ARM64"
                    except (FileNotFoundError, OSError):
                        pass
                except ImportError:
                    pass
        return machine


    def _get_toolchain(self):
        if self.is_windows:
            if self.arch in ["AMD64", "x86_64"]:
                return "x86_64-windows-msvc"
            if self.arch in ["ARM64", "aarch64"]:
                return "aarch64-windows-msvc"
        else:
            if self.arch == "aarch64":
                return "aarch64-oe-linux-gcc11.2"
            if self.arch == "x86_64":
                return "x86_64-linux-clang"
        return None

    def _find_executable(self, exe_name):
        """Find executable for the current platform."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_path = osp.join(script_dir, exe_name)
        if osp.exists(local_path):
            logger.info("Found %s at: %s", exe_name, local_path)
            return local_path

        if self.toolchain:
            sdk_path = osp.join(self.sdk_root, "bin", self.toolchain, exe_name)
            if osp.exists(sdk_path):
                logger.info("Found %s at: %s", exe_name, sdk_path)
                return sdk_path

        raise RuntimeError(f"{exe_name} not found in {self.sdk_name} SDK: {self.sdk_root}")

    def _run_subprocess(self, cmd):
        """Run a net-run command and log stdout/stderr."""
        logger.debug("Running command: %s", " ".join(cmd))
        executable_dir = osp.dirname(self.net_run_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
            cwd=executable_dir,
            shell=False,
        )
        if result.stdout:
            logger.debug("%s stdout: %s", self.exe_name, result.stdout)
        if result.stderr:
            logger.debug("%s stderr: %s", self.exe_name, result.stderr)
        return result




class QnnRunner(BaseRunner):
    """
    QNN inference runner using qnn-net-run CLI.
    
    ⚠️ **NOTICE**: This class is for QNN models (.bin/.dll/.so) only.
    For SNPE DLC models, use `SnpeRunner`.
    
    **Platform Notes:**
    - **Windows ARM**: Requires context binary (.dll.bin) for HTP inference
    - **Linux ARM**: HTP/CPU/GPU support
    - **x86 Linux**: CPU-only inference
    
    **Usage:**
    ```python
    from onnxwrapper import QnnRunner, save_tensor_to_raw
    
    runner = QnnRunner()
    results = runner._run(
        model="model.bin",  # or .dll, .so
        input_files=["input.raw"],
        output_dir="./output",
        backend="htp"  # or "cpu", "gpu"
    )
    ```
    
    **Backend Options:**
    - `htp`: HTP acceleration (Windows ARM, Linux ARM)
    - `cpu`: CPU inference (available on all platforms)
    - `gpu`: GPU acceleration (platform-dependent)
    """
    
    def __init__(self, sdk_root=None):
        super().__init__("QNN_SDK_ROOT", "QNN")
        self.exe_name = "qnn-net-run.exe" if self.is_windows else "qnn-net-run"
        self.lib_ext = ".dll" if self.is_windows else ".so"
        self.net_run_path = self._find_executable(self.exe_name)
        self.backend_libs = self._find_backend_libs()
        print("qnn")

    def _find_backend_libs(self):
        """Find all backend libraries for the current platform."""
        libs = {}
        if not self.toolchain:
            return libs

        lib_dir = osp.join(self.sdk_root, "lib", self.toolchain)
        if not osp.isdir(lib_dir):
            return libs

        prefix = "libQnn" if not self.is_windows else "Qnn"

        for lib_path in glob.glob(osp.join(lib_dir, f"{prefix}*{self.lib_ext}")):
            lib_name_full = osp.basename(lib_path)
            backend_name = lib_name_full.replace(prefix, "").replace(self.lib_ext, "").replace("Backend", "")
            libs[backend_name.lower()] = lib_path

        logger.info("Found QNN backend libraries: %s", list(libs.keys()))
        return libs

    def _execute_command(self, cmd, output_dir):
        try:
            self._run_subprocess(cmd)

            import yaml

            metadata_path = os.path.join(output_dir, "execution_metadata.yaml")
            if os.path.exists(metadata_path):
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = yaml.safe_load(f)

                output_tensors = []
                if metadata and "graphs" in metadata and metadata["graphs"]:
                    for graph in metadata["graphs"]:
                        if "output_tensors" in graph:
                            output_tensors.extend(graph["output_tensors"])

                results = []
                for tensor in output_tensors:
                    tensor_name = tensor["tensor_name"]
                    dimensions = tensor["dimensions"]
                    tensor_path = os.path.join(output_dir, "Result_0", f"{tensor_name}.raw")

                    if os.path.exists(tensor_path):
                        tensor_data = load_tensor_from_raw(tensor_path, tuple(dimensions))
                        results.append(tensor_data)
                    else:
                        logger.warning("Output tensor file not found: %s", tensor_path)
                return results

            output_files = sorted(glob.glob(os.path.join(output_dir, "Result_*", "*.raw")))
            if not output_files:
                raise RuntimeError(f"No output files found in {output_dir}")

            logger.warning("Metadata file not found, returning raw output file paths")
            return output_files

        except subprocess.CalledProcessError as e:
            logger.error("%s failed! Command: %s\nError: %s", self.exe_name, " ".join(cmd), e.stderr)
            raise RuntimeError(f"{self.sdk_name} inference failed: {e.stderr}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"{self.sdk_name} inference timed out after 5 minutes") from e

    def get_backend_lib(self, backend):
        backend_lib = self.backend_libs.get(backend.lower())
        if not backend_lib:
            if backend not in ["cpu", "gpu", "htp", "dsp"]:
                backend_lib = backend
            else:
                raise RuntimeError(
                    f"QNN backend library for '{backend}' not found in any supported toolchain."
                )
        return backend_lib

    def _run(self, model, input_files, output_dir, backend="cpu"):
        """Execute qnn-net-run and return outputs."""
        os.makedirs(output_dir, exist_ok=True)

        input_list_path = osp.join(output_dir, "input_list.txt")
        with open(input_list_path, "w", encoding="utf-8") as f:
            for input_file in input_files:
                f.write(f"{osp.abspath(input_file)} ")

        backend_lib = self.get_backend_lib(backend)

        name_lower = model.lower()
        is_context = (
            name_lower.endswith(".bin")
            or name_lower.endswith(".serialized.bin")
            or name_lower.endswith(".context.bin")
        )

        model_arg = "--retrieve_context" if is_context else "--model"
        cmd = [
            self.net_run_path,
            model_arg,
            osp.abspath(model),
            "--backend",
            backend_lib,
            "--input_list",
            osp.abspath(input_list_path),
            "--output_dir",
            osp.abspath(output_dir),
            "--profiling_level=detailed",
            "--profiling_option=optrace"
            #check detail inforation https://docs.qualcomm.com/doc/80-90441-15/topic/profile-your-model.html
        ]

        return self._execute_command(cmd, output_dir)

    def run(self, model, tensors, output_dir, qai_model= None,backend="cpu", nchw_to_nhwc=False):
        """Execute qnn-net-run with a list of tensors."""
        delete_directory(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        input_files = []
        for i, tensor in enumerate(tensors):
            if nchw_to_nhwc:
                if tensor.ndim == 4:
                    tensor = tensor.transpose(0, 2, 3, 1)
                elif tensor.ndim == 3:
                    tensor = tensor.transpose(0, 2, 1)

            input_file = osp.join(output_dir, f"input_{i}.raw")
            save_tensor_to_raw(tensor, input_file)
            input_files.append(input_file)

        results = self._run(model, input_files, output_dir, backend)

        if nchw_to_nhwc:
            processed_results = []
            for res in results:
                if res.ndim == 4:
                    processed_results.append(res.transpose(0, 3, 1, 2))
                elif res.ndim == 3:
                    processed_results.append(res.transpose(0, 2, 1))
                else:
                    processed_results.append(res)
            results = processed_results

        #delete_directory(output_dir)
        return results


class SnpeRunner(BaseRunner):
    """
    SNPE inference runner using snpe-net-run CLI.
    
    ⚠️ **NOTICE**: This class is for SNPE DLC models only.
    For QNN models (.bin/.dll/.so), use `QnnRunner` or `InferenceSession`.
    
    **Platform Notes:**
    - **Windows ARM**: DSP backend unavailable; use CPU or GPU
    - **Linux ARM**: DSP backend available on supported SoCs
    - **Android**: Full DSP/GPU/CPU support
    
    **Usage:**
    ```python
    from onnxwrapper import SnpeRunner, save_tensor_to_raw
    
    runner = SnpeRunner()
    results = runner._run(
        dlc_path="model.dlc",
        input_files=["input.raw"],
        output_dir="./output",
        backend="cpu"  # or "dsp", "gpu", "aip"
    )
    ```
    
    **Backend Options:**
    - `cpu`: CPU inference (available on all platforms)
    - `dsp`: DSP acceleration (Linux ARM, Android)
    - `gpu`: GPU acceleration (platform-dependent)
    - `aip`: AI Processor (platform-dependent)
    """
    
    def __init__(self,sdk_root=None):
        super().__init__("SNPE_ROOT", "SNPE")
        self.exe_name = "snpe-net-run.exe" if self.is_windows else "snpe-net-run"
        self.net_run_path = self._find_executable(self.exe_name)
        print(f"snpe runner initialized with path: {self.net_run_path}")

    def _run(self, model_dlc, input_files, output_dir, qai_model=None, backend="cpu"):
        """
        Execute snpe-net-run and return outputs.
        
        Args:
            model_dlc: Path to SNPE DLC model file
            input_files: List of input raw file paths
            output_dir: Output directory for results
            qai_model: Optional QAI model object for output tensor info
            backend: Runtime backend ("cpu", "dsp", "gpu", "aip")
        
        Returns:
            List of numpy arrays (one per output tensor)
        """
        os.makedirs(output_dir, exist_ok=True)

        abs_output_dir = osp.abspath(output_dir)
        abs_model_dlc = osp.abspath(model_dlc)
        abs_input_files = [osp.abspath(f) for f in input_files]
        print(f"run path {self.net_run_path}")
        input_list_path = osp.join(abs_output_dir, "input_list.txt")
        with open(input_list_path, "w", encoding="utf-8") as f:
            f.write(" ".join(abs_input_files) + "\n")

        cmd = [
            self.net_run_path,
            "--container",
            abs_model_dlc,
            "--input_list",
            input_list_path,
            "--output_dir",
            abs_output_dir,
            "--profiling_level detaile"
        ]
        
        # Build model_info from qai_model if available
        model_info = None
        if qai_model is not None:
            try:
                # Use QNNModelWrapper methods to get graph and output info
                graph_name = qai_model.getGraphName()
                output_names = qai_model.getOutputName()
                output_shapes = qai_model.getOutputShapes()
                output_dtypes = qai_model.getOutputDataType()
                
                # Build outputs list
                outputs = []
                for i, name in enumerate(output_names):
                    shape = output_shapes[i] if i < len(output_shapes) else None
                    dtype_str = output_dtypes[i] if i < len(output_dtypes) else "float32"
                    # Convert dtype string to numpy dtype
                    dtype = _np_dtype_from_str(dtype_str)
                    outputs.append({
                        "name": name,
                        "shape": tuple(shape) if shape else None,
                        "dtype": dtype
                    })
                
                model_info = {
                    "graph_name": graph_name,
                    "inputs": [],  # Not needed for --set_output_tensors
                    "outputs": outputs
                }
            except Exception as e:
                logger.warning(f"Failed to get model info from qai_model: {e}")
        
        # If model_info is available, add --set_output_tensors to command
        if model_info:
            graph_name = model_info.get("graph_name")
            output_names = [info["name"] for info in model_info.get("outputs", [])]
            if graph_name and output_names:
                output_tensors_str = f"{graph_name} {','.join(output_names)}"
                cmd.extend(["--set_output_tensors", output_tensors_str])
    
        # Add backend runtime flag
        # ⚠️ **NOTE**: SNPE backend selection - cpu is default if no flag specified
        if backend == "gpu":
            cmd.append("--use_gpu")
        elif backend == "dsp" or backend=="htp":
            cmd.append("--use_dsp")
        elif backend == "aip":
            cmd.append("--use_aip")
        elif backend == "cpu":
            # Explicit CPU flag (optional, but makes intent clear)
            cmd.append("--use_cpu")
        # else: default runtime (SNPE auto-selects based on platform)

        try:
            self._run_subprocess(cmd)

            output_files = sorted(glob.glob(os.path.join(abs_output_dir, "Result_*", "*.raw")))
            if not output_files:
                raise RuntimeError(f"No output files found in {abs_output_dir}")

            if model_info and model_info.get("outputs"):
                logger.info("Found model info. Reshaping raw outputs.")
                output_info = model_info["outputs"]

                if len(output_files) != len(output_info):
                    logger.warning("Mismatch between number of raw output files and outputs in DLC info.")
                    return output_files

                sorted_output_info = sorted(output_info, key=lambda x: x["name"])
                results = []
                for raw_file, tensor_info in zip(output_files, sorted_output_info):
                    shape = tensor_info.get("shape")
                    dtype = tensor_info.get("dtype", np.float32)
                    print(f"DEBUG: Loading {raw_file} with shape {shape} and dtype {dtype} (type: {type(dtype)})")
                    
                    # Check if the file size matches the expected size
                    file_size = os.path.getsize(raw_file)
                    expected_elements = 1
                    for dim in shape:
                        expected_elements *= dim
                    try:
                        dtype_size = np.dtype(dtype).itemsize
                    except:
                        dtype_size = 4  # Default to float32 size
                    expected_size = expected_elements * dtype_size
                    
                    if file_size != expected_size:
                        print(f"DEBUG: File size {file_size} doesn't match expected {expected_size} for dtype {dtype}")
                        # Try with float32
                        dtype = np.float32
                        expected_size = expected_elements * 4
                        print(f"DEBUG: Trying with float32, expected size: {expected_size}")
                    
                    tensor_data = load_tensor_from_raw(raw_file, shape, dtype=dtype)
                    results.append(tensor_data)
                return results

            logger.warning("Model info not available. Returning raw file paths.")
            return output_files

        except subprocess.CalledProcessError as e:
            logger.error("%s failed! Command: %s\nError: %s", self.exe_name, " ".join(cmd), e.stderr)
            raise RuntimeError(f"{self.sdk_name} inference failed: {e.stderr}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"{self.sdk_name} inference timed out after 5 minutes") from e

    def run(self, model_dlc, tensors, output_dir, backend="cpu", qai_model=None):
        """Execute snpe-net-run with a list of tensors."""
        delete_directory(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        input_files = []
        for i, tensor in enumerate(tensors):
            input_file = osp.join(output_dir, f"input_{i}.raw")
            save_tensor_to_raw(tensor, input_file)
            input_files.append(input_file)

        results = self._run(model_dlc, input_files, output_dir, qai_model, backend)

        # delete_directory(output_dir)  # Disabled to keep results for inspection
        return results
    

# -------------------- QNN Model Wrapper --------------------
class QNNModelWrapper(QNNContext):
    """
    QNNContext wrapper.
    Supports loading optional YAML config; if no YAML is provided/found, auto-generates
    a conservative IO config from QNNContext IO specs and (optionally) caches it.
    """

    def __init__(self, model_name: str, model_path: str, runtime_tag: str = ""):
        resolved, candidates = self._find_qnn_model_file(
            model_path, runtime_tag=runtime_tag, return_candidates=True
        )
        resolved = os.path.abspath(resolved)
        logger.info(f"[QNN] Selected QNN model file (runtime={runtime_tag}): {resolved}")
        print(f"[QNN] Selected QNN model file (runtime={runtime_tag}): {resolved}")

        logger.info(f"[QNN] Model candidates (runtime={runtime_tag}): {candidates}")

        super().__init__(model_name, resolved)
        self.model_file_path = resolved
        self.io_config: Dict[str, Any] = {}
        self._expected_input_shapes: Optional[List[Any]] = None
        self._expected_output_shapes: Optional[List[Any]] = None

        # Load YAML config (highest priority: env)
        yaml_paths: List[str] = []
        env_cfg = _env("QAI_IO_CONFIG", "")
        if env_cfg:
            yaml_paths.append(env_cfg)

        yaml_paths += [
            os.path.splitext(resolved)[0] + ".yaml",
            os.path.splitext(resolved)[0] + ".autogen.yaml",
            f"{model_name}.{(runtime_tag or 'htp').lower()}.autogen.yaml",
            model_name + ".yaml",
        ]
        for yp in yaml_paths:
            if yp and os.path.exists(yp):
                try:
                    with open(yp, "r", encoding="utf-8") as f:
                        self.io_config = yaml.safe_load(f) or {}
                    logger.info(f"Loaded IO config from {yp}")
                    break
                except Exception as e:
                    logger.warning(f"Failed to load IO config from {yp}: {e}")

        if not self._log_qnn_model_specs():
            raise RuntimeError("Failed to query QNN model specs. The model may be invalid or incompatible with this wrapper.")

        if not self.io_config:
            self.io_config = self._autogen_io_config(runtime_tag=runtime_tag)
            autogen_path = self._autogen_yaml_path(resolved, model_name, runtime_tag or "htp")
            self._maybe_save_autogen_yaml(autogen_path)
            logger.info("[QNN] Using auto-generated IO config")



    def _log_qnn_model_specs(self) -> None:
        try:
            # Check if QNNContext has getter methods 
            if not all(hasattr(self, m) for m in ('getInputName', 'getOutputName')):
                logger.error("[QNN] QNNContext getter methods not available,invalid model. Unable to query model specs.")
                return False
            graph = self.getGraphName()
            in_names = self.getInputName()
            out_names = self.getOutputName()
            in_shapes = self.getInputShapes()
            out_shapes = self.getOutputShapes()
            in_dt = self.getInputDataType()
            out_dt = self.getOutputDataType()
            self._expected_input_shapes = in_shapes
            self._expected_output_shapes = out_shapes
            logger.info(f"[QNN] Graph name: {graph}")
            logger.info(f"[QNN] QNN input names: {in_names}")
            logger.info(f"[QNN] QNN input shapes: {in_shapes}")
            logger.info(f"[QNN] QNN input dtype: {in_dt}")
            logger.info(f"[QNN] QNN output names: {out_names}")
            logger.info(f"[QNN] QNN output shapes: {out_shapes}")
            logger.info(f"[QNN] QNN output dtype: {out_dt}")
        except Exception as e:
            logger.warning(f"[QNN] Unable to query QNN model specs via QNNContext APIs: {e}")
        return True

    
    @staticmethod
    def _looks_like_token_or_step(name: str) -> bool:
        n = (name or "").strip().lower()
        keys = ("token", "tokens", "input_ids", "ids", "index", "offset", "step", "pos", "position")
        return any(k in n for k in keys) or n in ("x", "i", "t", "idx")

    @staticmethod
    def _is_scalarish_shape(shape: Any) -> bool:
        if not isinstance(shape, (list, tuple)):
            return False
        try:
            dims = [int(x) for x in shape]
        except Exception:
            return False
        if len(dims) == 0:
            return True
        if len(dims) == 1 and dims[0] == 1:
            return True
        if len(dims) == 2 and dims[0] == 1 and dims[1] == 1:
            return True
        return False

    def _autogen_io_config(self, runtime_tag: str = "") -> Dict[str, Any]:
        # Check if QNNContext has getter methods , 
        # if model can't get api, the model is invalid.
        if not all(hasattr(self, m) for m in ('getInputName', 'getOutputName')):
            logger.error("[QNN] QNNContext getter methods not available, returning empty config. invalid model.")
            return {"inputs": [], "outputs": [], "autogen": True}
        
        in_names = self.getInputName()
        out_names = self.getOutputName()

        try:
            in_shapes = self.getInputShapes()
        except Exception:
            in_shapes = [None] * len(in_names)

        try:
            out_shapes = self.getOutputShapes()
        except Exception:
            out_shapes = [None] * len(out_names)

        try:
            in_dt = self.getInputDataType()
        except Exception:
            in_dt = [None] * len(in_names)

        try:
            out_dt = self.getOutputDataType()
        except Exception:
            out_dt = [None] * len(out_names)

        enable_opt_heur = _env("QAI_AUTOGEN_OPTIONAL_HEURISTIC", "0") in ("1", "true", "TRUE", "yes", "YES")
        common_optional = {
            "attention_mask", "token_type_ids", "position_ids",
            "image_sizes", "pixel_values_videos", "pixel_values_video",
            "past_key_values", "past_key", "cache", "use_cache",
        }

        inputs_cfg: List[Dict[str, Any]] = []
        for i, n in enumerate(in_names):
            shape = in_shapes[i] if i < len(in_shapes) else None
            dtype = in_dt[i] if i < len(in_dt) else None

            item: Dict[str, Any] = {
                "name": n,
                "dtype": _dtype_str_from_any(dtype),
                "add_batch": True,
            }

            # GENERIC rule:
            # - if model says int/bool -> respect, and for scalar-ish inputs disable add_batch
            # - if dtype is unclear but name looks like token/step and shape is scalar-ish -> force int32 and disable add_batch
            dtype_str = item["dtype"]
            ln = str(n).strip().lower()

            if dtype_str in ("int32", "int64", "bool"):
                if self._is_scalarish_shape(shape) or ln in ("x", "index", "offset", "step", "pos", "idx"):
                    item["add_batch"] = False
            else:
                if self._looks_like_token_or_step(ln) and self._is_scalarish_shape(shape):
                    item["dtype"] = "int32"
                    item["add_batch"] = False

            lay = _infer_layout_from_shape(shape)
            if lay:
                item["layout"] = lay

            if enable_opt_heur and str(n).strip() in common_optional:
                item["optional"] = True

            inputs_cfg.append(item)

        outputs_cfg: List[Dict[str, Any]] = []
        for i, n in enumerate(out_names):
            shape = out_shapes[i] if i < len(out_shapes) else None
            dtype = out_dt[i] if i < len(out_dt) else None
            item = {"name": n, "dtype": _dtype_str_from_any(dtype)}
            lay = _infer_layout_from_shape(shape)
            if lay:
                item["layout"] = lay
            outputs_cfg.append(item)

        return {
            "inputs": inputs_cfg,
            "outputs": outputs_cfg,
            "autogen": True,
            "autogen_optional_heuristic": bool(enable_opt_heur),
            "runtime_tag": (runtime_tag or "").upper(),
        }

    def _autogen_yaml_path(self, resolved_model_path: str, model_name: str, runtime_tag: str) -> str:
        base_dir = os.path.dirname(resolved_model_path)
        filename = f"{model_name}.{runtime_tag.lower()}.autogen.yaml"
        candidate = os.path.join(base_dir, filename)
        try:
            test_path = os.path.join(base_dir, ".qai_write_test")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(test_path)
            return candidate
        except Exception:
            pass
        if os.name == "nt":
            root = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
            cache_dir = os.path.join(root, "qai_onnxruntime")
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "qai_onnxruntime")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, filename)

    def _maybe_save_autogen_yaml(self, path: str) -> None:
        save = _env("QAI_IO_AUTOGEN_SAVE", "1") not in ("0", "false", "FALSE", "no", "NO")
        if not save:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.io_config, f, sort_keys=False, allow_unicode=True)
            logger.info(f"[QNN] Auto-generated IO config saved to: {path}")
        except Exception as e:
            logger.warning(f"[QNN] Failed to save auto-generated IO config to {path}: {e}")

    def _find_qnn_model_file(
        self,
        model_path: str,
        runtime_tag: str = "",
        return_candidates: bool = False,
    ) -> Union[str, Tuple[str, List[str]]]:
        import platform

        system = platform.system().upper()
        rt = (runtime_tag or "").strip().upper()
        if os.path.exists(model_path) and os.path.isfile(model_path) and not model_path.endswith(".onnx"):
            resolved = model_path
            return (resolved, [resolved]) if return_candidates else resolved
        
        candidates: List[str] = []
        #print(f"Searching QNN model file candidates: {candidates}")
        base = model_path
        base_wo_ext = os.path.splitext(model_path)[0]
        is_onnx = model_path.lower().endswith(".onnx")

        if rt == "CPU":
            candidates += [base + ".cpu.bin", base_wo_ext + ".cpu.bin"]
        else:
            candidates += [base + ".htp.bin", base_wo_ext + ".htp.bin"]
        
        #print(f"Searching QNN model file candidates: {candidates}")
        if system == "WINDOWS":
            candidates += [
                base + ".dll.bin", # base + ".dll",
                base_wo_ext + ".dll.bin", # base_wo_ext + ".dll",
                base + ".onnx.dll.bin", #base + ".onnx.dll",
                base_wo_ext + ".onnx.dll.bin", # base_wo_ext + ".onnx.dll",
            ]
        else:
            candidates += [
                base + ".so.bin", base + ".so",
                base_wo_ext + ".so.bin", base_wo_ext + ".so",
                base + ".onnx.so.bin", base + ".onnx.so",
                base_wo_ext + ".onnx.so.bin", base_wo_ext + ".onnx.so",
            ]
        #print(f"Searching QNN model file candidates: {candidates}")
        if is_onnx:
            base_name = os.path.basename(base_wo_ext)
            dir_name = os.path.dirname(base_wo_ext)
            lib_base = os.path.join(dir_name, "lib" + base_name) if dir_name else ("lib" + base_name)
            if rt == "CPU":
                candidates += [lib_base + ".cpu.bin"]
            else:
                candidates += [lib_base + ".htp.bin"]
            
            candidates += [lib_base + ".dlc"]
            if system == "WINDOWS":
                candidates += [lib_base + ".dll.bin"] #, lib_base + ".dll"
            else:
                candidates += [lib_base + ".so.bin", lib_base + ".so"]

            candidates += [lib_base + ".bin"]

        ##.bin is easy to confuse from qnn model bin source file.  we select is as latest candidate.
        candidates += [base_wo_ext + ".dlc", base + ".bin", base_wo_ext + ".bin",  ]
        #print(f"after onnx Searching QNN model file candidates: {candidates}")        
        logger.debug(f"Searching QNN model file candidates: {candidates}")
        for c in candidates:
            if os.path.exists(c):
                logger.info(f"Found QNN model file: {os.path.basename(c)} (searched from {model_path})")
                return (c, candidates) if return_candidates else c

        logger.warning(f"No QNN model file found for '{model_path}', using original path.")
        return (model_path, candidates) if return_candidates else model_path

    def Inference(self, input_tensors: List[np.ndarray], input_names: Optional[List[str]] = None) -> List[np.ndarray]:
        cfg = self.io_config or {}
        if not hasattr(self, 'getInputName'):
                raise RuntimeError("QNNContext does not have getInputName method. Invalid model or QNNContext API mismatch.")
        full_in_names = self.getInputName()
        out_names = self.getOutputName()
        used_names = input_names if input_names is not None else full_in_names

        if len(used_names) != len(input_tensors):
            raise ValueError(f"Input names count {len(used_names)} != tensors count {len(input_tensors)}")

        per_in = {i.get("name"): i for i in (cfg.get("inputs") or []) if isinstance(i, dict) and i.get("name")}
        per_out = {o.get("name"): o for o in (cfg.get("outputs") or []) if isinstance(o, dict) and o.get("name")}

        exp_shapes = self._expected_input_shapes
        exp_rank: Dict[str, Optional[int]] = {n: None for n in full_in_names}
        exp_layout: Dict[str, Optional[str]] = {n: None for n in full_in_names}
        if exp_shapes:
            for n, s in zip(full_in_names, exp_shapes):
                exp_rank[n] = len(s) if isinstance(s, (list, tuple)) else None
                exp_layout[n] = _infer_layout_from_shape(s)

        processed: List[np.ndarray] = []
        for n, t in zip(used_names, input_tensors):
            item = per_in.get(n, {})
            arr = np.asarray(t)

            dtype_str = item.get("dtype") or cfg.get("dtype") or cfg.get("input_dtype")
            if dtype_str:
                np_dt = _np_dtype_from_str(dtype_str)
                if arr.dtype != np_dt:
                    arr = arr.astype(np_dt, copy=False)

            arr = _ensure_rank(arr, exp_rank.get(n), add_batch=bool(item.get("add_batch", True)))
            # Auto scale normalized [0,1] -> [0,255] for certain compiled models (no model-name checks)
            exp_in_shape = None
            try:
                exp_in_shape = self._get_expected_shape_by_name(n)
            except Exception:
                exp_in_shape = None
            if _auto_scale_01_to_255_needed(item, exp_in_shape, getattr(self, '_expected_output_shapes', None), arr):
                arr = (np.asarray(arr) * np.float32(255.0)).astype(np.asarray(arr).dtype, copy=False)
                logger.info(f"[QNN][Input:{n}] Auto-scaled input *255 (detected normalized [0,1])")

            dst_layout = _normalize_layout(item.get("layout")) or exp_layout.get(n)
            src_layout = _normalize_layout(item.get("src_layout"))

            if not src_layout and arr.ndim in (4, 5) and dst_layout:
                if arr.ndim == 4:
                    if dst_layout == "NCHW" and arr.shape[-1] in (1, 3, 4):
                        src_layout = "NHWC"
                    elif dst_layout == "NHWC" and arr.shape[1] in (1, 3, 4):
                        src_layout = "NCHW"
                else:
                    if dst_layout == "NCTHW" and arr.shape[-1] in (1, 3, 4):
                        src_layout = "NTHWC"
                    elif dst_layout == "NTHWC" and arr.shape[1] in (1, 3, 4):
                        src_layout = "NCTHW"

            if src_layout and dst_layout and src_layout != dst_layout:
                arr = _convert_layout(arr, src_layout, dst_layout)

            arr = np.ascontiguousarray(arr)
            logger.info(f"[QNN][Input:{n}] {_tensor_brief(arr)} layout={src_layout}->{dst_layout}")
            processed.append(arr)

        logger.info("[QNN] Entering QNN backend inference...")
        t0 = time.time()
        outs = super().Inference(processed)
        logger.info(f"[QNN] Inference done in {time.time() - t0:.3f}s")

        final_outs: List[np.ndarray] = []
        for n, o in zip(out_names, outs):
            item = per_out.get(n, {})
            arr = np.asarray(o)
            src_layout = _normalize_layout(item.get("src_layout"))
            dst_layout = _normalize_layout(item.get("layout"))
            if src_layout and dst_layout and src_layout != dst_layout and arr.ndim in (4, 5):
                arr = _convert_layout(arr, src_layout, dst_layout)
            final_outs.append(arr)
            logger.info(f"[QNN][Output:{n}] {_tensor_brief(arr)}")

        return final_outs


# -------------------- ORT enum shims --------------------
class GraphOptimizationLevel:
    """Compatibility shim for onnxruntime.GraphOptimizationLevel.
    onnxruntime uses integer levels 0..3; our SessionOptions.graph_optimization_level
    already stores an int, so exposing these constants makes existing scripts work.
    """
    ORT_DISABLE_ALL = 0
    ORT_ENABLE_BASIC = 1
    ORT_ENABLE_EXTENDED = 2
    ORT_ENABLE_ALL = 3

class ExecutionMode:
    """Compatibility shim for onnxruntime.ExecutionMode."""
    ORT_SEQUENTIAL = 0
    ORT_PARALLEL = 1

class SessionOptionsMode:
    """Placeholder for any future mode enums; kept for forward compatibility."""
    pass

# -------------------- SessionOptions --------------------
class SessionOptions:
    def __init__(self):
        self.log_severity_level = 0
        self.log_verbosity_level = 0
        self.enable_profiling = False
        self.optimized_model_filepath = ""
        self.graph_optimization_level = 1
        self.enable_mem_pattern = True
        self.execution_mode = ExecutionMode.ORT_SEQUENTIAL
        self.qnn_runtime = Runtime.HTP
        self.qnn_libs_dir = ""
        self.qnn_profiling_level = ProfilingLevel.OFF

        rt = _env("QAI_QNN_RUNTIME", "")
        if rt:
            self.set_qnn_runtime(rt)

        libs = _env("QAI_QNN_LIBS_DIR", "")
        if libs:
            self.qnn_libs_dir = libs
        if not self.qnn_libs_dir:
            self.qnn_libs_dir = _default_qai_libs_dir()

        _ensure_path_contains(self.qnn_libs_dir)
        logger.info(f"[QNN] Using QNN libs dir: {os.path.abspath(self.qnn_libs_dir)}")

    def set_qnn_runtime(self, runtime: Union[Runtime, str]):
        if isinstance(runtime, str):
            m = {"HTP": Runtime.HTP, "CPU": Runtime.CPU}
            runtime = m.get(runtime.strip().upper(), Runtime.HTP)
        self.qnn_runtime = runtime

    def set_qnn_libs_dir(self, path: str):
        self.qnn_libs_dir = path
        _ensure_path_contains(self.qnn_libs_dir)

    def enable_qnn_profiling(self, enable: bool = True):
        self.qnn_profiling_level = ProfilingLevel.BASIC if enable else ProfilingLevel.OFF


# -------------------- InferenceSession (ORT-like) --------------------
class InferenceSession:
    _qnn_initialized = False
    _last_config_key: Optional[tuple] = None

    def __init__(self, model_path: str, sess_options: Optional[SessionOptions] = None, providers: Optional[List[str]] = None):
        if sess_options is None:
            sess_options = SessionOptions()
        self.sess_options = sess_options
        self.model_path = model_path
        self._perf_profile = _env("QAI_QNN_PERF_PROFILE", "BURST").upper()
        self._auto_fill_missing = _env_bool("QAI_AUTO_FILL_MISSING", "0")
        self._model_profile = None

        key = (
            sess_options.qnn_libs_dir,
            str(sess_options.qnn_runtime),
            str(sess_options.qnn_profiling_level),
            sess_options.log_severity_level,
        )
        if (not InferenceSession._qnn_initialized) or (InferenceSession._last_config_key != key):
            log_level_map = {
                0: LogLevel.ERROR,
                1: LogLevel.WARN,
                2: LogLevel.WARN,
                3: LogLevel.INFO,
                4: LogLevel.DEBUG,
            }
            log_level = log_level_map.get(sess_options.log_severity_level, LogLevel.ERROR)
            _ensure_path_contains(sess_options.qnn_libs_dir)
            QNNConfig.Config(
                sess_options.qnn_libs_dir,
                sess_options.qnn_runtime,
                log_level,
                sess_options.qnn_profiling_level,
            )
            InferenceSession._qnn_initialized = True
            InferenceSession._last_config_key = key
            logger.info("QNN environment initialized")

        runtime_tag = "CPU" if sess_options.qnn_runtime == Runtime.CPU else "HTP"
        model_name = os.path.splitext(os.path.basename(model_path))[0]
        self._model = QNNModelWrapper(model_name, model_path, runtime_tag=runtime_tag)
        self._input_names = self._model.getInputName()
        self._output_names = self._model.getOutputName()

        logger.info(f"Loaded model (user arg): {model_path}")
        logger.info(f"Loaded model (resolved): {getattr(self._model, 'model_file_path', '')}")
        logger.info(f"Input names: {self._input_names}")
        logger.info(f"Output names: {self._output_names}")
        if self.sess_options.enable_profiling:
            
            lower = self._model.model_file_path.lower()
            print("enable profiling",lower)
            if any(lower.endswith(ext) for ext in (".dlc")):
                self._model_profile = SnpeRunner()
            elif any(lower.endswith(ext) for ext in (".so", ".so.bin", ".dll", ".dll.bin", ".bin")):
                self._model_profile = QnnRunner()

    # ---- expected helpers ----
    def _get_expected_shape_by_name(self, name: str):
        try:
            shapes = self._model.getInputShapes()
            for n, s in zip(self._input_names, shapes):
                if n == name:
                    return s
        except Exception:
            pass
        return None

    def _get_expected_dtype_by_name(self, name: str):
        try:
            dtypes = self._model.getInputDataType()
            for n, dt in zip(self._input_names, dtypes):
                if n == name:
                    s = str(dt).lower()
                    if "int32" in s:
                        return np.int32
                    if "int64" in s:
                        return np.int64
                    if "float16" in s:
                        return np.float16
                    if "float32" in s or "float" in s:
                        return np.float32
                    if "bool" in s:
                        return np.bool_
        except Exception:
            pass
        return None

    def _shape_to_int_list(self, s):
        if not isinstance(s, (list, tuple)):
            return None
        out = []
        for v in s:
            try:
                out.append(int(v))
            except Exception:
                out.append(-1)
        return out

    def _normalize_scalar_1x1(self, arr: Any) -> np.ndarray:
        a = np.asarray(arr)
        if a.ndim == 0:
            a = a.reshape((1, 1))
        elif a.ndim == 1 and a.shape == (1,):
            a = a.reshape((1, 1))
        elif a.ndim == 2 and a.shape == (1, 1):
            pass
        else:
            return np.ascontiguousarray(a)
        return np.ascontiguousarray(a)

    def _apply_generic_aliases(self, input_feed: Dict[str, np.ndarray]) -> None:
        alias_map: Dict[str, List[str]] = {
            # audio
            "audio": ["mel", "input", "waveform"],
            "mel": ["audio", "mel_input"],
            # token/step (common transformer)
            "x": ["tokens", "token", "input_ids", "ids"],
            "index": ["offset", "step", "pos", "position", "position_id", "position_ids"],
            # cache common variants
            "k_cache_cross": ["n_layer_cross_k", "cross_k", "k_cross", "encoder_k_cache", "k_cache_encoder"],
            "v_cache_cross": ["n_layer_cross_v", "cross_v", "v_cross", "encoder_v_cache", "v_cache_encoder"],
            "k_cache_self": ["in_n_layer_self_k_cache", "self_k", "k_self", "k_cache_in", "k_cache_self_in"],
            "v_cache_self": ["in_n_layer_self_v_cache", "self_v", "v_self", "v_cache_in", "v_cache_self_in"],
        }
        for expected, aliases in alias_map.items():
            if expected in self._input_names and expected not in input_feed:
                for a in aliases:
                    if a in input_feed:
                        input_feed[expected] = input_feed[a]
                        logger.info(f"[QNN] Input alias applied: '{a}' -> '{expected}'")
                        break

    def _pack_k_cache_headsplit(self, arr, exp_shape):
        a = np.asarray(arr)
        exp = self._shape_to_int_list(exp_shape)
        if exp and a.ndim == 4 and len(exp) == 4:
            H, D = exp[1], exp[2]
            if H > 0 and D > 0 and a.shape[-1] == H * D:
                if a.shape[1] == 1:
                    a = a[:, 0, :, :]
                a = a.reshape(a.shape[0], a.shape[1], H, D)
                a = np.transpose(a, (0, 2, 3, 1))
                return np.ascontiguousarray(a.astype(np.float32, copy=False))
        return np.ascontiguousarray(a.astype(np.float32, copy=False))

    def _pack_v_cache_headsplit(self, arr, exp_shape):
        a = np.asarray(arr)
        exp = self._shape_to_int_list(exp_shape)
        if exp and a.ndim == 4 and len(exp) == 4:
            H, D = exp[1], exp[3]
            if H > 0 and D > 0 and a.shape[-1] == H * D:
                if a.shape[1] == 1:
                    a = a[:, 0, :, :]
                a = a.reshape(a.shape[0], a.shape[1], H, D)
                a = np.transpose(a, (0, 2, 1, 3))
                return np.ascontiguousarray(a.astype(np.float32, copy=False))
        return np.ascontiguousarray(a.astype(np.float32, copy=False))

    def run(self, output_names: Optional[List[str]], input_feed: Dict[str, np.ndarray], run_options: Optional[Any] = None) -> List[np.ndarray]:
        # Get ONNX output order from YAML (io_config)
        yaml_output_names = getattr(self._model, "io_config", {}).get("output", []) or self._output_names
        
        cfg = getattr(self._model, "io_config", {}) or {}
        per_in = {i.get("name"): i for i in (cfg.get("inputs") or []) if isinstance(i, dict) and i.get("name")}
        
        expected_names = list(self._input_names)
        kv_schema_required = {"k_cache_self", "v_cache_self", "k_cache_cross", "v_cache_cross"}
        is_kv_cache_decoder = kv_schema_required.issubset(set(expected_names))

        if is_kv_cache_decoder:
            self._apply_generic_aliases(input_feed)

            if "x" in input_feed:
                input_feed["x"] = self._normalize_scalar_1x1(input_feed["x"])
            if "index" in input_feed:
                input_feed["index"] = self._normalize_scalar_1x1(input_feed["index"])

            for name in ("x", "index"):
                if name in input_feed:
                    dt = self._get_expected_dtype_by_name(name)
                    if dt is not None:
                        arr = np.asarray(input_feed[name])
                        if arr.dtype != dt:
                            input_feed[name] = arr.astype(dt, copy=False)

            for k in ("k_cache_cross", "k_cache_self"):
                if k in input_feed:
                    input_feed[k] = self._pack_k_cache_headsplit(input_feed[k], self._get_expected_shape_by_name(k))
            for v in ("v_cache_cross", "v_cache_self"):
                if v in input_feed:
                    input_feed[v] = self._pack_v_cache_headsplit(input_feed[v], self._get_expected_shape_by_name(v))

            for name in expected_names:
                if name not in input_feed:
                    item = per_in.get(name, {})
                    if bool(item.get("optional", False)):
                        logger.info(f"[QNN] Optional input '{name}' not provided; skipping.")
                        continue
                    if not self._auto_fill_missing:
                        raise ValueError(
                            f"Missing required input: {name}. Provide it from script, "
                            "or set QAI_AUTO_FILL_MISSING=1 to allow zero-fill (not recommended)."
                        )
                    if name in ("x", "index"):
                        dt = self._get_expected_dtype_by_name(name) or np.int32
                        input_feed[name] = np.zeros((1, 1), dtype=dt)
                    elif name in ("k_cache_cross", "v_cache_cross", "k_cache_self", "v_cache_self"):
                        input_feed[name] = np.zeros((1, 1, 1, 1), dtype=np.float32)

        # apply aliases for non-kv models too (mel->audio, etc.)
        self._apply_generic_aliases(input_feed)

        provided_names: List[str] = []
        inputs: List[np.ndarray] = []
        for n in self._input_names:
            if n in input_feed:
                provided_names.append(n)
                inputs.append(input_feed[n])
                continue
            item = per_in.get(n, {})
            if bool(item.get("optional", False)):
                logger.info(f"[QNN] Optional input '{n}' not provided; skipping.")
                continue
            raise ValueError(f"Missing input: {n}. Expected: {self._input_names}")

        if self._model_profile:
            # Determine backend based on model type
            lower = self._model.model_file_path.lower()
            if any(lower.endswith(ext) for ext in (".dlc")):
                # For SNPE models, use DSP backend (default) or CPU if specified
                backend = "cpu" if self.sess_options.qnn_runtime == Runtime.CPU else "dsp"
            else:
                # For QNN models, use HTP backend
                backend = "cpu" if self.sess_options.qnn_runtime == Runtime.CPU else "htp"
            # Use current working directory for profiling output
            output_dir = os.path.join(os.getcwd(), "qairt_profile_output")
            try:
                outs = self._model_profile.run( self._model.model_file_path, inputs, output_dir=output_dir, backend=backend, qai_model=self._model)
                
                # Reorder outputs to match ONNX/YAML order
                # QnnRunner returns outputs in QNN's internal order, need to reorder
                out_map = {n: t for n, t in zip(self._output_names, outs)}
                if output_names is not None:
                    return [out_map[n] for n in output_names]
                else:
                    return [out_map[n] for n in yaml_output_names]
            except Exception as e:
                print(f"Profile output saved to: {output_dir}")
                raise e

        if self._perf_profile == "BURST":
            PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

        try:
            outs = self._model.Inference(inputs, input_names=provided_names)
            # Reorder outputs to match ONNX/YAML order
            # QNN returns in its own order, we map back to ONNX order
            out_map = {n: t for n, t in zip(self._output_names, outs)}
            
            if output_names is not None:
                outs = [out_map[n] for n in output_names]
            else:
                # When no specific order requested, use ONNX order from YAML
                outs = [out_map[n] for n in yaml_output_names]
            return outs
        finally:
            try:
                PerfProfile.RelPerfProfileGlobal()
            except Exception:
                pass

    def get_inputs(self) -> List[TensorInfo]:
        shapes = None
        dtypes = None
        try:
            shapes = self._model.getInputShapes()
            dtypes = self._model.getInputDataType()
        except Exception:
            pass
        infos: List[TensorInfo] = []
        for i, n in enumerate(self._input_names):
            s = _as_list_of_ints(shapes[i]) if shapes and i < len(shapes) else None
            dt = _ort_type_from_dtype(str(dtypes[i])) if dtypes and i < len(dtypes) else "tensor(float)"
            infos.append(TensorInfo(name=n, shape=s, type=dt))
        return infos

    def get_outputs(self) -> List[TensorInfo]:
        shapes = None
        dtypes = None
        try:
            shapes = self._model.getOutputShapes()
            dtypes = self._model.getOutputDataType()
        except Exception:
            pass
        infos: List[TensorInfo] = []
        for i, n in enumerate(self._output_names):
            s = _as_list_of_ints(shapes[i]) if shapes and i < len(shapes) else None
            dt = _ort_type_from_dtype(str(dtypes[i])) if dtypes and i < len(dtypes) else "tensor(float)"
            infos.append(TensorInfo(name=n, shape=s, type=dt))
        return infos

    def get_input_names(self) -> List[str]:
        return self._input_names

    def get_output_names(self) -> List[str]:
        return self._output_names

    def __del__(self):
        if hasattr(self, "_model"):
            del self._model


# -------------------- module-level helpers --------------------
def get_available_providers() -> List[str]:
    return ["QNNExecutionProvider", "CPUExecutionProvider"]


def get_device() -> str:
    return "QNN"


def run_inference(
    model_path: str,
    input_data: Union[np.ndarray, Dict[str, np.ndarray]],
    qnn_libs_dir: str = "",
    runtime: str = "HTP",
) -> List[np.ndarray]:
    so = SessionOptions()
    so.set_qnn_runtime(runtime)
    if qnn_libs_dir:
        so.set_qnn_libs_dir(qnn_libs_dir)

    sess = InferenceSession(model_path, so)
    if isinstance(input_data, np.ndarray):
        names = sess.get_input_names()
        if len(names) != 1:
            raise ValueError(f"Model expects {len(names)} inputs; single array provided")
        input_data = {names[0]: input_data}
    return sess.run(None, input_data)