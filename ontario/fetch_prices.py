"""
ETL for Ontario: download the Hourly Ontario Energy Price (HOEP) from IESO, clean it, store it.

Source: IESO public reports, yearly HOEP & predispatch CSVs - free, no API key.
Market: Ontario real-time energy market, CAD/MWh. HOEP was the real-time uniform price
until IESO's Market Renewal went live on 2025-05-01, when it was replaced by the
Ontario Zonal Price and a day-ahead market. IESO's public folder only keeps about 90 days
of the new zonal prices, so this series stops at 2025-04-30.

Usage:
    python ontario/fetch_prices.py                        # default: 2022-01-01 to 2025-04-30
    python ontario/fetch_prices.py 2022-01-01 2024-12-31
"""

import io
import os
import sys
import time

import pandas as pd
import requests

API_URL = "https://reports-public.ieso.ca/public/PriceHOEPPredispOR/PUB_PriceHOEPPredispOR_{year}.csv"
ZONE = "ON"
LAST_HOEP_DAY = "2025-04-30"
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prices.csv")


def extract(start, end):
    """Download one yearly CSV per year in the window."""
    frames = []
    for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        resp = requests.get(API_URL.format(year=year), timeout=180)
        resp.raise_for_status()
        frames.append(parse_report(resp.text))
        print(f"  extracted {year}: {len(frames[-1])} rows")
        time.sleep(2)  # be polite to the public report server
    return pd.concat(frames, ignore_index=True)


def parse_report(text):
    """The CSV starts with three '\\\\' title lines before the 'Date,Hour,HOEP,...' header."""
    return pd.read_csv(io.StringIO(text), skiprows=3, usecols=["Date", "Hour", "HOEP"])


def transform(raw, start, end):
    """IESO hours are hour-ending 1-24 in Eastern Standard Time all year (no daylight saving)."""
    df = raw.dropna(subset=["HOEP"])
    begin = pd.to_datetime(df["Date"]) + pd.to_timedelta(df["Hour"].astype(int) - 1, unit="h")
    utc = pd.DatetimeIndex(begin).tz_localize("Etc/GMT+5").tz_convert("UTC")  # Etc/GMT+5 is UTC-5
    prices = pd.Series(pd.to_numeric(df["HOEP"], errors="coerce").to_numpy(), index=utc)
    prices = prices[~prices.index.duplicated()].sort_index()
    hourly = prices.resample("1h").mean().interpolate(limit=3)  # patch small gaps only
    local_day = hourly.index.tz_convert("America/Toronto").normalize().tz_localize(None)
    hourly = hourly[(local_day >= pd.Timestamp(start)) & (local_day <= pd.Timestamp(end))]
    out = hourly.dropna().to_frame("price_cad_mwh")
    out.index.name = "timestamp"
    out["zone"] = ZONE
    return out


def run_etl(start="2022-01-01", end=LAST_HOEP_DAY):
    end = min(pd.Timestamp(end), pd.Timestamp(LAST_HOEP_DAY)).strftime("%Y-%m-%d")
    print(f"ETL Ontario HOEP {start} -> {end}")
    df = transform(extract(start, end), start, end)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    df.to_csv(CSV_PATH)
    print(f"  loaded {len(df)} rows -> {CSV_PATH}")
    return df


if __name__ == "__main__":
    run_etl(*sys.argv[1:3])
