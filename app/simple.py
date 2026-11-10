"""Basit görünüm: yeni başlayan biri için yalnızca işe yarayan bilgiler.

İlke: her ekran tek bir soruya cevap verir.
  🔔 Bugün            → "Şu an dikkat etmem gereken haber var mı?"
  ✅ İşe yarayan       → "Hangi haber türü hisseleri gerçekten etkiliyor?"
  🏭 Büyük yatırımlar → "Büyük şirket yatırım haberleri hissede kanıtlanmış getiri sağlıyor mu?"
  🔎 Hisse sorgula     → "Benim hissem teşvik haberlerinden nasıl etkilenmiş?"
  🤖 Telegram          → "Bot çalışıyor mu, ne kadar isabetli?"
  ❓ Nasıl okunur?     → kısa sözlük
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import data as D

LEGEND = ("🟢 **Kanıtlandı**: geçmişte bu tür haber hisseleri belirgin ve tekrarlanabilir biçimde etkiledi.  \n"
          "🟡 **Umut verici**: etki görülüyor ama kanıt için daha çok örnek gerekiyor.  \n"
          "⚫ **Etkisiz**: çok örnek var, etki yok. Bu haberleri önemsemeyebilirsiniz.  \n"
          "⚪ **Belirsiz / veri birikiyor**: henüz hüküm yok.  \n"
          "🟠 **Askıda**: geçmişte kanıtlıydı ama son canlı sinyaller tutmadı.")
RESEARCH_NAMES = {"confirmed": "Doğrulandı", "candidate": "Aday · yeni veri", "filtered": "Çoklu testte elendi",
                  "failed": "Doğrulama başarısız", "no_effect": "Etkisiz", "collecting": "Veri birikiyor",
                  "unclear": "Belirsiz", "suspended": "Askıda"}


def _research_family(T, family: str) -> pd.DataFrame:
    registry = T("research_registry")
    return registry[registry["family"] == family] if not registry.empty else pd.DataFrame()


def _card(r: dict) -> None:
    with st.container(border=True):
        a, b = st.columns([1, 9])
        a.markdown(f"<div style='font-size:2rem;text-align:center'>{r['icon']}</div>", unsafe_allow_html=True)
        title = str(r["title"])[:180]
        b.markdown(f"**[{title}]({r['url']})**" if r.get("url") else f"**{title}**")
        b.caption(f"{r['date']} · {r.get('program_name') or r['program']} · {r['kind_name']} · {r['status_name']}")
        if r.get("affected"):
            b.markdown(f"🎯 **Etkilenebilecek hisseler:** `{r['affected']}` — {r['role_name']}")
        if r.get("sector"):
            b.markdown(f"🏭 **Sektör:** {r['sector']}")
        b.markdown(r["explain"])
        if r.get("detail"):
            with b.expander("Bu ne demek? (basit anlatım)"):
                st.markdown(r["detail"])


def _today(T, sig: pd.DataFrame) -> None:
    st.subheader("Şu an dikkat edilmesi gereken haberler")
    with st.expander("Renkler ne anlama geliyor?"):
        st.markdown(LEGEND)
    c1, c2 = st.columns([2, 1])
    days = c1.select_slider("Dönem", options=[3, 7, 14, 30], value=14, format_func=lambda d: f"Son {d} gün")
    show_all = c2.toggle("Kanıtsız haberleri de göster", value=False)
    feed = D.signal_feed(sig, days=days, only_strong=not show_all)
    registry = T("research_registry")
    confirmed = set(registry.loc[(registry["family"] == "evidence") & (registry["status"] == "confirmed"), "key"].astype(str)) \
        if not registry.empty else set()
    if "evidence_key" in feed:
        feed = feed[feed["evidence_key"].astype(str).isin(confirmed)]
    else:
        feed = feed.iloc[0:0]
    if feed.empty:
        st.info("Bu dönemde **kanıtlı veya umut verici** bir haber yok. Bu normaldir: sistem bir haber türünü ancak "
                "yeterli örnek birikip ileriye dönük testi de geçince öne çıkarır. Yeni sinyal olduğunda Telegram "
                "botu size haber verir.")
        return
    st.caption(f"{len(feed)} haber · en güçlü kanıt en üstte")
    for r in feed.head(40).to_dict("records"):
        _card(r)


def _invest(T) -> None:
    ev, sig = T("invest_evidence"), T("invest_signals")
    registry = T("research_registry")
    st.subheader("Büyük şirket yatırımları")
    st.caption("Büyük şirketlerin (1 milyar $ ve üstü) yatırım duyuruları. Sonuçlar tüm araştırma kollarıyla birlikte "
               "çoklu test düzeltmesinden geçer; yerel başarı yetmez. Yalnızca ortak kapıyı ve keşif sonrası ileri "
               "doğrulamayı geçen hipotez Telegram sinyali olabilir.")
    if ev.empty:
        st.info("Henüz kanıt oluşmadı. Geçmiş veri yüklemesi ve fiyatlar sonrası ilk kapanış/haftalık çalışmada oluşur.")
        return
    final = {}
    if not registry.empty:
        rows = registry[registry["family"] == "invest_evidence"]
        final = dict(zip(rows["key"].astype(str), rows["status"]))
    global_names = {"confirmed": "Ortak testten geçti", "candidate": "Aday · yeni veri bekleniyor",
                    "filtered": "Çoklu test düzeltmesinde elendi", "failed": "İleri doğrulama başarısız"}
    show = pd.DataFrame({
        "Ortak araştırma durumu": [global_names.get(final.get(str(k), ""), "Henüz doğrulanmadı")
                                   for k in ev["category"]],
        "Yatırım türü": ev["category_name"],
        "Haber sayısı": ev["n"].astype(int),
        "60 gün ort. getiri": ev["mean_car_60"].map(D.pct),
        "Pozitif oran": ev["hit_60"].map(lambda v: D.pct(v, signed=False)),
        "İleriye dönük isabet": [f"{D.pct(h, signed=False)} ({int(n)})" if n and n == n else "-"
                                 for h, n in zip(ev["oos_hit"], ev["oos_n"])],
        "Genel q": [float(registry[(registry["family"] == "invest_evidence") &
                                    (registry["key"].astype(str) == str(k))]["global_q"].iloc[0])
                    if not registry.empty and ((registry["family"] == "invest_evidence") &
                                               (registry["key"].astype(str) == str(k))).any() else float("nan")
                    for k in ev["category"]],
        "Açıklama": ev["explain"]})
    st.dataframe(show, hide_index=True)
    if not any(v == "confirmed" for v in final.values()):
        st.info("Henüz hiçbir yatırım türü kanıt eşiğini geçmedi; bu bir hata değil. Sistem örnek topluyor ve "
                "yalnızca kanıtlanan türler Telegram'a bildirilir.")
    if not registry.empty:
        with st.expander("🔬 Otomatik araştırma kayıt defteri"):
            st.caption("Tüm araştırma kolları tek Benjamini–Hochberg ailesinde düzeltilir. Yerel testte iyi görünen "
                       "sonuçlar, ortak düzeltmeyi ve gerekli ileri doğrulamayı geçmeden kanıt/sinyal sayılmaz.")
            label = {"evidence": "Federal haber", "invest_evidence": "Büyük yatırım",
                     "invest_subgroups": "Yatırım alt grubu", "insider_evidence": "İçeriden alım",
                     "insider_subgroups": "İçeriden alım alt grubu", "basket_evidence": "Büyüme sepeti",
                     "trade_evidence": "Kırılım stratejisi"}
            states = {"confirmed": "Doğrulandı", "candidate": "Aday · yeni veri", "filtered": "Çoklu testte elendi",
                      "failed": "Doğrulama başarısız", "no_effect": "Etkisiz", "collecting": "Veri birikiyor",
                      "unclear": "Belirsiz", "suspended": "Askıda"}
            top = registry.sort_values("global_q").head(40)
            st.dataframe(pd.DataFrame({
                "Araştırma kolu": top["family"].map(label).fillna(top["family"]),
                "Hipotez": top["key"], "Sonuç": top["status"].map(states).fillna(top["status"]),
                "Olay": top["n"].astype(int), "Etki": top["effect"].map(D.pct),
                "Genel q": top["global_q"].round(3),
                "Yeni olay": top["confirm_n"].astype(int),
            }), hide_index=True)
    sub = T("invest_subgroups")
    if not sub.empty:
        final_sub = _research_family(T, "invest_subgroups")
        final_map = dict(zip(final_sub["key"].astype(str), final_sub["status"])) if not final_sub.empty else {}
        q_map = dict(zip(final_sub["key"].astype(str), final_sub["global_q"])) if not final_sub.empty else {}
        with st.expander("🔬 Keşif: büyüklük, kısa ufuk ve düşüş yönü (yalnızca bilgi)"):
            st.caption("Ana test sonuç vermediği için alt gruplar da denendi. Çok test yapıldığından hepsi birlikte "
                       "düzeltilir, işlem maliyeti düşülür. Kapıyı geçen grup ancak 🟡 aday olur; keşiften sonraki yeni "
                       "olaylarda da tutarsa 🟢 doğrulanır. 'kısa' = hisse düşüyor (kaçınma sinyali).")
            top = sub.sort_values("q").head(15)
            st.dataframe(pd.DataFrame({
                "Ortak durum": [RESEARCH_NAMES.get(final_map.get(str(k), ""), "Henüz ortak doğrulama yok") for k in top["key"]],
                "Grup": top["group_name"], "Ufuk": top["horizon"],
                "Yön": top["direction"], "Haber": top["n"].astype(int), "Ort. (maliyetli)": top["mean"].map(D.pct),
                "İsabet": top["hit"].map(lambda v: D.pct(v, signed=False)), "Yerel q": top["q"].round(3),
                "Genel q": [q_map.get(str(k), float("nan")) for k in top["key"]]}), hide_index=True)
    _insider(T)
    _basket(T)
    _trade(T)
    if not sig.empty:
        st.markdown("#### Son yatırım haberleri")
        for r in sig.head(25).to_dict("records"):
            with st.container(border=True):
                st.markdown(f"{r['icon']} **[{str(r['title'])[:170]}]({r['url']})**" if r.get("url")
                            else f"{r['icon']} **{str(r['title'])[:170]}**")
                st.caption(f"{r['date']} · {r['category_name']} · `{r['ticker']}` · {D.fmt_usd(r.get('amount_usd'))} · {r['status_name']}")


def _insider(T) -> None:
    ev = T("insider_evidence")
    final = _research_family(T, "insider_evidence")
    status_map = dict(zip(final["key"].astype(str), final["status"])) if not final.empty else {}
    q_map = dict(zip(final["key"].astype(str), final["global_q"])) if not final.empty else {}
    st.markdown("#### 🕵️ İçeriden alımlar (SEC Form 4)")
    st.caption("Yöneticilerin kendi paralarıyla açık piyasadan hisse alması. Aynı kanıt kapıları uygulanır; veri SEC'te "
               "üç ayda bir yayınlandığı için canlı sinyal gecikmelidir.")
    if ev.empty:
        st.info("Henüz içeriden alım kanıtı yok. Geçmiş veri çalışması sonrası oluşur.")
        return
    st.dataframe(pd.DataFrame({
        "Ortak durum": [RESEARCH_NAMES.get(status_map.get(str(k), ""), "Henüz ortak doğrulama yok") for k in ev["category"]],
        "Tür": ev["category_name"], "Olay": ev["n"].astype(int),
        "60 gün ort. getiri": ev["mean_car_60"].map(D.pct),
        "Pozitif oran": ev["hit_60"].map(lambda v: D.pct(v, signed=False)),
        "Genel q": [q_map.get(str(k), float("nan")) for k in ev["category"]], "Açıklama": ev["explain"]}),
        hide_index=True)


def _basket(T) -> None:
    ev = T("basket_evidence")
    final = _research_family(T, "basket_evidence")
    status_map = dict(zip(final["key"].astype(str), final["status"])) if not final.empty else {}
    q_map = dict(zip(final["key"].astype(str), final["global_q"])) if not final.empty else {}
    st.markdown("#### 🌱 Büyüme sepeti testi")
    st.caption("Haberin olduğu sektörde, olay öncesi verilerle seçilen en güçlü 5 büyüme hissesi (12-1 ay ivme) haberden "
               "sonra, aynı sektörde haber olmayan günlere göre 60 günde ne yaptı? Tüm kanıt kapıları uygulanır.")
    if ev.empty:
        st.info("Henüz sonuç yok. Geçmiş veri çalışması sonrası oluşur.")
        return
    st.dataframe(pd.DataFrame({
        "Ortak durum": [RESEARCH_NAMES.get(status_map.get(str(k), ""), "Henüz ortak doğrulama yok") for k in ev["key"]],
        "Grup": ev["group_name"], "Olay": ev["n"].astype(int),
        "60 gün fark": ev["mean_car_60"].map(D.pct), "Pozitif oran": ev["hit_60"].map(lambda v: D.pct(v, signed=False)),
        "Yerel q": ev["q"].round(3), "Genel q": [q_map.get(str(k), float("nan")) for k in ev["key"]]}), hide_index=True)


def _trade(T) -> None:
    ev, pl = T("trade_evidence"), T("trade_plans")
    final = _research_family(T, "trade_evidence")
    status_map = dict(zip(final["key"].astype(str), final["status"])) if not final.empty else {}
    q_map = dict(zip(final["key"].astype(str), final["global_q"])) if not final.empty else {}
    st.markdown("#### 📈 Uzun vadeli kırılım stratejisi")
    st.caption("52 haftalık zirve kırılımı, 60/120 gün tutma. Ortak çoklu test ve keşif sonrası doğrulama geçilmeden "
               "işlem planı üretilmez. Plan yalnızca bilgidir, otomatik emir yoktur; yatırım tavsiyesi değildir.")
    if ev.empty:
        st.info("Henüz sonuç yok. Geçmiş veri çalışması sonrası oluşur.")
        return
    st.dataframe(pd.DataFrame({
        "Ortak durum": [RESEARCH_NAMES.get(status_map.get(str(k), ""), "Henüz ortak doğrulama yok") for k in ev["key"]],
        "Grup": ev["group_name"], "Gün": ev["horizon"].astype(int),
        "Olay": ev["n"].astype(int), "Kontrol-ayarlı": ev["mean_car_60"].map(D.pct), "Piyasaya göre ham": ev["raw_mean"].map(D.pct),
        "MC kötü senaryo (%5)": ev["mc_p05"].map(D.pct), "MC en kötü düşüş (%95)": ev["mc_dd95"].map(lambda v: D.pct(v, signed=False)),
        "Yerel q": ev["q"].round(3), "Genel q": [q_map.get(str(k), float("nan")) for k in ev["key"]]}), hide_index=True)
    if pl.empty:
        st.info("Kanıtlanmış kural olmadığı için işlem planı yok. Bu bir hata değil.")
    else:
        st.dataframe(pl, hide_index=True)


def _works(T) -> None:
    ev = T("evidence")
    st.subheader("Hangi haber türü hisseleri gerçekten etkiliyor?")
    st.caption("Her satır bir haber türüdür. Sistem bu türün geçmişteki tüm örneklerinde, haberden sonraki 5 işlem "
               "gününde hissenin piyasaya (SPY) göre ne yaptığını ölçer.")
    if ev.empty:
        st.info("Kanıt tablosu henüz oluşmadı. Fiyat verisi geldikten sonra ilk kapanış veya haftalık çalışmada oluşur.")
        return
    registry = T("research_registry")
    confirmed = set(registry.loc[(registry["family"] == "evidence") & (registry["status"] == "confirmed"), "key"].astype(str)) \
        if not registry.empty else set()
    shown = ev.copy()
    local_candidates = shown["status"].isin(["proven", "promising", "suspended"])
    rejected = local_candidates & ~shown["key"].astype(str).isin(confirmed)
    shown.loc[rejected, "status"] = "unclear"
    shown.loc[rejected, "status_name"] = "Belirsiz · ortak doğrulama geçmedi"
    shown.loc[rejected, "icon"] = "⚪"
    good = D.evidence_table(shown, ["proven", "promising", "suspended"])
    st.markdown("#### 🟢🟡 Etkisi görülen haber türleri")
    if good.empty:
        st.info("Henüz hiçbir haber türü kanıt eşiğini geçmedi. Sistem her gün yeni örnek topluyor.")
    else:
        st.dataframe(good, hide_index=True)
    bad = D.evidence_table(shown, ["no_effect"])
    st.markdown("#### ⚫ Etkisiz olduğu görülen haber türleri (önemsemeyebilirsiniz)")
    if bad.empty:
        st.caption("Henüz yok.")
    else:
        st.dataframe(bad.drop(columns=["İleriye dönük isabet"]), hide_index=True)
    with st.expander("⚪ Henüz hüküm verilemeyen türler"):
        st.dataframe(D.evidence_table(shown, ["unclear", "collecting"]), hide_index=True)
    qi = T("quality_issues")
    if not qi.empty:
        with st.expander(f"🛠️ Sistemin otomatik düzelttiği hatalar ({len(qi)})"):
            st.caption("Sistem yanlış programa düşen haberleri, akademik/idari belgeleri ve yanlış şirket "
                       "eşleşmelerini kendisi bulur ve düzeltir. Siz Etiketleme sekmesinden farklı karar verirseniz "
                       "sizin kararınız geçerli olur.")
            st.dataframe(qi.rename(columns={"title": "Kayıt", "old": "Eski", "new": "Yeni", "reason": "Neden"})[
                ["Kayıt", "Eski", "Yeni", "Neden"]].tail(200), hide_index=True)
    with st.expander("Bir haber türü nasıl 'kanıtlandı' sayılır?"):
        st.markdown(
            "Bir tür ancak **tüm** şartları sağlarsa 🟢 olur:\n"
            "1. **Yeterli örnek:** en az 15 farklı olay günü.\n"
            "2. **Şans değil:** çoklu test düzeltmesinden (Benjamini–Hochberg) sonra da anlamlı (q ≤ 0,10).\n"
            "3. **Anlamlı büyüklük:** ortalama etki en az %0,5.\n"
            "4. **Önceden bilinebilir:** sistem her olayı yalnızca *o tarihten önceki* verilerle tahmin etti ve "
            "en az %55 isabet tuttu (ileriye dönük test).\n"
            "5. **Canlıda tutuyor:** Telegram'a gönderilen sinyaller sonradan ölçülür. İsabet %45'in altına düşerse "
            "tür askıya alınır (🟠).\n\n"
            "Az örnekli türlerin etkisi, benzer türlerin ortalamasına doğru küçültülür (empirical Bayes). "
            "Bu yüzden tablodaki etki, ham ortalamadan daha temkinlidir.")


def _ticker(T) -> None:
    st.subheader("Hisse sorgula")
    t = st.text_input("Ticker yazın (ör. INTC, NEE, MP)", max_chars=10).strip().upper()
    if not t:
        exp = T("company_exposure")
        if not exp.empty:
            st.caption("Teşvik haberlerine en çok maruz kalan şirketler:")
            cols = [c for c in ["ticker", "name", "programs", "n_awards", "n_filings", "last_date"] if c in exp.columns]
            st.dataframe(exp[cols].head(20), hide_index=True)
        return
    sig = T("event_signals")
    if not sig.empty:
        mine = sig[sig["affected"].astype(str).str.split(",").map(lambda x: t in x)]
        if not mine.empty:
            st.markdown(f"#### Son haberler ({len(mine)})")
            for r in mine.head(10).to_dict("records"):
                _card(r)
    hist = D.ticker_history(T("event_returns"), t)
    if hist.empty:
        st.info(f"{t} için ölçülmüş geçmiş olay yok (fiyat verisi veya eşleşmiş olay bulunamadı).")
        return
    w = "car_0_5"
    h = hist.dropna(subset=[w])
    st.markdown(f"#### {t}: geçmiş teşvik olaylarından sonra 5 günlük piyasa üstü getiri")
    if not h.empty:
        m1, m2, m3 = st.columns(3)
        m1.metric("Olay sayısı", len(h))
        m2.metric("Ortalama", D.pct(h[w].mean()))
        m3.metric("Pozitif olay oranı", D.pct((h[w] > 0).mean(), signed=False))
    view = pd.DataFrame({"Tarih": hist["date"], "Haber türü": hist["kind"], "Program": hist["program"],
                         "Rol": hist["role"], "5 gün": hist[w].map(D.pct),
                         "20 gün": hist.get("car_0_20", pd.Series(dtype=float)).map(D.pct)})
    st.dataframe(view, hide_index=True)


def _telegram(T, ctx: dict) -> None:
    st.subheader("Telegram botu")
    tg = ctx.get("telegram", {})
    if tg.get("enabled"):
        st.success(f"Bot açık. Gönderilen düzeyler: {', '.join(tg.get('notify_statuses', []))}")
    else:
        st.warning("Bot kapalı. `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` GitHub Secrets olarak eklenmeli (aşağıda).")
    nt = T("notifications")
    sc = D.bot_scorecard(nt)
    a, b, c = st.columns(3)
    a.metric("Gönderilen sinyal", sc["sent"])
    b.metric("Sonuçlanan (5 gün doldu)", sc["resolved"])
    c.metric("Bot isabeti", D.pct(sc["hit"], signed=False) if sc["hit"] is not None else "bekleniyor",
             help="Sinyal yönü ile gerçekleşen 5 günlük piyasa üstü getirinin yönü aynı mı?")
    if not nt.empty:
        cols = [c for c in ["sent_at", "date", "title", "affected", "direction", "expected_car", "realized_car", "hit"]
                if c in nt.columns]
        st.dataframe(nt.sort_values("sent_at", ascending=False)[cols].head(100), hide_index=True)
    with st.expander("Kurulum (5 dakika)"):
        st.markdown(
            "1. Telegram'da **@BotFather** ile konuşun → `/newbot` → size bir **token** verir.\n"
            "2. GitHub → Settings → Secrets and variables → Actions → **TELEGRAM_BOT_TOKEN** olarak ekleyin.\n"
            "3. Telegram'da yeni botunuza `/start` yazın.\n"
            "4. GitHub → Actions → **Radar** → Run workflow → mod: **telegram-test**. Kayıtta **chat id** görünür.\n"
            "5. Bu sayıyı **TELEGRAM_CHAT_ID** olarak ekleyin. Testi tekrar çalıştırın: botunuz mesaj atar.\n\n"
            "Daha fazla bildirim için `config/sources.yaml` → `telegram.notify_statuses: [\"proven\", \"promising\"]`.")


def _guide() -> None:
    st.subheader("Nasıl okunur? (kısa sözlük)")
    st.markdown(
        "- **Teşvik haberi:** ABD hükümetinin hibe, kredi, vergi kredisi veya sözleşme ile ilgili resmi kaydı "
        "(kanun, kural, fon ilanı, ödül, şirket bildirimi).\n"
        "- **Piyasa üstü getiri (anormal getiri):** Hissenin, genel piyasanın (SPY) hareketinden arındırılmış getirisi. "
        "Piyasa %2 yükselip hisse %5 yükseldiyse, hissenin normal davranışına göre fark hesaplanır.\n"
        "- **5 gün:** Haber gününden itibaren 5 işlem günü.\n"
        "- **Olay sayısı:** Ölçülen farklı haber günü sayısı. Az örnek = zayıf kanıt.\n"
        "- **Aynı yön:** Olayların yüzde kaçı ortalama ile aynı yönde hareket etti.\n"
        "- **İleriye dönük isabet:** Sistem geçmişte, yalnızca o güne kadarki bilgiyle yön tahmin etseydi ne kadar "
        "tutardı? Bu, ezber (aşırı uyum) yapılmadığının kanıtıdır.\n"
        "- **Etkilenen:** *Şirketin kendi hissesi* (haber doğrudan şirketle ilgili), *program sepeti* (bu programdan "
        "daha önce fayda görmüş şirketler) veya *sektör ETF'i*.\n\n"
        + LEGEND +
        "\n\nDaha fazla ayrıntı için sol menüden **🔬 Uzman** görünümüne geçin.\n\n"
        "*Bu araç karar desteği içindir, yatırım tavsiyesi değildir.*")


def render(T, J, ctx: dict) -> None:
    st.title("📡 Federal Teşvik Radarı")
    st.markdown("ABD federal teşvik haberlerini izler. **Yalnızca geçmişte hisseleri gerçekten etkilemiş haber "
                "türlerini** öne çıkarır.")
    ev, sig, nt = T("evidence"), T("event_signals"), T("notifications")
    k1, k2, k3 = st.columns(3)
    registry = T("research_registry")
    confirmed_keys = set(registry.loc[(registry["family"] == "evidence") & (registry["status"] == "confirmed"), "key"].astype(str)) \
        if not registry.empty else set()
    nproven = int((ev["level"].isin(["program_kind", "kind"]) & ev["key"].astype(str).isin(confirmed_keys)).sum()) \
        if not ev.empty else 0
    k1.metric("Kanıtlanmış haber türü", nproven)
    strong = D.signal_feed(sig, days=7) if not sig.empty else sig
    strong = strong[strong["evidence_key"].astype(str).isin(confirmed_keys)] \
        if "evidence_key" in strong else strong.iloc[0:0]
    k2.metric("Son 7 günde güçlü sinyal", len(strong))
    sc = D.bot_scorecard(nt)
    k3.metric("Telegram bot isabeti", D.pct(sc["hit"], signed=False) if sc["hit"] is not None else "bekleniyor")
    tabs = st.tabs(["🔔 Bugün", "✅ İşe yarayan haberler", "🏭 Büyük yatırımlar", "🔎 Hisse sorgula", "🤖 Telegram",
                    "❓ Nasıl okunur?"])
    with tabs[0]:
        _today(T, sig)
    with tabs[1]:
        _works(T)
    with tabs[2]:
        _invest(T)
    with tabs[3]:
        _ticker(T)
    with tabs[4]:
        _telegram(T, ctx)
    with tabs[5]:
        _guide()
