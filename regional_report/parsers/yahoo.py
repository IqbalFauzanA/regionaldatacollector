"""Yahoo Finance scrapers and API queries."""

import logging
import re
import sys
from bs4 import BeautifulSoup
from .base import fetch, resolve_parser

logger = logging.getLogger(__name__)


def parse_yahoo_finance(ticker, report_label):
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
                report_label: {
                    "close": price,
                    "change": valid_change,
                    "change_pct": valid_pct,
                    "source": "Yahoo Finance",
                }
            }
    except Exception as e:
        print(
            f"  WARN {report_label} (Yahoo): {type(e).__name__}: {str(e)[:60]}",
            file=sys.stderr,
        )
    return result


def parse_yahoo_sector_indices():
    result = {}
    sectors = [
        ("IDX Energy", "IDXENERGY.JK"),
        ("IDX Basic Materials", "IDXBASIC.JK"),
        ("IDX Industrial", "IDXINDUST.JK"),
        ("IDX Consumer Non-Cyclical", "IDXNONCYC.JK"),
        ("IDX Healthcare", "IDXHEALTH.JK"),
        ("IDX Consumer Cyclical", "IDXCYCLIC.JK"),
        ("IDX Technology", "IDXTECHNO.JK"),
        ("IDX Transportation", "IDXTRANS.JK"),
        ("IDX Infrastructure", "IDXINFRA.JK"),
        ("IDX Finance", "IDXFINANCE.JK"),
        ("IDX Banking", "INFOBANK15.JK"),
    ]
    parser_fn = resolve_parser("parse_yahoo_finance", parse_yahoo_finance)
    for report_label, ticker in sectors:
        try:
            parsed = parser_fn(ticker, report_label)
            if parsed and isinstance(parsed, dict):
                result.update(parsed)
        except Exception as e:
            logger.debug("parse_yahoo_sector_indices %s: %s", ticker, e)

    return result


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
