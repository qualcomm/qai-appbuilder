# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Infrastructure layer for the ``model_runtime`` bounded context."""

from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)
from qai.model_runtime.infrastructure.service_config_repository import (
    FileServiceConfigRepository,
)

__all__ = [
    "ProcessBackedInferenceService",
    "FileServiceConfigRepository",
]
