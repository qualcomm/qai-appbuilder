# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Entry point for ``python -m apps.api``."""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
