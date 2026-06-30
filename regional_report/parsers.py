"""Data source parsers and collection orchestration."""

import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, cast
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .commons import (
    CACHE_MAX_AGE_SECONDS,
    MAX_FETCH_WORKERS,
    RETRY_IMPERSONATE,
    clean_num,
    fetch,
    has_css_class,
    is_recent_cached_item,
    is_valid_data,
    normalize_cached_item_timestamp,
    req,
)

logger = logging.getLogger(__name__)


def parse_barchart_price_change(price_raw, chg_raw):
    """Robust parsing for Barchart price and change cells.

    Handles values like '113.65', '113.65 unch', 'unch', '(unch)', '113.65 +0.50',
    and returns (price_str, change_str, change_pct_str) where any may be None.
    """

    def _find_num(x):
        if not x:
            return None
        m = re.search(r"[-+]?\d{1,3}(?:[\d,]*\d)?(?:\.\d+)?", x)
        if not m:
            return None
        return float(m.group(0).replace(",", ""))

    price = _find_num(price_raw)

    chg_num = None
    if chg_raw:
        # explicit numeric change
        explicit = _find_num(chg_raw)
        if explicit is not None:
            chg_num = explicit
        # explicit 'unch' token means change is 0 but not an explicit numeric value
        elif re.search(r"\bunch\b", chg_raw, flags=re.I) or chg_raw.strip().lower() in (
            "unch",
            "(unch)",
            "unch unch",
        ):
            chg_num = 0.0

    # If price exists, compute percentage if possible
    if price is not None:
        if chg_num is None:
            # no reliable change info — treat as implicit 0 (not explicit)
            chg_num = 0.0

        prev_close = price - chg_num
        pct = round((chg_num / prev_close) * 100, 2) if prev_close else 0.0

        price_str = f"{price:.2f}"
        change_str = f"{chg_num:+.2f}" if chg_num is not None else None
        change_pct = f"{pct:+.2f}%"
        return price_str, change_str, change_pct

    # price missing but change present
    if chg_num is not None:
        price_str = None
        change_str = f"{chg_num:+.2f}"
        change_pct = ""
        return price_str, change_str, change_pct

    return None, None, None


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
                # Expected: ICBI|arrow|426.4080|Previous|425.7156|Change|0.6925|Change (%)|0.16
                # PHEI displays Change as an unsigned magnitude and uses the
                # arrow/color for direction, so compute the signed value from
                # the current and previous levels.
                if len(parts) >= 9:
                    close_raw = parts[2]
                    prev_raw = parts[4]
                    chg_raw = parts[6]
                    pct_raw = parts[8]

                    def _to_float(value):
                        cleaned = clean_num(str(value).replace("%", ""))
                        if cleaned is None:
                            return None
                        return float(cleaned)

                    try:
                        close_num = _to_float(close_raw)
                        prev_num = _to_float(prev_raw)
                    except (ValueError, TypeError):
                        close_num = None
                        prev_num = None

                    if close_num is not None and prev_num is not None:
                        chg_num = round(close_num - prev_num, 4)
                        if abs(chg_num) < 0.00005:
                            chg_num = 0.0
                        pct_num = round((chg_num / prev_num) * 100, 2) if prev_num else 0.0
                        result["ICBI"] = {
                            "close": f"{close_num:.4f}",
                            "change": f"{chg_num:+.4f}",
                            "change_pct": f"{pct_num:+.2f}%",
                            "source": "PHEI",
                        }
                    else:
                        result["ICBI"] = {
                            "close": close_raw,
                            "change": clean_num(chg_raw),
                            "change_pct": f"{clean_num(pct_raw)}%",
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
    wanted_names = {
        "crude oil wti": "Oil(WT)",
        "wti crude oil": "Oil(WT)",
        "brent oil": "Oil(Brn)",
        "natural gas": "Ntrl Gas",
        "aluminium": "Aluminium",
        "aluminum": "Aluminium",
        "nickel": "Nickel",
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

            def normalize_header(value: str) -> str:
                value = value.lower().replace(".", " ")
                return re.sub(r"\s+", " ", value).strip()

            normalized_headers = [normalize_header(h) for h in header_texts]

            def find_header_index(accepted):
                return next(
                    (i for i, h in enumerate(normalized_headers) if h in accepted),
                    None,
                )

            name_idx = find_header_index(
                {"name", "contract", "commodity", "instrument", "symbol"}
            )
            last_idx = find_header_index({"last", "price", "close", "ltd"})
            pct_idx = next(
                (i for i, h in enumerate(normalized_headers) if "%" in h), None
            )
            chg_idx = next(
                (
                    i
                    for i, h in enumerate(normalized_headers)
                    if h in {"change", "chg", "change value", "chg value"}
                    and "%" not in h
                ),
                None,
            )

            # A compact sidebar table only has Last and Chg. %, so it cannot
            # supply the absolute move. Accept only the full futures table;
            # missing instruments are handled by the instrument-page fallback.
            if not name_idx or not last_idx or not chg_idx or not pct_idx:
                continue

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
                s = re.sub(r"\s+futures?$", "", s, flags=re.I).strip()
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

                # Use exact canonical names. Fuzzy token matching allowed rows such
                # as Soybean Oil and Dutch TTF Natural Gas to overwrite WTI and
                # Henry Hub Natural Gas respectively.
                canonical_name = re.sub(
                    r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name_norm.lower())
                ).strip()
                code = wanted_names.get(canonical_name)
                if code is None:
                    logger.debug("Commodities: no match for '%s'", name_norm)
                    continue

                if max(last_idx, chg_idx, pct_idx) >= len(cells):
                    continue
                last_txt = cells[last_idx].get_text(strip=True)
                chg_txt = cells[chg_idx].get_text(strip=True)
                pct_txt = cells[pct_idx].get_text(strip=True)

                if not last_txt:
                    continue

                logger.debug(
                    "Commodities: matched %r (canonical=%r, code=%s)",
                    name_norm,
                    canonical_name,
                    code,
                )
                results[code] = {
                    "close": clean_num(last_txt),
                    "change": clean_num(chg_txt) if chg_txt else None,
                    "change_pct": pct_txt,
                    "source": "Investing Futures",
                }
        # Only fall back after every table has been searched. Doing this inside
        # the table loop repeated the same network requests for multi-table pages.
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
            "Nickel": ["https://www.investing.com/commodities/nickel"],
        }
        wanted_codes = dict.fromkeys(wanted_names.values())
        for code in (code for code in wanted_codes if code not in results):
            for url in fallback_urls[code]:
                logger.debug("Commodities: fallback try %s for code %s", url, code)
                parsed = parse_instrument_page(url, "Investing Futures", code)
                value = parsed.get(code)
                if is_valid_data(value):
                    results[code] = value
                    break
    except Exception as e:
        print(f"  WARN Commodities: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return results


# ──────────────── STATE/JSON-BASED PARSERS ────────────────


BLOOMBERG_USDIDR_URL = "https://www.bloomberg.com/quote/USDIDR:CUR"
BLOOMBERG_DXY_URL = "https://www.bloomberg.com/quote/DXY:CUR"
BLOOMBERG_EURUSD_URL = "https://www.bloomberg.com/quote/EURUSD:CUR"
BLOOMBERG_TIN_URL = "https://www.bloomberg.com/quote/LMSNDS03:COM"
BLOOMBERG_METALS_URL = (
    "https://www.bloomberg.com/markets/commodities/futures/metals"
)
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


def parse_bloomberg_quote_html(html, ticker, code):
    """Parse one Bloomberg quote page into the collector's data shape."""
    quote = _next_data_page_props(html).get("quote", {})
    if not isinstance(quote, dict) or quote.get("id") != ticker:
        return {}
    item = _bloomberg_market_item(quote)
    return {code: item} if item else {}


def parse_bloomberg_usdidr_html(html):
    """Parse USD/IDR from Bloomberg's embedded quote payload."""
    return parse_bloomberg_quote_html(html, "USDIDR:CUR", "IDR")


def _parse_bloomberg_sections_html(html, wanted):
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
            code = wanted.get(id)
            if not code:
                continue
            item = _bloomberg_market_item(security)
            if item:
                result[code] = item
    return result


def parse_bloomberg_metals_html(html):
    """Parse the requested rows from Bloomberg's embedded metals tables."""
    return _parse_bloomberg_sections_html(
        html,
        {
            "GC1:COM": "Gold",
            "XAUUSD:CUR": "Gold(Spot)",
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
            "BO1:COM": "SoybeanOil",
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


def _parse_bloomberg_quote(url, ticker, code, label):
    try:
        return parse_bloomberg_quote_html(
            _fetch_bloomberg_html(url), ticker, code
        )
    except Exception as e:
        print(
            f"  WARN Bloomberg {label}: {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
        return {}


def parse_bloomberg_dxy():
    return _parse_bloomberg_quote(BLOOMBERG_DXY_URL, "DXY:CUR", "USDIndx", "DXY")


def parse_bloomberg_eurusd():
    return _parse_bloomberg_quote(
        BLOOMBERG_EURUSD_URL, "EURUSD:CUR", "Euro", "EUR/USD"
    )


def parse_bloomberg_tin():
    return _parse_bloomberg_quote(
        BLOOMBERG_TIN_URL, "LMSNDS03:COM", "Timah", "Tin"
    )


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
    ]
    for name, ticker in sectors:
        try:
            parsed = parse_yahoo_finance(ticker, name)
            if parsed and isinstance(parsed, dict):
                result.update(parsed)
        except Exception as e:
            logger.debug("parse_yahoo_sector_indices %s: %s", ticker, e)

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


def parse_indonesia_cds_payload(data):
    """Calculate Indonesia 5Y CDS change from the latest two dated quotes."""
    if not isinstance(data, dict) or not data.get("success"):
        return {}
    result = data.get("result", {})
    quotes = result.get("quote", {}) if isinstance(result, dict) else {}
    if not isinstance(quotes, dict):
        return {}

    # Keep the last observation for each date, then compare distinct dates.
    by_date = {}
    for quote in quotes.values():
        if not isinstance(quote, dict):
            continue
        date = str(quote.get("DATA_VAL", "")).strip()
        try:
            close_val = quote.get("CLOSE_VAL")
            if not isinstance(close_val, (int, float, str)):
                continue
            value = float(close_val)
        except (TypeError, ValueError):
            continue
        if date:
            by_date[date] = value
    dates = sorted(by_date)
    if not dates:
        return {}

    latest_date = dates[-1]
    latest = by_date[latest_date]
    close = str(result.get("ultimoValore") or f"{latest:.2f}")
    change = ""
    change_pct = ""
    previous_close = ""
    previous_date = ""
    if len(dates) > 1:
        previous_date = dates[-2]
        previous = by_date[previous_date]
        difference = latest - previous
        percent = (difference / previous * 100) if previous else 0.0
        previous_close = f"{previous:.4f}"
        change = f"{difference:+.2f}"
        change_pct = f"{percent:+.2f}%"

    return {
        "IndoCDS 5yr": {
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "date": latest_date,
            "previous_close": previous_close,
            "previous_date": previous_date,
            "source": "WorldGovernmentBonds",
        }
    }


def parse_indonesia_cds():
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
            impersonate=cast(Any, RETRY_IMPERSONATE[0]),
            timeout=20,
        )

        return parse_indonesia_cds_payload(resp.json())
    except Exception as e:
        print(f"  WARN IndoCDS: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


# ──────────────── AMMONIA ────────────────


SUNSIRS_AMMONIA_URL = (
    "https://www.sunsirs.com/m/page/commodity-price-detail/"
    "commodity-price-detail-965.html"
)


def parse_sunsirs_ammonia_html(html):
    """Parse SunSirs' seven-day China liquid-ammonia price series."""
    soup = BeautifulSoup(html, "lxml")
    entries = []
    for row in soup.select("li.zwd_table_li"):
        cells = [item.get_text(" ", strip=True) for item in row.find_all("p")]
        if len(cells) < 3 or cells[0].lower() != "liquid ammonia":
            continue
        try:
            price = float(cells[1].replace(",", ""))
        except ValueError:
            continue
        entries.append({"price": price, "date": cells[2]})

    if not entries:
        return {}
    latest = entries[0]
    change = ""
    change_pct = ""
    previous = next(
        (entry for entry in entries[1:] if entry["date"] != latest["date"]),
        None,
    )
    if previous:
        previous_price = previous["price"]
        difference = latest["price"] - previous_price
        percent = (difference / previous_price * 100) if previous_price else 0.0
        change = f"{difference:+.2f}"
        change_pct = f"{percent:+.2f}%"

    return {
        "Ammonia": {
            "close": f"{latest['price']:.2f}",
            "change": change,
            "change_pct": change_pct,
            "date": latest["date"],
            "previous_date": previous["date"] if previous else "",
            "unit": "RMB/ton",
            "note": f"SunSirs ({latest['date']})",
            "source": "SunSirs",
        }
    }


def parse_ammonia():
    try:
        return parse_sunsirs_ammonia_html(fetch(SUNSIRS_AMMONIA_URL, timeout=30).text)
    except Exception as e:
        print(f"  WARN Ammonia: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


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
                rates.append(m.group(1).replace(".", ""))
        if len(rates) >= 2:
            curr_f = float(rates[0])
            prev_f = float(rates[1])
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


# ──────────────── IDX Property from Yahoo Finance API ────────────────


def parse_yahoo_idx_property():
    """Fetch IDX Property (IDXPROPERT.JK) via Yahoo Finance v8 chart API."""
    result = {}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/IDXPROPERT.JK?interval=1d&range=5d"
        resp = fetch(url, timeout=20)
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


_BARCHART_MONTH_CODES = (
    "F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"
)
_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _barchart_contract_months(as_of=None, count=4):
    """Return consecutive Barchart contract months starting with this month."""
    as_of = as_of or datetime.now()
    start_index = as_of.month - 1
    contracts = []
    for offset in range(count):
        absolute_index = start_index + offset
        month_index = absolute_index % 12
        year = as_of.year + absolute_index // 12
        contracts.append(
            (
                _MONTH_ABBREVIATIONS[month_index],
                _BARCHART_MONTH_CODES[month_index],
                year % 100,
            )
        )
    return contracts


def parse_barchart_coal():
    result = {}
    contract_months = _barchart_contract_months()

    for root_name, root_sym, label in [
        ("Newcastle", "LQ", "Coal(Nwl)"),
        ("Rotterdam", "LU", "Coal(Rot)"),
    ]:
        contracts = []
        for month_name, code, contract_year in contract_months:
            sym = f"{root_sym}{code}{contract_year:02d}"

            found_row = False
            try:
                resp = fetch(
                    f"https://www.barchart.com/futures/quotes/{sym}/overview",
                    timeout=20,
                )
                soup = BeautifulSoup(resp.text, "lxml")
                tables = soup.find_all("table")
                for table in tables:
                    rows = table.find_all("tr")
                    if not rows or len(rows) <= 1:
                        continue
                    # Search all data rows for a matching symbol
                    for row in rows[1:]:
                        cells = row.find_all("td")
                        if len(cells) < 3:
                            continue
                        if cells[0].get_text(strip=True) != sym:
                            continue
                        price_raw = cells[1].get_text(" ", strip=True)
                        chg_raw = cells[2].get_text(" ", strip=True)
                        # normalize common noise
                        price_raw = price_raw.replace("s", "").replace(",", "")
                        chg_raw = chg_raw.replace(",", "")

                        price_str, change_str, change_pct = parse_barchart_price_change(
                            price_raw, chg_raw
                        )

                        if price_str or change_str:
                            contracts.append(
                                {
                                    "month": month_name,
                                    "price": price_str,
                                    "change": change_str,
                                    "change_pct": change_pct,
                                }
                            )
                        found_row = True
                        break
                    if found_row:
                        break
            except Exception as e:
                logger.debug("parse_barchart_coal page exception for %s: %s", sym, e)

        if contracts:
            result[label] = {
                "contracts": contracts,
                "source": "Barchart",
            }

    return result


def parse_bursa_cpo():
    """FCPO Day (T), third displayed row, using its Last Done value."""
    page_url = "https://www.bursamalaysia.com/market_information/derivatives_prices"
    api_url = (
        "https://www.bursamalaysia.com/api/v1/derivatives_prices/"
        "derivatives_prices?code=FCPO&ses=day&per_page=20&page=1"
    )
    try:
        response = fetch(
            api_url,
            headers={"Referer": page_url},
            timeout=30,
        )
        return parse_bursa_cpo_payload(response.json())
    except Exception as e:
        print(f"  WARN Bursa CPO: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
        return {}


def parse_bursa_cpo_payload(payload):
    """Parse Bursa's DataTables JSON; columns follow the page's visible table."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or len(rows) < 3:
        return {}
    row = rows[2]  # third displayed FCPO Day (T) row
    if not isinstance(row, list) or len(row) < 8:
        return {}
    name = BeautifulSoup(str(row[1]), "lxml").get_text(" ", strip=True)
    if name != "FCPO":
        return {}
    try:
        close = float(str(row[6]).replace(",", ""))  # Last Done
        change_text = BeautifulSoup(str(row[7]), "lxml").get_text(" ", strip=True)
        change = float(change_text.replace(",", ""))
    except (TypeError, ValueError):
        return {}
    previous_close = close - change
    percent = (change / previous_close * 100) if previous_close else 0.0
    return {
        "CPO": {
            "close": f"{close:.2f}",
            "change": f"{change:+.2f}",
            "change_pct": f"{percent:+.2f}%",
            "contract": str(row[2]),
            "source": "Bursa Malaysia",
        }
    }


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
        headers = rows[0].find_all(["th", "td"]) if rows else []
        previous_date = headers[2].get_text(strip=True) if len(headers) > 3 else ""
        latest_date = headers[3].get_text(strip=True) if len(headers) > 3 else ""
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
                        "date": latest_date,
                        "previous_date": previous_date,
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


MAJOR_INDEX_KEYS = (
    "Dow",
    "S&P 500",
    "Nasdaq",
    "S&P 500 VIX",
    "DAX",
    "FTSE",
    "CAC",
    "Nikkei",
    "Shanghai",
    "HSI",
    "KOSPI",
)
IDX_INDEX_KEYS = ("IDX", "LQ45", "IDX Kompas 100", "IDX30")
IDX_SECTOR_KEYS = (
    "IDXEnergy",
    "IDX BscMat",
    "IDXIndst",
    "IDXNONCYC",
    "IDXHlthcare",
    "IDXCYCLC",
    "IDX Tech",
    "IDX Transprt",
    "IDX Infra",
    "IDX Finance",
    "IDX Banking",
)
COMMODITY_FUTURES_KEYS = (
    "Oil(WT)",
    "Oil(Brn)",
    "Ntrl Gas",
    "Aluminium",
    "Nickel",
)
US_BOND_KEYS = ("US2Yr", "US5Yr", "US10Yr", "US30Yr")
REQUESTED_SOURCE_BY_KEY = {
    "IDR": "Bloomberg",
    "Gold": "Bloomberg",
    "Gold(Spot)": "Bloomberg",
    "Silver": "Bloomberg",
    "Copper": "Bloomberg",
    "USDIndx": "Bloomberg",
    "Euro": "Bloomberg",
    "Timah": "Bloomberg",
    "Corn": "Bloomberg",
    "Wheat": "Bloomberg",
    "SoybeanOil": "Bloomberg",
    "Ammonia": "SunSirs",
    "CPO": "Bursa Malaysia",
    "KOSPI": "KOSPI 50",
}


def collect_data(cache_raw=None, cache_max_age_seconds=CACHE_MAX_AGE_SECONDS):
    """Run all scrapers, return (data_dict, sources_list, timestamp)."""
    DATA = {}

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    cached_data = (
        cache_raw.get("data", {})
        if isinstance(cache_raw, dict) and isinstance(cache_raw.get("data"), dict)
        else {}
    )
    cache_now = datetime.now()

    def fresh_cached_results(keys):
        results = {}
        if not cached_data or not keys:
            return results
        for key in keys:
            cached_item = cached_data.get(key)
            required_source = REQUESTED_SOURCE_BY_KEY.get(key)
            if required_source and (
                not isinstance(cached_item, dict)
                or cached_item.get("source") != required_source
            ):
                continue
            if is_recent_cached_item(
                cache_raw, cached_item, cache_now, cache_max_age_seconds
            ):
                results[key] = normalize_cached_item_timestamp(cache_raw, cached_item)
        return results

    def run_task(label, fn):
        try:
            return fn()
        except Exception as e:
            log(f"  WARN {label}: {type(e).__name__}: {str(e)[:80]}")
            return {}

    log("Regional Screener -- collecting data...")
    t0 = datetime.now()

    single_pages = [
        ("KOSPI 50", "https://www.investing.com/indices/kospi-50", "KOSPI"),
        (
            "Iron Ore",
            "https://www.investing.com/commodities/iron-ore-62-cfr-futures",
            "Iron Ore 62%",
        ),
        (
            "BCOMIN",
            "https://www.investing.com/indices/bloomberg-industrial-metals",
            "BCOMIN",
        ),
    ]
    tasks = [
        ("Coal from Barchart", parse_barchart_coal, ("Coal(Nwl)", "Coal(Rot)")),
        ("IDX Sector Indices", parse_yahoo_sector_indices, IDX_SECTOR_KEYS),
        ("JISDOR", parse_jisdor, ("Jisdor",)),
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
            MAJOR_INDEX_KEYS,
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
            IDX_INDEX_KEYS,
        ),
        ("SunSirs Woodpulp", parse_sunsirs_woodpulp, ("Woodpulp",)),
        ("Commodities", parse_commodities_futures, COMMODITY_FUTURES_KEYS),
        ("Bloomberg USD/IDR", parse_bloomberg_usdidr, ("IDR",)),
        ("Bloomberg DXY", parse_bloomberg_dxy, ("USDIndx",)),
        ("Bloomberg EUR/USD", parse_bloomberg_eurusd, ("Euro",)),
        ("Bloomberg Tin", parse_bloomberg_tin, ("Timah",)),
        (
            "Bloomberg Metals",
            parse_bloomberg_metals,
            ("Gold", "Gold(Spot)", "Silver", "Copper"),
        ),
        (
            "Bloomberg Agriculture",
            parse_bloomberg_agriculture,
            ("Corn", "Wheat", "SoybeanOil"),
        ),
        *[
            (
                label,
                lambda url=url, label=label, code=code: parse_instrument_page(
                    url, label, code
                ),
                (code,),
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
            US_BOND_KEYS,
        ),
        ("Indo Bonds", parse_indonesia_bonds, ("Indo10Yr",)),
        ("PHEI (ICBI + Indo10Yr)", parse_phei, ("ICBI", "Indo10Yr")),
        *[
            (
                code,
                lambda ticker=ticker, code=code: parse_yahoo_finance(ticker, code),
                (code,),
            )
            for ticker, code in [
                ("EIDO", "EIDO"),
                ("EEM", "EEM"),
                ("TLK", "TLKM"),
            ]
        ],
        ("IndoCDS", parse_indonesia_cds, ("IndoCDS 5yr",)),
        ("SunSirs Ammonia", parse_ammonia, ("Ammonia",)),
        ("IDX Property", parse_yahoo_idx_property, ("IDX Property",)),
        ("Bursa CPO", parse_bursa_cpo, ("CPO",)),
    ]

    results_by_index = {}
    tasks_to_run = []
    cached_task_count = 0
    for idx, (label, fn, expected_keys) in enumerate(tasks):
        cached_results = fresh_cached_results(expected_keys)
        if expected_keys and len(cached_results) == len(expected_keys):
            results_by_index[idx] = cached_results
            cached_task_count += 1
            log(f"  cached: {label}")
            continue
        tasks_to_run.append((idx, label, fn))

    if tasks_to_run:
        log(
            f"Submitting {len(tasks_to_run)} scraper tasks with "
            f"{MAX_FETCH_WORKERS} workers..."
        )
        if cached_task_count:
            log(f"Using cache for {cached_task_count} scraper tasks.")
        with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(run_task, label, fn): idx
                for idx, label, fn in tasks_to_run
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
    else:
        log(f"All {len(tasks)} scraper tasks satisfied from cache.")

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
            r = fetch(url, timeout=15)
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
