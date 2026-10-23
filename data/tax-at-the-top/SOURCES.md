# Tax at the top: sources and method

All rates are **total effective tax rates**: every tax a group pays (personal income and wealth
taxes, payroll/social contributions, consumption taxes, and the corporate tax of companies they
own) divided by the group's **pre-tax economic income**, which includes profits kept inside
companies they own. Groups are ranked by income, not wealth.

| File | What it holds | Source |
|---|---|---|
| `effective_tax_rate_by_income_group.csv` | Total effective tax rate per income group, four countries | Bruil, van Essen, Leenders, Lejour, Möhlmann & Rabaté (2025), *Inequality and Redistribution in the Netherlands*, CBS/CPB working paper, **Figure 7**. NL 2016; IT 2015 (Guzzardi et al. 2024); FR 2018 (Bozio et al. 2024) with the billionaire point from Bach et al. (2023); US 2018 (Saez & Zucman 2019). |
| `france_tax_rate_by_income_definition.csv` | France, top 10%: rate on taxable income vs. on economic income vs. the rate if all economic income went through the personal tax schedule | Bach, Bozio, Guillouzouic & Malgouyres (2023), *Quels impôts les milliardaires paient-ils ?*, Note IPP n°92, **Figure 1a/1b**, 2016 data |
| `france_income_thresholds.csv` | France: economic income needed to enter each top group, number of households | Same IPP note, **Table 1** |
| `us_top400_tax_breakdown.csv` | US: tax rates by tax type for all households vs. the ~400 richest, 2018–2020 average | Balkir, Saez, Yagan & Zucman (2025), *How Much Tax Do US Billionaires Pay?*, NBER WP 34170, **Tables 1–2** |

## Accuracy notes

- Values in the first two files were **read off published charts** and are accurate to about ±1
  percentage point. Values in the last two files are copied from published tables.
- Italy's top point is **illustrative**: one billionaire (Silvio Berlusconi), estimated by Guzzardi.
  Italy's P99.9–99.99 point stands for the whole top 0.1%.
- France's P99.9999–100 point is the IPP estimate for the 0.0002% richest households (~75 households).
- France's rates include contributory social contributions, which raises its middle-class rates
  relative to the other countries.
- Ranking is by income. Very rich people with low realised income can sit lower in the ranking,
  which, if anything, understates how low rates fall for the wealthiest.
- Critics (e.g. Auten & Splinter) argue that assigning retained profits and corporate tax to owners
  is a choice; Bruil et al. (Figure A.20) test other assignment methods and the drop at the top remains.

## Missed revenue estimate (`analysis/missing_revenue.py`)

For each income group in the top 1%: `income × max(0, floor rate − rate paid)`, summed. Two floors:

- **P90–99 floor:** the income-weighted rate that high earners (P90–99, mostly salaried) pay.
- **Peak floor:** the highest rate any group in the top 10% pays, where the system stops being progressive.

| Input file | Source |
|---|---|
| `income_by_group.csv` | Netherlands: Bruil et al. (2025) Table 2 (adults × average pre-tax national income). France: IPP Note 92 Table 1 (households × average economic income). Italy and US: World Inequality Database, `sptincj992` (pre-tax national income shares, equal-split adults). |
| `national_income.csv` | World Inequality Database `mnninci999` (national income, 2025 prices) and `inyixxi999` (price index), used to convert to current prices and scale to 2024. |
| `france_band_rates.csv` | IPP Note 92 Figure 1b, averaged over the chart points inside each band. |

Outputs: `missing_revenue_summary.csv` (per country) and `missing_revenue_by_group.csv` (per band).

**Why not WID for every country:** WID's Dutch series gives the top 1% 6.9% of national income in 2016;
the Dutch study, which links retained company profits to their owners, finds 14.1%. Each country therefore
takes incomes and rates from the same study wherever possible.

**Limits:** static (no behavioural response, so an upper bound for a reform); the Italian study reports the top
0.1% as one group, which understates the drop; the US WID series stops at the top 0.001%, so its two top
Saez–Zucman points are averaged; bottom groups the studies don't report get the lowest reported group's rate
(only affects the total-revenue denominator).
