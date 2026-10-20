"""
ETL pipeline: download hourly day-ahead electricity prices, clean them, store them.

Source: Energy-Charts API (Fraunhofer ISE) - free, no API key.
Market: EPEX Spot day-ahead auction, Germany/Luxembourg bidding zone (DE-LU), EUR/MWh.

Extract  -> pull raw prices month by month from the API
Transform-> convert to hourly (the market moved to 15-min products in Oct 2025), fill gaps
Load     -> save to data/prices.csv, and to PostgreSQL if DATABASE_URL is set

Usage:
    python data.py                      # default: 2023-01-01 to 2025-12-31
    python data.py 2024-01-01 2024-12-31
    DATABASE_URL=postgresql://user:pass@localhost:5432/energy python data.py
"""

import os
import sys
import time

import pandas as pd
import requests

API_URL = "https://api.energy-charts.info/price"
ZONE = "DE-LU"
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "prices.csv")
TABLE = "hourly_prices"


def _get(params, retries=5):
    """GET with exponential backoff - the free API rate-limits (HTTP 429)."""
    for attempt in range(retries):
        resp = requests.get(API_URL, params=params, timeout=120)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        time.sleep(10 * 2 ** attempt)
    resp.raise_for_status()


def extract(start, end):
    """Download raw prices one calendar year per request."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    frames = []
    for year in range(start.year, end.year + 1):
        chunk_start = max(start, pd.Timestamp(f"{year}-01-01"))
        chunk_end = min(end, pd.Timestamp(f"{year}-12-31"))
        js = _get({"bzn": ZONE, "start": chunk_start.strftime("%Y-%m-%d"),
                   "end": chunk_end.strftime("%Y-%m-%d")})
        frames.append(pd.DataFrame({"unix": js["unix_seconds"], "price": js["price"]}))
        print(f"  extracted {year}: {len(js['price'])} rows")
        time.sleep(2)  # be polite to the free API
    return pd.concat(frames, ignore_index=True)


def transform(raw):
    """Clean raw rows into one price per hour, indexed by UTC timestamp."""
    df = raw.dropna().drop_duplicates("unix")
    df["timestamp"] = pd.to_datetime(df["unix"], unit="s", utc=True)
    hourly = df.set_index("timestamp")["price"].resample("1h").mean()
    hourly = hourly.interpolate(limit=3)  # patch small gaps only
    out = hourly.dropna().to_frame("price_eur_mwh")
    out["zone"] = ZONE
    return out


def load(df):
    """Write to CSV, and to PostgreSQL when DATABASE_URL is configured."""
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    df.to_csv(CSV_PATH)
    print(f"  loaded {len(df)} rows -> {CSV_PATH}")

    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        from sqlalchemy import create_engine  # only needed for the Postgres path
        engine = create_engine(db_url)
        df.to_sql(TABLE, engine, if_exists="replace", index=True, index_label="timestamp")
        print(f"  loaded {len(df)} rows -> PostgreSQL table '{TABLE}'")


def run_etl(start="2023-01-01", end="2025-12-31"):
    print(f"ETL {ZONE} day-ahead prices {start} -> {end}")
    df = transform(extract(start, end))
    load(df)
    return df


def load_prices():
    """Read cleaned prices (from Postgres if configured, else CSV). Runs ETL if missing."""
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        from sqlalchemy import create_engine
        df = pd.read_sql(f"SELECT * FROM {TABLE} ORDER BY timestamp", create_engine(db_url),
                         index_col="timestamp", parse_dates=["timestamp"])
    elif os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH, index_col="timestamp", parse_dates=["timestamp"])
    else:
        df = run_etl()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("Europe/Berlin")
    return df


if __name__ == "__main__":
    run_etl(*sys.argv[1:3]) if len(sys.argv) >= 3 else run_etl()
