"""National lobby registers: Germany (Bundestag Lobbyregister) and France (HATVP).

Run: python3 -m influence.sources.national_lobby

Sources (bulk open data, cached under data/influence/raw/national_lobby/):
  DE  https://www.lobbyregister.bundestag.de/sucheDetailJson?sort=REGISTRATION_DESC
      Full register (current version of every entry, active and inactive), schema
      documented at /informationen-und-hilfe/open-data-1049716. Older versions of
      matched entries are fetched one by one from /sucheJson/{R-number}/{entryId}
      (cached in de_versions/) to recover the previous fiscal year(s). Only versions
      under the 2024 law (GL2024, in force since March 2024) are published as JSON, so
      DE history is short: typically the last one or two completed fiscal years.
  FR  https://www.hatvp.fr/agora/opendata/agora_repertoire_opendata.json
      Répertoire des représentants d'intérêts: every registrant with its yearly
      "exercices" (expense bracket, staff, declared activity sheets). The file carries
      exercices ending 2019 onwards.

Outputs
  data/influence/clean/national_lobby_matches.csv
      company_id, country, register_id, register_name, match_method, note
      One row per register entry attributed to a company. Matching is by explicit,
      hand-verified register ids below (DE register number / FR SIREN or HATVP id);
      the expected name is checked against the register at run time. No fuzzy matching.
      Trade associations, foundations, JVs not controlled by the company, law/PR firms
      and spun-off companies are excluded; group subsidiaries are included. Where a
      subsidiary changed hands, the alias carries a year window (see note).

  data/influence/clean/national_lobby.csv  (one row per company_id x country x year)
      year                  DE: calendar year in which the reported fiscal year ENDS
                            (fiscal_year_end gives the exact date(s)); FR: year of the
                            exercice end date (dateFin), mostly calendar years.
      lobby_cost_low_eur    Sum over the company's matched registrations of the lower
      lobby_cost_high_eur   / upper bound of the declared lobbying-expense bracket.
      lobby_cost_mid_eur    Sum of bracket midpoints.
                            DE "financialExpensesEuro": EUR 10,000-wide bands (e.g.
                            1,280,001-1,290,000) of expenses for interest representation
                            towards Bundestag/federal government in the fiscal year.
                            FR "montantDepense": brackets of EUR 10k-250k width (e.g.
                            ">= 100 000 et < 200 000"), lobbying costs in France for the
                            exercice. Open-ended top bracket (">= 10 000 000") has no
                            high; its low is used as mid and high is left blank for
                            that row (cost_open_ended=1).
      lobbyists_fte_or_persons  DE: employeeFTE, full-time equivalents of employees
                            doing lobbying (summed). FR: nombreSalaries, number of
                            PERSONS (headcount, not FTE) carrying out lobbying (summed).
      n_registrations       Matched registrations with a published figure that year.
      n_legislative_projects  DE: regulatoryProjectsCount (Regelungsvorhaben listed in the
                            entry version that carries that fiscal year's figures; it is
                            a snapshot of the entry, not strictly tied to the year).
                            FR: number of activity sheets ("fiches d'activité" - one per
                            lobbying objective, each naming the public decisions targeted)
                            declared for the exercice.
      n_actions             FR only: number of action blocks (responsables publics x
                            decisions x actions) across the activity sheets. Blank for DE.
      n_statements          DE only: Stellungnahmen/Gutachten (position papers) attached
                            to the entry version. Blank for FR.
      cost_open_ended       1 if any summed FR bracket was the open top bracket.
      fiscal_year_end       DE/FR period end date(s) summed into the row (';'-joined).
      register_ids          Register ids summed into the row (';'-joined).
"""

import json
import time
from collections import defaultdict
from datetime import datetime

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

DE_URL = "https://www.lobbyregister.bundestag.de/sucheDetailJson?sort=REGISTRATION_DESC"
DE_VERSION_URL = "https://www.lobbyregister.bundestag.de/sucheJson/{reg}/{eid}"
FR_URL = "https://www.hatvp.fr/agora/opendata/agora_repertoire_opendata.json"
OUT = RAW / "national_lobby"

# ---------------------------------------------------------------------------
# Explicit aliases: company_id -> [(register_id, expected register name, note,
#                                   first_year or None, last_year or None)]
# Every id was looked up in the register files (Oct 2026 snapshot) by name.
# ---------------------------------------------------------------------------
P = "parent/group entity"
S = "subsidiary"

DE = {
    "alphabet": [("R001794", "Google Germany GmbH", S, None, None),
                 ("R004551", "Waymo LLC", S + " (Alphabet 'Other Bets')", None, None)],
    "meta": [("R002228", "Facebook Germany GmbH", S, None, None)],
    "microsoft": [("R002153", "Microsoft Deutschland GmbH", S, None, None),
                  ("R002353", "LinkedIn Germany GmbH", S + " (LinkedIn)", None, None),
                  ("R006681", "GitHub Germany GmbH", S + " (GitHub)", None, None)],
    "apple": [("R002359", "Apple Inc.", P, None, None)],
    "amazon": [("R002160", "Amazon Deutschland Services GmbH", S, None, None),
               ("R002135", "Amazon Web Services EMEA SARL", S + " (AWS)", None, None)],
    "oracle": [("R001882", "ORACLE Deutschland B.V. & Co. KG", S, None, None)],
    "ibm": [("R001842", "IBM Deutschland GmbH", S, None, None)],
    "intel": [("R006400", "Intel Deutschland GmbH", S, None, None),
              ("R002405", "Intel Germany GmbH & Co. KG", S, None, None),
              ("R002368", "Intel Germany Services GmbH", S, None, None),
              ("R006401", "Intel Magdeburg GmbH", S + " (Magdeburg fab project)", None, None)],
    "qualcomm": [("R002043", "Qualcomm Germany GmbH", S, None, None)],
    "nvidia": [("R001253", "NVIDIA GmbH", S, None, None)],
    "cisco": [("R002136", "Cisco Systems GmbH", S, None, None)],
    "uber": [("R001364", "Uber Germany GmbH", S, None, None),
             ("R001363", "Uber B.V.", S + " (EU HQ)", None, None),
             ("R001369", "Uber Switzerland GmbH", S, None, None)],
    "airbnb": [("R003522", "Airbnb Germany GmbH", S, None, None)],
    "pfizer": [("R001142", "Pfizer Pharma GmbH", S, None, None),
               ("R001183", "Pfizer Manufacturing Deutschland GmbH", S, None, None)],
    "jnj": [("R001109", "Janssen-Cilag GmbH", S + " (pharma; Kenvue Germany excluded - spun off 2023)", None, None),
            ("R001217", "Johnson & Johnson Medical GmbH", S + " (medtech)", None, None)],
    "merck": [("R001066", "MSD Sharp & Dohme GmbH", S + " (MSD = Merck & Co. outside US/CA; Merck KGaA entities excluded)", None, None)],
    "abbvie": [("R000796", "AbbVie Deutschland GmbH & Co. KG", S, None, None)],
    "lilly": [("R000994", "Lilly Deutschland GmbH", S, None, None)],
    "bms": [("R000931", "Bristol-Myers Squibb GmbH & Co. KGaA", S + " (BMS-Stiftung Immunonkologie excluded - foundation)", None, None)],
    "amgen": [("R001236", "Amgen GmbH", S, None, None)],
    "gilead": [("R001158", "Gilead Sciences GmbH", S, None, None)],
    "exxon": [("R001740", "ExxonMobil Central Europe Holding", S, None, None)],
    "conocophillips": [("R007096", "ConocoPhillips", P, None, None)],
    "goldman": [("R007490", "Goldman Sachs Bank Europe SE", S, None, None),
                ("R002851", "Goldman Sachs International", S, None, None)],
    "morganstanley": [("R002946", "Morgan Stanley Europe SE", S, None, None)],
    "bofa": [("R003854", "Bank of America Europe DAC", S, None, None)],
    "citi": [("R001922", "Citigroup Global Markets Europe AG", S, None, None)],
    "blackrock": [("R003422", "BlackRock Asset Management Deutschland AG", S, None, None)],
    "visa": [("R002282", "Visa Europe Management Services Limited, German Branch", S, None, None)],
    "mastercard": [("R002253", "Mastercard", S + " (Mastercard Europe, German branch)", None, None)],
    "boeing": [("R004507", "Boeing Deutschland GmbH", S, None, None)],
    "lockheed": [("R002245", "Lockheed Martin Overseas Services Corporation", S, None, None)],
    "rtx": [("R006753", "RTX Corporation", P, None, None),
            ("R002973", "Raytheon Company", S + " (Raytheon)", None, None),
            ("R002720", "Raytheon Deutschland GmbH", S + " (Raytheon)", None, None),
            ("R003111", "Goodrich Aerospace Europe GmbH", S + " (Collins Aerospace; register classes it as consulting)", None, None)],
    "generaldynamics": [("R001506", "General Dynamics European Land Systems - Deutschland GmbH", S + " (GDELS)", None, None),
                        ("R001504", "General Dynamics European Land Systems - FWW GmbH", S + " (GDELS)", None, None),
                        ("R001502", "General Dynamics European Land Systems-Bridge Systems GmbH", S + " (GDELS)", None, None),
                        ("R001508", "General Dynamics European Land Systems - Mowag GmbH", S + " (GDELS, Swiss)", None, None)],
    "northrop": [("R000975", "Northrop Grumman LITEF GmbH", S, None, None),
                 ("R007593", "Northrop Grumman International Holdings B.V.", S, None, None)],
    "honeywell": [("R004581", "Honeywell International Inc.", P, None, None)],
    "3m": [("R000831", "3M Deutschland GmbH", S, None, None)],
    "ford": [("R001871", "Ford-Werke GmbH", S, None, None)],
    "tesla": [("R003170", "Tesla Germany GmbH", S, None, None),
              ("R007554", "Tesla Manufacturing Brandenburg SE", S + " (Gigafactory Berlin)", None, None)],
    "cocacola": [("R001689", "Coca-Cola GmbH", S + " (Coca-Cola Europacific Partners excluded - independent bottler)", None, None)],
    "pepsico": [("R000073", "PepsiCo Deutschland GmbH", S, None, None)],
    "pg": [("R001773", "Procter & Gamble Service GmbH", S, None, None)],
    "pmi": [("R000144", "Philip Morris GmbH", S, None, None)],
    "mcdonalds": [("R001671", "McDonald’s Deutschland LLC, Zweigniederlassung München", S, None, None),
                  ("R001675", "McDonald’s Promotions GmbH & Co. KG Werbegesellschaft Deutschland", S + " (advertising arm)", None, None)],
    "att": [("R004272", "AT&T Global Network Services Belgium Luxembourg - Besloten Vennootschap (BV)", S, None, None)],
    "comcast": [("R000145", "Sky Deutschland", S + " (Sky, owned by Comcast since 2018)", None, None)],
    "dow": [("R001060", "Dow Deutschland Anlagengesellschaft mbH", S, None, None),
            ("R001059", "Dow Olefinverbund GmbH", S, None, None)],
    "dupont": [("R001856", "DuPont Deutschland Holding Gmbh & Co. KG", S, None, None)],
    "corteva": [("R001781", "Corteva Agriscience Germany GmbH", S, None, None)],
    "siemens": [("R001875", "Siemens AG", P + " (Siemens Energy excluded - spun off 2020, minority stake)", None, None),
                ("R002236", "Siemens Healthineers AG", S + " (majority-owned, ~70%)", None, None),
                ("R002229", "Siemens Healthcare Diagnostics Products GmbH", S + " (Healthineers)", None, None),
                ("R001892", "Siemens Mobility GmbH", S, None, None)],
    "basf": [("R002326", "BASF SE", P, None, None),
             ("R004839", "BASF Schwarzheide GmbH", S, None, None),
             ("R004625", "BASF Polyurethanes GmbH", S, None, None),
             ("R005584", "BASF Grenzach GmbH", S, None, None),
             ("R006597", "BASF Personal Care and Nutrition GmbH", S, None, None),
             ("R007600", "BASF InfraService & Solutions Lausitz GmbH", S, None, None),
             ("R004342", "BASF Lampertheim GmbH", S + " (register activity code says employer assoc.; it is a BASF plant company)", None, None)],
    "bayer": [("R002249", "Bayer AG", P, None, None),
              ("R002256", "Bayer Vital GmbH", S, None, None),
              ("R002257", "Bayer CropScience Deutschland GmbH", S, None, None)],
    "volkswagen": [("R001681", "Volkswagen AG", P + " (Porsche Automobil Holding SE excluded - shareholder, not subsidiary)", None, None),
                   ("R001702", "AUDI AG", S, None, None),
                   ("R001768", "Dr. Ing. h.c. F. Porsche AG", S + " (VW majority)", None, None),
                   ("R001704", "Volkswagen Financial Services AG", S, None, None),
                   ("R001890", "Volkswagen Group Charging GmbH", S + " (Elli)", None, None),
                   ("R006271", "CARIAD SE", S, None, None),
                   ("R006923", "PowerCo SE", S, None, None),
                   ("R001565", "TRATON SE", S + " (VW majority)", None, None),
                   ("R001638", "MAN Truck & Bus SE", S + " (TRATON)", None, None),
                   ("R001653", "Everllence SE", S + " (formerly MAN Energy Solutions)", None, None)],
    "bmw": [("R002370", "Bayerische Motoren Werke Aktiengesellschaft", P + " (BMW Foundation excluded)", None, None)],
    "mercedes": [("R002034", "Mercedes-Benz Group AG", P + " (Daimler Truck excluded - spun off 2021)", None, None)],
    "dtelekom": [("R002346", "Deutsche Telekom AG", P + " (Deutsche Telekom Stiftung excluded)", None, None)],
    "sap": [("R002180", "SAP SE", P, None, None)],
    "allianz": [("R001562", "Allianz SE", P, None, None),
                ("R001716", "Allianz Deutschland AG", S, None, None),
                ("R001717", "Allianz Versicherungs-AG", S, None, None),
                ("R001718", "Allianz Lebensversicherungs-AG", S, None, None),
                ("R001721", "Allianz Private Krankenversicherungs-AG", S, None, None),
                ("R007259", "Allianz Global Investors GmbH", S, None, None),
                ("R004159", "Allianz Capital Partners", S, None, None),
                ("R001724", "Allianz Agrar AG", S, None, None),
                ("R001728", "Allianz Kunde und Markt GmbH", S, None, None),
                ("R001723", "Allianz Beratungs- und Vertriebs-AG", S, None, None)],
    "deutschebank": [("R001998", "Deutsche Bank AG", P, None, None),
                     ("R001422", "DWS Group GmbH & Co. KGaA", S + " (DWS, ~80% Deutsche Bank)", None, None),
                     ("R003397", "DWS Alternatives Global Ltd", S + " (DWS)", None, None)],
    "airbus": [("R001179", "Airbus SE", P, None, None),
               ("R002323", "Airbus Defence and Space GmbH", S, None, None),
               ("R001204", "Airbus Operations GmbH", S, None, None),
               ("R002453", "Airbus Helicopters Deutschland GmbH", S, None, None),
               ("R005394", "Airbus Aerostructures GmbH", S, None, None),
               ("R002844", "Airbus GmbH", S, None, None),
               ("R002306", "Airbus DS Airborne Solutions GmbH", S, None, None)],
    "totalenergies": [("R002243", "TotalEnergies Marketing Deutschland GmbH", S, None, None),
                      ("R003553", "TotalEnergies Raffinerie Mitteldeutschland GmbH", S, None, None),
                      ("R006798", "TotalEnergies Renewables Deutschland GmbH", S, None, None)],
    "sanofi": [("R001233", "Sanofi-Aventis Deutschland GmbH", S, None, None)],
    "loreal": [("R002663", "L'Oreal Deutschland GmbH", S, None, None)],
    "bnpparibas": [("R001771", "BNP Paribas S.A. Niederlassung Deutschland", S + " (branch)", None, None)],
    "danone": [("R001099", "Danone Deutschland GmbH", S, None, None)],
    "safran": [("R002366", "Safran GmbH", S + " (ArianeGroup excluded - 50/50 JV)", None, None)],
    "thales": [("R000754", "Thales Deutschland GmbH", S, None, None),
               ("R001438", "Thales Alenia Space Deutschland GmbH", S + " (Thales Alenia Space, 67% Thales)", None, None),
               ("R008250", "Thales Secure Cloud Deutschland GmbH", S, None, None)],
    "stellantis": [("R002372", "Stellantis N.V.", P, None, None)],
    "shell": [("R001348", "Shell Deutschland GmbH", S, None, None),
              ("R001354", "Deutsche Shell Holding GmbH", S, None, None),
              ("R002274", "sonnen GmbH", S + " (sonnen, Shell-owned since 2019)", None, None),
              ("R006112", "sonnen eServices GmbH", S + " (sonnen)", None, None),
              ("R006113", "sonnen eServices Deutschland GmbH", S + " (sonnen)", None, None),
              ("R006709", "Next Kraftwerke", S + " (Shell-owned since 2021)", None, None)],
    "ing": [("R002164", "ING-DiBa AG", S, None, None)],
    "asml": [("R005213", "ASML Berlin AG", S, None, None)],
    "philips": [("R000803", "Philips GmbH", S, None, None)],
    "unilever": [("R003910", "Unilever Deutschland GmbH", S, None, None)],
    "prosus": [("R007693", "Prosus N.V.", P + " (Delivery Hero excluded - minority stake)", None, None),
               ("R005225", "Lieferando (yd. yourdelivery GmbH)", S + " (Just Eat Takeaway, acquired by Prosus 2025)", 2025, None)],
    "bp": [("R001753", "BP Europa SE", S + " (Aral; JERA Nex bp excluded - 50/50 JV)", None, None)],
    "astrazeneca": [("R002385", "AstraZeneca GmbH", S, None, None),
                    ("R003051", "Alexion Pharma Germany GmbH", S + " (Alexion, acquired 2021)", None, None)],
    "gsk": [("R001541", "GlaxoSmithKline GmbH & Co. KG", S, None, None)],
    "hsbc": [("R007014", "HSBC Continental Europe S.A., Germany", S + " (branch)", None, None)],
    "barclays": [("R006306", "Barclays PLC", P, None, None),
                 ("R006226", "Barclays Bank Ireland PLC", S, None, None)],
    "bat": [("R000873", "British American Tobacco (Industrie) GmbH", S + " (Reemtsma excluded - Imperial Brands)", None, None)],
    "riotinto": [("R004121", "Rio Tinto", P + " (registered via Brussels office)", None, None)],
    "vodafone": [("R001732", "Vodafone GmbH", S + " (Vodafone Stiftung and Vodafone Institut excluded)", None, None)],
    "nestle": [("R002156", "Nestlé Deutschland AG", S, None, None)],
    "novartis": [("R001296", "Novartis Pharma GmbH", S + " (Sandoz excluded - spun off 2023)", None, None)],
    "roche": [("R002169", "Roche Pharma AG", S, None, None),
              ("R002588", "Roche Diagnostics GmbH", S, None, None),
              ("R002587", "Roche Diagnostics Deutschland GmbH", S, None, None),
              ("R000461", "Roche Diabetes Care Deutschland GmbH", S, None, None),
              ("R000460", "Roche Diabetes Care GmbH", S, None, None)],
    "ubs": [("R002335", "UBS Europe SE", S, None, None)],
    "equinor": [("R002292", "Equinor ASA", P, None, None),
                ("R002402", "Equinor Deutschland GmbH", S, None, None)],
    "novonordisk": [("R002416", "Novo Nordisk Pharma GmbH", S, None, None)],
    "ericsson": [("R001102", "Ericsson GmbH", S, None, None)],
    "nokia": [("R002994", "Nokia Solutions and Networks GmbH & Co.KG", S, None, None)],
    "iberdrola": [("R003793", "Iberdrola Deutschland GmbH", S, None, None)],
    "spotify": [("R003966", "Spotify AB", S + " (operating parent)", None, None),
                ("R003968", "Spotify Belgium NV", S + " (Brussels office)", None, None)],
}

FR = {
    "alphabet": [("443061841", "GOOGLE FRANCE", S, None, None)],
    "meta": [("530085802", "FACEBOOK FRANCE", S, None, None)],
    "microsoft": [("327733184", "MICROSOFT FRANCE", S, None, None),
                  ("519059281", "LINKEDIN FRANCE SAS", S + " (LinkedIn)", None, None)],
    "apple": [("322120916", "APPLE FRANCE", S, None, None)],
    "amazon": [("809869043", "AMAZON FRANCE SERVICES SAS", S, None, None),
               ("831001334", "AMAZON WEB SERVICES EMEA SARL", S + " (AWS)", None, None),
               ("H803953217", "Audible GmbH", S + " (Audible)", None, None)],
    "oracle": [("335092318", "ORACLE FRANCE", S, None, None)],
    "ibm": [("552118465", "CIE IBM FRANCE", S, None, None)],
    "intel": [("302456199", "INTEL CORPORATION SAS", S, None, None)],
    "qualcomm": [("795039528", "QUALCOMM FRANCE", S, None, None),
                 ("799760087", "QUALCOMM COMMUNICATIONS", S, None, None)],
    "nvidia": [("432108355", "NVIDIA LTD", S, None, None)],
    "cisco": [("349166561", "CISCO SYSTEMS FRANCE", S, None, None)],
    "uber": [("539454942", "UBER FRANCE SAS", S, None, None)],
    "airbnb": [("750885410", "AIRBNB FRANCE", S, None, None)],
    "pfizer": [("433623550", "PFIZER", S + " (Pfizer SAS, France)", None, None)],
    "jnj": [("562033068", "JANSSEN CILAG", S, None, None),
            ("612030619", "JOHNSON & JOHNSON MEDICAL SAS", S, None, None)],
    "merck": [("417890589", "MSD FRANCE", S, None, None),
              ("392032934", "MSD VACCINS", S, None, None),
              ("331377960", "INTERVET", S + " (MSD Santé Animale; Merck Santé excluded - Merck KGaA)", None, None)],
    "abbvie": [("750775660", "ABBVIE", S, None, None)],
    "lilly": [("609849153", "LILLY FRANCE", S, None, None)],
    "bms": [("562011742", "BRISTOL MYERS SQUIBB", S + " (UPSA excluded - sold 2019)", None, None)],
    "amgen": [("377998679", "AMGEN SAS", S, None, None)],
    "gilead": [("391360971", "GILEAD SCIENCES", S, None, None)],
    "exxon": [("352170013", "EXXONMOBIL CHEMICAL FRANCE", S + " (deregistered 2021)", None, None),
              ("379914237", "ESSO RAFFINAGE", S + " (Esso SAF group; deregistered 2021)", None, None)],
    "blackrock": [("H999290616", "BLACKROCK INVESTMENT MANAGEMENT (UK) LIMITED", S, None, None)],
    "visa": [("509930699", "VISA EUROPE LIMITED", S, None, None)],
    "mastercard": [("501573158", "MASTERCARD FRANCE", S, None, None)],
    "boeing": [("612039503", "BOEING FRANCE SAS", S, None, None)],
    "rtx": [("302315510", "RAYTHEON SYSTEMS FRANCE", S + " (Raytheon)", None, None)],
    "ge": [("480046895", "GE FRANCE", S + " (counted to 2023; GE Vernova spun off Apr 2024)", None, 2023),
           ("404434946", "GE RENEWABLE MANAGEMENT", S + " (renewables, pre-Vernova spin; deregistered 2022)", None, 2022),
           ("315013359", "GE MEDICAL SYSTEMS", S + " (counted to 2022; GE HealthCare spun off Jan 2023)", None, 2022)],
    "honeywell": [("H210172663", "HONEYWELL EUROPE N.V", S, None, None)],
    "3m": [("542078555", "3M FRANCE", S, None, None)],
    "tesla": [("524335262", "TESLA FRANCE", S, None, None),
              ("933280521", "TESLA ENERGY VENTURES FRANCE", S, None, None)],
    "cocacola": [("404421083", "COCA COLA SERVICES FRANCE", S + " (CCEP France excluded - independent bottler)", None, None)],
    "pepsico": [("381511039", "PEPSICO FRANCE", S, None, None)],
    "pg": [("391543576", "PROCTER & GAMBLE FRANCE", S, None, None)],
    "pmi": [("712054014", "PHILIP MORRIS FRANCE", S, None, None)],
    "mcdonalds": [("722003936", "MCDONALD'S FRANCE", S, None, None)],
    "att": [("H533906828", "AT&T", S + " (AT&T Global Network Services Belgium Luxembourg BV)", None, None)],
    "comcast": [("502674252", "UNIVERSAL PICTURES INTERNATIONAL FRANCE SAS", S + " (NBCUniversal)", None, None)],
    "dow": [("552012791", "DOW FRANCE", S, None, None)],
    "corteva": [("950417493", "CORTEVA AGRISCIENCE FRANCE SAS", S, None, None),
                ("433934072", "DOW AGROSCIENCES DISTRIBUTION", S + " (Dow AgroSciences became part of Corteva at the 2019 DowDuPont split)", 2019, None)],
    "siemens": [("562016774", "SIEMENS S.A.S", S, None, None),
                ("810794800", "SIEMENS HEALTHCARE SAS", S + " (Healthineers)", None, None),
                ("833751431", "SIEMENS MOBILITY SAS", S, None, None),
                ("823619804", "SIEMENS ENERGY S.A.S.", S + " (only FY ending Oct 2019, before the Sept 2020 Siemens Energy spin-off)", None, 2019)],
    "basf": [("542069158", "BASF FRANCE SAS", S, None, None)],
    "bayer": [("562038893", "BAYER SAS", S, None, None),
              ("706580149", "BAYER HEALTHCARE SAS", S, None, None)],
    "bmw": [("722000965", "BMW FRANCE", S, None, None)],
    "sap": [("379821994", "SAP FRANCE", S, None, None)],
    "allianz": [("542110291", "ALLIANZ I.A.R.D.", S + " (Allianz France)", None, None),
                ("340234962", "ALLIANZ VIE", S + " (Allianz France)", None, None)],
    "airbus": [("383474814", "AIRBUS SAS", S, None, None),
               ("393341516", "AIRBUS DEFENCE AND SPACE SAS", S, None, None),
               ("352383715", "AIRBUS HELICOPTERS", S + " (MBDA, ArianeGroup, Arianespace excluded - JVs)", None, None)],
    "totalenergies": [("542051180", "TOTALENERGIES SE", P, None, None),
                      ("531680445", "TOTALENERGIES MARKETING FRANCE", S, None, None),
                      ("542034921", "TOTALENERGIES MARKETING SERVICES", S, None, None),
                      ("442395448", "TOTALENERGIES ELECTRICITE ET GAZ FRANCE", S, None, None),
                      ("434836276", "TOTALENERGIES RENOUVELABLES FRANCE", S, None, None),
                      ("692004807", "TOTALENERGIES RAFFINAGE CHIMIE", S, None, None),
                      ("529221749", "TOTALENERGIES RAFFINAGE FRANCE", S, None, None),
                      ("428891113", "TOTALENERGIES PETROCHEMICALS FRANCE", S, None, None)],
    "sanofi": [("403335938", "SANOFI-AVENTIS GROUPE", P + " (Opella excluded - majority sold 2025)", None, None),
               ("403335904", "SANOFI-AVENTIS FRANCE", S, None, None),
               ("775662257", "SANOFI WINTHROP INDUSTRIE", S, None, None)],
    "loreal": [("632012100", "L'OREAL", P, None, None),
               ("919434894", "L'OREAL FRANCE", S, None, None)],
    "lvmh": [("775670417", "LVMH MOET HENNESSY LOUIS VUITTON", P, None, None),
             ("338228414", "MOET HENNESSY", S + " (66% LVMH)", None, None)],
    "bnpparibas": [("662042449", "BNP PARIBAS", P, None, None),
                   ("542097902", "BNP PARIBAS PERSONAL FINANCE", S, None, None),
                   ("H409296750", "BNP PARIBAS IMMOBILIER PROMOTION", S, None, None)],
    "axa": [("572093920", "AXA", P, None, None),
            ("310499959", "AXA FRANCE VIE", S, None, None),
            ("722057460", "AXA FRANCE IARD", S, None, None)],
    "danone": [("552032534", "DANONE", P, None, None),
               ("672039971", "DANONE PRODUITS FRAIS FRANCE", S, None, None)],
    "orange": [("380129866", "ORANGE", P, None, None),
               ("572043800", "ORANGE BANK", S + " (deregistered 2024)", None, None)],
    "renault": [("780129987", "RENAULT SAS", S + " (Renault Trucks excluded - Volvo Group)", None, None)],
    "safran": [("562082909", "SAFRAN", P, None, None),
               ("818503146", "SAFRAN.AI", S + " (ex-Preligens, acquired by Safran 2024)", 2024, None)],
    "thales": [("552059024", "THALES", P, None, None),
               ("414725101", "THALES ALENIA SPACE FRANCE", S + " (67% Thales)", None, None)],
    "stellantis": [("879786085", "STELLANTIS N.V", P, None, None),
                   ("552100554", "PEUGEOT SA", "predecessor (PSA merged into Stellantis Jan 2021; deregistered 2022)", None, None)],
    "shell": [("780130175", "SHELL FRANCE", S, None, None),
              ("509432720", "SHELL FRANCE BIOGAS SAS", S, None, None)],
    "philips": [("811847243", "PHILIPS FRANCE", S, None, None)],
    "heineken": [("328730759", "HEINEKEN FRANCE", S, None, None)],
    "astrazeneca": [("558201075", "ASTRAZENECA", S + " (AstraZeneca SAS, France)", None, None)],
    "gsk": [("642041362", "LABORATOIRE GLAXOSMITHKLINE", S, None, None),
            ("413900382", "VIIV HEALTHCARE SAS", S + " (ViiV, majority GSK; Haleon excluded - spun off 2022)", None, None)],
    "bat": [("303765630", "BRITISH AMERICAN TOBACCO FRANCE", S, None, None)],
    "nestle": [("542014428", "NESTLE FRANCE", S, None, None),
               ("539351544", "NESTLE FRANCE MG", S, None, None),
               ("550200018", "NESTLE EXCELLENCE SUPPORTS FRANCE OU NES FRANCE", S, None, None),
               ("479463044", "NESTLE WATERS MARKETING & DISTRIBUTION", S, None, None),
               ("393204516", "NESTLE WATERS MANAGEMENT & TECHNOLOGY", S, None, None),
               ("382597821", "NESPRESSO FRANCE", S, None, None)],
    "novartis": [("410349070", "NOVARTIS PHARMA SAS", S, None, None),
                 ("892974460", "NOVARTIS GENE THERAPIES FRANCE S.A.S.", S, None, None),
                 ("552123341", "SANDOZ", S + " (counted to 2022; Sandoz spun off Oct 2023)", None, 2022)],
    "roche": [("552012031", "ROCHE", S + " (Roche SAS)", None, None),
              ("380484766", "ROCHE DIAGNOSTICS FRANCE", S, None, None),
              ("800418493", "ROCHE DIABETES CARE FRANCE", S, None, None)],
    "equinor": [("752957720", "EQUINOR ENERGY BELGIUM NV", S, None, None)],
    "novonordisk": [("451356992", "NOVO NORDISK", S + " (Novo Nordisk SAS)", None, None)],
    "ericsson": [("334736147", "ERICSSON FRANCE", S, None, None)],
    "nokia": [("493378939", "NOKIA NETWORKS FRANCE", S, None, None)],
    "iberdrola": [("538781857", "AILES MARINES SAS", S + " (Saint-Brieuc offshore wind, Iberdrola)", None, None)],
    "eni": [("451225692", "ENI PLENITUDE FRANCE", S + " (Plenitude, majority Eni)", None, None)],
}


def norm(s):
    return " ".join((s or "").replace("’", "'").split()).casefold()


def num(x):
    return "" if x is None else (round(x, 2) if isinstance(x, float) else x)


def year_ok(y, lo, hi):
    return (lo is None or y >= lo) and (hi is None or y <= hi)


# ----------------------------------------------------------------------------- DE
def de_record(e):
    """Figures of one DE register entry version, or None if no completed fiscal year."""
    fe = e.get("financialExpenses") or {}
    if not fe.get("relatedFiscalYearEnd") or not fe.get("financialExpensesEuro"):
        return None
    rng = fe["financialExpensesEuro"]
    emp = e.get("employeesInvolvedInLobbying") or {}
    rp = e.get("regulatoryProjects") or {}
    st = e.get("statements") or {}
    return {
        "fy_end": fe["relatedFiscalYearEnd"],
        "low": rng.get("from"), "high": rng.get("to"),
        "persons": emp.get("employeeFTE"),
        "projects": rp.get("regulatoryProjectsCount", len(rp.get("regulatoryProjects") or [])),
        "actions": None,
        "statements": st.get("statementsCount", len(st.get("statements") or [])),
        "open": False,
    }


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def de_history(e):
    """Walk back through earlier GL2024 versions to collect each prior fiscal year."""
    out, cur = [], e
    versions = (e.get("accountDetails") or {}).get("registerEntryVersions") or []
    reg = e["registerNumber"]
    seen = set()
    for _ in range(10):
        lfu = ts(((cur.get("registerEntryDetails") or {}).get("fiscalYearUpdate") or {}).get("lastFiscalYearUpdate"))
        if not lfu:
            break
        prior = [v for v in versions if v.get("validUntilDate") and ts(v["validUntilDate"]) <= lfu]
        if not prior:
            break
        v = max(prior, key=lambda v: ts(v["validUntilDate"]))
        if v["registerEntryId"] in seen:
            break
        seen.add(v["registerEntryId"])
        path = OUT / "de_versions" / f"{reg}_{v['registerEntryId']}.json"
        fetched = path.exists()

        def fetch(v=v):
            time.sleep(0.3)
            return get(DE_VERSION_URL.format(reg=reg, eid=v["registerEntryId"]))
        cur = json.loads(cached(path, fetch))
        if not fetched:
            pass
        rec = de_record(cur)
        if rec:
            out.append(rec)
    return out


def load_de():
    raw = cached(OUT / "de_lobbyregister.json", lambda: get(DE_URL))
    data = json.loads(raw)
    del raw
    entries = {e["registerNumber"]: e for e in data["results"]}
    meta = {"source_date": data.get("sourceDate"), "n_entries": len(entries)}
    return entries, meta


def de_name(e):
    li = e.get("lobbyistIdentity") or {}
    return li.get("name") or li.get("companyName") or ""


# ----------------------------------------------------------------------------- FR
def fr_bracket(s):
    """'> = 100 000 euros et < 200 000 euros' -> (100000, 200000, open_ended)."""
    if not s:
        return None
    parts = s.replace(" ", " ").split(" et ")
    def val(p):
        return int("".join(ch for ch in p if ch.isdigit()))
    if len(parts) == 2:
        return val(parts[0]), val(parts[1]), False
    if s.strip().startswith("<"):
        return 0, val(s), False
    return val(s), None, True


def fr_records(e):
    out = []
    for x in e.get("exercices") or []:
        p = x.get("publicationCourante") or {}
        b = fr_bracket(p.get("montantDepense"))
        if not b or not p.get("dateFin"):
            continue
        acts = p.get("activites") or []
        n_actions = sum(len((a.get("publicationCourante") or {}).get("actionsRepresentationInteret") or [])
                        for a in acts)
        d, m, y = p["dateFin"].split("-")
        out.append({
            "fy_end": f"{y}-{m}-{d}",
            "low": b[0], "high": b[1], "open": b[2],
            "persons": p.get("nombreSalaries"),
            "projects": len(acts) if acts else p.get("nombreActivite", 0),
            "actions": n_actions,
            "statements": None,
        })
    return out


def load_fr():
    raw = cached(OUT / "fr_agora_repertoire.json", lambda: get(FR_URL))
    data = json.loads(raw)
    del raw
    return {e["identifiantNational"]: e for e in data["publications"]}


# ----------------------------------------------------------------------------- main
def main():
    ids = {c["company_id"] for c in companies()}
    for table in (DE, FR):
        unknown = set(table) - ids
        assert not unknown, f"alias table has unknown company_ids: {unknown}"

    matches, recs = [], defaultdict(list)   # (cid, country, year) -> [(reg_id, rec)]

    def add(cid, country, reg_id, name, alias, note, lo, hi, records):
        _, expected, _, _, _ = alias
        method = "manual_id_alias" if norm(expected) == norm(name) else "manual_id_alias_NAME_CHANGED"
        if method.endswith("CHANGED"):
            print(f"  warning: {country} {reg_id} expected {expected!r}, register says {name!r}")
        window = ""
        if lo or hi:
            window = f" [counted {lo or ''}-{hi or ''}]"
        matches.append({"company_id": cid, "country": country, "register_id": reg_id,
                        "register_name": name, "match_method": method, "note": note + window})
        for r in records:
            y = int(r["fy_end"][:4])
            if year_ok(y, lo, hi):
                recs[(cid, country, y)].append((reg_id, r))

    print("DE: loading register ...")
    de, meta = load_de()
    print(f"DE: {meta['n_entries']} entries, source date {meta['source_date']}")
    for cid, aliases in DE.items():
        for alias in aliases:
            reg_id, expected, note, lo, hi = alias
            e = de.get(reg_id)
            if e is None:
                print(f"  warning: DE {reg_id} ({expected}) not in register")
                continue
            records = [r for r in [de_record(e)] if r] + de_history(e)
            # one record per fiscal year (latest version wins)
            byfy = {}
            for r in records:
                byfy.setdefault(r["fy_end"], r)
            add(cid, "DE", reg_id, de_name(e).strip(), alias, note, lo, hi, list(byfy.values()))
    del de

    print("FR: loading register ...")
    fr = load_fr()
    print(f"FR: {len(fr)} registrants")
    for cid, aliases in FR.items():
        for alias in aliases:
            reg_id, expected, note, lo, hi = alias
            e = fr.get(reg_id)
            if e is None:
                print(f"  warning: FR {reg_id} ({expected}) not in register")
                continue
            if e.get("dateCessation"):
                note = note + f" (dateCessation {e['dateCessation']})" if "deregistered" not in note else note
            add(cid, "FR", reg_id, e["denomination"].strip(), alias, note, lo, hi, fr_records(e))
    del fr

    rows = []
    for (cid, country, year), items in sorted(recs.items()):
        low = sum(r["low"] or 0 for _, r in items)
        is_open = any(r["open"] for _, r in items)
        high = None if is_open else sum(r["high"] or 0 for _, r in items)
        mid = sum((r["low"] if r["open"] else (r["low"] + r["high"]) / 2) for _, r in items)
        persons = [r["persons"] for _, r in items if r["persons"] is not None]
        rows.append({
            "company_id": cid, "country": country, "year": year,
            "lobby_cost_low_eur": low, "lobby_cost_high_eur": num(high),
            "lobby_cost_mid_eur": round(mid),
            "lobbyists_fte_or_persons": num(sum(persons)) if persons else "",
            "lobbyists_measure": "FTE" if country == "DE" else "persons",
            "n_registrations": len({rid for rid, _ in items}),
            "n_legislative_projects": sum(r["projects"] or 0 for _, r in items),
            "n_actions": "" if country == "DE" else sum(r["actions"] or 0 for _, r in items),
            "n_statements": sum(r["statements"] or 0 for _, r in items) if country == "DE" else "",
            "cost_open_ended": int(is_open),
            "fiscal_year_end": ";".join(sorted({r["fy_end"] for _, r in items})),
            "register_ids": ";".join(sorted({rid for rid, _ in items})),
        })

    fields = ["company_id", "country", "year", "lobby_cost_low_eur", "lobby_cost_high_eur",
              "lobby_cost_mid_eur", "lobbyists_fte_or_persons", "lobbyists_measure", "n_registrations",
              "n_legislative_projects", "n_actions", "n_statements", "cost_open_ended",
              "fiscal_year_end", "register_ids"]
    write_csv(CLEAN / "national_lobby.csv", rows, fields)
    write_csv(CLEAN / "national_lobby_matches.csv", matches,
              ["company_id", "country", "register_id", "register_name", "match_method", "note"])

    for country in ("DE", "FR"):
        cs = {r["company_id"] for r in rows if r["country"] == country}
        ys = sorted({r["year"] for r in rows if r["country"] == country})
        nm = sum(1 for m in matches if m["country"] == country)
        print(f"{country}: {nm} register entries matched, {len(cs)} companies with figures, years {ys}")
    print(f"wrote {len(rows)} rows -> {CLEAN / 'national_lobby.csv'}")


if __name__ == "__main__":
    main()
