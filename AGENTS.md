# AGENTS.md — Developer & AI Agent Guide

Welcome, Agent! This guide is designed to help you navigate, understand, and develop within the **Regional Markets Screener** codebase quickly, accurately, and with **maximum token/credit efficiency**.

---

## ⚡ 1. Token & Credit Efficiency Rules (Read This First!)

To minimize unnecessary context window bloat and reduce token/credit consumption:

1. **Read ONLY the specific parser module you need in `regional_report/parsers/`.**
   - The parsers have been modularized into `regional_report/parsers/` (`bloomberg.py`, `cnbc.py`, `investing.py`, `yahoo.py`, `indonesia.py`, `sunsirs.py`, `barchart.py`, `bursa.py`, `news.py`, `constants.py`, `collector.py`).
   - Each module is small (~50–200 lines). **NEVER** open multiple parser modules at once or dump unnecessary files into context.
   - Simply view the specific provider file you need (e.g. `parsers/bloomberg.py` or `parsers/sunsirs.py`).

2. **NEVER run live scraping to test formatting, export, or logic changes.**
   - Running `python regional_market_report.py` without flags triggers network calls across dozens of financial websites, taking 45–60 seconds, risking IP rate-limits (403/429), and cluttering your tool output.
   - **Instead**:
     - Fast format/export testing: `$env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe regional_market_report.py --from-cache` (runs in < 1s using existing cached data).
     - Isolated parser testing: Run unit tests with `.\.venv\Scripts\python.exe -m unittest discover tests` (30+ tests complete in ~0.02s!).
     - Stale/partial scraper testing: `.\.venv\Scripts\python.exe regional_market_report.py --partial-cache`.

3. **NEVER dump raw cache files or generated artifacts into context.**
   - Do not open `cache/regional_raw.json` or `output/` files unless inspecting a specific field.
   - If inspecting cached keys, use a fast one-liner:
     ```powershell
     .\.venv\Scripts\python.exe -c "import json; d=json.load(open('cache/regional_raw.json'))['data']; print(list(d.keys()))"
     ```

4. **Ignore build and binary directories.**
   - Avoid indexing, searching, or opening files in `build/`, `dist/`, `.venv/`, `__pycache__/`, or `.git/`.

---

## 🛠️ 2. Environment & Quick Commands

- **OS**: Windows (PowerShell)
- **Virtualenv**: `.\.venv\`
- **Python Executable**: `.\.venv\Scripts\python.exe` (Always use this executable to ensure dependencies are resolved).

### Primary Commands

> [!TIP]
> On Windows PowerShell, the console defaults to `cp1252`. When printing terminal reports containing emojis, prefix with `$env:PYTHONIOENCODING="utf-8";` to prevent `UnicodeEncodeError`.

| Task                             | Command                                                                                                               | When to use                                                     |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **Run Unit Tests**               | `.\.venv\Scripts\python.exe -m unittest discover tests`                                                               | Before and after any code change (runs in 0.02s).               |
| **Run Single Test File**         | `.\.venv\Scripts\python.exe -m unittest tests/test_requested_sources.py`                                              | When updating a specific parser.                                |
| **Test Output / Formatter**      | `$env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe regional_market_report.py --from-cache`                    | Instant re-generation of output text and PDF.                   |
| **Inspect JSON Output**          | `.\.venv\Scripts\python.exe regional_market_report.py --json-only`                                                    | Validating the parsed data dictionary structure.                |
| **Debug Scraper Issues**         | `$env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe regional_market_report.py --debug`                         | Investigating network or parsing failures with verbose logging. |
| **Build Standalone Windows EXE** | `.\.venv\Scripts\python.exe -m PyInstaller --onefile --clean --name regional_market_report regional_market_report.py` | Packaging executable for production.                            |

---

## 🏛️ 3. Codebase Architecture & File Map

```text
regionaldatacollector/
├── regional_market_report.py   # CLI entry point, cache handling, output dispatching
├── regional_report/            # Core package
│   ├── commons.py              # Paths, HTTP client (curl_cffi), host semaphores, validation
│   ├── parsers/                # Modular scraper package
│   │   ├── __init__.py         # Package exports & backwards-compatibility facade
│   │   ├── base.py             # Shared table parser, fetch dispatcher, label helpers
│   │   ├── constants.py        # Target keys & REQUESTED_SOURCE_BY_KEY mappings
│   │   ├── bloomberg.py        # Bloomberg quotes & Next.js page props scrapers
│   │   ├── cnbc.py             # CNBC quote scrapers
│   │   ├── investing.py        # Investing.com futures & instrument pages
│   │   ├── yahoo.py            # Yahoo Finance indices & chart APIs
│   │   ├── indonesia.py        # PHEI, JISDOR, Indo Bonds & Indo CDS
│   │   ├── sunsirs.py          # SunSirs Ammonia & Woodpulp
│   │   ├── barchart.py         # Barchart Coal futures & contract months
│   │   ├── bursa.py            # Bursa Malaysia CPO
│   │   ├── news.py             # Google News RSS scraper
│   │   └── collector.py        # Concurrent collection orchestrator (collect_data)
│   ├── formatters.py           # Markdown & WhatsApp/Telegram plain-text formatters
│   └── exports.py              # ReportLab PDF generator & text sanitizers
├── tests/                      # Unit test suite (Mock-based, fast offline tests)
│   ├── test_commons.py         # Network retry & semaphore tests
│   ├── test_exports.py         # Emoji sanitization & ReportLab inline markup tests
│   └── test_requested_sources.py # Parser tests with static HTML/JSON fixtures
├── cache/                      # Ignored in git: regional_raw.json
├── output/                     # Ignored in git: delivery reports
│   ├── regional_report_whatsapp.txt # Clean plain-text formatted for Telegram/WhatsApp
│   └── regional_report.pdf          # Formatted PDF document
├── requirements.txt            # Python dependencies
└── README.md                   # User documentation and operational SOP
```

### Module Responsibilities

1. **`regional_market_report.py`**:
   - Parses CLI flags (`--from-cache`, `--partial-cache`, `--json-only`, `--debug`, `--verbose`).
   - Resolves cache loading (`_load_cache`) and intelligent cache merging (`_merge_partial_cache`).
   - Fetches Google News RSS (`fetch_market_news(5)`).
   - Coordinates formatting and triggers export writes (`_save_outputs`).

2. **`regional_report/commons.py`**:
   - `fetch(url, impersonate=..., timeout=..., headers=...)`: Wraps `curl_cffi.requests` with automatic browser TLS fingerprint impersonation (`chrome120`, `firefox`, etc.) and fallbacks on 403/429.
   - `_get_host_semaphore(url)`: Enforces per-host concurrency limits (`HOST_LIMITS`) to avoid IP bans (e.g., Bloomberg max 1 worker).
   - `is_valid_data(d)`: Validates parsed metrics (checks for non-empty price/close or contract prices).
   - `strip_preview_emoji(text)`: Removes multi-byte unicode emoji sequences and variation selectors (`\ufe0f`) to prevent ReportLab font crashes.

3. **`regional_report/parsers/`**:
   - Clean, modular provider-specific parsers (`bloomberg.py`, `cnbc.py`, `investing.py`, `yahoo.py`, `indonesia.py`, `sunsirs.py`, `barchart.py`, `bursa.py`, `news.py`).
   - `constants.py`: Authoritative dictionary mapping each data key to its required source provider (`REQUESTED_SOURCE_BY_KEY`).
   - `collector.py`: Orchestrates concurrent scraper execution (`collect_data(...)`) via `ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS)` and handles partial cache fallback.

4. **`regional_report/formatters.py`**:
   - `format_report(data, market_news)`: Generates structured Markdown.
   - `format_report_whatsapp(report)`: Strips markdown headers and converts formatting into WhatsApp/Telegram friendly styles (`*bold*`, `_italic_`, bullets `•`, alert emojis `‼️` for moves > 3%).

5. **`regional_report/exports.py`**:
   - Generates `regional_report.pdf` using ReportLab.
   - Font management: Bundled Inter font (`fonts/`) with fallback to Helvetica.

---

## 📊 4. Data Model Conventions

Parsed instrument data is stored as a dictionary of key-value pairs in `data`:

### Standard Quote Format

```python
{
    "close": "45295.81",      # Required: Last/close price as string
    "change": "-249.07",      # Optional: Absolute point change with sign (+/-)
    "change_pct": "-0.55%",   # Optional: Percentage change formatted with '%' and sign (+/-)
    "source": "CNBC",         # Required: Source label matching REQUESTED_SOURCE_BY_KEY
    "fetched_at": "2026-09-06T16:22:18.338183"
}
```

### Multi-Contract / Futures Format (e.g., Coal)

```python
{
    "contracts": [
        {
            "contract": "Oct 26",
            "price": "113.65",
            "change": "+0.50",
            "change_pct": "+0.44%"
        },
        ...
    ],
    "source": "Barchart",
    "fetched_at": "2026-09-06T16:22:18.338183"
}
```

---

## 🔄 5. SOP for Common Development Tasks

### Task A: Updating or Fixing a Parser (e.g. Website HTML Changed)

1. **Find the Parser**: Open the corresponding module in `regional_report/parsers/` (e.g., `bloomberg.py`, `cnbc.py`, `sunsirs.py`).
2. **Inspect Code**: Because modules are small (~50–200 lines), you can view the entire module without context bloat.
3. **Write Unit Tests First**: Open `tests/test_requested_sources.py`. Notice how HTML fixtures are passed directly to the parser function (e.g., `parse_cnbc_quote_html(html, "Dow")`).
4. **Implement the Fix**: Update parsing logic (handling selector changes, regex tweaks, or JSON payload changes).
5. **Verify**: Run `.\.venv\Scripts\python.exe -m unittest tests/test_requested_sources.py`.

### Task B: Adding a New Financial Instrument

1. **Add Scraper**: Implement the parser function in `regional_report/parsers/<provider>.py` (or a new module if adding a new provider).
2. **Register Source**: Add entry to `REQUESTED_SOURCE_BY_KEY` in `regional_report/parsers/constants.py`:
   ```python
   REQUESTED_SOURCE_BY_KEY["New Instrument"] = "Source Provider"
   ```
3. **Register Task in `collect_data()`**:
   - Add the task tuple to `tasks` or `single_pages` in `collect_data()` in `regional_report/parsers/collector.py`.
4. **Re-export in `__init__.py`**: Add any new public symbols to `regional_report/parsers/__init__.py`.
5. **Update Formatter**: Add display handling in `regional_report/formatters.py` within the relevant section (e.g., Metals, Commodities, Asia Indices).
6. **Add Unit Test**: Add test case in `tests/test_requested_sources.py` using sample HTML/payload.
7. **Verify End-to-End**: Test with `python regional_market_report.py --from-cache` or run tests.

### Task C: Modifying Report Formatting or Layout

1. **Locate Target Section**: Search `regional_report/formatters.py` for the section name (e.g., `*🇺🇸 US Indices*`, `Energy`, etc.).
2. **Edit**: Modify layout or thresholds (e.g., alert marker threshold `> 3%`).
3. **Verify Instantly**:
   ```powershell
   $env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe regional_market_report.py --from-cache
   ```
4. Check `output\regional_report_whatsapp.txt` to confirm formatting is clean.

### Task D: Adjusting PDF Styling

1. Edit `regional_report/exports.py`.
2. **CRITICAL**: Never pass un-sanitized strings or raw emoji to ReportLab. ReportLab will either fail or render stray dingbat characters (`n`). Ensure `strip_preview_emoji()` is applied.
3. Run `.\.venv\Scripts\python.exe -m unittest tests/test_exports.py`.

---

## ⚠️ 6. Important Pitfalls & Constraints

1. **UTF-8 Everywhere**: Always specify `encoding="utf-8"` when reading or writing files in Python.
2. **HTTP Impersonation**: Never replace `commons.fetch()` with bare `requests.get()` or `urllib`. Financial portals (Bloomberg, Investing.com) employ Cloudflare/anti-bot protection that blocks standard Python user-agents.
3. **Downstream Delivery Contract**:
   - Automated delivery (Telegram/WhatsApp bots) reads exclusively from `output\regional_report_whatsapp.txt`.
   - Never write progress logs or debug messages to `stdout` without redirecting them to `stderr` (`file=sys.stderr`).
4. **Git Hygiene**: Never check in files in `cache/`, `output/`, `build/`, `dist/`, or `.spec` files unless explicitly requested.
5. **Source Links and Data Providers Are Absolute**:
   - All data sources and URLs specified in `REQUESTED_SOURCE_BY_KEY` and parser modules are **absolute and non-negotiable** unless explicitly instructed otherwise.
   - If an authoritative provider blocks requests (e.g. Bloomberg returning HTTP 403), **never** swap out or substitute the provider with an alternative source (e.g. Yahoo Finance, Investing.com) just to retrieve a valid value.
   - It is **expected and preferred** to leave the instrument as `[Fetch Failed]` rather than switching to an unauthorized provider.
   - Only implement fixes if there is a viable workaround that preserves and continues using the **exact same source** (e.g. adjusting headers, TLS impersonation, or fixing DOM selectors on that domain).
6. **No Dead or Unreachable Code (Verify Applied Edits)**:
   - When editing code, always inspect the applied changes via `git diff` or `view_file` to ensure tool replacements do not leave behind unreachable statements (e.g. early `return` preceding log/exception statements, duplicate checks, or orphaned code blocks).
   - Code must remain clean, reachable, and free of dead logic.

