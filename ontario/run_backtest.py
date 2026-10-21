"""
Walk-forward battery arbitrage backtest on Ontario HOEP prices (CAD).

Usage:
    python ontario/fetch_prices.py                 # download prices first
    python ontario/run_backtest.py                 # test 2023-01-01 -> 2025-04-30 (end of HOEP)
    python ontario/run_backtest.py 2024-01-01 2024-12-31 --fee 1.5 --wear 15
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from canada_backtest import main  # noqa: E402

NOTES = [
    "HOEP was a real-time price: the schedule is fixed from the previous day's prices and settled at HOEP.",
    "HOEP ends 2025-04-30 (Market Renewal); results do not cover Ontario's new day-ahead market.",
    "Global Adjustment and other non-energy charges on grid withdrawals are excluded.",
]

if __name__ == "__main__":
    main("Ontario (IESO HOEP)", os.path.join(HERE, "data", "prices.csv"), "America/Toronto",
         "2023-01-01", os.path.join(HERE, "results"), NOTES)
