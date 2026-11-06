"""Complete-record selection for decoded modem captures."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import Counter, deque
from pathlib import Path
from typing import Iterable

from typing import Any

KINDS = ("LOG_PACKET", "QTRACE", "QSH_EVENT", "EVENT", "DEBUG_MSG")


def canonical(kind: str, value: Any) -> str:
    text = str(value).strip().strip("[]\"' ")
    if kind == "KEYWORD":
        return text
    if kind in ("QTRACE", "QSH_EVENT", "DEBUG_MSG") and "/" in text:
        parts = [s.strip() for s in text.split("/")]
        if len(parts) not in (2, 3) or not parts[0].isdigit():
            raise ValueError(f"Invalid {kind} ID: {value}")
        if not parts[1].isdigit() and parts[1].lower() not in ("x", "*"):
            raise ValueError(f"Invalid {kind} ID: {value}")
        return f"{int(parts[0])}/{int(parts[1]) if parts[1].isdigit() else '*'}"
    if re.fullmatch(r"0x[0-9a-f]+", text, re.I):
        number = int(text, 16)
    elif text.isdigit():
        number = int(text, 10)
    else:
        raise ValueError(f"Non-numeric {kind} ID: {value}")
    if kind == "LOG_PACKET":
        if not 0 <= number <= 65535:
            raise ValueError(f"Not a 16-bit LOG_PACKET ID: {value}")
        return f"0x{number:04X}"
    if kind == "QSH_EVENT":
        return f"0x{number:X}"
    return str(number)


PAIR_HEADER = re.compile(
    r"\[\s*(\d+\s*/\s*\d+(?:\s*/\s*\d+)?)\s*\]\s*(QTRACE|QSH_EVENT|QEVENT|DEBUG_MSG|MSG)\b",
    re.I,
)
TIME = re.compile(
    r"^(?:\d{4}[-/]\d{2}[-/]\d{2}[ T]|\d{4}\s+[A-Za-z]{3}\s+\d{1,2}\s+|\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+"
)
ALIASES = {
    "QTRACES": "QTRACE",
    "QEVENT": "QSH_EVENT",
    "QEVENTS": "QSH_EVENT",
    "MSG": "DEBUG_MSG",
    "LOG": "LOG_PACKET",
    "EVENT_REPORT": "EVENT",
}


def packet_kind(value: str) -> str:
    kind = value.upper().replace("-", "_").replace(" ", "_")
    return ALIASES.get(kind, kind)


def header(line: str) -> tuple[str, str] | None:
    """Only inspect a record's header, never hex in its payload."""
    text = line.strip()
    pair = PAIR_HEADER.search(text)
    if pair and (pair.start() == 0 or TIME.match(text)):
        kind = packet_kind(pair[2])
        return kind, canonical(kind, pair[1])
    timestamp = TIME.match(text)
    if timestamp:
        text = text[timestamp.end() :].lstrip()
    tagged = re.match(r"\[LOG_PACKET_(0x[0-9a-f]{4})\]", text, re.I)
    if tagged:
        return "LOG_PACKET", canonical("LOG_PACKET", tagged[1])
    event = re.match(r"(?:\[(\d{3,5})\]\s*|(\d{3,5})\s+)(?:EVENT\b|EVENT_)", text)
    if event:
        return "EVENT", canonical("EVENT", event[1] or event[2])
    qevent = re.match(r"(?:QEvent|QSH_EVENT)\s+(0x[0-9a-f]+)\b", text, re.I)
    if qevent:
        return "QSH_EVENT", canonical("QSH_EVENT", qevent[1])
    if re.match(r"^\S+\.[ch](?:pp)?\s+\d+\s+[A-Z]\s+Sub-ID:", text):
        qevent = re.search(r"\bQEvent\s+(0x[0-9a-f]+)\b", text, re.I)
        if qevent:
            return "QSH_EVENT", canonical("QSH_EVENT", qevent[1])
    packet = re.match(r"\[?(0x[0-9a-f]{4})\]?\s+", text, re.I)
    if packet:
        return "LOG_PACKET", canonical("LOG_PACKET", packet[1])
    return None


def record_start(line: str) -> bool:
    if header(line):
        return True
    # Logcat or QCAT F3 rows. Arbitrary timestamps in a nested packet/table
    # are not treated as record boundaries.
    return bool(
        re.match(
            r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+[VDIWEF]\s", line
        )
        or re.match(r"^\s*\S+\.[ch](?:pp)?\s+\d+\s+[A-Z]\s+Sub-ID:", line)
    )


def text_records(path: Path, boundary: str | None = None) -> Iterable[dict]:
    custom = re.compile(boundary) if boundary else None
    start_line, chunks = 1, []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line_no, line in enumerate(handle, 1):
            if "\0" in line:
                raise ValueError("Binary bytes in TXT input; provide decoded text")
            is_start = bool(custom.search(line)) if custom else record_start(line)
            if chunks and is_start:
                yield text_record("".join(chunks), start_line, line_no - 1)
                chunks, start_line = [], line_no
            chunks.append(line)
        if chunks:
            yield text_record("".join(chunks), start_line, line_no)


def text_record(text: str, start: int, end: int) -> dict:
    record = {"text": text}
    typed = header(text.splitlines()[0])
    if typed:
        record.update(packet_type=typed[0], packet_id=typed[1])
    return {"record": record, "start_line": start, "end_line": end}


def jsonl_records(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL line {line_no} is not a record object")
            if "record" in record and "source" in record:
                raise ValueError(
                    "Use the original decoded records, not an already-filtered records.jsonl envelope"
                )
            yield {"record": record, "start_line": line_no, "end_line": line_no}


def decoded_text(record: dict) -> str:
    fields = (
        "text",
        "packet_name",
        "summary",
        "parsed_text",
        "parsedText",
        "decoded_payload_text",
        "query_result_json",
        "fields",
    )
    return "\n".join(str(record[k]) for k in fields if record.get(k) is not None)


def display_text(record: dict) -> str:
    metadata = {
        k: record[k]
        for k in (
            "timestamp",
            "receive_time",
            "packet_type",
            "packet_id",
            "subscription_id",
            "sub_id",
        )
        if k in record
    }
    return (
        json.dumps(metadata, ensure_ascii=False) + "\n" if metadata else ""
    ) + decoded_text(record)


def selectors(plan: dict) -> dict[str, list[str]]:
    if plan.get("schema_version") != 1 or plan.get("combination") != "OR":
        raise ValueError("Unsupported plan schema or filter combination")
    result = {}
    for kind, values in plan.get("filters", {}).items():
        if kind not in KINDS or not isinstance(values, list):
            raise ValueError(f"Invalid plan filter {kind}")
        result[kind] = [canonical(kind, value) for value in values]
    return result


def matches(record: dict, filters: dict, keywords: list[str]) -> tuple[list[str], bool]:
    kind = packet_kind(str(record.get("packet_type", record.get("packetType", ""))))
    raw_id = record.get("packet_id", record.get("packetId", ""))
    ids = []
    try:
        ids.append(canonical(kind, raw_id))
        if record.get("raw_packet_id"):
            ids.append(canonical(kind, record["raw_packet_id"]))
    except ValueError:
        pass
    if kind == "QSH_EVENT" and record.get("qsh_event_id"):
        try:
            ids.append(canonical(kind, record["qsh_event_id"]))
        except ValueError:
            pass
    if not kind or not ids:
        first = decoded_text(record).splitlines()
        typed = header(first[0]) if first else None
        if typed:
            kind, value = typed
            ids.append(value)
    reasons = []
    for value in filters.get(kind, []):
        if (
            value in ids
            or value.endswith("/*")
            and any(i.startswith(value[:-1]) for i in ids)
        ):
            reasons.append(f"{kind}:{value}")
    text = decoded_text(record).casefold()
    reasons.extend("KEYWORD:" + word for word in keywords if word.casefold() in text)
    return reasons, kind not in KINDS or not ids


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def prepare(plan, input_path, out, fmt, context=2, boundary=None, packets=None):
    filters = selectors(plan)
    keywords = plan.get("keywords", [])
    if context < 0 or not all(isinstance(k, str) and k for k in keywords):
        raise ValueError("Invalid selection parameters")
    if not filters and not keywords:
        raise ValueError("No selectors available for the selected workflows")
    input_path, out = input_path.resolve(strict=True), out.resolve()
    if out.exists():
        raise ValueError("Output already exists; use a new directory")
    if packets is not None:
        records = ({"record": row, "index": i} for i, row in enumerate(packets, 1))
    elif fmt == "text":
        records = text_records(input_path, boundary)
    elif fmt == "jsonl":
        records = jsonl_records(input_path)
    else:
        raise ValueError("Unsupported decoded format")
    counts = Counter()
    stats = Counter(total_records=0, matched_records=0, retained_records=0)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".modem-records-", dir=out.parent) as name:
        stage = Path(name)
        pending, after = deque(maxlen=context), 0
        with (stage / "records.jsonl").open("w", encoding="utf-8") as rows, (
            stage / "filtered.txt"
        ).open("w", encoding="utf-8") as decoded:

            def emit(item, reasons):
                rows.write(
                    json.dumps(
                        {
                            "record": item["record"],
                            "match_reasons": reasons,
                            "source": {k: v for k, v in item.items() if k != "record"},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                text = (
                    item["record"].get("text")
                    if fmt == "text"
                    else display_text(item["record"])
                )
                decoded.write(str(text or ""))
                if fmt != "text":
                    decoded.write("\n\n")
                stats["retained_records"] += 1

            for i, item in enumerate(records, 1):
                item["index"] = i
                stats["total_records"] += 1
                reasons, _ = matches(item["record"], filters, keywords)
                if reasons:
                    counts.update(reasons)
                    stats["matched_records"] += 1
                    for previous in pending:
                        emit(previous, ["adjacent_context"])
                    pending.clear()
                    emit(item, reasons)
                    after = context
                elif after:
                    emit(item, ["adjacent_context"])
                    after -= 1
                else:
                    pending.append(item)
        report = {
            "status": "prepared" if stats["matched_records"] else "no_selector_hits",
            "stats": dict(stats),
            "selector_counts": dict(counts),
            "input_sha256": file_hash(input_path),
            "context_records_each_side": context,
            "source_window_verified": False,
        }
        (stage / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "plan.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        stage.rename(out)
    return report
