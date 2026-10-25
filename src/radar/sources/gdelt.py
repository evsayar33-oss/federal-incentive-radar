"""Haber sinyali (aşama 0): GDELT DOC 2.0, yedek olarak Google News RSS. İkisi de anahtarsız.

GDELT, GitHub'ın paylaşılan IP'lerini sık sık 429 ile reddeder. İlk 429'da GDELT bırakılır
ve aynı sorgular Google News RSS ile çalıştırılır. Haber yalnızca erken uyarıdır, skora etkisi düşüktür.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.gdelt")
GNEWS = "https://news.google.com/rss/search"


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


def parse_rss(xml_text: str, query: str) -> list[dict]:
    rows = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            d = date.today().isoformat()
        rows.append(schema.event("gdelt", link or title, 0, d, title, text=title, url=link,
                                 agency=(src.text if src is not None else "") or "",
                                 extra={"query": query, "via": "google_news_rss"}))
    return rows


def _gdelt(q: str, cfg: dict) -> list[dict]:
    params = {"query": f"{q} sourcecountry:US", "mode": "artlist", "format": "json",
              "maxrecords": cfg["max_records"], "timespan": cfg["timespan"], "sort": "datedesc"}
    r = http.request("GET", cfg["url"], params=params, retries=0, timeout=30)
    data = r.json() if r.text.strip().startswith("{") else {}
    return parse(data.get("articles") or [], q)


def _gnews(q: str) -> list[dict]:
    r = http.request("GET", GNEWS, params={"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
                     browser=True, retries=1, timeout=30)
    return parse_rss(r.text, q)


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["gdelt"]
    rows: list[dict] = []
    gdelt_ok = True
    for q in cfg["queries"]:
        if gdelt_ok:
            try:
                rows.extend(_gdelt(q, cfg))
                continue
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    log.warning("GDELT 429 (paylaşılan IP sınırı); bu çalışmada Google News RSS kullanılacak")
                    gdelt_ok = False
                else:
                    log.warning("GDELT hatası: %s", exc)
            except Exception as exc:
                log.warning("GDELT hatası: %s", exc)
                gdelt_ok = False
        try:
            rows.extend(_gnews(q))
        except Exception as exc:
            log.warning("Google News RSS hatası: %s", str(exc)[:200])
    return schema.frame(rows)
