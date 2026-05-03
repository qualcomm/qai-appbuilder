#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import os
import sys
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("qai_appbuilder")
except PackageNotFoundError:
    __version__ = "2.45.40"

g_base_path = os.path.dirname(os.path.abspath(__file__))
if sys.platform.startswith('linux'):

    import ctypes
    ctypes.CDLL(g_base_path + "/libappbuilder.so", ctypes.RTLD_GLOBAL)
    ctypes.CDLL(g_base_path + "/libGenie.so", ctypes.RTLD_GLOBAL)

from .qnncontext import *
from .geniecontext import *
from .onnxwrapper import *
