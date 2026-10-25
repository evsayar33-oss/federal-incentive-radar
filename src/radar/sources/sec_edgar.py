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


def fetch(since: date, until: date | None = None) -> pd.DataFrame:
    cfg = sources()["sec_edgar"]
    rows: list[dict] = []
    for phrase in cfg["phrases"]:
        params = {"q": phrase, "dateRange": "custom", "startdt": since.isoformat(),
                  "enddt": (until or date.today()).isoformat(), "forms": cfg["forms"]}
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


# ---------------------------------------------------------------- şirket sektörü (SIC) — kalıcı bağ denetimi için

SIC_RANGES = [  # (başlangıç, bitiş, sektör) — ilk eşleşen kazanır
    (1300, 1399, "Energy"), (2900, 2999, "Energy"), (1000, 1499, "Materials"), (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"), (2200, 2399, "Consumer Discretionary"), (2400, 2699, "Materials"),
    (2700, 2799, "Communication Services"), (2830, 2836, "Health Care"), (2800, 2899, "Materials"),
    (3000, 3399, "Materials"), (3570, 3579, "Information Technology"), (3711, 3716, "Consumer Discretionary"),
    (3840, 3851, "Health Care"), (3600, 3699, "Information Technology"), (3800, 3899, "Information Technology"),
    (3400, 3799, "Industrials"), (3900, 3999, "Consumer Discretionary"), (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"), (4000, 4799, "Industrials"), (5000, 5199, "Industrials"),
    (5400, 5499, "Consumer Staples"), (5200, 5999, "Consumer Discretionary"), (6500, 6799, "Real Estate"),
    (6000, 6499, "Financials"), (7370, 7379, "Information Technology"), (8000, 8099, "Health Care"),
    (7000, 8999, "Industrials"),
]


def sic_sector(sic) -> str | None:
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return None
    if s == 6798:   # REIT
        return "Real Estate"
    if s == 6770:   # boş çek şirketi (SPAC)
        return "Financials"
    for lo, hi, sec in SIC_RANGES:
        if lo <= s <= hi:
            return sec
    return None


def company_sic(ciks: list[str]) -> pd.DataFrame:
    """data.sec.gov submissions → SIC kodu ve sektör. Sonuç 'company_sic' tablosunda önbelleklenir."""
    rows = []
    for cik in ciks:
        try:
            d = http.get_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", retries=1)
        except Exception:
            continue
        for t in d.get("tickers") or [""]:
            rows.append({"cik": str(int(cik)), "ticker": str(t).upper(), "sic": str(d.get("sic") or ""),
                         "sic_desc": d.get("sicDescription") or "", "sector": sic_sector(d.get("sic")) or ""})
    return pd.DataFrame(rows)
