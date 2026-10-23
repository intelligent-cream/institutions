"""Estimate the tax revenue missed because the richest pay lower effective rates.

For every income group above the 99th percentile:

    shortfall = income of the group x max(0, floor rate - rate the group pays)

Two floor rates are tested:
  * "p90_99": the income-weighted rate paid by the P90-99 group (high earners
    whose income is mostly salary).
  * "peak": the highest rate any group within the top 10% pays, i.e. the
    point where the system stops being progressive.

The result is static: it ignores behavioural responses such as avoidance,
emigration or lower investment, so it is an upper bound on what a reform
would raise. Inputs and their sources are listed in data/tax-at-the-top/.
"""

import csv
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "tax-at-the-top"
TOP_GROUPS_START = 11  # group_order of P99-99.9 in the four-country rates file

# Macro (all taxes / national income) rates published by the studies;
# Italy and the US are recomputed from the full distribution below.
PUBLISHED_MACRO_RATE = {"Netherlands": 0.45}
# Use the study's own total where its group incomes come from the study.
NATIONAL_INCOME_OVERRIDE = {"Netherlands": 591.6}  # Bruil et al. Table 2: 13,332,368 adults x EUR 44,371


def read(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def national_income_current(ni_rows, country, year):
    for r in ni_rows:
        if r["country"] == country and int(r["year"]) == year:
            return float(r["national_income_bn_2025_prices"]) * float(r["price_index_2025_eq_1"])
    raise KeyError((country, year))


def main():
    rates = read("effective_tax_rate_by_income_group.csv")
    incomes = read("income_by_group.csv")
    ni_rows = read("national_income.csv")
    fr_rates = {r["group"]: float(r["rate"]) for r in read("france_band_rates.csv")}

    rate_of = {(r["country"], r["group"]): float(r["rate"]) for r in rates if not r["note"].startswith("Illustrative")}
    # Italy's study reports the whole top 0.1% as one point.
    rate_of[("Italy", "P99.9-100")] = rate_of[("Italy", "P99.9-99.99")]
    # The US WID series stops at the top 0.001%; average the two top points of Saez & Zucman.
    rate_of[("United States", "P99.999-100")] = (rate_of[("United States", "P99.999-99.9999")] + rate_of[("United States", "P99.9999-100")]) / 2
    # France uses the IPP study throughout, so its incomes and rates share one definition.
    rate_of = {k: v for k, v in rate_of.items() if k[0] != "France"}
    for g, v in fr_rates.items():
        rate_of[("France", g)] = v
    fr_peak = max(float(r["economic_income_rate"]) for r in read("france_tax_rate_by_income_definition.csv"))

    by_country = defaultdict(list)
    for r in incomes:
        by_country[r["country"]].append(r)

    band_rows, summary_rows = [], []
    for country, rows in by_country.items():
        year = int(rows[0]["year"])
        ni = NATIONAL_INCOME_OVERRIDE.get(country) or national_income_current(ni_rows, country, year)
        # Scale to today with WID's growth in current-price national income.
        ni_2024 = ni * national_income_current(ni_rows, country, 2024) / national_income_current(ni_rows, country, year)

        groups = []
        for r in rows:
            income = float(r["income_bn"]) if r["income_bn"] else float(r["income_share"]) * ni
            g = r["group"]
            if (country, g) not in rate_of and g in ("P0-10", "P10-20"):
                # The studies start at P10 (Italy at P20); give the bottom the lowest reported group's rate.
                rate = next(rate_of[(country, x)] for x in ("P10-20", "P20-30") if (country, x) in rate_of)
            elif g == "P90-99" and country == "Netherlands":
                w = [(rate_of[(country, x)], s) for x, s in (("P90-95", 0.1028), ("P95-99", 0.1203))]
                rate = sum(a * b for a, b in w) / sum(b for _, b in w)  # NL weights from WID p90p95 / p95p99
            else:
                rate = rate_of[(country, g)]
            groups.append({"group": g, "income": income, "rate": rate})

        def is_top(g):
            return g.startswith("P99-") or g.startswith("P99.")

        def is_p90_99(g):
            return g in ("P90-99", "P90-95", "P95-99")

        hi = [g for g in groups if is_p90_99(g["group"])]
        top = [g for g in groups if is_top(g["group"])]
        floor_p90_99 = sum(g["income"] * g["rate"] for g in hi) / sum(g["income"] for g in hi)
        if country == "France":
            floor_peak = fr_peak
        else:
            floor_peak = max(v for (c, g), v in rate_of.items() if c == country and (is_top(g) or g.startswith(("P90", "P95"))))

        has_full = any(g["group"] == "P0-10" for g in groups)
        if has_full:
            macro = sum(g["income"] * g["rate"] for g in groups) / sum(g["income"] for g in groups)
        else:
            macro = PUBLISHED_MACRO_RATE.get(country)
        revenue = macro * ni if macro else None

        gaps = {"p90_99": 0.0, "peak": 0.0}
        for g in top:
            row = {
                "country": country, "year": year, "group": g["group"],
                "income_bn": round(g["income"], 2),
                "share_of_national_income": round(g["income"] / ni, 4),
                "rate": round(g["rate"], 3),
                "tax_paid_bn": round(g["income"] * g["rate"], 2),
            }
            for key, floor in (("p90_99", floor_p90_99), ("peak", floor_peak)):
                s = g["income"] * max(0.0, floor - g["rate"])
                gaps[key] += s
                row[f"shortfall_vs_{key}_bn"] = round(s, 2)
            band_rows.append(row)

        top_income = sum(g["income"] for g in top)
        top_tax = sum(g["income"] * g["rate"] for g in top)
        hi_income = sum(g["income"] for g in hi)
        summary_rows.append({
            "country": country,
            "year": year,
            "currency": next(r["currency"] for r in ni_rows if r["country"] == country),
            "national_income_bn": round(ni, 1),
            "total_tax_revenue_bn": round(revenue, 1) if revenue else "",
            "p90_99_income_bn": round(hi_income, 1),
            "p90_99_tax_bn": round(hi_income * floor_p90_99, 1),
            "p90_99_rate": round(floor_p90_99, 3),
            "top1_income_bn": round(top_income, 1),
            "top1_tax_bn": round(top_tax, 1),
            "top1_rate": round(top_tax / top_income, 3),
            "top1_share_of_income": round(top_income / ni, 4),
            "top1_share_of_tax_revenue": round(top_tax / revenue, 4) if revenue else "",
            "peak_rate": round(floor_peak, 3),
            "shortfall_vs_p90_99_bn": round(gaps["p90_99"], 1),
            "shortfall_vs_peak_bn": round(gaps["peak"], 1),
            "shortfall_vs_p90_99_pct_ni": round(gaps["p90_99"] / ni, 4),
            "shortfall_vs_peak_pct_ni": round(gaps["peak"] / ni, 4),
            "shortfall_vs_peak_pct_revenue": round(gaps["peak"] / revenue, 4) if revenue else "",
            "shortfall_vs_p90_99_today_bn": round(gaps["p90_99"] / ni * ni_2024, 1),
            "shortfall_vs_peak_today_bn": round(gaps["peak"] / ni * ni_2024, 1),
        })

    for name, rows in (("missing_revenue_by_group.csv", band_rows), ("missing_revenue_summary.csv", summary_rows)):
        with open(DATA / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
