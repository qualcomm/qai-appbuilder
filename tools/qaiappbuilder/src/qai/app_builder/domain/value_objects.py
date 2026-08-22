# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects shared across the App Builder domain.

All VOs are immutable (``frozen=True``), keyword-only, slotted and have
no behaviour beyond input validation. They are designed to be cheap to
construct and free of any I/O.

VOs defined here:

* :class:`RunId` — opaque run identifier (ULID-shaped string).
* :class:`AppModelId` — stable identifier of an :class:`AppModelDefinition`.
* :class:`InputPreset` — named preset bundling default inputs (audio /
  image / text). Just a name + opaque payload mapping; the runner adapter
  decides what to do with the payload.

:class:`Hash256` lives in :mod:`qai.platform.crypto.hashes` and is
re-exported here so existing callers (``from
qai.app_builder.domain.value_objects import Hash256``) continue to
work after PR-026 §10.1 lifted the type to the platform layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from qai.platform.crypto.hashes import Hash256

__all__ = [
    "RunId",
    "AppModelId",
    "Hash256",
    "InputPreset",
]


_RUN_ID_RE = re.compile(r"^[0-9A-Z]{26}$")
"""Crockford-base32 ULID pattern (matches :func:`qai.platform.ids.new_ulid`)."""

_APP_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
"""Allowed characters for app model IDs.

App model IDs are user-visible (used in HTTP path params and JSON
configs). We restrict them to a narrow safe alphabet to avoid path
traversal, shell-quoting issues and silent collisions when used as
filenames or registry keys.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class RunId:
    """Opaque identifier for a single :class:`Run` aggregate.

    The string MUST be a 26-character Crockford-base32 ULID. The VO does
    not generate the value itself — callers inject an
    :class:`qai.platform.ids.IdGenerator` and pass its output.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError(
                f"RunId.value must be str, got {type(self.value).__name__}"
            )
        if not _RUN_ID_RE.match(self.value):
            raise ValueError(
                "RunId.value must be a 26-char Crockford-base32 ULID, "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class AppModelId:
    """Stable identifier of an :class:`AppModelDefinition`.

    Allowed alphabet: ``[A-Za-z0-9._-]``, length 1..128. This matches the
    keys used in ``config/app_builder_models.json`` and the path
    parameter of ``/api/appbuilder/models/{model_id}`` legacy routes.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError(
                f"AppModelId.value must be str, got {type(self.value).__name__}"
            )
        if not _APP_MODEL_ID_RE.match(self.value):
            raise ValueError(
                "AppModelId.value must match [A-Za-z0-9._-]{1,128}, "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class InputPreset:
    """A named bundle of default inputs for an app model.

    The :attr:`payload` is a small JSON-shaped mapping interpreted by the
    runner adapter for the model. The domain layer treats it opaquely;
    the only invariants enforced here are that the name is non-empty and
    the payload is a ``dict`` (so callers cannot smuggle live mutable
    objects in via ``list`` references).
    """

    name: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("InputPreset.name must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError(
                "InputPreset.payload must be a dict, "
                f"got {type(self.payload).__name__}"
            )
