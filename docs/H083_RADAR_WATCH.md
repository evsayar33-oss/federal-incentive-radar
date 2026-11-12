# H083 araştırma izleme katmanı — Federal Incentive Radar

**Amaç:** Radar'ın doğrulanabilir olay/ticker evrenini, `quant_superlab` H083'te
araştırılan **aylık düşük-beta** ve **aylık ATR-volatilite sıkışması** hisse
seçim kriterleriyle izlemek. Bu modül **kanıtlanmış kârlı haber, alım-satım
talimatı veya otomatik emir değildir**.

## Bilimsel sınırlar

H083'nin 10 hisse rotasyonu araştırmasında düşük-beta ve volatilite sıkışması
stratejileri geçmiş örneklemde pozitif net getiri ve görece düşük maksimum
düşüş üretmişti; ancak SPY'a karşı anlamlı alpha gösterilemedi. White Reality
Check p≈0.637 ve 10 rotasyon kıyasında BH q≈0.978 idi. Aynı tarihsel dönemler
önceden incelendiğinden gerçek sealed out-of-sample da yok.

Dolayısıyla hiçbir Radar `proven`, `confirmed`, `buy` veya `trade`
sinyali değiştirilmez. Yeni çıktıların tamamı:
`status=WATCH_ONLY_UNVALIDATED`, `validated_alpha=false`,
`decision=NO_TRADE_AUTHORIZATION`.

**H083 rotasyonları bir olay türünün nedensel fiyat etkisini ispatlamıyor.**
Buradaki olay yalnızca hisse evrenine alınma gerekçesidir. Uzun süre önceki
olaylar nedeniyle listede yer alan hisseler olabilir; bu, yeni katalizör
olduğu anlamına gelmez.

## Gerçek kurallar

Bir hisse, Radar `event_trade_feed.csv` içinde doğrudan eşleştirilmiş bir
`ticker` ve zaman açısından uygun bir olay içeriyorsa evrendedir.
`exact_utc`, `date_only` kaynak tarihli; `first_seen_utc` yalnızca
ileriye dönük gözlem verisidir. `action_date_proxy` ve `unknown`
dışlanır. Tüm kod hesapları tamamlanmış son günlük mum verisini kullanır.

Her iki kuralda da önceki 20 günün ortalama dolar hacmi >=$5m gerekir.

- **monthly_low_beta:** 200 günlük ortalamanın üzerinde kapanış; önceki
  126 seanslık SPY betası (en az 80 gözlem) 0..1; 63 günlük momentum >0;
  sıralama en düşük betadan yukarı.
- **monthly_vol_squeeze:** 50 günlük ortalama üzerinde kapanış; 14 günlük
  ATR / kapanış, önceki 126 gözlemin %20'lik yüzdeliğinden küçük;
  sıralama en yüksek 63 günlük momentumdan aşağı.
- Her kuralın ilk 5 hissesi bağımsız gösterilir.
- SPY 200 günlük trendi yalnızca açıklayıcı piyasa bağlamıdır,
  bu iki kural için yeni bir filtre değildir.
- Midas sabit komisyon referansı 1,50 USD alış + 1,50 USD satış = 3 USD
  gidiş-dönüş; spread/kayma/vergiler buna dahil değildir.

**Dikkat:** H083'teki aylık yeniden dengeleme işlemleri burada çalıştırılmaz.
Her Radar kapanışında yalnızca izleme snapshot'ı hesaplanır. Günlük sıralamalar,
H083 backtest işlemi veya gerçekleşmiş portföy getirisi sayılmaz.
Kârlılık doğrulaması için aynı kurallarla ileriye dönük ayrı bir kağıt işlem
defteri ve tam Midas işlem maliyetiyle gerçek zamanlı takip gerekir.

## Entegrasyon

Radar kapanış workflow'unda `python -m radar.event_feed` ardından
`python -m radar.event_watch` çalışır. Kapanıştan önce tetiklenirse
gereksiz veri indirmez. 16:35 ET öncesi, aynı gün Yahoo mumunun hâlâ
oluşuyor olabileceği varsayıldığından geçmiş tam seans kullanılır.
Fiyat sağlayıcı Yahoo Finance/yfinance; açık pozisyon veya emir yok.

Yayımlanan veri dalı dosyaları:

- `event_watch_candidates.parquet` (izleme listesi, 0 satır olabilir)
- `event_watch_exclusions.parquet` (eksik fiyat/likidite gerekçeleri)
- `event_watch_status.json` (kaplama, zaman damgası, veri sorunları)
- `event_watch_history.parquet` (günlük snapshot kayıtları; trade sonucu değil)

Parquet yüklenemeyen yerel ortamlarda aynı tablolar .csv.gz olabilir.
Streamlit basit ve uzman görünümünde isteğe bağlı kapalı panel bulunur.

Yeni modül `src/radar/event_watch.py`. Çekirdek kanıt motoru, fiyat
deposu, event feed CSV şeması, mevcut Telegram kanıtlı sinyaller ve
canlı işlem dosyaları **değiştirilmez**.

## Test

`PYTHONPATH=src python -m pytest -q tests/test_event_watch.py`

Testler zaman sızıntısı, federal action-date proxy yasağı, `first_seen_utc`
ileri-gözlem ayrımı, veri eksikliği ve gerçek işlem yetkisinin yokluğunu
doğrular.

Referans: https://github.com/evsayar33-oss/quant_superlab/tree/main/research/h083
