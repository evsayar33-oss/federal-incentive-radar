"""Kanıt motoru: hangi haber türü hangi hisseleri GERÇEKTEN etkiliyor?

Bu modül bir haber türünün etkisini istatistiksel olarak kanıtlar veya reddeder. LLM yoktur.

Adımlar:
1. Hedef: her ilgili olay için etkilenen varlık belirlenir.
   - direct : olayda ticker var (SEC bildirimi, eşleşmiş ödül alıcısı).
   - basket : ticker yok ama program belli. Sepet = bu programda DAHA ÖNCE doğrudan olayı olan şirketler
              (yalnızca geçmiş bilgi kullanılır, ileriye bakma yanlılığı yoktur).
   - sector : ticker yok. Programın baskın sektör ETF'i.
2. Anormal getiri: piyasa modeli (SPY), tahmin penceresi [-250, -30] gün. CAR ve standartlaştırılmış
   CAR (SCAR = CAR / (σ·√L)) hesaplanır. SCAR, oynak küçük hisselerin sonucu ezmesini önler.
3. Kümeleme: aynı gün olan olaylar birbirinden bağımsız değildir. Test, gün kümeleri üzerinde yapılır.
4. Test: SCAR t-testi (BMP benzeri), işaret testi, Benjamini–Hochberg FDR (çoklu test düzeltmesi).
5. Hiyerarşik daralma (empirical Bayes): az örnekli tür, üst türün etkisine doğru çekilir.
6. İleriye dönük doğrulama (walk-forward): her küme için yalnızca ÖNCEKİ verilerle yön tahmini yapılır.
   Bir tür ancak geçmişte "önceden bilinebilir" bir etki gösterdiyse kanıtlanmış sayılır.
7. Canlı geri besleme: Telegram'a gönderilen sinyallerin gerçekleşen getirisi ölçülür. Canlıda
   başarısız olan tür askıya alınır. Böylece sistem kendi kararlarını sürekli denetler.
"""
from __future__ import annotations

import json
import logging
import math
import warnings
from datetime import date

import numpy as np
import pandas as pd

from . import store
from .settings import priors, programs, sector_etfs, sources

log = logging.getLogger("radar.evidence")

KIND_NAMES = {
    "sec_8k": "Şirketin 8-K önemli olay bildirimi",
    "sec_10q": "Şirketin çeyrek raporu (10-Q)",
    "sec_10k": "Şirketin yıllık raporu (10-K)",
    "sec_other": "Şirketin SEC bildirimi",
    "award_grant": "Federal hibe ödülü",
    "award_loan": "Federal kredi / kredi garantisi",
    "award_contract": "Federal sözleşme",
    "law": "Kanun / kararname",
    "bill_passed": "Tasarı bir meclisten geçti",
    "bill": "Yeni tasarı",
    "rule_final": "Kesinleşmiş kural / rehber",
    "rule_proposed": "Önerilen kural",
    "nofo": "Fon ilanı (başvuru çağrısı)",
    "notice": "Resmi duyuru",
    "news": "Haber",
    "other": "Diğer",
}
ROLE_NAMES = {"direct": "Şirketin kendi hissesi", "basket": "Programa maruz şirketler sepeti",
              "sector": "Sektör ETF'i"}
STATUS_NAMES = {"proven": "Kanıtlandı", "promising": "Umut verici", "no_effect": "Etkisiz (kanıtlı)",
                "unclear": "Belirsiz", "collecting": "Veri birikiyor", "suspended": "Askıda (canlıda başarısız)"}
STATUS_ICON = {"proven": "🟢", "promising": "🟡", "no_effect": "⚫", "unclear": "⚪", "collecting": "⚪",
               "suspended": "🟠"}
STATUS_RANK = {"proven": 5, "promising": 4, "suspended": 3, "no_effect": 2, "unclear": 1, "collecting": 0}
DEFINITE = {"proven", "promising", "no_effect", "suspended"}


def cfg() -> dict:
    return priors()["evidence"]


# ---------------------------------------------------------------- olay türü ve hedef

def _extra(row) -> dict:
    try:
        x = json.loads(row.get("extra") or "{}")
        return x if isinstance(x, dict) else {}
    except (TypeError, ValueError):
        return {}


def event_kind(row) -> str:
    src, stage, x = str(row.get("source", "")), int(row.get("stage", 0)), _extra(row)
    if src == "sec_edgar":
        form = str(x.get("form", "")).upper().replace("/A", "")
        return {"8-K": "sec_8k", "10-Q": "sec_10q", "10-K": "sec_10k"}.get(form, "sec_other")
    if src == "usaspending":
        t = str(row.get("amount_type", ""))
        k = x.get("kind") or ("contract" if t == "contract_obligation" else "loan" if t == "loan_subsidy_cost" else "grant")
        return f"award_{k}" if k in ("grant", "loan", "contract") else "award_grant"
    if src == "congress":
        s = str(x.get("status", ""))
        return "law" if s == "law" or stage == 2 else "bill_passed" if s == "passed_chamber" else "bill"
    if src == "federal_register":
        d = str(x.get("doc_type", ""))
        if d == "presidential" or stage == 2:
            return "law"
        return {"final_rule": "rule_final", "proposed_rule": "rule_proposed", "nofo_notice": "nofo"}.get(
            d, "nofo" if stage == 4 else "notice")
    if src == "grants_gov":
        return "nofo"
    if src == "gdelt":
        return "news"
    return "other"


def kind_label(program_name: str, kind: str) -> str:
    k = KIND_NAMES.get(kind, kind)
    return f"{program_name} · {k}" if program_name and program_name != "*" else k


def _dominant_sector(sw) -> str | None:
    try:
        d = json.loads(sw) if isinstance(sw, str) else (sw or {})
    except ValueError:
        return None
    return max(d, key=d.get) if d else None


def _tickers(v) -> list[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    return [t.strip() for t in str(v).split(",") if t.strip() and t.strip().lower() != "nan"]


def prepare(events: pd.DataFrame) -> pd.DataFrame:
    """İlgili olaylara tür (kind) ve doğrudan ticker listesini ekler."""
    if events is None or events.empty:
        return pd.DataFrame()
    e = events[events["relevant"].astype(str).str.lower().isin(["true", "1"])].copy()
    if e.empty:
        return e
    e["kind"] = [event_kind(r) for r in e.to_dict("records")]
    e["tick_list"] = e["tickers"].map(_tickers)
    e["d"] = pd.to_datetime(e["date"], errors="coerce")
    e["program"] = e["program"].fillna("other").astype(str)
    return e.dropna(subset=["d"])


class BasketBook:
    """Program → geçmişte doğrudan olayı olan şirketler. Yalnızca olay tarihinden önceki bilgi kullanılır."""

    def __init__(self, prepared: pd.DataFrame):
        x = prepared[prepared["tick_list"].map(len) > 0][["program", "d", "tick_list"]].explode("tick_list")
        self.x = x.rename(columns={"tick_list": "ticker"}).sort_values("d")

    def basket(self, program: str, before: pd.Timestamp, n: int, allowed: set[str] | None = None) -> list[str]:
        if program in ("", "other", "nan"):
            return []
        g = self.x[(self.x["program"] == program) & (self.x["d"] < before)]
        if allowed is not None:
            g = g[g["ticker"].isin(allowed)]
        return g["ticker"].value_counts().head(n).index.tolist()


def targets(prepared: pd.DataFrame, allowed: set[str] | None = None) -> pd.DataFrame:
    """Her olay için (rol, hedef) satırları üretir."""
    c, etfs = cfg(), sector_etfs()
    book = BasketBook(prepared)
    rows = []
    for r in prepared.to_dict("records"):
        base = {"event_id": r["event_id"], "date": r["d"], "program": r["program"], "kind": r["kind"]}
        if r["tick_list"]:
            for t in r["tick_list"]:
                rows.append({**base, "role": "direct", "target": t, "members": t})
            continue
        b = book.basket(r["program"], r["d"], c["basket_max"], allowed)
        if len(b) >= c["basket_min"]:
            rows.append({**base, "role": "basket", "target": f"BASKET:{r['program']}", "members": ",".join(b)})
        sec = _dominant_sector(r.get("sector_weights"))
        if sec and sec in etfs:
            rows.append({**base, "role": "sector", "target": etfs[sec], "members": etfs[sec]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- anormal getiri

def _returns(prices: pd.DataFrame) -> pd.DataFrame:
    p = prices.copy()
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    p["date"] = pd.to_datetime(p["date"], errors="coerce")
    px = p.dropna(subset=["close", "date"]).pivot_table(index="date", columns="ticker", values="close",
                                                         aggfunc="last").sort_index()
    return px.pct_change(fill_method=None).clip(-0.5, 0.5)


def _abnormal(y: np.ndarray, m: np.ndarray, k0: int, c: dict) -> dict | None:
    lo, hi = k0 + c["est_window"][0], k0 + c["est_window"][1]
    if lo < 1:
        return None
    ye, me = y[lo:hi], m[lo:hi]
    ok = np.isfinite(ye) & np.isfinite(me)
    if ok.sum() < c["min_est_obs"]:
        return None
    beta, alpha = np.polyfit(me[ok], ye[ok], 1)
    sigma = float(np.std(ye[ok] - (alpha + beta * me[ok]), ddof=2))
    if not np.isfinite(sigma) or sigma <= 0:
        return None
    out = {"beta": round(float(beta), 3), "sigma": sigma}
    for name, (a, b) in c["windows"].items():
        s, e = k0 + a, k0 + b
        out[f"car_{name}"] = out[f"scar_{name}"] = np.nan
        if s < 0 or e >= len(y):
            continue  # pencere henüz dolmadı → sonraki çalışmada hesaplanır
        ar = y[s:e + 1] - (alpha + beta * m[s:e + 1])
        if not np.isfinite(ar).all():
            continue
        L = e - s + 1
        out[f"car_{name}"] = float(ar.sum())
        out[f"scar_{name}"] = float(ar.sum() / (sigma * math.sqrt(L)))
    return out


def compute_returns(tg: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    c, bench = cfg(), sources()["benchmark"]
    if tg is None or tg.empty or prices is None or prices.empty:
        return pd.DataFrame()
    rets = _returns(prices)
    if bench not in rets.columns:
        return pd.DataFrame()
    idx, m = rets.index, rets[bench].to_numpy()
    cols = {t: rets[t].to_numpy() for t in rets.columns}
    basket_cache: dict[str, np.ndarray | None] = {}
    rows = []
    for r in tg.to_dict("records"):
        k0 = int(idx.searchsorted(r["date"]))
        if k0 >= len(idx):
            continue
        if r["role"] == "basket":
            if r["members"] not in basket_cache:
                mem = [cols[t] for t in r["members"].split(",") if t in cols]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    basket_cache[r["members"]] = (np.nanmean(np.vstack(mem), axis=0)
                                                  if len(mem) >= c["basket_min"] else None)
            y = basket_cache[r["members"]]
        else:
            y = cols.get(r["target"])
        if y is None:
            continue
        with np.errstate(all="ignore"):
            ab = _abnormal(y, m, k0, c)
        if ab is None:
            continue
        rows.append({**r, "date": r["date"].date().isoformat(), "t0": idx[k0].date().isoformat(), **ab})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- istatistik

LEVELS = [  # (ad, anahtar alanları, üst seviye)
    ("role", ("role",), None),
    ("kind", ("kind", "role"), "role"),
    ("program", ("program", "role"), "role"),
    ("program_kind", ("program", "kind", "role"), "kind"),
]


def make_key(program: str | None, kind: str | None, role: str) -> str:
    return f"{program or '*'}|{kind or '*'}|{role}"


def candidate_keys(program: str, kind: str, role: str) -> list[tuple[str, str]]:
    """En özelden en genele: olay için bakılacak kanıt anahtarları."""
    return [("program_kind", make_key(program, kind, role)), ("kind", make_key(None, kind, role)),
            ("program", make_key(program, None, role)), ("role", make_key(None, None, role))]


def _ttest_p(t: float, df: int) -> float:
    from scipy import stats
    return float(2 * stats.t.sf(abs(t), df))


def _binom_p(k: int, n: int) -> float:
    from scipy import stats
    return float(stats.binomtest(k, n, 0.5, alternative="greater").pvalue) if n else 1.0


def _clusters(g: pd.DataFrame, c: dict) -> pd.DataFrame:
    w = c["primary_window"]
    g = g.dropna(subset=[f"car_{w}", f"scar_{w}"]).drop_duplicates(["target", "t0"])
    g = g.assign(car=g[f"car_{w}"].clip(-c["clip_car"], c["clip_car"]),
                 scar=g[f"scar_{w}"].clip(-c["clip_scar"], c["clip_scar"]))
    cl = g.groupby("t0").agg(car=("car", "mean"), scar=("scar", "mean"), n=("car", "size")).reset_index()
    cl["d"] = pd.to_datetime(cl["t0"])
    return cl.sort_values("d")


def walk_forward(cl: pd.DataFrame, c: dict) -> dict:
    """Her küme için yalnızca geçmiş kümelerle yön tahmini. Örtüşmeyi önlemek için purge uygulanır."""
    hits, signed = [], []
    purge = pd.Timedelta(days=c["purge_days"])
    for i in range(len(cl)):
        prior = cl[cl["d"] <= cl["d"].iloc[i] - purge]
        if len(prior) < c["oos_min_train"]:
            continue
        sd = prior["scar"].std(ddof=1)
        t = prior["scar"].mean() / (sd / math.sqrt(len(prior))) if sd and sd > 0 else 0.0
        if abs(t) < c["oos_min_t"]:
            continue
        direction = 1.0 if prior["car"].mean() > 0 else -1.0
        real = float(cl["car"].iloc[i])
        hits.append(float(np.sign(real) == direction))
        signed.append(direction * real)
    n = len(hits)
    return {"oos_n": n, "oos_hit": round(float(np.mean(hits)), 3) if n else np.nan,
            "oos_signed": round(float(np.mean(signed)), 5) if n else np.nan,
            "oos_p": round(_binom_p(int(sum(hits)), n), 4) if n else np.nan}


def group_stats(g: pd.DataFrame, c: dict) -> dict:
    cl = _clusters(g, c)
    k = len(cl)
    out = {"n_obs": int(cl["n"].sum()) if k else 0, "n_clusters": k, "mean_car": np.nan, "median_car": np.nan,
           "se": np.nan, "t": np.nan, "p": np.nan, "consistency": np.nan, "first_date": "", "last_date": ""}
    if k == 0:
        return out
    out.update(first_date=cl["t0"].iloc[0], last_date=cl["t0"].iloc[-1],
               mean_car=float(cl["car"].mean()), median_car=float(cl["car"].median()))
    if k >= 3:
        sd_car = max(float(cl["car"].std(ddof=1)), c["sd_floor"])
        out["se"] = sd_car / math.sqrt(k)
        sd = float(cl["scar"].std(ddof=1))
        t = float(cl["scar"].mean() / (sd / math.sqrt(k))) if sd > 0 else 0.0
        out.update(t=round(t, 3), p=_ttest_p(t, k - 1))
        same = (np.sign(cl["car"]) == np.sign(out["mean_car"])).sum()
        out["consistency"] = round(float(same / k), 3)
    out.update(walk_forward(cl, c))
    return out


def _bh(p: pd.Series) -> pd.Series:
    """Benjamini–Hochberg q değerleri."""
    q = pd.Series(np.nan, index=p.index)
    v = p.dropna().sort_values()
    if v.empty:
        return q
    m = len(v)
    raw = v.to_numpy() * m / np.arange(1, m + 1)
    q[v.index] = np.minimum.accumulate(raw[::-1])[::-1].clip(max=1.0)
    return q


def _int(v) -> int:
    try:
        return int(v) if v is not None and np.isfinite(float(v)) else 0
    except (TypeError, ValueError):
        return 0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def shrink(ev: pd.DataFrame, c: dict) -> pd.DataFrame:
    """Hiyerarşik empirical Bayes: her seviye üst seviyenin posterior ortalamasına doğru daraltılır."""
    ev = ev.copy()
    ev["post_mean"], ev["post_sd"], ev["prob_dir"] = np.nan, np.nan, np.nan
    post: dict[str, float] = {}
    for level, fields, parent in LEVELS:
        lv = ev[(ev["level"] == level) & ev["se"].notna()]
        if lv.empty:
            continue
        # Önsel: üst seviyenin posterior'u. Önsel varyans = τ² (gruplar arası) + üst seviyenin belirsizliği.
        parents = [None if parent is None else make_key(None, r["kind"] if parent == "kind" else None, r["role"])
                   for r in lv.to_dict("records")]
        prior_mu = np.array([post.get(k, (0.0, 0.0))[0] if k else 0.0 for k in parents])
        prior_v = np.array([post.get(k, (0.0, 0.0))[1] ** 2 if k else 0.0 for k in parents])
        se2 = lv["se"].to_numpy() ** 2
        dev2 = (lv["mean_car"].to_numpy() - prior_mu) ** 2 - se2 - prior_v
        tau2 = max(c["tau_floor"] ** 2, float(np.mean(dev2)) if len(dev2) else 0.0)
        pv = tau2 + prior_v
        pm = (lv["mean_car"].to_numpy() / se2 + prior_mu / pv) / (1 / se2 + 1 / pv)
        ps = np.sqrt(1 / (1 / se2 + 1 / pv))
        ev.loc[lv.index, "post_mean"] = pm
        ev.loc[lv.index, "post_sd"] = ps
        ev.loc[lv.index, "prob_dir"] = [max(_norm_cdf(a / b), 1 - _norm_cdf(a / b)) for a, b in zip(pm, ps)]
        ev.loc[lv.index, "tau"] = math.sqrt(tau2)
        post.update({k: (m, sd) for k, m, sd in zip(lv["key"], pm, ps)})
    return ev


def classify_status(r: dict, c: dict) -> str:
    k = r["n_clusters"]
    if k < c["min_clusters_promising"] or not np.isfinite(r.get("p", np.nan)):
        return "collecting"
    eff = abs(r["post_mean"]) if np.isfinite(r["post_mean"]) else 0.0
    oos_ok = r["oos_n"] >= c["oos_min_n"] and r["oos_hit"] >= c["oos_hit_proven"] and r["oos_signed"] > 0
    oos_bad = r["oos_n"] >= c["oos_min_n"] and r["oos_signed"] < 0
    if (k >= c["min_clusters_proven"] and r["q"] <= c["q_proven"] and eff >= c["min_effect"]
            and r["prob_dir"] >= c["post_prob_proven"] and oos_ok):
        return "proven"
    if (r["p"] <= c["p_promising"] and r["q"] <= c["q_promising"] and r["prob_dir"] >= c["post_prob_promising"]
            and eff >= c["min_effect"] / 2 and not oos_bad):
        return "promising"
    if k >= c["null_min_clusters"] and r["p"] >= c["null_p"]:
        return "no_effect"
    return "unclear"


def build_evidence(er: pd.DataFrame, live: pd.DataFrame | None = None) -> pd.DataFrame:
    c = cfg()
    if er is None or er.empty:
        return pd.DataFrame()
    rows = []
    for level, fields, _parent in LEVELS:
        for vals, g in er.groupby(list(fields)):
            vals = vals if isinstance(vals, tuple) else (vals,)
            d = dict(zip(fields, vals))
            key = make_key(d.get("program"), d.get("kind"), d["role"])
            rows.append({"key": key, "level": level, "program": d.get("program", "*"), "kind": d.get("kind", "*"),
                         "role": d["role"], **group_stats(g, c)})
    ev = pd.DataFrame(rows)
    ev["q"] = _bh(ev["p"])
    ev = shrink(ev, c)
    ev["status"] = [classify_status(r, c) for r in ev.to_dict("records")]
    ev["direction"] = np.where(ev["post_mean"].fillna(ev["mean_car"]) >= 0, "+", "-")
    ev = apply_live(ev, live, c)
    pn = {k: v["name"] for k, v in programs()["programs"].items()}
    ev["program_name"] = ev["program"].map(lambda p: "Tüm programlar" if p == "*" else "Diğer" if p == "other"
                                           else pn.get(p, p))
    ev["kind_name"] = ev["kind"].map(lambda k: "Tüm haber türleri" if k == "*" else KIND_NAMES.get(k, k))
    ev["role_name"] = ev["role"].map(ROLE_NAMES)
    ev["status_name"], ev["icon"] = ev["status"].map(STATUS_NAMES), ev["status"].map(STATUS_ICON)
    ev["explain"] = [plain_text(r) for r in ev.to_dict("records")]
    ev["window"] = c["primary_window"]
    ev["asof"] = date.today().isoformat()
    return ev.sort_values(["status", "n_clusters"], key=lambda s: s.map(STATUS_RANK) if s.name == "status" else s,
                          ascending=False).reset_index(drop=True)


def apply_live(ev: pd.DataFrame, live: pd.DataFrame | None, c: dict) -> pd.DataFrame:
    """Canlı geri besleme: Telegram'a giden sinyallerin gerçekleşen isabeti kötüyse türü askıya al."""
    ev = ev.copy()
    ev["live_n"], ev["live_hit"] = 0, np.nan
    if live is None or live.empty or "hit" not in live.columns:
        return ev
    lv = live.dropna(subset=["hit"])
    for key, g in lv.groupby("evidence_key"):
        g = g.tail(c["live_window"])
        m = ev["key"] == key
        if not m.any():
            continue
        hit = float(pd.to_numeric(g["hit"], errors="coerce").mean())
        ev.loc[m, "live_n"], ev.loc[m, "live_hit"] = len(g), round(hit, 3)
        if len(g) >= c["live_min_n"] and hit < c["live_hit_floor"] and ev.loc[m, "status"].iloc[0] in ("proven", "promising"):
            ev.loc[m, "status"] = "suspended"
    return ev


# ---------------------------------------------------------------- olay → kanıt eşleme

def lookup(index: dict, program: str, kind: str, role: str) -> dict | None:
    """Özelden genele. Özel anahtarın yeterli verisi varsa (veri birikiyor değilse) onun hükmü geçerlidir.
    Genel seviyeye yalnızca özel anahtarda veri yetersizse inilir. Böylece bir programın kanıtı
    (ör. CHIPS 8-K) başka bir programın aynı tür haberine yanlışlıkla taşınmaz."""
    fallback = None
    for _level, key in candidate_keys(program, kind, role):
        r = index.get(key)
        if r is None:
            continue
        if r["status"] != "collecting":
            return r
        fallback = fallback or r
    return fallback


def plain_text(r: dict) -> str:
    """Yeni başlayan için tek cümlelik açıklama."""
    st = r.get("status", "collecting")
    n, eff = _int(r.get("n_clusters")), r.get("post_mean")
    w = cfg()["windows"][cfg()["primary_window"]][1]
    if st in ("proven", "promising", "suspended") and eff is not None and np.isfinite(eff):
        yon = "yükseldi" if eff > 0 else "düştü"
        s = (f"Geçmişte bu tür haberden sonra hisseler {w} işlem gününde piyasaya göre ortalama "
             f"%{abs(eff) * 100:.1f}".replace(".", ",") + f" {yon} ({n} örnek")
        if np.isfinite(r.get("consistency", np.nan)):
            s += f", %{r['consistency'] * 100:.0f}'i aynı yönde"
        s += ")."
        if st == "suspended":
            s += " Ancak son canlı sinyaller tutmadı; tür askıya alındı."
        return s
    if st == "no_effect":
        return f"Bu tür haber geçmişte ({n} örnek) hisseleri belirgin biçimde etkilemedi. Önemsemeyebilirsiniz."
    if st == "unclear":
        return f"{n} örnek var ama etki net değil."
    return f"Henüz yeterli örnek yok ({n}). Sistem veri topluyor."


def annotate(events: pd.DataFrame, ev: pd.DataFrame, days: int | None = None) -> pd.DataFrame:
    """Son olaylara kanıt rozetini ekler (arayüz ve Telegram aynı tabloyu kullanır)."""
    c = cfg()
    days = days or c["signal_days"]
    p = prepare(events)
    if p.empty:
        return pd.DataFrame()
    recent = p[p["d"] >= p["d"].max() - pd.Timedelta(days=days)]
    if recent.empty:
        return pd.DataFrame()
    # Sepet için TÜM geçmiş olaylar kullanılır; hedefler yalnızca son olaylar için üretilir.
    book = BasketBook(p)
    etfs = sector_etfs()
    index = {r["key"]: r for r in ev.to_dict("records")} if ev is not None and not ev.empty else {}
    rows = []
    for r in recent.to_dict("records"):
        roles = []
        if r["tick_list"]:
            roles.append(("direct", ",".join(r["tick_list"])))
        else:
            b = book.basket(r["program"], r["d"] + pd.Timedelta(days=1), c["basket_show"])
            if len(b) >= c["basket_min"]:
                roles.append(("basket", ",".join(b)))
            sec = _dominant_sector(r.get("sector_weights"))
            if sec in etfs:
                roles.append(("sector", etfs[sec]))
        best, best_role, best_tick = None, "", ""
        for role, tick in roles:
            m = lookup(index, r["program"], r["kind"], role)
            if m is not None and (best is None or STATUS_RANK[m["status"]] > STATUS_RANK[best["status"]]):
                best, best_role, best_tick = m, role, tick
        if best is None and roles:
            best_role, best_tick = roles[0]
        b = best or {"status": "collecting", "n_clusters": 0}
        rows.append({
            "event_id": r["event_id"], "date": r["d"].date().isoformat(), "title": r["title"], "url": r.get("url", ""),
            "source": r["source"], "program": r["program"], "program_name": r.get("program_name", ""),
            "kind": r["kind"], "kind_name": KIND_NAMES.get(r["kind"], r["kind"]), "amount_usd": r.get("amount_usd"),
            "role": best_role, "role_name": ROLE_NAMES.get(best_role, ""), "affected": best_tick,
            "evidence_key": b.get("key", ""), "evidence_level": b.get("level", ""), "status": b["status"],
            "status_name": STATUS_NAMES[b["status"]], "icon": STATUS_ICON[b["status"]],
            "direction": b.get("direction", ""), "effect": b.get("post_mean", np.nan),
            "n_clusters": _int(b.get("n_clusters")), "consistency": b.get("consistency", np.nan),
            "oos_hit": b.get("oos_hit", np.nan), "oos_n": _int(b.get("oos_n")), "q": b.get("q", np.nan),
            "explain": plain_text(b), "rank": STATUS_RANK[b["status"]],
        })
    return pd.DataFrame(rows).sort_values(["rank", "date"], ascending=[False, False]).reset_index(drop=True)


# ---------------------------------------------------------------- yenileme (haftalık ve kapanış)

def refresh(events: pd.DataFrame | None = None, prices: pd.DataFrame | None = None) -> pd.DataFrame:
    events = store.read("events") if events is None else events
    prices = store.read("prices") if prices is None else prices
    p = prepare(events)
    if p.empty or prices is None or prices.empty:
        return store.read("evidence")
    allowed = set(prices["ticker"].astype(str).unique())
    er = compute_returns(targets(p, allowed), prices)
    if er.empty:
        return store.read("evidence")
    store.write("event_returns", er)
    live = resolve_notifications(er)
    ev = build_evidence(er, live)
    store.write("evidence", ev)
    hist = ev[ev["level"].isin(["program_kind", "kind"])][
        ["asof", "key", "level", "status", "n_clusters", "post_mean", "q", "oos_hit"]]
    store.upsert("evidence_history", hist, ["asof", "key"])
    log.info("Kanıt: %d anahtar, %d kanıtlı, %d umut verici", len(ev), int((ev["status"] == "proven").sum()),
             int((ev["status"] == "promising").sum()))
    return ev


def resolve_notifications(er: pd.DataFrame) -> pd.DataFrame:
    """Gönderilmiş Telegram sinyallerinin gerçekleşen anormal getirisini yazar (botun karnesi)."""
    nt = store.read("notifications")
    if nt.empty or er is None or er.empty:
        return nt
    w = cfg()["primary_window"]
    r = er.dropna(subset=[f"car_{w}"]).drop_duplicates(["event_id", "role"]).set_index(["event_id", "role"])
    for c in ("realized_car", "hit"):
        if c not in nt.columns:
            nt[c] = np.nan
        nt[c] = pd.to_numeric(nt[c], errors="coerce")
    for i in nt.index[nt["hit"].isna()]:
        k = (str(nt.at[i, "event_id"]), str(nt.at[i, "role"]))
        if k in r.index:
            car = float(r.loc[k, f"car_{w}"])
            nt.at[i, "realized_car"] = round(car, 5)
            nt.at[i, "hit"] = float(np.sign(car) == (1 if nt.at[i, "direction"] == "+" else -1))
    store.write("notifications", nt)
    return nt
