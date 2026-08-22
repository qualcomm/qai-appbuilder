# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the ``model_catalog`` bounded context.

All errors here inherit from one of the three platform error roots
(:class:`DomainError`, :class:`ApplicationError`, or one of its
subclasses).  Adapters / use cases that need to distinguish "this is
business-rule" from "this is technical" still get clean filtering.

Naming follows S2 §9: ``<Context><Reason>Error``.  ``default_code``
follows ``"model_catalog.<reason>"``.
"""

from __future__ import annotations

from typing import Any

from qai.platform.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# NotFound family -- reach through ``application.NotFoundError`` so the HTTP
# layer can map them to 404 uniformly.
# ---------------------------------------------------------------------------


class ModelEntryNotFoundError(NotFoundError):
    """Raised when a referenced :class:`ModelEntry` is unknown."""

    default_code = "model_catalog.entry_not_found"

    def __init__(self, model_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            "model_entry",
            model_id,
            message,
        )


class ModelVersionNotFoundError(NotFoundError):
    """Raised when a referenced :class:`ModelVersion` is unknown."""

    default_code = "model_catalog.version_not_found"

    def __init__(self, version_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            "model_version",
            version_id,
            message,
        )


class DownloadJobNotFoundError(NotFoundError):
    """Raised when a referenced :class:`DownloadJob` is unknown."""

    default_code = "model_catalog.download_job_not_found"

    def __init__(self, job_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            "download_job",
            job_id,
            message,
        )


# ---------------------------------------------------------------------------
# Conflict / state-machine errors -- the model already exists, the job is
# already terminated, etc.
# ---------------------------------------------------------------------------


class ModelEntryConflictError(ConflictError):
    """Raised when registering a :class:`ModelEntry` that already exists."""

    default_code = "model_catalog.entry_conflict"

    def __init__(self, model_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            message
            if message is not None
            else f"model_entry {model_id!r} already exists",
            details={"model_id": model_id},
        )


class DownloadJobAlreadyTerminatedError(PreconditionFailedError):
    """Raised when transitioning a terminal job into a non-terminal state."""

    default_code = "model_catalog.download_job_terminated"

    def __init__(self, job_id: str, current_state: str) -> None:
        super().__init__(
            self.default_code,
            (
                f"download_job {job_id!r} is already terminal "
                f"(state={current_state!r})"
            ),
        )


# ---------------------------------------------------------------------------
# Pure-domain errors -- never escape past the use-case layer.
# ---------------------------------------------------------------------------


class InvalidDownloadStateTransitionError(DomainError):
    """Raised by the entity FSM when an illegal state edge is requested."""

    default_code = "model_catalog.invalid_download_transition"

    def __init__(self, *, from_state: str, to_state: str) -> None:
        message = (
            f"illegal DownloadJob state transition: {from_state!r} -> {to_state!r}"
        )
        super().__init__(
            self.default_code,
            message,
            details={"from_state": from_state, "to_state": to_state},
        )


class ChecksumMismatchError(DomainError):
    """Raised when a verifier rejects the candidate digest."""

    default_code = "model_catalog.checksum_mismatch"

    def __init__(
        self,
        *,
        expected: str,
        actual: str,
        version_id: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"expected": expected, "actual": actual}
        if version_id is not None:
            details["version_id"] = version_id
        super().__init__(
            self.default_code,
            "checksum mismatch detected",
            details=details,
        )


# ---------------------------------------------------------------------------
# Provider / manifest errors.
# ---------------------------------------------------------------------------


class ProviderConfigInvalidError(ValidationError):
    """Raised when a cloud-provider config record fails domain validation."""

    default_code = "model_catalog.provider_config_invalid"

    def __init__(
        self,
        provider_id: str,
        field_errors: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(
            self.default_code,
            f"provider config {provider_id!r} is invalid",
            field_errors=field_errors,
        )


class ReleaseManifestUnavailableError(DomainError):
    """Raised when the remote release manifest cannot be parsed.

    The *transport* failure (network down, 503) is an
    :class:`InfrastructureError` raised by the
    :class:`ManifestFetcherPort` adapter; this domain-level error is for
    *content* problems (malformed JSON, missing required fields).
    """

    default_code = "model_catalog.release_manifest_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


__all__ = [
    "ModelEntryNotFoundError",
    "ModelVersionNotFoundError",
    "DownloadJobNotFoundError",
    "ModelEntryConflictError",
    "DownloadJobAlreadyTerminatedError",
    "InvalidDownloadStateTransitionError",
    "ChecksumMismatchError",
    "ProviderConfigInvalidError",
    "ReleaseManifestUnavailableError",
]
