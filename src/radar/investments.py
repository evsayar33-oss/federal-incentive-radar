"""Büyük şirket yatırımı haberlerinin kanıt motoru (uzun vade).

Ana kanıt motorundan ayrıdır ve onu değiştirmez. Aynı anormal getiri ölçümünü (piyasa modeli, SPY'a göre)
kullanır ama 5 / 20 / 60 işlem günü ufuklarına bakar. Bir yatırım kategorisi yalnızca TÜM kapıları geçerse
"kanıtlandı" olur: yeterli örnek, yüksek isabet (pozitif getiri oranı), anlamlı ortalama 60 günlük getiri,
çoklu test düzeltmesi (Benjamini–Hochberg) ve zaman sırasına göre ikinci yarıda tekrar (ileriye dönük).
Sistem yalnızca uzun pozisyon (haber sonrası artış) arar; negatif etki "etkisiz" sayılır.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import evidence as E
from . import store
from .settings import sources

log = logging.getLogger("radar.invest")
CATEGORY_NAMES = {"datacenter_ai": "Veri merkezi / yapay zekâ altyapısı", "chips": "Yarı iletken / çip üretimi",
                  "energy": "Enerji altyapısı", "plant": "Fabrika / üretim tesisi", "rnd": "Ar-Ge / araştırma",
                  "other": "Diğer büyük yatırım", "all": "Tüm büyük yatırım haberleri"}
STATUS_NAMES = {"proven": "Kanıtlandı", "promising": "Umut verici", "no_effect": "Etkisiz",
                "unclear": "Belirsiz", "collecting": "Veri birikiyor"}
ICONS = {"proven": "🟢", "promising": "🟡", "no_effect": "⚫", "unclear": "⚪", "collecting": "⚪"}
STAT_COLS = ["key", "category", "category_name", "n", "mean_car_5", "mean_car_20", "mean_car_60", "hit_60", "p",
             "q", "oos_n", "oos_hit", "oos_mean", "status", "status_name", "icon", "explain", "asof"]


def cfg() -> dict:
    return sources()["corp_invest"]


# ---------------------------------------------------------------- toplama

def _store_events(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["known_at"] = df["date"]
    store.upsert("invest_events", df, "event_id", keep="first")
    return len(df)


def collect(since: date, status: dict | None = None) -> pd.DataFrame:
    """Canlı toplama: yeni büyük yatırım haberlerini 'invest_events' tablosuna ekler."""
    from .sources import corp_invest
    if not cfg().get("enabled", True):
        return pd.DataFrame()
    t0, df = time.time(), pd.DataFrame()
    old = store.read("invest_events")
    old_ids = set(old["event_id"].astype(str)) if not old.empty else set()
    try:
        df = corp_invest.fetch(since)
        _store_events(df)
        new = df[~df["event_id"].astype(str).isin(old_ids)] if not df.empty else df
        if status is not None:
            status["corp_invest"] = {"ok": True, "rows": len(df), "new": len(new), "sec": round(time.time() - t0, 1)}
        return new
    except Exception as exc:
        log.error("corp_invest başarısız: %s", exc)
        if status is not None:
            status["corp_invest"] = {"ok": False, "error": str(exc)[:300]}
        return pd.DataFrame()


def backfill(years: float = 5.0, budget_minutes: float = 40.0) -> dict:
    """Geçmiş pencereleri (31 gün) sırayla yükler; kesintide kaldığı yerden devam eder."""
    from .history import windows
    from .sources import corp_invest
    t0, end = time.time(), date.today()
    start = end - timedelta(days=int(365.25 * years))
    prog = store.read("invest_progress")
    done = set(prog["start"].astype(str)) if not prog.empty else set()
    out = {"windows_done": 0, "events": 0, "stopped_early": False, "errors": []}
    for a, b in windows(start, end, 31):
        if a.isoformat() in done:
            continue
        if time.time() - t0 > budget_minutes * 60:
            out["stopped_early"] = True
            break
        try:
            n = _store_events(corp_invest.fetch(a, b))
        except Exception as exc:
            out["errors"].append(f"{a}: {str(exc)[:120]}")
            continue
        store.append("invest_progress", pd.DataFrame([{"start": a.isoformat(), "end": b.isoformat(), "events": n}]))
        out["windows_done"] += 1
        out["events"] += n
    return out


# ---------------------------------------------------------------- ölçüm

def _ecfg() -> dict:
    return {"est_window": E.cfg()["est_window"], "min_est_obs": E.cfg()["min_est_obs"],
            "windows": {"0_5": [0, 5], "0_20": [0, 20], "0_60": [0, 60]}}


def _thin(ev: pd.DataFrame, gap_days: int) -> pd.DataFrame:
    """Aynı hissenin üst üste binen pencerelerini azaltır: aynı hissede ilk olaydan sonra gap_days içindekiler atılır."""
    ev = ev.sort_values("d")
    keep, last = [], {}
    for i, r in zip(ev.index, ev.to_dict("records")):
        t = r["ticker"]
        if t in last and (r["d"] - last[t]).days < gap_days:
            continue
        last[t] = r["d"]
        keep.append(i)
    return ev.loc[keep]


def measure(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Her olay için 5/20/60 günlük anormal getiri (piyasa modeli)."""
    if events is None or events.empty or prices is None or prices.empty:
        return pd.DataFrame()
    bench = sources()["benchmark"]
    rets = E._returns(prices)
    if bench not in rets.columns:
        return pd.DataFrame()
    idx, m = rets.index, rets[bench].to_numpy()
    c = _ecfg()
    rows = []
    for r in events.to_dict("records"):
        t = str(r["tickers"]).split(",")[0].strip()
        if t not in rets.columns:
            continue
        d = pd.to_datetime(r["date"], errors="coerce")
        if pd.isna(d):
            continue
        k0 = int(idx.searchsorted(d))
        if k0 >= len(idx):
            continue
        with np.errstate(all="ignore"):
            ab = E._abnormal(rets[t].to_numpy(), m, k0, c)
        if ab is None:
            continue
        try:
            cat = json.loads(r.get("extra") or "{}").get("category", "other")
        except ValueError:
            cat = "other"
        rows.append({"event_id": r["event_id"], "ticker": t, "date": d.date().isoformat(), "d": d,
                     "category": cat, "amount_usd": r.get("amount_usd"), **{k: v for k, v in ab.items() if k.startswith("car_")}})
    out = pd.DataFrame(rows)
    return _thin(out, int(cfg()["gate"]["min_gap_days"])).drop(columns="d") if not out.empty else out


def _stats(g: pd.DataFrame, gate: dict) -> dict:
    g = g.dropna(subset=["car_0_60"]).copy()
    clip = gate["clip_car"]
    for col in ("car_0_5", "car_0_20", "car_0_60"):
        g[col] = g[col].clip(-clip, clip)
    cl = g.groupby("date").agg(c5=("car_0_5", "mean"), c20=("car_0_20", "mean"), c60=("car_0_60", "mean")).reset_index()
    cl = cl.sort_values("date")
    n = len(cl)
    out = {"n": n, "mean_car_5": cl["c5"].mean() if n else np.nan, "mean_car_20": cl["c20"].mean() if n else np.nan,
           "mean_car_60": cl["c60"].mean() if n else np.nan, "hit_60": float((cl["c60"] > 0).mean()) if n else np.nan,
           "p": np.nan, "oos_n": 0, "oos_hit": np.nan, "oos_mean": np.nan}
    if n >= 3:
        se = cl["c60"].std(ddof=1) / np.sqrt(n)
        if se > 0:
            out["p"] = E._ttest_p(float(cl["c60"].mean() / se), n - 1)
    half = n // 2   # zaman sırasına göre: ilk yarı eğitim, ikinci yarı ileriye dönük sınav
    if half >= gate["oos_min_n"] and n - half >= gate["oos_min_n"]:
        test = cl["c60"].iloc[half:]
        out.update(oos_n=len(test), oos_hit=float((test > 0).mean()), oos_mean=float(test.mean()),
                   oos_train_mean=float(cl["c60"].iloc[:half].mean()))
    return out


def classify(r: dict, gate: dict) -> str:
    n, mean, hit, p, q = r["n"], r["mean_car_60"], r["hit_60"], r["p"], r.get("q", np.nan)
    if n < gate["min_n_promising"] or not np.isfinite(p):
        return "collecting"
    oos_ok = (r["oos_n"] >= gate["oos_min_n"] and r["oos_hit"] >= gate["oos_hit"] and r["oos_mean"] > 0
              and r.get("oos_train_mean", 0) > 0)
    oos_bad = r["oos_n"] >= gate["oos_min_n"] and r["oos_mean"] < 0
    if (n >= gate["min_n_proven"] and hit >= gate["hit_proven"] and mean >= gate["min_mean_proven"]
            and q <= gate["q_proven"] and oos_ok):
        return "proven"
    if (hit >= gate["hit_promising"] and mean >= gate["min_mean_promising"] and p <= gate["p_promising"]
            and q <= gate["q_promising"] and not oos_bad):
        return "promising"
    if n >= gate["null_min_n"] and (mean <= 0 or p >= gate["null_p"]):
        return "no_effect"
    return "unclear"


def _explain(r: dict, status: str) -> str:
    base = (f"{r['n']} yatırım haberi; haberden sonra 60 işlem gününde piyasaya (SPY) göre ortalama "
            f"{r['mean_car_60'] * 100:+.1f}%, haberlerin %{r['hit_60'] * 100:.0f}'inde pozitif.")
    if status == "proven":
        return "🟢 " + base + " Çoklu test düzeltmesinden ve ileriye dönük sınavdan geçti."
    if status == "promising":
        return "🟡 " + base + " Umut verici ama kanıt eşiği henüz geçilmedi."
    if status == "no_effect":
        return "⚫ " + base + " Anlamlı bir artış yok."
    if status == "unclear":
        return "⚪ " + base + " Etki net değil (tesadüf olabilir)."
    return f"⚪ Henüz {r['n']} örnek var; hüküm için daha çok örnek gerekiyor."


def build_evidence(ret: pd.DataFrame) -> pd.DataFrame:
    gate = cfg()["gate"]
    if ret is None or ret.empty:
        return pd.DataFrame(columns=STAT_COLS)
    groups = [("all", ret)] + [(c, g) for c, g in ret.groupby("category")]
    rows = [{"category": c, **_stats(g, gate)} for c, g in groups]
    ev = pd.DataFrame(rows)
    ev["q"] = E._bh(ev["p"])
    ev["status"] = [classify(r, gate) for r in ev.to_dict("records")]
    ev["key"] = ev["category"]
    ev["category_name"] = ev["category"].map(lambda c: CATEGORY_NAMES.get(c, c))
    ev["status_name"] = ev["status"].map(STATUS_NAMES)
    ev["icon"] = ev["status"].map(ICONS)
    ev["explain"] = [_explain(r, r["status"]) for r in ev.to_dict("records")]
    ev["asof"] = date.today().isoformat()
    return ev.reindex(columns=STAT_COLS + [c for c in ev.columns if c not in STAT_COLS])


def signals(events: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """Son günlerdeki yatırım haberlerini kategorilerinin kanıt durumuyla eşler."""
    if events is None or events.empty:
        return pd.DataFrame()
    days = int(cfg().get("signal_days", 30))
    fresh = (date.today() - timedelta(days=days)).isoformat()
    s = events[events["date"].astype(str) >= fresh].copy()
    if s.empty:
        return pd.DataFrame()
    status = dict(zip(ev["category"], ev["status"])) if not ev.empty else {}
    stat = {c: r for c, r in zip(ev["category"], ev.to_dict("records"))} if not ev.empty else {}
    cats = []
    for x in s["extra"]:
        try:
            cats.append(json.loads(x).get("category", "other"))
        except (TypeError, ValueError):
            cats.append("other")
    s["category"] = cats
    s["status"] = [status.get(c, "collecting") for c in cats]
    s["status_name"] = s["status"].map(STATUS_NAMES)
    s["icon"] = s["status"].map(ICONS)
    s["category_name"] = s["category"].map(lambda c: CATEGORY_NAMES.get(c, c))
    s["explain"] = [stat[c]["explain"] if c in stat else "⚪ Henüz kanıt yok." for c in cats]
    s["ticker"] = s["tickers"].astype(str).str.split(",").str[0]
    return s.sort_values("date", ascending=False).reset_index(drop=True)


def refresh(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ölç → kanıtla → sinyalle. Haftalık ve kapanış çalışmasında çağrılır."""
    events = store.read("invest_events")
    prices = store.read("prices") if prices is None else prices
    if events.empty or prices is None or prices.empty:
        return store.read("invest_evidence")
    ret = measure(events, prices)
    if not ret.empty:
        store.write("invest_returns", ret)
    ev = build_evidence(ret)
    store.write("invest_evidence", ev)
    store.write("invest_signals", signals(events, ev))
    log.info("Büyük yatırım kanıtı: %d kategori, %d kanıtlı", len(ev), int((ev["status"] == "proven").sum()) if len(ev) else 0)
    return ev


def tickers(limit: int = 600) -> list[str]:
    ev = store.read("invest_events")
    if ev.empty:
        return []
    t = ev.sort_values("date", ascending=False)["tickers"].astype(str).str.split(",").str[0]
    return list(dict.fromkeys(x.strip() for x in t if x.strip()))[:limit]
