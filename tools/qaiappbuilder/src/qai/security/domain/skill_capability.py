# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain types for skill capability declarations (PR-504).

A *skill capability* is the static contract a skill module declares
about which paths / executables it intends to read, write, or invoke.
Mirrors the legacy ``backend/security/skill_policy.py`` model where
each skill carries a JSON sidecar ``skill.policy.json`` listing its
intended IO surface; the security center used the declaration to
short-circuit prompts that asked the user to allow operations the
skill had already declared up front.

PR-504 introduces the data model + a registry port; the actual
``skill.policy.json`` loader is implemented in
:mod:`qai.security.infrastructure.skill_capability_loader` (a small
parser that turns ``dict`` payloads into :class:`SkillCapability`
instances; see PR-504 §3.1).

Aggregates / value objects defined here:

* :class:`SkillCapability` — value object: the per-skill declaration.
* :class:`SkillCapabilityViolation` — domain error: raised when a
  skill at runtime tries to access a path / binary outside its
  declared capability surface.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from qai.platform.errors import DomainError
from qai.platform.io_validator import (
    assert_max_length,
    assert_no_control_chars,
    assert_non_empty,
)

__all__ = [
    "SkillCapability",
    "SkillCapabilityViolation",
]


# ---------------------------------------------------------------------------
# SkillCapability
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SkillCapability:
    """Static IO contract a skill declares up front.

    Mirrors the legacy ``skill.policy.json`` payload (see
    ``backend/security/skill_policy.py:152-340``):

    * ``read_paths`` — paths the skill expects to read from
      (typically the workspace + a few cache directories).
    * ``write_paths`` — paths the skill expects to write to.
    * ``exec_paths`` — paths whose binaries the skill intends to
      invoke (subprocess targets).
    * ``trusted_binaries`` — exact filenames the skill is allowed to
      spawn even from PATH lookup (``"git.exe"`` / ``"python.exe"``).

    All four collections are stored as immutable tuples of strings.
    The empty tuple means "no capability declared in this category" —
    the security center treats it as an empty allowlist (deny by
    default) when evaluating runtime requests against the declaration.
    """

    capability_name: str
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    exec_paths: tuple[str, ...] = ()
    trusted_binaries: tuple[str, ...] = ()
    description: str = ""
    # PR-092 §2.1 C-10 / §17.5 #6 — supply-chain pinning. Each entry
    # is a ``(path, sha256_hex)`` pair where ``path`` matches an entry
    # of ``trusted_binaries`` (or an absolute path under ``exec_paths``)
    # and ``sha256_hex`` is the expected lower-case hex digest. Stored
    # as a tuple-of-pairs so the dataclass remains hashable. An empty
    # tuple disables pinning (legacy behaviour).
    sha256_pins: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        assert_non_empty(self.capability_name, name="capability_name")
        assert_max_length(
            self.capability_name, max_length=256, name="capability_name"
        )
        assert_no_control_chars(
            self.capability_name, name="capability_name"
        )
        if self.description:
            assert_max_length(
                self.description, max_length=2048, name="description"
            )
            assert_no_control_chars(
                self.description, name="description"
            )
        for label, items in (
            ("read_paths", self.read_paths),
            ("write_paths", self.write_paths),
            ("exec_paths", self.exec_paths),
            ("trusted_binaries", self.trusted_binaries),
        ):
            if not isinstance(items, tuple):
                raise TypeError(
                    f"{label} must be a tuple, got "
                    f"{type(items).__name__}"
                )
            for entry in items:
                if not isinstance(entry, str) or not entry:
                    raise ValueError(
                        f"{label} entries must be non-empty strings, "
                        f"got {entry!r}"
                    )
                assert_max_length(entry, max_length=4096, name=label)
                assert_no_control_chars(entry, name=label)
        # PR-092 — sha256_pins shape validation. Accept either a tuple
        # of ``(path, digest)`` pairs (canonical) or a Mapping that we
        # canonicalise into a sorted tuple. Storing the canonical form
        # keeps the dataclass hashable.
        raw_pins = self.sha256_pins
        pairs: list[tuple[str, str]] = []
        if isinstance(raw_pins, tuple):
            iterable = raw_pins
        elif hasattr(raw_pins, "items"):
            iterable = tuple(raw_pins.items())
        else:
            raise TypeError(
                "sha256_pins must be a tuple[tuple[str, str], ...] or a "
                f"Mapping[str, str], got {type(raw_pins).__name__}"
            )
        for entry in iterable:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], str)
            ):
                raise ValueError(
                    "sha256_pins entries must be (str, str) tuples, "
                    f"got {entry!r}"
                )
            key, value = entry
            if not key:
                raise ValueError(
                    f"sha256_pins keys must be non-empty strings, got {key!r}"
                )
            if len(value) != 64:
                raise ValueError(
                    "sha256_pins values must be 64-char hex SHA-256 "
                    f"digests, got {value!r}"
                )
            digest = value.lower()
            if any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(
                    "sha256_pins values must be lower-case hex; "
                    f"got {value!r}"
                )
            assert_max_length(key, max_length=4096, name="sha256_pins.key")
            assert_no_control_chars(key, name="sha256_pins.key")
            pairs.append((key, digest))
        canonical = tuple(sorted(pairs))
        # Re-bind the canonicalised tuple (frozen dataclass).
        object.__setattr__(self, "sha256_pins", canonical)

    def covers_read(self, path: str) -> bool:
        """Return True iff ``path`` lies inside one of ``read_paths``."""
        return self._covers(path, self.read_paths)

    def covers_write(self, path: str) -> bool:
        return self._covers(path, self.write_paths)

    def covers_exec(self, path: str) -> bool:
        return self._covers(path, self.exec_paths)

    @staticmethod
    def _covers(path: str, allowlist: tuple[str, ...]) -> bool:
        # PR-092 §2.1 C-10 / §17.5 #6 — fnmatch glob matcher with case
        # folding (legacy ``skill_policy.py:485-529`` behaviour). Patterns
        # like ``**/*.exe`` and ``C:/Windows/System32/cmd.exe`` both work;
        # forward / back slashes are unified before comparison.
        if not path:
            return False
        target = path.replace("\\", "/").lower()
        for pattern in allowlist:
            normalised = pattern.replace("\\", "/").lower()
            if fnmatch.fnmatchcase(target, normalised):
                return True
            # Prefix-style entries (no glob meta-characters) also
            # cover any path under that directory — preserves the
            # legacy ``startswith`` semantics for non-glob patterns.
            if not any(c in normalised for c in "*?["):
                stripped = normalised.rstrip("/")
                if target == stripped or target.startswith(stripped + "/"):
                    return True
        return False


# ---------------------------------------------------------------------------
# SkillCapabilityViolation
# ---------------------------------------------------------------------------
class SkillCapabilityViolation(DomainError):
    """Raised when runtime IO escapes a skill's declared capability.

    ``details`` carries:

    * ``"capability"`` — the skill capability name.
    * ``"requested"`` — the path / binary the skill tried to access.
    * ``"action"`` — ``"read"`` / ``"write"`` / ``"exec"``.
    """

    default_code = "security.skill_capability.violation"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(self.default_code, message, details=details)
