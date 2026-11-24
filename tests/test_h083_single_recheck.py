"""H083 single-symbol recovery: fully offline, PIT-calendar, fail-closed tests."""
from __future__ import annotations

import pandas as pd

from radar import price_resilience as P
from test_h083_partial_price_recovery import _bars


def test_single_yahoo_recheck_recovers_missing_asof_volume_without_stooq():
    spy = _bars()
    bad = _bars(last_volume=0)
    calls = []
    def single(ticker, period):
        calls.append(("yahoo", ticker, period))
        return _bars()
    def secondary(ticker):
        calls.append(("stooq", ticker))
        return pd.DataFrame()
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": bad}, []),
        single_loader=single, fallback=secondary,
    )
    assert calls == [("yahoo", "ACME", "3y")]
    assert bars["ACME"].loc[spy.index[-1], "Volume"] > 0
    assert audit["single_recheck_recovered"] == 1
    assert audit["partial_series_detected"] == 1
    assert audit["partial_series_recovered"] == 1
    assert audit["providers"]["ACME"] == "YAHOO_SINGLE_RECHECK_ADJUSTED"


def test_single_recheck_cannot_promote_stale_symbol():
    spy = _bars()
    old = _bars().iloc[:-1]
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": old}, []),
        single_loader=lambda ticker, period: old,
        fallback=lambda _: pd.DataFrame(),
    )
    assert bars["ACME"].index[-1] != spy.index[-1]
    assert audit["single_recheck_recovered"] == 0
    assert audit["single_recheck_rejected"]["ACME"] == "MISSING_ASOF_CLOSE"
    assert audit["fallback_recovered"] == 0


def test_single_recheck_rejects_adjustment_mismatch():
    spy = _bars()
    bad = _bars(last_volume=0)
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": bad}, []),
        single_loader=lambda ticker, period: _bars(shift=55),
        fallback=lambda _: pd.DataFrame(),
    )
    assert bars["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit["single_recheck_rejected"]["ACME"] == "ADJUSTMENT_OR_PRICE_MISMATCH"


def test_recovery_rejects_missing_internal_benchmark_session():
    spy = _bars()
    bad = _bars(last_volume=0)
    incomplete = _bars().drop(spy.index[-80])
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": bad}, []),
        single_loader=lambda ticker, period: incomplete,
        fallback=lambda _: incomplete,
    )
    assert bars["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit["single_recheck_rejected"]["ACME"] == "INCOMPLETE_BENCHMARK_CALENDAR"
    assert audit["secondary_rejected"]["ACME"] == "INCOMPLETE_BENCHMARK_CALENDAR"


def test_missing_symbol_may_recover_from_complete_yahoo_single():
    spy = _bars()
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        single_loader=lambda ticker, period: _bars(),
        fallback=lambda _: (_ for _ in ()).throw(AssertionError("stooq must not run")),
    )
    assert set(bars) == {"SPY", "ACME"}
    assert audit["missing"] == 0
    assert audit["single_recheck_recovered"] == 1


def test_single_retry_budget_is_bounded_and_auditable():
    spy = _bars()
    calls = []
    def single(ticker, period):
        calls.append(ticker)
        return _bars()
    bars, audit = P.download_bars(
        ["SPY", "AAA", "BBB", "CCC"],
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        single_loader=single,
        fallback=lambda _: pd.DataFrame(),
        max_fallback=1,
    )
    assert calls == ["AAA"]
    assert "BBB" not in bars
    assert audit["unreached_due_to_budget"] == 2
    assert audit["single_recheck_attempted"] == 1


def test_stooq_missing_symbol_must_have_current_close_and_full_history():
    spy = _bars()
    stale = _bars().iloc[:-1]
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        fallback=lambda _: stale,
    )
    assert "ACME" not in bars
    assert audit["secondary_rejected"]["ACME"] == "MISSING_ASOF_CLOSE"
