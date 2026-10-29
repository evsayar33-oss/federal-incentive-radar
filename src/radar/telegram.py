"""Telegram notices for proven signals and accurate system-health diagnostics.

This is a complete replacement for src/radar/telegram.py. Signal/weekly
notification behavior is preserved; system-health notifications distinguish a
fatal failed run from a degraded run and ordinary data-quality review warnings.
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
    except Exception as exc:  # Do not put the Telegram token in the log.
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
             f"🎯 <b>Etkilenebilecek hisseler:</b> {e(r['affected'])} <i>({e(r['role_name'])})</i>"]
    if r.get("sector"):
        lines.append(f"🏭 <b>Sektör:</b> {e(str(r['sector']))}")
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
    need = cfg().get("live_confirm_n", E.cfg()["live_min_n"])
    ln = int(r.get("live_n") or 0) if np.isfinite(r.get("live_n", 0) or 0) else 0
    if ln < need:
        lines.append(f"📜 <i>Geçmiş veriyle kanıtlandı. Canlı doğrulama sürüyor ({ln}/{need} sinyal).</i>")
    elif np.isfinite(r.get("live_hit", np.nan)):
        lines.append(f"✅ <i>Canlı doğrulandı: son {ln} sinyalde isabet %{r['live_hit'] * 100:.0f}.</i>")
    amt = pd.to_numeric(r.get("amount_usd"), errors="coerce")
    if pd.notna(amt) and amt > 0:
        lines.append(f"💵 Tutar: {amt / 1e6:,.0f} M$")
    if r.get("url"):
        lines.append(f'🔗 <a href="{e(str(r["url"]))}">Kaynak</a>')
    lines.append("<i>Karar desteğidir, yatırım tavsiyesi değildir.</i>")
    return "\n".join(lines)


def notify(signals: pd.DataFrame, new_ids: set[str], run_id: str) -> int:
    """Send only new, evidence-backed events and persist successfully sent notifications."""
    c = cfg()
    if signals is None or signals.empty or not new_ids:
        return 0
    allowed = set(c.get("notify_statuses", ["proven"])) & {"proven", "promising"}
    fresh = (date.today() - timedelta(days=c.get("max_event_age_days", 3))).isoformat()
    s = signals[signals["event_id"].astype(str).isin(new_ids) & signals["status"].isin(allowed)
                & (signals["date"].astype(str) >= fresh)]
    if "link_ok" in s.columns:
        s = s[s["link_ok"].astype(str).str.lower() != "false"]
    if c.get("min_abs_effect"):
        s = s[pd.to_numeric(s["effect"], errors="coerce").abs() >= c["min_abs_effect"]]
    old = store.read("notifications")
    if not old.empty:
        s = s[~s["event_id"].astype(str).isin(set(old["event_id"].astype(str)))]
    if s.empty:
        return 0
    s = s.sort_values(["rank", "n_clusters"], ascending=False)
    limit = int(c.get("max_per_run", 8))
    top, rest = s.head(limit), s.iloc[limit:]
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
    """Weekly summary of evidence-backed event types and signal calibration."""
    if not enabled() or not cfg().get("weekly_summary", True):
        return False
    lines = ["<b>📡 Haftalık rapor: etki yaratan haber türleri</b>"]
    if ev is not None and not ev.empty:
        top = ev[ev["status"].isin(["proven", "promising"]) & ev["level"].isin(["program_kind", "kind"])].head(8)
        if top.empty:
            lines.append("Bu hafta etkisi kanıtlanan haber türü yok. Sistem veri topluyor.")
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


def _one_line(value: object, limit: int = 210) -> str:
    raw = str(value or "").strip()
    parts = [part.strip() for part in raw.splitlines() if part.strip()]
    if len(parts) > 1:
        # Traceback examples should preserve both context and the final exception line.
        raw = parts[0] + " … " + parts[-1]
    text = " ".join(raw.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _component_status(status: dict) -> tuple[list[str], list[str], list[str]]:
    """Return failed components, fallback/degraded components, and healthy components."""
    failed, fallback, good = [], [], []
    for key, value in status.items():
        if not isinstance(value, dict) or "ok" not in value:
            continue
        label = str(key)
        if value.get("ok") is False:
            reason = value.get("error") or value.get("reason") or "hata nedeni raporlanmadı"
            failed.append(f"{label}: {_one_line(reason, 260)}")
        elif value.get("via") == "yedek":
            why = value.get("primary_error") or "birincil kaynak başarısız/boş yanıt"
            fallback.append(f"{label}: yedek kaynak kullanıldı ({_one_line(why, 160)})")
        else:
            suffix = f" ({value['rows']})" if "rows" in value else ""
            good.append(f"{label}{suffix}")
    return failed, fallback, good


def health_message(title: str, status: dict, ok: bool) -> str:
    """Create a truthful report: red means run failure; yellow means degraded/review."""
    e = html.escape
    failed, fallbacks, good = _component_status(status)
    quality = [str(w) for w in (status.get("quality") or []) if str(w).strip()]
    fatal = str(status.get("fatal") or "").strip()
    diag = status.get("_diagnostics") if isinstance(status.get("_diagnostics"), dict) else {}
    log_errors = int(diag.get("error_count", 0) or 0)
    log_warnings = int(diag.get("warning_count", 0) or 0)

    if not ok or fatal:
        heading = "🔴 <b>Çalışma başarısız</b>"
    elif failed:
        heading = "🟠 <b>Kısmi kesinti</b>"
    elif fallbacks:
        heading = "🟡 <b>Yedek kaynak devrede</b>"
    elif quality or log_errors:
        heading = "🟡 <b>İnceleme gerekiyor</b>"
    elif log_warnings:
        heading = "🟡 <b>Uyarı kaydı var</b>"
    else:
        heading = "🟢 <b>Sistem sağlıklı</b>"

    lines = [f"🩺 {heading} — {e(title)}"]
    if "events_new" in status:
        lines.append(f"📰 Yeni olay: {status['events_new']} · toplam: {status.get('events_total', '-')}")

    if good:
        lines.append("✅ Başarılı bileşenler: " + e(", ".join(good)[:800]))
    if failed:
        lines.append("❌ Başarısız bileşenler:")
        lines.extend(f"• {e(item)}" for item in failed[:6])
    if fallbacks:
        lines.append("♻️ Yedek kaynak uyarıları:")
        lines.extend(f"• {e(item)}" for item in fallbacks[:4])
    if fatal:
        lines.append("❌ <b>Hata izi (fatal):</b> " + e(_one_line(fatal.splitlines()[-1], 240)))
        # Preserve the final traceback lines, where the originating exception usually appears.
        tail = "\n".join(fatal.splitlines()[-5:])
        if tail and tail != fatal.splitlines()[-1]:
            lines.append("<code>" + e(_one_line(tail, 900)) + "</code>")

    if quality:
        lines.append("⚠️ <b>Veri kalitesi / manuel inceleme:</b>")
        lines.extend(f"• {e(_one_line(item, 280))}" for item in quality[:5])
    if diag:
        total_seen = int(diag.get("total_seen", diag.get("captured", 0)) or 0)
        kept = int(diag.get("records_kept", diag.get("captured", 0)) or 0)
        dropped = int(diag.get("records_dropped", max(0, total_seen - kept)) or 0)
        suffix = f"; en yeni {kept} kayıt saklandı" if dropped else f"; {kept} kayıt incelendi"
        lines.append(f"🧾 Günlük kayıtları: {diag.get('error_count', 0)} hata, "
                     f"{diag.get('warning_count', 0)} uyarı; toplam görülen {total_seen}{suffix}.")
        examples = diag.get("groups") or []
        shown = 0
        for item in examples:
            level = str(item.get("level", ""))
            if level not in ("CRITICAL", "ERROR", "WARNING"):
                continue
            # A few distinct examples, prioritized by error level.
            logger_name = _one_line(item.get("logger", "logger"), 60)
            message = _one_line(item.get("example", ""), 220)
            count = int(item.get("count", 1) or 1)
            lines.append(f"• <code>{e(level)} · {e(logger_name)}" + (f" ×{count}" if count > 1 else "")
                         + f"</code>: {e(message)}")
            shown += 1
            if shown >= 3:
                break
    if failed or fallbacks or fatal or quality or log_errors or log_warnings:
        lines.append("🔎 Ayrıntılı teşhis: GitHub → Actions çalışma kaydı veya data dalındaki last_run.json (log_diagnostics / warnings alanları).")
    return "\n".join(lines)[:3900]


def health(title: str, status: dict, ok: bool) -> bool:
    """Send all problem reports; send clean health messages only for configured slots."""
    c = cfg()
    if not enabled() or not c.get("health_messages", True):
        return False
    failed, fallbacks, _ = _component_status(status)
    diag = status.get("_diagnostics") if isinstance(status.get("_diagnostics"), dict) else {}
    bad = (not ok) or bool(status.get("fatal")) or bool(failed) or bool(fallbacks) or bool(status.get("quality")) \
        or int(diag.get("error_count", 0) or 0) > 0 or int(diag.get("warning_count", 0) or 0) > 0
    if not bad and not any(s in title for s in c.get("health_ok_slots", ["close", "weekly"])):
        return False
    return send(health_message(title, status, ok))


def test() -> None:
    """Check Telegram configuration and send a test message."""
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
