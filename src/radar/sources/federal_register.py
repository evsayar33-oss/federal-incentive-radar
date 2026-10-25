"""Federal Register API — anahtar gerekmez. Kurallar, önerilen kurallar, bildirimler, kararnameler."""
from __future__ import annotations

import logging
import re
from datetime import date

import pandas as pd

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.fr")
NOFO_RE = re.compile(r"notice of funding|funding opportunit|request for applications|solicitation of applications", re.I)
FIELDS = ["document_number", "title", "type", "abstract", "agencies", "publication_date",
          "html_url", "action", "significant", "dates"]


def stage_for(doc: dict) -> tuple[int, dict]:
    t = (doc.get("type") or "").lower()
    text = f"{doc.get('title','')} {doc.get('action','')} {doc.get('abstract','')}"
    if "presidential" in t:
        return 2, {"doc_type": "presidential"}
    if t == "rule":
        return 3, {"doc_type": "final_rule"}
    if "proposed" in t:
        return 3, {"doc_type": "proposed_rule"}
    if NOFO_RE.search(text):
        return 4, {"doc_type": "nofo_notice"}
    return 3, {"doc_type": "notice"}


def parse(results: list[dict]) -> list[dict]:
    rows = []
    for d in results:
        stage, extra = stage_for(d)
        agencies = ", ".join(a.get("name") or a.get("raw_name") or "" for a in (d.get("agencies") or []))
        extra.update({"significant": d.get("significant"), "action": d.get("action")})
        rows.append(schema.event(
            "federal_register", d.get("document_number"), stage, d.get("publication_date"),
            d.get("title"), text=f"{d.get('action') or ''}. {d.get('abstract') or ''}",
            url=d.get("html_url"), agency=agencies, extra=extra,
        ))
    return rows


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["federal_register"]
    rows: list[dict] = []
    for term in cfg["terms"]:
        params = [("per_page", cfg["per_page"]), ("order", "newest"),
                  ("conditions[publication_date][gte]", since.isoformat()),
                  ("conditions[term]", term)]
        params += [("conditions[type][]", t) for t in cfg["doc_types"]]
        params += [("fields[]", f) for f in FIELDS]
        try:
            data = http.get_json(cfg["url"], params=params)
        except Exception as exc:  # tek terim hatası tüm kaynağı durdurmasın
            log.warning("FR terim '%s' hatası: %s", term, exc)
            continue
        rows.extend(parse(data.get("results") or []))
    return schema.frame(rows)
