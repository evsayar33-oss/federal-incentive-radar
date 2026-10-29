"""Run diagnostics for Federal Incentive Radar.

Replacement for src/radar/diagnostics.py.
Collects warning/error log records, scrubs likely secrets, and stores a compact
summary alongside the existing last_run.json schema. This module must not make
the data pipeline fail merely because diagnostic collection fails.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import store

_MAX_RECORDS = 250
_records: list[dict] = []
_total_seen = 0

# Avoid persisting tokens/API credentials if a library accidentally includes them in a log.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bot)\d{5,}:[A-Za-z0-9_-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token|api[_-]?key|password|secret|authorization)(\s*[:=]\s*)([^\s,;]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(https?://[^\s/]+/bot)\d{5,}:[A-Za-z0-9_-]{20,}"), r"\1[REDACTED]"),
]


def _redact(value: object) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _truncate(text: str, limit: int = 2600) -> str:
    if len(text) <= limit:
        return text
    # For stack traces the final exception line is often the most useful part.
    head = max(250, limit // 5)
    tail = limit - head - 35
    return text[:head] + "\n... [orta bölüm kısaltıldı] ...\n" + text[-tail:]


class _Collector(logging.Handler):
    """Keep structured WARN/ERROR records in memory until the current run is saved."""

    def emit(self, record: logging.LogRecord) -> None:
        global _total_seen
        if record.levelno < logging.WARNING:
            return
        _total_seen += 1
        # Keep a rolling window so an early burst of provider errors cannot hide
        # the actual failure that happened near the end of the run.
        if len(_records) >= _MAX_RECORDS:
            _records.pop(0)
        try:
            message = record.getMessage()
            if record.exc_info:
                import traceback
                message = message + "\n" + "".join(traceback.format_exception(*record.exc_info))
            message = _truncate(_redact(message))
            _records.append({
                "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": str(record.name)[:120],
                "file": Path(record.pathname).name[:120],
                "line": int(record.lineno),
                # Retain legacy `msg` field for compatibility with existing readers.
                "msg": message,
            })
        except Exception:
            # Telemetry must never interrupt the main radar run.
            return


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, _Collector) for handler in root.handlers):
        root.addHandler(_Collector())


def _group_key(record: dict) -> tuple[str, str, str]:
    msg = str(record.get("msg", "")).splitlines()[0][:240]
    # Collapse common one-symbol-at-a-time price-provider messages into one group.
    msg = re.sub(r"(?i)(ticker|symbol)\s+['\"]?[A-Z][A-Z0-9.\-]{0,9}['\"]?", r"\1 <SYMBOL>", msg)
    msg = re.sub(r"\b\d{4,}\b", "<N>", msg)
    return str(record.get("level", "?")), str(record.get("logger", "?")), msg


def _summarize_logs(records: list[dict]) -> dict:
    counts = Counter(str(r.get("level", "UNKNOWN")) for r in records)
    groups: dict[tuple[str, str, str], dict] = {}
    for r in records:
        key = _group_key(r)
        if key not in groups:
            groups[key] = {
                "level": key[0], "logger": key[1], "count": 0,
                "example": r.get("msg", ""), "file": r.get("file", ""), "line": r.get("line"),
                "at": r.get("at", ""),
            }
        groups[key]["count"] += 1
        # Prefer an ERROR example over a previous WARNING example within a group.
        if r.get("level") in ("ERROR", "CRITICAL"):
            groups[key]["example"] = r.get("msg", "")
            groups[key]["file"] = r.get("file", "")
            groups[key]["line"] = r.get("line")
            groups[key]["at"] = r.get("at", "")

    ordered = sorted(groups.values(), key=lambda g: (
        0 if g["level"] == "CRITICAL" else 1 if g["level"] == "ERROR" else 2,
        -int(g["count"]), g["logger"],
    ))
    return {
        "captured": len(records),
        "warning_count": int(counts.get("WARNING", 0)),
        "error_count": int(counts.get("ERROR", 0) + counts.get("CRITICAL", 0)),
        "by_level": dict(counts),
        "groups": ordered[:30],
        # Keep a small ungrouped sample too, useful when a new error first appears.
        "recent_records": records[-25:],
    }


def _table_summary() -> dict:
    out: dict = {}
    for name in ["events", "universe", "prices", "outlays", "fred", "ledger", "scores_latest",
                 "company_exposure", "review_queue", "alerts", "event_study", "metrics"]:
        df = store.read(name)
        out[name] = int(len(df))
    ev = store.read("events")
    if not ev.empty:
        rel = ev["relevant"].astype(str).str.lower().isin(["true", "1"])
        out["events_by_source"] = {
            source: {
                "total": int(len(group)),
                "relevant": int(rel[group.index].sum()),
                "with_ticker": int((group["tickers"].fillna("").astype(str).replace("nan", "") != "").sum()),
                "with_amount": int(pd.to_numeric(group["amount_usd"], errors="coerce").gt(0).sum()),
                "last_date": str(group["date"].max()),
            }
            for source, group in ev.groupby("source")
        }
        out["events_by_program"] = ev[rel]["program"].value_counts().head(20).to_dict()
        if "match_method" in ev.columns:
            out["match_methods"] = ev["match_method"].fillna("").astype(str).value_counts().to_dict()
    uni = store.read("universe")
    if not uni.empty and "sector" in uni.columns:
        out["universe_by_sector"] = uni["sector"].fillna("(yok)").value_counts().to_dict()
        for col in ("sp500_weight", "r2000_weight", "sector_weight"):
            out[f"universe_{col}_n"] = int(pd.to_numeric(uni[col], errors="coerce").gt(0).sum()) if col in uni.columns else 0
    pr = store.read("prices")
    if not pr.empty:
        out["prices"] = {
            "tickers": int(pr["ticker"].nunique()),
            "first": str(pr["date"].min()),
            "last": str(pr["date"].max()),
        }
    return out


def write(kind: str, status: dict) -> None:
    """Save the slot report and log diagnostics without masking the pipeline result."""
    global _total_seen
    records = list(_records)
    total_seen = int(_total_seen)
    try:
        summary = _summarize_logs(records)
        summary["records_kept"] = len(records)
        summary["total_seen"] = total_seen
        summary["records_dropped"] = max(0, total_seen - len(records))
        # The caller uses this same status object for Telegram immediately after write().
        status["_diagnostics"] = summary
        doc = store.load_json("last_run")
        if not isinstance(doc, dict):
            doc = {}
        try:
            tables = _table_summary()
        except Exception as exc:
            tables = {"error": _truncate(_redact(exc), 800)}
        doc[kind] = {
            "at": store.now_iso(),
            "status": status,
            # Legacy key retained; each item is now structured and includes file/line/time.
            "warnings": records,
            "log_diagnostics": summary,
        }
        doc["diagnostics_version"] = 2
        doc["tables"] = tables
        store.save_json("last_run", doc)
    except Exception as exc:
        # Do not allow diagnostic serialization to turn a successful slot into a failure.
        logging.getLogger("radar.diagnostics").error(
            "Tanılama raporu yazılamadı (%s): %s", kind, _truncate(_redact(exc), 500)
        )
    finally:
        _records.clear()
        _total_seen = 0
