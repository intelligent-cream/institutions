# Corporate influence profiles

What 110 of the largest US and European companies spend on lobbying and political giving in
Washington, Brussels, Berlin and Paris, joined per company with their effective tax rates,
tax-haven subsidiaries and US federal contracts.

Dashboard: https://claude.ai/artifact/2fyEdfBjsh4zsp7WPx9hGa

## Run it

```sh
export SEC_USER_AGENT="Your Name you@example.org"   # SEC asks for a contact
python3 -m influence.sources.identifiers   # LEIs and SEC filer numbers
python3 -m influence.sources.fx            # ECB exchange rates
python3 -m influence.sources.us_lobby      # LDA filings (≈1.7 GB download, cached)
python3 -m influence.sources.eu_lobby      # EU Transparency Register + Commission meetings
python3 -m influence.sources.national_lobby  # German and French registers
python3 -m influence.sources.us_pac        # FEC bulk data
python3 -m influence.sources.tax           # SEC XBRL + ESEF filings
python3 -m influence.sources.us_contracts  # USAspending
python3 -m influence.sources.subsidiaries  # GLEIF ownership data
python3 -m influence.build                 # → data/influence/company_year.csv, company_profile.csv
python3 -m influence.analysis              # → data/influence/findings/
```

Standard library only. Raw downloads are cached in `data/influence/raw/` (git-ignored);
cleaned per-source tables, with a `*_matches.csv` audit file each, are in `data/influence/clean/`.

## Layout

| Path | What |
|---|---|
| `data/influence/companies_seed.csv` | The 110 companies, with manual identifier overrides |
| `influence/sources/*.py` | One module per public source |
| `influence/build.py` | Joins sources into a company-year panel and 2019–2024 profiles |
| `influence/analysis.py` | Rank correlations with permutation tests, US–EU trend, coverage |
| `influence/dashboard/` | Source of the published dashboard page |
| `data/influence/LITERATURE.md` | What earlier work found and what is new here |

## Known gaps

- Chevron's parent has no LEI, so it has no GLEIF subsidiaries.
- German companies are absent from the open ESEF repository and Swiss companies file neither ESEF nor with the SEC, so 11 companies have no tax rate.
- German lobby-register history starts with fiscal 2023; French with 2019.
- GLEIF lists only subsidiaries that have an LEI and report their parent; haven counts are lower bounds.
- Corporate fines are not included: Violation Tracker blocks automated access.
