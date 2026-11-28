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


def test_single_symbol_uses_supported_yahoo_history_window(monkeypatch):
    import sys
    from types import SimpleNamespace
    seen = {}
    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol
        def history(self, **kwargs):
            seen.update(kwargs)
            return _bars()
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker))
    assert len(P.yahoo_single_bars("ACME", "3y")) == 250
    assert seen["symbol"] == "ACME"
    assert seen["period"] == "5y"
    assert seen["auto_adjust"] is True


def test_excess_partial_symbols_rotate_across_benchmark_sessions():
    spy = _bars(n=251)
    names = ["SPY"] + ["X" + str(i).zfill(2) for i in range(29)]
    bad = _bars(n=251)
    bad["Volume"] = 0
    def run(benchmark):
        calls = []
        P.download_bars(
            names,
            yahoo_loader=lambda names, period: (
                {"SPY": benchmark, **{t: bad for t in names if t != "SPY"}}, []),
            single_loader=lambda ticker, period: (
                calls.append(ticker) or pd.DataFrame()
            ),
            fallback=lambda _: pd.DataFrame(),
            max_fallback=24,
        )
        return calls
    a = run(spy.iloc[:-1])
    b = run(spy)
    assert len(a) == len(b) == 24
    assert set(a) != set(b)


def test_same_session_retries_rotate_and_keep_previous_recovered_target():
    spy = _bars()
    names = ["SPY"] + [f"X{i:02}" for i in range(34)]
    incomplete = spy.iloc[:-1]
    def run(round_number, previous=()):
        calls = []
        _, report = P.download_bars(
            names, yahoo_loader=lambda *_: ({"SPY":spy, **{t:incomplete for t in names[1:]}},[]),
            single_loader=lambda ticker,period: (calls.append(ticker) or incomplete),
            independent_loader=lambda *_: spy, fallback=lambda _:pd.DataFrame(),
            max_fallback=24, max_independent=5, required_asof=spy.index[-1],
            recovery_round=round_number, previous_independent_recovered=previous)
        return calls, report
    first, a = run(0)
    second, b = run(1, tuple(a["independent_recovered_symbols"]))
    assert len(first)==len(second)==24
    assert set(first)!=set(second)
    assert set(a["independent_recovered_symbols"]) <= set(second)
    assert a["total_recovered"]==a["independent_recovered"]==5
    assert b["recovery_round"]==1


def test_stale_spy_is_prioritized_for_the_exact_session():
    spy = _bars()
    stale = spy.iloc[:-1]
    calls=[]
    bars,audit=P.download_bars(["SPY","ACME"],
        yahoo_loader=lambda *_:({"SPY":stale,"ACME":spy},[]),
        single_loader=lambda ticker,period:(calls.append(ticker) or spy),
        fallback=lambda _:pd.DataFrame(),required_asof=spy.index[-1])
    assert calls[0]=="SPY"
    assert bars["SPY"].index[-1]==spy.index[-1]
    assert audit["single_recheck_recovered"]==audit["total_recovered"]==1
    bars,audit=P.download_bars(["SPY","ACME"],
        yahoo_loader=lambda *_:({"SPY":stale,"ACME":spy},[]),
        single_loader=lambda *_:stale,fallback=lambda _:stale,required_asof=spy.index[-1])
    assert bars["SPY"].index[-1]!=spy.index[-1]
    assert audit["single_recheck_rejected"]["SPY"]=="MISSING_ASOF_CLOSE"


def test_adaptive_independent_budget_increases_only_when_coverage_is_bad():
    spy=_bars()
    names=["SPY"]+[f"X{i:02}" for i in range(40)]
    def run(missing):
        bad=spy.iloc[:-1]
        _,report=P.download_bars(names,
           yahoo_loader=lambda *_:({"SPY":spy,**{t:bad if t in missing else spy for t in names[1:]}},[]),
           independent_loader=lambda *_:spy,fallback=lambda _:pd.DataFrame(),
           required_asof=spy.index[-1], max_independent=12)
        return report
    low=run(set(names[1:25]))
    healthy=run(set(names[1:9]))
    assert low["independent_attempted"]==12
    assert low["total_recovered"]==12
    assert healthy["independent_attempted"]==5


def test_normalize_rejects_infinite_price_and_volume():
    import numpy as np
    bars=_bars().astype(float)
    bars.iloc[-1,3]=np.inf
    bars.iloc[-2,4]=np.inf
    assert len(P.normalize(bars))==len(bars)-2
