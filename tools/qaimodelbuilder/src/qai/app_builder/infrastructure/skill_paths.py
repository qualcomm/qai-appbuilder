# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem locator for App Builder SKILL.md file paths.

Companion to :class:`qai.app_builder.application.use_cases.skill_and_schema.FilesystemSkillFileLoader`
(which reads SKILL *bodies*); this adapter resolves the *paths* of the
SKILL files the chat system prompt should inline for an App Builder
session — the top-level guide plus the currently selected Pack's SKILL.

V1 parity (``backend/app_builder/skill_resolver.resolve_skill_files``):

* the top-level ``<pack_root>/../SKILL.md`` (V2 ships it at
  ``factory/app_builder/SKILL.md``; ``pack_root`` is
  ``factory/app_builder/models``, so it is ``pack_root.parent / "SKILL.md"``),
  injected unconditionally when the file exists;
* the selected Pack's SKILL file (``<pack_root>/<model_id>/<skill_file>``)
  injected only when ``manifest.skill.enabled`` and the file exists.

The locator returns **absolute, existing** path strings so the chat
:class:`RichSystemPromptBuilder._build_app_builder_prompt` can ``open()``
them directly. Missing files are skipped (never raised) so a half-installed
Pack never breaks the chat prompt.

Implements the structural
:class:`qai.app_builder.application.use_cases.skill_and_schema.SkillPathLocator`
protocol; wired by ``apps/api/_app_builder_di.py``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FilesystemSkillPathLocator"]


class FilesystemSkillPathLocator:
    """Resolve SKILL.md file paths for the App Builder chat prompt.

    Bounded to ``pack_root`` for the per-model lookup (relative paths
    cannot escape via ``..``). The top-level SKILL lives one level above
    the Pack root (``factory/app_builder/SKILL.md``), which is a fixed,
    install-controlled location.

    Dual-anchor support (built-in + user Pack roots)
    -----------------------------------------------
    Since P4 the runtime tracks two Pack anchors (与
    ``FileSystemWeightsPresence`` 双 anchor 探测语义一致，
    State-Truth-First 铁律 1):

    * **built-in** — ``pack_root`` (``<repo_root>/factory/app_builder/models``);
    * **user-imported** — ``user_pack_root``
      (``<data_dir>/app_builder/user_models``).

    A given Pack physically lives in **exactly one** of the two anchors
    (磁盘即真值). ``pack_skill_path()`` probes built-in first, then user;
    the first anchor that holds the file wins. ``user_pack_root`` defaults
    to ``None`` so existing test fixtures / lean containers（只有内置根）
    keep working。The top-level SKILL 只挂在 built-in 侧（``factory/app_builder/
    SKILL.md``），因为它是发行契约位置，不随用户导入变动。
    """

    __slots__ = ("_pack_root", "_user_pack_root", "_top_level_skill")

    def __init__(
        self,
        *,
        pack_root: Path,
        user_pack_root: Path | None = None,
    ) -> None:
        if not isinstance(pack_root, Path):
            raise TypeError("pack_root must be a Path")
        if user_pack_root is not None and not isinstance(user_pack_root, Path):
            raise TypeError("user_pack_root must be a Path or None")
        self._pack_root = pack_root.resolve()
        self._user_pack_root = (
            user_pack_root.resolve() if user_pack_root is not None else None
        )
        # V1 top-level: factory/app_builder/SKILL.md == pack_root.parent.
        # 只挂 built-in 侧（发行契约位置）。
        self._top_level_skill = self._pack_root.parent / "SKILL.md"

    def top_level_skill_path(self) -> str | None:
        """Absolute path of the top-level SKILL.md, or ``None`` if absent."""
        target = self._top_level_skill
        if target.is_file():
            return str(target)
        return None

    @staticmethod
    def _resolve_under(root: Path, model_id: str, file_name: str) -> str | None:
        """Sandboxed lookup under a single anchor. ``None`` on miss / escape."""
        target = (root / model_id / file_name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        if not target.is_file():
            return None
        return str(target)

    def pack_skill_path(self, model_id: str, file_name: str) -> str | None:
        """Absolute path of ``<pack_root>/<model_id>/<file_name>``.

        Probes built-in first, then user_pack_root. Returns ``None`` when
        the path escapes any root or the file does not exist under either
        anchor.
        """
        if not model_id or not file_name:
            return None
        # Built-in anchor first (V1 layout — release-contracted packs).
        hit = self._resolve_under(self._pack_root, model_id, file_name)
        if hit is not None:
            return hit
        # User anchor (P4 — user-imported packs under data_dir).
        if self._user_pack_root is not None:
            return self._resolve_under(
                self._user_pack_root, model_id, file_name
            )
        return None
