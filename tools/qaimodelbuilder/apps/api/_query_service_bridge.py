# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer bridge: build the chat query-service transport factory.

A *query service* (internal-only) is routed via a ``query::<id>`` model hint
(see :mod:`qai.chat.infrastructure.query_service`). This bridge composes, at the
``apps/api`` composition root, the concrete
:class:`~qai.chat.infrastructure.query_service.adapter.QueryServiceAdapter` for
a given hint from three sources that the chat context must not reach directly:

* **edition config** — the declarative descriptors live in the
  edition-excluded ``internal_config.toml`` (read via
  ``qai.platform.edition.get_query_services``);
* **SecretStore** — the per-service api_key (namespace
  ``qai.model_catalog.provider`` / key ``<service_id>``, the same namespace the
  ``edition_secrets`` install stage provisions and ``_model_resolver_bridge``
  reads);
* **OS identity** — the ``usid`` (single-user desktop app: the OS login name,
  mirroring ``usage_reporter``).

The factory returned here is injected into ``ProviderRoutingLLMStream`` as
``query_stream_factory``. It is **only** built when ``settings.is_internal`` is
true — on external editions the factory is absent (and the whole
``query_service`` subpackage + edition config are physically excluded), so a
``query::*`` hint can never resolve and the routing wrapper falls back to the
default stream.

Layering: this is the apps composition root, allowed to import both
``qai.chat.infrastructure`` and ``qai.platform``; the chat context itself never
imports ``qai.platform.edition`` or the SecretStore (it only sees the abstract
``LLMStreamPort`` the factory returns).
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.ports import LLMStreamPort
    from qai.platform.persistence.secrets import SecretStore

__all__ = ["make_query_stream_factory"]

_log = get_logger(__name__)

# Namespace the per-service api_key is stored under (shared with
# tools/init/edition_secrets + _model_resolver_bridge).
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

_QUERY_PREFIX = "query::"


def _safe_usid() -> str:
    """OS login name (single-user desktop app), degrading to ``unknown``.

    Mirrors ``qai.platform.usage`` ``_safe_username`` so the identity sent to a
    query service matches what usage reporting already uses.
    """
    try:
        return getpass.getuser() or "unknown"
    except Exception:  # username resolution must never raise
        return "unknown"


def _secret_get(store: "SecretStore | None", key: str) -> str | None:
    if store is None:
        return None
    try:
        if not store.exists(_PROVIDER_SECRET_SERVICE, key):
            return None
        return store.get(_PROVIDER_SECRET_SERVICE, key)
    except Exception:  # noqa: BLE001 — any failure ⇒ no usable credential
        return None


def _descriptor_from_config(
    descriptor_cls: Any, service_id: str, fields: dict[str, object]
) -> Any | None:
    endpoint = fields.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return None
    display_name = fields.get("display_name")
    # timeout_seconds may be written as int/float/str in toml; coerce
    # defensively, falling back to the descriptor default on bad input.
    raw_timeout = fields.get("timeout_seconds", 120.0)
    try:
        timeout_seconds = float(raw_timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timeout_seconds = 120.0
    raw_extra = fields.get("extra_payload")
    extra_payload = dict(raw_extra) if isinstance(raw_extra, dict) else {}
    # Session-transport fields (descriptor defaults to ndjson; these are only
    # meaningful when ``transport == "session"``). Passed through verbatim with
    # type-safe fallbacks so a malformed toml never aborts descriptor build.
    transport = fields.get("transport", "ndjson")
    if transport not in ("ndjson", "session", "gomaster"):
        transport = "ndjson"
    session_kwargs: dict[str, object] = {"transport": transport}
    if transport == "session":
        session_kwargs["insecure"] = bool(fields.get("insecure", False))
        for path_key, default in (
            ("session_path", "/session"),
            ("events_path", "/events/{sid}"),
            ("send_path", "/send/{sid}"),
            ("stop_path", "/stop/{sid}"),
            ("version_path", "/version"),
        ):
            raw = fields.get(path_key)
            if isinstance(raw, str) and raw:
                session_kwargs[path_key] = raw
            else:
                session_kwargs[path_key] = default
    elif transport == "gomaster":
        # GoMaster keeps its dialect config (sub-path templates) in the
        # descriptor's ``extra_payload`` so the adapter reads them without this
        # module or the descriptor schema knowing GoMaster-specific field names.
        # Every ``gomaster_*`` key from the edition config is copied verbatim.
        session_kwargs["insecure"] = bool(fields.get("insecure", False))
        for key, value in fields.items():
            if isinstance(key, str) and key.startswith("gomaster_") and isinstance(value, str):
                extra_payload[key] = value
    return descriptor_cls(
        service_id=service_id,
        display_name=str(display_name) if display_name else service_id,
        endpoint=endpoint,
        model=str(fields.get("model", "Turbo")),
        model_url=str(fields.get("model_url", "")),
        chat_type=str(fields.get("chat_type", "auto")),
        rag_mode=str(fields.get("rag_mode", "Default")),
        timeout_seconds=timeout_seconds,
        verify_tls=bool(fields.get("verify_tls", False)),
        extra_payload=extra_payload,
        **session_kwargs,
    )


def make_query_stream_factory(
    *,
    container: Any,
    ids: IdGenerator,
) -> Callable[[str], "LLMStreamPort | None"] | None:
    """Build the ``query::*`` transport factory, or ``None`` on external.

    Returns a callable ``(model_hint) -> LLMStreamPort | None`` suitable for
    ``ProviderRoutingLLMStream(query_stream_factory=...)``. Returns ``None``
    entirely when the build edition is not internal (so no query service is
    ever reachable on external editions).

    The factory is resilient: an unknown service id, a descriptor without a
    bound mapper, or a missing edition config all yield ``None`` for that hint
    (the routing wrapper then falls back to the default stream — graceful, no
    crash).

    The ``query_service`` subpackage + edition config are physically excluded
    from external artifacts; therefore every import of them is **local to this
    internal-gated body** so a stripped external tree never triggers an
    ImportError when ``_chat_di`` imports this bridge at module load.
    """
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    # All internal-only imports are local (the modules are excluded externally).
    try:
        from qai.platform.edition import get_query_services
        from qai.chat.infrastructure.query_service import (
            QueryServiceAdapter,
            QueryServiceDescriptor,
            SessionQueryServiceAdapter,
        )
        from qai.chat.infrastructure.query_service.gomaster_session_adapter import (
            GomasterSessionAdapter,
        )
        from qai.chat.infrastructure.query_service.mappers import (
            CebotMapper,
            GomasterMapper,
            MbProMapper,
        )
    except Exception:  # pragma: no cover - packages excluded on external
        return None

    descriptors_cfg = get_query_services()
    if not descriptors_cfg:
        return None

    # Per-service mapper bindings. Adding a new query service = one descriptor
    # (edition config) + one mapper module + one entry here. The generic
    # QueryServiceAdapter (NDJSON) / SessionQueryServiceAdapter (session) /
    # GomasterSessionAdapter (gomaster) are reused unchanged; the descriptor's
    # ``transport`` field selects which.
    mapper_factories: dict[str, Callable[[], Any]] = {
        "cebot": CebotMapper,
        "mb_pro": MbProMapper,
    }
    # ``query::gomaster`` (the conversational agent link) is only bound when the
    # gomaster service selects gomaster_mode agent/both. Under the default
    # "external" mode the chat hint stays unresolved (the one-click optimize
    # link is used instead); the mapper/adapter code is retained regardless.
    _gm = descriptors_cfg.get("gomaster")
    if _gm and str(_gm.get("gomaster_mode", "external")).lower() in ("agent", "both"):
        mapper_factories["gomaster"] = GomasterMapper

    secret_store = getattr(container, "secret_store", None)
    usid = _safe_usid()

    def _factory(model_hint: str) -> "LLMStreamPort | None":
        if not model_hint.startswith(_QUERY_PREFIX):
            return None
        service_id = model_hint[len(_QUERY_PREFIX):]
        fields = descriptors_cfg.get(service_id)
        if not fields:
            return None
        mapper_factory = mapper_factories.get(service_id)
        if mapper_factory is None:
            _log.warning(
                "chat.query_service.no_mapper_bound",
                extra={"service_id": service_id},
            )
            return None
        descriptor = _descriptor_from_config(
            QueryServiceDescriptor, service_id, fields
        )
        if descriptor is None:
            return None
        # Session-typed transports (MB Pro) resolve THEIR conversation's
        # SessionManager from the per-conversation registry at stream time (by
        # ``request.conversation_id``) — each conversation owns an independent
        # session so histories never mix and each reconnects/restores by its own
        # ``session_id`` (the MB Pro server keys history off it). The adapter is
        # reused across turns/conversations, so it must NOT bind a manager here.
        # NDJSON transports (CEBot) POST per turn with the credential resolved
        # fresh from the SecretStore.
        if getattr(descriptor, "transport", "ndjson") == "session":
            return SessionQueryServiceAdapter(
                descriptor=descriptor,
                mapper=mapper_factory(),
                ids=ids,
            )
        if getattr(descriptor, "transport", "ndjson") == "gomaster":
            # GoMaster is session-typed with per-turn POST→SSE + a per-tab
            # session-id registry (see GomasterSessionAdapter). Auth (option
            # (a)): the token is a credential resolved FRESH per turn — first
            # from the SecretStore (runtime-provisioned / user-set), then the
            # edition-config default (the config-provisioned token) — injected
            # server-side as ``Authorization: Bearer <token>``; the frontend
            # never touches it.
            def _gomaster_token(sid: str = service_id) -> str | None:
                tok = _secret_get(secret_store, sid)
                if tok:
                    return tok
                try:
                    from qai.platform.edition.loader import (
                        get_cloud_provider_api_keys,
                    )

                    return get_cloud_provider_api_keys().get(sid) or None
                except Exception:  # noqa: BLE001
                    return None

            return GomasterSessionAdapter(
                descriptor=descriptor,
                mapper=mapper_factory(),
                ids=ids,
                token_provider=_gomaster_token,
            )
        api_key = _secret_get(secret_store, service_id)
        return QueryServiceAdapter(
            descriptor=descriptor,
            mapper=mapper_factory(),
            ids=ids,
            api_key=api_key,
            usid=usid,
        )

    return _factory
