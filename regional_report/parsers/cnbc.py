"""CNBC quote scraper."""

import sys
from bs4 import BeautifulSoup
from regional_report.commons import clean_num
from .base import fetch


def parse_cnbc_quote_html(html, report_label):
    """Parse a CNBC quote strip into the report's standard quote fields."""
    soup = BeautifulSoup(html, "lxml")
    price_el = soup.select_one(".QuoteStrip-lastPrice")
    change_el = soup.select_one(
        ".QuoteStrip-changeUp, .QuoteStrip-changeDown, .QuoteStrip-changeFlat"
    )
    if not price_el or not change_el:
        return {}

    values = [
        span.get_text(" ", strip=True).strip().strip("()")
        for span in change_el.find_all("span", recursive=False)
    ]
    values = [value for value in values if value]
    if len(values) < 2:
        return {}

    close = clean_num(price_el.get_text(" ", strip=True))
    change = clean_num(values[0])
    change_pct = clean_num(values[1])
    if not close or not change or not change_pct:
        return {}
    if not change_pct.endswith("%"):
        change_pct = f"{change_pct}%"

    return {
        report_label: {
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "source": "CNBC",
        }
    }


def parse_cnbc_quote_pages(pages):
    """Fetch individual CNBC quote pages and combine their parsed results."""
    results = {}
    for report_label, url in pages:
        try:
            resp = fetch(url)
            results.update(parse_cnbc_quote_html(resp.text, report_label))
        except Exception as e:
            print(
                f"  WARN CNBC {report_label}: {type(e).__name__}: {str(e)[:60]}",
                file=sys.stderr,
            )
    return results
