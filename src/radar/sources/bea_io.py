"""BEA girdi-çıktı (Leontief) yayılma matrisi — ücretsiz BEA_API_KEY gerekir.

Mantık:
- BEA TableID 56: sektör düzeyi "Total Requirements" matrisi L (satır: endüstri, sütun: emtia).
  L[i, j] = j emtiasına 1 $ nihai talep geldiğinde i endüstrisinin toplam (doğrudan + dolaylı) üretimi.
- Bu 15×15 matris, config/bea_sectors.yaml ile 11×11 GICS-benzeri sektör matrisine dönüştürülür:
  S = Mᵀ · L · Pᵀ   (M: BEA→GICS payları, P: GICS→BEA dağıtımı)
- Skorlamada sektör payları W, W · ((1−a)·I + a·S) ile genişletilir (a = priors.spillover.weight).
  Böylece örneğin bir yarı iletken hibesi, Malzeme ve Kamu hizmetleri sektörlerine de dolaylı etki taşır.
Anahtar yoksa modül sessizce atlanır ve skor yalnızca doğrudan etkiyle hesaplanır.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import yaml

from .. import http
from ..settings import CONFIG_DIR, SECTORS, env

log = logging.getLogger("radar.bea")
URL = "https://apps.bea.gov/api/data"


def _cfg() -> dict:
    with open(CONFIG_DIR / "bea_sectors.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve(code: str, descr: str, cfg: dict) -> str | None:
    code = str(code).strip()
    if code in cfg["codes"]:
        return code
    d = str(descr or "").lower()
    for kw, c in cfg["keywords"].items():
        if kw in d:
            return c
    return None


def parse(data_rows: list[dict]) -> pd.DataFrame:
    """BEA yanıt satırlarından en son yılın uzun (row, col, value) tablosunu çıkarır.
    Alan adları değişse bile 'Row'/'Col'/'Value' içeren alanları otomatik bulur."""
    df = pd.DataFrame(data_rows)
    if df.empty:
        return df
    pick = lambda *keys: next((c for c in df.columns if all(k in c.lower() for k in keys)), None)  # noqa: E731
    rc, rd = pick("row", "code"), pick("row", "desc")
    cc, cd = pick("col", "code"), pick("col", "desc")
    vc = pick("datavalue") or pick("value")
    yc = pick("year")
    out = pd.DataFrame({"row": df[rc], "row_descr": df[rd] if rd else "", "col": df[cc],
                        "col_descr": df[cd] if cd else "",
                        "value": pd.to_numeric(df[vc].astype(str).str.replace(",", ""), errors="coerce"),
                        "year": df[yc].astype(str) if yc else "latest"})
    out = out.dropna(subset=["value"])
    return out[out["year"] == out["year"].max()]


def to_sector_matrix(long: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    cfg = _cfg()
    long = long.copy()
    long["r"] = [_resolve(c, d, cfg) for c, d in zip(long["row"], long["row_descr"])]
    long["c"] = [_resolve(c, d, cfg) for c, d in zip(long["col"], long["col_descr"])]
    long = long.dropna(subset=["r", "c"])
    codes = list(cfg["codes"])
    L = long.pivot_table(index="r", columns="c", values="value", aggfunc="sum").reindex(index=codes, columns=codes).fillna(0.0)
    M = np.zeros((len(codes), len(SECTORS)))  # BEA → GICS
    for i, c in enumerate(codes):
        for s, w in cfg["codes"][c].items():
            M[i, SECTORS.index(s)] = w
    P = M / np.where(M.sum(axis=0) == 0, 1, M.sum(axis=0))  # her GICS sektörü için BEA dağıtımı (sütun normalize)
    S = M.T @ L.to_numpy() @ P
    # Boş sektörler (BEA'da karşılığı yok) için birim etki.
    for k in range(len(SECTORS)):
        if S[:, k].sum() == 0:
            S[k, k] = 1.0
    year = str(long["year"].iloc[0]) if len(long) else ""
    return pd.DataFrame(S, index=SECTORS, columns=SECTORS), year


def fetch() -> pd.DataFrame:
    key = env("BEA_API_KEY")
    if not key:
        log.info("BEA_API_KEY yok; girdi-çıktı yayılması kapalı (yalnızca doğrudan etki).")
        return pd.DataFrame()
    params = {"UserID": key, "method": "GetData", "DataSetName": "InputOutput",
              "TableID": _cfg()["table_id"], "Year": "ALL", "ResultFormat": "JSON"}
    data = http.get_json(URL, params=params, timeout=90)
    res = (data.get("BEAAPI") or {}).get("Results")
    if isinstance(res, list):
        res = res[0] if res else {}
    if not res or "Data" not in res:
        err = (data.get("BEAAPI") or {}).get("Error") or (res or {}).get("Error")
        raise RuntimeError(f"BEA yanıtında veri yok: {str(err)[:300]}")
    S, year = to_sector_matrix(parse(res["Data"]))
    out = S.reset_index(names="sector")
    out["bea_year"] = year
    return out


def transform(io_table: pd.DataFrame, weight: float) -> np.ndarray | None:
    """Skorlama için 11×11 dönüşüm. Satır vektörü w için w_toplam = w · Tᵀ, bu yüzden Tᵀ döner.
    T = (1−a)·I + a·S. Tablo yoksa None."""
    if io_table is None or io_table.empty or weight <= 0:
        return None
    S = io_table.set_index("sector").reindex(index=SECTORS)[SECTORS].to_numpy(dtype=float)
    if not np.isfinite(S).all():
        return None
    return ((1 - weight) * np.eye(len(SECTORS)) + weight * S).T
