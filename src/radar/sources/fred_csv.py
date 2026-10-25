"""FRED grafik CSV uç noktası — API anahtarı gerekmez (az bilinen ama resmi ve kararlı yol).

Ekonomik Politika Belirsizliği (Baker-Bloom-Davis) mali alt endeksleri, faiz ve kredi riski primi.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .. import http
from ..settings import sources

log = logging.getLogger("radar.fred")


def parse_csv(text: str, series: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    date_col = "observation_date" if "observation_date" in df.columns else df.columns[0]
    val_col = series if series in df.columns else df.columns[-1]
    out = pd.DataFrame({"date": df[date_col].astype(str),
                        "value": pd.to_numeric(df[val_col], errors="coerce")})
    out["series"] = series
    return out.dropna(subset=["value"])


def fetch() -> pd.DataFrame:
    cfg = sources()["fred"]
    frames = []
    for s in cfg["series"]:
        try:
            r = http.request("GET", cfg["csv_url"], params={"id": s}, browser=True)
            frames.append(parse_csv(r.text, s))
        except Exception as exc:
            log.warning("FRED %s alınamadı: %s", s, exc)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "value", "series"])
