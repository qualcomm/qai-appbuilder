# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
"""
build_gallery.py — auto-discover community apps and regenerate every derived
artifact from each app's app.json (the single source of truth).

Nothing here is hand-maintained: drop a folder with an app.json into
CommunityApps/, run this script, and the app shows up in apps.json, the visual
wall (index.html), and the README index table automatically.

Usage:
    python build_gallery.py [--date YYYY-MM-DD] [--check]

What it does:
    1. Scans CommunityApps/<app>/app.json (skips _template, schema, assets).
    2. Validates each app.json against schema/app.schema.json (structural checks,
       no third-party jsonschema dependency required).
    3. Writes apps.json — the aggregated manifest read by index.html.
    4. Rewrites the table between the <!-- APPS_TABLE:START/END --> markers and
       the contributor wall between <!-- CONTRIBUTORS:START/END --> in README.md.

    --check : do not write; exit 1 if anything is invalid or out of date.
              (Use in CI to enforce "generated files are up to date".)
    --date  : value stamped into apps.json "generatedAt" (kept explicit so the
              output is deterministic / reproducible in CI). Defaults to the
              env var GALLERY_DATE, else "unknown".

Standard library only — no third-party deps.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

APPS_DIR = Path(__file__).resolve().parent
REPO_ROOT = APPS_DIR.parent
SAMPLES_DIR = REPO_ROOT / "samples"
SKIP = {"_template", "schema", "assets"}
DISCUSSIONS_URL = "https://github.com/qualcomm/qai-appbuilder/discussions/categories/show-and-tell"
ISSUES_URL = "https://github.com/qualcomm/qai-appbuilder/issues"

TABLE_START = "<!-- APPS_TABLE:START -->"
TABLE_END = "<!-- APPS_TABLE:END -->"
GALLERY_START = "<!-- GALLERY:START -->"
GALLERY_END = "<!-- GALLERY:END -->"
CONTRIB_START = "<!-- CONTRIBUTORS:START -->"
CONTRIB_END = "<!-- CONTRIBUTORS:END -->"

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VALID_CATEGORIES = {"genai", "vision", "audio", "multimodal", "agent", "other"}


def validate(meta, folder):
    """Lightweight structural validation mirroring schema/app.schema.json.

    Returns a list of human-readable problems (empty == valid). We avoid a hard
    dependency on the `jsonschema` package so CI stays stdlib-only.
    """
    problems = []

    def require(cond, msg):
        if not cond:
            problems.append(msg)

    require(meta.get("name"), "missing 'name'")
    slug = meta.get("slug", "")
    require(bool(SLUG_RE.match(slug)), f"'slug' must be lowercase-hyphenated, got {slug!r}")
    require(slug == folder.replace("_", "-"),
            f"'slug' ({slug!r}) must match the folder name ({folder!r}); "
            "underscores in the folder are treated as hyphens")
    require(re.match(r"^[0-9]+\.[0-9]+\.[0-9]+", meta.get("version", "")),
            "'version' must be semver, e.g. 1.0.0")

    author = meta.get("author") or {}
    require(author.get("name"), "author.name is required")

    desc = meta.get("description") or {}
    short = desc.get("short", "")
    require(bool(short), "description.short is required")
    require(len(short) <= 160, "description.short must be <= 160 chars")

    require(meta.get("category") in VALID_CATEGORIES,
            f"category must be one of {sorted(VALID_CATEGORIES)}")

    run = meta.get("run") or {}
    require(run.get("command"), "run.command is required")

    if meta.get("official"):
        problems.append("community apps must not set official:true")

    return problems


def make_record(meta_path):
    """Read, validate and normalize a single app.json into a gallery record.

    Returns (record, problems). `record` is None when the file is invalid.
    `path` is always POSIX and relative to CommunityApps/, so apps that live
    under samples/ get a ``../samples/...`` prefix that README links and
    index.html thumbnails resolve correctly."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {meta_path} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)

    folder = meta_path.parent.name
    problems = validate(meta, folder)
    if problems:
        return None, problems

    # Opt-out switch: an app.json may set "hidden": true to stay in the repo
    # but be excluded from apps.json, the gallery, and the README tables.
    if meta.get("hidden"):
        return None, []

    rel_path = Path(os.path.relpath(meta_path.parent, APPS_DIR)).as_posix()
    author = meta.get("author", {}) or {}
    desc = meta.get("description", {}) or {}
    run = meta.get("run", {}) or {}
    screenshot = meta.get("screenshot") or ""
    record = {
        "slug": meta.get("slug", folder),
        "name": meta.get("name", folder),
        "path": rel_path,
        "category": meta.get("category", "other"),
        "official": False,
        "author": author.get("name", "Unknown"),
        "authorGithub": author.get("github", ""),
        "short": desc.get("short", ""),
        "tags": meta.get("tags", []),
        "screenshot": f"{rel_path}/{screenshot}" if screenshot else "",
        "run": run.get("command", ""),
        "homepage": meta.get("homepage", ""),
        "source": meta.get("source", ""),
    }
    return record, []


def _iter_meta_paths():
    """Yield every candidate app.json path: CommunityApps/ (one level) plus
    samples/ (recursive). Ordering is deterministic for reproducible output."""
    for entry in sorted(APPS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in SKIP or entry.name.startswith("."):
            continue
        meta_path = entry / "app.json"
        if meta_path.exists():
            yield meta_path
    if SAMPLES_DIR.exists():
        for meta_path in sorted(SAMPLES_DIR.rglob("app.json")):
            yield meta_path


def discover_apps(strict):
    """Return normalized app records sorted by name. In strict mode, raise on any
    invalid app.json; otherwise print a warning and skip it. Apps are discovered
    from CommunityApps/ and recursively from samples/; a slug appearing in more
    than one place is an error (skipped in non-strict mode)."""
    apps = []
    errors = []
    seen_slugs = {}
    for meta_path in _iter_meta_paths():
        record, problems = make_record(meta_path)
        if problems:
            msg = f"{meta_path}:\n  - " + "\n  - ".join(problems)
            errors.append(msg)
            print(f"INVALID app.json {msg}", file=sys.stderr)
            continue
        if record is None:  # valid but "hidden": true — silently excluded
            continue

        slug = record["slug"]
        if slug in seen_slugs:
            msg = f"duplicate slug {slug!r}: {meta_path} conflicts with {seen_slugs[slug]}"
            errors.append(msg)
            print(f"DUPLICATE app.json {msg}", file=sys.stderr)
            continue
        seen_slugs[slug] = meta_path
        apps.append(record)

    if errors and strict:
        raise SystemExit(1)

    apps.sort(key=lambda a: a["name"].lower())
    return apps


def build_contributors(apps):
    """Unique contributors for the credits wall, most apps first."""
    seen = {}
    for a in apps:
        key = a["authorGithub"] or a["author"]
        if key not in seen:
            seen[key] = {"name": a["author"], "github": a["authorGithub"], "apps": 0}
        seen[key]["apps"] += 1
    contributors = list(seen.values())
    contributors.sort(key=lambda c: (-c["apps"], c["name"].lower()))
    return contributors


def render_manifest(apps, date):
    return {
        "generatedAt": date,
        "count": len(apps),
        "discussions": DISCUSSIONS_URL,
        "issues": ISSUES_URL,
        "contributors": build_contributors(apps),
        "apps": apps,
    }


def render_table(apps):
    lines = [
        "| App | Category | Author | Description |",
        "|-----|----------|--------|-------------|",
    ]
    if not apps:
        lines.append("| _Be the first — copy `_template/` and open a PR._ |  |  |  |")
        return "\n".join(lines)
    for a in apps:
        author = f"[@{a['authorGithub']}](https://github.com/{a['authorGithub']})" if a["authorGithub"] else a["author"]
        link = f"[{a['name']}]({a['path']}/)"
        short = a["short"].replace("|", "\\|")
        lines.append(f"| {link} | {a['category']} | {author} | {short} |")
    return "\n".join(lines)


def render_gallery(apps, cols=3):
    """Render an HTML image-grid of app cards that GitHub renders natively in
    README.md (GitHub does not run index.html's JavaScript, but it does render
    inline <table>/<img> with relative paths). This is the "app wall" visible
    straight from the repo file browser."""
    if not apps:
        return ("<p><em>No apps yet — copy <code>_template/</code>, fill "
                "<code>app.json</code>, and open a PR to appear here.</em></p>")

    def cell(a):
        thumb = a["screenshot"] or ""
        img = (f'<a href="{a["path"]}/"><img src="{thumb}" alt="{a["name"]}" width="260"></a>'
               if thumb else '<em>no screenshot</em>')
        author = (f'<a href="https://github.com/{a["authorGithub"]}">@{a["authorGithub"]}</a>'
                  if a["authorGithub"] else a["author"])
        short = a["short"]
        return (f'<td align="center" valign="top" width="33%">'
                f'{img}<br>'
                f'<a href="{a["path"]}/"><b>{a["name"]}</b></a><br>'
                f'<sub><code>{a["category"]}</code> · by {author}</sub><br>'
                f'<sub>{short}</sub>'
                f'</td>')

    rows = []
    for i in range(0, len(apps), cols):
        chunk = apps[i:i + cols]
        rows.append("  <tr>\n    " + "\n    ".join(cell(a) for a in chunk) + "\n  </tr>")
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def render_contributors_md(contributors):
    parts = []
    for c in contributors:
        if c["github"]:
            parts.append(f"[@{c['github']}](https://github.com/{c['github']})")
        else:
            parts.append(c["name"])
    return ", ".join(parts) if parts else "_Be the first — see below._"


def replace_block(text, start, end, new_inner):
    """Replace text between start/end markers; return unchanged if markers absent."""
    if start not in text or end not in text:
        return text
    head = text.split(start)[0]
    tail = text.split(end)[1]
    return f"{head}{start}\n{new_inner}\n{end}{tail}"


def write_or_check(path, new_content, check, changed):
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == new_content:
        return changed
    if check:
        print(f"OUT OF DATE: {path.name} would change. Run build_gallery.py.", file=sys.stderr)
        return True
    path.write_text(new_content, encoding="utf-8")
    print(f"wrote {path.name}")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=os.environ.get("GALLERY_DATE", "unknown"))
    parser.add_argument("--check", action="store_true", help="verify generated files are up to date (CI)")
    args = parser.parse_args()

    apps = discover_apps(strict=args.check)
    manifest = render_manifest(apps, args.date)
    changed = False

    # apps.json
    apps_json = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    changed = write_or_check(APPS_DIR / "apps.json", apps_json, args.check, changed)

    # README.md table + contributors block (only if the file has markers)
    readme_path = APPS_DIR / "README.md"
    if readme_path.exists():
        text = readme_path.read_text(encoding="utf-8")
        text = replace_block(text, TABLE_START, TABLE_END, render_table(apps))
        text = replace_block(text, GALLERY_START, GALLERY_END, render_gallery(apps))
        text = replace_block(text, CONTRIB_START, CONTRIB_END,
                             render_contributors_md(manifest["contributors"]))
        changed = write_or_check(readme_path, text, args.check, changed)

    if args.check and changed:
        raise SystemExit(1)
    print(f"OK: {len(apps)} app(s), {len(manifest['contributors'])} contributor(s).")


if __name__ == "__main__":
    main()
