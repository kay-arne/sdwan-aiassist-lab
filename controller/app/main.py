"""Web UI en API van de SD-WAN & FortiAI Lab controller."""
import asyncio
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Optional

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, deployer, faz, fmg, jobs, scenarios, updates
from .config import (DATA, GENERATED, HOST_FILE, ROOT, UPLOADS, generated_secret, get_state, inventory, load_host,
                     load_topology, nodes_by_role, template_vmid, update_state)
from .hostctl import host

HERE = Path(__file__).parent
DOCS = ROOT / "docs"

app = FastAPI(title="SD-WAN & FortiAI Lab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
PUBLIC_PATHS = ("/login", "/static/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PATHS) or auth.valid_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "niet ingelogd"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


STATIC_REF = re.compile(r'(/static/[\w.-]+\.(?:css|js))"')
NO_CACHE = {"Cache-Control": "no-cache"}


def _page(name: str) -> str:
    def versioned(m):
        digest = hashlib.sha1((HERE / m.group(1).lstrip("/")).read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}?v={digest}"'
    return STATIC_REF.sub(versioned, (HERE / "templates" / name).read_text())


def _login_page(error: str = "") -> HTMLResponse:
    block = f'<div class="login-error">{error}</div>' if error else ""
    return HTMLResponse(_page("login.html").replace("<!--ERROR-->", block), status_code=401 if error else 200,
                        headers=NO_CACHE)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.valid_session(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if not auth.check_credentials(username, password):
        await asyncio.sleep(1)
        return _login_page("Onjuiste gebruikersnaam of wachtwoord.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_SECONDS, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_page("index.html"), headers=NO_CACHE)


# --- instellingen ----------------------------------------------------------------------
def public_state() -> dict:
    s = get_state()
    return {
        "wan": s.get("wan") or {},
        "admin_password_set": bool(s.get("admin_password")),
        "admin_password": s.get("admin_password", ""),
        "license_mode": s.get("license_mode", "none"),
        "fortiflex_api": {"username": (s.get("fortiflex_api") or {}).get("username", ""),
                          "password_set": bool((s.get("fortiflex_api") or {}).get("password")),
                          "program_serial": (s.get("fortiflex_api") or {}).get("program_serial", ""),
                          "account_id": (s.get("fortiflex_api") or {}).get("account_id", "")},
        "fortiflex": {"fmg": s.get("fmg_fortiflex_token", ""), "faz": s.get("faz_fortiflex_token", ""),
                      "fgt": s.get("fgt_fortiflex_tokens") or {}},
        "images": {k: s.get(f"{k}_upload") for k in ("fgt", "fmg", "faz")},
        "deployed": bool(s.get("deployed")),
        "problems": (s.get("fmg_problems") or []) + (s.get("faz_problems") or []),
        "fmg_provisioned": bool(s.get("fmg_provisioned")),
    }


@app.get("/api/settings")
def get_settings():
    return public_state()


class Settings(BaseModel):
    wan: Optional[dict[str, str]] = None            # fmg_ip, faz_ip (met prefix), gateway, dns1, dns2, isp_ip
    admin_password: Optional[str] = None
    license_mode: Optional[str] = None
    fortiflex_api: Optional[dict[str, str]] = None   # username, password, program_serial, account_id
    fmg_fortiflex_token: Optional[str] = None
    faz_fortiflex_token: Optional[str] = None
    fgt_fortiflex_tokens: Optional[dict[str, str]] = None


def _cidr(value: str, what: str) -> str:
    try:
        return str(ipaddress.ip_interface(value.strip()))
    except ValueError:
        raise HTTPException(400, f"{what}: geef een IP met prefix op, bijv. 192.168.1.60/24") from None


def _ipv4(value: str, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError:
        raise HTTPException(400, f"{what}: ongeldig IPv4-adres") from None


@app.post("/api/settings")
def save_settings(s: Settings):
    changes = {}
    if s.wan is not None:
        w = {}
        for k in ("fmg_ip", "faz_ip"):
            if s.wan.get(k):
                w[k] = _cidr(s.wan[k], k)
        for k in ("gateway", "dns1", "dns2"):
            if s.wan.get(k):
                w[k] = _ipv4(s.wan[k], k)
        if s.wan.get("isp_ip"):
            w["isp_ip"] = "dhcp" if s.wan["isp_ip"] == "dhcp" else _cidr(s.wan["isp_ip"], "isp_ip")
        changes["wan"] = {**(get_state().get("wan") or {}), **w}
    if s.admin_password is not None:
        pw = s.admin_password
        if len(pw) < 8 or re.search(r"[\s'\"\\]", pw):
            raise HTTPException(400, "Wachtwoord: minimaal 8 tekens, geen spaties, quotes of backslash.")
        changes["admin_password"] = pw
    if s.license_mode is not None:
        if s.license_mode not in ("none", "file", "fortiflex", "fortiflex_api"):
            raise HTTPException(400, "onbekende licentiemodus")
        changes["license_mode"] = s.license_mode
    if s.fortiflex_api is not None:
        cur = dict(get_state().get("fortiflex_api") or {})
        for k in ("username", "password", "program_serial", "account_id"):
            v = (s.fortiflex_api.get(k) or "").strip()
            if v or k in ("program_serial", "account_id"):      # leeg wachtwoord = ongewijzigd laten
                cur[k] = v
        changes["fortiflex_api"] = cur
    for key in ("fmg_fortiflex_token", "faz_fortiflex_token"):
        v = getattr(s, key)
        if v is not None:
            changes[key] = v.strip()
    if s.fgt_fortiflex_tokens is not None:
        changes["fgt_fortiflex_tokens"] = {k: v.strip() for k, v in s.fgt_fortiflex_tokens.items() if v.strip()}
    update_state(**changes)
    return public_state()


async def _save(request: Request, name: str) -> str:
    with open(UPLOADS / name, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return name


@app.post("/api/upload/image")
async def upload_image(request: Request, filename: str, kind: str):
    if kind not in ("fgt", "fmg", "faz"):
        raise HTTPException(400, "kind moet fgt, fmg of faz zijn")
    if not filename.lower().endswith((".qcow2", ".qcow")):
        raise HTTPException(400, "verwacht een .qcow2-bestand")
    target = f"{kind}-image.qcow2"
    await _save(request, target)
    update_state(**{f"{kind}_image": target, f"{kind}_upload": Path(filename).name})
    return public_state()


@app.post("/api/fortiflex/test")
def fortiflex_test():
    from . import fortiflex
    try:
        return fortiflex.test(load_topology())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None


@app.post("/api/fortiflex/configs")
def fortiflex_configs():
    """Maak de lab-configuraties (FortiGate-VM, FortiManager-VM, FortiAnalyzer-VM) aan als ze nog niet bestaan."""
    from . import fortiflex
    topo, lines = load_topology(), []
    try:
        for kind in fortiflex.PRODUCTS:
            lines.append(f"{fortiflex.config_name(topo, kind)}: id {fortiflex.ensure_config(lines.append, topo, kind)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None
    return {"log": lines}


class FlexAction(BaseModel):
    device: str
    action: str            # create | token | stop | reactivate | unlink


@app.get("/api/fortiflex/entitlements")
def fortiflex_entitlements():
    from . import fortiflex
    try:
        return fortiflex.entitlements(load_topology())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None


@app.post("/api/fortiflex/entitlement")
def fortiflex_entitlement(a: FlexAction):
    from . import fortiflex
    topo, lines = load_topology(), []
    keys = [k for k, _ in fortiflex.device_keys(topo)]
    targets = keys if a.device == "all" else [a.device]
    if any(k not in keys for k in targets):
        raise HTTPException(400, "onbekend apparaat")
    try:
        for k in targets:
            if a.action == "activate":
                fortiflex.activate(lines.append, topo, k)
            elif a.action == "create":
                fortiflex.create(lines.append, topo, k)
            elif a.action == "token":
                fortiflex.new_token(lines.append, k)
            elif a.action == "stop":
                fortiflex.release(lines.append, k)
            elif a.action == "reactivate":
                fortiflex.reactivate(lines.append, k)
            elif a.action == "unlink":
                fortiflex.unlink(lines.append, k)
            else:
                raise HTTPException(400, "onbekende actie")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None
    return {"log": lines}


@app.get("/api/fortiflex/status")
def fortiflex_status():
    from . import fortiflex
    try:
        return fortiflex.status(load_topology())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None


@app.post("/api/fortiflex/stop-all")
def fortiflex_stop_all():
    from . import fortiflex
    lines: list[str] = []
    try:
        fortiflex.release_all(lines.append)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None
    return {"log": lines}


@app.post("/api/upload/license")
async def upload_license(request: Request, filename: str, site: str):
    if site not in [s["id"] for s in load_topology()["sites"]]:
        raise HTTPException(400, "onbekende site")
    if not filename.lower().endswith(".lic"):
        raise HTTPException(400, "verwacht een .lic-bestand")
    name = f"fgt-{site}.lic"
    await _save(request, name)
    files = dict(get_state().get("fgt_license_files") or {})
    files[site] = name
    update_state(fgt_license_files=files, license_mode="file")
    return public_state()


# --- host en status ------------------------------------------------------------------------
@app.get("/api/host")
def host_info():
    if not HOST_FILE.exists():
        return {"configured": False}
    h, topo = load_host(), load_topology()
    base = topo["lab"]["vmid_base"]
    pw_file = DATA / "ui_password"
    return {"configured": True, "host": h["host"], "node": h["node"], "ssh_user": h.get("ssh_user", "root"),
            "storage": h["storage"], "template": h["template"], "uplink_bridge": topo["lab"]["uplink_bridge"],
            "vmid_range": f"{base}-{base + 99}", "name_prefix": topo["lab"]["name_prefix"],
            "ui_password": pw_file.read_text().strip() if pw_file.exists() else ""}


@app.post("/api/host/test")
def host_test():
    try:
        h = host()
        who = h.run("id -un").strip()
        v = json.loads(h.run(["pvesh", "get", "/version", "--output-format", "json"]))
        return {"ok": True, "detail": f"ingelogd als {who} op {h.node}; Proxmox VE {v.get('version')}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


@app.get("/api/status")
def status():
    topo = load_topology()
    try:
        lab, err = deployer.lab_status(), None
    except Exception as e:  # noqa: BLE001
        lab = {"fmg": {"status": "unknown", "vmid": topo["fortimanager"]["vmid"]},
               "faz": {"status": "unknown", "vmid": topo["fortianalyzer"]["vmid"]},
               "sites": [{**{k: s[k] for k in ("id", "name", "role", "vmid", "hostname", "mgmt_ip", "wan1", "wan2", "lan_ip")},
                          "status": "unknown"} for s in topo["sites"]],
               "template": {"vmid": template_vmid(topo), "status": "unknown"},
               "nodes": [{**n, "status": "unknown"} for n in inventory(topo)],
               "segments": [{**s, "present": False} for s in topo["segments"]]}
        err = str(e)
    return {"settings": public_state(), "lab": lab, "host_error": err, "jobs": jobs.recent()}


# --- updates ---------------------------------------------------------------------------------
@app.get("/api/version")
def version():
    return updates.version()


@app.post("/api/update/check")
def update_check():
    try:
        return updates.check()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"controleren mislukt: {e}")


@app.post("/api/update/run")
def update_run():
    try:
        updates.start()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/update/log")
def update_log(offset: int = 0):
    return updates.log_lines(offset)


# --- lab-acties ------------------------------------------------------------------------------
def _start(title, fn, *args, exclusive=True):
    try:
        return jobs.start(title, fn, *args, exclusive=exclusive)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


class Selection(BaseModel):
    items: list[str]
    recreate: bool = False


class Destroy(BaseModel):
    template: bool = False
    network: bool = False


def _check_items(items):
    allowed = set(deployer.component_ids(load_topology()))
    bad = [i for i in items if i not in allowed]
    if bad or not items:
        raise HTTPException(400, f"onbekende of geen componenten: {', '.join(bad) or '-'}")
    return items


@app.post("/api/deploy")
def deploy():
    return _start("Volledig lab uitrollen", deployer.deploy_all)


@app.post("/api/provision")
def provision():
    return _start("Provisioning via FortiManager en FortiAnalyzer",
                  lambda log: deployer.provision_all(log, load_topology()))


@app.post("/api/deploy/selected")
def deploy_selected(sel: Selection):
    items = _check_items(sel.items)
    return _start(("Opnieuw uitrollen" if sel.recreate else "Uitrollen") + f" ({len(items)} componenten)",
                  deployer.deploy_selected, items, sel.recreate)


@app.post("/api/destroy/selected")
def destroy_selected(sel: Selection):
    return _start("Verwijderen", deployer.destroy_selected, _check_items(sel.items))


@app.post("/api/destroy")
def destroy(d: Destroy):
    return _start("Lab afbreken", deployer.destroy_all, d.template, d.network)


@app.post("/api/update-labnode")
def update_labnode():
    return _start("Labnode bijwerken", deployer.update_labnode)


@app.post("/api/install")
def fmg_install():
    def run(log):
        topo = load_topology()
        rpc = fmg.wait_api(log, topo)
        problems = []
        fmg.install(log, topo, rpc, lambda m: (problems.append(m), log(f"⚠ {m}")))
        rpc.logout()
    return _start("Installeren vanuit FortiManager", run)


@app.get("/api/jobs/{job_id}")
def job(job_id: str, offset: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return {**{k: v for k, v in j.items() if k != "lines"}, "lines": j["lines"][offset:], "offset": len(j["lines"])}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    if not jobs.stop(job_id):
        raise HTTPException(404, "geen lopende taak met dit id")
    return {"ok": True}


# --- live inzicht -------------------------------------------------------------------------------
@app.get("/api/devices")
def devices():
    try:
        return {"fmg": fmg.device_status(load_topology())}
    except Exception as e:  # noqa: BLE001
        return {"fmg": [], "error": str(e)}


@app.get("/api/sdwan/{device}")
def sdwan_health(device: str):
    try:
        return {"health": scenarios.sdwan_health(load_topology(), device)}
    except Exception as e:  # noqa: BLE001
        return {"health": [], "error": str(e)}


@app.get("/api/links")
def links():
    try:
        return scenarios.links(load_topology())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


class FmgQuery(BaseModel):
    url: str
    option: Optional[str] = None        # bijv. "syntax": schema van het object ophalen
    fields: Optional[list[str]] = None
    filter: Optional[list] = None


@app.post("/api/fmg/get")
def fmg_get(q: FmgQuery):
    """Alleen-lezen JSON-RPC 'get' naar FortiManager, voor het onderzoeken van het API-formaat van jouw versie."""
    from . import fmg
    from .jsonrpc import RpcError
    if not q.url.startswith("/"):
        raise HTTPException(400, "url moet met / beginnen")
    extra = {k: v for k, v in (("option", q.option), ("fields", q.fields), ("filter", q.filter)) if v}
    rpc = fmg.client(load_topology())
    try:
        return {"url": q.url, "data": rpc.get(q.url, **extra)}
    except RpcError as e:
        return {"url": q.url, "error": str(e)}
    finally:
        rpc.logout()


@app.get("/api/fortiai/status")
def fortiai_status():
    return faz.fortiai_status(load_topology())


# --- cockpit ----------------------------------------------------------------------------------------
@app.get("/api/scenarios")
def scenario_catalog():
    cat = scenarios.catalog()
    topo = load_topology()
    return {"scenarios": cat["scenarios"], "presets": list(cat["traffic_presets"]),
            "sites": [{"id": s["id"], "name": s["name"], "role": s["role"], "device": s["fgt_name"]} for s in topo["sites"]],
            "branches": [s["lan"] for s in topo["sites"] if s["role"] == "spoke"]}


class ScenarioRun(BaseModel):
    id: str
    phase: str = "setup"


@app.post("/api/scenarios/run")
def scenario_run(r: ScenarioRun):
    try:
        scenarios.scenario(r.id)
    except StopIteration:
        raise HTTPException(404, "onbekend scenario")
    return _start(f"Scenario {r.id} ({r.phase})", lambda log: scenarios.run_scenario(log, r.id, r.phase),
                  exclusive=False)


class Action(BaseModel):
    do: str
    site: Optional[str] = None
    transport: Optional[str] = None
    preset: Optional[str] = None
    state: Optional[str] = None
    op: Optional[str] = None
    delay: Optional[float] = None
    jitter: Optional[float] = None
    loss: Optional[float] = None
    rate: Optional[float] = None
    to: Optional[str] = None
    seconds: Optional[int] = None
    streams: Optional[int] = None


@app.post("/api/action")
def single_action(a: Action):
    data = {k: v for k, v in a.model_dump().items() if v is not None}
    return _start(f"Actie {a.do} {a.site or ''}", lambda log: scenarios.run_action(log, load_topology(), data),
                  exclusive=False)


# --- toegang en documenten -------------------------------------------------------------------------------
@app.get("/api/access")
def access():
    topo, st = load_topology(), get_state()
    wan = st.get("wan") or {}
    items = []
    for label, key, port in (("FortiManager", "fmg_ip", 443), ("FortiAnalyzer", "faz_ip", 443)):
        ip = (wan.get(key) or "").split("/")[0]
        items.append({"name": label, "url": f"https://{ip}" if ip else "", "user": "admin",
                      "password": st.get("admin_password", ""), "note": "GUI op de uplink; FortiAI-icoon in de banner"})
    for s in topo["sites"]:
        items.append({"name": f"{s['fgt_name']} ({s['name']})", "url": f"https://{s['mgmt_ip']}", "user": "admin",
                      "password": st.get("admin_password", ""),
                      "note": "alleen via lab-management (vanaf FortiManager/FortiAnalyzer of via Proxmox-console)"})
    tg = []
    for n in nodes_by_role(topo, "trafficgen"):
        try:
            ip = deployer.node_address(n["vmid"])
        except Exception:  # noqa: BLE001
            ip = ""
        tg.append({"name": n["hostname"], "url": f"https://{ip}:{topo['traffic_gen']['gui_port']}" if ip else "",
                   "user": "(wachtwoord)", "password": get_state().get("trafficgen_password", ""),
                   "note": f"Traffic-Gen {n['segment']} (bron {n['ip']}...)"})
    return {"platforms": items, "trafficgen": tg}


@app.get("/api/config", response_class=PlainTextResponse)
def generated_config():
    out = []
    for f in sorted(GENERATED.glob("*")):
        out.append(f"### {f.name}\n{f.read_text()}")
    if not out:
        topo = load_topology()
        out.append("### fmg-overlay-template.json (voorbeeld, nog niet uitgerold)\n"
                   + json.dumps(fmg.overlay_payload(topo), indent=2))
        health, services = fmg.sdwan_rules(topo)
        out.append("### fmg-sdwan-rules.json\n" + json.dumps({"health-check": health, "service": services}, indent=2))
    return "\n\n".join(out)


DOC_FILES = [("Labgids", DOCS / "lab-guide.md"), ("FortiAI-use cases", DOCS / "fortiai-usecases.md"),
             ("Praktijkkennis", DOCS / "praktijk.md")]


@app.get("/api/docs")
def docs_list():
    return [{"id": i, "title": t} for i, (t, p) in enumerate(DOC_FILES) if p.exists()]


@app.get("/api/docs/{idx}", response_class=HTMLResponse)
def doc(idx: int):
    if idx < 0 or idx >= len(DOC_FILES) or not DOC_FILES[idx][1].exists():
        raise HTTPException(404)
    return markdown.markdown(DOC_FILES[idx][1].read_text(), extensions=["tables", "fenced_code", "toc"])
