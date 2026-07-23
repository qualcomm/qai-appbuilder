# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — unified runtime-config surface (security/tools switches).

``GET / PUT /api/security/runtime-config`` is the single authoritative surface
for the typed security + tools switches the WebUI exposes (2026-06
security-settings unification). It replaces the six dead ``/api/settings/*`` KV
sections that wrote to ``forge.config`` but had no backend consumer — every
field here drives real backend behaviour.

Three-layer mental model (one switch per layer):

* **Tool safety (layer 1)** — ``file_broker_enabled`` / ``ssl_verify`` /
  ``project_skip_dirs`` / ``global_proxy`` (``ToolsSettings``). Pure-software
  ``PatternFileScreen`` hygiene + tool-execution tunables. ``ssl_verify`` /
  ``project_skip_dirs`` / ``global_proxy`` hot-apply immediately;
  ``file_broker_enabled`` is baked into the tool-bridge at build → reboot.
* **Policy guard (layer 2)** — ``file_guard_enabled`` /
  ``native_file_guard_enabled`` / ``allow_exec_tool``
  (``SecuritySettings``). PolicyCenter FileGuard + native guard64.dll
  sub-process hook. The unified FileGuard master switch
  (``file_guard_enabled`` + ``native_file_guard_enabled``) **hot-applies**
  (no reboot): FileGuardFacade reads ``file_guard_enabled`` live and
  the native hook is start()/stop()'d in place on PUT. ``allow_exec_tool``
  is a DI-build decision → reboot.
* **Command execution (layer 3)** — ``command_policy_enabled`` (exec-profile
  master switch) + ``dependency_approval_enabled`` /
  ``dependency_approval_deny_args`` / ``dependency_approval_timeout_s``
  (pip/uv install approval broker). Both gate command execution and
  **hot-apply** in place: the exec/dep brokers hold a live reference and
  read their enabled/tuning state at the next intercepted command.

Persistence (decision 2A): edits are written to the shared ``forge_config``
document via :mod:`apps.api._runtime_config_store`; ``create_app`` feeds them
back as ``load_settings(overrides=...)`` so they survive a restart and win over
``server.toml`` / env / defaults.

Reboot (decision 3B): a change to a build-time switch (security switches +
``file_broker_enabled``) returns ``needs_reboot=True`` so the frontend can show
the custom reboot-confirm dialog. The hot-applicable tools switches take effect
immediately and never set the flag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


class RuntimeConfigResponse(BaseModel):
    """Effective security + tools switch values for the current process."""

    # Layer 1 — tool safety (ToolsSettings)
    file_broker_enabled: bool
    file_broker_max_entries: int
    ssl_verify: bool
    project_skip_dirs: list[str]
    global_proxy: str | None
    # Layer 2 — policy guard (SecuritySettings)
    file_guard_enabled: bool
    allow_exec_tool: bool
    # 2026-07-04 native-hook integration — the OS-level guard64.dll hook
    # master switch. Tail-appended per §3.1. Part of the UNIFIED FileGuard
    # switch: the UI toggles it together with ``file_guard_enabled`` and both
    # hot-apply (no reboot) — Python layer reads settings live, native layer
    # start()/stop()s the DLL (re-entrant Init/Destroy, verified).
    native_file_guard_enabled: bool
    # dependency_approval — controlled dependency-install approval (hot-applied)
    dependency_approval_enabled: bool
    # M-3 — operator-tunable dependency_approval denied-arg list + approval timeout
    dependency_approval_deny_args: list[str]
    dependency_approval_timeout_s: int
    # M-2 — command_policy master switch (hot-applied)
    command_policy_enabled: bool
    # tool_output — in-prompt size caps for the file-search / read / list /
    # exec tool family (ToolOutputSettings). Build-time decision (installed
    # into the ai_coding tool-handler seam at tool-bridge build) → reboot, same
    # nature as ``file_broker_max_entries``. Tail-appended per §3.1.
    read_max_lines: int
    read_max_bytes: int
    read_max_line_length: int
    glob_max_results: int
    grep_max_matches: int
    grep_max_line_length: int
    grep_max_output_bytes: int


class RuntimeConfigUpdateRequest(BaseModel):
    """Partial update — every field optional; absent fields are unchanged."""

    file_broker_enabled: bool | None = None
    file_broker_max_entries: int | None = None
    ssl_verify: bool | None = None
    project_skip_dirs: list[str] | None = None
    global_proxy: str | None = None
    file_guard_enabled: bool | None = None
    allow_exec_tool: bool | None = None
    native_file_guard_enabled: bool | None = None
    dependency_approval_enabled: bool | None = None
    dependency_approval_deny_args: list[str] | None = None
    dependency_approval_timeout_s: int | None = None
    command_policy_enabled: bool | None = None
    # tool_output in-prompt size caps (partial update). The ``ge`` bounds
    # mirror ``ToolOutputSettings`` so an out-of-range value (e.g.
    # ``read_max_lines=0`` or ``grep_max_line_length=10``) is rejected with a
    # 422 before it can be persisted.
    read_max_lines: int | None = Field(default=None, ge=1)
    read_max_bytes: int | None = Field(default=None, ge=1024)
    read_max_line_length: int | None = Field(default=None, ge=80)
    glob_max_results: int | None = Field(default=None, ge=1)
    grep_max_matches: int | None = Field(default=None, ge=1)
    grep_max_line_length: int | None = Field(default=None, ge=80)
    grep_max_output_bytes: int | None = Field(default=None, ge=1024)


class RuntimeConfigUpdateResponse(RuntimeConfigResponse):
    """PUT response: the merged effective view + reboot/persistence signals.

    ``needs_reboot`` is True when a build-time switch changed (security
    switches + ``file_broker_enabled``); the hot-applicable tools switches
    (``ssl_verify`` / ``project_skip_dirs`` / ``global_proxy``) take effect
    immediately and never raise the flag. ``persisted`` echoes the keys that
    were written to forge_config so the UI can confirm the save.
    """

    needs_reboot: bool = False
    persisted: list[str] = Field(default_factory=list)


# Switches whose change requires a process restart to take effect (they are
# DI-build decisions, not runtime seams).
_REBOOT_FIELDS = frozenset(
    {
        # NOTE: ``file_guard_enabled`` is NO LONGER a reboot field — the
        # unified FileGuard master switch hot-applies it (FileGuardFacade
        # reads it live via ``enabled_provider``; the native guard64.dll hook
        # is start()/stop()'d in place). ``native_file_guard_enabled`` is
        # likewise hot-applied and intentionally NOT listed here.
        "allow_exec_tool",
        "file_broker_enabled",
        "file_broker_max_entries",
        # tool_output caps are installed into the ai_coding tool-handler seam
        # at tool-bridge build time (a DI-build decision, like
        # ``file_broker_max_entries``), so a change takes effect only after a
        # restart — never hot-applied.
        "read_max_lines",
        "read_max_bytes",
        "read_max_line_length",
        "glob_max_results",
        "grep_max_matches",
        "grep_max_line_length",
        "grep_max_output_bytes",
    }
)
# Switches that hot-apply at runtime via the ai_coding tool seams.
_HOT_TOOLS_FIELDS = frozenset({"ssl_verify", "project_skip_dirs", "global_proxy"})


def _effective(container: "Container") -> dict[str, object]:
    """Read the current effective switch values off the live Settings."""
    sec = container.settings.security
    tools = container.settings.tools
    # dependency_approval_enabled reflects the LIVE broker (hot-toggled in
    # place) when present, falling back to the settings default.
    dep_ns = getattr(container, "dependency_approval", None)
    dep_broker = getattr(dep_ns, "broker", None) if dep_ns is not None else None
    dep_enabled = (
        bool(getattr(dep_broker, "enabled", False))
        if dep_broker is not None
        else bool(getattr(tools, "dependency_approval_enabled", False))
    )
    # M-3 — live dependency_approval deny_args / approval timeout (fall back to
    # settings when the broker namespace is absent in minimal test builds).
    dep_deny_args = (
        list(getattr(dep_broker, "deny_args", ()))
        if dep_broker is not None
        else list(getattr(tools, "dependency_approval_deny_args", []))
    )
    dep_timeout = (
        int(getattr(dep_broker, "approval_timeout_s", 120))
        if dep_broker is not None
        else int(getattr(tools, "dependency_approval_timeout_s", 120))
    )
    # M-2 — live command_policy master switch.
    exec_ns = getattr(container, "command_policy", None)
    exec_broker = (
        getattr(exec_ns, "broker", None) if exec_ns is not None else None
    )
    exec_enabled = (
        bool(getattr(exec_broker, "enabled", False))
        if exec_broker is not None
        else bool(getattr(tools, "command_policy_enabled", True))
    )
    # tool_output caps — read the live ToolOutputSettings; best-effort: a
    # minimal test container whose Settings predates the ``tool_output`` field
    # falls back to the ToolOutputSettings defaults so the route never crashes.
    tool_output = getattr(container.settings, "tool_output", None)
    if tool_output is None:
        from qai.platform.config import ToolOutputSettings

        tool_output = ToolOutputSettings()
    return {
        "file_broker_enabled": bool(tools.file_broker_enabled),
        "file_broker_max_entries": int(tools.file_broker_max_entries),
        "ssl_verify": bool(container.settings.ssl_verify),
        "project_skip_dirs": list(tools.project_skip_dirs),
        "global_proxy": tools.global_proxy,
        "file_guard_enabled": bool(sec.file_guard_enabled),
        "allow_exec_tool": bool(sec.allow_exec_tool),
        "native_file_guard_enabled": bool(
            getattr(sec, "native_file_guard_enabled", False)
        ),
        "dependency_approval_enabled": dep_enabled,
        "dependency_approval_deny_args": dep_deny_args,
        "dependency_approval_timeout_s": dep_timeout,
        "command_policy_enabled": exec_enabled,
        "read_max_lines": int(tool_output.read_max_lines),
        "read_max_bytes": int(tool_output.read_max_bytes),
        "read_max_line_length": int(tool_output.read_max_line_length),
        "glob_max_results": int(tool_output.glob_max_results),
        "grep_max_matches": int(tool_output.grep_max_matches),
        "grep_max_line_length": int(tool_output.grep_max_line_length),
        "grep_max_output_bytes": int(tool_output.grep_max_output_bytes),
    }


def _register_runtime_config_routes(
    router: "APIRouter", *, container: "Container"
) -> None:
    @router.get("/runtime-config", response_model=RuntimeConfigResponse)
    async def runtime_config_get() -> RuntimeConfigResponse:
        return RuntimeConfigResponse(**_effective(container))  # type: ignore[arg-type]

    @router.put("/runtime-config", response_model=RuntimeConfigUpdateResponse)
    async def runtime_config_put(
        body: RuntimeConfigUpdateRequest,
    ) -> RuntimeConfigUpdateResponse:
        from apps.api._ai_coding_di import apply_tools_runtime_config
        from apps.api._runtime_config_store import write_runtime_config

        current = _effective(container)
        # Field-whitelisted partial update: only changed fields are persisted.
        submitted = body.model_dump(exclude_none=True)
        # List-valued fields need list() coercion before comparison / persist.
        _list_fields = {"project_skip_dirs", "dependency_approval_deny_args"}
        partial: dict[str, object] = {}
        for key, value in submitted.items():
            normalised = (
                list(value) if key in _list_fields else value
            )
            if normalised != current.get(key):
                partial[key] = normalised

        needs_reboot = any(key in _REBOOT_FIELDS for key in partial)

        # Persist every submitted switch (decision 2A) — write the full
        # submitted set (not just the diff) so a re-submit of the same value
        # still records operator intent into forge_config.
        persist_payload: dict[str, object] = {
            k: (list(v) if k in _list_fields else v)
            for k, v in submitted.items()
        }
        if persist_payload:
            write_runtime_config(container.data_paths.root, persist_payload)

        # Hot-apply the tools seams so they take effect without a restart. We
        # always re-apply the full effective tools view (merged with the
        # submitted overrides) so the live process reflects the new value.
        merged = {**current, **persist_payload}
        if any(key in _HOT_TOOLS_FIELDS for key in submitted):
            apply_tools_runtime_config(
                ssl_verify=bool(merged["ssl_verify"]),
                project_skip_dirs=tuple(merged["project_skip_dirs"]),  # type: ignore[arg-type]
                global_proxy=merged["global_proxy"],  # type: ignore[arg-type]
                # 退化 #9: pass the container so a runtime proxy edit also
                # embeds the user:pass@ credentials (parity with build-time
                # wiring + V1 _webfetch.py:120-143).
                container=container,
            )

        # dependency_approval_enabled hot-applies in place: the FileBroker/
        # FileGuard exec guards hold a live reference to the shared broker and
        # read ``broker.enabled`` at call time, so toggling it takes effect for
        # the next exec without a reboot (never sets needs_reboot).
        if "dependency_approval_enabled" in submitted:
            dep_ns = getattr(container, "dependency_approval", None)
            dep_broker = (
                getattr(dep_ns, "broker", None) if dep_ns is not None else None
            )
            set_enabled = getattr(dep_broker, "set_enabled", None)
            if callable(set_enabled):
                set_enabled(bool(submitted["dependency_approval_enabled"]))

        # M-3 — dependency_approval deny_args / approval_timeout hot-apply in
        # place (V1 ``reload_config``). The live broker reads these at the next
        # ``check_and_wait`` so changes take effect without a reboot.
        if (
            "dependency_approval_deny_args" in submitted
            or "dependency_approval_timeout_s" in submitted
        ):
            dep_ns = getattr(container, "dependency_approval", None)
            dep_broker = (
                getattr(dep_ns, "broker", None) if dep_ns is not None else None
            )
            if "dependency_approval_deny_args" in submitted:
                set_deny = getattr(dep_broker, "set_deny_args", None)
                if callable(set_deny):
                    set_deny(list(submitted["dependency_approval_deny_args"]))  # type: ignore[arg-type]
            if "dependency_approval_timeout_s" in submitted:
                set_to = getattr(dep_broker, "set_approval_timeout_s", None)
                if callable(set_to):
                    set_to(float(submitted["dependency_approval_timeout_s"]))  # type: ignore[arg-type]

        # M-2 — command_policy master switch hot-apply. The FileGuard exec
        # guard holds a live reference and reads ``broker.enabled`` /
        # ``is_enabled`` at call time, so toggling takes effect for the next
        # exec without a reboot (never sets needs_reboot).
        if "command_policy_enabled" in submitted:
            exec_ns = getattr(container, "command_policy", None)
            exec_broker = (
                getattr(exec_ns, "broker", None)
                if exec_ns is not None
                else None
            )
            set_exec_enabled = getattr(exec_broker, "set_enabled", None)
            if callable(set_exec_enabled):
                set_exec_enabled(bool(submitted["command_policy_enabled"]))

        # Unified FileGuard master switch — hot-apply (no reboot).
        # ``file_guard_enabled`` mutates the live Settings so
        # FileGuardFacade's ``enabled_provider`` reflects it on the next
        # guard call. ``native_file_guard_enabled`` mutates the live Settings
        # AND start()/stop()s the guard64.dll hook in place (re-entrant
        # Init/Destroy), re-seeding rules + grant sync on start and draining
        # on stop. Both take effect immediately for BOTH layers.
        if "file_guard_enabled" in submitted:
            try:
                container.settings.security.file_guard_enabled = bool(
                    submitted["file_guard_enabled"]
                )
            except Exception:  # noqa: BLE001 — settings frozen? degrade to reboot
                pass
        if "native_file_guard_enabled" in submitted:
            want_native = bool(submitted["native_file_guard_enabled"])
            try:
                container.settings.security.native_file_guard_enabled = (
                    want_native
                )
            except Exception:  # noqa: BLE001
                pass
            from apps.api._native_hook_rules import (
                start_native_guard,
                stop_native_guard,
            )

            guard = getattr(
                getattr(container, "security", None), "native_file_guard", None
            )
            is_active = bool(getattr(guard, "is_active", False)) if guard else False
            if want_native and not is_active:
                await start_native_guard(container)
            elif not want_native and is_active:
                await stop_native_guard(container)

        # Decision 3B — schedule a reboot when a build-time switch changed so
        # the new value (only readable at the next DI build) takes effect. The
        # frontend shows the custom confirm dialog *before* calling this with
        # the user's consent; the supervisor performs the actual exit(75).
        if needs_reboot:
            reboot = getattr(
                getattr(container, "security", None), "reboot_signal", None
            )
            if reboot is not None:
                await reboot.request_reboot(
                    reason="security runtime-config changed"
                )

        # The GET-style view reflects the *persisted* intent immediately even
        # for reboot-pending fields, so the UI shows the new value (with the
        # reboot banner) rather than the stale build-time value.
        view = {**current, **persist_payload}
        return RuntimeConfigUpdateResponse(
            needs_reboot=needs_reboot,
            persisted=sorted(persist_payload.keys()),
            **view,  # type: ignore[arg-type]
        )


__all__ = [
    "RuntimeConfigResponse",
    "RuntimeConfigUpdateRequest",
    "RuntimeConfigUpdateResponse",
    "_register_runtime_config_routes",
]
