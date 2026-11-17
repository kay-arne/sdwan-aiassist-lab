# SD-WAN & FortiAI Lab op Proxmox

Een volledig virtueel **Fortinet SD-WAN-lab**: twee hubs (datacenters Amsterdam en Frankfurt) en drie branches met elk twee
WAN-links, allemaal **FortiGate-VM's beheerd door FortiManager**. De SD-WAN-topologie (IPsec-overlay, BGP, ADVPN,
SD-WAN-zones, health-checks en business-intent-rules) wordt uit **templates** opgebouwd; alle logs gaan naar **FortiAnalyzer**.
**Traffic-Gen** ([s-ruiter/Traffic-Gen](https://github.com/s-ruiter/Traffic-Gen)) genereert realistisch gebruikersverkeer per branch,
en een **demo-cockpit** veroorzaakt storingen en incidenten waarop je **FortiAI-Assist** (in FortiManager en FortiAnalyzer 7.6+)
loslaat. Zelfde opzet als het [FortiGate OT Lab](https://github.com/kay-arne/fortigate-ot-lab) en het
[FortiADC Lab](https://github.com/kay-arne/fortiadc-lab): één installer op de Proxmox host, een controller-LXC met web UI.

```
                      uplink (vmbr0): FMG/FAZ GUI + internet (FortiGuard, FortiAI), ISP-uplink, Traffic-Gen GUI's
                                  │ port1                           │ port1                    │ eth0
                           ┌──────┴──────┐                   ┌──────┴──────┐            ┌──────┴───────┐
                           │ FortiManager│                   │FortiAnalyzer│            │  ISP-router  │  routing, NAT, DNS,
                           └──────┬──────┘                   └──────┬──────┘            │  (LXC)       │  tc/netem per link
                                  │ port2 ── lab-management 10.255.0.0/24 ──┘            └───┬──────┬───┘
                                  │                                                      A│      │B   (10 toegangslinks,
        ┌────────────┬────────────┼────────────┬────────────┐                              │      │     elk een eigen bridge)
   FGT-HUB-AMS   FGT-HUB-FRA   FGT-BR1      FGT-BR2      FGT-BR3  ←── port2 = underlay A, port3 = underlay B, port4 = LAN
   srv-ams       srv-fra       tg-br1       tg-br2       tg-br3
   (DC-apps)     (DC-apps)     (Traffic-Gen in elk van de branches)
```

| Component | Type | Inhoud |
|---|---|---|
| `sdwlab-controller` | LXC | web UI, Proxmox-orchestratie, FortiManager/FortiAnalyzer-provisioning, demo-cockpit |
| `sdwlab-fmg`, `sdwlab-faz` | VM | FortiManager 7.6 en FortiAnalyzer 7.6 (port1 uplink, port2 lab-management), elk met datadisk |
| `sdwlab-fgt-hub-ams/-fra`, `-br1/2/3` | VM | FortiGate-VM (FortiOS 7.6): port1 mgmt, port2 underlay A, port3 underlay B, port4 LAN |
| `sdwlab-isp` | LXC | underlay-router: routeert tussen alle toegangslinks, NAT naar internet, DNS en `linkctl` (tc netem) |
| `sdwlab-srv-ams/-fra` | LXC | datacenter-apps (HTTP, iperf3) achter de hubs; doel van de SLA-health-check |
| `sdwlab-tg-br1/2/3` | LXC | Traffic-Gen met zes bron-IP's per branch; GUI op de uplink |

## Installatie

Op de Proxmox host (als root):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/sdwan-aiassist-lab/main/install.sh)"
```

Net als bij de andere labs: **Standaard** of **Geavanceerd** (storage, bridge, VLAN, IP, VMID-reeks, aparte host-gebruiker met
beperkte sudo; intrekken met `sdwlab-revoke`). Opnieuw draaien biedt **Bijwerken** aan. Onbeheerd: `SDWLAB_UNATTENDED=1`, zie de kop
van [install.sh](install.sh).

## Gebruik

1. **Setup**: upload de KVM-images (`FGT_VM64_KVM-v7.6.x`, `FMG_VM64_KVM-v7.6.x`, `FAZ_VM64_KVM-v7.6.x`; elk het `.qcow2`-bestand),
   vul de uplink-IP's van FortiManager en FortiAnalyzer in (plus gateway/DNS), kies een admin-wachtwoord en de licentievorm
   (FortiFlex-tokens of `.lic`).
2. **Lab → Volledig lab uitrollen**. De controller:
   1. maakt de bridges (`vmbr600`, `611-612`, `621-623`, `631-635`, `641-645`; geen fysieke poort, geen host-IP);
   2. maakt FortiManager en FortiAnalyzer en de vijf FortiGates (FortiGates via een cloud-init-config-drive met interfaces, DNS, logging en licentie);
   3. bouwt één Debian 12-LXC-template en kloont daaruit ISP-router, datacenter-servers en Traffic-Gen (de repo van Traffic-Gen wordt met `git` opgehaald);
   4. doet day-0 van FortiManager en FortiAnalyzer via de seriële console (netwerk, wachtwoord, FortiFlex);
   5. laat **FortiManager** de FortiGates toevoegen, de **SD-WAN Overlay Template** (`LAB-OVERLAY`) bouwen en orchestreren, de
      health-check (SLA) en de SD-WAN-rules in de gegenereerde template zetten, een policy package met UTM-profielen maken en alles installeren;
   6. laat **FortiAnalyzer** de FortiGates autoriseren zodat de logging binnenkomt.
3. **Cockpit**: negen scenario's met de bijbehorende **FortiAI-vragen**; zie [docs/lab-guide.md](docs/lab-guide.md) en
   [docs/fortiai-usecases.md](docs/fortiai-usecases.md).
4. **Toegang & gidsen**: GUI-adressen, logins, Traffic-Gen-adressen en de documentatie.

## FortiAI-Assist: wat je nodig hebt

FortiAI-Assist (FortiManager/FortiAnalyzer 7.6.x en nieuwer) is een licentieonderdeel (FortiAI-licentie, maandelijks tokenbudget, gedeeld door de
gebruikers) en praat met Fortinets cloud: FortiManager en FortiAnalyzer hebben dus **uitgaand internet vanaf port1** nodig. Alleen lokale
administrators kunnen het gebruiken. De Setup-/FortiAI-tab controleert het AISN-contract (`diagnose fmupdate dbcontract`). De licentie
koppel je via FortiCare aan het serienummer van de FortiManager/FortiAnalyzer; dat kan de controller niet voor je doen.

## Wat zit erin

| Bestand | Inhoud |
|---|---|
| `lab/topology.yaml` | **één bron**: segmenten, sites, SD-WAN-overlay, SLA, rules, helpers |
| `lab/scenarios.yaml` | cockpit-scenario's, verkeerspresets en alle FortiAI-prompts |
| `controller/app/fmg.py` | FortiManager: day-0, devices, overlay template, SD-WAN-template, policy package, installatie |
| `controller/app/faz.py` | FortiAnalyzer: day-0, device-autorisatie, FortiAI-controle |
| `controller/app/fortigate.py` | FortiGate-bootstrap (config drive) |
| `controller/app/jsonrpc.py`, `console.py` | JSON-RPC-client; seriële console en SSH-CLI |
| `controller/app/deployer.py` | Proxmox-orchestratie: bridges, VM's, template, ISP, DC-servers, Traffic-Gen |
| `controller/app/scenarios.py` | cockpit: link-impairment, verkeer, hub aan/uit, iperf, live SD-WAN-status via FortiManager |
| `labnode/` | `linkctl` (tc netem), `tgctl` (Traffic-Gen-besturing), `dcserver.py` |
| `docs/` | labgids, FortiAI-use cases, praktijkkennis |

## Status

**Geschreven, maar nog niet uitgerold op een echte Proxmox met FortiOS/FortiManager/FortiAnalyzer 7.6.** Controller, API en UI zijn lokaal
rooktest-gedraaid; de FortiManager-API-payloads zijn gebouwd naar de 7.6-documentatie en Fortinets API-voorbeelden en nog niet tegen een
live FortiManager gecontroleerd. Elke provisioningstap is idempotent en niet-fatale afwijkingen komen als **waarschuwing** in de log en op
de Lab-tab, zodat een veldnaam die in jouw 7.6.x-build anders heet de uitrol niet breekt. Zie
[docs/praktijk.md](docs/praktijk.md) voor de verwachte aandachtspunten en de eerste-uitrol-checklist.
