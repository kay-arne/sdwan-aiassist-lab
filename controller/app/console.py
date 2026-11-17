"""Day-0 voor FortiManager en FortiAnalyzer via de seriële console (qm terminal) en daarna SSH-CLI.

FortiManager-VM en FortiAnalyzer-VM hebben geen cloud-init op Proxmox: net als bij het FortiADC-lab
loggen we in via de seriële console in de fabrieksstand (admin zonder wachtwoord), zetten het
wachtwoord en de minimale netwerkconfig en doen de rest via SSH en de JSON-RPC API."""
from __future__ import annotations

import re
import time

import paramiko

from .hostctl import host

PROMPT = re.compile(r"[#$] $")
MORE = re.compile(r"--More--\s*$")
# Tijdelijk wachtwoord voor de verplichte wijziging bij de eerste login (voldoet aan elke complexiteitspolicy);
# daarna zetten we de policy uit en het echte wachtwoord.
TEMP_PASSWORD = "Lab-Temp#2026-Xq9!"
ASK = re.compile(r"(\(y/n\)|password:)\s*$", re.I)


def netmask_of(cidr_prefix: int) -> str:
    import ipaddress
    return str(ipaddress.ip_network(f"0.0.0.0/{cidr_prefix}").netmask)


def bootstrap_cli(*, hostname: str, password: str, ports: dict[str, tuple[str, str]], gateway: str,
                  dns: list[str], timezone_idx: str | None = None) -> list[str]:
    """CLI-regels voor FortiManager/FortiAnalyzer (zelfde syntaxis).
    ports: {'port1': (ip, mask), 'port2': ...}."""
    lines = ["config system global", f"  set hostname {hostname}", "  set admin-timeout 480", "end"]
    lines += ["config system interface"]
    for port, (ip, mask) in ports.items():
        lines += [f"  edit {port}", "    set mode static", f"    set ip {ip} {mask}", "    set allowaccess https ssh ping",
                  "    set status up", "  next"]
    lines += ["end", "config system route", "  edit 1", f"    set gateway {gateway}", "    set device port1",
              "  next", "end", "config system dns", f"  set primary {dns[0]}"]
    if len(dns) > 1:
        lines.append(f"  set secondary {dns[1]}")
    lines += ["end"]
    return lines


def console_bootstrap(log, vmid: int, label: str, password: str, cli: list[str]):
    """Log in op de console (admin, leeg wachtwoord of al het ingestelde), zet het wachtwoord en typ de CLI."""
    h = host()
    log(f"[{label}] day-0 via de seriële console (qm terminal {vmid})")
    client = h._connect()
    chan = client.get_transport().open_session()
    chan.get_pty(width=200)
    chan.exec_command(f"{h.sudo}qm terminal {vmid} -iface serial0")

    def expect(pattern, timeout=60) -> str:
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            if chan.recv_ready():
                buf += chan.recv(65535).decode(errors="replace")
                if re.search(pattern, buf, re.I | re.M):
                    return buf
            else:
                time.sleep(0.2)
        raise RuntimeError(f"console: '{pattern}' niet gezien; laatste output: {buf[-300:]!r}")

    try:
        deadline, out = time.time() + 900, ""
        while time.time() < deadline:           # eerste boot kan een paar minuten duren
            chan.send("\r")
            try:
                out = expect(r"login:\s*$|# $", 20)
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError(f"console van {label}: geen login-prompt binnen 15 minuten")
        if "login" in out.lower():
            chan.send("admin\r")
            expect(r"password:\s*$", 30)
            for candidate in ([password, TEMP_PASSWORD, ""] if password else [TEMP_PASSWORD, ""]):
                chan.send(candidate + "\r")
                out = expect(r"# $|login:\s*$|new password:\s*$|\$ $", 30)
                if re.search(r"new password", out, re.I):         # eerste login: wachtwoord verplicht wijzigen
                    chan.send(TEMP_PASSWORD + "\r")
                    expect(r"(confirm|again|re-?enter).*:\s*$", 15)
                    chan.send(TEMP_PASSWORD + "\r")
                    out = expect(r"# $|\$ $", 30)
                if re.search(r"[#$] ?$", out.rstrip() + " "):
                    break
                chan.send("admin\r")
                expect(r"password:\s*$", 30)
        pre = ["config system password-policy", "  set status disable", "end",
               "config system admin user", "  edit admin", f'    set password "{password}"', "    set rpc-permit read-write", "  next", "end"]
        for line in pre + cli:
            chan.send(line + "\r")
            time.sleep(0.35)
        time.sleep(2)
        chan.send("exit\r")
        time.sleep(1)
        chan.send("\x0f")      # Ctrl-O: qm terminal verlaten
        log(f"[{label}] bootstrap-CLI ingevoerd")
    finally:
        chan.close()


# --- SSH-CLI ------------------------------------------------------------------------------
class Cli:
    def __init__(self, ip: str, password: str, user="admin", timeout=20):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(ip, username=user, password=password, look_for_keys=False, allow_agent=False,
                            timeout=timeout, auth_timeout=timeout)
        self.shell = self.client.invoke_shell(width=250, height=50)
        self._read(8)

    def _read(self, timeout=20) -> str:
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65535).decode(errors="replace")
                buf += chunk
                if MORE.search(buf):
                    self.shell.send(" ")
                    continue
                if ASK.search(buf):
                    return buf
                if PROMPT.search(buf):
                    time.sleep(0.15)
                    if not self.shell.recv_ready():
                        return buf
            else:
                time.sleep(0.1)
        return buf

    def run(self, cmd: str, timeout=30, answer: str | None = None) -> str:
        self.shell.send(cmd + "\n")
        out = self._read(timeout)
        if answer and ASK.search(out):
            self.shell.send(answer + "\n")
            out += self._read(timeout)
        return out

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def enable_rpc(log, ip: str, password: str, label: str):
    """JSON-RPC-login geeft [-11] 'No permission' als admin geen rpc-permit heeft: via SSH-CLI aanzetten."""
    log(f"[{label}] JSON-RPC geweigerd (-11); rpc-permit read-write voor admin zetten via SSH")
    cli = Cli(ip, password)
    try:
        for line in ("config system admin user", "edit admin", "set rpc-permit read-write", "next", "end"):
            cli.run(line)
    finally:
        cli.close()


def ensure_adom_mode(log, ip: str, password: str, label: str):
    """ADOM's staan standaard uit op FortiManager/FortiAnalyzer: aanzetten via de CLI (draait services opnieuw)."""
    cli = Cli(ip, password)
    try:
        status = cli.run("get system status", timeout=30)
        if re.search(r"Admin Domain Configuration\s*:\s*Enabled", status):
            return False
        log(f"[{label}] ADOM-modus is uit; aanzetten (adom-status enable)")
        cli.run("config system global")
        cli.run("set adom-status enable")
        cli.run("end", timeout=30, answer="y")
        time.sleep(45)
        return True
    finally:
        cli.close()


def allow_vm_devices(log, ip: str, password: str, label: str):
    """FortiManager weigert FortiGate-VM's met 'VM device is not allowed' tenzij fgfm-allow-vm aan staat."""
    cli = Cli(ip, password)
    try:
        cli.run("config system global")
        cli.run("set fgfm-allow-vm enable")
        out = cli.run("end", timeout=30, answer="y")
        log(f"[{label}] fgfm-allow-vm enable{(' (' + out.strip()[-80:] + ')') if 'rror' in out else ''}")
    finally:
        cli.close()


def ensure_uplink(log, ip: str, password: str, label: str, uplink: str, mask: str, gateway: str, dns=()):
    """port1 staat op FortiManager/FortiAnalyzer standaard op DHCP; zonder 'set mode static' werkt 'set ip' niet.
    Zet uplink-IP, default route en DNS (idempotent), en controleert of de VM de gateway en DNS bereikt.
    Wordt vóór de licentie-activatie gedraaid, want FortiFlex vraagt internet en DNS."""
    pat = r"\[ port1 \]\s*name: port1\s+status: \w+\s+mode: (\w+)\s+ip: (\S+)"
    cli = Cli(ip, password)
    try:
        m = re.search(pat, cli.run("get system interface", timeout=30))
        if not (m and m.group(1) == "static" and m.group(2) == uplink):
            log(f"[{label}] port1 staat op {m.group(1) + ' ' + m.group(2) if m else '?'}; zetten op static {uplink}")
            for line in ("config system interface", "edit port1", "set mode static", f"set ip {uplink} {mask}",
                         "set allowaccess https ssh ping", "set status up", "next", "end"):
                cli.run(line)
        for line in ("config system route", "edit 1", f"set gateway {gateway}", "set device port1", "next", "end"):
            cli.run(line)
        if dns:
            cli.run("config system dns")
            cli.run(f"set primary {dns[0]}")
            if len(dns) > 1:
                cli.run(f"set secondary {dns[1]}")
            cli.run("end")
        m = re.search(pat, cli.run("get system interface", timeout=30))
        log(f"[{label}] port1: {m.group(1) + ' ' + m.group(2) if m else '?'}; gateway {gateway}; DNS {', '.join(dns) or '-'}")
        gw = cli.run(f"execute ping {gateway}", timeout=40)
        ok_gw = bool(re.search(r"[1-9]\d* packets received", gw))
        nm = cli.run("execute ping www.fortiguard.com", timeout=40)
        ok_dns = "bad address" not in nm and bool(re.search(r"[1-9]\d* packets received", nm))
        log(f"[{label}] gateway {'bereikbaar' if ok_gw else 'NIET bereikbaar'}; "
            f"DNS/internet {'OK' if ok_dns else 'MISLUKT'}")
        if not ok_dns:
            log(f"[{label}] ping-uitvoer: {nm.strip()[-200:]}")
    finally:
        cli.close()


def activate_vm_license(log, ip: str, password: str, label: str, token: str, attempts=8, wait=60):
    """execute vm-license <token> met herhaling: een net aangemaakte FortiFlex-entitlement is soms pas na enkele
    minuten op te halen ('get license by token failed'). Succes = de VM meldt dat hij herstart."""
    last = ""
    for n in range(1, attempts + 1):
        cli = wait_ssh(log, ip, password, label)
        try:
            out = cli.run(f"execute vm-license {token}", timeout=120, answer="y")
            last = out.strip()[-200:]
            if re.search(r"will reboot|reboot to apply|rebooting", out, re.I) and "failed" not in out.lower():
                log(f"[{label}] FortiFlex-token geactiveerd; VM herstart")
                return
            log(f"[{label}] licentie ophalen mislukt (poging {n}/{attempts}): {last or 'geen uitvoer'}")
            if n == 1:     # eenmalig: staat de VM wel op internet en DNS?
                diag = cli.run("execute ping www.fortiguard.com", timeout=40)
                log(f"[{label}] internetcontrole: {diag.strip()[-160:]}")
        finally:
            cli.close()
        time.sleep(wait)
    raise RuntimeError(f"{label}: FortiFlex-token niet geaccepteerd na {attempts} pogingen ({last}). "
                       "Controleer internet/DNS op port1 en of de entitlement actief is in het FortiFlex-portaal.")


def wait_ssh(log, ip: str, password: str, label: str, timeout=900) -> Cli:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            return Cli(ip, password)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10)
    raise RuntimeError(f"{label}: SSH op {ip} niet bereikbaar na {timeout}s ({last})")
