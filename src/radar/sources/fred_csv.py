"""FRED verisi: önce anahtarsız grafik CSV, olmazsa resmi API (isteğe bağlı ücretsiz FRED_API_KEY).

Ekonomik Politika Belirsizliği (Baker-Bloom-Davis) mali alt endeksleri, faiz ve kredi riski primi.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .. import http
from ..settings import env, sources

log = logging.getLogger("radar.fred")
API_URL = "https://api.stlouisfed.org/fred/series/observations"


def parse_csv(text: str, series: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    date_col = "observation_date" if "observation_date" in df.columns else df.columns[0]
    val_col = series if series in df.columns else df.columns[-1]
    out = pd.DataFrame({"date": df[date_col].astype(str),
                        "value": pd.to_numeric(df[val_col], errors="coerce")})
    out["series"] = series
    return out.dropna(subset=["value"])


def _via_csv(series: str) -> pd.DataFrame:
    url = sources()["fred"]["csv_url"]
    last = None
    for browser in (False, True):
        try:
            r = http.request("GET", url, params={"id": series}, browser=browser, retries=2, timeout=60)
            if not r.text.lstrip().lower().startswith(("observation_date", "date")):
                raise ValueError(f"CSV değil: {r.text[:120]!r}")
            return parse_csv(r.text, series)
        except Exception as exc:
            last = exc
    raise last  # type: ignore[misc]


def _via_api(series: str, key: str) -> pd.DataFrame:
    data = http.get_json(API_URL, params={"series_id": series, "api_key": key, "file_type": "json"})
    obs = data.get("observations") or []
    df = pd.DataFrame({"date": [o["date"] for o in obs],
                       "value": pd.to_numeric([o["value"] for o in obs], errors="coerce")})
    df["series"] = series
    return df.dropna(subset=["value"])


def fetch() -> pd.DataFrame:
    cfg = sources()["fred"]
    key = env("FRED_API_KEY")
    frames = []
    for s in cfg["series"]:
        try:
            frames.append(_via_csv(s))
            continue
        except Exception as exc:
            log.warning("FRED CSV %s başarısız: %s", s, str(exc)[:200])
        if key:
            try:
                frames.append(_via_api(s, key))
            except Exception as exc:
                log.warning("FRED API %s başarısız: %s", s, str(exc)[:200])
    if not frames:
        raise RuntimeError("FRED serilerinin hiçbiri alınamadı" + ("" if key else " (FRED_API_KEY tanımlı değil)"))
    return pd.concat(frames, ignore_index=True)
