# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SDK-backed Claude Code provider adapter (CC file checkpoint/rewind).

This adapter mirrors V1's ``backend/ai_coding/session_manager.py`` model
(ADR-005: a **short-lived** ``claude_agent_sdk.ClaudeSDKClient`` per turn,
``resume=claude_session_id`` to restore conversation context) so the
Claude Code feature set that needs the *real* CLI — most importantly
``enable_file_checkpointing`` + ``ClaudeSDKClient.rewind_files`` which
**restore files on disk** — works exactly as it did in V1.

It co-exists with the pure-HTTP :class:`ClaudeCodeProvider`; the DI root
(``apps/api/_ai_coding_di.py``) selects one based on the operator's
``cc_backend`` config (``"http"`` | ``"sdk"``) and gracefully falls back
to HTTP when the SDK / CLI is unavailable (so existing HTTP users never
regress).

Design (V1 parity, V2 clean-arch)
---------------------------------
* **Short connection per turn** — every :meth:`stream` opens a fresh
  ``async with ClaudeSDKClient(options=...)`` and closes it when the turn
  ends (V1 ADR-005, ``session_manager.py:1860``).  The ``async with``
  guarantees the CLI subprocess is torn down each turn (self-cleaning,
  the safer lifecycle V1 chose) — no orphan watchdog needed for the happy
  path; AGENTS.md 铁律5 is satisfied because the SDK owns the child
  process group and the context-manager exit reaps it.
* **options construction** — mirrors V1 ``session_manager.py:1698-1848``:
  ``cwd`` / ``resume`` / ``cli_path`` (auto-located native exe) /
  ``enable_file_checkpointing`` / ``extra_args={"replay-user-messages":
  None}`` / ``env`` (SecretStore API key injected into the CLI subprocess
  env, V1 ``claude_code.py:69-78`` + session_manager env injection).
* **stream mapping** — SDK message stream → existing
  :class:`CodingStreamFrame` (reusing the existing
  :class:`StreamFrameKind` enum; no new frame kinds, §3.1 preserved).
* **session id + UUID capture** — the upstream ``claude_session_id`` and
  the per-user-message ``UserMessage.uuid`` checkpoints are captured into
  the in-memory handle so a follow-up turn resumes and
  :meth:`rewind_files` can map a rewind anchor → SDK UUID (V1
  ``session_manager.py:2024-2037,2080-2096,2604-2706``).
* **import guard** — ``claude_agent_sdk`` is an *optional* dependency
  (``pyproject [project.optional-dependencies].cc-sdk``); when it is not
  importable :meth:`is_available` returns ``False`` so the DI root falls
  back to the HTTP adapter (AGENTS.md cross-platform constraint — the SDK
  / native CLI must not be a hard runtime dependency).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from qai.ai_coding.domain import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    Provider,
    StreamFrameKind,
    Workspace,
)
from qai.platform.errors import NotFoundError
from qai.platform.logging import get_logger
from qai.platform.persistence.secrets import SecretStore

from .base import HttpCodingProviderBase, ProviderHttpConfig
from .claude_cli_locator import locate_claude_cli
from .claude_code import CLAUDE_CODE_DEFAULT_CONFIG, DEFAULT_MODEL

__all__ = ["ClaudeCodeSdkProvider", "claude_sdk_available"]

logger = get_logger(__name__)

#: SecretStore service namespace + key names the auth panel writes the
#: Anthropic credential under (V1 ``claude_code.py:69-78`` resolved the key
#: from the same operator-facing namespace before injecting it into the CLI
#: subprocess env).  Mirrors ``ClaudeCodeProvider._resolve_catalog_api_key``.
_CC_CRED_SERVICE = "ai_coding"
_CC_CRED_KEY_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: Default disallowed / allowed tool baselines (V1 ``session_manager.py``
#: :1815-1828 fallbacks).  Kept conservative; the operator config overrides.
_DEFAULT_ALLOWED_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
_DEFAULT_DISALLOWED_TOOLS = ("Bash", "WebFetch", "WebSearch")

#: Env vars V1 set to keep the CLI from blocking on telemetry / update
#: checks during the ``initialize`` handshake (V1 ``session_manager.py``
#: :465-469 diagnostic recommendation).  Injected into the CLI subprocess
#: env (NOT ``os.environ``) so the effect is session-scoped.
_CLI_QUIET_ENV = {
    "DISABLE_TELEMETRY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def claude_sdk_available() -> bool:
    """Return ``True`` when ``claude_agent_sdk`` is importable.

    Best-effort import guard (never raises) so the DI root can decide
    whether the SDK backend is wireable without taking a hard dependency
    on the optional ``cc-sdk`` extra.
    """
    try:
        import claude_agent_sdk  # noqa: F401  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001 — optional dependency / broken install.
        return False
    return True


class ClaudeCodeSdkProvider(HttpCodingProviderBase):
    """:class:`CodingProviderPort` adapter backed by ``claude_agent_sdk``.

    Reuses the HTTP base only for the per-session bookkeeping it already
    owns (``_handles`` / config stash / sequence counter / multi-turn
    history); the streaming + rewind paths are fully overridden to drive
    the real CLI subprocess through the SDK.
    """

    __slots__ = (
        "_max_tokens",
        "_model",
    )

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        config: ProviderHttpConfig | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
    ) -> None:
        super().__init__(
            provider=Provider.CLAUDE_CODE,
            config=config or CLAUDE_CODE_DEFAULT_CONFIG,
            secret_store=secret_store,
            transport=None,  # SDK path drives the CLI, not HttpTransportPort.
        )
        self._model = model
        self._max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    async def spawn(
        self,
        *,
        provider: Provider,
        workspace: Workspace,
        initial_prompt: Any = None,
        session_id: CodingSessionId | None = None,
        config: CodingSessionConfig | None = None,
    ) -> dict[str, Any]:
        """Spawn + stash the workspace path on the per-session handle.

        The SDK ``ClaudeAgentOptions(cwd=...)`` needs the workspace path on
        the handle (the base only stashes ``config`` / ``messages``), so we
        record it here for :meth:`_build_options` to read back.
        """
        result = await super().spawn(
            provider=provider,
            workspace=workspace,
            initial_prompt=initial_prompt,
            session_id=session_id,
            config=config,
        )
        if session_id is not None:
            record = self._handles.setdefault(session_id.value, {})
            record["workspace"] = workspace.path
        return result

    async def is_available(self) -> bool:
        """Provider liveness probe (P1-5).

        Returns ``True`` only when ``claude_agent_sdk`` is importable AND a
        native CLI executable can be located — otherwise the DI root falls
        back to the HTTP adapter (graceful degrade, no regression for HTTP
        users).  Best-effort: never raises.
        """
        if not claude_sdk_available():
            return False
        return locate_claude_cli(self._configured_cli_path()) is not None

    # ------------------------------------------------------------------
    # Streaming (V1 short-connection model, ADR-005)
    # ------------------------------------------------------------------
    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        """Open a short-lived ``ClaudeSDKClient`` and map its message stream.

        Mirrors V1 ``session_manager.py:1860`` (per-request connection via
        ``resume``) + the non-approval-flow message loop
        (``session_manager.py:2198-2360``).  Always terminates with an END
        frame; an SDK error surfaces an ERROR frame first (route contract).
        """
        return self._stream_impl(session_id)

    async def _stream_impl(
        self, session_id: CodingSessionId
    ) -> AsyncIterator[CodingStreamFrame]:
        try:
            from claude_agent_sdk import (  # type: ignore[import-untyped]
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                SystemMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
            )
        except Exception as exc:  # noqa: BLE001 — SDK missing / broken.
            yield self._error_frame(
                session_id,
                code="ai_coding.cc_sdk_unavailable",
                message=f"claude_agent_sdk not importable: {exc}",
            )
            yield self._end_frame(session_id)
            return

        cfg = self.session_config(session_id)
        prompt, image = self._next_prompt(session_id)
        if not prompt and image is None:
            # Nothing to send — emit a clean END (mirrors HTTP base's
            # benign-placeholder guard without fabricating a turn).
            yield self._end_frame(session_id)
            return

        try:
            options = self._build_options(
                session_id=session_id,
                cfg=cfg,
                options_cls=ClaudeAgentOptions,
            )
        except Exception as exc:  # noqa: BLE001 — option build failure.
            yield self._error_frame(
                session_id,
                code="ai_coding.cc_sdk_options_failed",
                message=str(exc),
            )
            yield self._end_frame(session_id)
            return

        enable_cp = bool(cfg.enable_file_checkpointing)
        sent_terminal = False
        try:
            async with ClaudeSDKClient(options=options) as client:
                # V1 ``session_manager.py:1907-1931`` — an image turn must
                # use Streaming Input (an async generator yielding a
                # multimodal ``user`` message); a text-only turn sends the
                # plain string.  ``query`` accepts either form.
                if image is not None:
                    await client.query(
                        self._image_input_generator(prompt, image)
                    )
                else:
                    await client.query(prompt)
                async for msg in client.receive_response():
                    for frame in self._map_sdk_message(
                        session_id,
                        msg,
                        enable_checkpointing=enable_cp,
                        assistant_cls=AssistantMessage,
                        user_cls=UserMessage,
                        system_cls=SystemMessage,
                        result_cls=ResultMessage,
                        text_block_cls=TextBlock,
                        tool_use_cls=ToolUseBlock,
                        tool_result_cls=ToolResultBlock,
                    ):
                        if frame.kind is StreamFrameKind.END:
                            sent_terminal = True
                        yield frame
        except Exception as exc:  # noqa: BLE001 — surface SDK/CLI errors.
            yield self._error_frame(
                session_id,
                code=self._classify_error(str(exc)),
                message=str(exc),
            )
        finally:
            if not sent_terminal:
                yield self._end_frame(session_id)

    # ------------------------------------------------------------------
    # rewind_files (V1 ``session_manager.py:2604-2706``) — TRUE file restore
    # ------------------------------------------------------------------
    async def rewind_files(
        self,
        *,
        session_id: CodingSessionId,
        marker_index: int,
    ) -> bool:
        """Restore on-disk files to the checkpoint at ``marker_index``.

        V1 parity (``session_manager.py:2604-2706``): looks up the SDK
        ``UserMessage.uuid`` captured for the user-message at
        ``marker_index`` and opens a short-lived ``ClaudeSDKClient`` with
        ``resume=claude_session_id`` + ``enable_file_checkpointing=True``
        to call ``client.rewind_files(sdk_uuid)`` — the SDK then rolls the
        workspace files back to that point on disk.

        Returns ``True`` only when a native rewind was issued; ``False``
        (best-effort, never raises) when checkpointing is off, the session
        has no upstream id, no UUID is mapped, or the SDK is unavailable —
        so :class:`RewindCheckpointUseCase` degrades to message-only.
        """
        record = self._handles.get(session_id.value)
        if not isinstance(record, dict):
            return False
        cfg = self.session_config(session_id)
        if not cfg.enable_file_checkpointing:
            logger.info(
                "ai_coding.cc_sdk.rewind_skipped_checkpointing_off",
                session_id=str(session_id),
            )
            return False
        claude_session_id = record.get("claude_session_id")
        if not claude_session_id:
            logger.info(
                "ai_coding.cc_sdk.rewind_skipped_no_upstream",
                session_id=str(session_id),
            )
            return False
        sdk_uuid = self._uuid_for_marker(record, marker_index)
        if not sdk_uuid:
            logger.warning(
                "ai_coding.cc_sdk.rewind_no_uuid",
                session_id=str(session_id),
                marker_index=marker_index,
            )
            return False
        try:
            from claude_agent_sdk import (  # type: ignore[import-untyped]
                ClaudeAgentOptions,
                ClaudeSDKClient,
            )
        except Exception as exc:  # noqa: BLE001 — SDK missing.
            logger.warning(
                "ai_coding.cc_sdk.rewind_sdk_unavailable",
                session_id=str(session_id),
                error=str(exc),
            )
            return False
        try:
            cli_path = locate_claude_cli(self._configured_cli_path())
            options = ClaudeAgentOptions(
                cwd=record.get("workspace"),
                resume=claude_session_id,
                cli_path=cli_path,
                enable_file_checkpointing=True,
                extra_args={"replay-user-messages": None},
                env=self._build_session_env(cfg),
                # CC-STDERR — same rationale as ``_build_options``: pipe the
                # native CLI's stderr into the V2 structured logger so the
                # bundled Anthropic CLI's ``Could not parse message into
                # JSON:`` / ``From chunk:`` diagnostics (emitted on malformed
                # upstream SSE chunks) don't print raw to the backend's
                # stderr during a rewind turn either.
                stderr=self._make_stderr_logger(session_id),
            )
            async with ClaudeSDKClient(options=options) as client:
                await client.rewind_files(sdk_uuid)
            logger.info(
                "ai_coding.cc_sdk.rewind_files_done",
                session_id=str(session_id),
                marker_index=marker_index,
                sdk_uuid=sdk_uuid,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — never abort the rewind.
            logger.error(
                "ai_coding.cc_sdk.rewind_files_failed",
                session_id=str(session_id),
                marker_index=marker_index,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # SDK options construction (V1 ``session_manager.py:1698-1848``)
    # ------------------------------------------------------------------
    def _build_options(
        self,
        *,
        session_id: CodingSessionId,
        cfg: CodingSessionConfig,
        options_cls: Any,
    ) -> Any:
        """Assemble ``ClaudeAgentOptions`` mirroring V1's constructor."""
        record = self._handles.get(session_id.value, {})
        cli_path = locate_claude_cli(self._configured_cli_path())
        enable_cp = bool(cfg.enable_file_checkpointing)
        # V1 ``session_manager.py:1726-1728``: only request replay when
        # checkpointing is on (UserMessage.uuid capture has overhead).
        extra_args: dict[str, Any] = dict(cfg.extra_args)
        if enable_cp:
            extra_args["replay-user-messages"] = None

        kwargs: dict[str, Any] = {
            "cwd": record.get("workspace"),
            "model": self._model,
            "resume": record.get("claude_session_id"),
            "cli_path": cli_path,
            "enable_file_checkpointing": enable_cp,
            "env": self._build_session_env(cfg),
            "allowed_tools": list(_DEFAULT_ALLOWED_TOOLS),
            "disallowed_tools": list(_DEFAULT_DISALLOWED_TOOLS),
            # CC-STDERR — capture the native CLI's stderr into the V2
            # structured logger instead of letting it inherit the parent
            # process's stderr.  Without this the SDK leaves the child's
            # stderr unpiped (``subprocess_cli.py:447-448``:
            # ``stderr_dest = PIPE if self._options.stderr is not None
            # else None``) so the bundled Anthropic CLI's
            # ``console.error("Could not parse message into JSON:", ...)``
            # diagnostics (emitted on malformed upstream SSE chunks) print
            # raw to the backend's stderr — spamming the V2 log on every
            # CC turn (notably each Feishu-triggered reply).  V1
            # ``session_manager.py:1812-1849`` never set ``stderr`` either,
            # so this is a benign-noise cleanup / observability enhancement
            # beyond V1 (AGENTS.md: fix noise, do not将错就错), not a parity
            # regression fix.  The callback is per-line, lightweight, and
            # swallows its own errors so logging can never break the turn.
            "stderr": self._make_stderr_logger(session_id),
        }
        # C-1 (V1 ``session_manager.py:1390-1432`` parity): pass the
        # OS-context / Git-Bash path hint as the CLI ``system_prompt`` so
        # the agent translates Windows drive-letter paths to MSYS form and
        # uses bash (not PowerShell) in shell tools.  Stashed on the handle
        # at spawn time; ``_os_hint_for_session`` falls back to a live
        # build when absent.  Only set when non-empty so an unconfigured
        # session matches the SDK default (no system prompt).
        os_hint = self._os_hint_for_session(session_id)
        if os_hint:
            kwargs["system_prompt"] = os_hint
        # V1 ``session_manager.py:1838-1844`` — extra knobs only when set,
        # so unconfigured sessions match the SDK defaults.
        if cfg.add_dirs:
            kwargs["add_dirs"] = list(cfg.add_dirs)
        if cfg.effort:
            kwargs["effort"] = cfg.effort
        if cfg.thinking is not None:
            kwargs["thinking"] = dict(cfg.thinking)
        # V1 ``session_manager.py:1768-1774,1844`` — Anthropic beta flags
        # (e.g. ``context-1m-2025-08-07``).  ``ClaudeAgentOptions.betas``
        # accepts a list[str]; forward the operator-configured tuple.
        if cfg.betas:
            kwargs["betas"] = list(cfg.betas)
        # V1 ``session_manager.py:1829`` — ``max_turns`` capped the agentic
        # loop.  V2 carries it on ``CodingSessionConfig.task_budget`` (the
        # documented ``ClaudeAgentOptions.max_turns`` analogue, value_objects
        # .py:416-417).  Forward it so the SDK loop honours the same bound.
        if cfg.task_budget is not None:
            kwargs["max_turns"] = int(cfg.task_budget)
        # NOTE (alignment gap, intentionally NOT forwarded):
        #   * ``agents`` — V1/SDK expect a mapping ``name -> AgentDefinition``
        #     but ``CodingSessionConfig.agents`` is a ``tuple[str]`` (names
        #     only); forwarding the bare tuple would mis-type the SDK arg.
        #     Reconstructing AgentDefinition objects needs a richer config
        #     schema (a follow-up), so we leave it unset (SDK default).
        #   * ``permission_mode`` / ``can_use_tool`` — V2 runs the approval
        #     flow at the APPLICATION layer (StreamCodingSessionUseCase
        #     registers a PERMISSION_REQUEST frame; ``/permissions/{id}/decide``
        #     resolves it), the same user-perceived gate as V1's in-stream
        #     ``can_use_tool`` but a different (clean-arch) seam — see report.
        if extra_args:
            kwargs["extra_args"] = extra_args
        return options_cls(**kwargs)

    @staticmethod
    def _make_stderr_logger(
        session_id: CodingSessionId,
    ) -> Any:
        """Build the per-line ``ClaudeAgentOptions.stderr`` callback.

        The SDK contract is ``stderr: Callable[[str], None] | None``
        (``claude_agent_sdk.ClaudeAgentOptions``); the SDK's
        ``SubprocessCLITransport._handle_stderr`` invokes it once per
        line of the CLI subprocess's stderr.  We route each line into the
        V2 structured logger at ``debug`` level so the noisy upstream-SSE
        diagnostics (``Could not parse message into JSON:`` /
        ``From chunk:``) stop spamming the backend's raw stderr yet stay
        retrievable when debug logging is enabled.

        ``debug`` (not ``warning``/``error``) is deliberate: these lines
        are CLI diagnostics, overwhelmingly benign (the reply still sends
        successfully); promoting them to a higher level would merely swap
        one form of刷屏 for another.  Operators who need them can raise the
        log level.

        The callback swallows any exception — a logging failure must never
        propagate into the CLI's stderr reader and break the streaming
        turn (mirrors the SDK's own ``_handle_stderr`` best-effort guard).
        """
        sid = session_id.value

        def _on_stderr_line(line: str) -> None:
            try:
                logger.debug(
                    "ai_coding.cc_sdk.cli_stderr",
                    session_id=sid,
                    line=line.rstrip(),
                )
            except Exception:  # noqa: BLE001 — logging must never break the turn.
                pass

        return _on_stderr_line

    def _build_session_env(self, cfg: CodingSessionConfig) -> dict[str, str]:
        """Build the CLI subprocess env (V1 ``_session_env`` injection).

        V1 (``claude_code.py:69-78`` + ``session_manager.py:1450-1464,
        1836``) resolved the Anthropic credential from the SecretStore and
        injected it — plus the operator's ``session_env`` — into the CLI
        child process env (NOT ``os.environ``, so it stays session-scoped).
        We mirror that: telemetry-quiet vars + resolved API key +
        per-session ``session_env`` overrides.
        """
        env: dict[str, str] = dict(_CLI_QUIET_ENV)
        api_key = self._resolve_api_key()
        if api_key:
            # The CLI reads ``ANTHROPIC_API_KEY`` from its env (V1 parity).
            env["ANTHROPIC_API_KEY"] = api_key
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if base_url and base_url.strip():
            env["ANTHROPIC_BASE_URL"] = base_url.strip()
        # Operator per-session env overrides win last (V1 ``session_env``).
        for key, value in cfg.session_env.items():
            if isinstance(key, str) and key.strip() and value is not None:
                env[key] = str(value)
        return env

    def _resolve_api_key(self) -> str | None:
        """Resolve the Anthropic credential (V1 ``claude_code.py:69-78``).

        Env-first (operator may export the key), then the auth-panel
        SecretStore namespace.  Best-effort: returns ``None`` when nothing
        is configured (the CLI then errors on its own — surfaced as an
        ERROR frame), never raises.
        """
        for env_name in _CC_CRED_KEY_NAMES:
            val = os.environ.get(env_name)
            if val and val.strip():
                return val.strip()
        for key_name in _CC_CRED_KEY_NAMES:
            try:
                val = self._secret_store.get(_CC_CRED_SERVICE, key_name)
            except NotFoundError:
                continue
            if val and val.strip():
                return val.strip()
        return None

    def _configured_cli_path(self) -> str | None:
        """Return the operator-configured ``cli_path`` (or ``None``).

        Reads the most-recently-stashed per-session config; falls back to
        ``None`` (auto-locate) when no session has been spawned yet.  The
        cli_path is a global capability (where the CLI lives) so any
        session's value is representative.
        """
        for record in self._handles.values():
            cfg = record.get("config")
            if isinstance(cfg, CodingSessionConfig) and cfg.cli_path:
                return cfg.cli_path
        return None

    # ------------------------------------------------------------------
    # SDK message → CodingStreamFrame mapping (V1 ``:2200-2360``)
    # ------------------------------------------------------------------
    def _map_sdk_message(
        self,
        session_id: CodingSessionId,
        msg: Any,
        *,
        enable_checkpointing: bool,
        assistant_cls: Any,
        user_cls: Any,
        system_cls: Any,
        result_cls: Any,
        text_block_cls: Any,
        tool_use_cls: Any,
        tool_result_cls: Any,
    ) -> list[CodingStreamFrame]:
        """Translate one SDK message into zero or more stream frames."""
        frames: list[CodingStreamFrame] = []
        if isinstance(msg, assistant_cls):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, text_block_cls):
                    text = getattr(block, "text", "") or ""
                    if text:
                        frames.append(
                            self._frame(
                                session_id,
                                StreamFrameKind.TEXT,
                                {"text": text},
                            )
                        )
                elif isinstance(block, tool_use_cls):
                    frames.append(
                        self._frame(
                            session_id,
                            StreamFrameKind.TOOL_CALL,
                            {
                                "id": getattr(block, "id", None),
                                "tool": getattr(block, "name", ""),
                                "args": getattr(block, "input", {}) or {},
                            },
                        )
                    )
        elif isinstance(msg, user_cls):
            # V1 ``:2227-2244`` — capture UserMessage.uuid for the ORIGINAL
            # user message (content is a str) when checkpointing is on, and
            # record it for rewind_files mapping.
            content = getattr(msg, "content", None)
            uuid = getattr(msg, "uuid", None) or getattr(msg, "id", None)
            if uuid and enable_checkpointing and isinstance(content, str):
                self._record_user_uuid(session_id, uuid)
                frames.append(
                    self._frame(
                        session_id,
                        StreamFrameKind.TEXT,
                        {"user_message_uuid": uuid},
                    )
                )
            # Tool-result blocks ride inside a UserMessage list content.
            for block in content if isinstance(content, list) else []:
                if isinstance(block, tool_result_cls):
                    frames.append(
                        self._frame(
                            session_id,
                            StreamFrameKind.TOOL_RESULT,
                            {
                                "tool_use_id": getattr(
                                    block, "tool_use_id", ""
                                ),
                                "is_error": bool(
                                    getattr(block, "is_error", False)
                                ),
                            },
                        )
                    )
        elif isinstance(msg, system_cls):
            subtype = getattr(msg, "subtype", "") or ""
            if subtype == "init":
                init_sid = getattr(msg, "session_id", None)
                if init_sid:
                    self._capture_session_id(session_id, init_sid)
        elif isinstance(msg, result_cls):
            upstream_sid = getattr(msg, "session_id", None)
            if upstream_sid:
                self._capture_session_id(session_id, upstream_sid)
            frames.append(
                self._frame(
                    session_id,
                    StreamFrameKind.END,
                    {"usage": self._extract_usage(msg)},
                )
            )
        return frames

    @staticmethod
    def _extract_usage(result_msg: Any) -> dict[str, int]:
        """Pull token counts off a ResultMessage (V1 ``:2344-2360``)."""
        usage = getattr(result_msg, "usage", None)
        if not isinstance(usage, dict):
            return {"input_tokens": 0, "output_tokens": 0}

        def _as_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        input_t = _as_int(usage.get("input_tokens"))
        cache_t = _as_int(usage.get("cache_read_input_tokens"))
        return {
            "input_tokens": input_t + cache_t,
            "output_tokens": _as_int(usage.get("output_tokens")),
        }

    # ------------------------------------------------------------------
    # Handle bookkeeping helpers
    # ------------------------------------------------------------------
    def _capture_session_id(
        self, session_id: CodingSessionId, upstream_sid: str
    ) -> None:
        record = self._handles.setdefault(session_id.value, {})
        if record.get("claude_session_id") != upstream_sid:
            record["claude_session_id"] = upstream_sid
            logger.info(
                "ai_coding.cc_sdk.session_id_captured",
                session_id=str(session_id),
                claude_session_id=upstream_sid,
            )

    def _record_user_uuid(
        self, session_id: CodingSessionId, uuid: str
    ) -> None:
        """Append an ordered ``user_message_checkpoints`` entry.

        The list index IS the 0-based user-message ordinal, which matches
        the ``marker_index`` the rewind use case passes (the kept-message
        anchor).  V1 stored a richer ``{frontend_msg_id, sdk_uuid, ...}``
        dict; V2 keys purely on order since the rewind anchor is an index.
        """
        record = self._handles.setdefault(session_id.value, {})
        checkpoints = record.setdefault("user_message_checkpoints", [])
        if isinstance(checkpoints, list):
            checkpoints.append(uuid)

    @staticmethod
    def _uuid_for_marker(
        record: dict[str, Any], marker_index: int
    ) -> str | None:
        """Map a rewind anchor index → captured SDK UUID (V1 ``:2637-2676``)."""
        checkpoints = record.get("user_message_checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            return None
        idx = max(0, min(marker_index, len(checkpoints) - 1))
        value = checkpoints[idx]
        return value if isinstance(value, str) and value else None

    def _next_prompt(
        self, session_id: CodingSessionId
    ) -> tuple[str, dict[str, str] | None]:
        """Return ``(text, image)`` for the latest queued user turn.

        ``send_message`` (base) appends to the multi-turn history; the SDK
        path resumes the upstream conversation so it only needs to send the
        newest user turn (the CLI keeps the rest via ``resume``).

        V1 ``session_manager.py:1907-1929`` parity: when the newest user
        message carries an inline image (the base stores it as a list
        ``content`` with an ``image`` + ``text`` block, ``base.py:275-290``)
        the image is surfaced here so :meth:`_stream_impl` sends it via the
        SDK Streaming-Input multimodal generator instead of dropping it.
        ``image`` is ``{"b64": ..., "mime": ...}`` or ``None``.
        """
        history = self._get_session_history(session_id)
        for entry in reversed(history):
            if entry.get("role") != "user":
                continue
            content = entry.get("content")
            if isinstance(content, str) and content:
                return content, None
            if isinstance(content, list):
                text = ""
                image: dict[str, str] | None = None
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        text = str(block["text"])
                    elif block.get("type") == "image":
                        source = block.get("source") or {}
                        if (
                            isinstance(source, dict)
                            and source.get("data")
                            and source.get("media_type")
                        ):
                            image = {
                                "b64": str(source["data"]),
                                "mime": str(source["media_type"]),
                            }
                return text, image
        return "", None

    @staticmethod
    def _image_input_generator(text: str, image: dict[str, str]) -> Any:
        """Build a Streaming-Input async generator for an image turn.

        Mirrors V1 ``session_manager.py:1908-1929``: yields a single
        ``user`` message whose ``content`` is a multimodal list (the
        base64 ``image`` block first, then the optional ``text`` block) —
        the only input shape the SDK / CLI accepts for media attachments.
        """

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            blocks: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image["mime"],
                        "data": image["b64"],
                    },
                },
            ]
            if text:
                blocks.append({"type": "text", "text": text})
            yield {
                "type": "user",
                "message": {"role": "user", "content": blocks},
            }

        return _gen()

    # ------------------------------------------------------------------
    # Frame factories
    # ------------------------------------------------------------------
    def _frame(
        self,
        session_id: CodingSessionId,
        kind: StreamFrameKind,
        payload: dict[str, Any],
    ) -> CodingStreamFrame:
        return CodingStreamFrame(
            kind=kind,
            payload=payload,
            sequence=self._next_sequence(session_id),
        )

    def _end_frame(self, session_id: CodingSessionId) -> CodingStreamFrame:
        return self._frame(session_id, StreamFrameKind.END, {})

    def _error_frame(
        self, session_id: CodingSessionId, *, code: str, message: str
    ) -> CodingStreamFrame:
        from qai.platform.errors import InfrastructureError

        err = InfrastructureError(code=code, message=message)
        return self._frame(session_id, StreamFrameKind.ERROR, err.to_dict())

    @staticmethod
    def _classify_error(err_str: str) -> str:
        """Map an SDK error string → a V1-parity error code (``:457-522``)."""
        if "Control request timeout: initialize" in err_str:
            return "ai_coding.cc_sdk_initialize_timeout"
        if "3221225477" in err_str:
            # x64 bundled CLI crash on ARM64 (V1 ``:481-496``).
            return "ai_coding.cc_sdk_cli_crash"
        if "No conversation found" in err_str:
            return "ai_coding.cc_sdk_session_context_lost"
        return "ai_coding.cc_sdk_stream_failed"
