"""Yol, ortam değişkeni ve konfigürasyon yükleyicileri."""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def store_dir() -> Path:
    """Veri deposu klasörü. GitHub Actions'ta 'data' dalının çalışma ağacıdır."""
    p = Path(os.environ.get("RADAR_STORE", ROOT / "store"))
    p.mkdir(parents=True, exist_ok=True)
    return p


@lru_cache(maxsize=None)
def sources() -> dict:
    with open(CONFIG_DIR / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def programs() -> dict:
    with open(CONFIG_DIR / "programs.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def priors() -> dict:
    with open(CONFIG_DIR / "priors.json", encoding="utf-8") as f:
        return json.load(f)


def sector_etfs() -> dict[str, str]:
    return dict(sources()["sector_etfs"])


SECTORS = [
    "Information Technology", "Energy", "Industrials", "Materials", "Utilities",
    "Health Care", "Financials", "Consumer Discretionary", "Consumer Staples",
    "Communication Services", "Real Estate",
]


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v if v not in ("", None) else default


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("RADAR_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger("radar")
