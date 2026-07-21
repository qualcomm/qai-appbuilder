# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""QAI v2 data/ initialisation tools (S6 PR-061..064).

This package supports the **fresh v2 install** workflow described in
``docs/90-refactor/refactor-plan.md`` v2.6 §9.4.10. There is no
"legacy → v2" data migration; the project ships zero user data.

Workflow
--------
1. Build-time (PR-060, ``tools/build/factory_compiler/compile_factory``) generates
   ``factory/`` from the sanitised repo config templates.
2. First-run install on the user's machine consumes ``factory/``:
   - **PR-061** (``data_dir``)  — create empty ``data/`` tree,
     apply 7 SQL migrations to a fresh ``qai.db``, create blob /
     secrets subtrees, copy default ``user_config.toml``.
   - **PR-062** (``seed_defaults``) — INSERT staging JSONL rows.
   - **PR-063** (``secret_bootstrap``) — register SecretStore
     namespaces from ``secrets_manifest.json`` (placeholder values).

Strict isolation rules (same as ``tools/build/factory_compiler``)
--------------------------------------------------
- Allowed new-code imports: ``qai.platform.errors``,
  ``qai.platform.logging``, ``qai.platform.persistence``
  (``Database`` + ``MigrationRunner`` are needed to apply schema),
  ``qai.platform.persistence.secrets`` (PR-063 only — registers
  SecretStore namespaces).
- Forbidden: ``backend.*`` / ``features.*`` / ``apps.*`` /
  ``interfaces.*`` / ``qai.<context>.*``.
- All CLI entry points support ``dry-run`` (default) / ``apply`` /
  ``verify``.
"""

from __future__ import annotations

__all__: list[str] = []
