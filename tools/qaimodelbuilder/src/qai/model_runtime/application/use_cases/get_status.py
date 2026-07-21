# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetStatusUseCase`` — detailed status of the inference daemon.

Returns the adapter's status dict augmented with the V1 ``path_warning``
field: a newline-joined set of warnings emitted when the GenieAPIService
install path or the configured models-root path contain non-ASCII
characters or spaces (the QNN backend converts paths Unicode->ANSI at init
time, which can otherwise break model loading).

The ``path_warning`` rule previously lived inline in the route handler. It
is here now so the interfaces layer only maps the use case result onto the
wire. The models-root value is obtained through an injected async callable
(``models_root_provider``) so this use case never imports ``user_prefs``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from qai.model_runtime.application.ports import InferenceServicePort
from qai.model_runtime.domain.entities import has_unsafe_path


async def _empty_models_root() -> str:
    """Default provider: no models-root configured."""
    return ""


class GetStatusUseCase:
    """Return detailed status (pid, uptime, model, port, memory) + path_warning."""

    def __init__(
        self,
        *,
        service: InferenceServicePort,
        models_root_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._service = service
        self._models_root_provider = models_root_provider or _empty_models_root

    async def execute(self) -> dict[str, Any]:
        status = await self._service.status()

        # V1 surfaces a ``path_warning`` so the UI can warn when the exe or
        # models-root path contains non-ASCII chars / spaces.
        warnings: list[str] = []
        exe_path = str(status.get("exe_path") or "")
        if exe_path and has_unsafe_path(exe_path):
            warnings.append(
                f"GenieAPIService install path contains "
                f"non-ASCII characters or spaces: {exe_path}"
            )
        models_root = await self._models_root_provider()
        if models_root and has_unsafe_path(models_root):
            warnings.append(
                f"Models root path contains non-ASCII characters "
                f"or spaces: {models_root}"
            )
        status["path_warning"] = "\n".join(warnings)
        return status


__all__ = ["GetStatusUseCase"]
