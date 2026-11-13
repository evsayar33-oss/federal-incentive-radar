"""V2 integration regressions; all tests offline and deterministic."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from radar import decision_audit as A
from radar import economic_intelligence as I
from radar import price_resilience as P
from radar import relationship_pipeline as G
from radar import research_assessment as R
from radar import research_risk
from radar.event_watch import evaluate_watch
from test_event_watch import _bars, _feed

NOW = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)


def _prices(length=255, volume=1_000_000):
    idx = pd.bdate_range("2025-01-01", periods=length)
    close = pd.Series(np.linspace(100, 150, length), index=idx)
    return pd.DataFrame({
        "Open": close * .999, "High": close * 1.002,
        "Low": close * .997, "Close": close,
        "Volume": volume,
    }, index=idx)


def test_provider_is_bounded_and_exposes_secondary_provenance():
    calls = []
    def yahoo(tickers, period):
        assert tickers == ["SPY", "ACME", "MISSING"]
        return {"ACME": _prices()}, ["429"]
    def fallback(ticker):
        calls.append(ticker)
        return _prices() if ticker == "SPY" else pd.DataFrame()
    bars, report = P.download_bars(
        ["SPY", "ACME", "MISSING"], yahoo_loader=yahoo,
        fallback=fallback, max_fallback=1,
    )
    assert calls == ["SPY"]
    assert set(bars) == {"SPY", "ACME"}
    assert report["fallback_recovered"] == 1
    assert report["missing"] == 1
    assert report["providers"]["SPY"].startswith("STOOQ")
    assert report["providers"]["ACME"] == "YAHOO_ADJUSTED"


def test_price_normalization_avoids_duplicate_and_inconsistent_bars():
    prices = _prices()
    duplicate = pd.concat([prices, prices.tail(1)])
    duplicate.iloc[-1, duplicate.columns.get_loc("High")] = -1
    clean = P.normalize(duplicate)
    assert clean.index.is_unique
    assert len(clean) == len(prices) - 1
    assert clean.index[-1] < prices.index[-1]


def test_bad_data_and_illiquidity_are_not_same_outage():
    spy, stock = _bars()
    thin = stock.copy()
    thin["Volume"] = 100
    feed = pd.concat([_feed(), _feed(ticker="THIN")], ignore_index=True)
    result, excluded, status = evaluate_watch(
        feed, {"SPY": spy, "ACME": stock, "THIN": thin},
        NOW, asof=spy.index[-1],
    )
    assert status["data_coverage_ok"] is True
    assert status["priced_tickers"] == 2
    assert status["liquid_tickers"] == 1
    assert status["exclusion_reasons"]["NOT_LIQUID_5M"] == 1
    assert status["ok"] is True
    assert "THIN" not in set(result.ticker)


def test_supply_venture_and_contract_parties_are_not_trading_signals():
    cases = [
        ("Acme to supply Beta with optical modules", "SUPPLY_ANNOUNCEMENT", "SUPPLIER"),
        ("Acme forms joint venture with Beta for AI infrastructure", "JOINT_VENTURE_ANNOUNCED", "PARTNER"),
        ("Acme signs supply agreement with Beta for GPUs", "AGREEMENT_ANNOUNCED", "COUNTERPARTY"),
    ]
    for headline, expected, role in cases:
        r = I.extract_relationship(headline, "https://example.org/source", NOW.isoformat())
        assert r is not None, headline
        assert r.relation == expected and r.source_role == role
        assert r.decision == "RESEARCH_ONLY_NO_TRADE"


def test_no_primary_source_claim_and_prospective_observation():
    rows = pd.DataFrame([{
        "event_id": "corp:1", "title": "NVIDIA invests $2 billion in Lumentum",
        "url": "https://news.google.com/rss/item",
        "known_at": "2026-11-12T20:00:00Z",
    }])
    universe = pd.DataFrame([
        {"name": "NVIDIA Corporation", "ticker": "NVDA"},
        {"name": "Lumentum Inc", "ticker": "LITE"},
    ])
    out = G.build_relationships(rows, universe, NOW)
    assert len(out) == 1
    r = out.iloc[0]
    assert r.source_ticker == "NVDA" and r.target_ticker == "LITE"
    assert r.source_role == "INVESTOR" and r.target_role == "DIRECT_RECIPIENT"
    assert r.observed_at_utc.startswith("2026-11-13T22:00:00")
    assert r.verification_status == "PRIMARY_SOURCE_REQUIRED"
    assert not r.validated_alpha


def test_ambiguous_legal_names_never_get_ticker():
    rows = pd.DataFrame([{
        "event_id": "corp:ambiguous",
        "title": "Acme invests $200 million in Beta",
        "url": "https://example.org/item",
        "known_at": "2026-11-12T20:00:00Z",
    }])
    universe = pd.DataFrame([
        {"name": "Acme Inc", "ticker": "AAA"},
        {"name": "Acme Corp", "ticker": "BBB"},
        {"name": "Beta Ltd", "ticker": "BETA"},
    ])
    out = G.build_relationships(rows, universe, NOW)
    assert len(out) == 1
    assert pd.isna(out.iloc[0].source_ticker)
    assert out.iloc[0].source_match_method == "AMBIGUOUS_ENTITY"


def test_future_source_publication_refused():
    rows = pd.DataFrame([{
        "event_id": "corp:future",
        "title": "NVIDIA invests $2 billion in Lumentum",
        "url": "https://example.org/item",
        "known_at": "2026-11-14T00:00:00Z",
    }])
    assert G.build_relationships(rows, pd.DataFrame(), NOW).empty


def test_audit_hash_chain_idempotent_and_tamper_detection(tmp_path, monkeypatch):
    from radar import store
    monkeypatch.setenv("RADAR_STORE", str(tmp_path))
    first = A.record("WATCH", "watch:1", "BLOCK", {"n": 2}, observed_at_utc=NOW)
    assert first["new"] is True
    assert A.record("WATCH", "watch:1", "BLOCK", {"n": 2},
                    observed_at_utc=NOW)["new"] is False
    with pytest.raises(ValueError, match="REPLAY_CONFLICT"):
        A.record("WATCH", "watch:1", "PASS", {"n": 3}, observed_at_utc=NOW)
    A.record("WATCH", "watch:2", "BLOCK", {"n": 4}, observed_at_utc=NOW)
    saved = store.read("decision_audit")
    assert len(saved) == 2
    assert A.verify(saved)
    corrupted = saved.copy()
    corrupted.loc[0, "decision"] = "PASS"
    assert not A.verify(corrupted)


def test_h083_skip_audit_does_not_count_as_forward_month(tmp_path, monkeypatch):
    from radar import store
    monkeypatch.setenv("RADAR_STORE", str(tmp_path))
    A.record_forward_attempt(NOW, "SKIPPED", "OUTSIDE_17_ET_AFTERCLOSE_SLOT")
    attempt = store.read("h083_run_audit").iloc[0]
    assert str(attempt.counts_as_forward_month).lower() in {"false", "0"}
    assert attempt.state == "SKIPPED"


def test_scorecard_only_uses_closed_forward_months_and_never_brokers():
    months = pd.DataFrame([
        {"rule": "monthly_low_beta", "entry_month": f"2026-{i:02d}",
         "complete": True, "net_return": .025, "excess_vs_spy": -.003,
         "net_return_stress": .005, "n_positions": 4}
        for i in range(1, 7)
    ])
    gates = pd.DataFrame([
        {"rule": "monthly_low_beta", "status": "COLLECTING_FORWARD",
         "validated_for_info_plans": False},
        {"rule": "monthly_vol_squeeze", "status": "COLLECTING_FORWARD",
         "validated_for_info_plans": False},
    ])
    report = R.build_scorecard(
        months, gates, {"ok": True, "data_coverage_ok": True},
        {"regime": "RISK_OFF"}, NOW)
    assert len(report) == 2
    low = report.iloc[0]
    assert low.net_mean_monthly == pytest.approx(.025)
    assert low.mean_excess_vs_spy == pytest.approx(-.003)
    assert low.research_decision == "RESEARCH_HOLD"
    assert not report.broker_authorized.astype(bool).any()


def test_regime_uses_only_observed_close():
    spy = _prices()
    asof = spy.index[-4]
    x = R.regime_snapshot(spy, asof)
    modified = spy.copy()
    modified.loc[modified.index > asof, "Close"] *= 10
    assert R.regime_snapshot(modified, asof) == x


def test_research_risk_positive_screen_never_authorizes_execution():
    out = research_risk.risk_gate(
        entry=100, stop=95, target=110, quantity=5,
        equity=10000, cash=10000, validated_alpha=True,
    )
    assert out["decision"] == "PASS_RESEARCH_SCREEN_ONLY"
    assert out["broker_authorized"] is False


def test_shared_dashboard_compiles():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ast.parse((root / "app/h083_page.py").read_text(encoding="utf-8"))


def test_sec_source_review_is_only_a_candidate():
    from radar import source_review
    rel = pd.DataFrame([{
        "event_id": "corp:1", "published_at_utc": "2026-11-12T20:00:00Z",
        "source_ticker": "NVDA", "target_ticker": "LITE",
        "source_company": "NVIDIA", "target_company": "Lumentum",
        "relation": "STRATEGIC_INVESTMENT",
    }])
    filings = pd.DataFrame([
        {"event_id": "sec:1", "source": "sec_edgar", "date": "2026-11-13",
         "tickers": "LITE", "url": "https://www.sec.gov/Archives/edgar/data/1/abc/8k.htm"},
        {"event_id": "other:1", "source": "news", "date": "2026-11-13",
         "tickers": "NVDA", "url": "https://example.org/fake"},
    ])
    suggestions = source_review.suggest_filings(rel, filings)
    assert len(suggestions) == 1
    assert suggestions.iloc[0].matched_ticker == "LITE"
    assert suggestions.iloc[0].review_state == "POTENTIAL_SEC_FILING_NOT_VERIFIED"
    assert not bool(suggestions.iloc[0].is_transaction_verified)


def test_research_alert_deduplicates_and_never_labels_news_a_trade(tmp_path, monkeypatch):
    from radar import research_notifications as N
    from radar import telegram
    monkeypatch.setenv("RADAR_STORE", str(tmp_path))
    sent = []
    monkeypatch.setattr(telegram, "enabled", lambda: True)
    monkeypatch.setattr(telegram, "send", lambda msg: sent.append(msg) or True)
    assert N.warn_data_quality("H083", "COVERAGE", {"priced": 0})
    assert not N.warn_data_quality("H083", "COVERAGE", {"priced": 0})
    assert len(sent) == 1
    rel = pd.DataFrame([{
        "event_id": "corp:1", "source_company": "NVIDIA",
        "target_company": "Lumentum", "source_role": "INVESTOR",
        "target_role": "DIRECT_RECIPIENT", "source_ticker": "NVDA",
        "target_ticker": "LITE", "relation": "STRATEGIC_INVESTMENT",
        "observed_at_utc": NOW.isoformat(),
        "source_url": "https://example.org/article",
    }])
    assert N.notify_relationships(rel) == 1
    assert N.notify_relationships(rel) == 0
    assert "DOĞRULANMAMIŞ" in sent[-1]
    assert "AL/SAT SİNYALİ DEĞİL" in sent[-1]
