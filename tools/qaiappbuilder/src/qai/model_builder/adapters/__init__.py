# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapter layer for the ``model_builder`` bounded context.

Concrete implementations of the application ports:

* :class:`WosAiWorkspaceReader` — reads ``C:/WoS_AI/<name>/`` into a
  :class:`qai.model_builder.domain.ModelWorkspace`;
* :class:`QaiPackExporter` — writes the ``app_pack/`` directory using
  the helpers in :mod:`._pack_layout`, :mod:`._manifest_builder`,
  :mod:`._runner_templates`, :mod:`._plan_parser`,
  :mod:`._io_contract_probe`;
* :class:`QaiPackValidator` — structural Pack validator equivalent
  to ``features/model-builder/scripts/qai_pack_validate.py``;
* :class:`RuleAndShapeTaxonomyClassifier` — three-layer classifier
  (rules → shape → optional LLM callable);
* :class:`FileSystemWorkspaceInitializer` — bootstraps a new
  workspace skeleton (``qai_workspace_init.py`` parity).
"""

from __future__ import annotations

from .pack_validator import QaiPackValidator
from .qai_pack_exporter import QaiPackExporter
from .taxonomy_classifier import RuleAndShapeTaxonomyClassifier
from .workspace_initializer import FileSystemWorkspaceInitializer
from .wos_workspace_reader import WosAiWorkspaceReader

__all__ = [
    "QaiPackValidator",
    "QaiPackExporter",
    "RuleAndShapeTaxonomyClassifier",
    "FileSystemWorkspaceInitializer",
    "WosAiWorkspaceReader",
]
