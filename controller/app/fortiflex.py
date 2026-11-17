"""FortiFlex (v2) via directe API-toegang: configuraties, entitlements en tokens voor het lab.

De controller regelt zelf de levenscyclus van de licenties, zodat je bij een nieuwe uitrol geen tokens
meer hoeft te maken:

  uitrollen   -> entitlement aanmaken (of hergebruiken: reactiveren + nieuwe token) en de token in de VM zetten
  verwijderen -> entitlement stoppen (geen punten meer verbruiken); de entitlement blijft bestaan voor hergebruik

Authenticatie: een API-gebruiker uit de FortiCloud IAM-portal (rechten op FortiFlex), grant_type password,
client_id 'flexvm'. Het access-token wordt gecached en bij 'Invalid security token' opnieuw opgehaald.
Endpoints en parameter-ID's volgen Fortinets eigen Ansible/Terraform-collecties voor FortiFlex v2."""
from __future__ import annotations

import threading
import time

import requests

from .config import get_state, update_state, vm_name

AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
API_URL = "https://support.fortinet.com/ES/api/fortiflex/v2/"

# Producttype- en parameter-ID's (FortiFlex v2)
PRODUCTS = {
    "fgt": {"product": 1, "params": {"cpu": 1, "service": 2, "vdom": 10, "fortiGuardServices": 43,
                                      "cloudServices": 44, "supportService": 45}},
    "fmg": {"product": 2, "params": {"device": 30, "adom": 9, "service": 108, "addons": 109, "fortiAITokens": 110}},
    "faz": {"product": 7, "params": {"storage": 21, "adom": 22, "service": 23, "addons": 58}},
}
DEFAULTS = {
    "fgt": {"cpu": 2, "service": "UTP"},
    "fmg": {"device": 20, "adom": 5, "service": "FMGFC247", "addons": ["FMGAISN"], "fortiAITokens": 100000},
    "faz": {"storage": 10, "adom": 5, "service": "FAZFC247", "addons": ["FAZAISN"]},
}
LABEL = {"fgt": "FortiGate-VM", "fmg": "FortiManager-VM", "faz": "FortiAnalyzer-VM"}

_lock = threading.Lock()
_token = {"value": "", "key": "", "at": 0.0}


class FlexError(RuntimeError):
    pass


# --- toegang ---------------------------------------------------------------------------
def creds() -> dict:
    c = get_state().get("fortiflex_api") or {}
    if not c.get("username") or not c.get("password"):
        raise FlexError("FortiFlex API-gebruiker en wachtwoord zijn niet ingesteld (Setup > Licenties).")
    return c


def is_api_mode() -> bool:
    return get_state().get("license_mode") == "fortiflex_api"


def _login(c: dict) -> str:
    r = requests.post(AUTH_URL, json={"username": c["username"], "password": c["password"],
                                      "client_id": "flexvm", "grant_type": "password"}, timeout=30)
    if r.status_code >= 400:
        raise FlexError(f"FortiFlex-login mislukt (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def call(path: str, data: dict | None = None) -> dict:
    c = creds()
    key = c["username"] + c["password"]
    for attempt in (1, 2):
        with _lock:
            if not _token["value"] or _token["key"] != key or time.time() - _token["at"] > 1500:
                _token.update(value=_login(c), key=key, at=time.time())
            bearer = _token["value"]
        r = requests.post(API_URL + path, json=data or {}, headers={"Authorization": f"Bearer {bearer}"}, timeout=60)
        try:
            body = r.json()
        except ValueError:
            raise FlexError(f"{path}: HTTP {r.status_code}: {r.text[:200]}") from None
        if body.get("status") == -1 and "security token" in str(body.get("message", "")).lower() and attempt == 1:
            _token["value"] = ""
            continue
        if r.status_code >= 400 or body.get("status", 0) not in (0, None):
            raise FlexError(f"{path}: {body.get('message') or body}")
        return body
    raise FlexError(f"{path}: authenticatie mislukt")


def _one(x):
    return (x[0] if x else {}) if isinstance(x, list) else (x or {})


def program_serial() -> str:
    c = creds()
    if c.get("program_serial"):
        return c["program_serial"]
    progs = call("programs/list").get("programs") or []
    if not progs:
        raise FlexError("Geen FortiFlex-programma gevonden voor deze API-gebruiker.")
    return progs[0]["serialNumber"]


def _base(c: dict) -> dict:
    out = {}
    if c.get("account_id"):
        out["accountId"] = int(c["account_id"])
    return out


# --- configuraties ---------------------------------------------------------------------
def config_name(topo, kind: str) -> str:
    return f"{topo['lab']['name_prefix']}-{kind}"


def spec(topo, kind: str) -> dict:
    return {**DEFAULTS[kind], **((topo.get("fortiflex") or {}).get(kind) or {})}


def _parameters(kind: str, values: dict) -> list[dict]:
    ids, out = PRODUCTS[kind]["params"], []
    for name, value in values.items():
        if name not in ids or value in (None, "", []):
            continue
        for v in (value if isinstance(value, list) else [value]):
            out.append({"id": ids[name], "value": v})
    return out


def ensure_config(log, topo, kind: str) -> int:
    """Zoek de lab-configuratie op naam of maak hem aan (parameters uit topology.yaml > fortiflex)."""
    c = creds()
    name = config_name(topo, kind)
    cfgs = call("configs/list", {"programSerialNumber": program_serial(), **_base(c)}).get("configs") or []
    for cfg in cfgs:
        if cfg.get("name") == name:
            return int(cfg["id"])
    values = spec(topo, kind)
    res = call("configs/create", {"programSerialNumber": program_serial(), "name": name,
                                  "productTypeId": PRODUCTS[kind]["product"],
                                  "parameters": _parameters(kind, values), **_base(c)})
    cfg = _one(res.get("configs"))
    log(f"[FortiFlex] configuratie {name} aangemaakt ({LABEL[kind]}: {values})")
    return int(cfg["id"])


# --- entitlements ----------------------------------------------------------------------
def _kind(key: str) -> str:
    return key.split(":")[0]


def _desc(topo, key: str) -> str:
    return f"{topo['lab']['name_prefix']} {key}"


def _list(**filters) -> list[dict]:
    return call("entitlements/list", filters).get("entitlements") or []


def _find(topo, key: str, config_id: int) -> dict | None:
    serial = (get_state().get("fortiflex_entitlements") or {}).get(key)
    if serial:
        found = _list(serialNumber=serial)
        if found:
            return found[0]
    for e in _list(configId=config_id):          # na verlies van state: terugvinden op beschrijving
        if e.get("description") == _desc(topo, key):
            return e
    return None


def _remember(key: str, serial: str):
    ents = dict(get_state().get("fortiflex_entitlements") or {})
    ents[key] = serial
    update_state(fortiflex_entitlements=ents)


def acquire(log, topo, key: str) -> str:
    """Geef een geldige, ongebruikte VM-token voor het apparaat (key: fgt:<site>, fmg, faz)."""
    kind = _kind(key)
    config_id = ensure_config(log, topo, kind)
    ent = _find(topo, key, config_id)
    if ent and ent.get("status") == "EXPIRED":
        ent = None
    if not ent:
        res = call("entitlements/vm/create", {"configId": config_id, "count": 1, "description": _desc(topo, key)})
        ent = _one(res.get("entitlements"))
        _remember(key, ent["serialNumber"])
        log(f"[FortiFlex] entitlement {ent['serialNumber']} aangemaakt voor {key}")
        return ent["token"]
    _remember(key, ent["serialNumber"])
    if ent.get("status") == "STOPPED":
        call("entitlements/reactivate", {"serialNumber": ent["serialNumber"]})
        log(f"[FortiFlex] entitlement {ent['serialNumber']} ({key}) gereactiveerd")
        ent = (_list(serialNumber=ent["serialNumber"]) or [ent])[0]
    if ent.get("tokenStatus") == "USED" or not ent.get("token"):
        res = call("entitlements/vm/token", {"serialNumber": ent["serialNumber"]})
        ent = _one(res.get("entitlements")) or ent
        log(f"[FortiFlex] nieuwe token voor {key} ({ent['serialNumber']})")
    return ent["token"]


def release(log, key: str):
    """Entitlement stoppen (geen punten meer); wordt bij de volgende uitrol gereactiveerd."""
    serial = (get_state().get("fortiflex_entitlements") or {}).get(key)
    if not serial:
        return
    try:
        call("entitlements/stop", {"serialNumber": serial})
        log(f"[FortiFlex] entitlement {serial} ({key}) gestopt")
    except FlexError as e:
        log(f"⚠ [FortiFlex] stoppen van {serial} ({key}) mislukt: {e}")


def release_all(log):
    for key in list((get_state().get("fortiflex_entitlements") or {})):
        release(log, key)


def status(topo) -> list[dict]:
    out = []
    for key, serial in (get_state().get("fortiflex_entitlements") or {}).items():
        found = _list(serialNumber=serial)
        e = found[0] if found else {}
        out.append({"device": key, "serial": serial, "status": e.get("status", "onbekend"),
                    "token_status": e.get("tokenStatus", ""), "config_id": e.get("configId")})
    return out


def test(topo) -> dict:
    """Verbinding testen: programma's en bestaande lab-configuraties."""
    c = creds()
    progs = call("programs/list").get("programs") or []
    cfgs = call("configs/list", {"programSerialNumber": program_serial(), **_base(c)}).get("configs") or []
    names = {config_name(topo, k): k for k in PRODUCTS}
    return {"programs": [p.get("serialNumber") for p in progs],
            "program_used": program_serial(),
            "lab_configs": {n: any(cf.get("name") == n for cf in cfgs) for n in names}}
