"""Backtest sector stock selection after federal-incentive and investment news converge."""
from __future__ import annotations

import logging
import zlib
from datetime import date

import numpy as np
import pandas as pd

from . import basket as BK
from . import evidence as E
from . import investments as I
from . import store
from .settings import sector_etfs, sources

log = logging.getLogger("radar.sector_confluence")
COLS = ["key", "selector", "exit_rule", "n", "mean_alpha_sector", "mean_excess_spy", "mean_raw",
        "raw_hit", "hit_60", "p", "q", "oos_n", "oos_hit", "oos_mean", "status", "status_name",
        "icon", "explain", "asof"]
def cfg() -> dict:
    return sources()["sector_confluence"]


def pair_events(events: pd.DataFrame, max_gap_days: int, cooldown_days: int) -> pd.DataFrame:
    """Create one signal on the later event date when the two sources converge."""
    columns = ["event_id", "sector", "date", "federal_date", "investment_date"]
    if events is None or events.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for sector, group in events.sort_values("date").groupby("sector"):
        federal = investment = None
        last_signal = None
        for event in group.to_dict("records"):
            current = pd.Timestamp(event["date"])
            if event["source"] == "federal":
                federal = event
            elif event["source"] == "investment":
                investment = event
            if federal is None or investment is None:
                continue
            fd, idate = pd.Timestamp(federal["date"]), pd.Timestamp(investment["date"])
            if abs((fd - idate).days) > max_gap_days:
                continue
            if last_signal is not None and (current - last_signal).days < cooldown_days:
                continue
            later = max(fd, idate)
            rows.append({"event_id": f"{sector}|{fd.date()}|{idate.date()}", "sector": sector,
                         "date": later.date().isoformat(), "federal_date": fd.date().isoformat(),
                         "investment_date": idate.date().isoformat()})
            last_signal = current
    return pd.DataFrame(rows, columns=columns).drop_duplicates("event_id").reset_index(drop=True)


def collect_events(sectors: dict[str, str] | None = None) -> pd.DataFrame:
    """Build sector events using only federal items and matched large-investment news."""
    sectors = sectors or {}
    rows = []
    federal = store.read("events")
    if not federal.empty:
        relevant = federal["relevant"].astype(str).str.lower().isin(["true", "1"])
        federal = federal[relevant & federal["program"].astype(str).ne("other")].copy()
        federal["sector"] = [E._dominant_sector(value) for value in federal["sector_weights"]]
        federal = federal.dropna(subset=["sector", "date"]).drop_duplicates(["program", "sector", "date"])
        rows.extend({"event_id": str(r["event_id"]), "source": "federal", "sector": str(r["sector"]),
                     "date": str(r["date"])[:10]} for r in federal.to_dict("records"))
    investments = store.read("invest_events")
    if not investments.empty:
        for row in investments.to_dict("records"):
            ticker = str(row.get("tickers", "")).split(",")[0].strip()
            sector = sectors.get(ticker)
            if ticker and sector and row.get("date"):
                rows.append({"event_id": str(row["event_id"]), "source": "investment", "sector": sector,
                             "date": str(row["date"])[:10]})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["event_id", "source", "sector", "date"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "sector"]).drop_duplicates(["source", "sector", "date"])
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    return frame.reset_index(drop=True)


def _exit_index(entry: int, rule: str, closes: np.ndarray, sma50: np.ndarray, max_horizon: int) -> int:
    end = entry + max_horizon
    if rule.startswith("fixed"):
        return min(entry + int(rule.removeprefix("fixed")), len(closes) - 1)
    if rule.startswith("sma"):
        end = min(end, len(closes) - 1)
        crossed = np.flatnonzero((closes[entry + 1:end] < sma50[entry + 1:end])
                                 & np.isfinite(sma50[entry + 1:end]))
        if len(crossed):
            return min(entry + 1 + int(crossed[0]) + 1, end)
    return min(end, len(closes) - 1)


def _portfolio(picks: list[int], entry: int, exit_rule: str, close: np.ndarray,
               sma50: np.ndarray, bench: int, sector_etf: int, cost: float,
               max_horizon: int) -> dict | None:
    if not picks or entry + max_horizon >= close.shape[0]:
        return None
    raw, excess_spy, excess_sector = [], [], []
    for ticker in picks:
        exit_at = _exit_index(entry, exit_rule, close[:, ticker], sma50[:, ticker], max_horizon)
        start_value, end_value = close[entry, ticker], close[exit_at, ticker]
        if not np.isfinite(start_value) or start_value <= 0 or not np.isfinite(end_value):
            continue
        stock_return = end_value / start_value - 1
        b0, b1 = close[entry, bench], close[exit_at, bench]
        s0, s1 = close[entry, sector_etf], close[exit_at, sector_etf]
        if not all(np.isfinite([b0, b1, s0, s1])) or b0 <= 0 or s0 <= 0:
            continue
        market_return, sector_return = b1 / b0 - 1, s1 / s0 - 1
        raw.append(stock_return - cost)
        excess_spy.append(stock_return - market_return - cost)
        excess_sector.append(stock_return - sector_return - cost)
    if len(raw) < max(3, len(picks) - 2):
        return None
    return {"raw_return": float(np.mean(raw)), "excess_spy": float(np.mean(excess_spy)),
            "excess_sector": float(np.mean(excess_sector))}


def measure(events: pd.DataFrame, prices: pd.DataFrame, sectors: dict[str, str]) -> pd.DataFrame:
    c = cfg()
    if events is None or events.empty or prices is None or prices.empty:
        return pd.DataFrame()
    benchmark = sources()["benchmark"]
    sector_funds = sector_etfs()
    p = prices.copy()
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    p["date"] = pd.to_datetime(p["date"], errors="coerce")
    px = p.dropna(subset=["close", "date"]).pivot_table(index="date", columns="ticker", values="close",
                                                         aggfunc="last").sort_index()
    if benchmark not in px:
        return pd.DataFrame()
    rets = px.pct_change(fill_method=None)
    px = px.ffill(limit=5)
    tickers = list(px.columns)
    ticker_ix = {ticker: i for i, ticker in enumerate(tickers)}
    if benchmark not in ticker_ix:
        return pd.DataFrame()
    sma_days = int(c["sma_days"])
    close, sma50 = px.to_numpy(dtype=float), px.rolling(sma_days, min_periods=sma_days).mean().to_numpy(dtype=float)
    selectors = list(c["selectors"])
    scores = {
        "mom12": (px.shift(21) / px.shift(252) - 1).to_numpy(dtype=float),
        "mom6": (px.shift(5) / px.shift(126) - 1).to_numpy(dtype=float),
        "high52": (px / px.rolling(252, min_periods=200).max() - 1).to_numpy(dtype=float),
        "lowvol": (-rets.rolling(126, min_periods=100).std()).to_numpy(dtype=float),
    }
    etf_symbols = set(sector_funds.values()) | {benchmark}
    pools: dict[str, list[int]] = {}
    for ticker, sector in sectors.items():
        if ticker in ticker_ix and ticker not in etf_symbols:
            pools.setdefault(sector, []).append(ticker_ix[ticker])
    selector_names = [name for name in selectors if name == "sector" or name in scores]
    rules = [f"fixed{int(h)}" for h in c["horizons"]] + [f"sma{sma_days}"]
    max_horizon = max([int(h) for h in c["horizons"]] + [int(c["sma_max_horizon"])])
    lag, top_n = int(c["entry_lag"]), int(c["top_n"])
    min_price, cost = float(c["min_price"]), float(c["cost_bps"]) / 1e4
    dates = px.index
    confluence_idx = {sector: [int(dates.searchsorted(pd.Timestamp(d))) for d in g["date"]]
                      for sector, g in events.groupby("sector")}

    def select(sector: str, k: int, selector: str, seed: str) -> list[int]:
        pool = pools.get(sector, [])
        if len(pool) < top_n * 3 or k < 252:
            return []
        prior = k - 1
        eligible = [t for t in pool if np.isfinite(close[prior, t]) and close[prior, t] >= min_price]
        if selector == "sector":
            if len(eligible) < top_n * 2:
                return []
            rng = np.random.default_rng(zlib.crc32(seed.encode()))
            return [int(t) for t in rng.permutation(eligible)[:top_n]]
        if len(eligible) < top_n * 2:
            return []
        score = scores[selector][prior, eligible]
        order = np.argsort(np.where(np.isfinite(score), score, -np.inf))[::-1]
        return [int(eligible[i]) for i in order[:top_n] if np.isfinite(score[i])]

    def run_one(sector: str, k: int, selector: str, rule: str, seed: str) -> dict | None:
        if sector not in sector_funds or sector_funds[sector] not in ticker_ix:
            return None
        picks = select(sector, k, selector, seed)
        if not picks:
            return None
        return _portfolio(picks, k + lag, rule, close, sma50, ticker_ix[benchmark],
                          ticker_ix[sector_funds[sector]], cost, max_horizon)

    rows = []
    for event in events.to_dict("records"):
        k = int(dates.searchsorted(pd.Timestamp(event["date"])))
        if k < 253 or k + lag + max_horizon >= len(dates):
            continue
        sector = str(event["sector"])
        blocked = confluence_idx.get(sector, [])
        lo, hi = max(253, k - int(c["placebo_span_days"])), min(len(dates) - lag - max_horizon, k + int(c["placebo_span_days"]))
        candidates = [d for d in range(lo, hi) if all(abs(d - b) > max_horizon for b in blocked)]
        if not candidates:
            continue
        for selector in selector_names:
            for rule in rules:
                seed = f"{event['event_id']}|{selector}"
                real = run_one(sector, k, selector, rule, seed)
                if real is None:
                    continue
                rng = np.random.default_rng(zlib.crc32(seed.encode()))
                placebo = []
                for d in rng.permutation(candidates):
                    result = run_one(sector, int(d), selector, rule, f"{seed}|{d}")
                    if result is not None:
                        placebo.append(result)
                    if len(placebo) >= int(c["placebo_n"]):
                        break
                if len(placebo) < 2:
                    continue
                rows.append({
                    "event_id": event["event_id"], "sector": sector, "date": event["date"],
                    "federal_date": event["federal_date"], "investment_date": event["investment_date"],
                    "key": f"{selector}|{rule}", "selector": selector, "exit_rule": rule,
                    "raw_return": real["raw_return"], "excess_spy": real["excess_spy"],
                    "excess_sector": real["excess_sector"],
                    "placebo_excess_sector": float(np.mean([x["excess_sector"] for x in placebo])),
                    "alpha_sector": real["excess_sector"] - float(np.mean([x["excess_sector"] for x in placebo])),
                    "alpha_spy": real["excess_spy"] - float(np.mean([x["excess_spy"] for x in placebo])),
                    "placebo_n": len(placebo),
                })
    return pd.DataFrame(rows)


def build(returns: pd.DataFrame) -> pd.DataFrame:
    gate = sources()["corp_invest"]["gate"]
    if returns is None or returns.empty:
        return pd.DataFrame(columns=COLS)
    rows = []
    for (selector, rule), group in returns.groupby(["selector", "exit_rule"]):
        x = group.assign(car_0_60=group["alpha_sector"], car_0_5=group["alpha_sector"],
                         car_0_20=group["alpha_sector"])
        stats = I._stats(x, gate)
        by_date = group.groupby("date").mean(numeric_only=True)
        row = {"key": f"{selector}|{rule}", "selector": selector, "exit_rule": rule, **stats,
               "mean_alpha_sector": float(by_date["alpha_sector"].mean()),
               "mean_excess_spy": float(by_date["excess_spy"].mean()),
               "mean_raw": float(by_date["raw_return"].mean()),
               "raw_hit": float((by_date["raw_return"] > 0).mean())}
        rows.append(row)
    evidence = pd.DataFrame(rows)
    evidence["q"] = E._bh(evidence["p"])
    evidence["status"] = [I.classify(r, gate) for r in evidence.to_dict("records")]
    raw_ok = ((evidence["mean_raw"] >= gate["min_mean_promising"])
              & (evidence["raw_hit"] >= gate["hit_promising"]) & (evidence["mean_excess_spy"] > 0))
    evidence.loc[evidence["status"].isin(["proven", "promising"]) & ~raw_ok, "status"] = "unclear"
    evidence["status_name"] = evidence["status"].map(I.STATUS_NAMES)
    evidence["icon"] = evidence["status"].map(I.ICONS)
    evidence["explain"] = [
        (f"{r['n']} olay günü; seçilen hisseler sektör ETF'ine ve yerel plaseboya göre "
         f"{r['mean_alpha_sector'] * 100:+.1f}%, SPY'a göre {r['mean_excess_spy'] * 100:+.1f}%, "
         f"net ham getiri {r['mean_raw'] * 100:+.1f}%.")
        if r["n"] else "Veri yok." for r in evidence.to_dict("records")]
    evidence["asof"] = date.today().isoformat()
    return evidence.reindex(columns=COLS).sort_values(["q", "p"], na_position="last").reset_index(drop=True)


def refresh(prices: pd.DataFrame | None = None) -> pd.DataFrame:
    if not cfg().get("enabled", True):
        return store.read("sector_confluence_evidence")
    sectors = BK.sector_map()
    events = pair_events(collect_events(sectors), int(cfg()["pair_days"]), int(cfg()["cooldown_days"]))
    store.write("sector_confluence_events", events)
    prices = store.read("prices") if prices is None else prices
    returns = measure(events, prices, sectors)
    store.write("sector_confluence_returns", returns)
    evidence = build(returns)
    store.write("sector_confluence_evidence", evidence)
    log.info("Teşvik + yatırım kesişimi: %d eşleşme, %d strateji, %d yerel aday",
             len(events), len(evidence), int(evidence["status"].isin(["proven", "promising"]).sum()) if len(evidence) else 0)
    return evidence
