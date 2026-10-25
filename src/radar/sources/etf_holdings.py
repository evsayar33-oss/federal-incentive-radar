"""ETF varlık dosyaları — ücretsiz, günlük. GICS sektörü ve endeks ağırlığı için vekil.

SPDR sektör ETF'leri (XLK, XLE, ...) = GICS sektör üyeliği.
iShares IVV = S&P 500 ağırlığı, IWM = Russell 2000 ağırlığı.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .. import http
from ..settings import sector_etfs, sources

log = logging.getLogger("radar.etf")


def parse_spdr_xlsx(content: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(content), header=None, engine="openpyxl")
    hdr = next(i for i, row in raw.iterrows() if any(str(v).strip() == "Ticker" for v in row.values))
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr].values]
    df = df[df["Ticker"].notna() & (df["Ticker"].astype(str).str.len() <= 7)]
    df = df[~df["Ticker"].astype(str).str.contains("CASH|USD|-", na=False)]
    return pd.DataFrame({"ticker": df["Ticker"].astype(str).str.upper().str.strip(),
                         "name": df["Name"].astype(str).str.strip(),
                         "weight": pd.to_numeric(df["Weight"], errors="coerce")})


def parse_ishares_csv(text: str) -> pd.DataFrame:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Ticker,") or ln.startswith('"Ticker"'))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    wcol = next(c for c in df.columns if c.startswith("Weight"))
    return pd.DataFrame({"ticker": df["Ticker"].astype(str).str.upper().str.strip(),
                         "name": df["Name"].astype(str).str.strip(),
                         "weight": pd.to_numeric(df[wcol], errors="coerce"),
                         "gics_sector_ishares": df.get("Sector", pd.Series(dtype=str))})


def fetch_universe() -> pd.DataFrame:
    cfg = sources()["etf_holdings"]
    parts = []
    for sector, etf in sector_etfs().items():
        try:
            r = http.request("GET", cfg["spdr_url"].format(ticker=etf.lower()), browser=True)
            d = parse_spdr_xlsx(r.content)
            d["sector"] = sector
            d = d.rename(columns={"weight": "sector_weight"})
            parts.append(d)
        except Exception as exc:
            log.warning("SPDR %s alınamadı: %s", etf, exc)
    uni = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["ticker", "name", "sector", "sector_weight"])
    # S&P 500 ağırlığı: SPDR SPY (sektör dosyalarıyla aynı, çalıştığı doğrulanmış biçim).
    try:
        r = http.request("GET", cfg["spdr_url"].format(ticker="spy"), browser=True)
        spy = parse_spdr_xlsx(r.content).rename(columns={"weight": "sp500_weight", "name": "name_spy"})
        uni = uni.merge(spy, on="ticker", how="outer")
        uni["name"] = uni["name"].fillna(uni.pop("name_spy"))
    except Exception as exc:
        log.warning("SPDR SPY alınamadı: %s", exc)
    # Russell 2000 ağırlığı: iShares IWM (GitHub IP'lerinden engellenebilir; olmazsa boş kalır).
    try:
        r = http.request("GET", cfg["ishares"]["IWM"], browser=True, retries=1)
        d = parse_ishares_csv(r.text)[["ticker", "name", "weight", "gics_sector_ishares"]].rename(
            columns={"weight": "r2000_weight", "name": "name_iwm"})
        uni = uni.merge(d, on="ticker", how="outer")
        uni["name"] = uni["name"].fillna(uni.pop("name_iwm"))
    except Exception as exc:
        log.warning("iShares IWM alınamadı: %s", str(exc)[:200])
    if "gics_sector_ishares" in uni.columns:
        uni["sector"] = uni["sector"].fillna(uni["gics_sector_ishares"])
        uni = uni.drop(columns=["gics_sector_ishares"])
    return uni.drop_duplicates("ticker")
