# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`CodingSessionRepositoryPort` (PR-046).

Schema reference: ``qai-db-schema.md`` §4.1 ~ §4.4 (ai_coding_session +
ai_coding_message + ai_coding_permission_request +
ai_coding_tool_invocation).  Replaces the in-memory
``_FakeCodingSessionRepository`` from S3 with a parent + three-child
relational store.

Save semantics — atomic full replacement of the session header and
all child rows within one ``BEGIN IMMEDIATE`` transaction:

    BEGIN IMMEDIATE;
    INSERT OR REPLACE INTO ai_coding_session (...) VALUES (?, ...);
    DELETE FROM ai_coding_message WHERE session_id = ?;
    DELETE FROM ai_coding_permission_request WHERE session_id = ?;
    DELETE FROM ai_coding_tool_invocation WHERE session_id = ?;
    INSERT INTO ai_coding_message (...) VALUES (?, ...);
    INSERT INTO ai_coding_permission_request (...) VALUES (?, ...);
    INSERT INTO ai_coding_tool_invocation (...) VALUES (?, ...);
    COMMIT;

This mirrors :class:`qai.security.adapters.policy_repository.SqlitePolicyRepository`
which collapses parent + rules under one transaction.  The aggregate
is small (≤ a few KB of metadata) so the full-replacement strategy
keeps the adapter ergonomic without sacrificing correctness; SSE
frame payloads are NOT stored on the aggregate (see
:meth:`CodingSession.record_stream_frame`).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionId,
    CodingSessionNotFoundError,
    MessageContent,
    PermissionDecision,
    PermissionRequest,
    PermissionRequestId,
    Provider,
    SessionStatus,
    ToolInvocation,
    ToolInvocationId,
    ToolName,
    Workspace,
)
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteCodingSessionRepository"]


_SESSION_COLUMNS = (
    "id, provider, workspace_path, status, title, last_stream_sequence, "
    "created_at, updated_at, terminated_at, termination_reason, "
    "wechat_notify_user_id, feishu_notify_user_id, last_duration_s, "
    "total_input_tokens, total_output_tokens, total_tool_calls, "
    "last_input_tokens, context_window, total_cost, "
    "oc_current_provider, oc_current_model, "
    "turn_count, last_turn_warning_threshold, "
    "oc_message_ids"
)


class SqliteCodingSessionRepository:
    """aiosqlite implementation of :class:`CodingSessionRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, session_id: CodingSessionId) -> CodingSession:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM ai_coding_session "
                    "WHERE id = ?",
                    (session_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise CodingSessionNotFoundError(
                        message=f"coding session {session_id} not found",
                        details={"session_id": str(session_id)},
                    )
                session = self._row_to_session(row)
                await self._load_children(conn, session)
                return session
        except CodingSessionNotFoundError:
            raise
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.session.get_failed",
                f"failed to load session {session_id}: {exc}",
                operation="coding_session.get",
                cause=exc,
            ) from exc

    async def list_active(self) -> list[CodingSession]:
        return await self._list(
            where_sql="WHERE status != 'terminated'",
            order_sql="ORDER BY updated_at DESC",
            params=(),
            operation="coding_session.list_active",
        )

    async def list_all(self) -> list[CodingSession]:
        return await self._list(
            where_sql="",
            order_sql="ORDER BY created_at ASC",
            params=(),
            operation="coding_session.list_all",
        )

    async def _list(
        self,
        *,
        where_sql: str,
        order_sql: str,
        params: tuple[object, ...],
        operation: str,
    ) -> list[CodingSession]:
        try:
            async with self._db.connection() as conn:
                sql = (
                    f"SELECT {_SESSION_COLUMNS} FROM ai_coding_session "
                    f"{where_sql} {order_sql}"
                )
                cur = await conn.execute(sql, params)
                rows = await cur.fetchall()
                await cur.close()
                sessions = [self._row_to_session(r) for r in rows]
                for s in sessions:
                    await self._load_children(conn, s)
                return sessions
        except Exception as exc:
            raise PersistenceError(
                f"ai_coding.session.{operation.split('.')[-1]}_failed",
                f"failed to list sessions: {exc}",
                operation=operation,
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def save(self, session: CodingSession) -> None:
        now_iso = session.created_at.isoformat()
        # ``updated_at`` follows the latest transition the aggregate has
        # observed; we approximate it with ``terminated_at`` if set,
        # else ``created_at``.  The application layer can refine this
        # by mutating the aggregate prior to ``save`` once the
        # ``last_updated_at`` field is plumbed through the domain.
        updated_at_iso = (
            session.terminated_at.isoformat()
            if session.terminated_at is not None
            else now_iso
        )

        message_params: list[tuple[object, ...]] = []
        for position, message in enumerate(session.messages):
            message_params.append(
                (
                    f"{session.session_id.value}:msg:{position}",
                    session.session_id.value,
                    message.text,
                    position,
                    now_iso,
                )
            )

        permission_params: list[tuple[object, ...]] = []
        for request in session.permission_requests.values():
            permission_params.append(
                (
                    request.request_id.value,
                    session.session_id.value,
                    request.tool_name.value,
                    json.dumps(request.args, sort_keys=True),
                    request.decision.value,
                    request.requested_at.isoformat(),
                    (
                        request.decided_at.isoformat()
                        if request.decided_at is not None
                        else None
                    ),
                )
            )

        invocation_params: list[tuple[object, ...]] = []
        for invocation in session.tool_invocations.values():
            invocation_params.append(
                (
                    invocation.invocation_id.value,
                    session.session_id.value,
                    invocation.tool_name.value,
                    json.dumps(invocation.args, sort_keys=True),
                    invocation.status,
                    invocation.started_at.isoformat(),
                    (
                        invocation.finished_at.isoformat()
                        if invocation.finished_at is not None
                        else None
                    ),
                    invocation.duration_ms,
                    (
                        json.dumps(invocation.result, sort_keys=True)
                        if invocation.result is not None
                        else None
                    ),
                    invocation.error_code,
                )
            )

        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO ai_coding_session "
                        f"({_SESSION_COLUMNS}) VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "provider=excluded.provider, "
                        "workspace_path=excluded.workspace_path, "
                        "status=excluded.status, "
                        "title=excluded.title, "
                        "last_stream_sequence=excluded.last_stream_sequence, "
                        "updated_at=excluded.updated_at, "
                        "terminated_at=excluded.terminated_at, "
                        "termination_reason=excluded.termination_reason, "
                        "wechat_notify_user_id=excluded.wechat_notify_user_id, "
                        "feishu_notify_user_id=excluded.feishu_notify_user_id, "
                        "last_duration_s=excluded.last_duration_s, "
                        "total_input_tokens=excluded.total_input_tokens, "
                        "total_output_tokens=excluded.total_output_tokens, "
                        "total_tool_calls=excluded.total_tool_calls, "
                        "last_input_tokens=excluded.last_input_tokens, "
                        "context_window=excluded.context_window, "
                        "total_cost=excluded.total_cost, "
                        "oc_current_provider=excluded.oc_current_provider, "
                        "oc_current_model=excluded.oc_current_model, "
                        "turn_count=excluded.turn_count, "
                        "last_turn_warning_threshold="
                        "excluded.last_turn_warning_threshold, "
                        "oc_message_ids=excluded.oc_message_ids",
                        (
                            session.session_id.value,
                            session.provider.value,
                            session.workspace.path,
                            session.status.value,
                            session.title,
                            session.last_stream_sequence,
                            session.created_at.isoformat(),
                            updated_at_iso,
                            (
                                session.terminated_at.isoformat()
                                if session.terminated_at is not None
                                else None
                            ),
                            session.termination_reason,
                            session.wechat_notify_user_id,
                            session.feishu_notify_user_id,
                            session.last_duration_s,
                            session.total_input_tokens,
                            session.total_output_tokens,
                            session.total_tool_calls,
                            session.last_input_tokens,
                            session.context_window,
                            session.total_cost,
                            session.oc_current_provider,
                            session.oc_current_model,
                            session.turn_count,
                            session.last_turn_warning_threshold,
                            (
                                json.dumps(list(session.oc_message_ids))
                                if session.oc_message_ids
                                else None
                            ),
                        ),
                    )
                    # Replace children atomically so a partial save
                    # never leaves the aggregate inconsistent.
                    for table in (
                        "ai_coding_message",
                        "ai_coding_permission_request",
                        "ai_coding_tool_invocation",
                    ):
                        await conn.execute(
                            f"DELETE FROM {table} WHERE session_id = ?",
                            (session.session_id.value,),
                        )
                    if message_params:
                        await conn.executemany(
                            "INSERT INTO ai_coding_message "
                            "(id, session_id, text, position, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            message_params,
                        )
                    if permission_params:
                        await conn.executemany(
                            "INSERT INTO ai_coding_permission_request "
                            "(id, session_id, tool_name, args_json, "
                            "decision, requested_at, decided_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            permission_params,
                        )
                    if invocation_params:
                        await conn.executemany(
                            "INSERT INTO ai_coding_tool_invocation "
                            "(id, session_id, tool_name, args_json, status, "
                            "started_at, finished_at, duration_ms, "
                            "result_json, error_code) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            invocation_params,
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.session.save_failed",
                f"failed to save session {session.session_id}: {exc}",
                operation="coding_session.save",
                cause=exc,
            ) from exc

    async def delete(self, session_id: CodingSessionId) -> None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM ai_coding_session WHERE id = ?",
                    (session_id.value,),
                )
                rowcount = cur.rowcount
                await cur.close()
                await conn.commit()
                if rowcount == 0:
                    raise CodingSessionNotFoundError(
                        message=f"coding session {session_id} not found",
                        details={"session_id": str(session_id)},
                    )
        except CodingSessionNotFoundError:
            raise
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.session.delete_failed",
                f"failed to delete session {session_id}: {exc}",
                operation="coding_session.delete",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Children loaders
    # ------------------------------------------------------------------
    @staticmethod
    async def _load_children(conn, session: CodingSession) -> None:
        # Messages — ordered by position to preserve user-input order.
        cur = await conn.execute(
            "SELECT text FROM ai_coding_message "
            "WHERE session_id = ? ORDER BY position ASC",
            (session.session_id.value,),
        )
        msg_rows = await cur.fetchall()
        await cur.close()
        for row in msg_rows:
            session.messages.append(MessageContent(text=str(row[0])))

        # Permission requests
        cur = await conn.execute(
            "SELECT id, tool_name, args_json, decision, "
            "requested_at, decided_at "
            "FROM ai_coding_permission_request "
            "WHERE session_id = ? ORDER BY requested_at ASC",
            (session.session_id.value,),
        )
        perm_rows = await cur.fetchall()
        await cur.close()
        for row in perm_rows:
            request_id = PermissionRequestId(value=str(row[0]))
            decided_at = (
                datetime.fromisoformat(str(row[5]))
                if row[5] is not None
                else None
            )
            session.permission_requests[request_id] = PermissionRequest(
                request_id=request_id,
                tool_name=ToolName(value=str(row[1])),
                args=json.loads(str(row[2] or "{}")),
                requested_at=datetime.fromisoformat(str(row[4])),
                decision=PermissionDecision(str(row[3])),
                decided_at=decided_at,
            )

        # Tool invocations
        cur = await conn.execute(
            "SELECT id, tool_name, args_json, status, started_at, "
            "finished_at, duration_ms, result_json, error_code "
            "FROM ai_coding_tool_invocation "
            "WHERE session_id = ? ORDER BY started_at ASC",
            (session.session_id.value,),
        )
        inv_rows = await cur.fetchall()
        await cur.close()
        for row in inv_rows:
            invocation_id = ToolInvocationId(value=str(row[0]))
            finished_at = (
                datetime.fromisoformat(str(row[5]))
                if row[5] is not None
                else None
            )
            duration_ms = int(row[6]) if row[6] is not None else None
            result = (
                json.loads(str(row[7])) if row[7] is not None else None
            )
            error_code = str(row[8]) if row[8] is not None else None
            session.tool_invocations[invocation_id] = ToolInvocation(
                invocation_id=invocation_id,
                tool_name=ToolName(value=str(row[1])),
                args=json.loads(str(row[2] or "{}")),
                started_at=datetime.fromisoformat(str(row[4])),
                status=str(row[3]),
                finished_at=finished_at,
                duration_ms=duration_ms,
                result=result,
                error_code=error_code,
            )

    # ------------------------------------------------------------------
    # Row helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_session(row: tuple[object, ...]) -> CodingSession:
        session_id = CodingSessionId(value=str(row[0]))
        provider = Provider(str(row[1]))
        workspace = Workspace(path=str(row[2]))
        status = SessionStatus(str(row[3]))
        title = str(row[4]) if row[4] is not None else None
        last_seq = int(row[5])
        created_at = datetime.fromisoformat(str(row[6]))
        terminated_at = (
            datetime.fromisoformat(str(row[8])) if row[8] is not None else None
        )
        termination_reason = str(row[9]) if row[9] is not None else None
        wechat_notify_user_id = str(row[10]) if row[10] is not None else None
        feishu_notify_user_id = str(row[11]) if row[11] is not None else None
        last_duration_s = float(row[12]) if row[12] is not None else None
        # U-010 / 2-H2: cumulative token / context counters (migration 024).
        # Columns are NOT NULL DEFAULT 0, but a row written before the
        # migration ran (then back-filled) could surface NULL through an
        # outer query — coalesce defensively to the aggregate's zero
        # default so loads never crash on legacy rows.
        total_input_tokens = int(row[13]) if row[13] is not None else 0
        total_output_tokens = int(row[14]) if row[14] is not None else 0
        total_tool_calls = int(row[15]) if row[15] is not None else 0
        last_input_tokens = int(row[16]) if row[16] is not None else 0
        context_window = int(row[17]) if row[17] is not None else 0
        total_cost = float(row[18]) if row[18] is not None else 0.0
        # 2-H10: OpenCode provider/model selection (migration 025).  NULL
        # on legacy rows / non-OC sessions → the aggregate's None default.
        oc_current_provider = str(row[19]) if row[19] is not None else None
        oc_current_model = str(row[20]) if row[20] is not None else None
        # 2-H12: turn-count + over-turn-warning bookkeeping (migration 026).
        turn_count = int(row[21]) if row[21] is not None else 0
        last_turn_warning_threshold = int(row[22]) if row[22] is not None else 0
        # 2-H3 / RE-OC-7: OpenCode native message ids (migration 033).  JSON
        # array on disk; NULL on legacy rows / non-OC sessions → empty tuple.
        oc_message_ids: tuple[str, ...] = ()
        if len(row) > 23 and row[23] is not None:
            try:
                parsed = json.loads(str(row[23]))
                if isinstance(parsed, list):
                    oc_message_ids = tuple(str(m) for m in parsed if m)
            except (ValueError, TypeError):
                oc_message_ids = ()
        return CodingSession(
            session_id=session_id,
            provider=provider,
            workspace=workspace,
            created_at=created_at,
            status=status,
            title=title,
            last_stream_sequence=last_seq,
            terminated_at=terminated_at,
            termination_reason=termination_reason,
            wechat_notify_user_id=wechat_notify_user_id,
            feishu_notify_user_id=feishu_notify_user_id,
            last_duration_s=last_duration_s,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tool_calls=total_tool_calls,
            last_input_tokens=last_input_tokens,
            context_window=context_window,
            total_cost=total_cost,
            oc_current_provider=oc_current_provider,
            oc_current_model=oc_current_model,
            turn_count=turn_count,
            last_turn_warning_threshold=last_turn_warning_threshold,
            oc_message_ids=oc_message_ids,
        )
