# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``FileSystemPackFileCleanup`` — delete an imported pack's on-disk files.

Symmetric counterpart to :class:`FileSystemAppImportAdapter._install_pack`:
import copies a committed pack into ``pack_root/<id>/`` and stages each
variant's weights ``.bin`` under the manifest ``installPath`` anchor
(``repo_root/models/<id>/<bin>``); delete removes exactly those.

V1 reference: ``backend/app_builder/api_routes.py`` ``deleteFiles=true`` branch
(rmtree pack dir + weights dir) and ``backend/app_builder/importer.py``
``delete_variants`` (per-variant weights unlink + manifest rewrite).

Wired by :mod:`apps.api._app_builder_di`. When ``pack_root`` / ``repo_root`` is
not configured (lean test container) the methods are best-effort no-ops, so the
delete use case degrades to DB-only removal (the legacy pre-port behaviour).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from qai.app_builder.application.ports import VariantDeleteResult

__all__ = ["FileSystemPackFileCleanup"]

logger = logging.getLogger(__name__)


class FileSystemPackFileCleanup:
    """Remove a pack's ``<root>/<id>`` tree + weights install copies.

    Dual-anchor support (built-in + user Pack roots)
    ------------------------------------------------
    Since P4 an imported Pack physically lives in **exactly one** of two
    anchor pairs:

    * **built-in** — ``pack_root`` + ``repo_root`` (``installPath``
      relative to ``repo_root``);
    * **user-imported** — ``user_pack_root`` + ``user_weights_root``.

    ``delete_pack_files`` / ``delete_variant_files`` first probe which
    anchor actually holds the pack (State-Truth-First §5 铁律 1: disk is
    the truth — no in-process ``user_imported`` flag lookup needed) and
    then delete under the paired weights anchor. When ``user_pack_root``
    / ``user_weights_root`` are ``None`` (lean test container) the class
    degrades to the legacy built-in-only behaviour.
    """

    __slots__ = (
        "_pack_root",
        "_repo_root",
        "_user_pack_root",
        "_user_weights_root",
    )

    def __init__(
        self,
        *,
        pack_root: Path | None = None,
        repo_root: Path | None = None,
        user_pack_root: Path | None = None,
        user_weights_root: Path | None = None,
    ) -> None:
        self._pack_root = pack_root
        self._repo_root = repo_root
        self._user_pack_root = user_pack_root
        self._user_weights_root = user_weights_root

    # ------------------------------------------------------------------
    # anchor resolution (dual-root)
    # ------------------------------------------------------------------
    def _locate_pack(
        self, model_id: str
    ) -> tuple[Path | None, Path | None]:
        """Return ``(pack_root, weights_root)`` for the anchor holding ``model_id``.

        Probes user anchor first (fresh imports land there; State-Truth-First
        picks whichever anchor really contains the pack dir), then falls
        back to built-in. Returns ``(None, None)`` when neither anchor is
        configured or holds the pack — caller degrades to a no-op.
        """
        if (
            self._user_pack_root is not None
            and (self._user_pack_root / model_id).is_dir()
        ):
            return (self._user_pack_root, self._user_weights_root)
        if (
            self._pack_root is not None
            and (self._pack_root / model_id).is_dir()
        ):
            return (self._pack_root, self._repo_root)
        # Neither anchor holds the pack — fall back to the legacy built-in
        # pair so the delete degrades to a no-op rather than skipping paths
        # that might exist only under the install-path anchor (e.g. weights
        # linger while pack dir was already rmtree'd out-of-band).
        return (self._pack_root, self._repo_root)

    # ------------------------------------------------------------------
    # full pack delete
    # ------------------------------------------------------------------
    def delete_pack_files(self, model_id: str) -> tuple[str, ...]:
        errors: list[str] = []
        pack_root, weights_root = self._locate_pack(model_id)
        if pack_root is None or weights_root is None:
            return ()

        pack_dir = pack_root / model_id
        # Collect each variant's install-path weights from the manifest BEFORE
        # removing the pack dir (the manifest lives inside it).
        install_paths = self._read_install_paths(pack_dir)

        # 1. Remove the pack directory (manifest / runner / weights tree).
        if pack_dir.is_dir():
            try:
                shutil.rmtree(pack_dir, ignore_errors=False)
                logger.info("app_delete.pack_dir_removed: id=%s %s", model_id, pack_dir)
            except OSError as exc:
                errors.append(f"rmtree {pack_dir}: {exc}")
                shutil.rmtree(pack_dir, ignore_errors=True)

        # 2. Remove staged weights under the install-path anchor
        #    (weights_root/models/<id>/<bin>). V1 rmtree'd ``models/<id>``.
        removed_dirs: set[Path] = set()
        for ip in install_paths:
            dest = self._resolve_install_path(ip, weights_root=weights_root)
            if dest is None:
                continue
            if dest.is_file():
                try:
                    dest.unlink()
                    logger.info("app_delete.weights_removed: id=%s %s", model_id, dest)
                except OSError as exc:
                    errors.append(f"unlink {dest}: {exc}")
            removed_dirs.add(dest.parent)

        # 3. Best-effort: drop the now-empty ``models/<id>`` weights dir(s).
        for d in removed_dirs:
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

        return tuple(errors)

    # ------------------------------------------------------------------
    # per-variant delete
    # ------------------------------------------------------------------
    def delete_variant_files(
        self, model_id: str, variant_ids: tuple[str, ...]
    ) -> VariantDeleteResult:
        pack_root, weights_root = self._locate_pack(model_id)
        if pack_root is None or weights_root is None:
            # No filesystem to act on; treat as noop so the caller does not
            # mistakenly fall back to a destructive full delete.
            return VariantDeleteResult(
                mode="noop", deleted=(), remaining=(), new_default=None
            )

        pack_dir = pack_root / model_id
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.is_file():
            return VariantDeleteResult(
                mode="noop",
                deleted=(),
                remaining=(),
                new_default=None,
                errors=(f"manifest.json not found: {manifest_path}",),
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return VariantDeleteResult(
                mode="noop",
                deleted=(),
                remaining=(),
                new_default=None,
                errors=(f"manifest unreadable: {exc}",),
            )
        if not isinstance(manifest, dict):
            return VariantDeleteResult(
                mode="noop", deleted=(), remaining=(), new_default=None
            )

        raw_variants = [
            v for v in (manifest.get("variants") or []) if isinstance(v, dict)
        ]
        if not raw_variants:
            return VariantDeleteResult(
                mode="noop",
                deleted=(),
                remaining=(),
                new_default=None,
                errors=("manifest has no variants[]; nothing to remove",),
            )

        requested = {v for v in variant_ids if v}
        existing_ids = {str(v.get("id")) for v in raw_variants if v.get("id")}
        unknown = sorted(requested - existing_ids)
        actual = sorted(requested & existing_ids)
        surviving = [v for v in raw_variants if str(v.get("id")) not in requested]
        unknown_err: tuple[str, ...] = (
            (f"unknown variant ids: {unknown}",) if unknown else ()
        )

        if not surviving:
            # Removing all variants → caller must do a full-pack delete.
            return VariantDeleteResult(
                mode="would_be_empty",
                deleted=tuple(actual),
                remaining=(),
                new_default=None,
                errors=unknown_err,
            )
        if not actual:
            return VariantDeleteResult(
                mode="noop",
                deleted=(),
                remaining=tuple(str(v.get("id")) for v in raw_variants),
                new_default=self._default_id(raw_variants),
                errors=unknown_err,
            )

        errors: list[str] = []

        # 1. Remove weights for each deleted variant (install copy + staged).
        for v in raw_variants:
            vid = str(v.get("id"))
            if vid not in requested:
                continue
            install_rel = ((v.get("assets") or {}).get("installPath")) or ""
            if not install_rel:
                continue
            bin_name = Path(install_rel).name
            dest = self._resolve_install_path(
                install_rel, weights_root=weights_root
            )
            if dest is not None and dest.is_file():
                try:
                    dest.unlink()
                except OSError as exc:
                    errors.append(f"unlink {dest}: {exc}")
            staged = pack_dir / "weights" / bin_name
            if staged.is_file() or staged.is_symlink():
                try:
                    staged.unlink()
                except OSError as exc:
                    errors.append(f"unlink {staged}: {exc}")

        # 2. Rewrite manifest: variants[] + mirror new default into top-level.
        old_default_deleted = any(
            str(v.get("id")) in requested and v.get("default")
            for v in raw_variants
        )
        if old_default_deleted:
            for i, v in enumerate(surviving):
                v["default"] = i == 0
        manifest["variants"] = surviving
        new_default = self._default_variant(surviving) or surviving[0]
        if isinstance(new_default, dict):
            new_runtime = dict(new_default.get("runtime") or {})
            top_runtime = manifest.get("runtime") or {}
            for k in ("supportedDevices", "requiresQairtVersion"):
                if k in top_runtime and k not in new_runtime:
                    new_runtime[k] = top_runtime[k]
            manifest["runtime"] = new_runtime
            manifest["assets"] = dict(new_default.get("assets") or {})
            if new_default.get("metrics"):
                manifest["metrics"] = dict(new_default["metrics"])

        try:
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            errors.append(f"manifest write {manifest_path}: {exc}")

        logger.info(
            "app_delete.variants: model=%s deleted=%s remaining=%s newDefault=%s",
            model_id,
            actual,
            [str(v.get("id")) for v in surviving],
            self._default_id(surviving),
        )
        return VariantDeleteResult(
            mode="partial",
            deleted=tuple(actual),
            remaining=tuple(str(v.get("id")) for v in surviving),
            new_default=self._default_id(surviving),
            errors=tuple(errors) + unknown_err,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _read_install_paths(self, pack_dir: Path) -> list[str]:
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.is_file():
            return []
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        paths: list[str] = []
        assets = data.get("assets")
        if isinstance(assets, dict):
            ip = assets.get("installPath")
            if isinstance(ip, str) and ip:
                paths.append(ip)
        for v in data.get("variants") or []:
            if not isinstance(v, dict):
                continue
            v_assets = v.get("assets")
            if isinstance(v_assets, dict):
                ip = v_assets.get("installPath")
                if isinstance(ip, str) and ip:
                    paths.append(ip)
        return list(dict.fromkeys(paths))  # de-dup, preserve order

    def _resolve_install_path(
        self, install_path: str, *, weights_root: Path | None
    ) -> Path | None:
        if weights_root is None:
            return None
        rel = Path(install_path)
        return rel if rel.is_absolute() else (weights_root / rel)

    @staticmethod
    def _default_variant(variants: list[dict]) -> dict | None:
        for v in variants:
            if v.get("default"):
                return v
        return variants[0] if variants else None

    @classmethod
    def _default_id(cls, variants: list[dict]) -> str | None:
        d = cls._default_variant(variants)
        return str(d.get("id")) if isinstance(d, dict) and d.get("id") else None
