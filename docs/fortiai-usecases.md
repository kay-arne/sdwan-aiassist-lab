# FortiAI-Assist: use cases voor deze demo

FortiAI-Assist is de AI-assistent in **FortiAnalyzer** en **FortiManager** (7.6.x en nieuwer). Hij werkt via de cloud van Fortinet, heeft een
FortiAI-licentie met maandelijks tokenbudget nodig en is alleen beschikbaar voor lokale administrators. Onderstaand overzicht groepeert de
use cases die dit lab laat zien; de prompts staan per scenario in de Cockpit (kopieerknop) en op het FortiAI-tabblad.

> De getoonde antwoorden zijn niet vooraf vastgelegd: FortiAI antwoordt op de data die het lab genereert. De kolom "verwacht" is de
> bedoelde uitkomst; controleer altijd in de FortiAnalyzer-GUI of het antwoord klopt (dat is zelf een goed demo-punt).

## Platform: FortiAnalyzer

| Use case | Scenario | Voorbeeldprompt | Wat je aantoont |
|---|---|---|---|
| Natuurlijke-taal vragen over verkeer | 1 Basislijn | "Which applications used the most bandwidth in the last hour, split per branch?" | geen filtersyntax, dezelfde data als FortiView |
| SD-WAN-gedrag verklaren | 2 Brownout | "Why did the Teams traffic from FGT-BR1 change transport?" | koppeling SLA-overschrijding ↔ rule ↔ transportwissel |
| Incident reconstrueren | 3 Blackout, 4 Hub-uitval | "Summarize the events on FGT-BR2 in the last 15 minutes and explain the root cause." | tijdlijn van tunnel-down tot herstel |
| Impact-analyse | 2, 4 | "Was there a service impact for users? Compare sessions before and after." | sessies verplaatst, niet verloren |
| Bandbreedte-analyse | 5, 7 | "What are the top 5 bandwidth consumers at FGT-BR3 and which rule matched them?" | business-intent in de praktijk |
| Security-samenvatting en uitleg | 6 Incident | "Explain the IPS and antivirus events for source 10.12.0.50 and recommend next steps." | uitleg in gewone taal + vervolgstappen |
| Detectie-effectiviteit | 6 | "Were any threats allowed through, or all blocked?" | blocked/passed, SSL-inspectie als oorzaak van 'missed' |
| Rapportage | 9 | "Give me an SD-WAN SLA summary per branch for the last 24 hours." | management-overzicht |

## Platform: FortiManager

| Use case | Scenario | Voorbeeldprompt | Wat je aantoont |
|---|---|---|---|
| Configuratie uitleggen | 8 Config | "Explain the SD-WAN overlay template LAB-OVERLAY..." | onboarding, kennisoverdracht |
| Script genereren | 5, 8 | "Write a CLI script that adds an SD-WAN service rule for Zoom..." | sneller werken; je reviewt en voert uit via *Scripts* |
| Fout verklaren | 8 | "The script LAB-BAD-SCRIPT failed on FGT-BR1. What is wrong?" | troubleshooting van installatiefouten |
| Wijzigingshistorie | 8 | "What changed on FGT-BR3 in the last 24 hours?" | audit/change-review |
| Status van het beheerde park | 4 | "Is FGT-HUB-AMS still connected? What is its configuration status?" | operationeel inzicht |

## Praktische tips voor de demo

- Geef FortiAI **tijd en data**: laat het verkeer minstens 5-10 minuten draaien voordat je vragen stelt over "het afgelopen uur".
- Stel vragen met **device-namen** zoals ze in FortiManager/FortiAnalyzer staan (`FGT-BR1`, `FGT-HUB-AMS`).
- Verwijs naar **tijdsvensters** ("last 30 minutes"): de scenario's zijn kort.
- Controleer het antwoord in de normale GUI; laat zien dat FortiAI de weg naar het antwoord verkort, niet de data vervangt.
- Het tokenbudget is gedeeld: gebruik de prompts uit de lijst in plaats van open exploratie als je het budget wilt sparen.
- Het FortiAI-venster zit in de banner van de GUI (FortiAnalyzer en FortiManager). Beschikbaarheid en naamgeving van functies verschilt per 7.6.x-build.

## Eigen use cases toevoegen

Voeg onder `fortiai:` in `lab/scenarios.yaml` een regel `{where, prompt, expect}` toe aan een scenario; de Cockpit en het FortiAI-tabblad pakken
hem automatisch op. Een nieuw scenario is een `id`, `title`, `story`, `setup`-acties en `fortiai`-vragen.
