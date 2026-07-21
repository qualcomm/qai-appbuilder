# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for the ai_coding bounded context.

Every external dependency of the use cases — provider subprocess,
session storage, workspace locking, skill registry, tool bridge,
permission policy — is expressed here as a :class:`typing.Protocol`.
Concrete adapters live under ``src/qai/ai_coding/adapters/`` (S4,
PR-045+) or are injected from other contexts (security via
:class:`PermissionDecisionPort` in PR-046).

Cross-context isolation
-----------------------
The ``.importlinter`` ``context-isolation`` contract forbids
``qai.ai_coding`` from importing ``qai.security.*`` and
``qai.tools.*``.  All cross-context collaboration therefore flows
through these ports:

* :class:`ToolBridgePort` replaces the legacy direct call into
  ``backend/tools/registry.py``.
* :class:`PermissionDecisionPort` replaces the legacy direct call into
  ``backend/security/permission_engine.py``.
* :class:`SkillRegistryPort` replaces the legacy direct read of
  ``backend/security/skill_policy.py``.

All async ports use ``asyncio`` semantics; the streaming port returns
an :class:`AsyncIterator` so adapters can back it with an SSE socket,
a subprocess pipe, or an in-memory queue without leaking
implementation details to the use cases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    MessageContent,
    PermissionDecision,
    PermissionRequest,
    Provider,
    Skill,
    ToolName,
    Workspace,
)

__all__ = [
    "CheckpointRecord",
    "CheckpointRepositoryPort",
    "ClaudeMdInjectorPort",
    "CodingConfigRepositoryPort",
    "CodingProviderPort",
    "CodingSessionRepositoryPort",
    "FileBrokerPort",
    "FileGuardPort",
    "OcServicePort",
    "OcServiceStatus",
    "PermissionDecisionPort",
    "SkillRegistryPort",
    "ToolBridgePort",
    "ToolBridgeResult",
    "ToolResultPreview",
    "ToolResultStorePort",
    "WorkspaceLockHandle",
    "WorkspaceLockPort",
]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
@runtime_checkable
class CodingProviderPort(Protocol):
    """Adapter for spawning, streaming and stopping a backend agent.

    A single :class:`CodingProviderPort` instance abstracts both Claude
    Code and OpenCode — concrete adapters select internally based on
    :attr:`Provider`.  Some deployments register *one* provider only
    (e.g. external edition without OC); the ``available_providers``
    method lets the use case fail fast with
    :class:`qai.ai_coding.domain.ProviderNotAvailableError`.
    """

    def available_providers(self) -> Iterable[Provider]:
        """Return the providers this adapter can currently honour."""
        ...

    async def spawn(
        self,
        *,
        provider: Provider,
        workspace: Workspace,
        initial_prompt: MessageContent | None,
        session_id: CodingSessionId | None = None,
        config: CodingSessionConfig | None = None,
    ) -> dict[str, Any]:
        """Start a backend session and return adapter-specific handles.

        The returned mapping is opaque to the domain layer; the adapter
        is expected to remember internally how to map a session id to
        its live handle.  The use case stores nothing of the dict on
        the aggregate.

        ``session_id`` and ``config`` (PR-107) let the use case
        pre-allocate the aggregate id BEFORE calling spawn so the
        adapter can stash the SDK 12-item config under the session
        handle for the streaming loop to consume.  Both default to
        ``None`` to preserve the PR-046 contract (call sites that don't
        ferry the new config keep working unchanged).
        """
        ...

    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        """Yield stream frames emitted by the backend.

        The iterator MUST terminate (rather than block forever) when
        the session reaches a quiescent or terminated state.  Adapters
        propagate cancellation by re-raising ``asyncio.CancelledError``.
        """
        ...

    async def send_message(
        self,
        *,
        session_id: CodingSessionId,
        content: MessageContent,
    ) -> None:
        """Forward a user message to a running session."""
        ...

    async def terminate(self, *, session_id: CodingSessionId) -> None:
        """Stop the backend process for ``session_id``.

        Idempotent — the adapter should treat "already gone" as success.
        """
        ...

    async def abort(self, *, session_id: CodingSessionId) -> bool:
        """Hard-abort the in-flight turn for ``session_id``.

        2-H4 (OC native abort).  Distinct from :meth:`terminate` (which
        tears down the local handle): ``abort`` asks the *upstream*
        provider to cancel the current turn.  The OpenCode adapter
        overrides this to call the native ``POST /session/{id}/abort``
        endpoint (V1 ``opencode_session_manager.abort_session`` parity);
        the base / Claude Code adapter falls back to :meth:`terminate`
        (CC has no separate native abort — the legacy CC path used the
        SDK interrupt, mirrored by the application-layer soft interrupt).

        Returns ``True`` when an upstream abort was issued, ``False``
        when the adapter only performed the local terminate fallback.
        Best-effort — implementations MUST NOT raise on a provider-side
        failure (the use case logs and continues).
        """
        ...

    async def rewind_files(
        self,
        *,
        session_id: CodingSessionId,
        marker_index: int,
    ) -> bool:
        """Restore the workspace files to a prior checkpoint, provider-natively.

        2-H3 (rewind file restoration).  V1 restored project files on a
        rewind via the *provider's own* native API — Claude Code's
        ``ClaudeSDKClient.rewind_files(sdk_uuid)``
        (``session_manager.py:2604-2695``) and OpenCode's
        ``POST /session/{id}/revert`` (``opencode_session_manager.py
        :1138-1168``) — NOT via a cross-context project_snapshot call.
        V2 therefore exposes this as a provider-port hook so the
        :class:`RewindCheckpointUseCase` can ask the owning adapter to
        roll back the on-disk files after it truncates the message
        history, keeping the whole flow inside the ai_coding context
        (no ``qai.project_snapshot`` import — the
        ``context-isolation`` contract stays clean).

        ``marker_index`` is the 0-based user-message index the history
        was rewound to (the same anchor
        :meth:`CodingSession.truncate_history_after` used) so the
        adapter can map it to its native checkpoint / message handle.

        Returns ``True`` when an upstream file rewind was issued,
        ``False`` when the adapter performed no file restoration (no
        native support wired, feature disabled, or unknown session).
        Best-effort — implementations MUST NOT raise; a provider-side
        failure degrades to message-only rewind.
        """
        ...

    async def fork_session(self, *, session_id: CodingSessionId) -> bool:
        """Fork the upstream conversation onto a fresh backend session.

        2-H6 (fork_session — real new session).  V1 set
        ``session.fork_session = True`` so the *next* turn dropped the
        cached upstream id and the SDK forked a brand-new backend
        conversation seeded from the prior context
        (``session_manager.py:1663-1669,2097-2100``).  V2 lifts this to
        an explicit provider hook the :class:`RestoreCodingSessionUseCase`
        calls when ``fork=True``: the adapter drops the cached upstream
        session id (CC ``claude_session_id`` / OC ``oc_session_id``) so
        the next :meth:`stream` lazily creates a new upstream session
        instead of resuming the old one.

        Returns ``True`` when a cached upstream id was cleared (a real
        fork will happen next turn), ``False`` when there was nothing to
        fork (no live handle / no cached upstream id yet — the next turn
        already starts fresh).  Best-effort — MUST NOT raise.
        """
        ...


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
@runtime_checkable
class CodingSessionRepositoryPort(Protocol):
    """Persistence port for the :class:`CodingSession` aggregate.

    PR-026 owns the actual SQLite schema; this Protocol only defines
    the surface the application layer needs.
    """

    async def save(self, session: CodingSession) -> None: ...

    async def get(self, session_id: CodingSessionId) -> CodingSession:
        """Return the persisted aggregate.

        Raises ``CodingSessionNotFoundError`` if the id is unknown.
        """
        ...

    async def list_active(self) -> list[CodingSession]:
        """Return all sessions whose status is not ``TERMINATED``."""
        ...

    async def list_all(self) -> list[CodingSession]:
        """Return every session, including terminated ones (history view)."""
        ...

    async def delete(self, session_id: CodingSessionId) -> None:
        """Hard-delete the session row.

        Used by ``DELETE /api/cc/sessions/{id}/permanent``.
        Raises ``CodingSessionNotFoundError`` if the id is unknown.
        """
        ...


# ---------------------------------------------------------------------------
# Workspace lock
# ---------------------------------------------------------------------------
@runtime_checkable
class WorkspaceLockHandle(Protocol):
    """A handle returned by :class:`WorkspaceLockPort.acquire`.

    The handle is a context manager (sync) so use cases can ``with``
    it within a single coroutine; alternatively they call
    :meth:`release` directly when ownership is transferred to a
    long-lived session.
    """

    @property
    def workspace(self) -> Workspace: ...

    async def release(self) -> None: ...


@runtime_checkable
class WorkspaceLockPort(Protocol):
    """Mutual exclusion over a workspace path."""

    async def acquire(self, workspace: Workspace) -> WorkspaceLockHandle:
        """Acquire exclusive access.

        Implementations MUST raise
        :class:`qai.ai_coding.domain.WorkspaceLockedError` if the
        workspace is already held.  They MUST NOT block indefinitely.
        """
        ...

    async def release(self, workspace: Workspace) -> None:
        """Release whatever is currently holding the workspace.

        Idempotent — a no-op if nothing is held.
        """
        ...


# ---------------------------------------------------------------------------
# Skill registry
# ---------------------------------------------------------------------------
@runtime_checkable
class SkillRegistryPort(Protocol):
    """Skill registration / lookup.

    Mirrors the legacy ``backend/security/skill_policy.py`` capability
    without the security coupling: this port only knows about
    *advertised* skills, not who is allowed to invoke them.
    """

    async def register(self, skill: Skill) -> None: ...

    async def list_skills(self) -> list[Skill]: ...

    async def get(self, name: str) -> Skill:
        """Return the skill named ``name``.

        Raises :class:`qai.ai_coding.domain.SkillNotRegisteredError`
        if the skill is unknown.
        """
        ...


# ---------------------------------------------------------------------------
# Tool bridge
# ---------------------------------------------------------------------------
class ToolBridgeResult:
    """Plain dataclass-like wrapper for tool invocation outcomes.

    Defined as a regular class (not a ``@dataclass``) to keep the
    ``ports.py`` module dependency-light; concrete adapters in S4 may
    pivot to a richer type without touching the domain.
    """

    __slots__ = ("error_code", "ok", "result")

    def __init__(
        self,
        *,
        ok: bool,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        if ok and error_code is not None:
            raise ValueError("ok=True must not carry error_code")
        if not ok and error_code is None:
            raise ValueError("ok=False requires error_code")
        self.ok: bool = ok
        self.result: dict[str, Any] | None = result
        self.error_code: str | None = error_code


@runtime_checkable
class ToolBridgePort(Protocol):
    """Bridge to the tools bounded context.

    The use case calls :meth:`invoke` after permission has been
    granted; the adapter is responsible for translating the call into
    whatever the ``qai.tools`` context exposes (in S4 this becomes the
    actual ``ToolRegistryPort`` from PR-040+).
    """

    async def invoke(
        self,
        *,
        tool_name: ToolName,
        args: dict[str, Any],
    ) -> ToolBridgeResult: ...


# ---------------------------------------------------------------------------
# Permission decision (security context bridge)
# ---------------------------------------------------------------------------
@runtime_checkable
class PermissionDecisionPort(Protocol):
    """Policy hook that decides whether a tool call is auto-approved.

    The default adapter (S4 PR-046) returns ``PENDING`` so the request
    surfaces to the user; a more aggressive policy (e.g. "allow all
    file reads under the workspace") can short-circuit by returning
    ``APPROVED`` immediately.

    This port is the reason ``qai.ai_coding`` does NOT depend on
    ``qai.security`` directly: the security context implements the
    port and the application root wires it in.
    """

    async def evaluate(
        self,
        *,
        request: PermissionRequest,
        workspace: Workspace,
    ) -> PermissionDecision: ...


# ---------------------------------------------------------------------------
# File guard (security context bridge for tool execution; PR-101)
# ---------------------------------------------------------------------------
@runtime_checkable
class FileGuardPort(Protocol):
    """Per-operation security gate consulted by ai_coding production tools.

    Mirrors the legacy ``backend/tools/_security.py`` ``_enforce_*``
    family without taking a hard dependency on ``qai.security`` from
    inside ``qai.ai_coding`` (which the ``context-isolation``
    importlinter contract forbids).  The default adapter
    (``NoopFileGuard`` in ``qai.ai_coding.infrastructure.tools``) is a
    pass-through used by tests and by deployments where security is
    handled out-of-band; the production wiring composes a
    ``PolicyCenter``-backed adapter at the application root via the
    bridge in ``apps/api/_permission_bridge.py``, which keeps the
    cross-context dependency one-way (``apps/api/`` → ``qai.security``)
    without ``qai.ai_coding`` ever importing the security context.

    All methods are async to keep parity with ``PolicyCenter.ask_user``
    which can block waiting for a UI authorisation popup; in-process
    static deny / allow paths simply return immediately.
    """

    async def enforce_read(self, *, path: str, caller: str) -> None:
        """Raise ``ToolGuardDenied`` if ``path`` is not readable.

        ``caller`` is a free-form audit string (e.g. ``"tool.read"``)
        used by the audit log when a real PolicyCenter is wired in.
        """
        ...

    async def enforce_write(self, *, path: str, caller: str) -> None:
        """Raise ``ToolGuardDenied`` if ``path`` is not writable."""
        ...

    async def enforce_delete(self, *, path: str, caller: str) -> None:
        """Raise ``ToolGuardDenied`` if ``path`` cannot be deleted.

        Delete is a mutating op and shares the write-grant decision path
        with :meth:`enforce_write`: the security answer is identical
        (a caller that could write the path can also delete it). What
        differs is the *audit* trail — this call records ``op="delete"``
        + the :class:`AceMask.delete` bit so the audit query can tell a
        delete apart from an in-place overwrite on the same path
        (SEC-ENHANCE-AUDITUX-1). Adapters that don't care about audit
        granularity (``NoopFileGuard``) may implement it as a pass-through
        aliased to :meth:`enforce_write`.
        """
        ...

    async def enforce_exec(
        self, *, command: str, cwd: str | None, caller: str
    ) -> None:
        """Raise ``ToolGuardDenied`` if ``command`` is not allowed."""
        ...

    async def enforce_project_access(
        self, *, path: str, operation: str
    ) -> None:
        """Raise ``ToolGuardDenied`` if project-access toggle blocks ``path``.

        Called by every file-touching tool BEFORE
        :meth:`enforce_read` / :meth:`enforce_write`.  Matches the
        legacy ordering in ``backend/tools/_security.py``.
        """
        ...

    # ------------------------------------------------------------------
    # Per-file read probes (non-raising) — restore V1 glob/grep
    # per-file / per-line FileGuard filtering (退化 #10).
    # ------------------------------------------------------------------
    # These two methods are OPTIONAL (§3.1 tail-append): the production
    # ``FileGuardFacade`` bridge implements them, while light test
    # stubs / ``NoopFileGuard`` may omit them.  Callers
    # (``handlers/search.py``) therefore probe via ``getattr`` and skip
    # per-file filtering gracefully when absent — preserving backward
    # compatibility with hand-rolled stubs that only ship the four
    # ``enforce_*`` gates.

    async def is_read_allowed(self, *, path: str) -> bool:
        """Return ``True`` iff ``path`` is currently readable (non-raising).

        Mirrors V1 ``PolicyCenter.check_read(path) == Decision.ALLOW``
        (``backend/tools/_glob.py:302-314`` / ``_grep.py:143-153``).
        Used by ``glob`` / ``grep`` to drop individual matches the read
        allowlist excludes WITHOUT raising (unlike :meth:`enforce_read`).

        MUST be fail-open: when evaluation errors or no PolicyCenter is
        wired, return ``True`` so a probe failure never hides a file the
        user is actually allowed to see (V1 ``allowed=True`` on except,
        ``_grep.py:151``).  When the FileGuard master switch is OFF the
        implementation returns ``True`` (V1 ``enabled=false → ALLOW``).
        """
        ...

    async def is_statically_allowed(self, *, path: str) -> bool:
        """Return ``True`` iff ``path`` is allowed by the *static* allowlist.

        Mirrors V1 ``PolicyCenter.explain_read(path) == Decision.ALLOW``
        (``backend/tools/_glob.py:293-294``).  ``glob`` uses this on the
        search *root* to decide whether per-file filtering applies:

        * static ALLOW (this returns ``True``) → the root is in the
          static read allowlist, so per-file :meth:`is_read_allowed`
          filtering runs (a whitelisted dir may still contain excluded
          sub-paths);
        * otherwise (ASK / dynamic authorisation → this returns
          ``False``) → the user explicitly authorised the whole tree, so
          per-file filtering is SKIPPED to avoid over-filtering a
          directory the user already granted.

        Fail-open semantics match :meth:`is_read_allowed`; when the
        master switch is OFF the implementation returns ``True``.
        """
        ...


# ---------------------------------------------------------------------------
# File broker (optional always_exclude / max_entries; PR-101)
# ---------------------------------------------------------------------------
@runtime_checkable
class FileBrokerPort(Protocol):
    """Optional pre/post hook around file-touching tool calls.

    Mirrors the legacy ``backend/tools/file_broker.py`` capability:

    * pre-call: raise ``ToolGuardDenied`` for paths matching an
      ``always_exclude`` pattern (e.g. ``.env``, ``node_modules/**``);
    * post-call: truncate ``glob`` / ``grep`` results to ``max_entries``.

    The default adapter (``NoopFileBroker``) is a pass-through;
    ``PatternFileScreen`` adds the exclude + truncation behaviour.

    The contract is intentionally narrow: only the path-extraction and
    truncation hooks are exposed (the broker may or may not change
    ``args``).
    """

    async def pre_call(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Return possibly-mutated ``args`` and raise on excluded paths."""
        ...

    async def post_call(
        self,
        *,
        tool_name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return possibly-truncated ``result`` for glob/grep tools."""
        ...


# ---------------------------------------------------------------------------
# Tool result storage (parity with legacy backend/tool_result_storage.py)
# ---------------------------------------------------------------------------
class ToolResultPreview:
    """Outcome of :meth:`ToolResultStorePort.store`.

    Defined as a regular class (not a ``@dataclass``) to keep the
    ``ports.py`` module dependency-light, matching the
    :class:`ToolBridgeResult` convention above.

    Attributes
    ----------
    preview:
        The redacted text the LLM should see (``head + omit-marker +
        tail`` when truncated; equals the original ``output`` when the
        body was below threshold).
    stored:
        ``True`` when the full body was persisted and is retrievable
        via ``stored_path``.
    stored_path:
        The path the model passes back to ``read`` to recover the full
        body.  ``None`` when nothing was persisted.
    total_bytes / omitted_bytes:
        Decision aids (UTF-8 byte counts) for callers / logs.
    truncated:
        ``True`` when the preview differs from the original output.
    """

    __slots__ = (
        "omitted_bytes",
        "preview",
        "stored",
        "stored_path",
        "total_bytes",
        "truncated",
    )

    def __init__(
        self,
        *,
        preview: str,
        stored: bool,
        stored_path: str | None,
        total_bytes: int,
        omitted_bytes: int,
        truncated: bool,
    ) -> None:
        self.preview: str = preview
        self.stored: bool = stored
        self.stored_path: str | None = stored_path
        self.total_bytes: int = total_bytes
        self.omitted_bytes: int = omitted_bytes
        self.truncated: bool = truncated


@runtime_checkable
class ToolResultStorePort(Protocol):
    """Persist an oversized tool output and render a redacted preview.

    Mirrors the legacy ``backend/tool_result_storage.py`` capability
    that ``backend/tools/_exec.py`` invoked on large outputs: when a
    tool body exceeds a byte threshold the *full* body is persisted to
    a retrievable location and the model is shown a
    ``head + omit-marker + tail`` preview plus a hint that it can
    ``read(path=...)`` the saved file to recover the elided middle.

    This restores the V1 behaviour that the V2 production path lost
    (``tool_exec`` previously hard-truncated and discarded the middle
    with no retrieval path).  The store lives in ``ai_coding`` because
    that is where the tool handlers actually execute; the
    ``layered-ai_coding`` contract keeps the concrete adapter under
    ``infrastructure/`` while this Protocol stays in ``application/``.

    Implementations MUST:

    * return the original ``output`` unchanged (``stored=False``) when
      it is below ``threshold_bytes`` — small outputs are never
      mutated;
    * degrade gracefully to a preview-only result (``stored=False``)
      when persistence fails (disk full / permission denied), never
      tanking an otherwise-successful tool call;
    * persist to a location whose ``stored_path`` the model can pass
      back to the ``read`` tool to recover the full body.
    """

    def store(
        self,
        output: str,
        *,
        tool_name: str = "",
        context_hint: str = "",
        force: bool = False,
    ) -> ToolResultPreview:
        """Persist ``output`` when oversized; return the preview outcome.

        ``force=True`` (§3.1 tail-append, default-False so existing callers
        are unaffected) persists the body REGARDLESS of the byte threshold.
        It exists for callers whose "must give the model a retrieval path"
        decision is driven by a NON-byte signal — e.g. ``grep`` capping at a
        match *count* (100 matches) whose total bytes can still sit below the
        16 KB store threshold: those matches must still be persisted so the
        model gets a ``read(path=...)`` hint for the elided rows. Disk
        failures still degrade gracefully to a preview-only result.
        """
        ...

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Delete persisted results older than ``max_age_hours``; return count.

        Mirrors the legacy ``backend/tool_result_storage.py``
        ``cleanup_old_results`` GC: a periodic background task (started at
        application lifespan) calls this so the store directory does not
        grow without bound.  Implementations MUST be best-effort — a
        per-file failure is swallowed (logged) and never raised, and a
        missing store location is a no-op returning ``0`` — so a GC pass
        can never tank application startup / shutdown.
        """
        ...


# ---------------------------------------------------------------------------
# Coding config (PR-104b — frequently-mutated UI prefs)
# ---------------------------------------------------------------------------
@runtime_checkable
class CodingConfigRepositoryPort(Protocol):
    """Persistence port for ai_coding session-scoped configuration.

    Backs the legacy ``GET /api/cc/config`` + ``POST /api/cc/config``
    routes which surface a free-form JSON document containing UI
    preferences (``permission_mode``, ``allowed_tools``,
    ``max_turns``, ``allowed_working_dirs``, ``model``,
    ``model_list``, …).

    The new architecture stores this document in the platform-shared
    ``kv_user_prefs`` table (migration 007) under a stable key (e.g.
    ``ai_coding.config``) so the UI prefs have a transactional
    destination without polluting ``data/user_config.toml`` (the
    static config) or any per-context schema.

    Sensitive values (API keys, auth tokens) are NEVER persisted
    here — they go through :class:`qai.platform.persistence.secrets.SecretStore`
    via the credentials use cases.

    Schema-of-the-document
    ----------------------
    The document is intentionally a free-form ``dict[str, object]``
    so the WebUI can ship feature toggles without forcing a domain
    migration.  The domain layer treats the doc as opaque; route
    layer DTOs validate the keys it knows about (allowed-tools list
    type, ``max_turns`` int, etc.) and forward the rest verbatim.
    """

    async def load(self) -> dict[str, Any]:
        """Return the current config document.

        Returns an empty dict when no document has been persisted yet
        (fresh install).  Implementations MUST NOT raise on the
        empty-document path.
        """
        ...

    async def save(self, *, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge ``updates`` into the persisted document and return it.

        Merge semantics: top-level keys in ``updates`` overwrite the
        corresponding keys in the persisted document; missing keys
        in ``updates`` are preserved.  This mirrors the legacy
        ``forge_config_manager.update`` shallow-merge behaviour.

        Implementations MUST be atomic — readers seeing a partial
        merge is a contract violation.
        """
        ...


# ---------------------------------------------------------------------------
# OC service control (PR-105)
# ---------------------------------------------------------------------------
class OcServiceStatus:
    """Plain dataclass-like wrapper for OC service status snapshots.

    Defined as a regular class (not a ``@dataclass``) to keep the
    ``ports.py`` module dependency-light, mirroring
    :class:`ToolBridgeResult` above.

    Attributes
    ----------
    running:
        :data:`True` iff a managed OpenCode subprocess is alive (or an
        external process is reachable on the configured port).
    pid:
        Process id of the managed subprocess; :data:`None` for an
        external / not-running process.
    uptime_seconds:
        Wall-clock seconds since the managed subprocess started;
        :data:`None` for not-running / external.
    port:
        TCP port the OpenCode HTTP server listens on (e.g. ``54321``).
    cli_path:
        Filesystem path to the ``opencode`` CLI binary; empty string
        when unconfigured.
    external:
        :data:`True` iff the running process is *not* managed by this
        adapter (detected via port reachability probe).
    """

    __slots__ = (
        "cli_path",
        "external",
        "pid",
        "port",
        "running",
        "uptime_seconds",
    )

    def __init__(
        self,
        *,
        running: bool,
        pid: int | None = None,
        uptime_seconds: float | None = None,
        port: int = 0,
        cli_path: str = "",
        external: bool = False,
    ) -> None:
        self.running: bool = running
        self.pid: int | None = pid
        self.uptime_seconds: float | None = uptime_seconds
        self.port: int = port
        self.cli_path: str = cli_path
        self.external: bool = external


@runtime_checkable
class OcServicePort(Protocol):
    """Adapter for managing the local OpenCode HTTP server subprocess.

    Mirrors the legacy ``opencode_proc_manager`` capability:

    * :meth:`status` reports whether the OC HTTP server is reachable
      (managed by this adapter or running externally on the configured
      port).
    * :meth:`start` spawns a fresh subprocess; idempotent on
      already-running.
    * :meth:`stop` terminates the managed subprocess; idempotent on
      not-running.
    * :meth:`logs` returns the most recent stdout/stderr lines.

    All methods are async to keep parity with subprocess control
    primitives in :mod:`asyncio`; in-memory test stubs return
    immediately.
    """

    async def status(self) -> OcServiceStatus: ...

    async def start(self) -> OcServiceStatus:
        """Start the OpenCode HTTP server.

        Returns the post-start status snapshot.  Idempotent — when
        the server is already running, the method returns the current
        status without restarting.

        Raises ``ValidationError`` if ``cli_path`` is unconfigured
        or the binary is missing on disk.
        """
        ...

    async def stop(self, *, force: bool = False) -> OcServiceStatus:
        """Stop the OpenCode HTTP server.

        Returns the post-stop status snapshot.  Idempotent —
        ``running=False`` when the server was already not running.
        """
        ...

    async def logs(self, *, last_n: int = 100) -> list[str]:
        """Return the last ``last_n`` log lines (clamped to a sane max)."""
        ...


# ---------------------------------------------------------------------------
# Checkpoint repository (PR-105)
# ---------------------------------------------------------------------------
class CheckpointRecord:
    """Persisted snapshot of a session's history at a point in time.

    Mirrors the legacy ``user_message_checkpoints`` shape but lifted
    to a first-class record.  The snapshot is stored in the shared
    ``kv_user_prefs`` table (PR-105) under the per-session key
    ``ai_coding.checkpoints.<session_id>`` so the v2.7 §3.8 release
    artifact rule is honoured (no new schema migration).

    The KV-backed adapter is the production storage; an alternative
    OpenCode-CLI-backed adapter that round-trips through OpenCode's
    native ``revert`` / ``checkpoint`` API would slot behind the same
    port without a schema change.
    """

    __slots__ = ("checkpoint_id", "created_at", "label", "snapshot")

    def __init__(
        self,
        *,
        checkpoint_id: str,
        created_at: str,
        label: str | None,
        snapshot: dict[str, Any],
    ) -> None:
        self.checkpoint_id: str = checkpoint_id
        self.created_at: str = created_at
        self.label: str | None = label
        self.snapshot: dict[str, Any] = snapshot


@runtime_checkable
class CheckpointRepositoryPort(Protocol):
    """Persistence port for per-session checkpoint snapshots.

    The aggregate stays append-only — checkpoints are an out-of-band
    audit trail, not a domain event source.  Implementations MUST be
    atomic on save and tolerate concurrent reads.
    """

    async def create(
        self,
        *,
        session_id: CodingSessionId,
        snapshot: dict[str, Any],
        label: str | None = None,
    ) -> CheckpointRecord:
        """Append a fresh checkpoint and return its record."""
        ...

    async def list_for_session(
        self, session_id: CodingSessionId
    ) -> list[CheckpointRecord]:
        """Return every checkpoint for ``session_id`` ordered oldest-first."""
        ...

    async def get(
        self,
        *,
        session_id: CodingSessionId,
        checkpoint_id: str,
    ) -> CheckpointRecord:
        """Return one checkpoint by id.

        Raises :class:`qai.platform.errors.ValidationError` (with the
        ``ai_coding.checkpoint_not_found`` code) when the id is unknown.
        """
        ...


# ---------------------------------------------------------------------------
# CLAUDE.md template injector (S9 close — clean-arch Port for the
# workspace-bootstrap helper consumed by SpawnCodingSessionUseCase)
# ---------------------------------------------------------------------------
@runtime_checkable
class ClaudeMdInjectorPort(Protocol):
    """Drop a project-internal ``CLAUDE.md`` template into a workspace.

    Defined as a Port so the application layer
    (:class:`SpawnCodingSessionUseCase`) does not import the concrete
    helper in
    :mod:`qai.ai_coding.infrastructure.claude_md_injector` directly.
    The ``layered-ai_coding`` import-linter contract forbids
    ``application -> infrastructure``; the use case takes the Port and
    the DI root (``apps/api/_ai_coding_di.py``) wires the concrete
    adapter.

    Behaviour contract (mirrors the legacy
    ``backend/ai_coding/session_manager.py`` line 1243-1311 helper):

    * idempotent — an existing ``CLAUDE.md`` is preserved unless the
      adapter is constructed with explicit overwrite semantics;
    * failure-tolerant — disk failures are caught and logged inside
      the adapter so a read-only workspace does not abort spawn;
    * returns the destination path on success, ``None`` on no-op /
      failure (including missing template).

    The Port is intentionally synchronous because the concrete
    implementation only does a small ``shutil.copyfile`` — there is no
    benefit to forcing the use case to ``await`` an I/O hop that
    completes in microseconds.
    """

    def copy_to(self, working_dir: Path) -> Path | None:
        """Copy the bundled template into ``working_dir``."""
        ...
