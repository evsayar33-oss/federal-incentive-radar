"""H083 research, news and conditional paper-trade plans shown in both UIs."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
import streamlit as st

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from radar import h083_catalog as C


def _pct(value) -> str:
    n=pd.to_numeric(value,errors="coerce")
    return f"%{float(n)*100:+.2f}".replace(".",",") if pd.notna(n) else "—"


def render(T,J) -> None:
    st.header("🔬 H083 | Haber kaynakları, işlem yöntemleri ve ileri doğrulama")
    st.warning(
        "H083'te 34 haber-sonrası işlem kuralı ve 10 hisse rotasyonu denendi. "
        "Hiçbiri SPY'a karşı istatistiksel olarak kanıtlanmış alpha üretmedi. "
        "Radar'da yalnızca düşük-beta ve volatilite sıkışması izleniyor. "
        "Bunlar bugün için AL/SAT sinyali değildir."
    )
    st.caption(
        "Haber, hisse evrenine neden dahil edildiğini açıklar; o haberin getiri "
        "sağladığı anlamına gelmez. 2 izleme modeli, 44 araştırma stilinin tümü değildir."
    )
    status=J("h083_forward_status") or {}
    watch_status=J("event_watch_status") or {}
    gate=T("h083_forward_gates")
    watch=T("event_watch_candidates")
    catalog=T("h083_style_catalog")
    if catalog.empty:
        catalog=C.catalog()
    news=T("h083_news_catalog")
    if news.empty:
        news=C.news_catalog()
    results=T("h083_forward_results")
    plans=T("h083_action_plans")
    cohorts=T("h083_forward_cohorts")
    a,b,c=st.columns(3)
    a.metric("H083 araştırma yöntemi",len(catalog))
    b.metric("Canlı izlenen yöntem",2)
    approved=int(gate["validated_for_info_plans"].astype(str).str.lower().isin(["true","1"]).sum()) if (
        not gate.empty and "validated_for_info_plans" in gate) else 0
    c.metric("İleri kağıt testinde doğrulanan",approved)
    if status:
        st.caption(
            f"Son ayrı ileri izleme çalışması: {status.get('last_complete_nyse_session','-')} "
            f"· Kaynak / sonuç: {status.get('watch_candidates','-')} aday "
            f"· Üretim broker emri: YOK"
        )
    elif watch_status:
        st.caption(f"H083 eski izleme gözlemi: {watch_status.get('feature_asof','-')}. "
                   "Ayrı ileri kağıt takip henüz başlamadı.")
    pages=st.tabs([
        "İşlem tarzları (44)", "Haber aileleri (3)", "Bugünkü izleme",
        "İleriye dönük kâr/zarar karnesi", "Koşullu işlem planları"
    ])
    with pages[0]:
        st.markdown(
            "**34 haber-tetikli işlem stili:** Haber sonrası zamanlı giriş, kırılım, "
            "teyit, RSI geri çekilmesi, momentum, düşük beta, likidite, sektör "
            "gücü ve ATR stop/hedef varyantları. "
            "**10 rotasyon stili:** Haftalık/aylık yeniden dengeleme ve trend/momentum/risk "
            "temelli hisse sıralaması. Yalnızca 2 aylık rotasyon yöntemi canlı izleniyor."
        )
        choice=st.radio("Araştırma katmanı",["Tümü","Haber sonrası","Hisse rotasyonu"],horizontal=True,key="h083_style_filter")
        show=catalog.copy()
        if choice!="Tümü":
            show=show[show.track.eq("HABER SONRASI" if choice=="Haber sonrası" else "HİSSE ROTASYONU")]
        fields=[x for x in ["track","rule","logic","entry","exit","monitoring","certified_alpha"] if x in show]
        st.dataframe(show[fields],hide_index=True,use_container_width=True)
        st.caption(
            "Araştırma yönteminin listelenmesi, ileri kağıt performansına sahip olduğu "
            "veya işlem planı oluşturabileceği anlamına gelmez."
        )
    with pages[1]:
        st.markdown(
            "H083'te haberler **olay-uygun hisse evrenini** oluşturdu; "
            "aynı hissenin hem federal haber alması hem yatırım duyurması "
            "nedensel fiyat etkisi kanıtlamaz. İlk kamuya açıklanma zamanı "
            "bilinmeyen federal işlem tarihleri geçmiş backtest kanıtı sayılmaz."
        )
        fields=[x for x in ["event_family","name","description","source_events","distinct_tickers","status"] if x in news]
        st.dataframe(news[fields],hide_index=True,use_container_width=True)
        st.markdown(
            "**İlişkilendirme yöntemi:** doğrudan şirket–ticker eşleşmesi, ilk gözlem "
            "zaman kalitesi ve kaynak bağlantısı birlikte tutulur. "
            "Şirket yatırımı içindeki fabrika, çip, veri merkezi, enerji, Ar-Ge "
            "alt türleri birbirinden farklıdır; hiçbiri otomatik 'kanıtlı kazanç' değildir."
        )
    with pages[2]:
        st.markdown(
            "**Aylık düşük-beta:** SMA200 üstünde, 63 günlük momentum pozitif, "
            "126 günlük SPY betası 0–1; en düşük beta ilk sırada. "
            "**Aylık volatilite sıkışması:** SMA50 üstünde, ATR14/fiyat "
            "kendi son 126 seansının %20 alt diliminde; en yüksek momentum ilk sırada. "
            "İkisinde de 20 günlük ortalama dolar hacmi ≥5 milyon USD."
        )
        if watch.empty:
            st.info("Henüz geçerli fiyat/kaynak koşullarını sağlayan H083 izleme adayı yayımlanmadı.")
        else:
            cols=[x for x in ["rule","rank","ticker","mom63","beta126","atr14_ratio",
                              "market_regime","event_family","category","event_date",
                              "source_url","feature_asof","status"] if x in watch]
            st.dataframe(watch[cols],hide_index=True,use_container_width=True,
                         column_config={"source_url":st.column_config.LinkColumn("Olay kaynağı")})
        st.caption("Bu liste her tam seansta hesaplanabilir. Aylık kağıt alım kararı "
                   "yalnızca gerçek NYSE ayının son işlem seansında dondurulur; "
                   "ertesi açılışın fiyatı önceden bilinmez.")
    with pages[3]:
        st.markdown(
            "**İleri kayıt geçmiş H083/H084 işlemlerinden bağımsız başlar.** "
            "Her ay ilk NYSE açılışında varsayımsal giriş ve sonraki ayın ilk "
            "açılışında çıkış kaydedilir. Eksik veya geç gözlemlenen açılış fiyatları "
            "sonradan uydurulmaz."
        )
        st.markdown(
            "Doğrulama kapısı: en az **24 ardışık tamamlanmış ay** ve **60 kağıt "
            "pozisyonu**, SPY'dan daha yüksek pozitif net getiri, pozitif "
            "30bp/yön stres getirisi, 2 yöntem üzerinde BH **q≤0,05** ve "
            "3 aylık blok-bootstrap ile pozitif alt güven sınırı."
        )
        if gate.empty:
            st.info("Yeni ileri çalışma henüz yeterli tarih üretmedi; doğrulanmış strateji yok.")
        else:
            show=gate.copy()
            fields=[x for x in ["rule","status","closed_months","paper_positions",
                                "mean_monthly_excess","total_strategy_return",
                                "total_spy_return","stress_total_return",
                                "pretest_p","bh_q","bootstrap_95_lower",
                                "validated_for_info_plans"] if x in show]
            st.dataframe(show[fields],hide_index=True,use_container_width=True)
        if not results.empty:
            with st.expander("Tamamlanan aylık simülasyonlar ve veri hataları"):
                st.dataframe(results.sort_values("entry_month",ascending=False).head(80),hide_index=True)
        if not cohorts.empty:
            with st.expander("Sinyal öncesi dondurulmuş aylık karar kayıtları"):
                st.dataframe(cohorts.sort_values("entry_month",ascending=False).head(50),hide_index=True)
        st.caption(
            "Kağıt işlemler gerçek broker gerçekleşmesi değildir. Sadece ay başı/"
            "sonu açılışlarıyla ölçülen model, H083'ün gerçek portföy NAV'ının "
            "birebir tekrarı değildir. Kurumsal aksiyon/veri sağlayıcı değişimleri de izlenmelidir."
        )
    with pages[4]:
        st.markdown(
            "Sistem işlemleri kendisi seçip taslak planı **yalnızca ileri kağıt testinin "
            "tüm kapıları geçtiği bir kural için** oluşturabilir. Yayımlanacak plan: "
            "hisse, alım/çıkış yönü, hedef NYSE seansı, önceki kapanış referansı, "
            "üst tutar, tahmini adet, olay türü ve kaynak URL."
        )
        if plans.empty or approved==0:
            st.info("Henüz ileriye dönük doğrulanmış H083 kuralı yok. Bu nedenle onaylı AL/SAT planı yok.")
        else:
            cols=[x for x in ["rule","ticker","side","target_session","exit_session",
                              "max_notional_usd","indicative_last_close",
                              "estimated_whole_shares","event_family","source_url",
                              "expected_order_status"] if x in plans]
            st.dataframe(plans[cols].sort_values("target_session",ascending=False).head(100),
                         hide_index=True,use_container_width=True,
                         column_config={"source_url":st.column_config.LinkColumn("Kaynak")})
        st.caption(
            "Varsayımsal hesap: 10.000 USD; 5'e kadar yaklaşık 1.990 USD pozisyon. "
            "Midas maliyeti 1,50 USD alış + 1,50 USD satış; 5 baz puan/yön kayma. "
            "Aylık kurallarda stop/TP tanımlı değildir; exit yeniden dengeleme açılışıdır. "
            "Telegram yalnızca bildirim gönderir, broker emri çalıştırmaz."
        )
