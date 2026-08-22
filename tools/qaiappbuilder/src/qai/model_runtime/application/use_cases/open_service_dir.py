# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``OpenServiceDirUseCase`` — open the service installation directory."""

from __future__ import annotations

from qai.model_runtime.application.ports import InferenceServicePort


class OpenServiceDirUseCase:
    """Open the service installation directory in the OS file explorer.

    The actual os.startfile / subprocess invocation is delegated to
    higher layers (the route handler); this use case simply resolves
    the directory path from the adapter.
    """

    def __init__(self, *, service: InferenceServicePort) -> None:
        self._service = service

    async def execute(self) -> str:
        """Return the install directory path (route handles opening)."""
        return self._service.get_install_dir()


__all__ = ["OpenServiceDirUseCase"]
