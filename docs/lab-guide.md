# Labgids: een week bij Polder Logistiek

Polder Logistiek heeft twee datacenters (Amsterdam en Frankfurt) en drie vestigingen (Utrecht, Rotterdam, Eindhoven). Elke vestiging
heeft twee internetlijnen: een glasvezellijn (**WAN-A**) en een breedbandlijn (**WAN-B**). Alles loopt via FortiGates die
door één FortiManager worden beheerd. Deze gids loopt het verhaal door; elk hoofdstuk is een scenario in het tabblad **Cockpit**.

## 0. Voorbereiding

- Open **FortiManager**, **FortiAnalyzer** (en eventueel een FortiGate) in aparte tabs; log in als `admin` (Toegang-tab).
- Controleer in FortiManager onder *Device Manager* dat de vijf FortiGates `Connected` en `Synchronized` zijn, en in FortiAnalyzer onder
  *Device Manager* dat ze logs sturen.
- Open het **FortiAI**-venster (icoon in de banner) in FortiManager én FortiAnalyzer en controleer de FortiAI-status op het FortiAI-tabblad
  van de controller.
- Start **Alles herstellen** in de Cockpit. Laat het lab 5 minuten draaien met **Verkeer starten** (alle branches, preset `normal`),
  zodat FortiAnalyzer iets heeft om te tonen.

## 1. Hoe is dit gebouwd? (FortiManager)

Toon in FortiManager:

1. *Device Manager → Provisioning Templates → SD-WAN Overlay Templates* (in 7.6: *SD-WAN Manager*): template `LAB-OVERLAY`
   met twee hubs en de spokes-groep. Eén definitie, vijf FortiGates.
2. De gegenereerde **IPsec-, BGP-, SD-WAN- en CLI-templates** en de device-groep `SPOKES`.
3. De **SD-WAN-template**: zones, health-check `DC_SLA` (SLA: 80 ms latency, 20 ms jitter, 2% verlies) en de vier rules
   (VOICE-VIDEO, BUSINESS-SAAS, STREAMING, DC-APPS).
4. Het **policy package** `LAB-PKG` met UTM-profielen en volledige logging.

Boodschap: *intent in templates; de FortiGates volgen*. Een nieuwe branch is een device toevoegen aan de groep en installeren.

## 2. Zichtbaarheid: scenario "Basislijn"

Start het scenario. In FortiAnalyzer: *FortiView → SD-WAN* en *Applications*. Stel dan de FortiAI-vragen: de gebruiker hoeft geen filter of dashboard te kennen.

## 3. Brownout op Utrecht, blackout in Rotterdam

- **Brownout** (150 ms, 40 ms jitter, 6% verlies op A): SD-WAN verplaatst Teams naar B, DC-verkeer blijft binnen de SLA.
  Laat live de **SD-WAN-health** zien in de Cockpit (health-check per member) en daarna de FortiAI-vragen.
- **Blackout** (link down): tunnels over A vallen weg, BGP herconvergeert, ADVPN-shortcuts. Meet hoe snel verkeer herstelt.

## 4. Hub-uitval

Zet **Hub Amsterdam** uit. Spokes sturen via Frankfurt. In FortiManager zie je de hub `Connection Down`; in FortiAnalyzer is zijn log-stroom stil.

## 5. Streaming versus business

Eindhoven start streaming. De rule STREAMING houdt dat op WAN-B; business-apps blijven op WAN-A. Toon dit per applicatie en per transport.

## 6. Beveiligingsincident (Rotterdam)

De Traffic-Gen-preset `security` laat harmloze testmalware (EICAR/WICAR) en botnet-achtig DNS-gedrag los. FortiAnalyzer toont AV/IPS/DNS-events;
laat FortiAI het incident samenvatten en vervolgstappen adviseren. Let op: HTTPS-malware wordt alleen gedetecteerd met SSL deep inspection;
zonder inspectie telt Traffic-Gen dat als *missed*, een mooi gespreksonderwerp.

## 7. Doorvoer en bandbreedte-hogs, 8. Configuratie-assistent, 9. Rapportage

Zie de scenario's in de Cockpit. In scenario 8 maakt de cockpit een bewust **kapot CLI-script** in FortiManager; FortiAI-Assist legt de fout uit
en schrijft een correct SD-WAN-script voor Zoom.

## Opruimen

Na elk scenario: **Opruimen** of **Alles herstellen** (links schoon en up, hubs aan, verkeer stop).

## Traffic-Gen zelf gebruiken

Elke branch-container heeft de GUI van Traffic-Gen (adres op de Toegang-tab). Daar stel je het verkeersmengsel, de intensiteit en de
doorvoer-/CPS-tests in. De controller zet de bronnetwerken vooraf (zes IP's per branch, gateway = de FortiGate-LAN) en bedient hem via `tgctl`.
