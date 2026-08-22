# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.user_prefs`` bounded context — user preferences.

This BC owns durable, user-facing preference documents stored in the
shared ``kv_user_prefs`` SQLite table (migration 007).  It replaces the
legacy ``backend/forge_config_manager.py`` JSON-on-disk store and the
scattered ``backend/main.py`` endpoints (``/api/forge-config``,
``/api/preferences``, ``/api/code-personas``, ``/api/proxy``,
``/api/settings/*``).

Scope (S7.5 lane L6):
---------------------
* PR-601a — backbone: ``GET / POST /api/forge-config`` and
  ``GET / POST /api/preferences``.  Establishes the BC skeleton, the
  ``UserPrefsRepositoryPort``, the ``KvUserPrefsRepository`` adapter
  against migration 007's ``kv_user_prefs`` table, and the two thin
  use cases ``LoadDocumentUseCase`` / ``SaveDocumentUseCase`` that
  every other endpoint in this BC layers on top of.
* PR-601b — ``proxy`` / ``code-personas`` / ``settings/*`` (12
  per-feature blobs) on top of the same backbone.

Design (Clean Architecture):
----------------------------
The KV store treats every preference document as an opaque JSON blob
keyed by a stable namespace (``forge.config``, ``ui.preferences``,
``proxy.config`` …).  No cross-context schema policing happens here —
each consumer of a key (e.g. channels reading ``proxy.config``) owns
its own schema validation.  This keeps user_prefs framework-thin and
avoids accidentally promoting it into a god BC that knows every other
context's preference shape.

Cross-context boundary:
-----------------------
Other BCs MUST NOT ``import qai.user_prefs.*`` (v2.7 §3.2).  When they
need a preference document, they read the ``kv_user_prefs`` table
directly via their own KV-style adapter (mirroring
:class:`qai.ai_coding.adapters.coding_config_repository.KvCodingConfigRepository`
which predates user_prefs and uses the same migration 007 table for
``ai_coding.config``).  user_prefs is the **owner of the routes** for
end-user-editable preferences — it is not the owner of the table.
"""
