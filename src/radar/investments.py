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
    qh = corp_invest.query_hash()
    # Sorgu kümesi değişince eski pencereler yeni sorgularla yeniden taranır.
    done = set()
    if not prog.empty and "qhash" in prog:
        done = set(prog.loc[prog["qhash"].astype(str) == qh, "start"].astype(str))
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
        store.append("invest_progress", pd.DataFrame([{"start": a.isoformat(), "end": b.isoformat(), "events": n, "qhash": qh}]))
        out["windows_done"] += 1
        out["events"] += n
    return out


# ---------------------------------------------------------------- ölçüm

def _ecfg(lag: int = 1) -> dict:
    # Pencereler olay gününü ATLAR (lag=1: olay günü kapanışında girilir; lag=2: ertesi gün kapanışı): haber/bildirim gün içinde çıkmış olabilir,
    # o günkü hareket işlem yapılabilir değildir. Anahtar adları geriye dönük uyumluluk için eski kaldı.
    return {"est_window": E.cfg()["est_window"], "min_est_obs": E.cfg()["min_est_obs"],
            "windows": {"0_3": [lag, lag + 3], "0_5": [lag, lag + 5], "0_20": [lag, lag + 20], "0_60": [lag, lag + 60]}}


def _abnormal(y: np.ndarray, m: np.ndarray, k0: int, c: dict) -> dict | None:
    """Piyasa modeli, ALFA OLMADAN: ar = y − beta·m. Tahmin penceresindeki alfa dışarı taşınmaz.
    (Düşüşten sonra alım yapılan hisselerde negatif alfa, CAR'ı yapay şişiriyordu.)"""
    lo, hi = k0 + c["est_window"][0], k0 + c["est_window"][1]
    if lo < 1:
        return None
    ye, me = y[lo:hi], m[lo:hi]
    ok = np.isfinite(ye) & np.isfinite(me)
    if ok.sum() < c["min_est_obs"]:
        return None
    beta = float(np.clip(np.polyfit(me[ok], ye[ok], 1)[0], 0.0, 3.0))
    out = {}
    for name, (a, b) in c["windows"].items():
        s, e = k0 + a, k0 + b
        out[f"car_{name}"] = np.nan
        if s < 0 or e >= len(y):
            continue
        ar = y[s:e + 1] - beta * m[s:e + 1]
        if np.isfinite(ar).all():
            out[f"car_{name}"] = float(ar.sum())
    out["beta"] = round(beta, 3)
    return out


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


def _weights() -> dict:
    u = store.read("universe")
    if u.empty or "sp500_weight" not in u:
        return {}
    return dict(zip(u["ticker"], pd.to_numeric(u["sp500_weight"], errors="coerce").fillna(0.0)))


def measure(events: pd.DataFrame, prices: pd.DataFrame, lag: int | None = None) -> pd.DataFrame:
    """Her olay için 5/20/60 günlük anormal getiri (piyasa modeli)."""
    if events is None or events.empty or prices is None or prices.empty:
        return pd.DataFrame()
    bench = sources()["benchmark"]
    rets = E._returns(prices)
    if bench not in rets.columns:
        return pd.DataFrame()
    idx, m = rets.index, rets[bench].to_numpy()
    c = _ecfg(int(cfg().get("entry_lag", 1) if lag is None else lag))
    wt = _weights()
    min_px = float(cfg().get("min_price", 0) or 0)
    prices_px = {}
    if min_px:
        pp = prices.assign(close=pd.to_numeric(prices["close"], errors="coerce"), date=pd.to_datetime(prices["date"], errors="coerce"))
        prices_px = pp.dropna(subset=["close", "date"]).pivot_table(index="date", columns="ticker", values="close",
                                                                    aggfunc="last").reindex(idx).ffill()
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
            px = prices_px.get(t)
            if min_px and (px is None or px.iloc[min(k0, len(px) - 1)] < min_px):
                continue   # düşük fiyatlı hisse: spread/likidite nedeniyle işlem yapılabilir değil
            ab = _abnormal(rets[t].to_numpy(), m, k0, c)
        if ab is None:
            continue
        try:
            cat = json.loads(r.get("extra") or "{}").get("category", "other")
        except ValueError:
            cat = "other"
        rows.append({"event_id": r["event_id"], "ticker": t, "date": d.date().isoformat(), "d": d,
                     "category": cat, "amount_usd": r.get("amount_usd"), "sp500_weight": wt.get(t, 0.0), **{k: v for k, v in ab.items() if k.startswith("car_")}})
    out = pd.DataFrame(rows)
    return _thin(out, int(cfg()["gate"]["min_gap_days"])).drop(columns="d") if not out.empty else out


def _stats(g: pd.DataFrame, gate: dict) -> dict:
    g = g.dropna(subset=["car_0_60"]).copy()
    clip = gate["clip_car"]
    for col in ("car_0_5", "car_0_20", "car_0_60"):
        g[col] = g[col].clip(-clip, clip)
    if "car_0_3" not in g:
        g["car_0_3"] = np.nan
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
    sub = build_subgroups(ret, store.read("invest_subgroups"))
    store.write("invest_subgroups", sub)
    if len(sub):
        log.info("Alt grup keşfi: %d test, %d aday, %d doğrulanmış", len(sub), int((sub["status"] == "candidate").sum()),
                 int((sub["status"] == "confirmed").sum()))
    store.write("invest_signals", signals(events, ev))
    log.info("Büyük yatırım kanıtı: %d kategori, %d kanıtlı", len(ev), int((ev["status"] == "proven").sum()) if len(ev) else 0)
    return ev


def tickers(limit: int = 600) -> list[str]:
    ev = store.read("invest_events")
    if ev.empty:
        return []
    t = ev.sort_values("date", ascending=False)["tickers"].astype(str).str.split(",").str[0]
    return list(dict.fromkeys(x.strip() for x in t if x.strip()))[:limit]


# ---------------------------------------------------------------- keşif katmanı (alt gruplar)
# Ana kategori testi sonuç vermedi. Burada büyüklük / göreli tutar / kısa ufuk / düşüş yönü ayrı ayrı denenir.
# Çok sayıda test yapıldığı için: (1) tüm testler birlikte BH ile düzeltilir, (2) işlem maliyeti düşülür,
# (3) kapıyı geçen grup yalnızca "aday" olur; keşiften SONRAKİ yeni olaylarda tekrar ederse "doğrulandı" olur.

SUB_COLS = ["key", "group", "group_name", "horizon", "direction", "n", "mean", "hit", "p", "q", "oos_n", "oos_hit",
            "oos_mean", "status", "status_name", "icon", "explain", "first_pass", "confirm_n", "asof"]
SUB_STATUS = {"confirmed": "Doğrulandı", "candidate": "Aday (yeni veriyle doğrulanmalı)", "no_effect": "Etkisiz",
              "unclear": "Belirsiz", "collecting": "Veri birikiyor", "promising": "Umut verici"}
HORIZONS = {"car_0_3": "3 gün", "car_0_60": "60 gün"}


def _groups(ret: pd.DataFrame, scfg: dict) -> list[tuple[str, str, pd.DataFrame]]:
    big = ret["sp500_weight"] >= scfg["large_weight"]
    rel = (ret["amount_usd"] / (ret["sp500_weight"].clip(lower=1e-6) / 100 * scfg["index_cap_usd"]))
    out = [("all", "Tümü", ret), ("large", "Büyük şirket (S&P 500 ağırlığı yüksek)", ret[big]),
           ("small", "Küçük / orta şirket", ret[~big]),
           ("rel_big", f"Tutar şirket değerinin ≥%{scfg['rel_big'] * 100:.0f}'i", ret[big & (rel >= scfg["rel_big"])]),
           ("rel_small", "Tutar şirkete göre küçük", ret[big & (rel < scfg["rel_big"])])]
    for c, g in ret.groupby("category"):
        out.append((f"cat:{c}", CATEGORY_NAMES.get(c, c), g))
        out.append((f"cat:{c}|small", CATEGORY_NAMES.get(c, c) + " · küçük/orta", g[g["sp500_weight"] < scfg["large_weight"]]))
    return out


def build_subgroups(ret: pd.DataFrame, prev: pd.DataFrame | None = None) -> pd.DataFrame:
    gate, scfg = cfg()["gate"], cfg()["subgroups"]
    if ret is None or ret.empty or "car_0_3" not in ret:
        return pd.DataFrame(columns=SUB_COLS)
    cost = scfg["cost_bps"] / 1e4
    prev_pass = {}
    if prev is not None and not prev.empty and "first_pass" in prev:
        prev_pass = {k: v for k, v in zip(prev["key"], prev["first_pass"]) if isinstance(v, str) and v}
    rows = []
    for gkey, gname, g in _groups(ret, scfg):
        for hcol, hname in HORIZONS.items():
            for direction, sign in (("uzun", 1), ("kısa", -1)):
                x = g.copy()
                # _stats/classify 60g sütununu okur: işaretli ve maliyetli getiriyi oraya koyuyoruz
                x["car_0_60"] = sign * x[hcol] - cost
                x["car_0_5"] = x["car_0_20"] = x["car_0_60"]
                rows.append({"key": f"{gkey}|{hcol}|{direction}", "group": gkey, "group_name": gname,
                             "horizon": hname, "direction": direction, **_stats(x, gate), "_x": x})
    ev = pd.DataFrame(rows)
    ev["q"] = E._bh(ev["p"])   # tüm denenen testler birlikte düzeltilir
    today = date.today().isoformat()
    status, first, cn, expl = [], [], [], []
    for r in ev.to_dict("records"):
        st = classify({**r, "mean_car_60": r["mean_car_60"], "hit_60": r["hit_60"]}, gate)
        fp, n2 = prev_pass.get(r["key"], ""), 0
        if st == "proven":
            fp = fp or today
            fresh = r["_x"][r["_x"]["date"].astype(str) > fp]
            fs = _stats(fresh, gate) if len(fresh) else {"n": 0}
            n2 = fs["n"]
            ok = n2 >= scfg["confirm_n"] and fs["mean_car_60"] > 0 and fs["hit_60"] >= gate["oos_hit"]
            st = "confirmed" if ok else "candidate"
        elif fp:
            fp = ""   # kapıdan düştü: aday durumu sıfırlanır
        status.append(st); first.append(fp); cn.append(n2)
        expl.append(f"{r['n']} haber; {r['horizon']} sonunda piyasaya göre {r['direction']} yönde işaretli ortalama "
                    f"{r['mean_car_60'] * 100:+.1f}% (maliyet düşülmüş), %{r['hit_60'] * 100:.0f} isabet." if r["n"] else "Veri yok.")
    ev["status"], ev["first_pass"], ev["confirm_n"], ev["explain"] = status, first, cn, expl
    ev = ev.rename(columns={"mean_car_60": "mean", "hit_60": "hit"})
    ev["status_name"] = ev["status"].map(SUB_STATUS)
    ev["icon"] = ev["status"].map({"confirmed": "🟢", "candidate": "🟡", "promising": "🟡", "no_effect": "⚫"}).fillna("⚪")
    ev["asof"] = today
    return ev[SUB_COLS].sort_values(["status", "q"]).reset_index(drop=True)
