"""Deterministic tests for predeclared alpha discovery; no network or H083 writes."""
from datetime import timezone

import numpy as np
import pandas as pd

from radar import alpha_discovery as A


def _fixture():
    dates = pd.bdate_range("2023-01-02", "2026-09-30")
    rows = []
    for symbol, growth in (("SPY", 0.0002), ("AAA", 0.0006),
                           ("BBB", -0.0002), ("XLK", 0.0004)):
        close = 100.0 * np.exp(np.arange(len(dates)) * growth)
        rows.extend({"date": day, "ticker": symbol, "close": float(value)}
                    for day, value in zip(dates, close))
    prices = pd.DataFrame(rows)
    feed = pd.DataFrame([
        {"event_id": "fed:1", "source": "usaspending",
         "event_family": "federal_award", "ticker": "AAA",
         "known_at": "2025-04-07T12:00:00Z", "timestamp_quality": "first_seen_utc",
         "sector_etf": "XLK", "event_date": "2025-04-07"},
        {"event_id": "fed:2", "source": "usaspending",
         "event_family": "federal_award", "ticker": "BBB",
         "known_at": "2025-04-08T12:00:00Z", "timestamp_quality": "date_only",
         "sector_etf": "XLK", "event_date": "2025-04-08"},
        {"event_id": "corp:backfill", "source": "corp_invest",
         "event_family": "corporate_capex", "ticker": "AAA",
         "known_at": "2024-01-01T12:00:00Z", "timestamp_quality": "exact_utc",
         "sector_etf": "XLK", "event_date": "2024-01-01"},
    ])
    return prices, feed


def test_true_first_seen_and_no_retroactive_rss():
    prices, feed = _fixture()
    summary, trades, diagnostic = A.execute(prices, feed)
    assert len(summary) == 5
    assert diagnostic["event_snapshot"]["private_rss_historical_replay_disabled"]
    assert diagnostic["event_snapshot"]["rejected"]["private_rss_proxy"] == 1
    assert diagnostic["event_snapshot"]["rejected"]["not_causal_quality"] == 1
    assert set(trades.event_id) == {"fed:1"}
    assert not summary.validated_alpha.any()
    assert not summary.broker_authorized.any()
    assert set(summary.status) == {"INSUFFICIENT_EVIDENCE"}


def test_entry_is_strictly_after_observation_and_future_price_change_does_not_change_screen():
    prices, feed = _fixture()
    p, _ = A.prepare_prices(prices)
    events, _ = A.prepare_events(feed, pd.DataFrame(), p["SPY"].index.max())
    event = events[0]
    result = A._trade_for(event, "FEDERAL_PRESTRENGTH_20D", p)
    assert result
    assert pd.Timestamp(result["entry_date"]) > event["day"]
    changed = {symbol: series.copy() for symbol, series in p.items()}
    signal_day = event["day"]
    changed["AAA"].loc[changed["AAA"].index > signal_day] *= 2
    # A positive prior relative-strength screen uses data no later than signal day.
    changed_trade = A._trade_for(event, "FEDERAL_PRESTRENGTH_20D", changed)
    assert changed_trade is not None


def test_economic_beneficiary_requires_prospective_first_seen():
    prices, feed = _fixture()
    relationships = pd.DataFrame([
        {"event_id": "rel:1", "observed_at_utc": "2025-05-10T12:00:00Z",
         "target_ticker": "AAA", "relation": "STRATEGIC_INVESTMENT",
         "source_url": "https://example.org/article"},
        {"event_id": "rel:2", "observed_at_utc": "2025-05-10T12:00:00",
         "target_ticker": "BBB", "relation": "STRATEGIC_INVESTMENT",
         "source_url": "https://example.org/article"},
    ])
    summary, trades, diag = A.execute(prices, feed, relationships)
    direct = trades.loc[trades.hypothesis.eq("DIRECT_PRIVATE_RECIPIENT_20D")]
    assert len(direct) == 1
    assert direct.iloc[0]["ticker"] == "AAA"
    assert diag["event_snapshot"]["prospective_direct_private_rows"] == 1
    assert not summary.validated_alpha.any()


def test_20_close_horizon_and_costs():
    prices, feed = _fixture()
    frames, _ = A.prepare_prices(prices)
    event = A.prepare_events(feed, pd.DataFrame(), frames["SPY"].index.max())[0][0]
    trade = A._trade_for(event, "FEDERAL_DIRECT_20D", frames)
    assert trade["net_return"] == trade["gross_return"] - A.ASSET_COST
    assert trade["net_excess_vs_spy"] == trade["net_return"] - trade["spy_net_return"]
    dates = frames["SPY"].index
    assert dates.get_loc(pd.Timestamp(trade["exit_date"])) - dates.get_loc(pd.Timestamp(trade["entry_date"])) == A.HORIZON


def test_bh_correction_preserves_family_and_sparsity():
    assert A._bh([.01, .05, 1., 1., 1.])[0] == .05
    diag = A.summarize(pd.DataFrame(columns=A.TRADE_COLUMNS))
    assert len(diag) == len(A.PREDECLARED)
    assert (diag.bh_q == 1).all()
    assert not diag.validated_alpha.any()
