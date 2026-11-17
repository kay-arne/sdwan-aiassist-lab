"""FortiGate-VM's: day-0 (cloud-init config drive). Alles daarna loopt via FortiManager.

De bootstrap is bewust minimaal: management (port1), de twee underlay-poorten (port2/3), de LAN-
poort (port4), een default route via underlay A, DNS en de koppeling met FortiManager. FortiManager
neemt het device daarna over (fmg.py) en bouwt de SD-WAN overlay, policies en logging."""
from __future__ import annotations

import tempfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import subprocess

from .config import UPLOADS, get_state, mgmt_gw, netmask, prefixlen, segment_map

ISO_FMT = "{prefix}-fgt-{site}-cfg.iso"


def lic_of(site_id: str) -> dict:
    s = get_state()
    return {"mode": s.get("license_mode", "none"),
            "file": (s.get("fgt_license_files") or {}).get(site_id),
            "token": (s.get("fgt_fortiflex_tokens") or {}).get(site_id)}


def bootstrap_config(topo: dict, state: dict, site: dict) -> str:
    seg = segment_map(topo)
    fmg = topo["fortimanager"]["mgmt_ip"]
    pw = state.get("admin_password") or ""
    lan = seg[site["lan"]]
    mgmt = seg["mgmt"]
    w1 = seg["wan-A"]
    w2 = seg["wan-B"]
    dns = w1["gateway"]
    lines = [
        "config system global",
        f"  set hostname {site['fgt_name']}",
        "  set admintimeout 480",
        "  set admin-sport 443",
        "  set gui-theme jade",
        "end",
        "config system password-policy",
        "  set status disable",
        "end",
        "config system admin",
        "  edit admin",
        f'    set password "{pw}"',
        "  next",
        "end",
        "config system interface",
        "  edit port1",
        "    set mode static",
        f"    set ip {site['mgmt_ip']} {netmask(mgmt['subnet'])}",
        "    set allowaccess ping https ssh fgfm",
        '    set alias "MGMT"',
        "  next",
        "  edit port2",
        "    set mode static",
        f"    set ip {site['wan1']} {netmask(w1['subnet'])}",
        "    set allowaccess ping",
        '    set alias "UNDERLAY-A"',
        "    set role wan",
        "  next",
        "  edit port3",
        "    set mode static",
        f"    set ip {site['wan2']} {netmask(w2['subnet'])}",
        "    set allowaccess ping",
        '    set alias "UNDERLAY-B"',
        "    set role wan",
        "  next",
        "  edit port4",
        "    set mode static",
        f"    set ip {site['lan_ip']} {netmask(lan['subnet'])}",
        "    set allowaccess ping https ssh",
        f'    set alias "LAN-{site["id"].upper()}"',
        "    set role lan",
        "  next",
        "end",
        "config router static",
        "  edit 1",
        f"    set gateway {w1['gateway']}",
        "    set device port2",
        "    set distance 10",
        "  next",
        "  edit 2",
        f"    set gateway {w2['gateway']}",
        "    set device port3",
        "    set distance 20",
        "  next",
        "end",
        "config system dns",
        f"  set primary {dns}",
        f"  set secondary {w2['gateway']}",
        "end",
        "config system dns-server",
        "  edit port4",
        "    set mode recursive",
        "  next",
        "end",
        # Beheer door FortiManager; FortiManager verbindt zelf (add device), dit is de terugkoppeling.
        "config system central-management",
        "  set type fortimanager",
        f"  set fmg {fmg}",
        "end",
        # Eerste logging: FortiManager zet dit later via de template opnieuw (idempotent)
        "config log fortianalyzer setting",
        "  set status enable",
        f"  set server {topo['fortianalyzer']['mgmt_ip']}",
        "  set upload-option realtime",
        "  set reliable enable",
        "end",
    ]
    return "\n".join(lines) + "\n"


def mime_multipart(parts: list[tuple[str, str]]) -> str:
    msg = MIMEMultipart()
    for filename, text in parts:
        sub = MIMEText(text, "plain", "us-ascii")
        sub.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(sub)
    return msg.as_string()


def build_config_iso(topo: dict, state: dict, site: dict, log=None) -> tuple[Path, str]:
    """Config drive (cloud-init): MIME-multipart user_data met de configuratie en de licentie."""
    out = Path(tempfile.mkdtemp(prefix="fgtiso-"))
    work = out / "config-drive"
    (work / "openstack" / "latest").mkdir(parents=True)
    (work / "openstack" / "content").mkdir(parents=True)
    parts = [("config.txt", bootstrap_config(topo, state, site))]
    lic = lic_of(site["id"])
    if lic["mode"] == "fortiflex_api":
        from . import fortiflex
        lic = {"mode": "fortiflex", "file": None,
               "token": fortiflex.acquire(log or (lambda m: None), topo, f"fgt:{site['id']}")}
    if lic["mode"] == "file" and lic["file"] and (UPLOADS / lic["file"]).exists():
        parts.append(("license.txt", (UPLOADS / lic["file"]).read_text().strip() + "\n"))
    elif lic["mode"] == "fortiflex" and lic["token"]:
        parts.append(("license.txt", f"LICENSE-TOKEN:{lic['token']}\n"))
    (work / "openstack" / "latest" / "user_data").write_text(mime_multipart(parts))
    name = ISO_FMT.format(prefix=topo["lab"]["name_prefix"], site=site["id"])
    iso = out / name
    subprocess.run(["genisoimage", "-quiet", "-R", "-r", "-V", "config-2", "-o", str(iso), str(work)], check=True)
    return iso, name
