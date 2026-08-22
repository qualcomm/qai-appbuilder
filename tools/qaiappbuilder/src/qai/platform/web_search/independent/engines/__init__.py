# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Concrete search engines behind the ``web`` provider.

Each module here implements the :class:`~.base.Engine` protocol for one source.
Engines are never imported eagerly by name: the engine loader resolves them
from the ``[[search_engines]]`` TOML ``handler_class`` path via ``importlib``,
so adding an engine is a new module here plus a TOML entry — no edit to this
package's ``__init__``.
"""

from __future__ import annotations

from qai.platform.web_search.independent.engines.base import (
    Engine,
    EngineHit,
    EngineQuery,
    EngineType,
)

__all__ = ["Engine", "EngineHit", "EngineQuery", "EngineType"]
