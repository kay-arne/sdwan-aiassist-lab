"""FortiManager: day-0, licentie, devices toevoegen en de SD-WAN topologie uit templates bouwen.

Gebouwd tegen de FortiManager 7.6 JSON-RPC API. Elke stap is idempotent en niet-fatale fouten komen
als waarschuwing in de log (en de 'problems'-lijst van de UI), zodat een afwijkend veld in een andere
7.6.x-build niet de hele uitrol breekt. De gegenereerde payloads staan in data/generated/ en zijn te
bekijken via 'Config bekijken'."""
from __future__ import annotations

import json
import time

from . import console
from .config import (GENERATED, get_state, hubs, mgmt_gw, netmask, prefixlen, segment_map, spokes,
                     update_state)
from .jsonrpc import JsonRpc, RpcError

PKG = "LAB-PKG"
SPOKE_GROUP = "SPOKES"
HUB_GROUP = "HUBS"
SDWAN_TEMPLATE_SUFFIX = "-SDWAN"


def uplink_ip(topo) -> str:
    ip = (get_state().get("wan") or {}).get("fmg_ip", "")
    if not ip:
        raise RuntimeError("Stel eerst het uplink-IP van FortiManager in (Setup).")
    return ip.split("/")[0]


def mgmt_ip(topo) -> str:
    """Beheeradres (port2, lab-management): daar praat de controller mee; uplink_ip is alleen voor GUI/internet."""
    return topo["fortimanager"]["mgmt_ip"]


def ensure_adom(log, rpc: JsonRpc, ad: str, desc: str):
    """Eigen ADOM (FortiOS 7.6) voor het lab, zodat de devices niet in 'root' staan."""
    try:
        if rpc.get(f"/dvmdb/adom/{ad}"):
            log(f"[{desc}] ADOM {ad} bestaat al")
            return
    except RpcError:
        pass
    rpc.add("/dvmdb/adom", {"name": ad, "desc": "SD-WAN & FortiAI Lab", "os_ver": "7.0", "mr": 6, "state": 1})
    log(f"[{desc}] ADOM {ad} aangemaakt (FortiOS 7.6)")


def client(topo) -> JsonRpc:
    return JsonRpc(mgmt_ip(topo), "admin", get_state().get("admin_password", "")).login()


def adom(topo) -> str:
    return topo["fortimanager"]["adom"]


# --------------------------------------------------------------------------------------
# day-0 en licentie
# --------------------------------------------------------------------------------------
def day0(log, topo):
    st = get_state()
    wan = st.get("wan") or {}
    fm = topo["fortimanager"]
    seg = segment_map(topo)
    ip, plen = wan["fmg_ip"].split("/")[0], wan["fmg_ip"].split("/")[1] if "/" in wan["fmg_ip"] else "24"
    cli = console.bootstrap_cli(
        hostname="FMG-LAB", password=st["admin_password"],
        ports={"port1": (ip, console.netmask_of(int(plen))),
               "port2": (fm["mgmt_ip"], netmask(seg["mgmt"]["subnet"]))},
        gateway=wan["gateway"], dns=[d for d in (wan.get("dns1"), wan.get("dns2")) if d] or ["1.1.1.1"])
    console.console_bootstrap(log, fm["vmid"], "FortiManager", st["admin_password"], cli)


def ensure_license(log, topo):
    st = get_state()
    token = st.get("fmg_fortiflex_token")
    if st.get("license_mode") != "fortiflex" or not token:
        log("[FortiManager] geen FortiFlex-token: evaluatiemodus (importeer een .lic zelf via de GUI)")
        return
    cli = console.wait_ssh(log, mgmt_ip(topo), st["admin_password"], "FortiManager")
    try:
        out = cli.run(f"execute vm-license {token}", timeout=90, answer="y")
        log(f"[FortiManager] FortiFlex-token geactiveerd; FortiManager herstart ({out.strip()[-120:]})")
    finally:
        cli.close()
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
                    console.enable_rpc(log, mgmt_ip(topo), get_state().get("admin_password", ""), "FortiManager")
                except Exception as e2:  # noqa: BLE001
                    log(f"rpc-permit zetten mislukt: {e2}")
                continue
            time.sleep(15)
    raise RuntimeError(f"FortiManager API niet bereikbaar ({last})")


# --------------------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------------------
def device_names(topo) -> list[str]:
    return [s["fgt_name"] for s in topo["sites"]]


def version_report(log, topo, rpc: JsonRpc) -> str:
    """Versies van FortiManager en de eerste FortiGate (voor een duidelijke melding bij 'ostypeunsupport')."""
    fmg_v, fgt_v = "?", "?"
    try:
        st = rpc.get("/sys/status")
        fmg_v = f"{st.get('Version', '?')}" if isinstance(st, dict) else str(st)
    except Exception:  # noqa: BLE001
        pass
    try:
        site = topo["sites"][0]
        res = rpc.exec("/dvm/cmd/discover/device", {"device": {"ip": site["mgmt_ip"], "adm_usr": "admin",
                                                               "adm_pass": get_state()["admin_password"]}})
        d = (res or {}).get("device", res) if isinstance(res, dict) else {}
        fgt_v = f"{d.get('os_ver', '?')} MR{d.get('mr', '?')} patch {d.get('patch', '?')} build {d.get('build', '?')}"
    except Exception:  # noqa: BLE001
        pass
    log(f"[FortiManager] versies: FortiManager {fmg_v}; FortiGate {fgt_v}")
    return f"FortiManager {fmg_v} / FortiGate {fgt_v}"


def add_devices(log, topo, rpc: JsonRpc, warn):
    ad = adom(topo)
    versions = version_report(log, topo, rpc)
    known = {d["name"]: d for d in (rpc.get(f"/dvmdb/adom/{ad}/device") or [])}
    for site in topo["sites"]:
        name = site["fgt_name"]
        if name in known:
            log(f"[FortiManager] {name} staat al in de ADOM ({known[name].get('conn_status')})")
            continue
        log(f"[FortiManager] {name} toevoegen ({site['mgmt_ip']})")
        try:
            res = rpc.exec("/dvm/cmd/add/device", {
                "adom": ad, "flags": ["create_task", "nonblocking"],
                "device": {"name": name, "ip": site["mgmt_ip"], "adm_usr": "admin",
                           "adm_pass": get_state()["admin_password"], "mgmt_mode": "fmgfaz"}})
            if isinstance(res, dict) and res.get("taskid"):
                rpc.wait_task(res["taskid"], log)
        except RpcError as e:
            warn(f"{name} toevoegen: {e}")
    have = {d["name"] for d in (rpc.get(f"/dvmdb/adom/{ad}/device") or [])}
    missing = [s["fgt_name"] for s in topo["sites"] if s["fgt_name"] not in have]
    if missing:
        raise RuntimeError(
            f"Devices niet toegevoegd aan FortiManager: {', '.join(missing)}. zie de waarschuwingen hierboven. "
            f"Versies: {versions}. Controleer bij 'VM device is not allowed' de instelling fgfm-allow-vm op "
            "FortiManager en de licentie van de FortiGate-VM; bij 'ostypeunsupport' de FortiOS-ondersteuning van de "
            "ADOM; en of de FortiGate FortiManager bereikt op tcp/541 (FGFM) via het management-netwerk.")
    # groepen voor de overlay template
    for grp, members in ((SPOKE_GROUP, spokes(topo)), (HUB_GROUP, hubs(topo))):
        try:
            rpc.upsert(f"/dvmdb/adom/{ad}/group", {"name": grp, "type": "normal", "os_type": "fos"})
            rpc.set(f"/dvmdb/adom/{ad}/group/{grp}/object member",
                    [{"name": m["fgt_name"], "vdom": "root"} for m in members])
        except RpcError as e:
            warn(f"groep {grp}: {e}")
    # SD-WAN management (alleen 7.6) zodat de devices in de overlay template geselecteerd kunnen worden
    for site in topo["sites"]:
        try:
            rpc.update(f"/dvmdb/adom/{ad}/device/{site['fgt_name']}", {"flags": ["sdwan_management"]})
        except RpcError as e:
            warn(f"{site['fgt_name']}: SD-WAN management inschakelen: {e}")


# --------------------------------------------------------------------------------------
# SD-WAN overlay template
# --------------------------------------------------------------------------------------
def overlay_payload(topo) -> dict:
    sd = topo["sdwan"]
    hub_list = hubs(topo)
    underlay_for = lambda: [{"id": u["id"], "cost": 0, "interface": u["port"], "private-link": "disable",
                             "override-ip": "disable", "ip": None, "transport-group": 0}
                            for u in sd["underlays"]]
    nodes, nid = [], 1
    for h in hub_list:
        nodes.append({"_scope": {"name": h["fgt_name"], "vdom": "root"}, "underlay": underlay_for(),
                      "advertisement": "connected", "role": "hub", "hub-role": "standalone",
                      "prefix": [], "id": nid})
        nid += 1
    nodes.append({"_scope": {"name": SPOKE_GROUP, "is group": 1}, "underlay": underlay_for(),
                  "advertisement": "connected", "role": "spoke", "id": nid})
    return {
        "name": sd["template"], "authmethod": sd["authmethod"], "topology": sd["topology"],
        "loopback-ip": _mask(sd["loopback_ip"]), "overlay-network": _mask(sd["overlay_network"]),
        "as": sd["as_number"], "route_reflection": "disable", "advpn": sd["advpn"],
        "dynamic_bgp": "disable", "auto-branch-id-assignment": "enable",
        "hub-number": len(hub_list), "nodes": nodes,
    }


def _mask(cidr: str) -> str:
    import ipaddress
    n = ipaddress.ip_network(cidr)
    return f"{n.network_address}/{n.netmask}"


def build_overlay(log, topo, rpc: JsonRpc, warn):
    ad, sd = adom(topo), topo["sdwan"]
    data = overlay_payload(topo)
    (GENERATED / "fmg-overlay-template.json").write_text(json.dumps(data, indent=2))
    name = sd["template"]
    try:
        rpc.upsert(f"/pm/template/_sdwan_overlay/adom/{ad}",
                   {"name": name, "type": "template",
                    "template setting": {"stype": "_sdwan_overlay",
                                         "description": "Gegenereerd door de SD-WAN & FortiAI Lab controller"}})
    except RpcError as e:
        warn(f"overlay template aanmaken: {e}")
    last = None
    # 'topology' heet in 7.6.x-builds verschillend; probeer de gekozen waarde en dan de varianten
    for topology in [sd["topology"]] + [t for t in ("multi-hub", "dual-hub") if t != sd["topology"]]:
        data["topology"] = topology
        try:
            rpc.set(f"/pm/config/adom/{ad}/template/_sdwan_overlay/{name}/sdwan/overlay", data)
            log(f"[FortiManager] overlay template {name} ingesteld (topology={topology}, {len(hubs(topo))} hubs)")
            last = None
            break
        except RpcError as e:
            last = e
    if last:
        warn(f"overlay template instellen: {last}")
        return False
    try:
        rpc.exec(f"/pm/config/adom/{ad}/_sdwan/overlay/orchestration", {"template": name})
        log("[FortiManager] orchestratie uitgevoerd: IPsec-, BGP-, SD-WAN- en CLI-templates gegenereerd")
        return True
    except RpcError as e:
        warn(f"overlay-orchestratie: {e}")
        return False


# --------------------------------------------------------------------------------------
# SD-WAN: SLA, rules, firewall policy
# --------------------------------------------------------------------------------------
def sdwan_template_name(topo) -> str:
    return topo["sdwan"]["template"] + SDWAN_TEMPLATE_SUFFIX


def find_sdwan_template(rpc: JsonRpc, topo) -> str | None:
    ad = adom(topo)
    try:
        profs = rpc.get(f"/pm/wanprof/adom/{ad}") or []
    except RpcError:
        return None
    names = [p["name"] for p in profs]
    want = topo["sdwan"]["template"]
    for n in names:
        if n == sdwan_template_name(topo) or want.lower() in n.lower():
            return n
    return names[0] if names else None


def sdwan_rules(topo) -> tuple[list[dict], list[dict]]:
    """(health-checks, services) voor de SD-WAN template."""
    sd = topo["sdwan"]
    hc = sd["health_check"]
    sla = hc["sla"]
    health = [{"name": hc["name"], "server": [hc["server"]], "protocol": hc["protocol"],
               "interval": 1000, "failtime": 3, "recoverytime": 5,
               "sla": [{"id": 1, "latency-threshold": sla["latency_ms"], "jitter-threshold": sla["jitter_ms"],
                        "packetloss-threshold": sla["loss_pct"]}]}]
    services = []
    for i, r in enumerate(sd["rules"], 1):
        svc = {"id": i, "name": r["name"], "mode": "sla" if r["mode"] == "sla" else "priority",
               "health-check": [hc["name"]] if r["mode"] == "sla" else [],
               "sla": [{"health-check": hc["name"], "id": [1]}] if r["mode"] == "sla" else []}
        if r.get("isdb"):
            svc["internet-service"] = "enable"
            svc["internet-service-name"] = r["isdb"]
        if r.get("dst"):
            svc["dst"] = r["dst"]
        services.append(svc)
    return health, services


def patch_sdwan_template(log, topo, rpc: JsonRpc, warn):
    ad = adom(topo)
    prof = find_sdwan_template(rpc, topo)
    health, services = sdwan_rules(topo)
    (GENERATED / "fmg-sdwan-rules.json").write_text(json.dumps({"health-check": health, "service": services}, indent=2))
    if not prof:
        warn("geen SD-WAN template gevonden na de orchestratie; health-check en rules niet gezet")
        return
    base = f"/pm/config/adom/{ad}/wanprof/{prof}/system/sdwan"
    for hc in health:
        try:
            rpc.upsert(f"{base}/health-check", hc)
            log(f"[FortiManager] SD-WAN health-check {hc['name']} (SLA) in template {prof}")
        except RpcError as e:
            warn(f"health-check {hc['name']}: {e}")
    for svc in services:
        try:
            rpc.upsert(f"{base}/service", svc)
            log(f"[FortiManager] SD-WAN rule {svc['name']} in template {prof}")
        except RpcError as e:         # één onbekende ISDB-naam mag de rest niet tegenhouden
            warn(f"SD-WAN rule {svc['name']}: {e}")


def policy_package(log, topo, rpc: JsonRpc, warn, zones: dict):
    ad = adom(topo)
    try:
        rpc.upsert(f"/pm/pkg/adom/{ad}", {"name": PKG, "type": "pkg"})
    except RpcError as e:
        warn(f"policy package: {e}")
        return
    # normalized interfaces met per-device mapping
    mapping = {"LAN": ("port4", None), "OVERLAY": (zones["overlay"], None), "UNDERLAY": (zones["underlay"], None)}
    for name, (intf, _) in mapping.items():
        try:
            rpc.upsert(f"/pm/config/adom/{ad}/obj/dynamic/interface",
                       {"name": name, "single-intf": 1, "dynamic_mapping": [
                           {"_scope": [{"name": s["fgt_name"], "vdom": "root"}], "local-intf": [intf]}
                           for s in topo["sites"]]})
        except RpcError as e:
            warn(f"interface-object {name}: {e}")
    common = {"schedule": ["always"], "service": ["ALL"], "logtraffic": "all", "action": "accept",
              "status": "enable", "srcaddr": ["all"], "dstaddr": ["all"]}
    utm = {"utm-status": "enable", "ssl-ssh-profile": "certificate-inspection", "av-profile": "default",
           "ips-sensor": "default", "application-list": "default", "webfilter-profile": "default",
           "dnsfilter-profile": "default"}
    policies = [
        {**common, **utm, "name": "LAN-to-OVERLAY", "srcintf": ["LAN"], "dstintf": ["OVERLAY"]},
        {**common, "name": "OVERLAY-to-LAN", "srcintf": ["OVERLAY"], "dstintf": ["LAN"]},
        {**common, **utm, "name": "LAN-to-INTERNET", "srcintf": ["LAN"], "dstintf": ["UNDERLAY"], "nat": "enable"},
        {**common, "name": "OVERLAY-hairpin", "srcintf": ["OVERLAY"], "dstintf": ["OVERLAY"]},
        {**common, "name": "LAN-to-LAN", "srcintf": ["LAN"], "dstintf": ["LAN"]},
    ]
    (GENERATED / "fmg-policy-package.json").write_text(json.dumps(policies, indent=2))
    for i, pol in enumerate(policies, 1):
        try:
            rpc.upsert(f"/pm/config/adom/{ad}/pkg/{PKG}/firewall/policy", {**pol, "policyid": i})
        except RpcError as e:
            warn(f"policy {pol['name']}: {e}")
    log(f"[FortiManager] policy package {PKG}: {len(policies)} policies")
    # package koppelen aan alle devices
    try:
        rpc.set(f"/pm/pkg/adom/{ad}/{PKG}/scope member",
                [{"name": s["fgt_name"], "vdom": "root"} for s in topo["sites"]])
    except RpcError as e:
        warn(f"package-scope: {e}")


def discover_zones(rpc: JsonRpc, topo) -> dict:
    zones = {"overlay": topo["sdwan"]["zones"]["overlay"], "underlay": topo["sdwan"]["zones"]["underlay"]}
    prof = find_sdwan_template(rpc, topo)
    if not prof:
        return zones
    try:
        found = [z["name"] for z in rpc.get(f"/pm/config/adom/{adom(topo)}/wanprof/{prof}/system/sdwan/zone") or []]
    except RpcError:
        return zones
    for z in found:
        low = z.lower()
        if "overlay" in low or "vpn" in low:
            zones["overlay"] = z
        elif "under" in low or low in ("virtual-wan-link", "wan"):
            zones["underlay"] = z
    return zones


def install(log, topo, rpc: JsonRpc, warn, devices: list[str] | None = None):
    """Device-settings (templates) en policy package installeren."""
    ad = adom(topo)
    scope = [{"name": n, "vdom": "root"} for n in (devices or device_names(topo))]
    for what, url, data in (
            ("device settings (templates)", "/securityconsole/install/device",
             {"adom": ad, "scope": scope, "flags": ["none"]}),
            ("policy package", "/securityconsole/install/package",
             {"adom": ad, "pkg": PKG, "scope": scope, "flags": ["none"]})):
        try:
            res = rpc.exec(url, data)
            if isinstance(res, dict) and res.get("task"):
                rpc.wait_task(res["task"], log)
            log(f"[FortiManager] {what} geïnstalleerd")
        except RpcError as e:
            warn(f"installatie {what}: {e}")


def provision(log, topo, devices=None) -> list[str]:
    problems: list[str] = []

    def warn(msg):
        problems.append(msg)
        log(f"⚠ {msg}")

    if console.ensure_adom_mode(log, mgmt_ip(topo), get_state()["admin_password"], "FortiManager"):
        time.sleep(30)
    console.allow_vm_devices(log, mgmt_ip(topo), get_state()["admin_password"], "FortiManager")
    rpc = wait_api(log, topo)
    ensure_adom(log, rpc, adom(topo), "FortiManager")
    add_devices(log, topo, rpc, warn)
    if build_overlay(log, topo, rpc, warn):
        patch_sdwan_template(log, topo, rpc, warn)
    zones = discover_zones(rpc, topo)
    log(f"[FortiManager] zones: overlay={zones['overlay']}, underlay={zones['underlay']}")
    policy_package(log, topo, rpc, warn, zones)
    install(log, topo, rpc, warn, devices)
    update_state(fmg_problems=problems, fmg_provisioned=not problems)
    rpc.logout()
    return problems


# --------------------------------------------------------------------------------------
# status voor de UI
# --------------------------------------------------------------------------------------
def device_status(topo) -> list[dict]:
    rpc = client(topo)
    try:
        devs = rpc.get(f"/dvmdb/adom/{adom(topo)}/device", fields=["name", "ip", "conn_status", "conf_status",
                                                                      "dev_status", "os_ver", "sn"]) or []
    finally:
        rpc.logout()
    mp = {0: "onbekend", 1: "up", 2: "down"}
    return [{"name": d["name"], "ip": d.get("ip"), "conn": mp.get(d.get("conn_status"), d.get("conn_status")),
             "conf": d.get("conf_status"), "ver": d.get("os_ver"), "sn": d.get("sn")} for d in devs]
