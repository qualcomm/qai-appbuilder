# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Install a local Python ``.whl`` as a stdio MCP server (agent self-install).

Why this exists: the MCP marketplace only knows ``npx`` launchers and ``.mcp``
zip packages (``mcp_client._install_mcp_package``); a plain wheel such as
``some_server-0.1.1-py3-none-any.whl`` had no supported path, so an
agent asked to "install this MCP" had nothing deterministic to call.

This module does the mechanical, error-prone part and nothing else:

1. read the wheel's metadata (name / version / ``console_scripts``) straight from
   the zip -- no install needed to learn the entry point;
2. create (or reuse) a dedicated venv under ``<data>/mcp_packages/<name>/venv``;
3. install the wheel into it (``uv pip`` when available, else ``pip``; ``pip`` is
   bootstrapped with ``ensurepip`` when the venv lacks it -- a uv-made venv has none);
4. return the command that launches the server.

It does NOT register the server, flip any gate, or decide whether installing is
allowed -- those are the caller's (and the user's) decisions.

Design notes
------------
* Never raises on a predictable failure: every step returns a structured
  :class:`WheelInstallResult` carrying ``step`` / ``error`` / output tail so the
  calling agent can read it and self-correct.
* The venv inherits the host's site-packages (``--system-site-packages``) and the
  host's installed versions are used as pip *constraints*, exactly like
  ``_install_mcp_package`` -- otherwise pip may upgrade ``mcp`` to 2.x and break
  the in-process SDK expectations / build native wheels from source.
* Blocking subprocess work runs through an injectable ``runner`` so tests never
  spawn a real venv.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from qai.platform.logging import get_logger

__all__ = [
    "choose_entry",
    "WheelInfo",
    "WheelInstallResult",
    "read_wheel_info",
    "install_wheel_as_mcp",
    "SERVER_NAME_PATTERN",
]

_log = get_logger(__name__)

#: Same rule as ``McpServerConfig.name`` (``domain/mcp_server.py``).
SERVER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"
_NAME_RE = re.compile(SERVER_NAME_PATTERN)

_OUTPUT_TAIL_CHARS = 2048
#: Most companion wheels one server may be installed with (a sanity bound, not a
#: tuning knob: a server that needs more than a handful of local wheels is almost
#: certainly mis-described).
_MAX_EXTRA_WHEELS = 8
_VENV_TIMEOUT_S = 180
_INSTALL_TIMEOUT_S = 600

#: ``runner(cmd, timeout)`` -> ``(returncode, combined_output)``.
Runner = Callable[[list[str], int], "tuple[int, str]"]


@dataclass(frozen=True, slots=True)
class WheelInfo:
    """Metadata read from a wheel without installing it."""

    dist_name: str
    version: str
    #: ``console_scripts`` entry-point names, in file order.
    console_scripts: tuple[str, ...] = ()
    #: ``(script name, "module:func")`` for every console script, same order.
    console_targets: tuple[tuple[str, str], ...] = ()
    #: Top-level importable packages (from ``top_level.txt`` or the zip layout).
    top_level: tuple[str, ...] = ()


@dataclass(slots=True)
class WheelInstallResult:
    """Outcome of :func:`install_wheel_as_mcp`. ``ok`` False -> read ``step``/``error``."""

    ok: bool
    step: str = ""
    error: str = ""
    dist_name: str = ""
    version: str = ""
    venv_dir: str = ""
    venv_python: str = ""
    #: Launch command for the stdio server (``command`` + ``args``) when ``ok``.
    command: str = ""
    args: tuple[str, ...] = ()
    #: Every console script the wheel offers (the chosen one is in ``command``).
    console_scripts: tuple[str, ...] = ()
    #: True when no console script exists and ``python -m <pkg>`` was guessed.
    entry_is_guess: bool = False
    #: ``(dist name, version)`` of every companion wheel installed into the same
    #: venv alongside the primary one (see ``extra_wheels`` on the installer).
    extra_wheels: tuple[tuple[str, str], ...] = ()
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Wheel metadata
# ---------------------------------------------------------------------------


def _parse_metadata_field(text: str, key: str) -> str:
    prefix = key.lower() + ":"
    for line in text.splitlines():
        if not line.strip():
            break  # end of the header block
        if line.lower().startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _parse_console_scripts(text: str) -> tuple[tuple[str, str], ...]:
    """Return ``(name, target)`` for every ``[console_scripts]`` line."""
    scripts: list[tuple[str, str]] = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = line[1:-1].strip() == "console_scripts"
            continue
        if in_section and "=" in line:
            name, target = line.split("=", 1)
            if name.strip():
                scripts.append((name.strip(), target.strip()))
    return tuple(scripts)


def read_wheel_info(wheel_path: Path) -> WheelInfo:
    """Read name / version / console scripts from a wheel. Raises ``ValueError``.

    ``ValueError`` (not a custom class) so callers can show ``str(exc)`` directly.
    """
    p = Path(wheel_path)
    if p.suffix.lower() != ".whl":
        raise ValueError(f"not a .whl file: {p.name!r}")
    if not p.is_file():
        raise ValueError(f"wheel not found: {p}")
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            meta_name = next(
                (n for n in names if n.endswith(".dist-info/METADATA")), None
            )
            if meta_name is None:
                raise ValueError("wheel has no .dist-info/METADATA (corrupt wheel?)")
            meta = zf.read(meta_name).decode("utf-8", errors="replace")
            dist_name = _parse_metadata_field(meta, "Name")
            version = _parse_metadata_field(meta, "Version")
            if not dist_name:
                raise ValueError("wheel METADATA has no Name")
            dist_info = meta_name.rsplit("/", 1)[0]
            targets: tuple[tuple[str, str], ...] = ()
            ep = f"{dist_info}/entry_points.txt"
            if ep in names:
                targets = _parse_console_scripts(
                    zf.read(ep).decode("utf-8", errors="replace")
                )
            top: tuple[str, ...] = ()
            tl = f"{dist_info}/top_level.txt"
            if tl in names:
                top = tuple(
                    ln.strip()
                    for ln in zf.read(tl).decode("utf-8", errors="replace").splitlines()
                    if ln.strip()
                )
            else:
                top = tuple(
                    sorted(
                        {
                            n.split("/", 1)[0]
                            for n in names
                            if "/" in n
                            and not n.split("/", 1)[0].endswith((".dist-info", ".data"))
                            and not n.startswith("__")
                        }
                    )
                )
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid wheel (bad zip): {exc}") from exc
    return WheelInfo(
        dist_name=dist_name,
        version=version,
        console_scripts=tuple(n for n, _ in targets),
        console_targets=targets,
        top_level=top,
    )


def choose_entry(info: WheelInfo, entry: str | None) -> tuple[str | None, str]:
    """Pick the console script that starts the MCP server.

    Returns ``(script, "")`` on success, ``(None, reason)`` when the choice is
    ambiguous or invalid (the caller must then ASK the user -- guessing between
    two MCP servers of one wheel would silently register the wrong one).

    Order: explicit ``entry`` (validated) -> the only script -> the only script
    whose name mentions ``mcp`` -> among the ``mcp``-named ones (or all, if none)
    the only one whose target module is a ``*server*`` module.
    """
    scripts = info.console_scripts
    if not scripts:
        return None, ""
    if entry:
        if entry in scripts:
            return entry, ""
        return None, (
            f"entry {entry!r} is not a console script of this wheel; "
            f"available: {', '.join(scripts)}"
        )
    if len(scripts) == 1:
        return scripts[0], ""
    mcp_named = [s for s in scripts if "mcp" in s.lower()]
    if len(mcp_named) == 1:
        return mcp_named[0], ""
    pool = mcp_named or list(scripts)
    target_of = dict(info.console_targets)
    server_like = [
        s for s in pool
        if "server" in target_of.get(s, "").split(":", 1)[0].rsplit(".", 1)[-1].lower()
    ]
    if len(server_like) == 1:
        return server_like[0], ""
    return None, (
        "this wheel has several console scripts and it is not clear which one "
        f"starts the MCP server: {', '.join(scripts)}. Ask the user (or read the "
        "package README) and call again with entry=<script>."
    )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _default_runner(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        cp = subprocess.run(  # noqa: S603 -- argv list, no shell
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd[:4])} ..."
    except OSError as exc:
        return 127, f"cannot run {cmd[0]!r}: {exc}"
    out = (cp.stdout or b"") + (cp.stderr or b"")
    return cp.returncode, out.decode("utf-8", errors="replace")


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL_CHARS else "..." + text[-_OUTPUT_TAIL_CHARS:]


def _base_python() -> str:
    """The interpreter a new MCP venv should be built FROM.

    Not ``sys.executable``: when the app runs from a uv-made venv that is a
    trampoline, and ``python -m venv`` run through it records the base it
    resolves to in the new venv's ``pyvenv.cfg`` -- observed to be uv's
    *minor-version alias* directory (``...\\uv\\python\\cpython-3.13-...``).
    That alias is not stable (uv removes / re-points it on a Python upgrade or
    cleanup), after which every MCP venv built through it fails to start with
    ``did not find executable at ...``. The real base interpreter named by
    ``sys.base_prefix`` is a concrete, versioned directory, so build from that.
    Falls back to ``sys.executable`` when it cannot be located.
    """
    base = Path(sys.base_prefix) / (
        "python.exe" if sys.platform == "win32" else "bin/python3"
    )
    return str(base) if base.exists() else sys.executable


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _script_path(venv_dir: Path, script: str) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / f"{script}.exe"
    return venv_dir / "bin" / script


def _remove_tree(path: Path) -> None:
    """``rmtree`` that copes with Windows' 260-character limit and read-only files.

    A skills / data bundle inside a venv (observed: ~22 MB of nested folders) easily
    pushes a file path past MAX_PATH once the data directory itself is long, and
    plain ``shutil.rmtree`` then fails with "path not found" on files that exist --
    which would make a broken venv impossible to rebuild. Windows' extended-length
    path prefix lifts the limit; read-only files are made writable and retried.
    """
    target = str(path)
    if sys.platform == "win32" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + str(path.resolve())

    def _retry(func: Callable[[str], object], p: str, _exc: object) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(target, onerror=_retry)


def _canonical_dist(name: str) -> str:
    """PEP 503 normalisation, so ``Foo_Bar`` / ``foo-bar`` / ``foo.bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def _validate_extra_wheels(
    primary_path: Path,
    primary: WheelInfo,
    extras: Sequence[str | Path],
) -> tuple[list[Path], list[WheelInfo], str]:
    """Check companion wheels BEFORE anything is created or installed.

    Returns ``(paths, infos, "")`` on success or ``([], [], reason)``. Generic by
    construction: it looks only at the files themselves (is it a readable ``.whl``,
    is it a duplicate, does it clash with the primary distribution) -- never at what
    package they are.
    """
    if len(extras) > _MAX_EXTRA_WHEELS:
        return [], [], (
            f"too many extra_wheels ({len(extras)}); at most {_MAX_EXTRA_WHEELS} "
            "companion wheels per server"
        )
    try:
        primary_key = primary_path.resolve()
    except OSError:
        primary_key = primary_path
    seen_paths = {primary_key}
    seen_dists = {_canonical_dist(primary.dist_name): primary.dist_name}
    paths: list[Path] = []
    infos: list[WheelInfo] = []
    for i, raw in enumerate(extras):
        if not isinstance(raw, (str, Path)) or not str(raw).strip():
            return [], [], f"extra_wheels[{i}] must be a non-empty path string"
        path = Path(str(raw).strip()).expanduser()
        try:
            info = read_wheel_info(path)
        except ValueError as exc:
            return [], [], f"extra_wheels[{i}] ({path.name}): {exc}"
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen_paths:
            continue  # the same file listed twice (or the primary again): install once
        dist_key = _canonical_dist(info.dist_name)
        if dist_key in seen_dists:
            return [], [], (
                f"extra_wheels[{i}] ({path.name}) is another wheel of "
                f"'{seen_dists[dist_key]}', which is already being installed; "
                "list each distribution once"
            )
        seen_paths.add(key)
        seen_dists[dist_key] = info.dist_name
        paths.append(path)
        infos.append(info)
    return paths, infos, ""


def _install_wheel_blocking(
    wheel: Path,
    info: WheelInfo,
    *,
    server_name: str,
    data_dir: Path,
    entry: str | None,
    runner: Runner,
    uv_path: str | None,
    extra_paths: Sequence[Path] = (),
    extra_infos: Sequence[WheelInfo] = (),
) -> WheelInstallResult:
    res = WheelInstallResult(
        ok=False,
        dist_name=info.dist_name,
        version=info.version,
        console_scripts=info.console_scripts,
    )
    # Decide the entry first: an ambiguous / invalid choice must fail BEFORE the
    # (slow, network-touching) install, with nothing created on disk.
    chosen, why = choose_entry(info, entry)
    if info.console_scripts and chosen is None:
        res.step, res.error = "resolve_entry", why
        return res
    pkg_dir = Path(data_dir) / "mcp_packages" / server_name
    venv_dir = pkg_dir / "venv"
    vpy = _venv_python(venv_dir)
    res.venv_dir = str(venv_dir)
    res.venv_python = str(vpy)
    try:
        pkg_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.step, res.error = "prepare", f"cannot create {pkg_dir}: {exc}"
        return res

    # 1. venv. Reuse a HEALTHY existing one (a re-install is an upgrade, not a
    #    rebuild); an existing venv that cannot even start (its base Python was
    #    moved / removed -- e.g. a uv cleanup) is rebuilt, which is also how a
    #    previously-broken registration gets repaired by simply installing again.
    healthy = False
    if vpy.exists():
        rc_h, _ = runner([str(vpy), "-c", "import sys"], 30)
        healthy = rc_h == 0
        if not healthy:
            res.notes.append(
                "the existing venv could not start (its base Python is gone or "
                "moved); rebuilt it"
            )
            try:
                _remove_tree(venv_dir)
            except OSError as exc:
                res.step = "create_venv"
                res.error = (
                    f"cannot remove the broken venv {venv_dir}: {exc} "
                    "(is a server from it still running?)"
                )
                return res
    if not healthy:
        rc, out = runner(
            [_base_python(), "-m", "venv", "--system-site-packages", str(venv_dir)],
            _VENV_TIMEOUT_S,
        )
        if rc != 0 or not vpy.exists():
            res.step, res.error = "create_venv", _tail(out) or f"exit code {rc}"
            return res

    # 2. constraints from the host's installed packages (best-effort)
    constraints: Path | None = None
    rc, frozen = runner([sys.executable, "-m", "pip", "freeze", "--all"], 60)
    if rc == 0 and frozen.strip():
        constraints = pkg_dir / "_host_constraints.txt"
        try:
            constraints.write_text(
                "\n".join(
                    ln
                    for ln in frozen.splitlines()
                    if ln and not ln.startswith(("-e ", "#", "WARNING"))
                    and "==" in ln
                ),
                encoding="utf-8",
            )
        except OSError:
            constraints = None
    else:
        res.notes.append(
            "host pip freeze unavailable; installed without version constraints"
        )

    # 3. install
    if uv_path:
        cmd = [uv_path, "pip", "install", "--python", str(vpy), "--native-tls"]
        if constraints:
            cmd += ["-c", str(constraints)]
        # ONE invocation for the primary and every companion wheel: the resolver
        # sees them together, and a conflict fails the whole install instead of
        # leaving the venv half-populated.
        cmd.extend(str(w) for w in (wheel, *extra_paths))
        rc, out = runner(cmd, _INSTALL_TIMEOUT_S)
        res.notes.append("installed with uv pip")
    else:
        rc_pip, _ = runner([str(vpy), "-m", "pip", "--version"], 30)
        if rc_pip != 0:
            rc_e, out_e = runner([str(vpy), "-m", "ensurepip", "--upgrade"], 120)
            if rc_e != 0:
                res.step = "bootstrap_pip"
                res.error = _tail(out_e) or "venv has no pip and ensurepip failed"
                return res
            res.notes.append("bootstrapped pip with ensurepip")
        cmd = [
            str(vpy), "-m", "pip", "install", "--quiet", "--prefer-binary",
            "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org",
        ]
        if constraints:
            cmd += ["-c", str(constraints)]
        cmd.extend(str(w) for w in (wheel, *extra_paths))  # see the uv branch
        rc, out = runner(cmd, _INSTALL_TIMEOUT_S)
        res.notes.append("installed with pip")
    if rc != 0:
        res.step, res.error = "install_wheel", _tail(out) or f"exit code {rc}"
        return res

    # 4. entry point
    if chosen is not None:
        exe = _script_path(venv_dir, chosen)
        if exe.exists():
            res.command, res.args = str(exe), ()
        else:
            # Script wrapper missing (unusual) -> run it through the venv python.
            module = dict(info.console_targets).get(chosen, "").split(":", 1)[0].strip()
            res.command, res.args = str(vpy), ("-m", module or chosen)
            res.entry_is_guess = True
            res.notes.append(
                f"console script wrapper {exe.name} not found in the venv; "
                "fell back to python -m"
            )
    else:
        pkgs = [t for t in info.top_level if t.isidentifier()]
        if not pkgs:
            res.step = "resolve_entry"
            res.error = "wheel declares no console_scripts and no importable package"
            return res
        res.command, res.args = str(vpy), ("-m", pkgs[0])
        res.entry_is_guess = True
        res.notes.append(
            "wheel has no console_scripts; guessed `python -m "
            f"{pkgs[0]}` -- verify with a connection test"
        )
    res.extra_wheels = tuple((i.dist_name, i.version) for i in extra_infos)
    res.ok = True
    _log.info(
        "chat.mcp.wheel_installed",
        server=server_name,
        dist=info.dist_name,
        version=info.version,
        command=res.command,
        guess=res.entry_is_guess,
    )
    return res


async def install_wheel_as_mcp(
    wheel_path: str | Path,
    *,
    server_name: str,
    data_dir: Path,
    entry: str | None = None,
    runner: Runner | None = None,
    uv_path: str | None = None,
    extra_wheels: Sequence[str | Path] = (),
) -> WheelInstallResult:
    """Validate + install a local wheel into a dedicated venv. Never raises.

    ``extra_wheels``: further LOCAL wheels installed into the SAME venv in the same
    pip/uv call -- for a server that imports a companion package at run time. Each
    server gets its own isolated venv, so a package installed for server A is not
    visible to server B; whatever B needs must be listed here. Console scripts of
    the extras are ignored (the launch command always comes from ``wheel_path``).

    ``uv_path`` defaults to ``shutil.which("uv")`` when ``runner`` is not injected
    (tests inject both and stay hermetic).
    """
    if not _NAME_RE.match(server_name or ""):
        return WheelInstallResult(
            ok=False,
            step="validate",
            error=(
                f"invalid server name {server_name!r}: use letters, digits, "
                "'_', '.', '-' and start with a letter or digit"
            ),
        )
    wheel = Path(str(wheel_path)).expanduser()
    try:
        info = read_wheel_info(wheel)
    except ValueError as exc:
        return WheelInstallResult(ok=False, step="read_wheel", error=str(exc))

    extra_paths, extra_infos, why = _validate_extra_wheels(wheel, info, extra_wheels)
    if why:
        return WheelInstallResult(ok=False, step="read_wheel", error=why)

    use_runner: Runner = runner or _default_runner
    use_uv = uv_path if (uv_path is not None or runner is not None) else shutil.which("uv")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: _install_wheel_blocking(
                wheel,
                info,
                server_name=server_name,
                data_dir=Path(data_dir),
                entry=entry,
                runner=use_runner,
                uv_path=use_uv,
                extra_paths=extra_paths,
                extra_infos=extra_infos,
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- contract: never raise
        _log.warning("chat.mcp.wheel_install_unexpected", error=str(exc))
        return WheelInstallResult(
            ok=False, step="unexpected", error=f"{type(exc).__name__}: {exc}"
        )
