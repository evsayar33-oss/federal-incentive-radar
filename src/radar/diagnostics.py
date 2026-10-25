"""Teşhis: her çalışmanın uyarılarını ve tablo özetlerini okunabilir JSON olarak yazar.

Dosya: store/last_run.json  (data dalında düz metin; tarayıcıdan veya arayüzden okunur)
"""
from __future__ import annotations

import logging

import pandas as pd

from . import store

_records: list[dict] = []


class _Collector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING and len(_records) < 300:
            _records.append({"level": record.levelname, "logger": record.name,
                             "msg": record.getMessage()[:400]})


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, _Collector) for h in root.handlers):
        root.addHandler(_Collector())


def _table_summary() -> dict:
    out: dict = {}
    for name in ["events", "universe", "prices", "outlays", "fred", "ledger", "scores_latest",
                 "company_exposure", "review_queue", "alerts", "event_study", "metrics"]:
        df = store.read(name)
        out[name] = int(len(df))
    ev = store.read("events")
    if not ev.empty:
        rel = ev["relevant"].astype(str).str.lower().isin(["true", "1"])
        out["events_by_source"] = {
            s: {"total": int(len(g)), "relevant": int(rel[g.index].sum()),
                "with_ticker": int((g["tickers"].fillna("").astype(str).replace("nan", "") != "").sum()),
                "with_amount": int(pd.to_numeric(g["amount_usd"], errors="coerce").gt(0).sum()),
                "last_date": str(g["date"].max())}
            for s, g in ev.groupby("source")}
        out["events_by_program"] = ev[rel]["program"].value_counts().head(20).to_dict()
        if "match_method" in ev.columns:
            out["match_methods"] = ev["match_method"].fillna("").astype(str).value_counts().to_dict()
    uni = store.read("universe")
    if not uni.empty and "sector" in uni.columns:
        out["universe_by_sector"] = uni["sector"].fillna("(yok)").value_counts().to_dict()
    pr = store.read("prices")
    if not pr.empty:
        out["prices"] = {"tickers": int(pr["ticker"].nunique()), "first": str(pr["date"].min()),
                         "last": str(pr["date"].max())}
    return out


def write(kind: str, status: dict) -> None:
    doc = store.load_json("last_run")
    doc[kind] = {"at": store.now_iso(), "status": status, "warnings": list(_records)}
    try:
        doc["tables"] = _table_summary()
    except Exception as exc:  # teşhis asla çalışmayı bozmasın
        doc["tables"] = {"error": str(exc)[:300]}
    store.save_json("last_run", doc)
    _records.clear()
