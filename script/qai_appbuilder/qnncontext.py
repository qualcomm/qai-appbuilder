
#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""
QNN context wrappers for qai_appbuilder.

Runtime libraries (QnnHtp.dll, QnnSystem.dll, QnnHtpV*Stub.dll,
libQnnHtpV*Skel.so, ...) are bundled inside this package's ``libs/``
directory and are resolved automatically at runtime. Callers normally do
not need to specify a libs path; passing one explicitly is also supported
for advanced use cases (e.g. pointing at a custom QNN SDK build).
"""
import os
import sys
import functools
import time
import numpy as np
from qai_appbuilder import appbuilder

QNN_SYSTEM_LIB = "QnnSystem.dll"
QNN_LIB_EXT = ".dll"
QNN_LIB_PRE = ""
PATH_SLASH = "\\"
if sys.platform.startswith('linux'):
    QNN_SYSTEM_LIB = "libQnnSystem.so"
    QNN_LIB_EXT = ".so"
    QNN_LIB_PRE = "lib"
    PATH_SLASH = "/"

g_backend_lib_path = "None"
g_system_lib_path = "None"
g_runtime = None
g_base_path = os.path.dirname(os.path.abspath(__file__))
# Prepend/append the package dir to PATH using the platform separator (';' on
# Windows, ':' on Linux) so the spawned QAIAppSvc service binary is found.
_path_env = os.getenv('PATH')
g_base_path = (_path_env if _path_env else "") + os.pathsep + g_base_path + os.pathsep
# ---------------------------------------------------------------------------
# Active-context registry & fork safety (see GitHub issue #109)
# ---------------------------------------------------------------------------
# Python's ``del`` and ``gc.collect()`` provide no deterministic, idempotent
# resource release for the underlying C++ QNN contexts. In addition, on Linux
# ``multiprocessing`` defaults to ``fork()``, which clones all unreleased C++
# context state (HTP device handles, backend library handles, the internal
# model map, the QAIAppSvc IPC channel, ...) into the child process where those
# handles are invalid.
#
# To make lifecycle management safe we:
#   1. Track every live context object in a weak registry.
#   2. Expose a module-level ``release_all()`` that deterministically releases
#      every registered context.
#   3. Register ``release_all()`` as an ``os.register_at_fork(before=...)``
#      handler so the parent's C++ state is drained *before* any fork, leaving
#      the child with a clean slate.
import threading
import weakref

_active_contexts = weakref.WeakSet()
_active_contexts_lock = threading.RLock()


def _register_context(ctx):
    """Register a live context so it can be released deterministically."""
    with _active_contexts_lock:
        _active_contexts.add(ctx)


def _unregister_context(ctx):
    """Remove a context from the active registry (best-effort)."""
    with _active_contexts_lock:
        _active_contexts.discard(ctx)


def release_all():
    """Deterministically release every live QNN/Genie context.

    This drains the active-context registry and calls ``release()`` on each
    registered object. It is safe to call multiple times (each ``release()``
    is idempotent) and is registered as a pre-fork handler so child processes
    never inherit live C++ context state.
    """
    with _active_contexts_lock:
        contexts = list(_active_contexts)
    for ctx in contexts:
        try:
            ctx.release()
        except Exception as e:  # never let cleanup raise
            print(f"[WARN] release_all: failed to release a context: {e}")


# Register the pre-fork handler exactly once. ``register_at_fork`` only exists
# on platforms that support fork (POSIX); guard accordingly.
if hasattr(os, "register_at_fork"):
    try:
        os.register_at_fork(before=release_all)
    except (RuntimeError, ValueError):
        pass


def timer(func):
    @functools.wraps(func)
    def wrapper_timer(*args, **kwargs):
        tic = time.perf_counter()
        value = func(*args, **kwargs)
        toc = time.perf_counter()
        elapsed_time = toc - tic
        print(f"Elapsed time: {elapsed_time:0.4f} seconds")
        return value
    return wrapper_timer


def reshape_input(input):
    for i in range(len(input)):
        try:
            input[i] = input[i].reshape(-1,)
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            print(f"reshape {input[i]} error:{e}")
    return input


def reshape_output(output, outputshape_list):
    for i in range(len(output)):
        try:
            output[i] = output[i].reshape(outputshape_list[i])
        except (ValueError, TypeError, IndexError) as e:
            print(f"reshape {outputshape_list[i]} error:{e}")
    return output

def _onnx_dtype_to_simple(dtype_str: str) -> str:
    s = str(dtype_str or "").lower()
    if "float16" in s:
        return "float16"
    if "float" in s:
        return "float32"
    if "uint64" in s:
        return "uint64"
    if "int64" in s:
        return "int64"
    if "uint32" in s:
        return "uint32"
    if "int32" in s:
        return "int32"
    if "uint16" in s:
        return "uint16"
    if "int16" in s:
        return "int16"
    if "uint8" in s:
        return "uint8"
    if "int8" in s:
        return "int8"
    if "bool" in s:
        return "bool"
    return "float32"


def _onnx_simple_to_np_dtype(dtype_name: str):
    name = str(dtype_name or "").lower()
    mapping = {
        "float16": np.float16,
        "fp16": np.float16,
        "float32": np.float32,
        "fp32": np.float32,
        "float": np.float32,
        "int8": np.int8,
        "uint8": np.uint8,
        "int16": np.int16,
        "uint16": np.uint16,
        "int32": np.int32,
        "uint32": np.uint32,
        "int64": np.int64,
        "uint64": np.uint64,
        "bool": np.bool_,
    }
    return mapping.get(name, np.float32)


class OnnxRuntimeContext:
    """ONNX Runtime backend used when model_path points to *.onnx."""

    def __init__(self, model_name: str, model_path: str, use_cpu: bool):
        import importlib
        import platform

        try:
            # qai_appbuilder.onnxwrapper may have already registered itself as
            # "onnxruntime". For true ONNX execution here, force-load the real
            # onnxruntime package.
            mod = sys.modules.get("onnxruntime")
            if mod is not None and str(getattr(mod, "__name__", "")).startswith("qai_appbuilder.onnxwrapper"):
                sys.modules.pop("onnxruntime", None)
            ort = importlib.import_module("onnxruntime")
        except Exception as e:
            raise ImportError(
                "onnxruntime is required for .onnx model_path in qai_appbuilder.QNNContext"
            ) from e

        self.model_name = model_name
        self.model_path = model_path
        self._provider_mode = "cpu"
        self._provider_options = {}

        ortq = None
        if not use_cpu:
            try:
                ortq = importlib.import_module("onnxruntime_qnn")
            except Exception as e:
                print(f"[OnnxRuntimeContext] Warning: Failed to import onnxruntime_qnn: {e}")
                ortq = None

        self.session = self._create_session(ort, ortq)
        self._inputs = self.session.get_inputs()
        self._outputs = self.session.get_outputs()

    @staticmethod
    def _env_true(name: str, default: str = "0") -> bool:
        v = str(os.environ.get(name, default) or default).strip().lower()
        return v in ("1", "true", "yes", "on")

    @staticmethod
    def _prepend_ld_library_path(path: str) -> None:
        if not path:
            return
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [x for x in cur.split(":") if x]
        if path not in parts:
            os.environ["LD_LIBRARY_PATH"] = path if not cur else f"{path}:{cur}"

    @staticmethod
    def _select_backend_path(ortq):
        backend = str(os.environ.get("QAI_ORTQNN_BACKEND", "htp")).strip().lower()
        if backend == "cpu" and hasattr(ortq, "get_qnn_cpu_path"):
            return ortq.get_qnn_cpu_path()
        if backend == "gpu" and hasattr(ortq, "get_qnn_gpu_path"):
            return ortq.get_qnn_gpu_path()
        return ortq.get_qnn_htp_path()

    def _build_qnn_session(self, ort, ortq, use_context_cache: bool):
        """Build a single QNN/HTP ORT InferenceSession.

        Mirrors yolov8_det-npu.py::create_session():
          1. Register the onnxruntime_qnn EP library (idempotent).
          2. Discover the QNN OrtEpDevice (NPU/HTP).
          3. Configure SessionOptions with backend_path (+ fp16) and bind the
             QNN device via add_provider_for_devices.

        When ``use_context_cache`` is True we additionally ask the QNN EP to
        compile the graph into an EPContext (.onnx_ctx.onnx) binary, which is
        the documented workaround for the
        "Conv ... com.ms.internal.nhwc ... graph is now invalid" layout-transform
        failure seen with some onnxruntime_qnn builds on raw float32 models.
        """
        # Check if ortq has the required methods
        if not hasattr(ortq, 'get_ep_name'):
            raise RuntimeError(
                "onnxruntime_qnn module does not have 'get_ep_name' method. "
                "This may indicate an incompatible version of onnxruntime_qnn. "
                "Please ensure onnxruntime_qnn is properly installed."
            )
        if not hasattr(ortq, 'get_library_path'):
            raise RuntimeError(
                "onnxruntime_qnn module does not have 'get_library_path' method. "
                "This may indicate an incompatible version of onnxruntime_qnn. "
                "Please ensure onnxruntime_qnn is properly installed."
            )

        # Register the QNN EP library (tolerate repeated registration).
        try:
            ort.register_execution_provider_library(
                ortq.get_ep_name(),
                ortq.get_library_path(),
            )
        except Exception as _e:
            if "already registered" not in str(_e):
                raise

        # Discover QNN OrtEpDevice (prefer the NPU device when available).
        qnn_devices = [
            d for d in ort.get_ep_devices()
            if getattr(d, "ep_name", "") == ortq.get_ep_name()
        ]
        if not qnn_devices:
            raise RuntimeError("No QNN OrtEpDevice discovered after registration")

        def _is_npu(dev):
            try:
                return "NPU" in str(getattr(dev, "device").type).upper()
            except Exception:
                return False

        npu_devices = [d for d in qnn_devices if _is_npu(d)]
        chosen_device = npu_devices[0] if npu_devices else qnn_devices[0]

        backend_path = self._select_backend_path(ortq)
        self._provider_options = {
            "backend_path": backend_path,
        }

        if self._env_true("QAI_ORTQNN_ENABLE_HTP_FP16", "1"):
            self._provider_options["enable_htp_fp16_precision"] = "1"

        so = ort.SessionOptions()
        # NOTE: We intentionally do NOT disable ORT-level graph optimizations or
        # set enable_htp_graph_finalization_optimization_level here. Doing so
        # prevents ORT's layout transformer from reconciling the
        # com.ms.internal.nhwc nodes the QNN EP inserts. Leaving the defaults in
        # place mirrors the proven yolov8_det-npu.py path.
        so.add_provider_for_devices([chosen_device], self._provider_options)
        print(f"[OnnxRuntimeContext]use_context_cache: {use_context_cache}")
        if use_context_cache:
            # Compile/serialize the QNN HTP graph into an EPContext binary that
            # sits next to the model. On subsequent runs ORT loads the prebuilt
            # context instead of re-partitioning the float32 graph.
            #
            # ORT derives the context file name as "<model_stem>_ctx.onnx" by
            # default; we set it explicitly so we can detect/reuse it.
            ctx_path = os.path.splitext(self.model_path)[0] + "_ctx.onnx"
            print(f"[OnnxRuntimeContext]ctx_path: {ctx_path}")
            if os.path.exists(ctx_path):
                # A previously-compiled context exists: load it directly. This
                # reuses the precompiled graph and avoids the
                # "EP context model ... exists already" generation error.
                load_so = ort.SessionOptions()
                load_so.add_provider_for_devices([chosen_device], self._provider_options)
                print(f"[OnnxRuntimeContext]Loading cached context from: {ctx_path}")
                return ort.InferenceSession(ctx_path, load_so)
            # ctx file does not exist yet: ask QNN EP to compile and save it.
            so.add_session_config_entry("ep.context_enable", "1")
            so.add_session_config_entry("ep.context_file_path", ctx_path)
            print(f"[OnnxRuntimeContext]Generating context cache at: {ctx_path}")
            return ort.InferenceSession(self.model_path, so)
        return ort.InferenceSession(self.model_path, so)

    def _create_session(self, ort, ortq):
        """Create an ORT InferenceSession, preferring the QNN HTP (NPU) backend.

        Strategy:
          1. Try the direct QNN/HTP session (no context cache).
          2. If that fails with the known layout-transform error, retry once
             using the EPContext precompile workaround.
          3. Fall back to CPUExecutionProvider on any remaining failure.
        """
        if ortq is None:
            self._provider_mode = "cpu"
            self._provider_options = {}
            print(f"[OnnxRuntimeContext] Running on CPU. ")
            return ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])

        # Attempt 1: direct QNN/HTP session.
        try:
            sess = self._build_qnn_session(ort, ortq, use_context_cache=True)
            self._provider_mode = "qnn-htp"
            print(f"[OnnxRuntimeContext]Running on HTP (NPU). "
                  f"Active providers: {sess.get_providers()}")
            return sess
        except Exception as e:
            err = str(e)
            print(f"[OnnxRuntimeContext] Direct QNN/HTP session failed: {err}")

            # Check for x86 emulation errors on ARM64 systems
            if "0xc000026f" in err or "x86 emulation" in err.lower() or "internal error" in err.lower():
                print("[OnnxRuntimeContext] ERROR: x86 emulation subsystem error detected!")
                print("[OnnxRuntimeContext] This may indicate an incompatible onnxruntime_qnn build")
                print("[OnnxRuntimeContext] for your ARM64 system (Windows on Snapdragon).")
                print("[OnnxRuntimeContext] Falling back to CPU execution.")
                self._provider_mode = "cpu"
                self._provider_options = {}
                return ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])

            # Attempt 2: EPContext precompile workaround for the layout-transform
            # bug ("com.ms.internal.nhwc ... graph is now invalid").
            if ("com.ms.internal.nhwc" in err or "graph is now invalid" in err) and \
               self._env_true("QAI_ORTQNN_TRY_CONTEXT_CACHE", "1"):
                try:
                    print("[OnnxRuntimeContext] Retrying with EPContext precompile "
                          "(ep.context_file_path) workaround ...")
                    sess = self._build_qnn_session(ort, ortq, use_context_cache=True)
                    self._provider_mode = "qnn-htp"
                    print(f"[OnnxRuntimeContext]Running on HTP (NPU) via EPContext. "
                          f"Active providers: {sess.get_providers()}")
                    return sess
                except Exception as e2:
                    print(f"[OnnxRuntimeContext] EPContext workaround also failed: {e2}")
                    print("[OnnxRuntimeContext] This onnxruntime_qnn build cannot run "
                          "this raw float32 ONNX graph on the HTP. Use a QNN-optimized "
                          "(e.g. Olive QnnPreprocess) or quantized ONNX model, or a "
                          "compiled .bin model, to execute on the NPU.")

        # Fallback: CPU.
        self._provider_mode = "cpu"
        self._provider_options = {}
        sess = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        print(f"[OnnxRuntimeContext]Running on CPU. "
              f"Active providers: {sess.get_providers()}")
        return sess

    def _shape_list(self, shape):
        out = []
        for d in list(shape or []):
            try:
                out.append(int(d))
            except Exception:
                out.append(-1)
        return out

    def _prepare_input(self, value, expected_shape, expected_dtype):
        arr = np.asarray(value)

        target_dtype = _onnx_simple_to_np_dtype(expected_dtype)
        if arr.dtype != target_dtype:
            arr = arr.astype(target_dtype, copy=False)

        if arr.ndim == 1 and expected_shape:
            concrete = [d for d in expected_shape if isinstance(d, int) and d > 0]
            if len(concrete) == len(expected_shape):
                expected_size = 1
                for d in concrete:
                    expected_size *= d
                if expected_size == arr.size:
                    arr = arr.reshape(expected_shape)

        return np.ascontiguousarray(arr)

    def Inference(self, input, *args):
        if isinstance(input, dict):
            feed = {}
            for i in self._inputs:
                if i.name not in input:
                    raise ValueError(f"Missing input: {i.name}")
                shp = self._shape_list(i.shape)
                dt = _onnx_dtype_to_simple(i.type)
                feed[i.name] = self._prepare_input(input[i.name], shp, dt)
        else:
            if len(input) != len(self._inputs):
                raise ValueError(
                    f"Input count mismatch: got {len(input)}, expected {len(self._inputs)}"
                )
            feed = {}
            for idx, i in enumerate(self._inputs):
                shp = self._shape_list(i.shape)
                dt = _onnx_dtype_to_simple(i.type)
                feed[i.name] = self._prepare_input(input[idx], shp, dt)

        return self.session.run([o.name for o in self._outputs], feed)

    def getInputShapes(self):
        return [self._shape_list(i.shape) for i in self._inputs]

    def getOutputShapes(self):
        return [self._shape_list(o.shape) for o in self._outputs]

    def getInputDataType(self):
        return [_onnx_dtype_to_simple(i.type) for i in self._inputs]

    def getOutputDataType(self):
        return [_onnx_dtype_to_simple(o.type) for o in self._outputs]

    def getGraphName(self):
        return self.model_name

    def getInputName(self):
        return [i.name for i in self._inputs]

    def getOutputName(self):
        return [o.name for o in self._outputs]

    def getProfilingEvent(self, eventType):
        return {}

    def getProviderMode(self):
        """Return 'qnn-htp' when running on the QNN/HTP NPU, or 'cpu'."""
        return self._provider_mode

    def release(self):
        """Release the underlying ORT InferenceSession (idempotent)."""
        sess = getattr(self, "session", None)
        if sess is not None:
            self.session = None
            del sess

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


class LogLevel:
    ERROR = 1
    WARN = 2
    INFO = 3
    VERBOSE = 4
    DEBUG = 5

    @staticmethod
    def SetLogLevel(log_level, log_path):
        appbuilder.set_log_level(log_level, log_path)


class ProfilingLevel:
    """
    file:///C:/Qualcomm/AIStack/QNN/2.19.0.240124/docs/QNN/general/htp/htp_backend.html?highlight=rpc_control_latency#qnn-htp-profiling
    """
    OFF = 0
    BASIC = 1
    DETAILED = 2
    INVALID = 3

    @staticmethod
    def SetProfilingLevel(profiling_level):
        appbuilder.set_profiling_level(profiling_level)


class Runtime:
    """Available runtimes for model execution on Qualcomm hardware."""
    CPU = "Cpu"
    HTP = "Htp"
    GPU = "Gpu"


class DataType:
    """Available runtimes for model execution on Qualcomm hardware."""
    FLOAT = "float"
    NATIVE = "native"


class PerfProfile:
    """
    Set the HTP perf profile.
    file:///C:/Qualcomm/AIStack/QNN/2.19.0.240124/docs/QNN/general/htp/htp_backend.html?highlight=rpc_control_latency#qnn-htp-performance-infrastructure-api
    """
    DEFAULT             = "default"     # not change the perf profile.
    HIGH_PERFORMANCE    = "high_performance"
    BURST               = "burst"

    @staticmethod
    def SetPerfProfileGlobal(perf_profile):
        """
        Set the perf profile globally. We can set HTP to 'burst' and keep it for running inference several times, the use RelPerfProfileGlobal to reset it.
        You should keep the 'perf_profile' parameter of function 'Inference()' as 'PerfProfile.DEFAULT' for the class QNNContext & QNNContextProc. If not, this
        global setting will be overwritten.
        """
        global g_runtime
        if g_runtime == Runtime.GPU:
            return
        if g_runtime is None:
            return
        try:
            appbuilder.set_perf_profile(perf_profile)
        except Exception as e:
            print(f"[WARN] Failed to set perf profile: {e}")

    @staticmethod
    def RelPerfProfileGlobal():
        """
        Release the perf profile which set by function SetPerfProfileGlobal().
        """
        global g_runtime
        if g_runtime == Runtime.GPU:
            return
        if g_runtime is None:
            return
        try:
            appbuilder.rel_perf_profile()
        except Exception as e:
            print(f"[WARN] Failed to release perf profile: {e}")


class QNNConfig:
    """Config QNN SDK libraries path, runtime(CPU/HTP/GPU), log level, and profiling level.

    QNN SDK libraries are bundled with the ``qai_appbuilder`` package and
    resolved automatically from the package's ``libs/`` directory, so
    ``qnn_lib_path`` is optional in normal usage.
    """

    @staticmethod
    def Config(runtime: str = Runtime.HTP,
               log_level: int = LogLevel.ERROR,
               profiling_level: int = ProfilingLevel.OFF,
               log_path: str = "None"
               ):
        """Configure the QNN runtime.

        Parameters
        ----------
        runtime : str
            One of the values from :class:`Runtime` (e.g. ``Runtime.HTP``).
        log_level : int
            Log verbosity, see :class:`LogLevel`.
        profiling_level : int
            Profiling verbosity, see :class:`ProfilingLevel`.
        log_path : str
            Optional log file path; ``"None"`` disables file logging.
        """
        global g_backend_lib_path, g_system_lib_path, g_runtime
        g_runtime = runtime
        # Fall back to the libs bundled with this package when no valid
        # explicit path is supplied.
        qnn_lib_path = "None"
        if qnn_lib_path in (None, "None", "") or not os.path.exists(qnn_lib_path):
            base_path = os.path.dirname(os.path.abspath(__file__))
            qnn_lib_path = os.path.join(base_path, "libs")

        if not sys.platform.startswith("win"):
            ADSP_LIBRARY_PATH = os.environ.get('ADSP_LIBRARY_PATH')
            if ADSP_LIBRARY_PATH is None or len(ADSP_LIBRARY_PATH) < 2:
                os.environ["ADSP_LIBRARY_PATH"] = qnn_lib_path

        if qnn_lib_path != "None":
            if runtime == Runtime.GPU:
                g_backend_lib_path = qnn_lib_path + PATH_SLASH + QNN_LIB_PRE + "QnnGpu" + QNN_LIB_EXT
            else:
                g_backend_lib_path = qnn_lib_path + PATH_SLASH + QNN_LIB_PRE + "Qnn" + runtime + QNN_LIB_EXT
            g_system_lib_path = qnn_lib_path + PATH_SLASH + QNN_SYSTEM_LIB

        if not os.path.exists(g_backend_lib_path):
            raise ValueError(f"backend library does not exist: {g_backend_lib_path}")
        if not os.path.exists(g_system_lib_path):
            raise ValueError(f"system library does not exist: {g_system_lib_path}")

        LogLevel.SetLogLevel(log_level, log_path)
        ProfilingLevel.SetProfilingLevel(profiling_level)


class _QNNContextBase:
    """
    Shared implementation for QNNContext / QNNLoraContext / QNNContextProc:
    - model path validation
    - backend/system lib path resolving
    - common getters (issue#24)
    - inference reshape workflow
    - resource cleanup (deterministic, idempotent release - issue#109)
    """

    # Set True once the underlying C++ context has been released. Guards against
    # repeated release (e.g. via gc.collect()) and operating on a stale context.
    _released = False

    def _validate_model_path(self):
        if self.model_path == "None":
            raise ValueError("model_path must be specified!")
        if not os.path.exists(self.model_path):
            raise ValueError(f"Model path does not exist: {self.model_path}")

    def _resolve_lib_paths(self, backend_lib_path: str, system_lib_path: str):
        if backend_lib_path == "None":
            backend_lib_path = g_backend_lib_path
        if system_lib_path == "None":
            system_lib_path = g_system_lib_path
        return backend_lib_path, system_lib_path

    def _ensure_live(self, op: str):
        """Raise if the context has already been released (issue#109, issue#4)."""
        if getattr(self, "_released", False) or getattr(self, "m_context", None) is None:
            raise RuntimeError(
                f"Cannot perform '{op}': context "
                f"'{getattr(self, 'model_name', '<unknown>')}' has been released."
            )

    def _call_ctx_getter(self, method_name: str):
        self._ensure_live(method_name)
        method = getattr(self.m_context, method_name)
        if hasattr(self, "proc_name"):
            return method(self.proc_name)
        return method()

    # issue#24
    def getInputShapes(self):
        return self._call_ctx_getter("getInputShapes")

    def getOutputShapes(self):
        return self._call_ctx_getter("getOutputShapes")

    def getInputDataType(self):
        return self._call_ctx_getter("getInputDataType")

    def getOutputDataType(self):
        return self._call_ctx_getter("getOutputDataType")

    def getGraphName(self):
        return self._call_ctx_getter("getGraphName")

    def getInputName(self):
        return self._call_ctx_getter("getInputName")

    def getOutputName(self):
        return self._call_ctx_getter("getOutputName")
    
    def getProfilingEvent(self, eventType):
        self._ensure_live("getProfilingEvent")
        return self.m_context.getProfilingEvent(eventType)

    def _inference_and_reshape(self, input, infer_fn):
        self._ensure_live("Inference")
        input = reshape_input(input)
        output = infer_fn(input)
        outputshape_list = self.getOutputShapes()
        output = reshape_output(output, outputshape_list)
        return output

    @property
    def is_released(self) -> bool:
        """True once the underlying C++ context has been released."""
        return getattr(self, "_released", False) or getattr(self, "m_context", None) is None

    def release(self):
        """Deterministically release the underlying C++ context.

        Idempotent: guarded by ``_released`` so repeated calls (including via
        ``gc.collect()`` or ``__del__``) are safe no-ops. After release the
        context must not be used for inference, metadata queries, etc."""
        if getattr(self, "_released", False):
            return
        self._released = True
        m_context = getattr(self, "m_context", None)
        if m_context is not None:
            self.m_context = None
            try:
                del m_context
            except Exception as e:
                print(f"[WARN] Failed to release context "
                      f"'{getattr(self, 'model_name', '<unknown>')}': {e}")
        # Drop our registry entry so release_all() won't revisit us.
        try:
            _unregister_context(self)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


class QNNContext(_QNNContextBase):
    """High-level Python wrapper for a AppBuilder model."""

    def __init__(self,
                 model_name: str = "None",
                 model_path: str = "None",
                 backend_lib_path: str = "None",
                 system_lib_path: str = "None",
                 is_async: bool = False,
                 input_data_type: str = DataType.FLOAT,
                 output_data_type: str = DataType.FLOAT,
                 deviceID: int = 0,
                 coreIdsStr: str = "None"
                 ) -> None:
        """Load a QNN model from `model_path`
        Args:
            model_path (str): model path
        """
        self.model_path = model_path
        self.model_name = model_name
        self.input_data_type = input_data_type
        self.output_data_type = output_data_type
        self._is_onnx_model = str(model_path).lower().endswith(".onnx")

        self._validate_model_path()

        if self._is_onnx_model:
            self.m_context = OnnxRuntimeContext(model_name, model_path, False)
            _register_context(self)
            return

        backend_lib_path, system_lib_path = self._resolve_lib_paths(backend_lib_path, system_lib_path)

        self.m_context = appbuilder.QNNContext(model_name, model_path, backend_lib_path, system_lib_path,
                                              is_async, input_data_type, output_data_type, deviceID, coreIdsStr)
        _register_context(self)

    #@timer
    def Inference(self, input, perf_profile=PerfProfile.DEFAULT, graphIndex=0):
        if self._is_onnx_model:
            self._ensure_live("Inference")
            return self.m_context.Inference(input)

        return self._inference_and_reshape(
            input,
            lambda _in: self.m_context.Inference(_in, perf_profile, graphIndex, self.input_data_type, self.output_data_type)
        )

    def isOnnxModel(self):
        """True when the loaded model is a *.onnx file served by onnxruntime(-qnn)."""
        return self._is_onnx_model

    def getProviderMode(self):
        """For .onnx models return 'qnn-htp' (NPU/HTP) or 'cpu'.
        For .bin/.dlc QNN models return 'qnn'."""
        if self._is_onnx_model:
            return self.m_context.getProviderMode()
        return "qnn"


class QNNContextProc(_QNNContextBase):
    """High-level Python wrapper for a AppBuilder model. Load and run the model in separate process."""

    def __init__(self,
                 model_name: str = "None",
                 proc_name: str = "None",
                 model_path: str = "None",
                 backend_lib_path: str = "None",
                 system_lib_path: str = "None",
                 is_async: bool = False,
                 input_data_type: str = DataType.FLOAT,
                 output_data_type: str = DataType.FLOAT,
                 deviceID: int = 0,
                 coreIdsStr: str = "None"
                 ) -> None:
        """Load a QNN model from `model_path`
        Args:
            model_path (str): model path
        """
        self.model_path = model_path
        self.proc_name = proc_name
        self.input_data_type = input_data_type
        self.output_data_type = output_data_type
        self.model_name = model_name

        if self.proc_name == "None":
            raise ValueError("proc_name must be specified!")
        self._validate_model_path()

        backend_lib_path, system_lib_path = self._resolve_lib_paths(backend_lib_path, system_lib_path)

        # Ensure the package dir is on PATH so the native side can spawn the
        # QAIAppSvc service binary (posix_spawnp / CreateProcess search PATH).
        os.environ['PATH'] = g_base_path
        os.putenv('PATH', g_base_path)
        self.m_context = appbuilder.QNNContext(model_name, proc_name, model_path, backend_lib_path, system_lib_path,
                                              is_async, input_data_type, output_data_type, deviceID, coreIdsStr)
        _register_context(self)

    #@timer
    def Inference(self, shareMemory, input, perf_profile=PerfProfile.DEFAULT, graphIndex=0):
        self._ensure_live("Inference")
        total_input_bytes = sum(arr.nbytes for arr in input)
        if total_input_bytes > shareMemory.share_memory_size:
            raise ValueError(f"Input data size {total_input_bytes} exceeds share memory size {shareMemory.share_memory_size}, you need to create a larger share memory for model {self.model_name} @ process {self.proc_name}.")

        return self._inference_and_reshape(
            input,
            lambda _in: self.m_context.Inference(shareMemory.m_memory, _in, perf_profile, graphIndex,
                                                 self.input_data_type, self.output_data_type)
        )

    def InferenceAsync(self, shareMemory, input, perf_profile=PerfProfile.DEFAULT, graphIndex=0):
        """Launch inference asynchronously on the Svc side.
        Returns a request_id string; the Svc main loop remains unblocked.
        Call InferenceWait(request_id, shareMemory) to collect the outputs."""
        self._ensure_live("InferenceAsync")
        total_input_bytes = sum(arr.nbytes for arr in input)
        if total_input_bytes > shareMemory.share_memory_size:
            raise ValueError(f"Input data size {total_input_bytes} exceeds share memory size.")
        reshaped = reshape_input(list(input))
        return self.m_context.InferenceAsync(shareMemory.m_memory, reshaped, perf_profile, graphIndex,
                                             self.input_data_type, self.output_data_type)

    def InferenceWait(self, request_id, shareMemory):
        """Wait for the async inference identified by request_id and return
        the reshaped output arrays."""
        self._ensure_live("InferenceWait")
        output = self.m_context.InferenceWait(request_id, shareMemory.m_memory, self.output_data_type)
        outputshape_list = self.getOutputShapes()
        return reshape_output(output, outputshape_list)


class QNNLoraContext(_QNNContextBase):
    """High-level Python wrapper for a AppBuilder model."""

    def __init__(self,
                 model_name: str = "None",
                 model_path: str = "None",
                 backend_lib_path: str = "None",
                 system_lib_path: str = "None",
                 lora_adapters=None,
                 is_async: bool = False,
                 input_data_type: str = DataType.FLOAT,
                 output_data_type: str = DataType.FLOAT,
                 deviceID: int = 0,
                 coreIdsStr: str = "None"
                 ) -> None:
        """Load a QNN model from `model_path`
        Args:
            model_name: name of the model
            model_path (str): model path
            bin_files (str) : List of LoraAdapter class objects.
        """
        self.model_path = model_path
        self.lora_adapters = lora_adapters
        self.input_data_type = input_data_type
        self.output_data_type = output_data_type

        # Keep original behavior/order: iterate adapters before validating model_path.
        m_lora_adapters = []
        for adapter in lora_adapters:
            m_lora_adapters.append(adapter.m_adapter)

        self._validate_model_path()
        backend_lib_path, system_lib_path = self._resolve_lib_paths(backend_lib_path, system_lib_path)

        self.m_context = appbuilder.QNNContext(model_name, model_path, backend_lib_path, system_lib_path, m_lora_adapters,
                                              is_async, input_data_type, output_data_type, deviceID, coreIdsStr)
        _register_context(self)

    #@timer
    def Inference(self, input, perf_profile=PerfProfile.DEFAULT, graphIndex=0):
        return self._inference_and_reshape(
            input,
            lambda _in: self.m_context.Inference(_in, perf_profile, graphIndex, self.input_data_type, self.output_data_type)
        )

    def apply_binary_update(self, lora_adapters=None):
        self._ensure_live("apply_binary_update")
        self.lora_adapters = lora_adapters
        m_lora_adapters = []
        for adapter in lora_adapters:
            m_lora_adapters.append(adapter.m_adapter)
        self.m_context.ApplyBinaryUpdate(m_lora_adapters)


class QNNShareMemory:
    """High-level Python wrapper for a AppBuilder model."""

    _released = False

    def __init__(self,
                 share_memory_name: str = "None",
                 share_memory_size: int = 0,
                 ) -> None:
        """Load a QNN model from `model_path`
        Args:
            model_path (str): model path
        """
        self.share_memory_name = share_memory_name
        self.m_memory = appbuilder.ShareMemory(share_memory_name, share_memory_size)
        self.share_memory_size = share_memory_size

    def release(self):
        """Deterministically release the shared memory (idempotent)."""
        if getattr(self, "_released", False):
            return
        self._released = True
        m_memory = getattr(self, "m_memory", None)
        if m_memory is not None:
            self.m_memory = None
            try:
                del m_memory
            except Exception as e:
                print(f"[WARN] Failed to release share memory "
                      f"'{getattr(self, 'share_memory_name', '<unknown>')}': {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    #@timer
    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


class LoraAdapter:  # this will just hold data
    m_adapter = None

    def __init__(self, graph_name, lora_file_paths):
        self.m_adapter = appbuilder.LoraAdapter(graph_name, lora_file_paths)  # cpp object
