"""
Battery arbitrage algorithm.

Each day, before the day-ahead auction closes, we:
  1. Forecast tomorrow's hourly prices using only prices we already know.
  2. Find the best charge/discharge schedule for that forecast with dynamic programming.
The schedule is then settled at the real prices in the backtest (no look-ahead).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Battery:
    capacity_mwh: float = 2.0       # energy the battery can store
    power_mw: float = 1.0           # max charge/discharge per hour
    round_trip_eff: float = 0.90    # 90% of energy put in comes back out
    degradation_eur_mwh: float = 5.0  # wear cost per internal battery MWh discharged
    soc_step_mwh: float = 0.1       # resolution of the state-of-charge grid
    transaction_fee_eur_mwh: float = 1.0  # research estimate per grid MWh bought or sold

    def __post_init__(self):
        for name in ("degradation_eur_mwh", "transaction_fee_eur_mwh"):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")

    @property
    def one_way_eff(self):
        return np.sqrt(self.round_trip_eff)


def forecast_prices(history, target_index, lookback_days=7):
    """
    Forecast each hour of tomorrow as a blend of yesterday's price at that hour and
    the average at that hour over the last week. Uses only data in `history`.
    """
    recent = history[history.index >= target_index[0] - pd.Timedelta(days=lookback_days)]
    by_hour = recent.groupby(recent.index.hour)
    weekly_avg = by_hour.mean()
    yesterday = by_hour.last()
    blend = 0.5 * yesterday + 0.5 * weekly_avg
    return np.array([blend.get(h, weekly_avg.mean()) for h in target_index.hour])


def optimize_schedule(prices, battery, soc_start=0.0):
    """
    Dynamic programming over a discretised state of charge.
    Returns the battery energy change (MWh, + = charge, - = discharge) for each hour.
    """
    b = battery
    levels = np.round(np.arange(0, b.capacity_mwh + 1e-9, b.soc_step_mwh), 6)
    n_states, n_hours = len(levels), len(prices)

    # delta[i, j]: energy change going from level i to level j in one hour
    delta = levels[None, :] - levels[:, None]
    feasible = np.abs(delta) <= b.power_mw + 1e-9
    grid_mwh = np.where(delta > 0, delta / b.one_way_eff, delta * b.one_way_eff)  # bought(+) / sold(-)
    wear = b.degradation_eur_mwh * np.maximum(-delta, 0)
    fees = b.transaction_fee_eur_mwh * np.abs(grid_mwh)

    value = np.zeros(n_states)  # value-to-go after the last hour (stored energy worth 0)
    best_next = np.zeros((n_hours, n_states), dtype=int)
    for t in range(n_hours - 1, -1, -1):
        reward = -prices[t] * grid_mwh - wear - fees + value[None, :]
        reward[~feasible] = -np.inf
        best_next[t] = reward.argmax(axis=1)
        value = reward.max(axis=1)

    state = int(np.abs(levels - soc_start).argmin())
    actions = np.zeros(n_hours)
    for t in range(n_hours):
        nxt = best_next[t, state]
        actions[t] = levels[nxt] - levels[state]
        state = nxt
    return actions


def fixed_schedule(index, charge_hours, discharge_hours, battery):
    """Baseline: charge and discharge at the same clock hours every day."""
    actions = np.zeros(len(index))
    actions[np.isin(index.hour, charge_hours)] = battery.power_mw
    actions[np.isin(index.hour, discharge_hours)] = -battery.power_mw
    return actions


def pick_fixed_hours(history, battery):
    """Pick the baseline's hours from the average daily price shape in `history`."""
    n = int(round(battery.capacity_mwh / battery.power_mw))
    profile = history.groupby(history.index.hour).mean()
    best, best_spread = None, -np.inf
    for c in range(24 - n):
        for d in range(c + n, 24 - n + 1):
            spread = profile.iloc[d:d + n].mean() - profile.iloc[c:c + n].mean()
            if spread > best_spread:
                best, best_spread = (list(range(c, c + n)), list(range(d, d + n))), spread
    return best
