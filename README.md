# Noter Araç İşlemleri — FINAL 3.2 SEO

Tek Vercel projesi içinde iki araç:

- `/satis` — Noter Araç Satış Ücreti Hesaplama
- `/kasko` — KaskoAI kasko değeri / kasko kodu bulma
- `/` — Mobil öncelikli ana menü

## Araç satış ücreti hesabı

2026 parametreleri `app.py` içindeki `FEE_CONFIG` bölümündedir:

- Harç oranı: %0,2
- Asgari harç: 1.000,00 TL
- Noter ücreti: 528,63 TL
- ARTES tescil: 350,92 TL
- KDV: noter + ARTES toplamının %20'si
- Darphane: 36,00 TL
- Tescil belge bedeli: 1.511,00 TL
- Yetki belgesi varsa harç: 0 TL
- Harç matrahı: satış bedeli / kasko bedelinden yüksek olan

Her yıl 1 Ocak'ta yalnızca `FEE_CONFIG` değerlerini güncellemek yeterlidir.

## KaskoAI entegrasyonu

Satış ekranındaki **KaskoAI ile Bul** düğmesi `/kasko?picker=1` ekranını modal içinde açar.
Kasko sonucu bulunduğunda **Bu Değeri Kullan** ile:

- Kasko değeri satış hesaplama formuna aktarılır.
- Kasko kodu gösterilir.
- Araç bilgisi gösterilir.

## Aylık kasko listesi

`data/kasko_guncel*.csv` dosyaları kullanılır. En son değiştirilen dosya otomatik seçilir.

## SEO

Sitemap:
- `/`
- `/satis`
- `/kasko`

Google Search Console doğrulama etiketi korunmuştur.


## FINAL 3.1 rötuşları

- Satış sonucunda harç matrahının hangi bedelden geldiği ayrıca gösterilir.
- Hesaplama düğmesinde işlem sırasında görsel durum bilgisi bulunur.
- Satış ve kasko alanlarında Enter ile hesaplama yapılabilir.
- KaskoAI ile aktarılan değer, kullanıcı elle değiştirmediği sürece kasko kodu/araç bilgisiyle birlikte korunur.
- Mobil modal ve seçili kasko bilgi kartı iyileştirildi.
- Hareket azaltma tercihi olan cihazlar için erişilebilirlik desteği eklendi.
- Aylık güncellemede öncelik artık `data/kasko_guncel.csv` sabit dosya adındadır. Bu dosya yoksa eski `kasko_guncel*.csv` yöntemi yedek olarak çalışır.


## FINAL 3.2 SEO

Canlı alan adı:
`https://noter-arac-islemleri.vercel.app/`

SEO düzenlemeleri:
- Ana sayfa, satış ve kasko sayfası için birbirinden farklı title ve meta description.
- Tüm canonical URL'ler yeni canlı alan adına taşındı.
- Open Graph ve Twitter paylaşım meta etiketleri tamamlandı.
- WebSite / WebApplication JSON-LD yapılandırılmış verileri eklendi.
- robots.txt içindeki sitemap adresi yeni alan adına taşındı.
- sitemap.xml içinde `/`, `/satis`, `/kasko` canonical adresleri bulunuyor.
- Google Search Console HTML doğrulama etiketi mevcut projeden korunmuştur. Yeni mülk doğrulamasında Google farklı bir etiket verirse ilgili meta etiketini değiştirmek yeterlidir.

Search Console'a gönderilecek sitemap:
`https://noter-arac-islemleri.vercel.app/sitemap.xml`
