"""Report export helpers."""

import html
import os
import re
from functools import lru_cache

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from .commons import BASE_DIR, OUTPUT_DIR, REPORT_PDF, strip_preview_emoji


@lru_cache(maxsize=1)
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


@lru_cache(maxsize=1)
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


def save_report_pdf(report):
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
            story.append(Paragraph(txt, bullet_styles[level], bulletText="\u2022"))
            continue

        style = (
            styles["italic"]
            if stripped.startswith("_") and stripped.endswith("_")
            else styles["body"]
        )
        txt = markdown_inline_to_reportlab(stripped)
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
    return [save_report_pdf(report)]
