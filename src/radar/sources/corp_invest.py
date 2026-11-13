"""Büyük şirket yatırımları (basın haberleri): Google News RSS, anahtarsız. Geçmiş için after:/before: pencereleri.

Başlıktan şirket adı, tutar ve kategori çıkarılır. Şirket yalnızca ETF/SEC evrenindeki bir hisseyle
yalnızca kesin (exact/override) eşleşirse tutulur; belirsiz eşleşmeler atılır (yanlış hisse sinyalden kötüdür).
Olaylar ana 'events' tablosuna girmez; 'invest_events' tablosunda ayrı izlenir ve ayrı kanıtlanır.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime

import pandas as pd

from .. import http, schema, store
from ..entity import Matcher
from ..settings import sources

log = logging.getLogger("radar.corp_invest")
GNEWS = "https://news.google.com/rss/search"

AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|bn|b|million|mn|m)\b", re.I)
VERB_RE = re.compile(
    r"^(?P<co>.+?)\s+(?:announces?|announced|to invest|will invest|invests?|investing|plans?|unveils?|commits?|"
    r"pledges?|breaks ground|expands?|to build|will build|to spend|boosts?|raises? capex|earmarks?)\b", re.I)
CATEGORIES = {   # sıra önemli: ilk eşleşen kazanır
    "datacenter_ai": ("data center", "data centre", "ai infrastructure", "gigawatt", "gpu", "supercomputer", "cloud region"),
    "chips": ("semiconductor", "chip", "fab ", "foundry", "wafer"),
    "energy": ("solar", "wind", "battery", "nuclear", "hydrogen", "grid", "refinery", "pipeline", "lng", "power plant"),
    "plant": ("plant", "factory", "manufacturing", "facility", "assembly", "production line", "gigafactory"),
    "rnd": ("research", "r&d", "lab", "innovation"),
}


def parse_amount(text: str) -> float | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    return v * (1e9 if unit in ("billion", "bn", "b") else 1e6)


def category(text: str) -> str:
    t = (text or "").lower()
    for name, words in CATEGORIES.items():
        if any(w in t for w in words):
            return name
    return "other"


def split_title(title: str) -> tuple[str, str]:
    """Google News başlığı 'Başlık - Yayıncı' biçimindedir."""
    head, _, publisher = (title or "").rpartition(" - ")
    return (head or title or "").strip(), publisher.strip()


def parse_rss(xml_text: str, query: str, universe_matcher: Matcher | None, min_usd: float) -> list[dict]:
    rows = []
    for it in ET.fromstring(xml_text).iter("item"):
        title, publisher = split_title(it.findtext("title") or "")
        amount = parse_amount(title)
        m = VERB_RE.match(title)
        if not amount or amount < min_usd or not m:
            continue
        company = m.group("co").strip(" ,:;\"'")
        ticker, method = "", "none"
        if universe_matcher is not None:
            t, _score, method = universe_matcher.match(company)
            ticker = t if method in ("exact", "override") else ""
        if not ticker:
            continue
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published_utc = published.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
        link = (it.findtext("link") or "").strip()
        rows.append(schema.event(
            "corp_invest", link or title, 7, published_utc.date().isoformat(), title, text=title, url=link,
            agency=publisher, amount_usd=amount, amount_type="private_investment", recipient=company,
            tickers=ticker, known_at=published_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            extra={"category": category(title), "query": query, "match": method, "kind": "corp_invest"}))
    return rows


def query_hash() -> str:
    import hashlib
    qs = "|".join(sorted(sources()["corp_invest"]["queries"]))
    return hashlib.md5(qs.encode()).hexdigest()[:10]


def _matcher() -> Matcher | None:
    uni = store.read("universe")
    return Matcher(uni) if not uni.empty else None


def fetch(since: date, until: date | None = None) -> pd.DataFrame:
    cfg = sources()["corp_invest"]
    matcher = _matcher()
    if matcher is None:
        log.info("Evren tablosu yok; büyük yatırım kaynağı atlandı (haftalık çalışma evreni oluşturur).")
        return schema.frame([])
    rows: list[dict] = []
    until = until or date.today()
    failed = 0
    for q in cfg["queries"]:
        # +1 gün: before: sınırı dışlayıcıdır
        query = f"{q} after:{since.isoformat()} before:{(until + timedelta(days=1)).isoformat()}"
        try:
            r = http.request("GET", GNEWS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                             browser=True, retries=1, timeout=30)
            rows.extend(parse_rss(r.text, q, matcher, float(cfg["min_usd"])))
        except Exception as exc:
            failed += 1
            log.warning("Büyük yatırım sorgusu başarısız (%s): %s", q, str(exc)[:160])
    if failed == len(cfg["queries"]):   # sessizce "0 satır" demesin; durum raporunda hata görünsün
        raise RuntimeError("Tüm büyük yatırım sorguları başarısız")
    return schema.frame(rows)
