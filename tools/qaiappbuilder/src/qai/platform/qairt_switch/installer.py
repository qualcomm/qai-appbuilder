# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Compatible extension installer for the QAIRT SDK hot-switch (Task 4).

Ensures the ``qai_appbuilder`` extension installed into the runtime venv is
at least the minimum version compatible with a target QAIRT SDK release,
downloading and installing a replacement wheel from the fixed Qualcomm
GitHub Releases repository only when required.

Design constraints (Locked Contracts, ``docs/superpowers/plans/
2026-09-06-qairt-sdk-hot-switch.md``):

* **No in-process import** (Contract-adjacent safety): both the pre-check
  and the post-install verification run ``importlib.metadata.version(...)``
  in a FRESH ``python -c ...`` subprocess. The extension ships native DLLs;
  importing it into the long-lived FastAPI process would pin those DLLs
  open for the process lifetime and make a later reinstall fail with a
  Windows file-in-use error. A throwaway subprocess has no such lifetime.
* **Exactly four candidates** (Contract 9): ``target.appbuilder_candidates()``
  — never an unbounded "latest" search.
* **Fixed source, deterministic URLs** (Contract 10): every candidate's
  download URL is built from the same
  ``https://github.com/qualcomm/qai-appbuilder/releases/download/v{version}/
  {asset_name}`` template; there is no GitHub release-list API call and no
  caller-supplied URL/path/command.
* **404 advances, everything else stops** (Contract 11): a missing asset
  tries the next candidate; a download, install, or post-install
  verification failure is classified and the whole operation stops — it
  does NOT fall through to the next candidate. Locked Contract 8 forbids
  ever leaving a WORSE extension installed than what was there before, so a
  stopped operation immediately re-probes the pre-existing extension and,
  if a failed attempt disturbed it, reinstalls the original version from a
  validated cached or vendor wheel.

This module is independent of ``qai.app_builder.*`` (layering: platform must
not depend back on a bounded context) — it shares only
:mod:`qai.platform.package_mutation` (the mutation lock + pip error
classifier) and :mod:`qai.platform.process` (arch probe + process-tree
teardown) with the App Builder dependency checker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

import httpx

from qai.platform.package_mutation import PackageMutationLock, classify_pip_error
from qai.platform.process import current_arch, terminate_process_tree
from qai.platform.qairt_switch import (
    AppBuilderVersion,
    CandidateAttempt,
    CandidateOutcome,
    DownloadProgress,
    Phase,
    QairtVersion,
    UnsupportedPlatformError,
    appbuilder_wheel_asset_name,
)

__all__ = [
    "CompatibleExtensionInstaller",
    "ExtensionInstallError",
    "ExtensionInstallResult",
    "ExtensionRecoveryFailedError",
    "InstallOutcome",
]

#: The only CPython ABI this product ever installs a ``qai_appbuilder``
#: wheel for (Locked Contract 10).
_PYTHON_TAG = "cp313-cp313"

#: Maps :func:`qai.platform.process.current_arch` output to the wheel
#: platform tag. Any other value is rejected before network access.
_ARCH_TO_PLATFORM_TAG: dict[str, str] = {"arm64": "win_arm64", "x64": "win_amd64"}

#: Fixed Qualcomm GitHub Releases template shared by every candidate
#: (Locked Contract 10) -- confirmed against the pinned fixture
#: ``https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/
#: qai_appbuilder-2.48.40-cp313-cp313-win_{arm64|amd64}.whl``.
_RELEASE_URL_TEMPLATE = (
    "https://github.com/qualcomm/qai-appbuilder/releases/download/"
    "v{version}/{asset_name}"
)

#: Bounded stderr tail fed to :func:`classify_pip_error`, matching the
#: existing ``DynamicPackDepChecker`` convention.
_INSTALL_STDERR_TAIL_BYTES = 500

_INSPECT_SCRIPT = (
    "import importlib.metadata as _m, sys\n"
    "try:\n"
    "    sys.stdout.write(_m.version('qai-appbuilder'))\n"
    "except _m.PackageNotFoundError:\n"
    "    pass\n"
)


class InstallOutcome(StrEnum):
    """The two success shapes :meth:`CompatibleExtensionInstaller.ensure_compatible` returns."""

    #: The installed extension already satisfied the target; no network/install occurred.
    ALREADY_COMPATIBLE = "already_compatible"
    #: A candidate wheel was downloaded, installed, and verified.
    INSTALLED = "installed"


@dataclass(frozen=True, slots=True)
class ExtensionInstallResult:
    """Successful outcome of :meth:`CompatibleExtensionInstaller.ensure_compatible`."""

    outcome: InstallOutcome
    installed_version: AppBuilderVersion
    attempts: tuple[CandidateAttempt, ...]


class ExtensionInstallError(RuntimeError):
    """All bounded candidates failed or were absent; the extension was left compatible.

    ``attempts`` records every candidate tried (in order) with its outcome.
    ``error_kind``/``error_hint`` are the allowlisted classification of the
    terminal failure (the last non-missing failure, or ``"no_match"`` when
    every candidate was absent).
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: tuple[CandidateAttempt, ...],
        error_kind: str,
        error_hint: str,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.error_kind = error_kind
        self.error_hint = error_hint


class ExtensionRecoveryFailedError(ExtensionInstallError):
    """A failed install attempt disturbed the pre-existing extension AND restoring it also failed.

    Distinct from :class:`ExtensionInstallError` so a caller (the switch
    service) can surface the ``rollback_failed``-style state rather than a
    plain failure: the runtime venv may now hold neither the original nor a
    compatible extension.
    """


class _CandidateMissingError(Exception):
    """Internal signal: the candidate asset does not exist upstream (HTTP 404)."""


class _CandidateAttemptFailedError(Exception):
    """Internal signal: a candidate's download/install/verification step failed."""

    def __init__(self, kind: str, hint: str) -> None:
        super().__init__(hint)
        self.kind = kind
        self.hint = hint


def _is_valid_wheel_file(path: Path) -> bool:
    """Non-empty file that opens as a ZIP archive (Locked Contract, bullet 5)."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None

def _emit(callback: Callable[[Any], None] | None, value: Any) -> None:
    """Publish one synchronous installer event when a listener is present."""
    if callback is not None:
        callback(value)


def _validate_download_response(response: httpx.Response, asset_name: str) -> None:
    if response.status_code == httpx.codes.NOT_FOUND:
        raise _CandidateMissingError()
    if response.status_code != httpx.codes.OK:
        raise _CandidateAttemptFailedError(
            "http_error",
            f"Download failed with unexpected HTTP status {response.status_code} "
            f"for {asset_name}.",
        )


def _validate_downloaded_wheel(path: Path, asset_name: str, downloaded: int) -> None:
    if downloaded == 0:
        raise _CandidateAttemptFailedError(
            "empty_download", f"Downloaded 0 bytes for {asset_name}."
        )
    if not _is_valid_wheel_file(path):
        raise _CandidateAttemptFailedError(
            "corrupt_download",
            f"Downloaded file for {asset_name} is not a valid wheel archive.",
        )


class CompatibleExtensionInstaller:
    """Ensures a compatible ``qai_appbuilder`` extension for a target QAIRT SDK.

    Construct once per process with the runtime interpreter, an optional
    ``uv`` executable, a downloads directory, and the ONE DI-owned
    :class:`PackageMutationLock` shared with
    ``qai.app_builder.infrastructure.dep_checker.DynamicPackDepChecker` so
    the two never spawn a pip/uv child concurrently against the same venv.
    """

    __slots__ = (
        "_downloads_dir",
        "_http_client_factory",
        "_inspect_timeout_s",
        "_install_timeout_s",
        "_package_mutation_lock",
        "_python_exe",
        "_uv_exe",
        "_vendor_wheel_dir",
    )

    def __init__(
        self,
        *,
        python_exe: Path,
        uv_exe: Path | None,
        downloads_dir: Path,
        package_mutation_lock: PackageMutationLock,
        vendor_wheel_dir: Path | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        install_timeout_s: float = 300.0,
        inspect_timeout_s: float = 30.0,
    ) -> None:
        self._python_exe = python_exe
        self._uv_exe = uv_exe
        self._downloads_dir = downloads_dir
        self._package_mutation_lock = package_mutation_lock
        self._vendor_wheel_dir = vendor_wheel_dir
        self._http_client_factory = http_client_factory
        self._install_timeout_s = float(install_timeout_s)
        self._inspect_timeout_s = float(inspect_timeout_s)

    # ── public API ────────────────────────────────────────────────────

    async def inspect_installed_version(self) -> AppBuilderVersion | None:
        """Return the currently-installed extension version, or ``None``.

        Public wrapper around the same fresh-subprocess inspection
        :meth:`ensure_compatible` uses internally -- for callers (the
        preflight HTTP route) that only need a read, never an install.
        """
        return await self._inspect_installed_version()


    async def ensure_compatible(
        self,
        target: QairtVersion,
        *,
        on_phase: Callable[[Phase], None] | None = None,
        on_attempt: Callable[[CandidateAttempt], None] | None = None,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> ExtensionInstallResult:
        """Serialize the complete inspect/install/verify/recovery transaction.

        Holding the shared mutation lock across the initial version probe and
        any recovery prevents the dependency checker from changing the same
        runtime venv between those steps.
        """
        async with self._package_mutation_lock:
            return await self._ensure_compatible_locked(
                target,
                on_phase=on_phase,
                on_attempt=on_attempt,
                on_progress=on_progress,
            )

    async def _ensure_compatible_locked(
        self,
        target: QairtVersion,
        *,
        on_phase: Callable[[Phase], None] | None = None,
        on_attempt: Callable[[CandidateAttempt], None] | None = None,
        on_progress: Callable[[DownloadProgress], None] | None = None,
    ) -> ExtensionInstallResult:
        """Ensure the installed extension is ``>= target.minimum_appbuilder``.

        Returns immediately with :data:`InstallOutcome.ALREADY_COMPATIBLE`
        (no network access) when the installed extension already satisfies
        the target. Otherwise tries exactly the four bounded candidates in
        order; a 404 advances to the next candidate, any other failure stops
        the operation (after attempting to restore the pre-existing
        extension) and raises :class:`ExtensionInstallError` or
        :class:`ExtensionRecoveryFailedError`.
        """
        platform_tag = self._platform_tag()
        required = target.minimum_appbuilder
        original_version = await self._inspect_installed_version()
        if original_version is not None and original_version >= required:
            return ExtensionInstallResult(
                outcome=InstallOutcome.ALREADY_COMPATIBLE,
                installed_version=original_version,
                attempts=(),
            )

        attempts: list[CandidateAttempt] = []
        for candidate in target.appbuilder_candidates():
            _emit(on_phase, Phase.PROBING_CANDIDATE)
            asset_name = appbuilder_wheel_asset_name(
                candidate, python_tag=_PYTHON_TAG, platform_tag=platform_tag
            )
            url = _RELEASE_URL_TEMPLATE.format(version=candidate, asset_name=asset_name)

            try:
                _emit(on_phase, Phase.DOWNLOADING)
                wheel_path = await self._obtain_wheel(
                    asset_name=asset_name, url=url, on_progress=on_progress
                )
            except _CandidateMissingError:
                attempt = CandidateAttempt(
                    candidate=candidate,
                    outcome=CandidateOutcome.MISSING,
                    detail="asset not found upstream (HTTP 404)",
                )
                attempts.append(attempt)
                _emit(on_attempt, attempt)
                continue
            except _CandidateAttemptFailedError as exc:
                attempt = CandidateAttempt(
                    candidate=candidate, outcome=CandidateOutcome.FAILED, detail=exc.hint
                )
                attempts.append(attempt)
                _emit(on_attempt, attempt)
                await self._recover_or_raise(
                    original_version=original_version,
                    attempts=tuple(attempts),
                    error_kind=exc.kind,
                    error_hint=exc.hint,
                )

            try:
                _emit(on_phase, Phase.INSTALLING)
                await self._install_wheel(wheel_path)
                _emit(on_phase, Phase.VERIFYING_EXTENSION)
                installed = await self._inspect_installed_version()
            except _CandidateAttemptFailedError as exc:
                attempt = CandidateAttempt(
                    candidate=candidate, outcome=CandidateOutcome.FAILED, detail=exc.hint
                )
                attempts.append(attempt)
                _emit(on_attempt, attempt)
                await self._recover_or_raise(
                    original_version=original_version,
                    attempts=tuple(attempts),
                    error_kind=exc.kind,
                    error_hint=exc.hint,
                )

            if installed is None or installed < required:
                detail = (
                    f"post-install probe reports {installed} which does not "
                    f"satisfy the required minimum {required}"
                )
                attempt = CandidateAttempt(
                    candidate=candidate, outcome=CandidateOutcome.FAILED, detail=detail
                )
                attempts.append(attempt)
                _emit(on_attempt, attempt)
                await self._recover_or_raise(
                    original_version=original_version,
                    attempts=tuple(attempts),
                    error_kind="verification_failed",
                    error_hint=detail,
                )

            attempt = CandidateAttempt(
                candidate=candidate, outcome=CandidateOutcome.INSTALLED
            )
            attempts.append(attempt)
            _emit(on_attempt, attempt)
            return ExtensionInstallResult(
                outcome=InstallOutcome.INSTALLED,
                installed_version=installed,
                attempts=tuple(attempts),
            )

        raise ExtensionInstallError(
            f"No compatible qai_appbuilder release was found for QAIRT {target} "
            f"after trying all {len(attempts)} bounded candidates.",
            attempts=tuple(attempts),
            error_kind="no_match",
            error_hint=(
                "All bounded candidate versions were absent from the Qualcomm "
                "release repository."
            ),
        )

    # ── platform / arch policy ───────────────────────────────────────

    def _platform_tag(self) -> str:
        """Reject unsupported OS/architecture BEFORE any network access."""
        if sys.platform != "win32":
            raise UnsupportedPlatformError(
                f"qai_appbuilder extension install is Windows-only; "
                f"got sys.platform={sys.platform!r}"
            )
        arch = current_arch()
        tag = _ARCH_TO_PLATFORM_TAG.get(arch)
        if tag is None:
            raise UnsupportedPlatformError(
                f"Unsupported architecture for qai_appbuilder extension install: {arch!r}"
            )
        return tag

    # ── fresh-subprocess version inspection (never import in-process) ──

    async def _inspect_installed_version(self) -> AppBuilderVersion | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._python_exe),
                "-c",
                _INSPECT_SCRIPT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._inspect_timeout_s
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(terminate_process_tree(proc))
            raise
        except TimeoutError:
            with contextlib.suppress(Exception):
                await asyncio.shield(terminate_process_tree(proc))
            return None
        output = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not output:
            return None
        try:
            return AppBuilderVersion.parse(output)
        except ValueError:
            return None

    # ── download ──────────────────────────────────────────────────────

    def _open_http_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        # ``follow_redirects=True`` is required: GitHub Releases assets are
        # served via a redirect to an S3-backed CDN URL (Locked Contract,
        # bullet 4 -- normal HTTPS redirects, no host allowlisting).
        return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0))

    async def _obtain_wheel(
        self,
        *,
        asset_name: str,
        url: str,
        on_progress: Callable[[DownloadProgress], None] | None,
    ) -> Path:
        """Return a validated local wheel path, downloading only when uncached."""
        final_path = self._downloads_dir / asset_name
        if _is_valid_wheel_file(final_path):
            return final_path

        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._downloads_dir / f"{asset_name}.{uuid.uuid4().hex}.part"
        try:
            async with (
                self._open_http_client() as client,
                client.stream("GET", url) as response,
            ):
                _validate_download_response(response, asset_name)
                total = _content_length(response)
                downloaded = 0
                with tmp_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        _emit(
                            on_progress,
                            DownloadProgress(bytes_downloaded=downloaded, total_bytes=total),
                        )
            _validate_downloaded_wheel(tmp_path, asset_name, downloaded)
            os.replace(tmp_path, final_path)
        except (_CandidateMissingError, _CandidateAttemptFailedError):
            raise
        except httpx.HTTPError as exc:
            # Fixed, allowlisted hint — never interpolate str(exc) here.
            # httpx exception text can embed low-level transport details
            # (redirect chains, chunked-encoding state) beyond the asset
            # URL; SwitchDiagnostics.error_message must stay path-free and
            # allowlisted like every other classified error in this module
            # (mirrors classify_pip_error's fixed hints below).
            raise _CandidateAttemptFailedError(
                "network",
                f"Network error downloading {asset_name}. Check internet "
                "access and HTTP_PROXY / HTTPS_PROXY settings, then retry.",
            ) from exc
        except OSError as exc:
            kind, hint = classify_pip_error(str(exc))
            raise _CandidateAttemptFailedError(kind, hint) from exc
        finally:
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
        return final_path

    # ── install ───────────────────────────────────────────────────────

    def _build_install_cmd(self, wheel_path: Path) -> list[str]:
        if self._uv_exe is not None and self._uv_exe.is_file():
            return [
                str(self._uv_exe),
                "pip",
                "install",
                "--python",
                str(self._python_exe),
                "--native-tls",
                "--reinstall",
                "--no-deps",
                str(wheel_path),
            ]
        return [
            str(self._python_exe),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--reinstall",
            "--no-deps",
            str(wheel_path),
        ]

    async def _install_wheel(self, wheel_path: Path) -> None:
        cmd = self._build_install_cmd(wheel_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise _CandidateAttemptFailedError(
                "os_error", f"Failed to spawn extension install process: {exc}"
            ) from exc

        try:
            _stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._install_timeout_s
            )
        except asyncio.CancelledError:
            await asyncio.shield(terminate_process_tree(proc))
            raise
        except TimeoutError as exc:
            await asyncio.shield(terminate_process_tree(proc))
            raise _CandidateAttemptFailedError(
                "timeout",
                f"Extension install timed out after {self._install_timeout_s:.0f}s.",
            ) from exc
        except OSError as exc:
            await asyncio.shield(terminate_process_tree(proc))
            raise _CandidateAttemptFailedError(
                "stream_error", f"Extension install process stream failed: {exc}"
            ) from exc

        if proc.returncode != 0:
            tail = stderr_bytes.decode("utf-8", errors="replace")[-_INSTALL_STDERR_TAIL_BYTES:]
            kind, hint = classify_pip_error(tail)
            raise _CandidateAttemptFailedError(kind, hint)

    # ── failure recovery ──────────────────────────────────────────────

    async def _recover_or_raise(
        self,
        *,
        original_version: AppBuilderVersion | None,
        attempts: tuple[CandidateAttempt, ...],
        error_kind: str,
        error_hint: str,
    ) -> NoReturn:
        """Stop the operation: restore the pre-existing extension if disturbed, then raise.

        Always raises: :class:`ExtensionInstallError` when the pre-existing
        extension is intact (or was successfully restored), or
        :class:`ExtensionRecoveryFailedError` when a failed attempt left the
        venv without a working extension and no validated wheel exists to
        restore the original version.
        """
        current = await self._inspect_installed_version()
        if current == original_version:
            raise ExtensionInstallError(
                error_hint, attempts=attempts, error_kind=error_kind, error_hint=error_hint
            )
        if await self._restore_original(original_version):
            raise ExtensionInstallError(
                error_hint, attempts=attempts, error_kind=error_kind, error_hint=error_hint
            )
        raise ExtensionRecoveryFailedError(
            f"A failed install attempt left the qai_appbuilder extension in an "
            f"inconsistent state and restoring the original version "
            f"({original_version}) also failed.",
            attempts=attempts,
            error_kind="recovery_failed",
            error_hint=error_hint,
        )

    async def _restore_original(self, original_version: AppBuilderVersion | None) -> bool:
        """Best-effort reinstall of ``original_version`` from a cached or vendor wheel.

        Called only by :meth:`_ensure_compatible_locked`, so the shared
        package-mutation lock is already held for this recovery attempt.
        """
        if original_version is None:
            return False
        platform_tag = self._platform_tag()
        asset_name = appbuilder_wheel_asset_name(
            original_version, python_tag=_PYTHON_TAG, platform_tag=platform_tag
        )
        candidate_paths = [self._downloads_dir / asset_name]
        if self._vendor_wheel_dir is not None:
            candidate_paths.append(self._vendor_wheel_dir / asset_name)
        wheel_path = next((p for p in candidate_paths if _is_valid_wheel_file(p)), None)
        if wheel_path is None:
            return False
        try:
            await self._install_wheel(wheel_path)
        except _CandidateAttemptFailedError:
            return False
        restored = await self._inspect_installed_version()
        return restored == original_version
