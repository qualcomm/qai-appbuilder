# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports owned by ``apps.api`` for the App Builder context.

These ports describe collaborators needed by the HTTP interface layer
that have **no use case wrapper** in ``src/qai/app_builder/application``,
but that the route handlers need to satisfy the inventory
``02-routes.md`` §3.3 capability surface.

S3 PR-034 introduced:

* :class:`ArtifactBlobReaderPort` — streaming read of an artifact's
  bytes (used by ``GET /api/app-builder/artifacts/{run_id}/{path}/blob``).
  The existing :class:`qai.app_builder.application.ports.ArtifactStorePort`
  defines ``write`` / ``write_stream`` only; reading bytes back is a
  separate concern intentionally kept at the apps-layer because it is
  HTTP-specific (streaming chunks straight into a
  :class:`fastapi.responses.StreamingResponse`).  The production
  implementation is
  :class:`qai.app_builder.infrastructure.artifact_blob_reader.FileSystemArtifactBlobReader`,
  which mirrors the layout of
  :class:`qai.app_builder.infrastructure.artifact_store.FileSystemArtifactStore`
  under ``DataPaths.app_builder_artifacts``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class ArtifactBlobReaderPort(Protocol):
    """Stream the bytes of an artifact previously persisted by a Run.

    Implementations MUST stream chunks (no whole-file buffering) so the
    HTTP layer can pipe them directly to a ``StreamingResponse``. The
    chunk size is left to the implementation (typically 64 KiB).

    The method is shaped like an async-generator factory: it returns an
    :class:`AsyncIterator[bytes]` synchronously (no ``await``) so the
    HTTP layer can ``async for chunk in reader.open(...)`` directly.
    """

    def open(
        self,
        *,
        run_id: str,
        relative_path: str,
    ) -> AsyncIterator[bytes]:
        """Return an async iterator of byte chunks for the given artifact.

        The implementation MUST raise
        :class:`qai.app_builder.domain.errors.ArtifactWriteError` (or a
        :class:`qai.platform.errors.NotFoundError`) when the artifact
        cannot be located. Path is the relative logical path stored on
        the :class:`qai.app_builder.domain.artifact.Artifact` VO.
        """
        ...


__all__ = ["ArtifactBlobReaderPort"]
