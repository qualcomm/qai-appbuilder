# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public ID-generation API for the qai platform layer.

Two flavours of identifier are supported:

* **ULID** -- 26-char Crockford base32 string, naturally time-sortable.
  Recommended default for new code (compact, copy-paste friendly).
* **UUID v7** -- 36-char canonical UUID string with an embedded
  millisecond timestamp.  Useful when downstream systems require the
  classic UUID shape.

Consumers should depend on :class:`IdGenerator` (a :class:`Protocol`)
rather than the free functions whenever the choice of identifier is a
configuration concern.
"""

from __future__ import annotations

from qai.platform.ids.ports import IdGenerator, UlidGenerator, Uuid7Generator
from qai.platform.ids.ulid import is_valid_ulid, new_ulid, ulid_to_timestamp_ms
from qai.platform.ids.uuid7 import is_valid_uuid, new_uuid7

__all__ = [
    # ULID
    "new_ulid",
    "is_valid_ulid",
    "ulid_to_timestamp_ms",
    # UUID v7
    "new_uuid7",
    "is_valid_uuid",
    # Ports
    "IdGenerator",
    "UlidGenerator",
    "Uuid7Generator",
]
