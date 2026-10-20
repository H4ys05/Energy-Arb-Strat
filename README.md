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
- **No look-ahead:** day-ahead bids are submitted before prices clear, so the strategy only
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

## Results (2024-2025, 731 days)

| | Optimized | Baseline | Perfect foresight |
|---|---|---|---|
| Profit / year | EUR 68,982 | EUR 45,360 | EUR 77,892 |
| Sharpe (annualized, daily P&L) | 23.7 | 18.0 | 26.1 |
| Max drawdown | EUR -99 | EUR -1,900 | 0 |
| Win rate (days) | 97% | 83% | 100% |

- **+52.1% profit vs the fixed-schedule baseline** (target: 15%)
- **Sharpe 23.7** (target: 1.2+)
- Captures 88.6% of the daily perfect-price benchmark profit

## Results on 2026 data (Jan 1 - Oct 19, 292 days)

| | Optimized | Baseline | Perfect foresight |
|---|---|---|---|
| Profit (292 days) | EUR 77,999 | EUR 65,848 | EUR 84,742 |
| Sharpe (annualized, daily P&L) | 28.1 | 23.6 | 30.6 |
| Max drawdown | EUR -34 | EUR -857 | 0 |
| Win rate (days) | 98% | 90% | 100% |

- **+18.5% profit vs the fixed-schedule baseline** (target: 15%)
- **Sharpe 28.1** (target: 1.2+)
- Captures 92.0% of the daily perfect-price benchmark profit

With `--fee 1 --wear 10`, 2026 optimized profit is EUR 73,771 versus EUR 62,928 for the baseline
(+17.2%). These are cost sensitivities on already observed data.

Note: Sharpe ratios for battery arbitrage are naturally very high, because the battery
almost never loses money on a given day. The real risks (capital cost, battery
degradation, prices changing over time) are not part of daily P&L volatility.

Data: Energy-Charts / Bundesnetzagentur SMARD.de, CC BY 4.0.

## Verify

```bash
python -m unittest discover -s tests -v
```

Checks cover fees on both grid flows, efficiency losses, negative prices, clipped actions,
fee-sensitive dispatch, all three strategies, invalid costs, and independent exhaustive
verification of the optimizer on small scheduling problems.
