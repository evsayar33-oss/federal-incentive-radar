"""Uzun vadeli kırılım (breakout) stratejisi: önce geçmişte kanıtla, sonra işlem planı üret.

Videodaki gün içi asistanın uzun vadeli (günlük mum, haftalar-aylar tutma) karşılığı. Hepsi ücretsiz günlük fiyat verisiyle.
Sinyal: kapanış, önceki `lookback` günün en yüksek kapanışını aşar (52 hafta zirvesi kırılımı) ve fiyat ≥ min_price.
Giriş: sinyalden `entry_lag` işlem günü sonraki kapanış. Çıkış: `horizon` işlem günü sonra (60 / 120).
Getiri: SPY'a göre anormal (beta, alfasız, yalnızca geçmiş veriyle) − işlem maliyeti.
Kontrol (plasebo): AYNI hissenin sinyal olmayan, ±`placebo_span` gün içindeki rastgele günlerinin ortalaması.
   Böylece "bu hisse zaten yükseliyordu" ve piyasa rejimi etkisi ayıklanır.
Bağlam: kırılımdan önceki `news_days` günde aynı sektörde büyük olay (federal/yatırım/içeriden alım) var mı → ayrı grup.
Kapılar yatırım bölümüyle aynıdır (`corp_invest.gate`), tüm gruplar birlikte BH ile düzeltilir; yalnızca "proven"
olan kural için canlı işlem planı üretilir. Otomatik emir YOKTUR; plan yalnızca bilgi/onay içindir.
"""
from __future__ import annotations

import logging
import zlib
from datetime import date

import numpy as np
import pandas as pd

from . import basket as BK
from . import evidence as E
from . import investments as I
from . import store
from .settings import sources

log = logging.getLogger("radar.trade")
COLS = ["key", "group", "group_name", "horizon", "n", "mean_car_60", "hit_60", "p", "q", "oos_n", "oos_hit", "oos_mean",
        "status", "status_name", "icon", "explain", "asof", "mc_p05", "mc_dd95", "raw_mean", "raw_hit"]


def cfg() -> dict:
    return sources()["trade"]


def _panel(prices: pd.DataFrame):
    bench = sources()["benchmark"]
    rets = E._returns(prices)
    if bench not in rets.columns:
        return None
    px = prices.assign(close=pd.to_numeric(prices["close"], errors="coerce"), date=pd.to_datetime(prices["date"], errors="coerce"))
    px = px.dropna(subset=["close", "date"]).pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    px = px.reindex(rets.index).ffill(limit=5)
    return bench, rets, px


def measure(prices: pd.DataFrame, sectors: dict, news: pd.DataFrame | None = None) -> pd.DataFrame:
    """Tüm hisselerde kırılım sinyalleri ve her biri için kontrol-ayarlı ileri getiri (horizon başına sütun)."""
    c = cfg()
    pan = _panel(prices)
    if pan is None:
        return pd.DataFrame()
    bench, rets, px = pan
    idx, m = rets.index, rets[bench]
    lb, lag, hs = int(c["lookback"]), int(c["entry_lag"]), [int(h) for h in c["horizons"]]
    cost, span, min_px = float(c["cost_bps"]) / 1e4, int(c["placebo_span"]), float(c["min_price"])
    ok = rets.notna().sum()
    cols = [t for t in rets.columns if t != bench and ok[t] > lb + max(hs) + 60 and px[t].max() < 1e5]
    # tüm sektör olay günleri (bağlam grubu için)
    ev_days: dict[str, np.ndarray] = {}
    if news is not None and not news.empty:
        for s, g in news.groupby("sector"):
            ev_days[s] = np.sort(idx.searchsorted(pd.to_datetime(g["date"])))
    rows = []
    mv = m.rolling(126, min_periods=100).var()
    for t in cols:
        y = rets[t]
        beta = (y.rolling(126, min_periods=100).cov(m) / mv).shift(1).clip(0, 3)   # yalnızca geçmiş
        ar = (y - beta * m).to_numpy()
        cs = np.nancumsum(np.nan_to_num(ar))
        close = px[t]
        hi = close.shift(1).rolling(lb, min_periods=int(lb * 0.8)).max()
        sig = ((close > hi) & (close >= min_px)).to_numpy()
        ks = np.flatnonzero(sig)
        if len(ks) == 0:
            continue
        # sinyal kümelerini seyrelt: aynı hissede 60 gün içindeki ardışık sinyaller tek sayılır
        keep, last = [], -10**9
        for k in ks:
            if k - last >= 60:
                keep.append(k); last = k
        ks_set = ks
        def fwd(k, h):
            a, b = k + lag, k + lag + h
            if k < 130 or b >= len(idx) or not np.isfinite(ar[k + lag:b + 1]).all():
                return np.nan
            return cs[b] - cs[a - 1]
        rng = np.random.default_rng(zlib.crc32(t.encode()))
        sec = sectors.get(t)
        for k in keep:
            res = {}
            lo, hi_ = max(130, k - span), min(len(idx) - max(hs) - lag - 2, k + span)
            if hi_ <= lo:
                continue
            cand = [d for d in rng.permutation(np.arange(lo, hi_))[:80] if np.abs(ks_set - d).min() > 30][:int(c["placebo_n"])]
            if len(cand) < 5:
                continue
            for h in hs:
                r = fwd(k, h)
                pl = [fwd(int(d), h) for d in cand]
                pl = [x for x in pl if np.isfinite(x)]
                if not np.isfinite(r) or len(pl) < 4:
                    res = {}
                    break
                res[f"car_h{h}"] = float(np.clip(r - np.mean(pl) - cost, -1, 1))
                res[f"raw_h{h}"] = float(np.clip(r - cost, -1, 1))
            if not res:
                continue
            ctx = False
            if sec in ev_days:
                a = ev_days[sec]
                ctx = bool(((a <= k) & (a > k - int(c["news_days"]))).any())
            rows.append({"ticker": t, "date": idx[k].date().isoformat(), "sector": sec or "", "news": ctx,
                         "sp500_weight": 0.0, **res})
    return pd.DataFrame(rows)


def monte_carlo(returns: np.ndarray, n_sims: int = 10000, trades: int = 40, seed: int = 0) -> dict:
    """Kuralın geçmiş işlem getirilerinden yeniden örnekleme: kötü senaryo ve en kötü düşüş aralığı."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return {"mc_p05": np.nan, "mc_dd95": np.nan}
    rng = np.random.default_rng(seed)
    pick = rng.choice(r, size=(n_sims, trades), replace=True) * float(cfg()["risk_per_trade"])
    eq = np.cumprod(1 + pick, axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = ((peak - eq) / peak).max(axis=1)
    return {"mc_p05": float(np.percentile(eq[:, -1] - 1, 5)), "mc_dd95": float(np.percentile(dd, 95))}


def build(ret: pd.DataFrame) -> pd.DataFrame:
    gate = sources()["corp_invest"]["gate"]
    if ret is None or ret.empty:
        return pd.DataFrame(columns=COLS)
    rows = []
    for h in [int(x) for x in cfg()["horizons"]]:
        r = ret.assign(car_0_60=ret[f"car_h{h}"], car_0_5=ret[f"car_h{h}"], car_0_20=ret[f"car_h{h}"])
        for name, g in (("all", r), ("news", r[r["news"]]), ("nonews", r[~r["news"]])):
            raw = g[f"raw_h{h}"].to_numpy()
            rows.append({"group": name, "horizon": h, **I._stats(g, gate), **monte_carlo(raw),
                         "raw_mean": float(np.nanmean(raw)) if len(raw) else np.nan,
                         "raw_hit": float(np.nanmean(raw > 0)) if len(raw) else np.nan})
    ev = pd.DataFrame(rows)
    ev["q"] = E._bh(ev["p"])
    ev["status"] = [I.classify(r, gate) for r in ev.to_dict("records")]
    # Çift kapı: kontrol hissesi hayatta kalma yanlılığı taşıyabilir. Piyasaya göre ham getiri de kapıyı geçmeli.
    for i, r in ev.iterrows():
        if r["status"] in ("proven", "promising") and not (r["raw_mean"] >= gate["min_mean_promising"] and r["raw_hit"] >= gate["hit_promising"]):
            ev.at[i, "status"] = "unclear"
    nm = {"all": "Tüm 52 hf kırılımları", "news": "Kırılım + sektörde büyük haber", "nonews": "Kırılım, haber yok"}
    ev["key"] = ev["group"] + "|" + ev["horizon"].astype(str)
    ev["group_name"] = ev["group"].map(nm)
    ev["status_name"] = ev["status"].map(I.STATUS_NAMES)
    ev["icon"] = ev["status"].map(I.ICONS)
    ev["explain"] = [f"{r['n']} kırılım; {r['horizon']} işlem günü sonunda, aynı hissenin sıradan günlerine ve piyasaya göre "
                     f"{r['mean_car_60'] * 100:+.1f}% (kontrol-ayarlı), piyasaya göre ham {r['raw_mean'] * 100:+.1f}%, %{r['hit_60'] * 100:.0f} pozitif (maliyet düşülmüş)." if r["n"] else "Veri yok."
                     for r in ev.to_dict("records")]
    ev["asof"] = date.today().isoformat()
    return ev.reindex(columns=COLS)


def position_size(entry: float, stop: float, equity: float | None = None, risk_pct: float | None = None,
                  max_pos_pct: float | None = None) -> dict:
    """Sabit risk: hesabın risk_pct'sini aşmayacak adet; tek pozisyon max_pos_pct'i geçmez (risk yöneticisi)."""
    c = cfg()
    equity = float(equity or c["equity"])
    risk_pct = float(risk_pct or c["risk_pct"])
    max_pos_pct = float(max_pos_pct or c["max_position_pct"])
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return {"shares": 0, "risk_usd": 0.0, "position_usd": 0.0}
    shares = int(min(equity * risk_pct / per_share, equity * max_pos_pct / entry))
    return {"shares": shares, "risk_usd": round(shares * per_share, 2), "position_usd": round(shares * entry, 2)}


def plans(prices: pd.DataFrame, ev: pd.DataFrame, sectors: dict, news: pd.DataFrame | None) -> pd.DataFrame:
    """Bugünkü kırılımlar için işlem planı. Yalnızca kanıtlanmış (proven) kural varsa üretilir."""
    from .research import confirmed_keys

    if ev is None or ev.empty or not (ev["status"] == "proven").any():
        return pd.DataFrame()
    if not set(ev.loc[ev["status"] == "proven", "key"].astype(str)) & confirmed_keys("trade_evidence"):
        return pd.DataFrame()
    pan = _panel(prices)
    if pan is None:
        return pd.DataFrame()
    bench, rets, px = pan
    c = cfg()
    lb = int(c["lookback"])
    last = px.iloc[-1]
    hi = px.shift(1).rolling(lb, min_periods=int(lb * 0.8)).max().iloc[-1]
    vol = rets.rolling(60, min_periods=40).std().iloc[-1]
    rows = []
    for t in px.columns:
        if t == bench or not (last[t] > hi[t] and last[t] >= float(c["min_price"])) or not np.isfinite(vol[t]):
            continue
        entry = float(last[t])
        stop = entry * (1 - float(c["stop_vol_mult"]) * vol[t] * np.sqrt(20))
        rows.append({"ticker": t, "sector": sectors.get(t, ""), "entry": round(entry, 2), "stop": round(stop, 2),
                     "target": round(entry * (1 + float(c["target_mult"]) * (entry - stop) / entry), 2),
                     **position_size(entry, stop), "asof": date.today().isoformat()})
    return pd.DataFrame(rows)


def refresh(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    if not cfg().get("enabled", True):
        return pd.DataFrame()
    prices = store.read("prices") if prices is None else prices
    if prices is None or prices.empty:
        return store.read("trade_evidence")
    sectors = BK.sector_map()
    news = BK.event_frame(sectors)
    ret = measure(prices, sectors, news)
    if not ret.empty:
        store.write("trade_returns", ret)
    ev = build(ret)
    store.write("trade_evidence", ev)
    store.write("trade_plans", pd.DataFrame())
    log.info("Kırılım stratejisi: %d grup, %d kanıtlı", len(ev), int((ev["status"] == "proven").sum()) if len(ev) else 0)
    return ev


def refresh_plans(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rebuild plans after the shared research registry has applied the global gate."""
    prices = store.read("prices") if prices is None else prices
    ev = store.read("trade_evidence")
    sectors = BK.sector_map()
    plans_df = plans(prices, ev, sectors, BK.event_frame(sectors))
    store.write("trade_plans", plans_df)
    return plans_df
