#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import os
import sys
g_base_path = os.path.dirname(os.path.abspath(__file__))
if sys.platform.startswith('linux'):
    ADSP_LIBRARY_PATH = os.environ.get('ADSP_LIBRARY_PATH')
    if ADSP_LIBRARY_PATH is None or len(ADSP_LIBRARY_PATH) < 2:
        os.environ["ADSP_LIBRARY_PATH"] = g_base_path = "/libs"

    import ctypes
    ctypes.CDLL(g_base_path + "/libappbuilder.so", ctypes.RTLD_GLOBAL)
    ctypes.CDLL(g_base_path + "/libGenie.so", ctypes.RTLD_GLOBAL)

from .qnncontext import *
from .geniecontext import *
