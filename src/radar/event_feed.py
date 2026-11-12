"""Export a point-in-time event feed for independent event-study research.

The feed is a research interface, not a trading signal. It contains only events
with a directly matched listed ticker and preserves source timestamps when known.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import store
from .settings import sector_etfs, store_dir

FEED_COLUMNS = [
    "event_id", "event_family", "category", "source", "known_at", "event_date",
    "ticker", "title", "url", "amount_usd", "amount_type", "recipient",
    "sector", "sector_etf", "timestamp_quality",
]


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _tickers(value) -> list[str]:
    if value is None or pd.isna(value):
        return []
    return list(dict.fromkeys(
        x.strip().upper() for x in str(value).split(",")
        if x.strip() and x.strip().lower() != "nan"
    ))


def _extra(value) -> dict:
    try:
        obj = json.loads(value or "{}")
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def _known_at(value, event_date) -> tuple[str, str]:
    raw = str(value or "").strip()
    if raw and raw.lower() not in {"nan", "nat", "none"}:
        ts = pd.to_datetime(raw, utc=True, errors="coerce")
        if not pd.isna(ts) and ("T" in raw or " " in raw):
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "exact_utc"
    d = pd.to_datetime(event_date, errors="coerce")
    return (d.strftime("%Y-%m-%d") if not pd.isna(d) else ""), "date_only"


def build_feed(
    events: pd.DataFrame,
    investments: pd.DataFrame,
    universe: pd.DataFrame,
    etfs: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Build a stable event feed from classified federal events and capex news."""
    etfs = etfs or sector_etfs()
    sector_by_ticker = {}
    if universe is not None and not universe.empty and {"ticker", "sector"}.issubset(universe.columns):
        sector_by_ticker = {
            str(t).strip().upper(): str(s).strip()
            for t, s in zip(universe["ticker"], universe["sector"])
            if str(t).strip() and str(s).strip() and str(s).lower() != "nan"
        }

    rows: list[dict] = []

    def append_record(row: dict, family: str, category: str, source: str) -> None:
        tickers = _tickers(row.get("tickers", row.get("ticker", "")))
        if not tickers:
            return
        known_at, quality = _known_at(row.get("known_at"), row.get("date", row.get("event_date")))
        # USAspending historical action_date is not necessarily the first public-disclosure
        # date. Only live Radar first-seen timestamps (exact UTC) are executable evidence.
        if source == "usaspending" and quality == "date_only":
            quality = "action_date_proxy"
        amount = pd.to_numeric(row.get("amount_usd"), errors="coerce")
        for ticker in tickers:
            sector = sector_by_ticker.get(ticker, "")
            rows.append({
                "event_id": str(row.get("event_id") or ""),
                "event_family": family,
                "category": str(category or "unclassified"),
                "source": source,
                "known_at": known_at,
                "event_date": str(row.get("date", row.get("event_date", "")))[:10],
                "ticker": ticker,
                "title": str(row.get("title") or "")[:500],
                "url": str(row.get("url") or ""),
                "amount_usd": amount,
                "amount_type": str(row.get("amount_type") or ""),
                "recipient": str(row.get("recipient") or ""),
                "sector": sector,
                "sector_etf": etfs.get(sector, ""),
                "timestamp_quality": quality,
            })

    if events is not None and not events.empty:
        for row in events.to_dict("records"):
            source = str(row.get("source") or "").lower()
            if not _truthy(row.get("relevant", False)):
                continue
            if source == "usaspending":
                family = "federal_award"
            elif source in {"federal_register", "congress"}:
                family = "policy_regulatory"
            else:
                continue
            append_record(row, family, row.get("program") or row.get("incentive_type"), source)

    if investments is not None and not investments.empty:
        for row in investments.to_dict("records"):
            extra = _extra(row.get("extra"))
            append_record(row, "corporate_capex", extra.get("category", "other"), "corp_invest")

    if not rows:
        return pd.DataFrame(columns=FEED_COLUMNS)
    out = pd.DataFrame(rows, columns=FEED_COLUMNS)
    out = out[out["event_id"].astype(str).str.len() > 0]
    out = out.drop_duplicates(["event_id", "ticker"], keep="last")
    out["amount_usd"] = pd.to_numeric(out["amount_usd"], errors="coerce")
    return out.sort_values(["known_at", "ticker", "event_id"]).reset_index(drop=True)


def export(path: str | Path | None = None) -> pd.DataFrame:
    """Write event_trade_feed.csv into the Radar data-branch worktree."""
    feed = build_feed(store.read("events"), store.read("invest_events"), store.read("universe"))
    target = Path(path) if path else store_dir() / "event_trade_feed.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    feed.to_csv(target, index=False)
    return feed


if __name__ == "__main__":
    result = export()
    print(f"[event-feed] exported {len(result)} rows; exact timestamps={int((result['timestamp_quality'] == 'exact_utc').sum()) if len(result) else 0}")
