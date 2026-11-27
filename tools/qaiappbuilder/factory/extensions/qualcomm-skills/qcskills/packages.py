# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Qualcomm skill packages: unpack, recognise, install, list, remove.

A Qualcomm external-skill delivery is a ZIP that may nest further ZIPs
(``HS11-70030-3.zip`` -> ``..._rev_1.1.zip`` -> ``..._Authorized.zip``). The
innermost level carries a README and a wheel whose package embeds an MCP
server (``<pkg>/server.py``) with all the skills compiled in. The app does
not unpack the skills: it installs the wheel into the package's own
directory (never into the app's environment) and runs the server as an MCP
server, so the model looks skills up through it (``search_skills`` ->
``get_skill``) instead of every skill being loaded into the context.

Uses the stdlib and ``packaging`` (plus ``pip`` as a subprocess), so it can
be tested without the app.

Layout (``<data>/extensions-data/qualcomm-skills/``)::

    packages/<package id>/
        site/             pip install --target (the wheel + its dependencies)
        README.md         as shipped, shown in the panel
        package.json      what was installed, from where, how to start it
    staging/<token>/      an upload being previewed (deleted after 1 h)
"""

from __future__ import annotations

import email.parser
import hashlib
import importlib.metadata as md
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

__all__ = [
    "MAX_NESTING",
    "PackageError",
    "Packages",
    "WheelInfo",
    "find_package",
]

#: Archive limits (counted while unpacking: header sizes cannot be trusted).
MAX_NESTING = 4
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 800 * 1024 * 1024
STAGING_TTL_S = 3600.0
PIP_TIMEOUT_S = 900.0
_README_MAX = 200_000
_README_MAX_PARTS = 2
_RM_ATTEMPTS = 20

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_TOKEN_RE = re.compile(r"^[a-f0-9]{24}$")
#: PEP 427 file name: {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl
_WHEEL_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$"
)


class PackageError(ValueError):
    """``code`` is machine-readable; the message is shown to the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WheelInfo:
    """What a Qualcomm MCP wheel is and how to start its server."""

    filename: str
    name: str  # distribution name, e.g. "dragonwing-windows-mcp"
    version: str
    module: str  # import package, e.g. "dragonwing_windows_mcp"
    server_module: str  # e.g. "dragonwing_windows_mcp.server"
    server_args: tuple[str, ...]  # e.g. () or ("--stdio",)
    summary: str
    author: str
    requires: tuple[str, ...]
    requires_python: str
    skill_count: int  # SKILL.md files embedded (0 when it ships no skills)
    sha256: str

    @property
    def package_id(self) -> str:
        return _normalise(f"{self.name}-{self.version}")


@dataclass(slots=True)
class Found:
    """The innermost level of a delivery, once unpacked."""

    wheel_path: Path
    wheel: WheelInfo
    readme: str
    nesting: list[str] = field(default_factory=list)  # archive names, outer -> inner
    documents: list[str] = field(default_factory=list)  # PDFs and the like, by name


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-.")[:100]


# -- unpacking ---------------------------------------------------------------------


def _safe_name(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise PackageError("qcskills.unsafe_archive", f"{name!r} is an absolute path")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PackageError("qcskills.unsafe_archive", f"{name!r} escapes the archive")
    if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
        return None
    return PurePosixPath(*parts)


class _Budget:
    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0

    def add_file(self) -> None:
        self.files += 1
        if self.files > MAX_FILES:
            raise PackageError("qcskills.too_large", f"more than {MAX_FILES} files in the delivery")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.bytes > MAX_TOTAL_BYTES:
            raise PackageError(
                "qcskills.too_large", f"more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB unpacked"
            )


#: The only files written to disk: what installing and showing a package needs.
#: The loose ``*-skills/`` tree a delivery also carries is never extracted (the
#: wheel embeds the same skills), which also keeps paths far below Windows'
#: 260-character limit (that tree nests deeply).
_KEEP_SUFFIXES = (".whl",)
_README_RE = re.compile(r"^readme(\.[a-z0-9]+)?$", re.IGNORECASE)
_DOC_SUFFIXES = (".pdf",)


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget) -> bytes:
    chunks: list[bytes] = []
    with zf.open(info) as src:
        while chunk := src.read(1 << 16):
            budget.add_bytes(len(chunk))
            chunks.append(chunk)
    return b"".join(chunks)


def _walk(
    zf: zipfile.ZipFile,
    dest: Path,
    budget: _Budget,
    seen: list[str],
    documents: list[str],
    *,
    depth: int,
) -> None:
    """Check every member, descend into nested ZIPs (in memory), and write
    the wheels and READMEs of this level to ``dest/<level>/``."""
    level = dest / f"{len(seen):02d}"
    for info in zf.infolist():
        rel = _safe_name(info.filename)
        if rel is None or info.is_dir():
            continue
        if stat.S_ISLNK(info.external_attr >> 16):
            raise PackageError("qcskills.unsafe_archive", f"{info.filename!r} is a symbolic link")
        budget.add_file()
        leaf = rel.name
        low = leaf.lower()
        if low.endswith(".zip"):
            data = _read_member(zf, info, budget)
            try:
                inner = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                continue  # only looks like a zip: ignore it
            if depth + 1 >= MAX_NESTING:
                raise PackageError(
                    "qcskills.too_deep", f"ZIP files nested deeper than {MAX_NESTING} levels"
                )
            seen.append(leaf)
            with inner:
                _walk(inner, dest, budget, seen, documents, depth=depth + 1)
        elif low.endswith(_KEEP_SUFFIXES) or (
            _README_RE.match(leaf) and len(rel.parts) <= _README_MAX_PARTS
        ):
            # Flat: a short path regardless of how deep the member sat.
            target = level / _flat_name(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_read_member(zf, info, budget))
        else:
            budget.add_bytes(info.file_size)  # counted (bomb guard), not written
            if low.endswith(_DOC_SUFFIXES):
                documents.append(leaf)


def _flat_name(rel: PurePosixPath) -> str:
    """``a/b/README.md`` -> ``a__b__README.md`` (keeps two READMEs apart)."""
    # Preserve wheel basename: pip validates it against METADATA. A short
    # hashed directory distinguishes paths without copying the deep source tree.
    return hashlib.sha256(str(rel.parent).encode()).hexdigest()[:8] + "/" + rel.name


def unpack(archive: Path | io.BytesIO, dest: Path) -> tuple[list[str], list[str]]:
    """Walk ``archive`` and its nested ZIPs (up to :data:`MAX_NESTING` levels).

    Only wheels and READMEs are written, under ``dest/<level>/``; everything
    else is checked and counted but not extracted. Returns ``(archive names,
    outer first; document names)``.
    """
    budget = _Budget()
    seen: list[str] = [getattr(archive, "name", "") or "upload.zip"]
    documents: list[str] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            _walk(zf, dest, budget, seen, documents, depth=0)
    except zipfile.BadZipFile as exc:
        raise PackageError("qcskills.bad_archive", f"not a valid ZIP file: {exc}") from None
    except (EOFError, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
        raise PackageError("qcskills.bad_archive", f"cannot read the ZIP file: {exc}") from None
    return seen, sorted(documents)


# -- recognising a Qualcomm skill package ------------------------------------------


def read_wheel(path: Path) -> WheelInfo:
    """Inspect a wheel without installing it. :class:`PackageError` if it is
    not a Qualcomm MCP package (no ``<pkg>/server.py``)."""
    match = _WHEEL_RE.match(path.name)
    if not match:
        raise PackageError("qcskills.not_a_wheel", f"{path.name!r} is not a wheel file name")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise PackageError("qcskills.not_a_wheel", f"{path.name!r} is not a valid wheel") from None
    with zf:
        names = zf.namelist()
        meta_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if meta_name is None:
            raise PackageError("qcskills.not_a_wheel", f"{path.name!r} has no METADATA")
        meta = email.parser.Parser().parsestr(zf.read(meta_name).decode("utf-8", "replace"))
        servers = sorted(
            n
            for n in names
            if n.count("/") == 1 and n.endswith("/server.py") and ".dist-info" not in n
        )
        if not servers:
            raise PackageError(
                "qcskills.not_mcp",
                f"{path.name!r} has no MCP server (<package>/server.py): "
                "not a Qualcomm skill package",
            )
        module = servers[0].split("/", 1)[0]
        source = zf.read(servers[0]).decode("utf-8", "replace")
        skill_count = sum(1 for n in names if n.endswith("/SKILL.md"))
    # Generated servers run stdio by default; the older hand-written ones want --stdio.
    args: tuple[str, ...] = ("--stdio",) if re.search(r"""['"]--stdio['"]""", source) else ()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return WheelInfo(
        filename=path.name,
        name=str(meta.get("Name") or match.group("name")),
        version=str(meta.get("Version") or match.group("version")),
        module=module,
        server_module=f"{module}.server",
        server_args=args,
        summary=str(meta.get("Summary") or ""),
        author=str(meta.get("Author") or meta.get("Author-email") or ""),
        requires=tuple(r for r in (meta.get_all("Requires-Dist") or []) if "extra ==" not in r),
        requires_python=str(meta.get("Requires-Python") or ""),
        skill_count=skill_count,
        sha256=digest,
    )


def _is_qualcomm(wheel: WheelInfo, readme: str) -> bool:
    text = f"{wheel.author} {wheel.summary} {readme}".lower()
    return "qualcomm" in text or "qcom" in text or "dragonwing" in text or "snapdragon" in text


def find_package(root: Path, nesting: list[str], documents: list[str] | None = None) -> Found:
    """The innermost directory holding a README and an MCP wheel.

    Several wheels (e.g. many versions side by side): the newest version of the
    first distribution found is used.
    """
    wheels: list[tuple[Path, WheelInfo]] = []
    errors: list[str] = []
    for path in sorted(root.rglob("*.whl")):
        try:
            wheels.append((path, read_wheel(path)))
        except PackageError as exc:
            errors.append(str(exc))
    if not wheels:
        detail = f" ({'; '.join(errors[:3])})" if errors else ""
        raise PackageError(
            "qcskills.no_wheel",
            "no Qualcomm MCP wheel found in the ZIP (expected a .whl with an MCP server)" + detail,
        )
    by_name: dict[str, list[tuple[Path, WheelInfo]]] = {}
    for item in wheels:
        by_name.setdefault(item[1].name, []).append(item)
    name = sorted(by_name)[0]
    path, info = max(by_name[name], key=lambda it: _version_key(it[1].version))
    # A README written at the same level as the wheel (unpack() flattens names).
    readme_path = next(
        (p for p in sorted(path.parent.iterdir()) if p.is_file() and _README_RE.match(p.name)),
        None,
    )
    readme = ""
    if readme_path is not None:
        readme = readme_path.read_text(encoding="utf-8", errors="replace")[:_README_MAX]
    if not _is_qualcomm(info, readme):
        raise PackageError(
            "qcskills.not_qualcomm",
            f"{info.filename} does not look like a Qualcomm package (author {info.author!r}); "
            "add your own skills through the Skills page instead",
        )
    return Found(
        wheel_path=path, wheel=info, readme=readme, nesting=nesting, documents=documents or []
    )


def _version_key(version: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", version))


# -- the package store -------------------------------------------------------------


Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv (sys.executable -m pip ...)
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONIOENCODING": "utf-8"},
    )


class Packages:
    """The installed packages under ``root`` (= the extension's data dir)."""

    def __init__(
        self,
        root: Path,
        *,
        python: str = sys.executable,
        runner: Runner = _run,
        clock: Callable[[], float] = time.time,
        proxy_url: Callable[[], str | None] | None = None,
    ) -> None:
        self.root = root
        self.packages_dir = root / "packages"
        self.staging_dir = root / "staging"
        self._python = python
        self._runner = runner
        self._clock = clock
        self._proxy_url = proxy_url or (lambda: None)

    # -- listing ---------------------------------------------------------------

    def installed(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.packages_dir.is_dir():
            return out
        for d in sorted(self.packages_dir.iterdir()):
            meta = d / "package.json"
            if not d.name.startswith(".") and d.is_dir() and meta.is_file():
                try:
                    out.append(json.loads(meta.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return out

    def readme(self, package_id: str) -> str:
        path = self._package_dir(package_id) / "README.md"
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def server_specs(self) -> list[dict[str, Any]]:
        """``{name, command, args, env}`` for every installed package's server."""
        specs: list[dict[str, Any]] = []
        for pkg in self.installed():
            site = self.packages_dir / pkg["id"] / "site"
            if not site.is_dir():
                continue
            specs.append(
                {
                    "name": pkg["server_name"],
                    "command": self._python,
                    "args": ["-m", pkg["server_module"], *pkg.get("server_args", [])],
                    "env": {
                        # Only this package's own directory: nothing leaks in from
                        # (or out to) the app's environment or the user's site.
                        "PYTHONPATH": str(site),
                        "PYTHONNOUSERSITE": "1",
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONUTF8": "1",
                    },
                }
            )
        return specs

    # -- preview ----------------------------------------------------------------

    def stage(self, data: bytes, filename: str = "upload.zip") -> dict[str, Any]:
        """Unpack an uploaded delivery and describe it. Installs nothing."""
        self._sweep()
        token = secrets.token_hex(12)
        work = self.staging_dir / token
        raw = work / "unpacked"
        raw.mkdir(parents=True)
        try:
            buf = io.BytesIO(data)
            buf.name = filename
            nesting, documents = unpack(buf, raw)
            found = find_package(raw, nesting, documents)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        (work / "staged.json").write_text(
            json.dumps({"wheel": str(found.wheel_path.relative_to(work)), "filename": filename}),
            encoding="utf-8",
        )
        return {"token": token, **self._describe(found, filename)}

    def _describe(self, found: Found, filename: str) -> dict[str, Any]:
        w = found.wheel
        installed = {p["id"]: p for p in self.installed()}
        same_name = [p for p in installed.values() if p["name"] == w.name]
        return {
            "source": filename,
            "nesting": found.nesting,
            "id": w.package_id,
            "name": w.name,
            "version": w.version,
            "summary": w.summary,
            "author": w.author,
            "wheel": w.filename,
            "sha256": w.sha256,
            "skill_count": w.skill_count,
            "server": f"{w.server_module} {' '.join(w.server_args)}".strip(),
            "requires": list(w.requires),
            "missing": self.missing_requirements(w.requires),
            "requires_python": w.requires_python,
            "python_ok": _python_ok(w.requires_python),
            "documents": found.documents,
            "readme": found.readme,
            "replaces": [p["version"] for p in same_name],
        }

    # -- install / remove -----------------------------------------------------

    def install(  # noqa: PLR0912, PLR0915 - transactional pip install and rollback
        self, token: str, *, allow_network: bool = True
    ) -> dict[str, Any]:
        """Install a staged package: ``pip install --target`` into its own
        directory (dependencies from the index when allowed). Replaces an
        installed version of the same distribution."""
        work = self._staged(token)
        staged = json.loads((work / "staged.json").read_text(encoding="utf-8"))
        wheel_path = work / staged["wheel"]
        info = read_wheel(wheel_path)
        if not _python_ok(info.requires_python):
            raise PackageError(
                "qcskills.python",
                f"{info.name} needs Python {info.requires_python}; the app runs "
                f"{sys.version_info.major}.{sys.version_info.minor}",
            )
        readme = find_package(work / "unpacked", []).readme
        target = self.packages_dir / info.package_id
        tmp = self.packages_dir / f".installing-{token}"
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "site").mkdir(parents=True)
        missing = self.missing_requirements(info.requires)
        if missing and not allow_network:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PackageError(
                "qcskills.dependencies", "network permission is needed for: " + ", ".join(missing)
            )
        cmd = [
            self._python,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-deps",
            "--target",
            str(tmp / "site"),
            str(wheel_path),
        ]
        if not allow_network:
            cmd.append("--no-index")
        proxy = self._proxy_url()
        if proxy and allow_network:
            cmd += ["--proxy", proxy]
        try:
            proc = self._runner(cmd, PIP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PackageError("qcskills.pip_failed", "pip timed out") from None
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PackageError("qcskills.pip_failed", _pip_error(proc, proxy))
        if missing:
            dep_cmd = [
                self._python,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--target",
                str(tmp / "site"),
                *missing,
            ]
            if proxy:
                dep_cmd += ["--proxy", proxy]
            try:
                deps = self._runner(dep_cmd, PIP_TIMEOUT_S)
            except (subprocess.TimeoutExpired, OSError) as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                raise PackageError(
                    "qcskills.pip_failed", f"dependency installation failed: {type(exc).__name__}"
                ) from None
            if deps.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise PackageError("qcskills.pip_failed", _pip_error(deps, proxy))
        (tmp / "README.md").write_text(readme, encoding="utf-8")
        meta = {
            "id": info.package_id,
            "name": info.name,
            "version": info.version,
            "summary": info.summary,
            "author": info.author,
            "wheel": info.filename,
            "sha256": info.sha256,
            "skill_count": info.skill_count,
            "server_module": info.server_module,
            "server_args": list(info.server_args),
            "server_name": _server_name(info.name),
            "source": staged["filename"],
            "installed_at": int(self._clock()),
        }
        (tmp / "package.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        backups: list[tuple[Path, Path]] = []
        try:
            for old in [p for p in self.installed() if p["name"] == info.name]:
                old_dir = self._package_dir(old["id"])
                backup = self.packages_dir / f".backup-{token}-{len(backups)}"
                old_dir.rename(backup)
                backups.append((old_dir, backup))
            tmp.rename(target)
        except OSError:
            for original, backup in reversed(backups):
                backup.rename(original)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        for _, backup in backups:
            self._rm(backup)
        shutil.rmtree(work, ignore_errors=True)
        return meta

    def discard(self, token: str) -> None:
        shutil.rmtree(self._staged(token), ignore_errors=True)

    def remove(self, package_id: str) -> None:
        d = self._package_dir(package_id)
        if not d.is_dir():
            raise PackageError("qcskills.unknown_package", f"no installed package {package_id!r}")
        self._rm(d)

    def missing_requirements(self, requires: Iterable[str]) -> list[str]:
        """Requirements not importable from the app's own environment (the
        wheel's site dir is empty before install, so these are what pip will
        download)."""
        missing: list[str] = []
        for raw in requires:
            req = Requirement(raw)
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            if req.url:
                raise PackageError(
                    "qcskills.dependencies", "direct-URL dependencies are not supported"
                )
            try:
                version = md.version(req.name)
            except md.PackageNotFoundError:
                missing.append(str(req))
                continue
            if version not in req.specifier or req.extras:
                missing.append(str(req))
        return missing

    # -- helpers ------------------------------------------------------------------

    def _package_dir(self, package_id: str) -> Path:
        if not _ID_RE.match(package_id):
            raise PackageError("qcskills.unknown_package", f"no installed package {package_id!r}")
        return self.packages_dir / package_id

    def _staged(self, token: str) -> Path:
        work = self.staging_dir / token
        if not _TOKEN_RE.match(token) or not (work / "staged.json").is_file():
            raise PackageError(
                "qcskills.unknown_token", "this upload has expired; choose the ZIP again"
            )
        return work

    def _sweep(self) -> None:
        if not self.staging_dir.is_dir():
            return
        now = self._clock()
        for d in self.staging_dir.iterdir():
            try:
                if now - d.stat().st_mtime > STAGING_TTL_S:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    @staticmethod
    def _rm(path: Path) -> None:
        # The server process may still hold files for a moment (Windows).
        for attempt in range(_RM_ATTEMPTS):
            try:
                target = str(path.resolve())
                if os.name == "nt" and not target.startswith("\\\\?\\"):
                    target = "\\\\?\\" + target
                shutil.rmtree(target)
            except FileNotFoundError:
                return
            except OSError as exc:
                if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in (
                    32,
                    145,
                ):
                    raise
                if attempt == _RM_ATTEMPTS - 1:
                    raise
                time.sleep(0.25)
            else:
                return


def _server_name(dist_name: str) -> str:
    """MCP server name (app-wide ``qualcomm-skills-<this>``): ``a-z0-9`` and
    single ``-``; ``-mcp`` dropped (``dragonwing-windows-mcp`` -> ``dragonwing-windows``)."""
    base = re.sub(r"[^a-z0-9]+", "-", dist_name.lower()).strip("-")
    base = re.sub(r"-mcp$", "", base) or "package"
    return base[:40].strip("-")


def _python_ok(spec: str) -> bool:
    """A simple ``Requires-Python`` check (``>=3.10``, ``>=3.9,<4``)."""
    return not spec or SpecifierSet(spec).contains(".".join(map(str, sys.version_info[:3])))


def _pip_error(proc: subprocess.CompletedProcess[str], proxy: str | None) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = "\n".join(lines[-12:])
    if proxy:
        tail = tail.replace(proxy, "<proxy>")
    hint = ""
    low = text.lower()
    if (
        "could not find a version" in low
        or "no matching distribution" in low
        or "connection" in low
    ):
        hint = " (a dependency could not be downloaded: check the network / proxy settings)"
    return f"pip install failed{hint}:\n{tail}"
