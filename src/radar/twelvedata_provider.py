"""Independent, key-gated Twelve Data daily OHLCV for H083 research only.

The free Basic tier is rate limited and restricted to eligible use.
Never request data without TWELVE_DATA_API_KEY, publish credentials, or
backfill/interpolate missing sessions. The caller validates the full series
against the completed benchmark close and the existing primary sample.
"""
from __future__ import annotations

import os

import pandas as pd

from . import http


def fetch_bars(ticker: str, period: str = "3y") -> pd.DataFrame:
    """Fetch a whole adjusted daily series, or return no usable observations."""
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TWELVE_DATA_NOT_CONFIGURED")
    if not ticker or not isinstance(ticker, str):
        raise ValueError("INVALID_TWELVE_DATA_SYMBOL")
    # The provider's official docs recommend Authorization rather than a
    # query parameter: this keeps the key out of request URLs and logs.
    response = http.request(
        "GET", "https://api.twelvedata.com/time_series",
        params={
            "symbol": ticker.replace(".", "-"),
            "interval": "1day",
            "outputsize": 750,
            "adjust": "all",
            "format": "JSON",
        },
        headers={"Authorization": "apikey " + key, "Accept": "application/json"},
        retries=0,
        timeout=12,
    )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        # Provider may return quota/permission errors as HTTP 200.
        # Never echo remote messages that might contain credentials.
        raise RuntimeError("TWELVE_DATA_RESPONSE_NOT_OK")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("TWELVE_DATA_METADATA_MISSING")
    requested = ticker.upper().replace(".", "-")
    received = str(meta.get("symbol", "")).upper().replace(".", "-")
    if (received != requested
            or meta.get("interval") != "1day"
            or meta.get("currency") != "USD"
            or meta.get("exchange_timezone") != "America/New_York"):
        raise ValueError("TWELVE_DATA_IDENTITY_OR_EXCHANGE_MISMATCH")
    values = payload.get("values")
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not isinstance(values, list) or not values:
        raise ValueError("TWELVE_DATA_EMPTY_SERIES")
    if any(not isinstance(v, dict) or not required.issubset(v) for v in values):
        raise ValueError("TWELVE_DATA_MISSING_OHLCV_FIELD")
    frame = pd.DataFrame(values)
    frame["Date"] = pd.to_datetime(frame["datetime"], errors="coerce")
    if frame["Date"].isna().any():
        raise ValueError("TWELVE_DATA_INVALID_SESSION_DATE")
    frame = frame.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    # normalize() lives in price_resilience; importing lazily avoids a cycle.
    from .price_resilience import normalize
    return normalize(frame.set_index("Date"))
