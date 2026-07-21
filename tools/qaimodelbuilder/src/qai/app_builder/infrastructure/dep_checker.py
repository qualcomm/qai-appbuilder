# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Dynamic pack dependency checker / installer (PR-094 §17.5 #11).

Restores the legacy ``backend/app_builder/dep_checker.py`` (494 LOC) as an
infrastructure adapter. The S9 audit (§3.3 A-14) flagged the loss of the
two-layer dependency strategy:

* Layer 1 (install-time): ``Setup.bat`` pre-installs all Pack
  deps into the shared ARM64 venv;
* Layer 2 (this module): on the first hit of ``GET /api/app-builder/models``
  we fan out a background ``asyncio.Task`` that probes each enabled Pack's
  ``requirements.txt`` against the venv via ``importlib.util.find_spec``,
  and if anything is missing (e.g. the user dropped a new Pack into
  ``data/app_builder/`` without re-running setup) we silently
  ``pip install`` it.

Without Layer 2 a freshly-dropped Pack throws ``ImportError`` deep inside
the runner subprocess, which surfaces as a generic "run failed" badge in
the UI rather than the specific "deps not installed" hint the legacy
backend produced.

This adapter is gated by ``Settings.app_builder.dep_checker_enabled``
(``qai.platform.config.settings.AppBuilderSettings``); when disabled the
class is a no-op so embedded / air-gapped deployments keep their
deterministic startup.

Cross-cutting notes:

* The pip / uv subprocess spawn intentionally goes through
  :class:`asyncio.create_subprocess_exec` (NOT through
  ``qai.platform.process.ProcessRunnerPort``), because dependency
  installation is a *trusted, host-process* operation.  Historically the
  ProcessRunnerPort wrapper routed commands through the AppContainer
  sandbox (which blocked writes to ``site-packages`` and broke pip);
  that sandbox execution chain was removed on 2026-07-01 (see
  ``docs/85-tasks/windows-acl-sandbox-cleanup-2026-07-01.md``) but the
  bypass to ``asyncio.create_subprocess_exec`` is kept intentionally so
  future file-protection layers (Protected Paths / PatternFileScreen /
  the pending native-hook layer) do not accidentally re-block pip.  The
  PEP 578 audit hook treats this subprocess as ``trusted_subprocess``
  via the existing ``qai.security.adapters.audit_hook`` allow-list.
* The cool-down (``_CHECK_COOLDOWN_SEC = 300``) and the asyncio.Lock
  serialisation on pip install are preserved from the legacy module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("qai.app_builder.dep_checker")

__all__ = [
    "DepStatus",
    "DynamicPackDepChecker",
    "PackDepDescriptor",
]


# ---------------------------------------------------------------------------
# Constants — preserved verbatim from legacy backend/app_builder/dep_checker.py
# ---------------------------------------------------------------------------
_CHECK_COOLDOWN_SEC: float = 300.0

# Known package-name → import-name overrides.
_IMPORT_NAME_MAP: dict[str, str] = {
    "pillow": "PIL",
    "opencv-python-headless": "cv2",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "openai-whisper": "whisper",
    "soundfile": "soundfile",
    "sentencepiece": "sentencepiece",
    "scikit-learn": "sklearn",
    "python-dateutil": "dateutil",
}

# Vendor-supplied wheels that must never be auto-fetched from PyPI.
_SKIP_PKGS: frozenset[str] = frozenset({"qai-appbuilder", "qai_appbuilder"})

# Packages that MUST be installed with ``--no-deps`` (PEP 503 normalized names).
# Mirrors ``scripts/setup/_pack_deps.NO_DEPS_PKGS`` — kept as an independent
# literal here because ``src/qai`` must not import ``scripts/`` (import-linter
# context isolation). ``openai-whisper`` only supplies the offline
# ``assets/gpt2.tiktoken`` vocab (the runner never imports it); pulling its
# transitive deps (numba / llvmlite) has no ARM64 Windows wheel and fails.
_NO_DEPS_PKGS: frozenset[str] = frozenset({"openai-whisper"})


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PackDepDescriptor:
    """Identifies one Pack and the requirements file we should probe.

    ``requirements_path`` is an absolute :class:`pathlib.Path` pointing at
    the Pack's ``requirements.txt``. ``model_id`` is the Pack's domain
    identifier — used as the cache key in ``DepStatus`` reporting.
    """

    model_id: str
    requirements_path: Path


@dataclass(slots=True, kw_only=True)
class DepStatus:
    """Mutable status report for one Pack.

    The legacy module published this as a dict; we keep the same field
    shape (camelCase preserved for the front-end contract) but type it
    as a dataclass for clarity inside the adapter.
    """

    satisfied: bool
    missing: list[str] = field(default_factory=list)
    installing: bool = False
    error_kind: str | None = None
    error_hint: str | None = None
    error_raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "satisfied": self.satisfied,
            "missing": list(self.missing),
            "installing": self.installing,
        }
        if self.error_kind is not None:
            out["errorKind"] = self.error_kind
            out["errorHint"] = self.error_hint or ""
            out["errorRaw"] = self.error_raw or ""
        return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class DynamicPackDepChecker:
    """Background dependency probe + auto-installer for App Builder Packs.

    Construct once per process; each App Builder UI hit calls
    :meth:`ensure` with the current set of :class:`PackDepDescriptor`
    entries. Subsequent calls within the cool-down window return
    immediately. The class is asyncio-friendly: ``ensure`` returns the
    background task so callers may either fire-and-forget or await
    completion in tests.
    """

    __slots__ = (
        "_check_task",
        "_enabled",
        "_install_lock",
        "_last_check_time",
        "_python_exe",
        "_status",
        "_uv_exe",
    )

    def __init__(
        self,
        *,
        python_exe: Path,
        uv_exe: Path | None = None,
        enabled: bool = True,
    ) -> None:
        if not isinstance(python_exe, Path):
            raise TypeError(
                f"python_exe must be Path, got {type(python_exe).__name__}"
            )
        self._python_exe = python_exe
        self._uv_exe = uv_exe
        self._enabled = bool(enabled)
        self._status: dict[str, DepStatus] = {}
        self._check_task: asyncio.Task[None] | None = None
        self._install_lock: asyncio.Lock | None = None
        self._last_check_time: float = 0.0

    # ── public API ────────────────────────────────────────────────────

    def ensure(
        self,
        pack_id: str,
        deps: list[str],
    ) -> asyncio.Task[None] | None:
        """Synchronous entry — schedule a single-pack probe + install.

        Returns the asyncio.Task driving the work, or ``None`` when the
        adapter is disabled by settings.
        """
        if not self._enabled:
            return None
        if not isinstance(pack_id, str) or not pack_id:
            raise ValueError("pack_id must be a non-empty string")
        if not isinstance(deps, list):
            raise TypeError(
                f"deps must be list[str], got {type(deps).__name__}"
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "dep_checker.ensure(%r): no running event loop", pack_id
            )
            return None
        return loop.create_task(
            self._ensure_one(pack_id, list(deps)),
            name=f"app_builder_dep_check[{pack_id}]",
        )

    def trigger_background_check(
        self, packs: list[PackDepDescriptor]
    ) -> asyncio.Task[None] | None:
        """Fire-and-forget background probe for every pack in ``packs``.

        Mirrors legacy ``trigger_background_check``. Returns ``None`` if a
        check is already in flight, the cool-down hasn't elapsed, or the
        adapter is disabled.
        """
        if not self._enabled:
            return None
        if self._check_task is not None and not self._check_task.done():
            return None
        if (
            self._last_check_time > 0
            and (time.monotonic() - self._last_check_time)
            < _CHECK_COOLDOWN_SEC
        ):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "dep_checker.trigger_background_check: no event loop"
            )
            return None
        self._check_task = loop.create_task(
            self._check_and_install_all(list(packs)),
            name="app_builder_dep_check",
        )
        self._check_task.add_done_callback(self._on_task_done)
        return self._check_task

    def get_status(self, pack_id: str) -> DepStatus | None:
        return self._status.get(pack_id)

    def get_all_status(self) -> dict[str, DepStatus]:
        return dict(self._status)

    def is_checking(self) -> bool:
        return self._check_task is not None and not self._check_task.done()

    def get_progress(self) -> dict[str, Any]:
        """Per-pack dependency progress in the V1 wire shape.

        Mirrors the legacy ``GET /api/appbuilder/deps-status`` body
        (``backend/app_builder/api_routes.py:835-845``) so the front-end
        gallery can render the "installing → ready / missing + error hint"
        badge exactly as V1's ``useAppBuilderRegistry.js:269-342``
        ``pollDepsStatus`` did::

            {
              "checking": bool,                       # a background check in flight
              "packs": {                              # one entry per probed pack
                "<model_id>": {
                  "satisfied": bool,
                  "missing": [...],
                  "installing": bool,
                  "errorKind"?: str, "errorHint"?: str, "errorRaw"?: str,
                }
              }
            }

        State-Truth-First (AGENTS.md §🔴): the per-pack rows come straight
        from :class:`DepStatus.to_dict` (the checker's real ``find_spec``
        probe + pip/uv install outcome) — never a fabricated "ready". Packs
        not yet probed simply do not appear (front-end treats them as
        unknown/checking, same as V1 omitting the row).
        """
        return {
            "checking": self.is_checking(),
            "packs": {
                pack_id: status.to_dict()
                for pack_id, status in self._status.items()
            },
        }

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "dep_checker background task failed: %s", exc, exc_info=exc
            )

    async def _get_install_lock(self) -> asyncio.Lock:
        if self._install_lock is None:
            self._install_lock = asyncio.Lock()
        return self._install_lock

    async def _ensure_one(
        self, pack_id: str, deps: list[str]
    ) -> None:
        if not deps:
            self._status[pack_id] = DepStatus(satisfied=True)
            return
        try:
            missing = await self._check_imports(deps)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dep check failed for pack %r: %s", pack_id, exc)
            return
        if not missing:
            self._status[pack_id] = DepStatus(satisfied=True)
            return
        self._status[pack_id] = DepStatus(
            satisfied=False, missing=missing, installing=True
        )
        try:
            await self._auto_install(pack_id, missing)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "auto-install exception for pack %r: %s", pack_id, exc
            )
            self._status[pack_id] = DepStatus(
                satisfied=False, missing=missing, installing=False
            )

    async def _check_and_install_all(
        self, packs: list[PackDepDescriptor]
    ) -> None:
        for pack in packs:
            req_path = pack.requirements_path
            if not req_path.is_file():
                self._status[pack.model_id] = DepStatus(satisfied=True)
                continue
            requirements = self._parse_requirements(req_path)
            if not requirements:
                self._status[pack.model_id] = DepStatus(satisfied=True)
                continue
            await self._ensure_one(pack.model_id, requirements)
        self._last_check_time = time.monotonic()

    async def _check_imports(self, requirements: list[str]) -> list[str]:
        pairs: list[tuple[str, str]] = []
        for spec in requirements:
            pkg_name = self._extract_pkg_name(spec)
            norm = self._normalize_name(pkg_name)
            if norm in _SKIP_PKGS:
                continue
            import_name = _IMPORT_NAME_MAP.get(
                norm, pkg_name.replace("-", "_").replace(".", "_")
            )
            pairs.append((spec, import_name))
        if not pairs:
            return []
        check_lines = ["import importlib.util as _u, json as _j", "_r={}"]
        for _, imp in pairs:
            safe_imp = imp.replace("'", "\\'")
            check_lines.append(
                f"_r['{safe_imp}']=_u.find_spec('{safe_imp}') is not None"
            )
        check_lines.append("print(_j.dumps(_r))")
        script = "; ".join(check_lines)

        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._python_exe),
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
            output = stdout_bytes.decode("utf-8", errors="replace").strip()
            if not output:
                return [spec for spec, _ in pairs]
            result = json.loads(output)
        except TimeoutError:
            logger.warning("dep check subprocess timed out")
            return [spec for spec, _ in pairs]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("dep check subprocess failed: %s", exc)
            return [spec for spec, _ in pairs]

        missing: list[str] = []
        for spec, imp in pairs:
            if not result.get(imp, False):
                missing.append(spec)
        return missing

    async def _auto_install(self, pack_id: str, missing: list[str]) -> None:
        lock = await self._get_install_lock()
        async with lock:
            logger.info(
                "dep_checker: installing deps for pack %r: %s",
                pack_id,
                missing,
            )
            # Partition into ``--no-deps`` packages (e.g. openai-whisper, whose
            # transitive numba/llvmlite have no ARM64 wheel) and the rest, so
            # each group gets the correct pip invocation. Mirrors the
            # install-time aggregator (scripts/setup/_pack_deps).
            no_deps_specs = [
                s for s in missing
                if self._normalize_name(self._extract_pkg_name(s)) in _NO_DEPS_PKGS
            ]
            normal_specs = [s for s in missing if s not in no_deps_specs]

            try:
                rc = 0
                err_msg = ""
                for specs, use_no_deps in (
                    (no_deps_specs, True),
                    (normal_specs, False),
                ):
                    if not specs:
                        continue
                    cmd = self._build_install_cmd(specs, no_deps=use_no_deps)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=300
                    )
                    if proc.returncode != 0:
                        rc = proc.returncode or 1
                        err_msg = stderr_bytes.decode(
                            "utf-8", errors="replace"
                        )[-500:]
                        # Stop at the first failing group; remaining groups are
                        # reflected by the re-probe below.
                        break

                if rc == 0:
                    self._status[pack_id] = DepStatus(satisfied=True)
                    return
                err_kind, err_hint = self._classify_pip_error(err_msg)
                still_missing = await self._check_imports(missing)
                self._status[pack_id] = DepStatus(
                    satisfied=len(still_missing) == 0,
                    missing=still_missing,
                    installing=False,
                    error_kind=err_kind,
                    error_hint=err_hint,
                    error_raw=err_msg.strip(),
                )
            except TimeoutError:
                self._status[pack_id] = DepStatus(
                    satisfied=False,
                    missing=missing,
                    installing=False,
                    error_kind="timeout",
                    error_hint=(
                        "Dependency install timed out after 5 minutes. "
                        "Check your network connection and try again."
                    ),
                    error_raw="pip install timed out (300s)",
                )
            except OSError as exc:
                self._status[pack_id] = DepStatus(
                    satisfied=False,
                    missing=missing,
                    installing=False,
                    error_kind="os_error",
                    error_hint=(
                        f"Failed to spawn pip process: {exc}. "
                        "Check that the ARM64 venv is correctly configured."
                    ),
                    error_raw=str(exc),
                )

    def _build_install_cmd(
        self, missing: list[str], *, no_deps: bool = False
    ) -> list[str]:
        no_deps_flag = ["--no-deps"] if no_deps else []
        if self._uv_exe is not None and self._uv_exe.is_file():
            return [
                str(self._uv_exe),
                "pip",
                "install",
                "--python",
                str(self._python_exe),
                "--native-tls",
                "--allow-insecure-host",
                "pypi.org",
                "--allow-insecure-host",
                "files.pythonhosted.org",
                *no_deps_flag,
                *missing,
            ]
        return [
            str(self._python_exe),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--trusted-host",
            "pypi.org",
            "--trusted-host",
            "files.pythonhosted.org",
            "--trusted-host",
            "pypi.python.org",
            *no_deps_flag,
            *missing,
        ]

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_requirements(req_path: Path) -> list[str]:
        try:
            text = req_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read %s: %s", req_path, exc)
            return []
        out: list[str] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            out.append(line)
        return out

    @staticmethod
    def _extract_pkg_name(spec: str) -> str:
        s = spec.strip()
        for i, ch in enumerate(s):
            if ch in "<>=!~;[":
                return s[:i].strip()
        return s

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).strip().lower()

    @staticmethod
    def _classify_pip_error(stderr: str) -> tuple[str, str]:
        s = stderr.lower() if stderr else ""
        if (
            "invalid peer certificate" in s
            or "unknownissuer" in s
            or "ssl: certificate_verify_failed" in s
            or "self signed certificate" in s
            or "certificate verify failed" in s
        ):
            return (
                "tls_cert",
                "TLS certificate verification failed when contacting pypi.org. "
                "Add your corporate root CA to Python's trust store, or set "
                "REQUESTS_CA_BUNDLE / SSL_CERT_FILE.",
            )
        if (
            "failed to fetch" in s
            or "could not fetch" in s
            or "connection refused" in s
            or "name or service not known" in s
            or "temporary failure in name resolution" in s
            or "network is unreachable" in s
            or "no route to host" in s
            or "failed establishing a new connection" in s
        ):
            return (
                "network",
                "Network connection failed when contacting pypi.org. "
                "Check internet access and HTTP_PROXY / HTTPS_PROXY settings.",
            )
        if (
            "no matching distribution" in s
            or "could not find a version" in s
            or ("no version of" in s and "satisfies" in s)
        ):
            return (
                "no_match",
                "The required package version could not be found on PyPI for "
                "this Python interpreter (likely an ARM64 wheel availability "
                "issue).",
            )
        if (
            "permission denied" in s
            or "operation not permitted" in s
            or "[winerror 5]" in s
        ):
            return (
                "permission",
                "Permission denied while writing to the venv. Close any "
                "process using the venv and retry.",
            )
        if "no space left" in s or "disk full" in s or "[errno 28]" in s:
            return (
                "disk_full",
                "Disk is full. Free up space on the drive containing the "
                "ARM64 venv and retry.",
            )
        if "read timed out" in s or "timeout" in s:
            return (
                "timeout",
                "Pip request timed out. Check your network speed.",
            )
        return (
            "unknown",
            "Dependency installation failed with an unrecognized error. "
            "See the raw stderr below for details.",
        )
