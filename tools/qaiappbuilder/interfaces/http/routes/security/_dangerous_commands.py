# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — custom dangerous-command patterns (P-10).

``GET / PUT /api/security/dangerous-command-patterns`` is the operator surface
for the *union-only* dangerous-command override layer described in
:mod:`qai.security.domain.dangerous_commands`:

* The security domain owns an IMMUTABLE built-in floor
  (:data:`BUILTIN_DANGEROUS_COMMAND_PATTERNS` — 9 high-confidence destructive
  patterns like ``rm -rf`` / ``format C:`` / fork-bomb). This floor is
  NON-REMOVABLE (red line §9.2.4: no one-click disable of the ``rm -rf``
  protection).
* Operators may only ADD extra regex patterns on top of that floor. The
  ``dangerous_command_patterns()`` helper always returns ``BUILTIN + extra``;
  there is no code path — and this endpoint provides no field — that deletes a
  floor entry.

The GET returns the floor as a read-only ``builtin`` list plus the editable
``extra`` list. The PUT accepts ONLY ``extra`` and writes it to the
``dangerous_command_patterns`` runtime-state bucket (shape ``{"extra": [...]}``,
compatible with ``apps.api._file_broker_bridge._resolve_extra_dangerous_patterns``).
Submitted patterns are pre-compiled via
:func:`qai.security.domain.dangerous_commands.compile_extra_patterns`; any
uncompilable entry is dropped and echoed back in the response ``invalid`` list
(a bad operator regex can never crash the guard nor open the box).

Reboot semantics (decision 3B parity): the extra patterns are baked into the
FileBroker guard closure at ``build_file_broker`` time (see
``_file_broker_bridge.build_file_broker``), so a PUT does NOT hot-apply — the
response always carries ``needs_reboot=True`` so the frontend can show the
reboot-confirm banner (same nature as ``file_broker_enabled``). We deliberately
do NOT mutate the live guard closure here (that would touch the production exec
consumption chain); this endpoint only writes the bucket + signals reboot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.security.domain.dangerous_commands import (
    BUILTIN_DANGEROUS_COMMAND_PATTERNS,
    compile_extra_patterns,
)

from ._dto import (
    DangerousCommandPatternsRequest,
    DangerousCommandPatternsResponse,
    DangerousCommandPatternsUpdateResponse,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


#: The read-only built-in floor projected to regex source strings. Computed
#: once at import — the floor is immutable, so this never drifts.
_BUILTIN_PATTERN_SOURCES: tuple[str, ...] = tuple(
    p.pattern for p in BUILTIN_DANGEROUS_COMMAND_PATTERNS
)

#: The runtime-state settings bucket the FileBroker bridge reads (union-only
#: extra patterns). MUST match
#: ``_file_broker_bridge._resolve_extra_dangerous_patterns``.
_BUCKET_KEY = "dangerous_command_patterns"


def _read_extra(container: "Container") -> list[str]:
    """Read the operator's extra patterns from the runtime-state bucket.

    Tolerant of both the ``{"extra": [...]}`` / ``{"patterns": [...]}`` dict
    shapes and a bare list (mirrors ``_resolve_extra_dangerous_patterns``'s
    parser). A missing / malformed bucket degrades to an empty list.
    """
    bucket = container.security.security_runtime_state.get_settings(_BUCKET_KEY)
    if isinstance(bucket, dict):
        raw = bucket.get("extra") or bucket.get("patterns") or []
    elif isinstance(bucket, (list, tuple)):
        raw = bucket
    else:
        raw = []
    return [str(p) for p in raw if isinstance(p, str) or not isinstance(p, (dict, list))]


def _register_dangerous_commands_routes(
    router: "APIRouter", *, container: "Container"
) -> None:
    # ── dangerous-command-patterns (2) ────────────────────────────────

    @router.get(
        "/dangerous-command-patterns",
        response_model=DangerousCommandPatternsResponse,
    )
    async def dangerous_command_patterns_get() -> DangerousCommandPatternsResponse:
        return DangerousCommandPatternsResponse(
            builtin=list(_BUILTIN_PATTERN_SOURCES),
            extra=_read_extra(container),
        )

    @router.put(
        "/dangerous-command-patterns",
        response_model=DangerousCommandPatternsUpdateResponse,
    )
    async def dangerous_command_patterns_put(
        body: DangerousCommandPatternsRequest,
    ) -> DangerousCommandPatternsUpdateResponse:
        # Validate + de-dupe the submitted extra patterns. Only well-formed,
        # compilable, non-empty strings are persisted; anything that fails to
        # compile is dropped and echoed back in ``invalid`` (a bad operator
        # regex can never crash the guard nor delete the floor).
        submitted: list[str] = [
            str(p) for p in body.extra if isinstance(p, str) and p.strip()
        ]
        # Compile the whole set once — the domain helper skips uncompilable
        # entries — then compute which raw strings survived to detect the bad
        # ones without re-implementing the compile rules here.
        valid: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for raw in submitted:
            if raw in seen:
                continue
            seen.add(raw)
            if compile_extra_patterns((raw,)):
                valid.append(raw)
            else:
                invalid.append(raw)

        # Write-through the bucket in the ``{"extra": [...]}`` shape the
        # FileBroker bridge consumes. Union-only by construction: we never
        # touch the immutable floor — the domain's ``dangerous_command_patterns``
        # always returns ``BUILTIN + extra`` regardless of this value.
        stored: dict[str, Any] = {"extra": valid}
        container.security.security_runtime_state.update_settings(
            _BUCKET_KEY, stored
        )

        return DangerousCommandPatternsUpdateResponse(
            builtin=list(_BUILTIN_PATTERN_SOURCES),
            extra=valid,
            # The extra patterns are baked into the FileBroker guard closure at
            # build time, so the write takes effect only after a restart.
            needs_reboot=True,
            invalid=invalid,
        )


__all__ = ["_register_dangerous_commands_routes"]
