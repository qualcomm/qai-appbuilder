# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.command_policy`` bounded context — exec command approval profiles.

This BC manages execution profiles that define which commands are allowed
or denied in the security sandbox. Each profile specifies allowed commands
and deny patterns.

Scope (S7.5 lane L6 — PR-603):
-------------------------------
* Domain: ``CommandProfile`` entity.
* Port: ``ExecBrokerPort`` — ``get_profiles()`` / ``is_enabled()``.
* Adapter: ``InMemoryExecBroker`` — production in-memory profile store.
* Use case: ``GetExecProfilesUseCase``.

Cross-context boundary:
-----------------------
Only imports ``qai.platform.*`` and ``qai.command_policy.*`` (v2.7 §3.2).
"""
