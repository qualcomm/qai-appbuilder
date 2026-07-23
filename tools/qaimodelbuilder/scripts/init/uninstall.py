# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``scripts.init.uninstall`` — Clean Cutover uninstaller (PR-902).

Replaces the legacy ``Uninstall.bat`` workflow with a Python script
that fits the Clean Cutover release pipeline.

What the script removes (DEFAULT)
---------------------------------
Rolls back what ``Setup.bat`` installed **outside** the project tree,
plus OS-level side effects:

1. **Running QAIModelBuilder processes** — graceful POST
   ``/api/system/reboot`` (best-effort) → kill any ``python.exe`` whose
   image path lives under ``%LOCALAPPDATA%\\QAIModelBuilder``.
2. **Virtual envs + bundled tools** — ``%LOCALAPPDATA%\\QAIModelBuilder\\``
   recursively (covers ``.venv_arm64_313``, ``.venv_x64_310``, optional
   ``git/`` PortableGit, ``node/`` portable Node.js + pnpm).
3. **Temp files** — ``%TEMP%\\QAIModelBuilder\\``.
4. **Setup install-time temp archives** — whitelisted filenames under
   ``data/downloads/`` (PortableGit-*.7z.exe, node-v*.zip, _uv_tmp.zip,
   _qairt_tmp.zip, vendor-deps.7z, plus .aria2 control files; and aria2c
   log / scratch sub-dirs). STRICT WHITELIST: runtime-downloaded model
   weights and other user files in ``data/downloads/`` are preserved.
5. **Setup-installed local tooling** — ``<repo>/data/bin/`` (uv.exe,
   aria2c.exe, 7zr.exe). Setup re-downloads them on next run.
6. **uv-managed Python interpreters** — only the cpython variants
   Setup.bat installs (cpython-3.13 ARM64 + cpython-3.10 x64) under
   ``%APPDATA%\\uv\\python\\``.

Note: previous versions also revoked per-run / persistent AppContainer
ACEs and removed orphan AppContainer registry profiles. The Windows
AppContainer / LPAC sandbox launcher chain has been deleted (Phase 3
cleanup, 2026-07-01); no fresh ACEs or AppContainer profiles are
created any more, so the corresponding uninstall stages were removed
in Phase 8. Any **residual** ACEs left over from older installs are
harmless (they reference capability SIDs that no current code resolves)
and can be cleaned manually via ``icacls`` if desired.

What ``--all`` ADDITIONALLY removes
-----------------------------------
* **uv package cache** (``%LOCALAPPDATA%\\uv\\cache``) — SHARED with
  other uv projects on this machine. Implied by --all (also enabled
  individually via ``--clean-uv``).
* **QAIRT SDK** — only the version Setup.bat installs (default
  ``C:\\Qualcomm\\AIStack\\QAIRT\\<version>\\`` where ``<version>`` comes from
  scripts/qairt_release.json; respects the
  ``QAIRT_SDK_ROOT`` / ``QAIRT_VERSION`` env vars Setup.bat uses).
* **Playwright Chromium cache** (``%LOCALAPPDATA%\\ms-playwright\\``) —
  only relevant if ``Setup.bat --dev`` ran; SHARED with other Playwright
  projects.
* **vendor/ runtime caches** — under ``<repo>/vendor/``: ``nltk_data``,
  ``g2p_data``, ``whl``, ``tiktoken``, plus ``__pycache__`` byte-code
  caches Step 6 pre-compiled. Re-running Setup.bat re-downloads
  ``vendor-deps.7z`` to repopulate these.
* **``%TEMP%\\jieba.cache``** — jieba's flat cache file Step 6 warmup
  produces (lives directly under %TEMP%, NOT under the QAIModelBuilder
  sub-tree, so it survives the default temp purge).

What ``--all`` does NOT remove (and why)
----------------------------------------
* **Visual Studio 2022** — Setup.bat may add components (VC.Tools.ARM64,
  Windows11SDK, CMake, clang-cl). VS is a heavy general-purpose IDE that
  other projects almost certainly depend on; ``--all`` deliberately
  skips VS. To **also uninstall VS**, pass ``--vs`` (separate flag,
  interactive YES confirmation, calls the VS Installer's
  ``setup.exe uninstall`` the same way Setup.bat ``modify``-ed it).
* **Rust toolchain / tauri-cli** — Setup.bat Step 5c installs them only
  when ``--desktop`` is passed (opt-in). ``~/.cargo`` and ``~/.rustup``
  are SHARED with any other Rust project on the box, so ``--all`` does
  NOT remove them. To **also remove the desktop build toolchain**, pass
  ``--desktop`` (removes only the project-specific ``cargo-tauri.exe``,
  safe) or ``--desktop-rust`` (removes the full Rust toolchain via
  ``rustup self uninstall``, interactive YES gate).

What is NEVER removed
---------------------
* **The project directory itself, and your USER DATA inside it.** The
  ``data/`` directory contents survive uninstall by default:
  ``qai.db`` (chat history / preferences), ``logs/``,
  ``config/``, and any **runtime-downloaded** files under ``data/downloads/``
  (e.g. multi-GB model weights). The uninstaller's only writes into the
  project tree are the whitelisted cleanup of install temp archives
  (item 4), removal of ``data/bin/`` Setup tooling (item 5), and — only
  with ``--all`` — the vendor/ runtime caches.
* System Python / VS BuildTools / CMake — system-level and out of scope.

Cross-platform
--------------
On non-Windows hosts the script removes the equivalent paths
(``~/.local/share/QAIModelBuilder``, ``~/.cache/QAIModelBuilder``) and
skips the Windows-specific stages (process kill via taskkill,
QAIRT SDK).

Usage::

    python -m scripts.init.uninstall                  # default uninstall
    python -m scripts.init.uninstall --yes            # non-interactive
    python -m scripts.init.uninstall --yes --clean-uv # default + uv cache
    python -m scripts.init.uninstall --yes --all      # FULL: default + uv cache + QAIRT + Playwright + vendor caches + jieba.cache
    python -m scripts.init.uninstall --yes --vs       # default + uninstall VS 2022 (interactive YES gate unless --yes)
    python -m scripts.init.uninstall --yes --all --vs # everything: --all + VS 2022
    python -m scripts.init.uninstall --yes --desktop      # default + remove tauri-cli (safe)
    python -m scripts.init.uninstall --yes --desktop-rust # default + remove Rust toolchain (SHARED!)

Exit codes
----------
* 0  — completed (some steps may have been skipped; details in stderr)
* 1  — operator cancelled at the confirmation prompt
* 2  — argparse rejected arguments
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Canonical install locations (mirror of install_qairt.py).
LOCALAPPDATA_QAI = Path("QAIModelBuilder")  # joined to %LOCALAPPDATA%
TEMP_QAI = Path("QAIModelBuilder")          # joined to %TEMP%


# Default QAIRT version — single source of truth: scripts/qairt_release.json
# (mirrors install_qairt.py / setup_qairt_env.py). Read by path so this works
# whether imported as ``scripts.init.uninstall`` or loaded standalone.
def _load_default_qairt_version() -> str:
    import json

    release_json = Path(__file__).resolve().parent.parent / "qairt_release.json"
    with open(release_json, encoding="utf-8") as fh:
        return json.load(fh)["qairt_version"]


DEFAULT_QAIRT_VERSION = _load_default_qairt_version()

# uv shared resources (only cleaned with --clean-uv).
APPDATA_UV_PYTHONS = ("uv", "python")
LOCALAPPDATA_UV_CACHE = ("uv", "cache")


@dataclass(frozen=True)
class UninstallTargets:
    localappdata_qai: Path
    temp_qai: Path
    uv_pythons: Path
    uv_cache: Path


# ----- Path discovery -------------------------------------------------------


def _localappdata() -> Path:
    env = os.environ.get("LOCALAPPDATA")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share"


def _appdata() -> Path:
    env = os.environ.get("APPDATA")
    if env:
        return Path(env)
    return Path.home() / ".config"


def _temp_dir() -> Path:
    env = os.environ.get("TEMP") or os.environ.get("TMP")
    if env:
        return Path(env)
    return Path("/tmp")


def _detect_repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent.parent


def _resolve_reboot_url() -> str:
    """Return the URL to ``POST /api/system/reboot`` for shutting the API down.

    Reads ``data/runtime/server.endpoint.json`` (single source of truth
    for the API's actual bound port — the supervisor in
    ``apps.cli.serve`` may have picked a fallback if the documented
    default port was inside a Windows excluded range). Falls back to
    ``http://localhost:8989`` when the file is absent or unparseable so
    a pre-endpoint-file install (or an already-stopped server) still
    gets the best-effort attempt — the caller wraps everything in a
    broad except so a wrong URL just fails silently.
    """

    fallback = "http://localhost:8989/api/system/reboot"
    try:
        from qai.platform.process.runtime_endpoint import read_endpoint
    except ImportError:
        return fallback
    repo_root = _detect_repo_root()
    info = read_endpoint(repo_root / "data")
    if info is None:
        return fallback
    url = info.get("url")
    if not isinstance(url, str) or not url:
        return fallback
    return url.rstrip("/") + "/api/system/reboot"


def _resolve_targets(repo_root: Path) -> UninstallTargets:
    lad = _localappdata()
    appdata = _appdata()
    temp = _temp_dir()

    return UninstallTargets(
        localappdata_qai=lad / LOCALAPPDATA_QAI,
        temp_qai=temp / TEMP_QAI,
        uv_pythons=appdata / Path(*APPDATA_UV_PYTHONS),
        uv_cache=lad / Path(*LOCALAPPDATA_UV_CACHE),
    )


# ----- Stage 1: stop running processes --------------------------------------


def stop_running_processes(targets: UninstallTargets, *, dry_run: bool = False) -> None:
    """Try to gracefully stop, then force-kill, QAIModelBuilder processes."""

    # 1) Best-effort graceful shutdown via the API.
    #
    # The API server's bound port is dynamic (see
    # ``apps/cli/serve.py:FALLBACK_PORTS``) — Hyper-V / WSL2 can reserve
    # the documented default 8989 inside a Windows excluded port range
    # at boot, forcing the supervisor to fall back to another candidate.
    # We therefore read the runtime endpoint file the API writes when it
    # starts serving traffic, and only fall back to the documented
    # default URL if the file is missing (e.g. the server is already
    # stopped, or this is an old install that pre-dates the endpoint
    # file). Best-effort throughout: any failure is silently swallowed
    # and the function continues to the kill-by-image-path path below.
    if not dry_run:
        try:
            import urllib.request

            reboot_url = _resolve_reboot_url()
            req = urllib.request.Request(
                reboot_url,
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2):  # noqa: S310
                pass
        except (OSError, ValueError):
            pass

    if sys.platform != "win32":
        # POSIX: best-effort pkill on python under our prefix.
        if not dry_run:
            _run_quiet(["pkill", "-f", str(targets.localappdata_qai)])
        return

    if dry_run:
        _info(f"[dry-run] would kill python.exe under {targets.localappdata_qai}")
        return

    _kill_processes_under(targets.localappdata_qai)


def _kill_processes_under(prefix: Path) -> None:
    """Find Python processes whose image path starts with ``prefix`` and kill them.

    Uses ``wmic`` (already installed on Windows; same approach as the
    legacy Uninstall.bat). On systems where wmic has been removed, we
    silently skip — the rmtree fallback path will retry locked files
    via robocopy mirroring.
    """

    prefix_str = str(prefix).replace("\\", "\\\\")  # wmic LIKE escape
    try:
        out = subprocess.check_output(  # noqa: S603, S607
            [
                "wmic",
                "process",
                "where",
                f"ExecutablePath like '{prefix_str}%%'",
                "get",
                "ProcessId",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return

    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))

    for pid in pids:
        if pid <= 4:  # skip system PIDs
            continue
        _run_quiet(["taskkill", "/F", "/PID", str(pid)])


# ----- Stage 2: remove venvs / temp / config --------------------------------


def _is_link_or_junction(path: Path) -> bool:
    """True if ``path`` is a symlink (any kind) OR a Windows junction.

    ``Path.is_symlink()`` returns False for NTFS junctions on Windows
    (junctions use IO_REPARSE_TAG_MOUNT_POINT, not IO_REPARSE_TAG_SYMLINK).
    We need to treat both as "link, do not recurse into the target" so
    rmtree/robocopy never touches the target's real files. Detect by the
    generic FILE_ATTRIBUTE_REPARSE_POINT flag, which is set for any reparse
    point including symlinks and junctions.
    """

    try:
        if path.is_symlink():
            return True
    except OSError:
        pass
    if sys.platform != "win32":
        return False
    try:
        import stat as _stat
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & _stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


def remove_directory(path: Path, *, label: str, dry_run: bool = False) -> bool:
    """Remove ``path`` if it exists. Returns True iff the dir is gone afterwards.

    On Windows, locked files are handled with a robocopy-mirroring
    fallback (mirror an empty dir on top of the target, then rmdir).

    Symlink / junction handling: uv 0.5+ stores managed Pythons under
    ``%APPDATA%\\uv\\python\\<id>`` as **directory symlinks or junctions**
    pointing into a separate cache. ``shutil.rmtree`` refuses to follow /
    remove a symlink and raises ``Cannot call rmtree on a symbolic link``;
    on a junction it would (much worse) recurse into the link target and
    delete the real cached files. We detect both reparse-point types via
    ``FILE_ATTRIBUTE_REPARSE_POINT`` and unlink the link itself only.
    """

    is_link = _is_link_or_junction(path)
    # Note: path.exists() is False for a broken symlink. Treat any link
    # as "present" so we always remove it, even when it dangles.
    if not is_link and not path.exists():
        _info(f"{label}: not present (skipping)")
        return True

    if dry_run:
        kind = "symlink/junction" if is_link else "tree"
        _info(f"[dry-run] would remove {label} ({kind}): {path}")
        return True

    _info(f"Removing {label}: {path}")

    if is_link:
        # Remove only the link, not its target. For symlinks, Path.unlink()
        # works. For NTFS junctions, unlink() raises PermissionError on
        # some Windows builds; os.rmdir() (which doesn't recurse) handles
        # both symlinked dirs and junctions reliably.
        removed = False
        try:
            os.rmdir(path)
            removed = True
        except OSError:
            try:
                path.unlink()
                removed = True
            except (OSError, IsADirectoryError, PermissionError) as exc:
                _err(f"failed to remove link {path}: {exc}")
                return False
        if not removed:
            _err(f"failed to remove link {path}")
            return False
        if path.exists() or _is_link_or_junction(path):
            _err(f"residual link remains at {path}")
            return False
        _ok(f"{label}: removed (symlink/junction)")
        return True

    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError as exc:
        _warn(f"shutil.rmtree failed ({exc}); attempting robocopy mirror fallback")
        if not _robocopy_purge(path):
            _err(f"failed to remove {path} (locked files?). Reboot and retry.")
            return False

    if path.exists():
        _err(f"residual files remain at {path}")
        return False

    _ok(f"{label}: removed")
    return True


def _robocopy_purge(path: Path) -> bool:
    """Force-empty a directory tree on Windows using robocopy /MIR.

    robocopy /MIR mirrors an empty source onto the target, which deletes
    everything inside the target — but leaves the target's root directory
    itself behind. We then explicitly retry rmdir on the (now-empty) root,
    with a short backoff for the case where Windows still holds a transient
    handle on a just-deleted child (causes ``[WinError 145] The directory
    is not empty``). Returns True iff the root directory is gone.
    """

    if sys.platform != "win32":
        return False
    # Use a unique temp dir (never inside the tree being purged) as the
    # empty mirror source. tempfile.mkdtemp avoids collisions and lands
    # outside %LOCALAPPDATA%\QAIModelBuilder.
    empty = Path(tempfile.mkdtemp(prefix="_qai_empty_uninstall_"))
    try:
        _run_quiet(
            [
                "robocopy",
                str(empty),
                str(path),
                "/mir",
                "/nfl",
                "/ndl",
                "/njh",
                "/njs",
                "/nc",
                "/ns",
            ]
        )
        # robocopy left the (empty) root behind. Try several times to remove
        # it: a just-finished mirror can briefly hold a directory handle.
        for attempt in range(5):
            try:
                shutil.rmtree(path, ignore_errors=False)
                break
            except OSError:
                # rmtree raises when path's content is empty but root is
                # locked, OR when stale child entries are still being
                # released. Wait briefly and retry.
                time.sleep(0.5)
        else:
            # Last-chance: if rmtree still fails, try the lower-level
            # rmdir which only works on truly-empty dirs.
            try:
                os.rmdir(path)
            except OSError:
                pass
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    return not path.exists()


# ----- Stage 3: clean Setup.bat install-time temp downloads -----------------


# Filenames Setup.bat writes into ``data/downloads/`` while installing the
# tooling stack. Setup deletes them inline on success, but Ctrl+C / network
# failures leave partial archives behind that survive across re-installs and
# can confuse the next Setup run (e.g. truncated PortableGit-*.7z.exe failing
# extraction). We clean these on uninstall using a STRICT WHITELIST so we do
# NOT touch any runtime-downloaded asset (model weights, etc.) the user may
# have placed under ``data/downloads/``.
_INSTALL_TEMP_FILE_PATTERNS = (
    "PortableGit-*.7z.exe",
    "PortableGit-*.7z.exe.aria2",
    "node-v*-win-arm64.zip",
    "node-v*-win-arm64.zip.aria2",
    "node-v*-win-x64.zip",
    "node-v*-win-x64.zip.aria2",
    "_uv_tmp.zip",
    "_uv_tmp.zip.aria2",
    "_qairt_tmp.zip",
    "_qairt_tmp.zip.aria2",
    "vendor-deps.7z",
    "vendor-deps.7z.aria2",
    "_aria2c_tmp.zip",
    "_aria2c_tmp.zip.aria2",
)
_INSTALL_TEMP_DIR_NAMES = (
    "log",                  # aria2c progress logs
    "_aria2c_tmp",          # aria2c extraction scratch
    "_node_tmp",            # node.js zip flatten scratch
    "_vendor_deps_tmp",     # vendor-deps.7z extraction scratch
)


def clean_install_temp_downloads(repo_root: Path, *, dry_run: bool = False) -> None:
    """Remove Setup.bat install-time temp archives from ``data/downloads/``.

    Strict whitelist: only files matching the patterns Setup.bat writes during
    install are removed. Any other content the user (or runtime) put under
    ``data/downloads/`` is left untouched. The directory itself is preserved.
    """

    downloads = repo_root / "data" / "downloads"
    if not downloads.is_dir():
        _info("install temp downloads: data/downloads/ not present; skipping")
        return

    removed_files = 0
    removed_dirs = 0

    for pattern in _INSTALL_TEMP_FILE_PATTERNS:
        for match in downloads.glob(pattern):
            if not match.is_file():
                continue
            if dry_run:
                _info(f"[dry-run] would remove install temp file: {match}")
                removed_files += 1
                continue
            try:
                match.unlink()
                removed_files += 1
            except OSError as exc:
                _warn(f"failed to remove {match}: {exc}")

    for name in _INSTALL_TEMP_DIR_NAMES:
        d = downloads / name
        if not d.is_dir():
            continue
        if dry_run:
            _info(f"[dry-run] would remove install temp dir: {d}")
            removed_dirs += 1
            continue
        try:
            shutil.rmtree(d, ignore_errors=False)
            removed_dirs += 1
        except OSError as exc:
            _warn(f"failed to remove {d}: {exc}")

    if removed_files == 0 and removed_dirs == 0:
        _info("install temp downloads: nothing to clean")
    else:
        _ok(
            f"install temp downloads: removed {removed_files} file(s), "
            f"{removed_dirs} dir(s) from {downloads}"
        )


# NOTE: The legacy "delete data/" step was removed: data/ lives INSIDE the
# project directory and holds user data; the uninstaller must never touch
# the project tree (only roll back what Setup.bat installed outside it).


# ----- Stage 4: uv-managed Python interpreters (DEFAULT CLEAN) ---------------


def clean_uv_pythons(targets: UninstallTargets, *, dry_run: bool = False) -> None:
    """Remove the uv-managed Python interpreters Setup.bat installs.

    Setup.bat runs ``uv python install cpython-3.13-windows-aarch64`` and
    ``uv python install cpython-3.10-windows-x86_64``. Those interpreters
    land under ``%APPDATA%\\uv\\python\\`` (Windows) / ``~/.local/share/uv/
    python/`` (POSIX). They are NOT shared by other uv projects in any
    meaningful way (each project re-resolves), so removing them is safe and
    aligned with the user's expectation that an uninstall reverts what
    Setup installed. Only our specific cpython variants are purged.
    """

    interp_root = targets.uv_pythons
    if not interp_root.is_dir():
        _info("uv pythons: not present (skipping)")
        # Even if interp_root is gone, the legacy shims under
        # ~/.local/bin may still be there from a prior Setup run.
        _purge_uv_python_shims(dry_run=dry_run)
        return

    for child in (
        "cpython-3.13-windows-aarch64-none",
        "cpython-3.13-windows-x86_64-none",
        "cpython-3.10-windows-x86_64-none",
    ):
        path = interp_root / child
        if path.exists():
            remove_directory(path, label=f"uv python ({child})", dry_run=dry_run)
        # Versioned variants (e.g. cpython-3.13.12-...) live alongside
        # the bare name; remove them too.
        if interp_root.is_dir():
            for sibling in interp_root.iterdir():
                if (
                    sibling.is_dir()
                    and sibling.name.startswith(child.replace("-windows", ""))
                ):
                    remove_directory(
                        sibling, label=f"uv python ({sibling.name})", dry_run=dry_run
                    )
    _purge_uv_python_shims(dry_run=dry_run)


def _purge_uv_python_shims(*, dry_run: bool = False) -> None:
    """Remove the per-version Python shims uv writes to ``~/.local/bin``.

    Older uv versions (and current uv when ``--no-bin`` is not passed)
    create ``python3.13.exe`` / ``python3.10.exe`` etc. shims in
    ``%USERPROFILE%\\.local\\bin``. Setup.bat now passes ``--no-bin`` to
    suppress these on new installs, but earlier Setup runs may have
    already created them. We purge the EXACT versioned shims for the two
    Pythons Setup installs — never the directory itself, never any other
    file (the user may have placed unrelated tools there).
    """

    home = Path.home()
    shim_dir = home / ".local" / "bin"
    if not shim_dir.is_dir():
        return
    purged = 0
    for name in ("python3.13.exe", "python3.10.exe"):
        shim = shim_dir / name
        if not shim.exists() and not shim.is_symlink():
            continue
        if dry_run:
            _info(f"[dry-run] would remove uv python shim: {shim}")
            purged += 1
            continue
        try:
            shim.unlink()
            purged += 1
        except OSError as exc:
            _warn(f"could not remove uv python shim {shim}: {exc}")
    if purged:
        _ok(f"uv python shims: removed {purged} from {shim_dir}")


# ----- Stage 5: data/bin/ tooling (uv / aria2c / 7zr) (DEFAULT CLEAN) --------


def clean_data_bin(repo_root: Path, *, dry_run: bool = False) -> None:
    """Remove ``<repo>/data/bin/`` — Setup.bat-installed local tooling.

    Setup.bat downloads uv.exe, aria2c.exe and 7zr.exe into
    ``data/bin/{uv,aria2c,7zr}/``. They are only used by Setup.bat itself
    and have no value once the project is uninstalled, so the whole
    ``data/bin/`` tree is removed by default. No user data lives here.
    """

    data_bin = repo_root / "data" / "bin"
    if not data_bin.is_dir():
        _info("data/bin: not present (skipping)")
        return
    remove_directory(data_bin, label="data/bin (uv/aria2c/7zr)", dry_run=dry_run)


# ----- Stage 6..10: extras (--all only) --------------------------------------


def clean_uv_cache(targets: UninstallTargets, *, dry_run: bool = False) -> None:
    """Remove uv's package cache. SHARED with other uv projects."""

    if dry_run:
        _info("[dry-run] would run `uv cache clean` (or rmtree fallback)")
        return
    if shutil.which("uv"):
        _run_quiet(["uv", "cache", "clean"])
        _ok("uv cache: cleaned via `uv cache clean`")
    elif targets.uv_cache.exists():
        remove_directory(targets.uv_cache, label="uv cache", dry_run=False)
    else:
        _info("uv cache: not present (skipping)")


def clean_qairt_sdk(*, dry_run: bool = False) -> None:
    """Remove the QAIRT SDK version Setup.bat installs.

    Path: ``C:\\Qualcomm\\AIStack\\QAIRT\\<version>\\``. Other Qualcomm SDK
    installs may share ``C:\\Qualcomm\\AIStack\\``; we only delete the
    SPECIFIC version sub-directory Setup.bat installs (default
    ``DEFAULT_QAIRT_VERSION`` from scripts/qairt_release.json; configurable
    via ``QAIRT_VERSION`` env var, mirrors
    Setup.bat). Other versions / siblings are left untouched.

    After removing the version dir, walk up the parent chain
    (``QAIRT``, ``AIStack``, ``Qualcomm``) and remove each one **only if
    it is empty** — never deleting a parent that still contains other
    Qualcomm SDKs / versions.
    """

    if sys.platform != "win32":
        _info("QAIRT SDK: Windows-only; skipping")
        return

    version = os.environ.get("QAIRT_VERSION", DEFAULT_QAIRT_VERSION)
    sdk_root_str = os.environ.get(
        "QAIRT_SDK_ROOT", rf"C:\Qualcomm\AIStack\QAIRT\{version}"
    )
    sdk_root = Path(sdk_root_str)
    if not sdk_root.is_dir():
        _info(f"QAIRT SDK: not present at {sdk_root}; skipping")
        return
    remove_directory(sdk_root, label=f"QAIRT SDK {version}", dry_run=dry_run)
    if dry_run:
        return

    # Walk up the Qualcomm tree and prune empty parents:
    #   ...\QAIRT\<version>  (just removed)
    #   ...\QAIRT            (remove if empty)
    #   ...\AIStack          (remove if empty)
    #   C:\Qualcomm          (remove if empty)
    # Only direct ancestors matching this fixed chain are considered, so we
    # never accidentally walk above C:\Qualcomm. iterdir() empty check
    # ignores nothing — any sibling SDK/version aborts the walk.
    pruning_root = Path(r"C:\Qualcomm")
    parent = sdk_root.parent
    while parent != parent.parent:  # stop at filesystem root
        try:
            relative = parent.relative_to(pruning_root.parent)
        except ValueError:
            break
        # Only touch the chain Qualcomm\AIStack\QAIRT (3 levels under C:\).
        if len(relative.parts) > 3:
            break
        if not parent.is_dir():
            break
        try:
            has_child = next(parent.iterdir(), None) is not None
        except OSError:
            break
        if has_child:
            _info(f"QAIRT SDK: parent {parent} is not empty; leaving it alone.")
            break
        try:
            parent.rmdir()
            _ok(f"QAIRT SDK: removed empty parent {parent}")
        except OSError as exc:
            _warn(f"QAIRT SDK: could not remove empty parent {parent}: {exc}")
            break
        if parent == pruning_root:
            break
        parent = parent.parent


def clean_playwright_chromium(*, dry_run: bool = False) -> None:
    """Remove the Playwright Chromium browser cache (--dev install only).

    Path: ``%LOCALAPPDATA%\\ms-playwright\\``. Setup.bat --dev runs
    ``playwright install chromium`` which lands here. SHARED with other
    Playwright projects on the same machine.
    """

    lad = _localappdata()
    pw_dir = lad / "ms-playwright"
    if not pw_dir.is_dir():
        _info("Playwright Chromium: not present (skipping)")
        return
    remove_directory(pw_dir, label="Playwright Chromium cache", dry_run=dry_run)


def clean_vendor_caches(repo_root: Path, *, dry_run: bool = False) -> None:
    """Remove vendor/ runtime caches Setup.bat downloads/builds.

    Cleans (under ``<repo>/vendor/``):
      - ``nltk_data/`` (NLTK corpora downloaded by Step 6 predeploy)
      - ``g2p_data/`` (g2p_en pickle cache built by Step 6)
      - ``whl/``      (ARM64 wheels from vendor-deps.7z)
      - ``tiktoken/`` (offline tiktoken BPE cache)
    Plus the ``__pycache__`` byte-code dirs Step 6 pre-compiles.

    Re-running Setup.bat will rebuild ``nltk_data`` / ``g2p_data`` /
    ``__pycache__`` from the network and re-download ``vendor-deps.7z`` to
    repopulate ``whl`` and ``tiktoken``.
    """

    vendor = repo_root / "vendor"
    if not vendor.is_dir():
        _info("vendor caches: vendor/ not present (skipping)")
        return

    for sub in ("nltk_data", "g2p_data", "whl", "tiktoken"):
        d = vendor / sub
        if d.is_dir():
            remove_directory(d, label=f"vendor/{sub}", dry_run=dry_run)

    # Step 6 byte-code __pycache__ dirs scattered under vendor/. Walk and
    # remove any __pycache__ directory under vendor/.
    pyc_count = 0
    if vendor.is_dir():
        for pyc in vendor.rglob("__pycache__"):
            if not pyc.is_dir():
                continue
            if dry_run:
                _info(f"[dry-run] would remove {pyc}")
            else:
                try:
                    shutil.rmtree(pyc, ignore_errors=False)
                except OSError as exc:
                    _warn(f"failed to remove {pyc}: {exc}")
                    continue
            pyc_count += 1
    if pyc_count:
        _ok(f"vendor pyc caches: removed {pyc_count} __pycache__ dir(s)")


def clean_jieba_cache(*, dry_run: bool = False) -> None:
    """Remove ``%TEMP%\\jieba.cache`` written by Setup Step 6 jieba warmup.

    jieba writes a single flat cache file at ``%TEMP%\\jieba.cache``
    (NOT under ``%TEMP%\\QAIModelBuilder\\``, so it is NOT cleaned by the
    default temp purge). Removed under --all; jieba will rebuild it on
    next use.
    """

    temp = _temp_dir()
    cache = temp / "jieba.cache"
    if not cache.exists():
        _info("jieba cache: not present (skipping)")
        return
    if dry_run:
        _info(f"[dry-run] would remove {cache}")
        return
    try:
        cache.unlink()
        _ok(f"jieba cache: removed {cache}")
    except OSError as exc:
        _warn(f"failed to remove {cache}: {exc}")


# ----- Stage 11: Visual Studio 2022 (--vs only, opt-in) ----------------------


def clean_vs_2022(*, dry_run: bool = False, force: bool = False) -> bool:
    """Uninstall the VS 2022 Community product Setup.bat installs/modifies.

    Mirrors how ``scripts/setup/install_vs.ps1`` invokes the VS Installer
    for ``modify``: we run the same ``setup.exe`` with ``uninstall`` instead.
    The VS Installer itself handles UAC elevation when needed (just like
    install/modify) — this script does NOT need to self-elevate.

    Tricky truth about VS Installer exit codes
    ------------------------------------------
    The VS Installer commonly performs a SELF-UPDATE before doing the
    requested operation: the original ``setup.exe`` (e.g. v4.6.58) downloads
    a newer installer (e.g. v4.7.25), spawns it, and **exits 0 immediately**
    even though the real uninstall has not finished. The newly spawned
    installer continues asynchronously and may eventually fail with
    exit ``-2146233079`` (E_FAIL: "No products are registered for instance
    ...") if the catalog is in a weird state. So:

    * Exit 0 from our ``subprocess.call`` is **NOT** a reliable success
      signal; it can mean "self-update spawned a child and detached".
    * The authoritative test is: query ``vswhere`` afterwards. If the
      product is gone, uninstall really succeeded; if it is still there,
      it really failed.

    Implementation:
      1. Launch ``setup.exe uninstall ...`` and wait for that process.
      2. Poll until any ``setup.exe`` from the VS Installer location stops
         running (covers self-update child processes), bounded by a 20-min
         timeout.
      3. Re-query vswhere; success = product no longer registered.
      4. Only then clean ProgramData package cache (cleaning earlier
         would corrupt the running installer's state).

    The ``force`` parameter (set when the operator passed ``--yes``) skips
    the YES/NO confirmation prompt. Without ``--yes`` the operator must
    type literal "YES" to proceed — the standard "irreversible operation"
    pattern.

    Returns True on confirmed uninstall (vswhere shows product gone), False
    when VS still appears registered after the polling window. Skipped
    (returns True) when VS is not present to begin with.
    """

    if sys.platform != "win32":
        _info("VS 2022 uninstall: Windows-only; skipping")
        return True

    vswhere = Path(
        r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    )
    installer = Path(
        r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe"
    )

    def _query_product() -> str:
        """Return the latest installed VS productId, or '' if none."""
        if not vswhere.is_file():
            return ""
        try:
            return subprocess.check_output(  # noqa: S603
                [str(vswhere), "-latest", "-property", "productId"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    product_id = _query_product()

    # Anything to do at all? (Both VS Installer absent AND no residual paths.)
    if not product_id and not vswhere.is_file() and not installer.is_file():
        residual = _vs_residual_paths()
        if not any(p.exists() for p in residual):
            _info("VS 2022 uninstall: VS not present (no installer, no product, no residual); skipping")
            return True

    if dry_run:
        if product_id:
            _info(f"[dry-run] would uninstall VS product {product_id} via {installer}")
        residual = [p for p in _vs_residual_paths() if p.exists()]
        if residual:
            _info(f"[dry-run] would remove {len(residual)} VS residual path(s):")
            for p in residual:
                _info(f"            {p}")
        return True

    # Confirmation gate (irreversible, may break other projects).
    if not force:
        _info("")
        _info("=" * 70)
        if product_id:
            _info(f"  About to UNINSTALL Visual Studio 2022 ({product_id}) and any residual files.")
        else:
            _info("  No VS product is registered, but VS residual files were detected.")
            _info("  About to remove those residual files (install dirs, Start menu shortcuts,")
            _info("  ProgramData / AppData VisualStudio caches).")
        _info("  WARNING: VS is a general-purpose IDE. Other projects on this")
        _info("  machine may depend on it. We cannot tell whether VS was")
        _info("  installed by Setup.bat or by you beforehand.")
        _info("  This will take 5-15 minutes and is irreversible.")
        _info("=" * 70)
        try:
            answer = input("Type YES to proceed (any other input cancels): ").strip()
        except EOFError:
            answer = ""
        if answer != "YES":
            _info("VS uninstall: cancelled by operator.")
            return True  # not an error — user opted out

    # Phase 1 — invoke setup.exe uninstall if a product is currently registered.
    if product_id and installer.is_file():
        _info(f"VS 2022 uninstall: invoking {installer} for {product_id}")
        _info("  (this can take 5-15 minutes; the VS Installer may self-update")
        _info("   first, then spawn a newer installer to do the actual uninstall)")
        try:
            spawn_rc = subprocess.call(  # noqa: S603
                [
                    str(installer),
                    "uninstall",
                    "--productId", product_id,
                    "--channelId", "VisualStudio.17.Release",
                    "--quiet", "--norestart", "--force",
                ],
            )
        except OSError as exc:
            _err(f"VS uninstall: failed to launch installer: {exc}")
            return False

        if spawn_rc == 740:
            _err(
                "VS uninstall: launcher requires administrator elevation (exit=740). "
                "Re-run Uninstall.bat from an elevated shell."
            )
            return False

        # Wait for ALL VS Installer setup.exe processes to exit (covers the
        # self-update -> spawn-child case). Bounded by a generous timeout so we
        # never hang forever.
        _info("VS uninstall: waiting for VS Installer processes to finish...")
        deadline = time.monotonic() + 20 * 60  # 20-minute hard cap
        last_count = -1
        while time.monotonic() < deadline:
            try:
                out = subprocess.check_output(  # noqa: S603
                    [
                        "powershell.exe", "-NoProfile", "-Command",
                        "(Get-Process -Name 'setup','vs_installer','vs_installerservice' "
                        "-ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Path -and $_.Path -like 'C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer*' }).Count",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                count = int(out) if out.isdigit() else 0
            except (OSError, subprocess.CalledProcessError, ValueError):
                count = 0
            if count == 0:
                break
            if count != last_count:
                _info(f"  ({count} VS Installer process(es) still running...)")
                last_count = count
            time.sleep(5)
        else:
            _warn("VS uninstall: VS Installer processes still running after 20 min; "
                  "checking final state anyway.")

        # Authoritative success check: ask vswhere again.
        final_product = _query_product()
        if final_product:
            _err(
                f"VS uninstall: product still registered as '{final_product}' "
                f"(initial spawn exit={spawn_rc}). Uninstall did NOT succeed."
            )
            _err(
                "  Open the Visual Studio Installer GUI and click Uninstall, "
                "then re-run this script (it will skip the gone product)."
            )
            return False
        _ok(f"VS 2022 uninstall: confirmed via vswhere — product '{product_id}' removed.")
    elif product_id and not installer.is_file():
        _warn(
            f"VS uninstall: product '{product_id}' is registered but setup.exe "
            "is missing. Cannot run a clean product uninstall; will purge "
            "residual paths only. You may need the GUI VS Installer to fully "
            "deregister the product."
        )
    else:
        _info("VS uninstall: no product registered; cleaning residual paths only.")

    # Phase 2 — purge residual VS paths (install dirs, Start Menu shortcuts,
    # caches). This runs whether or not Phase 1 ran, so a previously
    # half-broken state (Installer metadata gone but install tree + shortcuts
    # left behind) gets cleaned by a follow-up `--vs` invocation.
    _clean_vs_residual_paths()

    return True


def _vs_residual_paths() -> list[Path]:
    """Fixed list of VS 2022 residual paths to purge during ``--vs``.

    Ordered from most-specific (the install) to least-specific (parent
    dirs that are only removed if empty after the specific paths above
    are gone). The "remove only if empty" parents are NOT in this list;
    we walk them separately in :func:`_clean_vs_residual_paths` so a
    sibling install (e.g. VS 2019, VS 2026) anchors the parent and we
    don't touch it.
    """

    return [
        # Install root (VS 2022 Community).
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community"),
        # Start Menu shortcuts the screenshot showed.
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Visual Studio 2022"),
        Path(os.path.expandvars(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Visual Studio 2022"
        )),
        # VS Installer metadata + per-machine VS state.
        Path(r"C:\ProgramData\Microsoft\VisualStudio"),
        # Per-user VS settings / extension state.
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\VisualStudio")),
        Path(os.path.expandvars(r"%APPDATA%\Microsoft\VisualStudio")),
        # Visual Studio Installer itself (no longer useful once VS is gone;
        # users who want the Installer back can re-run the VS bootstrapper).
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer"),
    ]


def _clean_vs_residual_paths() -> None:
    """Remove VS 2022 residual paths, then prune empty VS parent dirs."""

    removed_any = False
    for p in _vs_residual_paths():
        if not p.exists():
            continue
        try:
            shutil.rmtree(p, ignore_errors=False)
            _ok(f"VS residual: removed {p}")
            removed_any = True
        except OSError as exc:
            _warn(f"VS residual: could not remove {p} ({exc})")

    # Prune empty VS parent directories. `Path.rmdir()` only removes empty
    # dirs, so a sibling install (e.g. VS 2019, VS 2017) anchors the parent
    # and we do NOT touch it. Order matters: deepest first.
    parent_chain_x64 = [
        Path(r"C:\Program Files\Microsoft Visual Studio\2022"),
        Path(r"C:\Program Files\Microsoft Visual Studio"),
    ]
    parent_chain_x86 = [
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio"),
    ]
    for parent in parent_chain_x64 + parent_chain_x86:
        if not parent.is_dir():
            continue
        try:
            has_child = next(parent.iterdir(), None) is not None
        except OSError:
            continue
        if has_child:
            _info(f"VS residual: parent {parent} not empty; leaving it alone.")
            continue
        try:
            parent.rmdir()
            _ok(f"VS residual: removed empty parent {parent}")
            removed_any = True
        except OSError as exc:
            _warn(f"VS residual: could not remove empty parent {parent}: {exc}")

    if not removed_any:
        _info("VS residual: nothing to remove.")


def _vs_uninstall_obsolete_marker() -> None:  # pragma: no cover
    """Empty placeholder so old import paths still resolve cleanly."""
    return None


# Backward-compat shim: existing callers / tests that reference
# ``clean_uv_shared`` keep working — it now means "clean uv pythons + cache",
# which is what the legacy --clean-uv flag did.
def clean_uv_shared(targets: UninstallTargets, *, dry_run: bool = False) -> None:
    """Legacy alias: clean uv-managed Python interpreters + uv cache."""

    clean_uv_pythons(targets, dry_run=dry_run)
    clean_uv_cache(targets, dry_run=dry_run)


# ----- Stage 12: Desktop build toolchain (--desktop / --desktop-rust) --------


def clean_tauri_cli(*, dry_run: bool = False) -> bool:
    """Remove the ``cargo tauri`` subcommand (tauri-cli) Setup Step 5c installs.

    tauri-cli is a project-specific cargo subcommand: ``cargo install tauri-cli``
    drops two files into ``%USERPROFILE%\\.cargo\\bin\\``:

      * ``cargo-tauri.exe`` — the subcommand executable.
      * ``cargo-tauri.pdb`` — debug symbols (optional).

    Both are SAFE to remove unconditionally — only this project's Build.bat
    --desktop / --desktop-dev uses ``cargo tauri``. Other Rust projects on
    the machine that also need tauri-cli will independently
    ``cargo install`` it.

    NOTE: We do NOT touch the Rust toolchain itself (cargo / rustc / .rustup
    / .cargo). That is shared with every other Rust project on the box; see
    ``clean_rust_toolchain`` for the opt-in flag that handles it.

    Idempotent: returns True (and logs SKIP) when tauri-cli is not present.
    """

    if sys.platform != "win32":
        _info("tauri-cli: Windows-only Setup.bat installs it; skipping")
        return True

    cargo_bin = Path(os.path.expanduser("~")) / ".cargo" / "bin"
    targets = [cargo_bin / "cargo-tauri.exe", cargo_bin / "cargo-tauri.pdb"]
    present = [p for p in targets if p.exists()]
    if not present:
        _info("tauri-cli: not present (skipping)")
        return True

    removed = 0
    for path in present:
        if dry_run:
            _info(f"[dry-run] would remove {path}")
            removed += 1
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            _warn(f"failed to remove {path}: {exc}")
    if removed:
        _ok(f"tauri-cli: removed {removed} file(s) from {cargo_bin}")
    return True


def clean_rust_toolchain(*, dry_run: bool = False, force: bool = False) -> bool:
    """Uninstall the Rust toolchain (rustup + cargo + ~/.rustup + ~/.cargo).

    Setup.bat Step 5c installs Rust via ``rustup-init -y --profile minimal``
    when ``--desktop`` is passed. The official, supported way to remove it is
    ``rustup self uninstall -y``, which:

      * Removes ``~/.rustup`` (all toolchains, components, downloaded sources).
      * Removes ``~/.cargo`` (cargo home, including ``bin/`` with every tool
        the user ``cargo install``-ed — NOT just ours).
      * Cleans up PATH entries (no-op for our install since Setup.bat passed
        ``--no-modify-path``, but harmless).

    WARNING — SHARED resource
    -------------------------
    ``~/.cargo/bin/`` and ``~/.rustup/`` are SHARED with every other Rust
    project on this machine. If the user maintains other Rust projects, this
    will yank their toolchain too. Setup.bat may or may not have been the
    party that installed rustup (the user could have installed it manually
    long before Setup.bat ran). We treat this exactly like ``--vs``:

      * Independent of ``--all`` (never implicitly triggered).
      * Interactive YES gate (must type literal "YES") unless ``--yes`` is set.
      * Detection skips silently when rustup is not present at all.

    Returns True on (confirmed) uninstall OR when nothing to do; False on
    operator cancel or on rustup self-uninstall failure.
    """

    if sys.platform != "win32":
        _info("Rust toolchain: Setup.bat installs it Windows-only; skipping")
        return True

    cargo_bin = Path(os.path.expanduser("~")) / ".cargo" / "bin"
    rustup_home = Path(os.path.expanduser("~")) / ".rustup"
    rustup_exe = cargo_bin / "rustup.exe"
    cargo_exe = cargo_bin / "cargo.exe"

    # Nothing to do? Both core paths absent => skip silently.
    if not rustup_exe.is_file() and not cargo_exe.is_file() and not rustup_home.is_dir():
        _info("Rust toolchain: not present (skipping)")
        return True

    if dry_run:
        _info(f"[dry-run] would run: {rustup_exe} self uninstall -y")
        if rustup_home.is_dir():
            _info(f"[dry-run] would remove {rustup_home} (rustup self uninstall handles this)")
        if cargo_bin.is_dir():
            _info(f"[dry-run] would remove {cargo_bin} (rustup self uninstall handles this)")
        return True

    # Confirmation gate (irreversible, may break other projects).
    if not force:
        _info("")
        _info("=" * 70)
        _info("  About to UNINSTALL the Rust toolchain (rustup + cargo + all toolchains).")
        _info(f"  This will remove ~/.rustup and ~/.cargo (including {cargo_bin}).")
        _info("")
        _info("  WARNING: SHARED resource. Any OTHER Rust project on this")
        _info("  machine that uses cargo / rustc / rustup will BREAK. We")
        _info("  cannot tell whether Setup.bat installed rustup or you")
        _info("  installed it manually before Setup.bat ever ran.")
        _info("")
        _info("  Any tools you `cargo install`-ed (ripgrep, fd, etc.) will")
        _info("  also be removed.")
        _info("=" * 70)
        try:
            answer = input("Type YES to proceed (any other input cancels): ").strip()
        except EOFError:
            answer = ""
        if answer != "YES":
            _info("Rust toolchain uninstall: cancelled by operator.")
            return False

    # Phase 1 — official rustup self-uninstall (handles ~/.rustup + ~/.cargo).
    if rustup_exe.is_file():
        _info(f"Rust toolchain: running {rustup_exe.name} self uninstall -y ...")
        rc = _run_quiet([str(rustup_exe), "self", "uninstall", "-y"])
        if rc != 0:
            _warn(
                f"rustup self uninstall exited {rc}; will fall through to "
                "manual rmtree of any residual ~/.rustup and ~/.cargo."
            )
    else:
        _info("Rust toolchain: rustup.exe missing — purging residual paths only.")

    # Phase 2 — defensive rmtree of any leftover paths (rustup self uninstall
    # is usually thorough, but if it failed, or if rustup.exe was already
    # gone and only stale dirs remain, clean them up here).
    for residual in (rustup_home, cargo_bin.parent):  # cargo_bin.parent = ~/.cargo
        if residual.is_dir():
            try:
                shutil.rmtree(residual, ignore_errors=False)
                _ok(f"Rust toolchain: removed {residual}")
            except OSError as exc:
                _warn(f"failed to remove {residual}: {exc}")

    # Phase 3 — authoritative success check.
    still_present = (
        rustup_exe.is_file() or cargo_exe.is_file() or rustup_home.is_dir()
    )
    if still_present:
        _warn(
            "Rust toolchain: some files still present after uninstall. "
            "Check ~/.rustup and ~/.cargo manually."
        )
        return False
    _ok("Rust toolchain: uninstall complete (~/.rustup + ~/.cargo removed).")
    return True


# ----- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uninstall",
        description=(
            "Remove all QAIModelBuilder-generated content outside the "
            "project directory. Replaces Uninstall.bat (P0-L3)."
        ),
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="skip the interactive confirmation prompt",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print actions without removing anything",
    )
    parser.add_argument(
        "--all", action="store_true", dest="all_extras",
        help=(
            "FULL uninstall — also remove uv cache, QAIRT SDK, Playwright "
            "Chromium, vendor/ runtime caches (nltk_data / g2p_data / whl / "
            "tiktoken / pyc), and %%TEMP%%\\jieba.cache. Does NOT touch VS "
            "2022 (a printed reminder asks the user to remove VS components "
            "manually if desired)."
        ),
    )
    parser.add_argument(
        "--clean-uv", action="store_true",
        help=(
            "also remove the uv package cache (%%LOCALAPPDATA%%\\uv\\cache). "
            "SHARED with other uv projects. Implied by --all."
        ),
    )
    parser.add_argument(
        "--vs", action="store_true", dest="uninstall_vs",
        help=(
            "ALSO uninstall Visual Studio 2022 Community via the VS Installer "
            "(setup.exe uninstall). VS is a general-purpose IDE and other "
            "projects on this machine may rely on it; this flag is OPT-IN and "
            "interactive (asks 'YES' before proceeding) unless --yes is set. "
            "Independent of --all. The VS Installer handles UAC elevation "
            "the same way Setup.bat's modify call does."
        ),
    )
    parser.add_argument(
        "--desktop", action="store_true", dest="uninstall_desktop",
        help=(
            "ALSO remove the tauri-cli `cargo tauri` subcommand Setup.bat "
            "Step 5c installed (cargo-tauri.exe + .pdb under "
            "%%USERPROFILE%%\\.cargo\\bin\\). tauri-cli is project-specific "
            "and safe to remove. Does NOT touch the Rust toolchain itself "
            "(cargo / rustc / .rustup / .cargo) — that is SHARED with any "
            "other Rust project on this machine; pass --desktop-rust to "
            "remove it too. Independent of --all."
        ),
    )
    parser.add_argument(
        "--desktop-rust", action="store_true", dest="uninstall_desktop_rust",
        help=(
            "ALSO uninstall the Rust toolchain itself (rustup + cargo + all "
            "toolchains + ~/.rustup + ~/.cargo) via `rustup self uninstall -y`. "
            "WARNING: SHARED with any other Rust project on this machine. "
            "Interactive YES gate unless --yes. Implies --desktop. "
            "Independent of --all."
        ),
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="repo root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--skip-localappdata", action="store_true",
        help=(
            "do NOT remove %%LOCALAPPDATA%%\\QAIModelBuilder (the venvs tree). "
            "Used by Uninstall.bat when this script runs *from* the venv "
            "python.exe inside that tree: deleting the tree would fail with "
            "WinError 5 (the running interpreter image is locked). The .bat "
            "wrapper removes the tree itself after this process exits."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve() if args.repo_root else _detect_repo_root()
    targets = _resolve_targets(repo)

    _info("This will remove (everything Setup.bat installed OUTSIDE the project):")
    _info(f"  1. Running processes under {targets.localappdata_qai}")
    _info(f"  2. {targets.localappdata_qai}/   (~3.8 GB, all venvs + PortableGit + Node.js)")
    _info(f"  3. {targets.temp_qai}/           (temp files)")
    _info(f"  4. Setup install temp archives in {repo / 'data' / 'downloads'} (whitelist; runtime downloads kept)")
    _info(f"  5. {repo / 'data' / 'bin'} (uv / aria2c / 7zr — Setup-installed local tooling)")
    _info(f"  6. uv-managed Python interpreters under {targets.uv_pythons} (cpython-3.13 ARM64 + cpython-3.10 x64)")
    _info("")
    _info(
        "  NOT removed (default): the project directory itself, including data/ "
        "(qai.db, logs, config, and any runtime-downloaded model weights under "
        "data/downloads/) -- that is YOUR data."
    )
    if args.all_extras or args.clean_uv:
        _info("")
        _info("  --all / --clean-uv extras:")
        if args.all_extras or args.clean_uv:
            _info(f"    - {targets.uv_cache} (uv package cache; SHARED with other uv projects)")
        if args.all_extras:
            _info("    - QAIRT SDK at C:\\Qualcomm\\AIStack\\QAIRT\\<version>\\ (Setup-installed version only)")
            _info("    - Playwright Chromium cache (%LOCALAPPDATA%\\ms-playwright\\)")
            _info(f"    - vendor/ runtime caches under {repo / 'vendor'} (nltk_data / g2p_data / whl / tiktoken / pyc)")
            _info("    - %TEMP%\\jieba.cache (Setup Step 6 jieba warmup cache)")
            _info("    NOTE: --all does NOT touch Visual Studio 2022; pass --vs separately to uninstall it.")
    if args.uninstall_vs:
        _info("")
        _info("  --vs extra:")
        _info("    - Visual Studio 2022 Community (via VS Installer setup.exe uninstall)")
        _info("      WARNING: VS is a general-purpose IDE; other projects may rely on it.")
        _info("      Interactive YES confirmation required (unless --yes is set).")
    if args.uninstall_desktop or args.uninstall_desktop_rust:
        _info("")
        _info("  --desktop / --desktop-rust extra:")
        _info("    - tauri-cli (~/.cargo/bin/cargo-tauri.exe + .pdb) — project-specific, safe")
        if args.uninstall_desktop_rust:
            _info("    - Rust toolchain (~/.rustup + ~/.cargo via `rustup self uninstall -y`)")
            _info("      WARNING: SHARED with any other Rust project on this machine.")
            _info("      Interactive YES confirmation required (unless --yes is set).")

    if not args.yes and not args.dry_run:
        try:
            answer = input("\nProceed with uninstall? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in {"y", "yes"}:
            _info("Cancelled.")
            return 1

    stop_running_processes(targets, dry_run=args.dry_run)
    clean_install_temp_downloads(repo, dry_run=args.dry_run)
    # NOTE: data/ is intentionally NOT removed -- it holds user data (qai.db,
    # logs, runtime-downloaded model weights, config). The uninstaller only
    # rolls back what Setup.bat installed OUTSIDE the project directory PLUS
    # install-time temp archives in data/downloads/ (above).
    if args.skip_localappdata:
        # The .bat wrapper will remove this tree after we exit, because the
        # python.exe running THIS script may live inside it (a running EXE
        # image is locked on Windows -> shutil.rmtree would hit WinError 5).
        _info(
            f"QAIModelBuilder envs (LOCALAPPDATA): deferred to Uninstall.bat "
            f"(post-exit): {targets.localappdata_qai}"
        )
    else:
        remove_directory(
            targets.localappdata_qai,
            label="QAIModelBuilder envs (LOCALAPPDATA)",
            dry_run=args.dry_run,
        )
    remove_directory(
        targets.temp_qai, label="QAIModelBuilder temp", dry_run=args.dry_run
    )
    # Default-clean: data/bin tooling + uv-managed Python interpreters.
    clean_data_bin(repo, dry_run=args.dry_run)
    clean_uv_pythons(targets, dry_run=args.dry_run)

    # Opt-in extras.
    if args.all_extras or args.clean_uv:
        clean_uv_cache(targets, dry_run=args.dry_run)
    if args.all_extras:
        clean_qairt_sdk(dry_run=args.dry_run)
        clean_playwright_chromium(dry_run=args.dry_run)
        clean_vendor_caches(repo, dry_run=args.dry_run)
        clean_jieba_cache(dry_run=args.dry_run)
    vs_attempted = False
    if args.uninstall_vs:
        vs_attempted = True
        clean_vs_2022(dry_run=args.dry_run, force=args.yes)
    desktop_attempted = False
    # --desktop-rust implies --desktop (tauri-cli would be left dangling
    # under a deleted ~/.cargo otherwise; cleaner to remove both together).
    if args.uninstall_desktop or args.uninstall_desktop_rust:
        desktop_attempted = True
        clean_tauri_cli(dry_run=args.dry_run)
    if args.uninstall_desktop_rust:
        clean_rust_toolchain(dry_run=args.dry_run, force=args.yes)

    _ok("uninstall: complete.")
    _info(
        "Removed what Setup.bat installed OUTSIDE the project (venvs, "
        "PortableGit, Node.js, temp files, data/bin tooling, uv pythons). "
        "The project directory -- including data/ (qai.db, logs, runtime "
        "model weights, config) -- is left fully intact."
    )

    # Footer hints — vary by which flags were passed.
    bold = "\033[1;33m" if sys.stderr.isatty() else ""
    reset = "\033[0m" if sys.stderr.isatty() else ""

    if not args.all_extras and not args.clean_uv and not vs_attempted and not desktop_attempted:
        # Bare uninstall — point the user at the optional deeper-clean flags.
        _info("")
        _info(f"{bold}{'=' * 70}{reset}")
        _info(f"{bold}  Optional: deeper cleanup{reset}")
        _info(f"{bold}{'=' * 70}{reset}")
        _info(
            "  --all           Also remove uv cache, QAIRT SDK, Playwright Chromium,")
        _info(
            "                  vendor/ runtime caches, and %TEMP%\\jieba.cache.")
        _info(
            "  --vs            Also UNINSTALL Visual Studio 2022 Community via the VS")
        _info(
            "                  Installer. WARNING: shared IDE; interactive confirmation.")
        _info(
            "  --desktop       Also remove tauri-cli (Setup Step 5c `cargo install` artifact).")
        _info(
            "                  Safe — project-specific. Does NOT touch the Rust toolchain.")
        _info(
            "  --desktop-rust  Also uninstall Rust toolchain (rustup + ~/.cargo + ~/.rustup).")
        _info(
            "                  WARNING: shared with every other Rust project; interactive YES.")
        _info("")
    elif args.all_extras and not vs_attempted:
        # --all but no --vs: keep the existing VS reminder.
        _info("")
        _info(f"{bold}{'=' * 70}{reset}")
        _info(f"{bold}  IMPORTANT: Visual Studio 2022 was NOT uninstalled.{reset}")
        _info(f"{bold}{'=' * 70}{reset}")
        _info(
            "  Setup.bat may have added components (VC.Tools.ARM64, "
            "Windows11SDK, CMake, clang-cl) to your existing VS install. We"
        )
        _info(
            "  do NOT remove them automatically because other projects on "
            "this machine may rely on Visual Studio 2022."
        )
        _info(
            "  To uninstall VS too: re-run with --vs (or open the Visual "
            "Studio Installer and click Uninstall / Modify)."
        )
        _info(
            "  VS lives under 'C:\\Program Files\\Microsoft Visual Studio\\2022\\Community'."
        )
        _info("")

    return 0


# ----- Helpers --------------------------------------------------------------


def _run_quiet(cmd: Sequence[str]) -> int:
    try:
        return subprocess.call(  # noqa: S603 — operator-supplied
            list(cmd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 1


def _info(msg: str) -> None:
    _stderr(f"[uninstall] {msg}\n")


def _ok(msg: str) -> None:
    _stderr(f"[uninstall] OK: {msg}\n")


def _warn(msg: str) -> None:
    _stderr(f"[uninstall] WARN: {msg}\n")


def _err(msg: str) -> None:
    _stderr(f"[uninstall] ERROR: {msg}\n")


def _stderr(msg: str) -> None:
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except OSError:
        pass


__all__ = [
    "UninstallTargets",
    "clean_data_bin",
    "clean_install_temp_downloads",
    "clean_jieba_cache",
    "clean_playwright_chromium",
    "clean_qairt_sdk",
    "clean_rust_toolchain",
    "clean_tauri_cli",
    "clean_uv_cache",
    "clean_uv_pythons",
    "clean_uv_shared",
    "clean_vendor_caches",
    "clean_vs_2022",
    "main",
    "remove_directory",
    "stop_running_processes",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())