# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
SpeechTranslator.py
=====================================================================
On-device bidirectional speech translator running on the Snapdragon NPU
(HTP) via QAI AppBuilder. The ASR / MT / TTS networks run as QNN context
binaries (.bin) on the Hexagon NPU; audio capture/playback, tokenizers,
G2P / BERT text front-ends and glue code run on the CPU. After the
one-time model download, no audio or text ever leaves the device.

Pipelines (choose with --direction):

  zh2en  (default):
    Chinese speech (mic or WAV) --Zipformer ASR--> Chinese text
                                 --OpusMT zh->en--> English text
                                 --PiperTTS-EN----> English speech

  en2zh:
    English speech (mic or WAV) --Zipformer ASR--> English text
                                 --OpusMT en->zh--> Chinese text
                                 --MeloTTS-ZH-----> Chinese speech

Zipformer is a single Mandarin+English mixed streaming ASR model, so
it is reused for both directions instead of adding a second ASR
model. All five sub-model groups (Zipformer x3, OpusMT x2 per
direction, PiperTTS-EN x4, MeloTTS-ZH x3) are QNN context binaries
(.bin only -- no ONNX / DLC) run through qai_appbuilder.QNNContext, and
are auto-downloaded on first use into ./models/<model_name>/ (next to
this script, or --models_dir). The download URL is chosen by chip
(Snapdragon X Elite = 8380, X2 Elite = 8480), auto-detected or forced
with --chipset. A run only loads/downloads the models its --direction
(and input type) actually needs: --text skips the ASR model entirely.
This file is self-contained: it does not import samples/shared/python.

Usage (works from any directory):
    python SpeechTranslator.py --direction zh2en
        Record Chinese speech from the microphone (auto-stops after you
        finish speaking), translate it, and speak the English result.

    python SpeechTranslator.py --direction en2zh --audio in.wav
        Translate an English WAV file and speak the Chinese result.

    python SpeechTranslator.py --direction en2zh --audio
        --audio without a file: download a sample clip into ./input/ and use
        it (en2zh: jfk.wav from AI Hub, zh2en: 0.wav from sherpa-onnx).

    python SpeechTranslator.py --direction zh2en --text "你好，世界"
        Skip the microphone/ASR step and translate+speak the given
        text directly (useful without a working microphone).

    python SpeechTranslator.py --direction zh2en --no_play --out out.wav
        Save the synthesized speech to out.wav (relative paths are resolved
        against the current directory) without playing it.

    Microphone captures are NOT kept unless --keep_capture is given.

Run `python SpeechTranslator.py --help` for the full option list.

Use as a library (the module is import-safe; nothing runs on import):
    import SpeechTranslator as st
    st.Init("zh2en", need_asr=False)
    r = st.Inference("zh2en", text="你好，世界")   # r["translated_text"], r["audio"], r["sample_rate"]
    st.Release("zh2en")
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import types
import zipfile
import zlib
from pathlib import Path
from typing import Any

import numpy as np

# NOTE: sounddevice is imported lazily (record_from_mic / play_audio) so that
# --text / --no_play runs and library use do not require PortAudio at all.
# Always import it through _import_sounddevice() (SECTION 5): sounddevice picks
# its bundled PortAudio DLL from platform.machine(), which reports the HOST CPU
# (ARM64) even inside an emulated x64 Python, so it would load the ARM64 DLL
# into an x64 process and fail.

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from qai_appbuilder import (  # noqa: E402
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)

SCRIPT_DIR = Path(__file__).resolve().parent               # .../samples/apps/speech-translator
APP_DIR = SCRIPT_DIR

# ─────────────────────────────────────────────────────────────────────────────
# Model storage: samples/apps/speech-translator/models/<model_name>/
# ─────────────────────────────────────────────────────────────────────────────
MODELS_DIR = APP_DIR / "models"
_ZIPFORMER_DIR = MODELS_DIR / "zipformer"
_OPUS_ZH_EN_DIR = MODELS_DIR / "opus_mt_zh_en"
_OPUS_EN_ZH_DIR = MODELS_DIR / "opus_mt_en_zh"
_PIPERTTS_EN_DIR = MODELS_DIR / "pipertts_en"
_MELOTTS_ZH_DIR = MODELS_DIR / "melotts_zh"

OUTPUT_DIR = APP_DIR / "output"
INPUT_DIR = APP_DIR / "input"        # default sample clips for a bare `--audio`


def set_models_dir(path) -> None:
    """Store/read the model binaries under `path`/<model_name>/ instead of the
    default ./models next to this script. Call before Init()."""
    global MODELS_DIR, _ZIPFORMER_DIR, _OPUS_ZH_EN_DIR, _OPUS_EN_ZH_DIR
    global _PIPERTTS_EN_DIR, _MELOTTS_ZH_DIR
    MODELS_DIR = Path(path).expanduser().resolve()
    _ZIPFORMER_DIR = MODELS_DIR / "zipformer"
    _OPUS_ZH_EN_DIR = MODELS_DIR / "opus_mt_zh_en"
    _OPUS_EN_ZH_DIR = MODELS_DIR / "opus_mt_en_zh"
    _PIPERTTS_EN_DIR = MODELS_DIR / "pipertts_en"
    _MELOTTS_ZH_DIR = MODELS_DIR / "melotts_zh"
    if "_OPUS_CONFIG" in globals():
        _OPUS_CONFIG["zh2en"]["model_dir"] = _OPUS_ZH_EN_DIR
        _OPUS_CONFIG["en2zh"]["model_dir"] = _OPUS_EN_ZH_DIR

# ═════════════════════════════════════════════════════════════════════════
# SECTION 0 -- Self-contained helpers: chip detection, model download, TLS
# (ported from samples/shared/python/install.py so this script does not
# import it: detect_device_model / download_url and their helpers)
# ═════════════════════════════════════════════════════════════════════════

# --- Chip selection -------------------------------------------------------
# AI Hub context binaries are compiled per chip: X Elite = 8380 (HTP v73),
# X2 Elite = 8480. Every model table below is keyed by these device names.
_DEV_X = "snapdragon_x_elite"
_DEV_X2 = "snapdragon_x2_elite"
_DEVICE_ALIASES = {
    "snapdragon_x_elite": _DEV_X, "x_elite": _DEV_X, "xelite": _DEV_X,
    "x1e": _DEV_X, "8380": _DEV_X,
    "snapdragon_x2_elite": _DEV_X2, "x2_elite": _DEV_X2, "x2elite": _DEV_X2,
    "x2e": _DEV_X2, "8480": _DEV_X2,
}
_device_state: dict[str, str | None] = {"override": None, "detected": None}


def normalize_chipset(value: str) -> str | None:
    """Map a --chipset value (x_elite / x2_elite / 8380 / 8480 / ...) to a device name."""
    return _DEVICE_ALIASES.get(str(value).strip().lower().replace("-", "_").replace(" ", "_"))


def set_chipset(value: str | None) -> None:
    """Force the target chip (x_elite / x2_elite / 8380 / 8480), or None to
    auto-detect from the CPU name. Call before Init()."""
    if value is None:
        _device_state["override"] = None
        return
    dev = normalize_chipset(value)
    if dev is None:
        raise ValueError(f"unknown chipset {value!r}; use x_elite (8380) or x2_elite (8480)")
    _device_state["override"] = dev


def _get_cpu_name() -> str:
    """Collect every CPU-name string we can get (env, wmic, PowerShell, platform).
    `wmic` is removed on recent Windows 11 builds, hence the fallbacks."""
    parts: list[str] = []
    pid = os.environ.get("PROCESSOR_IDENTIFIER")
    if pid:
        parts.append(pid)
    for cmd in (["wmic", "cpu", "get", "Name", "/value"],
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor).Name"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
        except Exception:
            continue
        got = False
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Name="):
                line = line[len("Name="):].strip()
            if line:
                parts.append(line)
                got = True
        if got:
            break
    try:
        import platform
        parts.append(platform.processor())
    except Exception:
        pass
    return " | ".join(dict.fromkeys(p for p in parts if p))


def detect_device_model() -> str:
    """Return 'snapdragon_x_elite' or 'snapdragon_x2_elite'.

    --chipset wins; otherwise the CPU name is inspected. Raises RuntimeError
    (with a hint) when the chip is unknown instead of exiting the process or
    silently picking a context binary that was built for a different chip."""
    if _device_state["override"]:
        return _device_state["override"]
    if _device_state["detected"]:
        return _device_state["detected"]
    cpu = _get_cpu_name()
    print(f"[INFO] Detected CPU: {cpu}")
    c = cpu.lower()
    if "family 8 model 2" in c or re.search(r"\bx2e?\b", c):
        dev = _DEV_X2
    elif "family 8 model 1" in c or "x elite" in c or "x1e" in c:
        dev = _DEV_X
    else:
        raise RuntimeError(
            f"Cannot map CPU {cpu!r} to a supported chip (Snapdragon X Elite / X2 Elite). "
            f"Pass --chipset x_elite|x2_elite (or 8380|8480)."
        )
    _device_state["detected"] = dev
    return dev


# --- TLS ------------------------------------------------------------------
# On Python >= 3.13 the default SSL context sets VERIFY_X509_STRICT, which
# rejects the AWS S3 / CDN chain with "CA cert does not include key usage
# extension". We clear ONLY that strictness flag: certificate-chain and
# hostname verification stay ON (we never use verify=False). --strict_tls
# keeps Python's default strict behaviour.
_STRICT_TLS = False


def _make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()          # OS trust store (incl. corporate roots)
    try:                                         # plus the Mozilla bundle, if available
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    if not _STRICT_TLS and hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _http_session():
    import requests
    from requests.adapters import HTTPAdapter
    ctx = _make_ssl_context()

    class _Adapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw["ssl_context"] = ctx
            return super().init_poolmanager(*a, **kw)

        def proxy_manager_for(self, *a, **kw):
            kw["ssl_context"] = ctx
            return super().proxy_manager_for(*a, **kw)

    s = requests.Session()
    s.mount("https://", _Adapter())
    return s


_hf_ssl_installed = False


def _install_hf_ssl_context() -> None:
    """Give huggingface_hub the same relaxed-strictness - but still verifying -
    SSL context as the model downloads above. Needed by the OpusMT tokenizer
    and MeloTTS (BERT) downloads. Handles both the httpx based hub (>= 1.0) and
    the requests based one (< 1.0). Best effort; safe to call repeatedly."""
    global _hf_ssl_installed
    if _hf_ssl_installed:
        return
    _hf_ssl_installed = True
    try:
        import huggingface_hub
    except Exception:
        return
    try:
        if hasattr(huggingface_hub, "set_client_factory"):          # hub >= 1.0 (httpx)
            import httpx
            ctx = _make_ssl_context()
            try:
                from huggingface_hub.utils._http import hf_request_event_hook
                hooks = {"request": [hf_request_event_hook]}
            except Exception:
                hooks = {}

            def _factory():
                return httpx.Client(event_hooks=hooks, follow_redirects=True,
                                    timeout=httpx.Timeout(120.0, connect=30.0), verify=ctx)

            huggingface_hub.set_client_factory(_factory)
        elif hasattr(huggingface_hub, "configure_http_backend"):    # hub < 1.0 (requests)
            huggingface_hub.configure_http_backend(backend_factory=_http_session)
    except Exception:
        pass


# --- Download -------------------------------------------------------------
class _Progress:
    def __init__(self, total: int, desc: str):
        self._bar = None
        try:
            from tqdm import tqdm
            self._bar = tqdm(total=total or None, unit="B", unit_scale=True, desc=desc)
        except Exception:
            self._total, self._done, self._desc, self._last = total, 0, desc, -1

    def update(self, n: int) -> None:
        if self._bar is not None:
            self._bar.update(n)
            return
        self._done += n
        if self._total:
            pct = int(self._done * 100 / self._total)
            if pct != self._last and pct % 5 == 0:
                self._last = pct
                print(f"\r{self._desc}: {pct}%", end="", flush=True)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
        else:
            print()


def _remote_size(url: str) -> int | None:
    try:
        with _http_session() as s:
            r = s.head(url, allow_redirects=True, timeout=30)
            return int(r.headers["Content-Length"])
    except Exception:
        return None


def _package_ready(url: str, filepath: str) -> bool:
    """True if filepath already exists and its size equals the remote size."""
    if not os.path.isfile(filepath):
        return False
    remote = _remote_size(url)
    return remote is not None and remote == os.path.getsize(filepath)


def _download_requests(url: str, filepath: str, chunk_size: int = 1 << 20) -> None:
    part = filepath + ".part"
    with _http_session() as s:
        with s.get(url, stream=True, timeout=(15, 60)) as r:
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code} for {url}")
            total = int(r.headers.get("content-length", 0))
            bar = _Progress(total, os.path.basename(filepath))
            got = 0
            try:
                with open(part, "wb") as f:
                    for data in r.iter_content(chunk_size=chunk_size):
                        f.write(data)
                        got += len(data)
                        bar.update(len(data))
            finally:
                bar.close()
    if total and got != total:
        raise IOError(f"incomplete download ({got} of {total} bytes)")
    os.replace(part, filepath)


def _download_urllib(url: str, filepath: str, chunk_size: int = 1 << 20) -> None:
    import urllib.request
    part = filepath + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "qai-appbuilder-speech-translator"})
    with urllib.request.urlopen(req, context=_make_ssl_context(), timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        bar = _Progress(total, os.path.basename(filepath))
        got = 0
        try:
            with open(part, "wb") as f:
                while True:
                    data = r.read(chunk_size)
                    if not data:
                        break
                    f.write(data)
                    got += len(data)
                    bar.update(len(data))
        finally:
            bar.close()
    if total and got != total:
        raise IOError(f"incomplete download ({got} of {total} bytes)")
    os.replace(part, filepath)


def _download_pywget(url: str, filepath: str) -> None:
    from py3_wget import download_file   # optional last-resort fallback
    download_file(url, output_path=filepath, overwrite=True)


def download_url(url: str, filepath, desc: str | None = None, fail: str | None = None) -> bool:
    """Download `url` to `filepath` (atomically, via a .part file).

    Tries requests, then urllib, then py3_wget (if installed). Returns True on
    success. An existing file whose size equals the remote Content-Length is
    reused. Never disables certificate verification."""
    filepath = str(filepath)
    if _package_ready(url, filepath):
        return True
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    print(desc if desc else f"Downloading {os.path.basename(filepath)}...")

    errors = []
    for name, fn in (("requests", _download_requests),
                     ("urllib", _download_urllib),
                     ("py3_wget", _download_pywget)):
        try:
            fn(url, filepath)
            return True
        except ImportError:
            continue
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"  [{name}] {type(e).__name__}: {e}")
            try:
                os.remove(filepath + ".part")
            except OSError:
                pass
    print()
    print(fail or f"Failed to download file from {url}. Please download it manually "
                  f"and place it at {filepath}.")
    for line in errors:
        print(line)
    print("If you still can't download, please consider using a proxy.")
    return False


def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """extractall() that refuses members escaping `dest` (zip-slip)."""
    dest = dest.resolve()
    for m in zf.infolist():
        target = (dest / m.filename).resolve()
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"Unsafe path in zip archive: {m.filename}")
    zf.extractall(dest)


def _find_dir_with(root: Path, required: list[str]) -> Path | None:
    """First directory under (and including) root holding all `required` files.
    Independent of the release's folder name, so a re-packaged zip still works."""
    if not root.is_dir():
        return None
    if all((root / m).is_file() for m in required):
        return root
    for sub in sorted(p for p in root.rglob("*") if p.is_dir()):
        if all((sub / m).is_file() for m in required):
            return sub
    return None


def _download_zip(url: str, work_dir: Path, tag: str, what: str) -> Path:
    """Download the model zip into work_dir (atomically, via download_url's
    .part file) and return its path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    zip_path = work_dir / os.path.basename(url)
    print(f"[{tag}] Downloading models: {url}")
    ok = download_url(
        url, str(zip_path),
        desc=f"Downloading {what} models...",
        fail=f"\nFailed to download {what} models from:\n  {url}\n"
             f"Please download manually and extract to: {work_dir}",
    )
    if not ok or not zip_path.is_file():
        raise RuntimeError(f"{what} model download failed from {url}")
    return zip_path


# --- Model install bookkeeping ---------------------------------------------
# Every model folder gets a stamp recording WHICH archive (release + chip,
# both encoded in the download URL) its binaries came from, so that
#   * binaries built for the other chip (wrong auto-detection corrected with
#     --chipset, or a --models_dir shared between an X Elite and an X2 Elite
#     machine) or from another release are replaced instead of being loaded;
#   * a manual install (no stamp) is checked against its metadata.json and
#     adopted -- never deleted -- when it matches the chip;
#   * an install interrupted while files were being moved into place (marker
#     file left behind) is detected and redone instead of being reused.
_STAMP_NAME = ".model_stamp.json"
_INSTALLING_NAME = ".installing"
_EXTRACT_PREFIX = ".extract_"


def _read_stamp(work_dir: Path) -> dict | None:
    try:
        data = json.loads((work_dir / _STAMP_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_stamp(work_dir: Path, url: str, device: str, source: str) -> None:
    stamp = {"url": url, "device": device, "source": source,
             "installed": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = work_dir / (_STAMP_NAME + ".tmp")
    tmp.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    os.replace(tmp, work_dir / _STAMP_NAME)


def _device_from_metadata(model_dir: Path) -> str | None:
    """Chip an AI Hub release was compiled for, read from its metadata.json
    (chipset_attributes.name / aliases, e.g. 'sc8380xp'). None if unknown."""
    try:
        meta = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
        attrs = meta.get("chipset_attributes") or {}
        names = [str(attrs.get("name", ""))] + [str(a) for a in attrs.get("aliases") or []]
    except (OSError, ValueError, AttributeError):
        return None
    text = " ".join(names).lower().replace("_", "-")
    if "x2-elite" in text or "8480" in text:
        return _DEV_X2
    if "x-elite" in text or "8380" in text:
        return _DEV_X
    return None


def _detect_device_or_none() -> str | None:
    """detect_device_model(), but None instead of raising when the chip cannot
    be identified (an already installed model can then still be used)."""
    try:
        return detect_device_model()
    except RuntimeError:
        return None


def _remove_stale_extract_dirs(work_dir: Path) -> None:
    for d in work_dir.glob(_EXTRACT_PREFIX + "*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

def _install_model_zip(url: str, work_dir: Path, required: list[str],
                       device: str, tag: str, what: str) -> None:
    """Download + extract the release zip into a temporary folder, check that
    it contains `required`, then move its files into work_dir and write the
    stamp. work_dir is untouched until the archive is fully extracted
    (zipfile verifies every member's CRC), and the .installing marker covers
    the short move phase, so an interrupted install can never leave a
    half-written model that a later run would reuse."""
    zip_path = _download_zip(url, work_dir, tag, what)
    print(f"[{tag}] Extracting models to {work_dir} ...")
    tmp_dir = Path(tempfile.mkdtemp(prefix=_EXTRACT_PREFIX, dir=str(work_dir)))
    try:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extractall(zf, tmp_dir)
        except (zipfile.BadZipFile, zlib.error, EOFError) as e:
            zip_path.unlink(missing_ok=True)      # corrupt download: force a fresh fetch next run
            raise RuntimeError(f"{what} archive is corrupt ({e}); please re-run.") from e
        src_dir = _find_dir_with(tmp_dir, required)
        if src_dir is None:
            raise FileNotFoundError(f"{what} archive {zip_path.name} does not contain {required}.")

        marker = work_dir / _INSTALLING_NAME
        marker.write_text(url, encoding="utf-8")
        (work_dir / _STAMP_NAME).unlink(missing_ok=True)
        # Flatten everything below the release folder into work_dir (the
        # OpusMT tokenizer files etc. must sit next to the .bin files).
        for item in sorted(src_dir.rglob("*")):
            if item.is_file():
                os.replace(item, work_dir / item.name)
        _write_stamp(work_dir, url, device, source="download")
        marker.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    zip_path.unlink(missing_ok=True)


def _ensure_model(work_dir: Path, required: list[str], urls: dict[str, str],
                  tag: str, what: str, release_ok: bool = True) -> Path:
    """Return the folder holding the `required` model files for the target
    chip, downloading / replacing them when they are missing, from an
    interrupted install, or built for another chip / release.

    An install without a stamp (manual, or made by an older version of this
    script) is adopted when its metadata.json matches the target chip (or
    cannot be checked) -- it is never deleted just for lacking a stamp.
    release_ok=False marks such an unstamped install as a known-bad release
    that must be replaced."""
    work_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_extract_dirs(work_dir)
    device = _detect_device_or_none()
    url = urls[device] if device else None

    interrupted = (work_dir / _INSTALLING_NAME).exists()
    model_dir = None if interrupted else _find_dir_with(work_dir, required)
    if interrupted:
        print(f"[{tag}][WARN] A previous model install was interrupted -- reinstalling.")

    if model_dir is not None:
        stamp = _read_stamp(work_dir)
        if stamp is not None:
            if url is None or stamp.get("url") == url:
                print(f"[{tag}] Models ready: {model_dir}")
                return model_dir
            print(f"[{tag}] Installed models ({stamp.get('device')}) do not match the target "
                  f"({device}: {url}) -- replacing them.")
        elif release_ok:
            found = _device_from_metadata(model_dir)
            if device is None or found is None or found == device:
                if device is not None:
                    _write_stamp(work_dir, url, device,
                                 source="adopted" if found else "adopted-unverified")
                if found is None:
                    print(f"[{tag}][WARN] Cannot verify which chip the models in {model_dir} were "
                          f"built for (no usable metadata.json); using them as-is.")
                print(f"[{tag}] Models ready: {model_dir}")
                return model_dir
            print(f"[{tag}] Installed models were built for {found}, but the target is "
                  f"{device} -- replacing them.")

    if url is None:
        detect_device_model()          # raises RuntimeError with the --chipset hint
    print(f"[{tag}] Device model: {device}")
    if model_dir is not None and model_dir.resolve() != work_dir.resolve():
        shutil.rmtree(model_dir, ignore_errors=True)       # stale release sub-folder
    _install_model_zip(url, work_dir, required, device, tag, what)
    model_dir = _find_dir_with(work_dir, required)
    if model_dir is None:
        raise FileNotFoundError(f"No folder with {required} under {work_dir} after extraction.")
    return model_dir


# --- HTP performance profile ----------------------------------------------
class _HtpBurst:
    """Hold the HTP BURST profile once across a whole compute-heavy task
    (nest-safe). Setting/releasing it per inference call only adds latency."""
    _depth = 0

    def __enter__(self):
        if _HtpBurst._depth == 0:
            PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
        _HtpBurst._depth += 1
        return self

    def __exit__(self, *exc):
        _HtpBurst._depth -= 1
        if _HtpBurst._depth == 0:
            PerfProfile.RelPerfProfileGlobal()
        return False


_QNN_CONFIGURED = False


def _ensure_qnn_config():
    """Configure the QNN HTP runtime exactly once per process.

    QNNConfig.Config() must be the first qai_appbuilder call in the process
    (constructing a QNNContext before it is configured dereferences
    uninitialized state and crashes the process), so every model-section
    init routine below calls this instead of QNNConfig.Config() directly.
    LogLevel.ERROR: WARN logs every input tensor on every call.
    """
    global _QNN_CONFIGURED
    if not _QNN_CONFIGURED:
        QNNConfig.Config(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.BASIC)
        _QNN_CONFIGURED = True


# --- QAIRT SDK compatibility ------------------------------------------------
# A QNN context binary only loads on a QNN runtime at least as new as the
# QAIRT SDK it was compiled with. A failed load does NOT raise in
# qai_appbuilder: QNN logs "Using newer context binary on old SDK" and the
# context later returns no outputs. Both the runtime library and every AI Hub
# context binary embed their full SDK build string (e.g. v2.45.0.260326154327),
# which is compared before loading so the user gets an actionable error.
_QAIRT_VERSION_RE = re.compile(rb"v(2)\.(\d{1,3})\.(\d{1,3})\.\d{6,14}")
_QAIRT_SCAN_BYTES = 4 << 20      # the build string sits in the binary header
_runtime_qairt_cache: list = []


def _qairt_version_in(path: Path, limit: int | None = _QAIRT_SCAN_BYTES) -> tuple[int, int, int] | None:
    try:
        with open(path, "rb") as f:
            data = f.read() if limit is None else f.read(limit)
    except OSError:
        return None
    found = [tuple(int(g) for g in m.groups()) for m in _QAIRT_VERSION_RE.finditer(data)]
    return max(found) if found else None


def _runtime_qairt_version() -> tuple[int, int, int] | None:
    """QAIRT version of the QNN HTP runtime bundled with qai_appbuilder."""
    if not _runtime_qairt_cache:
        ver = None
        mod = sys.modules.get("qai_appbuilder")
        if mod is not None and getattr(mod, "__file__", None):
            ver = _qairt_version_in(Path(mod.__file__).parent / "libs" / "QnnHtp.dll", limit=None)
        _runtime_qairt_cache.append(ver)
    return _runtime_qairt_cache[0]


def _check_context_sdk(path: Path, tag: str) -> None:
    """Raise if the context binary was built with a newer QAIRT SDK than the
    installed QNN runtime (it cannot be loaded). Unknown versions pass."""
    model_ver = _qairt_version_in(path)
    rt_ver = _runtime_qairt_version()
    if model_ver is None or rt_ver is None or model_ver[:2] <= rt_ver[:2]:
        return
    fmt = lambda v: ".".join(map(str, v))
    raise RuntimeError(
        f"[{tag}] {path.name} was compiled with QAIRT {fmt(model_ver)}, but the installed "
        f"qai_appbuilder QNN runtime is QAIRT {fmt(rt_ver)}; newer context binaries cannot be "
        f"loaded by an older runtime. Upgrade qai_appbuilder (>= {model_ver[0]}.{model_ver[1]}) "
        f"or delete {path.parent} so the pinned compatible release is downloaded.")


def _check_outputs(out, expected: int, what: str) -> None:
    """Turn the silent 'context failed to load' case into a clear error."""
    n = len(out) if out is not None else 0
    if n < expected:
        raise RuntimeError(
            f"{what} returned {n} output(s), expected {expected}. The QNN context most likely "
            f"failed to load (see the QNN errors above, e.g. 'Using newer context binary on "
            f"old SDK' = model built with a newer QAIRT than the qai_appbuilder runtime).")


def _resample(wave: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Polyphase resample (fast and artefact-free for any length)."""
    if sr_in == sr_out:
        return np.asarray(wave, dtype=np.float32)
    from math import gcd
    import scipy.signal
    g = gcd(int(sr_in), int(sr_out))
    return scipy.signal.resample_poly(wave, int(sr_out) // g, int(sr_in) // g).astype(np.float32)


# ─── Generic long-text chunker (shared by OpusMT / PiperTTS-EN / MeloTTS-ZH) ───
# Every one of the three NPU models downstream of ASR has a fixed-size
# context window (OpusMT: 256 encoder tokens; PiperTTS-EN / MeloTTS-ZH:
# 512 phoneme/phone slots) and, worse, each fails *silently* or *badly* once
# the input gets long enough to approach that window from a 20-30s+ audio
# clip: OpusMT's greedy decoder can fire EOS early partway through a long
# run-on sentence (observed: 119 input tokens decoded to only 31 output
# tokens -- roughly the first third -- in one shot); PiperTTS-EN raises a
# hard ValueError once its phoneme-id count exceeds 512; MeloTTS-ZH
# silently truncates its phone sequence to 512 with no error at all. This
# single chunker is reused by all three call sites below to pre-split long
# text into small, safe pieces *before* it reaches any of them, and each
# site re-concatenates the per-chunk results (translated text, or audio
# samples) afterwards.

_CJK_PUNCT_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"
_CJK_EDGE_START = re.compile(rf"[{_CJK_PUNCT_RANGE}]")
_CJK_EDGE_END = re.compile(rf"[{_CJK_PUNCT_RANGE}]$")


def _join_pieces(a: str, b: str) -> str:
    """Join two text pieces: a space between Latin words, nothing next to CJK."""
    if not a:
        return b
    if not b:
        return a
    if _CJK_EDGE_START.match(b) or _CJK_EDGE_END.search(a):
        return a + b
    return a + " " + b


def _split_text_for_model(text: str, measure_fn, max_measure: int) -> list[str]:
    """Split `text` into pieces where measure_fn(piece) <= max_measure.

    Prefers splitting at sentence boundaries (. ! ? 。！？), then at clause
    boundaries (, ， ; ；), then falls back to a greedy word-wrap, and
    finally to a hard character-wrap for a single run with no spaces to
    split on (e.g. unsegmented CJK text or ASR output with no punctuation
    at all). Adjacent small pieces are merged back together up to the
    budget afterwards, so a long punctuation-free ASR transcript still ends
    up as a handful of similarly-sized chunks instead of one per word.

    An ASCII '.', ',' or ';' only counts as a boundary when followed by
    whitespace, so numbers such as "3.14" or "1,000" are never cut apart.
    """
    text = (text or "").strip()
    if not text:
        return []
    if measure_fn(text) <= max_measure:
        return [text]

    def _char_wrap(s: str) -> list[str]:
        # No spaces to split on (e.g. a long CJK run or one huge token) --
        # hard char-wrap, preferring a CJK conjunction in the 2nd half.
        out = []
        breakers = ("然后", "但是", "可是", "不过", "因为", "所以", "如果", "而且", "并且",
                    "同时", "以及", "或者", "还有", "以便", "为了", "虽然", "但", "和", "并")
        rest = s
        while rest:
            if measure_fn(rest) <= max_measure:
                out.append(rest)
                break
            limit = len(rest)
            while limit > 1 and measure_fn(rest[:limit]) > max_measure:
                limit -= 1
            cut = None
            for i in range(limit, max(limit // 2, 3), -1):   # latest good break in the 2nd half
                if any(rest.startswith(b_, i) for b_ in breakers):
                    cut = i
                    break
            if cut is None:
                cut = limit
            out.append(rest[:cut])
            rest = rest[cut:]
        return out

    def _hard_wrap(s: str) -> list[str]:
        words = s.split()
        if len(words) <= 1:
            return _char_wrap(s.strip())
        out, buf = [], ""
        for w in words:
            if measure_fn(w) > max_measure:
                # A single word that is itself over budget (e.g. a CJK run
                # inside mixed-language text): flush, then char-wrap it so
                # no oversized chunk is ever returned.
                if buf:
                    out.append(buf)
                    buf = ""
                out.extend(_char_wrap(w))
                continue
            cand = _join_pieces(buf, w) if buf else w
            if measure_fn(cand) > max_measure and buf:
                out.append(buf)
                buf = w
            else:
                buf = cand
        if buf:
            out.append(buf)
        return out

    sentence_pat = r"(?<=[!?\u3002\uff01\uff1f])\s*|(?<=\.)\s+"
    sentences = [s for s in re.split(sentence_pat, text) if s.strip()]

    pieces = []
    for sent in sentences:
        if measure_fn(sent) <= max_measure:
            pieces.append(sent.strip())
            continue
        sub_pat = r"(?<=[\uff0c\uff1b])\s*|(?<=[,;])\s+"
        subs = [s for s in re.split(sub_pat, sent) if s.strip()]
        for sub in subs:
            if measure_fn(sub) <= max_measure:
                pieces.append(sub.strip())
            else:
                pieces.extend(_hard_wrap(sub))

    merged = []
    buf = ""
    for p in pieces:
        cand = _join_pieces(buf, p) if buf else p
        if measure_fn(cand) <= max_measure:
            buf = cand
        else:
            if buf:
                merged.append(buf)
            buf = p
    if buf:
        merged.append(buf)
    return merged


# ═════════════════════════════════════════════════════════════════════════
# SECTION 1 -- Zipformer streaming ASR (Mandarin + English mixed)
# Ported from samples/models/audio/speech_recognition/zipformer/python/zipformer.py
# Silence-detection VAD chunking (ffmpeg silencedetect) is on by default for
# any input backed by a real audio file -- long audio is split at natural
# pauses and each segment decoded on its own fresh encoder state, since the
# streaming encoder's state does not survive a long silence intact (see
# zf_transcribe()'s docstring for the concrete failure this fixes).
# ═════════════════════════════════════════════════════════════════════════

_ZF_MODEL_URLS = {
    # AI Hub context binaries (.bin only); X Elite = 8380, X2 Elite = 8480.
    # Pinned to v0.55.0 'float' (QAIRT 2.45): the newer v0.65.0 releases do
    # NOT work with this pipeline on the QAIRT 2.51 runtime -- the 'mixed'
    # joiner clamps every logit to >= 0 (many come back all-zero, so the
    # search emits garbage like "廪廪廪..." or nothing), and the v0.65.0
    # 'float' build decodes the sample clip to a single character.
    _DEV_X: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/zipformer/releases/v0.55.0/"
        "zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite.zip"
    ),
    _DEV_X2: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/zipformer/releases/v0.55.0/"
        "zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite.zip"
    ),
}
# Pinned to the release tag that matches the model zip (not the moving `main`).
_ZF_TOKENS_URL = (
    "https://raw.githubusercontent.com/qualcomm/ai-hub-models/v0.55.0/"
    "src/qai_hub_models/models/zipformer/tokens.txt"
)
_ZF_RELEASE = "v0.55.0"
_ZF_PRECISION = "float"        # metadata.json "precision" of the validated build
_ZF_QAIRT = "2.45."            # metadata.json tool_versions.qairt prefix of that build
_ZF_REQUIRED_MODELS = ["encoder.bin", "decoder.bin", "joiner.bin"]
_ZF_ENCODER_FILENAME = "encoder.bin"
_ZF_DECODER_FILENAME = "decoder.bin"
_ZF_JOINER_FILENAME = "joiner.bin"
_ZF_TOKENS_FILENAME = "tokens.txt"

ZF_SAMPLE_RATE = 16000
_ZF_N_MELS = 80
_ZF_HIGH_FREQ = -400.0

_ZF_DECODE_CHUNK_SIZE = 32
_ZF_OFFSET = _ZF_DECODE_CHUNK_SIZE * 2       # 64 frames -- chunk stride
_ZF_SEGMENT = _ZF_OFFSET + 7                 # 71 frames -- encoder input window

_ZF_BLANK_ID = 0
_ZF_CONTEXT_SIZE = 2                         # decoder consumes the last 2 tokens

# The encoder cache (5 layers x 7 tensors = 35) is built from the tensor
# names/shapes reported by the context binary itself -- see _ZFEncoder.

_zf_state: dict[str, Any] = {"loaded": False}


def _zf_flatten(model_dir: Path, work_dir: Path) -> Path:
    """Move every file of the extracted release folder `model_dir` up into
    `work_dir` and delete the (now empty) release folder. Returns work_dir."""
    print(f"[Zipformer] Moving model files from {model_dir.name}/ to {work_dir}")
    for item in model_dir.iterdir():
        if not item.is_file():
            continue
        target = work_dir / item.name
        if target.exists():
            target.unlink()
        shutil.move(str(item), str(target))
    top = work_dir / model_dir.resolve().relative_to(work_dir.resolve()).parts[0]
    shutil.rmtree(top, ignore_errors=True)
    if _find_dir_with(work_dir, _ZF_REQUIRED_MODELS) != work_dir:
        raise FileNotFoundError(f"Zipformer model files {_ZF_REQUIRED_MODELS} missing in {work_dir}.")
    return work_dir


def _zf_metadata_is_validated(work_dir: Path) -> bool:
    """True unless a metadata.json under work_dir positively identifies a
    build other than the validated _ZF_RELEASE one (different precision or
    QAIRT version). No / unreadable metadata -> True (cannot tell)."""
    for meta_path in work_dir.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        precision = str(meta.get("precision", _ZF_PRECISION)).lower()
        qairt = str((meta.get("tool_versions") or {}).get("qairt", _ZF_QAIRT))
        return precision == _ZF_PRECISION and qairt.startswith(_ZF_QAIRT)
    return True


def _zf_download(work_dir: Path):
    """Detect the chip, download + extract the AI Hub ZIP (.bin files only),
    and fetch tokens.txt. Returns (model_dir, tokens_path)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    # v0.55.0 'float' is the validated release (see _ZF_MODEL_URLS). Releases
    # differ in encoder I/O tensor order, which _ZFEncoder handles by name.
    # The release is part of the model URL, so _ensure_model's stamp check
    # replaces binaries from any other release (e.g. a v0.65.0 'mixed' install).
    # Older versions of this script wrote a plain-text ".model_release" stamp:
    # honour it once (a different release must be replaced), then drop it.
    # An install with no stamp at all (e.g. a manual one) is adopted, never deleted.
    legacy = work_dir / ".model_release"
    release_ok = True
    if legacy.is_file():
        try:
            release_ok = legacy.read_text(encoding="utf-8").strip() == _ZF_RELEASE
        except OSError:
            pass
        if not release_ok:
            print(f"[Zipformer] Installed models are not from release {_ZF_RELEASE} -- replacing them.")
    elif not (work_dir / _STAMP_NAME).is_file() and not _zf_metadata_is_validated(work_dir):
        # Unstamped install (manual copy / older script) of a known-bad build,
        # e.g. the v0.65.0 'mixed' binaries: replace instead of adopting it.
        print(f"[Zipformer] Installed models are not the validated {_ZF_RELEASE} "
              f"'{_ZF_PRECISION}' build -- replacing them.")
        release_ok = False
    model_dir = _ensure_model(work_dir, _ZF_REQUIRED_MODELS, _ZF_MODEL_URLS,
                              "Zipformer", "Zipformer", release_ok=release_ok)
    legacy.unlink(missing_ok=True)

    # Keep the .bin / .json files directly in work_dir (next to tokens.txt)
    # instead of the zip's <release folder>/ sub-directory. Migrates installs
    # made by older versions of this script (new installs are already flat).
    if model_dir.resolve() != work_dir.resolve():
        model_dir = _zf_flatten(model_dir, work_dir)

    tokens_path = model_dir / _ZF_TOKENS_FILENAME
    if not tokens_path.is_file():
        tokens_path = work_dir / _ZF_TOKENS_FILENAME
    if not tokens_path.is_file():
        print(f"[Zipformer] Downloading tokens.txt: {_ZF_TOKENS_URL}")
        ret = download_url(
            _ZF_TOKENS_URL, str(tokens_path),
            desc="Downloading Zipformer tokens.txt (vocabulary)...",
            fail=f"\nFailed to download tokens.txt from:\n  {_ZF_TOKENS_URL}\n"
                 f"Please download manually and save to: {tokens_path}",
        )
        if not ret or not tokens_path.is_file():
            # Without the vocabulary the ASR output would be a string of token
            # IDs, which the translator would then translate as garbage.
            raise RuntimeError(f"Zipformer tokens.txt is required but could not be downloaded: {tokens_path}")

    return model_dir, tokens_path


def _zf_np_dtype(qnn_dtype) -> type:
    """numpy dtype for a QNN tensor data-type string ('int32', 'float32', ...)."""
    t = str(qnn_dtype).lower()
    if "int64" in t:
        return np.int64
    if "int" in t:
        return np.int32
    if "16" in t:
        return np.float16
    return np.float32


class _ZFEncoder(QNNContext):
    """Streaming encoder: features x (1,71,80) + 35 cache tensors -> encoder_out
    (1,16,512) + 35 new cache tensors.

    The I/O ORDER differs between AI Hub releases (v0.55.0 'float': x first,
    caches interleaved per layer, encoder_out last; v0.65.0 'mixed': caches
    grouped by kind, x LAST, encoder_out at output index 5), so every tensor
    is addressed by NAME as reported by the context binary: input
    'cached_<k>_<layer>' is fed from output 'new_cached_<k>_<layer>' of the
    previous chunk.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        in_names = list(self.getInputName())
        in_shapes = [list(s) for s in self.getInputShapes()]
        in_types = list(self.getInputDataType())
        out_names = list(self.getOutputName())
        out_shapes = [list(s) for s in self.getOutputShapes()]
        if "x" not in in_names or "encoder_out" not in out_names:
            raise RuntimeError(
                f"[Zipformer] unexpected encoder I/O (inputs={in_names}, outputs={out_names})")
        cache_in = [n for n in in_names if n != "x"]
        missing = [n for n in cache_in if "new_" + n not in out_names]
        if missing:
            raise RuntimeError(f"[Zipformer] encoder outputs lack new_* for caches {missing}")
        self.in_names = in_names
        self.out_names = out_names
        self.out_shapes = out_shapes
        self.x_shape = in_shapes[in_names.index("x")]                 # [1, 71, 80]
        # name -> (shape, numpy dtype) of every cache input
        self.cache_spec = {n: (s, _zf_np_dtype(t))
                           for n, s, t in zip(in_names, in_shapes, in_types) if n != "x"}

    def init_state(self) -> dict:
        """Zero-initialized encoder cache, keyed by input tensor name."""
        return {n: np.zeros(s, dtype=dt) for n, (s, dt) in self.cache_spec.items()}

    def Inference(self, chunk, state: dict):
        """Run one chunk. Returns (encoder_out (1,16,512) float32, new_state dict)."""
        x = np.ascontiguousarray(chunk, dtype=np.float32)
        inputs = [x if n == "x" else np.ascontiguousarray(state[n]) for n in self.in_names]
        outputs = super().Inference(inputs)
        if len(outputs) != len(self.out_names):
            raise RuntimeError(f"unexpected Zipformer encoder output count: {len(outputs)} "
                               f"(expected {len(self.out_names)}) -- context binary failed to run?")
        by_name = {n: np.asarray(o).reshape(s)
                   for n, s, o in zip(self.out_names, self.out_shapes, outputs)}
        encoder_out = by_name["encoder_out"].astype(np.float32, copy=False)
        new_state = {n: by_name["new_" + n].astype(dt, copy=False)
                     for n, (_, dt) in self.cache_spec.items()}
        return encoder_out, new_state


class _ZFDecoder(QNNContext):
    """y (1,2) int32 -> decoder_out (1,512) float32."""

    def Inference(self, y):
        out = super().Inference([np.ascontiguousarray(y, dtype=np.int32)])
        return np.array(out[0], dtype=np.float32).reshape(1, 512)


class _ZFJoiner(QNNContext):
    """encoder_out (1,512) + decoder_out (1,512) -> logit (1,6254) float32."""

    def Inference(self, enc_frame, dec_out):
        out = super().Inference([
            np.ascontiguousarray(enc_frame, dtype=np.float32),
            np.ascontiguousarray(dec_out, dtype=np.float32),
        ])
        return np.array(out[0], dtype=np.float32).reshape(-1)


def _zf_compute_fbank(waveform: np.ndarray, sr: int) -> np.ndarray:
    """80-channel kaldi-style log-mel fbank, shape (T, 80) float32.
    Prefers kaldi_native_fbank; falls back to a pure-numpy STFT."""
    if sr != ZF_SAMPLE_RATE:
        waveform = _resample(waveform, sr, ZF_SAMPLE_RATE)
        sr = ZF_SAMPLE_RATE

    try:
        import kaldi_native_fbank as knf
    except ImportError:
        return _zf_compute_fbank_numpy(waveform, sr)

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq = sr
    opts.mel_opts.num_bins = _ZF_N_MELS
    opts.mel_opts.high_freq = _ZF_HIGH_FREQ

    extractor = knf.OnlineFbank(opts)
    extractor.accept_waveform(sampling_rate=sr, waveform=waveform.astype(np.float32, copy=False))
    extractor.input_finished()   # flush trailing frames (snip_edges=False)
    n_ready = extractor.num_frames_ready
    if n_ready <= 0:
        return np.zeros((0, _ZF_N_MELS), dtype=np.float32)
    frames = np.empty((n_ready, _ZF_N_MELS), dtype=np.float32)
    for i in range(n_ready):
        frames[i] = extractor.get_frame(i)
    return frames


def _zf_compute_fbank_numpy(waveform: np.ndarray, sr: int) -> np.ndarray:
    """Pure-numpy re-implementation of Kaldi fbank (used only when
    kaldi_native_fbank is missing). Mirrors knf's settings used above:
    25ms/10ms frames, snip_edges=False, dither=0, remove_dc_offset,
    pre-emphasis 0.97, povey window, 80 mel bins (20 Hz .. nyquist-400),
    natural-log power mel energies, and -- importantly -- NO per-utterance
    mean normalisation (the Zipformer model was trained without it; doing so
    degrades recognition and drops words)."""
    wav = np.asarray(waveform, dtype=np.float32)
    n_mels, low, high = _ZF_N_MELS, 20.0, _ZF_HIGH_FREQ
    win_len = int(round(sr * 0.025))
    shift = int(round(sr * 0.010))
    n_fft = 1
    while n_fft < win_len:
        n_fft *= 2
    n = len(wav)
    n_frames = (n + shift // 2) // shift          # snip_edges=False
    if n_frames <= 0:
        return np.zeros((0, n_mels), dtype=np.float32)

    starts = np.arange(n_frames) * shift + shift // 2 - win_len // 2
    idx = starts[:, None] + np.arange(win_len)[None, :]
    idx = np.where(idx < 0, -idx - 1, idx)                 # reflect at start
    idx = np.where(idx >= n, 2 * n - 1 - idx, idx)         # reflect at end
    idx = np.clip(idx, 0, n - 1)
    fr = wav[idx].astype(np.float64)

    fr -= fr.mean(axis=1, keepdims=True)                   # remove DC offset
    pre = np.empty_like(fr)
    pre[:, 1:] = fr[:, 1:] - 0.97 * fr[:, :-1]             # pre-emphasis
    pre[:, 0] = fr[:, 0] - 0.97 * fr[:, 0]
    window = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(win_len) / (win_len - 1)), 0.85)
    fr = np.pad(pre * window, ((0, 0), (0, n_fft - win_len)))
    power = np.abs(np.fft.rfft(fr, n=n_fft, axis=1)) ** 2

    def mel(f):
        return 1127.0 * np.log(1.0 + f / 700.0)

    nyq = sr / 2.0
    high_hz = high if high > 0 else nyq + high
    mlo, mhi = mel(low), mel(high_hz)
    delta = (mhi - mlo) / (n_mels + 1)
    n_bins = n_fft // 2
    fm = mel(np.arange(n_bins) * (sr / n_fft))
    fb = np.zeros((n_mels, n_bins + 1))
    for b in range(n_mels):
        left = mlo + b * delta
        center = left + delta
        right = center + delta
        fb[b, :n_bins] = np.maximum(
            0.0, np.minimum((fm - left) / (center - left), (right - fm) / (right - center)))
    log_mel = np.log(np.maximum(power @ fb.T, np.finfo(np.float32).eps))
    return log_mel.astype(np.float32)


def _zf_load_tokens(path: Path):
    """Load sherpa-onnx tokens.txt ('<token> <id>' per line). Returns id->token list."""
    if not path.is_file():
        print(f"[Zipformer][WARN] tokens.txt not found at {path} -- will show token IDs")
        return None
    id_to_token = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                id_to_token[int(parts[1])] = parts[0]
    if not id_to_token:
        return None
    size = max(id_to_token) + 1
    return [id_to_token.get(i, "") for i in range(size)]


def _zf_decode_tokens(token_ids, vocab) -> str:
    if vocab is None:
        return " ".join(str(t) for t in token_ids)
    pieces = []
    for tid in token_ids:
        tok = vocab[tid] if 0 <= tid < len(vocab) else ""
        if len(tok) >= 2 and tok.startswith("<") and tok.endswith(">"):
            continue   # suppress <blk> / <sos/eos> / <unk> specials
        pieces.append(tok)
    text = "".join(pieces).replace("\u2581", " ")   # SentencePiece word-start
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


# ─── Silence-detection VAD helpers (on by default) ───────────────────────────
# Ported from zipformer.py / qaiappbuilder_speech_recognition_windows_py's
# speech_recognition.py. Uses ffmpeg's `silencedetect` filter to skip silent
# regions and split long audio at natural pauses before it is fed to the
# streaming decoder -- each speech segment is decoded on its own encoder
# state so a pause does not leak stale context into the next segment (that
# earlier omission is what dropped "...AMERICANS, ASK NOT WHAT YOUR COUNTRY
# CAN DO FOR YOU," from the JFK sample: a long silence between clauses made
# the single-pass streaming decoder lose sync). Falls back silently to
# decoding the whole waveform in one pass if ffmpeg is unavailable or fails.

def _vad_parse_silencedetect(text):
    """Parse the stderr of ``ffmpeg -af silencedetect`` into (start, end) tuples."""
    silences = []
    current_start = None
    num = r"(-?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"   # ffmpeg prints %g, e.g. 1e-05
    for line in text.splitlines():
        m = re.search(r"silence_start:\s*" + num, line)
        if m:
            current_start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*" + num, line)
        if m and current_start is not None:
            silences.append((current_start, float(m.group(1))))
            current_start = None
    if current_start is not None:
        # Audio ends inside a silence and ffmpeg printed no silence_end:
        # treat the rest as silence (clamped to the duration by the caller).
        silences.append((current_start, float("inf")))
    return silences


def _vad_invert_silences(silences, duration):
    if duration <= 0:
        return []
    if not silences:
        return [(0.0, duration)]
    segments = []
    cursor = 0.0
    for s, e in sorted(silences):
        s = max(0.0, min(duration, s))
        e = max(0.0, min(duration, e))
        if s > cursor:
            segments.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        segments.append((cursor, duration))
    return segments


def _vad_merge_segments(segments, max_gap):
    if not segments:
        return []
    merged = [segments[0]]
    for s, e in segments[1:]:
        ps, pe = merged[-1]
        if s - pe <= max_gap:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _vad_split_long_segments(segments, max_segment, overlap):
    """Split any speech segment longer than max_segment seconds into
    overlapping windows (with overlap seconds of overlap between adjacent
    windows)."""
    result = []
    for s, e in segments:
        if e - s <= max_segment:
            result.append((s, e))
            continue
        cursor = s
        while cursor < e:
            chunk_end = min(e, cursor + max_segment)
            result.append((cursor, chunk_end))
            if chunk_end >= e:
                break
            cursor = max(cursor + 0.1, chunk_end - overlap)
    return result


def _vad_detect_speech_segments(audio_path, duration,
                                noise_db=-35.0, min_silence=0.6,
                                overlap=0.3, min_speech=0.25,
                                max_segment=25.0):
    """Run ``ffmpeg silencedetect`` on the file at audio_path and return a
    list of (start, end) speech segments in seconds. Returns an empty list
    on any ffmpeg failure -- the caller falls back to the full waveform."""
    import subprocess
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats",
                "-i", str(audio_path),
                "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
                "-f", "null", "-",
            ],
            check=True, timeout=300,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Zipformer][WARN] VAD ffmpeg failed ({type(e).__name__}: {e}) -- "
              f"falling back to full-waveform decode.")
        return []

    silences = _vad_parse_silencedetect(completed.stderr)
    raw = _vad_invert_silences(silences, duration)

    expanded = []
    for s, e in raw:
        if e - s < min_speech:
            continue
        expanded.append((max(0.0, s - overlap), min(duration, e + overlap)))

    merged = _vad_merge_segments(expanded, max_gap=overlap * 2)
    return _vad_split_long_segments(merged, max_segment=max_segment, overlap=overlap)


def _zf_chunk_waveform_by_vad(audio_path, waveform, sample_rate,
                              noise_db=-35.0, min_silence=0.6, overlap=0.3,
                              min_speech=0.25, max_segment=25.0):
    """Return a list of (start_time_s, waveform_slice) at ZF_SAMPLE_RATE using
    ffmpeg's silencedetect. On any failure (or when no speech regions are
    found) returns [(0.0, waveform)] so the caller decodes the whole
    waveform in one pass. audio_path may be None (e.g. mic recording with
    no backing file) -- VAD is skipped in that case since ffmpeg needs a
    real file to analyze."""
    if sample_rate != ZF_SAMPLE_RATE:
        waveform = _resample(waveform, sample_rate, ZF_SAMPLE_RATE)
        sample_rate = ZF_SAMPLE_RATE

    if not audio_path:
        return [(0.0, waveform)]

    duration = len(waveform) / float(sample_rate)
    speech = _vad_detect_speech_segments(
        audio_path, duration,
        noise_db=noise_db, min_silence=min_silence, overlap=overlap,
        min_speech=min_speech, max_segment=max_segment,
    )
    if not speech:
        print("[Zipformer] VAD produced no segments -- falling back to full-waveform decode.")
        return [(0.0, waveform)]

    chunks = []
    for s, e in speech:
        i0 = int(round(s * sample_rate))
        i1 = int(round(e * sample_rate))
        if i1 - i0 >= int(0.1 * sample_rate):
            chunks.append((s, waveform[i0:i1].astype(np.float32, copy=False)))
    if not chunks:
        return [(0.0, waveform)]
    print(f"[Zipformer] VAD produced {len(chunks)} speech segment(s) (silent regions skipped).")
    return chunks


def _zf_log_softmax(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float64, copy=False)
    m = np.max(x)
    return x - (m + np.log(np.sum(np.exp(x - m))))


def _zf_encoder_frames(encoder, features: np.ndarray):
    """Run the chunked streaming encoder over (T, 80) features and yield
    one (512,) encoder frame at a time."""
    T = features.shape[0]
    feats = features[np.newaxis, ...].astype(np.float32, copy=False)   # (1, T, 80)
    state = encoder.init_state()
    for start in range(0, T, _ZF_OFFSET):
        chunk = feats[:, start:start + _ZF_SEGMENT, :]
        if chunk.shape[1] < _ZF_SEGMENT:
            chunk = np.pad(chunk, ((0, 0), (0, _ZF_SEGMENT - chunk.shape[1]), (0, 0)),
                           mode="constant", constant_values=0.0)
        chunk = np.ascontiguousarray(chunk, dtype=np.float32)
        encoder_out, state = encoder.Inference(chunk, state)
        if encoder_out.ndim == 3:
            encoder_out = encoder_out[0]
        for t in range(encoder_out.shape[0]):
            yield encoder_out[t:t + 1]


def _zf_streaming_decode(encoder, decoder, joiner, features: np.ndarray,
                         beam: int = 4, token_mask: np.ndarray | None = None,
                         return_nbest: bool = False):
    """Streaming Zipformer transducer decode. features: (T, 80).

    beam == 1 is plain frame-wise greedy search. beam > 1 is sherpa-onnx's
    "modified_beam_search" (at most one symbol per frame, hypotheses merged
    by token sequence), which recovers from a locally wrong top-1 token that
    greedy search can never revisit.

    token_mask: optional bool array over the vocabulary; True entries are
    forbidden (used to keep Chinese characters out of an English transcript:
    the model is bilingual and otherwise turns an unclear "THIS IS AN" into a
    stray "事").
    """
    if features.shape[0] == 0:
        return ([], []) if return_nbest else []

    ctx = _ZF_CONTEXT_SIZE
    dec_cache: dict[tuple, np.ndarray] = {}

    def dec_out(key):
        out = dec_cache.get(key)
        if out is None:
            out = decoder.Inference(np.array([list(key)], dtype=np.int32))
            dec_cache[key] = out
        return out

    def frame_logp(enc_frame, key):
        logits = joiner.Inference(enc_frame, dec_out(key)).astype(np.float64)
        if token_mask is not None and token_mask.shape[0] == logits.shape[0]:
            logits = np.where(token_mask, -1e9, logits)
        return _zf_log_softmax(logits)

    start = (_ZF_BLANK_ID,) * ctx
    # hyps: token-tuple (including the leading context) -> log score
    hyps: dict[tuple, float] = {start: 0.0}

    for enc_frame in _zf_encoder_frames(encoder, features):
        cand: dict[tuple, float] = {}

        def add(k, sc):
            old = cand.get(k)
            cand[k] = sc if old is None else float(np.logaddexp(old, sc))

        for toks, score in hyps.items():
            lp = frame_logp(enc_frame, toks[-ctx:])
            add(toks, score + lp[_ZF_BLANK_ID])                       # emit nothing
            if beam == 1:
                tid = int(np.argmax(lp))
                if tid != _ZF_BLANK_ID:
                    add(toks + (tid,), score + lp[tid])
                    cand.pop(toks, None)
                continue
            lp_nb = lp.copy()
            lp_nb[_ZF_BLANK_ID] = -np.inf
            top = np.argpartition(-lp_nb, beam)[:beam]
            for tid in top:
                if lp_nb[tid] > -1e8:
                    add(toks + (int(tid),), score + lp_nb[tid])
        # prune
        if len(cand) > beam:
            keep = sorted(cand.items(), key=lambda kv: kv[1], reverse=True)[:beam]
            hyps = dict(keep)
        else:
            hyps = cand

    ranked = sorted(hyps.items(), key=lambda kv: kv[1], reverse=True)
    nbest = [(list(t[ctx:]), sc) for t, sc in ranked]
    best = nbest[0][0] if nbest else []
    return (best, nbest) if return_nbest else best


_ZF_BEAM = 4


def _zf_language_mask(vocab, language: str | None):
    """Bool mask over the vocabulary of tokens to forbid for `language`.
    'en' forbids every token containing a CJK character; None/'zh' forbids
    nothing (Mandarin speech legitimately contains English words too)."""
    if language != "en" or not vocab:
        return None
    cjk = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
    return np.array([bool(cjk.search(t)) for t in vocab], dtype=bool)


def zf_init():
    """Download (if needed) and load the Zipformer encoder/decoder/joiner."""
    if _zf_state.get("loaded"):
        return
    print("[Zipformer] Checking / downloading models ...")
    model_dir, tokens_path = _zf_download(_ZIPFORMER_DIR)
    vocab = _zf_load_tokens(tokens_path)

    _ensure_qnn_config()
    t0 = time.perf_counter()
    encoder = _ZFEncoder("zipformer_encoder", str(model_dir / _ZF_ENCODER_FILENAME),
                        input_data_type="native", output_data_type="native")
    decoder = _ZFDecoder("zipformer_decoder", str(model_dir / _ZF_DECODER_FILENAME),
                        input_data_type="native", output_data_type="native")
    joiner = _ZFJoiner("zipformer_joiner", str(model_dir / _ZF_JOINER_FILENAME),
                       input_data_type="native", output_data_type="native")
    print(f"[Zipformer] Loaded on HTP in {time.perf_counter() - t0:.2f}s "
          f"(vocab={len(vocab) if vocab else 'N/A'})")

    _zf_state.update(encoder=encoder, decoder=decoder, joiner=joiner, vocab=vocab, loaded=True)


def zf_transcribe(waveform: np.ndarray, sr: int, audio_path: str | None = None,
                  language: str | None = None, beam: int = _ZF_BEAM) -> str:
    """Transcribe a mono float32 waveform (any sample rate) to text.

    When ``audio_path`` is given (a real WAV file on disk, as opposed to an
    in-memory mic recording), the audio is first split into speech segments
    at natural pauses using ffmpeg's ``silencedetect`` (VAD), and each
    segment is decoded on its own fresh streaming-encoder state. This
    matters for anything longer than a short phrase: Zipformer's streaming
    encoder carries state across its whole 64-frame-stride chunk loop, so a
    long silence in the middle of a single-pass decode makes it lose sync
    and silently drop everything after the pause (observed on the JFK
    sample: "...AMERICANS, ASK NOT WHAT YOUR COUNTRY CAN DO FOR YOU," was
    dropped entirely). Falls back to a single full-waveform pass when
    ``audio_path`` is None (e.g. a live mic recording) or when ffmpeg is
    unavailable/fails.
    """
    zf_init()
    encoder = _zf_state["encoder"]
    decoder = _zf_state["decoder"]
    joiner = _zf_state["joiner"]
    vocab = _zf_state["vocab"]

    waveform = np.asarray(waveform, dtype=np.float32)
    energy = float(np.sqrt(np.mean(waveform ** 2))) if waveform.size else 0.0
    if energy < 1e-3:
        print("[Zipformer][WARN] audio looks like silence -- empty result is expected")

    segments = _zf_chunk_waveform_by_vad(audio_path, waveform, sr)
    token_mask = _zf_language_mask(vocab, language)

    texts = []
    last_text_end = None   # end time of the segment that produced texts[-1]
    with _HtpBurst():
        for i, (start_s, seg_waveform) in enumerate(segments):
            features = _zf_compute_fbank(seg_waveform, ZF_SAMPLE_RATE)
            ids = _zf_streaming_decode(encoder, decoder, joiner, features,
                                       beam=beam, token_mask=token_mask)
            seg_text = _zf_decode_tokens(ids, vocab)
            seg_end = start_s + len(seg_waveform) / float(ZF_SAMPLE_RATE)
            if seg_text and texts and last_text_end is not None and start_s < last_text_end:
                # Windows of a split long segment overlap in time: drop the
                # words/characters repeated at the join. Only compared with
                # text that really overlaps this window in time.
                seg_text = _merge_overlap_text(texts[-1], seg_text)
            if len(segments) > 1:
                print(f"[Zipformer]   [seg {i+1}/{len(segments)}  t={start_s:6.2f}s]  {seg_text}")
            if seg_text:
                texts.append(seg_text)
                last_text_end = seg_end

    return " ".join(texts).strip()


def _merge_overlap_text(prev: str, cur: str) -> str:
    """Remove from `cur` a leading run that repeats the tail of `prev`
    (word level for spaced text, character level otherwise).

    Word level needs >= 2 matching words; a single-word match is only
    accepted for a word of >= 4 letters, so a coincidental short repeat
    ("... I" / "I think ...", "the" / "the") is not deleted."""
    if not prev or not cur:
        return cur
    if " " in prev or " " in cur:
        a, b, join, min_k, max_k = prev.split(), cur.split(), " ", 1, 6
    else:
        a, b, join, min_k, max_k = list(prev), list(cur), "", 2, 8
    for k in range(min(max_k, len(a), len(b)), min_k - 1, -1):
        if [t.lower() for t in a[-k:]] == [t.lower() for t in b[:k]]:
            if join and k == 1 and len(b[0].strip(".,!?;:'\"")) < 4:
                break
            return join.join(b[k:])
    return cur


def zf_release():
    if not _zf_state.get("loaded"):
        return
    for key in ("encoder", "decoder", "joiner"):
        obj = _zf_state.pop(key, None)
        if obj is not None:
            del obj
    _zf_state["loaded"] = False


# ═════════════════════════════════════════════════════════════════════════
# SECTION 2 -- OpusMT bidirectional translation (zh<->en)
# Ported from samples/models/multifunctional_assistant/multifunctional_assistant4.py
# (itself ported from the standalone opus_mt_zh_en.py / opus_mt_en_zh.py samples).
# Both directions share one generic implementation parameterized by
# `direction` instead of duplicating near-identical code.
# ═════════════════════════════════════════════════════════════════════════

_OPUS_MAX_SEQ_LEN = 256   # encoder fixed input length
_OPUS_MAX_GEN_LEN = 256   # max decoder steps
_OPUS_NUM_LAYERS = 6
_OPUS_NUM_HEADS = 8
_OPUS_HEAD_DIM = 64

_OPUS_CONFIG = {
    "zh2en": {
        "model_name": "opus_mt_zh_en",
        "help_url": "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/multimodal/Text_Generation/opus_mt_zh_en/README.md",
        # AI Hub context binaries (.bin only); X Elite = 8380, X2 Elite = 8480
        "model_urls": {
            _DEV_X: "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/models/opus_mt_zh_en/releases/v0.62.1/opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x_elite.zip",
            _DEV_X2: "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/models/opus_mt_zh_en/releases/v0.62.1/opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x2_elite.zip",
        },
        "hf_model_id": "Helsinki-NLP/opus-mt-zh-en",
        "hf_revision": "cf109095479db38d6df799875e34039d4938aaa6",
        "model_dir": _OPUS_ZH_EN_DIR,
    },
    "en2zh": {
        "model_name": "opus_mt_en_zh",
        "help_url": "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/multimodal/Text_Generation/opus_mt_en_zh/README.md",
        "model_urls": {
            _DEV_X: "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/models/opus_mt_en_zh/releases/v0.62.1/opus_mt_en_zh-voice_ai-float-qualcomm_snapdragon_x_elite.zip",
            _DEV_X2: "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/models/opus_mt_en_zh/releases/v0.62.1/opus_mt_en_zh-voice_ai-float-qualcomm_snapdragon_x2_elite.zip",
        },
        "hf_model_id": "Helsinki-NLP/opus-mt-en-zh",
        "hf_revision": "408d9bc410a388e1d9aef112a2daba955b945255",
        "model_dir": _OPUS_EN_ZH_DIR,
    },
}

_opus_state: dict[str, dict[str, Any]] = {"zh2en": {}, "en2zh": {}}


class _TextGenQNNContext(QNNContext):
    """QNN context taking N positional inputs; a single output is unwrapped."""

    def Inference(self, *input_data):
        output_data = super().Inference(list(input_data))
        return output_data[0] if len(output_data) == 1 else output_data


class _OpusMTEncoder(_TextGenQNNContext):
    pass


class _OpusMTDecoder(_TextGenQNNContext):
    pass


def _opus_download_model(direction: str) -> Path:
    """Download and extract the encoder/decoder zip (.bin files) for the
    detected chip. All extracted files are flattened directly into model_dir.
    Binaries built for another chip / release, or left by an interrupted
    install, are replaced (see _ensure_model)."""
    cfg = _OPUS_CONFIG[direction]
    model_dir: Path = cfg["model_dir"]
    try:
        model_dir = _ensure_model(model_dir, ["encoder.bin", "decoder.bin"], cfg["model_urls"],
                                  f"OpusMT:{direction}", cfg["model_name"])
    except RuntimeError as e:
        raise RuntimeError(f"{e}\nSee: {cfg['help_url']}") from e
    print(f"[OpusMT:{direction}] Model ready: {model_dir}")
    return model_dir


def _opus_get_marian_tokenizer(cache_dir: Path, hf_model_id: str, revision: str | None = None):
    """Load MarianTokenizer from local HuggingFace cache or download it
    (pinned to `revision` when given)."""
    os.environ["TRANSFORMERS_VERBOSITY"] = "error"
    from transformers import MarianTokenizer

    def _find_snapshot(base_dir: Path):
        if (base_dir / "tokenizer_config.json").is_file():
            return base_dir
        org, name = hf_model_id.split("/")
        snapshots_root = base_dir / f"models--{org}--{name}" / "snapshots"
        if snapshots_root.is_dir():
            for snap in snapshots_root.iterdir():
                if (snap / "tokenizer_config.json").is_file():
                    return snap
        return None

    snap_dir = _find_snapshot(cache_dir) if cache_dir.exists() else None
    if snap_dir:
        print(f"[OpusMT]       Loading tokenizer from: {snap_dir}")
        return MarianTokenizer.from_pretrained(str(snap_dir))

    print("[OpusMT]       Tokenizer not found locally, downloading from HuggingFace...")
    cache_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"cache_dir": str(cache_dir)}
    if revision:
        kwargs["revision"] = revision
    return MarianTokenizer.from_pretrained(hf_model_id, **kwargs)


def opus_init(direction: str) -> None:
    """Initialize the encoder/decoder/tokenizer for one translation direction."""
    state = _opus_state[direction]
    if state.get("encoder") is not None:
        return
    cfg = _OPUS_CONFIG[direction]

    print(f"[OpusMT:{direction}] [1/3] Checking / downloading model...")
    model_subdir = _opus_download_model(direction)

    print(f"[OpusMT:{direction}] [2/3] Loading tokenizer...")
    tokenizer_dir = cfg["model_dir"] / "tokenizer"
    tokenizer = _opus_get_marian_tokenizer(tokenizer_dir, cfg["hf_model_id"], cfg.get("hf_revision"))
    print(f"[OpusMT:{direction}]       Vocab={len(tokenizer)}")

    print(f"[OpusMT:{direction}] [3/3] Loading encoder/decoder onto NPU...")
    _ensure_qnn_config()
    encoder_model = _OpusMTEncoder(f"{direction}_encoder", str(model_subdir / "encoder.bin"))
    decoder_model = _OpusMTDecoder(f"{direction}_decoder", str(model_subdir / "decoder.bin"))

    state["encoder"] = encoder_model
    state["decoder"] = decoder_model
    state["tokenizer"] = tokenizer


# Max "words" per OpusMT translation chunk. The 256-token encoder window is
# plenty for a chunk this size, but the real constraint is semantic: this
# model's greedy decoder can fire EOS early on a long run-on sentence (ASR
# output has no punctuation to break on), silently dropping everything
# after that point. Observed directly: a 119-token / ~55-word ASR
# transcript decoded to only 31 tokens (~1/3 of the input) in one shot, but
# translated completely when split into <=20-word pieces first for English
# input. For Chinese input, this was found NOT to be enough on its own --
# see the two fixes below -- so the limit is 12 units (chars for CJK,
# words for everything else), still comfortably inside a clause's worth of
# context for either language.
#
# Fix 1 of 2 -- the unit of measure: `len(s.split())` (whitespace word
# count) is meaningless for unsegmented CJK text -- Chinese has no spaces,
# so a whole run-on sentence like "这是一个中文的英文的例子你看看对不对" counted
# as a single "word" no matter how long it actually was, so
# _split_into_translation_chunks() below never split it and the full
# sentence was handed to OpusMT in one shot. _count_translation_units()
# instead counts each CJK character as its own unit and keeps every run
# of non-CJK characters (an English word, a number, punctuation) as a
# single unit, so Chinese input is measured by character count and
# English/mixed input is still measured by word count as before.
#
# Fix 2 of 2 -- the threshold itself: even measured correctly, that same
# 18-char sentence is still under the old 20-word/unit limit and so still
# was NOT split, and OpusMT's greedy decoder fired EOS early on it anyway,
# silently dropping "你看看对不对" off the end of the translation (reproduced
# directly: translating the same 18-char sentence in one shot drops the
# tail, but splitting it into two <=12-unit halves translates both
# completely). Dropping the limit to 12 keeps Chinese chunks well inside
# the window where this model's decoder reliably finds its own EOS.
_OPUS_CHUNK_MAX_WORDS = 12
# English source (en2zh) is measured in words and does not show the early-EOS
# problem at this length (a 20-word JFK sentence translates completely in one
# shot), while hard-cutting it every 12 words splits clauses mid-phrase and
# visibly degrades the Chinese. Keep 12 for CJK source text (zh2en) only.
_OPUS_CHUNK_MAX_WORDS_EN = 30
_CJK_UNIT_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\s\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _count_translation_units(text: str) -> int:
    """Measure `text`'s length in OpusMT chunking units: one unit per CJK
    character, one unit per whitespace-delimited non-CJK word."""
    return len(_CJK_UNIT_PATTERN.findall(text))


def _split_into_translation_chunks(text: str, max_words: int = _OPUS_CHUNK_MAX_WORDS) -> list[str]:
    """Split text into translation-safe chunks of ~max_words units each
    (see _split_text_for_model for the splitting strategy, and
    _count_translation_units for why CJK text can't just use
    ``len(s.split())`` like the other chunkers in this file)."""
    return _split_text_for_model(text, _count_translation_units, max_words)


# ─── English ASR transcript clean-up (runs between Zipformer and OpusMT) ─────
# Zipformer's English output is ALL-CAPS, has no punctuation or apostrophes,
# and for casual / accented speech often contains stutters ("ITS ITS"),
# wrong a/an, and sub-word fragments of a real word ("TRANSLA SHIN" for
# "translation"). MarianMT translates such text poorly, so we repair what can
# be repaired deterministically, all on-CPU and offline:
#   1. de-stutter + restore contractions / "I"
#   2. phonetic repair: non-dictionary tokens (and their neighbours) are
#      matched, by phoneme edit distance (g2p_en + CMUdict), against the
#      common English words of the Marian vocabulary, so "TRANSLA SHIN"
#      -> "translation"
#   3. a/an agreement, capitalisation, light punctuation (comma after a
#      leading "hello/so/well...", "?" for clear questions, final ".") --
#      punctuation measurably improves Marian output.
# Every step degrades to a no-op when its dependency is missing.

_EN_CONTRACTIONS = {
    "dont": "don't", "doesnt": "doesn't", "didnt": "didn't", "cant": "can't",
    "wont": "won't", "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't",
    "werent": "weren't", "havent": "haven't", "hasnt": "hasn't", "hadnt": "hadn't",
    "couldnt": "couldn't", "wouldnt": "wouldn't", "shouldnt": "shouldn't",
    "im": "I'm", "ive": "I've", "youre": "you're", "youve": "you've",
    "theyre": "they're", "theyve": "they've", "weve": "we've", "thats": "that's",
    "whats": "what's", "heres": "here's", "theres": "there's", "lets": "let's",
    "hows": "how's", "wheres": "where's",
}
# "its" is the contraction (it's) when followed by one of these.
_EN_ITS_CONTRACTION_NEXT = {
    "a", "an", "the", "not", "been", "going", "very", "so", "too", "just",
    "really", "time", "ok", "okay", "all", "only", "also", "still", "now",
    "like", "about", "what", "how", "your", "my", "his", "her", "our", "their",
    "this", "that", "there", "here", "good", "great", "fine", "nice", "true",
}
_EN_REPEAT_OK = {"had", "that", "very", "so", "no", "bye", "yes", "now", "go", "well"}
_EN_WH = {"what", "where", "when", "why", "who", "whom", "whose", "which", "how"}
_EN_AUX = {"is", "are", "was", "were", "do", "does", "did", "can", "could",
           "would", "will", "should", "has", "have", "had", "may", "might"}
_EN_SUBJ = {"you", "i", "we", "they", "it", "he", "she", "there", "this", "that"}
_EN_LEAD_COMMA = {"hello", "hi", "hey", "yes", "no", "okay", "ok", "well",
                  "so", "sorry", "oh", "right"}
# words that must be capitalised / upper-cased in the clean-up output
_EN_PROPER = {
    "american": "American", "americans": "Americans", "america": "America",
    "china": "China", "chinese": "Chinese", "english": "English", "shanghai": "Shanghai",
    "beijing": "Beijing", "london": "London", "paris": "Paris", "tokyo": "Tokyo",
    "japan": "Japan", "japanese": "Japanese", "europe": "Europe", "european": "European",
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday",
    "january": "January", "february": "February", "april": "April", "june": "June",
    "july": "July", "august": "August", "september": "September", "october": "October",
    "november": "November", "december": "December", "ai": "AI", "npu": "NPU",
    "cpu": "CPU", "gpu": "GPU", "usb": "USB", "wifi": "Wi-Fi", "tv": "TV",
}
_EN_PHONE_VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY",
                    "IH", "IY", "OW", "OY", "UH", "UW"}
_EN_PHONE_SIMILAR = [{"S", "Z"}, {"T", "D"}, {"P", "B"}, {"K", "G"}, {"F", "V"},
                     {"SH", "ZH", "CH", "JH"}, {"TH", "DH"}, {"M", "N", "NG"}]

_en_fix_state: dict[str, Any] = {"tried": False, "ok": False}


def _en_fix_init(spm_proc) -> bool:
    """Lazily build the phonetic lexicon: common English words (the Marian
    source vocabulary) that CMUdict can pronounce. Returns False if the
    optional deps (g2p_en / nltk cmudict) are unavailable."""
    if _en_fix_state["tried"]:
        return _en_fix_state["ok"]
    _en_fix_state["tried"] = True
    try:
        from g2p_en import G2p
        g2p = G2p()
        cmu = g2p.cmu
        lex = {}
        if spm_proc is not None:
            for i in range(spm_proc.get_piece_size()):
                piece = spm_proc.id_to_piece(i)
                if piece.startswith("\u2581") and re.fullmatch(r"[a-z]{3,}", piece[1:]):
                    w = piece[1:]
                    if w in cmu:
                        lex[w] = (tuple(re.sub(r"\d", "", ph) for ph in cmu[w][0]),
                                  spm_proc.get_score(i))
        for w in ("a", "an", "to", "of", "in", "on", "it", "is", "at", "as", "or"):
            if w in cmu:
                lex.setdefault(w, (tuple(re.sub(r"\d", "", ph) for ph in cmu[w][0]), -5.0))
        _en_fix_state.update(g2p=g2p, cmu=cmu, lex=lex, ok=bool(lex))
    except Exception as e:                                   # noqa: BLE001
        print(f"[ASR-Fix][WARN] phonetic repair disabled ({type(e).__name__}: {e})")
        _en_fix_state["ok"] = False
    return _en_fix_state["ok"]


def _en_phones(word: str) -> tuple:
    st = _en_fix_state
    if word in st["cmu"]:
        raw = st["cmu"][word][0]
    else:
        raw = st["g2p"].predict(word)
    return tuple(re.sub(r"\d", "", ph) for ph in raw if re.match(r"[A-Z]", ph))


def _en_sub_cost(x: str, y: str) -> float:
    if x == y:
        return 0.0
    if x in _EN_PHONE_VOWELS and y in _EN_PHONE_VOWELS:
        return 0.5
    for grp in _EN_PHONE_SIMILAR:
        if x in grp and y in grp:
            return 0.5
    return 1.0


def _en_phone_distance(a: tuple, b: tuple) -> float:
    prev = [float(j) for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        cur = [float(i)] + [0.0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1.0, cur[j - 1] + 1.0,
                         prev[j - 1] + _en_sub_cost(a[i - 1], b[j - 1]))
        prev = cur
    return prev[-1]


def _en_lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _en_best_word(phones: tuple, max_norm: float, spelling: str = ""):
    """Closest lexicon word -> (word, cost). Cost blends phoneme distance (the
    ASR error is acoustic), letter distance (g2p guesses unseen spellings
    badly: 'coffe' -> K AO F) and a small preference for common words."""
    best = (None, 1e9)
    n = len(phones)
    for w, (wp, score) in _en_fix_state["lex"].items():
        if abs(len(wp) - n) > max(2, n // 2):
            continue
        d = _en_phone_distance(phones, wp) / max(len(wp), n)
        if spelling:
            ld = _en_lev(spelling, w) / max(len(spelling), len(w))
            if ld > 0.3:                      # too different on paper: likely a name, not a typo
                continue
            if (spelling in _en_fix_state["cmu"] and len(spelling) >= 5
                    and (spelling.startswith(w) or w.startswith(spelling))):
                continue                      # 'jumps' vs 'jump': inflection, not an error
            d = 0.55 * d + 0.45 * ld
        d += 0.004 * (-score)
        if d < best[1]:
            best = (w, d)
    return best if best[1] <= max_norm else (None, best[1])


def _en_phonetic_repair(words: list[str]) -> list[str]:
    """Replace non-dictionary tokens (alone, or merged with a neighbour) by the
    phonetically closest common word."""
    cmu = _en_fix_state["cmu"]
    lex = _en_fix_state["lex"]
    suspects = [i for i, w in enumerate(words)
                if w.isalpha() and w.isascii() and w not in _EN_CONTRACTIONS
                and w not in lex and w not in _EN_SHORT_OK
                and (w not in cmu or len(w) >= 5)]
    def _is_inflection(w: str) -> bool:
        for suf in ("s", "es", "ed", "d", "ing", "ly", "er", "ers", "ness"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                stem = w[: -len(suf)]
                if stem in cmu or stem + "e" in cmu or (len(stem) > 3 and stem[-1] == stem[-2]
                                                         and stem[:-1] in cmu):
                    return True
        return False

    suspects = [i for i in suspects if not (words[i] in cmu and _is_inflection(words[i]))]
    known_rare = {i for i in suspects if words[i] in cmu}
    if not suspects:
        return words
    out = list(words)
    consumed = set()
    for i in suspects:
        if i in consumed:
            continue
        best = None                                   # (cost, start, end, word)
        for st_ in (i - 1, i):
            for en_ in (i + 1, i + 2, i + 3):
                if st_ < 0 or en_ > len(words) or (en_ - st_) > 3:
                    continue
                span = words[st_:en_]
                if any(j in consumed for j in range(st_, en_)):
                    continue
                if not all(t.isalpha() and t.isascii() for t in span):
                    continue
                if len(span) > 1 and any(len(t) > 2 and t in cmu and t != words[i]
                                         and len(span) > 2 for t in span):
                    continue                          # don't swallow >1 real words
                phones = ()
                for t in span:
                    phones += _en_phones(t)
                if len(phones) < 3:
                    continue
                w, cost = _en_best_word(phones, 0.34 if len(span) == 1 else 0.26, "".join(span))
                if w is None:
                    continue
                if i in known_rare and (len(span) > 1 or _en_lev(words[i], w) > 1):
                    continue          # a real (rare) word: only accept a 1-letter slip
                cost -= 0.02 * (len(span) - 1)        # mild bonus for merging fragments
                if best is None or cost < best[0]:
                    best = (cost, st_, en_, w)
        if best is not None:
            _, st_, en_, w = best
            out[st_] = w
            for j in range(st_ + 1, en_):
                out[j] = ""
            consumed.update(range(st_, en_))
    return [w for w in out if w]


_EN_SHORT_OK = {"a", "i", "an", "to", "of", "in", "on", "it", "is", "at", "as", "or",
                "be", "by", "do", "go", "he", "if", "me", "my", "no", "so", "up",
                "us", "we", "oh", "ok", "hi", "am", "ah", "ya", "yo"}


def _en_merge_split_word(words: list[str]) -> list[str]:
    """Join two adjacent *rare* words when their combined pronunciation almost
    exactly matches one common word ("trans nation" -> "translation").
    Frequent words (the/you/is...) are never merged, so "any one" etc. stay."""
    lex = _en_fix_state["lex"]
    out, i = [], 0
    while i < len(words):
        if i + 1 < len(words):
            a, b = words[i], words[i + 1]
            if (a.isalpha() and b.isalpha() and a.isascii() and b.isascii()
                    and a not in _EN_SHORT_OK and b not in _EN_SHORT_OK
                    and lex.get(a, (None, -99.0))[1] < -8.0
                    and lex.get(b, (None, -99.0))[1] < -8.0 + 3.0):
                phones = _en_phones(a) + _en_phones(b)
                if len(phones) >= 6:
                    w, cost = _en_best_word(phones, 0.18, a + b)
                    if w is not None and w not in (a, b):
                        out.append(w)
                        i += 2
                        continue
        out.append(words[i])
        i += 1
    return out


def _en_drop_stray_fragments(words: list[str]) -> list[str]:
    """Drop a trailing 1-2 letter token that is not a real short word -- ASR
    leftovers such as the 'ta' of a cut-off 'test'."""
    if (len(words) > 3 and words[-1].isalpha() and len(words[-1]) <= 2
            and words[-1] not in _EN_SHORT_OK):
        return words[:-1]
    return words


def _normalize_english_asr_text(text: str, spm_proc=None) -> str:
    """Clean a raw (usually ALL-CAPS, unpunctuated) Zipformer English transcript
    for MarianMT -- see the section comment above. Mixed-case input from a
    --text argument is only lightly touched (punctuation is never forced on
    text that already has some)."""
    raw = text.strip()
    if not raw:
        return raw
    letters = [c for c in raw if c.isalpha() and c.isascii()]
    shouty = bool(letters) and all(c.isupper() for c in letters)
    has_punct = bool(re.search(r"[.,!?;:]", raw))
    if not shouty:
        # Only the ALL-CAPS Zipformer transcript needs repairing. Anything with
        # real casing (e.g. --text "I love Alice") is passed through untouched,
        # so proper nouns keep their capital letters.
        return raw

    words = re.findall(r"[A-Za-z']+|[^\sA-Za-z']+", raw.lower())

    # 1. stutters: "its its" -> "its"
    deduped = []
    for w in words:
        if deduped and w == deduped[-1] and w not in _EN_REPEAT_OK:
            continue
        deduped.append(w)
    words = deduped

    # 2. contractions and the pronoun I (before repair, so "dont" is never
    #    mistaken for a mis-heard "don")
    fixed = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w == "its" and nxt in _EN_ITS_CONTRACTION_NEXT:
            w = "it's"
        elif w in _EN_CONTRACTIONS:
            w = _EN_CONTRACTIONS[w]
        elif w == "i":
            w = "I"
        elif re.fullmatch(r"i'(m|ll|ve|d)", w):
            w = "I" + w[1:]
        fixed.append(w)
    words = fixed

    # 3. phonetic repair of fragments / unknown words (optional)
    if _en_fix_init(spm_proc):
        words = _en_merge_split_word(words)
        words = _en_phonetic_repair(words)
    words = _en_drop_stray_fragments(words)

    # 4. a / an agreement (by the first phoneme when we have a dictionary)
    for i in range(len(words) - 1):
        if words[i] in ("a", "an") and words[i + 1].isalpha():
            nxt = words[i + 1]
            if _en_fix_state.get("ok") and (nxt in _en_fix_state["cmu"]):
                vowel = re.sub(r"\d", "", _en_fix_state["cmu"][nxt][0][0]) in _EN_PHONE_VOWELS
            else:
                vowel = nxt[0] in "aeiou"
            words[i] = "an" if vowel else "a"

    if not words:
        return raw

    # 5. punctuation + capitalisation
    body = []
    for w in words:
        if body and re.fullmatch(r"[.,!?;:]+", w):
            body[-1] += w
        else:
            body.append(w)
    if not has_punct:
        first, second = body[0], (body[1] if len(body) > 1 else "")
        lead_skip = 0
        if first in _EN_LEAD_COMMA and len(body) > 2:
            body[0] += ","
            lead_skip = 1
        elif first == "and" and second == "so" and len(body) > 3:
            body[1] += ","
        if lead_skip:
            first, second = body[1].rstrip(",."), (body[2] if len(body) > 2 else "")
        is_q = ((first in _EN_WH and (second in _EN_AUX or second in {"many", "much", "about", "time"}))
                or (first in _EN_AUX and second in _EN_SUBJ))
        if not body[-1][-1] in ".!?":
            body[-1] += "?" if is_q else "."
    body = [re.sub(r"^[a-z']+", lambda m: _EN_PROPER.get(m.group(0), m.group(0)), w) for w in body]
    sent = " ".join(body)
    return sent[:1].upper() + sent[1:]


# ─── English source rewrite for MT (en2zh) + Chinese post-fix ───────────────
# MarianMT handles archaic / elliptical English badly ("ask not what ..." ->
# "不问...", clause pairs glued together, "your" rendered as plural 你们 in one
# place and singular 你 in another). Rewrite such constructs into plain modern
# English with explicit clause punctuation before translating.
_EN_NEG_IMPERATIVE = ("ask", "think", "fear", "forget", "worry", "doubt", "wonder",
                      "wait", "stop", "care", "blame", "expect")
_EN_PLURAL_YOU_CUES = re.compile(
    r"\b(you all|y'all|you guys|you people|you folks|you two|you both|both of you|"
    r"all of you|everyone|everybody|ladies|gentlemen|folks|students|friends)\b", re.I)


def _en_rewrite_for_mt(text: str) -> str:
    t = text
    # "ask not X" -> "do not ask X" (archaic negative imperative)
    t = re.sub(r"\b(" + "|".join(_EN_NEG_IMPERATIVE) + r") not\b",
               lambda m: "do not " + m.group(1).lower() if m.group(0)[0].islower()
               else "Do not " + m.group(1).lower(), t, flags=re.I)
    # parallel imperative clauses: "... for you ask what ..." -> "..., ask what ..."
    t = re.sub(r"(\bdo not (?:ask|think|wonder|care)\b[^,.;!?]*?)\s+(ask|think|wonder|care)\s+(what|how|whether|why)\b",
               r"\1, but \2 \3", t, flags=re.I)
    # vocative comma: "my fellow Americans do not ask" -> "my fellow Americans, do not ask"
    t = re.sub(r"(\bmy fellow [A-Za-z]+|\bladies and gentlemen|\bmy friends) (?=(?:do not|let|ask|we|I|you)\b)",
               r"\1, ", t, flags=re.I)
    # second parallel clause with the same wh-structure: "..., ask what" stays;
    # ensure the final clause is not glued to the first by a bare space
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _zh_postfix_en2zh(src: str, zh: str) -> str:
    """Make 'you/your' consistently singular unless the source marks a plural
    addressee, and tidy MarianMT's literal negative imperatives."""
    if _EN_PLURAL_YOU_CUES.search(src) is None:
        zh = zh.replace("你们", "你")
    zh = re.sub(r"不问(?=你|我|他|她|它|我们|国家|[\u4e00-\u9fff]{0,4}能为)", "不要问", zh)
    zh = re.sub(r"(?<=，)问问(?=你)", "问", zh)
    # contrastive second clause: "不要问...，也不要问/但要问/问..." -> "...，而要问..."
    if "不要问" in zh and re.search(r"\bbut (ask|think|wonder|care)\b", src, re.I):
        zh = re.sub(r"(不要问[^，。]*，)(?:也不要问|但是?要问|但是?问|要问|问)", r"\1而要问", zh, count=1)
    # discourse marker: "And so" / "So" at sentence start -> 所以
    if re.match(r"\s*(and )?so\b", src, re.I):
        zh = re.sub(r"^(因此|因而|所以|那么)(?=，)", "所以", zh)
    # keep the possessive of the vocative: "my fellow Americans" -> 我的美国同胞们
    if re.search(r"\bmy fellow\b", src, re.I):
        zh = re.sub(r"(?<!我的)(?<!我)(美国同胞)(?!们)", r"\1们", zh)
        zh = re.sub(r"(?<!我的)(美国同胞们)", r"我的\1", zh, count=1)
        zh = re.sub(r"(我的美国同胞们)(?=不要)", r"\1，", zh)
    zh = zh.replace("而是要问", "而要问")
    if re.search(r"\bcan do for\b", src, re.I):
        zh = zh.replace("可以为", "能为")
    return zh


_CJK_CH = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
_ZH_PUNCT = {",": "\uff0c", "?": "\uff1f", "!": "\uff01", ";": "\uff1b", ":": "\uff1a", ".": "\u3002"}


def _clean_zh_text(text: str) -> str:
    """Tidy MarianMT's Chinese output. It separates clauses with a bare ASCII
    space ("今天天气真好 我们去公园散步吧") and mixes ASCII punctuation in.
    A space between two CJK characters is a clause break -> "，" (which also
    gives the TTS a proper pause); ASCII punctuation after a CJK character
    becomes full-width; spaces next to full-width punctuation are dropped.
    Spaces around Latin words / digits are kept."""
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(rf"(?<={_CJK_CH}) (?={_CJK_CH})", "\uff0c", t)
    t = re.sub(rf"(?<={_CJK_CH})\s*([,?!;:.])(?!\d)", lambda m: _ZH_PUNCT[m.group(1)], t)
    t = re.sub(r"\s*([\uff0c\u3002\uff1f\uff01\uff1b\uff1a\u3001])\s*", r"\1", t)
    t = re.sub(r"([\uff0c\u3002\uff1f\uff01\uff1b\uff1a])\1+", r"\1", t)
    t = re.sub(r"\uff0c([\u3002\uff1f\uff01])", r"\1", t)
    return t.strip("\uff0c ").strip()


# ─── Chinese ASR transcript preparation + coverage guard (zh2en) ──────────
# Zipformer's Mandarin output has no punctuation. MarianMT was trained on
# punctuated sentences and (a) fires EOS early on a long unpunctuated run
# ("...today." and the rest silently dropped), and (b) mistranslates a clause
# chopped off mid-phrase ("适合|出去旅游" -> a lone "Traveling."). So instead of
# cutting every 12 characters we (1) restore light punctuation at clause
# boundaries, (2) chunk only at those boundaries (<= _OPUS_CHUNK_MAX_ZH chars),
# and (3) after translating, check the English is not suspiciously short for
# its source and, if it is, retry with greedy search / a clause split.

_OPUS_CHUNK_MAX_ZH = 64
_ZH_CLAUSE_STARTERS = (
    "然后", "但是", "可是", "不过", "因为", "所以", "如果", "接着", "首先", "其次",
    "最后", "比如", "例如", "虽然", "即使", "只要", "于是", "因此", "由于", "除了",
    "总之", "其实", "当然", "不但", "不仅", "既然", "无论", "尽管", "否则", "此外",
    "请问", "麻烦",
)
# NOT in the list on purpose: 并且 / 而且 / 以及 / 或者 / 还有 / 同时 / 以便 / 为了.
# They join two parts of the *same* clause; a comma before them made MarianMT
# produce a broken fragment ("..., to be attended on time and to be ready").
_ZH_QUESTION_RE = re.compile(
    r"(什么|怎么|怎样|怎么样|哪里|哪儿|哪个|哪些|多少|几点|几个|谁|为什么|为何|是否|能否|"
    r"可不可以|能不能|行不行|好不好|对不对|是不是|有没有|要不要|会不会|可以吗|吗|呢)\s*$"
    r"|(.)不\2\s*$"
)
_ZH_PUNCT_CHARS = "，。？！；：、,.?!;:"


def _zh_restore_punctuation(text: str) -> str:
    """Add light clause punctuation to an unpunctuated Chinese ASR transcript.
    Text that already contains punctuation is returned unchanged."""
    # Remove whitespace next to CJK characters only; the spaces between Latin
    # words of code-switched speech ("use Python and Java") must survive.
    t = re.sub(rf"\s+(?={_CJK_CH})|(?<={_CJK_CH})\s+", "", (text or "").strip())
    t = re.sub(r"\s+", " ", t)
    if not t or any(c in t for c in _ZH_PUNCT_CHARS):
        return (text or "").strip()
    since_last = 0
    out = []
    i = 0
    while i < len(t):
        hit = None
        if since_last >= 4:
            for c in _ZH_CLAUSE_STARTERS:
                if t.startswith(c, i):
                    hit = c
                    break
        if hit:
            out.append("\uff0c")
            since_last = 0
        out.append(t[i])
        since_last += 1
        # sentence-final particles in the middle of the text end a clause
        if t[i] in "吗吧啊呀" and i + 1 < len(t) and since_last >= 3:
            out.append("\uff1f" if t[i] == "吗" else "\uff0c")
            since_last = 0
        i += 1
    res = "".join(out)
    if res[-1] not in "\u3002\uff1f\uff01":
        res += "\uff1f" if _ZH_QUESTION_RE.search(res) else "\u3002"
    return res


_EN_CONT_WORDS = {"but", "so", "because", "and", "then", "although", "or", "which",
                  "while", "also", "since", "though", "however"}


def _join_translated_parts(parts: list[str], seps: list[str]) -> str:
    """Join per-chunk English. When the source chunk ended with a comma, the
    next chunk continues the same sentence: use ', ' and lower-case a leading
    conjunction ('... milk. So we ...' -> '... milk, so we ...')."""
    out = ""
    for part, sep in zip(parts, seps):
        part = part.strip()
        if not part:
            continue
        if not out:
            out = part
            continue
        first = re.match(r"[A-Za-z']+", part)
        if sep in ("\uff0c", ",") and first and first.group(0).lower() in _EN_CONT_WORDS:
            out = re.sub(r"[.\u3002!]+$", "", out) + ", " + part[0].lower() + part[1:]
        else:
            out = out + " " + part
    return out


def _word_count(s: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", s))


def _out_units(direction: str, out: str) -> int:
    """Size of a translation in comparable units: English words (zh2en) or
    Chinese characters (en2zh)."""
    return _word_count(out) if direction == "zh2en" else len(re.findall(_CJK_CH, out))


def _low_coverage(direction: str, src: str, out: str) -> bool:
    """True if `out` is suspiciously short for `src` (early-EOS symptom)."""
    if direction == "zh2en":
        units = len(re.findall(_CJK_CH, src))
        return units >= 10 and _word_count(out) / units < 0.40
    units = _word_count(src)
    zh = len(re.findall(_CJK_CH, out))
    return units >= 8 and zh / units < 0.85


def _split_clause_midpoint(text: str) -> list[str]:
    """Split a chunk into two halves at the clause boundary nearest its middle."""
    n = len(text)
    if n < 12:
        return [text]
    cands = [m.end() for m in re.finditer(r"[\uff0c,;\uff1b]", text)]
    for c in _ZH_CLAUSE_STARTERS:
        cands += [m.start() for m in re.finditer(re.escape(c), text) if 4 <= m.start() <= n - 4]
    cands = [c for c in cands if 4 <= c <= n - 4]
    cut = min(cands, key=lambda c: abs(c - n / 2)) if cands else n // 2
    return [text[:cut], text[cut:]]


def _translate_chunk_guarded(direction: str, chunk: str):
    """_opus_translate_chunk + early-EOS recovery. Returns the same 4-tuple."""
    best = _opus_translate_chunk(direction, chunk)
    if not _low_coverage(direction, chunk, best[0]):
        return best
    tried = [("beam", best),
             ("greedy", _opus_translate_chunk(direction, chunk, beam=1))]
    ok = [t for t in tried if not _low_coverage(direction, chunk, t[1][0])]
    if not ok and direction == "zh2en":
        halves = [h for h in _split_clause_midpoint(chunk) if h.strip()]
        if len(halves) == 2:
            parts = [_opus_translate_chunk(direction, h) for h in halves]
            text = _join_translated_parts([p[0] for p in parts], ["", halves[0][-1:]])
            merged = (text, sum(p[1] for p in parts), sum(p[2] for p in parts), sum(p[3] for p in parts))
            tried.append(("split", merged))
            ok = [t for t in tried if not _low_coverage(direction, chunk, t[1][0])] or [tried[-1]]
    pool = ok or tried
    chosen = max(pool, key=lambda t: _out_units(direction, t[1][0]))
    return chosen[1]


# ─── Code-switched (Chinese + English) input (zh2en) ──────────────────────
# Zipformer writes the English part of mixed speech in ALL CAPS and glued to
# the Chinese ("昨天是MONDAY TODAY IS礼拜二THE DAY AFTER TOMORROW是星期三").
#   * MarianMT copies ALL-CAPS English verbatim and corrupts it ("TODAY IIS",
#     "TODY", "AFFTER"); lower-casing it instead makes Marian re-translate and
#     drop it ("the day after Wednesday").
#   * gruut (Piper's G2P) reads every ALL-CAPS word as an acronym and spells
#     it letter by letter ("T-O-D-A-Y"), so the English speech was garbage.
# So for zh2en every Latin run is replaced by a placeholder (X1, X2, ...),
# Marian translates the Chinese around it, and the original English (with
# normal casing restored) is put back. If Marian loses / duplicates a
# placeholder we fall back to translating the case-fixed text directly.
# _en_fix_caps is also applied in front of Piper for any remaining caps.

_EN_ACRONYMS = {
    "ok": "OK", "usa": "USA", "uk": "UK", "nasa": "NASA", "ui": "UI", "ceo": "CEO",
    "api": "API", "pc": "PC", "ppt": "PPT", "pdf": "PDF", "vip": "VIP", "qq": "QQ",
    "ios": "iOS", "iphone": "iPhone", "ipad": "iPad", "id": "ID",
    "python": "Python", "java": "Java", "javascript": "JavaScript", "github": "GitHub",
    "google": "Google", "microsoft": "Microsoft", "windows": "Windows", "android": "Android",
    "qualcomm": "Qualcomm", "snapdragon": "Snapdragon", "linux": "Linux", "wechat": "WeChat",
}
_EN_LETTER_VOWEL = set("AEFHILMNORSX")        # letter names that start with a vowel sound
_ZH_LATIN_RUN = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*(?: +[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*)*")
_ZH_PLACEHOLDER = re.compile(r"\bX(\d+)\b")

_gruut_lex_state: dict[str, Any] = {"tried": False, "conn": None}


def _gruut_lexicon_has(word: str) -> bool | None:
    """True/False if `word` (lower-case) is in gruut's en-us lexicon, None if
    the lexicon cannot be read."""
    st = _gruut_lex_state
    if not st["tried"]:
        st["tried"] = True
        try:
            import importlib.util
            import sqlite3
            spec = importlib.util.find_spec("gruut_lang_en")
            db = Path(spec.origin).parent / "lexicon.db"
            st["conn"] = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                                         check_same_thread=False)
        except Exception:                                    # noqa: BLE001
            st["conn"] = None
    if st["conn"] is None:
        return None
    try:
        return st["conn"].execute("SELECT 1 FROM word_phonemes WHERE word=? LIMIT 1",
                                  (word,)).fetchone() is not None
    except Exception:                                        # noqa: BLE001
        return None


def _en_fix_caps(text: str) -> str:
    """Give ALL-CAPS words normal casing ("TODAY IS" -> "today is", "MONDAY"
    -> "Monday") while keeping real acronyms ("AI", "NPU", "SDK"). Mixed-case
    words are untouched."""
    def fix(m):
        w = m.group(0)
        if len(w) < 2 or not w.isupper():
            return w
        low = w.lower()
        if low in _EN_ACRONYMS:
            return _EN_ACRONYMS[low]
        if low in _EN_PROPER:
            return _EN_PROPER[low]
        if not re.search(r"[aeiouy]", low):                  # SDK, GPS, HTTP
            return w
        known = _gruut_lexicon_has(low)
        if known or len(low) >= 5 or (known is None and len(low) >= 3):
            return low
        return w
    t = re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", fix, text)
    # an ALL-CAPS article "A" in front of a (now lower-case) word: "A good" -> "a good"
    t = re.sub(r"(?<=[a-z,] )A(?= [a-z])|^A(?= [a-z]+ [a-z])", "a", t)
    t = re.sub(r"\bi\b", "I", t)
    return re.sub(r"\bi'(m|ll|ve|d)\b", r"I'\1", t)


def _en_article_for(phrase: str) -> str:
    """'a' or 'an' in front of `phrase` (spelling-based heuristic)."""
    words = phrase.split()
    w = words[0] if words else ""
    if not w:
        return "a"
    if len(w) > 1 and w.isupper():                          # acronym: read letter by letter
        return "an" if w[0] in _EN_LETTER_VOWEL else "a"
    low = w.lower()
    if low[0].isdigit():
        return "an" if low.startswith(("8", "11", "18")) else "a"
    if low.startswith(("hour", "honest", "honor", "heir")):
        return "an"
    if low.startswith(("uni", "use", "usu", "eu", "one", "once")):
        return "a"
    return "an" if low[0] in "aeiou" else "a"


def _zh_protect_latin(chunk: str) -> tuple[str, list[str]]:
    """Replace each Latin-script run of a mixed Chinese chunk by X1, X2, ...
    Returns (protected_text, runs); runs is empty when nothing was replaced."""
    runs: list[str] = []
    if not re.search(_CJK_CH, chunk):
        return chunk, runs

    def sub(m):
        s = m.group(0)
        if not re.search(r"[A-Za-z]", s):                    # plain numbers: Marian handles them
            return s
        runs.append(s)
        return f" X{len(runs)} "
    prot = _ZH_LATIN_RUN.sub(sub, chunk)
    prot = re.sub(r" {2,}", " ", prot).strip()
    prot = re.sub(r" +(?=[\uff0c\u3002\uff1f\uff01\uff1b\uff1a\u3001,.?!;:])", "", prot)
    return prot, runs


def _zh_restore_latin(out: str, runs: list[str]) -> str | None:
    """Put the original English back into Marian's output. Returns None if
    the placeholders did not survive translation exactly once each."""
    found = sorted(int(n) for n in _ZH_PLACEHOLDER.findall(out))
    if found != list(range(1, len(runs) + 1)):
        return None
    fixed = [_en_fix_caps(r) for r in runs]

    def article(m):
        a = _en_article_for(fixed[int(m.group(2)) - 1])
        return (a[0].upper() + a[1:] if m.group(1) == "A" else a) + " "
    out = re.sub(r"\b([Aa])n? (?=X(\d+)\b)", article, out)

    def rep(m):
        run = fixed[int(m.group(1)) - 1]
        before = out[:m.start()].rstrip()
        if not before or before[-1] in ".!?":
            run = run[0].upper() + run[1:]
        return run
    out = _ZH_PLACEHOLDER.sub(rep, out)
    # the English run may already carry the verb/article Marian adds again
    # ("today is" + "X2 is Tuesday" -> "today is is Tuesday")
    return re.sub(r"\b(is|are|was|were|am|the|a|an|to|of|and|it's)\s+\1\b", r"\1", out, flags=re.I)


def _translate_chunk_code_switched(chunk: str, verbose: bool = False):
    """zh2en chunk translation that keeps embedded English intact (see the
    section comment above). Returns the same 4-tuple as _opus_translate_chunk."""
    prot, runs = _zh_protect_latin(chunk)
    if not runs:
        return _translate_chunk_guarded("zh2en", chunk)
    res = _translate_chunk_guarded("zh2en", prot)
    restored = _zh_restore_latin(res[0], runs)
    if restored is not None:
        if verbose:
            print(f"[MT-CodeSwitch] {prot!r} -> {res[0]!r}")
        return (restored,) + tuple(res[1:])
    if verbose:
        print(f"[MT-CodeSwitch] placeholders lost ({res[0]!r}); translating the plain text")
    plain = _translate_chunk_guarded("zh2en", _en_fix_caps(chunk))
    return (plain[0], res[1] + plain[1], res[2] + plain[2], res[3] + plain[3])


def opus_translate(direction: str, text: str, verbose: bool = True) -> str:
    """Translate `text` using the encoder+decoder loaded for `direction`.

    Long input is split into <=_OPUS_CHUNK_MAX_WORDS-unit chunks (chars for
    CJK text, words otherwise -- see _split_into_translation_chunks /
    _count_translation_units) and each chunk is translated independently,
    then the results are joined with a space. This avoids the greedy
    decoder firing EOS early partway through a long run-on sentence and
    silently dropping the rest of the translation.
    """
    opus_init(direction)

    max_units = _OPUS_CHUNK_MAX_WORDS
    if direction == "zh2en":
        raw_text = text
        text = _zh_restore_punctuation(text)
        if verbose and text != raw_text:
            print(f"[ASR-Fix] {raw_text!r} -> {text!r}")
        max_units = _OPUS_CHUNK_MAX_ZH
    if direction == "en2zh":
        raw_text = text
        spm_proc = getattr(_opus_state[direction].get("tokenizer"), "spm_source", None)
        text = _normalize_english_asr_text(text, spm_proc)
        if verbose and text != raw_text:
            print(f"[ASR-Fix] {raw_text!r} -> {text!r}")
        max_units = _OPUS_CHUNK_MAX_WORDS_EN
        _mt_src = _en_rewrite_for_mt(text)
        if verbose and _mt_src != text:
            print(f"[MT-Rewrite] {text!r} -> {_mt_src!r}")
        text = _mt_src

    chunks = _split_into_translation_chunks(text, max_units)
    if not chunks:
        return ""

    if verbose:
        print(f"\n[OpusMT:{direction}] Input : {text}")
        if len(chunks) > 1:
            print(f"[OpusMT:{direction}] Split into {len(chunks)} chunk(s) "
                  f"(<= {max_units} units each) to avoid early EOS on long input.")

    translated_parts = []
    total_enc_time = 0.0
    total_dec_time = 0.0
    total_tokens = 0
    seps = []
    # BURST once for the whole translation: the decoder runs hundreds of
    # single-token steps, which would otherwise execute at the idle clock.
    with _HtpBurst():
        for i, chunk in enumerate(chunks):
            if direction == "zh2en":
                part, enc_time, dec_time, n_tokens = _translate_chunk_code_switched(chunk, verbose)
            else:
                part, enc_time, dec_time, n_tokens = _translate_chunk_guarded(direction, chunk)
            total_enc_time += enc_time
            total_dec_time += dec_time
            total_tokens += n_tokens
            if len(chunks) > 1 and verbose:
                print(f"[OpusMT:{direction}]   [chunk {i+1}/{len(chunks)}] {chunk!r} -> {part!r}")
            if part:
                translated_parts.append(part)
                seps.append(chunks[i - 1].strip()[-1:] if i > 0 else "")

    if direction == "zh2en":
        translated = _join_translated_parts(translated_parts, seps).strip()
    else:
        translated = " ".join(translated_parts).strip()
    if direction == "en2zh":
        translated = _zh_postfix_en2zh(text, _clean_zh_text(translated))

    if verbose:
        print(f"[OpusMT:{direction}] Output: {translated}")
        print(f"[OpusMT:{direction}] Timing: Encoder {total_enc_time*1000:.0f}ms | "
              f"Decoder {total_dec_time*1000:.0f}ms ({total_tokens} tokens)")

    return translated


_OPUS_NO_REPEAT_NGRAM = 3


def _opus_banned_tokens(tokens: list[int], n: int) -> set[int]:
    """Tokens that would create an n-gram already present in `tokens`."""
    if n <= 0 or len(tokens) < n - 1:
        return set()
    prefix = tuple(tokens[len(tokens) - (n - 1):]) if n > 1 else ()
    banned = set()
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i:i + n - 1]) == prefix:
            banned.add(tokens[i + n - 1])
    return banned


_OPUS_BEAM = 4
_OPUS_LENGTH_PENALTY = 1.0


def _opus_beam_decode(decoder_model, dec_input_names, attention_mask, cross_key, cross_val,
                      bos: int, eos: int, max_new_tokens: int, beam: int = 4) -> list[int]:
    """Beam-search the KV-cached single-token OpusMT decoder (beam=1 == greedy).

    Each live hypothesis owns its own self-attention cache; the 'position'
    input is the shared step index. Same constraints as before: BOS/pad is
    never generated and no 3-gram repeats. Finished hypotheses are ranked by
    length-normalised log-probability, as in HF generate()."""
    n_layers = _OPUS_NUM_LAYERS
    shape = (1, _OPUS_NUM_HEADS, _OPUS_MAX_SEQ_LEN - 1, _OPUS_HEAD_DIM)
    hyps = [{"tokens": [], "score": 0.0,
             "pk": [np.zeros(shape, np.float32) for _ in range(n_layers)],
             "pv": [np.zeros(shape, np.float32) for _ in range(n_layers)]}]
    finished: list[tuple[float, list[int]]] = []
    out_names = decoder_model.getOutputName()

    def norm(score, n_tokens):
        return score / (max(1, n_tokens) ** _OPUS_LENGTH_PENALTY)

    for step in range(max_new_tokens):
        position = np.array([step], dtype=np.int32)
        cands = []                      # (score, hyp_index, token)
        presents = []
        for hi, h in enumerate(hyps):
            cur = np.array([[h["tokens"][-1] if h["tokens"] else bos]], dtype=np.int32)
            feed = {"input_ids": cur, "position": position,
                    "encoder_attention_mask": attention_mask}
            for i in range(n_layers):
                feed[f"block_{i}_past_self_key_states"] = h["pk"][i]
                feed[f"block_{i}_past_self_value_states"] = h["pv"][i]
                feed[f"block_{i}_cross_key_states"] = cross_key[i]
                feed[f"block_{i}_cross_value_states"] = cross_val[i]
            outs = decoder_model.Inference(*[feed[n] for n in dec_input_names])
            if not isinstance(outs, (list, tuple)):
                outs = [outs]
            od = dict(zip(out_names, outs))

            logits = np.array(od["logits"], dtype=np.float32)[0, 0].astype(np.float64)
            logits[bos] = -np.inf
            for banned in _opus_banned_tokens(h["tokens"], _OPUS_NO_REPEAT_NGRAM):
                logits[banned] = -np.inf
            m = logits.max()
            lp = logits - (m + np.log(np.exp(logits - m).sum()))
            k = min(beam * 2, lp.shape[0] - 1)
            for tid in np.argpartition(-lp, k)[:k]:
                if np.isfinite(lp[tid]):
                    cands.append((h["score"] + float(lp[tid]), hi, int(tid)))
            presents.append(([np.array(od[f"block_{i}_present_self_key_states"], dtype=np.float32)
                              for i in range(n_layers)],
                             [np.array(od[f"block_{i}_present_self_value_states"], dtype=np.float32)
                              for i in range(n_layers)]))

        cands.sort(key=lambda c: c[0], reverse=True)
        new_hyps, used_parent = [], set()
        for rank, (sc, hi, tid) in enumerate(cands[:2 * beam]):
            parent = hyps[hi]
            if tid == eos:
                if rank < beam:
                    finished.append((norm(sc, len(parent["tokens"]) + 1), list(parent["tokens"])))
                continue
            if len(new_hyps) >= beam:
                continue
            psk, psv = presents[hi]
            if hi in used_parent:               # second child of this parent -> own copy
                pk = [x.copy() for x in parent["pk"]]
                pv = [x.copy() for x in parent["pv"]]
            else:                               # first child re-uses the parent's buffers
                pk, pv = parent["pk"], parent["pv"]
                used_parent.add(hi)
            for i in range(n_layers):
                pk[i][:, :, step:step + 1, :] = psk[i]
                pv[i][:, :, step:step + 1, :] = psv[i]
            new_hyps.append({"tokens": parent["tokens"] + [tid], "score": sc, "pk": pk, "pv": pv})
        if not new_hyps:
            break
        hyps = new_hyps
        if len(finished) >= beam:
            best_alive = norm(max(h["score"] for h in hyps), step + 1)
            if max(f[0] for f in finished) >= best_alive:
                break

    if finished:
        return max(finished, key=lambda f: f[0])[1]
    best = max(hyps, key=lambda h: norm(h["score"], len(h["tokens"])))
    return best["tokens"]


def _opus_translate_chunk(direction: str, text: str, beam: int | None = None) -> tuple[str, float, float, int]:
    """Translate a single <=_OPUS_CHUNK_MAX_WORDS-unit chunk (beam=None uses
    _OPUS_BEAM). Returns (translated_text, encoder_seconds, decoder_seconds,
    generated_token_count). The caller holds the HTP BURST profile."""
    state = _opus_state[direction]
    encoder_model = state["encoder"]
    decoder_model = state["decoder"]
    tokenizer = state["tokenizer"]

    BOS_TOKEN_ID = tokenizer.pad_token_id
    EOS_TOKEN_ID = tokenizer.eos_token_id

    enc_inputs = tokenizer(
        text, return_tensors="np", padding="max_length",
        truncation=True, max_length=_OPUS_MAX_SEQ_LEN,
    )
    input_ids = enc_inputs["input_ids"].astype(np.int32)
    attention_mask = enc_inputs["attention_mask"].astype(np.int32)

    enc_input_names = encoder_model.getInputName()
    enc_name_to_tensor = {"input_ids": input_ids, "encoder_attention_mask": attention_mask}
    enc_in_list = [enc_name_to_tensor[n] for n in enc_input_names]

    t_enc = time.time()
    enc_outputs = encoder_model.Inference(*enc_in_list)
    enc_time = time.time() - t_enc

    if not isinstance(enc_outputs, (list, tuple)):
        enc_outputs = [enc_outputs]
    enc_out_names = encoder_model.getOutputName()
    enc_out_dict = dict(zip(enc_out_names, enc_outputs))

    dec_input_names = decoder_model.getInputName()
    cross_key = [np.array(enc_out_dict[f"block_{i}_cross_key_states"], dtype=np.float32) for i in range(_OPUS_NUM_LAYERS)]
    cross_val = [np.array(enc_out_dict[f"block_{i}_cross_value_states"], dtype=np.float32) for i in range(_OPUS_NUM_LAYERS)]

    # Translation length is roughly proportional to the source length; cap the
    # decode so a runaway loop cannot fill the whole 256-step budget.
    n_src_tokens = int(attention_mask.sum())
    max_new_tokens = min(_OPUS_MAX_SEQ_LEN - 2, max(16, 3 * n_src_tokens))

    t_dec = time.time()
    generated_tokens = _opus_beam_decode(
        decoder_model, dec_input_names, attention_mask, cross_key, cross_val,
        bos=BOS_TOKEN_ID, eos=EOS_TOKEN_ID, max_new_tokens=max_new_tokens,
        beam=beam if beam is not None else _OPUS_BEAM,
    )
    dec_time = time.time() - t_dec
    translated = tokenizer.decode(generated_tokens, skip_special_tokens=True).strip()
    return translated, enc_time, dec_time, len(generated_tokens)


def opus_release(direction: str) -> None:
    state = _opus_state[direction]
    if state.get("encoder") is not None:
        del state["encoder"]
    if state.get("decoder") is not None:
        del state["decoder"]
    state.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 -- PiperTTS-EN (English text-to-speech)
# Ported from samples/models/audio/audio_generation/pipertts_en/python/pipertts_en.py
# 4-stage pipeline: G2P (gruut) -> encoder -> SDP -> flow -> HiFi-GAN decoder.
# ═══════════════════════════════════════════════════════════════════════════════

PIPER_SAMPLE_RATE = 22050

_PIPER_ENC_TEXT_MAX = 512     # MAX_SEQ_LEN
_PIPER_FLOW_T_MEL = 1536      # UPSAMPLED_MAX_SEQ_LEN = MAX_SEQ_LEN * 3
_PIPER_FEAT_DIM = 192         # ENCODER_HIDDEN_DIM
_PIPER_UPSAMPLE_FACTOR = 256
_PIPER_DEC_SEQ_OVERLAP = 12
_PIPER_MAX_DEC_SEQ_LEN = 40
_PIPER_DEC_SEQ_LEN = _PIPER_MAX_DEC_SEQ_LEN + 2 * _PIPER_DEC_SEQ_OVERLAP  # 64

_PIPER_MODEL_URLS = {
    # AI Hub context binaries (.bin only); X Elite = 8380, X2 Elite = 8480.
    # Pinned to v0.62.2 (QAIRT 2.45, same SDK as the Zipformer / OpusMT
    # binaries): v0.63.0+ are compiled with QAIRT 2.50 and fail to load on the
    # QNN 2.48 runtime bundled with qai_appbuilder 2.48 ("Using newer context
    # binary on old SDK"). Model I/O signatures are identical across these
    # releases.
    _DEV_X: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/pipertts_en/releases/v0.62.1/"
        "pipertts_en-voice_ai-float-qualcomm_snapdragon_x_elite.zip"
    ),
    _DEV_X2: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/pipertts_en/releases/v0.62.1/"
        "pipertts_en-voice_ai-float-qualcomm_snapdragon_x2_elite.zip"
    ),
}
_PIPER_REQUIRED_MODELS = ["encoder.bin", "sdp.bin", "flow.bin", "decoder.bin"]

# Piper phoneme vocabulary (from the official piper-voices voice config).
# IPA char -> piper encoder input id. '_'=0(PAD), '^'=1(BOS), '$'=2(EOS), ' '=3(space)
PIPER_PHONEME_ID_MAP = {
    '_': 0, '^': 1, '$': 2, ' ': 3, '!': 4, "'": 5,
    '(': 6, ')': 7, ',': 8, '-': 9, '.': 10, ':': 11,
    ';': 12, '?': 13, 'a': 14, 'b': 15, 'c': 16, 'd': 17,
    'e': 18, 'f': 19, 'h': 20, 'i': 21, 'j': 22, 'k': 23,
    'l': 24, 'm': 25, 'n': 26, 'o': 27, 'p': 28, 'q': 29,
    'r': 30, 's': 31, 't': 32, 'u': 33, 'v': 34, 'w': 35,
    'x': 36, 'y': 37, 'z': 38, '\u00e6': 39, '\u00e7': 40, '\u00f0': 41,
    '\u00f8': 42, '\u0127': 43, '\u014b': 44, '\u0153': 45, '\u01c0': 46, '\u01c1': 47,
    '\u01c2': 48, '\u01c3': 49, '\u0250': 50, '\u0251': 51, '\u0252': 52, '\u0253': 53,
    '\u0254': 54, '\u0255': 55, '\u0256': 56, '\u0257': 57, '\u0258': 58, '\u0259': 59,
    '\u025a': 60, '\u025b': 61, '\u025c': 62, '\u025e': 63, '\u025f': 64, '\u0260': 65,
    '\u0261': 66, '\u0262': 67, '\u0263': 68, '\u0264': 69, '\u0265': 70, '\u0266': 71,
    '\u0267': 72, '\u0268': 73, '\u026a': 74, '\u026b': 75, '\u026c': 76, '\u026d': 77,
    '\u026e': 78, '\u026f': 79, '\u0270': 80, '\u0271': 81, '\u0272': 82, '\u0273': 83,
    '\u0274': 84, '\u0275': 85, '\u0276': 86, '\u0278': 87, '\u0279': 88, '\u027a': 89,
    '\u027b': 90, '\u027d': 91, '\u027e': 92, '\u0280': 93, '\u0281': 94, '\u0282': 95,
    '\u0283': 96, '\u0284': 97, '\u0288': 98, '\u0289': 99, '\u028a': 100, '\u028b': 101,
    '\u028c': 102, '\u028d': 103, '\u028e': 104, '\u028f': 105, '\u0290': 106, '\u0291': 107,
    '\u0292': 108, '\u0294': 109, '\u0295': 110, '\u0298': 111, '\u0299': 112, '\u029b': 113,
    '\u029c': 114, '\u029d': 115, '\u029f': 116, '\u02a1': 117, '\u02a2': 118, '\u02b2': 119,
    '\u02c8': 120, '\u02cc': 121, '\u02d0': 122, '\u02d1': 123, '\u02de': 124, '\u03b2': 125,
    '\u03b8': 126, '\u03c7': 127, '\u1d4b': 128, '\u2c71': 129, '0': 130, '1': 131,
    '2': 132, '3': 133, '4': 134, '5': 135, '6': 136, '7': 137,
    '8': 138, '9': 139, '\u0327': 140, '\u0303': 141, '\u032a': 142,
    '\u032f': 143, '\u0329': 144, '\u02b0': 145, '\u02e4': 146, '\u03b5': 147,
    '\u2193': 148, '#': 149, '"': 150, '\u2191': 151, '\u033a': 152, '\u033b': 153,
}
_PIPER_BOS_ID = PIPER_PHONEME_ID_MAP['^']
_PIPER_EOS_ID = PIPER_PHONEME_ID_MAP['$']

_PIPER_GRUUT_BOUNDARY_MAP = {
    '|': PIPER_PHONEME_ID_MAP[' '],
    '\u2016': PIPER_PHONEME_ID_MAP[' '],
}
_PIPER_GRUUT_SKIP_CHARS = {'\u0361'}
_PIPER_PUNCT_MAP = {
    ',': PIPER_PHONEME_ID_MAP[','],
    '.': PIPER_PHONEME_ID_MAP['.'],
    '?': PIPER_PHONEME_ID_MAP['?'],
    '!': PIPER_PHONEME_ID_MAP['!'],
    ':': PIPER_PHONEME_ID_MAP[':'],
    ';': PIPER_PHONEME_ID_MAP[';'],
}

_piper_state: dict[str, Any] = {"loaded": False}


def _piper_download(models_dir: Path) -> bool:
    """Make sure the PiperTTS-EN context binaries for the target chip are in
    models_dir. Extraction goes through a temporary folder + stamp (see
    _ensure_model), so an interrupted install never leaves a truncated .bin
    behind and binaries built for another chip are replaced."""
    try:
        model_dir = _ensure_model(models_dir, _PIPER_REQUIRED_MODELS, _PIPER_MODEL_URLS,
                                  "PiperTTS-EN", "PiperTTS-EN")
    except (RuntimeError, OSError) as e:
        print(f"[PiperTTS-EN][ERROR] {e}")
        return False
    if model_dir.resolve() != models_dir.resolve():
        # piper_init() loads <models_dir>/<name>.bin: flatten a manual install
        # that kept the zip's release sub-folder.
        for item in model_dir.iterdir():
            if item.is_file():
                os.replace(item, models_dir / item.name)
        top = models_dir / model_dir.resolve().relative_to(models_dir.resolve()).parts[0]
        shutil.rmtree(top, ignore_errors=True)
    missing = [m for m in _PIPER_REQUIRED_MODELS if not (models_dir / m).is_file()]
    if missing:
        print(f"[PiperTTS-EN][ERROR] Missing model files after extraction: {missing}")
        return False
    return True


def _piper_text_to_ids(text: str, lang: str = "en-us", verbose: bool = False) -> np.ndarray:
    """Text -> piper phoneme ids, matching rhasspy/piper-phonemize exactly
    (see pipertts_en.py docstring for the full rationale)."""
    import unicodedata
    import gruut

    PAD_ID = PIPER_PHONEME_ID_MAP['_']

    word_entries = []
    for sent in gruut.sentences(text, lang=lang):
        for word in sent:
            if word.phonemes:
                word_entries.append((word.text, word.phonemes, word.is_break))

    units = []
    skipped = []
    prev_was_word = False

    def _emit_codepoints(token):
        emitted = False
        for ch in unicodedata.normalize("NFD", token):
            if ch in _PIPER_GRUUT_SKIP_CHARS:
                continue
            if ch in PIPER_PHONEME_ID_MAP:
                units.append(PIPER_PHONEME_ID_MAP[ch])
                emitted = True
            else:
                skipped.append(repr(ch))
        return emitted

    for word_text, phonemes, is_break in word_entries:
        if is_break:
            if prev_was_word:
                units.append(PIPER_PHONEME_ID_MAP[' '])
            for ch in word_text:
                if ch in _PIPER_PUNCT_MAP:
                    units.append(_PIPER_PUNCT_MAP[ch])
            units.append(PIPER_PHONEME_ID_MAP[' '])
            prev_was_word = False
        else:
            if prev_was_word:
                units.append(PIPER_PHONEME_ID_MAP[' '])
            for p in phonemes:
                if p in _PIPER_GRUUT_BOUNDARY_MAP:
                    units.append(_PIPER_GRUUT_BOUNDARY_MAP[p])
                elif p in PIPER_PHONEME_ID_MAP:
                    units.append(PIPER_PHONEME_ID_MAP[p])
                else:
                    if not _emit_codepoints(p):
                        skipped.append(repr(p))
            prev_was_word = True

    while units and units[-1] == PIPER_PHONEME_ID_MAP[' ']:
        units.pop()

    ids = [_PIPER_BOS_ID, PAD_ID]
    for u in units:
        ids.append(u)
        ids.append(PAD_ID)
    ids.append(_PIPER_EOS_ID)

    if verbose and skipped:
        print(f"[PiperTTS-EN][G2P] Skipped unknown phoneme chars: {skipped}")

    return np.array(ids, dtype=np.int32)


def _piper_run_encoder(ctx, phoneme_ids: np.ndarray):
    n = len(phoneme_ids)
    x = np.zeros((1, _PIPER_ENC_TEXT_MAX), dtype=np.int32)
    x[0, :n] = phoneme_ids
    out = ctx["encoder"].Inference([x, np.array([n], dtype=np.int32)])
    _check_outputs(out, 4, "PiperTTS-EN encoder")
    return (np.array(out[0]), np.array(out[1]), np.array(out[2]), np.array(out[3]))


def _piper_run_sdp(ctx, x_encoded, x_mask, noise_scale_w=0.8, length_scale=1.0):
    out = ctx["sdp"].Inference([
        x_encoded, x_mask,
        np.array([noise_scale_w], dtype=np.float32),
        np.array([length_scale], dtype=np.float32),
    ])
    return np.array(out[0]), np.array(out[1])


def _piper_generate_path(w_ceil: np.ndarray, n_phones: int, y_len_sdp: int,
                         t_mel_max: int = _PIPER_FLOW_T_MEL,
                         t_text_max: int = _PIPER_ENC_TEXT_MAX):
    durs = np.clip(np.round(w_ceil[0, 0, :n_phones]).astype(np.int32), 0, None)
    y_len = min(int(y_len_sdp), t_mel_max)

    attn = np.zeros((t_mel_max, t_text_max), dtype=np.float32)
    t = 0
    for i, d in enumerate(durs):
        for _ in range(int(d)):
            if t >= y_len:
                break
            if i < t_text_max:
                attn[t, i] = 1.0
            t += 1
        if t >= y_len:
            break
    return attn[np.newaxis], y_len


def _piper_run_flow(ctx, attn_squeezed, m_p, logs_p, y_len, noise_scale=0.667):
    y_mask = np.zeros((1, 1, _PIPER_FLOW_T_MEL), dtype=np.float32)
    y_mask[0, 0, :y_len] = 1.0
    out = ctx["flow"].Inference([
        attn_squeezed, m_p, logs_p,
        np.array([noise_scale], dtype=np.float32),
        y_mask,
    ])
    return np.array(out[0])


def _piper_run_decoder(ctx, z: np.ndarray, y_len: int) -> np.ndarray:
    audio_chunks = []

    z_buf = np.zeros((1, _PIPER_FEAT_DIM, _PIPER_DEC_SEQ_LEN), dtype=np.float32)
    first_end = min(_PIPER_MAX_DEC_SEQ_LEN + _PIPER_DEC_SEQ_OVERLAP, z.shape[2])
    z_buf[0, :, :first_end] = z[0, :, :first_end]
    out = ctx["decoder"].Inference([z_buf])
    audio_chunk = np.array(out[0])[0, 0]
    audio_chunks.append(audio_chunk[:_PIPER_MAX_DEC_SEQ_LEN * _PIPER_UPSAMPLE_FACTOR])

    total_dec_seq_len = _PIPER_MAX_DEC_SEQ_LEN
    while total_dec_seq_len < y_len:
        s = total_dec_seq_len - _PIPER_DEC_SEQ_OVERLAP
        e = total_dec_seq_len + _PIPER_MAX_DEC_SEQ_LEN + _PIPER_DEC_SEQ_OVERLAP
        z_slice = z[:, :, s:min(e, z.shape[2])]
        if z_slice.shape[2] < _PIPER_DEC_SEQ_LEN:
            z_slice = np.pad(z_slice, ((0, 0), (0, 0), (0, _PIPER_DEC_SEQ_LEN - z_slice.shape[2])))
        out = ctx["decoder"].Inference([z_slice])
        audio_chunk = np.array(out[0])[0, 0]
        remaining = y_len - total_dec_seq_len
        actual_frames = min(_PIPER_MAX_DEC_SEQ_LEN, remaining)
        start_s = _PIPER_DEC_SEQ_OVERLAP * _PIPER_UPSAMPLE_FACTOR
        end_s = (_PIPER_DEC_SEQ_OVERLAP + actual_frames) * _PIPER_UPSAMPLE_FACTOR
        audio_chunks.append(audio_chunk[start_s:end_s])
        total_dec_seq_len += actual_frames

    if not audio_chunks:
        return np.zeros(_PIPER_MAX_DEC_SEQ_LEN * _PIPER_UPSAMPLE_FACTOR, dtype=np.float32)

    audio = np.concatenate(audio_chunks)
    return audio[:y_len * _PIPER_UPSAMPLE_FACTOR]


def piper_init() -> None:
    """Download (if needed) and load the 4 PiperTTS-EN QNN context binaries."""
    if _piper_state.get("loaded"):
        return
    print("[PiperTTS-EN] Checking / downloading models ...")
    ok = _piper_download(_PIPERTTS_EN_DIR)
    if not ok:
        raise RuntimeError("PiperTTS-EN model download failed.")

    _ensure_qnn_config()
    ctx = {}
    names = ["encoder", "sdp", "flow", "decoder"]
    t0 = time.perf_counter()
    for name in names:
        path = _PIPERTTS_EN_DIR / f"{name}.bin"
        if not path.is_file():
            raise FileNotFoundError(f"PiperTTS-EN model file not found: {path}")
        _check_context_sdk(path, "PiperTTS-EN")      # before any context is created
    for name in names:
        path = _PIPERTTS_EN_DIR / f"{name}.bin"
        ctx[name] = QNNContext(model_name=name, model_path=str(path))
    print(f"[PiperTTS-EN] Loaded 4 QNN context binaries in {time.perf_counter() - t0:.2f}s")

    _piper_state.update(ctx=ctx, loaded=True)


# Max words per PiperTTS-EN synthesis chunk. ENC_TEXT_MAX=512 piper ids is
# the hard limit (phoneme_ids beyond it raise ValueError when written into
# the fixed-size encoder input array -- see _piper_run_encoder); piper's id
# assembly is BOS+PAD + (phoneme,PAD)*N + EOS, i.e. roughly 2 ids per
# phoneme, and English words run ~15-30 phonemes/word including the
# inter-word space. 12 words/chunk measured at ~210-330 ids worst case,
# leaving comfortable headroom under 512.
_PIPER_CHUNK_MAX_WORDS = 12
_PIPER_CHUNK_SILENCE_S = 0.15   # inserted between concatenated chunks


def _piper_prepare_chunks(text: str, verbose: bool = False) -> list[tuple[str, np.ndarray]]:
    """Split `text` into chunks and convert each to phoneme ids, re-splitting
    any chunk whose id sequence would overflow the fixed 512-slot encoder
    input (long words / numbers can do that even under the word limit)."""
    return _piper_prepare_chunks_from(
        _split_text_for_model(text, lambda s: len(s.split()), _PIPER_CHUNK_MAX_WORDS), verbose)


def _piper_prepare_chunks_from(pending: list[str], verbose: bool = False) -> list[tuple[str, np.ndarray]]:
    """Phonemize pre-split text chunks (see _piper_prepare_chunks)."""
    def measure(s: str) -> int:
        return len(s.split())

    pending = list(pending)
    out: list[tuple[str, np.ndarray]] = []
    while pending:
        chunk = pending.pop(0)
        ids = _piper_text_to_ids(chunk, verbose=verbose)
        if len(ids) <= _PIPER_ENC_TEXT_MAX:
            out.append((chunk, ids))
            continue
        n_words = measure(chunk)
        halves = _split_text_for_model(chunk, measure, max(1, n_words // 2)) if n_words > 1 else []
        if len(halves) >= 2:
            pending = halves + pending
            continue
        print(f"[PiperTTS-EN][WARN] {len(ids)} phoneme ids exceed the {_PIPER_ENC_TEXT_MAX}-slot "
              f"window and cannot be split further; truncating: {chunk[:60]!r}")
        ids = np.concatenate([ids[:_PIPER_ENC_TEXT_MAX - 1],
                              np.array([_PIPER_EOS_ID], dtype=np.int32)]).astype(np.int32)
        out.append((chunk, ids))
    return out


def piper_synthesize(text: str, noise_scale: float = 0.667, noise_scale_w: float = 0.8,
                     length_scale: float = 1.0, verbose: bool = False) -> np.ndarray:
    """English text -> float32 mono waveform at PIPER_SAMPLE_RATE (22050 Hz).

    Long text is split into <=_PIPER_CHUNK_MAX_WORDS-word chunks (see
    _split_text_for_model, _piper_prepare_chunks) and each chunk is
    synthesized independently; the resulting waveforms are concatenated with
    a short silence gap. Without this, text whose phoneme-id count exceeds
    the model's fixed 512-slot encoder window would fail.
    """
    piper_init()
    ctx = _piper_state["ctx"]

    # gruut spells ALL-CAPS words letter by letter ("TODAY" -> T-O-D-A-Y):
    # give them normal casing first, keeping real acronyms (AI, NPU, ...).
    tts_text = _en_fix_caps(text)
    prepared = _piper_prepare_chunks(tts_text, verbose=verbose)
    if not prepared:
        return np.zeros(0, dtype=np.float32)

    print(f"\n[PiperTTS-EN] '{text}'")
    if tts_text != text:
        print(f"[PiperTTS-EN] Case-fixed for G2P: '{tts_text}'")
    if len(prepared) > 1:
        print(f"[PiperTTS-EN] Split into {len(prepared)} chunk(s) "
              f"(<= {_PIPER_CHUNK_MAX_WORDS} words, <= {_PIPER_ENC_TEXT_MAX} phoneme ids each).")
    t0 = time.perf_counter()

    silence = np.zeros(int(_PIPER_CHUNK_SILENCE_S * PIPER_SAMPLE_RATE), dtype=np.float32)
    audio_parts = []
    with _HtpBurst():
        pending = list(prepared)
        n_done = 0
        while pending:
            chunk, phoneme_ids = pending.pop(0)
            m_p, logs_p, x_encoded, x_mask = _piper_run_encoder(ctx, phoneme_ids)
            y_lengths, w_ceil = _piper_run_sdp(ctx, x_encoded, x_mask,
                                               noise_scale_w=noise_scale_w, length_scale=length_scale)
            y_len_sdp = int(float(y_lengths[0]))
            if y_len_sdp > _PIPER_FLOW_T_MEL:
                # The flow/decoder only cover 1536 frames (~17.8 s); anything
                # past that would be silently cut. Re-split and retry.
                n_words = len(chunk.split())
                halves = (_piper_prepare_chunks_from(
                              _split_text_for_model(chunk, lambda s: len(s.split()), max(1, n_words // 2)),
                              verbose=verbose)
                          if n_words > 1 else [])
                if len(halves) >= 2:
                    print(f"[PiperTTS-EN][WARN] {y_len_sdp} frames exceed the {_PIPER_FLOW_T_MEL}-frame "
                          f"window; re-splitting {chunk[:60]!r}")
                    pending = halves + pending
                    continue
                print(f"[PiperTTS-EN][WARN] {y_len_sdp} frames exceed the {_PIPER_FLOW_T_MEL}-frame "
                      f"window and cannot be split further; the tail of {chunk[:60]!r} will be cut off.")
            n_done += 1

            attn_squeezed, y_len = _piper_generate_path(w_ceil, len(phoneme_ids), y_len_sdp=y_len_sdp)
            z = _piper_run_flow(ctx, attn_squeezed, m_p, logs_p, y_len, noise_scale=noise_scale)
            chunk_audio = _piper_run_decoder(ctx, z, y_len)
            if len(prepared) > 1 or n_done > 1 or pending:
                print(f"[PiperTTS-EN]   [chunk {n_done}] {chunk!r} "
                      f"-> {len(chunk_audio)/PIPER_SAMPLE_RATE:.2f}s")
            if audio_parts:
                audio_parts.append(silence)
            audio_parts.append(chunk_audio)

    audio = np.concatenate(audio_parts) if audio_parts else np.zeros(0, dtype=np.float32)

    dur = len(audio) / PIPER_SAMPLE_RATE
    total = time.perf_counter() - t0
    rtf = total / max(dur, 1e-6)
    print(f"[PiperTTS-EN] {dur:.2f}s audio synthesized in {total:.2f}s (RTF={rtf:.2f})")
    return audio.astype(np.float32)


def piper_release() -> None:
    if not _piper_state.get("loaded"):
        return
    _piper_state.pop("ctx", None)
    _piper_state["loaded"] = False

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 -- MeloTTS-ZH (Chinese text-to-speech)
# Ported from samples/models/audio/audio_generation/melotts_zh/python/melotts_zh.py
# 3-stage NPU pipeline (encoder -> flow -> HiFi-GAN decoder); text
# preprocessing (G2P + BERT features) runs on CPU via the `melo` package.
#
# `melo`'s japanese.py hard-imports MeCab at module load time, which has
# no ARM64 wheel; the stubs below (torchaudio/numba/soxr/boto3/korean) and
# the download_utils monkeypatch mirror melotts_zh.py exactly so melo can
# be imported without any of its optional native dependencies. These run
# lazily inside melotts_init() so a zh2en-only run never needs `melo`,
# `torch`, or any of its heavy deps.
# ═══════════════════════════════════════════════════════════════════════════════

MELO_SAMPLE_RATE = 44100

_MELO_MAX_SEQ_LEN = 512
_MELO_UPSAMPLED_MAX_SEQ_LEN = 1536
_MELO_MAX_DEC_SEQ_LEN = 64
_MELO_UPSAMPLE_FACTOR = 512

# Max chars per MeloTTS-ZH synthesis chunk. Two fixed windows apply: the
# encoder's _MELO_MAX_SEQ_LEN=512 phone slots (~4 phones/char) and the
# flow/decoder's _MELO_UPSAMPLED_MAX_SEQ_LEN=1536 frames (1536*512/44100
# ~= 17.8 s of audio). At ~4-5 spoken chars/s, 80 chars could exceed the
# frame window and be cut silently, so chunks are kept to 50 chars (~11 s,
# ~200 phones). melotts_synthesize() still re-splits any chunk whose
# predicted frame count exceeds 1536 (e.g. slow length_scale).
_MELO_CHUNK_MAX_CHARS = 50
_MELO_CHUNK_SILENCE_S = 0.15   # inserted between concatenated chunks

_MELO_MODEL_URLS = {
    # AI Hub context binaries (.bin only); X Elite = 8380, X2 Elite = 8480
    _DEV_X: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/melotts_zh/releases/v0.62.1/"
        "melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip"
    ),
    _DEV_X2: (
        "https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
        "models/melotts_zh/releases/v0.62.1/"
        "melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip"
    ),
}
# Pinned HuggingFace revision (not the moving `main`).
_MELO_CONFIG_URL = ("https://huggingface.co/myshell-ai/MeloTTS-Chinese/resolve/"
                    "af5d207a364ea4208c6f589c89f57f88414bdd16/config.json")
_MELO_REQUIRED_MODELS = ["encoder.bin", "flow.bin", "decoder.bin"]

# Quantization parameters of the uint16 flow/decoder binaries. They are baked
# into the compiled graphs: if a different release of the model is used, they
# must be re-read from the new binaries (see the I/O encodings of the context).
_MELO_FLOW_IN_QUANT = {
    "attn_squeezed": (0.000030518509447574615, 32768),
    "logs_p": (0.00003420173015911132, 22619),
    "noise_scale": (0.000010177767762797885, 0),
    "m_p": (0.00007962718518683687, 34950),
    "y_mask": (0.000015259021893143654, 0),
    "g": (0.000020079150999663398, 36547),
}
_MELO_FLOW_OUT_QUANT = {"z": (0.00029126188019290566, 32329)}
_MELO_DEC_IN_QUANT = {
    "g": (0.000020079150999663398, 36547),
    "z": (0.0002923276915680617, 32350),
}
_MELO_DEC_OUT_QUANT = {"audio": (0.000013070309250906575, 35961)}

_melo_state: dict[str, Any] = {"loaded": False}


def _melo_quantize(x: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """float32 -> uint16 (QNN 'ufp16' native dtype)"""
    q = np.round(x.astype(np.float32) / scale) + zero_point
    return np.clip(q, 0, 65535).astype(np.uint16)


def _melo_dequantize(raw_u16: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """QNN native 'ufp16' output (uint16) -> float32.
    .view(uint16) keeps this working with older qai_appbuilder builds that
    exposed 16-bit quantized outputs as float16 (same bits)."""
    a = np.ascontiguousarray(raw_u16)
    if a.dtype == np.uint16:
        q = a
    elif a.dtype == np.float16:
        q = a.view(np.uint16)          # same bits, older qai_appbuilder builds
    else:
        raise RuntimeError(
            f"Unexpected MeloTTS native output dtype {a.dtype}; expected uint16 "
            f"(or float16 carrying uint16 bits). The quantization constants may not "
            f"match this model release.")
    return (q.astype(np.float32) - zero_point) * scale


def _module_importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _melo_install_native_stubs():
    """Stub torchaudio/numba/soxr/boto3/melo.text.korean -- same rationale
    as melotts_zh.py §1: none has an ARM64 wheel / is ever exercised by the
    ZH TTS path, but `melo`'s import chain references them at load time.
    A stub is installed only when the real package cannot be imported, so a
    working torchaudio/numba/soxr elsewhere in the process is never shadowed."""
    import importlib.util

    if not _module_importable("torchaudio"):
        _ta = types.ModuleType("torchaudio")
        _ta.__spec__ = importlib.util.spec_from_loader("torchaudio", loader=None)
        _ta.__version__ = "0.0.0"
        sys.modules["torchaudio"] = _ta

    if not _module_importable("numba"):
        _numba = types.ModuleType("numba")

        def _jit(*a, **kw):
            def _dec(fn):
                return fn
            return _dec if not (len(a) == 1 and callable(a[0])) else a[0]

        class _NumbaType:
            def __getitem__(self, _key):
                return self

            def __call__(self, *a, **kw):
                return self

        _nb_type = _NumbaType()
        _numba.jit = _jit
        _numba.void = _nb_type
        _numba.int32 = _numba.int64 = _numba.float32 = _numba.float64 = _nb_type
        sys.modules["numba"] = _numba

    if not _module_importable("soxr"):
        _soxr = types.ModuleType("soxr")
        _soxr.__version__ = "0.0.0"

        def _soxr_resample(*a, **kw):
            raise RuntimeError("soxr stub -- audio resampling not available")

        _soxr.resample = _soxr_resample
        _soxr.VHQ = _soxr.HQ = _soxr.MQ = _soxr.LQ = _soxr.QQ = "HQ"
        sys.modules["soxr"] = _soxr

    if not _module_importable("boto3"):
        _b = types.ModuleType("boto3")
        _b.session = types.ModuleType("boto3.session")
        _b.dynamodb = types.ModuleType("boto3.dynamodb")
        sys.modules["boto3"] = _b
        sys.modules["boto3.session"] = _b.session
        sys.modules["boto3.dynamodb"] = _b.dynamodb

    _ko = types.ModuleType("melo.text.korean")

    def _ko_get_bert_feature(*a, **kw):
        raise RuntimeError("Korean TTS not supported")

    _ko.get_bert_feature = _ko_get_bert_feature
    sys.modules["melo.text.korean"] = _ko


def _melo_download(work_dir: Path):
    """Detect the chip, download + extract the AI Hub ZIP (.bin files only),
    and fetch the melo hparams config. Returns (model_dir, config_json_path)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    config_json = work_dir / "melo_config_zh.json"
    model_dir = _ensure_model(work_dir, _MELO_REQUIRED_MODELS, _MELO_MODEL_URLS,
                              "MeloTTS-ZH", "MeloTTS-ZH")

    if not config_json.is_file():
        print(f"[MeloTTS-ZH] Downloading melo hparams config: {_MELO_CONFIG_URL}")
        ret = download_url(
            _MELO_CONFIG_URL, str(config_json),
            desc="Downloading MeloTTS-Chinese hparams config...",
            fail=f"\nFailed to download melo config from:\n  {_MELO_CONFIG_URL}\n"
                 f"Please download manually and save it to: {config_json}",
        )
        if not ret or not config_json.is_file():
            raise RuntimeError(f"melo config download failed from {_MELO_CONFIG_URL}")

    return model_dir, config_json


class _MeloEncoder(QNNContext):
    """float32 I/O"""

    def Inference(self, sid, bert, ja_bert, x, tone, language,
                  x_lengths, noise_scale_w, sdp_ratio, length_scale):
        out = super().Inference([sid, bert, ja_bert, x, tone, language,
                                 x_lengths, noise_scale_w, sdp_ratio, length_scale])
        return [np.array(o) for o in out]


class _MeloFlow(QNNContext):
    """uint16 quantized I/O (QNN reports ufp16, bits are actually uint16)"""

    def Inference(self, attn_sq_f, logs_p_f, noise_scale_f, m_p_f, y_mask_f, g_f):
        inp = [
            _melo_quantize(attn_sq_f, *_MELO_FLOW_IN_QUANT["attn_squeezed"]),
            _melo_quantize(logs_p_f, *_MELO_FLOW_IN_QUANT["logs_p"]),
            _melo_quantize(noise_scale_f, *_MELO_FLOW_IN_QUANT["noise_scale"]),
            _melo_quantize(m_p_f, *_MELO_FLOW_IN_QUANT["m_p"]),
            _melo_quantize(y_mask_f, *_MELO_FLOW_IN_QUANT["y_mask"]),
            _melo_quantize(g_f, *_MELO_FLOW_IN_QUANT["g"]),
        ]
        out = super().Inference(inp)
        return _melo_dequantize(np.array(out[0]), *_MELO_FLOW_OUT_QUANT["z"])


class _MeloDecoder(QNNContext):
    """uint16 quantized I/O"""

    def Inference(self, g_f, z_f):
        inp = [
            _melo_quantize(g_f, *_MELO_DEC_IN_QUANT["g"]),
            _melo_quantize(z_f, *_MELO_DEC_IN_QUANT["z"]),
        ]
        out = super().Inference(inp)
        return _melo_dequantize(np.array(out[0]), *_MELO_DEC_OUT_QUANT["audio"])


def _melo_generate_path(duration, mask):
    import torch
    import torch.nn.functional as F

    b, _, t_y, t_x = mask.shape
    cum_duration = torch.cumsum(duration, -1)
    cum_flat = cum_duration.view(b * t_x)
    x_range = torch.arange(t_y, dtype=cum_flat.dtype)
    path = (x_range.unsqueeze(0) < cum_flat.unsqueeze(1)).to(mask.dtype)
    path = path.view(b, t_x, t_y)
    path = path - F.pad(path, [0, 0, 1, 0, 0, 0])[:, :-1]
    return path.unsqueeze(1).transpose(2, 3) * mask


def melotts_init() -> None:
    """Download (if needed) and load the MeloTTS-ZH text frontend + 3 QNN
    context binaries. Heavy/optional deps (torch, melo) are imported here,
    not at module scope, so a zh2en-only run never needs them."""
    if _melo_state.get("loaded"):
        return

    print("[MeloTTS-ZH] Checking / downloading models ...")
    model_dir, config_json = _melo_download(_MELOTTS_ZH_DIR)

    _melo_install_native_stubs()

    _dl = types.ModuleType("melo.download_utils")

    def _load_or_download_config(locale):
        from melo import utils as _mu
        return _mu.get_hparams_from_file(str(config_json))

    def _load_or_download_model(locale, device):
        # The QNN encoder.bin already contains all trained weights; the CPU
        # torch model is only kept alive so get_text_for_tts_infer() can
        # access tts_obj.hps / tts_obj.symbol_to_id.
        return {"model": {}}

    _dl.load_or_download_config = _load_or_download_config
    _dl.load_or_download_model = _load_or_download_model
    sys.modules["melo.download_utils"] = _dl

    print("[MeloTTS-ZH] Loading MeloTTS text frontend (G2P + BERT)...")
    from melo.api import TTS as MeloTTS
    import melo.models as _melo_models

    _orig_load_sd = _melo_models.SynthesizerTrn.load_state_dict

    def _lax_load_sd(self, state_dict, strict=True, **kw):
        return _orig_load_sd(self, state_dict, strict=False, **kw)

    # The CPU torch model is only a shell (weights live in the QNN binaries),
    # so loading it with strict=False is expected -- but scope that patch to
    # this one construction instead of leaving it installed for the process.
    _melo_models.SynthesizerTrn.load_state_dict = _lax_load_sd
    try:
        tts_obj = MeloTTS("ZH", device="cpu")
    finally:
        _melo_models.SynthesizerTrn.load_state_dict = _orig_load_sd
    speaker_id = next(iter(tts_obj.hps.data.spk2id.values()))

    _ensure_qnn_config()
    t0 = time.perf_counter()
    encoder_model = _MeloEncoder(
        "melotts_encoder", str(model_dir / "encoder.bin"),
        input_data_type="float", output_data_type="float",
    )
    flow_model = _MeloFlow(
        "melotts_flow", str(model_dir / "flow.bin"),
        input_data_type="native", output_data_type="native",
    )
    decoder_model = _MeloDecoder(
        "melotts_decoder", str(model_dir / "decoder.bin"),
        input_data_type="native", output_data_type="native",
    )
    print(f"[MeloTTS-ZH] Loaded 3 QNN context binaries in {time.perf_counter() - t0:.2f}s")

    _melo_state.update(
        tts_obj=tts_obj, speaker_id=speaker_id,
        encoder=encoder_model, flow=flow_model, decoder=decoder_model,
        loaded=True,
    )


def _melo_preprocess_text(text: str):
    import torch.nn.functional as F

    tts_obj = _melo_state["tts_obj"]
    from melo.utils import get_text_for_tts_infer
    bert_feat, ja_bert_feat, phones, tones, lang_ids = get_text_for_tts_infer(
        text, tts_obj.language, tts_obj.hps, "cpu", tts_obj.symbol_to_id
    )
    phone_len = phones.size(0)
    if phone_len > _MELO_MAX_SEQ_LEN:
        print(f"[MeloTTS-ZH][WARN] {phone_len} phones exceed the {_MELO_MAX_SEQ_LEN}-slot "
              f"window; the tail of {text[:40]!r}... will be cut off.")

    def pad2d(t, n):
        c = t.size(1)
        return F.pad(t, (0, max(0, n - c)))[:, :n]

    def pad1d(t, n):
        c = t.size(0)
        return F.pad(t, (0, max(0, n - c)))[:n]

    phones = pad1d(phones, _MELO_MAX_SEQ_LEN)
    tones = pad1d(tones, _MELO_MAX_SEQ_LEN)
    lang_ids = pad1d(lang_ids, _MELO_MAX_SEQ_LEN)
    bert_feat = pad2d(bert_feat, _MELO_MAX_SEQ_LEN)
    ja_bert_feat = pad2d(ja_bert_feat, _MELO_MAX_SEQ_LEN)

    return phones, tones, lang_ids, bert_feat, ja_bert_feat, min(phone_len, _MELO_MAX_SEQ_LEN)


def melotts_synthesize(text: str, noise_scale: float = 0.667, length_scale: float = 1.0,
                       noise_scale_w: float = 0.8, sdp_ratio: float = 0.2) -> np.ndarray:
    """Chinese text -> float32 mono waveform at MELO_SAMPLE_RATE (44100 Hz).

    Long text is split into <=_MELO_CHUNK_MAX_CHARS-char chunks (see
    _split_text_for_model) and each chunk is synthesized independently;
    the resulting waveforms are concatenated with a short silence gap.
    Without this, a phone sequence longer than the model's fixed 512-slot
    window is silently truncated (pad1d/pad2d's ``[:n]``) with no error at
    all, so the audio just stops partway through (observed on a 30s audio
    clip's full transcript, which produced a 413-phone sequence from a
    single untranslated chunk already close to the limit, and would exceed
    it outright on a longer one).
    """
    import torch

    melotts_init()
    encoder_model = _melo_state["encoder"]
    flow_model = _melo_state["flow"]
    decoder_model = _melo_state["decoder"]
    speaker_id = _melo_state["speaker_id"]

    chunks = _split_text_for_model(text, len, _MELO_CHUNK_MAX_CHARS)
    if not chunks:
        return np.zeros(0, dtype=np.float32)

    print(f"\n[MeloTTS-ZH] '{text}'")
    if len(chunks) > 1:
        print(f"[MeloTTS-ZH] Split into {len(chunks)} chunk(s) "
              f"(<= {_MELO_CHUNK_MAX_CHARS} chars each) to stay under the 512-slot phone window.")
    t0 = time.perf_counter()

    silence = np.zeros(int(_MELO_CHUNK_SILENCE_S * MELO_SAMPLE_RATE), dtype=np.float32)
    audio_parts = []

    # Raise HTP to BURST ONCE and hold it across every chunk's NPU stages
    # (encoder -> flow -> decoder); do not Set/Rel per stage or per chunk
    # (ramps the HTP clock up/down, adding latency).
    with _HtpBurst():
        pending = list(chunks)
        n_done = 0
        while pending:
            chunk = pending.pop(0)
            phones, tones, lang_ids, bert_feat, ja_bert_feat, phone_len = _melo_preprocess_text(chunk)

            sid = np.array([speaker_id], dtype=np.int32)
            bert = bert_feat.unsqueeze(0).numpy().astype(np.float32)
            ja_bert = ja_bert_feat.unsqueeze(0).numpy().astype(np.float32)
            x = phones.unsqueeze(0).numpy().astype(np.int32)
            tone = tones.unsqueeze(0).numpy().astype(np.int32)
            language = lang_ids.unsqueeze(0).numpy().astype(np.int32)
            x_lengths = np.array([phone_len], dtype=np.int32)
            ns_w = np.array([noise_scale_w], dtype=np.float32)
            sdp_r = np.array([sdp_ratio], dtype=np.float32)
            len_scale = np.array([length_scale], dtype=np.float32)

            enc_out = encoder_model.Inference(
                sid, bert, ja_bert, x, tone, language, x_lengths, ns_w, sdp_r, len_scale
            )
            m_p, logs_p, w_ceil, y_lengths, x_mask, g = enc_out
            y_len_int = int(y_lengths[0])
            if y_len_int > _MELO_UPSAMPLED_MAX_SEQ_LEN:
                # The flow/decoder only cover 1536 frames (~17.8 s); anything
                # past that would be silently cut. Re-split and retry.
                halves = _split_text_for_model(chunk, len, max(1, len(chunk) // 2)) if len(chunk) > 1 else []
                if len(halves) >= 2:
                    print(f"[MeloTTS-ZH][WARN] {y_len_int} frames exceed the "
                          f"{_MELO_UPSAMPLED_MAX_SEQ_LEN}-frame window; re-splitting {chunk[:40]!r}")
                    pending = halves + pending
                    continue
                print(f"[MeloTTS-ZH][WARN] {y_len_int} frames exceed the "
                      f"{_MELO_UPSAMPLED_MAX_SEQ_LEN}-frame window and cannot be split further; "
                      f"the tail of {chunk[:40]!r} will be cut off.")
                y_len_int = _MELO_UPSAMPLED_MAX_SEQ_LEN
            n_done += 1
            y_mask_np = (
                torch.unsqueeze(
                    torch.arange(_MELO_UPSAMPLED_MAX_SEQ_LEN) < torch.tensor([y_len_int]).unsqueeze(-1),
                    dim=1,
                ).float().numpy()
            )
            attn_mask = torch.from_numpy(x_mask).unsqueeze(2) * torch.from_numpy(y_mask_np).unsqueeze(-1)
            attn = _melo_generate_path(torch.from_numpy(w_ceil), attn_mask)
            attn_squeezed = attn.squeeze(1).float().numpy()
            noise_scale_arr = np.array([noise_scale], dtype=np.float32)

            z = flow_model.Inference(attn_squeezed, logs_p, noise_scale_arr, m_p, y_mask_np, g)

            dec_chunks = []
            # z is padded to the fixed 1536 frames, but only the first y_len
            # frames carry speech (the audio is trimmed to y_len below): run
            # the HiFi-GAN decoder only over those.
            z_total = z.shape[2]
            n_frames = min(z_total, y_len_int)
            for start in range(0, n_frames, _MELO_MAX_DEC_SEQ_LEN):
                end = start + _MELO_MAX_DEC_SEQ_LEN
                if end > z_total:
                    z_slice = np.zeros([1, z.shape[1], _MELO_MAX_DEC_SEQ_LEN], dtype=np.float32)
                    z_slice[:, :, : z_total - start] = z[:, :, start:z_total]
                else:
                    z_slice = z[:, :, start:end]
                dec_chunk = decoder_model.Inference(g, z_slice)
                dec_chunks.append(dec_chunk.flatten())

            chunk_audio = np.concatenate(dec_chunks)
            chunk_audio = chunk_audio[: y_len_int * _MELO_UPSAMPLE_FACTOR]
            if len(chunks) > 1 or n_done > 1 or pending:
                print(f"[MeloTTS-ZH]   [chunk {n_done}] {chunk!r} "
                      f"-> {len(chunk_audio)/MELO_SAMPLE_RATE:.2f}s")
            if audio_parts:
                audio_parts.append(silence)
            audio_parts.append(chunk_audio)

    audio = np.concatenate(audio_parts) if audio_parts else np.zeros(0, dtype=np.float32)
    audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)

    peak = np.abs(audio).max()
    if peak > 1.0:
        audio = audio / peak * 0.9

    dur = len(audio) / MELO_SAMPLE_RATE
    print(f"[MeloTTS-ZH] {dur:.2f}s audio synthesized in {time.perf_counter() - t0:.2f}s")
    return audio.astype(np.float32)


def melotts_release() -> None:
    if not _melo_state.get("loaded"):
        return
    for key in ("encoder", "flow", "decoder"):
        obj = _melo_state.pop(key, None)
        if obj is not None:
            del obj
    _melo_state.pop("tts_obj", None)
    _melo_state["loaded"] = False

# SECTION 5 -- Audio I/O: microphone capture / WAV file I/O / playback
# Uses sounddevice (PortAudio bindings) for capture + playback, and
# soundfile for WAV read/write. Both are CPU-only, no NPU involved.

class _EnergyEndpointer:
    """Streaming energy-based start/end-of-speech detector for mic capture.

    Keeps a rolling pre-roll so the first syllables (spoken before the
    detector fires) are not lost, calibrates a noise floor from the first
    ~0.3 s, and reports end-of-utterance after `end_silence_s` of quiet
    following speech. Pure numpy; feed() takes consecutive mono float32
    blocks at `sr` and returns True once the utterance is complete."""

    def __init__(self, sr, block_s=0.03, preroll_s=0.5, end_silence_s=1.0,
                 min_speech_s=0.25, min_thresh=0.006, snr=3.0, calib_s=0.3,
                 max_thresh=0.02):
        self.sr = sr
        self.block_s = block_s
        self.preroll_blocks = max(1, int(round(preroll_s / block_s)))
        self.end_blocks = max(1, int(round(end_silence_s / block_s)))
        self.min_speech_blocks = max(1, int(round(min_speech_s / block_s)))
        self.min_thresh = min_thresh
        # Upper bound on the speech threshold: if the user is already talking
        # during calibration, the "noise floor" is really speech and an
        # uncapped threshold would never fire (or end the utterance at once).
        self.max_thresh = max(min_thresh, max_thresh)
        self.snr = snr
        self.calib_blocks = max(1, int(round(calib_s / block_s)))
        self._noise = []
        self._pre = []
        self._blocks = []
        self.started = False
        self._speech_blocks = 0
        self._silence_run = 0

    @staticmethod
    def _rms(x):
        return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0

    def threshold(self):
        # Low percentile, not median: robust to speech that starts partway
        # through the calibration window (the quiet blocks still win).
        floor = float(np.percentile(self._noise, 20)) if self._noise else 0.0
        return min(self.max_thresh, max(self.min_thresh, floor * self.snr))

    def feed(self, block) -> bool:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        r = self._rms(block)
        if not self.started and len(self._noise) < self.calib_blocks:
            self._noise.append(r)
            self._pre.append(block)
            self._pre = self._pre[-self.preroll_blocks:]
            return False
        loud = r >= self.threshold()
        if not self.started:
            self._pre.append(block)
            self._pre = self._pre[-self.preroll_blocks:]
            if loud:
                self._speech_blocks += 1
                if self._speech_blocks >= 2:          # 2 consecutive-ish loud blocks
                    self.started = True
                    self._blocks = list(self._pre)
                    self._silence_run = 0
            else:
                self._speech_blocks = 0
            return False
        self._blocks.append(block)
        if loud:
            self._speech_blocks += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
        return (self._silence_run >= self.end_blocks
                and self._speech_blocks >= self.min_speech_blocks)

    @property
    def calibrated(self) -> bool:
        """True once the noise floor has been measured (or speech has started)."""
        return self.started or len(self._noise) >= self.calib_blocks

    def audio(self):
        return np.concatenate(self._blocks) if self._blocks else np.zeros(0, np.float32)


def _prepare_mic_audio(audio: np.ndarray, sr: int, normalize: bool = True) -> np.ndarray:
    """Resample captured audio to ZF_SAMPLE_RATE, drop DC, optionally normalise
    level (quiet headset mics often peak at <0.1, which hurts accuracy) and add
    short silence padding at both ends (streaming Zipformer needs a little
    left context to decode the first word reliably).

    `normalize` must be False when no speech was detected: scaling a recording
    of pure background noise up to 0.6 peak would turn it into loud "speech"
    and defeat the silence warning in zf_transcribe()."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return audio
    audio = _resample(audio, int(sr), ZF_SAMPLE_RATE)
    audio = audio - float(audio.mean())
    if normalize:
        peak = float(np.abs(audio).max())
        if peak > 1e-4:
            audio = audio * (0.6 / peak)
    pad = np.zeros(int(0.3 * ZF_SAMPLE_RATE), dtype=np.float32)
    return np.concatenate([pad, audio, pad])


def _import_sounddevice():
    """Import sounddevice so that it loads the PortAudio DLL matching THIS
    interpreter's architecture.

    sounddevice (<= 0.5.x) chooses libportaudio{arm64,64bit}.dll from
    platform.machine(). On Windows on ARM that returns the host CPU ('ARM64')
    even in an emulated x64 Python, whose wheel ships only the x64 DLL --
    loading fails with 'error 0x7e'. sysconfig.get_platform() describes the
    interpreter itself, so platform.machine() is made to agree with it for
    the duration of the import (the DLL choice is made once, at import time).
    """
    if "sounddevice" in sys.modules:
        return sys.modules["sounddevice"]
    import platform
    import sysconfig
    interp = sysconfig.get_platform().lower()          # 'win-amd64' / 'win-arm64'
    real_machine = platform.machine
    if (sys.platform == "win32" and interp == "win-amd64"
            and real_machine().lower() in ("arm64", "aarch64")):
        platform.machine = lambda: "AMD64"
    try:
        import sounddevice
    finally:
        platform.machine = real_machine
    return sounddevice


def record_from_mic(duration_s: float, sample_rate: int = ZF_SAMPLE_RATE) -> np.ndarray:
    """Capture one utterance from the default microphone.

    Recording starts immediately (no fixed window): a rolling pre-roll keeps
    the first syllables, capture stops ~1 s after you stop speaking, and
    `duration_s` is only the maximum length. The stream is opened at the
    device's native sample rate and resampled with a proper polyphase filter
    (asking Windows/MME for 16 kHz directly gives lower-quality audio). If no
    speech is detected within `duration_s` the whole recording is returned.
    Raises RuntimeError with a clear message if no input device is available.
    """
    try:
        sd = _import_sounddevice()
        dev = sd.query_devices(kind="input")
        native_sr = int(dev["default_samplerate"])
        block = int(native_sr * 0.03)
        ep = _EnergyEndpointer(native_sr)
        everything = []
        print(f"\n[Mic] Using '{dev['name'].strip()}' @ {native_sr} Hz "
              f"(max {duration_s:.0f}s, stops after ~1s of silence).")
        with sd.InputStream(samplerate=native_sr, channels=1, dtype="float32",
                            blocksize=block) as stream:
            stream.read(block * 5)          # discard device warm-up transients
            print("[Mic]   measuring background noise (stay quiet for a moment) ...")
            n_blocks = int(duration_s / 0.03)
            done = False
            announced = False
            for _ in range(n_blocks):
                data, _ovf = stream.read(block)
                data = data.reshape(-1)
                everything.append(data.copy())
                finished = ep.feed(data)
                if not announced and ep.calibrated:
                    announced = True
                    print("[Mic]   >>> speak now <<<")
                if finished:
                    done = True
                    break
    except Exception as e:
        raise RuntimeError(
            f"Microphone recording failed ({type(e).__name__}: {e}). "
            f"Pass --audio <file.wav> or --text '<...>' instead."
        ) from e

    if ep.started:
        captured = ep.audio()
        print(f"[Mic]   done ({len(captured) / native_sr:.1f}s of speech"
              f"{'' if done else ', hit max length'}).")
    else:
        captured = np.concatenate(everything) if everything else np.zeros(0, np.float32)
        print("[Mic]   done (no clear speech detected -- using the whole recording).")
    out = _prepare_mic_audio(captured, native_sr, normalize=ep.started)
    if out.size:
        print(f"[Mic]   level: peak={float(np.abs(captured).max()) if captured.size else 0:.3f}"
              f"{' (normalised before ASR).' if ep.started else '.'}")
    return out


def load_wav(path: str) -> tuple[np.ndarray, int]:
    """Load a WAV file as (mono float32 waveform, sample_rate)."""
    import soundfile as sf
    waveform, sr = sf.read(path)
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    return waveform.astype(np.float32), sr


def save_wav(audio: np.ndarray, path: str, sample_rate: int) -> None:
    """Save a float32 mono waveform to a WAV file, creating parent dirs."""
    import soundfile as sf
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak * 0.95
    sf.write(path, audio, samplerate=sample_rate)
    print(f"[Audio] Saved {len(audio) / sample_rate:.2f}s -> {path}")


def play_audio(audio: np.ndarray, sample_rate: int) -> None:
    """Play a float32 mono waveform through the default output device.
    Never raises: the audio has already been saved, so a missing / broken
    audio stack only produces a warning."""
    print(f"[Speaker] Playing {len(audio) / sample_rate:.2f}s of audio...")
    try:
        sd = _import_sounddevice()
        sd.play(audio, samplerate=sample_rate)
        sd.wait()
    except Exception as e:
        print(f"[Speaker][WARN] Playback failed ({type(e).__name__}: {e}). "
              f"The audio file above was saved; use --no_play to skip playback.")


# SECTION 6 -- Orchestration: per-direction Init / Inference / Release

DIRECTIONS = ("zh2en", "en2zh")


def _check_direction(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction: {direction!r} (expected one of {DIRECTIONS})")


def Init(direction: str, need_asr: bool = True) -> None:
    """Load exactly the model set this run needs:
      zh2en: [Zipformer (ASR)] + OpusMT zh->en (MT) + PiperTTS-EN (TTS)
      en2zh: [Zipformer (ASR)] + OpusMT en->zh (MT) + MeloTTS-ZH (TTS)
    The ASR model is skipped (not even downloaded) when need_asr is False,
    i.e. when the caller supplies text directly.
    """
    _check_direction(direction)
    _install_hf_ssl_context()
    print(f"[Init] direction={direction}  asr={'on' if need_asr else 'off (text input)'}")
    if need_asr:
        print("[Init] [1/3] Zipformer ASR (Mandarin+English streaming)...")
        zf_init()
    else:
        print("[Init] [1/3] Zipformer ASR skipped.")

    print(f"[Init] [2/3] OpusMT {direction}...")
    opus_init(direction)

    print("[Init] [3/3] TTS...")
    if direction == "zh2en":
        piper_init()
    else:
        melotts_init()
    print("[Init] All models ready.\n")


def Release(direction: str) -> None:
    print("[Release] Releasing models...")
    if direction == "zh2en":
        piper_release()
    else:
        melotts_release()
    opus_release(direction)
    zf_release()


def Inference(
    direction: str,
    audio_path: str | None = None,
    text: str | None = None,
    record_seconds: float = 15.0,
    keep_capture: bool = False,
) -> dict[str, Any]:
    """Run one full speech-translation turn and return a result dict with
    keys: source_text, translated_text, audio (float32 waveform),
    sample_rate.

    Exactly one of audio_path / text should be given; if neither is
    given, up to record_seconds of audio is captured from the microphone
    (saved under output/ only when keep_capture is True).
    """
    _check_direction(direction)

    if text is not None:
        source_text = text.strip()
        print(f"[ASR] Skipped (--text given): {source_text}")
    else:
        if audio_path:
            waveform, sr = load_wav(audio_path)
            print(f"[ASR] Loaded audio: {audio_path} ({len(waveform)/sr:.2f}s @ {sr}Hz)")
        else:
            waveform, sr = record_from_mic(record_seconds), ZF_SAMPLE_RATE
            if keep_capture:   # keep the capture so a bad recognition can be reproduced / re-tested
                try:
                    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                    import soundfile as _sf
                    _rec = OUTPUT_DIR / f"mic_{direction}_{int(time.time())}.wav"
                    _sf.write(str(_rec), waveform, ZF_SAMPLE_RATE)
                    print(f"[Mic]   saved capture -> {_rec}")
                except Exception as e:                                # noqa: BLE001
                    print(f"[Mic][WARN] could not save capture ({type(e).__name__}: {e})")
        source_text = zf_transcribe(waveform, sr, audio_path=audio_path,
                                    language="en" if direction == "en2zh" else None)
        print(f"[ASR] Transcribed: {source_text}")
        if not source_text:
            print("[ASR][WARN] Empty transcription -- check microphone input / audio level.")

    translated_text = opus_translate(direction, source_text) if source_text else ""

    if direction == "zh2en":
        audio = piper_synthesize(translated_text) if translated_text else np.zeros(0, dtype=np.float32)
        out_sr = PIPER_SAMPLE_RATE
    else:
        audio = melotts_synthesize(translated_text) if translated_text else np.zeros(0, dtype=np.float32)
        out_sr = MELO_SAMPLE_RATE

    return {
        "source_text": source_text,
        "translated_text": translated_text,
        "audio": audio,
        "sample_rate": out_sr,
    }


# SECTION 7 -- CLI

def _check_dependencies(direction: str, need_mic: bool, need_playback: bool,
                        need_file_audio: bool) -> list[str]:
    """Return a list of missing REQUIRED packages (with install hints) and print
    a one-line note for missing OPTIONAL ones. Uses find_spec, so nothing heavy
    (torch, melo, ...) is imported just to check."""
    import importlib.util
    import shutil as _sh

    def has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    required = [                       # (module, pip hint)
        ("scipy", "scipy"),
        ("soundfile", "soundfile"),
        ("requests", "requests"),
        ("transformers", "transformers sentencepiece"),
        ("sentencepiece", "sentencepiece"),
    ]
    if direction == "zh2en":
        required.append(("gruut", "gruut"))
    else:
        required += [("torch", "torch (CPU wheel, see README)"),
                     ("melo", "melotts (patched, see README 'Environment Setup')")]
    if need_mic or need_playback:
        required.append(("sounddevice", "sounddevice"))

    missing = [hint for mod, hint in required if not has(mod)]

    optional = []
    if not has("kaldi_native_fbank"):
        optional.append("kaldi_native_fbank (more accurate ASR features; a numpy fallback is used)")
    if direction == "en2zh" and not has("g2p_en"):
        optional.append("g2p_en (phonetic repair of the English transcript)")
    if need_file_audio and _sh.which("ffmpeg") is None:
        optional.append("ffmpeg on PATH (silence-based VAD for long WAV files)")
    if optional:
        print("[Deps][note] optional, not found: " + "; ".join(optional))
    return missing


def _chipset_arg(value: str) -> str:
    dev = normalize_chipset(value)
    if dev is None:
        raise argparse.ArgumentTypeError(
            f"unknown chipset {value!r}; use x_elite (8380) or x2_elite (8480)")
    return dev


# Sample input clips used when `--audio` is given without a file name.
_DEFAULT_AUDIO_SENTINEL = "\0default-audio\0"
_DEFAULT_AUDIO_URLS = {
    "en2zh": ("https://china-qaihub-public-assets.s3.cn-northwest-1.amazonaws.com.cn/qai-hub-models/"
              "models/whisper_asr_shared/v1/audio/jfk.wav"),
    "zh2en": ("https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/"
              "resolve/main/test_wavs/0.wav"),
}


def download_default_audio(direction: str) -> str:
    """Download (once) the sample input WAV for `direction` into INPUT_DIR and
    return its path. An already downloaded file is reused without network access."""
    _check_direction(direction)
    url = _DEFAULT_AUDIO_URLS[direction]
    path = INPUT_DIR / os.path.basename(url)
    if path.is_file() and path.stat().st_size > 0:
        print(f"[Audio] Using default sample input: {path}")
        return str(path)
    print(f"[Audio] No --audio file given; downloading default sample: {url}")
    ok = download_url(
        url, str(path),
        desc=f"Downloading default {direction} sample audio ({path.name})...",
        fail=f"\nFailed to download the default sample audio from:\n  {url}\n"
             f"Please download it manually and save it to: {path}",
    )
    if not ok or not path.is_file():
        raise RuntimeError(f"Default sample audio could not be downloaded: {url}")
    print(f"[Audio] Using default sample input: {path}")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "On-device bidirectional speech translator (Snapdragon NPU / QNN HTP). "
            "zh2en: Chinese speech -> Zipformer ASR -> OpusMT -> English speech (PiperTTS-EN). "
            "en2zh: English speech -> Zipformer ASR -> OpusMT -> Chinese speech (MeloTTS-ZH)."
        )
    )
    parser.add_argument(
        "--direction", choices=DIRECTIONS, default="zh2en",
        help="Translation direction (default: zh2en).",
    )
    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument(
        "--audio", type=str, nargs="?", const=_DEFAULT_AUDIO_SENTINEL, default=None,
        metavar="FILE",
        help="Input WAV file path. Given without a FILE, a sample clip for the "
             "chosen --direction is downloaded to "
             f"{INPUT_DIR} and used (en2zh: jfk.wav, zh2en: 0.wav). If omitted "
             "(and --text is not given), records from the microphone instead.",
    )
    src_group.add_argument(
        "--text", type=str, default=None,
        help="Skip ASR and translate this text directly (source language "
             "must match --direction's input side). The ASR model is not "
             "loaded or downloaded in this mode.",
    )
    parser.add_argument(
        "--record_seconds", type=float, default=15.0,
        help="Maximum microphone recording length in seconds (default: 15). "
             "Recording stops by itself ~1s after you stop speaking, so this "
             "is only a ceiling -- raise it for long utterances, e.g. "
             "--record_seconds 60. Ignored when --audio or --text is given.",
    )
    parser.add_argument(
        "--keep_capture", action="store_true",
        help=f"Save the raw microphone recording to {OUTPUT_DIR}\\mic_<direction>_<timestamp>.wav "
             "(off by default: captures are not written to disk).",
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="Save the synthesized speech to this WAV path; a relative path is "
             f"relative to the current directory (default: {OUTPUT_DIR}\\<direction>_<timestamp>.wav).",
    )
    parser.add_argument(
        "--no_play", action="store_true",
        help="Do not play the synthesized speech through the speakers "
             "(it is still saved to --out).",
    )
    parser.add_argument(
        "--chipset", type=_chipset_arg, default=None, metavar="{x_elite,x2_elite,8380,8480}",
        help="Force the target chip (which model binaries to download/load): "
             "x_elite/8380 = Snapdragon X Elite, x2_elite/8480 = Snapdragon X2 Elite. "
             "Default: auto-detect from the CPU name.",
    )
    parser.add_argument(
        "--models_dir", type=str, default=None, metavar="DIR",
        help=f"Directory holding the model binaries, one sub-folder per model "
             f"(default: {MODELS_DIR}).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show the full Python traceback on errors instead of a short message.",
    )
    parser.add_argument(
        "--strict_tls", action="store_true",
        help="Use Python's default strict X.509 checking for downloads. By default only the "
             "strict-extension check is relaxed (needed for the AI Hub S3 CA chain on "
             "Python >= 3.13); certificate and hostname verification are always on.",
    )
    args = parser.parse_args()

    global _STRICT_TLS
    _STRICT_TLS = args.strict_tls

    if args.audio == _DEFAULT_AUDIO_SENTINEL:
        try:
            args.audio = download_default_audio(args.direction)
        except RuntimeError as e:
            print(f"\n[ERROR] {e}")
            return 1

    if args.audio and not os.path.isfile(args.audio):
        parser.error(f"--audio file not found: {args.audio}")
    if args.text is not None and not args.text.strip():
        parser.error("--text is empty")

    set_chipset(args.chipset)
    if args.models_dir:
        set_models_dir(args.models_dir)

    missing = _check_dependencies(
        args.direction,
        need_mic=args.text is None and not args.audio,
        need_playback=not args.no_play,
        need_file_audio=bool(args.audio),
    )
    if missing:
        print("[ERROR] Missing required Python packages:")
        for hint in missing:
            print(f"          - {hint}")
        print("        See README.md ('Requirements') for the exact install commands.")
        return 2

    result = None
    try:
        Init(args.direction, need_asr=args.text is None)
        result = Inference(
            args.direction,
            audio_path=args.audio,
            text=args.text,
            record_seconds=args.record_seconds,
            keep_capture=args.keep_capture,
        )
    except KeyboardInterrupt:
        print("\n[Interrupted]")
        return 130
    except (RuntimeError, OSError, ValueError) as e:
        if args.debug:
            raise
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        print("        (re-run with --debug for the full traceback)")
        return 1
    finally:
        Release(args.direction)

    print("\n" + "=" * 60)
    print("  SPEECH TRANSLATION RESULT")
    print("=" * 60)
    print(f"  Direction : {args.direction}")
    print(f"  Source    : {result['source_text']}")
    print(f"  Translated: {result['translated_text']}")
    print("=" * 60)

    audio = result["audio"]
    sr = result["sample_rate"]
    if audio.size == 0:
        print("[WARN] No audio was synthesized (empty translation) -- nothing to play/save.")
        return 0

    out_path = args.out
    if out_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = str(OUTPUT_DIR / f"{args.direction}_{int(time.time())}.wav")
    else:
        out_path = os.path.abspath(out_path)     # relative --out is relative to the cwd
    save_wav(audio, out_path, sr)

    if not args.no_play:
        play_audio(audio, sr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
