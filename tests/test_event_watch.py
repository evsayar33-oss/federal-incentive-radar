"""H083 watch-only Radar integration: source time, close-only features, fail closed."""
from __future__ import annotations
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from radar.event_watch import (
    CANDIDATE_COLS, eligible_events, evaluate_watch, features_asof,
)

NOW = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)


def _bars(*, beta=0.45, squeeze=False):
    dates = pd.bdate_range("2024-12-02", periods=390)
    rng = np.random.default_rng(33)
    sp_returns = rng.normal(0.0008, 0.009, len(dates))
    spy_close = 100 * np.cumprod(1 + sp_returns)
    asset_close = 100 * np.cumprod(1 + beta * sp_returns + 0.0015)
    def one(close, highlow=0.005):
        span = np.full(len(dates), highlow)
        if squeeze:
            span[-75:] = 0.0005
            span[:-75] = 0.07
        return pd.DataFrame({
            "Open": close * (1 - .0003),
            "High": close * (1 + span),
            "Low": close * (1 - span),
            "Close": close,
            "Volume": np.repeat(1_000_000., len(dates)),
        }, index=dates)
    return one(spy_close), one(asset_close)


def _feed(*, quality="exact_utc", ticker="ACME", known="2026-03-02T15:00:00Z"):
    return pd.DataFrame([{
        "event_id": "capex:1", "event_family": "corporate_capex",
        "category": "plant", "source": "corp_invest", "ticker": ticker,
        "event_date": "2026-03-02", "known_at": known,
        "timestamp_quality": quality, "url": "https://example.org/capex",
    }])


def test_action_date_proxy_is_not_eligible():
    assert eligible_events(_feed(quality="action_date_proxy"), NOW).empty


def test_forward_first_seen_is_not_historical_proof():
    d = eligible_events(_feed(quality="first_seen_utc"), NOW)
    assert len(d) == 1
    assert d.iloc[0]["evidence_scope"] == "FORWARD_ONLY"


def test_utc_future_or_missing_zone_excluded():
    assert eligible_events(_feed(known="2026-11-13T12:00:00Z"), NOW).empty
    assert eligible_events(_feed(known="2026-03-02T15:00:00"), NOW).empty


def test_market_features_only_use_asof_close_not_future():
    spy, stock = _bars()
    asof = spy.index[-4]
    first = features_asof(stock, spy, asof)
    mutated = stock.copy()
    mutated.loc[mutated.index > asof, ["Close", "High", "Low", "Volume"]] *= 10_000
    second = features_asof(mutated, spy, asof)
    assert first == second


def test_low_beta_watch_is_not_buy_signal():
    spy, stock = _bars()
    # Override historical event date to be before the selected bar.
    res, bad, status = evaluate_watch(
        _feed(), {"SPY": spy, "ACME": stock}, NOW, asof=spy.index[-1],
    )
    assert status["ok"] is True
    low = res.loc[res.rule.eq("monthly_low_beta")]
    assert len(low) == 1
    assert 0 <= low.iloc[0]["beta126"] <= 1
    assert low.iloc[0]["status"] == "WATCH_ONLY_UNVALIDATED"
    assert not bool(low.iloc[0]["validated_alpha"])
    assert low.iloc[0]["decision"] == "NO_TRADE_AUTHORIZATION"
    assert low.iloc[0]["midas_roundtrip_usd"] == 3.


def test_volatility_compression_watch():
    spy, stock = _bars(squeeze=True)
    res, _, _ = evaluate_watch(
        _feed(), {"SPY": spy, "ACME": stock}, NOW, asof=spy.index[-1],
    )
    assert (res.rule == "monthly_vol_squeeze").any()


def test_empty_or_missing_benchmark_fails_closed():
    res, missing, status = evaluate_watch(
        _feed(), {}, NOW,
    )
    assert res.empty
    assert set(CANDIDATE_COLS) == set(res.columns)
    assert not status["ok"]
    assert missing.iloc[0]["reason"] == "NO_BENCHMARK"


def test_latest_bar_missing_excludes_stock():
    spy, stock = _bars()
    stock = stock.iloc[:-1]
    res, audit, status = evaluate_watch(
        _feed(), {"SPY": spy, "ACME": stock}, NOW, asof=spy.index[-1],
    )
    assert res.empty
    assert "FEATURE_HISTORY_OR_LAST_PRICE_MISSING" in set(audit.reason)


def test_no_future_event_date():
    future = _feed()
    future.loc[0, "event_date"] = "2026-11-13"
    assert eligible_events(future, NOW).empty
