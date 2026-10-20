# Battery Energy Arbitrage

Three Python modules: a data pipeline, a strategy, and a backtester. Give the backtester dates and it downloads the data it needs. You can also fetch current prices on demand.

This version uses **NYISO NORTH, in USD**, with published day-ahead prices, realized hourly prices, and five-minute observations. NORTH was selected using 2024–2025 comparisons across four NYISO zones. The earlier Ontario archive-only version is preserved locally in `.archive/legacy_version.zip`.

## Run it

Requires Python 3.11+:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Downloads historical data and evaluates the selected strategy against the baseline.
python -m energy_arb.backtest --start 2024-01-01 --end 2025-12-31

# Any recent completed date range works through the same pipeline.
python -m energy_arb.backtest --start 2026-10-01 --end 2026-10-19

# Fetch currently published observations and tomorrow's day-ahead prices.
python -m energy_arb.pipeline latest
```

Dates are inclusive delivery dates in New York time. DST days contain 23 or 25 hours and are handled explicitly. Backtests require completed days; today's realized prices are still incomplete. `latest` fetches the current feed without running a backtest. It excludes future-stamped RT entries and records retrieval time in `data/live/latest_NORTH.csv`. Five-minute RT timestamps identify interval **ends**; hourly DA/RT timestamps identify interval **starts**.

The first historical run downloads monthly ZIPs. Later runs use the cache; the current month always refreshes. `--refresh` downloads all requested source files again to pick up revisions. There is no API key, subscription, or background process.

If an hourly legacy archive is incomplete, the pipeline retrieves the official replacement files from NYISO's newer OASIS portal. This recovered 19 missing July 2026 days during verification. The original and replacement sources remain identifiable in the manifest and SQL records.

## Project layout

| File | Responsibility |
| --- | --- |
| `energy_arb/pipeline.py` | Download, validate and cache prices; normalize timestamps; upsert into SQLite or PostgreSQL. |
| `energy_arb/strategy.py` | Forecast prices, optimize battery dispatch, and enforce physical accounting. |
| `energy_arb/backtest.py` | Replay decisions, settle trades, compare strategies and generate results. |

```text
energy_arb/                 # The three core modules above
tests/                     # Accounting, data and timing checks
data/
   raw/<year>/              # Original NYISO downloads, grouped by year
   raw/sources.json         # Download URLs, timestamps and checksums
   database/prices.db       # Local SQLite database
   live/latest_NORTH.csv    # Latest on-demand price snapshot, one file per zone
results/
   backtests/<zone>/<start>_<end>/
     summary.md             # Start here for a run's results
     metrics.json           # Exact statistics and assumptions
     profit.png             # Cumulative operating profit
     ledgers/               # Detailed daily.csv and hourly.csv
   research/                # Candidate comparisons and earlier evaluation runs
```

`tests/test_project.py` verifies the important behavior. `requirements.txt` lists dependencies. `compose.yaml` is an optional local PostgreSQL server. Downloaded data and detailed ledgers are excluded from Git. Existing downloads are reused from the year folders.

Backtests automatically save under `results/backtests/<zone>/<start>_<end>/`. Candidate comparisons save under `results/research/runs/<zone>/<start>_<end>/<family>/`, keeping research separate and preventing different zones or comparison families from sharing a folder. `--output` overrides the destination when needed.

The source files from immediately before this cleanup are preserved locally in `.archive/pre_organization.zip`. Saved metrics retain their original code checksums and generation metadata.

## How the strategy works

This is physical battery arbitrage across time: charge when electricity is expected to be cheap, then sell stored energy when it is expected to be more expensive.

The **baseline** optimizes one daily schedule using published day-ahead prices as a forecast of real-time prices. It settles at realized real-time prices. This is a competitive benchmark: it already selects charging and selling hours, accounts for costs, and can remain idle.

The **selected strategy** updates that schedule every hour:

1. Observe the latest five-minute price with an interval end at least ten minutes before the decision. If none is available within the freshness limit, use the day-ahead forecast for that decision.
2. Compare the observed price with the day-ahead price for the current hour.
3. Add half that difference to the current-hour forecast. Retain 90% of the adjustment for each later hour.
4. Optimize the remaining day using the battery's actual stored energy and remaining daily cycle allowance.
5. Execute only the first hour. Settle at the realized hourly RT price, then repeat.

The forecast adjustment is:

```text
forecast[t + k] = day_ahead[t + k]
                + 0.5 * (observed_spot - day_ahead[t]) * 0.9**k
```

The weights were selected using 2024 development data and 2025 validation data. The optimizer uses SciPy's bundled mixed-integer solver; the binary flags prevent simultaneous charging and selling, including at negative prices. No commercial solver is needed.

Both strategies use exactly the same assumptions:

- 1 MW power and 2 MWh internal energy capacity.
- 95% efficiency in each direction: 90.25% round-trip efficiency.
- USD 1/MWh modeled fees on both grid purchases and sales.
- USD 10/MWh modeled battery wear on energy delivered to the grid.
- At most one equivalent full discharge cycle per day, shared across all hourly replans.
- Start and physically finish each day empty, with no free starting inventory or terminal credit.

These are research assumptions, not researched installation costs or actual venue fee quotes. Set another wear assumption with `--wear`, for example `--wear 20`. Positive-price days need a spread that covers losses and costs; negative prices are valid inputs.

## Research and evaluation

The dispatch formulation follows the storage constraints and marginal discharge costs in [Zheng, Jaworski and Xu, *Arbitraging Variable Efficiency Energy Storage using Analytical Stochastic Dynamic Programming*](https://arxiv.org/abs/2108.06000). Their paper includes a benchmark that uses day-ahead prices to forecast real-time prices. Our implementation uses constant efficiency and deterministic hourly optimization, rather than reproducing their stochastic dynamic program.

The repeated update-and-replan idea is also informed by [Oeltz and Pfingsten, *Rolling intrinsic for battery valuation in day-ahead and intraday markets*](https://arxiv.org/abs/2510.01956). Their study concerns European trading venues and executable intraday products. We adapt the general replanning idea to a simplified NYISO battery simulation; we do not reproduce their order-book execution model or results.

We compared **35 candidate configurations** on 2024: a historical hourly-average forecast, 18 historical-bias corrections, 12 fixed-response intraday configurations, and four rolling least-squares responses. The 16 intraday candidates were also compared on 2025. The learned variants estimate a response to the recent price from the preceding 28 or 56 completed days, excluding the immediately preceding day. They use NumPy, with no machine-learning framework.

The initial WEST model chosen from 2024 improved development profit by 19.81%, but lost 1.64% to the baseline in its first 2025 test. That failed result is preserved in [results/research/initial_2025_test](results/research/initial_2025_test/summary.md). After observing it, 2025 became validation data for further comparisons. It must not be described as an untouched test of the final model.

The final model maximized combined WEST 2024–2025 profit among candidates improving on the baseline in each year. On WEST, its improvement was 13.80% in 2024, 4.57% in 2025 and 7.17% combined. [The full model comparison](results/research/model_comparison.csv) and [selection record](results/research/selection.json) show the decision.

- **2024:** development and model selection.
- **2025:** validation after the initial model failed its first test.
- **2026 through October 19:** separate test of the final configuration, selected before this run.

The experiment CSVs include rejected configurations. The selection JSON records the chosen parameters and evaluation roles. The combined 2024–2025 result includes model selection. Use the separate 2026 result when discussing the final strategy's performance outside development and validation.

The WEST 2026 check improved profit by only 1.16%, also missing the target. A subsequent market comparison evaluated the same fixed strategy on WEST, NORTH, N.Y.C. and LONGIL using only 2024–2025 to choose a market. All four markets appear in [results/research/market_comparison.csv](results/research/market_comparison.csv). NORTH produced the largest absolute profit improvement while improving in both years: **21.44% in 2024, 11.00% in 2025 and 13.89% combined**. Those model-selection results are close to the 15% target and remain below it. The NORTH 2026 results were not used to select its market or strategy parameters. The earlier WEST 2026 result is preserved in `results/research/markets/WEST_2026` and is not represented as a new untouched test.

### Measured NORTH results

| Period | Strategy net operating profit | Baseline net operating profit | Improvement | Role |
| --- | ---: | ---: | ---: | --- |
| 2024–2025 | USD 29,410.73 | USD 25,824.04 | **13.89%** | Development and validation |
| Jan 1–Oct 19, 2026 | USD 18,365.18 | USD 17,659.98 | **3.99%** | Separate test after choosing NORTH |

The corresponding daily operating P&L Sharpes are 6.22 and 7.91. These are the qualified operating metrics described below, not investment-return Sharpes. **The 15% target was not consistently achieved.** The separate 2026 result is smaller than the development/validation result, which limits claims about a persistent edge.

Open the [2024–2025 report](results/backtests/NORTH/2024-01-01_2025-12-31/summary.md) or [2026 report](results/backtests/NORTH/2026-01-01_2026-10-19/summary.md) for the ledgers and metrics. The two-year test retains all 17,544 delivery hours and uses the DA forecast during three hours with no timely intraday observation. The 2026 test retains all 7,007 delivery hours, with no intraday fallback hours.

![NORTH 2024–2025 operating profit](results/backtests/NORTH/2024-01-01_2025-12-31/profit.png)

To rerun the comparisons:

```sh
python -m energy_arb.backtest --start 2024-01-01 --end 2024-12-31 --zone WEST --compare
python -m energy_arb.backtest --start 2024-01-01 --end 2024-12-31 --zone WEST --compare --adaptive
python -m energy_arb.backtest --start 2024-01-01 --end 2024-12-31 --zone WEST --compare --learned
```

`--compare` reports candidate results; it does not overwrite the selected model. Changing parameters after inspecting a test makes further use of that period exploratory. Use new data for a fresh test.

## Data source and SQL

All market data comes directly from official NYISO public files:

- [Day-ahead zonal prices, P-2A](https://mis.nyiso.com/public/P-2Alist.htm): the pre-delivery forecast reference.
- [Time-weighted/integrated hourly RT zonal prices, P-4A](https://mis.nyiso.com/public/P-4Alist.htm): settlement prices.
- [Five-minute RT zonal prices, P-24A](https://mis.nyiso.com/public/P-24Alist.htm): delayed intraday signals.

NYISO day-ahead results are available before the delivery day; see its [Day-Ahead Scheduling Manual](https://www.nyiso.com/documents/20142/2923301/dayahd_schd_mnl.pdf/0024bc71-4dd9-fa80-a816-f9f3e26ea53a). We use cleared day-ahead prices to guide subsequent RT dispatch. They are not treated as information available before the day-ahead auction itself.

The two hourly series alone contain 35,088 observations across 2024–2025, plus the five-minute observations used for intraday signals. The selected strategy fetches one preceding day for its first delayed observation. Research comparisons fetch 60 preceding days for variants that require historical averages. Warmup rows are excluded from evaluation. Missing settlement hours stop the backtest. Missing intraday signals use the DA forecast and are counted in the metrics and ledger. Source URLs, SHA-256 hashes and download times are saved in `data/raw/sources.json`. Original downloaded files are grouped under `data/raw/<year>/`.

The default database is `data/database/prices.db` (SQLite). For PostgreSQL with Docker:

```sh
docker compose up -d --wait
export DATABASE_URL='postgresql+psycopg://energy_arb:local_dev_only@localhost:5432/energy_arb'
python -m energy_arb.pipeline fetch --start 2024-01-01 --end 2025-12-31
```

An existing PostgreSQL server works too: set its connection URL in `DATABASE_URL`. All writes use upserts keyed by UTC timestamp, market and zone. Repeated fetches update prices without duplicating observations. The pipeline downloads and parses the requested inputs before its SQL transaction starts. Credentials are never included in result artifacts.

You can fetch a custom range without running the backtest:

```sh
python -m energy_arb.pipeline fetch --start 2026-10-01 --end 2026-10-19
```

## Inspect results

Open `summary.md` in a run folder under `results/backtests/`. Each evaluation writes:

- `summary.md` and `profit.png`: comparison and cumulative operating profit.
- `ledgers/hourly.csv`: every forecast, dispatch, battery state, settlement price and cost. The selected strategy also records the intraday observation time and availability.
- `ledgers/daily.csv`: the exact daily P&L behind the statistics.
- `metrics.json`: metrics by year, target checks, fallback coverage, parameters, source hashes and package versions.

Profit improvement is `(strategy net profit - baseline net profit) / baseline net profit`. It is undefined if baseline profit is nonpositive. The project targets at least **15% improvement** and a **1.5 daily P&L Sharpe**; reports say whether those targets were met.

Daily P&L Sharpe is `sqrt(365) * mean(daily net operating profit) / std(daily net operating profit, ddof=1)`, including idle days with a zero risk-free rate. It describes operating profit variability, not investment returns. Dividing both daily profit and its standard deviation by the same battery capital amount would leave this ratio unchanged. Serial dependence, seasonality and price spikes limit square-root annualization. Drawdown is measured from end-of-day cumulative profit, starting at zero.

This is a price-taking research simulation. It omits nodal settlement differences, bid acceptance and gate closures, uplift, network charges, capital costs, fixed operating expenses, financing, taxes, outages, market impact and capacity fade. Historical final files are not publication-vintage data; the ten-minute lag models availability but cannot undo later corrections. A battery's hourly average settlement can also differ from its actual dispatch-weighted settlement. No live trades are submitted.

## Verify

```sh
python -m pytest -q tests/
ruff check energy_arb/ tests/ --select E,F,I --ignore E501
```

Tests cover energy conservation, fees, capacity/power/cycle limits, negative prices, DST, missing data, information timing, independence from future settlement prices, SQL upserts and cache behavior. PostgreSQL tests run when `TEST_DATABASE_URL` is set and use a unique disposable schema.

The previous full verification passed **all 18 tests with PostgreSQL enabled**, including recovery from an incomplete legacy archive, separate storage for multiple zones, current-price upserts and exclusion of future RT observations. After the folder cleanup, **17 local tests passed**; the PostgreSQL test was skipped because no test server was running.

The full 2024–2025 pipeline was also executed against PostgreSQL 17.11: 35,088 hourly observations and 214,649 nominal five-minute observations were loaded successfully. Actual five-minute report timestamps can contain irregular dispatch intervals, so their count is not simply 12 times the hourly count.

For an interview, explain the pipeline first, then the forecast update, physical constraints, comparison baseline, and the separate development/test results. Describe the reported Sharpe specifically as daily operating P&L Sharpe and discuss the settlement and cost assumptions.

Accurate résumé wording for the current implementation:

```latex
\resumeItem{Developed a Python battery arbitrage strategy using wholesale electricity prices and hourly forecast updates, achieving 13.9\% higher simulated net operating profit than a day-ahead-price baseline over 2024--2025.}
\resumeItem{Built date-driven backtesting and on-demand ETL pipelines for historical and current NYISO prices, with PostgreSQL upserts and 35,088 hourly observations across two years.}
```

The first result uses development/validation data. Discuss the separate 2026 improvement of 3.99% when explaining performance beyond model selection. If retaining a 15% figure, label it as a target.
