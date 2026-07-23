# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory runtime state for security global controls (PR-504).

The security-global routes (status / toggle / settings / reset) need a
process-local mutable view of "is the FileGuard currently enabled?",
"what mode is it in?", and similar runtime-only flags that operators can
flip without restarting the API.

Persistence (decision 2A, 2026-06 security-settings unification)
----------------------------------------------------------------
The state model is still **in-memory authoritative** for the live
process — the Settings model stays immutable per process. But operator
flips of the runtime buckets (``auto_approve`` / ``path_patterns`` /
``project_access`` / ``skill_policies``) plus the top-level ``enabled`` /
``mode`` / ``dynamic_authorization`` scalars **survive a restart** via an
injected :class:`RuntimeStatePersistencePort`. The security context must
not touch the filesystem / DB directly (domain-purity +
context-isolation), so the apps layer injects a persistence sink that
reads/writes the shared ``forge_config`` document. When no port is
injected (most unit tests) the service behaves exactly as the prior pure
in-memory implementation.

The :func:`build_security_services` wires a single
:class:`SecurityRuntimeStateService` instance per :class:`Container`, so
all routes share state for the lifetime of the API process.

History: renamed from ``SandboxRuntimeStateService`` /
``sandbox_runtime_state.py`` (2026-07-04 de-sandbox refactor) after the
OS-isolation sandbox was removed (2026-07-01, replaced by FileGuard).
The dead OS-sandbox ``sandbox_runtime`` bucket + the derived
``status_snapshot`` / ``compute_stats`` badge views were dropped in the
same pass; the service now only carries the live FileGuard runtime
buckets (``auto_approve`` / ``path_patterns`` / ``project_access`` /
``skill_policies`` / ``policy_overview``). The persisted forge_config
key value was renamed ``"sandbox_runtime_state"`` →
``"security_runtime_state"`` (2026-07 sandbox→path rename; see
``apps.api._runtime_config_store.RUNTIME_STATE_KEY``). The old key is
still read on load for backward compatibility with existing on-disk data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.ports import RuntimeStatePersistencePort

__all__ = [
    "SecurityRuntimeStateService",
    "SecurityRuntimeSnapshot",
]


_DEFAULT_MODE = "enforcing"  # enforcing | permissive | disabled
_DEFAULT_DYNAMIC_AUTHORIZATION = True

# The ``policy_overview`` settings bucket is the SINGLE source of truth for
# the master ``enabled`` switch and the ``run_mode`` (enforce|audit_only)
# sub-switch. It is the same bucket ``GET/PUT /api/security/policy`` and the
# apps-layer ``_run_mode_provider`` read/write, so the runtime snapshot MUST
# derive ``enabled`` from it (never hold an independent, divergible copy).
_POLICY_OVERVIEW_KEY = "policy_overview"


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityRuntimeSnapshot:
    """Read-only view of security runtime state."""

    enabled: bool
    mode: str
    dynamic_authorization: bool
    settings: dict[str, Any]


class SecurityRuntimeStateService:
    """In-process mutable state for security global controls."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        mode: str = _DEFAULT_MODE,
        dynamic_authorization: bool = _DEFAULT_DYNAMIC_AUTHORIZATION,
        persistence: "RuntimeStatePersistencePort | None" = None,
    ) -> None:
        self._lock = Lock()
        self._persistence = persistence
        self._mode = mode
        self._dynamic_authorization = bool(dynamic_authorization)
        # Epoch seconds of the last enable/disable transition observed this
        # process (V1 ``last_transition_time``). 0.0 = no transition yet.
        self._last_transition_time: float = 0.0
        # Free-form k/v "settings" — used by the security config endpoints
        # and the operator UI. Defaults match the legacy behaviour; we keep
        # the shape generic so future additions don't require a migration.
        self._settings: dict[str, Any] = {
            "auto_approve": {
                "enabled": False,
                "trusted_paths": [],
            },
            "path_patterns": {},
            "project_access": {
                "enabled": True,
            },
            "skill_policies": {},
        }
        # ``policy_overview`` is the single-source-of-truth bucket for the
        # master ``enabled`` switch (and ``run_mode``). Seed ``enabled`` from
        # the ctor arg so the snapshot never needs a separate ``_enabled``
        # scalar that could drift from the bucket the routes read/write.
        self._settings[_POLICY_OVERVIEW_KEY] = {"enabled": bool(enabled)}
        # Decision 2A — restore any persisted operator flips so they
        # survive a restart (V1 sandbox_state.json parity). Best-effort:
        # a malformed / absent persisted blob leaves the defaults intact.
        if self._persistence is not None:
            self._restore_from(self._persistence.load())
        # Ctor-time mode/enabled consistency guard (defence-in-depth).
        # After _restore_from, ensure mode and enabled are in lock-step so
        # that external seeding errors (e.g. enabled=False passed from DI)
        # never leave the snapshot in a contradictory state.
        # enabled=False  → force mode to "disabled"
        # enabled=True + mode=="disabled" → reset mode to "enforcing"
        #   (permissive is a deliberate user choice; we only fall back to
        #    enforcing, never to permissive, on an inconsistency correction)
        # No lock needed here — construction is single-threaded.
        _ctor_enabled = self._bucket_enabled_locked()
        if not _ctor_enabled and self._mode != "disabled":
            self._mode = "disabled"
        elif _ctor_enabled and self._mode == "disabled":
            self._mode = "enforcing"

    # ── persistence (decision 2A) ─────────────────────────────────────

    def _bucket_enabled_locked(self) -> bool:
        """Return the master ``enabled`` switch — the bucket is the truth.

        Caller must hold ``self._lock``. Falls back to ``True`` (master
        switch ON) when the bucket / key is absent or mistyped, matching
        the historic default and :func:`_read_overview_state`.
        """
        bucket = self._settings.get(_POLICY_OVERVIEW_KEY)
        value = bucket.get("enabled") if isinstance(bucket, dict) else None
        return value if isinstance(value, bool) else True

    def _set_bucket_enabled_locked(self, enabled: bool) -> None:
        """Write the master ``enabled`` switch into the truth bucket.

        Caller must hold ``self._lock``. Preserves every other key in the
        ``policy_overview`` bucket (``run_mode`` / ``dynamic_authorization``
        / ``no_ui_channels``) so this never clobbers sibling toggles.
        """
        bucket = self._settings.get(_POLICY_OVERVIEW_KEY)
        new_bucket = dict(bucket) if isinstance(bucket, dict) else {}
        new_bucket["enabled"] = bool(enabled)
        self._settings[_POLICY_OVERVIEW_KEY] = new_bucket

    def _serialise(self) -> dict[str, Any]:
        """Return a JSON-ready snapshot of the full runtime state.

        Caller must hold ``self._lock``. Mirrors the shape
        :meth:`_restore_from` consumes so a save→load round-trip is
        lossless. The top-level ``enabled`` scalar is DERIVED from the
        ``policy_overview`` bucket (the single truth source) purely for
        on-disk back-compat — it is never an independent value.
        """
        return {
            "enabled": self._bucket_enabled_locked(),
            "mode": self._mode,
            "dynamic_authorization": self._dynamic_authorization,
            "settings": {k: dict(v) if isinstance(v, dict) else v
                         for k, v in self._settings.items()},
        }

    def _restore_from(self, blob: Any) -> None:
        """Overlay a persisted blob onto the in-memory defaults.

        Tolerant of partial / malformed input: only well-typed keys are
        applied, everything else keeps its default. Caller must NOT hold
        ``self._lock`` (constructor-time use). ``enabled`` is restored into
        the ``policy_overview`` truth bucket; the persisted ``settings``
        bucket (if it carries ``policy_overview.enabled``) takes precedence
        so a legacy top-level ``enabled`` scalar never re-splits the truth.
        """
        if not isinstance(blob, dict) or not blob:
            return
        with self._lock:
            # Legacy top-level scalar → migrate into the truth bucket first;
            # a bucket-carried value below overrides it.
            if isinstance(blob.get("enabled"), bool):
                self._set_bucket_enabled_locked(blob["enabled"])
            mode = blob.get("mode")
            if mode in ("enforcing", "permissive", "disabled"):
                self._mode = mode
            if isinstance(blob.get("dynamic_authorization"), bool):
                self._dynamic_authorization = blob["dynamic_authorization"]
            persisted_settings = blob.get("settings")
            if isinstance(persisted_settings, dict):
                for key, value in persisted_settings.items():
                    if key in self._settings and isinstance(value, dict):
                        self._settings[key] = dict(value)

    def _persist_locked(self) -> None:
        """Write-through the current state (best-effort).

        Caller must hold ``self._lock``. A persistence failure is
        swallowed (the port implementation logs) so a UI toggle never
        500s on a transient disk error — the in-memory state stays
        authoritative for the live process.
        """
        if self._persistence is None:
            return
        try:
            self._persistence.save(self._serialise())
        except Exception:  # noqa: BLE001 — never crash a runtime toggle
            pass

    # ── status / toggle / mode ────────────────────────────────────────

    def snapshot(self) -> SecurityRuntimeSnapshot:
        with self._lock:
            return SecurityRuntimeSnapshot(
                enabled=self._bucket_enabled_locked(),
                mode=self._mode,
                dynamic_authorization=self._dynamic_authorization,
                settings=dict(self._settings),
            )

    def toggle(self, *, enabled: bool) -> SecurityRuntimeSnapshot:
        with self._lock:
            if bool(enabled) != self._bucket_enabled_locked():
                self._last_transition_time = (
                    datetime.now(timezone.utc).timestamp()
                )
            self._set_bucket_enabled_locked(bool(enabled))
            self._persist_locked()
        return self.snapshot()

    def set_mode(self, mode: str) -> SecurityRuntimeSnapshot:
        """Set the security master-switch semantic mode.

        ``mode`` is now a PURE semantic scalar (enforcing | permissive |
        disabled): it no longer flips the ``enabled`` master switch as a
        side effect. Instead, ``disabled`` ⟺ ``policy_overview.enabled ==
        False`` is kept in lock-step by writing the SINGLE truth bucket —
        so the two never drift. ``enforcing`` / ``permissive`` both imply
        the guard is ON (``enabled == True``); the enforce-vs-audit_only
        distinction for ``permissive`` is applied by :meth:`effective_run_mode`
        at decision time, never by disabling the guard.
        """
        if mode not in ("enforcing", "permissive", "disabled"):
            raise ValueError(
                "mode must be one of "
                "{'enforcing', 'permissive', 'disabled'}, got "
                f"{mode!r}"
            )
        with self._lock:
            self._mode = mode
            # enabled is DERIVED from mode via the single truth bucket:
            # disabled ⇒ master switch OFF, else ON. This is the ONLY
            # coupling — a pure ⟺ mapping through one truth source, not an
            # independent second scalar.
            new_enabled = mode != "disabled"
            if new_enabled != self._bucket_enabled_locked():
                self._last_transition_time = (
                    datetime.now(timezone.utc).timestamp()
                )
            self._set_bucket_enabled_locked(new_enabled)
            self._persist_locked()
        return self.snapshot()

    def effective_run_mode(self, bucket_run_mode: str) -> str:
        """Fold the semantic ``mode`` into an effective enforce|audit_only.

        This is the derivation the decision core consumes so the security
        master switch (``mode``) is honoured through the SINGLE existing
        ``audit_only`` override path (``CheckPermissionUseCase``), never a
        second "log-but-allow" implementation:

        * ``enforcing`` → the ``run_mode`` sub-switch stands as-is
          (``enforce`` or ``audit_only``).
        * ``permissive`` → forced ``audit_only`` (log-but-allow) regardless
          of the sub-switch.
        * ``disabled`` → ``audit_only`` (relaxes the toggleable subset;
          the always-on floors — protected_paths / DANGEROUS built-ins /
          the main+child audit sentinels — are enforced independently and
          NEVER read ``mode``, so they are unaffected).

        Fail-safe: any unknown/mistyped ``mode`` is treated as
        ``enforcing`` (the strict default), and an unknown ``bucket_run_mode``
        under ``enforcing`` falls back to ``enforce``.
        """
        mode = self._mode
        if mode == "permissive" or mode == "disabled":
            return "audit_only"
        # enforcing (and any unknown mode, fail-safe to strict) → pass the
        # sub-switch through, defaulting to enforce on a bad value.
        return bucket_run_mode if bucket_run_mode == "audit_only" else "enforce"

    # ── settings ──────────────────────────────────────────────────────

    def get_settings(self, key: str) -> Any:
        with self._lock:
            return self._settings.get(key)

    def update_settings(
        self,
        key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError(
                f"settings value must be a dict, got {type(value).__name__}"
            )
        with self._lock:
            self._settings[key] = dict(value)
            self._persist_locked()
            return dict(self._settings[key])

    def reset(self) -> SecurityRuntimeSnapshot:
        """Reset all runtime state to defaults."""
        with self._lock:
            self._mode = _DEFAULT_MODE
            self._dynamic_authorization = _DEFAULT_DYNAMIC_AUTHORIZATION
            self._last_transition_time = (
                datetime.now(timezone.utc).timestamp()
            )
            self._settings = {
                "auto_approve": {"enabled": False, "trusted_paths": []},
                "path_patterns": {},
                "project_access": {"enabled": True},
                "skill_policies": {},
                # master switch back ON — single truth bucket.
                _POLICY_OVERVIEW_KEY: {"enabled": True},
            }
            self._persist_locked()
        return self.snapshot()
