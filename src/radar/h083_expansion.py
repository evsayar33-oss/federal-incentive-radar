"""Prospective, isolated H083 discovery universe.

A current SEC exchange/CIK association is a discovery aid, not a historical
listing record. A filing mentioning a federal program is NOT an award or proof
of benefit. Never feed these rows to the existing H083 forward cohort engine.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from . import event_feed, event_watch as W, http, price_resilience, store
from .settings import sources

LOG = logging.getLogger("radar.h083_expansion")
START_LIMIT, TARGET_LIMIT, MAX_LIMIT, STEP, HEALTHY_SESSIONS = 200, 300, 500, 50, 3
LOOKBACK_DAYS = 365
SCHEMA = "H083_EXPANSION_V1"
TABLE = "h083_expansion"
EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
MEMBER_COLS = event_feed.FEED_COLUMNS + ["issuer_cik", "exchange", "evidence_kind", "source_known_at", "discovered_at_utc"]
FORMS = {"8-K", "10-Q", "10-K"}


def _cik(value):
    try:
        n = int(str(value))
        return str(n) if n > 0 else ""
    except (ValueError, TypeError):
        return ""


def listing_map(payload):
    """Reject ambiguous ticker identities; only today's NYSE/Nasdaq mapping."""
    fields = payload["fields"]
    if not isinstance(fields, list) or not {"cik", "ticker", "exchange"}.issubset(fields):
        raise ValueError("SEC_LISTING_SCHEMA")
    frame = pd.DataFrame(payload["data"], columns=fields)
    frame["ticker"] = frame.ticker.fillna("").astype(str).str.strip().str.upper()
    frame["issuer_cik"] = frame.cik.map(_cik)
    frame["exchange"] = frame.exchange.fillna("").astype(str).str.strip().str.upper()
    frame = frame.loc[frame.ticker.map(lambda t: bool(W.TICKER_RE.fullmatch(t))) & frame.issuer_cik.ne("")]
    ambiguous = frame.groupby("ticker").issuer_cik.nunique()
    frame = frame.loc[~frame.ticker.isin(ambiguous[ambiguous > 1].index)]
    frame = frame.drop_duplicates("ticker")
    if not frame.exchange.isin({"NYSE", "NASDAQ"}).any():
        raise ValueError("SEC_LISTING_EMPTY")
    return frame.set_index("ticker")[["issuer_cik", "exchange"]].to_dict("index")


def _valid_sec_url(url, cik, accession):
    p = urlparse(str(url))
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(accession)):
        return False
    prefix = f"/Archives/edgar/data/{cik}/{str(accession).replace('-', '')}/"
    return p.scheme == "https" and p.netloc == "www.sec.gov" and p.path.startswith(prefix) and len(p.path) > len(prefix)


def _text(value):
    return "" if pd.isna(value) else str(value)


def discover(feed, events, listings, now, session, previous_members=()):
    """Select only documented links, before examining any current prices."""
    eligible = W.eligible_events(feed, now)
    if eligible.empty:
        core = set()
        overflow = eligible
    else:
        eligible = eligible.sort_values(["event_date", "_known_sort", "ticker"], ascending=False)
        core = set(eligible.head(W.MAX_TICKERS).ticker)
        overflow = eligible.iloc[W.MAX_TICKERS:]
    core_ciks = {listings[t]["issuer_cik"] for t in core if t in listings}
    cutoff = pd.Timestamp(session) - pd.Timedelta(days=LOOKBACK_DAYS)
    now = pd.Timestamp(now).tz_convert("UTC")
    now_iso = now.isoformat()
    rows = []
    def add(row, ticker, kind, event_date, original_known):
        listing = listings.get(ticker)
        if ticker in core or not listing or listing["exchange"] not in {"NYSE", "NASDAQ"} or listing["issuer_cik"] in core_ciks:
            return
        if event_date < cutoff or event_date > session:
            return
        rows.append({
            **{k: row.get(k, "") for k in event_feed.FEED_COLUMNS},
            "ticker": ticker, "event_date": str(event_date.date()),
            # Current listing and this discovery have only been observed NOW.
            "known_at": now_iso, "timestamp_quality": "first_seen_utc",
            "source_known_at": original_known, "discovered_at_utc": now_iso,
            "evidence_kind": kind, **listing,
        })
    for row in overflow.to_dict("records"):
        add(row, row["ticker"], "EXISTING_ELIGIBLE_EVENT_LINK",
            row["event_date"], _text(row["known_at"]))
    phrases = set(sources()["sec_edgar"]["phrases"])
    # A multiple-ticker filing is ambiguous for our one-issuer discovery use.
    for row in events.loc[events.source.eq("sec_edgar")].to_dict("records") if "source" in events else []:
        if not event_feed._truthy(row.get("relevant", False)):
            continue
        extra = event_feed._extra(row.get("extra"))
        ticker = _text(row.get("tickers")).strip().upper()
        if not W.TICKER_RE.fullmatch(ticker) or ticker not in listings:
            continue
        cik = _cik(extra.get("cik"))
        if cik != listings[ticker]["issuer_cik"] or extra.get("form") not in FORMS or extra.get("phrase") not in phrases:
            continue
        if not _valid_sec_url(row.get("url"), cik, extra.get("adsh")):
            continue
        date = pd.to_datetime(row.get("date"), errors="coerce")
        known = pd.to_datetime(row.get("known_at"), errors="coerce", utc=True)
        if pd.isna(date) or pd.isna(known) or known > now or not _text(row.get("event_id")):
            continue
        day = pd.Timestamp(date).tz_localize(None).normalize()
        add({
            "event_id": row["event_id"], "event_family": "sec_federal_disclosure",
            "category": _text(row.get("program")), "source": "sec_edgar",
            "title": _text(row.get("title")), "url": row["url"],
            "recipient": _text(row.get("recipient")),
        }, ticker, "SEC_DISCLOSURE_LEAD_NOT_AWARD", day, known.isoformat())
    if not rows:
        return core, pd.DataFrame(columns=MEMBER_COLS)
    found = pd.DataFrame(rows, columns=MEMBER_COLS)
    prior = {str(t): i for i, t in enumerate(previous_members)}
    found["_prior"] = found.ticker.map(prior).fillna(len(prior))
    found["_direct"] = found.evidence_kind.eq("EXISTING_ELIGIBLE_EVENT_LINK")
    # Retain admitted members while eligible; no selection by data availability.
    found = found.sort_values(["_prior", "_direct", "event_date", "ticker", "event_id"],
                              ascending=[True, False, False, True, True])
    found = found.drop_duplicates("issuer_cik").drop_duplicates("ticker")
    return core, found[MEMBER_COLS].reset_index(drop=True)


def prior_history(leads, prices, session, previous_members=()):
    """Pre-admission screen uses only past closes, NEVER today's bar.

    This is a readiness check, not OHLCV/volume validation; all admitted
    names must still pass the exact-session coverage and $5m liquidity gate.
    Retain previously admitted names until their source eligibility changes.
    """
    import numpy as np
    import pandas_market_calendars as mcal
    target = pd.Timestamp(session).normalize()
    calendar = mcal.get_calendar("NYSE")
    required = calendar.schedule(start_date=target - pd.Timedelta(days=400),
                                 end_date=target - pd.Timedelta(days=1)).index[-200:]
    if len(required) != 200 or not {"ticker", "date", "close"}.issubset(prices):
        raise ValueError("PRIOR_HISTORY_SCHEMA_OR_CALENDAR")
    q = prices.loc[prices.ticker.isin({"SPY", *leads.ticker}), ["ticker","date","close"]].copy()
    q["date"] = pd.to_datetime(q.date, errors="coerce").dt.normalize()
    q["close"] = pd.to_numeric(q.close, errors="coerce")
    q = q[q.date.isin(required) & np.isfinite(q.close) & q.close.gt(0)]
    count = q.groupby("ticker").date.nunique()
    if count.get("SPY", 0) != len(required):
        raise ValueError("PRIOR_BENCHMARK_HISTORY_UNAVAILABLE")
    ready = set(count[count == len(required)].index) | set(previous_members)
    return leads[leads.ticker.isin(ready)].copy()


def _capacity(value):
    return value if type(value) is int and START_LIMIT <= value <= MAX_LIMIT and (value - START_LIMIT) % STEP == 0 else START_LIMIT


def capacity(prior, session):
    """Expand only between distinct sessions, never on same-session retries."""
    return _capacity(prior.get("total_limit") if prior.get("feature_asof") == session else prior.get("next_total_limit"))


def progress(prior, status, backlog):
    same = prior.get("feature_asof") == status["feature_asof"]
    old_streak = prior.get("healthy_sessions", 0)
    old_streak = old_streak if type(old_streak) is int and 0 <= old_streak <= HEALTHY_SESSIONS else 0
    healthy = status["ok"] and status["eligible_tickers"] > 0
    # Do not revoke a queued step or count the same successful session twice.
    if same and healthy and prior.get("ok") and _capacity(prior.get("next_total_limit")) > status["total_limit"]:
        return {"healthy_sessions": 0, "next_total_limit": _capacity(prior["next_total_limit"])}
    streak = min(HEALTHY_SESSIONS, old_streak + (0 if same and prior.get("ok") else 1)) if healthy else 0
    limit = status["total_limit"]
    next_limit = min(MAX_LIMIT, limit + STEP) if streak >= HEALTHY_SESSIONS and backlog > 0 else limit
    return {"healthy_sessions": 0 if next_limit > limit else streak, "next_total_limit": next_limit}


def fetch_prices(tickers, target, recovery_round=0, batch_loader=None, pause=time.sleep):
    """Stop a widespread primary outage instead of hammering a free source."""
    load = batch_loader or price_resilience._yahoo
    loaded, errors, poor, attempted = {}, [], 0, 0
    circuit = False
    names = ["SPY", *tickers]
    for i in range(0, len(names), price_resilience.BATCH_SIZE):
        batch = names[i:i + price_resilience.BATCH_SIZE]
        if i:
            pause(1.0)
        try:
            bars, issues = load(batch, "3y")
        except Exception as exc:
            bars, issues = {}, [type(exc).__name__]
        clean = {t: price_resilience.normalize(v) for t, v in bars.items() if t in batch}
        clean = {t: v.loc[v.index <= target] for t, v in clean.items()}
        clean = {t: v for t, v in clean.items() if not v.empty}
        loaded.update(clean)
        errors.extend(issues)
        attempted += len(batch)
        exact = {t for t, v in clean.items() if target in v.index and v.loc[target, "Volume"] > 0}
        poor = poor + 1 if len(exact) * 2 < len(batch) else 0
        if poor >= 2 or (i == 0 and "SPY" not in exact):
            circuit = True
            errors.append("EXPANSION_PRIMARY_BATCH_CIRCUIT_OPEN")
            break
    # Existing whole-series recovery checks remain in force. No Twelve
    # Data credits or additional single-symbol Yahoo requests are consumed.
    bars, report = price_resilience.download_bars(
        names, "3y", required_asof=target,
        yahoo_loader=lambda *_: (loaded, errors),
        max_independent=0, max_fallback=0 if circuit else 6,
        recovery_round=recovery_round)
    report.update({"primary_attempted": attempted, "primary_circuit_open": circuit,
                   "primary_unattempted": len(names) - attempted})
    return bars, report


def _blocked(now, session, reason, core_count=0, total_limit=START_LIMIT, eligible=0):
    return {"ok": False, "watch_only": True, "validated_alpha": False,
            "feature_asof": session, "generated_at_utc": pd.Timestamp(now).isoformat(),
            "eligible_tickers": eligible, "priced_tickers": 0, "liquid_tickers": 0,
            "candidates": 0, "data_coverage_pct": 0., "data_coverage_ok": False,
            "reason": reason, "core_eligible_tickers": core_count, "total_limit": total_limit}


def run(now=None):
    now = now or datetime.now(timezone.utc)
    target = W.completed_nyse_session(now, current_only=True)
    if target is None:
        print("[H083 expansion] no completed current session; skip")
        return
    session = str(target.date())
    prior = {}
    members = pd.DataFrame(columns=MEMBER_COLS)
    watch = pd.DataFrame(columns=W.CANDIDATE_COLS)
    exclusions = pd.DataFrame(columns=W.EXCLUSION_COLS)
    count, limit, backlog, discovered, history_waiting = 0, START_LIMIT, 0, 0, 0
    status = _blocked(now, session, "EXPANSION_NOT_RUN")
    try:
        prior = store.load_json(TABLE + "_status")
        if not isinstance(prior, dict):
            prior = {}
        limit = capacity(prior, session)
        if prior.get("feature_asof") == session and prior.get("ok") is True:
            print("[H083 expansion] healthy snapshot already published; no new price requests")
            return
        source = Path(os.environ.get("RADAR_EXPANSION_INPUT", ""))
        source_sha = os.environ.get("RADAR_EXPANSION_SOURCE_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("UNPINNED_CORE_SOURCE")
        feed = pd.read_csv(source / "event_trade_feed.csv", low_memory=False)
        core_status = json.loads((source / "event_watch_status.json").read_text())

        if core_status.get("feature_asof") != session:
            status = _blocked(now, session, "CORE_SESSION_UNAVAILABLE", total_limit=limit)
        else:
            listings = listing_map(http.get_json(EXCHANGE_URL, timeout=15, retries=1))
            retained = prior.get("retained_members", [])
            if not isinstance(retained, list):
                retained = []
            events = (pd.read_parquet(source / "events.parquet") if (source / "events.parquet").exists()
                      else pd.read_csv(source / "events.csv.gz", low_memory=False))
            core, leads = discover(feed, events, listings, now, target, retained[:MAX_LIMIT])
            count, discovered = len(core), len(leads)
            if count != core_status.get("eligible_tickers"):
                status = _blocked(now, session, "CORE_UNIVERSE_MISMATCH", count, limit)
            else:
                prices = (pd.read_parquet(source / "prices.parquet") if (source / "prices.parquet").exists()
                          else pd.read_csv(source / "prices.csv.gz", low_memory=False))
                ready = prior_history(leads, prices, target, retained)
                history_waiting = discovered - len(ready)
                members = ready.head(max(0, limit - count)).copy()
                backlog = len(ready) - len(members)
                # Membership is fixed before requesting ANY current prices.
                if members.empty:
                    status = _blocked(now, session, "NO_ELIGIBLE_EXPANSION_SYMBOLS", count, limit)
                else:
                    # Separate budget and no Twelve Data credits: core wins.
                    bars, audit = fetch_prices(members.ticker.tolist(), target,
                        recovery_round=int(prior.get("recovery_round", 0)) % 10001)
                    watch, exclusions, status = W.evaluate_watch(members, bars, now, asof=target)
                    status["price_download"] = audit
                    status["core_eligible_tickers"] = count
                    status["total_limit"] = limit
    except Exception as exc:
        LOG.warning("H083 expansion blocked: %s", type(exc).__name__)
        status = _blocked(now, session, "EXPANSION_SOURCE_UNAVAILABLE", count, limit, len(members))
        watch = pd.DataFrame(columns=W.CANDIDATE_COLS)
        exclusions = pd.DataFrame(columns=W.EXCLUSION_COLS)
    # Never write event_watch_status or forward cohort/plan/evidence tables.
    retained_members = members.ticker.tolist() if not members.empty else prior.get("retained_members", [])
    if not isinstance(retained_members, list):
        retained_members = []
    status.update({
        "schema": SCHEMA, "scope": "ISOLATED_PROSPECTIVE_WATCH_ONLY",
        "source_commit_sha": os.environ.get("RADAR_EXPANSION_SOURCE_SHA", ""),
        "decision": "WATCH_ONLY_NO_TRADE", "paper_approved": False,
        "discovered_tickers": discovered, "pending_capacity": backlog,
        "pending_prior_history": history_waiting, "initial_target_total": TARGET_LIMIT,
        "maximum_target_total": MAX_LIMIT,
        "total_screened_tickers": count + int(status["eligible_tickers"]),
        "core_coverage_not_modified": True, "retained_members": retained_members[:MAX_LIMIT],
        "recovery_round": (int(prior.get("recovery_round", 0)) + 1) % 10001
                          if type(prior.get("recovery_round", 0)) is int else 0,
        "evidence_kinds": {str(k): int(v) for k,v in members.evidence_kind.value_counts().items()},
        "limitation": "SEC mentions are disclosure leads, not awards. Current listings are not historical membership.",
    })
    status.update(progress(prior, status, backlog))
    store.write(TABLE + "_universe", members)
    store.write(TABLE + "_candidates", watch)
    store.write(TABLE + "_exclusions", exclusions)
    store.save_json(TABLE + "_status", status)
    print(f"[H083 expansion] {status['eligible_tickers']} screened; {status['priced_tickers']} priced; limit={limit}; ok={status['ok']}")


if __name__ == "__main__":
    run()
