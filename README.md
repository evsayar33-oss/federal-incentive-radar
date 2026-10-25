# 📡 ABD Federal Teşvik Radarı v2

Bu sistem ABD federal teşviklerini izler. Paranın yolunu kanun tasarısından şirket bildirimine kadar takip eder. Sonra her sektör ve şirket için beklenen etkiyi bir güven aralığıyla hesaplar.

- **Maliyet:** 0 $. Tüm kaynaklar ücretsiz ve resmidir.
- **LLM:** Yok. Sınıflandırma kural tabanlıdır ve klasik makine öğrenmesiyle gelişir.
- **Takvim:** Her iş günü 3 kontrol yapılır: açılış (09:45), gün ortası (12:30) ve kapanış (16:15). Saatler New York saatidir.
- **Öğrenme:** Sistem her tahmini kaydeder. Sonra tahmini gerçek sonuçla karşılaştırır ve kendini yeniden ayarlar.

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

### Adım 6 (isteğe bağlı) — Telefona bildirim

Önemli olaylarda sistem bir GitHub Issue açar. GitHub mobil uygulamasını kurun ve depoyu "Watch" yapın. Bildirimler telefonunuza gelir. Kapatmak için `config/sources.yaml` içinde `create_github_issue: false` yazın.

---

### Federal sözleşmeler ve tedarik zinciri etkisi

- **Sözleşmeler:** USAspending'den 50 milyon $ üstü federal sözleşmeler toplanır. Sözleşme bir teşvik değil, satın almadır. Bu yüzden tutarın %35'i sayılır (`config/priors.json` → `amount_type_weight`).
- **BEA girdi-çıktı yayılması:** Haftalık iş, BEA'nın sektör düzeyi "Total Requirements" tablosunu (TableID 56) indirir. Bir sektöre gelen teşvik, tedarik zinciri üzerinden diğer sektörlere dolaylı etki taşır. Dolaylı etkinin skora katılma oranı `spillover.weight` ile ayarlanır (varsayılan 0,5). `BEA_API_KEY` yoksa yalnızca doğrudan etki hesaplanır.

### Canlı tutma

`keepalive.yml` ayın 1'inde ve 15'inde çalışır. Radar iş akışlarını etkin tutar ve ayda bir küçük bir commit atar. Bu sayede GitHub'ın 60 gün kuralı zamanlanmış işleri kapatmaz.

## 3. Saatler neden iki kez tetikleniyor?

GitHub cron UTC ile çalışır ve yaz/kış saatini bilmez. Bu yüzden her slot için iki tetikleyici vardır. Kod New York saatini kontrol eder. Yalnızca doğru pencerede ve günde bir kez çalışır. Fazla tetiklemeler birkaç saniyede biter. GitHub zamanlanmış işleri bazen geciktirir. Pencereler (150–180 dakika) bu gecikmeyi tolere eder.

---

## 4. Sistem kendini nasıl geliştirir?

| Döngü | Ne öğrenir? | Ne zaman? |
|---|---|---|
| **Tahmin defteri + kalibrasyon** | Akış sürprizi → "SPY'ı geçme" olasılığı ilişkisi (lojistik a, b) ve sektör sapması | Haftalık |
| **Olay çalışması (CAR)** | Hangi program türü piyasada gerçekten fark yaratıyor? → program etkinlik çarpanı (0,6–1,4) | Haftalık |
| **Kendini eğiten sınıflandırıcı** | Sizin etiketleriniz + kuralın çok emin olduğu örnekler → TF-IDF + lojistik regresyon | Haftalık |
| **Varlık eşleştirme** | Onayladığınız her ad → ticker eşleşmesi kalıcı olur | Her çalışmada |
| **Drift izleme** | Program dağılımı değişimi (PSI) ve son 60 tahminde kötüleşme | Her çalışmada |

Modelin ağırlığı etiket sayısıyla artar: 0 etikette %10, 400+ etikette %60. Az etiketle model kurallara baskın gelmez.

**Model karnesi** sekmesi sistemin dürüst puanını gösterir. Beceri skoru 0'ın üstündeyse sistem taban orandan iyidir. Altındaysa sinyali henüz yoktur. Bu bilgi de değerlidir.

---

## 5. Yeni program veya kaynak eklemek

- **Yeni teşvik programı:** `config/programs.yaml` dosyasına bir blok ekleyin: anahtar kelimeler, sektör payları, teşvik türü. Kod değişikliği gerekmez.
- **Yeni SEC ifadesi:** `config/sources.yaml` → `sec_edgar.phrases` altına `'"ifade"': program_kodu` ekleyin.
- **Yeni haber sorgusu:** `config/sources.yaml` → `gdelt.queries`.
- **Yeni kaynak:** `src/radar/sources/` altına `fetch(since) -> DataFrame` sunan bir modül yazın. Ortak şema `src/radar/schema.py` içindedir.

---

## 6. Yerel çalıştırma

```bash
pip install -r requirements-etl.txt -r requirements.txt
PYTHONPATH=src python -m radar.run demo --days 200      # internetsiz demo
PYTHONPATH=src python -m radar.run slot --slot open --force
streamlit run app/streamlit_app.py
PYTHONPATH=src pytest -q tests
```

---

## 7. Sınırlamalar

- **Fiyat verisi** resmi bir kaynaktan gelmez (yfinance, yedek: Stooq). Sistem fiyat olmadan da skor üretir. Yalnızca puanlama ve olay çalışması ertelenir.
- **Vergi kredileri** (45X, 48E, ...) USAspending'de görünmez. Bunlar Federal Register rehberleri ve SEC bildirimleriyle izlenir. Tutarları tahminidir.
- **Tutarı bilinmeyen belgeler** (tasarı, kural) varsayılan tutarla ve geniş belirsizlikle hesaplanır. Aynı programdaki tekrarlar azalan ağırlık alır.
- **Ad eşleştirme** hatasız değildir. Belirsiz eşleşmeler otomatik kabul edilmez, onayınıza gelir.
- **Nedensellik sınırlıdır.** Skorlar karar desteği içindir. Yatırım tavsiyesi değildir.
- Ücretsiz katman limitleri değişebilir. Limitleri `config/sources.yaml` ve `src/radar/http.py` içinde güncelleyin.
