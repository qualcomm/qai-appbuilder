# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``install`` — end-to-end QAI install pipeline orchestrator (PR-064).

Chains the four S6 stages into a single ``dry-run / apply / verify``
interface so the release script (and operators) only need to know one
command:

1. **compile_factory** (PR-060 lineage; renamed from ``compile_factory``)
   Compile the factory defaults source (``factory/_source/``) into a
   ``factory/`` bundle (TOML + ``db_staging/*.jsonl`` +
   ``secrets_manifest.json``).  Skipped automatically when no
   ``--factory-source`` is supplied — production installs ship the
   pre-built bundle in the release tarball.

2. **data_dir** (PR-061)
   Create the empty ``data/`` directory tree, build a fresh
   ``db/qai.db`` and apply all SQL migrations, drop a
   ``user_config.toml`` from the bundle (or a placeholder).

3. **seed_defaults** (PR-062)
   ``INSERT OR IGNORE`` the bundle's ``db_staging/*.jsonl`` rows into
   ``qai.db`` — the bundled cloud-model catalogue and UI preference
   defaults.  Idempotent.

4. **secret_bootstrap** (PR-063)
   Register every ``(service, key)`` namespace from the bundle's
   ``secrets_manifest.json`` into the configured
   :class:`~qai.platform.persistence.secrets.SecretStore` with an
   empty placeholder value.  Idempotent (real values, once set, are
   never overwritten back to ``""``).

5. **edition_secrets** (internal edition only)
   Provision internal-edition factory cloud-provider API keys (e.g.
   the default provider) from the edition-excluded
   ``qai.platform.edition.internal_config.toml`` into the SecretStore
   (namespace ``qai.model_catalog.provider`` / ``<provider_id>``).
   A no-op on external editions (gated behind ``settings.is_internal``)
   and when no edition keys are declared.  Idempotent (a non-empty
   user-set value is never overwritten).

Apply mode short-circuits on the first failing stage so the operator
can fix it and re-run; the chain is fully idempotent so already-completed
stages are no-ops on re-entry.

Verify mode never short-circuits — every stage runs so the operator
sees every problem at once.

Dry-run mode runs every stage's planning pass without touching disk
even if an earlier stage's plan reported errors.
"""

from __future__ import annotations

from .runner import RunResult, StageResult, run

__all__ = ["RunResult", "StageResult", "run"]
