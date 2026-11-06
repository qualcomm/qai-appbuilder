---
name: modem-log-prefilter
description: Prepare packet, trace, event and keyword selections for modem analysis skills, then extract HDF/ISF captures through QUTS or filter decoded TXT/JSONL. Use for capture planning and log preparation before a modem analysis workflow.
tags: modem, hdf, isf, quts, log, prefilter
use_for: Planning which packets, traces and events a modem analysis skill needs; extracting HDF/ISF captures through QUTS; filtering decoded TXT/JSONL modem logs
---

# Modem log preparation

Select the intended analysis skills and prepare their log inputs using the bundled
requirements table. Commands below run from this Skill's directory with Python
3.10 or later. No additional Python packages are needed for decoded TXT/JSONL.
The record-boundary detection, selector matching, and complete-record retention
used by these commands are implemented in [scripts/records.py](scripts/records.py).

## Plan a capture

Use an exact Skill ID or name from
[requirements-matrix.md](references/requirements-matrix.md). Repeat `--skill` to
combine workflows, or use `--all` for the union of every registered workflow's
declared selectors.

```bash
python scripts/modem_log.py plan --skill Modem_NR_Camping --out work/plan.json
```

Packet IDs, trace client/buffer pairs, event IDs and message SSID/level pairs use
separate namespaces. The table is a declared-requirements catalog, not a
heuristic packet-discovery mechanism: selecting one workflow does not
automatically include selectors declared only by a different workflow. Combine
the relevant workflows or use `--all` when cross-workflow coverage is required.
Some workflows also require scenario-specific collection settings; an empty
selector list does not imply that no logs are needed.

## Audit SM8975 source coverage

When updating the catalog from an SM8975 skill package, verify that every source
workflow is registered before publishing the matrix:

```bash
python scripts/audit_catalog.py --skills-root path/to/sm8975-skills/tech/modem
```

Repeat `--skills-root` for additional source packages. The audit fails if a
source `SKILL.md` name is absent from catalog aliases. It does not infer packet
selectors from arbitrary prose; add selectors only when the source workflow
explicitly declares them in its capture requirements.

## Extract HDF/ISF captures

The bundled [adapter](scripts/quts_extract.py) calls QUTS directly. Install QUTS and its Python SDK on the
machine running extraction; no separate adapter project is needed. Verify setup,
then extract with the plan:

```bash
python scripts/modem_log.py doctor --connect
python scripts/modem_log.py acquire --plan work/plan.json --input logs/case.hdf --out work/evidence
```

Use the same command for `.isf` captures. If SDK discovery fails, pass
`--support-path "C:/Program Files/Qualcomm/QUTS/Support/python"`. For trace captures
requiring a matching message database, add `--qshrink logs/msg_hash.qsr4`.
Read [data-contract.md](references/data-contract.md) for setup, extraction scope
and status handling. Review `report.json` before treating extraction as complete.

## Prepare decoded logs

```bash
python scripts/modem_log.py filter --plan work/plan.json --input logs/decoded.txt --format text --out work/evidence
```

Use `--format jsonl` for one complete record per JSON line. Selection combines
IDs and literal case-insensitive keywords using OR. Matching records retain their
complete OTA messages, tables and nested fields. For additional surrounding
records, set `--context N`. Use `--record-start REGEX` only after confirming the
exporter's record boundaries.

Pass the resulting `records.jsonl`, `filtered.txt`, `report.json` and the original
capture to the analysis workflow. Zero matches do not establish that a modem
action did not occur. Keep the original capture for full-window correlation.

Native PCAP, ADPL and crash-dump workflows retain their original input requirements.
