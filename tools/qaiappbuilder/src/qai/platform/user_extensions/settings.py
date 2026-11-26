# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User-editable extension settings: read for the settings card, validate, save.

Non-secret values live in ``ExtensionStateStore`` (``extensions.json``);
``secret`` values live in the extension's secret space (``ext.<name>``) and
are WRITE-ONLY: :func:`settings_view` reports only whether each one is set.

Update semantics (one call = the whole card):

* a key with a value -> validated against its declaration and stored;
* a key with ``null`` -> cleared (back to the declared default / unset);
* a key not sent -> unchanged (so the card need not resend secrets);
* an undeclared key -> rejected;
* after the update, every ``required`` setting must have a value.

Stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .host import ExtensionSecrets
from .manifest import ExtensionManifest, SettingDeclaration
from .state import ExtensionStateStore

__all__ = ["SettingsValidationError", "settings_view", "update_settings"]


class SettingsValidationError(ValueError):
    """``errors`` maps a setting key to a user-facing reason."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


def settings_view(
    manifest: ExtensionManifest, state: ExtensionStateStore, secrets: ExtensionSecrets
) -> list[dict[str, Any]]:
    """Every declared setting with its current value (secrets: only ``is_set``)."""
    stored = state.settings(manifest.name)
    secret_keys = _secret_keys(secrets)
    out: list[dict[str, Any]] = []
    for s in manifest.settings:
        item: dict[str, Any] = {
            "key": s.key,
            "type": s.type,
            "label": s.label,
            "description": s.description,
            "required": s.required,
        }
        if s.type == "select":
            item["options"] = list(s.options)
        if s.type == "number":
            item["min"], item["max"] = s.min, s.max
        if s.type == "secret":
            item["is_set"] = s.key in secret_keys
        else:
            item["default"] = s.default
            item["value"] = stored.get(s.key, s.default)
        out.append(item)
    return out


def update_settings(  # noqa: PLR0912 - validate-all, then write-all
    manifest: ExtensionManifest,
    state: ExtensionStateStore,
    secrets: ExtensionSecrets,
    values: Mapping[str, Any],
) -> None:
    """Validate ``values`` as a whole, then store them (all or nothing)."""
    decls = {s.key: s for s in manifest.settings}
    errors: dict[str, str] = {}
    for key in values:
        if key not in decls:
            errors[key] = "is not a setting of this extension"
    normalised: dict[str, Any] = {}
    for key, value in values.items():
        decl = decls.get(key)
        if decl is None or value is None:
            continue
        try:
            normalised[key] = decl.check(value)
        except ValueError as exc:
            errors[key] = str(exc)
    stored = state.settings(manifest.name)
    secret_keys = _secret_keys(secrets)
    for decl in manifest.settings:
        if decl.required and not _will_have_value(decl, values, normalised, stored, secret_keys):
            errors.setdefault(decl.key, "is required")
    if errors:
        raise SettingsValidationError(errors)

    plain = {k: v for k, v in stored.items() if k in decls and decls[k].type != "secret"}
    for key, value in values.items():
        decl = decls[key]
        if decl.type == "secret":
            if value is None or value == "":
                secrets.delete(key)
            else:
                secrets.set(key, normalised[key])
        elif value is None:
            plain.pop(key, None)
        else:
            plain[key] = normalised[key]
    state.set_settings(manifest.name, plain)


def _will_have_value(
    decl: SettingDeclaration,
    values: Mapping[str, Any],
    normalised: Mapping[str, Any],
    stored: Mapping[str, Any],
    secret_keys: frozenset[str],
) -> bool:
    if decl.key in values:
        v = normalised.get(decl.key)
        if v is None or v == "":
            return decl.type != "secret" and decl.default not in (None, "")
        return True
    if decl.type == "secret":
        return decl.key in secret_keys
    current = stored.get(decl.key, decl.default)
    return current not in (None, "")


def _secret_keys(secrets: ExtensionSecrets) -> frozenset[str]:
    try:
        return frozenset(secrets.keys())
    except Exception:  # noqa: BLE001 - no secret store: nothing is set
        return frozenset()
