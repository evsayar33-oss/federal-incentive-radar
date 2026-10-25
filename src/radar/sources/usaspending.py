"""USAspending API — anahtar gerekmez. Hibe ve kredi yükümlülükleri (gerçek para)."""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from .. import http, schema
from ..settings import sources

log = logging.getLogger("radar.usaspending")

GRANT_FIELDS = ["Award ID", "Recipient Name", "Award Amount", "Description", "Start Date",
                "Awarding Agency", "Awarding Sub Agency", "Recipient UEI", "generated_internal_id"]
LOAN_FIELDS = ["Award ID", "Recipient Name", "Loan Value", "Subsidy Cost", "Description", "Issued Date",
               "Awarding Agency", "Awarding Sub Agency", "Recipient UEI", "generated_internal_id"]


def parse(results: list[dict], kind: str) -> list[dict]:
    rows = []
    for r in results:
        if kind == "loan":
            amount = r.get("Subsidy Cost") or 0.0   # kredide ekonomik değer = sübvansiyon maliyeti
            face = r.get("Loan Value")
            d = r.get("Issued Date")
            atype = "loan_subsidy_cost"
        else:
            amount = r.get("Award Amount")
            face = amount
            d = r.get("Start Date")
            atype = "contract_obligation" if kind == "contract" else "obligation"
        gid = r.get("generated_internal_id") or r.get("Award ID")
        rows.append(schema.event(
            "usaspending", f"{kind}-{r.get('Award ID')}", 5, d or "",
            f"{r.get('Awarding Sub Agency') or r.get('Awarding Agency')}: {r.get('Recipient Name')}",
            text=r.get("Description") or "", url=f"https://www.usaspending.gov/award/{gid}",
            agency=r.get("Awarding Agency") or "", amount_usd=amount, amount_type=atype,
            recipient=r.get("Recipient Name") or "",
            extra={"kind": kind, "face_value": face, "uei": r.get("Recipient UEI"),
                   "sub_agency": r.get("Awarding Sub Agency")},
        ))
    return rows


def _search(codes: list[str], fields: list[str], sort: str, since: date, kind: str, cfg: dict,
            until: date | None = None, max_pages: int | None = None) -> list[dict]:
    rows = []
    for page in range(1, (max_pages or cfg["max_pages"]) + 1):
        body = {
            "filters": {
                "time_period": [{"start_date": since.isoformat(), "end_date": (until or date.today()).isoformat(),
                                 "date_type": "action_date"}],
                "award_type_codes": codes,
            },
            "fields": fields, "page": page, "limit": 100, "sort": sort, "order": "desc",
        }
        if kind == "grant":
            body["filters"]["award_amounts"] = [{"lower_bound": cfg["min_award_usd"]}]
        elif kind == "contract":
            body["filters"]["award_amounts"] = [{"lower_bound": cfg["contract_min_usd"]}]
        data = http.post_json(cfg["url"], json=body)
        res = data.get("results") or []
        rows.extend(parse(res, kind))
        if not (data.get("page_metadata") or {}).get("hasNext"):
            break
    return rows


def fetch(since: date, until: date | None = None, max_pages: int | None = None) -> pd.DataFrame:
    cfg = sources()["usaspending"]
    rows = _search(cfg["grant_codes"], GRANT_FIELDS, "Award Amount", since, "grant", cfg, until, max_pages)
    try:
        rows += _search(cfg["loan_codes"], LOAN_FIELDS, "Loan Value", since, "loan", cfg, until, max_pages)
    except Exception as exc:
        log.warning("Kredi araması başarısız: %s", exc)
    if cfg.get("contracts_enabled", True):
        try:  # Federal sözleşmeler: şirketlere giden en büyük doğrudan federal para akışı
            rows += _search(cfg["contract_codes"], GRANT_FIELDS, "Award Amount", since, "contract", cfg, until, max_pages)
        except Exception as exc:
            log.warning("Sözleşme araması başarısız: %s", exc)
    return schema.frame(rows)
