# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context skill-registry bridge (PR-046).

Anti-corruption layer that adapts a *model_catalog*-style skill
registry (interface :class:`qai.model_catalog.application.ports.SkillRegistryPort`)
into the *ai_coding*-style skill registry (interface
:class:`qai.ai_coding.application.ports.SkillRegistryPort`).

The two contexts use intentionally different :class:`Skill`
representations:

* model_catalog stores ``(SkillName, version, enabled, manifest)``
  records — versioned and toggleable;
* ai_coding stores ``(name, description, spec)`` records — no
  security coupling, no version metadata (see
  ``src/qai/ai_coding/domain/entities.py`` ``Skill`` docstring).

This bridge lives in ``apps/api/`` because cross-context import is
forbidden by the ``context-isolation`` import-linter contract.
``apps/api/`` is the only layer permitted to compose two contexts;
the bridge ensures neither side acquires a hard dependency on the
other's domain.

Strategy: duck typing
---------------------
The bridge accepts any object that exposes the model_catalog port
shape (``list_skills`` / ``get`` / ``upsert``).  This lets the
``apps/api/_ai_coding_di.py`` wiring inject:

* the current S3 ``_FakeSkillRegistry`` from ``apps/api/di.py``
  (PR-044 will replace it with a real
  ``SqliteModelSkillRegistry``); or
* the future ``SqliteModelSkillRegistry`` from PR-044 directly.

No type-checked import of ``qai.model_catalog.*`` is performed —
the bridge re-validates the duck-typed input at construction so
mis-wirings fail fast.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from qai.ai_coding.domain import Skill, SkillNotRegisteredError
from qai.chat.application.ports import AppBuilderModelCode

__all__ = [
    "ModelCatalogSkillRegistrySource",
    "SkillRegistryBridge",
    "AppBuilderSkillCatalogAdapter",
]


@runtime_checkable
class ModelCatalogSkillRegistrySource(Protocol):
    """Duck-typed source covering the model_catalog skill registry port.

    The bridge calls only the three methods listed below; any object
    implementing them — including the current S3 fake in
    ``apps/api/di.py`` — is a valid input.
    """

    async def list_skills(self) -> list[Any]:  # SkillDefinition (model_catalog)
        ...

    async def get(self, skill: Any) -> Any | None:  # SkillName -> SkillDefinition?
        ...

    async def upsert(self, skill: Any) -> None:  # SkillDefinition (model_catalog)
        ...


class SkillRegistryBridge:
    """Adapt a model_catalog-style registry to the ai_coding port.

    The bridge maps:

    * ai_coding ``register(skill)`` → model_catalog ``upsert(...)``
      where the model_catalog skill is constructed lazily so this
      module imports nothing from the model_catalog context;
    * ai_coding ``list_skills()`` → model_catalog ``list_skills()``
      with a row-level mapping that drops ``version`` (ai_coding's
      ``Skill`` does not track it);
    * ai_coding ``get(name)`` → model_catalog ``get(SkillName(name))``
      with the same mapping; missing rows surface as
      :class:`SkillNotRegisteredError`.

    Construction time
    -----------------
    The bridge captures the lazy importer for
    ``model_catalog.domain`` so wiring stays under the apps layer
    without a top-level import.  This keeps the import-linter
    ``context-isolation`` contract clean for ``qai.ai_coding`` /
    ``qai.security`` while documenting where the cross-context join
    happens.
    """

    __slots__ = ("_name_factory", "_skill_factory", "_source")

    def __init__(
        self,
        *,
        source: ModelCatalogSkillRegistrySource,
        skill_definition_factory: Any | None = None,
        skill_name_factory: Any | None = None,
    ) -> None:
        if not isinstance(source, ModelCatalogSkillRegistrySource):
            raise TypeError(
                "source must implement model_catalog skill registry port "
                "(list_skills / get / upsert)"
            )
        self._source = source
        # Lazy imports so the bridge doesn't drag model_catalog into
        # every test that constructs ai_coding services.
        self._skill_factory = skill_definition_factory
        self._name_factory = skill_name_factory

    async def register(self, skill: Skill) -> None:
        skill_definition_cls, skill_name_cls = self._resolve_factories()
        # ai_coding ``Skill.spec`` is a loose dict (no schema), but
        # model_catalog ``SkillDefinition.version`` requires a string.
        # Coerce defensively so callers passing ``{"version": 1}``
        # (PR-046 wire-shape parity) continue to work after the
        # PR-103 bridge activation.
        raw_version: object = "1.0.0"
        if isinstance(skill.spec, dict):
            raw_version = skill.spec.get("version", "1.0.0")
        version = str(raw_version) if raw_version is not None else "1.0.0"
        definition = skill_definition_cls(
            skill_name=skill_name_cls(value=skill.name),
            version=version,
            enabled=True,
            manifest=self._build_manifest(skill),
        )
        await self._source.upsert(definition)

    async def list_skills(self) -> list[Skill]:
        rows = await self._source.list_skills()
        return [self._row_to_skill(r) for r in rows]

    async def get(self, name: str) -> Skill:
        _, skill_name_cls = self._resolve_factories()
        row = await self._source.get(skill_name_cls(value=name))
        if row is None:
            raise SkillNotRegisteredError(
                message=f"skill {name} not registered",
                details={"skill_name": name},
            )
        return self._row_to_skill(row)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_factories(self) -> tuple[Any, Any]:
        if self._skill_factory is not None and self._name_factory is not None:
            return self._skill_factory, self._name_factory
        # Lazy import — only model_catalog *types*, not its adapters.
        # The import here is on the apps layer (legitimate composition)
        # and does not violate context-isolation (which targets
        # ``qai.<context>`` source files, not ``apps.api``).
        from qai.model_catalog.domain.entities import SkillDefinition
        from qai.model_catalog.domain.ids import SkillName

        if self._skill_factory is None:
            self._skill_factory = SkillDefinition
        if self._name_factory is None:
            self._name_factory = SkillName
        return self._skill_factory, self._name_factory

    @staticmethod
    def _build_manifest(skill: Skill) -> dict[str, Any]:
        manifest: dict[str, Any] = {"description": skill.description}
        spec = skill.spec
        if isinstance(spec, dict):
            for k, v in spec.items():
                if k == "version":
                    continue
                manifest[k] = v
        return manifest

    @staticmethod
    def _row_to_skill(row: Any) -> Skill:
        # Duck-type: tolerate any object with ``skill_name``,
        # ``manifest`` (dict-like), and optional ``version``.
        skill_name_attr = getattr(row, "skill_name", None)
        if skill_name_attr is None:
            raise TypeError(
                "model_catalog skill row must expose ``skill_name`` attribute"
            )
        name_value = getattr(skill_name_attr, "value", str(skill_name_attr))
        manifest = getattr(row, "manifest", {}) or {}
        if not isinstance(manifest, dict):
            manifest = {}
        description = str(manifest.get("description") or "")
        # Strip ``description`` from the spec we hand back; carry over
        # ``version`` so round-trips through the bridge are lossless.
        spec: dict[str, Any] = {
            k: v for k, v in manifest.items() if k != "description"
        }
        version = getattr(row, "version", None)
        if version is not None and "version" not in spec:
            spec["version"] = version
        return Skill(name=str(name_value), description=description, spec=spec)


# ---------------------------------------------------------------------------
# AppBuilderSkillCatalogAdapter (PR-091 / S9 audit H-4)
# ---------------------------------------------------------------------------
class AppBuilderSkillCatalogAdapter:
    """Adapt App Builder Pack metadata into the chat-side port.

    Implements
    :class:`qai.chat.application.ports.AppBuilderSkillCatalogPort` for
    chat's :class:`RichSystemPromptBuilder` so the system prompt can
    inject the App Builder Pack catalog without ``qai.chat`` ever
    importing ``qai.app_builder``.

    The adapter is constructed in ``apps/api/_chat_di.py`` with two
    optional callables (``resolver`` / ``catalog_provider``) wired in
    from the App Builder context.  When either callable is missing
    (App Builder context not enabled, e.g. minimal release), the
    adapter degrades to the empty-result behaviour, which the chat
    builder treats as "no SKILL injected" / "no Pack catalog block".

    The two methods mirror the legacy
    ``app_builder.skill_resolver.resolve_skill_files`` and
    ``generate_pack_catalog_prompt`` (see
    ``backend/chat_handler.py:3003-3053``).
    """

    __slots__ = ("_catalog_provider", "_code_provider", "_resolver")

    def __init__(
        self,
        *,
        resolver: Any | None = None,
        catalog_provider: Any | None = None,
        code_provider: Any | None = None,
    ) -> None:
        # ``resolver`` is a callable
        # ``(tool_mode, tool_params) -> Iterable[str | os.PathLike]``.
        # ``catalog_provider`` is a callable ``() -> str``.
        # ``code_provider`` is a callable
        # ``(tool_params) -> Iterable[AppBuilderModelCode]`` (sync or async).
        self._resolver = resolver
        self._catalog_provider = catalog_provider
        self._code_provider = code_provider

    async def resolve_skill_files(
        self,
        *,
        tool_mode: str,
        tool_params: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        if self._resolver is None:
            return ()
        try:
            res = self._resolver(tool_mode, tool_params)
            # Tolerate sync or async resolvers.
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            if not res:
                return ()
            return tuple(str(p) for p in res)
        except Exception:  # noqa: BLE001 — best-effort cross-context call
            return ()

    async def generate_pack_catalog_prompt(self) -> str:
        if self._catalog_provider is None:
            return ""
        try:
            res = self._catalog_provider()
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            return str(res) if res else ""
        except Exception:  # noqa: BLE001 — best-effort cross-context call
            return ""

    async def resolve_model_inference_code(
        self,
        *,
        tool_params: dict[str, Any] | None,
    ) -> tuple[AppBuilderModelCode, ...]:
        if self._code_provider is None:
            return ()
        try:
            res = self._code_provider(tool_params)
            if hasattr(res, "__await__"):
                res = await res  # type: ignore[assignment]
            if not res:
                return ()
            out: list[AppBuilderModelCode] = []
            for item in res:
                mapped = self._map_code_block(item)
                if mapped is not None:
                    out.append(mapped)
            return tuple(out)
        except Exception:  # noqa: BLE001 — best-effort cross-context call
            return ()

    @staticmethod
    def _map_code_block(item: Any) -> AppBuilderModelCode | None:
        """Map an App Builder ``ModelInferenceCode`` (duck-typed) into the
        chat-side :class:`AppBuilderModelCode`. Returns ``None`` on a
        malformed / incomplete item so one bad block never breaks the set.
        """
        try:
            model_id = getattr(item, "model_id", None)
            code_path = getattr(item, "code_path", None)
            if not (
                isinstance(model_id, str)
                and model_id
                and isinstance(code_path, str)
                and code_path
            ):
                return None
            title = getattr(item, "title", "") or ""
            return AppBuilderModelCode(
                model_id=model_id,
                title=str(title),
                code_path=code_path,
            )
        except Exception:  # noqa: BLE001
            return None
