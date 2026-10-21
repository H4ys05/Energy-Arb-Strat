# Energy Arbitrage Strategy

Battery arbitrage on the German/Luxembourg day-ahead power market (EPEX Spot, DE-LU).
The battery charges when power is cheap and discharges when it is expensive.

## Files

| File | What it does |
|---|---|
| `data.py` | ETL: downloads hourly prices from the free Energy-Charts API, cleans them, writes `data/prices.csv` (and PostgreSQL if `DATABASE_URL` is set) |
| `strategy.py` | The algorithm: forecast tomorrow's prices, then pick the best charge/discharge schedule with dynamic programming |
| `backtest.py` | Walk-forward backtest: settles each day's schedule at real prices and reports profit, Sharpe, drawdown |

## Run

```bash
pip install -r requirements.txt
python data.py 2023-01-01 2026-10-19   # download prices (~33k hourly rows)
python backtest.py    # run the backtest (2024 -> latest data)
python backtest.py 2026-01-01              # test from a start date
python backtest.py 2024-01-01 2025-12-31   # test a specific window
python backtest.py 2026-01-01 --fee 1 --wear 10  # choose cost assumptions
python backtest.py 2026-01-01 --fee 0      # reproduce the earlier zero-fee case
python backtest.py 2026-01-01 --output results/2026  # keep this run separately
python backtest.py --capital 600000 --risk-free 0.025  # Sharpe capital and risk-free rate
```

Optional PostgreSQL storage:
```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/energy
python data.py && python backtest.py
```

## How it works

- **Battery:** 2 MWh / 1 MW, 90% round-trip efficiency, EUR 5 per internal MWh discharged for wear.
- **Transaction fees:** EUR 1 per grid MWh bought **and** EUR 1 per grid MWh sold by default.
  Fees enter both schedule optimization and actual settlement, including at negative prices.
  Optimized, baseline, and perfect-price strategies all pay the same fees and wear costs.
- **Forecast cutoff:** day-ahead bids are submitted before prices clear, so the strategy only
  sees prices up to the previous day. It forecasts each hour as a blend of yesterday's price
  and the 7-day average for that hour, optimizes on the forecast, then gets paid at actual prices.
- **Baseline:** a fixed schedule (same clock hours every day), with the hours picked from the average price shape of the 12 months before the test.
- **Perfect-price benchmark:** the same optimizer given each day's actual prices. It does not optimize overnight opportunities across the entire test window.
- **Warm-up:** data before the test start is only used for forecasts and picking baseline hours.

The default EUR 1/MWh is a combined variable trading-cost **research estimate**, not a verified
all-in market tariff. For comparison, [ECC's September 28, 2026 price list, page 13](https://www.ecc.de/fileadmin/ECC/Downloads/About_ECC_AG/Rules/Price_List/Current_Price_List/20260928_ECC_Price_List_085a.pdf)
lists a standard EUR 0.015/MWh power day-ahead clearing fee, subject to its exceptions.
That is one component of trading costs; it does not establish total exchange or intermediary costs.
The [current EPEX SPOT fee schedule](https://www.epexspot.com/system/files/dowload_center/EPEX%20SPOT-Fee%20schedule.pdf)
requires access to the EPEX member area. Its current trading charge could not be verified from
an accessible official schedule during the October 20, 2026 review, so a complete actual fee
has not been established. Annual clearing and access charges also depend on the membership route.
The default estimate includes an allowance for clearing; do not add that component a second time.
Replace `--fee` with the applicable variable fee for your trading arrangement. The current clearing
reference is not a reconstruction of historical tariffs. Fixed market-access and operating costs,
capital costs, network charges, taxes, and imbalance costs are excluded from reported P&L.

Wear is a separate battery-aging allowance. Its default is unchanged; use `--wear 10` to test EUR 10
per internal MWh discharged. Neither wear setting is calibrated to a specific battery's replacement
cost or warranted lifetime throughput. `--fee` and `--wear` must be finite and nonnegative.

Each run writes `daily_pnl.csv`, `metrics.csv`, and `assumptions.json` under `results/` or `--output`.
The assumptions file records the fee, wear, units, battery settings, and actual test period.
The results below use the defaults: **EUR 1/MWh transaction fees and EUR 5/MWh wear**.

## Results

Validated on October 20, 2026; see the [validation report](results/validation-2026-10-20/report.md).
Profits are simulated net operating P&L after fees, efficiency losses, and wear. They exclude capital,
fixed operating, financing, and tax costs.

**None of these results are genuinely out-of-sample.** The strategy was developed with 2024–2026
prices visible, and 2026 has been analyzed repeatedly. The 2023 row is the closest available check:
2023 was previously used only as warm-up and is tested here with downloaded 2022 warm-up prices.
It is still a backward-looking period that the developer could see. A true out-of-sample test
requires locking the model and assumptions and evaluating delivery dates after October 19, 2026.

### Improvement vs benchmarks

The **frozen schedule** is the main benchmark. It optimizes the pre-test year's average 24-hour price
shape once, with the same battery, fees, wear, and cycling permissions, and then repeats that schedule.
The **fixed-hours** baseline charges and discharges in one fixed block per day regardless of costs.
It loses money on 10–20% of days, which inflates uplift measured against it.

| Window | Status | Optimized | Frozen schedule | **Uplift vs frozen** | Fixed hours | Uplift vs fixed hours |
|---|---|---|---|---|---|---|
| 2023 | pre-sample (pseudo out-of-sample) | EUR 53,652 | EUR 48,366 | **+10.9%** | EUR 32,471 | +65.2% |
| 2024–2025 | in-sample (development) | EUR 138,154 | EUR 114,592 | **+20.6%** | EUR 90,844 | +52.1% |
| 2026 Jan 1–Oct 19 | in-sample (repeatedly inspected) | EUR 77,999 | EUR 67,139 | **+16.2%** | EUR 65,848 | +18.5% |
| Trailing 12 months to Oct 19, 2026 | in-sample (overlaps above) | EUR 85,005 | EUR 70,123 | **+21.2%** | EUR 65,142 | +30.5% |

- **The optimized strategy earned 11–21% more simulated profit than the frozen schedule** in every window.
  It beat the frozen schedule in 23 of 24 months in 2024–2025 and 8 of 9 complete months in 2026.
- The 15% target is met against the frozen schedule in 2024–2025, 2026, and the trailing 12 months, but not in 2023.
  Under a daily cycle cap, the 2026 uplift falls below the target; see the caveat below.
- **Caveat on the 21% figure (power limit and cycling).** The table above assumes a 1 MW limit on internal
  battery energy and unlimited cycling at a flat EUR 5/MWh wear cost. The
  [sensitivity run](results/validation-2026-10-20/sensitivity.csv) gives every strategy the same limits.

  | Scenario | Trailing 12m uplift | 2026 Jan–Oct uplift | Trailing 12m optimized profit |
  |---|---|---|---|
  | As reported | +21.2% | +16.2% | EUR 85,005 |
  | Grid import ≤ 1 MW (0.9 MW internal) | +21.7% | +16.6% | EUR 82,915 (−2.5%) |
  | Cap of 1.5 cycles/day | +20.9% | +12.3% | EUR 83,221 (−2.1%) |
  | Cap of 1.1 cycles/day (6,000 cycles in about 15 years) | +21.7% | +11.5% | EUR 80,165 (−5.7%) |
  | Grid limit and cap of 1.1 cycles/day | +21.7% | +12.1% | EUR 78,428 (−7.7%) |

  - The trailing-12-month uplift of about 21% holds under these limits. The frozen schedule loses
    about as much profit as the optimized strategy.
  - Absolute profit falls by up to about 8%.
  - The 2026 uplift falls to 11–12% under a cycle cap, below the 15% target. Treat **11–21%** as the range,
    not 21%.
  - Higher flat wear costs (EUR 10 or 20 per MWh) *raise* the measured uplift, to 27–45% in the trailing
    12 months. That is because the frozen schedule keeps cycling twice a day. Read this as weakness in
    the comparison, not as extra edge.
  - The caps are hard daily limits. Nonlinear degradation, capacity fade, and calendar aging are still unmodeled.
- The strategy captures 86–92% of the daily perfect-price benchmark. That benchmark optimizes each day
  separately, so it is not a global upper bound.
- With `--fee 1 --wear 10`, 2026 optimized profit is EUR 73,771 versus EUR 62,928 for the fixed-hours
  baseline (+17.2%). These are cost sensitivities on already observed data.

### Annual revenue

| Basis | Optimized | Frozen schedule | Fixed hours | Perfect (daily) |
|---|---|---|---|---|
| Trailing 12 months (Oct 20, 2025–Oct 19, 2026) | EUR 85,005 | EUR 70,123 | EUR 65,142 | EUR 93,664 |
| 2024–2025 average | EUR 68,982 | EUR 57,217 | EUR 45,360 | EUR 77,892 |
| 2023 | EUR 53,652 | EUR 48,366 | EUR 32,471 | EUR 62,723 |

The 2026 window is partial and is not annualized here. Scaling January–October up to a full year
gives EUR 97,499, which overstates revenue by about 15% relative to the trailing 12 months because
it omits November and December. `backtest.py` still prints `profit_per_year_eur` and
`return_on_capital_annual` that way for partial windows.

### Risk-adjusted metrics (operating P&L, not investment returns)

These ratios use simulated daily **operating P&L** for an already available battery. They are **not
conventional investment Sharpe ratios**: they exclude depreciation, fixed operating costs,
financing, capacity fade, and asset-value risk. They measure the stability of operating income.

| Window | Metric | Optimized | Frozen schedule | Fixed hours | Perfect (daily) |
|---|---|---|---|---|---|
| 2024–2025 | Operating Sharpe, monthly (24 months) | 7.4 | 5.0 | 3.2 | 8.5 |
| | Operating Sharpe, Lo 30–90 lags | 6.0–5.2 | 4.0–3.7 | 2.5–2.1 | 6.9–5.8 |
| | Max drawdown / win rate (days) | EUR -99 / 97% | EUR -268 / 91% | EUR -1,900 / 83% | 0 / 100% |
| Trailing 12 months | Operating Sharpe, monthly (11 complete months) | 5.6 | 4.0 | 3.4 | 6.3 |
| | Operating Sharpe, Lo 30–90 lags | 4.7–3.2 | 3.3–2.4 | 2.7–1.8 | 5.4–3.8 |
| | Max drawdown / win rate (days) | EUR -34 / 97% | EUR -419 / 87% | EUR -2,458 / 80% | 0 / 100% |
| 2026 Jan 1–Oct 19 | Operating Sharpe, monthly (9 complete months) | 7.1 | 5.5 | 5.1 | 8.0 |
| | Operating Sharpe, Lo 30–90 lags | 6.6–5.3 | 5.0–4.3 | 4.4–3.6 | 7.5–6.1 |

Incremental-P&L IR, optimized minus benchmark:

| Window | vs frozen: monthly / Lo 30 | vs fixed hours: monthly / Lo 30 | Newey-West t, 30 lags (vs frozen / vs fixed) |
|---|---|---|---|
| 2023 | 2.3 / 1.9 | 4.9 / 3.8 | 2.4 / 5.2 |
| 2024–2025 | 3.8 / 3.1 | 4.3 / 3.3 | 5.3 / 6.0 |
| 2026 Jan 1–Oct 19 | 4.7 / 3.7 | 4.0 / 3.7 | 4.3 / 4.3 |
| Trailing 12 months | 5.2 / 4.2 | 4.2 / 3.7 | 5.3 / 4.7 |

**Definitions:**
- **Operating Sharpe** uses daily returns `R_t = P&L_t / capital` minus a daily risk-free rate
  `(1 + r_f)^(1/365) - 1`, with sample standard deviation (ddof=1). Defaults are EUR 500,000 capital
  (2 MWh at about EUR 250k/MWh installed, a research assumption) and a 2% annual risk-free rate.
  Capital barely changes the operating Sharpe: EUR 350k to 750k moves the 2024–2025 Lo-30 value from 6.3 to 5.5.
- **IR** uses incremental daily P&L `P_strategy,t - P_benchmark,t` on identical dates. It measures how
  consistently the strategy adds operating profit; it is not a conventional portfolio IR.

**Serial correlation:**
- Daily P&L has lag-1 autocorrelation of 0.4–0.6 and remains correlated to about 30–60 days.
- Simple √365 scaling therefore overstates these ratios about threefold: the daily √365 Sharpe is
  20–25 in every window.
- Weekly aggregation still roughly doubles the monthly values.
- The headline figures above use complete calendar months (√12), or Lo's (2002) correction with
  30–90 lags. These broadly agree; monthly values run slightly higher.
- Monthly estimates rest on only 9–24 observations. The standard error is roughly ±1.3 at 24 months
  and ±2.5 at 9 months.
- `backtest.py` still prints the √365 value and a Newey-West rule-of-thumb (5–6 lag) adjustment;
  neither should be used as a headline.

**Investment-style context:**
- Deducting 15-year straight-line depreciation and EUR 10k/yr fixed O&M from daily P&L at EUR 500k
  capital gives a Lo-30 Sharpe of about 0.1 (2023), 1.6 (2024–2025), and 2.0 (trailing 12 months).
  This still omits regime risk in future price spreads.
- Annual net operating yield at EUR 350k / 500k / 750k on trailing-12-month revenue is
  24.3% / 17.0% / 11.3%, before depreciation and O&M.
- Illustrative real, pre-tax, unlevered project IRRs at EUR 500k range from about −9% to +10%,
  depending on which year's revenue persists, life, and spread erosion. They are scenario rankings,
  not an investment case.

### Timing and accounting checks

- The October 20 audits checked 1,753 day-decisions, plus perturbation tests that replaced
  delivery-day and later prices with noise. Each forecast used only prices for delivery hours before
  the target day; those were published before that day's auction gate closure.
- The cached hourly prices matched fresh raw API data with no gaps.
- Hourly P&L, state-of-charge bounds, energy balance, and zero initial and terminal storage were
  independently verified for every strategy.
- These checks do not verify historical publication vintages.
- The [look-ahead audit](results/lookahead-audit-2026-10-20/report.md) has the detailed timing and benchmark analysis.

### Outstanding weaknesses

The trading algorithm and its parameters were not changed after these audits. Known weaknesses:

- **Grid-side power limit:** the 1 MW limit applies to internal battery energy. Grid import reaches
  1.054 MW (export 0.949 MW). If 1 MW is the inverter or grid-connection rating, all strategies are
  modestly overstated: profit falls by about 2–4%, and the uplift vs frozen is unchanged (see the caveat above).
  The production model still uses the internal limit.
- **Cycle life:**
  - Wear is a flat EUR 5/MWh discharged, with no cycle cap, capacity fade, or calendar aging.
  - The optimized strategy cycles about 1.5–1.7 times per day versus 2.0 for the frozen schedule and
    1.0 for fixed hours. That is about 560 cycles a year, which would exhaust a 6,000-cycle warranty
    in about 10 years rather than 15.
  - Warranty cycle limits or nonlinear degradation would reduce profit. A cap of 1.1 cycles/day cuts
    optimized profit by about 6%. It leaves the trailing-12-month uplift near 21% but cuts the 2026 uplift
    to about 12%. The production model has no cycle cap.
- **Forecasting interface:** `forecast_prices` has no upper bound on the history it reads.
  - Called with an untruncated price series, it uses delivery-day prices.
  - The backtest is correct only because it passes `series[series.index < delivery_start]`.
  - The ETL's linear interpolation could similarly leak future prices if gaps appeared. The current data has none.
- **Other:**
  - Hourly blocks only, since the market moved to 15-minute products in October 2025.
  - Perfect execution and price-taking.
  - No imbalance, availability, or market-access costs.
  - A per-day perfect-price benchmark.
  - `max_drawdown_eur` does not start from zero equity.

Data: Energy-Charts / Bundesnetzagentur SMARD.de, CC BY 4.0.

## Canadian markets: Alberta and Ontario

The same algorithm, cost model and benchmarks, run on Canadian prices in CAD.

| Folder | Market | Data | Test period |
|---|---|---|---|
| `alberta/` | AESO pool price (real-time, floor CAD 0, cap CAD 999.99) | AESO ETS Historical Pool Price report | 2023-01-01 -> latest |
| `ontario/` | IESO HOEP (real-time) | IESO public yearly HOEP CSVs | 2023-01-01 -> 2025-04-30 |

```bash
python alberta/fetch_prices.py && python alberta/run_backtest.py
python ontario/fetch_prices.py && python ontario/run_backtest.py
python alberta/run_backtest.py 2025-01-01 --fee 1.5 --wear 15
```

Shared code is in `canada_backtest.py`. Default costs are the DE-LU research assumptions converted at
roughly 1.5 CAD/EUR: CAD 1.5/grid MWh fee, CAD 7.5/MWh wear, CAD 750k capital. None are verified
AESO or IESO tariffs. Transmission charges, Global Adjustment, and fixed costs are excluded.

Notes:
- Neither market ran a day-ahead auction in these periods. The schedule is still fixed from
  the previous day's prices and settled at the real-time price, which is conservative compared with
  a battery that re-optimizes during the day.
- HOEP ended on 2025-04-30, when Ontario's Market Renewal introduced a day-ahead market and the
  Ontario Zonal Price. IESO's public folder only keeps about 90 days of the new prices, so Ontario
  results do not cover the new market.
- Alberta prices are never negative (floor CAD 0). Profit comes from scarcity spikes and fell sharply
  after 2023 as prices came down.

Results from the October 21, 2026 run with default costs (see `*/results/`):

| Market | Optimized CAD/yr | Baseline CAD/yr | Perfect CAD/yr | Optimized share of perfect |
|---|---:|---:|---:|---:|
| Alberta 2023-01-01 -> 2026-10-20 | 61,483 | 49,126 | 157,861 | 38.9% |
| Ontario 2023-01-01 -> 2025-04-30 | 1,638 | 4,627 | 29,181 | 5.6% |

Alberta optimized profit by year: 2023 CAD 118k, 2024 45k, 2025 44k, 2026 (to Oct 20) 27k.
In Ontario, HOEP's daily spreads are too small for the forecast-based strategy to beat the fixed
schedule after costs.

Data: AESO and IESO public market reports.

## Verify

```bash
python -m unittest discover -s tests -v
```

Checks cover the Sharpe/IR definitions and autocorrelation adjustment, fees on both grid flows, efficiency losses, negative prices, clipped actions,
fee-sensitive dispatch, all three strategies, invalid costs, and independent exhaustive
verification of the optimizer on small scheduling problems.
