# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events emitted by the security bounded context.

Subscribers (other contexts, audit sinks, the reboot signal hook, …)
listen for these via :class:`qai.platform.events.EventBus`.

Each event carries only **value snapshots** — strings, value objects,
enum values — never live aggregates, so subscribers can be scheduled
later without risking races on mutable state.

Naming follows the spec §9: ``<Subject><Verb>Event`` with
``event_type = "security.<verb>_<subject>"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from qai.platform.events.types import DomainEvent

from .value_objects import (
    AceMask,
    GrantSource,
    PolicyAction,
    RequestId,
    Resource,
    Subject,
)

__all__ = [
    "PathGrantCreatedEvent",
    "PathGrantRevokedEvent",
    "PermissionApprovedEvent",
    "PermissionAskBlockedEvent",
    "PermissionRejectedEvent",
    "PermissionRequestCancelledEvent",
    "PermissionRequestedEvent",
    "PermissionResolvedEvent",
    "PolicyChangedEvent",
    "PolicyShadowDetectedEvent",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyChangedEvent(DomainEvent):
    """Fired after a policy is replaced or amended.

    The application layer typically subscribes to this in order to fire
    the reboot signal (REBOOT_EXIT_CODE = 75 contract) — but the domain
    event itself is silent on whether a reboot must happen; that's a
    policy decision belonging to the application layer.
    """

    event_type: ClassVar[str] = "security.policy_changed"

    old_version: int
    new_version: int
    occurred_at: datetime
    requires_reboot: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRequestedEvent(DomainEvent):
    """Fired when a new PermissionRequest is created."""

    event_type: ClassVar[str] = "security.permission_requested"

    request_id: RequestId
    subject: Subject
    resource: Resource
    requested_mask: AceMask
    occurred_at: datetime
    # P-EXEC (exec-broker dangerous-command ASK) — TAIL-appended optional
    # rationale ("why does this command need confirmation?"). Default ""
    # keeps the dataclass constructor backward-compatible for every S0-S7
    # caller that omits it (e.g. the plain FileGuard path ASK).
    reason: str = ""
    # i18n contract (A2): the structured reason CODE + interpolation ARGS the
    # frontend uses to render the localized "why confirm?" text from its own
    # locale catalog (so the backend stays language-agnostic and a UI language
    # switch re-localizes with zero backend round-trip). TAIL-appended optional
    # (§3.1 additive) — empty ``reason_code`` keeps S0-S7 / operator-custom
    # frames byte-for-byte unchanged (the frontend falls back to ``reason``).
    reason_code: str = ""
    reason_args: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project onto the V1 ``permission_request`` SSE frame shape.

        P0 ASK restore — the global ``/api/events`` SSE serialiser prefers a
        ``to_dict()`` when present; emitting the V1-aligned field names
        (``type=permission_request`` / ``id`` / ``op`` / ``path`` /
        ``caller`` / ``session_id`` / ``timestamp``, see V1
        ``backend/security/policy.py:1486-1495``) lets the front-end
        authorization-dialog renderer consume this event with the same
        contract it used in V1. ``op`` is derived from the requested mask
        (write > exec > read precedence); ``caller`` from the subject
        identifier; ``path`` from the resource identifier.
        """
        if self.requested_mask.write:
            op = "write"
        elif self.requested_mask.execute:
            op = "exec"
        else:
            op = "read"
        frame: dict[str, Any] = {
            "type": "permission_request",
            "id": self.request_id.value,
            "op": op,
            "path": self.resource.identifier,
            "caller": self.subject.identifier,
            # V2 has no per-request channel/session on the FileGuard ASK
            # path (the普通聊天 tool path is single web session); keep the
            # keys present (V1 shape) with safe defaults so the front-end
            # never KeyErrors.
            "channel": "web",
            "session_id": "",
            "timestamp": self.occurred_at.isoformat(),
            # P-11 (backend) — native-subprocess discriminator. TAIL-appended
            # per §3.1 so S0-S7 consumers that ignore it keep their byte-for-
            # byte shape. ``True`` iff this ASK originated from the native
            # ``guard64.dll`` OS hook, which attributes every intercepted file
            # event to Subject(kind="system", identifier="native.file_guard")
            # (see apps/api/_native_hook_bridge.py:69 ``_SUBJECT_IDENTIFIER``).
            # In-process tool ASKs use other subject identifiers → ``False``.
            # The front-end uses this to gray out the (native-invalid)
            # "session" scope button — native sub-process events carry NO
            # conversation context, so only ``process`` / ``permanent`` grants
            # can ever match at the native layer.
            "is_native_subprocess": (
                self.subject.kind == "system"
                and self.subject.identifier == "native.file_guard"
            ),
        }
        # P-EXEC — TAIL-appended rationale. Emitted only when non-empty so
        # S0-S7 frames stay byte-for-byte identical (no ``reason`` key) for
        # the plain FileGuard path ASK; the exec-broker dangerous-command
        # ASK sets it so the front-end can render "why confirm?".
        if self.reason:
            frame["reason"] = self.reason
        # i18n contract (A2): the structured code + args the frontend renders
        # via its locale catalog. Emitted only when a code is present so
        # operator-custom / legacy frames (no code) stay unchanged and the
        # frontend falls back to the verbatim ``reason`` string.
        if self.reason_code:
            frame["reason_code"] = self.reason_code
            if self.reason_args:
                frame["reason_args"] = dict(self.reason_args)
        return frame


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionResolvedEvent(DomainEvent):
    """Fired to tell the UI a pending ASK popup should CLOSE (problem ②).

    Pure UI-close signal — it does NOT mutate any aggregate. When the
    backend resolves an already-queued native ASK future WITHOUT a local
    user response (chat-Stop flush of an exec child's pending ASKs, or the
    subprocess-gone backstop sweep), there is otherwise no SSE frame telling
    the front-end to dequeue the dialog, so the FileGuard authorization
    popup keeps demanding a choice until a full SSE-reconnect re-fetch.

    This is deliberately DISTINCT from
    :class:`PermissionRequestCancelledEvent` (which carries the aggregate
    PENDING → CANCELLED transition semantics for a subject *withdrawing*
    their own request): the flush must "silence the POPUP, not withdraw the
    REQUEST" (mirrors the ``/permission/cancel`` route comment), so it
    emits this lightweight resolved-notification instead.

    ``resolution`` is a free-form UI hint (``"stopped"`` for the chat-Stop
    flush, ``"subprocess_gone"`` for the cleanup backstop) so the front-end
    / audit can distinguish *why* the dialog auto-closed; the front-end only
    needs ``id`` to dequeue.
    """

    event_type: ClassVar[str] = "security.permission_resolved"

    request_id: RequestId
    resolution: str
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Project onto the ``permission_resolved`` SSE close-frame shape.

        The global ``/api/events`` serialiser prefers a ``to_dict()`` when
        present; emitting ``type="permission_resolved"`` + ``id`` lets the
        front-end ``App.vue`` ``onEvent`` dispatch to
        ``permissionDialog.dequeue(id)`` and close the matching dialog.
        """
        return {
            "type": "permission_resolved",
            "id": self.request_id.value,
            "resolution": self.resolution,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionApprovedEvent(DomainEvent):
    """Fired when a PermissionRequest transitions PENDING → APPROVED."""

    event_type: ClassVar[str] = "security.permission_approved"

    request_id: RequestId
    subject: Subject
    resource: Resource
    granted_mask: AceMask
    decided_by: Subject | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRejectedEvent(DomainEvent):
    """Fired when a PermissionRequest transitions PENDING → REJECTED."""

    event_type: ClassVar[str] = "security.permission_rejected"

    request_id: RequestId
    subject: Subject
    resource: Resource
    decided_by: Subject | None
    reason: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRequestCancelledEvent(DomainEvent):
    """Fired when a PermissionRequest transitions PENDING → CANCELLED.

    Modelled after :class:`PermissionRejectedEvent` but reserved for the
    case where the **subject** withdraws their own request before any
    reviewer acts on it (issue d, decision B — PR-040). ``cancelled_by``
    is typically the same as ``subject`` but is preserved as a separate
    field so administrative cancellations performed on a user's behalf
    can be distinguished in audit trails.
    """

    event_type: ClassVar[str] = "security.permission_request_cancelled"

    request_id: RequestId
    subject: Subject
    resource: Resource
    cancelled_by: Subject | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PathGrantCreatedEvent(DomainEvent):
    """Fired when a new persistent ACL entry is created."""

    event_type: ClassVar[str] = "security.path_grant_created"

    grant_id: str
    subject: Subject
    path: str
    mask: AceMask
    source: GrantSource
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PathGrantRevokedEvent(DomainEvent):
    """Fired when a persistent ACL entry is removed."""

    event_type: ClassVar[str] = "security.path_grant_revoked"

    grant_id: str
    subject: Subject
    path: str
    revoked_by: Subject | None
    occurred_at: datetime


# Re-export PolicyAction for downstream subscribers that key on decisions.
_ = PolicyAction


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyShadowDetectedEvent(DomainEvent):
    """Fired alongside :class:`PolicyChangedEvent` when ``Policy.detect_shadows``
    surfaces non-fatal pattern-shadow conflicts (PR-501).

    The event carries the raw warning records as plain ``dict`` rows so
    cross-process subscribers (SSE bridges in ``apps.api.*``) can
    serialise them without depending on the security domain types.
    """

    event_type: ClassVar[str] = "security.policy_shadow_detected"

    policy_version: int
    occurred_at: datetime
    warnings: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionAskBlockedEvent(DomainEvent):
    """Fired when a permission ask is short-circuited by channel policy.

    Two cases (PR-501):

    * ``reason="no_ui_channel"`` — the originating channel is
      headless; ASK was mapped to DENY at the use case.
    * ``reason="rate_limited"`` — the channel's
      :class:`qai.security.domain.value_objects.AskQuotaWindow` cap was
      exceeded.

    Subscribers (audit dashboards / chat-channel command UX) use this
    to give the user a clear "we can't ask here" or "you've asked too
    many times" message instead of an opaque deny audit row.
    """

    event_type: ClassVar[str] = "security.permission_ask_blocked"

    channel_name: str
    subject: Subject
    resource: Resource
    reason: str
    occurred_at: datetime
