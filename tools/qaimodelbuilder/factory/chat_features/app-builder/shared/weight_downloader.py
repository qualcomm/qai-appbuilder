#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
weight_downloader - shared model-weight auto-download helper
============================================================

Shared, runner-agnostic implementation of the "lazy weight download"
fallback used by the voice / TTS App Builder Packs (whisper / zipformer /
melotts). Extracted from the per-Pack ``_ensure_weights_downloaded`` so the
three runners reuse ONE platform-detection + proxy-aware download + extract
path instead of each carrying a near-identical copy (judgment-1: reuse, not
re-create).

Pipeline mirrors the QAI AppBuilder reference samples' ``ensure_model_files``:
    1. fast path: all required files already on disk -> return immediately
       (idempotent; a re-entry after a successful first run never touches the
       network).
    2. detect the Snapdragon SoC family (``x_elite`` / ``x2_elite``) to pick
       the right device-specific zip variant.
    3. stream-download the zip (resume-from-cached-archive if a previous run
       left one behind), extract, and copy the required + optional files into
       the canonical model directory.

Global proxy (edition-dual-form §8 "缺口 10")
--------------------------------------------
The App Builder Pack runners run as isolated subprocesses (they CANNOT import
``apps.api`` — context-isolation §3.2). The apps/api wiring root resolves the
machine-readable global-proxy URL from ``ToolsSettings.global_proxy`` (file-
backed config — NO environment variable is the *source of truth*; AGENTS.md
hard-constraint ①) and injects it into the spawned runner's environment as the
standard ``HTTPS_PROXY`` / ``ALL_PROXY`` variables. ``urllib.request.urlopen``
honours those automatically via ``getproxies()``, so when a global proxy is
configured every weight download routes through it; when it is not, the env
vars are absent and the download connects directly (proxy is never forced —
State-Truth-First). The env var here is a parent->child *transport* of an
already-resolved value, not a configuration source.

Error model
-----------
On any network / extraction failure the helper raises
:class:`WeightDownloadError` carrying the structured ``WEIGHTS_NOT_INSTALLED``
code + a human-readable hint, which each runner re-raises as its own
``_UserError("WEIGHTS_NOT_INSTALLED", ...)``. Crucially the application never
crashes: a missing model only makes that one inference path return
``WEIGHTS_NOT_INSTALLED`` (the frontend then shows ``voiceInput.weightsMissing``)
— app startup and every other feature are unaffected (hard-constraint ②).
"""

from __future__ import annotations

import platform
import shutil
import ssl
import sys
import zipfile
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.request import Request, urlopen

__all__ = [
    "WeightDownloadError",
    "detect_device_model",
    "ensure_weights_downloaded",
    "extract_weights_archive",
    "download_with_progress",
]


# qai-hub public-asset host all three model zips share.
QAIHUB_PUBLIC_ASSETS = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com"
)


class WeightDownloadError(Exception):
    """Structured weight-download failure (always ``WEIGHTS_NOT_INSTALLED``).

    Runners catch this and re-raise their own ``_UserError`` so the SSE error
    event carries the ``WEIGHTS_NOT_INSTALLED`` code the frontend maps to
    ``voiceInput.weightsMissing``.
    """

    code = "WEIGHTS_NOT_INSTALLED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def detect_device_model(*, tag: str = "weight_downloader") -> str:
    """Detect the Snapdragon SoC family for picking the right zip variant.

    Returns ``"snapdragon_x_elite"`` or ``"snapdragon_x2_elite"``. On
    non-Windows or unrecognized CPUs, defaults to ``snapdragon_x_elite`` (the
    v73 HTP binary works on both X Elite and X2 Elite, just slightly slower on
    X2). This is the single source of the CPU-family detection the zipformer
    runner originally carried; whisper / melotts reuse it (judgment-1).
    """
    cpu_name = ""
    if platform.system() == "Windows":
        try:
            import wmi  # type: ignore[import-not-found]

            c = wmi.WMI()
            for processor in c.Win32_Processor():
                cpu_name = (processor.Name or "").lower()
                if cpu_name:
                    break
        except Exception:  # pylint: disable=broad-except
            pass
    if not cpu_name:
        cpu_name = (platform.processor() or "").lower()

    print(f"[{tag}] detected CPU: {cpu_name!r}", file=sys.stderr)

    # X2 Elite markers: family 8 model 2, or "x2"-style SKU strings.
    if "family 8 model 2" in cpu_name or " x2 " in cpu_name or "x2e" in cpu_name:
        return "snapdragon_x2_elite"
    # X Elite markers: family 8 model 1, or any SKU string containing
    # 'x elite' / 'x1e'. Excludes anything already classified as X2 above.
    if (
        "family 8 model 1" in cpu_name
        or "x elite" in cpu_name
        or "x1e" in cpu_name
    ):
        return "snapdragon_x_elite"
    print(
        f"[{tag}] unknown CPU family; defaulting to snapdragon_x_elite",
        file=sys.stderr,
    )
    return "snapdragon_x_elite"


def download_with_progress(
    url: str,
    dest: Path,
    *,
    phase: str = "download",
    progress_cb: Callable[..., None] | None = None,
    timeout: float = 60.0,
) -> None:
    """Stream-download ``url`` to ``dest`` with periodic progress events.

    SSL cert verification is disabled (matches the QAI AppBuilder SampleApp
    behaviour; the AWS bucket cert chain occasionally fails verification on
    Windows ARM64). ``urlopen`` automatically routes through any
    ``HTTPS_PROXY`` / ``ALL_PROXY`` env var the apps/api wiring root injected
    for the global proxy (缺口 10); when none is set it connects directly.

    ``progress_cb`` is the runner's ``runner_protocol.progress`` callable
    (``progress(phase, pct, **extra)``); when ``None`` no events are emitted.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = Request(url, headers={"User-Agent": "Mozilla/5.0 qai-weight-downloader"})
    last_pct = -1.0
    chunk_size = 1024 * 256
    downloaded = 0
    try:
        with urlopen(req, context=ctx, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk_size)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total > 0 and progress_cb is not None:
                        pct = round(downloaded * 100.0 / total, 1)
                        if pct - last_pct >= 1.0 or pct >= 99.9:
                            progress_cb(
                                phase, pct,
                                downloadedBytes=downloaded, totalBytes=total,
                            )
                            last_pct = pct
    except Exception as e:  # noqa: BLE001 — surfaced as WEIGHTS_NOT_INSTALLED
        # Drop a partial file so a future run retries from scratch.
        try:
            if dest.is_file():
                dest.unlink()
        except OSError:
            pass
        raise WeightDownloadError(
            f"failed to download {url}: {e}. Check network connectivity / "
            f"proxy settings, or manually place the model files at "
            f"{dest.parent}."
        ) from e
    if total > 0 and downloaded < total:
        try:
            dest.unlink()
        except OSError:
            pass
        raise WeightDownloadError(
            f"download truncated ({downloaded}/{total} B from {url})"
        )


def ensure_weights_downloaded(
    model_dir: Path,
    *,
    download_configs: Mapping[str, Mapping[str, str]],
    required_files: Sequence[str],
    optional_files: Sequence[str] = (),
    tag: str = "weight_downloader",
    progress_cb: Callable[..., None] | None = None,
) -> None:
    """Ensure ``model_dir`` contains all ``required_files``.

    Downloads + extracts the device-specific zip from ``download_configs``
    (keyed by ``detect_device_model()``'s return value) when one or more
    required files are missing. Idempotent: a re-entry after a successful run
    short-circuits on the existence check and never touches the network.

    ``download_configs`` entries carry ``{"url", "archive_name",
    "extracted_dir"}`` (same shape the zipformer runner used). On any failure
    raises :class:`WeightDownloadError` (``WEIGHTS_NOT_INSTALLED``).
    """
    required = tuple(required_files)
    optional = tuple(optional_files)

    # Fast path: all required files already present.
    if all((model_dir / name).is_file() for name in required):
        return

    device_model = detect_device_model(tag=tag)
    cfg = (
        download_configs.get(device_model)
        or download_configs.get("snapdragon_x_elite")
    )
    if cfg is None:
        raise WeightDownloadError(
            f"no download config for device {device_model!r} (and no "
            "snapdragon_x_elite fallback). Manually place the model files at "
            f"{model_dir}."
        )

    model_dir.mkdir(parents=True, exist_ok=True)
    archive_path = model_dir / cfg["archive_name"]

    # 1. Download zip (reuse a cached archive from a previous failed run).
    if not archive_path.is_file():
        print(
            f"[{tag}] downloading {cfg['archive_name']} for {device_model}",
            file=sys.stderr,
        )
        download_with_progress(
            cfg["url"], archive_path, phase="download", progress_cb=progress_cb,
        )
    else:
        print(f"[{tag}] reusing cached archive {archive_path}", file=sys.stderr)

    # 2. Extract + copy required/optional files into model_dir (single shared
    #    extract implementation reused by any API-side downloader too).
    extract_weights_archive(
        archive_path,
        model_dir,
        extracted_dir=cfg["extracted_dir"],
        required_files=required,
        optional_files=optional,
        tag=tag,
        progress_cb=progress_cb,
    )

    print(f"[{tag}] model files ready at {model_dir}", file=sys.stderr)


def extract_weights_archive(
    archive_path: Path,
    model_dir: Path,
    *,
    extracted_dir: str,
    required_files: Sequence[str],
    optional_files: Sequence[str] = (),
    tag: str = "weight_downloader",
    progress_cb: Callable[..., None] | None = None,
) -> None:
    """Extract a downloaded weight archive and copy files into ``model_dir``.

    Shared post-download phase, split out of :func:`ensure_weights_downloaded`
    so the SAME unzip → locate → copy → verify implementation is reusable by
    both the runner subprocess path and a future API-side downloader (single
    extract implementation, no drift).

    Steps (byte-identical to the historical inline block):
      1. unzip ``archive_path`` into a ``temp_extract`` dir under ``model_dir``
         (corrupt archive → removed + :class:`WeightDownloadError`);
      2. locate the extracted source dir (``extracted_dir`` nesting, or the
         single top-level subdir, or the temp root as fallback);
      3. copy required + optional files into ``model_dir``;
      4. on success, clean up the temp dir + archive; if any required file is
         still missing, keep the archive and raise :class:`WeightDownloadError`.
    """
    required = tuple(required_files)
    optional = tuple(optional_files)
    temp_extract_dir = model_dir / "temp_extract"

    # 1. Extract zip into a temp dir under the model dir.
    print(f"[{tag}] extracting {archive_path}", file=sys.stderr)
    if temp_extract_dir.is_dir():
        shutil.rmtree(temp_extract_dir, ignore_errors=True)
    temp_extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.namelist()
            total_n = max(1, len(members))
            for idx, name in enumerate(members):
                zf.extract(name, temp_extract_dir)
                if progress_cb is not None and (
                    idx == total_n - 1 or idx % 5 == 0
                ):
                    progress_cb(
                        "extract", round((idx + 1) * 100.0 / total_n, 1),
                        extractedFiles=idx + 1, totalFiles=total_n,
                    )
    except zipfile.BadZipFile as e:
        try:
            archive_path.unlink()
        except OSError:
            pass
        shutil.rmtree(temp_extract_dir, ignore_errors=True)
        raise WeightDownloadError(
            f"downloaded archive is corrupt: {e}. The archive has been "
            "removed; re-run to download again."
        ) from e

    # 2. Locate the extracted source dir (most zips nest under the
    #    device-specific dir; some put files at the root).
    src_dir = temp_extract_dir / extracted_dir
    if not src_dir.is_dir():
        subdirs = [p for p in temp_extract_dir.iterdir() if p.is_dir()]
        if len(subdirs) == 1:
            src_dir = subdirs[0]
        else:
            src_dir = temp_extract_dir

    # 3. Copy required + optional files into the canonical model dir.
    for name in (*required, *optional):
        src = src_dir / name
        dst = model_dir / name
        if src.is_file():
            shutil.copy2(src, dst)

    # 4. Cleanup temp + archive (keep the archive only if extraction failed
    #    to land all required files, so the user can investigate).
    if all((model_dir / name).is_file() for name in required):
        shutil.rmtree(temp_extract_dir, ignore_errors=True)
        try:
            archive_path.unlink()
        except OSError:
            pass
    else:
        missing = [n for n in required if not (model_dir / n).is_file()]
        raise WeightDownloadError(
            f"extraction completed but required files are still missing: "
            f"{missing}. The archive at {archive_path} has been kept for "
            "inspection; please report this with the archive contents."
        )
