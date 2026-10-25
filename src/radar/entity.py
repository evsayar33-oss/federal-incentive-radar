"""Varlık eşleştirme: ödül alıcısı adı → borsa ticker'ı.

Sıra: kullanıcı onayı (override) → tam normalize ad → bulanık eşleşme.
Belirsiz eşleşmeler (skor 85-92) etiketleme kuyruğuna gider. Onaylanan her eşleşme
kalıcı olur ve sonraki çalışmalarda doğrudan kullanılır (sistem kullanıldıkça gelişir).
"""
from __future__ import annotations

import re

import pandas as pd

try:
    from rapidfuzz import fuzz, process
    _HAVE_RF = True
except ImportError:  # yedek: standart kütüphane
    import difflib
    _HAVE_RF = False

SUFFIX = re.compile(
    r"\b(incorporated|inc|corp|corporation|co|company|llc|l\.l\.c|ltd|limited|plc|lp|l\.p|holdings?|group|"
    r"the|n\.?a|sa|ag|nv|se|usa|us|america|americas|north america|international|intl)\b\.?", re.I)
ACCEPT, REVIEW = 92, 85


def normalize(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"\bu\.\s?s\.(\s?a\.)?", " us ", n)   # "U.S." / "U.S.A." → us (sonra atılır)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


class Matcher:
    def __init__(self, universe: pd.DataFrame, overrides: pd.DataFrame | None = None):
        u = universe.dropna(subset=["ticker", "name"]).copy()
        u["norm"] = u["name"].map(normalize)
        u = u[u["norm"].str.len() >= 3]
        # Endeks üyesi şirketleri öne al (aynı normalize adda).
        u["_rank"] = u.get("sp500_weight", pd.Series(0, index=u.index)).fillna(0)
        u = u.sort_values("_rank", ascending=False).drop_duplicates("norm")
        self.by_norm = dict(zip(u["norm"], u["ticker"]))
        self.choices = list(self.by_norm.keys())
        self.overrides = {}
        if overrides is not None and not overrides.empty:
            o = overrides.drop_duplicates("recipient_norm", keep="last")
            self.overrides = dict(zip(o["recipient_norm"], o["ticker"].fillna("")))

    def match(self, recipient: str) -> tuple[str, float, str]:
        n = normalize(recipient)
        if not n:
            return "", 0.0, "none"
        if n in self.overrides:
            t = self.overrides[n]
            return (t, 100.0, "override") if t and t != "NONE" else ("", 100.0, "override_none")
        if n in self.by_norm:
            return self.by_norm[n], 100.0, "exact"
        if not self.choices:
            return "", 0.0, "none"
        if _HAVE_RF:
            best = process.extractOne(n, self.choices, scorer=fuzz.token_sort_ratio)
            cand, score = (best[0], float(best[1])) if best else ("", 0.0)
        else:
            m = difflib.get_close_matches(n, self.choices, n=1, cutoff=0.8)
            cand = m[0] if m else ""
            score = difflib.SequenceMatcher(None, n, cand).ratio() * 100 if cand else 0.0
        if score >= ACCEPT:
            return self.by_norm[cand], score, "fuzzy"
        if score >= REVIEW:
            return self.by_norm[cand], score, "review"
        # Alt küme eşleşmesi (ör. "constellation energy generation" ⊃ "constellation energy"):
        # otomatik kabul edilmez, insan onayına gönderilir.
        toks = set(n.split())
        best_sub, best_len = "", 0
        for c in self.choices:
            ct = c.split()
            if len(ct) >= 2 and set(ct) <= toks and len(ct) > best_len:
                best_sub, best_len = c, len(ct)
        if best_sub:
            return self.by_norm[best_sub], 80.0, "review"
        return "", score, "none"

    def apply(self, events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Ticker'ı olmayan ve alıcısı olan olayları eşleştirir. (olaylar, inceleme kuyruğu) döner."""
        ev = events.copy()
        ev["tickers"] = ev["tickers"].fillna("").astype(str)
        ev["match_score"] = ev.get("match_score", pd.Series(pd.NA, index=ev.index))
        ev["match_method"] = ev.get("match_method", pd.Series("", index=ev.index)).fillna("")
        review = []
        mask = (ev["tickers"] == "") & (ev["recipient"].fillna("") != "") & (ev["source"] != "sec_edgar")
        cache: dict[str, tuple[str, float, str]] = {}
        for idx in ev.index[mask]:
            rec = ev.at[idx, "recipient"]
            if rec not in cache:
                cache[rec] = self.match(rec)
            t, s, m = cache[rec]
            ev.at[idx, "match_score"] = s
            ev.at[idx, "match_method"] = m
            if m in ("exact", "fuzzy", "override"):
                ev.at[idx, "tickers"] = t
            elif m == "review":
                review.append({"recipient": rec, "recipient_norm": normalize(rec), "candidate": t,
                               "score": round(s, 1), "event_id": ev.at[idx, "event_id"],
                               "amount_usd": ev.at[idx, "amount_usd"]})
        rq = pd.DataFrame(review).drop_duplicates("recipient_norm") if review else pd.DataFrame(
            columns=["recipient", "recipient_norm", "candidate", "score", "event_id", "amount_usd"])
        return ev, rq
