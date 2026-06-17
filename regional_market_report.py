#!/usr/bin/env python3
"""
Regional Market Report — Scraper + Formatter (ALL-IN-ONE)
===========================================================
Scrapes ~118 market data points, formats into SOP layout, prints to screen.
No subprocess, no encoding war, no pipe fragility.

Usage:
    python regional_market_report.py                 # scrape fresh + format + save cache
    python regional_market_report.py --from-cache    # use cached JSON (instant)
    python regional_market_report.py --json-only     # scrape fresh, dump JSON only
    python regional_market_report.py --partial-cache # only overwrite cache keys with valid new data

Notes:
    - When bundled as an EXE, the program defaults to partial-cache mode
      so clicking the EXE will enable partial cache without adding CLI args.
"""

import html, json, os, sys, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import BoundedSemaphore
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from typing import Any, cast
import logging

# Module logger: debug messages are off by default; enable with --debug or --verbose
logger = logging.getLogger(__name__)

# Prefer curl_cffi for faster, modern TLS handling. Fall back to requests
# when curl_cffi isn't installed (useful in dev environments).
try:
    import curl_cffi.requests as req
except Exception:
    import requests as _requests

    class _ReqAdapter:
        def __init__(self):
            self._session = _requests.Session()

        def get(self, url, impersonate=None, timeout=15, headers=None, **kwargs):
            hdrs = dict(headers or {})
            if impersonate and "User-Agent" not in hdrs:
                hdrs["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            return self._session.get(url, timeout=timeout, headers=hdrs, **kwargs)

        def post(
            self,
            url,
            data=None,
            json=None,
            impersonate=None,
            timeout=15,
            headers=None,
            **kwargs,
        ):
            hdrs = dict(headers or {})
            if impersonate and "User-Agent" not in hdrs:
                hdrs["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            return self._session.post(
                url, data=data, json=json, timeout=timeout, headers=hdrs, **kwargs
            )

    req = _ReqAdapter()

from bs4 import BeautifulSoup

# PNG/MD export disabled — Pillow imports removed

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

# ──────────────────── Configuration / Globals ────────────────────
# runtime knobs
MAX_FETCH_WORKERS = 8
IMPRERSONATE = "chrome120"
RETRY_IMPRERSONATE = ["chrome120", "chrome119"]

# HTTP headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
ZH_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Per-host concurrency limits (sane defaults; parsers can still work)
HOST_LIMITS = {
    "www.investing.com": 3,
    "query1.finance.yahoo.com": 4,
    "finance.yahoo.com": 4,
    "www.barchart.com": 2,
}

# base paths
# When running as a frozen executable (PyInstaller / similar), place cache
# and output beside the executable so users double-clicking the EXE find the
# generated files in the same folder. Otherwise use the script's directory.
if getattr(sys, "frozen", False):
    # When bundled by PyInstaller --onefile the runtime extracts to a temp
    # folder; `sys.executable` will point into that temp location. Users
    # expect outputs (cache/output) next to the EXE in the dist folder, so
    # prefer the current working directory when the executable appears to be
    # running from a temp extraction directory.
    import tempfile

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cwd = os.getcwd()
    tempdir = tempfile.gettempdir()
    try:
        # If the extracted exe lives under the temp dir, prefer cwd
        if os.path.commonpath([exe_dir, tempdir]) == tempdir or exe_dir.startswith(
            tempdir
        ):
            BASE_DIR = cwd
        else:
            BASE_DIR = exe_dir
    except Exception:
        BASE_DIR = cwd
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_DIR = os.path.join(BASE_DIR, "cache")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# cache file
CACHE_JSON = os.path.join(CACHE_DIR, "regional_raw.json")

# report outputs
REPORT_PDF = os.path.join(OUTPUT_DIR, "regional_report.pdf")
REPORT_WA = os.path.join(OUTPUT_DIR, "regional_report_whatsapp.txt")

# internal semaphores cache
_HOST_SEMAPHORES = {}


def _get_host_semaphore(url: str):
    host = urlparse(url).netloc.lower()
    limit = HOST_LIMITS.get(host, 3)
    sem = _HOST_SEMAPHORES.get(host)
    if sem is None:
        sem = BoundedSemaphore(limit)
        _HOST_SEMAPHORES[host] = sem
    return sem


def fetch(
    url: str,
    impersonate: str | None = None,
    timeout: int = 15,
    headers: dict | None = None,
    max_retries: int = 3,
):
    """Fetch URL with per-host semaphore, impersonation and simple retries.

    Returns the `curl_cffi.requests.Response` or raises the last exception.
    """
    sem = _get_host_semaphore(url)
    last_exc = None
    # Build impersonation rotation list: prefer provided impersonate, then defaults
    if impersonate:
        impersonations = [impersonate] + [
            i for i in RETRY_IMPRERSONATE if i != impersonate
        ]
    else:
        impersonations = [IMPRERSONATE] + [
            i for i in RETRY_IMPRERSONATE if i != IMPRERSONATE
        ]

    for attempt in range(max_retries):
        sem.acquire()
        try:
            try:
                chosen_imp = impersonations[attempt % len(impersonations)]
                # debug trace for impersonation attempts
                # Note: keep lightweight to avoid noisy logs
                logger.debug(
                    "fetch attempt=%d impersonate=%s url=%s", attempt, chosen_imp, url
                )
                resp = req.get(
                    url,
                    impersonate=cast(Any, chosen_imp),
                    timeout=timeout,
                    headers=headers,
                )
                # treat 200 as success
                if getattr(resp, "status_code", None) == 200:
                    return resp

                # On 403/429, try a lightweight fallback: request once without
                # impersonation and with simple headers (may bypass anti-bot).
                if getattr(resp, "status_code", None) in (403, 429):
                    status = getattr(resp, "status_code", None)
                    logger.debug(
                        "fetch received %s for %s (impersonate=%s)",
                        status,
                        url,
                        chosen_imp,
                    )
                    try:
                        alt_headers = dict(headers or {})
                        alt_headers.setdefault("User-Agent", HEADERS.get("User-Agent"))
                        alt_headers.setdefault(
                            "Accept-Language", HEADERS.get("Accept-Language")
                        )
                        logger.debug(
                            "fetch fallback: trying without impersonate for %s", url
                        )
                        alt_resp = req.get(
                            url, impersonate=None, timeout=timeout, headers=alt_headers
                        )
                        alt_status = getattr(alt_resp, "status_code", None)
                        if alt_status == 200:
                            return alt_resp
                        if alt_status not in (403, 429) and alt_status is not None:
                            return alt_resp
                        last_exc = Exception(f"HTTP {alt_status}")
                    except Exception as e:
                        last_exc = e

                    # longer backoff for anti-bot cases before retrying
                    time.sleep(2 + attempt * 2)
                    continue

                return resp
            except Exception as e:
                last_exc = e
                time.sleep(0.5 * (attempt + 1))
                continue
        finally:
            try:
                sem.release()
            except Exception:
                pass

    if last_exc:
        raise last_exc
    raise RuntimeError("fetch failed")


def strip_preview_emoji(s: str) -> str:
    """Remove common emoji and preview markers from a string for PDF/HTML output.

    This is intentionally conservative — it removes obvious emoji codepoints and
    falls back to stripping non-ASCII characters if the regex engine can't handle
    high Unicode ranges on this Python build.
    """
    if not isinstance(s, str):
        return s
    try:
        # Also strip regional indicator symbols (U+1F1E6-U+1F1FF) which form
        # flag emoji like 🇺🇸 — ReportLab fonts cannot render these and they
        # appear as small square boxes in PDFs. Keep the regex conservative.
        return re.sub(
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E6-\U0001F1FF\u2600-\u27BF\U0001F900-\U0001F9FF]",
            "",
            s,
        )
    except re.error:
        # Fallback for narrow builds — strip non-ASCII
        return re.sub(r"[^\x00-\x7F]", "", s)


def has_css_class(element, cls: str) -> bool:
    """Return True if BeautifulSoup element has the given CSS class."""
    try:
        classes = element.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        return cls in (classes or [])
    except Exception:
        return False


# PNG preview helpers removed — PNG export disabled per user request


def reportlab_font() -> str:
    """Return the registered base font name used for body text in PDFs.

    Tries to register bundled fonts under `fonts/` if present; otherwise falls
    back to a standard PDF font.
    """
    try:
        font_regular = os.path.join(BASE_DIR, "fonts", "Inter-Regular.ttf")
        if os.path.exists(font_regular):
            pdfmetrics.registerFont(TTFont("Inter", font_regular))
            return "Inter"
    except Exception:
        pass
    return "Helvetica"


def reportlab_bold_font() -> str:
    """Return the registered bold font name for PDFs (fallback to Helvetica-Bold)."""
    try:
        font_bold = os.path.join(BASE_DIR, "fonts", "Inter-Bold.ttf")
        if os.path.exists(font_bold):
            pdfmetrics.registerFont(TTFont("Inter-Bold", font_bold))
            return "Inter-Bold"
    except Exception:
        pass
    return "Helvetica-Bold"


def markdown_inline_to_reportlab(text: str) -> str:
    """Convert a small subset of Markdown inline elements to ReportLab XML.

    Supports: links [text](url), bold **text**, italic *text* or _text_.
    """
    if not text:
        return ""

    # Extract links first to avoid HTML-escaping their characters
    links: list[tuple[str, str]] = []

    def _link_repl(m: re.Match) -> str:
        links.append((m.group(1), m.group(2)))
        return f"@@LINK{len(links)-1}@@"

    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link_repl, text)

    # Replace strong and emphasis with placeholders that do NOT use underscores
    # (underscores would collide with the italics regex). Use tildes as safe
    # markers and convert them to tags after HTML-escaping.
    s = re.sub(r"\*\*(.+?)\*\*", lambda m: "~~BOPEN~~" + m.group(1) + "~~BCLOSE~~", s)
    s = re.sub(
        r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",
        lambda m: "~~IOPEN~~" + m.group(1) + "~~ICLOSE~~",
        s,
    )
    s = re.sub(r"\*(.+?)\*", lambda m: "~~IOPEN~~" + m.group(1) + "~~ICLOSE~~", s)

    escaped = html.escape(strip_preview_emoji(s), quote=False)

    # Restore formatting tags from our safe placeholders
    escaped = escaped.replace("~~BOPEN~~", "<b>").replace("~~BCLOSE~~", "</b>")
    escaped = escaped.replace("~~IOPEN~~", "<i>").replace("~~ICLOSE~~", "</i>")

    # Inject links
    for idx, (label, url) in enumerate(links):
        label_html = html.escape(strip_preview_emoji(label), quote=False)
        url_html = html.escape(url, quote=True)
        escaped = escaped.replace(
            f"@@LINK{idx}@@",
            f'<link href="{url_html}" color="blue"><u>{label_html}</u></link>',
        )

    return escaped


def _clean_placeholders_for_pdf(s: str) -> str:
    """Remove leftover placeholder tokens that may appear verbatim in PDFs.

    This strips common markers produced during earlier markdown processing
    (e.g. __B_OPEN__, _BOPEN_, BCLOSE__, etc.) so the Paragraph text is
    rendered cleanly. Keep this conservative and run after inline->ReportLab
    conversion so legitimate `<b>`/`<i>` tags are preserved.
    """
    if not s:
        return s
    # remove explicit placeholder tokens
    s = re.sub(r"__B_OPEN__|__B_CLOSE__|__I_OPEN__|__I_CLOSE__", "", s)
    s = re.sub(r"_BOPEN_|BCLOSE__|_BOPEN|CLOSE__", "", s)
    # remove fragments like '(_B) (OPEN)' that can appear when PDF text is
    # split into multiple drawing operations
    s = re.sub(r"\(?_?B_?\)?\s*\(?OPEN\)?", "", s)
    s = re.sub(r"\(?CLOSE_+__?\)?", "", s)
    # collapse remaining multiple underscores
    s = re.sub(r"_+", "", s)
    return s


def save_report_pdf(report):
    if SimpleDocTemplate is None:
        print(
            "[PDF export skipped: reportlab is not installed.]",
            file=sys.stderr,
            flush=True,
        )
        return None

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    body_font = reportlab_font()
    bold_font = reportlab_bold_font()
    base = getSampleStyleSheet()
    styles = {
        "h1": ParagraphStyle(
            "ReportH1",
            parent=base["Heading1"],
            fontName=bold_font,
            fontSize=22,
            leading=28,
            textColor=colors.HexColor("#111827"),
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "ReportH2",
            parent=base["Heading2"],
            fontName=bold_font,
            fontSize=15,
            leading=20,
            textColor=colors.HexColor("#111827"),
            spaceBefore=8,
            spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "ReportH3",
            parent=base["Heading3"],
            fontName=bold_font,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#111827"),
            spaceBefore=5,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "ReportBody",
            parent=base["BodyText"],
            fontName=body_font,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#374151"),
            spaceAfter=2,
        ),
        "italic": ParagraphStyle(
            "ReportItalic",
            parent=base["BodyText"],
            fontName=body_font,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#4B5563"),
            spaceAfter=8,
        ),
    }

    story = []
    bullet_styles = {}

    for raw_line in report.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            story.append(Spacer(1, 4))
            continue

        if stripped == "---":
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=0.75,
                    color=colors.HexColor("#D1D5DB"),
                    spaceBefore=6,
                    spaceAfter=8,
                )
            )
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            txt = markdown_inline_to_reportlab(heading.group(2))
            txt = _clean_placeholders_for_pdf(txt)
            story.append(Paragraph(txt, styles[f"h{level}"]))
            continue

        bullet = re.match(r"^(\s*)-\s+(.+)$", line)
        if bullet:
            level = max(0, len(bullet.group(1)) // 2)
            if level not in bullet_styles:
                left_indent = 14 + (level * 16)
                bullet_styles[level] = ParagraphStyle(
                    f"ReportBullet{level}",
                    parent=styles["body"],
                    leftIndent=left_indent,
                    firstLineIndent=0,
                    bulletIndent=level * 16,
                    spaceAfter=1,
                )
            txt = markdown_inline_to_reportlab(bullet.group(2))
            txt = _clean_placeholders_for_pdf(txt)
            story.append(Paragraph(txt, bullet_styles[level], bulletText="\u2022"))
            continue

        style = (
            styles["italic"]
            if stripped.startswith("_") and stripped.endswith("_")
            else styles["body"]
        )
        txt = markdown_inline_to_reportlab(stripped)
        txt = _clean_placeholders_for_pdf(txt)
        story.append(Paragraph(txt, style))

    doc = SimpleDocTemplate(
        REPORT_PDF,
        pagesize=A4,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title="Good Morning",
    )
    doc.build(story)
    return REPORT_PDF


def save_report_exports(report):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    exports = []

    pdf_path = save_report_pdf(report)
    if pdf_path:
        exports.append(pdf_path)

    return exports


def clean_num(s):
    if not s:
        return None
    s = s.strip().replace(",", "").replace("\u2033", "").replace("\u2757", "")
    return s


def code_from_name(name):
    mapping = {
        "dow jones": "Dow",
        "s&p 500": "S&P 500",
        "nasdaq": "Nasdaq",
        "ftse 100": "FTSE",
        "dax": "DAX",
        "cac 40": "CAC",
        "nikkei 225": "Nikkei",
        "hang seng": "HSI",
        "euro stoxx 50": "Euro Stoxx 50",
        "ftse mib": "FTSE MIB",
        "swiss market index": "SMI",
        "shanghai": "Shanghai",
        "szse component": "SZSE Component",
        "idx composite": "IDX",
        "idx lq45": "LQ45",
        "idx kompas 100": "IDX Kompas 100",
        "ftse indonesia local": "FTSE Indonesia",
        "idx30": "IDX30",
        "idx 30": "IDX30",
        "idx energy": "IDXEnergy",
        "idx basic materials": "IDX BscMat",
        "idx industrials": "IDXIndst",
        "idx consumer non-cyclicals": "IDXNONCYC",
        "idx healthcare": "IDXHlthcare",
        "idx consumer cyclical": "IDXCYCLC",
        "idx technology": "IDX Tech",
        "idx transportation": "IDX Transprt",
        "idx infrastructure": "IDX Infra",
        "idx finance": "IDX Finance",
        "idx banking": "IDX Banking",
        "u.s. 2y": "US2Yr",
        "u.s. 5y": "US5Yr",
        "u.s. 10y": "US10Yr",
        "u.s. 30y": "US30Yr",
        "indo 10y": "Indo10Yr",
        "indonesia 10y": "Indo10Yr",
        "s&p 500 vix": "S&P 500 VIX",
        "nifty 50": "Nifty 50",
        "s&p/asx 200": "S&P/ASX 200",
        "psei composite": "PSEi Composite",
        "set": "SET",
        "taiwan weighted": "Taiwan Weighted",
        "smi": "SMI",
        "ftse mib": "FTSE MIB",
    }
    key = name.lower().strip()
    if key in mapping:
        return mapping[key]
    return name


# ────────────────── TABLE-BASED PARSERS ──────────────────


def parse_table_pages(pages):
    results = {}
    for label, url, name_col, last_col, chg_col, chg_pct_col in pages:
        try:
            resp = fetch(url)
            bs = BeautifulSoup(resp.text, "lxml")
            tables = bs.find_all("table")
            if not tables:
                continue
            # Some pages contain multiple tables; search all of them to avoid
            # missing rows (e.g. IDX30 appearing in a later table).
            for table in tables:
                rows = table.find_all("tr")
                if not rows:
                    continue
                for row in rows[1:]:
                    cells = row.find_all("td")
                    if len(cells) <= max(name_col, last_col, chg_col, chg_pct_col):
                        continue
                    name = cells[name_col].get_text(" ", strip=True)
                    name_clean = re.sub(r"\s*derived$", "", name).strip()
                    code = code_from_name(name_clean)
                    last_txt = cells[last_col].get_text(strip=True)
                    chg_txt = (
                        cells[chg_col].get_text(strip=True)
                        if chg_col < len(cells)
                        else ""
                    )
                    pct_txt = (
                        cells[chg_pct_col].get_text(strip=True)
                        if chg_pct_col < len(cells)
                        else ""
                    )
                    if last_txt and code:
                        results[code] = {
                            "close": clean_num(last_txt),
                            "change": clean_num(chg_txt),
                            "change_pct": pct_txt,
                            "source": label,
                        }
        except Exception as e:
            print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


def parse_phei():
    """ICBI + Indo10Yr from PHEI (Penilai Harga Efek Indonesia)."""
    result = {}
    try:
        resp = fetch(
            "https://www.phei.co.id/en-us/Data/Fair-Prices-and-Yield", timeout=30
        )
        bs = BeautifulSoup(resp.text, "lxml")

        # ── ICBI from the header card ──
        icbi_el = bs.find(string="ICBI")
        if icbi_el:
            container = icbi_el.find_parent(class_="col-md-12")
            if not container:
                container = icbi_el.find_parent("div", class_=True)
                while container:
                    if has_css_class(container, "col-md-12"):
                        break
                    container = container.parent
                    if not container or getattr(container, "name", "") == "html":
                        container = None
                        break
            if container:
                text = container.get_text("|", strip=True)
                parts = text.split("|")
                # Expected: ICBI|▲|426.4080|Previous|425.7156|Change|0.6925|Change (%)|0.16
                if len(parts) >= 9:
                    close = parts[2]
                    prev = parts[4]
                    chg = parts[6]
                    pct = parts[8]
                    result["ICBI"] = {
                        "close": close,
                        "change": chg,
                        "change_pct": f"{pct}%",
                        "source": "PHEI",
                    }

        # ── Indo10Yr from IGSYC table ──
        tables = bs.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue
            header = rows[0].find_all(["th", "td"])
            header_texts = [c.get_text(strip=True) for c in header]
            if "Tenor" not in " ".join(header_texts):
                continue
            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                tenor = cells[0].get_text(strip=True)
                if tenor == "10.0":
                    today = cells[1].get_text(strip=True)
                    yesterday = cells[2].get_text(strip=True)
                    try:
                        t = float(today)
                        y = float(yesterday)
                        chg = round(t - y, 4)
                        pct = round((chg / y) * 100, 2) if y else 0.0
                        result["Indo10Yr"] = {
                            "close": f"{t:.4f}",
                            "change": f"{chg:+.4f}",
                            "change_pct": f"{pct:+.2f}%",
                            "source": "PHEI",
                        }
                    except (ValueError, TypeError):
                        pass
                    break
            break  # only first tenor table
    except Exception as e:
        print(f"  WARN PHEI: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


def parse_commodities_futures():
    results = {}
    wanted = {
        "Crude Oil WTI": "Oil(WT)",
        "Brent Oil": "Oil(Brn)",
        "Natural Gas": "Ntrl Gas",
        "Gold": "Gold",
        "Silver": "Silver",
        "Copper": "Copper",
        "Aluminium": "Aluminium",
        "Nickel": "Nickel",
        "Tin": "Timah",
        "US Corn": "Corn",
        "US Soybean Oil": "SoybeanOil",
        "US Wheat": "Wheat",
    }
    try:
        resp = fetch("https://www.investing.com/commodities/real-time-futures")
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if not tables:
            return results

        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue

            # Inspect header row to detect column indices
            header_cells = rows[0].find_all(["th", "td"]) if rows else []
            header_texts = [c.get_text(" ", strip=True).lower() for c in header_cells]

            def find_header_index(keywords):
                for i, h in enumerate(header_texts):
                    for kw in keywords:
                        if kw in h:
                            return i
                return None

            name_idx = find_header_index(
                ["name", "contract", "commodity", "instrument", "symbol"]
            ) or (1 if len(header_cells) > 1 else 0)
            last_idx = (
                find_header_index(["last", "price", "close"])
                or find_header_index(["ltd"])
                or 3
            )
            chg_idx = find_header_index(["change", "chg"]) or None
            pct_idx = find_header_index(["%", "change (%)", "chg%", "change%", "ch%"])

            def _normalize_name(n: str) -> str:
                s = n or ""
                s = s.strip()
                # remove parenthetical notes like (Jul 26) and similar
                s = re.sub(r"\(.*?\)", "", s)
                # remove month tokens like 'Jul 26' or 'August 26'
                s = re.sub(
                    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{1,2}\b",
                    "",
                    s,
                    flags=re.I,
                )
                # strip 'derived' suffix and extra whitespace
                s = re.sub(r"\s*derived$", "", s, flags=re.I).strip()
                s = re.sub(r"\s+", " ", s)
                return s

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                name_raw = (
                    cells[name_idx].get_text(" ", strip=True)
                    if name_idx < len(cells)
                    else cells[0].get_text(" ", strip=True)
                )
                name_norm = _normalize_name(name_raw)
                # lightweight debug trace to help diagnose missing matches
                logger.debug("Commodities row: raw=%r norm=%r", name_raw, name_norm)

                # find the best matching wanted key (robust substring/word match)
                matched_key = None
                name_l = name_norm.lower()
                for wk in wanted.keys():
                    if wk.lower() == name_l:
                        matched_key = wk
                        break
                if not matched_key:
                    for wk in wanted.keys():
                        if wk.lower() in name_l or name_l in wk.lower():
                            matched_key = wk
                            break
                if not matched_key:
                    # token overlap (require at least 3-char token to avoid spurious matches)
                    tokens = re.findall(r"\w{3,}", name_l)
                    for wk in wanted.keys():
                        wk_l = wk.lower()
                        for t in tokens:
                            if t in wk_l:
                                matched_key = wk
                                break
                        if matched_key:
                            break
                if not matched_key:
                    logger.debug("Commodities: no match for '%s'", name_norm)
                    continue

                # robustly locate numeric fields after the name cell
                last_txt = ""
                chg_txt = ""
                pct_txt = ""

                # attempt by header indices first
                if isinstance(last_idx, int) and last_idx < len(cells):
                    last_txt = cells[last_idx].get_text(strip=True)

                # fallback: find the first numeric-like cell after the name column
                if not last_txt:
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if re.search(r"[0-9]", t) and re.search(r"\d", t):
                            # prefer values that look like a price (contains digit and dot or comma)
                            if re.match(r"^[+-]?\d[\d,\.]*%?$", t):
                                last_txt = t
                                break

                # change: try header index then first numeric after last that's different
                if chg_idx is not None and chg_idx < len(cells):
                    chg_txt = cells[chg_idx].get_text(strip=True)
                else:
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if not t:
                            continue
                        if t == last_txt:
                            continue
                        if re.match(r"^[+-]?\d[\d,\.]*%?$", t):
                            chg_txt = t
                            break

                # percent: prefer explicit '%' occurrence from the end of the row
                if pct_idx is not None and pct_idx < len(cells):
                    pct_txt = cells[pct_idx].get_text(strip=True)
                else:
                    for c in reversed(cells):
                        t = c.get_text(strip=True)
                        if "%" in t:
                            pct_txt = t
                            break

                if not last_txt:
                    continue

                code = wanted[matched_key]
                logger.debug(
                    "Commodities: matched %r -> %r (code=%s)",
                    name_norm,
                    matched_key,
                    code,
                )
                results[code] = {
                    "close": clean_num(last_txt),
                    "change": clean_num(chg_txt) if chg_txt else None,
                    "change_pct": pct_txt,
                    "source": "Investing Futures",
                }
            # If some wanted items were not found in the tables (Investing may
            # render them via different tables or separate instrument pages),
            # attempt fallback to their instrument pages.
            missing_codes = [c for c in wanted.values() if c not in results]
            if missing_codes:
                fallback_urls = {
                    "Oil(WT)": [
                        "https://www.investing.com/commodities/crude-oil",
                        "https://www.investing.com/commodities/crude-oil-wti",
                    ],
                    "Oil(Brn)": [
                        "https://www.investing.com/commodities/brent-oil",
                        "https://www.investing.com/commodities/brent-oil-futures",
                    ],
                    "Ntrl Gas": [
                        "https://www.investing.com/commodities/natural-gas",
                        "https://www.investing.com/commodities/natural-gas-futures",
                    ],
                    "Aluminium": [
                        "https://www.investing.com/commodities/aluminium",
                        "https://www.investing.com/commodities/aluminum",
                    ],
                    "Nickel": [
                        "https://www.investing.com/commodities/nickel",
                    ],
                }
                for code in missing_codes:
                    urls = fallback_urls.get(code, [])
                    for url in urls:
                        try:
                            logger.debug(
                                "Commodities: fallback try %s for code %s", url, code
                            )
                            parsed = parse_instrument_page(
                                url, "Investing Futures", code
                            )
                            if parsed and isinstance(parsed, dict) and parsed.get(code):
                                val = parsed.get(code)
                                logger.debug(
                                    "Commodities: fallback parsed for %s: %s",
                                    code,
                                    bool(val),
                                )
                                if is_valid_data(val):
                                    results[code] = val
                                    break
                        except Exception as e:
                            logger.warning(
                                "Commodities fallback %s: %s: %s",
                                url,
                                type(e).__name__,
                                str(e)[:80],
                            )
                            # ignore and try next fallback URL
                            continue
    except Exception as e:
        print(f"  WARN Commodities: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── STATE/JSON-BASED PARSERS ────────────────


def parse_instrument_page(url, label, code_name):
    result = {}
    try:
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, "lxml")
        for script in bs.find_all("script"):
            if script.get("id") == "__NEXT_DATA__":
                if not script.string:
                    continue
                try:
                    data = json.loads(script.string)
                except Exception:
                    continue
                state = data["props"]["pageProps"]["state"]

                for store_key in [
                    "commodityStore",
                    "indexStore",
                    "bondStore",
                    "currencyStore",
                    "etfStore",
                    "equityStore",
                ]:
                    store = state.get(store_key, {})
                    instrument = store.get("instrument", {})
                    if not instrument:
                        continue
                    price = instrument.get("price", {})
                    if price and price.get("last") is not None:
                        result = {
                            "close": str(price["last"]),
                            "change": str(price.get("change", "")),
                            "change_pct": str(price.get("changePcr", "")),
                            "high": str(price.get("high", "")),
                            "low": str(price.get("low", "")),
                            "open": str(price.get("open", "")),
                            "prev_close": str(price.get("lastClose", "")),
                            "source": label,
                        }
                        break
                if not result:
                    quotes = state.get("quotesStore", {}).get("quotes", [])
                    if isinstance(quotes, list) and len(quotes) > 0:
                        q = quotes[0]
                        if q.get("last") is not None:
                            result = {
                                "close": str(q["last"]),
                                "change": str(q.get("change", "")),
                                "change_pct": str(q.get("changePct", "")),
                                "source": label,
                            }
                break
    except Exception as e:
        print(f"  WARN {label}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return {code_name: result} if result else {}


# ──────────────── YAHOO FINANCE ────────────────


def parse_yahoo_finance(ticker, code_name):
    result = {}
    try:
        url = f"https://finance.yahoo.com/quote/{ticker}/"
        resp = fetch(url)
        bs = BeautifulSoup(resp.text, "lxml")
        qsp = bs.find("span", {"data-testid": "qsp-price"})
        price = qsp.get_text(strip=True) if qsp else None
        price = str(price) if price is not None else None

        if not price:
            price_el = bs.find(
                "fin-streamer",
                {"data-field": "regularMarketPrice", "data-symbol": ticker},
            )
            if not price_el:
                price_el = bs.find("fin-streamer", {"data-field": "regularMarketPrice"})
            price = (
                price_el.get("data-value") or price_el.get_text(strip=True)
                if price_el
                else None
            )
            price = str(price) if price is not None else None

        # First try: fin-streamer with matching data-symbol
        change_el = bs.find(
            "fin-streamer", {"data-field": "regularMarketChange", "data-symbol": ticker}
        )
        pct_el = bs.find(
            "fin-streamer",
            {"data-field": "regularMarketChangePercent", "data-symbol": ticker},
        )
        change = (
            change_el.get("data-value") or change_el.get_text(strip=True)
            if change_el
            else ""
        )
        change = str(change) if change is not None else ""
        pct = pct_el.get("data-value") or pct_el.get_text(strip=True) if pct_el else ""
        pct = str(pct) if pct is not None else ""

        # Second try: if fin-streamer not found for this ticker, parse from parent text
        if not change_el and qsp:
            parent_txt = qsp.parent.get_text(" ", strip=True) if qsp.parent else ""
            m = re.search(r"([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)%\)", parent_txt)
            if m:
                change = m.group(1)
                pct = m.group(2)
            else:
                m2 = re.search(r"([+-]?\d+[\d.]*)\s*\(([+-]?\d+[\d.]*)", parent_txt)
                if m2:
                    change = m2.group(1)
                    pct = m2.group(2)

        if price:
            price = str(price).replace(",", "")
            valid_change = change or ""
            valid_pct = f"{pct}%" if pct else ""
            if valid_change and price:
                try:
                    chg_num = abs(float(valid_change))
                    close_num = float(price)
                    if pct:
                        pct_num = abs(float(str(pct).rstrip("%")))
                        if pct_num < 1.0 and chg_num > close_num / 10:
                            valid_change = ""
                            valid_pct = ""
                except ValueError:
                    pass
            result = {
                code_name: {
                    "close": price,
                    "change": valid_change,
                    "change_pct": valid_pct,
                    "source": "Yahoo Finance",
                }
            }
    except Exception as e:
        print(
            f"  WARN {code_name} (Yahoo): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


def parse_yahoo_sector_indices():
    result = {}
    sectors = [
        ("IDXEnergy", "IDXENERGY.JK"),
        ("IDX BscMat", "IDXBASIC.JK"),
        ("IDXIndst", "IDXINDUST.JK"),
        ("IDXNONCYC", "IDXNONCYC.JK"),
        ("IDXHlthcare", "IDXHEALTH.JK"),
        ("IDXCYCLC", "IDXCYCLIC.JK"),
        ("IDX Tech", "IDXTECHNO.JK"),
        ("IDX Transprt", "IDXTRANS.JK"),
        ("IDX Infra", "IDXINFRA.JK"),
        ("IDX Finance", "IDXFINANCE.JK"),
        ("IDX Banking", "INFOBANK15.JK"),
        ("IDX Property", "IDXPROPERT.JK"),
    ]
    for code_name, ticker in sectors:
        try:
            resp = fetch(
                f"https://finance.yahoo.com/quote/{ticker}/",
                impersonate="chrome120",
                timeout=20,
            )
            soup = BeautifulSoup(resp.text, "lxml")
            qsp = soup.find("span", {"data-testid": "qsp-price"})
            price_str = qsp.get_text(strip=True) if qsp else None
            price_str = (
                str(price_str).replace(",", "") if price_str is not None else None
            )
            if not price_str:
                pe = soup.find("fin-streamer", {"data-field": "regularMarketPrice"})
                if pe and not pe.get("data-symbol"):
                    price_str = pe.get("data-value", "") or pe.get_text(strip=True)
                    price_str = (
                        str(price_str).replace(",", "")
                        if price_str is not None
                        else None
                    )
            if not price_str:
                continue
            price = price_str
            change = ""
            change_pct = ""
            prev_el = soup.find(
                "fin-streamer", {"data-field": "regularMarketPreviousClose"}
            )
            if prev_el:
                prev_raw = prev_el.get("data-value", "") or prev_el.get_text(strip=True)
                prev_str = str(prev_raw).replace(",", "")
                if prev_str:
                    try:
                        p = float(price)
                        prev = float(prev_str)
                        diff = round(p - prev, 2)
                        pct = round((diff / prev) * 100, 2) if prev != 0 else 0
                        change = f"+{diff}" if diff >= 0 else str(diff)
                        change_pct = f"+{pct}%" if pct >= 0 else f"{pct}%"
                    except (ValueError, TypeError):
                        pass
            result[code_name] = {
                "close": price,
                "change": change,
                "change_pct": change_pct,
                "source": "Yahoo Finance (Sector)",
            }
        except Exception as e:
            print(
                f"  WARN {code_name} (Yahoo Sector): {type(e).__name__}: {str(e)[:60]}",
                file=sys.stderr,
            )
    return result


# ──────────────── BONDS ────────────────


def parse_indonesia_bonds():
    results = {}
    try:
        resp = fetch(
            "https://www.investing.com/rates-bonds/indonesia-government-bonds?"
            "maturity_from=40&maturity_to=290"
        )
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if tables:
            table = tables[0]
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 5:
                    name = cells[1].get_text(" ", strip=True)
                    if "10Y" in name or "10 Yr" in name:
                        results["Indo10Yr"] = {
                            "close": cells[2].get_text(strip=True),
                            "prev": cells[3].get_text(strip=True),
                            "source": "Investing Bonds",
                        }
                        break
    except Exception as e:
        print(f"  WARN Indo Bonds: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── CDS ────────────────


def parse_indonesia_cds():
    result = {}
    try:
        payload = {
            "GLOBALVAR": {
                "FUNCTION": "CDS",
                "DOMESTIC": True,
                "ENDPOINT": "https://www.worldgovernmentbonds.com/wp-json/common/v1/historical",
                "DATE_RIF": "2099-12-31",
                "DEBUG": True,
                "OBJ": {
                    "UNIT": "",
                    "DECIMAL": 2,
                    "UNIT_DELTA": "%",
                    "DECIMAL_DELTA": 2,
                },
                "COUNTRY1": {
                    "SYMBOL": "39",
                    "PAESE": "Indonesia",
                    "PAESE_UPPERCASE": "INDONESIA",
                    "BANDIERA": "id",
                    "URL_PAGE": "indonesia",
                },
                "COUNTRY2": None,
                "OBJ1": {"DURATA_STRING": "5 Years", "DURATA": 60},
                "OBJ2": None,
            }
        }
        resp = req.post(
            "https://www.worldgovernmentbonds.com/wp-json/common/v1/historical",
            json=payload,
            headers={
                "Origin": "https://www.worldgovernmentbonds.com",
                "Referer": "https://www.worldgovernmentbonds.com/cds-historical-data/indonesia/5-years/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
            },
            impersonate=cast(Any, IMPRERSONATE),
            timeout=20,
        )

        data = resp.json()
        if not data.get("success"):
            return result
        r = data["result"]
        close = str(r["ultimoValore"])
        change = ""
        change_pct = ""
        html = r.get("htmlLatestChange", "")
        if html:
            soup = BeautifulSoup(html, "lxml")
            for tr in soup.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) >= 5 and "1 Week" in cells[0].get_text(strip=True):
                    min_div = cells[2].find("div")
                    prev_val = min_div.get_text(strip=True) if min_div else ""
                    if prev_val:
                        try:
                            curr = float(close)
                            prev = float(prev_val)
                            diff = round(curr - prev, 2)
                            pc = round((diff / prev) * 100, 2) if prev != 0 else 0
                            change = f"+{diff}" if diff >= 0 else str(diff)
                            change_pct = f"+{pc}%" if pc >= 0 else f"{pc}%"
                        except (ValueError, TypeError):
                            change = cells[1].get_text(strip=True)
                            change_pct = change
                    break
        result["IndoCDS 5yr"] = {
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "source": "WorldGovernmentBonds",
        }
    except Exception as e:
        print(f"  WARN IndoCDS: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── AMMONIA ────────────────


def parse_ammonia():
    result = {}
    try:
        resp = fetch(
            "https://www.chemicalbook.com/PriceInfoall_CB9854275.htm",
            headers=ZH_HEADERS,
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        entries = []
        for li in soup.find_all("li"):
            if has_css_class(li, "align_r"):
                continue
            txt = li.get_text(" ", strip=True)
            m = re.search(r"(\d+月\d+日).*?氨.*?报价[:：]?(\d[\d,.]*)", txt)
            if m:
                entries.append(
                    {
                        "date": m.group(1),
                        "price": m.group(2).replace(",", ""),
                    }
                )
        if entries:
            latest = entries[0]
            close = latest["price"]
            change = ""
            change_pct = ""
            if len(entries) >= 2:
                prev = entries[1]
                try:
                    c = float(close)
                    p = float(prev["price"])
                    diff = round(c - p, 2)
                    pc = round((diff / p) * 100, 2) if p != 0 else 0
                    change = f"+{diff}" if diff >= 0 else str(diff)
                    change_pct = f"+{pc}%" if pc >= 0 else f"{pc}%"
                except (ValueError, TypeError):
                    pass
            result["Ammonia"] = {
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "date": latest["date"],
                "unit": "Yuan/ton",
                "note": f"ChemicalBook ({entries[0]['date']})",
                "source": "ChemicalBook",
            }
    except Exception as e:
        print(f"  WARN Ammonia: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── JISDOR ────────────────


def parse_jisdor():
    result = {}
    try:
        resp = fetch(
            "https://www.bi.go.id/id/statistik/informasi-kurs/jisdor/default.aspx",
            timeout=25,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        rates = []
        for td in soup.find_all("td"):
            txt = td.get_text(strip=True)
            m = re.match(r"Rp(\d{2,3}\.\d{3})[,\s]", txt)
            if m:
                rates.append(m.group(1).replace(".", ","))
        if len(rates) >= 2:
            curr = rates[0].replace(",", "")
            prev = rates[1].replace(",", "")
            curr_f = float(curr)
            prev_f = float(prev)
            change = round(curr_f - prev_f, 0)
            change_pct = round(((curr_f - prev_f) / prev_f) * 100, 2)
            result["Jisdor"] = {
                "close": rates[0],
                "change": f"{change:+.0f}",
                "change_pct": f"{change_pct:+.2f}%",
                "source": "BI",
            }
        elif len(rates) == 1:
            result["Jisdor"] = {
                "close": rates[0],
                "change": "",
                "change_pct": "",
                "source": "BI",
            }
    except Exception as e:
        print(f"  WARN JISDOR: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── DXY from Yahoo Finance API ────────────────


def parse_yahoo_dxy():
    """Fetch DXY via Yahoo Finance v8 chart API (more reliable than HTML scraping)."""
    result = {}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=1d"
        resp = fetch(url, impersonate="chrome120", timeout=20)
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if price and prev_close:
            change = round(price - prev_close, 3)
            pct = round(((price - prev_close) / prev_close) * 100, 2)
            result["USDIndx"] = {
                "close": str(price),
                "change": f"{change:+.3f}",
                "change_pct": f"{pct:+.2f}%",
                "source": "Yahoo Finance API",
            }
        elif price:
            result["USDIndx"] = {
                "close": str(price),
                "change": "",
                "change_pct": "",
                "source": "Yahoo Finance API",
            }
    except Exception as e:
        print(
            f"  WARN DXY (Yahoo API): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────── IDX Property from Yahoo Finance API ────────────────


def parse_yahoo_idx_property():
    """Fetch IDX Property (IDXPROPERT.JK) via Yahoo Finance v8 chart API."""
    result = {}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/IDXPROPERT.JK?interval=1d&range=5d"
        resp = fetch(url, impersonate="chrome120", timeout=20)
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if price and prev_close:
            change = round(price - prev_close, 2)
            pct = round(((price - prev_close) / prev_close) * 100, 2)
            result["IDX Property"] = {
                "close": str(price),
                "change": f"{change:+.2f}",
                "change_pct": f"{pct:+.2f}%",
                "source": "Yahoo Finance API",
            }
        elif price:
            result["IDX Property"] = {
                "close": str(price),
                "change": "",
                "change_pct": "",
                "source": "Yahoo Finance API",
            }
    except Exception as e:
        print(
            f"  WARN IDX Property (Yahoo API): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────── BAR CHART COAL ────────────────


def parse_barchart_coal():
    result = {}
    month_codes = {
        "Jun": "M",
        "Jul": "N",
        "Aug": "Q",
        "Sep": "U",
    }

    for root_name, root_sym, label in [
        ("Newcastle", "LQ", "Coal(Nwl)"),
        ("Rotterdam", "LU", "Coal(Rot)"),
    ]:
        contracts = []
        for month_name, code in month_codes.items():
            sym = f"{root_sym}{code}26"
            try:
                resp = fetch(
                    f"https://www.barchart.com/futures/quotes/{sym}/overview",
                    impersonate="chrome120",
                    timeout=20,
                )
                soup = BeautifulSoup(resp.text, "lxml")
                tables = soup.find_all("table")
                for table in tables:
                    rows = table.find_all("tr")
                    if rows and len(rows) > 1:
                        cells = rows[1].find_all("td")
                        if len(cells) >= 3 and cells[0].get_text(strip=True) == sym:
                            price_raw = (
                                cells[1]
                                .get_text(strip=True)
                                .replace("s", "")
                                .replace(",", "")
                            )
                            chg_raw = cells[2].get_text(strip=True).replace(",", "")
                            try:
                                price = float(price_raw)
                                chg = float(chg_raw)
                                prev_close = price - chg
                                pct = (
                                    round((chg / prev_close) * 100, 2)
                                    if prev_close
                                    else 0
                                )
                                contracts.append(
                                    {
                                        "month": month_name,
                                        "price": f"{price:.2f}",
                                        "change": f"{chg:+.2f}",
                                        "change_pct": f"{pct:+.2f}%",
                                    }
                                )
                            except:
                                pass
                            break
            except Exception as e:
                pass

        if contracts:
            result[label] = {
                "contracts": contracts,
                "source": "Barchart",
            }

    return result


def parse_bursa_cpo():
    """FCPO from Bursa Malaysia derivatives market table, row 3."""
    result = {}
    try:
        resp = fetch(
            "https://www.bursamalaysia.com/trade/market/derivatives_market",
            impersonate="chrome120",
            timeout=30,
        )
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 4:
                continue
            # Find the FCPO table: first header cell should say "Futures/Months"
            header_cells = rows[0].find_all(["th", "td"])
            if not header_cells or "Futures" not in header_cells[0].get_text(
                strip=True
            ):
                continue
            # Row index 2 = 3rd row (0-based), the row the user wants
            row = rows[2]
            cells = row.find_all(["td", "th"])
            if len(cells) < 4:
                continue
            last_raw = cells[1].get_text(strip=True).replace(",", "")
            chg_raw = cells[2].get_text(strip=True).replace(",", "")
            try:
                close = float(last_raw)
                change = float(chg_raw)
                prev_close = close - change
                pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
                result["CPO"] = {
                    "close": f"{close:.2f}",
                    "change": f"{change:+.2f}",
                    "change_pct": f"{pct:+.2f}%",
                    "source": "Bursa Malaysia",
                }
            except (ValueError, TypeError):
                pass
            break
    except Exception as e:
        print(f"  WARN Bursa CPO: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return result


# ──────────────── SUNSIRS WOOD PULP ────────────────


def parse_sunsirs_woodpulp():
    """Wood pulp spot price from SunSirs Daily table (Building materials sector)."""
    result = {}
    try:
        resp = fetch("https://www.sunsirs.com/uk/sectors-17.html")
        bs = BeautifulSoup(resp.text, "lxml")
        tables = bs.find_all("table")
        if not tables:
            return result
        # First table = Spot Price: Daily
        table = tables[0]
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            name = cells[0].get_text(strip=True)
            if "Wood pulp" in name:
                prev_raw = cells[2].get_text(strip=True).replace(",", "")
                close_raw = cells[3].get_text(strip=True).replace(",", "")
                pct_raw = (
                    cells[4].get_text(strip=True).replace("%", "").replace(",", "")
                )
                try:
                    close = float(close_raw)
                    prev = float(prev_raw)
                    change = close - prev
                    pct = float(pct_raw)
                    result["Woodpulp"] = {
                        "close": f"{close:.2f}",
                        "change": f"{change:+.2f}",
                        "change_pct": f"{pct:+.2f}%",
                        "source": "SunSirs",
                    }
                except (ValueError, TypeError):
                    pass
                break
    except Exception as e:
        print(
            f"  WARN SunSirs Woodpulp: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


# ──────────────────── DATA COLLECTION ────────────────────


def collect_data():
    """Run all scrapers, return (data_dict, sources_list, timestamp)."""
    DATA = {}

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    def run_task(label, fn):
        try:
            return fn()
        except Exception as e:
            log(f"  WARN {label}: {type(e).__name__}: {str(e)[:80]}")
            return {}

    log("Regional Screener -- collecting data...")
    t0 = datetime.now()

    single_pages = [
        (
            "Iron Ore",
            "https://www.investing.com/commodities/iron-ore-62-cfr-futures",
            "Iron Ore 62%",
        ),
        (
            "Woodpulp",
            "https://id.investing.com/commodities/shfe-bleached-softwood-kraft-pulp-futures",
            "Woodpulp",
        ),
        ("Tin", "https://www.investing.com/commodities/tin", "Timah"),
        ("Silver", "https://www.investing.com/commodities/silver", "Silver"),
        ("Copper", "https://www.investing.com/commodities/copper", "Copper"),
        (
            "BCOMIN",
            "https://www.investing.com/indices/bloomberg-industrial-metals",
            "BCOMIN",
        ),
        ("COMIN", "https://www.investing.com/indices/commodity-index", "Como Indx"),
        ("USD/IDR", "https://www.investing.com/currencies/usd-idr", "IDR"),
        ("EUR/USD", "https://www.investing.com/currencies/eur-usd", "Euro"),
        ("Gold Spot", "https://www.investing.com/currencies/xau-usd", "Gold(Spot)"),
        # Additional direct instrument pages to ensure key commodities are fetched
        ("Crude Oil WTI", "https://www.investing.com/commodities/crude-oil", "Oil(WT)"),
        ("Brent Oil", "https://www.investing.com/commodities/brent-oil", "Oil(Brn)"),
        (
            "Natural Gas",
            "https://www.investing.com/commodities/natural-gas",
            "Ntrl Gas",
        ),
        ("Nickel", "https://www.investing.com/commodities/nickel", "Nickel"),
        ("Aluminium", "https://www.investing.com/commodities/aluminium", "Aluminium"),
    ]
    tasks = [
        ("IDX Sector Indices", parse_yahoo_sector_indices),
        ("JISDOR", parse_jisdor),
        (
            "Major Indices",
            lambda: parse_table_pages(
                [
                    (
                        "Major Indices",
                        "https://www.investing.com/indices/major-indices",
                        1,
                        2,
                        5,
                        6,
                    ),
                ]
            ),
        ),
        (
            "IDX Indices",
            lambda: parse_table_pages(
                [
                    (
                        "IDX Indices",
                        "https://www.investing.com/indices/indonesia-indices?include-major-indices=true&include-additional-indices=true&include-primary-sectors=true&include-other-indices=true",
                        1,
                        2,
                        5,
                        6,
                    ),
                ]
            ),
        ),
        ("SunSirs Woodpulp", parse_sunsirs_woodpulp),
        ("Commodities", parse_commodities_futures),
        ("Coal from Barchart", parse_barchart_coal),
        *[
            (
                label,
                lambda url=url, label=label, code=code: parse_instrument_page(
                    url, label, code
                ),
            )
            for label, url, code in single_pages
        ],
        (
            "US Bonds",
            lambda: parse_table_pages(
                [
                    (
                        "US Bonds",
                        "https://www.investing.com/rates-bonds/usa-government-bonds",
                        1,
                        2,
                        6,
                        7,
                    ),
                ]
            ),
        ),
        ("Indo Bonds", parse_indonesia_bonds),
        ("PHEI (ICBI + Indo10Yr)", parse_phei),
        *[
            (code, lambda ticker=ticker, code=code: parse_yahoo_finance(ticker, code))
            for ticker, code in [
                ("^VIX", "VIX"),
                ("EIDO", "EIDO"),
                ("EEM", "EEM"),
                ("TLK", "TLKM"),
                ("SI%3DF", "Silver"),
                ("HG%3DF", "Copper"),
            ]
        ],
        ("DXY Yahoo API", parse_yahoo_dxy),
        ("IndoCDS", parse_indonesia_cds),
        ("Ammonia", parse_ammonia),
        ("IDX Property", parse_yahoo_idx_property),
        ("Bursa CPO", parse_bursa_cpo),
    ]

    log(f"Submitting {len(tasks)} scraper tasks with {MAX_FETCH_WORKERS} workers...")
    results_by_index = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(run_task, label, fn): idx
            for idx, (label, fn) in enumerate(tasks)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                res = future.result()
            except Exception as e:
                res = {}
                log(f"  WARN task {idx}: {type(e).__name__}: {str(e)[:80]}")
            results_by_index[idx] = res
            label = tasks[idx][0]
            log(f"  done: {label}")

    for idx in range(len(tasks)):
        DATA.update(results_by_index.get(idx, {}))

    elapsed = (datetime.now() - t0).total_seconds()
    sources = sorted(
        set(v.get("source", "unknown") for v in DATA.values() if isinstance(v, dict))
    )

    log(f"\nDone in {elapsed:.1f}s -- {len(DATA)} items collected")
    ts = datetime.now().isoformat()
    # stamp per-key fetched_at if parsers didn't provide one
    for v in DATA.values():
        if isinstance(v, dict) and not v.get("fetched_at"):
            v["fetched_at"] = ts
    return DATA, sources, ts


# ──────────────────── MARKET NEWS ────────────────────


def fetch_market_news(max_items=5):
    """Fetch latest US market news headlines from Google News RSS."""
    news = []
    urls = [
        "https://news.google.com/rss/search?q=US+stock+market&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=Wall+Street&hl=en-US&gl=US&ceid=US:en",
    ]
    seen_titles = set()
    for url in urls:
        try:
            r = req.get(url, impersonate=cast(Any, IMPRERSONATE), timeout=15)
            text = r.text
            root = ET.fromstring(text)
            for item in root.iter("item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                if not title or not link:
                    continue
                title = title.split(" - ")[0].strip()
                key = title.lower()[:60]
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                news.append({"title": title, "url": link})
                if len(news) >= max_items:
                    return news
        except Exception:
            pass
    return news


# ──────────────────── FORMATTING HELPERS ────────────────────


def close_str(d):
    if isinstance(d, dict):
        return d.get("close", "")
    if isinstance(d, str):
        return d
    return ""


def fmt(d):
    if isinstance(d, str):
        return d
    if not isinstance(d, dict):
        return str(d)
    close = d.get("close", "")
    chg = get_point_change(d)
    pct = get_change(d)
    if pct:
        pct_clean = str(pct).strip()
        # Add % if missing (Investing API sometimes returns bare numbers)
        if pct_clean and not pct_clean.endswith("%"):
            pct_clean += "%"
        # Keep the sign (+/-) on percent values — do not strip '+'
        if chg and chg not in ("", "None"):
            return f"{close} {chg} {pct_clean}"
        else:
            return f"{close} {pct_clean}"
    if chg and chg not in ("", "None"):
        return f"{close} {chg}"
    return close


def get_change(d):
    if not isinstance(d, dict):
        return ""
    pct = d.get("change_pct", "")
    if pct and pct not in ("", "0", "0%"):
        try:
            pct_num = abs(
                float(str(pct).replace("%", "").replace("+", "").replace(",", ""))
            )
            if pct_num > 50:
                return ""
        except (ValueError, ZeroDivisionError):
            pass
        s = str(pct).strip()
        # Add % if missing (Investing API returns bare numbers)
        if not s.endswith("%"):
            s += "%"
        # Ensure sign is present for positives
        if not s.startswith(("+", "-")):
            s = "+" + s
        return s
    return ""


def get_point_change(d):
    if not isinstance(d, dict):
        return ""
    chg = d.get("change", "")
    if chg and chg not in ("", "0", "None"):
        s = str(chg).strip()
        if not s.startswith(("+", "-")):
            s = "+" + s
        return s
    return ""


def fmt_with_pct(d):
    if isinstance(d, str):
        return d
    close = close_str(d)
    pct = get_change(d)
    point = get_point_change(d)
    if point and pct and not (isinstance(point, str) and point.startswith("-19")):
        pct_clean = str(pct).strip()
        # Preserve leading +/-, and ensure percent sign
        if pct_clean and not pct_clean.endswith("%"):
            pct_clean += "%"
        return f"{close} {point} {pct_clean}"
    if pct:
        pct_clean = str(pct).strip()
        if pct_clean and not pct_clean.endswith("%"):
            pct_clean += "%"
        return f"{close} {pct_clean}"
    if point and not (isinstance(point, str) and point.startswith("-19")):
        return f"{close} {point}"
    return close


def is_valid_data(d):
    """Return True if a parsed data item looks usable (has a close or change)."""
    if not isinstance(d, dict):
        return False
    close = d.get("close")
    if close and str(close).strip() not in ("", "None"):
        return True
    if d.get("change") and str(d.get("change")).strip() not in ("", "None"):
        return True
    if d.get("change_pct") and str(d.get("change_pct")).strip() not in (
        "",
        "None",
        "0",
        "0%",
    ):
        return True
    return False


def format_percent_value(d, prefix="", suffix="%"):
    """Format numeric close values that are percentages (e.g. yields).

    Returns a string like: '4.529% +0.01 +0.22%'
    """
    if not isinstance(d, dict):
        return ""
    close = close_str(d)
    if not close:
        return ""
    # append percent sign if close looks numeric and doesn't already have '%'
    cp = str(close).strip()
    try:
        float(cp)
        if not cp.endswith(str(suffix)):
            cp = f"{cp}{suffix}"
    except Exception:
        pass

    parts = [f"{prefix}{cp}"]
    point = get_point_change(d)
    pct = get_change(d)
    if point:
        parts.append(point)
    if pct:
        parts.append(pct)
    return " ".join(parts)


def format_currency_value(d, prefix="$"):
    """Format currency-like values with close, point change and percent.

    Example: '$90.16 +0.14 +0.16%'
    """
    if not isinstance(d, dict):
        return ""
    close = close_str(d)
    if not close:
        return ""
    cp = str(close).strip()
    # keep as-is; don't force decimal formatting
    parts = [f"{prefix}{cp}"]
    point = get_point_change(d)
    pct = get_change(d)
    if point:
        parts.append(point)
    if pct:
        parts.append(pct)
    return " ".join(parts)


def decorate_value(d, base=None):
    """Wrap numeric base string with bold markers if percent thresholds met.

    Uses `get_change(d)` to determine percent magnitude. If abs(pct) > 3%
    the returned string is bolded and appended with '‼️'. If abs(pct) > 2%
    (and <= 3%) it is only bolded. If no percent is present, the base is
    returned unchanged.
    """
    if base is None:
        try:
            base = fmt_with_pct(d) if isinstance(d, dict) else str(d)
        except Exception:
            base = str(d) if d is not None else ""
    base = str(base).strip()
    if not base:
        return base
    pct = get_change(d) if isinstance(d, dict) else ""
    abs_pct = None
    if pct and pct not in ("", "0", "0%"):
        try:
            abs_pct = abs(
                float(str(pct).replace("%", "").replace("+", "").replace(",", ""))
            )
        except Exception:
            abs_pct = None
    if abs_pct is None:
        return base
    if abs_pct > 3.0:
        return f"**{base}** ‼️"
    if abs_pct > 2.0:
        return f"**{base}**"
    return base


# ──────────────────── REPORT FORMATTER ────────────────────


def format_report(data):
    """Build the full report text."""
    lines = []

    def kv(key):
        d = data.get(key)
        if d is None:
            return None
        base = fmt_with_pct(d)
        return decorate_value(d, base)

    def kv_full(key):
        d = data.get(key)
        if d is None:
            return None
        base = fmt(d)
        return decorate_value(d, base)

    # Header
    now = datetime.now()
    hari = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ][now.weekday()]
    bulan = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ][now.month - 1]
    lines.append("# 📊 Good Morning")
    lines.append(f"_🗓️ {hari}, {now.day} {bulan} {now.year}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Market News
    news = fetch_market_news(5)
    if news:
        lines.append("## 📰 Market News Summary")
        lines.append("")
        lines.append("### Top Market News")
        for n in news:
            lines.append(f'- [{n["title"]}]({n["url"]})')
        lines.append("")

    lines.append("---")
    lines.append("")

    # US Indices
    lines.append("## 🇺🇸 US Indices")
    for key, label in [
        ("Dow", "Dow"),
        ("S&P 500", "S&P 500"),
        ("Nasdaq", "Nasdaq"),
        ("S&P 500 VIX", "S&P 500 VIX"),
    ]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")
    lines.append("")

    # Europe
    lines.append("## 🇪🇺 Europe")
    for key, label in [("DAX", "DAX"), ("FTSE", "FTSE"), ("CAC", "CAC")]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")
    lines.append("")

    # Asia
    lines.append("## 🌏 Asia")
    for key, label in [
        ("Nikkei", "Nikkei"),
        ("Shanghai", "Shanghai"),
        ("HSI", "HSI"),
        ("KOSPI", "KOSPI"),
        ("STI", "STI"),
    ]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")
    lines.append("")

    # Indonesia
    lines.append("## 🇮🇩 Indonesia")
    idx_val = kv("IDX")
    if idx_val:
        lines.append(f"- **IDX:** {idx_val}")
    for key, label in [
        ("LQ45", "LQ45"),
        ("IDX Kompas 100", "Kompas 100"),
        ("IDX30", "IDX30"),
    ]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")

    for k, label in [
        ("IDXEnergy", "Energy"),
        ("IDX BscMat", "Basic Materials"),
        ("IDXIndst", "Industrial"),
        ("IDX Tech", "Technology"),
        ("IDX Finance", "Finance"),
        ("IDX Banking", "Banking"),
        ("IDX Infra", "Infrastructure"),
        ("IDX Property", "Property"),
        ("IDX Transprt", "Transportation"),
        ("IDXCYCLC", "Consumer Cyclical"),
        ("IDXNONCYC", "Consumer Non-Cyclical"),
        ("IDXHlthcare", "Healthcare"),
    ]:
        v = kv(k)
        if v:
            lines.append(f"- **IDX {label}:** {v}")
    lines.append("")

    # Indonesia-specific FX / bonds: USD/IDR, Indo10Yr, ICBI, IndoCDS
    idr_v = kv_full("IDR")
    if idr_v:
        lines.append(f"- **USD/IDR:** {idr_v}")

    jisdor_val = kv("Jisdor")
    if jisdor_val:
        lines.append(f"- **Jisdor:** {jisdor_val}")

    indo10 = data.get("Indo10Yr")
    if isinstance(indo10, dict) and is_valid_data(indo10):
        lines.append(
            f"- **Indo10Yr:** {decorate_value(indo10, format_percent_value(indo10))}"
        )

    icbi_val = kv_full("ICBI")
    if icbi_val:
        lines.append(f"- **ICBI:** {icbi_val}")

    icds = data.get("IndoCDS 5yr")
    if isinstance(icds, dict) and is_valid_data(icds):
        icds_v = icds.get("close", "")
        if icds_v:
            parts = []
            ch = icds.get("change", "")
            pc = icds.get("change_pct", "")
            if ch and ch not in ("", "None"):
                chs = str(ch).strip()
                if not chs.startswith(("+", "-")):
                    chs = "+" + chs
                parts.append(chs)
            if pc and pc not in ("", "None"):
                pcs = str(pc).strip()
                if not pcs.endswith("%"):
                    pcs += "%"
                if not pcs.startswith(("+", "-")):
                    pcs = "+" + pcs
                parts.append(pcs)
            cds_str = f"{icds_v}"
            if parts:
                cds_str = cds_str + " " + " ".join(parts)
            lines.append(f"- **IndoCDS 5yr:** {decorate_value(icds, cds_str)}")
    lines.append("")

    # FX & Bonds
    lines.append("## 💵 FX & Bonds")
    euro_v = kv_full("Euro")
    if euro_v:
        lines.append(f"- **EUR/USD:** {euro_v}")

    dxy = data.get("USDIndx")
    if isinstance(dxy, dict) and is_valid_data(dxy):
        dxy_fmt = fmt(dxy)
        if dxy_fmt:
            lines.append(f"- **DXY:** {dxy_fmt}")

    # US Treasuries — display vertical list in order: 2Yr, 10Yr, 30Yr
    us2 = data.get("US2Yr")
    us10 = data.get("US10Yr")
    us30 = data.get("US30Yr")
    if any(isinstance(x, dict) and is_valid_data(x) for x in (us2, us10, us30)):
        lines.append("- **US Treasuries:**")
        if isinstance(us2, dict) and is_valid_data(us2):
            lines.append(
                f"  - **US2Yr:** {decorate_value(us2, format_percent_value(us2))}"
            )
        if isinstance(us10, dict) and is_valid_data(us10):
            lines.append(
                f"  - **US10Yr:** {decorate_value(us10, format_percent_value(us10))}"
            )
        if isinstance(us30, dict) and is_valid_data(us30):
            lines.append(
                f"  - **US30Yr:** {decorate_value(us30, format_percent_value(us30))}"
            )

    lines.append("")

    # Energy
    lines.append("## 🛢️ Energy")
    for key, label, prefix in [
        ("Oil(WT)", "Oil WTI", "$"),
        ("Oil(Brn)", "Oil Brent", "$"),
        ("Ntrl Gas", "Nat Gas", "$"),
    ]:
        d = data.get(key)
        if isinstance(d, dict) and is_valid_data(d):
            lines.append(
                f"- **{label}:** {decorate_value(d, format_currency_value(d, prefix))}"
            )
    lines.append("")

    # Coal (Barchart)
    lines.append("### Coal (Barchart) 🔄")
    coal_nwl = data.get("Coal(Nwl)")
    coal_rot = data.get("Coal(Rot)")
    if isinstance(coal_nwl, dict) and coal_nwl.get("contracts"):
        lines.append("- **Newcastle:**")
        for c in coal_nwl["contracts"]:
            d = {
                "close": c.get("price"),
                "change": c.get("change"),
                "change_pct": c.get("change_pct"),
            }
            formatted = decorate_value(d, fmt_with_pct(d))
            lines.append(f'  - **{c["month"]}:** {formatted}')

    if isinstance(coal_rot, dict) and coal_rot.get("contracts"):
        lines.append("- **Rotterdam:**")
        for c in coal_rot["contracts"]:
            d = {
                "close": c.get("price"),
                "change": c.get("change"),
                "change_pct": c.get("change_pct"),
            }
            formatted = decorate_value(d, fmt_with_pct(d))
            lines.append(f'  - **{c["month"]}:** {formatted}')
    lines.append("")

    # Metals & Mining
    lines.append("## 🏗️ Metals & Mining")
    for key, label in [
        ("Gold(Spot)", "Gold"),
        ("Silver", "Silver"),
        ("Copper", "Copper"),
        ("Nickel", "Nickel"),
        ("Timah", "Timah"),
        ("Aluminium", "Aluminium"),
        ("Iron Ore 62%", "Iron Ore 62%"),
        ("BCOMIN", "BCOMIN"),
    ]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")
    lines.append("")

    # Komoditas Lain
    lines.append("## 🌿 Komoditas Lain")
    for key, label in [
        ("CPO", "CPO"),
        ("Woodpulp", "Woodpulp"),
        ("Ammonia", "Ammonia"),
        ("Corn", "Corn"),
        ("Wheat", "Wheat"),
        ("SoybeanOil", "Soybean Oil"),
    ]:
        if key == "Ammonia":
            v = kv(key)
            if v:
                d = data.get(key, {})
                note = d.get("note", "") if isinstance(d, dict) else ""
                note_str = f" ({note})" if note else ""
                lines.append(f"- **{label}:** {v}{note_str}")
        else:
            v = kv(key)
            if v:
                lines.append(f"- **{label}:** {v}")
    lines.append("")

    # ETFs & Stocks
    lines.append("## 📈 ETFs & Stocks")
    for key, label in [("EIDO", "EIDO"), ("TLKM", "TLKM"), ("EEM", "EEM")]:
        d = data.get(key)
        if isinstance(d, dict):
            c = close_str(d)
            if c:
                sp = get_point_change(d)
                p = get_change(d)
                parts = []
                if sp:
                    parts.append(sp)
                if p:
                    parts.append(p)
                base = f"{c} {' '.join(parts)}" if parts else f"{c}"
                lines.append(f"- **{label}:** {decorate_value(d, base)}")

    # Footer
    lines.append("---")
    lines.append("## Footer")
    lines.append("- **Broker Code:** AT")
    lines.append("- **Prepared by:** Desy Erawati / DE")
    lines.append("- **Sources:** Bloomberg, Investing, IBPA, CNBC, Bursa Malaysia")
    lines.append("- **Copyright:** Phintraco Sekuritas")

    return "\n".join(lines)


# ──────────────────── MAIN ────────────────────


def format_report_whatsapp(report_md):
    """Convert the generated markdown report into a WhatsApp-friendly plain text.

    Rules:
    - Headings -> bold (wrap with *)
    - Bullets '-' -> '•'
    - Links [text](url) -> 'text — url'
    - Markdown bold '**text**' -> '*text*'
    - Horizontal rules '---' -> blank line
    """
    out_lines = []
    in_top_market_news = False

    for raw in report_md.splitlines():
        line = raw.rstrip()
        if not line:
            out_lines.append("")
            continue

        # Horizontal rule
        if line.strip() == "---":
            out_lines.append("")
            continue

        # Headings: convert '# ...' to '*...*'
        m = re.match(r"^#{1,6}\s*(.+)$", line)
        if m:
            content = m.group(1).strip()
            content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", content)
            # reset or enable Top Market News mode
            if "top market news" in content.lower():
                in_top_market_news = True
            else:
                in_top_market_news = False
            # keep links in headings as 'label — url'
            content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 — \2", content)
            out_lines.append(f"*{content}*")
            continue

        # Bullets (including indented bullets)
        b = re.match(r"^(\s*)-\s+(.*)$", line)
        if b:
            indent = b.group(1)
            content = b.group(2)
            content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", content)
            # In Top Market News, omit URLs and keep only the headline text
            if in_top_market_news:
                content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", content)
            else:
                content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 — \2", content)
            out_lines.append(f"{indent}• {content}")
            continue

        # Inline bold and links elsewhere
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        if in_top_market_news:
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", line)
        else:
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 — \2", line)
        out_lines.append(line)

    return "\n".join(out_lines)


def main():
    json_only = "--json-only" in sys.argv
    from_cache = "--from-cache" in sys.argv
    partial_cache_mode = "--partial-cache" in sys.argv

    # If running as frozen exe and no explicit flag provided, default to partial-cache
    if getattr(sys, "frozen", False) and "--partial-cache" not in sys.argv:
        partial_cache_mode = True

    # Configure logging: enable debug output when requested
    debug_mode = "--debug" in sys.argv or "--verbose" in sys.argv
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
        logger.debug("Logging enabled: DEBUG")
    else:
        logging.basicConfig(level=logging.WARNING)

    # Load cache if requested or if partial-cache available
    cache_exists = os.path.exists(CACHE_JSON)
    cache_raw = None
    if cache_exists:
        try:
            with open(CACHE_JSON, "r", encoding="utf-8") as f:
                cache_raw = json.load(f)
        except Exception:
            cache_raw = None

    # Decide whether to use cache-only (skip fetch).
    # Only auto-skip network fetch when the user explicitly asked to use cache
    # (i.e. not in partial-cache mode). Partial-cache mode must still perform
    # a fetch so missing keys can be filled and then merged.
    use_cache_only = False
    if cache_raw and not partial_cache_mode:
        try:
            cached_ts = cache_raw.get("timestamp")
            if cached_ts:
                then = datetime.fromisoformat(cached_ts)
                age_seconds = (datetime.now() - then).total_seconds()
                if age_seconds <= 3600:
                    use_cache_only = True
        except Exception:
            use_cache_only = False

    if (from_cache or use_cache_only) and cache_raw:
        if use_cache_only and not from_cache:
            print(
                "[Using recent cache (<=1h); skipping network fetch)]",
                file=sys.stderr,
                flush=True,
            )
        else:
            print("[Loading from cached screener data...]", file=sys.stderr, flush=True)
        data = cache_raw.get("data", {})
        sources = cache_raw.get("sources_used", [])
        ts = cache_raw.get("timestamp", "")
    else:
        # Collect fresh data
        data, sources, ts = collect_data()

        # Save raw JSON to cache (normal or partial merge)
        os.makedirs(CACHE_DIR, exist_ok=True)
        raw_out = {
            "timestamp": ts,
            "data": data,
            "sources_used": sources,
        }
        try:
            if (
                partial_cache_mode
                and cache_raw
                and isinstance(cache_raw.get("data"), dict)
            ):
                # Perform per-key partial-cache merge using fetched_at timestamps.
                # Rule: if cached entry exists and its fetched_at is <1 hour old and
                # the new value is invalid/missing, keep the cached value.
                merged = dict(cache_raw.get("data", {}))
                now_ts = datetime.now()
                for k, v in data.items():
                    # if the new value is valid, accept it
                    if is_valid_data(v):
                        merged[k] = v
                        continue

                    # new value invalid -> keep recent cached value if available
                    cached_item = merged.get(k)
                    if isinstance(cached_item, dict):
                        fetched_at = cached_item.get("fetched_at") or cache_raw.get(
                            "timestamp"
                        )
                        try:
                            if fetched_at:
                                then = datetime.fromisoformat(fetched_at)
                                age_seconds = (now_ts - then).total_seconds()
                                if age_seconds <= 3600:
                                    # keep cached recent value
                                    continue
                        except Exception:
                            # if parsing fails, fall through and skip storing invalid new value
                            pass

                    # new value invalid and no recent cached value -> skip adding it
                    # (do not overwrite or create entries with invalid data)
                    continue

                # also merge in any new keys that were only in cache but not in current data
                for k, v in cache_raw.get("data", {}).items():
                    if k not in merged:
                        merged[k] = v

                raw_out["data"] = merged
                raw_out["sources_used"] = sorted(
                    set(sources) | set(cache_raw.get("sources_used", []))
                )
            # write out cache
            with open(CACHE_JSON, "w", encoding="utf-8") as f:
                json.dump(raw_out, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(
                f"[WARN] failed to write cache: {type(e).__name__}: {str(e)[:80]}",
                file=sys.stderr,
            )

    if json_only:
        print(
            json.dumps(
                {"timestamp": ts, "data": data, "sources_used": sources},
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    # Build and print report
    report = format_report(data)
    print(report, flush=True)

    # Save report to output files (no Markdown/PNG exports per configuration)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Save WhatsApp-friendly plaintext export
    try:
        wa_text = format_report_whatsapp(report)
        with open(REPORT_WA, "w", encoding="utf-8") as f:
            f.write(wa_text)
        print(f"[Report saved to {REPORT_WA}]", file=sys.stderr, flush=True)
    except Exception as e:
        print(
            f"[WhatsApp export skipped: {type(e).__name__}: {str(e)[:80]}]",
            file=sys.stderr,
            flush=True,
        )

    for export_path in save_report_exports(report):
        print(f"[Report saved to {export_path}]", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
