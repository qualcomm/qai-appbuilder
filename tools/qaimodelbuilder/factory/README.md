# `factory/` — Factory bundle for fresh v2 installations

> Reference: `docs/90-refactor/refactor-plan.md` v2.6 §9.4.10 + §13.6 + §14.3.
> Historical anchor: introduced in S6 PR-060..PR-064 as `defaults/`; renamed to
> `factory/` on 2026-06-08 (`HANDOFF-after-factory-rename-2026-06-08.md`).
> Earlier PR manifests under `docs/99-archive/pr-manifests/` still reference the
> `defaults/` name.

## What lives here

This directory holds the **factory-shipped assets** that a fresh v2 install
loads on first startup. It replaces the legacy "ship a `config/*.json`
that the runtime modifies in place" pattern with a Clean Cutover
two-stage model:

1. **Build-time compilation.**
   `python -m scripts.build.compile_factory` reads a *sanitised*
   factory source (no user-private machine paths or secrets) under
   `factory/_source/` and produces:
   - `factory/user_config.toml`             — TOML default user config
   - `factory/db_staging/kv_user_prefs.jsonl` — UI prefs seed rows
   - `factory/db_staging/cloud_models_to_model_catalog_entry.jsonl`
                                              — `model_catalog_entry` seed rows
   - `factory/secrets_manifest.json`        — SecretStore namespace map
   - `factory/_source/policy_templates/*.toml`
   - `factory/_source/exec_profiles/*.toml`
   - `factory/compile_factory_manifest.json` (for verify mode)

   This step is run **once per release** (pre-packaging, by the release
   script — not by users). The output is bundled into the installer
   under this directory.

2. **First-run install** (`scripts.init.install` pipeline; stages 2-4).
   When a user first launches the app:
   - **`data_dir` stage** creates an empty `data/` tree, builds `data/db/qai.db`
     by applying the SQL migrations under
     `src/qai/platform/persistence/migrations_sql/`, creates the
     `data/blobs/{chat,app_builder,uploads}/` and `data/secrets/`
     subtrees, and copies `factory/user_config.toml` to
     `data/user_config.toml`.
   - **`seed_defaults` stage** INSERTs each row from `factory/db_staging/*.jsonl`
     into the corresponding `qai.db` table (`model_catalog_entry`
     receives the cloud-model catalogue, `kv_user_prefs` receives the
     UI defaults).
   - **`secret_bootstrap` stage** registers each entry from
     `factory/secrets_manifest.json` as a placeholder
     (empty-string) record in the SecretStore, so the UI's
     "credentials" form already knows which keys to expect; the user
     fills in real values post-install.

## Sub-directory layout

- `_source/` — factory **source material** (the V1-shaped JSON inputs
  consumed by `compile_factory`). The leading underscore signals
  "internal source-of-truth, never read at runtime". Contains
  `*.json` legacy-shaped inputs plus `exec_profiles/`,
  `policy_templates/` review-only TOML byproducts.
- `db_staging/` — JSONL seed rows for `qai.db` (consumed by the
  `seed_defaults` install stage).
- `chat_features/` — packaged chat feature / skill packs (model-builder,
  model-hub, app-builder, etc.). Each subdirectory is a self-contained
  skill with its own `SKILL.md`, references, and optional runtime scripts.
- `user_config.toml` — TOML default user config (copied into `data/`
  on first run).
- `secrets_manifest.json` — SecretStore namespace declaration (no
  plaintext values).

## What does NOT live here

- **No legacy user data**. v2.6 (refactor-plan §9.4.10) mandates that
  the project ships **no user history**. Old `data/history.db`,
  `data/uploads/`, `data/wechat_creds.json`, etc. are *not* carried over;
  the legacy code that read them was deleted in S8.
- **No real secrets**. `secrets_manifest.json` lists *namespaces*
  with redacted values; the `secret_bootstrap` stage never writes a
  real value to disk.

## Regenerating the factory bundle

The seed bundle (`user_config.toml` / `secrets_manifest.json` /
`db_staging/*.jsonl`) is **committed** to this directory and is the
supported supply path for both `build.py` (which reuses it) and
`install` (which seeds `data/` from it). Regenerate it manually only
when the factory source changes, using the committed source under
`factory/_source/`:

```powershell
$env:PYTHONPATH = "src;."
python -m scripts.build.compile_factory `
    --source       factory/_source `
    --source-data  data `
    --dest         factory `
    --apply
```

After regenerating, delete the apply-mode byproducts that must NOT be
committed: the `factory/db/backups/<ts>/` tree (it contains a raw
copy of the source, including plaintext credentials) and the
`compile_factory_manifest.json` / `compile_factory.report.jsonl`
verify-mode artifacts. Keep only `user_config.toml`,
`secrets_manifest.json`, and `db_staging/*.jsonl`.

Static TOMLs (policy templates / exec profiles) are emitted under
`<dest>/config/` (i.e. `factory/config/`), the shipped product config dir
read at runtime. They are NEVER written back into the `--source`
(`factory/_source/`) tree — that is build input only.

## .gitignore status

This directory IS tracked. Its contents change with every release.
Avoid committing machine-private values: pre-commit should reject any
file containing absolute Windows user paths (`C:\Users\<name>\...`),
real API keys, or non-`<redacted>` secret values.
