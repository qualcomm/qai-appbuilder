# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Config loader for the shared-kernel ``web`` search provider roster.

Reads :file:`search_config.toml` (sitting next to this module) which carries
the ``[[search_engines]]`` engine roster and the ``[independent_search]``
platform-operator tuning block (aggregator deadlines, browser parameters,
health-scoring knobs). This package ships to BOTH editions, so the roster and
its accessors are NOT internal-only: the multi-engine ``web`` provider works
externally too. Only the CEBot backend (in the excluded
``qai.platform.edition``) is internal-only.

Both accessors degrade gracefully (``[]`` / ``{}``) on a missing / malformed
file, mirroring ``qai.platform.edition.loader`` style, so the DI wiring becomes
a clean no-op rather than aborting startup.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

#: Path to the roster TOML file (sits next to this module).
_CONFIG_TOML = Path(__file__).with_name("search_config.toml")

__all__ = ["get_independent_search_config", "get_search_engines"]


def _load_config() -> Mapping[str, object]:
    """Return the whole parsed TOML as a dict (or ``{}`` on any failure)."""
    try:
        with _CONFIG_TOML.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data


def get_search_engines() -> list[dict[str, object]]:
    """Return the ``[[search_engines]]`` engine specs as a list of dicts.

    Each entry declares one independent web-search engine (``engine_id`` /
    ``handler_class`` / ``credential_key`` / ...) that
    ``independent.engine_loader`` normalizes into an ``EngineSpec`` and loads by
    ``handler_class`` via ``importlib``. The api_key itself is never stored here
    (it flows through the SecretStore path, AGENTS.md §3.3).

    Returns ``[]`` when the file or section is missing / malformed.
    """
    data = _load_config()
    section = data.get("search_engines", [])
    if not isinstance(section, list):
        return []
    out: list[dict[str, object]] = []
    for entry in section:
        if isinstance(entry, dict):
            out.append(dict(entry))
    return out


def get_independent_search_config() -> dict[str, object]:
    """Return the ``[independent_search]`` fixed-platform config block.

    Reads the ``[independent_search]`` table (plus its nested
    ``[independent_search.browser]`` / ``[independent_search.scoring]``
    companions) that carries the aggregator deadlines, browser parameters,
    health-scoring knobs — platform-operator settings that users cannot override
    from ``forge_config``.

    Returns ``{}`` when the file or section is missing / malformed.
    """
    data = _load_config()
    section = data.get("independent_search", {})
    if not isinstance(section, dict):
        return {}
    return dict(section)
