"""Descriptive tests on the company profiles and panel.

Writes data/influence/findings/*.csv and data/influence/findings/FINDINGS.md.

Every cross-company test is a Spearman rank correlation with a permutation
p-value (10,000 shuffles). Each test is run twice: on raw ranks and on ranks
taken within sector (comparing a firm only with firms in its own sector), since
sectors differ widely in both tax rates and lobbying. With roughly 100 firms
these are associations, not causal effects.
"""

import random
from collections import defaultdict

from influence.common import DATA, read_csv, write_csv

FINDINGS = DATA / "findings"
EXTRACTIVE = {"Energy", "Mining"}  # resource taxes dominate their tax rates
N_PERM = 10_000
random.seed(20261024)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def within_group_ranks(xs, groups):
    """Percentile rank of each value inside its group, centred on 0."""
    out = [0.0] * len(xs)
    by = defaultdict(list)
    for i, g in enumerate(groups):
        by[g].append(i)
    for idx in by.values():
        if len(idx) < 2:
            continue
        r = ranks([xs[i] for i in idx])
        for i, ri in zip(idx, r):
            out[i] = ri / (len(idx) - 1) - 0.5
    return out


def perm_test(a, b, groups=None):
    obs = pearson(a, b)
    hits = 0
    b2 = list(b)
    for _ in range(N_PERM):
        if groups is None:
            random.shuffle(b2)
        else:  # shuffle within sector to keep the sector structure
            by = defaultdict(list)
            for i, g in enumerate(groups):
                by[g].append(i)
            for idx in by.values():
                vals = [b[i] for i in idx]
                random.shuffle(vals)
                for i, v in zip(idx, vals):
                    b2[i] = v
        if abs(pearson(a, b2)) >= abs(obs):
            hits += 1
    return obs, (hits + 1) / (N_PERM + 1)


def correlate(rows, x, y, label, exclude=None, note=""):
    pts = [r for r in rows if num(r[x]) is not None and num(r[y]) is not None and not (exclude and exclude(r))]
    if len(pts) < 10:
        return {"test": label, "x": x, "y": y, "n": len(pts), "note": "too few companies"}
    xs, ys, gs = [num(r[x]) for r in pts], [num(r[y]) for r in pts], [r["sector"] for r in pts]
    rho, p = perm_test(ranks(xs), ranks(ys))
    rho_s, p_s = perm_test(within_group_ranks(xs, gs), within_group_ranks(ys, gs), gs)
    return {"test": label, "x": x, "y": y, "n": len(pts), "spearman_rho": round(rho, 3), "p_value": round(p, 4),
            "within_sector_rho": round(rho_s, 3), "within_sector_p": round(p_s, 4), "note": note}

SOURCES = [
    ("us_lobby", "year", "year", "US federal lobbying", "Lobbying Disclosure Act filings (House Clerk bulk XML)", "https://lda.gov/"),
    ("eu_lobby", "year", "year", "EU lobbying and Commission meetings", "EU Transparency Register snapshots; Integrity Watch EU meetings", "https://transparency-register.europa.eu/"),
    ("national_lobby", "year", "year", "German and French lobby registers", "Bundestag Lobbyregister; HATVP répertoire", "https://www.lobbyregister.bundestag.de/"),
    ("us_pac", "cycle", "cycle", "US corporate PAC giving", "FEC bulk data (committee, candidate, contribution files)", "https://www.fec.gov/data/browse-data/"),
    ("tax", "fiscal year", "fiscal_year", "Effective tax rates", "SEC XBRL company facts; ESEF filings via filings.xbrl.org", "https://www.sec.gov/edgar/sec-api-documentation"),
    ("us_contracts", "fiscal year", "fiscal_year", "US federal contracts", "USAspending.gov API", "https://api.usaspending.gov/"),
    ("subsidiaries", "snapshot", None, "Subsidiaries and tax havens", "GLEIF Level 2 relationship data", "https://www.gleif.org/"),
]


def source_coverage():
    rows = []
    for key, unit, ycol, title, origin, url in SOURCES:
        p = DATA / "clean" / f"{key}.csv"
        if not p.exists():
            continue
        data = read_csv(p)
        ids = {r["company_id"] for r in data if any(v not in ("", "0", "0.0") for k, v in r.items()
                                                    if k not in ("company_id", ycol, "country", "lei", "lei_used", "home_country"))}
        if key == "subsidiaries":
            ids = {r["company_id"] for r in data if (num(r["n_subsidiaries_with_lei"]) or 0) > 0}
        years = sorted({int(r[ycol]) for r in data if ycol and r.get(ycol)})
        rows.append({"source_id": key, "title": title, "origin": origin, "url": url, "companies": len(ids),
                     "first_year": years[0] if years else "", "last_year": years[-1] if years else "", "unit": unit,
                     "rows": len(data)})
    write_csv(FINDINGS / "source_coverage.csv", rows)


def main():
    prof = read_csv(DATA / "company_profile.csv")
    panel = read_csv(DATA / "company_year.csv")
    FINDINGS.mkdir(parents=True, exist_ok=True)


    tests = [
        correlate(prof, "lobby_per_bn_revenue_usd", "book_etr", "Lobbying intensity vs book tax rate",
                  exclude=lambda r: r["sector"] in EXTRACTIVE, note="excludes energy and mining"),
        correlate(prof, "lobby_per_bn_revenue_usd", "cash_etr", "Lobbying intensity vs cash tax rate",
                  exclude=lambda r: r["sector"] in EXTRACTIVE, note="excludes energy and mining"),
        correlate(prof, "lobby_per_bn_revenue_usd", "etr_gap", "Lobbying intensity vs gap below home statutory rate",
                  exclude=lambda r: r["sector"] in EXTRACTIVE, note="excludes energy and mining"),
        correlate(prof, "lobby_total_usd", "haven_share_ex_home", "Total lobbying vs share of subsidiaries in tax havens",
                  exclude=lambda r: r["subsidiary_low_coverage"] in ("1", "True", "true"),
                  note="excludes firms with low GLEIF coverage"),
        correlate(prof, "lobby_total_usd", "n_havens_ex_home", "Total lobbying vs number of tax-haven subsidiaries",
                  exclude=lambda r: r["subsidiary_low_coverage"] in ("1", "True", "true"),
                  note="excludes firms with low GLEIF coverage"),
        correlate(prof, "lobby_per_bn_revenue_usd", "havens_per_bn_revenue",
                  "Lobbying vs tax-haven subsidiaries, both per $bn revenue (size-adjusted)",
                  exclude=lambda r: r["subsidiary_low_coverage"] in ("1", "True", "true"),
                  note="excludes firms with low GLEIF coverage"),
        correlate(prof, "us_lobby_usd", "us_contracts_avg_usd", "US lobbying vs US federal contracts"),
        correlate(prof, "us_lobby_usd", "contracts_share_of_revenue", "US lobbying vs contracts as share of revenue"),
        correlate(prof, "pac_2020_2024_usd", "book_etr", "PAC giving vs book tax rate",
                  exclude=lambda r: r["sector"] in EXTRACTIVE or not num(r["pac_2020_2024_usd"]),
                  note="US PAC givers only; excludes energy and mining"),
        correlate(prof, "eu_lobby_usd", "ec_meetings", "EU declared spend vs Commission meetings (sanity check)"),
    ]
    write_csv(FINDINGS / "correlations.csv", tests,
              ["test", "x", "y", "n", "spearman_rho", "p_value", "within_sector_rho", "within_sector_p", "note"])

    # Washington vs Brussels over time: firms present in both series in every year compared.
    by_year = defaultdict(lambda: defaultdict(float))
    years = range(2016, 2025)
    firms = {r["company_id"] for r in panel}
    hq = {r["company_id"]: r["hq_region"] for r in prof}
    sector = {r["company_id"]: r["sector"] for r in prof}
    pv = {(r["company_id"], int(r["year"])): r for r in panel}
    balanced = [f for f in firms if all(num(pv.get((f, y), {}).get("eu_lobby_usd")) is not None and
                                        num(pv.get((f, y), {}).get("us_lobby_usd")) is not None for y in years)]
    trend = []
    for y in years:
        for f in balanced:
            row = pv[(f, y)]
            for grp in ("All", hq[f], sector[f]):
                by_year[(grp, y)]["us"] += num(row["us_lobby_usd"])
                by_year[(grp, y)]["eu"] += num(row["eu_lobby_usd"])
    for (grp, y), v in sorted(by_year.items()):
        trend.append({"group": grp, "year": y, "us_lobby_usd": round(v["us"]), "eu_lobby_usd": round(v["eu"]),
                      "eu_share": round(v["eu"] / (v["us"] + v["eu"]), 4) if v["us"] + v["eu"] else ""})
    write_csv(FINDINGS / "us_eu_trend.csv", trend)

    # Per-firm shift in the EU share of US+EU lobbying, 2016-17 vs 2023-24.
    shifts = []
    for f in balanced:
        def share(ys):
            us = sum(num(pv[(f, y)]["us_lobby_usd"]) for y in ys)
            eu = sum(num(pv[(f, y)]["eu_lobby_usd"]) for y in ys)
            return eu / (us + eu) if us + eu else None
        a, b = share((2016, 2017)), share((2023, 2024))
        if a is not None and b is not None:
            shifts.append({"company_id": f, "sector": sector[f], "hq_region": hq[f], "eu_share_2016_17": round(a, 3),
                           "eu_share_2023_24": round(b, 3), "change": round(b - a, 3)})
    shifts.sort(key=lambda r: -r["change"])
    write_csv(FINDINGS / "us_eu_shift_by_company.csv", shifts)

    # Cross-border reach: where do US and European firms lobby?
    reach = []
    for region in ("US", "Europe"):
        grp = [r for r in prof if r["hq_region"] == region]
        def cnt(field):
            return sum(1 for r in grp if num(r[field]))
        def tot(field):
            return round(sum(num(r[field]) or 0 for r in grp))
        reach.append({"hq_region": region, "n": len(grp),
                      "lobby_us": cnt("us_lobby_usd"), "lobby_eu": cnt("eu_lobby_usd"), "lobby_de": cnt("de_lobby_usd"),
                      "lobby_fr": cnt("fr_lobby_usd"), "us_pac": cnt("pac_2020_2024_usd"),
                      "us_lobby_usd": tot("us_lobby_usd"), "eu_lobby_usd": tot("eu_lobby_usd"),
                      "de_lobby_usd": tot("de_lobby_usd"), "fr_lobby_usd": tot("fr_lobby_usd")})
    write_csv(FINDINGS / "cross_border_reach.csv", reach)
    source_coverage()

    lines = ["# Findings (generated by influence/analysis.py)", "",
             f"Companies: {len(prof)}. Window: 2019–2024. Firms in the US–EU trend: {len(balanced)} (present in both series every year 2016–2024).", "",
             "## Correlations", "",
             "| Test | n | rho | p | within-sector rho | p | note |", "|---|---|---|---|---|---|---|"]
    for t in tests:
        lines.append(f"| {t['test']} | {t['n']} | {t.get('spearman_rho', '')} | {t.get('p_value', '')} | "
                     f"{t.get('within_sector_rho', '')} | {t.get('within_sector_p', '')} | {t.get('note', '')} |")
    allt = [t for t in trend if t["group"] == "All"]
    if allt:
        lines += ["", "## Washington vs Brussels", "",
                  f"EU share of combined US+EU lobbying: {allt[0]['eu_share']:.1%} in {allt[0]['year']} → "
                  f"{allt[-1]['eu_share']:.1%} in {allt[-1]['year']}."]
    (FINDINGS / "FINDINGS.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
