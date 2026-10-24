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

Output (data/influence/clean/eu_lobby.csv), one row per company_id x year where
the company had at least one matched registration in that year's snapshot or a
recorded meeting in that calendar year:
  year                     snapshot year (register state as of the snapshot month)
  snapshot                 snapshot used (YYYY-MM)
  cost_year                last closed financial year the declared costs refer to
                           (year of the declared period's end date; max over the
                           company's registrations)
  eu_lobby_cost_low/high/mid_eur   declared annual EU lobbying costs. The register
                           only asks for a bracket (e.g. 1,000,000-1,249,999); low/high
                           are the bracket bounds, mid their midpoint; "< 10,000"
                           has low 0; an exact amount gives low = high = mid.
  eu_lobbyists_fte         full-time equivalents declared
  eu_lobbyists_persons     persons involved
  ep_passes                persons holding European Parliament access badges
                           (only in the post-2021 schema, i.e. snapshot years >= 2023)
  ec_meetings              distinct meetings in calendar `year` with Commissioners,
                           cabinet members or DG-level staff (ec_meetings_cabinet +
                           ec_meetings_dg). From December 2024 the Commission also
                           publishes meetings of Directors (not only Directors-General),
                           so ec_meetings_dg jumps from 2025 - a disclosure break,
                           not necessarily more lobbying.
  ec_meetings_cabinet, ec_meetings_dg
  n_registrations          number of matched registrations in the snapshot
All register values are SUMMED across the company's registrations (e.g. a parent and
its EU subsidiaries registered separately); meetings attended by two registrations of
the same company are counted once. Meetings are counted for all register ids ever
matched to the company, so a year can have meetings but no snapshot rows (e.g. the
2014-2015 meetings, or a registration that lapsed); those rows have empty cost columns.

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
ALIASES = {}


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
    idx = {}
    for cid, entries in ALIASES.items():
        for e in entries:
            name, y0, y1 = (e, None, None) if isinstance(e, str) else e
            k = norm(name)
            if k in idx and idx[k][0] != cid:
                raise ValueError(f"alias {name!r} used for {idx[k][0]} and {cid}")
            idx[k] = (cid, y0 or min(YEARS), y1 or max(YEARS))
    return idx


def match(snaps):
    """{company_id: {register_id: {"years": {...}, ...}}} via exact alias names, then id."""
    idx = alias_index()
    by_id = collections.defaultdict(dict)                 # rid -> {year: rec}
    for y, s in snaps.items():
        for r in s["records"]:
            by_id[r["id"]].setdefault(y, r)
    hits = collections.defaultdict(dict)
    for rid, years in by_id.items():
        for y, r in years.items():
            k = norm(r["name"])
            if k in idx:
                cid, y0, y1 = idx[k]
                h = hits[cid].setdefault(rid, {"alias_names": set(), "window": (y0, y1)})
                h["alias_names"].add(r["name"])
                # widest window over the alias names that hit this id
                h["window"] = (min(h["window"][0], y0), max(h["window"][1], y1))
    out = {}
    for cid, regs in hits.items():
        out[cid] = {}
        for rid, h in regs.items():
            y0, y1 = h["window"]
            out[cid][rid] = {
                "years": {y: r for y, r in by_id[rid].items() if y0 <= y <= y1},
                "all_names": sorted({r["name"] for r in by_id[rid].values()}),
                "alias_names": sorted(h["alias_names"]),
                "window": h["window"],
            }
    return out


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

def build():
    snaps = load_snapshots()
    matched = match(snaps)
    meets = meetings()
    comp = companies()

    rows, mrows = [], []
    for c in comp:
        cid = c["company_id"]
        regs = matched.get(cid, {})
        if not regs:
            mrows.append({"company_id": cid, "register_id": "", "register_name": "",
                          "match_method": "not_found",
                          "note": f"no registration whose name equals an alias of {c['name']!r} in 2016-2026 snapshots"})
            continue
        for rid, h in sorted(regs.items()):
            yrs = sorted(h["years"])
            note = [f"snapshot years {yrs[0]}-{yrs[-1]} ({len(yrs)})" if yrs else "in no snapshot inside window"]
            if h["window"] != (min(YEARS), max(YEARS)):
                note.append(f"counted for {h['window'][0]}-{h['window'][1]} only (ownership)")
            if len(h["all_names"]) > 1:
                note.append("names over time: " + " / ".join(h["all_names"]))
            cats = sorted({r["category"] for r in h["years"].values()})
            note.append("category: " + ", ".join(cats))
            nm = len(meets.get(rid, []))
            note.append(f"{nm} EC meetings 2014-2026")
            mrows.append({"company_id": cid, "register_id": rid,
                          "register_name": h["years"][yrs[-1]]["name"] if yrs else h["all_names"][-1],
                          "match_method": "alias_exact_name+register_id", "note": "; ".join(note)})

        # meetings per calendar year, deduplicated across the company's registrations
        mcount = collections.defaultdict(lambda: collections.Counter())
        seen = set()
        for rid, h in regs.items():
            y0, y1 = h["window"]
            for key in meets.get(rid, []):
                y = int(key[0][:4])
                if not (y0 <= y <= y1 or (y < min(YEARS) and y0 == min(YEARS))):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                mcount[y][key[1]] += 1

        years = sorted(set(y for h in regs.values() for y in h["years"]) | set(mcount))
        for y in years:
            recs = [h["years"][y] for h in regs.values() if y in h["years"]]
            row = {"company_id": cid, "year": y, "n_registrations": len(recs)}
            if recs:
                s = snaps[y]
                row["snapshot"] = f"{y}-{s['month']:02d}"
                ends = [r["fin_end"][:4] for r in recs if r["fin_end"]]
                row["cost_year"] = max(ends) if ends else ""
                costs = [r for r in recs if r["cost_high"] is not None]
                if costs:
                    lo = sum(r["cost_low"] for r in costs)
                    hi = sum(r["cost_high"] for r in costs)
                    row.update(eu_lobby_cost_low_eur=round(lo), eu_lobby_cost_high_eur=round(hi),
                               eu_lobby_cost_mid_eur=round((lo + hi) / 2))
                for col, f in (("eu_lobbyists_fte", "fte"), ("eu_lobbyists_persons", "persons"),
                               ("ep_passes", "ep_passes")):
                    vals = [r[f] for r in recs if r[f] is not None]
                    if vals:
                        row[col] = round(sum(vals), 2)
            mc = mcount.get(y)
            if mc is not None or (2015 <= y <= max(int(k[0][:4]) for v in meets.values() for k in v)):
                mc = mc or {}
                row["ec_meetings_cabinet"] = mc.get("cabinet", 0)
                row["ec_meetings_dg"] = mc.get("dg", 0)
                row["ec_meetings"] = row["ec_meetings_cabinet"] + row["ec_meetings_dg"]
            rows.append(row)

    fields = ["company_id", "year", "snapshot", "cost_year", "eu_lobby_cost_low_eur", "eu_lobby_cost_high_eur",
              "eu_lobby_cost_mid_eur", "eu_lobbyists_fte", "eu_lobbyists_persons", "ep_passes",
              "ec_meetings", "ec_meetings_cabinet", "ec_meetings_dg", "n_registrations"]
    write_csv(CLEAN / "eu_lobby.csv", rows, fields)
    write_csv(CLEAN / "eu_lobby_matches.csv", mrows,
              ["company_id", "register_id", "register_name", "match_method", "note"])
    n_found = len({r["company_id"] for r in mrows if r["match_method"] != "not_found"})
    print(f"wrote {len(rows)} rows; {n_found}/{len(comp)} companies matched")
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
