# Build & Run — Quick Guide

> Three usage modes + one cheat-sheet.
>
> 中文版：[`QUICK-START.zh-CN.md`](QUICK-START.zh-CN.md)。

> **Platform**: Windows on Snapdragon (ARM64). Run every `.bat` from the repo root
> by double-click or in `cmd.exe`. The scripts auto-download uv / Python 3.13 ARM64 /
> PortableGit / Node.js into `%LOCALAPPDATA%\QAIModelBuilder\` — **no admin rights,
> no manual Python install needed.**

---

## Three modes at a glance

| Mode | Scripts | Who it's for | First-time prep | Daily run |
|---|---|---|---|---|
| **A. Dev mode (run from source)** | `Setup.bat` → `Build.bat` → `Start.bat` | Code changes / debugging / contributors | `Setup.bat` + `Build.bat` | `Start.bat` (backend change) / `Build.bat` + `Start.bat` (frontend change) |
| **B. Desktop app (Tauri)** | `Setup.bat --desktop` → `Build.bat --desktop` | Want a standalone `.exe` / `.msi` installer | `Setup.bat --desktop` | Run the installer under `desktop\src-tauri\target\release\bundle\` |
| **C. Release artifact (External / Internal)** | `Release.bat [version] [--internal]` | Packaging for end-users | `Setup.bat` once | Ship archive from `dist\release\` → user extracts → runs `Setup.bat` → `Start.bat` |

---

## A. Dev mode (most common)

### First time: one-shot environment setup

```cmd
Setup.bat
```

Does everything: download uv / install Python 3.13 ARM64 / create venv at
`%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313` / install every dep from
`pyproject.toml` / install PortableGit + Node.js + pnpm / install QAIRT SDK
(needed for model conversion, ~2 GB) / pre-download Whisper / Zipformer / MeloTTS
weights / initialize `data/` (`qai.db`, factory seeds, secret namespaces).

Common flags:

| Flag | Purpose |
|---|---|
| `--no-builder` | Skip QAIRT SDK / VS toolchain (~2 GB saved when you won't convert models) |
| `--dev` | Also install contributor toolchain (pytest / mypy / ruff / playwright + Chromium) |
| `--desktop` | Also install Rust + tauri-cli + WebView2, preparing the desktop build |
| `--no-pause` | Don't pause at the end (use from CI) |

> Idempotent — safe to re-run. **`data/` is NOT tracked in git**; delete it and
> re-run `Setup.bat` to regenerate from scratch.

### Building the frontend

The Python backend is interpreted — **no build step**. The Vue/Vite frontend
must be built into `frontend\dist\` so `Start.bat` can serve it:

```cmd
Build.bat              REM Fast incremental: vite build only (iteration loop)
Build.bat --full       REM Verified: gen:types + typecheck + lint + test + build (pre-commit/pre-release)
Build.bat --clean      REM node_modules is corrupt: wipe and reinstall
Build.bat --desktop    REM Also produce the Tauri desktop release installer
Build.bat --desktop-dev REM Debug desktop shell (`cargo tauri dev`, Rust hot-reload)
```

> **Changed Python backend?** Just restart `Start.bat`; you do NOT need `Build.bat`.
> **Changed Vue/TS frontend?** Run `Build.bat` once, then `Start.bat`.

### Launching the server

```cmd
Start.bat              REM Normal: spawn server in a new window, auto-open browser
Start.bat --reload     REM Hot-reload (development)
```

Port is auto-selected (default 8989, falls back to 8088 / 7799 / 12989 / 18989 /
28989 if occupied). The actual URL is
written to `data\runtime\server.endpoint.json` and the browser is opened to the
right address automatically. Ctrl+C to stop.

### Other handy entry points

| Command | Purpose |
|---|---|
| `qai.bat <args>` | Run the unified CLI without activating the venv (`qai --help` / `qai config provider list` / `qai build`…) |
| `Console.bat` | Drop into an activated venv shell for `pip install <pkg>` / ad-hoc Python |
| `Uninstall.bat` | Remove everything Setup installed OUTSIDE the project (venv / PortableGit / Node); **leaves `data/` untouched** |
| `Uninstall.bat --all` | Above + uv cache + QAIRT SDK + Playwright Chromium + `vendor/` runtime caches |

---

## B. Desktop app (Tauri)

Package the WebUI as a standalone `.exe` / `.msi` so end-users never see a browser.

```cmd
Setup.bat --desktop          REM First time: install Rust + tauri-cli + WebView2 runtime
Build.bat --desktop          REM Produce release installer
```

Artifacts: `desktop\src-tauri\target\release\bundle\` (`.msi` / `.exe`).

Local desktop debugging (no installer):

```cmd
Build.bat --desktop-dev      REM Launch cargo tauri dev with Rust hot-reload
```

---

## C. Release artifact (for end-users)

`Release.bat` runs the full pipeline: clean → frontend build → factory compile →
assemble → write `build_info.json` → sanitize internal-only assets (when external) →
manifest whitelist check → archive.

```cmd
Release.bat                  REM Default: external edition, version 2.0.0
Release.bat 2.1.0            REM External edition, custom version
Release.bat --internal       REM Internal full-feature edition (keeps internal providers / telemetry)
Release.bat 2.1.0 --internal REM Combined
```

Output: directory + archive under `dist\release\`; `build_info.json` self-reports
version and edition.

**End-user install flow** (what the user does after receiving the release archive):

```cmd
Extract release archive  →  Setup.bat  →  Start.bat
```

> User machines need no Python / Node / git — `Setup.bat` handles all of it.

---

## Cheat-sheet — what should I run right now?

| I want to … | Run |
|---|---|
| First-time checkout from source | `Setup.bat` |
| Changed Python backend | `Start.bat` (just restart) |
| Changed Vue/TS frontend | `Build.bat` then `Start.bat` |
| Changed frontend deps (`package.json`) | `Build.bat --install` |
| `node_modules` is broken | `Build.bat --clean` |
| Run pytest / write contributor tests | `Setup.bat --dev` once, then `Console.bat` to enter venv |
| Want the desktop app | `Setup.bat --desktop` + `Build.bat --desktop` |
| Cut a release for end-users | `Release.bat [version]` |
| Run a one-shot CLI command | `qai.bat <args>` |
| Install an extra Python pkg temporarily | `Console.bat` then `pip install <pkg>` |
| Full cleanup (keep `data/`) | `Uninstall.bat` (or `--all` for deeper cleanup) |

> **Every script supports `--help` / `-h` / `/?`** — e.g. `Build.bat --help` lists every flag.
