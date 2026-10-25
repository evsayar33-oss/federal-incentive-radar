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
