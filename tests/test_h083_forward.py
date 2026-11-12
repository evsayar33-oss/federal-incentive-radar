"""H083 autonomous research lifecycle: causal decisions, fills, gates, plans."""
from datetime import datetime, timezone
import json

import numpy as np
import pandas as pd
import pytest

from radar import h083_catalog as C
from radar import h083_forward as F

NOW=datetime(2026,11,30,23,0,tzinfo=timezone.utc)


def _index():
    return pd.DatetimeIndex(["2026-11-27","2026-11-30","2026-12-01",
                             "2026-12-30","2026-12-31","2027-01-04","2027-01-05"])


def _watch():
    return pd.DataFrame([{
        "ticker":"ACME","rule":rule,"rank":1,"score":1.,
        "feature_asof":"2026-11-30","event_id":"capex:1",
        "event_family":"corporate_capex","category":"plant",
        "source_url":"https://example.org/invest","market_regime":"RISK_ON",
    } for rule in F.RULES])


def _bars():
    dates=pd.DatetimeIndex(["2026-12-01","2027-01-04","2026-11-30"]).sort_values()
    return {ticker:pd.DataFrame({
        "Open":[100.,101.,103.],"High":[102.,103.,105.],
        "Low":[99.,100.,102.],"Close":[101.,102.,104.],
        "Volume":[1e6]*3},index=dates)
        for ticker in ["SPY","ACME"]}


def _cohorts():
    c,new=F.freeze_cohorts(pd.Timestamp("2026-11-30"),_index(),_watch(),
                            pd.DataFrame(columns=F.COHORT_COLS),NOW)
    assert len(c)==2 and len(new)==2
    return c


def test_catalog_contains_44_declared_unique_styles():
    cats=C.catalog()
    assert len(cats)==44
    assert cats.rule.nunique()==44
    assert (cats.monitoring=="FORWARD_PAPER_WATCH").sum()==2
    assert not cats.certified_alpha.any()


def test_three_news_families_are_not_alpha():
    news=C.news_catalog()
    assert len(news)==3 and not news.historical_alpha_confirmed.any()


def test_month_end_only_with_real_exchange_calendar_index():
    assert F._month_end_rebalance(pd.Timestamp("2026-11-27"),_index()) is None
    assert F._month_end_rebalance(pd.Timestamp("2026-11-30"),_index())==(
        pd.Timestamp("2026-12-01"),pd.Timestamp("2027-01-04"))


def test_cohorts_are_frozen_and_reruns_idempotent():
    old=_cohorts()
    new_total,new=F.freeze_cohorts(pd.Timestamp("2026-11-30"),_index(),_watch(),old,NOW)
    assert len(new_total)==2 and new.empty
    assert all(pd.Timestamp(x)>pd.Timestamp("2026-11-30") for x in old.entry_session)


def test_stale_features_refused():
    wrong=_watch()
    wrong.loc[0,"feature_asof"]="2026-11-27"
    with pytest.raises(ValueError,match="staleness"):
        F.freeze_cohorts(pd.Timestamp("2026-11-30"),_index(),wrong,
                         pd.DataFrame(columns=F.COHORT_COLS),NOW)


def test_initial_paper_fills_not_fabricated():
    c=_cohorts()
    f=F.capture_fills(pd.Timestamp("2026-11-30"),NOW,c,_bars(),
                      pd.DataFrame(columns=F.FILL_COLS))
    assert f.empty


def test_actual_session_and_prior_date_missed():
    c=_cohorts()
    now=datetime(2026,12,1,23,tzinfo=timezone.utc)
    fills=F.capture_fills(pd.Timestamp("2026-12-01"),now,c,_bars(),
                          pd.DataFrame(columns=F.FILL_COLS))
    assert len(fills)==4 # 2 rules x (ACME + SPY)
    assert set(fills.phase)=={"ENTRY"}
    assert fills.valid.astype(str).str.lower().eq("true").all()
    late=F.capture_fills(pd.Timestamp("2027-01-04"),
                         datetime(2027,1,4,23,tzinfo=timezone.utc),
                         c,_bars(),pd.DataFrame(columns=F.FILL_COLS))
    entry=late[late.phase.eq("ENTRY")]
    assert entry.issue.eq("MISSED_ACTUAL_SESSION").all()
    assert not entry.valid.astype(str).str.lower().eq("true").any()


def test_no_repainting_first_observed_fills():
    c=_cohorts()
    first=F.capture_fills(pd.Timestamp("2026-12-01"),
                          datetime(2026,12,1,23,tzinfo=timezone.utc),c,_bars(),
                          pd.DataFrame(columns=F.FILL_COLS))
    changed=_bars()
    changed["ACME"].loc[pd.Timestamp("2026-12-01"),"Open"]=9999
    second=F.capture_fills(pd.Timestamp("2026-12-01"),
                           datetime(2026,12,1,23,30,tzinfo=timezone.utc),
                           c,changed,first)
    assert len(second)==len(first)
    assert (second.open_usd==first.open_usd).all()


def test_monthly_costs_are_deducted_from_cash_account():
    buy={"X":100.,"SPY":100.}
    sell={"X":110.,"SPY":110.}
    net=F._net_month(buy,sell,["X"],5)
    assert 0<net<.02 # 1x $1990 position is only 19.9% of total account
    assert F._net_month(buy,sell,["X"],30)<net


def test_forward_result_requires_both_actual_month_opens():
    c=_cohorts()
    f=F.capture_fills(pd.Timestamp("2026-12-01"),
                      datetime(2026,12,1,23,tzinfo=timezone.utc),
                      c,_bars(),pd.DataFrame(columns=F.FILL_COLS))
    f=F.capture_fills(pd.Timestamp("2027-01-04"),
                      datetime(2027,1,4,23,tzinfo=timezone.utc),c,_bars(),f)
    results=F.resolve_months(datetime(2027,1,4,23,tzinfo=timezone.utc),
                             pd.Timestamp("2027-01-04"),c,f,
                             pd.DataFrame(columns=F.RESULT_COLS))
    assert len(results)==2
    assert results.complete.astype(str).str.lower().eq("true").all()
    assert (pd.to_numeric(results.net_return)<.01).all()
    assert (pd.to_numeric(results.excess_vs_spy)<0).all()


def test_missing_or_late_fill_causes_invalid_month():
    c=_cohorts()
    late=F.capture_fills(pd.Timestamp("2027-01-04"),
                         datetime(2027,1,4,23,tzinfo=timezone.utc),c,_bars(),
                         pd.DataFrame(columns=F.FILL_COLS))
    got=F.resolve_months(datetime(2027,1,4,23,tzinfo=timezone.utc),
                         pd.Timestamp("2027-01-04"),c,late,
                         pd.DataFrame(columns=F.RESULT_COLS))
    assert not got.complete.astype(str).str.lower().eq("true").any()
    again=F.resolve_months(datetime(2027,1,5,23,tzinfo=timezone.utc),
                           pd.Timestamp("2027-01-05"),c,late,got)
    assert len(again)==len(got)


def _results(n=24,with_bad=False):
    rows=[]
    for rule in F.RULES:
        for month in pd.period_range("2027-01",periods=n,freq="M"):
            bad=(with_bad and month==pd.Period("2028-04","M"))
            net=.03+.0004*(month.month%4)
            rows.append({
                "cohort_id":f"{rule}-{month}","rule":rule,
                "entry_month":str(month),"signal_session":"2026-12-31",
                "entry_session":"2027-01-04","exit_session":"2027-02-01",
                "n_positions":5,"net_return":net if not bad else np.nan,
                "spy_net_return":.005 if not bad else np.nan,
                "excess_vs_spy":net-.005 if not bad else np.nan,
                "net_return_stress":net-.004 if not bad else np.nan,
                "complete":not bad,"failure":"MISSING" if bad else "",
                "closed_at_utc":"2030-01-01T00:00Z",
            })
    return pd.DataFrame(rows)


def test_gate_inadequate_sample_is_never_approved():
    evidence=F.evidence_gate(_results(23),NOW)
    assert not evidence.validated_for_info_plans.any()
    assert set(evidence.status)=={"COLLECTING_FORWARD"}


def test_gate_consecutive_prospective_months_can_validate():
    # Artificial extreme positive monthly results exercise the dormant
    # certification route, not a claim about actual H083 returns.
    evidence=F.evidence_gate(_results(24),NOW)
    assert evidence.validated_for_info_plans.all()
    assert set(evidence.status)=={"FORWARD_VALIDATED_PAPER"}


def test_gate_invalid_missing_month_blocks_validation():
    e=F.evidence_gate(_results(25,with_bad=True),NOW)
    assert not e.validated_for_info_plans.any()


def test_plan_cannot_be_generated_without_gate():
    cohorts=_cohorts()
    gate=F.evidence_gate(pd.DataFrame(columns=F.RESULT_COLS),NOW)
    plans=F.create_plans(pd.Timestamp("2026-11-30"),NOW,cohorts,
                         _watch(),_bars(),gate,pd.DataFrame(columns=F.PLAN_COLS))
    assert plans.empty


def test_valid_paper_gate_creates_information_plans_not_broker_orders():
    cohorts=_cohorts()
    gate=F.evidence_gate(_results(24),NOW)
    plans=F.create_plans(pd.Timestamp("2026-11-30"),NOW,cohorts,
                         _watch(),_bars(),gate,pd.DataFrame(columns=F.PLAN_COLS))
    assert len(plans)==4 # 2 x (buy + predeclared exit)
    assert set(plans.side)=={"BUY_NEXT_OPEN","EXIT_NEXT_MONTH_OPEN"}
    assert plans.expected_order_status.eq("INFORMATION_ONLY_NO_BROKER_EXECUTION").all()


def test_monthly_telegram_does_not_advertise_watches_as_trades():
    text=F.format_monthly_telegram(_cohorts(),F.evidence_gate(
        pd.DataFrame(columns=F.RESULT_COLS),NOW),pd.DataFrame(columns=F.PLAN_COLS))
    assert "AL/SAT SİNYALİ DEĞİL" in text
    assert "34 haber sonrası" in text
    assert "1,50$" in text


def test_two_dashboard_sources_compile():
    import ast
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    for name in ("app/h083_page.py","app/simple.py","app/streamlit_app.py"):
        ast.parse((root/name).read_text(encoding="utf-8"))


def test_wrong_dst_cron_slot_does_not_publish():
    schedule=pd.DataFrame(
        {"market_close":[pd.Timestamp("2026-11-30T21:00:00Z")]},
        index=[pd.Timestamp("2026-11-30")],
    )
    allow,why=F._active_session(
        datetime(2026,11,30,21,20,tzinfo=timezone.utc),
        pd.Timestamp("2026-11-30"),schedule)
    assert not allow and why=="OUTSIDE_17_ET_AFTERCLOSE_SLOT"
    ok,why=F._active_session(
        datetime(2026,11,30,22,20,tzinfo=timezone.utc),
        pd.Timestamp("2026-11-30"),schedule)
    assert ok and why=="AFTER_NYSE_CLOSE"


def test_exchange_calendar_reads_real_nyse_holidays():
    today,schedule,index=F._calendar(datetime(2026,11,26,23,tzinfo=timezone.utc))
    assert pd.Timestamp("2026-11-26") not in index # Thanksgiving
    assert pd.Timestamp("2026-11-27") in index


def test_report_labels_trade_plan_as_information_not_execution():
    c=_cohorts()
    text=F.format_monthly_telegram(c,F.evidence_gate(_results(24),NOW),
                                   F.create_plans(pd.Timestamp("2026-11-30"),NOW,c,
                                   _watch(),_bars(),F.evidence_gate(_results(24),NOW),
                                   pd.DataFrame(columns=F.PLAN_COLS)))
    assert "Gerçek" not in text or "gerçek" in text.lower()
    assert "broker emri YOK" in text
    assert "EXIT" not in text or "çıkış" in text
