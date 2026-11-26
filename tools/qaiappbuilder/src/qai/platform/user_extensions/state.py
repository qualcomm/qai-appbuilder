# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Persistent per-extension switches (``<data>/config/extensions.json``).

Holds the user's choices that must survive a restart and must NOT live in
the extension's own directory (replacing the directory keeps them):

* ``disabled``: extension names the user switched off;
* ``settings``: user-edited values of the settings an extension declares
  (non-secret ones; secret values go to the secret store).

Document shape::

    {"version": 1, "disabled": ["a-ext"], "settings": {"a-ext": {"endpoint": "..."}}}

A missing or unreadable file reads as "nothing disabled, no settings" (an
unreadable file is logged and left in place, never overwritten blindly
until the next successful write). Writes are atomic (temp file + replace).
Stdlib only; thread-safe.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

__all__ = ["STATE_FILENAME", "ExtensionStateStore"]

logger = logging.getLogger("qai.platform.user_extensions")

STATE_FILENAME: Final[str] = "extensions.json"
_VERSION: Final[int] = 1


class ExtensionStateStore:
    """Read / write the switches file. Every read goes to disk (tiny file)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # -- disabled ---------------------------------------------------------

    def is_disabled(self, name: str) -> bool:
        return name in self._read()["disabled"]

    def disabled(self) -> frozenset[str]:
        return frozenset(self._read()["disabled"])

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self._lock:
            doc = self._read()
            names = set(doc["disabled"])
            if disabled:
                names.add(name)
            else:
                names.discard(name)
            doc["disabled"] = sorted(names)
            self._write(doc)

    # -- settings ---------------------------------------------------------

    def settings(self, name: str) -> dict[str, Any]:
        value = self._read()["settings"].get(name)
        return dict(value) if isinstance(value, dict) else {}

    def set_settings(self, name: str, values: Mapping[str, Any]) -> None:
        """Replace ``name``'s stored settings with ``values`` (empty -> removed)."""
        with self._lock:
            doc = self._read()
            if values:
                doc["settings"][name] = dict(values)
            else:
                doc["settings"].pop(name, None)
            self._write(doc)

    # -- forget -----------------------------------------------------------

    def forget(self, name: str) -> None:
        """Drop everything stored for ``name`` (uninstall with data removal)."""
        with self._lock:
            doc = self._read()
            doc["disabled"] = [n for n in doc["disabled"] if n != name]
            doc["settings"].pop(name, None)
            self._write(doc)

    # -- io ---------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"version": _VERSION, "disabled": [], "settings": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, ValueError):
            logger.warning("user_extensions.state_unreadable path=%s", self._path, exc_info=True)
            return empty
        if not isinstance(raw, dict):
            return empty
        disabled = raw.get("disabled")
        settings = raw.get("settings")
        return {
            "version": _VERSION,
            "disabled": [n for n in disabled if isinstance(n, str)]
            if isinstance(disabled, list)
            else [],
            "settings": {
                k: v for k, v in settings.items() if isinstance(k, str) and isinstance(v, dict)
            }
            if isinstance(settings, dict)
            else {},
        }

    def _write(self, doc: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".extensions.", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
