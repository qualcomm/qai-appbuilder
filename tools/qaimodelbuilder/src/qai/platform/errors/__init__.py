# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API of :mod:`qai.platform.errors`.

Importing from this package is the supported way to use the error
hierarchy::

    from qai.platform.errors import (
        QaiError,
        DomainError,
        ApplicationError,
        InfrastructureError,
        NotFoundError,
        ConflictError,
        ValidationError,
        UnauthorizedError,
        ForbiddenError,
        RateLimitedError,
        PreconditionFailedError,
        PersistenceError,
        ExternalServiceError,
        TimeoutError_,
        ConfigurationError,
    )
"""

from __future__ import annotations

from .application import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
    RateLimitedError,
    UnauthorizedError,
    ValidationError,
)
from .base import (
    ApplicationError,
    DomainError,
    InfrastructureError,
    QaiError,
)
from .infrastructure import (
    ConfigurationError,
    ExternalServiceError,
    PersistenceError,
    TimeoutError_,
)

__all__ = [
    # Base hierarchy
    "QaiError",
    "DomainError",
    "ApplicationError",
    "InfrastructureError",
    # Application-layer
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "UnauthorizedError",
    "ForbiddenError",
    "RateLimitedError",
    "PreconditionFailedError",
    # Infrastructure-layer
    "PersistenceError",
    "ExternalServiceError",
    "TimeoutError_",
    "ConfigurationError",
]
