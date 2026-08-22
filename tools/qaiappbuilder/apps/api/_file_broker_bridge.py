# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context FileBroker bridge (apps/api wiring root).

Builds the production :class:`PatternFileScreen` — the *pure-software*
file/exec safety layer — by injecting guards backed by other bounded
contexts' **pure** domain helpers, without ``qai.ai_coding`` importing those
contexts directly (the ``context-isolation`` import-linter contract forbids
that). The bridge lives in ``apps/api/`` — the one layer allowed to depend on
multiple bounded contexts — exactly like ``_file_guard_bridge.py``.

Why a separate layer from FileGuard
-----------------------------------
FileGuard (``_file_guard_bridge.py`` → PolicyCenter) is the heavier
policy-driven read/write/exec gate with an in-process ASK / approve /
audit loop. PatternFileScreen provides the cheap **pure-software** subset
that runs on every tool call before it:

* hard-protected write rejection — ``qai.platform.protected_paths``
  :func:`is_write_blocked`, the SAME floor ``CheckPermissionUseCase``
  enforces as its unbypassable hard-deny (2026-08-09: previously a
  parallel, contradictory V1 list — see :func:`_make_write_dir_guard`);
* dangerous exec-command rejection — ``qai.dependency_approval`` domain
  :func:`is_dep_install_command` / :func:`find_denied_args` (pure) plus a
  static dangerous-command regex set (V1 ``exec_deny`` defaults, no DB);
* always_exclude path rejection + glob/grep result truncation (in the broker).

Layering rule (2026-08-09): this screen may only enforce the hard floor and
content-shape hygiene. Every PATH-POLICY decision — which system roots are
readable, where writes prompt, which trees are allowed outright — belongs to
``factory/config/file_guard_paths.json`` and is enforced by FileGuard alone.
A second path list here would silently override that config, because this
screen runs first and raises before FileGuard is consulted.

Gated by ``settings.tools.file_broker_enabled`` (default ON).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.ai_coding.application.ports import FileBrokerPort
from qai.ai_coding.infrastructure.tools.file_broker import (
    NoopFileBroker,
    PatternFileScreen,
)
from qai.security.domain import (
    BUILTIN_DANGEROUS_COMMAND_PATTERNS,
    compile_extra_patterns,
    match_dangerous_command,
)

if TYPE_CHECKING:  # pragma: no cover
    import re

    from .di import Container

__all__ = ["build_file_broker", "DANGEROUS_COMMAND_PATTERNS"]


#: Back-compat alias for the dangerous-command deny floor, now owned by the
#: security domain (:mod:`qai.security.domain.dangerous_commands`). Phase 3a
#: (P-18 §6.2) promoted the built-in patterns to the domain as the immutable
#: floor + added a union-only runtime-override layer; this apps-layer name is
#: kept so existing importers keep working. It is the FLOOR only — the live
#: guard unions it with any operator-supplied extra patterns via
#: :func:`match_dangerous_command`.
DANGEROUS_COMMAND_PATTERNS: "tuple[re.Pattern[str], ...]" = (
    BUILTIN_DANGEROUS_COMMAND_PATTERNS
)


def _make_write_dir_guard(env: "dict[str, str]"):
    """Return a ``(path) -> bool`` guard for the hard-protected floor.

    2026-08-09 — single source of truth. This used to call the V1-era
    ``qai.security.domain.dangerous_paths.is_blocked_for_write``, which
    carried its OWN parallel block list (``C:\\Windows``, ``C:\\Program
    Files``, ``C:\\Program Files (x86)``, ``C:\\ProgramData``,
    ``C:\\$Recycle.Bin`` as prefixes; ``%TEMP%`` / ``%TMP%`` / ``%APPDATA%``
    / ``%LOCALAPPDATA%`` / ``%USERPROFILE%`` / the workspace root as exact
    matches). That list was a second, contradictory policy source:

    * it did NOT block ``C:\\Qualcomm`` — the one genuine hard-protected
      tree — because that path was never in the V1 list;
    * it DID block ``%USERPROFILE%`` itself, contradicting the shipped
      ``file_guard_paths.json`` entry that grants that tree rwx=allow;
    * running BEFORE FileGuard, it silently overrode config: a
      ``%SystemRoot%\\Temp`` write stayed blocked no matter what the config
      said, so any three-state entry under a V1 prefix was dead config.

    The floor now comes from ``qai.platform.protected_paths`` — the SAME
    module ``CheckPermissionUseCase`` uses for its unbypassable hard-deny
    (``check_permission.py`` → ``protected_paths.is_write_blocked``), which
    is in turn the in-process twin of the native guard64 deny rules. So this
    pre-call screen and FileGuard can no longer disagree, and everything
    else (system roots, profile dirs, temp) is decided by
    ``factory/config/file_guard_paths.json`` alone.

    ``env`` is accepted for signature stability (callers pass ``os.environ``)
    but is no longer read: the floor is env-independent.
    """
    _ = env  # floor is env-independent; kept for call-site compatibility

    from qai.platform.protected_paths import is_write_blocked

    def _guard(path: str) -> bool:
        try:
            return is_write_blocked(path) is not None
        except Exception:  # noqa: BLE001 — guard must never break a tool call
            return False

    return _guard


def _make_exec_command_guard(dep_broker=None, *, extra_dangerous_patterns=None):
    """Return a ``(command) -> str | None | Awaitable[str | None]`` guard.

    Blocks (returns a Chinese reason):

    * **dep-install commands carrying denied args** (``pip install -e`` /
      ``git+`` / ``--extra-index-url`` / ``--pre``) — routed through the
      stateful :class:`DepBrokerPort` when one is injected AND enabled. This
      runs the full V1 closed loop: enqueue a
      pending request → notify the WebUI → **block** until the operator
      approves (allow) / rejects / the approval timeout elapses (deny). The
      guard therefore returns a coroutine for dep-install commands so
      :meth:`PatternFileScreen.pre_call` can await the decision. When no
      stateful broker is wired (or it is disabled) the dep-install path is a
      no-op — the operator opted out.
    * commands matching a dangerous-command regex (``rm -rf`` …) — always a
      synchronous hard block (no approval flow; these are never legitimate).
      The pattern set is the security domain's immutable built-in floor
      (:data:`BUILTIN_DANGEROUS_COMMAND_PATTERNS`) UNIONed with any
      operator-supplied ``extra_dangerous_patterns`` (union-only: the floor
      can never be removed — red line §9.2.4).

    No DB / PolicyCenter dependency. The ``dep_broker`` is injected by the
    apps-layer bridge so ``qai.ai_coding`` never imports ``qai.dependency_approval``
    (context-isolation contract).
    """
    from qai.dependency_approval.domain import find_denied_args, is_dep_install_command

    def _static_dangerous(command: str) -> str | None:
        # Union of the immutable domain floor + operator extra patterns.
        source = match_dangerous_command(
            command, extra=extra_dangerous_patterns
        )
        if source is not None:
            return f"匹配危险命令模式：{source}"
        return None

    def _guard(command: str):
        try:
            # Static dangerous patterns: synchronous hard block (highest
            # priority; never subject to approval).
            static_reason = _static_dangerous(command)
            if static_reason is not None:
                return static_reason
            # Dep-install denied args: route through the stateful broker's
            # blocking approval loop when wired + enabled (V1 Gate①).
            if (
                dep_broker is not None
                and getattr(dep_broker, "enabled", False)
                and is_dep_install_command(command)
                and find_denied_args(command)
            ):
                # Returns a coroutine; pre_call awaits it (blocks until the
                # operator decides). check_and_wait re-validates enabled /
                # denied internally and returns (should_block, reason).
                return _await_dep_decision(dep_broker, command)
        except Exception:  # noqa: BLE001 — guard must never break a tool call
            return None
        return None

    async def _await_dep_decision(broker, command: str) -> str | None:
        try:
            should_block, reason = await broker.check_and_wait(command)
        except Exception:  # noqa: BLE001 — broker failure must not block exec
            return None
        return reason if should_block else None

    return _guard


def build_file_broker(container: "Container") -> FileBrokerPort:
    """Compose the production :class:`FileBrokerPort` from the container.

    Returns a configured :class:`PatternFileScreen` when
    ``settings.tools.file_broker_enabled`` is True (default); otherwise a
    pass-through :class:`NoopFileBroker`. Any missing namespace (hand-rolled
    test containers) degrades to ``NoopFileBroker``.

    The stateful dependency_approval broker (``container.dependency_approval.broker``) is injected into
    the exec guard so a dep-install command carrying denied args runs the V1
    approval loop (enqueue → notify → block until approve/reject/timeout)
    instead of being hard-blocked, whenever the broker is enabled.
    """
    import os

    settings = getattr(container, "settings", None)
    tools_settings = getattr(settings, "tools", None) if settings else None
    if tools_settings is None or not getattr(
        tools_settings, "file_broker_enabled", True
    ):
        return NoopFileBroker()

    repo_root = getattr(container, "repo_root", None)

    # Stateful dependency_approval broker for the interactive approval path (degrades to the
    # no-op dep path when the namespace is absent — hand-rolled containers).
    dep_ns = getattr(container, "dependency_approval", None)
    dep_broker = getattr(dep_ns, "broker", None) if dep_ns is not None else None

    # Runtime-override layer (P-18 §6.2): operators may UNION extra
    # dangerous-command patterns on top of the immutable domain floor. Sourced
    # from the security runtime bucket ``dangerous_command_patterns`` (a list
    # of regex strings) when wired; absent / malformed → empty (floor only).
    # This is union-only by construction (``match_dangerous_command`` always
    # includes the built-in floor), so an operator can ADD coverage but can
    # NEVER delete the built-in ``rm -rf`` floor (red line §9.2.4).
    extra_dangerous_patterns = _resolve_extra_dangerous_patterns(container)

    audit_path = None
    try:
        _data_paths = getattr(container, "data_paths", None)
        _root = getattr(_data_paths, "root", None) if _data_paths else None
        if _root is not None:
            from pathlib import Path

            audit_path = Path(_root) / "security" / "file_broker_audit.jsonl"
    except Exception:  # noqa: BLE001
        audit_path = None

    audit_sink = None
    if audit_path is not None:
        # P-17 §6.3 — unified JSONL schema via the shared factory (was a
        # hand-rolled json.dumps block with a schema that drifted from the
        # FileGuard emergency sink). ``PatternFileScreen`` calls the sink as
        # ``(op, path, reason)``; adapt to the canonical record shape.
        from ._jsonl_audit_sink import make_jsonl_audit_sink

        _jsonl = make_jsonl_audit_sink(audit_path, source="file_broker")

        def _audit_sink(op: str, path: str, reason: str) -> None:
            _jsonl(
                {
                    "op": op,
                    "path": path,
                    "decision": "deny",
                    "source": "file_broker",
                    "reason": reason,
                }
            )

        audit_sink = _audit_sink

    return PatternFileScreen(
        project_root=repo_root,
        write_dir_guard=_make_write_dir_guard(dict(os.environ)),
        exec_command_guard=_make_exec_command_guard(
            dep_broker, extra_dangerous_patterns=extra_dangerous_patterns
        ),
        audit_sink=audit_sink,
        max_entries=int(getattr(tools_settings, "file_broker_max_entries", 10000)),
    )


def _resolve_extra_dangerous_patterns(container: "Container"):
    """Read the operator's union-only extra dangerous-command patterns.

    Sourced from the security runtime bucket ``dangerous_command_patterns``
    (a list of regex strings) when the runtime-state service is wired. Best-
    effort: a missing namespace / bucket / malformed value degrades to the
    built-in floor ONLY (empty extra). Compiled via the domain helper, which
    skips uncompilable entries (a bad operator regex can never open the box).

    NOTE: this is a UNION-ONLY override layer — it can only ADD patterns; the
    immutable ``BUILTIN_DANGEROUS_COMMAND_PATTERNS`` floor is always applied by
    ``match_dangerous_command`` regardless of this value (red line §9.2.4).
    """
    try:
        security = getattr(container, "security", None)
        runtime_state = (
            getattr(security, "security_runtime_state", None)
            if security is not None
            else None
        )
        if runtime_state is None:
            return ()
        bucket = runtime_state.get_settings("dangerous_command_patterns")
        # Accept either a bare list of strings or a {"extra": [...]} shape.
        if isinstance(bucket, dict):
            raw = bucket.get("extra") or bucket.get("patterns") or []
        elif isinstance(bucket, (list, tuple)):
            raw = bucket
        else:
            raw = []
        return compile_extra_patterns(tuple(str(p) for p in raw))
    except Exception:  # noqa: BLE001 — override is best-effort; floor stands
        return ()
