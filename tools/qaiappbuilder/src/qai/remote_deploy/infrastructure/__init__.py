# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Infrastructure layer for ``qai.remote_deploy``.

Re-exports the paramiko SSH executor.
"""
from __future__ import annotations

from qai.remote_deploy.infrastructure.paramiko_executor import ParamikoSshExecutor

__all__ = ["ParamikoSshExecutor"]
