"""Büyüme sepeti testi: haber, haberin olduğu alandaki EN GÜÇLÜ BÜYÜME hisselerini gerçekten yükseltiyor mu?

Her olay için (federal haber, büyük yatırım, içeriden alım) olayın sektörü bulunur. Olay gününden ÖNCEKİ verilerle
sektörün en güçlü `top_n` büyüme hissesi seçilir (büyüme vekili: 12-1 ay getiri ivmesi; yalnızca fiyat verisi, ücretsiz).
Sepetin haberden sonraki anormal getirisi (piyasa modeli, alfasız, işlem yapılabilir giriş gecikmesiyle), AYNI sektörde
haber olmayan rastgele günlerde (plasebo) aynı kuralla seçilen sepetin getirisiyle karşılaştırılır. Fark = haberin
sepete katkısı. Böylece "büyüme hisseleri zaten yükselir" etkisi ayıklanır. Kanıt kapıları yatırım bölümüyle aynıdır
(`corp_invest.gate`) ve tüm gruplar birlikte BH ile düzeltilir. Bildirim yoktur; yalnızca bilgi.
"""
from __future__ import annotations

import json
import logging
import warnings
import zlib
from datetime import date

import numpy as np
import pandas as pd

from . import evidence as E
from . import investments as I
from . import store
from .settings import sources

log = logging.getLogger("radar.basket")
COLS = ["key", "group", "group_name", "n", "mean_car_5", "mean_car_20", "mean_car_60", "hit_60", "p", "q", "oos_n",
        "oos_hit", "oos_mean", "status", "status_name", "icon", "explain", "asof"]


def cfg() -> dict:
    return sources()["basket"]


def sector_map() -> dict:
    out: dict = {}
    sic = store.read("company_sic")
    if not sic.empty:
        out.update({t: s for t, s in zip(sic["ticker"], sic["sector"]) if isinstance(s, str) and s})
    uni = store.read("universe")
    if not uni.empty:
        out.update({t: s for t, s in zip(uni["ticker"], uni["sector"]) if isinstance(s, str) and s})
    return out


def event_frame(sectors: dict) -> pd.DataFrame:
    """Üç kaynaktan (group, sector, date) olayları."""
    c = cfg()
    rows: list[dict] = []
    for table, pre in (("invest_events", "invest"), ("insider_events", "insider")):
        ev = store.read(table)
        for r in ev.to_dict("records"):
            sec = sectors.get(str(r["tickers"]).split(",")[0].strip())
            if not sec:
                continue
            try:
                cat = json.loads(r.get("extra") or "{}").get("category", "other")
            except ValueError:
                cat = "other"
            rows.append({"group": f"{pre}:{cat}", "sector": sec, "date": str(r["date"])[:10]})
    fed = store.read("events")
    if not fed.empty:
        fed = fed[(fed["relevant"].fillna(False).astype(bool)) & (fed["program"].astype(str) != "other")]
        fed = fed.assign(sector=[E._dominant_sector(x) for x in fed["sector_weights"]])
        fed = fed.dropna(subset=["sector"]).drop_duplicates(["program", "sector", "date"])
        fed = fed.sample(min(len(fed), int(c["max_federal"])), random_state=0)
        rows += [{"group": f"fed:{p}", "sector": s, "date": str(d)[:10]}
                 for p, s, d in zip(fed["program"], fed["sector"], fed["date"])]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # aynı (grup, sektör) için üst üste binen pencereleri azalt
    df["ticker"] = df["group"] + "|" + df["sector"]
    df["d"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["d"]).drop_duplicates(["ticker", "date"])
    return I._thin(df, int(sources()["corp_invest"]["gate"]["min_gap_days"]))


def measure(events: pd.DataFrame, prices: pd.DataFrame, sectors: dict) -> pd.DataFrame:
    c = cfg()
    if events is None or events.empty or prices is None or prices.empty:
        return pd.DataFrame()
    bench = sources()["benchmark"]
    rets = E._returns(prices)
    if bench not in rets.columns:
        return pd.DataFrame()
    idx, m = rets.index, rets[bench].to_numpy()
    px = prices.assign(close=pd.to_numeric(prices["close"], errors="coerce"), date=pd.to_datetime(prices["date"], errors="coerce"))
    px = px.dropna(subset=["close", "date"]).pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    px = px.reindex(idx).ffill(limit=5)
    mom = px.shift(int(c["skip_days"])) / px.shift(int(c["mom_days"])) - 1   # 12-1 ay ivmesi, yalnızca geçmiş
    ok = rets.notna().sum()
    pools: dict[str, list[str]] = {}
    for t in rets.columns:
        s = sectors.get(t)
        if s and t != bench and ok[t] > 400:
            pools.setdefault(s, []).append(t)
    ec = I._ecfg(int(c["entry_lag"]))
    min_px, top_n = float(c["min_price"]), int(c["top_n"])
    wins = [f"car_{w}" for w in ec["windows"]]

    def basket(sec: str, k0: int) -> dict | None:
        pool = pools.get(sec, [])
        if len(pool) < top_n * 3 or k0 < 260 or k0 >= len(idx):
            return None
        sc = mom.iloc[k0 - 1][pool]
        sc = sc[(px.iloc[k0 - 1][pool] >= min_px) & sc.notna()]
        if len(sc) < top_n * 2:
            return None
        picks = list(sc.sort_values(ascending=False).index[:top_n])
        acc = []
        with np.errstate(all="ignore"):
            for t in picks:
                a = I._abnormal(rets[t].to_numpy(), m, k0, ec)
                if a is not None:
                    acc.append(a)
        if len(acc) < max(3, top_n - 2):
            return None
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return {k: float(np.nanmean([a[k] for a in acc])) for k in wins}

    ev_days: dict[str, set[int]] = {}
    for r in events.to_dict("records"):
        ev_days.setdefault(r["sector"], set()).add(int(idx.searchsorted(pd.Timestamp(r["date"]))))
    rows = []
    for r in events.to_dict("records"):
        k0 = int(idx.searchsorted(pd.Timestamp(r["date"])))
        real = basket(r["sector"], k0)
        if real is None:
            continue
        rng = np.random.default_rng(zlib.crc32(f"{r['group']}|{r['sector']}|{r['date']}".encode()))
        blocked = ev_days[r["sector"]]
        plc = []
        for k in rng.permutation(np.arange(260, max(261, len(idx) - 70)))[:60]:
            if any(abs(int(k) - b) < 40 for b in blocked):
                continue
            b = basket(r["sector"], int(k))
            if b is not None and np.isfinite(b["car_0_60"]):
                plc.append(b)
            if len(plc) >= int(c["placebo_n"]):
                break
        if len(plc) < 2:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            ctl = {k: float(np.nanmean([b[k] for b in plc])) for k in wins}
        rows.append({"group": r["group"], "sector": r["sector"], "date": r["date"],
                     **{k: real[k] - ctl[k] for k in wins}, **{f"raw_{k}": real[k] for k in wins}})
    return pd.DataFrame(rows)


def _name(g: str) -> str:
    kind, _, rest = g.partition(":")
    pre = {"fed": "Federal haber", "invest": "Büyük yatırım", "insider": "İçeriden alım"}.get(kind, kind)
    return f"{pre} · " + ("tümü" if rest == "all" else I.CATEGORY_NAMES.get(rest, rest))


def build(ret: pd.DataFrame) -> pd.DataFrame:
    gate = sources()["corp_invest"]["gate"]
    if ret is None or ret.empty:
        return pd.DataFrame(columns=COLS)
    groups = [("all", ret)] + [(k, g) for k, g in ret.groupby("group")]
    groups += [(f"{k}:all", g) for k, g in ret.assign(k=ret["group"].str.split(":").str[0]).groupby("k")]
    rows = [{"group": k, **I._stats(g.assign(sp500_weight=0.0), gate)} for k, g in groups]
    ev = pd.DataFrame(rows)
    ev["q"] = E._bh(ev["p"])
    ev["status"] = [I.classify(r, gate) for r in ev.to_dict("records")]
    ev["key"] = ev["group"]
    ev["group_name"] = ev["group"].map(lambda g: "Tümü" if g == "all" else _name(g))
    ev["status_name"] = ev["status"].map(I.STATUS_NAMES)
    ev["icon"] = ev["status"].map(I.ICONS)
    ev["explain"] = [(f"{r['n']} olay; haberden sonra sektörün en güçlü büyüme sepeti, haber olmayan günlere göre 60 günde "
                      f"{r['mean_car_60'] * 100:+.1f}% (%{r['hit_60'] * 100:.0f} pozitif).") if r["n"] else "Veri yok." for r in ev.to_dict("records")]
    ev["asof"] = date.today().isoformat()
    return ev.reindex(columns=COLS)


def refresh(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    if not cfg().get("enabled", True):
        return pd.DataFrame()
    prices = store.read("prices") if prices is None else prices
    if prices is None or prices.empty:
        return store.read("basket_evidence")
    sectors = sector_map()
    ret = measure(event_frame(sectors), prices, sectors)
    if not ret.empty:
        store.write("basket_returns", ret)
    ev = build(ret)
    store.write("basket_evidence", ev)
    log.info("Büyüme sepeti: %d grup, %d kanıtlı, %d umut verici", len(ev),
             int((ev["status"] == "proven").sum()) if len(ev) else 0, int((ev["status"] == "promising").sum()) if len(ev) else 0)
    return ev
