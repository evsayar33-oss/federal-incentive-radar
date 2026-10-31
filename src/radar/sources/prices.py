"""Daily closing prices: Yahoo Finance first, Yahoo Chart/Stooq fallbacks.

The radar can request more than 1,500 symbols. Download them in bounded batches
instead of letting yfinance create a large concurrent request burst. Missing
symbols remain non-fatal; fallback traffic is bounded and stops on rate limits.
"""
from __future__ import annotations

import io
import logging
import re
import time

import pandas as pd

from .. import http

log = logging.getLogger("radar.prices")
_YF_LOGGERS = (
    "yfinance",
    "yfinance.base",
    "yfinance.scrapers.history",
    "yfinance.data",
)
_BATCH_SIZE = 60
_BATCH_THREADS = 4
_BATCH_PAUSE_SECONDS = 0.25
_MAX_FALLBACK_SYMBOLS = 30


def _normalize_ticker(value: object) -> str:
    """Normalize display tickers without guessing company or ticker aliases."""
    if value is None:
        return ""
    ticker = re.sub(r"\s+", "", str(value).strip().upper().lstrip("$").strip())
    return ticker if ticker.isascii() else ""


def _is_rate_limit(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "429" in message or "too many requests" in message or "rate limit" in message


def _download_batch(tickers: list[str], period: str) -> pd.DataFrame:
    """Download one bounded symbol batch and return normalized long-form prices."""
    import yfinance as yf

    old_levels: dict[str, int] = {}
    for name in _YF_LOGGERS:
        logger = logging.getLogger(name)
        old_levels[name] = logger.level
        logger.setLevel(logging.CRITICAL)

    yahoo_tickers = [ticker.replace(".", "-") for ticker in tickers]
    try:
        data = yf.download(
            yahoo_tickers,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=_BATCH_THREADS,
        )
    finally:
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)

    empty = pd.DataFrame(columns=["date", "ticker", "close"])
    if data is None or data.empty:
        return empty

    if isinstance(data.columns, pd.MultiIndex):
        close = None
        for level in range(data.columns.nlevels):
            if "Close" in data.columns.get_level_values(level):
                close = data.xs("Close", axis=1, level=level, drop_level=True)
                break
        if close is None:
            return empty
        if isinstance(close, pd.Series):
            close = close.to_frame(name=yahoo_tickers[0])
    else:
        if "Close" not in data.columns:
            return empty
        close = data[["Close"]].rename(columns={"Close": yahoo_tickers[0]})

    reset = close.reset_index()
    if reset.empty:
        return empty
    date_col = reset.columns[0]
    long = reset.melt(id_vars=date_col, var_name="ticker", value_name="close")
    long = long.rename(columns={date_col: "date"})
    long["date"] = pd.to_datetime(long["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    long["ticker"] = long["ticker"].astype(str).str.replace("-", ".", regex=False)
    long["ticker"] = long["ticker"].map(_normalize_ticker)
    long["close"] = pd.to_numeric(long["close"], errors="coerce")
    return long.dropna(subset=["date", "ticker", "close"])


def _yfinance(tickers: list[str], period: str) -> pd.DataFrame:
    """Download all symbols in small batches; continue after a failed batch."""
    frames: list[pd.DataFrame] = []
    batch_count = (len(tickers) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for batch_no, start in enumerate(range(0, len(tickers), _BATCH_SIZE), start=1):
        batch = tickers[start : start + _BATCH_SIZE]
        try:
            result = _download_batch(batch, period)
            if not result.empty:
                frames.append(result)
        except Exception as exc:
            log.warning(
                "yfinance fiyat grubu %d/%d başarısız (%s); diğer gruplara devam edilecek",
                batch_no,
                batch_count,
                str(exc)[:160],
            )
        if batch_no < batch_count:
            time.sleep(_BATCH_PAUSE_SECONDS)

    if not frames:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        ["date", "ticker"], keep="last"
    ).reset_index(drop=True)


def _stooq(ticker: str) -> pd.DataFrame:
    symbol = ticker.lower().replace(".", "-") + ".us"
    response = http.request(
        "GET",
        "https://stooq.com/q/d/l/",
        params={"s": symbol, "i": "d"},
        browser=True,
        retries=0,
        timeout=10,
    )
    frame = pd.read_csv(io.StringIO(response.text))
    if not {"Date", "Close"}.issubset(frame.columns):
        return pd.DataFrame(columns=["date", "ticker", "close"])
    return pd.DataFrame({"date": frame["Date"], "ticker": ticker, "close": frame["Close"]})


def fetch(tickers: list[str], period: str = "3y") -> pd.DataFrame:
    """Fetch prices with bounded provider traffic; missing prices are non-fatal."""
    # dict.fromkeys preserves the priority order supplied by the caller.
    normalized = [_normalize_ticker(ticker) for ticker in (tickers or [])]
    tickers = list(dict.fromkeys(ticker for ticker in normalized if ticker))
    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "close"])

    try:
        frame = _yfinance(tickers, period)
        if frame.empty:
            missing = set(tickers)
        else:
            frame["ticker"] = frame["ticker"].map(_normalize_ticker)
            missing = set(tickers) - set(frame["ticker"].dropna())
    except Exception as exc:
        log.warning(
            "yfinance fiyat toplama işlemi başarısız (%s); yedek kaynaklar denenecek",
            str(exc)[:180],
        )
        frame = pd.DataFrame(columns=["date", "ticker", "close"])
        missing = set(tickers)

    from .fallbacks import yahoo_chart

    chart_range = period if period in (
        "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"
    ) else ("5d" if period.endswith("d") else "5y")

    # Retain a strict fallback budget. A 429 means Yahoo is rate-limiting the
    # environment; do not multiply the failure with more Yahoo or Stooq calls.
    attempted = [ticker for ticker in tickers if ticker in missing][:_MAX_FALLBACK_SYMBOLS]
    extras: list[pd.DataFrame] = []
    yahoo_rate_limited = False
    stooq_failures = 0

    for ticker in attempted:
        try:
            result = yahoo_chart(ticker, chart_range)
            if result is not None and not result.empty:
                result = result.copy()
                if "ticker" in result.columns:
                    result["ticker"] = result["ticker"].map(_normalize_ticker)
                extras.append(result)
                continue
        except Exception as exc:
            if _is_rate_limit(exc):
                yahoo_rate_limited = True
                log.warning(
                    "Yahoo Chart hız sınırına ulaştı; kalan yedek fiyat istekleri durduruldu"
                )
                break

        try:
            result = _stooq(ticker)
            if result is not None and not result.empty:
                extras.append(result)
        except Exception:
            stooq_failures += 1

    if extras:
        frame = pd.concat([frame] + extras, ignore_index=True)

    if not frame.empty and {"date", "ticker", "close"}.issubset(frame.columns):
        frame["ticker"] = frame["ticker"].map(_normalize_ticker)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["ticker", "date", "close"])
        frame = frame.drop_duplicates(["date", "ticker"], keep="last").reset_index(drop=True)

    resolved = set(frame["ticker"]) if not frame.empty else set()
    unresolved = [ticker for ticker in tickers if ticker not in resolved]
    if unresolved:
        sample = ", ".join(unresolved[:12])
        more = f" (+{len(unresolved) - 12} daha)" if len(unresolved) > 12 else ""
        not_attempted = max(0, len(missing) - len(attempted))
        suffixes = []
        if not_attempted:
            suffixes.append(f"{not_attempted} sembol yedek kaynak bütçesi dışında kaldı")
        if yahoo_rate_limited:
            suffixes.append("Yahoo hız sınırı nedeniyle yedek arama durduruldu")
        if stooq_failures:
            suffixes.append(f"{stooq_failures} Stooq isteği başarısız oldu")
        suffix = "; " + "; ".join(suffixes) if suffixes else ""
        log.warning(
            "Fiyat verisi bulunamayan sembol: %d/%d. Örnek: %s%s%s",
            len(unresolved),
            len(tickers),
            sample,
            more,
            suffix,
        )
    else:
        log.info("Fiyat verisi alındı: %d/%d sembol", len(tickers), len(resolved))

    return frame
