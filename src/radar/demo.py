"""İnternetsiz demo: gerçekçi sahte veriyle tüm hattı (sınıflandırma → eşleştirme → skor →
defter → değerlendirme → kalibrasyon → olay çalışması → model eğitimi) uçtan uca çalıştırır.

Amaç: kurulum testi, CI testi ve arayüz önizlemesi. Üretilen veriler GERÇEK DEĞİLDİR.
"""
from __future__ import annotations

import json
import logging
import random
import shutil

import numpy as np
import pandas as pd

from . import learn, run, schema, store
from .settings import SECTORS, programs, sector_etfs, sources, store_dir

log = logging.getLogger("radar.demo")

UNIVERSE = [
    ("INTC", "Intel Corp", "Information Technology", 0.30), ("MU", "Micron Technology Inc", "Information Technology", 0.25),
    ("TXN", "Texas Instruments Inc", "Information Technology", 0.35), ("GFS", "GlobalFoundries Inc", "Information Technology", 0.0),
    ("FSLR", "First Solar Inc", "Information Technology", 0.05), ("ENPH", "Enphase Energy Inc", "Information Technology", 0.03),
    ("NEE", "NextEra Energy Inc", "Utilities", 0.30), ("DUK", "Duke Energy Corp", "Utilities", 0.20),
    ("CEG", "Constellation Energy Corp", "Utilities", 0.15), ("GM", "General Motors Co", "Consumer Discretionary", 0.12),
    ("F", "Ford Motor Co", "Consumer Discretionary", 0.10), ("TSLA", "Tesla Inc", "Consumer Discretionary", 1.5),
    ("ALB", "Albemarle Corp", "Materials", 0.03), ("MP", "MP Materials Corp", "Materials", 0.0),
    ("FCX", "Freeport-McMoRan Inc", "Materials", 0.12), ("CAT", "Caterpillar Inc", "Industrials", 0.40),
    ("VMC", "Vulcan Materials Co", "Materials", 0.08), ("PWR", "Quanta Services Inc", "Industrials", 0.08),
    ("GD", "General Dynamics Corp", "Industrials", 0.15), ("HII", "Huntington Ingalls Industries Inc", "Industrials", 0.02),
    ("PLUG", "Plug Power Inc", "Industrials", 0.0), ("APD", "Air Products and Chemicals Inc", "Materials", 0.12),
    ("OXY", "Occidental Petroleum Corp", "Energy", 0.10), ("XOM", "Exxon Mobil Corp", "Energy", 0.9),
    ("ADM", "Archer-Daniels-Midland Co", "Consumer Staples", 0.06), ("PFE", "Pfizer Inc", "Health Care", 0.3),
    ("MRNA", "Moderna Inc", "Health Care", 0.02), ("T", "AT&T Inc", "Communication Services", 0.25),
    ("CSCO", "Cisco Systems Inc", "Information Technology", 0.4), ("NVDA", "NVIDIA Corp", "Information Technology", 7.0),
]
RECIPIENTS = [  # (USAspending alıcı adı, beklenen ticker veya "", program)
    ("INTEL CORPORATION", "INTC", "semiconductors"), ("MICRON TECHNOLOGY, INC.", "MU", "semiconductors"),
    ("GLOBALFOUNDRIES U.S. INC.", "GFS", "semiconductors"), ("GENERAL MOTORS LLC", "GM", "ev_auto"),
    ("FORD MOTOR COMPANY", "F", "ev_auto"), ("ALBEMARLE U.S., INC.", "ALB", "critical_minerals"),
    ("MP MATERIALS CORP.", "MP", "critical_minerals"), ("PLUG POWER INC", "PLUG", "hydrogen"),
    ("AIR PRODUCTS AND CHEMICALS, INC.", "APD", "hydrogen"), ("DUKE ENERGY CAROLINAS, LLC", "", "grid"),
    ("CONSTELLATION ENERGY GENERATION LLC", "CEG", "nuclear"), ("HUNTINGTON INGALLS INCORPORATED", "HII", "defense_industrial"),
    ("CITY OF PHOENIX", "", "infrastructure"), ("STATE OF TEXAS DEPARTMENT OF TRANSPORTATION", "", "infrastructure"),
    ("MODERNA US, INC.", "MRNA", "health_bio"), ("OCCIDENTAL PETROLEUM CORPORATION", "OXY", "carbon_management"),
]
FR_TITLES = [
    ("Notice of Funding Opportunity: Grid Resilience Utility and Industry Grants", "NOTICE", "Energy Department"),
    ("Section 45X Advanced Manufacturing Production Credit; Final Regulations", "RULE", "Internal Revenue Service"),
    ("Section 45V Credit for Production of Clean Hydrogen; Proposed Regulations", "PRORULE", "Internal Revenue Service"),
    ("CHIPS Incentives Program—Commercial Fabrication Facilities Notice of Funding Opportunity", "NOTICE", "Commerce Department"),
    ("Defense Production Act Title III: Critical Minerals Processing Awards", "NOTICE", "Defense Department"),
    ("Bridge Investment Program Notice of Funding Opportunity", "NOTICE", "Transportation Department"),
    ("Broadband Equity, Access, and Deployment Program Policy Notice", "NOTICE", "Commerce Department"),
    ("Clean Electricity Investment Credit (48E) Energy Community Guidance", "RULE", "Internal Revenue Service"),
    ("Advanced Reactor Demonstration Program; HALEU Availability", "NOTICE", "Energy Department"),
    ("Section 45Z Clean Fuel Production Credit Guidance", "PRORULE", "Internal Revenue Service"),
    ("Airworthiness Directives; The Boeing Company Airplanes", "RULE", "Federal Aviation Administration"),
    ("Fisheries of the Exclusive Economic Zone Off Alaska; Pollock", "RULE", "Commerce Department"),
    ("Agency Information Collection Activities; Comment Request", "NOTICE", "Labor Department"),
]
BILLS = [
    ("Critical Minerals Supply Chain Resilience Act", "critical_minerals"),
    ("To amend the Internal Revenue Code of 1986 to extend the clean electricity production credit (45Y)", "clean_power"),
    ("Semiconductor Manufacturing Incentive Extension Act (48D)", "semiconductors"),
    ("Shipbuilding and Submarine Industrial Base Act", "defense_industrial"),
    ("To designate the facility of the United States Postal Service as the John Smith Post Office", None),
    ("A resolution recognizing National Pickle Day", None),
]
SEC_PHRASES = [('"CHIPS Act"', ["INTC", "MU", "TXN", "GFS"]), ('"Section 45X"', ["FSLR", "ENPH"]),
               ('"Loan Programs Office"', ["PLUG", "GM"]), ('"Defense Production Act"', ["MP", "ALB"]),
               ('"Inflation Reduction Act"', ["NEE", "CEG", "DUK"])]


def _program_sectors(pid: str | None) -> dict:
    if pid is None:
        return {}
    return programs()["programs"][pid]["sectors"]


_RULES = None


def _guess_program(title: str) -> str | None:
    global _RULES
    if _RULES is None:
        from .classify import Classifier
        _RULES = Classifier(load_model=False)
    hits, _ = _RULES.rule_hits(title)
    best = max(hits, key=hits.get)
    return best if hits[best] > 0 else None


def build(days: int = 420, seed: int = 11) -> None:
    rnd, rng = random.Random(seed), np.random.default_rng(seed)
    d = store_dir()
    for p in d.iterdir():  # temiz başlangıç
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    uni = pd.DataFrame(UNIVERSE, columns=["ticker", "name", "sector", "sp500_weight"])
    store.write("universe", uni)

    # ---- olaylar
    by_day: dict[str, list[dict]] = {}
    intensity = pd.DataFrame(0.0, index=dates, columns=SECTORS)
    for dt in dates:
        ds = dt.date().isoformat()
        rows = []
        for _ in range(rng.poisson(2.0)):
            t, typ, ag = rnd.choice(FR_TITLES)
            rows.append(schema.event("federal_register", f"{ds}-{t}", 2 if typ == "PRESDOCU" else (4 if "Funding Opportunity" in t else 3),
                                     ds, t, text=t, agency=ag, url="https://www.federalregister.gov/",
                                     extra={"doc_type": {"RULE": "final_rule", "PRORULE": "proposed_rule"}.get(typ, "notice")}))
        if rng.random() < 0.5:
            t, pid = rnd.choice(BILLS)
            status = rnd.choice(["introduced", "introduced", "reported", "passed_chamber"] + (["law"] if rng.random() < 0.05 else []))
            rows.append(schema.event("congress", f"{ds}-{t}-{status}", 2 if status == "law" else 1, ds, t,
                                     text=status, agency="U.S. Congress", extra={"status": status}))
        for _ in range(rng.poisson(1.2)):
            name, _tick, pid = rnd.choice(RECIPIENTS)
            amt = float(np.exp(rng.normal(np.log(8e7), 1.1)))
            desc = {"semiconductors": "CHIPS semiconductor manufacturing incentive award",
                    "ev_auto": "domestic manufacturing conversion grant for electric vehicle production",
                    "critical_minerals": "Defense Production Act critical minerals processing",
                    "hydrogen": "clean hydrogen hub H2Hubs award", "grid": "grid resilience transmission line upgrade",
                    "nuclear": "advanced reactor HALEU fuel award", "defense_industrial": "submarine industrial base shipbuilding",
                    "infrastructure": "bridge investment program highway reconstruction",
                    "health_bio": "BARDA medical countermeasure vaccine manufacturing",
                    "carbon_management": "direct air capture DAC hub"}[pid]
            rows.append(schema.event("usaspending", f"grant-{ds}-{name}-{amt:.0f}", 5, ds, f"Awarding agency: {name}",
                                     text=desc, agency="Department of Energy" if pid in ("hydrogen", "grid", "nuclear", "carbon_management") else "Department of Commerce",
                                     amount_usd=amt, amount_type="obligation", recipient=name,
                                     url="https://www.usaspending.gov/"))
        if rng.random() < 0.6:
            phrase, ticks = rnd.choice(SEC_PHRASES)
            t = rnd.choice(ticks)
            rows.append(schema.event("sec_edgar", f"{ds}-{phrase}-{t}", 7, ds, f"{t} 8-K: {phrase.strip(chr(34))}",
                                     text=phrase, agency="SEC filing", tickers=t, recipient=t, url="https://www.sec.gov/"))
        for _ in range(rng.poisson(3)):
            t, pid = rnd.choice(BILLS)
            rows.append(schema.event("gdelt", f"{ds}-news-{rnd.random()}", 0, ds, f"Lawmakers debate {t}", text=t,
                                     agency="news.example.com"))
        by_day[ds] = rows
        for r in rows:
            pid = _guess_program(r["title"] + " " + r["text"])
            w = 3.0 if r["stage"] >= 5 else 1.0
            for s, v in _program_sectors(pid).items():
                intensity.loc[dt, s] += w * v

    # ---- fiyatlar: sektör getirisi, olay yoğunluğunun 20 günlük sürprizine zayıf bağlı (öğrenilebilir sinyal)
    surprise = (intensity.rolling(20).mean() - intensity.rolling(120, min_periods=30).mean()).fillna(0)
    surprise = surprise / (surprise.std().replace(0, 1))
    spy_ret = rng.normal(0.0003, 0.010, len(dates))
    px = {"SPY": 400 * np.cumprod(1 + spy_ret)}
    etf = sector_etfs()
    for s in SECTORS:
        r = 0.9 * spy_ret + rng.normal(0, 0.008, len(dates)) + 0.0006 * surprise[s].shift(1).fillna(0).to_numpy()
        px[etf[s]] = 100 * np.cumprod(1 + r)
    for t, _n, s, _w in UNIVERSE:
        r = 1.1 * (np.diff(np.log(px[etf[s]]), prepend=np.log(px[etf[s]][0]))) + rng.normal(0, 0.015, len(dates))
        px[t] = 50 * np.exp(np.cumsum(r))
    prices = pd.DataFrame(px, index=[x.date().isoformat() for x in dates]).reset_index(names="date")
    store.write("prices", prices.melt(id_vars="date", var_name="ticker", value_name="close"))

    # ---- makro ve ödeme verisi
    months = pd.date_range(end=pd.Timestamp.today(), periods=90, freq="MS")
    epu = 150 + 60 * np.sin(np.arange(90) / 7) + rng.normal(0, 20, 90)
    store.write("fred", pd.DataFrame({"date": [m.date().isoformat() for m in months], "value": epu, "series": "EPUFISCAL"}))
    cats = ["Dept of Energy (DOE)", "Federal Highway Administration", "Dept of Commerce (DOC)", "Dept of Defense (DoD) - misc",
            "Commodity Credit Corporation programs", "NIH Grants", "Housing and Urban Development programs"]
    out = [{"record_date": dt.date().isoformat(), "category": c,
            "amount_musd": float(abs(rng.normal(120 if "Energy" in c else 80, 30)) * (1.3 if (i > len(dates) - 20 and "Energy" in c) else 1))}
           for i, dt in enumerate(dates[-200:]) for c in cats]
    store.write("outlays", pd.DataFrame(out))

    # ---- kullanıcı etiketleri (öğrenen sınıflandırıcıyı tetiklemek için)
    lab_dir = d / "labels"
    lab_dir.mkdir(exist_ok=True)
    labels = []
    for ds in list(by_day)[:200]:
        for r in by_day[ds]:
            if r["source"] in ("federal_register", "congress") and len(labels) < 120:
                pid = _guess_program(r["title"])
                labels.append({"event_id": r["event_id"], "kind": "program", "label": pid or "irrelevant", "ts": ds})
    labels.append({"kind": "entity", "recipient_norm": "duke energy carolinas", "ticker": "DUK", "ts": "demo"})
    (lab_dir / "labels.jsonl").write_text("\n".join(json.dumps(x) for x in labels), encoding="utf-8")

    # ---- günlük kapanış döngüsü
    log.info("Demo: %d gün işleniyor...", len(dates))
    for i, dt in enumerate(dates):
        ds = dt.date().isoformat()
        raw = schema.frame(by_day[ds])
        status: dict = {}
        run.process(raw, f"{ds}T16:15_close", dt.date(), "close", status, write_ledger=True)
        if i % 20 == 19:
            learn.resolve_ledger(store.read("prices"))
        if i % 60 == 59:
            run.weekly_learning({})
    learn.resolve_ledger(store.read("prices"))
    st: dict = {}
    run.weekly_learning(st)
    store.append("runs", pd.DataFrame([{"run_id": "demo", "slot": "demo", "ok": True, "seconds": 0,
                                        "status": json.dumps(st, default=str)}]))
    ctx = store.load_json("context")
    ctx["demo"] = True
    store.save_json("context", ctx)
    log.info("Demo tamamlandı: %s", store_dir())
