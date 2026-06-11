# 📋 Regional Markets Screener — SOP

## Tujuan
Mengumpulkan data pasar regional (US, Europe, Asia, Indonesia, komoditas, currency, bonds)
setiap hari kerja pukul 05:00 WIB dan mengirimkan laporan terformat ke Telegram.

## Pipeline
```
regional_pipeline.py (Hermes script, no_agent mode)
  └─▶ regional_market_report.py  →  output/regional_report.md
  └─▶ baca file → stdout (clean report) → Telegram
```

Script `regional_pipeline.py` di `~/AppData/Local/hermes/scripts/` jalanin pipeline,
baca output dari `output/regional_report.md`, kirim report bersih (tanpa progress text).

## Cara Menjalankan
```powershell
cd C:\Users\satri\code\regionaldatacollector

# Scrape fresh + format (butuh ~50-60 detik)
.\venv\Scripts\python.exe regional_market_report.py

# Dari cache (instant)
.\venv\Scripts\python.exe regional_market_report.py --from-cache

# JSON doang (debug)
.\venv\Scripts\python.exe regional_market_report.py --json-only
```

## Cron
- **Waktu:** Setiap hari kerja pukul **05:30 WIB**
- **Job name:** Regional Markets Screener + News
- **Mode:** Agent-based — jalanin pipeline, baca output dari `output/regional_report.md`, kirim report bersih
- **Prompt:** Jalankan pipeline, baca file report, kirim mentah-mentah (tanpa tambahan teks)
- **Workdir:** `C:\Users\satri\code\regionaldatacollector`
- ⚡ Output diambil dari file, **bukan stdout pipeline langsung** (menghindari progress text)

## Dependencies
- curl_cffi — HTTP requests with impersonation
- beautifulsoup4 — HTML parsing
- lxml — XML/HTML parser

Semua terinstall di `venv/`

## File Structure
```
regionaldatacollector/
├── regional_market_report.py      # ALL-IN-ONE: scraper + formatter
├── requirements.txt               # Python dependencies
├── SOP.md                         # This file
├── cache/
│   └── regional_raw.json          # Cached raw data (untuk --from-cache)
├── output/
│   ├── regional_report.md         # Formatted Markdown report
│   ├── regional_report.pdf        # Clickable PDF export
│   └── regional_report.png        # Markdown preview image export
└── venv/                          # Virtual environment
```

Sisa file lama (regional_screener.py, format_regional_report.py) sudah dihapus.

## Layout Output
1. Header: 📊 Regional Markets Screener + tanggal
2. **📰 Market News Summary** — US indices (Dow, S&P 500, Nasdaq) + Top 5 news live dari Google News RSS
3. VIX, WTI, Brent, big movers (KOSPI >5%, Nikkei >2%)
4. 🇺🇸 **US Indices** — Dow, S&P 500, Nasdaq, VIX
5. 🇪🇺 **Europe** — DAX, FTSE, CAC, Euro Stoxx 50, FTSE MIB, SMI
6. 🌏 **Asia** — Nikkei, Shanghai, SZSE, HSI, Taiwan, KOSPI, Nifty 50, SET, ASX 200, PSEi
7. 🇮🇩 **Indonesia** — IDX 🔥, LQ45, Kompas 100, Jisdor, 12 sektor IDX vertikal, ICBI
8. 💵 **FX & Bonds** — USD/IDR, EUR/USD, DXY, US10Yr/US2Yr/US30Yr, Indo10Yr, IndoCDS 5yr
9. 🛢️ **Energy** — WTI, Brent, Nat Gas, Coal Newcastle + Rotterdam (4 kontrak per lokasi)
10. 🏗️ **Metals & Mining** — Gold, Silver, Copper, Nickel, Timah, Aluminium, Iron Ore 62%, BCOMIN
11. 🌿 **Komoditas Lain** — CPO, Woodpulp, Ammonia (Yuan/ton), Corn, Wheat, Soybean Oil
12. 📈 **ETFs & Stocks** — EIDO, TLKM, EEM
13. **Footer** — Broker Code: AT, Desy Erawati/ DE, Source: Bloomberg/Investing/IBPA/CNBC/Bursa Malaysia, Copy;right: Phintraco Sekuritas

## Catatan
- **1 file all-in-one** — gak ada lagi subprocess/encoding crash
- **% otomatis** — fmt() dan get_change() normalize "%" dari Investing (angka doang) dan Yahoo (sudah pake %)
- **News live** — dari Google News RSS tiap kali report dijalankan
- **Coal** — format kontrak per bulan (Jun/Jul/Aug/Sep) untuk Newcastle dan Rotterdam
- **Bogus values** — Yahoo Finance kadang return change/pct yang salah → auto-filter (change > 10% price tapi pct < 1% = discard)
- **Jisdor** — dari BI langsung (regex `Rp(\d{2,3}\.\d{3})`)
- **IndoCDS** — dari WorldGovernmentBonds API (POST ke /wp-json/common/v1/historical)
- **Iron Ore 62%** — kalo change_pct = 0, gak ditampilkan (bersih)
- **Cache** — `--from-cache` pake `cache/regional_raw.json`, instant tanpa scraping
