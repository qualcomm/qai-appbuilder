# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Lightweight, context-free input validation helpers.

This package provides cheap ``assert_*`` style validators for the
*platform* layer.  They are deliberately scoped much narrower than
pydantic / dataclass validators:

* No business / DTO knowledge -- just strings, bytes, paths, numbers.
* No I/O, no filesystem, no network.
* No module-level mutable state.

Use them as a *prefilter* before invoking application-layer schema
validators (pydantic), or whenever full schema validation would be
overkill (e.g. trimming a single CLI argument).

Public API
----------
The names re-exported from this package are the project's contract;
internal modules (``_errors``, etc.) are not.

* String / byte validators -- :mod:`.strings`
* Path / filename validators -- :mod:`.paths`
* Numeric range validator -- :mod:`.numbers`
* :class:`ValidationError` -- the exception type every helper raises.
"""

from __future__ import annotations

from ._errors import ValidationError
from .numbers import assert_in_range
from .paths import assert_no_path_traversal, assert_safe_filename
from .strings import (
    assert_byte_size,
    assert_max_length,
    assert_matches,
    assert_no_control_chars,
    assert_non_empty,
    assert_one_of,
    assert_utf8_decodable,
)

__all__ = [
    # error type
    "ValidationError",
    # string / byte assertions
    "assert_non_empty",
    "assert_max_length",
    "assert_matches",
    "assert_one_of",
    "assert_no_control_chars",
    "assert_byte_size",
    "assert_utf8_decodable",
    # path assertions
    "assert_no_path_traversal",
    "assert_safe_filename",
    # numeric assertions
    "assert_in_range",
]
