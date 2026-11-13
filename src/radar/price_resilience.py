"""Bounded, auditable OHLCV resilience for H083 research only.

Never fabricate a missing market session or interpolate historical prices.
A secondary source is used per *whole symbol*, not to fill individual bars;
the provider is recorded so its adjusted/unadjusted conventions are visible.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Callable

import pandas as pd

from . import http

LOG = logging.getLogger("radar.price_resilience")
MAX_FALLBACK_SYMBOLS = 24
BATCH_SIZE = 15
REQUIRED = ("Open", "High", "Low", "Close", "Volume")
VALID = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def normalize(frame: pd.DataFrame | None) -> pd.DataFrame:
    empty = pd.DataFrame(columns=list(REQUIRED))
    if frame is None or frame.empty or not set(REQUIRED).issubset(frame.columns):
        return empty
    out = frame.loc[:, list(REQUIRED)].copy()
    try:
        out.index = pd.DatetimeIndex(out.index)
    except (ValueError, TypeError):
        return empty
    if out.index.tz is not None:
        out.index = out.index.tz_convert("America/New_York").tz_localize(None)
    out.index = out.index.normalize()
    for field in REQUIRED:
        out[field] = pd.to_numeric(out[field], errors="coerce")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # One corrupt bar must never silently produce valid-looking indicators.
    good = (out["Open"].gt(0) & out["High"].gt(0) & out["Low"].gt(0)
            & out["Close"].gt(0) & out["Volume"].ge(0)
            & out["High"].ge(out["Low"])
            & out["High"].ge(out[["Open", "Close"]].max(axis=1) * 0.999)
            & out["Low"].le(out[["Open", "Close"]].min(axis=1) * 1.001))
    return out.loc[good].copy()


def _yahoo(tickers: list[str], period: str, yf_module=None) -> tuple[dict[str, pd.DataFrame], list[str]]:
    if yf_module is None:
        import yfinance as yf_module
    result: dict[str, pd.DataFrame] = {}
    errors = []
    for start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[start:start + BATCH_SIZE]
        symbols = [x.replace(".", "-") for x in batch]
        try:
            downloaded = yf_module.download(
                symbols, period=period, interval="1d", auto_adjust=True,
                group_by="ticker", progress=False, threads=False,
            )
            if downloaded is None or downloaded.empty:
                errors.append("EMPTY_YAHOO_BATCH")
                continue
            for ticker, symbol in zip(batch, symbols):
                try:
                    part = downloaded[symbol] if isinstance(downloaded.columns, pd.MultiIndex) else downloaded
                    clean = normalize(part)
                    if not clean.empty:
                        result[ticker] = clean
                except (KeyError, ValueError, TypeError):
                    continue
        except Exception as exc:
            LOG.warning("H083 Yahoo batch failed (%d): %s", len(batch), str(exc)[:130])
            errors.append(type(exc).__name__)
    return result, errors


def stooq_bars(ticker: str) -> pd.DataFrame:
    """Secondary OHLCV source; use only for missing *entire* symbol series."""
    symbol = ticker.lower().replace(".", "-") + ".us"
    response = http.request(
        "GET", "https://stooq.com/q/d/l/",
        params={"s": symbol, "i": "d"}, browser=True, retries=0, timeout=10,
    )
    data = pd.read_csv(io.StringIO(response.text))
    if not {"Date", *REQUIRED}.issubset(data.columns):
        return pd.DataFrame(columns=list(REQUIRED))
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    return normalize(data.set_index("Date"))


def download_bars(
    tickers: list[str], period: str = "3y",
    *, fallback: Callable[[str], pd.DataFrame] | None = None,
    yahoo_loader: Callable[[list[str], str], tuple[dict[str, pd.DataFrame], list[str]]] | None = None,
    max_fallback: int = MAX_FALLBACK_SYMBOLS,
) -> tuple[dict[str, pd.DataFrame], dict]:
    """Return price frames and a truthful, bounded provider coverage audit."""
    names = list(dict.fromkeys(str(t).strip().upper() for t in tickers if VALID.fullmatch(str(t).strip().upper())))
    loader = yahoo_loader or _yahoo
    try:
        bars, errors = loader(names, period)
    except Exception as exc:
        LOG.warning("H083 price provider failed: %s", type(exc).__name__)
        bars, errors = {}, [type(exc).__name__]
    bars = {t: normalize(frame) for t, frame in bars.items() if t in names}
    bars = {t: frame for t, frame in bars.items() if not frame.empty}
    providers = {t: "YAHOO_ADJUSTED" for t in bars}
    missing = [t for t in names if t not in bars]
    # Recover benchmark first. Never turn a price-provider outage into 180
    # uncapped HTTP requests; alternate provider traffic has a hard ceiling.
    ordered = sorted(missing, key=lambda t: (t != "SPY", names.index(t)))
    attempted = ordered[:max(0, min(int(max_fallback), MAX_FALLBACK_SYMBOLS))]
    secondary = fallback or stooq_bars
    for ticker in attempted:
        try:
            frame = normalize(secondary(ticker))
            if not frame.empty:
                bars[ticker] = frame
                providers[ticker] = "STOOQ_SECONDARY_ADJUSTMENT_NOT_VERIFIED"
        except Exception as exc:
            errors.append("STOOQ_" + type(exc).__name__)
            LOG.info("H083 secondary unavailable %s: %s", ticker, type(exc).__name__)
    report = {
        "requested": len(names),
        "loaded": len(bars),
        "missing": len(names) - len(bars),
        "fallback_attempted": len(attempted),
        "fallback_recovered": sum(t in bars for t in attempted),
        "fallback_budget": MAX_FALLBACK_SYMBOLS,
        "providers": providers,
        "errors": errors[:12],
        "note": "Providers may use different corporate-action adjustment conventions; no bar stitching.",
    }
    return bars, report
