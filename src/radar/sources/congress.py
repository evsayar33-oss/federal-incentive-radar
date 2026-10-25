"""Congress.gov API v3 — ücretsiz anahtar (CONGRESS_API_KEY). Tasarı durumu ve son eylemler."""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from .. import http, schema
from ..settings import env, sources

log = logging.getLogger("radar.congress")
TYPE_PATH = {"HR": "house-bill", "S": "senate-bill", "HJRES": "house-joint-resolution",
             "SJRES": "senate-joint-resolution"}


def ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def classify_action(text: str) -> tuple[int, str]:
    t = (text or "").lower()
    if "became public law" in t or "signed by president" in t:
        return 2, "law"
    if "passed senate" in t or "passed house" in t or "agreed to" in t or "passed/agreed" in t:
        return 1, "passed_chamber"
    if "reported" in t or "ordered to be reported" in t:
        return 1, "reported"
    return 1, "introduced"


def parse(bills: list[dict]) -> list[dict]:
    rows = []
    for b in bills:
        btype = (b.get("type") or "").upper()
        if btype not in TYPE_PATH:
            continue
        la = b.get("latestAction") or {}
        stage, status = classify_action(la.get("text", ""))
        congress = int(b.get("congress") or 0)
        number = b.get("number")
        url = f"https://www.congress.gov/bill/{ordinal(congress)}-congress/{TYPE_PATH[btype]}/{number}"
        rows.append(schema.event(
            "congress", f"{congress}-{btype}-{number}-{status}", stage,
            la.get("actionDate") or (b.get("updateDate") or "")[:10], b.get("title"),
            text=la.get("text", ""), url=url, agency="U.S. Congress",
            extra={"status": status, "bill": f"{btype} {number}", "congress": congress,
                   "chamber": b.get("originChamber")},
        ))
    return rows


def fetch(since: date) -> pd.DataFrame:
    key = env("CONGRESS_API_KEY")
    if not key:
        log.info("CONGRESS_API_KEY yok, Congress.gov atlandı")
        return schema.frame([])
    cfg = sources()["congress"]
    rows: list[dict] = []
    for page in range(cfg["max_pages"]):
        params = {"api_key": key, "format": "json", "limit": cfg["limit"], "offset": page * cfg["limit"],
                  "fromDateTime": f"{since.isoformat()}T00:00:00Z", "sort": "updateDate+desc"}
        data = http.get_json(cfg["url"], params=params)
        bills = data.get("bills") or []
        rows.extend(parse(bills))
        if len(bills) < cfg["limit"]:
            break
    # Tasarılar çok sayıda. İlgisiz olanlar sınıflandırma adımında elenir.
    return schema.frame(rows)
