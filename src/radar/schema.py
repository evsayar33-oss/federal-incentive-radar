"""Ortak olay şeması. Tüm kaynaklar bu biçimde satır üretir."""
from __future__ import annotations

import hashlib
import json
import math

import pandas as pd

EVENT_COLUMNS = [
    "event_id", "source", "stage", "date", "known_at", "title", "text", "url",
    "agency", "amount_usd", "amount_type", "recipient", "tickers", "extra",
]


def make_id(source: str, native_id: str) -> str:
    return f"{source}:{hashlib.sha1(str(native_id).encode()).hexdigest()[:16]}"


def event(source: str, native_id: str, stage: int, date: str, title: str, *, text: str = "",
          url: str = "", agency: str = "", amount_usd: float | None = None,
          amount_type: str = "", recipient: str = "", tickers: str = "",
          extra: dict | None = None, known_at: str = "") -> dict:
    return {
        "event_id": make_id(source, native_id),
        "source": source,
        "stage": int(stage),
        "date": str(date)[:10],
        "known_at": known_at,
        "title": (title or "").strip()[:500],
        "text": (text or "").strip()[:4000],
        "url": url or "",
        "agency": agency or "",
        "amount_usd": float(amount_usd) if amount_usd not in (None, "") and not _isnan(amount_usd) else math.nan,
        "amount_type": amount_type,
        "recipient": recipient or "",
        "tickers": tickers or "",
        "extra": json.dumps(extra or {}, ensure_ascii=False, default=str),
    }


def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


def frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    df = pd.DataFrame(rows)
    for c in EVENT_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[EVENT_COLUMNS].drop_duplicates("event_id", keep="last")
