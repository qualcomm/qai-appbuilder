# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Channel `/grant` slash-command bridge — routes to security session-grants.

PR-097 R-12 deliverable.  Restores the legacy
``backend/channels/wechat/session_commands.py:289-403``
``PolicyCenter.add_session_grant(session_id, op, path)`` shape on
top of the new :mod:`qai.security` :class:`PathGrant` aggregate
without :mod:`qai.channels.*` ever importing :mod:`qai.security.*`
directly (preserves the import-linter ``context-isolation`` contract).

How a channel ``/grant`` is resolved
------------------------------------

1. The dispatch bridge in :mod:`apps.api._channel_dispatch_bridge`
   parses the inbound text into a :class:`Command` and looks up the
   ``"grant"`` verb in its switch table; the handler invokes
   :meth:`ChannelGrantBridge.handle_grant_command`.
2. The bridge looks up the user's active CC/OC session via the
   :class:`~qai.channels.application.ports.SessionIndexRepositoryPort`.
3. With the resolved ``coding_session_id`` we build a
   :class:`~qai.security.domain.value_objects.Subject` with
   ``kind="user"`` and ``identifier=coding_session_id`` — the same
   identity rule used by the legacy
   ``PolicyCenter.add_session_grant``: each CC/OC session owns its
   own grant set so revoking one session does not affect another.
4. The bridge then invokes the security context's
   :class:`~qai.security.application.use_cases.create_path_grant.CreatePathGrantUseCase`
   / :class:`~qai.security.application.use_cases.revoke_path_grant.RevokePathGrantUseCase`
   / :meth:`~qai.security.application.ports.PathGrantRepositoryPort.list_for_subject`
   based on the verb (``grant`` / ``revoke`` / ``list``).
5. A user-facing reply text is returned for the dispatch bridge to
   surface via :class:`RealtimeDeliveryService` (Layer-1 / Layer-2).

Permission op → :class:`~qai.security.domain.value_objects.AceMask` mapping
---------------------------------------------------------------------------

* ``read``  → ``AceMask(read=True)``
* ``write`` → ``AceMask(read=True, write=True)``  (write implies read)
* ``exec``  → ``AceMask(read=True, write=True, execute=True)``
* ``all``   → ``AceMask(read=True, write=True, execute=True, delete=True)``

Mirrors the legacy mapping so prior persistent grants migrated by ops
continue to compare byte-for-byte equivalent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.errors import ApplicationError, NotFoundError
from qai.platform.logging import get_logger

from qai.channels.application.ports import (
    SessionIndexRepositoryPort,
)
from qai.channels.domain import (
    ChannelInstanceId,
    ChannelUserId,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = get_logger(__name__)

__all__ = ["ChannelGrantBridge"]


# Lightweight reply text helpers — kept local to avoid leaking Chinese
# strings into the security context.

_NO_ACTIVE_SESSION = (
    "\u26a0\ufe0f /grant 需要先 /cc 或 /oc 启动会话"
)


def _ace_mask_for_op(op: str) -> Any:
    """Translate a channel op token into an :class:`AceMask`.

    Lazy-imports :class:`AceMask` so this module's import graph stays
    free of :mod:`qai.security` at module load time (defensive — the
    bridge lives under ``apps/api/`` so the import is allowed, but we
    avoid pulling security in for the common bare-import case).
    """
    from qai.security.domain.value_objects import AceMask

    op_lower = op.strip().lower()
    if op_lower == "read":
        return AceMask(read=True)
    if op_lower == "write":
        return AceMask(read=True, write=True)
    if op_lower == "exec":
        return AceMask(read=True, write=True, execute=True)
    if op_lower == "all":
        return AceMask(
            read=True, write=True, execute=True, delete=True
        )
    raise ApplicationError(
        "channels.grant.invalid_op",
        f"unknown grant op {op!r}; expected read / write / exec / all",
    )


def _build_subject(coding_session_id: str) -> Any:
    """Build a per-session :class:`Subject` for grant operations."""
    from qai.security.domain.value_objects import Subject

    return Subject(kind="user", identifier=coding_session_id)


def _format_grant_summary(grants: tuple[Any, ...]) -> str:
    """Render a list of :class:`PathGrant` aggregates as channel text."""
    if not grants:
        return "\u2139\ufe0f 当前会话没有授权的路径。"
    lines = ["\U0001f4cb 当前会话授权列表："]
    for g in grants:
        mask = g.mask
        ops: list[str] = []
        if getattr(mask, "read", False):
            ops.append("read")
        if getattr(mask, "write", False):
            ops.append("write")
        if getattr(mask, "execute", False):
            ops.append("exec")
        if getattr(mask, "delete", False):
            ops.append("delete")
        ops_text = "/".join(ops) if ops else "none"
        lines.append(f"  • {g.path}  ({ops_text})  [{g.grant_id[:8]}]")
    return "\n".join(lines)


class ChannelGrantBridge:
    """Channel ``/grant`` handler that bridges into security session-grants.

    Constructor arguments are passed in by
    :func:`apps.api._channels_di.build_channels_services` — the
    bridge accepts the existing public namespaces so no new field
    name appears on :class:`Container` / :class:`SecurityServices`.

    Args:
        security_services: ``container.security`` namespace exposing
            ``create_path_grant_use_case``,
            ``revoke_path_grant_use_case``,
            ``path_grant_repository`` (read-only port for /list).
        channel_session_index_repo: the channel-side session index;
            used to resolve ``coding_session_id`` from the inbound
            ``(instance_id, user_id)`` pair without crossing into
            the security context.
    """

    __slots__ = ("_security", "_sessions")

    def __init__(
        self,
        *,
        security_services: Any,
        channel_session_index_repo: SessionIndexRepositoryPort,
    ) -> None:
        self._security = security_services
        self._sessions = channel_session_index_repo

    async def _resolve_session_id(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str | None:
        """Look up the user's active CC/OC session via the channel index."""
        entry = await self._sessions.find(instance_id, user_id)
        if entry is None:
            return None
        coding_session_id = getattr(entry, "coding_session_id", None)
        if not coding_session_id:
            return None
        return str(coding_session_id)

    async def handle_grant_command(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        verb: str,
        path: str | None = None,
        op: str | None = None,
    ) -> str:
        """Resolve ``/grant``, ``/grant revoke``, or ``/grant list``.

        Mirrors the three operations exposed by the legacy
        ``PolicyCenter`` for channels:

        * ``grant`` (default) — adds a PathGrant for the active
          CC/OC session.  Requires both ``path`` and ``op``.
        * ``revoke`` — removes an existing PathGrant by ``path``.
        * ``list`` — returns a textual summary of all current grants
          for the active session.

        Args:
            verb: ``"grant"`` / ``"revoke"`` / ``"list"``.
            path: Filesystem path the grant covers
                (``/grant`` and ``/grant revoke`` only).
            op: One of ``"read"`` / ``"write"`` / ``"exec"`` / ``"all"``
                (``/grant`` only).

        Returns:
            User-facing reply text for the channel (Chinese, single
            line for the simple cases, multi-line for /list).  An
            error string with the ⚠️ icon is returned when the
            preconditions fail (no session / invalid op / unknown
            verb) so the dispatch bridge can surface it directly.
        """
        coding_session_id = await self._resolve_session_id(
            instance_id=instance_id, user_id=user_id
        )
        if coding_session_id is None:
            return _NO_ACTIVE_SESSION

        verb_lower = verb.strip().lower()

        if verb_lower in ("list", "ls"):
            return await self._handle_list(
                coding_session_id=coding_session_id
            )
        if verb_lower in ("revoke", "remove", "rm"):
            if not path:
                return (
                    "\u26a0\ufe0f /grant revoke 需要指定路径"
                )
            return await self._handle_revoke(
                coding_session_id=coding_session_id, path=path
            )
        # default verb = "grant" (or any synonym treated as grant)
        if not path:
            return "\u26a0\ufe0f /grant 需要指定路径"
        if not op:
            return (
                "\u26a0\ufe0f /grant 需要指定操作 (read / write / exec)"
            )
        return await self._handle_grant(
            coding_session_id=coding_session_id, path=path, op=op
        )

    async def _handle_grant(
        self,
        *,
        coding_session_id: str,
        path: str,
        op: str,
    ) -> str:
        from qai.security.domain.value_objects import GrantSource

        try:
            mask = _ace_mask_for_op(op)
        except ApplicationError as exc:
            return f"\u26a0\ufe0f {exc!s}"

        create_uc = getattr(
            self._security, "create_path_grant_use_case", None
        )
        if create_uc is None:
            return "\u26a0\ufe0f 安全模块未启用 /grant 功能"
        try:
            await create_uc.execute(
                subject=_build_subject(coding_session_id),
                path=path,
                mask=mask,
                source=GrantSource.USER,
            )
        except ApplicationError as exc:
            return f"\u26a0\ufe0f /grant 失败: {exc!s}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.grant.create_failed",
                coding_session_id=coding_session_id,
                path=path,
                op=op,
                error=str(exc),
            )
            return f"\u26a0\ufe0f /grant 失败: {exc!s}"
        return f"\u2705 已授权: {path} ({op})"

    async def _handle_revoke(
        self,
        *,
        coding_session_id: str,
        path: str,
    ) -> str:
        repo = getattr(self._security, "path_grant_repository", None)
        revoke_uc = getattr(
            self._security, "revoke_path_grant_use_case", None
        )
        if repo is None or revoke_uc is None:
            return "\u26a0\ufe0f 安全模块未启用 /grant 功能"
        subject = _build_subject(coding_session_id)
        grants = await repo.list_for_subject(subject)
        target_id: str | None = None
        for g in grants:
            if g.path == path:
                target_id = g.grant_id
                break
        if target_id is None:
            return f"\u2139\ufe0f 当前会话没有 {path} 的授权。"
        try:
            await revoke_uc.execute(
                grant_id=target_id, revoked_by=subject
            )
        except NotFoundError:
            return f"\u2139\ufe0f 当前会话没有 {path} 的授权。"
        except ApplicationError as exc:
            return f"\u26a0\ufe0f /grant revoke 失败: {exc!s}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.grant.revoke_failed",
                coding_session_id=coding_session_id,
                path=path,
                error=str(exc),
            )
            return f"\u26a0\ufe0f /grant revoke 失败: {exc!s}"
        return f"\u2705 已撤销: {path}"

    async def _handle_list(self, *, coding_session_id: str) -> str:
        repo = getattr(self._security, "path_grant_repository", None)
        if repo is None:
            return "\u26a0\ufe0f 安全模块未启用 /grant 功能"
        try:
            grants = await repo.list_for_subject(
                _build_subject(coding_session_id)
            )
        except ApplicationError as exc:
            return f"\u26a0\ufe0f /grant list 失败: {exc!s}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.grant.list_failed",
                coding_session_id=coding_session_id,
                error=str(exc),
            )
            return f"\u26a0\ufe0f /grant list 失败: {exc!s}"
        return _format_grant_summary(tuple(grants))
