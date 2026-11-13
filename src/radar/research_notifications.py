"""Rate-limited, explicitly UNVERIFIED research Telegram notices.

Never shares tokens, never issues orders and never calls a headline an alpha.
Delivery records are written only when Telegram confirms the send request.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import pandas as pd

from . import store, telegram


def _safe(value: object, limit: int = 110) -> str:
    return html.escape(str(value or "")[:limit])


def notify_relationships(rows: pd.DataFrame, *, limit: int = 4) -> int:
    if rows is None or rows.empty or not telegram.enabled():
        return 0
    history = store.read("economic_notifications")
    notified = set(history.event_id.astype(str)) if not history.empty and "event_id" in history else set()
    fresh = rows[~rows.event_id.astype(str).isin(notified)].head(max(0, min(int(limit), 4)))
    completed = []
    now = datetime.now(timezone.utc).isoformat()
    for item in fresh.to_dict("records"):
        src = str(item.get("source_ticker") or "").strip()
        tgt = str(item.get("target_ticker") or "").strip()
        if src.lower() == "nan": src = ""
        if tgt.lower() == "nan": tgt = ""
        # Avoid an alert from an unnamed/unresolved private counterparty pair.
        if not (src or tgt):
            continue
        message = (
            "🔬 <b>Ekonomik ilişki · DOĞRULANMAMIŞ HABER</b>\n"
            f"{_safe(item.get('source_company'))} ({_safe(item.get('source_role'))}: {_safe(src or '—')})\n"
            f"→ {_safe(item.get('target_company'))} ({_safe(item.get('target_role'))}: {_safe(tgt or '—')})\n"
            f"Tür: {_safe(item.get('relation'))}\n"
            f"İlk gözlem: {_safe(item.get('observed_at_utc'), 32)}\n"
            "Kaynak doğrulaması: BEKLENİYOR. AL/SAT SİNYALİ DEĞİL.\n"
            f"{_safe(item.get('source_url'), 500)}"
        )
        if telegram.send(message):
            completed.append({"event_id": str(item["event_id"]), "sent_at_utc": now,
                              "status": "SECONDARY_SOURCE_RESEARCH_ONLY"})
    if completed:
        store.append("economic_notifications", pd.DataFrame(completed))
    return len(completed)


def warn_data_quality(kind: str, reason: str, context: dict | None = None) -> bool:
    """One warning per cause per UTC day; safe skips are not outages."""
    if not telegram.enabled() or not kind or not reason:
        return False
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    key = f"{day}|{kind}|{reason}"
    old = store.load_json("research_warning_state")
    sent = [str(k) for k in old.get("sent", []) if str(k).startswith(day)]
    if key in sent:
        return False
    details = context or {}
    report = ", ".join(f"{_safe(k, 40)}: {_safe(v, 70)}"
                       for k, v in list(details.items())[:5])
    message = (f"⚠️ <b>Radar araştırma sağlığı</b>\n"
               f"Bileşen: {_safe(kind)}\nNeden: {_safe(reason)}\n"
               f"{report}\nYeni araştırma fırsatları veri güvenilirliği nedeniyle engellenebilir. "
               "Broker emri YOK.")
    if not telegram.send(message):
        return False
    sent.append(key)
    store.save_json("research_warning_state", {"sent": sent[-40:],
                                               "updated_at_utc": now.isoformat()})
    return True
