"""US federal contract (and grant) obligations per company per fiscal year, FY2015-FY2025.

Source: USAspending API (https://api.usaspending.gov, no key).

Method
  1. Candidate recipients. For each company we search the recipient list
     (POST /api/v2/recipient/) with a short list of explicit keywords
     (`SEARCH`). Results are recipient-profile records at three levels:
     P = parent (all awards whose reported parent UEI/DUNS is this entity,
     including the entity itself and its children), C = child, R = recipient
     with no parent on file.
  2. Matching. A P or R record is accepted if its normalised name fully
     matches one of the company's explicit alias patterns (`ACCEPT`, anchored
     regexes written by hand after reviewing the candidates; no fuzzy
     matching). The recipient profiles of all alias-named records are then
     read and their parent history followed (see match_company): parents with
     an alias name, or with the same UEI/DUNS as an accepted record, are
     added; other parents of alias-named records are written to the matches
     file as `unmatched_parent_of_alias_record` (not summed) for review.
     C records are never summed (their awards sit inside a P record).
  3. Amounts. For every accepted record we call
     POST /api/v2/search/spending_over_time/ (group=fiscal_year,
     filters.recipient_id=<hash>-<level>, FY2015-FY2025), which splits
     obligations into Contract_Obligations (award types A-D), Idv_Obligations,
     Grant_Obligations, etc. P records are disjoint (each transaction carries
     exactly one parent), and R records hold only parent-less transactions,
     so summing accepted P and R records does not double count.

Outputs
  data/influence/clean/us_contracts.csv          company_id, fiscal_year,
      contract_obligations_usd (A-D), idv_obligations_usd, grants_usd,
      n_recipient_records, n_contract_awards (not available: the award-count
      endpoint ignores the recipient_id filter, left blank)
  data/influence/clean/us_contracts_matches.csv  one row per accepted /
      flagged recipient record with its FY2015-25 contract total

Caveats
  * Parent links are as reported in SAM.gov at the time of each action, so
    the hierarchy is fragmented: a group often has many P records (old DUNS-era
    parents, foreign ultimate parents such as "BAE SYSTEMS PLC", US holding
    companies). Subsidiaries trading under unrelated names are captured only
    when their reported parent is one of the accepted records.
  * Acquired / divested businesses follow the SAM parent at the time of the
    award (e.g. Raytheon before 2020 sits under "RAYTHEON COMPANY"; we accept
    the legacy names of the merged entity where the current group is the
    successor -- see notes in ACCEPT).
  * Obligations can be negative (de-obligations) in a given year.
"""

import json
import re
import sys

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

API = "https://api.usaspending.gov/api/v2"
OUT = RAW / "us_contracts"
FY0, FY1 = 2015, 2025

# Search keywords (substring search on recipient names).
SEARCH = {
    "alphabet": ["GOOGLE", "ALPHABET INC", "VERILY", "WAYMO"],
    "meta": ["META PLATFORMS", "FACEBOOK", "OCULUS"],
    "microsoft": ["MICROSOFT"],
    "apple": ["APPLE INC", "APPLE COMPUTER"],
    "amazon": ["AMAZON", "WHOLE FOODS"],
    "oracle": ["ORACLE"],
    "ibm": ["INTERNATIONAL BUSINESS MACHINES", "IBM "],
    "intel": ["INTEL CORP", "INTEL FEDERAL"],
    "qualcomm": ["QUALCOMM"],
    "nvidia": ["NVIDIA"],
    "cisco": ["CISCO SYSTEMS"],
    "uber": ["UBER TECHNOLOGIES"],
    "airbnb": ["AIRBNB"],
    "pfizer": ["PFIZER"],
    "jnj": ["JOHNSON & JOHNSON", "JOHNSON AND JOHNSON", "JANSSEN", "ETHICON", "DEPUY"],
    "merck": ["MERCK SHARP", "MERCK & CO", "MERCK AND CO"],
    "abbvie": ["ABBVIE"],
    "lilly": ["ELI LILLY", "LILLY USA"],
    "bms": ["BRISTOL-MYERS", "BRISTOL MYERS", "CELGENE"],
    "amgen": ["AMGEN"],
    "gilead": ["GILEAD SCIENCES"],
    "exxon": ["EXXON", "MOBIL OIL", "XTO ENERGY"],
    "chevron": ["CHEVRON"],
    "conocophillips": ["CONOCOPHILLIPS"],
    "jpmorgan": ["JPMORGAN", "JP MORGAN", "J.P. MORGAN", "J P MORGAN", "CHASE BANK"],
    "goldman": ["GOLDMAN SACHS"],
    "morganstanley": ["MORGAN STANLEY"],
    "bofa": ["BANK OF AMERICA", "MERRILL LYNCH"],
    "citi": ["CITIBANK", "CITIGROUP", "CITICORP"],
    "blackrock": ["BLACKROCK"],
    "visa": ["VISA U.S.A", "VISA USA", "VISA INC", "VISA INTERNATIONAL"],
    "mastercard": ["MASTERCARD"],
    "boeing": ["BOEING"],
    "lockheed": ["LOCKHEED", "SIKORSKY"],
    "rtx": ["RAYTHEON", "RTX ", "UNITED TECHNOLOGIES", "PRATT & WHITNEY", "COLLINS AEROSPACE", "ROCKWELL COLLINS"],
    "generaldynamics": ["GENERAL DYNAMICS", "ELECTRIC BOAT", "GULFSTREAM AEROSPACE", "BATH IRON WORKS"],
    "northrop": ["NORTHROP"],
    "ge": ["GENERAL ELECTRIC", "GE AVIATION", "GE AEROSPACE"],
    "honeywell": ["HONEYWELL"],
    "3m": ["3M CO", "3M INNOVATIVE"],
    "caterpillar": ["CATERPILLAR"],
    "ford": ["FORD MOTOR"],
    "gm": ["GENERAL MOTORS"],
    "tesla": ["TESLA"],
    "cocacola": ["COCA-COLA", "COCA COLA"],
    "pepsico": ["PEPSICO", "PEPSI-COLA", "FRITO"],
    "pg": ["PROCTER & GAMBLE", "PROCTER AND GAMBLE"],
    "pmi": ["PHILIP MORRIS"],
    "altria": ["ALTRIA"],
    "walmart": ["WALMART", "WAL-MART"],
    "mcdonalds": ["MCDONALD'S CORP", "MCDONALDS CORP"],
    "att": ["AT&T", "AT & T"],
    "verizon": ["VERIZON", "CELLCO"],
    "comcast": ["COMCAST", "NBCUNIVERSAL"],
    "dow": ["DOW CHEMICAL", "DOW INC"],
    "dupont": ["DUPONT", "DU PONT"],
    "corteva": ["CORTEVA", "PIONEER HI-BRED"],
    "siemens": ["SIEMENS"],
    "basf": ["BASF"],
    "bayer": ["BAYER", "MONSANTO"],
    "volkswagen": ["VOLKSWAGEN", "AUDI OF AMERICA"],
    "bmw": ["BMW"],
    "mercedes": ["MERCEDES-BENZ", "MERCEDES BENZ", "DAIMLER"],
    "dtelekom": ["T-MOBILE", "DEUTSCHE TELEKOM", "T-SYSTEMS", "SPRINT"],
    "sap": ["SAP AMERICA", "SAP NATIONAL SECURITY", "SAP SE", "SAP AG", "SAP PUBLIC", "SAP GOVERNMENT",
            "CONCUR TECHNOLOGIES"],
    "allianz": ["ALLIANZ", "PACIFIC INVESTMENT MANAGEMENT", "FIREMAN'S FUND"],
    "deutschebank": ["DEUTSCHE BANK"],
    "airbus": ["AIRBUS"],
    "totalenergies": ["TOTALENERGIES", "TOTAL S.A", "TOTAL SA", "TOTAL PETROCHEMICALS", "TOTAL E&P", "TOTAL GAS"],
    "sanofi": ["SANOFI", "GENZYME"],
    "loreal": ["L'OREAL", "LOREAL"],
    "lvmh": ["LVMH", "TIFFANY AND CO", "TIFFANY & CO"],
    "bnpparibas": ["BNP PARIBAS", "BANK OF THE WEST"],
    "axa": ["AXA "],
    "danone": ["DANONE"],
    "orange": ["ORANGE BUSINESS", "ORANGE S.A", "ORANGE SA"],
    "renault": ["RENAULT"],
    "safran": ["SAFRAN"],
    "thales": ["THALES"],
    "stellantis": ["STELLANTIS", "FCA US", "CHRYSLER", "FIAT CHRYSLER"],
    "shell": ["SHELL OIL", "SHELL USA", "SHELL TRADING", "SHELL ENERGY", "SHELL PLC", "SHELL INTERNATIONAL",
              "SHELL EXPLORATION", "SHELL CHEMICAL"],
    "ing": ["ING BANK", "ING GROEP", "ING CAPITAL"],
    "asml": ["ASML", "CYMER"],
    "philips": ["PHILIPS", "RESPIRONICS"],
    "unilever": ["UNILEVER", "CONOPCO"],
    "heineken": ["HEINEKEN"],
    "ahold": ["AHOLD", "DELHAIZE", "FOOD LION", "STOP & SHOP", "HANNAFORD"],
    "prosus": ["PROSUS", "NASPERS"],
    "bp": ["BP AMERICA", "BP PRODUCTS", "BP P.L.C", "BP PLC", "BP EXPLORATION", "BP OIL", "BP ENERGY",
           "BP CORPORATION"],
    "astrazeneca": ["ASTRAZENECA", "MEDIMMUNE"],
    "gsk": ["GLAXOSMITHKLINE", "GSK ", "VIIV"],
    "hsbc": ["HSBC"],
    "barclays": ["BARCLAYS"],
    "bae": ["BAE SYSTEMS"],
    "bat": ["BRITISH AMERICAN TOBACCO", "REYNOLDS AMERICAN", "R.J. REYNOLDS", "R J REYNOLDS"],
    "riotinto": ["RIO TINTO", "KENNECOTT"],
    "vodafone": ["VODAFONE"],
    "nestle": ["NESTLE", "GERBER PRODUCTS", "NESTLÉ"],
    "novartis": ["NOVARTIS", "SANDOZ"],
    "roche": ["ROCHE", "GENENTECH"],
    "ubs": ["UBS "],
    "equinor": ["EQUINOR", "STATOIL"],
    "novonordisk": ["NOVO NORDISK"],
    "ericsson": ["ERICSSON"],
    "nokia": ["NOKIA", "ALCATEL-LUCENT", "ALCATEL LUCENT"],
    "iberdrola": ["IBERDROLA", "AVANGRID"],
    "santander": ["SANTANDER"],
    "enel": ["ENEL "],
    "eni": ["ENI S.P.A", "ENI SPA", "ENI US", "ENI PETROLEUM", "ENI TRADING"],
    "spotify": ["SPOTIFY"],
}

# Accepted recipient names: regexes that must FULLY match the normalised name
# (upper case; every run of characters other than A-Z, 0-9 and & becomes one space).
ACCEPT = {
    "alphabet": [r"GOOGLE (LLC|INC|PUBLIC SECTOR LLC)", r"ALPHABET INC", r"VERILY LIFE SCIENCES LLC",
                 r"WAYMO LLC"],
    "meta": [r"META PLATFORMS INC", r"FACEBOOK INC", r"FACEBOOK GLOBAL HOLDINGS I LLC"],
    "microsoft": [r"MICROSOFT CORP(ORATION)?"],
    "apple": [r"APPLE INC"],
    "amazon": [r"AMAZON WEB SERVICES INC", r"AMAZON COM( INC| SERVICES LLC)?"],
    "oracle": [r"ORACLE CORP(ORATION)?", r"ORACLE AMERICA INC", r"ORACLE HEALTH GOVERNMENT SERVICES INC"],
    "ibm": [r"INTERNATIONAL BUSINESS MACHINES CORP(ORATION)?", r"IBM CORPORATION",
            r"IBM WORLD TRADE CORPORATION"],
    "intel": [r"INTEL CORP(ORATION)?", r"INTEL FEDERAL LLC"],
    "qualcomm": [r"QUALCOMM INCORPORATED", r"QUALCOMM INTELLIGENT SOLUTIONS INC"],
    "nvidia": [r"NVIDIA CORP(ORATION)?", r"NVIDIA PUBLIC SECTOR CORPORATION"],
    "cisco": [r"CISCO SYSTEMS INC"],
    "uber": [r"UBER TECHNOLOGIES INC"],
    "airbnb": [r"AIRBNB INC"],
    "pfizer": [r"PFIZER INC"],
    "jnj": [r"JOHNSON & JOHNSON", r"JOHNSON & JOHNSON (SERVICES LLC|SURGICAL INC|HEALTH CARE SYSTEMS INC)",
            r"ETHICON( US)?( INC| LLC)?", r"DEPUY SYNTHES SALES INC", r"DEPUY MITEK INCORPORATED",
            r"DEPUY ORTHOPAEDICS( INC)?", r"JANSSEN (PHARMACEUTICALS|RESEARCH & DEVELOPMENT|BIOTECH)( INC| LLC)?"],
    "merck": [r"MERCK & CO INC", r"MERCK SHARP & DOHME (CORP|LLC)"],
    "abbvie": [r"ABBVIE (INC|US LLC)"],
    "lilly": [r"ELI LILLY (AND COMPANY|CO)"],
    "bms": [r"BRISTOL MYERS SQUIBB (COMPANY|PHARMA COMPANY|CO)", r"CELGENE CORPORATION"],
    "amgen": [r"AMGEN (INC|USA INC)"],
    "gilead": [r"GILEAD SCIENCES INC"],
    "exxon": [r"EXXON MOBIL CORP(ORATION)?", r"EXXONMOBIL (OIL CORPORATION|EXPLORATION COMPANY|SALES & SUPPLY|"
              r"ENERGY PARTNERS LLC)", r"MOBIL OIL (GUAM|MARIANA ISLANDS) INC", r"EXXON RES & DEV LABORATORIES",
              r"XTO ENERGY INC"],
    "chevron": [r"CHEVRON CORPORATION", r"CHEVRON U ?S ?A INC", r"CHEVRON PHILLIPS CHEMICAL COMPANY LLC",
                r"CHEVRON TECHNOLOGY VENTURES( LLC)?", r"CHEVRON ENERGY SOLUTIONS COMPA(NY)?"],
    "conocophillips": [r"CONOCOPHILLIPS( COMPANY)?"],
    "jpmorgan": [r"JPMORGAN CHASE & CO", r"J ?P ?MORGAN CHASE BANK( N ?A| NATIONAL ASSOCIATION| NATIONAL)?",
                 r"JPMORGAN CHASE BANK N A LONDON", r"JPMORGAN CHASE"],
    "goldman": [r"GOLDMAN SACHS( GROUP INC| & CO LLC| BANK USA)?"],
    "morganstanley": [r"MORGAN STANLEY"],
    "bofa": [r"BANK OF AMERICA( CORPORATION| NATIONAL ASSOCIATION| N ?A)?"],
    "citi": [r"CITIBANK( NATIONAL ASSOCIATION| N ?A)?", r"CITIBANK N A LONDON", r"CITIGROUP INC"],
    "blackrock": [r"BLACKROCK INC"],
    "visa": [r"VISA (USA|U S A|INC|INTERNATIONAL SERVICE ASSOCIATION)( INC)?"],
    "mastercard": [r"MASTERCARD INCORPORATED"],
    "boeing": [r"BOEING COMPANY THE( INC)?", r"BOEING CO(MPANY)?", r"THE BOEING COMPANY", r"BOEING",
               r"BOEING (AEROSPACE OPERATIONS|INTELLIGENCE & ANALYTICS|SATELLITE SYSTEMS|NORTH AMERICAN|"
               r"DISTRIBUTION SERVICES DEFENSE|DISTRIBUTION SERVICES VI|CAPITAL CORPORATION)( INC| LLC)?", r"BOEING AEROSPACE OPERATIONS I",
               r"BOEING DEFENCE AUSTRALIA LTD"],
    "lockheed": [r"LOCKHEED MARTIN( CORP(ORATION)?| GLOBAL INC| INTEGRATED SYSTEMS (LLC|INC)| SERVICES( INC| LLC)?|"
                 r" ASPEN SYSTEMS CORPORATION| UNMANNED INTEGRATED SYSTEMS INC| OVERSEAS( SERVICES CORPORATION| LLC)|"
                 r" ACULIGHT CORPORATION| UK INSYS LTD| CANADA INC| TACTICAL SYSTEMS COMPANY|"
                 r" PROCERUS TECHNOLOGIES L C| ENERGY SYSTEMS)?",
                 # Sikorsky acquired from United Technologies in Nov 2015 (FY2016).
                 r"SIKORSKY (AIRCRAFT CORPORATION|SUPPORT SERVICES INC)"],
    # RTX = Raytheon Technologies (2020 merger of United Technologies and Raytheon Co). Carrier and
    # Otis (spun off 2020) report under their own names and are not matched.
    "rtx": [r"RTX CORPORATION", r"RAYTHEON( COMPANY( INC)?| CO| TECHNOLOGIES CORPORATION| TECHNICAL SERVICES"
            r" (CO|COMPANY LLC)| APPLIED SIGNAL TECHNOLOGY INC| CANADA LIMITED| SYSTEMS LTD| BLACKBIRD TECHNOLOGIES INC)",
            r"RTX BBN TECHNOLOGIES INC", r"UNITED TECHNOLOGIES CORP(ORATION|ORATIO)?",
            r"PRATT & WHITNEY( ENGINE SERVICES INC| COMPONENT SOLUTIONS INC)?",
            r"ROCKWELL COLLINS( INC)?", r"COLLINS AEROSPACE"],
    "generaldynamics": [r"GENERAL DYNAMICS( CORP(ORATION)?| LAND SYSTEMS( INC| CANADA CORPORATION)?|"
                        r" ORDNANCE AND TACTICAL SYSTEMS INC| MISSION SYSTEMS INC| MOTION CONTROL LLC|"
                        r" INFORMATION TECHNOLOGY( INC| INCOR)| GOVERNMENT SY| ADVANCED INFORMATION SYSTEMS INCORPORATED|"
                        r" OTS INC| GLOBAL IMAGING TECHNOLOGIES INC| SATCOM TECHNOLOGIES INC| CANADA LTD|"
                        r" EUROPEAN LAND SYSTEMS BRIDGE SYSTEMS GMBH)",
                        r"ELECTRIC BOAT CORPORATION", r"GULFSTREAM AEROSPACE (LP|CORPORATION)",
                        r"BATH IRON WORKS CORPORATION"],
    "northrop": [r"NORTHROP GRUMMAN( CORP(ORATION)?| SYSTEMS CORP(ORATION|O)?| INNOVATION SYSTEMS (LLC|INC)|"
                 r" SPACE & MISSION SYSTEMS CORP| DEFENSE MISSION SYSTEMS INC| INFORMATION TECHNOLOGY OVERSEAS INC|"
                 r" SPERRY MARINE.*| ITALIA SPA| SECURITY SYSTEMS LLC| INTERNATIONAL TRADING INC)", r"NGC DENMARK APS"],
    "ge": [r"GENERAL ELECTRIC (COMPANY|CO)( INC)?", r"GE AVIATION SYSTEMS (LLC|LIMITED|LTD)", r"GE AEROSPACE INC",
           r"GENERAL ELECTRIC INTERNATIONAL INC", r"GE AVIATION UK", r"IGE USA INVESTMENTS"],
    "honeywell": [r"HONEYWELL INTERNATIONAL INC(ORPORATED)?", r"HONEYWELL FEDERAL MANUFACTURING & TECHNOLOGIES LLC",
                  r"HONEYWELL TECHNOLOGY SOLUTIONS INC(ORPORATED)?", r"HONEYWELL INC", r"HONEYWELL INTL",
                  r"HONEYWELL AEROSPACE YEOVIL", r"HONEYWELL GMBH", r"HONEYWELL LIMITED",
                  r"HONEYWELL NORMALAIR GARRETT( HOLDINGS LTD| LI| LT)"],
    "3m": [r"3M CO(MPANY)?", r"3M INNOVATIVE PROPERTIES COMPANY"],
    "caterpillar": [r"CATERPILLAR INC"],
    "ford": [r"FORD MOTOR CO(MPANY)?"],
    "gm": [r"GENERAL MOTORS (COMPANY|CORPORATION|CORP|LLC)"],
    "tesla": [r"TESLA INC", r"TESLA MOTORS INC"],
    "cocacola": [r"COCA COLA COMPANY( THE)?", r"THE COCA COLA COMPANY", r"COCA COLA REFRESHMENTS USA INC"],
    "pepsico": [r"PEPSICO( INC| BEVERAGE SALES LLC)", r"FRITO LAY( INC| NORTH AMERICA INC)?"],
    "pg": [r"PROCTER & GAMBLE COMPANY THE", r"THE PROCTER & GAMBLE COMPANY"],
    "pmi": [r"PHILIP MORRIS INTERNATIONAL INC"],
    "altria": [r"ALTRIA GROUP INC", r"ALTRIA CLIENT SERVICES (INC|LLC)"],
    "walmart": [r"WALMART INC", r"WAL MART STORES INC", r"WAL ?MART ASSOCIATES INC"],
    "mcdonalds": [r"MCDONALD S CORPORATION"],
    "att": [r"AT&T( INC| CORP| ENTERPRISES LLC| MOBILITY( LLC| NATIONAL ACCOUNTS LL)?| TECHNICAL SERVICES COMPANY INC|"
            r" COMMUNICATIONS OF WASHINGTON D C LLC| FEDERAL SYSTEMS| DATACOMM L P)"],
    "verizon": [r"VERIZON( COMMUNICATIONS INC(ORPORATED)?( 0000)?| MARYLAND LLC| VIRGINIA (LLC|INC)|"
                r" NEW JERSEY INC(ORPORATED)?| NORTH LLC| ONLINE LLC| BUSINESS (GLOBAL|NETWORK SERVICES) LLC|"
                r" WIRELESS( CELLCO PARTNERSHIP| INC(ORPORATED)?| PUERTO RICO INCORPORATED)?| SOUTH INC|"
                r" WASHINGTON DC INC| NEW ENGLAND INC| HOLDINGS LLC| DELAWARE LLC| SOUTHWEST INC| NEW YORK INC|"
                r" PENNSYLVANIA LLC| FEDERAL INCORPORATED| HAWAII INCORPORATED| UK LIMITED|"
                r" DIGITAL MEDIA SERVICES INC| CONNECT (FLEET USA LLC|NWF INC))", r"CELLCO PARTNERSHIP( 2245)?"],
    "comcast": [r"COMCAST (CORP(ORATION)?|OF .+|GOVERNMENT SERVICES LLC|CABLE COMMUNICATIONS LLC|"
                r"BUSINESS COMMUNICATIONS( LLC)?|IP PHONE LLC|PHONE LLC( 4822)?|NATIONAL COMMUNICATIONS SERVICES LLC|"
                r"CABLEVISION OF .+|SPORTSNET NEW ENGLAND LLC|SOUTH EAST PENNSYLVANIA LLC)",
                r"NBCUNIVERSAL( MEDIA LLC)?"],
    "dow": [r"DOW CHEMICAL COMPANY THE", r"THE DOW CHEMICAL COMPANY", r"DOW CHEMICAL", r"DOW INC(ORPORATED)?", r"DOW EUROPE GMBH"],
    # E. I. du Pont records before the 2017 Dow merger / 2019 split are attributed to today's DuPont.
    "dupont": [r"E ?I ?DU ?PONT DE NEMOURS AND COM(PANY)?( 4090)?", r"E I DUPONT DE NEMOURS",
               r"DUPONT DE NEMOURS INC", r"DUPONT SPECIALTY PRODUCTS USA LLC"],
    "corteva": [r"CORTEVA (INC|AGRISCIENCE LLC)", r"PIONEER HI BRED INTERNATIONAL( INC)?"],
    # Siemens Energy (spun off 2020) excluded; Siemens Healthineers (majority-owned) included.
    "siemens": [r"SIEMENS (AKTIENGESELLSCHAFT|AG|CORPORATION|INDUSTRY,? INC|INDUSTRY INC|INDUSTRY SOFTWARE INC|"
                r"GOVERNMENT TECHNOLOGIES INC|HEALTHINEERS .+|HEALTHCARE .+|MEDICAL SOLUTIONS USA INC(ORPORATED)?|"
                r"MEDICAL SOLUTIONS|BUILDING TECHNOLOGIES.*|CANADA LIMITED|S A|S P A|PLC|LTD|PTE LTD|"
                r"MOBILITY INC|LOGISTICS AND ASSEMBLY SYSTEMS INC|WATER TECHNOLOGIES CORP|"
                r"PROCESS SYSTEMS ENGINEERING (INC|LIMITED)|AIRFIELDS SOLUTIONS I(NC)?)"],
    "basf": [r"BASF (SE|CORPORATION|METALS LLC|FUEL CELL INC)"],
    # Monsanto acquired June 2018; its records under its own parent are not matched (time-consistent).
    "bayer": [r"BAYER (AKTIENGESELLSCHAFT|AG|CORPORATION|HEALTHCARE LLC|CROPSCIENCE LLC|US LLC|PLC)"],
    "volkswagen": [r"VOLKSWAGEN (AKTIENGESELLSCHAFT|GROUP OF AMERICA INC|AG)"],
    "bmw": [r"BAYERISCHE MOTOREN WERKE (AG|AKTIENGESELLSCHAFT)", r"BMW OF NORTH AMERICA LLC"],
    "mercedes": [r"DAIMLER AG", r"MERCEDES BENZ (GROUP AG|USA LLC|RESEARCH & DEVEL)", r"DAIMLERCHRYSLER AG"],
    # T-Mobile US is majority-owned and consolidated by Deutsche Telekom (Sprint merged into it in 2020).
    "dtelekom": [r"DEUTSCHE TELEKOM (AG|AG T COM|BUSINESS SOLUTIONS GMBH)", r"T MOBILE (USA INC|US INC|"
                 r"SECURE FEDERAL OPERATIONS LLC)", r"T SYSTEMS (LLC|INTERNATIONAL GMBH|BUSINESS SERVICES GMBH|"
                 r"CANADA INC)"],
    "sap": [r"SAP (SE|AG|AMERICA INC|NATIONAL SECURITY SERVICES INC|PUBLIC SERVICES INC)",
            r"CONCUR TECHNOLOGIES INC"],
    "allianz": [r"ALLIANZ (SE|GLOBAL INVESTORS U S LLC|LIFE INSURANCE COMPANY OF NORTH AMERICA|"
                r"SUISSE VERSICHERUNGS GESELLSCHAFT AG)", r"PACIFIC INVESTMENT MANAGEMENT COMPANY LLC"],
    "deutschebank": [r"DEUTSCHE BANK( AG| AG NEW YORK( BRANCH)?| NEW YORK BRANCH| TRUST COMPANY AMERICAS|"
                     r" NATIONAL TRUST COMPANY)?"],
    "airbus": [r"AIRBUS (SE|US SPACE & DEFENSE INC|U S SPACE & DEFENSE INC|HELICOPTERS INC|DS GOVERNMENT SOLUTIONS INC|"
               r"AMERICAS( CUSTOMER SERVICES)? INC|NETHERLANDS B V|DS MILITARY AIRCRAFT INC|DS GEO INC|"
               r"DEFENCE AND SPACE .+|GROUP SE)"],
    "totalenergies": [r"TOTALENERGIES (SE|MARKETING PUERTO RICO CORP)", r"TOTAL SA", r"TOTAL GAS & POWER USA"],
    "sanofi": [r"SANOFI( S A| US SERVICES INC| PASTEUR (INC|VAXDESIGN CORP|LIMITED)| VACCINES US INC| AVENTIS.*)",
               r"GENZYME CORP(ORATION)?"],
    "loreal": [r"L ?OREAL( USA INC| S A| SA)"],
    "lvmh": [r"LVMH MOET HENNESSY LOUIS VUITTON( SE)?", r"TIFFANY AND COMPANY( U S SALES LLC)?"],
    "bnpparibas": [r"BNP PARIBAS( SA| S A)?"],
    "axa": [r"AXA( SA| S A| EQUITABLE LIFE INSURANCE COMPANY)?"],
    "danone": [r"DANONE (NORTH AMERICA (INC|PUBLIC BENEFIT CORPORATION)|S A|SA)"],
    "orange": [r"ORANGE BUSINESS SERVICES( INC| U S INC)?", r"ORANGE (S A|SA)"],
    "renault": [r"RENAULT (S A|SA|SAS)"],
    "safran": [r"SAFRAN (SA|S A|AEROSYSTEMS SERVICES AMERICAS LLC|ELECTRICAL COMPONENTS CANADA INC|DEFENSE & SPACE INC|"
               r"ELECTRONICS & DEFENSE AVIONICS USA LLC|VENTILATION SYSTEMS USA LLC|LANDING SYSTEMS.*|"
               r"OIL SYSTEMS LLC|CABIN MATERIALS LLC|POWER (UK LTD|USA LLC)|SENSING TECHNOLOGIES NORWAY AS|"
               r"USA INC|OPTICS 1 INC|ELECTRONICS & DEFENSE.*|HELICOPTER ENGINES.*|AIRCRAFT ENGINES.*)"],
    "thales": [r"THALES (SA|S A|USA INC|AEROSPACE COMMUNICATIONS FULLERTON INC|VISIONIX INC|DEFENSE & SECURITY INC|"
               r"AVIONICS INC.*|DEUTSCHLAND GMBH|DEFENCE & SECURITY SYSTEMS GMBH|AVS FRANCE SAS|AUSTRALIA LIMITED|"
               r"TRAINING & SIMULATION L.*|LAS FRANCE SAS|ALENIA SPACE .+|OPTRONICS LTD|BELGIUM|COMPONENTS CORP|"
               r"COMMUNICATIONS INC|E SECURITY INC|DIS USA INC)"],
    # Stellantis formed Jan 2021 (FCA + PSA); FCA US / Chrysler Group records are its US predecessor.
    "stellantis": [r"STELLANTIS N ?V", r"FCA US LLC", r"CHRYSLER GROUP LLC( THE)?", r"FIAT CHRYSLER AUTOMOBILES N V",
                   r"FIAT S ?P ?A", r"FCA INTERNATIONAL OPERATIONS LLC"],
    "shell": [r"SHELL OIL( CO(MPANY)?)?", r"SHELL ENERGY NORTH AMERICA US L ?P", r"SHELL EXPLORATION & PRODUCTION COMPANY",
              r"SHELL CHEMICAL YABUCOA INC", r"SHELL (PLC|USA INC|TRADING .+|INTERNATIONAL .+)",
              r"ROYAL DUTCH SHELL PLC", r"SHELL DEUTSCHLAND GMBH"],
    "ing": [r"ING (BANK .+|GROEP N V|CAPITAL LLC|CAPITAL MANAGEMENT INC)"],
    "asml": [r"ASML (US LLC|HOLDING N V|NETHERLANDS B V)", r"CYMER LLC"],
    "philips": [r"KONINKLIJKE PHILIPS (N V|ELECTRONICS N V)", r"PHILIPS (NORTH AMERICA LLC|ELECTRONICS NORTH AMER(ICA CORP)?|"
                r"MEDICAL SYSTEMS .+|HEALTHCARE INFORMATICS INC|HEALTHCARE LLC|RS NORTH AMERICA LLC|"
                r"ELECTRONICS NEDERLAND B V|ULTRASOUND (INC|LLC))", r"RESPIRONICS( NOVAMETRIX LLC| INC)"],
    "unilever": [r"UNILEVER (PLC|N V|UNITED STATES INC)", r"CONOPCO INC",
                 r"STICHTING ADMINISTRATIEKANTOOR UNILEVER N V"],
    "heineken": [r"HEINEKEN (N V|USA INC(ORPORATED)?)"],
    "ahold": [r"KONINKLIJKE AHOLD DELHAIZE N V", r"DELHAIZE AMERICA (INC|LLC)", r"AHOLD (USA|DELHAIZE USA) INC",
              r"STOP & SHOP SUPERMARKET COMPANY LLC( THE)?", r"HANNAFORD BROS CO( LLC)?", r"FOOD LION LLC",
              r"GEBROEDERS DELHAIZE EN CIE DE LEEUW NV"],
    "prosus": [r"PROSUS N V", r"NASPERS LIMITED"],
    "bp": [r"BP P ?L ?C", r"BP (PRODUCTS NORTH AMERICA INC|EXPLORATION (ALASKA INC|& OIL INC)|OIL INTERNATIONAL LIMITED|"
           r"ENERGY (INC|COMPANY)|AMERICA INC|CORPORATION NORTH AMERICA INC)"],
    "astrazeneca": [r"ASTRAZENECA (UK LTD|LP|PLC|PHARMACEUTICALS LP|AB)", r"MEDIMMUNE( INC| LLC| BIOLOGICS INC)"],
    "gsk": [r"GLAXOSMITHKLINE (LLC|PLC|BIOLOGICALS( S A| SA)?)", r"GSK (PLC|LLC)", r"VIIV HEALTHCARE COMPANY"],
    "hsbc": [r"HSBC (HOLDINGS PLC|BANK USA( N ?A)?|BANK PLC|USA INC|SECURITIES USA INC|BUSINESS CREDIT USA INC|"
             r"MORTGAGE CORPORATION|BANK EGYPT S A E)"],
    "barclays": [r"BARCLAYS (PLC|BANK PLC|CAPITAL INC|BANK DELAWARE)"],
    "bae": [r"BAE SYSTEMS( .+)?"],
    "bat": [r"BRITISH AMERICAN TOBACCO P ?L ?C", r"REYNOLDS AMERICAN INC", r"R ?J REYNOLDS TOBACCO (COMPANY|CO)"],
    "riotinto": [r"RIO TINTO (PLC|LIMITED|SERVICES INC)", r"KENNECOTT UTAH COPPER LLC"],
    "vodafone": [r"VODAFONE (GROUP PUBLIC LIMITED COMPANY|GROUP PLC|GLOBAL ENTERPRISE LTD|GLOBAL ENTERPRISE INC|"
                 r"ITALIA S P A|ESPANA SAU|GMBH|PORTUGAL .+|EGYPT .+|PANAFON .+|TELEKOMUNIKASYON .+|"
                 r"INTERNATIONAL SERVICES)"],
    "nestle": [r"NESTLE? S A", r"NESTLE (USA INC|HEALTHCARE NUTRITION INC|PURINA PETCARE CO(MPANY)?|"
               r"WATERS NORTH AMERICA INC)", r"GERBER PRODUCTS COMPANY"],
    # Sandoz was spun off in Oct 2023; the two Novartis foundations are SAM-reported top owners of Novartis units.
    "novartis": [r"NOVARTIS (AG|PHARMACEUTICALS CORPORATION|INNOVATIVE TECHNOLOGIES INC|VACCINES (&|AND) DIAGNOSTICS INC|"
                 r"INSTITUTES FOR BIOMEDICAL RESEARCH INC|SANIERUNGSSTIFTUNG|FORSCHUNGSSTIFTUNG.*)", r"SANDOZ INC"],
    "roche": [r"ROCHE (HOLDING AG|DIABETES CARE INC|DIAGNOSTICS .+|MOLECULAR SYSTEMS INC|PALO ALTO LLC|PRODUCTS LTD|"
              r"SEQUENCING SOLUTIONS INC)", r"GENENTECH INC", r"F HOFFMANN LA ROCHE( AG| LTD)"],
    "ubs": [r"UBS (AG|GROUP AG|FINANCIAL SERVICES INC|SECURITIES LLC)"],
    "equinor": [r"EQUINOR (ASA|US HOLDINGS INC|WIND US LLC)", r"STATOIL (ASA|USA .+)"],
    # The Novo Nordisk Foundation is the SAM-reported ultimate owner of Novo Nordisk (it also controls Novonesis).
    "novonordisk": [r"NOVO NORDISK (INC|A S|FONDEN|PHARMACEUTICAL INDUSTRIES LP)"],
    "ericsson": [r"ERICSSON (INC|AB|FEDERAL TECHNOLOGIES GROUP INC|ENTERPRISE WIRELESS SOLUTIONS INC)",
                 r"TELEFON AB L ?M ERICSSON",
                 r"TELEFONAKTIEBOLAGET L ?M ERICSSON"],
    "nokia": [r"NOKIA (OYJ|OF AMERICA CORPORATION|FEDERAL SOLUTIONS (INC|LLC)|AMERICA INC|SOLUTIONS AND NETWORKS .+|"
              r"SIEMENS NETWORKS (B V|USA LLC))", r"ALCATEL LUCENT USA INC"],
    "iberdrola": [r"IBERDROLA (SOCIEDAD ANONIMA|S A|SA|RENEWABLES .+)", r"AVANGRID( INC| RENEWABLES LLC)?"],
    "santander": [r"BANCO SANTANDER( S A| SA)?", r"SANTANDER (BANK N ?A|BANK NATIONAL ASSOCIATION|HOLDINGS USA INC|"
                  r"CONSUMER USA INC)"],
    "enel": [r"ENEL (SPA|S P A|X NORTH AMERICA INC|X ADVISORY SERVICES USA LLC|GREEN POWER NORTH AMERICA INC|"
             r"ENERGIA S P A|COLOMBIA S A E S P)"],
    "eni": [r"ENI (S P A|SPA|US OPERATING CO INC|PETROLEUM CO INC|TRADING & SHIPPING .+)"],
    "spotify": [r"SPOTIFY (USA INC|AB|TECHNOLOGY S A)"],
}


# Parent records that share a UEI/DUNS with an accepted record but carry another name.
# Each was checked year by year; only these are summed.
SHARED_ID_OK = {
    "rtx": {"ROCKWELL COLLINS AUSTRALIA PTY LIMITED":
            "SAM artifact: RTX's DUNS; holds RTX's FY2023-25 obligations ($23bn in FY2024)"},
    "honeywell": {"RESIDEO TECHNOLOGIES INC":
                  "SAM artifact: parent of Honeywell Federal Manufacturing & Technologies (NNSA Kansas City) "
                  "FY2022-25; Resideo itself is a 2018 Honeywell spin-off"},
    "jnj": {"JOHNSON & JOHNSON HEALTHCARE": "J&J US commercial entity"},
    "alphabet": {"MANDIANT INC": "acquired by Google Sep 2022; obligations only FY2023+"},
    "safran": {"SAFRAN": "Safran SA under a short name"},
    "thales": {"THALES": "Thales SA under a short name"},
}
# Rejected after review: BALL CORP (shares UEI with BAE Systems Space & Mission Systems,
# but holds Ball Aerospace's pre-Feb-2024 awards, before BAE owned it), UNILINE D.O.O.,
# WYOMING BEVERAGES INC, HAGEDORN RESEARCH INSTITUTE (zero).


def norm(s):
    s = (s or "").upper().replace("É", "E")
    return re.sub(r"[^A-Z0-9&]+", " ", s).strip()


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def search(keyword):
    """All recipient-profile records whose name contains `keyword` (max 5 pages of 1000)."""
    res, page = [], 1
    while True:
        p = OUT / "recipient_search" / f"{slug(keyword)}_p{page}.json"
        d = json.loads(cached(p, lambda: get(f"{API}/recipient/",
                                             data={"keyword": keyword, "limit": 1000, "page": page})))
        res += d["results"]
        if not d["page_metadata"].get("hasNext") or page >= 5:
            return res
        page += 1


def profile(rid):
    p = OUT / "recipient_profile" / f"{rid}.json"
    return json.loads(cached(p, lambda: get(f"{API}/recipient/{rid}/")))


def over_time(rid):
    p = OUT / "spending_over_time" / f"{rid}.json"
    body = {"group": "fiscal_year",
            "filters": {"recipient_id": rid,
                        "time_period": [{"start_date": f"{FY0 - 1}-10-01", "end_date": f"{FY1}-09-30"}]}}
    d = json.loads(cached(p, lambda: get(f"{API}/search/spending_over_time/", data=body)))
    return {int(r["time_period"]["fiscal_year"]): r for r in d["results"]}


def match_company(cid):
    """Return (accepted, flagged): dicts recipient_id -> info.

    Seeds are P/R search hits whose name fully matches an alias. Then the
    profile of every alias-named record (any level) and of every accepted
    parent is read, and each parent listed in its `parents` history is
      * accepted if the parent's name matches an alias, or
      * accepted if it carries the same UEI or DUNS as an already accepted
        record AND it was reviewed and listed in SHARED_ID_OK (SAM sometimes
        files a group's awards under a mis-named parent record with the
        group's DUNS, e.g. RTX FY2023+ under a record named "ROCKWELL COLLINS
        AUSTRALIA PTY LIMITED" with RTX's DUNS 001344142); unreviewed ones are
        flagged as shares_uei_or_duns_rejected,
      * otherwise flagged (written to the matches file, not summed).
    """
    pats = [re.compile(p) for p in ACCEPT.get(cid, [])]
    ok = lambda name: any(p.fullmatch(norm(name)) for p in pats)  # noqa: E731
    recs = {}
    for k in SEARCH.get(cid, []):
        for r in search(k):
            recs[r["id"]] = r
    accepted, flagged, ids = {}, {}, set()

    def accept(rid, name, uei, duns, method, note=""):
        accepted[rid] = {"recipient_name": name, "uei": uei or "", "level": rid[-1],
                         "match_method": method, "note": note}
        ids.update(x for x in (uei, duns) if x)

    hits = [r for r in recs.values() if ok(r["name"])]
    for r in hits:
        if r["recipient_level"] in ("P", "R"):
            accept(r["id"], r["name"], r.get("uei"), r.get("duns"), "alias_fullmatch")
    todo = [r["id"] for r in hits]
    seen = set()
    for _ in range(4):  # iterate until no new parents are accepted
        before = len(accepted)
        todo = list(dict.fromkeys(todo + list(accepted)))
        for rid in todo:
            if rid in seen:
                continue
            seen.add(rid)
            prof = profile(rid)
            if rid in accepted:
                ids.update(x for x in (prof.get("uei"), prof.get("duns")) if x)
            for par in prof.get("parents") or []:
                pid = par.get("parent_id")
                if not pid or pid in accepted:
                    continue
                if ok(par.get("parent_name")):
                    accept(pid, par["parent_name"], par.get("parent_uei"), par.get("parent_duns"),
                           "parent_name_alias_fullmatch", f"listed as parent of {prof.get('name')}")
                elif ((par.get("parent_uei") in ids) or (par.get("parent_duns") in ids)) and \
                        norm(par.get("parent_name")) in SHARED_ID_OK.get(cid, {}):
                    accept(pid, par.get("parent_name"), par.get("parent_uei"), par.get("parent_duns"),
                           "parent_shares_uei_or_duns",
                           f"listed as parent of {prof.get('name')}; same UEI/DUNS as an accepted record; "
                           + SHARED_ID_OK[cid][norm(par.get("parent_name"))])
                elif (par.get("parent_uei") in ids) or (par.get("parent_duns") in ids):
                    flagged.setdefault(pid, {"recipient_name": par.get("parent_name"),
                                             "uei": par.get("parent_uei") or "", "level": "P",
                                             "match_method": "shares_uei_or_duns_rejected",
                                             "note": f"parent of {prof.get('name')} ({rid}) with shared UEI/DUNS; "
                                                     "rejected after review; not summed"})
                else:
                    flagged.setdefault(pid, {"recipient_name": par.get("parent_name"),
                                             "uei": par.get("parent_uei") or "", "level": "P",
                                             "match_method": "unmatched_parent_of_alias_record",
                                             "note": f"parent of {prof.get('name')} ({rid}); not summed"})
        todo = []
        if len(accepted) == before:
            break
    flagged = {k: v for k, v in flagged.items() if k not in accepted}
    return accepted, flagged


def main():
    only = set(sys.argv[1:])
    cos = [c for c in companies() if not only or c["company_id"] in only]
    rows, matches = [], []
    for c in cos:
        cid = c["company_id"]
        accepted, flagged = match_company(cid)
        per_fy = {fy: {"contract": 0.0, "idv": 0.0, "grant": 0.0} for fy in range(FY0, FY1 + 1)}
        for i, info in sorted(accepted.items(), key=lambda kv: kv[1]["recipient_name"] or ""):
            ot = over_time(i)
            tot = 0.0
            for fy in range(FY0, FY1 + 1):
                x = ot.get(fy)
                if not x:
                    continue
                per_fy[fy]["contract"] += x["Contract_Obligations"] or 0
                per_fy[fy]["idv"] += x["Idv_Obligations"] or 0
                per_fy[fy]["grant"] += x["Grant_Obligations"] or 0
                tot += x["Contract_Obligations"] or 0
            matches.append({"company_id": cid, "recipient_id": i, **info,
                            "contracts_fy2015_2025_usd": round(tot, 2)})
        for i, info in flagged.items():
            matches.append({"company_id": cid, "recipient_id": i, **info, "contracts_fy2015_2025_usd": ""})
        for fy, v in per_fy.items():
            rows.append({"company_id": cid, "fiscal_year": fy,
                         "contract_obligations_usd": round(v["contract"], 2),
                         "idv_obligations_usd": round(v["idv"], 2),
                         "grants_usd": round(v["grant"], 2),
                         "n_recipient_records": len(accepted), "n_contract_awards": ""})
        print(f"{cid:16} records={len(accepted):3} flagged={len(flagged):3} "
              f"FY2024 contracts=${per_fy[2024]['contract'] / 1e6:12,.1f}M", flush=True)
    if only:
        return
    write_csv(CLEAN / "us_contracts.csv", rows,
              ["company_id", "fiscal_year", "contract_obligations_usd", "idv_obligations_usd", "grants_usd",
               "n_recipient_records", "n_contract_awards"])
    write_csv(CLEAN / "us_contracts_matches.csv", matches,
              ["company_id", "recipient_name", "recipient_id", "uei", "level", "match_method",
               "contracts_fy2015_2025_usd", "note"])


if __name__ == "__main__":
    main()
