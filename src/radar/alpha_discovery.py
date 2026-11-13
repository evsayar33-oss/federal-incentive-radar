"""Independent, pre-registered Radar Alpha Discovery V1.

Isolation: reads immutable-in-process snapshots of the published data branch;
never imports H083 forward cohorts/gates, sends Telegram, or places orders.
The current-universe/price-history dataset is NOT point-in-time survivorship-free.
Consequently, even significant retrospective results remain research candidates.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HORIZON = 20
ASSET_COST = 0.0025   # Hypothetical 25bp roundtrip incl. fixed fees/slippage
SPY_COST = 0.0005     # Hypothetical benchmark 5bp roundtrip
SPLIT = pd.Timestamp("2025-01-01")
HOLDOUT_START = pd.Timestamp("2025-02-01")  # One-month temporal embargo
MIN_DEV = 40
MIN_HOLDOUT = 40
MIN_HOLDOUT_MONTHS = 12
SEED = 20261113
PRE_REGISTERED_AT = pd.Timestamp("2026-11-13T19:54:31Z")
TIMESTAMP_PATTERN = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.I)

PREDECLARED = (
    ("FEDERAL_DIRECT_20D", "Direct federal-award recipient, next close then 20 closes"),
    ("POLICY_REGULATORY_20D", "Policy/regulatory directly matched issuer, next close then 20 closes"),
    ("FEDERAL_PRESTRENGTH_20D", "Federal recipient with positive prior 20-session SPY-relative momentum"),
    ("FEDERAL_SECTOR_LEAD_20D", "Federal recipient whose matched sector ETF outperformed SPY in prior 20 sessions"),
    ("DIRECT_PRIVATE_RECIPIENT_20D", "First-observed directly named private-investment recipient"),
)
TRADE_COLUMNS = [
    "hypothesis", "event_id", "event_source", "ticker", "signal_known_utc",
    "signal_date_et", "entry_date", "exit_date", "entry_close", "exit_close",
    "spy_entry_close", "spy_exit_close", "gross_return", "net_return",
    "spy_net_return", "net_excess_vs_spy", "stage", "eligible_forward",
]
SUMMARY_COLUMNS = [
    "hypothesis", "description", "development_n", "holdout_n", "holdout_months",
    "holdout_mean_net", "holdout_hit_rate", "holdout_mean_excess",
    "holdout_excess_bootstrap_95_lower", "holdout_positive_month_p",
    "bh_q", "status", "reason", "preregistered_matured_n",
    "validated_alpha", "broker_authorized",
]


def load_data(prices_path: str | Path, feed_path: str | Path,
              relationships_path: str | Path | None = None):
    prices = pd.read_parquet(prices_path, columns=["date", "ticker", "close"])
    feed = pd.read_csv(feed_path, low_memory=False)
    relationships = pd.DataFrame()
    if relationships_path and Path(relationships_path).exists():
        relationships = pd.read_parquet(relationships_path)
    return prices, feed, relationships


def prepare_prices(raw: pd.DataFrame) -> tuple[dict[str, pd.Series], dict]:
    required = {"date", "ticker", "close"}
    if not required.issubset(raw):
        raise ValueError("prices dataset missing " + str(sorted(required - set(raw))))
    data = raw.loc[:, ["date", "ticker", "close"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["ticker"] = data["ticker"].fillna("").astype(str).str.strip().str.upper()
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna(subset=["date", "close"])
    data = data.loc[data.close.gt(0) & data.ticker.ne("")]
    data = data.drop_duplicates(["ticker", "date"], keep="last")
    series = {ticker: x.set_index("date").sort_index().close.astype(float)
              for ticker, x in data.groupby("ticker", sort=False)}
    if "SPY" not in series or len(series["SPY"]) < 250:
        raise ValueError("SPY missing or insufficient benchmark sessions")
    meta = {
        "rows": int(len(data)), "symbols": int(len(series)),
        "spy_first": str(series["SPY"].index.min().date()),
        "spy_last": str(series["SPY"].index.max().date()),
        "source": "existing_data_branch_prices_close_only",
        "snapshot_is_point_in_time": False,
    }
    return series, meta


def _known_utc(raw: object) -> pd.Timestamp | None:
    value = str(raw or "").strip()
    if not TIMESTAMP_PATTERN.search(value):
        return None
    stamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(stamp) else stamp


def prepare_events(feed: pd.DataFrame, relationships: pd.DataFrame,
                   latest_close: pd.Timestamp) -> tuple[list[dict], dict]:
    """Require explicit timestamp and reject retrospective RSS publication proxies."""
    required = {"ticker", "known_at", "timestamp_quality", "event_id", "event_family", "source"}
    if not required.issubset(feed):
        raise ValueError("event_trade_feed missing " + str(sorted(required - set(feed))))
    rows = []
    rejected = {"missing_timestamp": 0, "not_causal_quality": 0, "private_rss_proxy": 0,
                "nonlisted_symbol": 0, "outside_price_period": 0}
    for event in feed.to_dict("records"):
        family = str(event.get("event_family") or "")
        if family not in {"federal_award", "policy_regulatory"}:
            # Corporate RSS pubDate may precede when the backfill first saw it.
            if family == "corporate_capex":
                rejected["private_rss_proxy"] += 1
            continue
        quality = str(event.get("timestamp_quality") or "").strip()
        if quality not in {"exact_utc", "first_seen_utc"}:
            rejected["not_causal_quality"] += 1
            continue
        stamp = _known_utc(event.get("known_at"))
        if stamp is None:
            rejected["missing_timestamp"] += 1
            continue
        ticker = str(event.get("ticker") or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", ticker):
            rejected["nonlisted_symbol"] += 1
            continue
        day = stamp.tz_convert("America/New_York").tz_localize(None).normalize()
        if day >= latest_close:
            rejected["outside_price_period"] += 1
            continue
        rows.append({
            "event_id": str(event.get("event_id") or ""),
            "family": family, "source": str(event.get("source") or ""),
            "ticker": ticker, "known": stamp, "day": day,
            "sector_etf": str(event.get("sector_etf") or "").upper().strip(),
        })
    relationship_candidates = 0
    if relationships is not None and not relationships.empty:
        required_relationship = {
            "event_id", "observed_at_utc", "target_ticker", "relation", "source_url"
        }
        if required_relationship.issubset(relationships):
            for r in relationships.to_dict("records"):
                if str(r.get("relation")) != "STRATEGIC_INVESTMENT":
                    continue
                stamp = _known_utc(r.get("observed_at_utc"))
                ticker = str(r.get("target_ticker") or "").strip().upper()
                if stamp is None or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", ticker):
                    continue
                day = stamp.tz_convert("America/New_York").tz_localize(None).normalize()
                if day >= latest_close:
                    continue
                relationship_candidates += 1
                rows.append({
                    "event_id": str(r.get("event_id") or ""),
                    "family": "direct_private_recipient", "source": "economic_relationships",
                    "ticker": ticker, "known": stamp, "day": day, "sector_etf": "",
                })
    rows.sort(key=lambda r: (r["known"], r["ticker"], r["event_id"]))
    diag = {
        "feed_rows": int(len(feed)), "causal_direct_events": int(
            sum(x["family"] != "direct_private_recipient" for x in rows)
        ), "prospective_direct_private_rows": relationship_candidates,
        "rejected": rejected,
        "private_rss_historical_replay_disabled": True,
    }
    return rows, diag


def _trade_for(event: dict, hypothesis: str, prices: dict[str, pd.Series]) -> dict | None:
    stock = prices.get(event["ticker"])
    spy = prices["SPY"]
    if stock is None:
        return None
    # Align to actual benchmark sessions, never forward-fill missing closes.
    days = spy.index
    entry_pos = int(days.searchsorted(event["day"], side="right"))
    exit_pos = entry_pos + HORIZON
    if entry_pos < 25 or exit_pos >= len(days):
        return None
    signal_pos = entry_pos - 1
    entry_date, exit_date = days[entry_pos], days[exit_pos]
    signal_date = days[signal_pos]
    needed = [signal_date, entry_date, exit_date]
    if any(day not in stock.index for day in needed):
        return None
    base = float(stock.loc[entry_date])
    sale = float(stock.loc[exit_date])
    spy_base, spy_sale = float(spy.loc[entry_date]), float(spy.loc[exit_date])
    if not all(math.isfinite(x) and x > 0 for x in (base, sale, spy_base, spy_sale)):
        return None

    if hypothesis in {"FEDERAL_PRESTRENGTH_20D", "FEDERAL_SECTOR_LEAD_20D"}:
        before_pos = signal_pos - 20
        if before_pos < 0:
            return None
        before_day = days[before_pos]
        if hypothesis == "FEDERAL_PRESTRENGTH_20D":
            if before_day not in stock.index:
                return None
            signal_strength = float(stock.loc[signal_date] / stock.loc[before_day]
                                    - spy.loc[signal_date] / spy.loc[before_day])
        else:
            sector_ticker = event.get("sector_etf", "")
            sector = prices.get(sector_ticker)
            if sector is None or before_day not in sector.index or signal_date not in sector.index:
                return None
            signal_strength = float(sector.loc[signal_date] / sector.loc[before_day]
                                    - spy.loc[signal_date] / spy.loc[before_day])
        if not math.isfinite(signal_strength) or signal_strength <= 0:
            return None

    gross = sale / base - 1
    net = gross - ASSET_COST
    benchmark_net = spy_sale / spy_base - 1 - SPY_COST
    return {
        "hypothesis": hypothesis,
        "event_id": event["event_id"], "event_source": event["source"],
        "ticker": event["ticker"], "signal_known_utc": event["known"].isoformat(),
        "signal_date_et": str(event["day"].date()),
        "entry_date": str(entry_date.date()), "exit_date": str(exit_date.date()),
        "entry_close": base, "exit_close": sale,
        "spy_entry_close": spy_base, "spy_exit_close": spy_sale,
        "gross_return": float(gross), "net_return": float(net),
        "spy_net_return": float(benchmark_net),
        "net_excess_vs_spy": float(net - benchmark_net),
        "stage": "DEVELOPMENT" if exit_date < SPLIT else (
            "HOLDOUT" if entry_date >= HOLDOUT_START else "EMBARGO"
        ),
        "eligible_forward": bool(entry_date >= HOLDOUT_START),
        "preregistered_observation": bool(event["known"] >= PRE_REGISTERED_AT),
    }


def run_hypotheses(events: list[dict], prices: dict[str, pd.Series]) -> pd.DataFrame:
    results = []
    for hypothesis, _desc in PREDECLARED:
        held_until: dict[str, pd.Timestamp] = {}
        for event in events:
            family = event["family"]
            if hypothesis == "DIRECT_PRIVATE_RECIPIENT_20D":
                if family != "direct_private_recipient":
                    continue
            elif hypothesis == "POLICY_REGULATORY_20D":
                if family != "policy_regulatory":
                    continue
            elif family != "federal_award":
                continue
            candidate = _trade_for(event, hypothesis, prices)
            if candidate is None:
                continue
            ticker = candidate["ticker"]
            if ticker in held_until and pd.Timestamp(candidate["entry_date"]) <= held_until[ticker]:
                continue
            held_until[ticker] = pd.Timestamp(candidate["exit_date"])
            results.append(candidate)
    return pd.DataFrame(results, columns=TRADE_COLUMNS)


def _month_bootstrap(values: pd.DataFrame) -> tuple[float, float]:
    """Cluster by entry month to preserve concurrent event dependence."""
    if values.empty:
        return float("nan"), 1.0
    month = pd.to_datetime(values.entry_date).dt.to_period("M").astype(str)
    monthly = values.groupby(month).net_excess_vs_spy.mean().to_numpy(dtype=float)
    if len(monthly) < 3:
        return float("nan"), 1.0
    rng = np.random.default_rng(SEED)
    sampled = monthly[rng.integers(0, len(monthly), size=(4000, len(monthly)))].mean(axis=1)
    lower = float(np.quantile(sampled, 0.05))
    signed = monthly[None, :] * rng.choice([-1., 1.], size=(4000, len(monthly)))
    p = float((np.sum(signed.mean(axis=1) >= float(monthly.mean())) + 1) / 4001)
    return lower, p


def _bh(values: list[float]) -> list[float]:
    raw = np.asarray(values, dtype=float)
    n = len(raw)
    order = np.argsort(raw)
    adjusted = np.minimum.accumulate((raw[order] * n / np.arange(1, n + 1))[::-1])[::-1]
    result = np.empty_like(adjusted)
    result[order] = np.clip(adjusted, 0., 1.)
    return result.tolist()


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    descriptions = dict(PREDECLARED)
    records = []
    for hypothesis, _description in PREDECLARED:
        selection = trades.loc[trades.hypothesis.eq(hypothesis)] if not trades.empty else pd.DataFrame(columns=TRADE_COLUMNS)
        development = selection.loc[selection.stage.eq("DEVELOPMENT")]
        holdout = selection.loc[selection.stage.eq("HOLDOUT")]
        lower, p = _month_bootstrap(holdout)
        months = int(pd.to_datetime(holdout.entry_date).dt.to_period("M").nunique()) if len(holdout) else 0
        reasons = []
        if len(development) < MIN_DEV:
            reasons.append("INSUFFICIENT_DEVELOPMENT")
        if len(holdout) < MIN_HOLDOUT:
            reasons.append("INSUFFICIENT_HOLDOUT")
        if months < MIN_HOLDOUT_MONTHS:
            reasons.append("INSUFFICIENT_INDEPENDENT_MONTHS")
        if len(holdout) and float(holdout.net_excess_vs_spy.mean()) <= 0:
            reasons.append("NONPOSITIVE_NET_EXCESS")
        if not math.isfinite(lower) or lower <= 0:
            reasons.append("BOOTSTRAP_LOWER_NOT_POSITIVE")
        records.append({
            "hypothesis": hypothesis, "description": descriptions[hypothesis],
            "development_n": len(development), "holdout_n": len(holdout),
            "holdout_months": months,
            "holdout_mean_net": float(holdout.net_return.mean()) if len(holdout) else float("nan"),
            "holdout_hit_rate": float(holdout.net_return.gt(0).mean()) if len(holdout) else float("nan"),
            "holdout_mean_excess": float(holdout.net_excess_vs_spy.mean()) if len(holdout) else float("nan"),
            "holdout_excess_bootstrap_95_lower": lower,
            "holdout_positive_month_p": p,
            "preregistered_matured_n": int(holdout.preregistered_observation.astype(bool).sum()) if len(holdout) else 0,
            "_reasons": reasons,
        })
    q = _bh([r["holdout_positive_month_p"] for r in records])
    for row, qv in zip(records, q):
        row["bh_q"] = qv
        if qv > 0.05:
            row["_reasons"].append("NO_FDR_SIGNIFICANCE")
        row["status"] = "INSUFFICIENT_EVIDENCE" if any(x.startswith("INSUFFICIENT") for x in row["_reasons"]) else (
            "REJECTED_RETROSPECTIVE" if row["_reasons"] else "CANDIDATE_REQUIRES_PROSPECTIVE"
        )
        row["reason"] = "|".join(row.pop("_reasons")) or "RETROSPECTIVE_ONLY_PIT_UNIVERSE_NOT_VALIDATED"
        row["validated_alpha"] = False
        row["broker_authorized"] = False
    return pd.DataFrame(records, columns=SUMMARY_COLUMNS)


def execute(prices: pd.DataFrame, feed: pd.DataFrame,
            relationships: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    prices_by_ticker, price_diag = prepare_prices(prices)
    events, event_diag = prepare_events(feed, relationships if relationships is not None else pd.DataFrame(),
                                        prices_by_ticker["SPY"].index.max())
    trades = run_hypotheses(events, prices_by_ticker)
    summary = summarize(trades)
    spy_dates = prices_by_ticker["SPY"].index
    cutoff_position = max(0, len(spy_dates) - HORIZON - 1)
    oldest_matured_signal_date = str(spy_dates[cutoff_position].date())
    prospective = [event for event in events if event["known"] >= PRE_REGISTERED_AT]
    # Independent forthcoming event queue: it is NOT a paper fill or validated alpha.
    forward_queue = pd.DataFrame([
        {"event_id": e["event_id"], "source": e["source"],
         "family": e["family"], "ticker": e["ticker"],
         "first_observed_utc": e["known"].isoformat(),
         "signal_date_et": str(e["day"].date()),
         "state": "AWAITING_PREDECLARED_SCREEN_AND_20_SESSION_OUTCOME",
         "validated_alpha": False, "broker_authorized": False}
        for e in prospective
    ], columns=["event_id", "source", "family", "ticker",
                "first_observed_utc", "signal_date_et", "state",
                "validated_alpha", "broker_authorized"])
    diagnostics = {
        "status": "EXPLORATORY_RESEARCH_ONLY",
        "predeclared_hypotheses": len(PREDECLARED),
        "horizon_sessions": HORIZON,
        "split_date": str(SPLIT.date()),
        "holdout_from": str(HOLDOUT_START.date()),
        "roundtrip_cost_assumption": {"stock": ASSET_COST, "benchmark": SPY_COST},
        "price_snapshot": price_diag, "event_snapshot": event_diag,
        "total_trades": len(trades),
        "pre_registered_at_utc": PRE_REGISTERED_AT.isoformat(),
        "prospective_first_seen_events": int(len(prospective)),
        "preregistered_matured_trades": int(trades.preregistered_observation.astype(bool).sum()) if not trades.empty else 0,
        "oldest_signal_to_allow_full_20_session_outcome": oldest_matured_signal_date,
        "event_known_utc_min": min((e["known"].isoformat() for e in events), default=None),
        "event_known_utc_max": max((e["known"].isoformat() for e in events), default=None),
        "no_prospective_validation": True,
        "no_live_trading": True,
        "h083_forward_untouched": True,
        "limitations": [
            "Current-universe survivorship bias; not point-in-time constituents",
            "Close-to-close hypothetical fills; no real spread/market impact",
            "Historical corporate RSS dates excluded to avoid backfill timing leakage",
            "Ticker clustering and repeated/overlapping macro shocks may remain",
            "Small holdout sizes cannot establish out-of-sample alpha",
            "Predeclared and BH adjusted, but still exploratory research",
        ],
    }
    return summary, trades, diagnostics, forward_queue


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prices", required=True)
    p.add_argument("--feed", required=True)
    p.add_argument("--relationships")
    p.add_argument("--output", required=True)
    args = p.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    prices, feed, relationships = load_data(args.prices, args.feed, args.relationships)
    summary, trades, diag, queue = execute(prices, feed, relationships)
    summary.to_csv(output / "hypothesis_scorecard.csv", index=False)
    trades.to_csv(output / "observations.csv", index=False)
    queue.to_csv(output / "preregistered_forward_queue.csv", index=False)
    (output / "diagnostics.json").write_text(json.dumps(diag, indent=2, ensure_ascii=False))
    print("ALPHA_DISCOVERY_V1_REPORT_BEGIN")
    print(json.dumps({"diagnostics": diag, "hypotheses": summary.where(pd.notnull(summary), None)
                      .to_dict("records")}, default=str, ensure_ascii=False))
    print("ALPHA_DISCOVERY_V1_REPORT_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
