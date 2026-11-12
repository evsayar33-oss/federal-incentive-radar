# H083 — Olay, işlem tarzı, gelecekteki koşullu işlem bildirimleri

## Gerçek araştırma durumu (12 Kasım 2026)
H083 / quant_superlab: **34 haber-tetikli varyant + 10 hisse rotasyonu = 44**.
Haberler üç kaynaktan gelir: (1) corporate_capex, (2) federal_award,
(3) policy_regulatory. Hiçbir haber türü tek başına "kanıtlanmış kâr" değildir.
H083'ün SPY-relative alpha kapıları geçilmedi: historical certified = 0.

Radar'ın canlı araştırma pilotunda yalnız **monthly_low_beta** ve
**monthly_vol_squeeze** olmak üzere iki aylık hisse rotasyonu etkin.
Diğer 42 yaklaşım, tanımı açık ama doğrudan AL/SAT izni vermeyen katalogdur.

## Sitede
Basit ve Uzman görünümüne ayrı **H083 · Haber & İşlem** sekmesi eklenir.
Tüm 44 kural (karar zamanı, giriş, çıkış), üç haber ailesinin rolleri,
doğrudan ticker/haber URL'leri, günlük izleme, ileri kanıt tablosu ve
**yalnız onaylanırsa** otomatik taslak bilgi planları gösterilir.

Önemli: haber başına fırsat ve aylık momentum/volatilite rotasyonu **farklı**
strateji aileleridir; bir haberin kendi getirisini kanıtlamayan rotasyon
sonucu yanlışlıkla 'kanıtlı haber' olarak yazılmaz.

## Otomatik NYSE kapanış döngüsü
Ayrı workflow `.github/workflows/h083_forward.yml`, New York 17:20'de
(21:20 UTC yaz / 22:20 UTC kış) çalışır. Gerçek NYSE takvimi,
resmi seans kapanışından ≥45 dakika ve aynı gün SPY OHLCV kapanışı şarttır.
Resmi tatil, veri eksikliği veya cron gecikmesi **fail-closed**.

1. Radar kaynakları -> event_trade_feed.csv. Olaylar:
   kaynak ve doğrudan ticker; ilk gözlem zamanı kalitesi ayrı.
   `action_date_proxy` tarihleri elenir.
2. Tamamlanmış son kapanıştan H083 iki aylık sıralama üretilir.
   Likidite en az 20g ortalama 5 milyon USD. En çok 180 son olay
   ticker'ı incelenir; aşım açıkça `event_watch_status` içinde izlenir.
3. Sadece NYSE ayının **son işlem seansında**, gelecek ayın ilk
   NYSE açılışı için sıralama ve olay meta verisi dondurulur.
   Bilet fiyatı ve gelecekteki kazanç karar anında bilinmez.
4. Gerçek sonraki ayın ilk seansı kapanınca o günün açılış OHLCV
   **as observed** kaydedilir. Ayın ardından gelen ayın ilk seansı
   kapanınca çıkış açılışı kaydedilir. İş akışı bu günü kaçırırsa
   daha sonra geriye fiyat doldurulmaz: kohort geçersizdir.
5. İlk sermaye 10.000 USD; en çok beş adet 1.990 USD'lik
   kağıt pozisyon, işlem başına 1,50 USD alış + 1,50 USD satış
   ve her yönde 5 baz puan kayma simülasyonu. Ağır stres 30 bp/yön.
   SPY aynı 10.000 USD portföy üzerinden, ücret/kayma dahil benchmark.
   Bu **aylık bağımsız kohort** modeli H083'ün tüm portföy yeniden
   dengelenme detaylarını birebir kopyalamaz; sonuçlar yeni modeldir.
6. Araştırma kanıtı yalnız ileriye dönük kapanmış aylarından oluşur.
   Eski H083/H084 sonuçlarını otomatik statüye taşımaz.

**Her ay için katı otomatik kapı**:
- Son en çok 36 ay içinden en az 24 ayın ardışık, eksiksiz ve zamanında
  gözlemlenmiş olması; toplam ≥60 simüle pozisyon.
- SPY üstünde pozitif aylık ortalama ve **SPY'ı geçen pozitif toplam net**
  portföy getirisi.
- Her iki modelin birlikte BH düzeltilmiş q≤0,05.
- Üç aylık hareketli blok bootstrap ile %95 tek taraflı alt sınır >0.
- 30bp/yön kayma maliyeti stresinde pozitif toplam getiri.

Bu şartlar geçerse statü **FORWARD_VALIDATED_PAPER** olur. Bunun
anlamı, **ileri kağıt araştırmada doğrulandı**; gerçek broker fill
kanıtı veya garanti edilmiş alpha değildir. Başka bir 'proven' Radar
etiketi değiştirilmez.

## Kurgulanmış işlem planları / bildirim
Yalnız **FORWARD_VALIDATED_PAPER** olan modellerde;
bir sonraki ay ilk NYSE açılışı için `BUY_NEXT_OPEN`, sonraki
ayı takip eden ilk NYSE açılışı için `EXIT_NEXT_MONTH_OPEN`
birlikte üretilir. Plan her hisse için ilgili kaynak olayı,
işlem tarihi, yaklaşık önceki kapanış, tahmini tam adet ve
bütçe sınırını içerir. Stop/TP icat edilmez; bu iki kuralın çıkışı
aylık dengelenmedir. Örnek emirler fiyat/likidite garantisi değildir.

Telegram her ay iki modeli ve haber kaynaklarının rolünü anlatan,
AL/SAT yetkisi yoksa açıkça "yalnız izleme" diyen bülten gönderebilir.
Onay varsa plan statüsü ve NYSE tarihi eklenir. Mesaj başarılı
gönderildiyse aylık tekilleştirilir; geçici hata aynı işlem gününde
tekrarlanabilir. Otomatik Midas / broker emri gönderilmez.

## Yeni data dosyaları
- `h083_style_catalog`, `h083_news_catalog` (sitede açıklama)
- `h083_forward_cohorts` (değişmeyen karar zamanı ve seçim)
- `h083_forward_fills` (açılış kaynak/ilk gözlem)
- `h083_forward_results` (tamamlanan net sonuç)
- `h083_forward_gates` (çoklu test ve süre kapıları)
- `h083_action_plans` (doğrulanmışsa bilgi amaçlı planlar)
- `h083_forward_status.json` (çalışma/sağlık)
- `h083_telegram_sent` (tekrar gönderim önleme)

**Kaynak riski:** Yahoo ayarlı OHLCV açılışları, Midas gerçek fill'i
değildir; kurumsal aksiyon ve fiyat revizyonu kaynak riski sürer.
Bu nedenle sistem gerçek para talimatı vermez.
