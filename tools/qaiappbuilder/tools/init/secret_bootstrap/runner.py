# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation of the secret-namespace bootstrap (PR-063)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.platform.config.paths import DataPaths
from qai.platform.errors import NotFoundError
from qai.platform.logging import get_logger
from qai.platform.persistence.secrets import (
    NullSecretStore,
    SecretStore,
    build_secret_store,
)

from .._common.modes import Mode
from .._common.report import InitReport, InitReportEntry

_LOGGER = get_logger("qai.init.secret_bootstrap")

# Sentinel that PR-060 must always write into ``redacted_value``. Any
# other value indicates the manifest leaked a real credential and we
# refuse to proceed (defence in depth — PR-060 has its own redaction
# but never trust upstream).
_REDACTED_SENTINEL = "<redacted>"

# The empty placeholder we register namespaces with. Real values are
# filled in by the user post-install through the UI.
_PLACEHOLDER_VALUE = ""


@dataclass
class RunResult:
    mode: Mode
    report: InitReport
    namespaces_registered: int = 0
    namespaces_skipped: int = 0

    @property
    def exit_code(self) -> int:
        return 0 if self.report.is_ok() else 1


def run(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    secret_backend: SecretStore | None = None,
) -> RunResult:
    """Register secret namespaces declared in ``factory/secrets_manifest.json``.

    Args:
        mode: ``dry-run`` / ``apply`` / ``verify``.
        data_root: target ``data/`` directory (used by the file backend
            to locate ``data/secrets/``; PR-061 already created it).
        factory_root: bundle root containing ``secrets_manifest.json``.
        secret_backend: pre-built :class:`SecretStore`. When ``None``
            (the production path) the runner constructs one via
            :func:`build_secret_store(prefer="auto")`. Tests typically
            pass a :class:`NullSecretStore`.

    Returns:
        :class:`RunResult` with the populated report.
    """
    report = InitReport(
        initialiser="secret_bootstrap",
        mode=mode,
        data_root=str(data_root),
        factory_root=str(factory_root),
    )

    manifest_path = factory_root / "secrets_manifest.json"
    entries = _load_manifest(manifest_path, report)
    if entries is None:
        # Hard error already added to report; bail out with empty result.
        return RunResult(mode=mode, report=report)

    # Empty manifest → tolerated; nothing to do but the run is OK.
    if not entries:
        return RunResult(mode=mode, report=report)

    # ``dry-run`` must not construct a store — it must not touch the
    # OS keyring nor create any encrypted file. Just enumerate entries.
    if mode == "dry-run":
        for entry in entries:
            _validate_entry_redaction(entry, report)
            service = str(entry.get("service", ""))
            key = str(entry.get("key", ""))
            report.add(InitReportEntry(
                initialiser="secret_bootstrap.register",
                location="noop_dryrun",
                target=f"{service}/{key}",
                note="dry-run: would register namespace with empty placeholder",
            ))
        return RunResult(mode=mode, report=report)

    # apply / verify both need a real (or supplied) store.
    try:
        store = _resolve_store(
            secret_backend=secret_backend,
            data_root=data_root,
        )
    except Exception as exc:  # noqa: BLE001 — surface any backend init failure
        report.add_error(f"failed to build secret store: {exc}")
        return RunResult(mode=mode, report=report)

    if mode == "verify":
        _verify(entries=entries, store=store, report=report)
        return RunResult(mode=mode, report=report)

    # apply
    registered, skipped = _apply(
        entries=entries,
        store=store,
        report=report,
    )
    return RunResult(
        mode=mode,
        report=report,
        namespaces_registered=registered,
        namespaces_skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_manifest(
    manifest_path: Path,
    report: InitReport,
) -> list[dict[str, Any]] | None:
    """Return the manifest's ``entries`` list, or ``None`` on hard error."""
    if not manifest_path.exists():
        report.add_error(
            f"secrets_manifest.json missing at {manifest_path}; "
            "run compile_factory (PR-060) first"
        )
        return None
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add_error(f"cannot read {manifest_path}: {exc}")
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        report.add_error(f"{manifest_path}: invalid JSON: {exc}")
        return None
    if not isinstance(obj, dict):
        report.add_error(f"{manifest_path}: top-level is not an object")
        return None
    if "entries" not in obj:
        report.add_error(f"{manifest_path}: missing required key 'entries'")
        return None
    entries = obj["entries"]
    if not isinstance(entries, list):
        report.add_error(f"{manifest_path}: 'entries' is not a list")
        return None
    # Light per-entry shape validation; defer redaction-sentinel check
    # to the per-entry loop so the report can identify the offender.
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report.add_error(
                f"{manifest_path}: entries[{idx}] is not an object"
            )
            return None
        for required in ("service", "key"):
            if required not in entry:
                report.add_error(
                    f"{manifest_path}: entries[{idx}] missing required "
                    f"field {required!r}"
                )
                return None
    return entries


def _validate_entry_redaction(
    entry: dict[str, Any],
    report: InitReport,
) -> None:
    """Defence: reject manifests that leak real credentials."""
    redacted = entry.get("redacted_value", _REDACTED_SENTINEL)
    if redacted != _REDACTED_SENTINEL:
        # Loud failure — this is a security invariant.
        raise RuntimeError(
            "secrets_manifest entry has non-redacted value for "
            f"{entry.get('service')!r}/{entry.get('key')!r}; "
            "PR-060 must never persist real credential values"
        )


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def _resolve_store(
    *,
    secret_backend: SecretStore | None,
    data_root: Path,
) -> SecretStore:
    if secret_backend is not None:
        return secret_backend
    data_paths = DataPaths(data_root)
    return build_secret_store(data_paths=data_paths, prefer="auto")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply(
    *,
    entries: list[dict[str, Any]],
    store: SecretStore,
    report: InitReport,
) -> tuple[int, int]:
    registered = 0
    skipped = 0
    for entry in entries:
        # Defensive redaction check on the apply path.
        _validate_entry_redaction(entry, report)
        service = str(entry["service"])
        key = str(entry["key"])
        target = f"{service}/{key}"
        try:
            already = store.exists(service, key)
        except ValueError as exc:
            # Malformed service/key (path-traversal etc.).
            report.add_error(
                f"{target}: invalid namespace: {exc}"
            )
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"{target}: exists() failed: {exc}")
            continue

        if already:
            skipped += 1
            report.add(InitReportEntry(
                initialiser="secret_bootstrap.register",
                location="skipped_existing",
                target=target,
                note="namespace already registered; not overwritten",
            ))
            continue

        try:
            store.set(service, key, _PLACEHOLDER_VALUE)
        except ValueError as exc:
            report.add_error(f"{target}: invalid namespace: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"{target}: set() failed: {exc}")
            continue
        registered += 1
        _LOGGER.info(
            "secret_bootstrap.registered",
            service=service,
            key=key,
        )
        report.add(InitReportEntry(
            initialiser="secret_bootstrap.register",
            location="secret_namespace",
            target=target,
            note="registered with empty placeholder",
        ))
    return registered, skipped


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _verify(
    *,
    entries: list[dict[str, Any]],
    store: SecretStore,
    report: InitReport,
) -> None:
    for entry in entries:
        _validate_entry_redaction(entry, report)
        service = str(entry["service"])
        key = str(entry["key"])
        target = f"{service}/{key}"
        try:
            present = store.exists(service, key)
        except ValueError as exc:
            report.add_error(f"verify: {target}: invalid namespace: {exc}")
            continue
        except NotFoundError as exc:
            # Some backends might raise rather than return False; treat
            # as missing.
            report.add_error(f"verify: {target}: missing ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"verify: {target}: exists() failed: {exc}")
            continue
        if not present:
            report.add_error(
                f"verify: {target}: namespace not registered; "
                "run --apply first"
            )
            continue
        report.add(InitReportEntry(
            initialiser="secret_bootstrap.verify",
            location="secret_namespace",
            target=target,
            note="namespace registered",
        ))


__all__ = ["RunResult", "run"]
