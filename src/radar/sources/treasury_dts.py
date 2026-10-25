"""Treasury Fiscal Data — Daily Treasury Statement (anahtar gerekmez).

Program/ajans bazlı GERÇEK günlük nakit çıkışı. Ödeme hızının (disbursement momentum)
çeyreklik verilerden aylar önce görülmesini sağlar.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .. import http
from ..settings import sources


def parse(data: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["record_date", "category", "amount_musd"])
    df = df.rename(columns={"transaction_catg": "category", "transaction_today_amt": "amount_musd"})
    df["amount_musd"] = pd.to_numeric(df["amount_musd"], errors="coerce")
    df = df[df["category"].notna() & ~df["category"].str.contains("Total|null", case=False, na=False)]
    return df[["record_date", "category", "amount_musd"]]


def fetch(since: date) -> pd.DataFrame:
    cfg = sources()["treasury_dts"]
    start = min(since, date.today() - timedelta(days=cfg["history_days"]))
    out, page = [], 1
    while True:
        params = {"filter": f"record_date:gte:{start.isoformat()},transaction_type:eq:Withdrawals",
                  "fields": "record_date,transaction_type,transaction_catg,transaction_today_amt",
                  "page[size]": 5000, "page[number]": page, "sort": "record_date"}
        data = http.get_json(cfg["url"], params=params)
        out.extend(data.get("data") or [])
        total_pages = (data.get("meta") or {}).get("total-pages", 1)
        if page >= total_pages:
            break
        page += 1
    return parse(out)
