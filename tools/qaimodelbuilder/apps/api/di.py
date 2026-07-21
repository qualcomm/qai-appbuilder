# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Dependency container for the API entry point.

Wiring is explicit — no global registry, no service locator. Each
component is built once per ``Container`` instance, which lives for the
duration of one application (one ``Settings``).

Tests build their own ``Container`` with whatever fakes they need.

Bounded-context namespaces
--------------------------
S3 introduces per-context service namespaces on the container so each
context PR (PR-030..036) can attach its own use cases without
exploding the top-level ``Container`` field list:

* ``container.system`` — :class:`SystemServices` (PR-030)
* ``container.security`` — :class:`SecurityServices` (PR-031,
  see :mod:`apps.api._security_di`)
* ``container.model_catalog`` — :class:`ModelCatalogServices` (PR-032
  shape; PR-044 retired the fakes — see
  :mod:`apps.api._model_catalog_di`)
* ``container.chat`` — :class:`ChatServices` (PR-033,
  see :mod:`apps.api._chat_di`)
* ``container.app_builder`` — :class:`AppBuilderServices` (PR-034,
  see :mod:`apps.api._app_builder_di`)
* ``container.ai_coding`` — :class:`AiCodingServices` (PR-035,
  see :mod:`apps.api._ai_coding_di`)
* ``container.channels`` — :class:`ChannelsServices` (PR-036,
  see :mod:`apps.api._channels_di`)

Each namespace is built by a private ``_build_<context>_services(...)``
function that injects production adapters (aiosqlite-backed
repositories, real HTTP clients, etc.) into the use cases. The
historical ``_Fake<Port>`` in-memory adapter pattern that previously
lived in this module has been retired across all bounded contexts:
PR-040 removed the security + system fakes, PR-044 removed the eight
model_catalog fakes, and PR-042/043/045/046/047 cleared the
chat / app_builder / ai_coding / channels / shared fakes. Tests that
need in-memory stubs construct them inside the test module itself
(see ``tests/fixtures/in_memory/``); ``apps/api/`` is fakes-free.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.config import DataPaths, Settings
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator, UlidGenerator
from qai.platform.persistence import Database
from qai.platform.persistence.secrets import SecretStore, build_secret_store
from qai.platform.process import prepend_bundled_paths_to_process
from qai.platform.time import Clock, SystemClock

from ._ai_coding_di import AiCodingServices, build_ai_coding_services
from ._app_builder_di import AppBuilderServices, build_app_builder_services
from ._app_builder_model_builder_bridge import (
    AppBuilderModelBuilderBridge,
    build_auto_export_bridge,
)
from ._background_process_di import (
    BackgroundProcessServices,
    build_background_process_services,
    wire_background_process_tool_into_chat,
)
from ._channels_di import ChannelsServices, build_channels_services
from ._chat_di import (
    ChatServices,
    build_chat_services,
    wire_ai_coding_tools_into_chat,
    wire_appbuilder_tools_into_chat,
    wire_web_search_tool_into_chat,
)
from ._dependency_approval_di import DependencyApprovalServices, build_dependency_approval_services
from ._command_policy_di import CommandPolicyServices, build_command_policy_services
from ._mb_pro_session_bridge import build_mb_pro_session_controller
from ._gomaster_session_bridge import build_gomaster_session_controller
from ._gomaster_service_bridge import build_gomaster_service_controller
from ._gomaster_external_optimize_bridge import (
    build_gomaster_external_optimize_controller,
)
from ._model_builder_di import (
    ModelBuilderServices,
    build_model_builder_services,
)
from ._model_catalog_di import (
    ModelCatalogServices,
    build_model_catalog_services,
)
from ._model_runtime_di import ModelRuntimeServices, build_model_runtime_services
from ._reboot_scheduler import (
    SystemRebootSignalAdapter,
    _RebootScheduler,
)
from ._security_di import SecurityServices, build_security_services
from ._service_release_di import (
    ServiceReleaseServices,
    build_service_release_services,
)
from ._uploads_di import build_upload_store
from ._user_prefs_di import UserPrefsServices, build_user_prefs_services
from .system_ports import RebootSignalPort

# ---------------------------------------------------------------------------
# system namespace (PR-030)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SystemServices:
    """Application services / ports for the ``system`` namespace.

    Only one collaborator for now (reboot signal); kept as a dataclass
    namespace so future fields slot in without churning route imports.
    """

    reboot_signal: RebootSignalPort


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from qai.app_builder.infrastructure import StickyWorkerHost


@dataclass(slots=True)
class Container:
    """Wired-up application graph.

    Build via ``Container.build(settings, repo_root)`` from ``lifespan.py``.
    Tests can construct one directly with hand-rolled fakes.
    """

    settings: Settings
    data_paths: DataPaths
    database: Database
    secret_store: SecretStore
    events: EventBus
    repo_root: Path
    clock: Clock = field(init=False)
    ids: IdGenerator = field(init=False)
    # SEC — per-process boot id, minted once when the container is built
    # (``_wire_platform``). Used as the ``scope_key`` for ``process``-scoped
    # PathGrants: a restart builds a new container → new boot id → old
    # process grants stop matching (真 process 隔离). Stable for the process
    # lifetime.
    boot_id: str = field(init=False, default="")
    reboot_scheduler: _RebootScheduler = field(init=False)
    system: SystemServices = field(init=False)
    model_catalog: ModelCatalogServices = field(init=False)
    service_release: ServiceReleaseServices = field(init=False)
    chat: ChatServices = field(init=False)
    app_builder: AppBuilderServices = field(init=False)
    security: SecurityServices = field(init=False)
    ai_coding: AiCodingServices = field(init=False)
    channels: ChannelsServices = field(init=False)
    user_prefs: UserPrefsServices = field(init=False)
    dependency_approval: DependencyApprovalServices = field(init=False)
    command_policy: CommandPolicyServices = field(init=False)
    model_runtime: ModelRuntimeServices = field(init=False)
    # Tail-appended (S9 close): ``model_builder`` context wires the
    # ModelBuilder -> AppBuilder Pack export pipeline; the
    # ``auto_export_bridge`` is the cross-context join consumed by
    # ``POST /api/app-builder/import/auto-export`` so app_builder
    # routes never import ``qai.model_builder``.
    model_builder: ModelBuilderServices = field(init=False)
    auto_export_bridge: AppBuilderModelBuilderBridge = field(init=False)
    # MB Pro (Model Builder Pro) session-lifecycle controller, or ``None`` on
    # external editions. Typed ``object | None`` (not the concrete
    # ``MbProSessionController``) so the container declaration carries no
    # import of the internal-only ``_mb_pro_session_bridge`` type at the field
    # level; the routes consume it by duck-typing. Built in
    # ``_wire_cross_context``.
    mb_pro_session: object | None = field(init=False, default=None)
    # GoMaster online-integration session-lifecycle controller, or ``None`` on
    # external editions. Same ``object | None`` duck-typed treatment as
    # ``mb_pro_session`` (no field-level import of the internal-only
    # ``_gomaster_session_bridge`` type). Built in ``_wire_cross_context``.
    gomaster_session: object | None = field(init=False, default=None)
    # GoMaster online-integration REST/stream capability-proxy adapter, or
    # ``None`` on external editions. Same duck-typed ``object | None`` treatment
    # (no field-level import of the internal-only adapter type). Backs the
    # ``/api/gomaster/*`` proxy routes. Built in ``_wire_cross_context``.
    gomaster_service: object | None = field(init=False, default=None)
    # GoMaster External Auto-Optimize job adapter (the ``external`` link), or
    # ``None`` when gomaster_mode excludes "external" / on external editions.
    # Backs the ``/api/gomaster/optimize/*`` proxy routes. Built in
    # ``_wire_cross_context``.
    gomaster_external_optimize: object | None = field(init=False, default=None)
    # Tail-appended (batch A — QAIRT runtime hand-off): optional path
    # to ``qairt_env.json``. When set, the App Builder DI uses
    # :class:`qai.app_builder.infrastructure.command_resolver.QairtEnvJsonResolver`
    # to pick the venv interpreter AND inject ``QAIRT_ROOT`` /
    # ``QNN_SDK_ROOT`` + the SDK ``bin``+``lib`` directories on
    # ``PATH`` so spawned Pack subprocesses can ``import qai_appbuilder``
    # without ``QAI_APPBUILDER_UNAVAILABLE``. Lifespan resolves the
    # canonical path under ``data/config/qairt_env.json``; tests may
    # override before calling :meth:`build`.
    qairt_env_file: Path | None = field(init=False, default=None)

    # Tail-appended (PR-302 wiring): the live persistent App Builder
    # sticky worker host, spawned by ``lifespan._spawn_sticky_worker``
    # AFTER the container (and its ``StickyBackedAppRunner``) are built.
    # ``None`` until/unless the spawn succeeds; the runner + worker-status
    # adapter read it lazily via ``getattr(container, "sticky_worker_host",
    # None)`` so they pick up the post-build value (State-Truth-First).
    sticky_worker_host: "StickyWorkerHost | None" = field(
        init=False, default=None
    )

    # Tail-appended (background_process platform module): the long-running
    # background subprocess manager + its Win32 Job Object kill group. Built
    # in ``_build_contexts`` after the events bus is live (the manager
    # publishes BackgroundProcessUpdated/Deleted onto ``container.events``).
    # Consumed by the ``/api/background_process`` routes and by the
    # ``background_process`` LLM tool (registered into the chat tool
    # registry via ``wire_background_process_tool_into_chat`` in phase 2
    # right after this services namespace is wired). New field —
    # AGENTS.md §3.1 permits tail-append.
    background_process: BackgroundProcessServices = field(init=False)

    @classmethod
    def build(cls, *, settings: Settings, repo_root: Path) -> "Container":
        data_paths = settings.data_paths()
        data_paths.ensure_top_levels()

        # Prefer application-owned tools over whatever is first on the host
        # PATH. Prepend our bundled binary dirs (``%LOCALAPPDATA%\QAIModelBuilder``
        # + ``<data_root>/bin``, as declared in
        # ``factory/config/exec_path_dirs.json``) to THIS process's PATH once, at
        # the single shared composition root (used by both the API server and the
        # CLI). Every child process spawned later inherits ``os.environ``, so this
        # one mutation makes bare-name commands (``uvx`` / ``npx`` / …) resolve to
        # our copy first — the generic fix, not a per-spawn-site patch.
        prepend_bundled_paths_to_process(data_paths.root, repo_root=repo_root)

        database = Database(path=data_paths.db_path())
        secret_store = build_secret_store(data_paths=data_paths, prefer="auto")
        events = EventBus()
        c = cls(
            settings=settings,
            data_paths=data_paths,
            database=database,
            secret_store=secret_store,
            events=events,
            repo_root=repo_root,
        )
        # Three explicit phases (architecture cleanup — byte-for-byte the
        # same wiring/order as the former single body, just grouped so the
        # cross-context dependency edge — chat → ai_coding → wire — reads
        # explicitly):
        #   1. core platform singletons (clock / ids / qairt / reboot / system),
        #   2. all 13 bounded-context namespaces (in dependency order),
        #   3. cross-context joins (model_builder export bridge).
        c._build_core(settings=settings, data_paths=data_paths, repo_root=repo_root)
        c._build_contexts()
        c._wire_cross_context()
        return c

    def _build_core(
        self,
        *,
        settings: Settings,
        data_paths: DataPaths,
        repo_root: Path,
    ) -> None:
        """Phase 1 — platform singletons that every context depends on."""
        self.clock = SystemClock()
        self.ids = UlidGenerator()
        # SEC — mint the per-process boot id once (process-scoped grant key).
        self.boot_id = self.ids.new_id()
        # Resolve canonical ``qairt_env.json`` location (batch A —
        # QAIRT runtime hand-off). Set BEFORE the app_builder
        # namespace is built so the command resolver picks up
        # ``QAIRT_ROOT`` / ``QNN_SDK_ROOT`` and the SDK PATH segments
        # on every Pack subprocess spawn. Resolution order:
        #   1. data/config/qairt_env.json   (canonical v2 location)
        #   2. config/qairt_env.json        (legacy compat — still
        #      shipped with some dev checkouts; release artifacts
        #      strip ``config/``).
        # ``None`` when neither file is present, in which case the
        # interpreter falls back to ``sys.executable`` and the spawn
        # env carries no QAIRT extras (parity with PR-303 default).
        self.qairt_env_file = _resolve_qairt_env_file(data_paths, repo_root)
        # Single reboot scheduler shared across system + security
        # adapters so concurrent reboot requests coalesce into one
        # exit task. Exit code is taken from settings (PR-040).
        self.reboot_scheduler = _RebootScheduler(
            exit_code=settings.server.reboot_exit_code,
        )
        self.system = _build_system_services(self)

    def _build_contexts(self) -> None:
        """Phase 2 — the 13 bounded-context namespaces, in dependency order.

        Order is load-bearing: ``chat`` is built before ``ai_coding`` so the
        chat tool registry exists, then :func:`wire_ai_coding_tools_into_chat`
        back-fills the ai_coding production tools onto that registry NOW that
        ai_coding is wired (the registration inside ``_chat_di.py`` was a
        no-op because ai_coding was still ``None`` at chat-build time).
        ``channels`` is built after the wire so its dispatch bridge sees the
        fully-wired chat + ai_coding surfaces.
        """
        self.model_catalog = build_model_catalog_services(self)
        self.service_release = build_service_release_services(self)
        self.chat = build_chat_services(self)
        self.app_builder = build_app_builder_services(self)
        self.security = build_security_services(self)
        # dep_broker + exec_broker are standalone (only need settings/events)
        # and MUST be built before ai_coding: ``build_ai_coding_services``
        # eagerly builds the FileBroker/FileGuard bridges, which read
        # ``container.dependency_approval.broker`` / ``container.command_policy.broker`` to
        # wire the exec-approval / profile gates. Building them after ai_coding
        # left those gates unwired (the bridges saw ``None``).
        self.dependency_approval = build_dependency_approval_services(self)
        self.command_policy = build_command_policy_services(self)
        self.ai_coding = build_ai_coding_services(self)
        # PR-fix-cloud-tools-di (2026-06-05): post-build hook — register the
        # ai_coding 9 production tools (with their OpenAI function-calling
        # schemas) onto the chat tool registry NOW that ai_coding is wired.
        # ``build_chat_services`` runs before ``build_ai_coding_services`` so
        # the registration inside ``_chat_di.py`` was a no-op (ai_coding was
        # None at that point).  Without this hook ``schemas()`` returns an
        # empty tuple and cloud LLMs never receive ``payload["tools"]``.
        wire_ai_coding_tools_into_chat(chat=self.chat, ai_coding=self.ai_coding)
        # appbuilder-tools fix (audit D6 §6.11 CRITICAL / §6.12 HIGH): wire the
        # REAL App Builder LLM Agent Pipeline tools into chat now that BOTH
        # ``app_builder`` (run_app_use_case) AND ``ai_coding`` (file_guard) are
        # wired. This OVERWRITES the ai_coding ``appbuilder_run`` stub with a
        # handler backed by ``RunAppUseCase`` and ADDS ``appbuilder_batch_run``
        # (V1 parity). The bridge lives in apps/api so chat never imports
        # qai.app_builder (context-isolation). Done AFTER the ai_coding wire so
        # the same FileGuardPort gates inputs paths exactly like ``read``.
        wire_appbuilder_tools_into_chat(
            chat=self.chat,
            app_builder=self.app_builder,
            ai_coding=self.ai_coding,
        )
        # web_search (internal-only): wire the conditional web_search tool into
        # chat now that ai_coding (file_guard) is wired. The bridge builds the
        # pluggable SearchProviderRegistry (CEBot) ONLY when
        # ``settings.is_internal`` — on external editions it is a no-op and the
        # tool never exists (four-layer edition defence). Done after the
        # ai_coding wire so it can share the same FileGuardPort for signature
        # parity. The bridge lives in apps/api so chat never imports
        # qai.platform.edition.web_search (context-isolation / no platform→chat
        # inversion).
        wire_web_search_tool_into_chat(
            chat=self.chat,
            container=self,
            ai_coding=self.ai_coding,
        )
        # WIRE-tools (V1 backend/tools/_exec.py:1010 parity): give the chat
        # streaming use case live exec stdout/stderr by wrapping its tool
        # port with an apps-layer bridge that exposes ``invoke_streaming``
        # routed through ``qai.tools.infrastructure.stream_exec``.  Done
        # here (not inside ``_chat_di``) so the bridge stays a cross-context
        # join at the composition root — the chat context never imports
        # ``qai.tools`` / ``qai.ai_coding`` directly (context-isolation).
        _wire_streaming_exec_into_chat(chat=self.chat, container=self)
        self.channels = build_channels_services(self)
        self.user_prefs = build_user_prefs_services(self)
        self.model_runtime = build_model_runtime_services(self)
        # background_process platform module — wired last in phase 2 so it
        # sees the fully-built events bus (publishes update/delete envelopes)
        # and the canonical ``data_paths.root``.  Nothing in phase 2 depends
        # on it, so its position at the tail is the safe slot.
        self.background_process = build_background_process_services(self)
        # PR-bgp-llm-tool (2026-07-01): post-build hook — register the
        # ``background_process`` LLM tool on the chat tool registry NOW that
        # both ``chat`` and ``background_process`` are wired. Without this
        # the schema never reaches ``payload["tools"]`` and an LLM-emitted
        # ``background_process`` call falls through to
        # ``chat.tool_not_registered``. The handler closes over the manager
        # so the registered callable speaks to the live subprocess manager
        # (not a stale snapshot); session isolation is enforced by passing
        # ``ConversationId.value`` as the handler's ``session_id``.
        wire_background_process_tool_into_chat(chat=self.chat, container=self)

    def _wire_cross_context(self) -> None:
        """Phase 3 — cross-context joins built after every namespace.

        Tail-appended (S9 close): ``model_builder`` context + bridge.
        The bridge is built last so its constructor sees the live
        ``c.model_builder.export_pack_use_case`` reference.
        """
        self.model_builder = build_model_builder_services(self)
        self.auto_export_bridge = build_auto_export_bridge(container=self)
        # MB Pro (Model Builder Pro) session-lifecycle controller — composed at
        # the apps root from edition config + the chat-infrastructure
        # per-conversation SessionManager registry, injected onto the container
        # so the ``interfaces.http.routes.mb_pro_session`` routes drive connect /
        # disconnect / version without importing infrastructure
        # (interfaces-stays-thin). ``None`` on external editions (is_internal
        # gate inside the bridge); the routes then short-circuit to 404.
        self.mb_pro_session = build_mb_pro_session_controller(container=self)
        # GoMaster online-integration session-lifecycle controller — composed
        # the same way as ``mb_pro_session`` (edition config + the chat-infra
        # per-tab GoMaster session registry), injected so the
        # ``interfaces.http.routes.gomaster_session`` routes drive connect /
        # disconnect / state / native-url without importing infrastructure.
        # ``None`` on external editions (is_internal gate inside the bridge);
        # the routes then short-circuit to 404 (and never disclose the intranet
        # native-url).
        self.gomaster_session = build_gomaster_session_controller(container=self)
        # GoMaster online-integration REST/stream capability-proxy adapter —
        # composed at the apps root from edition config + the model_builder
        # infrastructure adapter, injected so the ``interfaces.http.routes.
        # gomaster`` proxy routes relay auto-optimize / qnn-run-stream / graph /
        # benchmark / artifacts without importing infrastructure. ``None`` on
        # external editions (is_internal gate inside the bridge); the routes
        # then short-circuit to 404.
        self.gomaster_service = build_gomaster_service_controller(container=self)
        # GoMaster External Auto-Optimize job adapter — the ``external`` link
        # (config gomaster_mode in external/both). Injected so the
        # ``interfaces.http.routes.gomaster_optimize`` proxy routes relay
        # upload/poll/download/cancel. ``None`` when mode excludes external or
        # on external editions (is_internal gate) → routes 404.
        self.gomaster_external_optimize = build_gomaster_external_optimize_controller(
            container=self
        )


# ---------------------------------------------------------------------------
# system namespace wiring
# ---------------------------------------------------------------------------


def _resolve_qairt_env_file(
    data_paths: DataPaths, repo_root: Path
) -> Path | None:
    """Return the path to ``qairt_env.json`` if one exists, else ``None``.

    Resolution order (first hit wins):

    1. ``<data_dir>/config/qairt_env.json`` — canonical v2 location;
       written by the installer / by hand by the operator.
    2. ``<repo_root>/config/qairt_env.json`` — legacy V1 dev-checkout
       compat path. **Kept on purpose** even after the daemon /
       sandbox runtime moved off ``repo_root/config/`` (now under
       ``data/config/``): this fallback is for old development
       checkouts that still ship a ``config/qairt_env.json`` next to
       the source tree. The directory is allowed to be missing —
       both branches return ``None`` rather than raising — so a
       fresh checkout without ``config/`` is graceful.

    Both branches return ``None`` rather than raising when the file
    is absent, so a fresh install keeps booting with the
    ``sys.executable`` fallback (Pack subprocesses without QAIRT
    will surface ``QAI_APPBUILDER_UNAVAILABLE`` at import time, which
    becomes a normal RUN_FAILED frame — not a startup crash).
    """
    candidate = data_paths.root / "config" / "qairt_env.json"
    if candidate.is_file():
        return candidate
    legacy = repo_root / "config" / "qairt_env.json"
    if legacy.is_file():
        return legacy
    return None


def _build_system_services(container: Container) -> SystemServices:
    """Wire ``container.system``.

    PR-040 replaces the in-memory fake with a real
    :class:`SystemRebootSignalAdapter` delegating to the shared
    ``container.reboot_scheduler`` so reboot requests originating from
    either the system or security namespaces converge on a single
    debounce window and a single ``sys.exit(75)`` task.
    """
    return SystemServices(
        reboot_signal=SystemRebootSignalAdapter(
            scheduler=container.reboot_scheduler,
        ),
    )


# ---------------------------------------------------------------------------
# WIRE-tools: streaming exec bridge (cross-context join at the root)
# ---------------------------------------------------------------------------


class _StreamingToolInvocationBridge:
    """Wrap a chat :class:`ToolInvocationPort` to add ``invoke_streaming``.

    WIRE-tools (V1 ``backend/tools/_exec.py:1010`` + ``useChat.js:1041``
    parity).  The chat ``RegistryBackedToolInvocation`` adapter exposes a
    one-shot :meth:`invoke` that collapses an exec run into a single
    return value.  This apps-layer wrapper delegates every existing method
    (``invoke`` / ``schemas`` / ``register`` / ...) to the wrapped port
    verbatim and adds the optional :meth:`invoke_streaming` capability the
    chat use case probes for via ``getattr``.

    Only the ``exec`` tool streams; every other tool name returns ``None``
    so the use case drives the unchanged one-shot path.  Streaming is
    routed through :func:`qai.tools.infrastructure.tool_exec_stream.stream_exec`
    (the real-time stdout/stderr tee) — kept at the composition root so the
    chat context never imports ``qai.tools`` directly (context-isolation
    import-linter contract).
    """

    def __init__(
        self, inner: object, *, process_runner: object | None = None,
        session_workspace_resolver: object | None = None,
        tool_result_store: object | None = None,
        guard_token_provider: object | None = None,
        file_guard: object | None = None,
        ask_pending_probe: object | None = None,
        app_root: str | None = None,
        native_denial_probe: object | None = None,
        allow_x86: bool = False,
    ) -> None:
        self._inner = inner
        # When a ``ProcessRunnerPort`` is injected, the chat agentic loop's
        # streaming ``exec`` routes argv through it (parity with the one-shot
        # ai_coding path in ``_ai_coding_di.py``).  Post Phase 3 cleanup
        # (2026-07-01) this runner is the plain :class:`SubprocessProcessRunner`
        # — the AppContainer launcher wrap was removed — so the routed path is
        # functionally equivalent to the bare ``stream_exec`` path; the gate is
        # retained because the two branches differ in how stdout/stderr frames
        # are surfaced.  ``None`` keeps the legacy bare ``stream_exec`` path.
        self._process_runner = process_runner
        # Oversized-output store (V1 parity): the streaming exec's FINAL
        # consolidated output is routed through the SAME ``ToolResultStorePort``
        # the one-shot ai_coding exec uses, so a large stdout/stderr is
        # persisted to disk + the model sees a small head+tail preview with a
        # ``read(path=...)`` retrieval hint — instead of being destructively
        # truncated by the downstream model-aware ``ToolResultTruncatorPort``
        # (which would drop the middle with no way to recover it). ``None``
        # keeps the prior behaviour (no persistence) for callers without a store.
        self._tool_result_store = tool_result_store
        # Install/repo root (``${APP_ROOT}``) bound per-request so the ``read``
        # tool (reading a ``SKILL.md``) and the ``skill`` tool expand the
        # ``${APP_ROOT}`` placeholder to the real absolute path. ``None`` leaves
        # the placeholder verbatim (fail-safe). Sourced from
        # ``container.repo_root`` — correct in both dev and packaged/release.
        self._app_root = app_root
        # Async callable ``(conversation_id) -> str | None`` returning the
        # workspace base for a session (session-specific workspace → global
        # configured workspace). Used to (a) bind the per-request workspace
        # base contextvar so EVERY file/exec tool in the request shares the
        # same working root, and (b) default the exec CWD. ``None`` keeps the
        # legacy behaviour (CWD inherits the daemon process == repo root).
        self._session_workspace_resolver = session_workspace_resolver
        # FileGuard guard-token provider (2026-07-06 guard-only reversal).
        # Zero-arg callable returning the live guard-token (or ``None``).
        # The streaming ``exec`` is one of the two LLM tools that MUST mark
        # their spawned subtree as guarded (``QAI_FILEGUARD_GUARD_TOKEN``);
        # re-read per spawn (State-Truth-First — the guard starts lazily).
        # ``None`` (guard off / not started) injects nothing → child
        # bypassed (safe non-guarding default).
        self._guard_token_provider = guard_token_provider
        # Argument-level exec guard (exec_broker command/argument validation).
        # ``FileGuardPort`` whose ``enforce_exec`` runs the exec_broker checks
        # BEFORE the command is spawned (DENY → ``ToolGuardDenied``; ASK →
        # blocking approval popup; ALLOW → return). The non-streaming one-shot
        # ``tool_exec`` handler already calls this; the CHAT streaming exec path
        # (this bridge) is the one users actually hit, so it must run the SAME
        # pre-spawn check or exec_broker is bypassed for chat. ``None`` (guard
        # not wired / disabled) is graceful — the check is skipped (allow-all),
        # matching the one-shot handler's no-guard behaviour.
        self._file_guard = file_guard
        # 2026-07-08 — probe(child_pid) → is a native ASK pending on it?
        # Passed to stream_exec so the timeout PAUSES instead of killing
        # the child while the user decides on a native FileGuard dialog.
        self._ask_pending_probe = ask_pending_probe
        # D2-E: FileGuard native-denial probe (apps/api/_native_denial_probe.py).
        # Composed with build_native_guard_denial_note by the composition root.
        # ``None`` (not wired) → the append helper is a no-op (fail-open), so
        # the streaming exec behaves exactly as before D2-E.
        self._native_denial_probe = native_denial_probe
        # x86 process escape hatch: when True, propagates QAI_GUARD_ALLOW_X86=1
        # in the child env so the native guard64 does not terminate 32-bit
        # children. Sourced from ``settings.security.allow_x86_processes``.
        self._allow_x86 = allow_x86

    async def _enforce_exec_guard(
        self, *, command: str, cwd: str | None
    ) -> None:
        """Run the exec_broker argument-level guard before spawning.

        Delegates to the injected ``FileGuardPort.enforce_exec`` (the SAME
        entrypoint the one-shot ``tool_exec`` handler calls). Raises
        ``ToolGuardDenied`` on DENY; blocks on the approval popup for ASK;
        returns on ALLOW. A ``None`` guard (not wired / disabled) is a graceful
        no-op (allow-all) so this path keeps working when the guard is off.
        """
        guard = self._file_guard
        if guard is None:
            return
        # Always-on protected-write pre-check (parity with the non-streaming
        # ``tool_exec`` handler, which runs this before enforce_exec). The
        # streaming leg previously relied only on the native child-process audit
        # sentinel (subprocess
        # layer) — this restores the pre-spawn guard so a truncating write
        # (``> C:\Qualcomm\...``) is refused up front on BOTH legs.
        try:
            from qai.ai_coding.infrastructure.tools.handlers._protected_command_guard import (  # noqa: E501
                protected_command_sentinel,
            )

            _deny_reason = protected_command_sentinel(command)
        except Exception:  # noqa: BLE001 — pre-check must not break exec
            _deny_reason = None
        if _deny_reason:
            from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

            raise ToolGuardDenied(
                message=_deny_reason,
                error_code="ai_coding.tool.protected_write",
            )
        await guard.enforce_exec(
            command=command,
            cwd=cwd,
            caller="chat.tool.exec_stream",
        )

    def _resolve_guard_token(self) -> str | None:
        """Best-effort read of the live FileGuard guard-token (or ``None``)."""
        provider = self._guard_token_provider
        if provider is None:
            return None
        try:
            token = provider()
        except Exception:  # noqa: BLE001 — never let token lookup break exec
            return None
        return token if isinstance(token, str) and token else None

    async def _resolve_session_workspace(self, request: object) -> str | None:
        """Resolve the workspace base for ``request`` (best-effort)."""
        resolver = self._session_workspace_resolver
        if resolver is None:
            return None
        conversation_id = getattr(request, "conversation_id", None)
        try:
            return await resolver(conversation_id)
        except Exception:  # noqa: BLE001 — never break tool dispatch
            return None

    def _store_consolidated(self, consolidated: str) -> str:
        """Persist an oversized streaming-exec result + return a preview.

        V1 parity: route the streaming exec's FINAL consolidated output
        through the injected ``ToolResultStorePort`` (the SAME store the
        one-shot ai_coding exec uses). A large body is written to disk and the
        model gets a small head+tail preview with a ``read(path=...)`` hint, so
        the downstream model-aware ``ToolResultTruncatorPort`` no longer has to
        destructively truncate it (which dropped the middle irrecoverably).
        No-op when no store is wired or the output is under the store threshold.
        """
        store = self._tool_result_store
        if store is None or not isinstance(consolidated, str) or not consolidated:
            return consolidated
        try:
            preview = store.store(
                consolidated, tool_name="exec", context_hint="stdout"
            )
        except Exception:  # noqa: BLE001 — never break a tool result on store I/O
            return consolidated
        text = getattr(preview, "preview", None)
        return text if isinstance(text, str) and text else consolidated

    def __getattr__(self, name: str) -> object:
        # Transparent delegation for schemas / register / registered_tools /
        # unregister / anything else the route layer or use case reads off
        # the tool port. ``invoke`` is handled explicitly below so it can bind
        # the per-request workspace base; everything else falls through here.
        return getattr(self._inner, name)

    async def invoke(self, request: object) -> object:
        """One-shot tool dispatch with per-request workspace + conversation.

        Binds the session workspace (session-specific → global) as the
        relative-path / default-cwd base for the file tools (read / write /
        edit / glob / grep / apply_patch) so they all resolve under the
        active workspace instead of the daemon CWD (== repo root).

        Also binds the TOP-LEVEL conversation id so the security FileGuard can
        match ``session``-scoped grants only within this collaboration session
        (the main agent + all its sub-agents / participants share the same
        top-level conversation id — see ``PathGrant.matches_scope``). Both
        contextvars are reset in ``finally`` so they never leak across
        requests.
        """
        from qai.ai_coding.infrastructure.tools.handlers import (
            reset_app_root,
            reset_conversation_scope,
            reset_workspace_base,
            set_app_root,
            set_conversation_scope,
            set_workspace_base,
        )

        base = await self._resolve_session_workspace(request)
        token = set_workspace_base(base) if base else None
        # Bind the install/repo root so ``read``/``skill`` expand ``${APP_ROOT}``
        # in a loaded SKILL.md to the real absolute path (reset in ``finally``).
        app_root_token = (
            set_app_root(self._app_root) if self._app_root else None
        )
        # ``conversation_id`` is the top-level collaboration-session root for
        # both normal chat (sub-agents transparently reuse it) and multi-agent
        # discussion (all participants FK to the same conversation).
        conv = getattr(request, "conversation_id", None)
        conv_id = getattr(conv, "value", None) if conv is not None else None
        conv_token = (
            set_conversation_scope(conv_id) if conv_id else None
        )
        try:
            return await self._inner.invoke(request)
        finally:
            if conv_token is not None:
                reset_conversation_scope(conv_token)
            if app_root_token is not None:
                reset_app_root(app_root_token)
            if token is not None:
                reset_workspace_base(token)

    def invoke_streaming(self, request: object) -> object | None:
        tool_name = getattr(request, "tool_name", None)
        if tool_name != "exec":
            return None
        return self._exec_stream(request)

    async def _exec_stream(self, request: object):  # noqa: ANN001 — apps glue
        # Imports kept local so a chat-context import of this module (if it
        # ever happened) would not pull qai.tools / qai.ai_coding eagerly.
        from qai.ai_coding.infrastructure.tools.handlers._multiline_rewrite import (
            cleanup_temp_scripts,
            rewrite_multiline_to_argv,
        )
        from qai.ai_coding.infrastructure.tools.handlers.exec import (
            _resolve_shell,
            _resolve_timeout,
            _select_shell,
        )
        from qai.chat.application.ports import (
            ToolStreamChunk,
            ToolStreamChunkKind,
        )

        args = dict(getattr(request, "arguments", {}) or {})
        command = args.get("command") or ""
        if not isinstance(command, str) or not command.strip():
            yield ToolStreamChunk(
                kind=ToolStreamChunkKind.DONE,
                result="[tool_error] exec: 'command' argument is required",
                ok=False,
            )
            return
        shell = str(args.get("shell") or "auto").lower()
        cwd = args.get("cwd") or None
        if cwd is not None and not isinstance(cwd, str):
            cwd = None
        # Default the streaming-exec CWD to the session workspace (session
        # workspace → global configured workspace) instead of inheriting the
        # daemon process CWD (== repo root). Prevents model-builder runs from
        # polluting the application install dir; keeps artifacts in the
        # workspace. An explicit ``cwd`` from the model still wins.
        if cwd is None:
            cwd = await self._resolve_session_workspace(request)
        # Mirror tool_exec (handlers/exec.py:L394): materialise the cwd so a
        # missing workspace dir never causes [Errno 2] at subprocess spawn time.
        if cwd:
            try:
                from qai.ai_coding.infrastructure.tools.handlers.exec import (
                    _ensure_cwd_exists,
                )
                cwd = _ensure_cwd_exists(cwd)
            except Exception:  # noqa: BLE001 — never break exec on wiring error
                pass
        # V1/v0.5 parity (AGENTS.md 🟢): omit/0 = NO timeout. This is the
        # CHAT streaming exec path (the one users actually hit in chat), so it
        # must share the SAME ``_resolve_timeout`` semantics as the one-shot
        # ``tool_exec`` handler — a prior hard-coded 120s default here silently
        # killed long but legitimate commands (compile / install / big ops)
        # whenever the model omitted ``timeout``. ``0.0`` == unbounded; the
        # downstream stream engine arms its deadline only when ``timeout > 0``.
        timeout = _resolve_timeout(args.get("timeout"))

        # Argument-level exec guard (exec_broker) — MUST run BEFORE any
        # subprocess is spawned, mirroring the one-shot ``tool_exec`` handler
        # (``handlers/exec.py`` ``file_guard.enforce_exec`` before
        # ``_dispatch_exec``). This is the CHAT streaming exec path (the one
        # users actually hit), so without this call exec_broker's
        # command/argument validation + dangerous-flag approval popup would be
        # bypassed for chat entirely. On DENY the guard raises
        # ``ToolGuardDenied``; we convert it into a failed ``DONE`` chunk so the
        # model SEES the denial message and can course-correct (parity with the
        # non-streaming ``registry.py`` handler that maps ``ToolGuardDenied`` to
        # an ``{ok: False, message}`` tool result). ASK blocks here on the
        # approval popup (expected — the user decides); ALLOW returns and the
        # spawn proceeds.
        from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

        try:
            await self._enforce_exec_guard(command=command, cwd=cwd)
        except ToolGuardDenied as exc:
            yield ToolStreamChunk(
                kind=ToolStreamChunkKind.DONE,
                result=f"[tool_error] {exc.message}",
                ok=False,
            )
            return
        # Multi-line command support (chat streaming exec path). A multi-line
        # cmd body cannot survive ``["cmd","/c",command]`` (list2cmdline +
        # cmd.exe double-parse) nor ``cmd.exe /c "cmd1\ncmd2"`` (only cmd1
        # runs). ZERO-PARSE fix: materialise the whole command verbatim into a
        # ``.bat`` and run ``["cmd","/c","<tmp>.bat"]`` — cmd.exe applies its
        # own per-line parsing; the command CONTENT is never parsed (no fragile
        # ``python -c`` extraction). The original ``command`` is preserved for
        # diagnostics + the DONE result; the temp file is unlinked in the
        # ``finally`` once the stream has fully drained. ``(None, [])`` for
        # single-line / powershell / sh → normal ``_select_shell``.
        resolved_shell = _resolve_shell(command, shell)
        rewritten_argv, _tmp_paths = rewrite_multiline_to_argv(
            command, resolved_shell
        )
        if rewritten_argv is not None:
            argv = rewritten_argv
        else:
            # Mirror the one-shot exec tool's shell selection so the streamed
            # command behaves identically to the buffered path.
            argv, _ = _select_shell(command, resolved_shell)
        shell_command = subprocess_join(argv)

        # D2-E: sample wall-clock BEFORE spawn (and AFTER the enforce_exec
        # guard at line ~792) so the post-run native-denial audit query can
        # scope to denies triggered by THIS subprocess since it started.
        # ``timezone.utc`` matches AuditEntry.occurred_at's stored format.
        spawn_started_at = datetime.now(tz=timezone.utc)

        if self._process_runner is not None:
            # Routed-runner branch: hand ``argv`` (the ``["cmd","/c",cmd]``
            # form from ``_select_shell``) directly to the injected
            # :class:`ProcessRunnerPort` (spawns via ``create_subprocess_exec``,
            # no shell).  Post Phase 3 cleanup the runner is the plain
            # :class:`SubprocessProcessRunner` — there is no longer any
            # AppContainer launcher wrap, so behaviour is equivalent to the
            # bare branch below; we keep the gate so audit / diagnostics
            # paths that introspect "ran via runner vs ran via stream_exec"
            # can still distinguish them.
            branch = self._exec_stream_via_runner(
                argv=argv, command=command, cwd=cwd, timeout=timeout,
                spawn_started_at=spawn_started_at,
                resolved_shell=resolved_shell,
            )
        else:
            branch = self._exec_stream_bare(
                shell_command=shell_command,
                command=command,
                cwd=cwd,
                timeout=timeout,
                spawn_started_at=spawn_started_at,
                resolved_shell=resolved_shell,
            )
        try:
            async for chunk in branch:
                yield chunk
        finally:
            cleanup_temp_scripts(_tmp_paths)

    async def _exec_stream_bare(
        self,
        *,
        shell_command: str,
        command: str,
        cwd: str | None,
        timeout: float,
        spawn_started_at: datetime,
        resolved_shell: str,
    ):
        """Legacy streaming exec via ``stream_exec`` (no injected runner).

        Runs the command un-isolated through the OS shell.  Seeded with the
        V1 ``exec`` env (PortableGit PATH + venv ``Scripts`` +
        ``PYTHONUNBUFFERED``) so ``git`` / coreutils resolve and child
        ``print()`` output is line-buffered (V1
        ``backend/tools/_security.py::_build_exec_env``).

        Yields live ``ToolStreamChunk`` STDOUT/STDERR frames, then a final
        ``DONE`` chunk carrying the consolidated result.

        Architectural contract (2026-07-20 fix): the ``consolidated`` text
        fed back to the model must be assembled from ``result.stdout`` and
        ``result.stderr`` **separately** — ``_strip_powershell_clixml`` is
        applied to ``stderr_str`` only, so real user stdout (Write-Host /
        PSHOST output on the stdout channel) is never touched by CLIXML
        stripping.  Mirrors the non-streaming ``handlers.exec.tool_exec``
        path (``exec.py:880-889``).
        """
        from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
            format_exit_diagnostics,
        )
        from qai.chat.application.ports import (
            ToolStreamChunk,
            ToolStreamChunkKind,
        )
        from qai.tools.infrastructure.exec_env import build_exec_env
        from qai.tools.infrastructure.tool_exec_stream import (
            ExecStreamFrameKind,
            stream_exec,
        )
        from qai.ai_coding.infrastructure.tools.handlers.exec import (
            _maybe_append_native_denial_note,
            _strip_powershell_clixml,
        )

        ok = True
        try:
            frames, result = await stream_exec(
                shell_command,
                cwd=cwd,
                env=build_exec_env(
                    guard_token=self._resolve_guard_token(),
                    allow_x86=self._allow_x86,
                ),
                timeout=timeout,
                ask_pending_probe=self._ask_pending_probe,
            )
            child_pid: int | None = None
            async for frame in frames:
                if frame.kind is ExecStreamFrameKind.STDOUT:
                    if frame.data:
                        yield ToolStreamChunk(
                            kind=ToolStreamChunkKind.STDOUT, text=frame.data
                        )
                elif frame.kind is ExecStreamFrameKind.STDERR:
                    if frame.data:
                        yield ToolStreamChunk(
                            kind=ToolStreamChunkKind.STDERR, text=frame.data
                        )
                elif frame.kind is ExecStreamFrameKind.STARTED:
                    child_pid = frame.meta.get("pid") or None
                # CAP_REACHED / TERMINATED carry no user text.
            ok = (result.exit_code == 0) and not result.timed_out
            # 2026-07-20 architectural fix: strip CLIXML from stderr ONLY.
            # Previously we passed the merged ``full_output`` to
            # ``_strip_powershell_clixml`` — but that helper (see
            # ``exec.py:1073``) only guarantees stderr semantics, and its
            # merged-buffer branch could and did trim stdout content that
            # happened to appear after a CLIXML span (the "PSHOST 吞输出"
            # regression: a PowerShell ``Write-Host`` writes both a
            # ``#< CLIXML`` blob on stderr and readable text on stdout;
            # merging them and running strip on the union corrupted the
            # stdout half).  The non-streaming handler already runs strip
            # on ``err_text`` alone (``exec.py:885``); this path now
            # honours the same contract.
            stdout_str = result.stdout
            stderr_str = result.stderr
            if resolved_shell == "powershell":
                stderr_str = _strip_powershell_clixml(stderr_str)
            exit_code = result.exit_code if result.exit_code is not None else -1
            # V1 parity (backend/tools/_exec.py:941-1007): assemble the
            # consolidated LLM-visible text as ``<stdout>\n[stderr]\n<stderr>``
            # + ``[exit code: N]`` on failure + diagnostics on non-zero.  Uses
            # the same helper as ``_render_exec`` (apps/api/_chat_tool_result_render).
            consolidated = _compose_streaming_exec_output(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                timed_out=result.timed_out,
                command=command,
                sandboxed=False,
                format_exit_diagnostics=format_exit_diagnostics,
            )
            # D2-E: append precise native FileGuard denial note (covers both
            # timeout and non-zero exit). The helper is a no-op when exit_code
            # == 0, pid is None, probe is None, or audit found nothing.
            consolidated = await _maybe_append_native_denial_note(
                consolidated,
                exit_code=exit_code,
                pid=child_pid,
                spawn_started_at=spawn_started_at,
                probe=self._native_denial_probe,
            )
        except Exception as exc:  # noqa: BLE001 — surface as failed DONE
            consolidated = f"[tool_error] {exc}"
            ok = False

        yield ToolStreamChunk(
            kind=ToolStreamChunkKind.DONE,
            result=self._store_consolidated(consolidated),
            ok=ok,
        )

    async def _exec_stream_via_runner(
        self,
        *,
        argv: list[str],
        command: str,
        cwd: str | None,
        timeout: float,
        spawn_started_at: datetime,
        resolved_shell: str,
    ):
        """Streaming exec routed through an injected ``ProcessRunnerPort``.

        Hands ``argv`` to the injected runner so the command is spawned via
        ``create_subprocess_exec`` (no outer shell) with the V1 ``exec`` env
        seed (PATH/venv/PortableGit/PYTHONUNBUFFERED).  Post Phase 3 cleanup
        the runner is the plain :class:`SubprocessProcessRunner`; there is
        no AppContainer launcher wrap, so this branch executes the command
        directly on the host (functionally equivalent to the bare branch
        above — kept for diagnostic / audit attribution and to preserve
        the per-branch error semantics).

        Fail-closed: if the runner raises before yielding its first frame,
        surface an error ``DONE`` chunk rather than silently falling back
        to the bare path.

        Yields live ``ToolStreamChunk`` STDOUT/STDERR frames, then a final
        ``DONE`` chunk carrying the consolidated result.

        Architectural contract (2026-07-20 fix): the consolidated text
        assembles ``stdout`` and ``stderr`` **separately** (matching the
        non-streaming ``handlers.exec.tool_exec`` path,
        ``exec.py:880-889``); PowerShell CLIXML stripping is applied to
        the stderr half ONLY.  Previously this branch never called
        ``_strip_powershell_clixml`` at all — CLIXML blobs would reach
        the model verbatim.  Fixed here for architectural consistency
        with the bare branch even though this codepath currently only
        runs with ``resolved_shell != "powershell"`` in production.
        """
        from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
            format_exit_diagnostics,
        )
        from qai.chat.application.ports import (
            ToolStreamChunk,
            ToolStreamChunkKind,
        )
        from qai.platform.process.ports import (
            ProcessExecutionRequest,
            ProcessStartedFrame,
            ProcessStderrFrame,
            ProcessStdoutFrame,
            ProcessTerminatedFrame,
        )
        from qai.tools.infrastructure.exec_env import build_exec_env
        from qai.ai_coding.infrastructure.tools.handlers.exec import (
            _maybe_append_native_denial_note,
            _strip_powershell_clixml,
        )

        request = ProcessExecutionRequest(
            argv=tuple(argv),
            cwd=cwd,
            env=build_exec_env(
                    guard_token=self._resolve_guard_token(),
                    allow_x86=self._allow_x86,
                ),
            timeout_s=timeout if timeout and timeout > 0 else None,
        )

        # 2026-07-20 architectural fix: keep stdout and stderr in separate
        # accumulators rather than a merged list.  This lets us apply
        # ``_strip_powershell_clixml`` to the stderr half only (mirroring
        # exec.py:885 non-streaming behaviour) — a merged list would let
        # a CLIXML strip on the union corrupt real user stdout appearing
        # after the CLIXML span (the "PSHOST 吞输出" regression).
        stdout_collected: list[str] = []
        stderr_collected: list[str] = []
        exit_code: int | None = -1
        timed_out = False
        ok = True
        child_pid: int | None = None
        try:
            async for frame in self._process_runner.run(request):
                if isinstance(frame, ProcessStdoutFrame):
                    text = frame.data.decode("utf-8", errors="replace")
                    if text:
                        stdout_collected.append(text)
                        yield ToolStreamChunk(
                            kind=ToolStreamChunkKind.STDOUT, text=text
                        )
                elif isinstance(frame, ProcessStderrFrame):
                    text = frame.data.decode("utf-8", errors="replace")
                    if text:
                        stderr_collected.append(text)
                        yield ToolStreamChunk(
                            kind=ToolStreamChunkKind.STDERR, text=text
                        )
                elif isinstance(frame, ProcessTerminatedFrame):
                    exit_code = frame.status.exit_code
                    timed_out = frame.status.timed_out
                elif isinstance(frame, ProcessStartedFrame):
                    child_pid = frame.pid
                # STARTED carries no user text.
        except Exception as exc:  # noqa: BLE001 — fail-closed, never bare-run
            # Mid-stream runner error.  Fail-closed semantics: refuse to
            # silently degrade to a bare un-routed run; surface the error
            # to the model instead.
            yield ToolStreamChunk(
                kind=ToolStreamChunkKind.DONE,
                result=f"[tool_error] routed exec failed: {exc}",
                ok=False,
            )
            return

        _code = exit_code if exit_code is not None else -1
        ok = (_code == 0) and not timed_out
        stdout_str = "".join(stdout_collected)
        stderr_str = "".join(stderr_collected)
        # Strip PowerShell CLIXML from stderr ONLY (see the docstring +
        # exec.py:885 for the architectural contract).  Even though the
        # routed-runner branch typically runs argv-form ``["cmd","/c",...]``
        # today, the strip is architecturally required for
        # ``resolved_shell == "powershell"`` future callers and is a no-op
        # for cmd / sh (``_strip_powershell_clixml`` returns the input
        # verbatim when no ``#< CLIXML`` header is present).
        if resolved_shell == "powershell":
            stderr_str = _strip_powershell_clixml(stderr_str)
        # ``sandboxed=True`` here matches the pre-fix diagnostic
        # attribution: this branch is the "ran through the injected
        # runner" one (see the prior comment below in _render_exec's
        # caller in the old code).
        consolidated = _compose_streaming_exec_output(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=_code,
            timed_out=timed_out,
            command=command,
            sandboxed=True,
            format_exit_diagnostics=format_exit_diagnostics,
        )

        # D2-E: append precise native FileGuard denial note (covers both
        # timeout and non-zero exit). The helper is a no-op when _code == 0,
        # pid is None, probe is None, or audit found nothing.
        consolidated = await _maybe_append_native_denial_note(
            consolidated,
            exit_code=_code,
            pid=child_pid,
            spawn_started_at=spawn_started_at,
            probe=self._native_denial_probe,
        )

        yield ToolStreamChunk(
            kind=ToolStreamChunkKind.DONE,
            result=self._store_consolidated(consolidated),
            ok=ok,
        )



def _compose_streaming_exec_output(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    timed_out: bool,
    command: str,
    sandboxed: bool,
    format_exit_diagnostics: "Callable[..., str]",
) -> str:
    """Assemble the LLM-visible consolidated text for a streaming exec run.

    Mirrors :func:`apps.api._chat_tool_result_render._render_exec` (V1
    ``backend/tools/_exec.py:941-1007``): the non-streaming exec handler
    stores ``stdout`` / ``stderr`` as separate fields and lets the render
    step join them; the streaming path historically merged them and fed
    the merged form to the LLM directly, losing the invariant that
    per-stream post-processing (``_strip_powershell_clixml`` on stderr
    only) never touches the other stream.  This helper rebuilds that
    invariant on the streaming side.

    Format:

      * ``<stdout.rstrip()>`` when stdout non-empty
      * ``\\n[stderr]\\n<stderr.rstrip()>`` when stderr non-empty
      * ``\\n[exit code: N]`` when timed out OR ``exit_code != 0``
      * ``\\n<exit_diagnostics>`` when non-zero and diagnostics non-empty

    Empty stdout + empty stderr + exit 0 → ``""`` (an empty consolidated
    result surfaces to the caller as "no output", matching V1 behaviour).
    """
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip()}")
    if timed_out or exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")
    if exit_code != 0 and not timed_out:
        # V1 parity (_exec.py:948-958): targeted diagnostic hint AFTER the
        # ``[exit code: N]`` marker so the model can act on an otherwise
        # opaque non-zero exit.  ``sandboxed`` flag selects attribution
        # (routed-runner vs bare) — historical semantics unchanged.
        diag = format_exit_diagnostics(
            exit_code,
            stdout,
            stderr,
            sandboxed=sandboxed,
            command=command,
        )
        if diag:
            parts.append(diag.lstrip("\n"))
    return "\n".join(parts)


def subprocess_join(argv: list[str]) -> str:
    """Re-join a shell argv into a single command string for ``stream_exec``.

    ``stream_exec`` spawns via ``asyncio.create_subprocess_shell`` (a single
    command string). On Windows that runs ``cmd.exe /c "<command>"``; on POSIX
    ``/bin/sh -c "<command>"``. :func:`_select_shell` returns an argv list:

      * cmd        → ``["cmd", "/c", command]``
      * powershell → ``["powershell", ..., "-Command", wrapped]``
      * sh         → ``["sh", "-c", command]``

    The bug this guards (``''python' is not recognized`` / ``The filename,
    directory name, or volume label syntax is incorrect``): the previous
    implementation rejoined the WHOLE argv with **POSIX** ``shlex.quote``. For
    the cmd case that produced ``cmd /c 'python ...'`` — and since
    ``create_subprocess_shell`` ALREADY wraps in cmd.exe, the child actually
    ran ``cmd.exe /c "cmd /c 'python ...'"``. cmd.exe does not understand POSIX
    single quotes, so ``'python`` became the command name → failure. The model
    then burned extra rounds retrying with different quoting (the main reason
    V2 was slower than V1, which simply did ``Popen(command, shell=True)`` and
    never re-quoted — backend/tools/_exec.py:1411-1418).

    Fix (V1 parity):
      * cmd  → return the RAW inner ``command``. ``create_subprocess_shell``
        already runs it via cmd.exe, so ``cmd /c`` is implicit; no extra
        wrapping or quoting (exactly like V1's ``Popen(command, shell=True)``).
      * other interpreters (powershell / sh) → quote with the rules that match
        the OUTER wrapping shell (``cmd.exe`` on Windows via
        ``subprocess.list2cmdline``; ``/bin/sh`` on POSIX via ``shlex.quote``),
        NOT a hardcoded POSIX quoter.
    """
    # cmd path: hand the raw command straight to the shell (V1 behaviour).
    if len(argv) == 3 and argv[0] == "cmd" and argv[1] == "/c":
        return argv[2]
    # sh / bash path. ``_select_shell`` returns:
    #   * POSIX  → argv[0] == "sh" / "bash"        (bare name; PATH-resolved)
    #   * Windows→ argv[0] == "<...>\\bin\\sh.exe"  (ABSOLUTE PortableGit path,
    #             2026-07-12 arch-match fix — no longer the bare name "sh")
    # so a bare ``argv[0] == "sh"`` check silently STOPS matching on Windows
    # once the interpreter is an absolute path, dropping the whole argv into
    # the ``list2cmdline`` fallthrough below. Detect the sh/bash invocation
    # STRUCTURALLY (3-element ``[<interp>, "-c", command]`` whose interpreter
    # basename is sh/bash) instead of by literal name so both the bare-name
    # (POSIX) and absolute-path (Windows) forms are recognised.
    if len(argv) == 3 and argv[1] == "-c":
        interp = Path(argv[0]).name.lower()
        if interp in ("sh", "sh.exe", "bash", "bash.exe"):
            if sys.platform == "win32":
                # Windows: the outer wrapping shell is cmd.exe
                # (``create_subprocess_shell``). Quote the FULL argv with the
                # cmd.exe convention so an absolute sh.exe path (possibly under
                # a username containing spaces) is passed as a single token and
                # ``cmd.exe /c "<sh.exe> -c "<command>""`` launches the resolved
                # PortableGit interpreter rather than mis-tokenising the path.
                return subprocess.list2cmdline(argv)
            # POSIX: the wrapping shell IS /bin/sh, so returning the raw inner
            # command (which /bin/sh then executes) is correct and matches V1.
            return argv[2]
    # powershell / anything else: the wrapping shell is the platform default
    # (cmd.exe on Windows). Quote with the matching convention.
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _wire_streaming_exec_into_chat(
    *, chat: ChatServices, container: "Container | None" = None
) -> None:
    """Swap the chat streaming use case's tool port for the streaming bridge.

    The use case captured the original ``tools`` reference at construction
    time inside ``_chat_di.build_chat_services`` (DI order builds chat before
    ai_coding).  This post-build hook wraps that same registry in the
    :class:`_StreamingToolInvocationBridge` and re-binds the use case's
    ``_tools`` attribute, so an exec tool_call inside the agentic loop now
    streams stdout/stderr live (``partial=True`` ``tool_result`` frames)
    before the final consolidated frame.  Every other tool is untouched
    (the bridge returns ``None`` from ``invoke_streaming``).

    When ``container`` is supplied and ``security.sandbox_enabled`` is True,
    the security context's plain ``process_runner``
    (:class:`SubprocessProcessRunner` post Phase 3 cleanup; previously the
    ``SandboxedProcessRunner`` wrapper) is injected into the bridge so the
    streamed ``exec`` is routed through the same runner the one-shot
    ai_coding path uses (``_ai_coding_di.py``).  When the gate is off /
    security not booted the runner stays ``None`` and the bare
    ``stream_exec`` path is used.  Both branches now execute commands
    directly on the host; the gate is retained for branch-selection
    parity, not OS isolation.
    """
    uc = getattr(chat, "stream_chat_use_case", None)
    if uc is None:
        return
    inner = getattr(uc, "_tools", None)
    if inner is None:
        return
    # Idempotent: never double-wrap.
    if isinstance(inner, _StreamingToolInvocationBridge):
        return
    process_runner = _resolve_sandbox_process_runner(container)
    # Build the session workspace resolver so the streaming exec + the
    # delegated file tools default their CWD / relative-path base to the
    # active session's workspace (→ global configured workspace), not the
    # daemon process CWD (== repo root).
    session_workspace_resolver = None
    if container is not None:
        try:
            from ._workspace_resolver import build_session_workspace_resolver

            session_workspace_resolver = build_session_workspace_resolver(
                container
            )
        except Exception:  # noqa: BLE001 — never break wiring
            session_workspace_resolver = None
    # Resolve the oversized-output store from the ai_coding context (best-
    # effort): the streaming exec persists a large consolidated result through
    # the SAME store the one-shot ai_coding exec uses, so the model can
    # ``read(path=...)`` the full body instead of losing the middle to the
    # downstream model-aware truncator (V1 parity).
    _tool_result_store = None
    if container is not None:
        _ai_coding = getattr(container, "ai_coding", None)
        _tool_result_store = getattr(_ai_coding, "tool_result_store", None)
    # FileGuard guard-token provider (2026-07-06 guard-only reversal): the
    # streaming ``exec`` tool marks its spawned subtree as guarded. Resolved
    # here in the composition root (only layer allowed to read the
    # ``qai.security`` native-guard adapter); re-read per spawn.
    _guard_token_provider = None
    if container is not None:
        from ._guard_token import build_guard_token_provider

        _guard_token_provider = build_guard_token_provider(container)
    from ._guard_token import build_ask_pending_probe

    _ask_pending_probe = build_ask_pending_probe(container)

    from ._native_denial_probe import build_native_denial_probe

    _native_denial_probe = build_native_denial_probe(container)
    # Argument-level exec guard (exec_broker): the CHAT streaming exec is the
    # path users actually hit, so it must run the SAME ``enforce_exec``
    # pre-spawn check as the one-shot ``tool_exec`` handler — otherwise
    # exec_broker's command/argument validation + dangerous-flag approval popup
    # is bypassed for chat. Built from the SAME production
    # :class:`FileGuardFacade` bridge the one-shot ai_coding path uses
    # (``_file_guard_bridge.build_file_guard``); it honours the FileGuard master
    # switch and degrades to a pass-through when security is not booted. ``None``
    # (no container / build failure) leaves the bridge's guard unset → the check
    # is a graceful no-op (allow-all), matching the one-shot handler's no-guard
    # behaviour so this stays backward compatible.
    _file_guard = None
    if container is not None:
        try:
            from ._file_guard_bridge import build_file_guard

            _file_guard = build_file_guard(container)
        except Exception:  # noqa: BLE001 — never break wiring
            _file_guard = None
    bridge = _StreamingToolInvocationBridge(
        inner,
        process_runner=process_runner,
        session_workspace_resolver=session_workspace_resolver,
        tool_result_store=_tool_result_store,
        guard_token_provider=_guard_token_provider,
        file_guard=_file_guard,
        ask_pending_probe=_ask_pending_probe,
        native_denial_probe=_native_denial_probe,
        allow_x86=(
            container.settings.security.allow_x86_processes
            if container is not None else False
        ),
        app_root=(
            str(container.repo_root)
            if container is not None
            and getattr(container, "repo_root", None) is not None
            else None
        ),
    )
    uc._tools = bridge
    # Sub-agents (the ``agent`` tool) run their OWN tool loop through the
    # ``AgentToolHandler``, which was constructed with the bare tool registry
    # (DI order: handler built before this post-build hook). Re-bind its
    # executor to the SAME bridge so a sub-agent's exec / file tools also (a)
    # default their CWD / relative-path base to the active session workspace
    # and (b) stay routed through the same ``process_runner`` — exactly like
    # the parent loop. Without this
    # the sub-agent inherits the daemon CWD (== repo root) / global workspace
    # instead of the session's ``meta.workspace``.
    agent_handler = getattr(uc, "_agent_event_stream", None)
    if agent_handler is not None and hasattr(agent_handler, "_tool_executor"):
        _inner_exec = getattr(agent_handler, "_tool_executor", None)
        if not isinstance(_inner_exec, _StreamingToolInvocationBridge):
            agent_handler._tool_executor = bridge


def _resolve_sandbox_process_runner(container: "Container | None") -> object | None:
    """Return the security context's ``process_runner`` when ``sandbox_enabled``.

    Thin reader off :class:`SecurityServices`. Phase 3 cleanup (2026-07-01)
    deleted the AppContainer/LPAC launcher wrap; ``container.security.process_runner``
    is now the plain :class:`SubprocessProcessRunner`. The
    ``security.sandbox_enabled`` gate is retained (§3.1 field-name lock) but
    is now inert with respect to OS isolation — flipping it only chooses
    between the chat streaming branches (``_exec_stream_via_runner`` vs
    ``_exec_stream_bare``). The ONLY observable difference is shell vs
    no-shell invocation + audit attribution; both branches execute an
    un-isolated subprocess (no AppContainer/LPAC). Setting
    ``sandbox_enabled=True`` therefore costs an EXIT-75 reboot for a
    no-op OS-isolation gain — sub-process file writes are guarded by the
    native guard64.dll hook (``native_file_guard_enabled``), not this flag.
    Returns ``None`` (= legacy bare ``stream_exec`` path) when no container
    is provided or ``sandbox_enabled`` is False; otherwise returns the
    security context's :class:`ProcessRunnerPort`.
    """
    if container is None:
        return None
    settings = getattr(container, "settings", None)
    security_settings = getattr(settings, "security", None) if settings else None
    if not (
        security_settings
        and getattr(security_settings, "sandbox_enabled", False)
    ):
        return None
    return getattr(
        getattr(container, "security", None), "process_runner", None
    )
