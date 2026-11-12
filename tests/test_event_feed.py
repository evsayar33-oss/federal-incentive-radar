from __future__ import annotations

import pandas as pd

from radar.event_feed import build_feed
from radar.sources.corp_invest import parse_rss
from radar.entity import Matcher
from radar import schema


def test_corporate_investment_preserves_exact_rss_timestamp():
    xml = (
        "<rss><channel><item><title>Intel announces $20 billion semiconductor fab - Reuters</title>"
        "<link>https://example.com/story</link>"
        "<pubDate>Fri, 23 Oct 2026 14:07:31 GMT</pubDate>"
        "</item></channel></rss>"
    )
    matcher = Matcher(pd.DataFrame({"ticker": ["INTC"], "name": ["Intel Corp"]}))
    rows = parse_rss(xml, "semiconductor fab", matcher, 1_000_000)
    assert len(rows) == 1
    assert rows[0]["known_at"] == "2026-10-23T14:07:31Z"
    assert rows[0]["date"] == "2026-10-23"


def test_investment_storage_does_not_replace_timestamp_with_date():
    from radar import investments
    got = []
    old = investments.store.upsert
    investments.store.upsert = lambda name, frame, key, keep="last": got.append(frame.copy()) or frame
    try:
        investments._store_events(pd.DataFrame([{
            "event_id": "x", "date": "2026-10-23",
            "known_at": "2026-10-23T14:07:31Z",
        }]))
    finally:
        investments.store.upsert = old
    assert got[0]["known_at"].iloc[0] == "2026-10-23T14:07:31Z"


def test_event_feed_keeps_timestamp_and_separates_event_families():
    events = pd.DataFrame([
        {"event_id": "award-1", "source": "usaspending", "relevant": True, "tickers": "INTC",
         "date": "2026-10-23", "known_at": "2026-10-23", "program": "semiconductors",
         "title": "Federal award", "amount_usd": 500_000_000},
        {"event_id": "policy-1", "source": "federal_register", "relevant": True, "tickers": "CEG",
         "date": "2026-10-24", "known_at": "2026-10-24", "program": "energy",
         "title": "Final rule"},
        {"event_id": "ignore-1", "source": "usaspending", "relevant": False, "tickers": "AAPL",
         "date": "2026-10-24", "program": "other"},
    ])
    investments = pd.DataFrame([{
        "event_id": "capex-1", "source": "corp_invest", "tickers": "NVDA",
        "date": "2026-10-25", "known_at": "2026-10-25T12:30:00Z",
        "extra": '{"category":"datacenter_ai"}', "title": "Capex announcement",
    }])
    universe = pd.DataFrame({"ticker": ["INTC", "CEG", "NVDA"], "sector": [
        "Information Technology", "Utilities", "Information Technology"
    ]})
    feed = build_feed(events, investments, universe, {
        "Information Technology": "XLK", "Utilities": "XLU"
    })
    assert set(feed["event_family"]) == {"federal_award", "policy_regulatory", "corporate_capex"}
    award = feed.loc[feed["event_id"] == "award-1"].iloc[0]
    assert award["timestamp_quality"] == "action_date_proxy"
    assert feed.loc[feed["event_id"] == "policy-1", "timestamp_quality"].iloc[0] == "date_only"
    capex = feed.loc[feed["event_id"] == "capex-1"].iloc[0]
    assert capex["known_at"] == "2026-10-25T12:30:00Z"
    assert capex["timestamp_quality"] == "exact_utc"
    assert capex["sector_etf"] == "XLK"
    assert "ignore-1" not in set(feed["event_id"])
