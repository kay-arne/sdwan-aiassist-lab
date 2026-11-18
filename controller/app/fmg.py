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


def prepare_network(log, topo):
    """Uplink, route en DNS van port1 controleren/herstellen (nodig voor FortiFlex en FortiGuard)."""
    st = get_state()
    wan = st.get("wan") or {}
    cidr = wan.get("fmg_ip", "")
    plen = int(cidr.split("/")[1]) if "/" in cidr else 24
    console.ensure_uplink(log, mgmt_ip(topo), st["admin_password"], "FortiManager", cidr.split("/")[0],
                          console.netmask_of(plen), wan.get("gateway", ""),
                          [d for d in (wan.get("dns1"), wan.get("dns2")) if d] or ["1.1.1.1"])


def ensure_license(log, topo):
    st = get_state()
    token = st.get("fmg_fortiflex_token")
    if st.get("license_mode") == "fortiflex_api":
        from . import fortiflex
        token = fortiflex.acquire(log, topo, "fmg")
    elif st.get("license_mode") != "fortiflex":
        token = None
    if not token:
        log("[FortiManager] geen FortiFlex-token: evaluatiemodus (importeer een .lic zelf via de GUI)")
        return
    prepare_network(log, topo)
    console.activate_vm_license(log, mgmt_ip(topo), st["admin_password"], "FortiManager", token)
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
    """Schema volgens 'get ... option=syntax' op FortiManager 7.6.8 (/sdwan/overlay):
    - basisattributen op het overlay-object (zonder name/nodes);
    - nodes zijn een subtabel (.../sdwan/overlay/nodes), met per node een subtabel 'underlay'."""
    sd = topo["sdwan"]
    hub_list = hubs(topo)
    base = {"as": sd["as_number"], "authmethod": sd["authmethod"], "topology": sd["topology"],
            "loopback-ip": _mask(sd["loopback_ip"]), "overlay-network": _mask(sd["overlay_network"]),
            "route_reflection": "enable", "advpn": sd["advpn"], "dynamic_bgp": "disable",
            "auto-branch-id-assignment": "enable", "hub-number": len(hub_list),
            "sdwan_members": "enable", "sdwan_health_check": "enable"}   # laat de orchestratie ook SD-WAN-templates maken
    underlays = [{"id": u["id"], "interface": u["port"], "cost": 0, "private-link": "disable",
                  "override-ip": "disable", "transport-group": 0} for u in sd["underlays"]]
    nodes = []
    for i, h in enumerate(hub_list, 1):
        nodes.append({"id": i, "role": "hub", "hub-role": "primary" if i == 1 else "secondary" if i == 2 else "standalone",
                      "advertisement": "connected", "scope": [h["fgt_name"]], "underlay": underlays})
    nodes.append({"id": len(hub_list) + 1, "role": "spoke", "advertisement": "connected",
                  "scope": [s["fgt_name"] for s in spokes(topo)], "underlay": underlays})
    return {"base": base, "nodes": nodes}


def _mask(cidr: str) -> str:
    import ipaddress
    n = ipaddress.ip_network(cidr)
    return f"{n.network_address}/{n.netmask}"


def _put(rpc: JsonRpc, table_url: str, entry_id, data: dict):
    """Tabelrij toevoegen; bestaat hij al (-2), dan die ene rij bijwerken."""
    try:
        return rpc.add(table_url, data)
    except RpcError as e:
        if e.code == -2:
            return rpc.set(f"{table_url}/{entry_id}", data)
        raise


def build_overlay(log, topo, rpc: JsonRpc, warn):
    ad, sd = adom(topo), topo["sdwan"]
    spec = overlay_payload(topo)
    (GENERATED / "fmg-overlay-template.json").write_text(json.dumps(spec, indent=2))
    name = sd["template"]
    root = f"/pm/config/adom/{ad}/template/_sdwan_overlay/{name}/sdwan/overlay"
    try:
        rpc.upsert(f"/pm/template/_sdwan_overlay/adom/{ad}",
                   {"name": name, "type": "template",
                    "template setting": {"stype": "_sdwan_overlay",
                                         "description": "Gegenereerd door de SD-WAN & FortiAI Lab controller"}})
    except RpcError as e:
        warn(f"overlay template aanmaken: {e}")
    try:
        rpc.set(root, spec["base"])
        log(f"[FortiManager] overlay {name}: basisinstellingen gezet (topology={sd['topology']})")
    except RpcError as e:
        warn(f"overlay template instellen: {e}")
        return False
    ok = True
    for node in spec["nodes"]:
        nid, underlay, scope = node["id"], node["underlay"], node["scope"]
        body = {k: v for k, v in node.items() if k not in ("underlay", "scope")}
        last = None
        devs = [{"name": s, "vdom": "root"} for s in scope]
        if node["role"] == "spoke":      # alle branches via de apparaatgroep; per apparaat is de terugval
            forms = [[{"name": SPOKE_GROUP, "is group": 1}], {"name": SPOKE_GROUP, "is group": 1}, devs]
        else:
            forms = [devs, scope]
        for form in forms:                                                          # varianten voor _scope
            try:
                _put(rpc, f"{root}/nodes", nid, {**body, "_scope": form})
                last = None
                break
            except RpcError as e:
                last = e
        if last:
            warn(f"overlay node {nid} ({node['role']}): {last}")
            ok = False
            continue
        for u in underlay:
            try:
                _put(rpc, f"{root}/nodes/{nid}/underlay", u["id"], u)
            except RpcError as e:
                warn(f"overlay node {nid} underlay {u['id']}: {e}")
                ok = False
        log(f"[FortiManager] overlay node {nid} ({node['role']}): {', '.join(scope)}")
    if not ok:
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
def sdwan_template_names(topo) -> tuple[str, str]:
    base = topo["sdwan"]["template"]
    return f"{base}-SDWAN-HUB", f"{base}-SDWAN-BRANCH"


def _members(topo, role: str) -> list[dict]:
    """SD-WAN-members per rol. Namen volgen uit de overlay-orchestratie: op een hub heten de tunnels VPN<n>,
    op een branch HUB<h>-VPN<n> (n = underlay-id); de underlay-poorten staan in topology.yaml."""
    sd, out = topo["sdwan"], []
    for u in sd["underlays"]:
        gw = segment_map(topo)["wan-" + "AB"[u["id"] - 1]]["gateway"]      # zonder gateway blijft de default route inactief
        out.append({"interface": u["port"], "zone": sd["zones"]["underlay"], "underlay": u["id"], "gateway": gw})
    if role == "hub":
        for u in sd["underlays"]:
            out.append({"interface": f"VPN{u['id']}", "zone": sd["zones"]["overlay"]})
    else:
        for h in range(1, len(hubs(topo)) + 1):
            for u in sd["underlays"]:
                out.append({"interface": f"HUB{h}-VPN{u['id']}", "zone": sd["zones"]["overlay"]})
    for i, m in enumerate(out, 1):
        m["seq-num"] = i
    return out


def sdwan_rules(topo, members: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(health-checks, services, adresobjecten) voor de branch-template."""
    sd = topo["sdwan"]
    z_over, z_under = sd["zones"]["overlay"], sd["zones"]["underlay"]
    hc = sd["health_check"]
    sla = hc["sla"]
    # Expliciete members ('Participants: Specify'): zonder deze lijst start FortiOS 7.6 de probes niet
    health = [{"name": hc["name"], "server": ([hc["server"]] if isinstance(hc["server"], str) else list(hc["server"])), "protocol": hc["protocol"],
               "interval": 1000, "failtime": 3, "recoverytime": 5,
               # SLA-logregels: voeden FortiView > SD-WAN en de SLA-events in FortiAnalyzer
               "sla-fail-log-period": 10, "sla-pass-log-period": 60,
               "sla": [{"id": 1, "latency-threshold": sla["latency_ms"], "jitter-threshold": sla["jitter_ms"],
                        "packetloss-threshold": sla["loss_pct"]}],
               "members": [str(m["seq-num"]) for m in members if not m.get("underlay")]},
              # de underlays (internet) kunnen het datacenter niet bereiken: eigen health-check, anders staan ze 'dead'
              # en wordt de default-route naar het internet inactief
              {"name": "INET", "server": ["8.8.8.8"], "protocol": "ping", "interval": 1000, "failtime": 3, "recoverytime": 5,
               "sla-fail-log-period": 10, "sla-pass-log-period": 60,
               "members": [str(m["seq-num"]) for m in members if m.get("underlay")]}]
    addresses, services = [], []
    by_underlay = {m["underlay"]: m["seq-num"] for m in members if m.get("underlay")}
    last_u = max(by_underlay) if by_underlay else 1
    for i, r in enumerate(sd["rules"], 1):
        svc = {"id": i, "name": r["name"]}
        if r["mode"] == "sla":
            first = z_over if (r.get("dst") or "VOICE" in r["name"]) else z_under
            order = [first] + [z for z in (z_over, z_under) if z != first]
            if r.get("dst"):
                order = [z_over]
            svc.update({"mode": "sla", "priority-zone": order, "health-check": [hc["name"]],
                        "sla": [{"health-check": hc["name"], "id": [1]}]})
        else:       # 'cost': goedkoopste link (laatste underlay), nooit over de overlay
            svc.update({"mode": "priority", "priority-members": [by_underlay[last_u]], "health-check": [hc["name"]]})
        if r.get("isdb"):
            svc["internet-service"] = "enable"
            svc["internet-service-name"] = r["isdb"]
        if r.get("dst"):
            names = []
            for cidr in r["dst"]:
                import ipaddress
                n = ipaddress.ip_network(cidr)
                nm = "DC-" + str(n.network_address) + "_" + str(n.prefixlen)
                addresses.append({"name": nm, "type": "ipmask", "subnet": f"{n.network_address} {n.netmask}"})
                names.append(nm)
            svc["dst"] = names
        services.append(svc)
    return health, services, addresses


def _wanprof_oid(rpc: JsonRpc, ad: str, name: str, desc: str) -> int | None:
    try:
        rpc.add(f"/pm/wanprof/adom/{ad}", {"name": name, "type": "wanprof", "description": desc})
    except RpcError as e:
        if e.code != -2:
            raise
    for p in rpc.get(f"/pm/wanprof/adom/{ad}") or []:
        if p["name"] == name:
            return p.get("oid")
    return None


def _put_row(rpc: JsonRpc, table_url: str, key, row: dict):
    try:
        rpc.add(table_url, row)
    except RpcError as e:
        if e.code != -2:
            raise
        rpc.set(f"{table_url}/{key}", row)


def _attach(rpc: JsonRpc, ad: str, group: str, oid: int, tname: str):
    """SD-WAN-template (type 5) in de template-groep van de overlay opnemen; oude lab-SD-WAN-templates eruit."""
    url = f"/pm/tmplgrp/adom/{ad}/{group}"
    g = rpc.get(url)
    g = g[0] if isinstance(g, list) else g
    setting = dict(g["template group setting"])
    cur = [x for x in setting.get("templates", []) if not x.startswith("5-")]
    setting["templates"] = cur + [f"5-{oid}__{tname}"]
    rpc.update(url, {"template group setting": setting})


def _add_service(log, warn, rpc: JsonRpc, base: str, svc: dict, tname: str):
    """Rule toevoegen. Onbekende ISDB-namen (de ISDB van deze FortiManager kent niet alles) worden per naam
    uitgefilterd; een rule zonder enige geldige selector wordt overgeslagen (hij zou al het verkeer pakken)."""
    url = f"{base}/service"
    try:
        _put_row(rpc, url, svc["id"], svc)
        log(f"[FortiManager] SD-WAN rule {svc['name']} in {tname}")
        return
    except RpcError as e:
        err = e
    names = svc.get("internet-service-name")
    if not names or "internet-service-name" not in str(err):
        warn(f"SD-WAN rule {svc['name']}: {err}")
        return
    valid = []
    for n in names:
        probe = {**svc, "id": 99, "name": "probe", "internet-service-name": [n]}
        try:
            rpc.add(url, probe)
            valid.append(n)
        except RpcError:
            pass
        finally:
            try:
                rpc.delete(f"{url}/99")
            except RpcError:
                pass
    skipped = [n for n in names if n not in valid]
    if not valid:
        warn(f"SD-WAN rule {svc['name']} overgeslagen: geen van de ISDB-namen {names} is bekend op deze FortiManager "
             "(ISDB bijwerken via FortiGuard, of de namen in topology.yaml aanpassen)")
        return
    try:
        _put_row(rpc, url, svc["id"], {**svc, "internet-service-name": valid})
        log(f"[FortiManager] SD-WAN rule {svc['name']} in {tname} (ISDB: {', '.join(valid)})")
        if skipped:
            warn(f"SD-WAN rule {svc['name']}: onbekende ISDB-namen overgeslagen: {', '.join(skipped)}")
    except RpcError as e:
        warn(f"SD-WAN rule {svc['name']}: {e}")


# De orchestratie laat 'redistribute connected' uit staan: zonder dit adverteren hubs en branches hun LAN niet.
_REDISTRIBUTE = ["config router bgp", '  config redistribute "connected"', "    set status enable", "  end", "end"]


def _hub_tunnel_script(topo, h: int) -> str:
    """CLI voor de tunnelinterfaces van hub h. De overlay-orchestratie zet het IP van de hub-tunnelinterface niet
    (device-DB: ip 0.0.0.0), terwijl de branches de BGP-buur op <pool-einde>-2 verwachten (bijv. 10.10.31.253 voor
    VPN1 van hub 1). Pools: /19 per (hub, underlay), blok = (h-1)*4 + (underlay-1) binnen het overlay-netwerk."""
    import ipaddress
    net = ipaddress.ip_network(topo["sdwan"]["overlay_network"])
    lines = ["config system interface"]
    for u in topo["sdwan"]["underlays"]:
        start = int(net.network_address) + ((h - 1) * 4 + (int(u["id"]) - 1)) * 8192
        ip, remote = ipaddress.ip_address(start + 8192 - 3), ipaddress.ip_address(start + 8192 - 2)
        lines += [f'  edit "VPN{u["id"]}"', f"    set ip {ip} 255.255.255.255",
                  f"    set remote-ip {remote} 255.255.224.0", "  next"]
    lines.append("end")
    lines += _REDISTRIBUTE
    return "\n".join(lines) + "\n"


def build_hub_tunnel_ips(log, topo, rpc: JsonRpc, warn):
    """Eigen CLI-template per hub in de CLI-groep van de overlay (wordt na elke orchestratie opnieuw toegevoegd)."""
    ad, base = adom(topo), topo["sdwan"]["template"]
    for h in range(1, len(hubs(topo)) + 1):
        name = f"{base}_HUB{h}_TUNNEL_IPS"
        try:
            _put_row(rpc, f"/pm/config/adom/{ad}/obj/cli/template", name,
                     {"name": name, "type": "cli", "option": "sdwan-overlay", "script": _hub_tunnel_script(topo, h),
                      "description": "SD-WAN & FortiAI Lab: IP's van de hub-tunnelinterfaces"})
            grp_url = f"/pm/config/adom/{ad}/obj/cli/template-group/{base}_HUB{h}_CLIGRP"
            grp = rpc.get(grp_url)
            grp = grp[0] if isinstance(grp, list) else grp
            members = list(grp.get("member") or [])
            if name not in members:
                rpc.update(grp_url, {"member": members + [name]})
            log(f"[FortiManager] hub {h}: tunnel-IP's ({name}) toegevoegd aan de CLI-groep")
        except (RpcError, AttributeError, TypeError) as e:
            warn(f"hub {h} tunnel-IP's: {e}")
    name = f"{base}_BRANCH_REDISTRIBUTE"
    try:
        _put_row(rpc, f"/pm/config/adom/{ad}/obj/cli/template", name,
                 {"name": name, "type": "cli", "option": "sdwan-overlay", "script": "\n".join(_REDISTRIBUTE) + "\n",
                  "description": "SD-WAN & FortiAI Lab: LAN's van de branches adverteren"})
        grp_url = f"/pm/config/adom/{ad}/obj/cli/template-group/{base}_BRANCH_CLIGRP"
        grp = rpc.get(grp_url)
        grp = grp[0] if isinstance(grp, list) else grp
        members = list(grp.get("member") or [])
        if name not in members:
            rpc.update(grp_url, {"member": members + [name]})
        log(f"[FortiManager] branches: BGP-redistribute ({name}) toegevoegd aan de CLI-groep")
    except (RpcError, AttributeError, TypeError) as e:
        warn(f"branch BGP-redistribute: {e}")


def build_sdwan(log, topo, rpc: JsonRpc, warn) -> bool:
    """SD-WAN-templates voor hubs en branches: zones, members (underlay + overlay), health-check en rules,
    opgenomen in de template-groepen die de overlay-orchestratie heeft gemaakt."""
    ad, sd = adom(topo), topo["sdwan"]
    hub_t, branch_t = sdwan_template_names(topo)
    ok = True
    spec = {}
    for role, tname in (("hub", hub_t), ("branch", branch_t)):
        members = _members(topo, role)
        base = f"/pm/config/adom/{ad}/wanprof/{tname}/system/sdwan"
        try:
            oid = _wanprof_oid(rpc, ad, tname, f"SD-WAN & FortiAI Lab: {role}s")
            rpc.update(base, {"status": "enable"})
            for z in (sd["zones"]["underlay"], sd["zones"]["overlay"]):
                _put_row(rpc, f"{base}/zone", z, {"name": z})
            for m in members:
                row = {"seq-num": m["seq-num"], "interface": [m["interface"]], "zone": [m["zone"]], "cost": 0}
                if m.get("gateway"):
                    row["gateway"] = m["gateway"]
                _put_row(rpc, f"{base}/members", m["seq-num"], row)
            log(f"[FortiManager] SD-WAN-template {tname}: zones en {len(members)} members")
        except RpcError as e:
            warn(f"SD-WAN-template {tname}: {e}")
            ok = False
            continue
        spec[tname] = {"members": members}
        if role == "branch":
            health, services, addresses = sdwan_rules(topo, members)
            spec[tname].update({"health-check": health, "service": services, "addresses": addresses})
            for ad_obj in addresses:
                try:
                    _put_row(rpc, f"/pm/config/adom/{ad}/obj/firewall/address", ad_obj["name"], ad_obj)
                except RpcError as e:
                    warn(f"adresobject {ad_obj['name']}: {e}")
            for hc in health:
                try:
                    _put_row(rpc, f"{base}/health-check", hc["name"], hc)
                    log(f"[FortiManager] SD-WAN health-check {hc['name']} (SLA) in {tname}")
                except RpcError as e:
                    warn(f"health-check {hc['name']}: {e}")
            for svc in services:
                _add_service(log, warn, rpc, base, svc, tname)
        groups = ([f"{sd['template']}_HUB{i}" for i in range(1, len(hubs(topo)) + 1)] if role == "hub"
                  else [f"{sd['template']}_BRANCH"])
        for grp in groups:
            try:
                _attach(rpc, ad, grp, oid, tname)
                log(f"[FortiManager] {tname} toegevoegd aan template-groep {grp}")
            except (RpcError, KeyError, TypeError) as e:
                warn(f"template-groep {grp}: {e}")
                ok = False
    (GENERATED / "fmg-sdwan.json").write_text(json.dumps(spec, indent=2))
    for legacy in ("LAB-SDWAN", "LAB-SDWAN-BRANCH"):      # restanten van handmatige proefopbouw
        try:
            rpc.delete(f"/pm/wanprof/adom/{ad}/{legacy}")
        except RpcError:
            pass
    return ok


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


def set_locations(log, topo, rpc: JsonRpc, warn):
    """Breedte- en lengtegraad per FortiGate, zodat FortiManager (VPN-kaart) ze op de juiste plek toont."""
    ad = adom(topo)
    n = 0
    for s in topo["sites"]:
        if s.get("lat") is None:
            continue
        lat, lon = str(s["lat"]), str(s["lon"])
        try:
            rpc.update(f"/dvmdb/adom/{ad}/device/{s['fgt_name']}", {"latitude": lat, "longitude": lon})
            n += 1
        except RpcError as e:
            warn(f"locatie {s['fgt_name']}: {e}")
        try:       # ook op de FortiGate zelf (GUI toont de kaartlocatie in System > Settings)
            rpc.update(f"/pm/config/device/{s['fgt_name']}/global/system/global",
                       {"gui-device-latitude": lat, "gui-device-longitude": lon})
        except RpcError:
            pass
    log(f"[FortiManager] locaties gezet op {n} FortiGates")


def default_route_via_sdwan(log, topo, rpc: JsonRpc, warn):
    """Statische default-routes via port2/port3 zijn 'inactive' zodra die poorten SD-WAN-member zijn (branches hebben dan geen
    internet). Vervang ze door één default-route via de underlay-zone van de SD-WAN."""
    zone = topo["sdwan"]["zones"]["underlay"]
    for s in topo["sites"]:
        base = f"/pm/config/device/{s['fgt_name']}/vdom/root/router/static"
        try:
            for rid in (1, 2):
                try:
                    rpc.delete(f"{base}/{rid}")
                except RpcError:
                    pass
            rpc.upsert(base, {"seq-num": 1, "dst": ["0.0.0.0", "0.0.0.0"], "sdwan-zone": [zone], "status": "enable"})
        except RpcError as e:
            warn(f"default-route {s['fgt_name']}: {e}")
    log(f"[FortiManager] default-route via SD-WAN-zone {zone} op {len(topo['sites'])} FortiGates")


def discover_zones(rpc: JsonRpc, topo) -> dict:
    return {"overlay": topo["sdwan"]["zones"]["overlay"], "underlay": topo["sdwan"]["zones"]["underlay"]}


def install(log, topo, rpc: JsonRpc, warn, devices: list[str] | None = None, only: str | None = None):
    """Device-settings (templates) en/of policy package installeren (only: 'device' of 'package')."""
    ad = adom(topo)
    scope = [{"name": n, "vdom": "root"} for n in (devices or device_names(topo))]
    for key, what, url, data in (
            ("device", "device settings (templates)", "/securityconsole/install/device",
             {"adom": ad, "scope": scope, "flags": ["none"]}),
            ("package", "policy package", "/securityconsole/install/package",
             {"adom": ad, "pkg": PKG, "scope": scope, "flags": ["none"]})):
        if only and only != key:
            continue
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
    prepare_network(log, topo)
    console.allow_vm_devices(log, mgmt_ip(topo), get_state()["admin_password"], "FortiManager")
    rpc = wait_api(log, topo)
    ensure_adom(log, rpc, adom(topo), "FortiManager")
    add_devices(log, topo, rpc, warn)
    from . import tz
    tz.fortigates(log, topo, rpc, warn)
    set_locations(log, topo, rpc, warn)
    if build_overlay(log, topo, rpc, warn):
        build_hub_tunnel_ips(log, topo, rpc, warn)
        build_sdwan(log, topo, rpc, warn)
        default_route_via_sdwan(log, topo, rpc, warn)
        # eerst de device-settings (maakt de zones op de FortiGates), daarna pas objecten en policies
        install(log, topo, rpc, warn, devices, only="device")
    zones = discover_zones(rpc, topo)
    log(f"[FortiManager] zones: overlay={zones['overlay']}, underlay={zones['underlay']}")
    policy_package(log, topo, rpc, warn, zones)
    install(log, topo, rpc, warn, devices, only="package")
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
