"""GDELT DOC 2.0 — anahtar gerekmez. Yalnızca erken uyarı (aşama 0) ve dikkat sinyali."""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.gdelt")


def parse(articles: list[dict], query: str) -> list[dict]:
    rows = []
    for a in articles:
        if (a.get("language") or "English") != "English":
            continue
        sd = a.get("seendate") or ""
        d = f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) >= 8 else ""
        rows.append(schema.event(
            "gdelt", a.get("url"), 0, d, a.get("title"), text=a.get("title", ""),
            url=a.get("url"), agency=a.get("domain", ""), extra={"query": query, "domain": a.get("domain")},
        ))
    return rows


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["gdelt"]
    rows: list[dict] = []
    for q in cfg["queries"]:
        params = {"query": f"{q} sourcecountry:US", "mode": "artlist", "format": "json",
                  "maxrecords": cfg["max_records"], "timespan": cfg["timespan"], "sort": "datedesc"}
        try:
            r = http.request("GET", cfg["url"], params=params, retries=2)
            data = r.json() if r.text.strip().startswith("{") else {}
        except Exception as exc:
            log.warning("GDELT sorgu hatası: %s", exc)
            continue
        rows.extend(parse(data.get("articles") or [], q))
    return schema.frame(rows)
