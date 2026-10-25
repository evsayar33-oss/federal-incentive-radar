"""Arayüz için veri katmanı (Streamlit'ten bağımsız, test edilebilir).

Veri kaynağı sırası:
1. DATA_BASE_URL (ör. https://raw.githubusercontent.com/<kullanıcı>/<repo>/data)
2. Yerel klasör (RADAR_STORE veya ./store)
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pandas as pd
import requests

STAGE_NAMES = {0: "Sinyal (haber)", 1: "Tasarı", 2: "Kanun / Kararname", 3: "Kural / Rehber",
               4: "İlan (NOFO)", 5: "Yükümlülük (ödül)", 6: "Ödeme (outlay)", 7: "Şirket bildirimi"}
HORIZON_NAMES = {63: "3 ay", 252: "1 yıl", 756: "3 yıl"}
ROOT = Path(__file__).resolve().parents[1]


def local_dir() -> Path:
    return Path(os.environ.get("RADAR_STORE", ROOT / "store"))


def _read_bytes(base: str | None, fname: str) -> bytes | None:
    if base:
        try:
            r = requests.get(f"{base.rstrip('/')}/{fname}", timeout=30)
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            return None
        return None
    p = local_dir() / fname
    return p.read_bytes() if p.exists() else None


def load_table(name: str, base: str | None = None) -> pd.DataFrame:
    for fname in (f"{name}.parquet", f"{name}.csv.gz"):
        b = _read_bytes(base, fname)
        if b:
            try:
                if fname.endswith(".parquet"):
                    return pd.read_parquet(io.BytesIO(b))
                return pd.read_csv(io.BytesIO(b), compression="gzip", low_memory=False)
            except Exception:
                continue
    return pd.DataFrame()


def load_json(name: str, base: str | None = None) -> dict:
    b = _read_bytes(base, f"{name}.json")
    if not b:
        return {}
    try:
        return json.loads(b.decode("utf-8"))
    except ValueError:
        return {}


# ---------------------------------------------------------------- dönüşümler

def as_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1"])


def relevant_events(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty:
        return ev
    e = ev[as_bool(ev["relevant"])].copy()
    e["stage"] = e["stage"].astype(int)
    e["stage_name"] = e["stage"].map(STAGE_NAMES)
    e["amount_usd"] = pd.to_numeric(e["amount_usd"], errors="coerce")
    e["tickers"] = e["tickers"].fillna("").astype(str).replace("nan", "")
    return e.sort_values("date", ascending=False)


def pipeline_funnel(ev: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    if ev.empty:
        return pd.DataFrame(columns=["stage", "stage_name", "n", "amount_usd"])
    d = pd.to_datetime(ev["date"], errors="coerce")
    e = ev[d >= d.max() - pd.Timedelta(days=days)]
    g = e.groupby("stage").agg(n=("event_id", "count"), amount_usd=("amount_usd", "sum")).reset_index()
    g["stage_name"] = g["stage"].map(STAGE_NAMES)
    return g.sort_values("stage")


def program_pipeline(ev: pd.DataFrame, days: int = 365) -> pd.DataFrame:
    """Program × aşama matrisi: her programın boru hattında nerede olduğu."""
    if ev.empty:
        return pd.DataFrame()
    d = pd.to_datetime(ev["date"], errors="coerce")
    e = ev[d >= d.max() - pd.Timedelta(days=days)]
    return e.pivot_table(index="program_name", columns="stage_name", values="event_id", aggfunc="count",
                         fill_value=0)[[s for s in STAGE_NAMES.values() if s in set(e["stage_name"])]]


def score_matrix(scores: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return pd.DataFrame()
    s = scores.copy()
    s["Ufuk"] = s["horizon_days"].astype(int).map(HORIZON_NAMES)
    m = s.pivot_table(index="sector", columns="Ufuk", values="score", aggfunc="last")
    return m[[h for h in HORIZON_NAMES.values() if h in m.columns]].sort_values(m.columns[-1] if len(m.columns) else None, ascending=False)


def relative_performance(prices: pd.DataFrame, etf: str, bench: str = "SPY", days: int = 252) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame()
    q = prices[prices["ticker"].isin([etf, bench])].copy()
    q["close"] = pd.to_numeric(q["close"], errors="coerce")
    q["date"] = pd.to_datetime(q["date"], errors="coerce")
    q = q.dropna(subset=["close", "date"])
    if q.empty:
        return pd.DataFrame()
    p = q.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index().tail(days)
    if etf not in p.columns or bench not in p.columns or p.empty:
        return pd.DataFrame()
    rel = (p[etf] / p[etf].iloc[0]) / (p[bench] / p[bench].iloc[0]) - 1
    return rel.rename("SPY'a göre").to_frame()


def last_run_health(runs: pd.DataFrame) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame()
    r = runs[runs["slot"] != "weekly"].tail(1)
    if r.empty:
        return pd.DataFrame()
    try:
        st = json.loads(r.iloc[0]["status"])
    except (TypeError, ValueError):
        return pd.DataFrame()
    rows = [{"kaynak": k, "durum": "✅" if v.get("ok") else "❌", "satır": v.get("rows", ""),
             "süre_sn": v.get("sec", ""), "hata": v.get("error", "")}
            for k, v in st.items() if isinstance(v, dict)]
    return pd.DataFrame(rows)


def labeling_queue(ev: pd.DataFrame, n: int = 25) -> pd.DataFrame:
    """Aktif öğrenme: modelin en kararsız olduğu olaylar önce gelir."""
    if ev.empty or "relevance_prob" not in ev.columns:
        return pd.DataFrame()
    e = ev[ev["method"].astype(str) != "label"].copy()
    e = e[e["source"].isin(["federal_register", "congress", "gdelt"])]
    e["uncertainty"] = 1 - (pd.to_numeric(e["relevance_prob"], errors="coerce") - 0.5).abs() * 2
    return e.sort_values(["uncertainty", "date"], ascending=[False, False]).head(n)


def fmt_usd(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v != v:
        return "-"
    for unit, div in (("T$", 1e12), ("Mr$", 1e9), ("M$", 1e6), ("B$", 1e3)):
        if abs(v) >= div:
            return f"{v/div:,.1f} {unit}"
    return f"{v:,.0f} $"


# ---------------------------------------------------------------- basit görünüm (kanıt motoru tabloları)

STATUS_ORDER = {"proven": 5, "promising": 4, "suspended": 3, "no_effect": 2, "unclear": 1, "collecting": 0}


def pct(v, signed: bool = True) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v != v:
        return "-"
    s = f"{abs(v) * 100:.1f}".replace(".", ",")
    return (("+" if v > 0 else "−") if signed else "") + "%" + s


def evidence_table(ev: pd.DataFrame, statuses: list[str] | None = None) -> pd.DataFrame:
    """Kanıt tablosunu yeni başlayan için sadeleştirir (yalnızca 'program × tür' ve 'tür' seviyeleri)."""
    if ev.empty:
        return pd.DataFrame()
    e = ev[ev["level"].isin(["program_kind", "kind"])].copy()
    if statuses:
        e = e[e["status"].isin(statuses)]
    if e.empty:
        return pd.DataFrame()
    e["_r"] = e["status"].map(STATUS_ORDER)
    e = e.sort_values(["_r", "n_clusters"], ascending=False)
    out = pd.DataFrame({
        "Durum": e["icon"] + " " + e["status_name"],
        "Program": e["program_name"],
        "Haber türü": e["kind_name"],
        "Etkilenen": e["role_name"],
        "Ortalama etki (5 gün)": e["post_mean"].map(pct),
        "Olay sayısı": e["n_clusters"].astype(int),
        "Aynı yön": e["consistency"].map(lambda v: pct(v, signed=False)),
        "İleriye dönük isabet": [f"{pct(h, signed=False)} ({int(n)})" if n and n == n else "-"
                                 for h, n in zip(e["oos_hit"], e["oos_n"])],
        "Açıklama": e["explain"],
    })
    return out.reset_index(drop=True)


def signal_feed(sig: pd.DataFrame, days: int = 14, only_strong: bool = True) -> pd.DataFrame:
    if sig.empty:
        return sig
    d = pd.to_datetime(sig["date"], errors="coerce")
    s = sig[d >= d.max() - pd.Timedelta(days=days)]
    if only_strong:
        s = s[s["status"].isin(["proven", "promising"])]
    return s.sort_values(["rank", "date"], ascending=[False, False])


def bot_scorecard(nt: pd.DataFrame) -> dict:
    if nt.empty:
        return {"sent": 0, "resolved": 0, "hit": None, "avg_signed": None}
    hit = pd.to_numeric(nt.get("hit"), errors="coerce")
    car = pd.to_numeric(nt.get("realized_car"), errors="coerce")
    sign = nt["direction"].map({"+": 1, "-": -1}).fillna(0)
    res = hit.notna()
    return {"sent": int(len(nt)), "resolved": int(res.sum()),
            "hit": float(hit[res].mean()) if res.any() else None,
            "avg_signed": float((car * sign)[res].mean()) if res.any() else None}


def ticker_history(er: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Bir hissenin geçmiş teşvik olayları ve her olaydan sonraki piyasa üstü getirisi."""
    if er.empty or not ticker:
        return pd.DataFrame()
    t = ticker.strip().upper()
    m = (er["target"].astype(str) == t) | er["members"].astype(str).str.split(",").map(lambda x: t in x)
    x = er[m].copy()
    if x.empty:
        return x
    return x.sort_values("date", ascending=False)
