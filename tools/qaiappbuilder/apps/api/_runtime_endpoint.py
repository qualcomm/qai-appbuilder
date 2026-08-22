# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime endpoint file — backwards-compatible re-export shim.

The implementation moved to ``qai.platform.process.runtime_endpoint`` so
that BOTH the API process (``apps.*``) and init tooling
(``scripts/init/uninstall.py``) can share the same single source of truth
without ``scripts/init`` having to import ``apps.*`` (which violates the
init→apps isolation contract enforced by
``tests/integration/init/test_no_legacy_imports.py``). See INIT-ISO-1
(2026-06-27).

This module re-exports the platform implementation verbatim so existing
``apps.api._runtime_endpoint`` consumers (``apps/api/lifespan.py`` /
``apps/cli/_endpoint_helper.py`` and their tests) keep working unchanged.
New code should import from ``qai.platform.process.runtime_endpoint``
directly.
"""

from __future__ import annotations

from qai.platform.process.runtime_endpoint import (
    ENDPOINT_FILENAME,
    RUNTIME_SUBDIR,
    clear_endpoint,
    endpoint_path,
    read_endpoint,
    write_endpoint,
)

__all__ = [
    "ENDPOINT_FILENAME",
    "RUNTIME_SUBDIR",
    "clear_endpoint",
    "endpoint_path",
    "read_endpoint",
    "write_endpoint",
]
