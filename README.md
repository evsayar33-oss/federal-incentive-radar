# 📡 ABD Federal Teşvik Radarı v2

Bu sistem ABD federal teşviklerini izler. Paranın yolunu kanun tasarısından şirket bildirimine kadar takip eder. Sonra her sektör ve şirket için beklenen etkiyi bir güven aralığıyla hesaplar.

- **Maliyet:** 0 $. Tüm kaynaklar ücretsiz ve resmidir.
- **LLM:** Yok. Sınıflandırma kural tabanlıdır ve klasik makine öğrenmesiyle gelişir.
- **Takvim:** Her iş günü 3 kontrol yapılır: açılış (09:45), gün ortası (12:30) ve kapanış (16:15). Saatler New York saatidir.
- **Öğrenme:** Sistem her tahmini kaydeder. Sonra tahmini gerçek sonuçla karşılaştırır ve kendini yeniden ayarlar.
- **Kanıt motoru:** Sistem her haber türünün hisseleri gerçekten etkileyip etkilemediğini ölçer. Yalnızca kanıtlanmış türler öne çıkar.
- **Telegram:** Kanıtlanmış türde yeni bir haber gelince bot size mesaj gönderir.

---

## 1. Sistem nasıl çalışır?

```
GitHub Actions (günde 3 kez + haftada 1)
 ├─ TOPLA     Federal Register · Congress.gov · USAspending · Grants.gov · SEC EDGAR · GDELT
 │            (açılışta ek olarak: FRED belirsizlik endeksleri, Treasury günlük ödemeleri)
 │            (kapanışta ek olarak: günlük fiyatlar)
 ├─ SINIFLA   Kural motoru + kendini eğiten TF-IDF modeli → program, sektör payı, güven
 ├─ EŞLEŞTİR  Ödül alıcısı adı → ticker (ETF varlıkları + SEC listesi, insan onaylı)
 ├─ SKORLA    Monte Carlo: tutar × aşama olasılığı × zamanlama × sektör payı × etkinlik × rejim × ödeme hızı
 ├─ DEFTER    Kapanışta: "sektör ETF'i SPY'ı geçer mi?" olasılığı yazılır (değiştirilemez)
 ├─ KANIT     Kapanışta + haftalık: her haber türünün hisseye gerçek etkisi ölçülür (olay çalışması)
 ├─ TELEGRAM  Yeni haber kanıtlanmış bir türdeyse bildirim gider; sonucu 5 gün sonra ölçülür
 └─ YAYIMLA   Tüm tablolar 'data' dalına yazılır
                     │
                     ▼
Streamlit Community Cloud  →  yalnızca okur, hesap yapmaz (hızlı ve ücretsiz)
                     │
                     ▼
Siz etiket verirsiniz  →  haftalık eğitim bu etiketleri kullanır
```

### Teşvik boru hattı (aşamalar)

| Aşama | Ad | Kaynak | Gerçekleşme olasılığı (başlangıç) |
|---|---|---|---|
| 0 | Sinyal (haber) | GDELT | 0,10 |
| 1 | Tasarı | Congress.gov | 0,06 (bir meclisten geçtiyse 0,35) |
| 2 | Kanun / kararname | Congress.gov, Federal Register | 0,85 |
| 3 | Kural / rehber | Federal Register | 0,90 (önerilen kural 0,75) |
| 4 | İlan (NOFO) | Grants.gov, Federal Register | 0,92 |
| 5 | Yükümlülük (ödül) | USAspending | 0,95 |
| 6 | Ödeme | Treasury DTS (sektör çarpanı olarak) | 1,00 |
| 7 | Şirket bildirimi | SEC EDGAR (8-K, 10-Q, 10-K) | 0,90 |

Bu değerler `config/priors.json` dosyasındadır. Siz değiştirebilirsiniz.

---

## 2. Kurulum (yaklaşık 20 dakika)

### Adım 1 — Depoyu oluşturun

1. GitHub'da yeni bir depo oluşturun. Depo **public** olmalıdır. Public depoda Actions dakikaları ücretsiz ve sınırsızdır.
2. Bu klasördeki tüm dosyaları depoya yükleyin. `.github` ve `.streamlit` klasörlerini unutmayın.

### Adım 2 — Gizli anahtarları ekleyin

**Settings → Secrets and variables → Actions → New repository secret** yolunu izleyin.

| Ad | Zorunlu mu? | Değer | Nereden alınır? |
|---|---|---|---|
| `SEC_USER_AGENT` | Evet | `AdınızSoyadınız eposta@ornek.com` | SEC, ad ve e-posta ister. Anahtar gerekmez. |
| `CONGRESS_API_KEY` | Önerilir | Ücretsiz anahtar | https://api.congress.gov/sign-up/ |
| `BEA_API_KEY` | Önerilir | Ücretsiz 36 karakterlik anahtar | https://apps.bea.gov/api/signup/ (girdi-çıktı yayılması için) |
| `FRED_API_KEY` | İsteğe bağlı | Ücretsiz anahtar | https://fred.stlouisfed.org/docs/api/api_key.html (anahtarsız yol çalışmazsa yedek) |
| `GOVINFO_API_KEY` | Önerilir | Ücretsiz api.data.gov anahtarı | https://api.data.gov/signup/ (yedek kaynaklar için) |
| `TELEGRAM_BOT_TOKEN` | İsteğe bağlı | Bot token | Telegram → @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | İsteğe bağlı | Sohbet numarası | Aşağıdaki Adım 6 |

Diğer tüm kaynaklar anahtar istemez.

### Adım 3 — İlk çalıştırma

1. **Actions** sekmesini açın. İş akışlarını etkinleştirin.
2. **"Radar — günde 3 kontrol"** iş akışını seçin. **Run workflow** düğmesine basın.
3. Mod olarak **`bootstrap`** seçin. Bu mod şirket evrenini, fiyatları ve ilk verileri yükler.
4. Arayüzü hemen görmek isterseniz önce **`demo`** modunu çalıştırabilirsiniz. Demo verisi gerçek değildir. İlk gerçek çalışma onun üzerine yazar.

Bundan sonra sistem kendi kendine çalışır.

### Adım 4 — Streamlit arayüzü

1. https://share.streamlit.io adresine gidin. GitHub hesabınızla girin.
2. **Create app** düğmesine basın. Deponuzu ve `main` dalını seçin.
3. **Main file path** alanına `app/streamlit_app.py` yazın.
4. **Advanced settings → Secrets** alanına şunu yapıştırın:

```toml
DATA_BASE_URL = "https://raw.githubusercontent.com/<kullanici>/<repo>/data"
```

### Adım 5 (isteğe bağlı) — Arayüzden etiket kaydetme

1. GitHub → Settings → Developer settings → **Fine-grained token** oluşturun.
2. Yalnızca bu depoyu seçin. **Contents: Read and write** iznini verin.
3. Streamlit Secrets alanına ekleyin:

```toml
GH_TOKEN = "github_pat_..."
GH_REPO = "<kullanici>/<repo>"
```

Token yoksa da etiket verebilirsiniz. Arayüz bir dosya indirir. Siz onu `data` dalındaki `labels/labels.jsonl` dosyasına eklersiniz.

### Adım 6 (isteğe bağlı) — Telegram bildirimi

1. Telegram'da **@BotFather** ile konuşun. `/newbot` yazın. Size bir token verir.
2. Token'ı `TELEGRAM_BOT_TOKEN` adlı GitHub Secret olarak ekleyin.
3. Telegram'da yeni botunuza `/start` yazın.
4. **Actions → Radar → Run workflow** yolunu izleyin. Mod olarak **`telegram-test`** seçin. Kayıtta chat id görünür.
5. Bu sayıyı `TELEGRAM_CHAT_ID` adlı GitHub Secret olarak ekleyin. Testi tekrar çalıştırın. Bot size bir deneme mesajı gönderir.

Bot yalnızca **kanıtlanmış** haber türlerini gönderir. Daha çok bildirim için `config/sources.yaml` içinde `telegram.notify_statuses: ["proven", "promising"]` yazın. Cumartesi günleri bot bir kanıt raporu ve kendi isabet karnesini gönderir.

GitHub Issue bildirimi de devam eder. Kapatmak için `config/sources.yaml` içinde `create_github_issue: false` yazın.

---

### Federal sözleşmeler ve tedarik zinciri etkisi

- **Sözleşmeler:** USAspending'den 50 milyon $ üstü federal sözleşmeler toplanır. Sözleşme bir teşvik değil, satın almadır. Bu yüzden tutarın %35'i sayılır (`config/priors.json` → `amount_type_weight`).
- **BEA girdi-çıktı yayılması:** Haftalık iş, BEA'nın sektör düzeyi "Total Requirements" tablosunu (TableID 56) indirir. Bir sektöre gelen teşvik, tedarik zinciri üzerinden diğer sektörlere dolaylı etki taşır. Dolaylı etkinin skora katılma oranı `spillover.weight` ile ayarlanır (varsayılan 0,5). `BEA_API_KEY` yoksa yalnızca doğrudan etki hesaplanır.

### Canlı tutma

`keepalive.yml` ayın 1'inde ve 15'inde çalışır. Radar iş akışlarını etkin tutar ve ayda bir küçük bir commit atar. Bu sayede GitHub'ın 60 gün kuralı zamanlanmış işleri kapatmaz.

## 3. Kanıt motoru: hangi haber hangi hisseyi etkiler?

Sistem bir haber türünü ancak ölçerek kanıtlar. Kod: `src/radar/evidence.py`. Eşikler: `config/priors.json → evidence`.

**Haber türü** = program × belge türü × etkilenen varlık. Örnek: "Yarı iletken (CHIPS) · 8-K bildirimi · şirketin kendi hissesi".

| Etkilenen varlık | Ne zaman? | Nasıl seçilir? |
|---|---|---|
| Şirketin kendi hissesi | Olayda ticker var (SEC bildirimi, eşleşmiş ödül) | Doğrudan |
| Program sepeti | Ticker yok, program belli (kural, fon ilanı, tasarı) | Bu programda **daha önce** doğrudan olayı olan şirketler. Gelecek bilgi kullanılmaz. |
| Sektör ETF'i | Ticker yok | Programın baskın sektörü |

**Ölçüm adımları:**

1. **Anormal getiri:** Piyasa modeli (SPY), tahmin penceresi [-250, -30] gün. Pencereler: [-1,+1], [0,+5], [0,+20]. Ana pencere: [0,+5].
2. **Standartlaştırma:** CAR, hissenin kendi oynaklığına bölünür (SCAR). Oynak küçük hisseler sonucu ezmez.
3. **Kümeleme:** Aynı gündeki olaylar tek küme sayılır. Test kümeler üzerinde yapılır.
4. **Çoklu test düzeltmesi:** Benjamini–Hochberg q değeri. Çok sayıda tür test edildiği için şans eseri bulunan etkiler elenir.
5. **Hiyerarşik daralma (empirical Bayes):** Az örnekli türün etkisi üst türün etkisine doğru küçültülür.
6. **İleriye dönük doğrulama:** Her olay yalnızca o tarihten önceki verilerle tahmin edilir (8 günlük purge ile). Etki önceden bilinebilir değilse tür kanıtlanmaz.
7. **Canlı geri besleme:** Telegram sinyallerinin gerçekleşen getirisi ölçülür. Son 10+ sinyalde isabet %45'in altındaysa tür **askıya** alınır.

**Durumlar:**

| Durum | Şart |
|---|---|
| 🟢 Kanıtlandı | ≥15 olay günü, q ≤ 0,10, etki ≥ %0,5, posterior yön olasılığı ≥ 0,90, ileriye dönük isabet ≥ %55 (≥6 tahmin) |
| 🟡 Umut verici | ≥8 olay günü, p ≤ 0,10, q ≤ 0,25, posterior ≥ 0,80, ileriye dönük test negatif değil |
| ⚫ Etkisiz | ≥30 olay günü, p ≥ 0,5 — bu haberleri önemsemeyebilirsiniz |
| ⚪ Belirsiz / veri birikiyor | Hüküm yok |
| 🟠 Askıda | Kanıtlıydı, canlıda tutmadı |

Bir olay için önce en özel tür kontrol edilir. Özel türün yeterli verisi varsa onun hükmü geçerlidir. Genel türe yalnızca özel türde veri yetersizse inilir. Böylece CHIPS 8-K'larının kanıtı, başka bir programın 8-K'sına taşınmaz.

**Doğrulama:** Testler, etkisi bilinen sentetik bir piyasa kurar. Motor gerçek etkiyi bulmalı ve gürültüyü reddetmelidir (`tests/test_core.py`).

**Tablolar (data dalı):** `event_returns` (olay başına getiri), `evidence` (tür başına kanıt), `evidence_history` (kanıtın zamanla değişimi), `event_signals` (son olayların rozetleri), `notifications` (gönderilen sinyaller ve sonuçları).

### Akıllı otomatik etiketleme

Sistem hatalı kayıtları kendisi bulur ve düzeltir. Kod: `src/radar/autolabel.py`. Her çalışmada ve haftalık eğitimde çalışır.

| Kural | Ne bulur? | Ne yapar? |
|---|---|---|
| İdari/akademik belge | "Faculty Award", "Comment Request", "Privacy Act", toplantı, tören | İlgisiz yapar |
| Kurum çelişkisi | Program ile yayımlayan kurum uyumsuz (ör. NIH ödülü "altyapı") | Kuruma uygun programa taşır; uygun program yoksa "diğer" |
| Kamu alıcısı | Üniversite, belediye, eyalet kurumu bir hisseye eşleşmiş | Eşleşmeyi kaldırır (şirket eki varsa dokunmaz) |
| Güvenli eşleşme | İnceleme kuyruğunda aday adı alıcı adının içinde | Onaylar |
| Sepet kalitesi | 10-K'daki kalıp ifadeyle sepete giren ilgisiz şirket | Sepete yalnızca güçlü bağ (ödül, 8-K) veya uygun sektör + ≥2 bildirim ile alır |

Kararlar `labels/auto_labels.jsonl` ve `quality_issues` tablosuna gerekçeyle yazılır. Sizin etiketiniz her zaman otomatik etiketten önce gelir. Model otomatik etiketlerden daha düşük ağırlıkla öğrenir. Veri sorunları (bayat fiyat, sürekli boş kaynak, büyük inceleme kuyruğu) sağlık mesajına eklenir.

### Kalıcı bağ denetimi (genel kural)

Bir şirket ile bir teşvik programı arasındaki bağ iki testten geçer. Testler her olayda ve her zaman çalışır.

1. **Sektör uyumu:** Şirketin sektörü programın sektörlerinden biri olmalıdır. Sektör önce ETF evreninden, yoksa SEC SIC kodundan (`company_sic` tablosu, haftalık güncellenir) alınır. Örnek: BRX bir gayrimenkul fonudur (SIC 6798). Kritik mineral programına bağlanamaz. Biyoteknoloji şirketlerindeki "Inflation Reduction Act" ifadeleri (ilaç fiyatı yasası) temiz enerjiye bağlanmaz.
2. **Bağın gücü:** Federal ödül her zaman güçlü bağdır. 8-K yalnızca önemli olay maddesi (1.01, 1.02, 2.01, 7.01, 8.01) içeriyorsa güçlüdür. Rutin 8-K (2.02 finansal sonuç), 10-K ve 10-Q güçlü bağ sayılmaz.

Uyumsuz bağ kanıt hesabına girmez, sepete giremez ve Telegram sinyali üretmez. Arayüzde ⚠️ ile gösterilir. Her durum `quality_issues` tablosuna yazılır.

### Yedek kaynaklar

Birincil kaynak hata verirse veya alışılmadık biçimde boş dönerse yedek kaynak otomatik çalışır. Yedekler aynı olay kimliğini üretir, bu yüzden kayıtlar çiftlenmez. Yedek kullanılınca Telegram sağlık mesajı ♻️ ile bildirir.

| Birincil | Yedek |
|---|---|
| Federal Register API | GovInfo FR paketleri |
| Congress.gov API | GovInfo BILLS koleksiyonu |
| USAspending (ödül araması) | USAspending (işlem araması) |
| Grants.gov search2 | Grants.gov eski REST uç noktası |
| SEC tam metin arama | data.sec.gov: bilinen şirketlerin yeni 8-K'ları |
| yfinance | Yahoo chart API (2 sunucu) → Stooq |
| FRED CSV | FRED API (mevcut) |
| GDELT | Google News RSS (mevcut) |

GovInfo için `GOVINFO_API_KEY` secret'ını ekleyin (https://api.data.gov/signup/, ücretsiz). Yoksa `CONGRESS_API_KEY` kullanılır (aynı tür anahtar).

## 4. Arayüz: basit ve uzman görünüm

Sol menüden görünümü seçin.

- **🟢 Basit (varsayılan):** Bugün (yalnızca kanıtlı ve umut verici haberler), İşe yarayan haberler, Hisse sorgula, Telegram, Nasıl okunur?
- **🔬 Uzman:** Önceki tüm sekmeler ve **Kanıt motoru** sekmesi (tüm seviyeler, q, posterior, ileriye dönük test, canlı isabet).

## 5. Saatler neden iki kez tetikleniyor?

GitHub cron UTC ile çalışır ve yaz/kış saatini bilmez. Bu yüzden her slot için iki tetikleyici vardır. Kod New York saatini kontrol eder. Yalnızca doğru pencerede ve günde bir kez çalışır. Fazla tetiklemeler birkaç saniyede biter. GitHub zamanlanmış işleri bazen geciktirir. Pencereler (150–180 dakika) bu gecikmeyi tolere eder.

---

## 6. Sistem kendini nasıl geliştirir?

| Döngü | Ne öğrenir? | Ne zaman? |
|---|---|---|
| **Tahmin defteri + kalibrasyon** | Akış sürprizi → "SPY'ı geçme" olasılığı ilişkisi (lojistik a, b) ve sektör sapması | Haftalık |
| **Olay çalışması (CAR)** | Hangi program türü piyasada gerçekten fark yaratıyor? → program etkinlik çarpanı (0,6–1,4) | Haftalık |
| **Kanıt motoru** | Hangi haber türü hangi hisseyi etkiliyor? Yeni olaylarla kanıt güçlenir veya zayıflar | Her kapanış + haftalık |
| **Bot karnesi** | Gönderilen sinyaller tuttu mu? Tutmayan tür askıya alınır | Her kapanış + haftalık |
| **Kendini eğiten sınıflandırıcı** | Sizin etiketleriniz + kuralın çok emin olduğu örnekler → TF-IDF + lojistik regresyon | Haftalık |
| **Varlık eşleştirme** | Onayladığınız her ad → ticker eşleşmesi kalıcı olur | Her çalışmada |
| **Drift izleme** | Program dağılımı değişimi (PSI) ve son 60 tahminde kötüleşme | Her çalışmada |

Modelin ağırlığı etiket sayısıyla artar: 0 etikette %10, 400+ etikette %60. Az etiketle model kurallara baskın gelmez.

**Model karnesi** sekmesi sistemin dürüst puanını gösterir. Beceri skoru 0'ın üstündeyse sistem taban orandan iyidir. Altındaysa sinyali henüz yoktur. Bu bilgi de değerlidir.

---

## 7. Yeni program veya kaynak eklemek

- **Yeni teşvik programı:** `config/programs.yaml` dosyasına bir blok ekleyin: anahtar kelimeler, sektör payları, teşvik türü. Kod değişikliği gerekmez.
- **Yeni SEC ifadesi:** `config/sources.yaml` → `sec_edgar.phrases` altına `'"ifade"': program_kodu` ekleyin.
- **Yeni haber sorgusu:** `config/sources.yaml` → `gdelt.queries`.
- **Yeni kaynak:** `src/radar/sources/` altına `fetch(since) -> DataFrame` sunan bir modül yazın. Ortak şema `src/radar/schema.py` içindedir.

---

## 8. Yerel çalıştırma

```bash
pip install -r requirements-etl.txt -r requirements.txt
PYTHONPATH=src python -m radar.run demo --days 200      # internetsiz demo
PYTHONPATH=src python -m radar.run slot --slot open --force
PYTHONPATH=src python -m radar.run telegram-test           # TELEGRAM_BOT_TOKEN gerekir
streamlit run app/streamlit_app.py
PYTHONPATH=src pytest -q tests
```

---

## 9. Sınırlamalar

- **Fiyat verisi** resmi bir kaynaktan gelmez (yfinance, yedek: Stooq). Sistem fiyat olmadan da skor üretir. Yalnızca puanlama ve olay çalışması ertelenir.
- **Vergi kredileri** (45X, 48E, ...) USAspending'de görünmez. Bunlar Federal Register rehberleri ve SEC bildirimleriyle izlenir. Tutarları tahminidir.
- **Tutarı bilinmeyen belgeler** (tasarı, kural) varsayılan tutarla ve geniş belirsizlikle hesaplanır. Aynı programdaki tekrarlar azalan ağırlık alır.
- **Ad eşleştirme** hatasız değildir. Belirsiz eşleşmeler otomatik kabul edilmez, onayınıza gelir.
- **Kanıt zaman ister.** İlk aylarda çoğu tür "veri birikiyor" durumundadır. Bu doğru davranıştır: sistem az veriyle hüküm vermez.
- **Kanıtın kalitesi eşleştirmeye bağlıdır.** Yanlış ticker eşleşmeleri sepetleri bozar. Etiketleme sekmesinden eşleşmeleri onaylayın.
- **Bildirim gecikmesi:** Sistem günde 3 kez çalışır. Bir haber en geç birkaç saat içinde bildirilir.
- **Nedensellik sınırlıdır.** Skorlar karar desteği içindir. Yatırım tavsiyesi değildir.
- Ücretsiz katman limitleri değişebilir. Limitleri `config/sources.yaml` ve `src/radar/http.py` içinde güncelleyin.
