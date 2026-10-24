"""Merge the per-source tables into one panel and one profile per company.

Inputs:  data/influence/clean/<source>.csv (written by influence/sources/*)
Outputs: data/influence/company_year.csv  — one row per company per year
         data/influence/company_profile.csv — one row per company, 2019–2024 window

Money is converted to USD with ECB annual averages (fx.csv). EU and national
register costs are bracket midpoints. PAC money is kept separate from lobbying
spend: it comes from employees, not from the company's own funds.
"""

from collections import defaultdict

from influence.common import CLEAN, DATA, companies, read_csv, write_csv

WINDOW = range(2019, 2025)  # 2019–2024 inclusive
PAC_CYCLES = (2020, 2022, 2024)
MIN_PRETAX_MARGIN = 0.02    # ignore tax rates when pooled pretax income is below 2% of revenue


def load(name):
    p = CLEAN / f"{name}.csv"
    return read_csv(p) if p.exists() else []


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def rnd(x, n=0):
    return "" if x is None else round(x, n) if n else round(x)


def main():
    fx = {(int(r["year"]), r["currency"]): float(r["usd_per_unit"]) for r in load("fx")}
    first_fx_year, last_fx_year = min(y for y, _ in fx), max(y for y, _ in fx)

    def usd(amount, currency, year):
        if amount is None:
            return None
        return amount * fx[(max(first_fx_year, min(int(year), last_fx_year)), currency)]

    comps = companies()
    panel = defaultdict(dict)  # (company_id, year) -> fields

    # EU register: keep one value per cost year (the latest snapshot that reports it).
    eu_cost = {}
    for r in sorted(load("eu_lobby"), key=lambda r: r["snapshot"] or r["year"]):
        cid = r["company_id"]
        if r["cost_year"] and num(r["eu_lobby_cost_mid_eur"]) is not None:
            eu_cost[(cid, int(r["cost_year"]))] = num(r["eu_lobby_cost_mid_eur"])
        y = int(r["year"])
        if num(r["ec_meetings"]) is not None:
            panel[(cid, y)]["ec_meetings"] = num(r["ec_meetings"])
        if num(r["eu_lobbyists_fte"]) is not None:
            panel[(cid, y)]["eu_lobbyists_fte"] = num(r["eu_lobbyists_fte"])
    for (cid, y), v in eu_cost.items():
        panel[(cid, y)]["eu_lobby_usd"] = usd(v, "EUR", y)

    for r in load("us_lobby"):
        p = panel[(r["company_id"], int(r["year"]))]
        p["us_lobby_usd"] = num(r.get("us_lobby_spend_usd"))
        p["us_lobbyists"] = num(r.get("n_lobbyists"))
        p["us_lobbyists_former_gov"] = num(r.get("n_lobbyists_former_gov"))
        p["us_bills"] = num(r.get("n_bills_mentioned"))

    for r in load("national_lobby"):
        key = "de_lobby_usd" if r["country"] == "DE" else "fr_lobby_usd"
        v = usd(num(r["lobby_cost_mid_eur"]), "EUR", r["year"])
        if v is not None:
            panel[(r["company_id"], int(r["year"]))][key] = v

    for r in load("us_pac"):
        p = panel[(r["company_id"], int(r["cycle"]))]
        p["pac_usd"] = num(r["pac_to_candidates_usd"])
        p["pac_rep_usd"] = num(r["pac_to_rep_usd"])
        p["pac_dem_usd"] = num(r["pac_to_dem_usd"])

    for r in load("tax"):
        y = int(r["fiscal_year"])
        p = panel[(r["company_id"], y)]
        for k in ("revenue", "pretax_income", "income_tax_expense", "cash_taxes_paid"):
            p[k + "_usd"] = usd(num(r[k]), r["currency"], y)
        p["book_etr"] = num(r["book_etr"])
        p["statutory_rate"] = num(r["statutory_rate_home"])

    for r in load("us_contracts"):
        p = panel[(r["company_id"], int(r["fiscal_year"]))]
        p["us_contracts_usd"] = num(r.get("contract_obligations_usd"))

    year_fields = ["us_lobby_usd", "eu_lobby_usd", "de_lobby_usd", "fr_lobby_usd", "ec_meetings", "eu_lobbyists_fte",
                   "us_lobbyists", "us_lobbyists_former_gov", "us_bills", "pac_usd", "pac_rep_usd", "pac_dem_usd",
                   "revenue_usd", "pretax_income_usd", "income_tax_expense_usd", "cash_taxes_paid_usd", "book_etr",
                   "statutory_rate", "us_contracts_usd"]
    panel_rows = []
    for (cid, y) in sorted(panel):
        row = {"company_id": cid, "year": y}
        for f in year_fields:
            v = panel[(cid, y)].get(f)
            row[f] = rnd(v, 4) if f in ("book_etr", "statutory_rate") else rnd(v)
        panel_rows.append(row)
    write_csv(DATA / "company_year.csv", panel_rows, ["company_id", "year"] + year_fields)

    subs = {r["company_id"]: r for r in load("subsidiaries")}
    profiles = []
    for c in comps:
        cid = c["company_id"]
        w = [panel.get((cid, y), {}) for y in WINDOW]

        def wmean(f):
            return mean([d.get(f) for d in w])

        def wsum(f):
            vals = [d.get(f) for d in w if d.get(f) is not None]
            return sum(vals) if vals else None

        us = wmean("us_lobby_usd")
        eu = wmean("eu_lobby_usd")
        fr = wmean("fr_lobby_usd")
        de_years = sorted(y for (k, y) in panel if k == cid and panel[(k, y)].get("de_lobby_usd") is not None)
        de = mean([panel[(cid, y)]["de_lobby_usd"] for y in de_years]) if de_years else None
        parts = [x for x in (us, eu, fr, de) if x is not None]
        lobby_total = sum(parts) if parts else None

        pac = [panel.get((cid, y), {}) for y in PAC_CYCLES]
        pac_total = sum(d.get("pac_usd") or 0 for d in pac) if any(d.get("pac_usd") is not None for d in pac) else None
        pac_rep = sum(d.get("pac_rep_usd") or 0 for d in pac)
        pac_dem = sum(d.get("pac_dem_usd") or 0 for d in pac)

        pretax, tax, cash, rev = wsum("pretax_income_usd"), wsum("income_tax_expense_usd"), wsum("cash_taxes_paid_usd"), wsum("revenue_usd")
        etr_ok = pretax is not None and tax is not None and pretax > 0 and (not rev or pretax / rev >= MIN_PRETAX_MARGIN)
        book_etr = tax / pretax if etr_ok else None
        cash_etr = cash / pretax if etr_ok and cash is not None else None
        statutory = wmean("statutory_rate")
        rev_avg = wmean("revenue_usd")

        s = subs.get(cid, {})
        profiles.append({
            "company_id": cid, "name": c["name"], "hq_country": c["hq_country"], "sector": c["sector"],
            "hq_region": "US" if c["hq_country"] == "US" else "Europe",
            "us_lobby_usd": rnd(us), "eu_lobby_usd": rnd(eu), "fr_lobby_usd": rnd(fr), "de_lobby_usd": rnd(de),
            "lobby_total_usd": rnd(lobby_total),
            "us_share_of_us_eu_lobby": rnd(us / (us + eu), 3) if us is not None and eu is not None and us + eu > 0 else "",
            "lobby_per_bn_revenue_usd": rnd(lobby_total / (rev_avg / 1e9)) if lobby_total and rev_avg else "",
            "ec_meetings": rnd(wsum("ec_meetings")),
            "eu_lobbyists_fte": rnd(wmean("eu_lobbyists_fte"), 1),
            "us_lobbyists_former_gov_share": rnd(wsum("us_lobbyists_former_gov") / wsum("us_lobbyists"), 3)
            if wsum("us_lobbyists") else "",
            "pac_2020_2024_usd": rnd(pac_total),
            "pac_share_rep": rnd(pac_rep / (pac_rep + pac_dem), 3) if pac_rep + pac_dem > 0 else "",
            "revenue_avg_usd": rnd(rev_avg),
            "book_etr": rnd(book_etr, 4), "cash_etr": rnd(cash_etr, 4), "statutory_rate": rnd(statutory, 4),
            "etr_gap": rnd(statutory - book_etr, 4) if statutory is not None and book_etr is not None else "",
            "us_contracts_avg_usd": rnd(wmean("us_contracts_usd")),
            "contracts_per_lobby_usd": rnd(wmean("us_contracts_usd") / us) if us and wmean("us_contracts_usd") else "",
            "n_subsidiaries_with_lei": s.get("n_subsidiaries_with_lei", ""),
            "haven_share_ex_home": s.get("share_in_tax_havens_excluding_home", ""),
            "n_havens_ex_home": s.get("n_in_tax_havens_excluding_home", ""),
            "top_havens": s.get("top_haven_jurisdictions", ""),
            "subsidiary_low_coverage": s.get("low_coverage", ""),
        })
    write_csv(DATA / "company_profile.csv", profiles)
    print(f"wrote company_year.csv ({len(panel_rows)} rows) and company_profile.csv ({len(profiles)} rows)")


if __name__ == "__main__":
    main()
