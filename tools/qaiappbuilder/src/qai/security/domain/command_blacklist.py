# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.security.domain — default dangerous-command blacklist prefixes.

Pure domain constant (no I/O, no framework, no cross-context import). This
is the single source of truth for the built-in "enabled by default" command
blacklist that protects a fresh install out-of-the-box.

Two consumers read it (both are ALLOWED to import the domain layer):

* ``qai.security.adapters.auto_approve_adapter.RuntimeStateAutoApproveAdapter``
  — the ENFORCEMENT fallback: when neither the ``auto_approve`` nor the
  ``auto_approve_tool`` runtime bucket configures a ``command_blacklist``
  (fresh install: the user never saved the 自动审批 panel), the adapter
  denies destructive commands using this set.
* ``interfaces.http.routes.security._auto_approve`` — the DISPLAY mirror:
  the panel's default state surfaces this same set so a fresh install
  HONESTLY shows the built-in protection instead of an empty list.

Keeping the constant here (rather than in the adapter) lets the thin
``interfaces.http`` route reference the defaults WITHOUT a forbidden direct
import of ``qai.security.adapters`` (the ``interfaces-stays-thin`` contract),
while the adapter still imports it downward (``adapters -> domain``, which
``layered-security`` permits). One value, two readers, no duplication.

SEC-ENHANCE-EXECPOLICY 2-B — V1 semantics: the command blacklist is
"enabled by default". Curated to catch destructive / system-modifying
commands with minimal false-positive risk under substring matching — bare
``format`` / ``reboot`` are DELIBERATELY OMITTED (they collide with
``--format`` / ``--reboot``).

Mirrors ``factory/_source/access_policy.json`` command_blacklist (the
operator-editable source of record); kept in sync there.

NOTE: this is the SECOND layer of default exec protection; the first is
``apps/api/_file_broker_bridge.py`` DANGEROUS_COMMAND_PATTERNS (PatternFile
Broker, always-on). Both run before the FileGuard exec gate.
"""

from __future__ import annotations

DEFAULT_COMMAND_BLACKLIST_PREFIXES: tuple[str, ...] = (
    "curl|sh",
    "wget|sh",
    "powershell -enc",
    "cmd /c del",
    "rm -rf",
    "rmdir /s",
    "del /s",
    "del /q",
    "reg delete",
    "taskkill /f",
    "diskpart",
    "shutdown /",
    "mkfs.",
    "fdisk",
)

__all__ = ["DEFAULT_COMMAND_BLACKLIST_PREFIXES"]
