# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat-side feature SKILL.md content provider (Batch D / D-1).

Bridges the on-disk ``features/<dir>/SKILL.md`` files into the
``RichSystemPromptBuilder`` so that when the user activates a
toolbar feature mode (``model-build`` / ``ppt`` / ``code`` / ...),
the corresponding SKILL.md content is injected into the system
prompt's feature-prompt section.

V1 parity
---------
Mirrors ``QAIModelBuilder_v1_pure/backend/feature_manager.py``
(``FeatureManager.get_feature_prompt``):

* ``TOOL_MODE_DIR_MAP`` translates the frontend ``activeToolMode``
  value (e.g. ``"model-build"``) to the ``features/`` subdirectory
  name (``"model-builder"``).
* ``get_feature_prompt(tool_mode)`` reads ``features/<dir>/SKILL.md``
  fresh each call (V1 hot-reload semantics — edit the file, next
  request sees the change with no server restart).
* Missing / unmapped / unreadable ⇒ returns ``None``.

Encoding
--------
SKILL.md files contain Chinese + Markdown.  Per AGENTS.md §3.10
(file encoding fastener) we always read with
``open(p, encoding="utf-8")``.  Encoding errors are logged and the
provider returns ``None`` rather than crashing the prompt build.

Caching
-------
Following the v1 ``feature_manager.py`` no-cache semantics we
re-read the file on every call.  A typical SKILL.md is ~70 KB
(``features/model-builder/SKILL.md`` = 70 038 bytes) and chat
turns are user-paced, so the cost is acceptable.  If profiling
ever flags it, a (path, mtime)-keyed lru_cache can be slotted in
without changing the public callable signature.

Architecture
------------
This module lives in ``apps/api/`` because it composes:

* the ``features/`` directory layout (a platform-level concept —
  installed alongside the app), and
* the ``qai.chat`` ``SystemPromptBuilderPort`` (a chat-context
  abstraction).

``qai.chat`` cannot reach into ``features/`` directly; the
provider is exposed as a plain ``Callable[[str], str | None]``
that ``RichSystemPromptBuilder`` accepts via constructor
injection.  This keeps the chat domain free of filesystem
coupling.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("qai.chat.feature_skill_provider")

__all__ = [
    "FeatureSkillProvider",
    "TOOL_MODE_DIR_MAP",
    "WORKSPACE_PLACEHOLDER",
    "APP_ROOT_PLACEHOLDER",
]

#: Placeholder token used in the on-disk SKILL.md / references for the
#: model-builder workspace root. Substituted with the configured root at
#: injection time (see :class:`FeatureSkillProvider`).
WORKSPACE_PLACEHOLDER = "${WORKSPACE}"

#: Placeholder token used in the on-disk SKILL.md for the application
#: install root (repo root). Substituted with the absolute repo-root path
#: at injection time so the agent invokes the bundled model-builder
#: scripts via an ABSOLUTE path (the exec CWD is the workspace, not the
#: repo root, so a bare ``factory\...`` relative path would not resolve).
APP_ROOT_PLACEHOLDER = "${APP_ROOT}"


# ── tool_mode → features/ subdir name (V1 parity) ─────────────────────────
# Mirrors v1 ``backend/feature_manager.py:36-42`` exactly.  Frontend tool
# mode strings are normalised to hyphen form by the frontend bridge
# (``normaliseDetectedToolMode``); the underscore aliases here are kept
# as defensive aliases in case a back-compat mode label ever leaks
# through.
TOOL_MODE_DIR_MAP: dict[str, str] = {
    # Model-build (alias forms — see system_prompt_builder
    # ``_MODEL_BUILD_TOOL_MODE_ALIASES`` for the canonical tuple).
    "model-build":  "model-builder",
    "model_build":  "model-builder",
    "model_builder": "model-builder",
    # Model Hub (promoted former ``aihub-model-run`` skill) — download
    # pre-built AI Hub packages + export to App Builder. Dir name == tool_mode.
    "model-hub":    "model-hub",
    "model_hub":    "model-hub",
    # Other v1 features (registered for parity; will become useful
    # when batches E+ port them).
    "app-builder":  "app-builder",
    "ppt":          "ppt-gen",
    "ppt_gen":      "ppt-gen",
    "code":         "code-assist",
    "code_assist":  "code-assist",
    "translate":    "translate",
}


class FeatureSkillProvider:
    """Callable that returns the SKILL.md body for a ``tool_mode``.

    Parameters
    ----------
    features_dir:
        Absolute path to the chat-feature skill directory.  Typically
        ``<repo_root>/factory/chat_features``.
    workspace_root:
        The configured model-builder workspace root (e.g. ``C:/WoS_AI``
        or a user override). The on-disk SKILL.md / references use the
        ``${WORKSPACE}`` placeholder; this gate substitutes it with the
        actual configured path before the body reaches the system prompt
        so the agent is instructed to write artifacts under the *real*
        workspace (not the literal token, and not the repo root). When
        ``None`` the placeholder is left verbatim (defensive — the agent
        still sees a clearly-templated token rather than a wrong path).
    app_root:
        Absolute application install root (repo root). Substituted for the
        ``${APP_ROOT}`` placeholder so the agent invokes the bundled
        model-builder scripts (under ``factory/chat_features/...``) by
        ABSOLUTE path — the tool CWD is the workspace, not the repo root,
        so a bare relative ``factory\\...`` path would not resolve. When
        ``None`` it is inferred from ``features_dir`` (its parent's parent,
        i.e. ``<repo_root>/factory/chat_features`` → ``<repo_root>``).
    tool_mode_dir_map:
        Optional override of the tool_mode → subdir mapping.
        Defaults to :data:`TOOL_MODE_DIR_MAP`.
    """

    __slots__ = ("_features_dir", "_map", "_workspace_root", "_app_root")

    def __init__(
        self,
        *,
        features_dir: Path,
        workspace_root: str | None = None,
        app_root: str | None = None,
        tool_mode_dir_map: dict[str, str] | None = None,
    ) -> None:
        self._features_dir = features_dir
        self._workspace_root = (workspace_root or "").strip() or None
        # ``app_root`` defaults to the repo root inferred from ``features_dir``.
        # ``features_dir`` is ``<repo_root>/factory/chat_features`` (two levels
        # under the repo root), so the repo root is ``features_dir.parent.parent``
        # — NOT ``.parent`` (that would be ``<repo_root>/factory`` and produce a
        # doubled ``${APP_ROOT}/factory/...`` → ``<repo_root>/factory/factory/...``
        # path). Callers (``_chat_di``) pass ``app_root`` explicitly; this default
        # only guards stand-alone / test construction.
        _app = (app_root or "").strip()
        self._app_root = _app or str(features_dir.parent.parent)
        self._map = tool_mode_dir_map if tool_mode_dir_map is not None else TOOL_MODE_DIR_MAP

    def __call__(self, tool_mode: str | None) -> str | None:
        """Return the SKILL.md content for ``tool_mode``, or ``None``.

        Returns ``None`` when:

        * ``tool_mode`` is falsy or not in the dir map;
        * the resolved ``features/<dir>/SKILL.md`` file does not exist;
        * the file fails to read (OS error, encoding error).

        Each call re-reads the file from disk — matches v1
        ``FeatureManager.get_feature_prompt`` hot-reload semantics so
        editing a SKILL.md takes effect on the next request without
        a server restart.
        """
        if not tool_mode:
            return None
        dir_name = self._map.get(tool_mode)
        if not dir_name:
            return None
        # dir_name may be an absolute path (for skill packs that live outside
        # _features_dir, e.g. factory/app_builder).  Fall back to the
        # relative-to-_features_dir lookup for all other entries.
        _dir = Path(dir_name)
        skill_path = _dir / "SKILL.md" if _dir.is_absolute() else self._features_dir / dir_name / "SKILL.md"
        if not skill_path.is_file():
            _log.debug(
                "feature SKILL.md not found for tool_mode=%r at %s",
                tool_mode, skill_path,
            )
            return None
        try:
            content = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning(
                "feature SKILL.md read failed for tool_mode=%r at %s: %s",
                tool_mode, skill_path, exc,
            )
            return None
        if not content.strip():
            return None
        # Substitute the ``${WORKSPACE}`` placeholder with the configured
        # model-builder workspace root so the agent is directed to write
        # artifacts under the real workspace dir (single source of truth;
        # prevents the SKILL's directory convention from being a no-op
        # that leaves artifacts in the process CWD == repo root).
        if self._workspace_root and WORKSPACE_PLACEHOLDER in content:
            content = content.replace(
                WORKSPACE_PLACEHOLDER, self._workspace_root
            )
        # Substitute ``${APP_ROOT}`` with the absolute repo-root path so the
        # agent invokes the bundled scripts by absolute path (exec CWD is the
        # workspace, not the repo root).
        if APP_ROOT_PLACEHOLDER in content:
            content = content.replace(APP_ROOT_PLACEHOLDER, self._app_root)
        _log.debug(
            "loaded feature SKILL for tool_mode=%r from %s (%d chars)",
            tool_mode, skill_path, len(content),
        )
        return content
