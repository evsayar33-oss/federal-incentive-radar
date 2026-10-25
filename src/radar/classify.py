"""LLM'siz sınıflandırma: anahtar kelime kuralları + kendi kendini eğiten TF-IDF modeli.

Akış:
1. Kural motoru (config/programs.yaml) her metne program puanı verir.
2. Öğrenilmiş model varsa (store/models/classifier.joblib) olasılıkları kuralla harmanlanır.
   Modelin ağırlığı, kullanıcı etiketi sayısı arttıkça büyür.
3. Kullanıcı etiketi her zaman kazanır (etiket = gerçek).
"""
from __future__ import annotations

import json
import logging
import math
import re

import numpy as np
import pandas as pd

from .settings import programs, store_dir

log = logging.getLogger("radar.classify")
IRRELEVANT = "irrelevant"
ALWAYS_RELEVANT_SOURCES = {"usaspending", "sec_edgar", "grants_gov"}
FALLBACK_SECTORS = {"Industrials": 0.4, "Information Technology": 0.2, "Utilities": 0.2, "Materials": 0.2}


def _pat(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


class Classifier:
    def __init__(self, load_model: bool = True):
        cfg = programs()
        self.programs = cfg["programs"]
        self.agency_sectors = cfg.get("agency_sectors", {})
        self.generic = [_pat(t) for t in cfg.get("generic_terms", [])]
        self.rules = {
            pid: [(_pat(k), 3.0) for k in p.get("strong", [])] + [(_pat(k), 1.0) for k in p.get("keywords", [])]
            for pid, p in self.programs.items()
        }
        try:
            from .settings import sources
            self.sec_phrase_program = dict(sources()["sec_edgar"]["phrases"])
        except Exception:
            self.sec_phrase_program = {}
        self.model = None
        self.model_weight = 0.0
        if load_model:
            self._load_model()

    # ---- öğrenilmiş model ----
    def _load_model(self):
        path = store_dir() / "models" / "classifier.joblib"
        if not path.exists():
            return
        try:
            import joblib
            bundle = joblib.load(path)
            self.model = bundle["pipeline"]
            n = bundle.get("n_user_labels", 0)
            # Az etiketle model kurala baskın gelmesin: 0 etiket → 0.10, 400+ etiket → 0.60.
            self.model_weight = float(min(0.60, 0.10 + n / 800.0))
            log.info("Öğrenilmiş sınıflandırıcı yüklendi (ağırlık %.2f, etiket %d)", self.model_weight, n)
        except Exception as exc:
            log.warning("Model yüklenemedi: %s", exc)

    # ---- kural motoru ----
    def rule_hits(self, text: str) -> tuple[dict[str, float], int]:
        t = text.lower()
        hits = {pid: sum(w for p, w in pats if p.search(t)) for pid, pats in self.rules.items()}
        generic = sum(1 for p in self.generic if p.search(t))
        return hits, generic

    def agency_fallback(self, agency: str) -> dict[str, float] | None:
        a = (agency or "").lower()
        for key, sec in self.agency_sectors.items():
            if key in a:
                return dict(sec)
        return None

    @staticmethod
    def default_incentive_type(row) -> str:
        if row.get("source") == "usaspending":
            at = str(row.get("amount_type", ""))
            return "loan" if "loan" in at else ("contract" if "contract" in at else "grant")
        if row.get("source") == "grants_gov":
            return "grant"
        return "mixed"

    def classify(self, df: pd.DataFrame, labels: pd.DataFrame | None = None) -> pd.DataFrame:
        if df.empty:
            return df.assign(program=[], program_name=[], incentive_type=[], sector_weights=[],
                             confidence=[], relevance_prob=[], relevant=[], method=[])
        texts = (df["title"].fillna("") + ". " + df["text"].fillna("") + " " + df["agency"].fillna("")).tolist()
        ml_proba, ml_classes = None, []
        if self.model is not None:
            try:
                ml_proba = self.model.predict_proba(texts)
                ml_classes = list(self.model.classes_)
            except Exception as exc:
                log.warning("Model tahmini başarısız: %s", exc)
        label_map = {}
        if labels is not None and not labels.empty and "label" in labels.columns:
            lab = labels[labels["kind"] == "program"] if "kind" in labels.columns else labels
            label_map = dict(zip(lab["event_id"].astype(str), lab["label"].astype(str)))

        out = []
        for i, (text, row) in enumerate(zip(texts, df.to_dict("records"))):
            hits, generic = self.rule_hits(text)
            max_hits = max(hits.values()) if hits else 0.0
            total = sum(hits.values())
            rule_dist = {k: v / total for k, v in hits.items() if v > 0} if total > 0 else {}
            p_irrel_rule = math.exp(-0.7 * max_hits - 0.3 * generic)
            method = "rule"
            dist = {k: (1 - p_irrel_rule) * v for k, v in rule_dist.items()}
            dist[IRRELEVANT] = p_irrel_rule
            if ml_proba is not None:
                w = self.model_weight
                ml = dict(zip(ml_classes, ml_proba[i]))
                keys = set(dist) | set(ml)
                dist = {k: (1 - w) * dist.get(k, 0.0) + w * ml.get(k, 0.0) for k in keys}
                method = "rule+ml"
            if row["source"] in ALWAYS_RELEVANT_SOURCES:
                dist[IRRELEVANT] = min(dist.get(IRRELEVANT, 0.0), 0.05)
            if row["source"] == "sec_edgar":  # sorgu ifadesi programı zaten belirler
                try:
                    phrase = json.loads(row.get("extra") or "{}").get("phrase")
                except (TypeError, ValueError):
                    phrase = None
                mapped = self.sec_phrase_program.get(phrase)
                dist = {mapped: 0.95, IRRELEVANT: 0.05} if mapped else {"other": 0.95, IRRELEVANT: 0.05}
                method = "phrase"
            eid = str(row["event_id"])
            if eid in label_map:
                lbl = label_map[eid]
                dist = {lbl: 1.0}
                method = "label"
            best = max(dist, key=dist.get)
            relevant = best != IRRELEVANT or (row["source"] in ALWAYS_RELEVANT_SOURCES and method != "label")
            prog = best
            if prog == IRRELEVANT:
                prog = max((k for k in dist if k != IRRELEVANT), key=dist.get, default="other")
            if prog in self.programs:
                p = self.programs[prog]
                sectors, itype, pname = dict(p["sectors"]), p.get("incentive_type", "mixed"), p["name"]
            else:
                prog = "other"
                sectors = self.agency_fallback(row.get("agency", "")) or dict(FALLBACK_SECTORS)
                itype, pname = "mixed", "Diğer / ajans bazlı"
            if row["source"] == "usaspending":
                itype = self.default_incentive_type(row)
            p_rel = float(1.0 - dist.get(IRRELEVANT, 0.0)) if method != "label" else (0.0 if best == IRRELEVANT else 1.0)
            non_irr = {k: v for k, v in dist.items() if k != IRRELEVANT}
            prog_conf = (non_irr.get(prog, 0.0) / sum(non_irr.values())) if non_irr and sum(non_irr.values()) > 0 else 0.0
            conf = float(1.0 if method == "label" else (p_rel * prog_conf if relevant else 1.0 - p_rel))
            out.append({"program": prog, "program_name": pname, "incentive_type": itype,
                        "sector_weights": json.dumps(sectors), "confidence": round(conf, 3),
                        "relevance_prob": round(p_rel, 3), "relevant": bool(relevant), "method": method})
        return pd.concat([df.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def train(events: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """Haftalık eğitim. Kullanıcı etiketleri + yüksek güvenli kural çıktıları (self-training)."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    import joblib

    ev = events.copy()
    ev["event_id"] = ev["event_id"].astype(str)
    ev["_text"] = ev["title"].fillna("") + ". " + ev["text"].fillna("") + " " + ev["agency"].fillna("")
    rows, weights = [], []
    n_user = 0
    if labels is not None and not labels.empty:
        lab = labels[labels.get("kind", "program") == "program"] if "kind" in labels.columns else labels
        lab = lab.drop_duplicates("event_id", keep="last")
        by = lab["by"].astype(str) if "by" in lab.columns else pd.Series("user", index=lab.index)
        lab = lab.assign(_w=by.map(lambda b: 0.6 if b == "auto" else 1.0))   # otomatik etiket daha düşük ağırlık
        m = ev.merge(lab[["event_id", "label", "_w"]].astype({"event_id": str, "label": str}), on="event_id")
        rows += list(zip(m["_text"], m["label"]))
        weights += m["_w"].astype(float).tolist()
        n_user = int((m["_w"] == 1.0).sum())
    # Self-training örnekleri her seferinde SAF KURALLA yeniden hesaplanır
    # (modelin kendi çıktısıyla eğitilmesi döngüyü kilitler ve hatayı büyütür).
    base_cols = [c for c in ["event_id", "source", "stage", "date", "title", "text", "agency", "amount_type",
                             "extra", "url", "amount_usd", "recipient", "tickers", "known_at"] if c in ev.columns]
    rule = Classifier(load_model=False).classify(ev[base_cols].reset_index(drop=True))
    rule["_text"] = ev["_text"].to_numpy()
    rule["method"] = rule["method"].astype(str)
    pseudo = rule[(rule["confidence"] >= 0.6) & (rule["method"] == "rule") & (rule["program"] != "other")
                  & rule["relevant"].astype(bool)]
    pseudo = pseudo.sample(min(len(pseudo), 3000), random_state=0) if len(pseudo) else pseudo
    rows += list(zip(pseudo["_text"], pseudo["program"]))
    weights += [0.3] * len(pseudo)
    weak_neg = rule[(rule["relevance_prob"] <= 0.08) & (rule["source"].isin(["congress", "gdelt", "federal_register"]))]
    weak_neg = weak_neg.sample(min(len(weak_neg), 1500), random_state=0) if len(weak_neg) else weak_neg
    rows += [(t, IRRELEVANT) for t in weak_neg["_text"]]
    weights += [0.3] * len(weak_neg)
    if len(rows) < 30 or len({r[1] for r in rows}) < 2:
        return {"trained": False, "reason": "yetersiz örnek", "n": len(rows)}
    X, y = [r[0] for r in rows], [r[1] for r in rows]
    pipe = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=50000, sublinear_tf=True)),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced", C=2.0)),
    ])
    pipe.fit(X, y, clf__sample_weight=np.asarray(weights))
    path = store_dir() / "models"
    path.mkdir(exist_ok=True)
    joblib.dump({"pipeline": pipe, "n_user_labels": n_user, "classes": list(pipe.classes_)},
                path / "classifier.joblib", compress=3)
    return {"trained": True, "n": len(rows), "n_user_labels": n_user, "classes": len(pipe.classes_)}
