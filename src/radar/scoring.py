"""Etki skoru: Büyüklük × Gerçekleşme olasılığı × Zamanlama × Maruziyet × Etkinlik × Rejim.

Her sektör ve ufuk için tek sayı değil, Monte Carlo ile bir DAĞILIM üretilir (p10/p50/p90).
Belirsizlik kaynakları:
- olay tutarı (bilinmiyorsa geniş log-normal),
- aşama olasılığı (Beta),
- ortak politika şoku (tüm erken aşama olaylar birlikte; ör. iptal riski),
- sektör model hatası.
"""
from __future__ import annotations

import json
import math
from datetime import date

import numpy as np
import pandas as pd

from .settings import SECTORS, priors, programs


# ---------------------------------------------------------------- rejim ve ödeme hızı

def regime_multiplier(fred: pd.DataFrame) -> tuple[float, dict]:
    cfg = priors()["regime"]
    if fred is None or fred.empty:
        return 1.0, {"status": "veri yok"}
    s = fred[fred["series"] == cfg["series"]].sort_values("date")["value"].astype(float).tail(cfg["lookback_obs"])
    if len(s) < 12:
        return 1.0, {"status": "yetersiz veri"}
    z = (s.iloc[-1] - s.mean()) / (s.std() or 1.0)
    mult = float(np.clip(1.0 - cfg["coef"] * z, cfg["min"], cfg["max"]))
    return mult, {"series": cfg["series"], "last": float(s.iloc[-1]), "z": round(float(z), 2), "mult": round(mult, 3)}


def dts_sector_momentum(outlays: pd.DataFrame) -> tuple[dict[str, float], pd.DataFrame]:
    """Son 20 iş günü ödemesi / önceki 60 günün ortalaması → sektör çarpanı (sınırlı)."""
    cfg = priors()["dts_momentum"]
    mapping = programs().get("dts_category_sectors", {})
    mult = {s: 1.0 for s in SECTORS}
    if outlays is None or outlays.empty:
        return mult, pd.DataFrame()
    o = outlays.copy()
    o["record_date"] = o["record_date"].astype(str)
    days = sorted(o["record_date"].unique())
    if len(days) < cfg["recent_days"] + 10:
        return mult, pd.DataFrame()
    recent = set(days[-cfg["recent_days"]:])
    base = set(days[-(cfg["recent_days"] + cfg["base_days"]):-cfg["recent_days"]])
    rows = []
    for cat, g in o.groupby("category"):
        key = next((k for k in mapping if k in str(cat).lower()), None)
        if key is None:
            continue
        r = g[g["record_date"].isin(recent)]["amount_musd"].sum() / max(len(recent), 1)
        b = g[g["record_date"].isin(base)]["amount_musd"].sum() / max(len(base), 1)
        if b <= 0:
            continue
        rows.append({"category": cat, "sectors": ",".join(mapping[key]), "recent_daily_musd": round(r, 2),
                     "base_daily_musd": round(b, 2), "ratio": round(r / b, 3)})
    mom = pd.DataFrame(rows)
    if mom.empty:
        return mult, mom
    for s in SECTORS:
        rs = mom[mom["sectors"].str.contains(s, regex=False)]["ratio"]
        if len(rs):
            mult[s] = float(np.clip(rs.median(), cfg["min"], cfg["max"]))
    return mult, mom


# ---------------------------------------------------------------- olay parametreleri

def _stage_key(row) -> str:
    st = int(row["stage"])
    try:
        extra = json.loads(row.get("extra") or "{}")
    except (TypeError, ValueError):
        extra = {}
    if st == 1 and extra.get("status") == "passed_chamber":
        return "1_passed_chamber"
    if st == 3 and extra.get("doc_type") == "proposed_rule":
        return "3_proposed"
    return str(st)


def event_parameters(ev: pd.DataFrame, asof: date, state: dict) -> pd.DataFrame:
    pr = priors()
    eff_prog = state.get("program_efficacy", {})
    e = ev.copy()
    e["stage"] = e["stage"].astype(int)
    e["date_dt"] = pd.to_datetime(e["date"], errors="coerce")
    e = e.dropna(subset=["date_dt"])
    e["age_days"] = (pd.Timestamp(asof) - e["date_dt"]).dt.days.clip(lower=0)
    st = e["stage"].astype(str)
    e = e[e["age_days"] <= st.map(lambda s: pr["max_age_days"][s])]
    if e.empty:
        return e
    st = e["stage"].astype(str)
    amt = pd.to_numeric(e["amount_usd"], errors="coerce")
    known = amt.notna() & (amt > 0)
    e["amount_known"] = known
    e["amount_base"] = np.where(known, amt, st.map(lambda s: pr["default_amount_usd"][s]))
    e["sigma"] = np.where(known, pr["amount_sigma_known"], pr["amount_sigma_unknown"])
    e["p_stage"] = [pr["stage_probability"][k] for k in (_stage_key(r) for r in e.to_dict("records"))]
    e["decay"] = 0.5 ** (e["age_days"] / st.map(lambda s: pr["half_life_days"][s]))
    # Aynı program+aşamada tutarı bilinmeyen belgeler: azalan getiri (çift sayımı önler).
    e = e.sort_values("date_dt", ascending=False)
    e["_rank"] = e.groupby(["program", "stage", "amount_known"]).cumcount() + 1
    e["dup_factor"] = np.where(e["amount_known"], 1.0, 1.0 / np.sqrt(e["_rank"]))
    e["prog_eff"] = e["program"].map(lambda p: float(eff_prog.get(p, 1.0)))
    for j, h in enumerate(pr["horizons_days"]):
        base = e["incentive_type"].map(lambda t: pr["timing_share"].get(t, pr["timing_share"]["mixed"])[j])
        boost = st.map(lambda s: pr["stage_timing_boost"][s])
        e[f"t_{h}"] = np.clip(base * boost, 0.0, 1.0)
    e["early"] = e["stage"] <= 4
    return e


# ---------------------------------------------------------------- Monte Carlo

def simulate(ev: pd.DataFrame, asof: date, state: dict, regime_mult: float,
             dts_mult: dict[str, float], seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pr = priors()
    H = pr["horizons_days"]
    N = int(pr["mc_samples"])
    e = event_parameters(ev, asof, state)
    sector_bias = state.get("sector_efficacy", {})
    if e.empty:
        return pd.DataFrame(), pd.DataFrame(), e
    rng = np.random.default_rng(seed)
    E = len(e)
    W = np.zeros((E, len(SECTORS)))
    for i, sw in enumerate(e["sector_weights"]):
        d = json.loads(sw) if isinstance(sw, str) else (sw or {})
        tot = sum(d.values()) or 1.0
        for s, w in d.items():
            if s in SECTORS:
                W[i, SECTORS.index(s)] = w / tot
    det = (e["amount_base"] * e["decay"] * e["dup_factor"] * e["prog_eff"]).to_numpy()
    sig = e["sigma"].to_numpy()
    p = e["p_stage"].clip(0.01, 0.999).to_numpy()
    c = pr["probability_concentration"]
    early = e["early"].to_numpy()
    common = np.exp(pr["common_policy_sigma"] * rng.standard_normal(N) - pr["common_policy_sigma"] ** 2 / 2)
    sect_mult = np.array([regime_mult * dts_mult.get(s, 1.0) * float(sector_bias.get(s, 1.0)) for s in SECTORS])
    sect_noise = np.exp(pr["sector_model_sigma"] * rng.standard_normal((len(SECTORS), N)) - pr["sector_model_sigma"] ** 2 / 2)

    flows = np.zeros((len(H), len(SECTORS), N))
    chunk = 1500
    for a in range(0, E, chunk):
        b = min(E, a + chunk)
        z = rng.standard_normal((b - a, N))
        amt_mult = np.exp(sig[a:b, None] * z - sig[a:b, None] ** 2 / 2)
        pp = rng.beta(p[a:b, None] * c, (1 - p[a:b, None]) * c, size=(b - a, N))
        r = det[a:b, None] * amt_mult * pp
        r = np.where(early[a:b, None], r * common[None, :], r)
        for j, h in enumerate(H):
            t = e[f"t_{h}"].to_numpy()[a:b]
            flows[j] += (W[a:b] * t[:, None]).T @ r
    flows *= (sect_mult[:, None] * sect_noise)[None, :, :]

    lo, hi = pr["score_log10_floor"], pr["score_log10_cap"]
    rows = []
    for j, h in enumerate(H):
        for k, s in enumerate(SECTORS):
            q10, q50, q90 = np.percentile(flows[j, k], [10, 50, 90])
            score = 100 * np.clip((math.log10(max(q50, 1.0)) - lo) / (hi - lo), 0, 1)
            rows.append({"sector": s, "horizon_days": h, "p10": q10, "p50": q50, "p90": q90,
                         "score": round(float(score), 1), "n_events": int((W[:, k] > 0).sum()),
                         "sector_mult": round(float(sect_mult[k]), 3)})
    scores = pd.DataFrame(rows)

    # Program × sektör beklenen katkı (1 yıl ufku, ortalama).
    h1 = H[1]
    mean_r = det * p
    contrib = (W * (mean_r * e[f"t_{h1}"].to_numpy())[:, None]) * sect_mult[None, :]
    pc = pd.DataFrame(contrib, columns=SECTORS)
    pc["program"] = e["program"].to_numpy()
    prog = pc.groupby("program").sum().reset_index().melt(id_vars="program", var_name="sector", value_name="expected_usd_1y")
    prog = prog[prog["expected_usd_1y"] > 0]

    e = e.assign(expected_usd_3y=mean_r * e[f"t_{H[2]}"].to_numpy() * float(np.mean(sect_mult)))
    return scores, prog, e


def company_exposure(e: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    if e is None or e.empty:
        return pd.DataFrame()
    x = e[e["tickers"].fillna("") != ""].copy()
    if x.empty:
        return pd.DataFrame()
    x["ticker"] = x["tickers"].str.split(",")
    x = x.explode("ticker")
    x["ticker"] = x["ticker"].str.strip()
    x["is_award"] = x["source"] == "usaspending"
    x["is_filing"] = x["source"] == "sec_edgar"
    g = x.groupby("ticker").agg(
        expected_usd_3y=("expected_usd_3y", "sum"),
        awards_usd=("amount_usd", lambda s: float(pd.to_numeric(s, errors="coerce").where(x.loc[s.index, "is_award"]).sum())),
        n_awards=("is_award", "sum"), n_filings=("is_filing", "sum"),
        programs=("program", lambda s: ", ".join(sorted(set(s)))),
        last_date=("date", "max"),
        best_match=("match_method", lambda s: "sec" if s.isna().all() or (s.fillna("") == "").all() else ",".join(sorted(set(s.fillna("")) - {""}))),
    ).reset_index()
    if universe is not None and not universe.empty:
        cols = [c for c in ["ticker", "name", "sector", "sp500_weight", "r2000_weight"] if c in universe.columns]
        g = g.merge(universe[cols].drop_duplicates("ticker"), on="ticker", how="left")
    g["exposure_score"] = (np.log10(g["expected_usd_3y"].clip(lower=1)) * 10 + g["n_filings"] * 2).round(1)
    return g.sort_values("exposure_score", ascending=False)
