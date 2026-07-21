# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the App Builder bounded context.

All errors inherit from :class:`qai.platform.errors.DomainError` and
set a stable :attr:`default_code` of the shape ``"app_builder.<reason>"``.

Handlers in the application / interfaces layers may match on the
``code`` string for routing (e.g. mapping to HTTP 404 vs 409) without
caring about the exact subclass.

**Note on "not found" errors and HTTP mapping**: the global HTTP error
handler routes ``DomainError → 422`` and ``NotFoundError → 404``.
:class:`AppModelNotFoundError`, :class:`RunNotFoundError`, and
:class:`ShareNotFoundError` remain :class:`DomainError` subclasses to
preserve their zero-argument constructor and existing 12+ ``except``
clauses across the codebase. Routes that want a REST-shaped 404 catch
these explicitly and re-raise ``HTTPException(status_code=404)`` — see
``interfaces/http/routes/app_builder/_catalog.py:277``,
``_runs.py:504``, ``_models.py`` etc. for the pattern. Sub-classing from
:class:`NotFoundError` would break the constructor contract (that class
requires positional ``code, resource_type, resource_id``).
"""

from __future__ import annotations

from qai.platform.errors import DomainError

__all__ = [
    "AppModelNotFoundError",
    "AppModelInvalidError",
    "AppModelDisabledError",
    "RunNotFoundError",
    "RunAlreadyTerminatedError",
    "RunInvalidTransitionError",
    "ArtifactWriteError",
    "ImportConflictError",
    "VoicePreferenceInvalidError",
    "ShareNotFoundError",
    "ShareExpiredError",
]


class AppModelNotFoundError(DomainError):
    """Raised when an :class:`AppModelDefinition` cannot be located."""

    default_code = "app_builder.app_model_not_found"


class AppModelInvalidError(DomainError):
    """Raised when an app model definition fails domain validation."""

    default_code = "app_builder.app_model_invalid"


class AppModelDisabledError(DomainError):
    """Raised when a caller tries to start a Run on a disabled model."""

    default_code = "app_builder.app_model_disabled"


class RunNotFoundError(DomainError):
    """Raised when a :class:`Run` aggregate cannot be located by id."""

    default_code = "app_builder.run_not_found"


class RunAlreadyTerminatedError(DomainError):
    """Raised when a transition or mutation is attempted on a terminal run."""

    default_code = "app_builder.run_already_terminated"


class RunInvalidTransitionError(DomainError):
    """Raised when a status transition is rejected by the state machine."""

    default_code = "app_builder.run_invalid_transition"


class ArtifactWriteError(DomainError):
    """Raised when an artifact cannot be persisted (path / size invariant)."""

    default_code = "app_builder.artifact_write_error"


class ImportConflictError(DomainError):
    """Raised when a commit / rollback conflicts with current state.

    Examples: rollback referencing an unknown commit_id, or commit of a
    plan whose target ids were modified between dry-run and commit.
    """

    default_code = "app_builder.import_conflict"


class VoicePreferenceInvalidError(DomainError):
    """Raised when a :class:`VoiceInputPreference` payload is malformed."""

    default_code = "app_builder.voice_preference_invalid"


class ShareNotFoundError(DomainError):
    """Raised when a :class:`Share` token cannot be located by id."""

    default_code = "app_builder.share_not_found"


class ShareExpiredError(DomainError):
    """Raised when a Share token resolves but is revoked / past expiry."""

    default_code = "app_builder.share_expired"
