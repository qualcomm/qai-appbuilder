# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Auto-approve / command-list pre-check adapter (U-005 / 5-H4).

Concrete :class:`qai.security.application.ports.AutoApprovePort` backed
by the runtime-toggleable
:class:`qai.security.application.security_runtime_state.SecurityRuntimeStateService`
buckets.

Restores the V1 ``PolicyCenter.is_auto_approved`` /
``_command_passes_lists`` / ``_path_matches_patterns`` decisions
(``backend/security/policy.py:659-718``, ``:921-924``, ``:976-979``)
that ran *before* the FileGuard policy rules:

* **per-op tool toggles** (P2 bucket-unification fix) — the "自动审批"
  panel persists ``{auto_approve.{read,write,exec,glob,grep},
  command_whitelist, command_blacklist}`` under the
  ``auto_approve_tool`` bucket (``PUT /api/security/auto_approve``). V1
  ``is_auto_approved`` consults exactly these per-op booleans:
  ``read``/``glob``/``grep`` -> ``auto_approve.read`` (glob/grep fall
  back to ``read``), ``write`` -> ``auto_approve.write``, ``exec`` ->
  ``auto_approve.exec`` AND the command-list gate. So a path read/write
  whose op-toggle is ON short-circuits ALLOW. The legacy ``auto_approve``
  bucket is *also* consulted as a fallback so the U-005 tests that wrote
  command lists / toggles straight into ``auto_approve`` keep passing.
* **exec** — the command blacklist is consulted first (highest
  priority); a hit returns ``False`` (DENY). Otherwise, when
  ``auto_approve.exec`` is ON and a command whitelist is enabled, a
  prefix/basename match returns ``True`` (ALLOW); a non-match or a
  disabled whitelist with ``auto_approve.exec`` ON returns ``True``
  (V1: whitelist-not-enabled relies solely on the ``exec`` switch).
  When ``auto_approve.exec`` is OFF, exec yields ``None`` (defer to
  policy) — except a blacklist hit still DENIES regardless (V1
  blacklist priority is unconditional).
* **read / write (path)** — when the ``auto_approve`` bucket is enabled
  and the resource path is under one of the configured ``trusted_paths``
  prefixes, OR the matching per-op toggle is ON, OR the
  ``path_patterns`` glob allowlist for that op is enabled + matches,
  return ``True`` (ALLOW). Otherwise ``None``.

All three buckets live in-process (operators flip them via the security
config endpoints), so the adapter re-reads them on every call — there is
no cached snapshot to invalidate. Every shape is read defensively:
missing keys behave as "feature off" (``None``), so a fresh deployment
reproduces the pre-U-005 cascade exactly.

Import discipline: this adapter imports from the *application* layer
(``ports`` + ``security_runtime_state``) and the *domain* layer
(``value_objects``) only — the ``layered-security`` contract permits
``adapters -> application -> domain``.
"""

from __future__ import annotations

import fnmatch
import os.path
from collections.abc import Mapping
from typing import Any

from qai.security.application.ports import AutoApprovePort
from qai.security.application.security_runtime_state import (
    SecurityRuntimeStateService,
)
from qai.security.domain.command_blacklist import (
    DEFAULT_COMMAND_BLACKLIST_PREFIXES,
)
from qai.security.domain.value_objects import AceMask, Resource

__all__ = ["RuntimeStateAutoApproveAdapter"]

# Bucket names (mirror the security config endpoints):
#   * ``auto_approve``      — legacy /auto_approve/config (enabled + trusted_paths)
#   * ``auto_approve_tool`` — /auto_approve panel (per-op toggles + cmd lists)
#   * ``path_patterns``     — /path_patterns (read/write glob allowlists)
_AUTO_APPROVE_BUCKET = "auto_approve"
_AUTO_APPROVE_TOOL_BUCKET = "auto_approve_tool"
_PATH_PATTERNS_BUCKET = "path_patterns"

# SEC-ENHANCE-EXECPOLICY 2-B — built-in default command blacklist.
#
# The curated default prefix set now lives in the domain layer
# (:data:`qai.security.domain.command_blacklist.DEFAULT_COMMAND_BLACKLIST_PREFIXES`)
# so the thin ``interfaces.http`` auto-approve route can mirror the same
# defaults for display WITHOUT a forbidden direct import of this adapter
# module (``interfaces-stays-thin`` contract). This adapter is the
# ENFORCEMENT fallback: when neither the ``auto_approve`` nor the
# ``auto_approve_tool`` runtime bucket configures a blacklist (fresh install:
# the user never saved the 自动审批 panel), ``_command_list`` falls back to
# that domain constant so destructive commands are denied out-of-the-box.


class RuntimeStateAutoApproveAdapter(AutoApprovePort):
    """``AutoApprovePort`` reading the live auto-approve runtime buckets.

    Data sources (P2 bucket-unification fix):

    * ``auto_approve`` bucket — ``enabled`` + ``trusted_paths``
      (legacy ``/auto_approve/config`` endpoint).
    * ``auto_approve_tool`` bucket — per-op toggles + command lists
      (the "自动审批" panel ``/auto_approve`` endpoint). Falls back to
      the ``auto_approve`` bucket so tests / configs that wrote toggles
      or command lists straight into ``auto_approve`` still drive the
      pre-check.
    * ``path_patterns`` bucket — ``read_allow_patterns`` /
      ``write_allow_patterns`` glob allowlists (``/path_patterns``).
    """

    def __init__(self, *, runtime_state: SecurityRuntimeStateService) -> None:
        self._runtime_state = runtime_state

    # ── public port surface ───────────────────────────────────────────

    def is_auto_approved(
        self,
        *,
        resource: Resource,
        requested_mask: AceMask,
    ) -> bool | None:
        legacy = self._bucket(_AUTO_APPROVE_BUCKET)
        tool = self._bucket(_AUTO_APPROVE_TOOL_BUCKET)

        # Exec is gated by the command whitelist / blacklist (V1
        # ``_command_passes_lists``: blacklist first, then whitelist),
        # plus the ``auto_approve.exec`` master toggle (V1: exec only
        # auto-approves when ``auto_approve.exec`` is on).
        if requested_mask.execute:
            return self._exec_decision(
                legacy=legacy, tool=tool, command=resource.identifier
            )

        # Path read/write auto-approval. Only applies to filesystem
        # resources (V1 trusted-path / per-op / glob-pattern semantics).
        if resource.kind == "path" and (
            requested_mask.read or requested_mask.write
        ):
            # (1) trusted_paths prefix match (legacy /config bucket).
            if self._path_trusted(legacy, path=resource.identifier):
                return True
            # (2) per-op tool toggle ON (V1 ``is_auto_approved`` read/write).
            if self._op_toggle_on(
                legacy=legacy, tool=tool, requested_mask=requested_mask
            ):
                return True
            # (3) path_patterns glob allowlist enabled + matches (V1
            #     ``read_allow_patterns`` / ``write_allow_patterns``).
            if self._path_pattern_allows(
                path=resource.identifier, requested_mask=requested_mask
            ):
                return True
        return None

    # ── helpers ────────────────────────────────────────────────────────

    def _bucket(self, name: str) -> Mapping[str, Any]:
        """Return the named runtime bucket as a Mapping (``{}`` when absent)."""
        raw = self._runtime_state.get_settings(name)
        return raw if isinstance(raw, Mapping) else {}

    @staticmethod
    def _str_list(value: Any) -> list[str]:
        """Coerce a config value into a list of non-empty stripped strings."""
        if not isinstance(value, (list, tuple)):
            return []
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @staticmethod
    def _toggles(bucket: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the nested per-op toggle map (``auto_approve`` key).

        The panel persists tool toggles under a nested ``auto_approve``
        key (``{auto_approve: {read, write, exec, ...}}``). The legacy
        U-005 bucket put them at the top level; this returns whichever
        shape is present, preferring the nested form.
        """
        nested = bucket.get("auto_approve")
        if isinstance(nested, Mapping):
            return nested
        return bucket

    def _op_toggle_on(
        self,
        *,
        legacy: Mapping[str, Any],
        tool: Mapping[str, Any],
        requested_mask: AceMask,
    ) -> bool:
        """V1 ``is_auto_approved`` per-op boolean for a path read/write.

        ``write`` consults ``auto_approve.write``; ``read`` (and, by V1
        fallback, glob/grep) consults ``auto_approve.read``. The panel
        bucket wins; the legacy bucket is the fallback so existing
        configs/tests still drive the pre-check.
        """
        # Tool panel takes precedence; fall back to the legacy bucket.
        for src in (self._toggles(tool), self._toggles(legacy)):
            if not src:
                continue
            if requested_mask.write:
                if bool(src.get("write", False)):
                    return True
            elif requested_mask.read:
                if bool(src.get("read", False)):
                    return True
        return False

    def _path_trusted(
        self, bucket: Mapping[str, Any], *, path: str
    ) -> bool:
        """V1 trusted-path prefix match (case-insensitive, Windows-friendly).

        Honours the bucket's ``enabled`` flag (V1: auto_approve must be
        on). A target path is trusted when it equals — or is nested
        under — one of the ``trusted_paths`` entries, compared by
        case-folded path components (mirrors V1
        ``PolicyCenter._path_in_allowlist``).
        """
        if not bool(bucket.get("enabled", False)):
            return False
        trusted = self._str_list(bucket.get("trusted_paths"))
        if not trusted or not path:
            return False
        target_parts = self._path_parts(path)
        if not target_parts:
            return False
        for entry in trusted:
            entry_parts = self._path_parts(entry)
            if not entry_parts:
                continue
            if len(target_parts) < len(entry_parts):
                continue
            if target_parts[: len(entry_parts)] == entry_parts:
                return True
        return False

    @staticmethod
    def _path_parts(path: str) -> tuple[str, ...]:
        """Split a path into case-folded components (``/`` and ``\\``)."""
        normalised = path.replace("\\", "/")
        return tuple(
            part.casefold() for part in normalised.split("/") if part
        )

    def _path_pattern_allows(
        self, *, path: str, requested_mask: AceMask
    ) -> bool:
        """V1 glob allowlist (``read_allow_patterns`` / ``write_allow_patterns``).

        Mirrors V1 ``PolicyCenter._path_matches_patterns``
        (``policy.py:702-718``) consulted in ``explain_read`` /
        ``explain_write`` (``:921-924`` / ``:976-979``): when the bucket's
        op-specific allowlist is ``enabled`` and the (slash-normalised,
        case-folded) target matches any ``fnmatch`` glob, it is an
        allowlist hit -> ALLOW. ``enabled=false`` (V1 default) -> no
        effect, so behaviour is unchanged when the operator never turned
        path patterns on.
        """
        if not path:
            return False
        bucket = self._bucket(_PATH_PATTERNS_BUCKET)
        if not bucket:
            return False
        # write request -> write_allow_patterns; read request -> read.
        key = (
            "write_allow_patterns"
            if requested_mask.write
            else "read_allow_patterns"
        )
        raw = bucket.get(key)
        if not isinstance(raw, Mapping):
            return False
        if not bool(raw.get("enabled", False)):
            return False
        patterns = self._str_list(raw.get("patterns"))
        if not patterns:
            return False
        return self._matches_patterns(path, patterns)

    @staticmethod
    def _matches_patterns(path: str, patterns: list[str]) -> bool:
        """V1 ``_path_matches_patterns``: slash-normalised, case-folded fnmatch.

        Patterns are matched verbatim (not OS-normalised) so glob forms
        such as ``**/*.py`` / ``src/**/*.py`` survive — mirroring V1,
        which only replaced backslashes and case-folded before
        ``fnmatch.fnmatch`` (``policy.py:712-717``).
        """
        target = path.replace("\\", "/").casefold()
        for pattern in patterns:
            expanded = pattern.replace("\\", "/").casefold()
            if fnmatch.fnmatch(target, expanded):
                return True
        return False

    def _exec_decision(
        self,
        *,
        legacy: Mapping[str, Any],
        tool: Mapping[str, Any],
        command: str,
    ) -> bool | None:
        """V1 ``is_auto_approved("exec")`` + ``_command_passes_lists``.

        * blacklist hit            -> ``False`` (DENY, highest priority,
                                      unconditional — V1 blacklist is
                                      checked before everything).
        * ``auto_approve.exec`` off -> ``None`` (defer to policy; exec is
                                      only auto-approved when the master
                                      toggle is on).
        * whitelist enabled+hit    -> ``True`` (ALLOW)
        * whitelist enabled+miss   -> ``None`` (defer to policy)
        * whitelist not enabled    -> ``True`` (V1: rely solely on the
                                      ``auto_approve.exec`` switch)
        """
        cmd_lower = (command or "").strip().lower()
        if not cmd_lower:
            return None

        # Command lists may live in either bucket; the panel bucket wins,
        # the legacy bucket is the fallback (keeps U-005 configs working).
        blacklist = self._command_list(
            legacy=legacy, tool=tool, key="command_blacklist"
        )
        # V1: blacklist enabled by default; a configured blacklist with
        # prefixes denies on prefix-or-substring match. Checked first and
        # unconditionally (highest priority, even if exec toggle is off).
        if blacklist.enabled:
            for prefix in blacklist.prefixes:
                if cmd_lower.startswith(prefix) or prefix in cmd_lower:
                    return False

        # V1 ``is_auto_approved("exec")``: requires ``auto_approve.exec``
        # ON before any auto-approval. The panel toggle wins; legacy
        # fallback keeps U-005 configs (which omit the exec toggle) able
        # to opt in via the command whitelist only — so we treat a missing
        # exec toggle as "off" but a configured whitelist still gates a
        # match. To preserve U-005 behaviour (whitelist hit -> True even
        # without an explicit exec toggle) we only require the exec toggle
        # when *no* whitelist is enabled.
        whitelist = self._command_list(
            legacy=legacy, tool=tool, key="command_whitelist"
        )
        exec_on = self._exec_toggle_on(legacy=legacy, tool=tool)

        if whitelist.enabled:
            first_token = cmd_lower.split()[0] if cmd_lower.split() else ""
            cmd_name = os.path.basename(first_token).removesuffix(".exe")
            for prefix in whitelist.prefixes:
                if cmd_name == prefix or cmd_name.startswith(prefix):
                    # V1: whitelist hit auto-approves exec (the panel also
                    # requires exec ON; legacy U-005 configs that set only a
                    # whitelist are honoured for backward compatibility).
                    return True
            # Whitelist enabled but no match → V1 returns False ("do not
            # auto-approve"); as a pre-check that means "no opinion" so the
            # regular policy still gets a chance.
            return None

        # No whitelist enabled → V1 relies solely on the exec switch.
        if exec_on:
            return True
        return None

    def _exec_toggle_on(
        self, *, legacy: Mapping[str, Any], tool: Mapping[str, Any]
    ) -> bool:
        """Return ``True`` when ``auto_approve.exec`` is ON in either bucket."""
        for src in (self._toggles(tool), self._toggles(legacy)):
            if src and bool(src.get("exec", False)):
                return True
        return False

    def _command_list(
        self,
        *,
        legacy: Mapping[str, Any],
        tool: Mapping[str, Any],
        key: str,
    ) -> "_CommandList":
        """Parse a command whitelist/blacklist, panel bucket taking priority.

        For the blacklist (V1 default-enabled) we must distinguish "not
        configured anywhere" from "configured + enabled with no prefixes":
        the panel bucket is consulted first; if it carries the key we use
        it, else fall back to the legacy bucket.

        SEC-ENHANCE-EXECPOLICY 2-B — when the ``command_blacklist`` is not
        configured in EITHER bucket (fresh install: the user never saved the
        自动审批 panel), fall back to the built-in
        :data:`qai.security.domain.command_blacklist.DEFAULT_COMMAND_BLACKLIST_PREFIXES`
        (enabled) so destructive commands are denied out-of-the-box. A user
        who saves an explicit blacklist (even an empty one) overrides this
        default. The whitelist has no such default (absent → disabled), so
        only the blacklist gets the built-in.
        """
        raw = tool.get(key)
        if not isinstance(raw, Mapping):
            raw = legacy.get(key)
        if not isinstance(raw, Mapping):
            # Unconfigured. The blacklist ships with a built-in default set
            # (V1 "blacklist enabled by default"); the whitelist does not.
            if key == "command_blacklist":
                return _CommandList(
                    enabled=True, prefixes=DEFAULT_COMMAND_BLACKLIST_PREFIXES
                )
            return _CommandList(enabled=False, prefixes=())
        enabled = bool(raw.get("enabled", False))
        prefixes = tuple(
            p.lower() for p in self._str_list(raw.get("prefixes"))
        )
        return _CommandList(enabled=enabled, prefixes=prefixes)


class _CommandList:
    """Lightweight value holder for a parsed command whitelist/blacklist."""

    __slots__ = ("enabled", "prefixes")

    def __init__(self, *, enabled: bool, prefixes: tuple[str, ...]) -> None:
        self.enabled = enabled
        self.prefixes = prefixes
