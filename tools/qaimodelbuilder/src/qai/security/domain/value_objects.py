# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the security bounded context.

All value objects are frozen, ``slots=True``, ``kw_only=True`` dataclasses.
Equality and hashing flow naturally from the dataclass machinery, which
gives us by-value semantics suitable for use as dict keys or set members.

The VOs in this module are deliberately small and self-contained; they
have no dependencies beyond ``qai.platform.io_validator`` for input
validation and the standard library.

Value objects defined here:

* :class:`PathPattern`  — a path-matching pattern (literal or glob).
* :class:`AceMask`      — a CRUD-style permission bitmask.
* :class:`RequestId`    — typed wrapper around an opaque request id string.
* :class:`Subject`      — who is requesting (user / preset / system).
* :class:`Resource`     — what is being acted on (path or logical name).
* :class:`PolicyAction` — enum-like constants for ``allow`` / ``deny``.
* :class:`PolicyScope`  — enum-like constants for ``user`` / ``preset`` / ``path``.
* :class:`GrantSource`  — enum-like constants for ``user`` / ``auto`` / ``preset``.
* :class:`RequestState` — state-machine for permission requests.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

from qai.platform.io_validator import (
    assert_max_length,
    assert_no_control_chars,
    assert_non_empty,
)

__all__ = [
    "AceMask",
    "AskQuotaWindow",
    "Channel",
    "GrantSource",
    "PathPattern",
    "PolicyAction",
    "PolicyMatchKind",
    "PolicyOp",
    "PolicyScope",
    "RequestId",
    "RequestState",
    "Resource",
    "Subject",
]


# ---------------------------------------------------------------------------
# Enum-like constants
# ---------------------------------------------------------------------------
class PolicyAction(str, Enum):
    """Outcome a policy rule prescribes for a matching subject/resource."""

    ALLOW = "allow"
    DENY = "deny"


class PolicyScope(str, Enum):
    """The dimension a policy rule keys on.

    * ``USER``   — applies to a specific subject
    * ``PRESET`` — applies to a named preset (e.g. ``"strict"``)
    * ``PATH``   — applies to any subject for matching paths
    """

    USER = "user"
    PRESET = "preset"
    PATH = "path"


class PolicyOp(str, Enum):
    """The filesystem / exec operation a policy rule constrains.

    Restores the V1 ``PolicyCenter`` 4-list taxonomy
    (``backend/security/policy.py`` ``read_allow`` / ``write_allow`` /
    ``exec_allow_cwd`` / ``exec_deny_patterns``) as an explicit per-rule
    dimension so :class:`Policy.evaluate` can match the rule against the
    *operation* the caller actually requested — not just the path glob.

    * ``READ``      — rule applies when the request needs read access
      (V1 ``read_allow``). A read grant is implied by a write grant in
      V1, so write rules also satisfy a read probe (see
      :meth:`covers_request`).
    * ``WRITE``     — rule applies to write / modify requests
      (V1 ``write_allow``).
    * ``EXEC``      — rule applies to command-execution requests where
      the working directory falls under the pattern (V1
      ``exec_allow_cwd``).
    * ``EXEC_DENY`` — rule is a hard deny matched as a **regular
      expression** against the full command string before any other
      rule (V1 ``exec_deny_patterns`` — the first gate). Carried with
      :attr:`PolicyMatchKind.REGEX`.
    * ``ANY``       — operation-agnostic rule (the default). Preserves
      backward compatibility: every rule persisted before this field
      existed loads as ``ANY`` and continues to match on path glob
      regardless of the requested operation, exactly as before.
    """

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    EXEC_DENY = "exec_deny"
    ANY = "any"

    def covers_request(self, *, read: bool, write: bool, execute: bool) -> bool:
        """Return True iff a rule with this op is relevant to the request.

        Mirrors the V1 list semantics where a *write* grant implies
        *read* (``write_allow`` ⊇ ``read_allow`` for the same path):

        * ``ANY``       — relevant to every request.
        * ``READ``      — relevant when the request needs read.
        * ``WRITE``     — relevant when the request needs write OR read
          (write implies read; V1 ``write_allow`` also satisfies a read
          probe on the same path).
        * ``EXEC``      — relevant when the request needs execute.
        * ``EXEC_DENY`` — relevant when the request needs execute (it is
          the exec hard-deny gate); evaluated with regex matching.
        """
        if self is PolicyOp.ANY:
            return True
        if self is PolicyOp.READ:
            return read
        if self is PolicyOp.WRITE:
            return write or read
        if self is PolicyOp.EXEC:
            return execute
        # EXEC_DENY
        return execute


class PolicyMatchKind(str, Enum):
    """How a :class:`PathPattern` matches its candidate string.

    * ``GLOB``  — :mod:`fnmatch` glob semantics (the default; every rule
      persisted before this field existed loads as ``GLOB``).
    * ``REGEX`` — a Python :mod:`re` regular expression matched with
      :func:`re.search` against the candidate string. Used by
      ``op=exec_deny`` rules (V1 ``exec_deny_patterns``) which are
      regexes, not globs.
    """

    GLOB = "glob"
    REGEX = "regex"


class GrantSource(str, Enum):
    """Who created a sandbox grant.

    * ``USER``   — explicit user authorisation
    * ``AUTO``   — auto-approval based on policy / heuristics
    * ``PRESET`` — installed by a built-in preset
    """

    USER = "user"
    AUTO = "auto"
    PRESET = "preset"


class RequestState(str, Enum):
    """State machine for ``PermissionRequest``."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# PathPattern
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PathPattern:
    """Path-matching pattern with literal or glob semantics.

    The pattern string is normalised at construction time:

    * leading/trailing whitespace is stripped (rejected as empty if blank);
    * forward and backward slashes are kept verbatim — adapters are
      responsible for normalising path separators if they need to.

    ``case_sensitive`` defaults to ``False`` because the predominant
    deployment target (Windows) has case-insensitive file paths; callers
    on POSIX systems may opt in to case sensitivity explicitly.

    ``match_kind`` selects the matching engine: ``GLOB`` (default —
    :mod:`fnmatch`) or ``REGEX`` (:func:`re.search`). REGEX patterns are
    used by ``op=exec_deny`` policy rules (V1 ``exec_deny_patterns``)
    which match the full command string as a regular expression. An
    invalid regex is rejected at construction time so a malformed deny
    pattern can never silently fail open at evaluation time.
    """

    pattern: str
    case_sensitive: bool = False
    match_kind: "PolicyMatchKind" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        assert_non_empty(self.pattern, name="pattern")
        assert_max_length(self.pattern, max_length=4096, name="pattern")
        assert_no_control_chars(self.pattern, name="pattern")
        # Default to GLOB without forcing every existing caller (and the
        # hundreds of frozen-dataclass constructions in tests) to pass
        # the new kwarg. ``frozen=True`` means we go through
        # ``object.__setattr__`` to install the default.
        if self.match_kind is None:
            object.__setattr__(self, "match_kind", PolicyMatchKind.GLOB)
        if self.match_kind is PolicyMatchKind.REGEX:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            try:
                re.compile(self.pattern, flags)
            except re.error as exc:
                raise ValueError(
                    f"invalid regex pattern {self.pattern!r}: {exc}"
                ) from exc

    def expand(self, env: "Mapping[str, str]") -> "PathPattern":
        """Return a copy with ``${VAR}`` placeholders expanded via ``env``.

        U-003b / 6-H11 — restores V1's per-skill placeholder expansion
        (``backend/security/skill_policy.py:77-106``). The actual
        substitution lives in the pure-domain
        :func:`qai.security.domain.path_templates.expand_placeholders`
        helper; ``env`` carries the bindings the *caller* resolved from
        the process environment (the security ``domain`` never reads
        ``os.environ`` itself — that would breach the domain-purity
        contract). When the pattern contains no recognised token the
        returned pattern is value-equal to ``self``.

        REGEX patterns are expanded too (the token charset never collides
        with regex metacharacters), so a deny pattern can reference
        ``${TEMP}`` and still compile after expansion.
        """
        from qai.security.domain.path_templates import expand_placeholders

        expanded = expand_placeholders(self.pattern, env)
        if expanded == self.pattern:
            return self
        return PathPattern(
            pattern=expanded,
            case_sensitive=self.case_sensitive,
            match_kind=self.match_kind,
        )

    def matches(
        self,
        path: str,
        *,
        env: "Mapping[str, str] | None" = None,
    ) -> bool:
        """Return True iff ``path`` matches this pattern.

        ``GLOB`` patterns use :mod:`fnmatch` glob semantics
        (``*`` / ``?`` / ``[seq]``); ``REGEX`` patterns use
        :func:`re.search`. Case folding is applied when
        ``case_sensitive`` is ``False`` for both kinds.

        U-003b / 6-H11 — when ``env`` is supplied and the pattern
        contains ``${VAR}`` placeholders, the pattern is expanded
        (:meth:`expand`) before matching. ``env=None`` (the default)
        keeps the pre-existing behaviour byte-for-byte, so every legacy
        caller is unaffected.
        """
        if not isinstance(path, str):
            raise TypeError(f"path must be str, got {type(path).__name__}")
        if not path:
            return False
        target = self if env is None else self.expand(env)
        if target.match_kind is PolicyMatchKind.REGEX:
            flags = 0 if target.case_sensitive else re.IGNORECASE
            return re.search(target.pattern, path, flags) is not None
        # Windows path normalisation (2026-07-13): both the stored pattern and
        # the incoming path may use any mix of forward/back slashes and may
        # contain repeated separators (e.g. ``C:\\Dump\\**``, ``C:/Dump/**``,
        # ``C:\Dump/direct_del_test.txt``).  Normalise BOTH to forward-slash
        # with no consecutive separators before fnmatch so that a rule written
        # in one style matches paths arriving in any other style.
        def _norm(s: str) -> str:
            s = s.replace("\\", "/")
            return re.sub(r"/+", "/", s)

        norm_path = _norm(path)
        norm_pattern = _norm(target.pattern)
        if target.case_sensitive:
            return fnmatch.fnmatchcase(norm_path, norm_pattern)
        return fnmatch.fnmatchcase(norm_path.lower(), norm_pattern.lower())


# ---------------------------------------------------------------------------
# AceMask
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AceMask:
    """CRUD-style permission bitmask.

    Mirrors the four canonical operations of a filesystem ACE entry.
    Each flag is a plain ``bool`` so the dataclass remains ergonomic to
    construct via keyword arguments (``AceMask(read=True, write=True)``).

    The class also exposes :meth:`from_bits` / :meth:`to_bits` for compact
    serialisation (4-bit value, R=1, W=2, E=4, D=8) — useful when adapters
    persist the mask to SQLite or compare it with platform-native ACEs.
    """

    read: bool = False
    write: bool = False
    execute: bool = False
    delete: bool = False

    _BIT_READ: ClassVar[int] = 1
    _BIT_WRITE: ClassVar[int] = 2
    _BIT_EXECUTE: ClassVar[int] = 4
    _BIT_DELETE: ClassVar[int] = 8

    def is_empty(self) -> bool:
        """Return True iff no permission bit is set."""
        return not (self.read or self.write or self.execute or self.delete)

    def to_bits(self) -> int:
        """Encode the mask as a 4-bit integer (R=1 W=2 E=4 D=8)."""
        return (
            (self._BIT_READ if self.read else 0)
            | (self._BIT_WRITE if self.write else 0)
            | (self._BIT_EXECUTE if self.execute else 0)
            | (self._BIT_DELETE if self.delete else 0)
        )

    @classmethod
    def from_bits(cls, bits: int) -> AceMask:
        """Decode a 4-bit integer produced by :meth:`to_bits`.

        Raises:
            ValueError: if ``bits`` is negative or has bits beyond the 4
                supported flags set.
        """
        if not isinstance(bits, int) or isinstance(bits, bool):
            raise TypeError(f"bits must be int, got {type(bits).__name__}")
        if bits < 0 or bits > 0b1111:
            raise ValueError(f"bits must be in [0, 15], got {bits!r}")
        return cls(
            read=bool(bits & cls._BIT_READ),
            write=bool(bits & cls._BIT_WRITE),
            execute=bool(bits & cls._BIT_EXECUTE),
            delete=bool(bits & cls._BIT_DELETE),
        )

    def union(self, other: AceMask) -> AceMask:
        """Return a new mask with all bits from ``self`` and ``other`` set."""
        if not isinstance(other, AceMask):
            raise TypeError(
                f"other must be AceMask, got {type(other).__name__}"
            )
        return AceMask(
            read=self.read or other.read,
            write=self.write or other.write,
            execute=self.execute or other.execute,
            delete=self.delete or other.delete,
        )

    def covers(self, other: AceMask) -> bool:
        """Return True iff every bit in ``other`` is also set in ``self``."""
        if not isinstance(other, AceMask):
            raise TypeError(
                f"other must be AceMask, got {type(other).__name__}"
            )
        return (self.to_bits() & other.to_bits()) == other.to_bits()


# ---------------------------------------------------------------------------
# RequestId / Subject / Resource
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class RequestId:
    """Typed wrapper around an opaque permission-request identifier.

    The string itself is generated by ``qai.platform.ids.IdGenerator``;
    we simply enforce that it is non-empty and printable.
    """

    value: str

    def __post_init__(self) -> None:
        assert_non_empty(self.value, name="value")
        assert_max_length(self.value, max_length=256, name="value")
        assert_no_control_chars(self.value, name="value")


@dataclass(frozen=True, slots=True, kw_only=True)
class Subject:
    """A principal making a security-relevant request.

    ``kind`` identifies the role (``"user"`` / ``"preset"`` / ``"system"``)
    and ``identifier`` is the role-scoped opaque identity string.
    """

    kind: str
    identifier: str

    _ALLOWED_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"user", "preset", "system"}
    )

    def __post_init__(self) -> None:
        assert_non_empty(self.kind, name="kind")
        if self.kind not in self._ALLOWED_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(self._ALLOWED_KINDS)!r}, "
                f"got {self.kind!r}"
            )
        assert_non_empty(self.identifier, name="identifier")
        assert_max_length(self.identifier, max_length=512, name="identifier")
        assert_no_control_chars(self.identifier, name="identifier")


@dataclass(frozen=True, slots=True, kw_only=True)
class Resource:
    """The thing a subject is acting on.

    ``kind`` is one of ``"path"`` / ``"skill"`` / ``"network"`` /
    ``"exec"`` / ``"dep"`` mirroring the legacy ``PolicyCenter`` taxonomy
    (see refactor-plan §5 / inventory §2).
    """

    kind: str
    identifier: str

    _ALLOWED_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"path", "skill", "network", "exec", "dep"}
    )

    def __post_init__(self) -> None:
        assert_non_empty(self.kind, name="kind")
        if self.kind not in self._ALLOWED_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(self._ALLOWED_KINDS)!r}, "
                f"got {self.kind!r}"
            )
        assert_non_empty(self.identifier, name="identifier")
        assert_max_length(self.identifier, max_length=4096, name="identifier")
        assert_no_control_chars(self.identifier, name="identifier")


# ---------------------------------------------------------------------------
# Channel — PR-501
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class Channel:
    """A delivery surface through which a permission ask is presented.

    Mirrors the legacy ``PolicyCenter._no_ui_channels`` taxonomy
    (``backend/security/policy.py:377-484``): the system asks the user
    interactively only when the originating channel actually owns a UI
    surface; for headless channels (``wechat``, ``feishu``,
    background workers) ASK is mapped to DENY synchronously to avoid
    deadlocks where no one can answer the prompt.

    ``name`` is a stable lower-case identifier (``web``, ``wechat``,
    ``feishu``, ``cli``, ``background``); ``requires_ui``
    encodes whether ASK can be resolved interactively. The ``well_known``
    classmethod returns the canonical Channel for a given name with
    ``requires_ui`` defaulting to ``True`` for ``web`` / ``cli`` and
    ``False`` for the chat / background channels.
    """

    name: str
    requires_ui: bool = True

    _ALLOWED_NAMES: ClassVar[frozenset[str]] = frozenset(
        {"web", "cli", "wechat", "feishu", "background"}
    )

    _NO_UI_DEFAULTS: ClassVar[frozenset[str]] = frozenset(
        {"wechat", "feishu", "background"}
    )

    def __post_init__(self) -> None:
        assert_non_empty(self.name, name="name")
        if self.name != self.name.lower():
            raise ValueError(
                f"channel name must be lower-case, got {self.name!r}"
            )
        if self.name not in self._ALLOWED_NAMES:
            raise ValueError(
                f"channel name must be one of {sorted(self._ALLOWED_NAMES)!r}, "
                f"got {self.name!r}"
            )

    @classmethod
    def well_known(cls, name: str) -> Channel:
        """Return the canonical Channel for ``name``.

        ``requires_ui`` is ``False`` for chat / background channels and
        ``True`` for ``web`` / ``cli``. Callers that want to override
        the default may construct ``Channel(name=..., requires_ui=...)``
        directly; the defaults are *only* a constructor convenience.
        """
        return cls(name=name, requires_ui=name not in cls._NO_UI_DEFAULTS)


# ---------------------------------------------------------------------------
# AskQuotaWindow — PR-501
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AskQuotaWindow:
    """Sliding-window ask-rate cap for a :class:`Channel`.

    Replaces the inline rate-limit check in the legacy
    ``PolicyCenter.ask_user`` (``backend/security/policy.py:1336-1530``)
    where each pending request was counted per channel and rejected with
    ``rate_limited`` once the cap was hit. The legacy implementation
    used a fixed window per channel; we model it as a small value object
    so the policy can carry the parameters explicitly.

    Invariants:

    * ``window_seconds`` must be a positive int (sliding window length);
    * ``max_asks`` must be a positive int (cap inside the window);
    * the empty / disabled state is represented by *not* attaching a
      window to the channel policy at all (``ChannelPolicy.quota`` is
      ``None``), rather than by a zero value here.
    """

    window_seconds: int
    max_asks: int

    def __post_init__(self) -> None:
        if not isinstance(self.window_seconds, int) or isinstance(
            self.window_seconds, bool
        ):
            raise TypeError(
                "window_seconds must be int, got "
                f"{type(self.window_seconds).__name__}"
            )
        if self.window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be > 0, got {self.window_seconds!r}"
            )
        if not isinstance(self.max_asks, int) or isinstance(
            self.max_asks, bool
        ):
            raise TypeError(
                f"max_asks must be int, got {type(self.max_asks).__name__}"
            )
        if self.max_asks <= 0:
            raise ValueError(
                f"max_asks must be > 0, got {self.max_asks!r}"
            )

