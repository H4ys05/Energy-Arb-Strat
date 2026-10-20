"""Give dates, fetch prices, replay the strategy, and compare net profit with a baseline."""

import argparse
import hashlib
import json
import logging
import platform
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import DEFAULT_ZONE, ROOT, TZ, date_bounds, fetch_prices
from .strategy import (
    ADAPTIVE_CANDIDATES,
    BASELINE,
    CANDIDATES,
    LEARNED_CANDIDATES,
    SELECTED,
    Battery,
    Model,
    plan_day,
    settle,
)


def prepare(raw: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if raw.duplicated(["timestamp_utc", "market", "zone"]).any():
        raise ValueError("Duplicate price observations")
    if raw.zone.nunique() != 1:
        raise ValueError("A backtest must use exactly one price zone")
    hourly = raw.loc[raw.market.isin(["DA", "RT"])]
    prices = hourly.pivot(
        index="timestamp_utc", columns="market", values="price_usd_mwh"
    )
    prices.index = pd.DatetimeIndex(prices.index).tz_convert(TZ)
    prices = prices.sort_index()
    prices.attrs["zone"] = raw.zone.iloc[0]
    first, stop = date_bounds(start, end)
    expected = pd.date_range(first, stop, freq="h", inclusive="left").tz_convert(TZ)
    required = prices.reindex(expected)
    if (
        not {"DA", "RT"}.issubset(prices.columns)
        or required[["DA", "RT"]].isna().any().any()
    ):
        raise ValueError(
            "Missing hourly DA/RT prices. Recent days may still be incomplete; try yesterday."
        )
    if not np.isfinite(required[["DA", "RT"]]).all().all():
        raise ValueError("Non-finite hourly prices")
    fast = raw.loc[raw.market.eq("RT5")].sort_values("timestamp_utc")
    if not fast.empty:
        # Five-minute RT timestamps are interval ends. Wait ten minutes after that interval.
        lookups = pd.DataFrame(
            {"known_by": prices.index.tz_convert("UTC") - pd.Timedelta(minutes=10)}
        )
        observed = pd.merge_asof(
            lookups,
            fast[["timestamp_utc", "price_usd_mwh"]],
            left_on="known_by",
            right_on="timestamp_utc",
            direction="backward",
            tolerance=pd.Timedelta(minutes=10),
        )
        prices["lagged_spot"] = observed.price_usd_mwh.to_numpy()
        prices["spot_observation_time"] = observed.timestamp_utc.to_numpy()
    return prices


def simulate(
    prices: pd.DataFrame, start: str, end: str, model: Model, battery: Battery
):
    first, stop = date_bounds(start, end)
    evaluated = prices.loc[(prices.index >= first) & (prices.index < stop)]
    hourly, daily = [], []
    for day, observations in evaluated.groupby(evaluated.index.normalize()):
        plan, forecast = plan_day(
            prices,
            observations.DA,
            day,
            model,
            battery,
            observations.get("lagged_spot"),
        )
        ledger = settle(plan, observations.RT.to_numpy(), battery)
        ledger["timestamp_utc"] = observations.index.tz_convert("UTC")
        ledger["forecast_price"] = forecast
        ledger["day_ahead_price"] = observations.DA.to_numpy()
        ledger["market_date"] = str(day.date())
        ledger["model"] = model.name
        ledger["zone"] = prices.attrs.get("zone", "unknown")
        if model.forecast in ("adaptive", "learned"):
            ledger["spot_observation_time"] = (
                observations.spot_observation_time.to_numpy()
            )
            ledger["spot_available"] = observations.lagged_spot.notna().to_numpy()
        hourly.append(ledger)
        daily.append(
            {
                "market_date": str(day.date()),
                "model": model.name,
                "net_profit": ledger.net_profit.sum(),
                "energy_cashflow": ledger.energy_cashflow.sum(),
                "fees": ledger.fees.sum(),
                "wear": ledger.wear.sum(),
                "charge_mwh": ledger.charge_mwh.sum(),
                "discharge_mwh": ledger.discharge_mwh.sum(),
            }
        )
    if not daily:
        raise ValueError("No evaluation days")
    return pd.concat(hourly, ignore_index=True), pd.DataFrame(daily)


def statistics(daily: pd.DataFrame) -> dict:
    profit = daily.net_profit.to_numpy(dtype=float)
    volatility = profit.std(ddof=1) if len(profit) > 1 else 0.0
    cumulative = np.r_[0.0, profit.cumsum()]
    return {
        "days": len(profit),
        "net_profit_usd": float(profit.sum()),
        "mean_daily_profit_usd": float(profit.mean()),
        "std_daily_profit_usd": float(volatility),
        "daily_pnl_sharpe": float(np.sqrt(365) * profit.mean() / volatility)
        if volatility > 1e-10
        else None,
        "max_daily_drawdown_usd": float(
            (np.maximum.accumulate(cumulative) - cumulative).max()
        ),
        "profitable_days_pct": float(100 * (profit > 0).mean()),
        "active_days": int((daily.charge_mwh > 1e-7).sum()),
        "charge_mwh": float(daily.charge_mwh.sum()),
        "discharge_mwh": float(daily.discharge_mwh.sum()),
        "fees_usd": float(daily.fees.sum()),
        "wear_usd": float(daily.wear.sum()),
    }


def comparison(strategy: dict, baseline: dict) -> dict:
    difference = strategy["net_profit_usd"] - baseline["net_profit_usd"]
    base_profit = baseline["net_profit_usd"]
    improvement = 100 * difference / base_profit if base_profit > 0 else None
    return {
        "profit_difference_usd": difference,
        "profit_improvement_pct": improvement,
        "profit_target_pct": 15.0,
        "profit_target_met": improvement is not None and improvement >= 15,
        "daily_pnl_sharpe_target": 1.5,
        "sharpe_target_met": strategy["daily_pnl_sharpe"] is not None
        and strategy["daily_pnl_sharpe"] >= 1.5,
    }


def evaluate(prices, start, end, model, battery, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    ledgers, days, result = [], [], {}
    for candidate in (BASELINE, model):
        hourly, daily = simulate(prices, start, end, candidate, battery)
        ledgers.append(hourly)
        days.append(daily)
        result[candidate.name] = statistics(daily)
    result["comparison"] = comparison(result[model.name], result[BASELINE.name])
    if model.forecast in ("adaptive", "learned"):
        result["intraday_data"] = {
            "decision_hours": len(ledgers[1]),
            "da_fallback_hours": int((~ledgers[1].spot_available).sum()),
            "minimum_observation_lag_minutes": 10,
        }
    all_daily = pd.concat(days, ignore_index=True)
    result["by_year"] = {}
    for year, year_data in all_daily.groupby(all_daily.market_date.str[:4]):
        summaries = {
            name: statistics(group) for name, group in year_data.groupby("model")
        }
        summaries["comparison"] = comparison(
            summaries[model.name], summaries[BASELINE.name]
        )
        result["by_year"][year] = summaries
    result["assumptions"] = {
        "start": start,
        "end": end,
        "zone": prices.attrs.get("zone", "unknown"),
        "battery": asdict(battery),
        "strategy": asdict(model),
        "baseline": asdict(BASELINE),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "price_table_sha256": hashlib.sha256(prices.to_csv().encode()).hexdigest(),
        "source_sha256": {
            f"energy_arb/{name}": hashlib.sha256(
                (ROOT / "energy_arb" / name).read_bytes()
            ).hexdigest()
            for name in ("pipeline.py", "strategy.py", "backtest.py")
        },
        "python_version": platform.python_version(),
        "package_versions": {
            name: version(name) for name in ("numpy", "pandas", "scipy", "SQLAlchemy")
        },
        "sharpe_definition": "sqrt(365) * mean(daily operating P&L) / sample std; rf=0",
    }
    ledger_dir = output / "ledgers"
    ledger_dir.mkdir(exist_ok=True)
    pd.concat(ledgers, ignore_index=True).to_csv(ledger_dir / "hourly.csv", index=False)
    all_daily.to_csv(ledger_dir / "daily.csv", index=False)
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    plot_results(all_daily, output, prices.attrs.get("zone", "unknown"))
    strategy_summary, baseline_summary = result[model.name], result[BASELINE.name]
    improvement = result["comparison"]["profit_improvement_pct"]
    lines = [
        f"# NYISO {prices.attrs.get('zone', 'unknown')}: {start} through {end}",
        "",
        f"Selected model: `{model.name}`. Prices and profits are USD.",
        "",
        "| Metric | Strategy | Day-ahead forecast baseline |",
        "| --- | ---: | ---: |",
    ]
    for label, key in (
        ("Net operating profit", "net_profit_usd"),
        ("Daily P&L Sharpe", "daily_pnl_sharpe"),
        ("Maximum daily drawdown", "max_daily_drawdown_usd"),
        ("Active days", "active_days"),
    ):
        a, b = strategy_summary[key], baseline_summary[key]
        a, b = ("undefined" if value is None else f"{value:,.2f}" for value in (a, b))
        lines.append(f"| {label} | {a} | {b} |")
    lines.extend(
        [
            "",
            f"Profit improvement: {improvement:.2f}%."
            if improvement is not None
            else "Profit improvement undefined: baseline profit is nonpositive.",
            "",
            "![Cumulative operating profit](profit.png)",
            "",
            "Detailed CSVs: [daily P&L](ledgers/daily.csv) and [hourly dispatch](ledgers/hourly.csv).",
            "",
            "Both models use identical batteries, costs and cycle limits. Plans use published "
            "day-ahead prices as a forecast; settlement uses realized hourly RT prices. "
            "The strategy updates from five-minute observations at least ten minutes old. "
            "Final historical revisions are not publication-vintage data. "
            "Zonal prices proxy actual nodal storage settlement.",
            "",
            "Profits exclude capital costs, fixed operating expenses, financing and taxes. "
            "Sharpe describes simulated operating P&L, rather than investment returns. "
            "Square-root annualization does not correct for serial dependence. "
            "Idle days are included. Each day physically starts and finishes empty.",
            "",
        ]
    )
    (output / "summary.md").write_text("\n".join(lines))
    return result


def plot_results(daily: pd.DataFrame, output: Path, zone: str):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(10, 4))
    for name, group in daily.groupby("model"):
        axis.plot(
            pd.to_datetime(group.market_date), group.net_profit.cumsum(), label=name
        )
    axis.set(xlabel="Delivery date", ylabel="Cumulative net operating profit (USD)")
    axis.set_title(f"NYISO {zone}")
    axis.grid(alpha=0.2)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "profit.png", dpi=150)
    plt.close(figure)


def research(
    prices, start, end, battery, output: Path, adaptive_only=False, learned=False
):
    """Compare predefined candidates only on the explicitly requested development period."""
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    candidates = (
        LEARNED_CANDIDATES
        if learned
        else ADAPTIVE_CANDIDATES
        if adaptive_only
        else CANDIDATES
    )
    for model in [BASELINE, *candidates]:
        logging.info("Evaluating %s", model.name)
        _, daily = simulate(prices, start, end, model, battery)
        rows.append({**asdict(model), **statistics(daily)})
        pd.DataFrame(rows).to_csv(output / "experiments.csv", index=False)
    frame = pd.DataFrame(rows)
    base = frame.loc[frame.name == BASELINE.name, "net_profit_usd"].iloc[0]
    frame["improvement_pct"] = (
        100 * (frame.net_profit_usd - base) / base if base > 0 else np.nan
    )
    frame.sort_values("net_profit_usd", ascending=False).to_csv(
        output / "experiments.csv", index=False
    )
    winner = (
        frame.loc[frame.name != BASELINE.name]
        .sort_values("net_profit_usd", ascending=False)
        .iloc[0]
    )
    selection = {
        "development_start": start,
        "development_end": end,
        "selected_at_utc": datetime.now(timezone.utc).isoformat(),
        "criterion": "Highest development net operating profit, not proximity to a target",
        "model": asdict(
            next(model for model in candidates if model.name == winner["name"])
        ),
        "battery": asdict(battery),
    }
    (output / "selection.json").write_text(
        json.dumps(selection, indent=2, default=str) + "\n"
    )
    print(
        frame[["name", "net_profit_usd", "improvement_pct", "daily_pnl_sharpe"]]
        .sort_values("net_profit_usd", ascending=False)
        .to_string(index=False)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", required=True, help="First delivery date, YYYY-MM-DD"
    )
    parser.add_argument("--end", required=True, help="Last delivery date, inclusive")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--compare", action="store_true", help="Research candidates on these dates"
    )
    parser.add_argument(
        "--adaptive", action="store_true", help="Research intraday-updating candidates"
    )
    parser.add_argument(
        "--learned", action="store_true", help="Research rolling learned responses"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wear", type=float, default=10.0)
    args = parser.parse_args()
    if date.fromisoformat(args.end) >= pd.Timestamp.now(tz=TZ).date():
        parser.error(
            "Backtests require completed days. Use an end date of yesterday or earlier."
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    battery = Battery(wear_per_mwh=args.wear)
    needs_history = args.compare or SELECTED.forecast in (
        "historical",
        "bias",
        "learned",
    )
    warmup_days = 60 if needs_history else 1
    warmup = (date.fromisoformat(args.start) - timedelta(days=warmup_days)).isoformat()
    raw = fetch_prices(warmup, args.end, args.zone, args.refresh)
    prices = prepare(raw, warmup, args.end)
    if args.compare:
        family = "learned" if args.learned else "intraday" if args.adaptive else "daily"
        default_output = (
            ROOT
            / "results/research/runs"
            / args.zone
            / f"{args.start}_{args.end}"
            / family
        )
    else:
        default_output = (
            ROOT / "results/backtests" / args.zone / f"{args.start}_{args.end}"
        )
    output = args.output or default_output
    if args.compare:
        research(
            prices, args.start, args.end, battery, output, args.adaptive, args.learned
        )
    else:
        result = evaluate(prices, args.start, args.end, SELECTED, battery, output)
        print(
            json.dumps(
                {"strategy": result[SELECTED.name], "comparison": result["comparison"]},
                indent=2,
            )
        )
        print(f"Report: {output / 'summary.md'}")
