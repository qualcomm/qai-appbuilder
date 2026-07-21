# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Approve a pending permission request."""

from __future__ import annotations

from datetime import timedelta
from pathlib import PureWindowsPath

from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.domain.entities import PermissionRequest
from qai.security.domain.errors import PermissionRequestNotFoundError
from qai.security.domain.events import PermissionApprovedEvent
from qai.security.domain.value_objects import (
    GrantSource,
    RequestId,
    Subject,
)

from ..ports import PermissionRequestRepositoryPort

__all__ = ["ApprovePermissionUseCase"]

logger = get_logger(__name__)

# V1 ``resolve_permission`` grant vocabulary (``policy.py:1670``):
# only ``session`` / ``process`` / ``permanent`` persist a grant so the next
# same-path access is allowed without re-prompting; ``once`` grants this one
# call only (no persisted grant). ``deny`` is handled by the reject path.
_PERSISTING_SCOPES = frozenset({"session", "process", "permanent"})

# PR-4 (native-hook integration) — scope → grant lifetime. Previously
# ``_persist_grant`` passed NO ``expires_at``, so session / process /
# permanent all stored ``expires_at=NULL`` (non-expiring) — the 🟡🟡
# defect where a "session" or "process" grant silently outlived its scope
# and behaved identically to "permanent". We now map scope → a bounded TTL:
#   * session   — a working session window (12h); re-prompts next day.
#   * process   — the calling process's expected lifetime; a generous
#                 bound (8h) since V2 has no per-process grant GC hook.
#   * permanent — never expires (``expires_at=None``); the ONLY scope that
#                 seeds the native FileGuard persistent whitelist at startup.
# The domain invariant ``expires_at > created_at`` (entities.py) is always
# satisfied because both TTLs are strictly positive.
_SCOPE_TTL_SECONDS: dict[str, int | None] = {
    "session": 12 * 60 * 60,
    "process": 8 * 60 * 60,
    "permanent": None,
}

# P-11B directory grants — minimum path depth (non-drive path components) a
# parent directory must have before we will authorize the WHOLE directory.
# Guards against over-wide grants: authorizing ``C:\`` or ``C:\Users`` from a
# single file click would silently open a huge tree. A file like
# ``C:\a\b\c.txt`` has parent ``C:\a\b`` (2 components ≥ 2 → OK); a file
# directly under a drive root or one-level dir falls back to file-only. The
# matcher's ``_grant_path_ancestor_of`` boundary check still applies on top.
_MIN_DIR_GRANT_DEPTH = 2


def _exec_binary_token(command: str) -> str:
    """Extract + normalize the binary token from an exec command string.

    Mirrors ``check_permission._exec_binary_token`` (which in turn mirrors
    ``command_policy.extract_binary``) so the approve path stores exactly the
    token the matcher will compare against. Kept as a local helper to avoid
    ``qai.security`` importing ``qai.command_policy`` (context-isolation).
    Single source of truth for the RULE is ``command_policy.extract_binary`` —
    keep all three in sync. Normalization: basename, lowercase, drop ``.exe``.
    """
    cmd = (command or "").strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end_quote = cmd.find('"', 1)
        raw = cmd[1:end_quote] if end_quote > 0 else cmd
    else:
        parts = cmd.split()
        raw = parts[0] if parts else ""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for _ext in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(_ext):
            base = base[: -len(_ext)]
            break
    return base


def _dir_grant_path(file_path: str) -> str | None:
    """Return the parent directory to grant, or ``None`` if too shallow.

    Uses ``PureWindowsPath`` (this project is Windows-on-Snapdragon; native
    FileGuard paths are Windows) purely lexically — no disk IO. Returns
    ``None`` when the parent is a drive root / too few components (see
    :data:`_MIN_DIR_GRANT_DEPTH`), signalling the caller to fall back to a
    single-file grant rather than authorize an over-wide tree.
    """
    try:
        p = PureWindowsPath(file_path)
        parent = p.parent
        # parent.parts includes the drive/anchor (e.g. ('C:\\', 'a', 'b')).
        # Non-anchor components = the real directory depth.
        depth = len(parent.parts) - (1 if parent.anchor else 0)
        if depth < _MIN_DIR_GRANT_DEPTH:
            return None
        return str(parent)
    except Exception:  # noqa: BLE001 — any parse issue → file-only fallback
        return None


class ApprovePermissionUseCase:
    """Transition a PermissionRequest from PENDING to APPROVED.

    P0 ASK restore — when wired with a :class:`PermissionWaitRegistry` and a
    ``create_grant`` collaborator, approving also:

    * persists a :class:`PathGrant` for ``session`` / ``process`` /
      ``permanent`` scopes (so the next same-path access skips the prompt,
      V1 ``add_session_grant`` / ``add_process_grant`` / ``grant_permanent``
      parity); ``once`` persists nothing.
    * wakes the FileGuard ASK waiter blocked on this ``request_id`` with an
      ALLOW resolution (V1 ``pending.event.set()``).

    Both collaborators are optional so existing callers / tests keep working.
    """

    def __init__(
        self,
        *,
        request_repository: PermissionRequestRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        wait_registry: PermissionWaitRegistry | None = None,
        create_grant: object | None = None,
        audit_sink: object | None = None,
    ) -> None:
        self._requests = request_repository
        self._events = event_bus
        self._clock = clock
        self._wait_registry = wait_registry
        self._create_grant = create_grant
        # P-17 (2026-07-09): write the definitive ALLOW audit row here instead
        # of leaving a provisional deny/ask_pending in check_permission.
        self._audit_sink = audit_sink

    async def execute(
        self,
        *,
        request_id: RequestId,
        decided_by: Subject | None = None,
        reason: str = "",
        scope: str = "once",
        scope_conversation_id: str = "",
        scope_boot_id: str = "",
        grant_range: str = "file",
    ) -> PermissionRequest:
        existing = await self._requests.get(request_id)
        if existing is None:
            raise PermissionRequestNotFoundError(request_id.value)
        # Domain entity raises PermissionRequestAlreadyResolvedError
        # if state is not PENDING.
        now = self._clock.now()
        approved = existing.approve(now=now, reason=reason)
        await self._requests.save(approved)

        scope_n = (scope or "once").strip().lower()
        # Persist a grant for session/process/permanent so the next same-path
        # access is auto-allowed (V1 add_*_grant). Best-effort: a grant write
        # failure must not block the approval / wake (V1 wrapped the grant
        # persistence in try/except and still set the event).
        if scope_n in _PERSISTING_SCOPES and self._create_grant is not None:
            await self._persist_grant(
                existing=existing,
                now=now,
                scope=scope_n,
                scope_conversation_id=scope_conversation_id,
                scope_boot_id=scope_boot_id,
                grant_range=(grant_range or "file").strip().lower(),
            )

        await self._events.publish(
            PermissionApprovedEvent(
                request_id=request_id,
                subject=existing.subject,
                resource=existing.resource,
                granted_mask=existing.requested_mask,
                decided_by=decided_by,
                occurred_at=now,
            )
        )

        # Wake the FileGuard ASK waiter (if any) with an ALLOW resolution.
        if self._wait_registry is not None:
            self._wait_registry.resolve(
                request_id.value, allow=True, scope=scope_n
            )

        # P-17 (2026-07-09): write the definitive ALLOW audit row now that the
        # user has decided. This replaces the old provisional deny/ask_pending
        # row that check_permission used to write before the user responded.
        # Best-effort: an audit failure must never block the approval itself.
        if self._audit_sink is not None:
            try:
                from qai.security.domain.entities import AuditEntry
                from qai.security.domain.value_objects import PolicyAction

                _ids = getattr(self, "_ids", None)
                audit_id = (
                    _ids.new_id()
                    if _ids is not None
                    else f"approve-{request_id.value[:16]}"
                )
                await self._audit_sink.append(
                    AuditEntry(
                        audit_id=audit_id,
                        occurred_at=now,
                        subject=existing.subject,
                        resource=existing.resource,
                        decision=PolicyAction.ALLOW,
                        rule_id=None,
                        correlation_id=None,
                        note=f"approved scope={scope_n}",
                        channel=None,
                        op=existing.resource.kind,
                        process_path="",
                        command_line="",
                        actor_pid=None,
                        actor_parent_pid=None,
                    )
                )
            except Exception:  # noqa: BLE001 — audit failure must not block
                logger.warning(
                    "approve_permission: failed to write allow audit row "
                    "for request_id=%s — check AuditEntry construction / "
                    "audit_sink availability",
                    request_id.value,
                    exc_info=True,
                )

        return approved

    async def _persist_grant(
        self,
        *,
        existing: PermissionRequest,
        now: object,
        scope: str,
        scope_conversation_id: str = "",
        scope_boot_id: str = "",
        grant_range: str = "file",
    ) -> None:
        """Create a PathGrant covering the approved (subject, path, mask).

        PR-4 → SEC true-scoping: the grant now carries a REAL ``scope_kind``
        + ``scope_key`` so ``session`` / ``process`` are genuinely isolated
        (previously they were merely a TTL on a process-global grant — the
        🟡🟡 defect where a "session" grant silently outlived its scope):

        * ``session``   — ``scope_kind='session'``, ``scope_key`` = the
          top-level conversation id (shared by the main agent + all
          sub-agents / participants of that collaboration session). Only
          matches while the caller is in that conversation.
        * ``process``   — ``scope_kind='process'``, ``scope_key`` = the
          backend boot id. Only matches within this process; a restart mints
          a new boot id so old process grants stop matching.
        * ``permanent`` — ``scope_kind='permanent'``, no key; matches always
          and is the ONLY scope seeded into the native whitelist at startup.

        ``expires_at`` is ALSO set from :data:`_SCOPE_TTL_SECONDS` as a
        secondary GC safety net (so a session/process grant is eventually
        reaped even if scope-matching somehow lets it linger). ``permanent``
        stays non-expiring.

        Best-effort: a conflict (a SAME-scope grant for this subject+path
        already exists) or any write error is swallowed — the path is already
        authorised in that case, and the wake/ALLOW must still proceed.
        """
        scope_key = ""
        if scope == "session":
            scope_key = scope_conversation_id or ""
        elif scope == "process":
            scope_key = scope_boot_id or ""
        ttl_seconds = _SCOPE_TTL_SECONDS.get(scope)
        expires_at = None
        if ttl_seconds is not None:
            # ``now`` is the clock snapshot from ``execute`` (a datetime).
            expires_at = now + timedelta(seconds=ttl_seconds)  # type: ignore[operator]
        # P-11B: when the user explicitly chose "authorize the whole
        # directory", store the parent directory + is_directory=True so the
        # matcher's boundary-prefix check covers every file under it. If the
        # parent is too shallow (drive root / one level), fall back to a
        # single-file grant (no over-wide tree). exec commands (not real file
        # paths) never take the directory branch.
        grant_path = existing.resource.identifier
        is_directory = False
        is_program = False
        if grant_range == "directory" and existing.resource.kind != "exec":
            dir_path = _dir_grant_path(grant_path)
            if dir_path is not None:
                grant_path = dir_path
                is_directory = True
        elif grant_range == "program" and existing.resource.kind == "exec":
            # "Permanently allow this whole program": store a normalized binary
            # token (e.g. ``powershell``) instead of the exact command string,
            # so any future command with the same binary matches and the user
            # is not asked again. Uses the same extraction+normalization as the
            # matcher (_exec_binary_token in check_permission — single source of
            # truth documented there). Falls back to exact string if extraction
            # yields nothing (defensive).
            token = _exec_binary_token(grant_path)
            if token:
                grant_path = token
                is_program = True
        try:
            await self._create_grant.execute(  # type: ignore[attr-defined]
                subject=existing.subject,
                path=grant_path,
                mask=existing.requested_mask,
                source=GrantSource.USER,
                expires_at=expires_at,
                scope_kind=scope,
                scope_key=scope_key,
                is_directory=is_directory,
                is_program=is_program,
            )
        except Exception as exc:  # noqa: BLE001 — never block the approval
            logger.debug(
                "security.approve_permission.grant_persist_skipped",
                request_id=existing.request_id.value,
                error=str(exc),
            )

