"""Kibar HTTP istemcisi: ana bilgisayar başına hız sınırı, yeniden deneme, Retry-After desteği."""
from __future__ import annotations

import logging
import random
import time
from urllib.parse import urlparse

import requests

from .settings import env

log = logging.getLogger("radar.http")

# Saniye cinsinden iki istek arası en kısa süre (resmi limitlerin altında kalır).
MIN_INTERVAL = {
    "efts.sec.gov": 0.15,       # SEC: tüm makineler toplamı 10 istek/sn
    "www.sec.gov": 0.15,
    "data.sec.gov": 0.15,
    "api.gdeltproject.org": 5.5,  # topluluk önerisi: 5 sn'de 1
    "api.congress.gov": 0.8,      # 5.000 istek/saat
    "api.usaspending.gov": 0.5,
    "api.grants.gov": 0.5,
    "www.federalregister.gov": 0.3,
    "api.fiscaldata.treasury.gov": 0.3,
    "fred.stlouisfed.org": 0.6,
    "api.telegram.org": 1.1,      # Telegram: sohbet başına ~1 mesaj/sn
}
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_last_call: dict[str, float] = {}
_session = requests.Session()


def research_user_agent() -> str:
    """SEC, 'ad + e-posta' içeren bir User-Agent ister. GitHub Secret: SEC_USER_AGENT."""
    return env("SEC_USER_AGENT", "FederalIncentiveRadar research-bot@example.com")


def request(method: str, url: str, *, params=None, json=None, headers=None,
            timeout: int = 45, retries: int = 4, browser: bool = False) -> requests.Response:
    host = urlparse(url).netloc
    h = {"User-Agent": BROWSER_UA if browser else research_user_agent(),
         "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    last_exc: Exception | None = None
    resp = None
    for attempt in range(retries + 1):
        wait = MIN_INTERVAL.get(host, 0.3) - (time.time() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            resp = _session.request(method, url, params=params, json=json, headers=h, timeout=timeout)
        except requests.RequestException as exc:  # ağ hatası
            last_exc, resp = exc, None
        if resp is not None and resp.status_code < 400:
            return resp
        if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60.0, 2 ** attempt + random.random())
        if delay > 120:   # sunucu saatlerce beklemeyi istiyor (kota doldu) → bekleme, hemen yedeğe geç
            break
        delay = min(delay, 60.0)
        log.warning("%s %s -> %s, %.1fs sonra tekrar", method, host,
                    resp.status_code if resp is not None else last_exc, delay)
        time.sleep(delay)
    if resp is not None:
        resp.raise_for_status()
    raise last_exc or RuntimeError(f"İstek başarısız: {url}")


def get_json(url: str, **kw):
    return request("GET", url, **kw).json()


def post_json(url: str, **kw):
    return request("POST", url, **kw).json()
