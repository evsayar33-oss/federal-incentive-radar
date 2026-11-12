"""H083 methodology catalog. Descriptive, not a signal or evidence verdict.

Frozen H083 research: 34 event-anchored variants and 10 stock-rotation rules.
Only monthly_low_beta and monthly_vol_squeeze are in the Radar watch pilot.
All historical H083 strategies have certified_alpha=False.
"""
from __future__ import annotations
import pandas as pd

NEWS_FAMILIES = {
    "corporate_capex": (
        "Şirket sermaye yatırımı",
        "Fabrika, çip üretimi, veri merkezi, enerji altyapısı ve Ar-Ge gibi şirket yatırım duyuruları. "
        "Tutarın büyük olması hisse getirisinin pozitif olacağını kanıtlamaz.",
    ),
    "federal_award": (
        "Federal hibe / kredi / sözleşme",
        "USAspending vb. şirketle doğrudan eşleştirilmiş federal yükümlülükler. "
        "Action-date ilk kamuya açıklama tarihi değildir; date-only vekiller yürütülebilir kanıt sayılmaz.",
    ),
    "policy_regulatory": (
        "Kanun / düzenleme / program",
        "Federal Register ve Congress haberleri. Onaylanma, önerilme ve yürürlüğe girme ayrı aşamalardır; "
        "sektörün fayda görmesi hisse bazında kesin kâr anlamına gelmez.",
    ),
}

EVENT_STYLES = [
    ("baseline",(5,20,60),"Haberden sonraki ilk uygun seans; kıyas ölçümü."),
    ("delay_3",(5,20),"Haberi takiben üç seans gecikmeli giriş."),
    ("confirmation",(5,20),"Pozitif fiyat teyidi geldikten sonraki seans."),
    ("breakout_20",(5,20),"Önceki 20 seansın zirvesi üstünde kırılım."),
    ("pullback_rsi2",(5,20),"İki günlük RSI ile kısa vadeli geri çekilme."),
    ("dip_3d",(5,20),"Haber sonrası üç günlük düşüş/geri çekilme."),
    ("momentum_20",(5,20),"20 günlük pozitif fiyat momentumu."),
    ("trend_50",(5,20),"50 günlük fiyat trendi filtresi."),
    ("low_beta",(5,20),"SPY'a göre geçmiş beta filtresi (haber-tetikli)."),
    ("liquid_5m",(5,20),"Yeterli dolar hacmi ile likidite filtresi."),
    ("risk_on",(5,20),"SPY piyasa rejimi pozitifken haber girişi."),
    ("sector_rs",(5,20),"Sektörün SPY'a göre göreli gücü."),
    ("plant",(5,20),"Fabrika/üretim yatırımı türüne özel haber."),
    ("plant_trend",(5,20),"Fabrika yatırımı ile fiyat trendini birleştirme."),
]
ATR_EXITS = [
    ("baseline","atr_bracket","ATR tabanlı stop ve kâr alma eşikleri."),
    ("baseline","atr_trail","ATR tabanlı takip eden stop."),
    ("breakout_20","atr_bracket","20 günlük kırılım + ATR stop/hedef."),
    ("pullback_rsi2","atr_bracket","RSI2 geri çekilme + ATR stop/hedef."),
    ("risk_on","atr_trail","Risk-on rejimi + ATR takip stopu."),
]
ROTATIONS = [
    ("weekly_3m_mom","Haftalık","3 aylık momentum liderleri.",False),
    ("weekly_trend_50","Haftalık","50 günlük trend liderleri.",False),
    ("monthly_6m_mom","Aylık","6 aylık momentum liderleri.",False),
    ("monthly_breakout","Aylık","20 günlük kırılıma yakın liderler.",False),
    ("monthly_pullback","Aylık","200 günlük trendde RSI2 geri çekilmesi.",False),
    ("monthly_low_beta","Aylık","200 günlük trend, 63 günlük momentum >0, beta 0–1; düşük beta öncelikli.",True),
    ("monthly_sector_leader","Aylık","Sektörün piyasayı geçtiği güçlü hisseler.",False),
    ("monthly_vol_squeeze","Aylık","50 günlük trend ve geçmişine göre düşük ATR; güçlü momentum öncelikli.",True),
    ("monthly_risk_on_mom","Aylık","Piyasa trendi pozitifken momentum.",False),
    ("monthly_market_rs","Aylık","SPY'a göre göreli momentum.",False),
]


def catalog() -> pd.DataFrame:
    rows=[]
    for name,horizons,meaning in EVENT_STYLES:
        for hold in horizons:
            rows.append({"track":"HABER SONRASI","rule":f"{name}/{hold}/time",
                         "family":"Tüm uygun kaynak haberler" if not name.startswith("plant") else "corporate_capex/plant",
                         "logic":meaning,"entry":"Haber sonrası gözlenebilir kapanış → sonraki uygun açılış",
                         "exit":f"{hold} işlem günü","monitoring":"RESEARCH_ONLY",
                         "certified_alpha":False})
    for name,exit_type,meaning in ATR_EXITS:
        rows.append({"track":"HABER SONRASI","rule":f"{name}/20/{exit_type}",
                     "family":"Tüm uygun kaynak haberler","logic":meaning,
                     "entry":"Haber sonrası gözlenebilir kapanış → sonraki uygun açılış",
                     "exit":exit_type,"monitoring":"RESEARCH_ONLY","certified_alpha":False})
    for name,freq,meaning,watched in ROTATIONS:
        rows.append({"track":"HİSSE ROTASYONU","rule":name,"family":"Olayla ilişkilendirilmiş hisse evreni",
                     "logic":meaning,"entry":f"{freq} sıralama; önceki tam kapanıştan sonraki açılış",
                     "exit":"Bir sonraki yeniden dengeleme","monitoring":"FORWARD_PAPER_WATCH" if watched else "RESEARCH_ONLY",
                     "certified_alpha":False})
    out=pd.DataFrame(rows)
    assert len(out)==44 and out.rule.nunique()==44
    return out


def news_catalog(feed: pd.DataFrame | None = None) -> pd.DataFrame:
    frame = feed if feed is not None else pd.DataFrame()
    rows=[]
    for family,(title,desc) in NEWS_FAMILIES.items():
        subset=frame[frame.event_family.eq(family)] if not frame.empty and "event_family" in frame else pd.DataFrame()
        rows.append({"event_family":family,"name":title,"description":desc,
                     "source_events":int(len(subset)),
                     "distinct_tickers":int(subset.ticker.nunique()) if not subset.empty and "ticker" in subset else 0,
                     "historical_alpha_confirmed":False,
                     "status":"OLAY KAYNAĞI, KÂRLILIK KANITI DEĞİL"})
    return pd.DataFrame(rows)
