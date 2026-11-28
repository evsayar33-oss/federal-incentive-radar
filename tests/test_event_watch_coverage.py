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


def test_actual_early_close_and_missing_benchmark_are_fail_closed():
    from radar.event_watch import completed_nyse_session
    import pandas as pd
    # Thanksgiving Friday closes at 13:00 ET. Include the publication buffer.
    before=datetime(2026,11,27,18,30,tzinfo=timezone.utc)
    after=datetime(2026,11,27,18,46,tzinfo=timezone.utc)
    assert completed_nyse_session(before,current_only=True) is None
    assert completed_nyse_session(after,current_only=True)==pd.Timestamp("2026-11-27")
    spy,stock=_bars()
    res,_,status=evaluate_watch(_feed(),{"SPY":spy.iloc[:-1],"ACME":stock},
                               after,asof=spy.index[-1])
    assert res.empty
    assert status["ok"] is False
    assert status["reason"]=="MISSING_REQUIRED_BENCHMARK_SESSION"
    assert status["priced_tickers"]==0
    assert status["data_coverage_ok"] is False
    assert status["decision"]=="WATCH_ONLY_NO_TRADE"
    assert status["by_rule"]=={"monthly_low_beta":0,"monthly_vol_squeeze":0}


def test_recovery_state_rotates_only_for_same_completed_session(tmp_path,monkeypatch):
    from radar import event_watch as W,price_resilience as P
    import pandas as pd
    state={}
    calls=[]
    monkeypatch.setattr(W.store,"load_json",lambda _:state.copy())
    monkeypatch.setattr(W.store,"save_json",lambda _,obj:(state.clear(),state.update(obj)))
    def fake(tickers,period,**kwargs):
        calls.append(kwargs)
        return {},{"independent_recovered_symbols":["ACME"]}
    monkeypatch.setattr(P,"download_bars",fake)
    for d in ("2026-11-27","2026-11-27","2026-11-30"):
        W.download_daily_bars(["SPY","ACME"],asof=pd.Timestamp(d))
    assert [x["recovery_round"] for x in calls]==[0,1,0]
    assert calls[1]["previous_independent_recovered"]==("ACME",)
    assert calls[2]["previous_independent_recovered"]==()
