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
    import ctypes
    ctypes.CDLL(g_base_path + "/libappbuilder.so", ctypes.RTLD_GLOBAL)

from .qnncontext import *
from .geniecontext import *
