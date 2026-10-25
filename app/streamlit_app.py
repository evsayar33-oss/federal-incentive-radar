"""ABD Federal Teşvik Radarı v2 — Streamlit arayüzü (salt okunur + etiketleme).

Uygulama hiçbir dış API'yi çağırmaz. Yalnızca GitHub Actions'ın 'data' dalına yazdığı
hazır tabloları okur. Bu yüzden ücretsiz katmanda hızlı ve hafif çalışır.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as D  # noqa: E402

st.set_page_config(page_title="Federal Teşvik Radarı v2", page_icon="📡", layout="wide")


def secret(name: str, default: str | None = None) -> str | None:
    try:
        return st.secrets.get(name, os.environ.get(name, default))
    except Exception:
        return os.environ.get(name, default)


BASE = secret("DATA_BASE_URL")  # boşsa yerel ./store okunur


@st.cache_data(ttl=900, show_spinner=False)
def T(name: str) -> pd.DataFrame:
    return D.load_table(name, BASE)


@st.cache_data(ttl=900, show_spinner=False)
def J(name: str) -> dict:
    return D.load_json(name, BASE)


SECTOR_TR = {"Information Technology": "Bilgi Teknolojisi", "Energy": "Enerji", "Industrials": "Sanayi",
             "Materials": "Hammadde", "Utilities": "Kamu Hizmetleri", "Health Care": "Sağlık",
             "Financials": "Finans", "Consumer Discretionary": "İsteğe Bağlı Tüketim",
             "Consumer Staples": "Temel Tüketim", "Communication Services": "İletişim", "Real Estate": "Gayrimenkul"}
ETF = {"Information Technology": "XLK", "Energy": "XLE", "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU",
       "Health Care": "XLV", "Financials": "XLF", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
       "Communication Services": "XLC", "Real Estate": "XLRE"}

ctx = J("context")
events = D.relevant_events(T("events"))
scores = T("scores_latest")

# ---------------------------------------------------------------- başlık ve görünüm seçimi
mode = st.sidebar.radio("Görünüm", ["🟢 Basit", "🔬 Uzman"], index=0,
                        help="Basit: yalnızca işe yarayan bilgiler. Uzman: tüm tablolar ve model ayrıntıları.")
if st.sidebar.button("🔄 Veriyi yenile"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(f"Son çalışma: {ctx.get('run_id', '-')}")
if ctx.get("demo"):
    st.warning("DEMO VERİSİ gösteriliyor. Bu sayılar gerçek değildir. GitHub Actions ilk gerçek çalışmayı yapınca otomatik değişir.")


def simple_view() -> None:
    import simple
    simple.render(T, J, ctx)


def expert_view() -> None:
    st.title("📡 ABD Federal Teşvik Radarı v2 — uzman görünümü")
    st.caption(f"Son çalışma: **{ctx.get('run_id', '-')}** · sürüm {ctx.get('version', '-')} · "
               "Kaynaklar: Federal Register, Congress.gov, USAspending (hibe, kredi, sözleşme), Grants.gov, SEC EDGAR, "
               f"Treasury DTS, FRED, BEA, GDELT/Google News · Yayılma: {ctx.get('spillover', '-')}")
    tabs = st.tabs(["Özet", "Boru hattı", "Sektörler", "Şirketler", "Ödemeler", "Uyarılar", "Model karnesi", "Kanıt motoru", "Etiketleme"])

    # ---------------------------------------------------------------- 1. ÖZET
    with tabs[0]:
        reg = ctx.get("regime", {})
        drift = ctx.get("drift", {}) or {}
        metrics = T("metrics")
        k1, k2, k3, k4 = st.columns(4)
        recent = events[pd.to_datetime(events["date"], errors="coerce") >= pd.Timestamp.today() - pd.Timedelta(days=30)] if not events.empty else events
        k1.metric("İlgili olay (30 gün)", f"{len(recent):,}")
        k2.metric("Ödül tutarı (30 gün)", D.fmt_usd(recent.loc[recent["stage"] == 5, "amount_usd"].sum()) if not recent.empty else "-")
        k3.metric("Mali belirsizlik rejimi", f"×{reg.get('mult', 1):.2f}", help=f"EPUFISCAL z={reg.get('z', '-')}. >1 destekleyici, <1 frenleyici.")
        if not metrics.empty:
            m60 = metrics[metrics["horizon_days"].astype(int) == 60].sort_values("asof").tail(1)
            k4.metric("Model becerisi (60g)", f"{float(m60['skill'].iloc[0]):+.3f}" if len(m60) else "-",
                      help="1 − Brier/Brier_iklim. 0'ın üstü = taban orandan iyi.")
        else:
            k4.metric("Model becerisi (60g)", "veri bekleniyor")
        if drift.get("flag"):
            st.error(f"Drift alarmı: program dağılımı değişti (PSI={drift.get('psi')}). Yükselen: {drift.get('rising_programs')}")

        st.subheader("Sektör etki skorları (0–100, medyan beklenen teşvik akışı)")
        m = D.score_matrix(scores)
        if not m.empty:
            m.index = [SECTOR_TR.get(i, i) for i in m.index]
            fig = px.imshow(m, text_auto=".0f", color_continuous_scale="Blues", aspect="auto", zmin=0, zmax=100)
            fig.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0), coloraxis_showscale=False)
            st.plotly_chart(fig)
            h = st.radio("Belirsizlik bandı ufku", [63, 252, 756], index=1, horizontal=True,
                         format_func=lambda x: D.HORIZON_NAMES[x])
            s = scores[scores["horizon_days"].astype(int) == h].sort_values("p50")
            fig2 = go.Figure(go.Scatter(
                x=s["p50"], y=[SECTOR_TR.get(x, x) for x in s["sector"]], mode="markers",
                error_x=dict(type="data", symmetric=False, array=s["p90"] - s["p50"], arrayminus=s["p50"] - s["p10"]),
                marker=dict(size=10)))
            fig2.update_layout(height=420, xaxis_title="Beklenen teşvik akışı ($, p10–p50–p90)", xaxis_type="log",
                               margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig2)
            st.caption("Skor = log₁₀(medyan akış) ölçeği: 1 M$ → 0, 100 Mr$ → 100. Akış = tutar × aşama olasılığı × zamanlama × "
                       "sektör payı × öğrenilmiş etkinlik × rejim × ödeme hızı. Bant Monte Carlo ile hesaplanır.")
        else:
            st.info("Henüz skor yok. İlk GitHub Actions çalışmasını bekleyin.")

    # ---------------------------------------------------------------- 2. BORU HATTI
    with tabs[1]:
        st.subheader("Teşvik boru hattı: tasarı → kanun → kural → ilan → ödül → ödeme → şirket bildirimi")
        days = st.select_slider("Dönem (gün)", options=[7, 30, 90, 180, 365], value=90)
        f = D.pipeline_funnel(events, days)
        if not f.empty:
            fig = px.bar(f, x="n", y="stage_name", orientation="h", text="n",
                         hover_data={"amount_usd": ":,.0f"}, labels={"n": "Olay", "stage_name": ""})
            fig.update_layout(height=320, yaxis=dict(autorange="reversed"), margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig)
        pp = D.program_pipeline(events, days)
        if not pp.empty:
            st.markdown("**Program × aşama** (her program boru hattının neresinde?)")
            st.dataframe(pp)
        st.markdown("**Olaylar**")
        colf = st.columns(3)
        progs = sorted(events["program_name"].dropna().unique()) if not events.empty else []
        sel_p = colf[0].multiselect("Program", progs)
        sel_s = colf[1].multiselect("Aşama", list(D.STAGE_NAMES.values()))
        q = colf[2].text_input("Ara (başlık / ticker)")
        e = events.copy()
        if sel_p:
            e = e[e["program_name"].isin(sel_p)]
        if sel_s:
            e = e[e["stage_name"].isin(sel_s)]
        if q:
            e = e[e["title"].str.contains(q, case=False, na=False) | e["tickers"].str.contains(q.upper(), na=False)]
        cols = [c for c in ["date", "stage_name", "program_name", "title", "amount_usd", "tickers", "agency", "confidence", "url"] if c in e.columns]
        st.dataframe(e[cols].head(500), hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Kaynak"),
                                    "amount_usd": st.column_config.NumberColumn("Tutar $", format="%.0f"),
                                    "confidence": st.column_config.ProgressColumn("Güven", min_value=0, max_value=1)})

    # ---------------------------------------------------------------- 3. SEKTÖRLER
    with tabs[2]:
        sector = st.selectbox("Sektör", list(SECTOR_TR), format_func=lambda s: f"{SECTOR_TR[s]} ({ETF[s]})")
        if not scores.empty and "indirect_share" in scores.columns:
            row = scores[(scores["sector"] == sector) & (scores["horizon_days"].astype(int) == 252)]
            if len(row):
                m1, m2 = st.columns(2)
                m1.metric("1 yıllık skor", f"{float(row['score'].iloc[0]):.0f}")
                m2.metric("Dolaylı (tedarik zinciri) payı", f"%{100 * float(row['indirect_share'].iloc[0]):.0f}",
                          help="BEA girdi-çıktı tablosuyla hesaplanır. Kapalıysa 0 görünür (BEA_API_KEY gerekir).")
        a, b = st.columns(2)
        prog = T("program_impacts")
        if not prog.empty:
            ps = prog[prog["sector"] == sector].sort_values("expected_usd_1y", ascending=True).tail(12)
            a.markdown("**Programlara göre beklenen akış (1 yıl)**")
            a.plotly_chart(px.bar(ps, x="expected_usd_1y", y="program", orientation="h",
                                  labels={"expected_usd_1y": "$", "program": ""}).update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0)))
        hist = T("scores_history")
        if not hist.empty:
            hh = hist[(hist["sector"] == sector)].copy()
            hh["Ufuk"] = hh["horizon_days"].astype(int).map(D.HORIZON_NAMES)
            hh = hh.groupby(["asof", "Ufuk"], as_index=False)["score"].last()
            b.markdown("**Skor geçmişi**")
            b.plotly_chart(px.line(hh, x="asof", y="score", color="Ufuk").update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0)))
        rel = D.relative_performance(T("prices"), ETF[sector])
        if not rel.empty:
            st.markdown(f"**{ETF[sector]} / SPY göreli performans (1 yıl)** — piyasa teşviki ne kadar fiyatladı?")
            st.line_chart(rel)
        led = T("ledger")
        if not led.empty:
            cur = led[(led["sector"] == sector)].sort_values("created_at").groupby("horizon_days").tail(1)
            if not cur.empty:
                st.markdown("**Güncel tahmin** (SPY'ı geçme olasılığı, kalibre edilmiş)")
                st.dataframe(cur[["created_at", "horizon_days", "z", "p_up"]], hide_index=True)

    # ---------------------------------------------------------------- 4. ŞİRKETLER
    with tabs[3]:
        exp = T("company_exposure")
        st.subheader("Şirket teşvik maruziyeti")
        st.caption("Kaynak: USAspending ödülleri (ad eşleştirme) + SEC bildirimlerinde teşvik geçişleri. "
                   "Beklenen akış = olasılık ve zamanlama ile ağırlıklandırılmış 3 yıllık tutar.")
        if not exp.empty:
            only_sp = st.toggle("Yalnızca S&P 500 üyeleri", value=False)
            x = exp.copy()
            if only_sp and "sp500_weight" in x.columns:
                x = x[pd.to_numeric(x["sp500_weight"], errors="coerce").fillna(0) > 0]
            cols = [c for c in ["ticker", "name", "sector", "exposure_score", "expected_usd_3y", "awards_usd", "n_awards",
                                "n_filings", "programs", "sp500_weight", "r2000_weight", "last_date", "best_match"] if c in x.columns]
            st.dataframe(x[cols].head(300), hide_index=True,
                         column_config={"expected_usd_3y": st.column_config.NumberColumn("Beklenen 3y $", format="%.0f"),
                                        "awards_usd": st.column_config.NumberColumn("Ödüller $", format="%.0f")})
        else:
            st.info("Henüz şirket eşleşmesi yok.")

    # ---------------------------------------------------------------- 5. ÖDEMELER
    with tabs[4]:
        st.subheader("Treasury günlük ödeme hızı (DTS)")
        st.caption("Son 20 iş günü ortalaması / önceki 60 gün ortalaması. >1 = ödemeler hızlanıyor.")
        mom = T("dts_momentum")
        if not mom.empty:
            st.dataframe(mom.sort_values("ratio", ascending=False), hide_index=True)
        out = T("outlays")
        if not out.empty:
            cats = sorted(out["category"].unique())
            default = [c for c in cats if any(k in c.lower() for k in ("energy", "highway", "commerce"))][:3]
            sel = st.multiselect("Kategori", cats, default=default)
            o = out[out["category"].isin(sel)].copy()
            if not o.empty:
                o = o.pivot_table(index="record_date", columns="category", values="amount_musd", aggfunc="sum").sort_index()
                st.line_chart(o.rolling(20, min_periods=5).mean())
        dm = ctx.get("dts_multipliers", {})
        if dm:
            st.markdown("**Skora uygulanan sektör çarpanları**")
            st.dataframe(pd.DataFrame([{"sektör": SECTOR_TR.get(k, k), "çarpan": v} for k, v in dm.items()]), hide_index=True)

    # ---------------------------------------------------------------- 6. UYARILAR
    with tabs[5]:
        al = T("alerts")
        st.subheader("Uyarılar")
        if not al.empty:
            al = al.sort_values("run_id", ascending=False)
            st.dataframe(al[[c for c in ["run_id", "reason", "stage", "program", "title", "tickers", "url"] if c in al.columns]].head(300),
                         hide_index=True, column_config={"url": st.column_config.LinkColumn("Kaynak")})
        else:
            st.info("Uyarı yok.")

    # ---------------------------------------------------------------- 7. MODEL KARNESİ
    with tabs[6]:
        st.subheader("Model karnesi — sistem kendini nasıl puanlıyor?")
        metrics = T("metrics")
        if not metrics.empty:
            last = metrics.sort_values("asof").groupby("horizon_days").tail(1)
            st.dataframe(last, hide_index=True)
            st.plotly_chart(px.line(metrics, x="asof", y="skill", color=metrics["horizon_days"].astype(str),
                                    labels={"color": "Ufuk (gün)", "skill": "Beceri"}).update_layout(height=300))
            st.caption("Brier: olasılık hatası (düşük iyi). Beceri > 0: taban orandan iyi. drift_flag: son 60 tahmin belirgin kötüleşti.")
        else:
            st.info("Tahminler ufku doldukça puanlanacak (en erken 5 iş günü sonra).")
        state = J("model_state")
        if state:
            a, b = st.columns(2)
            a.markdown("**Kalibrasyon (lojistik a, b)**")
            a.json(state.get("calibration", {}))
            b.markdown("**Öğrenilmiş program etkinliği** (olay çalışmasından)")
            b.json(state.get("program_efficacy", {}) or {"durum": "henüz yeterli olay yok"})
        es = T("event_study")
        if not es.empty:
            st.markdown("**Olay çalışması: CAR[0,+5] programa göre**")
            g = es.groupby("program")["car_0_5"].agg(["count", "mean", "median"]).reset_index()
            st.dataframe(g, hide_index=True)
        st.markdown("**Son çalışma — kaynak sağlığı**")
        st.dataframe(D.last_run_health(T("runs")), hide_index=True)

    with tabs[7]:
        st.subheader("Kanıt motoru — tüm seviyeler")
        st.caption("Seviyeler: role (yalnız hedef türü) → kind (haber türü) / program → program_kind (en özel). "
                   "q = Benjamini–Hochberg düzeltilmiş p. post_mean = hiyerarşik empirical Bayes ile daraltılmış etki. "
                   "oos_* = ileriye dönük (walk-forward) doğrulama. live_* = Telegram sinyallerinin gerçekleşen isabeti.")
        evx = T("evidence")
        if not evx.empty:
            lv = st.multiselect("Seviye", ["program_kind", "kind", "program", "role"], default=["program_kind", "kind"])
            cols = [c for c in ["icon", "key", "level", "status", "n_obs", "n_clusters", "mean_car", "median_car", "post_mean",
                                "post_sd", "prob_dir", "t", "p", "q", "consistency", "oos_n", "oos_hit", "oos_signed",
                                "live_n", "live_hit", "first_date", "last_date"] if c in evx.columns]
            st.dataframe(evx[evx["level"].isin(lv)][cols], hide_index=True)
            eh = T("evidence_history")
            if not eh.empty:
                st.markdown("**Kanıt geçmişi** (anahtar başına örnek sayısı zamanla)")
                k = st.selectbox("Anahtar", sorted(eh["key"].unique()))
                st.line_chart(eh[eh["key"] == k].set_index("asof")[["n_clusters"]])
        else:
            st.info("Kanıt tablosu henüz yok. Kapanış veya haftalık çalışmadan sonra oluşur (fiyat verisi gerekir).")
        er = T("event_returns")
        if not er.empty:
            st.markdown("**Olay başına anormal getiriler** (son 300)")
            st.dataframe(er.sort_values("date", ascending=False).head(300), hide_index=True)

    # ---------------------------------------------------------------- 9. ETİKETLEME
    with tabs[8]:
        st.subheader("Etiketleme — sisteme öğretin")
        qi = T("quality_issues")
        if not qi.empty:
            with st.expander(f"🛠️ Otomatik etiketleme kararları ({len(qi)})"):
                st.dataframe(qi.sort_values("found_at", ascending=False) if "found_at" in qi.columns else qi,
                             hide_index=True)
        st.caption("Her etiket haftalık eğitimde kullanılır. Model en kararsız olduğu kayıtları önce sorar (aktif öğrenme).")
        gh_token, gh_repo = secret("GH_TOKEN"), secret("GH_REPO")
        branch = secret("DATA_BRANCH", "data")

        def push_labels(lines: list[dict]) -> str:
            if not (gh_token and gh_repo):
                return "nosecret"
            url = f"https://api.github.com/repos/{gh_repo}/contents/labels/labels.jsonl"
            h = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
            r = requests.get(url, headers=h, params={"ref": branch}, timeout=20)
            sha, old = None, ""
            if r.status_code == 200:
                sha = r.json()["sha"]
                old = base64.b64decode(r.json()["content"]).decode("utf-8")
            new = (old.rstrip("\n") + "\n" if old.strip() else "") + "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
            body = {"message": f"labels: +{len(lines)}", "content": base64.b64encode(new.encode()).decode(), "branch": branch}
            if sha:
                body["sha"] = sha
            r = requests.put(url, headers=h, json=body, timeout=20)
            return "ok" if r.status_code in (200, 201) else f"hata {r.status_code}"

        ts = datetime.now(timezone.utc).isoformat()
        all_ev = T("events")
        progs = ["irrelevant"] + sorted(all_ev["program"].dropna().unique().tolist()) if not all_ev.empty else ["irrelevant"]
        q = D.labeling_queue(all_ev)
        st.markdown("**1) Program / ilgisiz**")
        new_labels = []
        for _, r in q.iterrows():
            cur = r["program"] if r["program"] in progs else "irrelevant"
            if not D.as_bool(pd.Series([r["relevant"]])).iloc[0]:
                cur = "irrelevant"
            choice = st.selectbox(f"{r['date']} · {str(r['title'])[:110]}", progs, index=progs.index(cur), key=f"p_{r['event_id']}")
            if choice != cur:
                new_labels.append({"event_id": r["event_id"], "kind": "program", "label": choice, "ts": ts})
        rq = T("review_queue")
        st.markdown("**2) Şirket eşleşmesi onayı**")
        for _, r in rq.head(20).iterrows():
            col = st.columns([3, 2])
            ans = col[0].radio(f"{r['recipient']} → **{r['candidate']}** ? (skor {r['score']})", ["Atla", "Doğru", "Yanlış"],
                               horizontal=True, key=f"e_{r['recipient_norm']}")
            manual = col[1].text_input("Doğru ticker (isteğe bağlı)", key=f"t_{r['recipient_norm']}")
            if ans == "Doğru":
                new_labels.append({"kind": "entity", "recipient_norm": r["recipient_norm"], "ticker": r["candidate"], "ts": ts})
            elif ans == "Yanlış":
                new_labels.append({"kind": "entity", "recipient_norm": r["recipient_norm"],
                                   "ticker": manual.strip().upper() or "NONE", "ts": ts})
        if new_labels:
            st.info(f"{len(new_labels)} yeni etiket hazır.")
            if st.button("💾 Etiketleri kaydet"):
                res = push_labels(new_labels)
                if res == "ok":
                    st.success("Kaydedildi. Bir sonraki haftalık eğitimde kullanılacak.")
                elif res == "nosecret":
                    st.warning("GH_TOKEN / GH_REPO tanımlı değil. Dosyayı indirip data dalındaki labels/labels.jsonl'a ekleyin.")
                else:
                    st.error(res)
            st.download_button("⬇️ Etiketleri indir (jsonl)", "\n".join(json.dumps(x, ensure_ascii=False) for x in new_labels),
                               file_name="labels_new.jsonl")


if mode.startswith("🟢"):
    simple_view()
else:
    expert_view()

st.caption("Bu araç karar desteği içindir, yatırım tavsiyesi değildir. Veriler ücretsiz resmi API'lerden otomatik derlenir.")
