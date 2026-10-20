"""Fetch NYISO hourly prices for any dates, cache source files, and upsert them into SQL."""

import argparse
import hashlib
import io
import json
import logging
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import Column, DateTime, Float, MetaData, String, Table, create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
TZ = "America/New_York"
DEFAULT_ZONE = "NORTH"
BASE_URL = "https://mis.nyiso.com/public/csv"
DATASETS = {"DA": "damlbmp", "RT": "rtlbmp", "RT5": "realtime"}
POSTINGS = {"DA": "DAM_LBMP_ZONE", "RT": "RT_TW_LBMP_ZONE", "RT5": "RT_LBMP_ZONE"}
metadata = MetaData()
prices_table = Table(
    "prices",
    metadata,
    Column("timestamp_utc", DateTime(timezone=True), primary_key=True),
    Column("market", String(8), primary_key=True),
    Column("zone", String(16), primary_key=True),
    Column("price_usd_mwh", Float, nullable=False),
    Column("source_url", String(300), nullable=False),
)


def date_bounds(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Inclusive local calendar dates -> [start, end) UTC delivery timestamps."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("start must be on or before end")
    return (
        pd.Timestamp(first, tz=TZ).tz_convert("UTC"),
        pd.Timestamp(last + timedelta(days=1), tz=TZ).tz_convert("UTC"),
    )


def parse_csv(content: bytes, market: str, zone: str, source_url: str) -> pd.DataFrame:
    raw = pd.read_csv(io.BytesIO(content))
    required = {"Time Stamp", "Name", "LBMP ($/MWHr)"}
    if not required.issubset(raw.columns):
        raise ValueError(f"Unexpected NYISO CSV: {source_url}")
    raw = raw.loc[raw["Name"].eq(zone)].copy()
    if raw.empty:
        raise ValueError(f"Zone {zone!r} is absent from {source_url}")
    local = pd.to_datetime(raw["Time Stamp"], format="mixed", errors="raise")
    # Each zone is already chronological. The repeated autumn hour occurs twice, in order.
    ambiguous = raw["Time Zone"].eq("EDT") if "Time Zone" in raw else "infer"
    times = local.dt.tz_localize(
        TZ, ambiguous=ambiguous, nonexistent="raise"
    ).dt.tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "timestamp_utc": times,
            "market": market,
            "zone": zone,
            "price_usd_mwh": pd.to_numeric(raw["LBMP ($/MWHr)"], errors="raise"),
            "source_url": source_url,
        }
    ).reset_index(drop=True)
    if frame["timestamp_utc"].duplicated().any():
        raise ValueError(f"Duplicate delivery timestamps: {source_url}")
    if not np.isfinite(frame["price_usd_mwh"]).all():
        raise ValueError(f"Non-finite prices: {source_url}")
    return frame


def database(database_url: str | None = None):
    (ROOT / "data/database").mkdir(parents=True, exist_ok=True)
    url = database_url or os.environ.get(
        "DATABASE_URL", f"sqlite:///{ROOT / 'data/database/prices.db'}"
    )
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name not in ("sqlite", "postgresql"):
        raise ValueError("Use a SQLite or PostgreSQL database URL")
    metadata.create_all(engine)
    return engine


def store(frame: pd.DataFrame, engine) -> None:
    records = frame.to_dict("records")
    if not records:
        return
    for row in records:
        row["timestamp_utc"] = row["timestamp_utc"].to_pydatetime()
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    statement = insert(prices_table)
    statement = statement.on_conflict_do_update(
        index_elements=["timestamp_utc", "market", "zone"],
        set_={
            "price_usd_mwh": statement.excluded.price_usd_mwh,
            "source_url": statement.excluded.source_url,
        },
    )
    with engine.begin() as connection:
        for offset in range(0, len(records), 1000):
            connection.execute(statement, records[offset : offset + 1000])


def _download(url: str, path: Path, refresh: bool) -> tuple[bytes, dict]:
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        logging.info("Fetching %s", path.name)
        retry = Retry(
            total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]
        )
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=retry))
            response = session.get(url, timeout=(10, 45))
            response.raise_for_status()
        content = response.content
        # Detect HTML error pages before replacing a valid cache.
        if path.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if archive.testzip() is not None:
                    raise ValueError(f"Corrupt archive: {url}")
        elif b"Time Stamp" not in content[:300]:
            raise ValueError(f"Expected CSV data: {url}")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    else:
        content = path.read_bytes()
    return content, {
        "source_url": url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "downloaded_at_utc": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(),
    }


def oasis_archive(market, month, cache_dir, refresh):
    """The public OASIS portal retains files missing from some legacy monthly archives."""
    path = cache_dir / f"oasis_{DATASETS[market]}_{month.strftime('%Y%m')}.zip"
    api = "https://oasis-api.prod.nysdi.com/zip"
    params = {
        "postingName": POSTINGS[market],
        "format": "CSV",
        "zipStart": str(month.start_time.date()),
        "zipEnd": str(min(month.end_time.date(), pd.Timestamp.now(tz=TZ).date())),
    }
    public_url = requests.Request("GET", api, params=params).prepare().url
    if refresh or not path.exists():
        response = requests.get(public_url, timeout=(10, 60))
        response.raise_for_status()
        content, source = _download(response.json()["fileUrl"], path, True)
    else:
        content, source = _download(public_url, path, False)
    # Record the public lookup endpoint; temporary signed download URLs expire.
    source["source_url"] = public_url
    return content, path.name, source


def fetch_prices(
    start: str,
    end: str,
    zone: str = DEFAULT_ZONE,
    refresh: bool = False,
    cache_dir: Path | None = None,
    engine=None,
    include_intraday: bool = True,
) -> pd.DataFrame:
    """Fetch DA and hourly RT data. Current-month archives are always refreshed."""
    first, stop = date_bounds(start, end)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
    if date.fromisoformat(end) > today:
        raise ValueError(
            "Historical fetch cannot include future dates; use `python -m energy_arb.pipeline latest`"
        )
    cache_dir = Path(cache_dir or ROOT / "data/raw")
    cache_dir.mkdir(parents=True, exist_ok=True)
    months = pd.period_range(start[:7], end[:7], freq="M")
    markets = ("DA", "RT", "RT5") if include_intraday else ("DA", "RT")
    jobs = [(market, month) for month in months for market in markets]

    def fetch_month(job):
        market, month = job
        dataset = DATASETS[market]
        year_cache = cache_dir / str(month.year)
        name = f"{month.strftime('%Y%m')}01{dataset}_zone_csv.zip"
        url = f"{BASE_URL}/{dataset}/{name}"
        content, source = _download(
            url, year_cache / name, refresh or str(month) == today.strftime("%Y-%m")
        )
        frames = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in sorted(archive.namelist()):
                if not member.endswith(f"{dataset}_zone.csv"):
                    continue
                delivery_date = datetime.strptime(
                    Path(member).name[:8], "%Y%m%d"
                ).date()
                if (
                    date.fromisoformat(start)
                    <= delivery_date
                    <= date.fromisoformat(end)
                ):
                    frames.append(parse_csv(archive.read(member), market, zone, url))
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        a, b = date_bounds(
            str(max(date.fromisoformat(start), month.start_time.date())),
            str(min(date.fromisoformat(end), month.end_time.date())),
        )
        expected = pd.date_range(a, b, freq="h", inclusive="left")
        if market == "RT":
            expected = expected[
                expected + pd.Timedelta(hours=1, minutes=10)
                <= pd.Timestamp.now(tz="UTC")
            ]
        incomplete = result.empty or (
            market != "RT5" and not expected.isin(result.timestamp_utc).all()
        )
        if incomplete:
            logging.info("Recovering incomplete %s %s from NYISO OASIS", market, month)
            content, name, source = oasis_archive(market, month, year_cache, refresh)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                frames = [
                    parse_csv(archive.read(member), market, zone, source["source_url"])
                    for member in sorted(archive.namelist())
                    if member.endswith(".csv")
                ]
            result = pd.concat(frames, ignore_index=True)
            result = result.loc[
                (result.timestamp_utc >= a) & (result.timestamp_utc < b)
            ]
            if market != "RT5" and not expected.isin(result.timestamp_utc).all():
                raise ValueError(
                    f"Official prices remain incomplete for {market} {month}"
                )
        return result, f"{month.year}/{name}", source

    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = list(pool.map(fetch_month, jobs))
    frame = pd.concat([item[0] for item in downloaded], ignore_index=True)
    frame = frame.loc[(frame.timestamp_utc >= first) & (frame.timestamp_utc < stop)]
    as_of = pd.Timestamp.now(tz="UTC")
    # Delivery-day DA prices can be known in advance; realized intervals must have ended.
    ended = frame.timestamp_utc + pd.to_timedelta(
        frame.market.eq("RT").astype(int), unit="h"
    )
    frame = frame.loc[frame.market.eq("DA") | (ended <= as_of)]
    frame = frame.sort_values(["timestamp_utc", "market"]).reset_index(drop=True)
    if frame.duplicated(["timestamp_utc", "market", "zone"]).any():
        raise ValueError("Duplicate observations across source files")
    manifest_path = cache_dir / "sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.update({name: source for _, name, source in downloaded})
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    own_engine = engine is None
    engine = engine if engine is not None else database()
    try:
        store(frame, engine)
    finally:
        if own_engine:
            engine.dispose()
    return frame


def latest_prices(zone: str = DEFAULT_ZONE) -> pd.DataFrame:
    """Fetch today's latest published five-minute observations and tomorrow's DA curve."""
    today = pd.Timestamp.now(tz=TZ).date()
    cache = ROOT / "data/raw"
    cache.mkdir(parents=True, exist_ok=True)
    frames, sources = [], {}
    for market, delivery in (("RT5", today), ("DA", today + timedelta(days=1))):
        dataset = DATASETS[market]
        name = f"{delivery:%Y%m%d}{dataset}_zone.csv"
        url = f"{BASE_URL}/{dataset}/{name}"
        try:
            content, source = _download(
                url, cache / str(delivery.year) / name, refresh=True
            )
        except requests.HTTPError as error:
            if error.response.status_code == 404 and market == "DA":
                logging.info("Tomorrow's day-ahead prices are not published yet")
                continue
            raise
        parsed = parse_csv(content, market, zone, url)
        if market == "RT5":
            parsed = parsed.loc[parsed.timestamp_utc <= pd.Timestamp.now(tz="UTC")]
            if parsed.empty:
                raise ValueError(
                    "No completed real-time intervals have been published yet"
                )
        frames.append(parsed)
        sources[f"{delivery.year}/{name}"] = source
    frame = pd.concat(frames, ignore_index=True)
    engine = database()
    try:
        store(frame, engine)
    finally:
        engine.dispose()
    manifest_path = cache / "sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.update(sources)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    frame["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
    live = ROOT / "data/live"
    live.mkdir(parents=True, exist_ok=True)
    frame.to_csv(live / f"latest_{zone}.csv", index=False)
    return frame


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "latest"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "latest":
        data = latest_prices(args.zone)
        print(data.groupby("market").tail(3).to_string(index=False))
        print(
            f"Saved data/live/latest_{args.zone}.csv; RT5 timestamps are interval ends, DA timestamps are starts."
        )
    else:
        if not args.start or not args.end:
            parser.error("fetch requires --start and --end")
        data = fetch_prices(args.start, args.end, args.zone, args.refresh)
        print(f"Fetched and stored {len(data):,} hourly and five-minute prices.")
