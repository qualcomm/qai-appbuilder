# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union

import numpy as np
import onnx
import yaml


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


class Runtime:
    HTP = "HTP"
    CPU = "CPU"


class SessionOptions:
    def __init__(self):
        # x86 wrapper policy: always force CPU runtime for stable local execution.
        self.qnn_runtime = "CPU"

    def set_qnn_runtime(self, runtime: Union[str, Runtime]):
        # Ignore caller-provided runtime (e.g. HTP) and keep CPU.
        self.qnn_runtime = "CPU"


class InferenceSession:
    def __init__(
        self,
        model_path: str,
        sess_options: Optional[SessionOptions] = None,
        providers: Optional[List[str]] = None,
    ):
        self.model_path = os.path.abspath(model_path)
        self.model_stem = os.path.splitext(self.model_path)[0]
        self.sess_options = sess_options or SessionOptions()
        self.sdk_root = os.environ.get("QAIRT_SDK_ROOT", "")
        if not self.sdk_root:
            raise RuntimeError("QAIRT_SDK_ROOT is not set. Source QAIRT env setup first.")

        self.host_env = self._detect_host_env()
        self.input_names, self.output_names, self.input_shapes, self.output_shapes = self._load_io_metadata()
        self.backend_model = self._resolve_backend_model()

    def _detect_host_env(self) -> str:
        candidates = [
            "x86_64-linux-clang",
            "aarch64-linux-gcc",
            "x86_64-windows-msvc",
        ]
        for host_env in candidates:
            if os.path.isdir(os.path.join(self.sdk_root, "bin", host_env)):
                return host_env
        raise RuntimeError("Unable to detect QAIRT host toolchain path under QAIRT_SDK_ROOT/bin")

    def _load_io_metadata(self):
        yaml_candidates = [
            os.environ.get("QAI_IO_CONFIG", ""),
            self.model_stem + ".yaml",
            os.path.basename(self.model_stem) + ".yaml",
        ]
        input_names: List[str] = []
        output_names: List[str] = []
        for candidate in yaml_candidates:
            if candidate and os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                input_names = list(data.get("input") or [])
                output_names = list(data.get("output") or [])
                break

        graph = onnx.load(self.model_path).graph
        input_shapes: Dict[str, List[int]] = {}
        output_shapes: Dict[str, List[int]] = {}

        def _shape(value_info) -> List[int]:
            dims = []
            tensor_shape = value_info.type.tensor_type.shape
            for dim in tensor_shape.dim:
                dims.append(dim.dim_value if dim.HasField("dim_value") else -1)
            return dims

        for tensor in graph.input:
            input_shapes[tensor.name] = _shape(tensor)
        for tensor in graph.output:
            output_shapes[tensor.name] = _shape(tensor)

        if not input_names:
            input_names = [item.name for item in graph.input]
        if not output_names:
            output_names = [item.name for item in graph.output]

        return input_names, output_names, input_shapes, output_shapes

    def _resolve_backend_model(self) -> str:
        candidates = []
        prefer_qnn = os.environ.get("QAI_USE_QNN_NETRUN", "0").strip().lower() in {"1", "true", "yes", "on"}
        qnn_candidates = [
            os.path.join(os.path.dirname(self.model_path), "lib" + os.path.basename(self.model_stem) + ".so"),
            os.path.join(os.path.dirname(self.model_path), "lib" + os.path.basename(self.model_stem) + ".so.bin"),
            self.model_stem + ".so",
            self.model_stem + ".so.bin",
            self.model_stem + ".bin",
        ]
        dlc_candidates = []
        # Prefer explicit runtime artifacts first.
        if self.sess_options.qnn_runtime.upper() == "CPU":
            dlc_candidates.extend([
                self.model_stem + "_fp32_cpu.dlc",
                os.path.join(os.path.dirname(self.model_path), os.path.basename(self.model_stem) + "_fp32_cpu.dlc"),
                self.model_stem + "_cpu.dlc",
                os.path.join(os.path.dirname(self.model_path), os.path.basename(self.model_stem) + "_cpu.dlc"),
            ])
        dlc_candidates.extend([
            self.model_stem + ".dlc",
            os.path.join(os.path.dirname(self.model_path), os.path.basename(self.model_stem) + ".dlc"),
        ])
        candidates = qnn_candidates + dlc_candidates if prefer_qnn else dlc_candidates + qnn_candidates

        for candidate in candidates:
            if os.path.exists(candidate):
                return os.path.abspath(candidate)

        raise RuntimeError(f"No runtime model artifact found for {self.model_path}. Expected .dlc/.so/.bin.")

    def _snpe_net_run_path(self) -> str:
        exe = "snpe-net-run.exe" if os.name == "nt" else "snpe-net-run"
        path = os.path.join(self.sdk_root, "bin", self.host_env, exe)
        if not os.path.exists(path):
            raise RuntimeError(f"{exe} not found at {path}")
        return path

    def _qnn_net_run_path(self) -> str:
        exe = "qnn-net-run.exe" if os.name == "nt" else "qnn-net-run"
        path = os.path.join(self.sdk_root, "bin", self.host_env, exe)
        if not os.path.exists(path):
            raise RuntimeError(f"{exe} not found at {path}")
        return path

    def _qnn_backend_lib(self) -> str:
        lib_dir = os.path.join(self.sdk_root, "lib", self.host_env)
        runtime = self.sess_options.qnn_runtime.upper()
        if os.name == "nt":
            cpu_name = "QnnCpu.dll"
            htp_name = "QnnHtp.dll"
        else:
            cpu_name = "libQnnCpu.so"
            htp_name = "libQnnHtp.so"

        preferred = htp_name if runtime == "HTP" else cpu_name
        candidate = os.path.join(lib_dir, preferred)
        if os.path.exists(candidate):
            return candidate

        fallback = os.path.join(lib_dir, cpu_name)
        if os.path.exists(fallback):
            return fallback

        raise RuntimeError(f"QNN backend library not found in {lib_dir}")

    def _save_inputs(self, workdir: str, input_feed: Dict[str, np.ndarray]) -> str:
        paths = []
        for idx, name in enumerate(self.input_names):
            if name not in input_feed:
                raise ValueError(f"Missing input: {name}. Provided: {list(input_feed.keys())}")
            tensor = np.ascontiguousarray(input_feed[name]).astype(np.float32, copy=False)
            file_path = os.path.join(workdir, f"input_{idx}.raw")
            tensor.tofile(file_path)
            paths.append(file_path)

        input_list = os.path.join(workdir, "input_list.txt")
        with open(input_list, "w", encoding="utf-8") as handle:
            handle.write(" ".join(paths) + "\n")
        return input_list

    def _run_snpe(self, input_list: str, workdir: str):
        cmd = [
            self._snpe_net_run_path(),
            "--container",
            self.backend_model,
            "--input_list",
            input_list,
            "--output_dir",
            workdir,
        ]
        runtime = self.sess_options.qnn_runtime.upper()
        if runtime == "CPU":
            pass
        elif runtime == "HTP":
            cmd.append("--use_dsp")
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"snpe-net-run failed (exit={exc.returncode}). stderr:\\n{exc.stderr}"
            ) from exc

    def _run_qnn(self, input_list: str, workdir: str):
        is_context = self.backend_model.endswith(".bin")
        model_flag = "--retrieve_context" if is_context else "--model"
        cmd = [
            self._qnn_net_run_path(),
            model_flag,
            self.backend_model,
            "--backend",
            self._qnn_backend_lib(),
            "--input_list",
            input_list,
            "--output_dir",
            workdir,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"qnn-net-run failed (exit={exc.returncode}). stderr:\\n{exc.stderr}"
            ) from exc

    def _load_outputs(self, workdir: str) -> List[np.ndarray]:
        result_dirs = sorted(Path(workdir).glob("Result_*"))
        if not result_dirs:
            raise RuntimeError("No Result_* output directory produced by net-run")

        raw_files = sorted(result_dirs[0].glob("*.raw"))
        if not raw_files:
            raise RuntimeError("No .raw output files produced by net-run")

        outputs: List[np.ndarray] = []
        by_stem = {path.stem: path for path in raw_files}

        ordered_files: List[Path] = []
        for name in self.output_names:
            if name in by_stem:
                ordered_files.append(by_stem[name])
        if len(ordered_files) != len(self.output_names):
            ordered_files = raw_files

        for idx, raw_path in enumerate(ordered_files):
            data = np.fromfile(raw_path, dtype=np.float32)
            name = self.output_names[idx] if idx < len(self.output_names) else raw_path.stem
            shape = self.output_shapes.get(name)
            if shape and all(d > 0 for d in shape) and int(np.prod(shape)) == data.size:
                data = data.reshape(shape)
            outputs.append(data)
        return outputs

    def run(
        self,
        output_names: Optional[List[str]],
        input_feed: Dict[str, np.ndarray],
        run_options: Optional[Any] = None,
    ) -> List[np.ndarray]:
        workdir = tempfile.mkdtemp(prefix="aipc_netrun_")
        try:
            input_list = self._save_inputs(workdir, input_feed)
            if self.backend_model.endswith(".dlc"):
                self._run_snpe(input_list, workdir)
            else:
                self._run_qnn(input_list, workdir)

            outputs = self._load_outputs(workdir)
            if output_names is None:
                return outputs

            output_map = {}
            for idx, name in enumerate(self.output_names):
                if idx < len(outputs):
                    output_map[name] = outputs[idx]
            return [output_map[name] for name in output_names]
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def get_inputs(self) -> List[TensorInfo]:
        out: List[TensorInfo] = []
        for name in self.input_names:
            out.append(TensorInfo(name=name, shape=self.input_shapes.get(name), type="tensor(float)"))
        return out

    def get_outputs(self) -> List[TensorInfo]:
        out: List[TensorInfo] = []
        for name in self.output_names:
            out.append(TensorInfo(name=name, shape=self.output_shapes.get(name), type="tensor(float)"))
        return out

    def get_input_names(self) -> List[str]:
        return list(self.input_names)

    def get_output_names(self) -> List[str]:
        return list(self.output_names)


class ExecutionMode:
    ORT_SEQUENTIAL = 0


def set_default_logger_severity(severity: int) -> None:
    return None


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
    options = SessionOptions()
    options.set_qnn_runtime(runtime)
    session = InferenceSession(model_path, options)
    if isinstance(input_data, np.ndarray):
        names = session.get_input_names()
        if len(names) != 1:
            raise ValueError(f"Model expects {len(names)} inputs; single array provided")
        input_data = {names[0]: input_data}
    return session.run(None, input_data)
