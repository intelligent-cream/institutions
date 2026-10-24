"""Annual average exchange rates from the ECB, expressed as USD per unit of currency.

Writes data/influence/clean/fx.csv (year, currency, usd_per_unit).
"""

import csv
import io

from influence.common import CLEAN, RAW, cached, get, write_csv

URL = ("https://data-api.ecb.europa.eu/service/data/EXR/"
       "A.USD+GBP+CHF+DKK+SEK+NOK.EUR.SP00.A?format=csvdata&startPeriod=2014")


def main():
    raw = cached(RAW / "fx" / "ecb_annual.csv", lambda: get(URL)).decode()
    per_eur = {}  # (year, currency) -> units of currency per EUR
    for r in csv.DictReader(io.StringIO(raw)):
        per_eur[(int(r["TIME_PERIOD"]), r["CURRENCY"])] = float(r["OBS_VALUE"])
    rows = []
    for year in sorted({y for y, _ in per_eur}):
        usd = per_eur.get((year, "USD"))
        if not usd:
            continue
        rows.append({"year": year, "currency": "USD", "usd_per_unit": 1.0})
        rows.append({"year": year, "currency": "EUR", "usd_per_unit": round(usd, 6)})
        for cur in ("GBP", "CHF", "DKK", "SEK", "NOK"):
            if (year, cur) in per_eur:
                rows.append({"year": year, "currency": cur, "usd_per_unit": round(usd / per_eur[(year, cur)], 6)})
    write_csv(CLEAN / "fx.csv", rows)
    print(f"wrote fx.csv ({len(rows)} rows, {rows[0]['year']}-{rows[-1]['year']})")


if __name__ == "__main__":
    main()
