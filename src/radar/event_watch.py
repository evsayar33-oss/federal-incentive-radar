"""H083-derived *research watchlist*, not event alpha, a trade plan, or a broker signal.

The H083 results were retrospective and did not beat SPY statistically. This
module only evaluates predeclared low-beta and ATR-compression monthly screens
on listed tickers that have a Radar event. It never edits the proven-evidence
registry and does not authorize orders.

Feature close is strictly BEFORE the hypothetical next open. All ranks are
marked WATCH_ONLY_UNVALIDATED, including source first-seen (forward-only) rows.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import store
from .settings import store_dir

log = logging.getLogger("radar.event_watch")
RULES = ("monthly_low_beta", "monthly_vol_squeeze")
MAX_TICKERS = 180
CANDIDATE_COLS = [
    "rule", "rank", "ticker", "score", "status", "validated_alpha", "decision",
    "signal_frequency", "feature_asof", "generated_at_utc", "market_regime",
    "beta126", "mom63", "dollar_volume20", "atr14_ratio", "atr126_p20",
    "trend50", "trend200", "event_id", "event_family", "category",
    "event_date", "source", "source_url", "timestamp_quality", "evidence_scope",
    "midas_buy_usd", "midas_sell_usd", "midas_roundtrip_usd",
]
EXCLUSION_COLS = ["ticker", "reason"]
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
QUALITIES = {"exact_utc", "date_only", "first_seen_utc"}


def eligible_events(feed: pd.DataFrame, now_utc: datetime) -> pd.DataFrame:
    """Reject source action-date proxies and future/unobserved timestamps.

    first_seen_utc is prospective ONLY. It is never historical source evidence.
    """
    if feed is None or feed.empty:
        return pd.DataFrame()
    required = {"ticker", "timestamp_quality", "event_date", "event_id", "known_at"}
    if not required.issubset(feed.columns):
        raise ValueError("Watch feed missing " + str(sorted(required - set(feed.columns))))
    now = pd.Timestamp(now_utc)
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now = now.tz_convert("UTC")
    work = feed.copy()
    work["ticker"] = work["ticker"].fillna("").astype(str).str.strip().str.upper()
    work["timestamp_quality"] = work["timestamp_quality"].fillna("").astype(str).str.lower().str.strip()
    work["event_date"] = pd.to_datetime(work["event_date"], errors="coerce").dt.normalize()
    current_date = now.tz_convert("America/New_York").tz_localize(None).normalize()
    work = work.loc[
        work["timestamp_quality"].isin(QUALITIES)
        & work["ticker"].map(lambda t: bool(TICKER_RE.fullmatch(t)))
        & work["event_date"].notna() & work["event_date"].le(current_date)
    ].copy()
    if work.empty:
        return work
    known = pd.to_datetime(work["known_at"], errors="coerce", utc=True, format="mixed")
    # Never promote a date proxy; date_only is an explicitly known event date.
    explicit = work["timestamp_quality"].isin({"exact_utc", "first_seen_utc"})
    has_zone = work["known_at"].astype(str).str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    work = work.loc[(~explicit) | (has_zone & known.notna() & known.le(now))].copy()
    work["evidence_scope"] = np.where(
        work["timestamp_quality"].eq("first_seen_utc"), "FORWARD_ONLY", "SOURCE_DATED"
    )
    work["_known_sort"] = pd.to_datetime(work["known_at"], errors="coerce", utc=True, format="mixed")
    work["_known_sort"] = work["_known_sort"].fillna(
        work["event_date"].dt.tz_localize("UTC") + pd.Timedelta(hours=23, minutes=59)
    )
    return work.sort_values(["event_date", "_known_sort", "event_id"]).drop_duplicates(
        "ticker", keep="last"
    ).reset_index(drop=True)


def _regular_bars(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"Open", "High", "Low", "Close", "Volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=sorted(required))
    out = frame[list(("Open", "High", "Low", "Close", "Volume"))].copy()
    out.index = pd.DatetimeIndex(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_convert(None)
    out.index = out.index.normalize()
    out = out.loc[~out.index.duplicated(keep="last")].sort_index()
    for name in required:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    return out


LAST_PRICE_REPORT: dict = {}


def download_daily_bars(tickers: list[str], period: str = "3y") -> dict[str, pd.DataFrame]:
    """Bounded per-symbol OHLCV with provider audit; never stitches prices."""
    from .price_resilience import download_bars
    global LAST_PRICE_REPORT
    bars, report = download_bars(tickers, period)
    LAST_PRICE_REPORT = report
    return bars


def features_asof(stock: pd.DataFrame, spy: pd.DataFrame, asof: pd.Timestamp) -> dict | None:
    """H083 feature definitions evaluated at a COMPLETED close only."""
    benchmark = _regular_bars(spy)
    p = _regular_bars(stock)
    calendar = benchmark.index[benchmark.index <= pd.Timestamp(asof).normalize()]
    if len(calendar) < 215:
        return None
    p = p.reindex(calendar)
    benchmark = benchmark.reindex(calendar)
    close, b = p["Close"], benchmark["Close"]
    volume = p["Volume"]
    if pd.isna(close.iloc[-1]) or pd.isna(volume.iloc[-1]) or volume.iloc[-1] <= 0:
        return None
    a_ret = close.pct_change(fill_method=None)
    b_ret = b.pct_change(fill_method=None)
    covariance = a_ret.rolling(126, min_periods=80).cov(b_ret).iloc[-1]
    variance = b_ret.rolling(126, min_periods=80).var().iloc[-1]
    beta = covariance / variance if pd.notna(variance) and variance > 0 else np.nan
    liquidity = (close * volume).rolling(20, min_periods=20).mean().iloc[-1]
    mom63 = close.iloc[-1] / close.iloc[-64] - 1 if pd.notna(close.iloc[-64]) and close.iloc[-64] > 0 else np.nan
    trend50 = bool(close.iloc[-1] > close.rolling(50, min_periods=50).mean().iloc[-1])
    trend200 = bool(close.iloc[-1] > close.rolling(200, min_periods=200).mean().iloc[-1])
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(p.High - p.Low).abs(), (p.High - prev_close).abs(), (p.Low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_ratio = true_range.rolling(14, min_periods=14).mean() / close
    atr = atr_ratio.iloc[-1]
    atr20 = atr_ratio.rolling(126, min_periods=80).quantile(0.2).iloc[-1]
    market_ma = b.rolling(200, min_periods=200).mean().iloc[-1]
    risk_on = bool(pd.notna(market_ma) and b.iloc[-1] > market_ma)
    return {
        "beta126": float(beta) if pd.notna(beta) else np.nan,
        "mom63": float(mom63) if pd.notna(mom63) else np.nan,
        "dollar_volume20": float(liquidity) if pd.notna(liquidity) else np.nan,
        "atr14_ratio": float(atr) if pd.notna(atr) else np.nan,
        "atr126_p20": float(atr20) if pd.notna(atr20) else np.nan,
        "trend50": trend50, "trend200": trend200,
        "market_regime": "RISK_ON" if risk_on else "RISK_OFF",
        "feature_asof": str(calendar[-1].date()),
    }


def evaluate_watch(
    feed: pd.DataFrame, bars: dict[str, pd.DataFrame], now_utc: datetime,
    *, max_slots: int = 5, asof: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return two ranked, NON-TRADE watchlists with missing-data audit."""
    eligible = eligible_events(feed, now_utc)
    empty = pd.DataFrame(columns=CANDIDATE_COLS)
    excluded = []
    if "SPY" not in bars or bars["SPY"] is None or bars["SPY"].empty:
        return empty, pd.DataFrame([{"ticker": "SPY", "reason": "NO_BENCHMARK"}], columns=EXCLUSION_COLS), {
            "ok": False, "reason": "NO_BENCHMARK", "eligible_tickers": len(eligible),
            "candidates": 0,
        }
    now = pd.Timestamp(now_utc).tz_convert("America/New_York")
    # At Radar's usual 16:19 ET run, the same-day Yahoo bar may be incomplete.
    complete_cutoff = pd.Timestamp(asof).normalize() if asof is not None else (
        pd.Timestamp(now.date()) if now.time() >= time(16, 35)
        else pd.Timestamp(now.date()) - pd.Timedelta(days=1)
    )
    benchmark = _regular_bars(bars["SPY"])
    valid_sessions = benchmark.index[benchmark.index <= complete_cutoff]
    if len(valid_sessions) < 215:
        return empty, pd.DataFrame([{"ticker": "SPY", "reason": "INSUFFICIENT_BENCHMARK_HISTORY"}], columns=EXCLUSION_COLS), {
            "ok": False, "reason": "INSUFFICIENT_BENCHMARK_HISTORY",
            "eligible_tickers": len(eligible), "candidates": 0,
        }
    feature_at = valid_sessions[-1]
    qualified = {rule: [] for rule in RULES}
    feature_covered = 0
    liquid_count = 0
    for row in eligible.to_dict("records"):
        ticker = str(row["ticker"])
        if ticker not in bars:
            excluded.append({"ticker": ticker, "reason": "PRICE_MISSING"})
            continue
        feature = features_asof(bars[ticker], benchmark, feature_at)
        if feature is None:
            excluded.append({"ticker": ticker, "reason": "FEATURE_HISTORY_OR_LAST_PRICE_MISSING"})
            continue
        feature_covered += 1
        liquid = np.isfinite(feature["dollar_volume20"]) and feature["dollar_volume20"] >= 5_000_000
        if not liquid:
            excluded.append({"ticker": ticker, "reason": "NOT_LIQUID_5M"})
            continue
        liquid_count += 1
        common = {
            **feature,
            "ticker": ticker, "event_id": str(row.get("event_id", "")),
            "event_family": str(row.get("event_family", "")),
            "category": str(row.get("category", "")),
            "event_date": str(row["event_date"].date()),
            "source": str(row.get("source", "")),
            "source_url": str(row.get("url", "")),
            "timestamp_quality": str(row["timestamp_quality"]),
            "evidence_scope": str(row["evidence_scope"]),
            "status": "WATCH_ONLY_UNVALIDATED",
            "validated_alpha": False,
            "decision": "NO_TRADE_AUTHORIZATION",
            "signal_frequency": "MONTHLY_ROTATION_RESEARCH",
            "generated_at_utc": pd.Timestamp(now_utc).isoformat(),
            "midas_buy_usd": 1.50, "midas_sell_usd": 1.50, "midas_roundtrip_usd": 3.0,
        }
        beta, momentum = feature["beta126"], feature["mom63"]
        if feature["trend200"] and np.isfinite(beta) and 0 <= beta <= 1 and np.isfinite(momentum) and momentum > 0:
            qualified["monthly_low_beta"].append({"rule": "monthly_low_beta", "score": 1.0 - beta, **common})
        if feature["trend50"] and np.isfinite(momentum) and np.isfinite(feature["atr14_ratio"]) and np.isfinite(feature["atr126_p20"]) and feature["atr14_ratio"] < feature["atr126_p20"]:
            qualified["monthly_vol_squeeze"].append({"rule": "monthly_vol_squeeze", "score": momentum, **common})
    picks = []
    for rule in RULES:
        ordered = sorted(qualified[rule], key=lambda r: (-r["score"], r["ticker"]))
        for rank, row in enumerate(ordered[:max_slots], 1):
            picks.append({"rank": rank, **row})
    result = pd.DataFrame(picks, columns=CANDIDATE_COLS)
    audit = pd.DataFrame(excluded, columns=EXCLUSION_COLS)
    status = {
        "ok": True, "watch_only": True, "validated_alpha": False,
        "feature_asof": str(feature_at.date()),
        "generated_at_utc": pd.Timestamp(now_utc).isoformat(),
        "eligible_tickers": len(eligible),
        "priced_tickers": feature_covered,
        "liquid_tickers": liquid_count,
        "excluded_tickers": len(audit), "candidates": len(result),
        "exclusion_reasons": {str(k): int(v) for k, v in audit.reason.value_counts().items()},
        # Liquidity is a strategy eligibility filter, not a price feed outage.
        "data_coverage_pct": round(feature_covered / len(eligible), 4) if len(eligible) else 1.0,
        "data_coverage_ok": bool(len(eligible) == 0 or feature_covered / len(eligible) >= 0.8),
        "decision": "WATCH_ONLY_NO_TRADE",
        "by_rule": {rule: int((result.rule == rule).sum()) for rule in RULES},
        "source_first_seen_forward_only": int(eligible["timestamp_quality"].eq("first_seen_utc").sum()) if len(eligible) else 0,
        "logic_source": "H083 monthly_low_beta and monthly_vol_squeeze",
        "risk_note": "Historically profitable samples did not establish statistically significant SPY-relative alpha",
    }
    if not status["data_coverage_ok"]:
        status["ok"] = False
        status["reason"] = "INSUFFICIENT_PRICE_COVERAGE"
        # Fail closed: never publish candidates from a materially incomplete sample.
        result = empty
        status["candidates"] = 0
        status["by_rule"] = {rule: 0 for rule in RULES}
    return result, audit, status


def run() -> int:
    """After-close job; publishes an empty table if unavailable, never stale signals."""
    now = datetime.now(timezone.utc)
    et = now.astimezone(ZoneInfo("America/New_York"))
    # Scheduled workflows also run open/midday; no extra market-data load then.
    if et.time() < time(16, 0):
        print("[event-watch] pre-close skip; no update")
        return 0
    try:
        feed_path = store_dir() / "event_trade_feed.csv"
        if not feed_path.exists():
            raise FileNotFoundError(f"Event feed missing: {feed_path}")
        feed = pd.read_csv(feed_path, low_memory=False)
        eligible = eligible_events(feed, now)
        # Deterministic and transparent cap; most recently observed source events first.
        eligible = eligible.sort_values(["event_date", "_known_sort", "ticker"], ascending=False) if not eligible.empty else eligible
        capped = eligible.head(MAX_TICKERS)
        universe = ["SPY"] + sorted(capped["ticker"].unique().tolist())
        bars = download_daily_bars(universe)
        result, audit, status = evaluate_watch(capped, bars, now)
        status["universe_capped"] = int(max(0, len(eligible) - len(capped)))
        status["price_download"] = dict(LAST_PRICE_REPORT)
    except Exception as exc:
        log.exception("H083 watch research unavailable; publishing empty snapshot")
        result = pd.DataFrame(columns=CANDIDATE_COLS)
        audit = pd.DataFrame([{"ticker": "*", "reason": "RUNTIME_ERROR"}], columns=EXCLUSION_COLS)
        status = {"ok": False, "watch_only": True, "candidates": 0,
                  "error": type(exc).__name__, "validated_alpha": False,
                  "decision": "NO_TRADE_AUTHORIZATION"}
    store.write("event_watch_candidates", result)
    store.write("event_watch_exclusions", audit)
    store.save_json("event_watch_status", status)
    if len(result):
        history = result.assign(snapshot_date=et.date().isoformat())
        store.upsert("event_watch_history", history,
                     ["snapshot_date", "rule", "ticker"], keep="last")
    print("[event-watch] " + json.dumps(status, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
