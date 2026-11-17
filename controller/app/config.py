"""Paden, topologie en persistente state van de controller."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import string
import threading
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("SDWLAB_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
GENERATED = DATA / "generated"      # door de controller gegenereerde config (te bekijken in de UI)
TOPOLOGY_FILE = ROOT / "lab" / "topology.yaml"
STATE_FILE = DATA / "state.json"
HOST_FILE = DATA / "host.json"

for _d in (DATA, UPLOADS, GENERATED):
    _d.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def load_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        topo = yaml.safe_load(f)
    hcfg = load_host() if HOST_FILE.exists() else {}
    lab = topo["lab"]
    lab["uplink_bridge"] = hcfg.get("uplink_bridge") or lab["uplink_bridge"]
    lab["uplink_vlan"] = str(hcfg.get("uplink_vlan") or lab.get("uplink_vlan") or "")
    lab["template_net"] = hcfg.get("template_net") or lab.get("template_net", "ip=dhcp")
    lab["name_prefix"] = hcfg.get("name_prefix") or lab.get("name_prefix") or "sdwlab"
    if hcfg.get("vmid_base"):
        lab["vmid_base"] = int(hcfg["vmid_base"])
    base = lab["vmid_base"]
    topo["fortimanager"]["vmid"] = base + 2
    topo["fortianalyzer"]["vmid"] = base + 3
    for i, s in enumerate(topo["sites"]):
        s["vmid"] = base + 10 + i
        s["hostname"] = vm_name(topo, "fgt-" + s["id"])
        s["fgt_name"] = "FGT-" + s["id"].upper()          # naam in FortiManager/FortiOS
        s["idx"] = i + 1
        for key, (tid, u) in zip(("wan1", "wan2"), topo["underlays"].items()):
            net = f"{u['net']}.{s['idx']}"
            s[key] = f"{net}.2"
            s[key + "_gw"] = f"{net}.1"
            topo["segments"].append({"id": f"{key}-{s['id']}", "name": f"{u['name']} - {s['name']}",
                                     "bridge": f"vmbr{u['bridge_base'] + s['idx']}", "subnet": f"{net}.0/24",
                                     "gateway": f"{net}.1", "kind": "wan", "site": s["id"], "transport": tid})
    state = get_state()
    topo["wan"] = {**{"mode": "static"}, **state.get("wan", {})}      # uplink-instellingen voor FMG/FAZ
    return topo


def vm_name(topo: dict, name: str) -> str:
    return f"{topo['lab']['name_prefix']}-{name}"


def uplink_tag(topo: dict) -> str:
    vlan = topo["lab"].get("uplink_vlan")
    return f",tag={vlan}" if vlan else ""


def template_vmid(topo: dict) -> int:
    return topo["lab"]["vmid_base"] + 1


def inventory(topo: dict) -> list[dict]:
    """Alle LXC's met vaste VMID's (base+30 en verder)."""
    base = topo["lab"]["vmid_base"]
    return [{**n, "vmid": base + 30 + i, "hostname": vm_name(topo, n["name"]), "kind": "node"}
            for i, n in enumerate(topo["nodes"])]


def node(topo: dict, name: str) -> dict:
    return next(n for n in inventory(topo) if n["name"] == name)


def nodes_by_role(topo: dict, role: str) -> list[dict]:
    return [n for n in inventory(topo) if n["role"] == role]


def segment_map(topo: dict) -> dict:
    return {s["id"]: s for s in topo["segments"]}


def site_map(topo: dict) -> dict:
    return {s["id"]: s for s in topo["sites"]}


def hubs(topo: dict) -> list[dict]:
    return sorted((s for s in topo["sites"] if s["role"] == "hub"), key=lambda s: s["hub_rank"])


def spokes(topo: dict) -> list[dict]:
    return [s for s in topo["sites"] if s["role"] == "spoke"]


def prefixlen(subnet: str) -> int:
    return ipaddress.ip_network(subnet).prefixlen


def netmask(subnet: str) -> str:
    return str(ipaddress.ip_network(subnet).netmask)


def mgmt_gw(topo: dict) -> str:
    return segment_map(topo)["mgmt"]["gateway"]


# --- state -----------------------------------------------------------------------
def load_host() -> dict:
    with open(HOST_FILE) as f:
        return json.load(f)


def get_state() -> dict:
    with _lock:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def update_state(**changes) -> dict:
    with _lock:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        state.update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
        return state


def update_nested(key: str, sub: str, **changes):
    with _lock:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        state.setdefault(key, {}).setdefault(sub, {}).update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)


def generated_secret(key: str, length: int = 14) -> str:
    value = get_state().get(key)
    if not value:
        value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))
        update_state(**{key: value})
    return value
