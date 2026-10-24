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
import gzip
import json
import os
import re
import sys
import time
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


