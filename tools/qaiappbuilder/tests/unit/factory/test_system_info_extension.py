# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# ruff: noqa: S104 - the wildcard address (0.0.0.0) is exactly what is under test here
"""Built-in ``system-info``: the collector against fake OS data, the tool and
buttons through the real extension host, and a smoke test on this machine."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import socket
import sys
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from qai.platform.user_extensions import UserExtensionHost, module_name_for, run_action

ROOT = Path(__file__).resolve().parents[3]
EXT = ROOT / "factory" / "extensions" / "system-info"


def _load_collect():
    spec = importlib.util.spec_from_file_location(
        "sysinfo_collect_under_test", EXT / "sysinfo" / "collect.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


col = _load_collect()

Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")
Snic = namedtuple("Snic", "family address netmask broadcast ptp")
Stat = namedtuple("Stat", "isup duplex speed mtu")
Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free percent")
VM = namedtuple("VM", "total available percent used free")
SW = namedtuple("SW", "total used free percent sin sout")
IO = namedtuple("IO", "bytes_sent bytes_recv errin errout")
Freq = namedtuple("Freq", "current min max")
Batt = namedtuple("Batt", "percent secsleft power_plugged")
Fam = namedtuple("Fam", "name")

GB = 1024**3


class FakePs:
    """A ``psutil`` stand-in with just what the collector calls."""

    def __init__(self, conns=(), fail=()):
        self._conns, self._fail = list(conns), set(fail)

    def _check(self, name):
        if name in self._fail:
            raise PermissionError(f"{name} denied")

    def boot_time(self):
        return 1000.0

    def sensors_battery(self):
        return Batt(80.4, 3600, False)

    def cpu_percent(self, interval=None, percpu=False):
        self._check("cpu")
        return [10.0, 30.0]

    def cpu_count(self, logical=True):
        return 4 if logical else 2

    def cpu_freq(self):
        return Freq(2400.4, 0, 3200.0)

    def virtual_memory(self):
        self._check("memory")
        return VM(16 * GB, 4 * GB, 75.0, 12 * GB, 1 * GB)

    def swap_memory(self):
        return SW(2 * GB, GB // 2, GB, 25.0, 0, 0)

    def disk_partitions(self, all=False):
        return [
            Part("C:\\", "C:\\", "NTFS", "rw,fixed"),
            Part("D:\\", "D:\\", "", "cdrom"),  # an empty optical drive
            Part("E:\\", "E:\\", "NTFS", "rw,fixed"),
        ]

    def disk_usage(self, mount):
        if mount == "E:\\":
            raise OSError("not ready")
        return Usage(500 * GB, 125 * GB, 375 * GB, 25.0)

    def net_if_addrs(self):
        return {
            "Ethernet": [Snic(Fam("AF_LINK"), "AA-BB", None, None, None)],
            "Wi-Fi": [
                Snic(Fam("AF_LINK"), "11-22", None, None, None),
                Snic(Fam("AF_INET"), "192.168.1.5", None, None, None),
                Snic(Fam("AF_INET6"), "fe80::1%12", None, None, None),
            ],
        }

    def net_if_stats(self):
        return {"Ethernet": Stat(False, 0, 0, 1500), "Wi-Fi": Stat(True, 2, 866, 1500)}

    def net_io_counters(self):
        return IO(5 * 1024**2, 7 * 1024**2, 1, 2)

    def net_connections(self, kind="inet"):
        self._check("sockets")
        return list(self._conns)

    class Process:
        def __init__(self, pid):
            if pid == 999:
                raise ProcessLookupError(pid)
            self.pid = pid

        def name(self):
            return f"proc{self.pid}.exe"


def _probe(**kw):
    p = col.Probe.__new__(col.Probe)
    p.psutil = FakePs(**kw)
    return p


def tcp(laddr, status="LISTEN", raddr=(), pid=10):
    return Conn(-1, 2, socket.SOCK_STREAM, laddr, raddr, status, pid)


def udp(laddr, pid=10):
    return Conn(-1, 2, socket.SOCK_DGRAM, laddr, (), "NONE", pid)


# -- the collector -------------------------------------------------------------


def test_the_overview_has_the_compact_sections_and_only_counts_of_the_long_lists() -> None:
    out = col.collect(probe=_probe(conns=[tcp(Addr("0.0.0.0", 80))]), now=lambda: 4600.0)
    assert list(out) == ["system", "cpu", "memory", "disk", "network", "connections", "listening"]
    assert "ports" not in out["listening"] and "established" not in out["connections"]
    assert out["listening"]["total"] == 1 and out["connections"]["total"] == 1
    assert out["system"]["uptime_hours"] == 1.0
    assert out["system"]["battery"] == {"percent": 80, "plugged_in": False}


def test_cpu_memory_disk_numbers() -> None:
    out = col.collect(["cpu", "memory", "disk"], probe=_probe())
    cpu = {k: v for k, v in out["cpu"].items() if k != "model"}  # the model string is the host's
    assert cpu == {
        "physical_cores": 2,
        "logical_cores": 4,
        "usage_percent": 20.0,
        "per_core_percent": [10.0, 30.0],
        "frequency_mhz": 2400,
        "max_frequency_mhz": 3200,
    }
    assert out["memory"] == {
        "total_gb": 16.0,
        "used_gb": 12.0,
        "available_gb": 4.0,
        "used_percent": 75.0,
        "swap_total_gb": 2.0,
        "swap_used_gb": 0.5,
        "swap_used_percent": 25.0,
    }
    c, e = out["disk"]  # the empty CD drive is skipped
    assert c["total_gb"] == 500.0 and c["free_gb"] == 375.0 and c["used_percent"] == 25.0
    assert (
        e["device"] == "E:\\" and "error" in e and "total_gb" not in e
    )  # unreadable drive: reported, not fatal


def test_network_lists_active_interfaces_first_with_clean_addresses() -> None:
    net = col.collect(["network"], probe=_probe())["network"]
    wifi, eth = net["interfaces"]
    assert wifi["name"] == "Wi-Fi" and wifi["up"] is True and wifi["speed_mbps"] == 866
    assert wifi["addresses"] == ["192.168.1.5", "fe80::1"]  # the %zone suffix is dropped
    assert wifi["mac"] == "11-22" and eth["up"] is False and eth["addresses"] == []
    assert net["traffic_since_boot"] == {
        "sent_mb": 5,
        "received_mb": 7,
        "errors_in": 1,
        "errors_out": 2,
    }


@pytest.mark.parametrize(
    ("host", "scope"),
    [
        ("0.0.0.0", "network"),
        ("::", "network"),
        ("192.168.1.5", "network"),
        ("10.0.0.2", "network"),
        ("fe80::1", "network"),
        ("fe80::1%12", "network"),
        ("127.0.0.1", "local"),
        ("::1", "local"),
        ("127.1.2.3", "local"),
        ("not-an-ip", "network"),  # unknown: never claim "safe"
    ],
)
def test_scope_of(host: str, scope: str) -> None:
    assert col.scope_of(host) == scope


def test_listening_ports_scope_merge_and_order() -> None:
    conns = [
        tcp(Addr("127.0.0.1", 5000), pid=1),
        tcp(Addr("0.0.0.0", 8080), pid=2),
        tcp(Addr("::", 8080), pid=2),  # same service, IPv6 wildcard: one finding
        tcp(Addr("192.168.1.5", 22), pid=3),
        tcp(
            Addr("10.0.0.1", 443), status="ESTABLISHED", raddr=Addr("1.1.1.1", 443)
        ),  # not a listener
        udp(Addr("0.0.0.0", 123), pid=4),
        udp(Addr("127.0.0.1", 5353), pid=5),
        Conn(
            -1, 2, socket.SOCK_DGRAM, Addr("0.0.0.0", 9), Addr("1.2.3.4", 9), "NONE", 6
        ),  # connected UDP
        tcp(Addr("0.0.0.0", 7), pid=999),  # process that vanished
    ]
    res = col.collect(["listening"], probe=_probe(conns=conns))["listening"]
    assert (res["total"], res["exposed_to_network"], res["local_only"]) == (6, 4, 2)
    ports = res["ports"]
    assert [(r["port"], r["protocol"], r["scope"]) for r in ports] == [
        (7, "tcp", "network"),
        (22, "tcp", "network"),
        (123, "udp", "network"),
        (8080, "tcp", "network"),
        (5000, "tcp", "local"),
        (5353, "udp", "local"),
    ]  # network first, then by port
    web = next(r for r in ports if r["port"] == 8080)
    assert (
        web["addresses"] == ["0.0.0.0", "::"] and web["process"] == "proc2.exe" and web["pid"] == 2
    )
    assert (
        next(r for r in ports if r["port"] == 7)["process"] is None
    )  # unreadable process: null, not invented


def test_connections_summary_rows_and_ipv6_format() -> None:
    conns = [
        tcp(Addr("10.0.0.1", 5000), "ESTABLISHED", Addr("8.8.8.8", 443), pid=7),
        tcp(Addr("fe80::1", 5001), "ESTABLISHED", Addr("2001:db8::1", 443), pid=8),
        tcp(Addr("10.0.0.1", 5002), "TIME_WAIT", Addr("8.8.4.4", 80), pid=0),
        tcp(Addr("0.0.0.0", 80)),
    ]
    brief = col.collect(["connections"], probe=_probe(conns=conns))["connections"]
    assert brief["total"] == 4 and brief["by_state"] == {
        "ESTABLISHED": 2,
        "LISTEN": 1,
        "TIME_WAIT": 1,
    }
    assert brief["established_total"] == 2 and len(brief["established"]) == 2
    first = brief["established"][0]
    assert first == {
        "local": "10.0.0.1:5000",
        "remote": "8.8.8.8:443",
        "process": "proc7.exe",
        "pid": 7,
    }
    assert brief["established"][1]["remote"] == "[2001:db8::1]:443"


def test_the_long_lists_are_capped_and_say_so(monkeypatch) -> None:
    monkeypatch.setattr(col, "MAX_LISTENING", 3)
    monkeypatch.setattr(col, "MAX_CONNECTIONS", 2)
    conns = [tcp(Addr("0.0.0.0", 1000 + i), pid=i + 1) for i in range(6)]
    conns += [
        tcp(Addr("10.0.0.1", 6000 + i), "ESTABLISHED", Addr("8.8.8.8", 443)) for i in range(5)
    ]
    out = col.collect(["listening", "connections"], probe=_probe(conns=conns))
    assert len(out["listening"]["ports"]) == 3 and out["listening"]["truncated"] is True
    assert out["listening"]["total"] == 6  # the true count is still reported
    assert (
        len(out["connections"]["established"]) == 2 and out["connections"]["established_total"] == 5
    )


def test_one_failing_section_does_not_spoil_the_rest() -> None:
    out = col.collect(probe=_probe(fail={"sockets", "memory"}))
    assert out["memory"] == {"error": "PermissionError: memory denied"}
    assert out["connections"]["error"].startswith("PermissionError") and "error" in out["listening"]
    assert out["cpu"]["logical_cores"] == 4 and out["disk"] and out["network"]["interfaces"]


def test_unknown_sections_are_reported_not_fatal() -> None:
    out = col.collect(["memory", "bogus"], probe=_probe())
    assert list(out) == ["memory", "ignored_sections"] and out["ignored_sections"] == ["bogus"]
    only_unknown = col.collect(["bogus"], probe=_probe())
    assert only_unknown == {"ignored_sections": ["bogus"]}


def test_the_result_is_json_serialisable_and_small() -> None:
    conns = [tcp(Addr("0.0.0.0", p), pid=p) for p in range(1, 300)]
    text = json.dumps(col.collect(probe=_probe(conns=conns)), ensure_ascii=False)
    assert len(text) < 6000, "the overview must stay small enough for a model"


# -- through the real extension host --------------------------------------------


class Sink:
    def __init__(self):
        self.tools = {}

    def tool_names(self):
        return frozenset(self.tools)

    def register(self, name, handler, *, schema):
        self.tools[name] = (handler, schema)

    def unregister(self, name):
        self.tools.pop(name, None)

    def handler_for(self, name):
        return self.tools.get(name, (None,))[0]


@pytest.fixture
def host(tmp_path: Path):
    scan = tmp_path / "extensions"
    scan.mkdir()
    sink = Sink()
    # Only this extension, loaded from a private copy of the built-in folder, so the
    # test never touches the other built-ins or leaves modules behind.
    builtin = tmp_path / "builtin"
    shutil.copytree(EXT, builtin / "system-info", ignore=shutil.ignore_patterns("__pycache__"))
    prefix = module_name_for("system-info")
    before = {k for k in sys.modules if k == prefix or k.startswith(prefix + ".")}
    h = UserExtensionHost(scan, sink, data_root=tmp_path, builtin_root=builtin)
    h.load_all()
    try:
        yield h, sink
    finally:
        h.unload_all()
        for key in list(sys.modules):
            if (key == prefix or key.startswith(prefix + ".")) and key not in before:
                sys.modules.pop(key, None)


def _call(sink: Sink, **arguments):
    req = SimpleNamespace(
        arguments=arguments, tool_name="system_info", conversation_id="c", tab_id="t"
    )
    return json.loads(asyncio.run(sink.tools["system_info"][0](req)))


def test_the_extension_loads_clean_with_a_read_only_tool(host) -> None:
    h, sink = host
    st = h.status("system-info")
    assert st.state == "loaded" and st.error == "" and list(st.warnings) == []
    assert st.tools == ("system_info",) and h.is_builtin("system-info")
    assert h.tools["system_info"].declaration.group == "read"  # it only reads
    schema = sink.tools["system_info"][1]["function"]
    assert set(schema["parameters"]["properties"]["sections"]["items"]["enum"]) == set(col.SECTIONS)


def test_the_menu_entry_has_a_name_and_buttons_for_every_language(host) -> None:
    h, _ = host
    entry = next(e for e in h.ui_entries() if e["name"] == "system-info")
    assert entry["labels"] == {"en": "System Info", "zh-CN": "本机信息", "zh-TW": "本機資訊"}
    assert entry["label"] == "System Info"
    assert {a["id"] for a in entry["actions"]} == {"overview", "exposed_ports"}
    for a in entry["actions"]:
        assert set(a["labels"]) == {"en", "zh-CN", "zh-TW"}


def test_a_button_sends_a_prompt_that_names_the_tool(host) -> None:
    h, _ = host
    manifest = h.manifest("system-info")
    for action in manifest.ui.actions:
        found = h.action_handler("system-info", action.id)
        assert found is not None
        decl, handler = found
        result = asyncio.run(run_action(decl, handler, {}))
        assert result.ok and "system_info" in result.prompt


def test_the_active_prompt_is_registered(host) -> None:
    h, _ = host
    assert h.status("system-info").prompts == {"active": 1}


# -- this machine -----------------------------------------------------------------


def test_smoke_on_this_machine(host) -> None:
    pytest.importorskip("psutil")
    _h, sink = host
    out = _call(sink)
    for section in ("system", "cpu", "memory", "disk", "network", "connections", "listening"):
        assert "error" not in out[section], f"{section}: {out[section]}"  # every section reads here
    assert out["cpu"]["logical_cores"] >= 1 and 0 <= out["cpu"]["usage_percent"] <= 100
    assert out["memory"]["total_gb"] > 0 and out["disk"] and out["disk"][0]["total_gb"] > 0
    assert out["network"]["interfaces"]
    rows = _call(sink, sections=["listening"])["listening"]
    assert rows["total"] == rows["exposed_to_network"] + rows["local_only"]
    assert len(rows["ports"]) == min(rows["total"], col.MAX_LISTENING)
    assert all(r["scope"] in ("network", "local") for r in rows["ports"])
    # A port this test opens itself must be found, as local-only.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        mine = [
            r
            for r in _call(sink, sections=["listening"])["listening"]["ports"]
            if r["port"] == port
        ]
    assert mine and mine[0]["scope"] == "local" and mine[0]["protocol"] == "tcp" and mine[0]["pid"]
    # ...and one bound to every interface as exposed.
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        port = s.getsockname()[1]
        mine = [
            r
            for r in _call(sink, sections=["listening"])["listening"]["ports"]
            if r["port"] == port
        ]
    assert mine and mine[0]["scope"] == "network"
    shutil.rmtree(EXT / "sysinfo" / "__pycache__", ignore_errors=True)
