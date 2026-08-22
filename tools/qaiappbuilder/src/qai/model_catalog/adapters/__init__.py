# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real adapters for the ``model_catalog`` bounded context (PR-044).

Each adapter retires one of the eight ``_Fake<Port>`` classes that
PR-032 placed in ``apps/api/di.py``. Field names on
:class:`apps.api._model_catalog_di.ModelCatalogServices` are part of the
public route contract and have NOT been changed by this PR.
"""

from __future__ import annotations

from .checksum_verifier import (
    BlobPathResolverPort,
    Hash256ChecksumVerifier,
)
from .download_job_repository import SqliteDownloadJobRepository
from .model_entry_repository import SqliteModelEntryRepository
from .provider_registry import SqliteProviderRegistry
from .skill_registry import SqliteModelSkillRegistry

__all__ = [
    "BlobPathResolverPort",
    "Hash256ChecksumVerifier",
    "SqliteDownloadJobRepository",
    "SqliteModelEntryRepository",
    "SqliteModelSkillRegistry",
    "SqliteProviderRegistry",
]
