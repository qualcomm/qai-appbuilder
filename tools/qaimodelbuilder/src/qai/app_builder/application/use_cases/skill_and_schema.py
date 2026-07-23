# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SKILL.md resolution + Schema-driven UI use cases.

Layered concerns surfaced together because they share Pack
manifest data:

* **SKILL.md file resolution** —
  :class:`ResolveSkillFilesUseCase` resolves the ordered SKILL.md
  file paths (top-level guide + selected Pack guide) to inline for
  an App Builder chat turn, gated by each Pack manifest's
  ``skill.enabled`` flag and file existence.
* **Schema-driven UI** —
  :class:`GetModelSchemaUseCase` returns just the input/output schema
  for a model (a lighter alternative to PR-304's full manifest route
  for clients that only need to render the form).

PR-305 does NOT:
* Introduce a real G2P asset downloader (handled by install / PR-306)
* Modify the ai_coding tool registry directly (cross-BC; goes via the
  ``apps/api/_app_builder_skill_bridge.py`` which the I1 lane wires)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qai.app_builder.application.ports import AppModelRepositoryPort
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.errors import AppModelNotFoundError
from qai.app_builder.domain.pack_layout_constants import (
    BUILTIN_PACK_REL,
    BUILTIN_WEIGHTS_REL,
    USER_PACK_REL,
    USER_WEIGHTS_MODELS_SUBDIR,
    USER_WEIGHTS_REL,
)
from qai.app_builder.domain.value_objects import AppModelId

if TYPE_CHECKING:  # pragma: no cover
    from qai.app_builder.application.model_status_view import (
        AppModelStatusInfo,
    )
    from qai.app_builder.application.use_cases.inject_quality_score import (
        InjectQualityScoreUseCase,
    )

__all__ = [
    "GeneratePackCatalogUseCase",
    "GetModelSchemaUseCase",
    "ModelSchema",
    "ModelInferenceCode",
    "ManifestProvider",
    "SkillFileLoader",
    "FilesystemSkillFileLoader",
    "SkillPathLocator",
    "ResolveSkillFilesUseCase",
    "ResolveModelInferenceCodeUseCase",
    "ModelStatusProvider",
]


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type aliases / Protocols
# ---------------------------------------------------------------------------
ManifestProvider = Callable[[AppModelId], "Any | None"]
"""Looks up a :class:`PackManifest` by model id; ``None`` if absent.

Same shape used by PR-304's ``GetPackManifestUseCase``; the lifespan
(I1) injects a single instance into the container.
"""

ModelStatusProvider = Callable[[AppModelDefinition], "AppModelStatusInfo"]
"""Resolves a model's install status + category badge.

Same callable wired in ``apps/api/_app_builder_di.py`` as
``app_model_status_resolver`` and consumed by the ``GET /models`` mapper.
:class:`GeneratePackCatalogUseCase` reuses it so the catalog prompt's
``[✓]`` / ``[⚠ NotInstalled]`` status mark and ``### {category}`` grouping
match the gallery exactly (V1 ``generate_pack_catalog_prompt`` read the
same registry-augmented ``status`` / ``category`` fields).
"""


@runtime_checkable
class SkillPathLocator(Protocol):
    """Resolves SKILL.md *file paths* (not bodies) for the chat prompt.

    Implemented by
    :class:`qai.app_builder.infrastructure.skill_paths.FilesystemSkillPathLocator`
    and wired by the DI root. Returns absolute, existing path strings;
    ``None`` when the file is absent (the use case skips it).
    """

    def top_level_skill_path(self) -> str | None:
        """Absolute path of the top-level App Builder SKILL.md, or ``None``."""
        ...

    def pack_skill_path(self, model_id: str, file_name: str) -> str | None:
        """Absolute path of a Pack's SKILL file, or ``None`` when absent."""
        ...


@runtime_checkable
class SkillFileLoader(Protocol):
    """Reads a Pack's SKILL.md content given the model id.

    Returns the raw text or ``None`` when the file doesn't exist.
    Implementations MUST not raise on missing files — only on hard IO
    errors (the use case treats those as ``None`` and continues).
    """

    def load(self, model_id: AppModelId, file_name: str) -> str | None:
        ...


class FilesystemSkillFileLoader:
    """Default :class:`SkillFileLoader` reading
    ``<pack_root>/<model_id>/<file_name>`` from disk.

    Bounded to ``pack_root`` (constructor arg) — relative paths cannot
    escape via ``..``. Encoding is UTF-8 with ``errors="replace"`` so
    a malformed SKILL.md never crashes the prompt builder.

    Dual-anchor support (built-in + user Pack roots)
    -----------------------------------------------
    Since P4 the runtime tracks two Pack anchors (与
    ``FileSystemWeightsPresence`` 双 anchor 探测语义一致，
    State-Truth-First 铁律 1):

    * **built-in** — ``pack_root`` (``<repo_root>/factory/chat_features/app-builder/models``);
    * **user-imported** — ``user_pack_root``
      (``<data_dir>/app_builder/user_models``).

    A given Pack physically lives in **exactly one** of the two anchors
    (磁盘即真值). ``load()`` probes built-in first, then user; the first
    anchor that holds a readable file wins. ``user_pack_root`` defaults to
    ``None`` so existing test fixtures / lean containers（只有内置根）
    keep working。
    """

    __slots__ = ("_pack_root", "_user_pack_root")

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

    def _read_under(self, root: Path, model_id: str, file_name: str) -> str | None:
        """Sandboxed read under a single anchor. ``None`` on miss / escape / IO."""
        target = (root / model_id / file_name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def load(self, model_id: AppModelId, file_name: str) -> str | None:
        mid = str(model_id)
        # Built-in anchor first (V1 layout — release-contracted packs).
        text = self._read_under(self._pack_root, mid, file_name)
        if text is not None:
            return text
        # User anchor (P4 — user-imported packs under data_dir).
        if self._user_pack_root is not None:
            return self._read_under(self._user_pack_root, mid, file_name)
        return None


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ModelInferenceCode:
    """One selected model's inference code *reference* (``runner.py`` path).

    Returned by :class:`ResolveModelInferenceCodeUseCase` for the App
    Builder chat prompt. Only the file *path* is surfaced — the Agent
    decides whether to ``read`` the full reference implementation while
    helping the user build a WebUI around the model. Runners are large
    (700-950 lines); injecting only the path keeps the system prompt
    small and lets the Agent pull the code on demand.
    """

    model_id: str
    title: str
    code_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id must be non-empty str")
        if not isinstance(self.title, str):
            raise ValueError("title must be str")
        if not isinstance(self.code_path, str) or not self.code_path:
            raise ValueError("code_path must be non-empty str")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSchema:
    """Lightweight schema-only view of an :class:`AppModelDefinition`.

    Returned by :class:`GetModelSchemaUseCase` for the schema-driven UI
    flow. Carries just the input + output schema so the frontend can
    render the form without pulling the full manifest.

    The ``input_schema`` / ``output_schema`` mappings are dict views
    of the corresponding :class:`PackInputSchema` / :class:`PackOutputSchema`
    — keep the tuple-of-tuples internal representation hidden from the
    transport layer.
    """

    model_id: str
    title: str
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    variants: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("model_id must be non-empty str")
        if not isinstance(self.title, str):
            raise ValueError("title must be str")
        if self.input_schema is not None and not isinstance(
            self.input_schema, dict
        ):
            raise ValueError("input_schema must be dict or None")
        if self.output_schema is not None and not isinstance(
            self.output_schema, dict
        ):
            raise ValueError("output_schema must be dict or None")
        if not isinstance(self.variants, tuple):
            raise ValueError("variants must be tuple")


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------
class ResolveSkillFilesUseCase:
    """Resolve SKILL.md file paths to inline for an App Builder chat turn.

    V1 parity (``backend/app_builder/skill_resolver.resolve_skill_files``):
    returns an ordered tuple of absolute, existing SKILL.md path strings:

    1. the top-level App Builder SKILL (``factory/chat_features/app-builder/SKILL.md``),
       injected unconditionally when present;
    2. the SKILL of the currently selected Pack
       (``tool_params["selected_model_id"]``), gated by the Pack manifest's
       ``skill.enabled`` flag and the file's existence — only that one
       Pack's SKILL is injected, never the whole gallery's.

    Order is sensitive (top-level first) so the chat prompt builder
    concatenates "top-level guide + selected-model guide" in the same
    order V1 did. Missing files are silently skipped.

    Multi-model support: in addition to the legacy single
    ``tool_params["selected_model_id"]`` (str), the use case also reads
    ``tool_params["selected_model_ids"]`` (list[str]) so the user can
    select more than one imported model. The two sources are unioned and
    deduped preserving first-seen order (the single-id, when present, is
    appended first for byte-for-byte backward compatibility, then any
    list ids not already seen). Each pack's SKILL is still gated by the
    manifest ``skill.enabled`` flag + file existence; the top-level SKILL
    remains first.
    """

    _SELECTED_MODEL_KEY = "selected_model_id"
    _SELECTED_MODELS_KEY = "selected_model_ids"

    def __init__(
        self,
        *,
        locator: SkillPathLocator,
        manifest_provider: ManifestProvider,
    ) -> None:
        self._locator = locator
        self._manifest_provider = manifest_provider

    def execute(
        self, tool_params: dict[str, Any] | None
    ) -> tuple[str, ...]:
        files: list[str] = []
        top = self._locator.top_level_skill_path()
        if top is not None:
            files.append(top)

        for model_id in self._selected_model_ids(tool_params):
            pack_path = self._resolve_pack_skill(model_id)
            if pack_path is not None:
                files.append(pack_path)
        return tuple(files)

    def _selected_model_ids(
        self, tool_params: dict[str, Any] | None
    ) -> tuple[str, ...]:
        """Union of the single + list selected-model keys, order-preserving.

        Reads the legacy ``selected_model_id`` (str) first — preserving
        the exact single-model ordering existing callers relied on — then
        appends every entry of ``selected_model_ids`` (list[str]) not
        already seen. Non-string / blank entries and malformed containers
        are ignored so a bad ``tool_params`` never breaks the prompt.
        """
        if not isinstance(tool_params, dict):
            return ()
        seen: set[str] = set()
        ordered: list[str] = []

        def _add(value: Any) -> None:
            if isinstance(value, str):
                v = value.strip()
                if v and v not in seen:
                    seen.add(v)
                    ordered.append(v)

        _add(tool_params.get(self._SELECTED_MODEL_KEY))
        raw_list = tool_params.get(self._SELECTED_MODELS_KEY)
        if isinstance(raw_list, (list, tuple)):
            for item in raw_list:
                _add(item)
        return tuple(ordered)

    def _resolve_pack_skill(self, model_id: str) -> str | None:
        try:
            manifest = self._manifest_provider(AppModelId(value=model_id))
        except Exception:  # noqa: BLE001 — bad id never breaks the prompt
            return None
        if manifest is None:
            return None
        skill = getattr(manifest, "skill", None)
        if skill is None or not getattr(skill, "enabled", False):
            return None
        file_name = getattr(skill, "file", "SKILL.md") or "SKILL.md"
        return self._locator.pack_skill_path(model_id, file_name)


class ResolveModelInferenceCodeUseCase:
    """Resolve selected models' inference code (``runner.py``) for the chat.

    Companion to :class:`ResolveSkillFilesUseCase`: for each selected
    App Builder model (from ``tool_params["selected_model_id"]`` and/or
    ``tool_params["selected_model_ids"]``, unioned + deduped preserving
    order the same way), it returns the model's reference inference code
    so the chat Agent can help the user build a WebUI around the model.

    The runner file is located via the same :class:`SkillPathLocator` the
    SKILL resolver uses (``<pack_root>/<model_id>/<script>``); the script
    file name comes from ``manifest.runner.script`` when available, else
    the ``"runner.py"`` default. Only the *path* is returned (the file
    must exist) — the Agent decides whether to ``read`` the full code, so
    the system prompt stays small regardless of runner size.

    Missing files are skipped silently (a half-installed Pack never
    breaks the prompt). Returns an empty tuple when no model is selected
    or none has an existing runner file.
    """

    _SELECTED_MODEL_KEY = "selected_model_id"
    _SELECTED_MODELS_KEY = "selected_model_ids"

    def __init__(
        self,
        *,
        locator: SkillPathLocator,
        manifest_provider: ManifestProvider,
        app_models: "AppModelRepositoryPort | None" = None,
    ) -> None:
        self._locator = locator
        self._manifest_provider = manifest_provider
        # Optional: used only to resolve a friendly title for each model.
        # ``None`` on stripped-down containers — the model id doubles as
        # the title then.
        self._app_models = app_models

    async def execute(
        self, tool_params: dict[str, Any] | None
    ) -> tuple[ModelInferenceCode, ...]:
        model_ids = self._selected_model_ids(tool_params)
        if not model_ids:
            return ()
        titles = await self._resolve_titles(model_ids)
        out: list[ModelInferenceCode] = []
        for model_id in model_ids:
            block = self._resolve_one(model_id, titles.get(model_id, ""))
            if block is not None:
                out.append(block)
        return tuple(out)

    def _resolve_one(
        self, model_id: str, title: str
    ) -> ModelInferenceCode | None:
        script = self._runner_script(model_id)
        code_path = self._locator.pack_skill_path(model_id, script)
        if code_path is None:
            return None
        # Only surface the path when the file actually exists — never point
        # the Agent at a non-existent runner (a half-installed Pack).
        if not Path(code_path).is_file():
            return None
        return ModelInferenceCode(
            model_id=model_id,
            title=title or model_id,
            code_path=code_path,
        )

    def _runner_script(self, model_id: str) -> str:
        try:
            manifest = self._manifest_provider(AppModelId(value=model_id))
        except Exception:  # noqa: BLE001 — bad id never breaks the prompt
            return "runner.py"
        if manifest is None:
            return "runner.py"
        runner = getattr(manifest, "runner", None)
        script = getattr(runner, "script", None)
        if isinstance(script, str) and script.strip():
            return script.strip()
        return "runner.py"

    async def _resolve_titles(
        self, model_ids: tuple[str, ...]
    ) -> dict[str, str]:
        if self._app_models is None:
            return {}
        try:
            models = await self._app_models.list_all()
        except Exception:  # noqa: BLE001 — titles are cosmetic
            return {}
        wanted = set(model_ids)
        titles: dict[str, str] = {}
        for m in models:
            mid = str(m.id)
            if mid in wanted:
                titles[mid] = getattr(m, "title", "") or ""
        return titles

    def _selected_model_ids(
        self, tool_params: dict[str, Any] | None
    ) -> tuple[str, ...]:
        """Same union/dedupe rule as :class:`ResolveSkillFilesUseCase`."""
        if not isinstance(tool_params, dict):
            return ()
        seen: set[str] = set()
        ordered: list[str] = []

        def _add(value: Any) -> None:
            if isinstance(value, str):
                v = value.strip()
                if v and v not in seen:
                    seen.add(v)
                    ordered.append(v)

        _add(tool_params.get(self._SELECTED_MODEL_KEY))
        raw_list = tool_params.get(self._SELECTED_MODELS_KEY)
        if isinstance(raw_list, (list, tuple)):
            for item in raw_list:
                _add(item)
        return tuple(ordered)


class GeneratePackCatalogUseCase:
    """Build the LLM-facing "可调用的本地 AI 模型" catalog block.

    Verbatim port of V1
    ``backend/app_builder/skill_resolver.generate_pack_catalog_prompt``:
    enumerates every enabled Pack grouped by category, with each model's
    I/O kinds, key params (first 6), declared metrics, historical rating
    summary and available variants, followed by the 6 usage rules. The
    output is injected into the App Builder chat system prompt so the LLM
    knows which local models it can drive via ``appbuilder_run``.

    Data sources (all already wired in DI):

    * model list + ``is_runnable`` gate — :class:`AppModelRepositoryPort`;
    * display name / description / I/O / params / metrics / variants —
      ``manifest_provider`` (:class:`PackManifest`);
    * status mark (``✓`` / ``⚠``) + category grouping —
      ``status_provider`` (same resolver the gallery uses), with a
      ``Ready`` / ``Other`` fallback when absent;
    * historical 👍/👎 + quality score + run count —
      :class:`InjectQualityScoreUseCase.summarize`.

    Returns ``""`` when no enabled Pack exists (parity with V1 returning
    an empty string so the prompt builder injects no catalog block).
    """

    _MAX_PACKS = 20

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        manifest_provider: ManifestProvider,
        status_provider: "ModelStatusProvider | None" = None,
        inject_quality_score_use_case: "InjectQualityScoreUseCase | None" = None,
        origin_by_id: "Mapping[str, str] | None" = None,
    ) -> None:
        """Args:
            origin_by_id: Optional mapping ``{model_id: "built-in" | "user"}``
                identifying which anchor pair a pack lives under. Sourced from
                :func:`_read_pack_manifests_union` at DI wire-up time. When
                present, ``_render_model`` emits a **"位置" (Location)** block
                per pack telling the LLM the correct ``pack_dir`` /
                ``model_dir`` layout to use when writing ``app.yaml`` and the
                right env-var names for runtime resolution. When ``None``,
                the location block is omitted (backward-compat / lean
                containers).

                This is the **primary通用 channel** for path-awareness — it
                covers 100% of packs (built-in and user-imported) regardless
                of whether ``skill.enabled`` is True or a SKILL.md file
                exists. Absolute paths are never emitted; only
                ``${APP_ROOT}``-anchored relative paths + env-var names,
                keeping generated App code portable across machines.
        """
        self._app_models = app_models
        self._manifest_provider = manifest_provider
        self._status_provider = status_provider
        self._inject_quality_score = inject_quality_score_use_case
        self._origin_by_id = origin_by_id or {}

    async def execute(self) -> str:
        models = await self._app_models.list_all()
        enabled = [m for m in models if m.is_runnable]
        if not enabled:
            return ""

        total = len(enabled)
        truncated = False
        if total > self._MAX_PACKS:
            ready = [m for m in enabled if self._status_of(m) == "Ready"]
            others = [m for m in enabled if self._status_of(m) != "Ready"]
            enabled = (ready + others)[: self._MAX_PACKS]
            truncated = True

        # Historical rating summaries (best-effort).
        summaries: dict[Any, Any] = {}
        if self._inject_quality_score is not None:
            try:
                summaries = await self._inject_quality_score.summarize(
                    [m.id for m in enabled]
                )
            except Exception:  # noqa: BLE001 — ratings must not break catalog
                summaries = {}

        lines: list[str] = [
            "## 可调用的本地 AI 模型（通过 `appbuilder_run` 工具）",
            "",
            "你可以使用 `appbuilder_run` 工具调用以下已安装的本地模型进行推理。",
            "每次调用需要指定 `modelId` 和 `inputs`，可选 `params`。",
            "",
        ]

        # Group by category, preserving first-seen order.
        by_category: dict[str, list[AppModelDefinition]] = {}
        for m in enabled:
            cat = self._category_of(m) or "Other"
            by_category.setdefault(cat, []).append(m)

        for cat, group in by_category.items():
            lines.append(f"### {cat}")
            lines.append("")
            for model in group:
                lines.extend(
                    self._render_model(model, summaries.get(model.id))
                )

        lines.extend(self._usage_rules())

        if truncated:
            # P4 双根：built-in packs 在 ``factory/chat_features/app-builder/models/``，
            # user-imported packs 在 ``data/app_builder/user_models/``。
            # 主推 API：让 LLM 通过 App Builder catalog 接口拿完整列表
            # （API 契约稳定，路径可能演进）。若必须直接扫盘，须同时覆盖
            # 两根，否则会漏用户导入的 pack——因此路径信息仅作 fallback
            # 括号补充，不作主句。
            #
            # 常量来源：``pack_layout_constants``（single source of truth；
            # 见该模块 docstring 里的"镜像点"说明）。
            lines.append(
                f"> 注：共有 {total} 个模型可用，此处仅展示前 "
                f"{self._MAX_PACKS} 个。完整清单请通过 App Builder catalog "
                "接口获取（若必须扫盘，pack 分布在两根："
                f"`{BUILTIN_PACK_REL}/`（内置）+ "
                f"`{USER_PACK_REL}/`（用户导入），两根都需覆盖）。"
            )
            lines.append("")

        return "\n".join(lines)

    # ── per-model rendering ────────────────────────────────────────────
    def _render_model(
        self, model: AppModelDefinition, summary: Any
    ) -> list[str]:
        mid = str(model.id)
        manifest = self._manifest_for(model)
        name = model.title
        desc = ""
        inp_kind = "?"
        out_kind = "?"
        if manifest is not None:
            name = manifest.display_name or model.title
            desc = manifest.description or ""
            if manifest.input_schema is not None:
                inp_kind = manifest.input_schema.kind
            if manifest.output_schema is not None:
                out_kind = manifest.output_schema.kind

        status = self._status_of(model)
        status_mark = "✓" if status == "Ready" else f"⚠ {status}"

        out: list[str] = [
            f"- **{name}** (`{mid}`) [{status_mark}]",
            f"  - 描述: {desc}",
            f"  - I/O: `{inp_kind}` → `{out_kind}`",
        ]

        params_info = self._render_params(manifest)
        if params_info:
            out.append(f"  - 参数: {'; '.join(params_info)}")

        perf = self._render_perf(manifest)
        if perf:
            out.append(f"  - 性能: {perf}")

        rating_line = self._render_rating(summary)
        if rating_line:
            out.append(rating_line)

        variants_line = self._render_variants(manifest)
        if variants_line:
            out.append(variants_line)

        location_lines = self._render_location(mid)
        if location_lines:
            out.extend(location_lines)

        out.append("")
        return out

    @staticmethod
    def _render_params(manifest: Any) -> list[str]:
        if manifest is None or not manifest.params:
            return []
        info: list[str] = []
        for p in manifest.params[:6]:
            p_name = p.name or "?"
            p_type = p.type or "?"
            p_default = p.default
            if p_type == "select":
                options = p.options or ()
                opts_str = "/".join(str(o) for o in options[:5])
                info.append(f"{p_name}=[{opts_str}] default={p_default}")
            elif p_type == "number":
                p_min = "" if p.min is None else p.min
                p_max = "" if p.max is None else p.max
                info.append(f"{p_name}({p_min}~{p_max}) default={p_default}")
            elif p_type == "boolean":
                info.append(f"{p_name}=bool default={p_default}")
            else:
                info.append(f"{p_name}={p_default or '?'}")
        return info

    @staticmethod
    def _render_perf(manifest: Any) -> str:
        if manifest is None:
            return ""
        metrics = manifest.metrics
        parts: list[str] = []
        latency = metrics.latency_ms
        memory = metrics.memory_mb
        if latency:
            parts.append(f"~{_fmt_num(latency)}ms")
        if memory:
            parts.append(f"~{_fmt_num(memory)}MB")
        return " | ".join(parts)

    @staticmethod
    def _render_rating(summary: Any) -> str:
        if summary is None:
            return ""
        if summary.rating_count > 0:
            q = summary.quality_score if summary.quality_score is not None else 0.0
            return (
                f"  - 历史评分: 👍 {summary.thumbs_up} / 👎 "
                f"{summary.thumbs_down} (质量分 {q:.2f}, 共 "
                f"{summary.rating_count} 次反馈)"
            )
        if summary.run_count > 0:
            return f"  - 已成功运行 {summary.run_count} 次（暂无用户评分）"
        return ""

    @staticmethod
    def _render_variants(manifest: Any) -> str:
        if manifest is None or not manifest.variants:
            return ""
        v_strs = [
            f"{v.id}({v.runtime.quantization or '?'})"
            for v in manifest.variants[:4]
        ]
        return f"  - 可用精度: {', '.join(v_strs)}"

    def _render_location(self, mid: str) -> list[str]:
        """Emit path metadata for one pack so the Agent knows its layout.

        Renders (only when ``self._origin_by_id`` has an entry for ``mid``)::

            - 位置: 内置 pack (V1 built-in layout)
              - Pack 目录: `${APP_ROOT}/factory/chat_features/app-builder/models/<id>/`
              - 权重目录: `${APP_ROOT}/models/<id>/`
              - app.yaml: `builtin: true`

        or for user-imported (P4) packs::

            - 位置: 用户导入 pack (P4 user layout)
              - Pack 目录: `${APP_ROOT}/data/app_builder/user_models/<id>/`
              - 权重目录: `${APP_ROOT}/data/app_builder/user_model_weights/models/<id>/`
              - app.yaml: `builtin: false`

        Only ``${APP_ROOT}``-anchored relative paths are emitted — absolute
        paths never leak into the LLM prompt, so generated App code stays
        portable across machines (host UI, manual ``run.bat``, and
        packaged-zip-on-foreign-machine all resolve via the same
        ``_resolve_dir`` 4-tier logic).

        Silently returns ``[]`` when the pack's origin is unknown (lean
        container, pack seeded before the manifest union was wired, etc.) —
        Agent falls back to inspecting ``manifest.json`` location manually.
        """
        origin = self._origin_by_id.get(mid)
        if origin == "built-in":
            return [
                "  - 位置: 内置 pack (V1 built-in layout)",
                f"    - Pack 目录: `${{APP_ROOT}}/{BUILTIN_PACK_REL}/{mid}/`",
                f"    - 权重目录: `${{APP_ROOT}}/{BUILTIN_WEIGHTS_REL}/{mid}/`",
                "    - app.yaml: `builtin: true` + 上述两条 `${APP_ROOT}/...` 路径",
            ]
        if origin == "user":
            return [
                "  - 位置: 用户导入 pack (P4 user layout)",
                f"    - Pack 目录: `${{APP_ROOT}}/{USER_PACK_REL}/{mid}/`",
                f"    - 权重目录: `${{APP_ROOT}}/{USER_WEIGHTS_REL}/{USER_WEIGHTS_MODELS_SUBDIR}/{mid}/`",
                "    - app.yaml: `builtin: false` + 上述两条 `${APP_ROOT}/...` 路径",
                f"    - 运行时: `${{APP_BUILDER_USER_MODEL_ROOT}}/{USER_WEIGHTS_MODELS_SUBDIR}/<id>/`"
                f"（注意多一层 `{USER_WEIGHTS_MODELS_SUBDIR}/`）+ `${{APP_BUILDER_USER_PACK_ROOT}}/<id>/`",
            ]
        # origin unknown — omit the block, let the Agent inspect manifest.json itself.
        # Only log when origin was explicitly set to a non-standard value (future
        # extension leak, or a bad DI wire); ``None`` is the normal lean-container
        # / not-wired case and stays silent to avoid log noise.
        if origin is not None:
            _logger.debug(
                "app_builder.catalog.origin_unknown: model_id=%s origin=%r",
                mid,
                origin,
            )
        return []

    @staticmethod
    def _usage_rules() -> list[str]:
        return [
            "### 使用规则",
            "",
            "1. **输入路径**：必须是仓库内相对路径（如 "
            "`data/uploads/images/xxx.png`、`data/outputs/r-xxx.png`）"
            "或用户明确指定的绝对路径。",
            "2. **外部路径处理**：如果用户给出仓库外的路径（如 "
            "`C:\\test\\images`），先用 `glob` 或 `read` 工具确认文件存在，"
            "然后直接使用该路径（如文件确实存在）。",
            "3. **同类多模型选择**：",
            "   - 状态为 `Ready` 的优先于 `NotInstalled`",
            "   - 如果有历史推理记录和质量评分，选效果最好的",
            "   - 如果没有历史数据，根据模型描述和用户需求自行决策，并说明理由",
            "4. **批量处理**：一次只能跑一个模型推理（NPU 串行），"
            "需多次调用 `appbuilder_run`。",
            "5. **错误处理**：如果推理失败，分析错误信息后建议用户调整参数或换模型。",
            "6. **结果引用**：推理产出的文件路径（如 `data/outputs/r-xxx.png`）"
            "可直接作为下一步模型的输入。",
            "",
        ]

    # ── helpers ─────────────────────────────────────────────────────────
    def _manifest_for(self, model: AppModelDefinition) -> Any:
        try:
            return self._manifest_provider(model.id)
        except Exception:  # noqa: BLE001
            return None

    def _status_of(self, model: AppModelDefinition) -> str:
        info = self._status_info(model)
        if info is not None and getattr(info, "status", None):
            return info.status
        return "Ready"

    def _category_of(self, model: AppModelDefinition) -> str | None:
        info = self._status_info(model)
        if info is not None:
            return getattr(info, "category", None)
        return None

    def _status_info(self, model: AppModelDefinition) -> Any:
        if self._status_provider is None:
            return None
        try:
            return self._status_provider(model)
        except Exception:  # noqa: BLE001 — status probe must not break catalog
            return None


def _fmt_num(value: float) -> str:
    """Render a metric number without a trailing ``.0`` for whole values."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class GetModelSchemaUseCase:
    """Return the schema-only view of a single model.

    Lighter-weight than ``GetPackManifestUseCase`` (PR-304) — used by
    the schema-driven UI flow when the form just needs to know
    "what inputs / outputs does this model take?".

    Raises :class:`AppModelNotFoundError` when the model id is unknown.
    Returns a :class:`ModelSchema` with ``input_schema=None`` /
    ``output_schema=None`` when the manifest does not declare them
    (e.g. legacy minimal manifests) — that's a valid state, not an
    error.
    """

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        manifest_provider: ManifestProvider,
    ) -> None:
        self._app_models = app_models
        self._manifest_provider = manifest_provider

    async def execute(self, model_id: AppModelId) -> ModelSchema:
        model = await self._app_models.get(model_id)
        manifest = self._manifest_provider(model_id)
        input_schema_dict: dict[str, Any] | None = None
        output_schema_dict: dict[str, Any] | None = None
        variants: tuple[dict[str, Any], ...] = ()
        if manifest is not None:
            if manifest.input_schema is not None:
                input_schema_dict = {
                    "kind": manifest.input_schema.kind,
                    "constraints": manifest.input_schema.constraints_dict,
                }
            if manifest.output_schema is not None:
                output_schema_dict = {
                    "kind": manifest.output_schema.kind,
                    "constraints": manifest.output_schema.constraints_dict,
                    "jsonSchema": manifest.output_schema.json_schema_dict,
                }
            variants = tuple(
                {
                    "id": v.id,
                    "label": v.label,
                    "longLabel": v.long_label,
                    "default": v.default,
                }
                for v in manifest.variants
            )
        return ModelSchema(
            model_id=str(model.id),
            title=model.title,
            input_schema=input_schema_dict,
            output_schema=output_schema_dict,
            variants=variants,
        )


# Suppress unused-import warning.
_ = AppModelNotFoundError
