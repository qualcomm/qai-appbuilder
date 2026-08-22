# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``skill.policy.json`` disk loader (U-003a / 6-H10).

V1 truth source
---------------
``backend/security/skill_policy.py:288-369`` (``load_skill_policy``) read
each skill's ``skill.policy.json`` sidecar, validated the schema
(``version == 1`` + the five known keys), expanded ``${VAR}``
placeholders in the path lists, and registered the resulting capability
with the global ``PolicyCenter`` (``register_skill_capabilities`` at
``:386-437``). The V1 schema keys are::

    {
      "version": 1,
      "required_read":    [<glob>, ...],
      "required_write":   [<glob>, ...],
      "trusted_binaries": [<glob>, ...],
      "sha256_pins":      {<glob>: <64-hex>, ...}
    }

V2 mapping (Clean-Cutover)
--------------------------
* ``required_read``    → :attr:`SkillCapability.read_paths`
* ``required_write``   → :attr:`SkillCapability.write_paths`
* ``trusted_binaries`` → :attr:`SkillCapability.trusted_binaries`
* ``sha256_pins``      → :attr:`SkillCapability.sha256_pins`

V1 had no ``exec_paths`` key (trusted binaries carried the exec surface),
so this loader leaves ``exec_paths`` empty — matching V1 behaviour and the
existing ``factory/chat_features/**/skill.policy.json`` fixtures which omit it.

Placeholder expansion is delegated to the pure-domain
:func:`qai.security.domain.path_templates.expand_placeholders`; this
loader (an *infrastructure* module) is the layer allowed to read the
process environment, so it resolves the ``${VAR}`` bindings from
``os.environ`` / ``os.getcwd`` / ``tempfile.gettempdir`` / ``Path.home``
and passes them in. The domain never touches the environment itself.

Failure handling mirrors V1: every individual file is loaded inside a
``try/except`` so a malformed ``skill.policy.json`` is logged and skipped
rather than crashing boot.

Lifespan wiring (pending — main-agent domain)
---------------------------------------------
This module only provides :func:`load_all`; it does NOT register itself
into ``apps.api.lifespan``. Calling ``load_all(registry, project_root)``
at startup is left for the main agent to wire in lifespan (lifespan is
outside this sub-agent's file domain).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from qai.security.application.ports import SkillCapabilityRegistryPort
from qai.security.domain.path_templates import expand_placeholders
from qai.security.domain.skill_capability import SkillCapability

__all__ = [
    "SKILL_POLICY_FILENAME",
    "build_env_bindings",
    "load_all",
    "load_skill_policy_file",
]

_LOGGER = logging.getLogger("qai.security.skill_capability_loader")

SKILL_POLICY_FILENAME = "skill.policy.json"

# Schema (V1 ``skill_policy.py:228-234``): the recognised top-level keys.
_KNOWN_KEYS = frozenset(
    {
        "version",
        "required_read",
        "required_write",
        "trusted_binaries",
        "sha256_pins",
    }
)


def build_env_bindings(
    project_root: Path,
    *,
    workspace_root: str | None = None,
) -> dict[str, str]:
    """Resolve the ``${VAR}`` bindings for placeholder expansion.

    Infrastructure layer reads the environment here (the domain may not).
    ``PROJECT_ROOT`` is the supplied root rather than ``os.getcwd()`` so
    skill packs resolve relative to the actual install tree; ``TEMP`` /
    ``HOME`` / ``APPDATA`` mirror V1's ``skill_policy._expand_placeholders``
    bindings.

    ``WORKSPACE`` is the configurable model-builder workspace root. It is
    *not* derived from the environment — the apps/api caller injects the
    resolved value (``resolve_workspace_root``) via ``workspace_root``. When
    absent it falls back to the canonical default ``C:/WoS_AI`` (mirrors
    ``apps.api._workspace_resolver.DEFAULT_WORKSPACE_ROOT``), preserving
    backward-compatible behaviour for callers that do not yet inject it.
    """
    return {
        "PROJECT_ROOT": str(project_root),
        "TEMP": tempfile.gettempdir(),
        "HOME": str(Path.home()),
        "APPDATA": os.environ.get("APPDATA", ""),
        "WORKSPACE": workspace_root or "C:/WoS_AI",
    }


def _validate_str_list(
    value: object,
    key: str,
    source: Path,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    """Validate + placeholder-expand a list-of-strings field (V1 parity)."""
    if value is None:
        return ()
    if not isinstance(value, list):
        _LOGGER.warning(
            "skill_capability_loader: %s: %r must be a list (got %s); "
            "ignoring",
            source,
            key,
            type(value).__name__,
        )
        return ()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            _LOGGER.warning(
                "skill_capability_loader: %s: %r has a non-string / empty "
                "entry %r; skipping",
                source,
                key,
                item,
            )
            continue
        out.append(expand_placeholders(item.strip(), env))
    return tuple(out)


def _validate_sha256_pins(
    value: object,
    source: Path,
    env: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Validate + expand the ``sha256_pins`` object (V1 parity).

    Keys (paths/globs) are placeholder-expanded; values (digests) are
    not. Malformed entries are skipped with a warning.
    """
    if value is None:
        return ()
    if not isinstance(value, dict):
        _LOGGER.warning(
            "skill_capability_loader: %s: 'sha256_pins' must be an object "
            "(got %s); ignoring",
            source,
            type(value).__name__,
        )
        return ()
    pairs: list[tuple[str, str]] = []
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            _LOGGER.warning(
                "skill_capability_loader: %s: invalid sha256_pins key %r",
                source,
                k,
            )
            continue
        if not isinstance(v, str) or not v.strip():
            _LOGGER.warning(
                "skill_capability_loader: %s: invalid sha256_pins value "
                "for %r",
                source,
                k,
            )
            continue
        digest = v.strip().lower()
        if len(digest) != 64 or any(
            c not in "0123456789abcdef" for c in digest
        ):
            _LOGGER.warning(
                "skill_capability_loader: %s: sha256_pins[%r] is not a "
                "64-char hex digest; skipping",
                source,
                k,
            )
            continue
        pairs.append((expand_placeholders(k.strip(), env), digest))
    return tuple(pairs)


def load_skill_policy_file(
    policy_path: Path,
    env: Mapping[str, str],
    *,
    skill_name: str | None = None,
) -> SkillCapability | None:
    """Parse + validate one ``skill.policy.json`` into a SkillCapability.

    Returns ``None`` (logged, never raised) when the file is missing or
    malformed — V1's "skill loading must never crash the host" contract.
    ``skill_name`` defaults to the parent directory name.
    """
    try:
        path = Path(policy_path)
    except (TypeError, ValueError) as exc:
        _LOGGER.warning(
            "skill_capability_loader: invalid policy_path %r (%s)",
            policy_path,
            exc,
        )
        return None

    if not path.exists():
        _LOGGER.info(
            "skill_capability_loader: file not found, skipping: %s", path
        )
        return None

    try:
        # ``utf-8-sig`` tolerates a UTF-8 BOM (V1 parity); strict UTF-8
        # otherwise, consistent with AGENTS.md §3.10.
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        _LOGGER.warning(
            "skill_capability_loader: failed to parse %s: %s", path, exc
        )
        return None

    if not isinstance(raw, dict):
        _LOGGER.warning(
            "skill_capability_loader: %s: top-level value must be an "
            "object (got %s)",
            path,
            type(raw).__name__,
        )
        return None

    version = raw.get("version")
    if version != 1:
        _LOGGER.warning(
            "skill_capability_loader: %s: unsupported version=%r "
            "(expected 1)",
            path,
            version,
        )
        return None

    for key in raw:
        if key not in _KNOWN_KEYS:
            _LOGGER.warning(
                "skill_capability_loader: %s: unknown key %r ignored",
                path,
                key,
            )

    name = (skill_name or path.parent.name or "unnamed-skill").strip()

    try:
        capability = SkillCapability(
            capability_name=name,
            read_paths=_validate_str_list(
                raw.get("required_read"), "required_read", path, env
            ),
            write_paths=_validate_str_list(
                raw.get("required_write"), "required_write", path, env
            ),
            trusted_binaries=_validate_str_list(
                raw.get("trusted_binaries"), "trusted_binaries", path, env
            ),
            sha256_pins=_validate_sha256_pins(
                raw.get("sha256_pins"), path, env
            ),
        )
    except (ValueError, TypeError) as exc:
        _LOGGER.warning(
            "skill_capability_loader: validation failed for %s: %s",
            path,
            exc,
        )
        return None

    _LOGGER.info(
        "skill_capability_loader: loaded %s (read=%d write=%d "
        "binaries=%d pins=%d)",
        capability.capability_name,
        len(capability.read_paths),
        len(capability.write_paths),
        len(capability.trusted_binaries),
        len(capability.sha256_pins),
    )
    return capability


def _owning_skill_dir(policy_path: Path, scan_root: Path) -> Path:
    """Return the SKILL directory that owns ``policy_path``.

    The permission subject is a SKILL, never a file inside one. A skill is a
    FIRST-LEVEL child of a scan root — the same rule
    :meth:`qai.platform.skills.discovery.SkillDiscovery.scan` applies (it uses
    ``iterdir``, so ``app-builder/models/<id>/`` is explicitly not a skill; see
    ``apps/api/_user_prefs_di.py::_resolve_skills_dirs``). Reusing that rule is
    what keeps the Security > Skill panel and the Skills panel in agreement.

    A policy file nested deeper (``app-builder/models/ppocrv4/skill.policy.json``)
    therefore belongs to ``app-builder``, not to ``ppocrv4``.
    """
    try:
        rel = policy_path.parent.relative_to(scan_root)
    except ValueError:
        return policy_path.parent
    # ``rel == "."`` → the policy sits at the scan root itself.
    return scan_root if rel == Path(".") else scan_root / rel.parts[0]


def _merge_capabilities(
    base: SkillCapability, extra: SkillCapability
) -> SkillCapability:
    """Union two declarations of the SAME skill, preserving ``base`` identity.

    Order-preserving de-duplication so the merged surface is deterministic and
    a repeated path (the five model packs each re-declare ``data/uploads`` /
    ``data/outputs`` / ``${TEMP}``) collapses to one entry.

    Union is the correct operation: FileGuard allows by path prefix, so a
    sub-declaration could never have restricted anything its parent already
    granted. Verified on the shipped data — ``app-builder`` declares
    ``${PROJECT_ROOT}/features/app-builder``, which already contains every
    ``models/<id>`` path the nested files add, so merging grants ZERO new
    access. It only stops those files from masquerading as separate skills.
    """

    def _u(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in (*a, *b):
            seen.setdefault(item, None)
        return tuple(seen)

    return SkillCapability(
        capability_name=base.capability_name,
        read_paths=_u(base.read_paths, extra.read_paths),
        write_paths=_u(base.write_paths, extra.write_paths),
        exec_paths=_u(base.exec_paths, extra.exec_paths),
        trusted_binaries=_u(base.trusted_binaries, extra.trusted_binaries),
        # ``description`` is not a parsed key of ``skill.policy.json``
        # (``load_skill_policy_file`` never sets it), so both sides are always
        # "" in practice. Keep ``base``'s value rather than inventing a
        # precedence rule for a field this format does not carry.
        description=base.description,
        sha256_pins=tuple(
            dict.fromkeys((*base.sha256_pins, *extra.sha256_pins))
        ),
    )


async def load_all(
    registry: SkillCapabilityRegistryPort,
    project_root: Path,
    *,
    workspace_root: str | None = None,
) -> list[str]:
    """Scan ``factory/chat_features/**/skill.policy.json`` and register each SKILL.

    Walks the chat-feature skill packs for ``skill.policy.json`` sidecars, parses
    each into a :class:`SkillCapability`, and registers it with ``registry``.
    Malformed files are skipped (logged). Returns the list of skill names that
    registered successfully so callers can log the boot-time count.

    Granularity — one capability per SKILL
    --------------------------------------
    A policy file nested inside a skill does NOT become its own capability. The
    recursive walk previously registered one capability per ``skill.policy.json``,
    so the five App Builder model packs
    (``app-builder/models/{melotts-zh,ppocrv4,real-esrgan-x4plus,whisper-base,
    zipformer-zh}/``) each surfaced as a standalone "skill" — inflating the
    Security > Skill panel with entries the Skills panel (correctly) never showed,
    and making the permission subject a FILE rather than a skill.

    Now every declaration found under a skill is merged into that skill's single
    capability (see :func:`_owning_skill_dir` for the skill boundary, which is the
    same first-level rule ``SkillDiscovery`` uses, and
    :func:`_merge_capabilities` for the union). The merge grants no new access:
    the nested paths already sat under a parent path the owning skill declared.

    ``workspace_root`` is the configurable model-builder workspace root,
    forwarded to :func:`build_env_bindings` so any ``${WORKSPACE}`` token
    in a skill pack's path lists expands to the operator-configured root.
    When omitted it falls back to the canonical default (``C:/WoS_AI``),
    preserving backward-compatible behaviour.

    The function is best-effort: an individual register failure is
    logged and does not abort the remaining packs.
    """
    env = build_env_bindings(project_root, workspace_root=workspace_root)

    # Accumulate per SKILL first, then register once each, so a skill whose
    # declarations are spread over several files still yields ONE capability.
    #
    # The skill's OWN policy file (``<skill>/skill.policy.json``) is processed
    # FIRST so its identity and description win: ``rglob`` + ``sorted`` yields
    # ``app-builder/models/melotts-zh/...`` before ``app-builder/...``, which
    # would otherwise let a model pack's blurb become the skill's description.
    # Sorting by path DEPTH makes the owning skill's own file authoritative
    # instead of relying on alphabetical luck.
    merged: dict[str, SkillCapability] = {}
    order: list[str] = []
    scan_root = project_root / "factory" / "chat_features"
    seen_files: set[Path] = set()
    if scan_root.is_dir():
        policy_paths = sorted(
            scan_root.rglob(SKILL_POLICY_FILENAME),
            key=lambda p: (len(p.relative_to(scan_root).parts), str(p)),
        )
        for policy_path in policy_paths:
            resolved = policy_path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            capability = load_skill_policy_file(policy_path, env)
            if capability is None:
                continue
            skill_name = _owning_skill_dir(policy_path, scan_root).name
            existing = merged.get(skill_name)
            if existing is None:
                order.append(skill_name)
                # Re-key onto the OWNING skill. ``load_skill_policy_file``
                # derives the capability name from the file's own parent dir, so
                # a nested pack arrives named after itself ("ppocrv4") — which is
                # exactly how it used to masquerade as a standalone skill.
                merged[skill_name] = (
                    capability
                    if capability.capability_name == skill_name
                    else _merge_capabilities(
                        SkillCapability(capability_name=skill_name), capability
                    )
                )
            else:
                merged[skill_name] = _merge_capabilities(existing, capability)

    registered: list[str] = []
    for skill_name in order:
        try:
            await registry.register(skill_name, merged[skill_name])
        except Exception:  # noqa: BLE001 — one bad pack must not abort boot
            _LOGGER.warning(
                "skill_capability_loader: register failed for %s",
                skill_name,
                exc_info=True,
            )
            continue
        registered.append(skill_name)

    _LOGGER.info(
        "skill_capability_loader: registered %d skill capabilities",
        len(registered),
    )
    return registered
