"""Resolve each seed company to its GLEIF LEI and SEC CIK.

Writes data/influence/companies.csv. Every match records how it was made so a
reviewer can check it; `lei_override` / `cik_override` in the seed win.
"""

import json
import re

from influence.common import DATA, RAW, SEC_USER_AGENT, cached, get, get_json, read_csv, write_csv

GLEIF = "https://api.gleif.org/api/v1/lei-records"

# US listings (ADRs and foreign filers) for companies whose home ticker differs.
SEC_TICKER = {
    "sap": "SAP", "shell": "SHEL", "bp": "BP", "astrazeneca": "AZN", "gsk": "GSK", "hsbc": "HSBC",
    "barclays": "BCS", "bat": "BTI", "riotinto": "RIO", "novartis": "NVS", "ubs": "UBS", "ing": "ING",
    "philips": "PHG", "unilever": "UL", "sanofi": "SNY", "totalenergies": "TTE", "equinor": "EQNR",
    "novonordisk": "NVO", "ericsson": "ERIC", "nokia": "NOK", "santander": "SAN", "asml": "ASML",
    "deutschebank": "DB", "stellantis": "STLA", "spotify": "SPOT", "eni": "E",
}


def norm(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def gleif_candidates(name):
    out = []
    for flt in ({"filter[entity.legalName]": name}, {"filter[fulltext]": name}):
        d = get_json(GLEIF, {**flt, "page[size]": 20})
        out += d.get("data", [])
        if out:
            return out, "legalName" if "filter[entity.legalName]" in flt else "fulltext"
    return out, "none"


def pick(cands, company):
    def score(rec):
        a = rec["attributes"]
        e = a["entity"]
        s = 0
        if norm(e["legalName"]["name"]) == norm(company["legal_name"]):
            s += 10
        if e["legalAddress"]["country"] == company["hq_country"] or e["headquartersAddress"]["country"] == company["hq_country"]:
            s += 5
        if a["registration"]["status"] == "ISSUED":
            s += 3
        if e.get("status") == "ACTIVE":
            s += 2
        return s
    best = max(cands, key=score)
    return best, score(best)


def main():
    seed = read_csv(DATA / "companies_seed.csv")
    sec = json.loads(cached(RAW / "sec" / "company_tickers.json",
                            lambda: get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_USER_AGENT})))
    by_ticker = {v["ticker"].upper(): v for v in sec.values()}

    rows = []
    for c in seed:
        cands, how = gleif_candidates(c["legal_name"])
        lei = lei_name = lei_country = ""
        conf = 0
        if cands:
            rec, conf = pick(cands, c)
            lei = rec["id"]
            lei_name = rec["attributes"]["entity"]["legalName"]["name"]
            lei_country = rec["attributes"]["entity"]["legalAddress"]["country"]
        if c.get("lei_override") == "NONE":
            # GLEIF reports the parent under a "NO_LEI" exception (e.g. Chevron Corporation).
            lei = lei_name = lei_country = ""
            how, conf = "no_lei", 99
        elif c.get("lei_override"):
            rec = get_json(f"{GLEIF}/{c['lei_override']}")["data"]
            lei, how, conf = rec["id"], "override", 99
            lei_name = rec["attributes"]["entity"]["legalName"]["name"]
            lei_country = rec["attributes"]["entity"]["legalAddress"]["country"]

        t = SEC_TICKER.get(c["company_id"], c["ticker"] if c["hq_country"] == "US" else "")
        s = by_ticker.get(t.upper()) if t else None
        rows.append({
            **{k: c[k] for k in ("company_id", "name", "legal_name", "hq_country", "sector")},
            "lei": lei, "lei_legal_name": lei_name, "lei_country": lei_country,
            "lei_match": how, "lei_score": conf,
            "sec_cik": str(s["cik_str"]).zfill(10) if s else "", "sec_title": s["title"] if s else "",
            "sec_cik_prior": c.get("sec_cik_prior", ""),
        })
        print(f'{c["company_id"]:16} {lei:22} {how:9} {conf:3} {lei_name[:45]:45} {rows[-1]["sec_cik"]}')
    write_csv(DATA / "companies.csv", rows)


if __name__ == "__main__":
    main()
