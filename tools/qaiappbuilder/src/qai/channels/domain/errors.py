# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the channels bounded context.

All errors here inherit from :mod:`qai.platform.errors` so that callers
can filter coarsely (``except DomainError`` /
``except ApplicationError``) while still pinning a precise
machine-readable ``code`` per failure.

Error code namespace: ``channels.<reason>``.

References:
- ``docs/90-refactor/refactor-plan.md`` §8.7 (channel application
  service split — the errors below describe the failure modes that
  service must surface).
- ``docs/90-refactor/inventory/08-business-capabilities.md`` §5.2
  (eliminating the legacy SCCs and module-level state).
"""

from __future__ import annotations

from typing import Any

from qai.platform.errors import (
    ApplicationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Lookup misses (NotFoundError)
# ---------------------------------------------------------------------------
class ChannelInstanceNotFoundError(NotFoundError):
    """Raised when a channel instance lookup misses."""

    default_code = "channels.instance_not_found"

    def __init__(self, instance_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="channel_instance",
            resource_id=instance_id,
            message=message,
        )


class ChannelMessageNotFoundError(NotFoundError):
    """Raised when a channel message lookup misses."""

    default_code = "channels.message_not_found"

    def __init__(self, message_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="channel_message",
            resource_id=message_id,
            message=message,
        )


class SessionIndexEntryNotFoundError(NotFoundError):
    """Raised when a session-index lookup misses for a channel user."""

    default_code = "channels.session_index_not_found"

    def __init__(self, channel_user_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="session_index_entry",
            resource_id=channel_user_id,
            message=message,
        )


class CredentialsNotFoundError(NotFoundError):
    """Raised when an instance's credentials_ref does not resolve in the
    SecretStore."""

    default_code = "channels.credentials_not_found"

    def __init__(self, credentials_ref: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="channel_credentials",
            resource_id=credentials_ref,
            message=message,
        )


class QrLoginChallengeNotFoundError(NotFoundError):
    """Raised when a QR login challenge id does not resolve."""

    default_code = "channels.qr_login_not_found"

    def __init__(self, challenge_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="qr_login_challenge",
            resource_id=challenge_id,
            message=message,
        )


# ---------------------------------------------------------------------------
# Conflicts (ConflictError)
# ---------------------------------------------------------------------------
class ChannelInstanceAlreadyRunningError(ConflictError):
    """Raised when ``StartChannelInstanceUseCase`` is invoked on an
    instance that is already in the ``running`` or ``starting`` state."""

    default_code = "channels.instance_already_running"

    def __init__(
        self,
        instance_id: str,
        current_status: str,
        message: str | None = None,
    ) -> None:
        msg = (
            message
            if message is not None
            else (
                f"channel instance {instance_id!r} cannot start: "
                f"current status is {current_status!r}"
            )
        )
        super().__init__(
            self.default_code,
            msg,
            details={"instance_id": instance_id, "current_status": current_status},
        )
        self.instance_id: str = instance_id
        self.current_status: str = current_status


# ---------------------------------------------------------------------------
# State-machine preconditions
# ---------------------------------------------------------------------------
class ChannelInstanceStateError(PreconditionFailedError):
    """Raised when a state transition on :class:`ChannelInstance` is illegal.

    Examples:
    * Calling ``mark_stopped()`` on an instance that is already ``stopped``.
    * Calling ``mark_running()`` on an instance that is in ``error`` state.
    """

    default_code = "channels.instance_state_invalid"

    def __init__(
        self,
        message: str,
        *,
        current_status: str,
        attempted: str,
    ) -> None:
        super().__init__(self.default_code, message)
        self.current_status: str = current_status
        self.attempted: str = attempted


class ChannelMessageStateError(PreconditionFailedError):
    """Raised when a state transition on :class:`ChannelMessage` is illegal."""

    default_code = "channels.message_state_invalid"

    def __init__(
        self,
        message: str,
        *,
        current_status: str,
        attempted: str,
    ) -> None:
        super().__init__(self.default_code, message)
        self.current_status: str = current_status
        self.attempted: str = attempted


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------
class ChannelKindNotSupportedError(ValidationError):
    """Raised when a caller references a :class:`ChannelKind` value the
    runtime has no transport adapter or signature verifier registered for.

    This is distinct from "kind is unknown to the enum" — that surfaces
    as a :class:`ValueError` from :meth:`ChannelKind.from_str`.  The
    error here means the *application* layer accepted the kind but no
    adapter is wired in for it.
    """

    default_code = "channels.kind_not_supported"

    def __init__(self, kind: str, message: str | None = None) -> None:
        msg = (
            message
            if message is not None
            else f"channel kind {kind!r} has no adapter registered"
        )
        super().__init__(self.default_code, msg)
        self.kind: str = kind


class WebhookSignatureInvalidError(DomainError):
    """Raised when an inbound webhook fails signature verification.

    Modeled as a :class:`DomainError` (not a generic :class:`ValidationError`)
    because signature verification is a domain concept of the channel
    context, not a generic schema validation.
    """

    default_code = "channels.webhook_signature_invalid"

    def __init__(
        self,
        kind: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"kind": kind}
        if details:
            merged.update(details)
        super().__init__(
            self.default_code,
            f"webhook signature verification failed for kind {kind!r}",
            details=merged,
        )
        self.kind: str = kind


class WebhookPayloadInvalidError(ValidationError):
    """Raised when an inbound webhook body is malformed or violates the
    expected envelope schema for the channel kind."""

    default_code = "channels.webhook_payload_invalid"

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        field_errors: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(
            self.default_code,
            f"[{kind}] {message}",
            field_errors=field_errors,
        )
        self.kind: str = kind


class InvalidCommandError(ValidationError):
    """Raised when command-text parsing yields an unparseable command."""

    default_code = "channels.invalid_command"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


# ---------------------------------------------------------------------------
# Bridge / external coordination
# ---------------------------------------------------------------------------
class MessageBridgeUnavailableError(ApplicationError):
    """Raised when :class:`~qai.channels.application.ports.MessageBridgePort`
    cannot accept an incoming dispatch.

    Refactor-plan §8.7 states that the bridge is the only collaboration
    seam between channels and chat / ai_coding; this error preserves the
    failure shape without leaking concrete adapter details.
    """

    default_code = "channels.message_bridge_unavailable"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(self.default_code, message, details=details)


__all__ = [
    "ChannelInstanceNotFoundError",
    "ChannelMessageNotFoundError",
    "SessionIndexEntryNotFoundError",
    "CredentialsNotFoundError",
    "QrLoginChallengeNotFoundError",
    "ChannelInstanceAlreadyRunningError",
    "ChannelInstanceStateError",
    "ChannelMessageStateError",
    "ChannelKindNotSupportedError",
    "WebhookSignatureInvalidError",
    "WebhookPayloadInvalidError",
    "InvalidCommandError",
    "MessageBridgeUnavailableError",
]
