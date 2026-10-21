"""
ETL for Alberta: download hourly pool prices from AESO, clean them, store them.

Source: AESO Energy Trading System, Historical Pool Price report - free, no API key.
Market: Alberta real-time energy-only pool, CAD/MWh. There is no day-ahead auction;
the pool price is set each hour by real-time dispatch. Price floor CAD 0, cap CAD 999.99.

Usage:
    python alberta/fetch_prices.py                        # default: 2022-01-01 to today
    python alberta/fetch_prices.py 2022-01-01 2026-10-20
"""

import io
import os
import sys
import time

import pandas as pd
import requests

# The AESO report server does not complete a TLS handshake, so this public report uses HTTP
API_URL = "http://ets.aeso.ca/ets_web/ip/Market/Reports/HistoricalPoolPriceReportServlet"
ZONE = "AB"
TZ = "America/Edmonton"
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prices.csv")


def extract(start, end):
    """Download the report one calendar year per request (the end date is exclusive)."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    frames = []
    for year in range(start.year, end.year + 1):
        chunk_start = max(start, pd.Timestamp(f"{year}-01-01"))
        chunk_end = min(end, pd.Timestamp(f"{year + 1}-01-01"))
        resp = requests.get(API_URL, timeout=180, params={
            "beginDate": chunk_start.strftime("%m%d%Y"), "endDate": chunk_end.strftime("%m%d%Y"),
            "contentType": "csv"})
        resp.raise_for_status()
        frames.append(parse_report(resp.text))
        print(f"  extracted {year}: {len(frames[-1])} rows")
        time.sleep(2)  # be polite to the free report server
    return pd.concat(frames, ignore_index=True)


def parse_report(text):
    """The CSV starts with a title block; the table begins at the 'Date (HE)' header."""
    header = text.index("Date (HE)")
    return pd.read_csv(io.StringIO(text[header:]), dtype=str)


def transform(raw):
    """Turn AESO hour-ending local labels into one price per UTC hour."""
    label = raw["Date (HE)"].str.strip()
    repeated = label.str.endswith("*")
    day = pd.to_datetime(label.str[:10], format="%m/%d/%Y")
    hour_ending = label.str[11:13].astype(int)
    start = pd.DatetimeIndex(day + pd.to_timedelta(hour_ending - 1, unit="h"))
    # Labels are local clock hours. On the fall-back day the repeated hour is "02*" (standard time);
    # on the spring-forward day HE02 is skipped and HE03 is really 01:00-02:00 MST.
    local = start.tz_localize(TZ, ambiguous=~repeated.to_numpy(), nonexistent=pd.Timedelta(hours=-1))
    prices = pd.Series(pd.to_numeric(raw["Price ($)"], errors="coerce").to_numpy(),
                       index=local.tz_convert("UTC"))
    prices = prices[~prices.index.duplicated()].sort_index()
    hourly = prices.resample("1h").mean().interpolate(limit=3)  # patch small gaps only
    out = hourly.dropna().to_frame("price_cad_mwh")
    out.index.name = "timestamp"
    out["zone"] = ZONE
    return out


def run_etl(start="2022-01-01", end=None):
    end = end or pd.Timestamp.now(tz=TZ).strftime("%Y-%m-%d")
    print(f"ETL Alberta pool prices {start} -> {end}")
    df = transform(extract(start, end))
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    df.to_csv(CSV_PATH)
    print(f"  loaded {len(df)} rows -> {CSV_PATH}")
    return df


if __name__ == "__main__":
    run_etl(*sys.argv[1:3])
