# FortiAI-Assist: use cases voor deze demo

Gebaseerd op *Using FortiAI on FortiAnalyzer 7.6.0* en *Using FortiAI on FortiManager 7.6.0*. FortiAI heeft een licentie met maandelijks
tokenbudget nodig (gedeeld door alle gebruikers), internet vanaf het beheer-poort en een lokale administrator.

> De antwoorden zijn niet vooraf vastgelegd: FortiAI antwoordt op de data die het lab genereert. De kolom "verwacht" in de Cockpit is de
> bedoelde uitkomst; controleer in de normale GUI of het klopt.

## Wat FortiAI wel en niet kan

**FortiAnalyzer**: loganalyse en filteren (prompts worden vertaald naar SQL), reputatiecheck van IP's (FortiGuard, VirusTotal), taart- en
staafdiagrammen, event handlers aanmaken, incidenten opvragen/bijwerken/rapporteren (ook compromised hosts), proces-info van endpoints en
endpoints quarantaineren via IP (die laatste twee vereisen een endpoint-/connectorkoppeling die dit lab niet heeft).

**FortiManager**: *Script Assistant* (CLI- en Jinja-scripts, opslaan als script of CLI-template), *VPN Assistant* (VPN-scripts op basis van een
topologie-afbeelding of geselecteerde apparaten; tunnelstatus controleren en diagnosticeren) en *IoT Device Analysis* (alleen relevant met
IoT-detectie, niet in dit lab).

Niet: voorspellingen ("hoeveel aanvallen krijg ik morgen"), te vage vragen ("PCI-rapport voor mijn netwerk") en vrije uitleg van een
FortiManager-template of wijzigingshistorie; die horen bij de normale GUI. De scenario's zijn hierop afgestemd.

## Use cases per platform

| Platform | Use case | Scenario | Voorbeeldprompt |
|---|---|---|---|
| FortiAnalyzer | Loganalyse en filteren | 1, 2, 3, 5, 7 | "Show top applications by bytes, past hour." |
| FortiAnalyzer | Grafieken (taart/staaf) | 1, 4, 5, 6, 7, 9 | "Generate a pie chart of traffic per outgoing interface, past hour." |
| FortiAnalyzer | Event handler aanmaken | 3, 6 | "Create an event handler for IPsec tunnel down on FGT-BR2." |
| FortiAnalyzer | Reputatiecheck | 6 | "Perform a security reputation check for <IP>." |
| FortiAnalyzer | Incidenten en rapport | 6 | "List incidents created in the past 24 hours." |
| FortiAnalyzer | Quarantaine | 6 | "Quarantine 10.12.0.50." (alleen met koppeling) |
| FortiManager | Script Assistant | 5, 8 | "Create firewall addresses for 10.2.2.1 and awesome-service.com." |
| FortiManager | VPN Assistant: script | 8 | "Help me configure site-to-site VPN." |
| FortiManager | VPN Assistant: status/diagnose | 2, 3, 4, 8 | "Check the VPN tunnel status." / "Diagnose the VPN tunnel." |

## Tips voor de demo

- **Beknopte prompts**: meer tekst = meer tokens. Geef tijdvensters en limieten mee ("past hour", "top 5").
- Gebruik **woorden uit de GUI** ("generate report", "create an event handler"), en verwijs naar bestaande event handlers, charts en reports.
- Een thread overleeft geen reload of logout, en een lange thread kost meer tokens: **Restart Thread** na ongeveer 10 vragen.
- Het tokenpercentage staat onderin het venster; hover voor sessie-, maand- en totaalverbruik.
- Onder elk antwoord toont het help-icoon de achterliggende functie-aanroep.
- Laat verkeer 5-10 minuten draaien voor je naar "het afgelopen uur" vraagt.
- In FortiManager kies je eerst de assistent (Script, VPN, IoT) in het venster.

## Eigen use cases toevoegen

Voeg onder `fortiai:` in `lab/scenarios.yaml` een regel `{where, prompt, expect}` toe; de Cockpit en het FortiAI-tabblad pakken hem op.
