"""Tijdzone van het hele lab (standaard Europe/Amsterdam, instelbaar via lab.timezone in topology.yaml).

FortiGates krijgen hem via de FortiManager-device-database (anders zet een installatie hem terug), FortiManager en
FortiAnalyzer via de CLI (daar is de waarde een index uit een lijst), de LXC's via /etc/localtime."""
from __future__ import annotations

import re

from . import console, faz, fmg
from .config import get_state, inventory
from .hostctl import host


def name(topo) -> str:
    return topo["lab"].get("timezone", "Europe/Amsterdam")


def use_for_process(topo):
    """Logtijden van de controller zelf in de lab-tijdzone, ook zonder herstart."""
    import os
    import time
    os.environ["TZ"] = name(topo)
    time.tzset()


def appliance(log, ip: str, label: str, tz: str):
    """FortiManager/FortiAnalyzer: 'set timezone' verwacht een index; zoek 'Amsterdam' in de lijst van de CLI zelf."""
    city = tz.split("/")[-1]
    cli = console.Cli(ip, get_state().get("admin_password", ""))
    try:
        cli.run("config system global")
        listing = cli.run("set timezone ?", timeout=30)
        m = re.search(rf"^\s*(\d+)\s+.*{re.escape(city)}", listing, re.M)
        if not m:
            cli.run("end")
            raise RuntimeError(f"tijdzone '{city}' niet in de lijst van {label}")
        cli.run(f"set timezone {m.group(1)}")
        out = cli.run("end", timeout=30, answer="y")
        status = cli.run("get system status", timeout=30)
        zone = re.search(r"Time Zone\s*:\s*(.+)", status)
        log(f"[{label}] tijdzone {m.group(1)}" + (f" ({zone.group(1).strip()})" if zone else "")
            + (" - " + out.strip()[-80:] if "rror" in out else ""))
    finally:
        cli.close()


def fortigates(log, topo, rpc, warn):
    """FortiGates: zet de tijdzone in de device-database van FortiManager (komt mee bij de volgende installatie)."""
    tz = name(topo)
    for s in topo["sites"]:
        try:
            rpc.update(f"/pm/config/device/{s['fgt_name']}/global/system/global", {"timezone": tz})
        except Exception as e:  # noqa: BLE001
            warn(f"tijdzone {s['fgt_name']}: {e}")
    log(f"[FortiManager] tijdzone {tz} in de device-database van {len(topo['sites'])} FortiGates")


def containers(log, topo):
    tz = name(topo)
    h = host()
    n = 0
    for node in inventory(topo):
        if h.status(node["vmid"]) == "running":
            h.pct_exec(node["vmid"], f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime && echo {tz} > /etc/timezone")
            n += 1
    log(f"tijdzone {tz} in {n} containers")
