# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Conversation workspace → auto session-grant bridge (apps/api wiring root).

SEC true-scoping (PART E). When a user sets a conversation's working
directory (``PATCH /api/chat/conversations/{id}/workspace``) the AI should be
able to read / write under that directory for THAT conversation without being
re-prompted — but only for that conversation (session isolation). This module
auto-creates a ``session``-scoped :class:`PathGrant` for the workspace path
keyed by the conversation id.

Why here (apps/api layer)
-------------------------
``qai.chat`` must NOT import ``qai.security`` (the ``context-isolation``
import-linter contract). The set-workspace route lives in the ``chat`` HTTP
interface but the container it captures exposes BOTH namespaces, so this
bridge — living under ``apps/api``, the one layer allowed to depend on
multiple bounded contexts, exactly like ``_file_guard_bridge`` /
``_permission_bridge`` — is invoked by the route with the container. The chat
context never names ``qai.security``; only this apps-layer seam does.

Scope semantics
---------------
* ``scope_kind="session"`` + ``scope_key=<conversation_id>`` — the grant only
  matches while the caller is in that conversation (``matches_scope`` in the
  security domain), so setting a workspace on conversation *A* never
  authorises writes for conversation *B*.
* ``mask=read+write+execute`` — the AI needs read/write to work in the
  directory and execute so tools launched from the workspace (and the
  workspace subtree, covered by the session-aware prefix provider below)
  run without a re-prompt for THIS conversation.
* ``source=AUTO`` — distinguishes it from an operator-created grant in the
  audit / grants UI.
* ``expires_at=None`` — the ``session`` scope_kind is the real lifetime gate
  (the grant stops matching once the conversation context is gone / a new
  process boots); no TTL is needed on top.

Best-effort
-----------
Setting the same workspace twice must not error, so a
:class:`PathGrantConflictError` (a same-scope grant for this
subject+path+conversation already exists) is swallowed. Any other failure is
logged and swallowed too — a security-grant hiccup must never fail the
user's set-workspace action (the AI simply re-prompts on first access, the
pre-existing behaviour).

Revoke-on-change
----------------
On set we first REVOKE any stale AUTO session-workspace grants this
conversation previously created (same subject + ``scope_kind="session"`` +
``scope_key=<conversation_id>`` + ``source=AUTO``) whose path differs from the
new workspace, THEN create the fresh grant. This closes the
``SEC-WORKSPACE-GRANT-REVOKE-1`` gap: changing or clearing a conversation's
workspace no longer leaves the AI with write access to the previously-chosen
directory. Revocation is scoped narrowly — only this conversation's own AUTO
session grants are touched; operator-created (``source != AUTO``) grants and
permanent/process grants are never revoked. Best-effort: a revoke hiccup is
logged and swallowed (it must never fail the user's set-workspace action).
Clearing the workspace (``workspace=""``) revokes the stale grant(s) and
creates nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["ensure_workspace_session_grant"]

_log = get_logger(__name__)

#: The global subject the ai_coding tool layer acts as (matches the identity
#: ``_file_guard_bridge`` / ``_native_hook_bridge`` attribute file ops to, so
#: a grant created for it is the one those gates consult).
_AI_CODING_SUBJECT_IDENTIFIER = "ai_coding.tool"


async def ensure_workspace_session_grant(
    container: "Container",
    *,
    conversation_id: str,
    workspace: str,
) -> bool:
    """Auto-create a session-scoped read+write+execute grant for ``workspace``.

    First revokes any STALE AUTO session-workspace grant this conversation
    previously created for a DIFFERENT path (revoke-on-change), then creates
    the fresh grant. When ``workspace`` is empty (the user cleared it), the
    stale grant(s) are revoked and nothing is created.

    Returns ``True`` when a (new) grant was created, ``False`` when skipped
    (no security namespace / cleared workspace / conflict / any error). Never
    raises — the caller's set-workspace action must succeed regardless.
    """
    workspace = (workspace or "").strip()
    if not conversation_id:
        return False

    security = getattr(container, "security", None)
    create_grant = (
        getattr(security, "create_path_grant_use_case", None)
        if security is not None
        else None
    )
    if create_grant is None:
        return False

    # apps/api layer — importing qai.security VOs here is allowed (this is the
    # cross-context wiring seam; the chat context never sees these names).
    try:
        from qai.security.domain.errors import PathGrantConflictError
        from qai.security.domain.value_objects import (
            AceMask,
            GrantSource,
            Subject,
        )
    except Exception:  # noqa: BLE001 — security VOs unavailable → skip
        return False

    subject = Subject(kind="system", identifier=_AI_CODING_SUBJECT_IDENTIFIER)

    # Revoke-on-change: drop this conversation's prior AUTO session-workspace
    # grant(s) for any OTHER path before (re)granting. Best-effort.
    await _revoke_stale_workspace_grants(
        container,
        subject=subject,
        conversation_id=conversation_id,
        keep_path=workspace,
    )

    # Cleared workspace → nothing to grant (stale grants already revoked).
    if not workspace:
        return False

    try:
        await create_grant.execute(
            subject=subject,
            path=workspace,
            mask=AceMask(read=True, write=True, execute=True),
            source=GrantSource.AUTO,
            expires_at=None,
            scope_kind="session",
            scope_key=conversation_id,
        )
        _log.info(
            "workspace_session_grant.created",
            conversation_id=conversation_id,
            path=workspace,
        )
        return True
    except PathGrantConflictError:
        # Same workspace set twice for this conversation — already authorised.
        _log.debug(
            "workspace_session_grant.conflict_ignored",
            conversation_id=conversation_id,
            path=workspace,
        )
        return False
    except Exception:  # noqa: BLE001 — never fail the set-workspace action
        _log.warning(
            "workspace_session_grant.create_failed",
            conversation_id=conversation_id,
            exc_info=True,
        )
        return False


async def _revoke_stale_workspace_grants(
    container: "Container",
    *,
    subject: object,
    conversation_id: str,
    keep_path: str,
) -> None:
    """Revoke this conversation's AUTO session-workspace grants != ``keep_path``.

    Narrowly scoped: only grants matching (this subject) AND
    ``scope_kind == "session"`` AND ``scope_key == conversation_id`` AND
    ``source == AUTO`` AND ``path != keep_path`` are revoked. Operator grants
    (``source != AUTO``) and permanent/process grants are never touched. The
    current workspace path (``keep_path``) is preserved so re-setting the same
    directory is a no-op rather than a revoke+recreate churn. Best-effort:
    listing/revoke failures are logged and swallowed.
    """
    security = getattr(container, "security", None)
    repo = getattr(security, "path_grant_repository", None)
    revoke = getattr(security, "revoke_path_grant_use_case", None)
    if repo is None or revoke is None:
        return
    try:
        from qai.security.domain.value_objects import GrantSource
    except Exception:  # noqa: BLE001
        return
    try:
        grants = await repo.list_for_subject(subject)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — never fail set-workspace on a list error
        _log.warning(
            "workspace_session_grant.list_failed",
            conversation_id=conversation_id,
            exc_info=True,
        )
        return
    for grant in grants:
        if (
            getattr(grant, "scope_kind", None) == "session"
            and getattr(grant, "scope_key", None) == conversation_id
            and getattr(grant, "source", None) == GrantSource.AUTO
            and getattr(grant, "path", None) != keep_path
        ):
            try:
                await revoke.execute(grant_id=grant.grant_id)
                _log.info(
                    "workspace_session_grant.revoked_stale",
                    conversation_id=conversation_id,
                    path=getattr(grant, "path", ""),
                )
            except Exception:  # noqa: BLE001 — best-effort per grant
                _log.warning(
                    "workspace_session_grant.revoke_failed",
                    conversation_id=conversation_id,
                    grant_id=getattr(grant, "grant_id", "?"),
                    exc_info=True,
                )
