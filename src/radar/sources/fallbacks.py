"""Yedek kaynaklar: birincil kaynak hata verirse veya boş dönerse otomatik devreye girer.

| Birincil                     | Yedek                                                        | Anahtar                 |
|------------------------------|--------------------------------------------------------------|-------------------------|
| Federal Register API         | GovInfo FR paketleri (granül = FR belge numarası, aynı kimlik) | GOVINFO_API_KEY*        |
| Congress.gov API             | GovInfo BILLS koleksiyonu (aynı kimlik şeması)               | GOVINFO_API_KEY*        |
| USAspending spending_by_award| USAspending spending_by_transaction (farklı uç nokta)        | yok                     |
| Grants.gov search2           | Grants.gov eski REST (apply07.grants.gov)                    | yok                     |
| SEC EDGAR tam metin arama    | data.sec.gov submissions: bilinen şirketlerin yeni 8-K'ları   | yok                     |
| yfinance                     | Yahoo chart API (doğrudan) → Stooq                           | yok                     |
* GOVINFO_API_KEY yoksa CONGRESS_API_KEY (ikisi de api.data.gov anahtarı) veya DEMO_KEY kullanılır.

Yedekler aynı olay kimliğini üretir; birincil düzelince kayıtlar çiftlenmez.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import pandas as pd

from .. import http, schema, store
from ..settings import env, sources

log = logging.getLogger("radar.fallback")
GOVINFO = "https://api.govinfo.gov"
FR_CLASS = {"RULE": "Rule", "PRORULE": "Proposed Rule", "NOTICE": "Notice", "PRESDOCU": "Presidential Document"}
BILL_TYPES = {"hr": "HR", "s": "S", "hjres": "HJRES", "sjres": "SJRES"}
MATERIAL_8K_ITEMS = {"1.01", "1.02", "2.01", "7.01", "8.01"}


def govinfo_key() -> str:
    return env("GOVINFO_API_KEY") or env("CONGRESS_API_KEY") or "DEMO_KEY"


def _days(since: date):
    d = max(since, date.today() - timedelta(days=30))
    while d <= date.today():
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------- Federal Register ← GovInfo

def federal_register(since: date) -> pd.DataFrame:
    from .federal_register import stage_for
    rows = []
    for d in _days(since):
        mark = "*"
        while mark:
            try:
                data = http.get_json(f"{GOVINFO}/packages/FR-{d.isoformat()}/granules",
                                     params={"offsetMark": mark, "pageSize": 100, "api_key": govinfo_key()}, retries=1)
            except Exception:
                break   # o gün FR yayımlanmamış olabilir (tatil)
            for g in data.get("granules") or []:
                doc = {"type": FR_CLASS.get(g.get("granuleClass", ""), "Notice"), "title": g.get("title", "")}
                stage, extra = stage_for(doc)
                extra["via"] = "govinfo"
                rows.append(schema.event("federal_register", g.get("granuleId"), stage, d.isoformat(), g.get("title"),
                                         text=g.get("title", ""), url=f"https://www.federalregister.gov/d/{g.get('granuleId')}",
                                         extra=extra))
            nxt = data.get("nextPage") or ""
            m = re.search(r"offsetMark=([^&]+)", nxt)
            mark = requests_unquote(m.group(1)) if m else ""
    return schema.frame(rows)


def requests_unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


# ---------------------------------------------------------------- Congress ← GovInfo BILLS

def bill_status(version: str) -> str:
    v = version.lower()
    if v in ("enr",):
        return "passed_chamber"          # iki meclisten geçti, imza bekliyor
    if v.startswith("e") or v in ("pcs", "pch", "ats", "ath", "rds", "rhs"):
        return "passed_chamber"
    if v.startswith("r"):
        return "reported"
    return "introduced"


def parse_bill_package(pid: str) -> tuple[int, str, str, str] | None:
    m = re.match(r"BILLS-(\d+)(hjres|sjres|hr|s)(\d+)([a-z]+)$", pid or "")
    if not m:
        return None
    return int(m.group(1)), BILL_TYPES[m.group(2)], m.group(3), bill_status(m.group(4))


def congress(since: date) -> pd.DataFrame:
    from .congress import TYPE_PATH, ordinal
    rows, mark = [], "*"
    start = max(since, date.today() - timedelta(days=30)).isoformat()
    for _ in range(10):
        data = http.get_json(f"{GOVINFO}/published/{start}", retries=1,
                             params={"offsetMark": mark, "pageSize": 100, "collection": "BILLS",
                                     "api_key": govinfo_key()})
        for p in data.get("packages") or []:
            parsed = parse_bill_package(p.get("packageId", ""))
            if not parsed:
                continue
            cong, btype, num, status = parsed
            url = f"https://www.congress.gov/bill/{ordinal(cong)}-congress/{TYPE_PATH[btype]}/{num}"
            rows.append(schema.event("congress", f"{cong}-{btype}-{num}-{status}", 1, p.get("dateIssued", ""),
                                     p.get("title"), text=status, url=url, agency="U.S. Congress",
                                     extra={"status": status, "bill": f"{btype} {num}", "congress": cong,
                                            "via": "govinfo"}))
        m = re.search(r"offsetMark=([^&]+)", data.get("nextPage") or "")
        if not m:
            break
        mark = requests_unquote(m.group(1))
    return schema.frame(rows)


# ---------------------------------------------------------------- USAspending ← transaction uç noktası

def usaspending(since: date) -> pd.DataFrame:
    from .usaspending import parse
    cfg = sources()["usaspending"]
    url = cfg["url"].replace("spending_by_award", "spending_by_transaction")
    fields = ["Award ID", "Recipient Name", "Transaction Amount", "Action Date", "Awarding Agency",
              "Awarding Sub Agency", "Transaction Description", "internal_id"]
    rows = []
    for kind, codes, lo in (("grant", cfg["grant_codes"], cfg["min_award_usd"]),
                            ("contract", cfg["contract_codes"], cfg["contract_min_usd"])):
        body = {"filters": {"time_period": [{"start_date": since.isoformat(), "end_date": date.today().isoformat()}],
                            "award_type_codes": codes, "award_amounts": [{"lower_bound": lo}]},
                "fields": fields, "page": 1, "limit": 100, "sort": "Transaction Amount", "order": "desc"}
        try:
            res = http.post_json(url, json=body, retries=1).get("results") or []
        except Exception as exc:
            log.warning("USAspending yedek (%s): %s", kind, exc)
            continue
        mapped = [{"Award ID": r.get("Award ID"), "Recipient Name": r.get("Recipient Name"),
                   "Award Amount": r.get("Transaction Amount"), "Start Date": r.get("Action Date"),
                   "Awarding Agency": r.get("Awarding Agency"), "Awarding Sub Agency": r.get("Awarding Sub Agency"),
                   "Description": r.get("Transaction Description"), "generated_internal_id": r.get("internal_id")}
                  for r in res]
        rows += parse(mapped, kind)
    return schema.frame(rows)


# ---------------------------------------------------------------- Grants.gov ← eski REST

def grants_gov(since: date) -> pd.DataFrame:
    from .grants_gov import parse
    cfg = sources()["grants_gov"]
    rows = []
    for kw in cfg["keywords"]:
        try:
            data = http.post_json("https://apply07.grants.gov/grantsws/rest/opportunities/search/", retries=1,
                                  json={"rows": cfg["rows"], "keyword": kw, "oppStatuses": "forecasted|posted"})
        except Exception as exc:
            log.warning("Grants.gov yedek '%s': %s", kw, exc)
            continue
        rows += [r for r in parse(data.get("oppHits") or [], kw) if r["date"] >= since.isoformat()]
    return schema.frame(rows)


# ---------------------------------------------------------------- SEC ← submissions (bilinen şirketler)

def sec_edgar(since: date, max_companies: int = 60) -> pd.DataFrame:
    """Tam metin arama çalışmazsa: daha önce teşvik ifadesi geçen şirketlerin yeni 8-K'larını izler."""
    ev = store.read("events")
    if ev.empty:
        return schema.frame([])
    s = ev[ev["source"] == "sec_edgar"].copy()
    if s.empty:
        return schema.frame([])
    s["cik"] = s["extra"].map(lambda x: (json.loads(x) if isinstance(x, str) and x.startswith("{") else {}).get("cik", ""))
    s["phrase"] = s["extra"].map(lambda x: (json.loads(x) if isinstance(x, str) and x.startswith("{") else {}).get("phrase", ""))
    s = s[s["cik"].astype(str) != ""].sort_values("date", ascending=False)
    comp = s.groupby("cik").agg(phrase=("phrase", lambda p: p.value_counts().index[0]), tickers=("tickers", "first"),
                                last=("date", "max")).sort_values("last", ascending=False).head(max_companies)
    rows = []
    for cik, c in comp.iterrows():
        try:
            data = http.get_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", retries=1)
        except Exception:
            continue
        rec = (data.get("filings") or {}).get("recent") or {}
        for form, d, adsh, items, doc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("accessionNumber", []),
                                             rec.get("items", []), rec.get("primaryDocument", [])):
            if form != "8-K" or d < since.isoformat():
                continue
            its = [i.strip() for i in str(items).split(",") if i.strip()]
            rows.append(schema.event(
                "sec_edgar", f"{adsh}-{c['phrase']}", 7, d, f"{data.get('name', '')} 8-K: {str(c['phrase']).strip(chr(34))}",
                text=f"{c['phrase']} items={','.join(its)}",
                url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{doc}",
                agency="SEC filing", recipient=data.get("name", ""), tickers=str(c["tickers"] or ""),
                extra={"form": "8-K", "cik": str(int(cik)), "adsh": adsh, "phrase": c["phrase"], "items": its,
                       "via": "submissions", "context_unverified": True}))
    return schema.frame(rows)


# ---------------------------------------------------------------- fiyat ← Yahoo chart

def yahoo_chart(ticker: str, rng: str = "3y") -> pd.DataFrame:
    sym = ticker.replace(".", "-")
    data, last = None, None
    for host in ("query2", "query1"):     # iki ayrı sunucu: biri kısıtlanırsa diğeri
        try:
            data = http.get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}", browser=False,
                                 retries=0, timeout=15, params={"range": rng, "interval": "1d"})
            break
        except Exception as exc:
            last = exc
    if data is None:
        raise last or RuntimeError("Yahoo chart")
    res = ((data.get("chart") or {}).get("result") or [None])[0]
    if not res:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    adj = ((ind.get("adjclose") or [{}])[0].get("adjclose")) or ((ind.get("quote") or [{}])[0].get("close")) or []
    df = pd.DataFrame({"date": pd.to_datetime(ts, unit="s").strftime("%Y-%m-%d"), "ticker": ticker, "close": adj})
    return df.dropna(subset=["close"])


REGISTRY = {"federal_register": federal_register, "congress": congress, "usaspending": usaspending,
            "grants_gov": grants_gov, "sec_edgar": sec_edgar}
