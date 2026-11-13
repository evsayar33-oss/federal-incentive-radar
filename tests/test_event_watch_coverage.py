from datetime import datetime, timezone
from radar.event_watch import evaluate_watch
from test_event_watch import _bars, _feed

def test_partial_price_coverage_fails_closed():
    spy, stock = _bars()
    feed = _feed()
    extra = _feed(ticker="MISSING")
    import pandas as pd
    both = pd.concat([feed, extra], ignore_index=True)
    result, excluded, status = evaluate_watch(
        both, {"SPY": spy, "ACME": stock},
        datetime(2026, 11, 12, 22, tzinfo=timezone.utc), asof=spy.index[-1],
    )
    assert result.empty
    assert status["ok"] is False
    assert status["reason"] == "INSUFFICIENT_PRICE_COVERAGE"
    assert status["exclusion_reasons"]["PRICE_MISSING"] == 1
    assert status["data_coverage_ok"] is False
