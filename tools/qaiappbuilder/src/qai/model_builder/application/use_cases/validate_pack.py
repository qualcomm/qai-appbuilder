# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ValidatePackUseCase`` — structural validation of an emitted Pack.

Mirrors the legacy ``features/model-builder/scripts/qai_pack_validate.py``
top-level checker. The use case is a thin wrapper around
:class:`PackValidatorPort` so route handlers / bridges can validate
imported Packs (e.g. when the AppBuilder importer wants to revalidate
a candidate handed in via ``sourceWorkdir``) without re-running the
full export pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qai.model_builder.application.ports import PackValidatorPort

__all__ = ["ValidatePackUseCase"]


@dataclass(slots=True)
class ValidatePackUseCase:
    """Validate a Pack directory in place."""

    pack_validator: PackValidatorPort

    async def execute(self, *, pack_dir: Path) -> tuple[bool, tuple[str, ...]]:
        return await self.pack_validator.validate_dir(pack_dir=pack_dir)
