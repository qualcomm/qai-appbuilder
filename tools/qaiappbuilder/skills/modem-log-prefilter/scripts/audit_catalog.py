#!/usr/bin/env python3
"""Verify that every SM8975 modem source skill is registered in catalog.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "references" / "catalog.json"
NAME = re.compile(r"^name:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)


def source_name(path: Path) -> str:
    match = NAME.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"No frontmatter name in {path}")
    return match.group(1).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--skills-root",
        type=Path,
        action="append",
        required=True,
        help="SM8975 tech/modem source directory; repeat for another package",
    )
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported catalog version")
    aliases = {
        alias.casefold()
        for entry in catalog["skills"]
        for alias in entry.get("aliases", [])
    }
    sources = []
    seen = set()
    for root in args.skills_root:
        skill_paths = sorted(
            path
            for path in root.resolve(strict=True).rglob("*")
            if path.is_file() and path.name.casefold() == "skill.md"
        )
        for path in skill_paths:
            name = source_name(path)
            key = (name.casefold(), str(path))
            if key not in seen:
                sources.append({"name": name, "path": str(path)})
                seen.add(key)
    missing = [item for item in sources if item["name"].casefold() not in aliases]
    print(
        json.dumps(
            {
                "catalog_entries": len(catalog["skills"]),
                "source_skills": len(sources),
                "missing_source_skills": missing,
            },
            indent=2,
        )
    )
    return 1 if missing else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("Catalog audit failed: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
