"""H083 prospective net/benchmark scorecard and non-executing regime stress.

Descriptive monthly portfolio returns are NOT R-multiples. This code never
turns a statistical description into a broker or alpha certification.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .price_resilience import normalize

COLUMNS = [
    "rule", "closed_months", "invalid_months", "paper_positions",
    "net_mean_monthly", "net_win_rate", "net_profit_factor",
    "mean_excess_vs_spy", "excess_bootstrap_lower_95", "max_drawdown",
    "stress_total_return", "performance_drift", "regime", "price_coverage_ok",
    "forward_evidence_status", "research_decision", "reasons",
    "broker_authorized", "evaluated_at_utc",
]


def regime_snapshot(spy: pd.DataFrame | None, asof: pd.Timestamp) -> dict:
    p = normalize(spy)
    p = p.loc[p.index <= pd.Timestamp(asof).normalize()]
    if len(p) < 220:
        return {"regime": "UNKNOWN", "reason": "INSUFFICIENT_SPY_HISTORY",
                "stress_ratio": None}
    if p.index[-1] != pd.Timestamp(asof).normalize():
        return {"regime": "UNKNOWN", "reason": "STALE_SPY_SESSION",
                "stress_ratio": None}
    close = p.Close
    rets = close.pct_change(fill_method=None)
    short = float(rets.tail(20).std(ddof=1))
    baseline = float(rets.tail(200).std(ddof=1))
    if not math.isfinite(short) or not math.isfinite(baseline) or baseline <= 0:
        return {"regime": "UNKNOWN", "reason": "INVALID_SPY_VOLATILITY",
                "stress_ratio": None}
    ratio = short / baseline
    if ratio > 1.65:
        state = "HIGH_STRESS"
    elif float(close.iloc[-1]) < float(close.tail(200).mean()):
        state = "RISK_OFF"
    else:
        state = "RISK_ON"
    return {"regime": state, "reason": "OBSERVED_MARKET_DATA",
            "stress_ratio": round(ratio, 3), "feature_asof": str(p.index[-1].date())}


def _bootstrap_lower(excess: np.ndarray) -> float:
    if len(excess) < 12:
        return float("nan")
    rng = np.random.default_rng(2041)
    block = 3
    starts = rng.integers(0, len(excess) - block + 1,
                          size=(2000, math.ceil(len(excess) / block)))
    samples = np.concatenate([starts + offset for offset in range(block)], axis=1)[:, :len(excess)]
    return float(np.quantile(excess[samples].mean(axis=1), .05))


def build_scorecard(
    results: pd.DataFrame, gates: pd.DataFrame, watch_status: dict,
    regime: dict, now_utc: datetime,
) -> pd.DataFrame:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    from .event_watch import RULES
    rows = []
    asof = now_utc.astimezone(timezone.utc).isoformat()
    for rule in RULES:
        part = (results.loc[results.rule.astype(str).eq(rule)].copy()
                if results is not None and not results.empty else pd.DataFrame())
        if not part.empty:
            part = part.sort_values("entry_month").tail(36)
            mask = part.complete.astype(str).str.lower().isin(["true", "1"])
            valid = part.loc[mask].copy()
        else:
            valid = pd.DataFrame()
        if not valid.empty:
            values = pd.to_numeric(valid.net_return, errors="coerce")
            excess = pd.to_numeric(valid.excess_vs_spy, errors="coerce")
            stress = pd.to_numeric(valid.net_return_stress, errors="coerce")
            finite = values.notna() & excess.notna() & stress.notna()
            valid = valid.loc[finite].copy()
            values, excess, stress = values.loc[finite], excess.loc[finite], stress.loc[finite]
        else:
            values, excess, stress = pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)
        positives = values[values > 0].sum()
        negatives = -values[values < 0].sum()
        compounded = (1 + values).cumprod()
        if len(values):
            wealth = np.r_[1.0, compounded.to_numpy(dtype=float)]
            max_dd = float((wealth / np.maximum.accumulate(wealth) - 1).min())
        else:
            max_dd = np.nan
        drift = False
        if len(excess) >= 12:
            prior, recent = excess.iloc[:-6], excess.iloc[-6:]
            drift = bool(recent.mean() < prior.mean() - 0.02)
        gate_row = (gates.loc[gates.rule.astype(str).eq(rule)]
                    if gates is not None and not gates.empty and "rule" in gates
                    else pd.DataFrame())
        record = gate_row.iloc[0].to_dict() if not gate_row.empty else {}
        confirmed = str(record.get("validated_for_info_plans", "")).lower() in {"true", "1"}
        coverage_ok = bool(watch_status.get("data_coverage_ok", False)) and bool(watch_status.get("ok", False))
        market = str(regime.get("regime", "UNKNOWN"))
        reasons = []
        if not confirmed:
            reasons.append("FORWARD_ALPHA_UNVALIDATED")
        if not coverage_ok:
            reasons.append("PRICE_COVERAGE_UNSAFE")
        if market in {"UNKNOWN", "HIGH_STRESS", "RISK_OFF"}:
            reasons.append("MARKET_" + market)
        if drift:
            reasons.append("NEGATIVE_PERFORMANCE_DRIFT")
        if not len(valid):
            reasons.append("NO_COMPLETED_FORWARD_MONTH")
        # This is an explanation panel, never an order permission.
        rows.append({
            "rule": rule, "closed_months": len(valid),
            "invalid_months": len(part) - len(valid),
            "paper_positions": int(pd.to_numeric(valid.n_positions, errors="coerce").fillna(0).sum()) if len(valid) else 0,
            "net_mean_monthly": float(values.mean()) if len(values) else np.nan,
            "net_win_rate": float((values > 0).mean()) if len(values) else np.nan,
            "net_profit_factor": float(positives / negatives) if negatives > 0 else np.nan,
            "mean_excess_vs_spy": float(excess.mean()) if len(excess) else np.nan,
            "excess_bootstrap_lower_95": _bootstrap_lower(excess.to_numpy(dtype=float)),
            "max_drawdown": max_dd,
            "stress_total_return": float(np.prod(1 + stress) - 1) if len(stress) else np.nan,
            "performance_drift": drift, "regime": market,
            "price_coverage_ok": coverage_ok,
            "forward_evidence_status": str(record.get("status", "NO_GATE")),
            "research_decision": "WATCH_RESEARCH_ONLY" if not reasons else "RESEARCH_HOLD",
            "reasons": "|".join(reasons) or "NO_RESEARCH_BLOCKER",
            "broker_authorized": False, "evaluated_at_utc": asof,
        })
    return pd.DataFrame(rows, columns=COLUMNS)
