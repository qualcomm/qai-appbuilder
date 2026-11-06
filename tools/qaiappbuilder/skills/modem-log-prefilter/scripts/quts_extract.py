"""Stream selected DIAG records through the installed QUTS Python SDK."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import importlib
import os
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
import uuid

from records import canonical, matches, selectors


class QutsError(RuntimeError):
    """Actionable acquisition or SDK setup error."""


def load_sdk(support_path=None):
    explicit = support_path or os.environ.get("QUTS_SUPPORT_PYTHON")
    if explicit:
        candidates = [Path(explicit).expanduser()]
    elif os.environ.get("QUTS_ROOT"):
        candidates = [Path(os.environ["QUTS_ROOT"]).expanduser() / "Support/python"]
    elif os.name == "nt":
        candidates = [
            Path(os.environ.get(key, default)) / "Qualcomm/QUTS/Support/python"
            for key, default in (
                ("ProgramFiles", "C:/Program Files"),
                ("ProgramFiles(x86)", "C:/Program Files (x86)"),
            )
        ]
    else:
        candidates = [
            Path(root) / "Support/python"
            for root in ("/opt/Qualcomm/QUTS", "/opt/qcom/QUTS")
        ]
    support = next(
        (p.resolve() for p in candidates if (p / "QutsClient.py").is_file()), None
    )
    if support is None:
        raise QutsError(
            "QUTS Python SDK not found. Install QUTS with Python support and set "
            "--support-path to its Support/python directory (or QUTS_SUPPORT_PYTHON)."
        )
    sys.path.insert(0, str(support))
    try:
        with redirect_stdout(sys.stderr):
            client = importlib.import_module("QutsClient")
            common = importlib.import_module("Common.ttypes")
            session = importlib.import_module("LogSession.ttypes")
    except ImportError as exc:
        raise QutsError(
            "Cannot import the QUTS SDK. Install its Python dependencies in this "
            "interpreter (python -m pip install thrift six), and verify --support-path."
        ) from exc
    return SimpleNamespace(
        client=client, common=common, session=session, support=str(support)
    )


def connect(sdk):
    try:
        return sdk.client.QutsClient("ModemLogPrefilter", disableQutsSignalHandler=True)
    except Exception as exc:
        if "license" in (type(exc).__name__ + str(exc)).lower():
            raise QutsError(
                "QUTS license is unavailable. Verify the QUTS license in Qualcomm "
                "Package Manager for the account running this command."
            ) from exc
        raise QutsError(
            "Cannot connect to QUTS. Verify the local QUTS service is running, "
            "its SDK version matches, and Python runs on the same OS as the service."
        ) from exc


def doctor(support_path=None, check_connection=False):
    with redirect_stdout(sys.stderr):
        sdk = load_sdk(support_path)
        result = {
            "sdk_import": "ok",
            "support_path": sdk.support,
            "connection": "not_checked",
        }
        if check_connection:
            client = connect(sdk)
            try:
                result["connection"] = "ok"
            finally:
                client.stop()
                del client
        return result


def packet_record(item, common):
    diag = getattr(item, "diagPacket", None)
    if diag is None or getattr(diag, "errorCode", 0) not in (None, 0):
        return None
    kind = common.DiagPacketType._VALUES_TO_NAMES.get(diag.packetType, "UNKNOWN")
    raw = str(diag.packetId or "")
    try:
        value = canonical(kind, raw)
    except ValueError:
        return None
    record = {"packet_type": kind, "packet_id": value, "raw_packet_id": raw}
    for source, target in (
        ("packetName", "packet_name"),
        ("timeStampString", "timestamp"),
        ("receiveTimeString", "receive_time"),
        ("summaryText", "summary"),
        ("parsedText", "parsed_text"),
        ("queryResultJson", "query_result_json"),
        ("subscriptionId", "subscription_id"),
        ("processorId", "processor_id"),
        ("sessionIndex", "session_index"),
        ("protocolIndex", "protocol_index"),
    ):
        field = getattr(diag, source, None)
        if field is not None:
            record[target] = field
    record["protocol_handle"] = getattr(item, "protocolHandle", None)
    if kind == "QSH_EVENT":
        # QUTS returns client/event in packetId; the full packed QEvent ID is
        # printed in the decoded header. Preserve both without guessing bits.
        for key in ("parsed_text", "summary"):
            lines = str(record.get(key, "")).splitlines()
            found = re.search(
                r"\bQEvent\s+(0x[0-9a-f]{1,8})\s*(?:\||$)",
                lines[0] if lines else "",
                re.I,
            )
            if found:
                record["qsh_event_id"] = canonical("QSH_EVENT", found[1])
                break
    return record


@contextmanager
def open_capture(
    *,
    log_path,
    filters,
    support_path=None,
    view_timeout_seconds=300,
    page_size=1000,
    qshrink_files=(),
):
    """Keep the view alive while the caller consumes its paginated iterator."""
    path = Path(log_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in (
        ".hdf",
        ".isf",
        ".qmdl",
        ".qmdl2",
    ):
        raise ValueError("Provide an HDF/ISF/QMDL/QMDL2 file")
    if view_timeout_seconds <= 0 or not 1 <= page_size <= 10000:
        raise ValueError("Use a positive timeout and a page size between 1 and 10000")
    normalized = selectors(
        {"schema_version": 1, "combination": "OR", "filters": filters}
    )
    normalized = {k: v for k, v in normalized.items() if v}
    if not normalized:
        raise ValueError(
            "Binary extraction needs typed IDs; use decoded logs for keyword-only plans"
        )
    hashes = [str(Path(p).resolve(strict=True)) for p in qshrink_files]
    state = {
        "engine": "QUTS",
        "scope": "typed_ids",
        "candidate_records": 0,
        "selected_records": 0,
        "excluded_records": 0,
        "unreadable_records": 0,
        "missing_decoded_text": 0,
        "warnings": [],
    }
    client = session = view = None
    with redirect_stdout(sys.stderr):
        sdk = load_sdk(support_path)
        common, logtypes = sdk.common, sdk.session
        mask = {}
        for kind, values in normalized.items():
            number = common.DiagPacketType._NAMES_TO_VALUES.get(kind)
            if number is None:
                raise QutsError(
                    "Installed QUTS SDK does not support packet type " + kind
                )
            # Numeric IDs stay in their own namespace. Composite wildcard syntax
            # is passed as 'x', the SDK's client/buffer wildcard notation.
            mask[number] = []
            packed_ids = []
            for value in values:
                if kind == "QSH_EVENT" and "/" not in value:
                    # SDK idOrName accepts client/event pairs, not full packed
                    # IDs. Select exact decoded QEvent headers in this type only.
                    packed_ids.append(format(int(value, 16), "X"))
                else:
                    mask[number].append(
                        common.DiagIdFilterItem(idOrName=value.replace("/*", "/x"))
                    )
            if packed_ids:
                # Use one expression: SDK versions can merge entries with the
                # same empty idOrName, retaining only the first expression.
                pattern = r"\bQEvent\s+0x0*(?:" + "|".join(packed_ids) + r")\s*(?:\||$)"
                mask[number].append(
                    common.DiagIdFilterItem(regexFilter=pattern, caseInsensitive=True)
                )
        diag_filter = common.DiagPacketFilter(idOrNameMask=mask)
        flags = 0
        for name in (
            "PARSED_TEXT",
            "SUMMARY_TEXT",
            "PACKET_NAME",
            "PACKET_ID",
            "PACKET_TYPE",
            "TIME_STAMP_STRING",
            "RECEIVE_TIME_STRING",
            "SUBSCRIPTION_ID",
            "PROCESSOR_ID",
            "SESSION_INDEX",
            "PROTOCOL_INDEX",
        ):
            flags |= getattr(common.DiagReturnFlags, name)
        config = logtypes.PacketReturnConfig(
            diagConfig=common.DiagReturnConfig(flags=flags)
        )
        try:
            client = connect(sdk)
            session = client.openLogSession([str(path)])
            if not session:
                raise QutsError(
                    "QUTS could not open this capture. Verify its format and service file access."
                )
            handles = sorted(
                {
                    p.protocolHandle
                    for d in session.getDeviceList()
                    for p in session.getProtocolList(d.deviceHandle)
                    if p.protocolType == common.ProtocolType.PROT_DIAG
                }
            )
            if not handles:
                raise QutsError("The capture has no DIAG protocol to extract")
            state["diag_protocols"] = handles
            for handle in handles:
                for filename in hashes:
                    status = session.loadQShrinkFile(handle, filename)
                    if status not in (None, 0):
                        raise QutsError(
                            "QUTS could not load a supplied QShrink database"
                        )
            view = "modem_prefilter_" + uuid.uuid4().hex
            done = threading.Event()

            def updated(name, _size, finished):
                if name == view and finished:
                    done.set()

            client.setOnDataViewUpdatedCallback(updated)
            status = session.createDataView(
                view,
                logtypes.DataPacketFilter(
                    protocolHandleList=handles, diagFilter=diag_filter
                ),
                config,
            )
            if status not in (None, 0):
                raise QutsError(
                    "QUTS rejected the data view/filter configuration (code "
                    + str(status)
                    + ")"
                )
            if not done.wait(view_timeout_seconds):
                raise QutsError(
                    "QUTS data view timed out. Check service/decoder readiness or increase --timeout."
                )
            total = int(session.getDataViewItemCount(view))
            if total < 0:
                raise QutsError("QUTS returned an invalid data view count")
            state["view_records"] = total

            def packets():
                begin = 0
                while begin < total:
                    count = min(page_size, total - begin)
                    window = logtypes.PacketRange(
                        indexType=logtypes.IndexType.DATA_VIEW_INDEX,
                        beginIndex=begin,
                        count=count,
                    )
                    items = session.getDataViewItems(view, window)
                    if not items or len(items) > count:
                        raise QutsError(
                            "QUTS returned an incomplete data view; rerun extraction"
                        )
                    # Some service versions return a short page; continue at the
                    # next actual index, never skip the missing part of a page.
                    for offset, item in enumerate(items):
                        state["candidate_records"] += 1
                        record = packet_record(item, common)
                        if record is None:
                            state["unreadable_records"] += 1
                            continue
                        # Recheck IDs because mixed SDK masks may return extras.
                        # Keywords must not admit packets outside the native mask.
                        if not matches(record, normalized, [])[0]:
                            state["excluded_records"] += 1
                            continue
                        record["data_view_index"] = begin + offset
                        decoded = record.get("parsed_text") or record.get(
                            "query_result_json"
                        )
                        if record["packet_type"] != "LOG_PACKET":
                            decoded = decoded or record.get("summary")
                        if not decoded or not str(decoded).strip():
                            state["missing_decoded_text"] += 1
                        state["selected_records"] += 1
                        yield record
                    begin += len(items)

            yield {"packets": packets(), "acquisition": state}
        except (QutsError, ValueError, OSError):
            raise
        except Exception as exc:
            raise QutsError(
                "QUTS extraction failed. Verify capture access, decoder/SDK compatibility "
                "and any required QShrink database. SDK error type: "
                + type(exc).__name__
            ) from exc
        finally:
            for obj, method, args in (
                (session, "removeDataView", (view,)),
                (session, "destroyLogSession", ()),
                (client, "stop", ()),
            ):
                if obj is not None and (method != "removeDataView" or view):
                    try:
                        status = getattr(obj, method)(*args)
                        if status not in (None, 0):
                            state["warnings"].append(
                                "QUTS cleanup returned an error: " + method
                            )
                    except Exception:
                        state["warnings"].append(
                            "Could not complete QUTS cleanup: " + method
                        )
            obj = session = client = None
