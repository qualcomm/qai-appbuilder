# `scripts/release/` — Clean-Cutover release packaging

> Stage: S7 (PR-074). Replaces the legacy
> `build_external_release.py` / `check_external_release.py` pair —
> those files remain on disk through PR-074 (this PR) and are deleted
> by S8 PR-081 per `refactor-plan.md` §13.8 + §14.3 and the v2.7 §1
> "zero legacy coexistence" rule.

## What this package does

The release pipeline produces a single, self-contained artifact tree
(`.build/release/` by default) that contains:

```
apps/                                     # FastAPI app + DI + lifespan
src/qai/                                  # 9 contexts + platform
interfaces/                               # HTTP / WS / errors / middleware
src/qai/platform/persistence/migrations_sql/   # 7 SQL migrations
scripts/init/                             # install orchestrator
tools/init/                               # install stage runners
factory/                                  # build-time seed factory bundle
skills/                                   # built-in skill packs (SKILL.md)
frontend/dist/                            # Vite SPA bundle
scripts/release/{__init__.py, README.md}    # manifest.toml + check_release.py are NOT shipped (see note below)
pyproject.toml, README.md, LICENSE
build_info.json                           # edition + version marker (written by stage 5)
docs/{operations,api,architecture}/
Setup.bat, Start.bat, Uninstall.bat, qai.bat, Console.bat   # install / start / uninstall / CLI / shell launchers
```

`vendor/` is NOT shipped. The four heavy dependency caches —
`vendor/whl/` (ARM64-only wheels), `vendor/g2p_data/`, `vendor/nltk_data/`,
`vendor/tiktoken/` — are excluded and instead published as a single
downloadable `vendor-deps.7z`, which `Setup.bat` fetches and merges into
`vendor/` at install time (Step 0b). (The previous `vendor/bin/` sandbox
launcher binaries were removed 2026-07-01 along with the Windows
AppContainer sandbox stack.)

Everything else — the legacy `backend/`, `features/`, the *legacy* root
entry points (`start_server.py`, `start.ps1/.sh`, `Setup_Builder_Env.bat`),
no-build `frontend/{js,css,vendor,locales}/`,
`requirements.txt`, `config/internal_manifest.txt`, etc. — is excluded by the
manifest's
black list.

## Usage

```powershell
# 1) Dry-run (default). Prints the plan; no destructive side effects.
python -m scripts.release

# 2) Real build under .build/release/, archived as zip next to it
#    (.build/qaimodelbuilder-release.zip + .sha256 sidecar).
python -m scripts.release --apply

# 3) Custom output dir + JSON stage stream (CI consumption).
python -m scripts.release --apply --output-dir build/release --json

# 4) Skip frontend_build / factory if they were already produced
#    by a separate step in CI.
python -m scripts.release --apply --skip frontend_build,factory
```

### Stages

| # | Stage              | What it does | Skippable? |
|---|--------------------|---|---|
| 1 | `clean`            | `rm -rf` `output_dir` | yes |
| 2 | `frontend_build`   | `pnpm -C frontend install --frozen-lockfile && pnpm -C frontend build` | yes (reuses existing `frontend/dist/` when entry HTML present) |
| 3 | `factory`          | `python -m scripts.build.compile_factory --apply` to (re)generate `factory/` | yes (reuses existing `factory/user_config.toml`) |
| 4 | `assemble`         | Walk repo root; copy every file matching `[include]` and not `[exclude]` from `manifest.toml` into `output_dir` | yes |
| 5 | `write_build_info` | Write `build_info.json` so the artifact self-reports its edition + version at runtime | yes |
| 6 | `sanitize_factory` | External-only B-class cleanse of assembled `factory/` (remove internal-hostname cloud providers, blank enterprise ANTHROPIC base URL). Skipped for internal builds | yes |
| 7 | `finalize_opensource` | External-only: swap in the sanitized external README(s) (`README.external*.md` → `README.md` / `README.zh-CN.md`) and write a curated open-source `.gitignore`. Skipped for internal builds | yes |
| 8 | `check_release`    | Black / white list scan over `output_dir` (see below) | yes |
| 9 | `install_smoke` | `python -m scripts.init.install --skip compile_factory --apply` against a temp `_smoke_data/` inside the artifact, then teardown | yes |
| 10 | `archive`          | `zip` (or `tar.gz`) `output_dir` next to itself; emit `.sha256` sidecar | yes |

Each stage is independently skippable via `--skip stage1,stage2,...`.
Failure of any stage aborts the pipeline; the orchestrator returns
exit-code 1.

## Verifying an existing artifact

Run the checker from the **source tree / CI** (it is not shipped inside the
artifact — see the note in "What this package does"):

```powershell
python -m scripts.release.check_release `
    --artifact-root  .build/release `
    --emit-sha256    `
    --sha256-output  .build/release.sha256
```

`check_release.py` is intentionally dependency-free (only stdlib). It is
deliberately **excluded from the release artifact** because it embeds the
internal-network domain list (`SENSITIVE_KEYWORDS`) as detection constants;
shipping it would leak those intranet hosts into an external product. Verify
artifacts from a source checkout / the build host instead.

## The manifest is the contract

`scripts/release/manifest.toml` is the single source of truth for what
belongs in a release artifact. Two top-level lists:

* `[include].paths` — every file under these paths is copied in.
* `[exclude].paths` — every file matching one of these patterns is
  rejected by `check_release.py` (v2.7 §1 hard requirement).

Pattern semantics — see `check_release.matches_pattern`:
* trailing `/` → directory recursive (e.g. `backend/` matches every
  file under it).
* embedded `**` / `*` / `?` → fnmatch glob (so `**/__pycache__/`
  catches caches anywhere).
* plain segment without `/` → matches the basename anywhere
  (e.g. `LICENSE`, `README.md`).
* otherwise → anchored prefix / exact match.

When a path's role changes (S8 will rename `frontend/index-new.html` →
`frontend/index.html`), update the manifest in the same PR. There is
no compatibility window or feature flag.

## Programmatic entry

For tests and other Python callers:

```python
from pathlib import Path
from scripts.release.build import run

result = run(
    repo_root=Path("/repo"),
    output_dir=Path("/tmp/release"),
    apply=True,
    skip_stages=("frontend_build", "factory", "install_smoke", "archive"),
)
assert result.exit_code == 0
```

`run()` returns a `BuildResult` with structured per-stage records
(`StageResult`) and the `CheckResult` from stage 8.
