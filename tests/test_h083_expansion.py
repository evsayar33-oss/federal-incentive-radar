import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from radar import h083_expansion as E, event_watch as W

NOW = datetime(2026, 11, 27, 23, tzinfo=timezone.utc)
SESSION = pd.Timestamp("2026-11-27")
SHA = "a" * 40


def listings():
    return E.listing_map({"fields":["cik","name","ticker","exchange"], "data":[
        [1,"Core","CORE","NYSE"], [1,"Core other","CORB","Nasdaq"],
        [2,"New","NEW","Nasdaq"], [2,"New B","NEWB","NYSE"],
        [3,"Other","GOOD","NYSE"], [4,"OTC","OTC","OTC"],
        [5,"Future","FUT","NYSE"], [6,"Wrong","BAD","Nasdaq"],
        [7,"Ambiguous","AMB","NYSE"], [8,"Ambiguous","AMB","NYSE"],
        [9,"Third","THIRD","NYSE"]]})


def feed():
    return pd.DataFrame([{"ticker":"CORE","timestamp_quality":"exact_utc",
        "event_date":"2026-11-20","event_id":"core-id","known_at":"2026-11-20T12:00:00Z"}])


def filing(ticker, cik, date="2026-11-20", known="2026-11-21T00:00:00Z", form="10-Q",
           url=None, relevant=True, phrase='"CHIPS Act"'):
    accession="0000000001-26-000001"
    return {"ticker":ticker, "tickers":ticker, "source":"sec_edgar","event_id":f"id-{ticker}",
            "date":date,"known_at":known,"relevant":relevant,
            "url":url or f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/filing.htm",
            "extra":json.dumps({"cik":str(cik),"adsh":accession,"form":form,"phrase":phrase}),
            "title":"Issuer discussion","program":"semiconductors"}


def events():
    return pd.DataFrame([filing("NEW",2), filing("NEWB",2), filing("CORB",1),
        filing("GOOD",3), filing("OTC",4), filing("FUT",5,known="2026-11-28T00:00:00Z"),
        filing("BAD",99), filing("AMB",7), filing("THIRD",9,date="2024-01-01"),
        filing("THIRD",9,url="https://example.com/fake"), filing("THIRD",9,form="4"),
        filing("THIRD",9,relevant=False), filing("THIRD",9,phrase="irrelevant")])


def bars():
    index = pd.bdate_range(end=SESSION, periods=250)
    close = pd.Series(range(100,350), index=index,dtype=float)
    return pd.DataFrame({"Open":close,"High":close+1,"Low":close-1,
                         "Close":close,"Volume":1_000_000.0})


def test_discovery_checks_issuer_exchange_date_and_source_no_core_duplicates():
    core, leads = E.discover(feed(),events(),listings(),NOW,SESSION)
    assert core == {"CORE"}
    assert set(leads.ticker) == {"NEW","GOOD"}
    assert leads.issuer_cik.nunique() == len(leads)
    assert leads.evidence_kind.eq("SEC_DISCLOSURE_LEAD_NOT_AWARD").all()
    assert leads.timestamp_quality.eq("first_seen_utc").all()
    assert all(pd.Timestamp(t) == pd.Timestamp(NOW) for t in leads.known_at)
    assert "AMB" not in listings()


def test_retention_not_based_on_price_and_one_issuer():
    _, leads = E.discover(feed(),events(),listings(),NOW,SESSION,previous_members=["NEWB","GOOD"])
    assert leads.ticker.tolist() == ["NEWB","GOOD"]


def test_bad_mapping_is_fail_closed():
    with pytest.raises(ValueError):
        E.listing_map({"fields":["cik","ticker","exchange"],"data":[[1,"OTC","OTC"]]})


def test_growth_once_per_distinct_healthy_session_and_backlog():
    previous = {}
    for session in ("2026-11-23","2026-11-24","2026-11-25"):
        status = {"feature_asof":session,"total_limit":E.capacity(previous,session),
                  "ok":True,"eligible_tickers":153}
        previous = {**status,**E.progress(previous,status,100)}
    assert previous["next_total_limit"] == 250
    assert E.capacity(previous, "2026-11-25") == 200
    assert E.progress(previous,status,100)["next_total_limit"] == 250
    assert E.capacity(previous, "2026-11-27") == 250
    blocked = {**status, "ok":False}
    assert E.progress(previous,blocked,999)["next_total_limit"] == 200
    assert E.capacity({"next_total_limit":10000}, "2026-11-27") == 200


def test_price_outage_opens_circuit_before_other_symbols(monkeypatch):
    calls = []
    def empty(batch, period):
        calls.append(batch)
        return {}, ["RATE_LIMIT"]
    _, report = E.fetch_prices([f"T{i}" for i in range(70)], SESSION,batch_loader=empty,pause=lambda _:None)
    assert len(calls)==1
    assert report["primary_circuit_open"] is True
    assert report["primary_unattempted"] == 56
    assert report["fallback_attempted"] == report["independent_attempted"] == 0


def test_run_separate_store_keeps_core_files_unchanged(tmp_path,monkeypatch):
    inp = tmp_path/"input"; inp.mkdir()
    dest = tmp_path/"output"; dest.mkdir()
    feed().to_csv(inp/"event_trade_feed.csv",index=False)
    events().to_parquet(inp/"events.parquet")
    pd.DataFrame({"ticker":["SPY"],"date":["2026-11-25"],"close":[100.]}).to_parquet(inp/"prices.parquet")
    core = {"feature_asof":"2026-11-27","eligible_tickers":1,"data_coverage_ok":False}
    f = inp/"event_watch_status.json"; f.write_text(json.dumps(core))
    before = {x.name:x.read_bytes() for x in inp.iterdir()}
    monkeypatch.setenv("RADAR_STORE",str(dest))
    monkeypatch.setenv("RADAR_EXPANSION_INPUT",str(inp))
    monkeypatch.setenv("RADAR_EXPANSION_SOURCE_SHA",SHA)
    monkeypatch.setattr(E.W,"completed_nyse_session",lambda *_a,**_k:SESSION)
    monkeypatch.setattr(E.http,"get_json",lambda *_a,**_k: {"fields":[],"data":[]})
    mapped = listings()
    monkeypatch.setattr(E,"listing_map",lambda _:mapped)
    monkeypatch.setattr(E,"prior_history",lambda leads,*_a:leads)
    # 1/2 has data: no hiding the failed name or core outage.
    monkeypatch.setattr(E,"fetch_prices",lambda *_a,**_k:({"SPY":bars(),"NEW":bars()},{}))
    E.run(NOW)
    status=E.store.load_json(E.TABLE+"_status")
    assert status["eligible_tickers"]==2 and status["priced_tickers"]==1
    assert status["data_coverage_pct"]==0.5 and not status["ok"] and status["candidates"]==0
    assert status["core_eligible_tickers"]==1 and status["paper_approved"] is False
    assert status["source_commit_sha"]==SHA and status["core_coverage_not_modified"] is True
    assert {x.name:x.read_bytes() for x in inp.iterdir()}==before
    assert all(x.name.startswith(E.TABLE) for x in dest.iterdir())


def test_no_core_session_does_not_fetch_provider_or_publish_healthy(tmp_path,monkeypatch):
    inp=tmp_path/"input"; inp.mkdir()
    dest=tmp_path/"output"; dest.mkdir()
    feed().to_csv(inp/"event_trade_feed.csv",index=False)
    (inp/"event_watch_status.json").write_text('{"feature_asof":"2026-11-25"}')
    monkeypatch.setenv("RADAR_STORE",str(dest))
    monkeypatch.setenv("RADAR_EXPANSION_INPUT",str(inp))
    monkeypatch.setenv("RADAR_EXPANSION_SOURCE_SHA",SHA)
    monkeypatch.setattr(E.W,"completed_nyse_session",lambda *_a,**_k:SESSION)
    monkeypatch.setattr(E.http,"get_json",lambda *_a,**_k:pytest.fail("must not fetch on stale core session"))
    E.run(NOW)
    assert E.store.load_json(E.TABLE+"_status")["reason"]=="CORE_SESSION_UNAVAILABLE"

def test_prior_history_never_uses_current_session_to_admit_or_remove_retained():
    import pandas_market_calendars as mcal
    days = mcal.get_calendar("NYSE").schedule(start_date="2025-10-01",end_date="2026-11-25").index[-200:]
    prices = pd.DataFrame([{"ticker":t,"date":d,"close":100.}
        for t,dates in [("SPY",days),("NEW",days),("GOOD",days[1:].append(pd.DatetimeIndex([SESSION])))]
        for d in dates])
    _, leads = E.discover(feed(),events(),listings(),NOW,SESSION)
    assert E.prior_history(leads,prices,SESSION).ticker.tolist() == ["NEW"]
    assert set(E.prior_history(leads,prices,SESSION,["GOOD"]).ticker) == {"NEW","GOOD"}
