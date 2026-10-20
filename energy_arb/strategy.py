"""Battery dispatch: forecast hourly RT prices, then choose a feasible profitable schedule."""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

from .pipeline import TZ


@dataclass(frozen=True)
class Battery:
    capacity_mwh: float = 2.0
    power_mw: float = 1.0
    efficiency: float = 0.95  # Each direction, so round-trip efficiency is 90.25%.
    fee_per_mwh: float = 1.0
    wear_per_mwh: float = (
        10.0  # Per MWh delivered; research assumption, not a venue fee.
    )
    max_cycles_per_day: float = 1.0

    def __post_init__(self):
        if not np.isfinite(list(vars(self).values())).all():
            raise ValueError("Battery assumptions must be finite")
        if self.capacity_mwh <= 0 or self.power_mw <= 0 or self.max_cycles_per_day <= 0:
            raise ValueError("Capacity, power and cycle limit must be positive")
        if not 0 < self.efficiency <= 1 or min(self.fee_per_mwh, self.wear_per_mwh) < 0:
            raise ValueError("Efficiency must be in (0,1]; costs must be nonnegative")


@dataclass(frozen=True)
class Model:
    name: str
    forecast: str = "day_ahead"
    lookback_days: int = 28
    bias_weight: float = 1.0
    guard: float = (
        0.0  # Extra forecast-only cost per grid MWh; actual costs stay identical.
    )
    spot_weight: float = 1.0
    decay: float = 0.5


# The research command compares these on development data before evaluating later years.
BASELINE = Model("day_ahead_baseline")
# Selected using 2024 development and 2025 validation; locked before the 2026 test.
SELECTED = Model("adaptive_w0.5_d0.9_g0", "adaptive", 28, 0.0, 0.0, 0.5, 0.9)
CANDIDATES = [Model("historical_28d", forecast="historical")]
for days in (7, 28, 56):
    for weight in (0.5, 1.0):
        for guard in (0.0, 2.0, 5.0):
            CANDIDATES.append(
                Model(
                    f"bias_{days}d_w{weight:g}_g{guard:g}", "bias", days, weight, guard
                )
            )

ADAPTIVE_CANDIDATES = [
    Model(
        f"adaptive_w{weight:g}_d{decay:g}_g{guard:g}",
        "adaptive",
        28,
        0.0,
        guard,
        weight,
        decay,
    )
    for weight in (0.5, 1.0)
    for decay in (0.0, 0.5, 0.9)
    for guard in (0.0, 2.0)
]
LEARNED_CANDIDATES = [
    Model(f"learned_{days}d_w{weight:g}", "learned", days, 0.0, 0.0, weight)
    for days in (28, 56)
    for weight in (0.5, 1.0)
]


def forecast_prices(
    history: pd.DataFrame, delivery_da: pd.Series, day, model: Model
) -> np.ndarray:
    """DA prices are known before delivery; RT history excludes the entire preceding day."""
    if model.forecast in ("day_ahead", "adaptive", "learned"):
        return delivery_da.to_numpy(dtype=float)
    day = pd.Timestamp(day)
    cutoff = day - pd.DateOffset(days=1)
    start = cutoff - pd.DateOffset(days=model.lookback_days)
    historical = history.loc[(history.index >= start) & (history.index < cutoff)].copy()
    expected = pd.date_range(start, cutoff, freq="h", inclusive="left")
    if (
        not historical.index.equals(expected)
        or historical[["DA", "RT"]].isna().any().any()
    ):
        raise ValueError(
            f"Need {model.lookback_days} complete history days before {cutoff}"
        )
    hours = historical.index.tz_convert(TZ).hour
    if model.forecast == "historical":
        average = historical.RT.groupby(hours).mean()
        return average.reindex(delivery_da.index.tz_convert(TZ).hour).to_numpy()
    if model.forecast != "bias":
        raise ValueError(f"Unknown forecast: {model.forecast}")
    # Hour-specific systematic RT minus DA error; only past errors can correct today's DA curve.
    bias = (historical.RT - historical.DA).groupby(hours).mean()
    adjustment = bias.reindex(delivery_da.index.tz_convert(TZ).hour).to_numpy()
    return delivery_da.to_numpy() + model.bias_weight * adjustment


def optimize(
    prices,
    battery: Battery = Battery(),
    guard: float = 0.0,
    initial_soc: float = 0.0,
    discharge_budget: float | None = None,
) -> pd.DataFrame:
    """One small mixed-integer program: buy/sell/idle, with physical and daily wear limits."""
    prices = np.asarray(prices, dtype=float)
    if prices.ndim != 1 or not 1 <= len(prices) <= 25 or not np.isfinite(prices).all():
        raise ValueError("Expected 1 to 25 finite hourly forecast prices")
    if not np.isfinite(guard) or guard < 0:
        raise ValueError("guard must be finite and nonnegative")
    if not np.isfinite(initial_soc) or not 0 <= initial_soc <= battery.capacity_mwh:
        raise ValueError("initial_soc must be within battery capacity")
    if discharge_budget is None:
        discharge_budget = (
            battery.capacity_mwh * battery.efficiency * battery.max_cycles_per_day
        )
    if not np.isfinite(discharge_budget) or discharge_budget < -1e-7:
        raise ValueError("discharge_budget must be finite and nonnegative")
    n = len(prices)
    identity, zero, cumulative = np.eye(n), np.zeros((n, n)), np.tril(np.ones((n, n)))
    # Variables: grid purchases [n], grid sales [n], charging-mode binary flags [n].
    # Minimize cost = purchases - sale revenue + costs + a forecast uncertainty penalty.
    objective = np.r_[
        prices + battery.fee_per_mwh + guard,
        -prices + battery.fee_per_mwh + battery.wear_per_mwh + guard,
        np.zeros(n),
    ]
    soc_matrix = np.c_[
        battery.efficiency * cumulative, -cumulative / battery.efficiency, zero
    ]
    # SOC bounds; end empty; charge only when mode=1; sell only when mode=0; daily cycle cap.
    matrix = np.vstack(
        [
            soc_matrix,
            soc_matrix[-1],
            np.c_[identity, zero, -battery.power_mw * identity],
            np.c_[zero, identity, battery.power_mw * identity],
            np.r_[np.zeros(n), np.ones(n), np.zeros(n)],
        ]
    )
    lower = np.r_[np.full(n, -initial_soc), -initial_soc, np.full(2 * n + 1, -np.inf)]
    upper = np.r_[
        np.full(n, battery.capacity_mwh - initial_soc),
        -initial_soc,
        np.zeros(n),
        np.full(n, battery.power_mw),
        max(discharge_budget, 0.0),
    ]
    result = milp(
        objective,
        integrality=np.r_[np.zeros(2 * n), np.ones(n)],
        bounds=Bounds(
            np.zeros(3 * n), np.r_[np.full(2 * n, battery.power_mw), np.ones(n)]
        ),
        constraints=LinearConstraint(matrix, lower, upper),
        options={"mip_rel_gap": 1e-7, "time_limit": 10.0},
    )
    if not result.success:
        raise RuntimeError(f"Dispatch optimization failed: {result.message}")
    charge, discharge = np.maximum(result.x[:n], 0), np.maximum(result.x[n : 2 * n], 0)
    return pd.DataFrame({"charge_mwh": charge, "discharge_mwh": discharge})


def plan_day(
    history, delivery_da, day, model: Model, battery: Battery, lagged_spot=None
):
    """Plan without realized delivery-hour settlement prices; update only from delayed ticks."""
    forecast = forecast_prices(history, delivery_da, day, model).copy()
    if model.forecast not in ("adaptive", "learned"):
        return optimize(forecast, battery, model.guard), forecast
    if lagged_spot is None or np.isinf(lagged_spot).any():
        raise ValueError("Adaptive strategy requires five-minute observation history")
    actions, soc = [], 0.0
    weights = model.spot_weight * model.decay ** np.arange(25)
    if model.forecast == "learned":
        cutoff = day - pd.DateOffset(days=1)
        start = cutoff - pd.DateOffset(days=model.lookback_days)
        training = history.loc[(history.index >= start) & (history.index < cutoff)]
        if len(training) < 23 * model.lookback_days:
            raise ValueError("Insufficient completed history to learn price response")
        x = (training.lagged_spot - training.DA).to_numpy()
        y = (training.RT - training.DA).to_numpy()
        # Fit one least-squares response per horizon. Future targets stay inside past history.
        weights = np.zeros(25)
        for horizon in range(25):
            feature = x[: len(x) - horizon] if horizon else x
            target = y[horizon:]
            valid = np.isfinite(feature) & np.isfinite(target)
            denominator = np.dot(feature[valid], feature[valid])
            slope = (
                np.dot(feature[valid], target[valid]) / denominator
                if denominator > 0
                else 0.0
            )
            weights[horizon] = model.spot_weight * np.clip(slope, 0.0, 1.0)
    budget = battery.capacity_mwh * battery.efficiency * battery.max_cycles_per_day
    for hour in range(len(delivery_da)):
        remaining = len(delivery_da) - hour
        observed = lagged_spot.iloc[hour]
        # Missing timely ticks cause a DA-only forecast, not a missing evaluation hour.
        innovation = observed - delivery_da.iloc[hour] if np.isfinite(observed) else 0.0
        correction = innovation * weights[:remaining]
        updated = forecast[hour:] + correction
        plan = optimize(updated, battery, model.guard, soc, budget)
        action = plan.iloc[
            0
        ]  # Execute one hour, then update the forecast and solve again.
        actions.append(action)
        soc += (
            action.charge_mwh * battery.efficiency
            - action.discharge_mwh / battery.efficiency
        )
        soc = float(np.clip(soc, 0.0, battery.capacity_mwh))
        budget = max(0.0, budget - action.discharge_mwh)
        forecast[hour] = updated[0]
    return pd.DataFrame(actions).reset_index(drop=True), forecast


def settle(
    plan: pd.DataFrame, actual_prices, battery: Battery = Battery()
) -> pd.DataFrame:
    """Independently check the plan and calculate cash P&L using realized prices."""
    frame = plan.copy().reset_index(drop=True)
    prices = np.asarray(actual_prices, dtype=float)
    if prices.shape != (len(frame),) or not np.isfinite(prices).all():
        raise ValueError("Realized prices must match the plan and be finite")
    charge, discharge = frame.charge_mwh.to_numpy(), frame.discharge_mwh.to_numpy()
    if not np.isfinite(np.r_[charge, discharge]).all():
        raise ValueError("Dispatch must be finite")
    if (
        min(charge.min(), discharge.min()) < -1e-7
        or max(charge.max(), discharge.max()) > battery.power_mw + 1e-7
    ):
        raise ValueError("Dispatch violates hourly power limits")
    if ((charge > 1e-7) & (discharge > 1e-7)).any():
        raise ValueError("Simultaneous charging and discharging")
    soc = np.cumsum(charge * battery.efficiency - discharge / battery.efficiency)
    if (
        soc.min() < -1e-6
        or soc.max() > battery.capacity_mwh + 1e-6
        or abs(soc[-1]) > 1e-6
    ):
        raise ValueError("Dispatch violates capacity or leaves residual inventory")
    if (
        discharge.sum()
        > battery.capacity_mwh * battery.efficiency * battery.max_cycles_per_day + 1e-6
    ):
        raise ValueError("Dispatch violates the daily cycle limit")
    frame["soc_mwh"] = np.clip(soc, 0, battery.capacity_mwh)
    frame["actual_price"] = prices
    frame["energy_cashflow"] = (discharge - charge) * prices
    frame["fees"] = (charge + discharge) * battery.fee_per_mwh
    frame["wear"] = discharge * battery.wear_per_mwh
    frame["net_profit"] = frame.energy_cashflow - frame.fees - frame.wear
    return frame
