"""Telegram bildirimi: yalnızca etkisi KANITLANMIŞ haber türleri gönderilir.

Kurulum (README → Telegram botu):
  1. Telegram'da @BotFather → /newbot → token alın.            GitHub Secret: TELEGRAM_BOT_TOKEN
  2. Botunuza bir mesaj yazın (ör. /start).
  3. Actions → Radar → Run workflow → mod: telegram-test. Kayıtta chat id görünür. GitHub Secret: TELEGRAM_CHAT_ID
Hangi kanıt düzeyinin gönderileceği config/sources.yaml → telegram.notify_statuses ile seçilir.
"""
from __future__ import annotations

import html
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import evidence as E
from . import http, store
from .settings import env, sources

log = logging.getLogger("radar.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def cfg() -> dict:
    return sources().get("telegram", {})


def credentials() -> tuple[str | None, str | None]:
    return env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")


def enabled() -> bool:
    tok, chat = credentials()
    return bool(cfg().get("enabled", True) and tok and chat)


def send(text: str) -> bool:
    tok, chat = credentials()
    if not (tok and chat):
        return False
    try:
        http.request("POST", API.format(token=tok, method="sendMessage"), retries=2,
                     json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                           "disable_web_page_preview": True})
        return True
    except Exception as exc:  # token log'a yazılmaz
        log.warning("Telegram gönderilemedi: %s", str(exc).split("/bot")[0][:200])
        return False


def _pct(v: float) -> str:
    return f"%{abs(v) * 100:.1f}".replace(".", ",")


def format_signal(r: dict) -> str:
    e = html.escape
    eff = r.get("effect")
    yon = ("📈 YUKARI" if eff > 0 else "📉 AŞAĞI") if eff is not None and np.isfinite(eff) else ""
    head = {"proven": "🟢 KANITLI SİNYAL", "promising": "🟡 UMUT VERİCİ SİNYAL"}.get(r["status"], r["status_name"])
    w = E.cfg()["windows"][E.cfg()["primary_window"]][1]
    lines = [f"<b>{head}</b> {yon}",
             f"<b>{e(str(r.get('program_name') or r['program']))}</b> · {e(r['kind_name'])}",
             e(str(r["title"])[:300]),
             "",
             f"🎯 <b>Etkilenen:</b> {e(r['affected'])} <i>({e(r['role_name'])})</i>"]
    if eff is not None and np.isfinite(eff):
        lines.append(f"📊 <b>Geçmiş:</b> {w} işlem gününde SPY'a göre ortalama {_pct(eff)} "
                     f"{'artış' if eff > 0 else 'düşüş'}")
    det = [f"{r['n_clusters']} olay günü"]
    if np.isfinite(r.get("consistency", np.nan)):
        det.append(f"aynı yön %{r['consistency'] * 100:.0f}")
    if r.get("oos_n"):
        det.append(f"ileriye dönük test isabeti %{r['oos_hit'] * 100:.0f} ({r['oos_n']} tahmin)")
    if np.isfinite(r.get("q", np.nan)):
        det.append(f"q={r['q']:.2f}")
    lines.append("🔬 " + " · ".join(det))
    amt = pd.to_numeric(r.get("amount_usd"), errors="coerce")
    if pd.notna(amt) and amt > 0:
        lines.append(f"💵 Tutar: {amt / 1e6:,.0f} M$")
    if r.get("url"):
        lines.append(f'🔗 <a href="{e(str(r["url"]))}">Kaynak</a>')
    lines.append("<i>Karar desteğidir, yatırım tavsiyesi değildir.</i>")
    return "\n".join(lines)


def notify(signals: pd.DataFrame, new_ids: set[str], run_id: str) -> int:
    """Yeni ve kanıtlı olaylar için Telegram mesajı gönderir. Gönderilenler 'notifications' tablosuna yazılır."""
    c = cfg()
    if signals is None or signals.empty or not new_ids:
        return 0
    allowed = set(c.get("notify_statuses", ["proven"]))
    fresh = (date.today() - timedelta(days=c.get("max_event_age_days", 3))).isoformat()
    s = signals[signals["event_id"].astype(str).isin(new_ids) & signals["status"].isin(allowed)
                & (signals["date"].astype(str) >= fresh)]
    if c.get("min_abs_effect"):
        s = s[pd.to_numeric(s["effect"], errors="coerce").abs() >= c["min_abs_effect"]]
    old = store.read("notifications")
    if not old.empty:
        s = s[~s["event_id"].astype(str).isin(set(old["event_id"].astype(str)))]
    if s.empty:
        return 0
    s = s.sort_values(["rank", "n_clusters"], ascending=False)
    top, rest = s.head(c.get("max_per_run", 8)), s.iloc[c.get("max_per_run", 8):]
    if not enabled():
        log.info("Telegram kapalı (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yok): %d sinyal gönderilmedi.", len(s))
        return 0
    sent = []
    for r in top.to_dict("records"):
        if send(format_signal(r)):
            sent.append(r)
    if not rest.empty:
        body = "\n".join(f"{r['icon']} {html.escape(str(r['title'])[:90])} → {html.escape(r['affected'])}"
                         for r in rest.head(20).to_dict("records"))
        if send(f"<b>+{len(rest)} sinyal daha</b>\n{body}"):
            sent += rest.to_dict("records")
    if sent:
        rows = pd.DataFrame([{"event_id": r["event_id"], "sent_at": run_id, "date": r["date"],
                              "evidence_key": r["evidence_key"], "status": r["status"], "role": r["role"],
                              "affected": r["affected"], "direction": "+" if r["effect"] > 0 else "-",
                              "expected_car": r["effect"], "title": r["title"], "url": r["url"],
                              "realized_car": np.nan, "hit": np.nan} for r in sent])
        store.append("notifications", rows)
    return len(sent)


def weekly_summary(ev: pd.DataFrame) -> bool:
    """Haftalık özet: hangi türler kanıtlandı, bot ne kadar isabetli."""
    if not enabled() or not cfg().get("weekly_summary", True):
        return False
    lines = ["<b>📡 Haftalık kanıt raporu</b>"]
    if ev is not None and not ev.empty:
        vc = ev[ev["level"].isin(["program_kind", "kind"])]["status"].value_counts()
        lines.append(" · ".join(f"{E.STATUS_ICON[k]} {E.STATUS_NAMES[k]}: {int(v)}" for k, v in vc.items()))
        top = ev[ev["status"].isin(["proven", "promising"])].head(8)
        for r in top.to_dict("records"):
            lines.append(f"{E.STATUS_ICON[r['status']]} {html.escape(_key_label(r))}: "
                         f"{'+' if r['post_mean'] > 0 else '−'}{_pct(r['post_mean'])} ({r['n_clusters']} olay)")
    nt = store.read("notifications")
    if not nt.empty and "hit" in nt.columns:
        h = pd.to_numeric(nt["hit"], errors="coerce").dropna()
        if len(h):
            lines.append(f"🤖 Bot karnesi: {len(h)} sinyal sonuçlandı, isabet %{h.mean() * 100:.0f}")
    return send("\n".join(lines))


def _key_label(r: dict) -> str:
    from .settings import programs
    p = programs()["programs"].get(r["program"], {}).get("name", r["program"]) if r["program"] != "*" else "*"
    return f"{E.kind_label(p, r['kind']) if r['kind'] != '*' else p} → {E.ROLE_NAMES.get(r['role'], r['role'])}"


def test() -> None:
    """Kurulum yardımcısı: chat id'yi bulur ve deneme mesajı yollar."""
    tok, chat = credentials()
    if not tok:
        print("TELEGRAM_BOT_TOKEN tanımlı değil. GitHub → Settings → Secrets → Actions bölümüne ekleyin.")
        return
    if not chat:
        try:
            upd = http.get_json(API.format(token=tok, method="getUpdates"), retries=1)
            ids = sorted({str(u.get("message", {}).get("chat", {}).get("id")) for u in upd.get("result", [])
                          if u.get("message")})
            print("Bulunan chat id(ler):", ", ".join(ids) or "yok — önce botunuza Telegram'dan bir mesaj yazın.")
            print("Bu değeri TELEGRAM_CHAT_ID adlı GitHub Secret olarak ekleyin.")
        except Exception:
            print("getUpdates başarısız. Token doğru mu?")
        return
    ok = send("✅ <b>Federal Teşvik Radarı</b> bağlandı.\nYalnızca etkisi kanıtlanmış haber türleri bildirilecek.")
    print("Deneme mesajı gönderildi." if ok else "Gönderilemedi. Token ve chat id'yi kontrol edin.")
