# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Three-state import workflow value objects.

The legacy backend exposes a dry-run / commit / rollback flow under
``/api/appbuilder/import/*`` (see ``02-routes.md`` §3.3 lines 168–170).
The shape is:

* **dry-run** — examine candidate sources, return a plan describing
  which :class:`AppModelDefinition` objects would be added / replaced /
  skipped, without touching anything.
* **commit** — execute the previously-confirmed plan; produces a
  ``commit_id`` (a :class:`RunId`-shaped ULID) usable for rollback.
* **rollback** — undo a previous commit; idempotent.

These VOs describe the **plan** purely. The actual scanning, file
I/O and DB writes belong to adapters (PR-040+) reached through the
:class:`ImportPort` (see ``application/ports.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from qai.app_builder.domain.value_objects import AppModelId

__all__ = ["ImportAction", "ImportPlanItem", "ImportPlan", "CommitId"]


_COMMIT_ID_RE = re.compile(r"^[0-9A-Z]{26}$")
"""Same shape as :class:`qai.app_builder.domain.value_objects.RunId`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitId:
    """Identifier of a successful import commit, shaped like a ULID."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError(
                f"CommitId.value must be str, got {type(self.value).__name__}"
            )
        if not _COMMIT_ID_RE.match(self.value):
            raise ValueError(
                "CommitId.value must be a 26-char Crockford-base32 ULID, "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


class ImportAction(str, Enum):
    """What the import would do for a single candidate.

    ``ADD`` and ``REPLACE`` mutate the registry on commit. ``SKIP`` is
    informational (e.g. candidate already up-to-date or filtered out by
    user choice) and a no-op on commit.
    """

    ADD = "add"
    REPLACE = "replace"
    SKIP = "skip"


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportPlanItem:
    """One row in an :class:`ImportPlan`.

    ``source`` is an opaque string identifying where the candidate came
    from (e.g. a relative path under the import bins, or a remote
    manifest key). It is included verbatim in the audit trail.

    ``display_name`` / ``generated_at`` are OPTIONAL presentation-only
    metadata surfaced to the WebUI promote card (V1 parity — the V1
    candidate DTO carried ``displayName`` + ``generatedAt`` so the card
    could show a human-readable title + a generation timestamp instead of
    the bare model id). They never affect the add/replace/skip decision and
    are ``None`` when the candidate source has no manifest metadata.
    """

    model_id: AppModelId
    action: ImportAction
    source: str
    reason: str | None = None
    #: Human-readable model name (V1 ``displayName``); ``None`` when the
    #: candidate has no manifest / no name field.
    display_name: str | None = None
    #: ISO-8601 generation timestamp (V1 ``generatedAt``); ``None`` when
    #: unknown.
    generated_at: str | None = None
    #: Hard validation errors that block the import (V1 dry_run ``errors``:
    #: missing/too-small weights, runner.py absent / won't compile, missing
    #: required manifest fields). A non-empty tuple forces the item to
    #: ``SKIP`` and the promote card renders each line with a ``✗`` marker.
    errors: tuple[str, ...] = field(default_factory=tuple)
    #: Conflict notes (V1 dry_run ``conflicts``): the target id already
    #: exists. Informational — the resolution is driven by ``conflict_policy``.
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    #: Suggested next semver when the target already exists and the user
    #: picks ``conflict_policy="bump"`` (V1 ``suggestedVersion``). ``None``
    #: when there is no conflict.
    suggested_version: str | None = None
    #: Conflict resolution policy chosen by the user (``"bump"`` /
    #: ``"replace"`` / ``"cancel"``); V1 parity. Carried on the plan item so
    #: commit can bump the version / replace-with-backup / abort. Defaults to
    #: ``"bump"`` (V1 promote card default).
    conflict_policy: str = "bump"

    def __post_init__(self) -> None:
        if not isinstance(self.action, ImportAction):
            raise ValueError(
                "ImportPlanItem.action must be an ImportAction, "
                f"got {type(self.action).__name__}"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("ImportPlanItem.source must be a non-empty str")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError(
                "ImportPlanItem.reason must be str or None, "
                f"got {type(self.reason).__name__}"
            )
        if self.display_name is not None and not isinstance(
            self.display_name, str
        ):
            raise ValueError(
                "ImportPlanItem.display_name must be str or None, "
                f"got {type(self.display_name).__name__}"
            )
        if self.generated_at is not None and not isinstance(
            self.generated_at, str
        ):
            raise ValueError(
                "ImportPlanItem.generated_at must be str or None, "
                f"got {type(self.generated_at).__name__}"
            )
        if not isinstance(self.errors, tuple):
            raise ValueError("ImportPlanItem.errors must be a tuple")
        if not isinstance(self.conflicts, tuple):
            raise ValueError("ImportPlanItem.conflicts must be a tuple")
        if self.suggested_version is not None and not isinstance(
            self.suggested_version, str
        ):
            raise ValueError(
                "ImportPlanItem.suggested_version must be str or None, "
                f"got {type(self.suggested_version).__name__}"
            )
        if not isinstance(self.conflict_policy, str):
            raise ValueError(
                "ImportPlanItem.conflict_policy must be a str, "
                f"got {type(self.conflict_policy).__name__}"
            )

    @property
    def has_errors(self) -> bool:
        """True iff hard validation errors block this item's import."""
        return bool(self.errors)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportPlan:
    """An ordered collection of :class:`ImportPlanItem` objects.

    Constraints:

    * ``model_id`` values are unique across all items (a model cannot
      be both added and skipped in the same plan).
    * Items are stored as a tuple so the plan stays hashable and safe
      to share across threads / coroutines.
    """

    items: tuple[ImportPlanItem, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("ImportPlan.items must be a tuple")
        seen: set[str] = set()
        for item in self.items:
            key = item.model_id.value
            if key in seen:
                raise ValueError(
                    f"ImportPlan contains duplicate model_id {key!r}"
                )
            seen.add(key)

    @property
    def is_empty(self) -> bool:
        return not self.items

    @property
    def is_noop(self) -> bool:
        """True iff every item would be skipped on commit."""
        return all(item.action == ImportAction.SKIP for item in self.items)

    def filter_by_action(self, action: ImportAction) -> tuple[ImportPlanItem, ...]:
        """Return items matching ``action`` (preserving order)."""
        return tuple(item for item in self.items if item.action == action)
