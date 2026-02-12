#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
# -*- coding: utf-8 -*-
"""
Hot-patch replacement for `onnxruntime` using Qualcomm QAI AppBuilder (QNNContext).

Key capabilities:
- Run existing scripts unchanged by injecting this module as "onnxruntime".
- Resolve and load QNN model (.bin/.dll/.so) from an ONNX path.
- Log which QNN model file is loaded and the model IO specs.

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


logger = logging.getLogger(__name__)


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


# -------------------- helpers --------------------
def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


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
        # treat D as T for video, alias to our canonical naming
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

    # 5D video:
    # NCTHW = (N, C, T, H, W)
    # NTHWC = (N, T, H, W, C)
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
        "float32": "tensor(float)", "fp32": "tensor(float)", "float": "tensor(float)",
        "float16": "tensor(float16)", "fp16": "tensor(float16)", "half": "tensor(float16)",
        "uint8": "tensor(uint8)", "u8": "tensor(uint8)",
        "int8": "tensor(int8)", "i8": "tensor(int8)",
        "uint16": "tensor(uint16)", "u16": "tensor(uint16)",
        "int16": "tensor(int16)", "i16": "tensor(int16)",
        "int32": "tensor(int32)", "i32": "tensor(int32)",
        "int64": "tensor(int64)", "i64": "tensor(int64)",
        "bool": "tensor(bool)", "boolean": "tensor(bool)",
    }
    return m.get(s, "tensor(float)")


def _np_dtype_from_str(dtype: Optional[str]) -> np.dtype:
    if not dtype:
        return np.float32
    s = str(dtype).strip().lower()
    m = {
        "float32": np.float32, "fp32": np.float32, "float": np.float32,
        "float16": np.float16, "fp16": np.float16, "half": np.float16,
        "uint8": np.uint8, "u8": np.uint8,
        "int8": np.int8, "i8": np.int8,
        "uint16": np.uint16, "u16": np.uint16,
        "int16": np.int16, "i16": np.int16,
        "int32": np.int32, "i32": np.int32,
        "int64": np.int64, "i64": np.int64,
        "bool": np.bool_, "boolean": np.bool_,
    }
    return m.get(s, np.float32)


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


# -------------------- QNN wrapper --------------------
class QNNModelWrapper(QNNContext):
    """
    QNNContext wrapper.
    Supports loading optional YAML config; if no YAML is provided/found, auto-generates
    a conservative IO config from QNNContext IO specs and (optionally) caches it.
    """

    def __init__(self, model_name: str, model_path: str, runtime_tag: str = ""):
        resolved, candidates = self._find_qnn_model_file(model_path, runtime_tag=runtime_tag, return_candidates=True)
        resolved = os.path.abspath(resolved)
        logger.info(f"[QNN] Selected QNN model file (runtime={runtime_tag}): {resolved}")
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

        # Try autogen cache and co-located yaml
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

        self._log_qnn_model_specs()

        # If no yaml loaded, auto-generate and optionally persist
        if not self.io_config:
            self.io_config = self._autogen_io_config(runtime_tag=runtime_tag)
            autogen_path = self._autogen_yaml_path(resolved, model_name, runtime_tag or "htp")
            self._maybe_save_autogen_yaml(autogen_path)
            logger.info("[QNN] Using auto-generated IO config")

    def _log_qnn_model_specs(self) -> None:
        try:
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

    def _autogen_io_config(self, runtime_tag: str = "") -> Dict[str, Any]:
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

        # prefer model dir if writable
        try:
            test_path = os.path.join(base_dir, ".qai_write_test")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(test_path)
            return candidate
        except Exception:
            pass

        # fallback to user cache
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

        if os.path.exists(model_path) and os.path.isfile(model_path):
            resolved = model_path
            return (resolved, [resolved]) if return_candidates else resolved

        candidates: List[str] = []
        base = model_path
        base_wo_ext = os.path.splitext(model_path)[0]
        is_onnx = model_path.lower().endswith(".onnx")

        if rt == "CPU":
            candidates += [base + ".cpu.bin", base_wo_ext + ".cpu.bin"]
        else:
            candidates += [base + ".htp.bin", base_wo_ext + ".htp.bin"]
        candidates += [base + ".bin", base_wo_ext + ".bin"]

        if system == "WINDOWS":
            candidates += [
                base + ".dll.bin", base + ".dll",
                base_wo_ext + ".dll.bin", base_wo_ext + ".dll",
                base + ".onnx.dll.bin", base + ".onnx.dll",
                base_wo_ext + ".onnx.dll.bin", base_wo_ext + ".onnx.dll",
            ]
        else:
            candidates += [
                base + ".so.bin", base + ".so",
                base_wo_ext + ".so.bin", base_wo_ext + ".so",
                base + ".onnx.so.bin", base + ".onnx.so",
                base_wo_ext + ".onnx.so.bin", base_wo_ext + ".onnx.so",
            ]

        if is_onnx:
            base_name = os.path.basename(base_wo_ext)
            dir_name = os.path.dirname(base_wo_ext)
            lib_base = os.path.join(dir_name, "lib" + base_name) if dir_name else ("lib" + base_name)
            if rt == "CPU":
                candidates += [lib_base + ".cpu.bin"]
            else:
                candidates += [lib_base + ".htp.bin"]
            candidates += [lib_base + ".bin"]
            if system == "WINDOWS":
                candidates += [lib_base + ".dll.bin", lib_base + ".dll"]
            else:
                candidates += [lib_base + ".so.bin", lib_base + ".so"]

        for c in candidates:
            if os.path.exists(c):
                logger.info(f"Found QNN model file: {os.path.basename(c)} (searched from {model_path})")
                return (c, candidates) if return_candidates else c

        logger.warning(f"No QNN model file found for '{model_path}', using original path.")
        return (model_path, candidates) if return_candidates else model_path

    def Inference(self, input_tensors: List[np.ndarray], input_names: Optional[List[str]] = None) -> List[np.ndarray]:
        cfg = self.io_config or {}

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

            dst_layout = _normalize_layout(item.get("layout")) or exp_layout.get(n)
            src_layout = _normalize_layout(item.get("src_layout"))

            # heuristic src layout only if dst known and tensor is 4D/5D
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


# -------------------- ORT-like stubs --------------------
class SessionOptions:
    def __init__(self):
        self.log_severity_level = 2
        self.log_verbosity_level = 0
        self.enable_profiling = False
        self.optimized_model_filepath = ""
        self.graph_optimization_level = 1
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


class InferenceSession:
    _qnn_initialized = False
    _last_config_key: Optional[tuple] = None

    def __init__(self, model_path: str, sess_options: Optional[SessionOptions] = None, providers: Optional[List[str]] = None):
        if sess_options is None:
            sess_options = SessionOptions()
        self.sess_options = sess_options
        self.model_path = model_path
        self._perf_profile = _env("QAI_QNN_PERF_PROFILE", "BURST").upper()

        key = (sess_options.qnn_libs_dir, str(sess_options.qnn_runtime), str(sess_options.qnn_profiling_level), sess_options.log_severity_level)
        if (not InferenceSession._qnn_initialized) or (InferenceSession._last_config_key != key):
            log_level_map = {0: LogLevel.ERROR, 1: LogLevel.WARN, 2: LogLevel.WARN, 3: LogLevel.INFO, 4: LogLevel.DEBUG}
            log_level = log_level_map.get(sess_options.log_severity_level, LogLevel.WARN)
            _ensure_path_contains(sess_options.qnn_libs_dir)
            QNNConfig.Config(sess_options.qnn_libs_dir, sess_options.qnn_runtime, log_level, sess_options.qnn_profiling_level)
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

    def run(self, output_names: Optional[List[str]], input_feed: Dict[str, np.ndarray], run_options: Optional[Any] = None) -> List[np.ndarray]:
        cfg = getattr(self._model, "io_config", {}) or {}
        per_in = {i.get("name"): i for i in (cfg.get("inputs") or []) if isinstance(i, dict) and i.get("name")}

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

        if self._perf_profile == "BURST":
            PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

        try:
            outs = self._model.Inference(inputs, input_names=provided_names)
            if output_names is not None:
                out_map = {n: t for n, t in zip(self._output_names, outs)}
                outs = [out_map[n] for n in output_names]
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


def get_available_providers() -> List[str]:
    return ["QNNExecutionProvider", "CPUExecutionProvider"]


def get_device() -> str:
    return "QNN"


def run_inference(model_path: str, input_data: Union[np.ndarray, Dict[str, np.ndarray]], qnn_libs_dir: str = "", runtime: str = "HTP") -> List[np.ndarray]:
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
