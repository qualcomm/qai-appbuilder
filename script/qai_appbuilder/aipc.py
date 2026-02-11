#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import os
import sys
from typing import Optional

def patch_onnxruntime_to_qnn(repo_dir: Optional[str] = None) -> None:
    if repo_dir is None:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
    if repo_dir not in sys.path:
        sys.path.insert(0, repo_dir)
    from . import qai_onnxruntime as _ort
    sys.modules["onnxruntime"] = _ort