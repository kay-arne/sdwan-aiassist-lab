"""FortiAnalyzer: day-0, licentie, devices autoriseren, logging- en FortiAI-controle."""
from __future__ import annotations

import time

from . import console
from .config import get_state, netmask, segment_map, update_state
from .jsonrpc import JsonRpc, RpcError


def uplink_ip(topo) -> str:
    ip = (get_state().get("wan") or {}).get("faz_ip", "")
    if not ip:
        raise RuntimeError("Stel eerst het uplink-IP van FortiAnalyzer in (Setup).")
    return ip.split("/")[0]


def client(topo) -> JsonRpc:
    return JsonRpc(uplink_ip(topo), "admin", get_state().get("admin_password", "")).login()


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


def ensure_license(log, topo):
    st = get_state()
    token = st.get("faz_fortiflex_token")
    if st.get("license_mode") != "fortiflex" or not token:
        log("[FortiAnalyzer] geen FortiFlex-token: evaluatiemodus (importeer een .lic zelf via de GUI)")
        return
    cli = console.wait_ssh(log, uplink_ip(topo), st["admin_password"], "FortiAnalyzer")
    try:
        out = cli.run(f"execute vm-license {token}", timeout=90, answer="y")
        log(f"[FortiAnalyzer] FortiFlex-token geactiveerd; herstart ({out.strip()[-120:]})")
    finally:
        cli.close()
    time.sleep(120)


def wait_api(log, topo, timeout=900) -> JsonRpc:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            return client(topo)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(15)
    raise RuntimeError(f"FortiAnalyzer API niet bereikbaar ({last})")


def authorize_devices(log, topo, warn):
    """De FortiGates sturen logs naar FortiAnalyzer; ze verschijnen als 'unregistered' en worden
    hier geautoriseerd (of, als dat al gebeurd is, overgeslagen)."""
    rpc = wait_api(log, topo)
    ad = topo["fortianalyzer"]["adom"]
    try:
        known = {d["name"] for d in (rpc.get(f"/dvmdb/adom/{ad}/device") or [])}
    except RpcError:
        known = set()
    for site in topo["sites"]:
        name = site["fgt_name"]
        if name in known:
            continue
        try:
            res = rpc.exec("/dvm/cmd/add/device", {
                "adom": ad, "flags": ["create_task", "nonblocking"],
                "device": {"name": name, "ip": site["mgmt_ip"], "adm_usr": "admin",
                           "adm_pass": get_state()["admin_password"], "mgmt_mode": "fmgfaz"}})
            if isinstance(res, dict) and res.get("taskid"):
                rpc.wait_task(res["taskid"], log)
            log(f"[FortiAnalyzer] {name} geautoriseerd")
        except RpcError as e:
            warn(f"FortiAnalyzer: {name} autoriseren: {e}")
    rpc.logout()


def provision(log, topo) -> list[str]:
    problems: list[str] = []

    def warn(msg):
        problems.append(msg)
        log(f"⚠ {msg}")

    authorize_devices(log, topo, warn)
    update_state(faz_problems=problems)
    return problems


def fortiai_status(topo) -> dict:
    """Is er een FortiAI-licentie (AISN-contract) op FortiAnalyzer en FortiManager?"""
    from . import fmg
    out = {}
    for label, ip in (("FortiAnalyzer", uplink_ip(topo)), ("FortiManager", fmg.uplink_ip(topo))):
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
