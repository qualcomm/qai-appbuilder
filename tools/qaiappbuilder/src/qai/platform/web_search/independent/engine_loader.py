# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Engine loader: TOML specs -> live :class:`Engine` instances.

The set of independent web-search engines is declared entirely in
``search_config.toml`` (``[[search_engines]]``), never hard-coded. This module
turns those raw TOML dicts into typed :class:`EngineSpec` records
(:func:`load_engine_specs`) and then dynamically loads each ``handler_class`` via
``importlib`` and instantiates it (:func:`build_engines`).

Nothing here aborts on a bad entry: a malformed spec is skipped, an import or
construction failure is logged at ``warning`` and skipped, a credential-required
engine with no stored key is treated as *unconfigured* and skipped, and a browser
engine whose runtime is unavailable is skipped. One broken engine never blocks
the others — the aggregator runs whatever successfully assembled.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from qai.platform.logging import get_logger
from qai.platform.web_search.independent.engines.base import Engine, EngineType

if TYPE_CHECKING:
    from qai.platform.persistence.secrets import SecretStore

__all__ = ["EngineSpec", "build_engines", "load_engine_specs"]

_log = get_logger(__name__)

#: SecretStore namespace credentials are stored under (shared with the CEBot /
#: cloud-provider paths). The per-engine key is ``EngineSpec.credential_key``.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"  # noqa: S105 — namespace, not a secret

#: The engine transport kind that drives credential-free browser handling.
_BROWSER_ENGINE_TYPE = "browser"

#: Valid ``engine_type`` values, taken from the shared ``EngineType`` literal.
_VALID_ENGINE_TYPES: frozenset[str] = frozenset(get_args(EngineType))


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """One engine's declarative descriptor, normalized from a TOML entry.

    ``handler_class`` is a dotted import path (``pkg.module:Class`` or
    ``pkg.module.Class``) resolved by :func:`build_engines`. ``credential_key``
    is empty for keyless engines; when ``requires_credential`` is true it names
    the SecretStore key under the shared provider namespace.
    """

    engine_id: str
    display_name: str
    engine_type: str
    handler_class: str
    enabled_by_default: bool
    requires_credential: bool
    credential_key: str
    endpoint: str
    priority_hint: int
    description_i18n_key: str


def load_engine_specs(specs_raw: list[dict[str, object]]) -> list[EngineSpec]:
    """Normalize raw ``[[search_engines]]`` TOML dicts into :class:`EngineSpec`.

    Missing optional fields fall back to sensible defaults. An entry with no
    usable ``engine_id`` or ``handler_class``, or an unknown ``engine_type``, is
    skipped with a ``warning`` — it can never assemble into an engine.
    """
    specs: list[EngineSpec] = []
    for entry in specs_raw:
        if not isinstance(entry, dict):
            _log.warning("search_engine.spec_not_a_table", entry=repr(entry))
            continue
        engine_id = _as_str(entry.get("engine_id"))
        handler_class = _as_str(entry.get("handler_class"))
        if not engine_id or not handler_class:
            _log.warning(
                "search_engine.spec_incomplete",
                engine_id=engine_id,
                handler_class=handler_class,
            )
            continue
        engine_type = _as_str(entry.get("engine_type"))
        if engine_type not in _VALID_ENGINE_TYPES:
            _log.warning(
                "search_engine.spec_bad_type",
                engine_id=engine_id,
                engine_type=engine_type,
            )
            continue
        specs.append(
            EngineSpec(
                engine_id=engine_id,
                display_name=_as_str(entry.get("display_name")) or engine_id,
                engine_type=engine_type,
                handler_class=handler_class,
                enabled_by_default=bool(entry.get("enabled_by_default", True)),
                requires_credential=bool(entry.get("requires_credential", False)),
                credential_key=_as_str(entry.get("credential_key")),
                endpoint=_as_str(entry.get("endpoint")),
                priority_hint=_as_int(entry.get("priority_hint")),
                description_i18n_key=_as_str(entry.get("description_i18n_key")),
            )
        )
    return specs


def build_engines(
    specs: list[EngineSpec],
    *,
    secret_store: SecretStore | None = None,
) -> list[Engine]:
    """Dynamically load each spec's ``handler_class`` and instantiate it.

    Per spec:

    * a ``browser`` engine whose runtime is unavailable (``is_available()`` is
      false) is skipped — it can never run in this environment;
    * a ``requires_credential`` engine resolves its key from the SecretStore;
      a missing / unreadable credential marks it *unconfigured* and skips it
      (no exception — the spec stays available for the UI to prompt config);
    * an import or construction failure is logged at ``warning`` and skipped.

    Every engine's ``__init__`` is called with the subset of ``credential`` /
    ``http_client`` keywords it actually accepts (engines self-provision their
    HTTP client when none is passed).
    """
    engines: list[Engine] = []
    skipped: list[str] = []
    for spec in specs:
        cls = _import_handler(spec)
        if cls is None:
            skipped.append(f"{spec.engine_id}(import_failed)")
            continue
        if not _browser_runtime_ready(spec, cls):
            skipped.append(f"{spec.engine_id}(browser_unavail)")
            continue
        credential: str | None = None
        if spec.requires_credential:
            credential = _resolve_credential(spec, secret_store)
            if credential is None:
                _log.warning(
                    "search_engine.unconfigured",
                    engine_id=spec.engine_id,
                    credential_key=spec.credential_key,
                )
                skipped.append(f"{spec.engine_id}(no_credential)")
                continue
        engine = _instantiate(spec, cls, credential)
        if engine is not None:
            engines.append(engine)
        else:
            skipped.append(f"{spec.engine_id}(instantiate_failed)")
    _log.info(
        "search_engine.build_summary",
        extra={
            "built": [e.engine_id for e in engines],
            "skipped": skipped,
        },
    )
    return engines


def _import_handler(spec: EngineSpec) -> type | None:
    """Resolve ``spec.handler_class`` to a class, or ``None`` on failure."""
    module_path, _, attr = spec.handler_class.replace(":", ".").rpartition(".")
    if not module_path or not attr:
        _log.warning(
            "search_engine.bad_handler_path",
            engine_id=spec.engine_id,
            handler_class=spec.handler_class,
        )
        return None
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, attr)
    except Exception as exc:  # noqa: BLE001 — any import failure ⇒ skip engine
        _log.warning(
            "search_engine.import_failed",
            engine_id=spec.engine_id,
            handler_class=spec.handler_class,
            error=str(exc),
        )
        return None
    if not isinstance(cls, type):
        _log.warning(
            "search_engine.handler_not_a_class",
            engine_id=spec.engine_id,
            handler_class=spec.handler_class,
        )
        return None
    return cls


def _browser_runtime_ready(spec: EngineSpec, cls: type) -> bool:
    """Return ``False`` iff a browser engine reports its runtime unavailable."""
    if spec.engine_type != _BROWSER_ENGINE_TYPE:
        return True
    is_available = getattr(cls, "is_available", None)
    if not callable(is_available):
        return True
    try:
        available = bool(is_available())
    except Exception as exc:  # noqa: BLE001 — probe failure ⇒ treat as unavailable
        _log.warning(
            "search_engine.availability_probe_failed",
            engine_id=spec.engine_id,
            error=str(exc),
        )
        return False
    if not available:
        _log.warning("search_engine.browser_unavailable", engine_id=spec.engine_id)
        return False
    return True


def _resolve_credential(
    spec: EngineSpec,
    secret_store: SecretStore | None,
) -> str | None:
    """Return the stored credential for ``spec``, or ``None`` if unavailable."""
    if secret_store is None or not spec.credential_key:
        return None
    try:
        if not secret_store.exists(_PROVIDER_SECRET_SERVICE, spec.credential_key):
            return None
        return secret_store.get(_PROVIDER_SECRET_SERVICE, spec.credential_key)
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ no usable credential
        _log.warning(
            "search_engine.credential_read_failed",
            engine_id=spec.engine_id,
            credential_key=spec.credential_key,
            error=str(exc),
        )
        return None


def _instantiate(spec: EngineSpec, cls: type, credential: str | None) -> Engine | None:
    """Construct ``cls`` with the accepted subset of the uniform kwargs."""
    candidate: dict[str, object] = {"credential": credential, "http_client": None}
    kwargs = _accepted_kwargs(cls, candidate)
    try:
        instance = cls(**kwargs)
    except Exception as exc:  # noqa: BLE001 — construction failure ⇒ skip engine
        _log.warning(
            "search_engine.construct_failed",
            engine_id=spec.engine_id,
            handler_class=spec.handler_class,
            error=str(exc),
        )
        return None
    return instance


def _accepted_kwargs(cls: type, candidate: dict[str, object]) -> dict[str, object]:
    """Filter ``candidate`` down to the keyword params ``cls.__init__`` accepts."""
    try:
        params = inspect.signature(cls).parameters
    except (ValueError, TypeError):
        return {}
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_var_kw:
        return dict(candidate)
    return {name: value for name, value in candidate.items() if name in params}


def _as_str(value: object) -> str:
    """Coerce a TOML value to ``str`` (empty string when not a string)."""
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    """Coerce a TOML value to ``int`` (``0`` when not an int; bools rejected)."""
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0
