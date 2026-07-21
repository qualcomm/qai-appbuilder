# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Canonical repo-relative path constants for the App Builder pack layout.

Single source of truth for the four ``${APP_ROOT}``-anchored directory names
that identify **where a pack lives** on disk:

* built-in pack tree  — pack metadata + weights (release-shipped, version-locked)
* user-imported pack tree — pack metadata + weights (P4; added via
  ``app_import_adapter`` from QAI ModelBuilder or ModelHub exports)

The two anchor pairs (built-in ``pack_root`` / ``model_root``; user
``user_pack_root`` / ``user_weights_root``) are the runtime real-value sources
(see ``FileSystemWeightsPresence``, ``FilesystemSkillPathLocator``,
``FileSystemAppProjectPackager``); *this* module exposes their **repo-relative
string form** for anything that needs to render a path into an LLM prompt,
generate an ``app.yaml`` fragment, or emit user-facing documentation.

Why constants matter
====================
Before P4, these four strings were hard-coded in 8+ locations across the
codebase, SKILL.md files, and shell/batch scripts. Any layout change (e.g.
a future P5 restructure) required chasing every occurrence, and any two
locations could silently drift apart. This module collapses the Python-side
occurrences to a single import.

Mirror points that CANNOT import Python (漂移风险显式化)
=======================================================
Some occurrences of these strings live outside Python and **cannot** import
from this module. When you change a value here, you MUST also update:

1. ``factory/app_builder/fullstack-authoring.SKILL.md`` — the LLM-facing
   authoring guide contains YAML examples (§3 case 1 / case 2) and the
   ``_resolve_dir()`` 4-tier template (§4) that hard-code the same strings.

2. ``factory/app_builder/SKILL.md`` — the top-level Agent SKILL references
   both the built-in and user pack roots in its "what you do / don't do"
   sections.

3. Generated ``run.bat`` / ``run.ps1`` / ``run.sh`` templates in
   ``fullstack-authoring.SKILL.md`` §7 — the shell-side env-var derivation
   guards (``if exist "%REPO_ROOT%\\...\\"``) hard-code the same paths.
   Every existing app's checked-in ``run.bat`` under
   ``data/app_builder/*/run.bat`` is a materialised copy of that template
   and would need re-generation.

4. Any docstring in ``src/qai/app_builder/**`` that mentions these paths for
   explanation purposes (grep for ``factory/app_builder/models`` /
   ``data/app_builder/user_models``). These are read by humans, not code —
   they will not cause runtime bugs if they drift, but they will confuse
   readers.

Do not add trailing slashes here — callers that need one append it explicitly
(e.g. ``f"${{APP_ROOT}}/{USER_PACK_REL}/{mid}/"``). This matches the shape
already used by ``AppProjectPackager`` and ``app.yaml`` ``pack_dir`` fields.
"""
from __future__ import annotations

from typing import Final

#: Built-in pack directory (metadata: manifest.json / runner.py / SKILL.md /
#: assets / provenance). ``${APP_ROOT}/<BUILTIN_PACK_REL>/<pack_id>/``.
#: Mirrors ``FileSystemAppProjectPackager.pack_root``.
BUILTIN_PACK_REL: Final[str] = "factory/app_builder/models"

#: Built-in weights directory (``.bin`` files under
#: ``${APP_ROOT}/<BUILTIN_WEIGHTS_REL>/<pack_id>/<bin>``).
#: Mirrors ``FileSystemAppProjectPackager.model_root``.
BUILTIN_WEIGHTS_REL: Final[str] = "models"

#: User-imported pack directory (P4 layout).
#: ``${APP_ROOT}/<USER_PACK_REL>/<pack_id>/``. Mirrors
#: ``FileSystemAppProjectPackager.user_pack_root`` when wired.
USER_PACK_REL: Final[str] = "data/app_builder/user_models"

#: User-imported weights **root** (P4 layout).
#: Note real ``.bin`` files live one level deeper under
#: ``${APP_ROOT}/<USER_WEIGHTS_REL>/<USER_WEIGHTS_MODELS_SUBDIR>/<pack_id>/<bin>``
#: — the extra ``models/`` layer is required by the manifest ``installPath``
#: convention (each pack manifest declares its ``.bin`` at
#: ``models/<pack_id>/<bin>``, resolved against this root).
#: Mirrors ``FileSystemAppProjectPackager.user_weights_root`` when wired.
USER_WEIGHTS_REL: Final[str] = "data/app_builder/user_model_weights"

#: The extra layer under ``USER_WEIGHTS_REL`` that holds per-pack ``.bin``
#: files. Kept explicit so callers writing a full user-weights path do NOT
#: guess or duplicate the string. Full path pattern:
#: ``${APP_ROOT}/<USER_WEIGHTS_REL>/<USER_WEIGHTS_MODELS_SUBDIR>/<pack_id>/<bin>``.
USER_WEIGHTS_MODELS_SUBDIR: Final[str] = "models"


__all__ = [
    "BUILTIN_PACK_REL",
    "BUILTIN_WEIGHTS_REL",
    "USER_PACK_REL",
    "USER_WEIGHTS_REL",
    "USER_WEIGHTS_MODELS_SUBDIR",
]
