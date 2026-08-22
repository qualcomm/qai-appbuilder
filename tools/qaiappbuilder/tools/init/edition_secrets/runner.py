# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation of the internal-edition factory-secret provisioner.

See package docstring (``tools/init/edition_secrets/__init__.py``) for the
design rationale and the four-layer internal-asset defence this stage is part
of.
"""

from __future__ import annotations

import json as _json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from qai.platform.config.paths import DataPaths
from qai.platform.logging import get_logger
from qai.platform.persistence.secrets import (
    SecretStore,
    build_secret_store,
)

from .._common.modes import Mode
from .._common.report import InitReport, InitReportEntry

_LOGGER = get_logger("qai.init.edition_secrets")

# SecretStore namespace that the runtime provider read/write path uses
# (``UpdateProviderConfigUseCase._PROVIDER_SECRET_SERVICE`` /
# ``apps/api/_model_resolver_bridge``). The key is the provider id.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"


@dataclass
class RunResult:
    mode: Mode
    report: InitReport
    secrets_written: int = 0
    secrets_skipped: int = 0
    providers_written: int = 0
    providers_skipped: int = 0

    @property
    def exit_code(self) -> int:
        return 0 if self.report.is_ok() else 1


def _resolve_is_internal_fallback() -> bool:
    """Best-effort edition resolution when the orchestrator did not pass one.

    The install CLI / orchestrator are expected to resolve ``is_internal``
    explicitly (via ``load_settings(repo_root=data_root.parent).is_internal``)
    and pass it down — so this is only the fallback path for callers that
    forget. We try ``Path.cwd()`` as the repo_root because production install
    invocations always run from the artifact root (the directory holding
    ``build_info.json``); if that fails or the marker is absent we degrade to
    ``True`` (internal / full feature set), mirroring ``Settings.edition``'s
    default for the dev source tree.

    Never raises: a resolution failure must not abort install.
    """
    try:
        from pathlib import Path

        from qai.platform.config.settings import load_settings

        return bool(load_settings(repo_root=Path.cwd()).is_internal)
    except Exception:  # noqa: BLE001 — degrade to internal on any failure
        return True


def _load_factory_keys() -> dict[str, str]:
    """Read ``{provider_id: api_key}`` from the edition-local config.

    Returns ``{}`` if the edition package / section is absent (e.g. an
    external artifact that physically excluded it) — making this stage a
    clean no-op rather than an error.
    """
    try:
        from qai.platform.edition.loader import get_cloud_provider_api_keys

        return dict(get_cloud_provider_api_keys())
    except Exception:  # noqa: BLE001 — missing edition pkg => nothing to do
        return {}


def run(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    secret_backend: SecretStore | None = None,
    is_internal: bool | None = None,
) -> RunResult:
    """Provision internal-edition factory cloud-provider API keys.

    Args:
        mode: ``dry-run`` / ``apply`` / ``verify``.
        data_root: target ``data/`` directory (locates ``data/secrets/``).
        factory_root: bundle root (unused here; kept for stage-signature
            symmetry with the other init stages so the orchestrator can call
            every stage the same way).
        secret_backend: pre-built :class:`SecretStore`. ``None`` (production)
            builds one via ``build_secret_store(prefer="auto")``; tests pass a
            :class:`NullSecretStore`.
        is_internal: edition gate override (tests). ``None`` resolves the real
            edition via ``Settings.is_internal``.

    Returns:
        :class:`RunResult` with the populated report.
    """
    report = InitReport(
        initialiser="edition_secrets",
        mode=mode,
        data_root=str(data_root),
        factory_root=str(factory_root),
    )

    internal = (
        _resolve_is_internal_fallback() if is_internal is None else is_internal
    )
    if not internal:
        # External edition: never provision internal credentials.
        report.add(InitReportEntry(
            initialiser="edition_secrets.gate",
            location="skipped_empty",
            target="external_edition",
            note="external edition — internal factory secrets not provisioned",
        ))
        return RunResult(mode=mode, report=report)

    factory_keys = _load_factory_keys()
    # NOTE: an empty ``factory_keys`` (no non-empty api_key in
    # internal_config.toml) does NOT mean "nothing to do". The internal
    # edition intentionally ships model catalogs WITHOUT credentials — the
    # user supplies the api_key later via the in-app prompt / Settings. The
    # provider-config seed (base_url + models + default selection) is driven
    # by ``get_cloud_provider_full_configs()`` and is INDEPENDENT of api_keys,
    # so it must still run even when there is no key to provision.
    if not factory_keys:
        report.add(InitReportEntry(
            initialiser="edition_secrets.gate",
            location="skipped_empty",
            target="no_factory_keys",
            note=(
                "no edition cloud-provider api keys declared; secret "
                "provisioning skipped (provider model catalogs are still "
                "seeded — the user sets keys via the in-app prompt)"
            ),
        ))

    if mode == "dry-run":
        for provider_id in sorted(factory_keys):
            report.add(InitReportEntry(
                initialiser="edition_secrets.provision",
                location="noop_dryrun",
                target=f"{_PROVIDER_SECRET_SERVICE}/{provider_id}",
                note="dry-run: would write factory api_key (value redacted)",
            ))
        # Still surface the provider-config seed plan (no-op in dry-run).
        _seed_provider_configs(data_root=data_root, report=report, mode=mode)
        return RunResult(mode=mode, report=report)

    # apply / verify need a real (or supplied) store.
    try:
        store = (
            secret_backend
            if secret_backend is not None
            else build_secret_store(data_paths=DataPaths(data_root), prefer="auto")
        )
    except Exception as exc:  # noqa: BLE001
        report.add_error(f"failed to build secret store: {exc}")
        return RunResult(mode=mode, report=report)

    if mode == "verify":
        return _verify(
            factory_keys=factory_keys, store=store, report=report, mode=mode
        )

    return _apply(factory_keys=factory_keys, store=store, report=report, mode=mode,
                  data_root=data_root)


def _apply(
    *,
    factory_keys: dict[str, str],
    store: SecretStore,
    report: InitReport,
    mode: Mode,
    data_root: Path,
) -> RunResult:
    written = 0
    skipped = 0
    for provider_id in sorted(factory_keys):
        api_key = factory_keys[provider_id]
        target = f"{_PROVIDER_SECRET_SERVICE}/{provider_id}"
        # Idempotency: never clobber a non-empty value the user already set
        # via the UI. Only write when the namespace is missing or empty.
        try:
            existing = ""
            if store.exists(_PROVIDER_SECRET_SERVICE, provider_id):
                existing = store.get(_PROVIDER_SECRET_SERVICE, provider_id)
        except ValueError as exc:
            report.add_error(f"{target}: invalid namespace: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"{target}: read failed: {exc}")
            continue

        if isinstance(existing, str) and existing:
            skipped += 1
            report.add(InitReportEntry(
                initialiser="edition_secrets.provision",
                location="skipped_existing",
                target=target,
                note="user value present; factory key not overwritten",
            ))
            continue

        try:
            store.set(_PROVIDER_SECRET_SERVICE, provider_id, api_key)
        except ValueError as exc:
            report.add_error(f"{target}: invalid namespace: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"{target}: set() failed: {exc}")
            continue
        written += 1
        _LOGGER.info(
            "edition_secrets.provisioned",
            service=_PROVIDER_SECRET_SERVICE,
            key=provider_id,
        )
        report.add(InitReportEntry(
            initialiser="edition_secrets.provision",
            location="secret_namespace",
            target=target,
            note="factory api_key provisioned (value redacted)",
        ))
    # Phase 2: Seed full provider configs from internal_config.toml into DB
    prov_written, prov_skipped = _seed_provider_configs(
        data_root=data_root, report=report, mode=mode
    )
    return RunResult(
        mode=mode,
        report=report,
        secrets_written=written,
        secrets_skipped=skipped,
        providers_written=prov_written,
        providers_skipped=prov_skipped,
    )


def _seed_provider_configs(
    *,
    data_root: Path,
    report: InitReport,
    mode: Mode,
) -> tuple[int, int]:
    """Seed full cloud-provider configs from internal_config.toml into DB.

    Writes to kv_user_prefs table the same shape that the runtime
    SqliteProviderRegistry reads. Idempotent: skips rows that already exist.

    Returns (written, skipped) counts.
    """
    from datetime import datetime, timezone

    try:
        from qai.platform.edition.loader import get_cloud_provider_full_configs

        configs = get_cloud_provider_full_configs()
    except Exception:  # noqa: BLE001
        return 0, 0

    if not configs:
        return 0, 0

    if mode == "dry-run":
        for provider_id in sorted(configs):
            key = f"model_catalog.provider.{provider_id}"
            report.add(InitReportEntry(
                initialiser="edition_secrets.provider_seed",
                location="noop_dryrun",
                target=key,
                note="dry-run: would seed provider config into kv_user_prefs",
            ))
        return 0, 0

    db_path = data_root / "db" / "qai.db"
    if not db_path.exists():
        # The DB is created by init_data_dir (stage 1) and populated by
        # seed_defaults (stage 3), both of which run BEFORE this stage in the
        # real install pipeline — so a missing DB here means either the
        # pipeline was invoked out of order, or a caller (e.g. the
        # secret-only unit tests) ran this stage in isolation with a bare
        # data_root. Either way the api_key provisioning above already
        # succeeded and must NOT be reported as a failure; the provider-config
        # seed is simply skipped (non-fatal) rather than erroring the stage.
        report.add(InitReportEntry(
            initialiser="edition_secrets.provider_seed",
            location="skipped_empty",
            target=str(db_path),
            note="qai.db absent; provider config seed skipped (non-fatal)",
        ))
        return 0, 0

    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    skipped = 0

    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            for provider_id, config in sorted(configs.items()):
                key = f"model_catalog.provider.{provider_id}"
                # Build value_json matching runtime shape
                value = {
                    "base_url": config["base_url"],
                    "models": config["models"],
                    "pinned": config["pinned"],
                }
                value_json = _json.dumps(value, separators=(",", ":"), sort_keys=True)

                # Idempotent: skip if row already exists
                cur = conn.execute(
                    "SELECT 1 FROM kv_user_prefs WHERE key = ?", (key,)
                )
                if cur.fetchone() is not None:
                    skipped += 1
                    report.add(InitReportEntry(
                        initialiser="edition_secrets.provider_seed",
                        location="skipped_existing",
                        target=key,
                        note="provider config already present; not overwritten",
                    ))
                    continue

                conn.execute(
                    "INSERT INTO kv_user_prefs (key, value_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, value_json, now_iso),
                )
                written += 1
                report.add(InitReportEntry(
                    initialiser="edition_secrets.provider_seed",
                    location="kv_user_prefs",
                    target=key,
                    note="provider config seeded from internal_config.toml",
                ))

                # If this provider is selected_by_default, also seed the UI
                # prefs (provider + model). Both use "unset-or-empty" semantics
                # rather than "row absent": the seed_defaults stage writes empty
                # placeholder rows (ui.selected_model_provider="" /
                # ui.selected_model_id="") from kv_user_prefs.jsonl BEFORE this
                # stage runs, so a bare "row exists?" probe would always skip.
                # We therefore write when the stored value is missing OR empty,
                # which still never clobbers a real user selection.
                if config.get("selected_by_default"):
                    _seed_ui_pref_if_unset(
                        conn,
                        key="ui.selected_model_provider",
                        value=provider_id,
                        now_iso=now_iso,
                        report=report,
                        note=f"default model provider set to {provider_id!r}",
                    )

                    default_model_id = config.get("default_model_id") or ""
                    if default_model_id:
                        _seed_ui_pref_if_unset(
                            conn,
                            key="ui.selected_model_id",
                            value=default_model_id,
                            now_iso=now_iso,
                            report=report,
                            note=f"default model set to {default_model_id!r}",
                        )

            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        report.add_error(f"edition_secrets.provider_seed: DB error: {exc}")

    return written, skipped


def _seed_ui_pref_if_unset(
    conn: "sqlite3.Connection",
    *,
    key: str,
    value: str,
    now_iso: str,
    report: InitReport,
    note: str,
) -> None:
    """Write a scalar UI preference only when it is currently unset.

    "Unset" means the row is absent OR its decoded value is falsy (empty
    string). The ``seed_defaults`` stage seeds empty placeholder rows for
    these UI keys from ``kv_user_prefs.jsonl`` before this stage runs, so a
    plain "does the row exist?" probe would always short-circuit and the
    factory default would never take effect. Checking the *value* keeps the
    stage idempotent while still never clobbering a real user selection made
    through the UI on a later re-install.
    """
    cur = conn.execute(
        "SELECT value_json FROM kv_user_prefs WHERE key = ?", (key,)
    )
    row = cur.fetchone()
    current = ""
    if row is not None:
        try:
            current = _json.loads(row[0])
        except (ValueError, TypeError):
            current = ""
    if current:
        # A real selection already exists — never overwrite it.
        return
    value_json = _json.dumps(value)
    if row is None:
        conn.execute(
            "INSERT INTO kv_user_prefs (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value_json, now_iso),
        )
    else:
        conn.execute(
            "UPDATE kv_user_prefs SET value_json = ?, updated_at = ? "
            "WHERE key = ?",
            (value_json, now_iso, key),
        )
    report.add(InitReportEntry(
        initialiser="edition_secrets.provider_seed",
        location="kv_user_prefs",
        target=key,
        note=note,
    ))


def _verify(
    *,
    factory_keys: dict[str, str],
    store: SecretStore,
    report: InitReport,
    mode: Mode,
) -> RunResult:
    for provider_id in sorted(factory_keys):
        target = f"{_PROVIDER_SECRET_SERVICE}/{provider_id}"
        try:
            present = store.exists(_PROVIDER_SECRET_SERVICE, provider_id)
        except ValueError as exc:
            report.add_error(f"verify: {target}: invalid namespace: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"verify: {target}: exists() failed: {exc}")
            continue
        if not present:
            report.add_error(
                f"verify: {target}: namespace not registered; run --apply first"
            )
            continue
        report.add(InitReportEntry(
            initialiser="edition_secrets.verify",
            location="secret_namespace",
            target=target,
            note="namespace registered",
        ))
    return RunResult(mode=mode, report=report)


__all__ = ["RunResult", "run"]
