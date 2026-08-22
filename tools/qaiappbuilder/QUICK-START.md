# Build & Run — Quick Guide

> A concise guide to install, build, run, and package QAI AppBuilder.
>
> 中文版：[`QUICK-START.zh-CN.md`](QUICK-START.zh-CN.md).

> **Platform**: Windows on Snapdragon (ARM64). Run every `.bat` from the repo root
> by double-click or in `cmd.exe`. The scripts auto-download uv / Python 3.13 ARM64 /
> PortableGit / Node.js into `%LOCALAPPDATA%\QAIModelBuilder\` — **no admin rights,
> no manual Python install needed.**

---

## Just want to use it? (No coding)

```cmd
Setup.bat
Start.bat
```

That's it. `Setup.bat` installs everything automatically (first time only); `Start.bat`
launches the WebUI and opens your browser. Start chatting to build an app, convert a model,
or run one from AI Hub.

> **First screen is a login.** The WebUI is gated behind Okta single sign-on (on by default),
> so you sign in before you reach the tool. Disable it only for local `pnpm dev`.

---

## For developers

`Setup.bat` prepares the local environment. Use `Build.bat` to refresh the frontend, then use `Start.bat` to run the application.

---

## A. Dev mode (most common)

### First time: one-shot environment setup

```cmd
Setup.bat
```

Does everything: download uv / install Python 3.13 (ARM64 by default, or x64 with
`--arch x64`) / create venv at `%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313`
(or `.venv_x64_313`) / install every dep from `pyproject.toml` / install PortableGit
+ Node.js + pnpm / install QAIRT SDK (needed for model conversion, ~2 GB) /
pre-download Whisper / Zipformer / MeloTTS weights / initialize `data/` (`qai.db`,
factory seeds, secret namespaces).

Common flags:

| Flag | Purpose |
|---|---|
| `--arch arm64\|x64` | Force architecture (default: auto-detect from host). Use `--arch x64` on WoS to install the x64 stack under Prism emulation for validation |
| `--no-builder` | Skip QAIRT SDK / VS toolchain (~2 GB saved when you won't convert models) |
| `--dev` | Also install contributor toolchain (pytest / mypy / ruff / playwright + Chromium) |
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
```

> **Changed Python backend?** Just restart `Start.bat`; you do NOT need `Build.bat`.
> **Changed Vue/TS frontend?** Run `Build.bat` once, then `Start.bat`.

### Launching the server

```cmd
Start.bat
```

The server picks an available port at startup and writes the resolved URL to
`data\runtime\server.endpoint.json`; the browser is opened to the right address
automatically. Press `Ctrl+C` to stop.

### Other handy entry points

| Command | Purpose |
|---|---|
| `qai.bat <args>` | Run the unified CLI without activating the venv (`qai --help` / `qai config provider list` / `qai build`…) |
| `Console.bat` | Drop into an activated venv shell for `pip install <pkg>` / ad-hoc Python |
| `Uninstall.bat` | Remove everything Setup installed OUTSIDE the project (venv / PortableGit / Node); **leaves `data/` untouched** |
| `Uninstall.bat --all` | Above + uv cache + QAIRT SDK + Playwright Chromium + `vendor/` runtime caches |

---

## Package for end users

Build the frontend bundle, then create the distribution archive:

```cmd
Build.bat --install
```

Archive the complete `tools\qaiappbuilder\` directory as **`qaiappbuilder.zip`**. Do not include local runtime artifacts such as `data\`, `frontend\node_modules\`, or temporary build caches.

**End-user install flow:**

```cmd
Extract qaiappbuilder.zip  →  Setup.bat  →  Start.bat
```

> End-user machines need no preinstalled Python, Node.js, or Git; `Setup.bat` installs the required runtime tools.

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
| Package for end users | `Build.bat --install`, then archive `tools\qaiappbuilder\` as `qaiappbuilder.zip` |
| Run a one-shot CLI command | `qai.bat <args>` |
| Install an extra Python pkg temporarily | `Console.bat` then `pip install <pkg>` |
| Full cleanup (keep `data/`) | `Uninstall.bat` (or `--all` for deeper cleanup) |

> **`Setup.bat` / `Build.bat` / `Uninstall.bat` support `--help` / `-h` / `/?`** — e.g. `Build.bat --help` lists every flag. `Start.bat` / `qai.bat` forward any extra arguments to the underlying Python entry point.
