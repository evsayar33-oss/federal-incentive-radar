"""Prospective-only corporate relationship ledger (never a trading authorization).

Keeps issuer roles separate from the legacy investment evidence population.
Observation time is when Radar first saw the headline, NOT RSS publication time.
No source headline alone can graduate to a verified agreement.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

import pandas as pd

from . import store
from .economic_intelligence import extract_relationship, match_relationship
from .entity import Matcher, normalize

LOG = logging.getLogger("radar.relationship_pipeline")

COLUMNS = [
    "event_id", "source_event_id", "observed_at_utc", "published_at_utc",
    "headline", "source_url", "source_company", "target_company",
    "source_role", "target_role", "relation", "transaction_status",
    "amount_usd", "source_ticker", "target_ticker",
    "source_match_method", "target_match_method", "evidence",
    "verification_status", "primary_source_url", "validated_alpha", "decision",
]


def _resolver(universe: pd.DataFrame):
    if universe is None or universe.empty or not {"name", "ticker"}.issubset(universe):
        return lambda _name: (None, "NO_UNIVERSE")
    uni = universe.dropna(subset=["name", "ticker"]).copy()
    uni["norm"] = uni["name"].astype(str).map(normalize)
    collisions = set(
        uni.groupby("norm")["ticker"].nunique().loc[lambda s: s > 1].index
    )
    matcher = Matcher(universe)

    def resolve(name: str) -> tuple[str | None, str]:
        ticker, _score, method = matcher.match(name)
        if normalize(name) in collisions and method != "override":
            return None, "AMBIGUOUS_ENTITY"
        if method not in {"exact", "override"} or not ticker:
            return None, "REVIEW_REQUIRED" if method in {"review", "fuzzy"} else method
        return str(ticker).upper(), method

    return resolve


def build_relationships(
    newly_observed: pd.DataFrame, universe: pd.DataFrame, now_utc: datetime,
) -> pd.DataFrame:
    """Do not replay old headlines as if their observation was prospective."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now = pd.Timestamp(now_utc).tz_convert("UTC")
    if newly_observed is None or newly_observed.empty:
        return pd.DataFrame(columns=COLUMNS)
    resolve = _resolver(universe)
    rows = []
    for row in newly_observed.to_dict("records"):
        url = str(row.get("url") or "").strip()
        relationship = extract_relationship(str(row.get("title") or ""), url, now.isoformat())
        if relationship is None:
            continue
        published = pd.to_datetime(row.get("known_at"), utc=True, errors="coerce")
        if pd.isna(published) or published > now:
            continue
        record = match_relationship(relationship, resolve)
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        record.update({
            "event_id": event_id,
            "source_event_id": event_id,
            "headline": str(row.get("title") or "")[:500],
            "published_at_utc": published.isoformat(),
            "verification_status": "PRIMARY_SOURCE_REQUIRED",
            "primary_source_url": "",
        })
        rows.append(record)
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("event_id", keep="first")


def record_new(
    newly_observed: pd.DataFrame, now_utc: datetime | None = None,
) -> dict:
    """Persist first-observed role rows without rewriting prior observed timestamps."""
    now = now_utc or datetime.now(timezone.utc)
    observed = build_relationships(newly_observed, store.read("universe"), now)
    old = store.read("economic_relationships")
    known = set(old["event_id"].astype(str)) if not old.empty and "event_id" in old else set()
    additions = observed[~observed.event_id.astype(str).isin(known)] if not observed.empty else observed
    if not additions.empty:
        store.append("economic_relationships", additions)
    report = {
        "ok": True,
        "new_relationships": len(additions),
        "total_relationships": len(known) + len(additions),
        "pending_primary_verification": len(additions),
        "generated_at_utc": pd.Timestamp(now).tz_convert("UTC").isoformat(),
        "research_only": True,
        "note": "Secondary headlines and first-seen timestamps are NOT confirmed legal agreements.",
    }
    store.save_json("economic_intelligence_status", report)
    return report
