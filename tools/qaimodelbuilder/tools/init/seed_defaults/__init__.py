# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``seed_defaults`` — load PR-060 staging JSONL into qai.db (PR-062).

After PR-061 created an empty ``qai.db`` with the v2 schema, PR-062
populates it with the **seed default rows** that PR-060 generated at
build time:

- ``factory/db_staging/cloud_models_to_model_catalog_entry.jsonl``
  → INSERT into ``model_catalog_entry`` (the bundled cloud-model
  catalogue every install gets out of the box).

- ``factory/db_staging/kv_user_prefs.jsonl``
  → INSERT into ``kv_user_prefs`` (UI defaults: enabled toolbar
  modules, default model selection, etc.).

The loader is **fully idempotent** — running it twice on the same
``qai.db`` is a no-op (existing rows are detected via primary key
and skipped). This matches PR-061's idempotency contract, which
together let the install / upgrade flow re-run the entire S6 chain
safely.

Verify mode reads ``qai.db`` and confirms every staged row has a
matching DB row + values.
"""

from __future__ import annotations

from .runner import run

__all__ = ["run"]
