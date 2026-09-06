"""Bloomberg scrapers and Next.js page props extraction."""

import json
import sys
from bs4 import BeautifulSoup
from regional_report.commons import clean_num
from .base import fetch

BLOOMBERG_USDIDR_URL = "https://www.bloomberg.com/quote/USDIDR:CUR"
BLOOMBERG_DXY_URL = "https://www.bloomberg.com/quote/DXY:CUR"
BLOOMBERG_EURUSD_URL = "https://www.bloomberg.com/quote/EURUSD:CUR"
BLOOMBERG_TIN_URL = "https://www.bloomberg.com/quote/LMSNDS03:COM"
BLOOMBERG_WTI_URL = "https://www.bloomberg.com/quote/CL1:COM"
BLOOMBERG_BRENT_URL = "https://www.bloomberg.com/quote/CO1:COM"
BLOOMBERG_METALS_URL = "https://www.bloomberg.com/markets/commodities/futures/metals"
BLOOMBERG_AGRICULTURE_URL = (
    "https://www.bloomberg.com/markets/commodities/futures/agriculture"
)


def _fetch_bloomberg_html(url):
    """Use Bloomberg's currently supported browser profile and bounded retries."""
    return fetch(
        url,
        impersonate="chrome",
        timeout=20,
        max_retries=2,
    ).text


def _next_data_page_props(html):
    """Return pageProps from a Next.js page, or an empty dict."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {}
    try:
        payload = json.loads(script.string)
    except (TypeError, ValueError):
        return {}
    page_props = payload.get("props", {}).get("pageProps", {})
    return page_props if isinstance(page_props, dict) else {}


def _decimal(value, places=2, signed=False):
    """Normalize a Bloomberg numeric value for report output."""
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return ""
    pattern = f"{{:{'+' if signed else ''}.{places}f}}"
    return pattern.format(number)


def _bloomberg_market_item(security):
    if not isinstance(security, dict) or security.get("price") in (None, ""):
        return None
    return {
        "close": clean_num(str(security["price"])),
        "change": _decimal(security.get("priceChange1Day"), signed=True),
        "change_pct": (
            f"{_decimal(security.get('percentChange1Day'), signed=True)}%"
            if security.get("percentChange1Day") is not None
            else ""
        ),
        "source": "Bloomberg",
        "ticker": security.get("id", ""),
        "unit": security.get("commodityUnits") or security.get("issuedCurrency", ""),
        "last_update": security.get("lastUpdate", ""),
    }


def parse_bloomberg_quote_html(html, ticker, report_label):
    """Parse one Bloomberg quote page into the collector's data shape."""
    quote = _next_data_page_props(html).get("quote", {})
    if not isinstance(quote, dict) or quote.get("id") != ticker:
        return {}
    item = _bloomberg_market_item(quote)
    return {report_label: item} if item else {}


def parse_bloomberg_usdidr_html(html):
    """Parse USD/IDR from Bloomberg's embedded quote payload."""
    return parse_bloomberg_quote_html(html, "USDIDR:CUR", "USD/IDR")


def _parse_bloomberg_sections_html(html, labels_by_ticker):
    """Parse selected ticker rows from a Bloomberg section-front page."""
    page_props = _next_data_page_props(html)
    sections = (
        page_props.get("sectionFront", {})
        .get("sectionFrontTab", {})
        .get("sections", [])
    )
    result = {}
    if not isinstance(sections, list):
        return result
    for section in sections:
        if not isinstance(section, dict):
            continue
        for security in section.get("securities", []):
            if not isinstance(security, dict):
                continue
            id = security.get("id")
            if not isinstance(id, str):
                continue
            report_label = labels_by_ticker.get(id)
            if not report_label:
                continue
            item = _bloomberg_market_item(security)
            if item:
                result[report_label] = item
    return result


def parse_bloomberg_metals_html(html):
    """Parse the requested rows from Bloomberg's embedded metals tables."""
    return _parse_bloomberg_sections_html(
        html,
        {
            "GC1:COM": "Gold",
            "XAUUSD:CUR": "Gold (XAU/USD)",
            "SI1:COM": "Silver",
            "HG1:COM": "Copper",
        },
    )


def parse_bloomberg_agriculture_html(html):
    """Parse Corn, Wheat, and Soybean Oil from Bloomberg agriculture."""
    return _parse_bloomberg_sections_html(
        html,
        {
            "C 1:COM": "Corn",
            "W 1:COM": "Wheat",
            "BO1:COM": "Soybean Oil",
        },
    )


def parse_bloomberg_usdidr():
    try:
        return parse_bloomberg_usdidr_html(_fetch_bloomberg_html(BLOOMBERG_USDIDR_URL))
    except Exception as e:
        print(
            f"  WARN Bloomberg USD/IDR: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
        return {}


def _parse_bloomberg_quote(url, ticker, label):
    try:
        return parse_bloomberg_quote_html(_fetch_bloomberg_html(url), ticker, label)
    except Exception as e:
        print(
            f"  WARN Bloomberg {label}: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
        return {}


def parse_bloomberg_dxy():
    return _parse_bloomberg_quote(BLOOMBERG_DXY_URL, "DXY:CUR", "DXY")


def parse_bloomberg_eurusd():
    return _parse_bloomberg_quote(BLOOMBERG_EURUSD_URL, "EURUSD:CUR", "EUR/USD")


def parse_bloomberg_tin():
    return _parse_bloomberg_quote(BLOOMBERG_TIN_URL, "LMSNDS03:COM", "Timah")


def parse_bloomberg_wti():
    return _parse_bloomberg_quote(BLOOMBERG_WTI_URL, "CL1:COM", "Oil WTI")


def parse_bloomberg_brent():
    return _parse_bloomberg_quote(BLOOMBERG_BRENT_URL, "CO1:COM", "Oil Brent")


def parse_bloomberg_metals():
    try:
        return parse_bloomberg_metals_html(_fetch_bloomberg_html(BLOOMBERG_METALS_URL))
    except Exception as e:
        print(
            f"  WARN Bloomberg metals: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
        return {}


def parse_bloomberg_agriculture():
    try:
        return parse_bloomberg_agriculture_html(
            _fetch_bloomberg_html(BLOOMBERG_AGRICULTURE_URL)
        )
    except Exception as e:
        print(
            f"  WARN Bloomberg agriculture: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
        return {}
