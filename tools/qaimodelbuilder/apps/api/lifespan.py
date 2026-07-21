# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application lifespan: startup / shutdown orchestration.

Replaces the old ``backend/main.py`` lifespan() (570 lines of inline init).
Each step is explicit, ordered, and individually testable.

Order:
1. configure_logging  — capture every subsequent step in structured logs
2. database.start     — verify SQLite + apply PRAGMAs
3. migrate            — run any pending schema migrations
4. install audit hook — PEP 578 IO interception (L5 PR-502)
5. yield              — app is ready
6. on shutdown: uninstall audit hook, close database, drain event bus

Failures during startup raise; FastAPI propagates them and the supervisor
(future ``apps/cli/serve.py``) decides whether to retry / exit 75.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.logging import configure_logging, get_logger
from qai.platform.persistence import migrate

from . import _runtime_endpoint
from .di import Container

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from qai.platform.scheduling import BackgroundTaskManager

_log = get_logger(__name__)


# Phase 3 cleanup (2026-07-01) — the
# ``SandboxedProcessRunner._allocate_sandbox_dir`` per-call ``mkdtemp``
# helper, the per-invocation ``qai_sandbox_*`` directory pattern, the
# AppContainer/LPAC launcher binary itself and the entire orphan
# cleanup sweep have been deleted. ``_cleanup_orphaned_sandbox_dirs``
# stays defined below as an inert no-op stub so the lifespan call site
# does not need a feature flag, but it scans no directories.

# Tool-result store GC (V1 ``tool_result_storage.cleanup_old_results`` parity).
# A periodic background task sweeps ``data/tool_results/`` removing persisted
# oversized tool outputs older than the age threshold so the directory does not
# grow without bound across a long-running deployment. The sweep runs once
# shortly after startup, then every ``_TOOL_RESULT_GC_INTERVAL_SECONDS``.
_TOOL_RESULT_GC_MAX_AGE_HOURS = 24
_TOOL_RESULT_GC_INTERVAL_SECONDS = 6 * 60 * 60


def make_lifespan(
    *,
    container: Container,
    migrations_dir: Path | None = None,
):
    """Return a lifespan async context manager bound to ``container``.

    ``migrations_dir`` defaults to the bundled
    ``src/qai/platform/persistence/migrations_sql/`` directory; tests may
    inject any path (or an empty one).
    """
    resolved_migrations = migrations_dir or _default_migrations_dir(container.repo_root)

    @contextlib.asynccontextmanager
    async def lifespan(_app: "FastAPI") -> AsyncIterator[None]:
        configure_logging(
            level=container.settings.logging.level,  # type: ignore[arg-type]
            fmt=_resolve_fmt(container.settings.logging.fmt),
        )
        _log.info(
            "lifespan.starting",
            edition=container.settings.edition,
            host=container.settings.server.host,
            port=container.settings.server.port,
            data_dir=str(container.data_paths.root),
        )

        # SEC true-scoping — the backend process BOOT ID is minted once when
        # the Container is built (``apps/api/di.py`` ``_wire_platform``:
        # ``self.boot_id = self.ids.new_id()``). It is the ``scope_key`` for
        # ``process``-scoped PathGrants: a grant approved with scope=process
        # is keyed to this id and stops matching after a restart (a new process
        # builds a new container → new boot id, so a stale process grant from a
        # prior run never matches). Here we only ensure it is present +
        # non-empty (an empty boot id would make process grants silently
        # un-matchable) and log it. Best-effort uuid4 fallback for any exotic
        # test container that bypassed ``_wire_platform``.
        if not getattr(container, "boot_id", ""):
            import uuid

            try:
                container.boot_id = uuid.uuid4().hex
            except Exception:  # noqa: BLE001 — never let this abort boot
                pass
        _log.info("lifespan.boot_id_minted", boot_id=getattr(container, "boot_id", ""))

        # P-17 §6.3 — the protected-paths audit bypass sink (constructed +
        # started below with the hook). Declared here so it is in scope for the
        # shutdown ``finally`` (closed BEFORE the DB so its final drain still
        # has a live connection). ``None`` when no facade is wired (inert).
        audit_bypass_sink = None

        # ALWAYS-ON protected-paths guard (independent of FileGuard / sandbox,
        # both disabled by default). Seed the user-configured EXTRA protected
        # write prefixes from settings; the built-in non-removable set
        # (``C:\Qualcomm`` / QAIRT SDK) is enforced regardless. Origin:
        # 2026-06-16 incident (stray write truncated the QAIRT generator exe).
        try:
            from qai.platform import main_process_audit_sentinel, protected_paths

            extra = getattr(
                getattr(container.settings, "security", None),
                "protected_write_paths",
                (),
            )
            # 1) configure the effective prefix set BEFORE installing the hook
            #    so the in-process audit hook sees the user-configured paths too.
            protected_paths.set_user_protected_paths(extra)
            # P-17 §6.3 — build + start the zero-IO audit bypass sink BEFORE
            # installing the hook, then hand its ``enqueue`` in as the hook's
            # ``on_violation`` callback. Each in-process protected-write deny is
            # now enqueued (zero-IO, on the audited thread) and drained onto a
            # dedicated loop that awaits ``SecurityAuditFacade.record`` → the
            # canonical ``security_audit_entry`` table, closing the "hook denies
            # but records nothing" observability gap. When the facade is absent
            # (hand-rolled test container) the sink is inert and ``on_violation``
            # is None — the hook keeps its historical deny-only behaviour.
            _audit_facade = getattr(
                getattr(container, "security", None),
                "security_audit_facade",
                None,
            )
            if _audit_facade is not None:
                from apps.api._audit_bypass_sink import AuditBypassSink

                audit_bypass_sink = AuditBypassSink(_audit_facade)
                audit_bypass_sink.start()
            # 2) install the in-process audit hook (3rd tier: catches direct
            #    in-process writes that bypass the write/edit tools). This is
            #    separate from any FileGuard hook and only ever DENIES the small
            #    protected set, so enabling FileGuard later cannot conflict.
            main_process_audit_sentinel.install_protected_paths_audit_hook(
                on_violation=(
                    audit_bypass_sink.enqueue
                    if audit_bypass_sink is not None
                    else None
                )
            )
            # P-08 #6 — wire the CHILD-process protected-deny audit relay. The
            # child sentinel (isolated interpreter) emits a stderr marker per
            # deny; the ``exec`` handler parses it and calls this callback via
            # ``qai.platform.child_process_deny_audit`` (the shared kernel, so
            # neither ``qai.ai_coding`` nor ``qai.platform`` imports ``apps``).
            # Rows are tagged with the child subject/source. Inert when no sink.
            from qai.platform import child_process_deny_audit

            child_process_deny_audit.set_on_child_protected_deny(
                audit_bypass_sink.enqueue_child
                if audit_bypass_sink is not None
                else None
            )
            _log.info(
                "protected_paths.configured",
                builtin=len(protected_paths.BUILTIN_PROTECTED_PREFIXES),
                user=len(tuple(extra or ())),
                audit_hook=main_process_audit_sentinel.is_installed(),
                audit_bypass=audit_bypass_sink is not None,
            )
        except Exception:  # noqa: BLE001 — guard config must never block boot
            _log.warning("protected_paths.configure_failed", exc_info=True)

        # 9-L1 — Windows asyncio ``WinError 10054`` suppressor (V1
        # ``backend/main.py:201-232`` parity). On Windows the
        # ProactorEventLoop calls ``socket.shutdown`` during connection
        # teardown; when the remote client has already disconnected
        # (common after long SSE streams) Windows raises
        # ``ConnectionResetError`` (WinError 10054) inside an asyncio
        # callback. asyncio cannot propagate a callback exception so it
        # logs it as [ERROR], cluttering logs with benign noise. We install
        # a global exception handler that silently discards
        # ``ConnectionResetError`` and forwards everything else to the
        # original (or built-in) handler unchanged. ``sys.platform`` guard
        # keeps this a graceful no-op on non-Windows (the WinError 10054
        # noise is Windows-ProactorEventLoop specific); cross-platform
        # neutral per AGENTS.md. Ref: https://bugs.python.org/issue39010
        if sys.platform == "win32":
            _install_connection_reset_suppressor()

        await container.database.start()
        # State-Truth-First 铁律5: startup steps that may raise to abort the
        # boot (``migrate`` schema failure, ``_verify_sandbox_launcher_or_fail``)
        # run BEFORE the ``try/finally`` whose ``finally`` closes the DB. If one
        # raises, the just-opened DB connection would leak (the supervisor
        # respawns us, and the crash-restart limiter would repeat the leak up
        # to its bound). Close the DB on any early-startup failure before
        # re-raising so no connection is orphaned per failed boot.
        try:
            applied = await migrate(
                container.database, migrations_dir=resolved_migrations
            )
        except BaseException:
            await _close_database_quietly(container)
            raise
        if applied:
            _log.info("lifespan.migrations_applied", ids=applied)

        # 9-G1 — strip persisted ``meta.request_id`` from chat messages on
        # startup (prompt-snapshot parity; see ``_clear_request_ids``).
        await _clear_request_ids(container)

        # MCP integration — connect every persisted MCP server IN THE BACKGROUND
        # (when the ``chat.chat_mcp_enabled`` gate is on) so their tools are
        # advertised without the user re-adding them. Returns immediately: the
        # actual (slow, npx-download) connect runs as a background task so it
        # NEVER blocks service startup. No-op / best-effort when disabled.
        await _connect_mcp_servers(container)

        # State-Truth-First 铁律 (AGENTS.md 🔴): at startup no stream can
        # possibly be running — any tab stuck in ``streaming`` is a dirty
        # remnant from a prior crash, a failed ``_release_streaming_tab``
        # save, or a race condition where the release was swallowed.  Reset
        # all such tabs to ``idle`` so users aren't permanently locked out.
        await _recover_streaming_tabs(container)

        # One-shot user-Pack relocation (Sub-E). Before Sub-C wired the
        # ``DataPaths.app_builder_user_pack_root`` split, user-imported
        # App Builder Packs were committed to
        # ``<repo_root>/factory/app_builder/models/<id>/`` alongside
        # built-in Packs (with weight ``.bin`` blobs at
        # ``<repo_root>/models/<id>/``). Sub-D updates the adapters so
        # **future** imports land at the data-dir tree; this hook
        # migrates any **existing** user Pack from the legacy location
        # to ``<data_dir>/app_builder/{user_models,user_model_weights}/<id>/``
        # and rewrites each manifest's ``installPath`` so the adapters
        # resolve weights against the new anchor. Runs AFTER schema
        # migrations (needs the DB up to read ``user_imported``) and
        # BEFORE the built-in seed (so the seed scan of
        # ``factory/app_builder/models/`` no longer sees the just-
        # relocated user Packs and cannot mis-promote them to
        # ``user_imported=False``). Idempotent per-id via a ``.migrated_at``
        # sentinel: rerun is a no-op after the first success and
        # self-heals a crash mid-copy. Best-effort — a migration
        # failure logs a warning but must never abort startup; the
        # source is preserved on failure so the next boot retries.
        try:
            from scripts.migrate.move_user_packs_to_data import (
                migrate_user_packs,
            )

            _pack_report = await migrate_user_packs(
                repo_root=container.repo_root,
                data_paths=container.data_paths,
                db=container.database,
            )
            if not _pack_report.is_noop():
                _log.info(
                    "lifespan.app_builder_user_packs_migrated",
                    scanned=_pack_report.scanned,
                    migrated=_pack_report.migrated,
                    retried_incomplete=_pack_report.retried_incomplete,
                    skipped_builtin=len(_pack_report.skipped_builtin),
                    skipped_orphan=len(_pack_report.skipped_orphan),
                    skipped_already_migrated=len(
                        _pack_report.skipped_already_migrated
                    ),
                    failed=_pack_report.failed,
                )
        except Exception:  # noqa: BLE001 — migration must never abort startup
            _log.warning(
                "lifespan.app_builder_user_pack_migration_failed",
                exc_info=True,
            )

        # App Builder built-in model seed. V1 surfaced the bundled
        # models by scanning ``features/app-builder/models/`` on every
        # boot; the v2.7 architecture moved the source of truth to the
        # ``app_builder_model_definition`` table but never wired the
        # disk-scan → DB registration (PR-061 was planned, never landed),
        # so a fresh install showed "No models available". This step
        # closes that gap: it scans the bundled Pack root and inserts a
        # registry row for every manifest whose id is not already in the
        # table. Existing rows (incl. user edits / imports) are left
        # untouched, so the seed is idempotent and non-destructive.
        try:
            seeded = await _seed_app_builder_models(container)
            if seeded:
                _log.info("lifespan.app_builder_models_seeded", ids=seeded)
        except Exception:  # noqa: BLE001 — seed must never abort startup
            _log.warning(
                "lifespan.app_builder_model_seed_failed", exc_info=True
            )

        # State-Truth-First — reconcile orphan model rows whose on-disk pack
        # is gone (V1 "listed == pack exists on disk" invariant). Runs AFTER
        # the seed so freshly-seeded built-ins survive. Best-effort.
        try:
            removed_models = await _reconcile_orphan_app_builder_models(container)
            if removed_models:
                _log.info(
                    "lifespan.app_builder_orphan_models_reconciled",
                    ids=removed_models,
                )
        except Exception:  # noqa: BLE001 — reconcile must never abort startup
            _log.warning(
                "lifespan.app_builder_orphan_model_reconcile_failed",
                exc_info=True,
            )

        # G5 (State-Truth-First) — reconcile orphaned non-terminal runs.
        # After an unclean exit of the previous API process (crash / kill /
        # power loss) the DB can hold app-builder runs stuck in
        # pending/running/streaming whose driving drainer task no longer
        # exists; nothing would ever transition them to a terminal state, so
        # the history list would show them "running" forever. Sweep them to
        # FAILED once at startup before serving traffic. Best-effort: a sweep
        # failure must never abort startup.
        try:
            run_repo = getattr(
                getattr(container, "app_builder", None), "run_repository", None
            )
            reconcile = getattr(run_repo, "reconcile_stale_runs", None)
            if reconcile is not None:
                reconciled = await reconcile()
                if reconciled:
                    _log.info(
                        "lifespan.app_builder_stale_runs_reconciled",
                        count=reconciled,
                    )
        except Exception:  # noqa: BLE001 — reconcile must never abort startup
            _log.warning(
                "lifespan.app_builder_stale_run_reconcile_failed", exc_info=True
            )

        # Phase 3 cleanup (2026-07-01) — the Windows AppContainer/LPAC
        # sandbox launcher chain has been deleted, so the previous
        # fail-fast probe (``_verify_sandbox_launcher_or_fail``) and the
        # persistent daemon-pool startup are no longer needed. The
        # de-sandbox refactor (2026-07-04) removed the ``None``
        # ``sandboxed_process_runner`` / ``daemon_manager`` placeholder
        # fields entirely (see ``apps/api/_security_di.py``).
        daemon_manager_started = False

        # De-sandbox refactor (2026-07-04) — the OS-isolation sandbox was
        # removed (2026-07-01, replaced by FileGuard). The orphaned
        # ``SandboxConfigChangedEvent`` hot-reload pipeline (which rebuilt
        # the deleted ``SandboxConfigHolder`` via ``coerce_sandbox_config``)
        # was deleted alongside the security-side sandbox execution
        # framework, so there is no longer a subscription to register here.
        # Phase 3 cleanup (2026-07-01) — the orphan ``qai_sandbox_*``
        # temp-directory sweep was tied to ``SandboxedProcessRunner``'s
        # per-call ``mkdtemp`` allocator. With the AppContainer/LPAC
        # launcher chain deleted no such directories are ever created;
        # the sweep call is removed (the helper itself is kept below as
        # an inert no-op stub to avoid touching any indirect importer).

        # PEP 578 audit hook — install after DB is ready (policy may be
        # loaded from DB) but before yielding (all subsequent IO is guarded).
        audit_hook_handle = None
        # 2026-07-04 native-hook integration (PR-4) — grant create/revoke →
        # native allow-rule sync subscriptions. Declared here (alongside the
        # other startup teardown handles) so the shutdown ``finally`` can
        # always drain them even if a later startup step raises.
        native_guard_grant_subs: list = []
        if getattr(container.settings, "security", None) and getattr(
            container.settings.security, "audit_hook_enabled", False
        ):
            try:
                from qai.security.infrastructure.audit_hook import install_audit_hook

                # audit_hook policy_provider must be synchronous (called on
                # every IO event hot-path; see install_audit_hook docstring).
                # Pre-load the policy once at startup into an in-memory cache
                # and serve from there.  The hot-reload watcher (below) will
                # refresh _cached_policy when the policy changes at runtime.
                _cached_policy = await container.security.policy_repository.load()
                _policy_cache: list = [_cached_policy]  # mutable cell for closure

                audit_hook_handle = install_audit_hook(
                    policy_provider=lambda: _policy_cache[0],
                    # PR-092 §2.2 H-17 / §17.5 #4 — extend the handled
                    # event set with operator-configured extras
                    # (typically ``os.scandir`` / ``os.listdir`` /
                    # ``shutil.copyfile``).
                    extra_events=tuple(
                        getattr(
                            container.settings.security,
                            "audit_hook_extra_events",
                            (),
                        )
                        or ()
                    ),
                )
                _log.info("lifespan.audit_hook_installed")
            except Exception:  # noqa: BLE001
                _log.warning("lifespan.audit_hook_install_failed", exc_info=True)

        # 2026-07-04 native-hook integration — start the OS-level
        # guard64.dll hook after the DB is ready (grant hot-sync in PR-4
        # reads it) but before yielding (all subsequent subprocess file
        # events are guarded). Gated on ``native_file_guard_enabled``: the
        # DI layer already wired a zero-side-effect no-op when disabled, so
        # this only does real work when the operator opted in. PR-3 wires
        # the asyncio ASK filter callback via ``set_filter_callback`` before
        # this ``start`` runs. A load / init failure is logged and leaves
        # the hook inactive rather than crashing startup.
        native_file_guard = getattr(
            getattr(container, "security", None), "native_file_guard", None
        )
        if native_file_guard is not None and getattr(
            container.settings.security, "native_file_guard_enabled", False
        ):
            # Start the OS-level guard64.dll hook at boot when the unified
            # FileGuard master switch is on. The shared helper wires the ASK
            # bridge + seeds deny/allow rules + subscribes grant sync and
            # returns the subscription handles (stashed on the guard too).
            # The master-switch route reuses the same helper for live toggles.
            from apps.api._native_hook_rules import start_native_guard

            native_guard_grant_subs = await start_native_guard(container)

        # 2026-07-06 P-09 — startup orphan-boot reconciliation. Unresolved
        # rows in the durable ``security_pending_permission`` table whose
        # ``boot_id`` differs from THIS process's boot id belong to a previous
        # process whose native DLL pipe thread is already dead — the waiter
        # can never be woken. We resolve them as ``shutdown`` (matches native
        # FailDecision shutdown semantics; the schema CHECK has no 'orphaned'
        # value) so stale rows don't linger forever and ``/permission/pending``
        # surfaces only live rows. Best-effort: any failure is logged and
        # never aborts startup (State-Truth-First §5). We do NOT re-hydrate
        # these into the in-memory registry — a dead previous-process waiter
        # cannot be woken.
        try:
            _pending_store = getattr(
                getattr(container, "security", None),
                "permission_pending_store",
                None,
            )
            if _pending_store is not None:
                _boot_id = str(getattr(container, "boot_id", "") or "")
                _orphaned = await _pending_store.resolve_orphaned_boots(_boot_id)
                _log.info(
                    "lifespan.pending_orphan_boots_resolved",
                    resolved=_orphaned,
                    boot_id=_boot_id,
                )
        except Exception:  # noqa: BLE001 — reconcile must never abort startup
            _log.warning(
                "lifespan.pending_orphan_boots_failed", exc_info=True
            )

        # 2026-07-06 Phase 2 — start the subprocess-gone cleanup service.
        # Scans the in-memory PermissionWaitRegistry (+ durable
        # security_pending_permission table) every 10s and resolves any
        # pending ASK whose subprocess is dead as ``subprocess_gone``. This
        # is the safety net for the Phase 2 INFINITE ASK wait (no more
        # auto-DENY on 60s elapse — users may be away for days; but a
        # subprocess that died has no way to answer, so we resolve it
        # instead of hanging the future forever). Best-effort: a spawn
        # failure must never abort startup (State-Truth-First §5).
        pending_cleanup_service = getattr(
            getattr(container, "security", None),
            "pending_cleanup_service",
            None,
        )
        pending_cleanup_task: "asyncio.Task | None" = None
        if pending_cleanup_service is not None:
            try:
                pending_cleanup_task = pending_cleanup_service.start()
                _log.info("lifespan.pending_cleanup_started")
            except Exception:  # noqa: BLE001 — spawn must never abort startup
                _log.warning(
                    "lifespan.pending_cleanup_spawn_failed", exc_info=True
                )

        # PR-092 §2.1 C-9 / §17.5 #10 — Policy hot-reload watcher.
        # The new architecture stores Policy in SQLite (not a flat
        # file), so the watcher's loader callback is supplied by the
        # caller responsible for the on-disk policy snapshot. When no
        # snapshot path is configured, the watcher stays dormant.
        policy_hot_reload_watcher = None
        policy_files: tuple = ()
        policy_loader = getattr(container, "policy_hot_reload_loader", None)
        if (
            getattr(container.settings, "security", None)
            and getattr(
                container.settings.security,
                "policy_hot_reload_enabled",
                False,
            )
            and policy_loader is not None
        ):
            policy_files = tuple(
                getattr(container, "policy_hot_reload_paths", ()) or ()
            )
            if policy_files:
                try:
                    from qai.security.adapters.policy_hot_reload import (
                        PolicyHotReloadWatcher,
                    )

                    policy_hot_reload_watcher = PolicyHotReloadWatcher(
                        watched_paths=policy_files,
                        update_policy_use_case=container.security.update_policy_use_case,
                        loader=policy_loader,
                    )
                    await policy_hot_reload_watcher.start()
                    _log.info(
                        "lifespan.policy_hot_reload_started",
                        files=[str(p) for p in policy_files],
                    )
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.policy_hot_reload_start_failed",
                        exc_info=True,
                    )

        # GAP-2 — load skill capability declarations from the on-disk factory
        # so the SkillCapabilityRegistry has the per-skill read/exec grants the
        # sandbox policy builder consults. Best-effort (getattr-guarded): a
        # missing loader / registry / factory dir simply leaves the registry
        # empty (no skill-declared paths granted).
        try:
            _sec = getattr(container, "security", None)
            _registry = (
                getattr(_sec, "skill_capability_registry", None)
                if _sec
                else None
            )
            _repo_root = getattr(container, "repo_root", None)
            if _registry is not None and _repo_root is not None:
                from qai.security.infrastructure import (
                    skill_capability_loader,
                )

                from ._workspace_resolver import resolve_workspace_root

                _loaded = await skill_capability_loader.load_all(
                    _registry,
                    Path(_repo_root),
                    # Inject the configured model-builder workspace root so
                    # any ``${WORKSPACE}`` token in a skill pack's path lists
                    # expands to the operator-configured root.
                    workspace_root=resolve_workspace_root(container),
                )
                _log.info(
                    "lifespan.skill_capabilities_loaded",
                    count=len(_loaded),
                )
        except Exception:  # noqa: BLE001
            _log.warning(
                "lifespan.skill_capabilities_load_failed", exc_info=True
            )

        # PR-302 wiring — pre-spawn the persistent App Builder sticky
        # worker (V1 ``backend/main.py:631-642`` ``ensure_worker_process``)
        # so the first inference doesn't pay the ~8s process spawn cost
        # and the model stays resident across runs (no per-run
        # model_destroy / NPU Error 0x200). Best-effort: a non-NPU /
        # ARM64-venv-less host degrades to the one-shot fallback runner
        # without aborting startup.
        await _spawn_sticky_worker(container)

        # 9-L4 — Voice ASR warm-load on startup (best-effort; see
        # ``_spawn_voice_warmup``). Runs AFTER the worker is spawned so
        # the warm-up loads into the resident worker (V1 ordering).
        _spawn_voice_warmup(container)

        # H-1 — auto-connect channels whose ``auto_start`` is set, mirroring
        # V1 ``backend/main.py:516-525`` (``_auto_connect_wechat``) and
        # ``575-593`` (``_auto_start_feishu``). Fire-and-forget so a slow /
        # failing channel connect never blocks or aborts startup (V1 also
        # used ``asyncio.create_task``); the StartChannelInstanceUseCase's own
        # error path marks the instance ERROR on failure (e.g. Feishu without
        # credentials configured) without crashing the loop.
        #
        # State-Truth-First (铁律1) — BEFORE auto-start, reset any persisted
        # ``running`` / ``starting`` / ``stopping`` channel status back to
        # ``stopped``.  On a process restart every in-memory transport (Feishu
        # WS / WeChat long-poll) is gone, so a persisted active status is
        # necessarily stale; left as-is the WebUI would show a dead channel as
        # "已连接" and auto-start would silently skip it (it only starts
        # ``stopped`` instances).  V1/v0.5 had no such bug because their
        # connection status is an in-memory global that resets to ``stopped``
        # on every restart.  Awaited (not fire-and-forget) so the truth is
        # corrected before the auto-start task reads the repo below.
        await _reset_stale_channel_status(container)
        _spawn_channel_auto_start(container)

        # RE-OC-2 — auto-start the local OpenCode service when its config
        # doc has ``enabled=true`` AND ``auto_start=true``, mirroring V1
        # ``backend/main.py:443-467`` (``_auto_start_oc``). Fire-and-forget
        # so a slow / failing spawn never blocks or aborts startup; the
        # OcService adapter live-reads cli_path/port from the same config
        # doc (RE-OC-1) so no values need to be threaded here.
        _spawn_oc_service_auto_start(container)

        # S-E — anonymous usage reporter (internal edition only;
        # edition-dual-form-design.md §5). The four-layer internal-asset
        # defence's runtime-gate layer lives here: the reporter is registered
        # ONLY when ``container.settings.is_internal`` (the external edition
        # never schedules nor sends a report). Endpoints + fields come from
        # ``container.settings.usage`` (config), so no internal-network domain
        # literal is embedded in the wiring. Registration + scheduling are
        # entirely fire-and-forget and non-raising, so a failure here can never
        # abort startup.
        usage_task_manager: "BackgroundTaskManager | None" = None
        # Edition gate FIRST: ``qai.platform.usage`` is an internal-only module
        # that is physically excluded from the external artifact (manifest.toml
        # [exclude]). Importing it on the external edition would raise
        # ImportError (caught below, but it logs a spurious warning on every
        # external boot). Short-circuit before the import so the external
        # edition never touches the module — the runtime-gate layer of the
        # four-layer internal-asset defence.
        if container.settings.is_internal:
            try:
                from qai.platform.scheduling import BackgroundTaskManager
                from qai.platform.usage import register_usage_reporter

                from apps.api._global_proxy import build_ssl_verify_provider

                _usage_mgr = BackgroundTaskManager()
                if register_usage_reporter(
                    _usage_mgr,
                    usage_settings=container.settings.usage,
                    is_internal=container.settings.is_internal,
                    ssl_verify=container.settings.ssl_verify,
                    # Live provider so a runtime SSL toggle hot-applies to the
                    # usage reporter's HTTPS legs (read at post time).
                    ssl_verify_provider=build_ssl_verify_provider(container),
                ):
                    await _usage_mgr.start()
                    usage_task_manager = _usage_mgr
                    _log.info("lifespan.usage_reporter_started")
            except Exception:  # noqa: BLE001 — usage reporting must never abort startup
                _log.warning("lifespan.usage_reporter_start_failed", exc_info=True)

        # Tool-result store GC — periodic sweep of ``data/tool_results/``
        # (V1 ``tool_result_storage.cleanup_old_results`` parity). The store
        # persists oversized tool outputs (exec stdout/stderr, large
        # grep/glob results) so the model can ``read`` them back; without a
        # GC the directory grows unbounded across a long-running deployment.
        # Best-effort + fire-and-forget (State-Truth-First 铁律5): spawning /
        # running the sweep must never abort or block startup, so the task is
        # created defensively and the loop body swallows every exception.
        tool_result_gc_task: "asyncio.Task | None" = None
        try:
            _tr_store = getattr(
                getattr(container, "ai_coding", None), "tool_result_store", None
            )
            if _tr_store is not None and hasattr(_tr_store, "cleanup"):
                tool_result_gc_task = asyncio.create_task(
                    _tool_result_gc_loop(_tr_store),
                    name="tool-result-gc",
                )
                _log.info("lifespan.tool_result_gc_spawned")
        except Exception:  # noqa: BLE001 — GC spawn must never abort startup
            _log.warning("lifespan.tool_result_gc_spawn_failed", exc_info=True)

        # Edit-trash global cleanup — one-shot sweep of the edit-trash
        # recovery ledger(s) at startup. ``backup_to_trash`` is append-only
        # (per-file prune runs after each commit, but a process that never
        # re-touches a file would leave its stale backups forever, and the
        # total-size cap / manifest compaction are inherently cross-file), so
        # a startup sweep enforces the TTL + total cap + compacts the manifest.
        # The trash root FOLLOWS each edited file's workspace (it is per-request
        # and unknown here), so we sweep the roots that ARE predictable at
        # startup: the operator/test override ``$QAI_EDIT_TRASH_ROOT``, the
        # repo-root ``.edit_trash`` (dev checkout), and the per-user fallback
        # dir. Per-workspace trash in arbitrary user workspaces is still bounded
        # by the cheap per-file prune that runs on every commit. Fire-and-forget
        # + best-effort (State-Truth-First 铁律5): a sweep failure must never
        # abort or block startup.
        try:
            asyncio.create_task(
                _edit_trash_cleanup_once(container.repo_root),
                name="edit-trash-cleanup",
            )
            _log.info("lifespan.edit_trash_cleanup_spawned")
        except Exception:  # noqa: BLE001 — cleanup spawn must never abort startup
            _log.warning("lifespan.edit_trash_cleanup_spawn_failed", exc_info=True)

        # Cloud-model permission scan — one lightweight ``GET /v1/models`` per
        # configured cloud provider, comparing the returned model ids against
        # the configured catalog to derive per-model ALLOWED / DENIED status.
        # Purely advisory: the chat model dropdown hides models the current
        # API key has no access to (before the user hits 403 mid-turn), while
        # a failed / never-completed scan keeps every model visible
        # (never-preset-unavailable). Best-effort + non-blocking:
        #
        #   * spawned AFTER every other startup step so a slow provider probe
        #     never delays uvicorn's "ready to serve" moment;
        #   * the use case itself never raises (each provider is guarded), so
        #     the outermost try/except here is a defence-in-depth net;
        #   * the task is fire-and-forget: no shutdown drain, no await; even
        #     if the scan is still in flight when the app stops it will just
        #     be cancelled by the event loop.
        try:
            _probe_permissions_uc = (
                container.model_catalog.probe_cloud_model_permissions_use_case
            )

            async def _run_permission_scan() -> None:
                try:
                    result = await _probe_permissions_uc.execute()
                    _log.info(
                        "lifespan.cloud_model_permissions_scanned",
                        probed=len(result.probed_providers),
                        skipped=len(result.skipped_providers),
                    )
                except Exception:  # noqa: BLE001 — scan must never surface
                    _log.warning(
                        "lifespan.cloud_model_permissions_scan_failed",
                        exc_info=True,
                    )

            asyncio.create_task(
                _run_permission_scan(),
                name="cloud-model-permissions-scan",
            )
            _log.info("lifespan.cloud_model_permissions_scan_spawned")
        except Exception:  # noqa: BLE001 — spawn must never abort startup
            _log.warning(
                "lifespan.cloud_model_permissions_scan_spawn_failed",
                exc_info=True,
            )

        try:
            # Write the runtime endpoint file (single source of truth for
            # "where is the API now") just before yielding control to
            # uvicorn / FastAPI. By this point all startup steps have
            # succeeded and uvicorn has already bound the listening
            # socket, so the file accurately reflects a port that is
            # actually serving traffic. Best-effort: a write failure
            # (read-only data dir, full disk) is logged and swallowed —
            # downstream consumers fall back gracefully when the file
            # is absent. See ``apps/api/_runtime_endpoint.py``.
            try:
                # The actual port uvicorn binds may differ from
                # ``settings.server.port`` when the supervisor picked a
                # fallback port (see ``apps/cli/serve.py:FALLBACK_PORTS``).
                # The supervisor injects ``QAI_RUNTIME_PORT`` into the
                # child's environment with the true value; we prefer that
                # over settings so the endpoint file contains the URL users
                # can actually reach.
                _runtime_port = int(
                    os.environ.get(
                        "QAI_RUNTIME_PORT",
                        str(container.settings.server.port),
                    )
                )
                _runtime_endpoint.write_endpoint(
                    container.data_paths.root,
                    host=container.settings.server.host,
                    port=_runtime_port,
                    pid=os.getpid(),
                )
                _log.info(
                    "lifespan.runtime_endpoint_written",
                    host=container.settings.server.host,
                    port=_runtime_port,
                )
            except Exception:  # noqa: BLE001 — endpoint file is advisory
                _log.warning(
                    "lifespan.runtime_endpoint_write_failed", exc_info=True
                )

            # Heartbeat — log a periodic "still alive" line every 30 s so
            # operators can confirm the server is running without tailing
            # the full request log. Best-effort: the task is cancelled on
            # shutdown in the ``finally`` block below.
            _heartbeat_task: asyncio.Task | None = None
            try:
                _heartbeat_task = asyncio.create_task(
                    _heartbeat_loop(
                        host=container.settings.server.host,
                        port=_runtime_port,
                        interval=30,
                    ),
                    name="lifespan-heartbeat",
                )
            except Exception:  # noqa: BLE001
                _log.warning("lifespan.heartbeat_start_failed", exc_info=True)

            _log.info("lifespan.background_process_ready")

            # FastAPI's lifespan must yield either None or a state dict.
            # We expose ``container`` via ``app.state.container`` instead so
            # callers don't have to navigate FastAPI's nested state mapping.
            yield None
        finally:
            _log.info("lifespan.shutting_down")
            if _heartbeat_task is not None and not _heartbeat_task.done():
                _heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await _heartbeat_task
            # Remove the runtime endpoint file as the very first
            # teardown step so any consumer polling for it sees the
            # "server is going down" signal as early as possible
            # (Start.bat-side polling, desktop shell health checks).
            # Best-effort: clear_endpoint never raises, just returns
            # False on permission / FS errors.
            if _runtime_endpoint.clear_endpoint(container.data_paths.root):
                _log.info("lifespan.runtime_endpoint_cleared")
            # De-sandbox refactor (2026-07-04) — the SandboxConfigChangedEvent
            # subscription was removed alongside the orphaned sandbox
            # hot-reload pipeline, so there is nothing to drain here.
            # Phase 3 cleanup (2026-07-01) — daemon pool shutdown was
            # tied to the (removed) ``daemon_manager`` field; there is no
            # shutdown to invoke. ``daemon_manager_started`` is
            # unconditionally False, so the guarded branch below is
            # functionally dead; kept here as an explicit no-op so future
            # readers see the cleanup happened rather than wondering why a
            # shutdown call vanished.
            if daemon_manager_started:  # pragma: no cover -- always False post Phase 3
                pass
            if policy_hot_reload_watcher is not None:
                try:
                    await policy_hot_reload_watcher.stop()
                    _log.info("lifespan.policy_hot_reload_stopped")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.policy_hot_reload_stop_failed",
                        exc_info=True,
                    )
            if audit_hook_handle is not None:
                try:
                    audit_hook_handle.uninstall()
                    _log.info("lifespan.audit_hook_uninstalled")
                except Exception:  # noqa: BLE001
                    _log.warning("lifespan.audit_hook_uninstall_failed", exc_info=True)
            # 2026-07-04 native-hook integration — unload the guard64.dll
            # hook (idempotent). Drains the grant create/revoke sync
            # subscriptions first, then stops the guard, via the shared
            # helper (also used by the master-switch live toggle).
            from apps.api._native_hook_rules import stop_native_guard

            await stop_native_guard(container, native_guard_grant_subs)
            # R-2 / D6 — stop the local inference service (GenieAPIService
            # subprocess) so it does not outlive the API process as an
            # orphan. Bounded by ``timeout`` so a wedged graceful stop
            # cannot hang shutdown (graceful 8s + kill 3s + buffer).
            _inference = getattr(
                getattr(container, "model_runtime", None),
                "inference_service",
                None,
            )
            if _inference is not None:
                try:
                    await asyncio.wait_for(_inference.stop(), timeout=15.0)
                    _log.info("lifespan.inference_service_stopped")
                except asyncio.TimeoutError:
                    _log.warning("lifespan.inference_service_stop_timeout")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.inference_service_stop_failed", exc_info=True
                    )
            # Stop the OpenCode server subprocess so it does not outlive the
            # API process as an orphan. ``_spawn_oc_service_auto_start`` (boot)
            # may have started it, but shutdown previously never stopped it —
            # so every reboot leaked an OC process and its port (铁律5). The
            # adapter also assigns the child to a Job Object as a hard-kill
            # backstop; this is the graceful, bounded path. Best-effort.
            _oc_service = getattr(
                getattr(container, "ai_coding", None),
                "oc_service",
                None,
            )
            if _oc_service is not None:
                try:
                    await asyncio.wait_for(_oc_service.stop(), timeout=10.0)
                    _log.info("lifespan.oc_service_stopped")
                except asyncio.TimeoutError:
                    _log.warning("lifespan.oc_service_stop_timeout")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.oc_service_stop_failed", exc_info=True
                    )
            # R-2 / D6 — stop the aria2c download daemon (best-effort,
            # idempotent) so the RPC daemon subprocess is reaped.
            _aria2c = getattr(
                getattr(container, "service_release", None),
                "aria2c_manager",
                None,
            )
            if _aria2c is not None:
                try:
                    await _aria2c.stop()
                    _log.info("lifespan.aria2c_stopped")
                except Exception:  # noqa: BLE001
                    _log.warning("lifespan.aria2c_stop_failed", exc_info=True)
            # R-3 — cancel App Builder run-drain background tasks.
            _broadcaster = getattr(
                getattr(container, "app_builder", None),
                "run_stream_broadcaster",
                None,
            )
            if _broadcaster is not None and hasattr(_broadcaster, "aclose"):
                try:
                    await _broadcaster.aclose()
                    _log.info("lifespan.run_stream_broadcaster_closed")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.run_stream_broadcaster_close_failed",
                        exc_info=True,
                    )
            # Block 2 — cancel sub-agent live-stream broadcaster background
            # tasks (mirrors the App Builder broadcaster aclose above).
            _sa_broadcaster = getattr(
                getattr(container, "chat", None),
                "subagent_stream_broadcaster",
                None,
            )
            if _sa_broadcaster is not None and hasattr(
                _sa_broadcaster, "aclose"
            ):
                try:
                    await _sa_broadcaster.aclose()
                    _log.info("lifespan.subagent_stream_broadcaster_closed")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.subagent_stream_broadcaster_close_failed",
                        exc_info=True,
                    )
            # MCP integration — close all MCP server sessions / drop registered
            # MCP tool handlers so a re-wire does not double-register (mirrors
            # the broadcaster aclose above). Best-effort; never blocks teardown.
            _mcp_registry = getattr(
                getattr(container, "chat", None),
                "mcp_server_registry",
                None,
            )
            if _mcp_registry is not None and hasattr(_mcp_registry, "aclose"):
                # Cancel an in-flight background startup connect (if the service
                # is shut down while MCP servers are still connecting) before
                # closing the registry, so it does not race the aclose.
                _connect_task = getattr(
                    _mcp_registry, "_startup_connect_task", None
                )
                if _connect_task is not None and not _connect_task.done():
                    _connect_task.cancel()
                    with contextlib.suppress(Exception):
                        await _connect_task
                try:
                    await _mcp_registry.aclose()
                    _log.info("lifespan.mcp_server_registry_closed")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.mcp_server_registry_close_failed",
                        exc_info=True,
                    )
            # 9-L4 — cancel App Builder background tasks (the voice-input
            # warm-up spawned at startup + any in-flight benchmark drives)
            # so fire-and-forget coroutines are not orphaned on shutdown.
            _ab_tasks = getattr(
                getattr(container, "app_builder", None),
                "background_tasks",
                None,
            )
            if _ab_tasks is not None and hasattr(_ab_tasks, "cancel_all"):
                try:
                    await _ab_tasks.cancel_all()
                    _log.info("lifespan.app_builder_background_tasks_cancelled")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.app_builder_background_tasks_cancel_failed",
                        exc_info=True,
                    )
            # PR-302 wiring — shut down the persistent sticky worker so its
            # subprocess (and the NPU context it holds) is reaped instead
            # of outliving the API process as an orphan (State-Truth铁律5:
            # exception-path cleanup). Bounded by ``timeout`` so a wedged
            # graceful op:shutdown cannot hang teardown (the host's own
            # FORCE_KILL_S=5 escalates to SIGKILL; the extra buffer covers
            # the op:shutdown round-trip). Done after the background tasks
            # are cancelled so no in-flight run is mid-stream on the worker.
            _sticky_host = getattr(container, "sticky_worker_host", None)
            if _sticky_host is not None:
                try:
                    await asyncio.wait_for(
                        _sticky_host.shutdown(reason="lifespan_shutdown"),
                        timeout=12.0,
                    )
                    _log.info("lifespan.sticky_worker_shutdown")
                except asyncio.TimeoutError:
                    _log.warning("lifespan.sticky_worker_shutdown_timeout")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.sticky_worker_shutdown_failed", exc_info=True
                    )
            # R-4 — cancel the WeChat QR-login background task (single
            # instance; ``StopChannelInstance`` does not own it).
            _qr_login = getattr(
                getattr(container, "channels", None),
                "wechat_personal_qr_login",
                None,
            )
            if _qr_login is not None and hasattr(_qr_login, "logout"):
                try:
                    await _qr_login.logout()
                    _log.info("lifespan.wechat_qr_login_logged_out")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.wechat_qr_login_logout_failed", exc_info=True
                    )
            # R-5 — sweep any residual inbound consumer tasks not torn
            # down by an explicit StopChannelInstance.
            try:
                from qai.channels.application.use_cases.manage_lifecycle import (
                    cancel_all_inbound_consumers,
                )

                await cancel_all_inbound_consumers()
                _log.info("lifespan.inbound_consumers_cancelled")
            except Exception:  # noqa: BLE001
                _log.warning(
                    "lifespan.inbound_consumers_cancel_failed", exc_info=True
                )
            # State-Truth-First (铁律1, symmetric fix) — persist ``stopped``
            # for any still-active channel instance on graceful shutdown. The
            # inbound consumers were just cancelled and the transports are
            # being torn down, so the DB should not keep a ``running`` row that
            # would be stale on the next boot. Reuses the boot-time reset (it
            # only touches active, non-error states). Best-effort: a failure
            # here must never block teardown — the boot-time reset is the
            # backstop for hard kills / power loss that skip this path.
            try:
                await _reset_stale_channel_status(container)
                _log.info("lifespan.channel_status_persisted_stopped")
            except Exception:  # noqa: BLE001
                _log.warning(
                    "lifespan.channel_status_persist_stopped_failed",
                    exc_info=True,
                )
            # background_process platform module — shut down BEFORE the
            # usage reporter (design.md §10.3 "usage reporter 之前") and
            # BEFORE ``events.close()`` so the manager's terminal
            # ``BackgroundProcessUpdated`` / ``Deleted`` envelopes are
            # still delivered to any subscribed SSE client. The 10s timeout
            # bounds the kill cascade (graceful SIGTERM + force-kill +
            # buffer); the Win32 Job Object on the kill group is the
            # State-Truth-First iron-rule-5 backstop if we time out (the
            # OS reaps every spawned child once the daemon process
            # exits regardless).
            _bg_process = getattr(container, "background_process", None)
            if _bg_process is not None and getattr(_bg_process, "manager", None) is not None:
                try:
                    await asyncio.wait_for(
                        _bg_process.manager.shutdown(),
                        timeout=10.0,
                    )
                    _log.info("lifespan.background_process_shutdown")
                except asyncio.TimeoutError:
                    _log.warning(
                        "lifespan.background_process_shutdown_timeout"
                    )
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.background_process_shutdown_failed",
                        exc_info=True,
                    )
            # S-E — stop the usage reporter scheduler (best-effort, bounded)
            # so its 24h-interval background task is cancelled instead of
            # outliving the API process. Only present under the internal
            # edition (otherwise ``usage_task_manager`` stays None).
            if usage_task_manager is not None:
                try:
                    await usage_task_manager.shutdown(timeout=5.0)
                    _log.info("lifespan.usage_reporter_stopped")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.usage_reporter_stop_failed", exc_info=True
                    )
            # Tool-result store GC — cancel the periodic sweep so its
            # background task is not orphaned on shutdown (best-effort).
            if tool_result_gc_task is not None:
                tool_result_gc_task.cancel()
                try:
                    await tool_result_gc_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                _log.info("lifespan.tool_result_gc_cancelled")
            # 2026-07-06 Phase 2 — stop the subprocess-gone cleanup service so
            # its 10s scan loop is not orphaned on shutdown (best-effort).
            if pending_cleanup_service is not None:
                try:
                    await pending_cleanup_service.stop()
                    _log.info("lifespan.pending_cleanup_stopped")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.pending_cleanup_stop_failed",
                        exc_info=True,
                    )
            # Cancel any still-pending boot auto-start tasks (channel / OC).
            # They normally complete in seconds and self-discard, but if
            # shutdown races a slow first-connect (Feishu WS network timeout)
            # or a wedged OC spawn, an un-cancelled task would be abandoned as
            # the loop closes ("Task was destroyed but it is pending"). Cancel
            # + await them like the other fire-and-forget tasks (铁律5).
            _pending_autostart = list(_CHANNEL_AUTOSTART_TASKS) + list(
                _OC_AUTOSTART_TASKS
            )
            for _task in _pending_autostart:
                if not _task.done():
                    _task.cancel()
            for _task in _pending_autostart:
                try:
                    await _task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if _pending_autostart:
                _log.info("lifespan.autostart_tasks_cancelled")
            # P-17 §6.3 — drain + stop the protected-paths audit bypass sink
            # BEFORE closing the DB so its bounded final drain can still flush
            # any queued in-process denies into ``security_audit_entry``. The
            # sink's ``close`` is synchronous (it waits on its own dedicated
            # loop) and best-effort — a DB already tearing down just drops the
            # last rows; never blocks / raises into shutdown (铁律5).
            if audit_bypass_sink is not None:
                try:
                    audit_bypass_sink.close()
                    _log.info("lifespan.audit_bypass_sink_closed")
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "lifespan.audit_bypass_sink_close_failed", exc_info=True
                    )
                # P-08 #6 — unwire the child-deny relay so a post-shutdown exec
                # (or a stale ref) never enqueues into the closed sink.
                try:
                    from qai.platform import child_process_deny_audit

                    child_process_deny_audit.set_on_child_protected_deny(None)
                except Exception:  # noqa: BLE001,S110 — unwire is best-effort
                    pass
            await container.events.close()
            await container.database.close()
            _log.info("lifespan.stopped")

    return lifespan


async def _seed_app_builder_models(container: Container) -> list[str]:
    """Register bundled App Builder models that aren't yet in the DB.

    Scans ``<pack_root>/<id>/manifest.json`` (default Pack root:
    ``<repo_root>/factory/app_builder/models``) and inserts an
    :class:`AppModelDefinition` row for every manifest whose ``modelId``
    is absent from ``app_builder_model_definition``. Existing rows are
    never modified — this is a one-way "seed if missing" so user edits /
    imports survive restarts. Returns the list of newly-seeded ids.

    Mirrors V1's disk-scan registry (``backend/app_builder/registry.py``)
    so a fresh install surfaces the built-in models exactly like V1.

    Invariant: this scans **built-in** Pack roots only — every row it
    inserts is ``user_imported=False``. The scan root is resolved by
    ``_resolve_seed_pack_root`` which enforces that it never points at the
    user-imported Pack directory (``DataPaths.app_builder_user_pack_root``);
    the user-import commit path owns writing user rows with
    ``user_imported=True``. See ``_resolve_seed_pack_root`` for the "B4"
    mis-promotion bug this split prevents.
    """
    import json

    from qai.app_builder.adapters import SqliteAppModelRepository
    from qai.app_builder.domain.app_model import AppModelDefinition
    from qai.app_builder.domain.taxonomy import Taxonomy
    from qai.app_builder.domain.value_objects import AppModelId

    pack_root = _resolve_seed_pack_root(container)
    if pack_root is None:
        return []

    repo = SqliteAppModelRepository(db=container.database, clock=container.clock)
    existing = {m.id.value for m in await repo.list_all()}

    seeded: list[str] = []
    for child in sorted(pack_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log.warning(
                "lifespan.app_builder_manifest_unreadable",
                path=str(manifest_path),
            )
            continue
        if not isinstance(obj, dict):
            continue
        model_id = obj.get("modelId")
        title = obj.get("displayName")
        if not isinstance(model_id, str) or not isinstance(title, str):
            continue
        if not model_id.strip() or not title.strip():
            continue
        if model_id in existing:
            continue
        try:
            definition = AppModelDefinition(
                id=AppModelId(value=model_id),
                title=title,
                taxonomy=Taxonomy(segments=_manifest_taxonomy_segments(obj)),
                user_imported=False,
            )
        except ValueError:
            _log.warning(
                "lifespan.app_builder_manifest_invalid",
                model_id=model_id,
                exc_info=True,
            )
            continue
        await repo.save(definition)
        seeded.append(model_id)
    return seeded


async def _reconcile_orphan_app_builder_models(container: Container) -> list[str]:
    """Delete DB rows whose on-disk Pack no longer exists (orphan sweep).

    V1's gallery was disk-scan driven, so deleting a pack directory made the
    model vanish. V2's persistent ``app_builder_model_definition`` table does
    NOT self-heal: a pack removed externally/manually (or a row left by an
    earlier partial delete) lingers as a phantom "Ready" model the user can
    still select but never run. This startup sweep restores the V1 invariant
    "listed == pack exists on disk" by removing every row (built-in OR
    user-imported — the user chose full V1 parity) whose
    ``manifest.json`` is gone under **either** the built-in Pack root
    (``<repo_root>/factory/app_builder/models``) OR the user-import Pack
    root (``<data_dir>/app_builder/user_models``).

    Runs after the seed (so freshly-seeded built-ins are kept) and before
    serving traffic. The runtime list use case ALSO filters orphans live
    (covers packs deleted while the server is up); this sweep keeps the DB
    itself clean. Best-effort: a failure must never abort startup. Returns the
    list of removed ids.

    Bug fix — dual-root scan: previously this sweep only probed the built-in
    ``pack_root``, so **every user-imported model was misclassified as an
    orphan on the very next restart** and silently deleted from the DB
    (import succeeded → visible in the same session, then poof after
    restart). The user-imported Pack under
    ``<data_dir>/app_builder/user_models/<id>/manifest.json`` had never
    lived under the built-in root by design (§ Sub-C conservative-C
    layering), so the built-in-only ``is_file()`` check on
    ``pack_root / mid / "manifest.json"`` was structurally guaranteed to
    fail for every user model. We now probe **both** roots — same detection
    logic as :class:`FileSystemWeightsPresence.pack_dir_present`, which the
    runtime list use case already uses — so a pack visible in the running
    gallery is also treated as present by this restart-time sweep.
    """
    from qai.app_builder.adapters import SqliteAppModelRepository
    from qai.app_builder.domain.errors import AppModelNotFoundError

    pack_root = _resolve_seed_pack_root(container)
    # Resolve the user-import root the same way (defensive against test
    # containers that omit ``data_paths``). Either root may legitimately
    # be missing — the reconciler MUST refuse to run when NEITHER is
    # present, otherwise it would sweep every row as an orphan simply
    # because the whole filesystem view is unwired (e.g. a lean unit
    # test container). Match the FileSystemWeightsPresence fail-open
    # semantics.
    user_pack_root: Path | None = None
    data_paths = getattr(container, "data_paths", None)
    if data_paths is not None:
        candidate = getattr(data_paths, "app_builder_user_pack_root", None)
        if isinstance(candidate, Path) and candidate.is_dir():
            user_pack_root = candidate
    if (pack_root is None or not pack_root.is_dir()) and user_pack_root is None:
        return []

    def _pack_present_anywhere(mid: str) -> bool:
        # Mirrors ``FileSystemWeightsPresence.pack_dir_present`` — a pack
        # is "present" iff its ``manifest.json`` exists under EITHER
        # root. Returning True on the first match keeps user-imported
        # models (which never live under the built-in root) from being
        # falsely swept as orphans on restart.
        for root in (pack_root, user_pack_root):
            if root is None:
                continue
            if (root / mid / "manifest.json").is_file():
                return True
        return False

    repo = SqliteAppModelRepository(db=container.database, clock=container.clock)
    removed: list[str] = []
    for model in await repo.list_all():
        mid = model.id.value
        if _pack_present_anywhere(mid):
            continue  # pack present under at least one root — keep
        try:
            await repo.delete(model.id)
            removed.append(mid)
        except AppModelNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            _log.warning(
                "lifespan.app_builder_orphan_delete_failed",
                model_id=mid,
                exc_info=True,
            )
    return removed


def _resolve_seed_pack_root(container: Container) -> Path | None:
    """Resolve the **built-in** Pack root used by the model seed.

    Returns the on-disk directory holding release-distributed built-in Packs
    (``<repo_root>/factory/app_builder/models``) — or ``None`` when it does
    not exist. Preference order:

    1. ``container.app_builder_pack_root`` — an explicit override supplied by
       tests / the lifespan wiring. Callers MUST only inject a built-in
       Pack root here (see invariant below).
    2. ``<repo_root>/factory/app_builder/models`` — the release-distributed
       built-in root.

    Invariant (must be preserved by every caller that ever wires this):
        The path this function returns is scanned by
        ``_seed_app_builder_models`` which inserts each ``manifest.json``
        found there as ``user_imported=False`` — i.e. a **built-in** model
        row. It MUST therefore point at a location that holds ONLY
        release-distributed Packs — never at
        ``DataPaths.app_builder_user_pack_root``
        (``<data_dir>/app_builder/user_models/``), which is the destination
        of the user-import commit path (needs-2 / conservative-C layering).

    Why the split matters (subtle B4 bug this prevents):
        The user-import flow writes its own DB row with
        ``user_imported=True`` when it commits an imported Pack to
        ``user_models/``. If the seed scanner were ever pointed at the user
        root, then on a fresh install / DB reset the seed would find the
        already-imported user Pack directories and silently re-register
        them as built-in (``user_imported=False``) — permanently
        mis-classifying them (built-in Packs cannot be deleted through the
        normal delete flow, so the user would lose control of their own
        Pack). Keeping this resolver built-in-only is the single-source
        guarantee that stops that promotion path.
    """
    injected = getattr(container, "app_builder_pack_root", None)
    if isinstance(injected, Path) and injected.is_dir():
        # Defensive invariant: the injected root MUST NOT be the user-Pack
        # root (see the "subtle B4 bug" note above). Cheap identity check
        # against DataPaths.app_builder_user_pack_root when a DataPaths
        # instance is reachable on the container; a silent no-op otherwise
        # (unit tests supply SimpleNamespace containers without data_paths).
        data_paths = getattr(container, "data_paths", None)
        user_root_prop = getattr(data_paths, "app_builder_user_pack_root", None)
        if user_root_prop is not None:
            try:
                if injected.resolve() == Path(user_root_prop).resolve():
                    raise AssertionError(
                        "_resolve_seed_pack_root refuses to scan the user "
                        "Pack root (would mis-promote user-imported Packs "
                        "to built-in on DB reset — see docstring for the B4 "
                        "invariant)."
                    )
            except OSError:
                # ``resolve()`` on a non-existent path is fine (strict=False
                # by default) — the OSError branch is defensive-only.
                pass
        return injected
    candidate = (
        container.repo_root / "factory" / "app_builder" / "models"
    )
    return candidate if candidate.is_dir() else None


async def _tool_result_gc_loop(store: object) -> None:
    """Periodically sweep expired files from the tool-result store.

    Runs one sweep shortly after startup (a 1s settle delay keeps it off
    the boot hot-path) then repeats every
    ``_TOOL_RESULT_GC_INTERVAL_SECONDS``. Each ``cleanup`` call is the
    store's own best-effort GC (V1 ``cleanup_old_results`` parity); we wrap
    it again here so a transient error (or a store that raises) is logged
    and the loop survives — a GC failure must never tear down the running
    service (State-Truth-First 铁律5). Cancelled at shutdown via
    ``task.cancel()`` (the ``CancelledError`` propagates out of the sleep).
    The ``cleanup`` call is offloaded to a thread because it does blocking
    filesystem I/O (``iterdir`` / ``stat`` / ``unlink``).
    """
    cleanup = getattr(store, "cleanup", None)
    if not callable(cleanup):
        return
    # Small settle delay so the first sweep doesn't compete with boot I/O.
    await asyncio.sleep(1.0)
    while True:
        try:
            removed = await asyncio.to_thread(
                cleanup, _TOOL_RESULT_GC_MAX_AGE_HOURS
            )
            if removed:
                _log.info("lifespan.tool_result_gc_swept", removed=removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a GC error must never kill the loop
            _log.warning("lifespan.tool_result_gc_sweep_failed", exc_info=True)
        await asyncio.sleep(_TOOL_RESULT_GC_INTERVAL_SECONDS)


async def _edit_trash_cleanup_once(repo_root: Path) -> None:
    """One-shot startup sweep of the predictable edit-trash recovery ledgers.

    Enforces the TTL + total-size cap and compacts ``manifest.jsonl`` for the
    trash roots that are knowable at startup, PLUS any active user-workspace
    trash roots recorded during a previous run of this process (the per-request
    workspace trash is unknown at boot, but once an edit lands there it is
    remembered in ``_safe_commit.active_trash_roots`` and also gets a throttled
    per-process global-cap sweep at commit time — see
    ``_safe_commit.backup_to_trash``):

    #. ``$QAI_EDIT_TRASH_ROOT`` (operator / test override), if set.
    #. ``<repo_root>/.edit_trash`` (the dev-checkout workspace trash).
    #. The per-user fallback dir (``%LOCALAPPDATA%\\QAIModelBuilder\\edit_trash``
       / ``~/.qai/edit_trash``) used when no workspace root was writable.
    #. Any active user-workspace trash roots recorded this process.

    A small settle delay keeps it off the boot hot-path. Each sweep is the
    ``_safe_commit`` layer's own best-effort cleanup (never raises); the
    blocking filesystem walk is offloaded to a thread. Best-effort throughout:
    a failure here must never tear down the running service
    (State-Truth-First 铁律5).
    """
    try:
        from qai.ai_coding.infrastructure.tools._safe_commit import (
            TRASH_ROOT_ENV,
            _TRASH_DIRNAME,
            _user_app_trash_dir,
            active_trash_roots,
            cleanup_trash_root,
        )
    except Exception:  # noqa: BLE001 — import guard, never abort
        return

    await asyncio.sleep(1.0)

    roots: list[Path] = []
    env_override = os.environ.get(TRASH_ROOT_ENV)
    if env_override:
        roots.append(Path(env_override))
    try:
        roots.append(Path(repo_root) / _TRASH_DIRNAME)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        pass
    user_dir = _user_app_trash_dir()
    if user_dir is not None:
        roots.append(user_dir)
    # Include any active user-workspace trash roots seen this process (the
    # busy ``C:\WoS_AI\.edit_trash`` class the predictable list omits).
    try:
        roots.extend(active_trash_roots())
    except Exception:  # noqa: BLE001 — registry read must never abort
        pass

    # De-duplicate (resolved) so we never sweep the same tree twice.
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except (OSError, ValueError):  # pragma: no cover — defensive
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        try:
            removed = await asyncio.to_thread(cleanup_trash_root, root)
            if removed:
                _log.info(
                    "lifespan.edit_trash_cleanup_swept", root=str(root), removed=removed
                )
        except Exception:  # noqa: BLE001 — a sweep error must never propagate
            _log.warning(
                "lifespan.edit_trash_cleanup_failed", root=str(root), exc_info=True
            )



def _manifest_taxonomy_segments(obj: dict) -> tuple[str, ...]:
    """Extract ``(group, task)`` taxonomy segments from a manifest dict.

    Thin alias over the domain single-source-of-truth
    :func:`qai.app_builder.domain.taxonomy.manifest_taxonomy_segments`, shared
    with the import-commit materialiser
    (``qai.app_builder.infrastructure.app_import_adapter._materialise_from_source``)
    so the seed and import paths can never drift in how they classify a Pack.
    Reads ``manifest.taxonomy.{group, task}`` (object form) or the legacy list
    form, falling back to the ``category`` mapping.
    """
    from qai.app_builder.domain.taxonomy import (  # noqa: PLC0415
        manifest_taxonomy_segments,
    )

    return manifest_taxonomy_segments(obj)


def _default_migrations_dir(repo_root: Path) -> Path:
    """Resolve the SQL migrations directory.

    Prefer ``<repo_root>/src/qai/platform/persistence/migrations_sql/`` for
    workspace runs; fall back to the in-package directory shipped with the
    installed ``qai.platform.persistence`` module so tests using a
    throw-away ``repo_root=tmp_path`` (e.g. the integration HTTP fixture)
    can still locate migrations and bring tables up.

    Falling back is deterministic: the package directory is the same source
    of truth used at PR-013 install time, so production and tests apply the
    identical schema set.
    """
    candidate = repo_root / "src" / "qai" / "platform" / "persistence" / "migrations_sql"
    if candidate.exists():
        return candidate
    # Fall back to the directory bundled with the installed package.
    import qai.platform.persistence as _persistence

    package_dir = Path(_persistence.__file__).resolve().parent / "migrations_sql"
    return package_dir


def _resolve_fmt(value: str) -> str | None:
    """Settings stores 'auto' / 'json' / 'console'; logging takes None for auto."""
    return None if value == "auto" else value


async def _close_database_quietly(container: Container) -> None:
    """Best-effort ``database.close()`` for the early-startup failure path.

    Used when startup raises before the main ``try/finally`` (whose
    ``finally`` normally closes the DB) so a just-opened connection is not
    leaked on a failed boot. Never raises — teardown of a failed boot must
    not mask the original error.
    """
    try:
        await container.database.close()
    except Exception:  # noqa: BLE001 — never mask the original startup error
        _log.warning("lifespan.early_failure_db_close_failed", exc_info=True)


async def _clear_request_ids(container: Container) -> None:
    """Strip persisted ``meta.request_id`` from chat messages on startup.

    9-G1 — V1 ``history_store.py:660-686`` parity. The in-memory
    PromptSnapshotStore is empty after a restart, so a leftover
    ``request_id`` points at a snapshot that no longer exists and the UI's
    "view prompt snapshot" button 404s. Clearing it keeps the DB consistent
    with the volatile snapshot store. Best-effort: a missing chat namespace
    / repo simply skips the cleanup (never aborts startup).
    """
    try:
        _chat = getattr(container, "chat", None)
        _conversations = getattr(_chat, "conversations", None) if _chat else None
        if _conversations is not None and hasattr(
            _conversations, "clear_request_ids"
        ):
            cleared = await _conversations.clear_request_ids()
            if cleared:
                _log.info("lifespan.request_ids_cleared", rows=cleared)
    except Exception:  # noqa: BLE001 — cleanup must never abort startup
        _log.warning("lifespan.request_ids_clear_failed", exc_info=True)


async def _connect_mcp_servers(container: Container) -> None:
    """Connect every persisted MCP server at startup — WITHOUT blocking boot.

    MCP integration — the chat MCP registry persists its server configs to
    ``<data>/config/mcp_servers.json`` but does not connect until asked. On
    boot we connect persisted, enabled servers so their tools are advertised to
    the LLM from the first turn (no user re-add).

    🔴 Non-blocking (fire-and-forget): connecting a stdio server can be SLOW —
    ``npx`` downloads the package (and, e.g. for Playwright, a browser engine)
    on first launch, taking tens of seconds. Awaiting ``connect_all`` here would
    stall the ENTIRE service startup by that long (observed ~50s for a single
    cold ``memory`` server). So we spawn it as a background task and return
    immediately: the HTTP server comes up at once, and MCP tools become
    available as soon as the background connect finishes (well before the user's
    first turn in practice). The task handle is stashed on the registry so the
    shutdown path can cancel it. Best-effort — the registry's secure-by-default
    gate makes it a no-op when MCP is disabled, and any connect failure is
    recorded per-server (shown in the UI), never aborting startup.
    """
    try:
        _chat = getattr(container, "chat", None)
        _registry = getattr(_chat, "mcp_server_registry", None) if _chat else None
        if _registry is None or not hasattr(_registry, "connect_all"):
            return

        async def _bg_connect() -> None:
            try:
                await _registry.connect_all()
                _log.info("lifespan.mcp_servers_connected")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log.warning("lifespan.mcp_servers_connect_failed", exc_info=True)

        task = asyncio.create_task(_bg_connect(), name="lifespan-mcp-connect")
        # Stash on the registry so shutdown can cancel an in-flight connect.
        with contextlib.suppress(Exception):
            _registry._startup_connect_task = task  # type: ignore[attr-defined]
        _log.info("lifespan.mcp_servers_connecting_in_background")
    except Exception:  # noqa: BLE001 — MCP connect must never abort startup
        _log.warning("lifespan.mcp_servers_connect_failed", exc_info=True)


async def _recover_streaming_tabs(container: Container) -> None:
    """Reset any tabs stuck in ``streaming`` back to ``idle`` on startup.

    State-Truth-First (AGENTS.md 🔴 铁律5): at process start NO stream can
    be running — any ``chat_conversation_tab`` row with ``status='streaming'``
    is a dirty remnant from a prior crash, a failed ``_release_streaming_tab``
    DB save, or a swallowed exception whose ``asyncio.shield``-ed release was
    cancelled. Without this recovery the affected tab is permanently dead
    (every ``start_stream()`` raises ``requires status=IDLE, got streaming``).

    Operates at the DB level (not via the domain port) for two reasons:
    1. Startup runs BEFORE the DI-wired use cases are fully ready.
    2. A raw UPDATE is idempotent, atomic, and cannot raise ``TabStateError``.

    Best-effort: failure never aborts startup.
    """
    try:
        async with container.database.connection() as conn:
            cur = await conn.execute(
                "UPDATE chat_conversation_tab SET status = 'idle' "
                "WHERE status = 'streaming'"
            )
            recovered = cur.rowcount
            await cur.close()
            await conn.commit()
        if recovered:
            _log.warning(
                "lifespan.streaming_tabs_recovered",
                count=recovered,
            )
    except Exception:  # noqa: BLE001 — recovery must never abort startup
        _log.warning("lifespan.streaming_tabs_recovery_failed", exc_info=True)


def _resolve_native_guard_trust_token(container: Container) -> str | None:
    """Fetch the FileGuard TrustedInfra token from the native-guard adapter.

    Phase-1 identity: the host generates a random per-launch token inside
    :meth:`NativeFileGuard.start` and registers it with the native DLL via
    ``SetTrustedInfraToken``. Spawned worker children whose env carries the
    matching ``QAI_FILEGUARD_TRUST_TOKEN`` are classified as TrustedInfra
    inside DllMain (pass-through on undetermined ops; op-mask deny still
    enforced). Returns ``None`` when the adapter is disabled / not started
    (the worker then runs as an untrusted child under normal rule pipeline).

    Never raises — a missing attribute or exception path degrades to
    ``None`` so a diagnostics glitch never blocks the sticky-worker spawn.
    """
    try:
        security = getattr(container, "security", None)
        if security is None:
            return None
        adapter = getattr(security, "native_file_guard", None)
        if adapter is None:
            return None
        getter = getattr(adapter, "get_trusted_infra_token", None)
        if getter is None:
            return None
        token = getter()
        if not isinstance(token, str) or not token:
            return None
        return token
    except Exception:  # noqa: BLE001 — token missing must never block spawn
        _log.debug(
            "lifespan.sticky_worker_trust_token_lookup_failed", exc_info=True
        )
        return None


async def _spawn_sticky_worker(container: Container) -> None:
    """Spawn the persistent App Builder sticky worker (PR-302 wiring).

    V1 ``backend/main.py:631-642`` pre-spawns the shared worker process at
    startup via ``ensure_worker_process`` so the first inference doesn't
    pay the spawn cost and the model stays resident across runs. We mirror
    that: build the ``--persistent`` :class:`BootstrapSpec` (ARM64 venv
    python + ``_runner_bootstrap.py --persistent`` + QAIRT SDK env/PATH
    extras), spawn a :class:`StickyWorkerHost`, and expose it as
    ``container.sticky_worker_host`` so the (already-built)
    :class:`StickyBackedAppRunner` and the worker-status adapter pick it
    up live.

    Graceful degradation (NPU / SDK guard): the heavy ``qai_appbuilder`` /
    QNN import happens **inside** the spawned subprocess, not here — so a
    host without the ARM64 venv / NPU never imports it in-process. If the
    spawn or the ``worker_ready`` handshake fails (no ARM64 venv, missing
    SDK, non-NPU box), we log a warning and leave
    ``container.sticky_worker_host`` unset; every run then transparently
    uses the one-shot fallback runner (no crash, no startup abort) — V1's
    "will spawn lazily on first use" semantics, realised here as
    "fall back to one-shot per run".
    """
    if not hasattr(container, "app_builder"):
        return
    try:
        from qai.app_builder.infrastructure import (
            StickyWorkerHost,
            build_persistent_bootstrap_spec,
        )
        from qai.app_builder.infrastructure.app_manifest import (
            select_runner_interpreter,
        )
    except Exception:  # noqa: BLE001 — import guard for non-app_builder builds
        _log.warning("lifespan.sticky_worker_import_failed", exc_info=True)
        return

    try:
        repo_root = getattr(container, "repo_root", None)
        if repo_root is None:
            return

        # Resolve the same ARM64 venv interpreter + QAIRT SDK extras the
        # one-shot Pack runner uses (so the resident worker loads the QNN
        # runtime DLLs identically). ``select_runner_interpreter`` returns
        # a ``SysExecutableResolver`` when no ``qairt_env.json`` is present
        # (dev / non-NPU) — the spawn still works against sys.executable,
        # and a non-NPU runner simply fails to load the model later
        # (surfaced as a normal error, fallback handles it).
        interpreter = select_runner_interpreter(
            qairt_env_file=getattr(container, "qairt_env_file", None),
            repo_root=repo_root,
        )
        python_exe = interpreter.resolve()

        # Merge the QAIRT SDK env + PATH extras the same way the one-shot
        # resolver's ``_materialise_env`` does, so the worker subprocess
        # finds ``QAIRT_ROOT`` / the QNN DLLs on ``PATH``.
        import os as _os

        base_env = dict(_os.environ)
        extra_env_fn = getattr(interpreter, "extra_env", None)
        if callable(extra_env_fn):
            for _k, _v in extra_env_fn().items():
                base_env[str(_k)] = str(_v)
        path_segments_fn = getattr(interpreter, "path_segments", None)
        if callable(path_segments_fn):
            segments = path_segments_fn()
            if segments:
                prefix = _os.pathsep.join(str(s) for s in segments)
                existing = base_env.get("PATH", "")
                base_env["PATH"] = (
                    prefix + (_os.pathsep + existing if existing else "")
                )

        # 缺口 10: inject the live global proxy at spawn time so the resident
        # sticky-worker's first model download (``load_model`` ->
        # ``_ensure_weights_downloaded``) routes through the proxy. The sticky
        # worker is long-lived; a proxy change applied at runtime takes effect
        # on next restart (V1 parity — service-level proxy). One-shot spawns
        # read the live proxy at each spawn via the command_resolver.
        try:
            from ._global_proxy import build_global_proxy_provider as _bgp

            _proxy_url = _bgp(container)()
            if _proxy_url:
                for _pkey in (
                    "HTTPS_PROXY", "https_proxy",
                    "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy",
                ):
                    base_env[_pkey] = _proxy_url
        except Exception:  # noqa: BLE001 — proxy must never block spawn
            _log.debug(
                "lifespan.sticky_worker_proxy_injection_failed", exc_info=True
            )

        shared_dir = getattr(container, "app_builder_shared_dir", None)
        if shared_dir is None:
            candidate = Path(repo_root).joinpath(
                "factory", "app_builder", "shared"
            )
            if candidate.is_dir():
                shared_dir = candidate

        spec = build_persistent_bootstrap_spec(
            python_exe=Path(python_exe),
            shared_dir=shared_dir,
            base_env=base_env,
            trust_token=_resolve_native_guard_trust_token(container),
        )
        host = StickyWorkerHost(
            bootstrap=spec,
            event_bus=getattr(container, "events", None),
        )
        await host.spawn()
        container.sticky_worker_host = host  # type: ignore[attr-defined]
        _log.info("lifespan.sticky_worker_spawned", state=host.state)
    except Exception:  # noqa: BLE001 — spawn failure must never abort startup
        _log.warning(
            "lifespan.sticky_worker_spawn_failed "
            "(runs fall back to one-shot)",
            exc_info=True,
        )


#: H-1 — strong refs to fire-and-forget channel auto-start tasks so the
#: event loop does not garbage-collect them mid-flight (asyncio only keeps
#: weak refs to tasks). Cleared as each task completes.
_CHANNEL_AUTOSTART_TASKS: set = set()

#: RE-OC-2 — strong refs to the fire-and-forget OpenCode service
#: auto-start task (same GC-safety rationale as the channel set above).
_OC_AUTOSTART_TASKS: set = set()


async def _reset_stale_channel_status(container: Container) -> None:
    """Reset persisted active channel status to ``stopped`` on boot (铁律1).

    On a process restart the in-memory transports (Feishu WS client / WeChat
    long-poll bot) no longer exist, so any ``running`` / ``starting`` /
    ``stopping`` value persisted in ``channels_instance.status`` is stale and
    untrue.  Walking every kind, we force those active states back to
    ``stopped`` (via the domain's process-boundary
    :meth:`ChannelInstance.reset_to_stopped`) and persist the corrected truth
    so:

    * the WebUI no longer shows a dead channel as "已连接" (it keys off
      ``instance.status``), and
    * channel auto-start (run right after this) sees ``stopped`` and actually
      reconnects ``auto_start=true`` instances (it skips RUNNING/STARTING).

    ``error`` instances are left untouched — they require an explicit user
    acknowledge before leaving the error state (mirrors the auto-start ERROR
    skip), so a boot reset must not silently clear an unseen error.

    Best-effort per instance: one bad read / save must not abort startup.
    """
    from qai.channels.domain import ChannelKind
    from qai.channels.domain.value_objects import ChannelStatus

    channels = getattr(container, "channels", None)
    repo = getattr(channels, "instance_repository", None)
    if repo is None:
        return
    clock = getattr(container, "clock", None)

    def _now():  # type: ignore[no-untyped-def]
        if clock is not None:
            return clock.now()
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    for kind in ChannelKind:
        try:
            instances = await repo.list_by_kind(kind)
        except Exception:  # noqa: BLE001 — repo read is best-effort
            _log.warning(
                "lifespan.channel_status_reset_list_failed",
                kind=getattr(kind, "value", str(kind)),
                exc_info=True,
            )
            continue
        for instance in instances:
            # Only the active (non-terminal, non-error) states are stale after
            # a restart; stopped / error are left as-is by reset_to_stopped.
            if instance.status not in (
                ChannelStatus.RUNNING,
                ChannelStatus.STARTING,
                ChannelStatus.STOPPING,
            ):
                continue
            previous = instance.status
            try:
                now = _now()
                reset = instance.reset_to_stopped(now=now)
                await repo.save(reset)
                _log.info(
                    "lifespan.channel_status_reset_stale",
                    kind=getattr(kind, "value", str(kind)),
                    instance_id=getattr(
                        instance.instance_id, "value", "?"
                    ),
                    **{"from": getattr(previous, "value", str(previous))},
                )
            except Exception:  # noqa: BLE001 — one bad instance must not
                # block the others / abort startup.
                _log.warning(
                    "lifespan.channel_status_reset_failed",
                    kind=getattr(kind, "value", str(kind)),
                    instance_id=getattr(
                        instance.instance_id, "value", "?"
                    ),
                    exc_info=True,
                )


def _spawn_channel_auto_start(container: Container) -> None:
    """Fire-and-forget auto-connect of ``auto_start`` channel instances.

    H-1 — V1 (``backend/main.py:516-525`` / ``575-593``) auto-connected
    WeChat / Feishu on service start when their ``auto_connect`` /
    ``feishu_auto_start`` flag was set. V2 persisted the per-instance
    ``auto_start`` flag (and defaults WeChat to True) but had no boot-time
    consumer, so a service / machine restart left channels silently
    disconnected. This restores that behaviour.

    Fire-and-forget (``asyncio.create_task``, V1 parity): a slow or failing
    connect must never block or abort startup. The
    ``StartChannelInstanceUseCase`` marks the instance ERROR on failure
    (e.g. Feishu without app_secret) instead of raising into the loop.
    """
    try:
        channels = getattr(container, "channels", None)
        repo = getattr(channels, "instance_repository", None)
        start_uc = getattr(
            channels, "start_channel_instance_use_case", None
        )
        if repo is None or start_uc is None:
            return
        task = asyncio.create_task(
            _channel_auto_start_task(repo, start_uc),
            name="channels-auto-start",
        )
        _CHANNEL_AUTOSTART_TASKS.add(task)
        task.add_done_callback(_CHANNEL_AUTOSTART_TASKS.discard)
        _log.info("lifespan.channel_auto_start_spawned")
    except Exception:  # noqa: BLE001 — auto-start must never abort startup
        _log.warning(
            "lifespan.channel_auto_start_spawn_failed", exc_info=True
        )


async def _channel_auto_start_task(repo, start_uc) -> None:  # noqa: ANN001
    """Walk every channel kind, start instances whose ``auto_start`` is set.

    Best-effort per instance: a failure to start one instance (bad creds,
    transient SDK error) is logged and the loop continues to the next.
    """
    from qai.channels.domain import ChannelKind
    from qai.channels.domain.value_objects import ChannelStatus

    for kind in ChannelKind:
        try:
            instances = await repo.list_by_kind(kind)
        except Exception:  # noqa: BLE001 — repo read is best-effort
            _log.warning(
                "lifespan.channel_auto_start_list_failed",
                kind=getattr(kind, "value", str(kind)),
                exc_info=True,
            )
            continue
        for instance in instances:
            try:
                settings = instance.get_settings()
            except Exception:  # noqa: BLE001
                continue
            if not bool(getattr(settings, "auto_start", False)):
                continue
            # Skip instances already running/starting (idempotent boot).
            if instance.status in (
                ChannelStatus.RUNNING,
                ChannelStatus.STARTING,
            ):
                continue
            # Skip instances parked in ERROR: the domain requires an explicit
            # acknowledge before a restart (``instance.request_start`` raises
            # ``ChannelInstanceStateError`` for ERROR — by design, so a known-
            # broken channel is not auto-restarted in a loop). This is an
            # expected boot-time state, not a failure, so emit a calm info
            # line instead of a scary traceback (the previous behaviour logged
            # a full ``exc_info`` warning that looked like a crash).
            if instance.status is ChannelStatus.ERROR:
                _log.info(
                    "lifespan.channel_auto_start_skipped_error_state",
                    kind=getattr(kind, "value", str(kind)),
                    instance_id=getattr(instance.instance_id, "value", "?"),
                    hint="acknowledge the error before this channel auto-starts",
                )
                continue
            try:
                await start_uc.execute(instance.instance_id)
                _log.info(
                    "lifespan.channel_auto_started",
                    kind=getattr(kind, "value", str(kind)),
                    instance_id=instance.instance_id.value,
                )
            except Exception as exc:  # noqa: BLE001 — one bad instance must
                # not stop the others; the use case already marked it ERROR.
                # Emit a concise, user-friendly reason rather than a full
                # traceback (auto-start failures — bad creds, transient SDK
                # errors — are expected and recoverable from the UI). The
                # stack is kept at DEBUG for opt-in diagnosis only.
                reason = str(exc).strip() or type(exc).__name__
                _log.warning(
                    "lifespan.channel_auto_start_failed",
                    kind=getattr(kind, "value", str(kind)),
                    instance_id=getattr(
                        instance.instance_id, "value", "?"
                    ),
                    reason=reason,
                    hint=(
                        "channel was not auto-started; start it manually "
                        "from the Channels page (the instance is marked ERROR)"
                    ),
                )
                _log.debug(
                    "lifespan.channel_auto_start_failed.detail",
                    exc_info=True,
                )


def _spawn_oc_service_auto_start(container: Container) -> None:
    """Fire-and-forget auto-start of the OpenCode service (RE-OC-2).

    V1 (``backend/main.py:443-467``) auto-started the OpenCode subprocess
    on boot when the OC config had ``enabled=true`` AND ``auto_start=true``.
    V2 persisted both flags (config whitelist) but had no boot-time
    consumer, so a service / machine restart left OpenCode down even when
    the user opted into auto-start. This restores that behaviour.

    Fire-and-forget (``asyncio.create_task``, V1 parity): a slow or failing
    spawn must never block or abort startup. The ``StartOcServiceUseCase``
    surfaces ``ValidationError`` (cli_path unconfigured / binary missing)
    rather than raising into the loop; we log and move on.
    """
    try:
        ai_coding = getattr(container, "ai_coding", None)
        start_uc = getattr(ai_coding, "start_oc_service_use_case", None)
        get_config_uc = getattr(
            ai_coding, "get_oc_coding_config_use_case", None
        )
        if start_uc is None or get_config_uc is None:
            return
        task = asyncio.create_task(
            _oc_service_auto_start_task(start_uc, get_config_uc),
            name="oc-service-auto-start",
        )
        _OC_AUTOSTART_TASKS.add(task)
        task.add_done_callback(_OC_AUTOSTART_TASKS.discard)
        _log.info("lifespan.oc_service_auto_start_spawned")
    except Exception:  # noqa: BLE001 — auto-start must never abort startup
        _log.warning(
            "lifespan.oc_service_auto_start_spawn_failed", exc_info=True
        )


async def _oc_service_auto_start_task(start_uc, get_config_uc) -> None:  # noqa: ANN001
    """Start the OpenCode service iff ``enabled`` AND ``auto_start`` are set.

    Best-effort: reads the OC config doc, gates on the two flags (V1
    ``main.py:444``), then invokes the start use case. Any failure is
    logged calmly (cli_path unconfigured / binary missing are expected,
    recoverable from the Settings UI) without a scary traceback.
    """
    try:
        from qai.ai_coding.application.use_cases.manage_coding_config import (
            GetCodingConfigQuery,
        )

        doc = await get_config_uc.execute(GetCodingConfigQuery())
        if not isinstance(doc, dict):
            return
        if not (bool(doc.get("enabled", False)) and bool(
            doc.get("auto_start", False)
        )):
            return
    except Exception:  # noqa: BLE001 — config read is best-effort
        _log.warning(
            "lifespan.oc_service_auto_start_config_failed", exc_info=True
        )
        return

    try:
        status = await start_uc.execute()
        _log.info(
            "lifespan.oc_service_auto_started",
            running=getattr(status, "running", None),
            pid=getattr(status, "pid", None),
            port=getattr(status, "port", None),
        )
    except Exception as exc:  # noqa: BLE001 — expected (unconfigured / missing)
        reason = str(exc).strip() or type(exc).__name__
        _log.warning(
            "lifespan.oc_service_auto_start_failed",
            reason=reason,
            hint=(
                "OpenCode was not auto-started; configure cli_path and "
                "start it from Settings → AI Coding"
            ),
        )
        _log.debug(
            "lifespan.oc_service_auto_start_failed.detail", exc_info=True
        )


def _spawn_voice_warmup(container: Container) -> None:
    """Fire-and-forget the voice ASR warm-load on startup.

    9-L4 — V1 (``backend/main.py:644-708``) detects the ``voice_input``
    feature on startup and fires an ``asyncio.create_task`` to warm-load the
    ASR runtime (whisper-base / zipformer-zh) onto the NPU so the user's
    first microphone tap is not delayed by a multi-second cold load. We
    mirror that: fire-and-forget a warm-up task (held by the R-3
    ``TaskRegistry`` so it has a strong ref and is cancelled on shutdown)
    that (1) reads the voice preference via ``PreloadVoiceInputUseCase``,
    then (2) — when a resident sticky worker is wired — really loads the
    preferred model into it via ``host.load_model`` under the NPU lock
    (V1 ``main.py:685`` ``async with _npu_lock``). When voice-input is
    disabled, no preference is set, or no host is running, the warm-up is a
    no-op (``skipped``). The heavy ``qai_appbuilder`` / NPU import happens
    in the worker subprocess, so a host without the SDK / NPU degrades
    gracefully (no crash, no eager import here).
    """
    try:
        _ab = getattr(container, "app_builder", None)
        _preload_uc = (
            getattr(_ab, "preload_voice_input_use_case", None) if _ab else None
        )
        _tasks = getattr(_ab, "background_tasks", None) if _ab else None
        if _preload_uc is not None and _tasks is not None:
            _tasks.spawn(
                _voice_warmup_task(container, _preload_uc),
                name="voice-input-warmup",
            )
            _log.info("lifespan.voice_warmup_spawned")
    except Exception:  # noqa: BLE001 — warm-up must never abort startup
        _log.warning("lifespan.voice_warmup_spawn_failed", exc_info=True)


async def _voice_warmup_task(container: Container, preload_uc) -> None:  # noqa: ANN001
    """Read the voice preference, then really load it into the worker.

    Best-effort throughout: any failure (no preference, no host, no runner
    spec, load timeout, non-NPU box) is logged at warning/info and
    swallowed — the model loads on demand on the user's first mic tap.
    """
    try:
        result = await preload_uc.execute()
    except Exception:  # noqa: BLE001
        _log.warning("lifespan.voice_warmup_pref_read_failed", exc_info=True)
        return

    status = getattr(result, "status", "skipped")
    model_id = getattr(result, "model_id", None)
    variant_id = getattr(result, "variant_id", None)
    if status == "skipped" or not model_id:
        _log.info("lifespan.voice_warmup_skipped", status=status)
        return
    if status == "cached":
        # Already resident — nothing to do.
        _log.info("lifespan.voice_warmup_cached", model_id=model_id)
        return

    # Guard: qai_appbuilder SDK is required to run Pack runners (the runner
    # subprocess imports it for NPU inference).  On Linux / non-NPU boxes the
    # SDK is not installed; skip the warm-load to avoid a confusing
    # RuntimeError from the worker subprocess ("failed to import runner
    # module").  The model will still load on demand on the user's first
    # microphone tap (one-shot runner path, which also falls back gracefully).
    import importlib.util as _ilu
    if _ilu.find_spec("qai_appbuilder") is None:
        _log.info(
            "lifespan.voice_warmup_skipped_no_sdk",
            model_id=model_id,
            reason="qai_appbuilder not installed",
        )
        return

    await warm_load_model_into_host(
        container, model_id=str(model_id), variant_id=variant_id
    )


async def warm_load_model_into_host(
    container: Container,
    *,
    model_id: str,
    variant_id: str | None,
) -> bool:
    """Really load ``model_id`` into the resident sticky worker (V1 parity).

    Shared by the startup warm-up task and the ``POST
    /voice-input/preload`` route so both trigger an *actual* resident load
    (V1 ``api_routes.py:1044`` preload calls ``get_or_create_worker``; V1
    ``main.py:680-706`` warm-up does the same). The previous V2 behaviour
    — ``PreloadVoiceInputUseCase`` optimistically returning ``"loaded"``
    without loading — left ``worker/status.loaded_models`` empty so the UI
    voice-engine dot never flipped from "loading" to "ready".

    Best-effort: returns ``True`` on a successful load, ``False`` (logged)
    when there is no host / no runner spec / load timeout / non-NPU box.
    Acquires the same ``_npu_lock`` real inference takes so a concurrent
    user request waits politely (V1 ``main.py:685``), bounded by the 120s
    budget V1 used.
    """
    host = getattr(container, "sticky_worker_host", None)
    if host is None or not host.alive:
        _log.info("lifespan.voice_warmup_no_host", model_id=model_id)
        return False

    try:
        from qai.app_builder.infrastructure import (
            build_load_request_for_model_id,
        )
        from qai.app_builder.infrastructure.process_runner import _npu_lock
    except Exception:  # noqa: BLE001
        _log.warning("lifespan.voice_warmup_import_failed", exc_info=True)
        return False

    _ab = getattr(container, "app_builder", None)
    registry = getattr(_ab, "runner_command_registry", None) if _ab else None
    repo_root = getattr(container, "repo_root", None)
    if registry is None or repo_root is None:
        _log.info("lifespan.voice_warmup_no_registry", model_id=model_id)
        return False

    # Fast-path: already resident → nothing to do (V1 ``will_reuse``).
    if host.is_loaded(model_id, variant_id):
        _log.info("lifespan.voice_warmup_cached", model_id=model_id)
        return True

    manifest_provider = getattr(
        container, "app_builder_manifest_provider", None
    )
    load_request = build_load_request_for_model_id(
        registry=registry,
        repo_root=Path(repo_root),
        model_id=str(model_id),
        variant_id=variant_id,
        manifest_provider=manifest_provider,
    )
    if load_request is None:
        _log.info(
            "lifespan.voice_warmup_no_runner_spec", model_id=model_id
        )
        return False

    try:
        # Acquire the same NPU lock real inference takes so a concurrent
        # user request waits politely (V1 ``main.py:685``). Bounded by the
        # same 120s budget V1 used.
        async with _npu_lock:
            await asyncio.wait_for(host.load_model(load_request), timeout=120.0)
        _log.info("lifespan.voice_warmup_loaded", model_id=model_id)
        return True
    except asyncio.TimeoutError:
        _log.warning("lifespan.voice_warmup_timeout", model_id=model_id)
        return False
    except Exception:  # noqa: BLE001
        _log.warning(
            "lifespan.voice_warmup_load_failed", model_id=model_id,
            exc_info=True,
        )
        return False


def _install_connection_reset_suppressor() -> None:
    """Install an asyncio exception handler that swallows WinError 10054.

    9-L1 — V1 ``backend/main.py:201-232`` parity. On Windows the
    ProactorEventLoop raises ``ConnectionResetError`` (WinError 10054)
    inside a callback when the remote client disconnects mid socket
    shutdown (typical after a long SSE stream). asyncio cannot propagate a
    callback exception, so it logs it as a benign-but-noisy [ERROR]. This
    handler discards ``ConnectionResetError`` and delegates everything else
    to the original (or built-in default) handler unchanged.

    Idempotent and best-effort: when there is no running loop yet (should
    not happen inside lifespan) it is a no-op. Caller guards with
    ``sys.platform == "win32"``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — no running loop
        return
    original_handler = loop.get_exception_handler()

    def _suppress_connection_reset(
        _loop: asyncio.AbstractEventLoop, context: dict
    ) -> None:
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            _log.debug(
                "lifespan.suppressed_connection_reset",
                message=context.get("message", ""),
            )
            return
        if original_handler is not None:
            original_handler(_loop, context)
        else:
            _loop.default_exception_handler(context)

    loop.set_exception_handler(_suppress_connection_reset)
    _log.info("lifespan.connection_reset_suppressor_installed")


# Phase 3 cleanup (2026-07-01) — ``_verify_sandbox_launcher_or_fail``
# and ``_cleanup_orphaned_sandbox_dirs`` were the fail-fast launcher
# probe and the per-call ``qai_sandbox_*`` temp-directory sweep tied to
# the deleted Windows AppContainer/LPAC launcher chain
# (``SandboxedProcessRunner`` / ``SandboxPolicyBuilder`` /
# ``DaemonManager`` / ``launcher_resolver``). Both have been removed.


async def _heartbeat_loop(
    *,
    host: str,
    port: int,
    interval: int = 30,
) -> None:
    """Emit a structured heartbeat log line every *interval* seconds.

    Runs as a background ``asyncio.Task`` for the full lifetime of the
    server process.  Cancelled cleanly by the lifespan ``finally`` block
    on shutdown so no ``CancelledError`` leaks to the event loop.
    """
    while True:
        await asyncio.sleep(interval)
        _log.info(
            "lifespan.heartbeat",
            host=host,
            port=port,
            interval_seconds=interval,
        )
