"""Timestamp parsing checks for the Alberta and Ontario price downloads."""

from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alberta"))
import fetch_prices as aeso  # noqa: E402
sys.path.pop(0)
del sys.modules["fetch_prices"]
sys.path.insert(0, str(ROOT / "ontario"))
import fetch_prices as ieso  # noqa: E402
sys.path.pop(0)


class AlbertaParsingTests(unittest.TestCase):
    def test_report_title_block_is_skipped(self):
        text = 'Pool Price\n\n""\n\nDate (HE),Price ($),30Ravg ($),AIL Demand (MW)\n"01/01/2024 01","23.05","51.98","9809.0"\n'
        self.assertEqual(aeso.parse_report(text)["Price ($)"].tolist(), ["23.05"])

    def test_spring_forward_day_has_23_consecutive_hours(self):
        labels = [f"03/10/2024 {h:02d}" for h in range(1, 25) if h != 2]
        out = aeso.transform(pd.DataFrame({"Date (HE)": labels, "Price ($)": [str(h) for h in range(23)]}))
        self.assertEqual(len(out), 23)
        self.assertEqual(out.index[0], pd.Timestamp("2024-03-10 07:00", tz="UTC"))  # 00:00 MST
        self.assertEqual(out.index[1], pd.Timestamp("2024-03-10 08:00", tz="UTC"))  # HE03 = 01:00 MST
        self.assertTrue((out.index.to_series().diff().dropna() == pd.Timedelta("1h")).all())

    def test_fall_back_repeated_hour_is_kept(self):
        labels = ["11/03/2024 01", "11/03/2024 02", "11/03/2024 02*", "11/03/2024 03"]
        out = aeso.transform(pd.DataFrame({"Date (HE)": labels, "Price ($)": ["1", "2", "3", "4"]}))
        self.assertEqual(out["price_cad_mwh"].tolist(), [1, 2, 3, 4])
        self.assertEqual(out.index[1], pd.Timestamp("2024-11-03 07:00", tz="UTC"))  # 01:00 MDT
        self.assertEqual(out.index[2], pd.Timestamp("2024-11-03 08:00", tz="UTC"))  # 01:00 MST


class OntarioParsingTests(unittest.TestCase):
    def test_hours_are_eastern_standard_time_all_year(self):
        raw = pd.DataFrame({"Date": ["2024-07-01", "2024-07-01"], "Hour": [1, 24], "HOEP": [10.0, 20.0]})
        out = ieso.transform(raw, "2024-06-30", "2024-07-02")
        self.assertEqual(out.index[0], pd.Timestamp("2024-07-01 05:00", tz="UTC"))
        self.assertEqual(out.index[-1], pd.Timestamp("2024-07-02 04:00", tz="UTC"))


if __name__ == "__main__":
    unittest.main()
