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
