"""Tests for physical accounting, information timing, data quality, and SQL idempotency."""

import io
import os
import zipfile
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text

from energy_arb.backtest import prepare, simulate, statistics
from energy_arb.pipeline import (
    TZ,
    database,
    date_bounds,
    fetch_prices,
    metadata,
    parse_csv,
    prices_table,
    store,
)
from energy_arb.strategy import (
    BASELINE,
    Battery,
    Model,
    forecast_prices,
    optimize,
    settle,
)


@pytest.fixture
def raw_prices():
    index = pd.date_range("2024-01-01", periods=24 * 8, freq="h", tz=TZ).tz_convert(
        "UTC"
    )
    frames = []
    for market in ("DA", "RT"):
        value = np.where(index.tz_convert(TZ).hour < 12, 10.0, 80.0)
        frames.append(
            pd.DataFrame(
                {
                    "timestamp_utc": index,
                    "market": market,
                    "zone": "WEST",
                    "price_usd_mwh": value,
                    "source_url": "synthetic-test-only",
                }
            )
        )
    fast_index = pd.date_range(index[0], periods=8 * 288, freq="5min")
    frames.append(
        pd.DataFrame(
            {
                "timestamp_utc": fast_index,
                "market": "RT5",
                "zone": "WEST",
                "price_usd_mwh": np.where(
                    fast_index.tz_convert(TZ).hour < 12, 10.0, 80.0
                ),
                "source_url": "synthetic-test-only",
            }
        )
    )
    return pd.concat(frames, ignore_index=True)


def test_optimizer_physics_cash_and_costs():
    battery = Battery()
    prices = np.r_[np.full(12, 10.0), np.full(12, 100.0)]
    ledger = settle(optimize(prices, battery), prices, battery)
    bought, sold = ledger.charge_mwh.sum(), ledger.discharge_mwh.sum()
    assert bought == pytest.approx(2 / 0.95)
    assert sold == pytest.approx(1.9)
    assert ledger.soc_mwh.between(0, 2).all()
    assert ledger.soc_mwh.iloc[-1] == pytest.approx(0, abs=1e-6)
    assert ledger.net_profit.sum() == pytest.approx(
        100 * sold - 10 * bought - bought - sold - 10 * sold
    )


def test_negative_prices_never_cause_simultaneous_dispatch():
    battery = Battery(wear_per_mwh=0, fee_per_mwh=0)
    prices = np.full(24, -100.0)
    ledger = settle(optimize(prices, battery), prices, battery)
    assert not ((ledger.charge_mwh > 1e-7) & (ledger.discharge_mwh > 1e-7)).any()
    assert ledger.net_profit.sum() > 0  # Losses consume energy at a negative price.


def test_flat_positive_prices_do_not_trade_and_partial_days_fail():
    assert optimize(np.full(24, 30.0))[
        ["charge_mwh", "discharge_mwh"]
    ].sum().sum() == pytest.approx(0)
    with pytest.raises(ValueError):
        optimize([np.nan])
    with pytest.raises(ValueError):
        settle(pd.DataFrame({"charge_mwh": [1.0], "discharge_mwh": [1.0]}), [10])


def test_remaining_horizon_respects_existing_inventory_and_budget():
    battery = Battery()
    plan = optimize([10, 100, 20], battery, initial_soc=1.0, discharge_budget=0.95)
    change = np.cumsum(plan.charge_mwh * 0.95 - plan.discharge_mwh / 0.95)
    assert 1 + change.iloc[-1] == pytest.approx(0, abs=1e-6)
    assert plan.discharge_mwh.sum() <= 0.95 + 1e-7


def test_timezone_conventions_and_dst():
    content = (
        "Time Stamp,Name,LBMP ($/MWHr)\n"
        "11/03/2024 00:00,WEST,10\n11/03/2024 01:00,WEST,-5\n"
        "11/03/2024 01:00,WEST,20\n11/03/2024 02:00,WEST,30\n"
    ).encode()
    frame = parse_csv(content, "DA", "WEST", "synthetic")
    assert frame.timestamp_utc.diff().iloc[1:].eq(pd.Timedelta(hours=1)).all()
    assert frame.price_usd_mwh.min() == -5
    a, b = date_bounds("2024-11-03", "2024-11-03")
    assert (b - a) / pd.Timedelta(hours=1) == 25
    a, b = date_bounds("2024-03-10", "2024-03-10")
    assert (b - a) / pd.Timedelta(hours=1) == 23


def test_missing_duplicates_and_unknown_zone_are_rejected(raw_prices):
    with pytest.raises(ValueError, match="Missing hourly"):
        prepare(raw_prices.drop(index=0), "2024-01-01", "2024-01-07")
    with pytest.raises(ValueError, match="Duplicate"):
        prepare(
            pd.concat([raw_prices, raw_prices.iloc[[0]]]), "2024-01-01", "2024-01-07"
        )
    with pytest.raises(ValueError, match="absent"):
        parse_csv(
            b"Time Stamp,Name,LBMP ($/MWHr)\n01/01/2024 00:00,WEST,10\n",
            "DA",
            "OTHER",
            "test",
        )


def test_historical_forecast_excludes_yesterday_and_future(raw_prices):
    prices = prepare(raw_prices, "2024-01-01", "2024-01-08")
    model = Model("test", "bias", lookback_days=2)
    day = pd.Timestamp("2024-01-05", tz=TZ)
    delivery = prices.loc[prices.index.normalize() == day, "DA"]
    expected = forecast_prices(prices, delivery, day, model)
    changed = prices.copy()
    changed.loc[changed.index >= day - pd.DateOffset(days=1), "RT"] = -99999
    np.testing.assert_array_equal(
        forecast_prices(changed, delivery, day, model), expected
    )


@pytest.mark.parametrize(
    "model",
    [
        BASELINE,
        Model("adaptive_test", "adaptive", bias_weight=0),
        Model("learned_test", "learned", lookback_days=2),
    ],
)
def test_settlement_prices_cannot_change_dispatch_and_inputs_are_preserved(
    raw_prices, model
):
    prices = prepare(raw_prices, "2024-01-01", "2024-01-08")
    original = prices.copy(deep=True)
    hourly, daily = simulate(prices, "2024-01-05", "2024-01-05", model, Battery())
    pd.testing.assert_frame_equal(prices, original)
    changed = prices.copy()
    changed.loc[changed.index.date == pd.Timestamp("2024-01-05").date(), "RT"] *= -100
    other, _ = simulate(changed, "2024-01-05", "2024-01-05", model, Battery())
    np.testing.assert_allclose(hourly.charge_mwh, other.charge_mwh)
    np.testing.assert_allclose(hourly.discharge_mwh, other.discharge_mwh)
    assert daily.net_profit.sum() == pytest.approx(hourly.net_profit.sum())


def test_intraday_observations_are_delayed_and_future_ticks_cannot_change_first_trade(
    raw_prices,
):
    prices = prepare(raw_prices, "2024-01-01", "2024-01-08")
    assert (
        pd.DatetimeIndex(prices.spot_observation_time.dropna())
        <= prices.index[prices.spot_observation_time.notna()].tz_convert("UTC")
        - pd.Timedelta(minutes=10)
    ).all()
    model = Model("adaptive_test", "adaptive", bias_weight=0)
    expected, _ = simulate(prices, "2024-01-05", "2024-01-05", model, Battery())
    changed = raw_prices.copy()
    first = pd.Timestamp("2024-01-05", tz=TZ).tz_convert("UTC")
    changed.loc[
        changed.market.eq("RT5")
        & (changed.timestamp_utc > first - pd.Timedelta(minutes=10)),
        "price_usd_mwh",
    ] = -100
    other, _ = simulate(
        prepare(changed, "2024-01-01", "2024-01-08"),
        "2024-01-05",
        "2024-01-05",
        model,
        Battery(),
    )
    np.testing.assert_allclose(
        expected.iloc[0][["charge_mwh", "discharge_mwh"]].astype(float),
        other.iloc[0][["charge_mwh", "discharge_mwh"]].astype(float),
    )


def test_sharpe_and_drawdown_include_idle_days_and_starting_zero():
    daily = pd.DataFrame(
        {
            "net_profit": [-10.0, 0.0, 30.0],
            "charge_mwh": [1, 0, 1],
            "discharge_mwh": [0.9, 0, 0.9],
            "fees": [1, 0, 1],
            "wear": [1, 0, 1],
        }
    )
    result = statistics(daily)
    assert result["daily_pnl_sharpe"] == pytest.approx(
        np.sqrt(365) * daily.net_profit.mean() / daily.net_profit.std()
    )
    assert result["max_daily_drawdown_usd"] == 10
    assert result["active_days"] == 2


def test_missing_intraday_tick_falls_back_to_da_without_dropping_the_hour(raw_prices):
    prices = prepare(raw_prices, "2024-01-01", "2024-01-08")
    decision = pd.Timestamp("2024-01-05 14:00", tz=TZ)
    prices.loc[decision, "lagged_spot"] = np.nan
    model = Model("adaptive_test", "adaptive", bias_weight=0)
    hourly, _ = simulate(prices, "2024-01-05", "2024-01-05", model, Battery())
    assert len(hourly) == 24
    missing = hourly.loc[~hourly.spot_available]
    assert len(missing) == 1
    assert missing.forecast_price.iloc[0] == missing.day_ahead_price.iloc[0]


def check_upsert(engine, raw):
    store(raw, engine)
    changed = raw.copy()
    changed.loc[0, "price_usd_mwh"] = -42.0
    store(changed, engine)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(prices_table)) == len(
            raw
        )
        query = select(prices_table.c.price_usd_mwh).where(
            prices_table.c.timestamp_utc == raw.timestamp_utc.iloc[0].to_pydatetime(),
            prices_table.c.market == "DA",
        )
        assert connection.scalar(query) == -42.0
    other_zone = raw.iloc[[0]].copy()
    other_zone["zone"] = "OTHER"
    store(other_zone, engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(prices_table))
            == len(raw) + 1
        )


def test_sqlite_upsert(raw_prices):
    engine = database("sqlite:///:memory:")
    try:
        check_upsert(engine, raw_prices)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="Set a PostgreSQL test database URL"
)
def test_postgresql_upsert(raw_prices):
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    schema = "test_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        isolated = engine.execution_options(schema_translate_map={None: schema})
        metadata.create_all(isolated)
        check_upsert(isolated, raw_prices)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_fetch_uses_cache_and_explicit_refresh(tmp_path, monkeypatch):
    from energy_arb import pipeline

    calls = []
    content = (
        "Time Stamp,Name,LBMP ($/MWHr)\n"
        + "".join(f"01/01/2024 {hour:02}:00,WEST,10\n" for hour in range(24))
    ).encode()

    class Response:
        def __init__(self, body):
            self.content = body

        def raise_for_status(self):
            pass

    def get(self, url, **kwargs):
        calls.append(url)
        dataset = url.split("/")[-2]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"20240101{dataset}_zone.csv", content)
        return Response(buffer.getvalue())

    monkeypatch.setattr(pipeline.requests.Session, "get", get)
    engine = database("sqlite:///:memory:")
    try:
        for refresh in (False, False, True):
            result = fetch_prices(
                "2024-01-01",
                "2024-01-01",
                zone="WEST",
                cache_dir=tmp_path,
                engine=engine,
                include_intraday=False,
                refresh=refresh,
            )
            assert len(result) == 48
        assert len(calls) == 4
    finally:
        engine.dispose()


def test_incomplete_legacy_archive_recovers_official_oasis_prices(
    tmp_path, monkeypatch
):
    from energy_arb import pipeline

    def archive(content, member):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zip_file:
            zip_file.writestr(member, content)
        return buffer.getvalue()

    header = "Time Stamp,Name,LBMP ($/MWHr)\n"
    full = (
        header + "".join(f"01/01/2024 {h:02}:00,WEST,10\n" for h in range(24))
    ).encode()
    partial = (header + "01/01/2024 00:00,WEST,10\n").encode()
    recoveries = []

    def legacy(url, path, refresh):
        dataset = url.split("/")[-2]
        content = partial if dataset == "rtlbmp" else full
        return archive(content, f"20240101{dataset}_zone.csv"), {"source_url": url}

    def recover(market, month, cache, refresh):
        recoveries.append(market)
        return (
            archive(full, "rtlbmp_zone-20240101.csv"),
            "oasis.zip",
            {"source_url": "official-oasis"},
        )

    monkeypatch.setattr(pipeline, "_download", legacy)
    monkeypatch.setattr(pipeline, "oasis_archive", recover)
    engine = database("sqlite:///:memory:")
    try:
        result = fetch_prices(
            "2024-01-01",
            "2024-01-01",
            zone="WEST",
            cache_dir=tmp_path,
            engine=engine,
            include_intraday=False,
        )
        assert len(result) == 48
        assert recoveries == ["RT"]
        assert (
            result.loc[result.market.eq("RT"), "source_url"].eq("official-oasis").all()
        )
    finally:
        engine.dispose()


def test_latest_filters_future_rt_and_upserts_current_prices(tmp_path, monkeypatch):
    from energy_arb import pipeline

    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    now = pd.Timestamp.now(tz="UTC")
    previous = (now - pd.Timedelta(minutes=20)).tz_convert(TZ)
    future = (now + pd.Timedelta(hours=2)).tz_convert(TZ)
    tomorrow = pd.Timestamp.now(tz=TZ).normalize() + pd.DateOffset(days=1)
    header = "Time Stamp,Time Zone,Name,LBMP ($/MWHr)\n"

    def row(stamp):
        return f"{stamp:%m/%d/%Y %H:%M:%S},{stamp:%Z},WEST,20\n"

    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    def get(self, url, **kwargs):
        body = header + (
            row(previous) + row(future) if "/realtime/" in url else row(tomorrow)
        )
        return Response(body.encode())

    monkeypatch.setattr(pipeline.requests.Session, "get", get)
    result = pipeline.latest_prices("WEST")
    assert len(result.loc[result.market.eq("RT5")]) == 1
    assert (result.loc[result.market.eq("RT5"), "timestamp_utc"] <= now).all()
    assert (tmp_path / "data/live/latest_WEST.csv").exists()
    engine = pipeline.database()
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(prices_table)) == 2
            )
    finally:
        engine.dispose()
