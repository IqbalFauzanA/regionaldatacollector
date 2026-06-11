#!/usr/bin/env python3
"""
Regional Market Report — Scraper + Formatter (ALL-IN-ONE)
===========================================================
Scrapes ~118 market data points, formats into SOP layout, prints to screen.
No subprocess, no encoding war, no pipe fragility.

Usage:
    python regional_market_report.py              # scrape fresh + format + save cache
    python regional_market_report.py --from-cache # use cached JSON (instant)
    python regional_market_report.py --json-only  # scrape fresh, dump JSON only
"""

import html, json, os, sys, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import BoundedSemaphore
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from typing import Any, cast

import curl_cffi.requests as req

from bs4 import BeautifulSoup

from PIL import Image, ImageDraw, ImageFont

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

# ──────────────────────── CONFIG ────────────────────────

TIMEOUT = 30
MAX_FETCH_WORKERS = 8
IMPRERSONATE = "chrome120"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8,zh-CN;q=0.7",
}
ZH_HEADERS = {**HEADERS, "Accept-Language": "zh-CN,en;q=0.9,id;q=0.8"}
HOST_LIMITS = {
    "www.investing.com": BoundedSemaphore(2),
    "id.investing.com": BoundedSemaphore(1),
    "finance.yahoo.com": BoundedSemaphore(3),
    "query1.finance.yahoo.com": BoundedSemaphore(2),
    "www.barchart.com": BoundedSemaphore(2),
}
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_DIR = os.path.join(BASE_DIR, "cache")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CACHE_JSON = os.path.join(CACHE_DIR, "regional_raw.json")
REPORT_MD = os.path.join(OUTPUT_DIR, "regional_report.md")
REPORT_PDF = os.path.join(OUTPUT_DIR, "regional_report.pdf")
REPORT_PNG = os.path.join(OUTPUT_DIR, "regional_report.png")
REPORT_WA = os.path.join(OUTPUT_DIR, "regional_report_whatsapp.txt")

# ──────────────────────── HELPERS ────────────────────────


def fetch(url, impersonate=IMPRERSONATE, headers=HEADERS, timeout=TIMEOUT):
    host = urlparse(url).netloc.lower()
    limiter = HOST_LIMITS.get(host)
    if limiter:
        with limiter:
            return fetch_unlimited(
                url, impersonate=impersonate, headers=headers, timeout=timeout
            )
    return fetch_unlimited(
        url, impersonate=impersonate, headers=headers, timeout=timeout
    )


def fetch_unlimited(url, impersonate=IMPRERSONATE, headers=HEADERS, timeout=TIMEOUT):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            r = req.get(
                url,
                impersonate=cast(Any, impersonate),
                headers=headers,
                timeout=timeout,
            )
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = (
                status in (403, 429)
                or "HTTP Error 403" in str(e)
                or "HTTP Error 429" in str(e)
            )
            if not retryable or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("fetch failed")


def strip_preview_emoji(text):
    return re.sub(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F]",
        "",
        text,
    )


def clean_preview_text(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    text = strip_preview_emoji(text)
    return re.sub(r"\s+", " ", text).strip()


def has_css_class(element, cls: str) -> bool:
    """Return True if a BeautifulSoup element has the given CSS class."""
    if not element:
        return False
    classes = element.get("class")
    if not classes:
        return False
    if isinstance(classes, (list, tuple)):
        return cls in classes
    if isinstance(classes, str):
        return cls in classes.split()
    return False


def load_export_font(size=20, bold=False):
    if ImageFont is None:
        return None

    font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    names = (
        ("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf", "arialbd.ttf")
        if bold
        else ("msyh.ttc", "segoeui.ttf", "arial.ttf")
    )
    for name in names:
        path = os.path.join(font_dir, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def font_height(draw, font):
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


def wrap_text(text, draw, font, max_width):
    if not text:
        return []

    wrapped = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            wrapped.append(current)
            current = word

        while text_width(draw, current, font) > max_width and len(current) > 1:
            cut = len(current)
            while cut > 1 and text_width(draw, current[:cut], font) > max_width:
                cut -= 1
            wrapped.append(current[:cut])
            current = current[cut:]

    if current:
        wrapped.append(current)
    return wrapped


def markdown_preview_items(markdown, draw, fonts, max_width):
    items = []
    body_font = fonts["body"]
    link_font = fonts["body"]
    bullet_gap = 26

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            items.append({"type": "space", "height": 14})
            continue

        if stripped == "---":
            items.append({"type": "rule", "height": 30})
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            key = f"h{level}"
            font = fonts[key]
            text = clean_preview_text(heading.group(2))
            lines = wrap_text(text, draw, font, max_width)
            line_h = font_height(draw, font) + (10 if level == 1 else 8)
            items.append(
                {
                    "type": "heading",
                    "font": font,
                    "lines": lines,
                    "height": (len(lines) * line_h) + (20 if level == 1 else 14),
                    "line_height": line_h,
                    "color": (17, 24, 39),
                }
            )
            continue

        bullet = re.match(r"^(\s*)-\s+(.+)$", line)
        if bullet:
            level = max(0, len(bullet.group(1)) // 2)
            text = clean_preview_text(bullet.group(2))
            indent = min(96, level * 34)
            text_width_limit = max_width - indent - bullet_gap
            is_link = bool(re.search(r"\[[^\]]+\]\([^)]+\)", bullet.group(2)))
            lines = wrap_text(
                text, draw, link_font if is_link else body_font, text_width_limit
            )
            line_h = font_height(draw, body_font) + 8
            items.append(
                {
                    "type": "bullet",
                    "font": link_font if is_link else body_font,
                    "lines": lines,
                    "height": max(1, len(lines)) * line_h,
                    "line_height": line_h,
                    "indent": indent,
                    "bullet_gap": bullet_gap,
                    "color": (37, 99, 235) if is_link else (31, 41, 55),
                }
            )
            continue

        text = clean_preview_text(stripped)
        lines = wrap_text(text, draw, body_font, max_width)
        line_h = font_height(draw, body_font) + 8
        items.append(
            {
                "type": "paragraph",
                "font": body_font,
                "lines": lines,
                "height": max(1, len(lines)) * line_h,
                "line_height": line_h,
                "color": (31, 41, 55),
            }
        )

    return items


def draw_markdown_items(draw, items, y, margin, max_width):
    for item in items:
        if item["type"] == "space":
            y += item["height"]
            continue

        if item["type"] == "rule":
            line_y = y + (item["height"] // 2)
            draw.line(
                (margin, line_y, margin + max_width, line_y),
                fill=(209, 213, 219),
                width=2,
            )
            y += item["height"]
            continue

        if item["type"] == "bullet":
            bullet_x = margin + item["indent"]
            text_x = bullet_x + item["bullet_gap"]
            first_y = y + 4
            draw.ellipse(
                (bullet_x + 4, first_y + 7, bullet_x + 12, first_y + 15),
                fill=(75, 85, 99),
            )
            for i, line in enumerate(item["lines"]):
                draw.text(
                    (text_x, y + (i * item["line_height"])),
                    line,
                    fill=item["color"],
                    font=item["font"],
                )
            y += item["height"]
            continue

        for i, line in enumerate(item["lines"]):
            draw.text(
                (margin, y + (i * item["line_height"])),
                line,
                fill=item["color"],
                font=item["font"],
            )
        y += item["height"]

    return y


def render_markdown_page(items, width, height, margin):
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw_markdown_items(draw, items, margin, margin, width - (margin * 2))
    return image


def reportlab_font(name="ReportFont"):
    if pdfmetrics is None or TTFont is None:
        return "Helvetica"

    font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for filename in ("segoeui.ttf", "arial.ttf"):
        path = os.path.join(font_dir, filename)
        if os.path.exists(path):
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                pass
    return "Helvetica"


def reportlab_bold_font(name="ReportFontBold"):
    if pdfmetrics is None or TTFont is None:
        return "Helvetica-Bold"

    font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for filename in ("segoeuib.ttf", "arialbd.ttf"):
        path = os.path.join(font_dir, filename)
        if os.path.exists(path):
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                pass
    return "Helvetica-Bold"


def markdown_inline_to_reportlab(text):
    links = []

    def link_repl(match):
        idx = len(links)
        links.append((match.group(1), match.group(2)))
        return f"@@LINK{idx}@@"

    text = strip_preview_emoji(text)
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)
    escaped = html.escape(text, quote=False)

    def bold_repl(match):
        return f"<b>{match.group(1)}</b>"

    escaped = re.sub(r"\*\*(.+?)\*\*", bold_repl, escaped)
    escaped = escaped.replace("_", "")

    for idx, (label, url) in enumerate(links):
        label_html = html.escape(strip_preview_emoji(label), quote=False)
        url_html = html.escape(url, quote=True)
        escaped = escaped.replace(
            f"@@LINK{idx}@@",
            f'<link href="{url_html}" color="blue"><u>{label_html}</u></link>',
        )

    return escaped


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
            story.append(
                Paragraph(
                    markdown_inline_to_reportlab(heading.group(2)), styles[f"h{level}"]
                )
            )
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
            story.append(
                Paragraph(
                    markdown_inline_to_reportlab(bullet.group(2)),
                    bullet_styles[level],
                    bulletText="\u2022",
                )
            )
            continue

        style = (
            styles["italic"]
            if stripped.startswith("_") and stripped.endswith("_")
            else styles["body"]
        )
        story.append(Paragraph(markdown_inline_to_reportlab(stripped), style))

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

    if Image is None:
        print(
            "[PNG export skipped: Pillow is not installed.]",
            file=sys.stderr,
            flush=True,
        )
        return exports

    fonts = {
        "h1": load_export_font(34, bold=True),
        "h2": load_export_font(26, bold=True),
        "h3": load_export_font(22, bold=True),
        "body": load_export_font(20),
    }
    probe = Image.new("RGB", (1, 1), "white")
    probe_draw = ImageDraw.Draw(probe)

    png_width = 1400
    png_margin = 64
    png_items = markdown_preview_items(
        report, probe_draw, fonts, png_width - (png_margin * 2)
    )
    png_height = max(400, (png_margin * 2) + sum(item["height"] for item in png_items))
    png = render_markdown_page(png_items, png_width, png_height, png_margin)
    png.save(REPORT_PNG)

    exports.append(REPORT_PNG)
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

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells or name_idx >= len(cells):
                    continue
                name = cells[name_idx].get_text(" ", strip=True)
                name_clean = re.sub(r"\s*derived$", "", name).strip()
                if name_clean not in wanted:
                    continue

                last_txt = ""
                chg_txt = ""
                pct_txt = ""

                # last
                if isinstance(last_idx, int) and last_idx < len(cells):
                    last_txt = cells[last_idx].get_text(strip=True)
                else:
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if re.match(r"^[+-]?\d[\d,\.]*$", t):
                            last_txt = t
                            break

                # change
                if chg_idx is not None and chg_idx < len(cells):
                    chg_txt = cells[chg_idx].get_text(strip=True)
                else:
                    # find the first numeric after last_txt that's not the same
                    for c in cells[name_idx + 1 :]:
                        t = c.get_text(strip=True)
                        if not t:
                            continue
                        if t == last_txt:
                            continue
                        if re.match(r"^[+-]?\d[\d,\.]*$", t):
                            chg_txt = t
                            break

                # percent
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

                code = wanted[name_clean]
                results[code] = {
                    "close": clean_num(last_txt),
                    "change": clean_num(chg_txt) if chg_txt else None,
                    "change_pct": pct_txt,
                    "source": "Investing Futures",
                }
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
    ]
    tasks = [
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
                        -1,
                        -1,
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
        ("IDX Sector Indices", parse_yahoo_sector_indices),
        ("DXY Yahoo API", parse_yahoo_dxy),
        ("IndoCDS", parse_indonesia_cds),
        ("Ammonia", parse_ammonia),
        ("JISDOR", parse_jisdor),
    ]

    log(f"Submitting {len(tasks)} scraper tasks with {MAX_FETCH_WORKERS} workers...")
    results_by_index = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(run_task, label, fn): (idx, label)
            for idx, (label, fn) in enumerate(tasks)
        }
        for future in as_completed(futures):
            idx, label = futures[future]
            results_by_index[idx] = future.result()
            log(f"  done: {label}")

    for idx in range(len(tasks)):
        DATA.update(results_by_index.get(idx, {}))

    elapsed = (datetime.now() - t0).total_seconds()
    sources = sorted(
        set(v.get("source", "unknown") for v in DATA.values() if isinstance(v, dict))
    )

    log(f"\nDone in {elapsed:.1f}s -- {len(DATA)} items collected")

    return DATA, sources, datetime.now().isoformat()


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
        if chg:
            return f"{close} {chg} {pct}"
        else:
            return f"{close} {pct}"
    if chg:
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
    if point and pct and not point.startswith("-19"):
        return f"{close} {point} {pct}"
    if pct:
        return f"{close} {pct}"
    if point and not point.startswith("-19"):
        return f"{close} {point}"
    return close


# ──────────────────── REPORT FORMATTER ────────────────────


def format_report(data):
    """Build the full report text."""
    lines = []

    def kv(key, label=None):
        d = data.get(key)
        if d is None:
            return None
        return fmt_with_pct(d)

    def kv_full(key, label=None):
        d = data.get(key)
        if d is None:
            return None
        return fmt(d)

    # ── Header ──
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
    lines.append("# 📊 Regional Markets Screener")
    lines.append(f"_🗓️ {hari}, {now.day} {bulan} {now.year}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Market News Summary ──
    lines.append("## 📰 Market News Summary")
    lines.append("")

    news = fetch_market_news(5)
    if news:
        lines.append("### Top Market News")
        for n in news:
            lines.append(f'- [{n["title"]}]({n["url"]})')
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── US Indices ──
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

    # ── Europe ──
    lines.append("## 🇪🇺 Europe")
    for key, label in [
        ("DAX", "DAX"),
        ("FTSE", "FTSE"),
        ("CAC", "CAC"),
    ]:
        v = kv(key)
        if v:
            lines.append(f"- **{label}:** {v}")
    lines.append("")

    # ── Asia ──
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

    # ── Indonesia ──
    lines.append("## 🇮🇩 Indonesia")
    idx_val = kv("IDX")
    if idx_val:
        lines.append(f"- **IDX:** {idx_val} 🔥")
    lq_val = kv("LQ45")
    if lq_val:
        lines.append(f"- **LQ45:** {lq_val}")
    kom_val = kv("IDX Kompas 100")
    if kom_val:
        lines.append(f"- **Kompas 100:** {kom_val}")
    idx30_val = kv("IDX30")
    if idx30_val:
        lines.append(f"- **IDX30:** {idx30_val}")
    jisdor_val = kv("Jisdor")
    if jisdor_val:
        lines.append(f"- **Jisdor:** {jisdor_val}")

    idx_sectors = [
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
    ]
    for k, label in idx_sectors:
        v = kv(k)
        if v:
            lines.append(f"- **IDX {label}:** {v}")

    lines.append("")

    # ── FX & Bonds ──
    lines.append("## 💵 FX & Bonds")
    idr_v = kv_full("IDR")
    if idr_v:
        lines.append(f"- **USD/IDR:** {idr_v}")
    euro_v = kv_full("Euro")
    if euro_v:
        lines.append(f"- **EUR/USD:** {euro_v}")
    dxy = data.get("USDIndx")
    if isinstance(dxy, dict):
        dxy_fmt = fmt(dxy)
        if dxy_fmt:
            lines.append(f"- **DXY:** {dxy_fmt}")

    us10 = data.get("US10Yr")
    us2 = data.get("US2Yr")
    us30 = data.get("US30Yr")
    us10_v = close_str(us10) if isinstance(us10, dict) else ""
    us2_v = close_str(us2) if isinstance(us2, dict) else ""
    us30_v = close_str(us30) if isinstance(us30, dict) else ""
    if us10_v or us2_v or us30_v:
        lines.append(
            f"- **US Treasuries:** US10Yr {us10_v}% | US2Yr {us2_v}% | US30Yr {us30_v}%"
        )

    indo10 = data.get("Indo10Yr")
    if isinstance(indo10, dict):
        indo10_v = indo10.get("close", "")
        if indo10_v:
            lines.append(f"- **Indo10Yr:** {indo10_v}%")

    # ICBI should be shown under FX & Bonds below Indo10Yr
    icbi_val = kv_full("ICBI")
    if icbi_val:
        lines.append(f"- **ICBI:** {icbi_val}")

    icds = data.get("IndoCDS 5yr")
    if isinstance(icds, dict):
        icds_v = icds.get("close", "")
        icds_chg = icds.get("change", "")
        icds_pct = icds.get("change_pct", "")
        if icds_v:
            parts = []
            if icds_chg and icds_chg not in ("", "None"):
                ch = str(icds_chg).strip()
                if not ch.startswith(("+", "-")):
                    ch = "+" + ch
                parts.append(ch)
            if icds_pct and icds_pct not in ("", "None"):
                pc = str(icds_pct).strip()
                if not pc.endswith("%"):
                    pc += "%"
                if not pc.startswith(("+", "-")):
                    pc = "+" + pc
                parts.append(pc)
            cds_str = f"{icds_v}"
            if parts:
                cds_str = cds_str + " " + " ".join(parts)
            lines.append(f"- **IndoCDS 5yr:** {cds_str}")

    lines.append("")

    # ── Energy ──
    lines.append("## 🛢️ Energy")
    for key, label, prefix in [
        ("Oil(WT)", "Oil WTI", "$"),
        ("Oil(Brn)", "Oil Brent", "$"),
        ("Ntrl Gas", "Nat Gas", "$"),
    ]:
        d = data.get(key)
        if isinstance(d, dict):
            c = d.get("close", "")
            p = get_change(d)
            if c:
                if p:
                    lines.append(f"- **{label}:** {prefix}{c} {p}")
                else:
                    lines.append(f"- **{label}:** {prefix}{c}")

    lines.append("")

    # ── Coal (Barchart) ──
    lines.append("### Coal (Barchart) 🔄")
    coal_nwl = data.get("Coal(Nwl)")
    coal_rot = data.get("Coal(Rot)")

    if isinstance(coal_nwl, dict) and coal_nwl.get("contracts"):
        lines.append("- **Newcastle:**")
        for c in coal_nwl["contracts"]:
            ch = c.get("change", "")
            pc = c.get("change_pct", "")
            parts = []
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
            if parts:
                lines.append(f'  - **{c["month"]}:** {c["price"]} ' + " ".join(parts))
            else:
                lines.append(f'  - **{c["month"]}:** {c["price"]}')

    if isinstance(coal_rot, dict) and coal_rot.get("contracts"):
        lines.append("- **Rotterdam:**")
        for c in coal_rot["contracts"]:
            ch = c.get("change", "")
            pc = c.get("change_pct", "")
            parts = []
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
            if parts:
                lines.append(f'  - **{c["month"]}:** {c["price"]} ' + " ".join(parts))
            else:
                lines.append(f'  - **{c["month"]}:** {c["price"]}')
    lines.append("")

    # ── Metals & Mining ──
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

    # ── Komoditas Lain ──
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

    # ── ETFs & Stocks ──
    lines.append("## 📈 ETFs & Stocks")
    for key, label in [("EIDO", "EIDO"), ("TLKM", "TLKM"), ("EEM", "EEM")]:
        d = data.get(key)
        if isinstance(d, dict):
            c = close_str(d)
            p = get_change(d)
            if c:
                sp = get_point_change(d)
                if p and sp:
                    lines.append(f"- **{label}:** {c} {sp} {p}")
                elif p:
                    lines.append(f"- **{label}:** {c} {p}")
                elif sp:
                    lines.append(f"- **{label}:** {c} {sp}")
                else:
                    lines.append(f"- **{label}:** {c}")
    lines.append("")

    # ── Footer ──
    lines.append("---")
    lines.append("")
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

    if from_cache and os.path.exists(CACHE_JSON):
        print("[Loading from cached screener data...]", file=sys.stderr, flush=True)
        with open(CACHE_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = raw.get("data", {})
        sources = raw.get("sources_used", [])
        ts = raw.get("timestamp", "")
    else:
        data, sources, ts = collect_data()

        # Save raw JSON to cache
        os.makedirs(CACHE_DIR, exist_ok=True)
        raw_out = {
            "timestamp": ts,
            "data": data,
            "sources_used": sources,
        }
        with open(CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(raw_out, f, indent=2, ensure_ascii=False)

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

    # Save report to output files
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n\n[Report saved to {REPORT_MD}]", file=sys.stderr, flush=True)

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
