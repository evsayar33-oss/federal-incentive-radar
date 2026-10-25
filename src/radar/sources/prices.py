"""Günlük kapanış fiyatları. Birincil: yfinance (ücretsiz). Yedek: Stooq CSV.

Not: Bu katman resmi bir kaynak değildir. Sistem fiyat olmadan da çalışır;
yalnızca tahmin defteri puanlaması ve olay çalışması ertelenir.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .. import http

log = logging.getLogger("radar.prices")


def _yfinance(tickers: list[str], period: str) -> pd.DataFrame:
    import yfinance as yf  # Actions ortamında kurulu
    data = yf.download(tickers, period=period, interval="1d", auto_adjust=True,
                       progress=False, group_by="column", threads=True)
    if data is None or data.empty:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]].rename(columns={"Close": tickers[0]})
    long = close.reset_index().melt(id_vars=close.index.name or "Date", var_name="ticker", value_name="close")
    long = long.rename(columns={close.index.name or "Date": "date"})
    long["date"] = pd.to_datetime(long["date"]).dt.strftime("%Y-%m-%d")
    return long.dropna(subset=["close"])


def _stooq(ticker: str) -> pd.DataFrame:
    sym = ticker.lower().replace(".", "-") + ".us"
    r = http.request("GET", "https://stooq.com/q/d/l/", params={"s": sym, "i": "d"}, browser=True, retries=1)
    df = pd.read_csv(io.StringIO(r.text))
    if "Close" not in df.columns:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    return pd.DataFrame({"date": df["Date"], "ticker": ticker, "close": df["Close"]})


def fetch(tickers: list[str], period: str = "3y") -> pd.DataFrame:
    tickers = sorted({t for t in tickers if t and t.isascii()})
    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    try:
        df = _yfinance([t.replace(".", "-") for t in tickers], period)
        df["ticker"] = df["ticker"].str.replace("-", ".", regex=False)
        missing = set(tickers) - set(df["ticker"])
    except Exception as exc:
        log.warning("yfinance başarısız (%s), Stooq yedeğine geçiliyor", exc)
        df, missing = pd.DataFrame(columns=["date", "ticker", "close"]), set(tickers)
    extra = []
    for t in sorted(missing)[:40]:
        try:
            extra.append(_stooq(t))
        except Exception:
            pass
    if extra:
        df = pd.concat([df] + extra, ignore_index=True)
    return df
