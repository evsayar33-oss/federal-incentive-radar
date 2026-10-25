"""Grants.gov search2 + fetchOpportunity — anahtar gerekmez. Açık ve öngörülen hibe ilanları (NOFO)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

import pandas as pd

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.grants")


def _date(s: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except (TypeError, ValueError):
            continue
    return ""


def find_number(obj, keys=("estimatedFunding", "awardCeiling")) -> float | None:
    """Yanıt şeması değişse bile tutarı bulmak için özyinelemeli arama."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            try:
                if v not in (None, "", "none") and float(str(v).replace(",", "")) > 0:
                    return float(str(v).replace(",", ""))
            except ValueError:
                pass
        for v in obj.values():
            r = find_number(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_number(v, keys)
            if r:
                return r
    return None


def parse(hits: list[dict], keyword: str) -> list[dict]:
    rows = []
    for h in hits:
        status = (h.get("oppStatus") or "").lower()
        rows.append(schema.event(
            "grants_gov", h.get("id") or h.get("number"), 4, _date(h.get("openDate", "")) or date.today().isoformat(),
            h.get("title"), text=f"{h.get('title','')} ({keyword})",
            url=f"https://www.grants.gov/search-results-detail/{h.get('id')}",
            agency=h.get("agency") or h.get("agencyName") or h.get("agencyCode") or "",
            amount_type="nofo_estimated_funding",
            extra={"opp_id": h.get("id"), "number": h.get("number"), "status": status,
                   "close_date": _date(h.get("closeDate", "")), "cfda": h.get("cfdaList"), "keyword": keyword},
        ))
    return rows


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["grants_gov"]
    rows: list[dict] = []
    for kw in cfg["keywords"]:
        body = {"rows": cfg["rows"], "keyword": kw, "oppStatuses": "forecasted|posted"}
        try:
            data = http.post_json(cfg["search_url"], json=body)
        except Exception as exc:
            log.warning("Grants.gov '%s' hatası: %s", kw, exc)
            continue
        hits = ((data.get("data") or {}).get("oppHits")) or []
        rows.extend(r for r in parse(hits, kw) if r["date"] >= since.isoformat())
    df = schema.frame(rows)
    # Tutar için en fazla N ilan detayını çek.
    for i, row in df.head(cfg["max_detail_fetch"]).iterrows():
        try:
            oid = json.loads(row["extra"]).get("opp_id")
            detail = http.post_json(cfg["fetch_url"], json={"opportunityId": int(oid)})
            amt = find_number(detail)
            if amt:
                df.loc[i, "amount_usd"] = amt
        except Exception as exc:
            log.debug("Detay alınamadı %s: %s", row["event_id"], exc)
    return df
