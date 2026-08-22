# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Subprocess-backed hook engine for the chat agentic loop.

Migrated into the chat bounded context from the (removed) ``ai_coding``
agent harness ``hook_engine.py``.  Runs operator-configured shell
commands at :class:`qai.chat.domain.HookEvent` lifecycle points.

Two engines ship:

* :class:`NullHookEngine` — the zero-cost default when a session has no
  hooks; ``fire`` returns ``None`` immediately.
* :class:`SubprocessHookEngine` — executes the configured command via
  :func:`asyncio.create_subprocess_shell` with a strict per-hook
  timeout, capturing stdout/stderr.

Both implement :class:`qai.chat.application.ports.HookEnginePort`.

Failure policy mirrors the legacy harness: a failed / timed-out hook is
logged and the turn continues (``raise_on_failure=True`` opts into
strict mode for audited deployments).

Cross-context isolation
-----------------------
Imports only ``qai.chat.{domain,application}`` + stdlib.  No imports of
other bounded contexts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from qai.chat.application.ports import HookEnginePort, HookFiredRecord
from qai.chat.domain.hook import HookConfig, HookDecision, HookEvent

logger = logging.getLogger("qai.chat.hook_engine")

__all__ = [
    "HookDispatchError",
    "LazyReloadHookEngine",
    "NullHookEngine",
    "SubprocessHookEngine",
    "build_hook_engine",
]

_STDOUT_CAP_BYTES: int = 8192

#: Upper bound on the ``additional_context`` a hook may inject into the turn.
#: Bounds how much a hook can bloat the prompt (defence-in-depth); anything
#: longer is truncated with an explicit marker.
_ADDITIONAL_CONTEXT_CAP: int = 4096


def _parse_hook_stdout(stdout: str) -> dict[str, Any]:
    """Parse a hook's stdout into interceptor directives.

    A hook MAY print a single JSON object on stdout to steer the agent
    loop (mirrors the Claude-Agent-SDK ``PreToolUseHookSpecificOutput``
    shape, localised to a shell command). Recognised keys (all optional):

    * ``decision`` — ``"allow"`` / ``"deny"`` / ``"ask"``;
    * ``reason`` — justification string surfaced when denied;
    * ``updated_input`` — replacement tool-arguments object;
    * ``additional_context`` — text to fold into the turn.

    Returns a dict with the normalised, type-checked values (only keys the
    hook actually supplied). A hook that prints nothing, plain text, or
    malformed JSON returns ``{}`` — so a plain logging hook is a no-op
    interceptor (proceeds as ``ALLOW`` with the model's original input).
    Never raises.
    """
    text = (stdout or "").strip()
    if not text or text[0] not in "{[":
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    out: dict[str, Any] = {}

    raw_decision = parsed.get("decision")
    if isinstance(raw_decision, str):
        try:
            out["decision"] = HookDecision(raw_decision.strip().lower())
        except ValueError:
            # Unknown verdict string → ignore (proceed as ALLOW). Defensive:
            # a typo in a hook script must never wedge the tool loop.
            pass

    raw_reason = parsed.get("reason")
    if isinstance(raw_reason, str) and raw_reason.strip():
        out["reason"] = raw_reason.strip()[:512]

    raw_updated = parsed.get("updated_input")
    if isinstance(raw_updated, dict):
        # Only accept a plain object; the loop splats it as tool arguments.
        out["updated_input"] = dict(raw_updated)

    raw_ctx = parsed.get("additional_context")
    if isinstance(raw_ctx, str) and raw_ctx.strip():
        ctx = raw_ctx.strip()
        if len(ctx) > _ADDITIONAL_CONTEXT_CAP:
            ctx = ctx[:_ADDITIONAL_CONTEXT_CAP] + "\n…[hook context truncated]"
        out["additional_context"] = ctx

    return out


class HookDispatchError(RuntimeError):
    """Raised when a hook failed and the engine is in strict mode."""

    def __init__(
        self,
        *,
        event: HookEvent,
        command: str,
        exit_code: int | None,
        stderr: str = "",
    ) -> None:
        self.event = event
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"hook '{event.value}' failed (exit={exit_code}): "
            f"{command} :: {stderr[:200]}"
        )


class NullHookEngine(HookEnginePort):
    """No-op engine used when a session declares no hooks.

    ``has_hook`` is always ``False`` and ``fire`` returns ``None`` so
    the chat use case pays zero cost at every lifecycle point.
    """

    __slots__ = ()

    def has_hook(self, event: HookEvent) -> bool:
        return False

    async def fire(
        self,
        event: HookEvent,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> HookFiredRecord | None:
        return None


class SubprocessHookEngine(HookEnginePort):
    """Execute configured hook commands via the system shell.

    Construct one per turn (or per session) from the resolved hook
    configs.  Hooks are pre-indexed by event so :meth:`fire` is O(1)
    and a session that didn't declare a hook for an event pays zero
    cost.

    Args:
        hooks: the configured :class:`HookConfig` set; the last entry
            for a given event wins (dict semantics).
        env_overrides: optional env merged on top of ``os.environ`` for
            every spawned hook.
        cwd: working directory for the spawned shell.
        raise_on_failure: when ``True`` a failed hook raises
            :class:`HookDispatchError`; default ``False`` (log + continue).
        enabled: master execution gate (S-7 / align D4). When ``False``
            (the secure default) the engine never spawns subprocesses —
            :meth:`has_hook` reports ``False`` and :meth:`fire` is a no-op
            (returns ``None``) regardless of configured hooks. Operators
            must explicitly opt in (``settings.chat.hooks_enabled``) to
            allow arbitrary-command execution.
    """

    __slots__ = ("_by_event", "_cwd", "_enabled", "_env", "_raise_on_failure")

    def __init__(
        self,
        *,
        hooks: tuple[HookConfig, ...] = (),
        env_overrides: dict[str, str] | None = None,
        cwd: str | None = None,
        raise_on_failure: bool = False,
        enabled: bool = False,
    ) -> None:
        self._by_event: dict[HookEvent, HookConfig] = {
            hook.event: hook for hook in hooks
        }
        self._env: dict[str, str] | None = (
            dict(env_overrides) if env_overrides else None
        )
        self._cwd: str | None = cwd
        self._raise_on_failure: bool = raise_on_failure
        self._enabled: bool = enabled

    def has_hook(self, event: HookEvent) -> bool:
        if not self._enabled:
            return False
        return event in self._by_event

    async def fire(
        self,
        event: HookEvent,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> HookFiredRecord | None:
        if not self._enabled:
            return None
        hook = self._by_event.get(event)
        if hook is None:
            return None

        logger.info("chat.hook.exec event=%s command=%s", event.value, hook.command)

        record = await self._run(hook)

        if record.error is not None:
            logger.warning(
                "chat.hook event=%s exit_code=%s error=%s",
                event.value,
                record.exit_code,
                record.error,
            )
            if self._raise_on_failure:
                raise HookDispatchError(
                    event=event,
                    command=hook.command,
                    exit_code=record.exit_code,
                    stderr=record.stderr,
                )
        return record

    async def _run(self, hook: HookConfig) -> HookFiredRecord:
        env = None
        if self._env:
            env = dict(os.environ)
            env.update(self._env)

        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
            )
        except (FileNotFoundError, OSError, NotImplementedError) as exc:
            return HookFiredRecord(
                event=hook.event,
                command=hook.command,
                exit_code=None,
                error=f"spawn_failed: {exc}",
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=hook.timeout_s
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:  # pragma: no cover
                pass
            return HookFiredRecord(
                event=hook.event,
                command=hook.command,
                exit_code=None,
                error="timeout",
            )

        exit_code = proc.returncode
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")[
            :_STDOUT_CAP_BYTES
        ]
        # Interceptor directives are honoured only on a CLEAN exit (0): a hook
        # that errored/timed out must not be able to steer (deny / rewrite) the
        # tool call — its stdout may be partial/garbage. On success, parse the
        # optional JSON directive block (empty for a plain logging hook).
        directives = _parse_hook_stdout(stdout_text) if exit_code == 0 else {}
        return HookFiredRecord(
            event=hook.event,
            command=hook.command,
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_bytes.decode("utf-8", errors="replace")[:_STDOUT_CAP_BYTES],
            error=None if exit_code == 0 else f"exit_code={exit_code}",
            decision=directives.get("decision"),
            reason=directives.get("reason", ""),
            updated_input=directives.get("updated_input"),
            additional_context=directives.get("additional_context", ""),
        )


class LazyReloadHookEngine(HookEnginePort):
    """A :class:`HookEnginePort` that RE-READS its config on every use.

    Wraps a zero-arg ``provider`` closure the composition root wires to
    (re-)build a concrete engine (a :class:`NullHookEngine` when hooks are
    absent / the operator gate is off, else a :class:`SubprocessHookEngine`)
    from the CURRENT on-disk config. Both :meth:`has_hook` and :meth:`fire`
    delegate to a freshly-built engine each call, so saving hooks or flipping
    the ``hooks_enabled`` toggle in the UI takes effect on the very next turn
    WITHOUT a service restart (mirrors the per-turn forge-config reader pattern
    used elsewhere in DI).

    Correctness over micro-optimisation: a turn fires hooks only a handful of
    times, so rebuilding per call is negligible while guaranteeing the engine
    always reflects the latest config. The provider is best-effort — any error
    it raises is swallowed and treated as "no hooks" (a :class:`NullHookEngine`
    fast-path) so a malformed config file NEVER breaks a turn.
    """

    __slots__ = ("_provider",)

    def __init__(self, provider: Callable[[], HookEnginePort]) -> None:
        self._provider = provider

    def _engine(self) -> HookEnginePort:
        try:
            engine = self._provider()
        except Exception:  # noqa: BLE001 — config read must never break a turn
            return NullHookEngine()
        return engine if engine is not None else NullHookEngine()

    def has_hook(self, event: HookEvent) -> bool:
        try:
            return self._engine().has_hook(event)
        except Exception:  # noqa: BLE001 — best-effort; degrade to no hook
            return False

    async def fire(
        self,
        event: HookEvent,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> HookFiredRecord | None:
        engine = self._engine()
        return await engine.fire(event, payload=payload)


def build_hook_engine(
    hooks: tuple[HookConfig, ...] = (),
    *,
    env_overrides: dict[str, str] | None = None,
    cwd: str | None = None,
    raise_on_failure: bool = False,
    enabled: bool = False,
) -> HookEnginePort:
    """Return a :class:`NullHookEngine` when no hooks / disabled, else a
    subprocess one.

    Keeps the zero-cost guarantee explicit at the wiring site: sessions
    with no configured hooks — or any session while the operator
    execution gate ``enabled`` is ``False`` (the secure default, S-7 /
    align D4) — get the no-op engine and never spawn subprocesses.
    """
    if not hooks or not enabled:
        return NullHookEngine()
    return SubprocessHookEngine(
        hooks=hooks,
        env_overrides=env_overrides,
        cwd=cwd,
        raise_on_failure=raise_on_failure,
        enabled=enabled,
    )
