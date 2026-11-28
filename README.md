# Piyasa

Windows için fiyat izleme ve portföy takibi uygulaması.

## Windows’ta çalıştırma

```powershell
python -m pip install -r requirements.txt
python main.py
```

## Proje yapısı

| Konum | İçerik |
| --- | --- |
| `main.py` | Uygulama giriş noktası |
| `piyasa/widget.py` | Masaüstü widget’ı ve uygulama akışı |
| `piyasa/market/` | Fiyat verisi, izleme listesi ve SQLite fiyat geçmişi |
| `piyasa/portfolio/` | İşlem kayıtları, yedekleme ve portföy hesapları |
| `piyasa/ui/` | Tkinter pencereleri, bileşenler ve grafik çizimi |
| `piyasa/windows.py` | Windows başlangıç kaydı ve uygulama güncellemeleri |
| `piyasa/assets/` | Uygulama varlıkları |
| `tests/`, `installer/`, `docs/` | Testler, Windows kurucusu ve arayüz prototipi |

Kaynak koddan çalıştırıldığında uygulama verileri proje kökünde tutulur. Kurulu sürüm mevcut kurulumun `_internal` dizinini kullanır. İşlem kayıtları, ayarlar ve fiyat önbelleği Git dışında tutulur.

## Testler

```powershell
python -B -m unittest discover -s tests
```

Tkinter arayüz testleri etkileşimli bir Windows masaüstü gerektirir.
