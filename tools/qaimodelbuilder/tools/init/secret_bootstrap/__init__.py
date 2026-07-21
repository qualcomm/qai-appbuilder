# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``secret_bootstrap`` — register PR-060 secret namespaces (PR-063).

After PR-061 created the empty ``data/`` tree (incl. ``data/secrets/``)
and PR-062 seeded ``qai.db`` from ``factory/db_staging/*.jsonl``,
PR-063 ingests ``factory/secrets_manifest.json`` and **registers**
each ``(service, key)`` namespace into the configured
:class:`qai.platform.persistence.secrets.SecretStore`.

What "register" means
---------------------
The PR-060 manifest deliberately ships only ``"<redacted>"`` placeholder
values — real credentials are never persisted in the install bundle.
PR-063 calls ``SecretStore.set(service, key, "")`` so that the namespace
exists with an empty value. The user (or a downstream provisioning
flow) replaces the empty placeholder via the UI post-install.

Idempotency
-----------
Re-running ``--apply`` on an already-bootstrapped store is a no-op:
``SecretStore.exists(service, key)`` short-circuits the write so an
already-populated real value is **never** clobbered back to ``""``.

Verify mode
-----------
Confirms every manifest entry has a corresponding namespace registered
in the store. (It does NOT inspect the value: an empty string and a
real credential are both acceptable post-bootstrap states.)
"""

from __future__ import annotations

from .runner import run

__all__ = ["run"]
