"""Uyarı üretimi ve isteğe bağlı GitHub Issue bildirimi (GitHub mobil uygulaması bildirim gönderir)."""
from __future__ import annotations

import logging

import pandas as pd

from . import http
from .settings import env, priors, sources

log = logging.getLogger("radar.alerts")


def build(new_events: pd.DataFrame, scores: pd.DataFrame, prev_scores: pd.DataFrame, run_id: str) -> pd.DataFrame:
    cfg = sources()["alerts"]
    names = priors()["stage_names"]
    rows = []
    if new_events is not None and not new_events.empty:
        ne = new_events[new_events["relevant"].astype(str).str.lower().isin(["true", "1"])]
        for _, e in ne.iterrows():
            st = int(e["stage"])
            amt = pd.to_numeric(e.get("amount_usd"), errors="coerce")
            tick = "" if pd.isna(e.get("tickers")) else str(e.get("tickers")).strip()
            reason = None
            if st >= 5 and pd.notna(amt) and amt >= cfg["min_award_usd"]:
                reason = f"Büyük ödül: {amt/1e6:,.0f} M$"
            elif st == 7 and tick:
                reason = f"Şirket bildirimi ({tick})"
            elif st == 2:
                reason = "Kanun / kararname"
            elif st == 3 and float(e.get("relevance_prob", e.get("confidence", 0)) or 0) >= 0.7:
                reason = "Yeni kural / rehber"
            elif st == 1 and '"passed_chamber"' in str(e.get("extra", "")):
                reason = "Tasarı bir meclisten geçti"
            if reason:
                rows.append({"run_id": run_id, "type": "event", "reason": reason, "stage": names[str(st)],
                             "program": e.get("program_name", ""), "title": e["title"], "url": e["url"],
                             "tickers": tick, "date": e["date"]})
    if scores is not None and not scores.empty and prev_scores is not None and not prev_scores.empty:
        cur = scores[scores["horizon_days"] == 252].set_index("sector")["score"]
        prev = prev_scores[prev_scores["horizon_days"].astype(int) == 252].set_index("sector")["score"].astype(float)
        for s, v in cur.items():
            if s in prev.index and abs(v - prev[s]) >= cfg["sector_score_jump"]:
                rows.append({"run_id": run_id, "type": "sector", "reason": f"Sektör skoru {prev[s]:.0f} → {v:.0f}",
                             "stage": "", "program": "", "title": s, "url": "", "tickers": "", "date": run_id[:10]})
    return pd.DataFrame(rows)


def notify_github(alerts: pd.DataFrame, run_id: str) -> None:
    token, repo = env("GITHUB_TOKEN"), env("GITHUB_REPOSITORY")
    if alerts is None or alerts.empty or not token or not repo or not sources()["alerts"]["create_github_issue"]:
        return
    lines = [f"- **{r.reason}** — {r.title} {('(' + r.tickers + ')') if r.tickers else ''} {r.url}".strip()
             for r in alerts.head(40).itertuples()]
    body = "Otomatik radar uyarıları:\n\n" + "\n".join(lines)
    try:
        http.request("POST", f"https://api.github.com/repos/{repo}/issues",
                     json={"title": f"Radar uyarısı {run_id} ({len(alerts)})", "body": body, "labels": ["radar-alert"]},
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, retries=1)
    except Exception as exc:
        log.warning("GitHub issue açılamadı: %s", exc)
