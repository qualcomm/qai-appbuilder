# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory :class:`SkillCapabilityRegistryPort` adapter (PR-504).

Single-process, dict-backed registry. The new flow re-registers
capabilities from skill bundles when the API container boots, so the
in-memory registry is the production fit for the single-worker default
deployment — no SQLite mirror is defined because boot-time registration
already produces an authoritative live view.

PR-092 §2.1 C-10 / §17.5 #6 — supply-chain pinning
---------------------------------------------------
When a :class:`SkillCapability` carries a non-empty
:attr:`SkillCapability.sha256_pins` mapping the registry verifies the
on-disk binary digest at register time and on every subsequent
:meth:`get` call (sample-cached once per process per binary path).
A digest mismatch raises :class:`SkillCapabilityViolation` so the
caller can surface an audit entry; the capability is **not** added
to the active map.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import threading
from pathlib import Path

from qai.platform.crypto.hashes import Hash256

from qai.security.adapters.path_normalizer import normalize_path
from qai.security.domain.skill_capability import (
    SkillCapability,
    SkillCapabilityViolation,
)

__all__ = ["InMemorySkillCapabilityRegistry"]


_LOGGER = logging.getLogger("qai.security.skill_capability_registry")


def _matches_any(target: str, patterns: tuple[str, ...]) -> bool:
    """fnmatch glob match of ``target`` against any of ``patterns``.

    Mirrors V1 ``skill_policy._glob_match`` /
    :meth:`SkillCapability._covers`: forward/back slashes are unified
    and matching is case-insensitive; ``**`` is collapsed to ``*`` so
    recursive-style skill globs (``C:/x/**/git.exe``) work with
    :mod:`fnmatch` (which already treats ``*`` as crossing separators).
    Non-glob prefix entries also cover any path beneath that directory
    (legacy ``startswith`` parity).
    """
    norm_target = target.replace("\\", "/").lower()
    for pattern in patterns:
        norm = pattern.replace("\\", "/").lower()
        collapsed = norm.replace("**/", "*/").replace("/**", "/*").replace(
            "**", "*"
        )
        if fnmatch.fnmatchcase(norm_target, collapsed):
            return True
        if not any(c in norm for c in "*?["):
            stripped = norm.rstrip("/")
            if norm_target == stripped or norm_target.startswith(
                stripped + "/"
            ):
                return True
    return False


def _hash_file(path: Path) -> str | None:
    """Return the lower-case hex SHA-256 of ``path`` or ``None`` on failure.

    Reads the file in 64 KiB chunks. Any OS error (missing file,
    permission denied) yields ``None`` so the caller can decide
    whether the absence is a violation.
    """

    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class InMemorySkillCapabilityRegistry:
    """Concrete :class:`SkillCapabilityRegistryPort` (in-process)."""

    __slots__ = ("_active", "_warnings", "_pin_cache", "_pin_lock")

    def __init__(self) -> None:
        self._active: dict[str, SkillCapability] = {}
        self._warnings: dict[str, tuple[str, ...]] = {}
        # Process-wide cache: normalised path string -> verified digest.
        self._pin_cache: dict[str, str] = {}
        self._pin_lock = threading.Lock()

    async def register(
        self,
        skill_name: str,
        capability: SkillCapability,
        *,
        scanner_warnings: tuple[str, ...] = (),
    ) -> None:
        if not skill_name or not isinstance(skill_name, str):
            raise ValueError(
                f"skill_name must be a non-empty str, got {skill_name!r}"
            )
        # PR-092 — verify all sha256_pins before admitting the capability.
        if capability.sha256_pins:
            self._verify_pins(skill_name, capability)
        self._active[skill_name] = capability
        self._warnings[skill_name] = tuple(scanner_warnings)

    async def unregister(self, skill_name: str) -> None:
        self._active.pop(skill_name, None)
        self._warnings.pop(skill_name, None)

    async def list_active(self) -> list[SkillCapability]:
        return list(self._active.values())

    async def get(self, skill_name: str) -> SkillCapability | None:
        capability = self._active.get(skill_name)
        if capability is None or not capability.sha256_pins:
            return capability
        # Re-verify, sample-cached: if every pinned path's digest is
        # already in ``_pin_cache`` we skip the rehash.
        try:
            self._verify_pins(skill_name, capability, allow_cached=True)
        except SkillCapabilityViolation:
            # Mismatch on use — drop the capability so the next
            # ``get`` call surfaces ``None`` rather than a stale entry.
            self._active.pop(skill_name, None)
            self._warnings.pop(skill_name, None)
            raise
        return capability

    async def find_trusted_binary_for(
        self, exe_path: str
    ) -> SkillCapability | None:
        """First active capability whose ``trusted_binaries`` glob matches.

        U-003c / 6-H12 — V2 successor to V1's global
        ``skill_policy.is_trusted_binary`` (``skill_policy.py:485-529``).
        Reuses :meth:`SkillCapability.covers_exec`'s fnmatch /
        slash-normalising / case-folding semantics by matching ``exe_path``
        against each capability's ``trusted_binaries`` patterns. Returns
        the owning capability (not a bare bool) so callers can attribute
        the trust to a skill; ``None`` when no active skill trusts it.

        Does NOT trigger sha256-pin re-verification (V1's trust query was
        a pure glob check; pin enforcement happens at register / get time).
        """
        if not exe_path or not isinstance(exe_path, str):
            return None
        for capability in self._active.values():
            if _matches_any(exe_path, capability.trusted_binaries):
                return capability
        return None

    async def list_all_trusted_binaries(self) -> list[str]:
        """De-duplicated union of every active capability's
        ``trusted_binaries`` (U-003c / 6-H12). First-seen-stable order.
        """
        seen: set[str] = set()
        out: list[str] = []
        for capability in self._active.values():
            for pattern in capability.trusted_binaries:
                if pattern not in seen:
                    seen.add(pattern)
                    out.append(pattern)
        return out

    def warnings_for(self, skill_name: str) -> tuple[str, ...]:
        """Test-affordance: return the warnings recorded at register time."""
        return self._warnings.get(skill_name, ())

    # ------------------------------------------------------------------
    # PR-092 helpers
    # ------------------------------------------------------------------
    def _verify_pins(
        self,
        skill_name: str,
        capability: SkillCapability,
        *,
        allow_cached: bool = False,
    ) -> None:
        for pinned_path, expected in capability.sha256_pins:
            absolute = normalize_path(pinned_path)
            key = str(absolute).lower()
            with self._pin_lock:
                cached = self._pin_cache.get(key)
            if allow_cached and cached is not None:
                if cached != expected:
                    raise SkillCapabilityViolation(
                        f"sha256 pin mismatch for {pinned_path!r} "
                        f"(skill={skill_name!r}, cached={cached}, "
                        f"expected={expected})",
                        details={
                            "capability": capability.capability_name,
                            "requested": pinned_path,
                            "action": "exec",
                        },
                    )
                continue
            actual = _hash_file(absolute)
            if actual is None:
                raise SkillCapabilityViolation(
                    f"sha256 pin verification failed: {pinned_path!r} "
                    f"is not readable (skill={skill_name!r})",
                    details={
                        "capability": capability.capability_name,
                        "requested": pinned_path,
                        "action": "exec",
                    },
                )
            # Construct Hash256 to assert canonical shape.
            digest = Hash256.of(actual).value
            if digest != expected:
                raise SkillCapabilityViolation(
                    f"sha256 pin mismatch for {pinned_path!r} "
                    f"(skill={skill_name!r}, actual={digest}, "
                    f"expected={expected})",
                    details={
                        "capability": capability.capability_name,
                        "requested": pinned_path,
                        "action": "exec",
                    },
                )
            with self._pin_lock:
                self._pin_cache[key] = digest
            _LOGGER.debug(
                "skill_capability_registry: pinned digest verified "
                "(skill=%s, path=%s)",
                skill_name,
                pinned_path,
            )
