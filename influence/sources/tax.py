"""Effective tax rates from companies' own XBRL financial statements.

Sources
-------
* SEC companyfacts API (https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json):
  10-K filers tagged in `us-gaap`, 20-F filers tagged in `ifrs-full` (IFRS filers have
  machine-readable facts from ~FY2017/18 on).
* ESEF annual financial reports (EU/EEA/UK listed issuers, FY2020+) via filings.xbrl.org
  (https://filings.xbrl.org/api/filings?filter[entity.identifier]=<LEI>), xBRL-JSON per filing.
  filings.xbrl.org does not index German filings (Bundesanzeiger), and Swiss issuers are
  outside ESEF, so those companies are only covered when they file with the SEC.

For each company SEC values are used where available; ESEF fills fiscal years SEC lacks.

Method
------
* Annual values only: duration 350-380 days, from 10-K / 20-F / 40-F (SEC) or the annual
  ESEF report; non-dimensional (consolidated total) facts only.
* fiscal_year = calendar year of the period end (periods ending 1-7 January count to the
  previous year, for 52/53-week years).
* Restatements: for each concept and fiscal year the value from the latest filing is kept
  (later reports' comparatives supersede the original figure).
* Concepts are tried in priority order per fiscal year (see *_CONCEPTS below).
* book_etr = income_tax_expense / pretax_income; cash_etr = cash_taxes_paid / pretax_income;
  both left empty when pretax income <= 0.
* ESEF sign sanity checks: tax expense is flipped when it is clearly the negative of
  (pretax - profit from continuing operations); cash taxes paid tagged as a negative
  outflow are flipped when book tax is positive. Flips are recorded in tax_sources.note.

Raw cache: data/influence/raw/tax/ (SEC companyfacts gzip-compressed; ESEF xBRL-JSON reduced to
non-dimensional facts, gzip-compressed).

Run: python3 -m influence.sources.tax
"""

import gzip
import json
import time
import urllib.error
from collections import defaultdict
from datetime import date

from influence.common import CLEAN, RAW, SEC_USER_AGENT, cached, companies, get, write_csv

RAW_TAX = RAW / "tax"
YEARS = range(2015, 2026)

# ---------------------------------------------------------------- concepts (priority order)
USGAAP = {
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic+IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
    ],
    "income_tax_expense": [
        "IncomeTaxExpenseBenefit",
        "IncomeTaxExpenseBenefitContinuingOperations",
    ],
    "cash_taxes_paid": [
        "IncomeTaxesPaidNet",
        "IncomeTaxesPaid",
    ],
    # gross-of-excise ("IncludingAssessedTax") last: for tobacco it includes excise duties
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
}
IFRS = {
    # IFRS issuers that do not present a pre-tax subtotal (common in France: TotalEnergies,
    # LVMH, Danone, L'Oreal, Airbus, Sanofi) get pre-tax = profit (continuing ops) + income tax.
    "pretax_income": [
        "ProfitLossBeforeTax",
        "ProfitLossFromContinuingOperations+IncomeTaxExpenseContinuingOperations",
        "ProfitLoss+IncomeTaxExpenseContinuingOperations",
    ],
    "income_tax_expense": [
        "IncomeTaxExpenseContinuingOperations",
    ],
    "cash_taxes_paid": [
        "IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
        "IncomeTaxesPaidClassifiedAsOperatingActivities",
        "IncomeTaxesPaidRefund",
        "IncomeTaxesPaid",
    ],
    "revenue": [
        ("Revenue", "RevenueFromContractsWithCustomers"),
        "RevenueAndOperatingIncome",
    ],
}
# Per-company revenue concept overrides, where the generic priority picks a partial figure:
# BlackRock tags a sub-total as us-gaap:Revenues in FY2022-24 (e.g. 12.8bn vs 20.4bn total).
REVENUE_OVERRIDE = {"blackrock": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]}

# used only for derivation / sign checks
IFRS_PL_CONT = "ProfitLossFromContinuingOperations"

SEC_ANNUAL_FORMS = ("10-K", "20-F", "40-F")
FIELDS = ["pretax_income", "income_tax_expense", "cash_taxes_paid", "revenue"]

# ---------------------------------------------------------------- statutory rates
# Combined (central + average sub-central) statutory corporate income tax rate, %, from the
# OECD Corporate Tax Statistics database, Table II.1 "Statutory corporate income tax rates"
# (https://data-explorer.oecd.org, dataset CTS_CIT), rounded to 0.1. Approximate context only:
# US = 21% federal + average state (35% federal before the 2017 TCJA); FR includes surtaxes
# (2017 exceptional surtax, temporary 2025 surcharge on large companies not included);
# GB 2023 main rate 25% from April 2023; CH = average of cantons incl. federal.
# Format: country -> list of (first_year, rate) step changes.
STATUTORY = {
    "US": [(2015, 38.9), (2018, 25.8)],
    "DE": [(2015, 29.9)],
    "FR": [(2015, 38.0), (2016, 34.4), (2017, 44.4), (2018, 34.4), (2020, 32.0),
           (2021, 28.4), (2022, 25.8)],
    "GB": [(2015, 20.0), (2017, 19.0), (2023, 25.0)],
    "NL": [(2015, 25.0), (2022, 25.8)],
    "CH": [(2015, 21.1), (2020, 19.6)],
    "IT": [(2015, 31.4), (2017, 27.8)],
    "ES": [(2015, 28.0), (2016, 25.0)],
    "NO": [(2015, 27.0), (2016, 25.0), (2017, 24.0), (2018, 23.0), (2019, 22.0)],
    "DK": [(2015, 23.5), (2016, 22.0)],
    "SE": [(2015, 22.0), (2019, 21.4), (2021, 20.6)],
    "FI": [(2015, 20.0)],
    "LU": [(2015, 29.2), (2017, 27.1), (2018, 26.0), (2019, 24.9)],
}


def statutory(country, year):
    rate = ""
    for start, r in STATUTORY.get(country, []):
        if year >= start:
            rate = r
    return rate


# ---------------------------------------------------------------- helpers
def _d(s):
    return date.fromisoformat(s[:10])


def fiscal_year(end):
    e = _d(end) if isinstance(end, str) else end
    if e.month == 1 and e.day <= 7:
        return e.year - 1
    return e.year


def is_annual(start, end):
    return 350 <= (_d(end) - _d(start)).days <= 380


_last_sec = [0.0]


def sec_get(url):
    wait = 0.15 - (time.time() - _last_sec[0])
    if wait > 0:
        time.sleep(wait)
    _last_sec[0] = time.time()
    return get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "identity"})


def _gz_json(path, fetch):
    """Cache `fetch()` (bytes) gzip-compressed; return parsed JSON."""
    return json.loads(gzip.decompress(cached(path, lambda: gzip.compress(fetch()))))


def _missing_ok(fetch):
    def f():
        try:
            return fetch()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b"{}"
            raise
    return f


# ---------------------------------------------------------------- SEC
def sec_facts(cik):
    cik = cik.zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    return _gz_json(RAW_TAX / "sec" / f"CIK{cik}.json.gz", _missing_ok(lambda: sec_get(url)))


def sec_series(facts_list, taxonomy, concept):
    """{(unit, fy): (filed, val, end)} for annual 10-K/20-F facts of one concept (latest filed wins)."""
    out = {}
    for facts in facts_list:
        node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
        if not node:
            continue
        for unit, items in node.get("units", {}).items():
            for x in items:
                if "start" not in x or not is_annual(x["start"], x["end"]):
                    continue
                if not str(x.get("form", "")).startswith(SEC_ANNUAL_FORMS):
                    continue
                key = (unit, fiscal_year(x["end"]))
                cand = (x.get("filed", ""), x["val"], x["end"])
                if key not in out or cand[:1] + (cand[2],) > out[key][:1] + (out[key][2],):
                    out[key] = cand
    return out


def sec_company(ciks, cid=""):
    facts_list = [f for f in (sec_facts(c) for c in ciks) if f.get("facts")]
    if not facts_list:
        return None
    best = None
    usgaap = dict(USGAAP, revenue=REVENUE_OVERRIDE[cid]) if cid in REVENUE_OVERRIDE else USGAAP
    for source, taxonomy, concepts in (("sec_usgaap", "us-gaap", usgaap), ("sec_ifrs", "ifrs-full", IFRS)):
        res = extract(lambda c, t=taxonomy: sec_series(facts_list, t, c), concepts)
        if res and (best is None or len(res[0]) > len(best[1][0])):
            best = (source, res)
    if best is None:
        return None
    source, (rows, used, currency) = best
    notes = []
    if source == "sec_ifrs":
        pl = {fy: v[1] for (u, fy), v in sec_series(facts_list, "ifrs-full", IFRS_PL_CONT).items()
              if u == currency}
        flips = fix_signs(rows, pl)
        if flips:
            notes.append("sign flipped: " + flips)
    return source, rows, used, currency, notes


def fix_signs(rows, pl):
    """IFRS sign sanity checks (in place). `pl` = {fy: profit from continuing operations}.

    * income tax expense is flipped when it is clearly the negative of (pre-tax - profit);
    * cash taxes paid tagged as a negative outflow are flipped when book tax is positive.
    Returns a short description of the flips made."""
    flips = []
    for fy, r in sorted(rows.items()):
        t, p = r.get("income_tax_expense"), r["pretax_income"]
        if t and fy in pl and "+" not in r["_pretax_income"]:
            implied = p - pl[fy]
            if abs(t + implied) < abs(t - implied) * 0.1:
                r["income_tax_expense"] = -t
                flips.append(f"tax{fy}")
        t, c = r.get("income_tax_expense"), r.get("cash_taxes_paid")
        if c is not None and c < 0 and t and t > 0 and abs(c) > 0.2 * t:
            r["cash_taxes_paid"] = -c
            flips.append(f"cash{fy}")
    return " ".join(flips)


def extract(series_fn, concepts):
    """Apply per-year concept priority. Returns ({fy: row}, {field: set(concepts)}, currency) or None."""
    cache = {}

    def series(concept):
        if concept not in cache:
            if isinstance(concept, tuple):  # largest value among alternatives
                merged = {}
                for c in concept:
                    for k, v in series(c).items():
                        if k not in merged or v[1] > merged[k][1]:
                            merged[k] = v + (c,)
                cache[concept] = merged
            elif "+" in concept:  # sum of components, same unit/year
                parts = [series(p) for p in concept.split("+")]
                keys = set(parts[0]).intersection(*parts[1:])
                cache[concept] = {k: (max(p[k][0] for p in parts), sum(p[k][1] for p in parts), parts[0][k][2])
                                  for k in keys}
            else:
                cache[concept] = series_fn(concept)
        return cache[concept]

    # reporting currency: the unit most used by pre-tax income facts
    counts = defaultdict(int)
    for c in concepts["pretax_income"]:
        for unit, _ in series(c):
            counts[unit] += 1
    if not counts:
        return None
    currency = max(counts, key=counts.get)
    rows, used = {}, defaultdict(set)
    for field in FIELDS:
        for c in concepts[field]:
            for (unit, fy), (_, val, _, *member) in series(c).items():
                if unit != currency or fy not in YEARS:
                    continue
                row = rows.setdefault(fy, {})
                if field not in row:
                    name = member[0] if member else c
                    row[field] = val
                    row["_" + field] = name
                    used[field].add(name)
    rows = {fy: r for fy, r in rows.items() if "pretax_income" in r}
    if not rows:
        return None
    return rows, used, currency


# ---------------------------------------------------------------- ESEF (filings.xbrl.org)
XBRL_ORG = "https://filings.xbrl.org"


def esef_filings(lei):
    data = _gz_json(RAW_TAX / "esef" / lei / "filings.json.gz",
                    lambda: get(f"{XBRL_ORG}/api/filings",
                                {"filter[entity.identifier]": lei, "page[size]": 200}))
    by_period = defaultdict(list)
    for f in data.get("data", []):
        a = f["attributes"]
        if a.get("json_url"):
            by_period[a["period_end"]].append(a)
    chosen = []
    for period, fs in by_period.items():
        # prefer English-language report, then fewest validation errors, then latest processed
        fs.sort(key=lambda a: ("-en" not in a["json_url"].lower(), a.get("error_count") or 0))
        chosen.append(fs[0])
    return sorted(chosen, key=lambda a: a["period_end"])


def _reduce_xbrl_json(raw):
    """Keep only non-dimensional facts (consolidated totals) to keep the cache small."""
    d = json.loads(raw)
    base = {"concept", "entity", "period", "unit", "language"}
    facts = {k: v for k, v in d.get("facts", {}).items() if set(v.get("dimensions", {})) <= base}
    return json.dumps({"documentInfo": {"documentType": d.get("documentInfo", {}).get("documentType")},
                       "facts": facts}).encode()


def esef_company(lei):
    filings = esef_filings(lei)
    if not filings:
        return None, 0
    # {concept_local: {(unit, fy): (filed, val, end)}}
    series = defaultdict(dict)
    for a in filings:
        path = RAW_TAX / "esef" / lei / (a["json_url"].rsplit("/", 1)[-1] + ".gz")
        url = XBRL_ORG + a["json_url"]
        try:
            doc = _gz_json(path, lambda: _reduce_xbrl_json(get(url)))
        except Exception as e:  # noqa: BLE001 - a broken filing should not stop the run
            print(f"  ESEF {lei} {a['period_end']}: {e}")
            continue
        filed = a.get("published") or a["period_end"]
        for v in doc.get("facts", {}).values():
            dim = v.get("dimensions", {})
            concept = dim.get("concept", "")
            if not concept.startswith("ifrs-full:") or "/" not in dim.get("period", ""):
                continue
            start, end = dim["period"].split("/")
            end_d = date.fromisoformat(end[:10])
            if end[11:19] == "00:00:00":  # xBRL-JSON end instants are exclusive midnights
                end_d = date.fromordinal(end_d.toordinal() - 1)
            if not is_annual(start, end_d.isoformat()):
                continue
            try:
                val = float(v["value"])
            except (TypeError, ValueError):
                continue
            unit = dim.get("unit", "").replace("iso4217:", "")
            key = (unit, fiscal_year(end_d))
            local = concept.split(":", 1)[1]
            cand = (filed, val, end_d.isoformat())
            if key not in series[local] or cand > series[local][key]:
                series[local][key] = cand
    res = extract(lambda c: series.get(c, {}), IFRS)
    notes = []
    if res is None:
        return None, len(filings)
    rows, used, currency = res
    pl = {fy: v[1] for (u, fy), v in series.get(IFRS_PL_CONT, {}).items() if u == currency}
    flips = fix_signs(rows, pl)
    if flips:
        notes.append("sign flipped: " + flips)
    return (rows, used, currency, notes), len(filings)


# ---------------------------------------------------------------- main
def fmt(v):
    if v is None or v == "":
        return ""
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:.2f}"


def ratio(num, den):
    if num is None or den is None or den <= 0:
        return ""
    return f"{num / den:.4f}"


def build():
    out_rows, src_rows = [], []
    for co in companies():
        cid, country = co["company_id"], co["hq_country"]
        ciks = [c for c in (co.get("sec_cik"), co.get("sec_cik_prior")) if c]
        lei = co.get("lei", "")
        print(cid, flush=True)
        per_year = {}  # fy -> (source, row, currency)
        if ciks:
            res = sec_company(ciks, cid)
            if res:
                source, rows, used, currency, notes = res
                for fy, r in rows.items():
                    per_year[fy] = (source, r, currency)
                src_rows.append({"company_id": cid, "source": source, "identifier": ";".join(ciks),
                                 "concepts": "; ".join(f"{k}={'|'.join(sorted(v))}" for k, v in used.items()),
                                 "years_found": years_str(rows),
                                 "note": "; ".join([f"currency {currency}"] + notes)})
            else:
                src_rows.append({"company_id": cid, "source": "sec", "identifier": ";".join(ciks),
                                 "concepts": "", "years_found": "",
                                 "note": "no annual pre-tax income facts in SEC companyfacts"})
        if lei and country != "US":
            res, nfil = esef_company(lei)
            if res:
                rows, used, currency, notes = res
                sec_cur = {v[2] for v in per_year.values()}
                added = {}
                if sec_cur and currency not in sec_cur:
                    notes.append(f"ESEF currency {currency} differs from SEC {'/'.join(sec_cur)}; not merged")
                else:
                    for fy, r in rows.items():
                        if fy not in per_year:
                            per_year[fy] = ("esef", r, currency)
                            added[fy] = r
                if per_year and len(added) < len(rows):
                    notes.append("used only for years missing from SEC: " + (years_str(added) or "none"))
                src_rows.append({"company_id": cid, "source": "esef", "identifier": lei,
                                 "concepts": "; ".join(f"{k}={'|'.join(sorted(v))}" for k, v in used.items()),
                                 "years_found": years_str(rows),
                                 "note": "; ".join([f"currency {currency}", f"{nfil} filings"] + notes)})
            else:
                why = (f"{nfil} ESEF filings but no ProfitLossBeforeTax facts" if nfil else
                       "no filings on filings.xbrl.org (DE issuers not indexed; CH outside ESEF)"
                       if country in ("DE", "CH") else "no filings on filings.xbrl.org")
                src_rows.append({"company_id": cid, "source": "esef", "identifier": lei,
                                 "concepts": "", "years_found": "", "note": why})
        if not ciks and not lei:
            src_rows.append({"company_id": cid, "source": "", "identifier": "", "concepts": "",
                             "years_found": "", "note": "no CIK or LEI"})
        if not per_year:
            src_rows.append({"company_id": cid, "source": "none", "identifier": "", "concepts": "",
                             "years_found": "", "note": "not available from SEC or ESEF XBRL"})
        for fy in sorted(per_year):
            source, r, currency = per_year[fy]
            p = r.get("pretax_income")
            out_rows.append({
                "company_id": cid, "fiscal_year": fy, "currency": currency,
                "revenue": fmt(r.get("revenue")), "pretax_income": fmt(p),
                "income_tax_expense": fmt(r.get("income_tax_expense")),
                "cash_taxes_paid": fmt(r.get("cash_taxes_paid")),
                "book_etr": ratio(r.get("income_tax_expense"), p),
                "cash_etr": ratio(r.get("cash_taxes_paid"), p),
                "source": source, "statutory_rate_home": statutory(country, fy),
            })
    write_csv(CLEAN / "tax.csv", out_rows,
              ["company_id", "fiscal_year", "currency", "revenue", "pretax_income", "income_tax_expense",
               "cash_taxes_paid", "book_etr", "cash_etr", "source", "statutory_rate_home"])
    write_csv(CLEAN / "tax_sources.csv", src_rows,
              ["company_id", "source", "identifier", "concepts", "years_found", "note"])
    print(f"wrote {len(out_rows)} rows for {len({r['company_id'] for r in out_rows})} companies")
    return out_rows


def years_str(rows):
    ys = sorted(rows)
    if not ys:
        return ""
    parts, start, prev = [], ys[0], ys[0]
    for y in ys[1:] + [None]:
        if y != (prev + 1 if prev is not None else None):
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = y
        prev = y
    return ",".join(parts)


if __name__ == "__main__":
    build()
