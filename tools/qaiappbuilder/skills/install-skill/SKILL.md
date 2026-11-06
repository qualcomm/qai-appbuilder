---
name: install-skill
description: Install a skill the user supplies (a local folder or .zip containing a SKILL.md) so it shows up in the app's skill list. Use the skill_install tool.
tags: skill, install, import, zip, folder, SKILL.md
use_for: Installing, adding or importing the user's own skill from a local folder or .zip file; upgrading a skill that is already installed
---

# Install a user's skill

Use the **`skill_install`** tool. Do NOT copy files, `mkdir`, `unzip` or edit the app's
`skills/` folder yourself — the tool validates the package, refuses unsafe archives and
installs atomically (a failed install leaves nothing behind and a previous version intact).

## Steps

1. **Confirm with the user** (use the `question` tool) before installing:
   - the full path of the skill **folder or `.zip`** (it must contain a `SKILL.md` at its
     top level — a single wrapping folder inside a zip is fine), and
   - the skill id to install it under (optional; by default the `name` in its `SKILL.md`).

   A skill can bundle scripts that you will later run when following it. Say that, and only
   install skills the user trusts. Never skip this step.
2. `skill_install(source="<path exactly as the user gave it>")` — add `name="<id>"` only if the
   user wants a different id (letters, digits, `_`, `.`, `-`). Do not add quotes or escape
   characters around the path; spaces and non-ASCII characters are fine.
3. Report the result: where it was installed, its name/description, and — if the result lists
   them — the **runnable files it bundles**. Tell the user it is enabled for cloud models by
   default and can be switched off in the Skills page.
4. **The new skill is available from the NEXT turn**, not this one. Say so, and use it in the
   following message (load it with the `skill` tool by its id).

## Upgrading

If the result says the skill is **already installed**, ask the user whether to replace it. Only
if they agree, call again with `replace=true`. Never replace without asking.

## When something goes wrong

- **`[FileGuard]` / "blocked" / permission message while reading the source** — the path is
  outside the folders the app may access. **Stop.** Tell the user which path was blocked and ask
  them to authorize it in *Security → Allow Lists*, then retry. Do not copy it elsewhere.
- **`no_skill_md`** — the folder/zip has no `SKILL.md` (or `skill.json`) at its top level. Tell the
  user; do not create one for them unless they ask.
- **`not_discovered`** — the files were copied but the app cannot read the skill (usually the
  `SKILL.md` does not begin with a `---` front-matter block). Nothing was installed.
- **`built-in skill`** — the id belongs to a built-in skill. Ask for another id and pass `name=`.
- **`unsafe_archive` / `too_large`** — the zip has paths that escape its folder, links, or is far
  larger than a skill should be (limits: 50 MB, 2000 files). Do not retry with workarounds; tell
  the user.
- **`exists`** — see *Upgrading*.
