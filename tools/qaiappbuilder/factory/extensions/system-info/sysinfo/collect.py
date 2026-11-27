# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Read-only facts about this computer: CPU, memory, disks, network.

Every section is collected on its own and may fail on its own (``psutil``
raises on some systems, e.g. a permission error listing other users'
sockets): a failure becomes ``{"error": "..."}`` for that section and never
spoils the others. Lists are capped so the result stays small enough to hand
to a model.

Only the standard library and ``psutil`` (an app dependency) are used. The OS
is reached through :class:`Probe`, so tests can pass fake data.
"""

from __future__ import annotations

import ipaddress
import platform
import socket
import time
from collections.abc import Callable
from typing import Any, Final

__all__ = ["SECTIONS", "Probe", "collect"]

#: Sections ``collect`` knows, in output order.
SECTIONS: Final[tuple[str, ...]] = (
    "system",
    "cpu",
    "memory",
    "disk",
    "network",
    "connections",
    "listening",
)

MAX_LISTENING: Final[int] = 200
MAX_CONNECTIONS: Final[int] = 100
#: What "everything" means when no section is named: the compact summary. The two
#: long lists (open ports, connection rows) are returned only when asked for by name.
OVERVIEW: Final[tuple[str, ...]] = ("system", "cpu", "memory", "disk", "network")
SUMMARY_ONLY: Final[tuple[str, ...]] = ("connections", "listening")
_GIB: Final[int] = 1024**3
_MIB: Final[int] = 1024**2


class Probe:
    """The OS access, in one place. Replace it in tests."""

    def __init__(self) -> None:
        # Imported here: the module stays importable (and the tool can say so) without it.
        import psutil  # noqa: PLC0415

        self.psutil = psutil

    def process_name(self, pid: int | None) -> str | None:
        if not pid:
            return None
        try:
            return str(self.psutil.Process(pid).name())
        except Exception:  # noqa: BLE001 - gone, or not ours to read
            return None


def collect(
    sections: list[str] | None = None,
    *,
    probe: Probe | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The requested ``sections`` as plain JSON-serialisable data.

    No ``sections`` = the overview: system, cpu, memory, disk, network, plus the
    COUNTS of connections and open ports (not their long lists -- name
    ``connections`` / ``listening`` to get the rows).
    """
    named = sections is not None and len(sections) > 0
    wanted = [s for s in (sections if named else SECTIONS) if s in SECTIONS]
    unknown = [s for s in (sections or []) if s not in SECTIONS]
    p = probe or Probe()
    brief = not named  # the overview carries summaries of the two long lists
    builders: dict[str, Callable[[], Any]] = {
        "system": lambda: _system(p, now),
        "cpu": lambda: _cpu(p),
        "memory": lambda: _memory(p),
        "disk": lambda: _disk(p),
        "network": lambda: _network(p),
        "connections": lambda: _connections(p, rows=not brief),
        "listening": lambda: _listening(p, rows=not brief),
    }
    out: dict[str, Any] = {}
    for name in wanted:
        try:
            out[name] = builders[name]()
        except Exception as exc:  # noqa: BLE001 - one section never spoils the others
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    if unknown:
        out["ignored_sections"] = unknown
    return out


# -- sections ----------------------------------------------------------------


def _system(p: Probe, now: Callable[[], float]) -> dict[str, Any]:
    boot = float(p.psutil.boot_time())
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "uptime_hours": round(max(0.0, now() - boot) / 3600, 1),
    }
    battery = _try(p.psutil.sensors_battery)
    if battery is not None:
        info["battery"] = {"percent": round(battery.percent), "plugged_in": battery.power_plugged}
    return info


def _cpu(p: Probe) -> dict[str, Any]:
    ps = p.psutil
    freq = _try(ps.cpu_freq)
    per_core = ps.cpu_percent(interval=0.3, percpu=True)
    info: dict[str, Any] = {
        "model": platform.processor() or None,
        "physical_cores": ps.cpu_count(logical=False),
        "logical_cores": ps.cpu_count(logical=True),
        "usage_percent": round(sum(per_core) / len(per_core), 1) if per_core else None,
        "per_core_percent": [round(x, 1) for x in per_core],
    }
    if freq is not None and getattr(freq, "current", 0):
        info["frequency_mhz"] = round(freq.current)
        if getattr(freq, "max", 0):
            info["max_frequency_mhz"] = round(freq.max)
    return info


def _memory(p: Probe) -> dict[str, Any]:
    vm, sw = p.psutil.virtual_memory(), p.psutil.swap_memory()
    return {
        "total_gb": _gb(vm.total),
        "used_gb": _gb(vm.total - vm.available),
        "available_gb": _gb(vm.available),
        "used_percent": round(vm.percent, 1),
        "swap_total_gb": _gb(sw.total),
        "swap_used_gb": _gb(sw.used),
        "swap_used_percent": round(sw.percent, 1),
    }


def _disk(p: Probe) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for part in p.psutil.disk_partitions(all=False):
        opts = (part.opts or "").split(",")
        if "cdrom" in opts or not part.fstype:
            continue  # an empty optical drive / unmounted entry
        item: dict[str, Any] = {"device": part.device, "mount": part.mountpoint, "fs": part.fstype}
        try:
            usage = p.psutil.disk_usage(part.mountpoint)
        except Exception as exc:  # noqa: BLE001 - e.g. a disconnected network drive
            item["error"] = f"{type(exc).__name__}: {exc}"
        else:
            item.update(
                total_gb=_gb(usage.total),
                used_gb=_gb(usage.used),
                free_gb=_gb(usage.free),
                used_percent=round(usage.percent, 1),
            )
        disks.append(item)
    return disks


def _network(p: Probe) -> dict[str, Any]:
    ps = p.psutil
    stats = ps.net_if_stats()
    interfaces: list[dict[str, Any]] = []
    for name, addrs in ps.net_if_addrs().items():
        ips, mac = [], None
        for a in addrs:
            fam = getattr(a.family, "name", str(a.family))
            if fam in {"AF_LINK", "AF_PACKET"}:
                mac = a.address
            elif fam in ("AF_INET", "AF_INET6"):
                ips.append(a.address.split("%", 1)[0])
        st = stats.get(name)
        interfaces.append(
            {
                "name": name,
                "up": bool(st.isup) if st else None,
                "speed_mbps": int(st.speed) if st and st.speed else None,
                "mac": mac,
                "addresses": ips,
            }
        )
    # Active interfaces first (the ones that matter), then by name.
    interfaces.sort(key=lambda i: (not i["up"], i["name"].lower()))
    io = ps.net_io_counters()
    return {
        "interfaces": interfaces,
        "traffic_since_boot": {
            "sent_mb": round(io.bytes_sent / _MIB),
            "received_mb": round(io.bytes_recv / _MIB),
            "errors_in": io.errin,
            "errors_out": io.errout,
        },
    }


def _sockets(p: Probe) -> list[Any]:
    return list(p.psutil.net_connections(kind="inet"))


def _connections(p: Probe, *, rows: bool = True) -> dict[str, Any]:
    socks = _sockets(p)
    by_state: dict[str, int] = {}
    for c in socks:
        by_state[c.status] = by_state.get(c.status, 0) + 1
    established = [c for c in socks if c.status == "ESTABLISHED" and c.raddr]
    out: dict[str, Any] = {
        "total": len(socks),
        "by_state": dict(sorted(by_state.items())),
        "established_total": len(established),
    }
    if rows:
        out["established"] = [
            {
                "local": _addr(c.laddr),
                "remote": _addr(c.raddr),
                "process": p.process_name(c.pid),
                "pid": c.pid or None,
            }
            for c in established[:MAX_CONNECTIONS]
        ]
        out["truncated"] = len(established) > MAX_CONNECTIONS
    return out


def _listening(p: Probe, *, rows: bool = True) -> dict[str, Any]:
    socks = _sockets(p)
    # One row per (protocol, port, scope, process): the IPv4 and IPv6 wildcard of the
    # same service are one finding, not two.
    merged: dict[tuple[str, int, str, int | None], dict[str, Any]] = {}
    for c in socks:
        proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
        # TCP: LISTEN. UDP has no listen state: a bound socket with no peer.
        if proto == "tcp" and c.status != "LISTEN":
            continue
        if proto == "udp" and c.raddr:
            continue
        host, port = _split(c.laddr)
        scope = scope_of(host)
        key = (proto, port, scope, c.pid or None)
        row = merged.get(key)
        if row is None:
            merged[key] = {
                "protocol": proto,
                "port": port,
                "scope": scope,
                "addresses": [host],
                "process": p.process_name(c.pid),
                "pid": c.pid or None,
            }
        elif host not in row["addresses"]:
            row["addresses"].append(host)
    ports = sorted(
        merged.values(), key=lambda r: (r["scope"] != "network", r["port"], r["protocol"])
    )
    exposed = sum(1 for r in ports if r["scope"] == "network")
    out: dict[str, Any] = {
        "total": len(ports),
        "exposed_to_network": exposed,
        "local_only": len(ports) - exposed,
    }
    if rows:
        out["ports"] = ports[:MAX_LISTENING]
        out["truncated"] = len(ports) > MAX_LISTENING
    return out


# -- helpers -----------------------------------------------------------------


def scope_of(host: str) -> str:
    """``network`` when a socket bound to ``host`` can be reached from other
    machines, ``local`` when only from this one.

    A wildcard address (``0.0.0.0`` / ``::``) accepts connections on every
    interface, so it is ``network``; so is any non-loopback address.
    """
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return "network"  # unknown form: do not claim it is safe
    if ip.is_loopback:
        return "local"
    return "network"


def _split(addr: Any) -> tuple[str, int]:
    if hasattr(addr, "ip"):
        return str(addr.ip), int(addr.port)
    host, port = addr[0], addr[1]
    return str(host), int(port)


def _addr(addr: Any) -> str | None:
    if not addr:
        return None
    host, port = _split(addr)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _gb(n: float) -> float:
    return round(n / _GIB, 2)


def _try(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional detail (battery, frequency)
        return None
