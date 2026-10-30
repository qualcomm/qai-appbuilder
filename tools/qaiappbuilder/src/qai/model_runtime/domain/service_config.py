# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain helpers for the GenieAPIService ``service_config.json`` document.

Pure (no I/O) data + functions for the service-config document so they can
live in the domain layer and be unit-tested in isolation:

- :func:`default_service_config` returns a fresh copy of the built-in
  defaults (V1 parity — ``forge_config_manager`` defaults).
- :func:`deep_merge_defaults` merges a submitted/persisted override into a
  base document (override wins for leaf values), recursing into nested dicts.

These mirror the behaviour previously inlined in the route layer
(``interfaces/http/routes/model_runtime.py``) verbatim; moving them here
keeps the persistence/merge policy out of ``interfaces/``.
"""

from __future__ import annotations

import copy
from typing import Any

# Built-in defaults for service_config.json (V1 parity). Kept as a private
# template; callers MUST go through :func:`default_service_config` so they
# always receive an independent deep copy (never a shared mutable instance).
_SERVICE_CONFIG_DEFAULTS: dict[str, Any] = {
    "local_model": {"enabled": True},
    "cloud_shared": {
        "timeout_seconds": 120,
        "stream_timeout_seconds": 1800,
        "log_debug": False,
        "retry": {"max": 2, "backoff_ms": 200},
        "circuit_breaker": {"failure_threshold": 3, "cooldown_seconds": 60},
        "rate_limit": {"max_inferences_per_task": 20, "max_tokens_per_task": 0},
    },
    "cloud_model": {
        "enabled": True,
        "base_url": "",
        "api_key": "",
        "model": "",
        "context_size": 1000000,
        "upload_policy": {"enable_sensitivity_check": True, "enable_desensitization": True},
        "endpoints": [],
    },
    "enterprise_cloud_model": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "",
        "context_size": 32000,
        "endpoints": [],
    },
    "routing": {
        "enabled": True,
        "prefer_local_for_simple": True,
        "fallback": {"enabled": True, "strategy": "cloud"},
        "agent_routing": {"enabled": False},
        "sticky_routing": {"enabled": True, "ttl_seconds": 1800},
        "incremental_check": {"enabled": False},
        "s2_turn_cleaning": {"enabled": False},
        "metrics": {"enabled": False},
        "cache": {"ttl_seconds": 60, "max_entries": 256},
    },
    "prompt_optimization": {"allowed_tools": []},
    # Debug段（V1 双份 service_config.json 模板均含）。
    # status_update_content_visible:false = GenieAPIService 不把 preparing/
    # inference/tool_call 等状态行（"Processing long text..." / "Preparing
    # inference..." / "Inferencing..."）写入 delta.content，客户端正文不显示
    # 状态噪声（V1 用户感知 parity）。新建 fallback / 深合并时必须保留此默认，
    # 否则 GenieAPIService 进程默认行为会把状态行混入回复正文。
    "debug": {
        "status_update_content_visible": False,
        "log_rule_matches": False,
        "log_inference_stream": False,
    },
}


# Top-level keys of the daemon's retired multi-model slot table. QAI used to
# rewrite ``models[*]`` / ``default_model`` before every spawn; it no longer
# manages them and no UI edits them, yet GenieAPIService still honours them
# (``ModelManager::LoadAllModelsFromConfig`` loads every enabled ``models``
# entry next to the ``-c`` model and lets ``default_model`` override the
# default handle). A stale slot left over from the old sync therefore loads an
# extra model or reroutes requests that omit ``model``, so these keys are
# stripped ONCE per install (one-time migration). Multi-model slots are still a
# real daemon capability: a ``models[]`` the user hand-writes afterwards must
# survive, which is why the migration never re-runs once it has completed.
# Only these exact top-level keys: nothing else in the document is touched.
LEGACY_MODEL_SLOT_KEYS: tuple[str, ...] = (
    "_comment_models",
    "models",
    "_comment_default_model",
    "default_model",
)


def strip_legacy_model_slot_keys(doc: dict[str, Any]) -> list[str]:
    """Remove :data:`LEGACY_MODEL_SLOT_KEYS` from *doc* in place.

    Returns the removed keys in document order; an empty list means *doc*
    was already clean (callers use that to skip the write).
    """
    removed = [k for k in doc if k in LEGACY_MODEL_SLOT_KEYS]
    for k in removed:
        del doc[k]
    return removed


def default_service_config() -> dict[str, Any]:
    """Return a fresh deep copy of the built-in service_config defaults."""
    return copy.deepcopy(_SERVICE_CONFIG_DEFAULTS)


def deep_merge_defaults(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge *override* into *base* in place (override wins for leaf values).

    Recurses into nested dicts; non-dict values from *override* replace the
    corresponding base value. Returns *base* (mutated) for convenience.
    """
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            deep_merge_defaults(base[k], v)
        else:
            base[k] = v
    return base


__all__ = [
    "LEGACY_MODEL_SLOT_KEYS",
    "default_service_config",
    "deep_merge_defaults",
    "strip_legacy_model_slot_keys",
]
