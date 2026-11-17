# Praktijkkennis en eerste-uitrol-checklist

Dit lab is geschreven zonder live toegang tot FortiOS/FortiManager/FortiAnalyzer 7.6. Dit document zegt wat bewust is aangenomen en wat je bij
de eerste uitrol als eerste controleert. Vergelijk het met `docs/praktijk.md` van het FortiADC-lab: na de eerste echte uitrol hoort hier
wat je tegenkwam.

## Ontwerpkeuzes

- **Per site en transport een eigen toegangslink** naar de ISP-router (10 bridges). Anders praten FortiGates op één gedeelde bridge rechtstreeks
  met elkaar en kun je geen verslechtering per link instellen.
- **Impairment alleen in de richting ISP → site** (tc netem op de egress van de ISP-interface): een ping vanaf de site ziet vertraging/verlies
  één keer. Voor symmetrische vertraging stel je ook de andere kant in.
- **FortiManager is de enige plek van configuratie**: de bootstrap (config drive) zet alleen interfaces, DNS, default routes, logging en de
  central-management-koppeling; FortiManager voegt de devices toe (`dvm/cmd/add/device`) en installeert daarna.
- **FortiManager/FortiAnalyzer**: port1 uplink (internet voor FortiGuard en FortiAI), port2 lab-management (10.255.0.0/24).
  De FortiGates bereiken ze alleen via port2 (geen default route via management).
- **Licenties**: FortiGates via config drive (`.lic` of FortiFlex-token); FortiManager/FortiAnalyzer via FortiFlex-token over SSH
  (`execute vm-license`) of handmatig een `.lic` in de GUI. De FortiAI-licentie is een aparte entitlement in FortiCare.

## Eerste-uitrol-checklist

1. **Images**: FortiGate-VM, FortiManager en FortiAnalyzer KVM-`.qcow2` van dezelfde 7.6.x-lijn. FortiManager/FortiAnalyzer: controleer dat
   de ADOM-versie (root) 7.6 is.
2. **Day-0 via console**: controleer in het joblog dat de console-login slaagt (de eerste login kan een verplichte wachtwoordwijziging
   vragen; de code behandelt dat). Mislukt het, voer dan handmatig de getoonde CLI in op `qm terminal <vmid>`.
3. **Devices in FortiManager**: staat een FortiGate op `Unauthorized`/`Down`, controleer dat `port1` (10.255.0.x) bereikbaar is en
   `fgfm` toegestaan is; voeg anders in de GUI toe en draai **Opnieuw provisionen**.
4. **Overlay template (meest versiegevoelig)**: de payload staat in *Lab → Gegenereerde config*. Het veld `topology` (`multi-hub`/`dual-hub`),
   de namen van de nodes en het scope-formaat verschillen per 7.6.x-build; de code probeert varianten en meldt wat mislukt. Bouw bij twijfel de
   template één keer in de GUI en vergelijk met `GET /pm/config/adom/root/template/_sdwan_overlay/LAB-OVERLAY/sdwan/overlay`.
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
