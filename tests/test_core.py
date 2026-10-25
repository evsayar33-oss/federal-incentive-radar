"""Çekirdek testler. İnternet gerektirmez.  Çalıştırma: PYTHONPATH=src pytest -q tests
(pytest yoksa: PYTHONPATH=src python tests/test_core.py)"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("RADAR_STORE", tempfile.mkdtemp(prefix="radar_test_"))
os.environ.setdefault("RADAR_LOG", "WARNING")


def test_federal_register_parse():
    from radar.sources import federal_register as fr
    rows = fr.parse([
        {"document_number": "2026-1", "title": "Notice of Funding Opportunity: Grid Resilience", "type": "Notice",
         "abstract": "", "agencies": [{"name": "Energy Department"}], "publication_date": "2026-10-01", "html_url": "u"},
        {"document_number": "2026-2", "title": "Section 45X credit", "type": "Proposed Rule", "abstract": "",
         "agencies": [], "publication_date": "2026-10-02", "html_url": "u"},
    ])
    assert rows[0]["stage"] == 4 and rows[1]["stage"] == 3


def test_congress_status():
    from radar.sources.congress import classify_action, ordinal
    assert classify_action("Became Public Law No: 119-21.") == (2, "law")
    assert classify_action("Passed Senate with an amendment")[1] == "passed_chamber"
    assert ordinal(119) == "119th" and ordinal(111) == "111th" and ordinal(122) == "122nd"


def test_sec_ticker_extraction():
    from radar.sources.sec_edgar import tickers_from_names
    assert tickers_from_names(["INTEL CORP  (INTC)  (CIK 0000050863)"]) == ["INTC"]
    assert tickers_from_names(["Alphabet Inc.  (GOOGL, GOOG)  (CIK 0001652044)"]) == ["GOOGL"]


def test_grants_amount_search():
    from radar.sources.grants_gov import find_number
    assert find_number({"data": {"synopsis": {"awardCeiling": "", "estimatedFunding": "25,000,000"}}}) == 25_000_000


def test_google_news_rss():
    from radar.sources.gdelt import parse_rss
    xml = ('<rss><channel><item><title>DOE awards $1B for grid</title><link>https://x/a</link>'
           '<pubDate>Fri, 23 Oct 2026 14:00:00 GMT</pubDate><source url="https://r">Reuters</source></item></channel></rss>')
    rows = parse_rss(xml, "q")
    assert rows[0]["date"] == "2026-10-23" and rows[0]["agency"] == "Reuters" and rows[0]["stage"] == 0


def test_bea_spillover():
    import numpy as np
    from radar.settings import SECTORS
    from radar.sources import bea_io
    codes = ["11", "21", "22", "23", "31G", "42", "44RT", "48TW", "51", "FIRE", "PROF", "6", "7", "81"]
    rows = []
    for r in codes:
        for c in codes:
            v = 1.2 if r == c else 0.02
            if r == "21" and c == "31G":
                v = 0.15        # imalat talebi madenciliği çeker
            rows.append({"RowCode": r, "RowDescr": r, "ColCode": c, "ColDescr": c, "DataValue": f"{v}", "Year": "2023"})
            rows.append({"RowCode": r, "RowDescr": r, "ColCode": c, "ColDescr": c, "DataValue": "9", "Year": "2019"})
    S, year = bea_io.to_sector_matrix(bea_io.parse(rows))
    assert year == "2023" and S.shape == (11, 11)
    # Bilgi teknolojisine şok → Enerji/Malzemeye dolaylı etki doğmalı
    assert S.loc["Energy", "Information Technology"] > 0.01
    T = bea_io.transform(S.reset_index(names="sector"), 0.5)
    w = np.zeros(11); w[SECTORS.index("Information Technology")] = 1.0
    tot = w @ T
    assert tot[SECTORS.index("Energy")] > 0 and tot.sum() > 1.0


def test_contract_type():
    from radar import schema
    from radar.classify import Classifier
    df = schema.frame([schema.event("usaspending", "k", 5, "2026-10-01", "Navy: HUNTINGTON INGALLS",
                                    text="submarine construction", amount_usd=9e8,
                                    amount_type="contract_obligation", agency="Department of Defense")])
    out = Classifier(load_model=False).classify(df)
    assert out.loc[0, "incentive_type"] == "contract" and bool(out.loc[0, "relevant"])


def test_fred_csv():
    from radar.sources.fred_csv import parse_csv
    df = parse_csv("observation_date,EPUFISCAL\n2026-01-01,120.5\n2026-02-01,.\n", "EPUFISCAL")
    assert len(df) == 1 and df["value"].iloc[0] == 120.5


def test_entity_matching():
    import pandas as pd
    from radar.entity import Matcher, normalize
    assert normalize("ALBEMARLE U.S., INC.") == "albemarle"
    uni = pd.DataFrame({"ticker": ["ALB", "CEG", "INTC"], "name": ["Albemarle Corp", "Constellation Energy Corp", "Intel Corp"]})
    m = Matcher(uni)
    assert m.match("ALBEMARLE U.S., INC.")[0] == "ALB"
    assert m.match("INTEL CORPORATION")[2] == "exact"
    assert m.match("CONSTELLATION ENERGY GENERATION LLC")[2] == "review"   # insan onayı ister
    assert m.match("CITY OF PHOENIX")[0] == ""


def test_classifier_rules():
    import pandas as pd
    from radar import schema
    from radar.classify import Classifier
    df = schema.frame([
        schema.event("federal_register", "a", 4, "2026-10-01", "CHIPS Incentives Program—Commercial Fabrication Facilities"),
        schema.event("federal_register", "b", 3, "2026-10-01", "Airworthiness Directives; The Boeing Company Airplanes"),
        schema.event("congress", "c", 1, "2026-10-01", "To amend the IRC to extend the clean hydrogen production credit (45V)"),
    ])
    out = Classifier(load_model=False).classify(df)
    assert out.loc[0, "program"] == "semiconductors" and bool(out.loc[0, "relevant"])
    assert not bool(out.loc[1, "relevant"])
    assert out.loc[2, "program"] == "hydrogen"


def test_slot_resolution_dst():
    from radar.run import resolve_slot
    utc = ZoneInfo("UTC")
    # Yaz saati (EDT, UTC-4): 13:47 UTC = 09:47 ET → açılış
    assert resolve_slot("auto", False, {}, datetime(2026, 7, 15, 13, 47, tzinfo=utc)) == "open"
    # Kış saati (EST, UTC-5): 13:47 UTC = 08:47 ET → henüz değil; 14:47 UTC → açılış
    assert resolve_slot("auto", False, {}, datetime(2026, 1, 14, 13, 47, tzinfo=utc)) is None
    assert resolve_slot("auto", False, {}, datetime(2026, 1, 14, 14, 47, tzinfo=utc)) == "open"
    # Aynı gün ikinci tetikleme atlanır
    assert resolve_slot("auto", False, {"slots_done": {"2026-07-15": ["open"]}}, datetime(2026, 7, 15, 14, 47, tzinfo=utc)) is None
    # Kapanış (EST): 21:19 UTC = 16:19 ET
    assert resolve_slot("auto", False, {}, datetime(2026, 1, 14, 21, 19, tzinfo=utc)) == "close"
    # Hafta sonu çalışmaz
    assert resolve_slot("auto", False, {}, datetime(2026, 7, 18, 13, 47, tzinfo=utc)) is None


def test_demo_end_to_end():
    from radar import demo, store
    import data as D
    demo.build(days=90)
    s = store.read("scores_latest")
    assert len(s) == 33 and (s["p10"] <= s["p50"]).all() and (s["p50"] <= s["p90"]).all()
    assert not store.read("ledger").empty
    assert not store.read("company_exposure").empty
    ev = D.relevant_events(D.load_table("events"))
    assert not D.pipeline_funnel(ev).empty
    assert not D.score_matrix(D.load_table("scores_latest")).empty
    assert len(D.labeling_queue(D.load_table("events"))) > 0


def _synthetic_market(seed: int = 3):
    """Gerçek etkisi bilinen sentetik piyasa: CHIPS 8-K'ları +%4 (5 gün), 10-Q'lar etkisiz."""
    import json
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=700)
    tick = [f"T{i}" for i in range(30)]
    spy = rng.normal(0.0003, 0.01, len(dates))
    rets = {t: 1.0 * spy + rng.normal(0, 0.015, len(dates)) for t in tick}
    rows = []
    for j, k in enumerate(rng.choice(np.arange(280, 680), 90, replace=False)):
        t = tick[j % 30]
        eight_k = j % 2 == 0
        if eight_k:
            rets[t][k:k + 5] += 0.008
        rows.append({"event_id": f"e{j}", "source": "sec_edgar", "stage": 7, "date": dates[k].date().isoformat(),
                     "title": f"{t} {'8-K' if eight_k else '10-Q'}", "url": "", "tickers": t, "relevant": True,
                     "program": "semiconductors" if eight_k else "clean_power", "program_name": "",
                     "extra": json.dumps({"form": "8-K" if eight_k else "10-Q"}), "amount_type": "",
                     "amount_usd": float("nan"), "sector_weights": json.dumps({"Information Technology": 1.0})})
    px = {"SPY": 400 * np.cumprod(1 + spy), **{t: 50 * np.cumprod(1 + r) for t, r in rets.items()}}
    prices = pd.DataFrame(px, index=[d.date().isoformat() for d in dates]).reset_index(names="date")
    return pd.DataFrame(rows), prices.melt(id_vars="date", var_name="ticker", value_name="close")


def test_evidence_finds_real_effect_and_rejects_noise():
    from radar import evidence as E
    events, prices = _synthetic_market()
    p = E.prepare(events)
    assert set(p["kind"]) == {"sec_8k", "sec_10q"}
    er = E.compute_returns(E.targets(p, set(prices["ticker"])), prices)
    ev = E.build_evidence(er).set_index("key")
    real, noise = ev.loc["semiconductors|sec_8k|direct"], ev.loc["clean_power|sec_10q|direct"]
    assert real["status"] == "proven" and real["direction"] == "+" and real["oos_hit"] >= 0.55
    assert 0.01 < real["post_mean"] < real["mean_car"] + 1e-9      # daraltma temkinli olmalı
    assert noise["status"] not in ("proven", "promising")
    # Olay → kanıt eşlemesi: özel anahtar yeterli veriye sahipse genel seviyenin kanıtı devralınmaz.
    idx = {k: dict(r, key=k) for k, r in ev.iterrows()}
    assert E.lookup(idx, "clean_power", "sec_10q", "direct")["key"] == "clean_power|sec_10q|direct"
    assert E.lookup(idx, "semiconductors", "sec_8k", "direct")["status"] == "proven"
    sig = E.annotate(events, ev.reset_index())
    assert set(sig.loc[sig["kind"] == "sec_8k", "status"]) == {"proven"}


def test_evidence_live_feedback_suspends():
    import pandas as pd
    from radar import evidence as E
    events, prices = _synthetic_market()
    er = E.compute_returns(E.targets(E.prepare(events), set(prices["ticker"])), prices)
    live = pd.DataFrame({"evidence_key": ["semiconductors|sec_8k|direct"] * 12, "hit": [0.0] * 10 + [1.0] * 2})
    ev = E.build_evidence(er, live).set_index("key")
    assert ev.loc["semiconductors|sec_8k|direct", "status"] == "suspended"


def test_benjamini_hochberg():
    import pandas as pd
    from radar.evidence import _bh
    q = _bh(pd.Series([0.01, 0.04, 0.03, 0.5, None]))
    assert abs(q.iloc[0] - 0.04) < 1e-9 and abs(q.iloc[1] - 0.0533333) < 1e-6 and q.iloc[3] == 0.5 and q.isna().iloc[4]


def test_telegram_gate_and_format():
    from datetime import date
    import pandas as pd
    from radar import store, telegram
    today = date.today().isoformat()
    base = {"date": today, "status": "proven", "effect": 0.03, "rank": 5, "n_clusters": 20, "url": "",
            "program": "semiconductors", "program_name": "Yarı iletken", "kind_name": "8-K", "role": "direct",
            "role_name": "Şirketin kendi hissesi", "evidence_key": "semiconductors|sec_8k|direct",
            "consistency": 0.7, "oos_hit": 0.6, "oos_n": 10, "q": 0.03, "status_name": "Kanıtlandı",
            "icon": "🟢", "amount_usd": None}
    sig = pd.DataFrame([
        {**base, "event_id": "a", "title": "Intel 8-K <CHIPS>", "affected": "INTC", "url": "https://x"},
        {**base, "event_id": "b", "title": "belirsiz", "affected": "F", "status": "unclear", "rank": 1},
        {**base, "event_id": "c", "title": "eski olay", "affected": "MU", "date": "2020-01-01"},
    ])
    msg = telegram.format_signal(sig.iloc[0].to_dict())
    assert "&lt;CHIPS&gt;" in msg and "INTC" in msg and "%3,0" in msg
    sent = []
    old_send, old_cred = telegram.send, telegram.credentials
    telegram.send = lambda text: sent.append(text) or True
    telegram.credentials = lambda: ("tok", "chat")
    try:
        assert telegram.notify(sig, {"a", "b", "c"}, "run1") == 1          # yalnızca yeni + kanıtlı + taze
        assert telegram.notify(sig, {"a"}, "run2") == 0                    # aynı olay iki kez gönderilmez
    finally:
        telegram.send, telegram.credentials = old_send, old_cred
    nt = store.read("notifications")
    assert list(nt["event_id"]) == ["a"] and nt["direction"].iloc[0] == "+"


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print("OK  ", name)
            except Exception as exc:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if fails else 0)
