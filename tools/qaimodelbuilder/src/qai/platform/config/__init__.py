# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.config — Application configuration and path resolution.

The ``Settings`` class centralises every tunable that used to live in:
- hard-coded literals scattered across ``backend/main.py`` (the V1 port literal
  was 8899; the V2 default is ``ServerSettings.port`` = 4099, matching the
  Okta redirect_uri ``http://localhost:4099/callback`` registered on the
  authorization server — see ``settings.py``), host "127.0.0.1", "data/" path
  prefixes — see inventory 01 / 09 for counts
- ``config/service_config.json`` / ``config/forge_config.json``
- environment variables read with ``os.environ.get(...)`` directly

The ``DataPaths`` port enforces the rule from refactor-plan v2.5 §9.4:
new code MUST NOT reference legacy path literals; everything goes through
``data_paths.db_path()`` / ``blob_dir(...)`` / etc.
"""

from __future__ import annotations

from .paths import DataPaths
from .settings import (
    LOOPBACK_HOST,
    LOOPBACK_HOSTS,
    PUBLIC_BIND_SENTINELS,
    ChatSettings,
    DataSettings,
    LoggingSettings,
    ModelRuntimeSettings,
    SecuritySettings,
    ServerSettings,
    Settings,
    ToolOutputSettings,
    get_settings,
    load_settings,
)

__all__ = [
    "LOOPBACK_HOST",
    "LOOPBACK_HOSTS",
    "PUBLIC_BIND_SENTINELS",
    "ChatSettings",
    "DataPaths",
    "DataSettings",
    "LoggingSettings",
    "ModelRuntimeSettings",
    "SecuritySettings",
    "ServerSettings",
    "Settings",
    "ToolOutputSettings",
    "get_settings",
    "load_settings",
]
