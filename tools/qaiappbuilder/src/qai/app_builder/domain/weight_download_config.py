# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Weight-download configuration value object (App Builder domain).

Pure, framework-free VO describing the per-Pack ``weights.json`` a
built-in App Builder model ships with (``factory/chat_features/app-builder/models/<id>/
weights.json``). The concrete reader lives in
``qai.app_builder.infrastructure.weight_download_config_reader`` and is
wired by the DI root; the application layer only ever sees this VO via
the :class:`~qai.app_builder.application.ports.WeightDownloadConfigPort`.

Shape (mirrors the on-disk JSON)::

    {
      "tag": "whisper-medium",
      "required_files": ["encoder.bin", "decoder.bin"],
      "optional_files": ["metadata.json"],
      "download_configs": {
        "snapdragon_x_elite":  {"url", "archive_name", "extracted_dir"},
        "snapdragon_x2_elite": {"url", "archive_name", "extracted_dir"}
      }
    }

The VO stores ``download_configs`` as an immutable mapping-of-mappings so
the use case can resolve the per-device ``{url, archive_name,
extracted_dir}`` triple (with a documented ``snapdragon_x_elite``
fallback) without touching the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

__all__ = ["WeightDownloadConfig"]


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightDownloadConfig:
    """Decoded per-Pack ``weights.json`` (immutable value object).

    Attributes
    ----------
    tag:
        Human-readable model tag used for log / progress lines (e.g.
        ``"whisper-medium"``).
    required_files:
        Files that MUST land in ``models/<id>/`` for the model to be
        considered installed.
    optional_files:
        Extra files copied when present but never required.
    download_configs:
        Device-family → ``{"url", "archive_name", "extracted_dir"}``
        mapping. Keys are the values :func:`detect_device_model` returns
        (``"snapdragon_x_elite"`` / ``"snapdragon_x2_elite"``).
    """

    tag: str
    required_files: tuple[str, ...]
    optional_files: tuple[str, ...]
    download_configs: Mapping[str, Mapping[str, str]]

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or not self.tag.strip():
            raise ValueError("WeightDownloadConfig.tag must be a non-empty str")
        if not isinstance(self.required_files, tuple) or not self.required_files:
            raise ValueError(
                "WeightDownloadConfig.required_files must be a non-empty tuple"
            )
        if not isinstance(self.optional_files, tuple):
            raise ValueError(
                "WeightDownloadConfig.optional_files must be a tuple"
            )
        if not isinstance(self.download_configs, Mapping) or not self.download_configs:
            raise ValueError(
                "WeightDownloadConfig.download_configs must be a non-empty mapping"
            )
        # Deep-freeze the nested mappings so the VO is truly immutable and
        # callers cannot mutate a shared config after construction.
        frozen: dict[str, Mapping[str, str]] = {}
        for device, cfg in self.download_configs.items():
            if not isinstance(cfg, Mapping):
                raise ValueError(
                    f"download_configs[{device!r}] must be a mapping"
                )
            for key in ("url", "archive_name", "extracted_dir"):
                if key not in cfg or not isinstance(cfg[key], str) or not cfg[key]:
                    raise ValueError(
                        f"download_configs[{device!r}] missing non-empty {key!r}"
                    )
            frozen[device] = MappingProxyType(dict(cfg))
        object.__setattr__(
            self, "download_configs", MappingProxyType(frozen)
        )

    def resolve_device_config(self, device_model: str) -> Mapping[str, str]:
        """Return the ``{url, archive_name, extracted_dir}`` for a device.

        Falls back to the ``snapdragon_x_elite`` variant when the exact
        ``device_model`` key is absent (mirrors
        ``weight_downloader.ensure_weights_downloaded``: the v73 HTP binary
        runs on both X Elite and X2 Elite). Raises :class:`KeyError` when
        neither the device key nor the fallback exists.
        """
        cfg = self.download_configs.get(device_model) or self.download_configs.get(
            "snapdragon_x_elite"
        )
        if cfg is None:
            raise KeyError(
                f"no download config for device {device_model!r} "
                "(and no snapdragon_x_elite fallback)"
            )
        return cfg
