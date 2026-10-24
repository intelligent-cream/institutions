"""Subsidiaries by jurisdiction from GLEIF Level 2 (relationship) data.

For each company LEI we page through
    https://api.gleif.org/api/v1/lei-records/{LEI}/ultimate-children
    https://api.gleif.org/api/v1/lei-records/{LEI}/direct-children
and take the union of the child LEI records. Each child is located by
legalAddress.country (also recorded: the country part of entity.jurisdiction).

Writes
    data/influence/clean/subsidiaries.csv             one row per company
    data/influence/clean/subsidiaries_by_country.csv  company_id, country, n

Tax havens -- Tørsløv, Wier & Zucman, "The Missing Profits of Nations",
Review of Economic Studies 90(3), 2023 (working paper version of 8 April 2022,
https://gabriel-zucman.eu/files/TWZ2022.pdf, section 3.1 "Definition of tax
havens" and footnote 10). They use 41 countries/territories: 5 OECD countries
(Belgium, Ireland, Luxembourg, Netherlands, Switzerland) plus 36 non-OECD ones
taken from Hines & Rice (1994), with Netherlands, Belgium and Puerto Rico added.
The paper's "Netherlands Antilles" (dissolved 2010) is mapped to its successor
ISO codes CW (Curaçao), SX (Sint Maarten) and BQ (Bonaire, Sint Eustatius, Saba).

Caveats
  * GLEIF Level 2 only covers children that (a) have an LEI and (b) report the
    parent relationship (accounting-consolidation basis). Many subsidiaries --
    especially small holding/finance vehicles in Caribbean havens that never
    need an LEI -- are missing, so counts are a lower bound and coverage varies
    by company and country (EU/US entities are over-represented because LEIs
    are required for EU/US financial reporting and trading).
  * Only children whose entity.status is ACTIVE are counted. low_coverage = 1
    flags companies with fewer than 10 such children (shares are unreliable).
  * n_in_tax_havens_by_jurisdiction uses entity.jurisdiction (place of
    incorporation) instead of the legal-address country; the two rarely differ.
  * Where companies.csv holds a BRANCH LEI (Deutsche Bank, BNP Paribas,
    Santander) the head-office legal entity is used instead (lei_used).
  * Companies whose own record has no reporting children (e.g. Safran, Altria
    at the time of writing) get n_subsidiaries_with_lei = 0; that is a GLEIF
    reporting gap, not evidence of no subsidiaries.
  * A parent domiciled in a haven (NL, CH, LU, IE ...) has domestic children
    there, hence n_in_tax_havens_excluding_home (home = parent's LEI legal
    address country, falling back to hq_country).
"""

import json

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

GLEIF = "https://api.gleif.org/api/v1/lei-records"
OUT = RAW / "subsidiaries"
PAGE = 200

# ISO 3166-1 alpha-2 codes of the 41 TWZ (2023) tax havens.
TAX_HAVENS = {
    # 5 OECD havens
    "BE": "Belgium", "IE": "Ireland", "LU": "Luxembourg", "NL": "Netherlands", "CH": "Switzerland",
    # 36 non-OECD havens (TWZ footnote 10)
    "AD": "Andorra", "AI": "Anguilla", "AG": "Antigua and Barbuda", "AW": "Aruba", "BS": "Bahamas",
    "BH": "Bahrain", "BB": "Barbados", "BZ": "Belize", "BM": "Bermuda", "VG": "British Virgin Islands",
    "KY": "Cayman Islands", "CY": "Cyprus", "GI": "Gibraltar", "GD": "Grenada", "GG": "Guernsey",
    "HK": "Hong Kong", "IM": "Isle of Man", "JE": "Jersey", "LB": "Lebanon", "LI": "Liechtenstein",
    "MO": "Macau", "MT": "Malta", "MH": "Marshall Islands", "MU": "Mauritius", "MC": "Monaco",
    "CW": "Netherlands Antilles (Curaçao)", "SX": "Netherlands Antilles (Sint Maarten)",
    "BQ": "Netherlands Antilles (Bonaire, Sint Eustatius, Saba)",
    "PA": "Panama", "PR": "Puerto Rico", "WS": "Samoa", "SC": "Seychelles", "SG": "Singapore",
    "KN": "Saint Kitts and Nevis", "LC": "Saint Lucia", "VC": "Saint Vincent and the Grenadines",
    "TC": "Turks and Caicos Islands", "VU": "Vanuatu",
}


def children(lei, rel):
    """All child LEI records for `rel` in {'ultimate-children', 'direct-children'}."""
    out, page = [], 1
    while True:
        path = OUT / f"{lei}_{rel}_p{page}.json"
        url = f"{GLEIF}/{lei}/{rel}"
        params = {"page[size]": PAGE, "page[number]": page}
        try:
            d = json.loads(cached(path, lambda: get(url, params)))
        except Exception as e:  # noqa: BLE001 - GLEIF returns 404 when there are no children
            if getattr(e, "code", None) == 404:
                return out
            raise
        out += d.get("data", [])
        last = d.get("meta", {}).get("pagination", {}).get("lastPage", 1)
        if page >= last:
            return out
        page += 1


def resolve(lei):
    """Return (lei_to_use, note). Branch LEIs (category BRANCH) have no children;
    follow their head-office relationship to the legal entity."""
    rec = json.loads(cached(OUT / f"{lei}_record.json", lambda: get(f"{GLEIF}/{lei}")))["data"]
    if rec["attributes"]["entity"].get("category") != "BRANCH":
        return lei, ""
    ho = json.loads(cached(OUT / f"{lei}_head-office.json",
                           lambda: get(f"{GLEIF}/{lei}/head-office")))["data"]
    name = ho["attributes"]["entity"]["legalName"]["name"]
    return ho["id"], f"companies.csv LEI {lei} is a BRANCH record; used head office {ho['id']} ({name})"


def main():
    summary, by_country_rows = [], []
    for c in companies():
        cid, lei = c["company_id"], c["lei"].strip()
        home = c.get("lei_country") or c["hq_country"]
        row = {"company_id": cid, "lei": lei, "home_country": home}
        if not lei:
            row["note"] = "no LEI for company; GLEIF relationship data unavailable"
            summary.append(row)
            print(f"{cid:16} no LEI")
            continue
        lei, note = resolve(lei)
        row["lei_used"] = lei
        recs = {}
        for rel in ("ultimate-children", "direct-children"):
            for r in children(lei, rel):
                recs.setdefault(r["id"], r)
        recs.pop(lei, None)
        active = [r for r in recs.values() if r["attributes"]["entity"].get("status") == "ACTIVE"]
        countries, juris_haven = {}, 0
        for r in active:
            e = r["attributes"]["entity"]
            cc = (e["legalAddress"].get("country") or "").upper() or "XX"
            countries[cc] = countries.get(cc, 0) + 1
            j = (e.get("jurisdiction") or "").split("-")[0].upper()
            if j in TAX_HAVENS:
                juris_haven += 1
        n = len(active)
        havens = {k: v for k, v in countries.items() if k in TAX_HAVENS}
        n_hav = sum(havens.values())
        n_hav_x = sum(v for k, v in havens.items() if k != home)
        row.update({
            "n_subsidiaries_with_lei": n,
            "n_inactive_excluded": len(recs) - n,
            "n_in_tax_havens": n_hav,
            "share_in_tax_havens": round(n_hav / n, 4) if n else "",
            "n_in_tax_havens_excluding_home": n_hav_x,
            "share_in_tax_havens_excluding_home": round(n_hav_x / n, 4) if n else "",
            "n_in_tax_havens_by_jurisdiction": juris_haven,
            "n_countries": len(countries),
            "low_coverage": int(n < 10),
            "n_domestic": countries.get(home, 0),
            "top_haven_jurisdictions": ";".join(f"{k}:{v}" for k, v in
                                                sorted(havens.items(), key=lambda kv: (-kv[1], kv[0]))),
            "note": "; ".join(x for x in (note, "" if n else "no active children reporting this LEI as parent in GLEIF") if x),
        })
        summary.append(row)
        for k, v in sorted(countries.items(), key=lambda kv: (-kv[1], kv[0])):
            by_country_rows.append({"company_id": cid, "country": k, "n": v,
                                    "tax_haven": int(k in TAX_HAVENS)})
        print(f"{cid:16} n={n:5} havens={n_hav:4} ex-home={n_hav_x:4} countries={len(countries)}")

    fields = ["company_id", "lei", "lei_used", "home_country", "n_subsidiaries_with_lei", "n_inactive_excluded",
              "n_in_tax_havens", "share_in_tax_havens", "n_in_tax_havens_excluding_home",
              "share_in_tax_havens_excluding_home", "n_in_tax_havens_by_jurisdiction", "n_countries", "low_coverage",
              "n_domestic", "top_haven_jurisdictions", "note"]
    write_csv(CLEAN / "subsidiaries.csv", summary, fields)
    write_csv(CLEAN / "subsidiaries_by_country.csv", by_country_rows,
              ["company_id", "country", "n", "tax_haven"])


if __name__ == "__main__":
    main()
