"""EU lobbying: EU Transparency Register declarations + meetings with the European Commission.

Run from the repo root:  python3 -m influence.sources.eu_lobby
Audit helper:            python3 -m influence.sources.eu_lobby --search "google|alphabet"

Sources
-------
1. EU Transparency Register (TR) open-data snapshots, listed on data.europa.eu
   (dataset "transparency-register"). One XML snapshot per year 2016-2026, the
   January one where it exists (2019: only the June snapshot is XML; the January
   one is a binary .xls). Each snapshot is cached gzip-compressed under
   data/influence/raw/eu_lobby/ together with a parsed, compact JSON version.
   Two XML schemas: the "intws/20141104" schema (2016 - Jan 2022) and the
   "odp" schema of the new register created under the 2021 Interinstitutional
   Agreement (2023 onwards; the 2026 files are XML 1.1 with control-character
   references that expat rejects, so they are sanitised while streaming).
2. Meetings of Commissioners, their cabinets and Directors-General / senior
   Commission staff with registered organisations, as scraped from the
   Commission's published meeting lists by Transparency International EU's
   Integrity Watch EU (https://www.integritywatch.eu/ecmeetings). Their dashboard
   loads three JSON files (Juncker, von der Leyen I, von der Leyen II
   Commissions); each record is one meeting x one organisation and carries the
   TR identification number of the organisation, so meetings are joined on the
   register id, not on names.

Output (data/influence/clean/eu_lobby.csv), one row per company_id x year (2015-2026)
where the company had a matched registration in that year's snapshot or a recorded
meeting in that calendar year:
  year                     snapshot year for register values (register state as of the
                           snapshot month); calendar year for meetings
  snapshot                 snapshot used (YYYY-MM); empty for meeting-only rows (all 2015 rows)
  cost_year                financial year the declared costs refer to (the "last closed
                           financial year": year of the declared period's end date minus
                           one month; max over the company's registrations). Usually
                           year - 2 in January snapshots.
  eu_lobby_cost_low/high/mid_eur   declared annual EU lobbying costs. Large companies
                           declare a bracket (e.g. 1,000,000-1,249,999); low/high are its
                           bounds, mid their midpoint; "< 10,000" has low 0; an exact
                           amount gives low = high = mid.
  cost_open_top_bracket    1 if a registration ticked the open-ended top bracket
                           ("10,000,000 or more", no upper bound); high and mid then use
                           the 10m lower bound, i.e. they understate.
  eu_lobbyists_fte         full-time equivalents declared
  eu_lobbyists_persons     persons involved
  ep_passes                persons holding European Parliament access badges. The field
                           only exists from 2023 and is 0 for every registrant in the
                           2023 and 2024 snapshots, so it is left empty before 2025.
  ec_meetings              distinct meetings in calendar `year` with Commissioners,
                           cabinet members or DG-level staff (ec_meetings_cabinet +
                           ec_meetings_dg). Since December 2024 the Commission also
                           publishes meetings of Directors (not only Directors-General),
                           so ec_meetings_dg jumps from 2025: a disclosure break,
                           not necessarily more lobbying. The current year is partial.
  ec_meetings_cabinet, ec_meetings_dg
  n_registrations          number of matched registrations in the snapshot
All register values are SUMMED across the company's registrations (e.g. a parent and
its EU subsidiaries registered separately); a meeting attended by two registrations of
the same company is counted once. Meetings are counted for every register id matched to
the company (within its ownership window), so a year can have meetings but no snapshot
values (2015, or a registration that existed between two snapshots).
The 2022-01 snapshot was taken during the move to the new register: ~45% of
registrations (and 28 of our companies) carry no financial data there.

Matching (data/influence/clean/eu_lobby_matches.csv): explicit per-company alias lists
(ALIASES below) are compared, after normalisation (case, accents, punctuation), for
exact equality with the registered organisation name in any snapshot. A matched
register id is then followed through every snapshot (ids are stable; renames are
listed in the note column). Optional year windows restrict subsidiaries to the years
the company owned them (e.g. Opel for GM until 2017). Trade associations, foundations,
JVs, independent bottlers/franchisees, spun-off companies and consultancies are not
aliases. Companies without any registration get one `not_found` line.
"""

import collections
import gzip
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

OUT = RAW / "eu_lobby"
HUB = "https://data.europa.eu/api/hub/search/datasets/transparency-register"
YEARS = range(2016, 2027)
MONTHS = {"JANUARY": 1, "JUNE": 6}
IW_BASE = "https://integritywatch.eu/autoupdate_data_eu/lobbyists_ecmeetings/latest/"
IW_FILES = ["ecmeetings_juncker.json", "ecmeetings_vonderleyen1.json", "ecmeetings_vonderleyen2.json"]
PARSE_VERSION = 1

# --------------------------------------------------------------------------------------
# Aliases: company_id -> list of entries. An entry is a registered name (matched after
# normalisation) or a tuple (name, first_year, last_year) restricting snapshot years.
# Every name here was checked against the actual register names (see --search).
# --------------------------------------------------------------------------------------
ALIASES = {
    # --- US technology
    "alphabet": ["Google", "Alphabet Inc.", "DeepMind Technologies Limited", "Waymo", "Wing Aviation LLC"],
    "meta": ["Facebook Ireland Limited", "Meta Platforms Ireland Limited",
             "Meta Platforms Ireland Limited and its various subsidiaries"],
    "microsoft": ["Microsoft Corporation", "LinkedIn Ireland", "GitHub, Inc."],      # GitHub owned since 10/2018
    "apple": ["Apple Inc."],
    "amazon": ["Amazon Europe Core SARL", "Twitch Interactive, Inc."],
    "oracle": ["Oracle"],
    "ibm": ["IBM Corporation", "IBM Research GmbH"],
    "intel": ["Intel Corporation"],
    "qualcomm": ["QUALCOMM Incorporated", "QUALCOMM Inc.", "Qualcomm Communications SARL", "Qualcomm France"],
    "nvidia": ["NVIDIA Corporation"],
    "cisco": ["Cisco Systems Inc."],         # Splunk's registrations lapsed before the 3/2024 acquisition
    "uber": ["Uber"],
    "airbnb": ["Airbnb Ireland", "Airbnb Ireland UC"],
    # --- US pharma (Merck KGaA, Darmstadt, is a different company: not an alias of merck)
    "pfizer": ["Pfizer Inc."],
    "jnj": ["Johnson & Johnson"],            # Kenvue (spun off 2023) excluded
    "merck": ["MSD (Europe), Inc. (Merck Sharp & Dohme)", "Merck Sharp & Dohme Europe Belgium SRL"],
    "abbvie": ["AbbVie"],
    "lilly": ["Eli Lilly and Company"],      # Elanco (separated 2019) excluded
    "bms": ["Bristol-Myers Squibb Company", ("CELGENE", 2020, None)],             # Celgene acquired 11/2019
    "amgen": ["Amgen Inc", ("Horizon Therapeutics Public Limited Company", 2024, None)],  # acquired 10/2023
    "gilead": ["Gilead Sciences"],
    # --- US energy (Chevron Phillips Chemical is a 50/50 JV: excluded)
    "exxon": ["ExxonMobil Petroleum & Chemical"],
    "chevron": ["Chevron Belgium NV", "Chevron Belgium BVBA", "Chevron Belgium BV",
                ("CHEVRON RENEWABLE ENERGY GROUP", 2023, None)],                # REG acquired 6/2022
    "conocophillips": ["ConocoPhillips"],
    # --- US finance
    "jpmorgan": ["JPMorgan Chase & Co.", "JPMorganChase & Co."],
    "goldman": ["The Goldman Sachs Group, Inc."],
    "morganstanley": ["Morgan Stanley"],
    "bofa": ["Bank of America Corporation", "Bank of America Merrill Lynch", "Bank of America"],
    "citi": ["Citigroup Inc."],
    "blackrock": ["BlackRock"],
    "visa": [("Visa Europe", 2017, None)],    # Visa Europe was member-owned until Visa Inc. bought it 6/2016
    "mastercard": ["MasterCard Europe", "Mastercard Europe", "Mastercard"],
    # --- US aerospace & defence (no registration found for General Dynamics or Northrop Grumman)
    "boeing": ["Boeing Belgium SRL", "Boeing International Corporation - Belgium Branch Office"],
    "lockheed": ["Lockheed Martin Global Inc."],
    "rtx": ["RTX Corporation", "Raytheon Technologies", "United Technologies Corporation"],  # same legal entity
    "generaldynamics": [],
    "northrop": [],
    # --- US industrials / autos (GE HealthCare, GE Vernova, Baker Hughes, Solventum: separate companies)
    "ge": ["General Electric Company", "Avio Aero - GE Avio"],
    "honeywell": ["Honeywell Europe NV", "NOVAR - HONEYWELL COMMERCIAL SECURITY"],
    "3m": ["3M"],
    "caterpillar": ["Caterpillar Belgium SA", "Caterpillar Distribution Services Europe bv"],
    "ford": ["Ford Motor Company"],
    "gm": ["General Motors", "General Motors Company", ("Opel Group", None, 2017)],  # Opel sold to PSA 8/2017
    "tesla": ["Tesla Motors Netherlands B.V.", "Tesla Motors, Inc.", "Tesla, Inc."],
    # --- US consumer (Coca-Cola bottlers CCEP/CCHBC are independent listed companies: excluded)
    "cocacola": ["The Coca-Cola Company"],
    "pepsico": ["PepsiCo", "PepsiCo Europe"],
    "pg": ["Procter & Gamble"],
    "pmi": ["Philip Morris International Inc.", ("Swedish Match", 2023, None)],  # acquired 11/2022
    "altria": [],
    "walmart": ["Walmart", "Wal-Mart Stores, Inc. and its subsidiaries and affiliates"],
    "mcdonalds": ["McDonald's Europe", "McDonald’s Global Franchising Limited", "McD Europe Franchising LLC"],
    # --- US telecom & media
    "att": ["AT&T, Inc."],                   # Time Warner's registration lapsed before the 6/2018 acquisition
    "verizon": ["Verizon Communications Inc",
                ("Oath EMEA Ltd.", 2018, 2021), ("Verizon Media (previously Yahoo! EMEA Ltd.)", 2018, 2021),
                ("Yahoo! (Oath EMEA Ltd.)", 2018, 2021)],                       # Yahoo owned 6/2017-9/2021
    "comcast": ["NBCUniversal International", "NBC Universal - News",
                ("Sky plc", 2019, None), ("Sky Group", 2019, None)],               # Sky acquired 10/2018
    # --- US chemicals & agri
    "dow": ["Dow Europe GmbH", ("Dow Corning Europe", 2017, None)],             # Dow Corning 100% Dow from 6/2016
    "dupont": ["DuPont de Nemours International SARL", "Du Pont de Nemours International SARL"],
    "corteva": ["Corteva Agriscience International SARL", "Corteva Agriscience/DuPont International Operations SARL"],
    # --- Germany (Siemens Energy / Siemens Gamesa, Daimler Truck: spun off before registering)
    "siemens": ["Siemens AG", "Siemens Healthineers AG"],
    "basf": ["BASF SE", ("Wintershall Holding GmbH", None, 2018)],            # Wintershall Dea (JV) from 5/2019
    "bayer": ["Bayer AG", "Bayer Nordic SE", ("Monsanto", 2019, None)],          # Monsanto acquired 6/2018
    "volkswagen": ["Volkswagen Aktiengesellschaft", "Audi Aktiengesellschaft", "MAN SE", "MAN  SE",
                   "MAN Truck & Bus SE", "Scania AB (publ)"],
    "bmw": ["Bayerische Motoren Werke Aktiengesellschaft"],
    "mercedes": ["Daimler Aktiengesellschaft", "Mercedes-Benz Group AG"],
    "dtelekom": ["Deutsche Telekom", "S.C. TELEKOM ROMANIA COMMUNICATIONS S.A.", "Slovak Telekom, a. s.",
                 "Slovak Telekom, a.s.", "T-Mobile Austria GmbH"],
    "sap": ["SAP"],
    "allianz": ["Allianz SE"],
    "deutschebank": ["Deutsche Bank AG", "Deutsche Postbank AG", "DB Privat- und Firmenkundenbank AG",
                     "DWS Group GmbH & Co. KGaA"],
    # --- France / Netherlands
    "airbus": ["Airbus", "Airbus Group N.V."],
    "totalenergies": ["TOTAL S.A.", "TOTAL S.E.", "TotalEnergies SE",
                      ("Total Direct Energie", 2019, None),
                      ("TotalEnergies Electricité et Gaz France", 2019, None)],  # Direct Energie bought 7/2018
    "sanofi": ["SANOFI", "Genzyme a SANOFI Company", "SANOFI GENZYME", "Sanofi Pasteur"],
    "loreal": ["L'Oréal"],
    "lvmh": ["LVMH - Moët Hennessy Louis Vuitton", "LVMH Publica", "Louis Vuitton Malletier, SA",
             "Moët Hennessy"],
    "bnpparibas": ["BNP PARIBAS", "BNP Paribas Cardif", "BNP Paribas Personal Finance", "BNP Paribas Securities Services"],
    "axa": ["AXA", "AXA Investment Managers", ("AXA XL", 2019, None)],       # XL Group acquired 9/2018
    "danone": ["Danone"],
    "orange": ["Orange", "Orange Belgium"],
    "renault": ["RENAULT"],                  # Renault Trucks belongs to Volvo Group: excluded
    "safran": ["Safran", "SAFRAN"],
    "thales": ["THALES", "Thales Norway AS"],
    "stellantis": ["Stellantis", ("Fiat Chrysler Automobiles", 2021, None), ("Peugeot S.A.", 2021, None)],  # merger 1/2021
    "shell": ["Shell Companies"],
    "ing": ["ING Group"],
    "asml": ["ASML Netherlands B.V.", "ASML Netherllands B.V."],
    "philips": ["Koninklijke Philips", ("Philips Lighting", None, 2017), ("Philips Lighting BV", None, 2017)],
    "unilever": ["Unilever"],
    "heineken": ["Heineken N.V."],
    "ahold": ["Ahold", "Ahold Delhaize"],
    "prosus": ["Naspers / Prosus", "Prosus", "Naspers", "OLX Global BV",
               ("Just Eat Takeaway.com N.V.", 2026, None)],                    # JET acquired 2025; Delivery Hero is an associate
    # --- UK
    "bp": ["BP p.l.c.", ("Lightsource bp", 2025, None)],   # 50/50 JV until bp bought the rest 10/2024
    "astrazeneca": ["AstraZeneca", "AstraZeneca PLC", ("Alexion Pharmaceuticals", 2022, None)],  # Alexion bought 7/2021
    "gsk": ["GSK", "GlaxoSmithKline", "ViiV Healthcare Limited"],             # Haleon (demerged 2022) excluded
    "hsbc": ["HSBC Holdings PLC"],
    "barclays": ["Barclays PLC"],
    "bae": ["BAE Systems"],
    "bat": ["British American Tobacco"],
    "riotinto": ["Rio Tinto", "RIO TINTO"],
    "vodafone": ["Vodafone Belgium SA", "Vodafone GmbH"],  # Vodafone Spain sold 5/2024, VodafoneZiggo is a 50/50 JV
    # --- Switzerland / Nordics / Southern Europe (Alcon, Sandoz: spun off before registering)
    "nestle": ["Nestlé S.A."],
    "novartis": ["Novartis International AG"],
    "roche": ["F. Hoffmann-La Roche Ltd"],
    "ubs": ["UBS Group AG", ("Credit Suisse Group AG", 2024, None)],              # CS acquired 6/2023
    "equinor": ["Equinor ASA", "Statoil ASA"],
    "novonordisk": ["Novo Nordisk", "Novo Nordisk A/S"],
    "ericsson": ["Ericsson"],
    "nokia": ["Nokia", "Alcatel-Lucent"],    # Alcatel Submarine Networks (registered after its 2024 sale) excluded
    "iberdrola": ["IBERDROLA"],
    "santander": ["Banco Santander, S.A."],
    "enel": ["ENEL SpA", "Endesa", "Enel Green Power S.p.A."],
    "eni": ["Eni S.p.A."],
    "spotify": ["Spotify", "Spotify AB", "Spotify Belgium NV", "Spotify Belgium SA"],
}


# ------------------------------------------------------------------------- downloads --

def snapshot_urls():
    """{year: (month, url, title)} choosing the earliest XML snapshot of each year."""
    hub = json.loads(cached(OUT / "hub_transparency_register.json", lambda: get(HUB)))
    best = {}
    for d in hub["result"]["distributions"]:
        title = d.get("title")
        title = title.get("en", "") if isinstance(title, dict) else (title or "")
        m = re.search(r"(20\d\d)\s*[-–]\s*(JANUARY|JUNE)", title)
        url = (d.get("access_url") or [None])[0]
        fmt = (d.get("format") or {}).get("id", "")
        if not m or not url or not (fmt == "XML" or url.lower().endswith(".xml")):
            continue
        y, mo = int(m.group(1)), MONTHS[m.group(2)]
        if y in YEARS and (y not in best or mo < best[y][0]):
            best[y] = (mo, url, title.strip())
    return best


def fetch_slow(url):
    print("  downloading", url, flush=True)
    data = get(url)
    time.sleep(2)
    return data


# --------------------------------------------------------------------------- parsing --

# XML 1.0 forbids most C0 control characters even as character references.
BAD_REF = re.compile(rb"&#(?:x0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));")


class Sanitised:
    """File wrapper: drops invalid char refs and downgrades an XML 1.1 declaration."""

    def __init__(self, f):
        self.f, self.tail, self.first = f, b"", True

    def read(self, n=-1):
        chunk = self.f.read(n if n and n > 0 else 1 << 20)
        data = self.tail + chunk
        if self.first:
            data = re.sub(rb"^(<\?xml[^>]*version=)(['\"])1\.1\2", rb"\g<1>\g<2>1.0\g<2>", data, count=1)
            self.first = False
        i = data.rfind(b"&")
        if chunk and i >= 0 and len(data) - i < 16:  # keep a possibly split reference
            data, self.tail = data[:i], data[i:]
        else:
            self.tail = b""
        return BAD_REF.sub(b" ", data)


def tag(e):
    return e.tag.rsplit("}", 1)[-1]


def child(e, *path):
    for p in path:
        if e is None:
            return None
        e = next((c for c in e if tag(c) == p), None)
    return e


def text(e, *path):
    e = child(e, *path)
    return (e.text or "").strip() if e is not None else ""


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def cost_of(c):
    """(low, high) from a <cost>/<costs> element: CostRange or AbsoluteCost."""
    if c is None:
        return None, None
    a = num(text(c, "absoluteCost"))
    if a is not None:
        return a, a
    rng = child(c, "range")
    if rng is None:
        return None, None
    lo, hi = num(text(rng, "min")), num(text(rng, "max"))
    if hi is None and lo is None:
        return None, None
    return (lo if lo is not None else 0.0), hi


def parse_ir(ir):
    fin = child(ir, "financialData")
    closed = child(fin, "closedYear")
    if closed is not None:                      # new (2021 IIA) schema
        start, end = text(closed, "startDate"), text(closed, "endDate")
        lo, hi = cost_of(child(closed, "costs"))
        schema = "odp"
    else:                                        # old schema
        start, end = text(fin, "startDate"), text(fin, "endDate")
        lo, hi = cost_of(child(fin, "financialInformation", "cost"))
        schema = "intws" if child(ir, "category") is not None else "odp"
    return {
        "id": text(ir, "identificationCode"),
        "name": text(ir, "name", "originalName"),
        "acronym": text(ir, "acronym"),
        "category": text(ir, "registrationCategory") or text(ir, "category", "subCategory"),
        "country": text(ir, "headOffice", "country") or text(ir, "contactDetails", "country"),
        "fin_start": start[:10], "fin_end": end[:10],
        "cost_low": lo, "cost_high": hi,
        "fte": num(text(ir, "members", "membersFTE")),
        "persons": num(text(ir, "members", "members")),
        "ep_passes": num(text(ir, "EPAccreditedNumber")) if schema == "odp" else None,
        "schema": schema,
    }


def snapshot(year, month, url):
    """Parsed records of one snapshot (cached as compact JSON)."""
    xml_path = OUT / f"tr_{year}_{month:02d}.xml.gz"
    parsed = OUT / f"tr_{year}_{month:02d}.parsed.json.gz"
    if parsed.exists():
        d = json.loads(gzip.decompress(parsed.read_bytes()))
        if d.get("version") == PARSE_VERSION:
            return d
    cached(xml_path, lambda: gzip.compress(fetch_slow(url), 6))
    recs, meta = [], {}
    with gzip.open(xml_path) as f:
        for _, el in ET.iterparse(Sanitised(f)):
            t = tag(el)
            if t == "interestRepresentative":
                recs.append(parse_ir(el))
                el.clear()
            elif t in ("exportDate", "numberOfIR"):
                meta[t] = (el.text or "").strip()
    d = {"version": PARSE_VERSION, "year": year, "month": month, "url": url, "meta": meta, "records": recs}
    parsed.write_bytes(gzip.compress(json.dumps(d).encode(), 6))
    print(f"  parsed {year}-{month:02d}: {len(recs)} registrations (export {meta.get('exportDate', '?')[:10]})")
    return d


def load_snapshots():
    snaps = {}
    for y, (mo, url, _title) in sorted(snapshot_urls().items()):
        snaps[y] = snapshot(y, mo, url)
    return snaps


# -------------------------------------------------------------------------- matching --

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def alias_index():
    """normalised name -> (company_id, first_year or None, last_year or None)."""
    idx = {}
    for cid, entries in ALIASES.items():
        for e in entries:
            name, y0, y1 = (e, None, None) if isinstance(e, str) else e
            k = norm(name)
            if k in idx and idx[k][0] != cid:
                raise ValueError(f"alias {name!r} used for {idx[k][0]} and {cid}")
            idx[k] = (cid, y0, y1)
    return idx


def in_window(y, window):
    y0, y1 = window
    return (y0 is None or y >= y0) and (y1 is None or y <= y1)


def match(snaps):
    """{company_id: {register_id: info}}: exact alias-name hit in any snapshot, then follow the id.

    A register id's window is the union of the windows of the alias names that hit it.
    """
    idx = alias_index()
    by_id = collections.defaultdict(dict)                 # rid -> {year: rec}
    for y, s in snaps.items():
        for r in s["records"]:
            by_id[r["id"]].setdefault(y, r)
    hits = collections.defaultdict(dict)
    for rid, years in by_id.items():
        for y, r in years.items():
            k = norm(r["name"])
            if k not in idx:
                continue
            cid, y0, y1 = idx[k]
            h = hits[cid].setdefault(rid, {"alias_names": set(), "window": (y0, y1)})
            h["alias_names"].add(r["name"])
            w0, w1 = h["window"]
            h["window"] = (None if y0 is None or w0 is None else min(w0, y0),
                           None if y1 is None or w1 is None else max(w1, y1))
    for cid, regs in hits.items():
        others = {c for c, rr in hits.items() if c != cid and set(rr) & set(regs)}
        if others:
            raise ValueError(f"register ids shared by {cid} and {others}")
        for rid, h in list(regs.items()):
            h["years"] = {y: r for y, r in by_id[rid].items() if in_window(y, h["window"])}
            h["all_names"] = sorted({r["name"] for r in by_id[rid].values()})
            if not h["years"]:          # e.g. a lapsed registration from before an acquisition
                del regs[rid]
    return {cid: regs for cid, regs in hits.items() if regs}


# -------------------------------------------------------------------------- meetings --

def meetings():
    """{register_id: [(date, kind, key), ...]} from Integrity Watch EU."""
    out = collections.defaultdict(list)
    for fn in IW_FILES:
        rows = json.loads(cached(OUT / f"iw_{fn}", lambda fn=fn: fetch_slow(IW_BASE + fn)))
        for m in rows:
            rid = (m.get("OrgId") or "").strip()
            if not rid or not m.get("date"):
                continue
            kind = "dg" if m.get("type") == "dg" else "cabinet"
            key = (m["date"], kind, m.get("cabinet") or m.get("dgacronym") or "",
                   tuple(sorted(m.get("Host") or [])), (m.get("subject") or "").strip().lower())
            out[rid].append(key)
    return out


# ------------------------------------------------------------------------------ main --

FIRST_YEAR = 2015   # first full calendar year of published Commission meetings (lists start 12/2014)


def cost_year(fin_end):
    """Financial year of a declared period: year of (end date - 1 month), since periods are
    recorded as e.g. 2014-01-01..2014-12-01 or 2015-01-01..2016-01-01 (both = FY2014/FY2015)."""
    if not fin_end:
        return None
    y, m = int(fin_end[:4]), int(fin_end[5:7])
    return y - 1 if m == 1 else y


def company_rows(cid, regs, snaps, meets, ep_years, last_meeting_year):
    # meetings per calendar year, deduplicated across the company's registrations
    mcount = collections.defaultdict(collections.Counter)
    seen = set()
    for h in regs.values():
        for key in h["meetings"]:
            y = int(key[0][:4])
            if y < FIRST_YEAR or not in_window(y, h["window"]) or key in seen:
                continue
            seen.add(key)
            mcount[y][key[1]] += 1

    rows = []
    for y in sorted(set(y for h in regs.values() for y in h["years"]) | set(mcount)):
        recs = [h["years"][y] for h in regs.values() if y in h["years"]]
        row = {"company_id": cid, "year": y, "n_registrations": len(recs)}
        if recs:
            row["snapshot"] = f"{y}-{snaps[y]['month']:02d}"
            cys = [cost_year(r["fin_end"]) for r in recs if r["fin_end"] and r["cost_low"] is not None]
            row["cost_year"] = max(cys) if cys else ""
            costs = [r for r in recs if r["cost_low"] is not None]
            if costs:
                # open top bracket ("10,000,000 or more", no max): high = low bound
                open_top = any(r["cost_high"] is None for r in costs)
                lo = sum(r["cost_low"] for r in costs)
                hi = sum(r["cost_high"] if r["cost_high"] is not None else r["cost_low"] for r in costs)
                row.update(eu_lobby_cost_low_eur=round(lo), eu_lobby_cost_high_eur=round(hi),
                           eu_lobby_cost_mid_eur=round((lo + hi) / 2), cost_open_top_bracket=int(open_top))
            for col, f in (("eu_lobbyists_fte", "fte"), ("eu_lobbyists_persons", "persons")):
                vals = [r[f] for r in recs if r[f] is not None]
                if vals:
                    row[col] = round(sum(vals), 2)
            if y in ep_years:
                row["ep_passes"] = int(sum(r["ep_passes"] or 0 for r in recs))
        if FIRST_YEAR <= y <= last_meeting_year:
            mc = mcount.get(y, {})
            row["ec_meetings_cabinet"] = mc.get("cabinet", 0)
            row["ec_meetings_dg"] = mc.get("dg", 0)
            row["ec_meetings"] = row["ec_meetings_cabinet"] + row["ec_meetings_dg"]
        rows.append(row)
    return rows


def build():
    snaps = load_snapshots()
    matched = match(snaps)
    meets = meetings()
    last_meeting_year = max(int(k[0][:4]) for v in meets.values() for k in v)
    # EPAccreditedNumber exists from the 2023 snapshot but is 0 for every registrant in 2023-2024:
    # only use it in snapshots where it is actually populated.
    ep_years = {y for y, s in snaps.items() if any((r["ep_passes"] or 0) > 0 for r in s["records"])}

    rows, mrows = [], []
    for c in companies():
        cid = c["company_id"]
        regs = matched.get(cid, {})
        if cid not in ALIASES:
            print("  warning: no alias entry for", cid)
        if not regs:
            mrows.append({"company_id": cid, "register_id": "", "register_name": "",
                          "match_method": "not_found",
                          "note": f"no registration of {c['name']} or its subsidiaries in the {min(YEARS)}-{max(YEARS)} snapshots"})
            continue
        for rid, h in sorted(regs.items()):
            h["meetings"] = meets.get(rid, [])
            yrs = sorted(h["years"])
            note = [f"in snapshots {yrs[0]}-{yrs[-1]} ({len(yrs)} years)"]
            if h["window"] != (None, None):
                w0, w1 = h["window"]
                note.append(f"counted only {w0 or ''}-{w1 or ''} (ownership)")
            if len(h["all_names"]) > 1:
                note.append("names over time: " + " / ".join(h["all_names"]))
            note.append("category: " + ", ".join(sorted({r["category"] for r in h["years"].values()})))
            note.append(f"{len(h['meetings'])} EC meeting records 2014-{last_meeting_year}")
            mrows.append({"company_id": cid, "register_id": rid, "register_name": h["years"][yrs[-1]]["name"],
                          "match_method": "alias_exact_name" if h["window"] == (None, None) else "alias_exact_name_windowed",
                          "note": "; ".join(note)})
        rows += company_rows(cid, regs, snaps, meets, ep_years, last_meeting_year)

    fields = ["company_id", "year", "snapshot", "cost_year", "eu_lobby_cost_low_eur", "eu_lobby_cost_high_eur",
              "eu_lobby_cost_mid_eur", "cost_open_top_bracket", "eu_lobbyists_fte", "eu_lobbyists_persons",
              "ep_passes", "ec_meetings", "ec_meetings_cabinet", "ec_meetings_dg", "n_registrations"]
    write_csv(CLEAN / "eu_lobby.csv", rows, fields)
    write_csv(CLEAN / "eu_lobby_matches.csv", mrows,
              ["company_id", "register_id", "register_name", "match_method", "note"])
    found = {r["company_id"] for r in mrows if r["match_method"] != "not_found"}
    print(f"wrote {len(rows)} company-year rows; {len(found)}/{len(companies())} companies matched "
          f"({sum(1 for r in mrows if r['register_id'])} registrations); meetings up to {last_meeting_year}")
    return rows, mrows


def search(pattern):
    """Print every registration (any snapshot) whose name or acronym matches `pattern`."""
    rx = re.compile(pattern, re.I)
    seen = {}
    for y, s in load_snapshots().items():
        for r in s["records"]:
            if rx.search(r["name"]) or (r["acronym"] and rx.search(r["acronym"])):
                e = seen.setdefault(r["id"], {"years": [], "names": set(), "r": r})
                e["years"].append(y)
                e["names"].add(r["name"])
                e["r"] = r
    for rid, e in sorted(seen.items(), key=lambda kv: sorted(kv[1]["names"])[0].lower()):
        r = e["r"]
        print(f"{rid:>18} | {' / '.join(sorted(e['names']))} | {r['acronym']} | {r['category'][:24]} | "
              f"{r['country'][:14]} | {min(e['years'])}-{max(e['years'])} | fte {r['fte']} cost {r['cost_high']}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--search":
        search(sys.argv[2])
    else:
        build()
