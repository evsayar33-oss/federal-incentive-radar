"""Akıllı otomatik etiketleme ve veri denetimi (insan beklemeden hatayı bulur ve düzeltir).

Yöntem: zayıf denetim (weak supervision). Birbirinden bağımsız "etiketleme fonksiyonları" (LF) her olaya
oy verir. Bir LF yüksek güvenle hata görürse otomatik etiket üretilir. Otomatik etiketler:
  - sınıflandırıcıda kullanıcı etiketi gibi uygulanır (kullanıcı etiketi her zaman önce gelir),
  - haftalık eğitimde daha düşük ağırlıkla öğretilir (model hatayı genelleştirmeyi öğrenir),
  - her karar gerekçesiyle 'quality_issues' tablosuna yazılır (denetlenebilir, geri alınabilir).

LF listesi:
  1. noise_title     : idari / akademik / tören belgeleri → ilgisiz (ör. "Young Faculty Award", "Comment Request").
  2. agency_conflict : program ile yayımlayan kurum çelişiyor ve başlıkta güçlü kanıt yok → kurumla uyumlu
                       programa taşı; uygun program yoksa ilgisiz/diğer.
  3. public_recipient: ödül alıcısı üniversite, belediye, eyalet vb. → borsada değil (ticker = NONE).
  4. safe_entity     : inceleme kuyruğundaki aday adı alıcı adının içinde ve ilk kelime aynı → onayla.
Ek denetimler (data_checks): kaynak sessizliği, bayat fiyat, aşırı inceleme kuyruğu.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import pandas as pd

from . import store
from .entity import normalize
from .settings import programs, store_dir

log = logging.getLogger("radar.autolabel")
AUTO_FILE = "auto_labels.jsonl"
TEXT_SOURCES = {"federal_register", "congress", "grants_gov", "gdelt"}

NOISE = re.compile("|".join([
    r"information collection", r"comment request", r"sunshine act", r"privacy act", r"\bmeeting\b",
    r"advisory committee", r"\bfaculty\b", r"fellowship", r"scholarship", r"\bstudents?\b", r"\beducation",
    r"training program", r"research centers?\b", r"young investigator", r"career development award",
    r"clinical trial", r"\balcohol", r"post office", r"designate the facility", r"commemorat",
    r"\brecogniz(ing|e)\b.*\b(day|week|month)\b", r"national .{1,40} (day|week|month)\b", r"airworthiness",
    r"fisheries", r"small business development center", r"\bmigratory bird", r"endangered and threatened",
    r"\bnaming\b", r"\bmedal\b", r"\bmemorial\b",
]), re.I)

# Kurum anahtar kelimesi → makul programlar. Kurum burada yoksa LF oy vermez.
AGENCY_PROGRAMS = {
    "internal revenue": {"clean_power", "clean_manufacturing", "ev_auto", "hydrogen", "nuclear", "carbon_management",
                         "agriculture", "housing", "semiconductors"},
    "treasury": {"clean_power", "clean_manufacturing", "ev_auto", "hydrogen", "nuclear", "carbon_management",
                 "agriculture", "housing", "semiconductors", "critical_minerals"},
    "energy": {"clean_power", "clean_manufacturing", "ev_auto", "hydrogen", "nuclear", "grid", "carbon_management",
               "critical_minerals", "ai_quantum", "semiconductors"},
    "transportation": {"infrastructure", "ev_auto"},
    "highway": {"infrastructure", "ev_auto"},
    "federal aviation": {"infrastructure"},
    "agriculture": {"agriculture", "broadband", "clean_power"},
    "housing and urban": {"housing", "infrastructure"},
    "health and human": {"health_bio"},
    "national institutes": {"health_bio"},
    "food and drug": {"health_bio"},
    "defense": {"defense_industrial", "critical_minerals", "semiconductors", "ai_quantum", "health_bio", "nuclear"},
    "darpa": {"defense_industrial", "semiconductors", "ai_quantum"},
    "defense advanced research": {"defense_industrial", "semiconductors", "ai_quantum"},
    "commerce": {"semiconductors", "broadband", "critical_minerals", "ai_quantum", "defense_industrial"},
    "national telecommunications": {"broadband"},
    "interior": {"critical_minerals", "clean_power", "infrastructure"},
    "environmental protection": {"clean_power", "ev_auto", "carbon_management", "infrastructure", "housing"},
}
PUBLIC = re.compile(r"\b(university|college|school|city of|county|state of|commonwealth|department of|authority|"
                    r"district|tribe|tribal|board of|regents|institute of technology|foundation|hospital|"
                    r"municipal|village of|town of|port of|government|ministry|council)\b", re.I)


CORPORATE = re.compile(r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|company|co|ltd|lp|plc|holdings)\b\.?", re.I)


def is_public(name: str) -> bool:
    """Kamu kurumu mu? Şirket eki (Inc, LLC, Corp) varsa şirkettir (ör. 'X Government Solutions, Inc.')."""
    n = str(name or "")
    return bool(PUBLIC.search(n)) and not CORPORATE.search(n)


def _rules():
    from .classify import Classifier
    return Classifier(load_model=False)


# ---------------------------------------------------------------- etiketleme fonksiyonları

def lf_noise(row: dict) -> tuple[str, str] | None:
    if row.get("source") not in TEXT_SOURCES:
        return None
    if NOISE.search(str(row.get("title", ""))):
        return "irrelevant", "idari/akademik/tören belgesi (piyasayı etkilemez)"
    return None


def _allowed(agency: str) -> set[str] | None:
    a = (agency or "").lower()
    sets = [v for k, v in AGENCY_PROGRAMS.items() if k in a]
    return set().union(*sets) if sets else None


def lf_agency(row: dict, clf) -> tuple[str, str] | None:
    prog = str(row.get("program", ""))
    if row.get("source") == "sec_edgar" or prog in ("", "other", "nan"):
        return None
    allowed = _allowed(str(row.get("agency", "")))
    if not allowed or prog in allowed:
        return None
    hits, _ = clf.rule_hits(str(row.get("title", "")))
    strong = {p for p, pats in clf.rules.items() for pat, w in pats if w >= 3 and pat.search(str(row.get("title", "")).lower())}
    if prog in strong:
        return None  # başlık programı açıkça söylüyor; kurum çelişkisi önemsiz
    text_hits, _ = clf.rule_hits(f"{row.get('title', '')}. {row.get('text', '')}")
    cand = {p: s for p, s in text_hits.items() if p in allowed and s > 0}
    if cand:
        best = max(cand, key=cand.get)
        return best, f"kurum ({row.get('agency')}) '{prog}' ile uyumsuz → '{best}'"
    new = "other" if row.get("source") in ("usaspending", "grants_gov") else "irrelevant"
    return new, f"kurum ({row.get('agency')}) '{prog}' ile uyumsuz, uygun program yok"


def lf_public_recipient(row: dict) -> tuple[str, str] | None:
    rec = str(row.get("recipient") or "")
    if row.get("source") != "usaspending" or not rec or not is_public(rec):
        return None
    return "NONE", "alıcı kamu kurumu/üniversite (borsada işlem görmez)"


def lf_safe_entity(rec_norm: str, cand_name_norm: str) -> bool:
    r, c = rec_norm.split(), cand_name_norm.split()
    return bool(r and c and r[0] == c[0] and set(c) <= set(r) and len(r[0]) >= 4)


# ---------------------------------------------------------------- denetim

def audit(events: pd.DataFrame, review: pd.DataFrame | None = None, universe: pd.DataFrame | None = None,
          user_labels: pd.DataFrame | None = None) -> tuple[list[dict], pd.DataFrame]:
    """Otomatik etiketler ve gerekçeli sorun listesi üretir. Kullanıcının etiketlediği olaya dokunmaz."""
    if events is None or events.empty:
        return [], pd.DataFrame()
    clf = _rules()
    ts = date.today().isoformat()
    done_ev, done_ent = set(), set()
    if user_labels is not None and not user_labels.empty:
        done_ev = set(user_labels.loc[user_labels["kind"] == "program", "event_id"].astype(str))
        done_ent = set(user_labels.loc[user_labels["kind"] == "entity", "recipient_norm"].astype(str))
    labels, issues = [], []
    for r in events.to_dict("records"):
        eid = str(r.get("event_id"))
        if eid in done_ev or str(r.get("method")) == "label":
            continue
        for lf, fn in (("noise_title", lf_noise), ("agency_conflict", lambda x: lf_agency(x, clf))):
            res = fn(r)
            if res and res[0] != r.get("program") and not (res[0] == "irrelevant" and str(r.get("relevant")).lower() in ("false", "0")):
                labels.append({"event_id": eid, "kind": "program", "label": res[0], "ts": ts, "by": "auto", "lf": lf})
                issues.append({"type": "program", "lf": lf, "event_id": eid, "title": str(r.get("title"))[:200],
                               "old": r.get("program"), "new": res[0], "reason": res[1], "date": r.get("date")})
                break
        res = lf_public_recipient(r)
        rn = normalize(str(r.get("recipient") or ""))
        if res and rn and rn not in done_ent and str(r.get("tickers") or "").strip() not in ("", "nan"):
            labels.append({"kind": "entity", "recipient_norm": rn, "ticker": "NONE", "ts": ts, "by": "auto",
                           "lf": "public_recipient"})
            issues.append({"type": "entity", "lf": "public_recipient", "event_id": eid, "title": r.get("recipient"),
                           "old": r.get("tickers"), "new": "NONE", "reason": res[1], "date": r.get("date")})
            done_ent.add(rn)
    if review is not None and not review.empty and universe is not None and not universe.empty:
        names = dict(zip(universe["ticker"].astype(str), universe["name"].astype(str).map(normalize)))
        for q in review.to_dict("records"):
            rn = str(q["recipient_norm"])
            if rn in done_ent:
                continue
            if is_public(str(q["recipient"])):
                t, why = "NONE", "alıcı kamu kurumu/üniversite"
            elif lf_safe_entity(rn, names.get(str(q["candidate"]), "")):
                t, why = str(q["candidate"]), "aday şirket adı alıcı adının içinde, ilk kelime aynı"
            else:
                continue
            labels.append({"kind": "entity", "recipient_norm": rn, "ticker": t, "ts": ts, "by": "auto",
                           "lf": "safe_entity" if t != "NONE" else "public_recipient"})
            issues.append({"type": "entity", "lf": "review_queue", "event_id": q.get("event_id"), "title": q["recipient"],
                           "old": q["candidate"], "new": t, "reason": why, "date": ts})
            done_ent.add(rn)
    return labels, pd.DataFrame(issues)


def save(labels: list[dict], issues: pd.DataFrame) -> int:
    """Yeni otomatik etiketleri dosyaya ekler. Aynı karar iki kez yazılmaz."""
    d = store_dir() / "labels"
    d.mkdir(parents=True, exist_ok=True)
    p = d / AUTO_FILE
    old = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
    key = lambda x: (x["kind"], x.get("event_id") or x.get("recipient_norm"))  # noqa: E731
    have = {key(x): x for x in old}
    new = [x for x in labels if have.get(key(x), {}).get("label", have.get(key(x), {}).get("ticker"))
           != x.get("label", x.get("ticker"))]
    if new:
        for x in new:
            have[key(x)] = x
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in have.values()) + "\n", encoding="utf-8")
    if issues is not None and not issues.empty:
        issues = issues.assign(found_at=date.today().isoformat())
        store.upsert("quality_issues", issues, ["type", "event_id", "title"])
    return len(new)


# ---------------------------------------------------------------- veri denetimleri

def data_checks(runs: pd.DataFrame, prices: pd.DataFrame, review: pd.DataFrame) -> list[str]:
    """Sistem sağlığı mesajına eklenecek uyarılar (insan müdahalesi gerekenler)."""
    out = []
    if prices is not None and not prices.empty:
        last = pd.to_datetime(prices["date"], errors="coerce").max()
        if pd.notna(last) and last.date() < date.today() - timedelta(days=5):
            out.append(f"Fiyat verisi bayat (son: {last.date()}). Fiyat kaynağı çalışmıyor olabilir.")
    if runs is not None and not runs.empty and "status" in runs.columns:
        recent = runs[runs["slot"] != "weekly"].tail(6)
        zero: dict[str, int] = {}
        for s in recent["status"]:
            try:
                st = json.loads(s)
            except (TypeError, ValueError):
                continue
            for k, v in st.items():
                if isinstance(v, dict) and v.get("ok") and v.get("rows") == 0:
                    zero[k] = zero.get(k, 0) + 1
        for k, n in zero.items():
            if n >= 6 and k not in ("gdelt",):
                out.append(f"'{k}' son {n} çalışmada hiç veri getirmedi. Kaynak değişmiş olabilir.")
    if review is not None and len(review) > 50:
        out.append(f"İnceleme kuyruğunda {len(review)} belirsiz şirket eşleşmesi var.")
    return out


# ---------------------------------------------------------------- sepet kalitesi

def program_sectors() -> dict[str, set[str]]:
    return {pid: set(p["sectors"]) for pid, p in programs()["programs"].items()}
