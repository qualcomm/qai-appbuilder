# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — Policy + health endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.security.application.use_cases.update_policy import (
    UpdatePolicyUseCase,
)

from ._dto import (
    PolicyResponse,
    PolicyVersionResponse,
    SecurityHealthResponse,
    SecurityModeResponse,
    SetSecurityModeRequest,
    UpdatePolicyRequest,
    _persist_overview_state,
    _policy_to_response,
    _read_overview_state,
    _rule_from_dto,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _register_policy_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── policy ─────────────────────────────────────────────────────────

    @router.get("/policy", response_model=PolicyResponse)
    async def get_policy() -> PolicyResponse:
        repo = container.security.policy_repository
        return _policy_to_response(await repo.load(), container=container)

    @router.get("/policy/version", response_model=PolicyVersionResponse)
    async def get_policy_version() -> PolicyVersionResponse:
        repo = container.security.policy_repository
        policy = await repo.load()
        return PolicyVersionResponse(version=policy.version)

    @router.put("/policy", response_model=PolicyResponse)
    async def put_policy(body: UpdatePolicyRequest) -> PolicyResponse:
        # Persist the tail-appended operational toggles FIRST (they live
        # in runtime-state, not the rules-based Policy aggregate). Only
        # fields explicitly present in the body are touched, preserving
        # the original rules-only PUT behaviour byte-for-byte.
        _persist_overview_state(
            container,
            enabled=body.enabled,
            mode=body.mode,
            dynamic_authorization=body.dynamic_authorization,
            no_ui_channels=body.no_ui_channels,
        )
        use_case: UpdatePolicyUseCase = (
            container.security.update_policy_use_case
        )
        rules = tuple(_rule_from_dto(r) for r in body.rules)
        result = await use_case.execute_with_warnings(
            new_rules=rules,
            reboot_reason=body.reboot_reason,
            auto_reboot=False,
        )
        return _policy_to_response(
            result.policy,
            container=container,
            needs_reboot=result.requires_reboot,
        )

    # ── security master switch (mode) — 3c switch-tree §6.4 ────────────

    @router.put("/mode", response_model=SecurityModeResponse)
    async def put_security_mode(
        body: SetSecurityModeRequest,
    ) -> SecurityModeResponse:
        """Set the security master switch (enforcing | permissive | disabled).

        ``mode`` is a semantic master control ORTHOGONAL to the locked
        ``policy.mode`` (enforce | audit_only) sub-switch. Setting it is
        best-effort and folds through ``effective_run_mode`` so
        ``permissive`` / ``disabled`` relax the TOGGLEABLE subset to
        log-but-allow via the single existing ``audit_only`` override —
        the always-on floors (protected_paths / DANGEROUS built-ins / the
        main+child audit sentinels) do NOT read ``mode`` and stay enforced.
        Invalid ``mode`` values are rejected with 422 by the Literal.
        """
        state = container.security.security_runtime_state
        snap = state.set_mode(body.mode)
        # Unify the FileGuard master control under this single semantic switch
        # (2026-07 dedup): the ToolSafety "Enable FileGuard" toggle was removed,
        # so the security mode is now the ONE place the operator turns FileGuard
        # on/off. ``disabled`` ⇒ FileGuard OFF (open-box, no gating); enforcing
        # / permissive ⇒ ON. Drive BOTH the Python tool-layer guard
        # (``file_guard_enabled``, read live by FileGuardFacade) AND the OS-level
        # subprocess hook (``native_file_guard_enabled``) together, hot-applied
        # in place (mirrors ``PUT /runtime-config``'s live-apply block). Composed
        # HERE in the apps/api layer (the only layer allowed to touch both
        # ``qai.security`` runtime-state AND platform ``settings``).
        _want_guard = body.mode != "disabled"
        try:
            container.settings.security.file_guard_enabled = _want_guard
        except Exception:  # noqa: BLE001 — frozen settings: degrade gracefully
            pass
        try:
            container.settings.security.native_file_guard_enabled = _want_guard
        except Exception:  # noqa: BLE001
            pass
        # Persist the coupled FileGuard fields to forge_config so the choice
        # SURVIVES A RESTART in lock-step with the mode (State-Truth-First):
        # ``set_mode`` above persists ``mode`` into the runtime-state region,
        # but ``file_guard_enabled`` / ``native_file_guard_enabled`` boot from
        # the separate ``security_runtime_config`` region + Settings defaults —
        # without this write they would revert to their (ON) defaults on the
        # next boot while ``mode`` stayed ``disabled``, leaving the two out of
        # sync. Mirrors ``PUT /runtime-config``'s persistence path.
        try:
            from apps.api._runtime_config_store import write_runtime_config

            write_runtime_config(
                container.data_paths.root,
                {
                    "file_guard_enabled": _want_guard,
                    "native_file_guard_enabled": _want_guard,
                },
            )
        except Exception:  # noqa: BLE001 — persistence best-effort; live state already set
            pass
        try:
            from apps.api._native_hook_rules import (
                start_native_guard,
                stop_native_guard,
            )

            _guard = getattr(
                getattr(container, "security", None), "native_file_guard", None
            )
            _active = bool(getattr(_guard, "is_active", False)) if _guard else False
            if _want_guard and not _active:
                await start_native_guard(container)
            elif not _want_guard and _active:
                await stop_native_guard(container)
        except Exception:  # noqa: BLE001 — never fail the mode flip on hook error
            pass
        # Fold the current run_mode sub-switch through the freshly-set master
        # switch so the response advertises the run-mode the decision core
        # will actually honour (same derivation as the DI run_mode_provider).
        _enabled, run_mode, _dyn, _no_ui = _read_overview_state(container)
        effective = state.effective_run_mode(run_mode)
        if effective not in ("enforce", "audit_only"):
            effective = "enforce"
        return SecurityModeResponse(
            mode=snap.mode,  # type: ignore[arg-type]
            enabled=snap.enabled,
            effective_run_mode=effective,  # type: ignore[arg-type]
        )

    # ── health (Overview header pill + down/test_mode banner) ──────────

    @router.get("/health", response_model=SecurityHealthResponse)
    async def security_health() -> SecurityHealthResponse:
        """Report FileGuard health for the Overview header (V1 parity).

        Health is derived from observable state: the policy must be
        loadable (else ``down``), and a globally-disabled sandbox maps to
        ``test_mode`` (V1's ``FILEGUARD_DISABLED`` bypass). This is the
        read-only companion to the Overview status card; it never
        mutates state.

        The native sub-process hook state is reported alongside so the
        Overview card can honestly surface "Python on but native DLL
        failed to load" as ``degraded`` rather than "healthy"
        (🔴 State-Truth-First): ``native_enabled`` is operator intent
        (the setting), ``native_active`` is the LIVE probe
        (``NativeFileGuardPort.is_active``). When the software guard is
        enabled and the native hook was requested but is not actually
        active, the composite status is ``degraded``.
        """
        enabled, run_mode, _dyn, _no_ui = _read_overview_state(container)
        # Native sub-process hook state — LIVE probe, never a cached flag
        # (the adapter's ``is_active`` reflects DLL-loaded AND hook-installed,
        # and ``diagnostics`` reads the DLL's internal counters directly).
        native_enabled = bool(
            getattr(
                container.settings.security,
                "native_file_guard_enabled",
                False,
            )
        )
        native_guard = getattr(container.security, "native_file_guard", None)
        try:
            native_active = bool(
                native_guard is not None and native_guard.is_active
            )
        except Exception:  # noqa: BLE001 -- probe must never break health
            native_active = False
        native_diagnostics: dict[str, int] = {}
        if native_active and native_guard is not None:
            try:
                raw = native_guard.diagnostics()
                # Keep only int-valued counters for the wire DTO
                # (dict[str, int]); the DLL snapshot may carry other types.
                native_diagnostics = {
                    str(k): int(v)
                    for k, v in dict(raw).items()
                    if isinstance(v, (int, bool)) and not isinstance(v, str)
                }
            except Exception:  # noqa: BLE001 -- best-effort diagnostics
                native_diagnostics = {}
        try:
            await container.security.policy_repository.load()
        except Exception:  # noqa: BLE001 -- any load failure = fail-closed
            return SecurityHealthResponse(
                status="down",
                enabled=enabled,
                mode=run_mode,  # type: ignore[arg-type]
                test_mode=False,
                native_enabled=native_enabled,
                native_active=native_active,
                native_diagnostics=native_diagnostics,
            )
        if not enabled:
            return SecurityHealthResponse(
                status="test_mode",
                enabled=False,
                mode=run_mode,  # type: ignore[arg-type]
                test_mode=True,
                native_enabled=native_enabled,
                native_active=native_active,
                native_diagnostics=native_diagnostics,
            )
        # Software guard enabled + native requested but not live ⇒ degraded:
        # in-process tool writes are still guarded, but LLM-spawned
        # sub-process file writes are NOT (the DLL failed to load / install).
        status = "degraded" if (native_enabled and not native_active) else "ok"
        return SecurityHealthResponse(
            status=status,  # type: ignore[arg-type]
            enabled=True,
            mode=run_mode,  # type: ignore[arg-type]
            test_mode=False,
            native_enabled=native_enabled,
            native_active=native_active,
            native_diagnostics=native_diagnostics,
        )

