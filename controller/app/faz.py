"""FortiAnalyzer: day-0, licentie, devices autoriseren, logging- en FortiAI-controle."""
from __future__ import annotations

import time

from . import console, fmg
from .config import get_state, netmask, segment_map, update_state
from .jsonrpc import JsonRpc, RpcError


def uplink_ip(topo) -> str:
    ip = (get_state().get("wan") or {}).get("faz_ip", "")
    if not ip:
        raise RuntimeError("Stel eerst het uplink-IP van FortiAnalyzer in (Setup).")
    return ip.split("/")[0]


def mgmt_ip(topo) -> str:
    return topo["fortianalyzer"]["mgmt_ip"]


def client(topo) -> JsonRpc:
    return JsonRpc(mgmt_ip(topo), "admin", get_state().get("admin_password", "")).login()


def day0(log, topo):
    st = get_state()
    wan = st.get("wan") or {}
    fz = topo["fortianalyzer"]
    seg = segment_map(topo)
    ip = wan["faz_ip"].split("/")[0]
    plen = int(wan["faz_ip"].split("/")[1]) if "/" in wan["faz_ip"] else 24
    cli = console.bootstrap_cli(
        hostname="FAZ-LAB", password=st["admin_password"],
        ports={"port1": (ip, console.netmask_of(plen)), "port2": (fz["mgmt_ip"], netmask(seg["mgmt"]["subnet"]))},
        gateway=wan["gateway"], dns=[d for d in (wan.get("dns1"), wan.get("dns2")) if d] or ["1.1.1.1"])
    console.console_bootstrap(log, fz["vmid"], "FortiAnalyzer", st["admin_password"], cli)


def prepare_network(log, topo):
    """Uplink, route en DNS van port1 controleren/herstellen (nodig voor FortiFlex en FortiGuard)."""
    st = get_state()
    wan = st.get("wan") or {}
    cidr = wan.get("faz_ip", "")
    plen = int(cidr.split("/")[1]) if "/" in cidr else 24
    console.ensure_uplink(log, mgmt_ip(topo), st["admin_password"], "FortiAnalyzer", cidr.split("/")[0],
                          console.netmask_of(plen), wan.get("gateway", ""),
                          [d for d in (wan.get("dns1"), wan.get("dns2")) if d] or ["1.1.1.1"])


def ensure_license(log, topo):
    st = get_state()
    token = st.get("faz_fortiflex_token")
    if st.get("license_mode") == "fortiflex_api":
        from . import fortiflex
        token = fortiflex.acquire(log, topo, "faz")
    elif st.get("license_mode") != "fortiflex":
        token = None
    if not token:
        log("[FortiAnalyzer] geen FortiFlex-token: evaluatiemodus (importeer een .lic zelf via de GUI)")
        return
    prepare_network(log, topo)
    console.activate_vm_license(log, mgmt_ip(topo), st["admin_password"], "FortiAnalyzer", token)
    time.sleep(120)


def wait_api(log, topo, timeout=900) -> JsonRpc:
    deadline, last, fixed = time.time() + timeout, None, False
    while time.time() < deadline:
        try:
            return client(topo)
        except Exception as e:  # noqa: BLE001
            last = e
            if "-11" in str(e) and not fixed:
                fixed = True
                try:
                    console.enable_rpc(log, mgmt_ip(topo), get_state().get("admin_password", ""), "FortiAnalyzer")
                except Exception as e2:  # noqa: BLE001
                    log(f"rpc-permit zetten mislukt: {e2}")
                continue
            time.sleep(15)
    raise RuntimeError(f"FortiAnalyzer API niet bereikbaar ({last})")


def authorize_devices(log, topo, warn):
    """De FortiGates sturen logs naar FortiAnalyzer; ze verschijnen als 'unregistered' en worden
    hier geautoriseerd (of, als dat al gebeurd is, overgeslagen)."""
    prepare_network(log, topo)
    if console.ensure_adom_mode(log, mgmt_ip(topo), get_state()["admin_password"], "FortiAnalyzer"):
        time.sleep(30)
    rpc = wait_api(log, topo)
    ad = topo["fortianalyzer"]["adom"]
    fmg.ensure_adom(log, rpc, ad, "FortiAnalyzer")
    try:
        known = {d["name"] for d in (rpc.get(f"/dvmdb/adom/{ad}/device") or [])}
    except RpcError:
        known = set()
    def unregistered() -> dict:
        try:                                # nog niet geautoriseerde FortiGates (komen binnen via hun logging)
            return {d["name"]: d for d in (rpc.get("/dvmdb/device") or []) if d.get("name")}
        except RpcError:
            return {}

    unreg = unregistered()
    wanted = [s["fgt_name"] for s in topo["sites"] if s["fgt_name"] not in known]
    deadline = time.time() + 600            # bij een schone uitrol sturen de FortiGates hun eerste logs pas na FortiAnalyzer-start
    while [n for n in wanted if n not in unreg] and time.time() < deadline:
        log(f"[FortiAnalyzer] wacht op logs van: {', '.join(n for n in wanted if n not in unreg)}")
        time.sleep(30)
        unreg = unregistered()
    for site in topo["sites"]:
        name = site["fgt_name"]
        if name in known:
            continue
        if name in unreg:                   # de normale weg: een 'unregistered' device goedkeuren
            device = {"name": name, "device action": "promote_unreg"}
            how = "goedgekeurd"
        else:
            log(f"[FortiAnalyzer] {name} stuurt nog geen logs; probeer als echt device toe te voegen")
            device = {"name": name, "ip": site["mgmt_ip"], "adm_usr": "admin",
                      "adm_pass": get_state()["admin_password"], "mgmt_mode": "fmgfaz"}
            how = "toegevoegd"
        try:
            res = rpc.exec("/dvm/cmd/add/device", {"adom": ad, "flags": ["create_task", "nonblocking"], "device": device})
            if isinstance(res, dict) and res.get("taskid"):
                rpc.wait_task(res["taskid"], log)
            log(f"[FortiAnalyzer] {name} {how} in ADOM {ad}")
        except RpcError as e:
            warn(f"FortiAnalyzer: {name} autoriseren: {e}")
    rpc.logout()


def set_locations(log, topo, warn):
    """Breedte- en lengtegraad per FortiGate in FortiAnalyzer (kaartweergaves)."""
    from .jsonrpc import RpcError
    rpc = client(topo)
    try:
        ad = topo["fortianalyzer"]["adom"]
        n = 0
        for s in topo["sites"]:
            if s.get("lat") is None:
                continue
            try:
                rpc.update(f"/dvmdb/adom/{ad}/device/{s['fgt_name']}", {"latitude": str(s["lat"]), "longitude": str(s["lon"])})
                n += 1
            except RpcError as e:
                warn(f"locatie {s['fgt_name']} (FortiAnalyzer): {e}")
        log(f"[FortiAnalyzer] locaties gezet op {n} FortiGates")
    finally:
        rpc.logout()


def provision(log, topo) -> list[str]:
    problems: list[str] = []

    def warn(msg):
        problems.append(msg)
        log(f"⚠ {msg}")

    authorize_devices(log, topo, warn)
    set_locations(log, topo, warn)
    update_state(faz_problems=problems)
    return problems


def fortiai_status(topo) -> dict:
    """Is er een FortiAI-licentie (AISN-contract) op FortiAnalyzer en FortiManager?"""
    from . import fmg
    out = {}
    for label, ip in (("FortiAnalyzer", mgmt_ip(topo)), ("FortiManager", fmg.mgmt_ip(topo))):
        try:
            cli = console.Cli(ip, get_state().get("admin_password", ""))
            try:
                res = cli.run("diagnose fmupdate dbcontract", timeout=30)
            finally:
                cli.close()
            out[label] = {"licensed": "AISN" in res or "FortiAI" in res, "raw": res.strip()[-400:]}
        except Exception as e:  # noqa: BLE001
            out[label] = {"licensed": None, "raw": f"niet bereikbaar: {e}"}
    return out


def log_counts(topo) -> dict:
    """Aantal logregels per device (om te tonen dat logging binnenkomt)."""
    rpc = client(topo)
    try:
        ad = topo["fortianalyzer"]["adom"]
        data = rpc.get(f"/dvmdb/adom/{ad}/device", fields=["name", "conn_status", "logdisk_size"]) or []
        return {d["name"]: d.get("conn_status") for d in data}
    finally:
        rpc.logout()
