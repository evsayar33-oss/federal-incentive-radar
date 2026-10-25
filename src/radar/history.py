"""Geçmiş veri (backtest) yükleme: kanıt tablosunu geçmiş olaylarla doldurur.

Kaynaklar: Federal Register, USAspending, SEC EDGAR. Bu kaynakların olay tarihi gerçek yayın tarihidir.
Kullanılmayanlar ve nedeni:
  - Congress.gov : 'son eylem' bugünün durumunu verir, tasarının o günkü durumunu vermez (ileriye bakma yanlılığı).
  - Grants.gov   : yalnızca açık ilanları verir, geçmiş ilan yoktur.
  - GDELT        : geçmiş haber vermez.

Yanlılık önlemleri:
  - Olayın known_at değeri olay tarihidir (geriye dönük test sızıntısı olmaz).
  - Kanıt motoru her olayı yalnızca o tarihten ÖNCEKİ verilerle tahmin eder (ileriye dönük test).
  - Kanıt eşikleri değiştirilmez. Sistem testi geçerse geçer, geçemezse geçemez.
  - Hayatta kalma yanlılığı: bugün işlem görmeyen (iflas, birleşme) şirketlerin fiyatı yoktur; bu olaylar ölçülemez.
    Bu sınır 'history_report.json' dosyasına yazılır.
İlerleme 'history_progress' içinde saklanır. Kesintide kaldığı yerden devam eder.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

import pandas as pd

from . import store
from .settings import store_dir

log = logging.getLogger("radar.history")
SOURCES = ["federal_register", "usaspending", "sec_edgar"]


def windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    out, d = [], start
    while d <= end:
        e = min(d + timedelta(days=days - 1), end)
        out.append((d, e))
        d = e + timedelta(days=1)
    return out


def _fetch(source: str, a: date, b: date) -> pd.DataFrame:
    from .sources import federal_register, sec_edgar, usaspending
    if source == "federal_register":
        return federal_register.fetch(a, b, max_pages=4)
    if source == "usaspending":
        return usaspending.fetch(a, b, max_pages=3)
    return sec_edgar.fetch(a, b)


def _span(source: str) -> int:
    return {"federal_register": 31, "usaspending": 31, "sec_edgar": 92}[source]


def run(years: float = 5.0, budget_minutes: float = 100.0, sources: list[str] | None = None) -> dict:
    """Geçmiş pencereleri sırayla yükler. Süre bütçesi dolarsa durur; sonraki çalışma devam eder."""
    t0, end = time.time(), date.today()
    start = end - timedelta(days=int(365.25 * years))
    prog = store.read("history_progress")
    done = set(zip(prog["source"], prog["start"])) if not prog.empty else set()
    rows_new, summary = [], {"windows_done": 0, "events": 0, "stopped_early": False, "errors": []}
    for src in sources or SOURCES:
        for a, b in windows(start, end, _span(src)):
            if (src, a.isoformat()) in done:
                continue
            if time.time() - t0 > budget_minutes * 60:
                summary["stopped_early"] = True
                break
            try:
                df = _fetch(src, a, b)
            except Exception as exc:  # tek pencere hatası tümünü durdurmasın
                summary["errors"].append(f"{src} {a}: {str(exc)[:120]}")
                continue
            if df is not None and not df.empty:
                df = df.copy()
                df["known_at"] = df["date"]            # olay tarihinde bilinirdi
                rows_new.append(df)
                summary["events"] += len(df)
            rows_new.append(None)
            store.append("history_progress", pd.DataFrame([{"source": src, "start": a.isoformat(), "end": b.isoformat(),
                                                            "events": 0 if df is None else len(df)}]))
            summary["windows_done"] += 1
            if len([x for x in rows_new if x is not None]) >= 12:   # bellek ve ara kayıt: toplu işle
                _ingest([x for x in rows_new if x is not None])
                rows_new = []
    _ingest([x for x in rows_new if x is not None])
    return summary


def _ingest(frames: list[pd.DataFrame]) -> None:
    if not frames:
        return
    from . import run as R
    raw = pd.concat(frames, ignore_index=True).drop_duplicates("event_id", keep="last")
    labels = R.load_labels()
    clf = R.Classifier()
    cls, review = R.classify_and_match(raw, labels, clf)
    if R.auto_fix(cls, review, store.read("universe"), {}):
        cls, review = R.classify_and_match(raw, R.load_labels(), clf)
    if not review.empty:
        store.upsert("review_queue", review, "recipient_norm")
    # Eski ve ilgisiz kayıtlar kanıt için işe yaramaz; depoyu şişirmesin (canlı sistem de 120 gün sonra atar).
    rel = cls["relevant"].astype(str).str.lower().isin(["true", "1"])
    old = pd.to_datetime(cls["date"], errors="coerce") < pd.Timestamp.today() - pd.Timedelta(days=120)
    cls = cls[rel | ~old]
    store.upsert("events", cls, "event_id", keep="first")   # canlı kayıt varsa onu koru
    log.info("Geçmiş: %d olay işlendi", len(cls))


def report(status: dict) -> None:
    ev = store.read("events")
    pr = store.read("prices")
    have = set(pr["ticker"].astype(str)) if not pr.empty else set()
    tk = set()
    if not ev.empty:
        for v in ev["tickers"].fillna("").astype(str):
            tk |= {t.strip() for t in v.split(",") if t.strip()}
    rep = {"asof": date.today().isoformat(), "events": int(len(ev)), "tickers_in_events": len(tk),
           "tickers_with_prices": len(tk & have), "tickers_without_prices": len(tk - have),
           "survivorship_note": "Fiyatı olmayan şirketler (iflas, birleşme, ticker değişimi) ölçülemez; "
                                "etkiler hafif iyimser olabilir.", **status}
    (store_dir() / "history_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
