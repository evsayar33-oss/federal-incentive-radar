"""Offline regressions for H083 incomplete-series recovery and audit gates."""
import numpy as np
import pandas as pd

from radar import price_resilience as P


def _bars(n=250, last_volume=1000000, shift=0):
    days = pd.bdate_range("2025-01-02", periods=n)
    close = pd.Series(np.linspace(100, 130, n) + shift, index=days)
    return pd.DataFrame({
        "Open": close * .999, "High": close * 1.002,
        "Low": close * .997, "Close": close,
        "Volume": [1000000] * (n - 1) + [last_volume],
    }, index=days)


def test_missing_last_volume_recovers_only_whole_consistent_secondary():
    spy = _bars()
    primary = _bars(last_volume=0)
    secondary = _bars()
    calls = []
    def yahoo(names, period):
        return {"SPY": spy, "ACME": primary}, []
    def fallback(ticker):
        calls.append(ticker)
        return secondary
    bars, audit = P.download_bars(["SPY", "ACME"], yahoo_loader=yahoo, fallback=fallback)
    assert calls == ["ACME"]
    assert bars["ACME"].loc[spy.index[-1], "Volume"] > 0
    assert audit["partial_series_detected"] == 1
    assert audit["partial_series_recovered"] == 1
    assert audit["providers"]["ACME"] == "STOOQ_SECONDARY_ADJUSTMENT_CHECKED"


def test_inconsistent_secondary_never_replaces_primary():
    spy = _bars()
    primary = _bars(last_volume=0)
    secondary = _bars(shift=50)
    bars, audit = P.download_bars(
        ["SPY", "ACME"], yahoo_loader=lambda t, p: ({"SPY": spy, "ACME": primary}, []),
        fallback=lambda _: secondary,
    )
    assert bars["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit["secondary_rejected"]["ACME"] == "ADJUSTMENT_OR_PRICE_MISMATCH"
    assert audit["partial_series_recovered"] == 0


def test_incomplete_secondary_never_patches_individual_bars():
    spy = _bars()
    primary = _bars(last_volume=0)
    short = _bars(n=100)
    bars, audit = P.download_bars(
        ["SPY", "ACME"], yahoo_loader=lambda t, p: ({"SPY": spy, "ACME": primary}, []),
        fallback=lambda _: short,
    )
    assert bars["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit["partial_series_recovered"] == 0


def test_fallback_budget_limits_partial_series_retries():
    spy = _bars()
    calls = []
    def fallback(ticker):
        calls.append(ticker)
        return _bars()
    bars, audit = P.download_bars(
        ["SPY", "AAA", "BBB"],
        yahoo_loader=lambda t, p: ({"SPY": spy, "AAA": _bars(last_volume=0),
                                      "BBB": _bars(last_volume=0)}, []),
        fallback=fallback, max_fallback=1,
    )
    assert len(calls) == 1
    assert audit["partial_series_detected"] == 2
    assert audit["partial_series_recovered"] == 1
    assert bars["BBB"].loc[spy.index[-1], "Volume"] == 0
