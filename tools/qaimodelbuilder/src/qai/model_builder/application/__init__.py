# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application layer for the ``model_builder`` bounded context.

Contains the ``Protocol`` ports that abstract every external
dependency of the use cases (workspace I/O, pack writing, validation,
taxonomy classification) and the use cases that orchestrate them.

Routes / bridges only ever import from this layer (or from
``qai.model_builder.domain``). Adapters live under
``qai.model_builder.adapters`` and are wired by
``apps.api._model_builder_di``.
"""

from __future__ import annotations

from .ports import (
    PackExporterPort,
    PackValidatorPort,
    TaxonomyClassifierPort,
    WorkspaceReaderPort,
)
from .use_cases.export_pack import ExportPackUseCase
from .use_cases.init_workspace import InitWorkspaceUseCase
from .use_cases.validate_pack import ValidatePackUseCase

__all__ = [
    "PackExporterPort",
    "PackValidatorPort",
    "TaxonomyClassifierPort",
    "WorkspaceReaderPort",
    "ExportPackUseCase",
    "InitWorkspaceUseCase",
    "ValidatePackUseCase",
]
