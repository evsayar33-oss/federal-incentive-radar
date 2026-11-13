"""Tamper-evident, idempotent research-decision journal.

Hash chaining catches accidental edits inside a retained snapshot. Because the
data branch is a replaceable Git snapshot, it is NOT an immutable third-party
timestamp or cryptographic proof of historical non-rewriting.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pandas as pd

from . import store

AUDIT_COLUMNS = ["seq", "decision_id", "recorded_at_utc", "family", "decision",
                 "payload_json", "prev_hash", "record_hash"]
GENESIS = "0" * 64


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str)


def _hash(prev: str, seq: int, decision_id: str, family: str,
          decision: str, recorded: str, payload: str) -> str:
    material = _json({
        "prev_hash": prev, "seq": seq, "decision_id": decision_id,
        "family": family, "decision": decision, "recorded_at_utc": recorded,
        "payload_json": payload,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify(frame: pd.DataFrame) -> bool:
    """Verify the chain without trusting an external mutable status marker."""
    if frame is None or frame.empty:
        return True
    if not set(AUDIT_COLUMNS).issubset(frame):
        return False
    prev = GENESIS
    seen = set()
    for index, row in enumerate(frame.to_dict("records"), 1):
        if str(row["decision_id"]) in seen or int(row["seq"]) != index:
            return False
        if str(row["prev_hash"]) != prev:
            return False
        expected = _hash(prev, index, str(row["decision_id"]), str(row["family"]),
                         str(row["decision"]), str(row["recorded_at_utc"]),
                         str(row["payload_json"]))
        if str(row["record_hash"]) != expected:
            return False
        seen.add(str(row["decision_id"]))
        prev = expected
    return True


def record(family: str, decision_id: str, decision: str, payload: dict,
           *, observed_at_utc: datetime | None = None) -> dict:
    now = observed_at_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("observed_at_utc must be timezone-aware")
    recorded = now.astimezone(timezone.utc).isoformat()
    if not family or not decision or not decision_id:
        raise ValueError("audit identity cannot be blank")
    value = _json(payload)
    old = store.read("decision_audit")
    if not verify(old):
        raise ValueError("DECISION_AUDIT_HASH_CHAIN_BROKEN")
    if not old.empty and decision_id in set(old.decision_id.astype(str)):
        previous = old.loc[old.decision_id.astype(str) == decision_id].iloc[0]
        if str(previous.payload_json) != value or str(previous.decision) != decision:
            raise ValueError("DECISION_AUDIT_REPLAY_CONFLICT")
        return {"new": False, "head_hash": str(old.iloc[-1].record_hash), "verified": True}
    seq = len(old) + 1
    prev = str(old.iloc[-1].record_hash) if not old.empty else GENESIS
    digest = _hash(prev, seq, decision_id, family, decision, recorded, value)
    row = {"seq": seq, "decision_id": decision_id, "recorded_at_utc": recorded,
           "family": family, "decision": decision, "payload_json": value,
           "prev_hash": prev, "record_hash": digest}
    store.append("decision_audit", pd.DataFrame([row], columns=AUDIT_COLUMNS))
    store.save_json("decision_audit_head", {"head_hash": digest, "records": seq,
                                           "verified": True, "asof_utc": recorded})
    return {"new": True, "head_hash": digest, "verified": True}


def record_forward_attempt(now: datetime, state: str, reason: str, session: str = "") -> None:
    """Operational attempts are not evidence of a successful forward month."""
    if now.tzinfo is None:
        raise ValueError("attempt timestamp must have timezone")
    stamp = now.astimezone(timezone.utc).isoformat()
    record = {"attempt_id": stamp + "|H083", "attempted_at_utc": stamp,
              "state": state, "reason": reason, "session": session,
              "counts_as_forward_month": False,
              "note": "A scheduled success or safe skip is not a completed forward outcome."}
    old = store.read("h083_run_audit")
    if old.empty or record["attempt_id"] not in set(old.attempt_id.astype(str)):
        store.append("h083_run_audit", pd.DataFrame([record]))
    store.save_json("h083_last_attempt", record)
    if state == "COMPLETED_RESEARCH":
        store.save_json("h083_last_successful_attempt", record)
