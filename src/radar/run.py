"""Komut satırı giriş noktası.

  python -m radar.run slot [--slot auto|open|midday|close] [--force]
  python -m radar.run weekly
  python -m radar.run demo            # internet olmadan sahte veriyle uçtan uca çalıştırma
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import __version__, alerts, diagnostics, learn, scoring, store
from .classify import Classifier, train
from .entity import Matcher, normalize
from .settings import setup_logging, sources, store_dir

log = setup_logging()
diagnostics.install()
INTRADAY_SOURCES = ["federal_register", "congress", "usaspending", "grants_gov", "sec_edgar", "gdelt"]


# ---------------------------------------------------------------- slot mantığı

def resolve_slot(requested: str, force: bool, state: dict, now_utc: datetime | None = None) -> str | None:
    cfg = sources()["schedule"]
    tz = ZoneInfo(cfg["timezone"])
    now = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(tz)
    today = now.date().isoformat()
    done = set(state.get("slots_done", {}).get(today, []))
    if requested != "auto":
        return requested if force or requested not in done else None
    if now.weekday() >= 5 and not force:
        return None
    for name, s in cfg["slots"].items():
        hh, mm = map(int, s["target"].split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start <= now <= start + timedelta(minutes=s["window_minutes"]) and (force or name not in done):
            return name
    return None


def _since(state: dict, source: str) -> date:
    cfg = sources()["schedule"]
    last = state.get("last_success", {}).get(source)
    if not last:
        return date.today() - timedelta(days=cfg["first_run_lookback_days"])
    return date.fromisoformat(last[:10]) - timedelta(days=cfg["overlap_days"])


# ---------------------------------------------------------------- etiketler

def load_labels() -> pd.DataFrame:
    p = store_dir() / "labels" / "labels.jsonl"
    if not p.exists():
        return pd.DataFrame(columns=["event_id", "kind", "label", "recipient_norm", "ticker", "ts"])
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    df = pd.DataFrame(rows)
    for c in ["event_id", "kind", "label", "recipient_norm", "ticker", "ts"]:
        if c not in df.columns:
            df[c] = ""
    return df


def entity_overrides(labels: pd.DataFrame) -> pd.DataFrame:
    if labels.empty:
        return pd.DataFrame(columns=["recipient_norm", "ticker"])
    e = labels[labels["kind"] == "entity"].copy()
    e["recipient_norm"] = e["recipient_norm"].fillna("").map(lambda s: normalize(s) if s else s)
    return e[["recipient_norm", "ticker"]]


# ---------------------------------------------------------------- toplama

def ingest(slot: str, state: dict, run_status: dict) -> pd.DataFrame:
    from .sources import (congress, federal_register, fred_csv, gdelt, grants_gov, prices,
                          sec_edgar, treasury_dts, usaspending)
    mods = {"federal_register": federal_register, "congress": congress, "usaspending": usaspending,
            "grants_gov": grants_gov, "sec_edgar": sec_edgar, "gdelt": gdelt}
    frames = []
    for name in INTRADAY_SOURCES:
        if not sources().get(name, {}).get("enabled", True):
            continue
        t0 = time.time()
        try:
            df = mods[name].fetch(_since(state, name))
            frames.append(df)
            state.setdefault("last_success", {})[name] = date.today().isoformat()
            run_status[name] = {"ok": True, "rows": len(df), "sec": round(time.time() - t0, 1)}
        except Exception as exc:
            log.error("%s başarısız: %s", name, exc)
            run_status[name] = {"ok": False, "error": str(exc)[:300], "sec": round(time.time() - t0, 1)}
    if slot == "open":
        for name, fn in (("fred", lambda: store.upsert("fred", fred_csv.fetch(), ["series", "date"])),
                         ("treasury_dts", lambda: store.upsert(
                             "outlays", treasury_dts.fetch(_since(state, "treasury_dts")),
                             ["record_date", "category"]))):
            try:
                fn()
                state.setdefault("last_success", {})[name] = date.today().isoformat()
                run_status[name] = {"ok": True}
            except Exception as exc:
                run_status[name] = {"ok": False, "error": str(exc)[:300]}
    if slot == "close":
        try:
            update_prices(prices, period="10d")
            run_status["prices"] = {"ok": True}
        except Exception as exc:
            run_status["prices"] = {"ok": False, "error": str(exc)[:300]}
    return pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) if frames else pd.DataFrame()


def price_tickers(extra_limit: int | None = None) -> list[str]:
    cfg = sources()
    base = list(cfg["sector_etfs"].values()) + [cfg["benchmark"]]
    exp = store.read("company_exposure")
    extra = exp["ticker"].dropna().astype(str).head(extra_limit or cfg["prices"]["max_extra_tickers"]).tolist() if not exp.empty else []
    return sorted(set(base + extra))


def update_prices(prices_mod, period: str) -> None:
    df = prices_mod.fetch(price_tickers(), period=period)
    if not df.empty:
        store.upsert("prices", df, ["date", "ticker"])


# ---------------------------------------------------------------- işleme

def process(raw: pd.DataFrame, run_id: str, asof: date, slot: str, run_status: dict,
            write_ledger: bool) -> None:
    labels = load_labels()
    clf = Classifier()
    old = store.read("events")
    old_ids = set(old["event_id"].astype(str)) if not old.empty else set()
    if raw is not None and not raw.empty:
        raw = raw.copy()
        raw["known_at"] = raw["known_at"].replace("", None).fillna(run_id)
        if old_ids:  # ilk görülme zamanı korunur (geriye dönük test sızıntısını önler)
            first_seen = dict(zip(old["event_id"].astype(str), old["known_at"].astype(str)))
            raw["known_at"] = [first_seen.get(str(i), k) for i, k in zip(raw["event_id"], raw["known_at"])]
        cls = clf.classify(raw, labels)
        uni = store.read("universe")
        if not uni.empty:
            cls, review = Matcher(uni, entity_overrides(labels)).apply(cls)
            if not review.empty:
                store.upsert("review_queue", review, "recipient_norm")
        else:
            cls["match_score"], cls["match_method"] = pd.NA, ""
        events = store.upsert("events", cls, "event_id")
        new_events = cls[~cls["event_id"].astype(str).isin(old_ids)]
    else:
        events, new_events = old, pd.DataFrame()
    # İlgisiz olayları 120 günden sonra at (depo küçük kalır, eğitim negatifleri kalır).
    if not events.empty:
        d = pd.to_datetime(events["date"], errors="coerce")
        rel = events["relevant"].astype(str).str.lower().isin(["true", "1"])
        keep = rel | (d > pd.Timestamp(asof) - pd.Timedelta(days=120))
        if (~keep).any():
            events = events[keep]
            store.write("events", events)
    run_status["events_total"] = int(len(events))
    run_status["events_new"] = int(len(new_events))
    score_and_report(events, new_events, run_id, asof, write_ledger)


def score_and_report(events: pd.DataFrame, new_events: pd.DataFrame, run_id: str, asof: date,
                     write_ledger: bool) -> None:
    state = learn.load_state()
    rel = events[events["relevant"].astype(str).str.lower().isin(["true", "1"])] if not events.empty else events
    regime_mult, regime_info = scoring.regime_multiplier(store.read("fred"))
    dts_mult, mom = scoring.dts_sector_momentum(store.read("outlays"))
    prev = store.read("scores_latest")
    scores, prog, ev_params = scoring.simulate(rel, asof, state, regime_mult, dts_mult)
    if not scores.empty:
        scores.insert(0, "asof", asof.isoformat())
        scores.insert(0, "run_id", run_id)
        store.write("scores_latest", scores)
        store.append("scores_history", scores[["run_id", "asof", "sector", "horizon_days", "p10", "p50", "p90", "score"]])
    if not prog.empty:
        store.write("program_impacts", prog)
    exp = scoring.company_exposure(ev_params, store.read("universe"))
    if not exp.empty:
        store.write("company_exposure", exp)
    if not mom.empty:
        store.write("dts_momentum", mom)
    al = alerts.build(new_events, scores, prev, run_id)
    if not al.empty:
        store.append("alerts", al)
        alerts.notify_github(al, run_id)
    if write_ledger and not scores.empty:
        learn.write_ledger(store.read("scores_history"), asof.isoformat(), state, __version__)
    store.save_json("context", {"run_id": run_id, "asof": asof.isoformat(), "regime": regime_info,
                                "dts_multipliers": dts_mult, "version": __version__,
                                "drift": learn.program_psi(events, asof)})


# ---------------------------------------------------------------- komutlar

def cmd_slot(args) -> None:
    state = store.load_json("state")
    slot = resolve_slot(args.slot, args.force, state)
    if slot is None:
        log.info("Bu tetikleme için çalışacak slot yok (saat penceresi dışında veya zaten tamamlandı).")
        return
    now_et = datetime.now(ZoneInfo(sources()["schedule"]["timezone"]))
    run_id = now_et.strftime("%Y-%m-%dT%H:%M") + f"_{slot}"
    log.info("Slot başlıyor: %s", run_id)
    run_status: dict = {}
    t0 = time.time()
    try:
        raw = ingest(slot, state, run_status)
        process(raw, run_id, now_et.date(), slot, run_status, write_ledger=(slot == "close"))
        if slot == "close":
            learn.resolve_ledger(store.read("prices"))
        ok = True
    except Exception:
        log.error(traceback.format_exc())
        run_status["fatal"] = traceback.format_exc()[-800:]
        ok = False
    state.setdefault("slots_done", {}).setdefault(now_et.date().isoformat(), []).append(slot)
    state["slots_done"] = dict(sorted(state["slots_done"].items())[-10:])
    store.save_json("state", state)
    store.append("runs", pd.DataFrame([{"run_id": run_id, "slot": slot, "ok": ok, "seconds": round(time.time() - t0, 1),
                                        "status": json.dumps(run_status, default=str)}]))
    diagnostics.write(f"slot_{slot}", run_status)
    if not ok:
        raise SystemExit(1)


def cmd_weekly(args) -> None:
    from .sources import etf_holdings, prices, sec_edgar
    status: dict = {}
    try:
        uni = etf_holdings.fetch_universe()
        try:
            sec = sec_edgar.company_tickers()
            uni = pd.concat([uni, sec[~sec["ticker"].isin(set(uni["ticker"]))][["ticker", "name"]]], ignore_index=True)
        except Exception as exc:
            status["sec_tickers"] = str(exc)[:200]
        if len(uni) > 100:
            store.write("universe", uni)
        status["universe"] = len(uni)
    except Exception as exc:
        status["universe_error"] = str(exc)[:300]
    try:
        update_prices(prices, period=sources()["prices"]["history_period"])
    except Exception as exc:
        status["prices_error"] = str(exc)[:300]
    weekly_learning(status)
    store.append("runs", pd.DataFrame([{"run_id": datetime.now().strftime("%Y-%m-%dT%H:%M") + "_weekly",
                                        "slot": "weekly", "ok": True, "seconds": 0,
                                        "status": json.dumps(status, default=str)}]))
    diagnostics.write("weekly", status)


def weekly_learning(status: dict) -> None:
    events, labels = store.read("events"), load_labels()
    prices_df = store.read("prices")
    state = learn.load_state()
    learn.resolve_ledger(prices_df)
    state, metrics = learn.evaluate_and_calibrate(state)
    es = learn.event_study(events, prices_df)
    if not es.empty:
        store.upsert("event_study", es, ["event_id", "ticker"])
        state = learn.update_program_efficacy(state, store.read("event_study"))
    if not events.empty:
        status["classifier"] = train(events, labels)
        # Yeni modelle tüm olayları yeniden sınıflandır ve yeniden eşleştir.
        base_cols = [c for c in events.columns if c not in
                     ("program", "program_name", "incentive_type", "sector_weights", "confidence", "relevance_prob",
                      "relevant", "method", "match_score", "match_method")]
        recl = Classifier().classify(events[base_cols], labels)
        uni = store.read("universe")
        if not uni.empty:
            recl, review = Matcher(uni, entity_overrides(labels)).apply(recl)
            store.write("review_queue", review)
        store.write("events", recl)
    state["history"] = (state.get("history", []) + [{"date": date.today().isoformat(),
                        "calibration": state["calibration"], "n_program_eff": len(state["program_efficacy"])}])[-104:]
    store.save_json("model_state", state)
    status["metrics_rows"] = 0 if metrics is None else len(metrics)


def cmd_demo(args) -> None:
    from . import demo
    demo.build(days=args.days)


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("slot")
    s.add_argument("--slot", default="auto", choices=["auto", "open", "midday", "close"])
    s.add_argument("--force", action="store_true")
    sub.add_parser("weekly")
    d = sub.add_parser("demo")
    d.add_argument("--days", type=int, default=420)
    args = ap.parse_args()
    {"slot": cmd_slot, "weekly": cmd_weekly, "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    main()
