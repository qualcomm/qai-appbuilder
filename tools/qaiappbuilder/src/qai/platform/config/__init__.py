# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.config — Application configuration and path resolution.

The ``Settings`` class centralises every tunable that used to live in:
- hard-coded literals scattered across the legacy WebUI entrypoint (a legacy
  WebUI port literal; the V2 default is ``ServerSettings.port``, sourced from
  the ``factory/config/ports.json`` ``backend`` key and matching the Okta redirect_uri
  registered on the authorization server — see ``settings.py`` /
  ``factory/config/ports.json``), host "127.0.0.1", "data/" path
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
    resolve_app_version,
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
    "resolve_app_version",
]
