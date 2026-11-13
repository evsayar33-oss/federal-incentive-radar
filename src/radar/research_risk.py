"""Reusable, fail-closed research metrics and hypothetical portfolio risk gates.

This module does not place orders, modify strategy status, or imply alpha.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Sequence


@dataclass(frozen=True)
class Expectancy:
    n: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    cost_r: float
    net_expectancy_r: float
    profit_factor: float | None
    status: str = "DESCRIPTIVE_ONLY_NOT_VALIDATED"


def expectancy(outcomes_r: Sequence[float], *, roundtrip_cost_r: float = 0.0) -> Expectancy:
    """Calculate descriptive net-R expectation; no alpha claims from samples."""
    if not math.isfinite(roundtrip_cost_r) or roundtrip_cost_r < 0:
        raise ValueError("cost must be finite and nonnegative")
    values = [float(v) for v in outcomes_r]
    if not values or any(not math.isfinite(v) for v in values):
        raise ValueError("outcomes must be nonempty and finite")
    net = [v - roundtrip_cost_r for v in values]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    gross_wins, gross_losses = sum(wins), sum(losses)
    return Expectancy(
        n=len(net),
        win_rate=len(wins) / len(net),
        avg_win_r=gross_wins / len(wins) if wins else 0.0,
        avg_loss_r=gross_losses / len(losses) if losses else 0.0,
        cost_r=roundtrip_cost_r,
        net_expectancy_r=sum(net) / len(net),
        profit_factor=gross_wins / gross_losses if gross_losses > 0 else None,
    )


def risk_gate(
    *, entry: float, stop: float, target: float, quantity: int,
    equity: float, cash: float, existing_exposure: float = 0.0,
    max_position_pct: float = 0.10, max_portfolio_exposure_pct: float = 0.80,
    max_loss_per_trade_pct: float = 0.005, min_reward_risk: float = 1.5,
    validated_alpha: bool = False,
) -> dict:
    """Long-only research gate. Unknown alpha always blocks trade authorization."""
    values = [entry, stop, target, equity, cash, existing_exposure,
              max_position_pct, max_portfolio_exposure_pct,
              max_loss_per_trade_pct, min_reward_risk]
    if any(not math.isfinite(v) for v in values) or quantity <= 0 or not isinstance(quantity, int):
        return {"decision": "BLOCK", "reasons": ["INVALID_INPUT"]}
    if equity <= 0 or cash < 0 or existing_exposure < 0 or not (0 < stop < entry < target):
        return {"decision": "BLOCK", "reasons": ["INVALID_INPUT"]}
    if not (0 < max_position_pct <= 1 and 0 < max_portfolio_exposure_pct <= 1
            and 0 < max_loss_per_trade_pct <= 1 and min_reward_risk > 0):
        return {"decision": "BLOCK", "reasons": ["INVALID_LIMITS"]}
    notional = entry * quantity
    at_risk = (entry - stop) * quantity
    reward_risk = (target - entry) / (entry - stop)
    reasons = []
    if not validated_alpha:
        reasons.append("UNVALIDATED_ALPHA")
    if notional > cash:
        reasons.append("INSUFFICIENT_CASH")
    if notional > equity * max_position_pct:
        reasons.append("POSITION_CAP")
    if existing_exposure + notional > equity * max_portfolio_exposure_pct:
        reasons.append("PORTFOLIO_EXPOSURE_CAP")
    if at_risk > equity * max_loss_per_trade_pct:
        reasons.append("RISK_CAP")
    if reward_risk < min_reward_risk:
        reasons.append("REWARD_RISK_BELOW_MIN")
    return {"decision": "BLOCK" if reasons else "PASS", "reasons": reasons,
            "notional_usd": round(notional, 2), "stop_risk_usd": round(at_risk, 2),
            "reward_risk": round(reward_risk, 4),
            "note": "Research gate only; stops may slip and PASS is not an order."}
