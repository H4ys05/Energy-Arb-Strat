"""
Walk-forward battery arbitrage backtest on Alberta pool prices (CAD).

Usage:
    python alberta/fetch_prices.py                 # download prices first
    python alberta/run_backtest.py                 # test 2023-01-01 -> end of data
    python alberta/run_backtest.py 2025-01-01 2025-12-31 --fee 1.5 --wear 15
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from canada_backtest import main  # noqa: E402

NOTES = [
    "Alberta has no day-ahead auction: the schedule is fixed from the previous day's prices and",
    "settled at the real-time pool price, so a battery re-optimizing intraday could do better or worse.",
]

if __name__ == "__main__":
    main("Alberta (AESO pool price)", os.path.join(HERE, "data", "prices.csv"), "America/Edmonton",
         "2023-01-01", os.path.join(HERE, "results"), NOTES)
