# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OpenCode provider-config synchroniser (RE-OC-3).

Writes the Cloud Models providers into OpenCode's own config file
(``~/.config/opencode/opencode.jsonc``) so the local OpenCode server can
use the providers the operator configured in QAIModelBuilder.

Why a file writer (not an API call)
-----------------------------------
OpenCode exposes **no** API to configure providers dynamically — the only
way to register a provider is to edit ``opencode.jsonc`` on disk (V1
``opencode_session_manager._sync_providers_to_opencode_config``
docstring, ``backend/ai_coding/opencode_session_manager.py:117-118``).
V1 (and v0.5, byte-identical) ran this sync inside the OpenCode session
manager's ``start()``.  V2 collapsed the session manager into the
unified coding aggregate + adapters and dropped this sync entirely, so a
provider configured in the Cloud Models panel never reached the local
OpenCode server.  This module restores the capability as a pure
infrastructure writer.

Architecture / layering
------------------------
This module takes **already-resolved** inputs (the providers map + the
Cloud-Models→OpenCode id mapping); it does NOT import
``qai.model_catalog`` (that cross-context data is gathered by the apps
DI layer, which legitimately sees both contexts — §3.2).  It only
touches the filesystem (an infrastructure concern), mirroring V1's
behaviour exactly:

* merge non-destructively — never overwrite a provider the user added by
  hand, only add missing ones and refresh a stale ``baseURL``;
* ``apiKey`` is written as the ``"public"`` placeholder (V1 parity,
  ``:152-155``) — the real key is the operator's to set in
  ``opencode.jsonc`` (OpenCode reads its own key; QAIModelBuilder keeps
  credentials in the SecretStore and never writes them here, §3.3);
* JSONC comments (``//`` and ``/* */``) in an existing file are stripped
  before parsing (V1 ``:170-172``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qai.platform.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "OPENCODE_CONFIG_PATH",
    "build_oc_provider_entries",
    "sync_providers_to_opencode_config",
]


def OPENCODE_CONFIG_PATH() -> Path:  # noqa: N802 — factory, not a constant
    """Return the OpenCode config file path (``~/.config/opencode/opencode.jsonc``).

    A function (resolved at call time) rather than a module constant so a
    test can monkeypatch ``Path.home`` and so the value reflects the
    current user/home at the moment of the sync (V1 ``:163``).
    """
    return Path.home() / ".config" / "opencode" / "opencode.jsonc"


def build_oc_provider_entries(
    *,
    providers: dict[str, dict[str, Any]],
    provider_mapping: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Build the ``{oc_provider_id: {apiKey, baseURL}}`` map (V1 ``:142-157``).

    ``providers`` is the Cloud Models provider map
    ``{provider_name: {"base_url": ...}}`` (V2 equivalent of V1
    ``CloudModelsConfig.providers``).  ``provider_mapping`` maps a Cloud
    Models provider name to an OpenCode provider id; an unmapped name
    falls back to its lower-cased form (V1 ``:151``).  Providers without a
    ``base_url`` are skipped (V1 ``:148-149``).
    """
    oc_providers: dict[str, dict[str, str]] = {}
    for provider_name, provider_info in providers.items():
        if not isinstance(provider_info, dict):
            continue
        base_url = provider_info.get("base_url") or provider_info.get("baseURL")
        if not base_url or not isinstance(base_url, str):
            continue
        oc_provider_id = provider_mapping.get(
            provider_name, provider_name.lower()
        )
        oc_providers[oc_provider_id] = {
            "apiKey": "public",
            "baseURL": base_url,
        }
    return oc_providers


def sync_providers_to_opencode_config(
    *,
    providers: dict[str, dict[str, Any]],
    provider_mapping: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> bool:
    """Merge the Cloud Models providers into ``opencode.jsonc``.

    Mirrors V1 ``_sync_providers_to_opencode_config``
    (``opencode_session_manager.py:113-208``) 1:1:

    * skip silently when there are no providers to sync;
    * load + JSONC-strip the existing file (corrupt/parse error → start
      from an empty doc, never raise);
    * add missing providers; for an existing provider only refresh a
      stale ``baseURL`` (never clobber a user-set ``apiKey``);
    * ensure the ``$schema`` key; write back with ``indent=2`` /
      ``ensure_ascii=False``.

    Best-effort: any filesystem / parse failure is logged and swallowed
    (the OpenCode service start must never abort because the optional
    provider sync failed — V1 ``:207-208``).

    Returns ``True`` when the file was (re)written, ``False`` when there
    was nothing to change (or on a swallowed error).
    """
    if not providers:
        return False

    oc_providers = build_oc_provider_entries(
        providers=providers,
        provider_mapping=provider_mapping or {},
    )
    if not oc_providers:
        return False

    path = config_path if config_path is not None else OPENCODE_CONFIG_PATH()

    # Load existing config (JSONC → JSON).
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover — fs perm edge.
            logger.warning("ai_coding.oc_sync.mkdir_failed", error=str(exc))
            return False
        existing: dict[str, Any] = {}
    else:
        try:
            content = path.read_text(encoding="utf-8")
            # Strip JSONC comments.  V1 (``:171``) used a naive
            # ``//[^\n]*`` which ALSO corrupts a ``://`` inside a string
            # value (e.g. our own ``"$schema": "https://opencode.ai/...``)
            # — re-reading a file we just wrote would then fail to parse.
            # That is a latent V1 defect (AGENTS.md 🟡🟡 "fix V1 defects,
            # never carry them forward"): we strip a ``//`` line comment
            # only when it is NOT preceded by a ``:`` (so scheme-bearing
            # URLs survive) and strip ``/* */`` blocks as before.
            content_clean = re.sub(r"(?<!:)//[^\n]*", "", content)
            content_clean = re.sub(
                r"/\*.*?\*/", "", content_clean, flags=re.DOTALL
            )
            existing = json.loads(content_clean) if content_clean.strip() else {}
        except Exception as exc:  # noqa: BLE001 — corrupt file → fresh doc.
            logger.warning("ai_coding.oc_sync.parse_failed", error=str(exc))
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    existing_providers = existing.get("provider")
    if not isinstance(existing_providers, dict):
        existing_providers = {}

    updated = False
    for pid, pcfg in oc_providers.items():
        if pid not in existing_providers:
            existing_providers[pid] = pcfg
            updated = True
            logger.info(
                "ai_coding.oc_sync.provider_added",
                provider=pid,
                base_url=pcfg["baseURL"],
            )
        else:
            current = existing_providers[pid]
            if (
                isinstance(current, dict)
                and current.get("baseURL") != pcfg["baseURL"]
            ):
                current["baseURL"] = pcfg["baseURL"]
                updated = True
                logger.info(
                    "ai_coding.oc_sync.provider_base_url_updated",
                    provider=pid,
                )

    if not updated:
        return False

    existing["provider"] = existing_providers
    if "$schema" not in existing:
        existing["$schema"] = "https://opencode.ai/config.json"

    try:
        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "ai_coding.oc_sync.written",
            provider_count=len(existing_providers),
            path=str(path),
        )
        return True
    except OSError as exc:
        logger.warning("ai_coding.oc_sync.write_failed", error=str(exc))
        return False
