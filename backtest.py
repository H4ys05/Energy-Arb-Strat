"""
Backtesting engine.

Walks forward one day at a time. For each day, a strategy produces an hourly schedule
from past data, and the engine settles it at the actual day-ahead prices.

Compares three strategies:
  - optimized : forecast + dynamic programming (the algorithm)
  - baseline  : fixed clock-hour schedule (picked from the warm-up year)
  - perfect   : DP with perfect knowledge of each day's prices

Usage:
    python backtest.py                         # test 2024-01-01 -> end of data
    python backtest.py 2026-01-01              # test from a start date
    python backtest.py 2024-01-01 2025-12-31   # test a specific window
    python backtest.py 2026-01-01 --fee 1 --wear 10
"""

import argparse
from dataclasses import asdict
import json
import os

import numpy as np
import pandas as pd

from data import load_prices
from strategy import Battery, fixed_schedule, forecast_prices, optimize_schedule, pick_fixed_hours

TEST_START = "2024-01-01"   # data before this is only used to warm up forecasts / pick baseline hours
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
CAPITAL_EUR = 500_000      # assumed capital allocated: 2 MWh at ~EUR 250k/MWh installed (research assumption)
RISK_FREE_ANNUAL = 0.02    # assumed annual EUR risk-free rate


def settle(actions, prices, battery, soc):
    """Apply a schedule hour by hour within battery limits; return P&L per hour and final SoC."""
    pnl = np.zeros(len(actions))
    for t, want in enumerate(actions):
        delta = np.clip(want, -min(battery.power_mw, soc), min(battery.power_mw, battery.capacity_mwh - soc))
        grid = delta / battery.one_way_eff if delta > 0 else delta * battery.one_way_eff
        fee = battery.transaction_fee_eur_mwh * abs(grid)
        pnl[t] = -prices[t] * grid - battery.degradation_eur_mwh * max(-delta, 0) - fee
        soc += delta
    return pnl, soc


def run_backtest(prices, battery, test_start=TEST_START, test_end=None):
    series = prices["price_eur_mwh"]
    start = pd.Timestamp(test_start, tz=series.index.tz)
    end = pd.Timestamp(test_end, tz=series.index.tz) + pd.Timedelta(days=1) if test_end else series.index[-1] + pd.Timedelta(hours=1)
    test = series[(series.index >= start) & (series.index < end)]
    warmup = series[(series.index < start) & (series.index >= start - pd.Timedelta(days=365))]
    charge_h, discharge_h = pick_fixed_hours(warmup, battery)  # baseline uses the prior 12 months

    soc = {"optimized": 0.0, "baseline": 0.0, "perfect": 0.0}
    rows = []
    for day, actual in test.groupby(test.index.date):
        idx, p = actual.index, actual.to_numpy()
        history = series[series.index < idx[0]]
        plans = {
            "optimized": optimize_schedule(forecast_prices(history, idx), battery, soc["optimized"]),
            "baseline": fixed_schedule(idx, charge_h, discharge_h, battery),
            "perfect": optimize_schedule(p, battery, soc["perfect"]),
        }
        row = {"date": pd.Timestamp(day)}
        for name, plan in plans.items():
            pnl, soc[name] = settle(plan, p, battery, soc[name])
            row[name] = pnl.sum()
        rows.append(row)
    daily = pd.DataFrame(rows).set_index("date")
    return daily, (charge_h, discharge_h)


def autocorr_lags(n_obs):
    """Newey-West rule-of-thumb lag truncation for autocorrelation corrections."""
    return int(np.floor(4 * (n_obs / 100) ** (2 / 9)))


def annualized_ratio(series, periods=365, max_lag=None):
    """
    Annualize mean / std (ddof=1) of a daily series.

    Returns (sqrt-of-time ratio, Lo (2002) autocorrelation-adjusted ratio, lag-1 autocorrelation).
    sqrt(periods) scaling assumes i.i.d. days; positive serial correlation makes it overstate.
    Lo's adjustment scales by periods / sqrt(periods + 2 * sum_k (periods - k) * rho_k), with
    autocorrelations truncated at `max_lag`.
    """
    x = series.dropna()
    sd = x.std(ddof=1)
    if len(x) < 2 or not sd > 0:
        return np.nan, np.nan, np.nan
    daily = x.mean() / sd
    max_lag = autocorr_lags(len(x)) if max_lag is None else max_lag
    rho = np.array([x.autocorr(k) for k in range(1, min(max_lag, len(x) - 2) + 1)])
    k = np.arange(1, len(rho) + 1)
    var_factor = periods + 2 * np.sum((periods - k) * rho)
    adjusted = daily * periods / np.sqrt(var_factor) if var_factor > 0 else np.nan
    return daily * np.sqrt(periods), adjusted, rho[0] if len(rho) else np.nan


def metrics(daily_pnl, battery, baseline_pnl, capital_eur, risk_free_annual):
    cum = daily_pnl.cumsum()
    years = len(daily_pnl) / 365
    # Sharpe on net operating returns: daily P&L (after fees, efficiency, wear) / capital allocated
    rf_daily = (1 + risk_free_annual) ** (1 / 365) - 1
    sharpe, sharpe_adj, sharpe_rho1 = annualized_ratio(daily_pnl / capital_eur - rf_daily)
    # Information-ratio-style measure on incremental daily P&L vs the fixed schedule
    if daily_pnl is baseline_pnl:
        ir, ir_adj, ir_rho1 = np.nan, np.nan, np.nan
    else:
        incremental = (daily_pnl - baseline_pnl.reindex(daily_pnl.index)).dropna()
        ir, ir_adj, ir_rho1 = annualized_ratio(incremental)
    return {
        "total_profit_eur": daily_pnl.sum(),
        "profit_per_year_eur": daily_pnl.sum() / years,
        "return_on_capital_annual": daily_pnl.sum() / years / capital_eur,
        "sharpe_annualized": sharpe,
        "sharpe_autocorr_adjusted": sharpe_adj,
        "sharpe_lag1_autocorr": sharpe_rho1,
        "ir_vs_baseline": ir,
        "ir_autocorr_adjusted": ir_adj,
        "ir_lag1_autocorr": ir_rho1,
        "max_drawdown_eur": (cum - cum.cummax()).min(),
        "win_rate": (daily_pnl > 0).mean(),
    }


def main(test_start=TEST_START, test_end=None, *, battery=None, results_dir=RESULTS_DIR,
         capital_eur=CAPITAL_EUR, risk_free_annual=RISK_FREE_ANNUAL):
    battery = Battery() if battery is None else battery
    prices = load_prices()
    print(f"Loaded {len(prices):,} hourly prices "
          f"({prices.index[0]:%Y-%m-%d} -> {prices.index[-1]:%Y-%m-%d})")
    daily, (ch, dh) = run_backtest(prices, battery, test_start, test_end)

    table = pd.DataFrame({name: metrics(daily[name], battery, daily["baseline"], capital_eur, risk_free_annual)
                          for name in daily.columns})
    print(f"\nBattery: {battery.capacity_mwh} MWh / {battery.power_mw} MW, "
          f"{battery.round_trip_eff:.0%} round-trip")
    print(f"Costs: EUR {battery.transaction_fee_eur_mwh:g}/grid MWh bought or sold; "
          f"EUR {battery.degradation_eur_mwh:g}/internal MWh discharged for wear")
    print("Costs are research assumptions; fixed costs and capital costs are excluded.")
    print(f"Sharpe: daily P&L / EUR {capital_eur:,.0f} capital, less {risk_free_annual:.2%}/yr risk-free; "
          f"IR: optimized/perfect minus baseline daily P&L; both ddof=1. Autocorrelation-adjusted "
          f"versions use Lo (2002) with {autocorr_lags(len(daily))} lags.")
    print(f"Baseline schedule: charge {ch}, discharge {dh} (local hours)")
    print(f"Test period: {daily.index[0]:%Y-%m-%d} -> {daily.index[-1]:%Y-%m-%d} ({len(daily)} days)\n")
    print(table.to_string(float_format=lambda x: f"{x:,.2f}"))

    uplift = daily["optimized"].sum() / daily["baseline"].sum() - 1
    capture = daily["optimized"].sum() / daily["perfect"].sum()
    print(f"\nOptimized vs baseline profit: {uplift:+.1%}")
    print(f"Share of perfect-foresight profit captured: {capture:.1%}")

    os.makedirs(results_dir, exist_ok=True)
    daily.to_csv(os.path.join(results_dir, "daily_pnl.csv"))
    table.to_csv(os.path.join(results_dir, "metrics.csv"))
    assumptions = {
        "test_start": daily.index[0].strftime("%Y-%m-%d"),
        "test_end": daily.index[-1].strftime("%Y-%m-%d"),
        "days": len(daily),
        "battery": asdict(battery),
        "transaction_fee_basis": "EUR per grid MWh bought or sold, charged on each side",
        "transaction_fee_status": "Research estimate, not a verified all-in market tariff",
        "wear_basis": "EUR per internal battery MWh discharged",
        "costs_applied_to": list(daily.columns),
        "excluded_costs": ["fixed market access", "fixed operating costs", "capital costs"],
        "sharpe_capital_eur": capital_eur,
        "sharpe_capital_status": "Research assumption for installed battery cost, not a quote",
        "risk_free_annual": risk_free_annual,
        "ratio_std_ddof": 1,
        "autocorr_adjustment": f"Lo (2002), {autocorr_lags(len(daily))} lags",
    }
    with open(os.path.join(results_dir, "assumptions.json"), "w") as handle:
        json.dump(assumptions, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"\nSaved results to {results_dir}/")


def nonnegative_cost(value):
    cost = float(value)
    if not np.isfinite(cost) or cost < 0:
        raise argparse.ArgumentTypeError("cost must be finite and nonnegative")
    return cost


def positive_amount(value):
    amount = float(value)
    if not np.isfinite(amount) or amount <= 0:
        raise argparse.ArgumentTypeError("amount must be finite and positive")
    return amount


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest battery arbitrage after fees and wear.")
    parser.add_argument("test_start", nargs="?", default=TEST_START)
    parser.add_argument("test_end", nargs="?", default=None)
    parser.add_argument("--fee", type=nonnegative_cost, default=Battery.transaction_fee_eur_mwh,
                        help="EUR per grid MWh bought or sold (default: %(default)s; research estimate)")
    parser.add_argument("--wear", type=nonnegative_cost, default=Battery.degradation_eur_mwh,
                        help="EUR per internal battery MWh discharged (default: %(default)s)")
    parser.add_argument("--capital", type=positive_amount, default=CAPITAL_EUR,
                        help="EUR capital allocated, the Sharpe return denominator (default: %(default)s)")
    parser.add_argument("--risk-free", type=float, default=RISK_FREE_ANNUAL,
                        help="annual risk-free rate as a decimal (default: %(default)s)")
    parser.add_argument("--output", default=RESULTS_DIR, help="directory for results and cost assumptions")
    args = parser.parse_args()
    main(args.test_start, args.test_end,
         battery=Battery(transaction_fee_eur_mwh=args.fee, degradation_eur_mwh=args.wear),
         results_dir=args.output, capital_eur=args.capital, risk_free_annual=args.risk_free)
