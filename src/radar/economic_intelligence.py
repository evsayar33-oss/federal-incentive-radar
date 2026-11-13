"""Conservative, source-attributed corporate investment relationship extraction.

Research-only: never promotes a relationship to a validated trading signal.
Headlines are weak evidence; only explicit, unambiguous patterns are emitted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Callable

_AMOUNT = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(billion|bn|million|mn)\b", re.I)
_INVEST = re.compile(
    r"^(?P<investor>.+?)\s+(?:to\s+)?(?:invests?|invested|investing|will invest|"
    r"commits?|committed|pledges?|pledged)\s+(?:up to\s+)?"
    r"\$\s*[\d,.]+\s*(?:billion|bn|million|mn)\s+"
    r"(?:in|into)\s+(?P<recipient>.+?)"
    r"(?=\s+(?:for|to|as|amid|after|under|in order to)\b|$)", re.I
)
_ACQUIRE = re.compile(
    r"^(?P<buyer>.+?)\s+(?:to acquire|acquires|acquired|agrees to acquire|"
    r"to buy|buys|bought)\s+(?P<target>.+?)"
    r"(?=\s+(?:for|in|to|as|amid|after)\b|$)", re.I
)
_SUPPLY = re.compile(
    r"^(?P<supplier>.+?)\\s+(?:to supply|will supply|supplies|agrees to supply)\\s+"
    r"(?P<buyer>.+?)(?=\\s+(?:with|for|under|to)\\b|$)", re.I
)
_JOINT = re.compile(
    r"^(?P<partner_a>.+?)\\s+(?:forms|launches|announces)\\s+"
    r"(?:a\\s+)?joint venture with\\s+(?P<partner_b>.+?)"
    r"(?=\\s+(?:for|to|in)\\b|$)", re.I
)
_AGREEMENT = re.compile(
    r"^(?P<party_a>.+?)\\s+(?:signs|signed|announces)\\s+"
    r"(?:a\\s+)?(?:\\$[\\d,.]+\\s*(?:billion|bn|million|mn)\\s+)?"
    r"(?:supply|capacity|purchase) agreement with\\s+(?P<party_b>.+?)"
    r"(?=\\s+(?:for|to|in|under)\\b|$)", re.I
)
_SUFFIX = re.compile(r"\s*[-–]\s*(?:Reuters|Bloomberg|CNBC|AP News|PR Newswire)\s*$", re.I)


@dataclass(frozen=True)
class Relationship:
    source_company: str
    target_company: str
    relation: str
    amount_usd: float | None
    source_url: str
    observed_at_utc: str
    source_role: str = "UNRESOLVED"
    target_role: str = "UNRESOLVED"
    transaction_status: str = "ANNOUNCED_UNVERIFIED"
    evidence: str = "HEADLINE_ONLY_UNVERIFIED"
    decision: str = "RESEARCH_ONLY_NO_TRADE"


def extract_relationship(headline: str, source_url: str, observed_at_utc: str) -> Relationship | None:
    """Extract only explicitly directed transactions, without guessing beneficiaries.

    observed_at_utc must be the actual UTC observation timestamp, not an
    inferred event date or the source publication date.
    """
    if not headline or not source_url.startswith(("https://", "http://")):
        return None
    stamp = datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("observation timestamp requires timezone")
    stamp = stamp.astimezone(timezone.utc).isoformat()
    title = _SUFFIX.sub("", headline.strip()).strip(" .")
    amount_match = _AMOUNT.search(title)
    amount = None
    if amount_match:
        amount = float(amount_match.group(1).replace(",", "")) * (
            1e9 if amount_match.group(2).lower() in {"billion", "bn"} else 1e6
        )
    for pattern, relation, left, right, source_role, target_role in (
        (_INVEST, "STRATEGIC_INVESTMENT", "investor", "recipient", "INVESTOR", "DIRECT_RECIPIENT"),
        (_ACQUIRE, "ACQUISITION_ANNOUNCED", "buyer", "target", "ACQUIRER", "TARGET"),
        (_SUPPLY, "SUPPLY_ANNOUNCEMENT", "supplier", "buyer", "SUPPLIER", "BUYER"),
        (_JOINT, "JOINT_VENTURE_ANNOUNCED", "partner_a", "partner_b", "PARTNER", "PARTNER"),
        (_AGREEMENT, "AGREEMENT_ANNOUNCED", "party_a", "party_b", "COUNTERPARTY", "COUNTERPARTY"),
    ):
        match = pattern.match(title)
        if not match:
            continue
        source = match.group(left).strip(" ,:;\"'")
        target = match.group(right).strip(" ,:;\"'")
        if not source or not target or source.casefold() == target.casefold():
            return None
        return Relationship(source, target, relation, amount, source_url, stamp,
                            source_role=source_role, target_role=target_role)
    return None


def match_relationship(
    relationship: Relationship, matcher: Callable[[str], tuple[str | None, str]]
) -> dict:
    """Resolve both parties independently; ambiguous entities never get tickers.

    matcher returns (ticker, method). Only exact and explicit overrides qualify.
    """
    row = asdict(relationship)
    for name, prefix in ((relationship.source_company, "source"), (relationship.target_company, "target")):
        ticker, method = matcher(name)
        row[f"{prefix}_ticker"] = ticker if method in {"exact", "override"} else None
        row[f"{prefix}_match_method"] = method
    row["validated_alpha"] = False
    return row
