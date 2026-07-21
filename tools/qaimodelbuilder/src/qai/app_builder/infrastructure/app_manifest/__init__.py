# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``manifest.json`` reader + populating registry adapters (PR-303).

Two pieces:

* :class:`FileSystemManifestReader` — adapter that reads a directory of
  Pack manifests (``<root>/<model_id>/manifest.json``) into typed
  :class:`qai.app_builder.domain.pack_manifest.PackManifest` instances,
  performing field-name translation (camelCase wire ↔ snake_case
  domain) and schema-version validation.

* :func:`populate_runner_registry_from_manifests` — bridge that takes
  a list of :class:`PackManifest` + a base directory + a
  :class:`qai.app_builder.infrastructure.command_resolver.RunnerCommandRegistryPort`
  and registers a :class:`RunnerSpec` for each pack so the
  :class:`ProcessBackedAppRunner` knows how to spawn each Pack.

PR-306 will move the Pack root from ``features/app-builder/models/``
to ``factory/app_builder/models/`` (release artifacts); the reader
accepts whatever root path the caller passes, so that migration is a
DI / lifespan change, not a reader change.
"""

from __future__ import annotations

from .reader import FileSystemManifestReader, ManifestParseError
from .registry_bridge import (
    populate_runner_registry_from_manifests,
    select_runner_interpreter,
)

__all__ = [
    "FileSystemManifestReader",
    "ManifestParseError",
    "populate_runner_registry_from_manifests",
    "select_runner_interpreter",
]
