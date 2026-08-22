# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Internal error-class shim for the io_validator subpackage.

The platform-wide :class:`ValidationError` lives in
:mod:`qai.platform.errors.application`.  Production code imports it
through :mod:`qai.platform.errors`'s package re-export; this shim
prefers that path and falls back to the deeper submodule if a partial
install only ships the submodule.  As a final safety net (e.g. when
this module is imported in isolation by a smoke test that bypasses
the full ``qai.platform.errors`` package) the shim degrades to the
builtin :class:`ValueError`, which keeps ``except (ValidationError,
ValueError)`` patterns working without a hard dependency on the
package layout.

Constructor convention used by io_validator
-------------------------------------------
The real :class:`ValidationError` accepts ``(code, message,
field_errors=None)``.  We always pass two positional arguments
(``code`` and ``message``); when the fallback :class:`ValueError` is
used those two arguments are accepted as the regular ``args`` tuple,
and ``except (ValidationError, ValueError)`` keeps working in either
mode.
"""

from __future__ import annotations

from typing import Final

# Prefer the package-level re-export so callers see the canonical
# class identity; fall back to the submodule path, then to the
# builtin :class:`ValueError` when neither is importable.  This keeps
# the two-argument constructor convention working uniformly.
try:
    from qai.platform.errors import ValidationError as _RealValidationError  # type: ignore[attr-defined]
except ImportError:
    try:
        from qai.platform.errors.application import (
            ValidationError as _RealValidationError,
        )
    except ImportError:  # pragma: no cover - errors module always present in prod
        _RealValidationError = ValueError  # type: ignore[misc, assignment]

ValidationError: Final[type[Exception]] = _RealValidationError


__all__ = ["ValidationError", "raise_validation"]


def raise_validation(code: str, message: str) -> None:
    """Raise the platform-level :class:`ValidationError`.

    Centralising the construction call lets callers stay agnostic of
    the (future) ``field_errors`` parameter and of the ValueError
    fallback used while the errors subtask is in flight.
    """

    # ``ValidationError(code, message)`` matches both the real two-arg
    # __init__ (``code, message, field_errors=None``) and the
    # builtin ``ValueError(*args)`` fallback.
    raise ValidationError(code, message)
