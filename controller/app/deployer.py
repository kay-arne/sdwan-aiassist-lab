"""Uitrol en afbraak van het lab op de Proxmox host."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import yaml

from . import console, faz, fmg, fortigate, updates
from .config import (DATA, ROOT, UPLOADS, generated_secret, get_state, inventory, load_host, load_topology,
                     mgmt_gw, netmask, nodes_by_role, prefixlen, segment_map, template_vmid, update_state,
                     uplink_tag, vm_name)
from .hostctl import HostError, host

APT_PACKAGES = ("python3 python3-yaml curl ca-certificates openssl dnsutils tcpdump iproute2 iputils-ping "
                "iputils-arping nftables dnsmasq iperf3 netcat-openbsd traceroute mtr-tiny util-linux logrotate "
                "git jq")
TG_SRC = DATA / "trafficgen-src"
TG_FILES = ["trafficgen.py", "netconf.py", "webui.py", "throughput.py", "report.py", "profiles.yaml"]
TG_WEB = ["index.html", "favicon.svg"]
LINKCTL = "/usr/local/sbin/sdwlab-linkctl"
APPLIANCE_IMAGE = {"fmg": "fmg_image", "faz": "faz_image"}


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------
def lab_status() -> dict:
    topo = load_topology()
    h = host()
    present = {r["vmid"]: r for r in json.loads(
        h.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"]))}

    def st(vmid):
        r = present.get(vmid)
        return r.get("status", "unknown") if r else "absent"

    links = set(h.run("ls /sys/class/net").split())
    return {
        "fmg": {"vmid": topo["fortimanager"]["vmid"], "status": st(topo["fortimanager"]["vmid"]),
                "name": vm_name(topo, topo["fortimanager"]["name"])},
        "faz": {"vmid": topo["fortianalyzer"]["vmid"], "status": st(topo["fortianalyzer"]["vmid"]),
                "name": vm_name(topo, topo["fortianalyzer"]["name"])},
        "sites": [{"id": s["id"], "name": s["name"], "role": s["role"], "vmid": s["vmid"], "hostname": s["hostname"],
                   "status": st(s["vmid"]), "mgmt_ip": s["mgmt_ip"], "wan1": s["wan1"], "wan2": s["wan2"],
                   "lan_ip": s["lan_ip"]} for s in topo["sites"]],
        "template": {"vmid": template_vmid(topo), "status": st(template_vmid(topo))},
        "nodes": [{**n, "status": st(n["vmid"])} for n in inventory(topo)],
        "segments": [{**s, "present": s["bridge"] in links} for s in topo["segments"]],
    }


# --------------------------------------------------------------------------------------
# netwerk
# --------------------------------------------------------------------------------------
def ensure_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    created = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            continue
        log(f"bridge {seg['bridge']} aanmaken ({seg['name']}; geen fysieke poort, geen host-IP)")
        h.run(["pvesh", "create", f"/nodes/{h.node}/network", "--iface", seg["bridge"], "--type", "bridge",
               "--autostart", "1", "--comments", f"sdwlab {seg['name']}"])
        created = True
    if created:
        log("netwerkconfig toepassen (ifreload)")
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])


def ensure_host_tools(log):
    """Host-script voor link-impairment (tc op de tap-interfaces) op de Proxmox host zetten."""
    h = host()
    h.run(["mkdir", "-p", h.workdir])
    h.put(ROOT / "host" / "sdwlab-linkctl", f"{h.workdir}/sdwlab-linkctl")
    h.run(["install", "-m", "0755", f"{h.workdir}/sdwlab-linkctl", LINKCTL])
    log(f"{LINKCTL} bijgewerkt op de host")


def remove_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    removed = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            h.run(["pvesh", "delete", f"/nodes/{h.node}/network/{seg['bridge']}"], check=False)
            removed = True
    if removed:
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])
        log("bridges verwijderd")


# --------------------------------------------------------------------------------------
# VM's: FortiGate, FortiManager, FortiAnalyzer
# --------------------------------------------------------------------------------------
def _put_with_progress(log, local: Path, remote: str):
    h = host()
    if h.run(f"test -f {shlex.quote(remote)} && stat -c %s {shlex.quote(remote)}", check=False).strip() \
            == str(local.stat().st_size):
        return
    log(f"{local.name} naar de host kopiëren...")
    last = [0]

    def progress(done, total):
        pct = int(done * 100 / total) if total else 100
        if pct >= last[0] + 20:
            last[0] = pct
            log(f"  {pct}%")
    h.put(local, remote, callback=progress)


def _import_disk(log, vmid, image, storage, index, bus="scsi", opts=""):
    h = host()
    try:
        h.run(["qm", "disk", "import", str(vmid), image, storage], log=log)
    except HostError:
        h.run(["qm", "importdisk", str(vmid), image, storage], log=log)
    unused = re.search(r"^unused0:\s*(\S+)", h.run(["qm", "config", str(vmid)]), re.M)
    if not unused:
        raise RuntimeError("geïmporteerde disk niet gevonden in de VM-config")
    h.run(["qm", "set", str(vmid), f"--{bus}{index}", unused.group(1) + opts], log=log)


def _image(state: dict, key: str, label: str) -> Path:
    name = state.get(key)
    if not name or not (UPLOADS / name).exists():
        raise RuntimeError(f"Geen {label}-image geüpload (Setup).")
    return UPLOADS / name


def deploy_fortigate(log, topo, site):
    h, hcfg, state = host(), load_host(), get_state()
    fg = topo["fortigate"]
    vmid = site["vmid"]
    if h.exists(vmid):
        log(f"{site['hostname']} ({vmid}) bestaat al, overslaan")
        return
    if not state.get("admin_password"):
        raise RuntimeError("Geen admin-wachtwoord ingesteld (Setup).")
    img = _image(state, "fgt_image", "FortiGate")
    h.run(["mkdir", "-p", h.workdir])
    remote = f"{h.workdir}/{img.name}"
    _put_with_progress(log, img, remote)
    iso_local, iso_name = fortigate.build_config_iso(topo, state, site, log)
    h.put(iso_local, f"{h.workdir}/{iso_name}")
    h.run(["install", "-m", "0644", f"{h.workdir}/{iso_name}", f"/var/lib/vz/template/iso/{iso_name}"])
    seg = segment_map(topo)
    nics = [seg["mgmt"]["bridge"], seg["wan-A"]["bridge"], seg["wan-B"]["bridge"],
            seg[site["lan"]]["bridge"]]
    args = ["qm", "create", str(vmid), "--name", site["hostname"], "--ostype", "l26", "--cpu", "host",
            "--cores", str(fg["cores"]), "--memory", str(fg["memory"]), "--scsihw", "virtio-scsi-single",
            "--serial0", "socket", "--tags", f"sdwlab;{site['role']}", "--onboot", "1",
            "--description", f"FortiGate-VM {site['name']} (SD-WAN & FortiAI Lab). port1=mgmt, port2=underlay A, "
                             "port3=underlay B, port4=LAN"]
    for i, b in enumerate(nics):
        args += [f"--net{i}", f"virtio,bridge={b},firewall=0"]
    try:
        log(f"{site['hostname']} ({vmid}) aanmaken (4 NIC's)")
        h.run(args, log=log)
        _import_disk(log, vmid, remote, hcfg["storage"], 0)
        h.run(["qm", "set", str(vmid), "--scsi1", f"{hcfg['storage']}:{fg.get('log_disk_gb', 32)}"], log=log)
        h.run(["qm", "set", str(vmid), "--ide2", f"local:iso/{iso_name},media=cdrom"], log=log)
        h.run(["qm", "set", str(vmid), "--boot", "order=ide2;scsi0;scsi1"], log=log)
    except Exception:
        log(f"aanmaken mislukt; VM {vmid} opruimen")
        h.run(["qm", "destroy", str(vmid), "--purge", "1"], check=False)
        raise
    h.run(["qm", "start", str(vmid)], log=log)


def deploy_appliance(log, topo, kind: str):
    """FortiManager (fmg) of FortiAnalyzer (faz): KVM-image + datadisk, port1 = uplink, port2 = lab-management."""
    h, hcfg, state = host(), load_host(), get_state()
    cfg = topo["fortimanager" if kind == "fmg" else "fortianalyzer"]
    label = "FortiManager" if kind == "fmg" else "FortiAnalyzer"
    vmid = cfg["vmid"]
    if h.exists(vmid):
        log(f"{label} ({vmid}) bestaat al, overslaan")
        return
    key = "fmg_ip" if kind == "fmg" else "faz_ip"
    if not (state.get("wan") or {}).get(key):
        raise RuntimeError(f"Stel eerst het uplink-IP van {label} in (Setup).")
    if not state.get("admin_password"):
        raise RuntimeError("Geen admin-wachtwoord ingesteld (Setup).")
    img = _image(state, APPLIANCE_IMAGE[kind], label)
    h.run(["mkdir", "-p", h.workdir])
    remote = f"{h.workdir}/{img.name}"
    _put_with_progress(log, img, remote)
    seg = segment_map(topo)
    nics = [topo["lab"]["uplink_bridge"] + uplink_tag(topo), seg["mgmt"]["bridge"]]
    args = ["qm", "create", str(vmid), "--name", vm_name(topo, cfg["name"]), "--ostype", "l26", "--cpu", "host",
            "--cores", str(cfg["cores"]), "--memory", str(cfg["memory"]), "--scsihw", "virtio-scsi-single",
            "--serial0", "socket", "--tags", f"sdwlab;{kind}", "--onboot", "1",
            "--description", f"{label}-VM (SD-WAN & FortiAI Lab). port1=uplink (GUI, internet), port2=lab-management"]
    for i, b in enumerate(nics):
        args += [f"--net{i}", f"virtio,bridge={b},firewall=0"]
    try:
        log(f"{label} ({vmid}) aanmaken")
        h.run(args, log=log)
        _import_disk(log, vmid, remote, hcfg["storage"], 0)
        h.run(["qm", "set", str(vmid), "--scsi1", f"{hcfg['storage']}:{cfg['data_disk_gb']}"], log=log)
        h.run(["qm", "set", str(vmid), "--boot", "order=scsi0"], log=log)
    except Exception:
        log(f"aanmaken mislukt; VM {vmid} opruimen")
        h.run(["qm", "destroy", str(vmid), "--purge", "1"], check=False)
        raise
    h.run(["qm", "start", str(vmid)], log=log)
    log(f"{label} start; day-0 volgt via de seriële console")


# --------------------------------------------------------------------------------------
# LXC-template
# --------------------------------------------------------------------------------------
def _labnode_tarball() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="labnode-")) / "labnode.tgz"
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(ROOT / "labnode", arcname="labnode", filter=lambda t: None if "__pycache__" in t.name else t)
    return tmp


def push_labnode(log, vmid):
    h = host()
    h.put(_labnode_tarball(), f"{h.workdir}/labnode.tgz")
    h.run(["pct", "push", str(vmid), f"{h.workdir}/labnode.tgz", "/tmp/labnode.tgz"])
    h.pct_exec(vmid, "rm -rf /opt/labnode && tar xzf /tmp/labnode.tgz -C /opt && "
                     "install -m755 /opt/labnode/tgctl /usr/local/bin/tgctl && "
                     "install -m755 /opt/labnode/dcserver.py /usr/local/bin/dcserver", check=True)


def _is_template(h, vmid) -> bool:
    return bool(re.search(r"^template:\s*1", h.run(["pct", "config", str(vmid)], check=False), re.M))


def wait_for_internet(log, h, vmid, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        if h.pct_exec(vmid, "getent hosts deb.debian.org")[0] == 0:
            log(f"internet OK na {int(time.time() - start)}s")
            return
        time.sleep(3)
    _, diag = h.pct_exec(vmid, "ip -4 -br addr; ip route; cat /etc/resolv.conf")
    raise RuntimeError(f"container {vmid} heeft geen internet via de uplink.\n{diag}")


def build_template(log, topo):
    h, hcfg = host(), load_host()
    vmid = template_vmid(topo)
    if h.exists(vmid):
        if _is_template(h, vmid):
            log(f"template {vmid} bestaat al, overslaan")
            return
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], log=log)
    tnet = topo["lab"]["template_net"]
    bridge = f"{topo['lab']['uplink_bridge']}{uplink_tag(topo)}"
    log(f"template-LXC {vmid} bouwen (tijdelijk op {bridge}, {tnet})")
    h.run(["pct", "create", str(vmid), hcfg["template"], "--hostname", vm_name(topo, "template"), "--cores", "2",
           "--memory", "1024", "--rootfs", f"{hcfg['storage']}:8", "--unprivileged", "1",
           "--net0", f"name=eth0,bridge={bridge},{tnet}", "--tags", "sdwlab"], log=log)
    try:
        h.run(["pct", "start", str(vmid)], log=log)
        wait_for_internet(log, h, vmid)
        h.pct_exec(vmid, f"export DEBIAN_FRONTEND=noninteractive; apt-get update -q && apt-get -yq upgrade && "
                         f"apt-get install -yq {APT_PACKAGES}", timeout=1800, check=True)
        tz = topo["lab"].get("timezone", "Europe/Amsterdam")
        h.pct_exec(vmid, f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime && echo {tz} > /etc/timezone")
        push_labnode(log, vmid)
        h.pct_exec(vmid, "systemctl disable --now dnsmasq iperf3 2>/dev/null; apt-get clean; truncate -s0 /etc/machine-id")
        h.run(["pct", "stop", str(vmid)])
        h.run(["pct", "set", str(vmid), "--delete", "net0"])
        h.run(["pct", "template", str(vmid)])
    except Exception:
        log(f"template-build mislukt; container {vmid} opruimen")
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], check=False)
        raise


# --------------------------------------------------------------------------------------
# LXC's: isp, dcserver, trafficgen
# --------------------------------------------------------------------------------------
def _write_files(vmid, files: dict[str, str], mode="644"):
    h = host()
    for path, content in files.items():
        local = Path(tempfile.mkdtemp(prefix="sdw-")) / "f"
        local.write_text(content)
        h.put(local, f"{h.workdir}/_f")
        h.run(["pct", "push", str(vmid), f"{h.workdir}/_f", path, "--perms", mode])


def _clone(log, topo, node, res):
    h = host()
    vmid, tpl = node["vmid"], template_vmid(topo)
    try:
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"]])
    except HostError:
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"], "--full", "1"])
    h.run(["pct", "set", str(vmid), "--cores", str(res["cores"]), "--memory", str(res["memory"]), "--onboot", "1",
           "--tags", f"sdwlab;{node['role']}", "--description", f"sdwlab {node['role']}: {node.get('note', '')}"])


def isp_nets(topo) -> list[tuple[str, dict]]:
    """NIC's van de ISP-router: eth0 uplink (internet), eth1 lab-management, eth2 = WAN-A, eth3 = WAN-B."""
    seg = segment_map(topo)
    return [("eth1", seg["mgmt"]), ("eth2", seg["wan-A"]), ("eth3", seg["wan-B"])]


def deploy_isp(log, topo, node):
    h = host()
    vmid = node["vmid"]
    wan = get_state().get("wan") or {}
    up = wan.get("isp_ip") or "dhcp"
    up_opts = "ip=dhcp" if up == "dhcp" else f"ip={up},gw={wan.get('gateway', '')}"
    _clone(log, topo, node, topo["resources"]["isp"])
    args = ["pct", "set", str(vmid), "--net0",
            f"name=eth0,bridge={topo['lab']['uplink_bridge']}{uplink_tag(topo)},{up_opts},firewall=0",
            "--nameserver", wan.get("dns1") or "1.1.1.1"]
    for ifc, s in isp_nets(topo):
        n = int(ifc[3:])
        args += [f"--net{n}", f"name={ifc},bridge={s['bridge']},ip={s['gateway']}/{prefixlen(s['subnet'])},firewall=0"]
    h.run(args)
    h.run(["pct", "start", str(vmid)])
    time.sleep(3)
    h.pct_exec(vmid, "systemd-machine-id-setup >/dev/null")
    seg_a, seg_b = segment_map(topo)["wan-A"]["gateway"], segment_map(topo)["wan-B"]["gateway"]
    _write_files(vmid, {
        "/etc/sysctl.d/90-isp.conf": "net.ipv4.ip_forward=1\nnet.ipv4.conf.all.rp_filter=0\n",
        "/etc/nftables.conf": ("flush ruleset\ntable ip nat {\n  chain postrouting {\n"
                               "    type nat hook postrouting priority 100;\n"
                               "    oifname \"eth0\" masquerade\n  }\n}\n"),
        "/etc/dnsmasq.d/lab.conf": ("no-resolv\nserver=1.1.1.1\nserver=9.9.9.9\ninterface=eth1\n"
                                    + "".join(f"interface={i}\n" for i, _ in isp_nets(topo)[1:])
                                    + "bind-interfaces\ncache-size=1000\n"),
    })
    h.pct_exec(vmid, "sysctl --system >/dev/null; systemctl enable nftables dnsmasq; "
                     "systemctl restart nftables dnsmasq", check=True)
    _, rules = h.pct_exec(vmid, "nft list ruleset")
    if "masquerade" not in rules:
        raise RuntimeError("ISP-router: NAT-regel (masquerade) niet actief; controleer /etc/nftables.conf")
    log(f"{node['hostname']}: gateway + NAT + DNS actief op WAN-A ({seg_a}) en WAN-B ({seg_b})")


def deploy_dcserver(log, topo, node):
    h = host()
    vmid = node["vmid"]
    seg = segment_map(topo)[node["segment"]]
    _clone(log, topo, node, topo["resources"]["dcserver"])
    h.run(["pct", "set", str(vmid), "--net0",
           f"name=eth0,bridge={seg['bridge']},ip={node['ip']}/{prefixlen(seg['subnet'])},gw={seg['gateway']},firewall=0",
           "--nameserver", seg["gateway"]])
    h.run(["pct", "start", str(vmid)])
    time.sleep(3)
    h.pct_exec(vmid, "systemd-machine-id-setup >/dev/null")
    unit = ("[Unit]\nDescription=lab dcserver\nAfter=network.target\n[Service]\nExecStart=/usr/local/bin/dcserver\n"
            "Restart=always\n[Install]\nWantedBy=multi-user.target\n")
    iperf = ("[Unit]\nDescription=iperf3 server\nAfter=network.target\n[Service]\nExecStart=/usr/bin/iperf3 -s\n"
             "Restart=always\n[Install]\nWantedBy=multi-user.target\n")
    _write_files(vmid, {"/etc/systemd/system/dcserver.service": unit, "/etc/systemd/system/iperf3-lab.service": iperf})
    h.pct_exec(vmid, "systemctl daemon-reload && systemctl enable --now dcserver iperf3-lab", check=True)
    log(f"{node['hostname']}: web (:80), iperf3 (:5201) op {node['ip']}")


def _trafficgen_src(log) -> Path:
    topo = load_topology()
    tg = topo["traffic_gen"]
    if (TG_SRC / ".git").exists():
        subprocess.run(["git", "-C", str(TG_SRC), "pull", "--quiet", "--ff-only"], capture_output=True, timeout=120)
    else:
        log(f"Traffic-Gen ophalen ({tg['repo']})")
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "-b", tg["branch"], tg["repo"], str(TG_SRC)],
                       check=True, timeout=300)
    return TG_SRC


def traffic_config(topo, node) -> dict:
    seg = segment_map(topo)[node["segment"]]
    site = next(s for s in topo["sites"] if s["lan"] == node["segment"])
    return {"mgmt_interface": "eth0", "duration_minutes": 0, "intensity": "medium", "threads_per_ip": 2,
            "networks": [{"name": f"{site['name']}", "parent": "eth1", "first": node["ip"],
                          "prefix": prefixlen(seg["subnet"]), "count": topo["traffic_gen"]["source_ips"],
                          "gateway": site["lan_ip"], "dns": site["lan_ip"]}],
            "groups": {"office365": 25, "collab": 20, "streaming": 20, "social": 10, "saas": 10, "web": 10,
                       "updates": 3, "download": 2, "sdwan": 5, "webfilter": 0, "malware": 0, "ioc": 0}}


def deploy_trafficgen(log, topo, node):
    h, state = host(), get_state()
    vmid = node["vmid"]
    seg = segment_map(topo)[node["segment"]]
    src = _trafficgen_src(log)
    _clone(log, topo, node, topo["resources"]["trafficgen"])
    h.run(["pct", "set", str(vmid), "--net0", f"name=eth0,bridge={topo['lab']['uplink_bridge']}{uplink_tag(topo)},"
                                              f"ip=dhcp,firewall=0",
           "--net1", f"name=eth1,bridge={seg['bridge']},firewall=0", "--nameserver", "1.1.1.1"])
    h.run(["pct", "start", str(vmid)])
    time.sleep(3)
    h.pct_exec(vmid, "systemd-machine-id-setup >/dev/null")
    tarf = Path(tempfile.mkdtemp(prefix="tg-")) / "tg.tgz"
    with tarfile.open(tarf, "w:gz") as tar:
        for f in TG_FILES:
            tar.add(src / "src" / f, arcname=f"trafficgen/{f}")
        for f in TG_WEB:
            tar.add(src / "src" / "web" / f, arcname=f"trafficgen/web/{f}")
    h.put(tarf, f"{h.workdir}/tg.tgz")
    h.run(["pct", "push", str(vmid), f"{h.workdir}/tg.tgz", "/tmp/tg.tgz"])
    pw = generated_secret("trafficgen_password")
    h.pct_exec(vmid, "mkdir -p /opt /etc/trafficgen/tls /etc/sdwlab /var/log/trafficgen /var/lib/trafficgen && "
                     "tar xzf /tmp/tg.tgz -C /opt && chmod 755 /opt/trafficgen/trafficgen.py && "
                     "ln -sf /opt/trafficgen/trafficgen.py /usr/local/bin/trafficgen", check=True)
    h.pct_exec(vmid, f"openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj '/CN={node['hostname']}' "
                     "-keyout /etc/trafficgen/tls/server.key -out /etc/trafficgen/tls/server.crt 2>/dev/null; "
                     "chmod 600 /etc/trafficgen/tls/server.key", check=True)
    _write_files(vmid, {
        "/etc/trafficgen/config.yaml": yaml.safe_dump(traffic_config(topo, node), sort_keys=False),
        "/etc/sdwlab/networks.json": json.dumps(traffic_config(topo, node)["networks"]),
        "/etc/sdwlab/tg-password": pw + "\n",
        "/root/.tg-pw": pw + "\n",
        "/etc/systemd/system/trafficgen-web.service": (
            "[Unit]\nDescription=Traffic-Gen web GUI\nAfter=network-online.target\n[Service]\n"
            "ExecStart=/usr/bin/python3 /opt/trafficgen/webui.py\nRestart=always\nWorkingDirectory=/opt/trafficgen\n"
            "[Install]\nWantedBy=multi-user.target\n")}, mode="600")
    h.pct_exec(vmid, "/usr/bin/python3 /opt/trafficgen/trafficgen.py passwd --file /root/.tg-pw; rm -f /root/.tg-pw; "
                     "chmod 644 /etc/systemd/system/trafficgen-web.service; systemctl daemon-reload; "
                     "systemctl enable --now trafficgen-web.service", check=True)
    log(f"{node['hostname']}: Traffic-Gen (bronnen {node['ip']} + {topo['traffic_gen']['source_ips'] - 1}) "
        f"- GUI op https://<eth0-ip>:{topo['traffic_gen']['gui_port']}")


def deploy_node(log, topo, node):
    h = host()
    if h.exists(node["vmid"]):
        log(f"{node['hostname']} ({node['vmid']}) bestaat al")
        return
    log(f"{node['hostname']} ({node['vmid']}) [{node['role']}]")
    {"isp": deploy_isp, "dcserver": deploy_dcserver, "trafficgen": deploy_trafficgen}[node["role"]](log, topo, node)


def node_address(vmid: int) -> str:
    """Uplink-IP (eth0) van een LXC, via pct."""
    rc, out = host().pct_exec(vmid, "ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1")
    out = out.strip()
    return out if rc == 0 and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", out) else ""


def update_labnode(log):
    topo = load_topology()
    for n in inventory(topo):
        if host().status(n["vmid"]) == "running":
            push_labnode(log, n["vmid"])
            log(f"labnode bijgewerkt in {n['hostname']}")
    update_state(labnode_tree=updates.labnode_tree())
    apply_traffic_config(log)


def apply_traffic_config(log):
    """Traffic-Gen-config (bronnetwerken) opnieuw schrijven en de service herstarten."""
    topo = load_topology()
    for n in nodes_by_role(topo, "trafficgen"):
        if host().status(n["vmid"]) != "running":
            continue
        _write_files(n["vmid"], {"/etc/trafficgen/config.yaml": yaml.safe_dump(traffic_config(topo, n), sort_keys=False),
                                 "/etc/sdwlab/networks.json": json.dumps(traffic_config(topo, n)["networks"])})
        host().pct_exec(n["vmid"], "systemctl restart trafficgen-web")
        log(f"{n['hostname']}: bronnetwerken opnieuw gezet")


# --------------------------------------------------------------------------------------
# alles
# --------------------------------------------------------------------------------------
def day0_and_license(log, topo, kinds=("fmg", "faz")):
    for kind in kinds:
        mod = fmg if kind == "fmg" else faz
        if not get_state().get(f"{kind}_day0"):
            mod.day0(log, topo)
            update_state(**{f"{kind}_day0": True})
        if not get_state().get(f"{kind}_licensed"):      # losse stap, zodat een mislukte licentie opnieuw wordt geprobeerd
            mod.ensure_license(log, topo)
            update_state(**{f"{kind}_licensed": True})


def provision_all(log, topo):
    log("== FortiManager: devices, SD-WAN overlay template, policies ==")
    fmg.provision(log, topo)
    log("== FortiAnalyzer: devices autoriseren ==")
    faz.provision(log, topo)
    from . import tz
    for label, ip in (("FortiManager", fmg.mgmt_ip(topo)), ("FortiAnalyzer", faz.mgmt_ip(topo))):
        try:
            tz.appliance(log, ip, label, tz.name(topo))
        except Exception as e:  # noqa: BLE001
            log(f"⚠ tijdzone {label}: {e}")
    tz.containers(log, topo)
    for label, ip in (("FortiManager", fmg.mgmt_ip(topo)), ("FortiAnalyzer", faz.mgmt_ip(topo))):
        try:
            console.set_idle_timeout(log, ip, get_state().get("admin_password", ""), label)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ sessie-timeout {label}: {e}")


def deploy_all(log):
    topo = load_topology()
    from . import fortiflex
    if fortiflex.is_api_mode():
        fortiflex.activate_all(log, topo)
    log("== 1/6 netwerk: WAN-A, WAN-B, management en site-LAN's ==")
    ensure_bridges(log, topo)
    ensure_host_tools(log)
    log("== 2/6 ISP-router (internet/DNS voor de FortiGates; moet eerst draaien voor de licentie-activatie) ==")
    build_template(log, topo)
    for node in nodes_by_role(topo, "isp"):
        deploy_node(log, topo, node)
    log("== 3/6 FortiManager en FortiAnalyzer ==")
    deploy_appliance(log, topo, "fmg")
    deploy_appliance(log, topo, "faz")
    log("== 4/6 FortiGates (2 hubs, 3 spokes) en overige helpers (datacenter-servers, Traffic-Gen) ==")
    for site in topo["sites"]:
        deploy_fortigate(log, topo, site)
    for node in inventory(topo):
        if node["role"] != "isp":
            deploy_node(log, topo, node)
    log("== 5/6 day-0 en licenties van FortiManager en FortiAnalyzer ==")
    day0_and_license(log, topo)
    log("== 6/6 provisioning via FortiManager ==")
    provision_all(log, topo)
    _start_background(log, topo)
    update_state(deployed=True, labnode_tree=updates.labnode_tree())


def _start_background(log, topo):
    try:
        from . import scenarios
        scenarios.start_background(log, topo)
    except Exception as e:      # verkeer is bijzaak; de uitrol mag hier niet op stuklopen
        log(f"! achtergrondverkeer niet gestart: {e}")


def component_ids(topo) -> list[str]:
    return (["network", "fmg", "faz"] + [f"fgt:{s['id']}" for s in topo["sites"]] + ["template"]
            + [f"node:{n['name']}" for n in inventory(topo)] + ["config"])


def _release_license(log, key):
    """Bij FortiFlex-API: entitlement stoppen zodra de VM weg is (punten besparen, hergebruik bij uitrol)."""
    from . import fortiflex
    if fortiflex.is_api_mode():
        fortiflex.release(log, key)


def _destroy_vmid(log, h, vmid, label):
    kind = h.exists(vmid)
    if not kind:
        return
    tool = "qm" if kind == "qemu" else "pct"
    log(f"{label} ({vmid}) verwijderen")
    h.run([tool, "stop", str(vmid)], check=False)
    h.run([tool, "destroy", str(vmid), "--purge", "1"], log=log)


def destroy_selected(log, items):
    topo, h, items = load_topology(), host(), set(items)
    for n in [n for n in inventory(topo) if f"node:{n['name']}" in items]:
        _destroy_vmid(log, h, n["vmid"], n["hostname"])
    for s in topo["sites"]:
        if f"fgt:{s['id']}" in items:
            _destroy_vmid(log, h, s["vmid"], s["hostname"])
            _release_license(log, f"fgt:{s['id']}")
    for kind, cfg in (("fmg", topo["fortimanager"]), ("faz", topo["fortianalyzer"])):
        if kind in items:
            _destroy_vmid(log, h, cfg["vmid"], vm_name(topo, cfg["name"]))
            update_state(**{f"{kind}_day0": False, f"{kind}_licensed": False})
            _release_license(log, kind)
    if "template" in items:
        _destroy_vmid(log, h, template_vmid(topo), vm_name(topo, "template"))
    if "network" in items:
        ids = [topo["fortimanager"]["vmid"], topo["fortianalyzer"]["vmid"], template_vmid(topo)] + \
              [s["vmid"] for s in topo["sites"]] + [n["vmid"] for n in inventory(topo)]
        if [v for v in ids if h.exists(v)]:
            log("bridges niet verwijderd: er draaien nog lab-VM's/containers")
        else:
            remove_bridges(log, topo)


def deploy_selected(log, items, recreate=False):
    topo, items = load_topology(), set(items)
    if recreate:
        destroy_selected(log, items - {"network", "config"})
    from . import fortiflex
    if fortiflex.is_api_mode():
        keys = [k for k in items if k in ("fmg", "faz") or k.startswith("fgt:")]
        if keys:
            fortiflex.activate_all(log, topo, keys)
    ensure_bridges(log, topo)
    ensure_host_tools(log)
    nodes = [n for n in inventory(topo) if f"node:{n['name']}" in items]
    if "template" in items or nodes:
        build_template(log, topo)
    for n in sorted(nodes, key=lambda n: n["role"] != "isp"):      # ISP-router eerst
        deploy_node(log, topo, n)
    for kind in ("fmg", "faz"):
        if kind in items:
            deploy_appliance(log, topo, kind)
    for s in topo["sites"]:
        if f"fgt:{s['id']}" in items:
            deploy_fortigate(log, topo, s)
    day0_and_license(log, topo, [k for k in ("fmg", "faz") if k in items])
    if "config" in items or any(i.startswith("fgt:") for i in items):
        provision_all(log, topo)
        _start_background(log, topo)


def destroy_all(log, remove_template=False, remove_net=False):
    topo, h = load_topology(), host()
    ids = ([n["vmid"] for n in inventory(topo)] + [s["vmid"] for s in topo["sites"]]
           + [topo["fortimanager"]["vmid"], topo["fortianalyzer"]["vmid"]])
    if remove_template:
        ids.append(template_vmid(topo))
    for vmid in ids:
        kind = h.exists(vmid)
        if kind:
            tool = "qm" if kind == "qemu" else "pct"
            log(f"{tool} destroy {vmid}")
            h.run([tool, "stop", str(vmid)], check=False)
            h.run([tool, "destroy", str(vmid), "--purge", "1"], check=False)
    from . import fortiflex
    if fortiflex.is_api_mode():
        fortiflex.release_all(log)
    if remove_net:
        remove_bridges(log, topo)
    update_state(deployed=False, fmg_day0=False, faz_day0=False, fmg_licensed=False, faz_licensed=False, fmg_provisioned=False)
