# Praktijkkennis en eerste-uitrol-checklist

Dit lab is geschreven zonder live toegang tot FortiOS/FortiManager/FortiAnalyzer 7.6. Dit document zegt wat bewust is aangenomen en wat je bij
de eerste uitrol als eerste controleert. Vergelijk het met `docs/praktijk.md` van het FortiADC-lab: na de eerste echte uitrol hoort hier
wat je tegenkwam.

## Ontwerpkeuzes

- **Twee gedeelde WAN-switches** (WAN-A en WAN-B): alle FortiGates hangen op beide, de ISP-router levert gateway, DNS en NAT naar het
  internet. Zo gedraagt het lab zich als een echt internet-segment (ADVPN-shortcuts, IPsec tussen alle sites). Het gedeelde segment betekent
  dat FortiGates elkaar rechtstreeks zien; de ISP-router zit er dus niet tussen.
- **Impairment per FortiGate-NIC op de tap-interface** van de Proxmox host (`sdwlab-linkctl`, tc netem + ifb): vertraging en verlies worden over
  beide richtingen verdeeld (een ping ziet de ingestelde waarde), `rate` geldt per richting. Link-down is een echte link-down in de gast (QEMU
  `set_link`). Na het herstarten van een FortiGate-VM is de impairment weg (nieuwe tap); 'Alles herstellen' ruimt alles op.
- **ISP-router start vóór de FortiGates**: de FortiFlex/FortiCare-licentie-activatie bij de eerste boot heeft DNS en internet nodig.
- **Eigen ADOM `SDWAN-LAB`** (FortiOS 7.6) op FortiManager en FortiAnalyzer; niets staat in `root`. De controller zet de ADOM-modus aan
  (`adom-status enable`, kort herstart van de services) en maakt de ADOM aan.
- **FortiManager is de enige plek van configuratie**: de bootstrap (config drive) zet alleen interfaces, DNS, default routes, logging en de
  central-management-koppeling; FortiManager voegt de devices toe (`dvm/cmd/add/device`) en installeert daarna.
- **FortiManager/FortiAnalyzer**: port1 uplink (internet voor FortiGuard en FortiAI), port2 lab-management (10.255.0.0/24).
  De FortiGates bereiken ze alleen via port2 (geen default route via management).
- **Licenties**: FortiGates via config drive (`.lic` of FortiFlex-token); FortiManager/FortiAnalyzer via FortiFlex-token over SSH
  (`execute vm-license`) of handmatig een `.lic` in de GUI. De FortiAI-licentie is een aparte entitlement in FortiCare.

## Eerste-uitrol-checklist

1. **Images**: FortiGate-VM, FortiManager en FortiAnalyzer KVM-`.qcow2` van dezelfde 7.6.x-lijn. FortiManager/FortiAnalyzer: controleer dat
   de ADOM-versie (SDWAN-LAB) 7.6 is.
2. **Day-0 via console**: controleer in het joblog dat de console-login slaagt (de eerste login kan een verplichte wachtwoordwijziging
   vragen; de code behandelt dat). Mislukt het, voer dan handmatig de getoonde CLI in op `qm terminal <vmid>`.
3. **Devices in FortiManager**: staat een FortiGate op `Unauthorized`/`Down`, controleer dat `port1` (10.255.0.x) bereikbaar is en
   `fgfm` toegestaan is; voeg anders in de GUI toe en draai **Opnieuw provisionen**.
4. **Overlay template (meest versiegevoelig)**: de payload staat in *Lab → Gegenereerde config*. Het veld `topology` (`multi-hub`/`dual-hub`),
   de namen van de nodes en het scope-formaat verschillen per 7.6.x-build; de code probeert varianten en meldt wat mislukt. Bouw bij twijfel de
   template één keer in de GUI en vergelijk met `GET /pm/config/adom/SDWAN-LAB/template/_sdwan_overlay/LAB-OVERLAY/sdwan/overlay`.
5. **SD-WAN-template**: de health-check en rules worden in de door de orchestratie gegenereerde SD-WAN-template gezet (naam gezocht op
   `LAB-OVERLAY`). ISDB-namen (`Microsoft-Teams`, `Zoom-Zoom`, ...) moeten bestaan in jouw ISDB-versie; een onbekende naam geeft een waarschuwing.
6. **Zones en policy package**: de controller leest de zone-namen uit de gegenereerde SD-WAN-template (`overlay`/`underlay`); controleer dat
   de policies in `LAB-PKG` naar bestaande zones verwijzen.
7. **Installatie**: eerst *device settings* (templates), dan het *policy package*. Een mislukte taak staat in FortiManager onder *Task Monitor*.
8. **Logging**: in FortiAnalyzer moeten de vijf devices `Registered` zijn; `diagnose test application oftpd 3` toont de verbindingen.
9. **FortiAI**: gebruik de status-knop; zonder AISN-contract blijft het FortiAI-icoon uit of toont het een aanschafmelding. Controleer
   dat FortiManager/FortiAnalyzer uitgaand HTTPS naar Fortinet kunnen (DNS en gateway op de Setup-pagina).
10. **Traffic-Gen**: de eerste keer *Apply* onder Settings > Network (de controller doet dit bij `tgctl start`). Controleer dat de DNS-server op
    de FortiGate-LAN (port4) antwoordt.

## Bekende beperkingen

- FortiManager/FortiAnalyzer-licentie via `.lic` wordt niet door de controller geüpload (alleen FortiFlex of handmatig).
- De FortiAI-licentie koppel je zelf in FortiCare; de controller kan alleen de status uitlezen.
- De SD-WAN-doorvoer in het lab wordt beperkt door de Proxmox-host (virtuele NIC's, geen offloading) en FortiGate-VM-licentie; het lab
  is bedoeld om gedrag te laten zien, niet voor throughput-metingen.
- De ISP-router NAT't ook de overlay-onderliggende adressen naar internet; de overlay zelf loopt echter ongeNAT't tussen de sites.

## FortiFlex via API

Kies in Setup > Licenties **FortiFlex via API** en vul de API-gebruiker (FortiCloud IAM, met FortiFlex-rechten) en het wachtwoord in.
Het programma-serienummer is optioneel (leeg = eerste programma van de gebruiker).

- **Uitrollen**: de controller zoekt per type de configuratie `<prefix>-fgt/-fmg/-faz` of maakt hem aan (parameters: `fortiflex:` in
  `lab/topology.yaml`), maakt per apparaat één entitlement en zet de token in de FortiGate-config drive of via `execute vm-license`.
- **Opnieuw uitrollen**: een bestaande entitlement wordt gereactiveerd en krijgt een nieuwe token; je hoeft niets handmatig te maken.
- **Verwijderen**: bij het verwijderen van een VM (of het hele lab) stopt de controller de entitlement, zodat er geen punten meer worden
  verbruikt. 'Alle entitlements stoppen' in Setup doet dat ook handmatig.
- **FortiAI**: de FortiManager-configuratie bevat de add-on `FMGAISN` met `fortiAITokens` en FortiAnalyzer `FAZAISN`. Controleer de
  puntenkosten met de FortiFlex-calculator voordat je uitrolt; pas de waarden aan in `topology.yaml` voor de eerste uitrol.
- De API-paden en parameter-ID's komen uit Fortinets Ansible/Terraform-collecties voor FortiFlex v2 en zijn niet tegen jouw account
  getest; fouten staan in de joblog. Het API-wachtwoord staat (zoals het admin-wachtwoord) in `data/state.json` van de controller.
