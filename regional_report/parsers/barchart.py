"""Barchart coal contract scraper."""

import logging
from datetime import datetime
from bs4 import BeautifulSoup
from .base import fetch, parse_barchart_price_change

logger = logging.getLogger(__name__)

_BARCHART_MONTH_CODES = ("F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z")
_MONTH_ABBREVIATIONS = (
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
    last_error = None

    for label, root_sym in [("Newcastle", "LQ"), ("Rotterdam", "LU")]:
        contracts = []
        for month_name, code, contract_year in contract_months:
            sym = f"{root_sym}{code}{contract_year:02d}"

            found_row = False
            try:
                resp = fetch(
                    f"https://www.barchart.com/futures/quotes/{sym}/overview",
                    timeout=20,
                    max_retries=2,
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
                last_error = e
                if "AWS WAF Challenge" in str(e):
                    logger.warning(
                        "Barchart blocked by AWS WAF Challenge (HTTP 202) for %s", sym
                    )
                    break

        if contracts:
            result[label] = {
                "contracts": contracts,
                "source": "Barchart",
            }
        elif last_error and "AWS WAF Challenge" in str(last_error):
            break

    if not result and last_error:
        raise last_error

    return result
