# Log preparation data contract

## Requirement plan

`plan --skill NAME [--skill NAME...] --out plan.json` reads the bundled catalog.
The result contains selected Skill IDs/names, typed `filters`, and literal
`keywords`. The selection is an OR across all IDs and keywords. `--all` includes
the union of all registered workflow selectors. A selected workflow does not
implicitly include selectors from another workflow. An empty plan needs
additional capture configuration before filtering; it must not silently select
an entire input file.

Namespaces are `LOG_PACKET` (16-bit hex), `QTRACE` (client/buffer), `QSH_EVENT`
(packed ID or client/buffer), `EVENT` (decimal), and `DEBUG_MSG` (SSID/level).
Do not substitute identifiers between namespaces. A trace buffer selector covers
its severity variants; a message selector retains its explicit severity level.
For packed QSH_EVENT IDs, the adapter selects the exact QEvent ID in the decoded
header because QUTS uses client/event pairs for its native ID field. Both forms
are retained in `packet_id` and `qsh_event_id` when available. Such selection
requires the message database to decode that header.

## Decoded input

TXT accepts UTF-8 with an optional BOM. Recognized record headers include packet
codes, tagged packet headers, timestamped packet headers, trace client/buffer
pairs, event rows and F3/logcat rows. Whole OTA trees and multiline tables remain
attached to their record. For another exporter, supply a verified boundary regex.
Untyped records can match keywords; they are not selected merely because their
format is unknown.

Each JSONL line is one object. Identifier fields are `packet_type`/`packet_id`
or `packetType`/`packetId`. Decoded text can use `text`, `parsed_text`, `parsedText`
or `decoded_payload_text`; `summary`, `packet_name`, `query_result_json` and
structured `fields` also participate in keyword matching. The selected object
is preserved in full. For example:

```json
{"packet_type":"LOG_PACKET","packet_id":"0xB821","parsed_text":"rrcSetupComplete","subscription_id":1}
```

## Files and status

Use a new output directory for each run. The original capture is unchanged.

| File | Contents |
|---|---|
| `records.jsonl` | Complete selected objects with source record/line positions and matching selectors |
| `filtered.txt` | Complete selected text, or a decoded text view of JSON records |
| `report.json` | Selection counts, observed selectors, input hash, status and binary acquisition diagnostics |
| `plan.json` | The exact requirement plan used |

Context defaults to zero adjacent records; `--context N` includes that many on
both sides of a match. Adjacent windows are deduplicated. Context records and
keyword matches can have IDs beyond the numeric selectors in the plan.

Exit 0 means records were prepared. Exit 2 means no selector hits or a decoder
reported incomplete extraction. Exit 1 indicates an input/configuration error.
These codes do not establish a root cause or certify capture-window completeness.
Keep boot/setup context in the original capture for registration and RRC analysis.

## Bundled QUTS extraction

Install QUTS with Python support and an active license on the extraction machine.
Run Python 3.10+ on the same OS as the local QUTS service. For a Windows QUTS
installation, run Windows Python, including when invoking it from WSL. The SDK
needs `thrift` and `six` in that interpreter; follow the installed SDK's dependency
versions when specified:

```bash
python -m pip install thrift six
python scripts/modem_log.py doctor --connect
python scripts/modem_log.py acquire --plan work/plan.json --input logs/case.hdf --out work/evidence
```

`acquire` defaults to the bundled `scripts/quts_extract.py`. It opens the
capture using `QutsClient.openLogSession`, includes every DIAG protocol, creates a
typed `DiagPacketFilter`, waits for the data view, and reads every page using
`getDataViewItems`. It rechecks returned IDs, preserves complete decoded records,
and removes its view/session when finished. It neither builds an image nor needs
another Skill or adapter repository. QUTS binaries and SDK are installed separately.

SDK discovery order is `--support-path`, `QUTS_SUPPORT_PYTHON`,
`QUTS_ROOT/Support/python`, then the standard installations below. An explicit
invalid path fails instead of silently choosing another SDK.

| OS | Default SDK locations |
|---|---|
| Windows | `C:/Program Files/Qualcomm/QUTS/Support/python`, then `C:/Program Files (x86)/Qualcomm/QUTS/Support/python` |
| Linux | `/opt/Qualcomm/QUTS/Support/python`, then `/opt/qcom/QUTS/Support/python` |

`doctor` checks imports; `doctor --connect` also opens and closes a service client.
It does not modify service or license settings. The service must be able to read
the capture and any message databases. Use a local readable path if a network
share is unavailable to the service account.

Use the same `acquire` command for HDF, ISF, QMDL or QMDL2 when the installed QUTS
version supports the format. `--timeout 300` bounds the data-view completion wait,
not every SDK call. `--page-size 1000` controls memory per read, not the total
record count. For QShrink traces, supply the database matching the captured build
with `--qshrink logs/msg_hash.qsr4`; repeat the option for multiple databases.

Binary acquisition uses the plan's typed IDs as its scope. Keywords are evaluated
within that scope; they cannot recover packets outside the acquisition mask.
Keyword-only plans require a decoded export and the `filter` command. No time or
SIM restriction is applied. `report.json` records view/selected/excluded counts,
unreadable records, records missing decoded text, DIAG protocols and cleanup
warnings. Metadata includes timestamps, subscription, protocol and source indices
when returned by QUTS. A summary without a LOG_PACKET payload is marked partial;
event/debug/trace records may use their decoded summary. A missing message database
or unsupported decoder may require re-extraction. Nonempty text alone does not
prove every field was successfully decoded; inspect decoder diagnostics in it.

Timeouts and interrupted/incomplete reads fail without publishing a new output
directory. Successful reads containing unreadable records or missing decoded text
produce exit 2 and `status: partial`; inspect those counters before analysis.
The script does not decode raw QDSS or convert PCAP/ADPL/dumps.

### Custom adapter override

Existing integrations may still specify `--decoder MODULE:FUNCTION`.
The callable receives `log_path`, typed `filters`, `search_keywords=[]`,
`max_packets=0`, `view_timeout_seconds` and optional `support_path`. It returns a
dictionary (or an object with `to_dict()`) containing `packets`, and optionally
`truncated` and `ignored_filters`. Records follow the JSONL contract above.
The override manages its own decoding lifecycle. `--qshrink` and `--page-size`
apply only to the bundled adapter.
