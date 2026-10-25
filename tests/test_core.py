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
    sig2 = sig.assign(status="no_effect", event_id=["x", "y", "z"])
    telegram.send, telegram.credentials = (lambda t: True), (lambda: ("tok", "chat"))
    try:
        assert telegram.notify(sig2, {"x", "y", "z"}, "run3") == 0          # etkisiz haber asla gönderilmez
    finally:
        telegram.send, telegram.credentials = old_send, old_cred


def test_health_message():
    from radar import telegram
    bad = telegram.health_message("run", {"sec_edgar": {"ok": False, "error": "429 <x>"},
                                          "congress": {"ok": True, "rows": 5}, "events_new": 3}, True)
    assert "🔴" in bad and "sec_edgar" in bad and "&lt;x&gt;" in bad and "congress (5)" in bad
    assert "🟢" in telegram.health_message("run", {"congress": {"ok": True}}, True)


def test_autolabel_finds_errors():
    import pandas as pd
    from radar import autolabel as A
    ev = pd.DataFrame([
        {"event_id": "1", "source": "grants_gov", "title": "Young Faculty Award (YFA) 2027", "text": "",
         "agency": "DARPA", "program": "semiconductors", "relevant": True, "method": "rule", "recipient": "", "tickers": ""},
        {"event_id": "2", "source": "usaspending", "title": "Department of Energy: CIRBA SOLUTIONS, LLC",
         "text": "lithium battery critical minerals recycling", "agency": "Department of Energy",
         "program": "infrastructure", "relevant": True, "method": "rule", "recipient": "CIRBA SOLUTIONS, LLC", "tickers": ""},
        {"event_id": "3", "source": "federal_register", "title": "CHIPS Incentives Program NOFO", "text": "",
         "agency": "Commerce Department", "program": "semiconductors", "relevant": True, "method": "rule",
         "recipient": "", "tickers": ""},
        {"event_id": "4", "source": "usaspending", "title": "x", "text": "", "agency": "", "program": "other",
         "relevant": True, "method": "rule", "recipient": "UNIVERSITY OF ARIZONA", "tickers": "UA"},
    ])
    review = pd.DataFrame([{"recipient": "LUMEN TECHNOLOGIES GOVERNMENT SOLUTIONS, INC.",
                            "recipient_norm": "lumen technologies government solutions", "candidate": "LUMN",
                            "score": 86, "event_id": "9"}])
    uni = pd.DataFrame({"ticker": ["LUMN"], "name": ["Lumen Technologies Inc"]})
    labels, issues = A.audit(ev, review, uni, pd.DataFrame(columns=["kind", "event_id", "recipient_norm"]))
    got = {(x["kind"], x.get("event_id") or x.get("recipient_norm")): x.get("label", x.get("ticker")) for x in labels}
    assert got[("program", "1")] == "irrelevant"                 # akademik ödül, piyasa haberi değil
    assert got[("program", "2")] == "critical_minerals"          # kurum + metin → doğru program
    assert ("program", "3") not in got                            # doğru kayda dokunulmaz
    assert got[("entity", "university of arizona")] == "NONE"   # üniversite borsada değil
    assert got[("entity", "lumen technologies government solutions")] == "LUMN"   # şirket eki → şirket
    # Kullanıcı etiketi otomatik etiketi bastırır
    user = pd.DataFrame([{"kind": "program", "event_id": "1", "recipient_norm": ""}])
    labels2, _ = A.audit(ev, None, None, user)
    assert ("program", "1") not in {(x["kind"], x.get("event_id")) for x in labels2}


def test_basket_quality_filter():
    import pandas as pd
    from radar.evidence import BasketBook
    p = pd.DataFrame({"program": ["critical_minerals"] * 4, "kind": ["sec_10k", "sec_8k", "sec_10k", "award_grant"],
                      "d": pd.to_datetime(["2026-01-01"] * 4), "tick_list": [["BXP"], ["MP"], ["ALB"], ["CAT"]],
                      "strong": [False, True, False, True]})
    b = BasketBook(p, {"BXP": "Real Estate", "MP": "Materials", "CAT": "Industrials"})
    assert set(b.basket("critical_minerals", pd.Timestamp("2026-02-01"), 10)) == {"MP", "CAT"}


def test_permanent_link_checks():
    from radar.evidence import link_plausible, strong_link
    from radar.sources.sec_edgar import sic_sector
    assert sic_sector(6798) == "Real Estate" and sic_sector(3674) == "Information Technology"
    assert sic_sector(2834) == "Health Care" and sic_sector(1311) == "Energy" and sic_sector(6770) == "Financials"
    assert link_plausible("Real Estate", "critical_minerals") is False      # BRX gibi
    assert link_plausible("Materials", "critical_minerals") is True
    assert link_plausible(None, "critical_minerals") is None
    assert strong_link("sec_8k", {"items": ["1.01", "9.01"]})                # önemli sözleşme
    assert not strong_link("sec_8k", {"items": ["2.02", "9.01"]})            # rutin finansal sonuç
    assert not strong_link("sec_8k", {"items": ["8.01"], "context_unverified": True})
    assert not strong_link("sec_10k", {}) and strong_link("award_grant", {})


def test_fallback_parsers_and_switch():
    import pandas as pd
    from radar import run, schema
    from radar.sources import fallbacks as F
    assert F.parse_bill_package("BILLS-119hr10879ih") == (119, "HR", "10879", "introduced")
    assert F.parse_bill_package("BILLS-119s25es") == (119, "S", "25", "passed_chamber")
    assert F.parse_bill_package("BILLS-119hjres7rh")[3] == "reported"
    # Birincil çökerse yedek devreye girer ve durum "yedek" olarak raporlanır.
    class Broken:
        @staticmethod
        def fetch(since):
            raise RuntimeError("API kapalı")
    old = F.REGISTRY["grants_gov"]
    F.REGISTRY["grants_gov"] = lambda since: schema.frame([schema.event("grants_gov", "1", 4, "2026-10-01", "NOFO")])
    import radar.sources as S
    real = S.grants_gov
    S.grants_gov = Broken
    st = {}
    try:
        df = run.ingest("midday", {}, st, only=["grants_gov"])
    finally:
        F.REGISTRY["grants_gov"], S.grants_gov = old, real
    assert st["grants_gov"]["via"] == "yedek" and st["grants_gov"]["ok"] and len(df) == 1
    from radar import telegram
    msg = telegram.health_message("r", st, True)
    assert "♻️" in msg and "🔴" in msg


def test_history_windows_resume_and_known_at():
    import pandas as pd
    from datetime import date
    from radar import history, schema, store
    w = history.windows(date(2024, 1, 1), date(2024, 3, 31), 31)
    assert w[0][0] == date(2024, 1, 1) and w[-1][1] == date(2024, 3, 31) and all(a <= b for a, b in w)
    calls = []

    def fake(src, a, b):
        calls.append((src, a))
        return schema.frame([schema.event(src, f"{src}-{a}", 5, a.isoformat(), "Energy grant award",
                                          text="grid resilience grant", amount_usd=5e7, amount_type="obligation",
                                          recipient="CITY OF PHOENIX")])
    old = history._fetch
    history._fetch = fake
    try:
        s1 = history.run(years=0.2, sources=["usaspending"])
        n1 = len(calls)
        s2 = history.run(years=0.2, sources=["usaspending"])      # ikinci çalışma: yeni pencere yok
    finally:
        history._fetch = old
    assert s1["windows_done"] == n1 > 0 and s2["windows_done"] == 0 and len(calls) == n1
    ev = store.read("events")
    h = ev[ev["event_id"].str.startswith("usaspending:")]
    assert (h["known_at"].astype(str).str[:10] == h["date"].astype(str).str[:10]).all()   # olay tarihinde bilinirdi


def test_telegram_backtest_note():
    from radar import telegram
    base = {"status": "proven", "effect": 0.03, "n_clusters": 20, "title": "t", "url": "", "program": "x",
            "program_name": "P", "kind_name": "K", "status_name": "Kanıtlandı", "role": "direct", "role_name": "R", "affected": "A",
            "consistency": 0.7, "oos_hit": 0.6, "oos_n": 10, "q": 0.03, "amount_usd": None, "sector": ""}
    assert "Canlı doğrulama sürüyor (0/10" in telegram.format_signal({**base, "live_n": 0, "live_hit": float("nan")})
    assert "Canlı doğrulandı" in telegram.format_signal({**base, "live_n": 14, "live_hit": 0.64})


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
