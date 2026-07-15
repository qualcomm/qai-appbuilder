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
    ctypes.CDLL(g_base_path + "/libs" + "/libQnnSystem.so", ctypes.RTLD_GLOBAL)
    ctypes.CDLL(g_base_path + "/libs" + "/libQnnHtp.so", ctypes.RTLD_GLOBAL)
    ctypes.CDLL(g_base_path + "/libs" + "/libQnnHtpNetRunExtensions.so", ctypes.RTLD_GLOBAL)

    # The QAIAppSvc service binary (used by QNNContextProc for cross-process
    # inference) must be executable so posix_spawnp can launch it. Some pip /
    # wheel-unpacking paths drop the executable bit, so restore it here if
    # missing. Best-effort: never fail the import over this.
    try:
        import stat
        _svc = os.path.join(g_base_path, "QAIAppSvc")
        if os.path.exists(_svc) and not os.access(_svc, os.X_OK):
            _mode = os.stat(_svc).st_mode
            os.chmod(_svc, _mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    # Cross-process inference (QNNContextProc) spawns a separate "QAIAppSvc"
    # executable. That child process must locate libappbuilder.so (and its QNN
    # dependencies) via the dynamic linker. The package only adds its dir to
    # PATH, not LD_LIBRARY_PATH, so the child fails with
    # "libappbuilder.so: cannot open shared object file". Prepend the package
    # dir (+ an optional QNN lib dir from QAI_QNN_LIB_DIR) to LD_LIBRARY_PATH
    # here; the spawned process inherits this environment.
    _extra_lib_dirs = [g_base_path]
    _qnn_dir = os.environ.get("QAI_QNN_LIB_DIR")
    if _qnn_dir:
        _extra_lib_dirs.append(_qnn_dir)
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
        _extra_lib_dirs + ([os.environ["LD_LIBRARY_PATH"]] if os.environ.get("LD_LIBRARY_PATH") else [])
    )

from .qnncontext import *
from .geniecontext import *
from .onnxwrapper import *
