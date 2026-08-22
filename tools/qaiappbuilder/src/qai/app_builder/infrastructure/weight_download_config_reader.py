# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem reader for per-Pack ``weights.json`` download configs.

Concrete adapter implementing
:class:`qai.app_builder.application.ports.WeightDownloadConfigPort`. Reads
the bundled ``<repo_root>/factory/chat_features/app-builder/models/<id>/weights.json``
(the per-pack weight-config file that ships with each built-in App Builder
model) and decodes it into the pure
:class:`qai.app_builder.domain.weight_download_config.WeightDownloadConfig`
value object.

``factory/`` is intentionally NOT a Python package (it is shipped install
assets), so this adapter reads the file **by path** with ``json.load``
rather than importing it. A missing file, malformed JSON, or a config
missing required keys yields ``None`` (the use case then surfaces a
"no downloadable weights" signal instead of crashing).

The DI root (``apps/api/_app_builder_di.py``) constructs this with the
resolved ``repo_root``; keeping the path join here (not in the use case)
preserves the ``layered-app_builder`` clean-architecture contract
(application never touches ``pathlib`` / the filesystem).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from qai.app_builder.domain.weight_download_config import WeightDownloadConfig

__all__ = ["FileSystemWeightDownloadConfigReader"]

_logger = logging.getLogger(__name__)

# App model ids feed into a filesystem path; restrict to the same safe
# alphabet ``AppModelId`` enforces so a caller can never traverse out of
# the models dir even if it bypasses the VO.
_SAFE_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class FileSystemWeightDownloadConfigReader:
    """``WeightDownloadConfigPort`` backed by the bundled ``weights.json``."""

    __slots__ = ("_repo_root",)

    def __init__(self, *, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)

    def get(self, model_id: str) -> WeightDownloadConfig | None:
        """Return the decoded config for ``model_id`` or ``None``."""
        if not model_id or any(c not in _SAFE_ID_CHARS for c in model_id):
            return None
        path = (
            self._repo_root
            / "factory"
            / "chat_features"
            / "app-builder"
            / "models"
            / model_id
            / "weights.json"
        )
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("failed to read weights.json for %r: %s", model_id, exc)
            return None
        return self._decode(model_id, raw)

    @staticmethod
    def _decode(model_id: str, raw: object) -> WeightDownloadConfig | None:
        if not isinstance(raw, dict):
            _logger.warning("weights.json for %r is not an object", model_id)
            return None
        tag = raw.get("tag")
        required = raw.get("required_files")
        optional = raw.get("optional_files", [])
        download_configs = raw.get("download_configs")
        if not isinstance(tag, str) or not tag:
            _logger.warning("weights.json for %r missing 'tag'", model_id)
            return None
        if not isinstance(required, list) or not required:
            _logger.warning(
                "weights.json for %r missing non-empty 'required_files'",
                model_id,
            )
            return None
        if not isinstance(optional, list):
            optional = []
        if not isinstance(download_configs, dict) or not download_configs:
            _logger.warning(
                "weights.json for %r missing 'download_configs'", model_id
            )
            return None
        try:
            return WeightDownloadConfig(
                tag=tag,
                required_files=tuple(str(f) for f in required),
                optional_files=tuple(str(f) for f in optional),
                download_configs={
                    str(dev): {str(k): str(v) for k, v in cfg.items()}
                    for dev, cfg in download_configs.items()
                    if isinstance(cfg, dict)
                },
            )
        except (ValueError, TypeError, AttributeError) as exc:
            _logger.warning(
                "weights.json for %r failed validation: %s", model_id, exc
            )
            return None
