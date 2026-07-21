# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem weight-presence probe (D3 domain-purity fix).

Concrete adapter implementing
:class:`qai.app_builder.application.ports.WeightsPresencePort` (and, by
matching method shapes, the domain's structural
:class:`qai.app_builder.domain.model_status.WeightsProbe`). This is the
single place that performs the filesystem reads the legacy
``backend/app_builder/registry.py:_detect_status`` /
``_detect_variant_status`` did inline (``Path.resolve()`` / ``.exists()``
/ ``.is_dir()`` / ``.iterdir()``).

Pulling the IO out of ``qai.app_builder.domain.model_status`` keeps that
module a pure mapping ("probe results → status enum"); the DI root
(``apps/api/_app_builder_di.py``) constructs this adapter with the
resolved ``pack_root`` + ``repo_root`` and hands it to the domain
functions.

V1 parity:

* ``install_path_present`` — relative paths are anchored at ``repo_root``
  and ``.resolve()``-d (V1 ``(_repo_root / p).resolve()``); absolute
  paths are stat'd verbatim. Returns ``Path.exists()``.
* ``pack_weights_dir_is_present_but_empty`` — ``<pack_root>/<pack_id>/
  weights/`` is checked with ``is_dir() and not any(iterdir())``, the
  exact V1 legacy-pack fallback predicate.

Probe failures (permission / IO errors) propagate as :class:`OSError`;
the domain catches them and maps to the ``Error`` status (V1 parity).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FileSystemWeightsPresence"]


class FileSystemWeightsPresence:
    """``WeightsPresencePort`` backed by real ``pathlib`` filesystem reads.

    Constructed per status-resolver build with the resolved pack/repo
    roots so the domain never sees a ``Path`` (only the high-level
    boolean answers).

    Dual-anchor support (built-in + user Pack roots)
    -----------------------------------------------
    Since P4 the runtime tracks two Pack anchors:

    * **built-in** — ``pack_root`` (``<repo_root>/factory/app_builder/models``)
      + ``repo_root`` (``installPath`` relative to repo_root; V1 layout);
    * **user-imported** — ``user_pack_root``
      (``<data_dir>/app_builder/user_models``) +
      ``user_weights_root``
      (``<data_dir>/app_builder/user_model_weights``); the manifest
      ``installPath`` is still relative but anchored at
      ``user_weights_root``.

    A given Pack physically lives in **exactly one** of the two anchors
    (State-Truth-First §5 铁律 1: disk is the truth). Each probe checks
    the built-in anchor first, then the user anchor — the first anchor
    that satisfies the predicate wins. Both may be ``None`` for a lean
    test container; ``install_path_present`` / ``pack_dir_present`` /
    ``pack_weights_dir_is_present_but_empty`` all degrade to the
    documented fallbacks (``False`` / fail-open ``True`` / ``False``).
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
        pack_root: Path | None,
        repo_root: Path | None,
        user_pack_root: Path | None = None,
        user_weights_root: Path | None = None,
    ) -> None:
        self._pack_root = pack_root
        self._repo_root = repo_root
        self._user_pack_root = user_pack_root
        self._user_weights_root = user_weights_root

    def _resolve_install_path(
        self, install_path: str, *, weights_root: Path | None
    ) -> Path:
        """Resolve a manifest ``installPath`` relative to ``weights_root``.

        V1 anchored relative install paths at the repo root
        (``_detect_status``: ``(_repo_root / p).resolve()``). For user
        Packs the anchor is ``user_weights_root`` instead. Absolute
        paths are returned verbatim.
        """
        p = Path(install_path)
        if not p.is_absolute() and weights_root is not None:
            p = (weights_root / p).resolve()
        return p

    def install_path_present(self, install_path: str) -> bool:
        """Whether ``install_path``'s weights exist under EITHER anchor.

        State-Truth-First: a Pack's weights physically live under exactly
        one of the two anchors; ``True`` iff the file is present under
        built-in ``repo_root`` OR user ``user_weights_root``. Absolute
        paths sidestep the anchor entirely (probed verbatim).
        """
        if not install_path:
            return False
        # Absolute path — anchor-agnostic.
        raw = Path(install_path)
        if raw.is_absolute():
            return raw.exists()
        # Built-in anchor.
        if self._repo_root is not None:
            if self._resolve_install_path(
                install_path, weights_root=self._repo_root
            ).exists():
                return True
        # User anchor.
        if self._user_weights_root is not None:
            if self._resolve_install_path(
                install_path, weights_root=self._user_weights_root
            ).exists():
                return True
        return False

    def pack_weights_dir_is_present_but_empty(self, pack_id: str) -> bool:
        """V1 legacy-pack fallback: ``is_dir() and not any(iterdir())``.

        Returns ``True`` iff ``<root>/<pack_id>/weights/`` is a
        present-yet-empty directory under whichever anchor holds the
        pack. Both anchors are probed; ``True`` on the first match.
        Absent anchors, absent dirs, or non-empty dirs all yield
        ``False``.
        """
        for root in (self._pack_root, self._user_pack_root):
            if root is None:
                continue
            weights_dir = root / pack_id / "weights"
            if not weights_dir.is_dir():
                continue
            if not any(weights_dir.iterdir()):
                return True
        return False

    def pack_dir_present(self, pack_id: str) -> bool:
        """Whether ``<pack_root>/<pack_id>/manifest.json`` exists on disk.

        Mirrors V1 ``registry._scan_packs`` admission test: a pack is
        "present" iff its directory contains a readable ``manifest.json``.
        Both anchors are probed; ``True`` on the first match. Returns
        ``True`` (fail-open) when NEITHER anchor is configured so a
        lean test container never hides every model.
        """
        if self._pack_root is None and self._user_pack_root is None:
            return True  # fail-open: no pack root configured
        for root in (self._pack_root, self._user_pack_root):
            if root is None:
                continue
            if (root / pack_id / "manifest.json").is_file():
                return True
        return False
