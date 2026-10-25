"""Kendi kendine öğrenme döngüsü.

1. Tahmin defteri (append-only): her gün kapanışta sektör başına "SPY'ı geçme olasılığı" yazılır.
2. Değerlendirme: ufuk dolunca gerçekleşen getiri ile puanlanır (Brier, isabet, beceri skoru).
3. Kalibrasyon: lojistik (a, b) ve sektör sapması yeniden tahmin edilir.
4. Olay çalışması: şirket olaylarında CAR[0,+5] ölçülür → program etkinlik çarpanı güncellenir.
5. Drift: program dağılımı (PSI) ve son dönem Brier kötüleşmesi izlenir.
Hepsi LLM'siz, istatistiksel ve tekrarlanabilir.
"""
from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
import pandas as pd

from . import store
from .settings import SECTORS, priors, sector_etfs, sources

log = logging.getLogger("radar.learn")


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def default_state() -> dict:
    pr = priors()
    init = pr["calibration_init"]
    return {
        "version": 1,
        "calibration": {str(h): {"a": init["a"], "b": init["b"], "n": 0} for h in pr["ledger_horizons_days"]},
        "sector_logit_bias": {s: 0.0 for s in SECTORS},
        "program_efficacy": {},
        "sector_efficacy": {},
        "history": [],
    }


def load_state() -> dict:
    st = store.load_json("model_state")
    base = default_state()
    if not st:
        return base
    for k, v in base.items():
        st.setdefault(k, v)
    return st


# ---------------------------------------------------------------- tahmin defteri

def sector_signal(scores_hist: pd.DataFrame, asof: str, horizon_flow: int = 252) -> pd.DataFrame:
    """Akış sürprizi: bugünkü medyan akış / son 60 çalışmanın medyanı (log ölçek, z benzeri)."""
    h = scores_hist[scores_hist["horizon_days"].astype(int) == horizon_flow].copy()
    h["p50"] = pd.to_numeric(h["p50"], errors="coerce")
    out = []
    for s in SECTORS:
        g = h[h["sector"] == s].sort_values("asof")
        if g.empty:
            continue
        cur = g.iloc[-1]["p50"]
        past = g.iloc[:-1].tail(60)["p50"]
        if len(past) < 5 or not np.isfinite(cur) or cur <= 0:
            z = 0.0
        else:
            med = float(np.median(past[past > 0])) if (past > 0).any() else cur
            z = float(np.clip(math.log(cur / med) / 0.25, -3, 3))
        out.append({"sector": s, "z": round(z, 3), "flow_p50": cur})
    return pd.DataFrame(out)


def write_ledger(scores_hist: pd.DataFrame, asof: str, state: dict, model_version: str) -> pd.DataFrame:
    pr = priors()
    sig = sector_signal(scores_hist, asof)
    if sig.empty:
        return store.read("ledger")
    etfs = sector_etfs()
    rows = []
    for _, r in sig.iterrows():
        for h in pr["ledger_horizons_days"]:
            cal = state["calibration"].get(str(h), {"a": 0.0, "b": 0.15})
            bias = state["sector_logit_bias"].get(r["sector"], 0.0)
            p_up = float(sigmoid(cal["a"] + cal["b"] * r["z"] + bias))
            rows.append({"pred_id": f"{asof}|{r['sector']}|{h}", "created_at": asof, "sector": r["sector"],
                         "etf": etfs[r["sector"]], "horizon_days": h, "z": r["z"], "p_up": round(p_up, 4),
                         "model_version": model_version, "resolved": False, "realized_excess": np.nan,
                         "hit": np.nan, "resolved_at": ""})
    new = pd.DataFrame(rows)
    old = store.read("ledger")
    if not old.empty:
        new = new[~new["pred_id"].isin(set(old["pred_id"].astype(str)))]  # defter değiştirilmez
    return store.append("ledger", new)


# ---------------------------------------------------------------- değerlendirme

def _price_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    p = prices.copy()
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    return p.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()


def resolve_ledger(prices: pd.DataFrame) -> pd.DataFrame:
    led = store.read("ledger")
    if led.empty or prices is None or prices.empty:
        return led
    bench = sources()["benchmark"]
    px = _price_matrix(prices)
    dates = list(px.index)
    led["resolved"] = led["resolved"].astype(str).str.lower().isin(["true", "1"])
    led["resolved_at"] = led["resolved_at"].fillna("").astype(object)
    led["realized_excess"] = pd.to_numeric(led["realized_excess"], errors="coerce")
    led["hit"] = pd.to_numeric(led["hit"], errors="coerce")
    for i in led.index[~led["resolved"]]:
        etf, h, c = led.at[i, "etf"], int(led.at[i, "horizon_days"]), str(led.at[i, "created_at"])[:10]
        if etf not in px.columns or bench not in px.columns:
            continue
        start = next((k for k, d in enumerate(dates) if d >= c), None)
        if start is None or start + h >= len(dates):
            continue
        e0, e1 = px[etf].iloc[start], px[etf].iloc[start + h]
        b0, b1 = px[bench].iloc[start], px[bench].iloc[start + h]
        if not all(np.isfinite([e0, e1, b0, b1])):
            continue
        ex = (e1 / e0 - 1) - (b1 / b0 - 1)
        led.at[i, "realized_excess"] = ex
        led.at[i, "hit"] = float(ex > 0)
        led.at[i, "resolved"] = True
        led.at[i, "resolved_at"] = dates[start + h]
    store.write("ledger", led)
    return led


def evaluate_and_calibrate(state: dict) -> tuple[dict, pd.DataFrame]:
    pr = priors()
    led = store.read("ledger")
    if led.empty:
        return state, pd.DataFrame()
    led["resolved"] = led["resolved"].astype(str).str.lower().isin(["true", "1"])
    res = led[led["resolved"]].copy()
    for c in ("p_up", "hit", "z", "realized_excess"):
        res[c] = pd.to_numeric(res[c], errors="coerce")
    metrics = []
    today = date.today().isoformat()
    for h in pr["ledger_horizons_days"]:
        r = res[res["horizon_days"].astype(int) == h].dropna(subset=["p_up", "hit"])
        n = len(r)
        if n == 0:
            continue
        brier = float(np.mean((r["p_up"] - r["hit"]) ** 2))
        clim = float(r["hit"].mean())
        brier_clim = float(np.mean((clim - r["hit"]) ** 2)) or 1e-9
        recent = r.sort_values("created_at").tail(60)
        brier_recent = float(np.mean((recent["p_up"] - recent["hit"]) ** 2))
        up = r[r["p_up"] > 0.5]["realized_excess"].mean()
        dn = r[r["p_up"] <= 0.5]["realized_excess"].mean()
        metrics.append({"asof": today, "horizon_days": h, "n": n, "brier": round(brier, 4),
                        "brier_climatology": round(brier_clim, 4), "skill": round(1 - brier / brier_clim, 4),
                        "hit_rate": round(float(((r["p_up"] > 0.5) == (r["hit"] == 1)).mean()), 4),
                        "long_short_spread": round(float((up if np.isfinite(up) else 0) - (dn if np.isfinite(dn) else 0)), 5),
                        "brier_recent60": round(brier_recent, 4),
                        "drift_flag": bool(n >= 120 and brier_recent > brier * 1.25)})
        if n >= pr["calibration_min_samples"] and r["hit"].nunique() == 2 and r["z"].std() > 0:
            from sklearn.linear_model import LogisticRegression
            lr = LogisticRegression(C=1.0).fit(r[["z"]].to_numpy(), r["hit"].astype(int).to_numpy())
            state["calibration"][str(h)] = {"a": float(lr.intercept_[0]), "b": float(lr.coef_[0][0]), "n": n}
    # Sektör sapması: yalnızca son 250 çözülmüş tahmin, güçlü shrinkage, ±0.3 sınırı.
    # (Defter tahminleri bağımsız değildir: aynı gün 3 ufuk birlikte yazılır, bu yüzden temkinli.)
    if not res.empty:
        for s, g in res.dropna(subset=["hit", "p_up"]).sort_values("created_at").groupby("sector"):
            g = g.tail(250)
            gap = float((g["hit"] - g["p_up"]).sum())
            state["sector_logit_bias"][s] = round(float(np.clip(4 * gap / (len(g) + 100), -0.3, 0.3)), 4)
    m = pd.DataFrame(metrics)
    if not m.empty:
        store.upsert("metrics", m, key=["asof", "horizon_days"])
    return state, m


# ---------------------------------------------------------------- olay çalışması

def event_study(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    bench = sources()["benchmark"]
    if events is None or events.empty or prices is None or prices.empty:
        return pd.DataFrame()
    px = _price_matrix(prices)
    if bench not in px.columns:
        return pd.DataFrame()
    rets = px.pct_change(fill_method=None)
    dates = list(px.index)
    ev = events[(events["tickers"].fillna("") != "") & (events["stage"].astype(int) >= 5)].copy()
    rows = []
    for _, r in ev.iterrows():
        d0 = str(r["date"])[:10]
        k0 = next((k for k, d in enumerate(dates) if d >= d0), None)
        if k0 is None:
            continue
        for t in str(r["tickers"]).split(","):
            t = t.strip()
            if t not in rets.columns or k0 - 250 < 1:
                continue
            est = rets.iloc[k0 - 250:k0 - 30][[t, bench]].dropna()
            if len(est) < 120:
                continue
            beta, alpha = np.polyfit(est[bench], est[t], 1)
            out = {"event_id": r["event_id"], "ticker": t, "program": r.get("program", ""),
                   "stage": int(r["stage"]), "date": d0, "beta": round(float(beta), 3)}
            for w in (5, 20):
                if k0 + w < len(dates):
                    win = rets.iloc[k0:k0 + w + 1][[t, bench]].dropna()
                    ar = win[t] - (alpha + beta * win[bench])
                    out[f"car_0_{w}"] = float(ar.sum())
            if "car_0_5" in out:
                rows.append(out)
    return pd.DataFrame(rows)


def update_program_efficacy(state: dict, es: pd.DataFrame) -> dict:
    pr = priors()
    if es is None or es.empty:
        return state
    k = pr["program_efficacy_shrink_n"]
    lo, hi = pr["program_efficacy_bounds"]
    for prog, g in es.groupby("program"):
        n = len(g)
        mean = float(pd.to_numeric(g["car_0_5"], errors="coerce").mean())
        if not np.isfinite(mean):
            continue
        shrunk = n / (n + k) * mean
        state["program_efficacy"][prog] = round(float(np.clip(1 + 0.5 * shrunk / pr["car_scale"], lo, hi)), 3)
    return state


# ---------------------------------------------------------------- drift

def program_psi(events: pd.DataFrame, asof: date) -> dict:
    if events is None or events.empty or "program" not in events.columns:
        return {}
    e = events.copy()
    e["d"] = pd.to_datetime(e["date"], errors="coerce")
    ts = pd.Timestamp(asof)
    recent = e[e["d"] > ts - pd.Timedelta(days=30)]["program"].value_counts(normalize=True)
    base = e[(e["d"] <= ts - pd.Timedelta(days=30)) & (e["d"] > ts - pd.Timedelta(days=210))]["program"].value_counts(normalize=True)
    if recent.empty or base.empty:
        return {"psi": None}
    keys = set(recent.index) | set(base.index)
    psi = sum((recent.get(k, 1e-4) - base.get(k, 1e-4)) * math.log(recent.get(k, 1e-4) / base.get(k, 1e-4)) for k in keys)
    top = (recent - base.reindex(recent.index).fillna(0)).sort_values(ascending=False).head(3)
    return {"psi": round(float(psi), 4), "flag": bool(psi > 0.25), "rising_programs": top.round(3).to_dict()}
