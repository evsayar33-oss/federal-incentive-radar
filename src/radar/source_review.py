"""Suggest SEC issuer filings for human verification; never certify transactions.

SEC filing search metadata and ticker proximity alone cannot prove an announced
investment, binding supply contract, completed acquisition or economic benefit.
"""
from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd

from . import store

COLS = [
    "relationship_event_id", "filing_event_id", "matched_ticker",
    "filing_date", "publication_date", "filing_url", "source_company",
    "target_company", "relation", "review_state", "is_transaction_verified",
    "reason",
]


def suggest_filings(relationships: pd.DataFrame, filings: pd.DataFrame) -> pd.DataFrame:
    if relationships is None or relationships.empty or filings is None or filings.empty:
        return pd.DataFrame(columns=COLS)
    required = {"event_id", "source", "date", "tickers", "url"}
    if not required.issubset(filings) or "event_id" not in relationships:
        return pd.DataFrame(columns=COLS)
    sec = filings.loc[filings.source.astype(str).eq("sec_edgar")].copy()
    if sec.empty:
        return pd.DataFrame(columns=COLS)
    sec["filing_date"] = pd.to_datetime(sec.date, errors="coerce")
    sec = sec.dropna(subset=["filing_date"]).tail(2500)
    records = []
    for relationship in relationships.tail(200).to_dict("records"):
        observed = pd.to_datetime(relationship.get("published_at_utc"), utc=True, errors="coerce")
        if pd.isna(observed):
            continue
        published = observed.tz_localize(None).normalize()
        parties = {
            str(relationship.get("source_ticker") or "").strip().upper(),
            str(relationship.get("target_ticker") or "").strip().upper(),
        } - {"", "NAN", "NONE"}
        if not parties:
            continue
        for filing in sec.to_dict("records"):
            filing_date = filing.get("filing_date")
            if abs((filing_date - published).days) > 14:
                continue
            url = str(filing.get("url") or "").strip()
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}
                    or not parsed.path.startswith("/Archives/")):
                continue
            sec_tickers = {v.strip().upper() for v in str(filing.get("tickers") or "").split(",")}
            overlap = parties & sec_tickers
            for ticker in sorted(overlap):
                records.append({
                    "relationship_event_id": str(relationship["event_id"]),
                    "filing_event_id": str(filing["event_id"]),
                    "matched_ticker": ticker,
                    "filing_date": filing_date.date().isoformat(),
                    "publication_date": published.date().isoformat(),
                    "filing_url": url,
                    "source_company": str(relationship.get("source_company") or ""),
                    "target_company": str(relationship.get("target_company") or ""),
                    "relation": str(relationship.get("relation") or ""),
                    "review_state": "POTENTIAL_SEC_FILING_NOT_VERIFIED",
                    "is_transaction_verified": False,
                    "reason": "Issuer and filing date proximity only; read primary contract and parties.",
                })
    if not records:
        return pd.DataFrame(columns=COLS)
    return pd.DataFrame(records, columns=COLS).drop_duplicates(
        ["relationship_event_id", "filing_event_id", "matched_ticker"]
    )


def record_candidates(new_relationships: pd.DataFrame) -> int:
    candidates = suggest_filings(new_relationships, store.read("events"))
    if not candidates.empty:
        store.upsert(
            "economic_primary_review", candidates,
            ["relationship_event_id", "filing_event_id", "matched_ticker"],
            keep="first",
        )
    return len(candidates)
