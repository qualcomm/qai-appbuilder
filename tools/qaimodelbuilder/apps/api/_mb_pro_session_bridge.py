# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer bridge: MB Pro session-lifecycle controller.

The chat composer's「Pro / 增强」mode exposes user-controlled
connect / disconnect / version buttons (mb-pro-integration-plan.md §2.9). The
HTTP routes for those buttons live in ``interfaces.http.routes.mb_pro_session``,
but the ``interfaces.http`` layer may only depend on application ports +
platform (import-linter ``interfaces-stays-thin`` contract) — it must NOT reach
into ``qai.chat.infrastructure`` (where the
:class:`~qai.chat.infrastructure.query_service.session_adapter.SessionManager`
per-tab registry + the edition descriptor live).

This bridge is the composition-root seam (apps/api is allowed to import both
``qai.chat.infrastructure`` and ``qai.platform``): it builds a small
``MbProSessionController`` object that the routes consume by duck-typing off
``container.mb_pro_session`` — exactly mirroring how ``_query_service_bridge``
composes the chat ``query_stream_factory`` from edition config + infrastructure
without leaking either into the chat context.

internal-only: the controller is only built when ``settings.is_internal`` is
true; on external editions ``build_mb_pro_session_controller`` returns ``None``
(the ``query_service`` subpackage + edition config are physically excluded), so
the routes short-circuit to a 404-equivalent disabled response.

All imports of the excluded packages are **local to the internal-gated body**
so a stripped external tree never triggers an ImportError at module load.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "MbProProbeError",
    "MbProSessionController",
    "build_mb_pro_session_controller",
]

_log = get_logger(__name__)


def _resolve_effective_session_id(
    session_id: str | None, username: str | None
) -> str | None:
    """Resolve the remote session id to use, scoped to the authenticated user.

    SECURITY: never let an arbitrary client-supplied ``session_id`` win over the
    authenticated ``username``. Remote session ids are namespaced
    ``<username>_<tab_id>`` (see :func:`_find_idle_agent_url`), so a client could
    otherwise POST ``session_id="<other_user>..."`` and attach to another user's
    ``sessions/<other_user>/`` history. A client-supplied session_id is honoured
    ONLY when it is scoped to THIS user (equals the username, or is prefixed
    ``<username>_``); anything else is ignored and we fall back to the trusted
    username. With no username at all, no session id is derivable.
    """
    if not username:
        return None
    if session_id and (session_id == username or session_id.startswith(f"{username}_")):
        return session_id
    return username


class MbProProbeError(RuntimeError):
    """Structured auto-probe failure raised by :func:`_find_idle_agent_url`.

    Carries a machine-readable ``code`` + a ``details`` mapping so the
    interfaces layer maps it to the right HTTP status AND the frontend can
    render a localized (i18n) message from the code instead of transporting a
    pre-formatted Chinese sentence (PROJECT-RULES §3.9 / i18n: user-facing text
    is the frontend's job, the backend ships DATA). Subclasses ``RuntimeError``
    so existing ``except RuntimeError`` handlers still catch it (the "not
    configured" path and generic per-port failures keep working unchanged).

    Codes (stable contract — the frontend switches on these):
      * ``mb_pro.pool_all_offline`` — every port unreachable (→ 502).
      * ``mb_pro.pool_all_busy``    — every port reachable but busy (→ 503);
        ``details["busy_count"]`` = how many machines are busy.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details or {}


class MbProSessionController:
    """Thin lifecycle facade over the per-tab MB Pro session registry.

    Each chat TAB owns an INDEPENDENT MB Pro session (the server keys history
    off ``session_id``; concurrent sessions are supported natively), so every
    method takes a ``tab_id`` and operates on THAT tab's :class:`SessionManager`
    from the registry. ``tab_id`` (not ``conversation_id``) is the key because a
    brand-new chat has no conversation id until its first message while the Pro
    toolbar's「连接」happens before that — and the chat turn keys its session
    lookup off ``tab_id`` too, so the two agree. Rebuilds the MB Pro descriptor
    from edition config on each call (so a config change takes effect without a
    restart). Methods return plain dicts / raise :class:`RuntimeError` so the
    interfaces layer can map them to HTTP responses without importing any
    infrastructure type.
    """

    __slots__ = (
        "_descriptor_factory",
        "_probe_config_factory",
        "_get_manager",
        "_peek_manager",
        "_drop_manager",
        "_greeting_use_case",
    )

    def __init__(
        self,
        *,
        descriptor_factory: Any,
        get_manager: Any,
        peek_manager: Any,
        drop_manager: Any,
        greeting_use_case: Any = None,
        probe_config_factory: Any = None,
    ) -> None:
        self._descriptor_factory = descriptor_factory
        self._probe_config_factory = probe_config_factory
        self._get_manager = get_manager
        self._peek_manager = peek_manager
        self._drop_manager = drop_manager
        self._greeting_use_case = greeting_use_case

    @staticmethod
    def _snapshot(state: Any) -> dict[str, Any]:
        return {
            "connected": state.connected,
            "session_id": state.session_id,
            "agent_url": state.agent_url,
            "insecure": state.insecure,
        }

    @staticmethod
    def _disconnected() -> dict[str, Any]:
        return {
            "connected": False,
            "session_id": None,
            "agent_url": None,
            "insecure": False,
        }

    def get_state(self, *, tab_id: str) -> dict[str, Any]:
        # Read-only: never materialise a manager for a tab that never connected
        # (peek → None ⇒ a clean disconnected snapshot).
        mgr = self._peek_manager(tab_id)
        if mgr is None:
            return self._disconnected()
        return self._snapshot(mgr.get_state())

    async def connect(
        self,
        *,
        tab_id: str,
        agent_url: str | None,
        session_id: str | None,
        insecure: bool | None,
        conversation_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, Any]:
        descriptor = self._descriptor_factory()
        if descriptor is None:
            raise RuntimeError("MB Pro not configured")

        # Auto-probe: when the frontend calls connect WITHOUT specifying
        # ``agent_url``, transparently pick the right port from the pool.
        # Phase 1 finds the port currently running THIS user's session (so a
        # disconnect + reconnect from the same tab lands back on the same
        # machine); Phase 2 falls back to any idle port. We probe regardless
        # of whether the frontend supplied ``session_id`` — a remembered
        # session_id from a previous connect no longer pins the port, since
        # ``_find_idle_agent_url`` uses ``username`` (server-side session dir)
        # to locate the owning machine reliably.
        resolved_agent_url = agent_url
        # See ``_resolve_effective_session_id`` — client-supplied session_id is
        # only honoured when scoped to the authenticated username (prevents
        # cross-user session attach).
        effective_session_id = _resolve_effective_session_id(session_id, username)
        if resolved_agent_url is None:
            probe_cfg = (
                self._probe_config_factory()
                if self._probe_config_factory is not None
                else None
            )
            if probe_cfg is not None:
                host, ports, timeout = probe_cfg
                resolved_agent_url = await _find_idle_agent_url(
                    descriptor=descriptor,
                    insecure=insecure,
                    host=host,
                    ports=ports,
                    timeout=timeout,
                    expected_session_id=effective_session_id,
                )

        # Lazily create THIS tab's manager (connecting is the user's explicit
        # action). Other tabs' sessions are untouched — no displacement, so the
        # UI no longer needs a "disconnect the other one" prompt.
        mgr = self._get_manager(tab_id)
        state = await mgr.connect(
            descriptor=descriptor,
            agent_url=resolved_agent_url,
            session_id_hint=effective_session_id,
            insecure=insecure,
        )
        # Greeting persistence + broadcast (fire-and-forget). On a fresh session
        # the remote Agent pushes a 3-event greeting burst (``queue_state`` →
        # ``agent_ready`` → ``turn`` with self-intro); without consumption it
        # would be discarded by the next turn's ``flush_pending_events``. We
        # drain it HERE, persist the assistant intro as a standalone message,
        # and broadcast frames so any subscriber sees it immediately.
        #
        # Only triggered on a brand-new session creation (no ``session_id``
        # hint): reconnect-by-sid attaches to an existing remote session that
        # already emitted its greeting once. Also requires a ``conversation_id``
        # — the persistence anchor — which the frontend ensures before
        # calling connect on a brand-new tab.
        if (
            state.connected
            and not session_id
            and conversation_id
            and self._greeting_use_case is not None
        ):
            # Reserve the broadcast slot SYNCHRONOUSLY — before the HTTP
            # response returns. The frontend's WS attach right after success
            # would otherwise race the fire-and-forget task and see
            # ``broadcaster.get(tab) is None`` → 404. Reserving here flips
            # ``get()`` to non-None instantly so the WS waits on ``replay``
            # while the background task drains + publishes.
            self._greeting_use_case.reserve_broadcast(
                tab_id=tab_id, conversation_id=conversation_id
            )

            from qai.chat.application.use_cases.mb_pro_greeting import (
                PersistMbProGreetingInput,
            )

            async def _greet() -> None:
                try:
                    await self._greeting_use_case.execute(
                        PersistMbProGreetingInput(
                            conversation_id=conversation_id,
                            tab_id=tab_id,
                        )
                    )
                except Exception:  # noqa: BLE001 — never break connect
                    _log.warning(
                        "mb_pro.greeting_use_case_failed",
                        tab_id=tab_id,
                        conversation_id=conversation_id,
                        exc_info=True,
                    )

            # Detach from the connect HTTP request: the user already sees
            # connection success when this returns; the greeting can land
            # whenever its 2-second drain completes.
            asyncio.create_task(_greet(), name=f"mb_pro_greeting[{tab_id}]")
        return self._snapshot(state)

    async def disconnect(self, *, tab_id: str) -> dict[str, Any]:
        mgr = self._peek_manager(tab_id)
        if mgr is None:
            return self._disconnected()
        state = await mgr.disconnect()
        # Forget the now-disconnected tab's manager so it does not leak an idle
        # entry in the registry (its remote session_id stays restorable until
        # the server's LRU evicts it; the frontend remembers it).
        self._drop_manager(tab_id)
        return self._snapshot(state)

    async def fetch_version(
        self,
        *,
        agent_url: str | None,
        insecure: bool | None,
    ) -> dict[str, Any]:
        # Version is a host-level probe (not tied to a tab/session); use a
        # throwaway manager so it never touches a live tab's one.
        descriptor = self._descriptor_factory()
        if descriptor is None:
            raise RuntimeError("MB Pro not configured")
        from qai.chat.infrastructure.query_service import SessionManager

        return await SessionManager().fetch_version(
            descriptor=descriptor,
            agent_url=agent_url,
            insecure=insecure,
        )


async def _find_idle_agent_url(
    *,
    descriptor: Any,
    insecure: bool | None,
    host: str,
    ports: tuple[int, ...],
    timeout: float,
    busy_retries: int = 2,
    busy_backoff_s: float = 2.0,
    expected_session_id: str | None = None,
) -> str:
    """Probe the MB Pro pool and return the best available port's URL.

    Two-phase selection:

    Phase 1 -- caller's own session (tab-scoped):
      When ``expected_session_id`` is provided, check ``owner_session`` in each
      port's ``/api/busy`` response. If any port is currently running THIS
      tab's session (``owner_session == expected_session_id``), connect there
      immediately -- the tab's task is live on that machine and we must not
      redirect it to a different port. Matching is exact on the tab-scoped id
      (``<username>_<tab_id>``) so other tabs owned by the same user do NOT
      hijack this tab's connection.

    Phase 2 -- idle port fallback:
      If no port owns the caller's session, fall back to the existing
      idle-probe logic (first port that reports ``busy=false``). The tab-scoped
      session_id is still passed as ``session_id_hint`` by the caller so the
      remote server creates ``sessions/<sid>/`` on whichever idle port is
      selected.

    Error codes (raised as :class:`MbProProbeError`):
      * ``mb_pro.pool_all_offline`` -- every port unreachable (-> 502).
      * ``mb_pro.pool_all_busy``    -- every port reachable but busy (-> 503).
    """
    import httpx  # local import — external editions may not carry httpx? (it's a hard dep, safe)

    # HTTP GET is fast; cap probe timeout at 3s regardless of the configured
    # session-connect timeout (which was calibrated for session creation +
    # greeting drain). If a port doesn't answer /api/busy in 3s we treat it
    # as unreachable rather than blocking the whole autoprobe.
    _probe_timeout = min(3.0, max(1.0, float(timeout)))

    async def _probe_one(
        client: httpx.AsyncClient, port: int
    ) -> tuple[int, str, bool, dict[str, Any]] | tuple[int, str, None, str] | None:
        """Probe one port.

        Returns one of:
        * ``(port, url, is_idle, meta)``  — port responded with valid JSON.
        * ``(port, url, None, reason)``   — port answered but not usable;
          ``reason`` is ``"offline"`` (connection refused / non-200) or
          ``"network"`` (host unreachable / DNS / timeout).
        * ``None`` — asyncio.CancelledError was propagated (peer found idle).

        Distinguishing offline from network-error lets the caller tell the user
        whether the *server* is down or their *network* is the problem.
        """
        url = f"http://{host}:{port}"
        try:
            r = await client.get(f"{url}/api/busy", timeout=_probe_timeout)
        except asyncio.CancelledError:
            raise  # a peer already found idle; propagate for cleanup
        except httpx.ConnectError:
            # Connection actively refused (RST) — host reachable, port not
            # listening → the service is offline on this port.
            return (port, url, None, "offline")
        except (httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.TimeoutException):
            # Could not establish a connection within the timeout — could be
            # a slow network, firewall silently dropping packets, or the host
            # being down. Treat conservatively as a network / routing issue
            # rather than a clean service-offline signal.
            return (port, url, None, "network")
        except Exception:  # noqa: BLE001 — other transport errors → network
            return (port, url, None, "network")
        if r.status_code != 200:
            # HTTP error from a reachable host (e.g. 404 on old server) →
            # service is present but doesn't expose /api/busy; treat as offline.
            return (port, url, None, "offline")
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001 — malformed JSON → treat as offline
            return (port, url, None, "offline")
        if not isinstance(payload, dict) or "busy" not in payload:
            return (port, url, None, "offline")
        is_idle = not bool(payload.get("busy"))
        return (port, url, is_idle, payload)

    async def _probe_round(
        client: httpx.AsyncClient,
    ) -> tuple[str | None, list[tuple[int, str, bool, dict[str, Any]]], list[str]]:
        """One concurrent probe pass over all ports.

        Returns ``(idle_url_or_None, reachable_results, failure_reasons)``.
        Collects ALL results before returning so the caller can inspect every
        port's ``owner_session`` for Phase 1 (user's own session) before
        falling back to the first idle port in Phase 2.
        ``failure_reasons`` is a list of ``"offline"`` / ``"network"`` strings
        from failed probes, used by the caller to distinguish network-level
        failures from service-level failures.
        """
        tasks = {
            asyncio.ensure_future(_probe_one(client, p)): p for p in ports
        }
        reachable: list[tuple[int, str, bool, dict[str, Any]]] = []
        failure_reasons: list[str] = []
        try:
            for fut in asyncio.as_completed(list(tasks)):
                res = await fut
                if res is None:
                    continue
                if res[2] is None:
                    # Failed probe: (port, url, None, reason)
                    failure_reasons.append(res[3])  # type: ignore[index]
                    continue
                reachable.append(res)  # type: ignore[arg-type]
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Phase 1: prefer the port that is currently running THIS tab's session.
        if expected_session_id:
            for _port, url, _is_idle, meta in sorted(reachable, key=lambda r: r[0]):
                if meta.get("owner_session") == expected_session_id:
                    _log.info(
                        "mb_pro.autoprobe_owner_match",
                        port=_port,
                        url=url,
                        expected_session_id=expected_session_id,
                    )
                    return url, reachable, failure_reasons

        # Phase 2: first idle port (lowest port number wins for determinism).
        idle_ports = [r for r in reachable if r[2]]
        if idle_ports:
            idle_ports.sort(key=lambda r: r[0])
            _port, url, _, _ = idle_ports[0]
            _log.info("mb_pro.autoprobe_picked", port=_port, url=url)
            return url, reachable, failure_reasons

        return None, reachable, failure_reasons

    attempts = max(1, busy_retries + 1)
    last_reachable: list[tuple[int, str, bool, dict[str, Any]]] = []
    last_failure_reasons: list[str] = []
    async with httpx.AsyncClient(trust_env=False) as client:
        for attempt in range(attempts):
            idle_url, reachable, failure_reasons = await _probe_round(client)
            if idle_url is not None:
                return idle_url
            if not reachable:
                # Nothing answered successfully. Classify: if ALL failures are
                # network-type (timeout / route error), the user's network
                # can't reach the pool — surface a distinct error so the user
                # knows to check their own connectivity rather than assuming
                # the servers are down. If at least one port gave a clean
                # "connection refused" we know the host is reachable and the
                # service just isn't running there.
                all_network = bool(failure_reasons) and all(
                    r == "network" for r in failure_reasons
                )
                if all_network:
                    raise MbProProbeError(
                        "mb_pro.pool_network_error",
                        "无法连接远端 Agent，网络不可达",
                        details={"port_count": len(ports)},
                    )
                raise MbProProbeError(
                    "mb_pro.pool_all_offline",
                    "远端 Agent 全部离线",
                    details={"port_count": len(ports)},
                )
            last_reachable = reachable
            if attempt < attempts - 1:
                _log.info(
                    "mb_pro.autoprobe_all_busy_retry",
                    attempt=attempt + 1,
                    busy_count=len(reachable),
                )
                await asyncio.sleep(busy_backoff_s)

    # All retries exhausted; every reachable port was busy. Aggregate a compact
    # summary for the UI: total busy machines, total queue depth across the
    # pool, distinct phases (so operator sees what kind of work is running).
    busy_metas = [meta for (_, _, _, meta) in last_reachable]
    queue_total = 0
    for meta in busy_metas:
        try:
            queue_total += int(meta.get("queue_len") or 0)
        except (TypeError, ValueError):
            pass
    phases = sorted({str(meta.get("phase") or "") for meta in busy_metas if meta.get("phase")})
    raise MbProProbeError(
        "mb_pro.pool_all_busy",
        f"当前 {len(last_reachable)} 台机器全部繁忙，请稍后再试",
        details={
            "busy_count": len(last_reachable),
            "queue_total": queue_total,
            "phases": phases,
        },
    )


def build_mb_pro_session_controller(*, container: Any) -> MbProSessionController | None:
    """Build the controller, or ``None`` on external / unconfigured editions.

    The whole ``query_service`` subpackage + edition config are physically
    excluded from external artifacts; every import of them is **local to this
    internal-gated body** so a stripped external tree never triggers an
    ImportError when ``di`` imports this bridge at module load.
    """
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    try:
        from qai.platform.edition import get_query_services
        from qai.chat.infrastructure.query_service import (
            QueryServiceDescriptor,
            drop_session_manager,
            get_session_manager,
            peek_session_manager,
        )
    except Exception:  # pragma: no cover - excluded on external
        return None

    def _descriptor_factory() -> Any | None:
        fields = get_query_services().get("mb_pro")
        if not fields:
            return None
        endpoint = fields.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None

        def _path(key: str, default: str) -> str:
            raw = fields.get(key)
            return raw if isinstance(raw, str) and raw else default

        return QueryServiceDescriptor(
            service_id="mb_pro",
            display_name=str(fields.get("display_name") or "Model Builder Pro"),
            endpoint=endpoint,
            transport="session",
            insecure=bool(fields.get("insecure", False)),
            session_path=_path("session_path", "/session"),
            events_path=_path("events_path", "/events/{sid}"),
            send_path=_path("send_path", "/send/{sid}"),
            stop_path=_path("stop_path", "/stop/{sid}"),
            version_path=_path("version_path", "/version"),
        )

    def _probe_config_factory() -> tuple[str, tuple[int, ...], float] | None:
        """Return ``(host, ports, timeout_s)`` for auto-probe, or ``None``.

        Deployment topology is edition-config, not source
        (PROJECT-RULES §3.8.1): ``probe_host`` / ``probe_ports`` /
        ``probe_timeout_s`` live in ``[query_services.mb_pro]`` of
        ``internal_config.toml``. Missing / malformed pool config ⇒ ``None``
        (auto-probe silently disabled; connect falls back to the descriptor's
        single ``endpoint`` — backward compatible with a single-instance
        deployment).
        """
        fields = get_query_services().get("mb_pro")
        if not fields:
            return None
        host_raw = fields.get("probe_host")
        ports_raw = fields.get("probe_ports")
        if not isinstance(host_raw, str) or not host_raw:
            return None
        if not isinstance(ports_raw, (list, tuple)) or not ports_raw:
            return None
        ports: list[int] = []
        for p in ports_raw:
            if isinstance(p, bool):  # bool is-a int in Python; exclude explicitly
                continue
            if isinstance(p, int) and 1 <= p <= 65535:
                ports.append(p)
        if not ports:
            return None
        timeout_raw = fields.get("probe_timeout_s", 2.0)
        try:
            timeout = float(timeout_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            timeout = 2.0
        if timeout <= 0:
            timeout = 2.0
        return (host_raw, tuple(ports), timeout)

    # Greeting use case — built only when the chat container is ready (it
    # depends on the chat conversation repo + stream broadcaster + id
    # generator). Returns None if any dependency is missing so the controller
    # still constructs cleanly (just without greeting injection).
    greeting_use_case = _build_greeting_use_case(
        container=container,
        peek_manager=peek_session_manager,
    )

    # No mb_pro descriptor configured ⇒ the controller would be inert; still
    # build it so /state returns a clean "disconnected", but connect/version
    # raise "not configured" (handled by the factory returning None).
    return MbProSessionController(
        descriptor_factory=_descriptor_factory,
        probe_config_factory=_probe_config_factory,
        get_manager=get_session_manager,
        peek_manager=peek_session_manager,
        drop_manager=drop_session_manager,
        greeting_use_case=greeting_use_case,
    )


def _build_greeting_use_case(*, container: Any, peek_manager: Any) -> Any | None:
    """Build the greeting use case, or ``None`` if dependencies are missing.

    Best-effort: a missing chat dependency is logged once and treated as
    "feature disabled" rather than failing the whole bridge construction.
    """
    chat = getattr(container, "chat", None)
    if chat is None:
        return None
    conversations = getattr(chat, "conversations", None)
    broadcaster = getattr(chat, "chat_stream_broadcaster", None)
    ids = getattr(container, "ids", None) or getattr(chat, "ids", None)
    if conversations is None or broadcaster is None or ids is None:
        _log.info(
            "mb_pro.greeting_disabled_missing_deps",
            has_conversations=conversations is not None,
            has_broadcaster=broadcaster is not None,
            has_ids=ids is not None,
        )
        return None
    try:
        from qai.chat.application.use_cases.mb_pro_greeting import (
            PersistMbProGreetingUseCase,
        )
        # Infrastructure collaborators are constructed HERE at the composition
        # root (the layered contract forbids the application use case from
        # importing infrastructure). This bridge is already internal-gated and
        # is the legitimate place to wire concrete infra into the app-layer Port.
        from qai.chat.infrastructure.query_service.mapper import (
            QueryMappingContext,
        )
        from qai.chat.infrastructure.query_service.mappers.mb_pro_mapper import (
            MbProMapper,
        )
    except Exception:  # pragma: no cover - excluded on external
        return None

    class _MbProGreetingMapper:
        """Adapter satisfying ``GreetingMapperPort`` — wraps the infra mapper
        + per-stream context factory so the application use case stays free of
        any infrastructure import."""

        def __init__(self, id_gen: Any) -> None:
            self._ids = id_gen
            self._mapper = MbProMapper()

        def new_context(self, *, my_session_id: Any = None) -> Any:
            return QueryMappingContext(ids=self._ids, my_session_id=my_session_id)

        def map_event(self, event: Any, ctx: Any) -> Any:
            return self._mapper.map_event(event, ctx)

    return PersistMbProGreetingUseCase(
        conversations=conversations,
        broadcaster=broadcaster,
        peek_manager=peek_manager,
        ids=ids,
        greeting_mapper=_MbProGreetingMapper(ids),
    )
