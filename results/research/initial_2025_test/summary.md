# Battery arbitrage: 2025-01-01 through 2025-12-31

Selected model: `adaptive_w0.5_d0.5_g0`. Prices and profits are USD.

| Metric | Strategy | Day-ahead forecast baseline |
| --- | ---: | ---: |
| Net operating profit | 17,479.01 | 17,769.87 |
| Daily P&L Sharpe | 6.94 | 5.65 |
| Maximum daily drawdown | 150.27 | 261.02 |
| Active days | 353.00 | 353.00 |

Profit improvement: -1.64%.

![Cumulative operating profit](profit.png)

Detailed CSVs: [daily P&L](ledgers/daily.csv) and [hourly dispatch](ledgers/hourly.csv).

Both models use identical batteries, costs and cycle limits. Plans use published day-ahead prices as a forecast; settlement uses realized hourly RT prices. The strategy updates from five-minute observations at least ten minutes old. Final historical revisions are not publication-vintage data. Zonal prices proxy actual nodal storage settlement.

Profits exclude capital costs, fixed operating expenses, financing and taxes. Sharpe describes simulated operating P&L, rather than investment returns. Square-root annualization does not correct for serial dependence. Idle days are included. Each day physically starts and finishes empty.
