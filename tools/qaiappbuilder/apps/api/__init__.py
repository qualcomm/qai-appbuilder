# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai apps.api — FastAPI HTTP entry point (S1 skeleton).

This is the new application entry. Constraints (refactor-plan v2.5 §6 / §11):

- ``main.py`` ≤ 150 lines: only ``create_app`` + ``main`` + thin uvicorn glue.
- ``lifespan.py`` orchestrates startup/shutdown with explicit DI.
- ``di.py`` builds the dependency container. NO module-level singletons.
- This entry point MUST NOT import ``backend.*``, ``features.*``,
  ``start_server``, or any legacy module. Verified by import-linter.
"""
