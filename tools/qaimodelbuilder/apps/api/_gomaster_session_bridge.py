# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer bridge: GoMaster session-lifecycle controller (internal-only).

The chat composer's「GoMaster 在线」mode exposes user-controlled connect /
disconnect / state buttons plus a "open the original GoMaster site" link. The
HTTP routes for those live in :mod:`interfaces.http.routes.gomaster_session`,
but the ``interfaces.http`` layer may only depend on application ports +
platform (import-linter ``interfaces-stays-thin`` contract) — it must NOT reach
into ``qai.chat.infrastructure`` (where the GoMaster per-tab session registry +
the edition descriptor live).

This bridge is the composition-root seam (apps/api may import both
``qai.chat.infrastructure`` and ``qai.platform``): it builds a small
``GomasterSessionController`` the routes consume by duck-typing off
``container.gomaster_session`` — mirroring ``_mb_pro_session_bridge``.

GoMaster's session model is simpler than MB Pro's: there is no long-lived SSE
to hold open. A server-side session is created lazily on the first turn and its
``session_id`` is remembered per-tab in the registry. So here:

* ``connect``  = probe the base_url is reachable (auth check) and eagerly
  create + remember this tab's session so the first turn is instant. Returns a
  connected snapshot.
* ``disconnect`` = forget this tab's remembered ``session_id`` (a fresh one is
  created on the next turn).
* ``get_state`` = whether this tab currently has a remembered session.
* ``native_url`` = the GoMaster base url (internal-only) for the "open original
  site" link — surfaced ONLY on internal editions.

internal-only: the controller is only built when ``settings.is_internal`` is
true; on external editions ``build_gomaster_session_controller`` returns
``None`` (the ``query_service`` subpackage + edition config are physically
excluded), so the routes short-circuit to a 404-equivalent disabled response.
All imports of the excluded packages are **local to the internal-gated body**
so a stripped external tree never triggers an ImportError at module load.
"""

from __future__ import annotations

from typing import Any

import httpx

from qai.platform.logging import get_logger

__all__ = ["GomasterSessionController", "build_gomaster_session_controller"]

_log = get_logger(__name__)


class GomasterSessionController:
    """Thin lifecycle facade over the per-tab GoMaster session registry.

    Each chat TAB owns an independent GoMaster session (the server keys context
    off ``session_id``), so every method takes a ``tab_id``. Methods return
    plain dicts / raise :class:`RuntimeError` so the interfaces layer can map
    them to HTTP responses without importing any infrastructure type.
    """

    __slots__ = (
        "_config_factory",
        "_registry",
        "_token_provider",
    )

    def __init__(
        self,
        *,
        config_factory: Any,
        registry: Any,
        token_provider: Any,
    ) -> None:
        # Returns ``(base_url, session_path, verify) | None`` from edition config.
        self._config_factory = config_factory
        self._registry = registry
        self._token_provider = token_provider

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def _snapshot(self, tab_id: str, *, base_url: str | None) -> dict[str, Any]:
        sid = self._registry.peek(tab_id)
        return {
            "connected": sid is not None,
            "session_id": sid,
            "agent_url": base_url,
        }

    @staticmethod
    def _disconnected() -> dict[str, Any]:
        return {"connected": False, "session_id": None, "agent_url": None}

    # ------------------------------------------------------------------
    # Public API (consumed by the route layer)
    # ------------------------------------------------------------------
    def native_url(self) -> str | None:
        """Return the GoMaster base url for the "open original site" link.

        Internal-only: this controller only exists on internal editions, so a
        non-None return here is itself the edition gate for the frontend link.
        """
        cfg = self._config_factory()
        if cfg is None:
            return None
        return cfg.get("base_url") or None

    def get_state(self, *, tab_id: str) -> dict[str, Any]:
        cfg = self._config_factory()
        base_url = cfg.get("base_url") if cfg else None
        return self._snapshot(tab_id, base_url=base_url)

    async def connect(
        self,
        *,
        tab_id: str,
        agent_url: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Eagerly create + remember this tab's GoMaster session.

        ``agent_url`` overrides the configured base_url (settings dialog); a
        non-empty ``session_id`` attaches to an existing remote session instead
        of creating a new one. Raises :class:`RuntimeError` on failure so the
        route maps it to a 502.
        """
        cfg = self._config_factory()
        if cfg is None:
            raise RuntimeError("GoMaster not configured")
        base = (agent_url or cfg["base_url"]).rstrip("/")
        session_path = cfg["session_path"]
        verify = bool(cfg.get("verify", True))

        # Attach-by-id: trust a caller-supplied session id (remembered client
        # side) without a round-trip; the next turn validates it (404 → re-create).
        if session_id:
            self._registry.remember(tab_id, session_id)
            return self._snapshot(tab_id, base_url=base)

        headers = {"Accept": "application/json"}
        token = self._token_provider()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=5.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, verify=verify, trust_env=False,
                follow_redirects=True, headers=headers,
            ) as client:
                sid = await self._registry.ensure_session(
                    tab_id=tab_id,
                    client=client,
                    base_url=base,
                    session_path=session_path,
                )
        except RuntimeError:
            raise
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 GoMaster ({base})：{exc}") from exc
        return self._snapshot(tab_id, base_url=base) | {"session_id": sid}

    async def disconnect(self, *, tab_id: str) -> dict[str, Any]:
        self._registry.forget(tab_id)
        return self._disconnected()


def build_gomaster_session_controller(
    *, container: Any
) -> GomasterSessionController | None:
    """Build the controller, or ``None`` on external / unconfigured editions.

    Every import of the excluded packages is **local to this internal-gated
    body** so a stripped external tree never triggers an ImportError when
    ``di`` imports this bridge at module load.
    """
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    try:
        from qai.platform.edition import get_query_services
        from qai.platform.edition.loader import get_cloud_provider_api_keys
        from qai.chat.infrastructure.query_service.gomaster_session_adapter import (
            get_gomaster_session_registry,
        )
    except Exception:  # pragma: no cover - excluded on external
        return None

    # Config switch: only wire the conversational ``agent`` link when selected
    # (gomaster_mode in agent/both). Default "external" ⇒ this link is not
    # assembled (the agent API is currently server-gated; the code is retained
    # so flipping the mode enables it with no code change).
    _fields = get_query_services().get("gomaster") or {}
    if str(_fields.get("gomaster_mode", "external")).lower() not in ("agent", "both"):
        return None

    def _config_factory() -> dict[str, Any] | None:
        fields = get_query_services().get("gomaster")
        if not fields:
            return None
        endpoint = fields.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        session_path = fields.get("gomaster_session_path")
        return {
            "base_url": endpoint.rstrip("/"),
            "session_path": (
                session_path
                if isinstance(session_path, str) and session_path
                else "/api/agent/session"
            ),
            "verify": bool(fields.get("verify_tls", True))
            and not bool(fields.get("insecure", False)),
        }

    def _token_provider() -> str | None:
        # Prefer the SecretStore (runtime-provisioned / user-set); fall back to
        # the edition config default (the config-provisioned placeholder/token).
        store = getattr(container, "secret_store", None)
        if store is not None:
            try:
                if store.exists("qai.model_catalog.provider", "gomaster"):
                    val = store.get("qai.model_catalog.provider", "gomaster")
                    if val:
                        return val
            except Exception:  # noqa: BLE001 — any failure ⇒ fall through
                pass
        try:
            return get_cloud_provider_api_keys().get("gomaster") or None
        except Exception:  # noqa: BLE001
            return None

    return GomasterSessionController(
        config_factory=_config_factory,
        registry=get_gomaster_session_registry(),
        token_provider=_token_provider,
    )
