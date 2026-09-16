# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Install prebuilt Windows ARM64 (win_arm64) wheels for packages that have NO
precompiled wheel on the default PyPI index for the running Python version
on WoS (Windows on Snapdragon).

EasyOCR depends on ``opencv-python`` (and, transitively, on
``scikit-image`` / ``scipy`` / ``shapely`` / ``pyclipper``). On ARM64 Windows
PyPI only ships source distributions for these, so a plain ``pip install``
tries to compile OpenCV/scipy from source and fails.

The community ``cgohlke/win_arm64-wheels`` release ships ONE big archive that
bundles all wheels for a given CPython minor version. This helper downloads
that archive, extracts ONLY the wheels we need, and installs them with pip
(``--no-deps``) so the subsequent ``pip install --no-deps easyocr`` sees its
native dependencies as already satisfied and skips the source build.

Only the standard library is used so this can run before requests/tqdm are
guaranteed to be installed. It never hard-fails: if a download or install
fails, the sample still works with a clear message pointing at the cause.
"""
import os
import sys
import ssl
import tempfile
import zipfile
import subprocess
import urllib.request
import urllib.error

# The bundled archive for the running interpreter's cp3xx win_arm64 wheels.
# cgohlke publishes one release per CPython minor version; pick the tag that
# matches sys.version_info so we never try to `pip install` a cp313 wheel
# into a cp312 venv (or vice versa) - that fails with "not a supported wheel
# on this platform" and silently leaves numpy/opencv/scipy uninstalled.
_ARCHIVE_BY_PYVER = {
    (3, 11): ("v2023.12.6", "2023.12.6-experimental-cp311-win_arm64.whl.zip"),
    (3, 12): ("v2024.11.3", "2024.11.3-experimental-cp312-win_arm64.whl.zip"),
    (3, 13): ("v2025.3.31", "2025.3.31-experimental-cp313-win_arm64.whl.zip"),
}
_PYVER = sys.version_info[:2]
if _PYVER not in _ARCHIVE_BY_PYVER:
    # Fall back to the closest known tag rather than hard-failing.
    _closest = min(_ARCHIVE_BY_PYVER, key=lambda v: abs(v[0] * 100 + v[1] - (_PYVER[0] * 100 + _PYVER[1])))
    print(f"[arm64-wheels] WARN: no known wheel bundle for Python {_PYVER[0]}.{_PYVER[1]}; "
          f"trying closest match {_closest[0]}.{_closest[1]} (may fail).")
    _PYVER = _closest
_TAG, _ASSET = _ARCHIVE_BY_PYVER[_PYVER]
_ARCHIVE_URL = (
    f"https://github.com/cgohlke/win_arm64-wheels/releases/download/{_TAG}/{_ASSET}"
)

# Wheels to extract+install, matched by filename prefix (case-insensitive).
# These are EasyOCR's native deps that have no ARM64 wheel on PyPI.
#
# IMPORTANT: numpy is included and MUST come from this SAME bundle. cgohlke's
# scipy is built against a specific numpy version and links a matching
# OpenBLAS DLL (shipped inside that numpy's numpy.libs folder). If a
# different numpy (e.g. PyPI's) is installed instead, scipy's _fblas.pyd
# fails at import with:
#     ImportError: DLL load failed while importing _fblas: The specified
#     module could not be found.
# which in turn breaks `import easyocr`. Installing numpy from the bundle
# keeps numpy + scipy + their OpenBLAS DLL in lockstep.
_WANTED_PREFIXES = (
    "numpy-",
    "opencv_python-",
    "opencv_python_headless-",
    "scikit_image-",
    "scipy-",
    "shapely-",
    "pyclipper-",
)


def _open_url(url):
    """Open a URL, falling back to an unverified TLS context when a corporate
    proxy presents a CA cert that fails strict verification (seen on WoS as
    'CA cert does not include key usage extension')."""
    req = urllib.request.Request(url, headers={"User-Agent": "setup_env.bat"})
    try:
        return urllib.request.urlopen(req, context=ssl.create_default_context())
    except (ssl.SSLError, urllib.error.URLError) as exc:
        print(f"[arm64-wheels] TLS verification failed ({exc}); retrying "
              f"without certificate verification ...")
        return urllib.request.urlopen(req, context=ssl._create_unverified_context())


def _download(url, dst):
    print(f"[arm64-wheels] Downloading bundle (~500 MB, one-time):\n"
          f"               {url}")
    done = 0
    with _open_url(url) as resp, open(dst, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                sys.stdout.write(f"\r[arm64-wheels]   {pct:3d}%  "
                                 f"({done >> 20} / {total >> 20} MB)")
                sys.stdout.flush()
    sys.stdout.write("\n")


def _pip_install(whl_path):
    print(f"[arm64-wheels] Installing {os.path.basename(whl_path)} ...")
    return subprocess.call(
        [sys.executable, "-m", "pip", "install", "--force-reinstall",
         "--no-deps", whl_path]
    )


def main():
    # Allow reusing an already-downloaded bundle to avoid the ~500 MB download.
    # Priority: CLI arg  >  ARM64_WHEEL_BUNDLE env var  >  download.
    # Only reuse it if its filename matches the asset for THIS interpreter's
    # Python version - otherwise we would silently feed cp313 wheels to a
    # cp312 venv (or vice versa), which pip rejects as "not a supported wheel".
    local_bundle = None
    for cand in (sys.argv[1] if len(sys.argv) > 1 else None,
                 os.environ.get("ARM64_WHEEL_BUNDLE")):
        if cand and os.path.isfile(cand):
            if os.path.basename(cand) != _ASSET:
                print(f"[arm64-wheels] WARN: ignoring cached bundle {cand!r} - "
                      f"it does not match the expected asset for Python "
                      f"{_PYVER[0]}.{_PYVER[1]} ({_ASSET}); will download instead.")
                continue
            local_bundle = cand
            break

    with tempfile.TemporaryDirectory(prefix="arm64whl_") as tmp:
        if local_bundle:
            zip_path = local_bundle
            print(f"[arm64-wheels] Using existing bundle (no download): {zip_path}")
        else:
            zip_path = os.path.join(tmp, "win_arm64_bundle.whl.zip")
            try:
                _download(_ARCHIVE_URL, zip_path)
            except Exception as exc:                          # noqa: BLE001
                print(f"[arm64-wheels] WARN: download failed: {exc}")
                print("[arm64-wheels] Skipping; EasyOCR may not install on ARM64.")
                return 0
            # Persist alongside this script so setup_env.bat's cache lookup
            # (keyed by cp-tag) finds it next run and skips the ~500 MB download.
            try:
                cache_dir = os.path.dirname(os.path.abspath(__file__))
                cache_path = os.path.join(cache_dir, f"arm64_bundle_cp{_PYVER[0]}{_PYVER[1]}.zip")
                import shutil
                shutil.copyfile(zip_path, cache_path)
                print(f"[arm64-wheels] Cached bundle for reuse at {cache_path}")
            except Exception as exc:                          # noqa: BLE001
                print(f"[arm64-wheels] WARN: could not cache bundle for reuse: {exc}")

        extracted = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    base = os.path.basename(member).lower()
                    if not base.endswith(".whl"):
                        continue
                    if not base.startswith(_WANTED_PREFIXES):
                        continue
                    out = os.path.join(tmp, os.path.basename(member))
                    with zf.open(member) as src, open(out, "wb") as dst:
                        dst.write(src.read())
                    extracted.append(out)
        except Exception as exc:                          # noqa: BLE001
            print(f"[arm64-wheels] WARN: could not read bundle: {exc}")
            return 0

        if not extracted:
            print("[arm64-wheels] WARN: no matching wheels found in bundle.")
            return 0

        # Install in _WANTED_PREFIXES order so numpy lands first and the rest
        # bind against it (they are installed with --no-deps, so pip will not
        # pull a mismatching numpy from PyPI).
        def _rank(path):
            b = os.path.basename(path).lower()
            for i, pref in enumerate(_WANTED_PREFIXES):
                if b.startswith(pref):
                    return i
            return len(_WANTED_PREFIXES)
        extracted.sort(key=_rank)

        ok = True
        for whl in extracted:
            if _pip_install(whl) != 0:
                print(f"[arm64-wheels] WARN: pip install failed for "
                      f"{os.path.basename(whl)}")
                ok = False

        if ok:
            print("[arm64-wheels] Installed: " +
                  ", ".join(os.path.basename(w) for w in extracted))
        else:
            print("[arm64-wheels] Some wheels failed; EasyOCR may not install.")

    # Never hard-fail the parent script.
    return 0


if __name__ == "__main__":
    sys.exit(main())
