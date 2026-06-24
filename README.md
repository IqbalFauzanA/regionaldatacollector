# Regional Markets Screener

Automated market briefing pipeline for Phintraco Sekuritas. The script collects
regional market data, adds live market-news headlines, formats the result into a
Telegram/WhatsApp-ready report, and exports a PDF copy.

The project is intentionally a single-command workflow: run the collector, read
the generated clean report from `output\regional_report_whatsapp.txt`, then send
that text through the delivery channel.

---

## What It Does

- Collects 100+ market data points across US, Europe, Asia, Indonesia, FX,
  bonds, energy, metals, commodities, ETFs, and selected stocks.
- Fetches the top 5 US market headlines from Google News RSS on each run.
- Normalizes inconsistent source formats, including percent signs, point
  changes, zero values, and occasional bad Yahoo Finance values.
- Writes reusable raw data to `cache\regional_raw.json`.
- Produces both terminal output and clean delivery files under `output\`.
- Supports a Windows EXE build for scheduled or non-Python environments.

---

## Requirements

- Windows PowerShell
- Python 3.10+
- Internet access for live scraping
- Optional: Git for cloning the repo

Python packages are listed in `requirements.txt`:

| Package | Purpose |
| --- | --- |
| `curl_cffi` | HTTP requests with browser impersonation |
| `beautifulsoup4` | HTML parsing |
| `lxml` | XML/HTML parser |
| `pillow` | Image/font support used by exports |
| `reportlab` | PDF export |
| `pyinstaller` | Windows EXE build |

---

## Setup

```powershell
git clone <repo-url> regionaldatacollector
cd regionaldatacollector

python -m venv .venv
.\.venv\Scripts\activate

python -m pip install -r requirements.txt
```

If the repo already exists, start from the project root and run:

```powershell
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

---

## Run The Report

Run commands from the project root.

| Command | Use case |
| --- | --- |
| `python regional_market_report.py` | Fresh scrape, formatted report, cache update, PDF export |
| `python regional_market_report.py --from-cache` | Reformat the latest cached data without scraping |
| `python regional_market_report.py --json-only` | Print raw JSON for debugging or downstream processing |
| `python regional_market_report.py --partial-cache` | Merge fresh valid data with recently cached values when a source fails |
| `python regional_market_report.py --debug` | Show debug logging while scraping |

Recommended Windows command:

```powershell
.\.venv\Scripts\python.exe regional_market_report.py
```

The fresh scrape usually takes about 45-60 seconds, depending on source
latency.

---

## Outputs

Every normal run prints the Markdown report to stdout and writes delivery files:

```text
cache\regional_raw.json
output\regional_report_whatsapp.txt
output\regional_report.pdf
```

Use `output\regional_report_whatsapp.txt` for Telegram/WhatsApp delivery. It is
the clean report text with progress logs removed and Markdown converted into a
plain-text-friendly format.

Use `output\regional_report.pdf` as the formatted archive/shareable version.

---

## Report Layout

| Section | Contents |
| --- | --- |
| Header | Greeting and current report date |
| Market News Summary | Top 5 Google News RSS market headlines |
| US Indices | Dow, S&P 500, Nasdaq, S&P 500 VIX |
| Europe | DAX, FTSE, CAC |
| Asia | Nikkei, Shanghai, HSI, KOSPI, STI |
| Indonesia | IDX, LQ45, Kompas 100, IDX30, IDX sector indices |
| Indonesia Rates & Credit | USD/IDR, Jisdor, Indo10Yr, ICBI, IndoCDS 5yr |
| FX & Bonds | EUR/USD, DXY, US2Yr, US10Yr, US30Yr |
| Energy | WTI, Brent, natural gas |
| Coal | Newcastle and Rotterdam futures contracts |
| Metals & Mining | Gold, silver, copper, nickel, tin, aluminium, iron ore, BCOMIN |
| Other Commodities | CPO, woodpulp, ammonia, corn, wheat, soybean oil |
| ETFs & Stocks | EIDO, TLKM, EEM |
| Footer | Broker code, preparer, sources, copyright |

Large moves are emphasized automatically by the formatter:

- Absolute percent move above 2%: bold.
- Absolute percent move above 3%: bold plus alert marker.
- Suspicious outlier data can be suppressed before formatting.

---

## Daily SOP

### Objective

Collect regional market data each workday before market open and send a clean
formatted report to Telegram.

### Schedule

| Item | Value |
| --- | --- |
| Time | Every workday at 05:30 WIB |
| Job name | Regional Markets Screener + News |
| Workdir | `C:\Users\satri\code\regionaldatacollector` |
| Delivery source | `output\regional_report_whatsapp.txt` |

### Pipeline

```text
regional_pipeline.py
  -> regional_market_report.py
  -> output\regional_report_whatsapp.txt
  -> Telegram
```

Operational notes:

- The Hermes script lives in `~/AppData/Local/hermes/scripts/`.
- The delivery job should read from `output\regional_report_whatsapp.txt`, not
  directly from stdout, so progress text and warnings are not sent to Telegram.
- The prompt for an agent-based schedule should be: run the pipeline, read the
  report file, and send the report exactly as written without extra commentary.
- For a quick retry after a successful scrape, use `--from-cache`.

---

## Build The Windows EXE

Install dependencies first, then build from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m PyInstaller --onefile --clean --name regional_market_report regional_market_report.py
```

The executable is created at:

```text
dist\regional_market_report.exe
```

When running as an EXE, the app resolves `cache\` and `output\` beside the EXE
when possible. It also enables partial-cache behavior by default, so a temporary
source failure can reuse recently cached values instead of producing a sparse
report.

---

## Project Structure

```text
regionaldatacollector/
|-- regional_market_report.py       # CLI entrypoint, cache handling, exports
|-- regional_report/
|   |-- commons.py                  # Shared paths, fetch helper, validation
|   |-- parsers.py                  # Scrapers and data collection orchestration
|   |-- formatters.py               # Markdown and WhatsApp report formatting
|   `-- exports.py                  # PDF export helpers
|-- requirements.txt                # Python dependencies
|-- README.md                       # Project manual
|-- cache/                          # Generated raw cache
|-- output/                         # Generated report files
|-- build/                          # Generated PyInstaller build files
`-- dist/                           # Generated EXE output
```

Generated folders can be recreated by running the pipeline or EXE build. The
source files that normally matter for maintenance are `regional_market_report.py`
and the modules under `regional_report\`.

---

## Data Sources And Parsing Notes

- Google News RSS supplies the live market headlines.
- Bank Indonesia supplies Jisdor data.
- WorldGovernmentBonds supplies IndoCDS data through
  `/wp-json/common/v1/historical`.
- Barchart supplies Newcastle and Rotterdam coal futures.
- Bursa Malaysia supplies CPO data.
- Yahoo Finance and Investing-style pages supply many index, FX, ETF, stock,
  commodity, and rates values.
- `curl_cffi` is preferred for modern TLS and browser impersonation; the code
  falls back to `requests` when needed in development.
- Per-host concurrency limits are used to avoid overwhelming source sites.
- Cache fallback is intentionally conservative: cached values are reused only
  when the current item is invalid and the cached item is recent enough.

---

## Maintenance Notes

- Keep all file I/O in UTF-8.
- Add new instruments in `regional_report\parsers.py`, then include display
  behavior in `regional_report\formatters.py`.
- Use `is_valid_data()` before showing optional data to avoid empty report rows.
- Keep delivery automation pointed at `output\regional_report_whatsapp.txt`.
- If source pages change structure, update the related parser only; avoid
  changing formatter behavior unless the report layout must change.

---

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Report has missing rows | Run with `--debug`, then retry with `--partial-cache` |
| Telegram text includes progress logs | Read `output\regional_report_whatsapp.txt` instead of stdout |
| PDF export fails | Confirm `reportlab` and `pillow` are installed |
| EXE writes files in the wrong folder | Run it from the intended working directory or place it in the target folder |
| Cached report is stale | Run without `--from-cache` to scrape fresh data |

---

## License

Internal use: Phintraco Sekuritas.
