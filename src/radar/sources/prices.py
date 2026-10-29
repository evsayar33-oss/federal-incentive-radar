"""Daily closing prices: Yahoo Finance first, Yahoo Chart/Stooq fallbacks.

Normalizes display tickers (e.g. "$AZEK" -> "AZEK"), limits noisy per-symbol
yfinance logging, and summarizes symbols for which no prices were found.
"""
from __future__ import annotations
import io
import logging
import re
import pandas as pd
from .. import http

log = logging.getLogger("radar.prices")
_YF_LOGGERS = ("yfinance", "yfinance.base", "yfinance.scrapers.history", "yfinance.data")

def _normalize_ticker(value: object) -> str:
    if value is None:
        return ""
    ticker = re.sub(r"\\s+", "", str(value).strip().upper().lstrip("$").strip())
    return ticker if ticker.isascii() else ""

def _yfinance(tickers: list[str], period: str) -> pd.DataFrame:
    import yfinance as yf
    old_levels = {}
    for name in _YF_LOGGERS:
        logger = logging.getLogger(name)
        old_levels[name] = logger.level
        logger.setLevel(logging.CRITICAL)
    try:
        yahoo_tickers = [t.replace(".", "-") for t in tickers]
        data = yf.download(yahoo_tickers, period=period, interval="1d",
                           auto_adjust=True, progress=False, group_by="column", threads=True)
    finally:
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)
    if data is None or data.empty:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    if isinstance(data.columns, pd.MultiIndex):
        if "Close" not in data.columns.get_level_values(0):
            return pd.DataFrame(columns=["date", "ticker", "close"])
        close = data["Close"]
    else:
        if "Close" not in data.columns:
            return pd.DataFrame(columns=["date", "ticker", "close"])
        close = data[["Close"]].rename(columns={"Close": yahoo_tickers[0]})
    idx = close.index.name or "Date"
    long = close.reset_index().melt(id_vars=idx, var_name="ticker", value_name="close")
    long = long.rename(columns={idx: "date"})
    long["date"] = pd.to_datetime(long["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    long["ticker"] = long["ticker"].astype(str).str.replace("-", ".", regex=False)
    return long.dropna(subset=["close", "date"])

def _stooq(ticker: str) -> pd.DataFrame:
    sym = ticker.lower().replace(".", "-") + ".us"
    response = http.request("GET", "https://stooq.com/q/d/l/",
                            params={"s": sym, "i": "d"}, browser=True, retries=0, timeout=10)
    df = pd.read_csv(io.StringIO(response.text))
    if not {"Date", "Close"}.issubset(df.columns):
        return pd.DataFrame(columns=["date", "ticker", "close"])
    return pd.DataFrame({"date": df["Date"], "ticker": ticker, "close": df["Close"]})

def fetch(tickers: list[str], period: str = "3y") -> pd.DataFrame:
    """Fetch available prices; unresolved symbols do not abort the radar pipeline."""
    tickers = sorted({_normalize_ticker(t) for t in (tickers or [])} - {""})
    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    try:
        df = _yfinance(tickers, period)
        if df.empty:
            missing = set(tickers)
        else:
            df["ticker"] = df["ticker"].map(_normalize_ticker)
            missing = set(tickers) - set(df["ticker"].dropna())
    except Exception as exc:
        log.warning("yfinance toplu fiyat isteği başarısız (%s); yedek kaynaklar denenecek", str(exc)[:180])
        df = pd.DataFrame(columns=["date", "ticker", "close"])
        missing = set(tickers)
    extra = []
    from .fallbacks import yahoo_chart
    rng = period if period in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max") else (
        "5d" if period.endswith("d") else "5y")
    attempted = sorted(missing)[:60]
    for ticker in attempted:
        try:
            got = yahoo_chart(ticker, rng)
            if got is not None and not got.empty:
                got = got.copy()
                if "ticker" in got.columns:
                    got["ticker"] = got["ticker"].map(_normalize_ticker)
                extra.append(got)
                continue
        except Exception:
            pass
        try:
            got = _stooq(ticker)
            if got is not None and not got.empty:
                extra.append(got)
        except Exception:
            pass
    if extra:
        df = pd.concat([df] + extra, ignore_index=True)
    if not df.empty and {"ticker", "date", "close"}.issubset(df.columns):
        df["ticker"] = df["ticker"].map(_normalize_ticker)
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["ticker", "date", "close"])
        df = df.drop_duplicates(["date", "ticker"], keep="last").reset_index(drop=True)
    resolved = set(df["ticker"]) if not df.empty else set()
    unresolved = sorted(set(tickers) - resolved)
    if unresolved:
        sample = ", ".join(unresolved[:12])
        more = f" (+{len(unresolved)-12} daha)" if len(unresolved) > 12 else ""
        not_attempted = max(0, len(missing) - len(attempted))
        suffix = f"; {not_attempted} sembol yedek kaynak deneme sınırına takıldı" if not_attempted else ""
        log.warning("Fiyat verisi bulunamayan sembol: %d/%d. Örnek: %s%s%s",
                    len(unresolved), len(tickers), sample, more, suffix)
    else:
        log.info("Fiyat verisi alındı: %d/%d sembol", len(tickers), len(resolved))
    return df
