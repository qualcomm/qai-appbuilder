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
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
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
    #: The environment the server was installed into (``"shared"`` by default).
    environment: str = ""
    #: True when it kept using a PRE-EXISTING per-server venv (the old layout).
    env_is_legacy: bool = False
    #: ``(dist, version)`` of the wheels OTHER servers installed into this same
    #: environment -- i.e. who this server shares its packages with.
    env_wheels: tuple[tuple[str, str], ...] = ()
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


_SHARED_ENV = "shared"
_ENVS_DIRNAME = "_envs"
_MANIFEST_NAME = "env.json"

# One install at a time per environment (an agent can issue two ``install_wheel``
# calls in parallel; two pip runs on one venv would corrupt it).
_ENV_LOCKS: dict[str, threading.Lock] = {}
_ENV_LOCKS_GUARD = threading.Lock()


def _env_lock(env_dir: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(env_dir)))
    with _ENV_LOCKS_GUARD:
        return _ENV_LOCKS.setdefault(key, threading.Lock())


def _resolve_environment(
    data_dir: Path, server_name: str, environment: str | None
) -> tuple[str, Path, bool]:
    """Pick the environment a server is installed into: ``(name, dir, is_legacy)``.

    * explicit ``environment`` -> ``<data>/mcp_packages/_envs/<environment>``;
    * else a PRE-EXISTING per-server venv (``<data>/mcp_packages/<server>/venv``, the
      old layout) keeps being used, so re-installing an already-registered server
      upgrades it in place instead of silently moving it;
    * else the shared environment.

    ``_envs`` starts with an underscore, so it can never collide with a server name
    (those must start with a letter or digit).
    """
    root = Path(data_dir) / "mcp_packages"
    if environment:
        return environment, root / _ENVS_DIRNAME / environment, False
    legacy = root / server_name
    if (legacy / "venv").exists():
        return server_name, legacy, True
    return _SHARED_ENV, root / _ENVS_DIRNAME / _SHARED_ENV, False


def _read_manifest(env_dir: Path) -> list[dict[str, str]]:
    """The wheels this environment was built from. Missing / corrupt -> ``[]``."""
    try:
        data = json.loads((env_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    wheels = data.get("wheels") if isinstance(data, dict) else None
    if not isinstance(wheels, list):
        return []
    return [
        {"dist": w["dist"], "version": str(w.get("version") or ""), "path": w["path"]}
        for w in wheels
        if isinstance(w, dict)
        and isinstance(w.get("dist"), str)
        and isinstance(w.get("path"), str)
    ]


def _write_manifest(env_dir: Path, entries: list[dict[str, str]]) -> bool:
    try:
        (env_dir / _MANIFEST_NAME).write_text(
            json.dumps({"wheels": entries}, indent=2), encoding="utf-8"
        )
    except OSError:
        return False
    return True


def _freeze_constraints(frozen: str, *, exclude: set[str]) -> str:
    """``pip freeze`` output -> a constraints file body, minus the dists in ``exclude``."""
    keep: list[str] = []
    for ln in frozen.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("-e ", "#", "WARNING")) or "==" not in ln:
            continue
        if _canonical_dist(ln.split("==", 1)[0]) in exclude:
            continue
        keep.append(ln)
    return "\n".join(keep)


def _install_wheel_blocking(
    wheel: Path,
    info: WheelInfo,
    *,
    server_name: str,
    env_name: str,
    env_dir: Path,
    env_legacy: bool,
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
        environment=env_name,
        env_is_legacy=env_legacy,
    )
    # Decide the entry first: an ambiguous / invalid choice must fail BEFORE the
    # (slow, network-touching) install, with nothing created on disk.
    chosen, why = choose_entry(info, entry)
    if info.console_scripts and chosen is None:
        res.step, res.error = "resolve_entry", why
        return res
    venv_dir = env_dir / "venv"
    vpy = _venv_python(venv_dir)
    res.venv_dir = str(venv_dir)
    res.venv_python = str(vpy)
    try:
        env_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.step, res.error = "prepare", f"cannot create {env_dir}: {exc}"
        return res

    # What this call installs, and who else already lives in the environment.
    this_install: list[tuple[WheelInfo, Path]] = [
        (info, wheel),
        *zip(extra_infos, extra_paths),
    ]
    this_keys = {_canonical_dist(i.dist_name) for i, _ in this_install}
    others = (
        []
        if env_legacy
        else [m for m in _read_manifest(env_dir) if _canonical_dist(m["dist"]) not in this_keys]
    )

    # 1. venv. Reuse a HEALTHY existing one (a re-install is an upgrade, not a
    #    rebuild). One that cannot even start (its base Python was moved / removed
    #    -- e.g. a uv cleanup) or that is gone is (re)built, and then EVERY wheel the
    #    environment was made from is installed again with it: the venv is shared, so
    #    rebuilding it for one server must not silently drop the others' packages.
    healthy = False
    rebuilt = False
    if vpy.exists():
        rc_h, _ = runner([str(vpy), "-c", "import sys"], 30)
        healthy = rc_h == 0
    restore: list[Path] = []
    if not healthy:
        missing = [m["path"] for m in others if not Path(m["path"]).is_file()]
        if missing:
            res.step = "rebuild_env"
            res.error = (
                f"environment '{env_name}' must be rebuilt, but these wheels it was "
                f"built from are no longer on disk: {', '.join(missing)}. Restore "
                "them, or install into a different environment name."
            )
            return res
        restore = [Path(m["path"]) for m in others]
        if vpy.exists():
            rebuilt = True
            res.notes.append(
                "the existing venv could not start (its base Python is gone or "
                "moved); rebuilt it"
                + (f" and reinstalled {len(restore)} other wheel(s)" if restore else "")
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
        rc, out = runner(
            [_base_python(), "-m", "venv", "--system-site-packages", str(venv_dir)],
            _VENV_TIMEOUT_S,
        )
        if rc != 0 or not vpy.exists():
            res.step, res.error = "create_venv", _tail(out) or f"exit code {rc}"
            return res

    # 2. version constraints (best-effort): the host's installed packages, and -- for
    #    an environment that already has other members -- what is installed in it NOW.
    #    The second file is what makes sharing safe: a new package that needs a
    #    different version of something already installed FAILS the install loudly
    #    instead of silently upgrading / downgrading it under the other servers.
    constraint_files: list[Path] = []
    rc, frozen = runner([sys.executable, "-m", "pip", "freeze", "--all"], 60)
    if rc == 0 and frozen.strip():
        host_file = env_dir / "_host_constraints.txt"
        try:
            host_file.write_text(_freeze_constraints(frozen, exclude=set()), encoding="utf-8")
            constraint_files.append(host_file)
        except OSError:
            pass
    else:
        res.notes.append(
            "host pip freeze unavailable; installed without version constraints"
        )
    protected = False
    if healthy and not env_legacy:
        rc_f, env_frozen = runner([str(vpy), "-m", "pip", "freeze", "--local", "--all"], 60)
        if rc_f == 0 and env_frozen.strip():
            env_file = env_dir / "_env_constraints.txt"
            try:
                env_file.write_text(
                    _freeze_constraints(env_frozen, exclude=this_keys), encoding="utf-8"
                )
                constraint_files.append(env_file)
                protected = True
            except OSError:
                pass

    # 3. install -- ONE invocation for the primary, every companion and (after a
    #    rebuild) every previous member: the resolver sees them together, and a
    #    conflict fails the whole install instead of leaving the venv half-populated.
    targets = [str(w) for w in (wheel, *extra_paths, *restore)]
    if uv_path:
        cmd = [uv_path, "pip", "install", "--python", str(vpy), "--native-tls"]
        for f in constraint_files:
            cmd += ["-c", str(f)]
        cmd.extend(targets)
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
        for f in constraint_files:
            cmd += ["-c", str(f)]
        cmd.extend(targets)
        rc, out = runner(cmd, _INSTALL_TIMEOUT_S)
        res.notes.append("installed with pip")
    if rc != 0:
        res.step, res.error = "install_wheel", _tail(out) or f"exit code {rc}"
        if protected:
            res.error += (
                f"\n\n(Environment '{env_name}' already holds other packages whose "
                "versions are kept fixed. If this wheel needs different versions of "
                "them, install it into its own environment: pass "
                "environment='<another name>'.)"
            )
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
    res.env_wheels = tuple((m["dist"], m["version"]) for m in others)
    if not env_legacy:
        recorded = {
            _canonical_dist(m["dist"]): m
            for m in others
        }
        for i, p in this_install:
            try:
                path_str = str(p.resolve())
            except OSError:
                path_str = str(p)
            recorded[_canonical_dist(i.dist_name)] = {
                "dist": i.dist_name, "version": i.version, "path": path_str,
            }
        if not _write_manifest(env_dir, list(recorded.values())):
            res.notes.append(
                "could not record the environment's wheel list; a later rebuild of "
                "this environment will not know to restore it"
            )
    res.ok = True
    _log.info(
        "chat.mcp.wheel_installed",
        server=server_name,
        environment=env_name,
        dist=info.dist_name,
        version=info.version,
        command=res.command,
        guess=res.entry_is_guess,
        rebuilt=rebuilt,
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
    environment: str | None = None,
) -> WheelInstallResult:
    """Validate + install a local wheel into an MCP environment. Never raises.

    ``environment``: which venv the server lives in. Servers installed into the SAME
    environment can import each other's packages (what a server that needs a
    companion package at run time requires) and share one copy of common
    dependencies. Omitted -> the shared environment, unless the server already has a
    per-server venv from the old layout (then that one is upgraded in place). Pass
    another name to keep a server apart, e.g. when it needs different versions of
    packages the shared environment already holds: installs into an environment that
    already has other members keep those members' installed versions fixed, so a
    conflict FAILS instead of altering them.

    ``extra_wheels``: further LOCAL wheels installed into the same environment in the
    same pip/uv call. Console scripts of the extras are ignored (the launch command
    always comes from ``wheel_path``).

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
    if environment and not _NAME_RE.match(str(environment)):
        return WheelInstallResult(
            ok=False,
            step="validate",
            error=(
                f"invalid environment name {environment!r}: use letters, digits, "
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
    env_name, env_dir, env_legacy = _resolve_environment(
        Path(data_dir), server_name, environment
    )

    def _locked() -> WheelInstallResult:
        with _env_lock(env_dir):
            return _install_wheel_blocking(
                wheel,
                info,
                server_name=server_name,
                env_name=env_name,
                env_dir=env_dir,
                env_legacy=env_legacy,
                entry=entry,
                runner=use_runner,
                uv_path=use_uv,
                extra_paths=extra_paths,
                extra_infos=extra_infos,
            )

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _locked)
    except Exception as exc:  # noqa: BLE001 -- contract: never raise
        _log.warning("chat.mcp.wheel_install_unexpected", error=str(exc))
        return WheelInstallResult(
            ok=False, step="unexpected", error=f"{type(exc).__name__}: {exc}"
        )
