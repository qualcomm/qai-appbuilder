# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Three-mode init vocabulary (parallel of ``tools.build.factory_compiler._common.modes``).

Kept as a separate module to avoid cross-package imports between
``tools.init`` and ``tools.build.factory_compiler``. The literal values are identical
by spec — both packages must accept the same CLI flag strings.
"""

from __future__ import annotations

from typing import Literal

Mode = Literal["dry-run", "apply", "verify"]

VALID_MODES: tuple[Mode, ...] = ("dry-run", "apply", "verify")


def parse_mode(value: str) -> Mode:
    if value not in VALID_MODES:
        raise ValueError(
            f"invalid mode {value!r}; expected one of {list(VALID_MODES)}"
        )
    return value  # type: ignore[return-value]


__all__ = ["Mode", "VALID_MODES", "parse_mode"]
