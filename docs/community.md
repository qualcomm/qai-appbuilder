# Community Apps — Submission Guide & Review Criteria

Welcome! The [`CommunityApps/`](../CommunityApps/) gallery showcases on-device AI
apps built by the community on top of
[QAI AppBuilder](https://github.com/quic/ai-engine-direct-helper). This guide
explains **how to submit**, **what reviewers look for**, and where to find more.

- 🖼️ Gallery wall: [`CommunityApps/index.html`](../CommunityApps/index.html)
- 📇 App index & quick start: [`CommunityApps/README.md`](../CommunityApps/README.md)
- 🧩 Copy-me template: [`CommunityApps/_template/`](../CommunityApps/_template/)

### Currently in the gallery

These entries are auto-generated from each app's `app.json` — do not hand-edit the
list below; it's here as a snapshot of what's live today.

| App | Category | Author | Description |
|-----|----------|--------|-------------|
| [Real-ESRGAN x4plus](../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/) | vision | shengtin | Upscale any image 4× with Real-ESRGAN x4plus, running fully on the Snapdragon NPU (HTP) via QAI AppBuilder. |
| [Track-Anything](../samples/ComputerVision/video_object_tracking/track_anything/) | vision | [@tim202503](https://github.com/tim202503) | Click a target in any video and track it frame-by-frame with XMem segmentation, running fully on the Snapdragon NPU. |
| [Edge Inferencer](https://github.com/YeWenxuan64/Edge_Inferencer) | other | [@YeWenxuan64](https://github.com/YeWenxuan64) | Reusable QNN inference executors (single/multi-process, async, shared-memory) for running models on the Snapdragon NPU, with a runnable demo. |

The first two live under [`samples/`](../samples/) (curated samples that also carry an
`app.json`); the third is a classic community app under
[`CommunityApps/edge-inferencer/`](../CommunityApps/edge-inferencer/).

---

## Why `app.json` is the key

Every app carries one metadata file — `app.json` — validated against
[`schema/app.schema.json`](../CommunityApps/schema/app.schema.json). It is the
**single source of truth**:

- `build_gallery.py` auto-discovers each app from its `app.json`, so the
  **gallery wall**, the **README index table**, the **contributor wall**, and
  **CI validation** all derive from it.
- No human-maintained list of apps exists anywhere — you add a folder, fill in
  `app.json`, run the script, and your app appears everywhere.

**Where apps are discovered.** `build_gallery.py` scans two locations:

- `CommunityApps/<slug>/app.json` — one level deep (the classic community app).
- `samples/**/app.json` — **recursively**, so a curated sample under
  `samples/` (e.g.
  [`samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/`](../samples/ComputerVision/Super_Resolution/real_esrgan_x4plus/))
  can carry an `app.json` and appear in the same gallery. Paths in the generated
  `apps.json`/README are computed relative to `CommunityApps/`, so a sample app
  gets a `../samples/...` link automatically.

Keep `slug` matching your folder name (underscores in the folder are treated as
hyphens, so a `track_anything/` folder may use the slug `track-anything`), keep
`official` out (or `false`), and keep `description.short` under 160 characters.

**Hiding an app.** Set `"hidden": true` in an `app.json` to keep the app in the
repo but exclude it from `apps.json`, the gallery, the README tables, and the
contributor count. Useful for drafts or work-in-progress. The file must still be
valid — `hidden` skips *listing*, not *validation*.

---

## How to submit

### Option A — Show & Tell (lowest barrier)

Just built something? Post it in
[Discussions → Show & Tell](https://github.com/qualcomm/qai-appbuilder/discussions/categories/show-and-tell)
or open an [Issue](https://github.com/qualcomm/qai-appbuilder/issues). No PR
needed. A maintainer may invite you to open a PR to get listed in the gallery.

### Option B — Pull Request (gets you into the gallery)

1. **Fork** the repo.
2. **Copy the template**: `CommunityApps/_template/` → `CommunityApps/<your-slug>/`.
3. **Fill in `app.json`** (see the key fields below). Add a 16:9 screenshot under
   `assets/` (`.png`/`.jpg`; the template ships a placeholder `assets/screenshot.png`).
4. **Make it run** with the `run.command` you declared (default `python main.py`).
5. **Regenerate & self-check**:
   ```bash
   cd CommunityApps
   python build_gallery.py --date <YYYY-MM-DD>
   python build_gallery.py --check   # must exit 0 — this is what CI runs
   ```
6. **Open a PR** and link the Discussion/Issue it came from (`source` in `app.json`).

---

## Required `app.json` fields

| Field | Notes |
|-------|-------|
| `name` | Display name on the card. |
| `slug` | Lowercase-hyphenated; **must match the folder name** (folder underscores count as hyphens). |
| `version` | Semantic version, e.g. `1.0.0`. |
| `author.name` (+ `github`) | You get credited on the contributor wall. |
| `description.short` | ≤ 160 chars, shown on the card and index table. |
| `category` | One of `genai`, `vision`, `audio`, `multimodal`, `agent`, `other`. |
| `run.command` | The command CI/users run. Keep it working. |
| `models[].source` | **Download URL only — never commit weights.** |
| `source` | The Discussion/Issue URL your app came from (optional but encouraged). |
| `hidden` | Optional. `true` excludes the app from the gallery while keeping it in the repo. |

---

## Review criteria

Reviewers approve an app when it is:

1. **On-device & powered by QAI AppBuilder.** Inference runs on the Snapdragon
   NPU (or GPU/CPU) through `qai_appbuilder`. Prefer offline; if it needs the
   network, say so and say why.
2. **Runnable from a clean checkout.** `pip install -r requirements.txt` then the
   declared `run.command` works on Windows on ARM64. Models download on demand.
3. **No committed weights or large binaries.** List `models[].source`; auto-fetch
   at first run. Keep the folder lean.
4. **Metadata valid & gallery up to date.** `python build_gallery.py --check`
   passes; `slug` matches the folder (underscores count as hyphens); screenshot present.
5. **Clear docs.** A per-app `README.md` covering what it does, requirements, and
   how to run.
6. **Appropriately licensed & attributed.** Include a `license`; credit upstream
   models (e.g. Qualcomm AI Hub) and any code you adapted.
7. **Safe & respectful.** No malware, no scraping of private data, follows the
   repo [Code of Conduct](../CODE-OF-CONDUCT.md).

Apps that don't yet meet the bar are welcome in **Show & Tell** while you iterate.

---

## Skill Index (hand-maintained)

Beyond apps, `CommunityApps/README.md` carries a **🧩 Skill Index** listing agent
skills that live elsewhere in the repo (e.g. under
[`tools/`](../tools/)) for use with Claude Code / agent tooling. Unlike the app
tables, this section is **not** generated by `build_gallery.py` — it is a plain
Markdown table edited by hand, kept outside the `<!-- ... -->` marker blocks so
the generator never overwrites it. To add a skill, edit that table (and the
adjacent **Skill Contributors** line) directly in the README.

---

## Awesome QAI AppBuilder apps

A community-curated list of apps, demos, and tutorials:

- 🌟 [Awesome AI Engine Direct Helper apps & discussions](https://github.com/tim202503/ai-engine-direct-helper/discussions/1)
- 💬 [Show & Tell discussions](https://github.com/qualcomm/qai-appbuilder/discussions/categories/show-and-tell)
- 🐛 [Issues](https://github.com/qualcomm/qai-appbuilder/issues)

Want your project on the Awesome list? Add it in the Discussion thread above.

---

## Questions?

Open a [Discussion](https://github.com/qualcomm/qai-appbuilder/discussions) or an
[Issue](https://github.com/qualcomm/qai-appbuilder/issues). Happy building! 🚀
