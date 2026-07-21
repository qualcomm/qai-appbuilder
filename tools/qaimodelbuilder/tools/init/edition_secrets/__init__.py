# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``edition_secrets`` — provision internal-edition factory credentials.

The internal edition ships a default cloud provider (cloud LLM service) whose
base_url + model list are seeded into ``kv_user_prefs`` by ``seed_defaults``,
but whose API KEY is a credential and therefore must NEVER live in any
committed seed artefact (``cloud_models.json`` / ``kv_user_prefs.jsonl`` /
``secrets_manifest.json`` — refactor-plan §3.3 red line). The factory-default
key instead lives in the edition-excluded package
``src/qai/platform/edition/internal_config.toml`` (``[cloud_providers.*]``),
which is physically dropped from external artifacts.

This stage reads those edition-local factory keys and writes them into the
SecretStore under the exact namespace the runtime read/write path uses
(``qai.model_catalog.provider`` / ``<provider_id>`` — see
``UpdateProviderConfigUseCase`` + ``_model_resolver_bridge``).

Four-layer internal-asset defence:
  1. runtime gate — this stage is a no-op unless ``is_internal`` is True;
  2. physical exclusion — the edition TOML never ships to external;
  3. factory sanitize — internal provider rows are cleansed from the
     external factory bundle;
  4. sensitive-word scan — internal-only endpoints / domains are on the
     external release blacklist (see ``scripts/release/check_release.py``
     ``SENSITIVE_KEYWORDS``).

Idempotency: an already-populated, non-empty secret is never overwritten
(the user may have replaced the factory key via the UI). An empty / missing
namespace is (re)written with the factory default.
"""

from __future__ import annotations

from .runner import RunResult, run

__all__ = ["RunResult", "run"]
