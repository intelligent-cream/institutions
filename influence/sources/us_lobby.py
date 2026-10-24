"""US federal lobbying (Lobbying Disclosure Act LD-2 quarterly reports), 2015-2025.

Source
------
Every LD-2 is filed once and published by both chambers. The Senate's REST API
(https://lda.gov/api/v1/) allows anonymous clients 15 requests/minute at 25 rows per
page (120/min with a free key), which makes per-client paging of ~110 companies x 11 years
slow. The House Clerk publishes the *same* filings as bulk XML, one zip per quarter
(https://disclosurespreview.house.gov/data/LD/<year>_<n>Quarter_XML.zip, ~40 MB each,
regenerated as amendments arrive), so this module uses the bulk files as the primary
source and the lda.gov API only for an optional spot check (`--api-check`, honours
`LDA_API_KEY`).

Method
------
* One "report" = latest version of a registrant-client-quarter (key: senateID =
  "<registrant id>-<client id>", year, quarter). Originals (Q1..Q4), amendments (1A..4A),
  terminations (1T..4T) and termination amendments (1@..4@) all compete; the latest
  signedDate wins, so amendments replace rather than add.
* Clients are matched to companies with explicit, hand-verified alias regexes (ALIASES,
  full-match on a normalised client name). Trade associations, coalitions, foundations
  etc. are excluded even if they carry a company name. Every matched client name is
  written to us_lobby_matches.csv.
* Spend (OpenSecrets convention): an in-house ("self-filing") registrant reports
  *expenses*, which under the LDA must cover all its lobbying costs including fees paid
  to outside lobbying firms; an outside firm reports *income* from the client. Per
  client entity and quarter: if the entity self-filed, its expenses are the spend and
  outside firms' income for that same entity is treated as already included; otherwise
  the outside firms' income is summed. Subcontracted engagements (client "X on behalf
  of Y") are excluded from spend (the prime firm's income already includes them) but do
  count as activity. us_lobby_spend_sum_usd gives the naive in-house + outside sum as an
  upper bound.
"""

import argparse
import collections
import datetime as dt
import functools
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ProcessPoolExecutor

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

YEARS = range(2015, 2026)
QUARTERS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
HOUSE_BASE = "https://disclosurespreview.house.gov/data/LD/"
LDA_API = "https://lda.gov/api/v1/"
OUT = RAW / "us_lobby"
ZIPDIR = OUT / "house_ld2"
PARSED = OUT / "parsed"
PARSE_VERSION = 2


# ---------------------------------------------------------------------------
# Download + parse
# ---------------------------------------------------------------------------

def zip_path(year, q):
    return ZIPDIR / f"{year}_{QUARTERS[q]}Quarter_XML.zip"


def download_all(years=YEARS):
    cached(OUT / "LdSearchPastFilings.json", lambda: get(HOUSE_BASE + "LdSearchPastFilings.json"))
    for y in years:
        for q in QUARTERS:
            p = zip_path(y, q)
            if not p.exists():
                print(f"  downloading {p.name}", file=sys.stderr)
            cached(p, lambda p=p: get(HOUSE_BASE + p.name, retries=5, pause=5))


def _t(el, tag):
    v = el.findtext(tag)
    return v.strip() if v else ""


def _amount(s):
    s = (s or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _signed(s):
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).isoformat()
        except ValueError:
            pass
    return ""


def parse_filing(name, raw):
    r = ET.fromstring(raw)
    org = _t(r, "organizationName") or " ".join(x for x in (_t(r, "firstName"), _t(r, "lastName")) if x)
    issues, lobbyists = [], {}
    for ali in r.iter("ali_info"):
        code = _t(ali, "issueAreaCode")
        desc = " ".join((d.text or "").strip() for d in ali.iter("description"))
        issues.append([code, desc])
        for lb in ali.iter("lobbyist"):
            fn, ln = _t(lb, "lobbyistFirstName"), _t(lb, "lobbyistLastName")
            if not (fn or ln):
                continue
            key = f"{fn} {ln}".strip()
            cov = _t(lb, "coveredPosition")
            if cov.upper() in ("N/A", "NA", "NONE", "N\\A", "-"):
                cov = ""
            if key not in lobbyists or (cov and not lobbyists[key]):
                lobbyists[key] = cov
    return {
        "doc": name.rsplit("/", 1)[-1].split(".")[0],
        "registrant": org,
        "client": _t(r, "clientName"),
        "senate_id": _t(r, "senateID"),
        "house_id": _t(r, "houseID"),
        "year": _t(r, "reportYear"),
        "type": _t(r, "reportType"),
        "self": _t(r, "selfSelect"),
        "no_lobbying": _t(r, "noLobbying"),
        "income": _amount(_t(r, "income")),
        "expenses": _amount(_t(r, "expenses")),
        "expenses_method": _t(r, "expensesMethod"),
        "signed": _signed(_t(r, "signedDate")),
        "issues": issues,
        "lobbyists": [[k, v] for k, v in lobbyists.items()],
    }


def parse_zip(path):
    """Parse one quarterly zip to a list of compact dicts, cached as gzipped JSON."""
    out = PARSED / (path.stem + ".json.gz")
    st = path.stat()
    stamp = f"v{PARSE_VERSION}-{st.st_size}-{int(st.st_mtime)}"
    if out.exists():
        with gzip.open(out, "rt", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("stamp") == stamp:
            return d["filings"]
    recs, bad = [], 0
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not n.lower().endswith(".xml"):
                continue
            try:
                recs.append(parse_filing(n, z.read(n)))
            except ET.ParseError:
                bad += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "bad_xml": bad, "filings": recs}, f)
    print(f"  parsed {path.name}: {len(recs)} filings ({bad} unparseable)", file=sys.stderr)
    return recs




# ---------------------------------------------------------------------------
# Client -> company aliases (hand-written, verified against the bulk client names)
# ---------------------------------------------------------------------------
# Each entry: (entity label, core regex, first quarter, last quarter). The core is
# full-matched against the normalised client name followed by optional legal-form
# tokens (INC, LLC, CORP, ...) and an optional "formerly/fka/dba/and affiliates ..." tail.
# Quarter bounds ("2018Q3") restrict subsidiaries to the period they belonged to the
# group (acquisitions / spin-offs); None = unbounded.

def A(entity, core, start=None, end=None):
    return (entity, core, start, end)


ALIASES = {
    "alphabet": [A("Google", r"GOOGLE( CLIENT SERVICES| SERVICES| CLOUD| PUBLIC SECTOR)?"),
                 A("Waymo", r"WAYMO"), A("Verily", r"VERILY LIFE SCIENCES"),
                 A("DeepMind", r"DEEPMIND TECHNOLOGIES"), A("Wing", r"WING( AVIATION)?"),
                 A("X", r"X GOOGLE INC")],
    "meta": [A("Meta", r"META PLATFORMS|META PLATCORMS|FACEBOOK")],
    "microsoft": [A("Microsoft", r"MICROSOFT"), A("LinkedIn", r"LINKEDIN", "2017Q1"),
                  A("Activision Blizzard", r"ACTIVISION BLIZZARD", "2023Q4")],
    "apple": [A("Apple", r"APPLE")],
    "amazon": [A("Amazon", r"AMAZON( COM)?( SERVICES| CORPORATE| WEB SERVICES| PRIME AIR)?|AWS PUBLIC POLICY AMERICAS"),
               A("PillPack", r"PILLPACK( AN AMAZON COMPANY)?"), A("Zoox", r"ZOOX"),
               A("Whole Foods", r"WHOLE FOODS MARKET", "2017Q4"), A("One Medical", r"ONE MEDICAL", "2023Q1")],
    "oracle": [A("Oracle", r"ORACLE( AMERICA| AMERICAN| CERNER)?"),
               A("Cerner", r"CERNER( GOVERNMENT SERVICES)?", "2022Q3")],
    "ibm": [A("IBM", r"IBM|INTERNATIONAL BUSINESS MACHINES|INTERNATIONAL BUSINESS MACHINES CORPORATION IBM"),
            A("Red Hat", r"RED HAT", "2019Q3")],
    "intel": [A("Intel", r"INTEL")],
    "qualcomm": [A("Qualcomm", r"QUALCOMM")],
    "nvidia": [A("Nvidia", r"NVIDIA")],
    "cisco": [A("Cisco", r"CISCO( SYSTEMS)?")],
    "uber": [A("Uber", r"UBER TECHNOLOGIES"), A("Postmates", r"POSTMATES", "2021Q1")],
    "airbnb": [A("Airbnb", r"AIRBNB")],
    "pfizer": [A("Pfizer", r"PFIZER"), A("Hospira", r"HOSPIRA", "2015Q4"), A("Seagen", r"SEAGEN", "2024Q1")],
    "jnj": [A("Johnson & Johnson", r"JOHNSON AND JOHNSON( SERVICES| VISION)?"),
            A("Actelion", r"ACTELION PHARMACEUTICALS US", "2017Q3"), A("Abiomed", r"ABIOMED", "2023Q1")],
    "merck": [A("Merck & Co", r"MERCK( AND CO| AND COMPANY)?|MERCK SHARP AND DOHME|MERCK ANIMAL HEALTH")],
    "abbvie": [A("AbbVie", r"ABBVIE"), A("Allergan", r"ALLERGAN( USA)?", "2020Q2")],
    "lilly": [A("Eli Lilly", r"ELI LILLY( AND)?|LILLY USA")],
    "bms": [A("Bristol-Myers Squibb", r"BRISTOL MYERS SQUIBB"), A("Celgene", r"CELGENE", "2019Q4"),
            A("Karuna", r"KARUNA THERAPEUTICS", "2024Q2")],
    "amgen": [A("Amgen", r"AMGEN( USA)?"), A("Horizon", r"HORIZON THERAPEUTICS( USA)?", "2023Q4")],
    "gilead": [A("Gilead", r"GILEAD( SCIENCES)?"), A("Kite", r"KITE PHARMA", "2017Q4")],
    "exxon": [A("ExxonMobil", r"EXXON ?MOBIL|EXXON MOBIL COPORATION"),
              A("Pioneer Natural Resources", r"PIONEER NATURAL RESOURCES", "2024Q2")],
    "chevron": [A("Chevron", r"CHEVRON( U S A| USA| U S A)?"), A("Noble Energy", r"NOBLE ENERGY", "2020Q4"),
                A("Hess", r"HESS", "2025Q3")],
    "conocophillips": [A("ConocoPhillips", r"CONOCOPHILLIPS"), A("Marathon Oil", r"MARATHON OIL", "2025Q1")],
    "jpmorgan": [A("JPMorgan Chase", r"(JPMORGAN|J P MORGAN|JP MORGAN)( CHASE)?( AND| HOLDINGS| BANK)?")],
    "goldman": [A("Goldman Sachs", r"GOLDMAN SACHS( GROUP| AND)?")],
    "morganstanley": [A("Morgan Stanley", r"MORGAN STANLEY( AND| DEAN WITTER AND)?")],
    "bofa": [A("Bank of America", r"BANK OF AMERICA|MERRILL LYNCH")],
    "citi": [A("Citigroup", r"CITIGROUP( WASHINGTON| MANAGEMENT| GLOBAL MARKETS)?|CITIGROUP WASHINGTON INC CWI|CITIBANK")],
    "blackrock": [A("BlackRock", r"BLACKROCK( FUNDS? SERVICES( GROUP)?| CAPITAL MANAGEMENT)?")],
    "visa": [A("Visa", r"VISA( U S A| USA)?")],
    "mastercard": [A("Mastercard", r"MASTERCARD( WORLDWIDE| INTERNATIONAL)?")],
    "boeing": [A("Boeing", r"BOEING( DEFENSE SPACE AND SECURITY)?"),
               A("Aurora Flight Sciences", r"AURORA FLIGHT SCIENCES", "2017Q4")],
    "lockheed": [A("Lockheed Martin", r"LOCKHEED MARTIN( AERONAUTIC SECTOR| CORPORORATION| CORPORATION QTC HOLDINGS)?"),
                 A("Sikorsky", r"SIKORSKY AIRCRAFT", "2015Q4")],
    "rtx": [A("RTX", r"RTX|RAYTHEON TECHNOLOGIES|UNITED TECHNOLOGIES|COLLINS AEROSPACE|PRATT AND WHITNEY"),
            A("Raytheon Co", r"RAYTHEON", "2020Q2"), A("Rockwell Collins", r"ROCKWELL COLLINS", "2018Q4")],
    "generaldynamics": [A("General Dynamics", r"GENERAL DYNAMICS( LAND SYSTEMS| INFORMATION TECHNOLOGY)?|GULFSTREAM AEROSPACE|ELECTRIC BOAT"),
                        A("CSRA", r"CSRA", "2018Q2")],
    "northrop": [A("Northrop Grumman", r"NORTHROP GRUMMAN( SYSTEMS)?"), A("Orbital ATK", r"ORBITAL ATK", "2018Q3")],
    "ge": [A("General Electric", r"GENERAL ELECTRIC|GE AVIATION|GE AEROSPACE"),
           A("GE HealthCare", r"GE HEALTHCARE( TECHNOLOGIES| PHARMACEUTICAL DIAGNOSTICS)?", None, "2022Q4"),
           A("GE Vernova", r"GE VERNOVA( INTERNATIONAL)?", None, "2024Q1")],
    "honeywell": [A("Honeywell", r"HONEYWELL( INTERNATIONAL)?|INTELLIGRATED SYSTEMS")],
    "3m": [A("3M", r"3M( HEALTH INFORMATION SYSTEMS| COGENT)?")],
    "caterpillar": [A("Caterpillar", r"CATERPILLAR")],
    "ford": [A("Ford", r"FORD MOTOR")],
    "gm": [A("General Motors", r"GENERAL MOTORS|GM DEFENSE"), A("Cruise", r"CRUISE")],
    "tesla": [A("Tesla", r"TESLA( MOTORS)?")],
    "cocacola": [A("Coca-Cola", r"COCA ?COLA")],
    "pepsico": [A("PepsiCo", r"PEPSICO")],
    "pg": [A("Procter & Gamble", r"PROCTER AND GAMBLE")],
    "pmi": [A("Philip Morris International", r"PMI GLOBAL SERVICES|PMI US CORPORATE SERVICES|PHILIP MORRIS INTERNATIONAL"),
            A("Swedish Match", r"SWEDISH MATCH NORTH AMERICA", "2023Q1")],
    "altria": [A("Altria", r"ALTRIA( CLIENT SERVICES)?|PHILIP MORRIS USA"), A("NJOY", r"NJOY", "2023Q3")],
    "walmart": [A("Walmart", r"WAL ?MART( STORES)?")],
    "mcdonalds": [A("McDonald's", r"MCDONALDS")],
    "att": [A("AT&T", r"AT AND T( SERVICES| CORP| CORPORATION)?"),
            A("DIRECTV", r"DIRECTV( GROUP)?", "2015Q3", "2021Q2"),
            A("WarnerMedia", r"TIME WARNER|WARNER ?MEDIA( GROUP)?", "2018Q3", "2022Q1")],
    "verizon": [A("Verizon", r"VERIZON( COMMUNICATIONS| SERVICES)?"),
                A("Oath/Yahoo", r"OATH", "2017Q3", "2021Q3"), A("TracFone", r"TRACFONE WIRELESS", "2021Q4")],
    "comcast": [A("Comcast", r"COMCAST( NBCUNIVERSAL)?|NBC ?UNIVERSAL( MEDIA)?")],
    "dow": [A("Dow", r"DOW CHEMICAL( COMPANY)?|DOW|DOWDUPONT"),
            A("Dow AgroSciences", r"DOW AGRO ?SCIENCES", None, "2019Q2")],
    "dupont": [A("DuPont", r"DUPONT DE NEMOURS|DUPONT( SPECIALTY PRODUCTS( USA)?| USA)?"),
               A("E. I. du Pont", r"E I DU ?PONT DE ?NEMOURS( AND)?", None, "2019Q2")],
    "corteva": [A("Corteva", r"CORTEVA( AGRISCIENCE)?"),
                A("E. I. du Pont", r"E I DU ?PONT DE ?NEMOURS( AND)?", "2019Q3"),
                A("Dow AgroSciences", r"DOW AGRO ?SCIENCES", "2019Q3")],
    "siemens": [A("Siemens", r"SIEMENS( CORPORATIONS?| MEDICAL SOLUTIONS USA| HEALTHINEERS| GOVERNMENT TECHNOLOGIES| RAIL AUTOMATION)?"),
                A("Siemens Energy", r"SIEMENS ENERGY|SIEMENS GAMESA RENEWABLE ENERGY", None, "2020Q3")],
    "basf": [A("BASF", r"BASF( CATALYSTS DIVISION)?")],
    "bayer": [A("Bayer", r"BAYER( U S| US| CROPSCIENCE)?"), A("Monsanto", r"MONSANTO", "2018Q3")],
    "volkswagen": [A("Volkswagen", r"VOLKSWAGEN GROUP OF AMERICA|VOLKSWAGEN|AUDI OF AMERICA"),
                   A("Electrify America", r"ELECTRIFY AMERICA"),
                   A("Porsche", r"PORSCHE CARS( OF)? NORTH AMERICA"),
                   A("Navistar", r"NAVISTAR( DEFENSE)?|INTERNATIONAL MOTORS", "2021Q3")],
    "bmw": [A("BMW", r"BMW( OF)? NORTH AMERICA|BMW MANUFACTURING")],
    "mercedes": [A("Mercedes-Benz", r"DAIMLER MERCEDES BENZ USA|MERCEDES BENZ( USA| NORTH AMERICA)?"),
                 A("Daimler Truck", r"DAIMLER TRUCKS? NORTH AMERICA", None, "2021Q4")],
    "dtelekom": [A("T-Mobile US", r"T MOBILE( USA| US)?|DEUTSCHE TELEKOM"), A("Sprint", r"SPRINT", "2020Q2")],
    "sap": [A("SAP", r"SAP( AMERICA| NATIONAL SECURITY SERVICES( NS2)?)?|CONCUR TECHNOLOGIES"),
            A("Qualtrics", r"QUALTRICS( INTERNATIONAL)?", "2019Q1", "2023Q2")],
    "allianz": [A("Allianz", r"AZOA SERVICES CORP( FIREMANS FUND)? ALLIANZ OF AMERICA( AND AFFILIATES)?"
                             r"|ALLIANZ( OF AMERICA( CORPORATION)?| LIFE INSURANCE COMPANY OF NORTH AMERICA"
                             r"| GLOBAL CORPORATE AND SPECIALTY| CAPITAL PARTNERS GMBH| REAL ESTATE OF AMERICA)?"
                             r"|PIMCO( FUNDS)?")],
    "deutschebank": [A("Deutsche Bank", r"DEUTSCHE BANK( SECURITIES| USA)?")],
    "airbus": [A("Airbus", r"AIRBUS( AMERICAS| GROUP| HELICOPTERS| DEFENSE AND SPACE)?")],
    "totalenergies": [A("TotalEnergies", r"TOTAL ?ENERGIES( WASHINGTON OFFICE| EP MOZAMBIQUE AREA 1)?"
                                         r"|TOTAL WASHINGTON DC REPRESENTATIVE OFFICE|TOTAL S ?A")],
    "sanofi": [A("Sanofi", r"SANOFI( US| U S)?( SERVICES)?|SANOFI PASTEUR|GENZYME"),
               A("Bioverativ", r"BIOVERATIV", "2018Q2")],
    "loreal": [A("L'Oreal", r"LOREAL USA( S D)?")],
    "lvmh": [A("LVMH", r"LVMH( MOET HENNESSY LOUIS VUITTON)?|MOET HENNESSY USA"),
             A("Tiffany", r"TIFFANY AND", "2021Q1")],
    "bnpparibas": [A("BNP Paribas", r"BNP PARIBAS( US WHOLESALE HOLDINGS| RCC)?"),
                   A("Bank of the West", r"BANK OF THE WEST", None, "2022Q4")],
    "axa": [A("AXA", r"AXA( SA)?"),
            A("AXA Financial/Equitable", r"AXA FINANCIAL( EQUITABLE LIFE ASSURANCE)?|AXA EQUITABLE LIFE INSURANCE", None, "2018Q4"),
            A("XL Group", r"XL GROUP|AXA XL", "2018Q4")],
    "danone": [A("Danone", r"DANONE( NORTH AMERICA( PUBLIC BENEFIT CORPORATION)?)?|DANONEWAVE"),
               A("WhiteWave", r"WHITEWAVE FOODS", "2017Q2")],
    "orange": [A("Orange", r"ORANGE BUSINESS SERVICES( US)?|FRANCE TELECOM")],
    "renault": [A("Renault", r"RENAULT")],
    "safran": [A("Safran", r"SAFRAN( USA| DEFENSE AND SPACE)?|OROLIA DEFENSE SYSTEMS SAFRAN FEDERAL SYSTEMS")],
    "thales": [A("Thales", r"THALES( USA| DEFENSE AND SECURITY| VISIONIX| AVS FRANCE)?")],
    "stellantis": [A("Stellantis", r"STELLANTIS|FCA US|CHRYSLER GROUP|FIAT CHRYSLER( AUTOMOBILES)?")],
    "shell": [A("Shell", r"SHELL( OIL( PRODUCTS US)?| USA| NA LNG| INTERNATIONAL| EXPLORATION AND PRODUCTION)?")],
    "ing": [A("ING", r"ING FINANCIAL HOLDINGS|ING BANK|ING GROEP")],
    "asml": [A("ASML", r"ASML( US| NETHERLANDS)?")],
    "philips": [A("Philips", r"PHILIPS( HOLDING USA| NORTH AMERICA| HEALTHCARE| ELECTRONICS NORTH AMERICA)?")],
    "unilever": [A("Unilever", r"UNILEVER( UNITED STATES| ASCC)?")],
    "heineken": [A("Heineken", r"HEINEKEN USA")],
    "ahold": [A("Ahold Delhaize", r"AHOLD DELHAIZE( USA)?|STOP AND SHOP SUPERMARKET|GIANT FOOD STORES|HANNAFORD BROS"),
              A("Food Lion", r"FOOD LION", "2016Q3")],
    "prosus": [A("Prosus", r"PROSUS( GROUP)?|NASPERS")],
    "bp": [A("BP", r"BP( AMERICA| AMERICAS| CORPORATION NORTH AMERICA| P L C)?")],
    "astrazeneca": [A("AstraZeneca", r"ASTRAZENECA( PHARMACEUTICALS| UK)?"),
                    A("Alexion", r"ALEXION PHARMACEUTICALS", "2021Q3")],
    "gsk": [A("GSK", r"GLAXOSMITHKLINE|GSK"),
            A("GSK Consumer Healthcare", r"GLAXOSMITHKLINE CONSUMER HEALTHCARE( HOLDINGS US)?", None, "2022Q2")],
    "hsbc": [A("HSBC", r"HSBC( NORTH AMERICA| TECHNOLOGY( AND)? SERVICES( USA)?| GR CORP| LATIN AMERICA HOLDINGS UK)?")],
    "barclays": [A("Barclays", r"BARCLAYS( US GPF| US GFP| BANK| CAPITAL)?")],
    "bae": [A("BAE Systems", r"BAE SYSTEMS( LAND AND ARMAMENTS| SHIP REPAIR)?")],
    "bat": [A("BAT", r"BRITISH AMERICAN TOBACCO"),
            A("Reynolds American", r"RAI SERVICES( COMPANY)?|REYNOLDS AMERICAN", "2017Q3")],
    "riotinto": [A("Rio Tinto", r"RIO TINTO( SERVICES| AMERICA HOLDINGS)?|RESOLUTION COPPER( COMPANY| MINING)?|KENNECOTT UTAH COPPER")],
    "vodafone": [A("Vodafone", r"VODAFONE( GROUP SERVICES)?")],
    "nestle": [A("Nestle", r"NESTLE( USA| PURINA PET ?CARE| HEALTHCARE NUTRITION)?|GERBER PRODUCTS"),
               A("Nestle Waters NA", r"NESTLE WATERS NORTH AMERICA", None, "2021Q1")],
    "novartis": [A("Novartis", r"NOVARTIS( CORPORATION| SERVICES| PHARMACEUTICALS)?"),
                 A("Sandoz", r"SANDOZ", None, "2023Q3"), A("Alcon", r"ALCON( VISION| LABORATORIES)?", None, "2019Q1")],
    "roche": [A("Roche", r"GENENTECH|F HOFFMANN LA ROCHE|ROCHE( DIAGNOSTICS| MOLECULAR SYSTEMS| HOLDINGS)?"),
              A("Spark Therapeutics", r"SPARK THERAPEUTICS", "2020Q1"),
              A("Foundation Medicine", r"FOUNDATION MEDICINE", "2018Q3")],
    "ubs": [A("UBS", r"UBS( AMERICAS| GROUP| AG)?"),
            A("Credit Suisse", r"CREDIT SUISSE( SECURITIES( USA)?)?", "2023Q3")],
    "equinor": [A("Equinor", r"EQUINOR( US OPERATIONS| WIND US| US HOLDINGS| GULF SERVICES)?|STATOIL( WIND US| GULF SERVICES)?")],
    "novonordisk": [A("Novo Nordisk", r"NOVO NORDISK( PHARMACEUTICALS)?")],
    "ericsson": [A("Ericsson", r"ERICSSON")],
    "nokia": [A("Nokia", r"NOKIA( OF AMERICA| USA| SOLUTIONS AND NETWORKS US)?"),
              A("Alcatel-Lucent", r"ALCATEL LUCENT USA", "2016Q1")],
    "iberdrola": [A("Iberdrola/Avangrid", r"IBERDROLA( CORPORATE SERVICES| RENEWABLES)?|AVANGRID( MANAGEMENT COMPANY)?")],
    "santander": [A("Santander", r"SANTANDER( HOLDINGS USA| CONSUMER USA| BANK)?")],
    "enel": [A("Enel", r"ENEL( GREEN POWER NORTH AMERICA| NORTH AMERICA)?")],
    "eni": [A("Eni", r"ENI( US OPERATING)?")],
    "spotify": [A("Spotify", r"SPOTIFY( USA)?")],
}

# Subcontracts that don't use an "on behalf of" phrase: firm (client) -> company_id.
EXTRA_SUBCONTRACTS = {"LIVINGSTON GROUP LLC VERIZON COMMUNICATIONS INC": "verizon"}

LEGAL = (r"INC|INCORPORATED|LLC|L L C|LP|L P|LLP|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|P L C|SE|AG|NV|N V"
         r"|SA|S A|ASA|AB|OYJ|PBC|THE|NA|N A|BV|B V|SAS|GMBH|A S")
TAIL = (r"FKA|F K A|FFA|FORMERLY|FORMALLY|PREVIOUSLY|FRA|F R A|FNA|F N A|AKA|A K A|DBA|D B A|F D B A|INCLUDING"
        r"|CONSOLIDATED|AND ITS|AND VARIOUS|AND VAR|AND AFFILIATES|AND SUBSIDIARIES|SUBSIDIARIES")
EXCLUDE = re.compile(r"\b(ASSOCIATION|ASSN|COALITION|COALILTION|COUNCIL|INSTITUTE|ALLIANCE|FOUNDATION|CHAMBER"
                     r"|FEDERATION|SOCIETY|EMPLOYEES|CREDIT UNION|RETIREES|DEALER|DEALERS|PAC)\b")
SUBKW = re.compile(r"\b(ON BEHALF OF|BEHALF OF|OBO|O B O|THROUGH)\b")


def _q(s):
    return int(s[:4]) * 10 + int(s[-1]) if s else None


_COMPILED = [(cid, ent, re.compile(rf"(?:{core})(?: (?:{LEGAL}))*(?: (?:{TAIL})\b.*)?"), _q(a), _q(b))
             for cid, entries in ALIASES.items() for ent, core, a, b in entries]


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    s = s.replace("&", " AND ").replace("'", "").replace("’", "")
    s = re.sub(r"[^A-Z0-9]+", " ", s).strip()
    return re.sub(r"^THE ", "", s)


def _direct(n):
    """[(company_id, entity, start, end)] whose alias full-matches normalised name n."""
    return [(cid, ent, a, b) for cid, ent, rx, a, b in _COMPILED if rx.fullmatch(n)]


@functools.lru_cache(maxsize=None)
def classify(client):
    """-> (kind, [(company_id, entity, start, end)], note). kind: direct | subcontract | excluded | None."""
    n = norm(client)
    if not n:
        return None, [], ""
    if n in EXTRA_SUBCONTRACTS:
        cid = EXTRA_SUBCONTRACTS[n]
        return "subcontract", [(cid, ALIASES[cid][0][0], None, None)], "listed subcontract"
    m = SUBKW.search(n)
    if m:
        before, after = n[:m.start()].strip(), n[m.end():].strip()
        prime, benef = (after, before) if m.group(1) == "THROUGH" else (before, after)
        hits = _direct(prime)
        if hits:
            return "direct", hits, f"company is principal ('{m.group(1).lower()}' {benef[:40]})"
        hits = _direct(benef)
        if hits:
            return "subcontract", hits, f"subcontract via {prime[:60]}"
        return None, [], ""
    hits = _direct(n)
    if hits and EXCLUDE.search(n.replace("FOUNDATION MEDICINE", "")):
        return "excluded", hits, "trade association / coalition / foundation"
    return ("direct" if hits else None), hits, ""


# ---------------------------------------------------------------------------
# Report selection and aggregation
# ---------------------------------------------------------------------------

BILL_RX = [
    (re.compile(r"(?<![A-Za-z.'’])H\.?\s?R\.?\s?(\d{1,5})\b", re.I), "HR"),
    (re.compile(r"(?<![A-Za-z.'’])(H|S)\.?\s?(J\.?\s?|CON\.?\s?)?RES\.?\s?(\d{1,4})\b", re.I), "RES"),
    (re.compile(r"(?<![A-Za-z.'’])S\.?\s?(\d{1,4})\b(?![.,]\d)", re.I), "S"),
]


def bills_in(text):
    out = set()
    for rx, kind in BILL_RX:
        for m in rx.finditer(text or ""):
            if kind == "HR":
                out.add("HR" + str(int(m.group(1))))
            elif kind == "RES":
                mid = re.sub(r"[^A-Z]", "", (m.group(2) or "").upper())
                out.add(m.group(1).upper() + mid + "RES" + str(int(m.group(3))))
            else:
                out.add("S" + str(int(m.group(1))))
    return out


_NOT_GOV = re.compile(r"(N ?A|NONE|NO|NOT APPLICABLE|PARTNER|PRINCIPAL|OWNER|PRESIDENT|CONSULTANT|LOBBYIST"
                      r"|LEGISLATIVE CONSULTANT|GOVERNMENT RELATIONS|COUNSEL|ATTORNEY|N A N A)?")


def covered_ok(cov):
    """A real covered-position entry (not blank, 'N/A', or a private-sector job title)."""
    n = norm(cov)
    return len(n) >= 3 and not _NOT_GOV.fullmatch(n)


def lobbyist_key(name):
    return re.sub(r"[^A-Z ]", "", norm(name)).strip()


def _scan_zip(path):
    """Worker: parse one zip, keep reports whose client matches a company; collect covered positions."""
    recs = parse_zip(path)
    y0 = int(path.name[:4])
    q0 = int(path.name[5])
    kept, covered = [], {}
    for r in recs:
        ry = int(r["year"]) if r["year"].isdigit() else y0
        t = r["type"]
        rq = int(t[1]) if t[:1] == "Q" and t[1:2].isdigit() else (int(t[0]) if t[:1].isdigit() else q0)
        reg_id = r["senate_id"].split("-")[0] if r["senate_id"] else norm(r["registrant"])
        for name, cov in r["lobbyists"]:
            if covered_ok(cov):
                k = (reg_id, lobbyist_key(name))
                covered[k] = min(covered.get(k, 9999), ry)
        kind, hits, note = classify(r["client"])
        if not hits:
            continue
        r = dict(r, year=ry, quarter=rq, reg_id=reg_id, kind=kind, note=note, hits=hits)
        kept.append(r)
    return kept, covered


def _version(r):
    return (r["signed"], int(r["doc"]) if r["doc"].isdigit() else 0)


def collect(years=YEARS):
    paths = [zip_path(y, q) for y in years for q in QUARTERS]
    workers = max(1, min(6, os.cpu_count() or 2))
    latest, covered = {}, {}
    n_versions = 0
    with ProcessPoolExecutor(workers) as ex:
        for kept, cov in ex.map(_scan_zip, paths):
            for k, v in cov.items():
                covered[k] = min(covered.get(k, 9999), v)
            for r in kept:
                n_versions += 1
                key = (r["senate_id"] or f"{r['registrant'].upper()}|{r['client'].upper()}", r["year"], r["quarter"])
                if key not in latest or _version(r) > _version(latest[key]):
                    latest[key] = r
    reports = [r for r in latest.values() if r["year"] in years]
    print(f"  {n_versions} matched filing versions -> {len(reports)} latest-version reports", file=sys.stderr)
    return reports, covered


def assign(reports):
    """Expand reports to (company_id, entity, report) honouring ownership windows."""
    out = []
    for r in reports:
        yq = r["year"] * 10 + r["quarter"]
        seen = set()
        for cid, ent, a, b in r["hits"]:
            if (a and yq < a) or (b and yq > b) or cid in seen:
                continue
            seen.add(cid)
            out.append((cid, ent, r))
    return out


def is_inhouse(r):
    return r["self"] == "Y" or (r["expenses"] is not None and r["income"] is None)


def aggregate(assigned, covered, years=YEARS):
    by_cy = collections.defaultdict(list)
    for cid, ent, r in assigned:
        if r["kind"] in ("direct", "subcontract"):
            by_cy[(cid, r["year"])].append((ent, r))
    rows, issue_rows = [], []
    for c in companies():
        cid = c["company_id"]
        for y in years:
            items = by_cy.get((cid, y), [])
            inhouse = outside_all = outside_counted = 0.0
            methods = set()
            # spend per (entity, quarter): in-house expenses if the entity self-filed, else outside income
            cell = collections.defaultdict(lambda: {"in": 0.0, "out": 0.0})
            firms, lobbyists, former, bills, issues = set(), set(), set(), set(), collections.Counter()
            n_inh = n_sub = n_active = 0
            for ent, r in items:
                active = r["no_lobbying"] != "Y"
                n_active += active
                if r["kind"] == "subcontract":
                    n_sub += 1
                elif is_inhouse(r):
                    n_inh += 1
                    amt = r["expenses"] or 0.0
                    inhouse += amt
                    cell[(ent, r["quarter"])]["in"] += amt
                    if r["expenses_method"]:
                        methods.add(r["expenses_method"])
                else:
                    amt = r["income"] or 0.0
                    outside_all += amt
                    cell[(ent, r["quarter"])]["out"] += amt
                    if active or amt:
                        firms.add(r["reg_id"])
                for name, cov in r["lobbyists"]:
                    k = lobbyist_key(name)
                    if not k:
                        continue
                    lobbyists.add(k)
                    if covered_ok(cov) or covered.get((r["reg_id"], k), 9999) <= y:
                        former.add(k)
                codes = set()
                for code, desc in r["issues"]:
                    if code:
                        codes.add(code)
                    bills |= bills_in(desc)
                for code in codes:
                    issues[code] += 1
            spend = 0.0
            for v in cell.values():
                if v["in"] > 0:
                    spend += v["in"]
                else:
                    spend += v["out"]
                    outside_counted += v["out"]
            rows.append({
                "company_id": cid, "year": y,
                "us_lobby_spend_usd": round(spend),
                "inhouse_expenses_usd": round(inhouse),
                "outside_income_usd": round(outside_all),
                "outside_income_counted_usd": round(outside_counted),
                "us_lobby_spend_sum_usd": round(inhouse + outside_all),
                "n_filings": len(items),
                "n_filings_active": n_active,
                "n_inhouse_filings": n_inh,
                "n_subcontract_filings": n_sub,
                "n_outside_firms": len(firms),
                "n_lobbyists": len(lobbyists),
                "n_lobbyists_former_gov": len(former),
                "n_bills_mentioned": len(bills),
                "n_issue_codes": len(issues),
                "inhouse_expense_methods": "".join(sorted(methods)),
            })
            for code, n in sorted(issues.items()):
                issue_rows.append({"company_id": cid, "year": y, "issue_code": code, "n_filings": n})
    return rows, issue_rows


def match_rows(reports):
    """Audit trail: one row per company x distinct client name (incl. excluded and out-of-window)."""
    agg = {}
    for r in reports:
        yq = r["year"] * 10 + r["quarter"]
        for cid, ent, a, b in r["hits"]:
            in_window = not ((a and yq < a) or (b and yq > b))
            key = (cid, r["client"].strip().upper())
            e = agg.setdefault(key, {"entity": ent, "kind": r["kind"], "note": r["note"], "ids": set(),
                                     "years_in": set(), "years_out": set(), "n": 0, "window": (a, b)})
            e["ids"].add(r["senate_id"])
            e["n"] += 1
            (e["years_in"] if in_window else e["years_out"]).add(r["year"])
    rows = []
    for (cid, name), e in sorted(agg.items()):
        if e["kind"] == "excluded":
            method = "excluded_trade_association"
        elif not e["years_in"]:
            method = "excluded_outside_ownership_window"
        elif e["kind"] == "subcontract":
            method = "alias_regex_subcontract"
        else:
            method = "alias_regex"
        a, b = e["window"]
        note = [f"entity={e['entity']}", f"reports={e['n']}"]
        if e["years_in"]:
            note.append(f"years={min(e['years_in'])}-{max(e['years_in'])}")
        if a or b:
            fmt = lambda v: f"{v // 10}Q{v % 10}" if v else ""
            note.append(f"ownership_window={fmt(a)}..{fmt(b)}")
        if e["years_out"]:
            note.append(f"excluded_years={','.join(map(str, sorted(e['years_out'])))}")
        if e["note"]:
            note.append(e["note"])
        if e["kind"] == "subcontract":
            note.append("counted as activity, not spend (prime firm's income already includes it)")
        rows.append({"company_id": cid, "client_or_registrant_name": name,
                     "source_id": ";".join(sorted(i for i in e["ids"] if i)),
                     "match_method": method, "note": "; ".join(note)})
    return rows


# ---------------------------------------------------------------------------
# Optional cross-check against the lda.gov API (anonymous: 15 req/min)
# ---------------------------------------------------------------------------

def api_get(endpoint, params):
    key = os.environ.get("LDA_API_KEY")
    headers = {"Authorization": f"Token {key}"} if key else {}
    tag = re.sub(r"[^A-Za-z0-9]+", "_", json.dumps(params, sort_keys=True))[:150]
    path = OUT / "api" / endpoint.strip("/").replace("/", "_") / f"{tag}.json"

    def fetch():
        time.sleep(0.6 if key else 4.5)          # stay under 120/min (key) or 15/min (anonymous)
        return get(LDA_API + endpoint, params, headers=headers, retries=5, pause=10)
    return json.loads(cached(path, fetch))


def api_check(reports, year=2024, n=6):
    """Compare our in-house expense totals with the lda.gov API for the largest in-house filers."""
    tot = collections.Counter()
    for r in reports:
        if r["year"] == year and is_inhouse(r) and r["kind"] == "direct":
            tot[(r["senate_id"], r["registrant"])] += r["expenses"] or 0
    print(f"\nAPI cross-check ({year}, in-house registrants):")
    for (sid, reg), ours in tot.most_common(n):
        reg_id = sid.split("-")[0]
        d = api_get("filings/", {"registrant_id": reg_id, "filing_year": year, "page_size": 25})
        rows = d.get("results", [])
        latest = {}
        for f in rows:
            if str(f["client"]["client_id"]) != sid.split("-")[1]:
                continue
            k = f["filing_period"]
            if k not in latest or f["dt_posted"] > latest[k]["dt_posted"]:
                latest[k] = f
        api = sum(float(f["expenses"] or 0) for f in latest.values())
        flag = "OK" if abs(api - ours) < 1 else "DIFF"
        print(f"  {reg[:40]:40s} house_xml={ours:>14,.0f}  lda.gov={api:>14,.0f}  {flag}"
              + ("" if d.get("next") is None else "  (more pages not fetched)"))


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api-check", action="store_true", help="cross-check top in-house filers with lda.gov API")
    ap.add_argument("--from-year", type=int, default=YEARS.start)
    args = ap.parse_args()
    years = range(args.from_year, YEARS.stop)

    download_all(years)
    reports, covered = collect(years)
    assigned = assign(reports)
    rows, issue_rows = aggregate(assigned, covered, years)
    write_csv(CLEAN / "us_lobby.csv", rows)
    write_csv(CLEAN / "us_lobby_issues.csv", issue_rows, ["company_id", "year", "issue_code", "n_filings"])
    write_csv(CLEAN / "us_lobby_matches.csv", match_rows(reports),
              ["company_id", "client_or_registrant_name", "source_id", "match_method", "note"])

    matched = {r["company_id"] for r in rows if r["n_filings"]}
    print(f"wrote {len(rows)} company-years; {len(matched)}/{len({r['company_id'] for r in rows})} companies "
          f"with LDA filings; years {years.start}-{years.stop - 1}")
    top = sorted((r for r in rows if r["year"] == 2024), key=lambda r: -r["us_lobby_spend_usd"])[:15]
    print("top 2024 spenders:")
    for r in top:
        print(f"  {r['company_id']:16s} ${r['us_lobby_spend_usd']:>12,}  (sum-variant ${r['us_lobby_spend_sum_usd']:>12,})")
    if args.api_check:
        api_check(reports)


if __name__ == "__main__":
    main()
