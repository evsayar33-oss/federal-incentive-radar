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



def yahoo_single_bars(ticker: str, period: str) -> pd.DataFrame:
    """Independently re-query Yahoo for one symbol after a partial batch result.

    This is the SAME vendor as the batch, not independent corroboration.
    It may recover transient batch/transport gaps, never fabricate absent trades.
    """
    import yfinance as yf
    # Yahoo's chart API does not promise arbitrary periods such as "3y".
    # Use a documented longer range and verify the actual as-of/200-session
    # window against the benchmark before accepting anything.
    supported_period = "5y" if period == "3y" else period
    return normalize(yf.Ticker(ticker.replace(".", "-")).history(
        period=supported_period, interval="1d", auto_adjust=True, actions=False,
    ))


def _replacement_quality(candidate: pd.DataFrame, primary: pd.DataFrame | None,
                         benchmark: pd.DataFrame | None, asof) -> str | None:
    """Validate the complete replacement on the actual benchmark calendar."""
    if candidate.empty:
        return "EMPTY_RECHECK"
    if asof is None:
        # Only SPY can establish the benchmark when it is missing.
        if len(candidate) < 215:
            return "INSUFFICIENT_BENCHMARK_HISTORY"
    else:
        if asof not in candidate.index:
            return "MISSING_ASOF_CLOSE"
        if pd.isna(candidate.loc[asof, "Volume"]) or candidate.loc[asof, "Volume"] <= 0:
            return "MISSING_ASOF_VOLUME"
        if benchmark is not None:
            sessions = benchmark.index[benchmark.index <= asof][-200:]
            if len(sessions) < 200 or candidate.reindex(sessions)["Close"].isna().any():
                return "INCOMPLETE_BENCHMARK_CALENDAR"
    if primary is not None and not primary.empty:
        overlap = primary[["Close"]].join(
            candidate[["Close"]], how="inner",
            lsuffix="_primary", rsuffix="_secondary",
        ).tail(20)
        if len(overlap) < 5:
            return "INSUFFICIENT_SOURCE_OVERLAP"
        ratio = overlap["Close_secondary"] / overlap["Close_primary"]
        if not ratio.notna().all() or (ratio - 1).abs().max() > 0.02:
            return "ADJUSTMENT_OR_PRICE_MISMATCH"
    return None


def download_bars(
    tickers: list[str], period: str = "3y",
    *, fallback: Callable[[str], pd.DataFrame] | None = None,
    yahoo_loader: Callable[[list[str], str], tuple[dict[str, pd.DataFrame], list[str]]] | None = None,
    single_loader: Callable[[str, str], pd.DataFrame] | None = None,
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
    # A downloaded symbol can still be unusable for H083 when it lacks
    # the benchmark's latest completed close, volume, or 200 sessions.
    # Use the benchmark's last observed session, never wall-clock today.
    benchmark = bars.get("SPY")
    asof = benchmark.index[-1] if benchmark is not None and not benchmark.empty else None
    impaired = []
    impairment_reasons = {}
    if asof is not None:
        for ticker in names:
            if ticker == "SPY" or ticker not in bars:
                continue
            frame = bars[ticker]
            recent = frame.loc[frame.index <= asof]
            if recent.empty or asof not in recent.index:
                reason = "MISSING_ASOF_CLOSE"
            elif pd.isna(recent.loc[asof, "Volume"]) or recent.loc[asof, "Volume"] <= 0:
                reason = "MISSING_ASOF_VOLUME"
            elif recent["Close"].tail(200).notna().sum() < 200:
                reason = "INSUFFICIENT_200_SESSION_TREND"
            else:
                continue
            impaired.append(ticker)
            impairment_reasons[ticker] = reason
    # Prioritise the benchmark, then missing symbols, then unusable features.
    # Keep the same symbol budget across re-checks and secondary attempts.
    priority_missing = sorted(missing, key=lambda t: (t != "SPY", names.index(t)))
    available = max(0, min(int(max_fallback), MAX_FALLBACK_SYMBOLS))
    # Otherwise the same last few partial symbols are starved forever.
    # Rotate only when there are more incomplete names than remaining
    # capacity; the benchmark session makes retries deterministic per day.
    shift = 0
    if asof is not None and len(impaired) > max(0, available - len(priority_missing)):
        shift = int(asof.toordinal()) % len(impaired)
    rotated = impaired[shift:] + impaired[:shift]
    ordered = priority_missing + rotated
    selected = ordered[:available]
    # When the default Yahoo batch loader is used, re-check incomplete symbols
    # individually first: a single-symbol chart can resolve partial batch reads.
    # Injected loaders remain entirely offline unless single_loader is provided.
    retry = single_loader if single_loader is not None else (
        yahoo_single_bars if yahoo_loader is None else None
    )
    retry_recovered = []
    retry_rejected = {}
    if retry is not None:
        for ticker in selected:
            try:
                candidate = normalize(retry(ticker, period))
                quality = ("NO_VALID_BENCHMARK" if asof is None and ticker != "SPY"
                           else _replacement_quality(candidate, bars.get(ticker), benchmark, asof))
                if quality is not None:
                    retry_rejected[ticker] = quality
                    continue
                bars[ticker] = candidate
                providers[ticker] = "YAHOO_SINGLE_RECHECK_ADJUSTED"
                retry_recovered.append(ticker)
                if ticker == "SPY":
                    benchmark, asof = candidate, candidate.index[-1]
            except Exception as exc:
                retry_rejected[ticker] = type(exc).__name__
                errors.append("YAHOO_SINGLE_" + type(exc).__name__)
    attempted = [t for t in selected if t not in retry_recovered]
    secondary = fallback or stooq_bars
    recovered_impaired = []
    rejected_secondary = {}
    for ticker in attempted:
        try:
            frame = normalize(secondary(ticker))
            quality = ("NO_VALID_BENCHMARK" if asof is None and ticker != "SPY"
                       else _replacement_quality(frame, bars.get(ticker), benchmark, asof))
            if quality is not None:
                rejected_secondary[ticker] = quality
                continue
            if ticker in impaired:
                recovered_impaired.append(ticker)
            bars[ticker] = frame
            providers[ticker] = (
                "STOOQ_SECONDARY_ADJUSTMENT_CHECKED"
                if ticker in recovered_impaired else "STOOQ_SECONDARY_ADJUSTMENT_NOT_VERIFIED"
            )
            if ticker == "SPY":
                benchmark, asof = frame, frame.index[-1]
        except Exception as exc:
            errors.append("STOOQ_" + type(exc).__name__)
            LOG.info("H083 secondary unavailable %s: %s", ticker, type(exc).__name__)
    report = {
        "requested": len(names),
        "loaded": len(bars),
        "missing": len(names) - len(bars),
        "fallback_attempted": len(attempted),
        "fallback_recovered": sum(t in bars for t in attempted if t in missing) + len(recovered_impaired),
        "single_recheck_attempted": len(selected) if retry is not None else 0,
        "single_recheck_recovered": len(retry_recovered),
        "single_recheck_rejected": retry_rejected,
        "unreached_due_to_budget": len(ordered) - len(selected),
        "recovery_rotation": shift,
        "partial_series_detected": len(impaired),
        "partial_series_recovered": len(recovered_impaired) + sum(t in impaired for t in retry_recovered),
        "partial_series_reasons": impairment_reasons,
        "secondary_rejected": rejected_secondary,
        "fallback_budget": MAX_FALLBACK_SYMBOLS,
        "providers": providers,
        "errors": errors[:12],
        "note": "Single Yahoo re-check is not independent confirmation; no bar stitching, calendar gaps or OHLCV fabrication.",
    }
    return bars, report
