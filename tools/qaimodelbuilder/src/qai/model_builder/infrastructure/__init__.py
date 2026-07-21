# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Infrastructure layer for the ``model_builder`` bounded context.

Currently empty: the export pipeline runs entirely from adapters and
domain types; no shared infrastructure helpers are required beyond
the standard library.

The ``[importlinter:contract:layered-model_builder]`` contract still
recognises this layer so adapters that grow heavyweight collaborators
(e.g. a process-based PowerShell launcher to run the legacy
``run_pipeline.py`` driver) can land them here without churning the
contract.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
