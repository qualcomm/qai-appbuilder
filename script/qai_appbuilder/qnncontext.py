#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

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

g_base_path = os.path.dirname(os.path.abspath(__file__))
g_base_path = os.getenv('PATH') + ";" + g_base_path + ";"

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
            input[i].reshape(-1,)
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            print(f"reshape {input[i]} error:{e}")
    return input
                
def reshape_output(output, outputshape_list):
    for i in range(len(output)):
        try:
            output[i].reshape(outputshape_list[i])
        except (ValueError, TypeError, IndexError) as e:
            print(f"reshape {outputshape_list[i]} error:{e}")
    return output
                
class LogLevel():
    ERROR = 1
    WARN = 2
    INFO = 3
    VERBOSE = 4
    DEBUG = 5
    
    @staticmethod
    def SetLogLevel(log_level, log_path):
        appbuilder.set_log_level(log_level, log_path)

class ProfilingLevel():
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

class Runtime():
    """Available runtimes for model execution on Qualcomm harwdware."""
    CPU = "Cpu"
    HTP = "Htp"

class PerfProfile():
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
        global setting will be overwrited.
        """
        appbuilder.set_perf_profile(perf_profile)

    @staticmethod
    def RelPerfProfileGlobal():
        """
        Release the perf profile which set by function SetPerfProfileGlobal().
        """
        appbuilder.rel_perf_profile()


class QNNConfig():
    """Config QNN SDK libraries path, runtime(CPU/HTP), log leverl, profiling level."""

    @staticmethod
    def Config(qnn_lib_path: str = "None",
               runtime : str = Runtime.HTP,
               log_level : int = LogLevel.ERROR,
               profiling_level : int = ProfilingLevel.OFF,
               log_path : str = "None"
    ):
        global g_backend_lib_path, g_system_lib_path

        if not os.path.exists(qnn_lib_path):
            base_path = os.path.dirname(os.path.abspath(__file__))
            qnn_lib_path = base_path + "/libs"

        if (qnn_lib_path != "None"):
            g_backend_lib_path = qnn_lib_path + PATH_SLASH + QNN_LIB_PRE + "Qnn" + runtime + QNN_LIB_EXT
            g_system_lib_path = qnn_lib_path + PATH_SLASH + QNN_SYSTEM_LIB

        if not os.path.exists(g_backend_lib_path):
            raise ValueError(f"backend library does not exist: {g_backend_lib_path}")

        if not os.path.exists(g_system_lib_path):
            raise ValueError(f"system library does not exist: {g_system_lib_path}")

        LogLevel.SetLogLevel(log_level, log_path)
        ProfilingLevel.SetProfilingLevel(profiling_level)


class QNNLoraContext:
    """High-level Python wrapper for a AppBuilder model."""
    def __init__(self,
                model_name: str = "None",
                model_path: str = "None",
                backend_lib_path: str = "None",
                system_lib_path: str = "None",
                lora_adapters = None,
                runtime : str = Runtime.HTP,
                is_async: bool = False
    ) -> None:
        """Load a QNN model from `model_path`

        Args:
            model_name: name of the model
            model_path (str): model path
            bin_files (str) : List of LoraAdapter class objects. 
        """
        self.model_path = model_path
        self.lora_adapters = lora_adapters
        
        m_lora_adapters = []
        for adapter in lora_adapters:
            m_lora_adapters.append(adapter.m_adapter)

        if self.model_path == "None":
            raise ValueError("model_path must be specified!")

        if not os.path.exists(self.model_path):
            raise ValueError(f"Model path does not exist: {self.model_path}") 

        if (backend_lib_path == "None"):
            backend_lib_path = g_backend_lib_path
        if (system_lib_path == "None"):
            system_lib_path = g_system_lib_path
       
        self.m_context = appbuilder.QNNContext(model_name, model_path,
                                               backend_lib_path, system_lib_path,
                                               m_lora_adapters, is_async)

    # issue#24
    def getInputShapes(self, ):
        return self.m_context.getInputShapes()

    def getOutputShapes(self, ):
        return self.m_context.getOutputShapes()

    def getInputDataType(self, ):
        return self.m_context.getInputDataType()

    def getOutputDataType(self, ):
        return self.m_context.getOutputDataType()
    #@timer
    def Inference(self, input, perf_profile = PerfProfile.DEFAULT, graphIndex = 0):
        input= reshape_input(input)         
        output = self.m_context.Inference(input, perf_profile, graphIndex)
        outputshape_list = self.getOutputShapes()
        output = reshape_output(output, outputshape_list)

        return output
    
    def apply_binary_update(self, lora_adapters=None):
        self.lora_adapters = lora_adapters
        
        m_lora_adapters = []
        for adapter in lora_adapters:
            m_lora_adapters.append(adapter.m_adapter)
            
        self.m_context.ApplyBinaryUpdate(m_lora_adapters)


    #@timer
    def __del__(self):
        if hasattr(self, "m_context") and self.m_context is not None:
            del(self.m_context)
            m_context = None


class QNNContext:
    """High-level Python wrapper for a AppBuilder model."""
    def __init__(self,
                model_name: str = "None",
                model_path: str = "None",
                backend_lib_path: str = "None",
                system_lib_path: str = "None",
                runtime: str = Runtime.HTP,
                is_async: bool = False
    ) -> None:
        """Load a QNN model from `model_path`

        Args:
            model_path (str): model path
        """
        self.model_path = model_path

        if self.model_path == "None":
            raise ValueError("model_path must be specified!")

        if not os.path.exists(self.model_path):
            raise ValueError(f"Model path does not exist: {self.model_path}")

        if (backend_lib_path == "None"):
            backend_lib_path = g_backend_lib_path
        if (system_lib_path == "None"):
            system_lib_path = g_system_lib_path

        self.m_context = appbuilder.QNNContext(model_name, model_path, backend_lib_path, system_lib_path, is_async)

    # issue#24
    def getInputShapes(self, ):
        return self.m_context.getInputShapes()

    def getOutputShapes(self, ):
        return self.m_context.getOutputShapes()

    def getInputDataType(self, ):
        return self.m_context.getInputDataType()

    def getOutputDataType(self, ):
        return self.m_context.getOutputDataType()

    #@timer
    def Inference(self, input, perf_profile = PerfProfile.DEFAULT, graphIndex = 0):
        input = reshape_input(input) 

        output = self.m_context.Inference(input, perf_profile, graphIndex)

        outputshape_list = self.getOutputShapes()
        output = reshape_output(output, outputshape_list)

        return output

    #@timer
    def __del__(self):
        if hasattr(self, "m_context") and self.m_context is not None:
            del(self.m_context)
            m_context = None


class QNNContextProc:
    """High-level Python wrapper for a AppBuilder model. Load and run the model in separate process."""
    def __init__(self,
                 model_name: str = "None",
                 proc_name: str = "None",
                 model_path: str = "None",
                 backend_lib_path: str = "None",
                 system_lib_path: str = "None",
                 runtime : str = Runtime.HTP,
                 is_async: bool = False
    ) -> None:
        """Load a QNN model from `model_path`

        Args:
            model_path (str): model path
        """
        self.model_path = model_path
        self.proc_name = proc_name

        if self.proc_name == "None":
            raise ValueError("proc_name must be specified!")

        if self.model_path == "None":
            raise ValueError("model_path must be specified!")

        if not os.path.exists(self.model_path):
            raise ValueError(f"Model path does not exist: {self.model_path}")

        if (backend_lib_path == "None"):
            backend_lib_path = g_backend_lib_path
        if (system_lib_path == "None"):
            system_lib_path = g_system_lib_path

        os.putenv('PATH', g_base_path)
        self.m_context = appbuilder.QNNContext(model_name, proc_name, model_path, backend_lib_path, system_lib_path, is_async)

    # issue#24
    def getInputShapes(self, ):
        return self.m_context.getInputShapes()

    def getOutputShapes(self, ):
        return self.m_context.getOutputShapes()

    def getInputDataType(self, ):
        return self.m_context.getInputDataType()

    def getOutputDataType(self, ):
        return self.m_context.getOutputDataType()

    #@timer
    def Inference(self, shareMemory, input, perf_profile = PerfProfile.DEFAULT, graphIndex = 0):
        input = reshape_input(input)
                
        output = self.m_context.Inference(shareMemory.m_memory, input, perf_profile, graphIndex)

        outputshape_list = self.getOutputShapes()
        output = reshape_output(output, outputshape_list)

        return output
        
    #@timer
    def __del__(self):
        if hasattr(self, "m_context") and self.m_context is not None:
            del(self.m_context)
            m_context = None


class QNNShareMemory:
    """High-level Python wrapper for a AppBuilder model."""
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

    #@timer
    def __del__(self):
        if hasattr(self, "m_memory") and self.m_memory is not None:
            del(self.m_memory)
            m_memory = None


class LoraAdapter:   # this will just hold data
    m_adapter = None
    
    def __init__(self, graph_name, lora_file_paths):
        self.m_adapter = appbuilder.LoraAdapter(graph_name, lora_file_paths)  # cpp object
