# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CLAUDE.md template injector for ai_coding workspaces (PR-095 / S9 H-12).

The legacy ``backend/ai_coding/session_manager.py`` (lines 1243-1311)
copied a project-internal ``CLAUDE.md`` template into every spawned
workspace so the upstream Anthropic agent picked up project-wide
house rules without per-turn prompting (path conventions, dependency
discipline, testing etiquette).

This module restores parity by exposing :func:`copy_claude_md_to`:

* idempotent — it does not overwrite an existing file unless the
  caller passes ``overwrite=True`` (logged as a warning so audit
  trails are clear);
* failure-tolerant — disk failures (out-of-space, permission denied)
  are caught + logged at WARNING; the spawn flow continues so a
  read-only workspace does not block session creation.

Audit: ``docs/90-refactor/S9-final-parity-audit.md`` §2.2 H-12.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from qai.platform.logging import get_logger

__all__ = ["copy_claude_md_to", "find_template", "ClaudeMdInjector"]

logger = get_logger(__name__)

# Co-located template — the file lives next to this module so the
# wheel ships it as package data without extra MANIFEST entries.
_TEMPLATE_PACKAGE_REL = Path(__file__).parent / "templates" / "CLAUDE.md"


def find_template() -> Path | None:
    """Locate the bundled CLAUDE.md template.

    Returns the resolved path when the bundled file is present.
    Returns ``None`` when the template is missing — callers may
    treat that as "no injection requested" and continue.
    """
    if _TEMPLATE_PACKAGE_REL.is_file():
        return _TEMPLATE_PACKAGE_REL
    return None


def copy_claude_md_to(
    working_dir: Path,
    *,
    overwrite: bool = False,
) -> Path | None:
    """Copy the bundled CLAUDE.md template into ``working_dir``.

    Behaviour:

    * If ``working_dir`` does not exist or is not a directory, returns
      ``None`` (caller may decide whether to create it).
    * If ``working_dir/CLAUDE.md`` already exists and ``overwrite`` is
      ``False`` (default), the existing file is preserved and the
      function returns its path.  This makes the call idempotent
      across re-spawns of the same workspace.
    * If ``overwrite=True``, the existing file is replaced and an
      audit log line is emitted at INFO level.
    * Disk failures are caught and logged at WARNING; the function
      returns ``None`` so the spawn flow can continue on a read-only
      workspace without aborting.

    Returns the destination path on success, ``None`` on no-op or
    failure.
    """
    if not working_dir.exists() or not working_dir.is_dir():
        logger.warning(
            "ai_coding.claude_md.workdir_missing",
            working_dir=str(working_dir),
        )
        return None

    template = find_template()
    if template is None:
        logger.warning(
            "ai_coding.claude_md.template_missing",
            looked_at=str(_TEMPLATE_PACKAGE_REL),
        )
        return None

    dest = working_dir / "CLAUDE.md"
    if dest.exists() and not overwrite:
        logger.info(
            "ai_coding.claude_md.kept_existing",
            dest=str(dest),
        )
        return dest

    try:
        shutil.copyfile(template, dest)
    except OSError as exc:
        logger.warning(
            "ai_coding.claude_md.copy_failed",
            dest=str(dest),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    if overwrite and dest.exists():
        logger.info(
            "ai_coding.claude_md.overwritten",
            dest=str(dest),
        )
    else:
        logger.info(
            "ai_coding.claude_md.injected",
            dest=str(dest),
        )
    return dest


class ClaudeMdInjector:
    """Concrete adapter implementing the application-layer
    :class:`qai.ai_coding.application.ports.ClaudeMdInjectorPort`.

    Wraps :func:`copy_claude_md_to` so the production DI
    (``apps/api/_ai_coding_di.py``) can inject it into
    :class:`SpawnCodingSessionUseCase` without the application layer
    importing this infrastructure module — which the
    ``layered-ai_coding`` import-linter contract forbids.

    The ``overwrite`` constructor flag controls whether re-spawning a
    workspace overwrites a user-edited ``CLAUDE.md``; production wires
    ``overwrite=False`` to preserve manual edits across sessions.
    """

    __slots__ = ("_overwrite",)

    def __init__(self, *, overwrite: bool = False) -> None:
        self._overwrite = bool(overwrite)

    def copy_to(self, working_dir: Path) -> Path | None:
        """Copy the bundled CLAUDE.md template into ``working_dir``.

        Delegates to :func:`copy_claude_md_to` so the copy / template-
        lookup logic stays in one place; subclasses / alternate
        adapters may override this method without touching the helper
        function.
        """
        return copy_claude_md_to(working_dir, overwrite=self._overwrite)
