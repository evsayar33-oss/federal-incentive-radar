"""Tablo deposu. pyarrow varsa Parquet, yoksa sıkıştırılmış CSV kullanır.

Tüm tablolar `store/` içinde düz dosyalardır. GitHub Actions bu klasörü 'data' dalına commit eder.
Streamlit uygulaması aynı dosyaları raw.githubusercontent.com üzerinden okur.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .settings import store_dir

try:
    import pyarrow  # noqa: F401
    FORMAT = "parquet"
except ImportError:  # yerel test ortamı
    FORMAT = "csv"

EXT = {"parquet": ".parquet", "csv": ".csv.gz"}


def _path(name: str, fmt: str | None = None) -> Path:
    return store_dir() / f"{name}{EXT[fmt or FORMAT]}"


def exists(name: str) -> bool:
    return any(_path(name, f).exists() for f in EXT)


def read(name: str) -> pd.DataFrame:
    for fmt in (FORMAT, "parquet", "csv"):
        p = _path(name, fmt)
        if p.exists():
            if fmt == "parquet":
                try:
                    return pd.read_parquet(p)
                except ImportError:
                    continue
            return pd.read_csv(p, low_memory=False)
    return pd.DataFrame()


def write(name: str, df: pd.DataFrame) -> None:
    df = df.reset_index(drop=True)
    p = _path(name)
    if FORMAT == "parquet":
        # Karışık tipli object sütunlarını metne çevir (Parquet şema hatasını önler).
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].map(lambda v: v if v is None or isinstance(v, str) else str(v))
        df.to_parquet(p, index=False)
    else:
        df.to_csv(p, index=False, compression="gzip")


def upsert(name: str, new: pd.DataFrame, key: list[str] | str, keep: str = "last") -> pd.DataFrame:
    if new is None or new.empty:
        return read(name)
    old = read(name)
    merged = new if old.empty else pd.concat([old, new], ignore_index=True)
    key = [key] if isinstance(key, str) else key
    for k in key:
        merged[k] = merged[k].astype(str)
    merged = merged.drop_duplicates(subset=key, keep=keep)
    write(name, merged)
    return merged


def append(name: str, new: pd.DataFrame) -> pd.DataFrame:
    if new is None or new.empty:
        return read(name)
    old = read(name)
    merged = new if old.empty else pd.concat([old, new], ignore_index=True)
    write(name, merged)
    return merged


# ---- JSON durum dosyaları -------------------------------------------------

def load_json(name: str, default=None):
    p = store_dir() / f"{name}.json"
    if not p.exists():
        return {} if default is None else default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(name: str, obj) -> None:
    p = store_dir() / f"{name}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
