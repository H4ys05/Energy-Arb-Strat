# Battery arbitrage: 2026-01-01 through 2026-10-19

Selected model: `adaptive_w0.5_d0.9_g0`. Prices and profits are USD.

| Metric | Strategy | Day-ahead forecast baseline |
| --- | ---: | ---: |
| Net operating profit | 14,564.07 | 14,397.39 |
| Daily P&L Sharpe | 6.47 | 5.89 |
| Maximum daily drawdown | 373.96 | 249.12 |
| Active days | 288.00 | 281.00 |

Profit improvement: 1.16%.

![Cumulative operating profit](profit.png)

Detailed CSVs: [daily P&L](ledgers/daily.csv) and [hourly dispatch](ledgers/hourly.csv).

Both models use identical batteries, costs and cycle limits. Plans use published day-ahead prices as a forecast; settlement uses realized hourly RT prices. The strategy updates from five-minute observations at least ten minutes old. Final historical revisions are not publication-vintage data. Zonal prices proxy actual nodal storage settlement.

Profits exclude capital costs, fixed operating expenses, financing and taxes. Sharpe describes simulated operating P&L, rather than investment returns. Square-root annualization does not correct for serial dependence. Idle days are included. Each day physically starts and finishes empty.
