"""Independent accounting and dispatch checks for transaction fees."""

import itertools
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np
import pandas as pd

from backtest import annualized_ratio, metrics, run_backtest, settle
from strategy import Battery, optimize_schedule


class CostTests(unittest.TestCase):
    def test_fees_use_both_grid_volumes_after_efficiency(self):
        battery = Battery(round_trip_eff=0.81, degradation_eur_mwh=5,
                          transaction_fee_eur_mwh=2)
        pnl, soc = settle([1, -1], [10, 100], battery, 0)
        # Buy 10/9 MWh at EUR 10 plus EUR 2 fees; sell 0.9 MWh
        # at EUR 100 less EUR 2 fees, plus EUR 5 wear on 1 internal MWh.
        np.testing.assert_allclose(pnl, [-12 * 10 / 9, 98 * 0.9 - 5])
        self.assertAlmostEqual(soc, 0)

    def test_negative_prices_still_incur_fees(self):
        battery = Battery(round_trip_eff=0.81, degradation_eur_mwh=5,
                          transaction_fee_eur_mwh=2)
        pnl, _ = settle([1, -1], [-100, -50], battery, 0)
        np.testing.assert_allclose(pnl, [98 * 10 / 9, -52 * 0.9 - 5])

    def test_fees_charge_only_executed_energy(self):
        battery = Battery(round_trip_eff=0.81, transaction_fee_eur_mwh=2)
        pnl, soc = settle([10], [0], battery, 0)
        self.assertAlmostEqual(pnl[0], -2 * 10 / 9)
        self.assertAlmostEqual(soc, 1)
        pnl, soc = settle([1], [100], battery, battery.capacity_mwh)
        self.assertEqual(pnl[0], 0)
        self.assertEqual(soc, battery.capacity_mwh)

    def test_fees_prevent_unprofitable_small_spread(self):
        without_fees = Battery(capacity_mwh=1, round_trip_eff=1,
                               degradation_eur_mwh=0, transaction_fee_eur_mwh=0)
        with_fees = Battery(capacity_mwh=1, round_trip_eff=1,
                            degradation_eur_mwh=0, transaction_fee_eur_mwh=1)
        np.testing.assert_allclose(optimize_schedule([10, 11], without_fees), [1, -1])
        np.testing.assert_allclose(optimize_schedule([10, 11], with_fees), [0, 0])

    def test_optimizer_matches_independent_exhaustive_search(self):
        rng = np.random.default_rng(2026)
        for case in range(40):
            with self.subTest(case=case):
                battery = Battery(capacity_mwh=2, soc_step_mwh=1,
                                  degradation_eur_mwh=float(rng.uniform(0, 20)),
                                  transaction_fee_eur_mwh=float(rng.uniform(0, 5)))
                prices = rng.uniform(-150, 250, 5)
                initial = float(rng.integers(0, 3))
                best = -np.inf
                for actions in itertools.product([-1, 0, 1], repeat=5):
                    state, profit = initial, 0.0
                    for delta, price in zip(actions, prices):
                        state += delta
                        if state < 0 or state > 2:
                            break
                        if delta > 0:
                            profit -= (price + battery.transaction_fee_eur_mwh) / battery.one_way_eff
                        elif delta < 0:
                            profit += ((price - battery.transaction_fee_eur_mwh) * battery.one_way_eff
                                       - battery.degradation_eur_mwh)
                    else:
                        best = max(best, profit)
                actions = optimize_schedule(prices, battery, initial)
                actual, _ = settle(actions, prices, battery, initial)
                self.assertAlmostEqual(actual.sum(), best, places=8)

    def test_all_backtest_strategies_pay_fees(self):
        index = pd.date_range("2023-12-25", "2024-01-02 23:00", freq="h", tz="Europe/Berlin")
        values = np.full(len(index), 50.0)
        values[np.isin(index.hour, [13, 14])] = 10
        values[np.isin(index.hour, [19, 20])] = 100
        prices = pd.DataFrame({"price_eur_mwh": values}, index=index)
        free, _ = run_backtest(prices, Battery(transaction_fee_eur_mwh=0),
                               "2024-01-01", "2024-01-02")
        paid, _ = run_backtest(prices, Battery(transaction_fee_eur_mwh=1),
                               "2024-01-01", "2024-01-02")
        expected_daily_fee = 2 / np.sqrt(0.9) + 2 * np.sqrt(0.9)
        np.testing.assert_allclose(free - paid, expected_daily_fee)
        self.assertEqual(list(paid.columns), ["optimized", "baseline", "perfect"])

    def test_sharpe_uses_capital_returns_risk_free_and_sample_std(self):
        index = pd.date_range("2024-01-01", periods=4, freq="D")
        pnl = pd.Series([100.0, 300.0, 200.0, 400.0], index=index)
        baseline = pd.Series([50.0, 250.0, 250.0, 250.0], index=index)
        result = metrics(pnl, Battery(), baseline, capital_eur=10_000, risk_free_annual=0.05)
        excess = pnl / 10_000 - (1.05 ** (1 / 365) - 1)
        expected = excess.mean() / np.std(excess, ddof=1) * np.sqrt(365)
        self.assertAlmostEqual(result["sharpe_annualized"], expected)
        diff = pnl - baseline
        self.assertAlmostEqual(result["ir_vs_baseline"],
                               diff.mean() / np.std(diff, ddof=1) * np.sqrt(365))
        self.assertTrue(np.isnan(metrics(baseline, Battery(), baseline, 10_000, 0.05)["ir_vs_baseline"]))

    def test_positive_autocorrelation_lowers_adjusted_ratio(self):
        rng = np.random.default_rng(0)
        noise = rng.normal(size=2000)
        iid = pd.Series(1 + noise)
        persistent = pd.Series(1 + np.convolve(noise, np.ones(5) / 5, mode="same"))
        plain, adjusted, _ = annualized_ratio(iid)
        self.assertAlmostEqual(adjusted / plain, 1, delta=0.15)
        plain, adjusted, rho1 = annualized_ratio(persistent)
        self.assertGreater(rho1, 0.5)
        self.assertLess(adjusted, 0.6 * plain)

    def test_costs_must_be_finite_and_nonnegative(self):
        for name in ("transaction_fee_eur_mwh", "degradation_eur_mwh"):
            for value in (-1, np.nan, np.inf):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    Battery(**{name: value})

    def test_cli_rejects_invalid_cost_before_loading_data(self):
        script = Path(__file__).resolve().parents[1] / "backtest.py"
        for option in ("--fee", "--wear"):
            for value in ("-1", "nan", "inf"):
                with self.subTest(option=option, value=value):
                    result = subprocess.run([sys.executable, str(script), option, value],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("finite and nonnegative", result.stderr)


if __name__ == "__main__":
    unittest.main()
