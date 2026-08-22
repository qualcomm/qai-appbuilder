# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Ports (abstract interfaces) for ID generation.

Defining a :class:`Protocol` lets application code depend on the *idea*
of an ID generator without binding to a concrete implementation.  Tests
can substitute a deterministic fake without monkey-patching, and
production code can swap ULID for UUID v7 (or vice versa) by changing
exactly one wiring point.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.platform.ids.ulid import new_ulid
from qai.platform.ids.uuid7 import new_uuid7


@runtime_checkable
class IdGenerator(Protocol):
    """Anything that can mint a fresh, opaque string identifier.

    The protocol is decorated with :func:`runtime_checkable` so callers
    can use :func:`isinstance` to verify that injected dependencies
    satisfy the contract -- useful at composition-root boundaries.
    """

    def new_id(self) -> str:
        """Return a fresh identifier.  Must be unique with high probability."""
        ...


class UlidGenerator:
    """Concrete :class:`IdGenerator` that produces ULIDs.

    Stateless by design: each call delegates to :func:`new_ulid`.  Safe
    to instantiate once per process and share across threads.
    """

    def new_id(self) -> str:
        return new_ulid()


class Uuid7Generator:
    """Concrete :class:`IdGenerator` that produces UUID v7 strings.

    Stateless and safe for concurrent use; see :func:`new_uuid7`.
    """

    def new_id(self) -> str:
        return new_uuid7()


__all__ = [
    "IdGenerator",
    "UlidGenerator",
    "Uuid7Generator",
]
