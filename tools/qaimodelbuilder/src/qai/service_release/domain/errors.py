# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the ``service_release`` bounded context.

Inherits the platform error roots so the HTTP layer maps them uniformly.
``default_code`` follows ``"service_release.<reason>"``.
"""

from __future__ import annotations

from qai.platform.errors import (
    DomainError,
    NotFoundError,
    ValidationError,
)


class ServiceVersionNotFoundError(NotFoundError):
    """Raised when a requested GenieAPIService version is unknown locally."""

    default_code = "service_release.version_not_found"

    def __init__(self, version: str, message: str | None = None) -> None:
        super().__init__(self.default_code, "service_version", version, message)


class CatalogModelNotFoundError(NotFoundError):
    """Raised when a requested catalog model / variant is unknown."""

    default_code = "service_release.model_not_found"

    def __init__(self, model_id: str, message: str | None = None) -> None:
        super().__init__(self.default_code, "catalog_model", model_id, message)


class DownloadNotFoundError(NotFoundError):
    """Raised when no downloaded/installed artifact matches a delete request."""

    default_code = "service_release.download_not_found"

    def __init__(self, identifier: str, message: str | None = None) -> None:
        super().__init__(self.default_code, "download", identifier, message)


class InvalidDownloadRequestError(ValidationError):
    """Raised when a download request has a missing/invalid URL or id."""

    default_code = "service_release.invalid_download_request"

    def __init__(
        self, message: str, field_errors: dict[str, list[str]] | None = None
    ) -> None:
        super().__init__(self.default_code, message, field_errors=field_errors)


class CatalogUnavailableError(DomainError):
    """Raised when a remote manifest/catalog cannot be parsed (content error).

    Transport failures surface as platform ``InfrastructureError`` /
    ``ExternalServiceError`` from the fetcher adapter; this is for content
    problems (malformed JSON, missing required fields, empty body) and for
    "url not configured".
    """

    default_code = "service_release.catalog_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class InstallFailedError(DomainError):
    """Raised when unzip/install into bin/ or models/ fails."""

    default_code = "service_release.install_failed"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


__all__ = [
    "ServiceVersionNotFoundError",
    "CatalogModelNotFoundError",
    "DownloadNotFoundError",
    "InvalidDownloadRequestError",
    "CatalogUnavailableError",
    "InstallFailedError",
]
