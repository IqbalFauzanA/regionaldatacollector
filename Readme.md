# 📊 Regional Markets Screener

Automated daily market data pipeline — scrapes **118+ data points** across US, Europe, Asia, Indonesia indices, FX, bonds, energy, metals, and commodities, then formats into a clean briefing ready for Telegram delivery.

---

## ✨ Features

- **116+ data points** — Major indices, sector indices, FX, bonds, energy, coal (futures), metals, commodities, ETFs
- **Live news** — Top 5 US market headlines from Google News RSS
- **Smart formatting** — `%` normalization, zero-value suppression, big mover alerts (🚀📉)
- **Caching** — `--from-cache` re-formats instantly without re-scraping
- **No subprocess** — Single Python process, no encoding issues on Windows
- **Telegram-ready** — Output formatted with markdown for direct delivery

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- Git (optional, for cloning)

### Installation

```powershell
# 1. Clone or create folder
git clone <repo-url> regionaldatacollector
cd regionaldatacollector

# 2. Create virtual environment
python -m venv venv

# 3. Activate it
.\venv\Scripts\activate

# 4. Install dependencies
pip install -r requirements.txt
```

### Run

```powershell
# Scrape fresh data + format report (takes ~45-60 seconds)
python regional_market_report.py

# From cache (instant — uses saved JSON)
python regional_market_report.py --from-cache

# JSON output only (for debugging / custom processing)
python regional_market_report.py --json-only
```

---

## 📁 File Structure

```
regionaldatacollector/
├── regional_market_report.py     # 🧠 All-in-one: scraper + formatter
├── requirements.txt              # Python dependencies
├── Readme.md                     # This file
├── SOP.md                        # Standard Operating Procedure
├── prompt.txt                    # Prompt template for AI tools
├── cache/
│   └── regional_raw.json         # Cached raw data (for --from-cache)
├── output/
│   ├── regional_report.md        # Last formatted Markdown report
│   ├── regional_report.pdf       # Clickable PDF export
│   └── regional_report.png       # Markdown preview image export
└── venv/                         # Python virtual environment
```

---

## 📋 Output Sections

| Section | Contents |
|---------|----------|
| **📰 Market News Summary** | Dow, S&P 500, Nasdaq closes + 5 live news headlines |
| **🇺🇸 US Indices** | Dow, S&P 500, Nasdaq, VIX |
| **🇪🇺 Europe** | DAX, FTSE, CAC, Euro Stoxx 50, FTSE MIB, SMI |
| **🌏 Asia** | Nikkei, Shanghai, SZSE, HSI, Taiwan, KOSPI, Nifty 50, SET, ASX 200, PSEi |
| **🇮🇩 Indonesia** | IDX, LQ45, Kompas 100, Jisdor, 12 sector indices, ICBI |
| **💵 FX & Bonds** | USD/IDR, EUR/USD, DXY, US10Yr/2Yr/30Yr, Indo10Yr, IndoCDS 5yr |
| **🛢️ Energy** | WTI, Brent, Nat Gas, Coal (Newcastle & Rotterdam futures) |
| **🏗️ Metals & Mining** | Gold, Silver, Copper, Nickel, Tin, Aluminium, Iron Ore, BCOMIN |
| **🌿 Komoditas Lain** | CPO, Woodpulp, Ammonia, Corn, Wheat, Soybean Oil |
| **📈 ETFs & Stocks** | EIDO, TLKM, EEM |

---

## 🧠 Using with AI Coding Tools

This pipeline is designed to work with AI-assisted coding tools for enhanced news summarization. The `prompt.txt` provides instructions for tools like:

- **Claude Code** — `claude -p "$(cat prompt.txt)"`
- **OpenCode** — `opencode -m "run pipeline and summarize"`
- **Codex CLI** — `codex -p "$(cat prompt.txt)"`
- **Continue / Cline** — Point to the repo folder

Without AI tools, running `python regional_market_report.py` directly still produces a complete formatted report — but without LLM-powered news summarization. Result can be seen in `output\regional_report.md`, with exports in `output\regional_report.pdf` and `output\regional_report.png`.

---

## 🔧 Dependencies

- [`curl_cffi`](https://github.com/yifeikong/curl_cffi) — HTTP requests with browser impersonation
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [`lxml`](https://lxml.de/) — Fast XML/HTML parser

All installed in `venv/` via `pip install -r requirements.txt`.

---

## 📝 Notes

- **Encoding:** All file I/O uses UTF-8 (Windows cp1252 safe)
- **Coal data:** Futures contracts (Jun/Jul/Aug/Sep) from Barchart for Newcastle & Rotterdam
- **JISDOR:** Directly from Bank Indonesia (`bi.go.id`)
- **IndoCDS:** From WorldGovernmentBonds API
- **% signs:** Auto-normalized — Investing.com returns raw numbers, Yahoo returns with `%`
- **Bogus filter:** Yahoo Finance cross-contaminated data is auto-detected and discarded

---

## 📄 License

Internal use — Phintraco Sekuritas
