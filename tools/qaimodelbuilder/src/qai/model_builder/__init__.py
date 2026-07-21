# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model Builder bounded context.

Owns the **ModelBuilder → AppBuilder Pack export** business capability.

Layers (Clean Architecture, ``[importlinter:contract:layered-model_builder]``)::

    domain        — pure value objects + entities, no I/O
    application   — Protocol ports + use cases
    adapters      — concrete implementations (workspace reader, pack exporter,
                    pack validator, taxonomy classifier)
    infrastructure — small filesystem helpers shared by adapters

Cross-context isolation (``[importlinter:contract:context-isolation]``):
this package does not import ``qai.<other_context>``. The HTTP route
``POST /api/app-builder/import/auto-export`` reaches the export use
case via :class:`apps.api._app_builder_model_builder_bridge.AppBuilderModelBuilderBridge`.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
