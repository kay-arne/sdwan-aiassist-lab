"""Demo-cockpit: scenario's uitvoeren (links verslechteren, verkeer, hubs, iperf) en live SD-WAN-status."""
from __future__ import annotations

import json
import time

import yaml

from . import fmg
from .config import ROOT, inventory, load_topology, node, nodes_by_role, site_map
from .hostctl import host
from .jsonrpc import RpcError

SCEN_FILE = ROOT / "lab" / "scenarios.yaml"


def catalog() -> dict:
    return yaml.safe_load(SCEN_FILE.read_text())


def scenario(sid: str) -> dict:
    return next(s for s in catalog()["scenarios"] if s["id"] == sid)


# --------------------------------------------------------------------------------------
# ISP-links
# --------------------------------------------------------------------------------------
def _iface(topo, site_id: str, transport: str) -> str:
    """Interface op de ISP-router voor de toegangslink van een site."""
    from .deployer import isp_nets
    key = f"wan{'1' if transport.upper() == 'A' else '2'}-{site_id}"
    for ifc, seg in isp_nets(topo):
        if seg["id"] == key:
            return ifc
    raise ValueError(f"geen link voor {site_id} transport {transport}")


def _isp(topo) -> dict:
    return nodes_by_role(topo, "isp")[0]


def impair(log, topo, site, transport, delay=None, jitter=None, loss=None, rate=None):
    args = f"linkctl set {_iface(topo, site, transport)}"
    for k, v in (("delay", delay), ("jitter", jitter), ("loss", loss), ("rate", rate)):
        if v is not None:
            args += f" --{k} {v}"
    rc, out = host().pct_exec(_isp(topo)["vmid"], args)
    log(f"[{site} {transport}] {out.strip()}")


def link_state(log, topo, site, transport, state):
    rc, out = host().pct_exec(_isp(topo)["vmid"], f"linkctl {state} {_iface(topo, site, transport)}")
    log(f"[{site} {transport}] {out.strip()}")


def links(topo) -> dict:
    rc, out = host().pct_exec(_isp(topo)["vmid"], "linkctl show")
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    from .deployer import isp_nets
    names = {ifc: seg for ifc, seg in isp_nets(topo)}
    return {ifc: {**info, "site": names[ifc].get("site"), "transport": names[ifc].get("transport")}
            for ifc, info in data.items() if ifc in names}


def clear_all(log, topo):
    host().pct_exec(_isp(topo)["vmid"], "linkctl clear all")
    log("alle ISP-links schoon en up")
    h = host()
    for s in topo["sites"]:
        if h.status(s["vmid"]) == "stopped":
            h.run(["qm", "start", str(s["vmid"])])
            log(f"{s['hostname']} weer gestart")


# --------------------------------------------------------------------------------------
# verkeer
# --------------------------------------------------------------------------------------
def traffic(log, topo, site, preset):
    groups = catalog()["traffic_presets"][preset]
    targets = nodes_by_role(topo, "trafficgen")
    if site != "all":
        targets = [n for n in targets if n["segment"] == site]
    for n in targets:
        rc, out = host().pct_exec(n["vmid"], f"tgctl start --minutes 0 --groups '{json.dumps(groups)}'")
        log(f"{n['name']}: verkeer '{preset}' {'gestart' if rc == 0 else 'MISLUKT: ' + out.strip()[-200:]}")


def traffic_stop(log, topo):
    for n in nodes_by_role(topo, "trafficgen"):
        host().pct_exec(n["vmid"], "tgctl stop")
    log("verkeer gestopt")


def vm_power(log, topo, site, op):
    s = site_map(topo)[site]
    host().run(["qm", "stop" if op == "stop" else "start", str(s["vmid"])], check=False)
    log(f"{s['hostname']}: {op}")


def iperf(log, topo, site, to, seconds=20, streams=4):
    src = next(n for n in nodes_by_role(topo, "trafficgen") if n["segment"] == site)
    dst = node(topo, to)
    # bronadres uit het eerste bronnetwerk (via de overlay, niet via eth0)
    rc, out = host().pct_exec(src["vmid"], f"iperf3 -c {dst['ip']} -B {src['ip']} -t {seconds} -P {streams} | tail -4",
                              timeout=seconds + 60)
    log(f"iperf3 {src['name']} → {dst['name']}:\n{out.strip()}")


def fmg_script(log, topo, device, name, content):
    ad = fmg.adom(topo)
    rpc = fmg.client(topo)
    try:
        rpc.upsert(f"/dvmdb/adom/{ad}/script", {"name": name, "type": "cli", "content": content,
                                               "desc": "Demo-script (cockpit)", "target": "device_database"})
        res = rpc.exec(f"/dvmdb/adom/{ad}/script/execute",
                       {"adom": ad, "script": name, "scope": [{"name": device, "vdom": "root"}], "package": ""})
        log(f"script {name} uitgevoerd op {device}: {res}")
    except RpcError as e:
        log(f"script {name} op {device}: {e} (verwacht bij het 'kapotte' demo-script)")
    finally:
        rpc.logout()


def run_action(log, topo, a: dict):
    do = a["do"]
    if do == "impair":
        impair(log, topo, a["site"], a["transport"], a.get("delay"), a.get("jitter"), a.get("loss"), a.get("rate"))
    elif do == "link":
        link_state(log, topo, a["site"], a["transport"], a["state"])
    elif do == "clear":
        clear_all(log, topo)
    elif do == "traffic":
        traffic(log, topo, a["site"], a["preset"])
    elif do == "traffic_stop":
        traffic_stop(log, topo)
    elif do == "vm":
        vm_power(log, topo, a["site"], a["op"])
    elif do == "iperf":
        iperf(log, topo, a["site"], a["to"], a.get("seconds", 20), a.get("streams", 4))
    elif do == "fmg_script":
        fmg_script(log, topo, a["device"], a["name"], a["content"])
    elif do == "sleep":
        for _ in range(int(a["seconds"]) // 5):
            time.sleep(5)
            log(f"… {a['seconds']}s wachten")
    else:
        raise ValueError(f"onbekende actie {do}")


def run_scenario(log, sid: str, phase: str = "setup"):
    topo = load_topology()
    sc = scenario(sid)
    log(f"== {sc['title']} ({phase}) ==")
    for a in sc.get("setup" if phase == "setup" else "cleanup", []):
        run_action(log, topo, a)
    log("klaar; zie de FortiAI-vragen bij dit scenario")


# --------------------------------------------------------------------------------------
# live SD-WAN-status via FortiManager (proxy naar de FortiGate-API)
# --------------------------------------------------------------------------------------
def sdwan_health(topo, device: str) -> list[dict]:
    rpc = fmg.client(topo)
    try:
        res = rpc.exec("/sys/proxy/json", {"target": [f"adom/{fmg.adom(topo)}/device/{device}"], "action": "get",
                                           "resource": "/api/v2/monitor/virtual-wan/health-check"})
    finally:
        rpc.logout()
    out = []
    for r in (res or []):
        body = (r.get("response") or {}).get("results") or {}
        for hc, members in body.items():
            for member, v in members.items():
                out.append({"health_check": hc, "member": member, "status": v.get("status"),
                            "latency": v.get("latency"), "jitter": v.get("jitter"),
                            "loss": v.get("packet_loss"), "sla_map": v.get("sla_targets_met")})
    return out
