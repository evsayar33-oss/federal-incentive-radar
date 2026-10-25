"""SEC EDGAR tam metin arama + company_tickers.json — anahtar gerekmez, User-Agent zorunlu.

Şirket bildirimlerinde teşvik geçişlerini yakalar. Ticker bilgisi doğrudan sonuçtan gelir,
bu yüzden ad eşleştirmesi gerekmez (en güvenilir şirket bağı budur).
"""
from __future__ import annotations

import logging
import re
from datetime import date

import pandas as pd

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.sec")
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})(?:,\s*[A-Z0-9.\-]+)*\)\s*\(CIK", re.I)


def tickers_from_names(names: list[str]) -> list[str]:
    out = []
    for n in names or []:
        m = TICKER_RE.search(n)
        if m:
            out.append(m.group(1).upper())
    return out


def parse(hits: list[dict], phrase: str) -> list[dict]:
    rows = []
    for h in hits:
        src = h.get("_source") or {}
        _id = h.get("_id") or ""
        adsh, _, fname = _id.partition(":")
        ciks = src.get("ciks") or []
        cik = str(int(ciks[0])) if ciks else ""
        names = src.get("display_names") or []
        tick = tickers_from_names(names)
        form = src.get("form") or src.get("file_type") or ""
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
               if cik and adsh else "")
        company = re.sub(r"\s*\(.*$", "", names[0]) if names else ""
        rows.append(schema.event(
            "sec_edgar", f"{adsh}-{phrase}", 7, src.get("file_date") or "",
            f"{company} {form}: {phrase.strip(chr(34))}",
            text=f"{phrase} {src.get('file_description') or ''} items={src.get('items') or ''}",
            url=url, agency="SEC filing (Department of Energy)" if ("Energy" in phrase or "Loan Programs" in phrase) else "SEC filing",
            recipient=company, tickers=",".join(sorted(set(tick))),
            extra={"form": form, "cik": cik, "adsh": adsh, "phrase": phrase, "items": src.get("items")},
        ))
    return rows


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["sec_edgar"]
    rows: list[dict] = []
    for phrase in cfg["phrases"]:
        params = {"q": phrase, "dateRange": "custom", "startdt": since.isoformat(),
                  "enddt": date.today().isoformat(), "forms": cfg["forms"]}
        got = 0
        frm = 0
        while got < cfg["max_hits_per_phrase"]:
            params["from"] = frm
            try:
                data = http.get_json(cfg["url"], params=params)
            except Exception as exc:
                log.warning("SEC '%s' hatası: %s", phrase, exc)
                break
            hits = (data.get("hits") or {}).get("hits") or []
            rows.extend(parse(hits, phrase))
            got += len(hits)
            frm += len(hits)
            if len(hits) < 100:
                break
    return schema.frame(rows)


def company_tickers() -> pd.DataFrame:
    """SEC'in resmi ticker ↔ CIK ↔ ad tablosu (ad eşleştirmesi için evren)."""
    data = http.get_json(sources()["sec_edgar"]["tickers_url"])
    df = pd.DataFrame(list(data.values()))
    df = df.rename(columns={"cik_str": "cik", "title": "name"})
    df["ticker"] = df["ticker"].str.upper()
    return df[["ticker", "name", "cik"]]
