"""İçeriden alım (SEC Form 4) kanıt motoru.

Kaynak: SEC'in üç aylık "Insider Transactions Data Sets" dosyaları (tek indirme = bir çeyrek, anahtar gerekmez).
Yalnızca açık piyasa ALIMLARI (işlem kodu P) kullanılır; opsiyon/ödül/vergi satışları dışarıda kalır.
Olay tanımları:
  cluster   : 10 gün içinde en az 3 farklı yöneticinin açık piyasadan alımı (toplam ≥ min_total_usd)
  exec_buy  : CEO/CFO/Başkan'ın tek bildirimde ≥ min_total_usd alımı
Olay tarihi = tetikleyen bildirimin SEC'e verildiği gün (known_at); sonrası bilinemez → ileriye bakış yok.
Ölçüm ve kanıt kapıları yatırım bölümüyle aynıdır (investments.measure/build_evidence/build_subgroups,
`corp_invest.gate`): eşikler gevşetilmez. Veri üç aylık yayınlandığı için canlı sinyal gecikmelidir;
amaç önce etkinin var olup olmadığını kanıtlamaktır.
"""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from datetime import date

import pandas as pd

from . import http, schema, store
from . import investments as I
from .settings import sources

log = logging.getLogger("radar.insider")
URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip"
NAMES = {"cluster": "İçeriden toplu alım (3+ yönetici)", "exec_buy": "CEO/CFO açık piyasa alımı"}
EXEC_WORDS = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL", "PRESIDENT")
I.CATEGORY_NAMES.update(NAMES)


def cfg() -> dict:
    return sources()["insider"]


def quarters(years: float, today: date | None = None) -> list[tuple[int, int]]:
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(int(years * 4) + 1):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out[::-1]


def _tsv(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    match = [n for n in z.namelist() if n.upper().endswith(name)]
    if not match:
        return pd.DataFrame(columns=cols)
    with z.open(match[0]) as f:
        df = pd.read_csv(f, sep="\t", dtype=str, usecols=lambda c: c.upper() in cols, on_bad_lines="skip")
    df.columns = [c.upper() for c in df.columns]
    return df


def parse_zip(blob: bytes) -> pd.DataFrame:
    """Zip → açık piyasa alımları: filing_date, ticker, owner, is_exec, value."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        sub = _tsv(z, "SUBMISSION.TSV", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"])
        own = _tsv(z, "REPORTINGOWNER.TSV", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_TITLE"])
        trn = _tsv(z, "NONDERIV_TRANS.TSV", ["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                              "TRANS_ACQUIRED_DISP_CD"])
    if sub.empty or trn.empty:
        return pd.DataFrame()
    sub = sub[sub["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    trn = trn[(trn["TRANS_CODE"] == "P") & (trn["TRANS_ACQUIRED_DISP_CD"].fillna("A") == "A")].copy()
    trn["value"] = pd.to_numeric(trn["TRANS_SHARES"], errors="coerce") * pd.to_numeric(trn["TRANS_PRICEPERSHARE"], errors="coerce")
    trn = trn[trn["value"] > 0]
    own = own.drop_duplicates(["ACCESSION_NUMBER", "RPTOWNERCIK"])
    df = trn.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    df["filing_date"] = pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce")
    df["ticker"] = df["ISSUERTRADINGSYMBOL"].fillna("").str.upper().str.strip()
    title = df["RPTOWNER_TITLE"].fillna("").str.upper()
    df["is_exec"] = title.apply(lambda t: any(w in t for w in EXEC_WORDS))
    df["owner"] = df["RPTOWNERCIK"].fillna(df["ACCESSION_NUMBER"])
    df = df.dropna(subset=["filing_date"])
    df = df[df["ticker"] != ""]
    return df[["ACCESSION_NUMBER", "filing_date", "ticker", "owner", "is_exec", "value"]]


def make_events(tx: pd.DataFrame, universe: set[str] | None = None) -> pd.DataFrame:
    """Alım satırları → cluster / exec_buy olayları (olay tarihi = bildirim günü)."""
    c = cfg()
    if tx is None or tx.empty:
        return schema.frame([])
    tx = tx[tx["value"] >= c["min_tx_usd"]]
    if universe is not None:
        tx = tx[tx["ticker"].isin(universe)]
    rows: list[dict] = []
    win = pd.Timedelta(days=int(c["cluster_days"]))
    for t, g in tx.sort_values("filing_date").groupby("ticker"):
        seen_until = pd.Timestamp.min
        for i in range(len(g)):
            now = g.iloc[i]["filing_date"]
            if now <= seen_until:
                continue
            w = g[(g["filing_date"] > now - win) & (g["filing_date"] <= now)]
            if w["owner"].nunique() >= c["cluster_min_insiders"] and w["value"].sum() >= c["min_total_usd"]:
                rows.append(_ev("cluster", t, now, w))
                seen_until = now + win   # kümenin içinde tekrar olay üretme
        by_acc = g[g["is_exec"]].groupby("ACCESSION_NUMBER").agg(d=("filing_date", "max"), v=("value", "sum"))
        for acc, r in by_acc[by_acc["v"] >= c["min_total_usd"]].iterrows():
            rows.append(_ev("exec_buy", t, r["d"], g[g["ACCESSION_NUMBER"] == acc]))
    return schema.frame(rows)


def _ev(kind: str, ticker: str, d: pd.Timestamp, w: pd.DataFrame) -> dict:
    total, n = float(w["value"].sum()), int(w["owner"].nunique())
    day = d.date().isoformat()
    return schema.event("insider", f"{kind}:{ticker}:{day}", 8, day, f"{ticker}: {n} yönetici açık piyasadan ${total:,.0f} alım",
                        tickers=ticker, amount_usd=total, amount_type="insider_purchase", known_at=day,
                        extra={"category": kind, "insiders": n})


def sync(years: float = 5.0, budget_minutes: float = 20.0) -> dict:
    """Henüz yüklenmemiş çeyrekleri indirir; yayınlanmamış (404) çeyrekleri sonra tekrar dener."""
    if not cfg().get("enabled", True):
        return {"skipped": True}
    t0 = time.time()
    prog = store.read("insider_progress")
    done = set(prog["quarter"].astype(str)) if not prog.empty else set()
    uni = store.read("universe")
    universe = set(uni["ticker"].astype(str)) if not uni.empty else None
    out = {"quarters_done": 0, "events": 0, "pending": [], "errors": []}
    for y, q in quarters(years):
        key = f"{y}q{q}"
        if key in done:
            continue
        if time.time() - t0 > budget_minutes * 60:
            out["pending"].append(key)
            continue
        try:
            r = http.request("GET", URL.format(y=y, q=q), retries=2, timeout=120)
            ev = make_events(parse_zip(r.content), universe)
        except Exception as exc:
            msg = str(exc)
            (out["pending"] if "404" in msg else out["errors"]).append(f"{key}: {msg[:100]}" if "404" not in msg else key)
            continue
        if not ev.empty:
            store.upsert("insider_events", ev, "event_id", keep="first")
        store.append("insider_progress", pd.DataFrame([{"quarter": key, "events": len(ev)}]))
        out["quarters_done"] += 1
        out["events"] += len(ev)
    return out


def tickers(limit: int = 800) -> list[str]:
    ev = store.read("insider_events")
    if ev.empty:
        return []
    t = ev.sort_values("date", ascending=False)["tickers"].astype(str)
    return list(dict.fromkeys(x.strip() for x in t if x.strip()))[:limit]


def refresh(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    events = store.read("insider_events")
    prices = store.read("prices") if prices is None else prices
    if events.empty or prices is None or prices.empty:
        return store.read("insider_evidence")
    ret = I.measure(events, prices)
    if not ret.empty:
        store.write("insider_returns", ret)
    ev = I.build_evidence(ret)
    if not ev.empty:
        ev["explain"] = ev["explain"].str.replace("yatırım haberi", "içeriden alım olayı", regex=False)
    store.write("insider_evidence", ev)
    sub = I.build_subgroups(ret, store.read("insider_subgroups"))
    store.write("insider_subgroups", sub)
    log.info("İçeriden alım kanıtı: %d grup, %d kanıtlı, %d aday", len(ev),
             int((ev["status"] == "proven").sum()) if len(ev) else 0,
             int((sub["status"].isin(["candidate", "confirmed"])).sum()) if len(sub) else 0)
    return ev
