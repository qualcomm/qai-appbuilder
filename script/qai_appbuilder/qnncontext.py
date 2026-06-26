
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
        appbuilder.set_perf_profile(perf_profile)

    @staticmethod
    def RelPerfProfileGlobal():
        """
        Release the perf profile which set by function SetPerfProfileGlobal().
        """
        global g_runtime
        if g_runtime == Runtime.GPU:
            return
        appbuilder.rel_perf_profile()


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

        self._validate_model_path()
        backend_lib_path, system_lib_path = self._resolve_lib_paths(backend_lib_path, system_lib_path)

        self.m_context = appbuilder.QNNContext(model_name, model_path, backend_lib_path, system_lib_path,
                                              is_async, input_data_type, output_data_type, deviceID, coreIdsStr)
        _register_context(self)

    #@timer
    def Inference(self, input, perf_profile=PerfProfile.DEFAULT, graphIndex=0):
        return self._inference_and_reshape(
            input,
            lambda _in: self.m_context.Inference(_in, perf_profile, graphIndex, self.input_data_type, self.output_data_type)
        )


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
            # print(f"Input data size {total_input_bytes} exceeds share memory size {shareMemory.share_memory_size}, you need to create a larger share memory for model {self.model_name} @ process {self.proc_name}.")

        return self._inference_and_reshape(
            input,
            lambda _in: self.m_context.Inference(shareMemory.m_memory, _in, perf_profile, graphIndex,
                                                 self.input_data_type, self.output_data_type)
        )


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
