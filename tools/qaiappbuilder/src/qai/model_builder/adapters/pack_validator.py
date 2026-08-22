# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Concrete :class:`PackValidatorPort` adapter.

Equivalent to ``features/model-builder/scripts/qai_pack_validate.py``
top-level structural checker. Runs **after** the exporter has written
``app_pack/`` (or against any candidate Pack handed in from disk) and
verifies:

* ``manifest.json`` exists, parses, has the expected core fields;
* ``runner.py`` exists and ``py_compile`` clean;
* ``requirements.txt`` exists;
* the weights file referenced by ``assets.installPath`` (or
  ``variants[0].assets.installPath`` for multi-variant Packs) exists
  under ``<pack>/weights/`` and matches the recorded SHA-256;
* ``_candidate.json`` exists and parses.

Validation failures land on the returned ``errors`` tuple — never
raise (use cases convert ``(False, errors)`` into route-level
diagnostics).
"""

from __future__ import annotations

import hashlib
import json
import py_compile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.model_builder.application.ports import PackValidatorPort
from qai.model_builder.domain import Pack

__all__ = ["QaiPackValidator"]


@dataclass(slots=True)
class QaiPackValidator:
    """Inspect a Pack directory in place."""

    async def validate(self, *, pack: Pack) -> tuple[bool, tuple[str, ...]]:
        return await self.validate_dir(pack_dir=pack.pack_dir)

    async def validate_dir(
        self,
        *,
        pack_dir: Path,
    ) -> tuple[bool, tuple[str, ...]]:
        errors: list[str] = []

        if not pack_dir.is_dir():
            return False, (f"pack directory not found: {pack_dir}",)

        # manifest.json
        manifest_path = pack_dir / "manifest.json"
        manifest: dict[str, Any] = {}
        if not manifest_path.is_file():
            errors.append("manifest.json missing")
        else:
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"manifest.json unreadable: {exc}")

        if manifest:
            for required in ("modelId", "displayName", "runtime", "assets"):
                if required not in manifest:
                    errors.append(f"manifest.json missing field {required!r}")

        # runner.py
        runner_path = pack_dir / "runner.py"
        if not runner_path.is_file():
            errors.append("runner.py missing")
        else:
            try:
                py_compile.compile(str(runner_path), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append(f"runner.py does not compile: {exc}")

        # requirements.txt
        if not (pack_dir / "requirements.txt").is_file():
            errors.append("requirements.txt missing")

        # weights — multi-variant aware.
        weights_dir = pack_dir / "weights"
        variants = manifest.get("variants") or []
        if isinstance(variants, list) and variants:
            for v in variants:
                self._check_variant(
                    variant=v,
                    weights_dir=weights_dir,
                    errors=errors,
                )
        else:
            assets = manifest.get("assets") or {}
            install_path = assets.get("installPath", "")
            checksum = assets.get("checksum", "")
            if install_path and checksum:
                weights_basename = Path(install_path).name
                self._check_weights_file(
                    weights_path=weights_dir / weights_basename,
                    expected_checksum=checksum,
                    errors=errors,
                )

        # _candidate.json
        candidate_path = pack_dir / "_candidate.json"
        if not candidate_path.is_file():
            errors.append("_candidate.json missing")
        else:
            try:
                json.loads(candidate_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"_candidate.json unreadable: {exc}")

        return (not errors), tuple(errors)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_variant(
        self,
        *,
        variant: dict[str, Any],
        weights_dir: Path,
        errors: list[str],
    ) -> None:
        v_id = variant.get("id", "<unknown>")
        assets = variant.get("assets") or {}
        install_path = assets.get("installPath", "")
        checksum = assets.get("checksum", "")
        if not install_path:
            errors.append(f"variant {v_id!r} missing assets.installPath")
            return
        weights_basename = Path(install_path).name
        weights_path = weights_dir / weights_basename
        self._check_weights_file(
            weights_path=weights_path,
            expected_checksum=checksum,
            errors=errors,
            tag=f"variant {v_id!r}",
        )

    def _check_weights_file(
        self,
        *,
        weights_path: Path,
        expected_checksum: str,
        errors: list[str],
        tag: str = "weights",
    ) -> None:
        if not weights_path.is_file():
            errors.append(
                f"{tag}: weights file not found at {weights_path}"
            )
            return
        if not expected_checksum.startswith("sha256:"):
            return
        expected = expected_checksum[len("sha256:"):]
        h = hashlib.sha256()
        try:
            with open(weights_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError as exc:
            errors.append(f"{tag}: could not hash {weights_path}: {exc}")
            return
        actual = h.hexdigest()
        if actual != expected:
            errors.append(
                f"{tag}: SHA-256 mismatch for {weights_path.name} "
                f"(expected {expected[:12]}..., got {actual[:12]}...)"
            )
