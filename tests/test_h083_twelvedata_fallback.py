"""Offline regression for a key-gated, rate-limited independent H083 provider."""
from __future__ import annotations

import pandas as pd
import pytest

from radar import price_resilience as P
from radar import twelvedata_provider as TD
from radar.h083_forward import _unverified_secondary
from test_h083_partial_price_recovery import _bars


def _api_payload(ticker="ACME", currency="USD", tz="America/New_York"):
    index = pd.bdate_range("2025-01-02", periods=250)
    return {
        "status": "ok",
        "meta": {
            "symbol": ticker, "interval": "1day", "currency": currency,
            "exchange_timezone": tz, "exchange": "NASDAQ",
        },
        "values": [
            {"datetime": str(date.date()), "open": "100", "high": "102",
             "low": "99", "close": "101", "volume": "2000000"}
            for date in index[::-1]
        ],
    }


def test_twelve_data_requires_secret_and_makes_zero_calls(monkeypatch):
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setattr(TD.http, "request", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("network without key")))
    with pytest.raises(RuntimeError, match="NOT_CONFIGURED"):
        TD.fetch_bars("ACME")


def test_independent_provider_uses_header_adjusted_daily_and_us_metadata(monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "fake-test-key")
    seen = {}
    class Response:
        def json(self):
            return _api_payload()
    def fake_request(method, url, **kwargs):
        seen.update({"method": method, "url": url, **kwargs})
        return Response()
    monkeypatch.setattr(TD.http, "request", fake_request)
    result = TD.fetch_bars("ACME")
    assert len(result) == 250
    assert result.index.is_monotonic_increasing
    assert result["Close"].iloc[-1] == 101
    assert seen["method"] == "GET"
    assert seen["params"]["adjust"] == "all"
    assert seen["params"]["interval"] == "1day"
    assert seen["params"]["outputsize"] >= 250
    assert "apikey" not in seen["params"]
    assert "fake-test-key" not in seen["url"]
    assert seen["headers"]["Authorization"] == "apikey fake-test-key"
    assert seen["retries"] == 0


@pytest.mark.parametrize("payload", [
    {"status": "error", "code": 429, "message": "quota exceeded"},
    _api_payload(ticker="WRONG"),
    _api_payload(currency="EUR"),
    _api_payload(tz="Europe/London"),
])
def test_independent_provider_fail_closes_on_bad_response(monkeypatch, payload):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "fake-test-key")
    class Response:
        def json(self):
            return payload
    monkeypatch.setattr(TD.http, "request", lambda *args, **kwargs: Response())
    with pytest.raises((ValueError, RuntimeError)):
        TD.fetch_bars("ACME")


def test_no_provider_key_preserves_existing_stooq_path(monkeypatch):
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    spy = _bars()
    calls = []
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        fallback=lambda ticker: (calls.append(ticker) or _bars()),
    )
    assert calls == ["ACME"]
    assert audit["independent_provider"] == "DISABLED_NO_API_KEY"
    assert audit["independent_attempted"] == 0
    assert audit["fallback_recovered"] == 1
    assert "ACME" in bars


def test_independent_recovers_complete_series_without_stooq(monkeypatch):
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    spy = _bars()
    primary = _bars(last_volume=0)
    calls = []
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": primary}, []),
        independent_loader=lambda ticker, period: (calls.append(ticker) or _bars()),
        fallback=lambda _: (_ for _ in ()).throw(AssertionError("Stooq not needed")),
    )
    assert calls == ["ACME"]
    assert bars["ACME"].loc[spy.index[-1], "Volume"] > 0
    assert audit["partial_series_recovered"] == 1
    assert audit["independent_attempted"] == 1
    assert audit["independent_recovered"] == 1
    assert audit["fallback_attempted"] == 0
    assert audit["providers"]["ACME"] == "TWELVE_DATA_INDEPENDENT_ADJUSTMENT_CHECKED"


def test_independent_only_watch_when_no_primary_overlap():
    spy = _bars()
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        independent_loader=lambda ticker, period: _bars(),
        fallback=lambda _: (_ for _ in ()).throw(AssertionError("do not call stooq")),
    )
    assert "ACME" in bars
    assert audit["providers"]["ACME"] == "TWELVE_DATA_INDEPENDENT_NO_PRIMARY_OVERLAP"
    assert _unverified_secondary(audit["providers"]["ACME"])


def test_independent_mismatch_and_missing_session_fail_closed():
    spy = _bars()
    primary = _bars(last_volume=0)
    bad = _bars(shift=80)
    bars, audit = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": primary}, []),
        independent_loader=lambda ticker, period: bad,
        fallback=lambda _: pd.DataFrame(),
    )
    assert bars["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit["independent_recovered"] == 0
    assert audit["independent_rejected"]["ACME"] == "ADJUSTMENT_OR_PRICE_MISMATCH"
    missing = _bars().drop(spy.index[-70])
    bars2, audit2 = P.download_bars(
        ["SPY", "ACME"],
        yahoo_loader=lambda names, period: ({"SPY": spy, "ACME": primary}, []),
        independent_loader=lambda ticker, period: missing,
        fallback=lambda _: pd.DataFrame(),
    )
    assert bars2["ACME"].loc[spy.index[-1], "Volume"] == 0
    assert audit2["independent_rejected"]["ACME"] == "INCOMPLETE_BENCHMARK_CALENDAR"


def test_independent_calls_bounded_before_stooq():
    spy = _bars()
    called = []
    names = ["SPY"] + [f"T{i:02d}" for i in range(9)]
    _, audit = P.download_bars(
        names,
        yahoo_loader=lambda names, period: ({"SPY": spy}, []),
        independent_loader=lambda ticker, period: (
            called.append(ticker) or pd.DataFrame()
        ),
        fallback=lambda _: pd.DataFrame(),
        max_independent=2,
    )
    assert called == ["T00", "T01"]
    assert audit["independent_attempted"] == 2
    assert audit["independent_recovered"] == 0
    assert audit["fallback_attempted"] == 9
    assert audit["independent_budget"] == 5


def test_forward_fill_gate_rejects_every_third_party_provider():
    assert _unverified_secondary("STOOQ_SECONDARY_ADJUSTMENT_CHECKED")
    assert _unverified_secondary("TWELVE_DATA_INDEPENDENT_ADJUSTMENT_CHECKED")
    assert _unverified_secondary("TWELVE_DATA_INDEPENDENT_NO_PRIMARY_OVERLAP")
    assert not _unverified_secondary("YAHOO_ADJUSTED")
    assert not _unverified_secondary("YAHOO_SINGLE_RECHECK_ADJUSTED")
