# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""
QAI ONNX Runtime Compatibility Layer

Provides an `onnxruntime`-like Python API backed by Qualcomm AI (QNN via `qai_appbuilder`).

This module is intended to be injected as `sys.modules["onnxruntime"]` so existing scripts
that do `import onnxruntime as ort` can run on QNN without source changes.
"""

import numpy as np
import os
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Union, Any, Iterator

try:
    # Py3.10+
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from typing import Mapping
from qai_appbuilder import QNNConfig, QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile
import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TensorInfo(Mapping[str, Any]):
    """Minimal ONNX Runtime-like tensor metadata.

    ONNX Runtime's `InferenceSession.get_inputs()` returns a list of `NodeArg`
    objects with attributes like `.name`, `.shape`, and `.type`. Some scripts
    rely on `.name` attribute access (not `['name']`).

    This wrapper keeps compatibility with both attribute and mapping-style
    access.
    """

    name: str
    shape: Optional[List[int]] = None
    type: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        if key in {"name", "shape", "type"}:
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        # Stable key order similar to common dict representations.
        yield "name"
        yield "shape"
        yield "type"

    def __len__(self) -> int:
        return 3


class QNNModelWrapper(QNNContext):
    """Internal wrapper for QNN models with I/O reordering support."""
    
    def __init__(self, model_name: str, model_path: str):
        # Search for QNN model file with platform-specific extensions
        model_path = self._find_qnn_model_file(model_path)
        
        model_path = os.path.abspath(model_path)
        super().__init__(model_name, model_path)
        
        # Initialize I/O config
        self.io_config = None
        
        # Check for YAML config file
        yaml_paths = [os.path.splitext(model_path)[0] + ".yaml", model_name + ".yaml"]
        print("model",model_name, yaml_paths)
        for yaml_path in yaml_paths:
            if os.path.exists(yaml_path):
                try:
                    with open(yaml_path, 'r') as f:
                        self.io_config = yaml.safe_load(f)
                    logger.info(f"Loaded IO config from {yaml_path}")
                    print(f"Loaded IO config from {yaml_path}")
                    break
                except Exception as e:
                    logger.warning(f"Failed to load IO config from {yaml_path}: {e}")
                    print(f"Failed to load IO config from {yaml_path}: {e}")
    
    def _find_qnn_model_file(self, model_path: str) -> str:
        """
        Find QNN model file with platform-specific detection.
        
        Search priority:
        1. If path ends with .bin/.so/.dll, use as-is if exists
        2. Search for .bin (universal)
        3. Search for platform-specific extensions:
           - Linux: .so.bin, .so, .onnx.so.bin, .onnx.so
           - Windows: .dll.bin, .dll, .onnx.dll.bin, .onnx.dll
        
        Args:
            model_path: Original model path
            
        Returns:
            Path to QNN model file
        """
        import platform
        
        # Determine platform
        system = platform.system()
        
        # If already has QNN extension and exists, use it
        # Check platform-specific extensions
        if system == 'Windows':
            valid_extensions = ('.bin', '.dll','.dlc')
        else:  # Linux, Darwin (macOS), etc.
            valid_extensions = ('.bin', '.so','.dlc')
        
        if model_path.endswith(valid_extensions) and os.path.exists(model_path):
            logger.info(f"Using provided model file: {model_path}")
            return model_path
        
        # Determine platform-specific extensions for search
        if system == 'Windows':
            platform_extensions = [
                '.dll.bin',
                '.dll',
                '.onnx.dll.bin',
                '.onnx.dll',
                '.dlc',
                '.dlc.bin'

            ]
        else:  # Linux, Darwin (macOS), etc.
            platform_extensions = [
                '.so.bin',
                '.so',
                '.onnx.so.bin',
                '.onnx.so',
                '.dlc',
                '.dlc.bin'
            ]
        
        # Build search list with priority
        candidates = [
            # Universal binary format (highest priority)
            model_path + '.bin',
        ]
        
        # Add platform-specific extensions
        for ext in platform_extensions:
            candidates.append(model_path + ext)
        
        # If model_path ends with .onnx, also search with base path (without .onnx)
        # and with lib prefix
        if model_path.lower().endswith('.onnx'):
            base_path = os.path.splitext(model_path)[0]
            base_name = os.path.basename(base_path)
            dir_name = os.path.dirname(base_path)
            
            # Add base path searches
            candidates.append(base_path + '.bin')
            for ext in platform_extensions:
                candidates.append(base_path + ext)
            print(candidates)
            
            # Add lib prefix searches
            lib_base_path = os.path.join(dir_name, 'lib' + base_name) if dir_name else 'lib' + base_name
            candidates.append(lib_base_path + '.bin')
            for ext in platform_extensions:
                candidates.append(lib_base_path + ext)
        
        # Search for first existing file
        for candidate in candidates:
            if os.path.exists(candidate):
                logger.info(f"Found QNN model file: {candidate} (searched from {model_path})")
                return candidate
        
        # If nothing found, log warning and return original path
        logger.warning(f"No QNN model file found for '{model_path}'")
        logger.warning(f"Searched: {candidates[:5]}... (and {len(candidates)-5} more)")
        logger.warning(f"Attempting to use original path. This may fail if not a valid QNN model.")
        return model_path
        
    def _reorder_tensors(self, tensors, current_names, desired_names, tag=""):
        """Reorder tensors based on name mapping."""
        if len(current_names) == len(tensors):
            tensor_map = {name: tensor for name, tensor in zip(current_names, tensors)}
            try:
                return [tensor_map[name] for name in desired_names]
            except KeyError as e:
                logger.warning(f"{tag} name {e} not found in available names: {current_names}. Returning original order.")
        else:
            logger.warning(f"Number of {tag} names ({len(current_names)}) does not match tensors ({len(tensors)})")
        return tensors
    
    def Inference(self, input_tensors: list):
        """Run inference with optional I/O reordering."""
        if self.io_config is not None and "input" in self.io_config:
            if isinstance(self.io_config["input"], list):
                input_tensors = self._reorder_tensors(
                    input_tensors, self.io_config["input"], 
                    self.getInputName(), "Input"
                )
        
        output_tensors = super().Inference(input_tensors)
        
        if self.io_config is not None and "output" in self.io_config:
            if isinstance(self.io_config["output"], list):
                output_tensors = self._reorder_tensors(
                    output_tensors, self.getOutputName(), 
                    self.io_config["output"], "Output"
                )
        
        return output_tensors


class SessionOptions:
    """ONNX Runtime-compatible SessionOptions for QNN configuration."""
    
    def __init__(self):
        self.log_severity_level = 2  # Warning level
        self.log_verbosity_level = 0
        self.enable_profiling = False
        self.optimized_model_filepath = ""
        self.graph_optimization_level = 1
        
        # QNN-specific options
        self.qnn_runtime = Runtime.HTP
        self.qnn_libs_dir = ""
        self.qnn_profiling_level = ProfilingLevel.OFF
        
    def set_qnn_runtime(self, runtime: Union[Runtime, str]):
        """Set QNN runtime (HTP, DSP, GPU, CPU)."""
        if isinstance(runtime, str):
            runtime_map = {
                'HTP': Runtime.HTP,
                #'DSP': Runtime.DSP,
                #'GPU': Runtime.GPU,
                'CPU': Runtime.CPU
            }
            runtime_upper = runtime.upper()
            if runtime_upper not in runtime_map:
                logger.warning(f"Unknown runtime '{runtime}'. Valid options: {list(runtime_map.keys())}. Defaulting to HTP.")
            runtime = runtime_map.get(runtime_upper, Runtime.HTP)
        self.qnn_runtime = runtime
        
    def set_qnn_libs_dir(self, path: str):
        """Set QNN libraries directory path."""
        self.qnn_libs_dir = path
        
    def enable_qnn_profiling(self, enable: bool = True):
        """Enable or disable QNN profiling."""
        self.qnn_profiling_level = ProfilingLevel.BASIC if enable else ProfilingLevel.OFF


class InferenceSession:
    """
    ONNX Runtime-compatible InferenceSession that uses qai_appbuilder internally.
    
    This class mimics the ONNX Runtime InferenceSession API while using QNN for acceleration.
    
    Example:
        >>> import qai_onnxruntime as ort
        >>> 
        >>> # Configure QNN
        >>> sess_options = ort.SessionOptions()
        >>> sess_options.set_qnn_runtime('HTP')
        >>> sess_options.set_qnn_libs_dir('/path/to/qnn/libs')
        >>> 
        >>> # Create session
        >>> session = ort.InferenceSession('model.bin', sess_options)
        >>> 
        >>> # Run inference
        >>> input_data = {'input': np.random.randn(1, 3, 224, 224).astype(np.float32)}
        >>> outputs = session.run(None, input_data)
    """
    
    _qnn_initialized = False
    
    def __init__(self, model_path: str, sess_options: Optional[SessionOptions] = None, 
                 providers: Optional[List[str]] = None):
        """
        Initialize an inference session.
        
        Args:
            model_path: Path to the QNN model (.bin or .so file)
            sess_options: Session configuration options
            providers: Execution providers (for ONNX Runtime compatibility, ignored)
        """
        if sess_options is None:
            sess_options = SessionOptions()
        
        self.model_path = model_path
        self.sess_options = sess_options
        
        # Initialize QNN config if not already done
        if not InferenceSession._qnn_initialized:
            log_level_map = {
                0: LogLevel.ERROR,
                1: LogLevel.WARN,
                2: LogLevel.WARN,
                3: LogLevel.INFO,
                4: LogLevel.DEBUG
            }
            log_level = log_level_map.get(sess_options.log_severity_level, LogLevel.DEBUG)
            
            QNNConfig.Config(
                sess_options.qnn_libs_dir,
                sess_options.qnn_runtime,
                log_level,
                sess_options.qnn_profiling_level
            )
            InferenceSession._qnn_initialized = True
            logger.info("QNN environment initialized")
        
        # Create QNN model wrapper
        model_name = os.path.splitext(os.path.basename(model_path))[0]
        self._model = QNNModelWrapper(model_name, model_path)
        
        # Cache input/output metadata
        self._input_names = self._model.getInputName()
        self._output_names = self._model.getOutputName()
        
        logger.info(f"Loaded model: {model_path}")
        logger.info(f"Input names: {self._input_names}")
        logger.info(f"Output names: {self._output_names}")
    
    def run(self, output_names: Optional[List[str]], input_feed: Dict[str, np.ndarray],
            run_options: Optional[Any] = None) -> List[np.ndarray]:
        """
        Run inference on the model.
        
        Args:
            output_names: Names of outputs to compute (None for all outputs)
            input_feed: Dictionary mapping input names to numpy arrays
            run_options: Run options (for ONNX Runtime compatibility, ignored)
            
        Returns:
            List of output numpy arrays
        """
        # Convert input dict to list in correct order
        input_tensors = []
        for name in self._input_names:
            if name not in input_feed:
                raise ValueError(f"Missing input: {name}. Expected inputs: {self._input_names}")
            input_tensors.append(input_feed[name])
        
        # Set performance profile
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
        
        try:
            # Run inference
            output_tensors = self._model.Inference(input_tensors)
            
            # Filter outputs if specific names requested
            if output_names is not None:
                output_map = {name: tensor for name, tensor in zip(self._output_names, output_tensors)}
                output_tensors = [output_map[name] for name in output_names]
            
            return output_tensors
        finally:
            # Release performance profile
            PerfProfile.RelPerfProfileGlobal()
    
    def get_inputs(self) -> List[TensorInfo]:
        """Get input metadata (ONNX Runtime compatible).

        ONNX Runtime returns `NodeArg` objects with a `.name` attribute.
        Returning `TensorInfo` preserves that API while also supporting
        mapping-style access.
        """
        return [TensorInfo(name=name) for name in self._input_names]

    def get_outputs(self) -> List[TensorInfo]:
        """Get output metadata (ONNX Runtime compatible)."""
        return [TensorInfo(name=name) for name in self._output_names]

    def get_input_names(self) -> List[str]:
        """Get list of input names."""
        return self._input_names
    
    def get_output_names(self) -> List[str]:
        """Get list of output names."""
        return self._output_names
    
    def __del__(self):
        """Clean up resources."""
        if hasattr(self, '_model'):
            del self._model


def get_available_providers() -> List[str]:
    """
    Get available execution providers (ONNX Runtime compatible).
    
    Returns:
        List of provider names
    """
    return ['QNNExecutionProvider', 'CPUExecutionProvider']


def get_device() -> str:
    """
    Get current device.
    
    Returns:
        Device string
    """
    return 'QNN'


# Convenience function for simple use cases
def run_inference(model_path: str, input_data: Union[np.ndarray, Dict[str, np.ndarray]], 
                  qnn_libs_dir: str = "", runtime: str = "HTP") -> List[np.ndarray]:
    """
    Convenience function to run inference with minimal setup.
    
    Args:
        model_path: Path to QNN model
        input_data: Input data as numpy array or dict of arrays
        qnn_libs_dir: Path to QNN libraries (empty for default)
        runtime: QNN runtime to use ('HTP', 'DSP', 'GPU', 'CPU')
        
    Returns:
        List of output numpy arrays
        
    Example:
        >>> outputs = run_inference('model.bin', input_array)
    """
    sess_options = SessionOptions()
    sess_options.set_qnn_runtime(runtime)
    sess_options.set_qnn_libs_dir(qnn_libs_dir)
    
    session = InferenceSession(model_path, sess_options)
    
    # Handle single array input
    if isinstance(input_data, np.ndarray):
        input_names = session.get_input_names()
        if len(input_names) != 1:
            raise ValueError(f"Model expects {len(input_names)} inputs, but single array provided")
        input_data = {input_names[0]: input_data}
    
    return session.run(None, input_data)
