"""
Shared runner for the Canadian backtests in alberta/ and ontario/.

Same algorithm, cost model and benchmarks as backtest.py (forecast + dynamic programming,
fixed-hour baseline, perfect-price benchmark). Only the market data, time zone and currency differ:
prices, fees, wear and capital are all in CAD.
"""

import argparse
from dataclasses import asdict
import json
import os

import pandas as pd

from backtest import autocorr_lags, metrics, nonnegative_cost, positive_amount, run_backtest
from strategy import Battery

# Research assumptions, converted from the DE-LU defaults at roughly 1.5 CAD per EUR
FEE_CAD_MWH = 1.5        # per grid MWh bought or sold; not a verified AESO/IESO tariff
WEAR_CAD_MWH = 7.5       # per internal battery MWh discharged
CAPITAL_CAD = 750_000    # 2 MWh at ~CAD 375k/MWh installed
RISK_FREE_ANNUAL = 0.025  # assumed annual CAD risk-free rate


def load_prices(csv_path, tz):
    """Read a market's cleaned CSV and convert timestamps to the market's local time."""
    if not os.path.exists(csv_path):
        raise SystemExit(f"{csv_path} not found - run the market's fetch_prices.py first")
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=["timestamp"])
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(tz)
    # run_backtest reads the price column by the DE-LU name; the values here stay in CAD/MWh
    return df.rename(columns={"price_cad_mwh": "price_eur_mwh"})


def main(market, csv_path, tz, default_start, results_dir, notes):
    parser = argparse.ArgumentParser(description=f"Backtest battery arbitrage on {market} prices (CAD).")
    parser.add_argument("test_start", nargs="?", default=default_start)
    parser.add_argument("test_end", nargs="?", default=None)
    parser.add_argument("--fee", type=nonnegative_cost, default=FEE_CAD_MWH,
                        help="CAD per grid MWh bought or sold (default: %(default)s; research estimate)")
    parser.add_argument("--wear", type=nonnegative_cost, default=WEAR_CAD_MWH,
                        help="CAD per internal battery MWh discharged (default: %(default)s)")
    parser.add_argument("--capital", type=positive_amount, default=CAPITAL_CAD,
                        help="CAD capital allocated, the Sharpe return denominator (default: %(default)s)")
    parser.add_argument("--risk-free", type=float, default=RISK_FREE_ANNUAL,
                        help="annual risk-free rate as a decimal (default: %(default)s)")
    parser.add_argument("--output", default=results_dir, help="directory for results and cost assumptions")
    args = parser.parse_args()

    battery = Battery(transaction_fee_eur_mwh=args.fee, degradation_eur_mwh=args.wear)
    prices = load_prices(csv_path, tz)
    print(f"{market}: loaded {len(prices):,} hourly prices "
          f"({prices.index[0]:%Y-%m-%d} -> {prices.index[-1]:%Y-%m-%d})")
    daily, (ch, dh) = run_backtest(prices, battery, args.test_start, args.test_end)

    table = pd.DataFrame({name: metrics(daily[name], battery, daily["baseline"], args.capital, args.risk_free)
                          for name in daily.columns})
    table.index = [i.replace("_eur", "_cad") for i in table.index]
    print(f"\nBattery: {battery.capacity_mwh} MWh / {battery.power_mw} MW, "
          f"{battery.round_trip_eff:.0%} round-trip")
    print(f"Costs: CAD {args.fee:g}/grid MWh bought or sold; CAD {args.wear:g}/internal MWh discharged for wear")
    print("Costs are research assumptions; fixed costs, capital costs and transmission charges are excluded.")
    for note in notes:
        print(note)
    print(f"Sharpe: daily P&L / CAD {args.capital:,.0f} capital, less {args.risk_free:.2%}/yr risk-free.")
    print(f"Baseline schedule: charge {ch}, discharge {dh} (local hours)")
    print(f"Test period: {daily.index[0]:%Y-%m-%d} -> {daily.index[-1]:%Y-%m-%d} ({len(daily)} days)\n")
    print(table.to_string(float_format=lambda x: f"{x:,.2f}"))

    uplift = daily["optimized"].sum() / daily["baseline"].sum() - 1
    capture = daily["optimized"].sum() / daily["perfect"].sum()
    print(f"\nOptimized vs baseline profit: {uplift:+.1%}")
    print(f"Share of perfect-foresight profit captured: {capture:.1%}")

    os.makedirs(args.output, exist_ok=True)
    daily.to_csv(os.path.join(args.output, "daily_pnl.csv"))
    table.to_csv(os.path.join(args.output, "metrics.csv"))
    battery_cad = {k.replace("_eur", "_cad"): v for k, v in asdict(battery).items()}
    assumptions = {
        "market": market,
        "currency": "CAD",
        "test_start": daily.index[0].strftime("%Y-%m-%d"),
        "test_end": daily.index[-1].strftime("%Y-%m-%d"),
        "days": len(daily),
        "battery": battery_cad,
        "transaction_fee_basis": "CAD per grid MWh bought or sold, charged on each side",
        "transaction_fee_status": "Research estimate, not a verified market tariff",
        "wear_basis": "CAD per internal battery MWh discharged",
        "excluded_costs": ["fixed market access", "fixed operating costs", "capital costs",
                           "transmission and distribution charges"],
        "sharpe_capital_cad": args.capital,
        "risk_free_annual": args.risk_free,
        "autocorr_adjustment": f"Lo (2002), {autocorr_lags(len(daily))} lags",
        "notes": notes,
    }
    with open(os.path.join(args.output, "assumptions.json"), "w") as handle:
        json.dump(assumptions, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"\nSaved results to {args.output}/")
