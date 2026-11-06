#!/usr/bin/env python3
"""Plan and select log evidence for modem analysis workflows."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

from records import KINDS, canonical, prepare
from quts_extract import QutsError, doctor, open_capture

CATALOG = Path(__file__).resolve().parents[1] / "references/catalog.json"


def select_plan(catalog, names, all_skills=False):
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported catalog version")
    selected = catalog["skills"] if all_skills else []
    for name in names:
        found = [
            s
            for s in catalog["skills"]
            if name.casefold() in {a.casefold() for a in s["aliases"]}
        ]
        if len(found) != 1:
            raise ValueError(
                "Use a skill ID or an exact name from the requirements table"
            )
        if found[0] not in selected:
            selected.append(found[0])
    if not selected:
        raise ValueError("Choose --skill or --all")
    filters = {
        k: sorted({canonical(k, v) for s in selected for v in s["filters"].get(k, [])})
        for k in KINDS
    }
    return {
        "schema_version": 1,
        "combination": "OR",
        "skills": [{k: s[k] for k in ("id", "name")} for s in selected],
        "filters": {k: v for k, v in filters.items() if v},
        "keywords": sorted({w for s in selected for w in s["keywords"]}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("doctor", help="Check the installed QUTS SDK and service")
    p.add_argument("--support-path")
    p.add_argument("--connect", action="store_true")
    p = commands.add_parser("plan")
    p.add_argument("--skill", action="append", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--out", type=Path, required=True)
    for name in ("filter", "acquire"):
        p = commands.add_parser(name)
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--input", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        if name == "filter":
            p.add_argument("--format", choices=("text", "jsonl"), required=True)
            p.add_argument("--context", type=int, default=0)
            p.add_argument("--record-start")
        else:
            p.add_argument(
                "--decoder",
                help="Optional MODULE:FUNCTION override; defaults to bundled QUTS",
            )
            p.add_argument("--support-path")
            p.add_argument("--timeout", type=int, default=300)
            p.add_argument("--page-size", type=int, default=1000)
            p.add_argument(
                "--qshrink",
                type=Path,
                action="append",
                default=[],
                help="QShrink database for this capture; repeat for multiple files",
            )
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor(args.support_path, args.connect)))
        return 0
    if args.command == "plan":
        plan = select_plan(
            json.loads(CATALOG.read_text(encoding="utf-8")), args.skill, args.all
        )
        if args.out.exists():
            raise ValueError("Plan output already exists")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "skills": len(plan["skills"]),
                    "selectors": sum(map(len, plan["filters"].values())),
                    "keywords": len(plan["keywords"]),
                }
            )
        )
        return 0
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if args.command == "filter":
        report = prepare(
            plan, args.input, args.out, args.format, args.context, args.record_start
        )
    else:
        if args.input.suffix.lower() not in (".hdf", ".isf", ".qmdl", ".qmdl2"):
            raise ValueError("This adapter accepts HDF/ISF/QMDL/QMDL2")
        if args.out.exists():
            raise ValueError("Output already exists")
        if not any(plan.get("filters", {}).values()):
            raise ValueError(
                "Binary extraction needs typed IDs; use decoded logs for keyword-only plans"
            )
        if not args.decoder:
            with open_capture(
                log_path=args.input,
                filters=plan["filters"],
                support_path=args.support_path,
                view_timeout_seconds=args.timeout,
                page_size=args.page_size,
                qshrink_files=args.qshrink,
            ) as data:
                report = prepare(
                    plan,
                    args.input,
                    args.out,
                    "decoded",
                    context=0,
                    packets=data["packets"],
                )
            acquisition = data["acquisition"]
            acquisition["keyword_scope"] = "within_typed_ids_only"
            report["acquisition"] = acquisition
            if acquisition["unreadable_records"] or acquisition["missing_decoded_text"]:
                report["status"] = "partial"
        else:
            if args.qshrink or args.page_size != 1000:
                raise ValueError(
                    "--qshrink and --page-size require the bundled adapter"
                )
            module, sep, name = args.decoder.partition(":")
            if not sep:
                raise ValueError("Use MODULE:FUNCTION for --decoder")
            decoder = getattr(importlib.import_module(module), name)
            result = decoder(
                log_path=str(args.input.resolve(strict=True)),
                filters=plan["filters"],
                search_keywords=[],
                max_packets=0,
                view_timeout_seconds=args.timeout,
                support_path=args.support_path,
            )
            data = result if isinstance(result, dict) else result.to_dict()
            if not isinstance(data.get("packets"), list):
                raise ValueError("Decoder returned no records")
            report = prepare(
                plan,
                args.input,
                args.out,
                "decoded",
                context=0,
                packets=data["packets"],
            )
            if data.get("truncated") or data.get("ignored_filters"):
                report["status"] = "partial"
        (args.out / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": report["status"], "stats": report["stats"]}))
    return 0 if report["status"] == "prepared" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, AttributeError, QutsError) as exc:
        print("Unable to prepare records: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
