# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application layer for the ``app_builder`` bounded context.

Contains use cases (``application/use_cases/``) and abstract ports
(``application/ports.py``). Adapters and infrastructure live one layer
out (``adapters/``, ``infrastructure/``) and will be filled in by
PR-040+ — see ``docs/90-refactor/refactor-plan.md`` §13.
"""

from __future__ import annotations
