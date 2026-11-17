# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""File-backed, in-memory provider overlay for scripted runs (``--provider-file``).

A caller that owns its own gateway address and credential (for example a
validator service driving ``qai agent run``) hands over a small JSON file
describing the providers for ONE run. The file is read once, held in memory
and consulted *before* the persisted providers; nothing is written to the
database or the secret store, and the overlay disappears with the process.

File format::

    {"providers": [
      {"provider_id": "qgenie",
       "base_url": "https://qgenie-api.qualcomm.com/v1",
       "api_key": "${QGENIE_API_KEY}",
       "models": [
         {"model_id": "azure::gpt-6-luna", "context_length": 128000,
          "api_model_id": null, "params": {}}
       ]}
    ]}

``api_key`` is either a literal or a ``${ENV_NAME}`` reference that is resolved
from the environment when the file is loaded. A reference to an unset variable
is an error (never silently "no key"). Keys are never logged or included in
error messages.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["FileProvider", "ProviderFileError", "ProviderOverlay", "load_provider_file"]

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ProviderFileError(ValueError):
    """The provider file is missing, malformed or references an unset variable."""


@dataclass(frozen=True, slots=True, repr=False)
class FileProvider:
    provider_id: str
    base_url: str
    api_key: str | None
    models: tuple[dict[str, Any], ...]

    def __repr__(self) -> str:  # never expose the credential
        return (
            f"FileProvider(provider_id={self.provider_id!r}, base_url={self.base_url!r}, "
            f"api_key={'<set>' if self.api_key else None}, models={len(self.models)})"
        )

    def config(self) -> dict[str, Any]:
        """The provider config dict in the shape the registry bridge reads."""
        return {"base_url": self.base_url, "models": [dict(m) for m in self.models]}


class ProviderOverlay:
    """Immutable set of file providers, queried by model id."""

    __slots__ = ("_providers",)

    def __init__(self, providers: tuple[FileProvider, ...]) -> None:
        self._providers = providers

    @property
    def providers(self) -> tuple[FileProvider, ...]:
        return self._providers

    def rows(self) -> list[dict[str, Any]]:
        """Rows shaped like ``ProviderRegistryPort.list_provider_configs``."""
        return [{"provider_id": p.provider_id, "config": p.config()} for p in self._providers]

    def api_key_for(self, provider_id: str) -> str | None:
        for provider in self._providers:
            if provider.provider_id == provider_id:
                return provider.api_key
        return None

    def has_provider(self, provider_id: str) -> bool:
        return any(p.provider_id == provider_id for p in self._providers)


def load_provider_file(path: str | Path, *, environ: dict[str, str] | None = None) -> ProviderOverlay:
    """Read and validate ``path``; raise :class:`ProviderFileError` on any problem."""
    env = os.environ if environ is None else environ
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ProviderFileError(f"cannot read provider file {file_path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise ProviderFileError(f"provider file {file_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list) or not raw["providers"]:
        raise ProviderFileError("provider file must be an object with a non-empty 'providers' list")

    providers: list[FileProvider] = []
    seen_ids: set[str] = set()
    seen_models: dict[str, str] = {}
    for index, item in enumerate(raw["providers"]):
        where = f"providers[{index}]"
        if not isinstance(item, dict):
            raise ProviderFileError(f"{where} must be an object")
        provider_id = item.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProviderFileError(f"{where}.provider_id must be a non-empty string")
        provider_id = provider_id.strip()
        if provider_id in seen_ids:
            raise ProviderFileError(f"duplicate provider_id {provider_id!r}")
        seen_ids.add(provider_id)
        base_url = item.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ProviderFileError(f"{where}.base_url must be a non-empty string")
        models = item.get("models")
        if not isinstance(models, list) or not models:
            raise ProviderFileError(f"{where}.models must be a non-empty list")
        clean_models: list[dict[str, Any]] = []
        for m_index, model in enumerate(models):
            m_where = f"{where}.models[{m_index}]"
            if not isinstance(model, dict):
                raise ProviderFileError(f"{m_where} must be an object")
            model_id = model.get("model_id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ProviderFileError(f"{m_where}.model_id must be a non-empty string")
            context_length = model.get("context_length")
            if not isinstance(context_length, int) or isinstance(context_length, bool) or context_length <= 0:
                raise ProviderFileError(f"{m_where}.context_length must be a positive integer")
            if model_id in seen_models:
                raise ProviderFileError(
                    f"model {model_id!r} is listed by both {seen_models[model_id]!r} and {provider_id!r}"
                )
            seen_models[model_id] = provider_id
            clean_models.append(dict(model))
        providers.append(
            FileProvider(
                provider_id=provider_id,
                base_url=base_url.strip(),
                api_key=_resolve_key(item.get("api_key"), env, f"{where}.api_key"),
                models=tuple(clean_models),
            )
        )
    return ProviderOverlay(tuple(providers))


def _resolve_key(value: Any, env: Any, where: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProviderFileError(f"{where} must be a string")
    match = _ENV_REF.match(value.strip())
    if match is None:
        return value
    resolved = str(env.get(match.group(1), "")).strip()
    if not resolved:
        raise ProviderFileError(f"{where} references environment variable {match.group(1)} which is not set")
    return resolved
